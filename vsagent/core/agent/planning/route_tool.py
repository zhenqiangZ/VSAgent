"""`route_intent`：主控每轮第一步调用的结构化意图工具。

解析不再是循环之外的一次独立模型调用：主会话在自己的第一步拿到这一份工具，
模型读出用户意图、按字段填好，内核随即校验、编译任务、路由角色，并把结果
写回会话。之后同一轮里主控只按这份计划派发。

三条边界：

- **Schema 就是合同**：字段与 `IntentSpec` 一一对应，多写、写错取值都会被
  参数检查或 `IntentSpec` 挡回来，模型在下一步自己改。
- **不碰任务状态**：真正写 `task_spec`、发过程记录、拦派发的是循环层给的
  `apply` 回调（`loop._apply_route_decision`）；这一层只负责把模型给的 JSON
  变成一次完整决策。
- **失败可重填**：意图本身不合法时返回失败结果、保持运行时有效，让模型在
  同一轮内重填；决策成立（含明确拒绝与需要澄清）之后运行时关闭，不再接受
  第二次调用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from vsagent.core.agent.planning.intent_prompt import INTENT_PROMPT
from vsagent.core.agent.planning.intent_summary import ACTION_WORDS, RELATION_WORDS
from vsagent.core.agent.roles.catalog import AGENTS
from vsagent.core.agent.roles.contracts import (
    CONVERSATION_ACTIONS,
    OBJECT_ORIGINS,
    OBJECT_TYPES,
    TASK_RELATIONS,
    ContractError,
    IntentSpec,
)
from vsagent.core.agent.roles.contracts.task import ACCEPTANCE_KINDS
from vsagent.core.agent.tools import Tool, ToolResult, ToolSpec

TOOL_NAME = "route_intent"

DESCRIPTION = (
    INTENT_PROMPT
    + """

调用约定：
- 每一轮的第一个动作就是调用 route_intent，一次只给这一句的意图。
- 调用之后系统会校验、编译任务并路由角色；结果会写进本轮的 TASK 层。
- 用户还没说清、不回答就无法开始时，把问题写进 clarification_required；
  能靠检索或计算自己得到的缺口，写进 missing_information，不要追问。
