"""查询结果的整理、去重、评估与分类。

整理：把适配器给出的证据放进证据池，去掉同一来源的重复命中。
评估：按相关程度、来源可靠程度、时间适用性、直接支持程度四项打分，
再判支持、补充、冲突、无关四类，并映射到具体说法。

规则能判的不交给模型；规则判不出的少数几条批量交给一次模型请求。
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import policy, prompts
from .adapters import adapter_for
from .models import (
    RELATIONS,
    AnswerState,
    Claim,
    EvidenceAssessment,
    SearchEvidence,
    SearchReasoningError,
)
from .jsonio import Ask, parse_json

# 文字重合低于这个数就判无关。
RELEVANT_FLOOR = 0.08
# 超过这个数才算「直接支持」。
DIRECT_FLOOR = 0.35
# 规则判不出、值得交给模型复核的区间。
AMBIGUOUS_LOW = 0.08
AMBIGUOUS_HIGH = 0.16
# 一条证据最多挂到几条说法上。
CLAIM_LINKS = 3

NEGATIONS = (
    "不能", "没有", "未能", "无显著", "不显著", "无效", "失败", "相反",
    "不一致", "不能提高", "并不", "未见", "did not", "no significant",
    "failed", "contradict", "however",
)
POSITIVES = ("提高", "增加", "改善", "有效", "优于", "支持", "成立", "improve", "increase", "effective")
FRESH_WORDS = ("最新", "最近", "近期", "今年", "去年", "目前", "现在", "当前")

CJK = re.compile(r"[\u4e00-\u9fff]")
ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]{1,}")
YEAR = re.compile(r"(19|20)\d{2}")
# 配体、化合物这类编号：一至四位大写字母开头，带数字，后面可以再接字母数字。
CODE = re.compile(r"\b[A-Z]{1,4}\d{1,4}[A-Z0-9]*\b")


def _terms(text: str) -> set[str]:
    """把一段文字切成可以比对的词。

    中文按相邻两个字切，英文与数字按整词切（至少两个字符）。不做分词，
    因为这里要的只是「两段话有没有说到一起」，切得过细会把常用字算成相关。
    """
    value = (text or "").lower()
    words = set(ASCII_WORD.findall(value))
    chars = CJK.findall(value)
    words.update(
        "".join(chars[index : index + 2]) for index in range(len(chars) - 1)
    )
    if len(chars) == 1:
        words.add(chars[0])
    return words


def _overlap(left: str, right: str) -> float:
    """右一段的用词有多少出现在左一段里。取值 0 到 1。"""
    left_terms = _terms(left)
    right_terms = _terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(right_terms)


def _freshness(question: str, published_at: str) -> float:
    """时间适用性：问题要求近期时，越旧越低；不要求时统一给 0.8。"""
    if not any(word in (question or "") for word in FRESH_WORDS):
        return 0.8
    match = YEAR.search(published_at or "")
    if match is None:
        return 0.4
    age = max(0, time.localtime().tm_year - int(match.group(0)))
    return max(0.1, 1.0 - 0.2 * age)


def _contradicts(evidence: str, claim: str) -> bool:
    """证据是否在说相反的结论。

    规则很粗：说法是正向判断、证据里出现否定词，就按相反处理，交给整合
    阶段写成有条件结论；反过来只在证据明显否定时判。判不准的会在
    评估结果里标成 `contextual`，不直接推翻原说法。
    """
    body = (evidence or "").lower()
    if not any(word in body for word in NEGATIONS):
        return False
    return any(word in (claim or "") for word in POSITIVES)


def _duplicate_key(evidence: SearchEvidence) -> tuple[str, str, str, str]:
    """一条证据的四个去重键：链接、DOI、标题、正文指纹。"""
    url = (evidence.url or "").strip().rstrip("/").split("#")[0].lower()
    doi = str(evidence.metadata.get("doi") or "").strip().lower()
    title = re.sub(r"\s+", "", (evidence.title or "")).lower()
    body = re.sub(r"\s+", "", (evidence.content or ""))[:160].lower()
    return url, doi, title, body


def is_duplicate(evidence: SearchEvidence, pool: list[SearchEvidence]) -> bool:
    """这条证据与池子里的某一条是否同一来源。"""
    url, doi, title, body = _duplicate_key(evidence)
    for item in pool:
        other_url, other_doi, other_title, other_body = _duplicate_key(item)
        if url and url == other_url:
            return True
        if doi and doi == other_doi:
            return True
        if title and title == other_title:
            return True
        if body and body == other_body:
            return True
    return False


def items_from(parts: list[Any]) -> list[SearchEvidence]:
    """把一组工具结果转换成证据对象，不改证据池。

    失败的调用跳过：失败输出里写的是「这次没搜成」，把这句话当成证据，
    会让「没有查到」变成「查到了某条结论」。
    """
    items: list[SearchEvidence] = []
    for part in parts:
        status = getattr(part, "tool_status", None)
        if getattr(status, "value", status) == "failed":
            continue
        adapter = adapter_for(getattr(part, "tool_name", ""))
        if adapter is None:
            continue
        items.extend(adapter.convert(part))
    return items


def merge(items: list[SearchEvidence], state: AnswerState) -> int:
    """把一组现成的证据对象并入证据池，返回新增条数。"""
    added = 0
    for item in items:
        if is_duplicate(item, state.evidence_pool):
            continue
        state.add_evidence(item)
        added += 1
    return added


def collect_many(parts: list[Any], state: AnswerState) -> int:
    """把一组工具结果收进证据池，返回新增条数。"""
    return merge(items_from(parts), state)


def collect(part: Any, state: AnswerState) -> int:
    """把一次工具调用的结果收进证据池，返回新增条数。"""
    return collect_many([part], state)


def _claim_scores(evidence: SearchEvidence, claims: list[Claim]) -> list[tuple[float, Claim]]:
    """一条证据与每条说法的文字重合度，按高到低排。"""
    scored = [(_overlap(evidence.content, claim.content), claim) for claim in claims]
    return sorted(scored, key=lambda item: item[0], reverse=True)


def _rule_assessment(
    evidence: SearchEvidence,
    claims: list[Claim],
    question: str,
    mode: str,
) -> EvidenceAssessment:
    """按规则给出关系、支持程度与整合动作。"""
    scored = _claim_scores(evidence, claims)
    best_score, best_claim = scored[0] if scored else (0.0, None)
    if best_claim is None or best_score < RELEVANT_FLOOR:
        return EvidenceAssessment(
            evidence_id=evidence.id,
            related_claim_ids=[],
            relation="irrelevant",
            support_level="none",
            integration_action="reject",
            rationale="与现有说法没有对应关系",
        )

    related = [claim.id for score, claim in scored if score >= RELEVANT_FLOOR][:CLAIM_LINKS]
    contradictory = _contradicts(evidence.content, best_claim.content)
    if contradictory and best_score >= AMBIGUOUS_LOW:
        relation = "contradictory"
        level = "direct" if best_score >= DIRECT_FLOOR else "partial"
        action = "revise"
        reason = f"与说法「{best_claim.id}」方向相反"
    elif best_score >= DIRECT_FLOOR:
        relation = "supporting"
        level = "direct"
        action = "strengthen"
        reason = f"直接支持说法「{best_claim.id}」"
    else:
        relation = "supplementary"
        level = "partial" if best_score >= AMBIGUOUS_LOW else "contextual"
        action = "append"
        reason = f"补充说法「{best_claim.id}」的背景"

    # 四项分数留在 metadata 里：后面排序与报告都读它，不重新算一遍。
    direct = min(1.0, best_score / DIRECT_FLOOR) if DIRECT_FLOOR else 0.0
    freshness = _freshness(question, evidence.published_at)
    evidence.metadata.update(
        {
            "relevance": round(best_score, 3),
            "freshness": round(freshness, 3),
            "direct": round(direct, 3),
            "quality": round(
                policy.quality(
                    evidence,
                    policy.weights_for(mode),
                    freshness=freshness,
                    direct=direct,
                ),
                3,
            ),
        }
    )
    return EvidenceAssessment(
        evidence_id=evidence.id,
        related_claim_ids=related,
        relation=relation,
        support_level=level,
        integration_action=action,
        rationale=reason,
    )


def _ambiguous(assessment: EvidenceAssessment, evidence: SearchEvidence) -> bool:
    """这条评估是否规则判不准、值得让模型复核。"""
    score = float(evidence.metadata.get("relevance") or 0.0)
    return AMBIGUOUS_LOW <= score < AMBIGUOUS_HIGH


async def _refine(
    ambiguous: list[tuple[EvidenceAssessment, SearchEvidence]],
    claims: list[Claim],
    question: str,
    ask: Ask,
) -> list[EvidenceAssessment]:
    """把判不准的几条交给一次模型请求复核。"""
    payload_claims = [
        {"id": claim.id, "content": claim.content, "category": claim.category}
        for claim in claims
    ]
    payload_evidence = [
        {
            "id": item.id,
            "content": item.content[:600],
            "source": item.source,
            "source_type": item.source_type,
        }
        for _, item in ambiguous
    ]
    messages = [
        {"role": "system", "content": prompts.ASSESS_SYSTEM},
        {
            "role": "user",
            "content": prompts.ASSESS_USER.format(
                question=question,
                claims=prompts.dumps(payload_claims),
                evidence=prompts.dumps(payload_evidence),
            ),
        },
    ]
    data = parse_json(await ask(messages), "证据评估")
    rows = data.get("assessments")
    if not isinstance(rows, list):
        raise SearchReasoningError("证据评估没有给出 assessments 数组")
    by_id = {item.id for _, item in ambiguous}
    refined: list[EvidenceAssessment] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        evidence_id = str(row.get("evidence_id") or "").strip()
        if evidence_id not in by_id:
            continue
        relation = str(row.get("relation") or "").strip()
        if relation not in RELATIONS:
            raise SearchReasoningError(f"证据 {evidence_id} 的关系无法识别：{relation!r}")
        refined.append(
            EvidenceAssessment(
                evidence_id=evidence_id,
                related_claim_ids=[
                    str(item) for item in (row.get("related_claim_ids") or [])
                ],
                relation=relation,
                support_level=row.get("support_level", "contextual"),
                integration_action=row.get("integration_action", "append"),
                rationale=str(row.get("rationale") or ""),
            )
        )
    return refined


async def assess(
    state: AnswerState,
    *,
    question: str = "",
    ask: Ask | None = None,
    refine: bool = False,
) -> dict[str, int]:
    """给证据池里的每一条证据写一份评估，返回各类关系的条数。

    `refine=False` 时只跑规则，并跳过已经复核过的证据；收尾阶段传
    `refine=True`，让模型重新看一遍规则判不准的条目。第一次检索后只跑规则，
    避免下一步之前连续两次模型请求。
    """
    claims = state.current_claims()
    if not claims or not state.evidence_pool:
        return {relation: 0 for relation in RELATIONS}

    assessments: list[EvidenceAssessment] = []
    ambiguous: list[tuple[EvidenceAssessment, SearchEvidence]] = []
    known = {item.evidence_id for item in state.assessments}
    for evidence in state.evidence_pool:
        assessment = _rule_assessment(evidence, claims, question or state.question, state.mode)
        assessments.append(assessment)
        if (
            ask is not None
            and (refine or evidence.id not in known)
            and _ambiguous(assessment, evidence)
        ):
            ambiguous.append((assessment, evidence))

    if ambiguous and ask is not None:
        refined = await _refine(ambiguous, claims, question or state.question, ask)
        replaced = {item.evidence_id: item for item in refined}
        assessments = [replaced.get(item.evidence_id, item) for item in assessments]

    for assessment in assessments:
        state.add_assessment(assessment)
    counts = {relation: 0 for relation in RELATIONS}
    for assessment in assessments:
        counts[assessment.relation] = counts.get(assessment.relation, 0) + 1
    _note_conflicts(state, assessments)
    return counts


def _note_conflicts(
    state: AnswerState, assessments: list[EvidenceAssessment]
) -> None:
    """把规则判出的相反结论记进冲突列表。

    整合阶段还会补一份逐项比较的冲突；规则这一份是保底：模型没有写出冲突时，
    用户仍然能在答案状态里看到「某条说法有相反证据」。
    """
    pool = {item.id: item for item in state.evidence_pool}
    claims = {item.id: item for item in state.current_claims()}
    found: list[dict[str, Any]] = []
    for assessment in assessments:
        if assessment.relation != "contradictory" or not assessment.related_claim_ids:
            continue
        evidence = pool.get(assessment.evidence_id)
        claim = claims.get(assessment.related_claim_ids[0])
        if evidence is None or claim is None:
            continue
        found.append(
            {
                "topic": claim.content[:60],
                "sides": [evidence.source, "初始说法"],
                "condition": "证据与说法方向相反，待按数据集与实验条件比较",
                "source": "assess",
            }
        )
    # 同一说法下，两条来源给出不同的对象编号（例如配体代号不同）：
    # 两条都保留，记成一组待核对的冲突，不替用户选一份。
    groups: dict[str, list[SearchEvidence]] = {}
    for assessment in assessments:
        if assessment.relation not in ("supporting", "supplementary"):
            continue
        evidence = pool.get(assessment.evidence_id)
        if evidence is None:
            continue
        for claim_id in assessment.related_claim_ids[:1]:
            groups.setdefault(claim_id, []).append(evidence)
    for claim_id, items in groups.items():
        if len(items) < 2:
            continue
        codes = [set(CODE.findall(item.content)) for item in items]
        codes = [item for item in codes if item]
        if len(codes) < 2:
            continue
        if len(set().union(*codes)) <= 1:
            continue
        claim = claims.get(claim_id)
        if claim is None:
            continue
        found.append(
            {
                "topic": claim.content[:60],
                "sides": [item.source for item in items[:4]],
                "condition": "不同来源对同一对象给出不同编号，待核对",
                "source": "assess",
            }
        )
    # 每次评估重建规则这一份，模型写的那一份保留。
    kept = [
        item for item in state.conflicts
        if str(item.get("source") or "") != "assess"
    ]
    state.conflicts = kept + found[:5]


def assessment_for(state: AnswerState, evidence_id: str) -> EvidenceAssessment | None:
    """取某条证据的评估。"""
    for item in state.assessments:
        if item.evidence_id == evidence_id:
            return item
    return None


def select_for_context(state: AnswerState, limit: int = 0) -> list[SearchEvidence]:
    """挑出要放进上下文的证据：去掉无关与低质量，按综合分排序。

    完整证据池留在答案状态里；上下文每一步只带这几条，避免把几十条
    查询结果重复注入每一次请求。
    """
    cap = limit or policy.EVIDENCE_IN_CONTEXT
    ranked: list[tuple[float, SearchEvidence]] = []
    for evidence in state.evidence_pool:
        assessment = assessment_for(state, evidence.id)
        if assessment is None or assessment.relation == "irrelevant":
            continue
        score = float(evidence.metadata.get("quality") or 0.0)
        ranked.append((score, evidence))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [evidence for _, evidence in ranked[:cap]]
