"""语料库的只读取数：列表、详情、切块、统计。

**只读**。索引以 `mode=ro` 打开，原件与 `labels.json` 只读不写；写入口在
`routes.py`（投递、改名、删除）与 M2 的标签接口里，它们都按「网页进程能写什么」
那条界线分过。

列表里的块数与向量数来自索引，不从 `meta.json` 读：`meta.json` 记的是入库那一刻
的数，之后重新切块会变，而页面上要回答的是「检索现在看到多少块」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ... import corpus, corpus_config, index
from ...paths import config_home

CHUNK_PAGE = 40
"""切块列表一页多少块。一个 200 页的 PDF 可能有几百块，整包发出去是几 MB 的
JSON；40 块约一屏半，翻页也不费事。"""

CHUNK_PAGE_MAX = 100
"""一页最多给多少块。再大就回到「一次几 MB」，而页面也读不完。"""

LIBRARY_PAGE_MAX = 500
"""列表一页最多给多少份源。默认不分页（`limit=None`），传了才切。"""

UPLOAD_SUFFIXES = (".pdf", ".md", ".txt")
"""投递口收的扩展名。与 `parse.SUPPORTED_SUFFIXES` 同口径。"""


def archive_directory() -> Path:
    """查询归档的目录。真相在 `domain/rsearch.search_dir()`，这里只读它的位置。

    从 `core/web` 导入 `core/agent/domain` 会把依赖方向倒过来（网页端不该认识
    领域工具），所以这一处按同一个约定拼路径，注释指回那个函数。
    """
    return config_home() / "search"


def library_payload(
    root: Path,
    *,
    query: str = "",
    kind: str = "",
    status: str = "",
    sort: str = "added",
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """语料库列表 + 待入库数 + 占用 + 开关现状。**只读，不动任何文件。**

    筛选与分页都是可选的：一个参数都不给时返回全部，与 M0 之前的行为一致。
    `stats` 描述的是**整库**，不随筛选变化——顶部那行读数要回答「现在有多少
    语料」，不是「这一页有多少」；`total` 是筛选之后的条数，页面用它写「筛出 N 份」。
    """
    items = _collect_items(root)
    matched = _filter_items(items, query=query, kind=kind, status=status)
    matched = _sort_items(matched, sort)

    total = len(matched)
    if limit is not None:
        items_out = matched[offset : offset + limit]
    else:
        items_out = matched[offset:]

    inbox = _inbox_files(root)
    settings = corpus_config.load(root.parent)
    return {
        "items": items_out,
        "total": total,
        "offset": offset,
        "limit": limit,
        "inbox": {"count": len(inbox), "names": [path.name for path in inbox[:20]]},
        "archive": {"count": len(list(archive_directory().glob("web-*.json")))},
        "stats": {
            "sources": len(items),
            "chunks": sum(item["chunks"] for item in items),
            "bytes": sum(item["size"] for item in items),
        },
        "config": {
            "indexWeb": settings.index_web,
            "embed": settings.embed,
            "embedModel": settings.embed_model,
            # 浏览器里只显示，不能改：改配置是写操作（方案稿 §10）。
            "source": settings.source,
        },
    }


def corpus_stats(root: Path) -> dict:
    """`/api/corpus/stats`：整库读数。

    `badMeta` 是 `meta.json` 读不出来的源数。它们照样列在列表里，但块数一定是 0
    ——入库那一步就没走完。把这个数单独给出来，页面上「为什么这几份没有块」
    才有答案。
    """
    items = _collect_items(root)
    chunks = sum(item["chunks"] for item in items)
    vectors = sum(item["vectors"] for item in items)
    by_kind = {"doc": 0, "web": 0}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {
        "sources": len(items),
        "byKind": by_kind,
        "chunks": chunks,
        "vectors": vectors,
        "vectorCoverage": round(vectors / chunks, 4) if chunks else 0.0,
        "badMeta": sum(1 for item in items if item["status"] != "ok"),
        "bytes": sum(item["size"] for item in items),
        "inbox": len(_inbox_files(root)),
    }


def source_detail(root: Path, kind: str, source_id: str) -> dict | None:
    """一份源的详情：`meta.json`、`labels.json`、索引里的块数与向量数、目录里的文件。

    `meta.json` 不在时返回 None（调用方回 404）。列表里「元数据缺失」那一类仍然
    会出现在列表里，但详情打不开——那一份本来就没走完入库。
    """
    directory = root / kind / source_id
    meta_path = directory / corpus.META_FILE
    if not meta_path.is_file():
        return None
    try:
        meta = corpus.read_json(meta_path)
    except (OSError, ValueError):
        meta = {}
    try:
        labels = corpus.read_json(directory / corpus.LABELS_FILE)
    except (OSError, ValueError):
        labels = {}

    chunks, vectors = _source_counts(root, f"{kind}:{source_id}")
    display = str(labels.get("display_title") or "").strip()
    files: list[dict[str, Any]] = []
    for entry in sorted(directory.iterdir()):
        try:
            if entry.is_file():
                files.append({"name": entry.name, "bytes": entry.stat().st_size})
        except OSError:
            continue
    return {
        "id": f"{kind}:{source_id}",
        "kind": kind,
        "ref": f"{kind}#{source_id}",
        "title": display or str(meta.get("title") or source_id),
        "displayTitle": display,
        "rawTitle": str(meta.get("title") or ""),
        "locator": str(meta.get("url") or meta.get("file") or ""),
        "status": "ok" if meta else "bad-meta",
        "meta": meta,
        "labels": labels,
        "chunks": chunks,
        "vectors": vectors,
        "files": files,
    }


def chunks_payload(
    root: Path,
    kind: str,
    source_id: str,
    *,
    offset: int = 0,
    limit: int = CHUNK_PAGE,
    query: str = "",
) -> dict:
    """一份源的切块。**只读**：索引以 `mode=ro` 打开，取不到就给空表。

    给的是**索引里那一份**，不是按原件重新切一遍：用户要看的就是「检索看到的
    东西」。重新切会得到另一份（哪怕这次逐字相同），而两边的差异恰恰是最该被
    看见的——重切出来的那份不会解释为什么检索命中了别的段落。

    `query` 是块内关键词过滤，只影响这一页的取数与 `total`；它不改变排序。
    """
    source = f"{kind}:{source_id}"
    conn = index.open_readonly(root)
    if conn is None:
        return {
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "reason": "索引还没建",
        }
    clause = "source_id = ?"
    params: list[Any] = [source]
    term = query.strip()
    if term:
        clause += " AND text LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(term)}%")
    try:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM chunks WHERE {clause}", params).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT chunk_no, section, level, page, page_end, chars, text FROM chunks "
            f"WHERE {clause} ORDER BY chunk_no LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    except Exception:  # noqa: BLE001 - 库坏了不该让整页 500
        return {
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "reason": "索引读不出来",
        }
    finally:
        conn.close()
    return {
        "items": [
            {
                "no": int(row["chunk_no"]),
                "section": str(row["section"] or ""),
                "level": int(row["level"]),
                "page": int(row["page"]),
                "pageEnd": int(row["page_end"]),
                "chars": int(row["chars"]),
                "text": str(row["text"]),
            }
            for row in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _escape_like(term: str) -> str:
    """把 LIKE 的通配符转义掉：用户搜 `50%` 时 `%` 是字面量。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _collect_items(root: Path) -> list[dict]:
    """全部源，按加入时间倒序，块数与向量数一次补齐。"""
    items: list[dict] = []
    for kind in ("doc", "web"):
        base = root / kind
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            item = _library_item(kind, entry)
            if item is not None:
                items.append(item)
    items.sort(key=lambda item: (-float(item["addedAt"] or 0), item["id"]))

    counts = _index_counts(root)
    for item in items:
        item["chunks"], item["vectors"] = counts.get(item["id"], (0, 0))
    return items


