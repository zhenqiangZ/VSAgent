"""子 agent 的聊天界面：与主聊天同构，但没有派发入口，且带一条返回栏。

它继承 `ChatScreen` 而不是另写一份：消息区、输入框、状态栏、事件订阅全部一样，
差别只在「哪条会话」「能不能再派发」「怎么回去」。
"""

from __future__ import annotations

from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from ..core.models import Session
from .chat import ChatScreen

# 子 agent 界面能用的命令。**白名单而不是黑名单**：以后新增一条命令时，
# 它默认不会出现在子 agent 界面里，要放进来必须在这里显式加一笔。
# 挡掉的三类：再派发（agent）、开新会话（new）、切会话（sessions）。
ALLOWED_COMMANDS = frozenset(
    {
        "agents",
        "back",
        "compact",
        "connect",
        "delete",
        "details",
        "editor",
        "effort",
        "events",
        "exit",
        "export",
        "forget",
        "help",
        "init",
        "log",
        "memory",
        "model",
        "redo",
        "remember",
        "rename",
        "share",
        "sidebar",
        "task",
        "themes",
        "thinking",
        "tools",
        "undo",
        "unshare",
    }
)


class SubagentBanner(Static):
    """顶部返回栏。整条可点，不必记住快捷键。"""

    def on_click(self) -> None:
        """点一下 banner 就返回。"""
        screen = self.screen
        if isinstance(screen, SubagentChatScreen):
            screen.action_back()


class StartBar(Static):
    """「仅创建」留下的子 agent 顶部的开始条：点一下就开始跑。"""

    def on_click(self, event: events.Click) -> None:
        """点整条开始，不是只点某个按钮。"""
        screen = self.screen
        if isinstance(screen, SubagentChatScreen):
            event.stop()
            screen.action_start()


class SubagentChatScreen(ChatScreen):
    """一个子 agent 的持久聊天界面。"""

    # 身份跟着父会话走：这里不提供 Build/Plan 切换。
    allow_agent_switch = False
    # 命令入口按白名单收敛；新增命令默认不出现。
    allowed_commands = ALLOWED_COMMANDS

    BINDINGS = [
        *[binding for binding in ChatScreen.BINDINGS if binding.key != "tab"],
        Binding("ctrl+b", "back", "Back", priority=True),
        Binding("ctrl+r", "start", "Start", priority=True),
    ]

    def __init__(self, root: Path | None = None, session_id: str = "") -> None:
        super().__init__(root, session_id=session_id)

    @property
    def session(self) -> Session:
        """子 agent 会话。找不到时退回父会话，至少不会整屏崩掉。"""
        session = self.app.session_manager.get_session(self._session_id or "")
        if session is not None:
            return session
        return self.app.session_manager.current

    def compose(self) -> ComposeResult:
        yield SubagentBanner(self._banner_text(), id="subagent-banner", markup=False)
        yield StartBar("", id="subagent-start", markup=False)
        yield from super().compose()

    def _banner_text(self) -> str:
        """返回栏文字：父会话、名字、运行状态。"""
        session = self.session
        parent = self.app.session_manager.get_session(session.parent_id)
        status = (
            "待开始"
            if session.has_pending_prompt
            else {
                "idle": "空闲",
                "running": "运行中",
                "done": "完成",
                "failed": "失败",
                "cancelled": "已中断",
            }.get(session.status, session.status)
        )
        icon = "●" if session.status == "running" else "○"
        note = f" · {session.status_note[:16]}" if session.status_note else ""
        origin = "用户派发" if session.origin == "user" else "模型派发"
        span = ""
        if session.seed_end > session.seed_start:
            span = f" · 第 {session.seed_start + 1}–{session.seed_end} 条"
        return (
            f"◀ 返回   子 AGENT · {session.title}   "
            f"父会话：{parent.title if parent is not None else session.parent_id}   "
            f"{icon} {status}{note} · {origin}{span}"
        )

    def refresh_status(self) -> None:
        """刷新状态栏并把返回栏、开始条上的状态一起更新。"""
        super().refresh_status()
        banner = self.query(SubagentBanner)
        if banner:
            banner.first().update(self._banner_text())
        bar = self.query(StartBar)
        if bar:
            pending = self.session.has_pending_prompt
            bar.first().styles.display = "block" if pending else "none"
            if pending:
                bar.first().update("▶ 任务已就绪 · 点这里或按 ctrl+r 开始")

    def action_start(self) -> None:
        """开始「仅创建」留下的任务。

        任务书创建时已经写进会话，这里只起一轮；已经在跑或没有待发任务时
        只给一句提示，不重复发。
        """
        session = self.session
        if not session.has_pending_prompt:
            self.app.show_toast("这个子 agent 没有待开始的任务", "info")
            return
        if not self.app.host.start_pending(session.id):
            self.app.show_toast("任务没能开始：它已经在运行", "warning")
            return
        self.refresh_status()

    def on_editor_pane_interrupt_requested(self) -> None:
        """esc：运行中先中断，空闲时返回父会话。"""
        if self._busy:
            self.action_interrupt()
            return
        self.action_back()

    def action_back(self) -> None:
        """返回上一层。栈底是父会话，所以回到原处。"""
        self.app.pop_screen()
