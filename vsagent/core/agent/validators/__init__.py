"""校验器：在执行前后拿已有事实判一次。

现在有两条：

- `capability_guard`：这一步的工具族在不在计划里；
- `task_precondition`：任务本身是不是处在能执行的状态。

另外两条（输出合同、交付验收）需要先定义"每种交付物怎么验"，等这一层稳定了再加。
"""

from __future__ import annotations

from typing import Any

from ..contracts import CapabilityPlan, TaskSpec
from . import capability_guard, delivery, output_contract, task_precondition
from .base import DEFAULT_MODE, ENV, MODES, Finding, Verdict, mode_from_env

__all__ = [
    "DEFAULT_MODE",
    "ENV",
    "MODES",
    "Finding",
    "Verdict",
    "capability_guard",
    "check_tool",
    "check_delivery",
    "mode_from_env",
    "task_precondition",
]


def check_delivery(task: TaskSpec | None, *, ledger: Any = None, workspace: Any = None) -> list[Finding]:
    """轮末的交付验收。不阻断，只出发现。"""
    return delivery.check(task, ledger=ledger, workspace=workspace)


def check_tool(
    tool_name: str,
    *,
    plan: CapabilityPlan | None = None,
    task: TaskSpec | None = None,
) -> Verdict:
    """一次工具调用前的全部判定。顺序固定：先判计划，再判任务。"""
    findings = [
        *capability_guard.check(tool_name, plan),
        *task_precondition.check(tool_name, task),
    ]
    return Verdict(tuple(findings))
