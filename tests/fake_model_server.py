from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODEL = "fake-model"


@dataclass
class Reply:
    """一次请求的应答。

    正常应答看 chunks 与 disconnect_after：写完第 disconnect_after 条分片就关掉连接，
    不给收尾的那一块，客户端读到的是"半截分块流"。status 不是 200 时只看 body。
    delay 是每写一条分片之前等的时间，用来模拟真实接口的流式节奏——窗口合并那类
    断言要看到多条增量在时间上真的分开到达。
    """

    status: int = 200
    chunks: list[dict[str, Any]] = field(default_factory=list)
    disconnect_after: int | None = None
    body: str = ""
    delay: float = 0.0


def delta_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """造一条流式分片，delta 里放哪些字段由参数决定。"""
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def usage_chunk(
    prompt_tokens: int,
    completion_tokens: int,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """造收尾那一条只带用量的分片，它的 choices 是空数组。"""
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def tool_call_delta(
    index: int,
    *,
    call_id: str = "",
    name: str = "",
    arguments: str = "",
) -> dict[str, Any]:
    """造一条工具调用分片。id 与 name 只在第一段给出，其余段只带 arguments。"""
    function: dict[str, Any] = {}
    if name:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments
    entry: dict[str, Any] = {"index": index, "function": function}
    if call_id:
        entry["id"] = call_id
        entry["type"] = "function"
    return entry


def text_chunks(text: str, size: int = 4) -> list[dict[str, Any]]:
    """把一段文字切成若干条文本增量分片。"""
    return [delta_chunk(content=text[start : start + size]) for start in range(0, len(text), size)]


def reasoning_chunks(text: str, size: int = 4) -> list[dict[str, Any]]:
    """把一段思考文字切成若干条推理增量分片。"""
    return [
        delta_chunk(reasoning=text[start : start + size]) for start in range(0, len(text), size)
    ]


def argument_fragments(arguments: dict[str, Any], pieces: int = 3) -> list[str]:
    """把工具参数序列化后切成给定段数，模拟 arguments 横跨多个分片到达。"""
    text = json.dumps(arguments, ensure_ascii=False)
    step = -(-len(text) // pieces)
    return [text[start : start + step] for start in range(0, len(text), step)]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.replies: list[Reply] = []
        self.requests: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    """只认 POST /chat/completions 的处理函数。

    正常应答走分块传输，和真实服务一致。这样"流写到一半关掉连接"对客户端就是
    一个半截的分块流，读到的是协议错误；若用长度已知的应答，提前关闭只会被当成正常收尾。
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else {}
        server = self.server
        assert isinstance(server, _Server)
        server.requests.append(body)
        reply = server.replies.pop(0) if server.replies else Reply()
        if reply.status != 200:
            self._write_error(reply)
            return
        self._write_stream(reply)

    def _write_error(self, reply: Reply) -> None:
        payload = reply.body.encode("utf-8")
        self.send_response(reply.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _write_stream(self, reply: Reply) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        for index, chunk in enumerate(reply.chunks):
            if reply.disconnect_after is not None and index >= reply.disconnect_after:
                # 不给收尾的 0 长度分块，直接关掉连接
                self.close_connection = True
                return
            if reply.delay:
                time.sleep(reply.delay)
            self._write_block(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        if reply.disconnect_after is None:
            self._write_block(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        self.close_connection = True

    def _write_block(self, payload: bytes) -> None:
        """按分块传输的格式写出一块。"""
        self.wfile.write(f"{len(payload):x}\r\n".encode("ascii"))
        self.wfile.write(payload + b"\r\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        """不往标准错误里写访问日志。"""


class FakeModelServer:
    """回环地址上的假模型服务，按排入的脚本返回 OpenAI 兼容的 SSE。

    请求体与应答都留得住，测试可以直接读它来断言循环层真的把工具清单与历史发了出去。
    """

    def __init__(self) -> None:
        self._server = _Server(("127.0.0.1", 0))
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """服务的根地址，与真实接口一样以 /v1 结尾。"""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        """在后台线程里开始接收请求。"""
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止服务并等后台线程结束。"""
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def script(self, *replies: Reply) -> None:
        """排入接下来几次请求的应答，按到达顺序取用。"""
        self._server.replies.extend(replies)

    @property
    def requests(self) -> list[dict[str, Any]]:
        """已经收到的请求体，按到达顺序。"""
        return self._server.requests

    def clear(self) -> None:
        """清空排入的应答与已收请求。"""
        self._server.replies.clear()
        self._server.requests.clear()
