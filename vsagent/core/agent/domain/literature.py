"""搜索工具（二）：文献检索（PubMed E-utilities / Europe PMC）。

与 `rsearch.py` 同一类：**给模型用的只读工具**，返回候选清单，**不下结论**
（不判「哪篇讲了结合位点」、不判「这篇可信不可信」）。搜索结果不写进 `meta.json`。

两条来源，分工不同：

- PubMed（`search_literature`）：两步 `esearch` → `efetch`，给标题、期刊、年份、作者
  与摘要**片段**。只收 PubMed 收录的期刊文献。
- Europe PMC（`search_europepmc` / `read_fulltext`）：一步检索就能拿到摘要、被引次数、
  OA 标志与 PMCID，还能**读开放全文的某一节正文**，并且覆盖预印本与专利。

**摘要片段是片段，不是全文。** 想知道方法里用了什么、结果里写了哪些数，用
`read_fulltext` 拿那一节；但只有 `isOpenAccess=Y` 的条目有全文，其余只能点链接看。
"""

from __future__ import annotations

import html
import re
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
    "EPMC_ABSTRACT_CHARS",
    "EPMC_DEFAULT_LIMIT",
    "EPMC_MAX_LIMIT",
    "ESEARCH_URL",
    "EUROPEPMC_ARTICLE_URL",
    "EUROPEPMC_FULLTEXT_URL",
    "EUROPEPMC_SEARCH_URL",
    "FULLTEXT_DEFAULT_CHARS",
    "FULLTEXT_MAX_CHARS",
    "LiteratureError",
    "MAX_LIMIT",
    "Paper",
    "ReadFulltextTool",
    "SearchEuropePmcTool",
    "SearchLiteratureTool",
    "domain_tools",
    "esearch",
    "fetch_details",
    "read_fulltext",
    "search_europepmc",
    "search_literature",
]

# 两个搜索工具共用一个缓存目录（§7.11）：`~/.vsagent/search/`。
# 所以缓存的读写与路径拼法从 `rsearch.py` 拿，不在这里再写一遍——两份缓存逻辑
# 迟早会漂，而漂了之后「同一个查询两次结果不同」会被当成接口不稳。
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Europe PMC 的三个地址。检索与全文各一个接口；条目页只用来给人点。
# 选它而不是 PubMed 的第二套接口，是因为**一次检索就带回摘要、被引次数与 OA 标志**，
# 而 PubMed 那条要 esearch + efetch 两次；要读正文时它也是唯一给得到 JATS XML 的。
EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
EUROPEPMC_ARTICLE_URL = "https://europepmc.org/article/{source}/{id}"

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
"""文献一次最多列多少条。再多就该把检索式写细。"""

EPMC_DEFAULT_LIMIT = 10
EPMC_MAX_LIMIT = 50
EPMC_ABSTRACT_CHARS = 400
"""Europe PMC 的摘要片段长度。与 PubMed 那条取同一个数量级——两条路的返回要能并排看。"""

FULLTEXT_DEFAULT_CHARS = 4000
FULLTEXT_MAX_CHARS = 12000
FULLTEXT_MIN_CHARS = 200
"""全文一节给多少字。

默认 4000（约一节的方法或结果），硬上限 12000：再长就不是「读一节」而是「读整篇」，
那会把上下文一口气吃掉，而模型下一步通常只需要那一节里的几个数。"""

MAX_SECTIONS = 25
"""章节清单最多列多少条。"""

ABSTRACT_CHARS = 400
"""PubMed 摘要片段最多留多少字。"""


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
            "`Nav1.7 structure cryo-EM` 是按主题找。要预印本或要读开放全文的正文，"
            "换 search_europepmc / read_fulltext（这条只查 PubMed、只给摘要片段）。"
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


