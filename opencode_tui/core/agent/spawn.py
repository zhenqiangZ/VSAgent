"""子 agent 的派发：把父会话的一段消息复制成一条独立会话，并交给它一个任务。

这个模块只放两样东西：

- `fork_messages`：纯数据复制，不碰事件、不碰界面；
- `SpawnAgentTool`：模型能调用的那个工具，只做参数校验与守卫。

真正「起一条后台任务」的是 `AgentHost`（`host.py`）。分开的理由和 `task.py`
一样：工具层不该知道怎么调度，调度层也不该知道模型的参数长什么样。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models import Message, Part, PartType, Session, ToolStatus
from .prompt import estimate_tokens, translate_message
from .tools import Tool, ToolError, ToolResult, ToolSpec

# 复制时跳过的 Part：它们是「上一轮怎么跑的」过程标记，不是对话内容。
# 留着会让子 agent 的历史里出现一堆别人的 step 编号，既占额度又误导。
SKIPPED_PARTS = frozenset(
    {PartType.STEP_START, PartType.STEP_FINISH, PartType.RETRY, PartType.COMPACTION}
)

# 复制区间最多占可用上下文的比例。超过就从最早的消息开始丢。
# 取 0.6 而不是 0.9：子 agent 还要留出工具定义、任务书与自己干活的额度。
SEED_RATIO = 0.6


def _message_tokens(message: Message) -> int:
    """一条消息按模型看到的那一份估算 token。"""
    return estimate_tokens(json.dumps(translate_message(message), ensure_ascii=False))


def fork_messages(
    parent: Session,
    child: Session,
    start: int,
    end: int,
    budget: int,
) -> tuple[int, int]:
    """把父会话 `[start, end)` 复制进子会话，返回 (复制条数, 因预算丢掉的条数)。

    **每条消息、每个 Part 都重新生成 ID**：ID 序列是全局的，复用会让两个会话里
    出现同号的 message/part，事件路由与 `part_by_id` 都会指错。
    """
    start = max(0, min(start, len(parent.messages)))
    end = max(start, min(end, len(parent.messages)))
    picked = list(parent.messages[start:end])

    dropped = 0
    total = sum(_message_tokens(message) for message in picked)
    # 至少留最后一条：任务书之前总得有一句父会话的上下文，
    # 全丢光的话子 agent 只剩一句「任务」，与「复制这段历史」的预期相反。
    while len(picked) > 1 and total > budget:
        total -= _message_tokens(picked[0])
        picked.pop(0)
        dropped += 1

    for message in picked:
        copy = Message(
            role=message.role,
            session_id=child.id,
            created_at=message.created_at,
            model_id=message.model_id,
            provider_id=message.provider_id,
        )
        for part in message.parts:
            if part.type in SKIPPED_PARTS:
                continue
            copy.add_part(
                Part(
                    type=part.type,
                    text=part.text,
                    tool_name=part.tool_name,
                    tool_status=part.tool_status,
                    tool_input=dict(part.tool_input),
                    tool_output=part.tool_output,
                    file_path=part.file_path,
                    agent_name=part.agent_name,
                    permission=part.permission,
                    patch_added=part.patch_added,
                    patch_removed=part.patch_removed,
                    timestamp=part.timestamp,
                )
            )
        # 一条只剩过程标记的消息没有内容可复制，跳过；否则子会话里会出现空壳消息。
        if copy.parts:
            child.add_message(copy)

    # 任务状态只继承用户约束：那是用户提的要求，复制过去仍然生效。
    # 目标、进度与阻塞属于父会话自己的任务，照搬会让子 agent 以为那些也是它的。
    child.task_state.constraints = list(parent.task_state.constraints)
    child.task_state.goal = child.title
    child.seed_start = start
    child.seed_end = end
    return len(picked), dropped


def task_preamble(
    parent: Session,
    start: int,
    end: int,
    dropped: int = 0,
    digest: str = "",
) -> str:
    """子 agent 的第一段话：它从哪来、要做什么、不能做什么。

    写成用户消息而不是系统提示：它只该在子会话的第一轮出现，
    进稳定前缀的话会一直跟着这个子 agent 的每一次请求。

    `digest` 是父会话的工作区索引（`SessionLedger.digest()`），**只在没复制历史
    时才给**：fork 过来的子会话在历史里已经见过那些 handle，再列一遍是重复；
    而 fresh 的只带着任务书，不给它就得自己 `list_workspace` —— 那还得先确认
    装过 listing 族，两件事都可能白烧一步。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    lines = [
        "[子 agent 任务]",
        f"你是从父会话「{parent.title}」在 {stamp} 分出的子 agent。",
    ]
    copied = max(0, end - start - dropped)
    if copied:
        lines.append(f"父会话第 {end - copied + 1}–{end} 条消息已作为上文复制过来。")
    else:
        lines.append("没有复制父会话的历史，你只带着这份任务书开始。")
        if digest.strip():
            lines.append("父会话现在的工作区（handle 可以直接用）：")
            lines.append(digest.strip())
    if dropped:
        lines.append(f"其中更早的 {dropped} 条消息因上下文预算没有复制。")
    lines.extend(
        [
            "你不能再创建新的子 agent；需要拆分时把建议写进结论。",
            "你与父会话共享工作目录、runs/ 与登记表，handle 可以直接引用。",
            "完成后给出：结论、产出文件路径、未完成项。",
        ]
    )
    return "\n".join(lines)


