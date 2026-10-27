"""构效关系：骨架分组、匹配分子对、类似物枚举、实验回填。

这一层回答的是「下一轮该改哪里」：哪些分子属于同一个骨架（先看骨架再看单个分子）、
哪一对只差一个基团而活性差了多少（匹配分子对，SAR 里最硬的证据）、把骨架与替换基团
重新组合能生出哪些没测过的分子、以及用户测完回来怎么把数据接上。

**四个工具的输入都带分数或结构，不重新打分。** 分数从候选表来（`rank_candidates`
的产物），结构与 R 基用 RDKit 拆；这一层不做预测，只做整理与组合。

**实验数据是唯一收路径的地方之一。** `record_assay` 要读用户手里的 CSV——那是模型编不
出来的东西。其余三个仍只收 handle。
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    ArtifactHandle,
    LigandSetHandle,
    SessionLedger,
    expect_handle,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import export, ligand, products
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem

__all__ = [
    "EnumerateAnalogsTool",
    "MatchedPairsTool",
    "RecordAssayTool",
    "ScaffoldAnalysisTool",
    "domain_tools",
    "sar_dir",
]

SAR_DIR = "sar"
MANIFEST_FILE = "manifest.json"
PARAMS_FILE = products.PARAMS_FILE
MAX_TOP = 200
MAX_MOLECULES = 5000
MAX_ANALOGS = 2000
ACYCLIC = "acyclic"


def sar_dir() -> Path:
    """SAR 产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / SAR_DIR


# ---------------------------------------------------------------------------
# 读来源
# ---------------------------------------------------------------------------
def _candidate_rows(entry: Any) -> tuple[list[dict[str, Any]], str, bool]:
    """候选表 → `([{id,name,smiles,score}], 分数列名, 是否越高越好)`。"""
    meta = dict(entry.meta or {})
    column = str(meta.get("score_column") or "")
    payload = products.read_json(Path(str(entry.path)) / "candidates.json")
    rows = payload.get("rows") or []
    if not column:
        # 合并后的候选表没有单一分数列：用共识名次，越低越好。
        column = "consensus_rank"
        records = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "smiles": str(item.get("smiles") or ""),
                "score": float(item.get("consensus_rank") or 0.0),
            }
            for item in rows
        ]
        return records, column, False
    records = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "smiles": str(item.get("smiles") or ""),
            "score": float((item.get("scores") or {}).get(column) or 0.0),
        }
        for item in rows
        if (item.get("scores") or {}).get(column) is not None
    ]
    return records, column, bool(meta.get("higher_is_better", True))


def _ligandset_rows(entry: Any) -> list[dict[str, Any]]:
    try:
        items, _kind = ligand._source_cleaned(entry)
    except ligand.LigandError as error:
        raise ToolError(str(error)) from error
    return [
        {
            "id": item.id,
            "name": item.name,
            "smiles": item.cleaned.smiles,
            "score": 0.0,
        }
        for item in items
        if item.cleaned.smiles
    ]


def _rows_of(entry: Any) -> tuple[list[dict[str, Any]], str, bool]:
    """候选表与配体集都收：前者带分数，后者按出现顺序。"""
    kind = getattr(entry.handle, "kind", "")
    if kind == "artifact":
        return _candidate_rows(entry)
    return _ligandset_rows(entry), "", True


# ---------------------------------------------------------------------------
# 骨架
# ---------------------------------------------------------------------------
def scaffold_of(mol: Any, level: str) -> str:
    """Murcko 骨架。无环分子返回空串（调用方把它归成 `acyclic`）。"""
    from rdkit.Chem.Scaffolds import MurckoScaffold

    try:
        scaffold = MurckoScaffold.GetScaffoldForMol(mol)
        if level == "generic":
            scaffold = MurckoScaffold.MakeScaffoldGeneric(scaffold)
        return MurckoScaffold.MurckoScaffoldSmiles(mol=scaffold)
    except Exception:
        return ""


def _score_key(row: dict[str, Any], higher_is_better: bool) -> float:
    value = row.get("score")
    if not isinstance(value, (int, float)):
        return math.inf
    return -float(value) if higher_is_better else float(value)


def _render_scaffolds(handle: str, groups: Sequence[dict[str, Any]],
                      total: int, small: int, target: Path) -> str:
    lines = [f"骨架分组完成，登记为 {handle}。"]
    lines.append(f"{total} 个分子分成 {len(groups)} 个骨架组"
                 + (f"；另有 {small} 个分子属于小到不单列的组" if small else "") + "。")
    for index, group in enumerate(groups, start=1):
        label = group["scaffold"] or ACYCLIC
        lines.append(
            f"  {index}. {label}  ×{group['size']}  代表 {group['representative']['id']}"
        )
    lines.append(f"产物目录：{target}")
    lines.append("下一步：enumerate_analogs(scaffold=..., group=...) 用这些骨架生成新分子。")
    return "\n".join(lines)


