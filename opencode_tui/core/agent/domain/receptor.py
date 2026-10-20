"""准备受体：把一套实验坐标变成**可以直接喂给对接引擎的受体**，外加一个盒子。

上游的 `fetch_structure` / `import_structure` 拿来的是「原料」——一份包含水、离子、
共晶配体、可能好几条链、可能带交替构象的坐标文件。对接要的是「成品」：选定的链、
去掉水与杂原子、每个重原子都有正确的 AutoDock 原子类型与部分电荷、质子化态与键级都对，
再加上一个能框住结合位点的盒子。

**分工是这件事的核心。** 本模块负责选链、去水去杂、先取出共晶配体再删掉它、算盒子、
组织体检报告；Meeko（Forli lab，AutoDock 那一系）负责模板匹配 → 参数化 → PDBQT。
自己写原子类型/电荷指派不是代码量的问题，是**化学正确性**的问题。算盒子那部分是刻意的
例外：它纯粹是几何，留在化学依赖那一侧就白白让最该被测的代码依赖一百多兆的东西
（所以它在 `pdbtext.py` 里，零第三方依赖）。

四个设计决定值得单独说：

**严格起步，放宽要显式。** `relax=false` 时 Meeko 匹配不上的残基一律拒绝，
并把**是哪些残基**报出来，由用户决定要不要用 `relax=true` 丢掉它们。同一条道理还管着
交替构象：`altloc` 不给值时直接拒绝并列出是哪些残基带交替构象——**不替用户选构象**。

**两阶段确认在这里不是为了防危险，是为了把化学决定摆在动手之前。**
「删了 20 个水还是 21 个」「盒子以哪个配体为准」这类判断用户必须看见才能反对。
默认只做预演，什么都不写盘；确认之后才用 `confirm=true` 重来一次。

**参数即身份，产物目录按参数寻址。** 目录名里带参数的指纹，`params.json` 冻结全部参数
（含 Meeko 版本与盒子），所以同一组参数第二次调用直接复用、不重做；换一个参数就是
另一个目录，绝不会覆盖上一次的结果。

**预演与真做走同一条代码路径。** 预演不是「另算一遍」——它把文本编辑、盒子、Meeko
参数化全部真跑一遍，只是不写盘。所以预演报的原子数与真做写下的原子数必然是同一批，
不会出现「预演说删 20 个水、真做删了 21 个」。两份清单不一致时，**少报的那一侧是危险
的**，登记表那一层已经吃过一次。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ledger import SessionLedger, fingerprint_parts, fingerprint_path
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from ...file_log import record as log_read
from . import pdbtext
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem
from .pdbtext import HeteroGroup, PdbDocument, PdbEditError
from .structure import StructureFacts, parse_pdb

__all__ = ["PrepareReceptorTool", "prepared_receptor_dir", "domain_tools"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_PADDING = 4.0
"""盒子四周默认留多少埃。

这是**方法学选择**不是实现细节，所以它会进 `params.json`。太小则配体伸不出去，
引擎会把贴边的 pose 判成不可信；太大则搜索空间白涨。
"""

MAX_PADDING = 20.0
"""padding 的上限。

100 埃这种值只会来自笔误，而它的后果是搜索空间涨上百万倍、对接从几分钟变成几小时——
那种错误不该以「慢」的形式表现出来。
"""

WHOLE = "whole"
"""`ligand` 的特殊取值：用整份受体算盒子（盲对接）。"""

NONE = "none"
"""`ligand` 的特殊取值：不取配体，盒子只能显式给。"""

BIG_BOX_EDGE = 30.0
"""单边超过这个长度就提醒一句。不是硬限制，是「你可能不是故意要这么大」。"""

BIG_LIGAND_ATOMS = 100
"""共晶配体超过这么多原子就提醒一句（肽类抑制剂会让盒子意外地大）。"""

WATER_NAMES = ("HOH", "DOD", "WAT")

PARAMS_FILE = "params.json"
"""参数快照，同时也是**提交标记**——它最后写，所以「它存在」基本等于「产物齐了」。"""

RECEPTOR_PDB = "receptor.pdb"
RECEPTOR_PDBQT = "receptor.pdbqt"
LIGAND_PDB = "ligand.pdb"
BOX_FILE = "box.txt"
REPORT_FILE = "report.json"

MAX_PRODUCT_NAME_ATTEMPTS = 1000
"""同名产物目录最多往后找多少个序号。

