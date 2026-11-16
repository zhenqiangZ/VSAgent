"""能力守卫：这一步调用的工具，在不在这个会话的名单里。

它守的是「计划说只做结构，模型却去调用对接」这一类越界。判据只有一条：
工具名必须在 `Session` 的允许清单里（名单来自 `agents.toml` 与基础档）。

**有意的宽松**：名单为 `None`（full 档、调试与回滚）时不拦。名单为空也不拦：
那多半是装配出了问题，回退到原来的路径比把工具全锁死更安全。
"""

from __future__ import annotations

from typing import Iterable

from .base import Finding

NAME = "capability_guard"


def check(tool_name: str, allowed: Iterable[str] | None = None) -> list[Finding]:
    if allowed is None:
        return []
    names = set(allowed)
    if not names or tool_name in names:
        return []
    return [
        Finding(
            validator=NAME,
            code="tool_not_allowed",
            message=(
                f"这个角色没有装配 {tool_name}。要它请改 agents.toml，"
                "或把这件事派给装了它的角色。"
            ),
            severity="deny",
        )
    ]
