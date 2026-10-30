from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..core.agent.permission import Verdict

# 参数预览的上限。比旧版的 200 放宽很多：询问弹窗的价值就在于让用户看清
# 将要执行什么，截得太早会把危险的后半段藏起来。再长就交给预览框自己滚动。
ARGUMENT_LIMIT = 2000

# 不同工具的主参数含义不同，标签跟着变。写死「参数」的话，
# bash 里那一长串命令与 write 里的一个路径看起来像同一种东西。
ARGUMENT_LABELS = {
    "bash": "命令",
    "read": "路径",
    "write": "路径",
    "patch": "路径",
    "grep": "模式",
}


def argument_label_for(tool_name: str) -> str:
    """主参数在弹窗上的标签。"""
    return ARGUMENT_LABELS.get(tool_name, "参数")


@dataclass(frozen=True)
class PermissionAnswer:
    """用户在弹窗上给出的答复。"""

    verdict: Verdict
    remember: bool


class PermissionDialog(ModalScreen[PermissionAnswer]):
    """工具调用前的询问弹窗。

    弹窗上要写清三件事：是哪个工具、拿什么参数、为什么被拦下来。
    参数单独放进一个带边框的预览框里：命令、路径、正则长得完全不一样，
    混在「原因」那一堆行里时，用户很难一眼看出这次到底要跑什么。
    预览框超过八行就自己滚动，弹窗本身不会被一条长命令撑破。

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

    def __init__(
        self,
        tool_name: str,
        argument: str,
        agent_name: str,
        reason: str,
    ) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._argument = argument
        self._agent_name = agent_name
        self._reason = reason
        self._argument_label = argument_label_for(tool_name)

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-dialog"):
            yield Static(f"允许调用 {self._tool_name}？", id="permission-title", markup=False)
            yield Static(
                f"Agent　{self._agent_name}　·　工具　{self._tool_name}",
                id="permission-detail",
                markup=False,
            )
            yield Static(self._argument_label, id="permission-label", markup=False)
            # 用可滚动容器而不是 Static：Static 的 max-height 只截断、滚不动，
            # 长命令的后半段就永远看不到了。
            with VerticalScroll(id="permission-argument"):
                yield Static(self.argument_text(), id="permission-argument-text", markup=False)
            if self._reason:
                yield Static(f"原因　{self._reason}", id="permission-reason", markup=False)
            yield Static(self.hint_text(), id="permission-hint")

    def hint_text(self) -> str:
        """提示行：三个按键做成色块，下面一行写清 a 的边界。"""
        return (
            "[#0A0A0A on #4A9EFF] y [/] 允许一次    "
            "[#0A0A0A on #FFB300] a [/] 本会话放行所有工具    "
            "[#0A0A0A on #666666] n [/] 拒绝\n"
            "[#666666]a 放行的是工具，不是磁盘：工作目录之外仍会问；esc 等同拒绝[/]"
        )

    def argument_text(self) -> str:
        """参数预览正文，过长时截断并写明去哪里看全文。"""
        argument = self._argument or "（无）"
        if len(argument) > ARGUMENT_LIMIT:
            argument = (
                argument[: ARGUMENT_LIMIT - 3]
                + "…（已截断，完整参数见消息区里的工具卡片）"
            )
        return argument

    def action_allow(self) -> None:
        """允许这一次。"""
        self.dismiss(PermissionAnswer(Verdict.ALLOW, False))

    def action_allow_always(self) -> None:
        """允许这一次，并把本会话的所有工具一次性放行。"""
        self.dismiss(PermissionAnswer(Verdict.ALLOW, True))

    def action_deny(self) -> None:
        """拒绝这一次。"""
        self.dismiss(PermissionAnswer(Verdict.DENY, False))
