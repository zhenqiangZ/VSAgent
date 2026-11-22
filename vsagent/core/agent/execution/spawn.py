"""子 agent 的派发：把父会话的一段消息复制成一条独立会话，并交给它一个任务。

这个模块放两样东西：

- `fork_messages`：纯数据复制，不碰事件、不碰界面；
- `DispatchCampaignTool`：唯一的派发工具，按任务规格派给专业角色，并等它们全部结束。

真正「起一条后台任务」的是 `AgentHost`（`host.py`）。分开的理由和 `task.py`
一样：工具层不该知道怎么调度，调度层也不该知道模型的参数长什么样。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from vsagent.core.models import Message, Part, PartType, Session
from vsagent.core.agent.execution import campaign_state
from vsagent.core.agent.execution.projection import RolePlan
from vsagent.core.agent.roles import catalog
from vsagent.core.agent.roles.contracts.task import ACCEPTANCE_KINDS
from vsagent.core.agent.roles.prompt import estimate_tokens, translate_message
from vsagent.core.agent.roles.templates import template_for
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
    *,
    constraints: Iterable[str] | None = None,
    goal: str = "",
) -> tuple[int, int]:
    """把父会话 `[start, end)` 复制进子会话，返回 (复制条数, 因预算丢掉的条数)。

    **每条消息、每个 Part 都重新生成 ID**：ID 序列是全局的，复用会让两个会话里
    出现同号的 message/part，事件路由与 `part_by_id` 都会指错。

    `constraints` 给了就只写这一份；留 `None` 时沿用旧行为，复制父会话的约束。
    结构化派发要给，因为每个角色只该看到自己那几条；自由文本派发没有任务书，
    沿用父会话的约束才有内容。`goal` 留空时用子会话标题。
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
    # 结构化派发给 constraints 时不再复制父会话的整份清单：每个角色只该看到
    # 自己那几条，否则一句只针对口袋的要求会跟着所有子 agent 跑。
    if constraints is None:
        child.task_state.constraints = list(parent.task_state.constraints)
    else:
        child.task_state.constraints = list(constraints)
    child.task_state.goal = goal.strip() or child.title
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

    这是**唯一**的派发入口：角色与依赖都由任务规格与角色前置算出来，工具由每个角色的配置决定；
    `roles` 只能从计划里减，加角色要先改任务规格。子 agent 之间不递归派发，深度守卫在下面。
    """

    spec = ToolSpec(
        name="dispatch_campaign",
        description=(
            "当任务规格已经明确、需要专业角色分工时，用这个工具排一次计划并派发。"
            "它按任务规格里的子目标决定派谁、谁等谁，能并行的并行。`roles` 只能从计划里选子集"
            "（上游会自动补上），不能加计划外的角色；只选一个角色时 `task` 可以改写任务书。\n"
            "`constraints` 由你按每个角色这一段的任务写：键是角色名或 global，值是要求原文。"
            "它不从父会话自动抄；没写的角色只带角色说明书里的固定限制。"
            "confirm=true 时必填，没有额外限制就传 {}。\n"
            "路由自动补出来的上游角色在任务规格里没有子目标；给它们写 `plans`"
            "（每个角色一段 goals 与 acceptance_criteria），否则 confirm=true 会拒绝派发。"
            "任务规格里已经有子目标的角色也可以补写，条目只增不减。\n"
            "不适用：一次调用就能做完的事、或下一步要用到你此刻的推理时，不要派"
            "（子 agent 看不到你的推理）。缺任务规格时先让用户把目标、对象与交付物说清楚。\n"
            "confirm=false 只返回计划预览（角色、谁等谁），不建会话、不跑计算；"
            "用户同意后再用 confirm=true。同一会话同时只能有一场；计划里已经跑完的"
            "角色再派一次会开新的子会话，不会把上一轮的结论原样带回来。\n"
            "confirm=true 会一直等到所有子 agent 结束，再带回每个角色的结论与报告判定；"
            "用户中断时这场执行连子 agent 一起停。\n"
            "副作用：每个角色新建一个子会话，产物写进共享的运行目录与登记表。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "false=只预览计划（推荐第一次这样调用）；true=真正启动并等全部结束。"
                        "必填，没有默认值。示例：{\"confirm\": false}"
                    ),
                },
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "只派这几种角色（括号内的名字，如 pocket）；上游会自动补上。"
                        "只能从当前计划里选，计划外的角色会被拒绝。"
                        "不给则整份计划都派"
                    ),
                },
                "constraints": {
                    "type": "object",
                    "description": (
                        "每个子 agent 的限制。键是角色名（如 pocket）或 global（写给所有角色），"
                        "值是字符串数组。按这个角色这一段的任务写它该遵守什么；"
                        "不写就没有额外限制。confirm=true 时必填，没有就传 {}"
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "task": {
                    "type": "string",
                    "description": (
                        "自定义任务书，只给 roles 指定的那一个角色用；"
                        "不给则按子目标自动拼。同一个角色不要再写 plans.goals"
                    ),
                },
                "plans": {
                    "type": "object",
                    "description": (
                        "按角色补写的计划。键是这次要派的角色名；每格给 goals 与 "
                        "acceptance_criteria 至少一项。它只影响这一次投影，不写回任务规格"
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "goals": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "enum": list(ACCEPTANCE_KINDS),
                                        },
                                        "target": {"type": "string"},
                                        "output": {"type": "string"},
                                    },
                                    "required": ["kind", "target"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "additionalProperties": False,
                    },
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
        from vsagent.core.agent.planning.agent_router import (
            AgentRouter,
            missing_prerequisites,
        )
        from vsagent.core.agent.roles.contracts import TaskSpec
        from vsagent.core.agent.execution.coordinator import Coordinator, build_tasks
        from vsagent.core.agent.execution.dispatcher import AgentDispatcher
        from vsagent.core.agent.execution.scheduler import AgentScheduler

        session: Session = arguments["session"]
        host = arguments["spawner"]
        if session.depth >= 1:
            raise ToolError(
                "子 agent 不能发起多角色执行：把这一段做完，把结论交回父会话。"
            )
        blocked = str(getattr(session, "route_blocked_reason", "") or "").strip()
        if blocked:
            raise ToolError(f"这一轮不能派发：{blocked}")
        if not session.task_spec:
            raise ToolError(
                "还没有任务规格，不能发起多角色执行。先把要做什么说清楚"
                "（目标、对象、交付物），再调用一次。"
            )
        try:
            spec = TaskSpec.from_dict(session.task_spec)
        except Exception as error:  # noqa: BLE001 - 坏状态当成"读不出来"，不往外抛协议错误
            raise ToolError(f"当前任务规格读不出来：{error}") from error

        routing = AgentRouter().route(spec)
        roles = _requested_roles(arguments)
        if roles:
            # 计划是这一轮的边界：roles 只能从里面减，不能加。
            # 计划外的角色没有子目标、交付物与验收条件，派出去只能重复父任务。
            from vsagent.core.agent.roles import catalog

            unknown = [role for role in roles if role not in catalog.AGENTS]
            if unknown:
                raise ToolError(
                    f"没有这个角色：{'、'.join(unknown)}。"
                    f"可用：{'、'.join(catalog.AGENTS)}"
                )
            outside = [
                role for role in roles if role not in routing.plan.selected_agents
            ]
            if outside:
                raise ToolError(
                    f"这个角色不在这份计划里：{'、'.join(outside)}。"
                    f"这次计划只有 {'、'.join(routing.plan.selected_agents)}。"
                    "要加角色，先让用户把要补的子目标说清楚，下一轮重新解析后再派；"
                    "不要用 roles 硬加。"
                )
        custom = str(arguments.get("task") or "").strip()
        # 限制由父 agent 在派发这一刻按角色写；不再从父会话的任务状态里按前缀挑。
        from vsagent.core.agent.execution.projection import select_roles

        chosen = (
            select_roles(routing.plan, roles)
            if roles
            else list(routing.plan.selected_agents)
        )
        written = _written_constraints(arguments.get("constraints"), chosen)
        if written is None and arguments.get("confirm"):
            raise ToolError(
                "派发前要写清楚每个子 agent 的限制。"
                "按角色写成一张表，键是角色名或 global，值是字符串数组；"
                "这一场没有额外限制就传 {}。"
            )
        plans = _role_plans(arguments.get("plans"), chosen)
        single = list(roles) if roles is not None else list(routing.plan.selected_agents)
        if custom and len(single) == 1:
            target = single[0]
            planned = plans.get(target) if plans else None
            if planned is not None and planned.goals:
                raise ToolError(
                    f"task 与 plans[\"{target}\"].goals 只能留一个："
                    "自定义任务书已经替代了这个角色的子目标"
                )
        tasks, warnings = build_tasks(
            routing.plan,
            spec,
            roles=roles,
            task=custom,
            constraints=written,
            role_plans=plans,
        )
        # 路由那边的告警也要带上：补派了哪些计划外角色、补了哪些前置，都在那里。
        warnings = list(routing.warnings) + list(warnings)
        if not tasks:
            raise ToolError(
                "这份任务规格里没有可派的角色：先确认要查什么、要交什么，再发起。"
            )
        if custom:
            # 按**请求的**角色判，不按补完上游后的任务数：请求一个下游角色时，
            # 上游是按依赖补进来的，不该因此把这条路堵死（P-83）。
            if len(single) != 1:
                names = "、".join(single)
                raise ToolError(
                    f"自定义任务书一次只给一个角色；现在指定了 {len(single)} 个"
                    f"（{names}）。用 roles 指定是哪一个，或去掉 task。"
                )
        covered = {str(row.get("agent") or "") for row in spec.subgoals}
        covered.update(role for role, plan in (plans or {}).items() if plan.goals)
        if custom and len(single) == 1:
            covered.add(single[0])
        missing_plan = [task.role for task in tasks if task.role not in covered]
        if missing_plan and arguments.get("confirm"):
            raise ToolError(
                "这些角色没有子目标，不能直接派发："
                + "、".join(missing_plan)
                + "。路由补出来的上游角色要在 `plans` 里给每个角色写 goals"
                "（可以再加 acceptance_criteria），再调用一次。"
            )
        preview = _campaign_preview(routing.plan, tasks, warnings)
        if written is None and spec.constraints:
            # 预览时还没写限制：把任务规格里的原话摆出来，提醒它按角色重写。
            # 这些原话不会自己流进任务书——那正是这次改动要去掉的「拼接」。
            preview += (
                "\n任务规格里还有这些要求："
                + "；".join(spec.constraints[:6])
                + "\n确认时按角色写进 constraints；不写不会下发给子 agent。"
            )
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
                "这个会话已经有一场多角色执行在跑：等它结束，不要重复发起。"
            )

        _advance_state(session, campaign_state.DELEGATING)
        from vsagent.core.agent.execution.reports import prepare_report_dir

        # 目录在开跑前就建：写不进去属于启动条件不成立，不该等整场跑完才报错，
        # 那时子 agent 的结论已经花掉了，父会话却只拿到一个工具失败。
        prepare_report_dir(session)
        coordinator = Coordinator(AgentScheduler(AgentDispatcher(host)))
        # 这一场真正派出的子会话号。收报告时按它取，不能按任务号或计划号取：
        # 延续任务再次派发会开新的子会话，旧的那几个也带着同一个计划号。
        dispatched: list[str] = []
        started = host.start_campaign(
            session,
            coordinator.run(
                session,
                routing.plan,
                spec,
                timeout=(minutes * 60 if minutes else None),
                roles=roles,
                task=custom,
                constraints=written,
                role_plans=plans,
                on_dispatch=lambda task, child: dispatched.append(child.id),
            ),
            on_done=lambda campaign: _record_campaign(session, campaign),
        )
        if not started:
            raise ToolError("启动失败：这个会话已经有一场多角色执行在跑。")
        # 记下这份计划是按哪一版规格编的。之后用户改口，拿它比一次就知道该不该作废。
        session.campaign_task_id = routing.plan.task_id
        session.campaign_revision = spec.revision
        campaign, error, cancelled = await host.wait_campaign(session.id)
        children = _campaign_children(session, host, dispatched)
        if cancelled:
            saved, _, index = _write_reports(session, children, routing.plan.task_id)
            return ToolResult(
                output=(
                    preview
                    + "\n\n已中断：这场执行和它派出的子 agent 都停了。"
                    "需要继续时，让用户重新确认后再派一次。"
                    + _saved_lines(children, saved, index)
                ),
                meta={
                    "confirm": True,
                    "agents": len(tasks),
                    "task_id": routing.plan.task_id,
                    "cancelled": True,
                },
            )
        if error or campaign is None:
            saved, _, index = _write_reports(session, children, routing.plan.task_id)
            return ToolResult(
                output=(
                    preview
                    + f"\n\n战役没有正常结束：{error or '没有拿到结果'}"
                    "。子任务的状态在任务状态与右侧子 agent 列表里，不要直接重派。"
                    + _saved_lines(children, saved, index)
                ),
                meta={
                    "confirm": True,
                    "agents": len(tasks),
                    "task_id": routing.plan.task_id,
                    "error": error,
                },
            )
        body, evidence = _campaign_result(session, children, campaign)
        return ToolResult(
            output=preview + "\n\n" + body,
            meta={
                "confirm": True,
                "agents": len(tasks),
                "task_id": routing.plan.task_id,
                "evidence": evidence,
            },
        )


def _campaign_children(
    session: Session, host: Any, dispatched: Iterable[str]
) -> list[Session]:
    """这一场真正派出的子会话，按创建时间排序。

    按派发时记下的会话号取。同一份计划下的旧子会话也带着相同的任务号与
    计划号，按它们筛选会把上一轮的会话一起收进这一场的报告里。
    """
    wanted = set(dispatched)
    children = [
        child for child in host.children_of(session.id) if child.id in wanted
    ]
    children.sort(key=lambda item: item.created_at)
    return children


def _built_report(child: Session) -> Any:
    """给一个子会话建报告。自由文本派发或角色不认识时返回 None。"""
    from vsagent.core.agent.roles.profiles import profile
    from vsagent.core.agent.execution.reports import build

    if not child.agent_role:
        return None
    try:
        spec = profile(child.agent_role)
    except KeyError:
        return None
    return build(
        child, spec, task_id=child.task_id, campaign_id=child.campaign_id
    )


def _write_reports(
    session: Session, children: Iterable[Session], key: str
) -> tuple[dict[str, str], dict[str, Any], str]:
    """每个子会话写一份完整报告，再写这一场的索引。

    返回（子会话 id 到路径, 子会话 id 到报告, 索引路径）。报告只建一次：
    列表、证据与磁盘那一份用的是同一个判定结果。
    """
    from vsagent.core.agent.execution.reports import save_index, save_report

    paths: dict[str, str] = {}
    built: dict[str, Any] = {}
    for child in children:
        report = _built_report(child)
        built[child.id] = report
        paths[child.id] = save_report(
            session, child, _report_markdown(session, child, report)
        )
    index = save_index(session, _report_index(session, children, paths), key)
    return paths, built, index


def _saved_lines(
    children: Iterable[Session], paths: dict[str, str], index: str
) -> str:
    """中断或出错时附在结果后面：报告已经收下，在哪读。"""
    if not paths:
        return ""
    lines = [f"\n子 agent 的报告已经写好，全文索引：{index}"]
    for child in children:
        path = paths.get(child.id, "")
        if path:
            lines.append(f"- {child.id}（{_status_text(child.status)}）：{path}")
    return "\n".join(lines)


def _campaign_result(
    session: Session, children: list[Session], campaign: Any
) -> tuple[str, list[dict]]:
    """一场战役结束后给父会话看的那一段：每个角色交了什么都写清楚。"""
    # 先给每一份报告写文件：下面的条目会被 1500 token 的预算整段切掉，
    # 磁盘上的那份不会。超过 MAX_REPORTS 的子 agent 也因此有全文可读。
    paths, built, index = _write_reports(session, children, campaign.plan.task_id)
    lines: list[str] = [
        f"已全部结束：{campaign.dispatched()} 个角色（计划 {campaign.plan.task_id}）",
        f"全文索引：{index}（每个子 agent 的全文路径写在索引里，用 read 读）",
    ]
    evidence: list[dict] = []
    for child in children[:MAX_REPORTS]:
        status = "待开始" if child.has_pending_prompt else _status_text(child.status)
        lines.append(
            f"■「{child.title}」（{child.id}）"
            f"　{status} · {len(child.messages)} 条 · "
            f"{child.total_tokens()} tok"
        )
        if child.status_note:
            lines.append(f"  说明：{child.status_note}")
        result = _last_result(child)
        lines.append(
            "  结论：" + (result.replace("\n", "\n  ") if result else "（还没有回复）")
        )
        if child.agent_role and child.status in ("done", "failed", "cancelled"):
            lines.extend(_report_lines(child, built[child.id]))
            evidence.extend(_report_evidence(child, built[child.id]))
        files = _output_files(child)
        if files:
            shown = "、".join(files[:5])
            if len(files) > 5:
                shown += f" 等 {len(files)} 个"
            lines.append(f"  产出文件：{shown}")
    if len(children) > MAX_REPORTS:
        lines.append(
            f"（还有 {len(children) - MAX_REPORTS} 个子 agent 没列，"
            f"全文都在索引里）"
        )
    pending = list(getattr(campaign, "pending", ()) or ())
    if pending:
        lines.append(f"计划里还有没派的角色：{'、'.join(pending)}")
    return _cap_report("\n".join(lines), index=index), evidence


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
    """解析 `roles`。不给返回 None；给了只校验形状，角色名在 `run` 里对计划判。

    `roles` 只做减法：可以少派计划里的角色，不能加计划外的角色。
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


