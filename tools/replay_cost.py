"""成本探针：把一次真实会话按**当前**的装配器重放一遍，量出每一步各层用了多少。

```
python tools/replay_cost.py                      # 最近一个会话，并与它的 trace 对照
python tools/replay_cost.py --session ses_0001
python tools/replay_cost.py --list               # 列出有消息的会话与 trace
python tools/replay_cost.py --json .probe/cost.json   # 逐步数据落盘
```

回答的是「改造前后差多少」这一类问题：`~/.vsagent/logs/<会话>.jsonl` 里留着当时
每一步的 `context.metrics`（改造前的真实数字），把同一份消息交给现在的
`ContextAssembler` 再装配一次，就得到改造后的数字。同一份输入、两把尺子，才可比。

**它不调模型、不联网**，只读 `state.toml`、`ledgers/`、`.vsmemory/` 与 trace。

重放要和真实循环对齐三处，否则数字没有意义：

1. **按 `STEP_START` 切步**，不按工具调用切。一步可能没有工具（纯回答），
   也可能一次调三个；按工具切会把步数算多，和 trace 里的度量对不上。
2. **补 `load_tools` 的副作用**。族表决定下一步发多少工具定义，不补的话
   工具定义会一直停在核心那一份，凭空省下一大截。
3. **去掉旧会话里的 `COMPACTION` 部件**。那是「历史超窗 → 摘要」那条路的产物，
   新设计只在首轮就溢出时兜底。留着等于拿旧包袱算新账。

**三处已知失真**（方向固定，量级小）：`TASK` 用最终的工作区索引（偏高）；
证据层不模拟（偏低，它是循环层的运行时状态，不在消息里）；事件的标题与结论
用本地兜底那份（模型真调 `close_topic` 会稍长）。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core.agent.context import ContextAssembler, estimate_tokens  # noqa: E402
from vsagent.core.agent.domain.all_tools import domain_tools  # noqa: E402
from vsagent.core.agent.event_log import (  # noqa: E402
    TOPIC_SOFT_LIMIT,
    domain_tools as event_tools,
    event_boundary,
    open_tokens,
    record_step,
    seal_range,
)
from vsagent.core.agent.ledger import SessionLedger, default_snapshot_path  # noqa: E402
from vsagent.core.agent.memory import MemoryStore, RecallTool, RememberTool  # noqa: E402
from vsagent.core.agent.prompt import stable_prefix  # noqa: E402
from vsagent.core.agent.spawn import AgentStatusTool, SpawnAgentTool  # noqa: E402
from vsagent.core.agent.task import TaskTool  # noqa: E402
from vsagent.core.agent.toolset import LoadToolsTool, hidden_names, prune_families  # noqa: E402
from vsagent.core.agent.tools import (  # noqa: E402
    BASH_TIMEOUT,
    BashTool,
    GrepTool,
    PatchTool,
    ReadTool,
    ReadTurnTool,
    ToolRegistry,
    WriteTool,
)
from vsagent.core.agent.workspace import Workspace  # noqa: E402
from vsagent.core.models import PartType, Session  # noqa: E402
from vsagent.core.session_manager import SessionManager, default_state_path  # noqa: E402

DEFAULT_STATE = default_state_path()
DEFAULT_LOGS = Path.home() / ".vsagent" / "logs"


def build_registry() -> ToolRegistry:
    """与 `app._build_registry` 同一份装配。

    这里不调 `app` 那个方法：它挂在 Textual 的 App 上，起一个应用只为了量 token
    太重。两边要改一起改——工具集合不一致时，工具定义那一层的数就白量了。
    """
    memory = MemoryStore(PROJECT_ROOT)
    registry = ToolRegistry()
    for tool in (
        BashTool(cwd=PROJECT_ROOT, timeout=BASH_TIMEOUT),
        ReadTool(cwd=PROJECT_ROOT),
        WriteTool(cwd=PROJECT_ROOT),
        PatchTool(cwd=PROJECT_ROOT),
        GrepTool(cwd=PROJECT_ROOT),
        RememberTool(memory),
        RecallTool(memory),
        TaskTool(),
        ReadTurnTool(),
    ):
        registry.register(tool)
    for tool in event_tools():
        registry.register(tool)
    registry.register(LoadToolsTool())
    registry.register(SpawnAgentTool())
    registry.register(AgentStatusTool())
    for tool in domain_tools():
        registry.register(tool)
    return registry


def strip_parts(session: Session) -> None:
    """去掉改造后不会再产生的压缩部件（见模块说明第 3 条）。"""
    for message in session.messages:
        message.parts = [
            part
            for part in message.parts
            if part.type not in (PartType.COMPACTION, PartType.SUMMARY)
        ]


def steps_of(session: Session) -> list[tuple[int, int, int, list]]:
    """一步 = 一条 `STEP_START` 到下一个 `STEP_START` 之前。

    返回 `(消息下标, part 起点, part 终点, 这一步的工具调用)`。
    """
    out = []
    for index, message in enumerate(session.messages):
        if message.role != "assistant":
            continue
        starts = [
            position
            for position, part in enumerate(message.parts)
            if part.type == PartType.STEP_START
        ]
        for order, start in enumerate(starts):
            end = starts[order + 1] if order + 1 < len(starts) else len(message.parts)
            calls = [part for part in message.parts[start:end] if part.type == PartType.TOOL]
            out.append((index, start, end, calls))
    return out


def truncate(session: Session, index: int, part_index: int) -> Session:
    """装配第 k 步请求时，会话里只该有到上一步为止的部件。"""
    view = copy.deepcopy(session)
    for position, message in enumerate(view.messages):
        if position < index:
            continue
        if position == index:
            message.parts = message.parts[:part_index]
        else:
            view.messages = view.messages[:position]
            break
    return view


def replay(session: Session, window: int) -> list[dict[str, Any]]:
    """按当前装配器逐步重放，返回每一步的度量。"""
    strip_parts(session)
    # 从「什么都没发生」开始：事件、压缩位置、族装载都归零。
    session.events = []
    session.compact_upto = 0
    session.summary_upto = 0
    session.summary = ""
    session.pending_steps = []
    session.tool_families = []
    session.family_phase = {}

    registry = build_registry()
    workspace = Workspace(session.id, root=PROJECT_ROOT)
    snapshot = default_snapshot_path(session.id)
    ledger = SessionLedger(
        session_id=session.id,
        snapshot_path=snapshot if snapshot.exists() else None,
    )
    memory = MemoryStore(PROJECT_ROOT)
    memory.load()
    query = session.messages[0].parts[0].text if session.messages else ""
    steps = steps_of(session)
    print(f"会话 {session.id}：{len(steps)} 步、{len(session.messages)} 条消息")

    current_message = -1

    def finish(index: int) -> None:
        """第 index 步（0 起）跑完了：装载族、记流水。

        `load_tools` 的副作用必须在这里补——它改的是会话上的族表，
        而族表决定了下一步发出去的工具定义有多少。
        """
        message_index, start, end, calls = steps[index]
        for call in calls:
            if call.tool_name != "load_tools":
                continue
            family = str(call.tool_input.get("family", "")).strip()
            if family and family not in session.tool_families:
                session.tool_families.append(family)
                session.family_phase[family] = len(session.events) + 1
        if message_index != current_message:
            return
        record_step(
            session,
            step_no=len(session.pending_steps) + 1,
            parts=calls,
            message_index=message_index,
            part_start=start,
            part_end=end,
        )

    rows: list[dict[str, Any]] = []
    for number, (message_index, start, end, _calls) in enumerate(steps, 1):
        # 上一步已经跑完：先把它的副作用补上，再看这一步的上下文。
        if number > 1:
            finish(number - 2)
        # 换轮：先把上一轮封存（与循环层轮末那一处同义）。
        if message_index != current_message:
            if current_message >= 0:
                event = seal_range(
                    session, kind="auto", upto=event_boundary(session, message_index)
                )
                if event is not None and event.steps:
                    prune_families(
                        session,
                        {name for step in event.steps for name in step.tools},
                        event.topic_index,
                    )
            current_message = message_index
        elif open_tokens(session) > TOPIC_SOFT_LIMIT:
            seal_range(session, kind="auto", upto=event_boundary(session, message_index))

        view = truncate(session, message_index, start)
        view.events = session.events
        view.pending_steps = session.pending_steps
        view.tool_families = list(session.tool_families)
        view.family_topic = dict(session.family_topic)

        bundle = ContextAssembler(window).build(
            view,
            stable_prefix(workspace, view),
            tools=registry.specs(exclude=hidden_names(view)),
            memory_text=memory.resident_text(query=query),
            task_text="" if view.task_state.is_empty() else view.task_state.render(),
            digest_text=ledger.digest(),
            env_text="",
            evidence=[],
        )
        usage = dict(bundle.metrics["usage"])
        # 推理通道：改造前每一步都把历史推理逐字重发；改造后跨轮换占位。
        # 两边用同一把尺子量，才可比。
        old_reasoning = sum(
            estimate_tokens(part.text)
            for message in view.messages
            for part in message.parts
            if part.type == PartType.REASONING
        )
        new_reasoning = sum(
            estimate_tokens(item.get("reasoning_content") or "")
            for item in bundle.messages
            if item.get("role") == "assistant"
        )
        rows.append(
            {
                "step": number,
                "message": message_index,
                "total": bundle.metrics["total"],
                "tools": bundle.metrics["tools"],
                "events": len(session.events),
                "families": list(session.tool_families),
                "usage": usage,
                "old_reasoning": old_reasoning,
                "new_reasoning": new_reasoning,
                "degraded": list(bundle.degraded),
            }
        )
        print(
            f"{number:>3} msg{message_index:>3} total={bundle.metrics['total']:>7} "
            f"tools={bundle.metrics['tools']:>6} events={len(session.events)} "
            f"recent={usage.get('recent', 0):>7} event={usage.get('event', 0):>5} "
            f"reasoning={new_reasoning:>6}(旧 {old_reasoning:>6}) "
            f"{','.join(bundle.degraded) or '-'}"
        )
    return rows


def load_trace(session_id: str, logs: Path) -> list[dict[str, Any]]:
    path = logs / f"{session_id}.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open() as handle:
        for line in handle:
            if '"context.metrics"' not in line:
                continue
            try:
                out.append(json.loads(line)["payload"])
            except (KeyError, ValueError):
                continue
    return out


def compare(rows: list[dict[str, Any]], old: list[dict[str, Any]]) -> None:
    """改造前（trace 实测）与改造后（重放）逐层对照。"""
    if not old:
        print("\n没有 trace，跳过对照（只输出改造后的数字）")
        return
    if len(old) != len(rows):
        print(f"\n注意：trace 有 {len(old)} 条度量，重放有 {len(rows)} 步，"
              "两者不是一一对应，对照只能看量级")
    print("\n逐层对照（改造前 trace 实测 / 改造后重放）")
    print(f"{'层':<10}{'改造前':>10}{'改造后':>10}{'变化':>9}")
    for layer in ("prefix", "memory", "task", "event", "recent", "evidence", "history"):
        before = sum(item["usage"].get(layer, 0) for item in old)
        after = sum(row["usage"].get(layer, 0) for row in rows)
        if layer == "history":
            # 残差项（header 减去各段），改造后可以是负数，百分比没有意义。
            share = "残差"
        elif before:
            share = f"{(after - before) / before * 100:+.1f}%"
        else:
            share = "新增"
        print(f"{layer:<10}{before:>10}{after:>10}{share:>9}")
    before_tools = sum(item.get("tools", 0) for item in old)
    after_tools = sum(row["tools"] for row in rows)
    print(f"{'tools':<10}{before_tools:>10}{after_tools:>10}"
          f"{(after_tools - before_tools) / before_tools * 100:>+8.1f}%")
    before_total = sum(item["total"] for item in old) + before_tools
    after_total = sum(row["total"] for row in rows) + after_tools
    print(f"{'合计':<10}{before_total:>10}{after_total:>10}"
          f"{(after_total - before_total) / before_total * 100:>+8.1f}%")
    print(f"{'其中推理':<8}{sum(row['old_reasoning'] for row in rows):>10}"
          f"{sum(row['new_reasoning'] for row in rows):>10}")

    inputs = sum(item.get("input", 0) for item in old)
    cached = sum(item.get("cached", 0) for item in old)
    if inputs:
        ratio = inputs / max(1, before_total)
        print(f"\n改造前实际输入 {inputs}（缓存命中 {cached}，未命中 {inputs - cached}）；"
              f"与估算的比 {ratio:.3f}")
        print(f"按同一比例折算，改造后约 {round(after_total * ratio)} 输入")

    print("\n分轮")
    print(f"{'轮':<4}{'步':>4}{'改造前':>10}{'改造后':>10}{'变化':>9}")
    turn = 0
    index = 0
    while index < len(rows):
        message = rows[index]["message"]
        end = index
        while end + 1 < len(rows) and rows[end + 1]["message"] == message:
            end += 1
        turn += 1
        before = sum(item["total"] + item.get("tools", 0) for item in old[index:end + 1])
        after = sum(row["total"] + row["tools"] for row in rows[index:end + 1])
        share = f"{(after - before) / before * 100:+.1f}%" if before else "—"
        print(f"{turn:<4}{end - index + 1:>4}{before:>10}{after:>10}{share:>9}")
        index = end + 1


def pick(manager: SessionManager, wanted: str) -> Session | None:
    if wanted:
        return manager.sessions.get(wanted)
    usable = [item for item in manager.sessions.values() if item.messages]
    if not usable:
        return None
    return max(usable, key=lambda item: item.updated_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按当前装配器重放一次会话，量各层 token")
    parser.add_argument("--session", default="", help="会话 id，默认取最近一个有消息的")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="state.toml 路径")
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOGS, help="trace 目录")
    parser.add_argument("--window", type=int, default=1_000_000, help="上下文窗口")
    parser.add_argument("--json", type=Path, default=None, help="逐步数据落盘到哪个文件")
    parser.add_argument("--list", action="store_true", help="列出会话后退出")
    args = parser.parse_args(argv)

    manager = SessionManager(args.state)
    if not manager.load_from_toml():
        print(f"读不到状态文件：{args.state}")
        return 1
    if args.list:
        for item in sorted(manager.sessions.values(), key=lambda s: s.updated_at, reverse=True):
            trace = args.logs / f"{item.id}.jsonl"
            print(f"{item.id}  消息 {len(item.messages):>3}  事件 {len(item.events):>2}  "
                  f"trace {'有' if trace.exists() else '无'}  {item.title}")
        return 0

    session = pick(manager, args.session)
    if session is None or not session.messages:
        print("没有可重放的会话")
        return 1

    rows = replay(session, args.window)
    compare(rows, load_trace(session.id, args.logs))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f"\n逐步数据写到 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
