"""把多路筛选结果合成一张候选表，并回答「这一条是靠什么接触的」。

**为什么单独一层**：`dock` 只回一条最佳，`score_affinity` 只回一条 pK 表。要看
「哪几个分子在两路证据里都靠前」，模型只能把两个产物目录 `read` 进来自己心算——
那正是纪律 7（要算数就用代码算）要挡的动作，而且两个产物的行序本来就不是一回事。

**分数方向写死在模块里**：`dock` 是 kcal/mol（越低越好），`score_affinity` 是 pK
（越高越好）。方向由来源的 `meta.kind` 决定，**schema 里没有让模型填方向的参数**：
同一份产物两次调用得出相反的排名，排查起来比算错还费劲。

**为什么取平均秩而不是 z-score**：两路来源时标准差没有意义（样本量 2），z-score 会把
两路之间那点分差放大成假信号。秩只依赖顺序，对量纲免疫。

**缺一路的分子不补 0**：0 比任何真实秩都靠前，补 0 等于把「只出现在一路里」的分子
直接推到第一。这里按「已有几路」取平均，再用覆盖路数打破平局——证据多的优先。

**`analyze_pose` / `pick_controls` 是这里声明 `context_argument="ledger"` 的只读工具**
（纪律 2 的例外，另一处是 `assess_structure`）。它们必须沿 handle 找到产物——坐标只在
登记表里，候选表也一样；但**一个字节都不写、不登记任何东西**，所以模型不会因此
以为手上多了一个 handle。理由写在这里，是因为「只读却挂了登记表」看着像违规。
"""

from __future__ import annotations

import csv
import io
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    ArtifactHandle,
    LigandSetHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import affinity, docking, export, pdbtext, products

__all__ = [
    "AnalyzeHydrophobicTool",
    "AnalyzePoseTool",
    "CandidateRow",
    "ContactMapTool",
    "EfficiencyProfileTool",
    "PickControlsTool",
    "RankCandidatesTool",
    "candidates_dir",
    "domain_tools",
    "read_candidates",
]

CANDIDATES_FILE = "candidates.json"
CANDIDATES_CSV = "candidates.csv"
PARAMS_FILE = products.PARAMS_FILE

DEFAULT_TOP = 20
MAX_TOP = 200
DEFAULT_CUTOFFS = (5.0, 20.0)
MIN_TOP = 1

TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_LOW = "low"
TIER_BELOW = "below_cutoff"
TIERS = (TIER_HIGH, TIER_MEDIUM, TIER_LOW, TIER_BELOW)

# artifact 的 meta.kind -> (列名, 是否越高越好)。
# 只认这里列出来的 kind：来源认不出来时不猜方向，直接拒绝——猜错方向得到的是
# 一张完全合理但顺序反了的表，没有任何迹象说明它错了。
SOURCE_KINDS: dict[str, tuple[str, bool]] = {
    "docking": ("dock", False),
    "affinity": ("affinity", True),
}

# 有阈值的门禁项：值超过上限算不通过。布尔门禁为 True 即不通过（如 pains）。
GATE_LIMITS: dict[str, float] = {"redock_rmsd": 2.0}


@dataclass
class CandidateRow:
    """候选表里的一行。`scores` / `ranks` 缺的那一路是 `None`，不是 0。"""

    id: str
    name: str
    smiles: str
    scores: dict[str, float] = field(default_factory=dict)
    ranks: dict[str, float] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    gates: dict[str, Any] = field(default_factory=dict)
    consensus: float = 0.0
    tier: str = TIER_LOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "smiles": self.smiles,
            "scores": {k: v for k, v in self.scores.items()},
            "ranks": {k: v for k, v in self.ranks.items()},
            "consensus_rank": self.consensus,
            "tier": self.tier,
            "gates": self.gates,
            "sources": list(self.sources),
        }


