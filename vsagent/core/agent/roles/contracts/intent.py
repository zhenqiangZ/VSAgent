"""IntentSpec：用户这一句话要做什么，以及它与当前任务是什么关系。

五个维度：

- `goal`：用户最终想解决什么问题。
- `objects`：涉及哪些科研对象，尽量带已有引用。
- `operations`：要哪些子 Agent，取值就是九个角色名。
- `constraints` / `exclusions`：用户明确提出的要求与禁止。
- `expected_outputs`：用户希望拿到什么。

两条与任务相关的轴：

- `task_relation`：新建、延续、修改、比较、恢复、无关。
- `conversation_action`：执行、查进度、问结果、回答澄清、取消、闲聊。

三条边界：

- `constraints` 只放用户明确说过的要求；推断出来的假设进 `uncertainties`。
- `exclusions` 是强约束，不能在编译阶段被“优化”掉。
- `missing_information` 记录还缺什么；`clarification_required` 只放不回答就无法开始的问题。
  发现类任务缺结构编号、口袋编号不算阻塞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from vsagent.core.agent.roles.catalog import AGENTS
from vsagent.core.agent.roles.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    choice,
    check_version,
    mapping,
    number,
    records,
    reject_unknown,
    text,
    texts,
)

OBJECT_TYPES: tuple[str, ...] = (
    "target",
    "structure",
    "pocket",
    "ligand",
    "pose",
    "score",
    "artifact",
    "hypothesis",
)
"""科研对象的类型。"""

OBJECT_ORIGINS: tuple[str, ...] = ("existing", "new", "current")
"""对象来源：已有引用、本轮新提、当前任务正在使用的对象。"""

TASK_RELATIONS: tuple[str, ...] = (
    "new",
    "continue",
    "modify",
    "compare",
    "resume",
    "none",
)
"""这一句与当前任务的关系。"""

CONVERSATION_ACTIONS: tuple[str, ...] = (
    "execute",
    "status_query",
    "ask_result",
    "clarify",
    "cancel",
    "chat",
)
"""这一轮的对话行为。"""

NON_EXECUTING_ACTIONS: frozenset[str] = frozenset(
    {"status_query", "ask_result", "cancel", "chat"}
)
"""这些行为不选操作，也不编译任务。"""

OPERATIONS: tuple[str, ...] = AGENTS
"""操作的取值就是九个子 Agent。"""


@dataclass(frozen=True)
class ObjectRef:
    """用户说到的一个科研对象。"""

    type: str
    label: str
    ref: str = ""
    origin: str = "new"

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", choice(self.type, OBJECT_TYPES, "ObjectRef.type"))
        object.__setattr__(self, "label", text(self.label, "ObjectRef.label"))
        object.__setattr__(self, "origin", choice(self.origin, OBJECT_ORIGINS, "ObjectRef.origin"))
        reference = text(self.ref, "ObjectRef.ref", allow_empty=True)
        if self.origin != "new" and not reference:
            raise ContractError(
                f"ObjectRef({self.type}, {self.label}) 标成了 {self.origin}，"
                "却没有写已有的 ref"
            )
        object.__setattr__(self, "ref", reference)

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "label": self.label,
            "ref": self.ref,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ObjectRef":
        row = mapping(data, "ObjectRef")
        reject_unknown(row, {"type", "label", "ref", "origin"}, name="ObjectRef")
        return cls(
            type=row.get("type"),
            label=row.get("label"),
            ref=row.get("ref", ""),
            origin=row.get("origin", "new"),
        )


@dataclass(frozen=True)
class IntentSpec:
    """一轮解析的结果。"""

    goal: str
    intent_id: str = ""
    task_relation: str = "new"
    conversation_action: str = "execute"
    objects: tuple[ObjectRef, ...] = ()
    operations: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    clarification_required: tuple[str, ...] = ()
    confidence: float = 0.0
    uncertainties: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        goal = text(self.goal, "IntentSpec.goal")
        relation = choice(self.task_relation, TASK_RELATIONS, "IntentSpec.task_relation")
        action = choice(
            self.conversation_action, CONVERSATION_ACTIONS, "IntentSpec.conversation_action"
        )
        kinds: list[str] = []
        for name in texts(self.operations, "IntentSpec.operations"):
            word = choice(name, OPERATIONS, "IntentSpec.operations")
            if word not in kinds:
                kinds.append(word)

        objects: list[ObjectRef] = []
        raw_objects = self.objects
        if raw_objects is None:
            raw_objects = ()
        if isinstance(raw_objects, (str, bytes)) or not isinstance(raw_objects, (list, tuple)):
            raise ContractError(
                "IntentSpec.objects 应当是对象数组，"
                f"收到 {type(raw_objects).__name__}"
            )
        for index, row in enumerate(raw_objects):
            if isinstance(row, ObjectRef):
                item = row
            elif isinstance(row, Mapping):
                item = ObjectRef.from_dict(row)
            else:
                raise ContractError(
                    f"IntentSpec.objects[{index}] 应当是对象，收到 {type(row).__name__}"
                )
            if item not in objects:
                objects.append(item)

        questions = texts(self.clarification_required, "IntentSpec.clarification_required")

        if action in NON_EXECUTING_ACTIONS and kinds:
            raise ContractError(
                f"{action} 这一轮不选操作，operations 应当为空，收到 {'、'.join(kinds)}"
            )
        if action == "chat" and relation != "none":
            raise ContractError("闲聊的 task_relation 应当是 none")
        if action == "execute" and relation == "none":
            raise ContractError("要执行任务时 task_relation 不能是 none")
        if (
            action == "execute"
            and relation in ("new", "modify", "compare")
            and not kinds
            and not questions
        ):
            raise ContractError(
                f"{relation} 任务至少要选一个操作；解析不出来就留空并写 clarification_required"
            )

        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "intent_id", text(self.intent_id, "IntentSpec.intent_id", allow_empty=True))
        object.__setattr__(self, "task_relation", relation)
        object.__setattr__(self, "conversation_action", action)
        object.__setattr__(self, "objects", tuple(objects))
        object.__setattr__(self, "operations", tuple(kinds))
        object.__setattr__(
            self, "constraints", texts(self.constraints, "IntentSpec.constraints")
        )
        object.__setattr__(self, "exclusions", texts(self.exclusions, "IntentSpec.exclusions"))
        object.__setattr__(self, "inputs", mapping(self.inputs, "IntentSpec.inputs"))
        object.__setattr__(
            self,
            "expected_outputs",
            texts(self.expected_outputs, "IntentSpec.expected_outputs"),
        )
        object.__setattr__(
            self,
            "missing_information",
            texts(self.missing_information, "IntentSpec.missing_information"),
        )
        object.__setattr__(
            self,
            "clarification_required",
            questions,
        )
        object.__setattr__(
            self, "confidence", number(self.confidence, "IntentSpec.confidence", low=0.0, high=1.0)
        )
        object.__setattr__(self, "uncertainties", texts(self.uncertainties, "IntentSpec.uncertainties"))
        object.__setattr__(
            self,
            "source_message_ids",
            texts(self.source_message_ids, "IntentSpec.source_message_ids"),
        )
        object.__setattr__(
            self, "schema_version", check_version(self.schema_version, name="IntentSpec")
        )

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification_required)

    @property
    def is_execution(self) -> bool:
        return self.conversation_action == "execute"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "task_relation": self.task_relation,
            "conversation_action": self.conversation_action,
            "goal": self.goal,
            "objects": [item.to_dict() for item in self.objects],
            "operations": list(self.operations),
            "constraints": list(self.constraints),
            "exclusions": list(self.exclusions),
            "inputs": dict(self.inputs),
            "expected_outputs": list(self.expected_outputs),
            "missing_information": list(self.missing_information),
            "clarification_required": list(self.clarification_required),
            "confidence": self.confidence,
            "uncertainties": list(self.uncertainties),
            "source_message_ids": list(self.source_message_ids),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "IntentSpec":
        reject_unknown(
            mapping(data, "IntentSpec"),
            {
                "schema_version",
                "intent_id",
                "task_relation",
                "conversation_action",
                "goal",
                "objects",
                "operations",
                "constraints",
                "exclusions",
                "inputs",
                "expected_outputs",
                "missing_information",
                "clarification_required",
                "confidence",
                "uncertainties",
                "source_message_ids",
            },
            name="IntentSpec",
        )
        return cls(
            goal=data.get("goal"),
            intent_id=data.get("intent_id", ""),
            task_relation=data.get("task_relation", "new"),
            conversation_action=data.get("conversation_action", "execute"),
            objects=tuple(
                ObjectRef.from_dict(row) for row in records(data.get("objects"), "IntentSpec.objects")
            ),
            operations=tuple(data.get("operations") or ()),
            constraints=tuple(data.get("constraints") or ()),
            exclusions=tuple(data.get("exclusions") or ()),
            inputs=data.get("inputs") or {},
            expected_outputs=tuple(data.get("expected_outputs") or ()),
            missing_information=tuple(data.get("missing_information") or ()),
            clarification_required=tuple(data.get("clarification_required") or ()),
            confidence=data.get("confidence", 0.0),
            uncertainties=tuple(data.get("uncertainties") or ()),
            source_message_ids=tuple(data.get("source_message_ids") or ()),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