def _filter_items(
    items: list[dict], *, query: str = "", kind: str = "", status: str = ""
) -> list[dict]:
    """按标题、地址、标签找；类型与状态是精确匹配。"""
    picked = items
    if kind:
        picked = [item for item in picked if item["kind"] == kind]
    if status:
        picked = [item for item in picked if item["status"] == status]
    term = query.strip().casefold()
    if not term:
        return picked
    found: list[dict] = []
    for item in picked:
        haystack = " ".join(
            [
                str(item["title"]),
                str(item["rawTitle"]),
                str(item["locator"]),
                " ".join(item.get("tags") or []),
            ]
        ).casefold()
        if term in haystack:
            found.append(item)
    return found


def _sort_items(items: list[dict], sort: str) -> list[dict]:
    """`added`（默认，新的在前）/ `title` / `chunks` / `size`。认不出的按默认排。"""
    if sort == "title":
        return sorted(items, key=lambda item: (str(item["title"]), item["id"]))
    if sort == "chunks":
        return sorted(items, key=lambda item: (-int(item["chunks"]), item["id"]))
    if sort == "size":
        return sorted(items, key=lambda item: (-int(item["size"]), item["id"]))
    return sorted(items, key=lambda item: (-float(item["addedAt"] or 0), item["id"]))


