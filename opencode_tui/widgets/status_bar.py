from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Static


def shorten_path(path: Path, limit: int = 28) -> str:
    """把路径截断到指定长度，超出部分用 … 收尾。"""
    text = str(path)
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :]


def git_branch(cwd: Path) -> str:
    """读取当前目录的 git 分支，失败时返回 - 。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "-"
    branch = result.stdout.strip()
    return branch or "-"


def format_tokens(tokens: int) -> str:
    """把 token 数格式化为 157.8K 这样的短形式。"""
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}K"
    return f"{tokens / 1_000_000:.1f}M"


class StatusBar(Container):
    """底部状态栏：左端项目路径，右端用量与快捷键提示。"""

    # 右端常驻的快捷键提示，与实际按键绑定保持一致。
    KEYS_HINT = "ctrl+p commands"

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self._cwd = Path.cwd()
        self._branch = ""
        self._busy = False
        self._written: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="status-left")
        yield Static("", id="status-usage")
        yield Static(self.KEYS_HINT, id="status-keys")

    def on_mount(self) -> None:
        """先把路径画出来，git 分支交给另一个线程去问。

        问分支要起一个 git 子进程，实测一次 110 到 220 毫秒。在事件循环上同步等它回来，
        这期间键盘输入与重绘全部排队；进聊天界面那段路上要问好几次，累计将近一秒。
        """
        self._render_left()
        self.run_worker(self._probe_branch(), group="git-branch")

    async def _probe_branch(self) -> None:
        """在另一个线程里读分支，读完回到事件循环更新。"""
        branch = await asyncio.to_thread(git_branch, self._cwd)
        self._apply_branch(branch)

    def _apply_branch(self, branch: str) -> None:
        """把后台读到的分支补进左端。"""
        self._branch = branch
        self._render_left()

    def update_status(
        self,
        *,
        tokens: int = 0,
        cost: float = 0.0,
        latency_ms: int = 0,
        context_window: int = 0,
        busy: bool = False,
    ) -> None:
        """按最新数据刷新两栏内容。"""
        self._busy = busy
        self._render_left()
        percent = round(tokens / context_window * 100) if context_window else 0
        seconds = latency_ms / 1000
        usage = f"{format_tokens(tokens)} ({percent}%) · ${cost:.2f} · {seconds:.2f}s"
        self._write("#status-usage", usage)

    def _render_left(self) -> None:
        """拼出左端的路径、分支与生成中标记。"""
        left = shorten_path(self._cwd)
        if self._branch and self._branch != "-":
            left = f"{left}  {self._branch}"
        if self._busy:
            left = f"{left}  ● 生成中"
        self._write("#status-left", left)

    def _write(self, selector: str, text: str) -> None:
        """内容没变就不写。

        状态刷新在一轮生成里会被调到很多次，每次 `update()` 都会让这个组件重算一遍
        样式再重绘，内容往往一个字都没变。
        """
        if self._written.get(selector) == text:
            return
        self._written[selector] = text
        self.query_one(selector, Static).update(text)
