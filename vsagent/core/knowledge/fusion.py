from __future__ import annotations

from typing import Any

from .. import index
from ..rrf import RRF_K

SAME_GROUP_CHARS = 600
"""同一节最多拼多少字给模型看。8 组 × 600 ≈ 4800 字，在压缩线以内。"""

GROUP_CHUNK_CHARS = 400
"""组内单块的上限。超过就截断——组的意义是「同节上下文」，不是把整节搬过来。"""


def fuse(channels: dict[str, list[int]]) -> dict[int, float]:
    """RRF：`Σ 1/(k + rank)`。**不把 BM25 分数与别的分数相加**——量纲不同。"""
    return fuse_with_channels(channels)[0]


def fuse_with_channels(
    channels: dict[str, list[int]],
) -> tuple[dict[int, float], dict[int, dict[str, int]]]:
    """同一次融合，多返回每个块在每一路里的名次。

    网页上要回答「这条是词法命中还是语义命中、各排第几」。融合之后只剩一个
    分数，名次信息在 `fuse` 里被丢掉了；`fuse` 的签名与返回值保持不变，
    是因为评测脚本按它断言线上口径。
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, ids in channels.items():
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            ranks.setdefault(chunk_id, {})[name] = rank
    return scores, ranks


def group(
    conn: Any,
    scored: list[tuple[int, float]],
    rows: dict[int, Any],
    ranks: dict[int, int] | None = None,
    channel_ranks: dict[int, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """按「源 + 章节」聚组，并把命中块的前后邻块并进来。

    不做这一步的话，top 8 会变成「同一页的五个碎片 + 三篇无关论文」——
    RRF 只看块，而人看的是「这一段出自哪里」。

    `ranks` 是精排之后每个块的位次。给了它，组的先后按「组内最好的那个位次」
    排，`score` 仍然是 RRF 分：两者量纲不同，混在一起相加之后名次不再可解释。

    `channel_ranks` 是每一路里每个块的名次。组的 `ranks` 取组内每个通道的
    最好名次：同一节里第 2 块与第 7 块都在词法里，读者要看到的是「这一节在
    词法里最高排第 2」。

    返回的每一项是一个字典而不是 dataclass：`tools/check_retrieval.py` 的评测
    直接读 `group["source_id"]` / `group["text"]`，这一层换了形状，评测测的
    就不再是线上那份实现。
    """
    scores = dict(scored)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    rank_of = ranks or {}
    channel_of = channel_ranks or {}

    for chunk_id, _score in scored:
        row = rows.get(chunk_id)
        if row is None:
            continue
        key = (str(row["source_id"]), str(row["section"]))
        entry = groups.setdefault(
            key,
            {
                "best": 0.0,
                "rank": len(rank_of) + 1,
                "rows": {},
                "ranks": {},
                "kind": str(row["kind"]),
                "title": str(row["title"]),
            },
        )
        entry["best"] = max(entry["best"], scores.get(chunk_id, 0.0))
        if chunk_id in rank_of:
            entry["rank"] = min(entry["rank"], rank_of[chunk_id])
        for name, position in channel_of.get(chunk_id, {}).items():
            known = entry["ranks"].get(name)
            entry["ranks"][name] = position if known is None else min(known, position)
        entry["rows"][int(row["chunk_no"])] = row
        for neighbor in index.neighbors(conn, row):
            entry["rows"].setdefault(int(neighbor["chunk_no"]), neighbor)

    out: list[dict[str, Any]] = []
    for (source_id, section), entry in groups.items():
        ordered = [entry["rows"][number] for number in sorted(entry["rows"])]
        kind, sid = source_id.split(":", 1)
        pages = [int(row["page"]) for row in ordered if int(row["page"])]
        out.append(
            {
                "score": entry["best"],
                "rank": entry["rank"],
                "ranks": dict(entry["ranks"]),
                "page": min(pages) if pages else 0,
                "ref": _citation(kind, sid, section, ordered),
                "source_id": source_id,
                "kind": kind,
                "title": entry["title"],
                "section": section,
                "text": _joined(ordered),
            }
        )
    if ranks is None:
        out.sort(key=lambda item: (-item["score"], item["source_id"], item["section"]))
    else:
        out.sort(
            key=lambda item: (
                item["rank"],
                -item["score"],
                item["source_id"],
                item["section"],
            )
        )
    return out


def _joined(rows: list[Any]) -> str:
    """组内各块拼起来，**按 chunk_no 去重并按顺序**，总长受预算限制。"""
    pieces: list[str] = []
    seen: set[str] = set()
    total = 0
    for row in rows:
        text = str(row["text"]).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        snippet = text[:GROUP_CHUNK_CHARS]
        if total + len(snippet) > SAME_GROUP_CHARS:
            break
        pieces.append(snippet)
        total += len(snippet)
    return "\n…\n".join(pieces)


def _citation(kind: str, source_id: str, section: str, rows: list[Any]) -> str:
    """`doc#ab12cd34ef §2.1 In vitro assay p.2`。

    节用路径的**最后一段**：`§2 Methods / 2.1 In vitro assay` 读起来太长，
    而引用只需要能对回原文的那一节。
    """
    ref = f"{kind}#{source_id}"
    leaf = section.split(" / ")[-1].strip()
    if leaf:
        ref += f" §{leaf}"
    pages = [int(row["page"]) for row in rows if int(row["page"])]
    if pages and kind == "doc":
        low, high = min(pages), max(pages)
        ref += f" p.{low}" if low == high else f" p.{low}-{high}"
    return ref
