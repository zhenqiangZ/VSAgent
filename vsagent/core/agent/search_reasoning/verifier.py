"""交付前的答案核对。

四件事：说法是否都有去向、外部事实有没有来源、答案有没有无依据的改变、
有没有违反任务限制。前三件能机械判，先机械判；语义方向（证据与说法是否
一致）可选交给一次模型请求。

不通过时最多修一轮：缺来源的降级成未核实，没有依据的改动恢复原说法，
遗漏的补回；违反限制的不自动改，写清楚交给用户。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .models import AnswerState
from .jsonio import Ask, parse_json

# 必须带来源的说法类别。预测与任务类不要求外部来源。
GROUNDED_CATEGORIES = frozenset({"method", "fact", "mechanism"})
# 需要有依据的状态。`unverified` 允许没有来源，它本身就是「还没核实」。
GROUNDED_STATUSES = frozenset({"supported", "partially_supported", "revised"})
# 任务限制里的禁止词。
FORBIDDEN_PREFIXES = ("不要", "禁止", "不得", "无需", "别")
# 判断「真的执行了」的标记。只提到名词不算，必须出现结果性的说法。
EXECUTION_MARKERS = (
    "对接结果", "对接分数", "对接得分", "已完成", "执行了", "结果是",
    "docking score", "对接构象",
)


@dataclass
class VerifyReport:
    """一次核对的结果。"""

    passed: bool = True
    issues: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    repaired: bool = False

    def texts(self) -> list[str]:
        return [str(item.get("text") or "") for item in self.issues]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [dict(item) for item in self.issues],
            "metrics": dict(self.metrics),
            "repaired": self.repaired,
        }


def _add(report: VerifyReport, kind: str, claim_id: str, text: str) -> None:
    report.issues.append({"kind": kind, "claim_id": claim_id, "text": text})


def _completeness(state: AnswerState, report: VerifyReport) -> None:
    """每条初始说法都要在修订后的清单里有一个去向。"""
    current = {claim.id for claim in state.current_claims()}
    for claim in state.initial_claims:
        if claim.id not in current:
            _add(report, "completeness", claim.id, f"初始说法「{claim.id}」没有去向")
    for claim in state.current_claims():
        if claim.status == "rejected" and not claim.rationale.strip():
            _add(report, "completeness", claim.id, f"说法「{claim.id}」被拒绝但没写理由")


def _grounding(state: AnswerState, report: VerifyReport) -> None:
    """外部事实类说法必须有证据编号。"""
    for claim in state.current_claims():
        if claim.category not in GROUNDED_CATEGORIES:
            continue
        if claim.status in GROUNDED_STATUSES and not claim.evidence_ids:
            _add(
                report,
                "grounding",
                claim.id,
                f"说法「{claim.id}」标成 {claim.status}，但没有挂证据编号",
            )


def _change(state: AnswerState, report: VerifyReport) -> None:
    """按说法集合算保留比例、覆盖与没有理由的改变。"""
    initial = {claim.id: claim for claim in state.initial_claims}
    current = {claim.id: claim for claim in state.current_claims()}
    if not initial:
        return
    retained = 0
    changed_without_reason = 0
    for claim_id, before in initial.items():
        after = current.get(claim_id)
        if after is None:
            continue
        if after.content.strip() == before.content.strip():
            retained += 1
        elif not after.evidence_ids:
            changed_without_reason += 1
            _add(report, "change", claim_id, f"说法「{claim_id}」改了内容却没有证据支持")
    report.metrics["retention"] = round(retained / len(initial), 3)
    report.metrics["coverage"] = round(len(current) / len(initial), 3)
    report.metrics["unjustified_change"] = round(
        changed_without_reason / len(initial), 3
    )


def _limit_subject(text: str) -> str:
    """从「不要进行分子对接」里取出「分子对接」。"""
    subject = (text or "").strip()
    for prefix in FORBIDDEN_PREFIXES:
        if subject.startswith(prefix):
            subject = subject[len(prefix):]
            break
    for verb in ("进行", "开展", "执行", "做"):
        if subject.startswith(verb):
            subject = subject[len(verb):]
            break
    return subject.strip("，。；;,. ")


def _limits(state: AnswerState, limits: list[str], report: VerifyReport) -> None:
    """用户明确禁止的动作，答案里不能出现执行结果。

    只提到名词不算：允许把对接文献当背景。出现「对接结果」「对接得分」
    这类结果性说法时才算违反。
    """
    answer = state.final_answer or ""
    for limit in limits:
        text = (limit or "").strip()
        if not text or not text.startswith(FORBIDDEN_PREFIXES):
            continue
        subject = _limit_subject(text)
        if subject and subject in answer and any(
            marker in answer for marker in EXECUTION_MARKERS
        ):
            _add(report, "limit", "", f"答案里出现了用户禁止的内容：{subject}")


async def _semantic(state: AnswerState, ask: Ask, report: VerifyReport) -> None:
    """可选的语义核对：证据与说法的方向是否一致。"""
    claims, evidence = [], []
    pool = {item.id: item for item in state.evidence_pool}
    for claim in state.current_claims():
        if not claim.evidence_ids:
            continue
        claims.append(
            {"id": claim.id, "content": claim.content, "status": claim.status}
        )
        for evidence_id in claim.evidence_ids[:2]:
            item = pool.get(evidence_id)
            if item is not None:
                evidence.append(
                    {
                        "id": item.id,
                        "content": item.content[:500],
                        "source_type": item.source_type,
                    }
                )
    if not claims or not evidence:
        return
    messages = [
        {"role": "system", "content": prompts.VERIFIER_SYSTEM},
        {
            "role": "user",
            "content": prompts.VERIFIER_USER.format(
                question=state.question,
                limits="（见上一步）",
                initial=prompts.dumps(
                    [{"id": item.id, "content": item.content} for item in state.initial_claims]
                ),
                current=prompts.dumps(claims),
                answer=state.final_answer,
            ),
        },
    ]
    data = parse_json(await ask(messages), "答案核对")
    for row in data.get("semantic") or []:
        if not isinstance(row, dict) or row.get("consistent", True):
            continue
        _add(
            report,
            "grounding",
            str(row.get("claim_id") or ""),
            str(row.get("note") or "证据与说法的方向不一致"),
        )


def verify(
    state: AnswerState,
    *,
    limits: list[str] | None = None,
    ask: Ask | None = None,
) -> VerifyReport:
    """按四件事核对一份答案状态。"""
    report = VerifyReport()
    _completeness(state, report)
    _grounding(state, report)
    _change(state, report)
    _limits(state, limits or [], report)
    report.passed = not any(item["kind"] != "change" for item in report.issues)
    return report


async def verify_async(
    state: AnswerState,
    *,
    limits: list[str] | None = None,
    ask: Ask | None = None,
) -> VerifyReport:
    """在机械核对之后可选用模型看语义方向。"""
    report = verify(state, limits=limits)
    if ask is not None:
        await _semantic(state, ask, report)
        report.passed = not any(item["kind"] != "change" for item in report.issues)
    return report


def repair(state: AnswerState, report: VerifyReport) -> None:
    """按核对结果修一轮：只做能确定的修复，不重写整段答案。"""
    initial = {claim.id: claim for claim in state.initial_claims}
    for issue in report.issues:
        claim_id = str(issue.get("claim_id") or "")
        claim = next(
            (item for item in state.current_claims() if item.id == claim_id), None
        )
        if claim is None:
            continue
        if issue["kind"] == "grounding":
            # 没有来源的事实不能写成已经证实，降级成未核实。
            state.revise_claim(claim.copy(status="unverified"))
        elif issue["kind"] == "change":
            before = initial.get(claim_id)
            if before is not None:
                state.revise_claim(before.copy())


async def verify_and_repair(
    state: AnswerState,
    *,
    limits: list[str] | None = None,
    ask: Ask | None = None,
) -> VerifyReport:
    """核对；有可修的项时修一轮再核对一次。修复只跑一轮。"""
    report = await verify_async(state, limits=limits, ask=ask)
    repairable = any(item["kind"] in ("grounding", "change") for item in report.issues)
    if report.passed and not repairable:
        return report
    repair(state, report)
    second = await verify_async(state, limits=limits, ask=None)
    second.repaired = True
    return second
