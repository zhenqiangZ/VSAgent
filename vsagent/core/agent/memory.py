from __future__ import annotations

import asyncio
from typing import Any

from .. import corpus_config, embed
from ..memory import (
    DECAY_DAYS,
    EXPERIENCE_DECAY_DAYS,
    FILE_STATE_NOTES,
    KIND_NAMES,
    MAX_ENTRIES,
    MAX_PREFERENCES,
    MEMORY_DIR_NAME,
    MEMORY_TEXT_LIMIT,
    PREFERENCE_LIMIT,
    RESIDENT_LIMIT,
    SNAPSHOT_VERSION,
    SUPERSEDE_MARKERS,
    SUPERSEDE_OVERLAP,
    MemoryEntry,
    MemoryError,
    MemoryKind,
    MemoryStore,
    memory_root,
    new_memory_id,
    overlap,
    parse_kind,
)
from .tools import Tool, ToolError, ToolResult, ToolSpec

__all__ = [
    "DECAY_DAYS",
    "EXPERIENCE_DECAY_DAYS",
    "FILE_STATE_NOTES",
    "KIND_NAMES",
    "MAX_ENTRIES",
    "MAX_PREFERENCES",
    "MEMORY_DIR_NAME",
    "MEMORY_TEXT_LIMIT",
    "PREFERENCE_LIMIT",
    "RESIDENT_LIMIT",
    "SNAPSHOT_VERSION",
    "SUPERSEDE_MARKERS",
    "SUPERSEDE_OVERLAP",
    "MemoryEntry",
    "MemoryError",
    "MemoryKind",
    "MemoryStore",
    "RecallTool",
    "RememberTool",
    "memory_root",
    "new_memory_id",
    "overlap",
    "parse_kind",
]


class RememberTool(Tool):
    """写一条项目记忆，跨会话保留。

    与领域模块各自定义自己的工具是同一个做法：谁的数据，谁的接口。
    """

    spec = ToolSpec(
        name="remember",
        description=(
            "把一条项目事实写进项目记忆，跨会话保留，下次开会话还能取到。"
            "只记打分与筛选之前就存在的东西：受体、结合位点、配体库、参考活性物。"
            "**引用一律写文件路径**（相对路径按工作目录解释），不要写 handle——"
            "handle 只在本次会话有效，换个会话就指向别的东西。"
            "kind=preference 只用来记用户明确说过的长期做法（例如「回答先给结论」），"
            "用户没有明确表态时一律用 fact；kind=experience 用来记带条件与结果的教训。"
            "只在用户明确要求、或一条事实确实值得跨会话复用时才调用，不要随手记。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "一行事实，写清楚是什么、在哪个文件里",
                },
                "kind": {
                    "type": "string",
                    "enum": ["fact", "preference", "experience"],
                    "description": "记忆类型，默认 fact。习惯不写引用文件",
                },
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "这条事实引用的文件路径，可以给多个；不给也行",
                },
                "ttl_days": {
                    "type": "number",
                    "description": "多少天后自动过期；不给就是长期有效。适合会变的临时结论",
                },
                "conflicts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "与哪几条已有记忆矛盾（写它们的编号）。两条都会保留，"
                        "显示时带一行「冲突，未解决」；只有确实对不上时才标，"
                        "数值更新用 supersedes 那种说法写新的一条即可"
                    ),
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """写一条记忆并写盘。"""
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("缺少参数 text")
        raw_refs = arguments.get("refs")
        if raw_refs is None:
            refs: list[str] = []
        elif isinstance(raw_refs, list):
            refs = [str(item) for item in raw_refs if str(item).strip()]
        else:
            raise ToolError("参数 refs 应当是字符串数组")
        raw_ttl = arguments.get("ttl_days") or 0
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
            raise ToolError("参数 ttl_days 应当是数字")
        raw_conflicts = arguments.get("conflicts")
        if raw_conflicts is None:
            conflicts: list[str] = []
        elif isinstance(raw_conflicts, list):
            conflicts = [str(item) for item in raw_conflicts if str(item).strip()]
        else:
            raise ToolError("参数 conflicts 应当是字符串数组")
        try:
            kind = parse_kind(arguments.get("kind") or MemoryKind.FACT.value)
        except MemoryError as error:
            raise ToolError(str(error)) from error
        try:
            entry = self.store.remember(
                text,
                refs,
                kind=kind,
                source="agent",
                ttl_days=float(raw_ttl),
                conflicts=conflicts,
            )
        except MemoryError as error:
            raise ToolError(str(error)) from error
        return ToolResult(output=f"已记住（{entry.id}）：{entry.text}")


