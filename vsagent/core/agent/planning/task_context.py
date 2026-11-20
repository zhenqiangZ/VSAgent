"""把这一轮的意图接到当前任务上。

回答两个问题：

1. 这一句与当前任务是什么关系？新建、延续、修改、比较、恢复、无关。
2. 这一轮是执行、查进度、问结果、回答澄清、取消还是闲聊？

任务关系决定任务号与版本号。对话行为决定要不要编译任务。两条轴分开之后，
“问上一轮结果”不会再被当成新任务，比较也不会把已有对象丢掉。

四条规则：

- `new` 不接当前任务；交给编译器新建任务号。
- `continue` 沿用任务号；新约束、新排除项、新输入并进去。出现新角色时版本加一，
  并重新编译；只改约束时版本也加一，但不重写子目标。
- `modify` 换目标或对象，版本加一；已有产物保留。
- `compare` 沿用任务号，版本加一，补上 `analysis`，输入指向两组已有对象。

合并一律**只增不减**：旧约束不会被这一句覆盖掉。
"""

from __future__ import annotations

from dataclasses import replace

from vsagent.core.agent.planning.decision import RECOMPILE_RELATIONS, RouteDecision
from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.roles.contracts import IntentSpec, TaskSpec

NON_EXECUTING_ACTIONS: frozenset[str] = frozenset(
    {"status_query", "ask_result", "clarify", "cancel", "chat"}
)
"""这些行为不编译任务。"""

CONTEXT_LIMIT = 400
"""喂回解析器的当前任务摘要上限（字符）。"""


class TaskContextResolver:
    """纯计算：不读会话、不写盘、不调模型。"""

    def resolve(
        self,
        intent: IntentSpec,
        task: TaskSpec | None = None,
        *,
        source_refs: tuple[str, ...] = (),
    ) -> RouteDecision:
        relation = intent.task_relation
        action = intent.conversation_action

        if action in NON_EXECUTING_ACTIONS:
            warnings: list[Note] = []
            if action == "cancel" and task is None:
                warnings.append(
                    Note(
                        "cancel_without_task",
                        "action",
                        "要取消任务，但当前没有正在进行的任务",
                    )
                )
            return RouteDecision(
                relation=relation,
                action=action,
                intent=intent,
                task=task,
                warnings=tuple(warnings),
                source_refs=source_refs,
            )

        if relation == "none":
            return RouteDecision(
                relation=relation,
                action=action,
                intent=intent,
                task=task,
                warnings=(
                    Note(
                        "no_task_goal",
                        "relation",
                        "这一轮没有任务目标，不编译任务规格",
                    ),
                ),
                source_refs=source_refs,
            )

        if relation == "new":
            return RouteDecision(
                relation=relation,
                action=action,
                intent=intent,
                task=None,
                source_refs=source_refs,
            )

        if task is None:
            return RouteDecision(
                relation=relation,
                action=action,
                intent=intent,
                task=None,
                warnings=(
                    Note(
                        "relation_without_task",
                        "relation",
                        f"标成 {relation}，但没有当前任务，按新任务处理",
                    ),
                ),
                source_refs=source_refs,
            )

        if relation == "continue":
            merged = self._merge(task, intent)
            known = self._agents(task)
            fresh = [agent for agent in intent.operations if agent not in known]
            if not fresh:
                return RouteDecision(
                    relation=relation,
                    action=action,
                    intent=intent,
                    task=merged,
                    source_refs=source_refs,
                )
            operations = self._ordered([*known, *intent.operations])
            return RouteDecision(
                relation=relation,
                action=action,
                intent=replace(intent, operations=operations),
                task=task,
                source_refs=source_refs,
                force_recompile=True,
            )

        if relation == "modify":
            return RouteDecision(
                relation=relation,
                action=action,
                intent=intent,
                task=task,
                source_refs=source_refs,
            )

        if relation == "compare":
            operations = self._ordered([*self._agents(task), *intent.operations, "analysis"])
            return RouteDecision(
                relation=relation,
                action=action,
                intent=replace(intent, operations=operations),
                task=task,
                source_refs=source_refs,
            )

        if relation == "resume":
            if intent.operations:
                return RouteDecision(
                    relation=relation,
                    action=action,
                    intent=replace(
                        intent,
                        operations=self._ordered([*self._agents(task), *intent.operations]),
                    ),
                    task=task,
                    source_refs=source_refs,
                    force_recompile=True,
                )
            return RouteDecision(
                relation=relation,
                action=action,
                intent=intent,
                task=task,
                source_refs=source_refs,
            )

        return RouteDecision(
            relation=relation,
            action=action,
            intent=intent,
            task=task,
            source_refs=source_refs,
        )

    @staticmethod
    def _agents(task: TaskSpec) -> list[str]:
        out: list[str] = []
        for row in task.subgoals:
            name = str(row.get("agent") or "").strip()
            if name and name not in out:
                out.append(name)
        return out

    @staticmethod
    def _ordered(names: list[str]) -> tuple[str, ...]:
        """按传入顺序去重。角色顺序由解析结果与当前任务决定，不再重排。"""
        out: list[str] = []
        for name in names:
            if name and name not in out:
                out.append(name)
        return tuple(out)

    @staticmethod
    def _merge(task: TaskSpec, intent: IntentSpec) -> TaskSpec:
        """把这一句新提的要求并进任务；没有变化就原样返回。"""
        scope = dict(task.research_scope)
        for item in intent.objects:
            scope[f"{item.type}:{item.label}"] = item.ref or item.label
        changes = {
            "constraints": list(task.constraints) + list(intent.constraints),
            "exclusions": list(task.exclusions) + list(intent.exclusions),
            "available_inputs": {**task.available_inputs, **intent.inputs},
            "research_scope": scope,
        }
        merged = task.with_updates(**changes)
        if merged.to_dict() == task.to_dict():
            return task
        return merged.with_updates(revision=task.revision + 1)


def context_line(task: TaskSpec | None) -> str:
    """给解析器看的当前任务摘要：目标、要求、禁止、状态，最多 400 字符。

    只带这几项，不带进度与历史。对象表由 `context_resolver` 另外拼。
    """
    if task is None:
        return ""
    parts = [f"任务：{task.task_id}（第 {task.revision} 版）"]
    parts.append(f"目标：{task.objective}")
    if task.constraints:
        parts.append("要求：" + "；".join(task.constraints[:5]))
    if task.exclusions:
        parts.append("禁止：" + "；".join(task.exclusions[:5]))
    parts.append(f"状态：{task.status}")
    return "；".join(parts)[:CONTEXT_LIMIT]
