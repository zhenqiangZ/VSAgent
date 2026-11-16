"""子 agent 的派发：把父会话的一段消息复制成一条独立会话，并交给它一个任务。

这个模块放三样东西：

- `fork_messages`：纯数据复制，不碰事件、不碰界面；
- `DispatchCampaignTool`：唯一的派发工具，按任务规格派给专业角色；
- `AgentStatusTool`：父会话取回子 agent 状态与报告的那条路。

真正「起一条后台任务」的是 `AgentHost`（`host.py`）。分开的理由和 `task.py`
一样：工具层不该知道怎么调度，调度层也不该知道模型的参数长什么样。
"""

from __future__ import annotations

import json
import time
from typing import Any

from vsagent.core.models import Message, Part, PartType, Session
from vsagent.core.agent.execution import campaign_state
from vsagent.core.agent.roles.prompt import estimate_tokens, translate_message
from vsagent.core.agent.tools import Tool, ToolError, ToolResult, ToolSpec

# 复制时跳过的 Part：它们是「上一轮怎么跑的」过程标记，不是对话内容。
# 留着会让子 agent 的历史里出现一堆别人的 step 编号，既占额度又误导。
# AGENT 是父会话里的派发标记（点一下进另一个子 agent），更不该跟着复制：
# 复制过去之后，子 agent 的界面里会多出一条指向兄弟会话的紫色行。
SKIPPED_PARTS = frozenset(
    {
        PartType.STEP_START,
        PartType.STEP_FINISH,
        PartType.RETRY,
        PartType.COMPACTION,
        PartType.AGENT,
        # 父会话的意图解析：子 agent 的任务书由父会话重写，带上这一段只会
        # 让它看见一份不是自己的任务规格。
        PartType.ROUTE,
    }
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
    而 fresh 的只带着任务书，不给它就得自己调一次 `registry`，白烧一步。
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


class DispatchCampaignTool(Tool):
    """按当前任务规格发起一次多角色执行。

    这是**唯一**的派发入口：角色与依赖都由能力计划算出来，工具由每个角色的配置决定；
    `roles` 只能从
    计划里减，不能加。子 agent 之间不递归派发，深度守卫在下面。
    """

    spec = ToolSpec(
        name="dispatch_campaign",
        description=(
            "当任务规格已经明确、需要专业角色分工时，用这个工具排一次计划并派发。"
            "它按能力计划决定派谁、谁等谁，能并行的并行。`roles` 可以从计划里选子集"
            "（上游会自动补上），也可以点名计划外的角色——按它的能力表补进来，"
            "只选一个角色时 `task` 可以改写任务书。\n"
            "不适用：一次调用就能做完的事、或下一步要用到你此刻的推理时，不要派"
            "（子 agent 看不到你的推理）。缺任务规格时先让用户把目标、对象与交付物说清楚。\n"
            "confirm=false 只返回计划预览（角色、谁等谁），不建会话、不跑计算；"
            "用户同意后再用 confirm=true。同一份计划不会重复派发；同一会话同时只能有一场。\n"
            "副作用：每个角色新建一个子会话，产物写进共享的运行目录与登记表。"
            "返回计划文字；**不含结果**——之后用 agent_status 取。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "false=只预览计划（推荐第一次这样调用）；true=真正启动。"
                        "必填，没有默认值。示例：{\"confirm\": false}"
                    ),
                },
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "只派这几种角色（括号内的名字，如 pocket）；上游会自动补上。"
                        "计划里没有的角色会按其能力表补进这次派发（用于中途发现要补一步）。"
                        "不给则整份计划都派"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "自定义任务书，只给 roles 指定的那一个角色用；"
                        "不给则按子目标自动拼"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "整场最多等多少分钟（1–720）。留空按每个角色说明书里的时限；"
                        "到点未完成记失败，不再等"
                    ),
                },
            },
            "required": ["confirm"],
            "additionalProperties": False,
        },
    )

    session_argument = "session"
    spawn_argument = "spawner"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        from vsagent.core.agent.planning.capability_router import (
            CapabilityRouter,
            RouterError,
            Routing,
            missing_prerequisites,
        )
        from vsagent.core.agent.roles.contracts import TaskSpec
        from vsagent.core.agent.execution.coordinator import Coordinator, build_tasks, role_label
        from vsagent.core.agent.execution.dispatcher import AgentDispatcher
        from vsagent.core.agent.execution.scheduler import AgentScheduler

        session: Session = arguments["session"]
        host = arguments["spawner"]
        if session.depth >= 1:
            raise ToolError(
                "子 agent 不能发起多角色执行：把这一段做完，把结论交回父会话。"
            )
        if not session.task_spec:
            raise ToolError(
                "还没有任务规格，不能发起多角色执行。先把要做什么说清楚"
                "（目标、对象、交付物），再调用一次。"
            )
        try:
            spec = TaskSpec.from_dict(session.task_spec)
        except Exception as error:  # noqa: BLE001 - 坏状态当成"读不出来"，不往外抛协议错误
            raise ToolError(f"当前任务规格读不出来：{error}") from error

        routing = CapabilityRouter().route(spec)
        roles = _requested_roles(arguments)
        if roles:
            # 计划外的角色按能力表补进来：中途发现要结构盘点时，不必等用户改口
            # 重新解析（P-83）。拼错的角色名由 `extend` 拒掉。
            try:
                extended = CapabilityRouter().extend(
                    routing.plan, roles, provided=spec.available_inputs
                )
            except RouterError as error:
                raise ToolError(str(error)) from error
            if extended.plan is not routing.plan:
                routing = Routing(
                    plan=extended.plan,
                    warnings=routing.warnings + extended.warnings,
                )
        custom = str(arguments.get("task") or "").strip()
        tasks, warnings = build_tasks(routing.plan, spec, roles=roles, task=custom)
        # 路由那边的告警也要带上：补派了哪些计划外角色、补了哪些前置，都在那里。
        warnings = list(routing.warnings) + list(warnings)
        if not tasks:
            raise ToolError(
                "这份任务规格里没有可派的角色：先确认要查什么、要交什么，再发起。"
            )
        if custom:
            # 按**请求的**角色判，不按补完上游后的任务数：请求一个下游角色时，
            # 上游是按依赖补进来的，不该因此把这条路堵死（P-83）。
            single = list(roles) if roles is not None else list(routing.plan.selected_agents)
            if len(single) != 1:
                names = "、".join(single)
                raise ToolError(
                    f"自定义任务书一次只给一个角色；现在指定了 {len(single)} 个"
                    f"（{names}）。用 roles 指定是哪一个，或去掉 task。"
                )
        preview = _campaign_preview(routing.plan, tasks, warnings)
        minutes = _campaign_timeout(arguments.get("timeout"))
        if minutes:
            preview += f"\n整场上限：{minutes:g} 分钟，到点未完成记失败。"
        # 前置不齐的节点不能派：调度器没有任何东西可以喂给它，跑起来也只是空转。
        # 只判**这次要派的角色**——只派上游时，另一个分支缺前置不该拦这一场。
        missing = missing_prerequisites(routing.plan, [task.role for task in tasks])
        if missing:
            blocked = (
                "**不可派发：缺前置产物**\n- "
                + "\n- ".join(missing)
                + "\n先把缺的那一步加进计划（重新解析意图或补子目标），"
                "或者只派已经齐了的上游角色。"
            )
            if not arguments.get("confirm"):
                # 预演不建会话，留着让用户看见「本来要派谁、卡在哪」。
                return ToolResult(
                    output=preview + "\n\n" + blocked,
                    meta={"confirm": False, "agents": len(tasks), "blocked": True},
                )
            raise ToolError(blocked)
        # 计划编出来了：这一步就是「排计划」。预演也算——它确实排了一遍。
        _advance_state(session, campaign_state.PLANNING)
        if not arguments.get("confirm"):
            return ToolResult(
                output=preview + "\n\n这只是预览。确认之后用 confirm=true 启动。",
                meta={"confirm": False, "agents": len(tasks)},
            )
        if host.campaign_running(session.id):
            stale = campaign_state.PlanStamp(
                session.campaign_task_id, session.campaign_revision
            ).stale_reason(spec.task_id, spec.revision)
            if stale:
                raise ToolError(
                    f"{stale}。现在这一场还在跑，先让用户决定要不要停："
                    "停下之后按新规格重新发起。"
                )
            raise ToolError(
                "这个会话已经有一场多角色执行在跑：用 agent_status 看进度，"
                "不要重复发起。"
            )

        _advance_state(session, campaign_state.DELEGATING)
        coordinator = Coordinator(AgentScheduler(AgentDispatcher(host)))
        started = host.start_campaign(
            session,
            coordinator.run(
                session,
                routing.plan,
                spec,
                timeout=(minutes * 60 if minutes else None),
                roles=roles,
                task=custom,
            ),
            on_done=lambda campaign: _record_campaign(session, campaign),
        )
        if not started:
            raise ToolError("启动失败：这个会话已经有一场多角色执行在跑。")
        # 记下这份计划是按哪一版规格编的。之后用户改口，拿它比一次就知道该不该作废。
        session.campaign_task_id = routing.plan.task_id
        session.campaign_revision = spec.revision
        return ToolResult(
            output=(
                preview
                + "\n\n已启动，在后台跑。用 agent_status 看进度与结论；"
                "需要停下时告诉用户，由用户中断。"
            ),
            meta={"confirm": True, "agents": len(tasks), "task_id": routing.plan.task_id},
        )


