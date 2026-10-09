from __future__ import annotations

from textual.app import ComposeResult
from textual.markup import escape
from textual.widget import Widget
from textual.widgets import Static


class AutocompletePopup(Widget):
    """补全弹窗基类，渲染一个带高亮项的可选列表。"""

    # 一次显示多少个候选。弹窗高度由它算出来（见 `__init__`），改这一处就够。
    MAX_ROWS = 6

    DEFAULT_CSS = """
    AutocompletePopup {
        height: auto;
        /* 浮在界面上，不占布局流：占流的话弹窗一开就把输入区往上顶（实测顶掉 11 行），
           终端矮的时候连输入框都被挤出屏幕。上移自身一个高度，底边停在摆放位置上。 */
        overlay: screen;
        offset: 0 -100%;
        display: none;
        background: #1A1A1A;
        border: round #4A9EFF;
        padding: 0 1;
    }
    AutocompletePopup.-visible { display: block; }
    AutocompletePopup .ac-item { color: #999999; }
    AutocompletePopup .ac-item.-active { color: #FFFFFF; background: #4A9EFF; }
    """

    def __init__(
        self,
        id: str,
        title: str,
        keep_below: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(id=id, **kwargs)  # type: ignore[arg-type]
        self._title = title
        self._items: list[str] = []
        self._index = 0
        # 往上浮：把自己挪上去一个身位，底边停在摆放位置上，改高度不会把它挪走。
        # `keep_below` 是哪个部件不能被盖住：启动界面的输入框在屏幕中间，弹窗从底部
        # 往上长，矮终端下会长到盖在输入框上面。
        self.keep_below = keep_below
        self.styles.offset = (0, "-100%")
        # 高度按"标题 1 行 + 候选 MAX_ROWS 行 + 上下边框各 1 行"算。把高度写进 DEFAULT_CSS
        # 会与 MAX_ROWS 走散：子类自己那份 DEFAULT_CSS 盖不掉基类的 max-height（实测命令
        # 弹窗写成 10，量出来还是基类的 8），于是渲染出来的行比容器高，末尾几行被裁掉，
        # 高亮挪到那几行时标记整个看不见，而回车照样采纳那条看不见的候选。
        self.styles.max_height = self.MAX_ROWS + 3

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
        # 浮层不占布局流，但也不能浮到屏幕外面：等这次刷新把位置定下来之后，
        # 再按"弹窗底边以上还剩多少行"收一次高度。
        self.call_after_refresh(self._clamp_to_screen)

    def on_resize(self) -> None:
        """终端尺寸变了，弹窗底边也跟着变，高度要重新收一次。"""
        self._clamp_to_screen()

    def _clamp_to_screen(self) -> None:
        """把高度压到这一屏放得下为止。"""
        if not self.is_mounted or not self._items:
            return
        if self._available_rows <= 0:
            return
        wanted = self.visible_rows + 3
        if self.styles.max_height != wanted:
            self.styles.max_height = wanted

    @property
    def _available_rows(self) -> int:
        """这一屏能给弹窗多少行：从底边往上量到屏幕顶，再扣掉不能盖住的那块。

        底边（`region.bottom`）只由摆放位置决定，与高度无关——上浮靠 `offset: -100%`
        把自己挪一个身位，改高度不会把它挪走。
        """
        if not self.is_mounted:
            return 0
        space = self.region.bottom
        if self.keep_below is not None and self.screen is not None:
            found = self.screen.query(self.keep_below)
            if found:
                space = min(space, space - found.first().region.bottom)
        return space

    @property
    def visible_rows(self) -> int:
        """这一屏真正放得下的候选行数。

        窗口要按这个数切，不能按 MAX_ROWS：终端矮的时候高度被压过，
        按 MAX_ROWS 切出来的末尾几行画在容器外面，高亮挪过去就看不见了。

        这个数只能由**可用空间**算，不能由 `region.height` 算：高度是内容行数定的，
        而内容行数又由它定，两个互相依赖会互相拖住——实测第一次弹开时读到的是
        隐藏状态残留的那个高度，候选于是被压成 1 条再也回不来。`offset: 0 -100%`
        让弹窗底边固定在布局起点、与高度无关，所以 `region.bottom` 就是可用行数。
        """
        if not self.is_mounted:
            return self.MAX_ROWS
        available = self._available_rows
        if available <= 0:
            return self.MAX_ROWS
        # 上下边框各 1 行，标题再占 1 行，剩下的才给候选。
        return max(1, min(self.MAX_ROWS, available - 3))

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

    @property
    def index(self) -> int:
        """当前高亮项的下标，没有候选项时是 -1。"""
        return self._index

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

        只画前 N 个候选是不够的：下移到第 N + 1 个之后，没有任何一行的下标能对上高亮
        下标，标记会从画面上消失，而回车照样插入那一个看不见的候选。所以窗口要跟着
        高亮走，让高亮项一定出现在显示范围里。N 取真实可见行数而不是 MAX_ROWS——
        高度被屏幕压过的时候这两个数不相等。
        """
        total = len(self._items)
        if not total:
            return 0, []
        rows = self.visible_rows
        start = max(0, min(self._index - rows + 1, total - rows))
        return start, self._items[start : start + rows]

    def _title_line(self) -> str:
        """标题行，候选多于一屏时把"这一段 / 共几条"写在后面。

        一屏只放得下 8 条而命令有 20 条，光看画面不知道后面还有没有，也不知道
        现在翻到了哪里。
        """
        total = len(self._items)
        if total <= self.visible_rows:
            return f"[b]{self._title}[/b]"
        start, window = self._window()
        return f"[b]{self._title}[/b]{escape(f'  {start + 1}-{start + len(window)}/{total}')}"

    def _render_text(self) -> str:
        """弹窗正文。

        候选项来自文件路径与 Agent 名称，里面可能出现方括号。正文走 markup 解析，
        不转义的话路径会被当成标签吞掉，画面上少一截，插进编辑框的却是完整路径。
        标题是固定文字，保留加粗。
        """
        lines = [self._title_line()]
        start, window = self._window()
        for position, item in enumerate(window, start=start):
            marker = "▸" if position == self._index else " "
            lines.append(escape(f"{marker} {item}"))
        if not self._items:
            lines.append("(无匹配)")
        return "\n".join(lines)


class FileAutocomplete(AutocompletePopup):
    """文件补全弹窗，输入 @ 时触发。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(id="file-autocomplete", title="文件引用", **kwargs)
