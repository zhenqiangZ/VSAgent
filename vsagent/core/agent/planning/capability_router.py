"""Capability Router：把任务规格切成能力、角色、工具族与依赖。

输入是 `TaskSpec`，它的每个子目标上带着 `capability` 标签（Phase 4 的模板写的）。
**不做文字匹配**：模板生成子目标时就知道它是什么能力，让它顺手记下来，比事后拿中文
去猜可靠。

三条规则值得单独说：

- **按子目标取族，不按角色取全部族。** `mechanism` 这个角色有四个族、八千多 token，
  但只回答"关键残基是谁"时不需要 `sar`。角色是职责范围，子目标才是这一次要用的。
- **前置分两类。** 评分任务要有筛选产物，但那份产物可能是上一次战役留下的——缺了
  只记进 `unresolved_inputs` 并告警，不替用户另开一场筛选。而筛选没有口袋、没有
  配体，连输入都没有，缺了就把上游补成计划里的节点（`AUTO_PREREQUISITES`）：
  计划必须自己可执行，`dispatch_campaign` 的硬闸门才有意义。
- **依赖必须无环。** 环意味着调度器永远启动不了任何节点，这里当场拒绝，不留给
  Phase 10 去发现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from vsagent.core.agent.roles.capabilities import capability as lookup_capability
from vsagent.core.agent.roles.contracts import CapabilityPlan, TaskSpec
from vsagent.core.agent.planning.task_compiler import APPROVAL_ORDER, TEMPLATES

PREREQUISITES: dict[str, tuple[str, ...]] = {
    "target_intelligence": (),
    "structure_discovery": (),
    # 口袋发现自己就会取结构（`structure` 族），不需要单另的取结构子任务。
    "pocket_discovery": (),
    "pocket_profiling": ("pocket_discovery",),
    "ligand_intelligence": (),
    "literature_evidence": (),
    "virtual_screening": ("pocket_discovery", "ligand_intelligence"),
    "scoring_evaluation": ("virtual_screening",),
    "mechanism_analysis": ("pocket_profiling", "ligand_intelligence"),
    "hypothesis_validation": ("mechanism_analysis", "literature_evidence"),
}
"""能力之间的前置关系。

**只写真正缺产物的情况**：下游能力没有能产出上游产物的工具族，才算前置。
`pocket_discovery` 不依赖 `structure_discovery`——它自己的族里就有取结构的工具；
`mechanism_analysis` 依赖 `ligand_intelligence`，因为它没有 ChEMBL 那一族，
自己拿不到已知活性。
"""

AUTO_PREREQUISITES: dict[str, tuple[str, ...]] = {
    # 没有口袋就没有可画像的位点。
    "pocket_profiling": ("pocket_discovery",),
    # 筛选要两条输入：一个定义好的口袋、一组配体。两样都没有就开不了跑，
    # 所以补成节点。用户已经给了库时，配体那一步就是给这份库做盘点，不白跑。
    "virtual_screening": ("pocket_discovery", "ligand_intelligence"),
}
"""缺了就必须现做的前置：补成计划里的节点，不留成告警。

和 `PREREQUISITES` 的差别只有一条——**能不能花上一次的产物**。评分可以吃已有
候选集，筛选不能凭空有口袋。只补这一类；别的照旧记未决，替用户多开一场筛选
是越权。

补的边界也只到这里：`mechanism_analysis` 自己有 `pocket` 族，`hypothesis_validation`
本身就是拿现有证据做综合，都不往上补。
"""

# 用户说"不要 X"时，哪项能力被明确排除。与 Phase 4 的模板过滤是同一件事的两端：
# 那边删模板，这边在计划里留一条"为什么没有它"。
DENIAL_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("对接", "分子对接"), ("virtual_screening",)),
    (("筛选", "虚拟筛选"), ("virtual_screening",)),
)


class RouterError(RuntimeError):
    """计划自身不成立（环、没有能力标签等）。"""


PROVIDED_KEYS: dict[str, str] = {
    "structure_discovery": "structure",
    "pocket_discovery": "pocket",
    "ligand_intelligence": "ligand_set",
}
"""能力对应的「用户已经给了」的输入键。

