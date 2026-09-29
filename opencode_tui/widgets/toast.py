from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

KIND_ICON = {
    "info": "●",
    "success": "◆",
    "warning": "▲",
    "error": "✕",
}


class ToastNotification(Widget):
    """非阻塞通知浮层，右下角显示并在数秒后自动消失。"""

    DEFAULT_CSS = """
    ToastNotification {
        dock: bottom;
        height: auto;
        width: auto;
        max-width: 60%;
        margin: 0 2 1 0;
        padding: 0 1;
        align: right middle;
        text-align: right;
        background: #1565C0;
        color: #FFFFFF;
    }
    ToastNotification.success { background: #2E7D32; }
    ToastNotification.warning { background: #EF6C00; }
    ToastNotification.error { background: #C62828; }
    """

    def __init__(self, text: str, kind: str = "info") -> None:
        super().__init__(classes=f"toast {kind}")
        self._text = text
        self._kind = kind

    def compose(self) -> ComposeResult:
        icon = KIND_ICON.get(self._kind, "●")
        yield Static(f"{icon} {self._text}", classes="toast-body")

    @property
    def kind(self) -> str:
        """通知类型。"""
        return self._kind
