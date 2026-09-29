from __future__ import annotations

from pathlib import Path

from textual.app import App

from .core.agent.loop import AgentLoop
from .core.agent.provider import OpenAICompatProvider, ProviderConfig
from .core.agent.tools import BashTool, ToolRegistry
from .core.command_registry import CommandRegistry
from .core.event_bus import EventBus
from .core.exporter import export_filename, render_session_markdown
from .core.external_editor import (
    editor_buffer_path,
    launch,
    read_buffer,
    resolve_editor,
    write_buffer,
)
from .core.keybind_manager import KeybindManager
from .core.models import EFFORTS, Message
from .core.session_manager import SessionManager
from .env import load_env
from .screens.chat import ChatScreen
from .screens.diff_overlay import DiffOverlayScreen
from .screens.help import HelpScreen
from .screens.home import HomeScreen
from .screens.model_selector import ModelInfo, ModelSelectorScreen
from .screens.session_picker import SessionPickerScreen
from .screens.theme_selector import ThemeSelectorScreen
from .widgets.toast import ToastNotification


class OpenCodeApp(App[None]):
    """opencode TUI 的应用入口，持有核心层并管理屏幕切换。"""

    CSS_PATH = "styles/app.tcss"
    TITLE = "opencode tui"
    TOAST_SECONDS = 3.0
    # 关掉 Textual 自带的命令面板，改用本项目实现的 CommandPalette
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        root: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.root = root or Path.cwd()
        self.session_manager = SessionManager(state_path)
        self.command_registry = CommandRegistry()
        self.keybind_manager = KeybindManager(self.command_registry)
        self.event_bus = EventBus()
        self.agent = self._build_agent()
        self._removed_message: Message | None = None
        self._removed_session_id = ""

    def _build_agent(self) -> AgentLoop:
        """装配循环层。

        连接参数、单价与上下文窗口都取自项目根目录的 `.env`。
        """
        load_env()
        config = ProviderConfig.from_env()
        registry = ToolRegistry()
        registry.register(BashTool(cwd=self.root))
        return AgentLoop(
            self.event_bus,
            OpenAICompatProvider(config),
            registry,
            config,
            context_window=config.context_window,
        )

    def on_mount(self) -> None:
        """启动时载入状态并进入启动界面。"""
        loaded = self.session_manager.load_from_toml()
        if loaded:
            self.theme = self.session_manager.theme
        if not self.session_manager.sessions:
            self.session_manager.new_session()
        self.event_bus.subscribe("turn.done", self._on_turn_done)
        self.push_screen(HomeScreen())

    @property
    def available_models(self) -> list[ModelInfo]:
        """可选的模型。只有 `.env` 里给出的那一个。"""
        config = self.agent.config
        return [
            ModelInfo(
                config.model,
                config.model,
                config.provider,
                config.context_window,
            )
        ]

    @property
    def current_model_label(self) -> str:
        """当前模型的展示名。"""
        return self.agent.config.model

    @property
    def current_model_provider(self) -> str:
        """当前模型的提供方。"""
        return self.agent.config.provider

    @property
    def context_window(self) -> int:
        """上下文窗口的容量。"""
        return self.agent.context_window

    def start_chat_with(self, prompt: str) -> None:
        """带着一句初始输入进入聊天界面，由聊天界面负责发出。"""
        self.switch_screen(ChatScreen(self.root, initial_prompt=prompt))

    def save_state(self) -> None:
        """把会话与显示设定写入磁盘。"""
        self.session_manager.last_model_id = self.agent.config.model
        self.session_manager.theme = self.theme
        self.session_manager.save_to_toml()

    def show_toast(self, text: str, kind: str = "info") -> None:
        """显示一条会自动消失的通知。"""
        toast = ToastNotification(text, kind)
        target = self.screen
        if target.is_mounted:
            target.mount(toast)
        else:
            self.call_after_refresh(lambda: target.mount(toast))
        self.set_timer(self.TOAST_SECONDS, lambda: self._remove_toast(toast))

    def _remove_toast(self, toast: ToastNotification) -> None:
        """移除已经挂载过的通知。"""
        if toast.is_mounted:
            toast.remove()

    def execute_command(self, name: str) -> None:
        """按名字执行斜杠命令。"""
        command = self.command_registry.get(name)
        if command is None:
            self.show_toast(f"未知命令：{name}", "error")
            return
        handler = getattr(self, f"_command_{command.name}", None)
        if handler is None:
            self.show_toast(f"/{command.name} 尚未接入", "warning")
            return
        handler()

    def _command_new(self) -> None:
        """新建会话。"""
        self.session_manager.new_session()
        self._ensure_chat_screen()
        self.save_state()
        self.show_toast("已创建新会话", "success")

    def _command_sessions(self) -> None:
        """列出会话。"""
        sessions = self.session_manager.list_sessions()
        self.push_screen(SessionPickerScreen(sessions), self._on_session_picked)

    def _command_models(self) -> None:
        """列出模型。"""
        self.push_screen(ModelSelectorScreen(self.available_models), self._on_model_picked)

    def _command_themes(self) -> None:
        """列出主题。"""
        themes = sorted(self.available_themes)
        self.push_screen(ThemeSelectorScreen(themes), self._on_theme_picked)

    def _command_help(self) -> None:
        """显示帮助。"""
        self.push_screen(HelpScreen(self.command_registry, self.keybind_manager, self._help_origin()))

    def _help_origin(self) -> str:
        """找出帮助是从哪个界面打开的：屏幕层级里最靠上的启动界面或聊天界面。

        从命令面板选 /help 时，最上面那一层是命令面板自己，所以要看它下面那一层。
        """
        for screen in reversed(self.screen_stack):
            if isinstance(screen, HomeScreen):
                return "home"
            if isinstance(screen, ChatScreen):
                return "chat"
        return "chat"

    def _command_exit(self) -> None:
        """退出应用。"""
        self.save_state()
        self.exit()

    def _command_export(self) -> None:
        """把当前会话导出为 Markdown 文件。"""
        session = self.session_manager.current
        if not session.messages:
            self.show_toast("当前会话还没有消息", "warning")
            return
        target = self.root / export_filename(session)
        target.write_text(render_session_markdown(session), encoding="utf-8")
        self.show_toast(f"已导出到 {target}", "success")

    def _command_editor(self) -> None:
        """用外部编辑器撰写当前输入。"""
        top = self.screen
        if not isinstance(top, ChatScreen):
            self.show_toast("请先进入聊天界面", "warning")
            return
        editor = resolve_editor()
        if editor is None:
            self.show_toast("未找到外部编辑器，请设置 EDITOR 环境变量", "warning")
            return
        buffer = editor_buffer_path()
        write_buffer(buffer, top.editor.text)
        with self.suspend():
            launch(editor, buffer)
        top.editor.text = read_buffer(buffer).rstrip("\n")

    def _command_details(self) -> None:
        """切换工具执行详情。"""
        self.session_manager.show_details = not self.session_manager.show_details
        self._apply_display_toggles()
        state = "显示" if self.session_manager.show_details else "隐藏"
        self.show_toast(f"工具详情：{state}", "info")

    def _command_effort(self) -> None:
        """按 low → medium → high 轮换推理强度。"""
        session = self.session_manager.current
        index = EFFORTS.index(session.effort) if session.effort in EFFORTS else 0
        session.effort = EFFORTS[(index + 1) % len(EFFORTS)]
        self.refresh_chat()
        self.save_state()
        self.show_toast(f"推理强度：{session.effort}", "info")

    def _command_sidebar(self) -> None:
        """切换聊天界面右侧的上下文侧栏。"""
        top = self.screen
        if not isinstance(top, ChatScreen):
            self.show_toast("请先进入聊天界面", "warning")
            return
        visible = top.toggle_sidebar()
        state = "显示" if visible else "隐藏"
        self.show_toast(f"上下文侧栏：{state}", "info")

    def _command_thinking(self) -> None:
        """切换思考块可见性。"""
        self.session_manager.show_thinking = not self.session_manager.show_thinking
        self._apply_display_toggles()
        state = "显示" if self.session_manager.show_thinking else "隐藏"
        self.show_toast(f"思考块：{state}", "info")

    def _command_undo(self) -> None:
        """撤销最后一条消息。"""
        session = self.session_manager.current
        if not session.messages:
            self.show_toast("没有可撤销的消息", "warning")
            return
        self._removed_message = session.messages.pop()
        self._removed_session_id = session.id
        self.refresh_chat()
        self.save_state()
        self.show_toast("已撤销最后一条消息", "success")

    def _command_redo(self) -> None:
        """恢复被撤销的消息。"""
        if self._removed_message is None or self._removed_session_id != self.session_manager.current_session_id:
            self.show_toast("没有可重做的消息", "warning")
            return
        session = self.session_manager.current
        session.messages.append(self._removed_message)
        session.touch()
        self._removed_message = None
        self.refresh_chat()
        self.save_state()
        self.show_toast("已恢复被撤销的消息", "success")

    def _on_turn_done(self, data: dict[str, object]) -> None:
        """一轮生成结束后持久化状态。"""
        self.save_state()

    def _on_session_picked(self, session_id: str | None) -> None:
        """切换会话。"""
        if not session_id:
            return
        self.session_manager.switch_session(session_id)
        self._ensure_chat_screen()
        session = self.session_manager.get_session(session_id)
        self.show_toast(f"已切换到 {session.title if session else session_id}", "success")

    def _on_model_picked(self, model_id: str | None) -> None:
        """切换模型。只有 `.env` 里给出的那一个可选，选中它就是再确认一次。"""
        if not model_id:
            return
        self.session_manager.current.model_id = model_id
        self.refresh_chat()
        self.show_toast(f"模型已切换为 {self.current_model_label}", "success")

    def _on_theme_picked(self, theme: str | None) -> None:
        """切换主题。"""
        if not theme:
            return
        self.theme = theme
        self.session_manager.theme = theme
        self.save_state()
        self.show_toast(f"主题已切换为 {theme}", "success")

    def _ensure_chat_screen(self) -> None:
        """保证当前显示的是聊天界面。"""
        top = self.screen
        if isinstance(top, ChatScreen):
            top.rebuild()
        elif isinstance(top, HomeScreen):
            self.switch_screen(ChatScreen(self.root))
        else:
            self.push_screen(ChatScreen(self.root))

    def refresh_chat(self) -> None:
        """重建聊天界面的消息列表。

        当前屏幕可能是盖在上面的弹窗（例如删掉当前会话之后，列表还开着），
        所以要沿着屏幕层级往下找聊天界面。
        """
        for screen in reversed(self.screen_stack):
            if isinstance(screen, ChatScreen):
                screen.rebuild()
                return

    def _apply_display_toggles(self) -> None:
        """把显示开关应用到消息列表。"""
        top = self.screen
        if isinstance(top, ChatScreen):
            top.messages.set_details(self.session_manager.show_details)
            top.messages.set_thinking(self.session_manager.show_thinking)

    def show_diff(self, diff_text: str | None = None) -> None:
        """打开 Diff 查看器。"""
        self.push_screen(DiffOverlayScreen(diff_text))
