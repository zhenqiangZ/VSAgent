from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from .click_dismiss import BODY_CLASS, ClickDismissModal


class MessageScreen(ClickDismissModal[None]):
    """一段只读文本的弹窗。

    与帮助界面、Diff 弹窗同一类：只显示，不收输入，esc 或 enter 关掉。
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "dismiss", "Close", priority=True),
        Binding("q", "dismiss", "Close", priority=True),
    ]

    def __init__(self, title: str, lines: list[str], ident: str = "message") -> None:
        super().__init__()
        self._title = title
        self._lines = lines
        self._ident = ident

    def compose(self) -> ComposeResult:
        with Vertical(id=f"{self._ident}-dialog", classes=BODY_CLASS):
            yield Static(self._title, id=f"{self._ident}-title", markup=False)
            with VerticalScroll(id=f"{self._ident}-body"):
                for line in self._lines:
                    yield Static(line, classes="message-line", markup=False)
            yield Static("点击空白处关闭（esc）", id=f"{self._ident}-hint")

    def action_dismiss(self) -> None:
        """关闭弹窗。"""
        self.dismiss(None)
