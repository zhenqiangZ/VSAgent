from __future__ import annotations

import time
from pathlib import Path

from rich.cells import cell_len

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message as TextualMessage
from textual.reactive import Reactive
from textual.screen import Screen
from textual.selection import SelectState
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core.models import Message, Part, PartType, Session, ToolStatus
from ..core.session_manager import RAIL_MAX, RAIL_MIN, SIDE_MAX, SIDE_MIN
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import EditorPane
from ..widgets.messages_pane import SESSION_STATUS_TEXT, MessagesPane
from ..widgets.session_list import SessionList, SessionRow
from ..widgets.splitter import Splitter
from ..widgets.status_bar import (
    ContextUsage,
    SidebarToggle,
    StatusBar,
    format_tokens,
)
from ..widgets.topic_card import ArtifactChip
from ..widgets.topic_list import TopicList, TopicRow
from .context_usage import ContextUsageScreen

# 中间那一栏至少要留这么多列。拖动分栏界线时宽度夹在这里，终端变窄时按同一档重算。
MIN_MAIN_WIDTH = 40
# 右栏最多列这么多条产物。再多就要滚动，而它是一份索引，不是正文。
ARTIFACT_ROWS = 6


def _short(text: str, width: int) -> str:
    """按显示格数截断标题。超长补省略号，避免一行被挤成两行。

    不能用 `len()`：一个汉字占两格，12 个汉字就是 24 格，
    在 30 列宽的右栏里会把状态行顶到下一行。
    """
    text = text.strip()
    if cell_len(text) <= width:
        return text
    while text and cell_len(text) + 1 > width:
        text = text[:-1]
    return text + "…"


class SideOptionList(OptionList):
    """右栏的可点列表（产物、子 agent 各一个）。

    单独一个类是为了给 esc 一个出口：列表拿到焦点之后，按 esc 要能回到
    输入框。否则键盘用户只能靠鼠标点回去，而 `/agents` 的意义正是给不摸鼠标的人用。
    """

    BINDINGS = [Binding("escape", "release", "Back")]

    def action_release(self) -> None:
        """把焦点交回输入框。"""
        screen = self.screen
        editor = getattr(screen, "editor", None)
        if editor is not None:
            editor.focus_input()


