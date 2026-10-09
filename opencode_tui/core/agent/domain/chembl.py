"""ChEMBL 已知活性数据。

UniProt 回答「这是什么蛋白」，ChEMBL 回答「什么东西能结合它、结合得多强」。
后者是虚拟筛选里**无法从序列推出来**的那一半信息，它处在漏斗的三个位置：

1. 可成药性判定 —— 一个靶点有几十个亚微米抑制剂，和一个都没有，是两种完全不同的
   任务。后者（孤儿靶点）的对接结果没有已知活性可以验证，这件事必须在会话早期就
   告诉用户，而不是等对接跑完才补一句「结果仅供参考」。
2. 现成的配体集 —— 已知活性分子本身就是一批配体，可以直接喂给对接工具。
3. 结果校准 —— 带实测活性的分子是检验对接打分排序能力的唯一依据。

`search_actives` 回答「这个靶点有哪些已知配体」，一次调用同时给出上面三样东西：
一行摘要、一个 `ligandset` 句柄、一份写盘的完整活性表。

另外两个入口回答另一半问题：`find_analogs` 拿一个分子去问「还有什么东西长得像它」
（新颖性检查与骨架跃迁），`describe_molecule` 拿一个编号去问「它是个什么分子」
（成药状态、类药性、已知作用靶点、适应症）。三个工具共用同一套网络与错误分级。

**ChEMBL 的接口有几处会静默出错的地方，处理都收在这个模块内部：**

- **认不出的过滤参数不报错，只是不生效。** 实测 `activity.json?...&confidence_score=9`
  返回 HTTP 200，且命中数与不过滤时一模一样——`confidence_score` 挂在 assay 表上，
  根本不在 activity 表上；任何 `assay__*` 形式的跨表过滤也一并被忽略。写错参数名不会
  炸，只会静默返回没筛过的数据，比报错危险得多。所以这里只使用实测确实生效的服务端
  过滤（`pchembl_value__gte`、`standard_type__in`、`potential_duplicate`、`order_by`），
  其余一律在本地做。
- **置信度要反查。** activity 与 assay 是约 10:1 的汇聚比（1000 条活性只涉及 113 个
  assay），所以拉完活性再拿出现过的 assay 编号分批反查，代价是一次请求；反过来先扫
  全库 assay 是六页，而且更容易撞上 URL 长度上限。
- **必须排序。** 不指定 `order_by` 时返回顺序是任意的（实测前三条 pChEMBL 是
  7.39 / 6.52 / 6.77），一旦命中数超过上限，拿到手的就是随便一批。按 `-pchembl_value`
  排序之后，「取回上限」的含义才变成「最强的那些」。注意别按 `standard_value` 排
  ——那个字段混着 nM 与 μM，排出来没有意义。
- **一个 UniProt 编号会映射到多个 ChEMBL 靶点。** 实测 P00533 查出 16 个，其中只有
  一个是 `SINGLE PROTEIN`，其余是蛋白家族（EGFR 连同 ErbB2/3/4）、嵌合蛋白、蛋白-蛋白
  互作。不选型就会把整个家族的活性数据当成这一个靶点的。
- **等式关系不代表活性值。** `IC50 > 10000 nM` 的真实含义是「没测到抑制」，但 ChEMBL
  照样按这个值算出 pChEMBL。这类记录混进数据集就是假阴性。
- **同一化合物有多个编号。** 盐型、共晶、游离碱各算一个 ChEMBL ID（`ASPIRIN` 与
  `ASPIRIN DL-LYSINE` 的相似度是 100），要按 `parent_molecule_chembl_id` 归并，否则
  同一化合物的多种盐型会占满结果。
- **相似性检索的阈值是 40–100 的整数百分比，写分数会被拒。** 实测 `0.85` 拿到的是
  HTTP 404 加一页 HTML（不是 JSON），`30` 与 `0` 是 HTTP 400。下界 40 是 ChEMBL 自己
  定的，所以这里**拒绝**越界值而不是悄悄截断。
- **查询结构写坏了返回的是 HTTP 500。** 实测把结构写成 `XYZ123`、或写一个没闭合的环，
  ChEMBL 一律回 500——和「服务器故障」同一个状态码，但它是确定性的，重试多少次都
  一样。这一条直接决定了这个模块需要 `server_5xx_as_error` 那个开关，否则内核的默认
  重试策略会把它当网络抖动白跑一遍，还把错误说成「服务暂时不可用」。
- **结构串是拼进 URL 路径的，凡是被 URL 语法赋了含义的字符都得编码。** 实测不编码时
  `/` **反而没事**（服务端容忍路径段里带斜杠），但 `#` 会出事：httpx 它当 fragment
  起点，实际发出的是 `.../similarity?limit=3#Cc1ccccc1/85.json`——**结构整段不见**，
  服务端回 404。既然「哪些字符安全」取决于服务端的路由写法而不是规范，一律
  `quote(..., safe="")` 全编码，别按印象挑几个。
- **`only=` 只能裁到嵌套对象这一层。** 写成 `molecule_structures__canonical_smiles` 会
  被静默忽略、整个 `molecule_structures` 照旧返回，所以 `molfile`（约 1.5 KB/条）裁不
  掉，而它是页大小的主因。控制流量要靠 `limit`，不是靠 `only`。
"""

from __future__ import annotations

import csv
import io
import json
import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..ledger import SessionLedger, fingerprint_parts, fingerprint_text
from ..tools import Tool, ToolResult, ToolSpec
from . import http
from .uniprot import normalize_accession

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"

TARGET_URL = f"{BASE_URL}/target.json"
ACTIVITY_URL = f"{BASE_URL}/activity.json"
ASSAY_URL = f"{BASE_URL}/assay.json"
SIMILARITY_URL = f"{BASE_URL}/similarity"
MOLECULE_URL = f"{BASE_URL}/molecule"
MECHANISM_URL = f"{BASE_URL}/mechanism.json"
DRUG_INDICATION_URL = f"{BASE_URL}/drug_indication.json"

TIMEOUT = 60.0

PAGE_SIZE = 1000
"""ChEMBL 的单页上限。实测 `limit` 超过它会被拒绝。"""

ASSAY_CHUNK = 120
"""反查 assay 时每块塞多少个编号。

实测 113 个编号拼出的查询串不到 1500 字符，一次请求就够。分块是为「拉得更多、涉及的
assay 更多」留余量，同时避开各 HTTP 实现不一致的 URL 长度上限。
"""

MAX_ROWS_LIMIT = 5000
"""`max_rows` 的硬上限。

实测一页一千条约 5-11 秒，封在 5000 就是最多五页、约一分钟。再往上不该靠翻页，而该
收紧 `min_pchembl` 或限定 `measure_types`——筛选要的是「够用的已知活性」，不是把
ChEMBL 搬回家。
"""

DEFAULT_MEASURE_TYPES = "IC50,Ki,Kd"
"""默认只取定量结合数据。

EC50、抑制率这类在 `standard_value` 里与其他量纲混在一起，且受细胞体系、孵育时间影响
极大，不适合当对接打分的参照物。
"""

RELATION_EQUAL = "="
SINGLE_PROTEIN = "SINGLE PROTEIN"

CSV_COLUMNS: tuple[str, ...] = (
    "molecule_chembl_id",
    "parent_molecule_chembl_id",
    "molecule_pref_name",
    "smiles",
    "pchembl_value",
    "standard_type",
    "standard_value",
    "standard_units",
    "assay_chembl_id",
    "confidence_score",
    "assay_type",
    "document_year",
    "ligand_efficiency",
)
"""写盘表的列。

`pchembl_value` 排在活性描述的第一位：它是跨测定类型、跨单位可比的唯一一列，下游排序、
设阈值都该用它，而不是 `standard_value`。
"""

DROP_REASONS: dict[str, str] = {
    "unmeasured": "记录值不是精确测量（未测到抑制的不等式，或未标注测定关系）",
    "no_value": "没有定量活性值",
    "no_structure": "没有结构（无结构串，无法用于对接）",
    "low_confidence": "测定置信度不足（靶点归属不够直接）",
}

POTENCY_FLOOR_HINT = 10.5
"""pChEMBL 到了这个量级就多半不是精确测量值。

实测 EGFR 取回的最强几条是 `IC50 0.01 nM`（pChEMBL 11.00），而且 10.9 与 11 之间一条
记录都没有——这种形状像是踩到了读数下限。在 10.5 以上，长时间预孵育的不可逆抑制剂、
或者仪器的检测限，都能造出比真实亲和力强好几个数量级的「表观 IC50」。当「非常强」读
没问题，当精确值去校准对接打分就有问题。
"""

MIN_SIMILARITY = 40
"""相似性检索接受的相似度下界（百分比整数）。

**下界 40 是 ChEMBL 定的，不是我们选的。** 实测 20 / 30 / 0 一律 HTTP 400，101 也是
400，而 `-1` 与 `abc` 是 404 加一页 HTML。所以越界值要**拒绝**而不是悄悄截断：截断之后
「我要 30」和「我要 85」会拿到同一份结果，而输出里看不出差别。
"""

MAX_SIMILARITY = 100

MAX_ANALOG_ROWS = 1000
"""`find_analogs` 的取回上限。到这个量级就该收紧阈值，而不是翻更多页。"""

SIMILARITY_PAGE = 200
"""相似性检索的单页条数。

**不是 ChEMBL 的上限，是我们自己选的。** 每条记录都带着 `molfile`（约 1.5 KB）而
`only=` 裁不掉它：实测 100 条就是 228 KB，1000 条是 2.3 MB 一个响应——一口气拿下来
要等很久，中途断了还得从头再来。分页之后默认那次请求只取一页，要更多再续。
"""

QUERY_SMILES_LIMIT = 2500
"""查询结构串的长度上限。

它是拼在 URL **路径**里的（`/similarity/<结构>/<阈值>.json`）。小分子通常不到 200 字符，
2500 已是十倍余量；再长就不是这个工具该处理的分子，报错好过发一个超长 URL。
"""

CHEMBL_ID_PATTERN = re.compile(r"CHEMBL\d+", re.IGNORECASE)

_ORGANIC_SUBSET = frozenset(
    ("B", "C", "N", "O", "P", "S", "F", "I", "Cl", "Br", "H", "b", "c", "n", "o", "p", "s")
)
"""结构串里**不写方括号**时允许出现的原子符号。

这是 SMILES 规范自身的规定：方括号外只认有机子集（B C N O P S F Cl Br I，以及芳香的
b c n o p s），其他元素必须写进方括号（`[Se]`、`[Na+]`）。于是「方括号外的字母拼不出
合法符号」可以被当场判成写错的结构——`XYZ123` 这类输入的破绽就在这儿，而 ChEMBL 对它
的回应是 HTTP 500（一个听起来像服务器故障的码）。
"""

ANALOG_COLUMNS: tuple[str, ...] = (
    "molecule_chembl_id",
    "parent_molecule_chembl_id",
    "molecule_pref_name",
    "smiles",
    "similarity",
    "max_phase",
    "molecule_type",
    "mw_freebase",
    "alogp",
    "qed_weighted",
    "num_ro5_violations",
)
"""写盘的相似分子表。

`similarity` 紧跟结构，和 `CSV_COLUMNS` 把 `pchembl_value` 提前是同一条理由：下游筛选
取的是这一列，不是分子编号的字典序。后面四列来自 ChEMBL 算好的物性，让这份表可以直接
拿去做类药性初筛，不必为了 MW / logP 再逐个查一遍。
"""

