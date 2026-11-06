"""主题头与步行：只负责画和响应点击，状态都存在消息区里。

两块都不持有展开状态：点击时把「哪一个主题 / 哪一步」交回消息区，由它决定
隐藏哪些 Part、卸载哪些消息。两处各记一份的话，重建之后必然对不上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.message import Message as TextualMessage
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
    "completed": "#4CAF50",
    "failed": "#C62828",
    "interrupted": "#FFB300",
    "running": "#4A9EFF",
    "unsealed": "#5A5A5A",
}

TITLE_LIMIT = 28
LINE_LIMIT = 72
# 收起主题的工具名最多占这么多格。名字是 ASCII，格数等于字符数；
# 那一行还要接结论，给多了结论就全被截掉。
TOOLS_LIMIT = 24
# 收起的主题把前几个工具名列出来：这是它「做了什么」的唯一线索，
# 展开之后步行的文字里已经有了，不必重复。
TOOLS_SHOWN = 3
# 主题下面那一行列几个产物。多出来的进右栏，这一行只回答「这一段交出了什么」。
ARTIFACT_LIMIT = 3


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


def tool_names(names: list[str]) -> str:
    """把工具名压成一行：最多三个，总宽不超过 `TOOLS_LIMIT`，余下的写 `+N`。

    只留三个是因为这一行还要装结论；`+N` 让「还有别的」这件事不丢。
    """
    shown: list[str] = []
    used = 0
    for name in names[:TOOLS_SHOWN]:
        cost = cell_len(name) + (2 if shown else 0)
        if used + cost > TOOLS_LIMIT:
            break
        shown.append(name)
        used += cost
    if not shown:
        return ""
    rest = len(names) - len(shown)
    return "、".join(shown) + (f" +{rest}" if rest else "")


def detail_color(topic: TopicView) -> str:
    """说明行的颜色：有未决项就是红色，进行中蓝色，其余次要文字。

    失败那一句与「正在运行 x」可能排在同一行，整行按失败涂红：
    一行里两种颜色读起来像两句话。
    """
    if topic.issues:
        return "#C62828"
    if topic.status == "running":
        return "#4A9EFF"
    return "#8B949E"


def detail_text(topic: TopicView, open_now: bool) -> str:
    """主题下面那一行。进行中的主题写当前动作，收起的主题写工具名与结论，其余写结论。

    留成模块级函数：它只读 `TopicView`，判据可以不起界面直接验这一段文字。
    """
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
        # 前面某一步失败过而这一轮还在跑，那一句必须放在最前面：
        # 尾巴上的「正在运行 x」会把读者留在「一切正常」的印象里。
        if topic.issues:
            head = f"失败：{topic.issues[0]} · {head}"
        return short(head, LINE_LIMIT)
    if not open_now and topic.tools:
        names = tool_names(topic.tools)
        if names:
            prefix = "本地记录 · " if topic.degraded else ""
            body = ""
            if topic.summary:
                room = max(12, LINE_LIMIT - len(names) - 5)
                body = f" · {prefix}{short(topic.summary, room)}"
            return f"工具 {names}{body}"
    if topic.issues:
        return short(f"失败：{topic.issues[0]}", LINE_LIMIT)
    if topic.summary:
        prefix = "本地记录 · " if topic.degraded else ""
        return short(f"{prefix}{topic.summary}", LINE_LIMIT)
    return ""


class TopicHeader(Container):
    """一个主题的标题、一行说明与一个工具开关。

    两层折叠各有一个入口：点标题行收起或展开整段，点最后那行开关一次展开或
    收起本主题全部工具输出。开关只在主题展开且有步时出现——收起的状态下工具卡
    本来就不在画面上，这个按钮没有可作用的对象。
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
    TopicHeader .topic-title { width: 1fr; height: auto; }
    TopicHeader .topic-detail { width: 1fr; height: auto; }
    TopicHeader .topic-tools { width: 1fr; height: 1; color: #5A5A5A; }
    TopicHeader .topic-tools:hover { color: #E0E0E0; }
    TopicHeader .topic-detail.-hidden,
    TopicHeader .topic-tools.-hidden { display: none; }
    """

    def __init__(self, topic: TopicView, pane: MessagesPane) -> None:
        super().__init__()
        self.topic = topic
        self.pane = pane
        self.add_class("topic-header")
        # 三块都是普通 Static：点哪一块都走同一个入口，只有开关那一块自己拦住点击。
        self._title = Static("", classes="topic-title", markup=False)
        self._detail = Static("", classes="topic-detail", markup=False)
        self._tools = Static("", classes="topic-tools", markup=False)
        self._write(topic, True, False)

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._detail
        yield self._tools

    def sync(self, topic: TopicView, open_now: bool, tools_open: bool) -> None:
        """换一份主题数据，或两层展开状态变了，重画这三块。"""
        self.topic = topic
        self._write(topic, open_now, tools_open)

    def _write(self, topic: TopicView, open_now: bool, tools_open: bool) -> None:
        self._title.update(self.render_topic(open_now))
        detail = self.detail_text(open_now)
        self._detail.update(detail)
        self._detail.styles.color = detail_color(topic)
        self._detail.set_class(not detail, "-hidden")
        has_tools = bool(topic.steps) and open_now
        self._tools.set_class(not has_tools, "-hidden")
        if has_tools:
            mark = "▾" if tools_open else "▸"
            self._tools.update(f"{mark} {'收起' if tools_open else '展开'}全部工具")

    def plain(self) -> str:
        """把画出来的几行拼成一段，供判据与日志使用。"""
        lines = [str(self._title.render())]
        detail = str(self._detail.render())
        if detail:
            lines.append(detail)
        return "\n".join(lines)

    def render_topic(self, open_now: bool) -> Text:
        """第一行标题与计数。"""
        topic = self.topic
        line = Text()
        line.append("▾ " if open_now else "▸ ", style="#5A5A5A")
        line.append(f"{topic.index:02d} ", style="#4A9EFF")
        line.append(short(topic.title, TITLE_LIMIT) or "新主题", style="bold")
        status = topic.status if topic.status in STATUS_MARK else "completed"
        line.append(f"  {STATUS_MARK[status]} {STATUS_LABEL[status]}", style=STATUS_COLOR[status])
        if topic.steps:
            line.append(f" · {len(topic.steps)} 步", style="#5A5A5A")
        if topic.calls:
            line.append(f" · {topic.calls} 次调用", style="#5A5A5A")
        if topic.artifacts:
            line.append(f" · {len(topic.artifacts)} 个产物", style="#5A5A5A")
        return line

    def detail_text(self, open_now: bool) -> str:
        """第二行。内容由模块级的 `detail_text` 决定，这里只补上当前主题。"""
        return detail_text(self.topic, open_now)

    def on_click(self, event: events.Click) -> None:
        """点标题或说明行切换整段折叠，点开关那一行切换本主题全部工具。"""
        event.stop()
        if event.widget is self._tools:
            self.pane.toggle_topic_tools(self.topic.key)
            return
        self.pane.toggle_topic(self.topic.key)


class ArtifactChip(Static):
    """一个可点的产物名。"""

    DEFAULT_CSS = """
    ArtifactChip { width: auto; height: 1; color: #4A9EFF; margin-right: 2; }
    ArtifactChip:hover { color: #E0E0E0; text-style: underline; }
    """

    class Picked(TextualMessage):
        """用户点了某个产物。"""

        def __init__(self, handle: str) -> None:
            super().__init__()
            self.handle = handle

    def __init__(self, handle: str) -> None:
        super().__init__(handle, markup=False)
        self.handle = handle

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Picked(self.handle))


class ArtifactRow(Horizontal):
    """主题下面那一行产物：这一段交出了什么，点一下在浏览器里打开。

    收起主题时它仍然在：收起的是过程，产物是结果，正是收起来之后最想看到的那一行。
    """

    DEFAULT_CSS = """
    ArtifactRow {
        height: 1;
        padding: 0 2;
        margin-left: 2;
    }
    /* 与主题头同一套挂法：挂在消息区那一层时补上那两列。 */
    ArtifactRow.-pane-row { margin-left: 4; }
    ArtifactRow .artifact-label { width: auto; height: 1; color: #5A5A5A; margin-right: 1; }
    ArtifactRow.-hidden { display: none; }
    """

    def __init__(self, topic: TopicView) -> None:
        super().__init__()
        self.topic = topic
        self.add_class("artifact-row")
        self._handles = tuple(topic.artifacts[:ARTIFACT_LIMIT])
        self._chips = [ArtifactChip(handle) for handle in self._handles]
        self.set_class(not self._handles, "-hidden")

    def compose(self) -> ComposeResult:
        yield Static("产物", classes="artifact-label")
        for chip in self._chips:
            yield chip

    def sync(self, topic: TopicView) -> None:
        """换一份主题数据；产物名单变了才重挂那几个名字。"""
        self.topic = topic
        handles = tuple(topic.artifacts[:ARTIFACT_LIMIT])
        if handles == self._handles:
            return
        if not self.is_mounted:
            # 还没挂上：换不掉子组件，等挂上之后那一次 sync 再换。
            return
        self._handles = handles
        for chip in self._chips:
            chip.remove()
        self._chips = [ArtifactChip(handle) for handle in handles]
        self.set_class(not handles, "-hidden")
        if self._chips:
            self.mount_all(self._chips)


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
