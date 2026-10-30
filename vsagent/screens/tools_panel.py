from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

# 开关状态用一个字符表示。开/关两态用同一对「实心/空心圆」而不是
# 「on/off」这种词：一列排下来，扫一眼就能数出开了几个。
ON_MARK = "●"
OFF_MARK = "○"


def summarize(text: str) -> str:
    """把工具说明压成一行：取第一行、去掉强调标记。

    说明里有整段带换行的（`search_pdb` 那种），不压的话第二行起就顶出组件边界，
    一行高的格子把它裁成半截，看着像乱码。太宽的部分交给 CSS 打省略号——
    这里只保证「一行」这件事，宽度是多少个字符由面板宽度说了算。
    """
    line = next((item for item in text.splitlines() if item.strip()), "")
    return line.replace("**", "").strip()


@dataclass(frozen=True)
class ToolEntry:
    """面板里的一行。

    `origin` 是「关掉它的那份配置不在项目目录」时的说明文字（例如「用户配置」）：
    那种条目面板改不动——面板写的是项目侧那份，用户目录那份仍会说关。
    留空表示没这个问题。
    """

    name: str
    description: str
    enabled: bool
    origin: str = ""


class ToolsPanelScreen(ModalScreen[None]):
    """工具管理面板：逐条开关，改完立刻生效。

    开关写进项目侧的 `.opencode_tui/tools.toml`，与手改那份文件是同一个东西；
    面板只是让它不必去翻文件、也不必重启——关掉的工具不装进注册表，
    下一次发问的请求体里就没有它。

    回调而不是返回值：一按就要看见结果（这一行变灰、标题上的计数跟着变），
    关掉面板再统一生效的话，用户没法确认自己刚才按对了没有。
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("space", "toggle", "Toggle", priority=True),
        Binding("enter", "toggle", "Toggle", priority=True),
    ]

    def __init__(self, entries: list[ToolEntry], on_toggle: Callable[[str, bool], list[ToolEntry]]) -> None:
        super().__init__()
        self.entries = list(entries)
        self._on_toggle = on_toggle

    def compose(self) -> ComposeResult:
        with Vertical(id="tools-panel"):
            yield Static("工具", id="tools-title")
            yield ListView(id="tools-list")
            # 提示行要能在 80 列的终端里整句放得下（面板宽 70%、最多 90 列）：
            # 挤不下的后半句会被裁掉，而这句话正是在解释「关掉意味着什么」。
            yield Static("space/enter 开关  esc 关闭  关掉的工具模型看不到", id="tools-hint")

    def on_mount(self) -> None:
        """填充列表并聚焦。"""
        self.refresh_list()
        self.query_one("#tools-list", ListView).focus()

    def refresh_list(self) -> None:
        """重建列表，尽量把光标留在原来那一行。

        每按一次开关都要重建（状态可能被别处的配置改回来），光标要是跟着回到第一行，
        连着关几个工具就得每次重新往下找。
        """
        view = self.query_one("#tools-list", ListView)
        cursor = view.index or 0
        view.clear()
        for entry in self.entries:
            view.append(ListItem(self._row(entry)))
        self.query_one("#tools-title", Static).update(self._title_text())
        if self.entries:
            view.index = min(cursor, len(self.entries) - 1)

    def _row(self, entry: ToolEntry) -> Vertical:
        """一行两段：名字（带开关标记）与说明。"""
        mark = ON_MARK if entry.enabled else OFF_MARK
        title = Static(f"{mark} {entry.name}", classes="tool-row-title", markup=False)
        if not entry.enabled:
            title.add_class("-off")
        text = f"  {summarize(entry.description)}{self._origin_text(entry)}"
        meta = Static(text, classes="tool-row-meta", markup=False)
        return Vertical(title, meta, classes="tool-row")

    def _origin_text(self, entry: ToolEntry) -> str:
        """说明后面补一句「这条是谁关的」，只在不是项目配置时出现。"""
        return f"　· 由{entry.origin}关闭" if entry.origin else ""

    def _title_text(self) -> str:
        """标题栏：总数与开着的个数。"""
        enabled = sum(1 for entry in self.entries if entry.enabled)
        return f"工具　{enabled}/{len(self.entries)} 已启用"

    def _current(self) -> ToolEntry | None:
        """光标所在的那一行。"""
        index = self.query_one("#tools-list", ListView).index
        if index is None or not 0 <= index < len(self.entries):
            return None
        return self.entries[index]

    def action_toggle(self) -> None:
        """把光标那一行翻过来。"""
        entry = self._current()
        if entry is None:
            return
        self.entries = self._on_toggle(entry.name, not entry.enabled)
        self.refresh_list()

    def action_dismiss(self) -> None:
        """关闭面板。"""
        self.dismiss(None)
