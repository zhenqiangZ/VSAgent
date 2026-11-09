from __future__ import annotations

from typing import Any

from ..models import Event, Session
from ..text import terms
from .tools import Tool, ToolResult, ToolSpec

HISTORY_LIMIT = 8
HISTORY_LIMIT_MAX = 20
# 跨会话时每个会话最多出几条。一个长会话里同一件事反复出现，不设这个上限
# 会把名额全占掉，别处的命中一条都进不来。
PER_SESSION_HITS = 3
# 各字段的分量。标题与结论是本地或模型写过的一句话，命中它最说明「就是这个主题」；
# 流水是一步一步的过程记录，命中它只能说明「这里提到过」。
TITLE_WEIGHT = 4.0
SUMMARY_WEIGHT = 3.0
ARTIFACT_WEIGHT = 2.0
HANDOFF_WEIGHT = 1.5
STEP_WEIGHT = 1.0
# 每段正文给模型看多少字。完整原文用 recall_event 取，这里只给能认出来的那一截。
TITLE_CHARS = 40
SUMMARY_CHARS = 120
LINE_CHARS = 60


def _short(text: str, limit: int) -> str:
    """压成一行，超长截断并留一个省略号。"""
    plain = " ".join(str(text).split())
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


def _segments(event: Event) -> list[tuple[str, float]]:
    """一个主题里可以被查到的文字，以及各段的分量。"""
    parts: list[tuple[str, float]] = [
        (event.title, TITLE_WEIGHT),
        (event.summary, SUMMARY_WEIGHT),
        (event.handoff, HANDOFF_WEIGHT),
        (" ".join(event.artifacts), ARTIFACT_WEIGHT),
        (" ".join(event.issues), ARTIFACT_WEIGHT),
    ]
    steps = " ".join(
        f"{step.intent} {step.result} {' '.join(step.tools)}" for step in event.steps
    )
    parts.append((steps, STEP_WEIGHT))
    return [(text.lower(), weight) for text, weight in parts if text.strip()]


def score_event(event: Event, words: list[str]) -> float:
    """一个主题对这次查询的得分：每个词只按它命中的最高分量算一次。

    不让长主题凭段数多赢：同一段文字里出现十次与出现一次，说明的是同一件事。
    """
    if not words:
        return 0.0
    segments = _segments(event)
    total = 0.0
    for word in words:
        weight = max((size for text, size in segments if word in text), default=0.0)
        total += weight
    return total


def render_hit(event: Event, session_id: str = "") -> str:
    """一个命中主题的几行：编号、标题、结论、产物、未决与原文位置。

    跨会话命中必须带会话编号：`evt_0007` 在每个会话里都存在，单给编号
    在本会话里指不到那一条，取回时会拿错主题。
    """
    start = event.step_start
    span = f"第 {start}–{event.step_end - 1} 步" if event.step_end > start else "还没有步"
    head = f"{event.id} · 主题 {event.topic_index} · {_short(event.title, TITLE_CHARS)}"
    lines = [f"{session_id} · {head}" if session_id else head]
    lines.append(f"  结论：{_short(event.summary, SUMMARY_CHARS) if event.summary else '（没有结论）'}")
    if event.artifacts:
        shown = "、".join(event.artifacts[:4])
        if len(event.artifacts) > 4:
            shown += f" 等 {len(event.artifacts)} 件"
        lines.append(f"  产物：{shown}")
    if event.issues:
        lines.append(f"  未决：{_short('；'.join(event.issues[:3]), LINE_CHARS)}")
    if session_id:
        where = f"原文用 recall_event session_id={session_id} event={event.id} 取回"
    else:
        where = f"原文用 recall_event {event.id} 取回"
    lines.append(f"  位置：止于第 {event.cut_message + 1} 条消息，{span}；{where}")
    return "\n".join(lines)