def _role_plans(raw: Any, chosen: Iterable[str]) -> dict[str, RolePlan] | None:
    """把派发时补写的计划理成 {角色: RolePlan}。没写返回 None。

    键只认这一场实际派发的角色。验收条件指向的交付物必须在这个角色的模板里；
    角色只有一个交付物时可以省 `output`，多个就必须写明，不替它猜。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ToolError(
            'plans 要写成一张表，例如 {"prepare": {"goals": ["准备受体与配体"]}}'
        )

    names = list(chosen)
    out: dict[str, RolePlan] = {}
    for key, value in raw.items():
        role = str(key).strip()
        if not role:
            raise ToolError("plans 的键要是角色名，不能是空串")
        if role not in names:
            if role in catalog.AGENTS:
                raise ToolError(
                    f"这次不派 {role}，写计划也没有人去执行。"
                    f"这次派的是：{'、'.join(names)}"
                )
            raise ToolError(f"没有这个角色：{role}。可用：{'、'.join(catalog.AGENTS)}")
        if not isinstance(value, dict):
            raise ToolError(f"plans 里 {role} 的值要写成对象")
        extra = set(value) - {"goals", "acceptance_criteria"}
        if extra:
            raise ToolError(
                f"plans 里 {role} 有不认识的字段：{'、'.join(sorted(extra))}；"
                "可用 goals、acceptance_criteria"
            )
        goals_raw = value.get("goals") or []
        if isinstance(goals_raw, str):
            goals_raw = [goals_raw]
        if not isinstance(goals_raw, (list, tuple)) or any(
            not isinstance(item, str) for item in goals_raw
        ):
            raise ToolError(f"plans 里 {role}.goals 要写成字符串数组")
        goals = tuple(item.strip() for item in goals_raw if item.strip())

        rows = value.get("acceptance_criteria") or []
        if not isinstance(rows, (list, tuple)):
            raise ToolError(f"plans 里 {role}.acceptance_criteria 要写成数组")
        outputs = template_for(role).required_outputs
        criteria: list[dict[str, str]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ToolError(
                    f"plans 里 {role}.acceptance_criteria[{index}] 要写成对象"
                )
            extra = set(row) - {"kind", "target", "output"}
            if extra:
                raise ToolError(
                    f"plans 里 {role}.acceptance_criteria[{index}] 有不认识的字段："
                    f"{'、'.join(sorted(extra))}"
                )
            kind = str(row.get("kind") or "").strip()
            if kind not in ACCEPTANCE_KINDS:
                raise ToolError(
                    f"plans 里 {role}.acceptance_criteria[{index}].kind 不认识：{kind!r}。"
                    f"可用：{'、'.join(ACCEPTANCE_KINDS)}"
                )
            target = str(row.get("target") or "").strip()
            if not target:
                raise ToolError(
                    f"plans 里 {role}.acceptance_criteria[{index}].target 不能为空"
                )
            output = str(row.get("output") or "").strip()
            if output:
                if output not in outputs:
                    raise ToolError(
                        f"plans 里 {role}.acceptance_criteria[{index}].output 不在"
                        f"这个角色的交付物里：{output!r}。"
                        f"可用：{'、'.join(outputs)}"
                    )
            elif len(outputs) == 1:
                output = outputs[0]
            else:
                raise ToolError(
                    f"plans 里 {role}.acceptance_criteria[{index}] 要写明 output，"
                    f"这个角色有多个交付物：{'、'.join(outputs)}"
                )
            criteria.append(
                {"kind": kind, "target": target, "output": output}
            )
        if not goals and not criteria:
            raise ToolError(
                f"plans 里 {role} 是空的：goals 与 acceptance_criteria 至少写一项"
            )
        out[role] = RolePlan(
            goals=goals,
            acceptance_criteria=tuple(criteria),
        )
    return out


def _written_constraints(
    raw: Any, chosen: Iterable[str]
) -> dict[str, list[str]] | None:
    """把父 agent 写的 `constraints` 理成 {角色: [正文]}。没写返回 None。

    键只认这一场实际派发的角色或 global。写错当场报出来：这些限制会进每个子
    agent 的任务书，收下一个没人执行或拼错的键，等于让模型以为它已经交代过了。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ToolError(
            'constraints 要写成一张表，例如 {"pocket": ["只用实验结构"], '
            '"global": ["只用已上市药物"]}'
        )
    from vsagent.core.agent.execution.projection import GLOBAL_CONSTRAINT
    from vsagent.core.agent.roles import catalog

    names = list(chosen)
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            raise ToolError("constraints 的键要是角色名或 global，不能是空串")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) for item in value
        ):
            raise ToolError(f"constraints 里 {name} 的值要写成字符串数组")
        items = [item.strip() for item in value if item.strip()]
        if not items:
            continue
        if name.casefold() in (GLOBAL_CONSTRAINT, "全局"):
            kind = GLOBAL_CONSTRAINT
        elif name in names:
            kind = name
        elif name in catalog.AGENTS:
            raise ToolError(
                f"这次不派 {name}，写给它也没有人去执行。"
                f"这次派的是：{'、'.join(names)}"
            )
        else:
            raise ToolError(f"没有这个角色：{name}。可用：{'、'.join(catalog.AGENTS)}")
        out[kind] = out.get(kind, []) + items
    return out


