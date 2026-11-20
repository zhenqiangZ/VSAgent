"""校验器：在执行前后拿已有事实判一次。

现在有两条：

- `tool_guard`：这一步的工具在不在这个角色的名单里；
- `task_precondition`：任务本身是不是处在能执行的状态。

另外两条（输出合同、交付验收）需要先定义"每种交付物怎么验"，等这一层稳定了再加。
"""

from __future__ import annotations

from typing import Any

from vsagent.core.agent.roles.contracts import TaskSpec
from . import delivery, output_contract, task_precondition, tool_guard
from .base import DEFAULT_MODE, ENV, MODES, Finding, Verdict, mode_from_env

__all__ = [
    "DEFAULT_MODE",
    "ENV",
    "MODES",
    "Finding",
    "Verdict",
    "tool_guard",
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
    allowed: Any = None,
    task: TaskSpec | None = None,
) -> Verdict:
    """一次工具调用前的全部判定。顺序固定：先判名单，再判任务。

    `plan` 参数已经不用了：角色定了工具，计划不必再逐角色写一份族清单。
    留着关键字只会让人以为它还在生效。
    """
    findings = [
        *tool_guard.check(tool_name, allowed),
        *task_precondition.check(tool_name, task),
    ]
    return Verdict(tuple(findings))
