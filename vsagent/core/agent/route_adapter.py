"""Route Adapter：把意图、任务与能力计划接到循环层上。

三种模式，靠 `VSA_ROUTE` 切换：

- `off`　　：一概不做，与加入路由之前完全一致；
- `shadow` ：照常解析与路由，**只记录，不改工具清单**。用来量"计划与实际用族"的
             一致率；解析失败也只是少一条记录，不影响这一轮；
- `enforce`：计划照常成立并交给派发；主 agent 自己一族都不装。它现在与 `shadow`
             对工具清单的作用一致（改工具清单的那一步已经删掉，见 P-79），差别只在
             trace 里记下的模式名，保留它是为了显式选择"严格路径"。

三条边界：

- **失败不改行为**：解析两次仍失败、路由抛错，都只是记一条 `route.error`，
  这一轮照旧走原来的路径。路由是新加的一层，不能成为新的故障点。
- **子 agent 不路由**：它的任务书由父会话给，自己没有"用户意图"可解析。
- **shadow 也写任务规格**：否则下一轮没有当前任务可继承，"继续""改目标"永远
  判不出来，样本就白采了。写的是会话状态，不动工具清单。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .capability_router import CapabilityRouter, RouterError
from .contracts import CapabilityPlan, IntentSpec, TaskSpec
from .intent_parser import IntentParser
from .task_compiler import CompileError, TaskCompiler
from .task_context import TaskContextResolver, context_line

MODES: tuple[str, ...] = ("off", "shadow", "enforce")
ENV = "VSA_ROUTE"
DEFAULT_MODE = "shadow"

TURN_WORDS: dict[str, str] = {
    "new_task": "新任务",
    "continue_task": "接着做",
    "modify_task": "改目标",
    "ask_about_result": "问结果",
    "resume_historical_task": "恢复旧任务",
    "general_chat": "闲聊",
}

SUBGOAL_LIMIT = 6
"""正文里最多列几条子目标。任务书可能有十几条，这一块要能被一眼扫完。"""

WARNING_LIMIT = 4
FAMILY_LIMIT = 8
TEXT_LIMIT = 2_000
"""整段的字符上限。它是过程记录，每一轮都存一份，不能比答复本身还长。"""


def _clip(text: str, limit: int) -> str:
    """压成一行再截断：折叠块的正文按行排，换行会把一条要求拆成两行。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"



@dataclass(frozen=True)
class RouteDecision:
    """这一轮路由做了什么。调用方据此决定装不装族、记什么日志。"""

    mode: str
    intent: IntentSpec | None = None
    task: TaskSpec | None = None
    plan: CapabilityPlan | None = None
    families: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str = ""

    @property
    def usable(self) -> bool:
        return not self.error and self.intent is not None


def mode_from_env(default: str = DEFAULT_MODE) -> str:
    """当前模式。认不出的一律回落到 `default`，不因为一个拼写错误停掉路由。"""
    value = os.environ.get(ENV, "").strip().lower()
    return value if value in MODES else default


class RouteAdapter:
    """一次路由 = 一次解析 + 编译 + 路由，全是纯计算，除了那一次模型调用。"""

    def __init__(
        self,
        provider: Any,
        *,
        mode: str = DEFAULT_MODE,
        parser: IntentParser | None = None,
    ) -> None:
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.parser = parser or IntentParser(provider)
        self.compiler = TaskCompiler()
        self.router = CapabilityRouter()
        self.resolver = TaskContextResolver()

    async def prepare(self, text: str, *, current: TaskSpec | None = None) -> RouteDecision:
        if self.mode == "off":
            return RouteDecision(mode=self.mode)
        if not (text or "").strip():
            return RouteDecision(mode=self.mode, error="没有可解析的用户输入")

        try:
            intent = await self.parser.parse(text, context=context_line(current))
        except Exception as error:
            # 解析失败不拦这一轮：旧的执行路径照走，只是少了路由信息。
            # 这里接的是 Exception 而不是 IntentParseError：网络错误、协议错误
            # 同样不该让整轮起不来（CancelledError 继承 BaseException，照常向上抛）。
            message = str(error) or type(error).__name__
            return RouteDecision(mode=self.mode, warnings=(message,), error=message)

        turn = self.resolver.resolve(intent, current)
        task = turn.task
        plan: CapabilityPlan | None = None
        warnings = list(turn.warnings)

        if turn.recompile:
            # modify 的合并交给编译器做一次：这里传**原始**任务，
            # 传 turn.task 会把同一批约束合并两遍。
            try:
                compiled = self.compiler.compile(
                    intent,
                    current=current if intent.turn_mode == "modify_task" else None,
                )
            except CompileError as error:
                return RouteDecision(
                    mode=self.mode, intent=intent, warnings=warnings, error=str(error)
                )
            task = compiled.task
            warnings.extend(compiled.warnings)

        if task is not None:
            try:
                routing = self.router.route(task)
            except RouterError as error:
                return RouteDecision(
                    mode=self.mode, intent=intent, task=task, warnings=tuple(warnings), error=str(error)
                )
            plan = routing.plan
            warnings.extend(routing.warnings)

        families: list[str] = []
        if plan is not None:
            for names in plan.tool_families.values():
                for family in names:
                    if family not in families:
                        families.append(family)
        return RouteDecision(
            mode=self.mode,
            intent=intent,
            task=task,
            plan=plan,
            families=tuple(families),
            warnings=tuple(warnings),
        )


