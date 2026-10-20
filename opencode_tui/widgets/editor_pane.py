from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static, TextArea

from .agent_autocomplete import AgentAutocomplete
from .command_autocomplete import CommandAutocomplete
from .file_autocomplete import AutocompletePopup, FileAutocomplete

# 状态行最右端显示当前输入模式。
MODE_LABEL = {
    "chat": "chat",
    "command": "命令",
    "bash": "bash",
}

FILE_TOKEN = re.compile(r"@([^\s@]*)$")
# 斜杠命令只认第一段的那个词：整段输入以 / 开头、且后面没有空格，才把它当命令名去补全。
# 一出现空格就说明命令名已经打完（或者这根本不是命令），弹窗该收起来。
COMMAND_TOKEN = re.compile(r"^/(\S*)$")

IGNORED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".probe",
    ".workbuddy",
    "build",
    "dist",
}
MAX_SCAN_FILES = 20000


def search_files(root: Path, query: str, limit: int = 12) -> list[str]:
    """在工作目录内做模糊文件搜索，返回相对路径列表。

    用 os.walk 并在走进子目录之前把忽略目录剔掉。换成 rglob 的话，忽略是在取到文件之后
    才判断的，rglob 仍然会走遍 build/ 与 .venv 里的每一个条目 —— 那些条目照样占用
    MAX_SCAN_FILES 的名额，名额用完就 break，后面的文件一个都看不见，
    候选列表于是取决于目录遍历顺序。
    """
    if not root.exists() or not root.is_dir():
        return []
    cleaned = query.lower()
    scored: list[tuple[float, str]] = []
    scanned = 0
    exhausted = False
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS)
        for name in sorted(files):
            if scanned >= MAX_SCAN_FILES:
                exhausted = True
                break
            scanned += 1
            path = Path(current) / name
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if not cleaned:
                scored.append((1.0, relative))
                continue
            ratio = difflib.SequenceMatcher(None, cleaned, relative.lower()).ratio()
            if cleaned in relative.lower():
                ratio += 0.6
            if ratio > 0.35:
                scored.append((ratio, relative))
        if exhausted:
            break
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[1] for item in scored[:limit]]


class ScrollingTextArea(TextArea):
    """内容超出可见高度之后，滚动要跟着光标走到底的多行输入框。

    `TextArea._watch_selection` 在光标移动时立即滚一次，用的还是插入之前算出来的
    滚动上限：刚插进去的那一行还没进虚拟高度，`max_scroll_y` 少一，`clamp_delta`
    就把滚动量截掉一行。内容还在可见高度以内时看不出来；一旦超出，光标停在输入区
    外面一行，此后每次插入都差这一行，屏幕上根本看不到光标在哪。等这次刷新推完、
    虚拟高度更新之后再补滚一次就补齐了。
    """

    def on_text_area_selection_changed(self) -> None:
        """光标移动之后，等布局刷新完再补一次滚动。"""
        self.call_after_refresh(self._scroll_cursor_into_view)

    def _scroll_cursor_into_view(self) -> None:
        """补滚一次，并把终端光标挪到补滚之后的位置。

        终端光标的位置由 `App.cursor_position` 决定，父类是在补滚之前设的，
        不跟着改就会停在输入区外面。
        """
        if not self.is_mounted:
            return
        self.scroll_cursor_visible()
        if self.has_focus:
            self.app.cursor_position = self.cursor_screen_offset