def recent_start(messages: list[Message], turns: int) -> int:
    """最近 N 个用户轮的起点下标。`turns` 小于 1 时返回末尾（不复制）。"""
    if turns < 1:
        return len(messages)
    seen = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            seen += 1
            if seen >= turns:
                return index
    return 0


def history_end(messages: list[Message]) -> int:
    """可复制的历史终点。

    模型调 `spawn_agent` 时，最后一条助手消息正在写、里面就是这次调用本身。
    把它复制进子会话，子 agent 会看到一条没有结果的工具调用——那是父会话的
    半成品，不是它的历史。派发之后这条消息才补上结果，所以终点要往前退一条。
    """
    if not messages:
        return 0
    last = messages[-1]
    if last.role == "assistant" and any(
        part.type is PartType.TOOL
        and part.tool_name == "spawn_agent"
        and part.tool_status
        in (ToolStatus.PENDING, ToolStatus.RUNNING, ToolStatus.AWAITING)
        for part in last.parts
    ):
        return len(messages) - 1
    return len(messages)


class SpawnAgentTool(Tool):
    """从当前会话 fork 一个子 agent，在后台独立运行。

    与常见「子任务工具」的区别：它**立即返回**，不等待子 agent 跑完。
    父会话该继续做自己的事；结果在右侧列表里看。
    """

    spec = ToolSpec(
        name="spawn_agent",
        description=(
            "从当前会话 fork 一个子 agent，在后台独立运行，立即返回、不等待结果。"
            "子 agent 有独立上下文，但与本会话共享工作目录、runs/ 与登记表。"
            "适合把一段可以并行推进的调研或计算分出去；同一个任务不要重复派发。"
            "子 agent 内不能再派发新的 agent。"
            "派发之后用一句话告诉用户：派了什么、去哪看结果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "子 agent 的名字，两到八个字，会显示在右侧列表里",
                },
                "task": {
                    "type": "string",
                    "description": "交给子 agent 的完整任务书：做什么、产出什么、有什么限制",
                },
                "history": {
                    "type": "string",
                    "enum": ["all", "recent", "none"],
                    "description": (
                        "复制多少父会话历史作为子 agent 的初始上下文："
                        "all=整段；recent=最近若干轮（默认）；none=只给任务书"
                    ),
                },
                "recent_turns": {
                    "type": "integer",
                    "description": "history=recent 时复制最近几个用户轮，默认 6",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    )

    session_argument = "session"
    spawn_argument = "spawner"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """校验参数、守卫深度，然后交给 host。"""
        session: Session = arguments["session"]
        spawner = arguments["spawner"]
        if session.depth >= 1:
            raise ToolError(
                "子 agent 内不能再创建 agent。把任务做完，把结论交回父会话；"
                "如果确实需要拆分，把建议写进结论里。"
            )
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ToolError("缺少参数 task：子 agent 需要一份完整任务书")
        task = task.strip()
        title = str(arguments.get("title") or "").strip()
        if not title:
            title = task.splitlines()[0][:20]
        history = str(arguments.get("history") or "recent")
        if history not in ("all", "recent", "none"):
            raise ToolError("history 只能是 all / recent / none")

        if history == "all":
            start, end = 0, history_end(session.messages)
        elif history == "none":
            start = end = history_end(session.messages)
        else:
            turns = arguments.get("recent_turns")
            turns = 6 if not isinstance(turns, int) or turns < 1 else turns
            end = history_end(session.messages)
            start = recent_start(session.messages[:end], turns)

        child = spawner.spawn(
            session,
            title=title,
            task=task,
            start=start,
            end=end,
            origin="agent",
        )
        return ToolResult(
            output=(
                f"已派发子 agent「{child.title}」（{child.id}），它在后台运行。"
                "不要重复派发同一任务；用 agent_status 查看进度或等它给出结论。"
            ),
            meta={"session_id": child.id},
        )


# 一次报告里最多写几个子 agent、每个写多长。取小值：这份报告进的是父会话
# 的上下文，子 agent 的全文在它自己的会话里，要看完整内容点右栏进去。
MAX_REPORTS = 8
REPORT_LIMIT = 800
# 一份报告整段进父会话上下文的上限（token）。
#
# 单条的 800 字是「别把结论全文抄过来」；这个总数管的是「八个子 agent 一起报」：
# 8 × 800 字最坏约 3K token，等于把子 agent 的结论抄了四份进父会话。
# 1500 是计划里给「父会话只多一条消息」定的量级，超出的写一句去哪看全文。
REPORT_TOKENS = 1500


def _status_text(status: str) -> str:
    """状态的中文说法。与界面右栏用同一套词。"""
    return {
        "idle": "空闲",
        "running": "运行中",
        "done": "完成",
        "failed": "失败",
        "cancelled": "已中断",
    }.get(status, status)


def _last_result(session: Session) -> str:
    """子 agent 最后一条助手正文或收尾总结。没有就返回空串。"""
    for message in reversed(session.messages):
        if message.role != "assistant":
            continue
        texts = [
            part.text.strip()
            for part in message.parts
            if part.type in (PartType.TEXT, PartType.SUMMARY) and part.text.strip()
        ]
        if texts:
            text = texts[-1]
            if len(text) > REPORT_LIMIT:
                text = text[:REPORT_LIMIT] + "…（完整内容在这个子 agent 的会话里）"
            return text
    return ""


def _output_files(session: Session) -> list[str]:
    """子 agent 会话里出现过的产出文件。

    只取路径，不读内容：报告进父会话的上下文，列文件就够了，
    细节进那个子 agent 的界面看。最多列 5 个，多了写「等 N 个」。
    """
    found: list[str] = []
    for message in session.messages:
        for part in message.parts:
            if part.type is PartType.FILE and part.file_path:
                path = part.file_path
            elif part.type is PartType.PATCH and part.text:
                path = part.text
            else:
                continue
            if path not in found:
                found.append(path)
    return found


def _cap_report(text: str, limit: int = REPORT_TOKENS) -> str:
    """把整份报告压到限额以内：**按行**丢，末尾写明去哪看全文。

    不按字符截：报告是「一行一个子 agent」的结构，切在半行上会留下半句结论，
    读的人分不清那一行说的是谁。
    """
    if estimate_tokens(text) <= limit:
        return text
    note = "\n（报告已截断；完整内容在子 agent 的会话里，给 session_id 单独查）"
    kept: list[str] = []
    used = estimate_tokens(note)
    for line in text.splitlines():
        cost = estimate_tokens(line) + 1
        if used + cost > limit:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) + note