_ANALOG_ONLY = ",".join(
    (
        "molecule_chembl_id",
        "similarity",
        "pref_name",
        "molecule_structures",
        "molecule_hierarchy",
        "molecule_type",
        "max_phase",
        "molecule_properties",
    )
)
"""相似性检索的字段裁剪。

**只能裁到嵌套对象这一层**：实测 `molecule_structures__canonical_smiles` 会被静默忽略，
整个 `molecule_structures`（含约 1.5 KB/条的 `molfile`）照旧返回。所以这一项省下的是顶层
几十个用不上的字段，页大小的主因仍是 `molfile`，控制它要靠 `limit`。
"""

MOLECULE_ONLY = ",".join(
    (
        "molecule_chembl_id",
        "pref_name",
        "molecule_type",
        "structure_type",
        "molecule_structures",
        "molecule_properties",
        "molecule_hierarchy",
        "max_phase",
        "first_approval",
        "withdrawn_flag",
        "black_box_warning",
        "oral",
        "parenteral",
        "topical",
        "prodrug",
        "natural_product",
        "therapeutic_flag",
        "orphan",
        "molecule_synonyms",
        "atc_classifications",
        "chirality",
    )
)
"""`describe_molecule` 第一次取回的字段。比整条记录少一半，且都是要展示的。"""

MAX_PHASE_LABELS: dict[float, str] = {
    -1: "未进入开发流程",
    0: "临床前",
    0.5: "早期 I 期临床",
    1: "I 期临床",
    2: "II 期临床",
    3: "III 期临床",
    4: "已上市药物",
}
"""ChEMBL 的 `max_phase` 取值含义（它自己文档里的约定）。

查表打不中时**不猜**，原样报出数值——猜出来的「阶段 6」比「阶段 6」本身更坏。
"""

_ATOM_RUN = re.compile(r"[A-Za-z]+")
_ATOM_LETTERS = re.compile(r"[A-Za-z]")

MECHANISM_LIMIT = 50
"""一次取多少条作用机制。

ChEMBL 的 mechanism 表整个库只有 7561 条（实测），所以单个分子通常是 1–3 条，50 是
「不至于截断」的量级。真截断了摘要里会说。
"""

INDICATION_SAMPLE = 5

MAX_TOP_MOLECULES = 10
"""条目 meta 里放前几条分子。

meta 会被别的工具整段回注，把一千多个分子塞进去的结果是它被截断、前面的字段也跟着
看不见。完整清单在那份 CSV 里。
"""


def chembl_cache_dir() -> Path:
    """ChEMBL 数据的本地缓存目录，与靶点缓存、结构目录同处一个家目录。"""
    return Path.home() / ".opencode_tui" / "chembl"


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
_SIMILARITY_500_HINT = (
    "ChEMBL 处理不了这个结构。它把「结构解析失败」回成 HTTP 500，"
    "所以这多半是结构本身的问题（价键、芳香性、立体化学），"
    "换个写法或改用 ChEMBL 分子编号再试——原样重试不会有变化"
)


def _request_json(
    url: str,
    params: Mapping[str, Any],
    *,
    timeout: float = TIMEOUT,
    server_5xx_as_error: str = "",
) -> Any:
    """向 ChEMBL 发一次请求并解析 JSON。**测试替换的就是这个函数。**

    4xx（编号不存在、参数语法错）重试没有意义，5xx 与连不上才是该重试的；
    `server_5xx_as_error` 让相似性检索那一侧把 5xx 当确定性失败（见模块开头）。
    """
    return http.get_json(
        url,
        dict(params),
        timeout=timeout,
        headers={"Accept": "application/json"},
        server_5xx_as_error=server_5xx_as_error,
    )


# ---------------------------------------------------------------------------
# 数值
# ---------------------------------------------------------------------------
def _as_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # NaN / inf 能通过 float()，但会让排序与统计全乱
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _ligand_efficiency(raw: Any) -> str:
    """`ligand_efficiency` 是个字典（bei/le/lle/sei），不是标量。

    直接塞进 CSV 会写成一串 Python 字典的 repr，下游解析不了；这里只取 `le`
    （配体效率），它是按重原子数归一化过的、最能跨分子比较的那个。
    """
    if isinstance(raw, Mapping):
        value = raw.get("le")
        if value not in (None, ""):
            return str(value)
    return ""


def _max_phase_label(raw: Any) -> str:
    """把 `max_phase` 说成人话。查表打不中时返回空串，**不猜**。

    猜出来的「阶段 6」比原样报出「6」更坏：前者看起来像 ChEMBL 承认过这个阶段。
    查不到时调用方自己决定是省略还是报原始值。
    """
    number = _as_float(raw)
    if number is None:
        return ""
    return MAX_PHASE_LABELS.get(number, "")


def _yes_no(value: Any) -> bool:
    """ChEMBL 的布尔字段混着 `0`/`1`、`true`/`false` 与 `null`。

    缺省一律当「否」——这里只用来决定要不要多写一句话，漏写比写错好。
    """
    if isinstance(value, bool):
        return value
    number = _as_int(value)
    if number is not None:
        return bool(number)
    return str(value).strip().lower() in {"true", "yes"}


# ---------------------------------------------------------------------------
# 靶点选型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _TargetChoice:
    """选中的 ChEMBL 靶点，以及被排除掉的同类候选。"""

    chembl_id: str
    name: str
    alternatives: list[str] = field(default_factory=list)
    skipped_complexes: int = 0


def _sole_component_accession(target: Mapping[str, Any]) -> str | None:
    components = target.get("target_components") or []
    if len(components) != 1:
        return None
    return str((components[0] or {}).get("accession") or "")


def _resolve_target(accession: str, *, timeout: float) -> _TargetChoice:
    """UniProt 编号 → ChEMBL 靶点。

    **默认只取 `SINGLE PROTEIN`，这不是洁癖。** 实测 `P00533` 查出 16 个候选，其中只有
    `CHEMBL203` 是 EGFR 本身，其余是「EGFR 连同 ErbB2/3/4」的蛋白家族、MER/EGFR 嵌合
    蛋白、EGFR/cereblon 这类蛋白-蛋白互作。拿家族条目取数据，会把其他三个 ErbB 成员的
    活性全当成 EGFR 的——「只针对 EGFR」这个前提就没了。
    """
    payload = _request_json(
        TARGET_URL, {"target_components__accession": accession, "limit": "100"}, timeout=timeout
    )
    targets = list(payload.get("targets") or []) if isinstance(payload, Mapping) else []
    if not targets:
        raise http.ToolError(
            f"ChEMBL 里没有以 {accession} 为组分的靶点。"
            "先确认这确实是 UniProt 编号（用 search_targets / fetch_target 取得），"
            "并且它是人的蛋白——ChEMBL 的靶点以人为主，其他物种覆盖很少"
        )

    singles = sorted(
        (item for item in targets if str(item.get("target_type") or "") == SINGLE_PROTEIN),
        key=lambda item: str(item.get("target_chembl_id") or ""),
    )
    if not singles:
        kinds = "、".join(sorted({str(item.get("target_type")) for item in targets}))
        raise http.ToolError(
            f"ChEMBL 里 {accession} 只作为复合物的组分出现，没有单一蛋白靶点"
            f"（现有类型：{kinds}）。这类条目的活性属于整个复合物，"
            "不能直接当成这个蛋白的配体数据用"
        )

    # 组分数恰好为 1 且就是它自己——这是「单一蛋白」里最干净的一种
    exact = [item for item in singles if _sole_component_accession(item) == accession]
    chosen = (exact or singles)[0]
    alternatives = [f"{item.get('target_chembl_id')}" for item in singles if item is not chosen]
    return _TargetChoice(
        chembl_id=str(chosen.get("target_chembl_id") or ""),
        name=str(chosen.get("pref_name") or ""),
        alternatives=alternatives,
        skipped_complexes=len(targets) - len(singles),
    )


# ---------------------------------------------------------------------------
# 查询结构（分子编号与结构串）
# ---------------------------------------------------------------------------
def normalize_molecule_id(value: str) -> str:
    """把 `chembl939` 之类归一成 `CHEMBL939`，格式不对就报错。

    与 `uniprot.normalize_accession` 同一个角色，理由也一样：编号格式的校验只该有一份
    实现。两份正则迟早分叉，表现成「一个工具收这个编号、另一个不收」。
    """
    code = (value or "").strip().upper()
    if not CHEMBL_ID_PATTERN.fullmatch(code):
        raise http.ToolError(
            f"{value!r} 不是 ChEMBL 分子编号。编号形如 CHEMBL939，"
            "取自 search_actives / find_analogs 的结果；"
            "如果想按结构找，请直接给一段 SMILES"
        )
    return code


def _is_organic_run(run: str) -> bool:
    """方括号外的一段连续字母能不能切成合法的有机子集符号。

    先试两个字符再试一个：`Cl` / `Br` 必须以两字符优先，否则 `ClC` 会被切成
    `C` + `l` + `C`，而 `l` 不是任何元素。这一步同时挡住 `XYZ123` 这类「看着像结构串、
    其实拼不出原子」的输入。
    """
    index = 0
    while index < len(run):
        if len(run) - index >= 2 and run[index : index + 2] in _ORGANIC_SUBSET:
            index += 2
            continue
        if run[index] in _ORGANIC_SUBSET:
            index += 1
            continue
        return False
    return True


def sanity_check_smiles(value: str) -> str:
    """在联网之前挡掉写坏的结构串。

    **为什么值得自己查一遍**：ChEMBL 对「结构解析不了」的回应是 HTTP 500（实测
    `XYZ123`、未闭合的环都是 500），和服务器故障同码。不先拦一道，一个笔误要等一次
    网络往返，换回来一个措辞误导的错误。

    查得住的：非 ASCII、空白、长度、括号配对、方括号外拼不出合法原子。
    查不住的：价键、芳香性、立体化学——那些留给 ChEMBL，它回 500 时我们会说明
    「这多半是结构本身的问题，重试不会有变化」。
    """
    text = (value or "").strip()
    if not text:
        raise http.ToolError(
            "没有给结构。要么给 ChEMBL 分子编号（形如 CHEMBL939），要么给一段 SMILES"
        )
    if not text.isascii():
        raise http.ToolError(
            f"结构串里有非 ASCII 字符（{text[:30]!r}）。"
            "如果你本来就不打算给结构串，请改给 ChEMBL 分子编号，形如 CHEMBL939"
        )
    if any(char.isspace() for char in text):
        raise http.ToolError("结构串里不能有空白字符——去掉换行与空格再试")
    if len(text) > QUERY_SMILES_LIMIT:
        raise http.ToolError(
            f"结构串有 {len(text)} 个字符，超过上限 {QUERY_SMILES_LIMIT}。"
            "相似性检索把结构拼在 URL 路径里，放不下这种规模；"
            "这个工具面向小分子（通常不到 200 字符），生物大分子没有这项检索"
        )
    if not _ATOM_LETTERS.search(text):
        raise http.ToolError(f"{text[:30]!r} 里一个原子都没有，不像结构串")

    outside: list[str] = []
    depth = 0
    for char in text:
        if char == "[":
            depth += 1
            if depth > 1:
                raise http.ToolError("结构串里的方括号嵌套了（如 [C[C]]），这不是有效写法")
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise http.ToolError("结构串的方括号不配对（多了一个 ]）")
        elif depth == 0:
            outside.append(char)
    if depth:
        raise http.ToolError("结构串的方括号不配对（少了一个 ]）")
    if text.count("(") != text.count(")"):
        raise http.ToolError("结构串的圆括号不配对")

    for run in _ATOM_RUN.findall("".join(outside)):
        if not _is_organic_run(run):
            raise http.ToolError(
                f"结构串里方括号外的 {run!r} 拼不成合法原子。"
                "方括号外只认 C N O P S F Cl Br I 与芳香的 b c n o p s，"
                "其他元素必须写在方括号里（如 [Se]、[Na+]）。"
                f"请检查 {text[:40]!r} 有没有写错，或者改给 ChEMBL 分子编号"
            )
    return text


