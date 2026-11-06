from __future__ import annotations

import time

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message as TextualMessage
from textual.reactive import Reactive
from textual.screen import Screen
from textual.selection import SelectState
from textual.widget import Widget
from textual.widgets import Static

from .. import __version__
from ..core.models import Session
from ..core.session_manager import HOME_MAX, HOME_MIN
from ..widgets.agent_autocomplete import AGENTS
from ..widgets.command_autocomplete import CommandAutocomplete
from ..widgets.command_palette import CommandPalette
from ..widgets.editor_pane import COMMAND_TOKEN, FILE_TOKEN, ScrollingTextArea, search_files
from ..widgets.file_autocomplete import AutocompletePopup, FileAutocomplete
from ..widgets.session_list import NewTaskRow, SessionList, SessionRow, fit
from ..widgets.splitter import Splitter
from ..widgets.top_bar import TopBar, TopLink

# 中间那一栏至少留这么多列：字标一行 60 格，再窄就放不下了。
MIN_CENTER_WIDTH = 40

# 字标逐行写、最后补齐到同一宽度再拼起来。**不能省掉补齐**：`#home-logo` 是
# `text-align: center`，每行各自居中，长短不一的几行会互相错开一格，整块看着是歪的。
_LOGO_ROWS = (
    r" __     __   ____       _       ____   _____   _   _   _____",
    r" \ \   / /  / ___|     / \     / ___| | ____| | \ | | |_   _|",
    r"  \ \ / /   \___ \    / _ \   | |  _  |  _|   |  \| |   | |",
    r"   \ V /     ___) |  / ___ \  | |_| | | |___  | |\  |   | |",
    r"    \_/     |____/  /_/   \_\  \____| |_____| |_| \_|   |_|",
)
_LOGO_WIDTH = max(len(row) for row in _LOGO_ROWS)
ASCII_LOGO = "\n".join(row.ljust(_LOGO_WIDTH) for row in _LOGO_ROWS)

TAGLINE = "虚拟筛选 Agent　·　查靶点 · 取结构 · 定口袋 · 对接 · 亲和力"
PLACEHOLDER = "说一件事，例如「EGFR 的 ATP 口袋在哪」"
HELP_TEXT = "tab 切换 Agent    ctrl+p 命令面板    / 列出命令"

# 输入框下方的快捷入口。它们只是任务模板：点一下把这段话填进输入框，交不交、
# 怎么改由用户自己决定。模板里的靶点写成具体例子，用户改一个词就能用。
QUICK_TEMPLATES: dict[str, tuple[str, str]] = {
    "research": (
        "研究",
        "调研 EGFR 的已知抑制剂与耐药突变，列出关键文献与结论。",
    ),
    "structure": (
        "结构",
        "分析 PDB 4HJO 的结构：共晶配体、口袋位置与分辨率。",
    ),
    "knowledge": (
        "知识",
        "在本机语料库里查 EGFR 抑制剂的选择性数据。",
    ),
}


def _clock(seconds: float) -> str:
    """列表里的时间：当天的只写时分，更早的带上月日。"""
    if not seconds:
        return ""
    if time.time() - seconds < 86400:
        return time.strftime("%H:%M", time.localtime(seconds))
    return time.strftime("%m-%d", time.localtime(seconds))


def _artifact_count(session: Session) -> int:
    """这条任务产出过多少个产物：各主题的 handle 去重之后数一遍。"""
    names: set[str] = set()
    for event in session.events:
        names.update(event.artifacts)
    return len(names)


def _latest_result(session: Session) -> str:
    """最近一次执行结果：最后一个封存主题的结论，没有就取失败原因或状态说明。

    只看最后一条事件：更早的结论不是「最近一次」的结果，拿它顶上来会读成这一轮
    已经做完了。
    """
    if session.events:
        event = session.events[-1]
        if event.summary.strip():
            return event.summary
        if event.issues:
            return event.issues[0]
    return session.status_note.strip()


class HomeInput(ScrollingTextArea):
    """启动界面输入框：内容换行时框体随之增高，超出高度上界后滚动掩盖最早的内容。"""

    BINDINGS = [Binding("shift+enter", "insert_newline", "Newline", show=False)]

    def action_insert_newline(self) -> None:
        """插入换行而不提交。"""
        self.insert("\n")


