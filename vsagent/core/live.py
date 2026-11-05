"""当前主题的实时快照。

状态文件在一轮结束时才写一次，网页端隔着磁盘只能看到上一轮的结果。这个模块
把「这一轮走到哪儿了」写成一个小文件，与状态文件放在同一层目录下：网页端读它，
补上状态文件还没有的那一截；轮末写一次 `running=false`，整份历史仍以
`state.toml` 为准。

两条别的路都不走：

- 每步重写 `state.toml`：单会话 0.8 MB、本机全部 5.8 MB，一轮二十步就是几十 MB
  重写，还要 fsync，而保存是同步调用，坐在循环的路径上。
- 网页线程直接读活的 `Session`：Parts 正在追加，读到的快照会半新半旧；
  要一致就得给会话加锁，改动面比写一个小文件大得多。

文件大小控制在几 KB：只放主题序号、已完成的步、当前工具与状态、首行结果。
reasoning 与完整输出不放进来——它每两秒被拉一次，放多了既费带宽，也把工具正文
复制出第二份。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .models import PartType, Session, ToolStatus

LIVE_DIRECTORY = "live"

# 超过这个秒数没更新，页面按「可能已断开」显示。轮末写 `running=false`，
# 所以只有在进程被强杀、或者某个回调卡住时才会走到这条判断。
STALE_SECONDS = 15.0

PREVIEW_LIMIT = 120
"""当前工具结果的首行长度。放整段输出会把文件撑大，页面也不显示那么多。"""


def directory(state_path: Path) -> Path:
    """快照目录：与状态文件同一层，测试与独立服务都不用另配参数。"""
    return Path(state_path).parent / LIVE_DIRECTORY


def path_for(state_path: Path, session_id: str) -> Path:
    return directory(state_path) / f"{session_id}.json"


def _head(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """第一个非空行，超长截断。"""
    line = next((item.strip() for item in (text or "").splitlines() if item.strip()), "")
    return line if len(line) <= limit else f"{line[:limit]}…"


def _step_json(step: Any) -> dict[str, Any]:
    """一条已完成的步。形状与 `/events` 里的步一致，页面共用一套画法。"""
    return {
        "seq": step.seq,
        "intent": step.intent,
        "tools": list(step.tools),
        "result": step.result,
        "issues": list(step.issues),
        # 步记下来时这一步的工具已经跑完；有未决项就是失败。
        "status": "failed" if step.issues else "completed",
    }


def _current_step(session: Session) -> dict[str, Any] | None:
    """当前这一步在干什么。

    只读最后一条 assistant 消息，并且只看最近一个 `STEP_START` 之后的工具：
    一条消息装着一整轮的所有往返，往前翻会把上一步的工具当成现在这一次。

    一次往返可以带多个工具调用，它们按顺序执行。挑选的次序是「等确认的 → 正在跑的
    → 还没开始的」，这样一个工具跑完、下一个还没轮到的时候，页面显示的仍是那一个
    正在执行的调用。
    """
    if not session.messages:
        return None
    message = session.messages[-1]
    if message.role != "assistant" or not message.parts:
        return None

    round_parts: list[Any] = []
    started_at = 0.0
    for part in reversed(message.parts):
        round_parts.append(part)
        if part.type is PartType.STEP_START:
            started_at = part.timestamp
            break
    round_parts.reverse()

    tools = [part for part in round_parts if part.type is PartType.TOOL and part.tool_name]

    def pick(status: ToolStatus) -> Any | None:
        return next(
            (part for part in tools if part.tool_status is status),
            None,
        )

    chosen = pick(ToolStatus.AWAITING) or pick(ToolStatus.RUNNING) or pick(ToolStatus.PENDING)
    if chosen is None:
        # 这一轮的工具都跑完了，模型在写下一次请求或收尾文字。
        return {
            "phase": "thinking",
            "tool": "",
            "status": "",
            "preview": "",
            "since": started_at,
            "calls": len(tools),
        }
    return {
        "phase": "waiting" if chosen.tool_status is ToolStatus.AWAITING else "tool",
        "tool": chosen.tool_name,
        "status": chosen.tool_status.value if chosen.tool_status else "",
        "preview": _head(chosen.tool_output),
        "since": chosen.timestamp,
        "calls": len(tools),
    }


def snapshot(
    session: Session,
    *,
    running: bool,
    now: float | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """把当前主题折成一份能直接发给页面的快照。"""
    steps = [_step_json(step) for step in session.pending_steps]
    tools: list[str] = []
    calls = 0
    for step in session.pending_steps:
        calls += len(step.tools)
        for name in step.tools:
            if name not in tools:
                tools.append(name)
    current = _current_step(session) if running else None
    # 「模型在生成」那一档，上一次往返的工具已经记进 pending_steps 了；
    # 只有还没记步的那一轮才把它的调用数补进来，否则同一个数字算两遍。
    unrecorded = bool(current) and current["phase"] in ("tool", "waiting")
    if unrecorded:
        if current["tool"] and current["tool"] not in tools:
            tools.append(current["tool"])
        calls += current["calls"]
    return {
        "sessionId": session.id,
        "running": running,
        "updatedAt": time.time() if now is None else now,
        "pid": os.getpid() if pid is None else pid,
        "eventCount": len(session.events),
        "topicIndex": len(session.events) + 1,
        # 模型用 `close_topic` 报过名字时才非空；没报就留空，页面用自己的兜底写法。
        "title": session.topic_title,
        "steps": steps,
        "current": current,
        "tools": tools,
        "calls": calls,
    }


def write(state_path: Path, session: Session, *, running: bool) -> None:
    """写一份快照。原子替换，不做 fsync。

    这一份是显示用的：进程被强杀时丢掉最后一帧可以接受，页面按 `stale` 处理。
    加 fsync 会让每一步都等一次磁盘，而它要回答的问题只是「现在在做什么」。
    """
    payload = snapshot(session, running=running)
    target = path_for(state_path, session.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)


def read(state_path: Path, session_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """读一份快照；没有时返回 `None`，过期时补 `stale`。"""
    target = path_for(state_path, session_id)
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    moment = time.time() if now is None else now
    data["stale"] = bool(data.get("running")) and (
        moment - float(data.get("updatedAt") or 0) > STALE_SECONDS
    )
    return data