# ---------------------------------------------------------------------------
# 活性数据
# ---------------------------------------------------------------------------
def _count(url: str, params: Mapping[str, str], *, timeout: float) -> int:
    """只取 `page_meta.total_count`。

    先查计数再看要不要拉数据，是这个工具能同时服务「可成药性探路」与「取配体集」两种
    意图的关键——前者只想知道有个几百还是几万，不该为此拉一页数据。
    """
    payload = _request_json(url, {**params, "limit": "1"}, timeout=timeout)
    meta = (payload or {}).get("page_meta") or {}
    total = meta.get("total_count")
    return total if isinstance(total, int) else 0


def _fetch_activities(params: Mapping[str, str], *, want: int, timeout: float) -> list[dict[str, Any]]:
    """按活性从强到弱翻页取回至多 `want` 条记录。

    调用方必须已经在 `params` 里放了 `order_by=-pchembl_value`——不排序的话，到达上限时
    留下的是任意一批（实测默认顺序是 7.39 / 6.52 / 6.77 这种乱序）。
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while len(rows) < want:
        page = min(PAGE_SIZE, want - len(rows))
        payload = _request_json(
            ACTIVITY_URL, {**params, "limit": str(page), "offset": str(offset)}, timeout=timeout
        )
        batch = [dict(item) for item in (payload.get("activities") or [])]
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return rows


def _assay_facts(assay_ids: Iterable[str], *, timeout: float) -> dict[str, dict[str, Any]]:
    """按编号批量反查 assay，取回置信度与实验体系。

    置信度是这份数据最重要的质量维度，而它不在 activity 上，只能这样绕一次。好消息是
    汇聚比很高：1000 条活性只涉及约 113 个 assay，所以这一次反查很便宜。
    """
    facts: dict[str, dict[str, Any]] = {}
    ids = sorted({str(item) for item in assay_ids if item})
    for start in range(0, len(ids), ASSAY_CHUNK):
        chunk = ids[start : start + ASSAY_CHUNK]
        payload = _request_json(
            ASSAY_URL,
            {"assay_chembl_id__in": ",".join(chunk), "limit": str(len(chunk))},
            timeout=timeout,
        )
        for row in payload.get("assays") or []:
            key = str((row or {}).get("assay_chembl_id") or "")
            if not key:
                continue
            facts[key] = {
                "confidence_score": _as_int((row or {}).get("confidence_score")),
                "assay_type": str((row or {}).get("assay_type") or ""),
            }
    return facts


def _normalize(
    row: Mapping[str, Any], *, pchembl: float, confidence: int, assay_type: str
) -> dict[str, Any]:
    """压成写盘表的一行。列的顺序由 `CSV_COLUMNS` 决定，这里只管取值。"""
    molecule = str(row.get("molecule_chembl_id") or "")
    return {
        "molecule_chembl_id": molecule,
        # 母体为空时退回自己：没有盐型区分的记录，它就是自己的母体
        "parent_molecule_chembl_id": str(row.get("parent_molecule_chembl_id") or molecule),
        "molecule_pref_name": str(row.get("molecule_pref_name") or ""),
        "smiles": str(row.get("canonical_smiles") or ""),
        "pchembl_value": pchembl,
        "standard_type": str(row.get("standard_type") or ""),
        "standard_value": str(row.get("standard_value") or ""),
        "standard_units": str(row.get("standard_units") or ""),
        "assay_chembl_id": str(row.get("assay_chembl_id") or ""),
        "confidence_score": confidence,
        "assay_type": assay_type,
        "document_year": row.get("document_year") or "",
        "ligand_efficiency": _ligand_efficiency(row.get("ligand_efficiency")),
    }


def _select(
    rows: Sequence[Mapping[str, Any]],
    *,
    confidence: Mapping[str, Mapping[str, Any]],
    min_confidence: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """逐条过掉不干净的数据，并按母体分子归并。

    四类剔除各有理由，缺一条都会让下游拿着错的数据算：

    - 关系符不是 `=` 的：`IC50 > 10000 nM` 意思是「没测到抑制」，它的 pChEMBL 是个下界，
      不是活性值。混进去就是假阴性。
    - 没有 pChEMBL 的：只有文字描述的记录无法参与排序。
    - 没有结构串的：对接工具拿到它没法用。
    - 置信度不足的：ChEMBL 的 `confidence_score` 描述「这条测定的靶点归属有多可信」，
      9 是直接测定的单一蛋白，5 以下多为同源蛋白推定——那些数据属于**别的蛋白**。

    归并放在最后：同一个母体的多条记录只留活性最强的那条。盐型不同、测定不同、文献
    不同，都算同一个化合物——`ASPIRIN` 与 `ASPIRIN DL-LYSINE` 就是这种情况。
    """
    dropped = dict.fromkeys(DROP_REASONS, 0)
    kept: list[dict[str, Any]] = []

    for row in rows:
        pchembl = _as_float(row.get("pchembl_value"))
        if pchembl is None:
            dropped["no_value"] += 1
            continue
        relation = str(row.get("standard_relation") or "").strip()
        if relation != RELATION_EQUAL:
            dropped["unmeasured"] += 1
            continue
        if not str(row.get("canonical_smiles") or "").strip():
            dropped["no_structure"] += 1
            continue

        facts = confidence.get(str(row.get("assay_chembl_id") or ""), {})
        score = facts.get("confidence_score")
        if not isinstance(score, int) or score < min_confidence:
            dropped["low_confidence"] += 1
            continue

        kept.append(
            _normalize(
                row,
                pchembl=pchembl,
                confidence=score,
                assay_type=str(facts.get("assay_type") or ""),
            )
        )

    best: dict[str, dict[str, Any]] = {}
    for item in kept:
        key = str(item["parent_molecule_chembl_id"])
        current = best.get(key)
        if current is None or item["pchembl_value"] > current["pchembl_value"]:
            best[key] = item

    merged = sorted(
        best.values(), key=lambda item: (-item["pchembl_value"], item["molecule_chembl_id"])
    )
    return merged, dropped


# ---------------------------------------------------------------------------
# 分子记录：结构、作用机制、适应症
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Query:
    """相似性检索的输入，归一成「一段结构 + 它是谁」。"""

    smiles: str
    chembl_id: str = ""
    name: str = ""
    parent_id: str = ""


def _molecule_query(molecule_id: str, *, timeout: float) -> _Query:
    """编号 → 结构，用作相似性检索的入口。

    生物药在这里就断掉：它们的 `molecule_structures` 只有序列
    （`structure_type == "SEQ"`），没有 `canonical_smiles`（实测 BEVACIZUMAB
    `CHEMBL1201583` 就是这样）。相似性检索比的是结构指纹，没有结构就无从比起——当场
    说清楚，好过让它带着空串去撞一个 HTTP 500。
    """
    payload = _request_json(
        f"{MOLECULE_URL}/{molecule_id}.json",
        {
            "only": "molecule_chembl_id,pref_name,molecule_type,structure_type,"
            "molecule_structures,molecule_hierarchy"
        },
        timeout=timeout,
    )
    record = payload if isinstance(payload, Mapping) else {}
    structures = record.get("molecule_structures") or {}
    smiles = str(structures.get("canonical_smiles") or "").strip()
    if not smiles:
        kind = str(record.get("molecule_type") or "未知类型")
        raise http.ToolError(
            f"{molecule_id} 是{kind}，ChEMBL 里没有它的结构式（生物药与蛋白只有序列），"
            "做不了相似性检索——这个工具只针对小分子"
        )
    hierarchy = record.get("molecule_hierarchy") or {}
    return _Query(
        smiles=smiles,
        chembl_id=molecule_id,
        name=str(record.get("pref_name") or ""),
        parent_id=str(hierarchy.get("parent_chembl_id") or molecule_id),
    )


def _mechanisms(molecule_id: str, *, timeout: float) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """取作用机制，并把靶点编号解析成可读的名字。

    `mechanism.json` 只给 `target_chembl_id`，不给靶点名——光看 `CHEMBL203` 谁也认不出
    那是 EGFR。补一次批查就够：`target_chembl_id__in` 实测生效（全库 18552 条靶点，查
    两个编号回 2 条、查一个不存在的回 0 条）。

    这一次批查**刻意不加 `only=`**：字段裁剪在这里省不下多少（几十条靶点），而裁错字段
    的后果是名字全空、表格看起来只是「ChEMBL 没收录名称」。
    """
    payload = _request_json(
        MECHANISM_URL,
        {"molecule_chembl_id": molecule_id, "limit": str(MECHANISM_LIMIT)},
        timeout=timeout,
    )
    mechanisms = [dict(item) for item in (payload.get("mechanisms") or [])]
    ids = sorted(
        {
            str(item.get("target_chembl_id") or "")
            for item in mechanisms
            if item.get("target_chembl_id")
        }
    )
    names: dict[str, str] = {}
    if ids:
        extra = _request_json(
            TARGET_URL,
            {"target_chembl_id__in": ",".join(ids), "limit": str(len(ids))},
            timeout=timeout,
        )
        for target in extra.get("targets") or []:
            key = str((target or {}).get("target_chembl_id") or "")
            if key:
                names[key] = str((target or {}).get("pref_name") or "")
    return mechanisms, names


def _indications(molecule_id: str, *, timeout: float) -> tuple[list[dict[str, Any]], int]:
    """取适应症样本与总数。

    一次请求同时拿到两样东西：`limit` 控制条数，`page_meta.total_count` 给总数。所以
    「51 条里的前 5 条」不需要为了知道总数而多跑一次。
    """
    payload = _request_json(
        DRUG_INDICATION_URL,
        {"molecule_chembl_id": molecule_id, "limit": str(INDICATION_SAMPLE)},
        timeout=timeout,
    )
    total = int((payload.get("page_meta") or {}).get("total_count") or 0)
    return [dict(item) for item in (payload.get("drug_indications") or [])], total


# ---------------------------------------------------------------------------
# 相似分子
# ---------------------------------------------------------------------------
def _similarity_url(smiles: str, threshold: int) -> str:
    """相似性检索的 URL。结构串拼在**路径**里，所以全部编码。

    `quote(..., safe="")` 不是美化，这条是实测出来的：`/` **不编码没事**（服务端容忍
    路径段里的斜杠），`+` 与 `[` `]` 也没事；出事的是 `#`：httpx 把它当 fragment 起点，
    实际请求变成 `.../similarity?limit=3#Cc1ccccc1/85.json`，**结构整段消失**，服务端
    404。既然安全与否取决于服务端怎么解析路径，就不该按印象挑字符，全编码最稳。
    """
    return f"{SIMILARITY_URL}/{quote(smiles, safe='')}/{threshold}.json"


def _fetch_analogs(
    query: _Query, *, threshold: int, want: int, timeout: float
) -> tuple[list[dict[str, Any]], int]:
    """按相似度从高到低翻页取回，返回（记录、命中总数）。

    与活性表相反，这个端点**服务端本来就按相似度降序返回**，没有 `order_by` 可给。所以
    截断留下来的天然是最像的那些——不需要额外排序，也不能靠别人代劳。
    """
    rows: list[dict[str, Any]] = []
    total = 0
    offset = 0
    url = _similarity_url(query.smiles, threshold)
    while len(rows) < want:
        page = min(SIMILARITY_PAGE, want - len(rows))
        payload = _request_json(
            url,
            {"limit": str(page), "offset": str(offset), "only": _ANALOG_ONLY},
            timeout=timeout,
            server_5xx_as_error=_SIMILARITY_500_HINT,
        )
        meta = payload.get("page_meta") or {}
        if not total:
            total = int(meta.get("total_count") or 0)
        batch = [dict(item) for item in (payload.get("molecules") or [])]
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return rows, total


def _normalize_analog(row: Mapping[str, Any], *, similarity: float) -> dict[str, Any]:
    """压成相似分子表的一行。列的顺序由 `ANALOG_COLUMNS` 决定。"""
    molecule = str(row.get("molecule_chembl_id") or "")
    hierarchy = row.get("molecule_hierarchy") or {}
    properties = row.get("molecule_properties") or {}
    return {
        "molecule_chembl_id": molecule,
        # 母体为空时退回自己：没有盐型区分的记录，它就是自己的母体
        "parent_molecule_chembl_id": str(hierarchy.get("parent_chembl_id") or molecule),
        "molecule_pref_name": str(row.get("pref_name") or ""),
        "smiles": str((row.get("molecule_structures") or {}).get("canonical_smiles") or ""),
        "similarity": similarity,
        "max_phase": _as_float(row.get("max_phase")),
        "molecule_type": str(row.get("molecule_type") or ""),
        "mw_freebase": _as_float(properties.get("mw_freebase")),
        "alogp": _as_float(properties.get("alogp")),
        "qed_weighted": _as_float(properties.get("qed_weighted")),
        "num_ro5_violations": _as_int(properties.get("num_ro5_violations")),
    }


def _analog_rank(item: Mapping[str, Any]) -> tuple[float, int]:
    """归并时的取舍依据：先比相似度，打平再比有没有药名。

    相似度打平时留下有药名的那个——同一化合物的两种记录里，有名字的信息更多，而读者
    要靠名字认人。写成元组而不是一串 `and`/`or`，是因为「先比 A 再比 B」这件事本身
    就是这个函数的全部内容。
    """
    return (float(item["similarity"]), 1 if item["molecule_pref_name"] else 0)


def _select_analogs(
    rows: Sequence[Mapping[str, Any]], *, query: _Query
) -> tuple[list[dict[str, Any]], int, int]:
    """清洗并归并，返回（分子表、归并掉的条数、没有结构的条数）。

    三件事，每一件不做的后果都很具体：

    - **排除查询分子自己。** 第一条永远是它本人（实测查阿司匹林，前三条都是阿司匹林的
      不同形态），留着只占掉一行，还容易让人以为「找到了一个 100% 相似的分子」。判据用
      `parent_chembl_id` 相等而不是编号相等：查询若是某个盐型，它本人与它的游离碱都不该
      出现在结果里。
    - **丢掉没有结构式的**：`molecule_type` 为空或 `Unknown` 的记录拿不到
      `canonical_smiles`（实测 595 条里有 57 条如此）。写进配体集也没法对接。
    - **按母体归并**：盐型、共晶、立体异构各占一个编号（实测那三条阿司匹林的相似度都是
      100，`parent_chembl_id` 都是 `CHEMBL25`）。不归并，前排会被同一化合物的多种形态
      占满，看起来像「找到了好几个新分子」。
    """
    kept: list[dict[str, Any]] = []
    without_structure = 0
    for row in rows:
        structures = row.get("molecule_structures") or {}
        smiles = str(structures.get("canonical_smiles") or "").strip()
        if not smiles:
            without_structure += 1
            continue
        molecule = str(row.get("molecule_chembl_id") or "")
        parent = str((row.get("molecule_hierarchy") or {}).get("parent_chembl_id") or molecule)
        if query.parent_id and parent == query.parent_id:
            continue
        if not query.parent_id and smiles == query.smiles:
            continue
        kept.append(
            _normalize_analog(row, similarity=round(_as_float(row.get("similarity")) or 0.0, 1))
        )

    best: dict[str, dict[str, Any]] = {}
    for item in kept:
        key = str(item["parent_molecule_chembl_id"])
        current = best.get(key)
        if current is None or _analog_rank(item) > _analog_rank(current):
            best[key] = item

    merged = sorted(best.values(), key=lambda item: (-item["similarity"], item["molecule_chembl_id"]))
    return merged, len(kept) - len(merged), without_structure


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------
def _relative(path: Path) -> str:
    """相对家目录展示，一律 POSIX 分隔符——反斜杠容易被原样抄去拼接。"""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _take_n(items: Mapping[Any, int], n: int) -> list[tuple[Any, int]]:
    """按出现次数取前 n 项，次数相同的按名字排——顺序要稳定，否则指纹会飘。"""
    return sorted(items.items(), key=lambda pair: (-pair[1], str(pair[0])))[:n]


def _render_csv(rows: Sequence[Mapping[str, Any]], *, columns: Sequence[str] = CSV_COLUMNS) -> str:
    """渲染成 CSV。

    `lineterminator` 显式写成 `\\n`：默认是 `\\r\\n`，那样同一份数据在不同平台上会算出
    不同的指纹，「同一件事」就到重不掉了。**但只写这一句还不够**——写盘的 `write_text`
    默认会把 `\\n` 再翻译回 `os.linesep`，所以 `_write_cache` 走的是字节写入。
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _cache_path(accession: str, key: str) -> Path:
    """按参数指纹命名缓存文件。

    不像 UniProt 那样一个编号一个文件（`<编号>.json`）：同一个靶点会被用不同条件问很多次
    （先看 pChEMBL≥7，嫌少再放宽到 6），共用一个文件名会让后一次静默盖掉前一次——而登记
    表里那条旧条目还指着这个路径，摘要写着 10053 个分子，文件里却是两万个。参数进文件名
    就没有这个问题。
    """
    return chembl_cache_dir() / f"{accession}_actives_{key}.csv"


