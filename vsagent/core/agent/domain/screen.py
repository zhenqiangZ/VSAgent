"""不看靶点的筛选通道：2D 指纹相似度、3D 形状。

与 `docking.py` 的分工：那边问「这个分子能不能塞进这个口袋」，这边问「它长得像不像
已知活性的那些」。两者互相独立，`rank_candidates` 再把它们并成一张表——**只有一路
证据的候选表不叫共识**，这也是这个模块存在的主要理由。

**分数一律是「越高越像」**：Tanimoto 与形状分都是 0–1，方向写死在产物 meta 里
（`higher_is_better`），不交给模型判断。

**查询集与待筛库都收 handle**：参考集是 `build_reference_set` 的产物，待筛库是
`import_ligandset` / `curate_ligands` 的产物，两者形状一致（`ligands.csv`），
所以这里只认 handle，不收路径。
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any, Callable, Sequence

from ..ledger import (
    LigandSetHandle,
    SessionLedger,
    expect_handle,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import ligand, products
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem

__all__ = [
    "PickDiverseTool",
    "ScreenShapeTool",
    "ScreenSimilarityTool",
    "candidates_dir",
    "cluster_by_similarity",
    "domain_tools",
    "pose_jaccard",
    "pose_volume_cells",
    "selected_dir",
    "usr_descriptor",
]

CANDIDATES_FILE = "candidates.json"
CANDIDATES_CSV = "candidates.csv"
PARAMS_FILE = products.PARAMS_FILE

DEFAULT_TOP = 100
MAX_TOP = 2000
FP_BITS = 2048
ECFP_RADIUS = 2

METRICS: dict[str, str] = {
    "ecfp4": f"Morgan/ECFP4 半径 {ECFP_RADIUS}、{FP_BITS} 位",
    "maccs": "MACCS 167 位",
    "atompair": f"AtomPair {FP_BITS} 位",
}

SCORE_COLUMN = "similarity"


def candidates_dir() -> Path:
    """相似度产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".vsagent" / "screens"


