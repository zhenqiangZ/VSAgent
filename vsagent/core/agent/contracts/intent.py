"""IntentSpec：用户这一句话想做什么，以及系统还缺什么。

它与 `intent.py` 的关键词提示是两件事：那里回答"该查哪几类来源"，这里回答
"这是一个什么科研任务、哪些要求是用户明确说的、哪些不许做"。

三条边界：

- `explicit_constraints` 只放用户**明确说过**的要求；推断出来的假设进
  `uncertainties`。两者混在一起，后面就没有办法区分"用户要求"与"我们以为"。
- `exclusions` 是强约束：它进任务规格、进路由计划、进执行检查，不能在编译
  阶段被"优化"掉。
- 解析不出来就写 `needs_clarification`，不要凑一个看起来完整的意图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import (
    SCHEMA_VERSION,
    ContractError,
    choice,
    check_version,
    mapping,
    number,
    reject_unknown,
    text,
    texts,
)

INTENT_TYPES: tuple[str, ...] = (
    "target_inquiry",
    "structure_discovery",
    "pocket_discovery",
    "pocket_profiling",
    "ligand_discovery",
    "virtual_screening",
    "scoring_evaluation",
    "mechanism_analysis",
    "literature_evidence",
    "hypothesis_validation",
    "multi_objective",
    "task_continuation",
    "general_conversation",
    "needs_clarification",
)
"""意图类型。**可以多选**：一句话同时要结构与口袋时，两个都要出现。"""

TURN_MODES: tuple[str, ...] = (
    "new_task",
    "continue_task",
    "modify_task",
    "ask_about_result",
    "resume_historical_task",
    "general_chat",
)
"""这一句与当前任务的关系。

与 `intent_types` 是两条正交的轴：前者说"要做什么"，这里说"和刚才那件事什么关系"。
分开的用处只有一个——决定要不要重新编译任务规格：只有 `new_task` 与 `modify_task`
需要，其余复用当前任务。
"""


@dataclass(frozen=True)
class IntentSpec:
    """一轮解析的结果。"""

    primary_goal: str
    intent_types: tuple[str, ...] = ()
    turn_mode: str = "new_task"
    research_objects: dict[str, Any] = field(default_factory=dict)
    explicit_constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    provided_inputs: dict[str, Any] = field(default_factory=dict)
    missing_information: tuple[str, ...] = ()
    clarification_required: tuple[str, ...] = ()
    expected_deliverables: tuple[str, ...] = ()
    confidence: float = 0.0
    uncertainties: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        goal = text(self.primary_goal, "IntentSpec.primary_goal")
        kinds: list[str] = []
        for kind in texts(self.intent_types, "IntentSpec.intent_types"):
            kinds.append(choice(kind, INTENT_TYPES, "IntentSpec.intent_types"))
        if not kinds:
            raise ContractError(
                "IntentSpec.intent_types 至少要有一个：解析不出类型时用 "
                "needs_clarification，而不是留空"
            )
        if "needs_clarification" in kinds and not self.clarification_required:
            raise ContractError(
                "标了 needs_clarification 就要写出要问什么（clarification_required 不能为空）；"
                "没有问题要问说明这句已经解析完了"
            )
        object.__setattr__(self, "primary_goal", goal)
        object.__setattr__(self, "intent_types", tuple(kinds))
        object.__setattr__(
            self, "turn_mode", choice(self.turn_mode, TURN_MODES, "IntentSpec.turn_mode")
        )
        object.__setattr__(
            self, "research_objects", mapping(self.research_objects, "IntentSpec.research_objects")
        )
        object.__setattr__(
            self, "provided_inputs", mapping(self.provided_inputs, "IntentSpec.provided_inputs")
        )
        object.__setattr__(
            self,
            "explicit_constraints",
            texts(self.explicit_constraints, "IntentSpec.explicit_constraints"),
        )
        object.__setattr__(self, "exclusions", texts(self.exclusions, "IntentSpec.exclusions"))
        object.__setattr__(
            self, "missing_information", texts(self.missing_information, "IntentSpec.missing_information")
        )
        object.__setattr__(
            self,
            "clarification_required",
            texts(self.clarification_required, "IntentSpec.clarification_required"),
        )
        object.__setattr__(
            self,
            "expected_deliverables",
            texts(self.expected_deliverables, "IntentSpec.expected_deliverables"),
        )
        object.__setattr__(self, "uncertainties", texts(self.uncertainties, "IntentSpec.uncertainties"))
        object.__setattr__(
            self,
            "source_message_ids",
            texts(self.source_message_ids, "IntentSpec.source_message_ids"),
        )
        object.__setattr__(
            self,
            "confidence",
            number(self.confidence, "IntentSpec.confidence", low=0.0, high=1.0),
        )
        object.__setattr__(
            self, "schema_version", check_version(self.schema_version, name="IntentSpec")
        )

    @property
    def needs_clarification(self) -> bool:
        return "needs_clarification" in self.intent_types

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "primary_goal": self.primary_goal,
            "intent_types": list(self.intent_types),
            "turn_mode": self.turn_mode,
            "research_objects": dict(self.research_objects),
            "explicit_constraints": list(self.explicit_constraints),
            "exclusions": list(self.exclusions),
            "provided_inputs": dict(self.provided_inputs),
            "missing_information": list(self.missing_information),
            "clarification_required": list(self.clarification_required),
            "expected_deliverables": list(self.expected_deliverables),
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
                "primary_goal",
                "intent_types",
                "turn_mode",
                "research_objects",
                "explicit_constraints",
                "exclusions",
                "provided_inputs",
                "missing_information",
                "clarification_required",
                "expected_deliverables",
                "confidence",
                "uncertainties",
                "source_message_ids",
            },
            name="IntentSpec",
        )
        return cls(
            primary_goal=data.get("primary_goal"),
            intent_types=tuple(data.get("intent_types") or ()),
            turn_mode=data.get("turn_mode", "new_task"),
            research_objects=data.get("research_objects") or {},
            explicit_constraints=tuple(data.get("explicit_constraints") or ()),
            exclusions=tuple(data.get("exclusions") or ()),
            provided_inputs=data.get("provided_inputs") or {},
            missing_information=tuple(data.get("missing_information") or ()),
            clarification_required=tuple(data.get("clarification_required") or ()),
            expected_deliverables=tuple(data.get("expected_deliverables") or ()),
            confidence=data.get("confidence", 0.0),
            uncertainties=tuple(data.get("uncertainties") or ()),
            source_message_ids=tuple(data.get("source_message_ids") or ()),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
