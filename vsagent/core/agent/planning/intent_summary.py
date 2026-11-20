"""把意图解析的结果写成模型能看的一页摘要。

意图解析的产物原本只进两处：界面的折叠块（`PartType.ROUTE`）与派发工具。
模型在自己的上下文里看不到它，只能看到一行「本轮是新任务」。这一层把解析结果
压到 500 个文字单位以内，放进 TASK 层，让主控在派发前能核对：系统理解的目标、
对象、角色、约束是不是用户的意思。

字段按重要程度排。超出上限时整行丢弃，不截半句：先丢不确定、确认、验收、
对象，再把子目标减到三条。目标、操作、约束、禁止永远保留。
"""

from __future__ import annotations

from vsagent.core.agent.roles.contracts import AgentPlan, IntentSpec, TaskSpec
from vsagent.core.agent.roles.prompt import estimate_tokens

BLOCK_LIMIT = 500
"""整块的上限，按 `estimate_tokens` 算。三角色、三交付物的一轮约 260。"""

GOAL_LIMIT = 120
OBJECTS_LIMIT = 120
SUBGOAL_COUNT = 6
SUBGOAL_LIMIT = 70
CONSTRAINT_COUNT = 3
CONSTRAINT_LIMIT = 120
EXCLUSION_COUNT = 3
EXCLUSION_LIMIT = 80
OUTPUT_LIMIT = 80
ACCEPTANCE_COUNT = 3
ACCEPTANCE_LIMIT = 120
APPROVAL_COUNT = 4
NOTE_LIMIT = 100

RELATION_WORDS: dict[str, str] = {
    "new": "新建",
    "continue": "延续",
    "modify": "修改",
    "compare": "比较",
    "resume": "恢复",
    "none": "无任务",
}

ACTION_WORDS: dict[str, str] = {
    "execute": "执行",
    "status_query": "查进度",
    "ask_result": "问结果",
    "clarify": "回答澄清",
    "cancel": "取消",
    "chat": "闲聊",
}

DROP_ORDER: tuple[str, ...] = (
    "missing",
    "uncertain",
    "approval",
    "acceptance",
    "objects",
    "outputs",
    "exclusions",
)
"""超长时按这个顺序整行丢弃。越靠前越先丢，越不重要的越先走。"""


def _clip(text: str, limit: int) -> str:
    """压成一行再截断。换行会把一条要求分成两行，读起来像两件事。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _mode_word(mode: str) -> str:
    return "本轮任务规格" if mode == "enforce" else "本轮理解，仅参考"


def render_intent(
    intent: IntentSpec | None,
    task: TaskSpec | None,
    plan: AgentPlan | None,
    *,
    mode: str = "shadow",
    limit: int = BLOCK_LIMIT,
) -> str:
    """渲染一块给模型看的意图摘要。没有意图时返回空串。"""
    if intent is None:
        return ""

    relation = RELATION_WORDS.get(intent.task_relation, intent.task_relation)
    action = ACTION_WORDS.get(intent.conversation_action, intent.conversation_action)
    head = f"（{_mode_word(mode)}：{relation} / {action}"
    if task is not None and task.task_id:
        head += f" · {task.task_id}"
    head += "）"

    order: list[str] = ["head", "goal", "objects", "operations", "subgoals"]
    fields: dict[str, str] = {
        "head": head,
        "goal": f"目标：{_clip(intent.goal, GOAL_LIMIT)}",
    }

    if intent.objects:
        rows = [
            f"{item.label}（{item.type}{'，' + item.ref if item.ref else ''}）"
            for item in intent.objects
        ]
        fields["objects"] = "对象：" + _clip("；".join(rows), OBJECTS_LIMIT)

    roles = list(plan.selected_agents) if plan is not None else list(intent.operations)
    if roles:
        fields["operations"] = "操作：" + "、".join(roles)

    if task is not None and task.subgoals:
        rows = [
            f"- {row.get('agent') or '?'}：{_clip(row.get('goal') or '', SUBGOAL_LIMIT)}"
            for row in task.subgoals[:SUBGOAL_COUNT]
        ]
        fields["subgoals"] = "子目标：\n" + "\n".join(rows)

    if task is not None and task.constraints:
        order.append("constraints")
        fields["constraints"] = "约束：" + _clip(
            "；".join(task.constraints[:CONSTRAINT_COUNT]), CONSTRAINT_LIMIT
        )
    if task is not None and task.exclusions:
        order.append("exclusions")
        fields["exclusions"] = "禁止：" + _clip(
            "；".join(task.exclusions[:EXCLUSION_COUNT]), EXCLUSION_LIMIT
        )
    if task is not None and task.required_outputs:
        order.append("outputs")
        fields["outputs"] = "交付：" + _clip(
            "、".join(task.required_outputs), OUTPUT_LIMIT
        )
    if task is not None and task.acceptance_criteria:
        order.append("acceptance")
        rows = [
            f"{row.get('kind')}（{row.get('target')}）"
            for row in task.acceptance_criteria[:ACCEPTANCE_COUNT]
        ]
        fields["acceptance"] = "验收：" + _clip("；".join(rows), ACCEPTANCE_LIMIT)
    if task is not None and task.approval_points:
        order.append("approval")
        fields["approval"] = "确认：" + "、".join(
            task.approval_points[:APPROVAL_COUNT]
        )

    uncertain = list(intent.uncertainties or ())
    if uncertain:
        order.append("uncertain")
        fields["uncertain"] = "不确定：" + _clip("；".join(uncertain[:2]), NOTE_LIMIT)

    missing = list(intent.missing_information or ())
    if missing:
        order.append("missing")
        fields["missing"] = "缺的输入：" + _clip("；".join(missing[:2]), NOTE_LIMIT)

    return _cap(fields, order, limit)


def _cap(fields: dict[str, str], order: list[str], limit: int) -> str:
    """按丢弃顺序把整块压到上限以内。"""
    for key in DROP_ORDER:
        if estimate_tokens(_join(fields, order)) <= limit:
            return _join(fields, order)
        fields.pop(key, None)

    subgoals = fields.get("subgoals")
    if subgoals:
        rows = subgoals.splitlines()
        fields["subgoals"] = "\n".join([rows[0], *rows[1:4]])
        if estimate_tokens(_join(fields, order)) <= limit:
            return _join(fields, order)
        fields.pop("subgoals", None)
    return _join(fields, order)


def _join(fields: dict[str, str], order: list[str]) -> str:
    return "\n".join(fields[key] for key in order if fields.get(key))
