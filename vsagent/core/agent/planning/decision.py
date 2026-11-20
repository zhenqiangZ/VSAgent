"""这一轮怎么走：意图、任务关系与最终计划放在同一个对象里。

以前有两个几乎一样的对象：`TurnContext`（意图接到哪个任务上）和 `RouteDecision`
（这一轮要不要执行、算出了什么计划）。字段重合八成，调用方要在两个之间来回抄。
现在合成一个：`TaskContextResolver` 只填前一半，`RouteAdapter` 再补上模式、验证
结论与计划，中间全程用 `dataclasses.replace()`。

`Verdict` 只在类型检查时导入：`intent_validator → context_resolver → task_context
→ decision → intent_validator` 会成环。运行时这个字段只存对象，不解析注解。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.roles.contracts import AgentPlan, IntentSpec, TaskSpec

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查
    from vsagent.core.agent.planning.intent_validator import Verdict

RECOMPILE_RELATIONS: frozenset[str] = frozenset({"new", "modify", "compare"})
"""这些关系一定要重新编译任务规格。"""


@dataclass(frozen=True)
class RouteDecision:
    """一轮路由的全部结果。前半段由 `TaskContextResolver` 填，后半段由路由补。"""

    mode: str = ""
    relation: str = ""
    action: str = ""
    intent: IntentSpec | None = None
    task: TaskSpec | None = None
    plan: AgentPlan | None = None
    verdict: "Verdict | None" = None
    clarification: tuple[str, ...] = ()
    warnings: tuple[Note, ...] = ()
    error: str = ""
    source_refs: tuple[str, ...] = ()
    force_recompile: bool = False

    @property
    def usable(self) -> bool:
        """有没有解析出可用的意图。"""
        return not self.error and self.intent is not None

    @property
    def recompile(self) -> bool:
        """要不要让编译器重新生成任务规格。"""
        if self.action != "execute":
            return False
        return self.relation in RECOMPILE_RELATIONS or self.force_recompile