def candidates_dir() -> Path:
    """候选表的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".vsagent" / "candidates"


# ---------------------------------------------------------------------------
# 读来源
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Source:
    handle: str
    kind: str
    label: str
    higher_is_better: bool
    records: tuple[dict[str, Any], ...]
    note: str = ""
    """这一路的细节，用在表头上：对接来源会写 `vina:ad4`。

    **力场必须露出来**：Vina 引擎能跑 vina / vinardo / ad4 三种力场，三者的分数
    互不可比。两份不同力场的产物并排时（今天是拒绝的，将来未必），没有这一行就
    只剩一个 `dock`，看起来像是同一个尺度。
    """


def _read_manifest(entry: Any, filename: str) -> dict[str, Any]:
    path = Path(str(entry.path)) / filename
    try:
        payload = products.read_json(path)
    except OSError as error:
        raise ToolError(f"{entry.handle} 的产物读不出来（{path}）：{error}") from error
    except Exception as error:  # JSONDecodeError
        raise ToolError(f"{entry.handle} 的 {filename} 不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ToolError(f"{entry.handle} 的 {filename} 不是一个对象")
    return payload


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _validation_gates(ledger: SessionLedger, docking_handle: str) -> dict[str, float]:
    """同一份对接产物上的重对接 RMSD，按分子 id 取出来当门禁。"""
    gates: dict[str, float] = {}
    for entry in ledger.find(kind="artifact", include_archived=True):
        meta = dict(entry.meta or {})
        if str(meta.get("kind") or "") != "validation":
            continue
        if str(meta.get("docking") or "") != docking_handle:
            continue
        for row in meta.get("results") or []:
            rmsd = _number(row.get("rmsd"))
            if rmsd is not None:
                gates[str(row.get("id") or "")] = rmsd
    return gates


def _records_of(
    entry: Any, ledger: SessionLedger | None = None
) -> tuple[str, tuple[dict[str, Any], ...], bool, str]:
    """按 `meta.kind` 取这张表里「分子 → 分数」的那一列。

    返回 `(列名, 行, 是否越高越好, 表头补注)`。补注只对对接来源有值——Vina 引擎能跑
    三种力场，三种的分数互不可比，表头不写就会看着像同一个尺度。
    """
    kind = str((entry.meta or {}).get("kind") or "")
    if kind == "candidates":
        # 筛选通道（相似度 / 形状）写出来的也是候选表。列名与方向记在 meta 里，
        # 这里照着读——不去猜「0.87 是相似度还是形状分」。
        column = str((entry.meta or {}).get("score_column") or "")
        if not column:
            raise ToolError(
                f"{entry.handle} 是**合并后**的候选表（没有单一分数列），不能再当来源。"
                "它已经排好序了，直接看它的 rows 即可。"
            )
        higher = bool((entry.meta or {}).get("higher_is_better", True))
        payload = _read_manifest(entry, "candidates.json")
        rows = payload.get("rows") or []
        records = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "smiles": str(item.get("smiles") or ""),
                "score": _number((item.get("scores") or {}).get(column)),
                "gates": dict(item.get("gates") or {}),
            }
            for item in rows
        ]
        kept = tuple(item for item in records if item["score"] is not None)
        if not kept:
            raise ToolError(f"{entry.handle} 里没有一行可用的 {column} 分数。")
        return column, kept, higher, ""

    if kind not in SOURCE_KINDS:
        raise ToolError(
            f"{entry.handle} 是 {kind or '未知类型'}，没法当候选来源。"
            f"能合并的只有：{', '.join(sorted(SOURCE_KINDS))}，以及筛选通道产出的候选表。"
        )
    label, higher = SOURCE_KINDS[kind]
    note = ""

    if kind == "docking":
        manifest = _read_manifest(entry, docking.MANIFEST_FILE)
        results = manifest.get("results") or []
        engine = manifest.get("engine") or {}
        note = f"{engine.get('name') or 'vina'}:{engine.get('scoring') or 'vina'}"
        # 重对接是**协议质量**的证据，不是每个分子的分数。它按 id 挂在 gates 上，
        # 只有那条真的是共晶配体本人才会有值——其余分子这里是空的。
        gates = _validation_gates(ledger, str(entry.handle)) if ledger is not None else {}
        records = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "smiles": str(item.get("smiles") or ""),
                "score": _number(item.get("score")),
                "gates": (
                    {"redock_rmsd": gates[str(item.get("id") or "")]}
                    if str(item.get("id") or "") in gates
                    else {}
                ),
            }
            for item in results
        ]
    else:
        manifest = _read_manifest(entry, affinity.MANIFEST_FILE)
        results = manifest.get("results") or []
        records = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("id") or ""),
                "smiles": str(item.get("smiles") or ""),
                "score": _number(item.get("pk")),
                "gates": {},
            }
            for item in results
        ]

    # 分数不是有限数的行跳过：留着它只会得到一个「秩未知」的坑位。
    kept = tuple(item for item in records if item["score"] is not None)
    if not kept:
        raise ToolError(f"{entry.handle} 里没有一行可用的 {label} 分数。")
    return label, kept, higher, note if kind == "docking" else ""


# ---------------------------------------------------------------------------
# 对齐
# ---------------------------------------------------------------------------
def _molecule_key(smiles: str) -> str:
    """结构相同的两条算同一个分子。装不了 rdkit 时退回原始 SMILES 串。

    退回不是等价物（写法不同就认不出来），但比「一律对不上」强，而且只影响
    缺 id 的那部分行——正常情况下 id 是第一对齐键。
    """
    text = str(smiles).strip()
    if not text:
        return ""
    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(text)
        if mol is not None:
            inchikey = Chem.MolToInchiKey(mol)
            if inchikey:
                return f"inchikey:{inchikey}"
    except Exception:
        pass
    return f"smiles:{text}"


def _merge(sources: Sequence[_Source]) -> tuple[list[CandidateRow], int]:
    """按「先 id、后结构键」把各路对齐成行；返回 (行, 对不上的行数)。"""
    rows: list[CandidateRow] = []
    by_id: dict[str, CandidateRow] = {}
    by_key: dict[str, CandidateRow] = {}
    unmatched = 0

    for index, source in enumerate(sources):
        for record in source.records:
            identity = record["id"]
            key = _molecule_key(record["smiles"])
            row = by_id.get(identity) if identity else None
            if row is None and key:
                row = by_key.get(key)
            if row is None:
                row = CandidateRow(id=identity, name=record["name"], smiles=record["smiles"])
                rows.append(row)
                if index > 0:
                    unmatched += 1
            if identity:
                by_id.setdefault(identity, row)
            if key:
                by_key.setdefault(key, row)
            # 同一路里同一个分子出现两次时留第一个：后出现的那条多半是重复行。
            if source.label in row.scores:
                continue
            row.scores[source.label] = float(record["score"])
            row.sources.append(source.handle)
            if record["gates"]:
                row.gates.update(record["gates"])
            if not row.name:
                row.name = record["name"]
            if not row.smiles:
                row.smiles = record["smiles"]
    return rows, unmatched


def _average_ranks(values: list[tuple[str, float]], *, higher_is_better: bool) -> dict[str, float]:
    """按分数排秩，并列取平均秩。键是行的下标（同一路里 id 可能重复）。"""
    ordered = sorted(values, key=lambda item: (-item[1] if higher_is_better else item[1]))
    ranks: dict[str, float] = {}
    position = 0
    while position < len(ordered):
        end = position
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[position][1]:
            end += 1
        average = (position + 1 + end + 1) / 2
        for cursor in range(position, end + 1):
            ranks[ordered[cursor][0]] = average
        position = end + 1
    return ranks


def _gate_failures(gates: dict[str, Any]) -> list[str]:
    """门禁项里没过的那些。数值门禁比上限，布尔门禁为真即不通过。"""
    failed: list[str] = []
    for name, value in gates.items():
        if isinstance(value, bool):
            if value:
                failed.append(name)
            continue
        number = _number(value)
        limit = GATE_LIMITS.get(name)
        if number is not None and limit is not None and number > limit:
            failed.append(f"{name}={number:g}>{limit:g}")
    return failed


def _assign_tiers(rows: list[CandidateRow], cutoffs: Sequence[float]) -> dict[str, int]:
    """按名次分档：前 `cutoffs[0]`% 是 high，前 `cutoffs[1]`% 是 medium，其余 low。

    用**名次**而不是分数或百分位值：`rank / n * 100` 在小表上会把 top 5% 算成
    「0.25 个位置」，于是一个 20 条的表里没有任何一条能进 high。名次至少给高档留
    一个位置（`max(1, ...)`），20 条的表就是 1 个 high、3 个 medium。
    """
    counts = {tier: 0 for tier in TIERS}
    total = len(rows)
    if not total:
        return counts
    high_slots = max(1, math.ceil(total * cutoffs[0] / 100))
    medium_slots = max(high_slots, math.ceil(total * cutoffs[1] / 100))
    for position, row in enumerate(rows, start=1):
        failures = _gate_failures(row.gates)
        if failures:
            row.tier = TIER_BELOW
        elif position <= high_slots:
            row.tier = TIER_HIGH
        elif position <= medium_slots:
            row.tier = TIER_MEDIUM
        else:
            row.tier = TIER_LOW
        counts[row.tier] += 1
    return counts


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def _number_argument(
    arguments: dict[str, Any], name: str, default: float, low: float, high: float
) -> float:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"参数 {name} 应当是数")
    number = float(value)
    if not math.isfinite(number):
        raise ToolError(f"参数 {name} 不是一个有限的数")
    if not low <= number <= high:
        raise ToolError(f"参数 {name} 要在 {low:g} 到 {high:g} 之间（现在是 {number:g}）")
    return number


def _int_argument(arguments: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {name} 应当是整数")
    if not low <= value <= high:
        raise ToolError(f"参数 {name} 要在 {low} 到 {high} 之间（现在是 {value}）")
    return value


def _cutoffs(arguments: dict[str, Any]) -> tuple[float, float]:
    raw = str(arguments.get("cutoffs") or "").strip()
    if not raw:
        return DEFAULT_CUTOFFS
    parts = [piece.strip() for piece in raw.replace("，", ",").split(",") if piece.strip()]
    if len(parts) != 2:
        raise ToolError("参数 cutoffs 要写成两个百分位，例如 5,20（第一个是 high 的上界）")
    try:
        high, medium = float(parts[0]), float(parts[1])
    except ValueError as error:
        raise ToolError(f"参数 cutoffs 里不是数：{raw}") from error
    if not (0 < high < medium <= 100):
        raise ToolError(
            f"cutoffs 要满足 0 < high < medium <= 100（现在是 {high:g},{medium:g}）"
        )
    return high, medium


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def _csv_text(rows: Sequence[CandidateRow], labels: Sequence[str]) -> str:
    buffer = io.StringIO()
    header = ["id", "name", "smiles", "consensus_rank", "tier"]
    for label in labels:
        header.extend([label, f"{label}_rank"])
    header.append("sources")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        line: list[Any] = [row.id, row.name, row.smiles, f"{row.consensus:g}", row.tier]
        for label in labels:
            line.append("" if label not in row.scores else f"{row.scores[label]:g}")
            line.append("" if label not in row.ranks else f"{row.ranks[label]:g}")
        line.append(",".join(row.sources))
        writer.writerow(line)
    return buffer.getvalue()


def _render(
    *,
    handle: str,
    reused: bool,
    rows: Sequence[CandidateRow],
    labels: Sequence[str],
    counts: dict[str, int],
    top: int,
    unmatched: int,
    sources: Sequence[_Source],
    target: Path,
) -> str:
    total = len(rows)
    distribution = "/".join(str(counts[tier]) for tier in TIERS)
    if reused:
        lines = [f"登记表里已有同一份候选表：{handle}（未重算）。"]
    else:
        lines = [f"候选表已生成，登记为 {handle}。"]
    lines.append(
        f"共 {total} 条，四档 {'/'.join(TIERS)} = {distribution}。"
    )
    lines.append(
        "合并来源："
        + "、".join(
            f"{s.handle}（{s.label}" + (f"·{s.note}" if s.note else "") + "）"
            for s in sources
        )
        + "；方向："
        + "、".join(
            f"{s.label} {'越高' if s.higher_is_better else '越低'}越好" for s in sources
        )
        + "。"
    )
    if len(sources) == 1:
        lines.append("只有一路来源：这是单路分位数，不是共识。")
    else:
        lines.append(
            "秩：每路先转秩再取平均；缺一路的不补 0，按已有几路取平均，"
            "平局时覆盖路数多的在前。"
        )

    header = f"{'#':>3}  {'分子':<14}{'名称':<14}"
    for label in labels:
        header += f"{label:>10}{'秩':>6}"
    header += f"{'共识':>7}  档位"
    lines.append(header)
    for index, row in enumerate(rows[:top], start=1):
        line = f"{index:>3}  {row.id[:14]:<14}{row.name[:14]:<14}"
        for label in labels:
            score = row.scores.get(label)
            rank = row.ranks.get(label)
            line += f"{'—' if score is None else f'{score:.2f}':>10}"
            line += f"{'—' if rank is None else f'{rank:g}':>6}"
        line += f"{row.consensus:>7.1f}  {row.tier}"
        failures = _gate_failures(row.gates)
        if failures:
            line += f"（门禁未过：{', '.join(failures)}）"
        lines.append(line)
    if total > top:
        lines.append(f"只列前 {top} 条，其余 {total - top} 条见 {CANDIDATES_CSV}。")

    if unmatched:
        lines.append(
            f"对齐：{unmatched} 行在后续来源里没对上（缺 id 或结构不同），按独立行保留。"
        )
    lines.append(f"产物目录：{target}")
    if rows:
        best = rows[0]
        source_handle = sources[0].handle
        lines.append(
            f"下一步：analyze_pose(docking={source_handle}, molecule={best.id}) "
            "看这一条是靠哪些残基接触的。"
            if sources[0].kind == "docking"
            else "下一步：用 write_run 把结果落进运行目录，或再并入一路来源重排。"
        )
    return "\n".join(lines)


def read_candidates(path: Path) -> dict[str, Any]:
    """读回一份候选表（`analyze_pose` 与后续工具用它找分子）。"""
    payload = products.read_json(Path(path) / CANDIDATES_FILE)
    if not isinstance(payload, dict):
        raise ToolError(f"{path} 的 {CANDIDATES_FILE} 不是一个对象")
    return payload


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class RankCandidatesTool(Tool):
    """把多路分数合成一张按共识排名的候选表。"""

    spec = ToolSpec(
        name="rank_candidates",
        description=(
            "把**多路**筛选分数合成一张候选表：先对齐分子，再按平均秩排共识名次，"
            "最后按名次分档（前 5% 是 high、前 20% 是 medium，其余 low；"
            "没过门禁的单列 below_cutoff）。\n"
            "来源写 handle，逗号分隔（如 artifact#4,artifact#5）。分数方向由来源类型"
            "决定——dock 的 kcal/mol 越低越好、affinity 的 pK 越高越好——**不要自己"
            "换算方向，也没有这个参数**。\n"
            "只合并、不重算：表里出现的分数与各路产物里逐字一致。某一路缺这个分子时"
            "该列留空，不补 0。\n"
            "下一步：analyze_pose(docking=..., molecule=...) 看榜首那条的接触依据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "sources": {
                    "type": "string",
                    "description": "产物 handle，逗号分隔，例如 artifact#4,artifact#5",
                },
                "cutoffs": {
                    "type": "string",
                    "description": (
                        f"两个百分位，默认 {DEFAULT_CUTOFFS[0]:g},{DEFAULT_CUTOFFS[1]:g}；"
                        "第一个是 high 的上界，第二个是 medium 的上界"
                    ),
                },
                "top": {
                    "type": "integer",
                    "description": f"返回里列几行，默认 {DEFAULT_TOP}，上限 {MAX_TOP}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["sources"],
            "additionalProperties": False,
        },
    )
    primary_argument = "sources"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：rank_candidates 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw = str(arguments.get("sources") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 sources。先 list_workspace 看清有哪些打分产物。",
                failed=True,
            )
        tokens = [piece.strip() for piece in raw.replace("，", ",").split(",") if piece.strip()]
        if not tokens:
            return ToolResult(output=f"参数 sources 里没有 handle：{raw}", failed=True)
        if len(set(tokens)) != len(tokens):
            return ToolResult(
                output=f"参数 sources 里有重复的 handle：{raw}（同一路算两次会把它的权重翻倍）",
                failed=True,
            )

        entries: list[Any] = []
        for token in tokens:
            try:
                entry = ledger.get(token)
                expect_handle(entry, ArtifactHandle, "artifact")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            entries.append(entry)

        try:
            top = _int_argument(arguments, "top", DEFAULT_TOP, MIN_TOP, MAX_TOP)
            cutoffs = _cutoffs(arguments)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        sources: list[_Source] = []
        for entry in entries:
            kind = str((entry.meta or {}).get("kind") or "")
            try:
                label, records, higher, note = _records_of(entry, ledger)
            except ToolError as error:
                return ToolResult(output=str(error), failed=True)
            if any(source.label == label for source in sources):
                return ToolResult(
                    output=(
                        f"两路来源都是 {label}（{entry.handle}），合起来没有意义："
                        "同一列会出现两个值，秩也会被算两遍。换一路不同类型的产物。"
                    ),
                    failed=True,
                )
            sources.append(
                _Source(
                    handle=str(entry.handle),
                    kind=kind,
                    label=label,
                    higher_is_better=higher,
                    records=records,
                    note=note,
                )
            )

        params = {
            "sources": [
                {"handle": source.handle, "label": source.label, "engine": source.note}
                for source in sources
            ],
            "cutoffs": [cutoffs[0], cutoffs[1]],
            "top": top,
        }
        stem = str(arguments.get("name") or "candidates")
        try:
            target, reused = products.content_dir(
                candidates_dir(), stem, params, (CANDIDATES_FILE, CANDIDATES_CSV, PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            try:
                payload = read_candidates(target)
            except (ToolError, OSError):
                payload = {}
            existing = ledger.find(
                kind="artifact", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("kind") or "") == "candidates":
                    rows = _rows_from_payload(payload)
                    counts = {tier: int((payload.get("counts") or {}).get(tier, 0)) for tier in TIERS}
                    return ToolResult(
                        output=_render(
                            handle=str(candidate.handle),
                            reused=True,
                            rows=rows,
                            labels=[source.label for source in sources],
                            counts=counts,
                            top=top,
                            unmatched=int(payload.get("alignment_warnings") or 0),
                            sources=sources,
                            target=target,
                        ),
                        meta={
                            "handle": str(candidate.handle),
                            "kind": "candidates",
                            "count": len(rows),
                            "counts": counts,
                            "reused": True,
                        },
                    )

        rows, unmatched = _merge(sources)
        if not rows:
            return ToolResult(output="两路来源里没有任何一行能合并成候选表。", failed=True)

        for source in sources:
            values = [(str(index), row.scores[source.label]) for index, row in enumerate(rows)
                      if source.label in row.scores]
            ranks = _average_ranks(values, higher_is_better=source.higher_is_better)
            for index, row in enumerate(rows):
                if str(index) in ranks:
                    row.ranks[source.label] = ranks[str(index)]

        for row in rows:
            available = [row.ranks[label] for label in row.ranks]
            row.consensus = round(sum(available) / len(available), 3)
        rows.sort(key=lambda row: (row.consensus, -len(row.ranks), row.id))
        counts = _assign_tiers(rows, cutoffs)

        payload = {
            "kind": "candidates",
            "sources": [source.handle for source in sources],
            "tier_method": "percentile",
            "cutoffs": [cutoffs[0], cutoffs[1]],
            "counts": counts,
            "alignment_warnings": unmatched,
            "rows": [row.as_dict() for row in rows],
        }
        try:
            products.write_json_atomic(target / CANDIDATES_FILE, payload)
            products.write_text_atomic(
                target / CANDIDATES_CSV,
                _csv_text(rows, [source.label for source in sources]),
            )
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写候选表失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        existing = ledger.find(kind="artifact", fingerprint=fingerprint, include_archived=True)
        handle = ""
        for candidate in existing:
            if str((candidate.meta or {}).get("kind") or "") == "candidates":
                handle = str(candidate.handle)
                break
        if not handle:
            entry = ledger.add(
                "artifact",
                path=target,
                summary=f"候选表 · {len(rows)} 条 · high {counts[TIER_HIGH]}",
                fingerprint=fingerprint,
                parents=[entry.handle for entry in entries],
                meta={
                    "kind": "candidates",
                    "stage": "candidates",
                    "tool": "rank_candidates",
                    "sources": [source.handle for source in sources],
                    "tier_method": "percentile",
                    "counts": counts,
                    "alignment_warnings": unmatched,
                    "params": params,
                    "file": CANDIDATES_FILE,
                    "csv": CANDIDATES_CSV,
                },
            )
            handle = str(entry.handle)

        return ToolResult(
            output=_render(
                handle=handle,
                reused=False,
                rows=rows,
                labels=[source.label for source in sources],
                counts=counts,
                top=top,
                unmatched=unmatched,
                sources=sources,
                target=target,
            ),
            meta={
                "handle": handle,
                "kind": "candidates",
                "count": len(rows),
                "counts": counts,
                "reused": False,
            },
        )


def _rows_from_payload(payload: dict[str, Any]) -> list[CandidateRow]:
    rows: list[CandidateRow] = []
    for item in payload.get("rows") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            CandidateRow(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                smiles=str(item.get("smiles") or ""),
                scores={k: float(v) for k, v in (item.get("scores") or {}).items()},
                ranks={k: float(v) for k, v in (item.get("ranks") or {}).items()},
                sources=[str(v) for v in (item.get("sources") or [])],
                gates=dict(item.get("gates") or {}),
                consensus=float(item.get("consensus_rank") or 0.0),
                tier=str(item.get("tier") or TIER_LOW),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 位姿分析：接触残基、氢键、冲突
# ---------------------------------------------------------------------------
CONTACT_CUTOFF = 4.0
MIN_CONTACT_CUTOFF = 2.5
MAX_CONTACT_CUTOFF = 6.0
HBOND_DISTANCE = 3.5
HBOND_ANGLE = 120.0
CLASH_DISTANCE = 2.0
MAX_CONTACT_ROWS = 30
MAX_HBOND_ROWS = 10
MAX_IDS_SHOWN = 20
MAX_FINGERPRINT_ROWS = 8
POLAR = frozenset({"N", "O", "S"})


@dataclass(frozen=True)
class _Contact:
    label: str
    resname: str
    distance: float
    atoms: int


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.dist(tuple(left), tuple(right))


def _contacts(
    receptor: Sequence[pdbtext.AtomRecord],
    ligand: Sequence[pdbtext.AtomRecord],
    cutoff: float,
) -> list[_Contact]:
    """每个残基取「最短距离 + 进入 cutoff 的原子数」。"""
    buckets: dict[str, list[pdbtext.AtomRecord]] = {}
    for atom in receptor:
        if atom.is_hetero or not pdbtext.is_heavy(atom):
            continue
        buckets.setdefault(atom.label, []).append(atom)

    found: list[_Contact] = []
    for label, atoms in buckets.items():
        best = math.inf
        count = 0
        for atom in atoms:
            nearest = min(_distance(atom.coord, other.coord) for other in ligand)
            if nearest <= cutoff:
                count += 1
                best = min(best, nearest)
        if count:
            found.append(_Contact(label, atoms[0].resname, best, count))
    found.sort(key=lambda item: (item.distance, item.label))
    return found


def _clashes(
    receptor: Sequence[pdbtext.AtomRecord],
    ligand: Sequence[pdbtext.AtomRecord],
) -> list[tuple[str, str, str, float]]:
    """`(残基, 受体原子, 配体原子, 距离)`——两个原子名都要，否则两行看起来一模一样。"""
    pairs: list[tuple[str, str, str, float]] = []
    for atom in receptor:
        if atom.is_hetero or not pdbtext.is_heavy(atom):
            continue
        for other in ligand:
            distance = _distance(atom.coord, other.coord)
            if distance < CLASH_DISTANCE:
                pairs.append((atom.label, atom.name, other.name, distance))
    pairs.sort(key=lambda item: item[3])
    return pairs


def _angle(center: Sequence[float], left: Sequence[float], right: Sequence[float]) -> float:
    """`left-center-right` 的夹角（度）。"""
    first = [a - b for a, b in zip(left, center)]
    second = [a - b for a, b in zip(right, center)]
    norm = math.dist((0, 0, 0), first) * math.dist((0, 0, 0), second)
    if norm == 0:
        return 0.0
    cosine = sum(a * b for a, b in zip(first, second)) / norm
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _donor_reference(
    donor: pdbtext.AtomRecord,
    atoms: Sequence[pdbtext.AtomRecord],
) -> tuple[pdbtext.AtomRecord, bool] | None:
    """给供体找「量角度用的第二个原子」：优先共价氢，其次最近的重原子。

    结构里通常没有氢（Vina 的输入也常常只留极性氢），所以必须有个退路，否则
    「角度 ≥ 120°」这条判据在不带氢的结构上根本算不出来。退路是 `D'–D···A`：
    氢在 D' 的反方向，真实角度与这个近似角只差几度。返回第二项标明用的是不是氢，
    输出里据此写「近似」。
    """
    hydrogen: tuple[float, pdbtext.AtomRecord] | None = None
    heavy: tuple[float, pdbtext.AtomRecord] | None = None
    for other in atoms:
        if other is donor:
            continue
        distance = _distance(donor.coord, other.coord)
        if other.element == "H" and distance <= 1.3:
            if hydrogen is None or distance < hydrogen[0]:
                hydrogen = (distance, other)
        elif other.element != "H" and distance <= 2.2:
            if heavy is None or distance < heavy[0]:
                heavy = (distance, other)
    if hydrogen is not None:
        return hydrogen[1], True
    if heavy is not None:
        return heavy[1], False
    return None


@dataclass(frozen=True)
class _HBond:
    donor_label: str
    donor_atom: str
    acceptor_label: str
    acceptor_atom: str
    distance: float
    angle: float
    approximate: bool


def _hbonds(
    receptor: Sequence[pdbtext.AtomRecord],
    ligand: Sequence[pdbtext.AtomRecord],
) -> list[_HBond]:
    """极性原子对：距离 ≤ 3.5 Å 且 D'–D···A ≥ 120°。两侧都试，取角度大的那个方向。"""
    receptor_polar = [
        atom for atom in receptor
        if not atom.is_hetero and atom.element in POLAR and pdbtext.is_heavy(atom)
    ]
    ligand_polar = [
        atom for atom in ligand if atom.element in POLAR and pdbtext.is_heavy(atom)
    ]

    found: list[_HBond] = []
    for left in receptor_polar:
        for right in ligand_polar:
            distance = _distance(left.coord, right.coord)
            if distance > HBOND_DISTANCE:
                continue
            best: _HBond | None = None
            for donor, acceptor, donor_side, acceptor_side, flipped in (
                (left, right, receptor, ligand, False),
                (right, left, ligand, receptor, True),
            ):
                reference = _donor_reference(donor, donor_side)
                if reference is None:
                    continue
                anchor, is_hydrogen = reference
                angle = _angle(donor.coord, anchor.coord, acceptor.coord)
                if angle < HBOND_ANGLE:
                    continue
                if best is not None and angle <= best.angle:
                    continue
                if flipped:
                    best = _HBond(
                        donor_label="配体",
                        donor_atom=donor.name,
                        acceptor_label=acceptor.label,
                        acceptor_atom=acceptor.name,
                        distance=distance,
                        angle=angle,
                        approximate=not is_hydrogen,
                    )
                else:
                    best = _HBond(
                        donor_label=donor.label,
                        donor_atom=donor.name,
                        acceptor_label="配体",
                        acceptor_atom=acceptor.name,
                        distance=distance,
                        angle=angle,
                        approximate=not is_hydrogen,
                    )
            if best is not None:
                found.append(best)
    found.sort(key=lambda item: item.distance)
    return found


