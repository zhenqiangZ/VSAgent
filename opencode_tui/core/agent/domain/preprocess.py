"""结构预处理：把一份坐标清洗成「能看」与「能用来找口袋」的形态。

这一层只做两件事，**都不联网、不认化学依赖**：

1. **清洗结构**（`clean_structure`）——选链、选交替构象、去水、去溶剂、
   去氢。输出仍是标准 PDB 文本，没被要求动的行逐字节不动。
2. **筛杂原子组**（`screen_ligands`）——在名字名单之外，再用三条几何判据
   判断一个 `HETATM` 组到底像不像配体：重原子太少、与蛋白共价连接、
   蛋白接触的残基太少。

**为什么要有第 2 步。** 名字名单永远收不全：同一个结晶条件在不同实验室里
可能写成 `SO4` / `PO4` / `ACT`，也可能是某个没见过的商品缓冲液。名单漏一个，
它就变成一个「口袋」出现在页面上，而用户无从判断那是溶剂还是配体。几何判据
补的是这一侧：真正的配体要有足够多的重原子、不是共价挂在蛋白上的修饰基团、
并且在蛋白表面占住一块不止一个残基的位置。

**判据的取舍一律朝「宁可漏不要错杀」让。** 漏掉的后果是多一个看得见的口袋
（用户能发现），错杀的后果是真配体消失、页面上什么都没有（用户发现不了）。
所以阈值都取得很低：4 个重原子、1.9 Å 共价键、3 个接触残基。命中任何一条
才排除，并且把原因逐类报出去——**用户看不到的就是没发生的**。
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from . import pdbtext
from .pdbtext import HeteroGroup, PdbDocument, ResidueKey, is_heavy

__all__ = [
    "COVALENT_CUTOFF",
    "CONTACT_CUTOFF",
    "MIN_CONTACT_RESIDUES",
    "MIN_LIGAND_HEAVY_ATOMS",
    "LigandScreen",
    "PreprocessReport",
    "PreprocessResult",
    "clean_structure",
    "polymer_chains",
    "screen_ligands",
]

MIN_LIGAND_HEAVY_ATOMS = 4
"""一个杂原子组至少要有这么多重原子，才可能是配体（不是溶剂/离子）。

甘油 6 个、乙二醇 4 个、硫酸根 5 个——这条拦不住它们，拦的是甲醇（1）、
乙酸根（4 个但名字名单里）、单个卤素之类。真正的片段筛选配体通常 8 个以上，
取 4 是刻意留出的余量。
"""

COVALENT_CUTOFF = 1.9
"""配体原子与蛋白重原子近于这个距离（埃），就当作共价连接。

共价键长：C–N 1.35–1.5、C–O 1.4、S–C 1.8、P–O 1.6。金属配位键通常 2.0 Å
以上，所以 1.9 不会把金属配位误判成共价键。这条主要拦糖基化（NAG 挂在 ASN
的 ND2 上，1.4 Å）与共价抑制剂。
"""

CONTACT_CUTOFF = 4.5
"""「接触」的距离口径（埃）。与口袋残基的圈定半径一致，两处说的是同一件事。"""

MIN_CONTACT_RESIDUES = 3
"""至少碰到几个蛋白残基才算「占住了一个位置」。

