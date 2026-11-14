"""CapabilityPlan：为一份 TaskSpec 分配专业能力、工具族与依赖关系。

它不是"工具清单"，而是**分工方案**：谁负责哪一段、用哪些族、谁等谁、哪些能力
明确不启动。模型照着它干活之前，先由这里决定它该看见什么。

两条校验是这一层存在的理由：

- 依赖必须闭在已选角色里。指向一个没选的角色的依赖，等于计划里留了一个永远
  等不到的节点。
- 一个角色只能出现在一个并行组里。出现在两组意味着调度器会启动它两次，
  而 `task_id` 幂等要到 Phase 10 才做，现在会真的跑两份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import (
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
class CapabilityPlan:
    """一次规划的结果。"""

    task_id: str
    selected_capabilities: tuple[str, ...] = ()
    selected_agents: tuple[str, ...] = ()
    tool_families: dict[str, list[str]] = field(default_factory=dict)
    standalone_tools: dict[str, list[str]] = field(default_factory=dict)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    parallel_groups: list[list[str]] = field(default_factory=list)
    denied_capabilities: tuple[str, ...] = ()
    unresolved_inputs: tuple[str, ...] = ()
    # 用户已经给出、因而不必再派角色去做的能力（例如自带口袋时的
    # `pocket_discovery`）。依赖与缺前置判据都把它当成"已有"。
    provided_capabilities: tuple[str, ...] = ()
    approval_points: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()
    revision: int = 0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.task_id = text(self.task_id, "CapabilityPlan.task_id")
        self.revision = int(number(self.revision, "CapabilityPlan.revision", low=0))
        self.selected_capabilities = texts(
            self.selected_capabilities, "CapabilityPlan.selected_capabilities"
        )
        self.selected_agents = texts(self.selected_agents, "CapabilityPlan.selected_agents")
        self.denied_capabilities = texts(
            self.denied_capabilities, "CapabilityPlan.denied_capabilities"
        )
        self.unresolved_inputs = texts(self.unresolved_inputs, "CapabilityPlan.unresolved_inputs")
        self.provided_capabilities = texts(
            self.provided_capabilities, "CapabilityPlan.provided_capabilities"
        )
        self.approval_points = texts(self.approval_points, "CapabilityPlan.approval_points")
        self.rationale = texts(self.rationale, "CapabilityPlan.rationale")

        selected = set(self.selected_capabilities)
        both = sorted(selected & set(self.denied_capabilities))
        if both:
            raise ContractError(f"这些能力既被选中又被拒绝：{'、'.join(both)}")

        agents = set(self.selected_agents)
        self.tool_families = self._per_agent(
            self.tool_families, "CapabilityPlan.tool_families", agents
        )
        self.standalone_tools = self._per_agent(
            self.standalone_tools, "CapabilityPlan.standalone_tools", agents
        )
        self.dependencies = self._dependencies(agents)
        self.parallel_groups = self._parallel(self.parallel_groups, agents)

    @staticmethod
    def _per_agent(value: Any, field_name: str, agents: set[str]) -> dict[str, list[str]]:
        table = mapping(value, field_name)
        out: dict[str, list[str]] = {}
        for name, items in table.items():
            agent = text(name, f"{field_name} 的键")
            if agent not in agents:
                raise ContractError(f"{field_name} 里给了没选中的角色：{agent}")
            out[agent] = list(texts(items, f"{field_name}.{agent}"))
        return out

    def _dependencies(self, agents: set[str]) -> list[dict[str, Any]]:
        rows = records(self.dependencies, "CapabilityPlan.dependencies")
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            source = text(row.get("from"), f"CapabilityPlan.dependencies[{index}].from")
            target = text(row.get("to"), f"CapabilityPlan.dependencies[{index}].to")
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
            raise ContractError("CapabilityPlan.parallel_groups 应当是数组的数组")
        out: list[list[str]] = []
        placed: set[str] = set()
        for index, group in enumerate(groups):
            names = list(texts(group, f"CapabilityPlan.parallel_groups[{index}]"))
            if len(names) < 2:
                raise ContractError(
                    f"CapabilityPlan.parallel_groups[{index}] 少于两个角色："
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
            "selected_capabilities": list(self.selected_capabilities),
            "selected_agents": list(self.selected_agents),
            "tool_families": {key: list(value) for key, value in self.tool_families.items()},
            "standalone_tools": {key: list(value) for key, value in self.standalone_tools.items()},
            "dependencies": [dict(row) for row in self.dependencies],
            "parallel_groups": [list(group) for group in self.parallel_groups],
            "denied_capabilities": list(self.denied_capabilities),
            "unresolved_inputs": list(self.unresolved_inputs),
            "provided_capabilities": list(self.provided_capabilities),
            "approval_points": list(self.approval_points),
            "rationale": list(self.rationale),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CapabilityPlan":
        reject_unknown(
            mapping(data, "CapabilityPlan"),
            {
                "schema_version",
                "task_id",
                "revision",
                "selected_capabilities",
                "selected_agents",
                "tool_families",
                "standalone_tools",
                "dependencies",
                "parallel_groups",
                "denied_capabilities",
                "unresolved_inputs",
                "provided_capabilities",
                "approval_points",
                "rationale",
            },
            name="CapabilityPlan",
        )
        return cls(
            task_id=data.get("task_id"),
            selected_capabilities=tuple(data.get("selected_capabilities") or ()),
            selected_agents=tuple(data.get("selected_agents") or ()),
            tool_families=data.get("tool_families") or {},
            standalone_tools=data.get("standalone_tools") or {},
            dependencies=data.get("dependencies") or [],
            parallel_groups=data.get("parallel_groups") or [],
            denied_capabilities=tuple(data.get("denied_capabilities") or ()),
            unresolved_inputs=tuple(data.get("unresolved_inputs") or ()),
            provided_capabilities=tuple(data.get("provided_capabilities") or ()),
            approval_points=tuple(data.get("approval_points") or ()),
            rationale=tuple(data.get("rationale") or ()),
            revision=data.get("revision", 0),
            schema_version=check_version(data.get("schema_version"), name="CapabilityPlan"),
        )
