"""搜索工具（三）：联网搜索与单页抓取（DeepSeek Search / Fetch）。

与 `rsearch.py` / `literature.py` 同一类，但有一处**本质不同**：那两个是纯查询，
这两个会**在本地留下东西**——搜到的网页当场落进 `corpus/web/`，成为可引用、
可离线检索的源。所以描述里必须写明「有副作用」，否则模型会以为搜一下不要钱、
不留痕，然后拿它当免费探路工具反复调。

四条：

1. **计费**：5 元 / 1000 次**成功**请求，空结果、失败、超时都不计费，`count` 不影响
   单价。所以请求恒发 10 条、本地再切——多要几条不花钱。
2. **不设过期**：查过的 query 与服务当时的返回原文存进 `~/.opencode_tui/search/`，
   永久保留。再查同一个 query 直接读档案，**不再付钱**；要新结果只能 `refresh: true`。
3. **空结果不是失败**：`results` 为空与「这次没搜成」对用户的含义相反，沿用
   `rsearch._empty_meta` 的 `reason: checked | failed` 分档。
4. **引用号只能来自返回值**：`web#ab12cd34ef` 是 URL 规范化后的哈希，模型编不出来。

**S1 之后再补的一句**：`search_corpus` / `read_source` 已经上线，所以两个工具的
说明第一段都写上了「先查本地（免费、离线），不够再联网」——顺序反了的话，模型每问
一句都要付一次钱，而答案可能早就在盘上了。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ... import corpus, corpus_config, embed, index, search_config
from ..tools import RetryableToolError, Tool, ToolResult, ToolSpec
from . import http
from .rsearch import cache_path, read_cache, write_cache

__all__ = [
    "DEFAULT_PAGE_CHARS",
    "DEFAULT_RESULTS",
    "FETCH_STORE_CHARS",
    "MAX_PAGE_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_RESULTS",
    "MIN_PAGE_CHARS",
    "REQUEST_COUNT",
    "SUMMARY_CHARS",
    "FetchWebpageTool",
    "SearchWebTool",
    "WebHit",
    "domain_tools",
]

MAX_QUERY_CHARS = 128
"""接口的硬上限（1～128 个 Unicode 字符）。超了先拦下来，别付一次 400 的钱。"""

REQUEST_COUNT = 10
"""请求恒发 10 条：单价与 `count` 无关，多要的本地留着，模型只看到它要的条数。"""

DEFAULT_RESULTS = 5
MAX_RESULTS = 10

SUMMARY_CHARS = 400
"""单条摘要渲染给模型的字数。接口给的 summary 通常 600～800 字，
10 条全展开就是 6000～8000 字——一次搜索吃掉半个上下文，后面的步骤就没地方了。"""

DEFAULT_PAGE_CHARS = 3000
MAX_PAGE_CHARS = 8000
MIN_PAGE_CHARS = 500

FETCH_STORE_CHARS = 20000
"""本地存多少正文。抓一次按次计费，存少了以后再读要重抓（再付一次钱）。"""


@dataclass(frozen=True)
class WebHit:
    """一条搜索结果。**字段全部来自接口**，缺的就是 None / 空串。"""

    title: str
    url: str
    summary: str
    site: str = ""
    date: str = ""
    authority: int | None = None
    score: float | None = None


# ---------------------------------------------------------------------------
# 一、解析
# ---------------------------------------------------------------------------
def _hits_from(body: Any) -> list[WebHit]:
    """只认 `results[]`，每项只取需要的七个字段。

    `site_icon` 直接丢：一个 favicon 地址对模型没有任何用处。`content` 也不取——
    搜索时 `need_content` 恒 false，它本来就是空字符串。
    """
    if not isinstance(body, dict):
        return []
    results = body.get("results")
    if not isinstance(results, list):
        return []
    hits: list[WebHit] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            # 没有链接的结果引用不了，也就落不了库。
            continue
        hits.append(
            WebHit(
                title=str(item.get("title") or "").strip(),
                url=url,
                summary=str(item.get("summary") or item.get("snippet") or "").strip(),
                site=str(item.get("site_name") or "").strip(),
                date=_as_date(item.get("date")),
                authority=_as_authority(item.get("authority")),
                score=_as_float(item.get("rerank_score")),
            )
        )
    return hits


def _as_date(value: Any) -> str:
    """RFC 3339 → `2026-06-01`。写不出日期就留空，不猜。"""
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 and text[4] == "-" else text


def _as_authority(value: Any) -> int | None:
    """权威度 0～4；接口用 `-1` 表示未知，这一档**不显示**（显示成「-1 权威」只会误导）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if 0 <= number <= 4 else None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# 二、落库
