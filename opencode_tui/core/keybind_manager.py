from __future__ import annotations

from .command_registry import CommandRegistry


class KeybindManager:
    """维护直接绑定表与 ctrl+x 第二击的命令映射，供帮助界面列出。"""

    LEADER_KEY = "ctrl+x"

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry
        self.direct_bindings: dict[str, str] = {
            "ctrl+p": "palette",
            "tab": "switch_agent",
            "ctrl+c": "interrupt",
            "ctrl+d": "exit",
            "ctrl+z": "suspend",
            "escape": "close_modal",
        }
        # 启动界面自己接住的那一组按键。与 direct_bindings 一样是手写的表，
        # 冒烟测试拿它与 HomeScreen.BINDINGS 逐对比，走散了会当场失败。
        self.home_bindings: dict[str, str] = {
            "enter": "submit",
            "tab": "switch_agent",
            "ctrl+p": "palette",
            "ctrl+n": "new_session",
            "ctrl+l": "sessions",
            "ctrl+m": "models",
            "ctrl+t": "themes",
            "ctrl+h": "help",
            "ctrl+q": "quit",
        }

    @property
    def leader_bindings(self) -> dict[str, str]:
        """第二击到命令名的映射。"""
        return {key: command.name for key, command in self.registry.leader_keys().items()}

    def describe(self) -> list[tuple[str, str]]:
        """返回帮助界面使用的快捷键说明列表。"""
        rows: list[tuple[str, str]] = []
        for key, action in self.direct_bindings.items():
            rows.append((key, action))
        rows.append((self.LEADER_KEY, "leader key 前导键"))
        for key, command in sorted(self.leader_bindings.items()):
            rows.append((f"{self.LEADER_KEY} {key}", command))
        return rows

    def describe_home(self) -> list[tuple[str, str]]:
        """返回帮助界面里启动界面那一段的快捷键说明。"""
        return list(self.home_bindings.items())
