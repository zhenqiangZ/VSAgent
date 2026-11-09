from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provenance:
    """一条知识来自哪里。检索侧只填得出前几项，其余由调用方补。"""

    kind: str                 # doc | web | memory | tool
    source_id: str = ""
    locator: str = ""         # 文件路径或 URL
    authority: int | None = None
    published: str = ""
    checked_at: float = 0.0


@dataclass(frozen=True)
class KnowledgeQuery:
    """一次检索请求。

    `scope` 现在只有 `corpus` 一种实现：记忆那一侧的检索要拿到 `MemoryStore`，
    它跟着应用走，不跟着这个包走。

    `mode` 决定跑哪几路：`hybrid` 是默认的三路，`lexical` 只跑两张 FTS 表，
    `semantic` 只跑向量。网页上要对照「词法命中还是语义命中」时用它。
    `source_ids` 非空时把检索限在这几份源里。
    """

    text: str
    scope: str = "corpus"
    limit: int = 8
    rewrite: bool = False     # 只有长口语查询打开
    mode: str = "hybrid"
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeHit:
    """一条命中。

    `id` 就是渲染与引用用的那串（`doc#ab12cd34ef §2.1 p.7`）：引用号在聚组那一步
    已经拼好，这里再分成页码字段只会多一份要对得上的数据。
    """

    id: str
    text: str
    score: float              # RRF 分，量纲只在融合内部有意义
    kind: str
    source_id: str
    section: str = ""
    title: str = ""
    page: int = 0             # 组内第一处非零页码；网页来源是 0
    ranks: dict[str, int] = field(default_factory=dict)
    """每一路里这个块的名次。融合之后只剩分数，网页要显示「哪一路排第几」。"""
    rerank: float = 0.0       # 精排原分，未开精排时为 0
    provenance: Provenance | None = None


@dataclass(frozen=True)
class SearchResult:
    """一次检索的结果与诊断读数。

    `channels` 是每一路召回了多少条，工具把它原样写进 `meta`：
    少了它，模型看到「本地没有」时无法判断是索引空、还是语义那一路没跑。
    `reason` 为 `empty` 表示三路都没有命中，`stale` 表示命中的源已经被删。
    """

    hits: list[KnowledgeHit] = field(default_factory=list)
    channels: dict[str, int] = field(default_factory=dict)
    vector_coverage: float = 0.0
    groups: int = 0
    hits_total: int = 0
    reason: str = ""
    rerank: bool = False