def _advance_state(session: Session, target: str) -> str:
    """推进战役状态，返回跳不过去的理由（能跳时为空串）。

    跳不过去不改状态：这是记账，不是闸门。真正需要拦住的情形（重复派发、
    计划作废）由调用方单独判，不靠状态机兜。
    """
    state, reason = campaign_state.soft_advance(session.campaign_state, target)
    session.campaign_state = state
    return reason


def _campaign_preview(plan: Any, tasks: list[Any], warnings: Iterable[Any]) -> str:
    """派发前的预演：派谁、谁等谁。不查状态，只说计划本身。

    导入放在函数里：`coordinator` → `scheduler` → `dispatcher` → `host` 这条链
    会绕回本模块，放在文件顶部就成了循环导入。
    """
    from vsagent.core.agent.roles.profiles import role_label

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
        outputs = "、".join(task.required_outputs) or "无"
        lines.append(
            f"- {role_label(task.role)}（{task.role}）：{goal}{tail}"
            f"；交付 {outputs}；验收 {len(task.acceptance_criteria)} 条"
        )
    if warnings:
        lines.extend(f"注意：{item}" for item in warnings)
    dispatched = {task.role for task in tasks}
    pending = [role for role in plan.selected_agents if role not in dispatched]
    if pending:
        # 按子集派发时，计划里剩下的角色不会自己跑；不说的话，主 agent 会以为
        # 整场已经跑完（2026-11-13 的 HA2 会话就是这样漏掉准备与对接的）。
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
# 的上下文；每个子 agent 的全文另写到 `runs/reports/` 下，索引里的路径
# 可以直接用 `read` 取。没列出的那些也有文件，不缺入口。
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
    """子 agent 最后交回来的那一段：正文，或非正常结束时的收尾总结。

    预览只留 800 字。全文的入口在报告开头那条索引里，这里不再重复写一遍
    路径：每行一句路径会把 1500 token 的预算吃掉一大块。
    """
    from vsagent.core.agent.execution.reports import answer_text

    text = answer_text(session)
    if len(text) > REPORT_LIMIT:
        text = text[:REPORT_LIMIT] + "…（已截断，全文见上面的索引）"
    return text


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


