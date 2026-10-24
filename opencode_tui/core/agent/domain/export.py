"""从坐标算口袋：纯几何，零依赖，不认化学。

这一层回答四个问题，全部只用 `pdbtext.py` 读出来的坐标：

1. **哪些杂原子算配体**（`ligand_groups`）——排掉水、修饰残基、单原子离子与常见缓冲液。
2. **配体是不是同一个口袋**（`cluster_ligands`）——并查集，判据是「任一对原子距离」而不是中心距。
3. **口袋圈住哪些残基**（`pocket_residues`）——配体任一个原子 5 Å 以内的残基，带最短距离。
4. **两处口袋是不是同一处**（`merge_pockets`）——跨复合物合并时用，中心 6 Å 以内算一处。

**为什么是纯 Python。** 本机没装化学依赖（`chemistry.require_chem()` 会拒），
走它就只能靠假替身验流程、验不了真的坐标数学。而这一层的全部输入是「一堆三元组」，
用 `math.dist` 就够了。代价是上万原子几百毫秒到一秒——这是一次导出动作，可接受。

**为什么不算「接触」与「打分」。** `contacts`（逐原子接触对）要真实位姿，
`score` 要对接程序。本轮两样都没有，**字段本身不出现**——写 0 会被当成「算出来是零」。
见 `docs/web-viewer-plan.md` §2.1。

数值一律现算，**绝不接受调用方传进来的距离**：模型心算的 `min_distance: 2.67`
看上去永远很像对的，而页面上几十个这样的值没有一个是真的。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import tempfile
import webbrowser
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...paths import project_root
from ..ledger import (
    ArtifactHandle,
    LigandSetHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_text,
)
from ..tools import Tool, ToolResult, ToolSpec
from . import pdbtext, preprocess, products, structure
from .pdbtext import HeteroGroup, PdbDocument, ResidueKey, hetero_groups, is_heavy

__all__ = [
    "BUFFER_LIKE",
    "CLUSTER_GAP",
    "LigandCluster",
    "MONATOMIC_IONS",
    "PocketResidue",
    "POCKET_RADIUS",
    "MERGE_RADIUS",
    "ExportGeometryError",
    "OpenViewerTool",
    "WriteRunTool",
    "cluster_ligands",
    "contact_counts",
    "domain_tools",
    "is_heavy",
    "ligand_center",
    "ligand_groups",
    "merge_pockets",
    "min_distance",
    "pocket_residues",
    "runs_dir",
]

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------
CLUSTER_GAP = 4.0
"""两个配体之间「任意一对原子」相距 ≤ 这个值就算同一个簇（埃）。

**判据是原子对距离，不是中心距。** 长条配体（伊马替尼两端相距 10 Å 以上）按中心算
会把本该连在一起的两个分子分开；`tools/build_demo_data.py:95` 的注释记过同一件事。
经验值，碰到反例只改这一个常量，不动算法（见方案稿 §7.4）。
"""

POCKET_RADIUS = 5.0
"""口袋残基的圈定半径（埃）。与 `build_demo_data.py` 一致，使 P0/P1 的口袋在页面上尺度一致。"""

MERGE_RADIUS = 6.0
"""跨复合物合并时，两处口袋中心相距 ≤ 这个值就算同一处（见方案稿 §2.3.5）。

比 `POCKET_RADIUS` 大一档是刻意的：不同晶体里同一个位点的配体位置能差几埃，
用 5 Å 会把同一个口袋分成两个。合并半径只决定「归并」这一件事，不改残基集合的圈法。
"""

MONATOMIC_IONS = frozenset(
    {
        # 碱金属 / 碱土金属
        "NA",  # 钠离子，缓冲液与结晶条件里最常见
        "K",  # 钾离子
        "LI",  # 锂离子
        "RB",  # 铷离子
        "CS",  # 铯离子
        "MG",  # 镁离子，辅因子也是它——但单原子本身不构成口袋
        "CA",  # 钙离子（注意与碳 α 的原子名重名，这里比的是残基名）
        "SR",  # 锶离子
        "BA",  # 钡离子
        # 过渡金属与重金属
        "ZN",  # 锌离子，金属酶活性中心，本轮认下来会看不到
        "FE",  # 铁离子
        "MN",  # 锰离子
        "CO",  # 钴离子
        "NI",  # 镍离子
        "CU",  # 铜离子
        "CD",  # 镉离子
        "HG",  # 汞离子
        # 卤素与其它单原子阴离子
        "CL",  # 氯离子，几乎每个晶体结构里都有
        "BR",  # 溴离子
        "IOD",  # 碘离子
        "F",  # 氟离子
    }
)
"""单原子离子。**放行会让金属离子变成一个「配体」**，簇中心就是那个金属原子，圈出的一圈残基
看着像个口袋但不是——本轮的取舍是宁可漏（金属酶看不到口袋，认下来，见方案稿 §2.3.7）
也不要错杀（把真配体丢掉是看不见的错）。

名单按「最常见的十来个」收，**不追求收全**（§7.5）。
"""

BUFFER_LIKE = frozenset(
    {
        # 多元醇（冷冻保护剂）
        "GOL",  # 甘油，几乎所有冷冻晶体里都有
        "EDO",  # 乙二醇
        "PEG",  # 聚乙二醇（单体的通名）
        "PG4",  # 四乙二醇
        "PGE",  # 三乙二醇
        "1PE",  # 聚乙二醇单甲醚
        "2PE",  # 聚乙二醇（另一种聚合度）
        "P6G",  # 六乙二醇
        "7PE",  # 七乙二醇
        "12P",  # 十二乙二醇
        "15P",  # 十五乙二醇
        "PGO",  # 丙二醇（1,2-丙二醇）
        "PGR",  # 丙二醇（另一种写法）
        "MPD",  # 2-甲基-2,4-戊二醇，常用的抗冻剂
        "BU3",  # 1,3-丁二醇
        # 醇 / 酮 / 酸（结晶添加剂）
        "IPA",  # 异丙醇
        "MOH",  # 甲醇
        "EOH",  # 乙醇
        "ACE",  # 乙酸根
        "ACT",  # 乙酸根（另一种写法）
        "ACY",  # 乙酸（质子化写法）
        "FMT",  # 甲酸根
        "CIT",  # 柠檬酸根
        "TLA",  # 酒石酸根
        "MLA",  # 苹果酸根
        "MLI",  # 苹果酸根（另一种写法）
        "SIN",  # 琥珀酸根
        "SUC",  # 蔗糖 / 琥珀酸（常见写法）
        "FUM",  # 富马酸根
        "MAL",  # 麦芽糖 / 苹果酸（常见写法）
        "LAC",  # 乳酸根
        # 缓冲液
        "TRS",  # 三羟甲基氨基甲烷（Tris）
        "EPE",  # 4-(2-羟乙基)-1-哌嗪乙磺酸（HEPES）
        "MES",  # 2-(N-吗啉)乙磺酸
        "BES",  # N,N-双(2-羟乙基)-2-氨基乙磺酸
        "BIS",  # 双(2-羟乙基)氨基三羟甲基甲烷（Bis-Tris）
        "IMD",  # 咪唑（镍柱洗脱残留）
        "CAC",  # 二甲砷酸根（旧结晶条件里常见）
        "PO4",  # 磷酸根
        "SO4",  # 硫酸根，极常见
        "NO3",  # 硝酸根
        "SCN",  # 硫氰酸根
        "AZI",  # 叠氮根（防腐剂）
        "NH4",  # 铵根
        "DMS",  # 二甲基亚砜
        "DMF",  # 二甲基甲酰胺
        # 还原剂 / 防聚集
        "DTT",  # 二硫苏糖醇
        "BME",  # β-巯基乙醇
        "TCEP",  # 三(2-羧乙基)膦
        # 糖类（糖基化修饰常挂成 HETATM，单独成组时不是配体）
        "NAG",  # N-乙酰葡糖胺，糖基化位点的标准残基
        "BMA",  # β-D-甘露糖
        "MAN",  # α-D-甘露糖
        "GAL",  # 半乳糖
        "GLC",  # 葡萄糖
        "FUC",  # 岩藻糖
        "XYP",  # 木吡喃糖
        "NDG",  # 2-脱氧-2-氨基葡萄糖
        "NGA",  # N-乙酰半乳糖胺
        "SIA",  # 唾液酸
        "GLA",  # α-D-半乳糖（另一种写法）
        "BGC",  # β-D-葡萄糖
        # 未知/未建模原子
        "UNX",  # 未知原子
        "UNL",  # 未知配体
    }
)
"""常见缓冲液 / 冷冻保护剂 / 结晶添加剂 / 糖基化残基。

