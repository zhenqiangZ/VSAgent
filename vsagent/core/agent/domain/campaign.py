"""开工前的盘点：受体侧与配体侧各看几个数，给出这一轮该走哪条路。

`ses_0005` 的毛病不在工具少，而在开工时没有一次判断：拿到 1M17 就直接准备受体、
对接、看位姿，而「有没有共晶、活性有多少条、化学型有几组、抗靶有没有数据」从来
没有一起看过。这个工具把那一次判断做成一次调用。

**常驻，不登记进 `FAMILIES`**：它决定这一轮装哪几族，一个「决定装哪族」的工具本身
要先装族才看得见，顺序就反了。注册位置在 `app.py:_build_registry`。

**判定是一张阈值表，不是模型打分。** 每个数都能复核，每个阈值都带一句理由；
换成分类模型之后「为什么判成同系物优化」就答不上来，而这张表的全部价值在于理由可查。

**不报 0**：查不到就写「没有记录」，记录为零会另算——两者对起点的影响完全不同。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..ledger import LigandSetHandle, SessionLedger, expect_handle
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import affinity, chembl, docking, export, pdbtext, preprocess, products, structure, uniprot

__all__ = [
    "AssessCampaignTool",
    "THRESHOLDS",
    "domain_tools",
    "render_strategies",
    "render_verdict",
]

# 阈值表。每一条都带一句理由：复核时不用翻代码，也不用问写的人当时怎么想的。
THRESHOLDS: dict[str, float] = {
    # 侧链与配体位置能解释的下限。更差的密度能看出口袋在哪，看不出配体怎么摆。
    "resolution": 3.0,
    # 够在骨架之间做比较的量。10–19 条只够做参考集与骨架分组。
    "actives_many": 20,
    # `build_reference_set` 标 `too_small` 的线。少于它，参考分布不稳。
    "actives_few": 10,
    # 一个系列要能拿出五个同骨架的分子，才谈得上在它里面做优化。
    "largest_scaffold": 5,
    # 三组以上算化学型分散。
    "scaffold_groups": 3,
    # ΔpChEMBL ≥2 是 100 倍差值；这种对子攒到五对才看得出趋势。
    "cliff_pairs": 5,
    # 十倍是项目常见的起点，也是「能不能算选择性」的最小分界。
    "selectivity": 10.0,
    # 低于它只能定方向（哪一侧更亲脂），不能定构象（配体怎么摆）。
    "plddt": 70,
}

ACTIVES_FLOOR = 7.0
"""盘点用的 pChEMBL 下限。7 约等于 100 nM，低于它的记录多是初筛级数据。"""

ACTIVES_WANT = 600
"""每端取回的条数上限。两端各 600 条，够看分布，也不必把整个库拉下来。"""

MMP_SCAN_LIMIT = 200
"""算匹配对时最多看这么多个分子。