def describe(decision: RouteDecision, *, hint: str = "") -> str:
    """把一次路由写成折叠块要显示的一段文本：第一行是标题，余下是正文。

    写成纯文本而不是再加一套 JSON 形状：这一块只给人读，TUI 与网页各画一遍就够。
    多一层结构就多两处解析，而它回答的问题只有三个——用户要什么、这一步要交什么、
    谁去做，按这三段分开就够读。

    `hint` 是关键词提示那一行（`intent.summary`）。它与路由不是一回事：那个按词面
    猜「该看哪几类来源」，不看模型。两样都写在一块里，读的人才能对照「系统猜这是个
    什么事」与「系统猜该去查哪儿」。

    `role_label` 是延迟导入的：`coordinator` 经 `scheduler → dispatcher → host`
    绕回 `loop`，在模块层导入会在这条链上形成环。
    """
    from .coordinator import role_label

    intent = decision.intent
    if intent is None:
        reason = _clip(decision.error or "解析器没有返回结果", 200)
        blocks = ["意图与任务 · 未解析"]
        if hint:
            blocks.append(f"[来源] {hint}")
        blocks.append(f"这一轮没有生成任务规格：{reason}")
        return "\n\n".join(blocks)

    task, plan = decision.task, decision.plan
    turn = TURN_WORDS.get(intent.turn_mode, intent.turn_mode)
    facts = [turn]
    if plan is not None:
        facts.append(f"{len(plan.selected_agents)} 个角色")
    facts.append(decision.mode)
    blocks = [f"意图与任务 · {' · '.join(facts)}"]

    rows = [f"  类型：{'、'.join(intent.intent_types)}"]
    rows.append(f"  目标：{_clip(intent.primary_goal, 120)}")
    for label, values in (
        ("要求", intent.explicit_constraints),
        ("禁止", intent.exclusions),
        ("缺", intent.missing_information),
        ("要问", intent.clarification_required),
        ("交付", intent.expected_deliverables),
    ):
        if values:
            rows.append(f"  {label}：{_clip('；'.join(values), 160)}")
    blocks.append("[意图]\n" + "\n".join(rows))

    if hint:
        blocks.append(f"[来源] {hint}")

    if task is not None:
        rows = [f"  {task.task_id} · rev {task.revision} · {task.status}"]
        if task.subgoals:
            rows.append("  子目标：")
            for order, row in enumerate(task.subgoals[:SUBGOAL_LIMIT], 1):
                rows.append(f"    {order}. {_clip(str(row.get('goal') or ''), 100)}")
        outputs = [str(row.get("name") or "") for row in task.required_outputs]
        if outputs:
            rows.append(f"  交付物：{'、'.join(outputs)}")
        approvals = (
            list(plan.approval_points)
            if plan is not None and plan.approval_points
            else [str(row.get("name") or "") for row in task.approval_points]
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
        rows: list[str] = ["  主 agent：只编排，不装领域族"]
        for role in plan.selected_agents:
            row = f"  {role_label(role)}（{role}）"
            if waits.get(role):
                upstream = "、".join(role_label(dep) for dep in waits[role])
                row += f"  ← 等 {upstream}"
            rows.append(row)
            # 族按角色分行写。原先压成一行并集，读起来像「这个会话装了这十族」——
            # 而它其实是两个角色各自要用的，主 agent 一族都不装（P-79）。
            names = list(plan.tool_families.get(role) or [])
            if names:
                tail = "…" if len(names) > FAMILY_LIMIT else ""
                rows.append(f"    族：{'、'.join(names[:FAMILY_LIMIT])}{tail}")
        rows.append("  应用：计划按角色派发，主 agent 不装族")
        if plan.unresolved_inputs:
            # 与告警不同：这句是「这一场不能派」，不是提醒。`dispatch_campaign`
            # 按同一份清单拒绝执行（`missing_prerequisites`）。
            rows.append(f"  不可派发：{_clip('；'.join(plan.unresolved_inputs), 200)}")
        blocks.append("[计划]\n" + "\n".join(rows))

    if decision.warnings:
        # 缺前置那几条已经写成「不可派发」，不再在提示里重复一遍。
        rest = [text for text in decision.warnings if text not in (plan.unresolved_inputs if plan else ())]
        if rest:
            blocks.append(
                "[提示]\n" + "\n".join(f"  · {_clip(text, 160)}" for text in rest[:WARNING_LIMIT])
            )
    if decision.error:
        blocks.append(f"[错误] {_clip(decision.error, 200)}")

    text = "\n\n".join(blocks)
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"

