from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from .file_log import record as log_read
from .models import Session, new_session_id, sync_sequences

STATE_VERSION = 2

# 换名在目标文件正被别的进程打开时会被系统拒绝，隔几毫秒重试。
# 读的人打开文件的时间只有几毫秒，几次之内就能换过去。
REPLACE_ATTEMPTS = 40
REPLACE_RETRY_DELAY = 0.005


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
        session = self.create_session(title)
        self.current_session_id = session.id
        return session

    def create_session(
        self,
        title: str = "New Session",
        *,
        parent_id: str = "",
        root_id: str = "",
        depth: int = 0,
        origin: str = "user",
        seed_start: int = 0,
        seed_end: int = 0,
        status: str = "idle",
        spawned_at: float = 0.0,
    ) -> Session:
        """创建一个会话，**不切换**当前会话。

        子 agent 必须走这一条：切换 current 会让根会话界面在下次重建时读到子会话，
        而子 agent 是后台跑的另一条线，跟用户正看着哪一屏没有关系。
        """
        session = Session(id=new_session_id(), title=title)
        session.model_id = self.last_model_id
        session.agent_name = self.last_agent
        session.parent_id = parent_id
        session.root_id = root_id
        session.depth = depth
        session.origin = origin
        session.seed_start = seed_start
        session.seed_end = seed_end
        session.status = status
        session.spawned_at = spawned_at
        self.sessions[session.id] = session
        return session

    def children_of(self, session_id: str) -> list[Session]:
        """某个会话的直接子 agent，按创建时间升序。"""
        children = [
            session
            for session in self.sessions.values()
            if session.parent_id == session_id
        ]
        return sorted(children, key=lambda item: item.created_at)

    def root_sessions(self) -> list[Session]:
        """根会话列表，按更新时间倒序。子 agent 不进会话选择器。"""
        return [
            session
            for session in self.list_sessions()
            if not session.parent_id and session.depth == 0
        ]

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
        """删除会话，删掉当前会话时自动切到最近的根会话。

        删根会话要连它的子 agent 一起删：子 agent 的 `parent_id` 指向一个
        已经不存在的会话时，右栏永远列不出来，状态文件里却还留着它们的消息。
        """
        if session_id not in self.sessions:
            return False
        doomed = {session_id}
        doomed.update(
            session.id
            for session in self.sessions.values()
            if session.parent_id == session_id
        )
        for sid in doomed:
            del self.sessions[sid]
        if self.current_session_id in doomed:
            remaining = self.root_sessions() or self.list_sessions()
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
        """把状态写入 TOML 文件，返回写入路径。

        先把内容写进同目录下的临时文件，写完再换名到目标位置。读的人按
        名字打开，要么拿到换名前的旧文件，要么拿到换名后的新文件，中间
        不会看到写了一半的内容。直接往目标文件里写就做不到这一点：文件
        先缩到零字节再分段长回来，中间任何时刻去读都是半截内容。
        """
        target = Path(path) if path else self.state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            with staged.open("wb") as handle:
                tomli_w.dump(self.to_state(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(staged, target)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return target

    def load_from_toml(self, path: str | Path | None = None) -> bool:
        """从 TOML 文件载入状态，文件不存在时返回 False。"""
        target = Path(path) if path else self.state_path
        if not target.exists():
            log_read(target, purpose="state.load", ok=False, detail="文件不存在")
            return False
        try:
            with target.open("rb") as handle:
                state = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            # 坏状态文件在这里只记一笔再抛：上层要自己决定是重建还是退出。
            log_read(target, purpose="state.load", ok=False, detail=f"{type(error).__name__}：{error}")
            raise
        log_read(target, purpose="state.load", size=target.stat().st_size)
        self.load_from_state(state)
        return True

    def load_from_state(self, state: dict[str, Any]) -> None:
        """按已经解析好的状态字典填充内存。

        与 load_from_toml 分开是为了让调用方自己掌握读取那一刻的内容：
        状态文件可能正被写入，调用方读到的字节需要自己校验完整与否，校验
        通过之后直接把这份字典交进来，不必再读一次文件。
        """
        self.sessions = {}
        for raw in state.get("sessions") or []:
            session = Session.from_dict(raw)
            # 上次退出时还在跑的，进程已经不在了。留着 running 会让右栏显示
            # 一个永远转圈的子 agent，而它其实没有任何任务在跑。
            if session.status == "running":
                session.status = "idle"
                session.status_note = "上次退出时仍在运行"
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


def default_state_path() -> Path:
    """返回默认状态文件路径。"""
    return Path.home() / ".opencode_tui" / "state.toml"


def _replace_with_retry(staged: Path, target: Path) -> None:
    """把临时文件换名到目标位置，遇到占用就稍等再试。

    换名在目标文件正被别的进程打开时会被系统拒绝，读的人打开文件的时间
    只有几毫秒，隔几毫秒重试几次就能换过去。
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(staged, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_DELAY)