`TaskSpec.available_inputs` 里出现这些键时，对应能力不必再派角色去做——用户自带
口袋时还补一个口袋发现角色，是白跑一个子会话（P-83）。键名与
`intent_parser.PROMPT` 里 `provided_inputs` 的说明必须一致。
"""


def missing_prerequisites(
    plan: CapabilityPlan, roles: Iterable[str] | None = None
) -> list[str]:
    """这几项能力缺哪些前置产物。

    `roles` 给定时只看这些角色承担的能力：只派上游角色（例如只做配体盘点）时，
    计划里另一个分支缺前置不该拦这一次。**这是「限制执行」的判据**——
    `dispatch_campaign` 在这里判，不靠 `unresolved_inputs` 那句告警。
    """
    chosen = {str(role) for role in roles} if roles is not None else set(plan.selected_agents)
    # 用户已经给出的输入等于产物已有：自带口袋时，筛选不再算"缺口袋"。
    selected = set(plan.selected_capabilities) | set(plan.provided_capabilities)
    out: list[str] = []
    for name in plan.selected_capabilities:
        try:
            role = lookup_capability(name).role
        except KeyError:
            continue
        if role not in chosen:
            continue
        lack = [item for item in PREREQUISITES.get(name, ()) if item not in selected]
        if lack:
            out.append(f"{name} 需要 {'、'.join(lack)} 的产物")
    return out


@dataclass(frozen=True)
class Routing:
    """路由结果：计划本身，加上过程里的告警。"""

    plan: CapabilityPlan
    warnings: tuple[str, ...] = ()


class CapabilityRouter:
    """无状态；同一份任务规格两次路由结果一致。"""

    def route(self, task: TaskSpec) -> Routing:
        warnings: list[str] = []
        capabilities, unknown = self._capabilities(task)
        if unknown:
            warnings.append(f"这些子目标的能力标签不认识，已跳过：{'、'.join(unknown)}")
        if not capabilities:
            warnings.append("任务里没有可路由的能力标签，计划为空")

        denied = self._denied(task)
        provided = self._provided(task.available_inputs)
        capabilities, auto, skipped = self._close(capabilities, denied, task.available_inputs)
        if auto:
            warnings.append(
                f"补上了缺的前置步骤：{'、'.join(auto)}（不补这一步，下游没有输入）"
            )
        warnings.extend(skipped)

        plan, unresolved = self._build(
            task.task_id, task.revision, capabilities, denied, provided
        )
        warnings.extend(unresolved)
        return Routing(plan=plan, warnings=tuple(warnings))

    def extend(
        self,
        plan: CapabilityPlan,
        roles: Iterable[str],
        *,
        provided: Any = None,
    ) -> Routing:
        """把计划外的角色补进计划，返回新计划。

        用户问的是「配体筛选」，解析出来的计划只有配体与筛选；跑了一半才发现要
        受体侧的结构与口袋，而派发只允许从计划里减、不许加，这一步就谁也做不了
        （2026-11-13 的 HA2 会话）。这里按角色的能力表补：该角色的能力全加进来，
        族与依赖由 `_build` 重算，任务号仍是 `计划号:角色`，与整份计划一致。

        不在能力表里的角色直接拒：拼错一个名字不该悄悄多派一个空任务。
        """
        from vsagent.core.agent.roles.capabilities import capabilities_for

        wanted: list[str] = []
        for raw in roles:
            role = str(raw)
            if role in plan.selected_agents or role in wanted:
                continue
            if not capabilities_for(role):
                raise RouterError(
                    f"没有这个角色：{role}。可用：{'、'.join(self._role_names())}"
                )
            wanted.append(role)
        if not wanted:
            return Routing(plan=plan)

        capabilities = list(plan.selected_capabilities)
        added: list[str] = []
        for role in wanted:
            touched = False
            for capability in capabilities_for(role):
                if capability.name in plan.denied_capabilities:
                    continue
                if capability.name not in capabilities:
                    capabilities.append(capability.name)
                    touched = True
            if touched:
                added.append(role)
        if not added:
            return Routing(plan=plan)

        denied = list(plan.denied_capabilities)
        available = provided if provided is not None else {}
        given = [str(item) for item in (plan.provided_capabilities or ())]
        capabilities, auto, skipped = self._close(capabilities, denied, available)
        extended, unresolved = self._build(
            plan.task_id,
            plan.revision,
            capabilities,
            denied,
            list(dict.fromkeys([*given, *self._provided(available)])),
        )
        warnings: list[str] = [f"补派计划外的角色：{'、'.join(added)}"]
        if auto:
            warnings.append(
                f"补上了缺的前置步骤：{'、'.join(auto)}（不补这一步，下游没有输入）"
            )
        warnings.extend(skipped)
        return Routing(plan=extended, warnings=tuple(warnings) + tuple(unresolved))

    @staticmethod
    def _role_names() -> list[str]:
        from vsagent.core.agent.roles.capabilities import roles_with_capabilities

        return list(roles_with_capabilities())

    def _build(
        self,
        task_id: str,
        revision: int,
        capabilities: list[str],
        denied: list[str],
        provided: Iterable[str] = (),
    ) -> tuple[CapabilityPlan, list[str]]:
        """按能力清单装配计划，返回（计划, 未决输入）。"""
        given = [str(item) for item in provided]
        agents = self._agents(capabilities)
        families = {agent: self._families(capabilities, agent) for agent in agents}
        standalone = {agent: self._standalone(capabilities, agent) for agent in agents}
        dependencies, unresolved = self._dependencies(capabilities, agents, given)
        groups = self._parallel(agents, dependencies)
        rationale = tuple(
            f"{agent}：承担 {('、'.join(self._capabilities_of(capabilities, agent)))}"
            for agent in agents
        )
        plan = CapabilityPlan(
            task_id=task_id,
            revision=revision,
            selected_capabilities=tuple(capabilities),
            selected_agents=tuple(agents),
            tool_families={agent: list(names) for agent, names in families.items() if names},
            standalone_tools={agent: list(names) for agent, names in standalone.items() if names},
            dependencies=list(dependencies),
            parallel_groups=groups,
            denied_capabilities=tuple(denied),
            unresolved_inputs=tuple(item for item in unresolved),
            provided_capabilities=tuple(given),
            approval_points=tuple(self._approvals(capabilities)),
            rationale=rationale,
        )
        return plan, unresolved

    # ------------------------------------------------------------------
    @staticmethod
    def _capabilities(task: TaskSpec) -> tuple[list[str], list[str]]:
        """按子目标顺序取能力标签，去重；不认识的单独返回。"""
        picked: list[str] = []
        unknown: list[str] = []
        for row in task.subgoals:
            name = str(row.get("capability") or "").strip()
            if not name:
                continue
            try:
                lookup_capability(name)
            except KeyError:
                if name not in unknown:
                    unknown.append(name)
                continue
            if name not in picked:
                picked.append(name)
        return picked, unknown

    @classmethod
    def _close(
        cls,
        capabilities: list[str],
        denied: list[str],
        provided: Iterable[str] = (),
    ) -> tuple[list[str], list[str], list[str]]:
        """把 `AUTO_PREREQUISITES` 里的前置补成节点，返回（补完的能力, 新补的, 跳过的）。

        补进来的节点排在被依赖的那一项**前面**，计划读起来就是执行顺序。
        `denied` 里的不补：用户禁了对接，不能靠补一个筛选节点绕开他的禁止项。
        用户**已经给了**对应输入的不补（`PROVIDED_KEYS`）：自带口袋时再派一个
        口袋发现角色是白跑。
        """
        out: list[str] = []
        added: list[str] = []
        skipped: list[str] = []
        given = {str(item) for item in provided}

        def visit(name: str) -> None:
            if name in out:
                return
            for needed in AUTO_PREREQUISITES.get(name, ()):
                if needed in denied:
                    continue
                key = PROVIDED_KEYS.get(needed, "")
                if key and key in given:
                    note = f"{key} 已给出，不再补 {needed}"
                    if note not in skipped:
                        skipped.append(note)
                    continue
                visit(needed)
                if needed not in capabilities and needed not in added:
                    added.append(needed)
            out.append(name)

        for name in capabilities:
            visit(name)
        return out, added, skipped

    @staticmethod
    def _agents(capabilities: list[str]) -> list[str]:
        agents: list[str] = []
        for name in capabilities:
            role = lookup_capability(name).role
            if role not in agents:
                agents.append(role)
        return agents

    @staticmethod
    def _capabilities_of(capabilities: list[str], agent: str) -> list[str]:
        return [name for name in capabilities if lookup_capability(name).role == agent]

    @staticmethod
    def _approvals(capabilities: list[str]) -> list[str]:
        """这次计划里各阶段的确认点，按流水线顺序。

        从**选中能力**反查模板，而不是照抄任务规格：路由补出来的前置（口袋发现）
        在任务规格里没有对应的意图类型，照抄会漏掉「结合口袋确认」。
        """
        out: list[str] = []
        for capability in capabilities:
            for row in TEMPLATES.values():
                if str(row.get("capability") or "") != capability:
                    continue
                for name in row.get("approvals") or ():
                    if str(name) not in out:
                        out.append(str(name))
        order = {name: index for index, name in enumerate(APPROVAL_ORDER)}
        return sorted(out, key=lambda name: order.get(name, len(order)))

    def _families(self, capabilities: list[str], agent: str) -> list[str]:
        """这个角色**这一次**要用的族：从被选中的能力取，不取角色名下的全部族。"""
        out: list[str] = []
        for name in self._capabilities_of(capabilities, agent):
            for family in lookup_capability(name).families:
                if family not in out:
                    out.append(family)
        return out

    def _standalone(self, capabilities: list[str], agent: str) -> list[str]:
        out: list[str] = []
        for name in self._capabilities_of(capabilities, agent):
            for tool in lookup_capability(name).standalone_tools:
                if tool not in out:
                    out.append(tool)
        return out

    def _dependencies(
        self, capabilities: list[str], agents: list[str], provided: Iterable[str] = ()
    ) -> tuple[list[dict[str, str]], list[str]]:
        edges: list[dict[str, str]] = []
        unresolved: list[str] = []
        given = {str(item) for item in provided}
        selected = set(capabilities) | given
        for name in capabilities:
            role = lookup_capability(name).role
            for needed in PREREQUISITES.get(name, ()):
                if needed not in selected:
                    unresolved.append(
                        f"{name} 需要 {needed} 的产物，但计划里没有这一步"
                    )
                    continue
                if needed in given and needed not in capabilities:
                    # 用户自带的产物：没有上游节点，也就没有依赖边。
                    continue
                source = lookup_capability(needed).role
                if source == role:
                    continue
                edge = {"from": source, "to": role}
                if edge not in edges:
                    edges.append(edge)
        self._check_cycle(agents, edges)
        return edges, unresolved

    @staticmethod
    def _check_cycle(agents: list[str], edges: list[dict[str, str]]) -> None:
        """Kahn 拓扑：剩下的节点就是环里的。"""
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
        """按依赖层级分组：同层且互不依赖的角色可以并行。

        只输出两个以上的组——单个角色不需要并行组，`CapabilityPlan` 也不收。
        """
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
    def _provided(inputs: Any) -> list[str]:
        """用户已经给出的输入对应哪些能力。键名见 `PROVIDED_KEYS`。"""
        given = {str(key) for key in (inputs or {})}
        return [name for name, key in PROVIDED_KEYS.items() if key in given]

    @staticmethod
    def _denied(task: TaskSpec) -> list[str]:
        denied: list[str] = []
        for words, names in DENIAL_RULES:
            if any(word in item for item in task.exclusions for word in words):
                for name in names:
                    if name not in denied:
                        denied.append(name)
        return denied
