"""搜索增强推理的事件名与载荷。

八类事件都走事件总线，由 `TraceWriter` 写成 JSONL。载荷只带编号与计数，
证据正文从答案状态文件里读：把正文抄进事件流，一次查询就能把日志撑大几倍。
"""

from __future__ import annotations

from typing import Any

from .models import AnswerState

RETRIEVAL_OBSERVED = "retrieval_observed"
ANSWER_INITIALIZED = "answer_initialized"
EVIDENCE_COLLECTED = "evidence_collected"
EVIDENCE_ASSESSED = "evidence_assessed"
CLAIM_UPDATED = "claim_updated"
ANSWER_VERIFIED = "answer_verified"
ANSWER_FINALIZED = "answer_finalized"

EVENT_NAMES = (
    RETRIEVAL_OBSERVED,
    ANSWER_INITIALIZED,
    EVIDENCE_COLLECTED,
    EVIDENCE_ASSESSED,
    CLAIM_UPDATED,
    ANSWER_VERIFIED,
    ANSWER_FINALIZED,
)


def base(state: AnswerState, session_id: str) -> dict[str, Any]:
    """每条事件共有的部分：会话、答案编号、模式与轮次。"""
    return {
        "session_id": session_id,
        "answer_id": state.answer_id,
        "mode": state.mode,
        "revision": state.revision,
    }


def observed(
    session_id: str, tool_name: str, step: int, count: int, total: int
) -> dict[str, Any]:
    """第一次检索结果返回。`count` 是这一步转换出的条数，`total` 是当前证据池。"""
    return {
        "session_id": session_id,
        "tool": tool_name,
        "step": step,
        "count": count,
        "total": total,
    }


def initialized(state: AnswerState, session_id: str) -> dict[str, Any]:
    return {
        **base(state, session_id),
        "baseline_source": state.baseline_source,
        "trigger_tool": state.trigger_tool,
        "trigger_step": state.trigger_step,
        "claims": len(state.initial_claims),
        "gaps": len(state.knowledge_gaps),
    }


def collected(state: AnswerState, session_id: str, added: int, total: int) -> dict[str, Any]:
    return {**base(state, session_id), "added": added, "total": total}


def assessed(state: AnswerState, session_id: str, counts: dict[str, int]) -> dict[str, Any]:
    return {**base(state, session_id), "relations": dict(counts)}


def updated(state: AnswerState, session_id: str, actions: dict[str, int]) -> dict[str, Any]:
    return {**base(state, session_id), "actions": dict(actions)}


def verified(state: AnswerState, session_id: str, passed: bool, issues: list[str]) -> dict[str, Any]:
    return {
        **base(state, session_id),
        "passed": passed,
        "issues": len(issues),
    }


def finalized(state: AnswerState, session_id: str, chars: int) -> dict[str, Any]:
    return {**base(state, session_id), "chars": chars}
