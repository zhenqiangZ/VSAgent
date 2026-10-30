from __future__ import annotations

import time
from pathlib import Path

from ..models import Session


def workspace_root() -> Path:
    """会话工作目录的默认根。只在没有指定根时用到。"""
    return Path.home() / ".opencode_tui" / "workspaces"


class Workspace:
    """一个会话的工作目录，以及它可以向模型描述的当前状态。

    `app.py` 装配时把项目目录交给循环层，因此 `root` 就是项目目录：模型在系统提示里
    读到的路径与 `BashTool` 执行命令的位置是同一个。没有指定根时退回用户目录下按会话
    编号分开的一块地方，测试与单独调用走这一条。
    """

    def __init__(self, session_id: str, root: Path | None = None) -> None:
        self.session_id = session_id
        self.root = root if root is not None else workspace_root() / session_id

    def ensure(self) -> Path:
        """保证工作目录存在，返回它。"""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def state_text(self, session: Session) -> str:
        """写进系统提示的那一段：会话标题、工作目录与当前时间。"""
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        return (
            f"会话标题：{session.title}\n"
            f"工作目录：{self.root}\n"
            f"当前时间：{stamp}"
        )
