"""任务前置：这一步之前，任务本身是否处在能开工的状态。

现在能机械判定的只有两件事：

- 任务是不是已经被取消或标记失败（那种状态下不该再执行任何执行类动作）；
- 任务要求的输入是不是都在手上。

**不把"缺 PDB 编号"算作前置缺失**：那是发现类任务的正常起点，不是阻塞。
这条边界与意图解析里的澄清规则一致，两边都按同一个标准，免得一处放行一处拦下。
"""

from __future__ import annotations

from vsagent.core.agent.roles.contracts import TaskSpec
from .base import Finding

NAME = "task_precondition"

_EXECUTE_FAMILIES = ("docking", "affinity", "screen", "pharmacophore", "receptor", "ligand")


def check(tool_name: str, task: TaskSpec | None) -> list[Finding]:
    if task is None:
        return []
    findings: list[Finding] = []
    if task.status in ("cancelled", "failed"):
        findings.append(
            Finding(
                validator=NAME,
                code="task_not_runnable",
                message=f"任务状态是 {task.status}，不能再执行 {tool_name}。先确认是否要重开一个任务。",
                severity="deny",
            )
        )
    missing = [
        row["name"]
        for row in task.required_inputs
        if row["name"] not in task.available_inputs
    ]
    if missing:
        findings.append(
            Finding(
                validator=NAME,
                code="missing_input",
                message=f"任务还缺这些输入：{'、'.join(missing)}。缺输入时可以先去查，但不要跳过它直接交付。",
                severity="warn",
            )
        )
    from ..toolset import family_of

    if family_of(tool_name) in _EXECUTE_FAMILIES and task.status == "draft":
        findings.append(
            Finding(
                validator=NAME,
                code="task_not_ready",
                message="任务还是草稿状态：先把它确认成 ready，再执行需要产物的动作。",
                severity="deny",
            )
        )
    return findings
