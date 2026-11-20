"""Agent Router：把任务规格切成角色与依赖。

输入是 `TaskSpec`，它的每个子目标上带着 `agent`（编译器写的角色名）。
**不做文字匹配**：模板生成子目标时就知道它归谁，让它顺手记下来，比事后拿中文
去猜可靠。

三条规则值得单独说：

- **角色定工具。** 路由只回答「这一步归谁」，不决定装哪些工具：
  每个角色的清单在会话建立时由 `agents.toml` 定稿，运行当中不变。
- **前置分两类。** `analysis` 读已有结果，没有硬前置；`prepare` 没有口袋、没有配体，
  连输入都没有，缺了就把上游补成计划里的节点（`AUTO_PREREQUISITES`）。
- **依赖必须无环。** 环意味着调度器永远启动不了任何节点，这里当场拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.roles import catalog
from vsagent.core.agent.roles.contracts import AgentPlan, TaskSpec
from vsagent.core.agent.roles.templates import APPROVAL_ORDER, TEMPLATES, template_for

PREREQUISITES: dict[str, tuple[str, ...]] = {
    "research": (),
    # `pocket` 自己就会取结构，结构发现与口袋画像在同一个会话里先后做。
    "pocket": (),
    "ligand": (),
    "prepare": ("pocket", "ligand"),
    "docking": ("prepare",),
    "affinity": ("prepare",),
    # `analysis` 读已有结果；至少有一份上游产物由验证器的完整性检查负责。
    "analysis": (),
    "mechanism": ("pocket", "ligand", "docking"),
    "visualization": (),
}
"""角色之间的前置关系。