只碰到一两个残基的杂原子组基本是贴在表面的结晶添加剂；真正的配体在口袋里
会被三面以上的残基围住。取 3 而不是更高，是为了不把浅槽里的小片段筛掉。
"""


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LigandScreen:
    """几何筛查的结果。`excluded` 与 `export.LigandFilter` 同形。"""

    kept: tuple[HeteroGroup, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()
    """(残基名, 排除原因)，按组在文件里出现的顺序。"""

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for _name, reason in self.excluded:
            tally[reason] = tally.get(reason, 0) + 1
        return tally

    def describe(self) -> str:
        if not self.excluded:
            return "几何筛查没有排除任何杂原子组"
        parts = "、".join(f"{reason} {count} 个" for reason, count in sorted(self.counts.items()))
        return f"几何筛查排除了 {len(self.excluded)} 个杂原子组（{parts}）"


@dataclass(frozen=True)
class PreprocessReport:
    """清洗结构动了什么。**每一条都要能进报告**，否则用户分不清
    「这份结构本来就这么干净」与「被我们删掉了」。"""

    chains: tuple[str, ...] = ()
    """保留下来的链。"""

    dropped_chains: tuple[tuple[str, int], ...] = ()
    """被删掉的链 → 原子数。"""

    waters: int = 0
    """删掉的水残基个数（不是原子数）。"""

    removed: tuple[tuple[str, str], ...] = ()
    """删掉的非水杂原子组：(残基名, 原因)。"""

    kept_ligands: tuple[str, ...] = ()
    """当作配体保留下来的残基名（去重，按出现顺序）。"""

    altloc: tuple[tuple[str, str], ...] = ()
    """每个带交替构象的残基选了哪个构象：`("A:1", "A")`。"""

    hydrogens: int = 0
    """删掉的氢原子数。"""

    models: int = 0
    """被丢掉的多余模型数（NMR 结构常见）。"""

    @property
    def removed_counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for _name, reason in self.removed:
            tally[reason] = tally.get(reason, 0) + 1
        return tally

    def as_dict(self) -> dict[str, Any]:
        return {
            "chains": list(self.chains),
            "dropped_chains": dict(self.dropped_chains),
            "waters": self.waters,
            "removed": dict(self.removed_counts),
            "kept_ligands": list(self.kept_ligands),
            "altloc": dict(self.altloc),
            "hydrogens": self.hydrogens,
            "models": self.models,
        }

    def describe(self) -> str:
        """一句给人看的话。没有动任何东西时也要明说。"""
        parts: list[str] = []
        if self.chains:
            text = "保留链 " + "".join(self.chains)
            if self.dropped_chains:
                shown = self.dropped_chains[:6]
                text += "（去掉 " + "、".join(
                    f"{chain} {count} 个原子" for chain, count in shown
                )
                if len(self.dropped_chains) > len(shown):
                    text += f" 等 {len(self.dropped_chains)} 条链"
                text += "）"
            parts.append(text)
        if self.waters:
            parts.append(f"去水 {self.waters} 个")
        if self.removed:
            detail = "、".join(
                f"{reason} {count} 个" for reason, count in sorted(self.removed_counts.items())
            )
            parts.append(f"去掉杂原子组 {len(self.removed)} 个（{detail}）")
        if self.kept_ligands:
            shown = self.kept_ligands[:8]
            text = "保留配体 " + "+".join(shown)
            if len(self.kept_ligands) > len(shown):
                text += f" 等 {len(self.kept_ligands)} 个"
            parts.append(text)
        if self.altloc:
            parts.append(f"交替构象选了 {len(self.altloc)} 个残基（每个只留一份坐标）")
        if self.hydrogens:
            parts.append(f"去氢 {self.hydrogens} 个")
        if self.models:
            parts.append(f"只留第一套模型（丢掉 {self.models} 套）")
        return "；".join(parts) if parts else "结构无需清洗"


@dataclass(frozen=True)
class PreprocessResult:
    text: str
    report: PreprocessReport
    kept_ligands: tuple[HeteroGroup, ...] = ()


# ---------------------------------------------------------------------------
# 几何判据
# ---------------------------------------------------------------------------
def polymer_chains(doc: PdbDocument) -> tuple[str, ...]:
    """有 `ATOM` 记录的链，按出现顺序。**`HETATM` 单独成链的不算**——
    那是配体或溶剂自己的链号，不是受体链。"""
    seen: list[str] = []
    for atom in doc.atoms:
        if not atom.is_hetero and atom.chain not in seen:
            seen.append(atom.chain)
    return tuple(seen)


def _protein_atoms(
    doc: PdbDocument, chains: Sequence[str] = ()
) -> tuple[pdbtext.AtomRecord, ...]:
    wanted = set(chains)
    return tuple(
        atom
        for atom in doc.atoms
        if not atom.is_hetero
        and is_heavy(atom)
        and (not wanted or atom.chain in wanted)
    )


def _within(atom: pdbtext.AtomRecord, other: pdbtext.AtomRecord, cutoff: float) -> bool:
    dx = atom.x - other.x
    dy = atom.y - other.y
    dz = atom.z - other.z
    return dx * dx + dy * dy + dz * dz <= cutoff * cutoff


@dataclass(frozen=True)
class _ProteinIndex:
    """蛋白重原子的网格索引。

    每个配体组都要问「附近有哪些蛋白原子」，直接全表扫是 O(组数 × 原子数)：
    十万原子的复合物、几十个杂原子组就是上亿次距离计算，一次导出能卡上几分钟。
    按 `CONTACT_CUTOFF` 分格之后只看相邻 27 格，结果一样、代价降一个量级。
    """

    cells: dict[tuple[int, int, int], tuple[pdbtext.AtomRecord, ...]]
    size: float = CONTACT_CUTOFF

    @classmethod
    def build(
        cls, atoms: Sequence[pdbtext.AtomRecord], size: float = CONTACT_CUTOFF
    ) -> _ProteinIndex:
        buckets: dict[tuple[int, int, int], list[pdbtext.AtomRecord]] = {}
        for atom in atoms:
            buckets.setdefault(_cell_key(atom, size), []).append(atom)
        return cls(
            cells={key: tuple(items) for key, items in buckets.items()}, size=size
        )

    def near(
        self, atom: pdbtext.AtomRecord, cutoff: float
    ) -> Iterator[pdbtext.AtomRecord]:
        x, y, z = _cell_key(atom, self.size)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for other in self.cells.get((x + dx, y + dy, z + dz), ()):
                        if _within(atom, other, cutoff):
                            yield other


def _cell_key(atom: pdbtext.AtomRecord, size: float) -> tuple[int, int, int]:
    return (
        math.floor(atom.x / size),
        math.floor(atom.y / size),
        math.floor(atom.z / size),
    )


def _is_covalent(group: HeteroGroup, protein: _ProteinIndex) -> bool:
    for atom in group.atoms:
        if not is_heavy(atom):
            continue
        for _other in protein.near(atom, COVALENT_CUTOFF):
            return True
    return False


def _contact_residues(
    group: HeteroGroup, protein: _ProteinIndex
) -> set[ResidueKey]:
    found: set[ResidueKey] = set()
    for atom in group.atoms:
        if not is_heavy(atom):
            continue
        for other in protein.near(atom, CONTACT_CUTOFF):
            found.add(other.key)
    return found


def screen_ligands(
    doc: PdbDocument,
    groups: Iterable[HeteroGroup],
    *,
    chains: Sequence[str] = (),
) -> LigandScreen:
    """对名字筛完剩下的组再做一遍几何筛查。

    `chains` 是可选的口袋链：给了就只按这些链的原子判接触与共价连接（清洗
    结构时按选中的链判）；留空时按结构里**全部聚合物链**判。复合物条目里
    链号未必与受体一致，按全部链判才不会把整条配体误判成「接触不足」。
    """
    protein = _ProteinIndex.build(_protein_atoms(doc, chains))
    kept: list[HeteroGroup] = []
    excluded: list[tuple[str, str]] = []

    for group in groups:
        heavy = [atom for atom in group.atoms if is_heavy(atom)]
        if len(heavy) < MIN_LIGAND_HEAVY_ATOMS:
            excluded.append((group.resname, "重原子太少"))
            continue
        if _is_covalent(group, protein):
            excluded.append((group.resname, "与蛋白共价连接"))
            continue
        contacts = _contact_residues(group, protein)
        if len(contacts) < MIN_CONTACT_RESIDUES:
            excluded.append((group.resname, "蛋白接触不足"))
            continue
        kept.append(group)

    return LigandScreen(kept=tuple(kept), excluded=tuple(excluded))


# ---------------------------------------------------------------------------
# 清洗结构
# ---------------------------------------------------------------------------
def clean_structure(
    text: str,
    *,
    chains: Sequence[str] = (),
    modified_names: Iterable[str] = (),
    keep_ligands: bool = True,
) -> PreprocessResult:
    """把一份坐标清洗成可展示的形态。

    顺序是：选链 → 选交替构象 → 筛配体 → 删杂原子 → 去氢。

    **筛配体在删杂原子之前**：先决定哪些 `HETATM` 组值得留，再按残基键精确
    删除其余的。按残基名删会把「同名但不同位置的两个组」一起留下或一起删掉，
    而它们可能一个是真配体、一个是溶剂。

    `keep_ligands=False` 时只留聚合物（结构总览用）；默认留下通过筛查的配体，
    因为共晶配体是这套结构里最有信息量的东西之一，删掉只剩一条空口袋壁。
    """
    doc = pdbtext.parse(text)
    report = PreprocessReport(models=doc.extra_models)

    wanted = tuple(chains) or polymer_chains(doc)
    doc, dropped = pdbtext.keep_chains(doc, wanted)
    report = dataclasses.replace(
        report,
        chains=tuple(wanted),
        dropped_chains=tuple(sorted(dropped.items())),
    )

    altloc_keys = pdbtext.altloc_residues(doc)
    if altloc_keys:
        doc, chosen, _fell_back = pdbtext.select_altloc(doc, "A")
        report = dataclasses.replace(
            report,
            altloc=tuple(
                (str(key), value)
                for key, value in sorted(chosen.items(), key=lambda item: str(item[0]))
            ),
        )

    # 延迟导入：`export.py` 在顶层导入了本模块（它要用 clean_structure），
    # 这里再在顶层导入它就成了循环。函数体里导入时两个模块都已经加载完。
    from . import export as export_module

    modified = {name.upper() for name in modified_names}
    filtered = export_module.ligand_groups(
        doc, modified_names=tuple(modified_names)
    )
    screen = screen_ligands(doc, filtered.kept, chains=wanted)

    keep_keys: set[ResidueKey] = set()
    if keep_ligands:
        keep_keys.update(group.key for group in screen.kept)
    # 修饰残基写着 HETATM 却是聚合物的一部分，**任何情况下都要留**：
    # 删掉它就是给骨架挖掉一个残基，而下游报错会指向别处。
    for atom in doc.atoms:
        if atom.is_hetero and atom.resname.upper() in modified:
            keep_keys.add(atom.key)

    # 水单独计数：`hetero_groups` 本来就把水排掉了，不单独扫一遍会漏掉这一类。
    water_keys = {
        atom.key for atom in doc.atoms if atom.is_hetero and atom.is_water
    }

    reasons: dict[str, str] = {}
    for name, reason in filtered.excluded:
        reasons.setdefault(name.upper(), reason)
    for name, reason in screen.excluded:
        reasons.setdefault(name.upper(), reason)

    removed: list[tuple[str, str]] = []
    doomed: list[int] = []
    for group in pdbtext.hetero_groups(doc, exclude_names=tuple(modified_names)):
        if group.key in keep_keys:
            continue
        removed.append((group.resname, reasons.get(group.resname.upper(), "未保留")))
        doomed.extend(atom.index for atom in group.atoms)
    # 水也在 HETATM 里，但不在 `hetero_groups` 的返回里。
    for atom in doc.atoms:
        if atom.is_hetero and atom.is_water:
            doomed.append(atom.index)
    if doomed:
        doc = doc.drop(doomed)

    hydrogen_indices = [atom.index for atom in doc.atoms if not is_heavy(atom)]
    if hydrogen_indices:
        doc = doc.drop(hydrogen_indices)

    kept_names: list[str] = []
    for group in screen.kept:
        if keep_ligands and group.resname not in kept_names:
            kept_names.append(group.resname)

    report = dataclasses.replace(
        report,
        waters=len(water_keys),
        removed=tuple(removed),
        kept_ligands=tuple(kept_names),
        hydrogens=len(hydrogen_indices),
    )
    return PreprocessResult(text=doc.text, report=report, kept_ligands=screen.kept)
