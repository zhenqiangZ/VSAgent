from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable

DB_NAME = "vectors.db"

WRITE_LOCK = threading.RLock()

DDL = """
CREATE TABLE IF NOT EXISTS memory_vectors (
  memory_id TEXT PRIMARY KEY,
  text_hash TEXT NOT NULL,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  vec       BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS memory_vectors_ai AFTER INSERT ON memory_vectors BEGIN
  INSERT INTO kv (key, value) VALUES ('memory_vectors', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS memory_vectors_ad AFTER DELETE ON memory_vectors BEGIN
  INSERT INTO kv (key, value) VALUES ('memory_vectors', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS memory_vectors_au AFTER UPDATE ON memory_vectors BEGIN
  INSERT INTO kv (key, value) VALUES ('memory_vectors', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);
END;
"""


def path_for(root: Path) -> Path:
    """向量文件。**派生物**：删掉能由 `entries.json` 重建。"""
    return Path(root) / DB_NAME


def connect(root: Path) -> sqlite3.Connection:
    """打开（必要时创建）向量库。调用方负责 `close()`。"""
    target = path_for(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.commit()
    return conn


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sync(
    conn: sqlite3.Connection,
    records: Iterable[tuple[str, str]],
    model: str,
    encode: Callable[[list[str]], list[list[float]]],
) -> dict:
    """按内容指纹补齐向量，返回 `{"embedded": n, "skipped": n, "removed": n}`。

    没有 outbox：本地嵌入失败只有「模型没下载」与「运行库坏了」两种原因，
    重试多少次都一样；下次打开时按指纹再扫一遍就补上了，幂等。
    """
    rows = {
        str(row["memory_id"]): row
        for row in conn.execute("SELECT memory_id, text_hash, model FROM memory_vectors")
    }
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for memory_id, text in records:
        seen.add(memory_id)
        digest = text_hash(text)
        row = rows.get(memory_id)
        if row is None or str(row["text_hash"]) != digest or str(row["model"]) != model:
            pending.append((memory_id, text))
    removed = [memory_id for memory_id in rows if memory_id not in seen]

    with WRITE_LOCK, conn:
        if removed:
            conn.executemany(
                "DELETE FROM memory_vectors WHERE memory_id = ?",
                [(memory_id,) for memory_id in removed],
            )
        embedded = 0
        if pending:
            import numpy as np

            vectors = encode([text for _, text in pending])
            for (memory_id, text), vector in zip(pending, vectors):
                array = np.asarray(vector, dtype="<f4")
                conn.execute(
                    "INSERT INTO memory_vectors (memory_id, text_hash, model, dim, vec) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "text_hash = excluded.text_hash, model = excluded.model, "
                    "dim = excluded.dim, vec = excluded.vec",
                    (memory_id, text_hash(text), model, int(array.size), array.tobytes()),
                )
                embedded += 1
    return {
        "embedded": embedded,
        "skipped": len(pending) - embedded,
        "removed": len(removed),
    }


def search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    model: str,
    limit: int,
    min_score: float,
) -> list[tuple[str, float]]:
    """余弦检索，低于下限的不算命中。"""
    import numpy as np

    rows = list(
        conn.execute(
            "SELECT memory_id, dim, vec FROM memory_vectors WHERE model = ?", (model,)
        )
    )
    if not rows:
        return []
    query = np.asarray(query_vector, dtype="<f4")
    scored: list[tuple[str, float]] = []
    for item in rows:
        vector = np.frombuffer(item["vec"], dtype="<f4")
        if vector.size != query.size:
            continue
        score = float(query @ vector)
        if score < min_score:
            continue
        scored.append((str(item["memory_id"]), score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:limit]


def version(conn: sqlite3.Connection) -> int:
    """向量表的版本号，供进程内缓存判断是否失效。"""
    row = conn.execute("SELECT value FROM kv WHERE key = 'memory_vectors'").fetchone()
    return int(row["value"]) if row else 0