# ---------------------------------------------------------------------------
def _store(
    hit: WebHit,
    *,
    depth: str,
    content: str,
    query: str = "",
    truncated: bool = False,
) -> corpus.WebPage:
    """把一条结果 upsert 成 `corpus/web/<id>/`，返回落盘后的那一份。

    三个要当心的地方：

    - **搜索不能把全文打回摘要。** 先 `fetch` 过全文的源，之后任何一次搜索命中都
      只会更新 `fetched_at` 与来源 query，正文与 `depth` 保持 full。反过来的话，
      一次随手搜索就把花过钱的正文冲掉了。
    - **标题不覆盖成空。** 接口偶尔给 `title: ""`，用空串覆盖已存的标题就是净损失。
    - **`labels.json` 不碰**（`corpus.write_web_page` 保证）：那是网页端改名字的地方。
    """
    normalized = corpus.normalize_url(hit.url)
    source_id = corpus.web_id(normalized)
    now = time.time()
    existing = corpus.read_web_page(source_id)

    meta: dict[str, Any] = dict(existing.meta) if existing else {}
    if existing is not None and existing.meta.get("depth") == "full" and depth != "full":
        depth = "full"
        content = existing.content

    queries = [str(item) for item in meta.get("queries") or [] if isinstance(item, str)]
    if query and query not in queries:
        queries.append(query)

    meta.update(
        {
            "id": source_id,
            "kind": "web",
            "url": normalized,
            "url_raw": hit.url,
            "title": hit.title or str(meta.get("title") or ""),
            "summary": hit.summary or str(meta.get("summary") or ""),
            "site": hit.site or str(meta.get("site") or ""),
            "date": hit.date or str(meta.get("date") or ""),
            "authority": hit.authority if hit.authority is not None else meta.get("authority"),
            "rerank_score": hit.score if hit.score is not None else meta.get("rerank_score"),
            "depth": depth,
            "fetched_at": now,
            "queries": queries[-5:],
            "chars": len(content),
            "truncated": bool(truncated),
        }
    )
    meta.setdefault("added_at", now)

    corpus.write_web_page(source_id=source_id, content=content, meta=meta)
    return corpus.WebPage(
        source_id=source_id,
        directory=corpus.web_dir(source_id),
        meta=meta,
        content=content,
    )


def _index_pages(pages: list[corpus.WebPage]) -> dict[str, Any]:
    """把刚落盘的网页同步进索引（含向量）。**失败不抛**：正文已经存好了。

    S0/S1 里这一步是漏的：结果落进了 `corpus/web/`，但 `search_corpus` 要等到
    下一次 `reindex` 才看得见——而工具说明写的是「之后免费可查」。对模型来说
    那就是「刚搜到的东西查不到」，比搜不到更难解释。

    一次搜索最多 10 条，所以在这里合并成一次连接；逐条开连接是 10 次建库。
    """
    if not pages:
        return {"indexed": 0, "vectors": 0}
    config = corpus_config.load()
    indexed = vectors = 0
    try:
        conn = index.connect()
    except Exception as error:  # noqa: BLE001 - 索引坏了不该让搜索结果丢掉
        return {"indexed": 0, "vectors": 0, "error": f"{type(error).__name__}: {error}"}
    try:
        with index.writing():
            for page in pages:
                source_id = f"web:{page.source_id}"
                try:
                    index.upsert_web_page(conn, source_id, page.content, page.meta)
                except Exception:  # noqa: BLE001 - 一条切不开不影响另外九条
                    continue
                indexed += 1
                if config.embed and not embed.missing_reason(config.embed_model):
                    try:
                        result = embed.index_source(conn, source_id, config.embed_model)
                    except Exception:  # noqa: BLE001
                        continue
                    vectors += int(result.get("embedded") or 0)
    finally:
        conn.close()
    return {"indexed": indexed, "vectors": vectors}


