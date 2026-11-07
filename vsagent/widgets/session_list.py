"""会话列表：主界面左栏与聊天界面左栏共用的一份。

两处要的东西一样——按更新时间排的会话、运行中的单独一段、当前会话高亮、
子 agent 缩进挂在父会话下面。各写一份的话，状态词与排序迟早会在一边改漏。

这里不读盘、不改会话：只把 `SessionManager` 里现成的那份画出来，点击之后
把会话编号交给界面（`Picked`），切不切由界面决定。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Static

from .. import palette
from ..core.models import Session
from .messages_pane import SESSION_STATUS_TEXT

# 任务状态在列表里只用一处颜色区分：运行中是强调色，失败与中断各一档语义色，
# 完成是低饱和绿，其余一律次要文字。整列都上色会让「哪一行还在跑」这件事看不出来。
_STATUS_COLOR = {
    "running": palette.ACCENT,
    "done": palette.OK,
    "failed": palette.ERROR,
    "cancelled": palette.WARN,
}


def fit(text: str, width: int) -> str:
    """按显示格数截断，超长补省略号。

    汉字占两格，`len()` 数出来的是字数；用字数算宽度会让一行在含中文时溢出。
    """
    text = text.strip()
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    while text and cell_len(text) + 1 > width:
        text = text[:-1]
    return text + "…"


class SessionRow(Static):
    """会话树的一行。点一下把会话编号交给界面。"""

    # 一行就是一行：标题再长也只截断、不折行。类名用 `rail-row`，
    # 会话选择器那一屏的 `.session-row` 是两行条目，同名会互相覆盖。
    DEFAULT_CSS = """
    SessionRow {
        height: 1;
        padding: 0 1;
    }
    """

    class Picked(TextualMessage):
        """用户点了这一行。"""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(
        self,
        session: Session,
        *,
        current: bool = False,
        depth: int = 0,
        width: int = 28,
        meta: str = "",
    ) -> None:
        super().__init__(classes="rail-row")
        self.session_id = session.id
        self._title = session.title or "（没有标题）"
        self._status = session.status or "idle"
        self._depth = depth
        self._width = width
        self._meta = meta
        if current:
            self.add_class("-current")
        if depth:
            self.add_class("-child")

    def render(self) -> Text:
        # 右边留给状态词、附加说明与两层缩进。状态词最长是「运行中」6 格，
        # 它前后各有一段两格的空隙；这四格与 `6` 一起算进 reserve，
        # 否则「运行中」那一行的末字会比内容区宽出几格被裁掉。
        # 行自己左右各有一列内边距，可用宽度也要先减掉这两列。
        inner = max(1, self._width - 2)
        reserve = 10 + self._depth * 2 + (cell_len(self._meta) + 3 if self._meta else 0)
        text = Text()
        if self._depth:
            text.append("  " * self._depth)
        text.append(
            fit(self._title, inner - reserve),
            style=f"bold {palette.TEXT_1}" if self.has_class("-current") else palette.TEXT_1,
        )
        text.append("  ")
        text.append(
            SESSION_STATUS_TEXT.get(self._status, self._status),
            style=_STATUS_COLOR.get(self._status, palette.TEXT_3),
        )
        if self._meta:
            text.append(f"  {self._meta}", style=palette.TEXT_4)
        return text

    def on_click(self) -> None:
        self.post_message(self.Picked(self.session_id))


class NewTaskRow(Static):
    """左栏顶部的「新建任务」入口。

    只把动作交回界面：清空输入框、把焦点放回去，会话等提交时再建。
    在这里直接建会话的话，点了又什么都不输，列表里会多一个空壳。
    """

    DEFAULT_CSS = palette.css("""
    NewTaskRow {
        height: 1;
        padding: 0 1;
        color: $vsa-text-2;
    }
    NewTaskRow:hover {
        background: $vsa-layer;
        color: $vsa-text-1;
    }
    """)

    class Pressed(TextualMessage):
        """用户点了这一行。"""

    def __init__(self) -> None:
        super().__init__("+ 新建任务", classes="rail-row new-task-row", markup=False)

    def on_click(self) -> None:
        self.post_message(self.Pressed())


class SessionList(VerticalScroll):
    """会话树。`with_children` 为真时把子 agent 缩进挂在父会话下面。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._with_children = False
        self._new_task = False
        self._list_title: str | None = None
        # 上一次画出来的那份内容。每一步都会刷新左栏，内容没变时重建一次等于
        # 把滚动位置推回顶部：用户正翻着会话，模型每走一步就被弹回去一次。
        self._signature: tuple[object, ...] | None = None

    def refresh_sessions(
        self,
        *,
        with_children: bool = False,
        limit: int = 40,
        new_task: bool = False,
        list_title: str | None = None,
    ) -> None:
        """重画会话树。

        `new_task` 在顶部加一行「新建任务」入口（主界面用）；`list_title` 换掉
        非运行中那一段的标题（主界面叫「最近任务」，聊天左栏叫「全部任务」）。
        """
        self._with_children = with_children
        self._new_task = new_task
        self._list_title = list_title
        manager = self.app.session_manager
        current = manager.current_session_id
        roots = manager.root_sessions()
        width = max(12, self.content_size.width or 30)

        # 先把这一段要画的东西列成一张表，再与上一次那张表比。
        # 第一条是种类，第二条是载荷（分节标题的字符串或会话），第三条是缩进层数。
        entries: list[tuple[str, str | Session, int]] = []
        if new_task:
            entries.append(("new", "", 0))
        running = [item for item in roots if item.status == "running"]
        if running:
            entries.append(("section", f"运行中 {len(running)}", 0))
            entries.extend(("row", item, 0) for item in running)
        listed = [item for item in roots if item.status != "running"][:limit]
        # 有运行中的任务时，第二段只列其余那些，标题跟着改：写「全部任务 3」而下面
        # 一行都没有，读的人会以为列表坏了。段标题带条数，栏拖窄之后数字是唯一
        # 还读得出来的信息。
        if listed:
            label = list_title or ("其他任务" if running else "全部任务")
            entries.append(("section", f"{label} {len(listed)}", 0))
        for item in listed:
            entries.append(("row", item, 0))
            if with_children:
                entries.extend(("row", child, 1) for child in manager.children_of(item.id))
        if not roots:
            entries.append(("empty", "还没有会话", 0))

        signature = (width, tuple(self._mark(entry, current) for entry in entries))
        if signature == self._signature:
            return
        self._signature = signature

        rows: list[Static] = []
        for kind, payload, depth in entries:
            if kind == "new":
                rows.append(NewTaskRow())
            elif kind == "section":
                classes = "rail-section" if rows else "rail-section -first"
                rows.append(Static(str(payload), classes=classes))
            elif kind == "empty":
                rows.append(Static(str(payload), classes="rail-empty"))
            elif isinstance(payload, Session):
                rows.append(
                    SessionRow(
                        payload,
                        current=payload.id == current,
                        depth=depth,
                        width=width,
                    )
                )
        self.remove_children()
        self.mount_all(rows)

    @staticmethod
    def _mark(entry: tuple[str, str | Session, int], current: str) -> tuple[object, ...]:
        """一条表项里真正影响画面的那几个字段。"""
        kind, payload, depth = entry
        if kind == "new":
            return (kind,)
        if isinstance(payload, str):
            return (kind, payload)
        return (kind, payload.id, payload.title, payload.status, payload.id == current, depth)

    def on_resize(self) -> None:
        """宽度变了要重截标题：留着旧的那份会溢出或过早省略。"""
        self.refresh_sessions(
            with_children=self._with_children,
            new_task=self._new_task,
            list_title=self._list_title,
        )