class SidePanel(Vertical):
    """会话右栏：产物在上，子 agent 与用量在下。

    产物排第一：这一栏最常被打开的时刻是「这一段做出了什么」。
    子 agent 段只在有子会话时出现；没有时不占任何高度，/sidebar 的行为与以前一致。
    列表用 `OptionList` 而不是 `Static`：它要能点。

    这一段不写会话名与模型名：会话名在底部状态栏，模型名也在那里。
    同一个名字在两处出现，读的人要自己判断哪一个才是真的。
    引用文件与工具调用两份清单不再列：消息区里的文件 Part 与步行已经各写了一遍，
    30 列的面板里再抄一份，占掉的正是产物要用的高度。
    """

    class AgentPicked(TextualMessage):
        """用户点了右栏里的一个子 agent。"""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class ArtifactPicked(TextualMessage):
        """用户点了右栏里的一个产物。"""

        def __init__(self, handle: str) -> None:
            super().__init__()
            self.handle = handle

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._written: dict[str, str] = {}
        # None 表示还没画过：空名单与「还没算过」要分开，否则第一次打开侧栏时
        # 那行「还没有产物」会被当成「名单没变」跳过。
        self._artifacts: list[str] | None = None

    def compose(self) -> ComposeResult:
        yield Static("产物", classes="side-section -first")
        yield SideOptionList(id="side-artifacts")
        yield Static("子 AGENT", id="side-agents-title", classes="side-section")
        yield SideOptionList(id="side-agents")
        yield Static("用量", id="side-context-title", classes="side-section")
        yield Static("", id="side-stats", classes="side-line", markup=False)

    def update_context(self, session: Session) -> None:
        """按当前会话刷新产物与用量。

        侧栏没打开时整个跳过：产物清单要把会话里全部事件走一遍，
        而它算出来只会写进一个不显示的组件。打开侧栏的那一处会重新调一次。
        """
        if not self.has_class("-visible"):
            return
        stats = (
            f"{len(session.messages)} 条消息"
            f" · 总 {format_tokens(session.total_tokens())} tokens"
        )
        # 累计用量只写在这里：状态栏那个百分比回答「还能聊多久」，这三项回答「钱花在哪」。
        # 输入分成命中与未命中，两者的单价差十倍，合成一个数就看不出省了多少。
        stats += f"\n缓存命中 {format_tokens(session.total_cached())}"
        stats += f"\n未命中 {format_tokens(session.total_uncached())}"
        stats += f"\n输出 {format_tokens(session.total_output())} · ${session.total_cost():.2f}"
        self._write("#side-stats", stats)
        self._refresh_artifacts(session)

    @staticmethod
    def _artifact_handles(session: Session) -> list[str]:
        """这一轮产出的 handle，按主题倒序取最近几条。

        未封存那一段也算：一轮跑完才封存，只看 `session.events` 的话，
        刚刚写出来的运行目录要等这一轮结束才出现在栏里。
        handle 只在产出它的那次会话里有效，所以这里只列名字，不给路径：
        要点开就去网页端，要交给工具就照抄整行。
        """
        names: list[str] = []

        def remember(handle: str) -> None:
            if handle and handle not in names and len(names) < ARTIFACT_ROWS:
                names.append(handle)

        for step in reversed(session.pending_steps):
            for handle in step.artifacts:
                remember(handle)
        for event in reversed(session.events):
            if len(names) >= ARTIFACT_ROWS:
                break
            for handle in event.artifacts:
                remember(handle)
        return names

    def _refresh_artifacts(self, session: Session) -> None:
        """重画产物清单。名单没变就不动，省掉一遍组件的重建。"""
        listing = self.query_one("#side-artifacts", SideOptionList)
        handles = self._artifact_handles(session)
        if handles == self._artifacts:
            return
        self._artifacts = handles
        listing.clear_options()
        if not handles:
            listing.add_option(Option("还没有产物", disabled=True))
            return
        for handle in handles:
            listing.add_option(Option(handle, id=handle))

    def refresh_agents(self, children: list[Session], current_id: str) -> None:
        """重画子 agent 列表。

        这一段**不走 `_write`**：既有判据断言侧栏打开后 `_written` 恰好是上下文那四个键，子 agent 列表是一组可点条目，不是几段文字。

        每条两行：第一行状态、名字、时间，第二行一句说明（失败原因或消息条数）。
        父会话取了哪一段历史、谁派发的，都不进这两行——那是打开之后才要读的细节，
        常驻三行会把这个列表撑到十行以上，而多数时间里用户只需要认名字。
        """
        if not self.has_class("-visible"):
            return
        title = self.query_one("#side-agents-title", Static)
        listing = self.query_one("#side-agents", SideOptionList)
        visible = bool(children)
        title.styles.display = "block" if visible else "none"
        listing.styles.display = "block" if visible else "none"
        if not visible:
            listing.clear_options()
            return
        title.update(f"子 AGENT ({len(children)})")
        listing.clear_options()
        # 标题按栏宽截：把右栏拖宽之后还按 12 字截，多出来的地方就白空着。
        width = max(16, self.content_size.width or 28)
        for child in children:
            marker = {
                "running": "●",
                "done": "✓",
                "failed": "✕",
                "cancelled": "‖",
            }.get(child.status, "○")
            now = "（当前）" if child.id == current_id else ""
            stamp = time.strftime("%H:%M", time.localtime(child.spawned_at or child.created_at))
            # 有失败/中断原因就显示原因，没有就显示消息条数：
            # 「失败」两个字不说明是请求出错还是工具没能完成，那正是用户要看的。
            note = child.status_note.strip()
            # 「仅创建」的子 agent 还是 idle，但任务书已经在了：说「空闲」会让人
            # 以为它什么都不用做，说「待开始」才对应界面里那条开始条。
            detail = (
                "待开始"
                if child.has_pending_prompt
                else SESSION_STATUS_TEXT.get(child.status, child.status)
            )
            if note:
                detail += f" · {_short(note, max(6, width - 18))}"
            else:
                detail += f" · {len(child.messages)} 条"
            listing.add_option(
                Option(
                    f"{marker} {_short(child.title, max(8, width - 16))}{now} · {stamp}\n  {detail}",
                    id=child.id,
                )
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """点中一行：产物交给应用去打开页面，子 agent 交给聊天界面去压栈。"""
        if not event.option.id:
            return
        if event.option_list.id == "side-artifacts":
            self.post_message(self.ArtifactPicked(event.option.id))
        else:
            self.post_message(self.AgentPicked(event.option.id))

    def focus_agents(self) -> bool:
        """把焦点交给子 agent 列表，返回有没有可选项。

        `/agents` 走这里：命令才是这个项目里稳定的入口，键位只留给最常按的几个。
        """
        listing = self.query_one("#side-agents", SideOptionList)
        if not listing.options:
            return False
        listing.focus()
        return True

    def _write(self, selector: str, text: str) -> None:
        """内容没变就不写，省掉一遍样式重算。"""
        if self._written.get(selector) == text:
            return
        self._written[selector] = text
        self.query_one(selector, Static).update(text)


class ChatScreen(Screen):
    """聊天主界面，消息区在上、编辑器在下、状态栏在最底。

    没有单独的标题栏：会话名、目录、分支、用量与侧栏开关全部放在底部那一行。
    标题栏与状态栏各占一行时，两行画的都是低优先级信息，而消息区被挤短两行。
    """

    # 鼠标在消息区按下时，`Screen.set_focus` 会给 `focused` 赋值，
    # `Screen._forward_event` 会给 `_select_state` 赋值。这两个响应式变量在 Textual 里
    # 都带着默认的重画标记，每赋值一次就调一次不带区域的 `Screen.refresh()`，
    # 把整块屏幕标成脏区域；合成器看到屏幕区域在脏区域里就走整屏重画那条分支。
    # 实测 34 行 120 列的终端里点一下消息区，屏幕上只有输入区左侧那一列的十个格子
    # 换了底色，应用却写出两帧整屏（各 4080 个格子）、共两万字节。Windows 终端经
    # ConPTY 不支持同步输出，这两帧分开送到，看起来就是点击时整屏抖一下。
    #
    # 两个变量各自的重画都已经由监视函数做掉了：`_watch_focused` 只刷新按键绑定，
    # `_watch__select_state` 把选区写进 `Screen.selections`、再由 `_watch_selections`
    # 通知受影响的组件重画自己。这里去掉那个多余的标记。
    focused: Reactive[Widget | None] = Reactive(None, repaint=False)
    _select_state: Reactive[SelectState | None] = Reactive(None, repaint=False)

    # 快捷键只留最常按的这三个：命令面板、退出、切 Agent。
    # 中断生成挂在输入框的 esc 上（见 `on_editor_pane_interrupt_requested`）：
    # 输入框自己接住 esc 关补全，没有弹窗可关时才把这一下交上来。
    # 其余操作一律走 /命令（输入框里打 / 会列出全部）或 ctrl+p 命令面板。
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Commands", priority=True),
        Binding("ctrl+c", "quit_app", "Quit", priority=True),
        Binding("tab", "switch_agent", "Agent"),
    ]

    # None 表示「不限制」；子 agent 界面按白名单过滤命令面板。
    allowed_commands: frozenset[str] | None = None
    # 子 agent 界面设成 False：Build/Plan 跟着父会话走，不能在这里改。
    allow_agent_switch = True
    # 子 agent 的返回栏里已经有会话名，底部状态栏就不必再写一遍。
    show_chat_title = True

    def __init__(
        self,
        root: Path | None = None,
        initial_prompt: str = "",
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self.root = root or Path.cwd()
        self._initial_prompt = initial_prompt
        # None = 跟随 session_manager.current（根会话）；显式 id = 子 agent。
        # 子 agent 绝不能跟随 current：用户点它的时候当前会话仍然是父会话。
        self._session_id = session_id
        self._busy = False
        self._unsubscribes: list[object] = []
        self._title_written = ""
        # 当前画出来的两栏宽度。拖动时按这一份加减，不读样式里的标量：
        # 终端变窄之后样式被夹过一档，读回来再加减会跳。
        self._rail_width = 30
        self._side_width = 30

    @property
    def session(self) -> Session:
        """这一屏正在显示的会话。"""
        if self._session_id:
            session = self.app.session_manager.get_session(self._session_id)
            if session is not None:
                return session
        return self.app.session_manager.current

    @property
    def loop(self):
        """这一屏对应的循环层。"""
        return self.app.host.loop_for(self.session.id)

    def allows_command(self, name: str) -> bool:
        """这一屏能不能执行某个斜杠命令。"""
        if self.allowed_commands is None:
            return True
        return name in self.allowed_commands

    @property
    def command_registry(self):
        """命令面板与补全用的命令表。子 agent 界面按白名单过滤。"""
        registry = self.app.command_registry
        if self.allowed_commands is None:
            return registry
        return registry.filtered(self.allowed_commands)

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="chat-body"):
                # 左栏是导航：上面是会话树（子 agent 缩进挂在父会话下），下面是这一轮的主题。
                # 中间是对话与输入框，右栏是上下文与产物。三栏的显隐互不牵连。
                with Vertical(id="chat-rail"):
                    yield SessionList(id="chat-sessions")
                    yield TopicList(id="chat-topics")
                yield Splitter(id="rail-split")
                # 对话与输入框放在同一栏里，两者的宽度天然一致：
                # 输入框横贯整屏时，它比消息区多出左右两栏的宽度，两边的文字起始列对不上。
                with Vertical(id="chat-main"):
                    yield MessagesPane(self._stop_consuming)
                    yield EditorPane(root=self.root)
                yield Splitter(sign=-1, id="side-split")
                yield SidePanel(id="side-panel")
            yield StatusBar()

    def on_mount(self) -> None:
        """挂载后订阅事件总线、重建消息并聚焦输入框。"""
        bus = self.app.event_bus
        self._unsubscribes = [
            bus.subscribe("message.add", self._on_message_add),
            bus.subscribe("part.add", self._on_part_add),
            bus.subscribe("part.update", self._on_part_update),
            bus.subscribe("turn.done", self._on_turn_done),
            bus.subscribe("event.sealed", self._on_event_sealed),
            bus.subscribe("step.recorded", self._on_step_recorded),
            bus.subscribe("status.update", self._on_status_update),
            bus.subscribe("context.metrics", self._on_context_metrics),
            bus.subscribe("agent.created", self._on_agent_changed),
            bus.subscribe("agent.status", self._on_agent_changed),
        ]
        self.side_toggle.render_state(self.side_panel.has_class("-visible"))
        self.apply_widths()
        self.rebuild()
        self.editor.focus_input()
        self.refresh_status()
        # 后台可能已经跑完了一轮：进屏幕时按真实状态校准一次 busy，
        # 否则会显示「空闲」，而输入框一提交就被 host 拒掉。
        self._busy = self.app.host.is_running(self.session.id)
        self.refresh_status()
        if self._initial_prompt:
            prompt = self._initial_prompt
            self._initial_prompt = ""
            self._submit_prompt(prompt)

    def on_unmount(self) -> None:
        """卸载时取消全部订阅。"""
        self._stop_consuming()

    def _stop_consuming(self) -> None:
        """取消全部订阅。

        消息区自己卸载时就调一次，比界面自己的卸载事件早：消息区走了之后界面没有
        地方可以画，再往下接事件只会去打一个已经摘掉的组件。
        """
        for unsubscribe in self._unsubscribes:
            unsubscribe()  # type: ignore[operator]
        self._unsubscribes = []

    def on_screen_resume(self) -> None:
        """弹窗关闭后按最新的模型与用法重画状态栏与侧栏。"""
        self.refresh_status()

    def on_resize(self, event: events.Resize) -> None:
        """窄终端里收起左栏：导航会把 80 列终端的正文挤到没法读。

        宽度按新尺寸重算一次：终端变窄时两栏一起收，变宽时把用户拖出来的那份
        宽度还回去。状态文件里那一份不动，免得临时缩小一次就把设定改掉。
        """
        if not self.is_mounted:
            return
        self.apply_widths()
        self.refresh_status()

    def apply_widths(self) -> None:
        """按终端宽度摆好左栏、右栏与两条分栏界线。

        状态文件里存的是用户拖出来的宽度，画的时候再按当前终端夹一次：
        终端临时变窄不该把设定改掉，变宽之后原来的宽度要能回来。
        """
        manager = self.app.session_manager
        total = self.app.size.width
        rail_visible = total >= 92
        side_visible = self.side_panel.has_class("-visible")
        self.query_one("#chat-rail").styles.display = "block" if rail_visible else "none"
        self.query_one("#rail-split").styles.display = "block" if rail_visible else "none"
        self.query_one("#side-split").styles.display = "block" if side_visible else "none"

        room = total - 1 - MIN_MAIN_WIDTH
        rail_room = room - (self._side_width + 1 if side_visible else 0)
        rail = max(RAIL_MIN, min(manager.rail_width, RAIL_MAX, max(RAIL_MIN, rail_room)))
        side_room = room - (rail + 1 if rail_visible else 0)
        side = max(SIDE_MIN, min(manager.side_width, SIDE_MAX, max(SIDE_MIN, side_room)))

        self._rail_width = rail
        self._side_width = side
        self.query_one("#chat-rail").styles.width = rail
        self.query_one("#side-panel").styles.width = side

    def on_splitter_resize_requested(self, event: Splitter.ResizeRequested) -> None:
        """拖动界线或按方向键：改左边或右边那一栏的宽度。"""
        manager = self.app.session_manager
        if event.splitter.id == "rail-split":
            manager.rail_width = max(
                RAIL_MIN, min(RAIL_MAX, self._rail_width + event.delta)
            )
        elif event.splitter.id == "side-split":
            manager.side_width = max(
                SIDE_MIN, min(SIDE_MAX, self._side_width + event.delta)
            )
        else:
            return
        self.apply_widths()

    def on_splitter_resize_finished(self, event: Splitter.ResizeFinished) -> None:
        """松手之后才写盘：拖动过程中每移一格都写一次状态文件太重。"""
        # 右栏里子 agent 那两行是按栏宽截出来的，松手之后重截一次；拖动过程中
        # 每移一格都遍历一遍会话里全部 Part，代价比得到的效果大。
        if event.splitter.id == "side-split":
            self.refresh_status()
        self.app.save_state()

    def _watch__select_state(self, select_state: SelectState | None) -> None:
        """清空选区时不重画整屏。

        基类在这个分支里调不带区域的 `self.refresh()`，把整屏标成脏区域。
        而 `Screen.clear_selection` 先写 `self.selections = {}`，
        `_watch_selections` 已经逐个通知原来有选区的组件重画自己；
        点一下没有拖动时本来就什么都没选，重画一整屏更是多余的。
        """
        if select_state is None:
            self._selecting = False
            return
        super()._watch__select_state(select_state)

    @property
    def messages(self) -> MessagesPane:
        """消息列表组件。"""
        return self.query_one(MessagesPane)

    @property
    def editor(self) -> EditorPane:
        """输入编辑器组件。"""
        return self.query_one(EditorPane)

    @property
    def status(self) -> StatusBar:
        """底部状态栏组件。"""
        return self.query_one(StatusBar)

    @property
    def side_panel(self) -> SidePanel:
        """右侧的上下文侧栏组件。"""
        return self.query_one(SidePanel)

    @property
    def side_toggle(self) -> SidebarToggle:
        """底部状态栏右端的侧栏开关。"""
        return self.query_one(SidebarToggle)

    def rebuild(self) -> None:
        """按当前会话内容重建消息列表。"""
        session = self.session
        self.messages.rebuild(
            session.messages, session, running=self.app.host.is_running(session.id)
        )
        self.messages.set_thinking(self.app.session_manager.show_thinking)
        self.refresh_rail()
        self.refresh_status()

    def refresh_rail(self) -> None:
        """重画左栏：会话树与这一轮的主题。

        主题清单读消息区算出来的那一份，不在这里另算一遍边界；换会话、新主题、
        封存与轮末都走这一个入口。
        """
        self.query_one("#chat-sessions", SessionList).refresh_sessions(with_children=True)
        self.query_one("#chat-topics", TopicList).refresh_topics(self.messages.topics)

    def on_session_row_picked(self, message: SessionRow.Picked) -> None:
        """点左栏的会话：切过去。"""
        message.stop()
        self.app.open_session(message.session_id)

    def on_topic_row_picked(self, message: TopicRow.Picked) -> None:
        """点左栏的主题：打开它并滚到那一章。"""
        message.stop()
        self.messages.focus_topic(message.key)

    def on_artifact_chip_picked(self, message: ArtifactChip.Picked) -> None:
        """点主题下面的产物名：在浏览器里打开它的网页视图。"""
        message.stop()
        self.app.open_artifact(message.handle)

    def _context_metrics(self) -> dict[str, object] | None:
        """最近一步的上下文度量；还没有发过请求时返回 None。

        读的是循环层现场记下的那一份，不在界面里另存一遍：`/context`、trace 与
        右端占比读的都是同一处，三处各存一份必然有一处会晚一步。
        """
        history = self.loop.context_history
        return history[-1] if history else None

    def refresh_status(self) -> None:
        """刷新状态栏、输入框状态行与右栏的会话上下文。"""
        session = self.session
        self.status.update_status(
            title=session.title if self.show_chat_title else "",
            context_tokens=session.last_input_tokens(),
            context_window=self.loop.context_window,
            busy=self._busy,
        )
        self.status.set_sidebar(self.side_panel.has_class("-visible"))
        self.editor.update_context(
            agent=session.agent_name,
            effort=session.effort,
        )
        self.side_panel.update_context(session)
        # 在子 agent 界面里显示的是兄弟，不是它自己的子会话（它不能有子会话）。
        # 锚点取父会话，两边看到的是同一组。
        anchor = session.parent_id or session.id
        self.side_panel.refresh_agents(
            self.app.host.children_of(anchor),
            session.id,
        )

    def set_sidebar(self, visible: bool) -> bool:
        """设定侧栏可见性，返回设定之后的状态。

        三条路都走这里：`/sidebar` 命令、点状态栏那端的开关、子 agent 刚建出来时自动打开。
        各写一份的话，按钮上的字会跟实际状态对不上——「收起」两个字配着一条关着的侧栏。
        """
        panel = self.side_panel
        panel.set_class(visible, "-visible")
        # 右栏出现或消失会改变中间那一栏的可用宽度，界线与两栏宽度一起重算。
        self.apply_widths()
        self.status.set_sidebar(visible)
        # 侧栏关着的时候内容是被跳过的，打开时必须在这里补一次刷新。
        self.refresh_status()
        return visible

    def toggle_sidebar(self) -> bool:
        """显示或隐藏上下文侧栏，返回切换后的可见状态。"""
        return self.set_sidebar(not self.side_panel.has_class("-visible"))

    def on_sidebar_toggle_toggled(self, event: SidebarToggle.Toggled) -> None:
        """点了状态栏右端的开关。"""
        event.stop()
        self.toggle_sidebar()

    def on_editor_pane_submitted(self, event: EditorPane.Submitted) -> None:
        """处理编辑器提交的输入。"""
        if event.mode == "command":
            self.app.execute_command(event.text.lstrip("/").strip())
            return
        session = self.session
        if event.mode == "bash":
            command = event.text.lstrip("!").strip()
            if not self.app.host.run_bash(session.id, command):
                self.app.show_toast("这个会话还在跑，先按 esc 中断", "warning")
                return
            self._mark_busy(True)
            return
        self._submit_prompt(event.text)

    def _submit_prompt(self, text: str) -> None:
        """把一段普通文本作为用户消息发出，并启动流式回复。

        写消息与起任务都在 `AgentHost` 里：后台派发与这里必须走同一条路，
        否则同一种「用户说了一句话」会有两种消息形状。
        """
        if not self.app.is_configured:
            # 把这句话放回输入框：配完接口回来按回车就能重发，不用重打一遍。
            # 同时把表单弹出来，提示不是一闪而过的 toast，而是当场能填的地方。
            self.editor.text = text
            self.app.show_toast(
                "还没有配置模型接口，先填接口地址、密钥与模型名", "warning"
            )
            self.app.execute_command("model")
            return
        if not self.app.host.prompt(self.session.id, text):
            self.app.show_toast("这个会话还在跑，先按 esc 中断", "warning")
            return
        self._mark_busy(True)

    def _mark_busy(self, busy: bool) -> None:
        """切到生成中并刷新状态栏。"""
        self._busy = busy
        self.messages.set_running(busy)
        self.refresh_status()

    def on_editor_pane_agent_changed(self, event: EditorPane.AgentChanged) -> None:
        """处理 Agent 切换。子 agent 界面直接拒绝，不改身份。"""
        if not self.allow_agent_switch:
            self.app.show_toast("子 agent 的 Build/Plan 跟着父会话走，不能切换", "warning")
            return
        session = self.session
        session.agent_name = event.agent
        self.app.session_manager.last_agent = event.agent
        self.refresh_status()
        self.app.show_toast(f"已切换 Agent：{event.agent}", "success")

    def action_command_palette(self) -> None:
        """打开命令面板，先把补全弹窗收掉。

        不收的话面板叠在上面、下面那个弹窗还开着，等面板退回来它仍在。
        这里只收弹窗、不算"用户按了 esc"：`_command_dismissed` 留着 false，
        面板选完命令之后补全照旧可用。
        """
        self.editor.command_popup.hide()
        self.app.push_screen(
            CommandPalette(self.command_registry), self._on_palette_result
        )

    def on_editor_pane_interrupt_requested(self) -> None:
        """输入框没有弹窗可关时按了 esc：中断当前生成。

        空闲状态下 esc 不做任何事：没有东西可中断，弹一句"已中断"只会让人以为
        自己误触了什么。
        """
        if self._busy:
            self.action_interrupt()

    def action_interrupt(self) -> None:
        """中断当前生成。只中断这一屏对应的会话。"""
        self.app.host.cancel(self.session.id)
        # 取消后台任务会在下一个 await 点把这个协程摘掉，循环层里"把工具标成失败"那一段
        # 就再也走不到了，工具会一直停在 running。这里补上这一步，并把卡片刷成失败态。
        for part in self._unfinished_tools():
            part.tool_status = ToolStatus.FAILED
            part.tool_output = "interrupted"
            self.messages.update_part(part)
        # 取消这一条不走 `turn.done`（循环层明确跳过收尾），消息区不会自己知道已经停下：
        # 不收回来，主题头会一直写着「进行中」。
        self.messages.set_running(False)
        self._busy = False
        self.refresh_status()
        self.app.save_state()
        self.app.show_toast("已中断当前生成", "warning")

    def _unfinished_tools(self) -> list[Part]:
        """当前会话里还停在等待、询问或执行中的工具 Part。

        询问中的也要算进来：中断时循环层自己会去拒掉那个 future，
        但 worker 在下一个 await 点就被摘掉了，那一段走不到，卡片会一直停在询问上。
        """
        return [
            part
            for message in self.session.messages
            for part in message.parts
            if part.type is PartType.TOOL
            and part.tool_status in (ToolStatus.PENDING, ToolStatus.AWAITING, ToolStatus.RUNNING)
        ]

    def action_quit_app(self) -> None:
        """退出应用。"""
        self.app.execute_command("exit")

    def action_switch_agent(self) -> None:
        """用 tab 打开 Agent 列表。"""
        self.editor.action_agent_popup()

    def on_side_panel_agent_picked(self, event: SidePanel.AgentPicked) -> None:
        """右栏点了一个子 agent：压栈进入它的界面。"""
        self.app.open_subagent(event.session_id)

    def on_side_panel_artifact_picked(self, event: SidePanel.ArtifactPicked) -> None:
        """右栏点了一个产物：在浏览器里打开它的网页视图。"""
        event.stop()
        self.app.open_artifact(event.handle)

    def _on_palette_result(self, command_name: str | None) -> None:
        """命令面板选中的命令。"""
        if command_name:
            self.app.execute_command(command_name)

    def _belongs(self, data: dict[str, object]) -> bool:
        """这条事件是不是这一屏该管的。

        没有 `session_id` 的事件照旧处理（老事件与测试里的手工发布），
        有的话必须与当前会话一致：后台子 agent 的流不能写进父会话的消息区。
        """
        session_id = str(data.get("session_id", ""))
        return not session_id or session_id == self.session.id

    def _find_message(self, message_id: str) -> Message | None:
        """在当前会话里按 ID 找消息。"""
        for message in self.session.messages:
            if message.id == message_id:
                return message
        return None

    def _on_message_add(self, data: dict[str, object]) -> None:
        """事件总线：新增消息。

        新的用户消息意味着上一个主题已经做完：先把更早的主题收起来，再把消息
        接上去。用户主动点开过的主题留在原样（消息区自己记着）。
        """
        if not self._belongs(data):
            return
        message = self._find_message(str(data.get("message_id", "")))
        if message is not None:
            self.messages.fold_sealed_topics()
            self.messages.add_message(message)
            self.refresh_rail()

    def _on_event_sealed(self, data: dict[str, object]) -> None:
        """事件总线：一个主题封存了。卡片上的标题、状态与结论换成封存后的那一份。"""
        if not self._belongs(data):
            return
        self.messages.refresh_topics()
        self.refresh_rail()

    def _on_step_recorded(self, data: dict[str, object]) -> None:
        """事件总线：一步跑完了。主题头的步数与当前动作跟着变。"""
        if not self._belongs(data):
            return
        self.messages.refresh_live()
        self.refresh_rail()

    def _on_part_add(self, data: dict[str, object]) -> None:
        """事件总线：新增 Part。"""
        if not self._belongs(data):
            return
        message = self._find_message(str(data.get("message_id", "")))
        if message is None:
            return
        part = message.part_by_id(str(data.get("part_id", "")))
        if part is not None:
            self.messages.add_part(message.id, part)

    def _on_part_update(self, data: dict[str, object]) -> None:
        """事件总线：更新 Part。"""
        if not self._belongs(data):
            return
        message = self._find_message(str(data.get("message_id", "")))
        if message is None:
            return
        part = message.part_by_id(str(data.get("part_id", "")))
        if part is not None:
            self.messages.update_part(part)
            # 工具状态变了才刷主题头。正文每来一小段都刷一遍，等于把同一份快照
            # 算几十次，而第二行里的「正在运行 X」只跟工具状态有关。
            if part.type is PartType.TOOL:
                self.messages.refresh_live()

    def _on_turn_done(self, data: dict[str, object]) -> None:
        """事件总线：一轮生成结束。"""
        if not self._belongs(data):
            return
        self._busy = False
        self.messages.set_running(False)
        self.messages.flush_text()
        self.refresh_rail()
        self.refresh_status()
        # 最后一个 token 之后虚拟高度还没重算，立刻滚会停在差几行的位置，等这次刷新结束再滚
        self.call_after_refresh(self.messages.scroll_to_bottom)

    def _on_status_update(self, data: dict[str, object]) -> None:
        """事件总线：状态刷新。"""
        if self._belongs(data):
            self.refresh_status()

    def _on_context_metrics(self, data: dict[str, object]) -> None:
        """事件总线：这一步的装配度量到了。

        只重画右端的百分比：这一步没换会话、没换模型，左端与侧栏都没有新东西，
        为一行百分比把整块侧栏的清单重走一遍不值得。
        """
        if not self._belongs(data):
            return
        self.status.update_usage(
            context_tokens=self.session.last_input_tokens(),
            context_window=self.loop.context_window,
        )

    def on_context_usage_pressed(self, event: ContextUsage.Pressed) -> None:
        """点了右端的占比数字：在状态栏上方展开柱状图，水平中心对准数字。"""
        event.stop()
        metrics = self._context_metrics()
        if metrics is None:
            self.app.show_toast("还没有上下文度量，发起一轮生成后就有了", "info")
            return
        widget = self.query_one(ContextUsage)
        anchor = widget.region.x + widget.region.width // 2
        self.app.push_screen(ContextUsageScreen(metrics, anchor))

    def _on_agent_changed(self, data: dict[str, object]) -> None:
        """事件总线：子 agent 建了或状态变了。

        只重画右栏：消息区与状态栏跟子 agent 的状态无关。中断状态下
        `turn.done` 不会来，`_busy` 必须靠 `agent.status` 收回来。
        """
        session_id = str(data.get("session_id", ""))
        if not session_id:
            return
        # 刚建出来的那一条：把右栏打开。用户不必先摸到 /sidebar 才看得到
        # 自己刚派出去的东西——这是「创建后右侧栏就会出现」的落点。
        if data.get("parent_id"):
            self.set_sidebar(True)
        status = str(data.get("status", ""))
        if session_id == self.session.id:
            self._busy = status == "running"
        elif status in ("done", "failed", "cancelled"):
            # 子 agent 在后台跑完时给一句提示：右栏的状态点会变，但用户
            # 正盯着消息区，不看右栏的话不会注意到。
            child = self.app.session_manager.get_session(session_id)
            anchor = self.session.parent_id or self.session.id
            if child is not None and child.parent_id == anchor:
                label = {"done": "完成", "failed": "失败", "cancelled": "已中断"}[status]
                kind = "success" if status == "done" else "warning"
                self.app.show_toast(f"子 agent「{child.title}」{label}", kind)
        self.refresh_status()