**宁可漏不要错杀**（§7.5）：漏掉的后果是多出一个看得见的口袋（用户能发现），
错杀的后果是真配体消失、页面上什么都没有（用户发现不了）。
所以这份名单只收「确定不是配体」的那些，名字有歧义的一概不收。

`NAG` 这类糖收进来是因为它们在多数晶体里是糖基化修饰而非配体——但糖苷酶/凝集素的
真实底物糖就会被一起筛掉，这是本轮认下来的代价。
"""


class ExportGeometryError(ValueError):
    """这一组坐标算不出口袋。**不返回一个看着合理的数**，直接说清哪里不对。"""


# ---------------------------------------------------------------------------
# 重原子
# ---------------------------------------------------------------------------
# `is_heavy` 的定义在 `pdbtext.py`：清洗结构与算口袋都要用它，只能有一份。
# 这里保留导出名，是因为判据与下游一直按 `export.is_heavy` 取。


def _heavy_coords(group: HeteroGroup) -> tuple[tuple[float, float, float], ...]:
    return tuple(atom.coord for atom in group.atoms if is_heavy(atom))


# ---------------------------------------------------------------------------
# 配体口径
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LigandFilter:
    """筛杂原子组的结果。**排除的东西要报出来**，否则用户分不清
    「真只有一个配体」还是「被筛掉了」（方案稿 §2.3.7）。"""

    kept: tuple[HeteroGroup, ...]
    excluded: tuple[tuple[str, str], ...]
    """(残基名, 排除原因) 的列表，按组在文件里出现的顺序。"""

    @property
    def counts(self) -> dict[str, int]:
        """各类排除各几个。原因字符串作为键。"""
        tally: dict[str, int] = {}
        for _name, reason in self.excluded:
            tally[reason] = tally.get(reason, 0) + 1
        return tally

    def describe(self) -> str:
        """一句给人看的话。没有排除时也要明说，不然读者以为这一段没跑。"""
        if not self.excluded:
            return "没有排除任何杂原子组"
        parts = "、".join(f"{reason} {count} 个" for reason, count in sorted(self.counts.items()))
        return f"排除了 {len(self.excluded)} 个杂原子组（{parts}）"


def ligand_groups(
    doc: PdbDocument, *, modified_names: Iterable[str] = ()
) -> LigandFilter:
    """挑出当配体看的杂原子组。

    四类排除，**顺序即优先级**（一个组只记最先命中的那条理由）：

    1. **水** —— 由 `hetero_groups` 排掉（`WATER_RESIDUES`）。水太多，不逐条记入清单。
    2. **修饰残基** —— `MODRES` 登记过的名字（`modified_names`）。它们写着 `HETATM`
       却是聚合物的一部分，当配体抽走等于给骨架挖掉一个残基。
    3. **单原子离子** —— `MONATOMIC_IONS`。判据是「这个名字且只有一个原子」：
       名字对上但成组的（某些金属簇）留下。
    4. **缓冲液 / 冷冻保护剂 / 糖基化残基** —— `BUFFER_LIKE`。

    回来的是 `LigandFilter`，带着排除清单——**用户看不到的就是没发生的**。

    **修饰残基要在这里自己数一遍**：`hetero_groups(exclude_names=...)` 会把它们
    直接排除掉，于是它们根本到不了下面那个循环，清单里就少了这一类。而「MSE 被当修饰
    残基排掉了」与「这个结构里没有 MSE」对用户是两件事，所以要单独走一趟把名字收齐。
    """
    modified = {name.upper() for name in modified_names}
    kept: list[HeteroGroup] = []
    excluded: list[tuple[str, str]] = []

    # 修饰残基先自己扫一遍收名字（`hetero_groups` 之后不会再看到它们）。
    reported: set[str] = set()
    for atom in doc.atoms:
        name = atom.resname.upper()
        if not atom.is_hetero or atom.is_water or name not in modified:
            continue
        if atom.resname in reported:
            continue
        reported.add(atom.resname)
        excluded.append((atom.resname, "修饰残基"))

    for group in hetero_groups(doc, exclude_names=modified):
        name = group.resname.upper()
        if name in MONATOMIC_IONS and group.size == 1:
            excluded.append((group.resname, "单原子离子"))
            continue
        if name in BUFFER_LIKE:
            excluded.append((group.resname, "缓冲液或糖"))
            continue
        if not _heavy_coords(group):
            # 全是氢的组（少见但合法）：没有任何重原子可以算距离。
            excluded.append((group.resname, "没有重原子"))
            continue
        kept.append(group)

    return LigandFilter(kept=tuple(kept), excluded=tuple(excluded))


# ---------------------------------------------------------------------------
# 距离
# ---------------------------------------------------------------------------
def min_distance(
    left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]
) -> float:
    """两组坐标之间的最短距离（埃）。**只算重原子**是调用方的事——
    这里收的就是坐标本身，氢已经在上游滤掉了。

    任一组为空时**抛**，不返回 0：0 会被读成「贴在一起」，而真实含义是「没得算」。
    """
    if not left:
        raise ExportGeometryError("左边一组坐标是空的，最短距离无从算起")
    if not right:
        raise ExportGeometryError("右边一组坐标是空的，最短距离无从算起")

    best = math.inf
    for a in left:
        ax, ay, az = float(a[0]), float(a[1]), float(a[2])
        for b in right:
            dx = ax - float(b[0])
            dy = ay - float(b[1])
            dz = az - float(b[2])
            d2 = dx * dx + dy * dy + dz * dz
            if d2 < best:
                best = d2
    return math.sqrt(best)


def ligand_center(coords: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    """坐标的算术平均，即一个配体的几何中心。

    **不是包围盒中心**——与 `HeteroGroup.center` 同一个口径（那边是给报告用的算均）。
    页面上 Mol\\* 画球用的就是这个点，所以两者必须一致。空坐标抛，不返回原点。
    """
    if not coords:
        raise ExportGeometryError("一个坐标都没有，中心定不出来")
    total = [0.0, 0.0, 0.0]
    for point in coords:
        for axis in range(3):
            total[axis] += float(point[axis])
    count = len(coords)
    return (total[0] / count, total[1] / count, total[2] / count)


# ---------------------------------------------------------------------------
# 聚类（并查集）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LigandCluster:
    """一簇配体。**`members` 是组的下标**（指回调用方给的那条序列），不是坐标。"""

    members: tuple[int, ...]
    groups: tuple[HeteroGroup, ...]
    resnames: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def coords(self) -> tuple[tuple[float, float, float], ...]:
        """簇里所有配体的**重原子**坐标。圈残基与定中心都从它算。"""
        points: list[tuple[float, float, float]] = []
        for group in self.groups:
            points.extend(_heavy_coords(group))
        return tuple(points)

    @property
    def center(self) -> tuple[float, float, float]:
        return ligand_center(self.coords)

    def describe(self) -> str:
        return f"{'+'.join(self.resnames)}（{self.size} 个配体，{len(self.coords)} 个重原子）"


class _Union:
    """并查集。路径压缩 + 按秩合并——纯粹为了别在几千个原子组上退化成平方。"""

    __slots__ = ("_parent", "_rank")

    def __init__(self, count: int) -> None:
        self._parent = list(range(count))
        self._rank = [0] * count

    def find(self, item: int) -> int:
        parent = self._parent
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:  # 路径压缩
            parent[item], item = root, parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        rank = self._rank
        if rank[a] < rank[b]:
            a, b = b, a
        self._parent[b] = a
        if rank[a] == rank[b]:
            rank[a] += 1


def cluster_ligands(
    groups: Sequence[HeteroGroup], gap: float = CLUSTER_GAP
) -> tuple[LigandCluster, ...]:
    """把配体分簇：**任意一对重原子**相距 ≤ `gap` 就算同一簇。

    **并查集，不是顺序贪心。** A-B-C 三点，A 近 B、B 近 C、A 远 C：顺序贪心会把 C
    分出去，而它们在几何上是连成一片的。长条位点里很常见，不是理论问题
    （方案稿 §2.3.5 记了这条）。并查集还有个附带好处——结果与遍历顺序无关，
    判据里有一条专门盯它。

    判据是原子对距离而不是中心距，理由同 `CLUSTER_GAP` 的注释。

    回来按「重原子数降序、其次残基名」排，**排序确定**（同一组输入必得同一顺序）。
    """
    if not groups:
        return ()

    heavy = [_heavy_coords(group) for group in groups]
    joint = _Union(len(groups))
    gap2 = float(gap) * float(gap)

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if _within(heavy[i], heavy[j], gap2):
                joint.union(i, j)

    buckets: dict[int, list[int]] = {}
    for index in range(len(groups)):
        buckets.setdefault(joint.find(index), []).append(index)

    clusters = [
        LigandCluster(
            members=tuple(members),
            groups=tuple(groups[m] for m in members),
            resnames=tuple(groups[m].resname for m in members),
        )
        for members in buckets.values()
    ]
    clusters.sort(key=lambda c: (-len(c.coords), c.resnames, c.members))
    return tuple(clusters)


def _within(
    left: Sequence[Sequence[float]], right: Sequence[Sequence[float]], limit2: float
) -> bool:
    """任一对原子距离的平方 ≤ `limit2`。为了不每对都开根号。"""
    for a in left:
        ax, ay, az = a[0], a[1], a[2]
        for b in right:
            dx = ax - b[0]
            dy = ay - b[1]
            dz = az - b[2]
            if dx * dx + dy * dy + dz * dz <= limit2:
                return True
    return False


# ---------------------------------------------------------------------------
# 圈残基
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PocketResidue:
    """一个被口袋圈住的残基。`min_distance` 是**算出来的**，不是传进来的。"""

    chain: str
    number: str
    icode: str
    resname: str
    min_distance: float

    @property
    def key(self) -> ResidueKey:
        return ResidueKey(self.chain, self.number, self.icode)

    def as_dict(self) -> dict[str, object]:
        """`meta.json` 里 `pockets[].residues[]` 的形状。

        键名与 `runs/1iep-molstar/meta.json` 逐字一致（`resseq` / `resname`）——
        前端按这两个名字读，改一个字母就整块渲染不出来。
        """
        entry: dict[str, object] = {
            "chain": self.chain,
            "resseq": int(self.number) if self.number.isdigit() else self.number,
            "resname": self.resname,
            "min_distance": round(self.min_distance, 2),
        }
        if self.icode:
            entry["icode"] = self.icode
        return entry


def pocket_residues(
    doc: PdbDocument,
    ligand_coords: Sequence[Sequence[float]],
    radius: float = POCKET_RADIUS,
    *,
    chains: Iterable[str] | None = None,
) -> tuple[PocketResidue, ...]:
    """圈出配体周围 `radius` 埃以内的残基，带各自的最短距离。

    只算**聚合物**残基（`ATOM` 记录）：`HETATM` 里的水和别的配体不算——
    否则配体会互相把对方圈成「残基」。

    `chains` 给定时只算这几条链（P0 里一个复合物可能有多条链，受体只是其中一条）。

    **两侧都只算重原子**：配体那一侧由调用方滤（`ligand_groups` 顺手做了），残基这一侧
    在这里滤。氢必须两边都排掉——残基上的氢伸出来能比它的母原子更靠近配体
    （实测：`HA` 在 2.0 埃而它挂的 `CA` 在 3.0 埃），滤不干净就会出现
    「带氢的坐标算出 2.0、不带氢算出 3.0」这种同一份结构两个数的情况。
    而对接里认的距离是重原子之间的距离，2.0 那个数没有意义。

    `min_distance` 取该残基所有重原子到配体所有重原子的最短值，**保留两位小数**写进页面
    （与 `runs/1iep-demo/meta.json` 的 `2.67` 同一个精度）。

    `ligand_coords` 为空时抛——空口袋不是「零个残基」，是「没得算」。
    """
    if not ligand_coords:
        raise ExportGeometryError("配体一个重原子坐标都没有，圈不出残基")

    wanted = set(chains) if chains is not None else None
    radius2 = float(radius) * float(radius)

    buckets: dict[ResidueKey, list] = {}
    for atom in doc.atoms:
        if atom.is_hetero:
            continue
        if not is_heavy(atom):
            continue
        if wanted is not None and atom.chain not in wanted:
            continue
        buckets.setdefault(atom.key, []).append(atom)

    found: list[PocketResidue] = []
    for key, atoms in buckets.items():
        # 先按「任一原子进入半径」做粗筛，再算精确最短距离——粗筛只做平方比较。
        hit = False
        for atom in atoms:
            ax, ay, az = atom.x, atom.y, atom.z
            for point in ligand_coords:
                dx = ax - float(point[0])
                dy = ay - float(point[1])
                dz = az - float(point[2])
                if dx * dx + dy * dy + dz * dz <= radius2:
                    hit = True
                    break
            if hit:
                break
        if not hit:
            continue
        distances = tuple(atom.coord for atom in atoms)
        found.append(
            PocketResidue(
                chain=key.chain,
                number=key.number,
                icode=key.icode,
                resname=atoms[0].resname,
                min_distance=min_distance(distances, ligand_coords),
            )
        )

    found.sort(key=lambda item: (item.min_distance, item.chain, item.number, item.icode))
    return tuple(found)


# ---------------------------------------------------------------------------
# 跨复合物合并 / 接触计数
# ---------------------------------------------------------------------------
@dataclass
class _MergeSeed:
    """合并时的一条候选口袋。可变——合并就是往 `origins` 里追加。"""

    center: tuple[float, float, float]
    coords: tuple[tuple[float, float, float], ...]
    origins: list[dict] = field(default_factory=list)


def merge_pockets(
    pockets: Sequence[tuple[tuple[float, float, float], Sequence[Sequence[float]], dict]],
    radius: float = MERGE_RADIUS,
) -> list[dict]:
    """把多个复合物里算出的口袋按位置合并。

    每条输入是 `(中心, 重原子坐标, 来源信息)`；回来的是合并后的条目，每条带
    `centers` / `coords` / `origins`。**中心相距 ≤ `radius` 的并成一条**，
    来源信息全部保留（`complexes` 表要列全，见 §2.3.10）。

    合并用并查集，所以同样与输入顺序无关——两个来源隔着第三个来源时也能连起来。
    """
    seeds = [
        _MergeSeed(center=tuple(center), coords=tuple(coords), origins=[dict(origin)])
        for center, coords, origin in pockets
    ]
    if not seeds:
        return []

    joint = _Union(len(seeds))
    radius2 = float(radius) * float(radius)
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            dx = seeds[i].center[0] - seeds[j].center[0]
            dy = seeds[i].center[1] - seeds[j].center[1]
            dz = seeds[i].center[2] - seeds[j].center[2]
            if dx * dx + dy * dy + dz * dz <= radius2:
                joint.union(i, j)

    buckets: dict[int, list[int]] = {}
    for index in range(len(seeds)):
        buckets.setdefault(joint.find(index), []).append(index)

    merged: list[dict] = []
    for members in buckets.values():
        points: list[tuple[float, float, float]] = []
        origins: list[dict] = []
        for m in members:
            points.extend(seeds[m].coords)
            origins.extend(seeds[m].origins)
        merged.append(
            {
                "center": ligand_center(points) if points else seeds[members[0]].center,
                "coords": tuple(points),
                "origins": origins,
            }
        )

    merged.sort(key=lambda entry: (-len(entry["coords"]), entry["center"]))
    return merged


def contact_counts(
    ligand_coords: Sequence[Sequence[float]],
    residue_coords: Sequence[Sequence[float]],
    cutoff: float = 4.0,
) -> int:
    """有多少对配体原子—残基原子在 `cutoff` 埃以内。

    **本轮不写进 `meta.json`**（要真实位姿才算得准）。留着是因为「一个残基被几个
    原子夹住」在判断口袋松紧时有用，而且它同样是纯几何、不该交给模型心算。
    """
    if not ligand_coords or not residue_coords:
        return 0
    cutoff2 = float(cutoff) * float(cutoff)
    count = 0
    for a in ligand_coords:
        ax, ay, az = a[0], a[1], a[2]
        for b in residue_coords:
            dx = ax - b[0]
            dy = ay - b[1]
            dz = az - b[2]
            if dx * dx + dy * dy + dz * dz <= cutoff2:
                count += 1
    return count


def contacts_for(
    doc: PdbDocument,
    ligand_coords: Sequence[Sequence[float]],
    cutoff: float = 4.0,
) -> tuple[tuple[str, int], ...]:
    """每个残基各有多少个原子进入配体 `cutoff` 埃以内，按数量降序。

    与 `pocket_residues` 配着用：它回答「哪些残基在附近」，这一条回答「贴得多紧」。
    """
    if not ligand_coords:
        raise ExportGeometryError("配体一个重原子坐标都没有，接触无从算起")

    buckets: dict[ResidueKey, list] = {}
    for atom in doc.atoms:
        if atom.is_hetero or not is_heavy(atom):
            continue
        buckets.setdefault(atom.key, []).append(atom)

    tally: list[tuple[str, int]] = []
    for key, atoms in buckets.items():
        count = contact_counts(ligand_coords, tuple(atom.coord for atom in atoms), cutoff)
        if count:
            tally.append((f"{key}:{atoms[0].resname}", count))
    tally.sort(key=lambda item: (-item[1], item[0]))
    return tuple(tally)


# ---------------------------------------------------------------------------
# 运行目录：把口袋装配结果写成一页网页要的那几份文件
#
# 这一层是**装配点，不是算法**：口袋从哪来是 `pockets.py` 的事（P0 复合物 → P1 预测 → 空），
# 这里只负责按优先级去问、把答案写成 `meta.json` / `receptor.pdb` / `ligands/`，
# 再登记成 `artifact`。网页那一侧读的就是这三样。
# ---------------------------------------------------------------------------
RUN_META = "meta.json"
RUN_RECEPTOR = "receptor.pdb"
RUN_LIGANDS = "ligands"

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""运行目录名的允许形状。

运行目录名会进 URL（`#/structure/<run_id>`），所以**不接受路径分隔符与 `..`**——
`../../etc` 这种输入不该有机会被拼进一个待创建目录的路径里。
"""

