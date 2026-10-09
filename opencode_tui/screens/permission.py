from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..core.agent.permission import Verdict

ARGUMENT_LIMIT = 200


@dataclass(frozen=True)
class PermissionAnswer:
    """用户在弹窗上给出的答复。"""

    verdict: Verdict
    remember: bool


class PermissionDialog(ModalScreen[PermissionAnswer]):
    """工具调用前的询问弹窗。

    弹窗上要写清三件事：是哪个工具、拿什么参数、为什么被拦下来。
    用户按 a 之后**本会话的所有工具**都不再问；escape 与 n 一样是拒绝，
    随手按一下不会把这次调用放过去。

    a 的文案写的是「所有工具」而不是「这个工具」：两者授权范围差得很远。
    范围写得比实现窄，用户按下去之后以为只放行了一条，实际整个会话的询问都关了；
    写得比实现宽，用户会在不知情的情况下放开更多东西。这一版按实现如实写。

    **工作目录之外的访问不在这条放行里**。边界判定排在放行之前，所以按了 a
    之后走出工作目录仍然会问一次。这一点由提示行点明，免得用户以为 a 是一张
    通行整个磁盘的许可证。
    """

    BINDINGS = [
        Binding("y", "allow", "允许", priority=True),
        Binding("a", "allow_always", "本会话放行所有工具", priority=True),
        Binding("n", "deny", "拒绝", priority=True),
        Binding("escape", "deny", "拒绝", priority=True),
    ]

    def __init__(self, tool_name: str, argument: str, agent_name: str, reason: str) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._argument = argument
        self._agent_name = agent_name
        self._reason = reason

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-dialog"):
            yield Static(f"允许调用 {self._tool_name}？", id="permission-title", markup=False)
            yield Static(self.detail_text(), id="permission-detail", markup=False)
            yield Static(self.hint_text(), id="permission-hint")

    def hint_text(self) -> str:
        """提示行。a 键放行所有工具，并点明它管不到工作目录之外。"""
        return (
            "y 允许一次    a 本会话放行所有工具（工作目录之外仍会问）    n 拒绝（esc 同）"
        )

    def detail_text(self) -> str:
        """弹窗正文，一行一项。"""
        argument = self._argument
        if len(argument) > ARGUMENT_LIMIT:
            argument = argument[: ARGUMENT_LIMIT - 3] + "..."
        lines = [
            f"Agent　{self._agent_name}",
            f"参数　　{argument or '（无）'}",
        ]
        if self._reason:
            lines.append(f"原因　　{self._reason}")
        return "\n".join(lines)

    def action_allow(self) -> None:
        """允许这一次。"""
        self.dismiss(PermissionAnswer(Verdict.ALLOW, False))

    def action_allow_always(self) -> None:
        """允许这一次，并把本会话的所有工具一次性放行。"""
        self.dismiss(PermissionAnswer(Verdict.ALLOW, True))

    def action_deny(self) -> None:
        """拒绝这一次。"""
        self.dismiss(PermissionAnswer(Verdict.DENY, False))