# ---------------------------------------------------------------------------
# PLIP 交互指纹：上面那组只能看见「离得近」和「极性对得上」，看不见盐桥、
# π 堆积、疏水、卤键、水桥、金属配位——而这些恰恰是「为什么这个分子强」的答案。
# ---------------------------------------------------------------------------
PLIP_INSTALL_HINT = "pip install -r requirements-chem.txt"
"""PLIP 的安装入口与化学栈共用一份文件：它也是化学依赖，只是**不装也能跑别的**。

刻意不为它单开一份 requirements：多一个文件就多一处会漂的地方，而用户的动作是
一样的（装一次、重启会话）。
"""

# PLIP 的 interaction set 用 (配体残基名, 链, 序号) 做键。位姿里那个残基名是 Vina
# 写的 `UNL`，而受体里**可能恰好也有同名残基**（保留下来的共晶配体最常见）——那样
# PLIP 会把两个都当配体，我们挑错一个就会拿晶体里的相互作用去解释对接位姿，而输出
# 看上去完全正常。所以合并前把位姿的残基名换成本文件里没出现过的名字。
PLIP_RESNAMES = ("LIG", "UNL", "LG1", "Q01", "ZZZ")

_PLIP_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("saltbridge", "盐桥", ("saltbridge_lneg", "saltbridge_pneg")),
    ("hbond", "氢键", ("hbonds_ldon", "hbonds_pdon")),
    ("pistacking", "π 堆积", ("pistacking",)),
    ("pication", "π-阳离子", ("pication_laro", "pication_paro")),
    ("hydrophobic", "疏水", ("hydrophobic_contacts",)),
    ("halogen", "卤键", ("halogen_bonds",)),
    ("waterbridge", "水桥", ("water_bridges",)),
    ("metal", "金属配位", ("metal_complexes",)),
)
"""`(键, 中文名, PLIP 的属性名)`。

**属性名是照着 `PLInteraction.__init__` 抄的**，不是猜的：`hbonds` 不存在（是
`hbonds_ldon` / `hbonds_pdon`），`saltbridge` 也不存在（是 `saltbridge_lneg` /
`saltbridge_pneg`）。用 `getattr(site, "hbonds", ())` 那种写法不会报错，只会永远
返回空——指纹安静地少一整类，而输出看起来毫无异样。
"""


@dataclass(frozen=True)
class _PlipHit:
    """一个残基参与的一类相互作用。`count` 是同一残基同一方向的条数。"""

    residue: str
    distance: float
    detail: str
    count: int = 1


def _plip_distance(row: Any) -> float:
    """PLIP 各类记录的距离字段不同：氢键是 `distance_ad`（供体-受体），水桥是两个。"""
    for name in ("distance", "distance_ad", "distance_aw", "distance_dw"):
        value = getattr(row, name, None)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _plip_detail(attr: str, row: Any) -> str:
    """方向与几何。**按属性名判断，不按行里的布尔字段**：属性名就决定了方向，
    而 `protisdon` / `protispos` 在少数行里与属性名不一致（PLIP 用它们标的是
    「这一条是从哪边算过来的」，不是「谁带电」）。"""
    if attr == "saltbridge_lneg":
        return "蛋白正电"
    if attr == "saltbridge_pneg":
        return "配体正电"
    if attr == "hbonds_ldon":
        return "配体供体"
    if attr == "hbonds_pdon":
        return "蛋白供体"
    if attr == "pication_laro":
        return "配体环/蛋白正电"
    if attr == "pication_paro":
        return "蛋白环/配体正电"
    if attr == "pistacking":
        return {"P": "平行", "T": "T 型"}.get(str(getattr(row, "type", "")).upper(), "")
    if attr == "water_bridges":
        return "蛋白供体" if getattr(row, "protisdon", False) else "配体供体"
    if attr == "metal_complexes":
        return str(getattr(row, "metal_type", "") or "")
    return ""


def _plip_hits(site: Any) -> dict[str, list[_PlipHit]]:
    """把 PLIP 的交互集合压成「残基 → 类型」的指纹。

    同一残基同一方向的重复记录合并成一条（留最近的那个距离，`count` 记条数）：
    指纹要回答的是「这个位姿靠哪几个残基撑住」，同一个 Asp 的两条盐桥在残基层面
    是同一条证据。
    """
    found: dict[str, list[_PlipHit]] = {}
    for kind, _label, attrs in _PLIP_GROUPS:
        buckets: dict[tuple[str, str], _PlipHit] = {}
        for attr in attrs:
            for row in getattr(site, attr, ()) or ():
                residue = (
                    f"{getattr(row, 'reschain', '')}:"
                    f"{getattr(row, 'resnr', '')}:"
                    f"{getattr(row, 'restype', '')}"
                )
                detail = _plip_detail(attr, row)
                distance = _plip_distance(row)
                key = (residue, detail)
                previous = buckets.get(key)
                if previous is None:
                    buckets[key] = _PlipHit(residue=residue, distance=distance, detail=detail)
                else:
                    buckets[key] = _PlipHit(
                        residue=residue,
                        distance=min(previous.distance, distance),
                        detail=detail,
                        count=previous.count + 1,
                    )
        if buckets:
            found[kind] = sorted(buckets.values(), key=lambda hit: hit.distance)
    return found


def _plip_missing_message(missing: str) -> str:
    """缺依赖时的那句话。`missing` 是**真正缺席**的导入名（`exc.name`），不是 `plip`。

    「装了 plip 但缺 openbabel」是实际会遇到的形态（openbabel 是它的依赖，装 plip
    时会一起装，但环境被人动过就会只剩一个）。这时候说「本机没装 PLIP」是错的，
    用户会去重装已经装好的东西——与 `chemistry._probe_deps` 报 `exc.name` 同一个理由。
    """
    if (missing or "plip").split(".")[0] == "plip":
        return f"本机没装 PLIP。装它：{PLIP_INSTALL_HINT}，装完重启会话。其余部分不受影响。"
    return (
        f"PLIP 用不了：它的依赖 {missing} 没装上（plip 本身在）。"
        f"补齐：{PLIP_INSTALL_HINT}，装完重启会话。其余部分不受影响。"
    )


def _plip_loader() -> tuple[Any, str]:
    """拿到 `PDBComplex`，或一句「为什么拿不到」。**导入必须在函数体里**：
    openbabel 装完是 35 MB，顶层 import 会让没装它的人连会话都开不起来
    （`check_chemistry_gate` 的 AST 断言盯着这一条）。"""
    try:
        from plip.structure.preparation import PDBComplex  # noqa: PLC0415
    except ModuleNotFoundError as error:
        return None, _plip_missing_message(error.name or "plip")
    except Exception as error:  # noqa: BLE001 —— 装上了但导入就炸，原因必须带上
        return None, f"PLIP 装上了但导入失败（{type(error).__name__}: {error}）。"
    return PDBComplex, ""


def _pdb_records(text: str, *, resname: str = "") -> list[str]:
    """读 PDB 的原子行，丢掉结束标记与 CONECT，必要时换掉残基名。

    **`END` / `MASTER` 必须丢**：受体文件末尾就有 `END`，直接拼上配体会让
    openbabel 在那里停下——配体一个原子都没读进去，PLIP 于是报「没有配体」。
    这个坑很安静：文件看起来是完整的，错在中间那一行。

    **`CONECT` 也丢**：两份文件的原子序号各自从 1 开始，拼在一起后受体的
    CONECT 会指到配体原子上（反之亦然），造出根本不存在的键。丢掉之后由
    openbabel 按距离认键，实测苯环照样被认成芳香环、羧基照样被认成负电中心。
    """
    kept: list[str] = []
    for line in text.splitlines():
        record = line[:6].strip()
        if record in {"END", "ENDMDL", "MASTER", "CONECT", "ANISOU"}:
            continue
        if resname and record == "HETATM":
            line = line[:17] + f"{resname:<3}" + line[20:]
        kept.append(line)
    return kept


def _plip_complex_text(receptor_text: str, pose_text: str) -> tuple[str, str]:
    """拼一份 PLIP 能读的复合物，返回 `(文本, 位姿用的残基名)`。"""
    used = {
        line[17:20].strip().upper()
        for line in receptor_text.splitlines()
        if line[:6].strip() in {"ATOM", "HETATM"}
    }
    resname = next((name for name in PLIP_RESNAMES if name not in used), PLIP_RESNAMES[0])
    lines = _pdb_records(receptor_text)
    lines.extend(_pdb_records(pose_text, resname=resname))
    lines.append("END")
    return "\n".join(lines) + "\n", resname


def _plip_missing_attrs(site: Any) -> list[str]:
    """`site` 上缺哪些预期的属性。

    这一条防的是**版本漂移**：`getattr(site, "hbonds", ())` 不会报错，只会永远返回
    空——PLIP 哪天改了字段名，指纹就安静地少一整类，而输出看起来毫无异样。所以
    先核对字段在不在，不在就明说「没算」，不是「没有」。
    """
    expected = [attr for _kind, _label, attrs in _PLIP_GROUPS for attr in attrs]
    return [attr for attr in expected if not hasattr(site, attr)]