# ---------------------------------------------------------------------------
# 3. Europe PMC：一步检索（含预印本）→ 开放全文的某一节
# ---------------------------------------------------------------------------
# 接口给的摘要带 `<h4>Background</h4>` 这类小标签。不剥的话，摘要那行会以
# `&lt;h4&gt;` 的样子进上下文，模型读到的是带排版噪声的文字；而摘要本来就是要在
# 几百字里判断「要不要点开」。
#
# **用这条正则而不是 HTMLParser**：这里要处理的只有 b/i/sub/sup/h4 与实体，
# 引一个解析器反而会把 `<` 当标签开头时的科学写法（`p < 0.05`）吃掉。要求 `<`
# 后面紧跟字母，正是为了放过那个空格。
MARKUP = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]{0,15}(\s[^<>]{0,80})?/?>")
BLOCK_MARKUP = re.compile(r"</?(?:h[1-6]|p|br|div|li|ul|ol|tr|td|th|section)\b[^>]*>", re.I)
"""块级标签换成空格，行内标签（`<i>` / `<sub>`）**直接删掉**。

差别不是排版洁癖：Europe PMC 的摘要里 `second-<i>line</i>` 这种断词很常见，
统一换成空格会写成「second- line」，在化学名与基因名上看着像拼错了——
PDB 标题那边（`structure._join_wrapped`）踩的是同一个坑。"""

WHITESPACE = re.compile(r"\s+")


def _plain(raw: str) -> str:
    """HTML 片段 → 一行纯文本。接口层用，不与 JATS 的 XML 解析混在一起。"""
    text = BLOCK_MARKUP.sub(" ", raw or "")
    text = MARKUP.sub("", text)
    text = html.unescape(text)
    return WHITESPACE.sub(" ", text).strip()


@dataclass
class Paper:
    """一条 Europe PMC 命中。字段全部来自接口，**没有一处是我们补的**。"""

    source: str = ""
    ident: str = ""
    pmid: str = ""
    pmcid: str = ""
    doi: str = ""
    title: str = ""
    journal: str = ""
    year: str = ""
    authors: str = ""
    abstract: str = ""
    cited_by: int = 0
    open_access: bool = False
    kinds: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return EUROPEPMC_ARTICLE_URL.format(source=self.source or "MED", id=self.ident)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "id": self.ident,
            "pmid": self.pmid,
            "pmcid": self.pmcid,
            "doi": self.doi,
            "title": self.title,
            "journal": self.journal,
            "year": self.year,
            "authors": self.authors,
            "abstract": self.abstract,
            "cited_by": self.cited_by,
            "open_access": self.open_access,
            "kinds": list(self.kinds),
            "url": self.url,
        }


PAPER_FIELDS = frozenset(Paper.__dataclass_fields__)
"""缓存读回来时按这个过滤：`as_dict` 多给了 `id` / `url` 两个给 model 看的键，
直接把整条塞进构造器会 TypeError。"""


def _paper_from(item: dict[str, Any]) -> Paper:
    journal_info = item.get("journalInfo") if isinstance(item.get("journalInfo"), dict) else {}
    journal = journal_info.get("journal") if isinstance(journal_info.get("journal"), dict) else {}
    book = (
        item.get("bookOrReportDetails")
        if isinstance(item.get("bookOrReportDetails"), dict)
        else {}
    )
    kinds = item.get("pubTypeList") if isinstance(item.get("pubTypeList"), dict) else {}
    abstract = _plain(str(item.get("abstractText") or ""))
    if len(abstract) > EPMC_ABSTRACT_CHARS:
        abstract = abstract[:EPMC_ABSTRACT_CHARS].rstrip() + "…"
    return Paper(
        source=str(item.get("source") or ""),
        ident=str(item.get("id") or ""),
        pmid=str(item.get("pmid") or ""),
        pmcid=str(item.get("pmcid") or ""),
        doi=str(item.get("doi") or ""),
        title=_plain(str(item.get("title") or "")),
        journal=str(journal.get("title") or journal.get("medlineAbbreviation") or book.get("publisher") or ""),
        year=str(item.get("pubYear") or ""),
        authors=str(item.get("authorString") or "").rstrip("."),
        abstract=abstract,
        cited_by=int(item.get("citedByCount") or 0),
        open_access=str(item.get("isOpenAccess") or "N").upper() == "Y",
        kinds=tuple(str(kind) for kind in (kinds.get("pubType") or [])),
    )


