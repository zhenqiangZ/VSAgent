"""AutoDock Vina 对接：读已准备的受体与配体，逐条跑引擎，登记成 artifact。

这个模块是领域工具里**第一个调用外部可执行文件**的。规矩只有三条：

- 用 `create_subprocess_exec`，参数数组化，绝不拼 shell；
- 超时/取消都杀整棵进程树（复用内核 `tools.py` 的 `terminate_tree`）；
- 单条失败不中断整批，失败原因逐条写进 manifest 并回灌。

分数只从 Vina 输出里读，**不接受模型传进来的数**；排序、配体效率都由代码算。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
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
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem

MANIFEST_FILE = "manifest.json"
PARAMS_FILE = products.PARAMS_FILE
POSE_DIR = "poses"
LOG_DIR = "logs"
VINA_ENV = "VSA_VINA_BIN"

VINA_INSTALL_HINT = (
    "本机没有找到 AutoDock Vina。装一个 1.2.x 的 vina 可执行文件并放进 PATH，"
    f"或用 {VINA_ENV} 指向它的绝对路径。对接引擎不随本应用打包。"
)

DEFAULT_EXHAUSTIVENESS = 8
MAX_EXHAUSTIVENESS = 64
DEFAULT_NUM_MODES = 9
MAX_NUM_MODES = 20
DEFAULT_SEED = 42
DEFAULT_CPU = 0
MAX_CPU = 256
DEFAULT_ENERGY_RANGE = 3.0
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_TIMEOUT = 300.0
MIN_TIMEOUT = 10.0
MAX_TIMEOUT = 3600.0

MAX_BOX_EDGE = 126.0
MAX_LOG_CHARS = 512 * 1024
LOG_KEEP_CHARS = 64 * 1024
MAX_EXCERPT_CHARS = 2000
MAX_FAILURES_INLINE = 5

_VINA_RESULT = re.compile(
    r"^REMARK VINA RESULT:\s+"
    r"(-?\d+(?:\.\d+)?)\s+"
    r"(-?\d+(?:\.\d+)?)\s+"
    r"(-?\d+(?:\.\d+)?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class VinaBinary:
    path: str
    version: str = ""


@dataclass(frozen=True, slots=True)
class Box:
    center: tuple[float, float, float]
    size: tuple[float, float, float]
    source: str


@dataclass(frozen=True, slots=True)
class DockParams:
    exhaustiveness: int = DEFAULT_EXHAUSTIVENESS
    num_modes: int = DEFAULT_NUM_MODES
    seed: int = DEFAULT_SEED
    cpu: int = DEFAULT_CPU
    energy_range: float = DEFAULT_ENERGY_RANGE
    limit: int = DEFAULT_LIMIT
    timeout: float = DEFAULT_TIMEOUT

    def as_dict(self) -> dict[str, Any]:
        return {
            "exhaustiveness": self.exhaustiveness,
            "num_modes": self.num_modes,
            "seed": self.seed,
            "cpu": self.cpu,
            "energy_range": self.energy_range,
            "limit": self.limit,
            "timeout": self.timeout,
        }


@dataclass(frozen=True, slots=True)
class VinaMode:
    score: float
    rmsd_lb: float
    rmsd_ub: float


@dataclass(frozen=True, slots=True)
class VinaRun:
    argv: tuple[str, ...]
    returncode: int
    output: str
    timed_out: bool
    log_path: Path


def dockings_dir() -> Path:
    """对接产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / "docking"


# ---------------------------------------------------------------------------
# 引擎与盒子
# ---------------------------------------------------------------------------
def resolve_vina() -> VinaBinary:
    """`VSA_VINA_BIN` 优先，否则 PATH 里的 `vina`。

    显式设了却不可用就**直接失败，不静默回退到 PATH**：那会让排查指向错误的那个
    引擎。版本留空，由 `vina_version` 异步补。
    """
    raw = os.environ.get(VINA_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ToolError(f"{VINA_ENV} 指向的文件不存在：{path}。{VINA_INSTALL_HINT}")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise ToolError(f"{VINA_ENV} 指向的文件不可执行：{path}。{VINA_INSTALL_HINT}")
        return VinaBinary(str(path))

    found = shutil.which("vina")
    if not found:
        raise ToolError(VINA_INSTALL_HINT)
    return VinaBinary(found)


async def vina_version(binary: str) -> str:
    """跑一次 `--version`；失败返回 `"unknown"`，不阻断对接。"""
    extra: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **extra,
        )
    except OSError:
        return "unknown"

    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        terminate_tree(process)
        await process.communicate()
        return "unknown"
    except asyncio.CancelledError:
        terminate_tree(process)
        raise
    finally:
        release_pipes(process)

    text = decode_output(raw or b"").strip()
    return text.splitlines()[0] if text else "unknown"


