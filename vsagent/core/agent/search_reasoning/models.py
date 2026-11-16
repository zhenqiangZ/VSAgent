"""搜索增强推理用到的四个记录对象。

四个对象分别回答四个问题：

- `Claim`：这一轮回答里有哪些可以被单独核对的说法；
- `SearchEvidence`：查回来的一条结果，来源、时间与分数都留在上面；
- `EvidenceAssessment`：这条结果对哪条说法起什么作用；
- `AnswerState`：这一轮从初始答案到最终答复的完整演变。

取值都用元组固定，写入时校验。认不出的取值抛 `SearchReasoningError`，
由调用方按出错处理，避免一份写坏的状态继续往下走。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 三种答案状态。`rag` 是检索触发的状态：第一次检索结果返回后才建立。
ANSWER_MODES = ("augmented", "research", "rag")
# 先验答案从哪来。目前只有一种：第一次检索结果返回前单独写下。
BASELINE_SOURCES = ("prior",)

# 说法的五类。类别决定它能不能被某一类证据直接支持。
CLAIM_CATEGORIES = ("method", "fact", "mechanism", "prediction", "task")
# 说法的七种状态。初始阶段一律 `initial`，之后只由整合与核对改动。
CLAIM_STATUSES = (
    "initial",
    "supported",
    "partially_supported",
    "unverified",
    "conflicted",
    "revised",
    "rejected",
)

# 证据的来源类别。计算产物与模型预测单列：它们不套用文献证据的评价规则。
EVIDENCE_TYPES = (
    "literature",
    "web",
    "corpus",
    "database",
    "computational",
    "model_prediction",
)

# 一条证据与一条说法的四种关系。
RELATIONS = ("supporting", "supplementary", "contradictory", "irrelevant")
# 支持到什么程度：直接给出结论、只给一部分、只作背景、没有支持。
SUPPORT_LEVELS = ("direct", "partial", "contextual", "none")
# 整合时允许的五种动作。
INTEGRATION_ACTIONS = ("retain", "append", "strengthen", "revise", "reject")

# 一份答案状态的生命周期。
ANSWER_STATUSES = ("initial", "searching", "integrating", "verifying", "final", "failed")


class SearchReasoningError(Exception):
    """搜索增强推理的输入、状态或模型输出不合法。

    这一类错误不静默降级：调用方把它显示成当前轮的错误，用户看得见是哪一步
    没有走通。吞掉它会让界面显示一份看起来正常、实际没有核对过的答复。
    """


def _choice(value: Any, allowed: tuple[str, ...], label: str) -> str:
    """把一个取值收进固定集合；认不出时抛错并列出可用取值。"""
    text = str(value or "").strip()
    if text not in allowed:
        raise SearchReasoningError(
            f"{label}的取值无法识别：{value!r}；可用取值：{'、'.join(allowed)}"
        )
    return text


def _text(value: Any, label: str, *, required: bool = True) -> str:
    """读出必填或选填的文字字段。"""
    text = str(value or "").strip()
    if required and not text:
        raise SearchReasoningError(f"{label}不能为空")
    return text


def _score(value: Any, label: str) -> float | None:
    """读出一个 0 到 1 之间的分数；没有给时返回 None。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise SearchReasoningError(f"{label}要写成数字，收到 {value!r}") from error
    if number < 0.0 or number > 1.0:
        raise SearchReasoningError(f"{label}要在 0 到 1 之间，收到 {number}")
    return number


@dataclass
class Claim:
    """一条可以被单独核对的说法。"""

    id: str
    content: str
    category: str = "fact"
    confidence: float = 0.5
    evidence_ids: list[str] = field(default_factory=list)
    status: str = "initial"
    rationale: str = ""

    def __post_init__(self) -> None:
        self.id = _text(self.id, "说法编号")
        self.content = _text(self.content, "说法正文")
        self.category = _choice(self.category, CLAIM_CATEGORIES, "说法类别")
        self.status = _choice(self.status, CLAIM_STATUSES, "说法状态")
        score = _score(self.confidence, "说法置信度")
        self.confidence = 0.5 if score is None else score
        # 模型偶尔把编号写成字符串数组；统一收成去重后的字符串列表。
        self.evidence_ids = [
            str(item).strip() for item in (self.evidence_ids or []) if str(item).strip()
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "status": self.status,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            category=data.get("category", "fact"),
            confidence=data.get("confidence", 0.5),
            evidence_ids=list(data.get("evidence_ids") or []),
            status=data.get("status", "initial"),
            rationale=str(data.get("rationale") or ""),
        )

    def copy(self, **changes: Any) -> "Claim":
        """复制一份并换掉指定字段，用于修订时不改原对象。"""
        values = self.to_dict()
        values.update(changes)
        return Claim.from_dict(values)


