"""搜索工具（一）：RCSB 文本搜 / 属性搜 + GraphQL 批量取配体。

**这一步给的是候选，不是结论。** 返回条目 id 与每个条目的配体代号与名称，
**不做任何归类**：不判「这个配体算不算真配体」、不判「这是不是结合位点」。
那些判断要过 `export.py` 的口径（排除水 / 修饰残基 / 单原子离子 / 常见缓冲液），
是 `write_run` 的活（方案稿 §2.3.11）。

**搜索结果不写进 `meta.json`。** 只缓存到 `~/.vsagent/search/`（时效一天）；
要进页面仍走 `write_run` 的口径。

三条纪律：

1. **文本搜与属性搜分工明确**：文本搜是「按猜测找」，结果随输入变；属性搜是「按条件筛」，
   结果可复现。所以「按 UniProt 列结构」走属性搜，其余走文本搜——内部自己决定。
2. **失败返回结果，不抛异常**：网络不可达 / 4xx / 5xx 都回一句「这次没搜成」并带原因。
   **命中 0 条不算失败**，那是「查了、确实没有」——正文措辞与 `meta.reason` 都要分开写。
   混成一句的话，模型会当成工具出错，换个说法反复重试。
3. **只解出需要的字段**：多解一项，就多一种把响应形状的偶然当成约定的可能。
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools import RetryableToolError, Tool, ToolError, ToolResult, ToolSpec
from . import http

__all__ = [
    "ChemComp",
    "DEFAULT_LIMIT",
    "GRAPHQL_BATCH",
    "MAX_LIMIT",
    "PdbHit",
    "PolymerChain",
    "RCSB_CHEMCOMP_URL",
    "RCSB_GRAPHQL_URL",
    "RCSB_SEARCH_URL",
    "SearchError",
    "SearchPdbTool",
    "attribute_query",
    "cache_path",
    "chemcomp_record",
    "domain_tools",
    "fetch_ligands",
    "ligand_display_names",
    "ligand_synonym_map",
    "ligand_synonyms",
    "read_cache",
    "search_dir",
    "search_entries",
    "text_query",
    "write_cache",
]

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"
RCSB_CHEMCOMP_URL = "https://data.rcsb.org/rest/v1/core/chemcomp/{code}"

SEARCH_CACHE_TTL = 24 * 3600
"""搜索结果缓存时效（秒）。一天——新结构发布是低频事件，一天足够新鲜。

