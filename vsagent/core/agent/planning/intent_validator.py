"""意图验证：在编译之前把明显不成立、指代不清或与禁止项冲突的意图拦下来。

**纯规则，不调用模型。** 规则能查“结构是否齐全、引用是否存在、操作是否冲突”，
查不了“是否真的理解了用户的科学问题”。真正的语义复核需要第二次模型调用，
默认不做。

四种结果：

- `accept`：继续编译与路由。
- `clarify`：有歧义或关键信息缺失，先问用户。
- `update`：解析有效，但必须先修改已有任务规格再执行。
- `reject`：请求无效，或当前条件下做不了。
"""

from __future__ import annotations

from dataclasses import dataclass

from vsagent.core.agent.planning.context_resolver import ContextBundle
from vsagent.core.agent.roles import catalog
from vsagent.core.agent.roles.contracts import IntentSpec, TaskSpec
from vsagent.core.agent.roles.templates import TEMPLATES, template_for

PRONOUNS: tuple[str, ...] = (
    "它",
    "这个",
    "那个",
    "之前",
    "刚才",
    "第一个",
    "第二个",
    "第三个",
    "上一个",
    "下一个",
)
"""这些说法没有明确引用时，说明指代还没解开。"""

CONFIRM_OPERATIONS: frozenset[str] = frozenset({"prepare", "docking", "affinity"})
"""这些操作会改共享状态或花较长时间，计划里应当有确认点。"""


@dataclass(frozen=True)
class Reason:
    code: str
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


@dataclass(frozen=True)
class Verdict:
    decision: str
    reasons: tuple[Reason, ...] = ()
    clarification: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.decision in ("clarify", "reject")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reasons": [item.to_dict() for item in self.reasons],
            "clarification": list(self.clarification),
        }


class IntentValidator:
    """规则验证器。同一份输入两次验证结果一致。"""

    def validate(
        self,
        intent: IntentSpec,
        bundle: ContextBundle | None = None,
        *,
        current: TaskSpec | None = None,
    ) -> Verdict:
        reasons: list[Reason] = []
        questions: list[str] = []

        self._shape(intent, reasons, questions)
        self._references(intent, bundle, reasons, questions)
        self._completeness(intent, bundle, current, reasons, questions)
        self._consistency(intent, reasons)
        self._executability(intent, reasons)
        self._authorization(intent, reasons)

        if any(item.code == "unknown_operation" for item in reasons):
            return Verdict("reject", tuple(reasons), tuple(questions))
        if questions:
            return Verdict("clarify", tuple(reasons), tuple(dict.fromkeys(questions)))
        if intent.task_relation in ("modify", "compare") and current is not None:
            return Verdict("update", tuple(reasons), ())
        return Verdict("accept", tuple(reasons), ())

    @staticmethod
    def _shape(
        intent: IntentSpec, reasons: list[Reason], questions: list[str]
    ) -> None:
        if not intent.goal.strip():
            reasons.append(Reason("empty_goal", "goal", "没有说清要做什么"))
        if intent.conversation_action == "execute" and intent.task_relation in (
            "new",
            "modify",
            "compare",
        ) and not intent.operations:
            questions.append("这次要交给哪个子 Agent 做？")
        if intent.conversation_action in ("status_query", "ask_result", "cancel", "chat"):
            if intent.operations:
                reasons.append(
                    Reason(
                        "operation_in_query",
                        "operations",
                        f"{intent.conversation_action} 这一轮不该选操作",
                    )
                )
        if intent.task_relation == "compare" and len(intent.objects) < 2:
            questions.append("要比较哪两组对象？请指出两个已有对象。")

    @staticmethod
    def _references(
        intent: IntentSpec,
        bundle: ContextBundle | None,
        reasons: list[Reason],
        questions: list[str],
    ) -> None:
        if bundle is None:
            return
        for index, item in enumerate(intent.objects):
            if item.origin in ("existing", "current"):
                if item.ref not in bundle.refs:
                    questions.append(
                        f"{item.label} 指向的 {item.ref} 在已有对象里找不到，"
                        "请确认是哪一个。"
                    )
                continue
            if not item.ref and any(word in item.label for word in PRONOUNS):
                reasons.append(
                    Reason(
                        "unresolved_reference",
                        f"objects[{index}]",
                        f"{item.label} 还没有落到具体引用",
                    )
                )
                questions.append(f"{item.label} 指的是哪一个已有对象？")

    @staticmethod
    def _completeness(
        intent: IntentSpec,
        bundle: ContextBundle | None,
        current: TaskSpec | None,
        reasons: list[Reason],
        questions: list[str],
    ) -> None:
        if intent.conversation_action != "execute":
            return
        if intent.task_relation in ("continue", "resume") and current is not None:
            return
        has_anchor = bool(intent.objects or intent.inputs)
        if bundle is not None and (bundle.refs or bundle.artifacts):
            has_anchor = True
        if not has_anchor:
            if "visualization" in intent.operations:
                questions.append("要展示哪些已有对象或产物？")
            elif "analysis" in intent.operations:
                questions.append("要分析或比较哪一组已有结果？")
            else:
                questions.append("要研究哪个靶点、结构或对象？")
            return
        if "analysis" in intent.operations and not _has_result(intent, bundle, current):
            questions.append("要分析或比较哪一组已有结果？")
        if "visualization" in intent.operations and not _has_artifact(bundle, current):
            questions.append("要展示哪些已有对象或产物？")

    @staticmethod
    def _consistency(intent: IntentSpec, reasons: list[Reason]) -> None:
        denied = denied_roles(intent.exclusions)
        for operation in intent.operations:
            if operation in denied:
                reasons.append(
                    Reason(
                        "excluded_operation",
                        "operations",
                        f"用户明确禁止 {operation}，编译时会把它去掉",
                    )
                )

    @staticmethod
    def _executability(intent: IntentSpec, reasons: list[Reason]) -> None:
        for operation in intent.operations:
            if operation not in catalog.AGENTS:
                reasons.append(
                    Reason("unknown_operation", "operations", f"没有这个角色：{operation}")
                )
                continue
            if operation not in TEMPLATES:
                reasons.append(
                    Reason("missing_template", "operations", f"{operation} 没有编译模板")
                )

    @staticmethod
    def _authorization(intent: IntentSpec, reasons: list[Reason]) -> None:
        for operation in intent.operations:
            if operation not in CONFIRM_OPERATIONS:
                continue
            approvals = template_for(operation).approval_points
            if not approvals:
                reasons.append(
                    Reason(
                        "missing_approval",
                        "operations",
                        f"{operation} 需要用户确认，但模板里没有确认点",
                    )
                )


def denied_roles(exclusions: tuple[str, ...]) -> set[str]:
    """用户的禁止项对应哪些角色。"""
    out: set[str] = set()
    for words, roles in catalog.EXCLUSION_RULES:
        if any(word in item for item in exclusions for word in words):
            out.update(roles)
    return out


def _has_result(
    intent: IntentSpec, bundle: ContextBundle | None, current: TaskSpec | None
) -> bool:
    upstream = {"docking", "affinity", "analysis", "mechanism"}
    if upstream & set(intent.operations):
        return True
    if current is not None:
        for row in current.subgoals:
            if str(row.get("agent") or "") in upstream:
                return True
        if current.required_outputs:
            return True
    if bundle is not None and (bundle.refs or bundle.artifacts):
        return True
    return False


def _has_artifact(bundle: ContextBundle | None, current: TaskSpec | None) -> bool:
    if bundle is not None and (bundle.artifacts or bundle.refs):
        return True
    if current is not None and current.required_outputs:
        return True
    return False