def _campaign_timeout(raw: Any) -> float:
    """整场上限（分钟）。0 表示没给；超过一天按一天算。

    不合法的值当没给：宁可跑完，也不要因为一个字符串把整次派发拒掉。
    """
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return min(720.0, value)


def _requested_roles(arguments: dict[str, Any]) -> list[str] | None:
    """解析 `roles`。不给返回 None；给了就只校验形状，角色名由 `extend` 判。

    计划外的角色是允许的（按能力表补进计划），所以这里不能拿计划当白名单：
    那样一来「中途发现要结构盘点」就又被拒一次。
    """
    raw = arguments.get("roles")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ToolError('roles 要写成数组，例如 ["pocket"]')
    roles = [str(item).strip() for item in raw if str(item).strip()]
    if not roles:
        raise ToolError("roles 里没有有效的角色名")
    return roles


def _campaign_line(session: Session) -> str:
    """这件事走到哪一步，以及手上这份计划是不是已经作废。

    只报「当前是什么」：调用方是模型，它要据此决定下一步做什么，不是看历史。
    """
    state = session.campaign_state
    if not state:
        return ""
    line = f"多角色执行：{campaign_state.label(state)}（{state}）"
    spec = session.task_spec or {}
    stale = campaign_state.PlanStamp(
        session.campaign_task_id, session.campaign_revision
    ).stale_reason(
        str(spec.get("task_id") or ""), int(spec.get("revision") or 0)
    )
    if stale:
        line += f"\n注意：{stale}"
    return line


