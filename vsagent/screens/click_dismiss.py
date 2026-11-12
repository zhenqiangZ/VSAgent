"""模态弹窗的点击关闭基类。

这一层让所有弹窗都能通过点击弹窗本体之外的半透明遮罩关闭。键盘上的
esc、enter、q 等原有键位仍然保留，不使用鼠标时照样能关闭。

关闭时返回的结果由 `cancel_result()` 决定，默认返回空值。带返回值的弹窗
（权限询问、接口表单、会话选择器）必须覆盖它，保证「点击空白处」与「按
取消键」得到完全相同的结果，不会把工具调用误放行。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from textual import events
from textual.errors import NoWidget
from textual.screen import ModalScreen
from textual.widget import Widget

ResultT = TypeVar("ResultT")

# 弹窗本体统一挂的类。点击落在带这个类的组件之内时说明点的是弹窗内容，
# 落在外面就是半透明遮罩。
BODY_CLASS = "modal-body"


class ClickDismissModal(ModalScreen[ResultT], Generic[ResultT]):
    """点击弹窗之外的空白处即可关闭的模态弹窗。"""

    # 点击空白处时返回的结果，默认是与「按取消键」一致的空值。带返回值的
    # 弹窗在这里写明取消值，保证点空白与按取消键的结果完全相同。
    CANCEL_RESULT: ResultT | None = None

    def cancel_result(self) -> ResultT | None:
        """点击空白处时返回的结果。"""
        return self.CANCEL_RESULT

    def on_click(self, event: events.Click) -> None:
        """点在遮罩上就关闭，点在弹窗本体之内保持打开。

        鼠标下方的组件要用屏幕坐标去查：冒泡到屏幕上的 `Click` 事件里，
        `event.widget` 指向的是屏幕自己，分不出点在遮罩还是点在弹窗里。
        点击会沿组件树冒泡，关闭之后同一个事件可能再被送到这一层，所以
        先确认自己仍在栈顶，已经退下来的直接忽略。
        """
        if self.app.screen is not self:
            return
        try:
            widget, _ = self.app.get_widget_at(event.screen_x, event.screen_y)
        except NoWidget:
            widget = None
        if widget is not None:
            for node in widget.ancestors_with_self:
                if isinstance(node, Widget) and node.has_class(BODY_CLASS):
                    return
        self.dismiss(self.cancel_result())