def _analog_cache_path(slug: str, key: str) -> Path:
    """相似分子表的写盘路径。

    文件名里同时有「查询是谁」与「参数指纹」：同一个查询会被用不同阈值问很多次（先 85，
    嫌少降到 70），共用一个文件名会让后一次静默盖掉前一次。与活性表是同一个理由。
    """
    return chembl_cache_dir() / f"{slug}_analogs_{key}.csv"


@dataclass
class _Snapshot:
    """一次查询的完整结果。

    分子表与下面这几个计数一起写盘（CSV + 同名 `.meta.json`），而不是只存分子表。理由是
    摘要：命中多少、取回多少、归并掉多少、各类剔除多少，这些数字都来自网络，只存表的话
    缓存命中时复现不出来，同一组参数两次调用会给出两份不同的摘要——而摘要恰恰是模型读的
    那一份。存成快照之后，命中缓存是**纯读盘**。
    """

    molecules: list[dict[str, Any]]
    total_all: int
    total_matched: int
    fetched: int
    dropped: dict[str, int]
    fingerprint: str = ""

    @property
    def merged_away(self) -> int:
        """因为属于同一化合物而被合并掉的记录条数。

        同一个母体的多种盐型、多篇文献的重复测定都走到这里。它是「取回多少条记录」与
        「得到多少个分子」之间那个差额的唯一解释——不交代的话，读者会以为差额是凭空掉的。
        """
        return max(0, self.fetched - sum(self.dropped.values()) - len(self.molecules))


@dataclass
class _AnalogSnapshot:
    """一次相似性检索的完整结果。

    与 `_Snapshot` 一样把计数一起写盘，好让缓存命中在纯读盘的情况下复现同一份摘要。另外
    三样也必须一起存：查询结构、查询编号、查询的母体编号——它们决定「排除查询分子自己」
    这一步，而这一步直接影响分子个数与相似度区间。
    """

    molecules: list[dict[str, Any]]
    total_matched: int
    fetched: int
    merged_away: int
    without_structure: int
    query_smiles: str = ""
    query_id: str = ""
    query_name: str = ""
    fingerprint: str = ""


def _meta_path(path: Path) -> Path:
    return path.parent / f"{path.name}.meta.json"


def _write_cache(path: Path, *, text: str, fingerprint: str, meta: Mapping[str, Any]) -> None:
    """写盘分子表与元数据。

    先写 `.tmp` 再替换：中途写坏会留下半截 CSV，而下次调用会把它当成有效缓存读进来。

    **用字节写入而不是 `write_text`。** 后者默认会把 `\\n` 翻译成 `os.linesep`，于是一份
    在 Windows 上写盘的 CSV 是 `\\r\\n`、在 Linux 上是 `\\n`：同一份数据内容相同、字节
    不同，指纹也就不同。读写两侧都走字节，指纹才是内容的函数而不是平台的函数。

    元数据后写，里面记着分子表的指纹。万一两次写之间进程挂了，读到的是「表在、元数据
    缺失或指纹对不上」——那是**拒绝使用**，不是用半截数据。重下花几十秒，用错数据花的
    是整条结论。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_meta = {**meta, "fingerprint": fingerprint}
    for target, body in (
        (path, text),
        (_meta_path(path), json.dumps(payload_meta, ensure_ascii=False, indent=2)),
    ):
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(body.encode("utf-8"))
        temporary.replace(target)


def _read_cache(
    path: Path,
    *,
    numbers: Mapping[str, Callable[[Any], Any]],
    required: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """读回一份完整快照。任何一处不对都当作没有缓存（返回 None）而重下。

    半截文件、表与元数据指纹不匹配、CSV 解析失败——这三种都会让工具永久卡在一份错数据
    上，所以一律退回重下，而不是「尽力而为地用一用」。

    读的也是字节（与 `_write_cache` 对称）：`read_text` 会把 `\\r\\n` 归一成 `\\n`，指纹
    对着归一后的文本算就永远对不上原文件——那会变成「缓存永远失效」，一个只在 Windows
    上出现的、看不出来的性能陷阱。

    CSV 里所有值都是字符串，读回来必须按列还原类型，否则 `"9.5" < 10` 这类比较会静默
    走错分支。`numbers` 给出要还原的列及还原函数；`required` 里的列解析不出来就丢掉那
    一行（它在下游是排序依据，缺了没法用）。
    """
    meta_path = _meta_path(path)
    if not path.is_file() or not meta_path.is_file():
        return None
    try:
        text = path.read_bytes().decode("utf-8")
        meta = json.loads(meta_path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(meta, dict) or meta.get("fingerprint") != fingerprint_text(text):
        return None

    rows: list[dict[str, Any]] = []
    try:
        for raw in csv.DictReader(io.StringIO(text)):
            row: dict[str, Any] = dict(raw)
            for column, coerce in numbers.items():
                row[column] = coerce(raw.get(column))
            if any(row.get(column) is None for column in required):
                continue
            rows.append(row)
    except (ValueError, csv.Error):
        return None
    return rows, meta


_ACTIVITY_NUMBERS: dict[str, Callable[[Any], Any]] = {
    "pchembl_value": _as_float,
    "confidence_score": _as_int,
}
"""活性表读回时要还原类型的列。两列都是下游的排序/筛选依据，缺失即丢行。"""

_ACTIVITY_REQUIRED = ("pchembl_value", "confidence_score")

_ANALOG_NUMBERS: dict[str, Callable[[Any], Any]] = {
    "similarity": _as_float,
    "max_phase": _as_float,
    "mw_freebase": _as_float,
    "alogp": _as_float,
    "qed_weighted": _as_float,
    "num_ro5_violations": _as_int,
}
"""相似分子表读回时要还原类型的列。