**按活性从强到弱等距抽，不取最强的那些。** 一个靶点最强的前 200 条往往来自同一个
优化系列，彼此只差 1 个 pChEMBL 单位，于是「ΔpChEMBL ≥2 的对子」恒为 0——实测
P00533 的 325 条里最强 200 条的最大差只有 1.2。等距抽能把弱活性那一端带进来，
而匹配对要回答的正是「换掉这个基团，活性差多少」。
"""

START_NARROW = "narrow"
START_EXPAND = "expand"
START_LIGAND = "ligand"
START_BORROW = "borrow"

START_LABELS = {
    START_NARROW: "同系物优化",
    START_EXPAND: "换骨架",
    START_LIGAND: "配体侧建模",
    START_BORROW: "借邻近受体",
}

START_STEPS = {
    START_NARROW: (
        "detect_pockets(sources=ligand) → define_pocket → write_run(view=pockets) → "
        "build_reference_set → screen_similarity → pick_diverse → dock"
    ),
    START_EXPAND: (
        "detect_pockets(sources=ligand,complex,prior,predicted) → write_run(view=pockets) → "
        "curate_ligands → screen_shape / screen_pharmacophore → pick_diverse → dock"
    ),
    START_LIGAND: (
        "scaffold_analysis → matched_pairs → fetch_alphafold → define_pocket(residues=…) → "
        "prepare_receptor → dock 只当过滤"
    ),
    START_BORROW: (
        "similar_pockets → 邻近受体的 search_actives → build_reference_set → "
        "screen_similarity → pick_diverse"
    ),
}

START_FAMILIES = {
    START_NARROW: "pocket、receptor、ligand、screen、scoring、analysis、sar",
    START_EXPAND: "pocket、receptor、ligand、screen、pharmacophore、scoring、analysis",
    START_LIGAND: "sar、structure、pocket、receptor、scoring、analysis",
    START_BORROW: "pocket、chembl、ligand、screen",
}


@dataclass(frozen=True)
class Strategy:
    """一条候选策略：能不能走、为什么、走不通时卡在哪。

    四条策略各自判一次适用条件，不用 if 链短路。短路只够挑一条推荐，
    挑不出「另外一条也可行」——而策略比较要的正是那几条并列的。
    """

    start: str
    applies: bool
    reason: str
    blocked: str = ""


# 成本档是人工给定的先验：系统里没有任何关于合成与实验规模的数据。
# 排序值只决定候选的先后，不参与推荐——推荐按四类起点的固定优先级取。
STRATEGY_COSTS: dict[str, tuple[str, int]] = {
    START_LIGAND: ("纯计算，0 个合成", 0),
    START_BORROW: ("借邻近受体的已知配体，1 轮 IC50", 1),
    START_NARROW: ("合成 5–10 个同系物，1 轮 IC50", 2),
    START_EXPAND: ("筛一个库，命中后合成，1 轮 IC50", 3),
}

# 每条策略的产出能区分哪两条互相竞争的机制解释。写死在这里，因为
# 「这条策略回答什么问题」不随靶点变化，随靶点变的是它适不适用。
STRATEGY_DISCRIMINATES: dict[str, str] = {
    START_NARROW: "活性集中在一个骨架内 / 骨架外也有可结合型",
    START_EXPAND: "已知骨架覆盖足够 / 覆盖不足",
    START_LIGAND: "活性由某个锚定基团决定 / 由整体形状决定",
    START_BORROW: "局部口袋相似带来可迁移的配体 / 相似性不足以迁移",
}


def _row(identifier: str, name: str, smiles: str, score: float) -> dict[str, Any]:
    return {"id": identifier, "name": name, "smiles": smiles, "score": float(score)}


def _snapshot_rows(molecules: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """ChEMBL 快照 → 盘点用的行。分数是 pChEMBL。"""
    return [
        _row(
            str(item.get("parent_molecule_chembl_id") or item.get("molecule_chembl_id") or ""),
            str(item.get("molecule_pref_name") or ""),
            str(item.get("smiles") or ""),
            float(item.get("pchembl_value") or 0.0),
        )
        for item in molecules
    ]


def _scaffold_groups(rows: Sequence[dict[str, Any]]) -> tuple[list[int], dict[str, int]]:
    """Murcko 骨架分组。返回 `(每组的分子数, 骨架 → 分子数)`，只统计组内 ≥3 的组。"""
    from . import sar

    counts: dict[str, int] = {}
    for row in rows:
        if not row["smiles"]:
            continue
        try:
            from rdkit import Chem, rdBase
        except ImportError:  # pragma: no cover - rdkit 是可选依赖
            return [], {}
        with rdBase.BlockLogs():
            mol = Chem.MolFromSmiles(row["smiles"])
            if mol is None:
                continue
            scaffold = sar.scaffold_of(mol, "murcko") or sar.ACYCLIC
        counts[scaffold] = counts.get(scaffold, 0) + 1
    grouped = {name: size for name, size in counts.items() if size >= 3}
    return sorted(grouped.values(), reverse=True), grouped


def _scan_sample(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按活性排序后等距取 `MMP_SCAN_LIMIT` 个。少于此数时原样返回。"""
    ordered = sorted(rows, key=lambda row: -row["score"])
    if len(ordered) <= MMP_SCAN_LIMIT:
        return ordered
    step = len(ordered) / MMP_SCAN_LIMIT
    return [ordered[min(len(ordered) - 1, int(index * step))] for index in range(MMP_SCAN_LIMIT)]


