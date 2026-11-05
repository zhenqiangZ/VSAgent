from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
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

# 标题里的动作。每个工具要用户确认的事不一样：bash 是运行命令，
# write 是把内容写到某个位置，一律写成「执行这次调用」等于什么都没说。
TITLE_ACTIONS = {
    "bash": "运行这条命令",
    "read": "读取这个路径",
    "grep": "在这个路径里检索",
    "write": "写入这个路径",
    "patch": "修改这个路径",
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

    版式照 macOS 的授权面板：一行小标题说明这是什么窗口，一行问题说明
    要放行什么，下面按「命令 / 原因」分组，最后是右对齐的按钮行。
    层级靠亮度区分（标题亮、说明中灰、辅助说明更暗），只有警示图标用橙色；
    整块边框与标题都不着色，否则每一张询问都像一条错误。

    参数单独放进一个可滚动的预览块：命令、路径、正则长得完全不一样，
    混在原因那一行里时，用户很难一眼看出这次到底要跑什么。
    预览块超过八行就自己滚动，弹窗本身不会被一条长命令撑破。

    用户按 a 之后**本会话的所有工具调用**都不再问，包括工作目录之外的访问、
    Plan 方式下原本被拦下的工具与联网计费。只有用户中断仍然优先。
    范围写得比实现窄，用户按下去之后以为只放行了一条，实际整个会话的询问都关了；
    写得比实现宽，用户会在不知情的情况下放开更多东西。这一版按实现如实写。
    """

    BINDINGS = [
        Binding("enter", "allow", "允许一次", priority=True),
        Binding("y", "allow", "允许一次", priority=True),
        Binding("a", "allow_always", "本会话全部允许", priority=True),
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
            yield Static("VSAgent　请求执行权限", id="permission-eyebrow", markup=False)
            yield Static(self.title_text(), id="permission-title", markup=False)
            yield Static(
                f"Agent {self._agent_name}　·　工具 {self._tool_name}",
                id="permission-detail",
                markup=False,
            )
            yield Static(self._argument_label, id="permission-label", markup=False)
            # 用可滚动容器而不是 Static：Static 的 max-height 只截断、滚不动，
            # 长命令的后半段就永远看不到了。
            with VerticalScroll(id="permission-argument"):
                yield Static(self.argument_text(), id="permission-argument-text", markup=False)
            if self._reason:
                # 图标与正文分成两个部件：原因里可能出现越界路径，路径里带方括号时
                # 走 markup 会被当成样式标签吞掉，所以这里一律关闭 markup。
                with Horizontal(id="permission-reason-row"):
                    yield Static("⚠", id="permission-reason-icon", markup=False)
                    yield Static(f"原因　{self._reason}", id="permission-reason", markup=False)
            yield Static("─" * 200, id="permission-divider", markup=False)
            with Horizontal(id="permission-actions"):
                yield Static("拒绝  N", id="permission-deny", markup=False)
                yield Static("全部允许  A", id="permission-all", markup=False)
                yield Static("允许一次  Y", id="permission-allow", markup=False)
            yield Static(self.hint_text(), id="permission-hint")

    def title_text(self) -> str:
        """标题：这个工具接下来要做什么。"""
        action = TITLE_ACTIONS.get(self._tool_name, "执行这次调用")
        return f"允许 {self._tool_name} {action}？"

    def hint_text(self) -> str:
        """底部一行小字，写清 a 的授权范围与恢复方式。"""
        return "回车允许一次　·　A 之后本会话不再询问（含越界与计费），切换会话恢复。"

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
        """允许这一次，并把本会话的所有工具调用一次性放行。"""
        self.dismiss(PermissionAnswer(Verdict.ALLOW, True))

    def action_deny(self) -> None:
        """拒绝这一次。"""
        self.dismiss(PermissionAnswer(Verdict.DENY, False))