def _summary_document(hit: WebHit) -> str:
    """摘要源的 `content.md`：标题 + 出处 + 摘要。

    落库的正文要能**独立读懂**：只存 summary 的话，几个月后看这一段不知道它出自
    哪里、什么时候发的；而 `meta.json` 是给机器看的，模型读到的是这一份。
    """
    lines = [f"# {hit.title or hit.url}", ""]
    lines.append(f"- 来源：{hit.url}")
    if hit.site:
        lines.append(f"- 站点：{hit.site}")
    if hit.date:
        lines.append(f"- 日期：{hit.date}")
    lines.append("")
    lines.append(hit.summary or "（接口没有给出摘要）")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 三、凭据与错误
# ---------------------------------------------------------------------------
def _headers(config: search_config.SearchConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _status_text(error: http.ResponseError) -> str:
    """按状态码分档。**不做字符串匹配**——响应体里凑巧带个 402 就误判了。"""
    code = error.status_code
    if code == 401:
        return (
            "搜索接口拒绝了这把 key（HTTP 401：缺失或无效）。"
            "它可能只有模型权限、没有 /tools 权限。三条路：\n"
            f"  1. 换一把能调 /v1/tools/* 的 key，写进 {search_config.SEARCH_ENV_KEY}；\n"
            "  2. 在应用里用 /model 重新填 key（仅当地址是 api.deepseek.com 时会复用）；\n"
            "  3. 不联网：search_literature 查 PubMed，或检索本地语料。"
        )
    if code == 402:
        return "搜索账号余额不足（HTTP 402）。重试没有用，先充值；本地已有的语料照旧可查。"
    if code == 403:
        return "抓取被目标站点的安全策略拒绝（HTTP 403），换一个来源或直接用它的摘要。"
    if code == 413:
        return "请求体超过接口的大小限制（HTTP 413）。这通常是目标页面太大，换一页试。"
    if code in (400, 422):
        return f"接口说参数不合法（HTTP {code}）：{error}"
    return str(error)


def _failed(action: str, reason: str) -> ToolResult:
    return ToolResult(output=f"{action}没成：{reason}", failed=True)


# ---------------------------------------------------------------------------
# 四、工具
# ---------------------------------------------------------------------------
class SearchWebTool(Tool):
    """联网搜索。**会往本地语料里写东西**，所以它不是纯查询。"""

    spec = ToolSpec(
        name="search_web",
        description=(
            "联网搜索网页。**计费**：5 元 / 1000 次成功请求（空结果、失败、超时不计费）。"
            "**结果会自动存进本地语料**，每条给一个引用号 `web#xxxxxxxxxx`。\n\n"
            "**先查本地**（`search_corpus`，免费、离线、不联网），不够再来这里——"
            "已经搜过的问题答案多半在盘上。同一个 query 有本地档案，再搜不花钱；"
            "要拿最新结果给 `refresh: true`。\n\n"
            "返回每条结果的引用号、标题、站点、日期、权威度（0～4）与 400 字摘要。"
            "**命中 0 条不是失败**（那是「查了，确实没有」），换个说法或加站点/时间限定再试。\n\n"
            "`query` 最长 128 个字符，支持 `site:`、`-site:`、`\"精确短语\"`、"
            "`after:`/`before:`、`lang:` 这些写法。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        f"搜索词，1～{MAX_QUERY_CHARS} 个字符。"
                        "中英文都可以；英文文献用英文关键词命中率更高"
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": f"列给模型看的条数，默认 {DEFAULT_RESULTS}，最多 {MAX_RESULTS}（取更多不额外计费）",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true 时不读本地档案、重新联网（会再付一次费）。默认 false",
                },
            },
            "required": ["query"],
        },
    )
    primary_argument = "query"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failed("搜索", "query 不能为空")
        if len(query) > MAX_QUERY_CHARS:
            # 接口会回 400。它不计费，但白跑一次往返，而且那句报错对模型没有增量信息——
            # 本地就能说清「超了几个字符、怎么改」。
            return _failed(
                "搜索",
                f"query 太长（{len(query)} 字符），接口上限是 {MAX_QUERY_CHARS} 个字符。"
                "把关键词压缩一下，或分两次搜。",
            )
        shown = _clamp_int(arguments.get("count"), DEFAULT_RESULTS, 1, MAX_RESULTS)
        refresh = bool(arguments.get("refresh"))

        path = cache_path("web", {"query": query})
        body = None if refresh else read_cache(path, ttl=None)
        cached = body is not None

        if body is None:
            config = search_config.resolve()
            if config is None:
                return _failed("搜索", search_config.missing_message())
            try:
                body = http.post_json(
                    config.search_url,
                    {"query": query, "count": REQUEST_COUNT, "need_content": False},
                    headers=_headers(config),
                    timeout=search_config.SEARCH_TIMEOUT,
                    retry_statuses=(408,),
                )
            except http.NotFound as error:
                return _failed(
                    "搜索",
                    f"接口回了 404（{error}）。这通常不是 query 的问题，"
                    "稍后重试或检查 VSA_SEARCH_BASE_URL。",
                )
            except http.ResponseError as error:
                return _failed("搜索", _status_text(error))
            except RetryableToolError:
                # 429 / 5xx / 连不上：交给内核的重试策略，不在这里吞掉。
                raise
            write_cache(path, body)

        hits = _hits_from(body)
        if not hits:
            return ToolResult(
                output=(
                    f"搜索「{query}」：查了，确实没有（命中 0 条）。\n"
                    "（这是「查了没有」，不是「没查成」——换个说法、去掉过窄的限定可以再试。）"
                ),
                meta={"count": 0, "total": 0, "cached": cached, "sources": [], "reason": "checked"},
            )

        built = corpus_config.load()
        if not built or not built.index_web:
            # 关掉之后它退回纯查询：一条结果都不落盘，也就不该给出引用号——
            # 给了一个指向不存在目录的 `web#…`，模型下一步会拿它去 read_source。
            visible = hits[:shown]
            return ToolResult(
                output=_render_plain(query, visible, total=len(hits), cached=cached),
                meta={
                    "count": len(visible),
                    "total": len(hits),
                    "cached": cached,
                    "indexed": False,
                    "sources": [{"url": hit.url, "title": hit.title} for hit in visible],
                },
            )

        # **返回的每一条都入库**：一次请求的单价与条数无关，多存的几条以后免费可查。
        pages = [
            _store(hit, depth="summary", content=_summary_document(hit), query=query)
            for hit in hits
        ]
        stored = _index_pages(pages)
        visible = pages[:shown]
        return ToolResult(
            output=_render_search(query, pages, visible, cached=cached),
            meta={
                "count": len(visible),
                "total": len(pages),
                "stored": [page.ref for page in pages],
                "indexed": stored["indexed"],
                "vectors": stored["vectors"],
                "cached": cached,
                "sources": [_brief(page) for page in visible],
            },
        )


