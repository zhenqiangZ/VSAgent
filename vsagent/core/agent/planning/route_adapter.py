"""Route Adapter：把意图、任务与角色计划接到循环层上。

三种模式，由 `.vsagent/route.toml` 的 `[route] mode` 控制（环境变量
`VSA_ROUTE` 只做临时覆盖）：

- `off`　　：一概不做，与加入路由之前完全一致；
- `shadow` ：照常解析、验证、编译与路由，写出计划与过程记录，不改变任务状态；
- `enforce`：验证通过才编译与派发；需要澄清时先问；明确拒绝时不执行。

一条完整路径：

    上下文解析 → 意图解析 → 意图验证 → 任务关系 → 任务编译 → 角色路由

四条边界：

- **失败不改行为**：解析失败、路由抛错，都只是记一条 `route.error`。
- **子 agent 不路由**：它的任务书由父会话给，自己没有“用户意图”可解析。
- **计划写进会话状态**：解析成功就落成 `task_spec`，下一轮靠它判延续、修改与比较。
- **动作先于任务**：查进度、问结果、取消、闲聊不编译任务。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from vsagent.core.agent.planning.agent_router import AgentRouter, RouterError
from vsagent.core.agent.planning.context_resolver import ContextBundle, ContextResolver
from vsagent.core.agent.planning.decision import RouteDecision
from vsagent.core.agent.planning.intent_parser import IntentParser
from vsagent.core.agent.planning.intent_summary import ACTION_WORDS, RELATION_WORDS
from vsagent.core.agent.planning.intent_validator import IntentValidator, Verdict
from vsagent.core.agent.planning.notes import Note
from vsagent.core.agent.planning.task_compiler import CompileError, TaskCompiler
from vsagent.core.agent.planning.task_context import TaskContextResolver
from vsagent.core.agent.roles.contracts import AgentPlan, IntentSpec, TaskSpec
from vsagent.core.route_config import DEFAULT_MODE, MODES

SUBGOAL_LIMIT = 6
WARNING_LIMIT = 4
TEXT_LIMIT = 2_000


def _merge_rows(
    first: tuple[dict[str, Any], ...],
    second: tuple[dict[str, Any], ...],
    keys: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    """按几个字段去重，保留第一次出现的行。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in (*first, *second):
        key = tuple(str(row.get(name) or "") for name in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return tuple(out)


def _clip(text: str, limit: int) -> str:
    """压成一行再截断：折叠块的正文按行排，换行会把一条要求拆成两行。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class RouteAdapter:
    """一次路由 = 一次上下文解析 + 一次模型调用 + 纯规则计算。"""

    def __init__(
        self,
        provider: Any,
        *,
        mode: str = DEFAULT_MODE,
        parser: IntentParser | None = None,
    ) -> None:
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.parser = parser or IntentParser(provider)
        self.context = ContextResolver()
        self.validator = IntentValidator()
        self.compiler = TaskCompiler()
        self.router = AgentRouter()
        self.resolver = TaskContextResolver()

    async def prepare(
        self,
        text: str,
        *,
        current: TaskSpec | None = None,
        session: Any = None,
        ledger: Any = None,
        workspace: Any = None,
    ) -> RouteDecision:
        if self.mode == "off":
            return RouteDecision(mode=self.mode)
        if not (text or "").strip():
            return RouteDecision(mode=self.mode, error="没有可解析的用户输入")

        bundle: ContextBundle = self.context.resolve(
            session=session,
            text=text,
            current=current,
            ledger=ledger,
            workspace=workspace,
        )
        try:
            intent = await self.parser.parse(text, context=bundle.text)
        except Exception as error:
            message = str(error) or type(error).__name__
            return RouteDecision(
                mode=self.mode,
                warnings=(Note("parse_failed", "text", message),),
                error=message,
            )

        intent = self._merge_pending(intent, session)
        verdict = self.validator.validate(intent, bundle, current=current)
        # Reason 里本来就有机器码，转成提示时保留下来；以前只抄了 message。
        warnings: list[Note] = [
            Note(item.code, item.field, item.message) for item in verdict.reasons
        ]
        if verdict.decision == "reject":
            reason = "；".join(item.message for item in verdict.reasons) or "意图不成立"
            return RouteDecision(
                mode=self.mode,
                relation=intent.task_relation,
                action=intent.conversation_action,
                intent=intent,
                verdict=verdict,
                warnings=tuple(warnings),
                error=reason,
            )
        if verdict.decision == "clarify":
            return RouteDecision(
                mode=self.mode,
                relation=intent.task_relation,
                action=intent.conversation_action,
                intent=intent,
                verdict=verdict,
                clarification=verdict.clarification,
                warnings=tuple(warnings),
            )

        decision = self.resolver.resolve(intent, current)
        warnings.extend(decision.warnings)
        decision = replace(
            decision, mode=self.mode, verdict=verdict, warnings=tuple(warnings)
        )

        if decision.action != "execute" or decision.relation == "none":
            return decision

        task = decision.task
        if decision.recompile:
            try:
                compiled = self.compiler.compile(
                    decision.intent,
                    current=current if decision.relation != "new" else None,
                )
            except CompileError as error:
                return replace(
                    decision, warnings=tuple(warnings), error=str(error)
                )
            task = compiled.task
            warnings.extend(compiled.warnings)

        plan: AgentPlan | None = None
        if task is not None:
            try:
                routing = self.router.route(task)
            except RouterError as error:
                return replace(
                    decision, task=task, warnings=tuple(warnings), error=str(error)
                )
            plan = routing.plan
            warnings.extend(routing.warnings)

        return replace(decision, task=task, plan=plan, warnings=tuple(warnings))

    @staticmethod
    def _merge_pending(intent: IntentSpec, session: Any) -> IntentSpec:
        """用户在回答上一轮的澄清：把上一轮意图补回来，再按执行处理。

        解析器只看到这一句答案，容易丢掉上一轮的目标、操作与对象。这里以待澄清
        意图为底，合并新的操作、对象、输入与约束。合并失败就保留原意图，让验证器
        继续走澄清，不抛错。
        """
        if intent.conversation_action != "clarify" or session is None:
            return intent
        pending = getattr(session, "pending_intent", None)
        if not isinstance(pending, dict):
            return intent
        raw = pending.get("intent")
        if not isinstance(raw, dict):
            return intent
        try:
            base = IntentSpec.from_dict(raw)
            operations = tuple(dict.fromkeys([*base.operations, *intent.operations]))
            objects = tuple(dict.fromkeys([*base.objects, *intent.objects]))
            relation = intent.task_relation
            if relation in ("continue", "none") and base.task_relation != "none":
                relation = base.task_relation
            goal = intent.goal or base.goal
            if relation == "compare" and base.goal:
                goal = base.goal
            return replace(
                intent,
                task_relation=relation,
                conversation_action="execute",
                operations=operations,
                subgoals=_merge_rows(
                    base.subgoals, intent.subgoals, ("agent", "goal")
                ),
                acceptance_criteria=_merge_rows(
                    base.acceptance_criteria,
                    intent.acceptance_criteria,
                    ("kind", "target"),
                ),
                objects=objects,
                goal=goal,
                inputs={**base.inputs, **intent.inputs},
                constraints=tuple(dict.fromkeys([*base.constraints, *intent.constraints])),
                exclusions=tuple(dict.fromkeys([*base.exclusions, *intent.exclusions])),
            )
        except Exception:  # noqa: BLE001 - 合并失败时保留原意图，继续走澄清
            return intent


def describe(decision: RouteDecision, *, hint: str = "") -> str:
    """把一次路由写成折叠块要显示的一段文本。

    `hint` 是关键词提示那一行（`intent.summary`）。它与路由不是一回事：那个按词面
    猜“该看哪几类来源”，不看模型。
    """
    from vsagent.core.agent.roles.profiles import role_label

    intent = decision.intent
    if intent is None:
        reason = _clip(decision.error or "解析器没有返回结果", 200)
        blocks = ["意图与任务 · 未解析"]
        if hint:
            blocks.append(f"[来源] {hint}")
        blocks.append(f"这一轮没有生成任务规格：{reason}")
        return "\n\n".join(blocks)

    relation = RELATION_WORDS.get(intent.task_relation, intent.task_relation)
    action = ACTION_WORDS.get(intent.conversation_action, intent.conversation_action)
    facts = [relation, action]
    if decision.plan is not None:
        facts.append(f"{len(decision.plan.selected_agents)} 个角色")
    facts.append(decision.mode)
    blocks = [f"意图与任务 · {' · '.join(facts)}"]

    rows = [f"  目标：{_clip(intent.goal, 120)}"]
    if intent.objects:
        rows.append(
            "  对象："
            + _clip(
                "、".join(
                    f"{item.label}（{item.type}{'，' + item.ref if item.ref else ''}）"
                    for item in intent.objects
                ),
                160,
            )
        )
    if intent.operations:
        rows.append(f"  操作：{'、'.join(intent.operations)}")
    for label, values in (
        ("要求", intent.constraints),
        ("禁止", intent.exclusions),
        ("缺", intent.missing_information),
        ("要问", intent.clarification_required),
        ("交付", intent.expected_outputs),
    ):
        if values:
            rows.append(f"  {label}：{_clip('；'.join(values), 160)}")
    blocks.append("[意图]\n" + "\n".join(rows))

    if hint:
        blocks.append(f"[来源] {hint}")

    task, plan = decision.task, decision.plan
    if task is not None:
        rows = [f"  {task.task_id} · rev {task.revision} · {task.status}"]
        if task.subgoals:
            rows.append("  子目标：")
            for order, row in enumerate(task.subgoals[:SUBGOAL_LIMIT], 1):
                rows.append(f"    {order}. {_clip(str(row.get('goal') or ''), 100)}")
        outputs = list(task.required_outputs)
        if outputs:
            rows.append(f"  交付物：{'、'.join(outputs)}")
        approvals = (
            list(plan.approval_points)
            if plan is not None and plan.approval_points
            else list(task.approval_points)
        )
        if approvals:
            rows.append(f"  确认点：{'、'.join(approvals)}")
        kinds = sorted({str(row.get("kind") or "") for row in task.acceptance_criteria})
        if kinds:
            rows.append(f"  验收：{len(task.acceptance_criteria)} 条（{'、'.join(kinds)}）")
        blocks.append("[任务]\n" + "\n".join(rows))

    if plan is not None:
        waits: dict[str, list[str]] = {}
        for edge in plan.dependencies:
            waits.setdefault(str(edge.get("to") or ""), []).append(str(edge.get("from") or ""))
        rows: list[str] = ["  主 agent：只编排，不直接调用领域工具"]
        for role in plan.selected_agents:
            row = f"  {role_label(role)}（{role}）"
            if waits.get(role):
                upstream = "、".join(role_label(dep) for dep in waits[role])
                row += f"  ← 等 {upstream}"
            rows.append(row)
            from vsagent.core.agent.roles.agents import role_tools

            count = len(role_tools(role))
            if count:
                rows.append(f"    工具：{count} 个（来自 agents.toml）")
        rows.append("  应用：计划按角色派发，主 agent 不直接执行")
        if plan.unresolved_inputs:
            rows.append(f"  不可派发：{_clip('；'.join(plan.unresolved_inputs), 200)}")
        blocks.append("[计划]\n" + "\n".join(rows))

    if decision.clarification:
        blocks.append(
            "[先问]\n" + "\n".join(f"  · {_clip(item, 160)}" for item in decision.clarification)
        )
    if decision.warnings:
        rest = [
            text
            for text in decision.warnings
            if text not in (plan.unresolved_inputs if plan else ())
        ]
        if rest:
            blocks.append(
                "[提示]\n" + "\n".join(f"  · {_clip(text, 160)}" for text in rest[:WARNING_LIMIT])
            )
    if decision.error:
        blocks.append(f"[错误] {_clip(decision.error, 200)}")

    text = "\n\n".join(blocks)
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"
