from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable, cast

from textual import events
from textual.app import ComposeResult
from textual.containers import Container, ScrollableContainer, Vertical
from textual.scrollbar import ScrollTo
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Markdown, Static

from ..core.models import Message, Part, PartType, ToolStatus

if TYPE_CHECKING:
    from ..app import OpenCodeApp

STATUS_ICON = {
    ToolStatus.PENDING: "○",
    ToolStatus.AWAITING: "◐",
    ToolStatus.RUNNING: "●",
    ToolStatus.COMPLETED: "◆",
    ToolStatus.FAILED: "✕",
}

STATUS_TEXT = {
    ToolStatus.PENDING: "等待",
    ToolStatus.AWAITING: "询问",
    ToolStatus.RUNNING: "执行中",
    ToolStatus.COMPLETED: "完成",
    ToolStatus.FAILED: "失败",
}

# 卡片头上的工具图标。全是单格宽的字形（`☰` 是两格，没用它）：卡片头是一行文字，
# 混进一个双宽字形会让后面整段右移一格，几张卡叠在一起就看得出参差。
# 同一类工具给同一个图标——一眼分得出「这是检索」还是「这是算」比分清具体是哪个更重要。
TOOL_ICON = {
    "bash": "$",
    "read": "▤",
    "write": "✎",
    "patch": "◇",
    "grep": "⌕",
    "list_workspace": "≡",
    "describe_entry": "?",
    "switch_receptor": "⇄",
    "search_targets": "⌕",
    "fetch_target": "⇣",
    "fetch_structure": "⇣",
    "search_actives": "⌕",
    "find_analogs": "≈",
    "describe_molecule": "⬡",
    "search_pdb": "⌕",
    "search_literature": "⌕",
    "search_web": "⌕",
    "fetch_webpage": "⇣",
    "import_documents": "⇥",
    "search_corpus": "⌕",
    "read_source": "▤",
    "import_structure": "⇥",
    "import_ligandset": "⇥",
    "prepare_receptor": "⚙",
    "prepare_ligands": "⚙",
    "dock": "⊙",
    "write_run": "▣",
    "open_viewer": "◎",
}


def tool_icon(tool_name: str) -> str:
    """工具在卡片头上的图标。没登记过的工具用一个中性符号。"""
    return TOOL_ICON.get(tool_name, "◆")


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


# 工具输出最多显示这么多行，超出的部分折成一行提示，免得长输出把消息区整个刷过去。
MAX_OUTPUT_LINES = 6

# 超过上界时开头与末尾各留几行。命令的结论（报错、退出码）写在输出的末尾，
# 只留开头的话跑失败的 bash 只看得到命令本身，看不到为什么失败。
OUTPUT_HEAD_LINES = 3
OUTPUT_TAIL_LINES = 3

# 流式文本两次重写之间的最短间隔。窗口里累积的文本由 flush_text 写出来。
# 正文（Markdown 重解析）与思考块（Static 整段重排）共用这一个窗口。
TEXT_REFRESH_SECONDS = 0.05

# 打开会话时先建出最后这么多条消息，更早的那些留到滚到顶端时再补。
# 一段长会话里每一段正文、每一张工具卡各自都有子树，正文的 Markdown 还要解析一遍，
# 几千个组件的创建全挤在打开那一瞬间，界面会停住。
HISTORY_PAGE = 30

# 滚动位置离顶端还剩这么多行就去补下一批，用户滚到顶时内容已经在那儿了。
LOAD_MORE_MARGIN = 3

# 挂载消息条数的上界。滚动每一帧都要遍历全部已挂载的组件重排一遍（Textual 的
# `ScrollableContainer` 没有终端 scroll region 增量滚动），挂载越多滚得越慢：实测
# 30 条 62 毫秒、200 条 123 毫秒。超过这个上界就把远离视口那一端裁掉，滚动成本
# 于是有上限，翻阅多长的历史都一样流畅。
MAX_MOUNTED = 90


class CoalescedText:
    """流式文本的合并窗口。

    正文与思考块面对的是同一件事：模型每吐几个字就来一次更新，每次都要把整段
    重新写一遍——Markdown 要重解析，Static 要重排整段。一段回复下来是平方级的
    开销（实测正文一轮里重解析了 36 次）。两次重写之间隔一个短窗口，窗口里连着
    来的更新合并成一次；一轮结束时由 `MessagesPane.flush_text` 把最后一份立刻写出去。

    子类只管两件事：`__init__` 里调一次 `_start_window` 记下初始文本，
    再实现 `write_text` 把文本真的画出来。
    """

    def _start_window(self, text: str) -> None:
        """记下初始文本，当作「已经写出去的那一份」。"""
        self._shown_text = text
        self._latest_text = text
        self._refresh_timer: Timer | None = None

    def queue_text(self, text: str) -> None:
        """收到新文本，等这个窗口结束再写。"""
        if text == self._latest_text:
            return
        self._latest_text = text
        if self._refresh_timer is None:
            self._refresh_timer = self.set_timer(TEXT_REFRESH_SECONDS, self.flush_text)

    def flush_text(self) -> None:
        """把窗口里累积的最新文本立刻写出去。"""
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
        if self._latest_text == self._shown_text:
            return
        self._shown_text = self._latest_text
        self.write_text(self._shown_text)

    def write_text(self, text: str) -> None:
        """子类负责把文本真的画出来。"""
        raise NotImplementedError

    def on_unmount(self) -> None:
        """卸载时停掉还没到点的窗口。"""
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None


class TextPartView(CoalescedText, Markdown):
    """文本 Part，按 Markdown 渲染。

    流式更新走 `CoalescedText` 的合并窗口：两次重解析之间隔一个短窗口，
    窗口里连着来的更新合并成一次。
    """

    def __init__(self, part: Part) -> None:
        super().__init__(part.text, classes="part part-text")
        self.part_id = part.id
        self._start_window(part.text)

    def sync(self, part: Part) -> None:
        """收到新文本，等这个窗口结束再重解析。"""
        self.queue_text(part.text)

    def write_text(self, text: str) -> None:
        """把文本写进 Markdown。"""
        self.update(text)


