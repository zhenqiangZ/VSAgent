"""战役状态机：一件事走到哪一步，以及哪一步能跳到哪一步。

与 `stages.py` 分工：那里说的是**计算流程**走到哪一步（结构发现、口袋分析…），
这里说的是**这件事**走到哪一步（理解、规格、规划、派发、研究、汇总…）。
两者不是一一对应——一场战役在「研究」这一格里会依次经过好几个计算阶段。

三条取舍：

- **状态是"当前是什么"，不是"发生了一次变化"。** 重复写同一个状态不算错，
  直接返回；只有真的跳不过去时才抛。否则每轮都写一次的地方要各自判重。
- **终态之后开新一轮，不再校验。** 交付、取消、失败是这一轮的结束，不是这个
  会话的结束；下一轮从「研究」重新开始是正常的。终态之间互跳仍然拒绝——
  「已交付」变成「失败」没有意义。
- **卡住可以回到任何非终态。** 阻塞说的是这一步做不下去，不是这件事结束；
  解除之后回到哪一步由调用方决定，状态机不记历史。

前四格（理解、澄清、规格、规划）由路由层在编译任务规格时推进；派发、研究、
汇总、取消、失败由 host 与派发工具推进；复核、交付、重规划留给主 Agent 显式
推进——它们是人的判断，不该由代码替它宣布。
"""

from __future__ import annotations

from dataclasses import dataclass

UNDERSTANDING = "understanding"
CLARIFICATION = "clarification"
SPECIFICATION = "specification"
PLANNING = "planning"
DELEGATING = "delegating"
RESEARCH = "research"
INTEGRATION = "integration"
CRITICAL_REVIEW = "critical_review"
DELIVERY = "delivery"
REPLANNING = "replanning"
BLOCKED = "blocked"
CANCELLED = "cancelled"
FAILED = "failed"

STATES: tuple[str, ...] = (
    UNDERSTANDING,
    CLARIFICATION,
    SPECIFICATION,
    PLANNING,
    DELEGATING,
    RESEARCH,
    INTEGRATION,
    CRITICAL_REVIEW,
    DELIVERY,
    REPLANNING,
    BLOCKED,
    CANCELLED,
    FAILED,
)

STATE_LABELS: dict[str, str] = {
    UNDERSTANDING: "理解需求",
    CLARIFICATION: "澄清",
    SPECIFICATION: "定规格",
    PLANNING: "排计划",
    DELEGATING: "派发",
    RESEARCH: "研究进行中",
    INTEGRATION: "汇总结果",
    CRITICAL_REVIEW: "复核",
    DELIVERY: "已交付",
    REPLANNING: "重新规划",
    BLOCKED: "卡住",
    CANCELLED: "已取消",
    FAILED: "失败",
}

TERMINAL: frozenset[str] = frozenset({DELIVERY, CANCELLED, FAILED})

# 主链路。只写"往前走"的边，往后的横切边（卡住/取消/失败）由下面统一补。
_FORWARD: dict[str, tuple[str, ...]] = {
    UNDERSTANDING: (CLARIFICATION, SPECIFICATION),
    CLARIFICATION: (SPECIFICATION,),
    SPECIFICATION: (PLANNING,),
    PLANNING: (DELEGATING,),
    DELEGATING: (RESEARCH,),
    RESEARCH: (INTEGRATION,),
    INTEGRATION: (CRITICAL_REVIEW,),
    CRITICAL_REVIEW: (DELIVERY, REPLANNING),
    REPLANNING: (DELEGATING,),
}

_RESUMABLE: frozenset[str] = frozenset(_FORWARD)

ALLOWED: dict[str, frozenset[str]] = {
    state: frozenset(targets) | frozenset({BLOCKED, CANCELLED, FAILED})
    for state, targets in _FORWARD.items()
}
# 汇总之后可以直接补一轮研究：用户看了结果说「再查一个」，不必先走完交付。
# 这条边不进主链路——它不是一个必经步骤，而是一个允许的回头。
ALLOWED[INTEGRATION] = ALLOWED[INTEGRATION] | frozenset({RESEARCH})
ALLOWED[BLOCKED] = _RESUMABLE | frozenset({CANCELLED, FAILED})
ALLOWED[DELIVERY] = frozenset()
ALLOWED[CANCELLED] = frozenset()
ALLOWED[FAILED] = frozenset()


class CampaignStateError(ValueError):
    """跳不过去。消息里带两头的名字与能去的几格，调用方不必自己拼。"""


def label(state: str) -> str:
    """中文名。认不出的原样返回：一个没登记的状态不该让展示整段失败。"""
    return STATE_LABELS.get(state, state)


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def why_not(current: str, target: str) -> str:
    """不能跳的理由；能跳时返回空串。`can` 与 `advance` 共用这一份判断。"""
    if target not in STATES:
        return f"不认识的状态「{target}」"
    if not current:
        if target in TERMINAL:
            return f"还没开始就到「{label(target)}」：第一格必须是进行中的状态"
        return ""
    if current not in STATES:
        return f"当前状态「{current}」没有登记，先把它改回一个已知状态"
    if current == target:
        return ""
    if current in TERMINAL:
        if target in TERMINAL:
            return f"「{label(current)}」与「{label(target)}」都是终态，不能互跳"
        # 终态之后是新的一轮：前面那些步骤上一轮已经走过。
        return ""
    if target not in ALLOWED[current]:
        reachable = "、".join(label(item) for item in sorted(ALLOWED[current]))
        return f"从「{label(current)}」不能直接到「{label(target)}」；这一步只能去：{reachable}"
    return ""


def can(current: str, target: str) -> bool:
    return not why_not(current, target)


def advance(current: str, target: str) -> str:
    """推进到目标状态并返回它。跳不过去时抛 `CampaignStateError`。"""
    reason = why_not(current, target)
    if reason:
        raise CampaignStateError(reason)
    return target


def soft_advance(current: str, target: str) -> tuple[str, str]:
    """推进；跳不过去时原样返回当前状态与理由。

    给「记账不该拦住流程」的调用方：状态机是记录，不是闸门。跳不过去说明
    调用点写错了，理由由调用方记进事件或返回给模型。
    """
    reason = why_not(current, target)
    if reason:
        return current, reason
    return target, ""


@dataclass(frozen=True)
class PlanStamp:
    """一份计划是给哪一版任务规格编的。

    记两样：任务号与规格版本。用户改了目标之后任务号可能不变、`revision` 变，
    所以两者都要比；只比任务号会漏掉「同一件事改了要求」。
    """

    task_id: str = ""
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", str(self.task_id or ""))
        object.__setattr__(self, "revision", int(self.revision or 0))

    def stale_reason(self, task_id: str, revision: int) -> str:
        """这份计划是否已经作废；仍有效时返回空串。"""
        if not self.task_id:
            return ""
        current_id = str(task_id or "")
        if current_id and current_id != self.task_id:
            return (
                f"计划是给任务 {self.task_id} 编的，当前任务已经是 {current_id}："
                "这份计划作废，按新规格重新编译"
            )
        if int(revision or 0) > self.revision:
            return (
                f"计划按第 {self.revision} 版任务规格编的，当前是第 {int(revision or 0)} 版："
                "这份计划作废，按新规格重新编译"
            )
        return ""
