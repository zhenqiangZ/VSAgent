"""能力守卫：这一步用的工具族，在不在计划里。

它守的是"计划说只做结构，模型却去调用对接"这一类越界。判据只有一条：
工具所属的族必须在计划的 `tool_families` 里；常驻工具（不属于任何族）不在此列，
它们由工具档位管。

**有意的宽松**：计划为空、或一个族都没写时，什么都不拦。原因是这条规则只在
`enforce` 模式下才真的阻断，而计划缺失往往意味着解析失败——那种时候应当回退到
原来的路径，不是把工具全锁死。
"""

from __future__ import annotations

from ..contracts import CapabilityPlan
from ..toolset import family_of
from .base import Finding

NAME = "capability_guard"


def check(tool_name: str, plan: CapabilityPlan | None) -> list[Finding]:
    if plan is None:
        return []
    family = family_of(tool_name)
    if not family:
        # 常驻工具（bash、read、load_tools……）不属于任何族：它们是执行通道，
        # 不是某一项科研能力的产物工具。
        return []
    allowed: list[str] = []
    for names in plan.tool_families.values():
        for name in names:
            if name not in allowed:
                allowed.append(name)
    if family in allowed:
        return []
    return [
        Finding(
            validator=NAME,
            code="family_not_planned",
            message=(
                f"{tool_name} 属于 {family} 族，但这次计划里只有 "
                f"{'、'.join(allowed) or '（空）'}。先回到计划：确实需要这一族，"
                "就说明理由并重新规划；不需要就换一条路。"
            ),
            severity="deny",
        )
    ]