def plip_fingerprint(
    receptor_text: str, pose_text: str
) -> tuple[dict[str, list[_PlipHit]], str]:
    """跑 PLIP 拿交互指纹，返回 `(指纹, 没算成的原因)`。两者必有一个是空的。

    **失败不抛异常**：调用方已经把接触、氢键、冲突算完了，那些结果与 PLIP 无关，
    不能因为一个可选依赖缺席就整条作废。原因照实回给模型，它自己决定要不要转述。
    """
    loader, reason = _plip_loader()
    if loader is None:
        return {}, reason

    text, resname = _plip_complex_text(receptor_text, pose_text)
    # **整段都在 try 里**，包括 `loader()` 与写临时文件：openbabel 的初始化、临时目录
    # 不可写、PLIP 内部断言，任何一种漏出去都会让整次 analyze_pose 失败——而它已经
    # 把接触与氢键算完了，那些结果与 PLIP 无关。
    try:
        with tempfile.TemporaryDirectory(prefix="vsa-plip-") as tmp:
            path = Path(tmp) / "complex.pdb"
            path.write_text(text, encoding="utf-8")
            complex_ = loader()
            complex_.load_pdb(str(path))
            complex_.analyze()
            sets = dict(complex_.interaction_sets)
    except Exception as error:  # noqa: BLE001 —— PLIP 内部异常种类很多，一律降级
        return {}, f"PLIP 算不了这份复合物（{type(error).__name__}: {error}）。"

    site = next(
        (
            item
            for item in sets.values()
            if str(getattr(item.ligand, "hetid", "")).upper() == resname
        ),
        None,
    )
    if site is None:
        return {}, "PLIP 没把位姿认成配体（合并后的复合物里找不到它），交互指纹算不了。"

    missing_attrs = _plip_missing_attrs(site)
    if missing_attrs:
        return {}, (
            "PLIP 的接口与这个版本对不上（缺字段 "
            + "、".join(missing_attrs[:3])
            + "），交互指纹没算——不是这个位姿没有相互作用。"
        )
    return _plip_hits(site), ""


def _render_fingerprint(hits: dict[str, list[_PlipHit]]) -> list[str]:
    """指纹那一段。按 `_PLIP_GROUPS` 的顺序，一类一行。"""
    lines: list[str] = []
    for kind, label, _attrs in _PLIP_GROUPS:
        items = hits.get(kind) or []
        if not items:
            continue
        parts: list[str] = []
        for hit in items[:MAX_FINGERPRINT_ROWS]:
            piece = f"{hit.residue} {hit.distance:.2f} Å"
            if hit.count > 1:
                piece += f"（×{hit.count}）"
            if hit.detail:
                piece += f" {hit.detail}"
            parts.append(piece)
        line = f"  {label} {len(items)} 个残基：" + " · ".join(parts)
        if len(items) > MAX_FINGERPRINT_ROWS:
            line += f" ……另有 {len(items) - MAX_FINGERPRINT_ROWS} 个未显示"
        lines.append(line)
    if not lines:
        lines.append("  一类都没找到（没有盐桥、π 堆积、疏水接触、卤键、水桥或金属配位）")
    return lines


def _render_pose(
    *,
    molecule: str,
    name: str,
    pose: Path,
    contacts: Sequence[_Contact],
    hbonds: Sequence[_HBond],
    clashes: Sequence[tuple[str, str, str, float]],
    cutoff: float,
    fingerprint: dict[str, list[_PlipHit]] | None = None,
    fingerprint_note: str = "",
) -> str:
    summary = f"{len(contacts)} 个残基接触"
    if contacts:
        summary += f"（最近 {contacts[0].distance:.2f} Å）"
    summary += f"，{len(hbonds)} 个氢键"
    summary += "，无冲突。" if not clashes else f"，**{len(clashes)} 处冲突**。"
    if fingerprint:
        totals = " / ".join(
            f"{len(fingerprint[kind])} {label}"
            for kind, label, _attrs in _PLIP_GROUPS
            if fingerprint.get(kind)
        )
        summary += f" PLIP：{totals}。"
    elif fingerprint_note:
        # 没算成也必须在第一行说一句：下面没有「盐桥」那几行，读起来像「没有盐桥」，
        # 而真实原因是没算——这两种结论对决策的意义完全相反。
        summary += " PLIP 没算成（原因见末尾）。"
    lines = [f"结论：{summary}"]
    lines.append(f"分子：{molecule}" + (f"（{name}）" if name and name != molecule else ""))
    lines.append(f"位姿：{pose}")
    lines.append(
        f"口径：接触 ≤ {cutoff:g} Å（重原子-重原子）；"
        f"氢键 供体-受体 ≤ {HBOND_DISTANCE:g} Å 且角度 ≥ {HBOND_ANGLE:g}°；"
        f"冲突 < {CLASH_DISTANCE:g} Å。"
    )

    lines.append("接触残基（按最近距离）：")
    for item in contacts[:MAX_CONTACT_ROWS]:
        lines.append(f"  {item.label:<16}{item.distance:6.2f} Å   {item.atoms} 个原子")
    if len(contacts) > MAX_CONTACT_ROWS:
        lines.append(f"  ……另有 {len(contacts) - MAX_CONTACT_ROWS} 个残基未显示")

    if hbonds:
        lines.append("氢键：")
        for item in hbonds[:MAX_HBOND_ROWS]:
            note = "（角度按重原子近似）" if item.approximate else ""
            lines.append(
                f"  {item.donor_label} {item.donor_atom} → "
                f"{item.acceptor_label} {item.acceptor_atom}   "
                f"{item.distance:.2f} Å  {item.angle:.0f}°{note}"
            )
        if len(hbonds) > MAX_HBOND_ROWS:
            lines.append(f"  ……另有 {len(hbonds) - MAX_HBOND_ROWS} 个未显示")
    else:
        lines.append("氢键：无（极性原子对里没有同时满足距离与角度的）")

    if clashes:
        lines.append("冲突：")
        for label, receptor_atom, ligand_atom, distance in clashes[:MAX_HBOND_ROWS]:
            lines.append(f"  {label} {receptor_atom} ↔ 配体 {ligand_atom}   {distance:.2f} Å")

    if fingerprint_note:
        lines.append(f"PLIP 交互指纹：{fingerprint_note}")
    elif fingerprint is not None:
        lines.append(
            "PLIP 交互指纹（openbabel 认键级与质子化，阈值取 PLIP 默认；"
            "与上面那组本地几何是两套口径，数量对不上属正常）："
        )
        lines.extend(_render_fingerprint(fingerprint))
    return "\n".join(lines)


@dataclass(frozen=True)
class _Pose:
    """一个位姿，连同它所在的那份对接产物与受体坐标。

    `analyze_pose` 与 `analyze_hydrophobic` 都要「沿血缘找到对接时用的那份受体」，
    这段逻辑（handle → 产物 → 位姿路径 → 受体坐标 → 解析）一步都不能少，复制一遍
    必然有一份会漏掉某个校验。
    """

    molecule: str
    name: str
    pose: Path
    record: dict[str, Any]
    entry: Any
    receptor: Any
    receptor_text: str
    pose_text: str
    receptor_atoms: tuple[pdbtext.AtomRecord, ...]
    ligand_atoms: tuple[pdbtext.AtomRecord, ...]
    engine: str


@dataclass(frozen=True)
class _Receptor:
    """对接时用的那份受体：登记条目、原始文本、解析出来的原子。"""

    entry: Any
    text: str
    atoms: tuple[pdbtext.AtomRecord, ...]


def _docking_entry(
    ledger: SessionLedger, arguments: dict[str, Any], *, tool_name: str
) -> tuple[Any | None, ToolResult | None]:
    """取 `docking` 参数并确认它是 dock 的产物。

    `analyze_pose` / `analyze_hydrophobic` / `contact_map` 都要过这一关，
    分开写三份的话，总有一份会漏掉「kind 不是 docking」这条。
    """
    raw = str(arguments.get("docking") or "").strip()
    if not raw:
        return None, ToolResult(
            output="缺少参数 docking。先 list_workspace 看清有哪份对接产物。", failed=True
        )
    try:
        entry = ledger.get(raw)
        expect_handle(entry, ArtifactHandle, "docking")
    except Exception as error:
        return None, ToolResult(output=str(error), failed=True)

    kind = str((entry.meta or {}).get("kind") or "")
    if kind != "docking":
        return None, ToolResult(
            output=(
                f"{entry.handle} 是 {kind or '未知类型'}，不是对接产物。"
                f"{tool_name} 只认 dock 的产物。"
            ),
            failed=True,
        )
    return entry, None


def _receptor_of(
    ledger: SessionLedger, entry: Any, manifest: dict[str, Any]
) -> tuple[_Receptor | None, ToolResult | None]:
    """沿对接产物记的受体 handle 找到坐标并解析。

    坐标只在登记表里，产物里存的是 handle——中间少一步就是「接触算不了」，
    所以这段必须是唯一的一份。
    """
    receptor_handle = str((manifest.get("receptor") or {}).get("handle") or "")
    if not receptor_handle:
        return None, ToolResult(
            output=f"{entry.handle} 的产物里没有记受体 handle，取不到坐标。", failed=True
        )
    try:
        receptor_entry = ledger.get(receptor_handle)
    except Exception as error:
        return None, ToolResult(
            output=f"{error}（对接用的受体已经不在登记表里，接触算不了）", failed=True
        )
    coordinate = export._receptor_coordinate(receptor_entry)
    if coordinate is None:
        return None, ToolResult(
            output=f"{receptor_entry.handle} 找不到坐标文件（准备产物不完整？）", failed=True
        )
    try:
        text = coordinate.read_text(encoding="utf-8")
        document = pdbtext.parse(text)
    except OSError as error:
        return None, ToolResult(output=f"读坐标失败：{error}", failed=True)
    return _Receptor(entry=receptor_entry, text=text, atoms=document.atoms), None


def _resolve_pose(
    ledger: SessionLedger, arguments: dict[str, Any], *, tool_name: str
) -> tuple[_Pose | None, ToolResult | None]:
    """把 `docking` + `molecule` 两个参数解析成一个 `_Pose`。

    返回 `(位姿, None)` 或 `(None, 直接可以返回给模型的失败结果)`。失败一律以
    `ToolResult` 交回，不在中间抛异常——这些错都要能原样回灌给模型自纠。
    """
    entry, failure = _docking_entry(ledger, arguments, tool_name=tool_name)
    if failure is not None:
        return None, failure

    molecule = str(arguments.get("molecule") or "").strip()
    if not molecule:
        return None, ToolResult(
            output="缺少参数 molecule。它是 dock 产物里的 id 列，不是序号。", failed=True
        )

    try:
        manifest = _read_manifest(entry, docking.MANIFEST_FILE)
    except ToolError as error:
        return None, ToolResult(output=str(error), failed=True)

    results = manifest.get("results") or []
    record = next((item for item in results if str(item.get("id") or "") == molecule), None)
    if record is None:
        available = [str(item.get("id") or "") for item in results][:MAX_IDS_SHOWN]
        listing = "、".join(available) if available else "（这份产物里一条结果都没有）"
        return None, ToolResult(
            output=(
                f"{entry.handle} 里没有 id 为 {molecule} 的分子。"
                f"可用的 id：{listing}" + ("……" if len(results) > MAX_IDS_SHOWN else "")
            ),
            failed=True,
        )

    relative = str(record.get("pose_pdb") or "").strip()
    if not relative:
        return None, ToolResult(
            output=(
                f"{molecule} 这条没有转出 PDB（引擎给了分数，但最佳位姿转不成 PDB）。"
                "分数仍可用，接触与氢键算不了——这不是文件丢失。"
            ),
            failed=True,
        )
    pose = Path(str(entry.path)) / relative
    if not pose.is_file():
        return None, ToolResult(
            output=f"产物里记的位姿文件不在了：{pose}（目录被移动或清过？）", failed=True
        )

    receptor, failure = _receptor_of(ledger, entry, manifest)
    if failure is not None:
        return None, failure

    try:
        pose_text = pose.read_text(encoding="utf-8")
        ligand_doc = pdbtext.parse(pose_text)
    except OSError as error:
        return None, ToolResult(output=f"读坐标失败：{error}", failed=True)

    ligand_atoms = tuple(atom for atom in ligand_doc.atoms if pdbtext.is_heavy(atom))
    if not ligand_atoms:
        return None, ToolResult(output=f"{pose} 里没有一个重原子，位姿是空的。", failed=True)

    engine = manifest.get("engine") or {}
    return (
        _Pose(
            molecule=molecule,
            name=str(record.get("name") or ""),
            pose=pose,
            record=record,
            entry=entry,
            receptor=receptor.entry,
            receptor_text=receptor.text,
            pose_text=pose_text,
            receptor_atoms=receptor.atoms,
            ligand_atoms=ligand_atoms,
            engine=f"{engine.get('name') or 'vina'}:{engine.get('scoring') or 'vina'}",
        ),
        None,
    )



