"""PDB 坐标的**纯文本编辑层**：选链、去水去杂、抽共晶配体、算盒子。

这一层的全部职责是「按固定列规则增删行」。它不认识化学、不导入任何第三方库、
也不登记成工具——所以受体准备里除「模板匹配 → PDBQT」之外的逻辑，都能在没有装化学依赖的
机器上测干净。这不是洁癖：那套依赖有一百多兆、只有 wheel 才有，让「删一行水」这种
判断依赖于它，等于让最容易写错的那部分逻辑也最难测。

**为什么是「增删行」而不是「解析成对象再重新序列化」。** 重新序列化意味着把每一行
都按格式重写一遍，于是每一个列位位置都有机会写错一次，而写错不报错、只是静默产出
另一份坐标。这里的原则是 **没被要求动的行，一个字节都不动**：解析只是「给行做一个
索引」，输出就是把留下的行原样接起来。

由此带来两个刻意的选择：

* **不重编号。** 删掉水与配体之后 `ATOM` 的序号会有空洞。空洞是合法的，很多已发布
  的结构本身就有；而重编号要求我们把每个坐标行的 7–11 列重写一遍，那是拿「序号好看」
  换「可能写坏每一行」。下游（Meeko / Vina）也不按序号认原子。
* **`CONECT` 跟着一起走。** 删掉共晶配体却留着指向它的 `CONECT`，就是给受体留下
  指向不存在原子的键。规则是：一条 `CONECT` 引用的序号 **全部** 还在才保留，
  否则整条丢掉——**不做半条**，改写半条又是一次重新序列化。
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace

__all__ = [
    "ATOM_ALT_LOC",
    "ATOM_CHAIN",
    "ATOM_INSERTION_CODE",
    "ATOM_RESIDUE_NAME",
    "ATOM_RESIDUE_NUMBER",
    "WATER_RESIDUES",
    "AtomRecord",
    "HeteroGroup",
    "PdbDocument",
    "PdbEditError",
    "ResidueKey",
    "altloc_residues",
    "calc_box",
    "format_box",
    "hetero_groups",
    "is_heavy",
    "keep_chains",
    "parse",
    "select_altloc",
    "strip_hetero",
    "take_group",
]


# ---------------------------------------------------------------------------
# 列位
#
# 这些常量放在这一层而不是 `structure.py`：两个模块都要用，而 `pdbtext` 是
# 零依赖的那一个，让它当唯一的定义处，`structure` 反过来从这里导入。
# 两处各写一份的话，「链分类」与「链选择」迟早对不上，而对不上的表现是
# 「删掉了本来要留的那条链」——屏幕上一切正常。
# ---------------------------------------------------------------------------
WATER_RESIDUES = frozenset({"HOH", "DOD", "WAT"})
"""按残基名认水。三个名字都是常见的写法，缺一个就会把水当配体抽走。"""

ATOM_ALT_LOC = 16
ATOM_RESIDUE_NAME = slice(17, 20)
ATOM_CHAIN = 21
ATOM_RESIDUE_NUMBER = slice(22, 26)
ATOM_INSERTION_CODE = 26

ATOM_SERIAL = slice(6, 11)
ATOM_NAME = slice(12, 16)
ATOM_X = slice(30, 38)
ATOM_Y = slice(38, 46)
ATOM_Z = slice(46, 54)
ATOM_ELEMENT = slice(76, 78)
"""元素的列（规范里的 77–78 列）。

