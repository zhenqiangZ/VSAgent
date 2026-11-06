"""主题头与步行：只负责画和响应点击，状态都存在消息区里。

两块都不持有展开状态：点击时把「哪一个主题 / 哪一步」交回消息区，由它决定
隐藏哪些 Part、卸载哪些消息。两处各记一份的话，重建之后必然对不上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.widgets import Static

from ..core.agent.topics import TopicView
from ..core.models import Step

if TYPE_CHECKING:
    from .messages_pane import MessagesPane

# 与工具卡同一套字形：空心的圆还没开始，实心在做，勾与叉是两种结局，
# 方块表示用户按停。
STATUS_MARK = {
    "completed": "✓",
    "failed": "✕",
    "interrupted": "■",
    "running": "●",
    "unsealed": "○",
}

STATUS_LABEL = {
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "running": "进行中",
    "unsealed": "未封存",
}

STATUS_COLOR = {
    "completed": "#5A5A5A",
    "failed": "#C62828",
    "interrupted": "#FFB300",
    "running": "#4A9EFF",
    "unsealed": "#5A5A5A",
}

TITLE_LIMIT = 28
LINE_LIMIT = 72


def format_seconds(seconds: float) -> str:
    """把秒数写成「0.4s」「12s」「2m03s」。

    十秒以内留一位小数：工具大多几秒就回来了，都写成「0s」「3s」的话，
    看不出哪个快哪个慢。
    """
    seconds = max(0.0, seconds)
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def short(text: str, limit: int) -> str:
    """压成一行，超长截断。终端里换行会把一张卡的层次弄乱。"""
    line = " ".join((text or "").split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


class TopicHeader(Static):
    """一个主题的标题行，外加一行实时状态或结论。

    点击切换这个主题的折叠。只认整行的点击，卡内没有别的可点元素。
    """

    DEFAULT_CSS = """
    TopicHeader {
        height: auto;
        padding: 0 2;
        margin: 1 0 0 0;
        background: #000000;
    }
    /* 挂在消息区那一层的行要自己补上助手消息的左内边距：消息里的行本来就在
       两列之后，两种挂法都从同一列开始，主题头才不会一深一浅。 */
    TopicHeader.-pane-row { margin-left: 2; }
    """

    def __init__(self, topic: TopicView, pane: MessagesPane) -> None:
        super().__init__(markup=False)
        self.topic = topic
        self.pane = pane
        self.add_class("topic-header")
        self.update(self.render_topic(True))

    def sync(self, topic: TopicView, open_now: bool) -> None:
        """换一份主题数据，或展开状态变了，重画两行。"""
        self.topic = topic
        self.update(self.render_topic(open_now))

    def render_topic(self, open_now: bool) -> Text:
        """第一行标题与计数，第二行当前动作或结论。"""
        topic = self.topic
        line = Text()
        line.append("▾ " if open_now else "▸ ", style="#5A5A5A")
        line.append(f"{topic.index:02d} ", style="#4A9EFF")
        line.append(short(topic.title, TITLE_LIMIT) or "新主题", style="bold")
        status = topic.status if topic.status in STATUS_MARK else "completed"
        line.append(f"  {STATUS_MARK[status]} {STATUS_LABEL[status]}", style=STATUS_COLOR[status])
        if topic.steps:
            line.append(f" · {len(topic.steps)} 步", style="#5A5A5A")
        if topic.tools:
            line.append(f" · {len(topic.tools)} 个工具", style="#5A5A5A")
        if topic.artifacts:
            line.append(f" · {len(topic.artifacts)} 个产物", style="#5A5A5A")
        detail = self.detail_text()
        if detail:
            line.append("\n")
            line.append("   " + detail, style=STATUS_COLOR[status] if status != "completed" else "#8B949E")
        return line

    def detail_text(self) -> str:
        """第二行。进行中的主题写当前动作，其余写结论与失败原因。"""
        topic = self.topic
        if topic.status == "running":
            current = topic.current or {}
            phase = str(current.get("phase") or "")
            tool = str(current.get("tool") or "")
            if phase == "waiting" and tool:
                head = f"等待确认：{tool}"
            elif phase == "tool" and tool:
                head = f"正在运行 {tool}"
            else:
                head = "模型正在生成"
            if topic.steps:
                head = f"{head} · 已 {len(topic.steps)} 步"
            return short(head, LINE_LIMIT)
        if topic.issues:
            return short(f"失败：{topic.issues[0]}", LINE_LIMIT)
        if topic.summary:
            prefix = "本地记录 · " if topic.degraded else ""
            return short(f"{prefix}{topic.summary}", LINE_LIMIT)
        return ""

    def on_click(self, event: events.Click) -> None:
        """点标题行切换折叠。"""
        event.stop()
        self.pane.toggle_topic(self.topic.key)


class StepRow(Static):
    """一步一行：序号、做了什么、耗时、成败。

    点击收起或展开这一步的工具卡。行本身不藏起来，否则收起之后就没有入口再打开。
    """

    DEFAULT_CSS = """
    StepRow {
        height: 1;
        padding: 0 2;
        /* 比主题头深一级，比工具卡浅一级：缩进就是三层的关系。 */
        margin: 0 0 0 2;
        color: #8B949E;
    }
    StepRow.-pane-row { margin-left: 4; }
    """

    def __init__(self, step: Step, seconds: float, pane: MessagesPane) -> None:
        super().__init__(markup=False)
        self.step = step
        self.seconds = seconds
        self.pane = pane
        self.add_class("step-row")
        self.update(self.render_step(True))

    def sync(self, step: Step, seconds: float, open_now: bool) -> None:
        self.step = step
        self.seconds = seconds
        self.update(self.render_step(open_now))

    def render_step(self, open_now: bool) -> Text:
        step = self.step
        line = Text()
        line.append("▾ " if open_now else "▸ ", style="#5A5A5A")
        line.append(f"{step.seq:02d} ", style="#5A5A5A")
        line.append(short(step.intent or "、".join(step.tools), LINE_LIMIT), style="#8B949E")
        if self.seconds:
            line.append(f" · {format_seconds(self.seconds)}", style="#5A5A5A")
        if step.issues:
            line.append("  ✕ 失败", style="#C62828")
        else:
            line.append("  ✓", style="#5A5A5A")
        return line

    def on_click(self, event: events.Click) -> None:
        """点这一行，收起或展开它的工具卡。"""
        event.stop()
        self.pane.toggle_step(self.step.message_index, self.step.part_start)
