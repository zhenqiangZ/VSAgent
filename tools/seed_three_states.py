"""给界面判据造一个三态会话：完成 / 失败 / 中断各一个主题，外加一份运行中的快照。

判据要在真实浏览器里核对三种状态的颜色与文案，而本机的真实会话里通常只有
「完成」一种，另外两种很难等到。这个脚本往给定的状态文件里追加一条会话：

    python tools/seed_three_states.py --state .probe/web-ui-state/state.toml

标准输出是这条会话的编号，判据拿它去开页面。**只追加，不改已有会话**。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core import live
from vsagent.core.agent.event_log import seal_range
from vsagent.core.models import Message, Part, PartType, Session, ToolStatus
from vsagent.core.session_manager import SessionManager


def turn(session: Session, ask: str, tool: str, output: str, failed: bool, base: float) -> None:
    """一轮：一句用户话 + 一条助手消息（步开始、工具、文字）。"""
    user = Message(role="user", session_id=session.id, created_at=base)
    user.add_part(Part(type=PartType.TEXT, text=ask, timestamp=base))
    session.add_message(user)

    assistant = Message(role="assistant", session_id=session.id, created_at=base + 1)
    assistant.add_part(Part(type=PartType.STEP_START, text="1 · fake-model", timestamp=base + 1))
    assistant.add_part(
        Part(
            type=PartType.TOOL,
            tool_name=tool,
            tool_status=ToolStatus.FAILED if failed else ToolStatus.COMPLETED,
            tool_input={"pdb_id": "4HJO"},
            tool_output=output,
            timestamp=base + 1,
        )
    )
    assistant.add_part(Part(type=PartType.TEXT, text=f"这一步的结论：{ask}", timestamp=base + 4))
    session.add_message(assistant)


def seed(state: Path) -> str:
    manager = SessionManager(state)
    manager.load_from_toml()
    session = manager.create_session("三态演示")
    now = time.time()
    turn(session, "先查结构", "fetch_structure", "取到 4HJO，1.8 Å", False, now - 300)
    turn(session, "准备受体", "prepare_receptor", "缺少 meeko，无法准备受体", True, now - 200)
    turn(session, "定口袋", "define_pocket", "口袋 P1：12 个残基", False, now - 100)

    seal_range(session, kind="manual", upto=2, title="取结构", summary="4HJO 可用。")
    seal_range(session, kind="manual", upto=4, title="准备受体", summary="准备失败，缺依赖。")
    seal_range(
        session,
        kind="auto",
        upto=6,
        title="定口袋",
        summary="口袋定下来了，用户随后按了停止。",
        exit_reason="cancelled",
    )
    # 实时快照写的是「正在跑」，会话状态跟着写成运行中：首页按这个字段决定
    # 轮询哪几行，写成 idle 的话它会显示成「待开始」，与快照自相矛盾。
    session.status = "running"
    manager.save_to_toml()

    # 运行中的那一份：当前工具停在等确认上，页面应当画出「请回终端回答」。
    live.write(state, session, running=True)
    target = live.path_for(state, session.id)
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["topicIndex"] = 4
    snapshot["title"] = "正在对接"
    snapshot["steps"] = [
        {
            "seq": 1,
            "intent": "dock(ligandset=ligandset#1)",
            "tools": ["dock"],
            "result": "成功：docking#1 → 9 个位姿",
            "issues": [],
            "status": "completed",
        }
    ]
    snapshot["current"] = {
        "phase": "waiting",
        "tool": "analyze_pose",
        "status": "awaiting",
        "preview": "准备读取位姿",
        "since": time.time() - 3,
        "calls": 1,
    }
    snapshot["tools"] = ["dock", "analyze_pose"]
    snapshot["calls"] = 2
    snapshot["updatedAt"] = time.time()
    target.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return session.id


def main() -> int:
    parser = argparse.ArgumentParser(description="往状态文件里追加一个三态会话")
    parser.add_argument("--state", type=Path, required=True, help="要追加到哪个状态文件")
    args = parser.parse_args()
    if not args.state.exists():
        raise SystemExit(f"状态文件不存在：{args.state}")
    print(seed(args.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
