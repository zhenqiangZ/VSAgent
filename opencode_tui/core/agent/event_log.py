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
3. **收口不花请求**。事件什么时候该收口，模型自己最清楚——它就站在那一段的末尾。
   所以标题与摘要在调用 `record_event` 时随参数一起给，内核不再为此另发一次请求；
   溢出时模型没有发言权，那就按 Part 本地汇编一条 `degraded=True` 的事件。
   两条路都不加请求，区别只在标题是谁写的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Event, PartType, Session, ToolStatus
from .tools import Tool, ToolResult, ToolSpec

# 标题与摘要的长度上限。**代码里截**，不只写在提示词里：
# 模型不会每次都照做，而这两段进的是每步都要发的索引。
EVENT_TITLE_MAX = 40
EVENT_SUMMARY_MAX = 600
# 本地标题里最多拼几段用户诉求。两段够看出「从哪做到哪」，再多就把标题
# 变成一份清单；超出的用「等 N 段」交代，不假装没发生。
TITLE_ASKS = 2
# `recall_event` 一次最多铺多少行原文。
EVENT_RECALL_LIMIT = 120


@dataclass
class EventRuntime:
    """记录事件时要用到的运行时。

    与登记表、任务状态一样由内核注入，工具自己不持有会话：
    事件是**当前会话**的一部分，会话一换，工具手上的那份就该跟着换。

    **没有 `summarize`**：标题与摘要由模型随调用一起给。收口的位置是模型自己
    选的，那一段的内容它刚刚看过，另起一次请求让它「再读一遍原文写摘要」
    只是把同一份上下文发两遍，还多等一个往返。
    """

    session: Session
    # 当前正在写的那条 assistant 消息的下标（不含）。事件收到这里为止。
    current_index: int = 0


def event_boundary(session: Session, index: int) -> int:
    """事件能收口到哪条消息（不含）。

    排掉当前位置之后，再往前跳过连续的 user 消息：那些是「用户刚说的话」，
    是下一步的输入，收进事件就等于把这一步的题目收走了。
    """
    end = max(0, min(index, len(session.messages)))
    while end > 0 and session.messages[end - 1].role == "user":
        end -= 1
    return end


def _user_texts(session: Session, start: int, end: int) -> list[str]:
    """这一段里每一句用户原话，按顺序。

    标题要描述**整段**，不是只描述开头。事件一合并就会跨过好几句诉求，
    只取第一句的话，后几句做的事在索引里一个字都不留（实测：合并到 12 条
    消息、10 个工具时，标题还写着第 1 句）。

    逐字重复的句子只算一句：用户连着说三次「继续」时，标题里出现三个「继续」
    既占地方又不提供信息。
    """
    texts: list[str] = []
    for message in session.messages[start:end]:
        if message.role != "user":
            continue
        for part in message.parts:
            if part.type is PartType.TEXT and part.text.strip():
                flat = " ".join(part.text.split())
                if flat and flat not in texts:
                    texts.append(flat)
    return texts


def _fit_title(text: str, suffix: str = "") -> str:
    """按标题上限裁 `text`，`suffix`（「等 N 段」）优先保住。

    先拼再截会把后缀截掉，读者就看不出这条事件到底覆盖了几段。
    """
    room = EVENT_TITLE_MAX - len(suffix)
    if len(text) > room:
        text = text[: max(1, room - 1)] + "…"
    return text + suffix


def _tool_list(actions: list[str]) -> str:
    """工具名列表：前三个 + 「等 N 个」。

    列全的话，十个工具就占掉半条索引行，把产物挤到截断线外面去。
    """
    head = "、".join(actions[:3])
    return head + (f" 等 {len(actions)} 个" if len(actions) > 3 else "")


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

    asks = _user_texts(session, start, end)
    if asks:
        title = _fit_title(
            "／".join(asks[:TITLE_ASKS]),
            f" 等 {len(asks)} 段" if len(asks) > TITLE_ASKS else "",
        )
    elif actions:
        suffix = "" if len(actions) <= 3 else f" 等 {len(actions)} 个"
        title = _fit_title("工具：" + "、".join(actions[:3]), suffix)
    else:
        title = f"第 {start + 1}–{end} 条"

    # 顺序是「最不能丢的在前」：索引行只给 160 字，超了从尾巴截。
    # 工具名最好补（标题里已经列了几个、recall_event 里也有全的），
    # 产物与未决才是下一步真的要用到的东西——它们排后面就会被截掉。
    pieces = [f"覆盖第 {start + 1}–{end} 条消息"]
    if artifacts:
        pieces.append("产物 " + "、".join(artifacts[:5]))
    if issues:
        pieces.append("未决 " + "；".join(issues[:3]))
    if actions:
        pieces.append("调用 " + _tool_list(actions))
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
            "把刚做完的一段工作收口成一条事件。收口之后那一段原文不再进上下文，"
            "只留一行索引；需要细节时用 recall_event 按编号取回原文。"
            "一段工作告一段落、准备换下一件事时调用。"
            "title 与 summary 都必填：收口的位置是你自己选的，这一段你刚看过，"
            "直接写下来即可——内核不会为了收口再发一次请求。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": f"不超过 {EVENT_TITLE_MAX} 字，写清这一段做了什么"},
                "summary": {
                    "type": "string",
                    "description": (
                        f"不超过 {EVENT_SUMMARY_MAX} 字，四段："
                        "做了什么（用了哪些工具）/ 结果与产物（handle、路径、关键数字，"
                        "写不准就写「未产出」，不要编）/ 遇到的问题 / 留给下一步"
                    ),
                },
            },
            "required": ["title", "summary"],
            "additionalProperties": False,
        },
    )
    event_argument = "event_runtime"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """收口一段：标题与摘要都由调用方给，这里只负责记账。"""
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
            # 不退回「让模型补一份」：那正是这里要取消的第二次请求。缺哪个说哪个，
            # 模型下一步补上就行——这条说明会作为工具结果回灌给它。
            missing = "title" if not title else "summary"
            return ToolResult(
                output=(
                    f"record_event 缺少 {missing}：标题与摘要都要给，"
                    "标题写「这一段做了什么」，摘要写结果、产物与未决。"
                    "原文还在，改完再调一次即可。"
                ),
                failed=True,
            )

        # 动作/产物/未决永远从 Part 里数，不让模型转述：它们是「这一段到底调了
        # 什么、留下了什么」的硬事实，模型写的摘要只负责解释，不负责记账。
        event = build_local_event(session, start, end, "manual")
        event.title = title
        event.summary = summary
        event.degraded = False

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
