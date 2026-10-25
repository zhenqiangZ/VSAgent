from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from typing import Any, Iterable, Literal

# 一条 Part 的结构化读数最多占这么多字节。超了整份丢掉：
# 它是提示不是账本，没必要为它把状态文件撑大。
TOOL_META_LIMIT = 4096

_SEQUENCES: dict[str, count] = {
    "part": count(1),
    "message": count(1),
    "session": count(1),
}


def _take(kind: str) -> int:
    """取某类 ID 的下一个序号。"""
    return next(_SEQUENCES[kind])


def _clean_meta(value: Any) -> Any:
    """把结构化读数收成 TOML 写得进去的形状。

    TOML 没有 null，遇到 None 只能丢；其余非基本类型一律转成字符串——
    宁可少一条读数，也不能因为一个枚举或 Path 让整次保存失败。
    """
    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _clean_meta(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_clean_meta(item) for item in value if item is not None]
    return str(value)


def new_part_id() -> str:
    """生成按时间顺序递增的 Part ID。"""
    return f"prt_{_take('part'):04d}"


def new_message_id() -> str:
    """生成按时间顺序递增的 Message ID。"""
    return f"msg_{_take('message'):04d}"


def new_session_id() -> str:
    """生成按时间顺序递增的 Session ID。"""
    return f"ses_{_take('session'):04d}"


def _event_serial(event_id: str) -> int:
    """从 `evt_0007` 里取出 7；格式不对时返回 0。

    事件编号是**会话内**的连号，不占全局序列：两个会话各自从 evt_0001 开始，
    这样 `/events` 里看到的编号短、也好念。
    """
    head, _, tail = event_id.partition("_")
    if head != "evt":
        return 0
    try:
        return int(tail)
    except ValueError:
        return 0


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
    # 工具返回的结构化读数（handle、计数、引擎参数这类）。
    #
    # 它**不进**发给模型的正文：正文是工具自己写的那一段，模型看那个。
    # 它有两个用处——界面/网页端取数字，以及证据层把「这次到底产出了什么」
    # 记成一行索引。后者是它真正被需要的地方：从中文输出里认关键行并不可靠。
    tool_meta: dict[str, Any] = field(default_factory=dict)
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
            "tool_meta": self.meta_for_state(),
            "file_path": self.file_path,
            "agent_name": self.agent_name,
            "permission": self.permission,
            "patch_added": self.patch_added,
            "patch_removed": self.patch_removed,
            "timestamp": self.timestamp,
        }

    def meta_for_state(self) -> dict[str, Any]:
        """落盘用的结构化读数；太大就整份丢掉。

        它是给证据层与界面的提示，不是账本：为它把状态文件撑大（甚至因为
        一个 TOML 写不进去的值把整次保存弄坏）不值得。丢掉之后唯一的变化是
        证据层回落到「认关键行」，功能不受影响。
        """
        if not self.tool_meta:
            return {}
        cleaned = {
            str(key): _clean_meta(value)
            for key, value in self.tool_meta.items()
            if value is not None
        }
        try:
            size = len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return {}
        return cleaned if size <= TOOL_META_LIMIT else {}

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
            # 旧状态文件里没有这个字段；类型不对时当没有，不让一条坏记录
            # 把整份状态读成空。
            tool_meta=(
                dict(data.get("tool_meta"))
                if isinstance(data.get("tool_meta"), dict)
                else {}
            ),
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

# 渲染时每个列表字段最多显示多少条。
#
# 这一段是 pinned 的、每一步都原样进上下文，模型一直往「已完成」里追加的话，
# 它会自己把窗口吃满。条目本身不删，只是不进这一份——`/task` 照旧能看到全部。
TASK_RENDER_LIMITS: dict[str, int] = {
    "constraints": 20,
    "done": 20,
    "todo": 10,
    "blockers": 10,
}


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
        """渲染成给模型看的一段文字。空字段整段不出现。

        每个列表字段有条数上限：这一段每一步都进上下文，模型一直往「已完成」
        里追加的话，它会自己把窗口吃满。超出的条目**不删**，只是不在这里显示，
        `/task` 仍然看得到全部。
        """
        blocks = ["（本会话的任务状态，随进度更新）"]
        if self.goal:
            blocks.append(f"目标：{self.goal}")
        for title, name in (
            ("用户约束（每一步都生效）", "constraints"),
            ("已完成", "done"),
            ("待办", "todo"),
            ("阻塞", "blockers"),
        ):
            items = getattr(self, name)
            if not items:
                continue
            shown = items[: TASK_RENDER_LIMITS[name]]
            block = title + "：\n" + "\n".join(f"- {item}" for item in shown)
            if len(items) > len(shown):
                block += f"\n- （另有 {len(items) - len(shown)} 条未列出）"
            blocks.append(block)
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