def _mmp_pairs(
    rows: Sequence[dict[str, Any]],
    *,
    min_delta: float = 2.0,
    max_cut: int = 10,
) -> tuple[int, list[tuple[str, str, float]]]:
    """匹配分子对的密度：只差一个基团、活性差 ≥`min_delta` 的对子有几对。

    与 `matched_pairs` 用同一套切法（`sar._fragments`），但只算一个计数——盘点不需要
    那几十行明细，它只需要「这个库有没有足够的悬崖」这一个数。
    """
    from . import sar

    top = _scan_sample(rows)
    by_core: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for row in top:
        if not row["smiles"]:
            continue
        for core, chain in sar._fragments(row["smiles"], max_cut):
            by_core.setdefault(core, []).append((row, chain))

    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str, float]] = []
    for core, members in by_core.items():
        for index in range(len(members)):
            for other in range(index + 1, len(members)):
                left, left_chain = members[index]
                right, right_chain = members[other]
                if left["id"] == right["id"] or left_chain == right_chain:
                    continue
                # 按分子对去重，不按「分子对 + 共同骨架」：两个分子可能共用不止一处
                # 可切的位置，逐个算会把同一对算成两三对，密度就虚高了。
                key = tuple(sorted([left["id"], right["id"]]))
                if key in seen:
                    continue
                seen.add(key)
                delta = abs(float(left["score"]) - float(right["score"]))
                if delta >= min_delta:
                    pairs.append((left["id"], right["id"], round(delta, 2)))
    pairs.sort(key=lambda item: -item[2])
    return len(pairs), pairs


def _best_resolution(meta: dict[str, Any]) -> tuple[float | None, str]:
    """UniProt 里登记的实验结构：最佳分辨率与对应编号。没有结构时返回 `(None, "")`。"""
    best: tuple[float, str] | None = None
    for item in meta.get("pdb_structures") or []:
        value = item.get("resolution")
        if not isinstance(value, (int, float)):
            continue
        if best is None or float(value) < best[0]:
            best = (float(value), str(item.get("id") or ""))
    if best is None:
        return None, ""
    return best


