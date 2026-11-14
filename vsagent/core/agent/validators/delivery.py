"""交付验收：轮末看每个必需交付物在不在。

它**不阻断**。原因很实际：一轮结束时回答已经给出去了，能做的只有记下来——
"这次少交了 CandidateSet"，供人看 trace 或由下一轮补。真正阻断要再来一轮
"补交"，那是另一个设计，不在这一步。

只验能机械验证的那几种（见 `output_contract.py`）。`manual` 类交付物不报问题，
但要能被查出来：`unverifiable()` 给调用方拿去说明"这些没有验"，免得把"没验"
当成"验过了"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts import TaskSpec
from .base import Finding
from .output_contract import NAME, unverifiable, verify


def check(
    task: TaskSpec | None,
    *,
    ledger: Any = None,
    workspace: Path | None = None,
) -> list[Finding]:
    if task is None:
        return []
    findings: list[Finding] = []
    for row in task.required_outputs:
        finding = verify(row["name"], ledger=ledger, workspace=workspace)
        if finding is not None:
            findings.append(finding)
    return findings


__all__ = ["NAME", "check", "unverifiable"]
