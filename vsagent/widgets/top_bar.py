from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message as TextualMessage
from textual.widgets import Static

from .. import palette

# 右侧入口：标签与它要执行的命令名。顺序按使用频率排，帮助放最后。
LINKS: tuple[tuple[str, str], ...] = (
    ("模型", "model"),
    ("工具", "tools"),
    ("主题", "themes"),
    ("帮助", "help"),
)


class TopLink(Static):
    """顶栏右侧的一格入口。"""

    class Picked(TextualMessage):
        """用户点了这一格。"""

        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    def __init__(self, label: str, command: str) -> None:
        super().__init__(label, classes="top-link", markup=False)
        self.command = command

    def on_click(self) -> None:
        self.post_message(self.Picked(self.command))


class TopBar(Horizontal):
    """顶栏：左边应用名，右边是模型、工具、主题、帮助四格。

    入口用命令名接回应用，不另开一条执行路径：`/model`、`/tools` 这些命令本身
    已经有面板，顶栏只是把它们摆到看得见的地方。整条只有一行，底色与底部那一条
    相同；中间正文是纯黑，上下两条浅底的横带把内容夹住。不画下边框——边框会把
    这一条撑成两行，而它提供的信息量不值两行。
    """

    DEFAULT_CSS = palette.css("""
    TopBar {
        width: 100%;
        height: 1;
        background: $vsa-rail;
    }
    TopBar .top-brand {
        width: 1fr;
        height: 1;
        padding: 0 1;
        color: $vsa-text-1;
        text-style: bold;
    }
    .top-link {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $vsa-text-2;
    }
    .top-link:hover {
        color: $vsa-accent;
    }
    """)

    def compose(self) -> ComposeResult:
        yield Static("VSAgent", classes="top-brand", markup=False)
        for label, command in LINKS:
            yield TopLink(label, command)
