"""把段落块切成检索单位（chunk）。

**chunk 永不跨节**：这是引用可信度的根。跨节的块在检索时会把「Methods 里的
一句话」配到「Discussion 的页码」，引用看上去有页码，实际指错了地方。

参数（方案稿 §6.2）与它们的理由：

| 参数 | 值 | 理由 |
|---|---|---|
| 目标块长 | 1200 字 | 有章节兜住上下文，块可以比纯滑窗大 |
| 浮动区间 | 900–1500 | 宁可短一点，也要落在段落/句末边界上 |
| 硬上限 | 2000 | 超过就按句末切，绝不切在句中 |
| 合并下限 | 300 | 同节过短的块合并，避免「一句话一块」 |
| 重叠 | 上一块最后一整句，≤180 字 | 固定字数会从词中间开始，重叠部分读起来是乱的 |

**不切表**：表格整块保留，超长按行切——切在行中间等于把表毁掉。

**确定性**：同一份文件、同一套参数，切两遍得到的 `(section, page, chunk_no, text)`
逐字相同。判据里有一条就是「重建两次完全一致」——检索结果的可复现性建立在它上面。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby

from .parse import Block, label_skip

__all__ = [
    "HARD_MAX",
    "MERGE_MIN",
    "OVERLAP_MAX",
    "Chunk",
    "chunk_blocks",
]

TARGET = 1200
FLOAT_LOW = 900
FLOAT_HIGH = 1500
HARD_MAX = 2000
MERGE_MIN = 300
OVERLAP_MAX = 180

_SENTENCE_END = "。！？；.!?;"
"""中英文的句末。引号与括号跟在句末后面，切点落在它们之后（见 `_split_sentences`）。"""

_CLOSERS = "”’」』）)】\"'"


@dataclass(frozen=True)
class Chunk:
    """一个检索单位。`chunk_no` 在**同一个源**内从 1 递增。"""

    section: str
    level: int
    page: int
    page_end: int
    chunk_no: int
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


def chunk_blocks(blocks: list[Block]) -> list[Chunk]:
    """按节切块。**同一个源内 `chunk_no` 连续**，读起来与原文顺序一致。"""
    out: list[Chunk] = []
    for section, group in groupby(blocks, key=lambda block: block.section):
        if label_skip(section):
            # 参考文献之类不进检索：正文仍留在 text.jsonl 里，read_source 读得到。
            continue
        for chunk in _section_chunks(list(group), section):
            out.append(
                Chunk(
                    section=chunk.section,
                    level=chunk.level,
                    page=chunk.page,
                    page_end=chunk.page_end,
                    chunk_no=len(out) + 1,
                    text=chunk.text,
                )
            )
    return out


def _section_chunks(blocks: list[Block], section: str) -> list[Chunk]:
    """一节之内按目标长度切，末尾过短的并回上一块，最后加句首重叠。"""
    level = blocks[0].level if blocks else 0
    pieces: list[tuple[int, str]] = []
    for block in blocks:
        for piece in _split_long(block.text):
            pieces.append((block.page, piece))

    drafts: list[tuple[int, int, list[str]]] = []  # (起始页, 结束页, 段落)
    buffer: list[str] = []
    size = 0
    page_from = 0
    page_to = 0

    def flush() -> None:
        nonlocal buffer, size, page_from, page_to
        if not buffer:
            return
        drafts.append((page_from, page_to, buffer))
        buffer = []
        size = 0

    for page, text in pieces:
        length = len(text)
        joined = size + (2 if buffer else 0) + length
        # 落点优先：已经够长、再加这一段就超出浮动上限，就在这一段**之前**收尾。
        if buffer and joined > FLOAT_HIGH and size >= FLOAT_LOW:
            flush()
        elif buffer and joined > HARD_MAX:
            flush()
        if not buffer:
            page_from = page
        page_to = page
        size += (2 if buffer else 0) + length
        buffer.append(text)
    flush()

    # 末尾过短：并回上一块（仍受硬上限约束）。「一句话一块」既浪费预算又缺上下文。
    if len(drafts) >= 2:
        last = drafts[-1]
        tail_len = sum(len(item) for item in last[2]) + 2 * (len(last[2]) - 1)
        prev = drafts[-2]
        prev_len = sum(len(item) for item in prev[2]) + 2 * (len(prev[2]) - 1)
        if tail_len < MERGE_MIN and prev_len + tail_len + 2 <= HARD_MAX:
            prev[2].extend(last[2])
            drafts.pop()

    chunks: list[Chunk] = []
    for index, (start, end, paragraphs) in enumerate(drafts):
        text = "\n\n".join(paragraphs)
        if index and chunks:
            # 重叠只在同一节内加：跨节的重叠会把上一节的话带进新节，引用就错位了。
            overlap = _last_sentence(chunks[-1].text)
            if overlap and len(overlap) + 2 + len(text) <= HARD_MAX:
                text = f"{overlap}\n\n{text}"
        chunks.append(
            Chunk(
                section=section,
                level=level,
                page=start,
                page_end=end,
                chunk_no=0,  # 外层统一编号
                text=text,
            )
        )
    return chunks


def _split_long(text: str) -> list[str]:
    """一段过长就切开：**表格按行，正文按句**，都不切在词中间。"""
    if len(text) <= HARD_MAX:
        return [text]
    lines = text.splitlines()
    if _looks_like_table(lines):
        return _pack(lines, separator="\n", limit=HARD_MAX)
    sentences = _split_sentences(text)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > HARD_MAX:
            pieces.append(buffer)
            buffer = sentence
            continue
        buffer += sentence
        if len(buffer) >= HARD_MAX:
            # 单句本身就超长（没有句末标点的长段落）：只能在这里硬切，
            # **切在字符上**是最后的退路，不是常规路径。
            while len(buffer) > HARD_MAX:
                pieces.append(buffer[:HARD_MAX])
                buffer = buffer[HARD_MAX:]
    if buffer:
        pieces.append(buffer)
    return [piece for piece in pieces if piece.strip()]


def _looks_like_table(lines: list[str]) -> bool:
    """表格的形状：多数行以 `|` 开头，或者行内有 3 段以上连续空格（对齐的列）。"""
    if len(lines) < 3:
        return False
    pipes = sum(1 for line in lines if line.lstrip().startswith("|"))
    if pipes >= len(lines) * 0.6:
        return True
    columns = sum(1 for line in lines if "   " in line)
    return columns >= len(lines) * 0.6


def _pack(lines: list[str], *, separator: str, limit: int) -> list[str]:
    """按行装箱：尽量装满但不超过上限，**不拆行**。"""
    pieces: list[str] = []
    buffer: list[str] = []
    size = 0
    for line in lines:
        length = len(line) + len(separator)
        if buffer and size + length > limit:
            pieces.append(separator.join(buffer))
            buffer = []
            size = 0
        buffer.append(line)
        size += length
    if buffer:
        pieces.append(separator.join(buffer))
    return [piece for piece in pieces if piece.strip()]


def _split_sentences(text: str) -> list[str]:
    """按句末标点切，**标点与紧跟的引号留在前一句**。"""
    sentences: list[str] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if text[index] in _SENTENCE_END:
            end = index + 1
            while end < length and text[end] in _CLOSERS:
                end += 1
            # 英文句号后面必须跟空白或结尾，否则 `3.2`、`e.g.` 会被切开。
            if text[index] in ".!?" and end < length and not text[end].isspace():
                index += 1
                continue
            sentences.append(text[start:end])
            start = end
            index = end
            continue
        if text[index] == "\n":
            sentences.append(text[start : index + 1])
            start = index + 1
        index += 1
    if start < length:
        sentences.append(text[start:])
    return sentences


def _last_sentence(text: str) -> str:
    """取最后**一整句**，超过 `OVERLAP_MAX` 就不要了。

    取不到就返回空串：宁可没有重叠，也不要半句——半句开头读起来像乱码，
    而且它会出现在每一次检索结果里。
    """
    sentences = [item.strip() for item in _split_sentences(text) if item.strip()]
    if not sentences:
        return ""
    last = sentences[-1]
    if len(last) > OVERLAP_MAX:
        return ""
    return last
