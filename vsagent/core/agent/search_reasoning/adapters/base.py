"""查询结果适配层的公共部分。

一个适配器只做一件事：把某个工具的结果整理成证据对象。取数仍由原工具负责，
这里不改请求、不改参数、不改输出，只把输出里能核对的部分取出来。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..models import SearchEvidence

# 一条证据正文留在对象里的上限。完整的工具输出还在 Part 上，
# 这里留标题与摘要；需要全文时按 `metadata` 里的引用回原文。
EXCERPT = 1200


def clip(text: str, limit: int = EXCERPT) -> str:
    """截到指定长度；空内容返回空串。"""
    value = (text or "").strip()
    if limit and len(value) > limit:
        return value[:limit] + "…"
    return value


def evidence_id(tool_name: str, *pieces: str) -> str:
    """按工具名与内容指纹生成一个稳定的证据编号。

    同一份结果重复出现时编号相同，去重就有依据；不同批次命中相同标题
    但正文不同时，指纹不同，两条都保留。
    """
    digest = hashlib.sha1(
        "|".join(piece or "" for piece in pieces).encode("utf-8")
    ).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9]+", "_", (tool_name or "tool").lower()).strip("_")
    return f"e_{slug}_{digest}"


def rank_score(index: int) -> float:
    """按命中顺序给一个检索相关度：越靠前越高，最低 0.4。"""
    return max(0.4, 1.0 - index * 0.08)


def one(
    part: Any,
    *,
    source_type: str,
    source: str = "",
    content: str = "",
    title: str = "",
    url: str = "",
    published_at: str = "",
    metadata: dict[str, Any] | None = None,
) -> SearchEvidence:
    """把一整段工具输出收成一条证据。"""
    body = clip(content or getattr(part, "tool_output", "") or "")
    name = getattr(part, "tool_name", "") or source_type
    return SearchEvidence(
        id=evidence_id(name, getattr(part, "tool_input", {}).get("query", ""), body),
        content=body or "（这条结果没有正文）",
        source=source or name,
        source_type=source_type,
        title=title,
        url=url,
        published_at=published_at,
        retrieval_score=0.6,
        metadata=dict(metadata or {}),
    )


def meta_of(part: Any) -> dict[str, Any]:
    """取工具写在 Part 上的结构化读数。"""
    value = getattr(part, "tool_meta", None)
    return dict(value) if isinstance(value, dict) else {}
