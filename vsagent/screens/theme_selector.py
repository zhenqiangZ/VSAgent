from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Label, ListItem, ListView, Static

from .click_dismiss import BODY_CLASS, ClickDismissModal


class ThemeSelectorScreen(ClickDismissModal[str | None]):
    """主题选择器，列出 Textual 可用主题。"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "select_theme", "Apply", priority=True),
    ]

    def __init__(self, themes: list[str] | None = None) -> None:
        super().__init__()
        self.themes = list(themes or [])

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-selector", classes=BODY_CLASS):
            yield Static("主题", id="theme-title")
            yield ListView(id="theme-list")
            yield Static("enter 应用   点击空白处关闭（esc）   ▸ 当前主题", id="theme-hint")

    def on_mount(self) -> None:
        """填充主题列表并聚焦。

        当前主题画 ▸ 并高亮：列表有二十多个，不标出来的话打开面板得先记住自己
        现在用的是哪一个，而 `enter` 是立刻生效的——选错就是整屏换色。
        """
        view = self.query_one("#theme-list", ListView)
        current = self.app.theme
        for name in self.themes:
            # 标记位与会话列表同一套：有标记画 ▸、没有留一个空格，名字列才对得齐。
            row = Label(
                f"{'▸' if name == current else ' '} {name}",
                markup=False,
                classes="theme-row",
            )
            if name == current:
                row.add_class("-current")
            view.append(ListItem(row))
        if self.themes:
            view.index = self.themes.index(current) if current in self.themes else 0
        self.query_one("#theme-title", Static).update(f"主题　当前 {current}")
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
