"""VCrossFormer 亲和力打分：读已准备的受体与配体，跑 models/DTA/virtual_screen.py，登记成 artifact。

这个模块是领域工具里**第二个调用外部进程**的（第一个是 docking 的 Vina），规矩照抄：

- 用 `create_subprocess_exec`，参数数组化，绝不拼 shell；
- 超时/取消都杀整棵进程树；
- 单条失败不中断整批，原因逐条写进 manifest 并回灌。

与 dock 的三处关键差异，改之前先看清楚：

1. **方向相反**：dock 是 kcal/mol，越低越好；这里是 pK，**越高越好**。跨工具比较没有意义。
2. **外部进程是一个 Python 解释器**，不是单个二进制。torch/dgl 只存在于 DTA 自己的
   环境里，主仓一行都不 import——否则应用起不来，exe 也会被 PyInstaller 拖进 2GB 依赖。
3. **口袋要现截**：模型训练用的是「配体 10 Å 内的残基」，不是整个受体。受体产物里的
   `box.txt` 给中心，`models/DTA/utils/pocket.py` 按半径截出残基再建图。
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    ArtifactHandle,
    LigandSetHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_parts,
    fingerprint_path,
)
from ..tools import (
    Tool,
    ToolError,
    ToolResult,
    ToolSpec,
    decode_output,
    release_pipes,
    terminate_tree,
)
from . import products
from .docking import read_box

PYTHON_ENV = "VSA_DTA_PYTHON"
ROOT_ENV = "VSA_DTA_ROOT"

INSTALL_HINT = (
    "亲和力打分要用 models/DTA 里的 VCrossFormer，它有自己的 Python 3.10 环境"
    "（torch + dgl，主程序不带这些）。装法见 models/DTA/README.md：\n"
    "  uv venv --python 3.10 models/DTA/.venv\n"
    "  uv pip install --python models/DTA/.venv/bin/python torch==2.2.1+cpu "
    "--index-url https://download.pytorch.org/whl/cpu\n"
    "  uv pip install --python models/DTA/.venv/bin/python torchdata==0.7.1\n"
    "  uv pip install --python models/DTA/.venv/bin/python dgl==2.1.0\n"
    "  uv pip install --python models/DTA/.venv/bin/python -r models/DTA/requirements.txt\n"
    "torch 必须是 2.2.x：dgl 2.1.0 的 graphbolt 库按 torch 次版本号命名，"
    "装 2.3 会报 libgraphbolt_pytorch_2.3.0.so 找不到。\n"
    f"装好后默认就用仓根的 models/DTA 与它里面的 .venv；放在别处时用 {PYTHON_ENV} "
    f"指向解释器、{ROOT_ENV} 指向 DTA 目录。"
)

AFF_UNITS = ("Kd", "Ki", "IC50")
DEFAULT_AFF_UNIT = "Ki"

DEFAULT_RADIUS = 10.0
MIN_RADIUS = 3.0
MAX_RADIUS = 20.0

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

DEFAULT_TIMEOUT = 900.0
MIN_TIMEOUT = 60.0
MAX_TIMEOUT = 7200.0

MANIFEST_FILE = "manifest.json"
PARAMS_FILE = products.PARAMS_FILE
SCORES_FILE = "scores.csv"
TASKS_FILE = "tasks.json"
LOG_DIR = "logs"
RUN_LOG = "run.log"

MAX_RESULTS_INLINE = 10
MAX_FAILURES_INLINE = 5
LOG_EXCERPT_LINES = 20


# ---------------------------------------------------------------------------
# 外部环境与路径
# ---------------------------------------------------------------------------
def default_dta_root() -> Path:
    """仓根的 `models/DTA`。

    **模型代码不在包内**：`check_chemistry_gate` 用 `rglob("*.py")` 扫整个
    `opencode_tui/`，DTA 有 45 处顶层的 torch / rdkit 导入，放进包里那条断言
    当场就红；PyInstaller 还会顺着 collect 把 54MB 数据拖进 exe。放 `models/`
    是为了既不进包、又不散落在仓库外面——整个目录被 `.gitignore` 挡住。
    """
    return Path(__file__).resolve().parents[4] / "models" / "DTA"


def dta_root() -> Path:
    """DTA 子项目的根。默认是仓根的 `models/DTA`，可用 `VSA_DTA_ROOT` 覆盖。"""
    raw = os.environ.get(ROOT_ENV, "").strip()
    root = Path(raw).expanduser() if raw else default_dta_root()
    if not (root / "virtual_screen.py").is_file():
        raise ToolError(f"找不到 DTA/virtual_screen.py：{root}。{INSTALL_HINT}")
    return root


def dta_python() -> str:
    """跑模型用的解释器。显式设了环境变量却不可用就直接失败，不静默回退。

    先找 DTA 目录**里面**的 `.venv`（自包含，搬目录只动一处），再找它旁边
    旧布局的 `.venv-dta`——两种都认，免得已经装好的人因为换了布局重装。
    """
    raw = os.environ.get(PYTHON_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ToolError(f"{PYTHON_ENV} 指向的文件不存在：{path}。{INSTALL_HINT}")
        return str(path)

    root = dta_root()
    for base, relative in (
        (root, ".venv/bin/python"),
        (root, ".venv/Scripts/python.exe"),
        (root.parent, ".venv-dta/bin/python"),
        (root.parent, ".venv-dta/Scripts/python.exe"),
    ):
        candidate = base / relative
        if candidate.is_file():
            return str(candidate)
    raise ToolError(
        f"没有找到 DTA 的解释器（试过 {root}/.venv 与 {root.parent}/.venv-dta）。"
        f"{INSTALL_HINT}")


def scorings_dir() -> Path:
    """打分产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / "affinity"


