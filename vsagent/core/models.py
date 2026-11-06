from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Literal

# 一条 Part 的结构化读数最多占这么多字节。超了整份丢掉：
# 它是提示不是账本，没必要为它把状态文件撑大。
TOOL_META_LIMIT = 4096

_SEQUENCES: dict[str, int] = {
    "part": 1,
    "message": 1,
    "session": 1,
}

_SEQUENCE_LOCK = threading.Lock()
"""护住取号与载入时的读改写。

网页服务跑在应用进程的另一个线程里，它每次读盘都会走 `sync_sequences`，
与事件循环线程的取号是并发的。两个线程各读一次再各写一次，就会发出同一个号。
"""


def _take(kind: str) -> int:
    """取某类 ID 的下一个序号。"""
    with _SEQUENCE_LOCK:
        value = _SEQUENCES[kind]
        _SEQUENCES[kind] = value + 1
        return value


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
    """载入历史数据后把 ID 序号推到已用最大值之后，避免 ID 撞车。

    只往前推，不往回设。这份计数器是整个进程共用的一份，状态文件又只在一轮
    结束时写盘：读盘那一刻磁盘上的编号常常低于手里已经发出去的编号。往回设
    会让正在跑的这一轮把用过的编号再发一遍，界面按编号找组件，新内容被当成
    旧组件的更新，屏幕上就不出新东西了。网页端每个 `/api/sessions` 请求都会
    读一次盘（`web/routes.read_settled`），它走的就是这里。
    """
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
        with _SEQUENCE_LOCK:
            _SEQUENCES[kind] = max(_SEQUENCES[kind], highest + 1)


class PartType(Enum):
    """Part 的类型，对应上游 Agent 的 Part 模型。"""

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

# 假设只渲染未结的，按粒度分组给配额。**两类都要留位置**：只按提出顺序取前三条，
# 先提的三条策略假设会把机制假设整段挤出去，而「这一轮怎么筛」与「这批为什么活」
# 是两份要同时看见的判断。这一段每一步都进上下文，十几条全铺开会把窗口吃满。
HYPOTHESIS_RENDER_LIMITS: dict[str, int] = {
    "strategy": 2,
    "mechanism": 3,
}

# 假设的两种粒度。**策略**是「这一轮该怎么筛」（聚焦库还是多样性库、先组外还是先组内），
# **机制**是「这批分子为什么活」（结合哪个亚位点、靠什么相互作用）。两者都要能提，
# 因为只谈机制会在错误的库上做精细分析，只谈策略则永远不知道为什么要这么筛。
HYPOTHESIS_KINDS: tuple[str, ...] = ("strategy", "mechanism")
# 策略假设必须带方向：组外（去已知骨架之外找新的）还是组内（在已有骨架组里优化）。
# 存的值仍是 `expand` / `narrow`——它们已经写进了 `state.toml` 与运行目录的
# `provenance.hypotheses`，改名要连带迁移旧会话；显示层用组内/组外。
HYPOTHESIS_MODES: tuple[str, ...] = ("expand", "narrow")
# 状态只由「实验数据回填」与「显式关闭」两条路改，不由 `update` 改——
# 否则模型可以绕过证据把一条假设标成 supported。
HYPOTHESIS_STATUSES: tuple[str, ...] = ("open", "supported", "refuted", "inconclusive")


def _clip(text: str, limit: int) -> str:
    """压成一行并截断。渲染用，不改原值。"""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


