"""上下文解析：给意图解析器一份短而准的现场说明。

它回答四件事：

- 当前任务是什么，做到哪一版，状态如何。
- 已经有哪些科研对象，引用怎么写。
- 已经有哪些产物，能不能直接拿来比较或分析。
- 上一轮有没有没回答完的问题。

**不传完整聊天历史。** 解析器只需要认出“它”“之前那个”“第二个口袋”指向什么，
把引用表给它就够了。四段各有上限，总量控制在一千五百字以内。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from vsagent.core.agent.planning.task_context import context_line
from vsagent.core.agent.roles.contracts import TaskSpec
from vsagent.core.models import PartType

TASK_LIMIT = 500
OBJECT_LIMIT = 500
ARTIFACT_LIMIT = 300
PENDING_LIMIT = 200
RECENT_LIMIT = 150
TOTAL_LIMIT = 1_500
OBJECT_ROWS = 20
ARTIFACT_ROWS = 10

OBJECT_KINDS: frozenset[str] = frozenset(
    {"target", "structure", "pocket", "ligand", "pose", "score", "hypothesis"}
)
ARTIFACT_KINDS: frozenset[str] = frozenset(
    {"artifact", "report", "receptor", "ligandset", "pose", "score"}
)


@dataclass(frozen=True)
class ContextBundle:
    """解析器看到的现场。`refs` 供验证器核对引用是否真的存在。"""

    text: str
    refs: frozenset[str]
    task: str = ""
    objects: str = ""
    artifacts: str = ""
    pending: str = ""


class ContextResolver:
    """从会话、登记表与待澄清意图里拼出四段上下文。纯读取，不写状态。"""

    def resolve(
        self,
        *,
        session: Any = None,
        text: str = "",
        current: TaskSpec | None = None,
        ledger: Any = None,
        workspace: Any = None,
    ) -> ContextBundle:
        task_text = _clip(context_line(current), TASK_LIMIT)
        pending_text = _pending(session)
        objects, artifacts, refs, recent = _entries(session, ledger, current)
        blocks: list[str] = []
        if task_text:
            blocks.append(f"当前任务：\n{task_text}")
        if pending_text:
            blocks.append(f"上一轮待回答：\n{pending_text}")
        if recent:
            blocks.append(f"最近提到的对象：\n{recent}")
        if objects:
            blocks.append(f"已有科研对象（引用时写 ref）：\n{objects}")
        if artifacts:
            blocks.append(f"已有产物：\n{artifacts}")
        if not blocks:
            blocks.append("当前没有任务，也没有已登记的科研对象。")
        body = "\n\n".join(blocks)
        return ContextBundle(
            text=_clip(body, TOTAL_LIMIT),
            refs=frozenset(refs),
            task=task_text,
            objects=objects,
            artifacts=artifacts,
            pending=pending_text,
        )


def _pending(session: Any) -> str:
    raw = getattr(session, "pending_intent", None)
    if not isinstance(raw, dict):
        return ""
    questions = raw.get("clarification") or raw.get("questions") or ()
    if isinstance(questions, str):
        questions = (questions,)
    if questions:
        return _clip("；".join(str(item) for item in questions), PENDING_LIMIT)
    goal = str(raw.get("goal") or "").strip()
    if goal:
        return _clip(f"上一轮想做的事：{goal}", PENDING_LIMIT)
    return ""


def _entries(
    session: Any, ledger: Any, current: TaskSpec | None
) -> tuple[str, str, set[str], str]:
    """从登记表取对象与产物两段，并返回所有可用引用与最近提到的对象。"""
    if ledger is None:
        return "", "", set(), ""
    try:
        rows: list[Any] = list(ledger.entries())
    except Exception:  # noqa: BLE001 - 读登记表失败不该让解析起不来
        return "", "", set(), ""

    objects: list[str] = []
    artifacts: list[str] = []
    refs: set[str] = set()
    counters: dict[str, int] = {}

    for entry in rows:
        handle = getattr(entry, "handle", None)
        if handle is None:
            continue
        kind = str(getattr(handle, "kind", "") or "").strip()
        reference = str(handle)
        summary = str(getattr(entry, "summary", "") or "").strip()
        path = getattr(entry, "path", None)
        refs.add(reference)
        if kind in OBJECT_KINDS:
            counters[kind] = counters.get(kind, 0) + 1
            note = f"{kind}，第{counters[kind]}个"
            if summary:
                note += f"，{summary}"
            objects.append(f"- {reference}（{note}）")
            continue
        if kind in ARTIFACT_KINDS or path is not None:
            note = summary or str(path or "")
            artifacts.append(f"- {reference}（{kind}，{note}）" if note else f"- {reference}（{kind}）")

    if current is not None:
        # 只收形如 `kind#序号` 的引用；自由文本标签不能当成已存在的对象。
        for value in current.research_scope.values():
            text = str(value)
            if "#" in text:
                refs.add(text)

    objects_text = _clip("\n".join(objects[:OBJECT_ROWS]), OBJECT_LIMIT)
    artifacts_text = _clip("\n".join(artifacts[:ARTIFACT_ROWS]), ARTIFACT_LIMIT)
    recent_text = _clip(_recent(session, rows), RECENT_LIMIT)
    if len(objects) > OBJECT_ROWS:
        objects_text += f"\n（还有 {len(objects) - OBJECT_ROWS} 个对象，可按需查询）"
    if len(artifacts) > ARTIFACT_ROWS:
        artifacts_text += f"\n（还有 {len(artifacts) - ARTIFACT_ROWS} 个产物，可按需查询）"
    return objects_text, artifacts_text, refs, recent_text


def _recent(session: Any, rows: list[Any]) -> str:
    """最近几条消息里出现过的对象引用。只做字面匹配，猜不到就留空。"""
    texts: list[str] = []
    for message in reversed(getattr(session, "messages", []) or []):
        for part in reversed(getattr(message, "parts", []) or []):
            if getattr(part, "type", None) is PartType.TEXT and getattr(part, "text", ""):
                texts.append(str(part.text))
        if len(texts) >= 6:
            break
    if not texts:
        return ""
    blob = "\n".join(texts)
    hits: list[str] = []
    for entry in rows:
        reference = str(getattr(entry, "handle", ""))
        summary = str(getattr(entry, "summary", "") or "")
        if reference and reference in blob:
            hits.append(reference)
        elif len(summary) >= 4 and summary in blob:
            hits.append(reference)
        if len(hits) >= 5:
            break
    return "、".join(item for item in hits if item)


def _clip(text: str, limit: int) -> str:
    body = " ".join(str(text).split())
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def refs_for(entries: Iterable[Any]) -> frozenset[str]:
    """给测试用：把一组 handle 变成引用集合。"""
    return frozenset(str(item) for item in entries)
