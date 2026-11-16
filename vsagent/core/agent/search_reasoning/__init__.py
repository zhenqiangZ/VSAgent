"""搜索增强推理（SAR）。

查询只补充与校正答案，不接管回答权。这个包负责四件事：

- `answer_state`：第一次检索结果返回后，先在结果进入上下文之前写下先验答案；
- `evidence`：把检索结果整理成证据、去重、评估、映射到说法；
- `integrator` / `verifier`：选择性整合与交付前核对；
- `jsonio` / `prompts` / `policy` / `events`：JSON 读取、提示、阈值与事件。

原有查询工具继续取数，`AgentLoop` 仍是唯一的执行循环。这个包不持有会话、
不改工具权限、不训练模型。
"""

from __future__ import annotations

from .answer_state import (
    answers_directory,
    create_state,
    capture_prior,
    latest_answer,
    load_answer,
    new_answer_id,
    save_answer,
)
from .evidence import assess, collect, select_for_context
from .integrator import integrate
from .models import (
    CLAIM_CATEGORIES,
    CLAIM_STATUSES,
    EVIDENCE_TYPES,
    INTEGRATION_ACTIONS,
    RELATIONS,
    SUPPORT_LEVELS,
    AnswerState,
    Claim,
    EvidenceAssessment,
    SearchEvidence,
    SearchReasoningError,
)
from .verifier import VerifyReport, verify, verify_and_repair

__all__ = [
    "CLAIM_CATEGORIES",
    "CLAIM_STATUSES",
    "EVIDENCE_TYPES",
    "INTEGRATION_ACTIONS",
    "RELATIONS",
    "SUPPORT_LEVELS",
    "AnswerState",
    "Claim",
    "EvidenceAssessment",
    "SearchEvidence",
    "SearchReasoningError",
    "VerifyReport",
    "answers_directory",
    "assess",
    "collect",
    "create_state",
    "capture_prior",
    "integrate",
    "latest_answer",
    "load_answer",
    "new_answer_id",
    "save_answer",
    "select_for_context",
    "verify",
    "verify_and_repair",
]
