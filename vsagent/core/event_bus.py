from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], None]


class EventBus:
    """进程内事件总线，模拟后端 SSE 事件流。

    本轮只做进程内发布订阅，后端接入时只需把远端事件转发到 publish 即可。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """订阅事件，返回取消订阅的函数。"""
        self._subscribers[event_type].append(handler)

        def unsubscribe() -> None:
            self.unsubscribe(event_type, handler)

        return unsubscribe

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """取消订阅某个事件的某个处理函数。"""
        handlers = self._subscribers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> int:
        """发布事件，返回被调用的处理函数数量（含通配订阅者）。"""
        payload = data or {}
        called = 0
        for handler in list(self._subscribers.get(event_type, ())):
            handler(payload)
            called += 1
        for handler in list(self._subscribers.get("*", ())):
            handler({"type": event_type, **payload})
            called += 1
        return called

    def clear(self) -> None:
        """清空全部订阅者。"""
        self._subscribers.clear()
