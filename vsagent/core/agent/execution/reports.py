"""从跑完的子会话生成 `AgentReport`。

**不调用模型。** 报告的内容来自三处可以机械读到的东西：

- 最后一条回答的文字（拆成结论）；非正常结束时优先取收尾总结，见 `answer_text`；
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
from pathlib import Path

from vsagent.core.agent.roles.contracts import AgentReport
from vsagent.core.models import PartType, Session, ToolStatus
from vsagent.core.agent.roles.profiles import AgentProfile
from vsagent.core.paths import project_root

HANDLE_RE = re.compile(r"\b(receptor|ligandset|artifact)#(\d+)\b")
URL_RE = re.compile(r"https?://[^\s，。；、）)]+")
MAX_CONCLUSIONS = 20
MAX_REFS = 12
MAX_EVIDENCE = 30
DIGEST_LIMIT = 1200
REPORT_DIRECTORY = "reports"
"""完整报告放在 `runs/reports/` 下，派发结果只带回结论预览与绝对路径。

父会话没有按 session_id 读别的会话的工具，能读的只有文件。报告被 800 字或
1500 token 的预算截掉之后，磁盘上这份是唯一的全文入口。

路径写成绝对路径：`read` 的相对路径按应用启动目录解释，源码运行与打包运行
不是同一个根，拼相对路径在另一种启动方式下会指到不存在的文件。
"""


def report_root() -> Path:
    """完整报告的根目录。判据把它换到 `.probe/`，不往真的 runs/ 里写。"""
    return project_root() / "runs" / REPORT_DIRECTORY


def report_path(parent: Session, child: Session) -> Path:
    return report_root() / parent.id / f"{child.id}.md"


def _index_name(key: str) -> str:
    """一场战役一个索引文件：`index-<计划号>.md`，没有计划号时用 `index.md`。"""
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", key.strip())[:48]
    return f"index-{safe}.md" if safe else "index.md"


def index_path(parent: Session, key: str = "") -> Path:
    return report_root() / parent.id / _index_name(key)


def index_abspath(parent: Session, key: str = "") -> str:
    """索引的绝对路径。返回给模型之前都走这里，别在调用方各拼一遍。"""
    return str(index_path(parent, key))


def prepare_report_dir(parent: Session) -> Path:
    """开跑之前先把目录建好：写不进去要在派发之前暴露，不能等跑完才报错。"""
    directory = report_root() / parent.id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_report(parent: Session, child: Session, text: str) -> str:
    """覆盖写入一份完整报告，返回给模型看的绝对路径。"""
    path = report_path(parent, child)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def save_index(parent: Session, text: str, key: str = "") -> str:
    path = index_path(parent, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


@dataclass(frozen=True)
class Built:
    """报告与它的判据结果。判据不通过时调用方应当退回重做，而不是照样收下。"""

    report: AgentReport
    problems: tuple[str, ...] = ()

    @property
    def acceptable(self) -> bool:
        return not self.problems


def answer_text(session: Session) -> str:
    """子 agent 交回来的那一段话：正文优先，非正常结束时优先收尾总结。

    非正常结束（步数用满、工具连续失败、被中断）时，正文可能只是最后
    一两步的一句交代；真正的结论在收尾总结里。搜索增强还会压住正文，
    等收尾总结发布之后才放出来，于是「最后一条部件」正好是那句空话
    （2026-11-18 的 ses_0060，父会话只拿到一行文件路径）。
    """
    reason = str(getattr(session, "last_exit_reason", "") or "").strip()
    for message in reversed(session.messages):
        if message.role != "assistant":
            continue
        texts = [
            part.text.strip()
            for part in message.parts
            if part.type is PartType.TEXT and part.text.strip()
        ]
        summaries = [
            part.text.strip()
            for part in message.parts
            if part.type is PartType.SUMMARY and part.text.strip()
        ]
        if reason and summaries:
            return summaries[-1]
        if texts:
            return texts[-1]
        if summaries:
            return summaries[-1]
    return ""


def answer_blocks(session: Session) -> tuple[str, str]:
    """最后一条助手消息里的正文与收尾总结，各自按出现顺序拼起来。

    磁盘上那份报告要的是全文：`answer_text` 只挑一段给预览，正文与总结
    各留一份才不会丢——非正常结束时，真正的交代往往在总结里。
    """
    for message in reversed(session.messages):
        if message.role != "assistant":
            continue
        texts = [
            part.text.strip()
            for part in message.parts
            if part.type is PartType.TEXT and part.text.strip()
        ]
        summaries = [
            part.text.strip()
            for part in message.parts
            if part.type is PartType.SUMMARY and part.text.strip()
        ]
        return "\n\n".join(texts), "\n\n".join(summaries)
    return "", ""


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


def _evidence(session: Session) -> list[dict]:
    """把子会话里查回来的结果转成证据对象。

    只处理适配层认得的查询工具；计算产物另成一类。同一条证据只留一次，
    最多 `MAX_EVIDENCE` 条：报告进父会话的上下文，证据太多会把结论挤掉。
    """
    from vsagent.core.agent.search_reasoning.adapters import adapter_for

    out: list[dict] = []
    seen: set[str] = set()
    for message in session.messages:
        for part in message.parts:
            if part.type is not PartType.TOOL or part.tool_status is not ToolStatus.COMPLETED:
                continue
            adapter = adapter_for(part.tool_name or "")
            if adapter is None:
                continue
            for item in adapter.convert(part):
                if item.id in seen:
                    continue
                seen.add(item.id)
                out.append(item.to_dict())
                if len(out) >= MAX_EVIDENCE:
                    return out
    return out


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
    answer = answer_text(session)
    conclusions = [{"text": line} for line in _lines(answer)]
    refs, findings = _handles(session)
    urls = _urls(session)
    evidence = _evidence(session)
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
        evidence=evidence,
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
    if report.evidence:
        lines.append(f"证据对象：{len(report.evidence)} 条（按编号在答案状态里查）")
    if report.limitations:
        lines.append(f"局限：{'；'.join(report.limitations[:3])}")
    if report.unresolved_questions:
        lines.append(f"未决：{'；'.join(report.unresolved_questions[:3])}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"
