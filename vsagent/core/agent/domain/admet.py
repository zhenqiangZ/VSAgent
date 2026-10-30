"""类药性预筛：只做**规则层**的 ADME。

六段流程里这一步发生在对接之后、买之前：算得再好的位姿，如果分子根本不溶、透不过膜、
或者带一个注定被代谢掉的结构警报，那它进不了下一轮。

**为什么只做规则层。** 预测模型（ADMET-AI 这类）要单独的环境与权重，装法得像 DTA 一样
走子进程；而规则层全是描述符加减乘除，装好 RDKit 就能算、结果可复现、每个数都能追到
公式。所以这里给的每个端点都带 `source="rule"`，manifest 里连公式与系数一起写下——
**用户看到的是一个能复核的数，不是一个神秘的分数**。

**做不了的端点明确标 `unsupported`，不硬编一个数。** hPPB（人血浆蛋白结合）与 CYP 抑制
都需要模型，规则层给不出可信值。把它们写成「不支持」比写一个看着像结果的数字诚实：
后者会被下游当成输入，而没有人会去核对一个已经算出来的数。

**每个端点都要能说清「这是算的还是没算」。** 返回的产物里 `columns` 把每一列的来源
与公式都列出来；缺输入（比如分子解析失败）时那一格留空，不补默认值。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Sequence

from ..ledger import LigandSetHandle, SessionLedger, expect_handle, fingerprint_path
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import ligand, products
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem

__all__ = [
    "AdmetTool",
    "admet_dir",
    "domain_tools",
    "esol",
]

ADMET_DIR = "admet"
MANIFEST_FILE = ligand.MANIFEST_FILE
CSV_FILE = ligand.CSV_FILE
PARAMS_FILE = products.PARAMS_FILE

MAX_MOLECULES = 5000
MAX_LISTED = 5

ENDPOINTS = ("solubility", "veber", "bbb", "cyp")
DEFAULT_ENDPOINTS = "solubility,veber,bbb"
UNSUPPORTED = ("hppb", "cyp_inhibition")

# 每条规则都带公式：结果要能复核，公式就得和数字放在一起。
ESOL_FORMULA = "logS = 0.16 − 0.63·cLogP − 0.0062·MW + 0.066·RB − 0.74·AP"
VEBER_LIMITS = {"rotatable": 10, "tpsa": 140.0}
BBB_LIMITS = {"tpsa": 90.0, "hbd": 3, "mw": 450.0, "logp_low": 1.0, "logp_high": 3.0}

# CYP 相关的警示子结构。它们不是「一定被代谢」，而是「常见代谢热点」——
# 返回里带的是一段能看懂的说明，不是「风险 0.73」这种没法追问的数。
CYP_ALERTS: tuple[tuple[str, str, str], ...] = (
    ("aniline", "[NX3;H2,H1;!$(NC=O)]-c", "苯胺/芳香胺：易氧化，常见于 CYP 底物"),
    ("nitro_aromatic", "O=[N+]([O-])-c", "芳香硝基：还原酶代谢，可能生成活性中间体"),
    ("quinone", "O=C1C=CC(=O)C=C1", "醌：氧化还原循环，常为假阳性来源"),
    ("catechol", "Oc1ccccc1O", "邻苯二酚：快速结合/氧化"),
    ("michael_acceptor", "C=CC=O", "α,β-不饱和羰基：共价结合风险"),
    ("hydrazine", "[NX3]-[NX3]", "肼：代谢活化"),
)


def admet_dir() -> Path:
    """类药性产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / ADMET_DIR


def esol(logp: float, mw: float, rotatable: int, aromatic_atoms: int, heavy_atoms: int) -> float:
    """Delaney (ESOL) 的水溶解度估算，单位 log10(mol/L)。

    `AP` 是芳香原子占比。**它必须用重原子数当分母**：用原子总数会让「同一个分子加不加氢」
    得到不同的溶解度，而溶解度跟氢原子上不上坐标没有关系。
    """
    ap = aromatic_atoms / heavy_atoms if heavy_atoms else 0.0
    return 0.16 - 0.63 * logp - 0.0062 * mw + 0.066 * rotatable - 0.74 * ap


