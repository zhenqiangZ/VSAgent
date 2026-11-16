"""文献类工具的结果适配。

`search_literature` 在 `tool_meta` 里给了逐篇的
标题、摘要、期刊、年份与编号；`read_fulltext` 给的是整段正文。前者按篇
分成多条证据，后者收成一条全文证据。
"""

from __future__ import annotations

from typing import Any

from ..models import SearchEvidence
from .base import clip, evidence_id, meta_of, one, rank_score

ARTICLE_KEYS = ("articles", "papers", "results")


class LiteratureAdapter:
    names = ("search_literature", "read_fulltext")
    source_type = "literature"

    def convert(self, part: Any) -> list[SearchEvidence]:
        meta = meta_of(part)
        items: list[dict[str, Any]] = []
        for key in ARTICLE_KEYS:
            value = meta.get(key)
            if isinstance(value, list) and value:
                items = [item for item in value if isinstance(item, dict)]
                break
        if not items:
            return [one(part, source_type="literature", source="文献")]

        results: list[SearchEvidence] = []
        for index, item in enumerate(items):
            title = str(item.get("title") or "").strip()
            abstract = str(item.get("abstract") or "").strip()
            pmid = str(item.get("pmid") or "").strip()
            doi = str(item.get("doi") or "").strip()
            url = str(item.get("url") or "").strip()
            if not url and pmid:
                url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            journal = str(item.get("journal") or "").strip()
            year = str(item.get("year") or "").strip()
            content = clip("\n".join(piece for piece in (title, abstract) if piece))
            if not content:
                continue
            results.append(
                SearchEvidence(
                    id=evidence_id(part.tool_name, pmid, doi, title),
                    content=content,
                    source=journal or "PubMed",
                    source_type="literature",
                    title=title,
                    url=url,
                    published_at=year,
                    retrieval_score=rank_score(index),
                    metadata={
                        "pmid": pmid,
                        "doi": doi,
                        "journal": journal,
                        "authors": list(item.get("authors") or [])[:6],
                    },
                )
            )
        return results or [one(part, source_type="literature", source="文献")]
