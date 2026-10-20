from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.selection import SelectState
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker

from ..core.agent.permission import Verdict, argument_value
from ..core.file_refs import resolve_file_refs
from ..core.models import Message, Part, PartType, Session, ToolStatus
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import EditorPane
from ..widgets.messages_pane import STATUS_ICON, MessagesPane
from ..widgets.status_bar import StatusBar, format_tokens
from .permission import PermissionAnswer, PermissionDialog


class SidePanel(Vertical):
    """会话上下文侧栏，默认隐藏，由 /sidebar 切换。

    只放消息区里看不到的东西：会话标题、消息条数与 token 用量、引用文件、工具清单。
    模型名与 Agent 名不在这里出现，那一对已经在输入框下方的状态行上。
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._written: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("上下文", classes="side-title")
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

    def __init__(self, root: Path | None = None, initial_prompt: str = "") -> None:
        super().__init__()
        self.root = root or Path.cwd()
        self._initial_prompt = initial_prompt
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
            bus.subscribe("permission.ask", self._on_permission_ask),
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
        self.app._sync_ledger()
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
        """打开命令面板，先把补全弹窗收掉。

        不收的话面板叠在上面、下面那个弹窗还开着，等面板退回来它仍在。
        这里只收弹窗、不算"用户按了 esc"：`_command_dismissed` 留着 false，
        面板选完命令之后补全照旧可用。
        """
        self.editor.command_popup.hide()
        self.app.push_screen(CommandPalette(self.app.command_registry), self._on_palette_result)

    def on_editor_pane_interrupt_requested(self) -> None:
        """输入框没有弹窗可关时按了 esc：中断当前生成。

        空闲状态下 esc 不做任何事：没有东西可中断，弹一句"已中断"只会让人以为
        自己误触了什么。
        """
        if self._busy:
            self.action_interrupt()

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
        """当前会话里还停在等待、询问或执行中的工具 Part。

        询问中的也要算进来：中断时循环层自己会去拒掉那个 future，
        但 worker 在下一个 await 点就被摘掉了，那一段走不到，卡片会一直停在询问上。
        """
        return [
            part
            for message in self.app.session_manager.current.messages
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

    def _on_permission_ask(self, data: dict[str, object]) -> None:
        """事件总线：工具调用需要用户答复。

        循环层停在这个 Part 的 future 上，答复由弹窗的回调交回去。
        弹窗在没有给出答复的情况下被关掉（应用正在退出、会话被换掉）时按拒绝处理，
        否则这一轮的 worker 会一直停在等待上。
        """
        message = self._find_message(str(data.get("message_id", "")))
        if message is None:
            return
        part = message.part_by_id(str(data.get("part_id", "")))
        if part is None:
            return
        tool = self.app.agent.registry.get(part.tool_name)
        argument = argument_value(tool, part.tool_input) if tool is not None else ""
        dialog = PermissionDialog(
            part.tool_name,
            argument,
            self.app.session_manager.current.agent_name,
            part.permission,
        )
        self.app.push_screen(dialog, lambda answer: self._answer_permission(part.id, answer))

    def _answer_permission(self, part_id: str, answer: PermissionAnswer | None) -> None:
        """把弹窗的答复交给循环层。"""
        if answer is None:
            self.app.agent.resolve_permission(part_id, Verdict.DENY)
            return
        self.app.agent.resolve_permission(part_id, answer.verdict, answer.remember)

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