class FoldBlock(CoalescedText, Container):
    """可折叠的块，用于思考过程。

    正文也走 `CoalescedText` 的合并窗口：思考块是整段 `Static` 重排，流式时每来
    一小段就重排一次，比正文重解析 Markdown 还贵，而它默认还是收起的——重排半天
    用户一个字都看不见。
    """

    DEFAULT_CSS = """
    FoldBlock {
        height: auto;
        border-left: thick #3A3A3A;
        padding: 0 1;
    }
    FoldBlock .fold-header { color: #6E7681; }
    FoldBlock .fold-body { color: #8B949E; }
    FoldBlock .fold-body.-hidden { display: none; }
    """

    def __init__(self, title: str, classes: str = "") -> None:
        super().__init__(classes=classes)
        self._title = title
        self._expanded = False
        self._header = Static(self._header_text(), classes="fold-header", markup=False)
        self._body = Static("", classes="fold-body -hidden", markup=False)
        self._start_window("")

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def _header_text(self) -> str:
        symbol = "▾" if self._expanded else "▸"
        hint = "点击折叠" if self._expanded else "点击展开"
        return f"{symbol} {self._title}（{hint}）"

    def on_click(self, event: events.Click) -> None:
        """点击标题时切换展开状态。

        只认标题行：正文展开之后点它多半是想读或选中，整段收起来是把人赶走。
        """
        if event.widget is not self._header:
            return
        self._expanded = not self._expanded
        self._header.update(self._header_text())
        self._body.set_class(not self._expanded, "-hidden")

    def set_body(self, text: str) -> None:
        """收到新正文，等这个窗口结束再重排。"""
        self.queue_text(text)

    def write_text(self, text: str) -> None:
        """把正文真的画进折叠块。"""
        self._body.update(text)


class ReasoningPartView(FoldBlock):
    """思考 Part，蓝色边框折叠块。"""

    def __init__(self, part: Part) -> None:
        super().__init__(title="思考过程", classes="part part-reasoning")
        self.part_id = part.id

    def sync(self, part: Part) -> None:
        """刷新思考正文。"""
        self.set_body(part.text)


