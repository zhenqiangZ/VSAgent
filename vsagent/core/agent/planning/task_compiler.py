"""Task Specification Compiler：把 `IntentSpec` 编译成 `TaskSpec`。

子目标与验收条件由意图模型写；编译器只做四件事：

1. 按操作取角色，去掉与禁止项冲突的那些；
2. 从角色模板取交付物与确认点；
3. 过滤、合并并去重模型写的子目标与验收条件；
4. 检查每件交付物都有验收条件，缺口记一条警告。

模板只保留不随用户改口变化的资产清单。模型漏写子目标时当场报错，不拿模板
步骤兜底：任务书里的目标必须来自这一轮对用户要求的理解。

未覆盖的操作**不静默降级**：记一条警告，让调用方与 trace 都看得见。

排除项的处理有两条：原样继承；以及**删掉与它冲突的角色**——说了“不要对接”，
计划里就不该有对接。
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

        subgoals, missing = self._subgoals(intent, covered, current)
        if missing:
            raise CompileError(
                "这些操作没有子目标，先让意图模型补齐再编译："
                + "、".join(missing)
            )
        if not subgoals:
            raise CompileError(f"这些操作没有可执行子目标：{'、'.join(covered) or '无'}")

        outputs, approvals = self._assets(covered)
        checks, check_notes = self._checks(intent, outputs, current)
        warnings.extend(check_notes)
        missing_outputs = [
            name
            for name in outputs
            if name not in {str(row.get("output") or "") for row in checks}
        ]
        if missing_outputs:
            warnings.append(
                Note(
                    "acceptance_uncovered",
                    "acceptance_criteria",
                    f"这些交付物没有验收条件：{'、'.join(missing_outputs)}",
                )
            )

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
    def _subgoals(
        intent: IntentSpec,
        roles: list[str],
        current: TaskSpec | None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """模型写的子目标。延续与恢复任务补上当前规格里的旧步骤。"""
        merge = (
            current is not None
            and intent.task_relation in ("continue", "resume")
        )
        out: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()

        def add(row: dict[str, object]) -> None:
            agent = str(row.get("agent") or "").strip()
            goal = str(row.get("goal") or "").strip()
            if agent not in roles or not goal:
                return
            key = (agent, goal)
            if key in seen:
                return
            seen.add(key)
            item: dict[str, object] = {"agent": agent, "goal": goal}
            stage = str(row.get("stage") or "").strip()
            if stage:
                item["stage"] = stage
            out.append(item)

        if merge:
            for row in current.subgoals:
                add(row)
        for row in intent.subgoals:
            add(row)
        covered = {str(row["agent"]) for row in out}
        missing = [role for role in roles if role not in covered]
        return out, missing

    @staticmethod
    def _assets(roles: list[str]) -> tuple[list[str], list[str]]:
        """从模板取交付物与确认点。"""
        outputs: list[str] = []
        approvals: list[str] = []
        for role in roles:
            template = template_for(role)
            for name in template.required_outputs:
                if name not in outputs:
                    outputs.append(name)
            for name in template.approval_points:
                if name not in approvals:
                    approvals.append(name)
        approvals.sort(
            key=lambda name: APPROVAL_ORDER.index(name)
            if name in APPROVAL_ORDER
            else 99
        )
        return outputs, approvals

    @staticmethod
    def _checks(
        intent: IntentSpec,
        outputs: list[str],
        current: TaskSpec | None,
    ) -> tuple[list[dict[str, object]], list[Note]]:
        """模型写的验收条件。延续与恢复任务补上当前规格里的旧条件。"""
        merge = (
            current is not None
            and intent.task_relation in ("continue", "resume")
        )
        out: list[dict[str, object]] = []
        notes: list[Note] = []
        seen: set[tuple[str, str]] = set()

        def add(row: dict[str, object]) -> None:
            kind = str(row.get("kind") or "").strip()
            target = str(row.get("target") or "").strip()
            output = str(row.get("output") or "").strip()
            if not kind or not target:
                return
            if output not in outputs:
                notes.append(
                    Note(
                        "acceptance_output_unknown",
                        "acceptance_criteria",
                        f"验收条件 {kind}：{target} 指向的交付物 {output or '（空）'} 不在本次清单里，已丢弃",
                    )
                )
                return
            key = (kind, target)
            if key in seen:
                return
            seen.add(key)
            out.append({"kind": kind, "target": target, "output": output})

        if merge:
            for row in current.acceptance_criteria:
                add(row)
        for row in intent.acceptance_criteria:
            add(row)
        return out, notes