def _advance_state(session: Session, target: str) -> str:
    """推进战役状态，返回跳不过去的理由（能跳时为空串）。

    跳不过去不改状态：这是记账，不是闸门。真正需要拦住的情形（重复派发、
    计划作废）由调用方单独判，不靠状态机兜。
    """
    state, reason = campaign_state.soft_advance(session.campaign_state, target)
    session.campaign_state = state
    return reason


def _campaign_preview(plan: Any, tasks: list[Any], warnings: list[str]) -> str:
    """派发前的预演：派谁、谁等谁。不查状态，只说计划本身。

    导入放在函数里：`coordinator` → `scheduler` → `dispatcher` → `host` 这条链
    会绕回本模块，放在文件顶部就成了循环导入。
    """
    from vsagent.core.agent.execution.coordinator import role_label

    roles = {task.task_id: task.role for task in tasks}
    lines = [f"这次会派 {len(tasks)} 个角色（计划 {plan.task_id}）："]
    for task in tasks:
        goal = task.objective
        if len(goal) > 40:
            goal = goal[:40] + "…"
        waits = "、".join(
            role_label(roles.get(dep, dep)) for dep in task.dependencies
        )
        tail = f"；等 {waits}" if waits else ""
        lines.append(f"- {role_label(task.role)}（{task.role}）：{goal}{tail}")
    if warnings:
        lines.extend(f"注意：{item}" for item in warnings)
    dispatched = {task.role for task in tasks}
    pending = [role for role in plan.selected_agents if role not in dispatched]
    if pending:
        # 按子集派发时，计划里剩下的角色不会自己跑；不说的话，主 agent 会以为
        # 整场已经跑完（2026-11-13 的 HA2 会话就是这样漏掉 screening 的）。
        lines.append(
            "计划里还有没派的角色："
            + "、".join(f"{role_label(role)}（{role}）" for role in pending)
            + "；要跑它们就单独派一次。"
        )
    return "\n".join(lines)