def _solubility_class(logs: float) -> str:
    """按 logS 分档。−4 大致对应 100 µM，是「能配成母液」的常用门槛。"""
    if logs >= -2:
        return "高"
    if logs >= -4:
        return "中"
    if logs >= -6:
        return "低"
    return "极低"


def _bbb_class(props: dict[str, float]) -> tuple[str, str]:
    """BBB 透过性的启发式判断。返回 `(档位, 依据)`。

    **这是启发式，不是模型**：四项都满足才说「可能」，一项明显越界就说「不太可能」，
    其余一律「边界」。返回依据是为了让用户知道是被哪一项挡下的。
    """
    reasons: list[str] = []
    if props["tpsa"] > 120:
        reasons.append(f"TPSA {props['tpsa']:.0f} > 120")
    if props["hbd"] > 5:
        reasons.append(f"HBD {props['hbd']:.0f} > 5")
    if props["mw"] > 500:
        reasons.append(f"MW {props['mw']:.0f} > 500")
    if reasons:
        return "不太可能", "；".join(reasons)
    ok = (
        props["tpsa"] < BBB_LIMITS["tpsa"]
        and props["hbd"] <= BBB_LIMITS["hbd"]
        and props["mw"] < BBB_LIMITS["mw"]
        and BBB_LIMITS["logp_low"] <= props["logp"] <= BBB_LIMITS["logp_high"]
    )
    if ok:
        return "可能", "TPSA/HBD/MW/logP 四项都在常见的中枢药物区间内"
    return "边界", "有一项跳出常见区间，但没到明显排除的程度"


def _cyp_alerts(mol: Any, catalogs: dict[str, Any]) -> list[str]:
    from rdkit import Chem

    hits: list[str] = []
    for name, smarts, _note in CYP_ALERTS:
        pattern = catalogs.get(name)
        if pattern is None:
            pattern = Chem.MolFromSmarts(smarts)
            catalogs[name] = pattern
        if pattern is not None and mol.HasSubstructMatch(pattern):
            hits.append(name)
    return hits


def _csv_text(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in columns})
    return buffer.getvalue()


def _render(handle: str, rows: Sequence[dict[str, Any]], target: Path,
            endpoints: Sequence[str]) -> str:
    lines = [f"类药性预筛完成，登记为 {handle}。"]
    lines.append(f"端点：{'、'.join(endpoints)}（全部来自规则层，公式写在产物里）。")
    if "solubility" in endpoints:
        values = [row["esol_logs"] for row in rows if isinstance(row.get("esol_logs"), float)]
        if values:
            low = sum(1 for value in values if value < -6)
            lines.append(
                f"溶解度（ESOL）：中位数 {sorted(values)[len(values) // 2]:.2f} logS，"
                f"{low} 个低于 −6（基本不溶）。"
            )
    if "veber" in endpoints:
        passed = sum(1 for row in rows if row.get("veber_pass") == "是")
        lines.append(f"Veber 规则（可旋转 ≤ 10 且 TPSA ≤ 140）：{passed}/{len(rows)} 通过。")
    if "bbb" in endpoints:
        likely = sum(1 for row in rows if row.get("bbb_class") == "可能")
        unlikely = sum(1 for row in rows if row.get("bbb_class") == "不太可能")
        lines.append(f"血脑屏障（启发式）：可能 {likely}、不太可能 {unlikely}、其余边界。")
    if "cyp" in endpoints:
        flagged = [row for row in rows if row.get("cyp_alerts")]
        lines.append(f"CYP 警示子结构：{len(flagged)}/{len(rows)} 个分子命中。")
        for row in flagged[:MAX_LISTED]:
            lines.append(f"  · {row['id']}：{row['cyp_alerts']}")
    lines.append(
        "未支持：hPPB、CYP 抑制——规则层给不出可信值，需要模型层（将来像 DTA 一样走子进程）。"
    )
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：把这张表和候选表一起看，或 prepare_ligands(ligands={handle})。")
    return "\n".join(lines)


