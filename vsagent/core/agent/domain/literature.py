"""搜索工具（二）：PubMed 文献检索（E-utilities）。

与 `rsearch.py` 同一类：**给模型用的只读工具**，返回候选清单，**不下结论**
（不判「哪篇讲了结合位点」、不判「这篇可信不可信」）。搜索结果不写进 `meta.json`。

两步：

1. `esearch`：按检索式拿 PMID 列表与命中总数。
2. `efetch`：按 PMID 取标题、期刊、年份、作者与摘要片段。

**摘要片段是片段，不是全文。** 想看全文要点开链接；这里只给够判断「要不要点」的信息。
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Any

from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import http
from .rsearch import cache_path, read_cache, write_cache

__all__ = [
    "Article",
    "DEFAULT_LIMIT",
    "EFETCH_URL",
    "ESEARCH_URL",
    "LiteratureError",
    "MAX_LIMIT",
    "SearchLiteratureTool",
    "domain_tools",
    "esearch",
    "fetch_details",
    "search_literature",
]

# 两个搜索工具共用一个缓存目录（§7.11）：`~/.opencode_tui/search/`。
# 所以缓存的读写与路径拼法从 `rsearch.py` 拿，不在这里再写一遍——两份缓存逻辑
# 迟早会漂，而漂了之后「同一个查询两次结果不同」会被当成接口不稳。
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
"""文献一次最多列多少条。再多就该把检索式写细。"""

ABSTRACT_CHARS = 400
"""摘要片段最多留多少字。"""


class LiteratureError(ToolError):
    """文献接口不可用或返回的形状不对。**当「这次没搜成」，不重试到底。**"""


@dataclass
class Article:
    """一篇文献。字段全部来自 PubMed，**没有一处是我们编的**。"""

    pmid: str
    title: str = ""
    journal: str = ""
    year: str = ""
    authors: tuple[str, ...] = ()
    abstract: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pmid": self.pmid,
            "title": self.title,
            "journal": self.journal,
            "year": self.year,
            "authors": list(self.authors),
            "abstract": self.abstract,
        }

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"


# ---------------------------------------------------------------------------
# 1. esearch：检索式 → PMID 列表
# ---------------------------------------------------------------------------
def esearch(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> tuple[list[str], int]:
    """按检索式拿 PMID 列表与命中总数。返回 `(id 列表, 总数)`。"""
    term = (query or "").strip()
    if not term:
        raise LiteratureError("检索式不能为空")

    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(max(1, limit)),
        "sort": "relevance",
    }
    try:
        payload = http.get_json(ESEARCH_URL, params, timeout=timeout)
    except Exception as error:  # 4xx / 5xx / 连不上都归为「这次没搜成」
        raise LiteratureError(f"PubMed 检索没成：{error}") from error

    result = payload.get("esearchresult") if isinstance(payload, dict) else None
    result = result if isinstance(result, dict) else {}
    idlist = result.get("idlist")
    ids = [str(item) for item in idlist] if isinstance(idlist, list) else []
    try:
        total = int(result.get("count") or len(ids))
    except (TypeError, ValueError):
        total = len(ids)
    return ids, total


# ---------------------------------------------------------------------------
# 2. efetch：PMID → 标题 / 期刊 / 年份 / 作者 / 摘要片段
# ---------------------------------------------------------------------------
def fetch_details(
    pmids: list[str],
    *,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> list[Article]:
    """按 PMID 取详情。**解不出来就少给几项，不编**。"""
    wanted = [str(pid).strip() for pid in pmids if str(pid).strip()]
    if not wanted:
        return []

    params = {"db": "pubmed", "id": ",".join(wanted), "retmode": "xml"}
    try:
        text = http.get_text(EFETCH_URL, params, timeout=timeout)
    except Exception as error:
        raise LiteratureError(f"PubMed 详情没取到：{error}") from error

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise LiteratureError(f"PubMed 返回的不是能解的 XML：{error}") from error

    articles: list[Article] = []
    for node in root.iter("PubmedArticle"):
        article = _article_from(node)
        if article is not None:
            articles.append(article)
    return articles


def _article_from(node: ElementTree.Element) -> Article | None:
    pmid = _text(node.find("./MedlineCitation/PMID"))
    if not pmid:
        return None
    article_node = node.find("./MedlineCitation/Article")
    if article_node is None:
        return Article(pmid=pmid)

    title = _text(article_node.find("./ArticleTitle"))
    journal = _text(article_node.find("./Journal/Title")) or _text(
        article_node.find("./Journal/ISOAbbreviation")
    )
    year = (
        _text(article_node.find("./Journal/JournalIssue/PubDate/Year"))
        or _text(article_node.find("./Journal/JournalIssue/PubDate/MedlineDate"))[:4]
    )
    authors = tuple(_author_of(item) for item in article_node.findall("./AuthorList/Author"))
    authors = tuple(name for name in authors if name)

    return Article(
        pmid=pmid,
        title=title,
        journal=journal,
        year=year,
        authors=authors,
        abstract=_abstract_of(article_node),
    )


def _abstract_of(article_node: ElementTree.Element) -> str:
    """摘要：几段 `AbstractText` 拼成一段，**截到片段长度**。"""
    chunks: list[str] = []
    for item in article_node.findall("./Abstract/AbstractText"):
        # 摘要里常有 <i>、<sub> 这类内嵌标签，itertext 把文字拼回来。
        piece = "".join(item.itertext()).strip()
        label = (item.get("Label") or "").strip()
        if piece:
            chunks.append(f"{label}: {piece}" if label else piece)
    text = " ".join(chunks).strip()
    if len(text) > ABSTRACT_CHARS:
        text = text[:ABSTRACT_CHARS].rstrip() + "…"
    return text


def _author_of(node: ElementTree.Element) -> str:
    last = _text(node.find("./LastName"))
    initials = _text(node.find("./Initials")) or _text(node.find("./ForeName"))
    if last and initials:
        return f"{last} {initials}"
    return last or _text(node.find("./CollectiveName"))


def _text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


# ---------------------------------------------------------------------------
# 合成一步
# ---------------------------------------------------------------------------
def search_literature(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> tuple[list[Article], int]:
    """检索 → 取详情。返回 `(文献列表, 命中总数)`。

    详情取不到时**退回只给 id**（`Article` 只填 `pmid`），不把整次检索判失败：
    标题丢了还能点链接看，整次判失败就什么也没有。
    """
    term = (query or "").strip()
    path = cache_path("literature", {"term": term, "limit": limit})
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            articles = [Article(**item) for item in cached.get("articles", [])]
            return articles, int(cached.get("total") or len(articles))

    ids, total = esearch(term, limit=limit, timeout=timeout)
    if not ids:
        return [], total

    try:
        articles = fetch_details(ids, timeout=timeout)
    except LiteratureError:
        articles = [Article(pmid=pid) for pid in ids]

    if use_cache:
        write_cache(
            path,
            {"articles": [item.as_dict() for item in articles], "total": total},
        )
    return articles, total


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class SearchLiteratureTool(Tool):
    """检索 PubMed 文献。纯查询，不产生产物，所以不收登记表。"""

    spec = ToolSpec(
        name="search_literature",
        description=(
            "检索 PubMed 文献，返回**候选清单**：PMID、标题、期刊、年份、作者与摘要片段。"
            "同时报出**命中总数**——只列前若干条时，别以为一共就这么多。\n\n"
            "**这是候选，不是结论。** 本工具不判「哪篇讲了结合位点」「这篇可不可信」"
            "——那是读文献的人（模型）的活。摘要只给片段，要判断详情请点 PubMed 链接。\n\n"
            "支持 PubMed 检索语法：`SCN9A AND \"PDB:6J8I\"` 可以按 PDB 编号反查文献；"
            "`Nav1.7 structure cryo-EM` 是按主题找。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "PubMed 检索式，例如 SCN9A Nav1.7 cryo-EM complex，"
                        "或 SCN9A AND \"PDB:6J8I\"（按 PDB 编号反查）"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多列多少条，上限 {MAX_LIMIT}",
                },
            },
            "required": ["query"],
        },
    )
    primary_argument = "query"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                output="query 不能为空。填检索式，例如 SCN9A Nav1.7 cryo-EM complex。",
                failed=True,
            )

        try:
            requested = int(arguments.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult(output="limit 应当是整数", failed=True)
        size = max(1, min(requested, MAX_LIMIT))

        try:
            articles, total = search_literature(query, limit=size)
        except LiteratureError as error:
            # 网络不可达 / 4xx / 5xx 都走这里：返回一句可读的失败，**不抛异常、不重试到底**。
            return ToolResult(output=f"这次没搜成（PubMed「{query}」）：{error}", failed=True)

        if not articles:
            return ToolResult(
                output=f"这次没搜成（PubMed「{query}」）：命中 0 篇，没有可列的文献。",
                meta={"count": 0, "total": total, "ids": [], "articles": []},
            )

        return ToolResult(
            output=_render(articles, total=total, query=query),
            meta={
                "count": len(articles),
                "total": total,
                "shown": len(articles),
                "truncated": total > len(articles),
                "ids": [item.pmid for item in articles],
                "articles": [item.as_dict() for item in articles],
            },
        )


def _render(articles: list[Article], *, total: int, query: str) -> str:
    lines = [f"PubMed「{query}」：命中 {total} 篇，列出 {len(articles)} 篇。"]
    for item in articles:
        head = f"  PMID {item.pmid}"
        if item.year:
            head += f"  {item.year}"
        if item.journal:
            head += f"  {item.journal}"
        lines.append(head)
        if item.title:
            lines.append(f"      {item.title}")
        if item.authors:
            shown = "、".join(item.authors[:3])
            more = " 等" if len(item.authors) > 3 else ""
            lines.append(f"      作者：{shown}{more}")
        if item.abstract:
            lines.append(f"      摘要：{item.abstract}")
        lines.append(f"      {item.url}")
    if total > len(articles):
        lines.append(
            f"（命中 {total} 篇，这里只列了 {len(articles)} 篇；要更全请把检索式写细，或调大 limit）"
        )
    lines.append("以上是候选清单，**不含任何判断**：哪篇相关、可不可信，由读的人定。")
    return "\n".join(lines)


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [SearchLiteratureTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