def _record_campaign(session: Session, campaign: Any) -> None:
    """把这次执行的结果写进任务状态。只追加，不清掉模型自己记的进度。"""
    done, todo, blocked = campaign.progress()
    pending = [str(role) for role in getattr(campaign, "pending", ()) or ()]
    if pending:
        # 没派的角色要进待办：不然下一轮看任务状态，会以为整份计划都跑过了。
        todo = [*todo, f"计划里还没派：{'、'.join(pending)}（要跑就单独派一次）"]
    state = session.task_state
    updates: dict[str, list[str]] = {}
    for name, fresh in (("done", done), ("todo", todo), ("blockers", blocked)):
        if not fresh:
            continue
        current = list(getattr(state, name))
        updates[name] = current + [item for item in fresh if item not in current]
    if updates:
        state.apply(**updates)
    session.touch()


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


def _report_lines(child: Session) -> list[str]:
    """结构化派发的子 agent：补一段按角色判过的报告摘要。

    自由文本派发的子会话没有角色，这里什么都不加——它们的结论已经在上一行。
    不合格时不重写一份"看起来能做"的摘要，而是把缺什么直接写出来，
    父会话据此决定要不要让它补交。
    """
    from vsagent.core.agent.roles.profiles import profile
    from vsagent.core.agent.execution.reports import build

    try:
        spec = profile(child.agent_role)
    except KeyError:
        return [f"  报告：角色 {child.agent_role} 不在说明书里，无法判"]
    built = build(
        child, spec, task_id=child.task_id, campaign_id=child.campaign_id
    )
    if built.acceptable:
        head = f"  报告：合格（{child.agent_role}）"
    else:
        head = f"  报告：不合格（{child.agent_role}）—— " + "；".join(built.problems)
    lines = [head]
    report = built.report
    if report.artifact_refs:
        lines.append(f"  产物句柄：{'、'.join(report.artifact_refs[:5])}")
    if report.evidence_refs:
        lines.append(f"  证据：{'、'.join(report.evidence_refs[:3])}")
    return lines


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


def _report_evidence(child: Session) -> list[dict]:
    """子会话报告里的证据对象，供父会话建立一场的证据池。

    与 `_report_lines` 用的是同一份报告；证据对象不塞进给模型看的那段文字，
    只放进工具结果的 `meta` 里，由循环层交给答案状态。
    """
    from vsagent.core.agent.roles.profiles import profile
    from vsagent.core.agent.execution.reports import build

    try:
        spec = profile(child.agent_role)
    except KeyError:
        return []
    built = build(
        child, spec, task_id=child.task_id, campaign_id=child.campaign_id
    )
    # 只带父会话建立证据池需要的字段，正文截短：这份元数据会写进状态文件，
    # 一条证据的全文留在子会话的答案状态里。
    trimmed: list[dict] = []
    for item in built.report.evidence:
        trimmed.append(
            {
                "id": item.get("id", ""),
                "content": str(item.get("content") or "")[:400],
                "source": item.get("source", ""),
                "source_type": item.get("source_type", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "published_at": item.get("published_at", ""),
                "metadata": {
                    key: value
                    for key, value in (item.get("metadata") or {}).items()
                    if key in ("pmid", "doi", "ref", "handle")
                },
            }
        )
    return trimmed


class AgentStatusTool(Tool):
    """查看子 agent 的状态与结论，可选等它们跑完。

    这是父会话取回结果的那条路：`dispatch_campaign` 只负责派出去，
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
            head = _campaign_line(session)
            return ToolResult(
                output=(head + "\n" if head else "") + "本会话还没有派出过子 agent。"
            )

        timeout = arguments.get("timeout")
        timeout = 20 if not isinstance(timeout, int) else min(60, max(1, timeout))
        if arguments.get("wait"):
            await host.wait([child.id for child in children], timeout)

        shown = children[:MAX_REPORTS]
        lines: list[str] = []
        evidence: list[dict] = []
        head = _campaign_line(session)
        if head:
            lines.append(head)
        for child in shown:
            status = "待开始" if child.has_pending_prompt else _status_text(child.status)
            lines.append(
                f"■「{child.title}」（{child.id}）"
                f"　{status} · {len(child.messages)} 条 · "
                f"{child.total_tokens()} tok"
            )
            if child.status_note:
                lines.append(f"  说明：{child.status_note}")
            result = _last_result(child)
            lines.append("  结论：" + (result.replace("\n", "\n  ") if result else "（还没有回复）"))
            if child.agent_role and child.status in ("done", "failed", "cancelled"):
                lines.extend(_report_lines(child))
                evidence.extend(_report_evidence(child))
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
            meta={
                "count": len(children),
                "wait": bool(arguments.get("wait")),
                # 证据对象交给循环层，不进给模型看的那段文字。
                "evidence": evidence[:30],
            },
        )
