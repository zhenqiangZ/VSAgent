"""检索索引：SQLite + FTS5 两路词法索引。**纯派生物，删掉能重建。**

三条设计：

1. **一个库、两张 FTS 表、一份正文。** 两张表都是 `external content`，正文只在
   `chunks` 里存一次——复制两份文本的话，改一处忘了另一处，检索结果与展示会不一致。
   `unicode61` 切英文词与标识符，`trigram` 管中文（默认分词器把 CJK 按单字切，
   「抑制剂」会变成三个单字，短语查询基本不可用）。
2. **章节也进索引。** 查 `Methods`、`3.2` 能命中标题，而不是只命中正文——
   「这一节讲什么」是检索里很常见的一类问题。
3. **向量先留位不填。** `vectors` 表在 S1 就建好（省一次迁移），但一行都不写；
   没有向量通道时检索只跑两路词法。

写入只有一个写者（TUI / agent）。网页端在 S2 只写 `inbox/` 与 `labels.json`，
从不打开这个库——允许两边同时写的话，`sources` 与 `chunks` 的级联删除会互相踩。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import corpus
from .chunker import Chunk, chunk_blocks
from .parse import ParsedDocument, parse_markdown, parse_path

__all__ = [
    "DB_NAME",
    "chunks_for",
    "connect",
    "ensure_schema",
    "lexical",
    "neighbors",
    "open_readonly",
    "reindex",
    "rows_by_id",
    "source_row",
    "stats",
    "upsert_source",
    "upsert_web_page",
]

DB_NAME = "library.db"
SCHEMA_VERSION = 1

# 每路词法召回的条数。30 足够 RRF 用好名次，再多只是把噪声往前推。
CHANNEL_LIMIT = 30

DDL = """
CREATE TABLE IF NOT EXISTS sources (
  id         TEXT PRIMARY KEY,          -- 'doc:ab12cd34ef' / 'web:ab12cd34ef'
  kind       TEXT NOT NULL,             -- doc | web
  title      TEXT NOT NULL,
  locator    TEXT NOT NULL,             -- 文件路径 / URL
  added_at   REAL NOT NULL,
  fetched_at REAL,
  depth      TEXT,                      -- doc: full；web: summary | full
  pages      INTEGER,
  structure  TEXT,                      -- outline | heading | page | markdown
  meta       TEXT NOT NULL              -- JSON：authors/site/authority/query/warning…
);

CREATE TABLE IF NOT EXISTS chunks (
  id        INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  section   TEXT NOT NULL DEFAULT '',
  level     INTEGER NOT NULL DEFAULT 0,
  page      INTEGER NOT NULL DEFAULT 0,
  page_end  INTEGER NOT NULL DEFAULT 0,
  chunk_no  INTEGER NOT NULL,
  text      TEXT NOT NULL,
  chars     INTEGER NOT NULL,
  UNIQUE (source_id, chunk_no)
);

CREATE INDEX IF NOT EXISTS chunks_by_source ON chunks (source_id, chunk_no);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  section, text, content='chunks', content_rowid='id', tokenize='unicode61');

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_cjk USING fts5(
  section, text, content='chunks', content_rowid='id', tokenize='trigram');

CREATE TABLE IF NOT EXISTS vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  model    TEXT NOT NULL,
  dim      INTEGER NOT NULL,
  vec      BLOB NOT NULL
);