MAX_NAME_LEN = 120

PORT_START = 8860
PORT_TRIES = 20


def runs_dir() -> Path:
    """运行目录的根：**项目根**下的 `runs/`，与服务的默认扫描位置一致。

    **两边必须指向同一个地方**：服务按目录名扫描，写在这里的目录它才看得到；
    换一处，网页上就是空的，而写盘那一步又是「成功」的——用户看到的是
    「导出了但页面上没有」，这比直接报错难查得多。

    这里原先回的是 `opencode_tui/runs/`（当时 `_project_root()` 给的是
    `opencode_tui` 而不是仓根），而服务的两个入口（`tools/serve_web.py` 与
    `python -m opencode_tui.core.web`）默认扫的是仓根 `runs/`。结果是
    `write_run` 产出的目录**服务一直看不见**，两边各自的判据却都是绿的：
    写盘的判据查文件在不在，服务的判据查读数与它扫的那个目录一致。

    定案用仓根：仓库里已有的两个演示运行（`runs/1iep-demo`、`runs/1iep-molstar`）
    在那，服务的默认值也在那，要改的只有这一处。

    打包后这个根跟着可执行文件走（`paths.project_root()`），与 `corpus/` 同一个
    判断：按 `__file__` 算会落到解包临时目录里，导出的运行重启即消失。
    """
    return project_root() / "runs"


