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

    生产环境的状态文件在 `~/.vsagent/state.toml`，日志于是写到
    `~/.vsagent/logs/`；测试里状态文件在项目内，日志跟着它走，
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
        # 写入缓冲。一次模型往返里有上百条 `part.update`（每个文本增量一条），
        # 每条都刷盘等于把一次对话写成几万次系统调用。留 200 毫秒的缓冲：
        # 进程真崩了，丢的也只是最后 200 毫秒，而崩溃前那一段仍然在文件里。
        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._last_flush = time.monotonic()
        self.flush_interval = 0.2
        self.max_buffer = 64 * 1024
        # `part.update` 按部件采样：同一个部件每 250 毫秒最多一行。
        # 它的负载只有三个编号，正文在状态文件里，中间帧对回放没有信息。
        self.update_interval = 0.25
        self._last_update: dict[str, float] = {}

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
        """当前会话的日志文件路径。取路径的人通常马上要读，先刷一次缓冲。"""
        self._flush()
        return self.directory / f"{self.session_id()}.jsonl"

    def _write(self, payload: dict[str, Any]) -> None:
        """把一条事件放进缓冲，按时间或大小刷出去。

        `part.update` 单独采样：同一个部件 250 毫秒内只留第一行。它不带正文，
        只说明「这一步还在动」；正文与工具结果都在状态文件里，少几帧不影响
        回放与成本分析，而一次 18 步的往返能因此从一万多行降到几百行。
        """
        event = str(payload.get("type", ""))
        if event == "part.update":
            key = str(payload.get("part_id", ""))
            now = time.monotonic()
            last = self._last_update.get(key, 0.0)
            if key and now - last < self.update_interval:
                return
            self._last_update[key] = now
        body = {key: value for key, value in payload.items() if key != "type"}
        line = json.dumps(
            {"at": time.time(), "event": event, "payload": body},
            ensure_ascii=False,
        )
        # 会话换了就先把旧缓冲写完再换句柄，避免把上一条会话的行写进新文件。
        path = self.directory / f"{self.session_id()}.jsonl"
        if self._handle_path is not None and self._handle_path != path:
            self._flush()
        self._open()
        self._buffer.append(f"{line}\n")
        self._buffer_chars += len(line) + 1
        if (
            self._buffer_chars >= self.max_buffer
            or time.monotonic() - self._last_flush >= self.flush_interval
        ):
            self._flush()

    def _flush(self) -> None:
        """把缓冲里的行写进当前文件并刷盘。

        只写已有句柄，不在这里切换文件：切换由 `_open` 负责，切换前会先调
        本函数把旧会话的尾部写完。没有句柄时保留缓冲，等 `_open` 建好再写。
        """
        if not self._buffer or self._handle is None:
            return
        self._handle.write("".join(self._buffer))
        self._handle.flush()
        self._buffer.clear()
        self._buffer_chars = 0
        self._last_flush = time.monotonic()

    def _open(self) -> TextIO:
        """取当前会话的文件句柄，会话换了就换一个。"""
        path = self.current_path()
        if self._handle is None or self._handle_path != path:
            self._close()
            self._handle = path.open("a", encoding="utf-8")
            self._handle_path = path
        return self._handle

    def _close(self) -> None:
        """把缓冲写完再关掉当前的文件句柄。"""
        self._flush()
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._handle_path = None
