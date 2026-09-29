from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

SAMPLE_DIFF = """\
--- a/opencode_tui/app.py
+++ b/opencode_tui/app.py
@@ -1,6 +1,8 @@
 from textual.app import App

+from .core.event_bus import EventBus
+from .core.session_manager import SessionManager

 class OpenCodeApp(App):
-    CSS_PATH = "styles/app.tcss"
+    CSS_PATH = "styles/app.tcss"
+    TITLE = "opencode tui"
"""


class DiffOverlayScreen(ModalScreen[None]):
    """全屏 Diff 查看器，支持滚动与简易语法着色。"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("q", "dismiss", "Quit", priority=True),
        Binding("up", "scroll_up", "Up", priority=True),
        Binding("down", "scroll_down", "Down", priority=True),
    ]

    def __init__(self, diff_text: str | None = None) -> None:
        super().__init__()
        self._diff_text = diff_text or SAMPLE_DIFF

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="diff-overlay"):
            yield Static("Diff", id="diff-title")
            yield Static(self._colorize(), id="diff-body")
            yield Static("up/down 滚动   q 或 esc 关闭", id="diff-hint")

    def on_mount(self) -> None:
        """聚焦滚动区域以便直接响应上下键。"""
        self.query_one("#diff-overlay", VerticalScroll).focus()

    def action_scroll_up(self) -> None:
        """向上滚动。"""
        self.query_one("#diff-overlay", VerticalScroll).scroll_relative(y=-1, animate=False)

    def action_scroll_down(self) -> None:
        """向下滚动。"""
        self.query_one("#diff-overlay", VerticalScroll).scroll_relative(y=1, animate=False)

    def action_dismiss(self) -> None:
        """关闭 Diff 查看器。"""
        self.dismiss(None)

    def _colorize(self) -> str:
        """按行着色的 diff 文本。"""
        lines = []
        for line in self._diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(f"[green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                lines.append(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                lines.append(f"[cyan]{line}[/cyan]")
            else:
                lines.append(line)
        return "\n".join(lines)