不是为了防碰撞，是为了让「参数哈希前缀相同」这件事有个明确的终点：
撞满一千个说明这个目录该清理了，而不是继续往后堆。
"""

KNOWN_LIMITS = (
    "不生成 3D 坐标、不指派质子化态：它做的是按残基的模板匹配，输入里缺的原子不会凭空补出来",
    "补的氢没做力场优化，键长不是平衡值——不影响对接打分（打分用格点），影响能量分析",
    "重原子要逐个对上模板，少一个或多一个都算匹配失败（这正是严格模式会失败的原因）",
    "不支持 charmm-gui 那种加帽残基",
    "这里只出刚性受体，柔性残基（flexres）没有做",
)

COORDINATE_ERROR_HINT = (
    "这份坐标文件按标准 PDB 列位读不出来：先用 fetch_structure 重新取一份，"
    "或把本地文件规整成标准列位再 import_structure"
)


def prepared_receptor_dir() -> Path:
    """准备后受体的产物目录，与结构文件、登记表同处一个家目录。"""
    return Path.home() / ".opencode_tui" / "receptors"


# ---------------------------------------------------------------------------
# 参数解析
#
# 工具参数从模型那边过来都是字符串，所以「逗号还是空格分隔」这类宽容要在这里一次做完，
# 不能让下游各自猜——两处各自猜的话，「A,B」在一处算两条链、在另一处算一条。
# ---------------------------------------------------------------------------
def _tokens(raw: str) -> list[str]:
    """把 `"A, B"` / `"A B"` / `"A,B"` 切成 `["A", "B"]`。"""
    return [item for item in raw.replace(",", " ").split() if item]


def _chain_token(token: str) -> str:
    """把用户写的链名还原成文件里的链名。

    链 ID 可以是**空白**（老结构里常见），而空白没法在参数里打出来，所以约定 `_`
    与 `·` 都表示空白链。列表里显示成 `·`，输入收 `_`。
    """
    return " " if token in {"_", "·", "-"} else token


def _display_chain(chain: str) -> str:
    return chain or "·"


def _vector(raw: str, *, label: str) -> tuple[float, float, float]:
    """解析 `"1.0, 2, -3.5"` 这样的三分量。"""
    items = _tokens(raw)
    if len(items) != 3:
        raise ToolError(f"{label} 需要三个数（x, y, z），收到的是 {raw!r}")
    try:
        return (float(items[0]), float(items[1]), float(items[2]))
    except ValueError:
        raise ToolError(f"{label} 里的分量不是数字：{raw!r}") from None


def _relative(path: Path) -> str:
    """相对家目录展示，一律 POSIX 分隔符——反斜杠容易被原样抄进别处去拼接。"""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass
class Plan:
    """本次准备「打算做什么」。

    预演与实际执行读的是同一份，所以预演不会说谎——这是两阶段确认唯一站得住的理由。
    """

    chains: tuple[str, ...]
    keep_hetero: tuple[str, ...]
    modified_names: tuple[str, ...]
    ligand: HeteroGroup | None
    ligand_note: str
    box: tuple[tuple[float, float, float], tuple[float, float, float]]
    box_source: str
    padding: float
    altloc: str
    altloc_choices: dict[str, str]
    altloc_fell_back: tuple[str, ...]
    relax: bool

    def params(self, *, source_fingerprint: str, meeko_version: str) -> dict[str, Any]:
        """冻结成可比较的参数字典。**凡是能改变产物的东西都必须在这里。**

        盒子也在内：同一个受体配不同的盒子是两份不同的产物，漏掉它就会出现「换了盒子
        却复用了旧结果」——而那份结果的 pose 在别处，看上去完全正常。Meeko 版本也在内：
        升级 Meeko 会改变原子类型与电荷的指派，那时旧产物不该被当成同一件事。

        **`altloc_choices` 也在内**，而它对同一份坐标是确定的（同样的目标字符算出同样的
        选择）——放进来是为了让「这份产物到底取了哪些构象」在参数快照里就能看见，
        而不是要翻 report。
        """
        return {
            "source_fingerprint": source_fingerprint,
            "chains": list(self.chains),
            "keep_hetero": list(self.keep_hetero),
            "ligand": self.ligand.label if self.ligand else None,
            "box_center": list(self.box[0]),
            "box_size": list(self.box[1]),
            "box_source": self.box_source,
            "padding": self.padding,
            "altloc": self.altloc,
            "altloc_choices": self.altloc_choices,
            "relax": self.relax,
            "meeko": meeko_version,
        }


# ---------------------------------------------------------------------------
# Meeko
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Parameterization:
    """Meeko 干完活之后的产物与它的自述。"""

    pdbqt: str
    atoms: int
    monomers: int
    ignored: tuple[str, ...]
    residues_with_added_h: int
    residues_with_excess_h: tuple[str, ...]
    excess_bonds: tuple[str, ...]
    chosen_by_fewest_missing_h: tuple[str, ...]


class ChemRejection(Exception):
    """Meeko 拒绝了这份输入，而这是**可操作的**——不是内部错误。"""

    def __init__(self, error: str, recommendations: str) -> None:
        super().__init__(error)
        self.error = error
        self.recommendations = recommendations


def _meeko():
    """延迟导入 Meeko。顶层不许 import 它——那会让可选依赖有能力拖垮整条 CLI。"""
    import meeko

    return meeko


def _meeko_version() -> str:
    try:
        return str(getattr(_meeko(), "__version__", "unknown"))
    except ImportError:  # pragma: no cover - 依赖闸已经拦在前面
        return "unknown"


_TEMPLATES: Any = None


def _templates():
    """按残基匹配用的化学模板，**按进程缓存一次**。

    实测加载要一秒出头（八百多个模板），而它在一份受体上是只读的——每次调用重来一遍
    纯属浪费。缓存的前提是「一个会话里依赖不会变」，与安装提示里那句「装完重启会话」
    是同一个前提。
    """
    global _TEMPLATES
    if _TEMPLATES is None:
        _TEMPLATES = _meeko().ResidueChemTemplates.create_from_defaults()
    return _TEMPLATES


def parameterize(receptor_text: str, *, relax: bool) -> Parameterization:
    """把受体坐标交给 Meeko：模板匹配 → 参数化 → PDBQT。

    **进程内调用，不走子进程调 `mk_prepare_receptor.py`。** 官方 CLI 在参数化之前
    能额外做的事（选链、取共晶配体、选交替构象、算盒子、写日志）本模块都已经自己做了，
    走子进程只会多出一层「解析它的人话输出」的耦合，要探测的失败模式反而更多。
    装配顺序与官方 CLI 一致（`create_from_defaults` → `from_config` →
    `from_pdb_string` → `write_from_polymer`），这样它的默认行为就是我们的默认行为。

    `allow_bad_res` 就是 `relax`：`False` 时匹配不上的残基让 Meeko 抛
    `ChemRejection`，其中带**具体是哪些残基**——这是刻意留的接口，用户看了那份清单
    才好决定要不要放宽。

    **交替构象在这里不做**（不给 `default_altloc`）：构象由 `pdbtext` 在文本层面就选定
    并删掉另一份，所以进到 Meeko 的坐标已经只有一个构象。这样 Meeko 的
    `res_needed_altloc` 就退成一张**备份网**——万一我们的过滤漏了，它会拒绝而不是
    替我们猜，而「它替我们猜」正是最坏的结果。
    """
    meeko = _meeko()
    config = meeko.MoleculePreparation.from_config({})
    try:
        polymer = meeko.Polymer.from_pdb_string(
            receptor_text,
            _templates(),
            config,
            allow_bad_res=relax,
        )
    except meeko.PolymerCreationError as exc:
        # .error 是「具体哪些残基出了什么问题」，.recommendations 是它给的两条路。
        # 直接取属性而不是解析 str(exc)：后者会把调用链一起带上，
        # 而且措辞一变我们的解析就会安静地失效。
        raise ChemRejection(
            str(getattr(exc, "error", exc)), str(getattr(exc, "recommendations", ""))
        ) from None

    rigid, flexible = meeko.PDBQTWriterLegacy.write_from_polymer(polymer)
    if flexible:
        # 只出刚性受体，所以这里不该有东西。真有的话说明 Meeko 的行为变了，
        # 而静默丢掉柔性残基等于给用户一份与他以为的不一样的受体。
        raise ToolError(
            "Meeko 给出了柔性残基的 PDBQT，而本工具只做刚性受体——"
            "说明 Meeko 的行为变了。请报告这个情况，不要直接用这份产物"
        )

    log = polymer.log
    anomalies = log.get("matched_with_H_anomaly", {})
    return Parameterization(
        pdbqt=rigid,
        atoms=sum(1 for line in rigid.splitlines() if line.startswith("ATOM")),
        monomers=len(polymer.get_valid_monomers()),
        ignored=tuple(str(key) for key in polymer.get_ignored_monomers()),
        residues_with_added_h=sum(
            1 for info in anomalies.values() if info[1].get("H_miss", 0) > 0
        ),
        residues_with_excess_h=tuple(
            key for key, info in anomalies.items() if info[1].get("H_excess", 0) > 0
        ),
        excess_bonds=tuple(str(key) for key in log.get("matched_with_excess_bond", ())),
        chosen_by_fewest_missing_h=tuple(
            str(key) for key in log.get("chosen_by_fewest_missing_H", ())
        ),
    )


# ---------------------------------------------------------------------------
# 编辑计划的推演
# ---------------------------------------------------------------------------
@dataclass
class Built:
    """一份文本编辑的结果：计划 + 真做出来的文档 + 删掉的东西。"""

    plan: Plan
    doc: PdbDocument
    ligand_text: str
    chains_dropped: dict[str, int]
    hetero_removed: tuple[HeteroGroup, ...]
    extra_models: int


def _select_chains(
    facts: StructureFacts, requested: Sequence[str], raw: str
) -> tuple[str, ...]:
    """决定留哪些链。

    **多链且没指定时必须拒绝。** 同源二聚体的两条链几乎一样，随便挑一条会让对接跑在
    对的位置之外的地方，而「跑完了、pose 也像样」这件事本身不会提示选错了链。
    """
    available = [str(chain.chain_id) for chain in facts.chains]
    if not available:
        raise ToolError("这套结构里没有聚合物链（只有杂原子），准备不出受体")

    if raw.strip().lower() == "all":
        return tuple(available)

    if not requested:
        if len(available) == 1:
            return (available[0],)
        raise ToolError(
            f"这套结构有 {len(available)} 条聚合物链，用哪条链必须显式指定。"
            f"各条链：{_chain_summary(facts)}。"
            "chain 里给链名（多条用逗号分隔，全要写 chain=all）"
        )

    wanted = tuple(_chain_token(token) for token in requested)
    unknown = [chain for chain in wanted if chain not in available]
    if unknown:
        listed = "、".join(_display_chain(chain) for chain in available)
        raise ToolError(
            f"这套结构里没有链 {'、'.join(_display_chain(chain) for chain in unknown)}；"
            f"可用的聚合物链是 {listed}（空白链写 _）"
        )
    return wanted


def _chain_summary(facts: StructureFacts) -> str:
    """可读的链清单：`A（312 残基，残基 672–1001）`。"""
    parts = []
    for chain in facts.chains:
        span = ""
        if chain.first_residue is not None and chain.last_residue is not None:
            span = f"，残基 {chain.first_residue}–{chain.last_residue}"
        parts.append(
            f"{_display_chain(str(chain.chain_id))}（{chain.observed_residues} 残基{span}）"
        )
    return "；".join(parts)


def _resolve_ligand(
    groups: Sequence[HeteroGroup], raw: str
) -> tuple[HeteroGroup | None, str]:
    """决定用哪一组杂原子残基当共晶配体（它同时是盒子的依据）。

    **歧义一律拒绝而不是挑一个。** 晶体学结构里常有好几个小分子（缓冲液、离子、
    冷冻保护剂），挑中体积最大的那个恰好是配体的概率并不高；而选错的表现只是
    「盒子位置不对」——对接照跑、结果照出，没有任何地方会报错。
    """
    spec = raw.strip()
    if spec.lower() == NONE:
        return None, "按参数要求不取共晶配体"

    if spec:
        resname, _, chain = spec.partition(":")
        candidates = [group for group in groups if group.resname == resname.strip()]
        if chain.strip():
            wanted = _chain_token(chain.strip())
            candidates = [group for group in candidates if group.key.chain == wanted]
        if not candidates:
            listed = "、".join(group.label for group in groups[:8]) or "（一组都没有）"
            raise ToolError(
                f"没找到残基名是 {resname.strip()!r} 的非水杂原子残基；"
                f"这套结构里有的是 {listed}"
            )
        if len(candidates) > 1:
            listed = "、".join(group.label for group in candidates)
            first = candidates[0]
            raise ToolError(
                f"{resname.strip()!r} 对应不止一组（{listed}），请用 "
                f"ligand=残基名:链 指定，例如 "
                f"ligand={first.resname}:{_display_chain(first.key.chain)}"
            )
        return candidates[0], ""

    if not groups:
        return None, "这套结构里没有非水的杂原子残基"

    if len(groups) == 1:
        return groups[0], f"自动取上唯一的那一组（{groups[0].label}）"

    listed = "、".join(f"{group.label}（{group.size} 原子）" for group in groups[:8])
    raise ToolError(
        f"这套结构里有 {len(groups)} 组非水杂原子残基，没法自动认定哪个是配体：{listed}。"
        f"请用 ligand 指定残基名（例如 ligand={groups[0].resname}）；"
        f"要盲对接就用 ligand={WHOLE}；确实不需要配体就写 ligand={NONE}"
    )


def _build(
    text: str,
    *,
    chain_raw: str,
    ligand_raw: str,
    keep_raw: str,
    center_raw: str,
    size_raw: str,
    padding: float,
    altloc: str,
    relax: bool,
) -> Built:
    """推演出完整的编辑计划，并**真的做一遍纯文本编辑**（不碰 Meeko、不写盘）。

    编辑结果直接拿去做参数化，所以「预演报的原子数」就是「真做写下的原子数」。
    """
    facts = parse_pdb(text)
    try:
        doc = pdbtext.parse(text)
    except PdbEditError as exc:
        raise ToolError(f"{exc}。{COORDINATE_ERROR_HINT}") from None

    # **两份解析对不上时报列位那一条**：`structure.parse_pdb` 对短行是宽容的（它只出摘要，
    # 列位不够就跳过），而 `pdbtext.parse` 是严格的。于是「有一堆 ATOM 行、但一条聚合物链
    # 都没认出来」既可能是「这份文件真的只有杂原子」，也可能是「列位整体错位」——
    # 后者更常见，而它的病因（列位）在别的报错里看不出来。
    if not facts.chains and doc.atoms:
        raise ToolError(
            "这份文件里有坐标行，但按标准 PDB 列位读不出一条聚合物链。"
            f"{COORDINATE_ERROR_HINT}"
        )

    chains = _select_chains(facts, _tokens(chain_raw), chain_raw)

    extra_models = doc.extra_models

    doc, chains_dropped = pdbtext.keep_chains(doc, chains)

    # 交替构象在**文本层面**就选掉：选完之后进 Meeko 的坐标只有一个构象。
    # 放在这一步（而不是交给 Meeko 的 default_altloc）有两个好处：
    # 一是 receptor.pdb 自己就是干净的、能直接给别的工具用（留着两份构象的话，
    # 那些工具多半会照单全收，然后得到一份原子重复的结构）；
    # 二是「构象由用户选」这件事变成了我们自己的、能离线测的逻辑，
    # 而不是一条要解析 Meeko 报错措辞的间接行为。
    altloc_keys = pdbtext.altloc_residues(doc)
    if altloc_keys and not altloc:
        listed = "、".join(str(key) for key in altloc_keys[:12])
        raise ToolError(
            f"这套结构里有 {len(altloc_keys)} 个残基带交替构象：{listed}。"
            "同一条链上它们采了两个位置，取哪个是化学决定，本工具不替你做——"
            '给 altloc 指定一个构象（绝大多数情况是 altloc="A"）'
        )
    altloc_choices: dict[str, str] = {}
    altloc_fell_back: tuple[str, ...] = ()
    if altloc_keys:
        doc, chosen, fell_back = pdbtext.select_altloc(doc, altloc)
        altloc_choices = {str(key): value for key, value in chosen.items()}
        altloc_fell_back = tuple(str(key) for key in fell_back)

    # MODRES 登记过的残基写着 HETATM 却是聚合物的一部分，**必须留下**：把它们当杂原子
    # 删掉就是给骨架挖掉一个残基，而下游的报错会指向别处（模板匹配失败），
    # 病因与现场隔着好几步。
    modified_names = tuple(name for name, _ in facts.modified)
    keep_names = tuple(dict.fromkeys([*modified_names, *_tokens(keep_raw)]))

    groups = pdbtext.hetero_groups(doc, exclude_names=modified_names)
    if ligand_raw.strip().lower() == WHOLE:
        ligand, ligand_note = None, "按参数用整份受体算盒子（盲对接）"
    else:
        ligand, ligand_note = _resolve_ligand(groups, ligand_raw)

    ligand_text = ""
    if ligand is not None:
        doc, ligand_text = pdbtext.take_group(doc, ligand)

    doc, removed = pdbtext.strip_hetero(doc, keep_names=keep_names)
    if not doc.atoms:
        # 走到这里说明链选对了、但删完之后一个原子都不剩。下游会「成功地」产出一份
        # 空受体——一份没有任何原子的 PDBQT 在语义上是可解析的，于是对接会给出
        # 一个看起来正常的分数区间。宁可在这里停住。
        raise ToolError(
            "按这些参数删完之后受体一个原子都不剩了（选中的链里没有 ATOM 记录？）。"
            f"这份结构的链清单：{_chain_summary(facts)}"
        )

    # 盒子：显式给的优先，其次共晶配体，最后才是整份受体（盲对接）。
    # 顺序不能反——由配体算出来的盒子是「结合位点」，由整份受体算出来的是「整张蛋白」，
    # 后者会让对接在几十埃的范围里瞎找。
    if center_raw.strip() or size_raw.strip():
        if not (center_raw.strip() and size_raw.strip()):
            raise ToolError("盒子要成对给：box_center 与 box_size 必须同时给")
        box = (
            _vector(center_raw, label="box_center"),
            _vector(size_raw, label="box_size"),
        )
        source = "显式给出"
    elif ligand is not None:
        box = pdbtext.calc_box(ligand.coords, padding)
        source = f"共晶配体 {ligand.label}"
    elif ligand_raw.strip().lower() == WHOLE:
        box = pdbtext.calc_box([atom.coord for atom in doc.atoms], padding)
        source = "整份受体（盲对接）"
    else:
        raise ToolError(
            f"没有盒子可用：{ligand_note or '没找到可用的共晶配体'}。"
            "给 box_center 与 box_size 显式指定，或者用 ligand=whole 让整份受体算"
            "（盲对接，搜索空间会很大）"
        )

    return Built(
        plan=Plan(
            chains=chains,
            keep_hetero=keep_names,
            modified_names=modified_names,
            ligand=ligand,
            ligand_note=ligand_note,
            box=box,
            box_source=source,
            padding=padding,
            altloc=altloc,
            altloc_choices=altloc_choices,
            altloc_fell_back=altloc_fell_back,
            relax=relax,
        ),
        doc=doc,
        ligand_text=ligand_text,
        chains_dropped=chains_dropped,
        hetero_removed=removed,
        extra_models=extra_models,
    )


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------
def _read_params(directory: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((directory / PARAMS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def has_product(directory: Path) -> bool:
    """产物是否齐全。

    `params.json` 是**最后**写的，所以它存在基本就等于「写完了」；这里仍逐个点一遍文件，
    是因为「基本」不够——漏一个文件的表现是下游读不到受体，而那时离病因（上次写到一半）
    已经很远。
    """
    return all(
        (directory / name).is_file()
        for name in (PARAMS_FILE, REPORT_FILE, RECEPTOR_PDBQT, RECEPTOR_PDB)
    )


def _product_dir(
    directory: Path,
    stem: str,
    chains: Sequence[str],
    digest: str,
    params: dict[str, Any],
) -> tuple[Path, bool]:
    """挑产物目录，返回 (目录, 能不能直接复用)。

    目录名的可读部分（`1M17-A-3f9c1a2b`）只是给人看的，**真正的身份是 `params.json`**：
    撞了名字、或者参数哈希的前缀相同，就比对参数，不一致就换一个目录名。
    覆盖是绝对不行的——被覆盖掉的是另一组参数下算出来的受体，而它可能已经派出去做过对接了。

    参数一致但产物不齐（上次写到一半崩了）时**也换目录**：在半成品上接着写，
    会让人分不清哪些文件是这一次的。
    """
    directory.mkdir(parents=True, exist_ok=True)
    chain_tag = "+".join(_display_chain(chain) for chain in chains) or "x"
    safe = _safe_stem(stem)
    base = f"{safe}-{chain_tag}-{digest}"

    for index in range(MAX_PRODUCT_NAME_ATTEMPTS):
        target = directory / (base if index == 0 else f"{base}-{index}")
        if not target.exists():
            return target, False
        if _read_params(target) == params and has_product(target):
            return target, True
    raise ToolError(
        f"{directory} 下有太多同名目录（{base} 系列），无法安全写盘。"
        "请清理这个目录里不再需要的准备结果"
    )


def _safe_stem(stem: str) -> str:
    """把来源文件名整理成能进路径的一段。

    延迟拿 `structure.UNSAFE_STEM`：这个模块的顶层已经 import 了 `structure`，
    这里只是为了让「哪个模块定义了这个白名单」在正文里一眼看得见。
    """
    from .structure import UNSAFE_STEM

    return UNSAFE_STEM.sub("_", stem).strip("._-") or "receptor"


def _write_product(
    directory: Path,
    *,
    receptor_text: str,
    ligand_text: str,
    box: tuple[tuple[float, float, float], tuple[float, float, float]],
    pdbqt: str,
    params: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """按固定顺序写盘；`params.json` 放最后当提交标记。"""
    directory.mkdir(parents=True, exist_ok=True)
    _write_text(directory / RECEPTOR_PDB, receptor_text)
    if ligand_text:
        _write_text(directory / LIGAND_PDB, ligand_text)
    _write_text(directory / BOX_FILE, pdbtext.format_box(*box))
    _write_text(directory / RECEPTOR_PDBQT, pdbqt)
    _write_text(
        directory / REPORT_FILE, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    _write_text(
        directory / PARAMS_FILE, json.dumps(params, ensure_ascii=False, indent=2) + "\n"
    )


def _write_text(path: Path, text: str) -> None:
    """一律 UTF-8 + `\\n` 写盘。

    换行不做平台转换（`newline=""`）：产物是给别的程序读的，在一台机器上生成、
    在另一台上读，行尾不该随生成机器变。
    """
    path.write_text(text, encoding="utf-8", newline="")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
@dataclass
class Situation:
    """一次准备里所有要说清楚的事实。渲染与写盘都从这里取。"""

    source_handle: str
    source_display: str
    source_fingerprint: str
    stage: str
    chains_kept: tuple[str, ...]
    altloc: str
    altloc_choices: dict[str, str] = field(default_factory=dict)
    altloc_fell_back: tuple[str, ...] = ()
    chains_dropped: dict[str, int] = field(default_factory=dict)
    waters: int = 0
    water_atoms: int = 0
    hetero_removed: tuple[HeteroGroup, ...] = ()
    hetero_kept: tuple[str, ...] = ()
    extra_models: int = 0
    ligand_label: str = ""
    ligand_atoms: int = 0
    ligand_note: str = ""
    ligand_written: bool = False
    box_center: tuple[float, float, float] | None = None
    box_size: tuple[float, float, float] | None = None
    box_source: str = ""
    receptor_atoms: int = 0
    receptor_residues: int = 0
    result: Parameterization | None = None
    product_dir: Path | None = None
    registered: str = ""
    reused: bool = False


def _count_span(groups: Sequence[HeteroGroup]) -> str:
    """`2 组（GOL ×1、PO4 ×1）` 这样的一行。"""
    names: dict[str, int] = {}
    for group in groups:
        names[group.resname] = names.get(group.resname, 0) + 1
    detail = "、".join(f"{name} ×{count}" for name, count in sorted(names.items()))
    return f"{len(groups)} 组（{detail}）"


def _situation_lines(situation: Situation) -> list[str]:
    lines = [
        f"来源：{situation.source_display}（{situation.source_handle}，"
        f"状态 {situation.stage}，指纹 {situation.source_fingerprint}）",
        f"链：{'、'.join(_display_chain(chain) for chain in situation.chains_kept)}",
    ]
    if situation.chains_dropped:
        dropped = "、".join(
            f"{_display_chain(chain)}（{count} 原子）"
            for chain, count in sorted(situation.chains_dropped.items())
        )
        lines.append(f"去掉的链：{dropped}")
    if situation.extra_models:
        lines.append(f"丢掉的多余模型：{situation.extra_models} 套（只留第一套）")

    lines.append(f"去掉的水：{situation.waters} 个残基 / {situation.water_atoms} 个原子")
    if situation.hetero_removed:
        lines.append(f"去掉的其他杂原子：{_count_span(situation.hetero_removed)}")
    kept = [name for name in situation.hetero_kept if name]
    if kept:
        lines.append(f"保留的杂原子残基名：{'、'.join(kept)}")

    if situation.altloc_choices:
        taken = "、".join(
            f"{key}→{value}" for key, value in sorted(situation.altloc_choices.items())
        )
        lines.append(
            f"交替构象（{len(situation.altloc_choices)} 个残基）：{taken}"
            f"　指定的是 {situation.altloc} 构象，另一份坐标已从 receptor.pdb 里删掉"
        )
        if situation.altloc_fell_back:
            failed = "、".join(situation.altloc_fell_back)
            lines.append(
                f"　其中 {failed} 里没有 {situation.altloc} 构象，退回了它自己最小的那个"
                "——想确认取的是不是你要的那个，看上面"
            )

    if situation.ligand_label:
        lines.append(
            f"共晶配体：{situation.ligand_label}，{situation.ligand_atoms} 个原子"
            "（单独写成 ligand.pdb，可用于复现或做重对接的参照）"
        )
    elif situation.ligand_note:
        lines.append(f"共晶配体：不取（{situation.ligand_note}）")

    if situation.box_center is not None and situation.box_size is not None:
        center = ", ".join(f"{value:.3f}" for value in situation.box_center)
        size = ", ".join(f"{value:.3f}" for value in situation.box_size)
        lines.append(f"盒子中心：({center})")
        lines.append(f"盒子边长：({size})　来源：{situation.box_source}")

    lines.append(
        f"受体：{situation.receptor_residues} 个残基、{situation.receptor_atoms} 个坐标行"
        "（写进 receptor.pdb；原文件的表头与 REMARK 一并保留，氢在 pdbqt 里）"
    )
    return lines


def _meeko_lines(situation: Situation) -> list[str]:
    result = situation.result
    assert result is not None
    lines = [
        f"参数化：{result.monomers} 个单体 → {result.atoms} 个原子的 PDBQT"
        f"（{len(result.pdbqt)} 字符），AutoDock 原子类型与部分电荷已写入"
    ]
    if result.ignored:
        lines.append(f"被忽略的单体：{'、'.join(result.ignored)}")
    lines.append(
        f"补氢：{result.residues_with_added_h} 个残基按模板补齐"
        "（实验结构里通常本来就没有氢，这属正常）"
    )
    if result.residues_with_excess_h:
        lines.append(
            f"氢超量的残基：{'、'.join(result.residues_with_excess_h)}"
            "　输入里的氢比模板多，质子化态可能不对，值得单独看一眼"
        )
    if result.excess_bonds:
        lines.append(f"多出的残基间键：{'、'.join(result.excess_bonds)}")
    if result.chosen_by_fewest_missing_h:
        lines.append(
            f"靠「缺氢最少」定下模板的残基：{'、'.join(result.chosen_by_fewest_missing_h)}"
            "　它们匹配到不止一个模板，选中的不一定对"
        )
    return lines


def _caution_lines(situation: Situation) -> list[str]:
    """值得提醒但不该拦住的事。"""
    lines: list[str] = []
    if situation.ligand_atoms > BIG_LIGAND_ATOMS:
        lines.append(
            f"配体有 {situation.ligand_atoms} 个原子，盒子可能比预期大很多——"
            "确认它确实是要对接的那个配体（肽类抑制剂常被写成一条链而不是杂原子）"
        )
    if situation.box_size is not None and max(situation.box_size) > BIG_BOX_EDGE:
        lines.append(
            f"盒子最长边 {max(situation.box_size):.1f} 埃（超过 {BIG_BOX_EDGE:.0f}）——"
            "搜索空间大、会慢很多；对接时把盒子收到结合位点附近"
        )
    return lines


def _known_limit_lines() -> list[str]:
    return ["Meeko 不做的事（别指望它）：", *[f"  · {item}" for item in KNOWN_LIMITS]]


def _assemble(title: str, blocks: Sequence[Sequence[str]], tail: Sequence[str]) -> str:
    out: list[str] = [title]
    for block in blocks:
        if not block:
            continue
        out.append("")
        out.extend(block)
    out.append("")
    out.extend(tail)
    return "\n".join(out)


def _render(situation: Situation, *, title: str, tail: Sequence[str]) -> str:
    blocks: list[Sequence[str]] = [_situation_lines(situation)]
    if situation.result is not None:
        blocks.append(_meeko_lines(situation))
    blocks.append(_caution_lines(situation))
    blocks.append(_known_limit_lines())
    return _assemble(title, blocks, tail)


def _render_rejection(situation: Situation, rejection: ChemRejection) -> str:
    body: list[str] = [
        "Meeko 拒绝了这份输入（它列的是具体问题，不是内部错误）：",
        "",
        *[f"  {line}" for line in rejection.error.splitlines() if line.strip()],
    ]
    if rejection.recommendations:
        body.extend(
            [
                "",
                "Meeko 自己的两条建议（它保守一些，第二条更稳妥）：",
                *[
                    f"  {line}"
                    for line in rejection.recommendations.splitlines()
                    if line.strip()
                ],
            ]
        )
    body.extend(
        [
            "",
            "可以这样往下走：",
            "  · 报出来的是交替构象（alternate location）时，说明这些残基采了两个构象。"
            '通常取 A 构象，用 altloc="A" 再来一次——注意它是全局选择',
            "  · 报出来的是模板匹配失败时，先看那些残基是什么（修饰残基、非标准残基、"
            "被截断的侧链）。确认可以丢掉再用 relax=true 跳过它们",
            "  · 也可以换一条链、换一套结构：同一个蛋白的几套结构，覆盖的序列段往往不一样",
        ]
    )
    return _assemble(
        "受体准备（预演）：这份输入被 Meeko 拒绝，没有写盘任何东西",
        # 「Meeko 不做的事」在**这一条路上最该出现**：模板匹配为什么失败、为什么补不了
        # 缺的原子，答案就在那几句话里。少了它，用户只会觉得工具坏了。
        [_situation_lines(situation), body, _known_limit_lines()],
        ["下一步：按上面的提示改一个参数再来一次。带 confirm=true 才会真正写盘。"],
    )


def _render_preview(situation: Situation) -> str:
    assert situation.product_dir is not None
    return _render(
        situation,
        title="受体准备（预演）：下面是这次会做的事，什么都没有写盘",
        tail=[
            f"下一步：确认无误后用 confirm=true 再来一次。产物会写到 "
            f"{_relative(situation.product_dir)}，目录名里带着参数指纹，"
            "同一组参数第二次直接复用",
        ],
    )


def _render_done(situation: Situation) -> str:
    assert situation.product_dir is not None
    title = (
        "受体准备（已存在同一组参数的产物，直接复用，没有重做）"
        if situation.reused
        else "受体准备（完成）"
    )
    return _render(
        situation,
        title=title,
        tail=[f"产物目录：{_relative(situation.product_dir)}", situation.registered],
    )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class PrepareReceptorTool(Tool):
    """把一套实验坐标准备成可以直接对接的受体。"""

    spec = ToolSpec(
        name="prepare_receptor",
        description=(
            "在用户没有充分了解受体以及确认结构前，不可调用。"
            "把一套实验坐标准备成**可以直接对接的受体**：选链、去水去杂、取出共晶配体、"
            "算盒子，再做按残基的模板匹配与 PDBQT 参数化。产物是一个目录（规整后的 "
            "receptor.pdb、受体 receptor.pdbqt、共晶配体 ligand.pdb、盒子 box.txt、"
            "参数 params.json、体检报告 report.json），并登记成一条**状态为 prepared "
            "的受体**。默认只做**预演**：把「会删掉哪些水与杂原子、盒子算在哪、哪些残基"
            "匹配不上」摆出来，什么都不写盘；确认之后再用 confirm=true 重来一次。"
            "**严格起步**：匹配不上的残基会被拒绝并列出具体是哪些，由你决定要不要用 "
            "relax=true 丢掉；交替构象也必须显式选（altloc）。这个工具**只出刚性受体**，"
            "也不生成 3D 坐标、不指派质子化态——它做的是模板匹配。"
            "上游要先用 fetch_structure 或 import_structure 拿到状态为原始结构的受体。"
            f"{CHEM_TOOL_DESCRIPTION_NOTE}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "structure": {
                    "type": "string",
                    "description": "原始结构的 handle（状态 raw 的受体），例如 receptor#1",
                },
                "chain": {
                    "type": "string",
                    "description": "留哪条链：一个字母 / 逗号分隔的多个 / all；单链结构可留空",
                },
                "ligand": {
                    "type": "string",
                    "description": "共晶配体：残基名或 残基名:链；whole 盲对接；none 不取",
                },
                "box_center": {
                    "type": "string",
                    "description": "盒子中心，例如 \"21.7, 0.3, 52.1\"，与 box_size 成对给",
                },
                "box_size": {
                    "type": "string",
                    "description": "盒子边长（埃），例如 \"24, 13, 15\"",
                },
                "padding": {
                    "type": "number",
                    "description": f"由配体算盒子时四周各留多少埃，默认 {DEFAULT_PADDING:g}，上限 {MAX_PADDING:g}",
                },
                "altloc": {
                    "type": "string",
                    "description": "交替构象取哪一个，单个字符（通常是 A）；留空则在有交替构象时拒绝",
                },
                "keep_hetero": {
                    "type": "string",
                    "description": "要保留的杂原子残基名，逗号分隔（金属离子、糖基化位点）",
                },
                "relax": {
                    "type": "boolean",
                    "description": "true 时允许丢掉模板匹配不上的残基，默认 false（拒绝并报告）",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "true 才真正写盘与登记，默认 false 只做预演",
                },
            },
            "required": ["structure"],
        },
    )
    primary_argument = "structure"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output=(
                    "内核装配问题：prepare_receptor 需要一个会话登记表，"
                    "但这次调用没有拿到。"
                ),
                failed=True,
            )

        # 依赖闸放在参数校验**之后**吗？不——放在最前面，而且理由是它更早：
        # 缺依赖时连参数都不必看，用户要做的事只有一件（装依赖）。
        # 但要把「参数写错」与「依赖没装」分开，所以这一句单独成一档失败。
        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        padding = arguments.get("padding")
        if padding is None:
            padding = DEFAULT_PADDING
        if not isinstance(padding, (int, float)) or isinstance(padding, bool):
            return ToolResult(output="参数 padding 应当是数字", failed=True)
        padding = float(padding)
        if padding < 0 or padding > MAX_PADDING:
            return ToolResult(
                output=(
                    f"padding 要在 0 到 {MAX_PADDING:g} 之间，收到 {padding:g}。"
                    "盒子尺寸随它平方增长，笔误的代价是对接慢上几个数量级"
                ),
                failed=True,
            )

        altloc = str(arguments.get("altloc") or "")
        if len(altloc) > 1:
            return ToolResult(
                output=f"altloc 只能是单个字符（例如 A），收到的是 {altloc!r}", failed=True
            )

        raw_handle = str(arguments.get("structure") or "")
        try:
            entry = ledger.get(raw_handle)
        except Exception as error:  # LedgerError —— 说明文案由登记表给
            return ToolResult(output=str(error), failed=True)

        # 类型对不上交给登记表那层判：它给的是「这个参数要的是 receptor，但收到
        # artifact#1」这样的中文，比在这里再写一遍判断准。
        from ..ledger import ReceptorHandle, expect_handle

        try:
            expect_handle(entry, ReceptorHandle, "structure")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        stage = str(entry.meta.get("stage", "raw"))
        if stage == "prepared":
            return ToolResult(
                output=(
                    f"{entry.handle} 是已经准备过的受体（状态 prepared），不能再准备一次——"
                    "它的坐标已经不含水与配体，再走一遍盒子就没了依据。"
                    "要换参数请回到原始结构（状态 raw 的那一条受体）上重做"
                ),
                failed=True,
            )
        if entry.path is None or not entry.path.is_file():
            # 目录也要单独说一句：prepared 的受体登记的是一条**产物目录**，
            # 走到这里又没被上面那条拦住时，说「文件不在了」是错的——它明明在，
            # 只是不是坐标文件。诊断说错，用户就会去错的地方找。
            if entry.path is not None and entry.path.is_dir():
                return ToolResult(
                    output=(
                        f"{entry.handle} 指向的是一个目录（{entry.path}），不是坐标文件。"
                        "准备受体要的是一份**原始结构的 .pdb**（状态 raw 的那一条受体）；"
                        "若这是一条准备后的受体，用 list_workspace 看它的状态是 prepared"
                    ),
                    failed=True,
                )
            return ToolResult(
                output=(
                    f"{entry.handle} 的坐标文件不在了（{entry.path}）。先跑一次工作区核对，"
                    "或用 fetch_structure 重新取一份"
                ),
                failed=True,
            )

        confirm = bool(arguments.get("confirm"))
        try:
            text = entry.path.read_text(encoding="utf-8", errors="replace")
            log_read(entry.path, purpose="receptor.prepare", size=len(text.encode("utf-8")))
            built = _build(
                text,
                chain_raw=str(arguments.get("chain") or ""),
                ligand_raw=str(arguments.get("ligand") or ""),
                keep_raw=str(arguments.get("keep_hetero") or ""),
                center_raw=str(arguments.get("box_center") or ""),
                size_raw=str(arguments.get("box_size") or ""),
                padding=padding,
                altloc=altloc,
                relax=bool(arguments.get("relax")),
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        plan = built.plan

        waters = [group for group in built.hetero_removed if group.resname in WATER_NAMES]
        others = tuple(
            group for group in built.hetero_removed if group.resname not in WATER_NAMES
        )
        situation = Situation(
            source_handle=str(entry.handle),
            source_display=entry.summary or str(entry.handle),
            source_fingerprint=entry.fingerprint or "unknown",
            stage=stage,
            chains_kept=plan.chains,
            altloc=plan.altloc,
            altloc_choices=plan.altloc_choices,
            altloc_fell_back=plan.altloc_fell_back,
            chains_dropped=built.chains_dropped,
            waters=len(waters),
            water_atoms=sum(group.size for group in waters),
            hetero_removed=others,
            hetero_kept=plan.keep_hetero,
            extra_models=built.extra_models,
            ligand_label=plan.ligand.label if plan.ligand else "",
            ligand_atoms=plan.ligand.size if plan.ligand else 0,
            ligand_note=plan.ligand_note,
            ligand_written=bool(built.ligand_text),
            box_center=plan.box[0],
            box_size=plan.box[1],
            box_source=plan.box_source,
            receptor_atoms=len(built.doc.atoms),
            receptor_residues=len({atom.key for atom in built.doc.atoms}),
        )

        params = plan.params(
            source_fingerprint=entry.fingerprint or "unknown", meeko_version=_meeko_version()
        )
        product, reusable = _product_dir(
            prepared_receptor_dir(),
            entry.path.stem,
            plan.chains,
            fingerprint_parts(params, length=8),
            params,
        )
        situation.product_dir = product

        # 复用只发生在**真要写盘**的那一次：预演仍然把 Meeko 跑一遍。
        # 反过来的话，「预演说会产出多少原子」就会变成一句没法验证的话。
        if confirm and reusable:
            situation.reused = True
            # 指纹要在这里现算：产物在盘上才算得出来，而预演那条路径根本不写盘
            handle, added = _register_prepared(
                ledger,
                product=product,
                fingerprint=fingerprint_path(product),
                source_handle=str(entry.handle),
                situation=situation,
                params=params,
                ligand_file=LIGAND_PDB if built.ligand_text else None,
                ligand_label=plan.ligand.label if plan.ligand else None,
            )
            if added:
                # 产物在盘上、这一份登记表里却没有它（新会话，或者登记表被清过）。
                # 这时候必须补登记：只说一句「已复用」而不给 handle，调用方下一步
                # 就会拿到一个不是 handle 的东西。
                situation.registered = (
                    f"工作区里已有同一组参数的产物，已登记为 {handle}"
                    "（状态 prepared；要让它成为当前受体用 switch_receptor）"
                )
            else:
                situation.registered = f"工作区里已有这条受体：{handle}（未重复登记）"
            return ToolResult(
                output=_render_done(situation),
                meta={
                    "handle": handle,
                    "reused": True,
                    "product_dir": str(product),
                },
            )

        try:
            situation.result = parameterize(built.doc.text, relax=plan.relax)
        except ChemRejection as rejection:
            # 拒绝是**可操作**的一档，交给模型与用户看清单——但它是失败：
            # 没有产物、没有登记，把它当成功会让下一步引用一个不存在的东西。
            return ToolResult(
                output=_render_rejection(situation, rejection),
                failed=True,
                meta={"rejected": True},
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        if not confirm:
            return ToolResult(
                output=_render_preview(situation),
                meta={
                    "preview": True,
                    "receptor_atoms": situation.receptor_atoms,
                    "product_dir": str(product),
                },
            )

        try:
            _write_product(
                product,
                receptor_text=built.doc.text,
                ligand_text=built.ligand_text,
                box=plan.box,
                pdbqt=situation.result.pdbqt,
                params=params,
                report=_report(situation, plan, params, parse_pdb(text)),
            )
        except OSError as error:
            return ToolResult(
                output=f"写盘 {_relative(product)} 出错（{type(error).__name__}：{error}）",
                failed=True,
            )

        # 写盘之后才算指纹——那之前目录还不齐
        fingerprint = fingerprint_path(product)
        handle, added = _register_prepared(
            ledger,
            product=product,
            fingerprint=fingerprint,
            source_handle=str(entry.handle),
            situation=situation,
            params=params,
            ligand_file=LIGAND_PDB if built.ligand_text else None,
            ligand_label=plan.ligand.label if plan.ligand else None,
        )
        if added:
            situation.registered = (
                f"已登记 {handle}（状态 prepared，父项 {entry.handle}；"
                "要让它成为当前受体用 switch_receptor）"
            )
        else:
            situation.registered = f"工作区里已有同一份产物：{handle}（未重复登记）"

        return ToolResult(
            output=_render_done(situation),
            meta={
                "handle": handle,
                "fingerprint": fingerprint,
                "reused": False,
                "product_dir": str(product),
            },
        )


def _register_prepared(
    ledger: SessionLedger,
    *,
    product: Path,
    fingerprint: str,
    source_handle: str,
    situation: Situation,
    params: dict[str, Any],
    ligand_file: str | None,
    ligand_label: str | None,
) -> tuple[str, bool]:
    """把一份产物登记成 prepared 的受体，返回 (handle, 是不是这一次新登记的)。

    已经有同一份指纹时直接返回那一条。**产物在盘上、登记表里没有它时也要补登记**：
    复用那条路径原先只查表，查不到就把一句「登记表里找不到对应的条目」当成 handle
    交出去——调用方拿着它做下一步，只会在别处炸开，而这里显示的是成功。
    """
    existing = ledger.find(kind="receptor", fingerprint=fingerprint, include_archived=True)
    if existing:
        return str(existing[0].handle), False
    registered = ledger.add(
        "receptor",
        path=product,
        summary=_prepared_summary(situation),
        fingerprint=fingerprint,
        parents=[source_handle],
        meta={
            "stage": "prepared",
            "prepared_from": source_handle,
            "box_file": BOX_FILE,
            "pdbqt_file": RECEPTOR_PDBQT,
            "pdb_file": RECEPTOR_PDB,
            "ligand_file": ligand_file,
            "ligand_label": ligand_label,
            "chains": list(situation.chains_kept),
            "params": params,
        },
    )
    return str(registered.handle), True


def _prepared_summary(situation: Situation) -> str:
    parts = [
        "准备后的受体",
        f"链 {'/'.join(_display_chain(chain) for chain in situation.chains_kept)}",
    ]
    if situation.ligand_label:
        parts.append(f"盒子来自 {situation.ligand_label}")
    parts.append(f"{situation.receptor_residues} 残基")
    return " · ".join(parts)


def _report(
    situation: Situation,
    plan: Plan,
    params: dict[str, Any],
    facts: StructureFacts,
) -> dict[str, Any]:
    """体检报告。

    **写盘的那一份要能独立回答「这份受体是怎么来的」**：隔一周回来问「当时删了哪些水、
    盒子是哪个配体算的、用的是哪个版本的 Meeko」，答案必须在这份文件里，
    而不是只在某次对话记录里。
    """
    result = situation.result
    return {
        "kind": "receptor-prep",
        "tool": "prepare_receptor",
        "prepared_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "handle": situation.source_handle,
            "summary": situation.source_display,
            "fingerprint": situation.source_fingerprint,
            "stage": situation.stage,
            "pdb_id": facts.pdb_id,
            "title": facts.title,
            "method": facts.method,
            "resolution": facts.resolution,
        },
        "params": params,
        "params_fingerprint": fingerprint_parts(params, length=16),
        "edited": {
            "chains_kept": list(plan.chains),
            "chains_dropped": situation.chains_dropped,
            "models_dropped": situation.extra_models,
            "altloc_requested": plan.altloc or None,
            "altloc_choices": plan.altloc_choices,
            "altloc_fell_back": list(plan.altloc_fell_back),
            "waters_removed": situation.waters,
            "water_atoms_removed": situation.water_atoms,
            "hetero_removed": [
                {"label": group.label, "resname": group.resname, "atoms": group.size}
                for group in situation.hetero_removed
            ],
            "hetero_kept_names": list(plan.keep_hetero),
            "modified_residue_names": list(plan.modified_names),
            "receptor_atoms": situation.receptor_atoms,
            "receptor_residues": situation.receptor_residues,
        },
        "ligand": (
            {
                "label": situation.ligand_label,
                "atoms": situation.ligand_atoms,
                "written": situation.ligand_written,
            }
            if situation.ligand_label
            else None
        ),
        "box": {
            "center": list(plan.box[0]),
            "size": list(plan.box[1]),
            "source": plan.box_source,
            "padding": plan.padding,
        },
        "files": {
            "receptor_pdb": RECEPTOR_PDB,
            "receptor_pdbqt": RECEPTOR_PDBQT,
            "ligand_pdb": LIGAND_PDB if situation.ligand_written else None,
            "box": BOX_FILE,
            "params": PARAMS_FILE,
            "report": REPORT_FILE,
        },
        "meeko": (
            {
                "version": params["meeko"],
                "monomers": result.monomers,
                "pdbqt_atoms": result.atoms,
                "ignored": list(result.ignored),
                "relax": plan.relax,
                "residues_with_added_h": result.residues_with_added_h,
                "residues_with_excess_h": list(result.residues_with_excess_h),
                "excess_bonds": list(result.excess_bonds),
                "chosen_by_fewest_missing_h": list(result.chosen_by_fewest_missing_h),
                "pdbqt_chars": len(result.pdbqt),
            }
            if result is not None
            else None
        ),
        "known_limits": list(KNOWN_LIMITS),
    }


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [PrepareReceptorTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
