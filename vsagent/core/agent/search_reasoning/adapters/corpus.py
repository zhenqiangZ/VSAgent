"""语料库工具的结果适配。

`search_corpus` 给的是命中摘要与引用号，`read_source` 给的是某一页的正文。
两者都按「一条来源 + 一段正文」收成证据，引用号留在 `metadata` 里，
需要原文时按它回去读。
"""

from __future__ import annotations

from typing import Any

from ..models import SearchEvidence
from .base import clip, meta_of, one


class CorpusAdapter:
    names = ("search_corpus", "read_source")
    source_type = "corpus"

    def convert(self, part: Any) -> list[SearchEvidence]:
        meta = meta_of(part)
        ref = str(meta.get("ref") or "")
        title = str(meta.get("title") or "")
        evidence = one(
            part,
            source_type="corpus",
            source=f"语料库 {ref}".strip(),
            content=clip(str(getattr(part, "tool_output", "") or "")),
            title=title,
            metadata={
                "ref": ref,
                "depth": meta.get("depth", ""),
                "pages": meta.get("pages", 0),
                "page": meta.get("page", 0),
            },
        )
        return [evidence]


def computational(part: Any, source_type: str = "computational") -> list[SearchEvidence]:
    """计算产物：对接、亲和力、结构分析这一类。

    它们不套用文献证据的评价规则，也不直接改动文献类说法；登记成一条
    计算证据，留给整合阶段按「计算结果」处理。
    """
    return [
        one(
            part,
            source_type=source_type,
            source=str(getattr(part, "tool_name", "") or "计算"),
            metadata=meta_of(part),
        )
    ]