def selected_dir() -> Path:
    """挑选结果（配体集）的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".vsagent" / "selected"


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def fingerprint_for(mol: Any, metric: str):
    """按 `metric` 算一个位向量指纹。新老 RDKit 的 API 都在这里收口。"""
    from rdkit.Chem import AllChem, MACCSkeys

    if metric == "maccs":
        return MACCSkeys.GenMACCSKeys(mol)
    try:
        if metric == "atompair":
            generator = AllChem.GetAtomPairGenerator(fpSize=FP_BITS)
        else:
            generator = AllChem.GetMorganGenerator(radius=ECFP_RADIUS, fpSize=FP_BITS)
        return generator.GetFingerprint(mol)
    except AttributeError:  # rdkit < 2022.09 没有生成器
        if metric == "atompair":
            return AllChem.GetHashedAtomPairFingerprintAsBitVect(mol, nBits=FP_BITS)
        return AllChem.GetMorganFingerprintAsBitVect(mol, ECFP_RADIUS, nBits=FP_BITS)


# ---------------------------------------------------------------------------
# 读写配体集
# ---------------------------------------------------------------------------
def _smiles_of(entry: Any) -> list[dict[str, str]]:
    """把一个配体集读成 `[{id, name, smiles}]`。形状由 `ligand._source_cleaned` 保证。"""
    try:
        items, _kind = ligand._source_cleaned(entry)
    except ligand.LigandError as error:
        raise ToolError(str(error)) from error
    return [
        {"id": item.id, "name": item.name, "smiles": item.cleaned.smiles}
        for item in items
        if item.cleaned.smiles
    ]


def _query_molecules(arguments: dict[str, Any], ledger: SessionLedger) -> list[dict[str, str]]:
    """查询集：给 handle 就整组都当查询，给 smiles 就一条（调用方已保证二选一）。"""
    raw_handle = str(arguments.get("query") or "").strip()
    raw_smiles = str(arguments.get("smiles") or "").strip()
    if raw_smiles:
        return [{"id": "query", "name": "query", "smiles": raw_smiles}]
    entry = ledger.get(raw_handle)
    expect_handle(entry, LigandSetHandle, "query")
    return _smiles_of(entry)


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
def _csv_text(rows: Sequence[dict[str, Any]], column: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["id", "name", "smiles", column, "rank"])
    for index, row in enumerate(rows, start=1):
        writer.writerow(
            [row["id"], row["name"], row["smiles"], f"{row['score']:.4f}", index]
        )
    return buffer.getvalue()


def write_candidates(
    *,
    ledger: SessionLedger,
    root: Path,
    stem: str,
    tool: str,
    params: dict[str, Any],
    column: str,
    rows: Sequence[dict[str, Any]],
    parents: Sequence[Any],
    extra_meta: dict[str, Any] | None = None,
) -> tuple[str, Path, bool]:
    """写一份候选表并登记。返回 `(handle, 目录, 是否复用)`。

    `screen_similarity` 与 `screen_shape` 共用：两者产物的形状一样，只是分数列不同。
    **列名写进 meta（`score_column`）**——`rank_candidates` 靠它决定读哪一列、方向如何，
    不用去猜「这张表里 0.87 是相似度还是形状分」。
    """
    target, reused = products.content_dir(
        root, stem, params, (CANDIDATES_FILE, CANDIDATES_CSV, PARAMS_FILE)
    )
    if reused:
        existing = ledger.find(
            kind="artifact", fingerprint=fingerprint_path(target), include_archived=True
        )
        for candidate in existing:
            if str((candidate.meta or {}).get("kind") or "") == "candidates":
                return str(candidate.handle), target, True

    payload = {
        "kind": "candidates",
        "score_column": column,
        "higher_is_better": True,
        "sources": [str(parent.handle) for parent in parents],
        "counts": {"rows": len(rows)},
        "rows": [
            {
                "id": row["id"],
                "name": row["name"],
                "smiles": row["smiles"],
                "scores": {column: row["score"]},
                "sources": [str(parent.handle) for parent in parents],
            }
            for row in rows
        ],
    }
    if extra_meta:
        payload.update(extra_meta)
    try:
        products.write_json_atomic(target / CANDIDATES_FILE, payload)
        products.write_text_atomic(target / CANDIDATES_CSV, _csv_text(rows, column))
        # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
        products.write_json_atomic(target / PARAMS_FILE, params)
    except OSError as error:
        raise ToolError(f"写候选表失败（{error}）") from error

    entry = ledger.add(
        "artifact",
        path=target,
        summary=f"候选表 · {column} · {len(rows)} 条",
        fingerprint=fingerprint_path(target),
        parents=list(parents),
        meta={
            "kind": "candidates",
            "stage": "candidates",
            "tool": tool,
            "score_column": column,
            "higher_is_better": True,
            "counts": {"rows": len(rows)},
            "params": params,
            "file": CANDIDATES_FILE,
            "csv": CANDIDATES_CSV,
        },
    )
    return str(entry.handle), target, False


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def _int_argument(arguments: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {name} 应当是整数")
    if not low <= value <= high:
        raise ToolError(f"参数 {name} 要在 {low} 到 {high} 之间（现在是 {value}）")
    return value


def _number_argument(
    arguments: dict[str, Any], name: str, default: float, low: float, high: float
) -> float:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"参数 {name} 应当是数")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise ToolError(f"参数 {name} 要在 {low:g} 到 {high:g} 之间（现在是 {number:g}）")
    return number


def _cutoff_argument(arguments: dict[str, Any]) -> float:
    value = arguments.get("cutoff")
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("参数 cutoff 应当是 0 到 1 之间的数")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ToolError(f"参数 cutoff 要在 0 到 1 之间（现在是 {number:g}）")
    return number


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class ScreenSimilarityTool(Tool):
    """按 2D 指纹相似度给整个库排序。"""

    spec = ToolSpec(
        name="screen_similarity",
        description=(
            "按 **2D 指纹相似度**给一个库排序，产出一张候选表（分数列 similarity，越高越像）。"
            + CHEM_TOOL_DESCRIPTION_NOTE
            + "\n"
            "查询可以是参考集（`query`，通常是 build_reference_set 的产物），"
            "也可以是一个分子（`smiles`），二选一；库用 `library`。多个查询时取"
            "**最像的那一个**作为这个分子的分数——「像不像已知活性」问的是最近邻。"
            "给了 `smarts` 就只做子结构检索，相似度分不参与。\n"
            "Tanimoto 由 RDKit 算，指纹类型与位数写在产物里；换 metric 会重新算，不复用。"
            "下一步：把这张表和 dock 的产物一起交给 rank_candidates。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询配体集 handle（参考集），与 smiles 二选一",
                },
                "smiles": {"type": "string", "description": "单个查询分子的 SMILES"},
                "library": {"type": "string", "description": "待筛配体集 handle"},
                "metric": {
                    "type": "string",
                    "description": "ecfp4（默认）/ maccs / atompair",
                },
                "smarts": {
                    "type": "string",
                    "description": "子结构查询；给了它就不再算相似度，只留匹配上的分子",
                },
                "cutoff": {
                    "type": "number",
                    "description": "相似度下限（0–1），默认 0（不设限）",
                },
                "top": {
                    "type": "integer",
                    "description": f"最多保留多少条，默认 {DEFAULT_TOP}，上限 {MAX_TOP}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["library"],
            "additionalProperties": False,
        },
    )
    primary_argument = "library"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：screen_similarity 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw_library = str(arguments.get("library") or "").strip()
        if not raw_library:
            return ToolResult(
                output="缺少参数 library。先 list_workspace 看清有哪份待筛配体集。", failed=True
            )
        raw_query = str(arguments.get("query") or "").strip()
        raw_smiles = str(arguments.get("smiles") or "").strip()
        if raw_query and raw_smiles:
            return ToolResult(
                output="query 与 smiles 只能给一个：给 query 是把整组当查询，给 smiles 是一条。",
                failed=True,
            )
        if not raw_query and not raw_smiles:
            return ToolResult(
                output="缺少查询：给 query（参考集 handle）或 smiles（单个分子）二选一。",
                failed=True,
            )

        metric = str(arguments.get("metric") or "ecfp4").strip().lower()
        if metric not in METRICS:
            return ToolResult(
                output=f"参数 metric 只能是 {', '.join(METRICS)}（现在是 {metric}）", failed=True
            )
        try:
            top = _int_argument(arguments, "top", DEFAULT_TOP, 1, MAX_TOP)
            cutoff = _cutoff_argument(arguments)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            library_entry = ledger.get(raw_library)
            expect_handle(library_entry, LigandSetHandle, "library")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            queries = _query_molecules(arguments, ledger)
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        if not queries:
            return ToolResult(
                output="查询集是空的：参考集里一条带 SMILES 的分子都没有。"
                "先 build_reference_set，或直接给一个 smiles。",
                failed=True,
            )
        try:
            library_rows = _smiles_of(library_entry)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        if not library_rows:
            return ToolResult(output=f"{library_entry.handle} 里没有可筛的分子。", failed=True)

        from rdkit import Chem, DataStructs, rdBase

        smarts = str(arguments.get("smarts") or "").strip()
        pattern = None
        if smarts:
            with rdBase.BlockLogs():
                pattern = Chem.MolFromSmarts(smarts)
            if pattern is None:
                return ToolResult(
                    output=(
                        f"SMARTS 解析不了：{smarts!r}。"
                        "常见原因是括号不配对或原子符号写错；先在 RDKit 里试一次再给。"
                    ),
                    failed=True,
                )

        scored: list[dict[str, Any]] = []
        with rdBase.BlockLogs():
            query_fps = []
            for item in queries:
                mol = Chem.MolFromSmiles(item["smiles"])
                if mol is None:
                    continue
                query_fps.append(fingerprint_for(mol, metric))
            if not smarts and not query_fps:
                return ToolResult(
                    output="查询分子一条都解析不了（SMILES 不合法）。", failed=True
                )
            for item in library_rows:
                mol = Chem.MolFromSmiles(item["smiles"])
                if mol is None:
                    continue
                if pattern is not None:
                    if not mol.HasSubstructMatch(pattern):
                        continue
                    score = 1.0
                else:
                    probe = fingerprint_for(mol, metric)
                    score = max(
                        DataStructs.TanimotoSimilarity(probe, reference)
                        for reference in query_fps
                    )
                if score < cutoff:
                    continue
                scored.append({**item, "score": float(score)})

        if not scored:
            return ToolResult(
                output=(
                    f"没有一条达到 cutoff={cutoff:g}"
                    + (f"，也没有一条匹配 {smarts}" if smarts else "")
                    + f"（库 {len(library_rows)} 条、查询 {len(query_fps)} 条）。"
                    "把 cutoff 调低或换 metric 再试；不要凭这一点说库是空的。"
                ),
                failed=True,
            )

        scored.sort(key=lambda row: (-row["score"], row["id"]))
        total = len(scored)
        kept = scored[:top]

        params = {
            "query": raw_query or raw_smiles,
            "library": str(library_entry.handle),
            "metric": metric,
            "smarts": smarts,
            "cutoff": cutoff,
            "top": top,
        }
        parents = [library_entry]
        if raw_query:
            parents.insert(0, ledger.get(raw_query))
        stem = str(arguments.get("name") or f"similarity-{metric}")
        try:
            handle, target, reused = write_candidates(
                ledger=ledger,
                root=candidates_dir(),
                stem=stem,
                tool="screen_similarity",
                params=params,
                column=SCORE_COLUMN,
                rows=kept,
                parents=parents,
                extra_meta={
                    "metric": metric,
                    "fingerprint": METRICS[metric],
                    "smarts": smarts,
                    "cutoff": cutoff,
                    "truncated": total - len(kept),
                },
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        lines = [
            (
                f"按子结构 {smarts} 筛完，登记为 {handle}。"
                if smarts
                else f"按 {METRICS[metric]} 筛完，登记为 {handle}。"
            )
            if not reused
            else f"登记表里已有同一份：{handle}（未重算）。",
            f"命中 {total} 条，保留 {len(kept)} 条"
            + (f"，另有 {total - len(kept)} 条没列出来（调大 top）" if total > len(kept) else "")
            + "。",
            f"查询：{'、'.join(item['id'] for item in queries[:5])}"
            + ("……" if len(queries) > 5 else "")
            + f"（{len(queries)} 条，取最近邻）",
        ]
        if smarts:
            lines.append(f"子结构：{smarts}（只留匹配上的，分数恒为 1）")
        lines.append(f"最高 {kept[0]['score']:.3f}：{kept[0]['id']}")
        lines.append(f"产物目录：{target}")
        lines.append(
            f"下一步：rank_candidates(sources=dock产物,{handle}) 把它并进共识表。"
        )
        return ToolResult(
            output="\n".join(lines),
            meta={
                "handle": handle,
                "count": len(kept),
                "hit": total,
                "reused": reused,
                "metric": metric,
            },
        )


# ---------------------------------------------------------------------------
# 3D 形状：USR（Ultrafast Shape Recognition）
# ---------------------------------------------------------------------------
SHAPE_COLUMN = "shape"
SHAPE_LIMIT = 2000
SHAPE_METRICS = ("shape_tanimoto", "overlap")
SHAPE_NOTE = "USR 12 维形状描述子（对齐无关），相似度 1/(1+平均绝对差/Å)"

# 范德华半径（Å）。只用于把坐标里明显不是重原子的项筛掉与描述子的参考点，
# 不参与体积积分——这里算的是距离分布，不是网格重叠。
VDW_RADII: dict[str, float] = {
    "C": 1.70, "N": 1.55, "O": 1.52, "S": 1.80, "P": 1.80,
    "F": 1.47, "CL": 1.75, "BR": 1.85, "I": 1.98,
}


def _pdbqt_points(path: Path) -> list[tuple[float, float, float]]:
    """从 PDBQT 读重原子坐标。

    不用 `pdbtext.parse`：它把 77–78 列当元素，而 PDBQT 那里放的是**原子类型**
    （`A` 代表芳香碳、`OA` 代表受体氧、`HD` 代表极性氢）。按元素读会把 `HD` 当成
    重原子算进去，于是同一份分子在「加没加氢」两种准备参数下形状分不一样。
    """
    points: list[tuple[float, float, float]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return points
    for line in text.splitlines():
        if line[:6].strip() not in ("ATOM", "HETATM"):
            continue
        if len(line) < 54:
            continue
        kind = line[77:79].strip().upper() if len(line) >= 79 else ""
        if not kind:
            kind = line[12:16].strip().upper()
        if kind.startswith("H"):
            continue
        try:
            points.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
        except ValueError:
            continue
    return points


def _pdb_points(path: Path) -> list[tuple[float, float, float]]:
    """从普通 PDB 读重原子坐标（共晶配体用）。"""
    from . import pdbtext

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        doc = pdbtext.parse(text)
    except Exception:
        return []
    return [atom.coord for atom in doc.atoms if pdbtext.is_heavy(atom)]


def _moments(values: Sequence[float]) -> tuple[float, float, float]:
    """均值 / 标准差 / 偏度。USR 的每一族距离都取这三个数。"""
    count = len(values)
    if not count:
        return 0.0, 0.0, 0.0
    mean = sum(values) / count
    if count < 2:
        return mean, 0.0, 0.0
    variance = sum((value - mean) ** 2 for value in values) / count
    deviation = math.sqrt(variance)
    if deviation == 0:
        return mean, 0.0, 0.0
    skew = sum(((value - mean) / deviation) ** 3 for value in values) / count
    return mean, deviation, skew


def usr_descriptor(points: Sequence[Sequence[float]]) -> list[float]:
    """12 维 USR 描述子：四族距离（到质心 / 最近原子 / 最远原子 / 第三参考点）各三阶矩。

    **为什么用它而不是体积重叠**：体积重叠要先把两个分子叠到一起，叠合本身就要
    找最大公共子结构，几千个分子×每个几十毫秒是分钟级；USR 只看距离分布，
    **对齐无关**，一个分子几微秒。代价是它分不清「同一堆原子摆成什么形状」，
    只分得清「摊开还是团着」——对粗筛够用，对精细排序不够。
    """
    if len(points) < 2:
        return []
    centroid = [
        sum(point[axis] for point in points) / len(points) for axis in range(3)
    ]
    farthest_from_centroid = max(
        points, key=lambda point: math.dist(point, centroid)
    )
    farthest_from_second = max(
        points, key=lambda point: math.dist(point, farthest_from_centroid)
    )

    to_centroid: list[float] = []
    to_nearest: list[float] = []
    to_far: list[float] = []
    to_third: list[float] = []
    for point in points:
        to_centroid.append(math.dist(point, centroid))
        to_nearest.append(
            min(
                (math.dist(point, other) for other in points if other is not point),
                default=0.0,
            )
        )
        to_far.append(math.dist(point, farthest_from_centroid))
        to_third.append(math.dist(point, farthest_from_second))

    descriptor: list[float] = []
    for series in (to_centroid, to_nearest, to_far, to_third):
        descriptor.extend(_moments(series))
    return descriptor


def _usr_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """`1 / (1 + 平均绝对差)`。两边维数不同（原子太少）时返回 0。"""
    if len(left) != len(right) or not left:
        return 0.0
    delta = sum(abs(a - b) for a, b in zip(left, right)) / len(left)
    return 1.0 / (1.0 + delta)


def _prepared_records(entry: Any) -> tuple[list[dict[str, Any]], str]:
    """读已备 3D 的配体集：返回 (记录, 错误说明)。"""
    meta = dict(entry.meta or {})
    if str(meta.get("stage") or "") != "prepared":
        return [], (
            f"{entry.handle} 还没有准备 3D（stage={meta.get('stage') or '未知'}）。"
            "先用 prepare_ligands 把 SMILES 变成 PDBQT——形状要看原子在空间里怎么摆。"
        )
    manifest_path = Path(str(entry.path)) / ligand.MANIFEST_FILE
    try:
        manifest = products.read_json(manifest_path)
    except (OSError, ValueError) as error:
        return [], f"读 {manifest_path} 失败（{type(error).__name__}：{error}）"
    # `pdbqt` 里**已经带了 `ligands/` 前缀**（`prepare_ligands` 写的是相对产物根的
    # 路径，见 `ligand.py` 的 `f"{PDBQT_DIR}/{filename}"`）。这里原先又拼了一次
    # `meta["pdbqt_dir"]`，于是变成 `…/ligands/ligands/x.pdbqt`——库里的每一条都
    # 读不出坐标，而报出来的是「5 条读不出坐标」，看着像分子的问题。
    # 判据当时没抓住，因为夹具里写的 `pdbqt` 是裸文件名，与真实产物不同形。
    root = Path(str(entry.path))
    records = []
    for item in manifest.get("records") or []:
        if str(item.get("status") or "") != "ok" or not item.get("pdbqt"):
            continue
        records.append({**item, "path": root / str(item["pdbqt"])})
    return records, ""


class ScreenShapeTool(Tool):
    """按 3D 形状（USR 描述子）给一个库排序。"""

    spec = ToolSpec(
        name="screen_shape",
        description=(
            "按 **3D 形状**给一个库排序，产出候选表（分数列 shape，越高越像）。"
            + CHEM_TOOL_DESCRIPTION_NOTE
            + "\n"
            "两侧都要**已经准备过 3D**（`prepare_ligands` 的产物）；查询也可以是"
            "准备过的受体（用它的共晶配体）。缺 3D 会直接说缺，不会静默跳过。\n"
            "用的是 USR（12 维距离分布，**对齐无关**），不是体积重叠：它分得清"
            "「摊开还是团着」，分不清细节；要精细排序靠 dock 与 2D 相似度，"
            "形状在这里是粗筛。分数与 Tanimoto 不可比，不要混着排序。\n"
            "下一步：把这张表和 dock / screen_similarity 的产物一起交给 rank_candidates。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询：已备 3D 的配体集，或准备过的受体（取其共晶配体）",
                },
                "library": {"type": "string", "description": "已备 3D 的待筛配体集"},
                "metric": {
                    "type": "string",
                    "description": "shape_tanimoto（默认，USR 相似度）/ overlap（同口径，保留别名）",
                },
                "cutoff": {
                    "type": "number",
                    "description": "形状分下限（0–1），默认 0（不设限）",
                },
                "top": {
                    "type": "integer",
                    "description": f"最多保留多少条，默认 {DEFAULT_TOP}，上限 {MAX_TOP}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["query", "library"],
            "additionalProperties": False,
        },
    )
    primary_argument = "library"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：screen_shape 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw_query = str(arguments.get("query") or "").strip()
        raw_library = str(arguments.get("library") or "").strip()
        if not raw_query or not raw_library:
            return ToolResult(
                output="缺少参数：query（已备 3D 的查询）与 library（已备 3D 的库）都要给。",
                failed=True,
            )
        metric = str(arguments.get("metric") or SHAPE_METRICS[0]).strip().lower()
        if metric not in SHAPE_METRICS:
            return ToolResult(
                output=f"参数 metric 只能是 {', '.join(SHAPE_METRICS)}（现在是 {metric}）",
                failed=True,
            )
        try:
            top = _int_argument(arguments, "top", DEFAULT_TOP, 1, MAX_TOP)
            cutoff = _cutoff_argument(arguments)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            query_entry = ledger.get(raw_query)
            library_entry = ledger.get(raw_library)
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        try:
            expect_handle(library_entry, LigandSetHandle, "library")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        # 查询：配体集 → 每个已备分子一份坐标；受体 → 它的共晶配体一份。
        query_shapes: list[tuple[str, list[float]]] = []
        if getattr(query_entry.handle, "kind", "") == "receptor":
            meta = dict(query_entry.meta or {})
            ligand_name = str(meta.get("ligand_file") or "").strip()
            if not ligand_name:
                return ToolResult(
                    output=(
                        f"{query_entry.handle} 里没有共晶配体（`ligand_file` 为空），"
                        "拿不到查询形状。改用已准备 3D 的配体集当 query。"
                    ),
                    failed=True,
                )
            points = _pdb_points(Path(str(query_entry.path)) / ligand_name)
            descriptor = usr_descriptor(points)
            if not descriptor:
                return ToolResult(
                    output=f"共晶配体 {ligand_name} 里读不出重原子坐标。", failed=True
                )
            query_shapes.append((str(query_entry.handle), descriptor))
        else:
            try:
                expect_handle(query_entry, LigandSetHandle, "query")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            query_records, reason = _prepared_records(query_entry)
            if reason:
                return ToolResult(output=reason, failed=True)
            for item in query_records:
                descriptor = usr_descriptor(_pdbqt_points(item["path"]))
                if descriptor:
                    query_shapes.append((str(item["id"]), descriptor))
        if not query_shapes:
            return ToolResult(
                output="查询侧一个可用的 3D 形状都没有（准备失败的分子没有 PDBQT）。",
                failed=True,
            )

        library_records, reason = _prepared_records(library_entry)
        if reason:
            return ToolResult(output=reason, failed=True)
        if not library_records:
            return ToolResult(
                output=f"{library_entry.handle} 里没有准备成功的分子（没有 PDBQT）。",
                failed=True,
            )
        batch = library_records[:SHAPE_LIMIT]
        skipped = len(library_records) - len(batch)

        scored: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in batch:
            points = _pdbqt_points(item["path"])
            descriptor = usr_descriptor(points)
            if not descriptor:
                failed.append(
                    {"id": item["id"], "reason": f"{item['path'].name} 里读不出重原子坐标"}
                )
                continue
            best = max(
                (_usr_similarity(descriptor, other) for _label, other in query_shapes),
                default=0.0,
            )
            if best < cutoff:
                continue
            scored.append(
                {
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                    "smiles": str(item.get("smiles") or ""),
                    "score": float(best),
                }
            )

        if not scored:
            return ToolResult(
                output=(
                    f"没有一条达到 cutoff={cutoff:g}（库 {len(batch)} 条，"
                    f"{len(failed)} 条读不出坐标）。形状分的量级比 Tanimoto 小，"
                    "默认 0 是不设限；先看一遍分布再定阈值。"
                ),
                failed=True,
            )

        scored.sort(key=lambda row: (-row["score"], row["id"]))
        total = len(scored)
        kept = scored[:top]
        params = {
            "query": str(query_entry.handle),
            "library": str(library_entry.handle),
            "metric": metric,
            "cutoff": cutoff,
            "top": top,
            "descriptor": SHAPE_NOTE,
        }
        stem = str(arguments.get("name") or "shape")
        try:
            handle, target, reused = write_candidates(
                ledger=ledger,
                root=candidates_dir(),
                stem=stem,
                tool="screen_shape",
                params=params,
                column=SHAPE_COLUMN,
                rows=kept,
                parents=[query_entry, library_entry],
                extra_meta={
                    "metric": metric,
                    "descriptor": SHAPE_NOTE,
                    "cutoff": cutoff,
                    "truncated": total - len(kept),
                    "alignment_failures": failed[:50],
                    "not_processed": skipped,
                },
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        lines = [
            f"按 3D 形状（{SHAPE_NOTE}）筛完，登记为 {handle}。"
            if not reused
            else f"登记表里已有同一份：{handle}（未重算）。",
            f"命中 {total} 条，保留 {len(kept)} 条"
            + (f"，另有 {total - len(kept)} 条没列出来（调大 top）" if total > len(kept) else "")
            + "。",
            f"最高 {kept[0]['score']:.3f}：{kept[0]['id']}（与 Tanimoto 不是一个量纲，别混着排名）",
        ]
        if failed:
            lines.append(f"{len(failed)} 条读不出坐标，前几条：")
            for item in failed[:3]:
                lines.append(f"  · {item['id']}：{item['reason']}")
        if skipped:
            lines.append(
                f"还有 {skipped} 条没处理（单批上限 {SHAPE_LIMIT}）；"
                "分批跑，或用 curate_ligands 先缩小库。"
            )
        lines.append(f"产物目录：{target}")
        lines.append(f"下一步：rank_candidates 把它与 dock / 相似度的产物并起来。")
        return ToolResult(
            output="\n".join(lines),
            meta={
                "handle": handle,
                "count": len(kept),
                "hit": total,
                "failed": len(failed),
                "skipped": skipped,
                "reused": reused,
            },
        )


# ---------------------------------------------------------------------------
# 挑选：从一批候选里挑出彼此不像的那些
#
# 相似度与形状两路都是「按分数排序再截断」，而截断留下的是近邻：一个库里同一骨架
# 换基团的几十个分子会把名额占满。这里补的是截断之前的那一步——先去重，再按
# 两两不相似度取，最后按骨架配额兜底。**三步都不看分数**，分数只决定从哪里开始。
# ---------------------------------------------------------------------------
SELECTED_STAGE = "selected"
SELECTED_MANIFEST = "manifest.json"
SELECTED_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "smiles",
    "input_smiles",
    "heavy_atoms",
    "pchembl_value",
    "source_index",
    "scaffold",
    "cluster",
    "max_similarity",
    "rank",
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
DEFAULT_MAX_SIMILARITY = 0.6
DEFAULT_PER_SCAFFOLD = 3
# 位姿去重的体积口径：重原子为中心、半径 1.8 埃的球并起来，1.0 埃一格。
# 判据取体积而不是中心距：两个分子可以中心差 3 埃而形状完全错开。
POSE_SPHERE_RADIUS = 1.8
POSE_VOXEL = 1.0
POSE_JACCARD = 0.5

# 组外探索的停止阈值。由论文的三轮经验换算，尚未在真实库上实测。
STOP_OVERLAP = 0.6
STOP_NEW_SCAFFOLDS = 2


def _ranked_rows(entry: Any) -> tuple[list[dict[str, Any]], str]:
    """候选表或配体集 → 按名次排好的 `[{id,name,smiles}]`。

    **行序就是名次**：`write_candidates` 与 `rank_candidates` 写盘时都按分数或共识
    名次排好了，这里再排一次只会与表里那一列对不上。配体集没有名次，按导入顺序。
    """
    path = Path(str(getattr(entry, "path", "") or ""))
    if path.is_dir() and (path / CANDIDATES_FILE).is_file():
        payload = products.read_json(path / CANDIDATES_FILE)
        rows = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "smiles": str(item.get("smiles") or ""),
            }
            for item in (payload.get("rows") or [])
            if isinstance(item, dict)
        ]
        return [row for row in rows if row["id"] and row["smiles"]], "candidates"
    # 准备后的配体集只有 manifest.json 与 ligands/ 子目录，没有 ligands.csv：
    # 那一份的行在 `records` 里（id / name / smiles / pdbqt）。不认这一路的话，
    # 「筛完直接挑」这条最常见的顺序会在挑选这一步失败，而前面每一步都是好的。
    manifest = path / ligand.MANIFEST_FILE
    if path.is_dir() and manifest.is_file() and not (path / ligand.CSV_FILE).is_file():
        payload = products.read_json(manifest)
        rows = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "smiles": str(item.get("smiles") or ""),
            }
            for item in (payload.get("records") or [])
            if isinstance(item, dict) and str(item.get("status") or "ok") == "ok"
        ]
        return [row for row in rows if row["id"] and row["smiles"]], "prepared"
    return _smiles_of(entry), "ligandset"


def _pose_voxels(entry: Any, wanted: set[str]) -> tuple[dict[str, set[tuple[int, int, int]]], str]:
    """每个分子最佳位姿占的体积格。返回 `(分子 → 格集合, 错误说明)`。

    `dock` 的产物里每个分子只存了一条最佳位姿，所以这里不需要再挑一次。
    """
    from . import docking

    manifest_path = Path(str(entry.path)) / docking.MANIFEST_FILE
    try:
        manifest = products.read_json(manifest_path)
    except (OSError, ValueError) as error:
        return {}, f"{entry.handle} 的 {docking.MANIFEST_FILE} 读不出来（{error}）"
    voxels: dict[str, set[tuple[int, int, int]]] = {}
    for item in manifest.get("results") or []:
        identifier = str(item.get("id") or "")
        if identifier not in wanted:
            continue
        relative = str(item.get("pose_pdb") or "").strip()
        if not relative:
            continue
        points = _pdb_points(Path(str(entry.path)) / relative)
        cells = pose_volume_cells(points)
        if cells:
            voxels[identifier] = cells
    return voxels, ""


def pose_volume_cells(points: Sequence[Sequence[float]]) -> set[tuple[int, int, int]]:
    """一组原子坐标占的 1.0 埃格子。半径 1.8 埃的球并起来。

    `pick_diverse` 的位姿去重与 `alternative_poses` 的摆法聚簇共用这一份：
    两处各写一套的话，同一对位姿在两边会被判成不同簇。
    """
    cells: set[tuple[int, int, int]] = set()
    reach = int(POSE_SPHERE_RADIUS / POSE_VOXEL) + 1
    for point in points:
        base = tuple(int(round(value / POSE_VOXEL)) for value in point[:3])
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for dz in range(-reach, reach + 1):
                    offset = (dx * POSE_VOXEL, dy * POSE_VOXEL, dz * POSE_VOXEL)
                    if math.sqrt(dx * dx + dy * dy + dz * dz) * POSE_VOXEL > POSE_SPHERE_RADIUS:
                        continue
                    cells.add((base[0] + dx, base[1] + dy, base[2] + dz))
    return cells


def pose_jaccard(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def cluster_by_similarity(
    items: Sequence[str],
    similarity: Callable[[str, str], float],
    threshold: float,
) -> dict[str, int]:
    """单链接聚簇：相似度 ≥ threshold 的并成一簇。返回 `项 → 簇号`。

    单链接：A 像 B、B 像 C 就算同一簇。链式相连会把一长串略有重叠的项并成
    一大簇，所以簇号只用来「每簇留一个代表」，不参与别的判断。
    """
    parent = {item: item for item in items}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if similarity(left, right) >= threshold:
                a, b = find(left), find(right)
                if a != b:
                    parent[b] = a
    roots: dict[str, int] = {}
    out: dict[str, int] = {}
    for item in items:
        root = find(item)
        if root not in roots:
            roots[root] = len(roots) + 1
        out[item] = roots[root]
    return out


def _pose_clusters(
    rows: Sequence[dict[str, Any]], voxels: dict[str, set[tuple[int, int, int]]]
) -> dict[str, int]:
    """位姿体积重叠聚簇：Jaccard ≥ 0.5 的并成一簇。返回 `分子 → 簇号`。"""
    ids = [row["id"] for row in rows if row["id"] in voxels]
    return cluster_by_similarity(
        ids,
        lambda left, right: pose_jaccard(voxels[left], voxels[right]),
        POSE_JACCARD,
    )


def _batch_summary(rows: Sequence[dict[str, Any]], voxels: dict[str, set]) -> str:
    """位姿去重那一段：并成几簇、最大一簇几个。"""
    if not voxels:
        return ""
    clusters: dict[int, int] = {}
    for identifier, cluster in _pose_clusters(rows, voxels).items():
        clusters[cluster] = clusters.get(cluster, 0) + 1
    largest = max(clusters.values(), default=0)
    return (
        f"位姿去重：{len(voxels)} 个位姿并成 {len(clusters)} 簇，最大一簇 {largest} 个"
    )


class PickDiverseTool(Tool):
    """从候选表或配体集里挑出一批彼此不像的分子。"""

    spec = ToolSpec(
        name="pick_diverse",
        description=(
            "从一批候选里挑出**彼此不像**的那些，交给实验或对接。产出是一个配体集"
            "（`stage=selected`），列里有骨架、簇号、与已选集合的最大相似度、名次。\n"
            "四步，**都不看分数**：① 位姿去重（给了 `poses` 才做）：按对接位姿的重原子"
            "体积，Jaccard ≥0.5 判同簇，每簇只留名次最高的一个；② MaxMin：从名次最高的"
            "开始，每步取「与已选集合最大相似度最小」的那个；③ 骨架配额：同一骨架最多"
            "`per_scaffold` 个；④ 给了 `previous` 就报与上一轮前 N 个的重合、新增骨架数。\n"
            "选不满 `limit` 时如实报数，不降低门槛凑数。`source` 收候选表（相似度、形状、"
            "共识表）与配体集；`poses` 收 dock 的产物。\n"
            "**为什么需要它**：按分数截断留下的是近邻，一个骨架换几个基团就能占满名额。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "候选表或配体集的 handle（名次取表里的行序）",
                },
                "poses": {
                    "type": "string",
                    "description": "对接产物 handle；给了才按位姿体积去重",
                },
                "limit": {
                    "type": "integer",
                    "description": f"挑几个，默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}",
                },
                "max_similarity": {
                    "type": "number",
                    "description": (
                        f"两两 ECFP4 Tanimoto 的上限，默认 {DEFAULT_MAX_SIMILARITY:g}；"
                        "越小越严"
                    ),
                },
                "per_scaffold": {
                    "type": "integer",
                    "description": f"同一骨架最多几个，默认 {DEFAULT_PER_SCAFFOLD}",
                },
                "previous": {
                    "type": "string",
                    "description": "上一轮的候选表或挑选结果；给了才算重合与新增骨架",
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
                output="内核装配问题：pick_diverse 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("source") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 source。先 list_workspace 看清有哪些候选表或配体集。",
                failed=True,
            )
        try:
            limit = _int_argument(arguments, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
            per_scaffold = _int_argument(
                arguments, "per_scaffold", DEFAULT_PER_SCAFFOLD, 1, MAX_LIMIT
            )
            max_similarity = _number_argument(
                arguments, "max_similarity", DEFAULT_MAX_SIMILARITY, 0.0, 1.0
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            source_entry = ledger.get(raw)
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            rows, source_kind = _ranked_rows(source_entry)
        except (ToolError, ligand.LigandError) as error:
            return ToolResult(output=str(error), failed=True)
        if not rows:
            return ToolResult(
                output=f"{source_entry.handle} 里没有带结构的分子，挑不出东西。", failed=True
            )

        pose_entry = None
        voxels: dict[str, set[tuple[int, int, int]]] = {}
        raw_poses = str(arguments.get("poses") or "").strip()
        if raw_poses:
            try:
                pose_entry = ledger.get(raw_poses)
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            voxels, reason = _pose_voxels(pose_entry, {row["id"] for row in rows})
            if reason:
                return ToolResult(output=reason, failed=True)
            if not voxels:
                return ToolResult(
                    output=(
                        f"{pose_entry.handle} 里没有这些分子的位姿文件（`pose_pdb` 为空或"
                        "文件不在了）。去掉 poses 只做 MaxMin，或先确认对接产物还在。"
                    ),
                    failed=True,
                )

        from rdkit import Chem, DataStructs, rdBase
        from rdkit.Chem.Scaffolds import MurckoScaffold

        from . import sar

        with rdBase.BlockLogs():
            molecules = {}
            for row in rows:
                mol = Chem.MolFromSmiles(row["smiles"])
                if mol is not None:
                    molecules[row["id"]] = mol
            pool = [row for row in rows if row["id"] in molecules]
            if not pool:
                return ToolResult(
                    output=f"{source_entry.handle} 里的 SMILES 一条都解析不了。", failed=True
                )
            fingerprints = {
                row["id"]: fingerprint_for(molecules[row["id"]], "ecfp4") for row in pool
            }
            scaffolds = {
                row["id"]: sar.scaffold_of(molecules[row["id"]], "murcko") or sar.ACYCLIC
                for row in pool
            }

        # ---- ① 位姿去重：每簇留名次最高的一个 ----
        clusters = _pose_clusters(pool, voxels)
        kept_clusters: set[int] = set()
        deduped: list[dict[str, Any]] = []
        for row in pool:
            cluster = clusters.get(row["id"])
            if cluster is not None:
                if cluster in kept_clusters:
                    continue
                kept_clusters.add(cluster)
            deduped.append({**row, "cluster": cluster})

        # ---- ② MaxMin + ③ 骨架配额 ----
        selected: list[dict[str, Any]] = []
        scaffold_counts: dict[str, int] = {}
        remaining = list(deduped)
        while remaining and len(selected) < limit:
            if not selected:
                choice, best_similarity = remaining[0], 0.0
            else:
                best: dict[str, Any] | None = None
                best_similarity = 1.0
                for candidate in remaining:
                    if scaffold_counts.get(scaffolds[candidate["id"]], 0) >= per_scaffold:
                        continue
                    similarity = max(
                        DataStructs.TanimotoSimilarity(
                            fingerprints[candidate["id"]], fingerprints[chosen["id"]]
                        )
                        for chosen in selected
                    )
                    if similarity > max_similarity:
                        continue
                    if best is None or similarity < best_similarity:
                        best, best_similarity = candidate, similarity
                if best is None:
                    break
                choice = best
            selected.append({**choice, "max_similarity": round(float(best_similarity), 3)})
            scaffold_counts[scaffolds[choice["id"]]] = (
                scaffold_counts.get(scaffolds[choice["id"]], 0) + 1
            )
            remaining = [row for row in remaining if row["id"] != choice["id"]]

        # ---- ④ 与上一轮的重合 ----
        previous_ids: list[str] = []
        previous_scaffolds: set[str] = set()
        previous_entry = None
        raw_previous = str(arguments.get("previous") or "").strip()
        if raw_previous:
            try:
                previous_entry = ledger.get(raw_previous)
                previous_rows, _kind = _ranked_rows(previous_entry)
            except (ToolError, ligand.LigandError) as error:
                return ToolResult(output=str(error), failed=True)
            previous_ids = [row["id"] for row in previous_rows][:limit]
            with rdBase.BlockLogs():
                for row in previous_rows:
                    mol = Chem.MolFromSmiles(row["smiles"])
                    if mol is not None:
                        previous_scaffolds.add(
                            sar.scaffold_of(mol, "murcko") or sar.ACYCLIC
                        )
        overlap = len({row["id"] for row in selected} & set(previous_ids))
        new_scaffolds = {
            scaffolds[row["id"]] for row in selected
        } - previous_scaffolds

        if not selected:
            return ToolResult(
                output=(
                    f"一个都没挑出来（{source_entry.handle} 里 {len(pool)} 个分子，"
                    f"max_similarity={max_similarity:g}、每骨架 {per_scaffold} 个）。"
                    "放宽门槛再试；这一批不要直接拿去测。"
                ),
                failed=True,
            )

        params = {
            "source": str(source_entry.handle),
            "poses": str(pose_entry.handle) if pose_entry is not None else "",
            "limit": limit,
            "max_similarity": max_similarity,
            "per_scaffold": per_scaffold,
            "previous": str(previous_entry.handle) if previous_entry is not None else "",
            "metric": "ecfp4",
            "pose_jaccard": POSE_JACCARD,
            "pose_radius": POSE_SPHERE_RADIUS,
        }
        stem = str(arguments.get("name") or "selected")
        try:
            target, reused = products.content_dir(
                selected_dir(),
                stem,
                params,
                (SELECTED_MANIFEST, ligand.CSV_FILE, products.PARAMS_FILE),
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="ligandset", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("stage") or "") == SELECTED_STAGE:
                    cached = list((candidate.meta or {}).get("rows") or [])
                    return ToolResult(
                        output=self._render(
                            handle=str(candidate.handle),
                            rows=cached,
                            total=len(pool),
                            limit=limit,
                            max_similarity=max_similarity,
                            per_scaffold=per_scaffold,
                            dedupe=_batch_summary(pool, voxels),
                            overlap=None if previous_entry is None else overlap,
                            new_scaffolds=None if previous_entry is None else len(new_scaffolds),
                            target=target,
                            reused=True,
                        ),
                        meta={"handle": str(candidate.handle), "count": len(cached),
                              "reused": True},
                    )

        record: list[dict[str, Any]] = []
        for rank, row in enumerate(selected, start=1):
            record.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "smiles": row["smiles"],
                    "scaffold": scaffolds[row["id"]],
                    "cluster": row.get("cluster") or 0,
                    "max_similarity": row["max_similarity"],
                    "rank": rank,
                }
            )
        manifest = {
            "kind": "ligandset",
            "stage": SELECTED_STAGE,
            "tool": "pick_diverse",
            "source": {"handle": str(source_entry.handle), "kind": source_kind},
            "poses": str(pose_entry.handle) if pose_entry is not None else "",
            "params": params,
            "counts": {
                "pool": len(pool),
                "selected": len(record),
                "clusters": len(kept_clusters),
                "scaffolds": len({item["scaffold"] for item in record}),
                "overlap": None if previous_entry is None else overlap,
                "new_scaffolds": None if previous_entry is None else len(new_scaffolds),
            },
            "rows": record,
            "columns": list(SELECTED_COLUMNS),
        }
        try:
            products.write_text_atomic(target / ligand.CSV_FILE, _selected_csv(record))
            products.write_json_atomic(target / SELECTED_MANIFEST, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / products.PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写挑选结果失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="ligandset", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("stage") or "") == SELECTED_STAGE:
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "ligandset",
                path=target,
                summary=f"待测批次 · {len(record)} 个 · {len({item['scaffold'] for item in record})} 个骨架",
                fingerprint=fingerprint,
                parents=[source_entry.handle],
                meta={
                    "kind": "ligandset",
                    "stage": SELECTED_STAGE,
                    "tool": "pick_diverse",
                    "source": {"handle": str(source_entry.handle), "kind": source_kind},
                    "params": params,
                    "counts": dict(manifest["counts"]),
                    "rows": record,
                    "file": ligand.CSV_FILE,
                    "manifest": SELECTED_MANIFEST,
                },
                link_current_receptor=False,
            )
            handle = str(registered.handle)

        return ToolResult(
            output=self._render(
                handle=handle,
                rows=record,
                total=len(pool),
                limit=limit,
                max_similarity=max_similarity,
                per_scaffold=per_scaffold,
                dedupe=_batch_summary(pool, voxels),
                overlap=None if previous_entry is None else overlap,
                new_scaffolds=None if previous_entry is None else len(new_scaffolds),
                target=target,
                reused=False,
            ),
            meta={
                "handle": handle,
                "count": len(record),
                "pool": len(pool),
                "clusters": len(kept_clusters),
                "scaffolds": manifest["counts"]["scaffolds"],
                "overlap": manifest["counts"]["overlap"],
                "new_scaffolds": manifest["counts"]["new_scaffolds"],
                "reused": False,
            },
        )

    def _render(
        self,
        *,
        handle: str,
        rows: Sequence[dict[str, Any]],
        total: int,
        limit: int,
        max_similarity: float,
        per_scaffold: int,
        dedupe: str,
        overlap: int | None,
        new_scaffolds: int | None,
        target: Path,
        reused: bool,
    ) -> str:
        lines = [
            f"登记表里已有同一份：{handle}（未重算）。"
            if reused
            else f"挑出 {len(rows)} 个，登记为 {handle}。"
        ]
        lines.append(
            f"候选 {total} 个，门槛：两两相似度 ≤{max_similarity:g}、每骨架 ≤{per_scaffold} 个、"
            f"目标 {limit} 个。"
        )
        if dedupe:
            lines.append(dedupe)
        if len(rows) < limit:
            lines.append(
                f"只挑出 {len(rows)} 个（目标 {limit}）：剩下的都超过了相似度上限，"
                "没有降低门槛凑数。"
            )
        lines.append(f"{'#':>3}  {'分子':<16}{'骨架':<24}{'簇':>3}{'最大相似':>9}")
        for item in rows[:limit]:
            lines.append(
                f"{item['rank']:>3}  {item['id'][:16]:<16}{item['scaffold'][:24]:<24}"
                f"{item.get('cluster') or 0:>3}{item['max_similarity']:>9.2f}"
            )
        if overlap is not None and new_scaffolds is not None:
            ratio = overlap / len(rows) if rows else 0.0
            lines.append(
                f"与上一轮前 {limit} 重合 {overlap} 个（{ratio:.0%}），新增骨架 {new_scaffolds} 组"
            )
            stop = ratio >= STOP_OVERLAP and new_scaffolds <= STOP_NEW_SCAFFOLDS
            if stop:
                lines.append(
                    f"判定：重合 ≥{STOP_OVERLAP:.0%} 且新增骨架 ≤{STOP_NEW_SCAFFOLDS}，"
                    "停止组外探索，转入实测与结案。"
                )
            elif ratio >= STOP_OVERLAP:
                lines.append(
                    "判定：重合高但新增骨架不少——上一轮的挑选没把新化学型选进来，"
                    "该改的是挑选规则，不是再加一轮。"
                )
            else:
                lines.append("判定：重合不高，这一轮还在找到新分子，可以再扩一轮。")
        lines.append(f"产物目录：{target}")
        lines.append(
            f"下一步：拿这 {len(rows)} 个去 dock 或直接做实测建议；"
            f"数据回来用 record_assay(batch={handle}) 记这一批的账。"
        )
        return "\n".join(lines)


def _selected_csv(rows: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SELECTED_COLUMNS)
    for item in rows:
        writer.writerow(
            [
                item["id"],
                item["name"],
                item["smiles"],
                item["smiles"],
                "",
                "",
                item["rank"],
                item["scaffold"],
                item.get("cluster") or 0,
                f"{item['max_similarity']:.4f}",
                item["rank"],
            ]
        )
    return buffer.getvalue()


def domain_tools() -> list[Tool]:
    return [ScreenSimilarityTool(), ScreenShapeTool(), PickDiverseTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