def _slug(text: str, *, fallback: str = "run") -> str:
    """把一段文字压成一个能进目录名与 URL 的短串。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-._")
    return cleaned[:48] or fallback


def _run_summary(name: str, target_pdb: str, chains: Sequence[str], ligand: str) -> str:
    parts = [name or "运行"]
    if target_pdb:
        parts.append(f"结构 {target_pdb}")
    if chains:
        parts.append(f"链 {''.join(chains)}")
    if ligand:
        parts.append(f"参考配体 {ligand}")
    return "・".join(parts)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写一份 JSON：临时文件 + `os.replace`。

    `meta.json` 是运行目录的提交标记；直接覆盖时中途失败会留下半截 JSON，
    网页端每次请求都重新读它，读到半截就是整页空白。`products.write_json_atomic`
    保持 `indent=2`、`ensure_ascii=False`、不写尾随换行，与旧实现逐字节一致。
    """
    products.write_json_atomic(path, payload)


class WriteRunTool(Tool):
    """把「受体 + 口袋」写成一个运行目录，交给网页看。"""

    spec = ToolSpec(
        name="write_run",
        description=(
            "把一个受体（与可选的对接产物）写成一个**运行目录**：`meta.json` 记目标与口袋来源、"
            "`receptor.pdb` 是坐标、`ligands/` 放对接后的最佳位姿。产出的目录登记成 artifact，"
            "网页端据此渲染结构页。\n\n"
            "**同一个工具负责两种展示**：`view=pockets`（默认）算口袋；`view=structure` "
            "只展示结构、不联网也不算口袋。两种模式都会先做同一套预处理"
            "（选链、选交替构象、去水、去溶剂、去氢），页面按链配不同颜色。\n\n"
            "**给了 docking 就走对接结果这一侧**：把 `status=ok` 的 pose 复制进 `ligands/`、"
            "填 `molecules` 的分数与排名、并在 `provenance.poses` 里记下引擎与分数方向。"
            "`docking` 与 `ligands` 不能同时给——前者已经带着配体集。\n\n"
            "**口袋来源按优先级自动选，两条路二选一**：\n"
            "  · 给了 candidates（你自己给出的 PDB 编号清单）就走**先验这一侧**："
            "逐个取回来核对（编号是否存在、链序列是不是受体序列），通过的按 CA 原子"
            "叠合到受体上，再用叠合后的共晶配体圈口袋（`source=ligand`，带 `rmsd`）。"
            "**这一侧不再按 UniProt 检索复合物**——检索只返回带配体的已发布复合物，"
            "覆盖到的口袋窄。核对不通过的编号逐条列在返回里。\n"
            "  · 没给 candidates 就按老路：先查该蛋白在 RCSB 里所有带配体的复合物，"
            "一个都没有才转 prankweb 的 p2rank 预测（`source=p2rank`）。\n"
            "两条路都拿不到就写空口袋数组并说明原因。\n\n"
            "**uniprot 与 pdb_id 不是一回事**：uniprot 是一个蛋白（如 P00519），"
            "它名下可能对应几十上百个 PDB 条目——只给 pdb_id 会让主来源退化成"
            "「只看手上这一份结构」。给出 uniprot 才能让工具去搜全部复合物。\n\n"
            "不要自己编 center / score / min_distance：这些一律现算或向接口取。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "receptor": {
                    "type": "string",
                    "description": "受体条目引用，例如 receptor#1。**不要传文件路径**",
                },
                "ligands": {
                    "type": "string",
                    "description": (
                        "配体集合条目引用，例如 ligandset#2。没有对接结果时可选；"
                        "**给了 docking 就不要传它**"
                    ),
                },
                "docking": {
                    "type": "string",
                    "description": (
                        "对接产物 handle，例如 artifact#4。给了它就把最佳 pose 写进 "
                        "`ligands/` 并填 `molecules`；不要再传 ligands"
                    ),
                },
                "run_id": {
                    "type": "string",
                    "description": "运行目录名（字母数字与 . _ -）。留空时按受体的 pdb_id 生成",
                },
                "uniprot": {
                    "type": "string",
                    "description": (
                        "该蛋白的 UniProt 编号（如 P00519），主来源据此搜全部复合物。"
                        "**不是 PDB 编号**。留空时从受体条目的 meta 里找"
                    ),
                },
                "pdb_id": {
                    "type": "string",
                    "description": (
                        "PDB 条目号（如 1IEP），**只在主来源为空时**用于查预测。留空时从 meta 里找"
                    ),
                },
                "name": {"type": "string", "description": "运行显示名，进了界面标题"},
                "candidates": {
                    "type": "string",
                    "description": (
                        "先验清单：你自己给出的 PDB 编号，逗号或空格分隔"
                        "（如 \"1M17,2ITY,4HJO\"）。给了它就走先验这一侧，"
                        "不再按 UniProt 检索复合物。每个编号都会核对编号是否存在、"
                        "链序列是不是受体序列，不通过的逐条列在返回里"
                    ),
                },
                "view": {
                    "type": "string",
                    "enum": ["pockets", "structure"],
                    "description": (
                        "页面要展示什么。`pockets`（默认）算口袋并展示；"
                        "`structure` 只展示清洗后的结构，**不联网、不算口袋**。"
                        "两种模式写出的 `receptor.pdb` 都经过同一套预处理"
                        "（选链、选构象、去水、去溶剂、去氢），结构页按链配色"
                    ),
                },
            },
            "required": ["receptor"],
        },
    )
    primary_argument = "receptor"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：write_run 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw = arguments.get("receptor")
        if raw is None or str(raw).strip() == "":
            return ToolResult(
                output="缺少参数 receptor。先 list_workspace 看清有哪份受体，再把它的 handle 传进来。",
                failed=True,
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, ReceptorHandle, "receptor")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        # 受体坐标文件：raw 条目的 path 本身就是坐标文件；prepared 条目登记的是一条
        # **产物目录**（坐标在目录里的 receptor.pdb）。按 is_file() 一刀切会把后者判成
        # 「坐标文件不在了」——而它明明在，只是不在 path 这一个层级上。
        coordinate = _receptor_coordinate(entry)
        if coordinate is None:
            return ToolResult(output=_no_coordinate_note(entry), failed=True)

        # ---- 对接产物 / 配体集合（二选一）----
        docking_entry = None
        raw_docking = arguments.get("docking")
        if raw_docking is not None and str(raw_docking).strip() != "":
            try:
                docking_entry = ledger.get(raw_docking)
                expect_handle(docking_entry, ArtifactHandle, "docking")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)

        ligand_entries: list = []
        raw_ligands = arguments.get("ligands")
        if raw_ligands is not None and str(raw_ligands).strip() != "":
            try:
                ligand_entry = ledger.get(raw_ligands)
                expect_handle(ligand_entry, LigandSetHandle, "ligands")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            ligand_entries.append(ligand_entry)
        if docking_entry is not None and ligand_entries:
            return ToolResult(
                output="docking 已经带着配体集，不要再传 ligands；二选一。",
                failed=True,
            )

        # ---- 目标与主键 ----
        meta = dict(entry.meta or {})
        uniprot = _uniprot_of(arguments.get("uniprot"), meta)
        pdb_id = _pdb_id_of(arguments.get("pdb_id"), meta)
        name = _name_of(arguments.get("name"), meta, entry)
        chains = _chains_of(meta, entry)
        view = str(arguments.get("view") or "pockets").strip().lower()
        if view not in ("pockets", "structure"):
            return ToolResult(
                output=f"view 只能是 pockets 或 structure，收到 {view!r}。",
                failed=True,
            )

        # ---- 对接产物：在写任何文件之前校验完 ----
        docking_manifest: dict[str, Any] | None = None
        docking_results: list[dict[str, Any]] = []
        docking_skipped = 0
        if docking_entry is not None:
            try:
                docking_manifest, _root, docking_results, docking_skipped = _read_docking(
                    docking_entry, entry
                )
            except ValueError as error:
                return ToolResult(output=str(error), failed=True)

        # ---- 取出受体文本，解析事实 ----
        try:
            text = coordinate.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return ToolResult(
                output=f"读受体文件失败（{type(error).__name__}：{error}）", failed=True
            )

        # 受体条目没记链时（本地导入、老运行目录）从坐标本身认。不补这一步，
        # 页面拿到空链清单，`showChains([])` 会把整份结构清空——看着像加载失败。
        facts = structure.parse_pdb(text)
        if not chains:
            chains = tuple(facts.polymer_chains)

        # ---- 展示用的坐标：同一套预处理，口袋与结构两种模式都走 ----
        try:
            display = preprocess.clean_structure(
                text,
                chains=chains,
                modified_names=[name for name, _count in facts.modified],
            )
        except pdbtext.PdbEditError as error:
            return ToolResult(
                output=f"受体坐标无法按标准 PDB 列位解析：{error}", failed=True
            )

        # ---- 口袋：先验清单优先，其次 P0，为空才 P1（延迟导入避免成环）----
        from . import pockets as pockets_module

        note_parts: list[str] = []
        bundle = pockets_module.PocketBundle()
        p0_total = 0
        p0_used = 0
        p1_state = "absent"
        verdicts: tuple = ()

        candidates = _candidates_of(arguments.get("candidates"))
        if view == "structure":
            # 结构展示**不碰口袋来源**：不算、不联网。用户要的是「看结构」，
            # 这时去检索几十个复合物会让一个本该秒开的动作变成几分钟的等待。
            bundle = pockets_module.PocketBundle(
                origin="none",
                note="view=structure：只展示结构，未计算口袋",
            )
            note_parts.append(bundle.note)
        elif candidates:
            # 给了先验清单就走这一侧，**不再按 UniProt 检索复合物**：那种检索只返回
            # 「带配体的已发布复合物」，覆盖到的口袋窄。清单覆盖面宽，代价是清单本身
            # 不可信，所以逐个核对编号与序列，逐条把结论报回去。
            bundle, verdicts = pockets_module.prior_pockets(
                receptor_text=text,
                candidates=candidates,
                receptor_chains=chains,
            )
            p0_total, p0_used = bundle.total, bundle.used
        elif uniprot:
            bundle = pockets_module.complex_pockets(
                uniprot=uniprot, receptor_chains=chains
            )
            p0_total, p0_used = bundle.total, bundle.used
        else:
            bundle = pockets_module.PocketBundle(
                origin="none",
                note="没有 UniProt 编号，主来源无从查起（没查成）",
            )

        if view == "pockets" and bundle.is_empty:
            # 主来源为空（或没查成）才轮到预测这一侧。
            note_parts.append(bundle.note)
            predicted = pockets_module.p2rank_pockets(
                pdb_id=pdb_id, receptor_chains=chains
            )
            if not predicted.is_empty:
                predicted.note = (
                    f"主来源为空（{bundle.note}），改取预测：{predicted.note}"
                )
                bundle = predicted
                p1_state = "ok"
            else:
                # 预测这一侧也没结果：按**结构化字段**分「查了没有」与「没查成」。
                # 以前这里是去 `predicted.note` 里搜「没查成」这几个字，
                # 文案一改就静默失效（实测：prankweb 服务端报失败时 note 写的是
                # 「服务端没给原因」，搜不到那四个字，于是把「没查成」说成了「查了没有」）。
                p1_state = (
                    "unavailable"
                    if (predicted.reason == "failed" or not pdb_id)
                    else "absent"
                )
                note_parts.append(predicted.note)
        # P0 有货时 p1_state 保持 absent —— 有实验事实就不该再问预测。

        # ---- 参考配体：与 target.ligand 同名的簇才标 is_reference ----
        reference = _reference_ligand(pdb_id, ligand_entries, meta)
        _mark_reference(bundle.pockets, reference)

        # ---- 写盘 ----
        run_id = _run_id(arguments.get("run_id"), pdb_id, name)
        if run_id is None:
            return ToolResult(
                output=(
                    f"运行目录名不合法：{arguments.get('run_id')!r}。"
                    "只接受字母数字与 . _ -（不能含斜杠或 ..），且以字母数字开头"
                ),
                failed=True,
            )
        try:
            target = _prepare_run_dir(run_id)
            molecules = _write_ligands(target, ligand_entries)
            staging: Path | None = None
            poses: dict[str, Any] | None = None
            if docking_manifest is not None:
                # 先全部复制到 staging；这一步失败时旧 pose / 旧 meta 都没动。
                molecules, staging = _stage_docking_poses(
                    target, docking_results, chains
                )
            else:
                # 同一 run_id 上一次是对接结果、这一次没给 docking：旧 pose 必须清掉，
                # 否则目录里留着新 meta 不认识的位姿文件。
                for old in (target / RUN_LIGANDS).glob("pose-*.pdb"):
                    old.unlink()
            (target / RUN_RECEPTOR).write_text(
                display.text, encoding="utf-8", newline=""
            )
            if staging is not None:
                _commit_docking_poses(target, staging)
                poses = _poses_provenance(
                    docking_manifest,
                    docking_handle=str(docking_entry.handle),
                    written=len(molecules),
                    skipped=docking_skipped,
                )
            payload = _build_meta(
                run_id=run_id,
                name=name,
                pdb_id=pdb_id,
                uniprot=uniprot,
                chains=chains,
                reference=reference,
                bundle=bundle,
                ligands=molecules,
                prior=verdicts,
                poses=poses,
                view=view,
                preprocess_report=display.report,
            )
            _write_json(target / RUN_META, payload)
        except OSError as error:
            return ToolResult(
                output=(
                    f"写运行目录失败（{type(error).__name__}：{error}）；"
                    "旧的 meta.json 没有被改动"
                ),
                failed=True,
            )

        # ---- 登记成 artifact，父项挂受体 ----
        artifact = ledger.add(
            "artifact",
            path=target,
            summary=_run_summary(name, pdb_id, chains, reference),
            fingerprint=fingerprint_text(json.dumps(payload, sort_keys=True)),
            parents=[entry.handle],
            meta={"kind": "run", "run_id": run_id, "origin": bundle.origin, "view": view},
        )

        docking_report = (
            _docking_summary(docking_manifest, molecules, docking_skipped)
            if docking_manifest is not None
            else None
        )
        docking_handle = str(docking_entry.handle) if docking_entry is not None else ""

        return ToolResult(
            output=_run_report(
                artifact=artifact,
                target=target,
                bundle=bundle,
                p0_total=p0_total,
                p0_used=p0_used,
                p1_state=p1_state,
                uniprot=uniprot,
                pdb_id=pdb_id,
                reference=reference,
                note_parts=note_parts,
                prior=verdicts,
                docking=docking_report,
                view=view,
                preprocess_report=display.report,
            ),
            meta={
                "handle": str(artifact.handle),
                "run_id": run_id,
                "view": view,
                "origin": bundle.origin,
                "pockets": len(bundle.pockets),
                "total": p0_total,
                "used": p0_used,
                "p2rank": p1_state,
                "docking": docking_handle,
                "molecules": len(molecules),
                "best_score": (
                    docking_report["best_score"] if docking_report else None
                ),
            },
        )


