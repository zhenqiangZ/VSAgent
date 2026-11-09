from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import corpus_config, embed
from ..onnx_gpu import providers as execution_providers

__all__ = [
    "download",
    "installed",
    "missing_reason",
    "rerank",
]

# 送进精排的正文上限。聚组之后每块只展示 600 字（`SAME_GROUP_CHARS`），
# 拿两千字的原文去算 MaxSim 只是在给模型更多「碰巧命中查询词」的机会，
# 而检索结果里看不到那一截。
MAX_RERANK_CHARS = 800

_CACHE: dict[str, object] = {}


def _cache_root() -> Path:
    """模型缓存目录。与文档嵌入共用 `~/.vsagent/models/`。

    走同一份缓存是因为 `embed._snapshot` 的保存布局固定在这个根下；
    再分一层子目录，fastembed 就找不到已经拉下来的文件。
    """
    return embed.models_dir()


def _marker(model: str) -> Path:
    """下载完成的记号。

    fastembed 自己的目录布局随版本变，判断「装没装」不能按目录名去猜。
    显式下载之后写一个记号文件，判断只看它：**没有记号就不实例化**，
    实例化会顺带联网下载，而这一步必须由人发起。
    """
    return _cache_root() / f"rerank-{model.replace('/', '--')}.json"


def installed(model: str) -> bool:
    """这个模型是否已经下载过。"""
    return _marker(model).is_file()


def missing_reason(model: str) -> str:
    """没装时给一句能照着做的话；装了返回空串。"""
    if installed(model):
        return ""
    return (
        f"精排模型 {model} 还没下载。运行 "
        "`python -m vsagent.core.knowledge.providers download` 拉一次。"
    )


def download(model: str) -> int:
    """显式拉取模型，返回写下的记号文件字节数。

    只有这里会触发下载。`rerank` 在没记号时直接返回空表，不做自动拉取。
    端点顺序照抄 `embed.download`：`HF_ENDPOINT`（或直连）→ 镜像。
    """
    from fastembed import LateInteractionTextEmbedding

    print(f"下载 {model} 到 {_cache_root()} …")
    preferred = (os.environ.get("HF_ENDPOINT") or "").strip() or embed.DEFAULT_ENDPOINT
    endpoints = [preferred]
    if embed.MIRROR_ENDPOINT not in endpoints:
        endpoints.append(embed.MIRROR_ENDPOINT)
    for endpoint in endpoints:
        if embed._snapshot(endpoint, model):
            break
    else:
        raise RuntimeError("下载失败：直连与镜像都没成。")

    # 离线加载一遍，确认权重真的能用；文件不全时在这里当场失败，
    # 而不是等到第一次检索。
    embedder = LateInteractionTextEmbedding(
        model_name=model,
        cache_dir=str(_cache_root()),
        providers=execution_providers(),
        local_files_only=True,
    )
    list(embedder.passage_embed(["warm up"]))
    payload = json.dumps({"model": model, "downloaded_at": time.time()})
    target = _marker(model)
    target.write_text(payload, encoding="utf-8")
    _CACHE[model] = embedder
    return len(payload)


def _embedder(model: str):
    """进程内缓存。加载一次要一两秒，一次检索不该付两遍。"""
    cached = _CACHE.get(model)
    if cached is not None:
        return cached
    from fastembed import LateInteractionTextEmbedding

    embedder = LateInteractionTextEmbedding(
        model_name=model,
        cache_dir=str(_cache_root()),
        providers=execution_providers(),
        local_files_only=True,
    )
    _CACHE[model] = embedder
    return embedder


def rerank(
    query: str,
    texts: list[str],
    *,
    top_n: int,
    model: str | None = None,
) -> list[tuple[int, float]]:
    """晚交互精排，返回 `(原始下标, MaxSim 分)`，按分数降序。

    `MaxSim = Σ_i max_j (q_i · d_j)`：每个查询词取它与文档词里最像的那一个，
    再求和。它比单个余弦多保留了「查询里的每个词分别对应哪」这一层。

    模型没下载或 fastembed 没装时返回空表，调用方按原顺序继续。
    """
    name = model or corpus_config.DEFAULT_RERANK_MODEL
    if not installed(name):
        return []
    import numpy as np

    embedder = _embedder(name)
    query_vectors = list(embedder.query_embed([query]))
    if not query_vectors:
        return []

    left = np.asarray(query_vectors[0])
    trimmed = [text[:MAX_RERANK_CHARS] for text in texts]
    scores: list[tuple[int, float]] = []
    for index, passage in enumerate(embedder.passage_embed(trimmed)):
        right = np.asarray(passage)
        if left.size == 0 or right.size == 0:
            scores.append((index, 0.0))
            continue
        similarity = left @ right.T
        scores.append((index, float(similarity.max(axis=1).sum())))
    scores.sort(key=lambda pair: (-pair[1], pair[0]))
    return scores[:top_n]


def _main(argv: list[str]) -> int:
    """`python -m vsagent.core.knowledge.providers download [模型名]`。"""
    if len(argv) < 1 or argv[0] != "download":
        print(
            "用法：python -m vsagent.core.knowledge.providers download [模型名]\n"
            f"不给模型名时下载 {corpus_config.DEFAULT_RERANK_MODEL}"
        )
        return 2
    model = argv[1] if len(argv) > 1 else corpus_config.DEFAULT_RERANK_MODEL
    size = download(model)
    print(f"已下载 {model}（记号 {size} 字节）")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