class AnalyzePoseTool(Tool):
    """看某一个分子的位姿是靠哪些残基、哪些氢键撑住的。"""

    spec = ToolSpec(
        name="analyze_pose",
        description=(
            "看**一个**分子的对接位姿：接触残基（含最短距离与接触原子数）、氢键、"
            "原子冲突。只读，不登记任何东西。\n"
            "分子用 id（如 mol-7），不是序号也不是路径——id 取自 dock 的产物。"
            "距离一律由代码算，判据写在返回的第一段，口径变了会看得出来。\n"
            "要判断「这个分子凭什么强」而不只是「碰到了谁」，把 interactions 打开："
            "盐桥、π 堆积、疏水、卤键、水桥、金属配位都由 PLIP 认键级后给出，"
            "接触距离看不出这些。它要多花 0.3–1 秒，且本机要装 PLIP"
            "（与化学栈同一份 requirements-chem.txt）；没装时照常给接触与氢键，"
            "只在末尾说明指纹没算成。\n"
            "用途：榜首那条为什么排第一、某个取代基是不是多了一个氢键、"
            "位姿有没有穿进受体。要给整张表排序用 rank_candidates。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "docking": {"type": "string", "description": "对接产物的 handle，如 artifact#3"},
                "molecule": {"type": "string", "description": "分子 id（dock 产物里的 id 列）"},
                "cutoff": {
                    "type": "number",
                    "description": (
                        f"接触判据（埃），默认 {CONTACT_CUTOFF:g}，"
                        f"范围 {MIN_CONTACT_CUTOFF:g}–{MAX_CONTACT_CUTOFF:g}"
                    ),
                },
                "hbond": {
                    "type": "boolean",
                    "description": "是否算氢键，默认 true（关掉只省一点时间，接触与冲突照算）",
                },
                "interactions": {
                    "type": "boolean",
                    "description": (
                        "是否加 PLIP 交互指纹（盐桥 / π 堆积 / 疏水 / 卤键 / 水桥 / 金属配位），"
                        "默认 false。要多花 0.3–1 秒，本机没装 PLIP 时这一段会变成一句说明"
                    ),
                },
            },
            "required": ["docking", "molecule"],
            "additionalProperties": False,
        },
    )
    primary_argument = "docking"
    evidence_key = ("molecule",)
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：analyze_pose 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        try:
            cutoff = _number_argument(
                arguments, "cutoff", CONTACT_CUTOFF, MIN_CONTACT_CUTOFF, MAX_CONTACT_CUTOFF
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        want_hbond = arguments.get("hbond")
        if want_hbond is not None and not isinstance(want_hbond, bool):
            return ToolResult(output="参数 hbond 应当是 true / false", failed=True)
        compute_hbond = True if want_hbond is None else want_hbond
        want_plip = arguments.get("interactions")
        if want_plip is not None and not isinstance(want_plip, bool):
            return ToolResult(output="参数 interactions 应当是 true / false", failed=True)

        pose_data, failure = _resolve_pose(ledger, arguments, tool_name="analyze_pose")
        if failure is not None:
            return failure

        contacts = _contacts(pose_data.receptor_atoms, pose_data.ligand_atoms, cutoff)
        clashes = _clashes(pose_data.receptor_atoms, pose_data.ligand_atoms)
        hbonds = (
            _hbonds(pose_data.receptor_atoms, pose_data.ligand_atoms) if compute_hbond else []
        )

        # PLIP 要原始文本（含氢），不能拿那份重原子视图——氢键的供体判据靠它。
        fingerprint: dict[str, list[_PlipHit]] | None = None
        fingerprint_note = ""
        if want_plip:
            fingerprint, fingerprint_note = plip_fingerprint(
                pose_data.receptor_text, pose_data.pose_text
            )

        meta: dict[str, Any] = {
            "molecule": pose_data.molecule,
            "engine": pose_data.engine,
            "contacts": len(contacts),
            "hbonds": len(hbonds),
            "clashes": len(clashes),
        }
        if want_plip:
            if fingerprint_note:
                # 算没成时**不写 interactions**：空字典与「算了但没有相互作用」长得一样，
                # 而这两种结论对决策的意义相反。有这个键 = 算过；有 interactions_error = 没算成。
                meta["interactions_error"] = fingerprint_note
            else:
                meta["interactions"] = {
                    kind: len(items) for kind, items in (fingerprint or {}).items() if items
                }

        return ToolResult(
            output=_render_pose(
                molecule=pose_data.molecule,
                name=pose_data.name,
                pose=pose_data.pose,
                contacts=contacts,
                hbonds=hbonds,
                clashes=clashes,
                cutoff=cutoff,
                fingerprint=fingerprint,
                fingerprint_note=fingerprint_note,
            ),
            meta=meta,
        )


DEFAULT_CONTACT_MAP_ROWS = 30
MAX_CONTACT_MAP_ROWS = 100


@dataclass(frozen=True)
class _ResidueContact:
    """一个残基在这份产物的所有位姿里出现的情况。"""

    label: str
    resname: str
    poses: int
    nearest: float
    atoms: int


def _contact_map(
    receptor: Sequence[pdbtext.AtomRecord],
    poses: Sequence[tuple[str, Sequence[pdbtext.AtomRecord]]],
    cutoff: float,
) -> list[_ResidueContact]:
    """每个残基在多少个位姿里接触过，最近的一次多远。

    排序按「出现次数降序、最近距离升序」：先看这一批是不是都挤在同一个亚口袋，
    再看是谁贴得最近。频次的分母是**算得出位姿的那些分子**，缺位姿的不计入，
    否则一个转不出 PDB 的分子会把所有残基的频次一起拉低。
    """
    distances: dict[str, list[float]] = {}
    atom_counts: dict[str, int] = {}
    resnames: dict[str, str] = {}
    for _molecule, ligand in poses:
        for item in _contacts(receptor, ligand, cutoff):
            distances.setdefault(item.label, []).append(item.distance)
            atom_counts[item.label] = atom_counts.get(item.label, 0) + item.atoms
            resnames.setdefault(item.label, item.resname)
    found = [
        _ResidueContact(
            label=label,
            resname=resnames[label],
            poses=len(values),
            nearest=min(values),
            atoms=atom_counts[label],
        )
        for label, values in distances.items()
    ]
    found.sort(key=lambda item: (-item.poses, item.nearest, item.label))
    return found


class ContactMapTool(Tool):
    """把一份对接产物里所有位姿的残基接触并成一张频次表。"""

    spec = ToolSpec(
        name="contact_map",
        description=(
            "把**一份对接产物里所有位姿**的残基接触并成一张表：每个残基在多少个位姿里"
            "接触过、最近一次多远、一共碰到多少个原子。只读，不登记任何东西。\n"
            "用途：判断这一批分子是不是都挤在同一个亚口袋（说明口袋选对了），"
            "还是各占一处（说明盒子里有好几个可结合的位置，分数不可直接比）。"
            "单个位姿的氢键与冲突用 analyze_pose。\n"
            "频次的分母是**算得出位姿的分子数**，不是产物里的条数：转不出 PDB 的分子"
            "不计入，第一行会写明可用几个、跳过了几个。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "docking": {"type": "string", "description": "对接产物的 handle，如 artifact#3"},
                "cutoff": {
                    "type": "number",
                    "description": (
                        f"接触判据（埃），默认 {CONTACT_CUTOFF:g}，"
                        f"范围 {MIN_CONTACT_CUTOFF:g}–{MAX_CONTACT_CUTOFF:g}"
                    ),
                },
                "top": {
                    "type": "integer",
                    "description": (
                        f"最多列几个残基，默认 {DEFAULT_CONTACT_MAP_ROWS}，"
                        f"上限 {MAX_CONTACT_MAP_ROWS}"
                    ),
                },
            },
            "required": ["docking"],
            "additionalProperties": False,
        },
    )
    primary_argument = "docking"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：contact_map 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        try:
            cutoff = _number_argument(
                arguments, "cutoff", CONTACT_CUTOFF, MIN_CONTACT_CUTOFF, MAX_CONTACT_CUTOFF
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        top = _int_argument(arguments, "top", DEFAULT_CONTACT_MAP_ROWS, 1, MAX_CONTACT_MAP_ROWS)

        entry, failure = _docking_entry(ledger, arguments, tool_name="contact_map")
        if failure is not None:
            return failure
        try:
            manifest = _read_manifest(entry, docking.MANIFEST_FILE)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        receptor, failure = _receptor_of(ledger, entry, manifest)
        if failure is not None:
            return failure

        poses: list[tuple[str, tuple[pdbtext.AtomRecord, ...]]] = []
        skipped = 0
        for record in manifest.get("results") or []:
            molecule = str(record.get("id") or "")
            relative = str(record.get("pose_pdb") or "").strip()
            if not relative:
                skipped += 1
                continue
            path = Path(str(entry.path)) / relative
            try:
                document = pdbtext.parse(path.read_text(encoding="utf-8"))
            except OSError:
                skipped += 1
                continue
            atoms = tuple(atom for atom in document.atoms if pdbtext.is_heavy(atom))
            if not atoms:
                skipped += 1
                continue
            poses.append((molecule, atoms))

        if not poses:
            return ToolResult(
                output=(
                    f"{entry.handle} 里没有一个能算出接触的位姿（共 "
                    f"{len(manifest.get('results') or [])} 条，全部缺 PDB 或 PDB 是空的）。"
                    "分数仍可用；先看 analyze_pose 对单条位姿的报法。"
                ),
                failed=True,
            )

        table = _contact_map(receptor.atoms, poses, cutoff)
        meta = {
            "handle": entry.handle,
            "poses": len(poses),
            "skipped": skipped,
            "residues": len(table),
            "cutoff": cutoff,
        }
        if not table:
            return ToolResult(
                output=(
                    f"结论：{len(poses)} 个位姿在 {cutoff:g} Å 内一个受体残基都没碰到"
                    f"（另跳过 {skipped} 条缺位姿的）。\n"
                    "这通常是盒子或受体坐标有问题，不是分子不好——先核对口袋。"
                ),
                meta=meta,
            )

        lines = [
            f"结论：{len(poses)} 个位姿里有 {table[0].poses} 个碰到过 "
            f"{table[0].label}，共 {len(table)} 个残基至少接触一次。"
        ]
        if skipped:
            lines.append(f"跳过 {skipped} 条缺位姿或空位姿的分子（不计入分母）。")
        lines.append(
            f"口径：接触 ≤ {cutoff:g} Å（重原子-重原子）；频次 = 接触该残基的位姿数 / {len(poses)}。"
        )
        lines.append("残基              出现      最近距离   接触原子")
        for item in table[:top]:
            lines.append(
                f"  {item.label:<16}{item.poses:>3}/{len(poses):<5}"
                f"{item.nearest:7.2f} Å   {item.atoms:>4}"
            )
        if len(table) > top:
            lines.append(f"  ……另有 {len(table) - top} 个残基未显示（调大 top 再看）")
        return ToolResult(output="\n".join(lines), meta=meta)


# ---------------------------------------------------------------------------
# 疏水分账：接触数（HP）/ 疏水匹配（HM）/ 埋藏极性（脱水账）
# ---------------------------------------------------------------------------
HYDROPHOBIC_ELEMENTS = frozenset({"C", "F", "Cl", "Br", "I"})
"""算「疏水」的元素。S 刻意不在里面：它在蛋白里既是疏水（Met/Cys）又是极性
（`POLAR` 把 S 算极性），两头都算会让同一对原子既进 HP 又进氢键。"""

POLAR_ELEMENTS = frozenset({"N", "O"})
"""这里说的「极性」只有 N 与 O。与上面的 `POLAR`（N/O/S）**故意不同**：那一组
是给氢键用的（S 能当弱受体），这一组是给「埋了要不要水」用的，硫醇的极性远弱于
羟基，算进来会把 Met 的硫醚误判成该成氢键的原子。差异写进返回的口径里。"""

POLAR_ADJACENT = 1.7
"""配体的碳在这个距离内有 N/O 时不算疏水。

PDB 里没有键，只能按距离认「这个碳挨着杂原子」——羰基碳、酰胺碳、羧基碳都是这种，
它们的化学性质和苯环碳完全相反。1.7 Å 取得比 C–O（1.23）/C–N（1.34）都宽，
是为了容忍坐标精度，又不会碰到 C–C（1.54）——太松的话芳环碳会被连坐。
"""

DEFAULT_HYDROPHOBIC_RADIUS = 4.0
MIN_HYDROPHOBIC_RADIUS = 3.0
MAX_HYDROPHOBIC_RADIUS = 6.0
POCKET_EXTRA = 0.5
MATCH_FRACTION = 0.5
BURIAL_MIN_ATOMS = 3
MAX_HYDROPHOBIC_ROWS = 8


@dataclass(frozen=True)
class _HydrophobicProfile:
    pairs: int
    closest: float
    total: int
    matched: int
    unmatched: tuple[tuple[str, float], ...]
    buried: tuple[tuple[str, int, str, float], ...]
    satisfied: tuple[str, ...]


def _element(atom: pdbtext.AtomRecord) -> str:
    return (atom.element or "").strip().upper()


def _hydrophobic_ligand_atoms(
    atoms: Sequence[pdbtext.AtomRecord],
) -> list[pdbtext.AtomRecord]:
    """配体上的疏水重原子：元素对，且 1.7 Å 内没有 N/O。"""
    hetero = [atom for atom in atoms if _element(atom) in POLAR_ELEMENTS]
    picked: list[pdbtext.AtomRecord] = []
    for atom in atoms:
        if _element(atom) not in HYDROPHOBIC_ELEMENTS:
            continue
        if any(_distance(atom.coord, other.coord) <= POLAR_ADJACENT for other in hetero):
            continue
        picked.append(atom)
    return picked


def _hydrophobic_profile(
    receptor: Sequence[pdbtext.AtomRecord],
    ligand: Sequence[pdbtext.AtomRecord],
    hbonds: Sequence[_HBond],
    radius: float,
) -> _HydrophobicProfile:
    """HP / HM / 脱水账。全部是几何，没有一项是拟合出来的能量。"""
    receptor_heavy = [
        atom for atom in receptor if pdbtext.is_heavy(atom) and not atom.is_hetero
    ]
    receptor_hydrophobic = [
        atom for atom in receptor_heavy if _element(atom) in HYDROPHOBIC_ELEMENTS
    ]
    ligand_hydrophobic = _hydrophobic_ligand_atoms(ligand)
    ligand_polar = [
        atom for atom in ligand if pdbtext.is_heavy(atom) and _element(atom) in POLAR_ELEMENTS
    ]

    pairs = 0
    closest = 0.0
    for left in ligand_hydrophobic:
        for right in receptor_hydrophobic:
            distance = _distance(left.coord, right.coord)
            if distance > radius:
                continue
            pairs += 1
            closest = distance if closest == 0.0 else min(closest, distance)

    # HM：每个疏水原子周围有没有「疏水口袋」。半径比接触稍宽——匹配问的是环境像不像，
    # 不是这一对原子贴得多近。
    matched = 0
    unmatched: list[tuple[str, float]] = []
    pocket_radius = radius + POCKET_EXTRA
    for atom in ligand_hydrophobic:
        neighbours = [
            other
            for other in receptor_heavy
            if _distance(atom.coord, other.coord) <= pocket_radius
        ]
        if not neighbours:
            unmatched.append((atom.name, 0.0))
            continue
        greasy = sum(1 for other in neighbours if _element(other) in HYDROPHOBIC_ELEMENTS)
        fraction = greasy / len(neighbours)
        if fraction >= MATCH_FRACTION:
            matched += 1
        else:
            unmatched.append((atom.name, fraction))
    unmatched.sort(key=lambda item: item[1])

    # 脱水账：埋进去的极性原子，成没成氢键。成了是「兑现」，没成就是白付了脱水代价。
    bonded = set()
    for item in hbonds:
        if item.donor_label == "配体":
            bonded.add(item.donor_atom)
        if item.acceptor_label == "配体":
            bonded.add(item.acceptor_atom)

    receptor_polar = [
        atom for atom in receptor_heavy if _element(atom) in POLAR_ELEMENTS
    ]
    buried: list[tuple[str, int, str, float]] = []
    satisfied: list[str] = []
    for atom in ligand_polar:
        contacts = sum(
            1 for other in receptor_heavy if _distance(atom.coord, other.coord) <= radius
        )
        if contacts < BURIAL_MIN_ATOMS:
            continue
        if atom.name in bonded:
            satisfied.append(atom.name)
            continue
        nearest = ""
        nearest_distance = 0.0
        for other in receptor_polar:
            distance = _distance(atom.coord, other.coord)
            if not nearest or distance < nearest_distance:
                nearest, nearest_distance = f"{other.label} {other.name}", distance
        buried.append((atom.name, contacts, nearest, nearest_distance))

    return _HydrophobicProfile(
        pairs=pairs,
        closest=closest,
        total=len(ligand_hydrophobic),
        matched=matched,
        unmatched=tuple(unmatched),
        buried=tuple(buried),
        satisfied=tuple(satisfied),
    )


def _render_hydrophobic(
    *,
    pose: _Pose,
    profile: _HydrophobicProfile,
    radius: float,
) -> str:
    # 埋了而没算出氢键的，再按「够不够近」分两类。这一刀很重要：3.19 Å 的极性对
    # 说成「白埋」是错的——它够近，只是角度没过本工具的口径（PDB 里的氢可能缺、
    # 也可能质子化状态不同）。真正的白埋是「最近极性受体都在氢键距离之外」。
    blank = [row for row in profile.buried if row[3] > HBOND_DISTANCE or not row[2]]
    facing = [row for row in profile.buried if row[2] and row[3] <= HBOND_DISTANCE]

    if profile.total:
        ratio = profile.matched / profile.total * 100
        summary = f"疏水接触 {profile.pairs} 对、匹配 {profile.matched}/{profile.total}（{ratio:.0f}%）"
    else:
        summary = "配体上没有疏水原子（HP / HM 不适用）"
    summary += f"；{len(blank)} 个极性原子白埋"
    if blank:
        summary += f"（{blank[0][0]} 最近极性受体 {blank[0][3]:.2f} Å）"
    if facing:
        summary += f"，另有 {len(facing)} 个埋进去的极性原子与受体极性原子只差 {facing[0][3]:.2f} Å 却没成键"
    summary += "。"

    lines = [f"结论：{summary}"]
    lines.append(
        f"分子：{pose.molecule}" + (f"（{pose.name}）" if pose.name and pose.name != pose.molecule else "")
    )
    lines.append(f"位姿：{pose.pose}")
    lines.append(
        f"口径：疏水 = {'/'.join(sorted(HYDROPHOBIC_ELEMENTS))} 且 {POLAR_ADJACENT:g} Å 内无 N/O；"
        f"接触 ≤ {radius:g} Å；埋深 = {radius:g} Å 内 ≥{BURIAL_MIN_ATOMS} 个受体重原子；"
        f"匹配 = {radius + POCKET_EXTRA:g} Å 内受体原子里疏水占比 ≥{MATCH_FRACTION:.0%}；"
        f"氢键 = 供体-受体 ≤ {HBOND_DISTANCE:g} Å 且角度 ≥ {HBOND_ANGLE:g}°。"
    )
    lines.append(
        "**这不是 HYDE**：HYDE 把脱水代价折成 kcal，系数要用它的原文校准；"
        "这里只给结构证据——哪些疏水原子落在疏水口袋里、哪些极性原子埋了却没成氢键。"
    )

    lines.append("疏水（HP / HM）：")
    if profile.total:
        closest = f"，最近 {profile.closest:.2f} Å" if profile.closest else ""
        lines.append(f"  HP 接触 {profile.pairs} 对{closest}")
        lines.append(
            f"  HM 匹配 {profile.matched}/{profile.total}"
            f"（{profile.matched / profile.total * 100:.0f}%）"
        )
        if profile.unmatched:
            shown = profile.unmatched[:MAX_HYDROPHOBIC_ROWS]
            detail = "、".join(f"{name}（周围疏水 {fraction:.0%}）" for name, fraction in shown)
            rest = len(profile.unmatched) - len(shown)
            lines.append(f"  未匹配：{detail}" + (f"，另有 {rest} 个" if rest else ""))
    else:
        lines.append("  配体上一个疏水原子都没有：全是极性/带电荷原子，HP 与 HM 不适用")

    lines.append("埋藏极性（脱水账）：")
    if profile.buried:
        for name, contacts, nearest, distance in profile.buried[:MAX_HYDROPHOBIC_ROWS]:
            if not nearest:
                lines.append(f"  {name}  埋深 {contacts} 个受体原子，未成氢键，受体上没有极性原子")
            elif distance <= HBOND_DISTANCE:
                lines.append(
                    f"  {name}  埋深 {contacts} 个受体原子，与 {nearest} 只差 {distance:.2f} Å "
                    "却没算成氢键（角度没过口径或质子化状态不同）"
                )
            else:
                lines.append(
                    f"  {name}  埋深 {contacts} 个受体原子，未成氢键，"
                    f"最近极性受体 {nearest} {distance:.2f} Å——白埋"
                )
        rest = len(profile.buried) - MAX_HYDROPHOBIC_ROWS
        if rest > 0:
            lines.append(f"  ……另有 {rest} 个未显示")
    else:
        lines.append("  没有被埋而没成氢键的极性原子：埋进去的极性原子都兑现了")
    if profile.satisfied:
        lines.append(f"  已成氢键：{len(profile.satisfied)} 个（{'、'.join(profile.satisfied)}）")
    return "\n".join(lines)


class AnalyzeHydrophobicTool(Tool):
    """一个位姿的分数，是靠疏水匹配撑住的，还是靠分子大、接触多。"""

    spec = ToolSpec(
        name="analyze_hydrophobic",
        description=(
            "把一个对接位姿的**疏水贡献拆开看**：HP（疏水接触对数）、"
            "HM（配体疏水原子里有多少落在疏水口袋里）、以及**脱水账**"
            "（埋进去却没成氢键的极性原子，逐个列出并给出最近受体极性原子的距离）。\n"
            "用途：判断榜首那条的高分是真结合还是「又大又油」；判断某个极性基团埋进去是赚是亏。"
            "HM 比 HP 更可靠——「疏水原子落进疏水口袋」比「接触数多」更能说明问题。\n"
            "口径写在返回里，**这不是 HYDE**：不给 kcal，只给结构证据。"
            "分子用 id（dock 产物里的 id 列）。只读，不登记任何东西。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "docking": {"type": "string", "description": "对接产物的 handle，如 artifact#3"},
                "molecule": {"type": "string", "description": "分子 id（dock 产物里的 id 列）"},
                "radius": {
                    "type": "number",
                    "description": (
                        f"接触半径（埃），默认 {DEFAULT_HYDROPHOBIC_RADIUS:g}，"
                        f"范围 {MIN_HYDROPHOBIC_RADIUS:g}–{MAX_HYDROPHOBIC_RADIUS:g}"
                    ),
                },
            },
            "required": ["docking", "molecule"],
            "additionalProperties": False,
        },
    )
    primary_argument = "docking"
    evidence_key = ("molecule",)
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output=(
                    "内核装配问题：analyze_hydrophobic 需要一个会话登记表，"
                    "但这次调用没有拿到。"
                ),
                failed=True,
            )
        try:
            radius = _number_argument(
                arguments,
                "radius",
                DEFAULT_HYDROPHOBIC_RADIUS,
                MIN_HYDROPHOBIC_RADIUS,
                MAX_HYDROPHOBIC_RADIUS,
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        pose_data, failure = _resolve_pose(
            ledger, arguments, tool_name="analyze_hydrophobic"
        )
        if failure is not None:
            return failure

        hbonds = _hbonds(pose_data.receptor_atoms, pose_data.ligand_atoms)
        profile = _hydrophobic_profile(
            pose_data.receptor_atoms, pose_data.ligand_atoms, hbonds, radius
        )
        return ToolResult(
            output=_render_hydrophobic(pose=pose_data, profile=profile, radius=radius),
            meta={
                "molecule": pose_data.molecule,
                "engine": pose_data.engine,
                "hp_pairs": profile.pairs,
                "hm_total": profile.total,
                "hm_matched": profile.matched,
                "buried_polar": len(profile.buried),
                "hbonded_polar": len(profile.satisfied),
            },
        )


# ---------------------------------------------------------------------------
# 效率指标：LE / LELP / LLE
# ---------------------------------------------------------------------------
EFFICIENCY_SORTS = ("lelp", "le", "score")
DEFAULT_EFFICIENCY_SORT = "lelp"
DEFAULT_EFFICIENCY_TOP = 20
MAX_EFFICIENCY_TOP = 200
FAT_LOGP = 4.0
FAT_LELP = 6.0
LELP_NOTE = "LELP = logP / LE，越低越好；常用门槛 ≤ 6，是经验值不是定律"


@dataclass(frozen=True)
class _EfficiencyRow:
    id: str
    name: str
    score: float
    heavy_atoms: int
    logp: float | None
    le: float | None
    lelp: float | None
    pchembl: float | None
    lle: float | None

    @property
    def fat(self) -> bool:
        """「大而油」：亲脂性高、而且每颗重原子换来的分数还少。"""
        return (
            self.logp is not None
            and self.lelp is not None
            and self.logp >= FAT_LOGP
            and self.lelp >= FAT_LELP
        )


def _logp(smiles: str) -> float | None:
    """Wildman-Crippen logP。没有 rdkit、SMILES 读不出来时返回 None（不猜）。"""
    if not smiles:
        return None
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import Descriptors

        RDLogger.DisableLog("rdApp.*")
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return float(Descriptors.MolLogP(mol))
    except Exception:  # noqa: BLE001 —— 没装 rdkit / 解析失败，都只是「这一列没有」
        return None


def _potency_table(entry: Any) -> dict[str, float]:
    """配体集里带 `pchembl_value` 的记录 → `{规范 SMILES: pChEMBL}`。

    同一个结构有多条记录时取最强的那个：LLE 问的是「这个结构最好能做到多强」，
    平均值会被测砸的实验拉低。
    """
    manifest = _read_manifest(entry, "manifest.json")
    table: dict[str, float] = {}
    for record in manifest.get("records") or []:
        smiles = str(record.get("smiles") or "")
        value = record.get("pchembl_value")
        if not smiles or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        key = _molecule_key(smiles)
        if not key:
            continue
        if key not in table or number > table[key]:
            table[key] = number
    return table


def _efficiency_rows(
    manifest: dict[str, Any], potency: dict[str, float] | None = None
) -> list[_EfficiencyRow]:
    rows: list[_EfficiencyRow] = []
    for item in manifest.get("results") or []:
        score = _number(item.get("score"))
        if score is None:
            continue
        heavy = item.get("heavy_atoms")
        heavy_atoms = heavy if isinstance(heavy, int) and heavy > 0 else 0
        le = docking.ligand_efficiency(score, heavy_atoms) if heavy_atoms else None
        logp = _logp(str(item.get("smiles") or ""))
        lelp = None
        if logp is not None and le:
            lelp = logp / le
        pchembl = None
        lle = None
        if potency:
            key = _molecule_key(str(item.get("smiles") or ""))
            pchembl = potency.get(key) if key else None
            if pchembl is not None and logp is not None:
                lle = pchembl - logp
        rows.append(
            _EfficiencyRow(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or item.get("id") or ""),
                score=score,
                heavy_atoms=heavy_atoms,
                logp=logp,
                le=le,
                lelp=lelp,
                pchembl=pchembl,
                lle=lle,
            )
        )
    return rows