def read_box(receptor_dir: Path, meta: dict[str, Any]) -> Box:
    """优先读 `box.txt`；文件不在才回退 `meta.params`。

    文件在但内容坏掉时**不静默回退**：那说明产物被改过，换一个盒子对接出来的
    结果与登记的参数对不上，比直接报错更危险。
    """
    box_name = str(meta.get("box_file") or "box.txt")
    if Path(box_name).name != box_name:
        raise ToolError(f"受体 meta 里的 box_file 不是文件名：{box_name}")
    box_path = Path(receptor_dir) / box_name
    if box_path.is_file():
        return _parse_box_file(box_path)

    params = meta.get("params") or {}
    center = params.get("box_center")
    size = params.get("box_size")
    if _is_triple(center) and _is_triple(size):
        return _make_box(center, size, "meta.params")
    raise ToolError(
        f"受体缺少盒子：{box_path} 不存在，meta.params 里也没有 box_center/box_size。"
        "重新跑 prepare_receptor，或显式给盒子。"
    )


def _parse_box_file(path: Path) -> Box:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ToolError(f"读盒子文件失败（{type(error).__name__}：{error}）") from error
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip().lower()] = value.strip()

    required = ("center_x", "center_y", "center_z", "size_x", "size_y", "size_z")
    missing = [key for key in required if key not in values]
    if missing:
        raise ToolError(f"盒子文件缺字段：{', '.join(missing)}（{path}）")
    try:
        numbers = {key: float(values[key]) for key in required}
    except ValueError as error:
        raise ToolError(f"盒子文件里有非数字：{path}（{error}）") from error
    return _make_box(
        (numbers["center_x"], numbers["center_y"], numbers["center_z"]),
        (numbers["size_x"], numbers["size_y"], numbers["size_z"]),
        "box.txt",
    )


def _is_triple(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and not isinstance(value, (str, bytes))
    )


def _make_box(center: Sequence[Any], size: Sequence[Any], source: str) -> Box:
    try:
        center_f = tuple(float(item) for item in center)
        size_f = tuple(float(item) for item in size)
    except (TypeError, ValueError) as error:
        raise ToolError(f"盒子坐标不是数字：{center!r} / {size!r}") from error
    if len(center_f) != 3 or len(size_f) != 3:
        raise ToolError("盒子要三个中心坐标与三个边长")
    if not all(math.isfinite(item) for item in (*center_f, *size_f)):
        raise ToolError("盒子坐标里有 NaN 或无穷大")
    if any(item <= 0 for item in size_f):
        raise ToolError(f"盒子边长必须大于 0：{size_f}")
    if any(item > MAX_BOX_EDGE for item in size_f):
        raise ToolError(f"盒子边长超过 Vina 的 {MAX_BOX_EDGE:g} Å 上限：{size_f}")
    return Box(center=center_f, size=size_f, source=source)  # type: ignore[arg-type]


def build_command(
    binary: str,
    receptor_pdbqt: Path,
    ligand_pdbqt: Path,
    out_pdbqt: Path,
    box: Box,
    params: DockParams,
) -> list[str]:
    """命令行数组。**不要**把它拼成一个字符串交给 shell。"""
    return [
        binary,
        "--receptor",
        str(receptor_pdbqt),
        "--ligand",
        str(ligand_pdbqt),
        "--center_x",
        f"{box.center[0]:.3f}",
        "--center_y",
        f"{box.center[1]:.3f}",
        "--center_z",
        f"{box.center[2]:.3f}",
        "--size_x",
        f"{box.size[0]:.3f}",
        "--size_y",
        f"{box.size[1]:.3f}",
        "--size_z",
        f"{box.size[2]:.3f}",
        "--exhaustiveness",
        str(params.exhaustiveness),
        "--num_modes",
        str(params.num_modes),
        "--seed",
        str(params.seed),
        "--cpu",
        str(params.cpu),
        "--energy_range",
        f"{params.energy_range:g}",
        "--out",
        str(out_pdbqt),
    ]


