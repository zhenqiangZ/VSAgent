"""派发子 agent 的模态：选一段消息区间、写任务、起个名字。

范围用两个数字输入而不是列表多选：TUI 里逐条勾选要半屏，而用户真正想说的是
「从第几条到第几条」。旁边实时显示首尾两条消息的时间与首行，确认选对了段。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from ..core.agent.context import ContextAssembler
from ..core.agent.prompt import estimate_tokens, translate_message
from ..core.models import Message, Session


@dataclass(frozen=True)
class SpawnRequest:
    """用户确认之后交给 app 的一份派发参数。"""

    parent_id: str
    title: str
    task: str
    start: int
    end: int


def _first_line(message: Message) -> str:
    """消息的可见首行，用来在预览里认出选中的是哪一条。"""
    for item in translate_message(message):
        text = str(item.get("content") or "").strip()
        if text:
            first = text.splitlines()[0]
            return first[:40] + ("…" if len(first) > 40 else "")
    return "（没有文字内容）"


def _stamp(message: Message) -> str:
    """消息时间，精确到分钟。"""
    return time.strftime("%H:%M", time.localtime(message.created_at))


class SpawnAgentScreen(ModalScreen[SpawnRequest | None]):
    """创建子 agent 的模态。"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Create", priority=True),
    ]

    def __init__(
        self,
        parent: Session,
        context_window: int,
        tools: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self.source = parent
        self.context_window = context_window
        self.tools = tools or []

    def compose(self) -> ComposeResult:
        total = len(self.source.messages)
        with Vertical(id="spawn-dialog"):
            yield Static("派发子 Agent", id="spawn-title")
            yield Static("名称", classes="spawn-label")
            yield Input(placeholder="靶点调研", id="spawn-name")
            yield Static("范围", classes="spawn-label")
            with Horizontal(id="spawn-range"):
                yield Input("0" if total == 0 else "1", id="spawn-start", placeholder="起点")
                yield Static("→", id="spawn-arrow")
                yield Input("0" if total == 0 else str(max(1, total)), id="spawn-end", placeholder="终点")
                yield Static(f"共 {total} 条", id="spawn-total")
            yield Static("", id="spawn-preview", markup=False)
            yield Static("任务", classes="spawn-label")
            yield TextArea(id="spawn-task")
            yield Static("", id="spawn-error")
            yield Static(
                "创建后立即在后台开始，父会话可以继续用。"
                "子 agent 内不能再派发新的 agent。\n"
                "ctrl+enter 创建并运行　esc 取消",
                id="spawn-hint",
            )
            with Horizontal(id="spawn-buttons"):
                yield Button("创建并运行", id="spawn-create", variant="primary")
                yield Button("取消", id="spawn-cancel")

    def on_mount(self) -> None:
        """默认把光标放在名称上，并把预览刷出来。"""
        self.query_one("#spawn-name", Input).focus()
        self._refresh_preview()

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        """范围一变就重算预览与预算提示。"""
        if event.input.id in ("spawn-start", "spawn-end"):
            self._refresh_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """两个按钮。"""
        if event.button.id == "spawn-create":
            self.action_submit()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        """取消，不创建任何东西。"""
        self.dismiss(None)

    def action_submit(self) -> None:
        """校验之后创建。校验不过就把原因写在窗里，不关窗。"""
        total = len(self.source.messages)
        try:
            start = int(self.query_one("#spawn-start", Input).value.strip())
            end = int(self.query_one("#spawn-end", Input).value.strip())
        except ValueError:
            self._error("范围要填整数，例如 1 和 12")
            return
        if total == 0:
            # 空会话没有区间可选，子 agent 只带任务书。
            start = end = 0
            zero_based = 0
        else:
            if not (1 <= start < end <= total):
                self._error(f"范围要在 1 到 {total} 之间，且起点小于终点")
                return
            # 界面上是 1 起的序号，数据里是 0 起的下标。
            zero_based = start - 1
        task = self.query_one("#spawn-task", TextArea).text.strip()
        if not task:
            self._error("任务不能为空：子 agent 需要知道要做什么")
            return
        title = self.query_one("#spawn-name", Input).value.strip()
        if not title:
            title = task.splitlines()[0][:20]
        self.dismiss(
            SpawnRequest(
                parent_id=self.source.id,
                title=title,
                task=task,
                start=zero_based,
                end=end,
            )
        )

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------
    def _error(self, text: str) -> None:
        """把校验原因写到提示行。"""
        self.query_one("#spawn-error", Static).update(text)

    def _refresh_preview(self) -> None:
        """显示区间首尾消息，并在区间太大时先提醒一句。"""
        self.query_one("#spawn-error", Static).update("")
        total = len(self.source.messages)
        if total == 0:
            self.query_one("#spawn-preview", Static).update(
                "当前会话还没有消息，子 agent 只带任务书。"
            )
            return
        try:
            start = int(self.query_one("#spawn-start", Input).value.strip())
            end = int(self.query_one("#spawn-end", Input).value.strip())
        except ValueError:
            self.query_one("#spawn-preview", Static).update("")
            return
        start = max(1, min(start, total))
        end = max(1, min(end, total))
        if start >= end:
            self.query_one("#spawn-preview", Static).update("起点要小于终点")
            return
        head = self.source.messages[start - 1]
        tail = self.source.messages[end - 1]
        lines = [
            f"{_stamp(head)}  你/助手：{_first_line(head)}",
            f"{_stamp(tail)}  你/助手：{_first_line(tail)}",
        ]
        picked = self.source.messages[start - 1 : end]
        tokens = sum(
            estimate_tokens(str(translate_message(message))) for message in picked
        )
        budget = int(ContextAssembler(self.context_window).available(self.tools) * 0.6)
        if budget > 0 and tokens > budget:
            lines.append(f"区间约 {tokens} tokens，超过预算 {budget}，将从最早的消息开始截断")
        self.query_one("#spawn-preview", Static).update("\n".join(lines))
