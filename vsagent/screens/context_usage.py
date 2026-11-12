from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from .. import palette
from ..widgets.status_bar import LAYER_LABELS, format_tokens
from .click_dismiss import BODY_CLASS, ClickDismissModal

# 条的最大格数。面板宽 72 列时，条 24 + 间隔 2 + 类别 10 + 数字 7 还余下十几列，
# 窄终端上也不会把类别或数字挤掉。
BAR_WIDTH = 24


def usage_items(metrics: dict[str, Any]) -> list[tuple[str, int]]:
    """当前上下文里每一项的 token 数，按占用从大到小排。

    只留非零项：零项的条长为零，列出来只会把真正的几项挤到面板下面。
    工具定义单独一项，它每一步都随请求重发；「其他开销」是服务端记账与装配
    估算之差，混进任何一层都会把那一层算多。
    """
    usage = metrics.get("usage") or {}
    items = [
        (LAYER_LABELS[name], int(usage.get(name) or 0))
        for name in LAYER_LABELS
        if int(usage.get(name) or 0) > 0
    ]
    tools = int(metrics.get("tools") or 0)
    if tools > 0:
        items.append(("工具定义", tools))
    extra = max(
        0,
        int(metrics.get("input") or 0)
        - int(metrics.get("total") or 0)
        - tools,
    )
    if extra > 0:
        items.append(("其他开销", extra))
    items.sort(key=lambda pair: -pair[1])
    return items


def bar_line(label: str, value: int, maximum: int) -> Text:
    """一行：左边是按最大值缩放的条，右边是类别与 token 数。

    条的底色留一条空轨道，长短才有比较的基准；只画实心条时，
    最长的那个看起来和「铺满整行」没有区别。
    """
    filled = round(value / maximum * BAR_WIDTH) if maximum else 0
    # 非零项至少留一格：四舍五入到 0 之后，它看起来像没占上下文。
    filled = max(1, min(BAR_WIDTH, filled))
    label_pad = " " * max(0, 10 - cell_len(label))
    line = Text()
    line.append("█" * filled, style=palette.ACCENT)
    line.append("░" * (BAR_WIDTH - filled), style=palette.LINE_SOFT)
    line.append(f"  {label}{label_pad}{format_tokens(value):>7}", style=palette.TEXT_1)
    return line


class ContextUsageScreen(ClickDismissModal[None]):
    """点开占比数字之后从下方升起的柱状图。

    贴底显示，底边紧挨状态栏；水平中心对准状态栏里那个百分比数字，
    这样点开之后视线不用左右移动。底部留 1 行，正好让开状态栏。
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter", "dismiss", "Close", priority=True),
        Binding("q", "dismiss", "Close", priority=True),
    ]

    # 面板宽度。条 24 + 间隔 2 + 类别 10 + 数字 7 = 43，加边框与内边距正好 47，
    # 留 1 格余量。比整屏窄，读完条不用转头；这个宽度也让面板中心与状态栏数字
    # 的中心落在同一列（数字中心距屏幕右沿 24 格，面板半宽 24 格）。
    WIDTH = 48

    def __init__(self, metrics: dict[str, Any], anchor_x: int) -> None:
        super().__init__()
        self._metrics = metrics
        self._anchor_x = anchor_x

    def on_mount(self) -> None:
        """把面板平移到百分比数字的中心，同时夹在屏幕内。"""
        dialog = self.query_one("#context-usage-dialog")
        screen_width = self.size.width
        width = min(self.WIDTH, int(screen_width * 0.9))
        left = self._anchor_x - width // 2
        left = max(0, min(left, screen_width - width))
        centered = (screen_width - width) // 2
        dialog.styles.offset = (left - centered, 0)

    def compose(self) -> ComposeResult:
        window = int(self._metrics.get("window") or 0)
        items = usage_items(self._metrics)
        with Vertical(id="context-usage-dialog", classes=BODY_CLASS):
            yield Static("上下文占比", id="context-usage-title", markup=False)
            yield Static(
                f"窗口 {format_tokens(window)}",
                id="context-usage-window",
                markup=False,
            )
            if items:
                maximum = items[0][1]
                for label, value in items:
                    yield Static(bar_line(label, value, maximum), classes="context-usage-line")
            else:
                yield Static("还没有装配度量", classes="context-usage-line", markup=False)
            yield Static("点击空白处关闭（esc）", id="context-usage-hint", markup=False)

    def action_dismiss(self) -> None:
        """关闭柱状图。"""
        self.dismiss(None)
