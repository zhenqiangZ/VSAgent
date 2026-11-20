"""TaskSpec：把意图编译成一份可执行、可验收的任务合同。

与 `TaskState` 的分工是这一层最容易被搅混的地方，所以写死在这里：

- **TaskSpec 是合同**：目标、范围、子目标、输入输出、验收条件、确认点。
  用户改目标才改它，改一次 `revision` 加一。
- **TaskState 是进度**：做到哪一步、还剩什么、卡在哪。执行过程中一直在变。

验收条件必须**能机器检查**，否则 Phase 8 的交付校验只能再写一段提示词。
所以每一项都带 `kind`，取值只有五种（见 `ACCEPTANCE_KINDS`）；需要专业判断的
写 `review_required`，明确交给评审，不伪装成布尔条件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsagent.core.agent.roles.catalog import AGENTS, STAGES
from vsagent.core.agent.roles.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    check_version,
    choice,
    mapping,
    number,
    records,
    reject_unknown,
    text,
    texts,
)

TASK_STATUSES: tuple[str, ...] = (
    "draft",
    "ready",
    "running",
    "blocked",
    "done",
    "failed",
    "cancelled",
)

ACCEPTANCE_KINDS: tuple[str, ...] = (
    "artifact_exists",      # 某个产物/文件在磁盘上
    "handle_registered",    # 登记表里有这个 handle
    "evidence_ref",         # 结论带了证据引用
    "approval_recorded",    # 某个确认点已被用户确认
    "review_required",      # 需要评审 Agent 或人工判断，不做布尔判定
)
"""机器可查的只有前四种。第五种是显式的人工入口，不许拿它冒充"已验收"。"""


def _named_rows(rows: Any, field_name: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """一组对象，每一项都要有这几个非空文本字段。"""
    out = records(rows, field_name)
    for index, row in enumerate(out):
        for key in keys:
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{field_name}[{index}].{key} 不能为空")
            row[key] = value.strip()
    return out


def _unique_texts(value: Any, field_name: str) -> tuple[str, ...]:
    """一组不重复的文本。重复项直接报错，不静默去重。

    `texts()` 会去重；交付物与确认点是合同里的清单，重复说明写入方算错了，
    静默合并会让「为什么只派了一份」变得难以追查。
    """
    raw = value
    if raw is None:
        raw = ()
    elif isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        raise ContractError(f"{field_name} 应当是字符串数组，收到 {type(raw).__name__}")
    out = texts(raw, field_name)
    if len(out) != len(raw):
        raise ContractError(f"{field_name} 里有重复项：{'、'.join(out)}")
    return out


@dataclass
class TaskSpec:
    """一次科研任务的合同。"""

    task_id: str
    objective: str
    campaign_id: str = ""
    research_scope: dict[str, Any] = field(default_factory=dict)
    subgoals: list[dict[str, Any]] = field(default_factory=list)
    constraints: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    required_inputs: list[dict[str, Any]] = field(default_factory=list)
    available_inputs: dict[str, Any] = field(default_factory=dict)
    required_outputs: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    acceptance_criteria: list[dict[str, Any]] = field(default_factory=list)
    approval_points: tuple[str, ...] = ()
    status: str = "draft"
    revision: int = 0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.task_id = text(self.task_id, "TaskSpec.task_id")
        self.objective = text(self.objective, "TaskSpec.objective")
        self.campaign_id = text(self.campaign_id, "TaskSpec.campaign_id", allow_empty=True)
        self.status = choice(self.status, TASK_STATUSES, "TaskSpec.status")
        self.revision = int(number(self.revision, "TaskSpec.revision", low=0))
        self.research_scope = mapping(self.research_scope, "TaskSpec.research_scope")
        self.available_inputs = mapping(self.available_inputs, "TaskSpec.available_inputs")
        self.constraints = texts(self.constraints, "TaskSpec.constraints")
        self.exclusions = texts(self.exclusions, "TaskSpec.exclusions")
        self.subgoals = _named_rows(self.subgoals, "TaskSpec.subgoals", ("goal",))
        for index, row in enumerate(self.subgoals):
            agent = row.get("agent")
            if not isinstance(agent, str) or not agent.strip():
                raise ContractError(
                    f"TaskSpec.subgoals[{index}].agent 不能为空：每个子目标都要有负责角色"
                )
            if agent.strip() not in AGENTS:
                raise ContractError(
                    f"TaskSpec.subgoals[{index}].agent 不认识：{agent!r}。"
                    f"可用：{'、'.join(AGENTS)}"
                )
            row["agent"] = agent.strip()
            stage = row.get("stage")
            if stage is None or (isinstance(stage, str) and not stage.strip()):
                row.pop("stage", None)
                continue
            if not isinstance(stage, str) or stage.strip() not in STAGES:
                raise ContractError(
                    f"TaskSpec.subgoals[{index}].stage 不认识：{stage!r}。"
                    f"可用：{'、'.join(STAGES)}"
                )
            row["stage"] = stage.strip()
        self.required_inputs = _named_rows(
            self.required_inputs, "TaskSpec.required_inputs", ("name",)
        )
        self.required_outputs = _unique_texts(
            self.required_outputs, "TaskSpec.required_outputs"
        )
        self.evidence_requirements = _unique_texts(
            self.evidence_requirements, "TaskSpec.evidence_requirements"
        )
        self.approval_points = _unique_texts(
            self.approval_points, "TaskSpec.approval_points"
        )
        self.acceptance_criteria = _named_rows(
            self.acceptance_criteria, "TaskSpec.acceptance_criteria", ("kind", "target")
        )
        for index, row in enumerate(self.acceptance_criteria):
            self.acceptance_criteria[index]["kind"] = choice(
                row["kind"], ACCEPTANCE_KINDS, f"TaskSpec.acceptance_criteria[{index}].kind"
            )

    @property
    def machine_checks(self) -> list[dict[str, Any]]:
        """能机器判定的那几条。交付校验只认这一份。"""
        return [row for row in self.acceptance_criteria if row["kind"] != "review_required"]

    @property
    def review_checks(self) -> list[dict[str, Any]]:
        return [row for row in self.acceptance_criteria if row["kind"] == "review_required"]

    def uncovered_outputs(self) -> list[str]:
        """没有验收条件的交付物。空列表才说明每个交付物都有人管。"""
        covered = {row.get("output") or row.get("target") for row in self.acceptance_criteria}
        return [name for name in self.required_outputs if name not in covered]

    def with_updates(self, **changes: Any) -> "TaskSpec":
        """改一份副本并重新校验。改目标时 `revision` 由调用方显式加一。"""
        data = self.to_dict()
        data.update(changes)
        return TaskSpec.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "campaign_id": self.campaign_id,
            "objective": self.objective,
            "research_scope": dict(self.research_scope),
            "subgoals": [dict(row) for row in self.subgoals],
            "constraints": list(self.constraints),
            "exclusions": list(self.exclusions),
            "required_inputs": [dict(row) for row in self.required_inputs],
            "available_inputs": dict(self.available_inputs),
            "required_outputs": list(self.required_outputs),
            "evidence_requirements": list(self.evidence_requirements),
            "acceptance_criteria": [dict(row) for row in self.acceptance_criteria],
            "approval_points": list(self.approval_points),
            "status": self.status,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TaskSpec":
        reject_unknown(
            mapping(data, "TaskSpec"),
            {
                "schema_version",
                "task_id",
                "campaign_id",
                "objective",
                "research_scope",
                "subgoals",
                "constraints",
                "exclusions",
                "required_inputs",
                "available_inputs",
                "required_outputs",
                "evidence_requirements",
                "acceptance_criteria",
                "approval_points",
                "status",
                "revision",
            },
            name="TaskSpec",
        )
        return cls(
            task_id=data.get("task_id"),
            objective=data.get("objective"),
            campaign_id=data.get("campaign_id", ""),
            research_scope=data.get("research_scope") or {},
            subgoals=data.get("subgoals") or [],
            constraints=tuple(data.get("constraints") or ()),
            exclusions=tuple(data.get("exclusions") or ()),
            required_inputs=data.get("required_inputs") or [],
            available_inputs=data.get("available_inputs") or {},
            required_outputs=tuple(data.get("required_outputs") or ()),
            evidence_requirements=tuple(data.get("evidence_requirements") or ()),
            acceptance_criteria=data.get("acceptance_criteria") or [],
            approval_points=tuple(data.get("approval_points") or ()),
            status=data.get("status", "draft"),
            revision=data.get("revision", 0),
            schema_version=check_version(data.get("schema_version"), name="TaskSpec"),
        )