- 同一个目标改了对象或范围时用 task_relation=modify；只补要求或加禁止用 continue。
"""
)

_OBJECT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {
            "type": "string",
            "enum": list(OBJECT_TYPES),
            "description": "科研对象类型",
        },
        "label": {"type": "string", "description": "用户说的名字"},
        "ref": {"type": "string", "description": "现场里已有的引用，新对象留空"},
        "origin": {
            "type": "string",
            "enum": list(OBJECT_ORIGINS),
            "description": "existing 已有引用、new 本轮新提、current 当前任务正在用",
        },
    },
    "required": ["type", "label"],
}

_SUBGOAL = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "agent": {"type": "string", "enum": list(AGENTS), "description": "承担这一段工作的角色"},
        "goal": {"type": "string", "description": "可执行的一句话子目标，不抄用户原话"},
        "stage": {"type": "string", "description": "该角色允许的阶段，可省略"},
    },
    "required": ["agent", "goal"],
}

_CRITERION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": list(ACCEPTANCE_KINDS)},
        "target": {"type": "string", "description": "检查什么"},
        "output": {"type": "string", "description": "这条检查对应的交付物"},
    },
    "required": ["kind", "target", "output"],
}

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "goal": {"type": "string", "description": "用户最终想解决什么问题，一句话"},
        "task_relation": {
            "type": "string",
            "enum": list(TASK_RELATIONS),
            "description": "这一句与当前任务的关系；分界是完成的目标本身变没变",
        },
        "conversation_action": {
            "type": "string",
            "enum": list(CONVERSATION_ACTIONS),
            "description": "这一轮的对话行为",
        },
        "objects": {"type": "array", "items": _OBJECT, "description": "用户说到的科研对象"},
        "operations": {
            "type": "array",
            "items": {"type": "string", "enum": list(AGENTS)},
            "description": "要交给哪些子 Agent，取值就是九个角色名",
        },
        "subgoals": {"type": "array", "items": _SUBGOAL, "description": "每个操作角色的子目标"},
        "acceptance_criteria": {
            "type": "array",
            "items": _CRITERION,
            "description": "每个交付物的验收条件",
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户明确提出的要求，照抄原意",
        },
        "exclusions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户明确禁止的事，逐条照抄，不改写",
        },
        "expected_outputs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户希望拿到什么",
        },
        "missing_information": {
            "type": "array",
            "items": {"type": "string"},
            "description": "完成任务还缺什么；发现类任务缺编号不算缺",
        },
        "clarification_required": {
            "type": "array",
            "items": {"type": "string"},
            "description": "不回答就无法开始的问题；能自己查到的不要问",
        },
        "inputs": {"type": "object", "description": "已知输入，可选"},
        "confidence": {
            "type": "number",
            "description": "对这份意图的把握，0 到 1",
        },
        "uncertainties": {
            "type": "array",
            "items": {"type": "string"},
            "description": "推断出来但用户没明说的东西",
        },
    },
    "required": ["goal", "task_relation", "conversation_action"],
}


def _result_text(decision: Any, note: str) -> str:
    """给模型看的一行结论；详细过程记录由 ROUTE 块承担。"""
    intent = getattr(decision, "intent", None)
    relation = RELATION_WORDS.get(getattr(intent, "task_relation", ""), "") if intent else ""
    action = ACTION_WORDS.get(getattr(intent, "conversation_action", ""), "") if intent else ""
    if getattr(decision, "error", ""):
        head = f"这次意图不成立：{decision.error}"
    elif getattr(decision, "clarification", ()):
        head = "先向用户澄清：" + "；".join(decision.clarification)
    elif getattr(decision, "task", None) is not None:
        task = decision.task
        agents = "、".join(decision.plan.selected_agents) if decision.plan else "无"
        head = (
            f"已解析：{relation} / {action}；任务 {task.task_id} rev {task.revision}；"
            f"角色 {agents}"
        )
        if decision.plan is not None and decision.plan.unresolved_inputs:
            head += "\n还不能派发：" + "；".join(decision.plan.unresolved_inputs)
    else:
        head = f"已解析：{relation} / {action}。"
    if note:
        head += f"\n本轮注意：{note}"
    return head


@dataclass
class RouteRuntime:
    """一轮的意图运行时：持有现场、调用适配器、把决策交回循环层。

    `apply` 由循环层给：它负责写 `task_spec`、发 `route.plan`、写过程记录并
    返回这一轮给模型的短提示。运行时自己不改会话状态。
    """

    adapter: Any
    session: Any
    message: Any
    text: str
    current: Any
    ledger: Any
    workspace: Any
    apply: Callable[[Any], str]
    active: bool = True

    async def wire(self, payload: dict[str, Any]) -> str:
        raw = dict(payload)
        if not str(raw.get("intent_id") or "").strip():
            body = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha1(f"{self.text}\n{body}".encode("utf-8")).hexdigest()
            raw["intent_id"] = f"intent_{digest[:10]}"
        intent = IntentSpec.from_dict(raw)
        decision = await self.adapter.wire(
            intent,
            text=self.text,
            current=self.current,
            session=self.session,
            ledger=self.ledger,
            workspace=self.workspace,
        )
        self.active = False
        note = self.apply(decision) or ""
        return _result_text(decision, note)


class RouteIntentTool(Tool):
    """主控每轮第一步的结构化意图工具。"""

    route_argument = "route"
    spec = ToolSpec(name=TOOL_NAME, description=DESCRIPTION, parameters=PARAMETERS)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        runtime = arguments.get("route")
        if not isinstance(runtime, RouteRuntime):
            return ToolResult(
                output="内核装配问题：route_intent 需要本轮的意图运行时，但这次调用没有拿到。",
                failed=True,
            )
        if not runtime.active:
            return ToolResult(
                output="本轮意图已经解析完成，不要重复调用；按计划继续或直接回答用户。",
                failed=True,
            )
        payload = {key: value for key, value in arguments.items() if key != "route"}
        try:
            output = await runtime.wire(payload)
        except (ContractError, ValueError) as error:
            return ToolResult(
                output=f"意图字段不合法：{error}\n按工具定义补全后，再调用一次 route_intent。",
                failed=True,
            )
        return ToolResult(output=output, meta={"intent": 1})
