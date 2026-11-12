from __future__ import annotations

import json
import time
from pathlib import Path

from ..models import PartType, ToolStatus
from .context import LAYER_ORDER

LAYERS = tuple(layer.value for layer in LAYER_ORDER)
"""上下文分层的名字，顺序与装配顺序一致。直接取枚举，避免这里另抄一份名单。"""

COMPAT_LAYERS = ("memory",)
"""旧 trace 把三类记忆合成一层 `memory`。不拆、不映射到新层——旧数据里
没有那次拆分，凭空造出来等于编造。只在确实有值时作为兼容层留下，由页面
标注「旧口径」。"""

STEP_LIMIT = 20
"""一个主题最多走多少步。超过这个数就是走到了主题步数上限。"""

REPORT_EVENTS = frozenset({"message.add", "turn.done", "agent.status", "context.metrics"})
"""分析要用的四类事件。

trace 是全集，一份长会话有十几万条；分析只关心这四类，整份留在内存里
没有用处。网页端按需取一次分析，内存占用要按会话大小算，所以这里先筛。
"""


def read_trace(path: Path, session_id: str) -> list[dict]:
    """读一个会话的 trace，只留分析要用的四类事件。

    文件按会话分，正常不会混进别的会话；`session_id` 仍然要核，因为
    `--session` 指错时读到的是另一份，把它的装配记录算进这一份会得出
    看起来合理、实际对不上的数字。
    """
    events: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(event.get("event") or "") not in REPORT_EVENTS:
                continue
            payload = event.get("payload") or {}
            owner = payload.get("session_id")
            if owner and owner != session_id:
                continue
            events.append(event)
    return events


def turn_windows(session, trace: list[dict]) -> tuple[list[dict], list[tuple[float, str]]]:
    """按用户消息切轮，时间取自 trace；trace 缺条目时退回消息自带的时间戳。"""
    added_at: dict[str, float] = {}
    done_at: dict[str, float] = {}
    last_at: dict[str, float] = {}
    status_at: list[tuple[float, str]] = []
    for event in trace:
        payload = event.get("payload") or {}
        message_id = payload.get("message_id")
        at = event.get("at")
        if at is None:
            continue
        if event.get("event") == "agent.status":
            status_at.append((at, payload.get("status", "")))
        if not message_id:
            continue
        if event.get("event") == "message.add":
            added_at[message_id] = at
        elif event.get("event") == "turn.done":
            done_at[message_id] = at
        last_at[message_id] = max(last_at.get(message_id, 0.0), at)

    windows = []
    for index, message in enumerate(session.messages):
        if message.role != "user":
            continue
        reply = None
        if index + 1 < len(session.messages) and session.messages[index + 1].role == "assistant":
            reply = session.messages[index + 1]
        start = added_at.get(message.id) or message.created_at
        end = None
        if reply is not None:
            end = done_at.get(reply.id) or last_at.get(reply.id)
        if end is None:
            later = next((m for m in session.messages[index + 1:] if m.role == "user"), None)
            if later is not None:
                # 没有 trace 时 `added_at` 是空的，退回下一条用户消息自带的时间。
                end = added_at.get(later.id) or later.created_at
            elif trace:
                end = trace[-1]["at"]
            else:
                end = start
        windows.append(
            {
                "index": len(windows) + 1,
                "user": message,
                "reply": reply,
                "start": start,
                "end": max(end, start),
            }
        )
    return windows, status_at


def tool_row(part) -> dict:
    failed = part.tool_status == ToolStatus.FAILED
    output = (part.tool_output or "").strip()
    if failed:
        # bash 的失败常常是「退出码非零但正文有用」，真正的原因在最后几行；
        # 其它工具把说明写在开头。
        if part.tool_name == "bash":
            lines = [line for line in output.splitlines() if line.strip()]
            excerpt = lines[-1] if lines else ""
        else:
            excerpt = output
    else:
        excerpt = ""
    return {
        "name": part.tool_name or "?",
        "status": part.tool_status.value if part.tool_status else "",
        "chars": len(part.tool_output or ""),
        "keys": list((part.tool_input or {}).keys()),
        "permission": part.permission or "",
        "error": excerpt[:300],
    }


def step_rows(reply, metrics: list[dict], end_at: float) -> list[dict]:
    rows: list[dict] = []
    current = None
    for part in reply.parts:
        if part.type == PartType.STEP_START:
            current = {
                "no": len(rows) + 1,
                "label": part.text or "",
                "tools": [],
                "reasoning": 0,
                "text": 0,
                "finish": "",
            }
            rows.append(current)
        elif current is not None:
            if part.type == PartType.TOOL:
                current["tools"].append(part.tool_name or "?")
            elif part.type == PartType.REASONING:
                current["reasoning"] += len(part.text or "")
            elif part.type == PartType.TEXT:
                current["text"] += len(part.text or "")
            elif part.type == PartType.STEP_FINISH:
                current["finish"] = part.text or ""
    for index, row in enumerate(rows):
        metric = metrics[index] if index < len(metrics) else None
        row["metric"] = metric
        if metric is not None:
            nxt = metrics[index + 1]["at"] if index + 1 < len(metrics) else end_at
            row["seconds"] = round(max(nxt - metric["at"], 0.0), 1)
        else:
            row["seconds"] = None
    return rows


