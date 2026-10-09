from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from ..event_bus import EventBus

# 日志子目录，与状态文件同在一个目录下
LOG_DIRECTORY = "logs"


def log_directory_for(state_path: Path) -> Path:
    """按状态文件的位置给出日志目录。

    生产环境的状态文件在 `~/.opencode_tui/state.toml`，日志于是写到
    `~/.opencode_tui/logs/`；测试里状态文件在项目内，日志跟着它走，
    不会写进用户目录。
    """
    return state_path.parent / LOG_DIRECTORY


class TraceWriter:
    """把事件总线上的每一条事件追加成一行 JSON。

    订阅通配事件，循环层新加事件时这一处不用跟着改。
    日志按会话分开，写到当前会话的那一个：事件负载里只有 id，
    会话要从外面问，所以这里持有一个取值函数。

    判定结果与钩子结果都走同一条通道，这是日志的主要用途：
    权限为什么不放行、钩子为什么拦下，看这一份就知道。
    """

    def __init__(
        self,
        bus: EventBus,
        session_id: Callable[[], str],
        directory: Path,
    ) -> None:
        self.bus = bus
        self.session_id = session_id
        self.directory = directory
        self._handle: TextIO | None = None
        self._handle_path: Path | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def start(self) -> None:
        """建出日志目录并开始记录。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        self._unsubscribe = self.bus.subscribe("*", self._write)

    def stop(self) -> None:
        """停止记录并关掉文件。"""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._close()

    def current_path(self) -> Path:
        """当前会话的日志文件路径。"""
        return self.directory / f"{self.session_id()}.jsonl"

    def _write(self, payload: dict[str, Any]) -> None:
        """把一条事件写成一行。

        每条写完就刷出去：这个文件要看的正是应用出错之前的那一段，
        留在缓冲区里的内容在进程结束时就没了。
        """
        event = str(payload.get("type", ""))
        body = {key: value for key, value in payload.items() if key != "type"}
        line = json.dumps(
            {"at": time.time(), "event": event, "payload": body},
            ensure_ascii=False,
        )
        handle = self._open()
        handle.write(f"{line}\n")
        handle.flush()

    def _open(self) -> TextIO:
        """取当前会话的文件句柄，会话换了就换一个。"""
        path = self.current_path()
        if self._handle is None or self._handle_path != path:
            self._close()
            self._handle = path.open("a", encoding="utf-8")
            self._handle_path = path
        return self._handle

    def _close(self) -> None:
        """关掉当前的文件句柄。"""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._handle_path = None