class FetchWebpageTool(Tool):
    """抓单页正文。**会覆盖同一 URL 的摘要源**，升成全文本。"""

    spec = ToolSpec(
        name="fetch_webpage",
        description=(
            "抓取单个网页的正文（Markdown），存进本地语料成**全文源**（`web#xxxxxxxxxx`）。"
            "**计费**：5 元 / 1000 次成功请求。\n\n"
            "本地已经有全文时**不再联网、不再计费**（除非 `refresh: true`）；"
            "只想看某段内容的话，先用 `read_source` 读本地那份。"
            "抓过的页面可以反复读，引用号不变。\n\n"
            "只给 `url`。抓不到时如实说明（站点拒绝 / 页面没有可提取正文 / 太大），"
            "此时本地那份摘要还在，只是没有全文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http / https 网页地址（通常来自 search_web 的返回）"},
                "max_chars": {
                    "type": "integer",
                    "description": (
                        f"这次返回多少字，默认 {DEFAULT_PAGE_CHARS}，"
                        f"夹取到 {MIN_PAGE_CHARS}～{MAX_PAGE_CHARS}。本地存的是完整正文"
                    ),
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true 时即使本地有全文也重新抓（会再付一次费）。默认 false",
                },
            },
            "required": ["url"],
        },
    )
    primary_argument = "url"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        raw_url = str(arguments.get("url") or "").strip()
        if not raw_url:
            return _failed("抓取", "url 不能为空")
        try:
            normalized = corpus.normalize_url(raw_url)
        except ValueError as error:
            return _failed("抓取", f"{raw_url} 不是可抓取的网页地址：{error}")
        limit = _clamp_int(
            arguments.get("max_chars"), DEFAULT_PAGE_CHARS, MIN_PAGE_CHARS, MAX_PAGE_CHARS
        )
        refresh = bool(arguments.get("refresh"))

        source_id = corpus.web_id(normalized)
        existing = corpus.read_web_page(source_id)
        if existing is not None and existing.meta.get("depth") == "full" and not refresh:
            return _local_hit(existing, limit)

        config = search_config.resolve()
        if config is None:
            return _failed("抓取", search_config.missing_message())
        try:
            body = http.post_json(
                config.fetch_url,
                {"url": normalized},
                headers=_headers(config),
                timeout=search_config.FETCH_TIMEOUT,
                retry_statuses=(408,),
            )
        except http.NotFound as error:
            # **这个 404 是抓取后端说的，不是页面说的。** 后端可能取不到这个地址
            # （站点拦截、超时、内容类型不支持），也可能压根没有这个路由。
            # 原先写成「资源不存在（HTTP 404）」，模型读到的结论是「页面不存在」，
            # 于是换个说法继续试——而该做的是换个来源。实测时本机直连那个站点
            # 20 秒超时，后端回的却是 404，两个说法指向完全不同的下一步。
            return _failed(
                "抓取",
                f"{normalized} 没抓到：抓取后端回了 404（{error}）。"
                "**这不等于页面不存在**——可能是站点拒绝抓取、后端取不到，"
                "或后端不认识这个地址。换一个来源，或先用 search_web 看看有没有别的页面。",
            )
        except http.ResponseError as error:
            return _failed("抓取", _status_text(error))
        except RetryableToolError:
            raise

        if not isinstance(body, dict):
            return _failed("抓取", "接口返回的不是 JSON 对象")
        content = str(body.get("content") or "").strip()
        if not content:
            return _failed("抓取", f"{normalized} 抓回来是空的：接口没有给出正文。")
        title = str(body.get("title") or "").strip()

        truncated = len(content) > FETCH_STORE_CHARS
        stored = content[:FETCH_STORE_CHARS] if truncated else content
        page = _store(
            WebHit(title=title, url=normalized, summary=""),
            depth="full",
            content=stored,
            truncated=truncated,
        )
        indexed = _index_pages([page])
        return ToolResult(
            output=_render_page(page, limit, fetched=True),
            meta={
                "ref": page.ref,
                "url": normalized,
                "title": page.meta.get("title", ""),
                "chars": len(stored),
                "truncated": truncated,
                "indexed": indexed["indexed"],
                "vectors": indexed["vectors"],
                "cached": False,
            },
        )


