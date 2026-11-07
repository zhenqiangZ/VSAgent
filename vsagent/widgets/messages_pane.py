from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Callable, cast

from textual import events
from textual.app import ComposeResult
from textual.containers import Container, ScrollableContainer, Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Markdown, Static

from .. import palette
from ..core.agent.event_log import artifact_refs
from ..core.agent.topics import (
    TopicView,
    position_key,
    step_seconds,
    step_status,
    topic_views,
)
from ..core.models import Message, Part, PartType, Session, Step, ToolStatus
from .topic_card import ArtifactRow, StepRow, TopicHeader, format_seconds

if TYPE_CHECKING:
    from ..app import VSAgentApp

# 每一档状态只有一个字形，没有对应的汉字标签：卡片头上一行里既有图标又写「执行中」，
# 是把同一件事说两遍。空心的圆表示还没开始，实心表示在做，勾与叉是两种结局。
STATUS_ICON = {
    ToolStatus.PENDING: "○",
    ToolStatus.AWAITING: "◐",
    ToolStatus.RUNNING: "●",
    ToolStatus.COMPLETED: "✓",
    ToolStatus.FAILED: "✕",
}

# 会话（任务）状态的文案。主界面的任务行、右栏的子 agent 用的是同一套词，
# 与网页端一致；集中在这里，两处各写一份必然有一处会改漏。
SESSION_STATUS_TEXT = {
    "idle": "空闲",
    "running": "运行中",
    "done": "完成",
    "failed": "失败",
    "cancelled": "已中断",
}


def _part_index(message: Message, part: Part) -> int:
    """这个 Part 在消息里的位置。

    流式追加时它就是最后一条，补挂与重建时可能不是；按位置找比按长度推稳。
    """
    for index, item in enumerate(message.parts):
        if item is part:
            return index
    return max(0, len(message.parts) - 1)


# 工具输出最多显示这么多行，超出的部分折成一行提示，免得长输出把消息区整个刷过去。
MAX_OUTPUT_LINES = 6

# 超过上界时开头与末尾各留几行。命令的结论（报错、退出码）写在输出的末尾，
# 只留开头的话跑失败的 bash 只看得到命令本身，看不到为什么失败。
OUTPUT_HEAD_LINES = 3
OUTPUT_TAIL_LINES = 3

# 单个参数值最多显示这么多个字符。参数里偶尔塞着整段文件内容，
# 一个值铺一屏的话，卡片就不是卡片了。
PARAM_LIMIT = 160

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


class SummaryPartView(Markdown):
    """收尾总结，按 Markdown 渲染。

    第一版把它当成普通系统提示行（`SimplePartView`，`markup=False`）原样输出，
    模型写的 `## 一、` 与 `**` 全都露在屏幕上。总结是这一轮停下来之后用户
    最先读的一段，值得按正文渲染。

    它只在收尾时写一次，没有流式更新，所以不需要 `CoalescedText` 的合并窗口。
    """

    DEFAULT_CSS = palette.css("""
    SummaryPartView {
        height: auto;
        padding: 0 2;
        margin-top: 1;
        border-left: thick $vsa-line-strong;
    }
    """)

    def __init__(self, part: Part) -> None:
        super().__init__(part.text, classes="part part-summary")
        self.part = part
        self.part_id = part.id

    def sync(self, part: Part) -> None:
        """收尾总结被重写时直接换正文。"""
        self.part = part
        self.update(part.text)