class PromptInput(ScrollingTextArea):
    """输入框：焦点进出时不重画整块，滚动跟着光标走。"""

    def _on_focus(self, event: events.Focus) -> None:
        """获得焦点时不重画整块输入区。

        `Widget._on_focus`（`widget.py:4767`）会调一次不带区域的 `self.refresh()`，
        把输入区整块（115 列 × 8 行）标成脏区域。输入区的样子不随焦点变化：
        应用自己的 tcss 里没有一条 `:focus` 规则。
        `prevent_default` 会拦住基类的同名处理函数，基类另外那两件事在这里补上。
        """
        event.prevent_default()
        self.has_focus = True
        if self.parent is not None:
            self.parent.post_message(events.DescendantFocus(self))

    def _on_blur(self, event: events.Blur) -> None:
        """失去焦点时不重画整块输入区，理由同 `_on_focus`。

        焦点从输入框挪到消息区是"点一下聊天界面"这条路上字节数的主要来源：
        实测这一块整块重画占掉那一次点击 3587 字节里的绝大一部分。
        """
        event.prevent_default()
        self.has_focus = False
        if self.parent is not None:
            self.parent.post_message(events.DescendantBlur(self))

    def focus(self, scroll_visible: bool = True) -> PromptInput:
        """把焦点交给输入框，不先重画整块。

        `Widget.focus`（`widget.py:4596`）在排聚焦回调之前调一次不带区域的
        `self.refresh()`，把输入区整块标成脏区域；真正的焦点变化由排出的那个回调
        完成，样式上没有任何东西依赖这一次提前重画。应用里所有"把焦点交给输入框"
        都走这里：`EditorPane.on_mount` 与 `focus_input`。
        """
        self.screen.set_focus(self, scroll_visible=scroll_visible)
        return self