# ---------------------------------------------------------------------------
# write_run 的取值小工具
# ---------------------------------------------------------------------------
def _uniprot_of(explicit: Any, meta: dict[str, Any]) -> str:
    value = str(explicit or "").strip()
    if value:
        return value
    return str(meta.get("uniprot") or meta.get("accession") or "").strip()


def _pdb_id_of(explicit: Any, meta: dict[str, Any]) -> str:
    value = str(explicit or "").strip()
    if value:
        return value.upper()
    return str(meta.get("pdb_id") or "").strip().upper()


def _name_of(explicit: Any, meta: dict[str, Any], entry: Any) -> str:
    value = str(explicit or "").strip()
    if value:
        return value[:MAX_NAME_LEN]
    title = str(meta.get("title") or "").strip()
    if title:
        return title[:MAX_NAME_LEN]
    return str(entry.summary or entry.handle)[:MAX_NAME_LEN]


def _receptor_coordinate(entry: Any) -> Path | None:
    """受体坐标文件的真实位置。

    raw 条目登记的就是坐标文件本身；prepared 条目登记的是**产物目录**，
    坐标在目录里（meta 的 `pdb_file`，默认 `receptor.pdb`）。
    """
    path = getattr(entry, "path", None)
    if path is None:
        return None
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    meta = dict(getattr(entry, "meta", {}) or {})
    declared = str(meta.get("pdb_file") or "").strip()
    for candidate_name in (declared, RUN_RECEPTOR):
        if not candidate_name:
            continue
        candidate = path / candidate_name
        if candidate.is_file():
            return candidate
    return None


