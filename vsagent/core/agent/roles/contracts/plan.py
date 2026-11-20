"""AgentPlan：为一份 TaskSpec 分配角色与依赖关系。

它不是工具清单，而是分工方案：谁负责哪一段、谁等谁、哪些角色明确不启动。
工具清单由 `roles/agents.toml` 按角色给，计划里不再抄一份。

两条校验是这一层存在的理由：

- 依赖必须闭在已选角色里。指向一个没选的角色的依赖，等于计划里留了一个永远
  等不到的节点。
- 一个角色只能出现在一个并行组里。出现在两组意味着调度器会启动它两次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsagent.core.agent.roles.catalog import AGENTS
from vsagent.core.agent.roles.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    check_version,
    mapping,
    number,
    records,
    reject_unknown,
    text,
    texts,
)


@dataclass
class AgentPlan:
    """一次规划的结果。"""

    task_id: str
    selected_agents: tuple[str, ...] = ()
    denied_agents: tuple[str, ...] = ()
    provided_agents: tuple[str, ...] = ()
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    parallel_groups: list[list[str]] = field(default_factory=list)
    unresolved_inputs: tuple[str, ...] = ()
    approval_points: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()
    revision: int = 0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.task_id = text(self.task_id, "AgentPlan.task_id")
        self.revision = int(number(self.revision, "AgentPlan.revision", low=0))
        self.selected_agents = self._agents(self.selected_agents, "AgentPlan.selected_agents")
        self.denied_agents = self._agents(self.denied_agents, "AgentPlan.denied_agents")
        self.provided_agents = self._agents(self.provided_agents, "AgentPlan.provided_agents")
        self.unresolved_inputs = texts(self.unresolved_inputs, "AgentPlan.unresolved_inputs")
        self.approval_points = texts(self.approval_points, "AgentPlan.approval_points")
        self.rationale = texts(self.rationale, "AgentPlan.rationale")

        both = sorted(set(self.selected_agents) & set(self.denied_agents))
        if both:
            raise ContractError(f"这些角色既被选中又被拒绝：{'、'.join(both)}")

        agents = set(self.selected_agents)
        self.dependencies = self._dependencies(agents)
        self.parallel_groups = self._parallel(self.parallel_groups, agents)

    @staticmethod
    def _agents(value: Any, field_name: str) -> tuple[str, ...]:
        out: list[str] = []
        for name in texts(value, field_name):
            if name not in AGENTS:
                raise ContractError(f"{field_name} 里有不存在的角色：{name}")
            if name not in out:
                out.append(name)
        return tuple(out)

    def _dependencies(self, agents: set[str]) -> list[dict[str, Any]]:
        rows = records(self.dependencies, "AgentPlan.dependencies")
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            reject_unknown(
                row,
                {"from", "to"},
                name=f"AgentPlan.dependencies[{index}]",
            )
            source = text(row.get("from"), f"AgentPlan.dependencies[{index}].from")
            target = text(row.get("to"), f"AgentPlan.dependencies[{index}].to")
            if source == target:
                raise ContractError(f"依赖指向自己：{source}")
            unknown = [name for name in (source, target) if name not in agents]
            if unknown:
                raise ContractError(
                    f"依赖里的角色没有被选中：{'、'.join(unknown)}。"
                    "计划里不能留一个永远等不到的节点"
                )
            edge = (source, target)
            if edge in seen:
                raise ContractError(f"重复的依赖：{source} → {target}")
            seen.add(edge)
            out.append({"from": source, "to": target})
        return out

    @staticmethod
    def _parallel(groups: Any, agents: set[str]) -> list[list[str]]:
        if groups is None:
            return []
        if not isinstance(groups, (list, tuple)):
            raise ContractError("AgentPlan.parallel_groups 应当是数组的数组")
        out: list[list[str]] = []
        placed: set[str] = set()
        for index, group in enumerate(groups):
            names = list(texts(group, f"AgentPlan.parallel_groups[{index}]"))
            if len(names) < 2:
                raise ContractError(
                    f"AgentPlan.parallel_groups[{index}] 少于两个角色："
                    "单个角色不需要并行组"
                )
            unknown = [name for name in names if name not in agents]
            if unknown:
                raise ContractError(f"并行组里有没选中的角色：{'、'.join(unknown)}")
            repeated = sorted(placed & set(names))
            if repeated:
                raise ContractError(f"这些角色出现在多个并行组里：{'、'.join(repeated)}")
            placed.update(names)
            out.append(names)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "revision": self.revision,
            "selected_agents": list(self.selected_agents),
            "denied_agents": list(self.denied_agents),
            "provided_agents": list(self.provided_agents),
            "dependencies": [dict(row) for row in self.dependencies],
            "parallel_groups": [list(group) for group in self.parallel_groups],
            "unresolved_inputs": list(self.unresolved_inputs),
            "approval_points": list(self.approval_points),
            "rationale": list(self.rationale),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AgentPlan":
        reject_unknown(
            mapping(data, "AgentPlan"),
            {
                "schema_version",
                "task_id",
                "revision",
                "selected_agents",
                "denied_agents",
                "provided_agents",
                "dependencies",
                "parallel_groups",
                "unresolved_inputs",
                "approval_points",
                "rationale",
            },
            name="AgentPlan",
        )
        return cls(
            task_id=data.get("task_id"),
            selected_agents=tuple(data.get("selected_agents") or ()),
            denied_agents=tuple(data.get("denied_agents") or ()),
            provided_agents=tuple(data.get("provided_agents") or ()),
            dependencies=data.get("dependencies") or [],
            parallel_groups=data.get("parallel_groups") or [],
            unresolved_inputs=tuple(data.get("unresolved_inputs") or ()),
            approval_points=tuple(data.get("approval_points") or ()),
            rationale=tuple(data.get("rationale") or ()),
            revision=data.get("revision", 0),
            schema_version=check_version(data.get("schema_version"), name="AgentPlan"),
        )