async def run_vina(
    argv: Sequence[str], *, cwd: Path, timeout: float, log_path: Path
) -> VinaRun:
    """跑一次 Vina。超时/取消都终止整棵进程树，输出落到日志文件。"""
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
        products.write_text_atomic(log_path, _truncate_log(output))
    except OSError:
        # 日志写不下不该把已经跑完的对接整批丢掉；manifest 里还有截断后的摘要。
        pass
    return VinaRun(
        argv=tuple(argv),
        returncode=int(process.returncode if process.returncode is not None else -1),
        output=output,
        timed_out=timed_out,
        log_path=log_path,
    )


def parse_vina_results(pdbqt_text: str) -> list[VinaMode]:
    """按 `REMARK VINA RESULT` 逐行读；Vina 把最佳位姿写在最前面。"""
    modes: list[VinaMode] = []
    for match in _VINA_RESULT.finditer(pdbqt_text):
        try:
            modes.append(
                VinaMode(
                    score=float(match.group(1)),
                    rmsd_lb=float(match.group(2)),
                    rmsd_ub=float(match.group(3)),
                )
            )
        except ValueError:
            continue
    return modes


def ligand_efficiency(score: float, heavy_atoms: int) -> float | None:
    """LE = -ΔG / 重原子数；分数非有限或重原子为 0 时不给值。"""
    if heavy_atoms <= 0 or not math.isfinite(score):
        return None
    return round(-score / heavy_atoms, 3)


def best_pose_to_pdb(pose_pdbqt: Path, dest: Path) -> bool:
    """把 Vina 输出里的第一个 pose 转成 PDB。

    Meeko 的 `RDKitMolCreate` 用 PDBQT 里的 `REMARK SMILES` 重建键级；Vina 默认会
    保留这行。读不出来就返回 False，让上层记 `no_pdb`，不要吞成“没有位姿”。
    """
    from meeko import PDBQTMolecule, RDKitMolCreate
    from rdkit import Chem, rdBase

    for skip_typing in (True, False):
        try:
            with rdBase.BlockLogs():
                pdbqt_mol = PDBQTMolecule.from_file(
                    str(pose_pdbqt), skip_typing=skip_typing
                )
                molecules = RDKitMolCreate.from_pdbqt_mol(pdbqt_mol)
                if not molecules or molecules[0] is None:
                    continue
                mol = molecules[0]
                if mol.GetNumConformers() == 0:
                    continue
                block = Chem.MolToPDBBlock(mol, confId=0)
        except Exception:
            continue
        if "ATOM" not in block and "HETATM" not in block:
            continue
        try:
            products.write_text_atomic(dest, block)
        except OSError:
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def _receptor_inputs(entry: Any) -> tuple[Path, Box]:
    meta = dict(entry.meta or {})
    if str(meta.get("stage") or "") != "prepared":
        raise ToolError(
            "受体还不处于 prepared 状态。先用 prepare_receptor 的 confirm=true 写盘，"
            "再把它拿来对接。"
        )
    directory = Path(entry.path) if entry.path is not None else None
    if directory is None or not directory.is_dir():
        raise ToolError("prepared 受体的产物目录不在了；重新跑 prepare_receptor。")
    name = str(meta.get("pdbqt_file") or "receptor.pdbqt")
    if Path(name).name != name:
        raise ToolError(f"受体 meta 里的 pdbqt_file 不是文件名：{name}")
    pdbqt = directory / name
    if not pdbqt.is_file():
        raise ToolError(f"受体的 PDBQT 不在：{pdbqt}；重新跑 prepare_receptor。")
    return pdbqt, read_box(directory, meta)


