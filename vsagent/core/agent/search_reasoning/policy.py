"""证据处理的阈值、权重与允许的整合动作。

数字集中在这里，不散落在评估与整合两个模块里。两处各写一份的话，
调整权重之后「评估时算的分」与「整合时判的分」会慢慢对不上。
"""

from __future__ import annotations

from .models import Claim, EvidenceAssessment, SearchEvidence

# 综合分的四项权重：相关度、来源可靠度、时间适用性、直接支持程度。
# 两种模式给两组：查一条近期事实与处理一组互相冲突的研究，看重的项不同。
MODE_WEIGHTS: dict[str, dict[str, float]] = {
    "augmented": {"relevance": 0.40, "reliability": 0.30, "freshness": 0.15, "direct": 0.15},
    "research": {"relevance": 0.30, "reliability": 0.25, "freshness": 0.15, "direct": 0.30},
    # 检索触发：证据先于正式答案到达，相关度与来源可靠度的分量更重。
    "rag": {"relevance": 0.35, "reliability": 0.30, "freshness": 0.15, "direct": 0.20},
}

# 每个查询方向保留几条高价值结果。
MODE_KEEP: dict[str, int] = {"augmented": 4, "research": 5, "rag": 5}

# 来源可靠度的基线。实验论文高于综述，综述高于预印本，预印本高于普通网页；
# 计算产物与模型预测单列，不按文献这一套排。
RELIABILITY_BASE: dict[str, float] = {
    "literature": 0.85,
    "database": 0.90,
    "corpus": 0.65,
    "web": 0.45,
    "computational": 0.70,
    "model_prediction": 0.40,
}

# 低于这个可靠度的单条证据不许单独推翻原说法。
OVERTURN_FLOOR = 0.60

# 一条证据的正文进入上下文时的上限（文字单位）。
EVIDENCE_EXCERPT = 600
# 上下文里最多放几条筛选后的证据。
EVIDENCE_IN_CONTEXT = 5
# 上下文里「本轮回答状态」与证据两段的上限。
ANSWER_LIMIT = 800
EVIDENCE_MAX = 6000

# 说法类别与证据类别的对应关系：预测与推断不能被文献写成实验事实。
PREDICTION_CATEGORIES = frozenset({"prediction"})
EXPERIMENTAL_TYPES = frozenset({"literature", "database", "corpus", "computational"})


def weights_for(mode: str) -> dict[str, float]:
    """按模式取权重；认不出的模式按 `augmented` 处理。"""
    return MODE_WEIGHTS.get(mode, MODE_WEIGHTS["augmented"])


def keep_for(mode: str) -> int:
    """按模式取每个方向保留的条数。"""
    return MODE_KEEP.get(mode, MODE_KEEP["augmented"])


def reliability_for(source_type: str) -> float:
    """按来源类别给可靠度基线。"""
    return RELIABILITY_BASE.get(source_type, 0.40)


def quality(evidence: SearchEvidence, weights: dict[str, float], *, freshness: float, direct: float) -> float:
    """四项加权成一个综合分，各项都在 0 到 1 之间。"""
    relevance = evidence.retrieval_score if evidence.retrieval_score is not None else 0.5
    reliability = (
        evidence.reliability if evidence.reliability is not None else reliability_for(evidence.source_type)
    )
    return (
        weights["relevance"] * relevance
        + weights["reliability"] * reliability
        + weights["freshness"] * freshness
        + weights["direct"] * direct
    )


def action_allowed(
    assessment: EvidenceAssessment,
    evidence: SearchEvidence,
    claim: Claim | None,
) -> tuple[bool, str]:
    """规则校验：这条评估能不能按它写的动作改说法。

    返回 `(是否允许, 不允许的理由)`。理由会写进评估的备注，方便事后看
    「为什么这条证据没有改动说法」。
    """
    if assessment.relation == "irrelevant":
        return False, "无关证据不改动说法"
    if claim is None:
        return False, "没有挂到具体说法"
    if assessment.relation == "contradictory":
        reliability = (
            evidence.reliability
            if evidence.reliability is not None
            else reliability_for(evidence.source_type)
        )
        if reliability < OVERTURN_FLOOR:
            return False, f"来源可靠度 {reliability:.2f} 低于推翻阈值 {OVERTURN_FLOOR:.2f}"
    if claim.category in PREDICTION_CATEGORIES and evidence.source_type in EXPERIMENTAL_TYPES:
        # 实验证据可以支持预测，但不能把预测直接写成实验结论。
        if assessment.integration_action == "strengthen":
            return False, "预测类说法不能用实验证据直接加强成事实"
    if assessment.integration_action in ("revise", "reject") and assessment.support_level == "none":
        return False, "没有直接支持的证据不能修改或拒绝说法"
    return True, ""
