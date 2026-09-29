from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class AutocompletePopup(Widget):
    """补全弹窗基类，渲染一个带高亮项的可选列表。"""

    # 一次显示多少个候选。与 DEFAULT_CSS 里的 max-height 对应：标题 + 6 行刚好放得下。
    MAX_ROWS = 6

    DEFAULT_CSS = """
    AutocompletePopup {
        height: auto;
        max-height: 8;
        display: none;
        background: #1A1A1A;
        border: round #4A9EFF;
        padding: 0 1;
    }
    AutocompletePopup.-visible { display: block; }
    AutocompletePopup .ac-item { color: #999999; }
    AutocompletePopup .ac-item.-active { color: #FFFFFF; background: #4A9EFF; }
    """

    def __init__(self, id: str, title: str, **kwargs: object) -> None:
        super().__init__(id=id, **kwargs)  # type: ignore[arg-type]
        self._title = title
        self._items: list[str] = []
        self._index = 0

    def compose(self) -> ComposeResult:
        yield Static(self._render_text(), classes="ac-body")

    @property
    def visible_items(self) -> list[str]:
        """当前候选列表。"""
        return list(self._items)

    def set_items(self, items: list[str]) -> None:
        """设置候选列表并按需显示弹窗。"""
        self._items = list(items)
        self._index = 0 if self._items else -1
        self.set_class(bool(self._items), "-visible")
        self._refresh()

    def hide(self) -> None:
        """隐藏弹窗。"""
        self._items = []
        self._index = -1
        self.remove_class("-visible")
        self._refresh()

    @property
    def is_open(self) -> bool:
        """弹窗是否处于显示状态。"""
        return bool(self._items)

    def move(self, delta: int) -> None:
        """在候选项之间移动高亮。"""
        if not self._items:
            return
        self._index = (self._index + delta) % len(self._items)
        self._refresh()

    def current(self) -> str | None:
        """返回当前高亮的候选项。"""
        if not self._items or self._index < 0:
            return None
        return self._items[self._index]

    def _refresh(self) -> None:
        """刷新渲染内容。"""
        if not self.is_mounted:
            return
        self.query_one(".ac-body", Static).update(self._render_text())

    def _window(self) -> tuple[int, list[str]]:
        """返回（起始下标, 这次要显示的那一段候选）。

        只画前 MAX_ROWS 个候选是不够的：下移到第 MAX_ROWS + 1 个之后，没有任何一行的
        下标能对上高亮下标，标记会从画面上消失，而回车照样插入那一个看不见的候选。
        所以窗口要跟着高亮走，让高亮项一定出现在显示范围里。
        """
        total = len(self._items)
        if not total:
            return 0, []
        start = max(0, min(self._index - self.MAX_ROWS + 1, total - self.MAX_ROWS))
        return start, self._items[start : start + self.MAX_ROWS]

    def _render_text(self) -> str:
        lines = [f"[b]{self._title}[/b]"]
        start, window = self._window()
        for position, item in enumerate(window, start=start):
            marker = "▸" if position == self._index else " "
            lines.append(f"{marker} {item}")
        if not self._items:
            lines.append("(无匹配)")
        return "\n".join(lines)


class FileAutocomplete(AutocompletePopup):
    """文件补全弹窗，输入 @ 时触发。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(id="file-autocomplete", title="文件引用", **kwargs)