def _report_lines(child: Session, built: Any = None) -> list[str]:
    """结构化派发的子 agent：补一段按角色判过的报告摘要。

    自由文本派发的子会话没有角色，这里什么都不加——它们的结论已经在上一行。
    不合格时不重写一份"看起来能做"的摘要，而是把缺什么直接写出来，
    父会话据此决定要不要让它补交。
    """
    if built is None:
        built = _built_report(child)
    if built is None:
        if not child.agent_role:
            return []
        return [f"  报告：角色 {child.agent_role} 不在说明书里，无法判"]
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


def _report_markdown(
    parent: Session, child: Session, built: Any = None
) -> str:
    """写进磁盘的那一份：交付正文与收尾总结全文，不受上下文预算限制。

    正文与总结分开写：非正常结束时模型最后的交代在总结里，只取其中一段
    会让磁盘上这份「全文」也缺一块。
    """
    from vsagent.core.agent.execution.reports import answer_blocks

    text, summary = answer_blocks(child)
    lines = [
        f"# {child.title or child.id}",
        "",
        f"会话：{child.id}",
        f"父会话：{parent.id}",
        f"角色：{child.agent_role or '自由文本'}",
        f"状态：{_status_text(child.status)}",
    ]
    if child.status_note:
        lines.append(f"说明：{child.status_note}")
    if text:
        lines.extend(["", "## 结论", text])
    if summary and summary != text:
        lines.extend(["", "## 收尾总结", summary])
    if not text and not summary:
        lines.extend(["", "## 结论", "（还没有回复）"])
    if child.agent_role and child.status in ("done", "failed", "cancelled"):
        lines.append("")
        lines.extend(line.strip() for line in _report_lines(child, built))
    files = _output_files(child)
    if files:
        lines.extend(["", "## 产出文件"])
        lines.extend(f"- {path}" for path in files)
    return "\n".join(lines) + "\n"