SORT_ORDERS = {
    "relevance": "",
    "cited": "CITED desc",
    "date": "P_PDATE_D desc",
}
"""给模型看的三种排序 → 接口的排序表达式。

**不做透传**：让模型直接填 `CITED desc` 这种表达式，它写错时接口回的是空结果，
而空结果在这里读起来和「真的没有」一样。"""


def search_europepmc(
    query: str,
    *,
    limit: int = EPMC_DEFAULT_LIMIT,
    sort: str = "relevance",
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> tuple[list[Paper], int]:
    """检索 Europe PMC。返回 `(论文列表, 命中总数)`。

    `resultType=core` 一次带回摘要、被引次数与 OA 标志；`lite` 要省的那几 KB
    换不来第二次请求，所以不省。缓存与 PubMed 那条共用 `~/.vsagent/search/`，
    键里带 `source`，两条路的同名查询不会互相顶掉。
    """
    term = (query or "").strip()
    if not term:
        raise LiteratureError("检索式不能为空")
    order = SORT_ORDERS.get(sort, "")
    path = cache_path(
        "literature", {"term": term, "limit": limit, "sort": sort, "source": "europepmc"}
    )
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            papers = [
                Paper(**{key: value for key, value in item.items() if key in PAPER_FIELDS})
                for item in cached.get("papers", [])
                if isinstance(item, dict)
            ]
            return papers, int(cached.get("total") or len(papers))

    params: dict[str, Any] = {
        "query": term,
        "format": "json",
        "resultType": "core",
        "pageSize": str(max(1, limit)),
    }
    if order:
        params["sort"] = order
    try:
        payload = http.get_json(EUROPEPMC_SEARCH_URL, params, timeout=timeout)
    except Exception as error:  # 4xx / 5xx / 连不上都算「这次没搜成」
        raise LiteratureError(f"Europe PMC 检索没成：{error}") from error

    if not isinstance(payload, dict):
        raise LiteratureError("Europe PMC 返回的不是一个 JSON 对象")
    rows = payload.get("resultList")
    rows = rows.get("result") if isinstance(rows, dict) else None
    papers = [_paper_from(item) for item in rows or [] if isinstance(item, dict)]
    try:
        total = int(payload.get("hitCount") or len(papers))
    except (TypeError, ValueError):
        total = len(papers)

    if use_cache:
        write_cache(path, {"papers": [item.as_dict() for item in papers], "total": total})
    return papers, total


def _resolve_article(article: str, *, timeout: float = http.DEFAULT_TIMEOUT) -> dict[str, Any]:
    """把 PMCID / PMID / DOI 换成一条 Europe PMC 记录（`resultType=lite`）。

    **先解析再取全文**，不直接拼全文地址：全文接口对「没有全文」的条目回的是
    HTTP 500（实测 PMC5884736），与真的服务故障同码。只看状态码分不出这两种，
    而 `isOpenAccess` 这个字段分得出——它就在这一步的返回里。
    """
    text = str(article or "").strip()
    upper = text.upper()
    if upper.startswith("PMC") and upper[3:].isdigit():
        query = f"PMCID:{upper}"
    elif upper.startswith("PPR") and upper[3:].isdigit():
        # 预印本的编号（bioRxiv/medRxiv 在 Europe PMC 里的 id）。它们基本没有
        # 全文 XML，但**认得出**才能给出「这篇没有 PMC 全文」这句实话——
        # 回一句「编号认不出来」会让人以为换个写法就能读。
        query = f"EXT_ID:{upper} AND SRC:PPR"
    elif text.isdigit():
        query = f"EXT_ID:{text} AND SRC:MED"
    elif text.startswith("10."):
        query = f'DOI:"{text}"'
    else:
        raise LiteratureError(
            f"{article!r} 认不出来。给 PMCID（PMC12545111）、PMID（41142240）或 DOI"
            "（10.3389/fphar.2025.1628024）——前两种在 search_europepmc 的每一行里都有"
        )

    try:
        payload = http.get_json(
            EUROPEPMC_SEARCH_URL,
            {"query": query, "format": "json", "resultType": "lite", "pageSize": "1"},
            timeout=timeout,
        )
    except Exception as error:
        raise LiteratureError(f"查 {text} 的条目没成：{error}") from error

    rows = payload.get("resultList") if isinstance(payload, dict) else None
    rows = rows.get("result") if isinstance(rows, dict) else None
    if not isinstance(rows, list) or not rows:
        raise LiteratureError(f"Europe PMC 里没有 {text} 对应的记录")
    first = rows[0]
    return first if isinstance(first, dict) else {}


def _strip_namespaces(root: ElementTree.Element) -> None:
    """去掉 JATS 里的命名空间前缀。

    **不做这一步，`find("body")` 会一无所获**：带默认命名空间的 XML 解析出来的
    tag 是 `{http://…}body`，而按无前缀写法去查永远匹配不上——症状是「全文读到了，
    但一节都没有」，看上去像这篇论文没有正文。
    """
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]


