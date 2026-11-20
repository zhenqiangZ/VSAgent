"""协调器：把一份能力计划变成可派发的任务，跑完再收成一份战役结果。

三段分工：

- `agent_router` 决定"这件事要哪些角色、谁等谁"（计划）；
- `projection` 把计划投影成每个角色的合同（`AgentTask`）；本模块只负责按计划
  调度、把结果收拢成战役；
- `scheduler` 按依赖把它们跑完。

**任务号必须可复现。** 用 `f"{plan.task_id}:{role}"` 而不是随机编号：同一份计划
重跑时，还在待发或在跑的子会话靠任务号被认出来，不会再开一个。已经跑完的角色
不再沿用旧会话：再派一次会带一代代号开新的子会话，用户补要求时不会把上一轮
的结论原样领回来。

**约束、交付物与子目标的分配都在 `projection.py`。** 这里曾经有一份同样的逻辑，
两份实现会让"任务书里的交付物"与"验收用的交付物"慢慢对不上。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from vsagent.core.agent.execution.projection import Projection, project
from vsagent.core.agent.execution.scheduler import AgentScheduler, Schedule
from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.roles.contracts import AgentPlan, AgentTask, TaskSpec
from vsagent.core.agent.roles.profiles import profile, role_label

__all__ = ["Campaign", "Coordinator", "build_tasks"]


@dataclass(frozen=True)
class Campaign:
    """一次多角色执行的完整结果。`schedule` 为 None 表示一个任务都没派。"""

    plan: AgentPlan
    tasks: tuple[AgentTask, ...]
    schedule: Schedule | None = None
    warnings: tuple[Note, ...] = ()
    pending: tuple[str, ...] = ()
    """计划里这次没派的角色。按子集派发时它们不会自己跑，必须说出来。"""

    def dispatched(self) -> int:
        return 0 if self.schedule is None else len(self.schedule.outcomes)

    def done(self) -> list[str]:
        return [] if self.schedule is None else self.schedule.done()

    def partial(self) -> list[str]:
        """跑完但报告不全的任务。与 `done` 有重叠：结束和合格是两件事。"""
        return [] if self.schedule is None else self.schedule.partial()

    def unfinished(self) -> list[str]:
        return [] if self.schedule is None else self.schedule.unfinished()

    def progress(self) -> tuple[list[str], list[str], list[str]]:
        """（已完成, 没来得及跑, 卡住）。写成能直接进任务状态的短句。

        用角色的中文名而不是 `pocket`：这一段是给用户看的进度，
        不是日志。名字取说明书标题冒号前那一段，与子会话标题一致。
        """
        if self.schedule is None:
            return [], [], []
        roles = {task.task_id: task.role for task in self.tasks}
        done: list[str] = []
        todo: list[str] = []
        blocked: list[str] = []
        for task_id, outcome in self.schedule.outcomes.items():
            line = f"{role_label(roles.get(task_id, task_id))}：{STATUS_WORDS.get(outcome.status, outcome.status)}"
            if outcome.status in ("done", "partial"):
                done.append(line)
            else:
                blocked.append(line)
        for task in self.tasks:
            if task.task_id not in self.schedule.outcomes:
                todo.append(f"{role_label(task.role)}：没来得及跑")
        return done, todo, blocked


STATUS_WORDS: dict[str, str] = {
    "done": "完成",
    "partial": "完成（报告不全）",
    "failed": "失败",
    "cancelled": "中断",
    "blocked": "卡住（上游失败）",
}


def build_tasks(
    plan: AgentPlan,
    spec: TaskSpec | None = None,
    *,
    roles: Iterable[str] | None = None,
    task: str = "",
    constraints: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[AgentTask], list[Note]]:
    """把计划翻成任务列表，返回（任务, 提示）。不派发，纯翻译。

    真正的分配在 `projection.project()`；这里保留这个入口，是因为评测脚本与
    判据按「任务 + 提示」两元组读它。
    """
    projection = project(
        plan,
        spec,
        roles=roles,
        task=task,
        constraints=constraints,
    )
    return projection.tasks(), list(projection.notes)


class Coordinator:
    """一份计划跑成一次战役。"""

    def __init__(self, scheduler: AgentScheduler) -> None:
        self.scheduler = scheduler

    def plan(
        self,
        plan: AgentPlan,
        spec: TaskSpec | None = None,
        *,
        roles: Iterable[str] | None = None,
        task: str = "",
        constraints: Mapping[str, Iterable[str]] | None = None,
    ) -> tuple[list[AgentTask], list[Note]]:
        return build_tasks(
            plan,
            spec,
            roles=roles,
            task=task,
            constraints=constraints,
        )

    def project(
        self,
        plan: AgentPlan,
        spec: TaskSpec | None = None,
        *,
        roles: Iterable[str] | None = None,
        task: str = "",
        constraints: Mapping[str, Iterable[str]] | None = None,
    ) -> Projection:
        return project(
            plan,
            spec,
            roles=roles,
            task=task,
            constraints=constraints,
        )

    async def run(
        self,
        parent: Any,
        plan: AgentPlan,
        spec: TaskSpec | None = None,
        *,
        timeout: float | None = None,
        role_timeouts: Mapping[str, float] | None = None,
        roles: Iterable[str] | None = None,
        task: str = "",
        constraints: Mapping[str, Iterable[str]] | None = None,
        on_dispatch: Callable[[AgentTask, Any], None] | None = None,
    ) -> Campaign:
        """翻译、调度、收拢。计划不成立时由调度器抛 `SchedulerError`，这里不吞。"""
        projection = project(
            plan,
            spec,
            roles=roles,
            task=task,
            constraints=constraints,
        )
        tasks = projection.tasks()
        warnings = list(projection.notes)
        pending = projection.pending
        if not tasks:
            return Campaign(
                plan=plan, tasks=(), warnings=tuple(warnings), pending=pending
            )
        budgets = dict(role_timeouts or {})
        if not budgets:
            # 按角色说明书给安全网：卡住的子会话到点中断，父会话能接着决定重派。
            for item in tasks:
                minutes = profile(item.role).timeout_minutes
                if minutes:
                    budgets[item.role] = float(minutes) * 60
        schedule = await self.scheduler.run(
            parent,
            tasks,
            timeout=timeout,
            role_timeouts=budgets,
            on_dispatch=on_dispatch,
        )
        return Campaign(
            plan=plan,
            tasks=tuple(tasks),
            schedule=schedule,
            warnings=tuple(warnings),
            pending=pending,
        )
