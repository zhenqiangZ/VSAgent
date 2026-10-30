"""把一份文件变成「带页码、带章节」的段落块。

这一层只做三件事：**抽取文字、认出结构、切成段落**。不切块（`core/chunker.py` 的活）、
不碰索引（`core/index.py` 的活）、不写盘。分开是因为它们的失败模式完全不同：
抽不出文字是输入的问题，切得不好是算法的问题，索引写坏是存储的问题。

结构有三档，`ParsedDocument.structure` 记下**实际用了哪一档**：

| 档 | 来源 | 什么时候用 |
|---|---|---|
| `outline` | PDF 书签（带页码） | 有书签就有页码，引用能落到 `§3.2 p.7` |
| `heading` | 行文本里的编号/白名单标题 | 没书签的论文；**启发式**，认错了就退回按页 |
| `page` | 一个 PDF 页就是一个块 | 上面两档都没认出来 |
| `markdown` | `#` 标题 / 空行段落 | 网页正文、`.md`、`.txt` |

**扫描件照实失败**：没有文本层时抛 `ParseError` 说「需要 OCR，本轮不支持」，绝不往
索引里塞空块——那种源在检索结果里看着正常、点进去一片空白，比直接报错难查得多。

`pypdf` 在函数体里导入（八条纪律第 8 条）：缺依赖时返回一句带 `pip install pypdf`
的话，而不是让整个应用起不来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Block",
    "ParseError",
    "ParsedDocument",
    "SUPPORTED_SUFFIXES",
    "label_skip",
    "parse_markdown",
    "parse_path",
    "unescape_markdown",
]

SUPPORTED_SUFFIXES = (".pdf", ".md", ".txt")
"""`import_documents` 收的扩展名。其余一律拒绝——猜格式猜错比拒绝难查。"""

# 不进检索的章节（正文仍留在 text.jsonl 里，read_source 读得到）。
# 常占论文 30～50% 篇幅、信息密度接近零。
SKIP_SECTIONS = (
    "references",
    "bibliography",
    "acknowledgments",
    "acknowledgements",
    "supplementary",
    "supplementary material",
    "参考文献",
    "致谢",
)

_HEADING_WORDS = (
    "abstract",
    "introduction",
    "background",
    "methods",
    "materials and methods",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
)
"""无编号也认的章节名。只收这几个：再宽就会把正文里的句子当标题。"""

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
"""`1 Introduction` / `2.1 In vitro assay` / `3.4.1 Statistics`。"""

_MIN_TEXT_CHARS = 50
"""整份 PDF 抽出不到这么多字，就当它没有文本层。"""

_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
"""`\\` + ASCII 标点。抓回来的网页 Markdown 常被整篇转义（`\\#`、`\\-`、`\\/`）。"""


class ParseError(Exception):
    """输入读不出可用文字。**这是输入的问题，重试无用。**"""


@dataclass(frozen=True)
class Block:
    """一段正文：来自哪一页、属于哪一节。

    `section` 是**章节路径**（`2 Methods / 2.1 In vitro assay`），不是一个标题：
    分块按路径聚，父子章节才不会混在一起。`page` 从 1 起，网页与 Markdown 恒为 0。
    """

    page: int
    section: str
    level: int
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    """一份解析完的文档。原件不在这里——它是 `corpus/doc/<id>/source.*`。

    `title` 只在**文件里真的写了标题**时才有值（PDF 元数据 `/Title`）。没有就留空，
    由调用方退回文件名：把「第一个章节名」当标题是错的——几乎每篇论文的第一节都叫
    `Introduction`，那样所有文献会拥有同一个标题。
    """

    pages: tuple[str, ...]
    blocks: tuple[Block, ...]
    structure: str
    warnings: tuple[str, ...] = ()
    title: str = ""

    @property
    def chars(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    def text_records(self) -> list[dict[str, object]]:
        """`text.jsonl` 的每一行：**按页存原文**，给 `read_source` 用。

        存原始页文本而不是块：块的切法是可变的（S4 还会调参数），而「第 7 页写了
        什么」不该随着切块参数变。页码是引用的一部分。
        """
        return [{"page": index + 1, "text": text} for index, text in enumerate(self.pages)]


def unescape_markdown(text: str) -> str:
    """把 `\\#` / `\\-` / `\\/` 这类转义还原。

    DeepSeek 的 Fetch 抓回来的一些站点（实测 `api-docs.deepseek.com`）整篇都是转义过
    的 Markdown：`\\#` 不是标题、`\\-` 不是列表。不还原的话结构切分认不出任何标题，
    整页退化成一块，而 `meta.structure` 还会写着 `markdown`——看上去一切正常。

    只认 `\\` + **ASCII 标点**：`\\alpha`、`C:\\path` 这类不动。一次解析里只做一遍。
    """
    return _ESCAPE.sub(r"\1", text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def parse_path(path: Path) -> ParsedDocument:
    """按扩展名分派。不支持的扩展名直接抛，**不猜格式**。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix in (".md", ".txt"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ParseError(f"读不出这个文件：{error}") from error
        except UnicodeDecodeError as error:
            raise ParseError(f"这个文件不是 UTF-8 文本：{error}") from error
        return parse_markdown(text, structure="markdown")
    raise ParseError(
        f"不支持 {suffix or '（无扩展名）'}：只收 {' / '.join(SUPPORTED_SUFFIXES)}。"
        "扫描版 PDF 需要 OCR，本轮不支持。"
    )


def parse_markdown(text: str, *, structure: str = "markdown", escaped: bool = False) -> ParsedDocument:
    """Markdown / 纯文本：`#` 标题分层，空行分段。

    `escaped=True` 时先反转义（网页抓回来的那一份）；本地 `.md` **不要**开这个开关，
    用户自己写的 `\\*`、`\\_` 是有意的。
    """
    if escaped:
        text = unescape_markdown(text)
    stack: list[tuple[int, str]] = []
    blocks: list[Block] = []
    buffer: list[str] = []
    section = ""
    level = 0
    in_fence = False

    def flush() -> None:
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            blocks.append(Block(page=0, section=section, level=level, text=body))

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            # 代码块整块留着：里面既有 `#` 也有空行，按普通段落切会切开一段代码。
            buffer.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            buffer.append(line)
            continue
        if stripped.startswith("#"):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped)
            if match:
                flush()
                depth = len(match.group(1))
                title = match.group(2).strip()
                stack = [(d, t) for d, t in stack if d < depth] + [(depth, title)]
                section = " / ".join(t for _, t in stack)
                level = depth
                continue
        if not stripped:
            flush()
            continue
        buffer.append(stripped)
    flush()

    if not blocks and not text.strip():
        raise ParseError("这个文件是空的")
    return ParsedDocument(
        pages=(text,),
        blocks=tuple(blocks),
        structure=structure,
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _parse_pdf(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - 主依赖，装漏了才走这里
        raise ParseError("读 PDF 需要 pypdf：pip install pypdf") from error

    try:
        reader = PdfReader(str(path))
    except Exception as error:  # noqa: BLE001 - pypdf 的异常类型太多，统一成一句话
        raise ParseError(f"这份 PDF 打不开（{type(error).__name__}: {error}）") from error

    pages: list[str] = []
    for page in reader.pages:
        try:
            # layout 模式保留列位置：两栏论文按默认模式抽会把左右栏交错在一起。
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:  # noqa: BLE001 - 单页抽不出文字不该让整份失败
            pages.append("")

    if sum(len(text.strip()) for text in pages) < _MIN_TEXT_CHARS:
        raise ParseError(
            "这份 PDF 抽不出文字（没有文本层，多半是扫描件）。需要 OCR，本轮不支持；"
            "请换成带文本层的版本，或先用 OCR 转一遍。"
        )

    outline = _outline_entries(reader)
    if outline:
        structure = "outline"
        headings = outline
    else:
        headings = _regex_headings(pages)
        structure = "heading" if headings else "page"

    blocks = _blocks_from_pages(pages, headings)
    return ParsedDocument(
        pages=tuple(pages),
        blocks=tuple(blocks),
        structure=structure,
        warnings=_warnings(pages),
        title=_pdf_title(reader),
    )


def _pdf_title(reader: object) -> str:
    """PDF 元数据里的标题。**排版软件填的垃圾要挡掉**：`Microsoft Word - draft.doc`
    这类值比文件名还差，不如留空让调用方用文件名。"""
    try:
        raw = getattr(reader.metadata, "title", "") or ""
    except Exception:  # noqa: BLE001 - 元数据坏了不影响正文
        return ""
    title = " ".join(str(raw).split()).strip()
    if len(title) < 3 or len(title) > 200:
        return ""
    lowered = title.lower()
    for bad in ("microsoft word", "untitled", ".doc", ".docx", ".pdf"):
        if bad in lowered:
            return ""
    return title


def _outline_entries(reader: object) -> list[tuple[int, int, str]]:
    """书签 → `(页码下标, 层级, 标题)`。**层级只由嵌套决定，不从标题里的编号猜。**

    没书签、书签指向的页不存在、标题为空——都只是少几条，不值得让整份解析失败。
    """
    try:
        raw = reader.outline  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return []
    found: list[tuple[int, int, str]] = []

    def walk(items: object, depth: int) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            try:
                index = reader.get_destination_page_number(item)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            if isinstance(index, int) and index >= 0:
                found.append((index, min(depth + 1, 6), title))

    walk(raw, 0)
    found.sort(key=lambda entry: (entry[0], entry[1]))
    return found


def _regex_headings(pages: list[str]) -> list[tuple[int, int, str]]:
    """没书签时按行猜标题。**宁少不多**：认错一个就把正文切碎。"""
    found: list[tuple[int, int, str]] = []
    for index, text in enumerate(pages):
        for line in text.splitlines():
            title = line.strip()
            level = _heading_level(title)
            if level:
                found.append((index, level, title))
    return found


def _heading_level(line: str) -> int:
    """一行是不是章节标题，是的话返回层级（1 起），不是返回 0。

    四条同时成立才算：长度 ≤ 80、不以句末标点结尾、不是整句（空格数少）、
    要么带编号要么命中白名单。
    """
    if not line or len(line) > 80:
        return 0
    if line[-1] in ".。,，;；:：":
        return 0
    numbered = _NUMBERED.match(line)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 6)
    lowered = line.lower()
    if lowered in _HEADING_WORDS:
        return 1
    return 0


def _blocks_from_pages(
    pages: list[str], headings: list[tuple[int, int, str]]
) -> list[Block]:
    """按页走一遍，把行收成段落，遇到标题就换节。

    书签只带页码，所以**每页的第一个标题在页首就生效**（论文的节通常从页首开始）。
    同一页里后面的标题再按行匹配——匹配不上也不影响：正文仍然落在当前节里。
    """
    by_page: dict[int, list[tuple[int, str]]] = {}
    for index, level, title in headings:
        by_page.setdefault(index, []).append((level, title))

    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []

    def push(level: int, title: str) -> str:
        nonlocal stack
        stack = [(depth, text) for depth, text in stack if depth < level] + [(level, title)]
        return " / ".join(text for _, text in stack)

    for index, text in enumerate(pages):
        page_headings = list(by_page.get(index, []))
        section = " / ".join(t for _, t in stack)
        level = stack[-1][0] if stack else 0
        remaining = list(page_headings)
        # 页首套用的那个标题，它的**那一行**还在正文里：不挡掉的话，
        # 标题会被当成正文写进块里，读起来像正文忽然重复了一遍小节名。
        pending: list[str] = []
        if remaining:
            first_level, first_title = remaining.pop(0)
            section = push(first_level, first_title)
            level = first_level
            pending.append(first_title)

        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            body = "\n".join(buffer).strip()
            buffer.clear()
            if body:
                blocks.append(Block(page=index + 1, section=section, level=level, text=body))

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                flush()
                continue
            consumed = next((title for title in pending if _same_heading(stripped, title)), None)
            if consumed is not None:
                pending.remove(consumed)
                continue
            matched = next(
                (item for item in remaining if _same_heading(stripped, item[1])), None
            )
            if matched is not None:
                flush()
                remaining.remove(matched)
                section = push(matched[0], matched[1])
                level = matched[0]
                continue
            buffer.append(stripped)
        flush()

        # 这一页剩下的标题正文里没认出来（多栏、断行、页眉页脚混排）：
        # 只记一条提醒，不在页尾补一个空块——那会造出一个没有正文的章节。
    return blocks


def _same_heading(line: str, title: str) -> bool:
    """行与标题是不是同一句话。容忍断行与多余空格，**不做模糊匹配**。"""
    left = " ".join(line.split()).lower()
    right = " ".join(title.split()).lower()
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def _warnings(pages: list[str]) -> tuple[str, ...]:
    """能看出来的排版问题。**只是提醒**，不影响引用，但让模型知道要打折。

    两栏检测是启发式：两栏论文抽出来的行大多很短（一栏只有半页宽），
    短行占比高就提示一句。宁可误报一句，也不要让模型拿串栏的文字当原文引用。
    """
    lines = [line.strip() for text in pages for line in text.splitlines() if line.strip()]
    if len(lines) < 20:
        return ()
    short = sum(1 for line in lines if len(line) < 45)
    if short / len(lines) > 0.75:
        return ("行普遍很短，可能是两栏排版：抽出来的文字有串栏风险，引用前先核对页码",)
    return ()


def label_skip(section: str) -> bool:
    """这一节是不是「不进检索」的那几类（按路径最后一段判）。

    只看最后一段：`2 Methods / References` 与单独的 `References` 一样跳过，
    而 `References in the methods`（正文小节）不会被误杀。
    """
    leaf = section.split(" / ")[-1].strip().lower()
    leaf = re.sub(r"^[\d.\s]+", "", leaf)
    return leaf in SKIP_SECTIONS
