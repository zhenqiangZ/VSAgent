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
from ..widgets.command_autocomplete import CommandAutocomplete
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import COMMAND_TOKEN, ScrollingTextArea

ASCII_LOGO = (
    r" __     ___      _               _ ____                           _             " "\n"
    r" \ \   / (_)_ __| |_ _   _  __ _| / ___|  ___ _ __ ___  ___ _ __ (_)_ __   __ _ " "\n"
    r"  \ \ / /| | '__| __| | | |/ _` | \___ \ / __| '__/ _ \/ _ \ '_ \| | '_ \ / _` |" "\n"
    r"   \ V / | | |  | |_| |_| | (_| | |___) | (__| | |  __/  __/ | | | | | | | (_| |" "\n"
    r"    \_/  |_|_|   \__|\__,_|\__,_|_|____/ \___|_|  \___|\___|_| |_|_|_| |_|\__, |" "\n"
    r"                                                                          |___/ "
)

PLACEHOLDER = "Ask anything... 'What is this project built with?'"
HELP_TEXT = "tab agents    ctrl+p commands    / 列出命令"


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

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # esc 关掉命令补全之后，这一次输入不再弹窗，直到输入不再是命令前缀。
        self._command_dismissed = False

    # 与聊天界面同一套：只留命令面板、切 Agent、退出这三个键，
    # 其余（新建会话、模型、主题、帮助、会话列表）都打 /命令，或者 ctrl+p 打开面板。
    BINDINGS = [
        Binding("enter", "submit_input", "Send", priority=True),
        Binding("ctrl+p", "command_palette", "Commands", priority=True),
        Binding("tab", "switch_agent", "Agent", priority=True),
        # 退出走自己的动作：动作名 `quit` 挂在 App 那一层，而输入框把按键先接住了，
        # App 那一条接不到（实测按下去没有任何反应）。自己接住再走 /exit 那条命令。
        Binding("ctrl+c", "quit_app", "Quit", priority=True),
        # 这三条只在命令补全弹窗开着时才生效，否则上下键要留给输入框挪光标。
        Binding("up", "popup_up", "Up", priority=True),
        Binding("down", "popup_down", "Down", priority=True),
        Binding("escape", "close_popup", "Close", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="home-frame"):
            with Vertical(id="home-center"):
                yield Static(ASCII_LOGO, id="home-logo")
                with Horizontal(id="home-box"):
                    yield Static("", id="home-accent-bar")
                    with Vertical(id="home-box-main"):
                        with Horizontal(id="home-prompt-row"):
                            yield Static("[ ]", id="home-prompt-mark", markup=False)
                            # 与聊天界面的输入框一样关掉光标行底色：那一层 `$boost`
                            # 随主题取值，会让光标所在行整行变一个颜色。
                            yield HomeInput(
                                placeholder=PLACEHOLDER,
                                id="home-input",
                                highlight_cursor_line=False,
                            )
                        yield Static(self._info_text(), id="home-info")
                yield Static(HELP_TEXT, id="home-help")
            # 斜杠命令补全弹窗：默认 display:none，往上浮、底边停在最底下那行状态栏上沿。
            # 不能挂在 `#home-center` 里：那一块是居中布局，弹窗虽然不占布局流，高度仍然
            # 算进它的内容总高，一弹出就把 logo 与输入框整块往上挪（实测挪了 5 行）。
            # `keep_below` 挡住它长到提示行以上：矮终端下会盖在输入框上面。
            yield CommandAutocomplete(keep_below="#home-help")
            with Horizontal(id="home-footer"):
                # 左端是项目根目录的完整路径，路径里可能出现方括号，关掉标签解析。
                yield Static(str(self.app.root), id="home-footer-left", markup=False)
                yield Static(__version__, id="home-footer-right")

    def on_mount(self) -> None:
        """把焦点交给输入框。"""
        self.input.focus()

    @property
    def input(self) -> HomeInput:
        """返回核心文本框里的输入框。"""
        return self.query_one("#home-input", HomeInput)

    @property
    def command_popup(self) -> CommandAutocomplete:
        """斜杠命令补全弹窗。"""
        return self.query_one("#command-autocomplete", CommandAutocomplete)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """弹窗开着的时候上下键与 esc 才归弹窗，否则留给输入框。"""
        if action in ("popup_up", "popup_down", "close_popup"):
            return self.command_popup.is_open
        return True

    def on_text_area_changed(self) -> None:
        """输入变化时刷新斜杠命令候选。"""
        self._update_command_popup()

    def _update_command_popup(self) -> None:
        """输入 / 时列出命令候选，与聊天界面那一份同一个写法。

        名字打全之后收起弹窗：留着只会挡住下面那行提示，而回车本来就要去执行命令。
        """
        popup = self.command_popup
        token = COMMAND_TOKEN.match(self.input.text.strip())
        if token is None:
            # 不再是个命令前缀（清空、加了空格、或多打了一个 /），esc 的抑制到这里为止。
            self._command_dismissed = False
            if popup.is_open:
                popup.hide()
            return
        if self._command_dismissed:
            return
        query = token.group(1)
        registry = self.app.command_registry
        # 名字或别名打全了就收起：留着弹窗会把回车截走，命令执行不了。
        if registry.is_exact(query):
            if popup.is_open:
                popup.hide()
            return
        popup.set_commands(registry.complete(query))

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
        """提交输入：命令补全开着时先采纳候选，斜杠开头走命令，其余进入聊天界面发送。"""
        popup = self.command_popup
        if popup.is_open:
            self._accept_command()
            return
        text = self.input.text.strip()
        if not text:
            return
        self.input.clear()
        if text.startswith("/"):
            self.app.execute_command(text.lstrip("/").strip())
            return
        self.app.start_chat_with(text)

    def action_quit_app(self) -> None:
        """退出应用，与聊天界面那一个键同一条命令。"""
        self.app.execute_command("exit")

    def action_popup_up(self) -> None:
        """候选项上移。"""
        self.command_popup.move(-1)

    def action_popup_down(self) -> None:
        """候选项下移。"""
        self.command_popup.move(1)

    def action_close_popup(self) -> None:
        """关闭命令补全弹窗，这一次输入不再弹。

        只收起一个的话，接着打下一个字母又会去匹配一次，esc 等于没按。
        """
        self._command_dismissed = True
        self.command_popup.hide()

    def _accept_command(self) -> None:
        """把当前高亮的命令名填进输入框。

        只填名字、不带尾空格：带空格的话下一次输入变化又会去匹配一次，弹窗刚收起来就
        又弹开，回车于是永远采纳候选、永远执行不了命令。
        """
        command = self.command_popup.current_command()
        if command is None:
            self.command_popup.hide()
            return
        self.command_popup.hide()
        self.input.text = f"/{command.name}"

    def action_command_palette(self) -> None:
        """打开命令面板，先把补全弹窗收掉。

        不收的话面板叠在上面、下面那个弹窗还开着，等面板退回来它仍在。
        """
        self.command_popup.hide()
        self._command_dismissed = False
        self.app.push_screen(CommandPalette(self.app.command_registry), self._on_palette_result)

    def action_switch_agent(self) -> None:
        """tab：命令补全开着时采纳候选，否则在可用 Agent 之间循环切换。"""
        if self.command_popup.is_open:
            self._accept_command()
            return
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