def _sorted_efficiency(rows: Sequence[_EfficiencyRow], sort: str) -> list[_EfficiencyRow]:
    """缺值的一律排最后：`None` 与真实的 0 是两件事，不能混进排序里。

    **不要用 `reverse=True` 做降序**：它会把「缺值排最后」这个前缀位一起翻过来，
    变成缺值的排最前。把符号放进值里，前缀位永远保持 False < True。
    """
    if sort == "score":
        return sorted(rows, key=lambda row: row.score)
    if sort == "le":
        return sorted(rows, key=lambda row: (row.le is None, -(row.le or 0.0)))
    return sorted(rows, key=lambda row: (row.lelp is None, row.lelp or 0.0))


def _render_efficiency(
    *,
    rows: Sequence[_EfficiencyRow],
    shown: Sequence[_EfficiencyRow],
    sort: str,
    engine: str,
    has_potency: bool,
    total: int,
) -> str:
    fat = sum(1 for row in rows if row.fat)
    without_lelp = sum(1 for row in rows if row.lelp is None)
    first = shown[0] if shown else None
    metric = {"lelp": "LELP", "le": "LE", "score": "对接分数"}[sort]

    summary = f"{total} 条里 {fat} 条「大而油」（logP ≥ {FAT_LOGP:g} 且 LELP ≥ {FAT_LELP:g}）"
    if first is not None:
        summary += f"；按 {metric} 排第一的是 {first.id}"
        if sort == "lelp" and first.lelp is not None:
            summary += f"（LELP {first.lelp:.1f}）"
        elif sort == "le" and first.le is not None:
            summary += f"（LE {first.le:.2f}）"
    summary += "。"

    lines = [f"结论：{summary}"]
    lines.append(f"对接产物：{engine}；共 {len(rows)} 条")
    lines.append(
        "口径：LE = −分数 / 重原子数；"
        + LELP_NOTE
        + "；LLE = pChEMBL − logP（只有给了带活性的配体集才有）。"
    )
    lines.append(
        "**从对接分数算出来的 LE 不等于从 ΔG 算的 LE**：它只能在同一批、同一引擎、"
        "同一力场里比较，不是绝对效率。logP 用 RDKit 的 Wildman-Crippen。"
    )
    if without_lelp:
        lines.append(f"（{without_lelp} 条算不出 logP——SMILES 缺失或读不出来，它们排在最后。）")

    header = f"  {'#':>3}  {'分子':<14}{'重原子':>6}{'logP':>8}{'LE':>8}{'LELP':>8}"
    if has_potency:
        header += f"{'pChEMBL':>9}{'LLE':>7}"
    lines.append(header)
    for index, row in enumerate(shown, start=1):
        line = f"  {index:>3}  {row.id[:14]:<14}{row.heavy_atoms:>6}"
        line += f"{row.logp:>8.2f}" if row.logp is not None else f"{'—':>8}"
        line += f"{row.le:>8.2f}" if row.le is not None else f"{'—':>8}"
        line += f"{row.lelp:>8.1f}" if row.lelp is not None else f"{'—':>8}"
        if has_potency:
            line += f"{row.pchembl:>9.2f}" if row.pchembl is not None else f"{'—':>9}"
            line += f"{row.lle:>7.1f}" if row.lle is not None else f"{'—':>7}"
        if row.fat:
            line += "  ← 大而油"
        lines.append(line)
    if len(rows) > len(shown):
        lines.append(f"  ……另有 {len(rows) - len(shown)} 条未显示")
    return "\n".join(lines)


