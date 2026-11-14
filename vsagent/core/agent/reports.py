"""从跑完的子会话生成 `AgentReport`。

**不调用模型。** 报告的内容来自三处可以机械读到的东西：

- 最后一条回答的文字（拆成结论）；
- 工具调用留下的句柄（受体、配体集、产物）与链接（证据）；
- 退出原因与失败的工具（局限）。

为什么不用模型总结：子会话的原文一旦搬进父上下文，多开几个子 agent 就会线性膨胀，
这正是报告要解决的问题。机械抽取会漏掉措辞上的润色，但漏掉的那部分本来也不该由
父会话承担——父会话要的是结论、引用和局限。

抽不出证据时**不编**：`evidence_refs` 就是空的，由 `profile.check_report()` 判不合格，
让子 agent 去补。这条与 Phase 8 的"`manual` 不假装通过"是同一条原则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import AgentReport
from ..models import PartType, Session, ToolStatus
from .profiles import AgentProfile

HANDLE_RE = re.compile(r"\b(receptor|ligandset|artifact)#(\d+)\b")
URL_RE = re.compile(r"https?://[^\s，。；、）)]+")
MAX_CONCLUSIONS = 20
MAX_REFS = 12
DIGEST_LIMIT = 1200


@dataclass(frozen=True)
class Built:
    """报告与它的判据结果。判据不通过时调用方应当退回重做，而不是照样收下。"""

    report: AgentReport
    problems: tuple[str, ...] = ()

    @property
    def acceptable(self) -> bool:
        return not self.problems


def _last_answer(session: Session) -> str:
    for message in reversed(session.messages):
        if message.role != "assistant":
            continue
        text = "\n".join(
            part.text for part in message.parts if part.type is PartType.TEXT and part.text
        ).strip()
        if text:
            return text
    return ""


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        line = re.sub(r"^\d+[.、]\s*", "", line)
        if not line:
            continue
        out.append(line)
        if len(out) >= MAX_CONCLUSIONS:
            break
    return out


def _handles(session: Session) -> tuple[list[str], list[str]]:
    """返回（产物引用, 由工具产出的发现）。同一个句柄只记一次。"""
    refs: list[str] = []
    findings: list[str] = []
    for message in session.messages:
        for part in message.parts:
            if part.type is not PartType.TOOL or part.tool_status is not ToolStatus.COMPLETED:
                continue
            for match in HANDLE_RE.finditer(part.tool_output or ""):
                handle = f"{match.group(1)}#{match.group(2)}"
                if handle in refs:
                    continue
                refs.append(handle)
                findings.append(f"{part.tool_name} 产出 {handle}")
                if len(refs) >= MAX_REFS:
                    return refs, findings
    return refs, findings


def _urls(session: Session) -> list[str]:
    out: list[str] = []
    for message in session.messages:
        for part in message.parts:
            text = part.tool_output or part.text or ""
            for match in URL_RE.finditer(text):
                url = match.group(0).rstrip(".,;")
                if url not in out:
                    out.append(url)
                if len(out) >= MAX_REFS:
                    return out
    return out


def _failures(session: Session) -> list[str]:
    names: list[str] = []
    for message in session.messages:
        for part in message.parts:
            if part.type is PartType.TOOL and part.tool_status is ToolStatus.FAILED:
                name = part.tool_name or "?"
                if name not in names:
                    names.append(name)
    return names


def _status(session: Session) -> str:
    reason = session.last_exit_reason or ""
    if reason == "cancelled":
        return "cancelled"
    if reason == "error":
        return "failed"
    if reason in ("exhausted", "tool_failed", "topic_full"):
        return "partial"
    return "completed"


def build(
    session: Session,
    profile: AgentProfile,
    *,
    task_id: str = "",
    campaign_id: str = "",
) -> Built:
    """把一份跑完的子会话转成报告，并按角色说明书判一遍。"""
    answer = _last_answer(session)
    conclusions = [{"text": line} for line in _lines(answer)]
    refs, findings = _handles(session)
    urls = _urls(session)
    failures = _failures(session)

    status = _status(session)
    if status == "completed" and not conclusions:
        # 没有回答却标完成，交付方什么也没说：降成 partial，避免父会话收下一份空报告。
        status = "partial"

    limitations: list[str] = []
    if failures:
        limitations.append(f"这些工具调用失败过：{'、'.join(failures)}")
    reason = session.last_exit_reason or ""
    if reason and reason != "":
        limitations.append(f"本轮以 {reason} 结束")

    report = AgentReport(
        task_id=task_id or session.id,
        campaign_id=campaign_id,
        agent_role=profile.role,
        status=status,
        findings=[{"text": item} for item in findings],
        evidence_refs=tuple(urls),
        artifact_refs=tuple(refs),
        conclusions=conclusions,
        limitations=tuple(limitations),
        unresolved_questions=(),
        suggested_followups=[{"action": f"重试 {name}"} for name in failures],
    )
    return Built(report=report, problems=tuple(profile.check_report(report)))


def digest(report: AgentReport, *, limit: int = DIGEST_LIMIT) -> str:
    """父会话读的那一段：结论、引用、局限，不搬原文。"""
    lines = [f"角色：{report.agent_role}　状态：{report.status}"]
    if report.conclusions:
        lines.append("结论：")
        lines.extend(f"- {row['text']}" for row in report.conclusions[:5])
    if report.artifact_refs:
        lines.append(f"产物：{'、'.join(report.artifact_refs[:6])}")
    if report.evidence_refs:
        lines.append(f"证据：{'、'.join(report.evidence_refs[:6])}")
    if report.limitations:
        lines.append(f"局限：{'；'.join(report.limitations[:3])}")
    if report.unresolved_questions:
        lines.append(f"未决：{'；'.join(report.unresolved_questions[:3])}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"