def _no_coordinate_note(entry: Any) -> str:
    """把「坐标文件不在」说清是哪一种：产物目录里没有，还是文件本身没了。"""
    path = getattr(entry, "path", None)
    if path is not None and path.is_dir():
        return (
            f"{entry.handle} 指向的产物目录里没有坐标文件（{path}）："
            "这份产物可能被覆盖或删过。用 list_workspace 核对工作区，"
            "或回到原始结构（状态 raw 的那一条受体）重新准备一次。"
        )
    return (
        f"{entry.handle} 的坐标文件不在了（{path}）。"
        "先用 list_workspace 核对工作区，或重新取一份结构。"
    )


def _chains_of(meta: dict[str, Any], entry: Any) -> tuple[str, ...]:
    """从受体条目的 meta 里挑出聚合物链，作为「只算受体那几条链」的默认依据。"""
    chains = meta.get("chains")
    if not isinstance(chains, list):
        return ()
    out: list[str] = []
    for item in chains:
        if isinstance(item, dict) and item.get("polymer"):
            chain_id = str(item.get("chain_id") or "")
            if chain_id:
                out.append(chain_id)
        elif isinstance(item, str) and item.strip():
            # prepared 受体的 meta 里是 `["A"]`，远端原始结构里是
            # `[{"chain_id": "A", "polymer": true}]`；两种都要认。
            out.append(item.strip())
    return tuple(out)


def _reference_ligand(pdb_id: str, ligand_entries: list, meta: dict[str, Any]) -> str:
    """参考配体代号：优先现成配体集合的名字，其次目标 meta 里的，最后空。"""
    names: list[str] = []
    for entry in ligand_entries:
        ligand_meta = dict(getattr(entry, "meta", {}) or {})
        name = str(ligand_meta.get("ligand") or ligand_meta.get("resname") or "").strip()
        if name:
            names.append(name)
    if names:
        return "+".join(names)
    return str(meta.get("ligand") or "").strip()


def _mark_reference(pockets: list[dict[str, Any]], reference: str) -> None:
    """把与参考配体同名的簇标上 `is_reference`。**只标第一个**，标多个就没有「参考」的意思了。"""
    if not reference:
        return
    wanted = {name.strip().upper() for name in reference.split("+") if name.strip()}
    for pocket in pockets:
        complexes = pocket.get("complexes")
        if not isinstance(complexes, list):
            continue
        for source in complexes:
            codes = {
                code.strip().upper()
                for code in str(source.get("ligand", "")).split("+")
                if code.strip()
            }
            if codes & wanted:
                pocket["is_reference"] = True
                pocket["rank"] = 1
                return


def _run_id(explicit: Any, pdb_id: str, name: str) -> str | None:
    """运行目录名：显式给就用显式的，否则按 pdb_id 生成。不合法时返回 None。"""
    value = str(explicit or "").strip()
    if value:
        return value if RUN_ID_PATTERN.match(value) else None
    base = _slug(pdb_id or name, fallback="run")
    candidate = base
    index = 2
    root = runs_dir()
    while (root / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
        if index > 999:
            break
    return candidate if RUN_ID_PATTERN.match(candidate) else None


def _prepare_run_dir(run_id: str) -> Path:
    target = runs_dir() / run_id
    (target / RUN_LIGANDS).mkdir(parents=True, exist_ok=True)
    return target


def _write_ligands(target: Path, ligand_entries: list) -> list[dict[str, Any]]:
    """没有对接产物时 `molecules` 留空。

    配体集合在这一步只用于找参考配体（`_reference_ligand`），不往运行目录里复制
    任何文件。以前这里写 `{handle, name, file: ""}` 占位，前端读到 `score` 会直接
    抛异常——一份「打不开的运行目录」比空数组更糟。
    """
    del target, ligand_entries
    return []


def _read_docking(
    entry: Any, receptor_entry: Any
) -> tuple[dict[str, Any], Path, list[dict[str, Any]], int]:
    """校验 docking artifact，返回 `(manifest, 根目录, 可用结果, 跳过数)`。

    这一步在写任何文件之前跑：pose 路径不存在、受体不匹配都应当让旧运行目录
    保持原样，而不是先写一半再报错。
    """
    if str(getattr(entry, "meta", {}).get("kind") or "") != "docking":
        raise ValueError(
            "这是运行目录或其他产物，不是对接产物；docking 参数要的是 kind=docking 的 artifact"
        )
    root = getattr(entry, "path", None)
    if root is None or not Path(root).is_dir():
        raise ValueError("对接产物的目录不在了；重新跑 dock")
    root = Path(root).resolve()
    try:
        manifest = products.read_json(root / "manifest.json")
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"读对接 manifest 失败（{type(error).__name__}：{error}）"
        ) from error
    if (
        str(manifest.get("kind") or "") != "docking"
        or str(manifest.get("stage") or "") != "docking"
    ):
        raise ValueError("对接 manifest 不是 docking 状态；重新跑 dock")

    expected = str(getattr(receptor_entry, "fingerprint", "") or "")
    actual = str((manifest.get("receptor") or {}).get("fingerprint") or "")
    if expected and actual:
        if expected != actual:
            raise ValueError(
                "对接产物属于另一份受体（指纹不一致）；先 switch_receptor，或对当前受体重新对接"
            )
    else:
        handle = str((manifest.get("receptor") or {}).get("handle") or "")
        if handle and handle != str(receptor_entry.handle):
            raise ValueError(
                f"对接产物属于 {handle}，本次给的是 {receptor_entry.handle}；不要跨受体混用"
            )

    usable: list[dict[str, Any]] = []
    skipped = 0
    for result in manifest.get("results") or []:
        if str(result.get("status") or "") != "ok":
            # no_pdb 这类有分数但没有位姿的记录：不写进运行目录，但要计数。
            skipped += 1
            continue
        relative = str(result.get("pose_pdb") or "")
        if not relative:
            skipped += 1
            continue
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"对接 manifest 指向的 pose 不存在或越界：{relative}")
        score = result.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            skipped += 1
            continue
        usable.append({**result, "_source": source, "score": float(score)})

    # 按 dock 给的 rank 排；没有 rank 的退回分数升序。写进运行目录后重新密集编号。
    usable.sort(
        key=lambda item: (
            int(item.get("rank") or 0) or 1_000_000,
            float(item["score"]),
            str(item.get("id") or ""),
        )
    )
    if not usable:
        raise ValueError(
            "对接产物里没有可用的 pose（status=ok 且 pose_pdb 存在的记录为零）；"
            "先看 dock 的 manifest.failures"
        )
    return manifest, root, usable, skipped