class SearchHistoryTool(Tool):
    """按内容检索已经封存的主题。

    事件层按编号浏览（`recall_event`）已经能做，但用户问「之前那个结合位点的事」
    时手上没有编号。这里把每个主题的标题、结论、产物、未决与每步的过程记录
    拼成可查的文字，先找到编号，再交给 `recall_event` 取原文。

    默认只查当前会话。`scope="all"` 查这台机器上的全部根会话，子 agent 的会话
    不查：它们的主题是父会话任务的碎片，标题与结论会与父会话重复。
    """

    spec = ToolSpec(
        name="search_history",
        description=(
            "按关键词检索已封存的历史主题，返回主题编号、结论、产物与原文位置。"
            "用户提到「上次」「之前」「刚才讨论过的」而你说不出是哪一段时用它；"
            "默认只查本会话；scope=all 查这台机器上的全部会话（不含子 agent），"
            "命中行带会话编号，原文用 recall_event session_id=… event=… 取回。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查的词，例如「结合位点」「对接分数」「4HJO」",
                },
                "scope": {
                    "type": "string",
                    "enum": ["current", "all"],
                    "description": "current 只查本会话（默认）；all 查全部会话",
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多返回几个主题，默认 {HISTORY_LIMIT}，上限 {HISTORY_LIMIT_MAX}",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    session_argument = "session"

    def __init__(self, sessions: Any = None) -> None:
        # 跨会话检索要看到别的会话，读的是 SessionManager 的内存表。
        # 不传时只查当前会话；`scope=all` 直接报装配问题，不退回本会话——
        # 退回去会让模型以为别的会话里没有相关内容。
        self.sessions = sessions

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """按词检索主题记录。"""
        session = arguments.get("session")
        raw = arguments.get("query")
        query = raw.strip() if isinstance(raw, str) else ""
        if not query:
            return ToolResult(
                output="参数 query 不能为空。要看全部主题用 recall_event（不带参数）。",
                failed=True,
            )
        given = arguments.get("limit")
        limit = HISTORY_LIMIT if given is None else given
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return ToolResult(output="参数 limit 应当是正整数", failed=True)
        limit = min(limit, HISTORY_LIMIT_MAX)
        scope = arguments.get("scope") or "current"
        if scope not in ("current", "all"):
            return ToolResult(output="参数 scope 只能是 current 或 all", failed=True)
        words = terms(query.lower())
        if scope == "all":
            return self._search_all(query, words, limit)
        events: list[Event] = list(session.events)
        if not events:
            return ToolResult(output="这份会话还没有封存过主题，没有可查的历史。")
        ranked = [
            (score_event(event, words), event)
            for event in events
        ]
        ranked = [pair for pair in ranked if pair[0] > 0]
        if not ranked:
            return ToolResult(
                output=f"没有与「{query}」相关的已封存主题。本会话现在有 {len(events)} 个主题，"
                "可以用 recall_event 逐个看。"
            )
        ranked.sort(key=lambda pair: (-pair[0], -pair[1].topic_index))
        chosen = [event for _, event in ranked[:limit]]
        return ToolResult(
            output="\n".join(render_hit(event) for event in chosen),
            meta={"events": [event.id for event in chosen], "query": query, "scope": scope},
        )

    def _search_all(self, query: str, words: list[str], limit: int) -> ToolResult:
        """在全部根会话里按同一套字段权重排一次名次。"""
        if self.sessions is None:
            return ToolResult(
                output="跨会话检索没有装配会话管理器，只能查当前会话：去掉 scope 再试。",
                failed=True,
            )
        roots = [
            item for item in list(self.sessions.sessions.values()) if not item.is_subagent
        ]
        ranked: list[tuple[float, Session, Event]] = []
        for item in roots:
            for event in item.events:
                score = score_event(event, words)
                if score > 0:
                    ranked.append((score, item, event))
        if not ranked:
            total = sum(len(item.events) for item in roots)
            return ToolResult(
                output=f"没有与「{query}」相关的已封存主题。"
                f"这台机器上的 {len(roots)} 个会话共有 {total} 个主题。"
            )
        # 同分先看会话的新旧：用户说「上次」时，最近改过的那个更可能是。
        ranked.sort(
            key=lambda pair: (
                -pair[0],
                -pair[1].updated_at,
                pair[1].id,
                -pair[2].topic_index,
            )
        )
        chosen: list[tuple[Session, Event]] = []
        per_session: dict[str, int] = {}
        for _, item, event in ranked:
            if len(chosen) >= limit:
                break
            used = per_session.get(item.id, 0)
            if used >= PER_SESSION_HITS:
                continue
            per_session[item.id] = used + 1
            chosen.append((item, event))
        return ToolResult(
            output="\n".join(render_hit(event, item.id) for item, event in chosen),
            meta={
                "hits": [{"session": item.id, "event": event.id} for item, event in chosen],
                "query": query,
                "scope": "all",
            },
        )