# ---------------------------------------------------------------------------
# 五、渲染
# ---------------------------------------------------------------------------
def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """夹取整数参数。写错类型时用默认值，不为此失败一次调用。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _brief(page: corpus.WebPage) -> dict[str, Any]:
    """meta 里每条结果的读数。**不含正文**——meta 会进 trace，塞正文就把它撑爆了。"""
    meta = page.meta
    return {
        "ref": page.ref,
        "url": meta.get("url", ""),
        "title": meta.get("title", ""),
        "site": meta.get("site", ""),
        "date": meta.get("date", ""),
        "authority": meta.get("authority"),
        "depth": meta.get("depth", ""),
    }


def _render_plain(query: str, hits: list[WebHit], *, total: int, cached: bool) -> str:
    """`index_web` 关掉时的渲染：**没有引用号**，因为一条都没落盘。"""
    head = f"搜索「{query}」：返回 {total} 条，列出 {len(hits)} 条。"
    if cached:
        head += "（读的是本地档案，没有重新联网、没有计费）"
    lines = [
        head,
        "（这次**没有入库**：`corpus.toml` 的 `index_web` 关着，结果只能现看，"
        "不能引用、也不会被 search_corpus 检索到。）",
    ]
    for hit in hits:
        lines.append(f"  {hit.title or hit.url}")
        detail = " · ".join(part for part in (hit.site, hit.date) if part)
        if detail:
            lines.append(f"      {detail}")
        lines.append(f"      {hit.url}")
        lines.append(f"      {_snippet(hit.summary, SUMMARY_CHARS)}")
    if total > len(hits):
        lines.append(f"（另有 {total - len(hits)} 条没列出，调大 count 可以看到。）")
    return "\n".join(lines)


def _render_search(
    query: str,
    pages: list[corpus.WebPage],
    visible: list[corpus.WebPage],
    *,
    cached: bool,
) -> str:
    head = f"搜索「{query}」：返回 {len(pages)} 条，列出 {len(visible)} 条。"
    if cached:
        head += "（读的是本地档案，没有重新联网、没有计费；要最新结果加 refresh: true）"
    lines = [head]
    for page in visible:
        meta = page.meta
        title = str(meta.get("title") or meta.get("url") or "")
        mark = ""
        authority = meta.get("authority")
        if isinstance(authority, int):
            mark = f"  [权威{authority}]"
        lines.append(f"  {page.ref}{mark}  {title}")
        detail = " · ".join(
            part for part in (str(meta.get("site") or ""), str(meta.get("date") or "")) if part
        )
        if detail:
            lines.append(f"      {detail}")
        lines.append(f"      {str(meta.get('url') or '')}")
        lines.append(f"      {_snippet(str(meta.get('summary') or page.content), SUMMARY_CHARS)}")
    if len(pages) > len(visible):
        lines.append(
            f"（另有 {len(pages) - len(visible)} 条也已存入本地语料，调大 count 可以看到；"
            "它们同样能用 search_corpus 检索到）"
        )
    lines.append(
        f"以上 {len(pages)} 条都已存进本地语料（引用号如上）。要读全文用 fetch_webpage，"
        "之后同一个 query 再搜不花钱。"
    )
    return "\n".join(lines)


def _render_page(page: corpus.WebPage, limit: int, *, fetched: bool) -> str:
    meta = page.meta
    title = str(meta.get("title") or meta.get("url") or "")
    lines = [f"{page.ref}  {title}", f"  {meta.get('url', '')}"]
    if fetched:
        lines.append(f"  已抓取并存入本地语料（全文 {meta.get('chars', 0)} 字）。")
    if meta.get("truncated"):
        # 存不下就明说。悄悄截断的话，模型会以为「后面没有内容」而不是「本地没存」。
        lines.append(f"（原文超过本地存储上限 {FETCH_STORE_CHARS} 字，后面那部分没有存。）")
    lines.append("")
    lines.append(_snippet(page.content, limit))
    if len(page.content) > limit:
        lines.append("")
        lines.append(f"（这里只给了前 {limit} 字，本地存了 {len(page.content)} 字；要读后面调大 max_chars）")
    return "\n".join(lines)


def _local_hit(page: corpus.WebPage, limit: int) -> ToolResult:
    """本地已有全文：不联网、不计费。**要明说这次没花钱**，否则模型会以为每读一次都付费。"""
    output = "（本地已有这份页面的全文，这次没有联网、没有计费。）\n" + _render_page(page, limit, fetched=False)
    return ToolResult(
        output=output,
        meta={
            "ref": page.ref,
            "url": page.meta.get("url", ""),
            "title": page.meta.get("title", ""),
            "chars": len(page.content),
            "truncated": len(page.content) > limit,
            "cached": True,
        },
    )


def _snippet(text: str, limit: int) -> str:
    """截一段并压成一行一段的样子：多行摘要会让每条结果占六七行，五条就一屏。"""
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。顺序即请求体里的顺序：先搜，再抓。"""
    return [SearchWebTool(), FetchWebpageTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
