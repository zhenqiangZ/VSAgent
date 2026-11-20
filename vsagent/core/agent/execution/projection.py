"""TaskSpec → AgentTask 的唯一投影。

计划里选好角色之后，这一步回答四件事：

- 每个角色认领哪几件交付物；
- 父 agent 在派发时给每个角色写了哪几条限制；
- 每个角色负责哪几句子目标；
- 哪些验收条件属于它认领的交付物。

**这是 `AgentTask(...)` 唯一的构造处。** 以前这四件事散在 `coordinator.py`
的几个函数里，每件都自己读一遍模板、自己定一种去重规则；现在模板只在
`RoleSlice` 构造时读一次，`coordinator.build_tasks` 只是 `project()` 的薄封装。

限制只有两个来源：角色说明书里的固定限制，与调用方按角色给的那一份
（`{"pocket": [...], "global": [...]}`）。不再从父会话的任务状态或任务规格里
按前缀切句子——「这条要求该给谁」只有派发的那一刻、看着子任务才判得准。

中间结构 `RoleSlice` 不落盘、不下发：它是投影的中间结果，真正的运输合同仍是
`AgentTask`。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.roles.contracts import AgentPlan, AgentTask, TaskSpec
from vsagent.core.agent.roles.profiles import profile
from vsagent.core.agent.roles.templates import template_for

# 写给所有角色的那一格。派发工具也收「全局」，归一化之后用这个键。
GLOBAL_CONSTRAINT = "global"


def task_id_for(plan: AgentPlan, role: str) -> str:
    """计划号 + 角色。同一份计划里角色不重复，所以任务号不会撞。"""
    return f"{plan.task_id}:{role}"


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    """去重并保留第一次出现的位置。空串不留下。"""
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _items(value: Any) -> Iterable[str]:
    """一格限制的取值。只写了一句也收，不把字符串拆成一个个字。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return value


