"""Validator 的公共形状。

校验器只做一件事：拿**已有的事实**判一次，返回发现（finding）。它不调用模型，
也不改状态——改状态是调用方的事。这样每条判据都能在测试里直接构造。

严重程度只有三档，语义固定：

- `info`：记一笔，不改行为；
- `warn`：这一轮继续，但 trace 里看得见；
- `deny`：这一步不能做（enforce 时拦下，shadow 时只记录）。

三档之外不加第四档。需要"有时拦有时不拦"的，是模式（off/shadow/enforce），
不是严重程度——两者混在一起会让"这条到底拦不拦"没法回答。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

MODES: tuple[str, ...] = ("off", "shadow", "enforce")
ENV = "VSA_VALIDATE"
DEFAULT_MODE = "shadow"


def mode_from_env(default: str = DEFAULT_MODE) -> str:
    """当前校验模式。认不出的一律回落到 `default`。"""
    value = os.environ.get(ENV, "").strip().lower()
    return value if value in MODES else default


@dataclass(frozen=True)
class Finding:
    """一次判定发现的问题。"""

    validator: str
    code: str
    message: str
    severity: str = "warn"

    def __post_init__(self) -> None:
        if self.severity not in ("info", "warn", "deny"):
            raise ValueError(f"severity 只能是 info/warn/deny：{self.severity}")

    @property
    def blocking(self) -> bool:
        return self.severity == "deny"

    def as_dict(self) -> dict[str, str]:
        return {
            "validator": self.validator,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class Verdict:
    """一组发现的汇总。"""

    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return any(item.blocking for item in self.findings)

    def as_dicts(self) -> list[dict[str, str]]:
        return [item.as_dict() for item in self.findings]
