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
    ERROR = "error"
    SUMMARY = "summary"


class ToolStatus(Enum):
    """工具执行状态。"""

    PENDING = "pending"
    AWAITING = "awaiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# 推理强度，输入框状态行与 /effort 命令都按这个顺序轮换。
EFFORTS: tuple[str, ...] = ("low", "medium", "high")

# 子 agent 的运行状态。只用于展示与重启恢复，不参与模型上下文。
AGENT_STATUSES: tuple[str, ...] = ("idle", "running", "done", "failed", "cancelled")


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
    # 这次调用是按哪一条规则放行的，或为什么被拒绝
    permission: str = ""
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
            "permission": self.permission,
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
            permission=data.get("permission", ""),
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


# 任务状态有五个字段。命令里可以打的别名 -> 数据里的字段名。
# 单数的写法也认，因为命令是手打的，多一个 s 少一个 s 不该是错。
TASK_FIELD_ALIASES: dict[str, str] = {
    "goal": "goal",
    "constraint": "constraints",
    "constraints": "constraints",
    "done": "done",
    "todo": "todo",
    "blocker": "blockers",
    "blockers": "blockers",
}

# 列表型字段，顺序即渲染顺序。goal 是单值，不在这里。
TASK_LIST_FIELDS: tuple[str, ...] = ("constraints", "done", "todo", "blockers")