def _stage_docking_poses(
    target: Path, results: Sequence[dict[str, Any]], chains: Sequence[str]
) -> tuple[list[dict[str, Any]], Path]:
    """把 pose 复制到 `ligands/.staging-*`，返回 `(molecules, staging)`。

    先全部复制成功再动旧文件：复制阶段失败时旧 `pose-*.pdb` 与旧 `meta.json`
    都还在，运行目录仍是上一次那份完整结果。
    """
    ligands_dir = target / RUN_LIGANDS
    ligands_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=ligands_dir))
    molecules: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    try:
        for index, result in enumerate(results, start=1):
            filename = f"pose-{index:02d}.pdb"
            shutil.copyfile(result["_source"], staging / filename)
            identifier = str(result.get("id") or f"LIG-{index:04d}")
            base = identifier
            suffix = 2
            while identifier.lower() in used_ids:
                identifier = f"{base}-{suffix}"
                suffix += 1
            used_ids.add(identifier.lower())
            molecules.append(
                {
                    "id": identifier,
                    "name": str(result.get("name") or identifier),
                    "score": float(result["score"]),
                    "pose": f"{RUN_LIGANDS}/{filename}",
                    "chain": chains[0] if chains else "",
                    "rank": index,
                }
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return molecules, staging


def _commit_docking_poses(target: Path, staging: Path) -> None:
    """删除旧 `pose-*.pdb`，把 staging 里的新文件换到 `ligands/`。"""
    ligands_dir = target / RUN_LIGANDS
    for old in ligands_dir.glob("pose-*.pdb"):
        old.unlink()
    try:
        for staged in sorted(staging.iterdir()):
            os.replace(staged, ligands_dir / staged.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _poses_provenance(
    manifest: dict[str, Any],
    *,
    docking_handle: str,
    written: int,
    skipped: int,
) -> dict[str, Any]:
    counts = manifest.get("counts") or {}
    engine = manifest.get("engine") or {}
    return {
        "source": "vina",
        "engine": str(engine.get("version") or engine.get("name") or "vina"),
        "score_direction": "lower_is_better",
        "score_unit": "kcal/mol",
        "ranked_by": "score",
        "receptor": str((manifest.get("receptor") or {}).get("handle") or ""),
        "ligandset": str((manifest.get("ligands") or {}).get("handle") or ""),
        "docking": str(docking_handle),
        "params": dict(manifest.get("params") or {}),
        "counts": {
            "docked": int(counts.get("docked") or 0),
            "failed": int(counts.get("failed") or 0),
            "written": written,
            "skipped": skipped,
        },
    }


def _docking_summary(
    manifest: dict[str, Any], molecules: Sequence[dict[str, Any]], skipped: int
) -> dict[str, Any]:
    counts = manifest.get("counts") or {}
    best = molecules[0] if molecules else None
    return {
        "written": len(molecules),
        "docked": int(counts.get("docked") or 0),
        "failed": int(counts.get("failed") or 0),
        "skipped": skipped,
        "best_score": float(best["score"]) if best else None,
        "best_id": str(best["id"]) if best else "",
    }


def _build_meta(
    *,
    run_id: str,
    name: str,
    pdb_id: str,
    uniprot: str,
    chains: Sequence[str],
    reference: str,
    bundle: Any,
    ligands: list[dict[str, Any]],
    prior: Sequence[Any] = (),
    poses: dict[str, Any] | None = None,
    view: str = "pockets",
    preprocess_report: Any = None,
) -> dict[str, Any]:
    """装配 `meta.json`。

    顶层键与 `runs/1iep-molstar/meta.json` 一致：
    `run_id` / `target` / `structure` / `reference_ligand` / `pockets` / `molecules` / `provenance`。
    **不要加新的顶层键**——判据按「与演示数据同键」核对，多一个键就会报出来。
    先验清单的取证结论因此挂在 `provenance.pockets.prior` 里，那是已有键的内层；
    本次的 `view` 与预处理报告同理，挂在 `provenance` 下。
    """
    pockets = [dict(pocket) for pocket in bundle.pockets]
    _label_pockets(pockets)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "target": {
            "name": name,
            "pdb_id": pdb_id,
            "chains": list(chains),
            "reference_chain": chains[0] if chains else "",
            "ligand": reference,
        },
        "structure": RUN_RECEPTOR,
        "reference_ligand": f"{RUN_LIGANDS}/{reference}.pdb" if reference else "",
        "pockets": pockets,
        "molecules": ligands,
        "provenance": {
            "view": view,
            "pockets": {
                "method": _origin_method(bundle.origin),
                "origin": bundle.origin,
                "note": bundle.note,
                "total": bundle.total,
                "used": bundle.used,
                "excluded": dict(bundle.excluded or {}),
                "sources": list(bundle.sources or []),
                "pdb_id": pdb_id,
                "uniprot": uniprot,
            },
        },
    }
    if preprocess_report is not None:
        payload["provenance"]["preprocess"] = preprocess_report.as_dict()
    if prior:
        payload["provenance"]["pockets"]["prior"] = [
            {
                "pdb_id": item.pdb_id,
                "state": item.state,
                "reason": item.reason,
                "pair": item.pair,
                "identity": item.identity,
                "rmsd": item.rmsd,
                "ligands": list(item.ligands),
            }
            for item in prior
        ]
    if poses is not None:
        payload["provenance"]["poses"] = poses
    return payload


def _label_pockets(pockets: list[dict[str, Any]]) -> None:
    """给缺 `id` / `label` 的口袋补上。

    页面按 `pocket.id` 与 `pocket.label` 渲染口袋列表，缺了会显示成 `undefined`
    ——而复合物与先验清单这两侧出来的口袋本来就没有这两个字段（预测那一侧有，
    编号就是预测给的排名）。补在这里而不是在各自的来源里，因为「第几个」只有
    排完序才知道，而排序发生在装配 `meta.json` 之前。
    """
    for index, pocket in enumerate(pockets, start=1):
        pocket.setdefault("id", f"P{index}")
        if pocket.get("label"):
            continue
        names = _pocket_ligand_names(pocket)
        pocket["label"] = f"{names} 位点" if names else f"口袋 {index}"


def _pocket_ligand_names(pocket: dict[str, Any]) -> str:
    """这个口袋是从哪些配体代号圈出来的，按出现顺序去重。"""
    complexes = pocket.get("complexes")
    if not isinstance(complexes, list):
        return ""
    names: list[str] = []
    for item in complexes:
        if not isinstance(item, dict):
            continue
        for code in str(item.get("ligand") or "").split("+"):
            code = code.strip()
            if code and code not in names:
                names.append(code)
    return "+".join(names)


def _candidates_of(explicit: Any) -> tuple[str, ...]:
    """把先验清单那段文字切成编号，按出现顺序去重。空串与重复项都丢掉。"""
    text = str(explicit or "").strip()
    if not text:
        return ()
    out: list[str] = []
    for token in re.split(r"[\s,;、]+", text):
        code = token.strip().upper()
        if code and code not in out:
            out.append(code)
    return tuple(out)


def _origin_method(origin: str) -> str:
    return {
        "complex": "rcsb-complex",
        "aligned": "prior-aligned",
        "predicted": "p2rank",
        "none": "",
    }.get(origin, "")


def _run_report(
    *,
    artifact: Any,
    target: Path,
    bundle: Any,
    p0_total: int,
    p0_used: int,
    p1_state: str,
    uniprot: str,
    pdb_id: str,
    reference: str,
    note_parts: list[str],
    prior: Sequence[Any] = (),
    docking: dict[str, Any] | None = None,
    view: str = "pockets",
    preprocess_report: Any = None,
) -> str:
    # 延迟导入：`align` 与 `pockets` 都不在模块顶层引，避免绕回成环。
    from . import align as align_module

    lines = [f"已写出运行目录 {target}，登记为 {artifact.handle}。"]
    if view == "structure":
        lines.append(
            "  展示模式：只展示结构（未计算口袋、未联网）。"
            "要口袋就把 view 改成 pockets 再写一次。"
        )
    else:
        lines.append(
            f"  口袋来源：{bundle.origin}（{_origin_label(bundle.origin)}）"
            f"，共 {len(bundle.pockets)} 个口袋。"
        )
    if preprocess_report is not None:
        lines.append("  结构预处理：" + preprocess_report.describe() + "。")
    if view == "structure":
        if reference:
            lines.append(f"  参考配体：{reference}。")
        if docking:
            best = docking.get("best_score")
            best_text = (
                f"最佳 {best:.2f} kcal/mol（{docking.get('best_id') or '—'}）"
                if isinstance(best, (int, float))
                else "没有可比较的分数"
            )
            lines.append(
                f"  对接：{docking.get('written', 0)} 个位姿写入 {RUN_LIGANDS}/，{best_text}。"
            )
        lines.append("  提示：用 open_viewer 打开这一份看结构。")
        return "\n".join(lines)
    if bundle.origin == "aligned":
        lines.append(
            f"  先验清单：共 {p0_total} 个编号，通过核对并使用 **{p0_used} 个**"
            "（逐个核对了编号是否存在、链序列是不是受体序列，再按 CA 原子叠合）。"
        )
    elif bundle.origin == "complex":
        lines.append(
            f"  主来源：UniProt {uniprot} 名下**搜到 {p0_total} 个**带配体复合物，"
            f"本次下载并使用 **{p0_used} 个**（只报 used 会让人以为这个蛋白只有这么多）。"
        )
    elif bundle.origin == "predicted":
        lines.append(
            f"  主来源为空（P0 total={p0_total}），改取 prankweb 预测"
            f"（PDB {pdb_id or '（缺编号）'}）。"
        )
    else:
        lines.append(
            f"  两条来源都没给出结果。预测这一侧：{_p2rank_label(p1_state)}。"
        )
    if note_parts:
        lines.append("  说明：" + "；".join(part for part in note_parts if part))
    elif bundle.note:
        lines.append("  说明：" + bundle.note)

    if prior:
        lines.extend(align_module.describe(prior))
        lines.append(
            "  提示：剔除的编号不要直接拿去用；要换清单就再调一次本工具，"
            "把新的编号写进 candidates。"
        )

    excluded = dict(bundle.excluded or {})
    if excluded:
        detail = "、".join(f"{reason} {count} 个" for reason, count in sorted(excluded.items()))
        lines.append(f"  筛掉的杂原子组：{detail}。")
    lines.append(
        "  提示：参考配体留空时页面不标 is_reference；用 open_viewer 打开这一份看结构。"
    )
    if reference:
        lines.append(f"  参考配体：{reference}（同名的簇已标 is_reference）。")
    if docking:
        best = docking.get("best_score")
        if isinstance(best, (int, float)):
            best_text = f"最佳 {best:.2f} kcal/mol（{docking.get('best_id') or '—'}）"
        else:
            best_text = "没有可比较的分数"
        lines.append(
            f"  对接：{docking.get('written', 0)} 个位姿写入 {RUN_LIGANDS}/，"
            f"{best_text}；失败 {docking.get('failed', 0)}、"
            f"跳过 {docking.get('skipped', 0)}。"
        )
        lines.append(
            "  提示：分数越低越好，只能在同一受体、同一盒子内比较，不是结合自由能。"
        )
    return "\n".join(lines)


def _origin_label(origin: str) -> str:
    return {
        "complex": "实验复合物共晶配体，实测事实",
        "aligned": "先验清单取证后叠合的共晶配体，实测事实",
        "predicted": "p2rank 预测，算出来的位置",
        "none": "没有可用来源，口袋留空",
    }.get(origin, origin)


def _p2rank_label(state: str) -> str:
    return {
        "ok": "有结果",
        "absent": "查了，确实没有",
        "unavailable": "没查成（网络或接口不可用）",
    }.get(state, state)


class OpenViewerTool(Tool):
    """给一份运行目录一个网页地址。纯查询，不登记任何东西。"""

    spec = ToolSpec(
        name="open_viewer",
        description=(
            "给一个已写出的运行目录（artifact）返回它在**本地结构页**上的地址，"
            "形如 `http://127.0.0.1:<port>/#/structure/<run_id>`。\n\n"
            "默认只返回地址、**不擅自打开浏览器**（无头环境会静默失败，"
            "在 TUI 里 suspend 去拉浏览器会把界面顶掉）——需要时再传 open_browser=true。\n\n"
            "服务没起来时返回一条可操作的说明，不是抛异常。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "run": {
                    "type": "string",
                    "description": "运行目录条目引用，例如 artifact#3。留空则指向首页",
                },
                "open_browser": {
                    "type": "boolean",
                    "description": "是否顺手用系统默认浏览器打开。默认不开",
                },
            },
        },
    )
    primary_argument = "run"
    context_argument = "ledger"
    viewer_argument = "viewer"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        viewer = arguments.get("viewer")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：open_viewer 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        run_id = ""
        raw = arguments.get("run")
        if raw is not None and str(raw).strip() != "":
            try:
                entry = ledger.get(raw)
                expect_handle(entry, ArtifactHandle, "run")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            run_id = str((entry.meta or {}).get("run_id") or entry.path.name if entry.path else "")

        port = _resolve_port(viewer)
        if not port:
            return ToolResult(
                output=(
                    "本地结构服务没有在监听（从 "
                    f"{PORT_START} 起试了 {PORT_TRIES} 个端口都没连上）。"
                    "先确认应用已启动、端口没被占用，再重试。"
                ),
                failed=True,
            )

        url = f"http://127.0.0.1:{port}/"
        if run_id:
            url += f"#/structure/{run_id}"

        opened = False
        if arguments.get("open_browser"):
            try:
                import webbrowser

                opened = bool(webbrowser.open(url))
            except Exception:  # 无头环境、没有默认浏览器
                opened = False

        suffix = "（已尝试打开浏览器）" if arguments.get("open_browser") else "（未打开浏览器，请自行点开）"
        if arguments.get("open_browser") and not opened:
            suffix = "（尝试打开浏览器没成功——环境可能没有默认浏览器，请手动复制地址）"
        return ToolResult(
            output=f"结构页地址：{url}{suffix}",
            meta={"url": url, "port": port, "run_id": run_id},
        )


def _resolve_port(viewer: Any) -> int:
    """服务实际监听的端口：优先问注入的 viewer 状态，否则从 8860 起探。"""
    for name in ("port", "web_port"):
        value = getattr(viewer, name, None) if viewer is not None else None
        if isinstance(value, int) and value > 0:
            return value
    return _probe_port()


def _probe_port() -> int:
    """从 8860 起逐个试，返回第一个有人监听的端口；都没有返回 0。"""
    for offset in range(PORT_TRIES):
        port = PORT_START + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return port
    return 0


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [WriteRunTool(), OpenViewerTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