# 事件怎么来的。三种都要能读回来，区别只在「谁写的」：
# manual 是模型主动收口，auto 是溢出时本地兜底，local 是用户 /compact 时补的。
EVENT_KINDS: tuple[str, ...] = ("manual", "auto", "local")


@dataclass
class Event:
    """一段已经收口的工作：用标题与摘要代替那段原文。

    它是摘要与 RECENT 之间的那一层。摘要回答「很久以前发生过什么」，
    事件回答「刚才那几步做了什么、留下了什么」；粒度比摘要细、比逐条消息粗。

    `start` / `end` 是消息下标（闭开区间），与 `compact_upto` 同一套口径。
    事件**连续覆盖**一段：首条从 `summary_upto` 开始，后一条接着前一条的 `end`，
    RECENT 从最后一条的 `end` 开始。这条不变式由 `Session.sanitize_events` 保证，
    装配层因此只需要发一段 RECENT，不必拼补。
    """

    id: str = ""
    title: str = ""
    summary: str = ""
    actions: list[str] = field(default_factory=list)    # 用过的工具名
    artifacts: list[str] = field(default_factory=list)  # 产出的文件与登记条目
    issues: list[str] = field(default_factory=list)     # 失败项的首行
    start: int = 0
    end: int = 0
    kind: str = "manual"  # manual / auto / local，见 EVENT_KINDS
    degraded: bool = False  # 摘要来自本地汇编而不是模型
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。"""
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "actions": list(self.actions),
            "artifacts": list(self.artifacts),
            "issues": list(self.issues),
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "degraded": self.degraded,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """从字典还原事件。字段类型不对时当空，不让一条坏记录毁掉整份状态。"""

        def strings(key: str) -> list[str]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [str(item) for item in raw if str(item).strip()]

        kind = str(data.get("kind") or "manual")
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            actions=strings("actions"),
            artifacts=strings("artifacts"),
            issues=strings("issues"),
            start=int(data.get("start") or 0),
            end=int(data.get("end") or 0),
            kind=kind if kind in EVENT_KINDS else "manual",
            degraded=bool(data.get("degraded")),
            created_at=float(data.get("created_at") or time.time()),
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
    # 事件列表：已经收口的一段消息被压成标题 + 摘要，原文不再进上下文。
    # 连续覆盖 `[summary_upto, events[-1].end)`，RECENT 从那里往后接。
    events: list[Event] = field(default_factory=list)
    event_seq: int = 0
    # 已经用 load_tools 装入的领域工具族。核心工具不受它影响，
    # 装在会话上而不是注册表上：注册表按「根 / 子 agent」共享两份，
    # 记在那里会让一个会话装过的工具漏到另一个会话。
    tool_families: list[str] = field(default_factory=list)
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

    def add_event(self, event: Event) -> Event:
        """追加一条事件；没编号的按会话内序号补一个。"""
        if not event.id:
            self.event_seq += 1
            event.id = f"evt_{self.event_seq:04d}"
        self.events.append(event)
        self.touch()
        return event

    @property
    def event_end(self) -> int:
        """事件覆盖到第几条消息（不含）；没有事件时是 0。"""
        return self.events[-1].end if self.events else 0

    def sanitize_events(self) -> None:
        """丢掉越界、非连续的事件，并把 `compact_upto` 推到覆盖范围末尾。

        事件必须连续：中间缺一段的话，那一段原文就既不在事件里、也不在 RECENT 里，
        内容无声消失，而装配出来的上下文看起来完全正常。
        所以一旦发现断点，断点之后的事件一律丢掉——保留前面能用的那一段。
        """
        limit = len(self.messages)
        kept: list[Event] = []
        cursor = self.summary_upto
        for event in self.events:
            if event.start != cursor or event.end <= event.start or event.end > limit:
                break
            kept.append(event)
            cursor = event.end
        self.events = kept
        self.event_seq = max(self.event_seq, len(kept))
        if kept:
            self.compact_upto = max(self.compact_upto, kept[-1].end)

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
            "events": [event.to_dict() for event in self.events],
            "event_seq": self.event_seq,
            "tool_families": list(self.tool_families),
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
        raw_events = data.get("events")
        if isinstance(raw_events, list):
            session.events = [
                Event.from_dict(item) for item in raw_events if isinstance(item, dict)
            ]
        session.event_seq = int(data.get("event_seq") or 0)
        for event in session.events:
            session.event_seq = max(session.event_seq, _event_serial(event.id))
        raw_families = data.get("tool_families")
        if isinstance(raw_families, list):
            session.tool_families = [str(item) for item in raw_families if str(item)]
        # 读回来就校验一次：手工改过状态文件、或者旧版本写了一半的事件，
        # 都在进装配之前被裁掉，而不是等到发请求时才露馅。
        session.sanitize_events()
        return session
