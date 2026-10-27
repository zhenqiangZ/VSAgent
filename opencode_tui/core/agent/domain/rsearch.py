"""搜索工具（一）：RCSB 文本搜 / 属性搜 + GraphQL 批量取配体。

**这一步给的是候选，不是结论。** 返回条目 id 与每个条目的配体代号与名称，
**不做任何归类**：不判「这个配体算不算真配体」、不判「这是不是结合位点」。
那些判断要过 `export.py` 的口径（排除水 / 修饰残基 / 单原子离子 / 常见缓冲液），
是 `write_run` 的活（方案稿 §2.3.11）。

**搜索结果不写进 `meta.json`。** 只缓存到 `~/.opencode_tui/search/`（时效一天）；
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools import RetryableToolError, Tool, ToolError, ToolResult, ToolSpec
from . import http

__all__ = [
    "DEFAULT_LIMIT",
    "GRAPHQL_BATCH",
    "MAX_LIMIT",
    "PdbHit",
    "RCSB_GRAPHQL_URL",
    "RCSB_SEARCH_URL",
    "SearchError",
    "SearchPdbTool",
    "attribute_query",
    "domain_tools",
    "fetch_ligands",
    "search_dir",
    "search_entries",
    "text_query",
]

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"

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
    `complexes` 自带一份 `~/.opencode_tui/rcsb/search/`，两处写的逻辑一样、
    目录不同——同一个 Uniprot 连问两遍也互相看不见。合并之后只认这一处，
    键由 `_cache_path(kind, payload)` 算，**不同模块的同类请求算出来的键必须一致**。
    """
    return Path.home() / ".opencode_tui" / "search"


def _cache_path(kind: str, payload: Any) -> Path:
    blob = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return search_dir() / f"{kind}-{digest}.json"


def _read_cache(path: Path, *, ttl: float | None = SEARCH_CACHE_TTL) -> Any | None:
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


def _write_cache(path: Path, body: Any) -> None:
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
    path = _cache_path("search", query)
    if use_cache:
        cached = _read_cache(path)
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
        _write_cache(path, body)
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
    "nonpolymer_entities{nonpolymer_comp{chem_comp{id name}}}"
    "}}"
)


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
        path = _cache_path("ligands", {"ids": chunk})
        body = _read_cache(path) if use_cache else None
        if body is None:
            try:
                body = http.post_json(RCSB_GRAPHQL_URL, payload, timeout=timeout)
            except RetryableToolError:
                # 同上：可重试的交给循环层，别在这里判死。
                raise
            except Exception as error:
                raise SearchError(f"RCSB 配体查询没成：{error}") from error
            if use_cache:
                _write_cache(path, body)
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
        seen: set[str] = set()
        entities = item.get("nonpolymer_entities")
        if isinstance(entities, list):
            for entity in entities:
                comp = _comp_of(entity)
                code = str(comp.get("id") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                codes.append(code)
                names.append((code, str(comp.get("name") or "").strip()))

        hits.append(
            PdbHit(
                pdb_id=pdb_id,
                title=str(structure.get("title") or "").strip(),
                resolution=_as_float(info.get("resolution_combined")),
                method=str(info.get("experimental_method") or "").strip(),
                ligands=tuple(codes),
                ligand_names=tuple(names),
                nonpolymer_count=int(info.get("nonpolymer_entity_count") or 0),
            )
        )
    return hits


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
                    "description": "配体代号（chem_comp id，如 STI）。给出它则按「含该配体」筛",
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
            return ToolResult(
                output=(
                    f"{how} {condition}：查了，确实没有（命中 0 条）。\n"
                    "（这是「查了没有」，不是「没查成」——换个条件或换个说法可以再试，"
                    "但不必当成工具出错。）"
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
