"""证据整合：只改动受有效证据影响的说法。

五个动作：

- `retain`：原说法不动；
- `append`：增加新说法；
- `strengthen`：补上依据；
- `revise`：修改说法；
- `reject`：不进入答案。

规则先过一遍：没有挂到具体说法的证据不许改动说法，可靠度低于阈值的
证据不许单独推翻原说法，预测不能写成实验事实。规则不过的评估降级成
「保留」或「补充」，理由写回评估里。然后由模型在保留下来的证据上重写。
"""

from __future__ import annotations

from typing import Any

from . import policy, prompts
from .models import (
    AnswerState,
    Claim,
    EvidenceAssessment,
    SearchReasoningError,
)
from .jsonio import Ask, parse_json

ACTIONS = ("retain", "append", "strengthen", "revise", "reject")

# 动作与默认状态：模型没写状态时按动作补一个，不凭空给「已证实」。
ACTION_STATUS = {
    "retain": "initial",
    "append": "unverified",
    "strengthen": "supported",
    "revise": "revised",
    "reject": "rejected",
}


def _downgrade(assessment: EvidenceAssessment) -> None:
    """规则不允许的整合动作降级，理由接在原来的说明后面。"""
    if assessment.relation == "supplementary":
        assessment.integration_action = "append"
    else:
        assessment.integration_action = "retain"


def apply_rules(state: AnswerState) -> dict[str, int]:
    """按规则校正评估里的整合动作，返回各动作的条数。"""
    claims = {claim.id: claim for claim in state.current_claims()}
    evidence = {item.id: item for item in state.evidence_pool}
    for assessment in state.assessments:
        item = evidence.get(assessment.evidence_id)
        if item is None:
            assessment.integration_action = "reject"
            assessment.rationale = "证据不在证据池里"
            continue
        claim = next(
            (claims.get(cid) for cid in assessment.related_claim_ids if claims.get(cid)),
            None,
        )
        allowed, reason = policy.action_allowed(assessment, item, claim)
        if not allowed and assessment.integration_action != "retain":
            _downgrade(assessment)
            assessment.rationale = "；".join(
                piece for piece in (assessment.rationale, reason) if piece
            )
    counts: dict[str, int] = {action: 0 for action in ACTIONS}
    for assessment in state.assessments:
        counts[assessment.integration_action] = (
            counts.get(assessment.integration_action, 0) + 1
        )
    return counts


def _payload(state: AnswerState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """整理成提示里的说法清单与证据清单。"""
    claims = [
        {
            "id": claim.id,
            "content": claim.content,
            "category": claim.category,
            "confidence": claim.confidence,
        }
        for claim in state.current_claims()
    ]
    evidence: list[dict[str, Any]] = []
    pool = {item.id: item for item in state.evidence_pool}
    for assessment in state.assessments:
        item = pool.get(assessment.evidence_id)
        if item is None or assessment.relation == "irrelevant":
            continue
        evidence.append(
            {
                "id": item.id,
                "content": item.content[:800],
                "source": item.source,
                "source_type": item.source_type,
                "title": item.title,
                "published_at": item.published_at,
                "relation": assessment.relation,
                "support_level": assessment.support_level,
                "suggested_action": assessment.integration_action,
                "related_claim_ids": list(assessment.related_claim_ids),
                "rationale": assessment.rationale,
            }
        )
    return claims, evidence


def _claim_from_row(
    row: dict[str, Any],
    state: AnswerState,
    base: Claim | None,
) -> Claim:
    """把模型输出的一行说法变成 `Claim`，证据编号必须在证据池里。"""
    known = {item.id for item in state.evidence_pool}
    evidence_ids = [
        str(item)
        for item in (row.get("evidence_ids") or [])
        if str(item) in known
    ]
    action = str(row.get("action") or "retain").strip()
    if action not in ACTIONS:
        raise SearchReasoningError(f"整合动作无法识别：{action!r}")
    status = str(row.get("status") or "").strip() or ACTION_STATUS.get(action, "initial")
    if base is None:
        if action not in ("append", "revise"):
            raise SearchReasoningError(f"整合结果里的新说法 {row.get('id')!r} 没有对应动作")
        return Claim(
            id=str(row.get("id") or ""),
            content=str(row.get("content") or ""),
            category=row.get("category", "fact"),
            confidence=row.get("confidence", 0.5),
            evidence_ids=evidence_ids,
            status=status,
            rationale=str(row.get("rationale") or ""),
        )
    return base.copy(
        content=str(row.get("content") or base.content),
        category=row.get("category", base.category),
        confidence=row.get("confidence", base.confidence),
        evidence_ids=evidence_ids or list(base.evidence_ids),
        status=status,
        rationale=str(row.get("rationale") or ""),
    )


def _apply_output(
    state: AnswerState,
    payload: dict[str, Any],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """把整合输出写回答案状态，返回动作计数与冲突列表。"""
    final = str(payload.get("final_answer") or "").strip()
    if not final:
        raise SearchReasoningError("整合结果没有给出最终答案")
    rows = payload.get("claims")
    if not isinstance(rows, list):
        raise SearchReasoningError("整合结果没有给出说法清单")

    current = {claim.id: claim for claim in state.current_claims()}
    actions: dict[str, int] = {action: 0 for action in ACTIONS}
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "retain").strip()
        if action not in ACTIONS:
            raise SearchReasoningError(f"整合动作无法识别：{action!r}")
        actions[action] = actions.get(action, 0) + 1
        if action == "retain":
            continue
        claim_id = str(row.get("id") or "").strip()
        if action == "reject" and claim_id in current:
            state.revise_claim(
                current[claim_id].copy(
                    status="rejected",
                    rationale=str(row.get("rationale") or "证据不支持这条说法"),
                )
            )
            continue
        claim = _claim_from_row(row, state, current.get(claim_id))
        state.revise_claim(claim)

    conflicts = [
        dict(item) for item in (payload.get("conflicts") or []) if isinstance(item, dict)
    ]
    # 冲突必须带双方来源，缺来源的条目不写进状态。
    model_conflicts = [
        item for item in conflicts if item.get("sides") or item.get("condition")
    ]
    # 评估阶段按规则记下的相反结论保留；模型这一份追加在后面。
    rules = [
        item for item in state.conflicts if str(item.get("source") or "") == "assess"
    ]
    state.conflicts = rules + model_conflicts
    state.final_answer = final
    state.status = "integrating"
    return actions, state.conflicts


async def integrate(
    state: AnswerState,
    ask: Ask,
    *,
    limits: list[str] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """跑一次整合，返回动作计数与规则降级的说明。

    `limits` 是用户明确提出的限制，随提示一起给模型；核对阶段还会再查一次。
    """
    rules = apply_rules(state)
    claims, evidence = _payload(state)
    limits_line = "；".join(limits or []) or "（没有额外限制）"
    messages = [
        {"role": "system", "content": prompts.INTEGRATOR_SYSTEM},
        {
            "role": "user",
            "content": prompts.INTEGRATOR_USER.format(
                question=state.question,
                answer=state.initial_answer,
                claims=prompts.dumps(claims),
                evidence=prompts.dumps(evidence),
            )
            + f"\n\n任务限制：{limits_line}\n只输出 JSON。",
        },
    ]
    payload = parse_json(await ask(messages), "证据整合")
    actions, _conflicts = _apply_output(state, payload)
    notes = [
        f"{item.evidence_id}：{item.rationale}"
        for item in state.assessments
        if item.rationale
    ]
    # `rules` 只用来确认各动作的初始分布；最终以模型改写后的动作计数为准。
    del rules
    return actions, notes