def metric_row(payload: dict) -> dict:
    usage = payload.get("usage") or {}
    layers = {
        name: max(int(usage.get(name, 0) or 0), 0) for name in LAYERS
    }
    # 旧聚合层只在确实有值时保留；新 8 层数据里它是 0，不该凭空多出第 9 层。
    for name in COMPAT_LAYERS:
        value = max(int(usage.get(name, 0) or 0), 0)
        if value:
            layers[name] = value
    return {
        "at": payload.get("_at", 0.0),
        "step": payload.get("step", 0),
        "input": payload.get("input", 0),
        "cached": payload.get("cached", 0),
        "uncached": max(payload.get("input", 0) - payload.get("cached", 0), 0),
        "output": payload.get("output", 0),
        "hit": payload.get("cache_hit", 0.0),
        "total": payload.get("total", 0),
        "window": payload.get("window_used", 0.0),
        "degraded": payload.get("degraded") or [],
        "events": payload.get("events", 0),
        "evidence": payload.get("evidence_entries", 0),
        "usage": layers,
    }


def build(session, trace: list[dict], *, text_limit: int = 0) -> dict:
    """把一个会话汇成分析数据。

    `text_limit` 大于 0 时截断用户原话与助手正文。网页端只要一段引子，
    整份正文动辄几十万字；静态报告要原文，所以默认不截。

    `hasTrace` 说明这份数据里有没有逐步读数。没有 trace 时 token 只取
    消息自带的数，缓存命中与分层占用是空的，页面上要说明这一点。
    """
    windows, status_at = turn_windows(session, trace)

    metrics_all = []
    for event in trace:
        if event.get("event") != "context.metrics":
            continue
        payload = dict(event.get("payload") or {})
        payload["_at"] = event.get("at", 0.0)
        metrics_all.append(payload)

    turns = []
    for window in windows:
        user = window["user"]
        reply = window["reply"]
        start, end = window["start"], window["end"]
        metrics = [
            metric_row(p)
            for p in metrics_all
            if start - 0.5 <= p["_at"] <= end + 0.5
        ]
        tools = [tool_row(p) for p in (reply.parts if reply else []) if p.type == PartType.TOOL]
        reasoning = sum(
            len(p.text or "") for p in (reply.parts if reply else []) if p.type == PartType.REASONING
        )
        text_chars = sum(
            len(p.text or "") for p in (reply.parts if reply else []) if p.type == PartType.TEXT
        )
        tokens = dict(reply.tokens) if reply is not None and reply.tokens else {}
        if not tokens:
            tokens = {
                "input": sum(m["input"] for m in metrics),
                "cached": sum(m["cached"] for m in metrics),
                "uncached": sum(m["uncached"] for m in metrics),
                "output": sum(m["output"] for m in metrics),
            }
        cached = int(tokens.get("cached", 0))
        total_in = int(tokens.get("input", 0))
        # agent.status 的 done / failed 写在 turn.done 之后，窗口要往后放一点才收得到
        statuses = [status for at, status in status_at if start <= at <= end + 2.0]
        answer = ""
        if reply is not None:
            answer = "".join(p.text or "" for p in reply.parts if p.type == PartType.TEXT)
        user_text = "".join(p.text or "" for p in user.parts if p.type == PartType.TEXT)
        if text_limit:
            user_text = user_text[:text_limit]
            answer = answer[:text_limit]
        # 没有助手回复的用户消息不是一轮：把它算到下一个用户消息为止，
        # 会把两次输入之间的空闲（可能隔夜）记成这一轮的耗时。
        seconds = round(end - start, 1) if reply is not None else 0.0
        turns.append(
            {
                "index": window["index"],
                "user_text": user_text,
                "answer_text": answer,
                "started_at": start,
                "ended_at": end,
                "seconds": seconds,
                "has_reply": reply is not None,
                "status": (statuses[-1] if statuses else "done") if reply is not None else "no-reply",
                "steps": step_rows(reply, metrics, end) if reply is not None else [],
                "tools": tools,
                "failed": sum(1 for t in tools if t["status"] == "failed"),
                "reasoning_chars": reasoning,
                "text_chars": text_chars,
                "tokens": {
                    "input": total_in,
                    "cached": cached,
                    "uncached": max(int(tokens.get("uncached", total_in - cached)), 0),
                    "output": int(tokens.get("output", 0)),
                },
                "cost": float(reply.cost) if reply is not None else 0.0,
                "metrics": metrics,
                "cache_hit": round(
                    sum(m["hit"] for m in metrics) / len(metrics), 3
                ) if metrics else 0.0,
            }
        )

    events = []
    for event in session.events:
        events.append(
            {
                "id": event.id,
                "topic_index": event.topic_index,
                "title": event.title,
                "summary": event.summary,
                "status": event.status,
                "steps": len(event.steps),
                "step_start": event.step_start,
                "step_end": event.step_end,
                "degraded": event.degraded,
                "exit_reason": event.exit_reason,
                "artifacts": event.artifacts,
                "issues": event.issues,
                "actions": event.actions,
                "created_at": event.created_at,
            }
        )

    total_tokens = {
        "input": sum(t["tokens"]["input"] for t in turns),
        "cached": sum(t["tokens"]["cached"] for t in turns),
        "uncached": sum(t["tokens"]["uncached"] for t in turns),
        "output": sum(t["tokens"]["output"] for t in turns),
    }
    model = ""
    for window in reversed(windows):
        if window["reply"] is not None and window["reply"].model_id:
            model = window["reply"].model_id
            break

    issues: list[dict] = []
    seen: set[str] = set()

    def add(level: str, turn_index: int | None, text: str, key: str) -> None:
        # 同一件事会在轮和主题两边各出现一次：工具调用记在轮上，事件的问题从步里聚合。
        # 按正文去重，保留先到的那条（轮上的带轮号，更好定位）。
        if key in seen:
            return
        seen.add(key)
        issues.append({"level": level, "turn": turn_index, "text": text})

    for turn in turns:
        if not turn["has_reply"]:
            add("info", turn["index"],
                f"第 {turn['index']} 轮没有助手回复：{turn['user_text'][:40] or '（空消息）'}",
                f"no-reply-{turn['index']}")
        for tool in turn["tools"]:
            if tool["status"] != "failed":
                continue
            error = tool["error"]
            body = error[:120]
            if error.startswith("本轮步数快用完了"):
                # 收束阶段按设计拒绝新工具，这不是工具本身的失败。
                add("info", turn["index"], f"第 {turn['index']} 轮收束阶段拒绝了 {tool['name']}", body[:60])
            else:
                add("error", turn["index"], f"第 {turn['index']} 轮 {tool['name']} 失败：{body}", body[:60])
        # 整份 trace 都没有时，每一轮都会命中这一条；那是「没有记录」，
        # 不是「这一轮缺了记录」，逐轮重复一遍只会把真正的问题淹掉。
        if trace and not turn["metrics"] and turn["has_reply"]:
            add("warn", turn["index"],
                f"第 {turn['index']} 轮没有 trace 装配记录，token 取自消息本身",
                f"no-metrics-{turn['index']}")
        low = [m for m in turn["metrics"] if m["hit"] and m["hit"] < 0.5]
        if low:
            steps = "、".join(str(m["step"]) for m in low)
            add("warn", turn["index"],
                f"第 {turn['index']} 轮缓存命中低于 0.5：第 {steps} 步",
                f"low-hit-{turn['index']}")
        if turn["metrics"] and all("event_layer" in m["degraded"] for m in turn["metrics"]):
            add("info", turn["index"],
                f"第 {turn['index']} 轮 {len(turn['metrics'])} 次装配全部裁过事件层",
                f"event-layer-{turn['index']}")
        if len(turn["steps"]) >= STEP_LIMIT:
            add("warn", turn["index"],
                f"第 {turn['index']} 轮走了 {len(turn['steps'])} 步，达到主题步数上限",
                f"step-limit-{turn['index']}")
    for event in events:
        if event["exit_reason"]:
            add("error", None, f"{event['id']} 停在 {event['exit_reason']}：{event['title']}",
                f"exit-{event['id']}")
        if event["degraded"]:
            add("info", None, f"{event['id']} 的标题与结论由本地拼出：{event['title']}",
                f"degraded-{event['id']}")
        for issue in event["issues"]:
            add("warn", None, f"{event['id']}：{issue}", issue[:60])

    return {
        "session": {
            "id": session.id,
            "title": session.title,
            "model": model,
            "status": session.status,
            "last_exit_reason": session.last_exit_reason,
            "messages": len(session.messages),
            "topics": len(events),
            "turns": len(turns),
            "steps": sum(len(t["steps"]) for t in turns),
            "tools": sum(len(t["tools"]) for t in turns),
            "failed": sum(t["failed"] for t in turns),
            "refused": sum(
                1
                for t in turns
                for tool in t["tools"]
                if tool["status"] == "failed" and tool["error"].startswith("本轮步数快用完了")
            ),
            "tokens": total_tokens,
            "cost": round(sum(t["cost"] for t in turns), 6),
            "cache_reuse": round(total_tokens["cached"] / total_tokens["input"], 4)
            if total_tokens["input"]
            else 0.0,
            "active_seconds": round(sum(t["seconds"] for t in turns), 1),
            "created_at": session.created_at,
        },
        "turns": turns,
        "events": events,
        "issues": issues,
        "hasTrace": bool(trace),
        "generated_at": time.time(),
    }