`similarity` 必须还原成数值：整表是按它排的序，字符串比较会让 `91.9` 排在 `100` 后面，
而那份表看上去完全正常。
"""

_ANALOG_REQUIRED = ("similarity",)


def _snapshot_from_cache(rows: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]) -> _Snapshot:
    """把通用缓存行还原成活性快照。

    计数不放在 CSV 里而是放在 `.meta.json` 里，因为它们是**摘要的一部分**：命中多少、
    取回多少、剔除多少都由网络决定，只存表的话缓存命中时复现不出来。
    """
    return _Snapshot(
        molecules=[dict(row) for row in rows],
        total_all=int(meta.get("total_all") or 0),
        total_matched=int(meta.get("total_matched") or 0),
        fetched=int(meta.get("fetched") or 0),
        dropped={key: int((meta.get("dropped") or {}).get(key, 0) or 0) for key in DROP_REASONS},
        fingerprint=str(meta.get("fingerprint") or ""),
    )


def _analog_snapshot_from_cache(
    rows: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]
) -> _AnalogSnapshot:
    """把通用缓存行还原成相似分子快照。"""
    return _AnalogSnapshot(
        molecules=[dict(row) for row in rows],
        total_matched=int(meta.get("total_matched") or 0),
        fetched=int(meta.get("fetched") or 0),
        merged_away=int(meta.get("merged_away") or 0),
        without_structure=int(meta.get("without_structure") or 0),
        query_smiles=str(meta.get("query_smiles") or ""),
        query_id=str(meta.get("query_id") or ""),
        query_name=str(meta.get("query_name") or ""),
        fingerprint=str(meta.get("fingerprint") or ""),
    )


def _activity_meta(snapshot: _Snapshot, context: Mapping[str, Any]) -> dict[str, Any]:
    """活性快照的元数据。`dropped` 只留非零项——零值写进去只会让文件变长。"""
    return {
        **context,
        "molecule_count": len(snapshot.molecules),
        "total_all": snapshot.total_all,
        "total_matched": snapshot.total_matched,
        "fetched": snapshot.fetched,
        "dropped": {key: count for key, count in snapshot.dropped.items() if count},
    }


def _analog_meta(snapshot: _AnalogSnapshot, *, threshold: int) -> dict[str, Any]:
    """相似分子快照的元数据。

    刻意不存 `source_url`：相似性检索的 URL 里嵌着整段结构串，存档等于把那份结构再抄
    一份，而 `query_smiles` 已经把 URL 完全决定了。
    """
    return {
        "threshold": threshold,
        "query_id": snapshot.query_id,
        "query_name": snapshot.query_name,
        "query_smiles": snapshot.query_smiles,
        "molecule_count": len(snapshot.molecules),
        "total_matched": snapshot.total_matched,
        "fetched": snapshot.fetched,
        "merged_away": snapshot.merged_away,
        "without_structure": snapshot.without_structure,
        "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ---------------------------------------------------------------------------
# 活性摘要的渲染
# ---------------------------------------------------------------------------
def _activity_span(molecules: Sequence[Mapping[str, Any]]) -> str:
    """活性范围与中位数。

    中位数与两端一起给：只报「最强 11.0、最弱 6.0」的话，读者会以为中间是均匀铺开的，
    而真实分布常常是顶端几条极高、其余挤在下界附近。中位数是把这个形状说清楚的最省
    字数的读数。
    """
    values = [float(item["pchembl_value"]) for item in molecules]
    if not values:
        return ""
    median = statistics.median(values)
    return f"pChEMBL {min(values):.2f}–{max(values):.2f}，中位 {median:.2f}"


def _strongest(molecules: Sequence[Mapping[str, Any]]) -> str:
    """最强那一条的原文写法。分子表已按活性降序，第一条就是它。"""
    top = molecules[0]
    unit = f" {top['standard_units']}" if top["standard_units"] else ""
    name = f"（{top['molecule_pref_name']}）" if top["molecule_pref_name"] else ""
    return (
        f"{top['molecule_chembl_id']}{name} · {top['standard_type']} "
        f"{top['standard_value']}{unit} · pChEMBL {top['pchembl_value']:.2f}"
    )


def _count_by(molecules: Sequence[Mapping[str, Any]], column: str) -> dict[Any, int]:
    """按某一列取值计数。空值归到「（未标注）」，不并进别的取值里。"""
    counts: dict[Any, int] = {}
    for item in molecules:
        value = item.get(column) or "（未标注）"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _render_digest(
    *,
    accession: str,
    choice: _TargetChoice,
    snapshot: _Snapshot,
    conditions: str,
    provenance: str,
) -> str:
    """这次检索的一整段说明。

    **每种「空结果」都单列一段，且各说各的下一步。** 「一条记录都没有」「有条记录但
    条件没筛中」「筛中了但取回的都不能用」是三种完全不同的处境：第一种是孤儿靶点
    （结论的可信度差一个量级），第二种是参数调得过严，第三种多半是置信度卡住了。
    合并成一句「没有找到可用数据」等于把三种处境抹平，而模型手里的下一步动作正是
    由这三种处境决定的。
    """
    molecules = snapshot.molecules
    head = f"ChEMBL 靶点 {choice.chembl_id}"
    if choice.name:
        head += f" · {choice.name}"
    lines = [f"{head}（对应 {accession}，单一蛋白）"]

    if snapshot.total_all == 0:
        lines.append("  这个靶点在 ChEMBL 里没有任何活性记录。")
        lines.append(
            "  这意味着它是孤儿靶点：没有已知配体可以当参照物，对接结果也无法用实测"
            "活性来验证，只能依赖打分函数本身的可靠性。"
        )
        lines.append(
            "  这一点要明确告诉用户，别让它被当成一个和别的靶点一样的筛选任务——"
            "同样的流程，结论的可信度差一个量级。"
        )
        return "\n".join([*lines, f"  {provenance}"])

    lines.append(f"  该靶点活性记录共 {snapshot.total_all:,} 条。")
    if snapshot.total_matched == 0:
        lines.append(f"  按当前条件（{conditions}）一条都没命中，是条件定得过严。")
        lines.append(
            "  可以试着：降低 min_pchembl（7 约等于 100 nM，6 约等于 1 μM）；"
            "放宽 measure_types；或把 min_confidence 降到 5 以内以纳入同源蛋白推定的"
            "数据，但那会把别的蛋白的活性也算进来，可信度明显下降"
        )
        return "\n".join([*lines, f"  {provenance}"])

    if snapshot.fetched >= snapshot.total_matched:
        lines.append(
            f"  按当前条件（{conditions}）命中 {snapshot.total_matched:,} 条，已全部取回。"
        )
    else:
        lines.append(
            f"  按当前条件（{conditions}）命中 {snapshot.total_matched:,} 条；"
            f"只取了活性最强的 {snapshot.fetched:,} 条。"
        )
        lines.append(
            "  因此下面的统计说的是取回的这一批，不是整个靶点。要更多就调大 max_rows，"
            "或收紧 min_pchembl 让命中数进到上限之内。"
        )

    if not molecules:
        lines.append("  但取回的记录里一条可用的都没有：")
        for key, count in snapshot.dropped.items():
            if count:
                lines.append(f"    {DROP_REASONS[key]}，{count:,} 条")
        lines.append("  多半是 min_confidence 卡住了，先看看放宽到 7 还剩多少")
        return "\n".join([*lines, f"  {provenance}"])

    merged = snapshot.merged_away
    if merged:
        lines.append(
            f"  得到 {len(molecules):,} 个分子（同一化合物的多种盐型与重复测定已归并，"
            f"合并掉 {merged:,} 条记录）。"
        )
    else:
        lines.append(f"  得到 {len(molecules):,} 个分子。")

    span = _activity_span(molecules)
    if span:
        lines.append(f"  这 {len(molecules):,} 个分子的活性范围 {span}")
    strongest = max(float(item["pchembl_value"]) for item in molecules)
    lines.append(f"  最强：{_strongest(molecules)}")
    if strongest >= POTENCY_FLOOR_HINT:
        picomolar = 10 ** (12 - strongest)
        lines.append(
            f"  注意：最上面这批 pChEMBL 已到 {strongest:.2f}（约 {picomolar:.0f} pM）。"
            "这种量级多半不是精确测量（长时间预孵育的不可逆抑制剂、仪器读数下限都能造出"
            "这样的表观值），当「非常强」读即可。要拿已知活性校准对接打分，用中段的分子更稳。"
        )

    kinds = ", ".join(
        f"{name} {count}" for name, count in _take_n(_count_by(molecules, "standard_type"), 4)
    )
    scores = sorted({int(item["confidence_score"]) for item in molecules})
    low = scores[0] if scores else 0
    lines.append(f"  测定类型：{kinds}；置信度均不低于 {low}（靶点归属为直接测定）")

    named = [item for item in molecules if item["molecule_pref_name"]]
    if named:
        samples = "、".join(str(item["molecule_pref_name"]) for item in named[:4])
        lines.append(f"  已有药名的化合物 {len(named)} 个（{samples}…）")

    reasons = [
        f"{DROP_REASONS[key]} {count:,} 条" for key, count in snapshot.dropped.items() if count
    ]
    if reasons:
        lines.append("  取回后剔除：" + " · ".join(reasons))

    if choice.alternatives:
        lines.append(
            "  注意：这个编号在 ChEMBL 还对应其他单一蛋白靶点（"
            + "、".join(choice.alternatives)
            + "），这里只用了一个；要换目标请直接说明"
        )
    if choice.skipped_complexes:
        lines.append(
            f"  另有 {choice.skipped_complexes} 个同编号的复合物/家族条目已排除"
            "（它们的活性属于整个复合物，不是这一个蛋白）"
        )

    lines.append(f"  {provenance}")
    return "\n".join(lines)


def _entry_summary(
    accession: str, choice: _TargetChoice, molecules: Sequence[Mapping[str, Any]]
) -> str:
    """登记表条目的一行摘要。它会被别的工具在清单里复读，所以要能独立读懂。"""
    values = [float(item["pchembl_value"]) for item in molecules]
    span = f"pChEMBL {min(values):.1f}–{max(values):.1f}" if values else "无可用分子"
    return " · ".join(
        part
        for part in (
            f"已知活性配体 {len(molecules):,} 个",
            choice.name or accession,
            f"ChEMBL {choice.chembl_id}",
            span,
        )
        if part
    )


def _entry_meta(
    *,
    accession: str,
    choice: _TargetChoice,
    snapshot: _Snapshot,
    path: Path,
) -> dict[str, Any]:
    """挂在登记表条目上的结构化字段。

    `top_molecules` 只放前 `MAX_TOP_MOLECULES` 条：meta 会被别的工具整段回注，把一千
    多个分子塞进去的结果是它被截断、前面的字段也跟着看不见。完整清单在那份 CSV 里。
    """
    molecules = snapshot.molecules
    values = [float(item["pchembl_value"]) for item in molecules]
    return {
        "source": "chembl",
        "accession": accession,
        "target_chembl_id": choice.chembl_id,
        "target_name": choice.name,
        "activity_records_total": snapshot.total_all,
        "activity_records_matched": snapshot.total_matched,
        "records_fetched": snapshot.fetched,
        "records_merged": snapshot.merged_away,
        "molecule_count": len(molecules),
        "pchembl_min": min(values) if values else None,
        "pchembl_max": max(values) if values else None,
        "top_molecules": list(molecules[:MAX_TOP_MOLECULES]),
        "dropped": {key: count for key, count in snapshot.dropped.items() if count},
        "retrieved_at": _mtime_label(path),
        "source_url": f"{BASE_URL}/activity?target_chembl_id={choice.chembl_id}",
        "file": _relative(path),
    }


def _mtime_label(path: Path) -> str:
    """写盘文件的修改时间，读不出来时给空串。

    `retrieved_at` 宁可空着也不填「此刻」：那是渲染这一行的时间，不是数据取回的时间，
    缓存命中时两者能差好几天，而这一栏的用途恰恰是让人判断数据有多旧。
    """
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 相似分子的渲染
# ---------------------------------------------------------------------------
def _short_smiles(smiles: str, *, limit: int = 40) -> str:
    """结构串截断。摘要那一行放不下全串，而它只用来认人，不用来当输入。"""
    return smiles if len(smiles) <= limit else f"{smiles[: limit - 1]}…"


def _describe_query(query: _Query) -> str:
    """查询分子的短标签，用在摘要与登记表条目上。"""
    if query.chembl_id:
        return f"{query.chembl_id}（{query.name}）" if query.name else query.chembl_id
    return _short_smiles(query.smiles)


def _analog_line(item: Mapping[str, Any]) -> str:
    """一行一个相似分子。字段顺序固定，好让多行之间能竖着对读。"""
    parts = [str(item["molecule_chembl_id"])]
    if item["molecule_pref_name"]:
        parts.append(str(item["molecule_pref_name"]))
    parts.append(f"相似度 {float(item['similarity']):.1f}%")
    parts.append(_max_phase_label(item.get("max_phase")) or "无开发记录")
    mass = item.get("mw_freebase")
    if mass is not None:
        parts.append(f"MW {float(mass):.1f}")
    logp = item.get("alogp")
    if logp is not None:
        parts.append(f"logP {float(logp):.2f}")
    return " · ".join(parts)


def _render_analog_digest(
    *,
    query: _Query,
    snapshot: _AnalogSnapshot,
    threshold: int,
    truncated: bool,
    provenance: str,
) -> str:
    """这次相似性检索的一整段说明。

    最后一句（「相似性只看结构、与活性无关」）不是客套：这个工具的产物长着一张配体集
    的脸，很容易被当成「一批对这个靶点有活性的分子」喂给下一步，而它其实只是一批
    长得像的化合物。这句话是这条边界唯一的说明。
    """
    lines = [f"ChEMBL 相似性检索 · 查询 {_describe_query(query)}"]
    lines.append(f"  查询结构：{query.smiles}")

    if snapshot.total_matched == 0:
        lines.append(f"  相似度 ≥ {threshold}% 的分子一个都没有。")
        lines.append(
            "  要么这个结构在 ChEMBL 里是独一份（本身就是个好信号），要么结构写得不对。"
            f"  可以把 min_similarity 降到 {max(MIN_SIMILARITY, threshold - 15)} 再看——"
            f"下界是 {MIN_SIMILARITY}，再低服务端会直接拒绝"
        )
        return "\n".join([*lines, f"  {provenance}"])

    lines.append(
        f"  相似度 ≥ {threshold}%（Morgan 指纹的 Tanimoto 系数）"
        f"命中 {snapshot.total_matched:,} 条记录。"
    )
    if truncated:
        lines.append(
            f"  只取了最像的 {snapshot.fetched:,} 条——服务端就是按相似度降序返回的，"
            "所以截断丢掉的是最不像的那些。"
        )
    else:
        lines.append(f"  已全部取回（{snapshot.fetched:,} 条记录）。")

    if not snapshot.molecules:
        lines.append("  但清洗归并之后一个可用分子都不剩：")
        if snapshot.without_structure:
            lines.append(
                f"    {snapshot.without_structure:,} 条没有结构式"
                "（molecule_type 为空或 Unknown，拿不到结构串，也就无法用于对接）"
            )
        lines.append(
            "  剩下的都与查询分子同属一个母体。换一个结构更「中心」的分子再查，"
            "或者把阈值降到 70 看有没有别的母核"
        )
        return "\n".join([*lines, f"  {provenance}"])

    if snapshot.merged_away:
        lines.append(
            f"  得到 {len(snapshot.molecules):,} 个分子"
            f"（同一化合物的盐型、共晶、立体异构已归并，合并掉 {snapshot.merged_away:,} 条记录）。"
        )
    else:
        lines.append(f"  得到 {len(snapshot.molecules):,} 个分子。")
    if snapshot.without_structure:
        lines.append(f"  另有 {snapshot.without_structure:,} 条记录没有结构式，已剔除。")

    lines.append("  最像的几个：")
    for item in snapshot.molecules[:5]:
        lines.append("    " + _analog_line(item))

    approved = sum(1 for item in snapshot.molecules if _as_float(item.get("max_phase")) == 4)
    named = sum(1 for item in snapshot.molecules if item["molecule_pref_name"])
    bits = [f"有药名的 {named} 个"]
    if approved:
        bits.append(f"已上市药物 {approved} 个")
    if _as_int(snapshot.molecules[0].get("num_ro5_violations")) is not None:
        clean = sum(
            1 for item in snapshot.molecules if _as_int(item.get("num_ro5_violations")) == 0
        )
        bits.append(f"不违反 Lipinski 五规则的 {clean} 个")
    lines.append("  其中：" + " · ".join(bits))

    lines.append(
        "  相似性只看结构，与活性无关——长得像不等于有活性，也不等于对这个靶点有活性。"
        "要确认活性得看 search_actives 的结果，或对这些分子另做一轮活性检索"
    )
    lines.append(f"  {provenance}")
    return "\n".join(lines)


def _analog_entry_summary(query: _Query, snapshot: _AnalogSnapshot, threshold: int) -> str:
    """登记表条目的一行摘要，会被别的工具在清单里复读，所以要能独立读懂。"""
    values = [float(item["similarity"]) for item in snapshot.molecules]
    span = f"相似度 {min(values):.1f}–{max(values):.1f}%" if values else "无可用分子"
    return " · ".join(
        (
            f"相似分子 {len(snapshot.molecules):,} 个",
            f"查自 {_describe_query(query)}",
            f"阈值 {threshold}%",
            span,
        )
    )


def _analog_entry_meta(
    *,
    query: _Query,
    snapshot: _AnalogSnapshot,
    threshold: int,
    path: Path,
) -> dict[str, Any]:
    """挂在登记表条目上的结构化字段。

    `top_molecules` 只放前 `MAX_TOP_MOLECULES` 条：meta 会被别的工具整段回注，塞进几百
    个分子只会让它被截断、连前面的字段一起看不见。完整清单在那份 CSV 里。
    """
    values = [float(item["similarity"]) for item in snapshot.molecules]
    return {
        "source": "chembl",
        "kind": "analogs",
        "query_id": query.chembl_id,
        "query_name": query.name,
        "query_smiles": query.smiles,
        "similarity_threshold": threshold,
        "records_matched": snapshot.total_matched,
        "records_fetched": snapshot.fetched,
        "records_merged": snapshot.merged_away,
        "records_without_structure": snapshot.without_structure,
        "molecule_count": len(snapshot.molecules),
        "similarity_min": min(values) if values else None,
        "similarity_max": max(values) if values else None,
        "top_molecules": list(snapshot.molecules[:MAX_TOP_MOLECULES]),
        "retrieved_at": _mtime_label(path),
        "file": _relative(path),
    }


# ---------------------------------------------------------------------------
# 分子画像的渲染
# ---------------------------------------------------------------------------
def _flag_line(record: Mapping[str, Any]) -> str:
    """给药方式与几个「这是什么性质的东西」标记。"""
    flags: list[str] = []
    routes = [
        label
        for key, label in (("oral", "口服"), ("parenteral", "注射"), ("topical", "外用"))
        if _yes_no(record.get(key))
    ]
    if routes:
        flags.append("给药方式：" + "、".join(routes))
    if _yes_no(record.get("prodrug")):
        flags.append("前药（本身无活性，体内转化后才起效）")
    if _yes_no(record.get("natural_product")):
        flags.append("天然产物")
    if _yes_no(record.get("orphan")):
        flags.append("罕见病用药")
    return " · ".join(flags)


def _property_lines(properties: Mapping[str, Any]) -> list[str]:
    """类药性速写。

    **数字都给，判断留给读者**——「多少算合格」在不同项目里差得很远，替读者拍板比
    不给数字更坏。只有 Lipinski 违反项不为零时补一句解释，因为那一项的常见误读是
    「违反就不能成药」（不少上市药同样违反）。
    """

    def shown(key: str) -> str:
        number = _as_float(properties.get(key))
        return "—" if number is None else f"{number:g}"

    lines = [
        "  类药性（ChEMBL 算好存在库里的）："
        f"MW {shown('mw_freebase')} · logP {shown('alogp')} · "
        f"HBD {shown('hbd')} · HBA {shown('hba')}"
    ]
    lines.append(
        f"    TPSA {shown('psa')} · 可旋转键 {shown('rtb')} · "
        f"芳香环 {shown('aromatic_rings')} · 重原子 {shown('heavy_atoms')}"
    )
    tail: list[str] = []
    formula = str(properties.get("full_molformula") or "")
    if formula:
        tail.append(f"分子式 {formula}")
    quality = _as_float(properties.get("qed_weighted"))
    if quality is not None:
        tail.append(f"QED {quality:.2f}（0–1，越接近 1 越像已知药物）")
    violations = _as_int(properties.get("num_ro5_violations"))
    if violations is not None:
        tail.append(f"Lipinski 五规则违反 {violations} 项")
    if tail:
        lines.append("    " + " · ".join(tail))
    if violations:
        lines.append(
            "    Lipinski 违反项不为零会让口服吸收打折扣，但它是经验规则不是红线"
            "——不少上市药同样违反，别据此直接淘汰"
        )
    return lines


def _mechanism_lines(
    mechanisms: Sequence[Mapping[str, Any]], targets: Mapping[str, str]
) -> list[str]:
    """作用机制那几行。

    「没有记录」这一句要说成「还没被系统研究过」而不是「不作用于任何靶点」：前者是
    ChEMBL 收录状态的事实，后者是一个关于分子的断言，而这份数据不支持那个断言。
    """
    if not mechanisms:
        return [
            "  已知作用机制：ChEMBL 里没有记录。这通常说明它还没被系统研究过，"
            "不代表它不作用于任何靶点"
        ]
    lines = [f"  已知作用机制 {len(mechanisms)} 条："]
    for item in mechanisms:
        target_id = str(item.get("target_chembl_id") or "")
        action = str(item.get("action_type") or "")
        parts = [
            target_id,
            targets.get(target_id) or "（ChEMBL 未收录名称）",
            str(item.get("mechanism_of_action") or ""),
            f"（{action.replace('_', ' ')}）" if action else "",
        ]
        lines.append("    " + " · ".join(part for part in parts if part))
    lines.append(
        "    这一栏也是脱靶风险的线索：作用机制里的靶点不止一个时，"
        "命中分子对这个靶点的选择性就要单独看"
    )
    return lines


def _indication_lines(indications: Sequence[Mapping[str, Any]], total: int) -> list[str]:
    """适应症那几行。样本有限时才写「最相关的几条」，全都在时不必多这一句。"""
    if not total:
        return ["  适应症：ChEMBL 里没有记录"]
    head = f"  适应症 {total} 条（MeSH 术语，ChEMBL 不提供中文译名）"
    if total > len(indications):
        head += "，最相关的几条："
    else:
        head += "："
    lines = [head]
    for item in indications:
        label = str(item.get("mesh_heading") or item.get("efo_term") or "").strip()
        phase = _max_phase_label(item.get("max_phase_for_ind"))
        lines.append("    " + " · ".join(part for part in (label, phase) if part))
    return lines


def _render_molecule(
    record: Mapping[str, Any],
    *,
    mechanisms: Sequence[Mapping[str, Any]],
    targets: Mapping[str, str],
    indications: Sequence[Mapping[str, Any]],
    indication_total: int,
    origin: str,
) -> str:
    """一个分子的完整画像。

    「没有结构式」那一条要说出后果（不能对接、物性不适用），而不只是陈述缺失：这一段
    的读者正要决定要不要把手上这个分子拿去对接，说明它做不到才是这段的用途。
    """
    code = str(record.get("molecule_chembl_id") or "")
    name = str(record.get("pref_name") or "")
    lines = [f"ChEMBL 分子 {code}" + (f" · {name}" if name else "（ChEMBL 里没有药名）")]

    kind = str(record.get("molecule_type") or "类型未标注")
    status = f"  类型：{kind}"
    phase = _max_phase_label(record.get("max_phase"))
    if phase:
        status += f" · {phase}"
    approval = _as_int(record.get("first_approval"))
    if approval:
        status += f" · {approval} 年首次批准"
    lines.append(status)

    alerts: list[str] = []
    if _yes_no(record.get("withdrawn_flag")):
        alerts.append("已撤市")
    if _as_int(record.get("black_box_warning")):
        alerts.append("带黑框警告")
    if alerts:
        lines.append("  警示：" + "、".join(alerts) + "——挑命中分子时这条比活性数值更要紧")

    flags = _flag_line(record)
    if flags:
        lines.append("  " + flags)

    structures = record.get("molecule_structures") or {}
    smiles = str(structures.get("canonical_smiles") or "").strip()
    if smiles:
        lines.append(f"  结构：{smiles}")
        inchi_key = str(structures.get("standard_inchi_key") or "")
        if inchi_key:
            lines.append(
                f"  InChIKey：{inchi_key}（这把钥匙唯一标识结构，可以跨库核对是不是同一个东西）"
            )
    else:
        lines.append(
            f"  结构：没有结构式（structure_type 是 {record.get('structure_type') or '未标注'}）。"
            "这是生物药/大分子，既不能拿来对接，下面那套小分子物性也不适用"
        )

    properties = record.get("molecule_properties") or {}
    if properties:
        lines.extend(_property_lines(properties))

    lines.extend(_mechanism_lines(mechanisms, targets))
    lines.extend(_indication_lines(indications, indication_total))

    synonyms = [
        str(item.get("molecule_synonym"))
        for item in (record.get("molecule_synonyms") or [])
        if (item or {}).get("molecule_synonym")
    ]
    if synonyms:
        tail = "…" if len(synonyms) > 6 else ""
        lines.append(f"  别名 {len(synonyms)} 个：" + "、".join(synonyms[:6]) + tail)
    atc = [str(item) for item in (record.get("atc_classifications") or []) if item]
    if atc:
        lines.append("  ATC 分类：" + "、".join(atc[:6]))

    lines.append(f"  {origin}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class SearchActivesTool(Tool):
    """按 UniProt 编号取 ChEMBL 里的已知活性分子，登记成 ligandset。"""

    spec = ToolSpec(
        name="search_actives",
        description=(
            "检索某靶点在 ChEMBL 里的已知活性化合物，并把它**登记成一个配体集**。"
            "输入是 UniProt 编号（fetch_target 取回来的那个），输出是一个可以直接喂给"
            "对接工具的 ligandset 句柄，外加一行可成药性摘要：这个靶点一共多少条活性记录、"
            "按当前条件命中多少、已知最强活性是多少、有没有已上市的药。**pChEMBL 越大活性"
            "越强**（它是摩尔浓度的负对数），下游排序与设阈值都该用它，不要用原始浓度值。"
            "默认只取单一蛋白靶点、精确定量的测定、置信度 8 以上的记录，并已剔除重复测定、"
            "未测到抑制的不等式记录和没有结构的化合物。**达到取回上限时保留的是活性最强的"
            "那些**（内部按活性排序取回，不是随便一批）。单页一千条约 5–11 秒，一次调用"
            "通常十几秒到一分钟。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "accession": {
                    "type": "string",
                    "description": (
                        "UniProt 编号，例如 P00533。必须来自 fetch_target 的返回"
                    ),
                },
                "min_pchembl": {
                    "type": "number",
                    "description": (
                        "活性下限，越大越严。6 约为 1 μM，7 约为 100 nM，8 约为 10 nM（默认 6）"
                    ),
                },
                "measure_types": {
                    "type": "string",
                    "description": (
                        "只取这几种定量测定，对应 ChEMBL 的 standard_type。"
                        "默认 IC50,Ki,Kd；要连细胞活性一起算也可以加 EC50，但那是另一套量纲"
                    ),
                },
                "min_confidence": {
                    "type": "integer",
                    "description": (
                        "测定置信度下限（ChEMBL 对「靶点归属有多可信」的分级）。"
                        "8 以上是直接测定的靶点，5 以下多为同源蛋白推定——放低会掺进别的"
                        "蛋白的数据（默认 8）"
                    ),
                },
                "max_rows": {
                    "type": "integer",
                    "description": (
                        "最多取回多少条活性记录，上限 5000。到顶时留下的是活性最强的那些"
                        "（默认 2000）"
                    ),
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true 时忽略本地缓存重新下载（认为远端数据已更新时才用）",
                },
            },
            "required": ["accession"],
        },
    )
    primary_argument = "accession"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：search_actives 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            code = normalize_accession(str(arguments.get("accession") or ""))
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        measure = str(arguments.get("measure_types") or DEFAULT_MEASURE_TYPES).strip()
        floor = _as_float(arguments.get("min_pchembl"))
        floor = 6.0 if floor is None else floor
        confidence_floor = _as_int(arguments.get("min_confidence"))
        confidence_floor = 8 if confidence_floor is None else confidence_floor
        want = _as_int(arguments.get("max_rows"))
        want = 2000 if want is None else max(1, min(want, MAX_ROWS_LIMIT))
        refresh = bool(arguments.get("refresh"))

        conditions = f"pChEMBL ≥ {floor:g}、{measure}、置信度 ≥ {confidence_floor}"

        # 缓存键只用「编号 + 条件」，**不含 ChEMBL 靶点编号**——靶点编号得先联网解析才
        # 知道，把它算进键里就意味着缓存永远不可能纯读盘（每次调用都得先打一次网络）。
        key = fingerprint_parts(code, f"{floor:g}", measure, confidence_floor, length=8)
        path = _cache_path(code, key)

        if not refresh:
            try:
                cached = _read_cache(
                    path, numbers=_ACTIVITY_NUMBERS, required=_ACTIVITY_REQUIRED
                )
            except OSError:
                cached = None
        else:
            cached = None

        if cached is not None:
            rows, meta = cached
            snapshot = _snapshot_from_cache(rows, meta)
            choice = _TargetChoice(
                chembl_id=str(meta.get("target_chembl_id") or ""),
                name=str(meta.get("target_name") or ""),
                alternatives=[str(item) for item in meta.get("target_alternatives") or []],
                skipped_complexes=_as_int(meta.get("skipped_complexes")) or 0,
            )
            origin = f"读自本地缓存 {_relative(path)}（未联网；要刷新用 refresh=true）"
        else:
            try:
                choice = _resolve_target(code, timeout=TIMEOUT)

                # 服务端确实生效的四个条件。别往这里加 confidence_score 或 assay__* ——
                # ChEMBL 会静默忽略它们，结果看起来筛过了其实没筛。
                base_params = {"target_chembl_id": choice.chembl_id}
                matched_params = {
                    **base_params,
                    "pchembl_value__gte": f"{floor:g}",
                    "standard_type__in": measure,
                    "potential_duplicate": "0",
                    "order_by": "-pchembl_value",
                }
                total_all = _count(ACTIVITY_URL, base_params, timeout=TIMEOUT)
                total_matched = _count(ACTIVITY_URL, matched_params, timeout=TIMEOUT)

                rows = (
                    _fetch_activities(matched_params, want=want, timeout=TIMEOUT)
                    if total_matched
                    else []
                )
                facts = _assay_facts(
                    (str(item.get("assay_chembl_id") or "") for item in rows), timeout=TIMEOUT
                )
                molecules, dropped = _select(
                    rows, confidence=facts, min_confidence=confidence_floor
                )
            except http.ToolError as error:
                return ToolResult(output=str(error), failed=True)

            text = _render_csv(molecules)
            snapshot = _Snapshot(
                molecules=molecules,
                total_all=total_all,
                total_matched=total_matched,
                fetched=len(rows),
                dropped=dropped,
                fingerprint=fingerprint_text(text),
            )
            try:
                _write_cache(
                    path,
                    text=text,
                    fingerprint=snapshot.fingerprint,
                    meta=_activity_meta(
                        snapshot,
                        {
                            "accession": code,
                            "conditions": conditions,
                            "target_chembl_id": choice.chembl_id,
                            "target_name": choice.name,
                            "target_alternatives": choice.alternatives,
                            "skipped_complexes": choice.skipped_complexes,
                            "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        },
                    ),
                )
            except OSError as error:
                return ToolResult(
                    output=(
                        f"写盘 {_relative(path)} 失败（{type(error).__name__}：{error}）。"
                        "数据已经在手上，但登记表里的条目要指出一个磁盘位置，"
                        "写不进去就不登记——请检查该目录是否可写"
                    ),
                    failed=True,
                )
            origin = (
                f"已下载 {len(rows):,} 条活性记录并写盘 {_relative(path)}"
                f"（指纹 {snapshot.fingerprint}）"
            )

        # 登记而不是每次新建：同一组条件被反复问是常态（问完有哪些分子，再问最强的几个），
        # 内容寻址之后「同一份东西」只该在登记表里出现一次。
        # include_archived 是必要的：配体集登记时会挂上当前受体作为父项，切换受体后会被
        # 搁置——那时若看不见它，同一份数据会被重复登记一份。
        if snapshot.molecules:
            existing = ledger.find(
                kind="ligandset", fingerprint=snapshot.fingerprint, include_archived=True
            )
            if existing:
                registered = f"登记表里已有同一份：{existing[0].handle}（未重复登记）"
                handle = str(existing[0].handle)
            else:
                entry = ledger.add(
                    "ligandset",
                    path=path,
                    summary=_entry_summary(code, choice, snapshot.molecules),
                    fingerprint=snapshot.fingerprint,
                    meta=_entry_meta(
                        accession=code, choice=choice, snapshot=snapshot, path=path
                    ),
                )
                registered = f"已登记 {entry.handle}"
                handle = str(entry.handle)
        else:
            registered = "没有登记任何东西（可用分子为零）"
            handle = ""

        provenance = " · ".join(part for part in (origin, registered) if part)
        return ToolResult(
            output=_render_digest(
                accession=code,
                choice=choice,
                snapshot=snapshot,
                conditions=conditions,
                provenance=provenance,
            ),
            meta={
                "accession": code,
                "target_chembl_id": choice.chembl_id,
                "molecule_count": len(snapshot.molecules),
                "handle": handle,
                "fingerprint": snapshot.fingerprint,
                "file": str(path),
            },
        )


class FindAnalogsTool(Tool):
    """按结构相似性找 ChEMBL 里的类似化合物，登记成 ligandset。"""

    spec = ToolSpec(
        name="find_analogs",
        description=(
            "按结构相似性找出 ChEMBL 里已知的**类似化合物**，并把它**登记成一个配体集**。"
            "两个典型用途：**新颖性检查**（对接刚命中的分子，看它是不是早就有人做过、甚至"
            "已经上市）与**骨架跃迁**（拿一个已知活性分子换一副结构骨架）。query 给 ChEMBL"
            "分子编号（形如 CHEMBL939）或直接给一段 SMILES，工具自己识别。min_similarity 是"
            "40–100 的整数百分比：85 约等于「同一个系列」，70 约等于「同一个母核」，50 以下"
            "动辄几千个分子（默认 85）。结果按相似度从高到低，已按母体归并（盐型与共晶不会"
            "占满前列），查询分子自身也已排除。**手上有编号就给编号**：按编号查时「排除自身」"
            "按母体编号判定，连查询分子的盐型、共晶、立体异构一并排掉；只给结构串时没有母体"
            "可依，只能按结构串全等排除，于是查询分子自己的盐型会以接近 100% 的相似度留在"
            "结果里。**相似性只看结构、与活性无关——长得像不等于有活性。**"
            "服务端按相似度降序返回，所以到取回上限时留下的是最像的那些。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "ChEMBL 分子编号（如 CHEMBL939）或一段 SMILES。"
                        "编号取自 search_actives / find_analogs 的结果"
                    ),
                },
                "min_similarity": {
                    "type": "integer",
                    "description": (
                        "相似度下限，40–100 的整数。低于 40 服务端直接拒绝，不是「结果变少」"
                        "（默认 85）"
                    ),
                },
                "max_rows": {
                    "type": "integer",
                    "description": (
                        "最多取回多少个分子，上限 1000（每页 200，默认就取一页）。"
                        "到顶时留下的是最像的那些"
                    ),
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true 时忽略本地缓存重新检索（认为远端数据已更新时才用）",
                },
            },
            "required": ["query"],
        },
    )
    primary_argument = "query"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：find_analogs 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("query") or "").strip()
        if not raw:
            return ToolResult(
                output=(
                    "没有给查询分子。给 ChEMBL 分子编号（形如 CHEMBL939）或一段 SMILES"
                ),
                failed=True,
            )

        threshold = _as_int(arguments.get("min_similarity"))
        threshold = 85 if threshold is None else threshold
        if not MIN_SIMILARITY <= threshold <= MAX_SIMILARITY:
            return ToolResult(
                output=(
                    f"min_similarity 要在 {MIN_SIMILARITY}–{MAX_SIMILARITY} 之间，"
                    f"给的是 {threshold}。下界 {MIN_SIMILARITY} 是 ChEMBL 自己定的——"
                    "低于它服务端直接拒绝，不是「结果会少一些」。"
                    "想要更多分子请调大 max_rows，别靠降阈值"
                ),
                failed=True,
            )
        want = _as_int(arguments.get("max_rows"))
        want = SIMILARITY_PAGE if want is None else max(1, min(want, MAX_ANALOG_ROWS))
        refresh = bool(arguments.get("refresh"))

        if CHEMBL_ID_PATTERN.fullmatch(raw):
            try:
                incoming_id = normalize_molecule_id(raw)
            except http.ToolError as error:
                return ToolResult(output=str(error), failed=True)
            slug = incoming_id
            query_smiles = ""
        else:
            incoming_id = ""
            try:
                query_smiles = sanity_check_smiles(raw)
            except http.ToolError as error:
                return ToolResult(output=str(error), failed=True)
            slug = f"smiles-{fingerprint_text(query_smiles)[:8]}"

        # 缓存键只用「查询是谁 + 阈值 + 条数」，**不含联网才知道的东西**：编号要先去
        # /molecule/ 解析成结构、结构又要先拼进 URL 路径。把这个解析结果算进键里，缓存就
        # 永远不会是纯读盘的那一步。给结构串输入时，用结构自身的内容指纹当滑杆，
        # 同一段结构的不同写法也会得到同一个键。
        key = fingerprint_parts(slug, str(threshold), want, length=8)
        path = _analog_cache_path(slug, key)

        if not refresh:
            try:
                cached = _read_cache(
                    path, numbers=_ANALOG_NUMBERS, required=_ANALOG_REQUIRED
                )
            except OSError:
                cached = None
        else:
            cached = None

        if cached is not None:
            rows, meta = cached
            snapshot = _analog_snapshot_from_cache(rows, meta)
            # 命中缓存时排除已经做完了，parent_id 留空即可——它只用于「排除自己」。
            found = _Query(
                smiles=snapshot.query_smiles,
                chembl_id=snapshot.query_id,
                name=snapshot.query_name,
            )
            origin = f"读自本地缓存 {_relative(path)}（未联网；要刷新用 refresh=true）"
        else:
            try:
                if incoming_id:
                    found = _molecule_query(incoming_id, timeout=TIMEOUT)
                else:
                    found = _Query(smiles=query_smiles)
                records, total = _fetch_analogs(
                    found, threshold=threshold, want=want, timeout=TIMEOUT
                )
                molecules, merged_away, without_structure = _select_analogs(
                    records, query=found
                )
            except http.ToolError as error:
                return ToolResult(output=str(error), failed=True)

            text = _render_csv(molecules, columns=ANALOG_COLUMNS)
            snapshot = _AnalogSnapshot(
                molecules=molecules,
                total_matched=total,
                fetched=len(records),
                merged_away=merged_away,
                without_structure=without_structure,
                query_smiles=found.smiles,
                query_id=found.chembl_id,
                query_name=found.name,
                fingerprint=fingerprint_text(text),
            )
            try:
                _write_cache(
                    path,
                    text=text,
                    fingerprint=snapshot.fingerprint,
                    meta=_analog_meta(snapshot, threshold=threshold),
                )
            except OSError as error:
                return ToolResult(
                    output=(
                        f"写盘 {_relative(path)} 失败（{type(error).__name__}：{error}）。"
                        "数据已经在手上，但登记表里的条目要指出一个磁盘位置，"
                        "写不进去就不登记——请检查该目录是否可写"
                    ),
                    failed=True,
                )
            origin = f"已检索并写盘 {_relative(path)}（指纹 {snapshot.fingerprint}）"

        if snapshot.molecules:
            existing = ledger.find(
                kind="ligandset", fingerprint=snapshot.fingerprint, include_archived=True
            )
            if existing:
                registered = f"登记表里已有同一份：{existing[0].handle}（未重复登记）"
                handle = str(existing[0].handle)
            else:
                entry = ledger.add(
                    "ligandset",
                    path=path,
                    summary=_analog_entry_summary(found, snapshot, threshold),
                    fingerprint=snapshot.fingerprint,
                    meta=_analog_entry_meta(
                        query=found, snapshot=snapshot, threshold=threshold, path=path
                    ),
                )
                registered = f"已登记 {entry.handle}"
                handle = str(entry.handle)
        else:
            registered = "没有登记任何东西（可用分子为零）"
            handle = ""

        provenance = " · ".join(part for part in (origin, registered) if part)
        return ToolResult(
            output=_render_analog_digest(
                query=found,
                snapshot=snapshot,
                threshold=threshold,
                truncated=snapshot.fetched < snapshot.total_matched,
                provenance=provenance,
            ),
            meta={
                "query": _describe_query(found),
                "threshold": threshold,
                "molecule_count": len(snapshot.molecules),
                "handle": handle,
                "fingerprint": snapshot.fingerprint,
                "file": str(path),
            },
        )


class DescribeMoleculeTool(Tool):
    """查一个 ChEMBL 分子编号的画像。**纯查询，登记表里不留东西。**"""

    spec = ToolSpec(
        name="describe_molecule",
        description=(
            "查一个 ChEMBL 分子编号的**画像**：是不是已知药物、类药性如何、作用于哪些靶点、"
            "治什么病。用在「拿到一个命中分子，先搞清楚它是什么来头」这一步——"
            "**max_phase 到 4 就是已上市药物**（那这个「新发现」并不新），已撤市与黑框警告"
            "是比活性数值更该先看的信号，Lipinski 违反项与 QED 给出类药性快照，"
            "作用机制那一栏是脱靶风险的线索。它**只认 ChEMBL 分子编号**；对接产生的新结构在"
            " ChEMBL 里没有编号，那种情况用 find_analogs 按结构串查相似分子（顺带回答"
            "「这个结构是不是已知的」）。本工具**不在登记表里留条目**——它是一次查询，"
            "不是产物。每次调用联网现查，约 2–4 秒。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "molecule": {
                    "type": "string",
                    "description": (
                        "ChEMBL 分子编号，形如 CHEMBL939"
                        "（取自 search_actives / find_analogs 的输出）"
                    ),
                }
            },
            "required": ["molecule"],
        },
    )
    primary_argument = "molecule"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            code = normalize_molecule_id(str(arguments.get("molecule") or ""))
        except http.ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            record = _request_json(
                f"{MOLECULE_URL}/{code}.json", {"only": MOLECULE_ONLY}, timeout=TIMEOUT
            )
            if not isinstance(record, Mapping) or not record.get("molecule_chembl_id"):
                return ToolResult(
                    output=(
                        f"ChEMBL 里没有分子编号 {code}。"
                        "编号取自 search_actives / find_analogs 的输出，"
                        "不要凭印象写一个"
                    ),
                    failed=True,
                )
            mechanisms, targets = _mechanisms(code, timeout=TIMEOUT)
            indications, indication_total = _indications(code, timeout=TIMEOUT)
        except http.ToolError as error:
            return ToolResult(output=str(error), failed=True)

        return ToolResult(
            output=_render_molecule(
                record,
                mechanisms=mechanisms,
                targets=targets,
                indications=indications,
                indication_total=indication_total,
                # 说明这是现查的：同一个编号再问一次会重新联网，结果可能因远端更新而变
                origin="数据取自 ChEMBL 分子库（每次调用现查，不写盘缓存）",
            ),
            meta={
                "molecule_chembl_id": code,
                "pref_name": str(record.get("pref_name") or ""),
                "max_phase": _as_float(record.get("max_phase")),
                "mechanism_count": len(mechanisms),
                "indication_count": indication_total,
            },
        )


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具，按装配顺序。"""
    return [SearchActivesTool(), FindAnalogsTool(), DescribeMoleculeTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