**只写真正缺产物的情况**：下游角色没有能产出上游产物的工具，才算前置。
`pocket` 不依赖 `research`——它的名单里就有取结构的工具；`mechanism` 依赖 `ligand`，
因为它的名单里没有 ChEMBL 那一组工具，自己拿不到已知活性；它依赖 `docking`，因为
`analyze_pose` 与 `contact_map` 只收对接产物的 handle。
"""

SINK_ROLES: tuple[str, ...] = ("visualization",)
"""必须排在同计划其他角色之后的展示角色。"""

AUTO_PREREQUISITES: dict[str, tuple[str, ...]] = {
    # `prepare` 要有口袋才能算盒子；`docking` 要有准备好的受体；`affinity` 沿同一条链。
    "prepare": ("pocket", "ligand"),
    "docking": ("prepare",),
    "affinity": ("prepare",),
}
"""缺了就必须现做的前置角色：补成计划里的节点，不留成告警。"""


class RouterError(RuntimeError):
    """计划自身不成立（环、角色不认识等）。"""


def _provided_roles(inputs: Any) -> set[str]:
    """用户已经给出的输入足以让哪些角色整段不必再跑。"""
    given = {str(key) for key in (inputs or {})}
    out: set[str] = set()
    for role, keys in catalog.ROLE_PROVIDED_INPUTS.items():
        if keys and all(key in given for key in keys):
            out.add(role)
    return out


def missing_prerequisites(
    plan: AgentPlan, roles: Iterable[str] | None = None
) -> list[str]:
    """这些角色缺哪些前置角色。

    `roles` 给定时只看这些角色：只派上游角色（例如只做配体盘点）时，计划里另一个
    分支缺前置不该拦这一次。**这是「限制执行」的判据**——`dispatch_campaign`
    在这里判，不靠 `unresolved_inputs` 那句告警。
    """
    chosen = {str(role) for role in roles} if roles is not None else set(plan.selected_agents)
    available = set(plan.selected_agents) | set(plan.provided_agents)
    out: list[str] = []
    for role in plan.selected_agents:
        if role not in chosen:
            continue
        lack = [item for item in PREREQUISITES.get(role, ()) if item not in available]
        if lack:
            out.append(f"{role} 需要 {'、'.join(lack)} 的产物")
    return out


def validate_prerequisites() -> list[str]:
    """核对角色前置表与模板。返回问题清单，空列表表示通过。"""
    problems: list[str] = list(catalog.validate())
    known = set(catalog.AGENTS)

    for role in sorted(known - set(PREREQUISITES)):
        problems.append(f"{role} 没有前置条目：空也要写一行")
    for role in sorted(set(PREREQUISITES) - known):
        problems.append(f"前置表里有不存在的角色：{role}")
    for role, needs in PREREQUISITES.items():
        for needed in needs:
            if needed not in known:
                problems.append(f"{role} 的前置不存在：{needed}")
            elif needed == role:
                problems.append(f"{role} 依赖自己")
    for role, needs in AUTO_PREREQUISITES.items():
        if role not in known:
            problems.append(f"自动补前置里有不存在的角色：{role}")
        extra = set(needs) - set(PREREQUISITES.get(role, ()))
        if extra:
            problems.append(
                f"{role} 自动补的前置不在前置表里：{'、'.join(sorted(extra))}"
            )
    for role in known:
        if role not in TEMPLATES:
            problems.append(f"{role} 没有编译模板")
            continue
        template = template_for(role)
        for step in template.subgoals:
            if step.stage and step.stage not in catalog.STAGES:
                problems.append(f"{role} 的模板阶段不认识：{step.stage}")
        if not template.required_outputs:
            problems.append(f"{role} 的模板没有交付物")
    for role in catalog.ROLE_PROVIDED_INPUTS:
        if role not in known:
            problems.append(f"自带输入表里有不存在的角色：{role}")

    problems.extend(_prerequisite_cycles(known))
    return problems


def _prerequisite_cycles(known: set[str]) -> list[str]:
    """Kahn 拓扑：剩下的节点就是环里的。"""
    waiting = {
        name: {item for item in PREREQUISITES.get(name, ()) if item in known}
        for name in known
    }
    seen = 0
    while True:
        layer = [name for name, items in waiting.items() if not items]
        if not layer:
            break
        for name in layer:
            waiting.pop(name, None)
            seen += 1
        for items in waiting.values():
            items.difference_update(layer)
    if seen == len(known):
        return []
    return [f"前置成环：{'、'.join(sorted(waiting))}"]


@dataclass(frozen=True)
class Routing:
    """路由结果：计划本身，加上过程里的提示。"""

    plan: AgentPlan
    warnings: tuple[Note, ...] = ()


class AgentRouter:
    """无状态；同一份任务规格两次路由结果一致。"""

    def route(self, task: TaskSpec) -> Routing:
        warnings: list[Note] = []
        agents, unknown = self._agents(task)
        if unknown:
            warnings.append(
                Note(
                    "unknown_agent",
                    "subgoals",
                    f"这些子目标的角色不认识，已跳过：{'、'.join(unknown)}",
                )
            )
        if not agents:
            warnings.append(
                Note("no_agents", "subgoals", "任务里没有可路由的角色，计划为空")
            )

        denied = self._denied(task)
        agents = [name for name in agents if name not in denied]
        provided = _provided_roles(task.available_inputs)
        agents, auto, skipped = self._close(agents, denied, provided)
        if auto:
            warnings.append(
                Note(
                    "prerequisite_added",
                    "selected_agents",
                    f"补上了缺的前置角色：{'、'.join(auto)}（不补这一步，下游没有输入）",
                )
            )
        warnings.extend(
            Note("provided_input_skipped", "available_inputs", item)
            for item in skipped
        )

        plan, unresolved = self._build(
            task.task_id, task.revision, agents, denied, provided
        )
        warnings.extend(
            Note("missing_prerequisite", "dependencies", item)
            for item in unresolved
        )
        return Routing(plan=plan, warnings=tuple(warnings))

    def _build(
        self,
        task_id: str,
        revision: int,
        agents: list[str],
        denied: Iterable[str],
        provided: Iterable[str],
    ) -> tuple[AgentPlan, list[str]]:
        dependencies, unresolved = self._dependencies(agents, provided)
        groups = self._parallel(agents, dependencies)
        rationale = tuple(
            f"{agent}：{'、'.join(catalog.DUTIES.get(agent, ()))}" for agent in agents
        )
        plan = AgentPlan(
            task_id=task_id,
            revision=revision,
            selected_agents=tuple(agents),
            denied_agents=tuple(denied),
            provided_agents=tuple(provided),
            dependencies=dependencies,
            parallel_groups=groups,
            unresolved_inputs=tuple(unresolved),
            approval_points=tuple(self._approvals(agents)),
            rationale=rationale,
        )
        return plan, unresolved

    @staticmethod
    def _agents(task: TaskSpec) -> tuple[list[str], list[str]]:
        picked: list[str] = []
        unknown: list[str] = []
        for row in task.subgoals:
            name = str(row.get("agent") or "").strip()
            if not name:
                continue
            if name not in catalog.AGENTS:
                if name not in unknown:
                    unknown.append(name)
                continue
            if name not in picked:
                picked.append(name)
        return picked, unknown

    @staticmethod
    def _close(
        roles: list[str],
        denied: Iterable[str],
        provided_roles: Iterable[str] = (),
    ) -> tuple[list[str], list[str], list[str]]:
        """把 `AUTO_PREREQUISITES` 里的前置角色补进来。"""
        out: list[str] = []
        added: list[str] = []
        skipped: list[str] = []
        blocked = {str(item) for item in denied}
        ready = {str(item) for item in provided_roles}

        def visit(role: str) -> None:
            if role in out:
                return
            for needed in AUTO_PREREQUISITES.get(role, ()):
                if needed in blocked:
                    continue
                if needed in ready:
                    note = f"{needed} 的产物已给出，不再补 {needed}"
                    if note not in skipped:
                        skipped.append(note)
                    continue
                fresh = needed not in roles and needed not in added
                visit(needed)
                if fresh:
                    added.append(needed)
            out.append(role)

        for role in roles:
            visit(role)
        return out, added, skipped

    @staticmethod
    def _approvals(agents: list[str]) -> list[str]:
        """这次计划里各阶段的确认点，按流水线顺序。

        计划可能补上了任务规格里没有的角色（例如自动补的 pocket），这些角色的
        确认点也要出现在计划里；所以它不能直接抄 `TaskSpec.approval_points`。
        """
        out: list[str] = []
        for agent in agents:
            for name in template_for(agent).approval_points:
                if name not in out:
                    out.append(name)
        order = {name: index for index, name in enumerate(APPROVAL_ORDER)}
        return sorted(out, key=lambda name: order.get(name, len(order)))

    def _dependencies(
        self, agents: list[str], provided_roles: Iterable[str] = ()
    ) -> tuple[list[dict[str, str]], list[str]]:
        edges: list[dict[str, str]] = []
        unresolved: list[str] = []
        given = {str(item) for item in provided_roles}
        selected = set(agents)
        for role in agents:
            for needed in PREREQUISITES.get(role, ()):
                if needed == role:
                    continue
                if needed not in selected:
                    if needed in given:
                        continue
                    unresolved.append(
                        f"{role} 需要 {needed} 的产物，但计划里没有这一步"
                    )
                    continue
                edge = {"from": needed, "to": role}
                if edge not in edges:
                    edges.append(edge)
        for role in agents:
            if role not in SINK_ROLES:
                continue
            for other in agents:
                if other == role:
                    continue
                edge = {"from": other, "to": role}
                if edge not in edges:
                    edges.append(edge)
        self._check_cycle(agents, edges)
        return edges, unresolved

    @staticmethod
    def _check_cycle(agents: list[str], edges: list[dict[str, str]]) -> None:
        indegree = {agent: 0 for agent in agents}
        out: dict[str, list[str]] = {agent: [] for agent in agents}
        for edge in edges:
            out[edge["from"]].append(edge["to"])
            indegree[edge["to"]] += 1
        ready = [agent for agent in agents if indegree[agent] == 0]
        seen = 0
        while ready:
            agent = ready.pop(0)
            seen += 1
            for target in out[agent]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if seen != len(agents):
            stuck = [agent for agent in agents if indegree[agent] > 0]
            raise RouterError(f"依赖成环，调度器会永远启动不了这些角色：{'、'.join(stuck)}")

    @staticmethod
    def _parallel(agents: list[str], edges: list[dict[str, str]]) -> list[list[str]]:
        """按依赖层级分组：同层且互不依赖的角色可以并行。"""
        if not agents:
            return []
        deps: dict[str, set[str]] = {agent: set() for agent in agents}
        for edge in edges:
            deps[edge["to"]].add(edge["from"])
        levels: dict[str, int] = {}

        def level(agent: str, trail: tuple[str, ...] = ()) -> int:
            if agent in levels:
                return levels[agent]
            if agent in trail:
                raise RouterError(f"依赖成环：{' → '.join(trail + (agent,))}")
            upstream = deps[agent]
            value = 0 if not upstream else 1 + max(level(item, trail + (agent,)) for item in upstream)
            levels[agent] = value
            return value

        buckets: dict[int, list[str]] = {}
        for agent in agents:
            buckets.setdefault(level(agent), []).append(agent)
        return [names for _, names in sorted(buckets.items()) if len(names) >= 2]

    @staticmethod
    def _denied(task: TaskSpec) -> list[str]:
        denied: list[str] = []
        for words, roles in catalog.EXCLUSION_RULES:
            if any(word in item for item in task.exclusions for word in words):
                for role in roles:
                    if role not in denied:
                        denied.append(role)
        return denied
