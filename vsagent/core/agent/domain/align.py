"""先验清单的取证与叠合：核序列、把配体搬进受体的参考系。

这一层**只做几何与比对，不碰网络**——下载由调用方（`pockets.prior_pockets`）负责，
所以它可被单独验，也不需要给测试留打桩点。

三个动作：

1. **取序列**（`sequences`）——一份坐标里每条聚合物链的一字母序列。
2. **核对并配对**（`best_pair`）——受体的链与候选的链两两比，取同一性最高的一对。
   同一性低于 `IDENTITY_FLOOR`、或覆盖度低于 `COVERAGE_FLOOR`，就是「这不是同一个蛋白」，
   该剔除。两道闸门各管一件事，理由见 `identity` 与 `COVERAGE_FLOOR`。
3. **叠合**（`superpose`）——按那一对链的 CA 原子求变换，把候选搬到受体的参考系里。

`moved_groups` 再把候选的配体坐标用这个变换搬过去。**搬进受体参考系是有意的**：
口袋的中心最终要当盒子用，而盒子是在受体的坐标系里——拿第一份复合物当参考系的话，
算出来的中心对不上受体。

**gemmi 是可选依赖**，所以在函数体里延迟导入，不在模块顶层 import
（这条由 `tests/test_agent.py::check_chemistry_gate` 用 AST 扫全仓守着）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Sequence

from ..tools import ToolError
from .chemistry import CHEM_INSTALL_HINT

__all__ = [
    "COVERAGE_FLOOR",
    "IDENTITY_FLOOR",
    "Verdict",
    "align_candidate",
    "best_pair",
    "identity",
    "moved_groups",
    "same_protein",
    "sequences",
    "superpose",
]

IDENTITY_FLOOR = 0.7
"""同一性低于这个值就当「不是同一个蛋白」，剔除。

分母取**较短的那条链**（见 `identity`）。这里曾经取较长的那条，理由是
「候选多出一大截时要看得出来」——但那个判断该由**覆盖度**回答，不该由同一性回答。
按长的那条算，208 aa 的表达构建体对 394 aa 的全长链会得到 0.528，
明明是 208 个残基一个不差，却被判成「另一个蛋白」（实测踩过：9QTN 对 8V8A）。

阈值 0.7 的来处是实测的两个点，不是拍的：同一份结构的受体与候选是 1.000，
而 EGFR（1M17）对 ABL（1IEP）这两条同科不同蛋白的链是 0.356。中间留了很宽的余量，
所以同源突变体（同一性通常在 0.95 以上）不会被误杀。
"""

COVERAGE_FLOOR = 0.5
"""覆盖度低于这个值也剔除。

**同一性只回答「比对区里像不像」，不回答「比上了多少」**。只覆盖两成的片段
哪怕 100% 相同，也不足以拿它的共晶配体当这个受体的口袋先验——配体完全可能
落在那没比上的八成上。所以两道闸门必须同时过：同一性挡住另一个蛋白，
覆盖度挡住「同一个蛋白的一小段」。

