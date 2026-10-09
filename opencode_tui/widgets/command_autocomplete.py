from __future__ import annotations

from ..core.command_registry import Command
from .file_autocomplete import AutocompletePopup


class CommandAutocomplete(AutocompletePopup):
    """斜杠命令补全弹窗，输入 / 时触发。

    与文件补全共用同一个基类，但候选项底下还挂着 `Command` 对象：采纳那一步要的是
    命令名，展示那一行要的是"/名字 + 说明"，只留字符串就拿不回名字了。
    """

    # 命令有二十条，一次只显示 6 行要翻四屏才看得全。放宽到 8 行，两屏看完。
    # 高度由基类按这个数算，不用在这里再写 max-height。
    MAX_ROWS = 8

    def __init__(self, **kwargs: object) -> None:
        super().__init__(id="command-autocomplete", title="斜杠命令", **kwargs)
        self._commands: list[Command] = []

    def set_commands(self, commands: list[Command]) -> None:
        """按命令列表刷新候选。列表为空时收起弹窗。"""
        self._commands = list(commands)
        self.set_items([command.display() for command in self._commands])

    def hide(self) -> None:
        """收起弹窗并清空候选命令。"""
        self._commands = []
        super().hide()

    def current_command(self) -> Command | None:
        """返回当前高亮的那条命令。"""
        if not self._commands or self._index < 0:
            return None
        return self._commands[self._index]

    @property
    def commands(self) -> list[Command]:
        """当前候选命令。"""
        return list(self._commands)
