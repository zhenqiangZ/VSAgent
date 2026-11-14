"""协调器：把一份能力计划变成可派发的任务，跑完再收成一份战役结果。

三段分工：

- `capability_router` 决定"这件事要哪些角色、谁等谁"（计划）；
- 这里把计划翻译成 `AgentTask`：一个角色一份任务，任务号由计划号与角色拼出来，
  同一份计划重跑时任务号不变，派发器据此判重；
- `scheduler` 按依赖把它们跑完，这里再把结果收拢成 `Campaign`。

**任务号必须可复现。** 用 `f"{plan.task_id}:{role}"` 而不是随机编号：用户改了任务
目标之后计划号不变、`revision` 变，但角色没变的那几个任务不该重跑——任务号一致，
派发器就能认出"这个已经派过"。真要重跑，换计划号，而不是靠随机编号碰运气。

**交付物只给最后一环。** 模板里的 `required_outputs`（`CandidateSet` 之类）是整个
任务的结果，不属于中间的每一步。这里按"没有下游的角色"（汇点）分配；汇点不止一个
时说明计划本身有多个终点，硬分会把同一件交付物挂到两份任务上，于是不分配、只告警。

**子集要补闭包。** 调用方可以只要其中几个角色（例如这一轮只做口袋），但依赖边的
另一端必须一起带上，否则调度器会报"依赖不存在的任务"；汇点也在子集内重新算，
交付物跟着新的汇点走，不沿用整份计划的那一个。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .capabilities import capability as lookup_capability
from .contracts import AgentTask, CapabilityPlan, TaskSpec
from .profiles import profile
from .scheduler import AgentScheduler, Schedule
from .task_compiler import TEMPLATES


@dataclass(frozen=True)
class Campaign:
    """一次多角色执行的完整结果。`schedule` 为 None 表示一个任务都没派。"""

    plan: CapabilityPlan
    tasks: tuple[AgentTask, ...]
    schedule: Schedule | None = None
    warnings: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    """计划里这次没派的角色。按子集派发时它们不会自己跑，必须说出来。"""

    def dispatched(self) -> int:
        return 0 if self.schedule is None else len(self.schedule.outcomes)

    def done(self) -> list[str]:
        return [] if self.schedule is None else self.schedule.done()

    def unfinished(self) -> list[str]:
        return [] if self.schedule is None else self.schedule.unfinished()

    def progress(self) -> tuple[list[str], list[str], list[str]]:
        """（已完成, 没来得及跑, 卡住）。写成能直接进任务状态的短句。

        用角色的中文名而不是 `structure_pocket`：这一段是给用户看的进度，
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


def role_label(role: str) -> str:
    """角色的中文名。取不到就退回角色名，不让一条进度把整场战役弄崩。"""
    try:
        return profile(role).title.split("：")[0]
    except KeyError:
        return role


def task_id_for(plan: CapabilityPlan, role: str) -> str:
    """计划号 + 角色。同一份计划里角色不重复，所以任务号不会撞。"""
    return f"{plan.task_id}:{role}"


def _goals_for(plan: CapabilityPlan, spec: TaskSpec | None, role: str) -> list[str]:
    """这个角色负责的子目标。按能力标签对，不按文字猜。

    一个都找不到时（路由补出来的前置节点在任务规格里没有对应子目标），用模板里
    那句子目标兜底：子 agent 拿到的任务书里必须有一句能说明"这一段要做什么"，
    不能只有「完成 pocket_discovery 这一段」。
    """
    out: list[str] = []
    if spec is not None:
        for row in spec.subgoals:
            name = str(row.get("capability") or "")
            if not name:
                continue
            try:
                owner = lookup_capability(name).role
            except KeyError:
                continue
            goal = str(row.get("goal") or "").strip()
            if owner == role and goal and goal not in out:
                out.append(goal)
    return out or _template_goals(plan, role)


def _template_goals(plan: CapabilityPlan, role: str) -> list[str]:
    """模板里给这个角色的子目标，只取这次计划里选中了的能力。"""
    out: list[str] = []
    for capability in plan.selected_capabilities:
        for row in TEMPLATES.values():
            if str(row.get("capability") or "") != capability:
                continue
            goal = str(row.get("subgoal") or "").strip()
            try:
                owner = lookup_capability(capability).role
            except KeyError:
                continue
            if owner == role and goal and goal not in out:
                out.append(goal)
    return out


def _subset(plan: CapabilityPlan, roles: Iterable[str]) -> list[str]:
    """所给角色 + 它们的全部上游，按计划顺序返回。

    只跑下游会让调度器报「依赖不存在的任务」：依赖边指向的任务不在子集里。
    所以裁剪必须先补闭包，而不是简单的白名单。
    """
    wanted = {str(role) for role in roles}
    while True:
        extra = {
            str(row["from"])
            for row in plan.dependencies
            if str(row["to"]) in wanted
        } - wanted
        if not extra:
            break
        wanted |= extra
    return [agent for agent in plan.selected_agents if agent in wanted]


