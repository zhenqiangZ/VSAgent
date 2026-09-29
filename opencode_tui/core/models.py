from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from typing import Any, Iterable, Literal

_SEQUENCES: dict[str, count] = {
    "part": count(1),
    "message": count(1),
    "session": count(1),
}


def _take(kind: str) -> int:
    """取某类 ID 的下一个序号。"""
    return next(_SEQUENCES[kind])


def new_part_id() -> str:
    """生成按时间顺序递增的 Part ID。"""
    return f"prt_{_take('part'):04d}"


def new_message_id() -> str:
    """生成按时间顺序递增的 Message ID。"""
    return f"msg_{_take('message'):04d}"


def new_session_id() -> str:
    """生成按时间顺序递增的 Session ID。"""
    return f"ses_{_take('session'):04d}"


def sync_sequences(session_ids: Iterable[str], message_ids: Iterable[str], part_ids: Iterable[str]) -> None:
    """载入历史数据后把 ID 序号推到已用最大值之后，避免 ID 撞车。"""
    for kind, prefix, values in (
        ("session", "ses_", session_ids),
        ("message", "msg_", message_ids),
        ("part", "prt_", part_ids),
    ):
        highest = 0
        for value in values:
            tail = value[len(prefix):] if value.startswith(prefix) else ""
            if tail.isdigit():
                highest = max(highest, int(tail))
        _SEQUENCES[kind] = count(highest + 1)


class PartType(Enum):
    """Part 的类型，对应 OpenCode 的 Part 模型。"""

    TEXT = "text"
    REASONING = "reasoning"
    TOOL = "tool"
    FILE = "file"
    AGENT = "agent"
    STEP_START = "step_start"
    STEP_FINISH = "step_finish"
    PATCH = "patch"
    SUBTASK = "subtask"
    RETRY = "retry"
    COMPACTION = "compaction"


class ToolStatus(Enum):
    """工具执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# 推理强度，输入框状态行与 /effort 命令都按这个顺序轮换。
EFFORTS: tuple[str, ...] = ("low", "medium", "high")


@dataclass
class Part:
    """一条消息内的最小渲染单元。"""

    id: str = field(default_factory=new_part_id)
    message_id: str = ""
    type: PartType = PartType.TEXT
    text: str = ""
    tool_name: str = ""
    tool_status: ToolStatus | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: str = ""
    file_path: str = ""
    agent_name: str = ""
    # 补丁的增删行数。放在数据里，渲染层只负责显示；接上真后端之后这里会被真实数字填上。
    patch_added: int = 0
    patch_removed: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。"""
        return {
            "id": self.id,
            "message_id": self.message_id,
            "type": self.type.value,
            "text": self.text,
            "tool_name": self.tool_name,
            "tool_status": self.tool_status.value if self.tool_status else "",
            "tool_input": self.tool_input,
            "tool_output": self.tool_output,
            "file_path": self.file_path,
            "agent_name": self.agent_name,
            "patch_added": self.patch_added,
            "patch_removed": self.patch_removed,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Part:
        """从字典还原 Part。"""
        status = data.get("tool_status") or ""
        return cls(
            id=data.get("id") or new_part_id(),
            message_id=data.get("message_id", ""),
            type=PartType(data.get("type", PartType.TEXT.value)),
            text=data.get("text", ""),
            tool_name=data.get("tool_name", ""),
            tool_status=ToolStatus(status) if status else None,
            tool_input=dict(data.get("tool_input") or {}),
            tool_output=data.get("tool_output", ""),
            file_path=data.get("file_path", ""),
            agent_name=data.get("agent_name", ""),
            patch_added=int(data.get("patch_added") or 0),
            patch_removed=int(data.get("patch_removed") or 0),
            timestamp=float(data.get("timestamp", time.time())),
        )


@dataclass
class Message:
    """一条对话消息，由若干 Part 组成。"""

    id: str = field(default_factory=new_message_id)
    session_id: str = ""
    role: Literal["user", "assistant"] = "user"
    parts: list[Part] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    model_id: str = ""
    provider_id: str = ""
    cost: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    def add_part(self, part: Part) -> Part:
        """把 Part 挂到本消息上并返回它。"""
        part.message_id = self.id
        self.parts.append(part)
        return part

    def part_by_id(self, part_id: str) -> Part | None:
        """按 ID 查找 Part。"""
        for part in self.parts:
            if part.id == part_id:
                return part
        return None

    def total_tokens(self) -> int:
        """返回输入与输出的 token 总数。"""
        return int(self.tokens.get("input", 0)) + int(self.tokens.get("output", 0))

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "parts": [part.to_dict() for part in self.parts],
            "created_at": self.created_at,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "cost": self.cost,
            "tokens": self.tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """从字典还原 Message。"""
        message = cls(
            id=data.get("id") or new_message_id(),
            session_id=data.get("session_id", ""),
            role=data.get("role", "user"),
            created_at=float(data.get("created_at", time.time())),
            model_id=data.get("model_id", ""),
            provider_id=data.get("provider_id", ""),
            cost=float(data.get("cost", 0.0)),
            tokens=dict(data.get("tokens") or {}),
        )
        for raw in data.get("parts") or []:
            message.add_part(Part.from_dict(raw))
        return message


@dataclass
class Session:
    """一个会话，持有消息列表与元信息。"""

    id: str = field(default_factory=new_session_id)
    title: str = "New Session"
    messages: list[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    model_id: str = ""
    agent_name: str = "Build"
    effort: str = EFFORTS[0]

    def add_message(self, message: Message) -> Message:
        """追加消息并刷新更新时间。"""
        message.session_id = self.id
        self.messages.append(message)
        self.touch()
        return message

    def touch(self) -> None:
        """刷新更新时间戳。"""
        self.updated_at = time.time()

    def total_tokens(self) -> int:
        """返回会话内累计 token 数。"""
        return sum(message.total_tokens() for message in self.messages)

    def total_cost(self) -> float:
        """返回会话内累计成本。"""
        return sum(message.cost for message in self.messages)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。"""
        return {
            "id": self.id,
            "title": self.title,
            "messages": [message.to_dict() for message in self.messages],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model_id": self.model_id,
            "agent_name": self.agent_name,
            "effort": self.effort,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        """从字典还原 Session。"""
        session = cls(
            id=data.get("id") or new_session_id(),
            title=data.get("title", "New Session"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            model_id=data.get("model_id", ""),
            agent_name=data.get("agent_name", "Build"),
            effort=data.get("effort") or EFFORTS[0],
        )
        for raw in data.get("messages") or []:
            session.messages.append(Message.from_dict(raw))
        return session
