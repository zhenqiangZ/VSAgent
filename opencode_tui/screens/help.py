from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..core.command_registry import CommandRegistry
from ..core.keybind_manager import KeybindManager


class HelpScreen(ModalScreen[None]):
    """帮助对话框，展示快捷键与斜杠命令。"""

    BINDINGS = [Binding("escape", "dismiss", "Close", priority=True)]

    def __init__(
        self,
        registry: CommandRegistry,
        keybinds: KeybindManager,
        origin: str = "chat",
    ) -> None:
        super().__init__()
        self.registry = registry
        self.keybinds = keybinds
        self.origin = origin

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-screen"):
            chat_section = ("快捷键", self.keybinds.describe(), "help-keys")
            home_section = ("启动界面", self.keybinds.describe_home(), "help-home")
            # 当前所在屏幕那一段排在最前：这两段加起来超过一屏，另一段一定要滚才看得到。
            first, second = (
                (home_section, chat_section)
                if self.origin == "home"
                else (chat_section, home_section)
            )
            for title, rows, element_id in (first, second):
                yield Static(title, classes="help-section")
                yield Static(self._rows_text(rows), id=element_id)
            yield Static("斜杠命令", classes="help-section")
            yield Static(self._commands_text(), id="help-commands")
            yield Static("esc 关闭", id="help-hint")

    @staticmethod
    def _rows_text(rows: list[tuple[str, str]]) -> str:
        """把（按键, 说明）列表排成两列。"""
        lines = ["快捷键            功能"]
        for key, action in rows:
            lines.append(f"{key:<18}{action}")
        return "\n".join(lines)

    def _commands_text(self) -> str:
        rows = ["命令              快捷键          说明"]
        for command in self.registry.all():
            keybind = command.keybind or "-"
            rows.append(f"/{command.name:<16}{keybind:<16}{command.desc}")
        return "\n".join(rows)

    def action_dismiss(self) -> None:
        """关闭帮助界面。"""
        self.dismiss(None)