def _node_text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return _plain("".join(node.itertext()))


def _jats_sections(root: ElementTree.Element) -> list[tuple[str, ElementTree.Element]]:
    """正文的顶层章节。没有 `sec` 的文章把整个 body 当成一节。"""
    body = root.find(".//body")
    if body is None:
        return []
    sections = [
        (_node_text(sec.find("title")) or "（无标题）", sec) for sec in body.findall("sec")
    ]
    if not sections and _node_text(body):
        sections.append(("正文", body))
    return sections


def _flatten_section(node: ElementTree.Element) -> str:
    """一节的全部正文：段落按顺序拼，子章节标题写成 `[标题]`。

    **不递归 `findall("p")` 而遍历整棵树**：`sec` 里的小节也是 `sec`，只取直接子级
    会漏掉「3.1 基线与敏感性分析」这些真正放了数字的段。
    """
    parts: list[str] = []
    for element in node.iter():
        if element is node:
            continue
        if element.tag == "title":
            text = _node_text(element)
            if text:
                parts.append(f"[{text}]")
        elif element.tag == "p":
            text = _node_text(element)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _jats_abstract(root: ElementTree.Element) -> str:
    node = root.find(".//abstract")
    if node is None:
        return ""
    paragraphs = [_node_text(item) for item in node.iter("p")]
    return "\n".join(text for text in paragraphs if text).strip()


@dataclass
class Fulltext:
    """一篇开放全文里我们真正用得上的部分。"""

    pmcid: str
    title: str
    journal: str
    year: str
    license: str
    abstract: str
    sections: list[tuple[str, str]]
    """`(标题, 正文)`，正文已按 `max_chars` 截断；`total_chars` 是截断前的长度。"""

    total_chars: int = 0
    matched: str = ""


