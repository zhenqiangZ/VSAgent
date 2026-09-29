from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.selection import SelectState
from textual.widget import Widget
from textual.widgets import Static

from .. import __version__
from ..widgets.agent_autocomplete import AGENTS
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import ScrollingTextArea

ASCII_LOGO = (
    r" __     ___      _               _ ____                           _             " "\n"
    r" \ \   / (_)_ __| |_ _   _  __ _| / ___|  ___ _ __ ___  ___ _ __ (_)_ __   __ _ " "\n"
    r"  \ \ / /| | '__| __| | | |/ _` | \___ \ / __| '__/ _ \/ _ \ '_ \| | '_ \ / _` |" "\n"
    r"   \ V / | | |  | |_| |_| | (_| | |___) | (__| | |  __/  __/ | | | | | | | (_| |" "\n"
    r"    \_/  |_|_|   \__|\__,_|\__,_|_|____/ \___|_|  \___|\___|_| |_|_|_| |_|\__, |" "\n"
    r"                                                                          |___/ "
)

PLACEHOLDER = "Ask anything... 'What is this project built with?'"
HELP_TEXT = "tab agents    ctrl+p commands"


class HomeInput(ScrollingTextArea):
    """启动界面输入框：内容换行时框体随之增高，超出高度上界后滚动掩盖最早的内容。"""

    BINDINGS = [Binding("shift+enter", "insert_newline", "Newline", show=False)]

    def action_insert_newline(self) -> None:
        """插入换行而不提交。"""
        self.insert("\n")


class HomeScreen(Screen):
    """启动界面：标题、输入框、状态模型行、快捷键提示与底部状态栏。"""

    # 与 `ChatScreen` 同一条根因：`Screen.focused`（`screen.py:229`）与
    # `Screen._select_state`（`screen.py:260`）都带默认重画标记，鼠标左键按下时
    # `Screen.set_focus`（`screen.py:1133`）与 `Screen._forward_event`（`screen.py:1910`）
    # 各赋值一次，每次都调不带区域的 `Screen.refresh()`，把整块屏幕标成脏区域。
    # 实测（120 列 34 行）点一下启动界面送出两帧整屏（各 4080 个格子）加两帧局部，
    # 而画面上真正变的只有状态行那一处。详细的因果链记在 `ISSUES.md` 第二十六节。
    focused: Reactive[Widget | None] = Reactive(None, repaint=False)
    _select_state: Reactive[SelectState | None] = Reactive(None, repaint=False)

    BINDINGS = [
        Binding("enter", "submit_input", "Send", priority=True),
        Binding("ctrl+p", "command_palette", "Commands", priority=True),
        Binding("tab", "switch_agent", "Agent", priority=True),
        ("ctrl+n", "new_session", "New session"),
        ("ctrl+l", "list_sessions", "Sessions"),
        ("ctrl+m", "select_model", "Models"),
        ("ctrl+t", "select_theme", "Themes"),
        ("ctrl+h", "show_help", "Help"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="home-frame"):
            with Vertical(id="home-center"):
                yield Static(ASCII_LOGO, id="home-logo")
                with Horizontal(id="home-box"):
                    yield Static("", id="home-accent-bar")
                    with Vertical(id="home-box-main"):
                        with Horizontal(id="home-prompt-row"):
                            yield Static("[ ]", id="home-prompt-mark")
                            # 与聊天界面的输入框一样关掉光标行底色：那一层 `$boost`
                            # 随主题取值，会让光标所在行整行变一个颜色。
                            yield HomeInput(
                                placeholder=PLACEHOLDER,
                                id="home-input",
                                highlight_cursor_line=False,
                            )
                        yield Static(self._info_text(), id="home-info")
                yield Static(HELP_TEXT, id="home-help")
            with Horizontal(id="home-footer"):
                yield Static(str(self.app.root), id="home-footer-left")
                yield Static(__version__, id="home-footer-right")

    def on_mount(self) -> None:
        """把焦点交给输入框。"""
        self.input.focus()

    @property
    def input(self) -> HomeInput:
        """返回核心文本框里的输入框。"""
        return self.query_one("#home-input", HomeInput)

    def on_screen_resume(self) -> None:
        """弹窗关闭后按最新的模型与 Agent 重画状态行。"""
        self._refresh_info()

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

    def action_submit_input(self) -> None:
        """提交输入：斜杠开头走命令，其余进入聊天界面发送。"""
        text = self.input.text.strip()
        if not text:
            return
        self.input.clear()
        if text.startswith("/"):
            self.app.execute_command(text.lstrip("/").strip())
            return
        self.app.start_chat_with(text)

    def action_new_session(self) -> None:
        """开启新会话并进入聊天界面。"""
        self.app.execute_command("new")

    def action_list_sessions(self) -> None:
        """打开会话列表。"""
        self.app.execute_command("sessions")

    def action_select_model(self) -> None:
        """打开模型选择器。"""
        self.app.execute_command("models")

    def action_select_theme(self) -> None:
        """打开主题选择器。"""
        self.app.execute_command("themes")

    def action_show_help(self) -> None:
        """打开帮助界面。"""
        self.app.execute_command("help")

    def action_command_palette(self) -> None:
        """打开命令面板。"""
        self.app.push_screen(CommandPalette(self.app.command_registry), self._on_palette_result)

    def action_switch_agent(self) -> None:
        """在可用 Agent 之间循环切换。"""
        agent = self._next_agent()
        self.app.session_manager.last_agent = agent
        self.app.session_manager.current.agent_name = agent
        self._refresh_info()
        self.app.show_toast(f"已切换 Agent：{agent}", "success")

    def _next_agent(self) -> str:
        """返回列表里的下一个 Agent。"""
        current = self.app.session_manager.last_agent
        index = AGENTS.index(current) if current in AGENTS else -1
        return AGENTS[(index + 1) % len(AGENTS)]

    def _info_text(self) -> Text:
        """拼出状态行：Agent 用蓝色，模型与提供方用浅灰。"""
        app = self.app
        text = Text()
        text.append(app.session_manager.last_agent, style="#4A9EFF")
        text.append(" · ", style="#666666")
        text.append(f"{app.current_model_label} {app.current_model_provider}", style="#AAAAAA")
        return text

    def _refresh_info(self) -> None:
        """重画状态行。"""
        self.query_one("#home-info", Static).update(self._info_text())

    def _on_palette_result(self, command_name: str | None) -> None:
        """执行命令面板选中的命令。"""
        if command_name:
            self.app.execute_command(command_name)
