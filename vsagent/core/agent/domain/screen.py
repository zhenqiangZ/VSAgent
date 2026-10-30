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
from typing import Any, Sequence

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
    "ScreenShapeTool",
    "ScreenSimilarityTool",
    "candidates_dir",
    "domain_tools",
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
    return Path.home() / ".opencode_tui" / "screens"


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


def domain_tools() -> list[Tool]:
    return [ScreenSimilarityTool(), ScreenShapeTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    main()
