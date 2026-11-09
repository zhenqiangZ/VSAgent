from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable

from .lifecycle import overlap
from .models import MEMORY_TEXT_LIMIT, MemoryError, MemoryKind, parse_kind
from .store import MemoryStore

# 抽取总开关。默认关闭：台账里现在只有两条，抽取的价值还没有样本。
EXTRACT_ENV = "VSA_MEMORY_EXTRACT"
# 与已有条目重合到这个程度就当成同一件事，不再写一条同义改写。
EXTRACT_DUP_OVERLAP = 0.85
# 有引用文件时的可信度。抽取没读过原文，只是从流水里推断的。
EXTRACT_CONFIDENCE = 0.6

SCHEMA_HINT = (
    '{"memories": [{"text": "一行事实", "kind": "fact|experience", '
    '"refs": ["文件路径"], "confidence": 0.0-1.0, "expires_days": 0}]}'
)


def enabled() -> bool:
    """抽取是否打开。写坏或没写都按关闭处理。"""
    raw = (os.environ.get(EXTRACT_ENV) or "").strip().lower()
    if not raw:
        return False
    return raw in ("1", "true", "yes", "on")


def prompt_for(event: Any) -> str:
    """把一段封存的主题摊成给本地模型看的结构化输入。

    只给流水与结论，不给原文：原文里可能夹着大段工具输出，本地模型读不完，
    而「这一轮做了什么、留下了什么、哪一步失败」在 Step 里都已经有了。
    """
    lines = [
        f"主题：{event.title or '（未命名）'}",
        f"结论：{event.summary or '（没有写）'}",
    ]
    if event.exit_reason:
        lines.append(f"停因：{event.exit_reason}")
    lines.append("步骤：")
    for step in event.steps:
        body = step.intent or "（没有记录）"
        if step.result:
            body += " · " + step.result
        lines.append(f"{step.seq}. {body}")
    artifacts = sorted({ref for step in event.steps for ref in step.artifacts})
    if artifacts:
        lines.append("产物：" + "、".join(artifacts))
    return "\n".join(lines)


def _has_substance(text: str) -> bool:
    """有没有可以核对的东西：数字、路径、或一个像名字的英文串。

    「今天先到这里」这类流水记下来只会占额度；判据要能把它们挡在外面。
    """
    if re.search(r"\d", text):
        return True
    if re.search(r"[\\/]", text):
        return True
    return bool(re.search(r"[A-Za-z][A-Za-z0-9_.\-]{2,}", text))


def _clean_refs(event_root: Any, raw: Any) -> list[str]:
    """只留盘上真的存在的引用。handle 一律丢掉：它换个会话就指向别的东西。"""
    if not isinstance(raw, list):
        return []
    kept: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if "#" in text and "/" not in text and "\\" not in text:
            continue
        target = event_root / text
        if target.exists() and text not in kept:
            kept.append(text)
    return kept


def apply(
    store: MemoryStore, event: Any, payload: dict | None, session_id: str = ""
) -> dict:
    """把一次抽取的结果写进记忆台账，返回写入与丢弃的条数。"""
    if not payload:
        return {"written": 0, "dropped": 0}
    items = payload.get("memories")
    if not isinstance(items, list):
        return {"written": 0, "dropped": 1}

    written = 0
    dropped = 0
    for raw in items:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        text = " ".join(str(raw.get("text") or "").split())
        if not text or len(text) > MEMORY_TEXT_LIMIT or not _has_substance(text):
            dropped += 1
            continue
        try:
            kind = parse_kind(raw.get("kind") or MemoryKind.FACT.value)
        except MemoryError:
            kind = MemoryKind.FACT
        # 习惯只能来自用户明确表态，一段工具流水推不出来。
        if kind is MemoryKind.PREFERENCE:
            kind = MemoryKind.FACT
        refs = _clean_refs(store.workspace_root, raw.get("refs"))
        live = [entry for entry in store.entries.values() if entry.is_live()]
        if any(overlap(text, entry.text) >= EXTRACT_DUP_OVERLAP for entry in live):
            dropped += 1
            continue
        raw_confidence = raw.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            confidence = EXTRACT_CONFIDENCE if refs else 0.3
        else:
            confidence = max(0.0, min(1.0, float(raw_confidence)))
            if not refs:
                confidence = min(confidence, 0.3)
        raw_days = raw.get("expires_days")
        days = float(raw_days) if isinstance(raw_days, (int, float)) else 0.0
        try:
            entry = store.remember(
                text,
                refs,
                kind=kind,
                source="local-extractor",
                ttl_days=days,
                session_id=session_id,
                turn=int(getattr(event, "step_end", 0) or 0),
            )
            entry.confidence = confidence
        except MemoryError:
            dropped += 1
            continue
        written += 1
    store.save()
    return {"written": written, "dropped": dropped}


class ExtractionQueue:
    """单线程消费的有界队列。同一会话的新任务替换旧任务。

    替换而不是排队：后一次封存的 Event 已经包含前一次的结论，先跑旧的那条
    只会写出更少的信息。容量满了丢**最新**的——丢旧的会让刚封的主题永远不被抽取。
    """

    def __init__(self, capacity: int = 8) -> None:
        self.capacity = capacity
        self.dropped = 0
        self._jobs: dict[str, Callable[[], None]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = False

    def submit(self, key: str, job: Callable[[], None]) -> None:
        with self._lock:
            if key not in self._jobs and len(self._jobs) >= self.capacity:
                self.dropped += 1
                return
            self._jobs[key] = job
            self._stop = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stop or not self._jobs:
                    self._thread = None
                    return
                key = next(iter(self._jobs))
                job = self._jobs.pop(key)
            job()

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)


_QUEUE = ExtractionQueue()


def queue() -> ExtractionQueue:
    """应用级的那个队列。抽取是进程内的后台活，整个进程一份就够。"""
    return _QUEUE


def submit(
    event: Any,
    store: MemoryStore | None,
    *,
    session_id: str = "",
    complete: Callable[..., dict | None] | None = None,
    work_queue: ExtractionQueue | None = None,
) -> None:
    """把一段封存的主题排进抽取队列。没开开关或没装配记忆时什么都不做。"""
    if store is None or not enabled() or not event.steps:
        return
    if complete is None:
        from ..local_llm import complete_json

        complete = complete_json
    prompt = prompt_for(event)
    target = work_queue or _QUEUE

    def job() -> None:
        payload = complete(prompt, schema_hint=SCHEMA_HINT)
        apply(store, event, payload, session_id)

    target.submit(str(getattr(event, "id", "") or id(event)), job)
