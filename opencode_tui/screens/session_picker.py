from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from ..core.models import Session

# 会话列表每行两段：标题一行、元信息一行。标题前面留一个标记位，
# 当前会话画 ▸，其余留空格——不这样对齐，标题列会随标记有无左右错开。
CURRENT_MARK = "▸"
EMPTY_MARK = " "


def relative_time(stamp: float, now: float | None = None) -> str:
    """把时间戳写成「刚刚 / 3 分钟前 / 昨天 / 09-30」这种相对时间。

    列表里排的是「最近用过哪个会话」，绝对时间反而要心算；超过一周之后
    相对时间（12 天前）已经没什么信息量，再换回日期。
    """
    current = time.time() if now is None else now
    delta = max(0.0, current - stamp)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86_400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 172_800:
        return "昨天"
    if delta < 604_800:
        return f"{int(delta // 86_400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


class ConfirmDialog(ModalScreen[bool]):
    """确认对话框，返回用户是否确认。"""

    BINDINGS = [
        Binding("y", "confirm", "Yes", priority=True),
        Binding("n", "cancel", "No", priority=True),
        Binding("enter", "confirm", "Yes", priority=True),
        Binding("escape", "cancel", "No", priority=True),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self._question, id="confirm-text", markup=False)
            yield Static("y 确认    n 取消", id="confirm-hint")

    def action_confirm(self) -> None:
        """确认。"""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """取消。"""
        self.dismiss(False)


class RenameDialog(ModalScreen[str | None]):
    """重命名对话框，返回新的标题。"""

    BINDINGS = [
        Binding("enter", "accept", "OK", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-dialog"):
            yield Static("重命名会话", id="rename-title")
            yield Input(value=self._title, id="rename-input")
            yield Static("enter 确定    esc 取消", id="rename-hint")

    def on_mount(self) -> None:
        """聚焦输入框。"""
        self.query_one("#rename-input", Input).focus()

    def action_accept(self) -> None:
        """接受新标题。"""
        self.dismiss(self.query_one("#rename-input", Input).value.strip() or None)

    def action_cancel(self) -> None:
        """放弃修改。"""
        self.dismiss(None)


class SessionPickerScreen(ModalScreen[str | None]):
    """会话选择器，支持选择、重命名与删除。"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "select_session", "Select", priority=True),
        Binding("r", "rename_session", "Rename", priority=True),
        Binding("d", "delete_session", "Delete", priority=True),
    ]

    def __init__(self, sessions: list[Session]) -> None:
        super().__init__()
        self.sessions = list(sessions)
        self._ids: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="session-picker"):
            yield Static("会话", id="picker-title")
            yield ListView(id="session-list")
            yield Static("enter 打开   r 重命名   d 删除   esc 关闭   ▸ 当前会话", id="picker-hint")

    def on_mount(self) -> None:
        """填充会话列表并聚焦。"""
        self.refresh_list()
        self.query_one("#session-list", ListView).focus()

    def refresh_list(self) -> None:
        """重建会话列表内容。

        每行两段：标题与元信息。标题前留一个标记位，当前会话画 ▸，
        其余留空格，标题列才对得齐；元信息放消息数、相对时间与模型名，
        这些是挑会话时真正要看的，标题本身往往看不出来是哪一次。
        """
        view = self.query_one("#session-list", ListView)
        view.clear()
        self._ids = []
        current = self.app.session_manager.current_session_id
        for session in self.sessions:
            is_current = session.id == current
            title = Static(
                f"{CURRENT_MARK if is_current else EMPTY_MARK} {session.title}",
                classes="session-row-title",
                markup=False,
            )
            if is_current:
                title.add_class("-current")
            meta = Static(
                f"  {len(session.messages)} 条消息 · "
                f"{relative_time(session.updated_at)} · {self._model_text(session)}",
                classes="session-row-meta",
                markup=False,
            )
            view.append(ListItem(Vertical(title, meta, classes="session-row")))
            self._ids.append(session.id)
        self.query_one("#picker-title", Static).update(self._title_text())
        if self._ids:
            view.index = 0

    def _title_text(self) -> str:
        """标题栏：会话总数。"""
        return f"会话　共 {len(self.sessions)} 个"

    def _model_text(self, session: Session) -> str:
        """这一轮会话用的模型名。

        旧会话上可能没记模型（`model_id` 为空），那时显示当前连接配置里的
        那一个；连接口都还没配就显示「未配置」，不要留一段空白。
        """
        return session.model_id or self.app.agent.config.model or "未配置"

    def _current_id(self) -> str | None:
        """返回当前高亮会话的 ID。"""
        view = self.query_one("#session-list", ListView)
        if not self._ids:
            return None
        return self._ids[view.index or 0]

    def action_select_session(self) -> None:
        """选择当前会话。"""
        self.dismiss(self._current_id())

    def action_dismiss(self) -> None:
        """关闭选择器。"""
        self.dismiss(None)

    def action_rename_session(self) -> None:
        """重命名当前会话。"""
        session_id = self._current_id()
        if session_id is None:
            return
        session = self._find(session_id)
        if session is None:
            return
        self.app.push_screen(RenameDialog(session.title), lambda title: self._apply_rename(session_id, title))

    def action_delete_session(self) -> None:
        """删除当前会话，先弹出确认框。"""
        session_id = self._current_id()
        if session_id is None:
            return
        session = self._find(session_id)
        if session is None:
            return
        question = f"删除会话「{session.title}」？"
        self.app.push_screen(ConfirmDialog(question), lambda ok: self._apply_delete(session_id, ok))

    def _apply_rename(self, session_id: str, title: str | None) -> None:
        """应用重命名结果。"""
        if not title:
            return
        self.app.session_manager.rename_session(session_id, title)
        # 重新填的时候只能填根会话：`list_sessions()` 里还有子 agent，
        # 一次重命名就会把它们混进 /sessions，看上去像一堆普通会话。
        self.sessions = self.app.session_manager.root_sessions()
        self.refresh_list()
        self.app.save_state()

    def _apply_delete(self, session_id: str, confirmed: bool) -> None:
        """应用删除结果。

        交给 `app.delete_session`：它还要清掉循环层、后台任务与共享登记表，
        只删数据的话正在跑的子 agent 会继续往一个已经不存在的会话上写。
        """
        if not confirmed:
            return
        self.app.delete_session(session_id)
        self.sessions = self.app.session_manager.root_sessions()
        self.refresh_list()
        self.app.show_toast("会话已删除", "success")

    def _find(self, session_id: str) -> Session | None:
        """按 ID 找会话对象。"""
        for session in self.sessions:
            if session.id == session_id:
                return session
        return None