@dataclass
class SearchEvidence:
    """一条查回来的结果，整理成统一形状。

    `retrieval_score` 是搜索引擎给的相关程度，`reliability` 是证据评估给的
    来源可靠程度。两个分数分开存：一个是「像不像在回答这个问题」，另一个是
    「这条来源本身可不可信」，混成一个数之后没法解释为什么被降权。
    """

    id: str
    content: str
    source: str
    source_type: str = "web"
    title: str = ""
    url: str = ""
    published_at: str = ""
    retrieval_score: float | None = None
    reliability: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = _text(self.id, "证据编号")
        self.content = _text(self.content, "证据正文")
        self.source = _text(self.source, "证据来源")
        self.source_type = _choice(self.source_type, EVIDENCE_TYPES, "证据类别")
        self.retrieval_score = _score(self.retrieval_score, "检索相关度")
        self.reliability = _score(self.reliability, "来源可靠度")
        self.metadata = dict(self.metadata or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "source_type": self.source_type,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "retrieval_score": self.retrieval_score,
            "reliability": self.reliability,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchEvidence":
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            source=data.get("source", ""),
            source_type=data.get("source_type", "web"),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            published_at=str(data.get("published_at") or ""),
            retrieval_score=data.get("retrieval_score"),
            reliability=data.get("reliability"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class EvidenceAssessment:
    """一条证据对一组说法的作用。

    一条证据可以对应多条说法，一条说法也可以对应多条证据。评估必须写清
    这条证据支持或反驳的是哪一条说法；挂不到具体说法的证据不允许改动说法。
    """

    evidence_id: str
    related_claim_ids: list[str] = field(default_factory=list)
    relation: str = "irrelevant"
    support_level: str = "none"
    integration_action: str = "reject"
    rationale: str = ""

    def __post_init__(self) -> None:
        self.evidence_id = _text(self.evidence_id, "评估的证据编号")
        self.related_claim_ids = [
            str(item).strip()
            for item in (self.related_claim_ids or [])
            if str(item).strip()
        ]
        self.relation = _choice(self.relation, RELATIONS, "证据关系")
        self.support_level = _choice(self.support_level, SUPPORT_LEVELS, "支持程度")
        self.integration_action = _choice(
            self.integration_action, INTEGRATION_ACTIONS, "整合动作"
        )
        if self.relation != "irrelevant" and not self.related_claim_ids:
            raise SearchReasoningError(
                f"证据 {self.evidence_id} 判成了 {self.relation}，但没有挂到任何说法"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "related_claim_ids": list(self.related_claim_ids),
            "relation": self.relation,
            "support_level": self.support_level,
            "integration_action": self.integration_action,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceAssessment":
        return cls(
            evidence_id=data.get("evidence_id", ""),
            related_claim_ids=list(data.get("related_claim_ids") or []),
            relation=data.get("relation", "irrelevant"),
            support_level=data.get("support_level", "none"),
            integration_action=data.get("integration_action", "reject"),
            rationale=str(data.get("rationale") or ""),
        )


@dataclass
class AnswerState:
    """一轮回答在查询前后的完整状态。

    与 `TaskState` 分开：`TaskState` 记任务进度、待办、阻塞与假设；
    `AnswerState` 记这一轮回答的初始框架、证据与改动。两者生命周期不同，
    混在一起之后「任务做到了哪」与「这一轮答得对不对」会互相覆盖。
    """

    answer_id: str
    question: str
    mode: str = "rag"
    initial_answer: str = ""
    # 先验答案的来源与触发位置，供报告与评测区分「检索前写下」与「第一版草稿」。
    baseline_source: str = "prior"
    trigger_tool: str = ""
    trigger_step: int = 0
    initial_claims: list[Claim] = field(default_factory=list)
    knowledge_gaps: list[str] = field(default_factory=list)
    # 规划阶段给出的查询问题。与缺口分开：缺口是「还不知道什么」，
    # 查询是「准备怎么去查」，多轮研究里要按它继续取数。
    queries: list[str] = field(default_factory=list)
    evidence_pool: list[SearchEvidence] = field(default_factory=list)
    assessments: list[EvidenceAssessment] = field(default_factory=list)
    revised_claims: list[Claim] = field(default_factory=list)
    # 互相冲突的结论。每条记主题、双方来源与各自成立的条件；
    # 整合阶段不替用户选一份，保留冲突供最终答复写条件。
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    revision: int = 0
    status: str = "initial"
    created_at: float = 0.0

    def __post_init__(self) -> None:
        self.answer_id = _text(self.answer_id, "答案编号")
        self.question = _text(self.question, "用户问题")
        self.mode = _choice(self.mode, ANSWER_MODES, "答案模式")
        self.baseline_source = _choice(
            self.baseline_source, BASELINE_SOURCES, "基线来源"
        )
        self.trigger_step = int(self.trigger_step or 0)
        self.status = _choice(self.status, ANSWER_STATUSES, "答案状态")
        if not self.created_at:
            import time

            self.created_at = time.time()

    def current_claims(self) -> list[Claim]:
        """当前有效的说法：已经修订过就用修订后的那一组。"""
        return list(self.revised_claims or self.initial_claims)

    def add_evidence(self, evidence: SearchEvidence) -> SearchEvidence:
        """把一条证据放进证据池；同一编号的旧条目被替换。"""
        for index, item in enumerate(self.evidence_pool):
            if item.id == evidence.id:
                self.evidence_pool[index] = evidence
                return evidence
        self.evidence_pool.append(evidence)
        return evidence

    def add_assessment(self, assessment: EvidenceAssessment) -> EvidenceAssessment:
        """把一条评估放进答案状态；同一证据的旧评估被替换。"""
        for index, item in enumerate(self.assessments):
            if item.evidence_id == assessment.evidence_id:
                self.assessments[index] = assessment
                return assessment
        self.assessments.append(assessment)
        return assessment

    def revise_claim(self, claim: Claim) -> Claim:
        """写入一条修订后的说法，修订号加一。

        第一次修订时先把初始说法整组复制过来，这样「哪些没动」在状态里
        看得见：只改一条时，其余说法的原样保留是可核对的。
        """
        if not self.revised_claims:
            self.revised_claims = [item.copy() for item in self.initial_claims]
        for index, item in enumerate(self.revised_claims):
            if item.id == claim.id:
                self.revised_claims[index] = claim
                break
        else:
            self.revised_claims.append(claim)
        self.revision += 1
        return claim

    def findings(self, limit: int = 0) -> list[dict[str, Any]]:
        """给报告用的一份摘要，不包含证据正文。"""
        claims = self.current_claims()
        if limit:
            claims = claims[:limit]
        return [
            {
                "id": item.id,
                "content": item.content,
                "category": item.category,
                "status": item.status,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in claims
        ]

    def snapshot(
        self,
        *,
        answer_limit: int = 400,
        claim_limit: int = 6,
        gap_limit: int = 5,
    ) -> dict[str, Any]:
        """放进上下文的短摘要。

        完整对象留在内存与独立文件里；上下文每一步只带这一小份，
        不把证据正文重复注入每一次请求。
        """
        answer = self.initial_answer.strip()
        if answer_limit and len(answer) > answer_limit:
            answer = answer[:answer_limit] + "…"
        return {
            "answer_id": self.answer_id,
            "mode": self.mode,
            "baseline_source": self.baseline_source,
            "trigger_tool": self.trigger_tool,
            "trigger_step": self.trigger_step,
            "status": self.status,
            "revision": self.revision,
            "initial_answer": answer,
            "claims": self.findings(claim_limit),
            "knowledge_gaps": self.knowledge_gaps[:gap_limit],
            "evidence_count": len(self.evidence_pool),
            "assessment_count": len(self.assessments),
            "conflict_count": len(self.conflicts),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_id": self.answer_id,
            "question": self.question,
            "mode": self.mode,
            "baseline_source": self.baseline_source,
            "trigger_tool": self.trigger_tool,
            "trigger_step": self.trigger_step,
            "initial_answer": self.initial_answer,
            "initial_claims": [item.to_dict() for item in self.initial_claims],
            "knowledge_gaps": list(self.knowledge_gaps),
            "queries": list(self.queries),
            "evidence_pool": [item.to_dict() for item in self.evidence_pool],
            "assessments": [item.to_dict() for item in self.assessments],
            "revised_claims": [item.to_dict() for item in self.revised_claims],
            "conflicts": [dict(item) for item in self.conflicts],
            "final_answer": self.final_answer,
            "revision": self.revision,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnswerState":
        return cls(
            answer_id=data.get("answer_id", ""),
            question=data.get("question", ""),
            mode=data.get("mode", "rag"),
            baseline_source=data.get("baseline_source") or "prior",
            trigger_tool=str(data.get("trigger_tool") or ""),
            trigger_step=int(data.get("trigger_step") or 0),
            initial_answer=str(data.get("initial_answer") or ""),
            initial_claims=[
                Claim.from_dict(item) for item in (data.get("initial_claims") or [])
            ],
            knowledge_gaps=[
                str(item) for item in (data.get("knowledge_gaps") or []) if str(item).strip()
            ],
            queries=[
                str(item) for item in (data.get("queries") or []) if str(item).strip()
            ],
            evidence_pool=[
                SearchEvidence.from_dict(item)
                for item in (data.get("evidence_pool") or [])
            ],
            assessments=[
                EvidenceAssessment.from_dict(item)
                for item in (data.get("assessments") or [])
            ],
            revised_claims=[
                Claim.from_dict(item) for item in (data.get("revised_claims") or [])
            ],
            conflicts=[
                dict(item) for item in (data.get("conflicts") or []) if isinstance(item, dict)
            ],
            final_answer=str(data.get("final_answer") or ""),
            revision=int(data.get("revision") or 0),
            status=data.get("status", "initial"),
            created_at=float(data.get("created_at") or 0.0),
        )
