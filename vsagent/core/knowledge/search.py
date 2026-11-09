from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import corpus, corpus_config, embed, index
from .fusion import fuse_with_channels, group
from .models import KnowledgeHit, KnowledgeQuery, SearchResult

SEARCH_MODES = ("hybrid", "lexical", "semantic")
"""检索模式。`hybrid` 是默认的三路，另外两个各关掉一半，用来对照。"""


def _alive(row: Any, root: Path | None = None) -> bool:
    """这个块的源还在不在盘上。

    删除在语料侧是「换名进 `.trash/`」，索引侧不通知任何人。这里查一下目录：
    目录没了就跳过——对用户来说删除就是「查不到」，不必等下一次重建。

    `root` 是语料根。网页服务可以指到别处（判据、`--corpus`），
    按代码位置算出来的那一份会与它读的索引不是同一个目录。
    """
    try:
        kind = str(row["kind"])
        source_id = str(row["source_id"]).split(":", 1)[1]
    except (ValueError, IndexError):
        return False
    base = Path(root) if root is not None else corpus.corpus_root()
    return (base / kind / source_id).is_dir()


def search(query: KnowledgeQuery, *, root: Path | None = None) -> SearchResult:
    """三路召回、RRF 融合、按节聚组，返回命中与诊断读数。

    这一层是同步的：ONNX 推理与 SQLite 查询都在调用方的线程里跑，
    工具层用 `asyncio.to_thread` 把它挪出事件循环。

    `root` 只在网页端传：那边可以指到一份临时语料，索引、存活核对与配置都要
    跟着它走。领域工具不传，按代码位置定位到仓根那份。
    """
    text = query.text.strip()
    if not text:
        raise ValueError("检索词不能为空")
    if query.scope != "corpus":
        raise ValueError(f"未知的检索范围：{query.scope!r}；现在只实现 corpus")
    mode = query.mode or "hybrid"
    if mode not in SEARCH_MODES:
        raise ValueError(f"未知的检索模式：{mode!r}；可用 {'、'.join(SEARCH_MODES)}")

    conn = index.connect(root)
    try:
        config = corpus_config.load(root.parent if root is not None else None)
        channels: dict[str, list[int]] = {}
        if mode in ("hybrid", "lexical"):
            channels.update(
                index.lexical(conn, text, source_ids=query.source_ids)
            )
        coverage = 0.0
        # 模型没装/没下载时**静默降级**：技术细节进 meta，不进正文——
        # 模型看到的应该是「本地没有」，而不是一段 pip install 指引。
        # 覆盖率也一并报 0：这一路没跑，报个 100% 只会让人以为语义召回声效正常。
        if mode in ("hybrid", "semantic") and config.embed and not embed.missing_reason(
            config.embed_model
        ):
            coverage = embed.coverage(conn, config.embed_model)
            # 按源筛是在取回之后做的，筛完可能一条不剩；所以过滤生效时这一路
            # 多取一些，筛完再截回正常深度。
            depth = index.CHANNEL_LIMIT * 4 if query.source_ids else index.CHANNEL_LIMIT
            semantic = embed.search(conn, text, config.embed_model, limit=depth)
            if query.source_ids:
                wanted = set(query.source_ids)
                owners = index.source_ids_of(conn, [chunk_id for chunk_id, _ in semantic])
                semantic = [
                    (chunk_id, score)
                    for chunk_id, score in semantic
                    if owners.get(chunk_id) in wanted
                ][: index.CHANNEL_LIMIT]
            if semantic:
                channels["vec"] = [chunk_id for chunk_id, _ in semantic]
        counts = {name: len(ids) for name, ids in channels.items()}
        fused, channel_ranks = fuse_with_channels(channels)
        if not fused:
            return SearchResult(
                channels=counts, vector_coverage=coverage, reason="empty"
            )

        order = sorted(fused, key=lambda cid: (-fused[cid], cid))
        candidates = [(cid, fused[cid]) for cid in order[: index.CHANNEL_LIMIT]]
        rows = index.rows_by_id(conn, [cid for cid, _ in candidates])
        # 源被删掉的那些直接从候选里去掉：删除是「换名进 .trash/」，索引不知道，
        # 这里按目录存在性对一次账。
        alive = [
            (cid, score)
            for cid, score in candidates
            if cid in rows and _alive(rows[cid], root)
        ]
        reranked = False
        ranks: dict[int, int] | None = None
        if config.rerank and len(alive) > 1:
            from . import providers

            texts = [str(rows[cid]["text"]) for cid, _ in alive]
            ranked = providers.rerank(
                text, texts, top_n=len(texts), model=config.rerank_model
            )
            if ranked:
                alive = [alive[index] for index, _ in ranked]
                ranks = {cid: position for position, (cid, _) in enumerate(alive, 1)}
                reranked = True
        groups = group(
            conn,
            alive,
            rows,
            ranks=ranks,
            channel_ranks=channel_ranks,
        )
    finally:
        conn.close()

    if not groups:
        return SearchResult(
            channels=counts,
            vector_coverage=coverage,
            hits_total=len(fused),
            reason="stale",
        )

    hits = [
        KnowledgeHit(
            id=item["ref"],
            text=item["text"],
            score=item["score"],
            kind=item["kind"],
            source_id=item["source_id"],
            section=item["section"],
            title=item["title"],
            page=item["page"],
            ranks=item["ranks"],
        )
        for item in groups[: query.limit]
    ]
    return SearchResult(
        hits=hits,
        channels=counts,
        vector_coverage=coverage,
        groups=len(groups),
        hits_total=len(fused),
        rerank=reranked,
    )