class ScaffoldAnalysisTool(Tool):
    """按 Murcko 骨架把一批分子分组。"""

    spec = ToolSpec(
        name="scaffold_analysis",
        description=(
            "按 **Murcko 骨架**把一批分子分组：每个骨架下有多少个、代表分子是谁"
            "（组内分数最好的那个）。" + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "输入可以是候选表（带分数，代表分子按分数取）或配体集（按出现顺序）。"
            "`level=murcko` 保留原子的元素类型，`generic` 把杂原子抹成碳（看的是环的拓扑）。"
            "无环分子单独归到 acyclic——它们没有骨架，混进别的组会让那一组失去意义。\n"
            "小于 min_size 的组不单列但仍计数：一个骨架只有 1 个分子时，它说明不了什么，"
            "可它确实占了库里的位置。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "候选表或配体集的 handle"},
                "level": {"type": "string", "description": "murcko（默认）/ generic"},
                "min_size": {
                    "type": "integer",
                    "description": "小于它的组不单列，默认 2",
                },
                "top": {
                    "type": "integer",
                    "description": f"最多列几组，默认 20，上限 {MAX_TOP}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    )
    primary_argument = "source"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：scaffold_analysis 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("source") or "").strip()
        if not raw:
            return ToolResult(output="缺少参数 source。先 list_workspace。", failed=True)
        try:
            entry = ledger.get(raw)
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        level = str(arguments.get("level") or "murcko").strip().lower()
        if level not in ("murcko", "generic"):
            return ToolResult(
                output=f"参数 level 只能是 murcko / generic（现在是 {level}）", failed=True
            )
        min_size = arguments.get("min_size", 2)
        top = arguments.get("top", 20)
        for name, value in (("min_size", min_size), ("top", top)):
            if isinstance(value, bool) or not isinstance(value, int):
                return ToolResult(output=f"参数 {name} 应当是整数", failed=True)
        if min_size < 1 or top < 1 or top > MAX_TOP:
            return ToolResult(
                output=f"min_size ≥ 1、top 在 1–{MAX_TOP} 之间（现在是 {min_size}/{top}）",
                failed=True,
            )

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            rows, column, higher = _rows_of(entry)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        rows = [row for row in rows[:MAX_MOLECULES] if row["smiles"]]
        if not rows:
            return ToolResult(output=f"{entry.handle} 里没有带结构的分子。", failed=True)

        from rdkit import Chem, rdBase

        groups: dict[str, list[dict[str, Any]]] = {}
        with rdBase.BlockLogs():
            for row in rows:
                mol = Chem.MolFromSmiles(row["smiles"])
                if mol is None:
                    continue
                scaffold = scaffold_of(mol, level) or ACYCLIC
                groups.setdefault(scaffold, []).append(row)
        if not groups:
            return ToolResult(output="一条都解析不了，分不出骨架。", failed=True)

        ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
        listed: list[dict[str, Any]] = []
        small = 0
        for scaffold, members in ordered:
            if len(members) < min_size:
                small += len(members)
                continue
            if len(listed) >= top:
                small += len(members)
                continue
            representative = min(members, key=lambda row: _score_key(row, higher))
            listed.append(
                {
                    "scaffold": "" if scaffold == ACYCLIC else scaffold,
                    "size": len(members),
                    "representative": representative,
                    "members": [row["id"] for row in members],
                }
            )

        params = {
            "source": str(entry.handle),
            "level": level,
            "min_size": min_size,
            "top": top,
            "score_column": column,
        }
        stem = str(arguments.get("name") or "scaffolds")
        try:
            target, reused = products.content_dir(
                sar_dir() / "scaffolds", stem, params, (MANIFEST_FILE, PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="artifact", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("kind") or "") == "scaffolds":
                    cached = list((candidate.meta or {}).get("groups") or [])
                    return ToolResult(
                        output=_render_scaffolds(
                            str(candidate.handle), cached,
                            int((candidate.meta or {}).get("total") or 0),
                            int((candidate.meta or {}).get("small") or 0), target,
                        ),
                        meta={"handle": str(candidate.handle), "reused": True},
                    )

        payload = {
            "kind": "scaffolds",
            "source": str(entry.handle),
            "level": level,
            "score_column": column,
            "total": len(rows),
            "small": small,
            "groups": listed,
        }
        try:
            products.write_json_atomic(target / MANIFEST_FILE, payload)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写产物失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="artifact", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("kind") or "") == "scaffolds":
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "artifact",
                path=target,
                summary=f"骨架分组 · {len(listed)} 组 · {len(rows)} 个分子",
                fingerprint=fingerprint,
                parents=[entry.handle],
                meta={
                    "kind": "scaffolds",
                    "stage": "scaffolds",
                    "tool": "scaffold_analysis",
                    "source": str(entry.handle),
                    "level": level,
                    "score_column": column,
                    "total": len(rows),
                    "small": small,
                    "groups": listed,
                    "file": MANIFEST_FILE,
                },
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_scaffolds(handle, listed, len(rows), small, target),
            meta={"handle": handle, "groups": len(listed), "small": small, "reused": False},
        )


# ---------------------------------------------------------------------------
# 匹配分子对
# ---------------------------------------------------------------------------
def _fragments(smiles: str, max_cut: int) -> list[tuple[str, str]]:
    """切一条非环单键，返回 `[(核心, 变体)]`，两边都带 `[*:1]` 连接点。

    **不用 `rdMMPA.FragmentMol`**：它按「最多切几刀」组织结果，`maxCuts=1` 时返回的是
    `('', '左手[*:1].右手[*:1]')`——核心是空的，两半都塞在 chains 里，还得再猜哪半是
    哪半。这里的判据是「切一刀之后较大的那半是核心，较小的那半是变体」，与匹配分子对
    的定义一致，也不需要去解释它的返回约定。

    `max_cut` 是**变体的重原子数上限**：一个只剩 2 个原子的取代基换来换去，SAR 上说明
    不了什么，而它会让对子数量爆炸。
    """
    from rdkit import Chem

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return []
    ring_bonds: set[int] = set()
    for ring in mol.GetRingInfo().AtomRings():
        for index in range(len(ring)):
            bond = mol.GetBondBetweenAtoms(ring[index], ring[(index + 1) % len(ring)])
            if bond is not None:
                ring_bonds.add(bond.GetIdx())

    out: list[tuple[str, str]] = []
    for bond in mol.GetBonds():
        if bond.GetIdx() in ring_bonds or bond.GetIsAromatic():
            continue
        try:
            cut = Chem.FragmentOnBonds(
                mol, [bond.GetIdx()], addDummies=True, dummyLabels=[(1, 1)]
            )
            pieces = Chem.GetMolFrags(cut, asMols=True, sanitizeFrags=True)
        except Exception:
            continue
        if len(pieces) != 2:
            continue
        pieces = sorted(pieces, key=lambda item: item.GetNumHeavyAtoms())
        variant, core = pieces
        if variant.GetNumHeavyAtoms() > max_cut:
            continue
        out.append((Chem.MolToSmiles(core), Chem.MolToSmiles(variant)))
    return out


def _render_mmp(handle: str, pairs: Sequence[dict[str, Any]], scanned: int,
                target: Path, column: str) -> str:
    lines = [f"匹配分子对完成，登记为 {handle}。"]
    if not pairs:
        lines.append(
            f"{scanned} 个分子里没有找到「共用同一个骨架、只差一个基团」的对子。"
            "要么库太小，要么分子之间差异太大——这时该先做骨架分组看整体。"
        )
        lines.append(f"产物目录：{target}")
        return "\n".join(lines)
    lines.append(f"{scanned} 个分子里找到 {len(pairs)} 对（分数列 {column}）。")
    for pair in pairs[:10]:
        sign = "+" if pair["delta"] > 0 else ""
        lines.append(
            f"  {pair['better']['id']} 优于 {pair['worse']['id']}  {sign}{pair['delta']:.2f}"
        )
        lines.append(f"    共同骨架：{pair['core']}")
        lines.append(f"    变体：{pair['better']['chain']} ↔ {pair['worse']['chain']}")
    if len(pairs) > 10:
        lines.append(f"  ……另有 {len(pairs) - 10} 对未显示（top 可调大）")
    lines.append(f"产物目录：{target}")
    lines.append("用途：同一骨架下只改一个基团，活性差多少——这是最硬的构效证据。")
    return "\n".join(lines)


class MatchedPairsTool(Tool):
    """找「只差一个基团、活性差多少」的分子对。"""

    spec = ToolSpec(
        name="matched_pairs",
        description=(
            "找**匹配分子对**（MMP）：共用同一个骨架、只在**一侧**切掉一个基团而不同的"
            "分子对，并给出活性差。" + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "分数方向按来源自动取（dock 越低越好、相似度越高越好），所以「谁更优」不用"
            "模型判断。只做**单侧**切分（一次切一刀）：双侧切出来的对子证据力弱得多，"
            "而且数量会爆炸。\n"
            "最小差值 `min_delta` 过滤掉噪声级的差别。没有对子时返回空并说明，"
            "不硬凑——这通常意味着库太小或分子之间差异太大。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "带分数的候选表 handle"},
                "max_cut": {
                    "type": "integer",
                    "description": "单侧最多切几个键，默认 10（切得越多越慢）",
                },
                "min_delta": {"type": "number", "description": "分数变化下限，默认 0.5"},
                "top": {"type": "integer", "description": f"最多列几对，默认 50，上限 {MAX_TOP}"},
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    )
    primary_argument = "source"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：matched_pairs 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("source") or "").strip()
        if not raw:
            return ToolResult(output="缺少参数 source。先 list_workspace。", failed=True)
        try:
            entry = ledger.get(raw)
            expect_handle(entry, ArtifactHandle, "source")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        max_cut = arguments.get("max_cut", 10)
        top = arguments.get("top", 50)
        min_delta = arguments.get("min_delta", 0.5)
        for name, value in (("max_cut", max_cut), ("top", top)):
            if isinstance(value, bool) or not isinstance(value, int):
                return ToolResult(output=f"参数 {name} 应当是整数", failed=True)
        if isinstance(min_delta, bool) or not isinstance(min_delta, (int, float)):
            return ToolResult(output="参数 min_delta 应当是数", failed=True)
        if not 1 <= max_cut <= 30 or not 1 <= top <= MAX_TOP or min_delta < 0:
            return ToolResult(
                output=f"max_cut 1–30、top 1–{MAX_TOP}、min_delta ≥ 0（现在是 "
                f"{max_cut}/{top}/{min_delta:g}）",
                failed=True,
            )

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            rows, column, higher = _candidate_rows(entry)
        except Exception as error:
            return ToolResult(
                output=f"{entry.handle} 不是候选表（读不到分数与结构）：{error}", failed=True
            )
        rows = [row for row in rows[:MAX_MOLECULES] if row["smiles"]]
        if len(rows) < 2:
            return ToolResult(output=f"{entry.handle} 里不足两个带结构的分子。", failed=True)

        # core → [(row, chain)]
        by_core: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for row in rows:
            for core, chain in _fragments(row["smiles"], max_cut):
                by_core.setdefault(core, []).append((row, chain))

        seen: set[tuple[str, str, str]] = set()
        pairs: list[dict[str, Any]] = []
        for core, members in by_core.items():
            if len({row["id"] for row, _chain in members}) < 2:
                continue
            for index in range(len(members)):
                for other in range(index + 1, len(members)):
                    left, left_chain = members[index]
                    right, right_chain = members[other]
                    if left["id"] == right["id"] or left_chain == right_chain:
                        continue
                    key = tuple(sorted([left["id"], right["id"]])) + (core,)
                    if key in seen:
                        continue
                    seen.add(key)
                    # 定向：delta 恒为正，表示 better 比 worse 好多少。
                    # 分数**越高越好**时左边减右边；**越低越好**时反过来——
                    # 两种写反的后果一样：把差的那个说成更优，结论整个倒过来。
                    if higher:
                        delta = float(left["score"]) - float(right["score"])
                    else:
                        delta = float(right["score"]) - float(left["score"])
                    if abs(delta) < min_delta:
                        continue
                    better, worse = (left, right) if delta > 0 else (right, left)
                    better_chain = left_chain if delta > 0 else right_chain
                    worse_chain = right_chain if delta > 0 else left_chain
                    pairs.append(
                        {
                            "core": core,
                            "delta": round(abs(delta), 3),
                            "better": {"id": better["id"], "chain": better_chain,
                                       "score": better["score"]},
                            "worse": {"id": worse["id"], "chain": worse_chain,
                                      "score": worse["score"]},
                        }
                    )
        pairs.sort(key=lambda item: (-item["delta"], item["better"]["id"]))
        pairs = pairs[:top]

        params = {
            "source": str(entry.handle),
            "max_cut": max_cut,
            "min_delta": float(min_delta),
            "top": top,
            "score_column": column,
        }
        stem = str(arguments.get("name") or "mmp")
        try:
            target, reused = products.content_dir(
                sar_dir() / "mmp", stem, params, (MANIFEST_FILE, PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="artifact", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("kind") or "") == "mmp":
                    cached = list((candidate.meta or {}).get("pairs") or [])
                    return ToolResult(
                        output=_render_mmp(str(candidate.handle), cached, len(rows), target, column),
                        meta={"handle": str(candidate.handle), "pairs": len(cached),
                              "reused": True},
                    )

        payload = {
            "kind": "mmp",
            "source": str(entry.handle),
            "score_column": column,
            "higher_is_better": higher,
            "scanned": len(rows),
            "pairs": pairs,
        }
        try:
            products.write_json_atomic(target / MANIFEST_FILE, payload)
            products.write_json_atomic(target / PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写产物失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="artifact", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("kind") or "") == "mmp":
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "artifact",
                path=target,
                summary=f"匹配分子对 · {len(pairs)} 对 · 来自 {entry.handle}",
                fingerprint=fingerprint,
                parents=[entry.handle],
                meta={
                    "kind": "mmp",
                    "stage": "mmp",
                    "tool": "matched_pairs",
                    "score_column": column,
                    "higher_is_better": higher,
                    "scanned": len(rows),
                    "pairs": pairs,
                    "file": MANIFEST_FILE,
                },
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_mmp(handle, pairs, len(rows), target, column),
            meta={"handle": handle, "pairs": len(pairs), "reused": False},
        )


# ---------------------------------------------------------------------------
# 类似物枚举
# ---------------------------------------------------------------------------
def _join(core_smiles: str, chain_smiles: str) -> str | None:
    """把带 `[*:1]` 的两段接起来：删掉两个虚原子、在它们的邻原子之间成键。

    不用 `ReplaceSubstructs`：它要求替换片段本身能当子结构查询匹配上，而 R 基常常只有
    两三个原子，稍微换个写法就匹配不到。直接改分子图反而稳，错了也能一眼看出是哪一步。
    """
    from rdkit import Chem

    core = Chem.MolFromSmiles(core_smiles)
    chain = Chem.MolFromSmiles(chain_smiles)
    if core is None or chain is None:
        return None
    combined = Chem.RWMol(Chem.CombineMols(core, chain))
    dummies = [
        atom.GetIdx() for atom in combined.GetAtoms()
        if atom.GetAtomicNum() == 0
    ]
    if len(dummies) != 2:
        return None
    left, right = dummies
    left_neighbors = [atom.GetIdx() for atom in combined.GetAtomWithIdx(left).GetNeighbors()]
    right_neighbors = [atom.GetIdx() for atom in combined.GetAtomWithIdx(right).GetNeighbors()]
    if len(left_neighbors) != 1 or len(right_neighbors) != 1:
        return None
    try:
        combined.AddBond(left_neighbors[0], right_neighbors[0], Chem.BondType.SINGLE)
    except Exception:
        return None
    for index in sorted(dummies, reverse=True):
        combined.RemoveAtom(index)
    mol = combined.GetMol()
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        return None
    return Chem.MolToSmiles(mol)


def _render_analogs(handle: str, count: int, attempted: int, skipped: int,
                    target: Path) -> str:
    lines = [f"类似物枚举完成，登记为 {handle}。"]
    lines.append(f"尝试组合 {attempted} 个，生成 {count} 个合法且不重复的分子"
                 + (f"（跳过 {skipped} 个：与组内原有分子相同、化合价不合理或重复）"
                    if skipped else "") + "。")
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：prepare_ligands(ligands={handle}, confirm=true) 再精选。")
    return "\n".join(lines)


class EnumerateAnalogsTool(Tool):
    """把骨架与替换基团重新组合，生出一批没测过的分子。"""

    spec = ToolSpec(
        name="enumerate_analogs",
        description=(
            "把**骨架**与**替换基团**重新组合，生出一批候选分子（登记成配体集，"
            "可直接进 prepare_ligands）。" + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "骨架与基团都来自 `scaffold_analysis` 的同一个分组：组里的分子在同一个核心上"
            "换着不同的基团，把它们交叉组合就是「这个骨架还能怎么改」。\n"
            "生成出来的分子**没算过活性**，只是结构上成立：化合价不合理或重复的会被跳过"
            "并计数，性质窗口（与 curate_ligands 同名同义）可以顺手过滤。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "scaffold": {
                    "type": "string",
                    "description": "scaffold_analysis 的产物 handle",
                },
                "group": {
                    "type": "integer",
                    "description": "用第几组（从 1 开始，对应 scaffold_analysis 返回的序号）",
                },
                "max": {
                    "type": "integer",
                    "description": f"最多生成多少，默认 200，上限 {MAX_ANALOGS}",
                },
                "max_mw": {"type": "number", "description": "分子量上限"},
                "max_logp": {"type": "number", "description": "logP 上限"},
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["scaffold", "group"],
            "additionalProperties": False,
        },
    )
    primary_argument = "scaffold"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：enumerate_analogs 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("scaffold") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 scaffold。先跑 scaffold_analysis。", failed=True
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, ArtifactHandle, "scaffold")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        if str((entry.meta or {}).get("kind") or "") != "scaffolds":
            return ToolResult(
                output=f"{entry.handle} 不是骨架分组的产物（kind={(entry.meta or {}).get('kind')}）。",
                failed=True,
            )

        group = arguments.get("group")
        if isinstance(group, bool) or not isinstance(group, int):
            return ToolResult(output="参数 group 应当是整数（从 1 开始）", failed=True)
        groups = list((entry.meta or {}).get("groups") or [])
        if not 1 <= group <= len(groups):
            return ToolResult(
                output=f"group 要在 1–{len(groups)} 之间（现在是 {group}）", failed=True
            )
        chosen = groups[group - 1]

        limit = arguments.get("max", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ANALOGS:
            return ToolResult(
                output=f"参数 max 要在 1 到 {MAX_ANALOGS} 之间（现在是 {limit}）", failed=True
            )
        max_mw = arguments.get("max_mw")
        max_logp = arguments.get("max_logp")
        for name, value in (("max_mw", max_mw), ("max_logp", max_logp)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                return ToolResult(output=f"参数 {name} 应当是数", failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        from rdkit import Chem, rdBase
        from rdkit.Chem import Descriptors

        core_smiles = str(chosen.get("scaffold") or "")
        if not core_smiles:
            return ToolResult(
                output="这一组的骨架是 acyclic（无环），没有可枚举的核心。换一组。",
                failed=True,
            )
        # 用组内分子的片段重建「核心 + 连接点」与替换基团：骨架本身没有连接点，
        # 直接从原始分子上切出来更可靠。
        cores: list[str] = []
        chains: list[str] = []
        members = set(chosen.get("members") or [])
        source_rows, _column, _higher = _rows_of(ledger.get(str((entry.meta or {}).get("source") or "")))
        with rdBase.BlockLogs():
            for row in source_rows:
                if row["id"] not in members:
                    continue
                for core, chain in _fragments(row["smiles"], 10):
                    if not core or not chain:
                        continue
                    cores.append(core)
                    chains.append(chain)
        cores = sorted(set(cores))
        chains = sorted(set(chains))
        if not cores or not chains:
            return ToolResult(
                output="这一组的分子切不出「核心 + 基团」，换一组或降低 max_cut 的限制。",
                failed=True,
            )

        # 组内原有的分子不算「新类似物」：把它们原样吐回来只会挤占名额。
        from rdkit import Chem as _Chem

        seen: set[str] = set()
        for row in source_rows:
            if row["id"] in members and row["smiles"]:
                molecule = _Chem.MolFromSmiles(row["smiles"])
                if molecule is not None:
                    seen.add(_Chem.MolToSmiles(molecule))
        generated: list[dict[str, Any]] = []
        attempted = 0
        skipped = 0
        with rdBase.BlockLogs():
            for core in cores:
                for chain in chains:
                    if len(generated) >= limit:
                        break
                    attempted += 1
                    smiles = _join(core, chain)
                    if not smiles:
                        skipped += 1
                        continue
                    if smiles in seen:
                        skipped += 1
                        continue
                    mol = Chem.MolFromSmiles(smiles)
                    if mol is None:
                        skipped += 1
                        continue
                    if max_mw is not None and Descriptors.MolWt(mol) > float(max_mw):
                        skipped += 1
                        continue
                    if max_logp is not None and Descriptors.MolLogP(mol) > float(max_logp):
                        skipped += 1
                        continue
                    seen.add(smiles)
                    generated.append(
                        {
                            "id": f"analog-{len(generated) + 1:04d}",
                            "name": f"analog-{len(generated) + 1:04d}",
                            "smiles": smiles,
                            "input_smiles": smiles,
                            "heavy_atoms": mol.GetNumHeavyAtoms(),
                            "source_index": len(generated) + 1,
                        }
                    )
                if len(generated) >= limit:
                    break

        if not generated:
            return ToolResult(
                output=f"一个都没生成（试了 {attempted} 个组合，跳过 {skipped} 个）。"
                "这一组的核心与基团可能互相接不上，换一组试试。",
                failed=True,
            )

        params = {
            "scaffold": str(entry.handle),
            "group": group,
            "max": limit,
            "max_mw": max_mw,
            "max_logp": max_logp,
        }
        stem = str(arguments.get("name") or f"analogs-g{group}")
        try:
            target, reused = products.content_dir(
                ligand.ligands_dir() / "analogs", stem, params,
                (ligand.MANIFEST_FILE, ligand.CSV_FILE, PARAMS_FILE),
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        handle = ""
        if reused:
            existing = ledger.find(
                kind="ligandset", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("tool") or "") == "enumerate_analogs":
                    handle = str(candidate.handle)
                    break

        if not handle:
            records = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "smiles": row["smiles"],
                    "input_smiles": row["input_smiles"],
                    "heavy_atoms": row["heavy_atoms"],
                    "source_index": row["source_index"],
                    "pchembl_value": "",
                }
                for row in generated
            ]
            buffer = io.StringIO()
            writer = csv.DictWriter(
                buffer, fieldnames=list(ligand.CSV_COLUMNS), lineterminator="\n",
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(records)
            manifest = {
                "kind": "ligandset",
                "stage": "raw",
                "tool": "enumerate_analogs",
                "source": {"handle": str(entry.handle), "group": group,
                           "members": sorted(members)},
                "params": params,
                "counts": {"generated": len(generated), "attempted": attempted,
                           "skipped": skipped},
            }
            try:
                products.write_text_atomic(target / ligand.CSV_FILE, buffer.getvalue())
                products.write_json_atomic(target / ligand.MANIFEST_FILE, manifest)
                products.write_json_atomic(target / PARAMS_FILE, params)
            except OSError as error:
                return ToolResult(output=f"写产物失败：{error}", failed=True)
            registered = ledger.add(
                "ligandset",
                path=target,
                summary=f"枚举类似物 · 第 {group} 组 · {len(generated)} 个",
                fingerprint=fingerprint_path(target),
                parents=[entry.handle],
                meta={
                    "kind": "ligandset",
                    "stage": "raw",
                    "tool": "enumerate_analogs",
                    "source": {"handle": str(entry.handle), "group": group},
                    "counts": manifest["counts"],
                    "file": ligand.CSV_FILE,
                    "manifest": ligand.MANIFEST_FILE,
                },
                link_current_receptor=False,
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_analogs(handle, len(generated), attempted, skipped, target),
            meta={"handle": handle, "count": len(generated), "skipped": skipped,
                  "reused": bool(handle) and reused},
        )


# ---------------------------------------------------------------------------
# 实验回填
# ---------------------------------------------------------------------------
_VALUE = re.compile(r"^\s*(>=|<=|>|<|=)?\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*$")


def parse_value(raw: str) -> tuple[float, str] | None:
    """`>10000` / `<1` / `12.5` → `(数值, 限定符)`。读不出就返回 None。"""
    match = _VALUE.match(str(raw or ""))
    if match is None:
        return None
    qualifier = match.group(1) or "="
    try:
        value = float(match.group(2))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value, qualifier


ASSAY_COLUMNS: tuple[str, ...] = (
    "molecule", "value", "qualifier", "assay", "campaign", "recorded_at",
)


class RecordAssayTool(Tool):
    """把用户测回来的实验数据记进运行目录。"""

    spec = ToolSpec(
        name="record_assay",
        description=(
            "把用户测回来的**实验数据**记进 `runs/<campaign>/assay.csv`。"
            "这是全库**唯一**收文件路径的领域动作之一——实验数据来自用户，模型编不出来源。\n"
            "CSV 至少要有分子标识与数值两列（列名认 molecule / id / compound 与 "
            "value / ic50 / activity）；数值支持限定符 `>10000`、`<1`。"
            "同一个 (分子, 测定) 重复出现时**保留最新的那条**，旧值进 "
            "`assay-history.csv`，不丢。\n"
            "写盘是原子的：中途失败不会留下半截表。别的 campaign 的文件一个字节都不动。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "实验数据 CSV 的路径"},
                "campaign": {"type": "string", "description": "项目名，落 runs/<campaign>/"},
                "assay": {"type": "string", "description": "测定类型，如 IC50"},
            },
            "required": ["file", "campaign", "assay"],
            "additionalProperties": False,
        },
    )
    primary_argument = "campaign"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：record_assay 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw_path = str(arguments.get("file") or "").strip()
        campaign = str(arguments.get("campaign") or "").strip()
        assay = str(arguments.get("assay") or "").strip()
        if not raw_path or not campaign or not assay:
            return ToolResult(
                output="缺少参数：file（CSV 路径）、campaign（项目名）、assay（测定类型）都要给。",
                failed=True,
            )
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", campaign):
            return ToolResult(
                output=f"campaign 只能是字母数字与 . _ -（现在是 {campaign!r}）：它会当目录名用。",
                failed=True,
            )
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return ToolResult(output=f"找不到这个文件，或它不是普通文件：{path}", failed=True)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as error:
            return ToolResult(output=f"读 {path} 失败（{type(error).__name__}：{error}）",
                              failed=True)

        first_line = text.splitlines()[0] if text.splitlines() else ""
        delimiter = "\t" if "\t" in first_line and "," not in first_line else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        fieldnames = [name for name in (reader.fieldnames or []) if name]
        lookup = {name.strip().lower(): name for name in fieldnames}
        molecule_key = next(
            (lookup[name] for name in ("molecule", "id", "compound", "molecule_id")
             if name in lookup),
            None,
        )
        value_key = next(
            (lookup[name] for name in ("value", "ic50", "activity", "measurement")
             if name in lookup),
            None,
        )
        if molecule_key is None or value_key is None:
            return ToolResult(
                output=(
                    f"CSV 里需要「分子标识」与「数值」两列，实际表头：{', '.join(fieldnames) or '（空）'}。"
                    "分子列认 molecule / id / compound，数值列认 value / ic50 / activity。"
                ),
                failed=True,
            )

        received: dict[str, dict[str, Any]] = {}
        skipped: list[dict[str, str]] = []
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for index, row in enumerate(reader, start=2):
            molecule = str(row.get(molecule_key) or "").strip()
            parsed = parse_value(row.get(value_key) or "")
            if not molecule or parsed is None:
                skipped.append(
                    {"line": str(index), "molecule": molecule,
                     "raw": str(row.get(value_key) or "")}
                )
                continue
            value, qualifier = parsed
            received[molecule] = {
                "molecule": molecule,
                "value": value,
                "qualifier": qualifier,
                "assay": assay,
                "campaign": campaign,
                "recorded_at": stamp,
            }
        if not received:
            return ToolResult(
                output=(
                    f"{path.name} 里一条可用记录都没有（跳过 {len(skipped)} 条："
                    "分子列为空，或数值既不是数字也不带限定符）。"
                ),
                failed=True,
            )

        target = export.runs_dir() / campaign
        main_path = target / "assay.csv"
        history_path = target / "assay-history.csv"
        existing: dict[str, dict[str, Any]] = {}
        if main_path.is_file():
            try:
                with main_path.open(encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        if str(row.get("assay") or "") == assay and row.get("molecule"):
                            existing[str(row["molecule"])] = row
            except OSError as error:
                return ToolResult(output=f"读已有 assay.csv 失败：{error}", failed=True)

        superseded = [existing[key] for key in received if key in existing]
        merged = dict(existing)
        merged.update(received)

        try:
            target.mkdir(parents=True, exist_ok=True)
            if superseded:
                new_history = not history_path.is_file()
                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=list(ASSAY_COLUMNS),
                                        lineterminator="\n", extrasaction="ignore")
                if new_history:
                    writer.writeheader()
                writer.writerows(superseded)
                with history_path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(buffer.getvalue())
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(ASSAY_COLUMNS),
                                    lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(merged.values(), key=lambda row: str(row["molecule"])))
            products.write_text_atomic(main_path, buffer.getvalue())
        except OSError as error:
            return ToolResult(output=f"写实验数据失败（{type(error).__name__}：{error}）",
                              failed=True)

        fingerprint = fingerprint_path(main_path)
        handle = ""
        existing_entries = ledger.find(
            kind="artifact", fingerprint=fingerprint, include_archived=True
        )
        for candidate in existing_entries:
            if str((candidate.meta or {}).get("kind") or "") == "assay":
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "artifact",
                path=main_path,
                summary=f"实验数据 · {campaign} · {len(merged)} 条（{assay}）",
                fingerprint=fingerprint,
                meta={
                    "kind": "assay",
                    "stage": "assay",
                    "tool": "record_assay",
                    "campaign": campaign,
                    "assay": assay,
                    "file": "assay.csv",
                    "counts": {
                        "received": len(received),
                        "merged": len(merged),
                        "superseded": len(superseded),
                        "skipped": len(skipped),
                    },
                },
            )
            handle = str(registered.handle)

        lines = [
            f"实验数据已记录，登记为 {handle}。",
            f"本次收到 {len(received)} 条，表里现在共 {len(merged)} 条（{assay}）。",
        ]
        if superseded:
            lines.append(f"{len(superseded)} 条旧值被覆盖，原文进了 assay-history.csv。")
        if skipped:
            first = skipped[0]
            lines.append(
                f"跳过 {len(skipped)} 条（第一条：第 {first['line']} 行 "
                f"molecule={first['molecule'] or '（空）'} value={first['raw']!r}）"
            )
        lines.append(f"文件：{main_path}")
        lines.append("下一步：把这些活性当参考集（build_reference_set 的输入），或做匹配分子对。")
        return ToolResult(
            output="\n".join(lines),
            meta={
                "handle": handle,
                "received": len(received),
                "merged": len(merged),
                "skipped": len(skipped),
                "superseded": len(superseded),
            },
        )


def domain_tools() -> list[Tool]:
    return [ScaffoldAnalysisTool(), MatchedPairsTool(), EnumerateAnalogsTool(),
            RecordAssayTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    main()