def _ligand_records(entry: Any) -> list[dict[str, Any]]:
    meta = dict(entry.meta or {})
    if str(meta.get("stage") or "") != "prepared":
        raise ToolError(
            "配体集还不处于 prepared 状态。先跑 prepare_ligands（confirm=true），"
            "再把它的 handle 拿来对接。"
        )
    directory = Path(entry.path) if entry.path is not None else None
    if directory is None or not directory.is_dir():
        raise ToolError("prepared 配体集的产物目录不在了；重新跑 prepare_ligands。")
    manifest_path = directory / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ToolError(f"配体集缺少 {MANIFEST_FILE}：{directory}")
    try:
        manifest = products.read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as error:
        raise ToolError(f"读配体集 manifest 失败（{type(error).__name__}：{error}）") from error
    if str(manifest.get("stage") or "") != "prepared":
        raise ToolError("配体集的 manifest 不是 prepared 状态；重新跑 prepare_ligands。")

    root = directory.resolve()
    records: list[dict[str, Any]] = []
    for record in manifest.get("records") or []:
        if str(record.get("status") or "") != "ok":
            continue
        relative = str(record.get("pdbqt") or "")
        if not relative:
            continue
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            continue
        identifier = str(record.get("id") or f"LIG-{len(records) + 1:04d}")
        records.append(
            {
                "id": identifier,
                "name": str(record.get("name") or identifier),
                "smiles": str(record.get("smiles") or ""),
                "heavy_atoms": _as_int(record.get("heavy_atoms")),
                "source": source,
            }
        )
    return records


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 产物与报告
# ---------------------------------------------------------------------------
def _safe_name(identifier: str, used: set[str]) -> str:
    base = products.safe_stem(identifier or "ligand")
    candidate = base
    index = 2
    while candidate.lower() in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate.lower())
    return candidate


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _truncate_log(text: str) -> str:
    if len(text) <= MAX_LOG_CHARS:
        return text
    omitted = len(text) - 2 * LOG_KEEP_CHARS
    return (
        text[:LOG_KEEP_CHARS]
        + f"\n...（日志过长，中间省略 {omitted} 字符）...\n"
        + text[-LOG_KEEP_CHARS:]
    )


def _log_excerpt(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-20:])[:MAX_EXCERPT_CHARS]


def _failure(
    record: dict[str, Any],
    *,
    reason: str,
    detail: str,
    log_relative: str,
    log_text: str,
) -> dict[str, Any]:
    return {
        "id": record["id"],
        "reason": reason,
        "detail": detail[:500],
        "log": log_relative,
        "log_excerpt": _log_excerpt(log_text),
    }


def _failure_digest(failures: Sequence[dict[str, Any]]) -> str:
    if not failures:
        return ""
    lines = ["失败的："]
    for item in failures[:MAX_FAILURES_INLINE]:
        lines.append(f"  · {item['id']}：{item['reason']}：{item['detail']}")
    rest = len(failures) - MAX_FAILURES_INLINE
    if rest > 0:
        lines.append(f"  还有 {rest} 条，完整记录见 {MANIFEST_FILE}。")
    return "\n".join(lines)


def _render_docked(
    *,
    handle: str,
    reused: bool,
    target: Path,
    manifest: dict[str, Any],
) -> str:
    counts = manifest.get("counts") or {}
    results = manifest.get("results") or []
    failures = manifest.get("failures") or []
    box = (manifest.get("receptor") or {}).get("box") or {}
    engine = manifest.get("engine") or {}

    if reused:
        lines = [f"登记表里已有同一份：{handle}（未重复对接）。"]
    else:
        lines = [f"已对接 {counts.get('docked', 0)} 个配体，登记为 {handle}。"]

    center = box.get("center") or [0, 0, 0]
    size = box.get("size") or [0, 0, 0]
    lines.append(
        f"引擎：{engine.get('name') or 'vina'} {engine.get('version') or 'unknown'}；"
        f"盒子中心 ({center[0]:g}, {center[1]:g}, {center[2]:g})，"
        f"边长 ({size[0]:g}, {size[1]:g}, {size[2]:g})。"
    )
    lines.append(
        f"可打分 {counts.get('docked', 0)}、转出 PDB {counts.get('pdb', 0)}、"
        f"失败 {counts.get('failed', 0)}、未处理 {counts.get('remaining', 0)}。"
    )
    if results:
        best = results[0]
        efficiency = best.get("ligand_efficiency")
        value = f"{efficiency:.2f}" if isinstance(efficiency, (int, float)) else "—"
        lines.append(
            f"最佳：{best['score']:.2f} kcal/mol（{best['id']}），配体效率 {value}。"
        )
    digest = _failure_digest(failures)
    if digest:
        lines.append(digest)
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：write_run(receptor=..., docking={handle})。")
    return "\n".join(lines)