-- 派生物的版本号。向量表的每次写入都把它 +1，进程内的矩阵缓存按它失效：
-- 只按「行数」做键是不够的——`reindex` 会删掉再插入，行数不变而 chunk id 已经
-- 换了人，缓存里的 id 会指到别的块上，检索结果看着正常、内容却是错的。
CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 版本号由**触发器**维护，不靠调用方记得调一次：级联删除（删源、整源重建）
-- 同样会清掉向量，而那条路径根本没有人会去想到「顺便 bump 一下」。
CREATE TRIGGER IF NOT EXISTS vectors_ai AFTER INSERT ON vectors BEGIN
  INSERT INTO kv (key, value) VALUES ('vectors', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS vectors_ad AFTER DELETE ON vectors BEGIN
  INSERT INTO kv (key, value) VALUES ('vectors', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS vectors_au AFTER UPDATE ON vectors BEGIN
  INSERT INTO kv (key, value) VALUES ('vectors', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts (rowid, section, text) VALUES (new.id, new.section, new.text);
  INSERT INTO chunks_cjk (rowid, section, text) VALUES (new.id, new.section, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts (chunks_fts, rowid, section, text)
    VALUES ('delete', old.id, old.section, old.text);
  INSERT INTO chunks_cjk (chunks_cjk, rowid, section, text)
    VALUES ('delete', old.id, old.section, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts (chunks_fts, rowid, section, text)
    VALUES ('delete', old.id, old.section, old.text);
  INSERT INTO chunks_cjk (chunks_cjk, rowid, section, text)
    VALUES ('delete', old.id, old.section, old.text);
  INSERT INTO chunks_fts (rowid, section, text) VALUES (new.id, new.section, new.text);
  INSERT INTO chunks_cjk (rowid, section, text) VALUES (new.id, new.section, new.text);
END;
"""


def connect(root: Path | None = None) -> sqlite3.Connection:
    """打开（必要时创建）索引库。调用方负责 `close()`。"""
    base = Path(root) if root is not None else corpus.corpus_root()
    path = base / ".index" / DB_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # 外键必须显式打开：SQLite 默认不执行 ON DELETE CASCADE，删源之后块会留一堆孤儿。
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def open_readonly(root: Path | None = None) -> sqlite3.Connection | None:
    """只读打开索引，**不建表、不写任何东西**。

    网页端要显示「多少个块、向量覆盖多少」，这些读数只在索引里。让它读一下就
    把 `chunks` 记进 `meta.json` 的话，等于在原件旁边维护第二份指数——两份迟早
    对不上，而页面上看不出哪一份是对的。

    **写者仍然只有一个**（TUI / agent）：这里用 `mode=ro`，SQLite 的连接层面就
    拒绝了写。库还不存在、或正被写锁住时返回 None，页面照常显示语料列表，只是
    少了这几项读数——没有一个统计数字值得让整页 500。
    """
    base = Path(root) if root is not None else corpus.corpus_root()
    path = base / ".index" / DB_NAME
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sources LIMIT 1").fetchall()
    except sqlite3.Error:
        return None
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表并记版本。版本只增不减——换结构时先加列再迁数据，别指望用户删库。"""
    conn.executescript(DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def upsert_source(conn: sqlite3.Connection, source: dict[str, object], chunks: list[Chunk]) -> int:
    """整源替换：先删旧块，再写新块。返回写进去的块数。

    整源替换而不是逐块 diff：分块参数一改，块边界全变，diff 出来的「改了几块」
    没有意义，还容易留下半新半旧的块。
    """
    with conn:
        conn.execute(
            """
            INSERT INTO sources
              (id, kind, title, locator, added_at, fetched_at, depth, pages, structure, meta)
            VALUES (:id, :kind, :title, :locator, :added_at, :fetched_at, :depth, :pages,
                    :structure, :meta)
            ON CONFLICT(id) DO UPDATE SET
              kind = excluded.kind, title = excluded.title, locator = excluded.locator,
              added_at = excluded.added_at, fetched_at = excluded.fetched_at,
              depth = excluded.depth, pages = excluded.pages,
              structure = excluded.structure, meta = excluded.meta
            """,
            source,
        )
        conn.execute("DELETE FROM chunks WHERE source_id = ?", (source["id"],))
        conn.executemany(
            """
            INSERT INTO chunks (source_id, section, level, page, page_end, chunk_no, text, chars)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    source["id"],
                    chunk.section,
                    chunk.level,
                    chunk.page,
                    chunk.page_end,
                    chunk.chunk_no,
                    chunk.text,
                    chunk.chars,
                )
                for chunk in chunks
            ],
        )
    return len(chunks)


def upsert_web_page(
    conn: sqlite3.Connection, source_id: str, content: str, meta: dict[str, object]
) -> int:
    """把刚落盘的网页同步进索引。`search_web` / `fetch_webpage` 存完立刻调它。

    与 `reindex` 走同一个 `chunks_for`：两条路各写一份解析参数的话，回填出来的块
    与当场入库的块会不一样，而「重建两次一致」的判据正好抓不到这种漂移。
    """
    document = parse_markdown(content, structure="markdown", escaped=True)
    chunks = chunk_blocks(list(document.blocks))
    directory = corpus.web_dir(source_id.split(":", 1)[1])
    return upsert_source(conn, source_row(source_id, "web", directory, meta), chunks)


def drop_source(conn: sqlite3.Connection, source_id: str) -> None:
    """删掉一个源与它的块（级联）。"""
    with conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def _fts_query(query: str) -> str:
    """把自然语言查询变成 FTS5 的 OR 短语串。

    逐词加引号是必需的：`-`、`:`、`*`、`(` 在 FTS5 里都是语法字符，用户搜
    `T790M-resistant` 时原样传进去会得到一个语法错误，而错误文本对用户毫无意义。
    CJK 连续段整体作为一个短语——`unicode61` 会把它切成单字，短语匹配才等价于
    「这几个字连在一起」。
    """
    terms = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", query or "")
    return " OR ".join(f'"{term}"' for term in terms)


def lexical(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = CHANNEL_LIMIT,
    only: tuple[str, ...] | None = None,
) -> dict[str, list[int]]:
    """两路词法召回的 chunk id（各自按相关度排序）。

    `trigram` 那一路要求查询至少 3 个字符：两个字的查询在 trigram 里没有 token，
    跑了也是空结果，白白多一次全表扫。

    `only` 给评测用：`("fts",)` 就是「词法单路」，与线上两路的结果分开量。
    """
    expression = _fts_query(query)
    if not expression:
        return {}
    found: dict[str, list[int]] = {}
    for name, table in (("fts", "chunks_fts"), ("cjk", "chunks_cjk")):
        if only is not None and name not in only:
            continue
        if name == "cjk" and len(query.replace(" ", "")) < 3:
            continue
        try:
            rows = conn.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? "
                f"ORDER BY bm25({table}) LIMIT ?",
                (expression, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # 语法被拒（极少数构造）时这一路不参与，另一路照跑——不能整次检索失败。
            continue
        found[name] = [int(row[0]) for row in rows]
    return found


_COLUMNS = (
    "c.id, c.source_id, c.section, c.level, c.page, c.page_end, c.chunk_no, c.text, c.chars, "
    "s.kind, s.title, s.locator, s.depth, s.meta"
)


def rows_by_id(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    """按 id 取块连同它的源信息。一次取完，不做 N+1。"""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM chunks c JOIN sources s ON s.id = c.source_id "
        f"WHERE c.id IN ({marks})",
        list(ids),
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def neighbors(conn: sqlite3.Connection, row: sqlite3.Row, *, span: int = 1) -> list[sqlite3.Row]:
    """命中块前后各 `span` 块，**同一源、同一节**。

    跨节的邻块不带：那会把上一节的末段接在下一节开头，引用页码也就跟着错了。
    """
    low = int(row["chunk_no"]) - span
    high = int(row["chunk_no"]) + span
    return conn.execute(
        f"SELECT {_COLUMNS} FROM chunks c JOIN sources s ON s.id = c.source_id "
        "WHERE c.source_id = ? AND c.section = ? AND c.chunk_no BETWEEN ? AND ? "
        "ORDER BY c.chunk_no",
        (row["source_id"], row["section"], low, high),
    ).fetchall()


def stats(conn: sqlite3.Connection, root: Path | None = None) -> dict[str, object]:
    """`/library` 顶部那行读数。**占用按整库文件大小算**，不是按正文估算。"""
    base = Path(root) if root is not None else corpus.corpus_root()
    path = base / ".index" / DB_NAME
    sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {
        "sources": int(sources),
        "chunks": int(chunks),
        "bytes": path.stat().st_size if path.is_file() else 0,
    }


def chunks_for(path: Path, *, escaped: bool = False) -> tuple[ParsedDocument, list[Chunk]]:
    """解析 + 切块。入库与重建走**同一条**路，两条路各写一份的话，重建出来的块
    跟当初写进去的会不一样，而「重建两次一致」的判据正好抓不到这种漂移。"""
    if escaped:
        document = parse_markdown(
            path.read_text(encoding="utf-8"), structure="markdown", escaped=True
        )
    else:
        document = parse_path(path)
    return document, chunk_blocks(list(document.blocks))


def reindex(root: Path | None = None) -> dict[str, int]:
    """按盘上的原件重建索引。**S1 的回填入口**（S0 落的 `corpus/web/` 靠它进库）。

    只处理「原件齐全」的源：目录没了就顺手把索引里那一行删掉——删除在语料侧是
    换名进 `.trash/`，索引侧不需要谁去通知，下一次重建自然对账。
    """
    base = Path(root) if root is not None else corpus.corpus_root()
    conn = connect(base)
    try:
        written = dropped = 0
        seen: set[str] = set()
        for kind in ("doc", "web"):
            directory = base / kind
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                source_id = f"{kind}:{entry.name}"
                try:
                    chunks = _chunks_of(entry, kind, meta)
                except Exception:  # noqa: BLE001 - 一份坏了不该让整批停下
                    continue
                if chunks is None:
                    continue
                upsert_source(conn, source_row(source_id, kind, entry, meta), chunks)
                seen.add(source_id)
                written += len(chunks)
        for row in conn.execute("SELECT id FROM sources").fetchall():
            if row["id"] not in seen:
                drop_source(conn, row["id"])
                dropped += 1
        return {"sources": len(seen), "chunks": written, "dropped": dropped}
    finally:
        conn.close()


def _chunks_of(entry: Path, kind: str, meta: dict[str, object]) -> list[Chunk] | None:
    """一份源目录 → 块。认不出原件时返回 None（跳过，不当成失败）。"""
    if kind == "web":
        content = entry / corpus.CONTENT_FILE
        if not content.is_file():
            return None
        return chunks_for(content, escaped=True)[1]
    name = str(meta.get("file") or "")
    if not name:
        return None
    original = entry / name
    if not original.is_file():
        return None
    return chunks_for(original)[1]


def source_row(
    source_id: str, kind: str, entry: Path, meta: dict[str, object]
) -> dict[str, object]:
    return {
        "id": source_id,
        "kind": kind,
        "title": str(meta.get("title") or entry.name),
        "locator": str(meta.get("url") or meta.get("file") or entry),
        "added_at": float(meta.get("added_at") or 0.0),
        "fetched_at": float(meta.get("fetched_at") or 0.0),
        "depth": str(meta.get("depth") or "full"),
        "pages": int(meta.get("pages") or 0),
        "structure": str(meta.get("structure") or ""),
        "meta": json.dumps(meta, ensure_ascii=False),
    }


def _embed_after_reindex() -> None:
    """重建之后补向量。**必须补**：`upsert_source` 会整源删块再插，级联把向量一起清了。"""
    from . import corpus_config, embed

    config = corpus_config.load()
    if not config.embed:
        print("语义通道已关闭（corpus.toml 的 embed），没有算向量。")
        return
    reason = embed.missing_reason(config.embed_model)
    if reason:
        print(reason)
        return
    conn = connect()
    try:
        result = embed.index_missing(conn, config.embed_model)
    finally:
        conn.close()
    print(
        f"向量：{result['embedded']} 块（{result['sources']} 个源，"
        f"{result['seconds']}s，失败 {result['failed']}）"
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "reindex":
        result = reindex()
        print(
            f"重建完成：{result['sources']} 个源、{result['chunks']} 个块"
            + (f"；清掉 {result['dropped']} 个已不存在的源" if result["dropped"] else "")
        )
        _embed_after_reindex()
    else:
        print("用法：python -m opencode_tui.core.index reindex")
        raise SystemExit(2)