def _receptor_facts(path: Path) -> dict[str, Any]:
    """受体坐标侧的事实：缺口、模型套数、链、非水杂原子组。

    只读本地文件，不联网。`ligand_groups` 之后还要过一遍几何筛查与脂质筛查——
    「有几个共晶配体」这句话必须与 `detect_pockets` 用的是同一套判据，否则盘点说
    「有 3 个配体」而口袋工具说「一个都没有」，用户不知道该信哪个。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    facts = structure.parse_pdb(text)
    doc = pdbtext.parse(text)
    filtered = export.ligand_groups(
        doc, modified_names=[name for name, _count in facts.modified]
    )
    screened = preprocess.screen_ligands(doc, filtered.kept)
    degreased = preprocess.screen_lipids(screened.kept)
    names = [group.resname for group in degreased.kept]
    missing = sum(chain.missing or 0 for chain in facts.chains)
    unknown = any(chain.missing is None for chain in facts.chains)
    return {
        "chains": list(facts.polymer_chains),
        "models": facts.models,
        "missing_residues": None if unknown else missing,
        "ligands": names,
        "ligand_groups": len(names),
    }


def _pick_strategies(
    *,
    structure_ok: bool,
    actives: int,
    largest_scaffold: int,
    pair_count: int,
) -> tuple[str, list[Strategy]]:
    """四条候选策略与一条推荐。

    推荐按「同系物优化 → 换骨架 → 配体侧建模 → 借邻近受体」的固定优先级
    取第一条适用的，与原来那条 if 链的结果一致。返回全部四条、按成本从低到高，
    不可行的也在里面：渲染时要说明它们为什么走不通，只返回可行的那几条会丢掉这句。

    适用条件按「这一轮还有没有别的做法」写，不按互斥写：结构可用时，
    系列内优化与骨架外探索都能走；结构不可用时，配体侧建模与借邻近受体
    都能走。互斥的写法只够给一条路线，给不出候选。
    """
    many = THRESHOLDS["actives_many"]
    few = THRESHOLDS["actives_few"]
    series = THRESHOLDS["largest_scaffold"]

    narrow_ok = structure_ok and actives >= many and largest_scaffold >= series
    expand_ok = structure_ok
    ligand_ok = not structure_ok and actives >= few
    # 活性不足 20 条时，系列内优化无从下手；借邻近受体不依赖本靶点的系列。
    borrow_ok = (not structure_ok) or actives < many

    if narrow_ok:
        narrow = Strategy(
            START_NARROW,
            True,
            f"结构可用、活性 {actives} 条（≥{many:g}）、最大骨架组 "
            f"{largest_scaffold} 个（≥{series:g}）、匹配对 {pair_count} 对："
            "已知化学型够多，可以在系列内做同系物优化。",
        )
    elif not structure_ok:
        narrow = Strategy(START_NARROW, False, "", "没有可用结构")
    else:
        narrow = Strategy(
            START_NARROW,
            False,
            "",
            f"活性 {actives} 条（<{many:g}）或最大骨架组 {largest_scaffold} 个"
            f"（<{series:g}），没有成型系列",
        )

    if expand_ok:
        expand = Strategy(
            START_EXPAND,
            True,
            "结构可用：可以在已知骨架之外用形状与药效团筛一个库。"
            if not narrow_ok
            else "结构可用：已知骨架够多，仍可在骨架之外探索新化学型。",
        )
    else:
        expand = Strategy(START_EXPAND, False, "", "没有可用结构，换骨架没有受体可筛")

    if ligand_ok:
        ligand = Strategy(
            START_LIGAND,
            True,
            f"没有可用实验结构，但活性 {actives} 条（≥{few:g}）：先从配体侧建模型，"
            "对接只当过滤器。",
        )
    elif not structure_ok:
        ligand = Strategy(
            START_LIGAND,
            False,
            "",
            f"活性 {actives} 条（<{few:g}），建不起参考分布",
        )
    else:
        ligand = Strategy(START_LIGAND, False, "", "有可用结构，走结构侧")

    if borrow_ok:
        borrow = Strategy(
            START_BORROW,
            True,
            f"结构{'可用' if structure_ok else '不可用'}、活性 {actives} 条："
            "本靶点缺少成型的系列，按口袋局部序列相似性去邻近受体借配体，"
            "先回答值不值得做。",
        )
    else:
        borrow = Strategy(
            START_BORROW,
            False,
            "",
            f"活性 {actives} 条（≥{many:g}），本靶点自己有起点",
        )

    candidates = [narrow, expand, ligand, borrow]
    recommended = next(item.start for item in candidates if item.applies)
    ordered = sorted(candidates, key=lambda item: STRATEGY_COSTS[item.start][1])
    return recommended, ordered


def render_strategies(strategies: Sequence[Strategy], recommended: str) -> list[str]:
    """候选策略那一段的文本行。

    抽成函数是为了让判据能量它的长度：`assess_campaign` 是常驻工具，返回文本
    每一步都可能进上下文，候选段写长一次就会一直长着。
    """
    viable = [item for item in strategies if item.applies]
    blocked = [item for item in strategies if not item.applies]
    lines: list[str] = [""]
    if len(viable) > 1:
        lines.append("候选策略（按成本从低到高；都可行，选哪条是这一轮的决策）")
    else:
        lines.append("候选策略（只有这一条可行）")
    for item in viable:
        cost, _rank = STRATEGY_COSTS[item.start]
        mark = " ← 推荐" if item.start == recommended else ""
        lines.append(
            f"- {START_LABELS[item.start]}（{item.start}）· 成本 {cost}"
            f" · 能区分 {STRATEGY_DISCRIMINATES[item.start]}{mark}"
        )
        if item.start != recommended:
            lines.append(f"  下一步：{START_STEPS[item.start]}")
    if blocked:
        lines.append(
            "不可行："
            + "；".join(
                f"{START_LABELS[item.start]}（{item.blocked}）" for item in blocked
            )
        )
    return lines


def pick_draft(drafts: Sequence[dict[str, str]], start: str) -> dict[str, str]:
    """按起点挑出那份假设草稿。

    `run()` 的 meta 与 `render_verdict()` 的文本用的是同一份草稿，两处各写一次
    `next(...)` 就有机会挑到不同的那份——`hypothesis` 字段与聊天里那段对不上时，
    模型会照 meta 里那份走。
    """
    return next(item for item in drafts if item["start"] == start)


def render_verdict(
    *,
    start: str,
    reason: str,
    structure_ok: bool,
    strategies: Sequence[Strategy],
    drafts: Sequence[dict[str, str]],
) -> list[str]:
    """判定那一段的文本行：推荐、候选、假设草稿。

    与 `render_strategies` 一起抽出来，是为了让整段返回文本能不起网络就跑到：
    这一段原来长在 `run()` 里，只有真连 UniProt 才走得到，漏一个变量名在测试里
    看不出来。
    """
    lines: list[str] = [
        "",
        f"判定：{START_LABELS[start]}（{start}）",
        f"理由：{reason}",
    ]
    if not structure_ok:
        lines.append("提示：结构不可用这一档里，口袋残基要从文献或突变数据来。")
    lines.append(f"建议装族：{START_FAMILIES[start]}")
    lines.append(f"下一步：{START_STEPS[start]}")
    lines.extend(render_strategies(strategies, start))
    chosen = pick_draft(drafts, start)
    lines.append("")
    lines.append(f"假设草稿（{chosen['kind']} / {chosen['mode']}）：{chosen['claim']}")
    lines.append(f"  检验：{chosen['test']}")
    lines.append(f"  预期：{chosen['expected']}")
    return lines


class AssessCampaignTool(Tool):
    """盘点受体侧与配体侧，给出这一轮该走哪条路。"""

    spec = ToolSpec(
        name="assess_campaign",
        description=(
            "开工前的**盘点**：一次调用看两侧的数，给出这一轮该走哪条路。"
            "受体侧看实验结构数、最佳分辨率、共晶配体、缺口；配体侧看 pChEMBL ≥7 的"
            "条数、骨架分组、最大一组、匹配对密度；给了抗靶就算两靶交集与选择性比。\n"
            "判定给一条**推荐**与 2–4 条**并列候选**：每条候选带成本档、能区分哪两条"
            "互相竞争的解释，以及一句理由（引用哪几个数）。走不通的策略也列出来并写明"
            "卡在哪。推荐的策略附一条假设草稿。**阈值写死在返回的第一行**，复核不用翻代码。\n"
            "`actives` 不给时自己去 ChEMBL 取一份摘要，**不登记任何产物**——盘点不该在"
            "登记表里留下模型没打算用的东西。`receptor` 给了才看缺口与共晶配体。\n"
            "查不到的数写「没有记录」，不写 0；抗靶没给时选择性整段不出现。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "accession": {
                    "type": "string",
                    "description": "靶点的 UniProt 编号，例如 P00533",
                },
                "actives": {
                    "type": "string",
                    "description": "search_actives 的配体集 handle；不给就自己取一份摘要",
                },
                "receptor": {
                    "type": "string",
                    "description": "已取的受体 handle；给了才能算缺口与共晶配体",
                },
                "anti_target": {
                    "type": "string",
                    "description": "抗靶的 UniProt 编号；给了就算两靶交集与选择性比",
                },
                "anti_actives": {
                    "type": "string",
                    "description": "抗靶的活性配体集 handle；不给就自己取一份摘要",
                },
                "pocket": {
                    "type": "string",
                    "description": "口袋集或口袋 handle；给了就报几处口袋",
                },
            },
            "required": ["accession"],
            "additionalProperties": False,
        },
    )
    primary_argument = "accession"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：assess_campaign 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            code = uniprot.normalize_accession(str(arguments.get("accession") or ""))
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        notes: list[str] = []
        record, meta = await self._target_record(code, notes)
        if record is None:
            return ToolResult(output="\n".join(notes), failed=True)

        rows, actives_note = await self._actives_rows(arguments, code, notes)
        anti_rows, anti_note = await self._anti_rows(arguments, notes)

        receptor: dict[str, Any] = {}
        raw_receptor = str(arguments.get("receptor") or "").strip()
        if raw_receptor:
            try:
                entry = ledger.get(raw_receptor)
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            path = export._receptor_coordinate(entry)
            if path is None:
                return ToolResult(output=export._no_coordinate_note(entry), failed=True)
            try:
                receptor = _receptor_facts(path)
            except (OSError, pdbtext.PdbEditError) as error:
                return ToolResult(output=f"读受体坐标失败：{error}", failed=True)
            receptor["handle"] = str(entry.handle)

        pocket_count = 0
        raw_pocket = str(arguments.get("pocket") or "").strip()
        if raw_pocket:
            try:
                pocket_entry = ledger.get(raw_pocket)
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            pocket_count = int((pocket_entry.meta or {}).get("count") or 0)
            if not pocket_count:
                try:
                    payload = products.read_json(Path(str(pocket_entry.path)) / "pockets.json")
                except (OSError, ValueError):
                    payload = {}
                pocket_count = len((payload or {}).get("pockets") or [])

        # ---- 受体侧 ----
        best_resolution, best_pdb = _best_resolution(meta)
        pdb_count = int(meta.get("pdb_count") or 0)
        structure_ok = bool(best_resolution is not None and best_resolution <= THRESHOLDS["resolution"])
        if not structure_ok and meta.get("alphafold_id"):
            structure_ok = True
            notes.append(
                f"没有分辨率达标的实验结构，按 AlphaFold 模型 {meta['alphafold_id']} 顶替；"
                "口袋区 pLDDT 低于 70 时只能定方向，不能定构象。"
            )

        # ---- 配体侧 ----
        groups, grouped = _scaffold_groups(rows)
        largest = groups[0] if groups else 0
        group_count = len(groups)
        pair_count, pairs = _mmp_pairs(rows)
        strongest = max((row["score"] for row in rows), default=0.0)

        # ---- 选择性 ----
        selectivity: dict[str, Any] = {}
        if anti_rows is not None:
            target_map = {row["id"]: row for row in rows}
            shared = [row for row in anti_rows if row["id"] in target_map]
            better = [
                row
                for row in shared
                if target_map[row["id"]]["score"] - row["score"] >= 1.0
            ]
            selectivity = {
                "anti": anti_note,
                "actives": len(anti_rows),
                "shared": len(shared),
                "selective": len(better),
            }

        start, strategies = _pick_strategies(
            structure_ok=structure_ok,
            actives=len(rows),
            largest_scaffold=largest,
            pair_count=pair_count,
        )
        reason = next(item.reason for item in strategies if item.start == start)

        lines = [
            "阈值：结构 ≤{resolution:g} Å、活性 ≥{actives_many:g} 条、"
            "最大骨架组 ≥{largest_scaffold:g} 个、SAR 密度 ≥{cliff_pairs:g} 对、"
            "选择性 ≥{selectivity:g} 倍".format(**THRESHOLDS),
            "",
            "受体侧",
        ]
        if pdb_count:
            lines.append(
                f"- 实验结构 {pdb_count} 套（UniProt 登记；"
                "RCSB 当前数量以 survey_structures 为准）"
                + (
                    f"，最佳分辨率 {best_resolution:.2f} Å（{best_pdb}）"
                    if best_resolution is not None
                    else "，都没有分辨率数据（NMR 或电镜）"
                )
            )
        else:
            lines.append("- 实验结构：UniProt 里没有登记（这是「没有记录」，不是 0 套）")
        if meta.get("alphafold_id"):
            lines.append(f"- AlphaFold 模型 {meta['alphafold_id']}")
        if receptor:
            lines.append(
                f"- 手上这份受体：{len(receptor['chains'])} 条链"
                f"（{'、'.join(receptor['chains'])}）、{receptor['models']} 套模型"
            )
            if receptor["missing_residues"] is None:
                lines.append("- 缺口：没有 SEQRES，无法判断（不报 0）")
            else:
                lines.append(f"- 缺口：{receptor['missing_residues']} 个残基不在坐标里")
            if receptor["ligand_groups"]:
                lines.append(
                    f"- 共晶配体 {receptor['ligand_groups']} 组："
                    + "、".join(receptor["ligands"][:8])
                )
            else:
                lines.append("- 共晶配体：这份结构里没有（水、离子、脂质已筛掉）")
        if pocket_count:
            lines.append(f"- 口袋集：{pocket_count} 处")

        lines.append("")
        lines.append("配体侧")
        if rows:
            lines.append(f"- pChEMBL ≥{ACTIVES_FLOOR:g} 的去重分子 {len(rows)} 个，最强 {strongest:.2f}")
            if group_count:
                lines.append(
                    f"- 骨架组 {group_count} 组（组内 ≥3），最大一组 {largest} 个"
                )
            else:
                lines.append("- 骨架组：没有一个组到 3 个分子（按「没有成型系列」处理）")
            lines.append(
                f"- ΔpChEMBL ≥2 的匹配对 {pair_count} 对"
                + (f"（最强一对 {pairs[0][0]} / {pairs[0][1]}，差 {pairs[0][2]:g}）" if pairs else "")
            )
        else:
            lines.append(f"- {actives_note}")

        if anti_rows is not None:
            lines.append("")
            lines.append("选择性")
            if selectivity["actives"]:
                lines.append(
                    f"- 抗靶 {anti_note}：活性 {selectivity['actives']} 条，"
                    f"两靶共有分子 {selectivity['shared']} 个，"
                    f"选择性比 ≥{THRESHOLDS['selectivity']:g} 的 {selectivity['selective']} 个"
                )
            else:
                lines.append(f"- 抗靶 {anti_note}：没有可用数据，本轮不承诺选择性")

        lines.append("")
        lines.append("打分环境")
        try:
            vina = docking.resolve_vina()
            lines.append(f"- 对接：{vina.path}")
        except ToolError as error:
            lines.append(f"- 对接：不可用（{error}）")
        try:
            autogrid = docking.resolve_autogrid()
            lines.append(f"- ad4 的 autogrid4：{autogrid}")
        except ToolError:
            lines.append("- ad4 的 autogrid4：不可用；vina / vinardo 不受影响")
        dta_ok, dta_detail = affinity.dta_readiness()
        if dta_ok:
            lines.append(f"- DTA：可用（{dta_detail}）")
        else:
            lines.append(f"- DTA：不可用（{dta_detail}）")
        lines.append(
            f"- DTA 样本门槛：≥40 个带 pChEMBL 的化合物；这里 pChEMBL ≥"
            f"{ACTIVES_FLOOR:g} 的有 {len(rows)} 个（保守估计）"
        )

        drafts = self._strategy_drafts(
            strategies=strategies,
            code=code,
            pair_count=pair_count,
            selectivity=selectivity,
        )
        lines.extend(
            render_verdict(
                start=start,
                reason=reason,
                structure_ok=structure_ok,
                strategies=strategies,
                drafts=drafts,
            )
        )

        return ToolResult(
            output="\n".join(lines),
            meta={
                "accession": code,
                "start": start,
                "structure_ok": structure_ok,
                "pdb_count": pdb_count,
                "best_resolution": best_resolution,
                "alphafold": meta.get("alphafold_id") or "",
                "actives": len(rows),
                "scaffold_groups": group_count,
                "largest_scaffold": largest,
                "mmp_pairs": pair_count,
                "strongest": strongest,
                "selectivity": selectivity,
                "pockets": pocket_count,
                "receptor": receptor,
                "hypothesis": pick_draft(drafts, start),
                "strategies": drafts,
            },
        )

    async def _target_record(
        self, code: str, notes: list[str]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """UniProt 条目：缓存优先，没有就联网。失败时把原因写进 notes 并返回 None。"""
        path = uniprot._record_path(code)
        cached = uniprot._read_cached(path)
        try:
            if cached is not None:
                record, _fingerprint = cached
            else:
                record = await asyncio.to_thread(
                    uniprot._fetch_record, code, timeout=60.0
                )
                uniprot._write_cached(path, record)
        except Exception as error:  # http.ToolError 或磁盘错误
            notes.append(
                f"UniProt 取 {code} 失败（{type(error).__name__}：{error}）。"
                "盘点做不下去：没有结构信息就没有受体侧这一半，"
                "不要凭记忆填。先 search_targets 确认编号，或稍后重试。"
            )
            return None, {}
        return record, uniprot._entry_meta(record, path)

    async def _actives_rows(
        self, arguments: dict[str, Any], code: str, notes: list[str]
    ) -> tuple[list[dict[str, Any]], str]:
        """配体侧的数。给了 handle 就只读产物；没给就取一份 ChEMBL 摘要，不登记。"""
        from . import sar

        raw = str(arguments.get("actives") or "").strip()
        if raw:
            entry = arguments["ledger"].get(raw)
            expect_handle(entry, LigandSetHandle, "actives")
            rows = sar._ligandset_rows(entry)
            return rows, f"{entry.handle} 里的活性分子 {len(rows)} 个"
        try:
            snapshot, _choice, _origin = await asyncio.to_thread(
                chembl.activity_snapshot,
                code,
                floor=ACTIVES_FLOOR,
                want=ACTIVES_WANT,
                spread=True,
            )
        except Exception as error:
            notes.append(
                f"ChEMBL 取 {code} 的活性失败（{type(error).__name__}：{error}）；"
                "按「没有记录」处理，不按 0 条处理。"
            )
            return [], "ChEMBL 没有取到活性数据"
        return _snapshot_rows(snapshot.molecules), "ChEMBL 摘要"

    async def _anti_rows(
        self, arguments: dict[str, Any], notes: list[str]
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """抗靶活性。没给抗靶时返回 `(None, "")`，选择性整段不出现。"""
        from . import sar

        raw_anti = str(arguments.get("anti_target") or "").strip()
        raw_actives = str(arguments.get("anti_actives") or "").strip()
        if not raw_anti and not raw_actives:
            return None, ""
        if raw_actives:
            entry = arguments["ledger"].get(raw_actives)
            expect_handle(entry, LigandSetHandle, "anti_actives")
            return sar._ligandset_rows(entry), entry.handle
        try:
            code = uniprot.normalize_accession(raw_anti)
        except ValueError as error:
            notes.append(str(error))
            return [], raw_anti
        try:
            snapshot, _choice, _origin = await asyncio.to_thread(
                chembl.activity_snapshot,
                code,
                floor=ACTIVES_FLOOR,
                want=ACTIVES_WANT,
                spread=True,
            )
        except Exception as error:
            notes.append(
                f"ChEMBL 取抗靶 {code} 失败（{type(error).__name__}：{error}）；选择性这一整段留空。"
            )
            return [], code
        return _snapshot_rows(snapshot.molecules), code

    def _strategy_drafts(
        self,
        *,
        strategies: Sequence[Strategy],
        code: str,
        pair_count: int,
        selectivity: dict[str, Any],
    ) -> list[dict[str, str]]:
        """每条可行策略一份填好的假设草稿。模型可以改，但不必从零想。

        只给可行的那几条：给走不通的策略写草稿，等于建议模型去走一条它走不通的路。
        """
        drafts: list[dict[str, str]] = []
        for item in strategies:
            if not item.applies:
                continue
            if item.start == START_NARROW:
                drafts.append(
                    {
                        "start": START_NARROW,
                        "kind": "strategy",
                        "mode": "narrow",
                        "claim": (
                            f"{code} 的已知活性集中在一个骨架系列里，"
                            "优化空间就在这个系列内。"
                        ),
                        "test": "在最大骨架组内算匹配分子对，看这些对子改的是不是同一处基团。",
                        "expected": (
                            f"ΔpChEMBL ≥2 的对子 ≥{THRESHOLDS['cliff_pairs']:g} 对"
                            f"（现在 {pair_count} 对），且这些对子改的是同一处基团。"
                        ),
                    }
                )
            elif item.start == START_EXPAND:
                drafts.append(
                    {
                        "start": START_EXPAND,
                        "kind": "strategy",
                        "mode": "expand",
                        "claim": "存在已知骨架之外的可结合化学型。",
                        "test": "用形状与药效团筛一个库，看命中落在哪些骨架上。",
                        "expected": "前 100 个里 ≥20 个不在已知骨架上。",
                    }
                )
            elif item.start == START_LIGAND:
                drafts.append(
                    {
                        "start": START_LIGAND,
                        "kind": "strategy",
                        "mode": "narrow",
                        "claim": "活性由某个骨架锚在特定残基上决定，结构不是必需的。",
                        "test": "去掉该锚定基团的类似物测一批，看活性掉多少。",
                        "expected": "活性下降 ≥10 倍，且对映体里只有一个有活性。",
                    }
                )
            else:
                expected = "≥1 个实测 <10 μM"
                if selectivity.get("selective"):
                    expected = (
                        f"借来的配体里 ≥1 个实测 <10 μM；抗靶 Ki 上升 "
                        f"≥{THRESHOLDS['selectivity']:g} 倍、本靶活性不降"
                    )
                drafts.append(
                    {
                        "start": START_BORROW,
                        "kind": "strategy",
                        "mode": "expand",
                        "claim": "邻近受体的配体库里有本靶点的起点。",
                        "test": "按口袋窗口的局部序列相似性借一批配体，测一批。",
                        "expected": expected,
                    }
                )
        return drafts


def domain_tools() -> list[Tool]:
    return [AssessCampaignTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
