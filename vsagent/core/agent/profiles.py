"""专业 Agent Profile：每个角色承担哪些能力、交回什么样的报告。

Profile 是"角色说明书"，不是运行期对象。它由三样东西拼出来：

- **能力**：来自 `capabilities.py`，不再抄一遍。角色承担哪些能力只有一处事实来源。
- **工具族与常驻工具**：能力里已经有，这里只做汇总。
- **报告要求**：这一层是新加的——它回答"这个角色交回的报告里，哪些字段不能空"。

报告要求的取值只允许四种，对应 `AgentReport` 里"能说明可信程度"的字段：

- `evidence_refs`：结论要有出处（结构、文献、产物）。
- `artifact_refs`：产物要能被找到。
- `limitations`：要说明这次哪里没做到。
- `unresolved_questions`：要留下没答的问题。

不要求 `findings` 与 `conclusions`：它们在 `AgentReport` 的构造校验里已经管住了
（标 completed 就必须有），重复一遍只会产生两条说法。

除了报告，说明书还要能回答"这份任务能不能派给它"：`check_task()` 核对任务的动作类
与工具族有没有超出这一角色的授权。派发前判一次，比跑起来之后再发现它用了不该用的
工具便宜得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .capabilities import (
    CAPABILITIES,
    capabilities_for,
    families_for,
    roles_with_capabilities,
    standalone_for,
)
from .contracts import AgentReport, AgentTask

REPORT_FIELDS: tuple[str, ...] = (
    "evidence_refs",
    "artifact_refs",
    "limitations",
    "unresolved_questions",
)

TITLES: dict[str, str] = {
    "principal": "主 Agent：拆解目标、派发与汇总",
    "target": "靶点情报",
    "structure_pocket": "结构与口袋",
    "conformation": "构象与口袋画像",
    "ligand": "配体情报",
    "literature": "文献证据",
    "screening": "筛选执行",
    "scoring": "评分与一致性",
    "mechanism": "机制解释",
    "hypothesis": "假设评审",
}

REPORT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "principal": (),
    "target": ("evidence_refs",),
    "structure_pocket": ("evidence_refs", "artifact_refs"),
    "conformation": ("artifact_refs", "limitations"),
    "ligand": ("evidence_refs",),
    "literature": ("evidence_refs",),
    "screening": ("artifact_refs", "limitations"),
    "scoring": ("evidence_refs", "limitations"),
    "mechanism": ("evidence_refs", "unresolved_questions"),
    "hypothesis": ("evidence_refs", "unresolved_questions"),
}
"""每个角色交报告时不能空的字段。空一项就算报告不合格，由 Phase 10 的派发处判。"""

TIMEOUT_MINUTES: dict[str, float | None] = {
    "principal": None,  # 主 agent 不派发，没有自己的执行时限
    "target": 20,
    "structure_pocket": 40,
    "conformation": 30,
    "ligand": 30,
    "literature": 20,
    "screening": 90,  # 对接可能跑很久，给足；超过这个数基本是卡住了
    "scoring": 30,
    "mechanism": 30,
    "hypothesis": 20,
}
"""单个角色的安全网（分钟）。**不是性能指标**：到点中断一个卡住的子会话，
把"还在跑"变成"失败，卡在这一步"，父会话才能决定重派还是换路。

