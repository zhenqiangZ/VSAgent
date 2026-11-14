"""把这一轮的意图接到当前任务上。

回答一个问题：**这一句是新任务，还是刚才那件事的一部分？** 答案决定要不要重新
编译任务规格。判断来自 `IntentSpec.turn_mode`（方案 B：与意图同一次调用产出，
不额外问一次模型），本模块只做接线与合并。

四条规则：

- `new_task` / `general_chat` 不接当前任务；前者交给 Phase 4 编译，后者什么都不建。
- `continue_task` / `ask_about_result` 复用当前任务，但**这一句里新提的约束与禁止
  项要并进去**——用户常常在追问里顺带加一句"别用 AD4"，那不是新任务。
- `modify_task` 改目标：目标换成这一句，约束与禁止项累加，`revision` 加一。
- 没有当前任务却要"继续"：不报错，退回新任务并记一条警告。让人重说一遍，
  比悄悄建一份空任务好。

合并一律**只增不减**：旧约束不会被这一句覆盖掉。要撤销，得用户明说——
那属于新的任务规格，不是合并。
"""

from __future__ import annotations

from dataclasses import dataclass

from vsagent.core.agent.roles.contracts import IntentSpec, TaskSpec

RECOMPILE_MODES: frozenset[str] = frozenset({"new_task", "modify_task"})
"""这两种轮次要重新编译任务规格；其余复用当前那一份。"""

CONTEXT_LIMIT = 400
"""喂回解析器的当前任务摘要上限（字符）。它每次解析都要发，不能长。"""


@dataclass(frozen=True)
class TurnContext:
    """这一轮要接在哪份任务上，以及接的时候发生了什么。"""

    mode: str
    intent: IntentSpec
    task: TaskSpec | None = None
    warnings: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    @property
    def recompile(self) -> bool:
        """要不要让 Phase 4 重新编译任务规格。"""
        return self.mode in RECOMPILE_MODES


class TaskContextResolver:
    """纯计算：不读会话、不写盘、不调模型。"""

    def resolve(
        self,
        intent: IntentSpec,
        task: TaskSpec | None = None,
        *,
        source_refs: tuple[str, ...] = (),
    ) -> TurnContext:
        mode = intent.turn_mode
        if mode in ("new_task", "general_chat"):
            return TurnContext(mode=mode, intent=intent)

        if task is None:
            # 要"继续"却没有任务：退回新任务。用户不必知道这里发生了降级，
            # 但 trace 里要留得下，所以写进 warnings。
            return TurnContext(
                mode="new_task",
                intent=intent,
                warnings=(f"标成 {mode}，但没有正在进行的任务，按新任务处理",),
            )

        if mode == "modify_task":
            return TurnContext(
                mode=mode,
                intent=intent,
                task=self._merge(task, intent, replace_objective=True),
                source_refs=source_refs,
            )

        if mode == "resume_historical_task":
            return TurnContext(
                mode=mode,
                intent=intent,
                task=self._merge(task, intent),
                warnings=() if source_refs else ("恢复旧任务但没有给出处，按当前任务继续",),
                source_refs=source_refs,
            )

        return TurnContext(
            mode=mode,
            intent=intent,
            task=self._merge(task, intent),
            source_refs=source_refs,
        )

    @staticmethod
    def _merge(task: TaskSpec, intent: IntentSpec, *, replace_objective: bool = False) -> TaskSpec:
        """把这一句新提的要求并进任务；没有变化就原样返回。"""
        scope = dict(task.research_scope)
        for key, value in intent.research_objects.items():
            scope[key] = value
        changes = {
            "constraints": list(task.constraints) + list(intent.explicit_constraints),
            "exclusions": list(task.exclusions) + list(intent.exclusions),
            "research_scope": scope,
        }
        if replace_objective and intent.primary_goal:
            changes["objective"] = intent.primary_goal
        merged = task.with_updates(**changes)
        if merged.to_dict() == task.to_dict():
            return task
        return merged.with_updates(revision=task.revision + 1)


def context_line(task: TaskSpec | None) -> str:
    """给解析器看的当前任务摘要：目标、要求、禁止、状态，最多 400 字符。

    只带这几项，不带进度与历史：解析器要判断的是"这句话和当前任务什么关系"，
    进度由任务状态自己记着，历史由装配器按需给。
    """
    if task is None:
        return ""
    parts = [f"目标：{task.objective}"]
    if task.constraints:
        parts.append("要求：" + "；".join(task.constraints[:5]))
    if task.exclusions:
        parts.append("禁止：" + "；".join(task.exclusions[:5]))
    parts.append(f"状态：{task.status}")
    return "；".join(parts)[:CONTEXT_LIMIT]
