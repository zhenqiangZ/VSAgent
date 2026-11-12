"""分栏界线：拖动或按左右键改相邻那一栏的宽度。

它只发消息、不改布局。宽度存在界面那一层，由界面决定夹到哪一档、什么时候写进
状态文件；界线自己也记一份的话，拖过几次之后界线两侧就会对不上。
"""

from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.widgets import Static

from .. import palette


class Splitter(Static):
    """1 列宽的分栏界线，左右拖动或按键调整宽度。

    `sign` 决定方向：界线左边的栏（会话导航）往右拖是加宽，界线右边的栏
    （上下文侧栏）往右拖是收窄。两种方向都在这里换算成界面认识的列数。
    """

    # 界线是 1 列宽的窄条，鼠标能点中；键盘用户按方向键同样能调。
    can_focus = True
    BINDINGS = [
        Binding("left", "resize(-1)", "变窄", show=False),
        Binding("right", "resize(1)", "变宽", show=False),
    ]

    DEFAULT_CSS = palette.css("""
    Splitter {
        width: 1;
        height: 100%;
        /* 默认是一条很淡的灰线：界线要看得见，但不该比正文更抢眼。 */
        background: $vsa-line-soft;
    }
    /* 悬停与聚焦都只抬一档灰，不用强调色：这一条能拖动，点亮到看得见就够。 */
    Splitter:hover {
        background: $vsa-line;
    }
    Splitter:focus {
        background: $vsa-line;
    }
    """)

    class ResizeRequested(TextualMessage):
        """界线动了一格：`delta` 是屏幕列数，正数往右。"""

        def __init__(self, splitter: "Splitter", delta: int) -> None:
            super().__init__()
            self.splitter = splitter
            self.delta = delta

    class ResizeFinished(TextualMessage):
        """拖动结束或按键调整完成：这时候才把宽度写进状态文件。"""

        def __init__(self, splitter: "Splitter") -> None:
            super().__init__()
            self.splitter = splitter

    def __init__(self, *, sign: int = 1, id: str | None = None) -> None:
        super().__init__(id=id)
        self._sign = sign
        self._dragging = False
        self._last_x = 0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """按下开始拖：接住后续的移动事件，焦点也留在这里。"""
        event.stop()
        self.focus()
        self._dragging = True
        self._last_x = int(event.screen_x)
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """拖动时按位移改宽度。

        位移取相邻两次事件的差，而不是「按下位置到当前位置」的总差：总差会在
        宽度被夹住之后继续累积，往回拖几格才开始动。
        """
        if not self._dragging:
            return
        event.stop()
        x = int(event.screen_x)
        delta = x - self._last_x
        if not delta:
            return
        self._last_x = x
        self.post_message(self.ResizeRequested(self, delta * self._sign))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """松手结束拖动，把宽度写进状态文件。"""
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        self.release_mouse()
        self.post_message(self.ResizeFinished(self))

    def action_resize(self, delta: int) -> None:
        """左右键调一格：改完立刻写进状态文件，不需要等松手。"""
        self.post_message(self.ResizeRequested(self, delta * self._sign))
        self.post_message(self.ResizeFinished(self))
