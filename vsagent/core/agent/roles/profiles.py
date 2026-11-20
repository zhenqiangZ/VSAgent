"""专业 Agent Profile：每个角色做什么、交回什么样的报告。

Profile 是“角色说明书”，不是运行期对象。数据全部来自 `roles/catalog.py`：

- **工作**：`DUTIES` 说明这个角色能做什么，只用于展示与提示词。
- **权限**：`PERMISSIONS` 说明它允许哪几类动作，派发前核对任务授权。
- **报告要求**：这一层回答“这个角色交回的报告里，哪些字段不能空”。

报告要求的取值只允许四种，对应 `AgentReport` 里“能说明可信程度”的字段：

- `evidence_refs`：结论要有出处（结构、文献、产物）。
- `artifact_refs`：产物要能被找到。
- `limitations`：要说明这次哪里没做到。
- `unresolved_questions`：要留下没答的问题。

不要求 `findings` 与 `conclusions`：它们在 `AgentReport` 的构造校验里已经管住了。
但“非空”不等于“能用”：非正常结束的报告还要再说一条，见 `check_report`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vsagent.core.agent.roles import catalog
from vsagent.core.agent.roles.contracts import AgentReport, AgentTask

REPORT_FIELDS: tuple[str, ...] = (
    "evidence_refs",
    "artifact_refs",
    "limitations",
    "unresolved_questions",
)
# 非正常结束时，结论至少要有一句能判断的话。2026-11-18 的 ses_0060
# 只交了「本轮写入：…/ha2_structure_catalog.md」，报告判据却因为引用非空
# 判了合格。引用回答「东西在哪」，结论要回答「做到了哪一步、还差什么」。
CONCLUSION_MIN_CHARS = 30

TITLES: dict[str, str] = catalog.TITLES
REPORT_REQUIREMENTS: dict[str, tuple[str, ...]] = catalog.REPORT_REQUIREMENTS
TIMEOUT_MINUTES: dict[str, float | None] = catalog.TIMEOUT_MINUTES
NOTES: dict[str, str] = catalog.NOTES


def _conclusions(report: AgentReport) -> list[str]:
    """报告里的结论行。契约已经规范成 `{"text": ...}`，这里只取非空行。"""
    out: list[str] = []
    for row in report.conclusions or ():
        text = str(row.get("text") or "").strip() if isinstance(row, dict) else str(row).strip()
        if text:
            out.append(text)
    return out


# 路径、网址和一长串编号不算“说清了一件事”：`runs/ha2_structure_catalog.md`
# 有三十多个字符，但它只回答东西在哪。量长度前先把这些片段去掉；只认带扩展名
# 的文件路径，免得把 `kcal/mol` 这类单位也当成路径。
_LOCATOR = re.compile(
    r"https?://\S+"
    r"|\S+\.(?:md|json|csv|txt|pdb|cif|sdf|mol2|png|html?|py|toml|log|ya?ml)\b"
    r"|\b[A-Za-z0-9_.:-]{16,}\b"
)


def _readable_length(text: str) -> int:
    """一句结论里“实际说了多少内容”的长度，去掉路径与编号。"""
    return len(_LOCATOR.sub("", text).strip())


@dataclass(frozen=True)
class AgentProfile:
    """一个专业角色的说明书。"""

    role: str
    title: str
    duties: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    report_requirements: tuple[str, ...] = ()
    note: str = ""
    timeout_minutes: float | None = None

    def check_report(self, report: AgentReport) -> list[str]:
        """报告是否满足这个角色的要求。返回问题清单，空列表表示合格。"""
        problems: list[str] = []
        if report.agent_role != self.role:
            problems.append(f"报告署名是 {report.agent_role}，这张说明书是 {self.role} 的")
        for field in self.report_requirements:
            value = getattr(report, field, None)
            if not value:
                problems.append(f"{self.role} 的报告缺 {field}：{note_for(field)}")
        if self.report_requirements:
            rows = _conclusions(report)
            if not rows:
                problems.append(f"{self.role} 的报告缺结论：父会话拿不到可以判断的东西")
            elif report.status != "completed" and not any(
                _readable_length(row) >= CONCLUSION_MIN_CHARS for row in rows
            ):
                problems.append(
                    f"{self.role} 的报告结论没有一条说清一件事"
                    f"（去掉路径与编号后不足 {CONCLUSION_MIN_CHARS} 字）；"
                    "非正常结束时要把做到哪一步、还差什么写清楚"
                )
        return problems

    def check_task(self, task: AgentTask) -> list[str]:
        """这份任务有没有超出角色的授权范围。

        动作类（读、分析、执行、写）要在说明书里才能派。工具名由 `agents.toml`
        定死，派发时不再另给一份清单，所以这里不查工具。
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
    """按角色取说明书。认不出来时列出可用角色。"""
    for item in PROFILES:
        if item.role == role:
            return item
    raise KeyError(f"没有这个角色：{role}。可用：{'、'.join(item.role for item in PROFILES)}")


def role_label(role: str) -> str:
    """角色的中文短名。取不到就退回角色名，不让一条进度把整场战役弄崩。"""
    try:
        return profile(role).title.split("：")[0]
    except KeyError:
        return role


def _build() -> tuple[AgentProfile, ...]:
    out: list[AgentProfile] = []
    for role in catalog.ROLES:
        out.append(
            AgentProfile(
                role=role,
                title=catalog.TITLES[role],
                duties=tuple(catalog.DUTIES.get(role, ())),
                permissions=tuple(catalog.PERMISSIONS.get(role, ())),
                report_requirements=catalog.REPORT_REQUIREMENTS.get(role, ()),
                note=catalog.NOTES[role],
                timeout_minutes=catalog.TIMEOUT_MINUTES.get(role),
            )
        )
    return tuple(out)


PROFILES: tuple[AgentProfile, ...] = _build()


def validate(profiles: tuple[AgentProfile, ...] = PROFILES) -> list[str]:
    """核对说明书与角色目录是否对得上。"""
    problems: list[str] = catalog.validate()
    seen: set[str] = set()
    for item in profiles:
        if item.role in seen:
            problems.append(f"角色重复：{item.role}")
        seen.add(item.role)
        if item.role not in catalog.ROLES:
            problems.append(f"说明书里有不存在的角色：{item.role}")
            continue
        if item.title != catalog.TITLES[item.role]:
            problems.append(f"{item.role} 的标题与目录不一致")
        if item.duties != tuple(catalog.DUTIES.get(item.role, ())):
            problems.append(f"{item.role} 的工作说明与目录不一致")
        if item.permissions != tuple(catalog.PERMISSIONS.get(item.role, ())):
            problems.append(f"{item.role} 的权限与目录不一致")
        if item.role != "principal" and not item.duties:
            problems.append(f"{item.role} 一项工作也没有：它做不了任何事")
        for field in item.report_requirements:
            if field not in REPORT_FIELDS:
                problems.append(f"{item.role} 要求了不认识的报告字段：{field}")
        if item.role != "principal" and not item.timeout_minutes:
            problems.append(f"{item.role} 没有执行时限")
    missing = sorted(set(catalog.ROLES) - {item.role for item in profiles})
    if missing:
        problems.append(f"目录里的角色没有说明书：{'、'.join(missing)}")
    return problems
