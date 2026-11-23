from __future__ import annotations

from textual import events
from textual.screen import Screen


class ForwardGuard:
    """鼠标按下时跳过已经不在组件树上的命中。

    `Widget.remove()` 只把 Prune 投进队列；真正从父节点摘下、把 `parent` 置空
    发生在之后的某一帧，而合成器的命中地图要等下一次重排才更新。这一小段
    窗口里按下鼠标，命中的是旧地图里已经摘掉的组件。Textual 8.2.8 的
    `Screen._forward_event`（screen.py:1914）把 `content_widget.parent` 直接
    当容器读 `region`，这里会抛 `AttributeError`。消息区在流式输出时不停补挂
    与裁剪，左栏列表、最近任务与提示条也会整段重建，都会造出这种窗口。

    命中组件已经不接收任何事件，这一次按下本来就没有接收者：丢掉它，
    下一帧重排之后点击会命中新组件。
    """

    def _forward_event(self, event: events.Event) -> None:
        if isinstance(event, events.MouseDown) and not event.is_forwarded:
            widget, _offset = self.get_widget_and_offset_at(event.x, event.y)
            if (
                widget is not None
                and widget.parent is None
                and not isinstance(widget, Screen)
            ):
                return
        super()._forward_event(event)