class EditorPane(Container):
    """输入编辑器，支持多行、模式切换与自动补全。"""

    class Submitted(Message):
        """用户提交输入的事件。"""

        def __init__(self, text: str, mode: str) -> None:
            super().__init__()
            self.text = text
            self.mode = mode

    class AgentChanged(Message):
        """切换 Agent 的事件。"""

        def __init__(self, agent: str) -> None:
            super().__init__()
            self.agent = agent

    class InterruptRequested(Message):
        """没有弹窗可关的时候按 esc：这一次按键交给聊天界面去中断生成。"""

    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("shift+enter", "insert_newline", "Newline", priority=True),
        Binding("up", "popup_up", "Up", priority=True),
        Binding("down", "popup_down", "Down", priority=True),
        Binding("tab", "agent_popup", "Agents", priority=True),
        Binding("escape", "close_popup", "Close", priority=True),
        Binding("ctrl+up", "history_prev", "History", priority=True),
        Binding("ctrl+down", "history_next", "History", priority=True),
    ]

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(id="editor-pane", classes="mode-chat")
        self.root = root or Path.cwd()
        self.mode = "chat"
        self.history: list[str] = []
        self._history_index = -1
        self._agent = "Build"
        self._model = ""
        self._effort = "low"
        self._written: dict[str, str] = {}
        # esc 关掉命令补全之后，这一次输入不再弹窗，直到输入不再是命令前缀。
        self._command_dismissed = False

    def compose(self) -> ComposeResult:
        yield FileAutocomplete()
        yield CommandAutocomplete()
        yield AgentAutocomplete()
        with Horizontal(id="editor-body"):
            yield Static("", id="editor-accent")
            with Vertical(id="editor-main"):
                # 关掉光标行底色。TextArea 默认会给光标所在行抹一层 `$boost`，
                # 换主题就换一个样子，在 gruvbox 下与底色混成同色、在 textual-dark 下亮出一档，
                # 看着像那一行变灰。运行中改这个开关不会立刻生效：`render_line` 有一层行缓存，
                # 键里不含这个字段，也没有清缓存的钩子。只能在构造时传。
                yield PromptInput(id="editor-input", highlight_cursor_line=False)
                with Horizontal(id="editor-status"):
                    # 这四个标签画的是 Agent 名称、模型名称与提示语，取值来自 `.env`，
                    # 关掉标签解析，模型名里带方括号时不会被当成标签吞掉。
                    yield Static(self._agent, id="editor-agent", markup=False)
                    yield Static(self._model, id="editor-model", markup=False)
                    yield Static(self._effort, id="editor-effort", markup=False)
                    yield Static(self._hint_text(), id="editor-hint", markup=False)

    def on_mount(self) -> None:
        """挂载后把焦点交给输入框。"""
        self.input.focus()

    def on_descendant_focus(self) -> None:
        """焦点进到这一块时点亮最左侧那条指示线。

        只给 `#editor-accent` 一个类，不碰 `#editor-pane` 自己的类：
        改区块的类会让整块（连同输入区与状态行）重算样式再重绘，
        焦点每进出一次就闪一下。
        """
        self.accent.add_class("-focused")

    def on_descendant_blur(self) -> None:
        """焦点离开这一块时把指示线收回暗色。"""
        self.accent.remove_class("-focused")

    def update_context(self, *, agent: str, model: str, effort: str) -> None:
        """刷新输入框下方状态行的 Agent、模型与推理强度。"""
        self._agent = agent
        self._model = model
        self._effort = effort
        self.refresh_status_line()

    def refresh_status_line(self) -> None:
        """按当前取值重画状态行。"""
        if not self.is_mounted:
            return
        self._write("#editor-agent", self._agent)
        self._write("#editor-model", self._model)
        self._write("#editor-effort", self._effort)
        self._write("#editor-hint", self._hint_text())

    def _write(self, selector: str, text: str) -> None:
        """内容没变就不写。

        `Static.update()` 每次都带走一遍整屏重新布局，而输入每变一个字符都会走到这里，
        四个标签里有三个的内容几乎从不变化。
        """
        if self._written.get(selector) == text:
            return
        self._written[selector] = text
        self.query_one(selector, Static).update(text)

    @property
    def accent(self) -> Static:
        """最左侧那条 1 列宽的指示线。"""
        return self.query_one("#editor-accent", Static)

    @property
    def input(self) -> PromptInput:
        """返回内部的多行输入框。"""
        return self.query_one("#editor-input", PromptInput)

    @property
    def text(self) -> str:
        """当前输入的文本。"""
        return self.input.text

    @text.setter
    def text(self, value: str) -> None:
        self.input.text = value

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """仅在补全弹窗打开时启用上下选择；esc 一直有效。

        esc 没有弹窗可关时要交给聊天界面去中断生成，所以不能像上下键那样
        在弹窗关着时被禁掉——禁掉之后这个键连动作都进不去。
        """
        if action in ("popup_up", "popup_down"):
            return self._active_popup() is not None
        return True

    def on_text_area_changed(self) -> None:
        """输入变化时更新模式与补全候选。"""
        self._update_mode()
        self._update_file_popup()
        self._update_command_popup()

    def action_submit(self) -> None:
        """提交输入，弹窗打开时改为接受候选项。"""
        popup = self._active_popup()
        if popup is not None:
            self._accept(popup)
            return
        text = self.input.text.strip()
        if not text:
            return
        self.history.append(text)
        self._history_index = len(self.history)
        mode = self.mode
        self.input.clear()
        self._update_mode()
        self.post_message(self.Submitted(text, mode))

    def action_insert_newline(self) -> None:
        """插入换行而不是提交。"""
        self.input.insert("\n")

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

    def action_agent_popup(self) -> None:
        """tab：命令补全开着时采纳当前候选，否则打开 Agent 补全弹窗。"""
        if self.command_popup.is_open:
            self._accept(self.command_popup)
            return
        self.file_popup.hide()
        self.agent_popup.open_list()

    def action_close_popup(self) -> None:
        """关闭当前补全弹窗，没有弹窗时把这一下当成中断。

        关掉命令补全之后，这一次输入就不再弹了：只收起一个的话，接着打下一个字母
        又会去匹配一次，esc 等于没按。
        中断之所以不直接绑在聊天界面上：输入框自己拿着 esc 的优先级绑定，
        屏幕上那一条永远轮不到；这里发消息才是唯一可靠的通道。
        """
        popup = self._active_popup()
        if popup is None:
            self.post_message(self.InterruptRequested())
            return
        if popup is self.command_popup:
            self._command_dismissed = True
        popup.hide()

    def action_history_prev(self) -> None:
        """翻阅上一条历史输入。"""
        if not self.history:
            return
        self._history_index = max(0, self._history_index - 1)
        self.input.text = self.history[self._history_index]

    def action_history_next(self) -> None:
        """翻阅下一条历史输入。"""
        if not self.history:
            return
        self._history_index = min(len(self.history) - 1, self._history_index + 1)
        self.input.text = self.history[self._history_index]

    def set_agent(self, agent: str) -> None:
        """切换当前 Agent 并通知外层。"""
        self.post_message(self.AgentChanged(agent))

    def focus_input(self) -> None:
        """把焦点移到输入框。"""
        self.input.focus()

    @property
    def file_popup(self) -> FileAutocomplete:
        """文件补全弹窗。"""
        return self.query_one("#file-autocomplete", FileAutocomplete)

    @property
    def agent_popup(self) -> AgentAutocomplete:
        """Agent 补全弹窗。"""
        return self.query_one("#agent-autocomplete", AgentAutocomplete)

    @property
    def command_popup(self) -> CommandAutocomplete:
        """斜杠命令补全弹窗。"""
        return self.query_one("#command-autocomplete", CommandAutocomplete)

    def _active_popup(self) -> AutocompletePopup | None:
        """返回当前打开的弹窗。"""
        for popup in (self.file_popup, self.command_popup, self.agent_popup):
            if popup.is_open:
                return popup
        return None

    def _accept(self, popup: AutocompletePopup) -> None:
        """接受当前候选项。"""
        choice = popup.current()
        if choice is None:
            popup.hide()
            return
        if popup is self.agent_popup:
            popup.hide()
            self.set_agent(choice)
            return
        if popup is self.command_popup:
            command = self.command_popup.current_command()
            if command is None:
                popup.hide()
                return
            # 只填命令名，末尾不带空格：带空格会让下一次输入变化又去匹配一次，
            # 弹窗刚收起来就又弹开。名字打全之后弹窗本来也要收（见 `_update_command_popup`）。
            popup.hide()
            self.input.text = f"/{command.name}"
            self._update_mode()
            return
        token = FILE_TOKEN.search(self.input.text)
        if token is None:
            popup.hide()
            return
        head = self.input.text[: token.start()]
        self.input.text = f"{head}@{choice} "
        popup.hide()
        self._update_mode()

    def _update_mode(self) -> None:
        """按输入前缀切换 chat / command / bash 模式。

        模式没变就不动类。原来的写法每敲一个字符都把三个类删一遍再加回一个，
        即使模式一个字都没变，也会让这一块连同子组件重算样式。
        """
        text = self.input.text.strip()
        if text.startswith("/"):
            mode = "command"
        elif text.startswith("!"):
            mode = "bash"
        else:
            mode = "chat"
        if mode != self.mode:
            self.remove_class(f"mode-{self.mode}")
            self.mode = mode
            self.add_class(f"mode-{mode}")
        self.refresh_status_line()

    def _update_file_popup(self) -> None:
        """输入 @ 时刷新文件补全候选。"""
        token = FILE_TOKEN.search(self.input.text)
        if token is None:
            if self.file_popup.is_open:
                self.file_popup.hide()
            return
        self.file_popup.set_items(search_files(self.root, token.group(1)))

    def _update_command_popup(self) -> None:
        """输入 / 时按已经打出来的那一段列出命令候选。

        打一个 / 就把全部命令列出来，这是命令唯一的一份清单；接着打字按名字、
        别名与说明筛。名字打全之后收起：留着只会挡住下面那行输入。
        """
        token = COMMAND_TOKEN.match(self.input.text.strip())
        if token is None:
            # 不再是个命令前缀（清空、加了空格、或多打了一个 /），esc 的抑制到这里为止。
            self._command_dismissed = False
            if self.command_popup.is_open:
                self.command_popup.hide()
            return
        if self._command_dismissed:
            return
        query = token.group(1)
        registry = self.app.command_registry
        # 名字或别名打全了就收起：留着弹窗会把回车截走，命令执行不了。
        if registry.is_exact(query):
            if self.command_popup.is_open:
                self.command_popup.hide()
            return
        self.command_popup.set_commands(registry.complete(query))

    def _hint_text(self) -> str:
        return MODE_LABEL[self.mode]