def read_fulltext(
    article: str,
    *,
    section: str = "",
    max_chars: int = FULLTEXT_DEFAULT_CHARS,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> Fulltext:
    """取一篇开放获取论文的全文结构（或其中一节）。

    `section` 为空时只返回元数据、摘要与章节清单——那是「先看看有什么」的那一步；
    给了就按标题子串取一节，截到 `max_chars` 并如实报出原文有多少字。
    """
    size = max(FULLTEXT_MIN_CHARS, min(int(max_chars), FULLTEXT_MAX_CHARS))
    record = _resolve_article(article, timeout=timeout)
    pmcid = str(record.get("pmcid") or "")
    title = _plain(str(record.get("title") or ""))
    if not pmcid:
        raise LiteratureError(
            f"{article} 没有 PMC 全文编号（预印本与未收全文的条目都是这样）。"
            f"只有摘要可看：用 {EUROPEPMC_ARTICLE_URL.format(source=record.get('source') or 'MED', id=record.get('id') or article)}"
            " 打开，或用 DOI 去出版社站点"
        )
    if str(record.get("isOpenAccess") or "N").upper() != "Y":
        raise LiteratureError(
            f"{pmcid} 不是开放获取（isOpenAccess=N），Europe PMC 不提供它的全文 XML。"
            "要读正文得去出版社站点；这里的摘要与元数据仍然可用"
        )

    url = EUROPEPMC_FULLTEXT_URL.format(pmcid=pmcid)
    try:
        # **必须显式要 XML**：`http` 的默认 Accept 是 application/json，而这个端点
        # 会照着 Accept 挑格式、挑不到就回 406——一个与「这篇有没有全文」完全无关的
        # 状态码，误报成「不能读」最难查。
        xml_text = http.get_text(url, timeout=timeout, headers={"Accept": "application/xml"})
    except http.RetryableToolError as error:
        raise LiteratureError(
            f"取 {pmcid} 的全文失败（{error}）。Europe PMC 对「没有全文」的条目也回 5xx，"
            "所以这可能是暂时故障、也可能是这篇其实没有开放全文；稍后重试一次，仍不行就只看摘要"
        ) from error
    except http.ToolError as error:
        raise LiteratureError(f"取 {pmcid} 的全文失败：{error}") from error

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise LiteratureError(f"Europe PMC 返回的全文不是能解的 XML：{error}") from error
    _strip_namespaces(root)

    sections = _jats_sections(root)
    if not sections:
        raise LiteratureError(f"{pmcid} 的全文里没有正文段落（可能只 deposit 了摘要）")

    info = root.find(".//journal-title")
    year_node = root.find(".//pub-date/year")
    license_node = root.find(".//license")

    abstract = _jats_abstract(root)
    wanted = section.strip().lower()
    matched = ""
    total_chars = 0
    picked: list[tuple[str, str]] = []

    if not wanted:
        # 只给清单时不拼正文：一篇十几节的论文全拼一遍只为印几个标题，白花时间，
        # 而清单本身只需要标题。
        picked = [(name, "") for name, _node in sections[:MAX_SECTIONS]]
    elif wanted in {"abstract", "摘要"}:
        matched = "Abstract"
        total_chars = len(abstract)
        picked = [(matched, abstract[:size].rstrip())]
    else:
        for name, node in sections:
            if wanted not in name.lower():
                continue
            text = _flatten_section(node)
            matched = name
            total_chars = len(text)
            picked = [(name, text[:size].rstrip())]
            break
        if not matched:
            raise LiteratureError(
                f"{pmcid} 里没有标题含「{section}」的一节。可读的有："
                + "、".join(name for name, _node in sections[:MAX_SECTIONS])
            )

    license_text = ""
    if license_node is not None:
        # **只取 `license-type` 这个短标签**（"cc by" / "open-access"）。JATS 的
        # `<license-p>` 是一整段授权声明——实测两百多字，塞进首行会把标题挤到看不见。
        license_text = str(license_node.get("license-type") or "").strip()

    return Fulltext(
        pmcid=pmcid,
        title=title or _node_text(root.find(".//article-title")),
        journal=_node_text(info),
        year=_node_text(year_node),
        license=license_text,
        abstract=abstract,
        sections=picked,
        total_chars=total_chars,
        matched=matched,
    )


def _render_papers(papers: list[Paper], *, total: int, query: str) -> str:
    lines = [f"Europe PMC「{query}」：命中 {total} 篇，列出 {len(papers)} 篇。"]
    for index, paper in enumerate(papers, 1):
        head = f"  [{index}]"
        if paper.pmid:
            head += f" PMID {paper.pmid}"
        if paper.pmcid:
            head += f" · {paper.pmcid}"
        if paper.year:
            head += f" · {paper.year}"
        if paper.journal:
            head += f" · {paper.journal}"
        if paper.cited_by:
            head += f" · 被引 {paper.cited_by}"
        if paper.source == "PPR":
            head += " · 预印本"
        lines.append(head)
        if paper.title:
            lines.append(f"      {paper.title}")
        if paper.authors:
            names = [name.strip() for name in paper.authors.split(",") if name.strip()]
            shown = "、".join(names[:3])
            lines.append(f"      作者：{shown}{' 等' if len(names) > 3 else ''}")
        if paper.abstract:
            lines.append(f"      摘要：{paper.abstract}")
        if paper.pmcid and paper.open_access:
            lines.append(f"      可读全文：read_fulltext article={paper.pmcid}")
        lines.append(f"      {paper.url}")
    if total > len(papers):
        lines.append(
            f"（命中 {total} 篇，这里只列了 {len(papers)} 篇；要更全请把检索式写细，或用 sort=relevance/cited/date 换排序）"
        )
    lines.append("以上是候选清单，**不含任何判断**：哪篇相关、可不可信，由读的人定。")
    return "\n".join(lines)


def _render_fulltext(view: Fulltext, *, section: str) -> str:
    head = view.pmcid
    for part in (view.journal, view.year, view.license):
        if part:
            head += f" · {part}"
    lines = [head]
    if view.title:
        lines.append(f"标题：{view.title}")
    if section and view.matched:
        lines.append(f"章节：{view.matched}（共 {view.total_chars:,} 字）")
        text = view.sections[0][1] if view.sections else ""
        lines.append(text or "（这一节没有正文段落，只有小节标题）")
        if view.total_chars > len(text):
            lines.append(
                f"（已截断：本节共 {view.total_chars:,} 字，这里给前 {len(text):,} 字；"
                "调大 max_chars 或换一个更小的 section）"
            )
    else:
        if view.abstract:
            lines.append(f"摘要：{view.abstract[:EPMC_ABSTRACT_CHARS]}")
        lines.append(
            f"可读章节（{len(view.sections)} 节，按标题子串取）："
            + "、".join(name for name, _text in view.sections)
            + ("……（只列前若干节）" if len(view.sections) >= MAX_SECTIONS else "")
        )
        lines.append(f'取某一节：read_fulltext article="{view.pmcid}" section="results"')
    lines.append("以上是原文摘录，**不含任何判断**：结论要自己读出来。")
    return "\n".join(lines)


class SearchEuropePmcTool(Tool):
    """检索 Europe PMC（含预印本），纯查询。"""

    spec = ToolSpec(
        name="search_europepmc",
        description=(
            "检索 Europe PMC（PubMed + PMC 全文 + 预印本 + 专利），返回**候选清单**："
            "PMID / PMCID / DOI、标题、期刊、年份、被引次数与摘要片段。\n\n"
            "**与 search_literature 的分工**：那条只查 PubMed 的期刊文献、只给摘要片段；"
            "这条多两样东西——**预印本**（检索式里加 `SRC:PPR`，覆盖 bioRxiv / medRxiv）"
            "与**全文入口**：带 `可读全文：read_fulltext article=PMC…` 那几行，可以直接读"
            "方法或结果那一节的正文，而不是只有 400 字摘要。\n\n"
            "检索语法：`TITLE_ABS:\"T790M\"`、`AUTH:\"Zhang Y\"`、`SRC:PPR`、"
            "`PUB_YEAR:2024`；直接写主题词也可以。排序用 sort。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Europe PMC 检索式，例如 EGFR T790M resistance，"
                        "或 SRC:PPR AND EGFR（只看预印本）"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多列多少条，上限 {EPMC_MAX_LIMIT}",
                },
                "sort": {
                    "type": "string",
                    "enum": list(SORT_ORDERS),
                    "description": "relevance（默认）/ cited（被引多的在前）/ date（新的在前）",
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
                output="query 不能为空。填检索式，例如 EGFR T790M resistance。",
                failed=True,
            )
        try:
            requested = int(arguments.get("limit") or EPMC_DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult(output="limit 应当是整数", failed=True)
        size = max(1, min(requested, EPMC_MAX_LIMIT))
        sort = str(arguments.get("sort") or "relevance").strip().lower()
        if sort not in SORT_ORDERS:
            return ToolResult(
                output=f"sort 只认 {'/'.join(SORT_ORDERS)}，收到 {sort!r}",
                failed=True,
            )

        try:
            papers, total = search_europepmc(query, limit=size, sort=sort)
        except LiteratureError as error:
            return ToolResult(output=f"这次没搜成（Europe PMC「{query}」）：{error}", failed=True)

        if not papers:
            return ToolResult(
                output=f"这次没搜成（Europe PMC「{query}」）：命中 0 篇，没有可列的文献。",
                meta={"count": 0, "total": total, "ids": [], "papers": []},
            )
        return ToolResult(
            output=_render_papers(papers, total=total, query=query),
            meta={
                "count": len(papers),
                "total": total,
                "truncated": total > len(papers),
                "ids": [paper.ident for paper in papers],
                "pmcids": [paper.pmcid for paper in papers if paper.pmcid],
                "papers": [paper.as_dict() for paper in papers],
            },
        )


class ReadFulltextTool(Tool):
    """读一篇开放获取论文的某一节正文。纯查询，不产生产物。"""

    spec = ToolSpec(
        name="read_fulltext",
        description=(
            "读一篇**开放获取**论文的正文（Europe PMC 全文 XML），不是摘要片段。"
            "不给 section 时返回摘要与**可读章节清单**；给了就返回那一节，按标题子串匹配"
            "（results / methods / discussion / abstract 都能匹配到带编号的标题）。\n\n"
            "**只有 isOpenAccess=Y 的条目有全文**：预印本与订阅文献通常没有，这时会明说"
            "而不是给一段空文本。返回内容按 max_chars 截断，并在末尾写清本节原本多长。\n\n"
            "入口：search_europepmc 的每一行都带着 PMCID（`PMC12545111`），也接受 PMID"
            "与 DOI。想知道「这组实验到底怎么做的、数字是多少」用它；只想知道「有没有这类"
            "文献」用 search_europepmc 就够。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "article": {
                    "type": "string",
                    "description": "PMCID（PMC12545111）、PMID（41142240）或 DOI",
                },
                "section": {
                    "type": "string",
                    "description": (
                        "章节标题的子串，例如 results / methods / discussion；"
                        "留空则返回摘要与章节清单"
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        f"这一节最多返回多少字，默认 {FULLTEXT_DEFAULT_CHARS}，"
                        f"上限 {FULLTEXT_MAX_CHARS}"
                    ),
                },
            },
            "required": ["article"],
        },
    )
    primary_argument = "article"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        article = str(arguments.get("article") or "").strip()
        if not article:
            return ToolResult(
                output="article 不能为空。填 PMCID（PMC12545111）、PMID 或 DOI。",
                failed=True,
            )
        section = str(arguments.get("section") or "").strip()
        try:
            requested = int(arguments.get("max_chars") or FULLTEXT_DEFAULT_CHARS)
        except (TypeError, ValueError):
            return ToolResult(output="max_chars 应当是整数", failed=True)
        size = max(FULLTEXT_MIN_CHARS, min(requested, FULLTEXT_MAX_CHARS))

        try:
            view = read_fulltext(article, section=section, max_chars=size)
        except LiteratureError as error:
            return ToolResult(output=f"这次没读到全文（{article}）：{error}", failed=True)
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        return ToolResult(
            output=_render_fulltext(view, section=section),
            meta={
                "pmcid": view.pmcid,
                "title": view.title,
                "journal": view.journal,
                "year": view.year,
                "license": view.license,
                "section": view.matched,
                "chars": len(view.sections[0][1]) if view.sections else 0,
                "total_chars": view.total_chars,
                "truncated": view.total_chars > (len(view.sections[0][1]) if view.sections else 0),
                "sections": [name for name, _text in view.sections],
            },
        )


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [SearchLiteratureTool(), SearchEuropePmcTool(), ReadFulltextTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