class AdmetTool(Tool):
    """按规则层给一批分子算溶解度、Veber、BBB 与 CYP 警示。"""

    spec = ToolSpec(
        name="predict_admet",
        description=(
            "给一批分子算**类药性**（规则层）：ESOL 水溶解度、Veber 口服规则、"
            "血脑屏障启发式、CYP 警示子结构。产出一份配体集（stage=admet），"
            "原有 SMILES 与 id 不变，多出几列数值。" + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "**只做规则层**：每个数都能追到公式（写在产物 manifest 里），"
            "不是模型预测。hPPB 与 CYP 抑制**明确标为不支持**——规则层给不出可信值，"
            "宁可说没有也不编一个数。\n"
            "溶解度单位是 log10(mol/L)，越低越难溶；Veber 只看「可旋转键 ≤ 10 且 TPSA ≤ 140」。"
            "输入里解析失败的分子该行留空，不补默认值。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ligands": {"type": "string", "description": "配体集 handle"},
                "endpoints": {
                    "type": "string",
                    "description": (
                        f"算哪些端点，逗号分隔，默认 {DEFAULT_ENDPOINTS}；"
                        f"可选 {', '.join(ENDPOINTS)}"
                    ),
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "ligands"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：predict_admet 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("ligands") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 ligands。先 import_ligandset 或 list_workspace。", failed=True
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, LigandSetHandle, "ligands")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        requested = [
            piece.strip().lower()
            for piece in str(arguments.get("endpoints") or DEFAULT_ENDPOINTS)
            .replace("，", ",")
            .split(",")
            if piece.strip()
        ]
        unknown = [name for name in requested if name not in ENDPOINTS]
        if unknown:
            return ToolResult(
                output=(
                    f"不认识的端点：{', '.join(unknown)}。可用的：{', '.join(ENDPOINTS)}；"
                    f"hPPB 与 CYP 抑制属于 {', '.join(UNSUPPORTED)}，这个工具不提供。"
                ),
                failed=True,
            )
        if not requested:
            return ToolResult(output="参数 endpoints 是空的：至少给一个端点。", failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            items, _kind = ligand._source_cleaned(entry)
        except ligand.LigandError as error:
            return ToolResult(output=str(error), failed=True)
        if not items:
            return ToolResult(output=f"{entry.handle} 里没有可算的分子。", failed=True)
        batch = items[:MAX_MOLECULES]
        remaining = len(items) - len(batch)

        from rdkit import Chem, rdBase
        from rdkit.Chem import Descriptors, Lipinski

        columns = ["esol_logs", "esol_class", "veber_pass", "veber_note",
                   "bbb_class", "bbb_note", "cyp_alerts"]
        rows: list[dict[str, Any]] = []
        with rdBase.BlockLogs():
            catalogs: dict[str, Any] = {}
            for item in batch:
                row: dict[str, Any] = {
                    "id": item.id,
                    "name": item.name,
                    "smiles": item.cleaned.smiles,
                    "input_smiles": item.cleaned.input_smiles,
                    "heavy_atoms": item.cleaned.heavy_atoms,
                    "source_index": item.source_index,
                }
                mol = Chem.MolFromSmiles(item.cleaned.smiles)
                if mol is None:
                    # 解析失败就留空：这一行能进产物，但每个端点的格子都是空的。
                    rows.append(row)
                    continue
                props = {
                    "mw": float(Descriptors.MolWt(mol)),
                    "logp": float(Descriptors.MolLogP(mol)),
                    "tpsa": float(Descriptors.TPSA(mol)),
                    "hbd": float(Lipinski.NumHDonors(mol)),
                    "hba": float(Lipinski.NumHAcceptors(mol)),
                    "rotb": float(Lipinski.NumRotatableBonds(mol)),
                }
                if "solubility" in requested:
                    logs = esol(
                        props["logp"],
                        props["mw"],
                        int(props["rotb"]),
                        sum(1 for atom in mol.GetAtoms() if atom.GetIsAromatic()),
                        mol.GetNumHeavyAtoms(),
                    )
                    row["esol_logs"] = round(logs, 3)
                    row["esol_class"] = _solubility_class(logs)
                if "veber" in requested:
                    failed: list[str] = []
                    if props["rotb"] > VEBER_LIMITS["rotatable"]:
                        failed.append(f"可旋转 {props['rotb']:.0f} > 10")
                    if props["tpsa"] > VEBER_LIMITS["tpsa"]:
                        failed.append(f"TPSA {props['tpsa']:.0f} > 140")
                    row["veber_pass"] = "否" if failed else "是"
                    row["veber_note"] = "；".join(failed)
                if "bbb" in requested:
                    label, note = _bbb_class(props)
                    row["bbb_class"] = label
                    row["bbb_note"] = note
                if "cyp" in requested:
                    row["cyp_alerts"] = ",".join(_cyp_alerts(mol, catalogs))
                rows.append(row)

        kept = [row for row in rows if row.get("smiles")]
        if not kept:
            return ToolResult(output="一条都解析不了，算不出任何端点。", failed=True)

        # 每列的来源：算出来的写 rule + 公式，没做的写 unsupported。
        column_sources: dict[str, dict[str, str]] = {
            "esol_logs": {"source": "rule", "method": ESOL_FORMULA},
            "esol_class": {"source": "rule", "method": "logS ≥ −2 高 / ≥ −4 中 / ≥ −6 低 / 其余极低"},
            "veber_pass": {
                "source": "rule",
                "method": "可旋转 ≤ 10 且 TPSA ≤ 140（Veber 2002）",
            },
            "veber_note": {"source": "rule", "method": "未通过的那几项"},
            "bbb_class": {
                "source": "rule",
                "method": "TPSA<90、HBD≤3、MW<450、logP 1–3 全中＝可能；TPSA>120 或 HBD>5 或 MW>500＝不太可能",
            },
            "bbb_note": {"source": "rule", "method": "判断依据"},
            "cyp_alerts": {"source": "rule", "method": "6 条常见代谢热点的 SMARTS"},
            "hppb": {"source": "unsupported", "method": "需要预测模型，规则层不提供"},
            "cyp_inhibition": {"source": "unsupported", "method": "需要预测模型，规则层不提供"},
        }
        params = {
            "source_handle": str(entry.handle),
            "endpoints": requested,
            "formula_version": "2026-10",
        }
        stem = str(arguments.get("name") or "admet")
        try:
            target, reused = products.content_dir(
                admet_dir(), stem, params, (MANIFEST_FILE, CSV_FILE, PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="ligandset", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("stage") or "") == "admet":
                    return ToolResult(
                        output=_render(str(candidate.handle), [], target, requested),
                        meta={"handle": str(candidate.handle), "reused": True,
                              "count": (candidate.meta or {}).get("count", 0)},
                    )

        manifest = {
            "kind": "ligandset",
            "stage": "admet",
            "tool": "predict_admet",
            "source": {"handle": str(entry.handle), "count": len(items)},
            "endpoints": requested,
            "unsupported": list(UNSUPPORTED),
            "formula_version": params["formula_version"],
            "columns": column_sources,
            "counts": {"input": len(items), "scored": len(kept), "remaining": remaining},
        }
        csv_columns = tuple(ligand.CSV_COLUMNS) + tuple(columns)
        try:
            products.write_text_atomic(target / CSV_FILE, _csv_text(kept, csv_columns))
            products.write_json_atomic(target / MANIFEST_FILE, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写产物失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="ligandset", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("stage") or "") == "admet":
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "ligandset",
                path=target,
                summary=f"类药性预筛 · {len(kept)} 个 · {'/'.join(requested)}",
                fingerprint=fingerprint,
                parents=[entry.handle],
                meta={
                    "kind": "ligandset",
                    "stage": "admet",
                    "tool": "predict_admet",
                    "endpoints": requested,
                    "unsupported": list(UNSUPPORTED),
                    "columns": column_sources,
                    "count": len(kept),
                    "file": CSV_FILE,
                    "manifest": MANIFEST_FILE,
                },
                # 配体库跨靶点：换受体时不该被搁置。
                link_current_receptor=False,
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render(handle, kept, target, requested),
            meta={"handle": handle, "count": len(kept), "reused": False,
                  "unsupported": list(UNSUPPORTED)},
        )


def domain_tools() -> list[Tool]:
    return [AdmetTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    main()