**只有这一处定义**：`complexes.py` 原先自己写了一份一模一样的值，合并缓存时
一并收掉。改这个数就是改全局，别再往别处抄第二个。
"""

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
"""一次最多列多少条。再多就该把检索式写细，而不是翻页。"""

GRAPHQL_BATCH = 50
"""一次 GraphQL 问多少个条目的配体。太大响应会很长，太小又多跑几趟。"""


class SearchError(ToolError):
    """这一侧的接口不可用或返回的形状不对。**当「这次没搜成」，不重试到底。**"""


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------
@dataclass
class ChemComp:
    """一个非聚合物组分（`chem_comp`）。

    **`name` 是首选名，实测常常是一长串系统命名**：`9SR` 的 `name` 是
    「(1R,5R,6R,7R,9S,11S,12S,13S,14S)-3-amino-14-(hydroxymethyl)-…-pentol」，
    「Tetrodotoxin」在另一处（`ligand_synonyms`）。只显示首选名，读的人认不出
    这是河豚毒素——`ses_0014` 就是把 9SR 当成一个陌生代号放过去的。
    """

    code: str
    name: str = ""
    kind: str = ""
    """`chem_comp.type`：`non-polymer` / `D-saccharide, beta linking` / …"""
    weight: float | None = None


@dataclass
class PolymerChain:
    """一条聚合物实体。`uniprot_ids` 为空时是合成肽或没有映射到 UniProt 的链。"""

    entity_id: str
    description: str = ""
    uniprot_ids: tuple[str, ...] = ()


@dataclass
class PdbHit:
    """一条命中的结构。字段全部来自 RCSB，**没有一处是我们猜的**。"""

    pdb_id: str
    title: str = ""
    resolution: float | None = None
    method: str = ""
    ligands: tuple[str, ...] = ()
    """配体代号（`chem_comp.id`），**原样列出、不做筛选**。"""

    ligand_names: tuple[tuple[str, str], ...] = ()
    """`(代号, 名称)`，同样原样。"""

    nonpolymer_count: int = 0

    comps: tuple[ChemComp, ...] = ()
    """非聚合物组分的完整几项（代号、名称、类型、分子量）。"""

    polymers: tuple[PolymerChain, ...] = ()
    """聚合物实体。有了它才能把「只有本靶点」与「还带一条别的链」分开——
    肽类毒素、抗体片段、辅助亚基都是聚合物实体，不会出现在 `nonpolymer_entities` 里。"""

    def as_dict(self) -> dict[str, Any]:
        """给 `ToolResult.meta` 用的结构。**不含任何归类字段**。"""
        return {
            "pdb_id": self.pdb_id,
            "title": self.title,
            "resolution": self.resolution,
            "method": self.method,
            "ligands": list(self.ligands),
            "ligand_names": [list(item) for item in self.ligand_names],
            "nonpolymer_count": self.nonpolymer_count,
        }


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def search_dir() -> Path:
    """**全仓唯一**的检索结果缓存目录（§7.11）。与状态文件、结构缓存同处一个家目录。

    用它的有四个模块：`rsearch`（RCSB 属性搜 / 文本搜 / 批量配体）、`complexes`
    （P0 复合物检索）、`literature`（PubMed）、`websearch`（联网摘要）。原先
    `complexes` 自带一份 `~/.vsagent/rcsb/search/`，两处写的逻辑一样、
    目录不同——同一个 Uniprot 连问两遍也互相看不见。合并之后只认这一处，
    键由 `cache_path(kind, payload)` 算，**不同模块的同类请求算出来的键必须一致**。
    """
    return Path.home() / ".vsagent" / "search"


def cache_path(kind: str, payload: Any) -> Path:
    blob = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return search_dir() / f"{kind}-{digest}.json"


def read_cache(path: Path, *, ttl: float | None = SEARCH_CACHE_TTL) -> Any | None:
    """读缓存。**过期或损坏都当没有**——损坏的缓存不能变成一次假结果。

    `ttl=None` 表示**永不过期**。联网搜索走的就是这一档：查过的 query 与服务
    当时返回的结果，是一份档案而不是加速用的缓存（方案稿 §4.4），过期后重查
    要再付一次钱，而旧结果本身还有「当时是这么返回的」这个用途。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or "saved_at" not in payload:
        return None
    if ttl is not None and time.time() - float(payload["saved_at"]) > ttl:
        return None
    return payload.get("body")


def write_cache(path: Path, body: Any) -> None:
    """写缓存。**失败不影响主流程**——缓存是加速用的，不是真相。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"saved_at": time.time(), "body": body}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 查询体
# ---------------------------------------------------------------------------
def text_query(text: str, *, rows: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """RCSB **文本搜**的查询体（`service: full_text`）。

    文本搜是「按猜测找」：输入换个说法，结果就变。所以它**不进 `write_run`**，
    只当模型自己的探索入口（§2.3.11）。
    """
    return {
        "query": {
            "type": "terminal",
            "service": "full_text",
            "parameters": {"value": text},
        },
        "return_type": "entry",
        "request_options": {
            "results_content_type": ["experimental"],
            "paginate": {"start": 0, "rows": rows},
        },
    }


_ATTRIBUTES = {
    "uniprot": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
    "ligand": "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
    "method": "exptl.method",
}
"""属性搜认的三个条件各自对应的 RCSB 字段名。

