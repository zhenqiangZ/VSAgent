"""文献与记忆之间的路径映射。

记忆的 `refs` 里放的是**文件路径**（跨会话稳定），而索引里认的是 `doc:<哈希>`。
两边说的是同一篇文献，写法不一样：`corpus/doc/ab12cd34ef/source.md` 与
`doc:ab12cd34ef` 之间没有别处做过换算，这个模块补的就是这一段。

映射从 `corpus/doc/*/meta.json` 建：`file` 是复制进来的那一份（相对源目录），
`origin` / `locator` 是它原来的位置。三个都可能出现在记忆的引用里——模型可能
在读原件时记住它，也可能在入库之后读目录里的那一份。

进程内缓存按 `corpus/doc` 的目录 mtime 失效：新增或删除一份文献都会改这个值。
改写 `meta.json` 不改映射（同一个源目录里 `id` 不变），所以不必逐文件对账。
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import corpus

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[int, dict[str, dict], dict[str, str]]] = {}
"""语料根 → (doc 目录 mtime, 文档表, 路径表)。"""


def documents(corpus_root: Path) -> dict[str, dict]:
    """`doc:<哈希>` → `{id, sourceId, ref, title, directory, origin}`。"""
    return _scan(corpus_root)[0]


def path_index(corpus_root: Path) -> dict[str, str]:
    """规范化的绝对路径 → `doc:<哈希>`。"""
    return _scan(corpus_root)[1]


def source_of(corpus_root: Path, raw: str) -> str:
    """一条引用落在哪份文献上。认不出来（不是语料里的文件）返回空串。"""
    key = _normalize(raw)
    if not key:
        return ""
    return path_index(corpus_root).get(key, "")


def _scan(corpus_root: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """扫一遍源目录。结果按目录 mtime 缓存。"""
    base = Path(corpus_root) / "doc"
    try:
        stamp = base.stat().st_mtime_ns
    except OSError:
        return {}, {}
    key = str(base)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]

    found: dict[str, dict] = {}
    paths: dict[str, str] = {}
    for directory in sorted(base.iterdir()):
        if not directory.is_dir():
            continue
        try:
            meta = corpus.read_json(directory / corpus.META_FILE)
        except (OSError, ValueError):
            continue
        source_id = str(meta.get("id") or directory.name)
        handle = f"doc:{source_id}"
        found[handle] = {
            "id": source_id,
            "sourceId": handle,
            "ref": f"doc#{source_id}",
            "title": str(meta.get("title") or ""),
            "directory": str(directory),
            "origin": str(meta.get("origin") or ""),
        }
        name = str(meta.get("file") or "")
        if name:
            paths[_normalize(str(directory / name))] = handle
        for raw in (meta.get("origin"), meta.get("locator")):
            if raw:
                paths[_normalize(str(raw))] = handle

    with _LOCK:
        _CACHE[key] = (stamp, found, paths)
    return found, paths


def _normalize(raw: str) -> str:
    """把引用折成绝对路径。空串、`~` 展开、相对路径都按当前目录算。

    `resolve()` 不要求路径存在：记忆里记的文件可能已经被删了，那时仍要能
    认出它是哪一条引用，再由调用方标成 `missing`。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text