@dataclass
class Hypothesis:
    """一条**可以被证伪**的判断。六要素缺一不可，缺了就不是假设而是印象。

    为什么要有这个对象：没有它的时候，模型交上来的是一张排名表——「前 10 个分子、
    各自的分数」。那种产出无法被证伪，也无法指导下一步该合哪个分子。有了它，
    「这批 hit 为什么活」才有地方落，实验数据回来才有东西可以对。

    字段对应 Roche 那篇 Perspective 的判据（`docs/reference.md` 第一节）：
    `claim` 是主张，`mechanism` 是机制，`evidence` 是已有证据，`test` 是怎么检验，
    `expected` 是预期结果（必须可判真假），`result_ref` 是回填的证据出处。
    """

    id: str
    kind: str = "mechanism"
    mode: str = ""          # 只有 strategy 用：expand / narrow
    claim: str = ""
    mechanism: str = ""
    evidence: str = ""
    test: str = ""
    expected: str = ""
    status: str = "open"
    result_ref: str = ""
    # 与哪几条假设在竞争。**两条假设给出不同预期时才填**：没有竞争关系的假设
    # 各自检验，填进来只会让「谁被实验排除」这件事读不清。
    rivals: list[str] = field(default_factory=list)

    def is_open(self) -> bool:
        return self.status == "open"

    def label(self) -> str:
        """给人看的一小段标签：`策略·组外` / `机制`。"""
        kind = "策略" if self.kind == "strategy" else "机制"
        if self.mode == "expand":
            kind += "·组外"
        elif self.mode == "narrow":
            kind += "·组内"
        return kind

    def render(self) -> str:
        """压成一行。进 TASK 层的那一份，所以要短。"""
        parts = [f"{self.id} [{self.label()}] {_clip(self.claim, 70)}"]
        if self.test:
            parts.append(f"检验：{_clip(self.test, 40)}")
        if self.expected:
            parts.append(f"预期：{_clip(self.expected, 30)}")
        if self.rivals:
            parts.append("竞争：" + "、".join(self.rivals))
        if not self.is_open():
            tail = f"状态：{self.status}"
            if self.result_ref:
                tail += f"（{self.result_ref}）"
            parts.append(tail)
        return "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "mode": self.mode,
            "claim": self.claim,
            "mechanism": self.mechanism,
            "evidence": self.evidence,
            "test": self.test,
            "expected": self.expected,
            "status": self.status,
            "result_ref": self.result_ref,
            "rivals": list(self.rivals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hypothesis:
        """从字典还原。认不出的 kind / status 一律回落到安全值，不抛。"""
        kind = str(data.get("kind") or "mechanism")
        status = str(data.get("status") or "open")
        return cls(
            id=str(data.get("id") or ""),
            kind=kind if kind in HYPOTHESIS_KINDS else "mechanism",
            mode=str(data.get("mode") or ""),
            claim=str(data.get("claim") or ""),
            mechanism=str(data.get("mechanism") or ""),
            evidence=str(data.get("evidence") or ""),
            test=str(data.get("test") or ""),
            expected=str(data.get("expected") or ""),
            status=status if status in HYPOTHESIS_STATUSES else "open",
            result_ref=str(data.get("result_ref") or ""),
            # 旧会话没有这个键：空表就是「没有登记竞争关系」，不需要迁移。
            rivals=[
                str(item).strip()
                for item in (data.get("rivals") or [])
                if str(item).strip()
            ],
        )


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
    # 本会话提出的假设。与 `done` 那几类不同，它们不整段替换：每条有自己的 id 与状态，
    # 由 `hypothesis` 工具与 `record_assay` 分别写。
    hypotheses: list[Hypothesis] = field(default_factory=list)
    version: int = 0  # 每次真的改了才 +1，供 trace 与判据比对

    def is_empty(self) -> bool:
        """所有字段全空时返回 True。空的就不必往上下文里塞一段废话。"""
        return not (
            self.goal or self.constraints or self.done
            or self.todo or self.blockers or self.hypotheses
        )

    def find_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        """按 `hyp#N` 找一条。找不到返回 None，由调用方给可读的错。"""
        wanted = str(hypothesis_id or "").strip()
        for item in self.hypotheses:
            if item.id == wanted:
                return item
        return None

    def next_hypothesis_id(self) -> str:
        """下一个编号。按已有条数推，不按最大 id 推：删过也不复用旧号。"""
        used = {item.id for item in self.hypotheses}
        index = len(self.hypotheses) + 1
        while f"hyp#{index}" in used:
            index += 1
        return f"hyp#{index}"

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
        # 假设只铺未结的：已结的留着是给导出与复盘用的，每一步都看一遍没有意义。
        # 配额按粒度分开，合并后仍按提出顺序——先给每一类各留位置，再还原顺序，
        # 读起来不会突然按类型跳。
        opened = [item for item in self.hypotheses if item.is_open()]
        if opened:
            budget = dict(HYPOTHESIS_RENDER_LIMITS)
            picked: list[int] = []
            for index, item in enumerate(opened):
                room = budget.get(item.kind, HYPOTHESIS_RENDER_LIMITS["mechanism"])
                if room > 0:
                    picked.append(index)
                    budget[item.kind] = room - 1
            shown = [opened[index] for index in picked]
            block = "假设（未结）：\n" + "\n".join(f"- {item.render()}" for item in shown)
            hidden = len(opened) - len(shown)
            if hidden:
                counts: dict[str, int] = {}
                for item in opened:
                    counts[item.kind] = counts.get(item.kind, 0) + 1
                for item in shown:
                    counts[item.kind] -= 1
                detail = "、".join(
                    f"{'策略' if kind == 'strategy' else '机制'} {count} 条"
                    for kind, count in counts.items()
                    if count > 0
                )
                block += (
                    f"\n- （另有 {hidden} 条未列出：{detail}；"
                    'hypothesis(action="list") 看全部）'
                )
            blocks.append(block)
        closed = [item for item in self.hypotheses if not item.is_open()]
        if closed:
            counts = "、".join(
                f"{status} {sum(1 for item in closed if item.status == status)}"
                for status in HYPOTHESIS_STATUSES
                if status != "open" and any(item.status == status for item in closed)
            )
            blocks.append(f"已结假设：{len(closed)} 条（{counts}）")
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
            "hypotheses": [item.to_dict() for item in self.hypotheses],
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

        raw_hypotheses = data.get("hypotheses")
        hypotheses = [
            Hypothesis.from_dict(item)
            for item in (raw_hypotheses if isinstance(raw_hypotheses, list) else [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]

        return cls(
            goal=str(data.get("goal") or ""),
            constraints=strings("constraints"),
            done=strings("done"),
            todo=strings("todo"),
            blockers=strings("blockers"),
            hypotheses=hypotheses,
            version=int(data.get("version") or 0),
        )


# 事件怎么来的。三种都要能读回来，区别只在「谁写的」：
# manual 是模型主动封存（close_topic），auto 是轮末由内核自动封存，
# local 是用户 /compact 或装不下时强推的。
EVENT_KINDS: tuple[str, ...] = ("manual", "auto", "local")

# 一步里几段文本的长度上限。**代码里截**，不只写在提示词里：
# 这些行要进每步都发的索引，一条长参数能把整块流水撑爆。
STEP_INTENT_MAX = 60
STEP_RESULT_MAX = 120


@dataclass
class Step:
    """一个主题里的一小步：一次模型往返加它调用的那些工具。

    只记**已经算好的事实**——工具名、参数、状态、产物 handle。不记 reasoning，
    也不让模型写自述：记录必须零请求，而自述会美化过程（「验证受体质量」比
    「读了一下文件」好听，但后者才是过程）。见 `docs/design.md` §4.3。

    `part_start` / `part_end` 是本步那几个 Part 在消息里的下标区间（开区间），
    Step 级召回（`recall_event evt_0003 7`）靠它切原文。
    """

    seq: int = 0                 # 主题内连号，1 起
    intent: str = ""             # 这一步干了什么，本地从工具名 + 关键参数拼
    tools: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    result: str = ""             # 成功 / 失败 + 首行，含产出的 handle
    artifacts: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    message_index: int = 0
    part_start: int = 0
    part_end: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。"""
        return {
            "seq": self.seq,
            "intent": self.intent,
            "tools": list(self.tools),
            "args": list(self.args),
            "result": self.result,
            "artifacts": list(self.artifacts),
            "issues": list(self.issues),
            "message_index": self.message_index,
            "part_start": self.part_start,
            "part_end": self.part_end,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        """从字典还原一步。字段类型不对时当空，不让一条坏记录毁掉整份状态。"""

        def strings(key: str) -> list[str]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [str(item) for item in raw if str(item).strip()]

        return cls(
            seq=int(data.get("seq") or 0),
            intent=str(data.get("intent") or ""),
            tools=strings("tools"),
            args=strings("args"),
            result=str(data.get("result") or ""),
            artifacts=strings("artifacts"),
            issues=strings("issues"),
            message_index=int(data.get("message_index") or 0),
            part_start=int(data.get("part_start") or 0),
            part_end=int(data.get("part_end") or 0),
        )


@dataclass
class Event:
    """一个主题的过程记录：一串 Step，加一句结论、一句交接。

    边界用**位置**表示：`cut_message` / `cut_part` 是本主题之后的第一个位置
    （左闭右开）。封存之后 RECENT 就从这里开始，它前面的原文不再发给模型。
    用位置而不用消息下标，是因为一条 assistant 消息装着一整轮的所有步，
    按消息切不出轮中途的主题边界；`Step.part_start` 与它同一套口径。

    事件**连续覆盖**：后一条的切点接着前一条的切点，中间没有缝。起点不用另存，
    它就是前一条的切点（首条从 `(0, 0)` 起）。断点由 `Session.sanitize_events`
    裁掉，保留前面能用的那一段。

    `actions` / `artifacts` / `issues` 不再单独存，从 `steps` 聚合出来——
    存两份必然漂移（这次会话里已经出现过同一件事两个数字对不上）。

    `step_start` / `step_end` 是会话内连号的步区间（1 起，左闭右开），
    只用于显示与召回时的指路；没有步的强制封存两者相等。
    """

    id: str = ""
    title: str = ""
    summary: str = ""
    handoff: str = ""        # 后续主题依赖什么，模型写；空串表示没写
    topic_index: int = 0
    steps: list[Step] = field(default_factory=list)
    cut_message: int = 0
    cut_part: int = 0
    step_start: int = 0
    step_end: int = 0
    tokens: int = 0          # 这一段原文的估算 token，预算用
    kind: str = "manual"     # manual / auto / local，见 EVENT_KINDS
    degraded: bool = False   # 标题与结论是本地拼的
    # 这一轮停下来时循环层给的原因（`EXIT_*`），空串表示模型自己说完了。
    # 主题的显示状态从它来：`cancelled` 画成中断，`error` / `tool_failed` 画成失败。
    # 不存这一项的话，取消之后照常封存，事后分不出「做完的」与「被停下的」。
    exit_reason: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def cut(self) -> tuple[int, int]:
        """本主题之后的第一段原文在哪。"""
        return (self.cut_message, self.cut_part)

    @property
    def status(self) -> str:
        """显示状态：`completed` / `failed` / `interrupted`。

        `exit_reason` 的取值来自循环层的 `EXIT_*`。没有停止原因时看最后一步
        有没有未决项：中间某步失败、后面换了做法做成了，读起来仍然是完成的，
        只有停在失败上才算这一段没有做成。
        """
        if self.exit_reason == "cancelled":
            return "interrupted"
        if self.exit_reason in ("error", "tool_failed"):
            return "failed"
        if self.steps and self.steps[-1].issues:
            return "failed"
        return "completed"

    @property
    def span(self) -> str:
        """步号区间的显示形式；没有步时是空串。"""
        if self.step_end <= self.step_start:
            return ""
        return f"第 {self.step_start}–{self.step_end - 1} 步"

    @property
    def actions(self) -> list[str]:
        """本主题用过的工具名，按首次出现的顺序去重。"""
        names: list[str] = []
        for step in self.steps:
            for name in step.tools:
                if name not in names:
                    names.append(name)
        return names

    @property
    def artifacts(self) -> list[str]:
        """本主题产出的 handle / 文件，按首次出现的顺序去重。"""
        found: list[str] = []
        for step in self.steps:
            for item in step.artifacts:
                if item not in found:
                    found.append(item)
        return found

    @property
    def issues(self) -> list[str]:
        """本主题失败项的首行，按首次出现的顺序去重。"""
        found: list[str] = []
        for step in self.steps:
            for item in step.issues:
                if item not in found:
                    found.append(item)
        return found

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 TOML 的字典。

        `actions` / `artifacts` / `issues` 是派生字段，不落盘。
        """
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "handoff": self.handoff,
            "topic_index": self.topic_index,
            "steps": [step.to_dict() for step in self.steps],
            "cut_message": self.cut_message,
            "cut_part": self.cut_part,
            "step_start": self.step_start,
            "step_end": self.step_end,
            "tokens": self.tokens,
            "kind": self.kind,
            "degraded": self.degraded,
            "exit_reason": self.exit_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """从字典还原事件。

        **两种旧形状也要读得回来**：

        - 旧版把 `actions` / `artifacts` / `issues` 平铺在事件上，没有 `steps`。
          那种事件被折成「只有一步的流水」——内容一个字不丢，只是粒度和新记录不同。
        - 更早的版本用 `start` / `end` 表示消息区间。折算成切点 `(end, 0)`：
          那条消息之后的位置，与新口径一致。
        """

        def strings(key: str) -> list[str]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [str(item) for item in raw if str(item).strip()]

        raw_steps = data.get("steps")
        if isinstance(raw_steps, list):
            steps = [
                Step.from_dict(item) for item in raw_steps if isinstance(item, dict)
            ]
        else:
            actions = strings("actions")
            artifacts = strings("artifacts")
            issues = strings("issues")
            steps = (
                [
                    Step(
                        seq=1,
                        intent="、".join(actions),
                        tools=actions,
                        artifacts=artifacts,
                        issues=issues,
                    )
                ]
                if (actions or artifacts or issues)
                else []
            )

        kind = str(data.get("kind") or "manual")
        if "cut_message" in data:
            cut_message = int(data.get("cut_message") or 0)
            cut_part = int(data.get("cut_part") or 0)
        else:
            cut_message = int(data.get("end") or 0)
            cut_part = 0
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            handoff=str(data.get("handoff") or ""),
            topic_index=int(data.get("topic_index") or data.get("phase_index") or 0),
            steps=steps,
            cut_message=cut_message,
            cut_part=cut_part,
            step_start=int(data.get("step_start") or 0),
            step_end=int(data.get("step_end") or 0),
            tokens=int(data.get("tokens") or 0),
            kind=kind if kind in EVENT_KINDS else "manual",
            degraded=bool(data.get("degraded")),
            exit_reason=str(data.get("exit_reason") or ""),
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
    # 事件列表：已经封存的主题。每条记着切点，RECENT 从最后一条的切点往后接。
    events: list[Event] = field(default_factory=list)
    event_seq: int = 0
    # 会话内连号的步计数器：事件上的 `step_start` / `step_end` 读它。
    # 落盘，重启之后新步不会重复用旧号。
    step_seq: int = 0
    # 当前这个主题已经走完、还没封存的步。**只在内存里**：每步落盘要重写整份
    # 状态文件，而丢几步是可以接受的（原文还在 RECENT 里，最坏是这一主题少几行流水）。
    pending_steps: list[Step] = field(default_factory=list)
    # 模型给当前主题起的名字、结论与交接（`close_topic` 调用时写下）。
    # 内核在步末取用并清空。
    topic_title: str = ""
    topic_summary: str = ""
    topic_handoff: str = ""
    # 已经用 load_tools 装入的领域工具族。核心工具不受它影响，
    # 装在会话上而不是注册表上：注册表按「根 / 子 agent」共享两份，
    # 记在那里会让一个会话装过的工具漏到另一个会话。
    tool_families: list[str] = field(default_factory=list)
    # 每一族是第几个主题装进来的。收敛（`toolset.prune_families`）用它判断
    # 「刚装、还没轮到用」的族，别刚装完就卸掉。
    family_topic: dict[str, int] = field(default_factory=dict)
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

    @property
    def has_pending_prompt(self) -> bool:
        """最后一条是用户消息、且这一轮还没跑：派发时「仅创建」留下的待发任务。

        不另存字段，按状态与消息形状推断：跑起来的第一步就会写一条 assistant
        消息，这个条件自然不再成立；重启读回来的状态也一样成立。
        """
        return (
            self.status == "idle"
            and bool(self.messages)
            and self.messages[-1].role == "user"
        )

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
    def event_cut(self) -> tuple[int, int]:
        """RECENT 的起点：最后一条事件的切点；没有事件时是会话开头。"""
        return self.events[-1].cut if self.events else (0, 0)

    @property
    def next_topic_index(self) -> int:
        """下一个主题号。取已有最大值 +1：事件被裁掉之后号也不会重复。"""
        return max((event.topic_index for event in self.events), default=0) + 1

    def sanitize_events(self) -> None:
        """丢掉切点不递增、或者越过会话末尾的事件。

        事件必须首尾相接：切点不递增就有两段原文既在事件里又在 RECENT 里，
        越过末尾则指向还不存在的内容。发现断点就丢掉断点之后的事件——
        保留前面能用的那一段。

        **不推 `compact_upto`**：那是「手动 / 强行压缩」留下的位置，与「主题归档到哪儿」
        是两件事，装配时取两者的较早者。
        """
        limit = len(self.messages)
        kept: list[Event] = []
        cursor = (self.summary_upto, 0)
        for event in self.events:
            position = event.cut
            if position <= cursor or position[0] > limit:
                break
            if position[0] == limit and position[1] > 0:
                break
            kept.append(event)
            cursor = position
        self.events = kept
        self.event_seq = max(self.event_seq, len(kept))

    def touch(self) -> None:
        """刷新更新时间戳。"""
        self.updated_at = time.time()

    def total_tokens(self) -> int:
        """返回会话内累计 token 数。"""
        return sum(message.total_tokens() for message in self.messages)

    def last_input_tokens(self) -> int:
        """返回最近一次请求实际带上的输入 token 数；还没发过请求时为 0。

        取最后一条有用量的消息：`tokens["input"]` 是服务端记账的 prompt token，
        也就是那一次请求发出去的整份上下文（含工具定义与缓存命中的部分）。
        累计值回答不了「窗口还剩多少」，这一份能。
        """
        for message in reversed(self.messages):
            sent = int(message.tokens.get("input", 0))
            if sent:
                return sent
        return 0

    def total_cached(self) -> int:
        """返回会话内累计的缓存命中输入 token 数。"""
        return sum(int(message.tokens.get("cached", 0)) for message in self.messages)

    def total_uncached(self) -> int:
        """返回会话内累计的缓存未命中输入 token 数。"""
        return sum(int(message.tokens.get("uncached", 0)) for message in self.messages)

    def total_output(self) -> int:
        """返回会话内累计的输出 token 数。"""
        return sum(int(message.tokens.get("output", 0)) for message in self.messages)

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
            "step_seq": self.step_seq,
            "tool_families": list(self.tool_families),
            "family_topic": dict(self.family_topic),
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
        session.step_seq = int(data.get("step_seq") or 0)
        for event in session.events:
            session.step_seq = max(session.step_seq, event.step_end - 1)
        raw_families = data.get("tool_families")
        if isinstance(raw_families, list):
            session.tool_families = [str(item) for item in raw_families if str(item)]
        raw_topics = data.get("family_topic", data.get("family_phase"))
        if isinstance(raw_topics, dict):
            session.family_topic = {
                str(key): int(value)
                for key, value in raw_topics.items()
                if str(key) and isinstance(value, int) and not isinstance(value, bool)
            }
        # 读回来就校验一次：手工改过状态文件、或者旧版本写了一半的事件，
        # 都在进装配之前被裁掉，而不是等到发请求时才露馅。
        session.sanitize_events()
        return session
