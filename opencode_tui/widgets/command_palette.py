from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from ..core.command_registry import Command, CommandRegistry


class CommandPalette(ModalScreen[str | None]):
    """命令面板，支持模糊搜索命令。"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "execute", "Execute", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
    ]

    def __init__(self, registry: CommandRegistry) -> None:
        super().__init__()
        self.registry = registry
        self._results: list[Command] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="command-palette"):
            yield Static("命令面板", id="palette-title")
            yield Input(placeholder="输入命令名或描述…", id="palette-input")
            yield ListView(id="palette-list")
            yield Static("enter 执行   up/down 选择   esc 关闭", id="palette-hint")

    def on_mount(self) -> None:
        """打开时聚焦输入框并列出全部命令。"""
        self.query_one("#palette-input", Input).focus()
        self._refresh(self.registry.all())

    def on_input_changed(self, event: Input.Changed) -> None:
        """输入变化时按模糊匹配刷新结果。"""
        self._refresh(self.registry.fuzzy_match(event.value))

    def action_execute(self) -> None:
        """执行当前选中的命令。"""
        index = self.query_one("#palette-list", ListView).index or 0
        if not self._results:
            self.dismiss(None)
            return
        self.dismiss(self._results[index].name)

    def action_move_up(self) -> None:
        """上移候选项。"""
        self._move(-1)

    def action_move_down(self) -> None:
        """下移候选项。"""
        self._move(1)

    def action_dismiss(self) -> None:
        """关闭面板且不返回命令。"""
        self.dismiss(None)

    def _move(self, delta: int) -> None:
        """移动候选项高亮。"""
        view = self.query_one("#palette-list", ListView)
        if not self._results:
            return
        view.index = (view.index + delta) % len(self._results)

    def _refresh(self, commands: list[Command]) -> None:
        """重建结果列表。"""
        self._results = list(commands)
        view = self.query_one("#palette-list", ListView)
        view.clear()
        for command in self._results:
            view.append(ListItem(Label(command.display(), markup=False)))
        if self._results:
            view.index = 0
