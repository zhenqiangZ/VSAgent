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
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import EditorPane
from ..widgets.messages_pane import STATUS_ICON, MessagesPane
from ..widgets.status_bar import StatusBar, format_tokens


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


def _status_text(status: str) -> str:
    """子 agent 状态的中文说法。"""
    return {
        "idle": "空闲",
        "running": "运行中",
        "done": "完成",
        "failed": "失败",
        "cancelled": "已中断",
    }.get(status, status)



class AgentOptionList(OptionList):
    """右栏的子 agent 列表。

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
    """会话右栏：子 agent 列表在上，上下文信息在下。

    子 agent 段只在有子会话时出现；没有时不占任何高度，/sidebar 的行为与以前一致。
    列表用 `OptionList` 而不是 `Static`：它要能点。
    """

    class AgentPicked(TextualMessage):
        """用户点了右栏里的一个子 agent。"""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._written: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("子 AGENT", id="side-agents-title", classes="side-section")
        yield AgentOptionList(id="side-agents")
        yield Static("上下文", id="side-context-title", classes="side-section")
        yield Static("", id="side-session", classes="side-line", markup=False)
        yield Static("", id="side-stats", classes="side-line", markup=False)
        yield Static("引用文件", classes="side-section")
        yield Static("", id="side-files", classes="side-body", markup=False)
        yield Static("工具调用", classes="side-section")
        yield Static("", id="side-tools", classes="side-body", markup=False)

    def update_context(self, session: Session) -> None:
        """按当前会话刷新标题、用量、引用文件与工具清单。

        侧栏没打开时整个跳过：底下这两份清单要把会话里全部 Part 走一遍，
        而它们算出来只会写进一个不显示的组件。打开侧栏的那一处会重新调一次。
        """
        if not self.has_class("-visible"):
            return
        self._write("#side-session", session.title)
        stats = f"{len(session.messages)} 条消息 · {format_tokens(session.total_tokens())} tokens"
        cached = session.total_cached()
        uncached = session.total_uncached()
        if cached and cached + uncached:
            stats += f" · 缓存命中 {round(cached / (cached + uncached) * 100)}%"
        self._write("#side-stats", stats)
        self._write("#side-files", self._files_text(session))
        self._write("#side-tools", self._tools_text(session))

    def refresh_agents(self, children: list[Session], current_id: str) -> None:
        """重画子 agent 列表。

        这一段**不走 `_write`**：既有判据断言侧栏打开后 `_written` 恰好是
        上下文那四个键，子 agent 列表是一组可点条目，不是几段文字。
        """
        if not self.has_class("-visible"):
            return
        title = self.query_one("#side-agents-title", Static)
        listing = self.query_one("#side-agents", AgentOptionList)
        visible = bool(children)
        title.styles.display = "block" if visible else "none"
        listing.styles.display = "block" if visible else "none"
        if not visible:
            listing.clear_options()
            return
        title.update(f"子 AGENT ({len(children)})")
        listing.clear_options()
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
            detail = "待开始" if child.has_pending_prompt else _status_text(child.status)
            if note:
                detail += f" · {_short(note, 8)}"
            else:
                detail += f" · {len(child.messages)} 条"
            if child.seed_end > child.seed_start:
                span = f"第 {child.seed_start + 1}–{child.seed_end} 条"
            else:
                span = "无父会话历史"
            origin = "用户派发" if child.origin == "user" else "模型派发"
            listing.add_option(
                Option(
                    f"{marker} {_short(child.title, 12)}{now}\n"
                    f"  {detail} · {stamp}\n"
                    f"  {span} · {origin}",
                    id=child.id,
                )
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """点中一行：交给聊天界面去压栈。"""
        if event.option.id:
            self.post_message(self.AgentPicked(event.option.id))

    def focus_agents(self) -> bool:
        """把焦点交给子 agent 列表，返回有没有可选项。

        `/agents` 走这里：命令才是这个项目里稳定的入口，键位只留给最常按的几个。
        """
        listing = self.query_one("#side-agents", AgentOptionList)
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

    @staticmethod
    def _files_text(session: Session) -> str:
        """列出会话里引用过的文件，按首次出现的顺序排列。"""
        paths: list[str] = []
        for message in session.messages:
            for part in message.parts:
                if part.type is PartType.FILE and part.file_path not in paths:
                    paths.append(part.file_path)
        return "\n".join(paths) if paths else "（暂无）"

    @staticmethod
    def _tools_text(session: Session) -> str:
        """列出会话里调用过的工具与它最后一次的状态。"""
        tools: dict[str, ToolStatus] = {}
        for message in session.messages:
            for part in message.parts:
                if part.type is PartType.TOOL and part.tool_name:
                    tools[part.tool_name] = part.tool_status or ToolStatus.PENDING
        lines = [f"{STATUS_ICON[status]} {name}" for name, status in tools.items()]
        return "\n".join(lines) if lines else "（暂无）"


class ChatScreen(Screen):
    """聊天主界面，消息区在上、编辑器在下、状态栏在最底。"""

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
                yield MessagesPane(self._stop_consuming)
                yield SidePanel(id="side-panel")
            yield EditorPane(root=self.root)
            yield StatusBar()

    def on_mount(self) -> None:
        """挂载后订阅事件总线、重建消息并聚焦输入框。"""
        bus = self.app.event_bus
        self._unsubscribes = [
            bus.subscribe("message.add", self._on_message_add),
            bus.subscribe("part.add", self._on_part_add),
            bus.subscribe("part.update", self._on_part_update),
            bus.subscribe("turn.done", self._on_turn_done),
            bus.subscribe("status.update", self._on_status_update),
            bus.subscribe("agent.created", self._on_agent_changed),
            bus.subscribe("agent.status", self._on_agent_changed),
        ]
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

    def rebuild(self) -> None:
        """按当前会话内容重建消息列表。"""
        session = self.session
        self.messages.rebuild(session.messages)
        self.messages.set_details(self.app.session_manager.show_details)
        self.messages.set_thinking(self.app.session_manager.show_thinking)
        self.refresh_status()

    def refresh_status(self) -> None:
        """刷新状态栏、输入框状态行与右栏的会话上下文。"""
        session = self.session
        self.status.update_status(
            tokens=session.total_tokens(),
            cost=session.total_cost(),
            latency_ms=self.loop.last_latency_ms,
            context_window=self.loop.context_window,
            busy=self._busy,
        )
        self.editor.update_context(
            agent=session.agent_name,
            model=self.app.current_model_label,
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

    def toggle_sidebar(self) -> bool:
        """显示或隐藏上下文侧栏，返回切换后的可见状态。

        显示时必须在这里补一次刷新：侧栏关着的时候它的内容是被跳过的。
        """
        panel = self.side_panel
        visible = not panel.has_class("-visible")
        panel.set_class(visible, "-visible")
        self.refresh_status()
        return visible

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
        """事件总线：新增消息。"""
        if not self._belongs(data):
            return
        message = self._find_message(str(data.get("message_id", "")))
        if message is not None:
            self.messages.add_message(message)

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

    def _on_turn_done(self, data: dict[str, object]) -> None:
        """事件总线：一轮生成结束。"""
        if not self._belongs(data):
            return
        self._busy = False
        self.messages.flush_text()
        self.refresh_status()
        # 最后一个 token 之后虚拟高度还没重算，立刻滚会停在差几行的位置，等这次刷新结束再滚
        self.call_after_refresh(self.messages.scroll_to_bottom)

    def _on_status_update(self, data: dict[str, object]) -> None:
        """事件总线：状态刷新。"""
        if self._belongs(data):
            self.refresh_status()

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
            self.side_panel.set_class(True, "-visible")
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
