from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container
from textual.message import Message
from textual.widgets import Static

from .. import palette

# 上下文各层在界面上的名字。键是 `context.Layer` 的值。
LAYER_LABELS: dict[str, str] = {
    "prefix": "稳定前缀",
    "memory": "常驻记忆",
    "task": "任务状态",
    "event": "阶段事件",
    "recent": "近期对话",
    "evidence": "按需证据",
    "history": "历史摘要",
}


def percent_text(value: int, base: int) -> str:
    """把 value 占 base 的比例写成百分比。小于 1% 时写 <1%，不四舍五入成 0%。"""
    if base <= 0 or value <= 0:
        return "0%"
    share = value / base * 100
    if share < 1:
        return "<1%"
    return f"{round(share)}%"


def percent_line(tokens: int, window: int) -> Text:
    """状态栏右端的占比数字。

    只写百分比：这一格只回答「窗口还剩多少」，具体的构成点开再看。
    占比超过 60% 转黄、超过 85% 转红，颜色本身就是提示。
    """
    percent = percent_text(tokens, window)
    ratio = tokens / window if window else 0.0
    color = palette.ACCENT
    if ratio >= 0.85:
        color = palette.ERROR
    elif ratio >= 0.6:
        color = palette.WARN
    return Text(percent, style=color)


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


class SidebarToggle(Static):
    """状态栏右端的侧栏开关：点一下折叠、再点一下展开。

    单独一个类而不是在 `ChatScreen.on_click` 里按坐标判断：认的是消息而不是位置，
    侧栏宽度、会话名长度怎么变都不会点到别的东西上。文字也由它自己换——
    「◧ 侧栏」是关着时按下去会打开，「◨ 收起」是开着时按下去会收起。
    """

    LABEL_HIDDEN = "◧ 侧栏"
    LABEL_SHOWN = "◨ 收起"

    class Toggled(Message):
        """用户点了这个按钮。"""

    def __init__(self) -> None:
        super().__init__("", id="side-toggle", markup=False)

    def render_state(self, visible: bool) -> None:
        """按侧栏当前是否可见改写标签。"""
        self.update(self.LABEL_SHOWN if visible else self.LABEL_HIDDEN)
        self.set_class(visible, "-open")

    def on_click(self) -> None:
        """点一下就把这一下交给聊天界面。"""
        self.post_message(self.Toggled())


class ContextUsage(Static):
    """状态栏里的上下文占比数字，点一下看每一项用了多少。

    单独一个类：状态栏各段的宽度随会话名与窗口变化，认消息比认位置稳。
    """

    class Pressed(Message):
        """用户点了上下文占比。"""

    def __init__(self) -> None:
        super().__init__(percent_line(0, 0), id="status-usage", markup=False)
        self._last: tuple[int, int] = (0, 0)

    def update_usage(self, tokens: int, window: int) -> None:
        """按当前输入 token 与窗口重画数字。数字没变就不重画。"""
        if (tokens, window) == self._last:
            return
        self._last = (tokens, window)
        self.update(percent_line(tokens, window))

    def on_click(self) -> None:
        """点一下就把这一下交给聊天界面。"""
        self.post_message(self.Pressed())


class StatusBar(Container):
    """底部状态栏：最左端会话名与目录，其后是分支与生成状态，右端占比与快捷键提示。"""

    # 右端常驻的快捷键提示，与实际按键绑定保持一致。
    KEYS_HINT = "ctrl+p 命令"

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self._cwd = Path.cwd()
        self._branch = ""
        self._busy = False
        self._title = ""
        self._written: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        # 左端画的是会话名与项目路径，路径里可能出现方括号，关掉标签解析。
        yield Static("", id="status-left", markup=False)
        yield ContextUsage()
        yield Static(self.KEYS_HINT, id="status-keys")
        # 侧栏开关靠右，与快捷键提示同一档颜色：两个都是次要控件。
        yield SidebarToggle()

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

    def set_sidebar(self, visible: bool) -> None:
        """把侧栏可见性反映到右端开关上。"""
        toggle = self.query(SidebarToggle)
        if toggle:
            toggle.first().render_state(visible)

    def update_status(
        self,
        *,
        title: str = "",
        context_tokens: int = 0,
        context_window: int = 0,
        busy: bool = False,
    ) -> None:
        """按最新数据刷新左端与右端的占比数字。

        右端的数字写的是当前上下文占窗口的比例：最近一次请求实际带上的输入 token
        除以窗口。会话累计用了多少不进这一行——它回答不了「还能聊多久」，
        而侧栏的用量段写着它。
        """
        self._busy = busy
        self._title = title
        self._render_left()
        self.update_usage(context_tokens=context_tokens, context_window=context_window)

    def update_usage(self, *, context_tokens: int, context_window: int) -> None:
        """只刷新右端的占比数字。每一步的装配度量到达时走这条，不去动左端与侧栏。"""
        if not self.query("#status-usage"):
            return
        self.query_one("#status-usage", ContextUsage).update_usage(
            context_tokens, context_window
        )

    def _render_left(self) -> None:
        """拼出左端的会话名、目录、分支与生成中标记。"""
        parts: list[str] = []
        if self._title:
            parts.append(self._title)
        parts.append(shorten_path(self._cwd))
        if self._branch and self._branch != "-":
            parts.append(self._branch)
        left = "  ".join(parts)
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
