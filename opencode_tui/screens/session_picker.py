from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from ..core.models import Session


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
            yield Static(self._question, id="confirm-text")
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
            yield Static("Sessions", id="picker-title")
            yield ListView(id="session-list")
            yield Static("enter 选择   r 重命名   d 删除   esc 关闭", id="picker-hint")

    def on_mount(self) -> None:
        """填充会话列表并聚焦。"""
        self.refresh_list()
        self.query_one("#session-list", ListView).focus()

    def refresh_list(self) -> None:
        """重建会话列表内容。"""
        view = self.query_one("#session-list", ListView)
        view.clear()
        self._ids = []
        for session in self.sessions:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(session.updated_at))
            label = f"{session.title}   {len(session.messages)} 条   {stamp}"
            view.append(ListItem(Label(label)))
            self._ids.append(session.id)
        if self._ids:
            view.index = 0

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
        self.sessions = self.app.session_manager.list_sessions()
        self.refresh_list()
        self.app.save_state()

    def _apply_delete(self, session_id: str, confirmed: bool) -> None:
        """应用删除结果。"""
        if not confirmed:
            return
        was_current = self.app.session_manager.current_session_id == session_id
        self.app.session_manager.delete_session(session_id)
        self.sessions = self.app.session_manager.list_sessions()
        self.refresh_list()
        self.app.save_state()
        if was_current:
            # 删掉的正是在看的那一个，会话管理器会切到最近的另一个。
            # 列表还盖在上面，聊天界面得重建，否则显示的还是被删掉的那份对话。
            self.app.refresh_chat()
        self.app.show_toast("会话已删除", "success")

    def _find(self, session_id: str) -> Session | None:
        """按 ID 找会话对象。"""
        for session in self.sessions:
            if session.id == session_id:
                return session
        return None
