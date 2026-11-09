"""检索接口的取数与拼装：语料一段、记忆一段、诊断读数一段。

**调的是线上同一个 `knowledge.search`**：网页上看到的名次与模型调 `search_corpus`
拿到的是同一份，不另写一条检索路径。这里多做的只有两件事：把每一路的原始名次
带出来（`fuse` 本身只留融合分），以及把记忆那一段按同一句查询取回来。

记忆检索**不算一次使用**：`recall(touch=False)`。网页打开一次就把
`last_used_at` 推到现在的话，「核心事实」会被页面浏览顺序改写，而它本该反映
模型真的用过哪几条。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ... import corpus_config, embed
from ...knowledge.models import KnowledgeQuery
from ...knowledge.search import SEARCH_MODES, search as knowledge_search
from ...memory import MemoryStore

MODES = SEARCH_MODES
"""检索模式，与 `knowledge.search` 同一份定义。"""

USAGE_DIRECTORY = ".usage"
USAGE_FILE = "search.jsonl"
USAGE_MAX_BYTES = 5 * 1024 * 1024
"""使用记录超过这个体量时换成 `search.jsonl.1`。

一条记录不到 200 字节，5MB 大约是两万多次检索；个人使用几个月也到不了。
放在索引库外面，是因为索引按设计可以删掉重建，而「查过什么」不是派生物。
"""


def knowledge_payload(
    workspace_root: Path,
    text: str,
    *,
    limit: int = 8,
    mode: str = "hybrid",
    source_ids: tuple[str, ...] = (),
    corpus_root: Path | None = None,
) -> dict:
    """一句查询的两段结果。`text` 为空由调用方先挡下（回 400）。

    `corpus_root` 与 `workspace_root` 分开传：语料可以指到别处，记忆跟着项目走。
    """
    result = knowledge_search(
        KnowledgeQuery(text=text, limit=limit, mode=mode, source_ids=source_ids),
        root=corpus_root,
    )
    return {
        "query": text,
        "mode": mode,
        "corpus": [
            {
                "ref": hit.id,
                "sourceId": hit.source_id,
                "kind": hit.kind,
                "title": hit.title,
                "section": hit.section,
                "page": hit.page,
                "text": hit.text,
                "rrf": round(hit.score, 6),
                "ranks": dict(hit.ranks),
            }
            for hit in result.hits
        ],
        "memory": memory_section(workspace_root, text, limit=limit),
        "diagnostics": {
            "channels": dict(result.channels),
            "vectorCoverage": round(result.vector_coverage, 4),
            "groups": result.groups,
            "hitsTotal": result.hits_total,
            "reason": result.reason,
            "rerank": result.rerank,
        },
    }


def memory_section(workspace_root: Path, text: str, *, limit: int = 8) -> list[dict]:
    """记忆那一段。模型没装时只跑字面一路，语义分不参与。"""
    store = MemoryStore(workspace_root)
    store.load()
    config = corpus_config.load(workspace_root)
    model = config.embed_model if config.embed and embed.available(config.embed_model) else ""
    scored = store.recall_scored(text, limit=limit, model=model)
    return [
        {
            "id": entry.id,
            "kind": entry.kind.value,
            "text": entry.text,
            "score": round(score, 6),
            "refs": list(entry.refs),
        }
        for entry, score in scored
    ]


def append_usage(corpus_root: Path, record: dict) -> dict:
    """追加一行使用记录。**不改索引、不改状态文件。**

    写的是语料根下的 `.usage/search.jsonl`，与索引分开：用户删掉 `.index`
    重建时不该连「查过什么」一起丢。文件超过 `USAGE_MAX_BYTES` 时换成
    `search.jsonl.1`，只保留上一份，不做多份轮转——这是使用统计，不是审计。
    """
    query = str(record.get("query") or "").strip()
    if not query:
        raise ValueError("使用记录缺 query")
    if len(query) > 500:
        raise ValueError("query 最多 500 字")
    mode = str(record.get("mode") or "hybrid").strip() or "hybrid"
    if mode not in MODES:
        raise ValueError(f"不认识的检索模式：{mode}")
    hits = _int_field(record.get("hits"), "hits", 0, 1000)
    ms = _int_field(record.get("ms"), "ms", 0, 600_000)
    item = {
        "at": time.time(),
        "query": query,
        "mode": mode,
        "hits": hits,
        "ms": ms,
        "empty": bool(record.get("empty")) or hits == 0,
    }
    directory = Path(corpus_root) / USAGE_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / USAGE_FILE
    if path.is_file() and path.stat().st_size >= USAGE_MAX_BYTES:
        path.replace(directory / f"{USAGE_FILE}.1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def analytics(corpus_root: Path) -> dict:
    """使用记录的汇总：次数、p95、模式分布、空结果率。

    `p95` 用最近的那一份与轮转出去的那一份一起算；两份都没有时给 0。
    读不出来的行跳过——一条坏行不该让整页统计失败。
    """
    directory = Path(corpus_root) / USAGE_DIRECTORY
    records: list[dict] = []
    for name in (f"{USAGE_FILE}.1", USAGE_FILE):
        path = directory / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
    if not records:
        return {
            "count": 0,
            "p95": 0,
            "mean": 0,
            "emptyRate": 0.0,
            "modes": {},
            "last": None,
        }
    times = sorted(int(item.get("ms") or 0) for item in records)
    modes: dict[str, int] = {}
    for item in records:
        key = str(item.get("mode") or "hybrid")
        modes[key] = modes.get(key, 0) + 1
    empty = sum(1 for item in records if item.get("empty"))
    return {
        "count": len(records),
        "p95": times[max(0, int(len(times) * 0.95) - 1)],
        "mean": round(sum(times) / len(times)),
        "emptyRate": round(empty / len(records), 4),
        "modes": modes,
        "last": records[-1],
    }


def _int_field(raw: object, name: str, low: int, high: int) -> int:
    """取一个整数并夹取。写错就回 400：统计里一个负数会让均值失去意义。"""
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 要是整数：{raw!r}") from error
    if value < low or value > high:
        raise ValueError(f"{name} 要在 {low} 到 {high} 之间：{value}")
    return value
