from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static


@dataclass(frozen=True)
class ModelInfo:
    """可选模型的描述信息。"""

    id: str
    name: str
    provider: str
    context_window: int


class ModelSelectorScreen(ModalScreen[str | None]):
    """模型选择器，展示名称、提供商与上下文窗口。

    可选模型由调用方给出：模型名与提供方都取自 `.env`。
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "select_model", "Select", priority=True),
    ]

    def __init__(self, models: list[ModelInfo]) -> None:
        super().__init__()
        self.models = list(models)

    def compose(self) -> ComposeResult:
        with Vertical(id="model-selector"):
            yield Static("Models", id="model-title")
            yield ListView(id="model-list")
            yield Static("enter 选择   esc 关闭", id="model-hint")

    def on_mount(self) -> None:
        """填充模型列表并聚焦。"""
        view = self.query_one("#model-list", ListView)
        for model in self.models:
            context = f"{model.context_window // 1000}k"
            view.append(ListItem(Label(f"{model.name}   {model.provider}   {context}")))
        if self.models:
            view.index = 0
        view.focus()

    def action_select_model(self) -> None:
        """选择当前模型。"""
        view = self.query_one("#model-list", ListView)
        if not self.models:
            self.dismiss(None)
            return
        self.dismiss(self.models[view.index or 0].id)

    def action_dismiss(self) -> None:
        """关闭选择器。"""
        self.dismiss(None)