def _render_no_docked(
    *, target: Path, counts: dict[str, Any], failures: Sequence[dict[str, Any]]
) -> str:
    lines = [
        f"没有产生可用的对接结果：失败 {counts.get('failed', 0)}、"
        f"未处理 {counts.get('remaining', 0)}。"
    ]
    digest = _failure_digest(failures)
    if digest:
        lines.append(digest)
    lines.append(f"产物目录（未登记）：{target}")
    return "\n".join(lines)


def _docking_meta(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "docking",
        "stage": "docking",
        "tool": "dock",
        "engine": manifest.get("engine") or {},
        "receptor": (manifest.get("receptor") or {}).get("handle") or "",
        "ligands": (manifest.get("ligands") or {}).get("handle") or "",
        "counts": manifest.get("counts") or {},
        "manifest": MANIFEST_FILE,
        "pose_dir": POSE_DIR,
    }


def _docking_entry_complete(entry: Any) -> bool:
    """`manifest.json` + `params.json` 都在就算完整。

    `params.json` 只在一批至少有一条 Vina 结果之后才写，所以它同时是提交标记。
    这里**不要求**一定有 PDB：全部 pose 都转不出来时仍是一份有效的打分结果，
    `write_run` 会在需要位姿时明确失败，而不是让这里反复新建 artifact。
    """
    path = getattr(entry, "path", None)
    if path is None:
        return False
    directory = Path(path)
    if not directory.is_dir():
        return False
    return (directory / MANIFEST_FILE).is_file() and (directory / PARAMS_FILE).is_file()


def _register_docking(
    ledger: SessionLedger,
    *,
    target: Path,
    fingerprint: str,
    summary: str,
    manifest: dict[str, Any],
    receptor: ReceptorHandle,
    ligands: LigandSetHandle,
) -> tuple[str, bool]:
    existing = ledger.find(
        kind="artifact", fingerprint=fingerprint, include_archived=True
    )
    for candidate in existing:
        if str(candidate.meta.get("kind") or "") == "docking" and _docking_entry_complete(candidate):
            return str(candidate.handle), True
    entry = ledger.add(
        "artifact",
        path=target,
        summary=summary,
        fingerprint=fingerprint,
        parents=[receptor, ligands],
        meta=_docking_meta(manifest),
    )
    return str(entry.handle), False


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def _int_argument(
    arguments: dict[str, Any], name: str, default: int, low: int, high: int
) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {name} 应当是整数")
    if not low <= value <= high:
        raise ToolError(f"参数 {name} 要在 {low} 到 {high} 之间（现在是 {value}）")
    return value


def _float_argument(
    arguments: dict[str, Any], name: str, default: float, low: float, high: float
) -> float:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"参数 {name} 应当是数字")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise ToolError(f"参数 {name} 要在 {low:g} 到 {high:g} 之间（现在是 {value}）")
    return number


