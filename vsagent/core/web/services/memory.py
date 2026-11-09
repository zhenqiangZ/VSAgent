"""记忆的只读取数：列表、详情、归档清单。

每次请求都新建一个 `MemoryStore` 从盘上读：网页端看到的必须与
`entries.json` 当前内容一致，而应用进程随时可能写入。读是整文件解析，几十条
记忆的量级下一次不到一毫秒。

**写不在这里**。归档与恢复要改 `entries.json`，它由应用进程那个 `MemoryStore`
持有；网页只把动作转交过去（见 `routes.py` 的 `memory_action`）。
"""

from __future__ import annotations

import time
from pathlib import Path

from ...memory import MemoryEntry, MemoryStore

STATE_TEXT = {
    "ok": "有效",
    "stale": "引用已变",
    "superseded": "已被取代",
    "expired": "已过期",
    "missing": "引用丢失",
}


def entry_state(entry: MemoryEntry, now: float | None = None) -> str:
    """这一条现在算哪种状态。

    `superseded_by` 与 `valid_to` 是同一种结局的两条记录路径，归成一类；
    `expires_at` 单独一类——过期与取代的原因不同，页面上要分开说。
    `stale` 与 `missing` 都来自引用核对（`lifecycle._state_for`）：前者是文件还在
    但内容变了，后者是文件不在了，页面上要分得开。
    """
    stamp = time.time() if now is None else now
    if entry.state in ("missing", "stale"):
        return entry.state
    if entry.superseded_by:
        return "superseded"
    if entry.valid_to and entry.valid_to <= stamp:
        return "superseded"
    if entry.expires_at and entry.expires_at <= stamp:
        return "expired"
    return "ok"


def entry_json(entry: MemoryEntry, *, archived: bool = False) -> dict:
    """一条记忆的网页形状。字段名与状态文件里的键一一对应，页面不再做映射。"""
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "text": entry.text,
        "refs": list(entry.refs),
        "state": "archived" if archived else entry_state(entry),
        "archived": archived,
        "supersedes": list(entry.supersedes),
        "supersededBy": entry.superseded_by,
        "supersededRule": entry.superseded_rule,
        "conflicts": list(entry.conflicts),
        "unverified": entry.unverified,
        "confidence": entry.confidence,
        "source": entry.source,
        "createdAt": entry.created_at,
        "updatedAt": entry.updated_at,
        "lastUsedAt": entry.last_used_at,
        "expiresAt": entry.expires_at,
        "sessionId": entry.session_id,
        "turn": entry.turn,
    }


def load_store(workspace_root: Path) -> MemoryStore:
    """新建一份并从盘上读。网页端每次请求都要重读，理由见文件头。"""
    store = MemoryStore(workspace_root)
    store.load()
    return store


def list_entries(
    workspace_root: Path, *, kind: str = "", state: str = "", query: str = ""
) -> dict:
    """全部记忆，活的与归档的都在一份清单里，用 `archived` 区分。

    `counts` 统计的是**全部**，不随筛选变化：页面顶部要显示「几条事实、几条经验」，
    那个数不该因为搜索框里打了个字就变。
    """
    store = load_store(workspace_root)
    live = [entry_json(entry) for entry in store.entries.values()]
    archived = [entry_json(entry, archived=True) for entry in store.archived_entries()]
    rows = live + archived

    counts = {
        "live": len(live),
        "archived": len(archived),
        "byKind": {
            name: sum(1 for item in live if item["kind"] == name)
            for name in ("fact", "preference", "experience")
        },
    }

    picked = rows
    if kind:
        picked = [item for item in picked if item["kind"] == kind]
    if state:
        picked = [item for item in picked if item["state"] == state]
    term = query.strip().casefold()
    if term:
        picked = [
            item
            for item in picked
            if term in item["text"].casefold()
            or any(term in ref.casefold() for ref in item["refs"])
        ]
    picked.sort(key=lambda item: (-float(item["updatedAt"] or 0), item["id"]))
    return {"entries": picked, "counts": counts, "states": dict(STATE_TEXT)}


def entry_detail(workspace_root: Path, entry_id: str) -> dict | None:
    """单条记忆；归档里的也能打开，`archived` 为真。"""
    store = load_store(workspace_root)
    entry = store.entries.get(entry_id)
    if entry is not None:
        return entry_json(entry)
    for item in store.archived_entries():
        if item.id == entry_id:
            return entry_json(item, archived=True)
    return None