# ---------------------------------------------------------------------------
# 跑一次批量打分
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BatchRun:
    """一次子进程的结果。与 `docking.VinaRun` 同构。"""

    argv: tuple[str, ...]
    returncode: int
    output: str
    timed_out: bool
    log_path: Path


async def run_batch(argv: Sequence[str], *, cwd: Path, timeout: float,
                    log_path: Path) -> BatchRun:
    """跑一次 virtual_screen.py。超时/取消都终止整棵进程树。"""
    extra: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd),
        **extra,
    )
    collect = asyncio.ensure_future(process.communicate())
    timed_out = False
    try:
        raw, _ = await asyncio.wait_for(asyncio.shield(collect), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        terminate_tree(process)
        raw, _ = await collect
    except asyncio.CancelledError:
        terminate_tree(process)
        await collect
        raise
    finally:
        release_pipes(process)

    output = decode_output(raw or b"")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        products.write_text_atomic(log_path, _truncate_log(output))
    except OSError:
        # 日志写不下不该把已经跑完的整批丢掉；manifest 里还有计数与失败摘要。
        pass
    code = process.returncode if process.returncode is not None else -1
    return BatchRun(argv=tuple(argv), returncode=code, output=output,
                    timed_out=timed_out, log_path=log_path)


def _truncate_log(text: str) -> str:
    """日志只留尾部：模型加载的进度条会把前面冲掉。"""
    lines = text.splitlines()
    if len(lines) <= 400:
        return text
    return "\n".join(["…（日志前部已截断）", *lines[-400:]]) + "\n"


def _log_excerpt(text: str, limit: int = LOG_EXCERPT_LINES) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScoreParams:
    """一次打分请求的参数。"""

    aff_unit: str = DEFAULT_AFF_UNIT
    pocket_radius: float = DEFAULT_RADIUS
    limit: int = DEFAULT_LIMIT
    timeout: float = DEFAULT_TIMEOUT
    device: str = "auto"

    def as_dict(self) -> dict[str, Any]:
        return {
            "aff_unit": self.aff_unit,
            "pocket_radius": self.pocket_radius,
            "limit": self.limit,
            "timeout": self.timeout,
            "device": self.device,
        }


def _number_argument(arguments: dict[str, Any], name: str, default: float,
                     minimum: float, maximum: float) -> float:
    raw = arguments.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolError(f"参数 {name} 应当是数字，收到 {raw!r}")
    value = float(raw)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ToolError(f"参数 {name} 要在 {minimum:g} 到 {maximum:g} 之间（现在是 {raw}）")
    return value


def _int_argument(arguments: dict[str, Any], name: str, default: int,
                  minimum: int, maximum: int) -> int:
    raw = arguments.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolError(f"参数 {name} 应当是整数，收到 {raw!r}")
    if not minimum <= raw <= maximum:
        raise ToolError(f"参数 {name} 要在 {minimum} 到 {maximum} 之间（现在是 {raw}）")
    return raw


def _params(arguments: dict[str, Any]) -> ScoreParams:
    unit = str(arguments.get("aff_unit") or DEFAULT_AFF_UNIT).strip()
    if unit not in AFF_UNITS:
        raise ToolError(f"aff_unit 只能是 {'、'.join(AFF_UNITS)}，收到 {unit!r}")
    device = str(arguments.get("device") or "auto").strip().lower()
    if device not in ("auto", "cpu", "cuda"):
        raise ToolError(f"device 只能是 auto / cpu / cuda，收到 {device!r}")
    return ScoreParams(
        aff_unit=unit,
        pocket_radius=_number_argument(arguments, "pocket_radius", DEFAULT_RADIUS,
                                       MIN_RADIUS, MAX_RADIUS),
        limit=_int_argument(arguments, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        timeout=_number_argument(arguments, "timeout", DEFAULT_TIMEOUT,
                                 MIN_TIMEOUT, MAX_TIMEOUT),
        device=device,
    )


# ---------------------------------------------------------------------------
# 输入：受体与配体
# ---------------------------------------------------------------------------
def _receptor_inputs(entry: Any) -> tuple[Path, tuple[float, float, float]]:
    """从 prepared 受体条目里取 (receptor.pdb, 口袋中心)。"""
    meta = dict(entry.meta or {})
    if str(meta.get("stage") or "") != "prepared":
        raise ToolError(
            "受体还不处于 prepared 状态。先用 prepare_receptor 的 confirm=true 写盘，"
            "再拿它来打分。")
    directory = Path(entry.path) if entry.path is not None else None
    if directory is None or not directory.is_dir():
        raise ToolError("prepared 受体的产物目录不在了；重新跑 prepare_receptor。")

    name = str(meta.get("pdb_file") or "receptor.pdb")
    if Path(name).name != name:                       # 防目录穿越
        raise ToolError(f"受体 meta 里的 pdb_file 不是文件名：{name}")
    pdb = directory / name
    if not pdb.is_file():
        raise ToolError(f"受体的 PDB 不在：{pdb}；重新跑 prepare_receptor。")

    box = read_box(directory, meta)
    return pdb, box.center


def _ligand_records(entry: Any) -> tuple[str, list[dict[str, str]]]:
    """返回 (来源种类, [{id, path, smiles}])。

    两种来源都只取「成功」的条目，且路径必须落在产物目录内。
    """
    meta = dict(entry.meta or {})
    root = Path(entry.path) if entry.path is not None else None
    if root is None or not root.is_dir():
        raise ToolError(f"{entry.handle} 的产物目录不在了；重新跑上游工具。")

    if str(meta.get("kind") or "") == "docking":
        return "docking", _docking_records(root)

    if isinstance(entry.handle, LigandSetHandle):
        if str(meta.get("stage") or "") != "prepared":
            raise ToolError(
                "配体集还不处于 prepared 状态。先用 prepare_ligands 准备（打分要 3D 坐标，"
                "原始配体集只有 SMILES），或改成传 dock 的产物。")
        return "ligandset", _prepared_records(root)

    raise ToolError(
        f"{entry.handle} 不能用来打分：要 prepared 的配体集，或 dock 的产物。")


def _prepared_records(root: Path) -> list[dict[str, str]]:
    manifest = products.read_json(root / "manifest.json")
    records = []
    for record in manifest.get("records") or []:
        if str(record.get("status") or "") != "ok":
            continue
        relative = str(record.get("pdbqt") or "")
        source = (root / relative).resolve()
        if not relative or not source.is_relative_to(root) or not source.is_file():
            continue
        records.append({
            "id": str(record.get("id") or source.stem),
            "path": str(source),
            "smiles": str(record.get("smiles") or ""),
        })
    return records


def _docking_records(root: Path) -> list[dict[str, str]]:
    manifest = products.read_json(root / "manifest.json")
    records = []
    for result in manifest.get("results") or []:
        if str(result.get("status") or "") != "ok":
            continue
        relative = str(result.get("pose_pdbqt") or "")
        source = (root / relative).resolve()
        if not relative or not source.is_relative_to(root) or not source.is_file():
            continue
        records.append({
            "id": str(result.get("id") or source.stem),
            "path": str(source),
            "smiles": str(result.get("smiles") or ""),
        })
    return records


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
def read_scores(path: Path) -> list[dict[str, Any]]:
    """读 scores.csv；pK 不是有限数的行跳过（模型给出了 NaN 就等于没算出来）。"""
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                pk = float(row.get("predicted_pK") or "nan")
            except ValueError:
                continue
            if not math.isfinite(pk):
                continue
            rows.append({
                "id": str(row.get("ligand_name") or ""),
                "smiles": str(row.get("smiles") or ""),
                "pk": pk,
                "nM": _float_or_none(row.get("predicted_value_nM")),
                "unit": str(row.get("predicted_unit") or ""),
            })
    rows.sort(key=lambda item: item["pk"], reverse=True)
    return rows


def _float_or_none(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_summary(output: str) -> dict[str, int]:
    """从 stdout 的 `SUMMARY {...}` 行取计数；没有就返回空字典。"""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("SUMMARY "):
            continue
        try:
            payload = json.loads(stripped[len("SUMMARY "):])
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        counts = {}
        for key in ("input", "scored", "failed"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[key] = value
        return counts
    return {}


def parse_errors(output: str) -> list[dict[str, str]]:
    """`ERROR <id>: <原因>` 逐条收集，最多留 50 条。"""
    failures = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ERROR "):
            continue
        body = stripped[len("ERROR "):]
        identifier, _, detail = body.partition(":")
        failures.append({"id": identifier.strip(), "detail": detail.strip()[:300]})
        if len(failures) >= 50:
            break
    return failures


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _render_scored(*, handle: str, reused: bool, target: Path, manifest: dict[str, Any]) -> str:
    counts = manifest.get("counts") or {}
    results = manifest.get("results") or []
    failures = manifest.get("failures") or []
    pocket = manifest.get("pocket") or {}
    params = manifest.get("params") or {}

    lines = []
    if results:
        best = results[0]
        nm = best.get("nM")
        nm_text = f"，约 {nm:.1f} nM" if isinstance(nm, (int, float)) else ""
        lines.append(
            f"结论：{counts.get('scored', 0)} 个配体已打分，最佳 pK {best['pk']:.2f}"
            f"（{best['id']}{nm_text}），登记为 {handle}。")
    else:
        lines.append(f"结论：没有配体打分成功（{counts.get('input', 0)} 个全部失败），"
                     f"产物仍留在 {target}。")

    lines.append(
        f"方向：pK 越高越好（结合越强），与 dock 的 kcal/mol 相反；"
        f"只能在同一受体、同一 aff_unit（{params.get('aff_unit')}）下比较。")
    lines.append(
        f"口袋：中心 {tuple(pocket.get('center') or ())}，半径 {pocket.get('radius')} Å，"
        f"{pocket.get('residues', 0)} 个残基。")
    lines.append(
        f"计数：输入 {counts.get('input', 0)}、成功 {counts.get('scored', 0)}、"
        f"失败 {counts.get('failed', 0)}。")

    if results:
        lines.append(f"排行（前 {min(len(results), MAX_RESULTS_INLINE)} 名）：")
        for index, item in enumerate(results[:MAX_RESULTS_INLINE], start=1):
            nm = item.get("nM")
            nm_text = f"（{nm:.1f} nM）" if isinstance(nm, (int, float)) else ""
            lines.append(f"  {index}. {item['id']}  pK {item['pk']:.2f}{nm_text}")

    if failures:
        lines.append(f"失败（前 {min(len(failures), MAX_FAILURES_INLINE)} 条）：")
        for item in failures[:MAX_FAILURES_INLINE]:
            lines.append(f"  - {item.get('id')}: {item.get('detail')}")
        if len(failures) > MAX_FAILURES_INLINE:
            lines.append(f"  …（共 {len(failures)} 条，完整列表见 {target / MANIFEST_FILE}）")

    lines.append(f"产物：{target}（{'复用已有结果' if reused else '本次新算'}）")
    return "\n".join(lines)


def _summary_text(manifest: dict[str, Any], handle: str) -> str:
    counts = manifest.get("counts") or {}
    return (f"亲和力打分 {handle}：{counts.get('scored', 0)}/{counts.get('input', 0)} 成功")


def _affinity_meta(manifest: dict[str, Any]) -> dict[str, Any]:
    """artifact 的 meta 是导出层认它的唯一凭据。"""
    return {
        "kind": "affinity",
        "stage": "affinity",
        "tool": "score_affinity",
        "engine": manifest.get("engine") or {},
        "receptor": (manifest.get("receptor") or {}).get("handle") or "",
        "ligands": (manifest.get("ligands") or {}).get("handle") or "",
        "counts": manifest.get("counts") or {},
        "manifest": MANIFEST_FILE,
        "scores": SCORES_FILE,
    }


def _register_affinity(ledger, *, target, fingerprint, summary, manifest,
                       receptor, ligands):
    existing = ledger.find(kind="artifact", fingerprint=fingerprint, include_archived=True)
    for candidate in existing:
        if str(candidate.meta.get("kind") or "") == "affinity" and _entry_complete(candidate):
            return str(candidate.handle), True
    entry = ledger.add(
        "artifact",
        path=target,
        summary=summary,
        fingerprint=fingerprint,
        parents=[receptor, ligands],
        meta=_affinity_meta(manifest),
    )
    return str(entry.handle), False


def _entry_complete(entry: Any) -> bool:
    path = entry.path
    if path is None:
        return False
    return (Path(path) / MANIFEST_FILE).is_file() and (Path(path) / PARAMS_FILE).is_file()


def _meta_result(manifest, handle, fingerprint, ledger_reused) -> dict[str, Any]:
    counts = manifest.get("counts") or {}
    results = manifest.get("results") or []
    return {
        "handle": handle,
        "fingerprint": fingerprint,
        "reused": ledger_reused,
        "input": counts.get("input", 0),
        "scored": counts.get("scored", 0),
        "failed": counts.get("failed", 0),
        "best_pk": results[0]["pk"] if results else None,
        "best_id": results[0]["id"] if results else "",
        "aff_unit": (manifest.get("params") or {}).get("aff_unit"),
        "kind": "affinity",
    }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class ScoreAffinityTool(Tool):
    """用 VCrossFormer 给已准备的受体-配体对打亲和力分。"""

    spec = ToolSpec(
        name="score_affinity",
        description=(
            "用 VCrossFormer 给**已准备**的受体与配体预测结合亲和力（pK）。"
            "**分数越高越好**（pK 越大结合越强），与 dock 的 kcal/mol 方向相反；"
            "只能在同一受体、同一 aff_unit 条件下比较，不能跨靶点比较。"
            "这是模型预测值，不是实验值，也不是对接打分。"
            "需要 DTA 的独立 Python 环境（torch + dgl）；缺环境时返回安装指引，不抛异常。"
            "默认 aff_unit=Ki、pocket_radius=10、limit=50、整批超时 900 秒；"
            "参数由用户决定，不要自己改。"
            "单条失败不中断整批，失败原因逐条写进 manifest。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "receptor": {
                    "type": "string",
                    "description": "已准备的受体 handle，例如 receptor#2；不要传原始结构",
                },
                "ligands": {
                    "type": "string",
                    "description": (
                        "已准备的配体集 handle（ligandset#3），或 dock 的产物 handle"
                        "（artifact#4，此时按对接姿势打分）"
                    ),
                },
                "aff_unit": {
                    "type": "string",
                    "enum": list(AFF_UNITS),
                    "description": f"条件单位，默认 {DEFAULT_AFF_UNIT}；它会影响预测值",
                },
                "pocket_radius": {
                    "type": "number",
                    "description": (
                        f"口袋半径（埃），默认 {DEFAULT_RADIUS:g}，范围 "
                        f"{MIN_RADIUS:g}–{MAX_RADIUS:g}；以受体盒子的中心为球心截残基"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"本次最多打多少个配体，默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}",
                },
                "timeout": {
                    "type": "number",
                    "description": f"整批超时秒数，默认 {DEFAULT_TIMEOUT:g}，范围 {MIN_TIMEOUT:g}–{MAX_TIMEOUT:g}",
                },
                "device": {
                    "type": "string",
                    "enum": ["auto", "cpu", "cuda"],
                    "description": "推理设备，默认 auto（有 GPU 用 GPU，否则 CPU）",
                },
            },
            "required": ["receptor", "ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "receptor"
    # 打分是一次「受体 × 配体集」的调用：换配体集重跑要算另一条证据。
    evidence_key = ("ligands",)
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：score_affinity 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        # 缺参在这里再拦一次：`Tool.execute` 只管注入，必填校验在循环层的
        # `argument_error` 里；直接调 `execute` 的测试与脚本会绕过那一层。
        raw_receptor = str(arguments.get("receptor") or "").strip()
        raw_ligands = str(arguments.get("ligands") or "").strip()
        if not raw_receptor:
            return ToolResult(
                output="缺少参数 receptor。先 list_workspace 看清有哪份已准备的受体。",
                failed=True,
            )
        if not raw_ligands:
            return ToolResult(
                output="缺少参数 ligands。先 list_workspace 看清有哪份已准备的配体集。",
                failed=True,
            )

        try:
            params = _params(arguments)
            receptor_entry = ledger.get(raw_receptor)
            expect_handle(receptor_entry, ReceptorHandle, "receptor")
            ligand_entry = ledger.get(raw_ligands)
            if not isinstance(ligand_entry.handle, (LigandSetHandle, ArtifactHandle)):
                raise ToolError(
                    f"参数 ligands 要的是 prepared 的配体集或 dock 的产物，"
                    f"但收到 {ligand_entry.handle}（属于 {ligand_entry.handle.kind}）。")
            python = dta_python()
            root = dta_root()
            receptor_pdb, center = _receptor_inputs(receptor_entry)
            source_kind, records = _ligand_records(ligand_entry)
        except Exception as error:          # LedgerError / ToolError / OSError
            return ToolResult(output=str(error), failed=True)

        if not records:
            return ToolResult(
                output=f"{ligand_entry.handle} 里没有可打分的配体"
                       "（需要 prepared 且 status=ok 的条目）。",
                failed=True)

        requested = len(records)
        selected = records[:params.limit]

        cache_params: dict[str, Any] = {
            "receptor_fingerprint": str(receptor_entry.fingerprint or ""),
            "ligands_fingerprint": str(ligand_entry.fingerprint or ""),
            "ligands_kind": source_kind,
            "ligand_ids": [item["id"] for item in selected],
            "pocket": {"center": [round(value, 3) for value in center],
                       "radius": params.pocket_radius},
            "engine": {"name": "vcrossformer", "device": params.device},
            **params.as_dict(),
        }
        stem = products.safe_stem(
            f"{Path(receptor_entry.path).name}-{Path(ligand_entry.path).name}-affinity")
        try:
            target, content_reused = products.content_dir(
                scorings_dir(), stem, cache_params, (MANIFEST_FILE, PARAMS_FILE))
        except OSError as error:
            return ToolResult(
                output=f"准备打分产物目录失败（{type(error).__name__}：{error}）", failed=True)

        if content_reused:
            try:
                manifest = products.read_json(target / MANIFEST_FILE)
            except (OSError, json.JSONDecodeError) as error:
                return ToolResult(
                    output=f"复用已有打分产物失败（{type(error).__name__}：{error}）",
                    failed=True)
            fingerprint = fingerprint_path(target)
            handle, ledger_reused = _register_affinity(
                ledger, target=target, fingerprint=fingerprint,
                summary=_summary_text(manifest, ""), manifest=manifest,
                receptor=receptor_entry.handle, ligands=ligand_entry.handle)
            return ToolResult(
                output=_render_scored(handle=handle, reused=True, target=target,
                                      manifest=manifest),
                meta=_meta_result(manifest, handle, fingerprint, ledger_reused))

        try:
            target.mkdir(parents=True, exist_ok=True)
            tasks_path = target / TASKS_FILE
            products.write_json_atomic(tasks_path, [
                {"id": item["id"], "path": item["path"], "smiles": item["smiles"]}
                for item in selected
            ])
        except OSError as error:
            return ToolResult(
                output=f"写任务清单失败（{type(error).__name__}：{error}）", failed=True)

        argv = [
            python,
            str(root / "virtual_screen.py"),
            "--config", str(root / "config" / "configall.yaml"),
            "--receptor", str(receptor_pdb),
            "--pocket-center", ",".join(f"{value:g}" for value in center),
            "--pocket-radius", f"{params.pocket_radius:g}",
            "--tasks", str(tasks_path),
            "--output", str(target / SCORES_FILE),
            "--aff_unit", params.aff_unit,
            "--device", params.device,
        ]
        log_path = target / LOG_DIR / RUN_LOG
        try:
            run = await run_batch(argv, cwd=root, timeout=params.timeout, log_path=log_path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ToolResult(
                output=(f"起打分进程失败（{type(error).__name__}：{error}）。{INSTALL_HINT}"),
                failed=True)

        if run.timed_out:
            return ToolResult(
                output=(f"打分超时（{params.timeout:g} 秒，{len(selected)} 个配体）。"
                        f"可以调小 limit 或调大 timeout。日志：{run.log_path}\n"
                        f"{_log_excerpt(run.output)}"),
                failed=True)
        if run.returncode != 0:
            return ToolResult(
                output=(f"打分进程退出码 {run.returncode}。日志：{run.log_path}\n"
                        f"{_log_excerpt(run.output)}"),
                failed=True)

        scores_path = target / SCORES_FILE
        if not scores_path.is_file():
            return ToolResult(
                output=(f"打分进程没有产出 {SCORES_FILE}。日志：{run.log_path}\n"
                        f"{_log_excerpt(run.output)}"),
                failed=True)
        try:
            results = read_scores(scores_path)
        except OSError as error:
            return ToolResult(
                output=f"读 {SCORES_FILE} 失败（{type(error).__name__}：{error}）", failed=True)

        summary = parse_summary(run.output)
        failures = parse_errors(run.output)
        if summary.get("input") is not None and summary["input"] != len(selected):
            # 计数对不上说明脚本没按我们给的清单跑，宁可报出来。
            failures.append({
                "id": "-",
                "detail": f"脚本报的输入数 {summary['input']} 与请求的 {len(selected)} 不一致",
            })

        counts = {
            "input": len(selected),
            "scored": len(results),
            "failed": max(len(selected) - len(results), 0),
            "requested": requested,
            "remaining": max(requested - len(selected), 0),
        }
        manifest = {
            "kind": "affinity",
            "stage": "affinity",
            "tool": "score_affinity",
            "engine": {"name": "vcrossformer", "device": params.device,
                       "python": python, "root": str(root)},
            "receptor": {
                "handle": str(receptor_entry.handle),
                "fingerprint": str(receptor_entry.fingerprint or ""),
                "pdb": str(receptor_pdb),
            },
            "ligands": {
                "handle": str(ligand_entry.handle),
                "fingerprint": str(ligand_entry.fingerprint or ""),
                "source": source_kind,
                "requested": requested,
                "used": len(selected),
            },
            "pocket": {
                "center": [round(value, 3) for value in center],
                "radius": params.pocket_radius,
                "residues": _pocket_residues(run.output),
            },
            "params": params.as_dict(),
            "params_fingerprint": fingerprint_parts(cache_params, length=16),
            "counts": counts,
            "results": results,
            "failures": failures,
            "log": f"{LOG_DIR}/{RUN_LOG}",
        }
        try:
            products.write_json_atomic(target / MANIFEST_FILE, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / PARAMS_FILE, cache_params)
        except OSError as error:
            return ToolResult(
                output=f"写打分产物失败（{type(error).__name__}：{error}）", failed=True)

        fingerprint = fingerprint_path(target)
        handle, ledger_reused = _register_affinity(
            ledger, target=target, fingerprint=fingerprint,
            summary=_summary_text(manifest, ""), manifest=manifest,
            receptor=receptor_entry.handle, ligands=ligand_entry.handle)

        rendered = _render_scored(handle=handle, reused=False, target=target,
                                  manifest=manifest)
        if not results:
            return ToolResult(
                output=rendered + f"\n日志：{run.log_path}", failed=True,
                meta=_meta_result(manifest, handle, fingerprint, ledger_reused))
        return ToolResult(
            output=rendered,
            meta=_meta_result(manifest, handle, fingerprint, ledger_reused))


def _pocket_residues(output: str) -> int:
    """从脚本的 `口袋：… 截出 N 个残基` 行取残基数；取不到就是 0。"""
    for line in output.splitlines():
        if "截出" not in line or "残基" not in line:
            continue
        parts = line.replace("（", " ").replace("）", " ").split()
        for index, part in enumerate(parts):
            if part == "截出" and index + 1 < len(parts):
                try:
                    return int(parts[index + 1])
                except ValueError:
                    return 0
    return 0


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [ScoreAffinityTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
