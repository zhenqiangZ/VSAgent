from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.selection import SelectState
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker

from ..core.command_registry import CommandRegistry
from ..core.file_refs import resolve_file_refs
from ..core.models import Message, Part, PartType, Session, ToolStatus
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import EditorPane
from ..widgets.messages_pane import STATUS_ICON, MessagesPane
from ..widgets.status_bar import StatusBar, format_tokens

LEADER_KEYS = sorted(CommandRegistry().leader_keys())


class SidePanel(Vertical):
    """会话上下文侧栏，默认隐藏，由 leader b 切换。

    只放消息区里看不到的东西：会话标题、消息条数与 token 用量、引用文件、工具清单。
    模型名与 Agent 名不在这里出现，那一对已经在输入框下方的状态行上。
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._written: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("上下文", classes="side-title")
        yield Static("", id="side-session", classes="side-line")
        yield Static("", id="side-stats", classes="side-line")
        yield Static("引用文件", classes="side-section")
        yield Static("", id="side-files", classes="side-body")
        yield Static("工具调用", classes="side-section")
        yield Static("", id="side-tools", classes="side-body")

    def update_context(self, session: Session) -> None:
        """按当前会话刷新标题、用量、引用文件与工具清单。

        侧栏没打开时整个跳过：底下这两份清单要把会话里全部 Part 走一遍，
        而它们算出来只会写进一个不显示的组件。打开侧栏的那一处会重新调一次。
        """
        if not self.has_class("-visible"):
            return
        self._write("#side-session", session.title)
        stats = f"{len(session.messages)} 条消息 · {format_tokens(session.total_tokens())} tokens"
        self._write("#side-stats", stats)
        self._write("#side-files", self._files_text(session))
        self._write("#side-tools", self._tools_text(session))

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

    # 按下前导键后等第二击的时间。超时就把等待状态收回去。
    LEADER_SECONDS = 1.5

    BINDINGS = [
        Binding("ctrl+x", "leader_key", "Leader", priority=True),
        Binding("ctrl+p", "command_palette", "Commands", priority=True),
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+d", "quit_app", "Quit", priority=True),
        Binding("ctrl+z", "suspend", "Suspend", priority=True),
        Binding("escape", "close_modal", "Close"),
        Binding("tab", "switch_agent", "Agent"),
        *[
            Binding(key, f"leader_{key}", f"leader {key}", priority=True)
            for key in LEADER_KEYS
        ],
    ]

    def __init__(self, root: Path | None = None, initial_prompt: str = "") -> None:
        super().__init__()
        self.root = root or Path.cwd()
        self._initial_prompt = initial_prompt
        self._leader_pending = False
        self._leader_timer: Timer | None = None
        self._leader_token = 0
        self._worker: Worker[object] | None = None
        self._busy = False
        self._unsubscribes: list[object] = []

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
        ]
        self.rebuild()
        self.editor.focus_input()
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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """前导键之后的第二击只有在前导键按下后才启用。"""
        if action.startswith("leader_") and action != "leader_key":
            return self._leader_pending
        return True

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
        session = self.app.session_manager.current
        self.messages.rebuild(session.messages)
        self.messages.set_details(self.app.session_manager.show_details)
        self.messages.set_thinking(self.app.session_manager.show_thinking)
        self.refresh_status()

    def refresh_status(self) -> None:
        """刷新状态栏、输入框状态行与侧栏的会话上下文。"""
        session = self.app.session_manager.current
        self.status.update_status(
            tokens=session.total_tokens(),
            cost=session.total_cost(),
            latency_ms=self.app.agent.last_latency_ms,
            context_window=self.app.context_window,
            busy=self._busy,
        )
        self.editor.update_context(
            agent=session.agent_name,
            model=self.app.current_model_label,
            effort=session.effort,
        )
        self.side_panel.update_context(session)

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
        session = self.app.session_manager.current
        if event.mode == "bash":
            command = event.text.lstrip("!").strip()
            message = session.add_message(Message(role="user", session_id=session.id))
            message.add_part(Part(type=PartType.TEXT, text=event.text))
            self.messages.add_message(message)
            self._start(self.app.agent.run_bash(session, command))
            return
        self._submit_prompt(event.text)

    def _submit_prompt(self, text: str) -> None:
        """把一段普通文本作为用户消息发出，并启动流式回复。"""
        session = self.app.session_manager.current
        clean_text, file_parts = resolve_file_refs(text, self.root)
        message = session.add_message(
            Message(role="user", session_id=session.id, model_id=session.model_id)
        )
        message.add_part(Part(type=PartType.TEXT, text=clean_text))
        for part in file_parts:
            message.add_part(part)
        self.messages.add_message(message)
        self._start(self.app.agent.stream_reply(session, clean_text))

    def on_editor_pane_agent_changed(self, event: EditorPane.AgentChanged) -> None:
        """处理 Agent 切换。"""
        session = self.app.session_manager.current
        session.agent_name = event.agent
        self.app.session_manager.last_agent = event.agent
        self.refresh_status()
        self.app.show_toast(f"已切换 Agent：{event.agent}", "success")

    def action_command_palette(self) -> None:
        """打开命令面板。"""
        self.app.push_screen(CommandPalette(self.app.command_registry), self._on_palette_result)

    def action_interrupt(self) -> None:
        """中断当前生成。"""
        self.app.agent.cancel()
        if self._worker is not None:
            self._worker.cancel()
        # 取消 worker 会在下一个 await 点把这个协程摘掉，循环层里"把工具标成失败"那一段
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
        """当前会话里还停在 pending 或 running 的工具 Part。"""
        return [
            part
            for message in self.app.session_manager.current.messages
            for part in message.parts
            if part.type is PartType.TOOL
            and part.tool_status in (ToolStatus.PENDING, ToolStatus.RUNNING)
        ]

    def action_quit_app(self) -> None:
        """退出应用。"""
        self.app.execute_command("exit")

    def action_suspend(self) -> None:
        """挂起提示：Windows 上不支持挂起。"""
        self.app.show_toast("Windows 终端不支持挂起（ctrl+z）", "warning")

    def action_close_modal(self) -> None:
        """退出前导键等待状态。"""
        self._clear_leader()

    def action_switch_agent(self) -> None:
        """用 tab 打开 Agent 列表。"""
        self.editor.action_agent_popup()

    def action_leader_key(self) -> None:
        """按下前导键，进入等待第二击的状态。"""
        self._leader_pending = True
        self.editor.add_class("-leader")
        self.editor.refresh_status_line()
        self._arm_leader_timer()

    def _arm_leader_timer(self) -> None:
        """给这一次等待重新计时。

        只停掉旧的定时器不够：到点的回调已经排在消息队列里，取消不掉，
        它可能在又一次按下前导键之后才轮到自己跑。每次等待发一个号，
        回调带着号回来，只认自己那一次。
        """
        self._leader_token += 1
        token = self._leader_token
        if self._leader_timer is not None:
            self._leader_timer.stop()
        self._leader_timer = self.set_timer(
            self.LEADER_SECONDS, lambda: self._expire_leader(token)
        )

    def _expire_leader(self, token: int) -> None:
        """等待超时回调。只有当前这一次到点才收回等待状态。"""
        if token == self._leader_token:
            self._clear_leader()

    def action_leader_b(self) -> None:
        """leader b：切换上下文侧栏。"""
        self._run_leader("b")

    def action_leader_c(self) -> None:
        """leader c：压缩会话。"""
        self._run_leader("c")

    def action_leader_o(self) -> None:
        """leader o：添加提供商。"""
        self._run_leader("o")

    def action_leader_d(self) -> None:
        """leader d：切换工具详情。"""
        self._run_leader("d")

    def action_leader_e(self) -> None:
        """leader e：外部编辑器。"""
        self._run_leader("e")

    def action_leader_q(self) -> None:
        """leader q：退出。"""
        self._run_leader("q")

    def action_leader_x(self) -> None:
        """leader x：导出对话。"""
        self._run_leader("x")

    def action_leader_h(self) -> None:
        """leader h：帮助。"""
        self._run_leader("h")

    def action_leader_i(self) -> None:
        """leader i：初始化 AGENTS.md。"""
        self._run_leader("i")

    def action_leader_m(self) -> None:
        """leader m：模型列表。"""
        self._run_leader("m")

    def action_leader_n(self) -> None:
        """leader n：新会话。"""
        self._run_leader("n")

    def action_leader_r(self) -> None:
        """leader r：重做。"""
        self._run_leader("r")

    def action_leader_l(self) -> None:
        """leader l：会话列表。"""
        self._run_leader("l")

    def action_leader_s(self) -> None:
        """leader s：分享会话。"""
        self._run_leader("s")

    def action_leader_t(self) -> None:
        """leader t：主题列表。"""
        self._run_leader("t")

    def action_leader_u(self) -> None:
        """leader u：撤销。"""
        self._run_leader("u")

    def _run_leader(self, key: str) -> None:
        """按第二击执行对应命令。"""
        self._clear_leader()
        command = self.app.command_registry.by_leader_key(key)
        if command is None:
            return
        self.app.execute_command(command.name)

    def _clear_leader(self) -> None:
        """清除前导键等待状态。"""
        self._leader_pending = False
        self.editor.remove_class("-leader")
        self.editor.refresh_status_line()

    def _on_palette_result(self, command_name: str | None) -> None:
        """命令面板选中的命令。"""
        if command_name:
            self.app.execute_command(command_name)

    def _start(self, coroutine: object) -> None:
        """启动一次生成任务。"""
        self._busy = True
        self.refresh_status()
        self._worker = self.run_worker(
            coroutine,  # type: ignore[arg-type]
            group="agent",
            exclusive=True,
        )

    def _find_message(self, message_id: str) -> Message | None:
        """在当前会话里按 ID 找消息。"""
        for message in self.app.session_manager.current.messages:
            if message.id == message_id:
                return message
        return None

    def _on_message_add(self, data: dict[str, object]) -> None:
        """事件总线：新增消息。"""
        message = self._find_message(str(data.get("message_id", "")))
        if message is not None:
            self.messages.add_message(message)

    def _on_part_add(self, data: dict[str, object]) -> None:
        """事件总线：新增 Part。"""
        message = self._find_message(str(data.get("message_id", "")))
        if message is None:
            return
        part = message.part_by_id(str(data.get("part_id", "")))
        if part is not None:
            self.messages.add_part(message.id, part)

    def _on_part_update(self, data: dict[str, object]) -> None:
        """事件总线：更新 Part。"""
        message = self._find_message(str(data.get("message_id", "")))
        if message is None:
            return
        part = message.part_by_id(str(data.get("part_id", "")))
        if part is not None:
            self.messages.update_part(part)

    def _on_turn_done(self, data: dict[str, object]) -> None:
        """事件总线：一轮生成结束。"""
        self._busy = False
        self.messages.flush_text()
        self.refresh_status()
        # 最后一个 token 之后虚拟高度还没重算，立刻滚会停在差几行的位置，等这次刷新结束再滚
        self.call_after_refresh(self.messages.scroll_to_bottom)

    def _on_status_update(self, data: dict[str, object]) -> None:
        """事件总线：状态刷新。"""
        self.refresh_status()