`screening` 给 90 是刻意放宽的：对接是真在算，不是卡住。要更紧的场合用
`dispatch_campaign(timeout=...)` 给整场加一个更短的上限。
"""

NOTES: dict[str, str] = {
    "principal": "只做拆解、派发与汇总，不亲自执行领域动作。",
    "target": "只读检索；不产生产物，不替用户做决定。",
    "structure_pocket": "结构检索与口袋发现；改共享状态的动作由父会话确认后执行。",
    "conformation": "只读分析口袋几何与理化特征；不准备受体。",
    "ligand": "汇总已知配体与活性；不生成新分子。",
    "literature": "检索与整理证据；结论必须带出处。",
    "screening": "执行受体准备、对接与筛选；每一步都要留下可复查的产物。",
    "scoring": "对已有产物做独立评分；不重新对接。",
    "mechanism": "解释结合模式与关键残基；区分证据与推断。",
    "hypothesis": "整理支持与反证，更新假设状态；不替用户裁决。",
}


@dataclass(frozen=True)
class AgentProfile:
    """一个专业角色的说明书。"""

    role: str
    title: str
    capabilities: tuple[str, ...] = ()
    report_requirements: tuple[str, ...] = ()
    note: str = ""
    # 单个角色的安全网（分钟）。调度器到期中断它，记失败并写清卡在哪。
    # `None` 表示不限；`principal` 不派发，所以是 None。
    timeout_minutes: float | None = None

    @property
    def families(self) -> tuple[str, ...]:
        return families_for(self.role)

    @property
    def standalone_tools(self) -> tuple[str, ...]:
        return standalone_for(self.role)

    @property
    def permissions(self) -> tuple[str, ...]:
        """角色能做的动作类：能力里声明的并集，按固定顺序。"""
        seen: list[str] = []
        for item in capabilities_for(self.role):
            for permission in item.permissions:
                if permission not in seen:
                    seen.append(permission)
        return tuple(seen)

    def check_report(self, report: AgentReport) -> list[str]:
        """报告是否满足这个角色的要求。返回问题清单，空列表表示合格。"""
        problems: list[str] = []
        if report.agent_role != self.role:
            problems.append(f"报告署名是 {report.agent_role}，这张说明书是 {self.role} 的")
        for field in self.report_requirements:
            value = getattr(report, field, None)
            if not value:
                problems.append(f"{self.role} 的报告缺 {field}：{note_for(field)}")
        return problems

    def check_task(self, task: AgentTask, families: Iterable[str] = ()) -> list[str]:
        """这份任务有没有超出角色的授权范围。

        `families` 是派发时打算给它的工具族（来自能力计划）。两样都要在说明书里
        才能派：动作类（读/分析/执行/写）与工具族。少一样就在派发前拦下，而不是
        等它跑起来用了不该用的工具再发现。
        """
        problems: list[str] = []
        if task.role != self.role:
            problems.append(f"任务派给 {task.role}，这张说明书是 {self.role} 的")
        extra_permissions = [item for item in task.permissions if item not in self.permissions]
        if extra_permissions:
            problems.append(
                f"{self.role} 没有授权 {'、'.join(extra_permissions)}："
                f"它能做的只有 {'、'.join(self.permissions) or '无'}"
            )
        extra_families = [item for item in families if item not in self.families]
        if extra_families:
            problems.append(
                f"{self.role} 没有授权 {'、'.join(extra_families)} 族："
                f"它能用的只有 {'、'.join(self.families) or '无'}"
            )
        return problems


FIELD_NOTES: dict[str, str] = {
    "evidence_refs": "结论要能追溯到结构、文献或产物",
    "artifact_refs": "产物要能被找到",
    "limitations": "要说明这次哪里没做到",
    "unresolved_questions": "要留下没答的问题",
}


def note_for(field: str) -> str:
    return FIELD_NOTES.get(field, "说明为什么没做")


def profile(role: str) -> AgentProfile:
    """按角色取说明书。取不到时列出可用角色。"""
    for item in PROFILES:
        if item.role == role:
            return item
    raise KeyError(f"没有这个角色：{role}。可用：{'、'.join(item.role for item in PROFILES)}")


def _build() -> tuple[AgentProfile, ...]:
    out: list[AgentProfile] = []
    for role in ("principal",) + roles_with_capabilities():
        out.append(
            AgentProfile(
                role=role,
                title=TITLES[role],
                capabilities=tuple(item.name for item in capabilities_for(role)),
                report_requirements=REPORT_REQUIREMENTS[role],
                note=NOTES[role],
                timeout_minutes=TIMEOUT_MINUTES.get(role),
            )
        )
    return tuple(out)


PROFILES: tuple[AgentProfile, ...] = _build()


def validate(profiles: tuple[AgentProfile, ...] = PROFILES) -> list[str]:
    """核对说明书与能力表是否对得上。"""
    problems: list[str] = []
    seen: set[str] = set()
    for item in profiles:
        if item.role in seen:
            problems.append(f"角色重复：{item.role}")
        seen.add(item.role)
        if item.role not in TITLES:
            problems.append(f"角色没有标题：{item.role}")
        if item.role != "principal" and not item.capabilities:
            problems.append(f"{item.role} 一项能力也没有：它做不了任何事")
        for name in item.capabilities:
            try:
                capability = next(c for c in CAPABILITIES if c.name == name)
            except StopIteration:
                problems.append(f"{item.role} 引用了不存在的能力：{name}")
                continue
            if capability.role != item.role:
                problems.append(f"{name} 属于 {capability.role}，却挂在 {item.role} 的名下")
        for field in item.report_requirements:
            if field not in REPORT_FIELDS:
                problems.append(f"{item.role} 要求了不认识的报告字段：{field}")
        # 每个会跑子会话的角色都要有时限：没有时限的失败会一直挂着，
        # 父会话只能靠人发现（P-83）。
        if item.role != "principal" and not item.timeout_minutes:
            problems.append(f"{item.role} 没有执行时限")
    return problems