def _library_item(kind: str, entry: Path) -> dict | None:
    """一份源在列表里的一行。读 `meta.json` 与 `labels.json`，**不读原件内容**。"""
    meta_path = entry / corpus.META_FILE
    try:
        meta = corpus.read_json(meta_path)
    except (OSError, ValueError):
        # 半截 meta（写到一半）不该让整页 500：列出来并标成「元数据缺失」。
        meta = {}
    try:
        labels = corpus.read_json(entry / corpus.LABELS_FILE)
    except (OSError, ValueError):
        labels = {}

    source_id = f"{kind}:{entry.name}"
    display = str(labels.get("display_title") or "").strip()
    size = 0
    for item in entry.rglob("*"):
        try:
            if item.is_file():
                size += item.stat().st_size
        except OSError:
            continue
    added = meta.get("added_at")
    if not isinstance(added, (int, float)):
        try:
            added = entry.stat().st_mtime
        except OSError:
            added = 0.0
    return {
        "id": source_id,
        "kind": kind,
        "ref": f"{kind}#{entry.name}",
        "title": display or str(meta.get("title") or entry.name),
        "displayTitle": display,
        "rawTitle": str(meta.get("title") or ""),
        "locator": str(meta.get("url") or meta.get("file") or ""),
        "pages": int(meta.get("pages") or 0),
        "structure": str(meta.get("structure") or ""),
        "depth": str(meta.get("depth") or ""),
        "chars": int(meta.get("chars") or 0),
        "size": size,
        "addedAt": float(added),
        "status": "ok" if meta else "bad-meta",
        # 组织数据：标签、收藏、文件夹都在 `labels.json` 里，与显示名同一份文件。
        "tags": _string_list(labels.get("tags")),
        "favorite": bool(labels.get("favorite")),
        "folderId": str(labels.get("folderId") or ""),
        # `chunks` / `vectors` 由 `_collect_items` 从只读索引里补上。
        "chunks": 0,
        "vectors": 0,
    }


def _string_list(raw: Any) -> list[str]:
    """`labels.json` 里的数组字段，只留字符串。"""
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def _index_counts(root: Path) -> dict[str, tuple[int, int]]:
    """`source_id -> (块数, 向量数)`。索引不可读时给空表——列表照常显示。"""
    conn = index.open_readonly(root)
    if conn is None:
        return {}
    counts: dict[str, tuple[int, int]] = {}
    try:
        for row in conn.execute("SELECT source_id, COUNT(*) FROM chunks GROUP BY source_id"):
            counts[str(row[0])] = (int(row[1]), 0)
        for row in conn.execute(
            "SELECT c.source_id, COUNT(*) FROM vectors v JOIN chunks c ON c.id = v.chunk_id "
            "GROUP BY c.source_id"
        ):
            known = counts.get(str(row[0]), (0, 0))
            counts[str(row[0])] = (known[0], int(row[1]))
    except Exception:  # noqa: BLE001 - 读数缺了不该让页面挂掉
        return {}
    finally:
        conn.close()
    return counts


def _source_counts(root: Path, source_id: str) -> tuple[int, int]:
    """一份源的 `(块数, 向量数)`。详情页只要这一份，不扫全库。"""
    conn = index.open_readonly(root)
    if conn is None:
        return (0, 0)
    try:
        chunks = int(
            conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
        )
        vectors = int(
            conn.execute(
                "SELECT COUNT(*) FROM vectors v JOIN chunks c ON c.id = v.chunk_id "
                "WHERE c.source_id = ?",
                (source_id,),
            ).fetchone()[0]
        )
    except Exception:  # noqa: BLE001 - 库坏了不该让详情 500
        return (0, 0)
    finally:
        conn.close()
    return (chunks, vectors)


def _inbox_files(root: Path) -> list[Path]:
    inbox = root / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(
        item
        for item in inbox.iterdir()
        if item.is_file() and item.suffix.lower() in UPLOAD_SUFFIXES
    )
