from __future__ import annotations

import time
from pathlib import Path

from ..models import Session


def workspace_root() -> Path:
    """会话工作目录的根。"""
    return Path.home() / ".opencode_tui" / "workspaces"


class Workspace:
    """一个会话的工作目录，以及它可以向模型描述的当前状态。

    受体文件、预处理产物、每一批的结果表都放在这个目录里，模型通过工具读写它们。
    这一轮只给出目录与会话的几项基本信息，受体与批次由后续工具层往描述里加。
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