下限取 0.5 而不是 0.7：激酶域、ECD 这类构建体常常只有全长的一半上下，
真实场景里 208/394 = 0.53 就是应当放行的形状。
"""


@dataclasses.dataclass(frozen=True)
class Verdict:
    """一条候选的取证结论。**通过与否都要带原因**，否则报出来也没法查。"""

    pdb_id: str
    state: str
    """`"ok"` / `"missing"` / `"unreachable"` / `"mismatch"` / `"no_polymer"`。

    `missing` 与 `unreachable` 分开是有意的：前者是「这个编号不存在」（换个编号就好），
    后者是「没查成」（稍后重试可能就好），对用户的下一步动作相反。
    """

    reason: str
    pair: str = ""
    """配对的形式，形如 `A→A`。没走到配对这一步时留空。"""

    identity: float | None = None
    coverage: float | None = None
    """比对覆盖了**较短那条**的多少，取值 0–1。

    与 `identity` 并列输出而不是折成一个数：208/394 的构建体是「同一性 1.00、
    覆盖 0.53」，读者要自己判断这个覆盖够不够，工具不替他判断。
    """

    rmsd: float | None = None
    ligands: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _gemmi() -> Any:
    """把 gemmi 取进来。缺依赖时给一条**能照着做**的错。

    刻意不用 `chemistry.require_chem()`：那一个查的是整张化学依赖表，
    本机只缺 rdkit 也会被它拦下，而这一步只要 gemmi。
    """
    try:
        import gemmi
    except ModuleNotFoundError as error:
        raise ToolError(
            f"叠合这一步要 gemmi，本机没有它（{error}）。"
            f"装上它：{CHEM_INSTALL_HINT}，装完重启会话即可。"
        ) from error
    return gemmi


def _read(text: str) -> Any:
    """把坐标文本读成 gemmi 的结构。读不出来时给一条说清是哪一份的错。"""
    gemmi = _gemmi()
    try:
        structure = gemmi.read_pdb_string(text)
    except Exception as error:  # gemmi 读不了时抛的类型不固定，一律转成可转述的一句
        raise ToolError(f"这份坐标读不出来（{type(error).__name__}：{error}）") from error
    structure.setup_entities()
    return structure


def sequences(text: str) -> dict[str, str]:
    """一份坐标里每条**聚合物**链的一字母序列，键是链编号。

    只留第一个模型：多模型文件（NMR）里各模型的序列是一样的，收进来只是重复。
    非聚合物链（水、离子、配体）没有序列，不收。
    """
    gemmi = _gemmi()
    structure = _read(text)
    out: dict[str, str] = {}
    for chain in structure[0]:
        polymer = chain.get_polymer()
        if not len(polymer):
            continue
        letters = gemmi.one_letter_code([residue.name for residue in polymer])
        if letters:
            out[chain.name] = letters
    return out


def identity(left: str, right: str) -> tuple[float, float]:
    """两条序列的 (同一性, 覆盖度)。

    同一性 = 比对上的相同残基 / **较短那条**的长度，回答「比对区里像不像」；
    覆盖度 = 较短 / 较长，回答「比上了多少」。两个数要一起看：

    | 情形 | 同一性 | 覆盖度 |
    | --- | --- | --- |
    | 同一个蛋白 | 1.00 | 1.00 |
    | 截短构建体（208 对 394） | 1.00 | 0.53 |
    | 同家族的另一条链 | 0.36 | 1.00 |
    | 只比上一小段（40 对 274） | 1.00 | 0.15 |

    只报一个数的话，第一行与第二行分不开（这正是把正确结论改错的那次事故），
    第二行与第四行也分不开。分母用 `min()` 还有一个好处：等长序列的结果
    与旧实现（分母取 `max()`）**逐位相同**，换口径不会动到已经验过的那些样本。
    """
    if not left or not right:
        return 0.0, 0.0
    gemmi = _gemmi()
    result = gemmi.align_string_sequences(list(left), list(right), [])
    matches = sum(1 for char in result.match_string if char == "|")
    short, long = sorted((len(left), len(right)))
    return matches / short, short / long


def same_protein(
    identity_value: float,
    coverage: float,
    *,
    floor: float = IDENTITY_FLOOR,
    coverage_floor: float = COVERAGE_FLOOR,
) -> bool:
    """两道闸门都过才算同一个蛋白。判据只有这一处，调用方不要自己写比较。"""
    return identity_value >= floor and coverage >= coverage_floor


def best_pair(
    receptor: dict[str, str], candidate: dict[str, str]
) -> tuple[str, str, float, float] | None:
    """两边的链两两比，取同一性最高的一对，返回 (受体链, 候选链, 同一性, 覆盖度)。

    任一边没有聚合物链时返回 None。

    **配成对的链要报出来**：受体是多聚体、候选是复合物时，「拿哪条链比的」
    决定了这个结论能不能信——不报出来，读者无从复核。

    排序按 (同一性, 覆盖度) 字典序：同一性打平时取覆盖更全的那一对。
    这在候选是复合物时很要紧——里面可能既有受体的完整同源链，又有一小段
    恰好也 100% 相同的肽；只按同一性排会留下那段肽，然后拿它的配体去圈口袋。
    """
    best: tuple[str, str, float, float] | None = None
    for receptor_chain, receptor_seq in receptor.items():
        for candidate_chain, candidate_seq in candidate.items():
            score, coverage = identity(receptor_seq, candidate_seq)
            if best is None or (score, coverage) > (best[2], best[3]):
                best = (receptor_chain, candidate_chain, score, coverage)
    return best


def superpose(
    receptor_text: str,
    candidate_text: str,
    receptor_chain: str,
    candidate_chain: str,
) -> tuple[Any, float]:
    """按两条链的 CA 原子求变换。返回 (变换, rmsd)。

    变换的方向是**把候选搬到受体上**（gemmi 的约定：第二个参数往第一个上搬），
    所以拿它去乘候选的配体坐标，得到的就是受体参考系里的坐标。
    """
    gemmi = _gemmi()
    fixed = _read(receptor_text)[0][receptor_chain].get_polymer()
    moving = _read(candidate_text)[0][candidate_chain].get_polymer()
    result = gemmi.calculate_superposition(
        fixed, moving, gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP
    )
    return result.transform, float(result.rmsd)


def align_candidate(
    receptor_text: str,
    candidate_text: str,
    pdb_id: str,
    *,
    floor: float = IDENTITY_FLOOR,
    coverage_floor: float = COVERAGE_FLOOR,
) -> tuple[Verdict, Any | None]:
    """核对一份候选是不是这个受体，并给出叠合变换。返回 (结论, 变换或 None)。

    **只有核对通过才给变换。** 不通过时返回 None 而不是一个「差不多能用」的变换：
    变换在任何一对链之间都算得出来，算得出来不代表那两个东西是一回事。
    """
    try:
        receptor_seqs = sequences(receptor_text)
        candidate_seqs = sequences(candidate_text)
    except ToolError as error:
        return Verdict(pdb_id, "no_polymer", str(error)), None

    if not receptor_seqs:
        return Verdict(pdb_id, "no_polymer", "受体这一侧读不出聚合物链，无从比对"), None
    if not candidate_seqs:
        return Verdict(
            pdb_id, "no_polymer", "这份坐标里没有聚合物链（只有配体或水）"
        ), None

    pair = best_pair(receptor_seqs, candidate_seqs)
    assert pair is not None  # 两边都非空，best_pair 必有结果
    receptor_chain, candidate_chain, score, coverage = pair
    label = f"{receptor_chain}→{candidate_chain}"
    short, long = sorted(
        (len(receptor_seqs[receptor_chain]), len(candidate_seqs[candidate_chain]))
    )
    span = f"{short}/{long} 个残基（{coverage:.2f}）"

    if not same_protein(score, coverage, floor=floor, coverage_floor=coverage_floor):
        # 两道闸门分开说。混成一句「对不上」的话，模型会以为是同一性不够，
        # 转头去找「更完整」的编号——而这次的问题恰恰是构建体本来就该短。
        if score < floor:
            detail = (
                f"链序列与受体对不上：最高同一性只有 {score:.3f}（{label}），"
                f"低于门槛 {floor:.2f}——这一份多半是另一个蛋白"
            )
        else:
            detail = (
                f"比对区里一模一样（同一性 {score:.3f}，{label}），但只覆盖 {span}，"
                f"低于覆盖度下限 {coverage_floor:.2f}——比对上的只是这个蛋白的一小段，"
                "它的配体不能代表受体口袋"
            )
        return (
            Verdict(
                pdb_id,
                "mismatch",
                detail,
                pair=label,
                identity=score,
                coverage=coverage,
            ),
            None,
        )

    try:
        transform, rmsd = superpose(
            receptor_text, candidate_text, receptor_chain, candidate_chain
        )
    except ToolError as error:
        return (
            Verdict(
                pdb_id,
                "mismatch",
                f"叠合没成：{error}",
                pair=label,
                identity=score,
                coverage=coverage,
            ),
            None,
        )

    return (
        Verdict(
            pdb_id,
            "ok",
            f"同一性 {score:.3f}（{label}），覆盖 {span}，叠合 rmsd {rmsd:.3f} 埃",
            pair=label,
            identity=score,
            coverage=coverage,
            rmsd=rmsd,
        ),
        transform,
    )


def moved_groups(groups: Iterable[Any], transform: Any) -> tuple[Any, ...]:
    """把配体组的坐标搬进参考系，返回新的组。

    只重建原子与组，不动别的字段——`HeteroGroup` 与 `AtomRecord` 都是 frozen 的，
    所以这里造新的，不改原来的那一份。
    """
    gemmi = _gemmi()
    out: list[Any] = []
    for group in groups:
        atoms = []
        for atom in group.atoms:
            point = transform.apply(gemmi.Position(atom.x, atom.y, atom.z))
            atoms.append(dataclasses.replace(atom, x=point[0], y=point[1], z=point[2]))
        out.append(dataclasses.replace(group, atoms=tuple(atoms)))
    return tuple(out)


def describe(verdicts: Sequence[Verdict], limit: int = 20) -> list[str]:
    """把取证结论排成人看的几行。**缩进已经排好**，调用方直接铺进报告即可。

    超过 `limit` 条时截断，并写明怎么取全——不写的话读者会以为清单就这么长。
    """
    lines = [f"  先验清单取证：共 {len(verdicts)} 个编号。"]
    for verdict in verdicts[:limit]:
        mark = "通过" if verdict.ok else "剔除"
        lines.append(f"    {mark} {verdict.pdb_id}：{verdict.reason}")
    if len(verdicts) > limit:
        lines.append(
            f"    （只显示前 {limit} 条，另有 {len(verdicts) - limit} 条没显示——"
            "把清单分成几段再各调一次，可以看到全部。）"
        )
    return lines
