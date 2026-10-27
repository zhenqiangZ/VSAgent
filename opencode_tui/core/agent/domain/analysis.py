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

**`analyze_pose` 是这里唯一声明 `context_argument="ledger"` 的只读工具**（纪律 2 的
两处例外之一，另一处是 `assess_structure`）。它必须沿血缘找到对接时用的那份受体
坐标，而坐标只在登记表里；但它**一个字节都不写、不登记任何东西**，所以模型不会因此
以为手上多了一个 handle。理由写在这里，是因为「只读却挂了登记表」看着像违规。
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    ArtifactHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import affinity, docking, export, pdbtext, products

__all__ = [
    "AnalyzePoseTool",
    "CandidateRow",
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
    return Path.home() / ".opencode_tui" / "candidates"


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
) -> tuple[str, tuple[dict[str, Any], ...], bool]:
    """按 `meta.kind` 取这张表里「分子 → 分数」的那一列，返回 `(列名, 行, 是否越高越好)`。"""
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
        return column, kept, higher

    if kind not in SOURCE_KINDS:
        raise ToolError(
            f"{entry.handle} 是 {kind or '未知类型'}，没法当候选来源。"
            f"能合并的只有：{', '.join(sorted(SOURCE_KINDS))}，以及筛选通道产出的候选表。"
        )
    label, higher = SOURCE_KINDS[kind]

    if kind == "docking":
        manifest = _read_manifest(entry, docking.MANIFEST_FILE)
        results = manifest.get("results") or []
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
    return label, kept, higher


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
        + "、".join(f"{s.handle}（{s.label}）" for s in sources)
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
                label, records, higher = _records_of(entry, ledger)
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
                )
            )

        params = {
            "sources": [
                {"handle": source.handle, "label": source.label} for source in sources
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


def _render_pose(
    *,
    molecule: str,
    name: str,
    pose: Path,
    contacts: Sequence[_Contact],
    hbonds: Sequence[_HBond],
    clashes: Sequence[tuple[str, str, str, float]],
    cutoff: float,
) -> str:
    summary = f"{len(contacts)} 个残基接触"
    if contacts:
        summary += f"（最近 {contacts[0].distance:.2f} Å）"
    summary += f"，{len(hbonds)} 个氢键"
    summary += "，无冲突。" if not clashes else f"，**{len(clashes)} 处冲突**。"
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
    return "\n".join(lines)


class AnalyzePoseTool(Tool):
    """看某一个分子的位姿是靠哪些残基、哪些氢键撑住的。"""

    spec = ToolSpec(
        name="analyze_pose",
        description=(
            "看**一个**分子的对接位姿：接触残基（含最短距离与接触原子数）、氢键、"
            "原子冲突。只读，不登记任何东西。\n"
            "分子用 id（如 mol-7），不是序号也不是路径——id 取自 dock 的产物。"
            "距离一律由代码算，判据写在返回的第一段，口径变了会看得出来。\n"
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
                    "analyze_pose 只认 dock 的产物。"
                ),
                failed=True,
            )

        molecule = str(arguments.get("molecule") or "").strip()
        if not molecule:
            return ToolResult(
                output="缺少参数 molecule。它是 dock 产物里的 id 列，不是序号。", failed=True
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

        try:
            manifest = _read_manifest(entry, docking.MANIFEST_FILE)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        results = manifest.get("results") or []
        record = next((item for item in results if str(item.get("id") or "") == molecule), None)
        if record is None:
            available = [str(item.get("id") or "") for item in results][:MAX_IDS_SHOWN]
            listing = "、".join(available) if available else "（这份产物里一条结果都没有）"
            return ToolResult(
                output=(
                    f"{entry.handle} 里没有 id 为 {molecule} 的分子。"
                    f"可用的 id：{listing}"
                    + ("……" if len(results) > MAX_IDS_SHOWN else "")
                ),
                failed=True,
            )

        relative = str(record.get("pose_pdb") or "").strip()
        if not relative:
            return ToolResult(
                output=(
                    f"{molecule} 这条没有转出 PDB（Vina 给了分数，但最佳位姿转不成 PDB）。"
                    "分数仍可用，接触与氢键算不了——这不是文件丢失。"
                ),
                failed=True,
            )
        pose = Path(str(entry.path)) / relative
        if not pose.is_file():
            return ToolResult(
                output=f"产物里记的位姿文件不在了：{pose}（目录被移动或清过？）", failed=True
            )

        receptor_handle = str((manifest.get("receptor") or {}).get("handle") or "")
        if not receptor_handle:
            return ToolResult(
                output=f"{entry.handle} 的产物里没有记受体 handle，取不到坐标。", failed=True
            )
        try:
            receptor_entry = ledger.get(receptor_handle)
        except Exception as error:
            return ToolResult(
                output=f"{error}（对接用的受体已经不在登记表里，接触算不了）", failed=True
            )
        coordinate = export._receptor_coordinate(receptor_entry)
        if coordinate is None:
            return ToolResult(
                output=f"{receptor_entry.handle} 找不到坐标文件（准备产物不完整？）", failed=True
            )

        try:
            receptor_doc = pdbtext.parse(coordinate.read_text(encoding="utf-8"))
            ligand_doc = pdbtext.parse(pose.read_text(encoding="utf-8"))
        except OSError as error:
            return ToolResult(output=f"读坐标失败：{error}", failed=True)

        receptor_atoms = receptor_doc.atoms
        ligand_atoms = tuple(atom for atom in ligand_doc.atoms if pdbtext.is_heavy(atom))
        if not ligand_atoms:
            return ToolResult(output=f"{pose} 里没有一个重原子，位姿是空的。", failed=True)

        contacts = _contacts(receptor_atoms, ligand_atoms, cutoff)
        clashes = _clashes(receptor_atoms, ligand_atoms)
        hbonds = _hbonds(receptor_atoms, ligand_atoms) if compute_hbond else []

        return ToolResult(
            output=_render_pose(
                molecule=molecule,
                name=str(record.get("name") or ""),
                pose=pose,
                contacts=contacts,
                hbonds=hbonds,
                clashes=clashes,
                cutoff=cutoff,
            ),
            meta={
                "molecule": molecule,
                "contacts": len(contacts),
                "hbonds": len(hbonds),
                "clashes": len(clashes),
            },
        )


# ---------------------------------------------------------------------------
# 重对接验证：把共晶配体再对一次，看位姿能不能回到晶体位置
# ---------------------------------------------------------------------------
VALIDATION_ROOT = "validation"
DEFAULT_RMSD_CUTOFF = 2.0
MIN_RMSD_CUTOFF = 0.5
MAX_RMSD_CUTOFF = 5.0
RMSD_METHOD = "原地 RMSD（不重新叠合），仅重原子，按元素自动配对并取对称等价的最小值"


def validation_dir() -> Path:
    """重对接产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / VALIDATION_ROOT


def _rdkit_mol(path: Path):
    """读一份单分子 PDB。**不 sanitize 成功也要能拿到坐标**——PDB 里没有键级。"""
    from rdkit import Chem

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    mol = Chem.MolFromPDBBlock(text, removeHs=True, sanitize=False)
    if mol is None:
        return None
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        # 键级猜错的 PDB 很常见；拿不到芳香性不影响原子配对与 RMSD。
        pass
    return mol


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
    matches = probe.GetSubstructMatches(target, uniquify=False, useChirality=False)
    if not matches:
        return None, "位姿与共晶配体的连接关系对不上（不是同一个分子，或键级猜错了）"

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
        verdict = "通过" if passed else f"**没有通过**（阈值 {cutoff:g} Å）"
        lines.append(
            f"{len(comparable)} 个分子可比，{len(passed)} 个通过 {verdict}；"
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


def domain_tools() -> list[Tool]:
    return [RankCandidatesTool(), AnalyzePoseTool(), ValidateDockingTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    main()