class RecallTool(Tool):
    """按关键词检索项目记忆。纯查询，不改动任何东西。"""

    spec = ToolSpec(
        name="recall",
        description=(
            "按关键词检索项目记忆，返回之前记住的项目事实。"
            "在向用户追问「上次那个受体在哪」之前先查一次——"
            "记忆是跨会话的，上一轮做过的事情这里可能已经有记录。"
            "留空 query 则返回全部记忆。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词，例如受体编号或文件名；留空则返回全部",
                },
                "kind": {
                    "type": "string",
                    "enum": ["fact", "preference", "experience"],
                    "description": "只看某一类；不给则三类都返回",
                },
            },
            "additionalProperties": False,
        },
    )

    # 收证据收集器。
    #
    # 它**不声明 `context_argument`**（纯查询工具不挂登记表，那条约定没变），
    # 但检索结果要进 EVIDENCE 层：记忆是跨会话的知识，RECENT 里本来就没有它，
    # 不给一份到证据层，下一步的 RECENT 一被压缩这段知识就没了。
    evidence_argument = "evidence"

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """检索记忆。"""
        raw = arguments.get("query")
        query = raw if isinstance(raw, str) else ""
        raw_kind = arguments.get("kind")
        try:
            kind = (
                parse_kind(raw_kind)
                if isinstance(raw_kind, str) and raw_kind.strip()
                else None
            )
        except MemoryError as error:
            raise ToolError(str(error)) from error
        # 检索之前对一次账：上一轮可能刚重新准备过受体，
        # 引用变了的那条要带着提醒交出去，而不是等下一次启动才发现。
        self.store.revalidate()
        config = corpus_config.load()
        model = ""
        if config.embed and not embed.missing_reason(config.embed_model):
            model = config.embed_model
        # 语义那一路是同步的 ONNX 推理，放进事件循环会挡住界面。
        hits = await asyncio.to_thread(
            self.store.recall, query, touch=True, kind=kind, model=model
        )
        if not hits:
            return ToolResult(output="没有匹配的记忆。")
        # 记下「这几条被用过」：常驻注入据此判断哪些是核心事实。
        # 这是唯一会写盘的地方，常驻注入内部那次 recall 不算。
        self.store.save()
        self._collect(arguments.get("evidence"), hits)
        return ToolResult(
            output="\n".join(self.store.render_line(entry) for entry in hits)
        )

    def _collect(self, collector: Any, hits: list[MemoryEntry]) -> None:
        """把命中的记忆放进证据层。

        只调收集器的 `add`，不认它的类型：`memory.py` 是工具层，
        不该反过来依赖上下文模块。收集器为 None 时什么也不做——
        工具单独被调（判据、脚本里）时没有这一层。

        摘要用正文，正文放引用文件：**一行事实就是这条记忆的全部内容**，
        真正可追溯的凭据是它引用的路径，那才是要留给模型去取的东西。
        """
        if collector is None:
            return
        for entry in hits:
            collector.add(
                ref=f"记忆 {entry.id}",
                summary=entry.text,
                detail="引用文件：" + "、".join(entry.refs) if entry.refs else "",
            )