def _report_index(
    parent: Session, children: Iterable[Session], paths: dict[str, str]
) -> str:
    """一场战役的索引：一行一个子 agent，指向它的全文。"""
    lines = [f"# {parent.title or parent.id} 的子 agent 报告", ""]
    for child in children:
        lines.append(
            f"- {child.title or child.id}（{child.id}，"
            f"{child.agent_role or '自由文本'}，{_status_text(child.status)}）"
            f"→ {paths.get(child.id, '（没有写成）')}"
        )
    return "\n".join(lines) + "\n"


def _cap_report(text: str, limit: int = REPORT_TOKENS, index: str = "") -> str:
    """把整份报告压到限额以内：**按行**丢，末尾写明去哪看全文。

    不按字符截：报告是「一行一个子 agent」的结构，切在半行上会留下半句结论，
    读的人分不清那一行说的是谁。全文在磁盘上，尾注给出 `read` 能用的路径。
    """
    if estimate_tokens(text) <= limit:
        return text
    if index:
        note = f"\n（报告已截断；全部子 agent 的全文见 {index}，用 read 读）"
    else:
        note = "\n（报告已截断；完整内容在各子 agent 的报告文件里）"
    kept: list[str] = []
    used = estimate_tokens(note)
    for line in text.splitlines():
        cost = estimate_tokens(line) + 1
        if used + cost > limit:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) + note


def _report_evidence(child: Session, built: Any = None) -> list[dict]:
    """子会话报告里的证据对象，供父会话建立一场的证据池。

    与 `_report_lines` 用的是同一份报告；证据对象不塞进给模型看的那段文字，
    只放进工具结果的 `meta` 里，由循环层交给答案状态。
    """
    if built is None:
        built = _built_report(child)
    if built is None:
        return []
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