class EfficiencyProfileTool(Tool):
    """一批对接结果的效率体检：每颗重原子换来多少分，是不是靠亲脂性堆上去的。"""

    spec = ToolSpec(
        name="efficiency_profile",
        description=(
            "对**一份对接产物**做效率体检：LE（每颗重原子换来多少分）、"
            "LELP（logP / LE，越低越好）、以及给了带活性的配体集时的 LLE（pChEMBL − logP）。"
            "并把「大而油」的标出来（logP ≥ 4 且 LELP ≥ 6）。\n"
            "用途：榜单前面那几个是不是分子肥胖——重原子越多、疏水接触越多，"
            "原始分数天然越高，而 LE / LELP 把这一层扣掉。"
            "给了 actives（带 pchembl_value 的配体集）时按规范 SMILES 联接算 LLE，"
            "它扣掉的是「亲脂性带来的假活性」。\n"
            "口径写在返回里：**从对接分数算的 LE 不等于从 ΔG 算的 LE**，"
            "只能在同一批、同一引擎、同一力场里比。只读，不登记新东西。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "docking": {"type": "string", "description": "对接产物的 handle，如 artifact#3"},
                "actives": {
                    "type": "string",
                    "description": (
                        "可选：带 pchembl_value 的配体集 handle（search_actives → "
                        "build_reference_set，或自己导入的活性表）。给了才算 LLE"
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": list(EFFICIENCY_SORTS),
                    "description": (
                        f"排序依据，默认 {DEFAULT_EFFICIENCY_SORT}（LELP 升序，越低越好）；"
                        "le 是降序、score 是升序"
                    ),
                },
                "top": {
                    "type": "integer",
                    "description": (
                        f"显示前多少条，默认 {DEFAULT_EFFICIENCY_TOP}，上限 {MAX_EFFICIENCY_TOP}"
                    ),
                },
            },
            "required": ["docking"],
            "additionalProperties": False,
        },
    )
    primary_argument = "docking"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：efficiency_profile 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw = str(arguments.get("docking") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 docking。先 list_workspace 看清有哪份对接产物。", failed=True
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, ArtifactHandle, "docking")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        kind = str((entry.meta or {}).get("kind") or "")
        if kind != "docking":
            return ToolResult(
                output=(
                    f"{entry.handle} 是 {kind or '未知类型'}，不是对接产物。"
                    "efficiency_profile 只认 dock 的产物。"
                ),
                failed=True,
            )

        try:
            top = _int_argument(
                arguments, "top", DEFAULT_EFFICIENCY_TOP, MIN_TOP, MAX_EFFICIENCY_TOP
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        sort = str(arguments.get("sort") or DEFAULT_EFFICIENCY_SORT).strip().lower()
        if sort not in EFFICIENCY_SORTS:
            return ToolResult(
                output=f"参数 sort 只能是 {' / '.join(EFFICIENCY_SORTS)}（现在是 {sort!r}）",
                failed=True,
            )

        potency: dict[str, float] | None = None
        actives = str(arguments.get("actives") or "").strip()
        if actives:
            try:
                actives_entry = ledger.get(actives)
                expect_handle(actives_entry, LigandSetHandle, "actives")
                potency = _potency_table(actives_entry)
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            except ToolError as error:
                return ToolResult(output=str(error), failed=True)
            if not potency:
                return ToolResult(
                    output=(
                        f"{actives} 里没有一条带 pchembl_value 的记录，算不了 LLE。"
                        "LLE 要实测活性：用 search_actives → build_reference_set 的产品，"
                        "或者导入一份带 pchembl_value 列的 CSV。"
                    ),
                    failed=True,
                )

        try:
            manifest = _read_manifest(entry, docking.MANIFEST_FILE)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        rows = _efficiency_rows(manifest, potency)
        if not rows:
            return ToolResult(
                output=f"{entry.handle} 里没有带分数的结果，效率指标无从算起。", failed=True
            )
        engine_block = manifest.get("engine") or {}
        engine = (
            f"{engine_block.get('name') or 'vina'}:{engine_block.get('scoring') or 'vina'}"
        )
        ordered = _sorted_efficiency(rows, sort)
        shown = ordered[:top]
        return ToolResult(
            output=_render_efficiency(
                rows=rows,
                shown=shown,
                sort=sort,
                engine=engine,
                has_potency=bool(potency),
                total=len(rows),
            ),
            meta={
                "engine": engine,
                "total": len(rows),
                "fat": sum(1 for row in rows if row.fat),
                "without_logp": sum(1 for row in rows if row.lelp is None),
                "sort": sort,
                "top": [row.id for row in shown],
                "has_potency": bool(potency),
            },
        )


# ---------------------------------------------------------------------------
# 重对接验证：把共晶配体再对一次，看位姿能不能回到晶体位置
# ---------------------------------------------------------------------------
VALIDATION_ROOT = "validation"
DEFAULT_RMSD_CUTOFF = 2.0
MIN_RMSD_CUTOFF = 0.5
MAX_RMSD_CUTOFF = 5.0
RMSD_METHOD = (
    "原地 RMSD（不重新叠合），仅重原子，按元素自动配对并取对称等价的最小值；"
    "配对只比拓扑不比键级（PDB 里没有键级，两边读法可能不同）"
)


def validation_dir() -> Path:
    """重对接产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".vsagent" / VALIDATION_ROOT


def _rdkit_mol(path: Path):
    """读一份单分子 PDB。**不 sanitize 成功也要能拿到坐标**——PDB 里没有键级。

    氢要显式删一次，不能只靠 `MolFromPDBBlock(removeHs=True)`：那个开关是在
    解析后的标准后处理里生效的，而标准后处理要求 `sanitize=True`；这里给的是
    `sanitize=False`，于是它静默空转。Vina 写出来的位姿就是带氢的（实测伊马替尼
    68 个原子对共晶配体 37 个重原子），不删的话每一行都变成「原子数不同」，
    工具看着像坏了——而真正的原因是这条读法从来没删过氢。
    """
    from rdkit import Chem

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    mol = Chem.MolFromPDBBlock(text, removeHs=False, sanitize=False)
    if mol is None:
        return None
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        # 键级猜错的 PDB 很常见；拿不到芳香性不影响原子配对与 RMSD。
        pass
    try:
        mol = Chem.RemoveHs(mol, sanitize=False)
    except Exception:
        # 删不掉就把氢留着：原子数对不上时下面的报错会说清是多少对多少，
        # 比吞掉异常、拿一份带着氢的分子去算 RMSD 强。
        pass
    return mol


def _topology(mol):
    """把分子压成**只有元素与连接**的图：所有键改单键、清掉芳香标记、不要隐式氢。

    不这么做的话「是不是同一个分子」这一问没有答案：PDB 里没有键级，键级是读文件
    时按 CONECT 与距离猜出来的。实测同一份伊马替尼，从 Vina 位姿里读出来是
    24 根芳香键 + 1 根双键（那份 PDB 带 35 行 CONECT），从共晶配体 `ligand.pdb`
    里读出来是 41 根单键——按键级比，这两个「同一个分子」永远对不上，
    而报出来的话是「连接图对不上」，把人指向错误的方向。

    代价是分不出「同一张连接图、不同键级」的两种分子（比如芳香环与还原环）。
    重对接要判的是「位姿有没有回到晶体位置」，不是「它是不是同一个互变异构体」；
    这一层的取舍写在 `RMSD_METHOD` 里。
    """
    from rdkit import Chem

    copy = Chem.RWMol(mol)
    for bond in copy.GetBonds():
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
    for atom in copy.GetAtoms():
        atom.SetIsAromatic(False)
        atom.SetNoImplicit(True)
    return copy.GetMol()


def _rmsd(pose: Path, reference: Path) -> tuple[float | None, str]:
    """返回 `(rmsd, 失败原因)`。对不上时 rmsd 为 `None`，原因写清是哪一步不对。

    **不用 `GetBestRMS`**：它先做刚体叠合再算 RMSD，于是「整体平移了 2 Å」会被算成 0。
    重对接要的恰恰是**原地**偏差——位姿已经在受体坐标系里，任何再叠合都在替它作弊。
    这里取所有对称等价映射里的最小值（`uniquify=False`），只做配对、不做旋转平移。
    """
    from rdkit import Chem

    probe = _rdkit_mol(pose)
    target = _rdkit_mol(reference)
    if probe is None:
        return None, "位姿文件读不出分子"
    if target is None:
        return None, "共晶配体文件读不出分子"
    if probe.GetNumAtoms() != target.GetNumAtoms():
        return None, (
            f"原子数不同（位姿 {probe.GetNumAtoms()}、共晶配体 {target.GetNumAtoms()}），"
            "不是同一个分子，没法算重对接 RMSD"
        )
    probe = _topology(probe)
    target = _topology(target)
    matches = probe.GetSubstructMatches(target, uniquify=False, useChirality=False)
    if not matches:
        return None, "位姿与共晶配体的连接图对不上（不是同一个分子，或 PDB 的连接丢了）"

    probe_conf = probe.GetConformer()
    reference_conf = target.GetConformer()
    best: float | None = None
    for match in matches:
        total = 0.0
        for reference_index, probe_index in enumerate(match):
            left = probe_conf.GetAtomPosition(probe_index)
            right = reference_conf.GetAtomPosition(reference_index)
            total += (left.x - right.x) ** 2 + (left.y - right.y) ** 2 + (left.z - right.z) ** 2
        value = math.sqrt(total / len(match))
        if best is None or value < best:
            best = value
    if best is None or not math.isfinite(best):
        return None, "算出来的 RMSD 不是有限数"
    return best, ""


def _render_validation(
    *,
    handle: str,
    reused: bool,
    rows: Sequence[dict[str, Any]],
    cutoff: float,
    target: Path,
    ligand_label: str,
) -> str:
    comparable = [row for row in rows if row.get("rmsd") is not None]
    passed = [row for row in comparable if row.get("pass")]
    lines = [
        f"重对接验证完成，登记为 {handle}。" if not reused
        else f"登记表里已有同一份：{handle}（未重算）。",
        f"参照是受体产物里的共晶配体 {ligand_label}；口径：{RMSD_METHOD}。",
    ]
    if comparable:
        best = min(comparable, key=lambda row: row["rmsd"])
        verdict = "整体通过" if passed else f"一条都没通过（阈值 {cutoff:g} Å）"
        lines.append(
            f"{len(comparable)} 个分子可比、{len(passed)} 个通过；{verdict}。"
            f"最小 RMSD {best['rmsd']:.2f} Å（{best['id']}）。"
        )
    else:
        lines.append(
            f"没有可比的分子（阈值 {cutoff:g} Å 用不上）——"
            "位姿与共晶配体不是同一个分子，见下面的原因。"
        )
    for row in rows[:MAX_HBOND_ROWS]:
        if row.get("rmsd") is None:
            lines.append(f"  {row['id']}：不算（{row.get('detail')}）")
        else:
            mark = "通过" if row.get("pass") else "未过"
            lines.append(f"  {row['id']}：RMSD {row['rmsd']:.2f} Å · {mark}")
    if len(rows) > MAX_HBOND_ROWS:
        lines.append(f"  ……另有 {len(rows) - MAX_HBOND_ROWS} 条未显示")
    if comparable and not passed:
        lines.append(
            "没过不代表分子不好：先看盒子是否覆盖共晶配体、exhaustiveness 是否太低。"
            "这个结果会作为 gates.redock_rmsd 进候选表。"
        )
    lines.append(f"产物目录：{target}")
    lines.append("下一步：rank_candidates 会把 redock_rmsd 当门禁，超标的那条降到 below_cutoff。")
    return "\n".join(lines)


class ValidateDockingTool(Tool):
    """把共晶配体重新对接一次，用它判断这套对接参数靠不靠谱。"""

    spec = ToolSpec(
        name="validate_docking",
        description=(
            "**重对接验证**：把受体产物里的共晶配体当成普通配体对一次，量它的位姿"
            "回到晶体位置有多远（RMSD）。这是对**对接协议**的体检，不是对候选分子的打分。"
            "\n参照是 `prepare_receptor` 写出的 `ligand.pdb`；没有它就直接说「做不了」，"
            "不假装通过。原子数或元素对不上时逐条说明原因，不给一个假的数。"
            "\n结果会作为 `gates.redock_rmsd` 进候选表：超过阈值的分子降到 below_cutoff。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "receptor": {
                    "type": "string",
                    "description": "准备后的受体 handle（必须有 ligand.pdb）",
                },
                "docking": {"type": "string", "description": "对接产物 handle"},
                "cutoff": {
                    "type": "number",
                    "description": (
                        f"通过阈值（埃），默认 {DEFAULT_RMSD_CUTOFF:g}，"
                        f"范围 {MIN_RMSD_CUTOFF:g}–{MAX_RMSD_CUTOFF:g}"
                    ),
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["receptor", "docking"],
            "additionalProperties": False,
        },
    )
    primary_argument = "docking"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：validate_docking 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw_receptor = str(arguments.get("receptor") or "").strip()
        raw_docking = str(arguments.get("docking") or "").strip()
        if not raw_receptor or not raw_docking:
            return ToolResult(
                output="缺少参数：receptor（准备后的受体）与 docking（对接产物）都要给。",
                failed=True,
            )
        try:
            receptor = ledger.get(raw_receptor)
            expect_handle(receptor, ReceptorHandle, "receptor")
            docking_entry = ledger.get(raw_docking)
            expect_handle(docking_entry, ArtifactHandle, "docking")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        meta = dict(receptor.meta or {})
        if str(meta.get("stage") or "") != "prepared":
            return ToolResult(
                output=(
                    f"{receptor.handle} 还没有准备过（stage={meta.get('stage') or '未知'}）。"
                    "重对接要用 prepare_receptor 的产物——它才会写出 ligand.pdb。"
                ),
                failed=True,
            )
        ligand_name = str(meta.get("ligand_file") or "").strip()
        if not ligand_name:
            return ToolResult(
                output=(
                    f"{receptor.handle} 里没有共晶配体（`ligand_file` 是空的），"
                    "**没法做重对接**。换一份带共晶配体的结构，或用 define_pocket 定盒子"
                    "之后不宣称「验证通过」。"
                ),
                failed=True,
            )
        reference = Path(str(receptor.path)) / ligand_name
        if not reference.is_file():
            return ToolResult(
                output=f"受体产物里记的共晶配体不在了：{reference}", failed=True
            )

        try:
            cutoff = _number_argument(
                arguments, "cutoff", DEFAULT_RMSD_CUTOFF, MIN_RMSD_CUTOFF, MAX_RMSD_CUTOFF
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            manifest = _read_manifest(docking_entry, docking.MANIFEST_FILE)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        if str((manifest.get("receptor") or {}).get("handle") or "") != str(receptor.handle):
            return ToolResult(
                output=(
                    f"这份对接用的是 {str((manifest.get('receptor') or {}).get('handle') or '未知受体')}，"
                    f"不是 {receptor.handle}——拿另一份受体的共晶配体当参照没有意义。"
                ),
                failed=True,
            )

        results = manifest.get("results") or []
        if not results:
            return ToolResult(output=f"{docking_entry.handle} 里没有任何对接结果。", failed=True)

        rows: list[dict[str, Any]] = []
        for item in results:
            identifier = str(item.get("id") or "")
            relative = str(item.get("pose_pdb") or "").strip()
            if not relative:
                rows.append(
                    {
                        "id": identifier,
                        "rmsd": None,
                        "pass": False,
                        "detail": "这条没有转出 PDB（只有分数）",
                    }
                )
                continue
            pose = Path(str(docking_entry.path)) / relative
            if not pose.is_file():
                rows.append(
                    {"id": identifier, "rmsd": None, "pass": False,
                     "detail": f"位姿文件不在了：{relative}"}
                )
                continue
            value, reason = _rmsd(pose, reference)
            rows.append(
                {
                    "id": identifier,
                    "rmsd": None if value is None else round(value, 3),
                    "pass": bool(value is not None and value <= cutoff),
                    "detail": reason,
                }
            )

        params = {
            "receptor": str(receptor.handle),
            "docking": str(docking_entry.handle),
            "cutoff": cutoff,
            "method": RMSD_METHOD,
        }
        stem = str(arguments.get("name") or "redock")
        try:
            target, reused = products.content_dir(
                validation_dir(), stem, params, ("validation.json", products.PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="artifact", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("kind") or "") == "validation":
                    cached = list((candidate.meta or {}).get("results") or [])
                    return ToolResult(
                        output=_render_validation(
                            handle=str(candidate.handle),
                            reused=True,
                            rows=cached,
                            cutoff=cutoff,
                            target=target,
                            ligand_label=str((candidate.meta or {}).get("ligand_label") or ligand_name),
                        ),
                        meta={"handle": str(candidate.handle), "reused": True,
                              "comparable": sum(1 for r in cached if r.get("rmsd") is not None)},
                    )

        payload = {
            "kind": "validation",
            "docking": str(docking_entry.handle),
            "receptor": str(receptor.handle),
            "ligand_label": str(meta.get("ligand_label") or ligand_name),
            "cutoff": cutoff,
            "method": RMSD_METHOD,
            "results": rows,
        }
        try:
            products.write_json_atomic(target / "validation.json", payload)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / products.PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写验证产物失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="artifact", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("kind") or "") == "validation":
                handle = str(candidate.handle)
                break
        if not handle:
            comparable = [row for row in rows if row["rmsd"] is not None]
            best = min((row["rmsd"] for row in comparable), default=None)
            entry = ledger.add(
                "artifact",
                path=target,
                summary=(
                    "重对接验证 · "
                    + (f"RMSD {best:.2f} Å" if best is not None else "无可比分子")
                ),
                fingerprint=fingerprint,
                parents=[receptor.handle, docking_entry.handle],
                meta={
                    "kind": "validation",
                    "stage": "validation",
                    "tool": "validate_docking",
                    "docking": str(docking_entry.handle),
                    "receptor": str(receptor.handle),
                    "ligand_label": payload["ligand_label"],
                    "cutoff": cutoff,
                    "method": RMSD_METHOD,
                    "results": rows,
                    "params": params,
                    "file": "validation.json",
                },
            )
            handle = str(entry.handle)

        return ToolResult(
            output=_render_validation(
                handle=handle,
                reused=False,
                rows=rows,
                cutoff=cutoff,
                target=target,
                ligand_label=payload["ligand_label"],
            ),
            meta={
                "handle": handle,
                "comparable": sum(1 for row in rows if row["rmsd"] is not None),
                "passed": sum(1 for row in rows if row["pass"]),
                "reused": False,
            },
        )


class PickControlsTool(Tool):
    """从候选表里挑出这一批的对照分子，并写明每一条为什么算对照。"""

    spec = ToolSpec(
        name="pick_controls",
        description=(
            "从候选表里挑出**对照分子**，并写明每一条为什么算对照。"
            "只读产物，不写盘。\n"
            "三类各挑一到几个：\n"
            "- 正向对照：排名最高、门禁全过。它测不出活性时，问题在实验或假设，不在排序。\n"
            "- 阴性对照：排名最低、门禁全过。预期不结合；它要也活，说明这个位点的排序没意义。\n"
            "- 背离对照：门禁没过却排在前半。分数好看而性质不过关，用来检验「只看分数」会不会踩坑。\n"
            "给了 `hypothesis` 会说明这批对照用来检验哪一条假设。"
            "**每批候选都该带对照**：没有对照的活性数据说明不了任何事。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "候选表 handle（rank_candidates 的产物），例如 artifact#5",
                },
                "hypothesis": {
                    "type": "string",
                    "description": "这条对照是为哪条假设准备的，如 hyp#1；可选",
                },
                "limit": {
                    "type": "integer",
                    "description": "每一类最多挑几个（默认 1，最多 3）",
                },
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    )
    primary_argument = "source"
    # 只读，但要沿 handle 找到候选表；与 analyze_pose 同理（纪律 2 的例外）。
    context_argument = "ledger"
    task_argument = "state"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：pick_controls 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("source") or "").strip()
        if not raw:
            return ToolResult(output="缺少参数 source：候选表的 handle。", failed=True)
        try:
            entry = ledger.get(raw)
            expect_handle(entry, ArtifactHandle, "候选表")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        if str((entry.meta or {}).get("kind") or "") != "candidates":
            return ToolResult(
                output=(
                    f"{entry.handle} 不是候选表（kind={(entry.meta or {}).get('kind')!r}）。"
                    "先用 rank_candidates 生成一张候选表。"
                ),
                failed=True,
            )
        try:
            payload = read_candidates(Path(str(entry.path)))
        except (ToolError, OSError) as error:
            return ToolResult(output=f"读候选表失败：{error}", failed=True)
        rows = _rows_from_payload(payload)
        if not rows:
            return ToolResult(output=f"{entry.handle} 里没有任何候选行。", failed=True)

        limit = _int_argument(arguments, "limit", 1, 1, 3)
        picks = _pick_controls(rows, limit)

        hypothesis = None
        hypothesis_id = str(arguments.get("hypothesis") or "").strip()
        if hypothesis_id:
            state = arguments.get("state")
            hypothesis = state.find_hypothesis(hypothesis_id) if state is not None else None
            if hypothesis is None:
                return ToolResult(
                    output=f"没有 {hypothesis_id} 这条假设。先用 hypothesis(action=\"list\") 看编号。",
                    failed=True,
                )

        lines = [f"对照分子（来源 {entry.handle}，候选 {len(rows)} 条）："]
        missing: list[str] = []
        for role, why, chosen in picks:
            if not chosen:
                missing.append(role)
                lines.append(f"- {role}：这张表里挑不出来（{why}）。")
                continue
            for row in chosen:
                lines.append(
                    f"- {row.id} {role}（排名 {row.consensus:g}，档位 {row.tier}）：{why}"
                )
        if hypothesis is not None:
            lines.append(
                f"\n这批对照用来检验 {hypothesis.id}：{hypothesis.claim}"
                f"（预期：{hypothesis.expected}）"
            )
        lines.append(
            "建议：下单时把对照与候选一起测；数据回来用 "
            "record_assay(hypothesis=...) 回填，状态会自动结案。"
        )
        if missing:
            lines.append(f"注意：{'、'.join(missing)}缺位，这批结果的说服力会打折。")
        return ToolResult(
            output="\n".join(lines),
            meta={
                "handle": entry.handle,
                "hypothesis": hypothesis.id if hypothesis is not None else "",
                "controls": {role: [row.id for row in chosen] for role, _why, chosen in picks},
                "missing": missing,
            },
        )


def _pick_controls(
    rows: Sequence[CandidateRow], limit: int
) -> list[tuple[str, str, list[CandidateRow]]]:
    """三类对照各挑几个。返回 (角色, 为什么算对照, 挑中的行)。

    排序沿用候选表自己的顺序（`rank_candidates` 已按共识秩排好），这里不重排：
    换个排序口径就会与表里那一列对不上，而用户看的是表。
    """
    total = len(rows)
    clean = [row for row in rows if not _gate_failures(row.gates)]
    top_half = rows[: max(1, total // 2)]
    divergent = [row for row in top_half if _gate_failures(row.gates)]

    picks: list[tuple[str, str, list[CandidateRow]]] = [
        (
            "正向对照",
            "排名最高且门禁全过。它测不出活性时，问题在实验或假设，不在排序。",
            clean[:limit],
        ),
        (
            "阴性对照",
            "排名最低且门禁全过。预期不结合；它要也活，说明这个位点的排序没有意义。",
            clean[-limit:][::-1] if len(clean) > 1 else [],
        ),
        (
            "背离对照",
            "门禁没过却排在前半。分数与可成药性背离，用来检验「只按分数挑」会不会踩坑。",
            divergent[:limit],
        ),
    ]
    # 同一行不该在两个角色里各出现一次：正向与阴性在只有一行时会是同一条。
    seen: set[str] = set()
    result: list[tuple[str, str, list[CandidateRow]]] = []
    for role, why, chosen in picks:
        unique = [row for row in chosen if row.id not in seen]
        seen.update(row.id for row in unique)
        result.append((role, why, unique))
    return result


def domain_tools() -> list[Tool]:
    return [
        RankCandidatesTool(),
        AnalyzePoseTool(),
        ContactMapTool(),
        AnalyzeHydrophobicTool(),
        EfficiencyProfileTool(),
        ValidateDockingTool(),
        PickControlsTool(),
    ]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    main()
