from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from ..core.connection import Connection

# 四个输入框的 id。回车在它们之间往下走，最后一个回车保存。
FIELDS = (
    "connection-base-url",
    "connection-api-key",
    "connection-model",
    "connection-window",
)


class ConnectionFormScreen(ModalScreen[Connection | None]):
    """模型接口表单：地址、密钥、模型名，外加可选的上下文窗口。

    密钥用密码框显示，界面上不会把明文留在屏幕上；但它的值会随配置写进
    `connection.toml`（权限 0600），否则每次启动都要重填。

    回车在四个框之间往下走，最后一格回车即保存；`ctrl+s` 随时保存。
    校验失败时把原因写在表单底部，不关窗——关掉就得从头再填一遍。
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(self, connection: Connection | None = None) -> None:
        super().__init__()
        self._current = connection

    def compose(self) -> ComposeResult:
        current = self._current
        with Vertical(id="connection-form"):
            yield Static("模型接口", id="connection-title")
            yield Label("接口地址", classes="connection-label")
            yield Input(
                value=current.base_url if current else "",
                placeholder="https://api.example.com/v1",
                id="connection-base-url",
            )
            yield Label("密钥", classes="connection-label")
            yield Input(
                value=current.api_key if current else "",
                placeholder="sk-...",
                password=True,
                id="connection-api-key",
            )
            yield Label("模型名", classes="connection-label")
            yield Input(
                value=current.model if current else "",
                placeholder="deepseek-flash",
                id="connection-model",
            )
            yield Label("上下文窗口（可空，清单外的模型才需要）", classes="connection-label")
            yield Input(
                value=str(current.context_window) if current and current.context_window else "",
                placeholder="128000",
                id="connection-window",
            )
            yield Static("", id="connection-error")
            yield Static("tab 切换    enter 下一项    ctrl+s 保存    esc 取消", id="connection-hint")

    def on_mount(self) -> None:
        """聚焦第一格。"""
        self.query_one("#connection-base-url", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """回车往下走一格，最后一格保存。"""
        index = FIELDS.index(event.input.id or FIELDS[0])
        if index + 1 < len(FIELDS):
            self.query_one(f"#{FIELDS[index + 1]}", Input).focus()
            return
        self.action_save()

    def action_save(self) -> None:
        """校验并返回连接配置。"""
        base_url = self._value("connection-base-url")
        api_key = self._value("connection-api-key")
        model = self._value("connection-model")
        window_text = self._value("connection-window")
        if not (base_url and api_key and model):
            self._error("接口地址、密钥、模型名都要填")
            return
        if not base_url.startswith(("http://", "https://")):
            self._error("接口地址要以 http:// 或 https:// 开头")
            return
        window: int | None = None
        if window_text:
            try:
                window = int(window_text)
            except ValueError:
                self._error("上下文窗口要写整数，例如 128000")
                return
            if window <= 0:
                self._error("上下文窗口要大于 0")
                return
        self.dismiss(
            Connection(
                base_url=base_url,
                api_key=api_key,
                model=model,
                context_window=window,
                # 单价不在表单上，改配置时原样带过去，免得顺手把已有的数字抹掉。
                price_in=self._current.price_in if self._current else None,
                price_out=self._current.price_out if self._current else None,
            )
        )

    def action_cancel(self) -> None:
        """放弃修改。"""
        self.dismiss(None)

    def _value(self, selector: str) -> str:
        """取一格去掉首尾空白的值。"""
        return self.query_one(f"#{selector}", Input).value.strip()

    def _error(self, text: str) -> None:
        """在表单底部写一行错误。"""
        self.query_one("#connection-error", Static).update(text)
