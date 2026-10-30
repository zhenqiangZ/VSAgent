"""Open Targets Platform：靶点的可成药性、化学探针、安全性与疾病关联。

前面几条线各回答一半：UniProt 说「这是什么蛋白」，ChEMBL 说「什么东西能结合它」，
PubChem 说「这段结构是什么」。Open Targets 回答的是**更靠前**的那个问题：
这个靶点值不值得做、做下去会撞上什么。

四样东西在这里汇合，而且都不是本地能推出来的：

1. **可成药性（tractability）** —— 按小分子 / 抗体 / PROTAC 三种模态分别给证据：
   有没有已批准药物、有没有带配体的实验结构、有没有高质量口袋、是不是可成药家族。
   「有高质量口袋」直接决定对接这一步值不值得做；「有已批准药物」说明这条路别人走通过。
2. **化学探针（chemical probes）** —— 对这个靶点效力够高、选择性经过验证的少数几个
   分子。用途是**阳性对照**：先把其中一个对接一遍，复现不出已知结合模式就说明受体准备、
   口袋或参数有问题，后面整批结果都不必看。它和「有已知活性」不是一回事——ChEMBL 里
   几千条 IC50 也不代表有探针，探针是额外经过选择性验证的那几个。
3. **安全性（safety liabilities）** —— 文献与数据库里报告过的、与该靶点调制相关的不良
   事件。它不回答「这个分子毒不毒」，只回答「动这个靶点，历史上出过什么事」。
4. **疾病关联** —— 靶点与疾病的双向关联分，带证据类型分解（临床 / 遗传 / 通路 / 文献 /
   体细胞突变）。**分解比总分有用**：0.8 分里如果全是文献，那是「研究得多」；
   有临床与遗传证据才说明「在人身上验证过」。

两个工具：`check_druggability`（靶点 → 上面四样）与 `disease_targets`（疾病 → 候选靶点）。

五条口径值得单独说明：

**分数不是概率。** Open Targets 的关联分是证据加权后的排序分，0–1 之间，没有
「多大概率致病」的含义；跨靶点比大小也没有意义（同一个 0.8，在不同靶点上由不同证据
凑出来）。输出里必须写清这一句，否则模型会拿 0.85 去和另一个靶点的 0.72 比「谁更相关」。

**可成药性是证据，不是结论。** 那些标志位来自结构、口袋预测、家族归属与已批准药物，
说「有」不等于「一定成」，说「没有」也不等于「一定不成」——它只说明**目前有没有先例**。
孤儿靶点上这条尤其重要：对接结果没有已知活性可以校准，这件事要在会话早期说出来。

**药物列表接口不分页。** `drugAndClinicalCandidates` 没有 `page` 参数，EGFR 一次回
82 条、9 KB。全部读进来只为印前几个是浪费，所以这里**按最高临床阶段本地排序**再截断
——接口给的顺序是任意的，不排序的话「前 5 个」是随便 5 个。

**一个 Ensembl 编号对应一个靶点，但用户嘴里说的是基因名。** 所以两个工具都接受
「基因名 / 疾病名」并自己解析成编号，解析结果打印出来供复用；直接把基因名塞进
`ensemblId` 参数时接口回的是 `null` 而不是报错，那种沉默的失败最难查。

**应答缓存一天，键是「查询 + 变量」。** 同一个靶点一天内重复查不再联网；但「列 5 条
疾病」与「列 20 条疾病」是两次不同的查询，各存一份——只按编号做键，第二次会拿到
第一次那份 5 条的。缓存与检索共用 `~/.vsagent/search/`（`rsearch` 那套 24h TTL，
**全局只有那一个定义**）。失败的应答（4xx / 5xx / `errors`）**不落缓存**：缓存是
「接口当时这么答的」这份档案，失败的应答不是档案。数据按月更新，一天的新鲜度足够。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import http
from .rsearch import cache_path, read_cache, write_cache

__all__ = [
    "BASE_URL",
    "DEFAULT_DISEASE_LIMIT",
    "DEFAULT_DRUG_LIMIT",
    "DEFAULT_TARGET_LIMIT",
    "DISEASE_PATTERN",
    "ENSEMBL_PATTERN",
    "CheckDruggabilityTool",
    "DiseaseTargetsTool",
    "OpenTargetsError",
    "disease_targets",
    "domain_tools",
    "search_entity",
    "target_profile",
]

BASE_URL = "https://api.platform.opentargets.org/api/v4/graphql"

ENSEMBL_PATTERN = re.compile(r"^ENSG\d{11}$", re.I)
"""Ensembl 基因编号：ENSG + 11 位数字。**大小写不敏感地认，但一律按大写发出去。**"""

DISEASE_PATTERN = re.compile(r"^(?:EFO|MONDO|ORPHANET|HP|DOID)_\d+$", re.I)
"""疾病编号：EFO_0000311 / MONDO_0005061 / Orphanet_558 / HP_0001250 / DOID_1612。"""

DEFAULT_TARGET_LIMIT = 5
MAX_TARGET_LIMIT = 15
DEFAULT_DISEASE_LIMIT = 5
MAX_DISEASE_LIMIT = 20
DEFAULT_DRUG_LIMIT = 5
MAX_DRUG_LIMIT = 20
DEFAULT_ASSOC_LIMIT = 10
MAX_ASSOC_LIMIT = 25
SAFETY_LIMIT = 6
PROBE_LIMIT = 5
FUNCTION_CHARS = 320

SEARCH_QUERY = """
query Search($q: String!, $entities: [String!]!, $size: Int!) {
  search(queryString: $q, entityNames: $entities, page: {index: 0, size: $size}) {
    total
    hits { id name entity description }
  }
}
"""

PROFILE_QUERY = """
query Profile($id: String!, $diseases: Int!) {
  target(ensemblId: $id) {
    id
    approvedSymbol
    approvedName
    biotype
    functionDescriptions
    genomicLocation { chromosome start end }
    tractability { label modality value }
    chemicalProbes {
      id drugId isHighQuality origin mechanismOfAction control
      scoreInCells scoreInOrganisms probesDrugsScore
    }
    safetyLiabilities { event datasource effects { direction } }
    associatedDiseases(page: {index: 0, size: $diseases}) {
      count
      rows { score disease { id name } datatypeScores { id score } }
    }
    drugAndClinicalCandidates {
      count
      rows { maxClinicalStage drug { id name drugType parentMolecule { id name } } }
    }
  }
}
"""

DISEASE_QUERY = """
query Disease($id: String!, $size: Int!) {
  disease(efoId: $id) {
    id
    name
    associatedTargets(page: {index: 0, size: $size}) {
      count
      rows { score target { id approvedSymbol approvedName } datatypeScores { id score } }
    }
  }
}
"""

MODALITIES = {"SM": "小分子", "AB": "抗体", "PR": "PROTAC/降解剂", "OC": "其他模态"}
"""tractability 的模态代码。顺序即输出顺序：先小分子——虚拟筛选做的就是这一路。"""

TRACTABILITY_LABELS = {
    "Approved Drug": "已批准药物",
    "Advanced Clinical": "已进高级临床",
    "Phase 1 Clinical": "已进临床 I 期",
    "Structure with Ligand": "有配体结构",
    "High-Quality Ligand": "高质量配体",
    "High-Quality Pocket": "高质量口袋",
    "Med-Quality Pocket": "中等质量口袋",
    "Druggable Family": "属可成药家族",
    "Literature": "文献支持",
    "UniProt Ubiquitination": "UniProt 泛素化位点",
    "Database Ubiquitination": "数据库泛素化位点",
    "Half-life Data": "有半衰期数据",
    "Small Molecule Binder": "有小分子结合剂",
    "UniProt loc high conf": "UniProt 定位（高置信）",
    "UniProt loc med conf": "UniProt 定位（中置信）",
    "UniProt SigP or TMHMM": "有信号肽/跨膜段",
    "GO CC high conf": "GO 细胞组分（高置信）",
    "GO CC med conf": "GO 细胞组分（中置信）",
    "Human Protein Atlas loc": "HPA 定位",
}

DATATYPE_LABELS = {
    "clinical": "临床",
    "literature": "文献",
    "genetic_association": "遗传关联",
    "genetic_literature": "遗传文献",
    "somatic_mutation": "体细胞突变",
    "affected_pathway": "通路",
    "known_drug": "已知药物",
    "rna_expression": "表达",
    "animal_model": "动物模型",
    "expression_atlas": "表达图谱",
}

PROBE_MECHANISMS = {
    "inhibitor": "抑制剂",
    "degrader": "降解剂",
    "agonist": "激动剂",
    "antagonist": "拮抗剂",
    "activator": "激活剂",
    "blocker": "阻断剂",
    "modulator": "调节剂",
}
"""化学探针的作用方式。**查不到就原样印英文**：探针名录里的取值比这张表长，
猜一个中文名比留英文更容易误导。"""

STAGE_LABELS = {
    "APPROVAL": "已批准",
    "PHASE_4": "临床 IV 期",
    "PHASE_3": "临床 III 期",
    "PHASE_2_3": "临床 II/III 期",
    "PHASE_2": "临床 II 期",
    "PHASE_1_2": "临床 I/II 期",
    "PHASE_1": "临床 I 期",
    "EARLY_PHASE_1": "临床 I 期（早期）",
    "PRECLINICAL": "临床前",
    "WITHDRAWAL": "已撤市",
    "UNKNOWN": "阶段未知",
}
STAGE_RANK = {
    "APPROVAL": 9,
    "PHASE_4": 8,
    "PHASE_3": 7,
    "PHASE_2_3": 6,
    "PHASE_2": 5,
    "PHASE_1_2": 4,
    "PHASE_1": 3,
    "EARLY_PHASE_1": 2,
    "PRECLINICAL": 1,
    "WITHDRAWAL": 0,
}
"""阶段排序权重。**只用于本地排序**：接口按任意顺序给，不排的话「前 5 个」是随便 5 个。"""

DIRECTION_LABELS = {
    "Inhibition/Decrease/Downregulation": "抑制/下调",
    "Activation/Increase/Upregulation": "激活/上调",
}

CITATION_PATTERN = re.compile(r"\s*\((?:PubMed|ECO):[^)]*\)")
EVIDENCE_PATTERN = re.compile(r"\s*\{ECO:[^}]*\}")
"""功能注释里的文献与证据编号。它们能把一整句描述挤到截断点之外，先剥掉再截。"""


class OpenTargetsError(ToolError):
    """Open Targets 的查询不可用（编号不存在、GraphQL 拒了这个查询）。**不重试到底。**"""


@dataclass
class Hit:
    """`search` 的一条命中。靶点与疾病共用这个形状。"""

    id: str
    name: str
    entity: str = ""
    description: str = ""


@dataclass
class Association:
    """一条「靶点 ↔ 疾病」关联。`datatypes` 是证据分解，按分数从高到低。"""

    id: str
    name: str
    score: float
    datatypes: tuple[tuple[str, float], ...] = ()


@dataclass
class Drug:
    """一个已进入临床的候选药。`stage` 保留接口原始值，渲染时再翻。"""

    name: str
    drug_id: str = ""
    kind: str = ""
    stage: str = ""


@dataclass
class Probe:
    """一个化学探针：对这个靶点效力够高、选择性经过验证的阳性对照。"""

    name: str
    drug_id: str = ""
    mechanism: str = ""
    quality: bool = False
    control: str = ""
    in_cells: float | None = None
    in_organisms: float | None = None
    score: float | None = None


@dataclass
class DiseaseProfile:
    """一个疾病的关联靶点清单。"""

    disease_id: str
    name: str
    targets: list[Association] = field(default_factory=list)
    total: int = 0
    note: str = ""


@dataclass
class Profile:
    """一个靶点的可成药性档案。"""

    ensembl: str
    symbol: str
    name: str = ""
    biotype: str = ""
    chromosome: str = ""
    start: int | None = None
    end: int | None = None
    function: str = ""
    tractability: dict[str, list[str]] = field(default_factory=dict)
    probes: list[Probe] = field(default_factory=list)
    probe_total: int = 0
    safety: list[tuple[str, str, str]] = field(default_factory=list)
    diseases: list[Association] = field(default_factory=list)
    disease_total: int = 0
    drugs: list[Drug] = field(default_factory=list)
    drug_total: int = 0
    drug_raw_total: int = 0


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
def _graphql(
    query: str,
    variables: dict[str, Any],
    *,
    timeout: float = http.DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> dict[str, Any]:
    """发一次 GraphQL 查询并取出 `data`（按查询 + 参数缓存一天）。

    GraphQL 有两层错误，**两层都要接**：HTTP 层的 4xx/5xx 由 `http.post_json`
    按码分档（4xx 是确定性失败、5xx 可重试）；HTTP 200 也可能带 `errors`
    （字段写错、参数类型不对），那是查询本身的问题，重试没有意义。

    **缓存键是「查询 + 变量」，不是「靶点」。** 同一个靶点用不同的 `diseases`
    条数是两次不同的查询，各自一份；只按编号做键会让「先看 5 条、再看 20 条」
    拿到同一份 5 条的缓存。`errors`、4xx、5xx **都不落缓存**——缓存是「接口当时
    这么答的」这份档案，失败的应答不是档案。
    """
    path = cache_path("opentargets", {"query": query, "variables": variables})
    if use_cache:
        cached = read_cache(path)
        if isinstance(cached, dict):
            return cached

    try:
        payload = http.post_json(
            BASE_URL, {"query": query, "variables": variables}, timeout=timeout
        )
    except http.RetryableToolError:
        # 瞬时故障原样交给内核重试，不在这里翻成永久失败。
        raise
    except http.ToolError as error:
        raise OpenTargetsError(f"Open Targets 查询失败：{error}") from error

    if not isinstance(payload, dict):
        raise OpenTargetsError("Open Targets 回的不是一个 JSON 对象")
    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        message = first.get("message") if isinstance(first, dict) else str(first)
        raise OpenTargetsError(f"Open Targets 拒了这个查询：{message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OpenTargetsError(f"Open Targets 的响应里没有 data：{str(payload)[:200]}")
    if use_cache:
        write_cache(path, data)
    return data


def search_entity(
    query: str,
    *,
    entity: str = "target",
    limit: int = DEFAULT_TARGET_LIMIT,
    timeout: float = http.DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> list[Hit]:
    """按名字搜靶点或疾病。**这是唯一能把基因名变成 Ensembl 编号的路。**"""
    text = (query or "").strip()
    if not text:
        raise OpenTargetsError("检索词不能为空")
    data = _graphql(
        SEARCH_QUERY,
        {"q": text, "entities": [entity], "size": max(1, limit)},
        timeout=timeout,
        use_cache=use_cache,
    )
    block = data.get("search") or {}
    hits = block.get("hits") if isinstance(block, dict) else None
    return [
        Hit(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or ""),
            entity=str(row.get("entity") or ""),
            description=str(row.get("description") or ""),
        )
        for row in hits or []
        if isinstance(row, dict) and row.get("id")
    ]


def _resolve(
    text: str,
    *,
    entity: str,
    pattern: re.Pattern[str],
    id_label: str,
    timeout: float,
    use_cache: bool = True,
) -> tuple[str, str]:
    """把「用户嘴里的名字」收敛成一个编号，返回 `(编号, 解析说明)`。

    已经是编号就直接用；否则搜一次，**精确同名优先**——`search` 会把
    「EGFR-AS1」「EPS8」这类名字里带 EGFR 的东西排在后面，但换个词就未必，
    取第一条等于把序列交给搜索排序。
    """
    raw = text.strip()
    if not raw:
        raise OpenTargetsError(f"{id_label}不能为空")
    if pattern.match(raw):
        return raw, ""
    hits = search_entity(
        raw, entity=entity, limit=DEFAULT_TARGET_LIMIT, timeout=timeout, use_cache=use_cache
    )
    if not hits:
        raise OpenTargetsError(
            f"Open Targets 里搜不到 {raw!r}。"
            + ("基因符号要写 HGNC 官方写法（EGFR / TP53），或直接给 Ensembl 编号"
               if entity == "target"
               else "疾病名要写英文（lung adenocarcinoma），或直接给 EFO / MONDO 编号")
        )
    exact = next((hit for hit in hits if hit.name.casefold() == raw.casefold()), None)
    chosen = exact or hits[0]
    if exact is not None:
        note = f"{raw} → {chosen.id}"
    else:
        note = (
            f"「{raw}」没有精确同名的{('靶点' if entity == 'target' else '疾病')}，"
            f"取最相近的一条：{chosen.name}（{chosen.id}）；不对的话直接给编号"
        )
    return chosen.id, note


def _function_text(descriptions: Any) -> str:
    """功能注释取第一段，剥掉文献编号与证据块再截断。"""
    if not isinstance(descriptions, list):
        return ""
    for item in descriptions:
        text = str(item or "").strip()
        if not text:
            continue
        text = EVIDENCE_PATTERN.sub("", CITATION_PATTERN.sub("", text))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > FUNCTION_CHARS:
            return text[:FUNCTION_CHARS].rstrip() + "…（截断）"
        return text
    return ""


def _tractability(rows: Any) -> dict[str, list[str]]:
    """把 tractability 摊成「模态 → 成立的条目」。**只留成立的**：25 项全列没人看。"""
    grouped: dict[str, list[str]] = {}
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("value"):
            continue
        modality = str(row.get("modality") or "")
        label = str(row.get("label") or "")
        grouped.setdefault(modality, []).append(
            TRACTABILITY_LABELS.get(label, label or "（未命名的证据）")
        )
    return grouped


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probes(rows: Any) -> list[Probe]:
    """化学探针：高质量优先，再按探针/药物评分从高到低。

    **`isHighQuality` 是这一栏唯一有门槛的字段**：名录里的条目未必都经过严格的效力与
    选择性验证，只有标了高质量的才适合直接拿去当阳性对照。所以它排在最前面，渲染时
    也标出来——模型据此判断「这个能不能当基准」，而不是把 12 条一视同仁。
    """
    items: list[Probe] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("id") or "").strip()
        if not name:
            continue
        mechanisms = [str(item) for item in row.get("mechanismOfAction") or [] if item]
        items.append(
            Probe(
                name=name,
                drug_id=str(row.get("drugId") or ""),
                mechanism="、".join(
                    PROBE_MECHANISMS.get(item.lower(), item) for item in mechanisms
                ),
                quality=bool(row.get("isHighQuality")),
                control=str(row.get("control") or ""),
                in_cells=_optional_float(row.get("scoreInCells")),
                in_organisms=_optional_float(row.get("scoreInOrganisms")),
                score=_optional_float(row.get("probesDrugsScore")),
            )
        )
    items.sort(key=lambda probe: (not probe.quality, -(probe.score or 0.0), probe.name))
    return items


def _safety(rows: Any) -> list[tuple[str, str, str]]:
    """不良事件：`(事件, 来源, 方向)`。方向可能是空的——ClinPGx 那几条就没有。"""
    items: list[tuple[str, str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or "").strip()
        if not event:
            continue
        source = str(row.get("datasource") or "").strip()
        directions = []
        for effect in row.get("effects") or []:
            if isinstance(effect, dict):
                raw = str(effect.get("direction") or "")
                if raw:
                    directions.append(DIRECTION_LABELS.get(raw, raw))
        items.append((event, source, "、".join(dict.fromkeys(directions))))
    return items


def _associations(block: Any) -> tuple[list[Association], int]:
    """疾病关联：`(按分数排序的前几条, 总数)`。"""
    if not isinstance(block, dict):
        return [], 0
    total = int(block.get("count") or 0)
    items: list[Association] = []
    for row in block.get("rows") or []:
        if not isinstance(row, dict):
            continue
        disease = row.get("disease") or {}
        datatypes = sorted(
            (
                (str(entry.get("id") or ""), float(entry.get("score") or 0.0))
                for entry in row.get("datatypeScores") or []
                if isinstance(entry, dict)
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        items.append(
            Association(
                id=str(disease.get("id") or ""),
                name=str(disease.get("name") or ""),
                score=float(row.get("score") or 0.0),
                datatypes=tuple(datatypes),
            )
        )
    items.sort(key=lambda item: item.score, reverse=True)
    return items, total


def _drugs(block: Any) -> tuple[list[Drug], int, int]:
    """临床候选药：`(按最高阶段排序并归并盐型后的清单, 归并后的个数, 接口原始条数)`。

    **盐型要并到母体上。** 接口按「分子」记，`AFATINIB` 与 `AFATINIB DIMALEATE`
    各算一条，EGFR 那 82 条里有一半是别人的盐——不归并的话「前 5 个」可能只是
    同一种药的三份记录。`parentMolecule` 只在盐型上非空，母体自身是 `None`。
    """
    if not isinstance(block, dict):
        return [], 0, 0
    raw_total = int(block.get("count") or 0)
    items: list[Drug] = []
    for row in block.get("rows") or []:
        if not isinstance(row, dict):
            continue
        drug = row.get("drug") or {}
        parent = drug.get("parentMolecule") or {}
        name = str(parent.get("name") or drug.get("name") or "").strip()
        if not name:
            continue
        items.append(
            Drug(
                name=name,
                drug_id=str(parent.get("id") or drug.get("id") or ""),
                kind=str(drug.get("drugType") or ""),
                stage=str(row.get("maxClinicalStage") or ""),
            )
        )
    items.sort(key=lambda item: STAGE_RANK.get(item.stage, -1), reverse=True)
    unique: list[Drug] = []
    seen: set[str] = set()
    for item in items:
        key = item.drug_id or item.name
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique, len(unique), raw_total


def target_profile(
    target: str,
    *,
    disease_limit: int = DEFAULT_DISEASE_LIMIT,
    drug_limit: int = DEFAULT_DRUG_LIMIT,
    timeout: float = http.DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> tuple[Profile, str]:
    """取一个靶点的可成药性档案。返回 `(档案, 解析说明)`。

    解析那一步（基因名 → 编号）也走缓存：同一个名字一天内只搜一次。
    """
    ensembl, note = _resolve(
        target,
        entity="target",
        pattern=ENSEMBL_PATTERN,
        id_label="target（基因名或 Ensembl 编号）",
        timeout=timeout,
        use_cache=use_cache,
    )
    data = _graphql(
        PROFILE_QUERY,
        {"id": ensembl.upper(), "diseases": max(1, disease_limit)},
        timeout=timeout,
        use_cache=use_cache,
    )
    block = data.get("target")
    if not isinstance(block, dict):
        raise OpenTargetsError(
            f"Open Targets 里没有 {ensembl.upper()} 这个靶点。"
            "编号要是 ENSG 加 11 位数字；基因名请交给 search 解析（这个工具已经做了，"
            "走到这里说明编号本身不存在）"
        )
    location = block.get("genomicLocation") or {}
    diseases, disease_total = _associations(block.get("associatedDiseases"))
    drugs, drug_total, drug_raw_total = _drugs(block.get("drugAndClinicalCandidates"))
    probes = _probes(block.get("chemicalProbes"))
    profile = Profile(
        ensembl=str(block.get("id") or ensembl.upper()),
        symbol=str(block.get("approvedSymbol") or ""),
        name=str(block.get("approvedName") or ""),
        biotype=str(block.get("biotype") or ""),
        chromosome=str(location.get("chromosome") or ""),
        start=location.get("start"),
        end=location.get("end"),
        function=_function_text(block.get("functionDescriptions")),
        tractability=_tractability(block.get("tractability")),
        probes=probes[:PROBE_LIMIT],
        probe_total=len(probes),
        safety=_safety(block.get("safetyLiabilities")),
        diseases=diseases,
        disease_total=disease_total,
        drugs=drugs[: max(1, drug_limit)],
        drug_total=drug_total,
        drug_raw_total=drug_raw_total,
    )
    return profile, note


def disease_targets(
    disease: str,
    *,
    limit: int = DEFAULT_ASSOC_LIMIT,
    timeout: float = http.DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> DiseaseProfile:
    """取一个疾病的关联靶点。

    与 `target_profile` 是镜像方向：那条从靶点出发问「和哪些病有关」，
    这条从疾病出发问「有哪些靶点」——找候选靶点时才用得上。
    """
    efo, note = _resolve(
        disease,
        entity="disease",
        pattern=DISEASE_PATTERN,
        id_label="disease（疾病名或 EFO/MONDO 编号）",
        timeout=timeout,
        use_cache=use_cache,
    )
    data = _graphql(
        DISEASE_QUERY,
        {"id": efo, "size": max(1, limit)},
        timeout=timeout,
        use_cache=use_cache,
    )
    block = data.get("disease")
    if not isinstance(block, dict):
        raise OpenTargetsError(
            f"Open Targets 里没有 {efo} 这个疾病。编号形如 EFO_0000311 / MONDO_0005061；"
            "疾病名请写英文"
        )
    rows = block.get("associatedTargets") or {}
    items: list[Association] = []
    for row in (rows.get("rows") if isinstance(rows, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        target = row.get("target") or {}
        datatypes = sorted(
            (
                (str(entry.get("id") or ""), float(entry.get("score") or 0.0))
                for entry in row.get("datatypeScores") or []
                if isinstance(entry, dict)
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        items.append(
            Association(
                id=str(target.get("id") or ""),
                name=str(target.get("approvedSymbol") or ""),
                score=float(row.get("score") or 0.0),
                datatypes=tuple(datatypes),
            )
        )
    items.sort(key=lambda item: item.score, reverse=True)
    return DiseaseProfile(
        disease_id=str(block.get("id") or efo),
        name=str(block.get("name") or ""),
        targets=items,
        total=int(rows.get("count") or 0) if isinstance(rows, dict) else 0,
        note=note,
    )


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _score(value: float) -> str:
    return f"{value:.2f}"


def _evidence(datatypes: tuple[tuple[str, float], ...], *, keep: int = 4) -> str:
    """证据分解：取分数最高的几路，并写明还有几路。"""
    if not datatypes:
        return "无证据分解"
    shown = [
        f"{DATATYPE_LABELS.get(name, name)} {_score(score)}" for name, score in datatypes[:keep]
    ]
    rest = len(datatypes) - len(shown)
    text = " / ".join(shown)
    return f"{text}（另有 {rest} 路）" if rest > 0 else text


def _render_profile(profile: Profile) -> list[str]:
    head = f"{profile.symbol or '（无符号）'}（{profile.ensembl}）"
    if profile.name:
        head += f" · {profile.name}"
    lines = [head]

    facts = []
    if profile.biotype:
        facts.append(f"类型 {profile.biotype}")
    if profile.chromosome:
        where = f"{profile.chromosome} 号染色体"
        if profile.start and profile.end:
            where += f" {profile.start:,}–{profile.end:,}"
        facts.append(f"位置 {where}")
    if facts:
        lines.append("  " + " · ".join(facts))
    if profile.function:
        lines.append(f"  功能：{profile.function}")

    lines.append("")
    lines.append("可成药性（Open Targets 的证据标志，按模态分；有=有先例，不是保证）：")
    if not profile.tractability:
        lines.append("  三种模态都没有阳性证据——这不等于不能做，只说明目前没有先例。")
    else:
        for code, label in MODALITIES.items():
            items = profile.tractability.get(code)
            if not items:
                continue
            lines.append(f"  {label}（{code}）：{'、'.join(items)}")
        others = [code for code in profile.tractability if code not in MODALITIES]
        for code in others:
            lines.append(f"  {code}：{'、'.join(profile.tractability[code])}")

    lines.append("")
    if profile.probes:
        lines.append(
            f"化学探针（经选择性验证的阳性对照，共 {profile.probe_total} 个，"
            f"高质量优先，列出前 {len(profile.probes)} 个）："
        )
        for probe in profile.probes:
            parts = [probe.name]
            if probe.mechanism:
                parts.append(probe.mechanism)
            if probe.quality:
                parts.append("高质量")
            if probe.drug_id:
                parts.append(probe.drug_id)
            scores = []
            if probe.in_cells is not None:
                scores.append(f"细胞 {probe.in_cells:g}")
            if probe.in_organisms is not None:
                scores.append(f"整体 {probe.in_organisms:g}")
            if scores:
                parts.append(" / ".join(scores))
            lines.append("  - " + " · ".join(parts))
            if probe.control:
                lines.append(f"      对照化合物：{probe.control}")
        rest = profile.probe_total - len(profile.probes)
        if rest > 0:
            lines.append(f"  （还有 {rest} 个）")
        lines.append(
            "  用途：先把其中一个按已知结合模式对接一遍——复现不出来，说明受体准备、"
            "口袋或参数有问题，下面这批分子的对接结果就没有基准可校。"
        )
    else:
        lines.append(
            "化学探针：没有收录。这个靶点没有经过验证的阳性对照，对接结果没有基准可校——"
            "只能拿已知活性分子（search_actives）当参照，或者只做相对排序、不判绝对好坏。"
        )

    lines.append("")
    if profile.safety:
        lines.append(
            f"安全性（{len(profile.safety)} 条记录，列出前 {min(SAFETY_LIMIT, len(profile.safety))} 条）："
        )
        for event, source, direction in profile.safety[:SAFETY_LIMIT]:
            tail = f"（{source}" + (f"，{direction}）" if direction else "）") if source else ""
            lines.append(f"  - {event}{tail}")
        rest = len(profile.safety) - SAFETY_LIMIT
        if rest > 0:
            lines.append(f"  （还有 {rest} 条）")
    else:
        lines.append("安全性：Open Targets 里没有该靶点的不良事件记录（不代表没有风险）。")

    lines.append("")
    if profile.drugs:
        salt_note = (
            f"，接口原始 {profile.drug_raw_total} 条（盐型已并入母体）"
            if profile.drug_raw_total > profile.drug_total
            else ""
        )
        lines.append(
            f"已知药物与临床候选（共 {profile.drug_total} 个{salt_note}，按最高临床阶段排序，"
            f"列出前 {len(profile.drugs)} 个）："
        )
        for drug in profile.drugs:
            parts = [drug.name]
            if drug.kind:
                parts.append(drug.kind)
            if drug.stage:
                parts.append(STAGE_LABELS.get(drug.stage, drug.stage))
            if drug.drug_id:
                parts.append(drug.drug_id)
            lines.append("  - " + " · ".join(parts))
        rest = profile.drug_total - len(profile.drugs)
        if rest > 0:
            lines.append(f"  （还有 {rest} 个）")
    else:
        lines.append("已知药物与临床候选：没有——这个靶点上还没有进入临床的分子。")

    lines.append("")
    if profile.diseases:
        lines.append(
            f"疾病关联（共 {profile.disease_total} 条，按关联分排序，"
            f"列出前 {len(profile.diseases)} 条）："
        )
        for item in profile.diseases:
            lines.append(
                f"  - {_score(item.score)} {item.name}（{item.id}）｜{_evidence(item.datatypes)}"
            )
        lines.append(
            "  **分数是证据加权后的排序分，不是概率，也不跨靶点比较大小**；"
            "要看这个关联靠什么撑着，看同一行里的证据分解。"
        )
    else:
        lines.append("疾病关联：没有记录。")

    lines.append("")
    lines.append(
        "  下一步：要实验结构 → fetch_structure / fetch_alphafold；"
        "要已知活性 → search_actives；要看某个病的其他靶点 → disease_targets。"
    )
    return lines


def _render_targets(profile: DiseaseProfile) -> list[str]:
    lines: list[str] = []
    if profile.note:
        lines.append(profile.note)
    head = f"{profile.name or '（未命名疾病）'}（{profile.disease_id}）：关联靶点共 {profile.total} 个"
    if profile.targets:
        head += f"，按关联分排序，列出前 {len(profile.targets)} 个："
    else:
        head += "。"
    lines.append(head)
    for index, item in enumerate(profile.targets, 1):
        lines.append(
            f"  [{index}] {_score(item.score)} {item.name}（{item.id}）｜{_evidence(item.datatypes)}"
        )
    if profile.total > len(profile.targets):
        lines.append(f"  （还有 {profile.total - len(profile.targets)} 个；调大 limit 再看）")
    if profile.targets:
        lines.append(
            "  **分数是证据加权后的排序分，不是概率，也不跨疾病比较大小**。"
            "要评估某个靶点能不能做，用 check_druggability。"
        )
    return lines


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class CheckDruggabilityTool(Tool):
    """靶点 → 可成药性 / 安全性 / 已知药物 / 疾病关联。纯查询。"""

    spec = ToolSpec(
        name="check_druggability",
        description=(
            "评估一个靶点**值不值得做**：可成药性（小分子 / 抗体 / PROTAC 三种模态各自的证据）、"
            "化学探针（可当阳性对照的那几个分子）、已报告的安全性风险、已进入临床的分子、"
            "以及关联疾病。输入基因名（EGFR）或 Ensembl 编号（ENSG00000146648）都行。\n\n"
            "用在两处：**挑靶点的时候**——「有高质量口袋」说明对接值得做，「已批准药物」说明"
            "这条路别人走通过，「有化学探针」说明有经过验证的阳性对照、对接结果能校准；"
            "这三样都没有的孤儿靶点，对接结果没有任何基准；"
            "**跑完对接之后**——看看这个靶点历史上出过什么安全性问题，比只看对接分数更能"
            "判断结果可不可信。\n\n"
            "**与 fetch_target 的分工**：那条取 UniProt 的蛋白注解（序列、结构域、可用实验"
            "结构），这条取「成药性与临床证据」。关联分是证据加权后的排序分，不是概率。"
            "探针那行带 ChEMBL 编号，要看它是不是已上市药，拿编号给 lookup_compound。"
            "一次调用约 1–2 秒；**同一天内重复查同一个靶点直接读本地缓存**（数据按月更新，"
            "不必担心看到旧的）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "基因符号（EGFR）、蛋白名或 Ensembl 编号（ENSG00000146648）",
                },
                "disease_limit": {
                    "type": "integer",
                    "description": f"列几条关联疾病，默认 {DEFAULT_DISEASE_LIMIT}，上限 {MAX_DISEASE_LIMIT}",
                },
                "drug_limit": {
                    "type": "integer",
                    "description": f"列几个已知药物/临床候选，默认 {DEFAULT_DRUG_LIMIT}，上限 {MAX_DRUG_LIMIT}",
                },
            },
            "required": ["target"],
        },
    )
    primary_argument = "target"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        target = str(arguments.get("target") or "").strip()
        if not target:
            return ToolResult(
                output="target 不能为空。填基因符号（EGFR）或 Ensembl 编号（ENSG00000146648）。",
                failed=True,
            )
        disease_limit = _clamp(arguments.get("disease_limit"), DEFAULT_DISEASE_LIMIT, MAX_DISEASE_LIMIT)
        drug_limit = _clamp(arguments.get("drug_limit"), DEFAULT_DRUG_LIMIT, MAX_DRUG_LIMIT)
        if disease_limit is None or drug_limit is None:
            return ToolResult(
                output="disease_limit / drug_limit 应当是整数", failed=True
            )
        try:
            profile, note = target_profile(
                target, disease_limit=disease_limit, drug_limit=drug_limit
            )
        except OpenTargetsError as error:
            return ToolResult(output=str(error), failed=True)

        lines = _render_profile(profile) if not note else [note] + _render_profile(profile)
        return ToolResult(
            output="\n".join(lines),
            meta={
                "ensembl_id": profile.ensembl,
                "symbol": profile.symbol,
                "name": profile.name,
                "biotype": profile.biotype,
                "tractability": profile.tractability,
                "probe_count": profile.probe_total,
                "probes": [
                    {"name": probe.name, "chembl_id": probe.drug_id, "quality": probe.quality}
                    for probe in profile.probes
                ],
                "safety_count": len(profile.safety),
                "disease_count": profile.disease_total,
                "drug_count": profile.drug_total,
                "drugs": [drug.name for drug in profile.drugs[:drug_limit]],
                "top_diseases": [
                    {"id": item.id, "name": item.name, "score": round(item.score, 4)}
                    for item in profile.diseases
                ],
                "source": "opentargets",
                "source_url": f"https://platform.opentargets.org/target/{profile.ensembl}",
            },
        )


class DiseaseTargetsTool(Tool):
    """疾病 → 关联靶点（与 check_druggability 镜像方向）。纯查询。"""

    spec = ToolSpec(
        name="disease_targets",
        description=(
            "从一个**疾病**出发找候选靶点：给疾病名（lung adenocarcinoma）或 EFO / MONDO 编号，"
            "回关联靶点及其证据分解（临床 / 遗传 / 通路 / 文献 / 体细胞突变）。\n\n"
            "用在「手上没有靶点、只有一个病」的时候：先看哪些靶点有临床或遗传证据，再挑一个"
            "去做结构准备与对接。**与 check_druggability 方向相反**——那条从靶点看疾病，"
            "这条从疾病看靶点；两者都是证据加权分，不是概率。\n\n"
            "分数高的不等于好做的：一个只有文献证据的高分靶点，可能既没有结构也没有口袋。"
            "挑中之后用 check_druggability 复核一遍可成药性。同一个疾病同一天内重复查直接"
            "读本地缓存。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "disease": {
                    "type": "string",
                    "description": "疾病名（英文，如 lung adenocarcinoma）或编号（MONDO_0005061）",
                },
                "limit": {
                    "type": "integer",
                    "description": f"列几个靶点，默认 {DEFAULT_ASSOC_LIMIT}，上限 {MAX_ASSOC_LIMIT}",
                },
            },
            "required": ["disease"],
        },
    )
    primary_argument = "disease"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        disease = str(arguments.get("disease") or "").strip()
        if not disease:
            return ToolResult(
                output="disease 不能为空。填英文疾病名（lung adenocarcinoma）或 MONDO/EFO 编号。",
                failed=True,
            )
        limit = _clamp(arguments.get("limit"), DEFAULT_ASSOC_LIMIT, MAX_ASSOC_LIMIT)
        if limit is None:
            return ToolResult(output="limit 应当是整数", failed=True)
        try:
            profile = disease_targets(disease, limit=limit)
        except OpenTargetsError as error:
            return ToolResult(output=str(error), failed=True)

        lines = _render_targets(profile)
        return ToolResult(
            output="\n".join(lines),
            meta={
                "disease": profile.name,
                "disease_id": profile.disease_id,
                "count": len(profile.targets),
                "total": profile.total,
                "targets": [
                    {
                        "ensembl_id": item.id,
                        "symbol": item.name,
                        "score": round(item.score, 4),
                    }
                    for item in profile.targets
                ],
                "source": "opentargets",
            },
        )


def _clamp(value: Any, default: int, maximum: int) -> int | None:
    """把 limit 类参数夹到 `[1, maximum]`。**超上限不报错**（夹住就行），非整数返回 None。"""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(number, maximum))


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [CheckDruggabilityTool(), DiseaseTargetsTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