@dataclass
class TaskState:
    """结构化的任务状态，独立于聊天记录。

    它是「用户约束不能因为丢弃就没了」这条要求的归处：约束存在这里，
    而不是靠摘要的某一段兜住。装配时它整块进 `TASK` 层、**不参与淘汰**，
    所以聊到多长，用户当初提的要求都还在。

    与聊天记录的分工：聊天记录是过程，这里是结论——目标是什么、答应了什么、
    做到哪一步、卡在哪。要改结论就改这里，不必回头翻聊天记录。
    """

    goal: str = ""
    constraints: list[str] = field(default_factory=list)  # 用户明确给出的要求
    done: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    version: int = 0  # 每次真的改了才 +1，供 trace 与判据比对

    def is_empty(self) -> bool:
        """五个字段全空时返回 True。空的就不必往上下文里塞一段废话。"""
        return not (
            self.goal or self.constraints or self.done
            or self.todo or self.blockers
        )

    def render(self) -> str:
        """渲染成给模型看的一段文字。空字段整段不出现。"""
        blocks = ["（本会话的任务状态，随进度更新）"]
        if self.goal:
            blocks.append(f"目标：{self.goal}")
        if self.constraints:
            blocks.append(
                "用户约束（每一步都生效）：\n"
                + "\n".join(f"- {item}" for item in self.constraints)
            )
        if self.done:
            blocks.append("已完成：\n" + "\n".join(f"- {item}" for item in self.done))
        if self.todo:
            blocks.append("待办：\n" + "\n".join(f"- {item}" for item in self.todo))
        if self.blockers:
            blocks.append("阻塞：\n" + "\n".join(f"- {item}" for item in self.blockers))
        return "\n".join(blocks)

    def apply(
        self,
        *,
        goal: str | None = None,
        add_constraints: Iterable[str] | None = None,
        done: Iterable[str] | None = None,
        todo: Iterable[str] | None = None,
        blockers: Iterable[str] | None = None,
    ) -> bool:
        """按字段更新，返回是否真的变了。变了才把 `version` 加一。

        两类字段的语义**刻意不同**：

        - `add_constraints` 是**追加**。约束是用户提的要求，模型每轮重写一遍的话，
          漏抄一条就等于把它删了——而这正是本模块要防的那件事。要删只能由用户来。
        - `done` / `todo` / `blockers` 是**整段替换**。这三类是模型自己的进度记录，
          随进度重写才是对的；传空列表就是清掉这一类。

        `None` 表示「这次不动这一类」，与传空列表不是一回事。
        """
        changed = False

        if goal is not None:
            text = goal.strip()
            if text and text != self.goal:
                self.goal = text
                changed = True

        for raw in add_constraints or ():
            text = str(raw).strip()
            if text and text not in self.constraints:
                self.constraints.append(text)
                changed = True

        for name, values in (("done", done), ("todo", todo), ("blockers", blockers)):
            if values is None:
                continue
            items = [str(item).strip() for item in values if str(item).strip()]
            if items != getattr(self, name):
                setattr(self, name, items)
                changed = True

        if changed:
            self.version += 1
        return changed

    def add(self, field: str, text: str) -> bool:
        """往某一类里加一条。`goal` 是单值，等同于覆盖。

        命令那条路走这里：用户打的是一句话，不该为了加一条待办而把整张表重写一遍。
        """
        name = TASK_FIELD_ALIASES.get(field.strip().lower())
        if name is None:
            raise ValueError(f"不认识的字段：{field}")
        text = text.strip()
        if not text:
            raise ValueError("内容不能为空")
        if name == "goal":
            return self.apply(goal=text)
        if name == "constraints":
            return self.apply(add_constraints=[text])
        return self.apply(**{name: [*getattr(self, name), text]})

    def clear(self) -> bool:
        """清空全部字段。已经空的返回 False，不必白写一次盘。"""
        if self.is_empty():
            return False
        self.goal = ""
        for name in TASK_LIST_FIELDS:
            getattr(self, name).clear()
        self.version += 1
        return True

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。"""
        return {
            "goal": self.goal,
            "constraints": list(self.constraints),
            "done": list(self.done),
            "todo": list(self.todo),
            "blockers": list(self.blockers),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        """从字典还原。老状态文件里没有这一段，缺字段时按空处理。"""

        def strings(key: str) -> list[str]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [str(item).strip() for item in raw if str(item).strip()]

        return cls(
            goal=str(data.get("goal") or ""),
            constraints=strings("constraints"),
            done=strings("done"),
            todo=strings("todo"),
            blockers=strings("blockers"),
            version=int(data.get("version") or 0),
        )


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
    # 手动压缩留下的位置：这个序号之前的消息不发出去，会话里的消息一条不少
    compact_upto: int = 0
    # 摘要正文，以及它覆盖到第几条消息。两者随会话写进状态文件
    summary: str = ""
    summary_upto: int = 0
    # 结构化的任务状态：目标、用户约束、进度、阻塞。整块进上下文的 TASK 层，不参与淘汰
    task_state: TaskState = field(default_factory=TaskState)
    # ---- 子 agent ----
    # 父会话 id。根会话为空，这一条也是「是不是子 agent」的唯一判据。
    parent_id: str = ""
    # 根会话 id。根会话自己为空，取家族时用 `family_id`。
    # 单独存一份而不是顺着 parent_id 递归：深度上限是 1，递归没有收益，
    # 而每次取登记表都要顺着链找一遍会把一件 O(1) 的事写成 O(depth)。
    root_id: str = ""
    # 0=根，1=子 agent。硬上限 1 层，见 AgentHost.spawn。
    depth: int = 0
    # user / agent：谁派发的。模型派发的会写进父会话的 AGENT Part。
    origin: str = "user"
    # fork 的父会话消息区间 [seed_start, seed_end)，闭开区间，便于直接切片。
    seed_start: int = 0
    seed_end: int = 0
    # idle / running / done / failed / cancelled
    status: str = "idle"
    status_note: str = ""
    spawned_at: float = 0.0

    @property
    def family_id(self) -> str:
        """同一个根会话下的所有会话共用的编号。根会话返回自己。"""
        return self.root_id or self.id

    @property
    def is_subagent(self) -> bool:
        """是不是子 agent。"""
        return bool(self.parent_id) or self.depth > 0

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

    def total_cached(self) -> int:
        """返回会话内累计的缓存命中输入 token 数。"""
        return sum(int(message.tokens.get("cached", 0)) for message in self.messages)

    def total_uncached(self) -> int:
        """返回会话内累计的缓存未命中输入 token 数。"""
        return sum(int(message.tokens.get("uncached", 0)) for message in self.messages)

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
            "compact_upto": self.compact_upto,
            "summary": self.summary,
            "summary_upto": self.summary_upto,
            "task_state": self.task_state.to_dict(),
            "parent_id": self.parent_id,
            "root_id": self.root_id,
            "depth": self.depth,
            "origin": self.origin,
            "seed_start": self.seed_start,
            "seed_end": self.seed_end,
            "status": self.status,
            "status_note": self.status_note,
            "spawned_at": self.spawned_at,
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
            compact_upto=int(data.get("compact_upto") or 0),
            summary=data.get("summary", ""),
            summary_upto=int(data.get("summary_upto") or 0),
            task_state=TaskState.from_dict(
                data.get("task_state") if isinstance(data.get("task_state"), dict) else {}
            ),
            parent_id=str(data.get("parent_id") or ""),
            root_id=str(data.get("root_id") or ""),
            depth=int(data.get("depth") or 0),
            origin=str(data.get("origin") or "user"),
            seed_start=int(data.get("seed_start") or 0),
            seed_end=int(data.get("seed_end") or 0),
            status=str(data.get("status") or "idle"),
            status_note=str(data.get("status_note") or ""),
            spawned_at=float(data.get("spawned_at") or 0.0),
        )
        for raw in data.get("messages") or []:
            session.messages.append(Message.from_dict(raw))
        return session