def role_constraints(
    written: Mapping[str, Iterable[str]] | None,
    roles: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """把父 agent 按角色写的限制并进各角色的说明书限制，返回每个角色一份。

    合并顺序是说明书固定限制、`global`、这个角色自己的；同一句只留一份。
    没写就是没有：不回退去读父会话的任务状态，也不按前缀从任务规格里猜。
    """
    table = written or {}
    global_items = _unique(_items(table.get(GLOBAL_CONSTRAINT)))
    return {
        role: _unique(
            [
                *template_for(role).constraints,
                *global_items,
                *_items(table.get(role)),
            ]
        )
        for role in dict.fromkeys(str(role) for role in roles)
    }


def _claim_outputs(
    spec: TaskSpec | None, chosen: list[str], sinks: list[str]
) -> tuple[dict[str, tuple[str, ...]], list[Note]]:
    """按角色模板的交付物认领，返回（每个角色一份, 提示）。"""
    if spec is None:
        return {}, []
    owners: dict[str, list[str]] = {}
    for role in chosen:
        for name in template_for(role).required_outputs:
            owners.setdefault(name, []).append(role)

    claimed: dict[str, list[str]] = {role: [] for role in chosen}
    notes: list[Note] = []
    for name in spec.required_outputs:
        name = str(name).strip()
        if not name:
            continue
        holders = owners.get(name) or []
        if holders:
            for role in holders:
                if name not in claimed[role]:
                    claimed[role].append(name)
            continue
        if len(sinks) == 1:
            if name not in claimed[sinks[0]]:
                claimed[sinks[0]].append(name)
            continue
        notes.append(
            Note(
                "output_unclaimed",
                "required_outputs",
                f"交付物 {name} 没有角色在模板里认领，计划有 {len(sinks)} 个终点"
                f"（{'、'.join(sinks)}），这次不下发",
            )
        )
    return {role: tuple(names) for role, names in claimed.items()}, notes


def _goals_for(spec: TaskSpec | None, role: str) -> list[str]:
    """这个角色负责的子目标。按 `agent` 字段对，不按文字猜。

    一个都找不到时（路由补出来的前置节点在任务规格里没有对应子目标），用模板里的
    步骤兜底：子 agent 拿到的任务书里必须有一句能说明“这一段要做什么”。
    """
    out: list[str] = []
    if spec is not None:
        for row in spec.subgoals:
            if str(row.get("agent") or "") != role:
                continue
            goal = str(row.get("goal") or "").strip()
            if goal and goal not in out:
                out.append(goal)
    return out or _template_goals(role)


def _template_goals(role: str) -> list[str]:
    """模板里给这个角色的步骤目标。"""
    out: list[str] = []
    for step in template_for(role).subgoals:
        if step.goal and step.goal not in out:
            out.append(step.goal)
    return out


def select_roles(plan: AgentPlan, roles: Iterable[str]) -> list[str]:
    """所给角色 + 它们的全部上游，按计划顺序返回。

    只跑下游会让调度器报「依赖不存在的任务」：依赖边指向的任务不在子集里。
    所以裁剪必须先补闭包，而不是简单的白名单。派发前校验 `constraints` 的键
    也要用这一份：自动补上来的上游同样会跑，它也该有地方放自己的限制。
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


@dataclass(frozen=True)
class RoleSlice:
    """一个角色的切片：它要做什么、看到什么、交什么、等谁。"""

    task_id: str
    campaign_id: str
    role: str
    goals: tuple[str, ...]
    objective: str
    input_refs: dict[str, str]
    constraints: tuple[str, ...]
    required_outputs: tuple[str, ...]
    acceptance_criteria: tuple[dict[str, Any], ...]
    dependencies: tuple[str, ...]
    background: str = ""

    def to_task(self) -> AgentTask:
        """转成下发的合同。全仓唯一的 `AgentTask(...)` 构造点。"""
        return AgentTask(
            task_id=self.task_id,
            role=self.role,
            objective=self.objective,
            background=self.background,
            campaign_id=self.campaign_id,
            input_refs=dict(self.input_refs),
            constraints=self.constraints,
            required_outputs=self.required_outputs,
            acceptance_criteria=[dict(row) for row in self.acceptance_criteria],
            dependencies=self.dependencies,
            # 权限从角色说明书来，不在这里另写一份。以前这个字段一直是空元组，
            # `profile.check_task()` 的权限校验于是空转——它比的是空集与说明书，
            # 永远通过。填上之后，派发前那道校验才真的在判。
            permissions=profile(self.role).permissions,
        )


@dataclass(frozen=True)
class Projection:
    """一次投影的结果。"""

    plan: AgentPlan
    slices: tuple[RoleSlice, ...] = ()
    notes: tuple[Note, ...] = ()
    pending: tuple[str, ...] = ()
    """计划里有、这次没派的角色。按子集派发时它们不会自己跑，必须说出来。"""

    def tasks(self) -> list[AgentTask]:
        return [item.to_task() for item in self.slices]

    def slice_for(self, role: str) -> RoleSlice | None:
        for item in self.slices:
            if item.role == role:
                return item
        return None


def project(
    plan: AgentPlan,
    spec: TaskSpec | None = None,
    *,
    roles: Iterable[str] | None = None,
    task: str = "",
    constraints: Mapping[str, Iterable[str]] | None = None,
) -> Projection:
    """把计划投影成每个角色的切片。不派发，纯计算。

    `roles` 给了就只保留这些角色**以及它们的上游**；交付物只挂给子集内的角色。
    `task` 只替换**请求的那一个角色**的任务书：请求 `docking` 时上游 `prepare`
    是按依赖补进来的，它不该被下游的任务书顶掉。请求了几个角色就只报错，不猜。

    `constraints` 是父 agent 在派发时按角色写的那一份：键是角色名或 `global`。
    没写的角色只带说明书里的固定限制。`spec.constraints` 不参与：那是父会话里
    用户的原话，不是这一段子任务的交付要求。
    """
    if not plan.selected_agents:
        return Projection(
            plan=plan,
            notes=(
                Note(
                    "no_agents",
                    "plan",
                    "计划里没有选中任何角色，没有可派发的任务",
                ),
            ),
        )

    requested = [str(item) for item in roles] if roles else list(plan.selected_agents)
    chosen = select_roles(plan, roles) if roles else list(plan.selected_agents)
    if not chosen:
        return Projection(
            plan=plan,
            notes=(
                Note(
                    "no_role_selected",
                    "roles",
                    "给定的角色都不在这份计划里，没有可派发的任务",
                ),
            ),
            pending=tuple(plan.selected_agents),
        )

    notes: list[Note] = []
    if roles and set(chosen) - set(requested):
        notes.append(
            Note(
                "upstream_role_added",
                "roles",
                f"补上了上游角色：{'、'.join(sorted(set(chosen) - set(requested)))}",
            )
        )

    upstream = {
        str(row["from"]) for row in plan.dependencies if str(row["to"]) in chosen
    }
    sinks = [role for role in chosen if role not in upstream]
    outputs, output_notes = _claim_outputs(spec, chosen, sinks)
    notes.extend(output_notes)

    written = constraints or {}
    for name in sorted(set(written) - {GLOBAL_CONSTRAINT} - set(chosen)):
        # 工具层已经拦过；直接调 project() 的脚本在这里也能看见自己写错了一格。
        notes.append(
            Note(
                "constraint_unknown_role",
                "constraints",
                f"限制写给了这次不派的角色 {name}，没有下发",
            )
        )
    per_role = role_constraints(written, chosen)

    context: dict[str, str] = {}
    if spec is not None and spec.exclusions:
        context["不做的事"] = "、".join(spec.exclusions)
    if plan.unresolved_inputs:
        context["缺的输入"] = "；".join(plan.unresolved_inputs)

    slices: list[RoleSlice] = []
    for role in chosen:
        goals = _goals_for(spec, role)
        duty = "；".join(goals) or f"完成 {role} 这一段（任务 {plan.task_id}）"
        # 本角色的目标只写它这一段要做什么。父目标进 `background`，
        # 不放在第一行——否则子 agent 会把父任务当成自己的交付物再做一遍。
        objective = duty
        background = spec.objective if spec is not None else ""
        outputs_for_role = outputs.get(role, ())
        criteria: tuple[dict[str, Any], ...] = ()
        if spec is not None and outputs_for_role:
            criteria = tuple(
                dict(row)
                for row in spec.acceptance_criteria
                if str(row.get("output") or "") in outputs_for_role
            )
        slices.append(
            RoleSlice(
                task_id=task_id_for(plan, role),
                campaign_id=plan.task_id,
                role=role,
                goals=tuple(goals),
                objective=objective,
                background=background,
                input_refs=dict(context),
                constraints=per_role.get(role, ()),
                required_outputs=outputs_for_role,
                acceptance_criteria=criteria,
                dependencies=tuple(
                    task_id_for(plan, str(row["from"]))
                    for row in plan.dependencies
                    if str(row["to"]) == role and str(row["from"]) in chosen
                ),
            )
        )

    if task and len(requested) == 1:
        for index, item in enumerate(slices):
            if item.role == requested[0]:
                slices[index] = replace(item, objective=task)
                break

    pending = tuple(
        role for role in plan.selected_agents if role not in set(chosen)
    )
    return Projection(
        plan=plan,
        slices=tuple(slices),
        notes=tuple(notes),
        pending=pending,
    )
