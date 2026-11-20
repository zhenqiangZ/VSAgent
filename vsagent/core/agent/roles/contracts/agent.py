"""AgentTask 与 AgentReport：专业 Agent 的输入合同与交付格式。

Phase 1 只定义、不调度。定义在这里的原因与其它协议相同：形状先定，后面加
Agent 时不用反复改主流程。

两条硬规定：

- **报告只回传摘要与引用**：`artifact_refs` / `evidence_refs` 指向产物与证据，
  不把子会话的聊天记录塞进父上下文。否则多开几个 Agent，父上下文会线性膨胀。
- **报告必须能说"没做成"**：`status` 与 `limitations` 是一等字段。一份只说
  结论不说局限的报告，到汇总时没有办法与另一份区分可信程度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsagent.core.agent.roles.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    check_version,
    choice,
    mapping,
    records,
    reject_unknown,
    text,
    texts,
)
from vsagent.core.agent.roles.contracts.task import ACCEPTANCE_KINDS

AGENT_TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "blocked",
    "done",
    "failed",
    "cancelled",
)

REPORT_STATUSES: tuple[str, ...] = ("completed", "partial", "failed", "cancelled")


def _acceptance(rows: Any) -> list[dict[str, Any]]:
    out = records(rows, "acceptance_criteria")
    for index, row in enumerate(out):
        kind = text(row.get("kind"), f"acceptance_criteria[{index}].kind")
        row["kind"] = choice(kind, ACCEPTANCE_KINDS, f"acceptance_criteria[{index}].kind")
        row["target"] = text(row.get("target"), f"acceptance_criteria[{index}].target")
    return out


@dataclass
class AgentTask:
    """派给一个专业 Agent 的一份任务。"""

    task_id: str
    role: str
    objective: str
    # 父任务的目标。只作背景：子 agent 不该把它当成自己的交付物再完成一遍。
    background: str = ""
    campaign_id: str = ""
    parent_task_id: str = ""
    input_refs: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()
    acceptance_criteria: list[dict[str, Any]] = field(default_factory=list)
    dependencies: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    # 计划里的字段表没有它，但 Phase 10 的「同一 task_id 只派一次」要靠它。
    status: str = "pending"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.task_id = text(self.task_id, "AgentTask.task_id")
        self.role = text(self.role, "AgentTask.role")
        self.objective = text(self.objective, "AgentTask.objective")
        self.background = text(
            self.background, "AgentTask.background", allow_empty=True
        )
        self.campaign_id = text(self.campaign_id, "AgentTask.campaign_id", allow_empty=True)
        self.parent_task_id = text(
            self.parent_task_id, "AgentTask.parent_task_id", allow_empty=True
        )
        if self.parent_task_id and self.parent_task_id == self.task_id:
            raise ContractError("AgentTask.parent_task_id 不能指向自己")
        self.status = choice(self.status, AGENT_TASK_STATUSES, "AgentTask.status")
        self.input_refs = mapping(self.input_refs, "AgentTask.input_refs")
        self.constraints = texts(self.constraints, "AgentTask.constraints")
        self.required_outputs = texts(self.required_outputs, "AgentTask.required_outputs")
        self.dependencies = texts(self.dependencies, "AgentTask.dependencies")
        if self.task_id in self.dependencies:
            raise ContractError("AgentTask.dependencies 不能包含自己")
        self.permissions = texts(self.permissions, "AgentTask.permissions")
        self.acceptance_criteria = _acceptance(self.acceptance_criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "campaign_id": self.campaign_id,
            "parent_task_id": self.parent_task_id,
            "role": self.role,
            "objective": self.objective,
            "background": self.background,
            "input_refs": dict(self.input_refs),
            "constraints": list(self.constraints),
            "required_outputs": list(self.required_outputs),
            "acceptance_criteria": [dict(row) for row in self.acceptance_criteria],
            "dependencies": list(self.dependencies),
            "permissions": list(self.permissions),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AgentTask":
        reject_unknown(
            mapping(data, "AgentTask"),
            {
                "schema_version",
                "task_id",
                "campaign_id",
                "parent_task_id",
                "role",
                "objective",
                "background",
                "input_refs",
                "constraints",
                "required_outputs",
                "acceptance_criteria",
                "dependencies",
                "permissions",
                "status",
            },
            name="AgentTask",
        )
        return cls(
            task_id=data.get("task_id"),
            role=data.get("role"),
            objective=data.get("objective"),
            background=data.get("background", ""),
            campaign_id=data.get("campaign_id", ""),
            parent_task_id=data.get("parent_task_id", ""),
            input_refs=data.get("input_refs") or {},
            constraints=tuple(data.get("constraints") or ()),
            required_outputs=tuple(data.get("required_outputs") or ()),
            acceptance_criteria=data.get("acceptance_criteria") or [],
            dependencies=tuple(data.get("dependencies") or ()),
            permissions=tuple(data.get("permissions") or ()),
            status=data.get("status", "pending"),
            schema_version=check_version(data.get("schema_version"), name="AgentTask"),
        )


@dataclass
class AgentReport:
    """一个专业 Agent 交回来的结构化报告。"""

    task_id: str
    agent_role: str
    status: str = "completed"
    campaign_id: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: tuple[str, ...] = ()
    # 子会话查回来的证据对象（编号、正文、来源、类别、时间）。父会话据此
    # 建立一场的证据池；`evidence_refs` 留着给旧报告读取。
    evidence: list[dict[str, Any]] = field(default_factory=list)
    artifact_refs: tuple[str, ...] = ()
    conclusions: list[dict[str, Any]] = field(default_factory=list)
    limitations: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    suggested_followups: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.task_id = text(self.task_id, "AgentReport.task_id")
        self.agent_role = text(self.agent_role, "AgentReport.agent_role")
        self.campaign_id = text(self.campaign_id, "AgentReport.campaign_id", allow_empty=True)
        self.status = choice(self.status, REPORT_STATUSES, "AgentReport.status")
        self.findings = self._claims(self.findings, "AgentReport.findings")
        self.conclusions = self._claims(self.conclusions, "AgentReport.conclusions")
        self.evidence_refs = texts(self.evidence_refs, "AgentReport.evidence_refs")
        self.evidence = self._evidence(self.evidence, "AgentReport.evidence")
        self.artifact_refs = texts(self.artifact_refs, "AgentReport.artifact_refs")
        self.limitations = texts(self.limitations, "AgentReport.limitations")
        self.unresolved_questions = texts(
            self.unresolved_questions, "AgentReport.unresolved_questions"
        )
        self.suggested_followups = self._claims(
            self.suggested_followups, "AgentReport.suggested_followups", key="action"
        )
        if self.status == "completed" and not (self.findings or self.conclusions):
            raise ContractError(
                "AgentReport.status=completed 却没有 findings 与 conclusions："
                "空报告应当标 partial 或 failed，否则汇总时会被当成有结论"
            )

    @staticmethod
    def _claims(rows: Any, field_name: str, *, key: str = "text") -> list[dict[str, Any]]:
        out = records(rows, field_name)
        for index, row in enumerate(out):
            row[key] = text(row.get(key), f"{field_name}[{index}].{key}")
        return out

    @staticmethod
    def _evidence(rows: Any, field_name: str) -> list[dict[str, Any]]:
        """校验证据对象：编号、正文、来源、类别四项必填。

        这里不按 `SearchEvidence` 的全套字段判：报告是跨进程交付的，
        旧报告与手工写的报告可能只给这四项；缺的字段由读取方补默认值。
        """
        out = records(rows, field_name)
        for index, row in enumerate(out):
            for key in ("id", "content", "source", "source_type"):
                row[key] = text(row.get(key), f"{field_name}[{index}].{key}")
            row.setdefault("title", "")
            row.setdefault("url", "")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "campaign_id": self.campaign_id,
            "agent_role": self.agent_role,
            "status": self.status,
            "findings": [dict(row) for row in self.findings],
            "evidence_refs": list(self.evidence_refs),
            "evidence": [dict(row) for row in self.evidence],
            "artifact_refs": list(self.artifact_refs),
            "conclusions": [dict(row) for row in self.conclusions],
            "limitations": list(self.limitations),
            "unresolved_questions": list(self.unresolved_questions),
            "suggested_followups": [dict(row) for row in self.suggested_followups],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AgentReport":
        reject_unknown(
            mapping(data, "AgentReport"),
            {
                "schema_version",
                "task_id",
                "campaign_id",
                "agent_role",
                "status",
                "findings",
                "evidence_refs",
                "evidence",
                "artifact_refs",
                "conclusions",
                "limitations",
                "unresolved_questions",
                "suggested_followups",
            },
            name="AgentReport",
        )
        return cls(
            task_id=data.get("task_id"),
            agent_role=data.get("agent_role"),
            status=data.get("status", "completed"),
            campaign_id=data.get("campaign_id", ""),
            findings=data.get("findings") or [],
            evidence_refs=tuple(data.get("evidence_refs") or ()),
            evidence=data.get("evidence") or [],
            artifact_refs=tuple(data.get("artifact_refs") or ()),
            conclusions=data.get("conclusions") or [],
            limitations=tuple(data.get("limitations") or ()),
            unresolved_questions=tuple(data.get("unresolved_questions") or ()),
            suggested_followups=data.get("suggested_followups") or [],
            schema_version=check_version(data.get("schema_version"), name="AgentReport"),
        )
