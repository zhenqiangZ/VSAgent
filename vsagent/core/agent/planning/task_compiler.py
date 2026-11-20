"""Task Specification Compiler：把 `IntentSpec` 编译成 `TaskSpec`。

**纯规则，不调用模型。** 理由不是“规则更聪明”，而是三条可验证的：

1. 操作只有九个角色，模板数量可控。
2. `TaskSpec` 的验收条件必须机器可查（交付校验按它判）。模板天然产出
   `kind` + `target`，模型天然产出句子。
3. 合同要稳定：同一份意图编译两次必须完全一致。

未覆盖的操作**不静默降级**：记一条警告，让调用方与 trace 都看得见。

排除项的处理有两条：原样继承；以及**删掉与它冲突的角色**——说了“不要对接”，
计划里就不该有对接。

模板本身在 `roles/templates.py`，这里只负责“意图 → 合同”的翻译。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.roles.catalog import EXCLUSION_RULES, OPERATIONS
from vsagent.core.agent.roles.contracts import IntentSpec, TaskSpec
from vsagent.core.agent.roles.templates import APPROVAL_ORDER, TEMPLATES, template_for

EVIDENCE_REQUIREMENT = "每条结论都要能追溯到结构、文献或计算产物（handle、编号或路径）"

MINIMAL_TASK_ID_LENGTH = 8


class CompileError(RuntimeError):
    """这份意图不该被编译成任务。"""


@dataclass(frozen=True)
class Compilation:
    """编译结果：任务本身，加上过程信息。"""

    task: TaskSpec
    warnings: tuple[Note, ...] = ()
    covered: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()


def default_task_id(intent: IntentSpec) -> str:
    """由意图内容派生任务号，保证同一份意图两次编译得到同一个号。"""
    body = json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
    return f"task_{digest[:MINIMAL_TASK_ID_LENGTH]}"


class TaskCompiler:
    """把意图编译成任务合同。无状态，可重复调用。"""

    def compile(
        self,
        intent: IntentSpec,
        *,
        current: TaskSpec | None = None,
        task_id: str = "",
        campaign_id: str = "",
    ) -> Compilation:
        if intent.needs_clarification:
            raise CompileError("意图还没解析完（需要澄清），先问用户再编译")
        if intent.conversation_action != "execute":
            raise CompileError(f"{intent.conversation_action} 这一轮不编译任务")
        if intent.task_relation == "none":
            raise CompileError("与任务无关的这一轮不编译")
        if intent.task_relation == "continue" and not intent.operations:
            raise CompileError("只补约束不需要重新编译：沿用当前任务规格即可")

        kinds, dropped = self._kinds(intent)
        warnings: list[Note] = []
        if dropped:
            warnings.append(
                Note(
                    "role_excluded",
                    "operations",
                    f"这些角色与用户的禁止项冲突，已从计划里去掉：{'、'.join(dropped)}",
                )
            )
        if not kinds:
            raise CompileError("没有可执行的角色：用户的要求与禁止项互相冲突")

        covered = [agent for agent in kinds if agent in TEMPLATES]
        uncovered = [agent for agent in kinds if agent not in TEMPLATES]
        if uncovered:
            warnings.append(
                Note(
                    "role_without_template",
                    "operations",
                    f"这些角色没有专用模板，未纳入计划：{'、'.join(uncovered)}",
                )
            )

        subgoals, outputs, checks, approvals = self._merge(covered)
        if not subgoals:
            raise CompileError(f"这些操作没有可用模板：{'、'.join(covered) or '无'}")

        objective = self._objective(intent, current)
        constraints = list(intent.constraints)
        exclusions = list(intent.exclusions)
        available_inputs = dict(intent.inputs)
        scope = {self._object_key(item): item.ref or item.label for item in intent.objects}
        revision = 0
        status = "ready"
        identifier = task_id or default_task_id(intent)
        campaign = campaign_id

        if current is not None and intent.task_relation != "new":
            identifier = current.task_id
            campaign = current.campaign_id or campaign
            revision = current.revision + 1
            status = current.status if current.status in ("running", "blocked") else "ready"
            constraints = list(current.constraints) + constraints
            exclusions = list(current.exclusions) + exclusions
            available_inputs = {**current.available_inputs, **available_inputs}
            scope = {**current.research_scope, **scope}

        task = TaskSpec(
            task_id=identifier,
            campaign_id=campaign,
            objective=objective,
            research_scope=scope,
            subgoals=subgoals,
            constraints=constraints,
            exclusions=exclusions,
            available_inputs=available_inputs,
            required_outputs=outputs,
            evidence_requirements=(EVIDENCE_REQUIREMENT,),
            acceptance_criteria=checks,
            approval_points=approvals,
            status=status,
            revision=revision,
        )
        return Compilation(
            task=task,
            warnings=tuple(warnings),
            covered=tuple(covered),
            uncovered=tuple(uncovered),
        )

    @staticmethod
    def _objective(intent: IntentSpec, current: TaskSpec | None) -> str:
        if current is not None and intent.task_relation in ("continue", "resume"):
            return current.objective
        return intent.goal

    @staticmethod
    def _object_key(item: object) -> str:
        kind = str(getattr(item, "type", "object"))
        label = str(getattr(item, "label", ""))
        return f"{kind}:{label}"

    @staticmethod
    def _kinds(intent: IntentSpec) -> tuple[list[str], list[str]]:
        """按角色顺序取操作，并去掉与禁止项冲突的那些。"""
        excluded: set[str] = set()
        for words, roles in EXCLUSION_RULES:
            if any(word in item for item in intent.exclusions for word in words):
                excluded.update(roles)
        ordered = [agent for agent in OPERATIONS if agent in intent.operations]
        kept = [agent for agent in ordered if agent not in excluded]
        dropped = [agent for agent in ordered if agent in excluded]
        return kept, dropped

    @staticmethod
    def _merge(
        roles: list[str],
    ) -> tuple[
        list[dict[str, object]],
        list[str],
        list[dict[str, object]],
        list[str],
    ]:
        """合并模板：子目标按文本去重，交付物按名字去重，验收条件按 (kind,target) 去重。"""
        subgoals: list[dict[str, object]] = []
        outputs: list[str] = []
        checks: list[dict[str, object]] = []
        approvals: list[str] = []
        seen_subgoals: set[str] = set()
        seen_outputs: set[str] = set()
        seen_checks: set[tuple[str, str]] = set()
        seen_approvals: set[str] = set()

        for role in roles:
            template = template_for(role)
            for step in template.subgoals:
                if step.goal in seen_subgoals:
                    continue
                row: dict[str, object] = {"goal": step.goal, "agent": role or "research"}
                if step.stage:
                    row["stage"] = step.stage
                subgoals.append(row)
                seen_subgoals.add(step.goal)
            for name in template.required_outputs:
                if name not in seen_outputs:
                    outputs.append(name)
                    seen_outputs.add(name)
            for criterion in template.acceptance_criteria:
                key = (criterion.kind, criterion.target)
                if key in seen_checks:
                    continue
                seen_checks.add(key)
                checks.append(
                    {
                        "kind": criterion.kind,
                        "target": criterion.target,
                        "output": criterion.output,
                    }
                )
            for name in template.approval_points:
                if name not in seen_approvals:
                    approvals.append(name)
                    seen_approvals.add(name)

        approvals.sort(
            key=lambda name: APPROVAL_ORDER.index(name)
            if name in APPROVAL_ORDER
            else 99
        )
        return subgoals, outputs, checks, approvals
