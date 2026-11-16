"""按工具名找适配器。

四类查询工具各有一个适配器；对接、亲和力、结构分析与模型预测另算一类。
认不出的工具返回 None，调用方照原样跳过：新的查询工具还没登记时，
不能因为「没有适配器」把这一步当成失败。
"""

from __future__ import annotations

from typing import Any

from .corpus import CorpusAdapter
from .database import ComputationalAdapter, DatabaseAdapter, PredictionAdapter
from .literature import LiteratureAdapter
from .web import WebAdapter

ADAPTERS = (
    LiteratureAdapter(),
    WebAdapter(),
    CorpusAdapter(),
    DatabaseAdapter(),
    ComputationalAdapter(),
    PredictionAdapter(),
)

_TABLE = {
    name: adapter
    for adapter in ADAPTERS
    for name in adapter.names
}

# 只有文献、网页、语料三类会触发搜索增强。数据库、结构、计算与模型预测
# 在状态已经建立之后可以并入证据池，但不单独启动检索流程。
TRIGGER_NAMES = frozenset(
    LiteratureAdapter.names + WebAdapter.names + CorpusAdapter.names
)


def adapter_for(tool_name: str) -> Any | None:
    """按工具名取适配器；不属于查询类工具时返回 None。"""
    return _TABLE.get(str(tool_name or "").strip())


def is_trigger_tool(tool_name: str) -> bool:
    """这个工具的结果能不能启动搜索增强。"""
    return str(tool_name or "").strip() in TRIGGER_NAMES


def query_tool_names() -> frozenset[str]:
    """全部认得出来的查询工具名。"""
    return frozenset(_TABLE)
