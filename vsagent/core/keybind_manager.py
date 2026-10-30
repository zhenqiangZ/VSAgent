from __future__ import annotations

from .command_registry import CommandRegistry


class KeybindManager:
    """维护快捷键说明表，供帮助界面列出。

    表里只有"按下去不经过输入框"的那几个键。其余操作一律走 /命令 或 ctrl+p 命令面板，
    所以这里不登记任何命令的键位——命令与快捷键是两层，各管各的。
    """

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry
        self.direct_bindings: dict[str, str] = {
            "ctrl+p": "命令面板",
            "escape": "中断生成",
            "ctrl+c": "退出应用",
            "tab": "切换 Agent",
        }
        # 启动界面自己接住的那一组按键。与 direct_bindings 一样是手写的表，
        # 冒烟测试拿它与 HomeScreen.BINDINGS 逐对比，走散了会当场失败。
        self.home_bindings: dict[str, str] = {
            "enter": "发送",
            "tab": "切换 Agent",
            "ctrl+p": "命令面板",
            "ctrl+c": "退出应用",
        }

    def describe(self) -> list[tuple[str, str]]:
        """返回帮助界面使用的快捷键说明列表。"""
        return list(self.direct_bindings.items())

    def describe_home(self) -> list[tuple[str, str]]:
        """返回帮助界面里启动界面那一段的快捷键说明。"""
        return list(self.home_bindings.items())