配体那一项**不能**写成 `...container_identifiers.comp_id`：那个名字看着更直白，
但 RCSB 明确拒绝按它检索——HTTP 400，`search is not enabled on [ ...comp_id ] attribute`，
一用整条查询就作废。可搜的名字是 `nonpolymer_comp_id`，`exact_match` 配 `EPJ`
实测命中 15 条（现场：`ses_0037` 第 6 步拿 `ligand=NCT` 搜，撞的就是这个 400）。
改这里之前先拿真实接口打一次，别照着名字猜。
"""


def attribute_query(
    *,
    uniprot: str | None = None,
    ligand: str | None = None,
    method: str | None = None,
    rows: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """RCSB **属性搜**的查询体（`service: text` + `attribute`）。

    属性搜是「按条件筛」：同一条件跑两遍，条目集合完全一致（这是一条判据）。
    P0（`complexes.py`）用的就是这一条路。
    """
    nodes: list[dict[str, Any]] = []
    for key, value in (("uniprot", uniprot), ("ligand", ligand), ("method", method)):
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        nodes.append(
            {
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": _ATTRIBUTES[key],
                    "operator": "exact_match",
                    "value": cleaned,
                },
            }
        )
    if not nodes:
        raise SearchError("属性搜至少要给一个条件：uniprot / ligand / method 之一")
    return {
        "query": {"type": "group", "logical_operator": "and", "nodes": nodes},
        "return_type": "entry",
        "request_options": {
            "results_content_type": ["experimental"],
            "paginate": {"start": 0, "rows": rows},
        },
    }


# ---------------------------------------------------------------------------
# 一、搜
# ---------------------------------------------------------------------------
def search_entries(
    query: dict[str, Any],
    *,
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> tuple[list[str], int, dict[str, Any]]:
    """跑一次检索。返回 `(条目 id 列表, 命中总数, 原始响应)`。

    **命中总数与 id 列表一起给**：只报列出来的那几条，用户会以为一共就这么多
    （`complexes.py` 的 `total` / `used` 是同一条道理）。

    **`complexes.list_complexes` 走的是同一条路**：它只负责造自己的查询体，
    请求、缓存、错误分档都在这里。零命中也进缓存（204 归一成空响应），
    所以「查了确实没有」这个结论一天内不会反复打接口。
    """
    path = cache_path("search", query)
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return _ids_from(cached), _total_from(cached), cached

    try:
        body = http.post_json(RCSB_SEARCH_URL, query, timeout=timeout)
    except http.EmptyResult:
        # **零命中不是失败。** RCSB 在零命中时回 204、不带响应体，`http` 那一层
        # 已经为这个场景单独造了 `EmptyResult`。这里原先用 `except Exception`
        # 把它一起吞了，于是「查了、确实没有」被写成「这次没搜成」——模型读到之后
        # 以为工具坏了，换个说法反复重试（现场：`ses_0037` 第 5~9 步连试四种说法，
        # 步数就是这么烧掉的）。当成空结果往下走，正文与 `meta.reason` 会分开说。
        #
        # **归一成带 `result_set` 的空响应再进缓存**：204 本身没有响应体，
        # 不写一份的话同一个「确实没有」每次都要再打一次接口。缓存里放的是
        # 一份形状与真实响应一致的记录（`_ids_from` / `_total_from` 都吃得下），
        # 而不是那个 `{}`——读回来的人和写下去的人看到的是同一种东西。
        body = {"result_set": [], "total_count": 0}
    except RetryableToolError:
        # **瞬时故障不在这里变成永久失败。** `http` 已经按「重试还有没有意义」
        # 分过档（429 / 5xx / 连不上是可重试），再包成 `SearchError` 就等于
        # 告诉循环层「别重试了」——而循环层是唯一会重试的那一层。
        raise
    except Exception as error:  # 其余（响应形状不对、4xx）归为「这次没搜成」
        raise SearchError(f"RCSB 检索没成：{error}") from error

    if use_cache:
        write_cache(path, body)
    return _ids_from(body), _total_from(body), body


def _ids_from(body: Any) -> list[str]:
    """**只认 `result_set[].identifier`**，不顺手解别的字段。"""
    if not isinstance(body, dict):
        return []
    result_set = body.get("result_set")
    if not isinstance(result_set, list):
        return []
    return [
        item["identifier"]
        for item in result_set
        if isinstance(item, dict) and isinstance(item.get("identifier"), str)
    ]


def _total_from(body: Any) -> int:
    if not isinstance(body, dict):
        return 0
    total = body.get("total_count")
    return int(total) if isinstance(total, (int, float)) else 0


# ---------------------------------------------------------------------------
# 二、批量取配体（GraphQL）
# ---------------------------------------------------------------------------
_GRAPHQL_QUERY = (
    "query($ids:[String!]!){"
    "entries(entry_ids:$ids){"
    "rcsb_id "
    "struct{title} "
    "rcsb_entry_info{nonpolymer_entity_count experimental_method resolution_combined} "
    "polymer_entities{rcsb_id rcsb_polymer_entity{pdbx_description} "
    "rcsb_polymer_entity_container_identifiers{uniprot_ids}} "
    "nonpolymer_entities{nonpolymer_comp{chem_comp{id name type formula_weight}}}"
    "}}"
)

_GRAPHQL_CACHE_VERSION = 2
"""缓存键里的版本号。**改了 `_GRAPHQL_QUERY` 就要加一**：缓存键只认条目 id，
不认查询体，旧响应里没有新字段，读回来会是「这一条没有聚合物实体」而不是报错。"""


def fetch_ligands(
    pdb_ids: list[str],
    *,
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> dict[str, PdbHit]:
    """一次问多个条目的配体代号与名称（GraphQL）。

    分批问（`GRAPHQL_BATCH` 个一批），**某一批失败就跳过那一批**——
    整批报失败会逼调用方重试，而重试救不回网络抖动的那一下。
    """
    wanted = [pid.strip().upper() for pid in pdb_ids if (pid or "").strip()]
    if not wanted:
        return {}

    out: dict[str, PdbHit] = {}
    for start in range(0, len(wanted), GRAPHQL_BATCH):
        chunk = wanted[start : start + GRAPHQL_BATCH]
        payload = {"query": _GRAPHQL_QUERY, "variables": {"ids": chunk}}
        path = cache_path("ligands", {"ids": chunk, "v": _GRAPHQL_CACHE_VERSION})
        body = read_cache(path) if use_cache else None
        if body is None:
            try:
                body = http.post_json(RCSB_GRAPHQL_URL, payload, timeout=timeout)
            except RetryableToolError:
                # 同上：可重试的交给循环层，别在这里判死。
                raise
            except Exception as error:
                raise SearchError(f"RCSB 配体查询没成：{error}") from error
            if use_cache:
                write_cache(path, body)
        for hit in _hits_from(body):
            out[hit.pdb_id] = hit
    return out


def _hits_from(body: Any) -> list[PdbHit]:
    """从 GraphQL 响应里解出条目。**只解需要的几项**。"""
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []

    hits: list[PdbHit] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        pdb_id = str(item.get("rcsb_id") or "").strip().upper()
        if not pdb_id:
            continue
        info = item.get("rcsb_entry_info")
        info = info if isinstance(info, dict) else {}
        structure = item.get("struct")
        structure = structure if isinstance(structure, dict) else {}

        codes: list[str] = []
        names: list[tuple[str, str]] = []
        comps: list[ChemComp] = []
        seen: set[str] = set()
        entities = item.get("nonpolymer_entities")
        if isinstance(entities, list):
            for entity in entities:
                comp = _comp_of(entity)
                code = str(comp.get("id") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                name = str(comp.get("name") or "").strip()
                codes.append(code)
                names.append((code, name))
                comps.append(
                    ChemComp(
                        code=code,
                        name=name,
                        kind=str(comp.get("type") or "").strip(),
                        weight=_as_float(comp.get("formula_weight")),
                    )
                )

        hits.append(
            PdbHit(
                pdb_id=pdb_id,
                title=str(structure.get("title") or "").strip(),
                resolution=_as_float(info.get("resolution_combined")),
                method=str(info.get("experimental_method") or "").strip(),
                ligands=tuple(codes),
                ligand_names=tuple(names),
                nonpolymer_count=int(info.get("nonpolymer_entity_count") or 0),
                comps=tuple(comps),
                polymers=_polymers_of(item),
            )
        )
    return hits


def _polymers_of(item: dict[str, Any]) -> tuple[PolymerChain, ...]:
    """解出聚合物实体。**缺字段就当没有**：老缓存与打桩响应里都没有这一段。"""
    entities = item.get("polymer_entities")
    if not isinstance(entities, list):
        return ()
    chains: list[PolymerChain] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        poly = entity.get("rcsb_polymer_entity")
        poly = poly if isinstance(poly, dict) else {}
        ident = entity.get("rcsb_polymer_entity_container_identifiers")
        ident = ident if isinstance(ident, dict) else {}
        raw_ids = ident.get("uniprot_ids")
        chains.append(
            PolymerChain(
                entity_id=str(entity.get("rcsb_id") or ""),
                description=str(poly.get("pdbx_description") or "").strip(),
                uniprot_ids=(
                    tuple(str(value) for value in raw_ids)
                    if isinstance(raw_ids, list)
                    else ()
                ),
            )
        )
    return tuple(chains)


def _comp_of(entity: Any) -> dict[str, Any]:
    if not isinstance(entity, dict):
        return {}
    comp = entity.get("nonpolymer_comp")
    comp = comp if isinstance(comp, dict) else {}
    chem = comp.get("chem_comp")
    return chem if isinstance(chem, dict) else {}


def _as_float(value: Any) -> float | None:
    """取一个浮点数。RCSB 的 `resolution_combined` 是**数组**（如 `[1.9]`），所以先取第一项。"""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 三、配体代号的常用名
# ---------------------------------------------------------------------------
_CHEMCOMP_CACHE_VERSION = 1

_PREFERRED_NAME_SUFFIX = " (non-preferred name)"

_SYSTEMATIC_MARKS = ("(", ")", "[", "]", "~")


def _looks_systematic(name: str) -> bool:
    """名字像不像系统命名。**只用于排序，不用于判断对错**。

    实测 RCSB 的 `type` 字段不可靠：`TB4` 的第一条 `Synonym` 就是
    「(2~{S},5~{R})-5-[4-[(2-fluorophenyl)methoxy]phenyl]pyrrolidine-2-carboxamide」，
    而「Vixotrigine」排在后面。只按 type 排序，显示出来的仍然是一串系统命名。
    """
    return len(name) > 40 or any(mark in name for mark in _SYSTEMATIC_MARKS)


def chemcomp_record(
    code: str,
    *,
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """化学组分字典里的一条。**不存在时抛 `http.NotFound`，其余错误照常抛**。

    三态是必要的：`None` 表示「取到了但形状不对」，`http.NotFound` 表示
    「这个代号确实没有登记」，其他异常表示「这次没查成」。零命中时要把
    「TTX 不是代号」与「这次没连上」分开写，混成一句就会让模型继续换说法重试。
    """
    cleaned = str(code or "").strip().upper()
    if not cleaned:
        return None
    path = cache_path("chemcomp", {"code": cleaned, "v": _CHEMCOMP_CACHE_VERSION})
    body = read_cache(path) if use_cache else None
    if body is None:
        body = http.get_json(RCSB_CHEMCOMP_URL.format(code=cleaned), timeout=timeout)
        if use_cache:
            write_cache(path, body)
    return body if isinstance(body, dict) else None


def ligand_synonyms(
    code: str,
    *,
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> list[str]:
    """一个配体代号的常用名清单，**短名在前、系统命名在后**。

    `chem_comp.name` 给的是首选名，实测常常是一长串系统命名（`9SR` 就是
    「(1R,5R,…)-3-amino-…-pentol」）。「Tetrodotoxin」这一档在
    `rcsb_chem_comp_synonyms` 里，只有 REST 接口给，GraphQL 的 `ChemComp`
    类型没有这个字段（字段清单实测过）。

    **取不到时返回空表**：这是显示用的补充，不是判据。调用方拿不到就退回首选名，
    并把「有几个代号没有短名」写进返回——悄悄退回的话，读的人会把系统命名
    当成这个配体只有这一个名字。
    """
    try:
        body = chemcomp_record(code, use_cache=use_cache, timeout=timeout)
    except Exception:
        return []

    rows = body.get("rcsb_chem_comp_synonyms") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    candidates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "") not in ("Synonym", "Preferred Name"):
            continue
        name = str(row.get("name") or "").strip()
        if name.endswith(_PREFERRED_NAME_SUFFIX):
            name = name[: -len(_PREFERRED_NAME_SUFFIX)].strip()
        if name and name not in candidates:
            candidates.append(name)
    # 稳定排序：同类里保持 RCSB 给的先后。
    candidates.sort(key=lambda name: (_looks_systematic(name), len(name)))
    return candidates


def ligand_synonym_map(
    codes: list[str] | tuple[str, ...],
    *,
    use_cache: bool = True,
) -> tuple[dict[str, list[str]], list[str]]:
    """一批代号的全部常用名。返回 `(代号 → 名字清单, 没取到的代号)`。

    顺序按传入的先后（去重后），这样同一份输入两次调用得到同一份清单。
    **并发查**：EGFR 一个靶点就有上百个不同代号，一个个问要十几秒；
    每个代号一个缓存文件，互不相干，八个一起发不会互相踩。
    """
    unique = [
        code
        for code in dict.fromkeys(str(item or "").strip().upper() for item in codes)
        if code
    ]
    if not unique:
        return {}, []

    def lookup(code: str) -> list[str]:
        return ligand_synonyms(code, use_cache=use_cache)

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(lookup, unique))

    names: dict[str, list[str]] = {}
    missed: list[str] = []
    for code, items in zip(unique, found):
        if items:
            names[code] = items
        else:
            missed.append(code)
    return names, missed


def ligand_display_names(
    codes: list[str] | tuple[str, ...],
    *,
    use_cache: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """一批代号的显示名（每组取第一个常用名）。"""
    names, missed = ligand_synonym_map(codes, use_cache=use_cache)
    return {code: found[0] for code, found in names.items()}, missed


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class SearchPdbTool(Tool):
    """检索 PDB 条目。纯查询，不产生产物，所以不收登记表。"""

    spec = ToolSpec(
        name="search_pdb",
        description=(
            "检索 PDB 结构，返回**候选清单**：条目 id、标题、分辨率、实验方法，"
            "以及每个条目里出现的配体代号与名称（来自 RCSB，原样列出）。"
            "同时报出**命中总数**——只列前若干条时，别以为一共就这么多。\n\n"
            "**这是候选，不是结论。** 本工具不筛配体、不判结合位点、不判哪个配体算「真配体」"
            "——那要过项目的口径（排掉水、修饰残基、单原子离子、常见缓冲液），由 write_run 做。\n\n"
            "两种检索方式，内部自己选：给 `uniprot` 走**属性搜**（按条件筛，结果可复现）；"
            "只给 `query` 走**文本搜**（按关键词找，结果随说法变）。"
            "「某个蛋白有哪些结构」优先用 uniprot，别用文本搜猜。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "文本检索词，例如 Nav1.7 ProTx-II。"
                        "与 uniprot 同时给出时，以 uniprot（属性搜）为准"
                    ),
                },
                "uniprot": {
                    "type": "string",
                    "description": "UniProt 编号（如 Q15858）。给出它就走属性搜，按该蛋白列结构",
                },
                "ligand": {
                    "type": "string",
                    "description": (
                        "配体**代号**（chem_comp id，三位字母数字，如 STI、9SR）。给出它则按"
                        "「含该配体」筛。名称（如 tetrodotoxin）要先换成代号，"
                        "`survey_structures` 会给出这个靶点的代号与常用名"
                    ),
                },
                "method": {
                    "type": "string",
                    "description": "实验方法（如 X-RAY DIFFRACTION）。给出它则按方法筛",
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多列多少条，上限 {MAX_LIMIT}；命中更多时会在末尾写明",
                },
            },
        },
    )
    primary_argument = "query"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        query_text = str(arguments.get("query") or "").strip()
        uniprot = str(arguments.get("uniprot") or "").strip()
        ligand = str(arguments.get("ligand") or "").strip()
        method = str(arguments.get("method") or "").strip()

        try:
            requested = int(arguments.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult(output="limit 应当是整数", failed=True)
        size = max(1, min(requested, MAX_LIMIT))

        if not any((query_text, uniprot, ligand, method)):
            return ToolResult(
                output=(
                    "至少要给一个条件：uniprot（按蛋白列结构）、ligand / method（按属性筛），"
                    "或 query（文本检索）。"
                ),
                failed=True,
            )

        # ---- 选路：属性条件优先（可复现），否则文本搜 ----
        if uniprot or ligand or method:
            how = "属性搜"
            try:
                body = attribute_query(
                    uniprot=uniprot, ligand=ligand, method=method, rows=size
                )
            except SearchError as error:
                return ToolResult(output=str(error), failed=True)
            condition = _describe_condition(uniprot=uniprot, ligand=ligand, method=method)
        else:
            how = "文本搜"
            body = text_query(query_text, rows=size)
            condition = f"文本「{query_text}」"

        try:
            ids, total, _raw = search_entries(body)
        except SearchError as error:
            # 网络不可达 / 4xx / 5xx 都走这里：返回一句可读的失败，**不抛异常、不重试到底**。
            return ToolResult(
                output=f"这次没搜成（{how} {condition}）：{error}",
                failed=True,
                meta=_empty_meta("failed", total=0),
            )

        if not ids:
            # **命中 0 条不是失败。** 这里原先也写「这次没搜成」，模型读到之后以为工具
            # 出了问题，换条件反复重试——现场 `ses_0037` 第 5~9 步连着试了四种说法，
            # 步数就是这么烧掉的。它与「没查成」对用户的含义相反：
            # 一个是「这个条件下确实没有」，一个是「这次没拿到答案」。
            #
            # **按配体查时补一句口径。** `ses_0014` 拿 `ligand=TTX` 查了一次 0 条，
            # 就把「TTX 与 STX 共晶各 0 条」写进结论，整条路线建在「没有参考构象」上；
            # 而 TTX 的代号是 9SR，同一个靶点下有 6 套。零命中在按代号查时几乎总是
            # 「这个字符串不是代号」，不写清楚就会被读成「这个复合物不存在」。
            hint = ""
            if ligand:
                hint = (
                    "\n`ligand` 只认配体代号（`chem_comp` id，三位字母数字，例如 9SR）。"
                    "名称要先换成代号：`survey_structures` 会列出这个靶点出现过的"
                    "代号与常用名。"
                )
                if len(ligand) != 3:
                    hint += (
                        f"\n「{ligand}」有 {len(ligand)} 个字符，形状上就不是配体代号——"
                        "这个 0 条说明的是「没有这个代号」，不是「没有这个复合物」。"
                    )
                else:
                    # 三位字符也可能是名字：`TTX` 是河豚毒素的通用缩写，代号是 `9SR`；
                    # 而 `TTX` 本身是登记过的——那是 TENTOXIN，另一种化合物。查一次
                    # 化学组分字典，把「不是代号」「是代号但指别的东西」「这个靶点里没有」
                    # 三种情况分开写，否则「TTX 0 条」会被读成「没有河豚毒素复合物」。
                    try:
                        record = chemcomp_record(ligand)
                    except http.NotFound:
                        record = None
                    except Exception:
                        record = {}
                    comp = record.get("chem_comp") if isinstance(record, dict) else None
                    comp = comp if isinstance(comp, dict) else {}
                    if record is not None and not comp:
                        hint += (
                            f"\n「{ligand}」在 RCSB 化学组分字典里没有登记，它不是配体代号。"
                            "这个 0 条说明的是「没有这个代号」，不是「没有这个复合物」。"
                        )
                    elif comp:
                        name = str(comp.get("name") or "").strip()
                        weight = comp.get("formula_weight")
                        detail = f"：{name}" if name else ""
                        if isinstance(weight, (int, float)):
                            detail += f"（{float(weight):.1f} Da）"
                        hint += (
                            f"\n{ligand} 是登记过的代号{detail}，这个靶点里没有它。"
                            "如果刚才那个是化合物的名称，代号要去 `survey_structures` "
                            "的结果里找——缩写常常对不上。"
                        )
            return ToolResult(
                output=(
                    f"{how} {condition}：查了，确实没有（命中 0 条）。"
                    "（这是「查了没有」，不是「没查成」——换个条件或换个说法可以再试，"
                    "但不必当成工具出错。）" + hint
                ),
                meta=_empty_meta("checked", total=total),
            )

        # ---- 批量取配体：拿不到就只给 id，别把整次检索判失败 ----
        hits: dict[str, PdbHit] = {}
        ligand_note = ""
        try:
            hits = fetch_ligands(ids[:size])
        except SearchError as error:
            ligand_note = f"（配体清单这次没取到：{error}；条目 id 仍然可用）"

        entries = [hits.get(pid, PdbHit(pdb_id=pid)) for pid in ids[:size]]
        return ToolResult(
            output=_render(entries, total=total, how=how, condition=condition, note=ligand_note),
            meta={
                "count": len(entries),
                "total": total,
                "shown": len(entries),
                "truncated": total > len(entries),
                "ids": [hit.pdb_id for hit in entries],
                "entries": [hit.as_dict() for hit in entries],
            },
        )


def _empty_meta(reason: str, *, total: int) -> dict[str, Any]:
    """空结果的 meta。**`reason` 是结构化字段**，不让调用方去正文里搜那几个字。

    `"checked"` = 查了、确实没有；`"failed"` = 没查成（网络或接口不可用）。
    与 `pockets.py` 的 `reason` 是同一套分档，措辞也照它那一份。
    """
    return {"count": 0, "total": total, "ids": [], "entries": [], "reason": reason}


def _describe_condition(*, uniprot: str, ligand: str, method: str) -> str:
    parts = []
    if uniprot:
        parts.append(f"UniProt={uniprot}")
    if ligand:
        parts.append(f"配体={ligand}")
    if method:
        parts.append(f"方法={method}")
    return " ".join(parts) or "（无条件）"


def _render(
    entries: list[PdbHit],
    *,
    total: int,
    how: str,
    condition: str,
    note: str = "",
) -> str:
    lines = [f"{how} {condition}：命中 {total} 条，列出 {len(entries)} 条。"]
    for hit in entries:
        head = f"  {hit.pdb_id}"
        if hit.resolution is not None:
            head += f"  {hit.resolution:.2f} Å"
        if hit.method:
            head += f"  {hit.method}"
        lines.append(head)
        if hit.title:
            lines.append(f"      {hit.title}")
        if hit.ligand_names:
            shown = "、".join(f"{code}({name})" if name else code for code, name in hit.ligand_names)
            lines.append(f"      配体：{shown}")
        elif hit.ligands:
            lines.append(f"      配体：{'、'.join(hit.ligands)}")
    if total > len(entries):
        lines.append(
            f"（命中 {total} 条，这里只列了 {len(entries)} 条；要更全请把条件写细，"
            "或调大 limit）"
        )
    lines.append(
        "以上是候选清单，**不含任何归类判断**：哪些配体算真配体、哪个是结合位点，"
        "由后续步骤按项目口径处理。"
    )
    if note:
        lines.append(note)
    return "\n".join(lines)


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [SearchPdbTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
