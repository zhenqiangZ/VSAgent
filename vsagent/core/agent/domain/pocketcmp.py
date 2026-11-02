"""口袋之间的比较：同一靶点里比差异（选择性），不同靶点之间比相似（借配体）。

两个工具共用一套序列对齐。对齐用的是 Needleman–Wunsch 的线性罚分版本，
没有引入比对库：这里只需要「第 i 个残基对应哪一个」，不需要打分矩阵与统计显著性，
而多一个依赖就意味着多一份装不上的可能。

**编号一律先换算到各自序列的下标再比。** 结构编号与 UniProt 编号常常错开
（1M17 比 UniProt 少 24 个残基），直接拿编号当位置比，会把两个不相干的残基
配成一对，而结果看上去完全正常。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..ledger import ArtifactHandle, LedgerError, ReceptorHandle, SessionLedger, expect_handle
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import chembl, export, pdbtext, pockets, products, uniprot

__all__ = ["ComparePocketsTool", "SimilarPocketsTool", "domain_tools"]

COMPARE_RADIUS = 8.0
"""比对只报口袋中心这么远以内的残基。更远的残基不影响配体怎么摆，列进来只是噪音。"""

MATCH, MISMATCH, GAP = 2, -1, -2
"""对齐罚分。只为把同一家族的残基对上，不做进化距离估计。"""

WINDOW = 4
"""局部窗口在两端各多取几个残基。只比中心那几个残基时，一个插入就会把整段错开。"""

MAX_CANDIDATES = 8
"""候选上限。每个候选要取一次 UniProt 条目与一次 ChEMBL 计数，多了等不起。"""

ACTIVES_FLOOR = 7.0
"""数活性条数用的 pChEMBL 下限，与 `assess_campaign` 同一口径。"""

MIN_WINDOW_IDENTITY = 0.40
"""窗口同一性低于它就不建议借配体。不同家族之间随便取一段也能撞到 20% 上下，
按那个数去借，借来的是别人的化学型。"""

THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    # 常见修饰残基按母体算：MSE 是硒代甲硫氨酸，SEP 是磷酸化丝氨酸。
    "MSE": "M", "SEP": "S", "TPO": "T", "PTR": "Y", "CSO": "C",
}

RESIDUE_CLASS = {
    "ALA": "疏水", "VAL": "疏水", "ILE": "疏水", "LEU": "疏水", "MET": "疏水",
    "PHE": "芳香", "TYR": "芳香", "TRP": "芳香", "HIS": "芳香",
    "SER": "极性", "THR": "极性", "ASN": "极性", "GLN": "极性", "CYS": "极性",
    "ASP": "酸性", "GLU": "酸性",
    "LYS": "碱性", "ARG": "碱性",
    "GLY": "无侧链", "PRO": "环状",
}

SIDE_CHAIN_ATOMS = {
    "GLY": 0, "ALA": 1, "SER": 2, "CYS": 2, "THR": 3, "VAL": 3, "PRO": 3,
    "ASN": 4, "ASP": 4, "LEU": 4, "ILE": 4, "MET": 4, "GLN": 5, "GLU": 5,
    "LYS": 5, "HIS": 6, "PHE": 7, "TYR": 8, "ARG": 7, "TRP": 10,
}


# ---------------------------------------------------------------------------
# 序列
# ---------------------------------------------------------------------------
def _chain_residues(doc: pdbtext.PdbDocument) -> dict[str, list[tuple[str, str, str]]]:
    """链 → `[(序号, 插入码, 残基名)]`，按坐标里第一次出现的顺序。"""
    seen: dict[str, list[tuple[str, str, str]]] = {}
    keys: dict[str, set[tuple[str, str]]] = {}
    for atom in doc.atoms:
        if atom.is_hetero:
            continue
        chain = atom.chain
        pair = (atom.number, atom.icode)
        bucket = keys.setdefault(chain, set())
        if pair in bucket:
            continue
        bucket.add(pair)
        seen.setdefault(chain, []).append((atom.number, atom.icode, atom.resname))
    return seen


def _sequence(entries: Sequence[tuple[str, str, str]]) -> str:
    return "".join(THREE_TO_ONE.get(name.upper(), "X") for _number, _icode, name in entries)


def _align_pairs(query: str, target: str) -> list[tuple[int, int]]:
    """全局对齐，返回两边都非空位的下标对。"""
    n, m = len(query), len(target)
    if not n or not m:
        return []
    score = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = score[i - 1][0] + GAP
    for j in range(1, m + 1):
        score[0][j] = score[0][j - 1] + GAP
    for i in range(1, n + 1):
        left = query[i - 1]
        row, prev = score[i], score[i - 1]
        for j in range(1, m + 1):
            diagonal = prev[j - 1] + (MATCH if left == target[j - 1] else MISMATCH)
            up = prev[j] + GAP
            back = row[j - 1] + GAP
            row[j] = diagonal if diagonal >= up and diagonal >= back else (up if up >= back else back)
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i and j:
        step = MATCH if query[i - 1] == target[j - 1] else MISMATCH
        if score[i][j] == score[i - 1][j - 1] + step:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif score[i][j] == score[i - 1][j] + GAP:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _identity(pairs: Sequence[tuple[int, int]], query: str, target: str) -> float:
    if not pairs:
        return 0.0
    same = sum(1 for left, right in pairs if query[left] == target[right])
    return same / len(pairs)


def _best_window(sequence: str, window: str) -> tuple[int, float]:
    """在一段序列里找与 `window` 最像的一处，返回 `(起始下标, 同一性)`。"""
    width = len(window)
    if width == 0 or len(sequence) < width:
        return -1, 0.0
    best_at, best_score = -1, -1.0
    for start in range(len(sequence) - width + 1):
        same = sum(1 for offset in range(width) if sequence[start + offset] == window[offset])
        ratio = same / width
        if ratio > best_score:
            best_at, best_score = start, ratio
    return best_at, best_score


# ---------------------------------------------------------------------------
# 残基差异
# ---------------------------------------------------------------------------
def _difference_note(target_name: str, anti_name: str) -> str:
    """两个残基的差别，用能直接判断位阻与相互作用的话写。"""
    if target_name == anti_name:
        return "相同"
    target_atoms = SIDE_CHAIN_ATOMS.get(target_name, 0)
    anti_atoms = SIDE_CHAIN_ATOMS.get(anti_name, 0)
    notes: list[str] = []
    delta = target_atoms - anti_atoms
    if delta >= 2:
        notes.append(f"抗靶小 {delta} 个重原子")
    elif delta <= -2:
        notes.append(f"抗靶大 {-delta} 个重原子")
    target_class = RESIDUE_CLASS.get(target_name, "未知")
    anti_class = RESIDUE_CLASS.get(anti_name, "未知")
    if target_class != anti_class:
        notes.append(f"{target_class}→{anti_class}")
    if not notes:
        notes.append("侧链大小与性质接近")
    return "、".join(notes)


def _residue_distance(
    doc: pdbtext.PdbDocument, key: pdbtext.ResidueKey, center: Sequence[float]
) -> float:
    points = [atom.coord for atom in doc.atoms if atom.key == key and pdbtext.is_heavy(atom)]
    if not points:
        return float("inf")
    return min(
        ((point[0] - center[0]) ** 2 + (point[1] - center[1]) ** 2 + (point[2] - center[2]) ** 2) ** 0.5
        for point in points
    )


def _coordinate(ledger: SessionLedger, raw: str, parameter: str) -> tuple[Any, Path]:
    """按 handle 取受体坐标。拿错了给一句说清「手上那个是什么」的话。"""
    entry = ledger.get(raw)
    expect_handle(entry, ReceptorHandle, parameter)
    path = export._receptor_coordinate(entry)
    if path is None:
        raise ToolError(export._no_coordinate_note(entry))
    return entry, path


# ---------------------------------------------------------------------------
# compare_pockets
# ---------------------------------------------------------------------------
class ComparePocketsTool(Tool):
    """同一处口袋在两个受体里的逐残基差异。"""

    spec = ToolSpec(
        name="compare_pockets",
        description=(
            "把本靶的口袋与抗靶的受体逐残基比一遍，只报口袋中心 8 埃以内的位置。"
            "两条序列先做全局对齐，再按对齐结果配对，所以两边的编号不一致也能对上。\n"
            "每条差异写明抗靶那边是大了还是小了、性质变了没有——"
            "「抗靶在该处更小，加大取代基会撞上」这类假设就从这个结果来。\n"
            "`pocket` 给 `define_pocket` 的产物；也可以直接用 `residues` 报残基（`A:745,A:788`）。\n"
            "**两个靶点的对接分数不能互相比较**：蛋白不同，分数没有共同零点；"
            "选择性比只能从实测活性算。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "本靶的受体 handle"},
                "anti": {"type": "string", "description": "抗靶的受体 handle"},
                "pocket": {"type": "string", "description": "口袋 handle；与 residues 二选一"},
                "residues": {
                    "type": "string",
                    "description": "本靶的残基清单，如 `A:745,A:788`；与 pocket 二选一",
                },
                "radius": {
                    "type": "number",
                    "description": f"只比口袋中心这么远以内的残基（默认 {COMPARE_RADIUS:g}）",
                },
            },
            "required": ["target", "anti"],
            "additionalProperties": False,
        },
    )
    primary_argument = "target"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：compare_pockets 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            radius = _number(arguments.get("radius"), COMPARE_RADIUS, "radius")
            target_entry, target_path = _coordinate(
                ledger, str(arguments.get("target") or ""), "target"
            )
            anti_entry, anti_path = _coordinate(ledger, str(arguments.get("anti") or ""), "anti")
            target_doc = pdbtext.parse(target_path.read_text(encoding="utf-8", errors="replace"))
            anti_doc = pdbtext.parse(anti_path.read_text(encoding="utf-8", errors="replace"))
            keys, center, source = self._pocket_side(ledger, arguments, target_doc)
        except (ToolError, OSError, ValueError) as error:
            return ToolResult(output=str(error), failed=True)
        except LedgerError as error:
            return ToolResult(output=str(error), failed=True)

        inside = (
            list(keys)
            if center is None
            else [key for key in keys if _residue_distance(target_doc, key, center) <= radius]
        )
        if not inside:
            return ToolResult(
                output=(
                    f"{source} 的这些残基离口袋中心都超过 {radius:g} 埃，没有可比的位置。"
                    "换一处口袋，或者把 radius 调大。"
                ),
                failed=True,
            )

        target_chains = _chain_residues(target_doc)
        chain = _majority_chain(inside)
        if chain not in target_chains:
            return ToolResult(output=f"本靶里没有 {chain} 链的聚合物残基。", failed=True)
        query_entries = target_chains[chain]
        query_seq = _sequence(query_entries)
        position = {(number, icode): index for index, (number, icode, _name) in enumerate(query_entries)}

        anti_chains = _chain_residues(anti_doc)
        if not anti_chains:
            return ToolResult(output=f"{anti_entry.handle} 里没有聚合物链。", failed=True)
        # 选链按「同一性 × 覆盖」：只看同一性的话，一条 30 个残基的短链（纳米抗体、
        # 肽段）能以 70% 同一性压过 278 个残基里对上 246 个的真同源链。
        best_chain, best_pairs, best_identity, best_coverage = "", [], 0.0, 0.0
        best_score = -1.0
        for name, entries in anti_chains.items():
            other_seq = _sequence(entries)
            pairs = _align_pairs(query_seq, other_seq)
            identity = _identity(pairs, query_seq, other_seq)
            coverage = len(pairs) / len(query_seq) if query_seq else 0.0
            score = identity * coverage
            if score > best_score:
                best_chain, best_pairs = name, pairs
                best_identity, best_coverage, best_score = identity, coverage, score
        mapping = dict(best_pairs)
        anti_entries = anti_chains[best_chain]

        rows: list[tuple[float, str, str, str, str]] = []
        same = 0
        for key in inside:
            index = position.get((key.number, key.icode))
            name = _residue_name(target_doc, key)
            distance = 0.0 if center is None else _residue_distance(target_doc, key, center)
            if index is None or index not in mapping:
                rows.append((distance, str(key), name, "缺位", "抗靶在这一处没有对应残基"))
                continue
            anti_name = anti_entries[mapping[index]][2]
            if anti_name == name:
                same += 1
                continue
            rows.append(
                (distance, str(key), name, anti_name, _difference_note(name, anti_name))
            )

        rows.sort(key=lambda item: item[0])
        different = sum(1 for row in rows if row[3] != "缺位")
        missing = len(rows) - different
        # 缺位也算差异：抗靶在这一处根本没有对应残基，位阻的结论与「换了一个更小的残基」同类。
        share = (different + missing) / len(inside)

        lines = [
            f"口袋比对：本靶 {target_entry.handle} 的 {chain} 链 {len(inside)} 个残基"
            f"（{radius:g} 埃内），抗靶 {anti_entry.handle} 取 {best_chain} 链"
            f"（序列同一性 {best_identity:.0%}，覆盖 {len(best_pairs)}/{len(query_seq)}）",
            f"来源：{source}",
            "",
            f"差异 {different} 个、相同 {same} 个、抗靶缺位 {missing} 个"
            f"（差异占比 {share:.0%}）",
        ]
        if rows:
            lines.append("")
            lines.append(f"{'位置':<14}{'本靶':<8}{'抗靶':<8}差别")
            for _distance, label, name, anti_name, note in rows[:30]:
                lines.append(f"{label:<14}{name:<8}{anti_name:<8}{note}")
            if len(rows) > 30:
                lines.append(f"……另有 {len(rows) - 30} 个差异未显示")
        lines.append("")
        if best_identity < 0.35 or best_coverage < 0.5:
            lines.append(
                f"序列同一性 {best_identity:.0%}、覆盖 {best_coverage:.0%}：这两条链多半不是同源蛋白，"
                "上面的配对不可用。抗靶要选同一家族、同一结合位点的受体；换个抗靶或换一条链再比。"
            )
        elif share < 0.10:
            lines.append(
                "差异占比不到 10%：这一处口袋在两个靶点里几乎一样，位阻不成立。"
                "选择性只能从实测活性算，不要用对接分数推断。"
            )
        else:
            lines.append(
                "用法：避开差异位点可以做选择性；刻意利用同一处差异可以做双抑制。"
                "两个靶点的对接分数不能互相比较，选择性比只能从实测活性算。"
            )
        return ToolResult(
            output="\n".join(lines),
            meta={
                "target": str(target_entry.handle),
                "anti": str(anti_entry.handle),
                "chain": chain,
                "anti_chain": best_chain,
                "identity": round(best_identity, 4),
                "coverage": round(best_coverage, 4),
                "residues": len(inside),
                "different": different,
                "same": same,
                "missing": missing,
                "share": round(share, 4),
                "source": source,
            },
        )

    def _pocket_side(
        self,
        ledger: SessionLedger,
        arguments: dict[str, Any],
        doc: pdbtext.PdbDocument,
    ) -> tuple[list[pdbtext.ResidueKey], tuple[float, float, float] | None, str]:
        """口袋这一侧：从 handle 读，或者从残基清单算。"""
        raw_pocket = str(arguments.get("pocket") or "").strip()
        raw_residues = str(arguments.get("residues") or "").strip()
        if raw_pocket and raw_residues:
            raise ToolError("pocket 与 residues 只能给一个：两处来源不一致时无法判断信哪个。")
        if raw_pocket:
            entry = ledger.get(raw_pocket)
            expect_handle(entry, ArtifactHandle, "pocket")
            path = Path(str(entry.path or "")) / pockets.POCKET_FILE
            if not path.is_file():
                raise ToolError(
                    f"{entry.handle} 不是单处口袋的产物（找不到 {pockets.POCKET_FILE}）。"
                    "口袋集要先选一处：define_pocket 产出的才是单处口袋。"
                )
            payload = products.read_json(path)
            residues = payload.get("residues") or []
            keys = [
                pdbtext.ResidueKey(
                    str(item.get("chain") or ""),
                    str(item.get("resseq") or ""),
                    str(item.get("icode") or ""),
                )
                for item in residues
                if isinstance(item, dict)
            ]
            center = payload.get("center") or []
            if len(center) != 3:
                raise ToolError(f"{entry.handle} 的口袋中心读不出来。")
            return keys, (float(center[0]), float(center[1]), float(center[2])), f"口袋 {entry.handle}"
        if raw_residues:
            tokens = [token.strip() for token in raw_residues.replace(";", ",").split(",") if token.strip()]
            keys, problems = pockets._parse_residue_tokens(doc, tokens)
            if problems:
                raise ToolError("这些残基对不上：\n  · " + "\n  · ".join(problems))
            # 残基清单本身就是口袋，不再按中心距离筛一遍：报上来的几个残基可能横跨
            # 十几埃（比如两端各取一个），按质心筛会把它们全部排除掉。
            return keys, None, f"残基清单（{len(keys)} 个）"
        raise ToolError("要给一处口袋：`pocket` 传 define_pocket 的产物，或 `residues` 直接报残基。")


def _residue_name(doc: pdbtext.PdbDocument, key: pdbtext.ResidueKey) -> str:
    for atom in doc.atoms:
        if atom.key == key:
            return atom.resname
    return ""


def _majority_chain(keys: Sequence[pdbtext.ResidueKey]) -> str:
    counts: dict[str, int] = {}
    for key in keys:
        counts[key.chain] = counts.get(key.chain, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0] if counts else ""


def _number(raw: Any, default: float, parameter: str) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"参数 {parameter} 要一个数，收到 {raw!r}") from error
    if value <= 0:
        raise ValueError(f"参数 {parameter} 应当大于 0，收到 {value:g}")
    return value


# ---------------------------------------------------------------------------
# similar_pockets
# ---------------------------------------------------------------------------
class SimilarPocketsTool(Tool):
    """按口袋窗口的局部序列相似性给候选受体排序，并给出各自的活性条数。"""

    spec = ToolSpec(
        name="similar_pockets",
        description=(
            "本靶点没有可用起点时，去邻近受体借配体：按**口袋窗口的局部序列相似性**"
            "给候选受体排序，同时给出每个候选在 ChEMBL 里 pChEMBL ≥7 的活性条数。\n"
            "`candidates` 是候选的 UniProt 编号（逗号分隔，至多 8 个），"
            "通常先用 `search_targets` 按蛋白名或基因名检索同家族成员。\n"
            "窗口按 **UniProt 编号**取：`residues` 直接写编号（`741,745,788`），"
            "给 `pocket` 时先把结构编号对齐到 UniProt 序列再取窗口。\n"
            "挑法：相似度最高且活性 ≥20 条的那一个；都不到 20 条时取活性最多的，"
            "并在结论里写明参考集不足。借来的配体排序误差比同受体大，"
            "下一轮只挑 20 个左右做实测，不直接大规模对接。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "accession": {"type": "string", "description": "本靶的 UniProt 编号"},
                "candidates": {
                    "type": "string",
                    "description": "候选受体的 UniProt 编号，逗号分隔，至多 8 个",
                },
                "residues": {
                    "type": "string",
                    "description": "口袋残基的 UniProt 编号，如 `741,745,788`",
                },
                "pocket": {
                    "type": "string",
                    "description": "口袋 handle；结构编号会先对齐到 UniProt 序列",
                },
                "window": {
                    "type": "integer",
                    "description": f"窗口在两端各多取几个残基（默认 {WINDOW}）",
                },
                "actives": {
                    "type": "boolean",
                    "description": "是否查每个候选的活性条数（默认查；关掉会快很多）",
                },
            },
            "required": ["accession", "candidates"],
            "additionalProperties": False,
        },
    )
    primary_argument = "accession"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：similar_pockets 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            code = uniprot.normalize_accession(str(arguments.get("accession") or ""))
            candidates = _candidates(arguments.get("candidates"))
            if not candidates:
                return ToolResult(
                    output="candidates 是空的：先用 search_targets 找同家族成员，再把这几个编号传进来。",
                    failed=True,
                )
            window = int(_number(arguments.get("window"), WINDOW, "window"))
            want_actives = arguments.get("actives")
            want_actives = True if want_actives is None else bool(want_actives)
            result = await asyncio.to_thread(
                self._rank, ledger, arguments, code, candidates, window, want_actives
            )
        except (ToolError, ValueError, OSError) as error:
            return ToolResult(output=str(error), failed=True)
        except LedgerError as error:
            return ToolResult(output=str(error), failed=True)
        return result

    def _rank(
        self,
        ledger: SessionLedger,
        arguments: dict[str, Any],
        code: str,
        candidates: list[str],
        window: int,
        want_actives: bool,
    ) -> ToolResult:
        record = _record(code)
        sequence = str((record.get("sequence") or {}).get("value") or "")
        if not sequence:
            return ToolResult(output=f"UniProt 里 {code} 没有序列。", failed=True)

        start, end, source = self._window(ledger, arguments, code, sequence, window)
        piece = sequence[start:end]
        lines = [f"窗口：{code} 第 {start + 1}–{end} 位（UniProt 编号），{len(piece)} 个残基", f"来源：{source}", ""]

        rows: list[tuple[float, str, int | None, str, int]] = []
        skipped: list[str] = []
        for candidate in candidates:
            if candidate == code:
                continue
            try:
                other = _record(candidate)
            except ToolError as error:
                skipped.append(f"{candidate}：{error}")
                continue
            other_seq = str((other.get("sequence") or {}).get("value") or "")
            at, identity = _best_window(other_seq, piece)
            if at < 0:
                skipped.append(f"{candidate}：序列比窗口还短")
                continue
            name = str((other.get("proteinDescription") or {}).get("recommendedName", {}).get("fullName", {}).get("value") or "")
            count = _active_count(candidate) if want_actives else None
            rows.append((identity, candidate, count, name, at))

        if not rows:
            lines.append("候选一个都没有取到。")
            if skipped:
                lines.extend(f"  · {item}" for item in skipped)
            return ToolResult(output="\n".join(lines), failed=True)

        rows.sort(key=lambda item: (-item[0], -(item[2] if item[2] is not None else -1)))
        lines.append(f"{'编号':<12}{'窗口同一性':<12}{'活性条数':<10}{'位置':<8}蛋白名")
        for identity, candidate, count, name, at in rows:
            shown = "未查" if count is None else ("没有记录" if count == 0 else str(count))
            lines.append(
                f"{candidate:<12}{identity:<12.0%}{shown:<10}{at + 1:<8}{name or '（未命名）'}"
            )
        if skipped:
            lines.append("")
            lines.extend(f"跳过：{item}" for item in skipped)

        usable = [
            row for row in rows if row[0] >= MIN_WINDOW_IDENTITY and row[2] is not None and row[2] >= 20
        ]
        chosen = usable[0] if usable else max(rows, key=lambda item: (item[2] or 0, item[0]))
        lines.append("")
        if chosen[0] < MIN_WINDOW_IDENTITY:
            lines.append(
                f"没有相近的候选：最高窗口同一性只有 {chosen[0]:.0%}（判据 ≥{MIN_WINDOW_IDENTITY:.0%}）。"
                "不要借——不同家族里随便取一段也能撞到 20% 上下，借来的是别人的化学型。"
                "先补候选：用 search_targets 按同一蛋白家族检索，或回到文献里的位点与突变数据。"
            )
            return ToolResult(
                output="\n".join(lines),
                meta={
                    "accession": code,
                    "window": [start + 1, end],
                    "source": source,
                    "candidates": [
                        {"accession": item[1], "identity": round(item[0], 4), "actives": item[2]}
                        for item in rows
                    ],
                    "chosen": "",
                },
            )
        lines.append(
            f"建议：取 {chosen[1]}（窗口同一性 {chosen[0]:.0%}、"
            f"{'活性 ' + str(chosen[2]) + ' 条' if chosen[2] is not None else '活性未查'}）。"
        )
        if not usable:
            lines.append("候选里没有一个到 20 条活性：参考集不足，结论里要写明这一条。")
        lines.append(
            f"借它的配体：search_actives(accession=\"{chosen[1]}\")；"
            "之后 build_reference_set → screen_similarity → pick_diverse(limit=20)。"
        )
        return ToolResult(
            output="\n".join(lines),
            meta={
                "accession": code,
                "window": [start + 1, end],
                "source": source,
                "candidates": [
                    {"accession": item[1], "identity": round(item[0], 4), "actives": item[2]}
                    for item in rows
                ],
                "chosen": chosen[1],
            },
        )

    def _window(
        self,
        ledger: SessionLedger,
        arguments: dict[str, Any],
        code: str,
        sequence: str,
        window: int,
    ) -> tuple[int, int, str]:
        """窗口在 UniProt 序列上的起止下标（半开）。"""
        raw_pocket = str(arguments.get("pocket") or "").strip()
        raw_residues = str(arguments.get("residues") or "").strip()
        if raw_pocket and raw_residues:
            raise ToolError("pocket 与 residues 只能给一个。")
        if raw_residues:
            numbers = _residue_numbers(raw_residues)
            start = max(0, min(numbers) - 1 - window)
            end = min(len(sequence), max(numbers) + window)
            return start, end, f"残基清单（UniProt 编号 {min(numbers)}–{max(numbers)}）"
        if raw_pocket:
            entry = ledger.get(raw_pocket)
            expect_handle(entry, ArtifactHandle, "pocket")
            path = Path(str(entry.path or "")) / pockets.POCKET_FILE
            if not path.is_file():
                raise ToolError(f"{entry.handle} 不是单处口袋的产物。")
            payload = products.read_json(path)
            residues = [item for item in (payload.get("residues") or []) if isinstance(item, dict)]
            if not residues:
                raise ToolError(f"{entry.handle} 里没有残基。")
            receptor = next(
                (str(item) for item in entry.parents if item.kind == "receptor"), ""
            )
            if not receptor:
                raise ToolError(
                    f"{entry.handle} 没有登记它来自哪个受体，无法把结构编号换算成 UniProt 编号。"
                    "改用 `residues` 直接给 UniProt 编号。"
                )
            _entry, receptor_path = _coordinate(ledger, receptor, "pocket")
            doc = pdbtext.parse(receptor_path.read_text(encoding="utf-8", errors="replace"))
            chains = _chain_residues(doc)
            chain = _majority_chain(
                [
                    pdbtext.ResidueKey(
                        str(item.get("chain") or ""),
                        str(item.get("resseq") or ""),
                        str(item.get("icode") or ""),
                    )
                    for item in residues
                ]
            )
            if chain not in chains:
                raise ToolError(f"受体里没有 {chain} 链。")
            entries = chains[chain]
            pairs = _align_pairs(_sequence(entries), sequence)
            mapping = dict(pairs)
            positions = [
                mapping[index]
                for index, (number, icode, _name) in enumerate(entries)
                if any(
                    str(item.get("resseq")) == number
                    and str(item.get("icode") or "") == icode
                    and str(item.get("chain") or "") == chain
                    for item in residues
                )
                and index in mapping
            ]
            if not positions:
                raise ToolError(
                    "口袋残基一个都没能对齐到 UniProt 序列上：受体与 UniProt 条目可能不是同一条蛋白。"
                )
            start = max(0, min(positions) - window)
            end = min(len(sequence), max(positions) + 1 + window)
            return start, end, f"口袋 {entry.handle}（结构编号已对齐到 UniProt）"
        raise ToolError("要给口袋范围：`residues` 传 UniProt 编号，或 `pocket` 传口袋产物。")


def _residue_numbers(raw: str) -> list[int]:
    numbers: list[int] = []
    for token in raw.replace(";", ",").split(","):
        text = token.strip()
        if not text:
            continue
        text = text.partition(":")[2] or text
        digits = "".join(character for character in text if character.isdigit())
        if not digits:
            raise ValueError(f"{token!r} 里读不出残基编号。")
        numbers.append(int(digits))
    if not numbers:
        raise ValueError("residues 里没有编号。")
    return numbers


def _candidates(raw: Any) -> list[str]:
    text = str(raw or "").replace(";", ",").replace(" ", ",")
    seen: list[str] = []
    for token in text.split(","):
        code = token.strip().upper()
        if code and code not in seen:
            seen.append(code)
    if len(seen) > MAX_CANDIDATES:
        raise ValueError(
            f"候选 {len(seen)} 个，超过上限 {MAX_CANDIDATES}：每个候选要取一次 UniProt 与一次 ChEMBL，"
            "先按家族或结构相似性缩小到 8 个以内。"
        )
    return seen


def _record(code: str) -> dict[str, Any]:
    """UniProt 条目：缓存优先，没有就联网。失败抛 `ToolError`。"""
    path = uniprot._record_path(code)
    cached = uniprot._read_cached(path)
    if cached is not None:
        return cached[0]
    record = uniprot._fetch_record(code, timeout=60.0)
    uniprot._write_cached(path, record)
    return record


def _active_count(code: str) -> int | None:
    """ChEMBL 里 pChEMBL ≥7 的活性条数。没有单一蛋白靶点时返回 None（没有记录）。"""
    try:
        choice = chembl._resolve_target(code, timeout=chembl.TIMEOUT)
    except ToolError:
        return None
    return chembl._count(
        chembl.ACTIVITY_URL,
        {
            "target_chembl_id": choice.chembl_id,
            "pchembl_value__gte": f"{ACTIVES_FLOOR:g}",
            "standard_type__in": chembl.DEFAULT_MEASURE_TYPES,
            "potential_duplicate": "0",
        },
        timeout=chembl.TIMEOUT,
    )


def domain_tools() -> list[Tool]:
    return [ComparePocketsTool(), SimilarPocketsTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