class HomeAction(Static):
    """输入框底部的一格操作提示。

    只是一行字，点一下才做事：附件插入 `@`，模型与工具打开各自的面板，发送等于回车。
    做成组件而不是给整行挂点击，是为了每一格有自己的动作，也能各自响应悬停。
    """

    class Pressed(TextualMessage):
        """用户点了某一格。"""

        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, action: str, label: str, id: str | None = None) -> None:
        super().__init__(label, id=id, classes="home-action", markup=False)
        self.action_name = action

    def on_click(self) -> None:
        self.post_message(self.Pressed(self.action_name))


class QuickTask(Static):
    """输入框下方的一个任务模板。点一下把模板填进输入框，不提交。"""

    class Picked(TextualMessage):
        """用户点了这个模板。"""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(self, key: str, label: str, id: str | None = None) -> None:
        super().__init__(label, id=id, classes="quick-task", markup=False)
        self.key = key

    def on_click(self) -> None:
        self.post_message(self.Picked(self.key))


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
        # 当前画出来的左栏宽度，拖动时按这一份加减；样式里那份可能被窄屏夹过。
        self._rail_width = 34

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
            yield TopBar()
            with Horizontal(id="home-body"):
                # 左栏是导航：运行中的任务单独一段，其余按更新时间排。点一行就切过去。
                yield SessionList(id="home-side")
                yield Splitter(id="home-split")
                with Vertical(id="home-center"):
                    yield Static(ASCII_LOGO, id="home-logo")
                    yield Static(TAGLINE, id="home-tagline", markup=False)
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
                            # 输入框底部的操作提示：附件、模型、工具与发送。
                            # 前三个只把入口摆出来，点一下才动作；发送那一格是回车的替身。
                            with Horizontal(id="home-actions"):
                                yield HomeAction("file", "@ 文件", id="home-action-file")
                                yield HomeAction("model", "/model 模型", id="home-action-model")
                                yield HomeAction("tools", "/tools 工具", id="home-action-tools")
                                yield HomeAction("send", "Enter 发送", id="home-send-hint")
                    # 快捷入口：填的是任务模板，不直接提交。矮终端里整行收起，
                    # 见 `apply_quick_visibility()`。
                    with Horizontal(id="home-quick"):
                        yield Static("快速开始", classes="quick-label", markup=False)
                        for key, (label, _text) in QUICK_TEMPLATES.items():
                            yield QuickTask(key, label, id=f"quick-{key}")
                    # 输入框下面是最近几条任务：左栏回答「有哪些」，这里回答「最近做了什么」。
                    yield Vertical(id="home-recent")
                    yield Static(HELP_TEXT, id="home-help")
            # 两个补全弹窗：斜杠命令与 @ 文件引用。默认 display:none，往上浮、
            # 底边停在最底下那行状态栏上沿。不能挂在 `#home-center` 里：那一块是
            # 居中布局，弹窗虽然不占布局流，高度仍然算进它的内容总高，一弹出就把
            # logo 与输入框整块往上挪（实测挪了 5 行）。
            # `keep_below` 挡住它长到提示行以上：矮终端下会盖在输入框上面。
            yield FileAutocomplete(keep_below="#home-help")
            yield CommandAutocomplete(keep_below="#home-help")
            with Horizontal(id="home-footer"):
                # 左端是项目根目录的完整路径，路径里可能出现方括号，关掉标签解析。
                yield Static(str(self.app.root), id="home-footer-left", markup=False)
                yield Static(__version__, id="home-footer-right")

    def on_mount(self) -> None:
        """把焦点交给输入框，并把左栏与最近任务画出来。"""
        self.apply_widths()
        self.apply_quick_visibility()
        self.input.focus()
        self.refresh_tasks()

    def on_resize(self, event: events.Resize) -> None:
        """窄终端里收起左栏。

        字标一行 60 格，加上导航之后 80 列的终端放不下，字标会折成两行。
        任务列表是导航，输入框与最近任务才是这一屏要回答的问题，先让导航退场。
        """
        if not self.is_mounted:
            return
        self.apply_widths()
        self.apply_quick_visibility()
        # 最近任务那几行是按当时的宽度截断的，宽度变了要重截一遍；
        # 留着旧的那份会在窄屏里溢出、宽屏里过早省略。
        self.refresh_tasks()

    def apply_quick_visibility(self) -> None:
        """矮终端里收起快捷入口。

        这一行加上它上面的间距占 2 行；24 行终端里除输入区以外已经占掉 20 行，
        多这两行会把字标顶出屏幕。
        """
        self.query_one("#home-quick").styles.display = (
            "block" if self.app.size.height >= 26 else "none"
        )

    def apply_widths(self) -> None:
        """按终端宽度摆好左栏与分栏界线。

        存的是用户拖出来的宽度，画的时候再按当前终端夹一次：临时缩小不该把设定改掉。
        """
        manager = self.app.session_manager
        total = self.app.size.width
        visible = total >= 92
        self.query_one("#home-side").styles.display = "block" if visible else "none"
        self.query_one("#home-split").styles.display = "block" if visible else "none"
        room = max(HOME_MIN, total - 1 - MIN_CENTER_WIDTH)
        width = max(HOME_MIN, min(manager.home_rail_width, HOME_MAX, room))
        self._rail_width = width
        self.query_one("#home-side").styles.width = width

    def on_splitter_resize_requested(self, event: Splitter.ResizeRequested) -> None:
        """拖动界线或按方向键：改左栏宽度。"""
        if event.splitter.id != "home-split":
            return
        manager = self.app.session_manager
        manager.home_rail_width = max(
            HOME_MIN, min(HOME_MAX, self._rail_width + event.delta)
        )
        self.apply_widths()
        self.refresh_tasks()

    def on_splitter_resize_finished(self, event: Splitter.ResizeFinished) -> None:
        """松手之后才写盘：拖动过程中每移一格都写一次状态文件太重。"""
        self.app.save_state()

    def refresh_tasks(self) -> None:
        """重画左栏与「最近任务」。会话列表随切换与新建变化，进屏幕时重取一次。"""
        self.query_one("#home-side", SessionList).refresh_sessions(
            new_task=True, list_title="最近任务"
        )
        box = self.query_one("#home-recent", Vertical)
        box.remove_children()
        sessions = self.app.session_manager.root_sessions()[:4]
        if not sessions:
            return
        box.mount(Static("最近任务", classes="rail-section -first"))
        current = self.app.session_manager.current_session_id
        width = max(20, box.content_size.width or 78)
        for item in sessions:
            box.mount(
                SessionRow(
                    item,
                    current=item.id == current,
                    width=width,
                    meta=self._recent_meta(item, width),
                )
            )

    @staticmethod
    def _recent_meta(session: Session, width: int) -> str:
        """最近任务那一行的附加信息：主题数、产物数、时间与最近结果。

        结果按剩余宽度截断。`SessionRow` 的两列内边距、状态词 6 格与它前后各
        两格的空隙，加上 meta 自己的分隔，一共占掉 13 格；标题留 12 格、
        meta 前面的分隔留 2 格，所以结果那一段最多 `width - 27 - counts` 格，
        不够 6 格就不写结果。
        """
        counts = (
            f"{len(session.events)} 主题 · {_artifact_count(session)} 产物"
            f" · {_clock(session.updated_at)}"
        )
        result = _latest_result(session)
        room = width - 27 - cell_len(counts)
        if not result or room < 6:
            return counts
        return f"{counts} · {fit(result, room)}"

    def on_session_row_picked(self, message: SessionRow.Picked) -> None:
        """点左栏或最近任务里的一行：切到那个会话并进入聊天界面。"""
        message.stop()
        self.app.open_session(message.session_id)

    def on_new_task_row_pressed(self) -> None:
        """左栏顶部的「新建任务」：清空输入框、把焦点放回去。

        会话不在这里建：点了又什么都不输入的话，列表里会多一个空壳。真正建会话
        在提交时由 `start_chat_with()` 决定（当前会话有内容才另起一个）。
        """
        self.input.text = ""
        self.input.focus()

    def on_top_link_picked(self, message: TopLink.Picked) -> None:
        """顶栏右侧的入口：走对应的斜杠命令，面板与命令那一头共用一份。"""
        message.stop()
        self.app.execute_command(message.command)

    def on_home_action_pressed(self, message: HomeAction.Pressed) -> None:
        """输入框底部那几格：附件插入引用前缀，模型与工具开面板，发送等于回车。"""
        message.stop()
        if message.action == "file":
            # 提交时由 `resolve_file_refs` 把 @路径 读成文件引用；这里只负责起个头。
            self.input.insert("@")
            self.input.focus()
            return
        if message.action == "send":
            self.action_submit_input()
            return
        # 模型与工具各自有面板，直接走命令；输入框里的草稿留着不动。
        self.app.execute_command(message.action)

    def on_quick_task_picked(self, message: QuickTask.Picked) -> None:
        """点快捷入口：把模板填进输入框，光标放到末尾，任务不提交。"""
        message.stop()
        self.input.text = QUICK_TEMPLATES[message.key][1]
        self.input.move_cursor(self.input.document.end)
        self.input.focus()

    @property
    def input(self) -> HomeInput:
        """返回核心文本框里的输入框。"""
        return self.query_one("#home-input", HomeInput)

    @property
    def command_popup(self) -> CommandAutocomplete:
        """斜杠命令补全弹窗。"""
        return self.query_one("#command-autocomplete", CommandAutocomplete)

    @property
    def file_popup(self) -> FileAutocomplete:
        """`@` 文件引用补全弹窗。"""
        return self.query_one("#file-autocomplete", FileAutocomplete)

    def _active_popup(self) -> AutocompletePopup | None:
        """当前打开的那个补全弹窗；两个都关着时返回 None。

        文件引用排在命令前面：同时开着的一瞬（输入里既有 / 又有 @）采纳文件，
        因为 `@` 只在光标附近有效，而命令要求整段输入以 / 开头。
        """
        for popup in (self.file_popup, self.command_popup):
            if popup.is_open:
                return popup
        return None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """弹窗开着的时候上下键与 esc 才归弹窗，否则留给输入框。"""
        if action in ("popup_up", "popup_down", "close_popup"):
            return self._active_popup() is not None
        return True

    def on_text_area_changed(self) -> None:
        """输入变化时刷新斜杠命令与文件引用候选。"""
        self._update_file_popup()
        self._update_command_popup()

    def _update_file_popup(self) -> None:
        """输入 @ 时按已经打出来的那一段列文件候选，与聊天界面同一个写法。"""
        token = FILE_TOKEN.search(self.input.text)
        if token is None:
            if self.file_popup.is_open:
                self.file_popup.hide()
            return
        self.file_popup.set_items(search_files(self.app.root, token.group(1)))

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
        """弹窗关闭后按最新的模型与 Agent 重画状态行，并把任务列表重取一次。"""
        self._refresh_info()
        self.refresh_tasks()

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
        """提交输入：补全开着时先采纳候选，斜杠开头走命令，其余进入聊天界面发送。"""
        popup = self._active_popup()
        if popup is not None:
            self._accept(popup)
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
        popup = self._active_popup()
        if popup is not None:
            popup.move(-1)

    def action_popup_down(self) -> None:
        """候选项下移。"""
        popup = self._active_popup()
        if popup is not None:
            popup.move(1)

    def action_close_popup(self) -> None:
        """关闭当前补全弹窗。

        命令补全关掉之后这一次输入不再弹：只收起一个的话，接着打下一个字母又会
        去匹配一次，esc 等于没按。文件补全跟着输入内容走，下一个字符仍然匹配就再弹。
        """
        popup = self._active_popup()
        if popup is None:
            return
        if popup is self.command_popup:
            self._command_dismissed = True
        popup.hide()

    def _accept(self, popup: AutocompletePopup) -> None:
        """采纳当前弹窗里高亮的那一条。"""
        if popup is self.file_popup:
            self._accept_file()
        else:
            self._accept_command()

    def _accept_file(self) -> None:
        """把高亮的文件路径填进输入框，替换掉正在输入的那一段引用。"""
        choice = self.file_popup.current()
        if choice is None:
            self.file_popup.hide()
            return
        token = FILE_TOKEN.search(self.input.text)
        if token is None:
            self.file_popup.hide()
            return
        head = self.input.text[: token.start()]
        self.input.text = f"{head}@{choice} "
        self.input.move_cursor(self.input.document.end)
        self.file_popup.hide()

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
        """拼出状态行：Agent 用蓝色，模型与提供方用次要文字那一档。"""
        app = self.app
        text = Text()
        text.append(app.session_manager.last_agent, style="#4A9EFF")
        text.append(" · ", style="#4A4A4A")
        text.append(f"{app.current_model_label} {app.current_model_provider}", style="#8B949E")
        return text

    def _refresh_info(self) -> None:
        """重画状态行。"""
        self.query_one("#home-info", Static).update(self._info_text())

    def _on_palette_result(self, command_name: str | None) -> None:
        """执行命令面板选中的命令。"""
        if command_name:
            self.app.execute_command(command_name)
