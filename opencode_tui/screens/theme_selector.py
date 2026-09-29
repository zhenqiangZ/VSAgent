from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static


class ThemeSelectorScreen(ModalScreen[str | None]):
    """主题选择器，列出 Textual 可用主题。"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "select_theme", "Apply", priority=True),
    ]

    def __init__(self, themes: list[str] | None = None) -> None:
        super().__init__()
        self.themes = list(themes or [])

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-selector"):
            yield Static("Themes", id="theme-title")
            yield ListView(id="theme-list")
            yield Static("enter 应用   esc 关闭", id="theme-hint")

    def on_mount(self) -> None:
        """填充主题列表并聚焦。"""
        view = self.query_one("#theme-list", ListView)
        for name in self.themes:
            view.append(ListItem(Label(name)))
        if self.themes:
            view.index = 0
        view.focus()

    def action_select_theme(self) -> None:
        """应用当前主题。"""
        view = self.query_one("#theme-list", ListView)
        if not self.themes:
            self.dismiss(None)
            return
        self.dismiss(self.themes[view.index or 0])

    def action_dismiss(self) -> None:
        """关闭选择器。"""
        self.dismiss(None)