class ToolCard(Container):
    """工具调用卡片，左侧竖条按状态着色，输出截断且可收起。

    卡片上有两级折叠，各自管一段：

    - 标题行（`▾/▸`）收起整段输出。点标题行才切换，点正文什么都不做——
      用户点一下输出多半只是想选中它读一眼，整段消失是最烦人的一种「帮忙」。
    - 省略行（`▾ 中间省略 N 行 · 点开看全文`）只展开被折掉的中间部分，点一下换回「收起」。
      没有这一条，长输出被折掉的那几十行在界面上就再也看不到了，只能去翻 trace。
    """

    DEFAULT_CSS = """
    ToolCard {
        height: auto;
        border-left: thick #555555;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    ToolCard.-running { border-left: thick #4A9EFF; }
    ToolCard.-awaiting { border-left: thick #FFB300; }
    ToolCard.-completed { border-left: thick #2E7D32; }
    ToolCard.-failed { border-left: thick #C62828; }
    ToolCard .tool-header { color: #9AA0A6; }
    ToolCard.-running .tool-header { color: #4A9EFF; }
    ToolCard.-awaiting .tool-header { color: #FFB300; }
    ToolCard.-completed .tool-header { color: #66BB6A; }
    ToolCard.-failed .tool-header { color: #EF5350; }
    ToolCard .tool-output { color: #6E7681; }
    ToolCard .tool-tail { color: #6E7681; }
    ToolCard .tool-more { color: #4A9EFF; height: 1; }
    ToolCard .tool-output.-hidden,
    ToolCard .tool-tail.-hidden,
    ToolCard .tool-more.-hidden { display: none; }
    """

    def __init__(self, part: Part) -> None:
        super().__init__(classes="part part-tool")
        self.part_id = part.id
        self._part = part
        self._show_output = True
        self._expanded = False
        # 执行耗时：只认亲眼看到的那一段。卡片是完成态建出来的（打开旧会话）时
        # `part.timestamp` 是几天前，减出来没有意义，宁可不显示。
        self._started_at: float | None = None
        self._elapsed: float | None = None
        self._timer: Timer | None = None
        self._header = Static(self._header_text(), classes="tool-header", markup=False)
        self._output = Static("", classes="tool-output", markup=False)
        self._more = Static("", classes="tool-more", markup=False)
        self._tail = Static("", classes="tool-tail", markup=False)
        self._refresh()

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._output
        yield self._more
        yield self._tail

    def _header_text(self) -> str:
        status = self._part.tool_status or ToolStatus.PENDING
        mark = "▾" if self._show_output else "▸"
        return (
            f"{mark} {tool_icon(self._part.tool_name)} {self._part.tool_name or 'tool'}"
            f" · {STATUS_TEXT[status]}{self._elapsed_text()}"
            f"{self._permission_text()}{self._detail_text()}"
        )

    def _elapsed_text(self) -> str:
        """执行耗时。执行中跟着秒表走，结束后停在最终值。"""
        if self._started_at is not None:
            return f" {format_seconds(time.time() - self._started_at)}"
        if self._elapsed is not None:
            return f" {format_seconds(self._elapsed)}"
        return ""

    def _permission_text(self) -> str:
        """判定那一层留下的说明。

        只有按规则放行、用户点过允许、或正在等答复时才有内容，
        默认放行的调用这里是空的，卡片上不多出一段。
        """
        note = self._part.permission
        if not note:
            return ""
        if len(note) > 40:
            note = note[:37] + "..."
        return f"  {note}"

    def _detail_text(self) -> str:
        if not self._part.tool_input:
            return ""
        first_key = next(iter(self._part.tool_input))
        value = str(self._part.tool_input[first_key])
        if len(value) > 40:
            value = value[:37] + "..."
        return f"  {first_key}={value}"

    def _split_output(self) -> tuple[str, str, int]:
        """把输出拆成「开头、末尾、被省略的行数」。

        命令的结论（报错、退出码）写在输出的末尾，只留开头会把结论切掉，
        跑失败的 bash 就只剩命令本身，看不出为什么失败。
        """
        lines = self._part.tool_output.splitlines()
        if len(lines) <= MAX_OUTPUT_LINES:
            return self._part.tool_output, "", 0
        hidden = len(lines) - OUTPUT_HEAD_LINES - OUTPUT_TAIL_LINES
        return (
            "\n".join(lines[:OUTPUT_HEAD_LINES]),
            "\n".join(lines[-OUTPUT_TAIL_LINES:]),
            hidden,
        )

    def _output_text(self) -> str:
        head, _tail, hidden = self._split_output()
        if hidden == 0:
            return self._part.tool_output
        return head

    def _tail_text(self) -> str:
        _head, tail, hidden = self._split_output()
        if hidden == 0:
            return ""
        if self._expanded:
            # 展开时中间那一段接在省略行后面，省略行本身留在原地：
            # 挪到整段末尾的话，展开一条几百行的输出之后「收起」就滚出屏幕了。
            lines = self._part.tool_output.splitlines()
            return "\n".join(lines[OUTPUT_HEAD_LINES:])
        return tail

    def _more_text(self) -> str:
        _head, _tail, hidden = self._split_output()
        if hidden == 0:
            return ""
        if self._expanded:
            return "▴ 收起"
        return f"▾ 中间省略 {hidden} 行 · 点开看全文"

    def _refresh(self) -> None:
        """按当前状态把四块内容与可见性一起写好。"""
        output = self._output_text()
        tail = self._tail_text()
        more = self._more_text()
        self._header.update(self._header_text())
        self._output.update(output)
        self._tail.update(tail)
        self._more.update(more)
        # 还没输出的卡片（等待、执行中）不留一行空白：空 Static 的 auto 高度是 1 行，
        # 每张卡多一行，几张卡叠起来就是一屏的空白。
        self._output.set_class(not self._show_output or not output, "-hidden")
        self._tail.set_class(not self._show_output or not tail, "-hidden")
        self._more.set_class(not self._show_output or not more, "-hidden")

    def _track_elapsed(self, status: ToolStatus) -> None:
        """执行中起一个一秒一跳的秒表，离开执行中就把最终值定下来。"""
        if status is ToolStatus.RUNNING:
            if self._started_at is None:
                self._started_at = self._part.timestamp or time.time()
                self._elapsed = None
                self._timer = self.set_interval(1.0, self._tick)
        elif self._started_at is not None:
            self._elapsed = max(0.0, time.time() - self._started_at)
            self._started_at = None
            if self._timer is not None:
                self._timer.stop()
                self._timer = None

    def _tick(self) -> None:
        """秒表每跳一次只改标题行。"""
        self._header.update(self._header_text())

    def sync(self, part: Part) -> None:
        """刷新工具状态与输出。"""
        self._part = part
        status = part.tool_status or ToolStatus.PENDING
        self._track_elapsed(status)
        self.set_class(status is ToolStatus.RUNNING, "-running")
        self.set_class(status is ToolStatus.AWAITING, "-awaiting")
        self.set_class(status is ToolStatus.COMPLETED, "-completed")
        self.set_class(status is ToolStatus.FAILED, "-failed")
        self._refresh()

    def on_click(self, event: events.Click) -> None:
        """只有标题行与省略行响应点击，点正文不动。"""
        if event.widget is self._header:
            self.set_output_visible(not self._show_output)
        elif event.widget is self._more:
            self._expanded = not self._expanded
            self._refresh()

    def set_output_visible(self, visible: bool) -> None:
        """控制工具输出是否显示（对应 /details 开关与点标题行收起）。"""
        self._show_output = visible
        self._refresh()

    def on_unmount(self) -> None:
        """卡片被裁掉时停掉秒表，别让定时器去打一个已经摘掉的组件。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


class SimplePartView(Static):
    """用一个 Static 渲染的简单 Part。"""

    # 与正文、思考块、工具卡的文字起始列取齐。正文那一类（Markdown）自带两列内边距，
    # 这里不补的话，patch 摘要、引用文件、step 分隔线这类行会比同一条消息里别的行左移两列。
    DEFAULT_CSS = """
    SimplePartView {
        height: auto;
        padding: 0 2;
    }
    """

    def __init__(self, part: Part) -> None:
        super().__init__(
            self.render_part_text(part),
            classes=f"part part-{part.type.value}",
            markup=False,
        )
        self.part = part
        self.part_id = part.id

    def sync(self, part: Part) -> None:
        """刷新显示文本。"""
        self.part = part
        self.update(self.render_part_text(part))

    def on_click(self, event: events.Click) -> None:
        """子 agent 的派发标记可以点：直接进那个子 agent。

        标记里记着会话编号（`Part.text`），不靠名字反查——名字可以重命名，
        编号不会。会话被删掉时什么都不做，标记仍然留在聊天记录里。
        """
        if self.part.type is not PartType.AGENT or not self.part.text:
            return
        session = self.app.session_manager.get_session(self.part.text)
        if session is None:
            return
        event.stop()
        self.app.open_subagent(session.id)

    @staticmethod
    def render_part_text(part: Part) -> str:
        if part.type is PartType.FILE:
            preview = f"  {len(part.text.splitlines())} 行" if part.text else ""
            return f"▤ {part.file_path or part.text}{preview}"
        if part.type is PartType.AGENT:
            return f"◆ 已派发子 agent「{part.agent_name}」 · 点击进入"
        if part.type is PartType.STEP_START:
            return f"── step {part.text or 'start'} " + "─" * 20
        if part.type is PartType.STEP_FINISH:
            return f"── step {part.text or 'finish'} " + "─" * 20
        if part.type is PartType.PATCH:
            stat = (
                f"  (+{part.patch_added} -{part.patch_removed})"
                if part.patch_added or part.patch_removed
                else ""
            )
            return f"◇ patch  {part.text or part.file_path}{stat}"
        if part.type is PartType.SUBTASK:
            return f"◇ 子任务 [{part.agent_name or 'subagent'}] {part.text}"
        if part.type is PartType.RETRY:
            return f"⟳ 重试 {part.text}"
        if part.type is PartType.COMPACTION:
            return f"░ 上下文已压缩 {part.text}"
        if part.type is PartType.ERROR:
            return f"■ {part.text}"
        if part.type is PartType.SUMMARY:
            return f"◆ 收尾总结\n{part.text}"
        return part.text


class PatchPartView(SimplePartView):
    """补丁 Part，点击后打开全屏 Diff。"""

    def on_click(self) -> None:
        """点击后打开 Diff 覆盖层。"""
        app = cast(OpenCodeApp, self.app)
        app.show_diff(self.part_text)

    @property
    def part_text(self) -> str:
        """补丁内容文本。"""
        return str(self.content)


class MessageView(Container):
    """一条消息的容器，内部按 Part 顺序堆叠子组件。

    用户消息画成一条对话框：浅灰底、最左侧一条蓝线；回复不画边框也不画角色标签，
    只靠左内边距与用户消息里的正文起始列保持一致。
    """

    DEFAULT_CSS = """
    MessageView { height: auto; margin-bottom: 1; }
    MessageView .message-parts { height: auto; }
    MessageView.message-user {
        background: #1E1E1E;
        border-left: thick #4A9EFF;
        /* 顶部留一行：正文不贴着气泡顶边，左侧那条蓝线覆盖这一行，线上沿于是高过文字。
           底部不再另加内边距：`.part-text` 的 margin-bottom 已经留出一行 */
        padding: 1 1 0 1;
    }
    MessageView.message-assistant { padding-left: 2; }
    /* 派发标记不是用户说的话。它仍然是一条 user 消息（翻译层把它翻成空，
       不进模型上下文），但视觉上必须去掉气泡底色与左侧蓝线，
       否则看起来就像用户自己发了一句「◆ 已派发子 agent…」。 */
    MessageView.message-notice {
        background: transparent;
        border: none;
        /* 与正文左边界取齐：正文是容器 2 列 + Markdown 自带 2 列，
           这里补上容器这 2 列，SimplePartView 自己还有 2 列。 */
        padding: 0 0 0 2;
    }
    """

    def __init__(self, message: Message, pane: MessagesPane) -> None:
        # 只有派发标记的消息不是「用户说了什么」：给它单独的样式类，
        # 让气泡的底色与蓝边不套在它身上。
        notice = bool(message.parts) and all(
            part.type is PartType.AGENT for part in message.parts
        )
        classes = f"message-view message-{message.role}"
        if notice:
            classes += " message-notice"
        super().__init__(classes=classes)
        self.message = message
        self.pane = pane
        self.parts_box: Vertical | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="message-parts") as box:
            self.parts_box = box
            for part in self.message.parts:
                if part.id in self.pane._part_views:
                    continue
                yield self.pane._build_and_register(part)


class NewMessagesHint(Static):
    """消息区底部的「有新内容」提示条。

    用户往上翻历史时不跟随，新内容还在下面长。没有这条提示的话，界面上一点动静
    都没有，长回合里用户不知道下面有没有东西；有了它，点一下就能回到底部。

    停靠在消息区底部（`dock: bottom`），所以它跟着视口走，不随内容滚动。
    """

    DEFAULT_CSS = """
    NewMessagesHint {
        dock: bottom;
        height: 1;
        display: none;
        background: #16212E;
        color: #4A9EFF;
        content-align: center middle;
    }
    NewMessagesHint.-visible { display: block; }
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__("↓ 有新内容 · 点这里回到底部", id=id, markup=False)

    def on_click(self) -> None:
        """点一下回到底部，跟随随之恢复。"""
        parent = self.parent
        if isinstance(parent, MessagesPane):
            parent.scroll_to_bottom()


