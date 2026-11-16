"""网页类工具的结果适配。

`search_web` 在 `tool_meta["sources"]` 里给了每条命中的标题与链接；
`fetch_webpage` 给的是整页正文。前者按条分开，后者收成一条全文证据。
"""

from __future__ import annotations

from typing import Any

from ..models import SearchEvidence
from .base import clip, evidence_id, meta_of, one, rank_score


class WebAdapter:
    names = ("search_web", "fetch_webpage")
    source_type = "web"

    def convert(self, part: Any) -> list[SearchEvidence]:
        meta = meta_of(part)
        sources = meta.get("sources")
        if not isinstance(sources, list) or not sources:
            return [
                one(
                    part,
                    source_type="web",
                    source=str(meta.get("url") or "网页"),
                    url=str(meta.get("url") or ""),
                )
            ]
        results: list[SearchEvidence] = []
        for index, item in enumerate(sources):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(
                item.get("snippet") or item.get("summary") or item.get("description") or ""
            ).strip()
            content = clip("\n".join(piece for piece in (title, snippet) if piece))
            if not content:
                continue
            results.append(
                SearchEvidence(
                    id=evidence_id(part.tool_name, url, title),
                    content=content,
                    source="网页",
                    source_type="web",
                    title=title,
                    url=url,
                    published_at=str(item.get("published_at") or ""),
                    retrieval_score=rank_score(index),
                    metadata={"snippet": snippet},
                )
            )
        return results or [one(part, source_type="web", source="网页")]