短于 78 列的行读不到它——那时给空串。元素列 **不能** 猜：它与原子名不是一回事
（`CA` 是碳 α 还是钙），猜错就是元素级别的错。
"""

CONECT_SERIALS = (
    slice(6, 11),
    slice(11, 16),
    slice(16, 21),
    slice(21, 26),
    slice(26, 31),
)
"""`CONECT` 最多带五个原子序号，每个占 5 列（7–11、12–16、…、27–31）。"""

MIN_COORDINATE_LINE = 54
"""一条坐标行至少要有的长度——读得到 z 就够（z 占 47–54 列）。"""


class PdbEditError(ValueError):
    """这份坐标文件按标准 PDB 列位读不出来。"""


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResidueKey:
    """定位一个残基的最小信息：链 + 序号 + 插入码。"""

    chain: str
    number: str
    icode: str = ""

    def __str__(self) -> str:
        return f"{self.chain or '·'}:{self.number}{self.icode}"


@dataclass(frozen=True)
class AtomRecord:
    """一行坐标记录。`line` 是 **原文**，其余字段只是它的索引。"""

    index: int
    line: str
    kind: str
    """`ATOM` 或 `HETATM`。"""

    serial: str
    name: str
    altloc: str
    resname: str
    chain: str
    number: str
    icode: str
    x: float
    y: float
    z: float
    element: str

    @property
    def is_hetero(self) -> bool:
        return self.kind == "HETATM"

    @property
    def is_water(self) -> bool:
        return self.is_hetero and self.resname in WATER_RESIDUES

    @property
    def key(self) -> ResidueKey:
        return ResidueKey(self.chain, self.number, self.icode)

    @property
    def label(self) -> str:
        """给人看的形式：`A:328:Glu`。报「哪个残基出问题」时够用。"""
        return f"{self.key}:{self.resname}"

    @property
    def coord(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


_HYDROGEN_NAMES = frozenset({"H", "D", "T"})


def is_heavy(atom: AtomRecord) -> bool:
    """这个原子是不是重原子（非氢）。

    判氢优先看元素列；元素列读不到时（短行）退回按原子名首字符判。
    **两种判法都会漏**（元素列缺失且原子名不以 H 开头的情形），但漏一个氢只是
    让最短距离多算几个数，不会算错——而把重原子误判成氢会让它整个不参与，
    那是错的。所以两个判据都朝「算它是重原子」这一侧让。

    放在文本层而不是几何层，是因为「清洗结构」与「算口袋」两处都要用它；
    各写一份的话，去氢的规则会在其中一处悄悄漂移。
    """
    element = (atom.element or "").strip().upper()
    if element:
        return element not in _HYDROGEN_NAMES
    return not (atom.name or "").strip().upper().startswith(("H", "D"))


@dataclass(frozen=True)
class HeteroGroup:
    """一组同链同号的杂原子残基（一个水、一个离子、一个配体各算一组）。"""

    key: ResidueKey
    resname: str
    atoms: tuple[AtomRecord, ...]

    @property
    def size(self) -> int:
        return len(self.atoms)

    @property
    def label(self) -> str:
        return f"{self.resname} {self.key}"

    @property
    def center(self) -> tuple[float, float, float]:
        """原子坐标的算术平均。给报告用，不是盒子中心。"""
        mean = [0.0, 0.0, 0.0]
        for atom in self.atoms:
            for axis in range(3):
                mean[axis] += atom.coord[axis]
        return (mean[0] / self.size, mean[1] / self.size, mean[2] / self.size)

    @property
    def coords(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(atom.coord for atom in self.atoms)


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Line:
    """文档里的一行。坐标行带 `AtomRecord`，`CONECT` 带序号，其余都不带。"""

    index: int
    text: str
    kind: str
    atom: AtomRecord | None = None
    serials: tuple[str, ...] = ()


class PdbDocument:
    """一份坐标文件的行序列。不可变——每次编辑返回新的一份。"""

    __slots__ = ("extra_models", "lines")

    def __init__(self, lines: Sequence[_Line], *, extra_models: int = 0) -> None:
        self.lines: tuple[_Line, ...] = tuple(lines)
        self.extra_models = extra_models
        """被丢掉的模型数（第 2 套起）。

        **不是零才算异常**：NMR 结构常有十套，把它们并进一份受体里意味着同一个原子
        出现十次、盒子也变成十套坐标的包围盒。所以只留第一套，并把这个数报出去。
        """

    @property
    def atoms(self) -> tuple[AtomRecord, ...]:
        return tuple(line.atom for line in self.lines if line.atom is not None)

    @property
    def text(self) -> str:
        """把留下的行原样接起来。没被删的行 **逐字节** 等于原文。"""
        return "".join(line.text for line in self.lines)

    def group(self, key: ResidueKey) -> HeteroGroup | None:
        """按残基键取回一组原子（拿它去算盒子或抽配体）。"""
        atoms = tuple(atom for atom in self.atoms if atom.key == key)
        if not atoms:
            return None
        return HeteroGroup(key=key, resname=atoms[0].resname, atoms=atoms)

    def drop(self, indices: Collection[int]) -> PdbDocument:
        """删掉这些行号；**顺带清理引用了已删原子的 `CONECT`**。

        行号是 **原文件里的** 行号，删多少次都是同一套编号——不回退、不重算。
        这样「先删链 B 再删水」与「一次删两组」得到同一份结果，
        调用方也不必在第二次编辑前重新数一遍。
        """
        gone = set(indices)
        kept = [line for line in self.lines if line.index not in gone]
        alive = {line.atom.serial for line in kept if line.atom is not None}
        kept = [
            line
            for line in kept
            if line.kind != "conect" or all(serial in alive for serial in line.serials)
        ]
        return PdbDocument(kept, extra_models=self.extra_models)


def _read_atom(line: str, index: int) -> AtomRecord:
    """把一行坐标读成记录。**读不出就抛**，不跳过。

    跳过一行坐标的后果是「受体少一个原子」，而屏幕上什么都不会显示——等到对接结果
    不对时，没人会想到是这里少了原子。抛出的位置离病因最近。

    **这两条检查拦得住什么、拦不住什么，说清楚比说好听重要。**

    拦得住：整行太短、残基序号列不是整数、坐标列不是数字、坐标不是有限数。字段
    写超了导致后面整体位移（原子名五列、残基名四列这类）会在这里报出来，报出来的是
    「列位不是标准 PDB 列」而不是「有个数字读不出来」——前者才是用户能据以行动的话。

    拦不住：**整体只移一格的坐标**。8 列宽的 `%8.3f` 少读一格只是丢掉最后一位小数
    （0.001 埃），数值照旧合法，没有任何办法从一行里看出来。不去假装能拦——
    会误报的守卫等于没有守卫，而这条更糟：它报的是「模型有问题」，
    用户会去改模型，而真正的问题在列位。真怕这个就先拿 `parse` 的结果核一遍原子数。
    """
    kind = line[:6].strip()
    if len(line) < MIN_COORDINATE_LINE:
        raise PdbEditError(
            f"第 {index + 1} 行的 {kind} 记录只有 {len(line)} 列，读不到全部坐标"
            f"（至少要 {MIN_COORDINATE_LINE} 列）"
        )

    raw_number = line[ATOM_RESIDUE_NUMBER]
    try:
        int(raw_number.strip())
    except ValueError:
        raise PdbEditError(
            f"第 {index + 1} 行的残基序号列（23–26 列）读出来是 {raw_number!r}，不是整数。"
            "标准 PDB 的列位对不上——常见原因是某一列的内容写超了（原子名五列、"
            "残基名四列），于是它后面的字段整体位移。请先把列位规整回标准形式"
        ) from None

    try:
        x = float(line[ATOM_X])
        y = float(line[ATOM_Y])
        z = float(line[ATOM_Z])
    except ValueError:
        raise PdbEditError(
            f"第 {index + 1} 行的坐标列（31–54 列）读不出数字，这份文件不是标准 PDB 列位"
        ) from None
    if not all(math.isfinite(value) for value in (x, y, z)):
        raise PdbEditError(f"第 {index + 1} 行的坐标不是有限数（读出来是 {x}, {y}, {z}）")

    return AtomRecord(
        index=index,
        line=line,
        kind=kind,
        serial=line[ATOM_SERIAL].strip(),
        name=line[ATOM_NAME].strip(),
        altloc=line[ATOM_ALT_LOC].strip(),
        resname=line[ATOM_RESIDUE_NAME].strip(),
        chain=line[ATOM_CHAIN],
        number=raw_number.strip(),
        icode=line[ATOM_INSERTION_CODE].strip(),
        x=x,
        y=y,
        z=z,
        element=line[ATOM_ELEMENT].strip() if len(line) >= 78 else "",
    )


def parse(text: str) -> PdbDocument:
    """解析成一份文档。**只留第一套模型**，其余模型的坐标行丢掉并计数。

    非坐标行（`HEADER` / `CRYST1` / `REMARK` / `END` …）全部原样保留：
    它们不影响坐标，而 `CRYST1` 这类晶胞信息在下游可能有用，删了是白删。

    `CONECT` 单列出来，因为它是唯一一种「内容引用了别的行」的记录——删原子时必须
    连着它一起决定去留。
    """
    lines: list[_Line] = []
    in_first_model = True
    models = 0

    for index, line in enumerate(text.splitlines(keepends=True)):
        record = line[:6]
        if record == "MODEL ":
            models += 1
        elif record == "ENDMDL":
            in_first_model = False

        if record in ("ATOM  ", "HETATM"):
            if not in_first_model:
                continue
            lines.append(_Line(index, line, "atom", atom=_read_atom(line, index)))
            continue

        if record == "CONECT":
            serials = tuple(line[part].strip() for part in CONECT_SERIALS if line[part].strip())
            lines.append(_Line(index, line, "conect", serials=serials))
            continue

        lines.append(_Line(index, line, "other"))

    return PdbDocument(lines, extra_models=max(0, models - 1))


# ---------------------------------------------------------------------------
# 编辑操作
#
# 每个操作都是「文档进、文档出 + 一句说清动了什么」。返回被删掉的东西、而不是只返回
# 新文档，是因为调用方要把它写进体检报告——**用户看不到的就是没发生的**：
# 「删了 312 个水和 1 个配体」与「删了 313 个杂原子」在化学上是两件事。
# ---------------------------------------------------------------------------
def keep_chains(doc: PdbDocument, chains: Collection[str]) -> tuple[PdbDocument, dict[str, int]]:
    """只留这些链的坐标行。返回 (新文档, 被删掉的链 → 原子数)。

    **`ATOM` 与 `HETATM` 一起按链判**：两类记录用的是同一列；分开判只会让
    「链 B 的水留下了」这种半吊子状态出现，而过几行又会被去水那一步删掉——
    中间态没人看，但它是 bug 的温床。
    """
    wanted = set(chains)
    doomed: list[int] = []
    removed: dict[str, int] = {}
    for line in doc.lines:
        atom = line.atom
        if atom is None or atom.chain in wanted:
            continue
        doomed.append(line.index)
        removed[atom.chain] = removed.get(atom.chain, 0) + 1
    return doc.drop(doomed), removed


def hetero_groups(
    doc: PdbDocument, *, exclude_names: Collection[str] = ()
) -> tuple[HeteroGroup, ...]:
    """把所有非水杂原子残基分组列出来，按原子数降序。

    `exclude_names` 里的是 **不该当配体看** 的残基名（`MODRES` 登记过的那些，
    即修饰残基）。排除它们不是可选项：修饰残基写着 `HETATM` 却是聚合物的一部分，
    把 MSE 当配体抽走，等于给受体的骨架挖掉一个残基。
    """
    excluded = set(exclude_names)
    buckets: dict[ResidueKey, list[AtomRecord]] = {}
    for atom in doc.atoms:
        if not atom.is_hetero or atom.is_water or atom.resname in excluded:
            continue
        buckets.setdefault(atom.key, []).append(atom)
    return _sorted_groups(buckets)


def strip_hetero(
    doc: PdbDocument, *, keep_names: Collection[str] = ()
) -> tuple[PdbDocument, tuple[HeteroGroup, ...]]:
    """删掉全部 `HETATM` 行（`keep_names` 里的残基名除外）。

    `keep_names` 至少该含 `MODRES` 里登记过的残基名——见 `hetero_groups`。
    想留金属离子或糖基化位点也走这个口子。

    **水也是 `HETATM`**，所以这里一并删掉。晶体学结构里的水有两类身份：一部分是
    真实结合水（对接时常常该留），一部分是建模产物。默认删是行业惯例，但报告里必须
    写清删了多少——那是本次准备的一个化学决定，不是格式整理。这也是为什么返回值里
    带的是 **分组** 而不是一个计数。
    """
    keep = set(keep_names)
    doomed: list[int] = []
    buckets: dict[ResidueKey, list[AtomRecord]] = {}
    for line in doc.lines:
        atom = line.atom
        if atom is None or not atom.is_hetero or atom.resname in keep:
            continue
        doomed.append(line.index)
        buckets.setdefault(atom.key, []).append(atom)
    return doc.drop(doomed), _sorted_groups(buckets)


def take_group(doc: PdbDocument, group: HeteroGroup) -> tuple[PdbDocument, str]:
    """把一组残基的行抽出来并从文档里删掉。返回 (剩下的文档, 抽出来的原文)。

    抽出来的那段 **保持原样**（还是 `HETATM` 记录、还是那几列），因为它的去处有两个：
    一是「配体」文件，下游的配体准备认的正是 `HETATM`；二是 **盒子的依据**——
    盒子是从它的坐标算的，所以原文与盒子必须出自同一份坐标。
    """
    indices = {atom.index for atom in group.atoms}
    remaining = doc.drop(indices)
    detached = PdbDocument([line for line in doc.lines if line.index in indices])
    return remaining, detached.text


def altloc_residues(doc: PdbDocument) -> tuple[ResidueKey, ...]:
    """哪些残基带交替构象（按在文件里出现的顺序，去重）。"""
    seen: list[ResidueKey] = []
    for atom in doc.atoms:
        if atom.altloc and atom.key not in seen:
            seen.append(atom.key)
    return tuple(seen)


def select_altloc(
    doc: PdbDocument, wanted: str
) -> tuple[PdbDocument, dict[ResidueKey, str], tuple[ResidueKey, ...]]:
    """每个带交替构象的残基只留一个构象，**并把留下那些原子的 altLoc 列擦干净**。

    返回 (新文档, 每个残基选了哪个构象, 用不上 `wanted` 而退回的残基)。

    **选择是按残基做的，不是按整个文件。** 一个残基里只有 B 构象（没有 A）是合法的，
    那时若按全局字符过滤，这个残基会被整段删光——症状是下游「模板匹配缺原子」，
    而病因（构象没选上）离现场很远。所以残基内没有 `wanted` 时，退回它 **字典序最小
    的那个构象**，并把这件事报出来，让「我选了 A，但有两个残基其实是别的构象」可见。

    `altloc` 为空的原子（多数残基的骨架）**任何情况下都保留**——空表示「这个原子
    没有第二个位置」，不是「另一个构象」。

    **擦列这一步不是修饰，是必需的。** 只删掉 B 那份的话，留下的 A 行在第 17 列
    仍然写着 `A`，而 Meeko 判断「这个残基需要选构象」看的是这一列 **有没有字**、
    不是「还剩几份」——于是它会以 `Residues with alternate location` 拒收一份明明
    只有一个构象的文件（实测）。擦掉之后文件说的才是实话：这里只有一个位置。
    代价是改写每一行的一个字节，所以只动那一列，其余逐字节不动。
    """
    options: dict[ResidueKey, set[str]] = {}
    for atom in doc.atoms:
        if atom.altloc:
            options.setdefault(atom.key, set()).add(atom.altloc)
    if not options:
        return doc, {}, ()

    chosen: dict[ResidueKey, str] = {}
    fell_back: list[ResidueKey] = []
    for key, available in options.items():
        pick = wanted if wanted in available else sorted(available)[0]
        chosen[key] = pick
        if pick != wanted:
            fell_back.append(key)

    lines: list[_Line] = []
    for line in doc.lines:
        atom = line.atom
        if atom is None:
            lines.append(line)
            continue
        if atom.altloc and atom.altloc != chosen[atom.key]:
            continue
        if atom.key in chosen:
            text = _blank_altloc(line.text)
            lines.append(replace(line, text=text, atom=replace(atom, line=text, altloc="")))
            continue
        lines.append(line)
    return PdbDocument(lines, extra_models=doc.extra_models), chosen, tuple(fell_back)


def _blank_altloc(line: str) -> str:
    """把 altLoc 那一列改成空格。**只动这一列**，其余逐字节不动。"""
    return line[:ATOM_ALT_LOC] + " " + line[ATOM_ALT_LOC + 1 :]


def _sorted_groups(buckets: dict[ResidueKey, list[AtomRecord]]) -> tuple[HeteroGroup, ...]:
    groups = [
        HeteroGroup(key=key, resname=items[0].resname, atoms=tuple(items))
        for key, items in buckets.items()
    ]
    groups.sort(key=lambda group: (-group.size, group.resname, str(group.key)))
    return tuple(groups)


# ---------------------------------------------------------------------------
# 盒子
#
# 口径必须与对接引擎一致——盒子差一点，对接出来的位姿就跑到了别的位置。所以这里
# 是照着 Meeko 的 `calc_box` 重写的一份，并且有一条对拍测试拿 Meeko 自己的函数逐值比。
# 之所以重写成纯 Python：算盒子与化学无关，把它留在化学依赖里就没法在没装那套依赖的
# 机器上测。
# ---------------------------------------------------------------------------
def calc_box(
    coords: Iterable[Sequence[float]], padding: float
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """坐标的轴平行包围盒，四周各留 `padding` 埃。返回 (中心, 边长)。

    `padding` 取多少是 **方法学选择** 而不是实现细节：太小则配体伸不出去（Vina 会把
    贴边的位姿判成不可信），太大则搜索空间白涨。这条数会进 `params.json`。
    """
    points = [(float(c[0]), float(c[1]), float(c[2])) for c in coords]
    if not points:
        raise PdbEditError("算盒子时一个坐标都没有——包围盒无从谈起")

    center: list[float] = []
    size: list[float] = []
    for axis in range(3):
        values = [point[axis] for point in points]
        low = min(values)
        high = max(values)
        center.append((low + high) / 2.0)
        size.append(high - low + 2 * float(padding))
    return (center[0], center[1], center[2]), (size[0], size[1], size[2])


def format_box(center: Sequence[float], size: Sequence[float]) -> str:
    """Vina 的盒子文件内容（`center_x = …` 六行）。

    格式与 Meeko 的 `box_to_vina_string` 逐字符相同，这样用户手里两份盒子文件可以
    直接比对。**刻意不写 `#` 注释行**：这份文件是喂给引擎的，多一行注释就多一种
    「某个解析器版本不认」的可能，而那时的报错来自引擎、离病因很远。要说明就写在
    `params.json` 里。
    """
    lines = [f"center_{axis} = {center[i]:.3f}" for i, axis in enumerate("xyz")]
    lines += [f"size_{axis} = {size[i]:.3f}" for i, axis in enumerate("xyz")]
    return "\n".join(lines) + "\n"