def _dock_params(arguments: dict[str, Any]) -> DockParams:
    return DockParams(
        exhaustiveness=_int_argument(
            arguments, "exhaustiveness", DEFAULT_EXHAUSTIVENESS, 1, MAX_EXHAUSTIVENESS
        ),
        num_modes=_int_argument(arguments, "num_modes", DEFAULT_NUM_MODES, 1, MAX_NUM_MODES),
        seed=_int_argument(arguments, "seed", DEFAULT_SEED, 0, 2_147_483_647),
        cpu=_int_argument(arguments, "cpu", DEFAULT_CPU, 0, MAX_CPU),
        energy_range=_float_argument(
            arguments, "energy_range", DEFAULT_ENERGY_RANGE, 0.1, 100.0
        ),
        limit=_int_argument(arguments, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        timeout=_float_argument(
            arguments, "timeout", DEFAULT_TIMEOUT, MIN_TIMEOUT, MAX_TIMEOUT
        ),
    )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class DockTool(Tool):
    """把已准备的受体与配体交给 AutoDock Vina。"""

    spec = ToolSpec(
        name="dock",
        description=(
            "用 AutoDock Vina 把一个**已准备**的受体与一组**已准备**的配体对接。"
            "分数**越低越好**（kcal/mol），只能在同一受体、同一盒子内比较，"
            "不是结合自由能，也不能跨靶点比较。"
            f"{CHEM_TOOL_DESCRIPTION_NOTE}"
            "默认 exhaustiveness=8、num_modes=9、limit=50、单配体超时 300 秒；"
            "参数由用户决定，不要自己替用户改。缺 Vina 可执行文件时返回安装指引，不抛异常。"
            "单条失败（超时/非零退出/没有结果）不中断整批，失败原因逐条写进 manifest。"
            "受体必须来自 prepare_receptor 的 confirm=true，配体集必须来自 "
            "prepare_ligands 的 confirm=true；盒子从受体的 box.txt 读，不收模型传的坐标。"
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
                    "description": "已准备的配体集 handle，例如 ligandset#3；不要传原始配体集",
                },
                "exhaustiveness": {
                    "type": "integer",
                    "description": f"Vina 搜索强度，默认 {DEFAULT_EXHAUSTIVENESS}，范围 1–{MAX_EXHAUSTIVENESS}",
                },
                "num_modes": {
                    "type": "integer",
                    "description": f"每个配体最多保留多少个位姿，默认 {DEFAULT_NUM_MODES}，范围 1–{MAX_NUM_MODES}",
                },
                "seed": {
                    "type": "integer",
                    "description": f"Vina 随机种子，默认 {DEFAULT_SEED}；0 表示随机",
                },
                "cpu": {
                    "type": "integer",
                    "description": f"CPU 线程数，默认 {DEFAULT_CPU}（Vina 用全部），范围 0–{MAX_CPU}",
                },
                "energy_range": {
                    "type": "number",
                    "description": f"保留位姿的能量窗口（kcal/mol），默认 {DEFAULT_ENERGY_RANGE:g}，范围 0.1–100",
                },
                "limit": {
                    "type": "integer",
                    "description": f"本次最多对接多少个配体，默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"单个配体的超时秒数，默认 {DEFAULT_TIMEOUT:g}，"
                        f"范围 {MIN_TIMEOUT:g}–{MAX_TIMEOUT:g}；超时只跳过该配体"
                    ),
                },
            },
            "required": ["receptor", "ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "receptor"
    # 换一份配体库重跑是两次不同的调用：只按受体命名会让后一条覆盖前一条。
    evidence_key = ("ligands",)
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：dock 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

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
            receptor_entry = ledger.get(raw_receptor)
            expect_handle(receptor_entry, ReceptorHandle, "receptor")
            ligand_entry = ledger.get(raw_ligands)
            expect_handle(ligand_entry, LigandSetHandle, "ligands")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        try:
            params = _dock_params(arguments)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            binary = resolve_vina()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        version = binary.version or await vina_version(binary.path)

        try:
            receptor_pdbqt, box = _receptor_inputs(receptor_entry)
            records = _ligand_records(ligand_entry)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        except OSError as error:
            return ToolResult(
                output=f"读对接输入失败（{type(error).__name__}：{error}）", failed=True
            )
        if not records:
            return ToolResult(
                output=(
                    "配体集里没有可对接的记录：prepare_ligands 的产物里 status 全是失败，"
                    "或者 PDBQT 文件都不在了。重新跑 prepare_ligands。"
                ),
                failed=True,
            )

        requested = len(records)
        selected = records[: params.limit]
        remaining = requested - len(selected)

        cache_params: dict[str, Any] = {
            "receptor_fingerprint": str(receptor_entry.fingerprint or ""),
            "ligandset_fingerprint": str(ligand_entry.fingerprint or ""),
            "box": {
                "center": [round(item, 3) for item in box.center],
                "size": [round(item, 3) for item in box.size],
            },
            "engine": {"name": "vina", "version": version},
            **params.as_dict(),
        }
        stem = products.safe_stem(
            f"{Path(receptor_entry.path).name}-{Path(ligand_entry.path).name}-docking"
        )
        try:
            target, content_reused = products.content_dir(
                dockings_dir(), stem, cache_params, (MANIFEST_FILE, PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(
                output=f"对接产物目录失败（{type(error).__name__}：{error}）", failed=True
            )

        if content_reused:
            try:
                manifest = products.read_json(target / MANIFEST_FILE)
            except (OSError, json.JSONDecodeError) as error:
                return ToolResult(
                    output=f"复用旧产物时读 {MANIFEST_FILE} 失败（{type(error).__name__}：{error}）",
                    failed=True,
                )
            fingerprint = fingerprint_path(target)
            handle, ledger_reused = _register_docking(
                ledger,
                target=target,
                fingerprint=fingerprint,
                summary=_docking_summary(manifest),
                manifest=manifest,
                receptor=receptor_entry.handle,
                ligands=ligand_entry.handle,
            )
            return ToolResult(
                output=_render_docked(
                    handle=handle, reused=ledger_reused, target=target, manifest=manifest
                ),
                meta=_docking_meta_result(manifest, handle, fingerprint, ledger_reused),
            )

        target.mkdir(parents=True, exist_ok=True)
        (target / POSE_DIR).mkdir(parents=True, exist_ok=True)
        (target / LOG_DIR).mkdir(parents=True, exist_ok=True)
        used_names: set[str] = set()
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for record in selected:
            safe_name = _safe_name(record["id"], used_names)
            out_pdbqt = target / POSE_DIR / f"{safe_name}_out.pdbqt"
            pose_pdb = target / POSE_DIR / f"{safe_name}.pdb"
            log_path = target / LOG_DIR / f"{safe_name}.log"
            log_relative = _relative(target, log_path)
            argv = build_command(
                binary.path, receptor_pdbqt, record["source"], out_pdbqt, box, params
            )

            try:
                run = await run_vina(
                    argv, cwd=target, timeout=params.timeout, log_path=log_path
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 起进程失败、管道异常这类都算这一条的环境问题；不中断整批。
                failures.append(
                    _failure(
                        record,
                        reason="engine_error",
                        detail=f"起 Vina 失败（{type(error).__name__}：{error}）",
                        log_relative=log_relative,
                        log_text="",
                    )
                )
                continue

            if run.timed_out:
                failures.append(
                    _failure(
                        record,
                        reason="timeout",
                        detail=f"超过 {params.timeout:g} 秒，进程树已终止",
                        log_relative=log_relative,
                        log_text=run.output,
                    )
                )
                continue
            if run.returncode != 0:
                failures.append(
                    _failure(
                        record,
                        reason=f"vina_exit_{run.returncode}",
                        detail=f"Vina 非零退出（{run.returncode}）",
                        log_relative=log_relative,
                        log_text=run.output,
                    )
                )
                continue
            if not out_pdbqt.is_file():
                failures.append(
                    _failure(
                        record,
                        reason="no_output",
                        detail=f"Vina 退出 0，但没有写出 {out_pdbqt.name}",
                        log_relative=log_relative,
                        log_text=run.output,
                    )
                )
                continue
            try:
                text = out_pdbqt.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                failures.append(
                    _failure(
                        record,
                        reason="no_output",
                        detail=f"读 Vina 输出失败（{type(error).__name__}：{error}）",
                        log_relative=log_relative,
                        log_text=run.output,
                    )
                )
                continue

            modes = parse_vina_results(text)
            if not modes or not math.isfinite(modes[0].score):
                failures.append(
                    _failure(
                        record,
                        reason="no_result",
                        detail="Vina 输出里没有可解析的 REMARK VINA RESULT",
                        log_relative=log_relative,
                        log_text=run.output,
                    )
                )
                continue

            best = modes[0]
            converted = best_pose_to_pdb(out_pdbqt, pose_pdb)
            result = {
                "id": record["id"],
                "name": record["name"],
                "smiles": record["smiles"],
                "heavy_atoms": record["heavy_atoms"],
                "score": best.score,
                "rmsd_lb": best.rmsd_lb,
                "rmsd_ub": best.rmsd_ub,
                "ligand_efficiency": ligand_efficiency(
                    best.score, record["heavy_atoms"]
                ),
                "modes": len(modes),
                "pose_pdbqt": _relative(target, out_pdbqt),
                "pose_pdb": _relative(target, pose_pdb) if converted else "",
                "log": log_relative,
                "status": "ok" if converted else "no_pdb",
                "error": "" if converted else "最佳 pose 转不成 PDB",
            }
            results.append(result)
            if not converted:
                failures.append(
                    _failure(
                        record,
                        reason="no_pdb",
                        detail="Vina 有分数，但最佳 pose 转不成 PDB（通常缺 REMARK SMILES）",
                        log_relative=log_relative,
                        log_text=run.output,
                    )
                )

        results.sort(key=lambda item: (item["score"], item["id"]))
        for index, item in enumerate(results, start=1):
            item["rank"] = index
        best_score = results[0]["score"] if results else None
        pdb_count = sum(1 for item in results if item["status"] == "ok")
        counts: dict[str, Any] = {
            "input": len(selected),
            "docked": len(results),
            "pdb": pdb_count,
            "failed": len(failures),
            "remaining": remaining,
            "best_score": best_score,
        }

        if not results:
            return ToolResult(
                output=_render_no_docked(
                    target=target, counts=counts, failures=failures
                ),
                failed=True,
            )

        manifest = {
            "kind": "docking",
            "stage": "docking",
            "tool": "dock",
            "engine": {
                "name": "vina",
                "binary": binary.path,
                "version": version,
            },
            "receptor": {
                "handle": str(receptor_entry.handle),
                "fingerprint": str(receptor_entry.fingerprint or ""),
                "box": {
                    "center": [round(item, 3) for item in box.center],
                    "size": [round(item, 3) for item in box.size],
                    "source": box.source,
                },
            },
            "ligands": {
                "handle": str(ligand_entry.handle),
                "fingerprint": str(ligand_entry.fingerprint or ""),
                "requested": requested,
                "used": len(selected),
            },
            "params": params.as_dict(),
            "params_fingerprint": fingerprint_parts(cache_params, length=16),
            "counts": counts,
            "results": results,
            "failures": failures,
        }
        try:
            products.write_json_atomic(target / MANIFEST_FILE, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / PARAMS_FILE, cache_params)
        except OSError as error:
            return ToolResult(
                output=f"写对接产物失败（{type(error).__name__}：{error}）", failed=True
            )

        fingerprint = fingerprint_path(target)
        handle, ledger_reused = _register_docking(
            ledger,
            target=target,
            fingerprint=fingerprint,
            summary=_docking_summary(manifest),
            manifest=manifest,
            receptor=receptor_entry.handle,
            ligands=ligand_entry.handle,
        )
        return ToolResult(
            output=_render_docked(
                handle=handle, reused=ledger_reused, target=target, manifest=manifest
            ),
            meta=_docking_meta_result(manifest, handle, fingerprint, ledger_reused),
        )


def _docking_summary(manifest: dict[str, Any]) -> str:
    counts = manifest.get("counts") or {}
    best = counts.get("best_score")
    if isinstance(best, (int, float)):
        return f"对接产物 · {counts.get('docked', 0)} 个结果 · 最佳 {best:.2f} kcal/mol"
    return f"对接产物 · {counts.get('docked', 0)} 个结果"


def _docking_meta_result(
    manifest: dict[str, Any], handle: str, fingerprint: str, reused: bool
) -> dict[str, Any]:
    counts = manifest.get("counts") or {}
    return {
        "handle": handle,
        "fingerprint": fingerprint,
        "docked": counts.get("docked", 0),
        "pdb": counts.get("pdb", 0),
        "failed": counts.get("failed", 0),
        "best_score": counts.get("best_score"),
        "remaining": counts.get("remaining", 0),
        "reused": reused,
    }


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [DockTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