class AgentStatusTool(Tool):
    """查看子 agent 的状态与结论，可选等它们跑完。

    这是父会话取回结果的那条路：`spawn_agent` 只负责派出去，
    跑完的结论不会自动进父会话的上下文——那样会把子 agent 的全文
    塞进来，把父会话的窗口吃掉。这里只取一份短报告，要细节再进那个界面。
    """

    spec = ToolSpec(
        name="agent_status",
        description=(
            "查看本会话派出的子 agent 的状态与最后一条结论；"
            "`wait=true` 时先等它们跑完（最多 60 秒）。"
            "派发之后用这个取结果，不要靠猜，也不要重复派发同一任务。"
            "报告是节选的；完整对话在右侧子 agent 列表里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "只看某一个子 agent；不填就看本会话派出的全部子 agent",
                },
                "wait": {
                    "type": "boolean",
                    "description": "true 表示等到它们跑完或超时再返回，默认 false",
                },
                "timeout": {
                    "type": "integer",
                    "description": "wait=true 时最多等多少秒，1–60，默认 20",
                },
            },
            "additionalProperties": False,
        },
    )

    session_argument = "session"
    spawn_argument = "spawner"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """汇总子 agent 的状态。只读，不改任何会话。"""
        session: Session = arguments["session"]
        host = arguments["spawner"]
        if session.depth >= 1:
            raise ToolError("子 agent 不能查询其他 agent；把你自己做完的结论交回父会话。")

        children = host.children_of(session.id)
        wanted = str(arguments.get("session_id") or "").strip()
        if wanted:
            children = [child for child in children if child.id == wanted]
            if not children:
                raise ToolError(
                    f"{wanted} 不是本会话派出的子 agent。"
                    "不填 session_id 可以看全部。"
                )
        if not children:
            return ToolResult(output="本会话还没有派出过子 agent。")

        timeout = arguments.get("timeout")
        timeout = 20 if not isinstance(timeout, int) else min(60, max(1, timeout))
        if arguments.get("wait"):
            await host.wait([child.id for child in children], timeout)

        shown = children[:MAX_REPORTS]
        lines: list[str] = []
        for child in shown:
            lines.append(
                f"■「{child.title}」（{child.id}）"
                f"　{_status_text(child.status)} · {len(child.messages)} 条 · "
                f"{child.total_tokens()} tok"
            )
            if child.status_note:
                lines.append(f"  说明：{child.status_note}")
            result = _last_result(child)
            lines.append("  结论：" + (result.replace("\n", "\n  ") if result else "（还没有回复）"))
            files = _output_files(child)
            if files:
                shown_files = "、".join(files[:5])
                if len(files) > 5:
                    shown_files += f" 等 {len(files)} 个"
                lines.append(f"  产出：{shown_files}")
        if len(children) > len(shown):
            lines.append(
                f"另有 {len(children) - len(shown)} 个没有列出；"
                "给 session_id 可以单独查。"
            )
        return ToolResult(
            output=_cap_report("\n".join(lines)),
            meta={"count": len(children), "wait": bool(arguments.get("wait"))},
        )
