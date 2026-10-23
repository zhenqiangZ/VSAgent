"""本地语料库的目录与落盘（S0：只落原件，不建索引）。

语料放在**仓根**的 `corpus/` 下，按代码位置定位而不是启动目录——与 `runs/` 同一种
做法。从别的目录启动应用时，语料仍然是同一份；换成启动目录定位的话，用户会看到
「昨天存的文献今天不见了」，而盘上其实还在。

三条从方案稿（`docs/knowledge-plan.md`）带过来的规矩，落在这里：

1. **真相在原件。** `content.md` / `meta.json` 是本体，索引是派生物（S1 起才有）。
   删掉索引能重建，原件不可变。
2. **写入只有一个写者。** 这里的所有写入口只被 agent/TUI 调用；网页端在 S2 之后
   只往 `inbox/` 落文件与写 `labels.json`，从不打开索引。
3. **原子写。** 先写同目录临时文件、`fsync`、再换名——网页端每个请求都重新读盘，
   读到半截 JSON 就是整页空白。

本模块不导入 `domain/` 里的任何东西：`core/` 是被 `domain/` 依赖的那一层，
反过来 import 会把依赖方向倒过来。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "CONTENT_FILE",
    "FILE_PREFIX",
    "LABELS_FILE",
    "META_FILE",
    "TEXT_FILE",
    "WebPage",
    "atomic_write_bytes",
    "atomic_write_text",
    "copy_file_atomic",
    "corpus_root",
    "doc_dir",
    "normalize_url",
    "read_json",
    "read_web_page",
    "source_dir",
    "source_ref",
    "web_dir",
    "web_id",
    "write_json",
    "write_web_page",
]

CONTENT_FILE = "content.md"
META_FILE = "meta.json"
LABELS_FILE = "labels.json"
TEXT_FILE = "text.jsonl"
"""文献的逐页原文：每行 `{"page": 7, "text": "…"}`。`read_source` 读它。"""

FILE_PREFIX = "source"
"""原件的文件名前缀：`source.pdf` / `source.md` / `source.txt`。原件不可变。"""

SOURCE_ID = re.compile(r"^[0-9a-f]{10}$")
"""源 id 的形状。10 位十六进制，10⁴ 份文档量级下碰撞 <0.005%。"""

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class WebPage:
    """一份本地网页源：目录、元数据与正文。

    `meta` 是那次 upsert 写下的字典，原样读出——它只给工具决定「本地有没有全文」
    这类问题用，不做二次解释。
    """

    source_id: str
    directory: Path
    meta: dict[str, Any]
    content: str

    @property
    def ref(self) -> str:
        """引用号：`web#ab12cd34ef`。**只能从工具返回里来**，不接受模型自己编。"""
        return source_ref(self.source_id)


def corpus_root() -> Path:
    """仓根下的 `corpus/`。

    `core/corpus.py` 位于 `<repo>/opencode_tui/core/`，`parents[2]` 就是仓根。
    与 `domain/export.py:_repo_root()` 指向同一个地方，只是层级不同。
    """
    return Path(__file__).resolve().parents[2] / "corpus"


def web_dir(source_id: str) -> Path:
    """一份网页源的目录。**只接受 10 位 id**，不拼路径——拼路径就等于收路径。"""
    if not SOURCE_ID.match(source_id or ""):
        raise ValueError(f"不是合法的源 id：{source_id!r}（应为 10 位十六进制）")
    return corpus_root() / "web" / source_id


def doc_dir(source_id: str) -> Path:
    """一份文献源的目录。与 `web_dir` 同一条规矩：只收 id，不收路径。"""
    if not SOURCE_ID.match(source_id or ""):
        raise ValueError(f"不是合法的源 id：{source_id!r}（应为 10 位十六进制）")
    return corpus_root() / "doc" / source_id


def source_dir(kind: str, source_id: str) -> Path:
    """按类型取源目录。`kind` 只认 `doc` / `web`。"""
    if kind == "web":
        return web_dir(source_id)
    if kind == "doc":
        return doc_dir(source_id)
    raise ValueError(f"不认识的源类型：{kind!r}")


def source_ref(source_id: str) -> str:
    """`ab12cd34ef` → `web#ab12cd34ef`。"""
    return f"web#{source_id}"


def normalize_url(url: str) -> str:
    """规范化 URL：**它决定 web 源的身份**。

    同一篇文章常有好几种写法——带不带 fragment、host 大小写、显式写默认端口。
    不归一化的话，同一页会变成三个源，检索时三份重复内容互相挤占。

    - scheme / host 转小写；fragment 丢掉（它不指向不同的内容）
    - query **保留**：`?id=123` 与 `?id=456` 常常是两篇文章
    - 默认端口去掉，非默认端口保留
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("只接受 http / https 的网页地址")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("地址里没有 host")
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def web_id(url: str) -> str:
    """URL → 源 id。规范化之后取哈希，同一页永远同一个 id。"""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:10]


def read_web_page(source_id: str) -> WebPage | None:
    """读一份本地网页源。**`meta.json` 不在就当作没有**——它是提交标记。

    正文先写、元数据后写，所以只有元数据存在时正文才可信；反过来先写元数据的话，
    中途中断会留下一份「元数据说有、正文是空的」源，检索结果看着正常、内容全空。
    """
    directory = web_dir(source_id)
    try:
        meta = json.loads((directory / META_FILE).read_text(encoding="utf-8"))
        content = (directory / CONTENT_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return WebPage(
        source_id=source_id,
        directory=directory,
        meta=meta if isinstance(meta, dict) else {},
        content=content,
    )


def write_web_page(*, source_id: str, content: str, meta: dict[str, Any]) -> Path:
    """写一份网页源，返回目录。

    正文先写、元数据后写（见 `read_web_page`）。**`labels.json` 一概不碰**：
    它是网页端唯一被允许改的文件，这里覆盖掉就等于把用户改过的标题冲了——
    摘要升成全文本（`depth: summary → full`）时也会走到这条路。
    """
    directory = web_dir(source_id)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / CONTENT_FILE, content)
    atomic_write_text(directory / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2))
    return directory


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 → `fsync` → `os.replace`。中途失败不留半截目标文件。"""
    atomic_write_bytes(Path(path), text.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """写入字节。**上传的 PDF 走这一条**——按文本写会把二进制毁掉。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def copy_file_atomic(source: Path, target: Path) -> None:
    """把原件复制进语料，**逐字节**，同样先写临时文件再换名。

    不直接引用用户那个文件：它可能待会儿就被覆盖或删掉，而语料要能长期引用。
    复制走二进制，PDF 里任何一次按文本解码都会毁掉它。
    """
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with open(source, "rb") as reader, os.fdopen(handle_fd, "wb") as writer:
            while True:
                piece = reader.read(1024 * 1024)
                if not piece:
                    break
                writer.write(piece)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any]:
    """读一份 JSON 对象。**读不出就抛**，调用方自己决定这是不是致命。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 里应当是一个 JSON 对象")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写一份 JSON（`indent=2`、中文不转义）。"""
    atomic_write_text(Path(path), json.dumps(payload, ensure_ascii=False, indent=2))