class FoldBlock(CoalescedText, Container):
    """可折叠的块，用于思考过程。

    正文也走 `CoalescedText` 的合并窗口：思考块是整段 `Static` 重排，流式时每来
    一小段就重排一次，比正文重解析 Markdown 还贵，而它默认还是收起的——重排半天
    用户一个字都看不见。
    """

    DEFAULT_CSS = palette.css("""
    FoldBlock {
        height: auto;
        border-left: thick $vsa-line-soft;
        padding: 0 1;
    }
    FoldBlock .fold-header { color: $vsa-text-3; }
    FoldBlock .fold-body { color: $vsa-text-2; }
    FoldBlock .fold-body.-hidden { display: none; }
    """)

    def __init__(self, title: str, classes: str = "") -> None:
        super().__init__(classes=classes)
        self._title = title
        self._expanded = False
        self._body = Static("", classes="fold-body -hidden", markup=False)
        self._start_window("")

    def compose(self) -> ComposeResult:
        # 标题行直接交给 `compose` 建：在 `__init__` 里先建再 yield 的话，
        # 组件会被挂载两次，测试里拿到的那一份是第一个、屏幕上的是第二个。
        yield Static(self._header_text(), classes="fold-header", markup=False)
        yield self._body

    def _header_text(self) -> str:
        symbol = "▾" if self._expanded else "▸"
        hint = "点击折叠" if self._expanded else "点击展开"
        return f"{symbol} {self._title}（{hint}）"

    def on_click(self, event: events.Click) -> None:
        """点击标题时切换展开状态。

        只认标题行：正文展开之后点它多半是想读或选中，整段收起来是把人赶走。
        """
        if not event.widget.has_class("fold-header"):
            return
        self._expanded = not self._expanded
        self.query_one(".fold-header", Static).update(self._header_text())
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
    """工具调用卡片，输入输出默认收起，点标题行才展开。

    卡片头只写状态字形、工具名、耗时（有的话）与第一个参数。原先还带一个按工具
    类别挑的图标，二十多个工具里有五个共用同一个字形，「这是检索还是计算」这个问题
    它答不了，答得了的是工具名本身。

    卡片上有两级折叠，各自管一段：

    - 标题行（`▾/▸`）收起整段正文，包括参数、输出与产物。默认是收起的，
      点标题行才切换；点正文什么都不做——用户点一下输出多半只是想选中它读一眼，
      整段消失是最烦人的一种「帮忙」。
    - 省略行（`▾ 中间省略 N 行 · 点开看全文`）只展开被折掉的中间部分，点一下换回「收起」。
      没有这一条，长输出被折掉的那几十行在界面上就再也看不到了，只能去翻 trace。

    展开之后的第一段是全部参数。标题行只装得下第一个键，模型究竟给了什么
    （尤其是 `dock` 的盒子、`score_affinity` 的 model）要在这里才看得全。
    失败时卡片自己展开：标题行变红只说明「出事了」，错误原文才是可用的信息。
    """

    DEFAULT_CSS = palette.css("""
    ToolCard {
        height: auto;
        padding: 0 1;
        /* 比步行再深一级：主题头 → 步行 → 工具卡，缩进本身就是这三层的关系。 */
        margin: 0 0 1 4;
    }
    /* 状态只用字形与标题那一档颜色表达，不另加左侧竖线：
       一条彩色的竖线在整屏里比卡片本身的正文还显眼。 */
    ToolCard .tool-header { color: $vsa-text-2; }
    ToolCard.-running .tool-header { color: $vsa-accent; }
    ToolCard.-awaiting .tool-header { color: $vsa-warn; }
    ToolCard.-failed .tool-header { color: $vsa-error; }
    ToolCard .tool-params { color: $vsa-text-2; }
    ToolCard .tool-output { color: $vsa-text-3; }
    ToolCard .tool-tail { color: $vsa-text-3; }
    ToolCard .tool-files { color: $vsa-accent; }
    ToolCard .tool-more { color: $vsa-text-2; height: 1; }
    ToolCard .tool-params.-hidden,
    ToolCard .tool-output.-hidden,
    ToolCard .tool-tail.-hidden,
    ToolCard .tool-files.-hidden,
    ToolCard .tool-more.-hidden { display: none; }
    """)

    def __init__(self, part: Part) -> None:
        super().__init__(classes="part part-tool")
        self.part_id = part.id
        self._part = part
        # 默认收起：卡上只留一行标题（状态、工具名、耗时、第一个参数）。
        # 一轮十几步、每步几个工具，全部摊开的话工具正文会把对话淹掉。
        self._show_output = False
        self._expanded = False
        # 执行耗时：只认亲眼看到的那一段。卡片是完成态建出来的（打开旧会话）时
        # `part.timestamp` 是几天前，减出来没有意义，宁可不显示。
        self._started_at: float | None = None
        self._elapsed: float | None = None
        self._timer: Timer | None = None
        self._header = Static(self._header_text(), classes="tool-header", markup=False)
        self._params = Static("", classes="tool-params", markup=False)
        self._output = Static("", classes="tool-output", markup=False)
        self._more = Static("", classes="tool-more", markup=False)
        self._tail = Static("", classes="tool-tail", markup=False)
        self._files = Static("", classes="tool-files", markup=False)
        self._refresh()

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._params
        yield self._output
        yield self._more
        yield self._tail
        yield self._files

    def _header_text(self) -> str:
        status = self._part.tool_status or ToolStatus.PENDING
        mark = "▾" if self._show_output else "▸"
        return (
            f"{mark} {STATUS_ICON[status]} {self._part.tool_name or 'tool'}"
            f"{self._elapsed_text()}"
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

    def _params_text(self) -> str:
        """展开之后的参数清单，一行一个键。

        标题行只装得下第一个键；盒子、模型名、配体集这些参数决定了这次调用
        究竟做了什么，缺了它们只能去翻 trace。
        """
        given = self._part.tool_input if isinstance(self._part.tool_input, dict) else {}
        if not given:
            return ""
        lines = ["参数"]
        for key, value in given.items():
            if isinstance(value, str):
                text = value
            else:
                text = json.dumps(value, ensure_ascii=False)
            text = " ".join(text.split())
            if len(text) > PARAM_LIMIT:
                text = text[: PARAM_LIMIT - 3] + "..."
            lines.append(f"  {key}: {text}")
        return "\n".join(lines)

    def _files_text(self) -> str:
        """这次调用登记下来的产物。读数从 `tool_meta` 取，与步的流水同一份口径。"""
        refs = artifact_refs(self._part.tool_meta)
        return f"产物 {'、'.join(refs)}" if refs else ""

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
        """按当前状态把几块内容与可见性一起写好。"""
        params = self._params_text()
        output = self._output_text()
        tail = self._tail_text()
        more = self._more_text()
        files = self._files_text()
        self._header.update(self._header_text())
        self._params.update(params)
        self._output.update(output)
        self._tail.update(tail)
        self._more.update(more)
        self._files.update(files)
        # 还没输出的卡片（等待、执行中）不留一行空白：空 Static 的 auto 高度是 1 行，
        # 每张卡多一行，几张卡叠起来就是一屏的空白。
        self._params.set_class(not self._show_output or not params, "-hidden")
        self._output.set_class(not self._show_output or not output, "-hidden")
        self._tail.set_class(not self._show_output or not tail, "-hidden")
        self._more.set_class(not self._show_output or not more, "-hidden")
        self._files.set_class(not self._show_output or not files, "-hidden")

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
        """刷新工具状态与输出。失败那一刻把正文展开：错误原文才是能用的信息。"""
        was_failed = self._part.tool_status is ToolStatus.FAILED
        self._part = part
        status = part.tool_status or ToolStatus.PENDING
        if status is ToolStatus.FAILED and not was_failed:
            self._show_output = True
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
        """控制工具输出是否显示（对应主题里的工具开关、`/details` 与点标题行收起）。"""
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
    # 这里不补的话，patch 摘要、引用文件、步骤分隔线这类行会比同一条消息里别的行左移两列。
    DEFAULT_CSS = """
    SimplePartView {
        height: auto;
        padding: 0 2;
    }
    /* 步骤分隔不再是横线：一轮里的每一步本来就是一串工具卡，
       卡片之间的留白已经把它们分开，再加一条「── step 1 ──」是把内部计数写给用户看。 */
    SimplePartView.part-step_start,
    SimplePartView.part-step_finish {
        display: none;
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
        """系统事件与普通 Part 的正文。

        系统事件（重试、压缩、收尾总结、错误、子任务）统一成「· 一句话」：
        圆点是这类行的唯一标记。原先各用 `⟳` `░` `■` `◇` 四种字形再加四种颜色，
        读的人要先学会每个字形是什么意思。
        """
        if part.type is PartType.FILE:
            preview = f"  {len(part.text.splitlines())} 行" if part.text else ""
            return f"{part.file_path or part.text}{preview}"
        if part.type is PartType.AGENT:
            return f"已派发子 agent「{part.agent_name}」 · 点击进入"
        if part.type is PartType.STEP_START:
            return f"step {part.text or 'start'}"
        if part.type is PartType.STEP_FINISH:
            return f"step {part.text or 'finish'}"
        if part.type is PartType.PATCH:
            stat = (
                f"  (+{part.patch_added} -{part.patch_removed})"
                if part.patch_added or part.patch_removed
                else ""
            )
            return f"· patch  {part.text or part.file_path}{stat}"
        if part.type is PartType.SUBTASK:
            return f"· 子任务 [{part.agent_name or 'subagent'}] {part.text}"
        if part.type is PartType.RETRY:
            return f"· 重试 {part.text}"
        if part.type is PartType.COMPACTION:
            return f"· 上下文已压缩 {part.text}"
        if part.type is PartType.ERROR:
            return f"· {part.text}"
        if part.type is PartType.SUMMARY:
            return part.text
        return part.text


class PatchPartView(SimplePartView):
    """补丁 Part，点击后打开全屏 Diff。"""

    def on_click(self) -> None:
        """点击后打开 Diff 覆盖层。"""
        app = cast(VSAgentApp, self.app)
        app.show_diff(self.part_text)

    @property
    def part_text(self) -> str:
        """补丁内容文本。"""
        return str(self.content)


class MessageView(Container):
    """一条消息的容器，内部按 Part 顺序堆叠子组件。

    用户消息与回复用同一条左边界，靠一小段浅底和留白分开：用户那一侧进消息区后
    换一层底色，回复那侧沿用屏幕底。不给任何一方画竖线、写角色标签——
    谁在说话，位置与底色已经说了。
    """

    DEFAULT_CSS = palette.css("""
    MessageView { height: auto; margin-bottom: 1; }
    MessageView .message-parts { height: auto; }
    /* 用户消息：与回复同左边界，整块换一层浅底，靠底色而不是竖线区分是谁在说话。
       上下各留一行，底色于是把这段文字包成一个方块，而不是贴着上一段回复的末行。
       回复那一侧的内边距在容器上，用户这一侧在容器上补同样多，正文（Markdown 自带
       两列）于是落在同一列；右侧不留，底色右沿与回复正文右沿对齐。 */
    MessageView.message-user {
        background: $vsa-layer;
        padding: 1 0 1 2;
    }
    MessageView.message-assistant { padding-left: 2; }
    /* 派发标记不是用户说的话。它仍然是一条 user 消息（翻译层把它翻成空，
       不进模型上下文），但视觉上必须去掉底色，
       否则看起来就像用户自己发了一句「已派发子 agent…」。 */
    MessageView.message-notice {
        background: transparent;
        border: none;
        /* 与正文左边界取齐：正文是容器 2 列 + Markdown 自带 2 列，
           这里补上容器这 2 列，SimplePartView 自己还有 2 列。 */
        padding: 0 0 0 2;
    }
    """)

    def __init__(self, message: Message, pane: MessagesPane, index: int = -1) -> None:
        # 只有派发标记的消息不是「用户说了什么」：给它单独的样式类，
        # 让底色不套在它身上。
        notice = bool(message.parts) and all(
            part.type is PartType.AGENT for part in message.parts
        )
        classes = f"message-view message-{message.role}"
        if notice:
            classes += " message-notice"
        super().__init__(classes=classes)
        self.message = message
        self.pane = pane
        # 主题头与步行按 (消息, Part) 定位，重建时要能报出自己的位置。
        self.index = index
        self.parts_box: Vertical | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="message-parts") as box:
            self.parts_box = box
            for part_index, part in enumerate(self.message.parts):
                if part.id in self.pane._part_views:
                    continue
                # 位置 0 上的行由消息区挂在消息外面：用户消息的头排在气泡下面，
                # 其余消息的头排在它前面。消息中间开始的那些插在这里。
                if part_index:
                    for row in self.pane._rows_before(self.index, part_index):
                        yield row
                yield self.pane._build_and_register(part, self.index, part_index)


class NewMessagesHint(Static):
    """消息区底部的「有新内容」提示条。

    用户往上翻历史时不跟随，新内容还在下面长。没有这条提示的话，界面上一点动静
    都没有，长回合里用户不知道下面有没有东西；有了它，点一下就能回到底部。

    停靠在消息区底部（`dock: bottom`），所以它跟着视口走，不随内容滚动。
    """

    DEFAULT_CSS = palette.css("""
    NewMessagesHint {
        dock: bottom;
        height: 1;
        display: none;
        background: $vsa-layer;
        color: $vsa-text-2;
        content-align: center middle;
    }
    NewMessagesHint.-visible { display: block; }
    """)

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

    DEFAULT_CSS = """
    /* 主题与步两层折叠各用一个类：主题收起时它盖住的整段都不占位置，
       步收起时只藏它的工具卡。两个类都只改 display，不碰组件自己的输出开关。 */
    MessagesPane .-topic-hidden,
    MessagesPane .-step-hidden {
        display: none;
    }
    """

    def __init__(self, on_removed: Callable[[], None] | None = None) -> None:
        super().__init__(id="messages-pane")
        self._on_removed = on_removed
        self._message_views: dict[str, MessageView] = {}
        self._part_views: dict[str, Widget] = {}
        self._follow = True
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
        # 每次改高度领一个号。上一次的补偿还排在刷新队列里时又点了一下，
        # 两个补偿会各按自己那份差值滚一次，画面来回跳；旧的那次看到号变了就退出。
        self._restore_token = 0
        self._was_at_bottom = False
        self._history_hint: Static | None = None
        self._new_hint: NewMessagesHint | None = None
        # 主题视图。展开状态只在这一层：行组件不认识字典，点击时回来问。
        # 键是起点位置（`topics.position_key`），封存前后不变，用户展开过的主题
        # 不会因为换了一个事件 id 就自己合上。
        self._session: Session | None = None
        self._running = False
        self._topics: list[TopicView] = []
        self._topic_by_key: dict[str, TopicView] = {}
        self._header_views: dict[str, TopicHeader] = {}
        self._artifact_rows: dict[str, ArtifactRow] = {}
        self._step_rows: dict[tuple[int, int], StepRow] = {}
        self._step_spans: list[tuple[tuple[int, int], tuple[int, int], Step]] = []
        self._open: dict[str, bool] = {}
        self._manual_open: set[str] = set()
        self._step_open: dict[tuple[int, int], bool] = {}
        # 每个主题的工具输出是不是被一次展开过。第三层的状态按主题记：
        # 主题、步、输出三层各管一段，互不覆盖。
        self._tools_open: dict[str, bool] = {}

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
        （`app.tcss` 里没有 `:focus-within` 规则），需要跟着焦点变的只有滚动条，
        所以重算名单里只放消息区自己，`update_nodes` 会自动带上它的滚动条。
        """
        self.app.stylesheet.update_nodes([self], animate=True)

    @property
    def topics(self) -> list[TopicView]:
        """当前算出来的主题清单。左栏的主题列表读这一份，不另外算一遍边界。"""
        return self._topics

    def refresh_topics(self, reset: bool = False) -> None:
        """重算主题视图，并给新出现的主题定默认展开状态。

        默认只开最后一个，以及没有步的主题（纯聊天的一轮收起来就什么都不剩了）。
        自动折叠只动 `_manual_open` 之外的：用户点开过的主题，下一条消息进来时
        不会被合上。
        """
        if self._session is None:
            self._topics = []
            self._topic_by_key = {}
            self._step_spans = []
            return
        self._topics = topic_views(self._session, running=self._running)
        self._topic_by_key = {topic.key: topic for topic in self._topics}
        self._step_spans = [
            (
                (step.message_index, step.part_start),
                (step.message_index, step.part_end),
                step,
            )
            for topic in self._topics
            for step in (*topic.steps, topic.live_step)
            if step is not None
        ]
        if reset:
            self._open.clear()
            self._manual_open.clear()
            self._step_open.clear()
            self._tools_open.clear()
        last = self._topics[-1].key if self._topics else ""
        for topic in self._topics:
            self._open.setdefault(topic.key, topic.key == last or not topic.steps)
        for start, _end, step in self._step_spans:
            self._step_open.setdefault(start, True)
        for key, header in list(self._header_views.items()):
            topic = self._topic_by_key.get(key)
            if topic is not None:
                header.sync(topic, self._is_open(key), self._tools_open.get(key, False))
        for key, row in list(self._artifact_rows.items()):
            topic = self._topic_by_key.get(key)
            if topic is not None:
                row.sync(topic)
        for key, row in list(self._step_rows.items()):
            step = self._step_by_key(key)
            if step is not None:
                row.sync(
                    step,
                    self._step_seconds(step),
                    self._step_is_open(key),
                    step_status(self._messages, step),
                )
        self._sync_step_rows()

    def refresh_live(self) -> None:
        """步被记下来时调用：主题头跟着变，新的一步补一行。"""
        self.refresh_topics()

    def set_running(self, running: bool) -> None:
        """宿主在不在跑这个会话。

        末尾那个主题靠它区分「进行中」与「未封存」：进程在一轮中间中断时消息还在、
        循环已经不在了，画成进行中等于告诉用户还在等一个不会来的结果。
        """
        if running == self._running:
            return
        self._running = running
        self.refresh_topics()

    def _is_open(self, key: str) -> bool:
        return self._open.get(key, True)

    def _step_is_open(self, key: tuple[int, int]) -> bool:
        return self._step_open.get(key, True)

    def _topic_starting_at(self, position: tuple[int, int]) -> TopicView | None:
        return self._topic_by_key.get(position_key(position))

    def _topic_for(self, position: tuple[int, int]) -> TopicView | None:
        """这个位置属于哪个主题。一条消息可以被两个主题分掉，所以按区间找。"""
        for topic in self._topics:
            if topic.contains(position):
                return topic
        return None

    def _step_starting_at(self, position: tuple[int, int]) -> Step | None:
        for start, _end, step in self._step_spans:
            if start == position:
                return step
        return None

    def _step_for(self, position: tuple[int, int]) -> Step | None:
        for start, end, step in self._step_spans:
            if start <= position < end:
                return step
        return None

    def _step_by_key(self, key: tuple[int, int]) -> Step | None:
        for start, _end, step in self._step_spans:
            if start == key:
                return step
        return None

    def _step_seconds(self, step: Step) -> float:
        """这一步的耗时。运行中的那一步不写。

        它的时段还含模型生成，而工具卡上另有秒表；两个数并排会互相矛盾。
        记成步之后按原文算出来的最终值显示。
        """
        for topic in self._topics:
            if topic.live_step is step:
                return 0.0
        return step_seconds(self._messages, step)

    def _header_for(self, topic: TopicView) -> TopicHeader:
        """主题头一列只有一个：重建与流式追加都从这里取。"""
        header = self._header_views.get(topic.key)
        if header is None:
            header = TopicHeader(topic, self)
            self._header_views[topic.key] = header
        else:
            header.sync(topic, self._is_open(topic.key), self._tools_open.get(topic.key, False))
        return header

    def _artifacts_for(self, topic: TopicView) -> ArtifactRow:
        """主题下面那一行产物。名单为空时它自己藏起来，不必在这里判断。"""
        row = self._artifact_rows.get(topic.key)
        if row is None:
            row = ArtifactRow(topic)
            self._artifact_rows[topic.key] = row
        else:
            row.sync(topic)
        return row

    def _rows_before(self, message_index: int, part_index: int) -> list[Widget]:
        """这个位置前面该放的行：主题头在前，产物行与步行在后。

        两个都可以出现在这里：主题能从一条助手消息中间开始（`close_topic` 的切点
        就在同一条消息里），步从第一个工具调用那一格开始。

        位置 0 上的行挂在消息区这一层，其余挂在消息内部。两层的内容起点差两列，
        挂出去之前补一个类，两种挂法都从同一列开始。
        """
        rows: list[Widget] = []
        topic = self._topic_starting_at((message_index, part_index))
        if topic is not None:
            rows.append(self._header_for(topic))
            rows.append(self._artifacts_for(topic))
        step = self._step_starting_at((message_index, part_index))
        if step is not None:
            key = (step.message_index, step.part_start)
            row = self._step_rows.get(key)
            if row is None:
                row = StepRow(
                    step,
                    self._step_seconds(step),
                    self,
                    step_status(self._messages, step),
                )
                self._step_rows[key] = row
            # 行是随消息一起建出来的，不走 `_build_and_register`，可见性得在这里补。
            self._apply_classes(row, key, None)
            rows.append(row)
        for row in rows:
            row.set_class(part_index == 0, "-pane-row")
        return rows

    def _sync_step_rows(self) -> None:
        """给还没有行的步补一行。

        步要等这一段跑完才记下来，追加 Part 那一刻它还不在流水里；运行中的那一步
        同样如此，它只存在于主题视图的 `live_step` 上。工具状态一变就把行补到
        它第一个工具卡前面。
        """
        mounted = False
        for topic in self._topics:
            for step in (*topic.steps, topic.live_step):
                if step is None:
                    continue
                key = (step.message_index, step.part_start)
                if key in self._step_rows or step.message_index >= len(self._messages):
                    continue
                message = self._messages[step.message_index]
                view = self._message_views.get(message.id)
                if view is None or view.parts_box is None or not view.parts_box.is_attached:
                    continue
                row = StepRow(
                    step,
                    self._step_seconds(step),
                    self,
                    step_status(self._messages, step),
                )
                self._step_rows[key] = row
                anchor = None
                for position in range(step.part_start, min(step.part_end, len(message.parts))):
                    widget = self._part_views.get(message.parts[position].id)
                    if widget is not None:
                        anchor = widget
                        break
                if anchor is None:
                    view.parts_box.mount(row)
                else:
                    view.parts_box.mount(row, before=anchor)
                if self._topics:
                    self._apply_classes(row, key, None)
                mounted = True
        # 行插在上面，多出来一行高度；停在底部时要重新贴回去。
        if mounted and self._follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def toggle_topic(self, key: str) -> None:
        """点主题头：收起或展开整段。"""
        topic = self._topic_by_key.get(key)
        if topic is None:
            return
        open_now = not self._is_open(key)
        self._open[key] = open_now
        if open_now:
            self._manual_open.add(key)
        else:
            self._manual_open.discard(key)
        self._sync_visibility(self._header_views.get(key))

    def toggle_step(self, message_index: int, part_start: int) -> None:
        """点步行：收起或展开这一步的工具卡。"""
        key = (message_index, part_start)
        step = self._step_by_key(key)
        if step is None:
            return
        self._step_open[key] = not self._step_is_open(key)
        open_now = self._step_open[key]

        def mutate() -> None:
            for position in range(step.part_start, step.part_end):
                if position >= len(self._messages[message_index].parts):
                    continue
                part = self._messages[message_index].parts[position]
                widget = self._part_views.get(part.id)
                if widget is not None:
                    widget.set_class(not open_now, "-step-hidden")
            row = self._step_rows.get(key)
            if row is not None:
                row.sync(
                    step,
                    self._step_seconds(step),
                    open_now,
                    step_status(self._messages, step),
                )

        self._change_height(mutate, self._step_rows.get(key))

    def toggle_topic_tools(self, key: str) -> bool:
        """一次展开或收起一个主题里全部工具的输出，返回换过之后的可见状态。

        只管输出这一层：步行的展开状态不动，收起的是工具正文，步本身仍是入口。
        """
        topic = self._topic_by_key.get(key)
        if topic is None:
            return False
        visible = not self._tools_open.get(key, False)
        self._tools_open[key] = visible

        def mutate() -> None:
            for card in self._topic_cards(topic):
                card.set_output_visible(visible)
            header = self._header_views.get(key)
            if header is not None:
                header.sync(topic, self._is_open(key), visible)

        self._change_height(mutate, self._header_views.get(key))
        return visible

    def _topic_cards(self, topic: TopicView) -> list[ToolCard]:
        """这个主题里已经建出来的工具卡。更早的段没挂上时返回空表。

        运行中的那一步也要算进来：点「展开全部工具」时，正在跑的那几张卡
        同样要跟着展开。
        """
        cards: list[ToolCard] = []
        for step in (*topic.steps, topic.live_step):
            if step is None or step.message_index >= len(self._messages):
                continue
            message = self._messages[step.message_index]
            for part in message.parts[step.part_start : step.part_end]:
                widget = self._part_views.get(part.id)
                if isinstance(widget, ToolCard):
                    cards.append(widget)
        return cards

    def focus_topic(self, key: str) -> bool:
        """把某个主题打开，并滚到它的标题行。左栏的主题列表点一下走这里。

        主题可能在当前挂载窗口之外（更早的那一段没有建组件），那种情况先把窗口
        挪到它开头那一条消息，再滚过去。跳到更早的位置是一次明确的用户动作，
        整段重建的代价可以接受。
        """
        topic = self._topic_by_key.get(key)
        if topic is None:
            return False
        self._manual_open.add(key)
        self._open[key] = True
        header = self._header_views.get(key)
        if header is not None and header.is_mounted:
            self._change_height(lambda: self._apply_visibility(), compensate=False)
            self.scroll_to_widget(header, animate=False, top=True)
            return True
        self._jump_window(topic.start[0], key)
        return True

    def _jump_window(self, index: int, key: str) -> None:
        """把挂载窗口挪到 `index` 这一条消息，画完之后滚到 `key` 那个主题的标题行。"""
        for view in list(self._message_views.values()):
            view.remove()
        if self._history_hint is not None:
            self._history_hint.remove()
            self._history_hint = None
        for header in list(self._header_views.values()):
            if header.is_mounted:
                header.remove()
        for row in list(self._artifact_rows.values()):
            if row.is_mounted:
                row.remove()
        for row in list(self._step_rows.values()):
            if row.is_mounted:
                row.remove()
        self._message_views.clear()
        self._part_views.clear()
        self._tool_cards.clear()
        self._reasoning_views.clear()
        self._pending_parts.clear()
        self._header_views.clear()
        self._artifact_rows.clear()
        self._step_rows.clear()

        total = len(self._messages)
        if total == 0:
            return
        self._mounted_start = max(0, min(index, total - 1))
        self._mounted_end = min(total, self._mounted_start + HISTORY_PAGE * 2)
        self.refresh_topics()
        widgets: list[Widget] = []
        if self._mounted_start:
            self._history_hint = Static(
                self._history_hint_text(), id="history-hint", markup=False
            )
            widgets.append(self._history_hint)
        widgets.extend(self._build_views(self._mounted_start, self._mounted_end))
        if widgets:
            self.mount_all(widgets)
        self._set_follow(False)
        self.call_after_refresh(self._focus_after_jump, key)

    def _focus_after_jump(self, key: str) -> None:
        """窗口重建之后把主题头滚到顶部。"""
        self._apply_visibility()
        header = self._header_views.get(key)
        if header is not None and header.is_mounted:
            self.scroll_to_widget(header, animate=False, top=True)
        self._fill_viewport()

    def fold_sealed_topics(self) -> None:
        """自动折叠：封存过的主题全部收起，用户主动打开过的留在原样。

        只在新的用户消息进来时调。`part.update` 与轮末都不调——收起一个正在读的
        主题会改掉内容高度，屏幕会跳。
        """
        changed = False
        for topic in self._topics:
            if not topic.sealed or not topic.steps:
                continue
            if topic.key in self._manual_open:
                continue
            if self._is_open(topic.key):
                self._open[topic.key] = False
                changed = True
        if changed:
            self._sync_visibility()

    def _is_task_header(self, index: int) -> bool:
        """这条消息是不是一个任务的头（用户消息）。

        用户消息不进主题的可见性：它回答的是「这一轮问了什么」，收起主题是为了
        收起过程，把提问一起收走之后整段对话就没有起点了。
        """
        if index < 0 or index >= len(self._messages):
            return False
        return self._messages[index].role == "user"

    def _message_hidden(self, index: int) -> bool:
        """整条消息是不是属于某个收起的主题。用户消息永远不算。"""
        if self._is_task_header(index):
            return False
        position = (index, 0)
        after = (index + 1, 0)
        for topic in self._topics:
            if self._is_open(topic.key):
                continue
            if topic.start <= position and after <= topic.end:
                return True
        return False

    def _apply_classes(
        self, widget: Widget, position: tuple[int, int], part: Part | None
    ) -> None:
        """按位置给一个组件设两层可见性。两层各管一段，互不覆盖。"""
        topic = None if self._is_task_header(position[0]) else self._topic_for(position)
        by_topic = topic is not None and not self._is_open(topic.key)
        widget.set_class(by_topic, "-topic-hidden")
        if part is None or part.type is not PartType.TOOL:
            return
        step = self._step_for(position)
        by_step = step is not None and not self._step_is_open(
            (step.message_index, step.part_start)
        )
        widget.set_class(by_step, "-step-hidden")

    def _sync_visibility(self, anchor: Widget | None = None) -> None:
        """把展开状态写进界面。

        一次处理全部主题：逐主题各做一遍的话，一次自动折叠会补偿好几次滚动。
        """
        self._change_height(self._apply_visibility, anchor)

    def _apply_visibility(self) -> None:
        for index in range(self._mounted_start, self._mounted_end):
            message = self._messages[index]
            for part_index, part in enumerate(message.parts):
                widget = self._part_views.get(part.id)
                if widget is not None:
                    self._apply_classes(widget, (index, part_index), part)
        for topic in self._topics:
            header = self._header_views.get(topic.key)
            if header is not None:
                header.sync(topic, self._is_open(topic.key), self._tools_open.get(topic.key, False))
        for key, row in list(self._step_rows.items()):
            step = self._step_by_key(key)
            if step is not None:
                row.sync(
                    step,
                    self._step_seconds(step),
                    self._step_is_open(key),
                    step_status(self._messages, step),
                )
        self._unmount_hidden_messages()
        self._mount_visible_messages()

    def _unmount_hidden_messages(self) -> None:
        if not self._topics:
            return
        start = self._mounted_start
        while start < self._mounted_end:
            if not self._message_hidden(start):
                start += 1
                continue
            stop = start
            while stop < self._mounted_end and self._message_hidden(stop):
                stop += 1
            # 标题行留着：收起的是过程，那个主题的入口还得在。整段滑出窗口时
            # 才由裁剪把它一起摘掉（`_trim_up` / `_trim_down`）。
            self._unmount_range(start, stop, drop_headers=False)
            start = stop

    def _mount_visible_messages(self) -> None:
        if not self._topics:
            return
        start = self._mounted_start
        while start < self._mounted_end:
            message = self._messages[start]
            if self._message_hidden(start) or message.id in self._message_views:
                start += 1
                continue
            stop = start
            while stop < self._mounted_end:
                item = self._messages[stop]
                if self._message_hidden(stop) or item.id in self._message_views:
                    break
                stop += 1
            self._mount_built(self._build_views(start, stop), stop - 1)
            start = stop

    def _mount_built(self, widgets: list[Widget], last_index: int) -> None:
        """把一批建好的组件插到 `last_index` 那条消息之后。

        锚点要取已经挂上的那一个：刚建好的视图还没进树，拿它当锚点会直接抛出。
        """
        if not widgets:
            return
        for later in range(last_index + 1, len(self._messages)):
            view = self._message_views.get(self._messages[later].id)
            if view is not None and view.is_mounted:
                self.mount_all(widgets, before=view)
                return
        self.mount_all(widgets)

    def _change_height(
        self,
        mutate: Callable[[], None],
        anchor: Widget | None = None,
        compensate: bool = True,
    ) -> None:
        """改完可见性之后把视口留在原处。

        补偿量按锚点那一块的屏幕行算：改完把它放回原来的行，用户正在看的那一段
        就不动。按总高度差补偿只在「变化的行全部在视口上方」时成立，那是向上补挂
        更早消息的情形；折叠与展开由点击触发，变化的行在锚点下面，照那个式子算
        会让整屏平移同样的行数。

        锚点缺一个时就退回高度差：自动折叠没有可点的位置，视口顶端那一块也可能
        正是这次要收走的那段。`compensate=False` 给的是「位置由调用方自己安排」的
        情形，例如跳到某个主题头，那里紧接着会显式滚一次。
        """
        self._loading = True
        scroll_before = self.scroll_y
        height_before = self.virtual_size.height
        if anchor is None or not anchor.is_mounted or not self._in_view(anchor):
            anchor = self._scroll_anchor()
        row_before = anchor.region.y if anchor is not None else None
        self._restore_token += 1
        token = self._restore_token
        mutate()
        if not compensate:
            self._loading = False
            return
        if row_before is not None:
            self.call_after_refresh(self._restore_anchor, anchor, row_before, token)
            return
        if self._follow:
            self._loading = False
            self.call_after_refresh(self.scroll_end, animate=False)
            return
        self.call_after_refresh(self._restore_after_layout, scroll_before, height_before)

    def _in_view(self, widget: Widget) -> bool:
        """这一块是不是还露在视口里。

        `/details` 不经过点击，目标主题的头可能已经滚到视口上方；拿它当参照量不到
        它下方那些行的变化，得换视口顶端那一块。
        """
        top = self.region.y
        bottom = self.region.bottom
        return widget.region.y < bottom and widget.region.bottom > top

    def _scroll_anchor(self) -> Widget | None:
        """视口最上面那一块组件。

        取底边还压在视口里的第一个：它可能是半个主题头、一条消息或一行产物。
        高度变化之后拿它当参照，就不必判断变化发生在上方还是下方。
        """
        top = self.region.y
        for child in self.children:
            if child.region.height <= 0:
                continue
            if child.region.bottom > top:
                return child
        return None

    def _restore_anchor(
        self,
        anchor: Widget,
        row_before: int,
        token: int,
        last_height: int = -1,
        attempts: int = 0,
    ) -> None:
        """布局停稳之后把锚点挪回原来的屏幕行。

        差多少滚多少：锚点被推到下面说明它上方长高了，往下滚同样的行数；锚点没动
        （变化全在它下面）就不滚。锚点在这一轮里被摘掉时不做补偿，交给上下界夹回。
        号变了说明又点了一下，这一轮的基线已经不作数，直接退出。
        """
        if token != self._restore_token:
            return
        height_after = self.virtual_size.height
        if height_after != last_height and attempts < 8:
            self.call_after_refresh(
                self._restore_anchor, anchor, row_before, token, height_after, attempts + 1
            )
            return
        shift = anchor.region.y - row_before if anchor.is_mounted else 0
        self.scroll_to(y=self.scroll_y + shift, animate=False, immediate=True)
        self._loading = False
        # 补偿之后不再贴底就关掉跟随：下一次流式输出不该把视口拽回底部。
        self._set_follow(self._at_bottom())
        self._after_layout_change()

    def _restore_after_layout(
        self, scroll_before: float, height_before: int, last_height: int = -1, attempts: int = 0
    ) -> None:
        self._restore_scroll(
            scroll_before, height_before, self._after_layout_change, last_height, attempts
        )

    def _after_layout_change(self) -> None:
        self._fill_viewport()
        self._maintain_window_down()

    def add_message(self, message: Message) -> MessageView:
        """添加一条消息到列表末尾。

        流式生成时新消息总是接在会话末尾，调用方已把它放进 `session.messages`（也就是
        `self._messages`）。这里建出视图挂到末尾，并把窗口下沿推进到包含这条新消息。
        """
        index = len(self._messages) - 1
        # 新的一条用户消息会开一个新主题：先把视图模型算出来，标题行才挂得上。
        self.refresh_topics()
        view = MessageView(message, self, index)
        self._message_views[message.id] = view
        if message.role == "user":
            # 提问在前，这一轮的主题头挂在它下面。
            self.mount(view)
            for row in self._rows_before(index, 0):
                self.mount(row)
        else:
            for row in self._rows_before(index, 0):
                self.mount(row)
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
        index = _part_index(view.message, part)
        if index:
            for row in self._rows_before(view.index, index):
                box.mount(row)
        widget = self._build_and_register(part, view.index, index)
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

    def rebuild(
        self,
        messages: list[Message],
        session: Session | None = None,
        running: bool = False,
    ) -> None:
        """按会话内容重建列表（切换会话时使用）。

        只建出最后一段，更早的那些留到用户滚到顶端时再补一批。一段几百条消息的会话
        全建出来要几千个组件，建完还要等布局算完，打开会话的那几秒就停在这里。
        """
        self.clear_messages()
        self._session = session
        self._running = running
        self._messages = messages
        self.refresh_topics(reset=True)
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
        for header in list(self._header_views.values()):
            if header.parent is self:
                header.remove()
        for row in list(self._artifact_rows.values()):
            if row.parent is self:
                row.remove()
        self._message_views.clear()
        self._part_views.clear()
        self._tool_cards.clear()
        self._reasoning_views.clear()
        self._pending_parts.clear()
        self._header_views.clear()
        self._artifact_rows.clear()
        self._step_rows.clear()
        self._topics = []
        self._topic_by_key = {}
        self._step_spans = []
        self._open.clear()
        self._manual_open.clear()
        self._step_open.clear()
        self._tools_open.clear()
        self._messages = []
        self._mounted_start = 0
        self._mounted_end = 0
        self._loading = False

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """滚动之后维护跟随状态，并滑动消息窗口（接近边界补挂、离远裁剪）。

        跟随状态按滚动量判，不只认键盘动作：鼠标滚轮与拖动滚动条都会改滚动量，
        它们不走 `action_scroll_up` 这几个动作。原先只认动作的写法下，用滚轮往上翻
        之后 `_follow` 还开着，下一条消息一到就把画面拽回底部，懒加载也不会被滚轮触发。
        拖动滚动条发来的目标位置不参与判定：那是按发消息那一刻的最大滚动量算的，
        消息排到队尾才被处理时视口可能已经变过，拿它比会把「拖到底」判成「没到底」。

        往上那一支的判读排在这次刷新之后：滚动量变小的原因不一定是用户往上翻，
        视口变高（提示条占位、输入框收放）也会把超出上限的位置夹回。夹回的那一刻
        `max_scroll_y` 还是布局之前的旧值，当场比会把「夹回两行」当成「用户往上翻
        两行」；等布局稳定再看落点，不在底部才关掉跟随。滚轮那种明确的用户动作
        由 `_on_mouse_scroll_up` 自己关。

        父类那条负责滚动条位置与重绘，少了它滚动条不跟着动，所以先调它。
        """
        super().watch_scroll_y(old_value, new_value)
        if self._loading:
            return
        if new_value < old_value:
            self.call_after_refresh(self._confirm_follow_after_scroll_up)
        elif new_value > old_value:
            # 竖向虚拟高度要等这次刷新之后才算得出来，紧接着读 max_scroll_y 读不准
            self.call_after_refresh(self._resume_follow_at_bottom)
        # 滚动方向决定窗口往哪边补、往哪边裁：往上补上端裁下端，往下补下端裁上端。
        # 补与裁永远同向，才不会出现「补上端又裁上端」的来回震荡。
        if new_value < old_value:
            self._maintain_window_up()
        elif new_value > old_value:
            self._maintain_window_down()

    def on_resize(self, event: events.Resize) -> None:
        """视口高度变了：落点还在底部就保持跟随。

        视口变高（提示条占位、输入框收放）会把最大滚动量压小，位置被一起夹回。
        这一下不触发「往下滚」的判读，最后一次按键排的判读可能已经跑过，
        跟随就停在关着——用户明明在底部，新消息却不再跟下来。
        """
        if self._loading:
            return
        self.call_after_refresh(self._confirm_follow_at_bottom)

    def _confirm_follow_at_bottom(self) -> None:
        """布局稳定之后再看一眼：还在底部就恢复跟随。"""
        if self._at_bottom():
            self._set_follow(True)

    def _confirm_follow_after_scroll_up(self) -> None:
        """往上滚动之后再看一眼：落点不在底部才关掉跟随。

        位置变小的原因不一定是用户往上翻：视口变高（提示条占位、输入框收放）会把
        超出上限的位置夹回来，那一下也得等布局算完再判。
        """
        if not self._at_bottom():
            self._set_follow(False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """滚轮往上：停止跟随。

        上面那条按位置判的分支在底部留了容差，从底部滚一格（24 → 23）落点仍在底部，
        不会关跟随。滚轮是明确的用户动作，这里补上，不然贴着底滚一格就再也停不下来。
        """
        self._set_follow(False)
        super()._on_mouse_scroll_up(event)

    def _history_hint_text(self) -> str:
        """顶端那条提示的文字，写明还剩多少条没建出来。"""
        return f"↑ 上方还有 {self._mounted_start} 条更早的消息"

    def _build_views(self, start: int, end: int) -> list[Widget]:
        """为 `[start, end)` 这一段消息建出视图并登记，挂载由调用方决定。

        返回的列表里还夹着主题头：它在消息之间，挂载顺序必须与滚动顺序一致。
        完全属于收起主题的消息不建视图，只留下那个主题的标题行——这就是折叠
        省下来的那部分高度。

        用户消息是任务的头，主题头排在它下面：提问在上、这一轮做了哪几件事在下。
        其余消息仍是主题头在前，从消息中间起的主题（`close_topic` 的切点）就在
        它真正开始的那一格上。
        """
        views: list[Widget] = []
        for index in range(start, end):
            message = self._messages[index]
            if self._message_hidden(index):
                topic = self._topic_starting_at((index, 0))
                if topic is not None:
                    views.append(self._header_for(topic))
                    views.append(self._artifacts_for(topic))
                continue
            view = MessageView(message, self, index)
            self._message_views[message.id] = view
            if message.role == "user":
                views.append(view)
                views.extend(self._rows_before(index, 0))
            else:
                views.extend(self._rows_before(index, 0))
                views.append(view)
        return [widget for widget in views if not widget.is_mounted]

    def _unmount_range(self, start: int, end: int, drop_headers: bool = True) -> None:
        """把 `[start, end)` 这一段消息的视图摘掉，并从索引表里清掉。

        消息本身留在 `_messages` 里不丢，摘掉的只是界面上那棵组件树。摘掉之后
        `_arrange_root` 遍历的组件就少了，滚动一帧的成本跟着降下来。

        `drop_headers=False` 用于折叠：那一段消息收起来，主题头是再打开的入口，
        跟着摘掉的话这个主题就从画面上消失了，再没有地方点开它。
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
        # 标题行与步行跟着起点所在的这批消息一起清掉。标题行如果挂在消息区上（主题
        # 从消息开头起），它不在这棵子树里，要单独摘。
        if drop_headers:
            for key, header in list(self._header_views.items()):
                if start <= header.topic.start[0] < end:
                    if header.parent is self:
                        header.remove()
                    self._header_views.pop(key, None)
            for key, row in list(self._artifact_rows.items()):
                if start <= row.topic.start[0] < end:
                    if row.parent is self:
                        row.remove()
                    self._artifact_rows.pop(key, None)
        for key, row in list(self._step_rows.items()):
            if start <= key[0] < end:
                self._step_rows.pop(key, None)

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
        self._restore_scroll(
            scroll_before, height_before, self._after_grow_up, last_height, attempts
        )

    def _after_grow_up(self) -> None:
        self._update_history_hint()
        self._fill_viewport()
        # 补上端是向上翻的动作，超上限时裁的是下端（用户在上方，下端远离视口）。
        self._maintain_window_up()

    def _restore_scroll(
        self,
        scroll_before: float,
        height_before: int,
        after: Callable[[], None],
        last_height: int = -1,
        attempts: int = 0,
    ) -> None:
        """等布局停稳，把滚动量补上高度差，然后交给调用方收尾。

        向上补挂、向上裁剪、折叠展开都会改内容高度，等待的理由是同一个：
        这一帧的高度与上一帧相同，布局才算停稳；早一帧读到的差值不是这批内容
        真正推进来的行数。
        """
        height_after = self.virtual_size.height
        if (height_after != last_height or height_after == height_before) and attempts < 8:
            self.call_after_refresh(
                self._restore_scroll,
                scroll_before,
                height_before,
                after,
                height_after,
                attempts + 1,
            )
            return
        self.scroll_to(y=scroll_before + height_after - height_before, animate=False, immediate=True)
        self._loading = False
        after()

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
        self._restore_scroll(
            scroll_before, height_before, self._after_trim_up, last_height, attempts
        )

    def _after_trim_up(self) -> None:
        self._update_history_hint()
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
        if part.type is PartType.SUMMARY:
            return SummaryPartView(part)
        return SimplePartView(part)

    def _build_and_register(
        self, part: Part, message_index: int = -1, part_index: int = 0
    ) -> Widget:
        """创建 Part 组件，登记到索引，并按当前显示开关设好可见性。

        组件的创建有两条路：流式追加走 `add_part`，会话重建时由 `MessageView.compose` 直接调用。
        两张表在这里填，两条路才都填得上；只填一条路的后果是另一张表空着，
        `/thinking` 只改了数据与提示，画面不会有任何变化。

        位置也在这里判一次：属于收起主题或收起步的组件建出来就是隐藏的，
        不必等折叠那一刻再去找它。
        """
        widget = self._build_part(part)
        self._part_views[part.id] = widget
        if isinstance(widget, ToolCard):
            self._tool_cards.append(widget)
        if isinstance(widget, ReasoningPartView):
            widget.set_class(not self._show_thinking, "-hidden")
            self._reasoning_views.append(widget)
        if message_index >= 0 and self._topics:
            self._apply_classes(widget, (message_index, part_index), part)
            # 主题被一次展开过之后，后面才流进来的工具卡也要跟着展开，
            # 否则用户点过「展开全部工具」，下一条输出仍然藏着。
            if isinstance(widget, ToolCard):
                topic = self._topic_for((message_index, part_index))
                if topic is not None and self._tools_open.get(topic.key):
                    widget.set_output_visible(True)
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
        """向下滚动，到底部后恢复跟随。

        先滚再排一次判读。滚动量没变的那一次也照排：贴底时最后一格往下滚不动，
        而这一帧里视口可能正好变高一行、最大滚动量跟着变小，这一下就没走到底。
        这一帧里滚动量没变，`watch_scroll_y` 不会被叫醒，排着的判读就是唯一的收尾。
        """
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

    def _resume_follow_at_bottom(self, retried: bool = False) -> None:
        """滚动完成之后，已经到底就恢复自动跟随。

        向下滚动与向下翻页的目标位置都排在下一次刷新之后才写到 scroll_y 上，
        动作里紧接着读到的还是滚动之前的位置，所以一路滚到最底也恢复不了跟随。
        跟着刷新之后再判一次，读到的才是新的位置。

        差一行的位置要再看一帧：这次刷新可能把视口抬高一行（输入框失去焦点、
        状态行收起），最大滚动量随之变小而滚动量还没夹回。这一帧判成没到底，
        下一帧夹回之后就到底了；不再多看一帧的话跟随会一直关着。

        看完这一帧还差一行，就按到底处理：视口反向变高一行时滚动量停在这一行上
        不会再被夹回，一直重排只会永远差一行，跟随永远关着（实测滚到底之后
        新消息不再跟下来）。`_at_bottom` 的容差本来就是一行，两处取同一个口径。
        """
        if self.scroll_y >= self.max_scroll_y:
            self._set_follow(True)
            return
        if self.max_scroll_y > 0 and self.scroll_y >= self.max_scroll_y - 1:
            if retried:
                self._set_follow(True)
                return
            self.call_after_refresh(lambda: self._resume_follow_at_bottom(True))

    def action_scroll_home(self) -> None:
        """回到顶部并停止跟随。"""
        self._set_follow(False)
        self.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        """跳到底部并恢复跟随。"""
        self.scroll_to_bottom()