class MessagesPane(ScrollableContainer):
    """消息列表，支持增量更新与自动滚动到底部。

    up/down/pageup/pagedown/home/end 由 ScrollableContainer 自带，这里只改动作的含义：
    手动滚动时停止自动跟随，滚回底部再恢复。
    """

    def __init__(self, on_removed: Callable[[], None] | None = None) -> None:
        super().__init__(id="messages-pane")
        self._on_removed = on_removed
        self._message_views: dict[str, MessageView] = {}
        self._part_views: dict[str, Widget] = {}
        self._follow = True
        self._show_details = True
        self._show_thinking = True
        self._tool_cards: list[ToolCard] = []
        self._reasoning_views: list[ReasoningPartView] = []
        self._pending_parts: list[tuple[str, Part]] = []
        # 当前会话的全部消息（与 Session.messages 是同一个列表），以及界面上已经建出来
        # 的那一段的起止索引 `[mounted_start, mounted_end)`。只有这一段是挂载着的，
        # 更早与更晚的消息都留在 `_messages` 里、不建组件，滚近时再补挂、滚远时再裁掉。
        self._messages: list[Message] = []
        self._mounted_start = 0
        self._mounted_end = 0
        self._loading = False
        self._was_at_bottom = False
        self._history_hint: Static | None = None
        self._new_hint: NewMessagesHint | None = None

    def compose(self) -> ComposeResult:
        """底部那条「有新内容」提示。停靠的组件不参与内容滚动，也不进窗口索引。"""
        self._new_hint = NewMessagesHint(id="new-messages")
        yield self._new_hint

    def on_unmount(self) -> None:
        """自己离开界面时通知外层：从这一刻起界面不再接后端事件。

        界面自己取消订阅要晚一步。Textual 卸载一棵子树时先把子组件摘掉、再把卸载
        事件派发给界面本身（`Widget._message_loop_exit` 里 `parent._nodes._remove(self)`
        在 `_dispatch_message(events.Unmount())` 之后）。中间这一小段里，生成任务那个
        协程可能正好从 `await` 里醒过来发一次事件，处理函数按类型去找消息区就会抛
        `NoMatches`，被记成 worker 失败。实测在生成中途退出，四十轮里能碰上两次。
        """
        if self._on_removed is not None:
            self._on_removed()

    def _on_focus(self, event: events.Focus) -> None:
        """获得焦点时不重画整块消息区。

        `Widget._on_focus` 会调一次不带区域的 `self.refresh()`，把消息区整块（二十多行）
        标成脏区域。消息区的样子不随焦点变化：应用自己的 tcss 里没有 `:focus` 规则，
        Textual 给 `ScrollableContainer` 与 `ScrollBar` 的默认样式里也没有。
        这次重画只会让终端把没变的格子重写一遍，在 Windows 上还要分成几块送达。
        `prevent_default` 会拦住基类的同名处理函数，基类另外那两件事在这里补上。
        """
        event.prevent_default()
        self.has_focus = True
        if self.parent is not None:
            self.parent.post_message(events.DescendantFocus(self))

    def _on_blur(self, event: events.Blur) -> None:
        """失去焦点时不重画整块消息区，理由同 `_on_focus`。"""
        event.prevent_default()
        self.has_focus = False
        if self.parent is not None:
            self.parent.post_message(events.DescendantBlur(self))

    def watch_has_focus(self, _has_focus: bool) -> None:
        """焦点进出消息区时只重算消息区自己，不重算整棵子树。

        `Widget.watch_has_focus` 走的是 `App.update_styles(self)`，会把整棵子树都重算一遍。
        消息区里每一段正文（Markdown 自己还铺着一棵子树）、每一张工具卡各自都有子树，
        一段会话下来是几千个组件：实测 296 条消息、4435 个组件时，一次重算要 2.6 到 3.9 秒，
        而消息区在点一下就获得焦点、点回输入框又失去焦点。样式表里没有任何一条规则靠焦点取值
        （应用自己的 tcss 里只有启动界面那三条 `:focus-within`），需要跟着焦点变的只有滚动条，
        所以重算名单里只放消息区自己，`update_nodes` 会自动带上它的滚动条。
        """
        self.app.stylesheet.update_nodes([self], animate=True)

    def add_message(self, message: Message) -> MessageView:
        """添加一条消息到列表末尾。

        流式生成时新消息总是接在会话末尾，调用方已把它放进 `session.messages`（也就是
        `self._messages`）。这里建出视图挂到末尾，并把窗口下沿推进到包含这条新消息。
        """
        view = MessageView(message, self)
        self._message_views[message.id] = view
        self.mount(view)
        # 窗口下沿贴着会话末尾：流式追加时用户正在底部，新消息进来窗口就跟着延长。
        self._mounted_end = len(self._messages)
        for part in message.parts:
            self.add_part(message.id, part)
        self.call_after_refresh(self._flush_pending)
        self._auto_scroll()
        # 新消息把挂载条数顶过上限时，裁掉远离视口的顶端，保持滚动成本有界。
        self._maintain_window_down()
        return view

    def add_part(self, message_id: str, part: Part) -> Widget | None:
        """为消息追加一个 Part 组件。"""
        if part.id in self._part_views:
            self.update_part(part)
            return self._part_views[part.id]
        view = self._message_views.get(message_id)
        if view is None:
            return None
        box = view.parts_box
        if box is None or not box.is_attached:
            self._pending_parts.append((message_id, part))
            return None
        widget = self._build_and_register(part)
        box.mount(widget)
        self._auto_scroll()
        return widget

    def update_part(self, part: Part) -> None:
        """用最新数据刷新某个 Part 组件。"""
        widget = self._part_views.get(part.id)
        if widget is None:
            return
        widget.sync(part)
        self._auto_scroll()

    def flush_text(self) -> None:
        """把还压在合并窗口里的流式文本立刻写出来。

        一轮生成结束时调用：最后一次更新多半还没到点，不写出来的话屏幕上就少一段，
        要等到下一次有更新才补上。正文与思考块用的是同一个窗口，这里一并收尾。
        """
        for widget in self._part_views.values():
            if isinstance(widget, CoalescedText):
                widget.flush_text()

    def rebuild(self, messages: list[Message]) -> None:
        """按会话内容重建列表（切换会话时使用）。

        只建出最后一段，更早的那些留到用户滚到顶端时再补一批。一段几百条消息的会话
        全建出来要几千个组件，建完还要等布局算完，打开会话的那几秒就停在这里。
        """
        self.clear_messages()
        self._messages = messages
        total = len(messages)
        self._mounted_start = max(0, total - HISTORY_PAGE)
        self._mounted_end = total
        widgets: list[Widget] = []
        if self._mounted_start:
            self._history_hint = Static(
                self._history_hint_text(), id="history-hint", markup=False
            )
            widgets.append(self._history_hint)
        widgets.extend(self._build_views(self._mounted_start, self._mounted_end))
        if widgets:
            self.mount_all(widgets)
        self._set_follow(True)
        self.scroll_end(animate=False)
        self.call_after_refresh(self._fill_viewport)

    def clear_messages(self) -> None:
        """清空列表内的全部消息。"""
        for view in list(self._message_views.values()):
            view.remove()
        if self._history_hint is not None:
            self._history_hint.remove()
            self._history_hint = None
        self._message_views.clear()
        self._part_views.clear()
        self._tool_cards.clear()
        self._reasoning_views.clear()
        self._pending_parts.clear()
        self._messages = []
        self._mounted_start = 0
        self._mounted_end = 0
        self._loading = False

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """滚动之后维护跟随状态，并滑动消息窗口（接近边界补挂、离远裁剪）。

        跟随状态按滚动量判，不只认键盘动作：鼠标滚轮与拖动滚动条都会改滚动量，
        它们不走 `action_scroll_up` 这几个动作。原先只认动作的写法下，用滚轮往上翻
        之后 `_follow` 还开着，下一条消息一到就把画面拽回底部，懒加载也不会被滚轮触发。

        例外是「减一行，但落点仍在底部」。滚动量有两个来源：用户输入与窗口维护，
        而维护自己也要改滚动量——裁掉上端要把位置减回来，布局还会把超出的位置夹回
        一行。按方向一律判成「用户往上翻」的话，滚到最底之后裁一次上端、位置被夹回
        一行，跟随就永远关着了：用户没动过键盘，新消息却不再跟到底。落点仍在底部时
        不动跟随，要关由用户动作去关（`action_scroll_up` 等自己会写 `_follow`）。

        父类那条负责滚动条位置与重绘，少了它滚动条不跟着动，所以先调它。
        """
        super().watch_scroll_y(old_value, new_value)
        if self._loading:
            return
        if new_value < old_value:
            # 底部留一行容差：夹回之后还在底部，就不算用户往上翻。
            if new_value < self.max_scroll_y - 1:
                self._set_follow(False)
        elif new_value > old_value:
            # 竖向虚拟高度要等这次刷新之后才算得出来，紧接着读 max_scroll_y 读不准
            self.call_after_refresh(self._resume_follow_at_bottom)
        # 滚动方向决定窗口往哪边补、往哪边裁：往上补上端裁下端，往下补下端裁上端。
        # 补与裁永远同向，才不会出现「补上端又裁上端」的来回震荡。
        if new_value < old_value:
            self._maintain_window_up()
        elif new_value > old_value:
            self._maintain_window_down()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """滚轮往上：停止跟随。

        上面那条按位置判的分支在底部留了容差，从底部滚一格（24 → 23）落点仍在底部，
        不会关跟随。滚轮是明确的用户动作，这里补上，不然贴着底滚一格就再也停不下来。
        """
        self._set_follow(False)
        super()._on_mouse_scroll_up(event)

    def _on_scroll_to(self, message: ScrollTo) -> None:
        """拖动滚动条：按目标位置决定跟不跟。

        滚动条只发一条 `ScrollTo`，不经过上面那些动作，同样得显式写 `_follow`。
        目标正好到底才算跟：拖到差一行松手也当作用户要停在那一行。
        """
        if message.y is not None:
            self._set_follow(message.y >= self.max_scroll_y)
        super()._on_scroll_to(message)

    def _history_hint_text(self) -> str:
        """顶端那条提示的文字，写明还剩多少条没建出来。"""
        return f"↑ 上方还有 {self._mounted_start} 条更早的消息"

    def _build_views(self, start: int, end: int) -> list[MessageView]:
        """为 `[start, end)` 这一段消息建出视图并登记，挂载由调用方决定。"""
        views: list[MessageView] = []
        for message in self._messages[start:end]:
            view = MessageView(message, self)
            self._message_views[message.id] = view
            views.append(view)
        return views

    def _unmount_range(self, start: int, end: int) -> None:
        """把 `[start, end)` 这一段消息的视图摘掉，并从索引表里清掉。

        消息本身留在 `_messages` 里不丢，摘掉的只是界面上那棵组件树。摘掉之后
        `_arrange_root` 遍历的组件就少了，滚动一帧的成本跟着降下来。
        """
        for message in self._messages[start:end]:
            view = self._message_views.pop(message.id, None)
            if view is None:
                continue
            # Part 视图与它自己注册进的三张表都要跟着清掉，否则下一次重建时
            # `add_part` 会以为这个 Part 已经挂上，直接 update 一个不存在的组件。
            for part in message.parts:
                widget = self._part_views.pop(part.id, None)
                if isinstance(widget, ToolCard):
                    if widget in self._tool_cards:
                        self._tool_cards.remove(widget)
                if isinstance(widget, ReasoningPartView):
                    if widget in self._reasoning_views:
                        self._reasoning_views.remove(widget)
            view.remove()

    def _maintain_window_up(self) -> None:
        """向上滚动：接近窗口顶就补上端，挂载超上限就裁下端。

        向上翻时下端远离视口，所以裁剪裁下端；补上端把内容往下推，需要位置补偿。
        """
        if self._loading:
            return
        if self.scroll_y <= LOAD_MORE_MARGIN and self._mounted_start > 0:
            self._grow_up()
            return
        if self._mounted_end - self._mounted_start > MAX_MOUNTED:
            self._trim_down()

    def _maintain_window_down(self) -> None:
        """向下滚动：接近窗口底就补下端，挂载超上限就裁上端。

        向下翻时上端远离视口，所以裁剪裁上端；补下端接在末尾不推移视口，无需位置补偿。
        """
        if self._loading:
            return
        total = len(self._messages)
        if self.scroll_y >= self.max_scroll_y - LOAD_MORE_MARGIN and self._mounted_end < total:
            self._grow_down()
            return
        if self._mounted_end - self._mounted_start > MAX_MOUNTED:
            self._trim_up()

    def _grow_up(self) -> None:
        """在窗口最前面补一批更早的消息，并把滚动量加上新推进来的行数。

        补进来的内容把已经看到的那部分往下推，画面才会跳。行数用虚拟高度的差值算：
        它不依赖某个子组件当时算到哪一步，只要这次布局把新内容算进去了，差值就是真正
        推进来的行数。
        """
        start = max(0, self._mounted_start - HISTORY_PAGE)
        if start == self._mounted_start:
            return
        self._loading = True
        scroll_before = self.scroll_y
        height_before = self.virtual_size.height
        views = self._build_views(start, self._mounted_start)
        self._mounted_start = start
        if views:
            # 插在提示条之后：提示条始终是最上面那一条，新补的内容排在它与已有消息之间
            if self._history_hint is not None:
                self.mount_all(views, after=self._history_hint)
            else:
                self.mount_all(views, before=0)
        self.call_after_refresh(self._restore_after_grow_up, scroll_before, height_before)

    def _restore_after_grow_up(
        self,
        scroll_before: float,
        height_before: int,
        last_height: int = -1,
        attempts: int = 0,
    ) -> None:
        """向上补挂完成后把滚动位置抬回原处，再判断要不要接着裁剪。

        刚挂上去的这批要等它自己的 compose 跑完、再等布局把它们算进虚拟高度，
        中间会经过好几帧。这一帧的高度与上一帧相同，才算布局停稳，那时读到的差值
        才是这批真正推进来的行数；早一帧去滚会少算一截，画面停在上一批中间。
        """
        height_after = self.virtual_size.height
        if (height_after != last_height or height_after == height_before) and attempts < 8:
            self.call_after_refresh(
                self._restore_after_grow_up, scroll_before, height_before, height_after, attempts + 1
            )
            return
        self.scroll_to(y=scroll_before + height_after - height_before, animate=False, immediate=True)
        self._update_history_hint()
        self._loading = False
        self._fill_viewport()
        # 补上端是向上翻的动作，超上限时裁的是下端（用户在上方，下端远离视口）。
        self._maintain_window_up()

    def _grow_down(self) -> None:
        """在窗口最后面补一批更晚的消息。

        补挂之后 `max_scroll_y` 会变大。如果补挂前视口就停在底部（用户在跟随、或刚滚到底），
        那补完还要继续贴住新的底部，否则窗口下沿就到不了会话末尾。
        """
        end = min(len(self._messages), self._mounted_end + HISTORY_PAGE)
        if end == self._mounted_end:
            return
        self._loading = True
        self._was_at_bottom = self._at_bottom()
        views = self._build_views(self._mounted_end, end)
        self._mounted_end = end
        if views:
            self.mount_all(views)
        self.call_after_refresh(self._restore_after_grow_down)

    def _restore_after_grow_down(self) -> None:
        """向下补挂完成，收尾并接着维护窗口。

        补挂前停在底部的话，补完滚到新的底部（此时 `max_scroll_y` 已经把这批算进去了），
        让窗口下沿一路推进到会话末尾。
        """
        self._loading = False
        if self._was_at_bottom:
            self.scroll_to(y=self.max_scroll_y, animate=False, immediate=True)
        self._fill_viewport()
        self._maintain_window_down()

    def _trim_up(self) -> None:
        """裁掉窗口最前面的一批消息，滚动量减去被裁掉的这部分高度。

        裁掉的在上方，下方内容整体上移，视口跟着上移同样多行，画面才不跳。
        """
        end = min(self._mounted_end, self._mounted_start + HISTORY_PAGE)
        if end == self._mounted_start:
            return
        self._loading = True
        scroll_before = self.scroll_y
        height_before = self.virtual_size.height
        self._unmount_range(self._mounted_start, end)
        self._mounted_start = end
        self.call_after_refresh(self._restore_after_trim_up, scroll_before, height_before)

    def _restore_after_trim_up(
        self,
        scroll_before: float,
        height_before: int,
        last_height: int = -1,
        attempts: int = 0,
    ) -> None:
        """向上裁剪完成后把滚动量减去被裁掉的行数，再判断要不要接着补。

        裁掉的内容摘掉之后虚拟高度变小，同样要等布局停稳再读差值。
        """
        height_after = self.virtual_size.height
        if (height_after != last_height or height_after == height_before) and attempts < 8:
            self.call_after_refresh(
                self._restore_after_trim_up, scroll_before, height_before, height_after, attempts + 1
            )
            return
        self.scroll_to(y=scroll_before + height_after - height_before, animate=False, immediate=True)
        self._update_history_hint()
        self._loading = False
        self._fill_viewport()
        # 裁上端让下方内容上移、视口跟着上移，接下来朝下维护：接近底就补下端。
        self._maintain_window_down()

    def _trim_down(self) -> None:
        """裁掉窗口最后面的一批消息。视口在上方，末尾被裁不影响画面位置。"""
        start = max(self._mounted_start, self._mounted_end - HISTORY_PAGE)
        if start == self._mounted_end:
            return
        self._loading = True
        self._unmount_range(start, self._mounted_end)
        self._mounted_end = start
        self.call_after_refresh(self._restore_after_trim_down)

    def _restore_after_trim_down(self) -> None:
        """向下裁剪完成，视口在上方没被推移，直接收尾并接着朝上维护窗口。"""
        self._loading = False
        self._fill_viewport()
        self._maintain_window_up()

    def _fill_viewport(self) -> None:
        """一屏都没填满时接着往下补，直到内容超过一屏或没有更多。

        这种情形下用户没有可滚的余地，`watch_scroll_y` 不会再被叫醒，不在这里补就停在
        一个既有更早消息、又滚不动的画面上。
        """
        if self._mounted_start and self.max_scroll_y <= 0:
            self._grow_up()

    def _update_history_hint(self) -> None:
        """提示条跟着窗口上沿走：上方还有没建出来的就显示，顶到第一条就摘掉。

        提示条始终是窗口最上面那一条，`_grow_up` 把新内容插在它之后。裁剪上端之后
        上方重新出现没建出来的消息，这里要把提示条再挂回来。
        """
        hint = self._history_hint
        if self._mounted_start == 0:
            if hint is not None:
                hint.remove()
                self._history_hint = None
            return
        if hint is None:
            hint = Static(self._history_hint_text(), id="history-hint", markup=False)
            self._history_hint = hint
            self.mount(hint, before=0)
        else:
            hint.update(self._history_hint_text())

    def scroll_to_bottom(self) -> None:
        """滚动到底部并恢复跟随。"""
        self._set_follow(True)
        self.scroll_end(animate=False)

    def set_details(self, visible: bool) -> None:
        """切换工具输出详情的可见性。"""
        self._show_details = visible
        for card in self._tool_cards:
            card.set_output_visible(visible)

    def set_thinking(self, visible: bool) -> None:
        """切换思考块的可见性。"""
        self._show_thinking = visible
        for view in self._reasoning_views:
            view.set_class(not visible, "-hidden")

    @property
    def following(self) -> bool:
        """是否处于自动跟随底部的状态。"""
        return self._follow

    def _build_part(self, part: Part) -> Widget:
        """按类型创建对应的渲染组件。"""
        if part.type is PartType.TEXT:
            return TextPartView(part)
        if part.type is PartType.REASONING:
            return ReasoningPartView(part)
        if part.type is PartType.TOOL:
            card = ToolCard(part)
            card.sync(part)
            return card
        if part.type is PartType.PATCH:
            return PatchPartView(part)
        return SimplePartView(part)

    def _build_and_register(self, part: Part) -> Widget:
        """创建 Part 组件，登记到索引，并按当前显示开关设好可见性。

        组件的创建有两条路：流式追加走 `add_part`，会话重建时由 `MessageView.compose` 直接调用。
        两张表在这里填，两条路才都填得上；只填一条路的后果是另一张表空着，
        `/thinking` 或 `/details` 只改了数据与提示，画面不会有任何变化。
        """
        widget = self._build_part(part)
        self._part_views[part.id] = widget
        if isinstance(widget, ToolCard):
            widget.set_output_visible(self._show_details)
            self._tool_cards.append(widget)
        if isinstance(widget, ReasoningPartView):
            widget.set_class(not self._show_thinking, "-hidden")
            self._reasoning_views.append(widget)
        return widget

    def _flush_pending(self) -> None:
        """挂载阶段尚未就绪的 Part 在刷新后补挂。

        先把自己那份取出来再遍历：`add_part` 在容器还没挂上时会把这个 Part 塞回
        `_pending_parts`，而遍历的是同一个列表对象，边遍历边追加就永远走不到头
        （实测内存涨到几个 G、界面停止响应）。取成局部变量之后，补挂失败只会留到下一次刷新。
        """
        pending = self._pending_parts
        self._pending_parts = []
        for message_id, part in pending:
            if part.id in self._part_views:
                continue
            # 补挂失败时由 add_part 自己把它塞回新的 _pending_parts，这里不再记一次
            self.add_part(message_id, part)

    def _auto_scroll(self) -> None:
        """跟随中滚到底部；没跟随就把「有新内容」那条提示亮起来。

        新内容进来时用户可能正翻在上面看历史。这里不能滚（会把人拽走），
        但也不能一声不响：亮一条提示，让他知道下面长了东西、点一下就能回去。
        """
        if self._follow:
            self.scroll_end(animate=False)
        else:
            self._show_new_hint()

    def _set_follow(self, follow: bool) -> None:
        """统一写跟随状态：打开跟随就把「有新内容」提示收起来。

        提示只在离开底部时才有意义；所有恢复跟随的路径（end 键、滚轮、拖动、
        点提示条自己）都走这里，不用各自记着去收。
        """
        self._follow = follow
        if follow:
            self._hide_new_hint()

    def _show_new_hint(self) -> None:
        """亮起底部提示。已经亮着时这次调用什么也不做。"""
        if self._new_hint is not None and not self._new_hint.has_class("-visible"):
            self._new_hint.add_class("-visible")

    def _hide_new_hint(self) -> None:
        """收起底部提示。"""
        if self._new_hint is not None and self._new_hint.has_class("-visible"):
            self._new_hint.remove_class("-visible")

    def _at_bottom(self) -> bool:
        """判断当前是否已经滚到底部。"""
        return self.scroll_y >= self.max_scroll_y - 1

    def action_scroll_up(self) -> None:
        """向上滚动时停止自动跟随。"""
        self._set_follow(False)
        self.scroll_relative(y=-1, animate=False)

    def action_scroll_down(self) -> None:
        """向下滚动，到底部后恢复跟随。"""
        self.scroll_relative(y=1, animate=False)
        self.call_after_refresh(self._resume_follow_at_bottom)

    def action_page_up(self) -> None:
        """向上翻页并停止跟随。"""
        self._set_follow(False)
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        """向下翻页，到底部后恢复跟随。"""
        self.scroll_page_down(animate=False)
        self.call_after_refresh(self._resume_follow_at_bottom)

    def _resume_follow_at_bottom(self) -> None:
        """滚动完成之后，已经到底就恢复自动跟随。

        向下滚动与向下翻页的目标位置都排在下一次刷新之后才写到 scroll_y 上，
        动作里紧接着读到的还是滚动之前的位置，所以一路滚到最底也恢复不了跟随。
        跟着刷新之后再判一次，读到的才是新的位置。
        """
        if self._at_bottom():
            self._set_follow(True)

    def action_scroll_home(self) -> None:
        """回到顶部并停止跟随。"""
        self._set_follow(False)
        self.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        """跳到底部并恢复跟随。"""
        self.scroll_to_bottom()
