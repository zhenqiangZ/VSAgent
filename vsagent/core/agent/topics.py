"""主题视图模型：把会话原文与事件记录折成界面上的一张张主题卡。

网页端画的是同一套三层结构（主题 → 步 → 工具），边界都取 `Event.cut` 的
(消息, Part) 位置，与装配器读的是同一组数，所以卡片上的分段不会与上下文里
发出去的那一段对不上。

这里只做纯计算：不 import textual、不写盘、不改事件。封存由内核决定，
展开与折叠由界面自己记，两边互不写回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import live
from ..models import Message, PartType, Session, Step

from .event_log import steps_from_messages

Position = tuple[int, int]


def position_key(position: Position) -> str:
    """位置当主题的键。

    用位置而不是事件 id：主题封存时 id 才会出现，而界面在封存之前就已经把它画出来了。
    位置在整个会话里不变，用户展开过的那个主题不会因为封存换一个键、把状态丢掉。
    """
    return f"{position[0]}:{position[1]}"


@dataclass
class TopicView:
    """界面要的一个主题：边界、计数器、结论，加未封存那一份的实时状态。"""

    key: str
    index: int
    title: str
    status: str
    start: Position
    end: Position
    steps: list[Step] = field(default_factory=list)
    summary: str = ""
    tools: list[str] = field(default_factory=list)
    calls: int = 0
    artifacts: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    sealed: bool = True
    degraded: bool = False
    # 只有未封存的那个有：`live.snapshot` 的 current，用来写「正在运行 dock」。
    current: dict[str, Any] | None = None

    def contains(self, position: Position) -> bool:
        """这个位置属不属于本主题。位置全序，比较两个端点就够。"""
        return self.start <= position < self.end


def step_seconds(messages: list[Message], step: Step) -> float:
    """这一步从 `STEP_START` 到下一步开始（或消息末尾）的秒数。

    口径与网页端一致：含模型生成与工具执行。工具自己没有记起止时间，
    硬把模型等待算成工具耗时会让「哪个工具慢」这件事看起来有数据支撑。
    """
    if step.message_index < 0 or step.message_index >= len(messages):
        return 0.0
    message = messages[step.message_index]
    begin = 0.0
    for part in message.parts[: step.part_start + 1]:
        if part.type is PartType.STEP_START:
            begin = part.timestamp
    if not begin:
        return 0.0
    stop = 0.0
    for part in message.parts[step.part_start + 1 :]:
        if part.type is PartType.STEP_START:
            stop = part.timestamp
            break
    if not stop and message.parts:
        stop = message.parts[-1].timestamp
    return max(0.0, stop - begin)


def topic_views(session: Session, *, running: bool) -> list[TopicView]:
    """按顺序列出全部主题，末尾带上还没封存的那一个。

    已封存的直接来自 `session.events`；尾部从最后一个切点算到会话末尾。
    边界连续覆盖，所以相邻两个主题不会重叠，也不会留缝。
    """
    views: list[TopicView] = []
    start: Position = (0, 0)
    for event in session.events:
        views.append(
            TopicView(
                key=position_key(start),
                index=event.topic_index,
                title=event.title,
                status=event.status,
                start=start,
                end=event.cut,
                steps=list(event.steps),
                summary=event.summary,
                tools=event.actions,
                calls=sum(len(step.tools) for step in event.steps),
                artifacts=event.artifacts,
                issues=event.issues,
                sealed=True,
                degraded=event.degraded,
            )
        )
        start = event.cut

    end: Position = (len(session.messages), 0)
    if _has_parts(session.messages, start, end):
        views.append(_tail_view(session, start, end, running=running))
    return views


def _has_parts(messages: list[Message], start: Position, end: Position) -> bool:
    """这一段里有没有可画的东西。空主题不画头，免得新会话一进来就多一行空标题。"""
    first = max(0, start[0])
    for index in range(first, min(end[0], len(messages) - 1) + 1):
        message = messages[index]
        low = start[1] if index == start[0] else 0
        high = end[1] if index == end[0] else len(message.parts)
        if high <= low:
            continue
        if message.parts[low:high]:
            return True
    return False


def _tail_view(session: Session, start: Position, end: Position, *, running: bool) -> TopicView:
    """还没封存的那一段。

    步优先用 `pending_steps`：那是每一步当场折出来的，零请求。重启之后它会是空的
    （进程在一轮中间中断），这时按原文里的 Part 回填，并把状态写成「未封存」——
    画成「进行中」等于告诉用户还在跑，而那个循环已经不在了。
    """
    steps = list(session.pending_steps)
    if not steps and not running:
        steps = steps_from_messages(session, start, end)
    snapshot = live.snapshot(session, running=running)
    current = snapshot.get("current") if running else None
    if running:
        # 运行中按快照取：它把还没记成步的那一轮工具调用也数了进去。
        tools = list(snapshot.get("tools") or [])
        calls = int(snapshot.get("calls") or 0)
    else:
        # 不跑的时候按回填出来的流水数。快照里的那份来自 `pending_steps`，
        # 重启之后是空的，拿它会把「3 步」与「0 个工具」写在同一行。
        tools = []
        calls = 0
        for step in steps:
            calls += len(step.tools)
            for name in step.tools:
                if name not in tools:
                    tools.append(name)
    artifacts: list[str] = []
    issues: list[str] = []
    for step in steps:
        for item in step.artifacts:
            if item not in artifacts:
                artifacts.append(item)
        for item in step.issues:
            if item not in issues:
                issues.append(item)
    return TopicView(
        key=position_key(start),
        index=len(session.events) + 1,
        title=session.topic_title,
        # 纯聊天的一轮没有流水，轮末也不封存；它已经说完了，画成「未封存」会让人
        # 去找一个不存在的记录。有步却没在跑的，才是进程断在一轮中间。
        status="running" if running else ("unsealed" if steps else "completed"),
        start=start,
        end=end,
        steps=steps,
        tools=tools,
        calls=calls,
        artifacts=artifacts,
        issues=issues,
        sealed=False,
        current=current,
    )
