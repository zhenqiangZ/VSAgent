from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from .models import Session, new_session_id, sync_sequences

STATE_VERSION = 1


class SessionManager:
    """管理会话的创建、切换与持久化。"""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or default_state_path()
        self.sessions: dict[str, Session] = {}
        self.current_session_id: str = ""
        self.theme: str = "textual-dark"
        self.last_model_id: str = ""
        self.last_agent: str = "Build"
        self.show_details: bool = True
        self.show_thinking: bool = True

    @property
    def current(self) -> Session:
        """返回当前会话，不存在时自动创建。"""
        session = self.sessions.get(self.current_session_id)
        if session is None:
            session = self.new_session()
        return session

    def new_session(self, title: str = "New Session") -> Session:
        """创建新会话并切换为当前会话。"""
        session = Session(id=new_session_id(), title=title)
        session.model_id = self.last_model_id
        session.agent_name = self.last_agent
        self.sessions[session.id] = session
        self.current_session_id = session.id
        return session

    def get_session(self, session_id: str) -> Session | None:
        """按 ID 取会话。"""
        return self.sessions.get(session_id)

    def switch_session(self, session_id: str) -> Session | None:
        """切换当前会话，ID 不存在时返回 None。"""
        if session_id not in self.sessions:
            return None
        self.current_session_id = session_id
        return self.sessions[session_id]

    def list_sessions(self) -> list[Session]:
        """按更新时间倒序返回会话列表。"""
        return sorted(self.sessions.values(), key=lambda item: item.updated_at, reverse=True)

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话，返回是否成功。"""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        session.title = title.strip() or session.title
        session.touch()
        return True

    def delete_session(self, session_id: str) -> bool:
        """删除会话，删掉当前会话时自动切到最近的会话。"""
        if session_id not in self.sessions:
            return False
        del self.sessions[session_id]
        if self.current_session_id == session_id:
            remaining = self.list_sessions()
            self.current_session_id = remaining[0].id if remaining else ""
        return True

    def clear_messages(self, session_id: str) -> bool:
        """清空会话内的消息。"""
        session = self.sessions.get(session_id)
        if session is None:
            return False
        session.messages.clear()
        session.touch()
        return True

    def to_state(self) -> dict[str, Any]:
        """把全部状态整理为可写入 TOML 的字典。"""
        return {
            "version": STATE_VERSION,
            "theme": self.theme,
            "current_session_id": self.current_session_id,
            "last_model_id": self.last_model_id,
            "last_agent": self.last_agent,
            "show_details": self.show_details,
            "show_thinking": self.show_thinking,
            "sessions": [session.to_dict() for session in self.list_sessions()],
        }

    def save_to_toml(self, path: str | Path | None = None) -> Path:
        """把状态写入 TOML 文件，返回写入路径。"""
        target = Path(path) if path else self.state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            tomli_w.dump(self.to_state(), handle)
        return target

    def load_from_toml(self, path: str | Path | None = None) -> bool:
        """从 TOML 文件载入状态，文件不存在时返回 False。"""
        target = Path(path) if path else self.state_path
        if not target.exists():
            return False
        with target.open("rb") as handle:
            state = tomllib.load(handle)
        self.sessions = {}
        for raw in state.get("sessions") or []:
            session = Session.from_dict(raw)
            self.sessions[session.id] = session
        self.current_session_id = str(state.get("current_session_id", ""))
        if self.current_session_id not in self.sessions:
            first = self.list_sessions()
            self.current_session_id = first[0].id if first else ""
        self.theme = str(state.get("theme", self.theme))
        self.last_model_id = str(state.get("last_model_id", ""))
        self.last_agent = str(state.get("last_agent", self.last_agent))
        self.show_details = bool(state.get("show_details", True))
        self.show_thinking = bool(state.get("show_thinking", True))
        sync_sequences(
            (session.id for session in self.sessions.values()),
            (message.id for session in self.sessions.values() for message in session.messages),
            (
                part.id
                for session in self.sessions.values()
                for message in session.messages
                for part in message.parts
            ),
        )
        return True


def default_state_path() -> Path:
    """返回默认状态文件路径。"""
    return Path.home() / ".opencode_tui" / "state.toml"
