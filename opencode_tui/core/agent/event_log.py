"""事件层：把一段已经做完的工作收口成「标题 + 摘要」，原文让位。

为什么需要这一层：摘要回答的是「很久以前发生过什么」，粒度太粗；RECENT 是逐条原文，
粒度太细。中间那段——「刚才调了几次工具、产出了哪个 handle、卡在哪儿」——两边都不管。
模型因此容易在长会话里反复做同一件事，或者接着一个已经被否掉的方案往下走。

三条规则，写在这里也写在文档里：

1. **连续覆盖**。首条事件从 `summary_upto` 开始，后一条接前一条的 `end`，
   RECENT 从最后一条的 `end` 开始。中间不许有缝——有缝就意味着那一段原文
   既不在事件里也不在 RECENT 里，内容无声消失，而上下文看起来完全正常。
2. **边界排除正在写的东西**。当前这条 assistant 消息与紧邻的用户消息永不进事件：
   前者还没写完，后者是下一步的输入。
3. **本地兜底不花请求**。模型写不出摘要（请求失败、被取消、返回空）时，
   按 Part 里的真实记录汇编一条 `degraded=True` 的事件。宁可读起来糙，
   也不能因为「再请求一次失败」让收口整条断掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..models import Event, PartType, Session, ToolStatus
from .prompt import event_messages
from .tools import Tool, ToolResult, ToolSpec

# 标题与摘要的长度上限。**代码里截**，不只写在提示词里：
# 模型不会每次都照做，而这两段进的是每步都要发的索引。
EVENT_TITLE_MAX = 40
EVENT_SUMMARY_MAX = 600
# `recall_event` 一次最多铺多少行原文。
EVENT_RECALL_LIMIT = 120


@dataclass
class EventRuntime:
    """记录事件时要用到的运行时。

    与登记表、任务状态一样由内核注入，工具自己不持有会话：
    事件是**当前会话**的一部分，会话一换，工具手上的那份就该跟着换。
    """

    session: Session
    # 当前正在写的那条 assistant 消息的下标（不含）。事件收到这里为止。
    current_index: int = 0
    # 让模型写摘要；返回原文。失败与空文本由调用方兜底。
    summarize: Callable[[Session, int, int], Awaitable[str]] | None = None
    is_cancelled: Callable[[], bool] = lambda: False


def event_boundary(session: Session, index: int) -> int:
    """事件能收口到哪条消息（不含）。

    排掉当前位置之后，再往前跳过连续的 user 消息：那些是「用户刚说的话」，
    是下一步的输入，收进事件就等于把这一步的题目收走了。
    """
    end = max(0, min(index, len(session.messages)))
    while end > 0 and session.messages[end - 1].role == "user":
        end -= 1
    return end


def _first_user_text(session: Session, start: int, end: int) -> str:
    """这一段里第一句用户原话，用来给事件起标题。"""
    for message in session.messages[start:end]:
        if message.role != "user":
            continue
        for part in message.parts:
            if part.type is PartType.TEXT and part.text.strip():
                return part.text.strip()
    return ""


def build_local_event(session: Session, start: int, end: int, kind: str) -> Event:
    """按 Part 里的真实记录汇编一条事件：动作、产物、失败项都是数出来的。

    这一份**一定出得来**：不请求模型、不解析自由文本，读的就是工具回报的
    `tool_name` / `tool_meta` / `tool_status`。读起来不如模型写的顺，
    但它是收口这条路的底线。
    """
    actions: list[str] = []
    artifacts: list[str] = []
    issues: list[str] = []
    for message in session.messages[start:end]:
        for part in message.parts:
            if part.type is PartType.TOOL and part.tool_name:
                if part.tool_name not in actions:
                    actions.append(part.tool_name)
                if part.tool_status is ToolStatus.FAILED:
                    issue = " ".join((part.tool_output or "没有写明原因").split())[:80]
                    if issue and issue not in issues:
                        issues.append(issue)
                meta = part.tool_meta if isinstance(part.tool_meta, dict) else {}
                for key in ("handle", "ref", "run_id"):
                    value = meta.get(key)
                    if isinstance(value, str) and value.strip() and value not in artifacts:
                        artifacts.append(value.strip())
            elif part.type is PartType.FILE and part.file_path:
                if part.file_path not in artifacts:
                    artifacts.append(part.file_path)
            elif part.type is PartType.PATCH and part.text:
                if part.text not in artifacts:
                    artifacts.append(part.text)

    title = " ".join(_first_user_text(session, start, end).split())[:EVENT_TITLE_MAX]
    if not title and actions:
        title = "工具：" + "、".join(actions[:3])
    if not title:
        title = f"第 {start + 1}–{end} 条"

    pieces = [f"覆盖第 {start + 1}–{end} 条消息"]
    if actions:
        pieces.append("调用 " + "、".join(actions))
    if artifacts:
        pieces.append("产物 " + "、".join(artifacts[:5]))
    if issues:
        pieces.append("未决 " + "；".join(issues[:3]))
    summary = "；".join(pieces)[:EVENT_SUMMARY_MAX]

    return Event(
        title=title,
        summary=summary,
        actions=actions,
        artifacts=artifacts,
        issues=issues,
        start=start,
        end=end,
        kind=kind,
        degraded=True,
    )


def record_local_event(session: Session, end: int, kind: str = "auto") -> Event | None:
    """在 `[event_end, end)` 上记一条本地事件，零模型请求。

    相邻的 auto/local 事件**合并**成一条：溢出是逐步发生的，一次挤出一条事件的话，
    事件层很快就变成另一份逐条列表，那就失去了「粗粒度」的意义。
    """
    end = min(end, len(session.messages))
    previous = session.events[-1] if session.events else None
    start = previous.end if previous else session.summary_upto
    if end <= start:
        return None
    if previous is not None and previous.kind in ("auto", "local") and kind in ("auto", "local"):
        merged = build_local_event(session, previous.start, end, previous.kind)
        merged.id = previous.id
        merged.created_at = previous.created_at
        session.events[-1] = merged
        session.touch()
        session.sanitize_events()
        return merged
    event = build_local_event(session, start, end, kind)
    session.add_event(event)
    session.sanitize_events()
    return event


def parse_event_text(text: str) -> tuple[str, str]:
    """从模型的输出里取标题与摘要。

    约定是第一行 `标题：…`，其余是摘要。模型没照格式写时，宁可把整段当摘要
    并用本地标题——格式不对不该让这次收口白费。
    """
    title = ""
    body: list[str] = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("标题"):
            for mark in ("：", ":"):
                if mark in stripped:
                    title = stripped.split(mark, 1)[1].strip()
                    break
            continue
        body.append(line)
    return title, "\n".join(body).strip()


def render_event_detail(
    session: Session,
    event: Event,
    offset: int = 0,
    limit: int = EVENT_RECALL_LIMIT,
) -> str:
    """一条事件的详情：元信息 + 摘要 + 原文的一段。

    `offset` 只作用于原文：摘要与产物就那么几行，翻页翻掉它们没有意义。
    """
    from .context import render_range

    lines = [
        f"{event.id} {event.title}",
        f"区间：第 {event.start + 1}–{event.end} 条　来源：{event.kind}"
        + ("（本地汇编）" if event.degraded else ""),
        f"摘要：\n{event.summary or '（没有摘要）'}",
    ]
    if event.actions:
        lines.append("动作：" + "、".join(event.actions))
    if event.artifacts:
        lines.append("产物：" + "、".join(event.artifacts))
    if event.issues:
        lines.append("未决：" + "；".join(event.issues))
    lines.append("原文：")
    rows = render_range(session, event.start, event.end).splitlines()
    lines.extend(rows[offset : offset + limit])
    done = offset + min(limit, max(0, len(rows) - offset))
    if len(rows) > done:
        lines.append(f"（还有 {len(rows) - done} 行，用 offset={done} 继续）")
    return "\n".join(lines)


class RecordEventTool(Tool):
    """把刚才做完的一段收口成一条事件。"""

    spec = ToolSpec(
        name="record_event",
        description=(
            "把刚做完的一段工作收口成一条事件（标题 + 摘要）。收口之后那一段原文不再进上下文，"
            "只留一行索引；需要细节时用 recall_event 按编号取回原文。"
            "一段工作告一段落、准备换下一件事时调用。"
            "title 与 summary 都给出时不会额外请求模型；只给 title 时由模型补摘要。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": f"不超过 {EVENT_TITLE_MAX} 字，写清这一段做了什么"},
                "summary": {
                    "type": "string",
                    "description": f"不超过 {EVENT_SUMMARY_MAX} 字：结果、产物、未决",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    event_argument = "event_runtime"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """收口一段：能不问模型就不问。"""
        runtime = arguments.get("event_runtime")
        if not isinstance(runtime, EventRuntime):
            return ToolResult(
                output="内核装配问题：record_event 需要事件运行时，但这次调用没有拿到。",
                failed=True,
            )
        session = runtime.session
        end = event_boundary(session, runtime.current_index)
        last_manual = next(
            (item for item in reversed(session.events) if item.kind == "manual"), None
        )
        start = last_manual.end if last_manual is not None else session.summary_upto
        if end <= start:
            return ToolResult(output="这一段还没有可收口的内容（当前这一步不算在内）。")

        title = " ".join(str(arguments.get("title") or "").split())[:EVENT_TITLE_MAX]
        summary = str(arguments.get("summary") or "").strip()[:EVENT_SUMMARY_MAX]
        if not title or not summary:
            text = ""
            if runtime.summarize is not None:
                try:
                    text = await runtime.summarize(session, start, end)
                except Exception:  # noqa: BLE001 - 请求、装配、解析任何一步失败都走兜底
                    text = ""
            if runtime.is_cancelled():
                return ToolResult(output="已中断，没有记录事件。", failed=True)
            model_title, model_summary = parse_event_text(text)
            title = title or model_title[:EVENT_TITLE_MAX]
            summary = summary or model_summary[:EVENT_SUMMARY_MAX]

        # 动作/产物/未决永远从 Part 里数，不让模型转述：它是两个入口
        # （模型写摘要、本地兜底）都该一致的那一部分。
        event = build_local_event(session, start, end, "manual")
        if title:
            event.title = title
        if summary:
            event.summary = summary
        event.degraded = not (title and summary)
        if event.degraded:
            # 标题或摘要缺一半：把本地那一份补上，别留半条事件。
            local = build_local_event(session, start, end, "manual")
            event.title = event.title or local.title
            event.summary = event.summary or local.summary

        # 手动事件吞掉它后面的 auto/local：那些是同一段内容的不同切法。
        session.events = [
            item
            for item in session.events
            if item.kind == "manual" or item.start < start
        ]
        session.add_event(event)
        session.sanitize_events()
        return ToolResult(
            output=(
                f"已记录 {event.id}：{event.title}"
                f"（第 {event.start + 1}–{event.end} 条，"
                f"产物 {len(event.artifacts)} 项，未决 {len(event.issues)} 项）"
            ),
            meta={"event": event.id, "start": event.start, "end": event.end},
        )


class RecallEventTool(Tool):
    """按编号取回一条事件：摘要、产物，以及那一段的原文。"""

    spec = ToolSpec(
        name="recall_event",
        description=(
            "不带参数时列出全部事件；带 event 编号时给出该事件的摘要、产物与原文。"
            "原文按 offset / limit 分页，默认 120 行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "事件编号，例如 evt_0003"},
                "offset": {"type": "integer", "description": "原文从第几行开始，默认 0"},
                "limit": {"type": "integer", "description": f"最多返回多少行，默认 {EVENT_RECALL_LIMIT}"},
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    session_argument = "session"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """列清单或读一条。"""
        session = arguments.get("session")
        events: list[Event] = list(session.events)
        if not events:
            return ToolResult(output="这份会话还没有事件。")
        wanted = str(arguments.get("event") or "").strip()
        if not wanted:
            from .context import event_line

            return ToolResult(
                output="\n".join(["本会话的事件：", *(event_line(item) for item in events)])
            )
        event = next((item for item in events if item.id == wanted), None)
        if event is None:
            available = "、".join(item.id for item in events)
            return ToolResult(
                output=f"没有 {wanted} 这条事件。可用的编号：{available}",
                failed=True,
            )
        offset = arguments.get("offset") or 0
        limit = arguments.get("limit") or EVENT_RECALL_LIMIT
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return ToolResult(output="参数 offset 应当是非负整数", failed=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return ToolResult(output="参数 limit 应当是正整数", failed=True)
        detail = render_event_detail(
            session, event, offset=offset, limit=min(limit, 400)
        )
        return ToolResult(output=detail, meta={"event": event.id, "start": event.start, "end": event.end})


def domain_tools() -> list[Tool]:
    """内核事件工具，装配顺序：写在前、读在后。"""
    return [RecordEventTool(), RecallEventTool()]