def build_tasks(
    plan: CapabilityPlan,
    spec: TaskSpec | None = None,
    *,
    roles: Iterable[str] | None = None,
    task: str = "",
) -> tuple[list[AgentTask], list[str]]:
    """把计划翻成任务列表，返回（任务, 告警）。不派发，纯翻译。

    `roles` 给了就只保留这些角色**以及它们的上游**；交付物只挂给子集内的终点。
    `task` 只替换**请求的那一个角色**的任务书：请求 `screening` 时上游 `ligand`
    是按依赖补进来的，它不该被下游的任务书顶掉。请求了几个角色就只报错，不猜。
    """
    warnings: list[str] = []
    if not plan.selected_agents:
        return [], ["计划里没有选中任何角色，没有可派发的任务"]

    requested = [str(item) for item in roles] if roles else list(plan.selected_agents)

    chosen = _subset(plan, roles) if roles else list(plan.selected_agents)
    if not chosen:
        return [], ["给定的角色都不在这份计划里，没有可派发的任务"]
    if roles and set(chosen) - set(roles):
        warnings.append(
            f"补上了上游角色：{'、'.join(sorted(set(chosen) - set(roles)))}"
        )
    upstream = {
        str(row["from"]) for row in plan.dependencies if str(row["to"]) in chosen
    }
    sinks = [role for role in chosen if role not in upstream]
    outputs: dict[str, tuple[str, ...]] = {}
    if spec is not None and spec.required_outputs:
        names = tuple(str(row["name"]) for row in spec.required_outputs)
        if len(sinks) == 1:
            outputs[sinks[0]] = names
        else:
            warnings.append(
                f"计划有 {len(sinks)} 个终点（{'、'.join(sinks)}），"
                f"交付物 {'、'.join(names)} 不知道该挂给谁，这次不下发"
            )

    constraints = tuple(spec.constraints) if spec is not None else ()
    context: dict[str, str] = {}
    if spec is not None:
        context["总目标"] = spec.objective
        if spec.exclusions:
            context["不做的事"] = "、".join(spec.exclusions)
    if plan.unresolved_inputs:
        context["缺的输入"] = "；".join(plan.unresolved_inputs)

    tasks: list[AgentTask] = []
    for role in chosen:
        goals = _goals_for(plan, spec, role)
        objective = "；".join(goals) or f"完成 {role} 这一段（任务 {plan.task_id}）"
        outputs_for_role = outputs.get(role, ())
        criteria = []
        if spec is not None and outputs_for_role:
            criteria = [
                dict(row)
                for row in spec.acceptance_criteria
                if str(row.get("output") or "") in outputs_for_role
            ]
        tasks.append(
            AgentTask(
                task_id=task_id_for(plan, role),
                role=role,
                objective=objective,
                campaign_id=plan.task_id,
                input_refs=dict(context),
                constraints=constraints,
                required_outputs=outputs_for_role,
                acceptance_criteria=criteria,
                dependencies=tuple(
                    task_id_for(plan, str(row["from"]))
                    for row in plan.dependencies
                    if str(row["to"]) == role and str(row["from"]) in chosen
                ),
                # 权限从角色说明书来，不在这里另写一份。以前这个字段一直是空元组，
                # `profile.check_task()` 的权限校验于是空转——它比的是空集与说明书，
                # 永远通过。填上之后，派发前那道校验才真的在判。
                permissions=profile(role).permissions,
            )
        )
    if task and len(requested) == 1:
        for item in tasks:
            if item.role == requested[0]:
                item.objective = task
                break
    return tasks, warnings


class Coordinator:
    """一份计划跑成一次战役。"""

    def __init__(self, scheduler: AgentScheduler) -> None:
        self.scheduler = scheduler

    def plan(
        self,
        plan: CapabilityPlan,
        spec: TaskSpec | None = None,
        *,
        roles: Iterable[str] | None = None,
        task: str = "",
    ) -> tuple[list[AgentTask], list[str]]:
        return build_tasks(plan, spec, roles=roles, task=task)

    async def run(
        self,
        parent: Any,
        plan: CapabilityPlan,
        spec: TaskSpec | None = None,
        *,
        timeout: float | None = None,
        role_timeouts: Mapping[str, float] | None = None,
        roles: Iterable[str] | None = None,
        task: str = "",
    ) -> Campaign:
        """翻译、调度、收拢。计划不成立时由调度器抛 `SchedulerError`，这里不吞。"""
        tasks, warnings = build_tasks(plan, spec, roles=roles, task=task)
        dispatched = {item.role for item in tasks}
        pending = tuple(role for role in plan.selected_agents if role not in dispatched)
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
            families=plan.tool_families,
            timeout=timeout,
            role_timeouts=budgets,
        )
        return Campaign(
            plan=plan,
            tasks=tuple(tasks),
            schedule=schedule,
            warnings=tuple(warnings),
            pending=pending,
        )
