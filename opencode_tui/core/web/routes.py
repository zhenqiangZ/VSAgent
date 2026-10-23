"""网页端只读服务的路由与 JSON 形状——**唯一真相**。

这份代码原先是 `tools/serve_web.py` 的全部内容。搬进来有两个原因：

1. **服务要进包**。`opencode_tui.spec` 的 `datas` 早先只有 `app.tcss`，
   `tools/` 不在冻结产物里，exe 上跑不到那个脚本（方案稿 §0.3）。
2. **形状只能一份**。命令行的 `serve_web.py` 与包内的后台服务走同一套接口，
   路由与字段名分成两份时，改一处忘一处会让页面上的字段悄悄少一个。
   两者都从这里导入（判据用注入验这一点：改这里，两边同时变）。

**只读**：全部路由都是 GET，没有写入口。只绑 `127.0.0.1`，不做鉴权。
"""

from __future__ import annotations

import json
import mimetypes
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import Any
from urllib.parse import unquote, urlparse

from ..session_manager import SessionManager

# 状态文件是原子换名写出的（`SessionManager.save_to_toml`）。但读的人仍可能
# 卡在「临时文件已写好、换名还没做」的空窗里读到上一版，或者遇到别的进程
# 用别的方式写（判据的 `--write-loop` 就是直接重写）。这里的做法是等文件
# 安静 READ_QUIET_GAP 这么久才去读，读到之后连读两次、内容一致才认，
# 并且解析的就是这份校验过的字节。
#
# 连读两次不是多余的：截在某张表结束时那份内容**仍然是合法 TOML**，
# 只靠解析成功判断不出完整与否。
READ_QUIET_GAP = 0.04
READ_STABLE_GAP = 0.002
READ_DEADLINE = 1.5
READ_POLL_DELAY = 0.01

TEXT_SUFFIXES = (".js", ".json", ".css", ".html", ".svg", ".map")


class HttpError(Exception):
    """带状态码的请求错误。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 状态文件的等待读取
# ---------------------------------------------------------------------------


def read_settled(state_path: Path) -> SessionManager | None:
    """读一份已经安静下来的内容，校验不通过时返回 None。

    连读两次、内容一致，并且这份字节能解析成 TOML，才算读成。空内容与
    解析失败都当作没读成。
    """
    try:
        first = state_path.read_bytes()
        time.sleep(READ_STABLE_GAP)
        second = state_path.read_bytes()
    except OSError:
        return None

    if not first or first != second:
        return None

    try:
        state = tomllib.loads(first.decode("utf-8"))
    except (TOMLDecodeError, UnicodeDecodeError):
        return None

    manager = SessionManager(state_path)
    manager.load_from_state(state)
    return manager


def load_manager(state_path: Path) -> SessionManager:
    """读取状态文件，碰到写了一半的内容时重试。

    文件正被写入时不读，等它安静 READ_QUIET_GAP 这么久再读，这样不会把
    写了一半的内容当成完整结果。到 READ_DEADLINE 还没拿到稳定内容就报错。
    """
    deadline = time.monotonic() + READ_DEADLINE
    last_mtime: int | None = None
    settled_at = time.monotonic()
    note = "状态文件不可读"

    while True:
        try:
            stamp = state_path.stat().st_mtime_ns
        except OSError:
            note = "状态文件不可读"
        else:
            if stamp != last_mtime:
                last_mtime = stamp
                settled_at = time.monotonic()
            elif time.monotonic() - settled_at >= READ_QUIET_GAP:
                manager = read_settled(state_path)
                if manager is not None:
                    return manager
                note = "读到的内容不完整"
                settled_at = time.monotonic()

        if time.monotonic() >= deadline:
            raise HttpError(
                500,
                f"状态文件在 {READ_DEADLINE} 秒内没有读到稳定内容：{state_path}（{note}）",
            )
        time.sleep(READ_POLL_DELAY)


# ---------------------------------------------------------------------------
# JSON 形状
# ---------------------------------------------------------------------------


def part_json(part) -> dict:
    """一个内容单元。全部字段都带上，页面按类型取用。"""
    return {
        "id": part.id,
        "type": part.type.value,
        "text": part.text,
        "toolName": part.tool_name,
        "toolStatus": part.tool_status.value if part.tool_status else "",
        "toolInput": part.tool_input,
        "toolOutput": part.tool_output,
        "filePath": part.file_path,
        "agentName": part.agent_name,
        "permission": part.permission,
        "patchAdded": part.patch_added,
        "patchRemoved": part.patch_removed,
        "timestamp": part.timestamp,
    }


def message_json(message) -> dict:
    """一条消息。"""
    return {
        "id": message.id,
        "role": message.role,
        "createdAt": message.created_at,
        "modelId": message.model_id,
        "providerId": message.provider_id,
        "cost": message.cost,
        "tokens": message.tokens,
        "parts": [part_json(part) for part in message.parts],
    }


def session_summary(session, run_id: str | None) -> dict:
    """会话列表里的一项。"""
    return {
        "id": session.id,
        "title": session.title,
        "messageCount": len(session.messages),
        "partCount": sum(len(message.parts) for message in session.messages),
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "modelId": session.model_id,
        "agentName": session.agent_name,
        "totalTokens": session.total_tokens(),
        "totalCost": session.total_cost(),
        "runId": run_id,
        # 子 agent：网页端只读展示，靠这几个字段决定缩进与状态点。
        "parentId": session.parent_id,
        "depth": session.depth,
        "origin": session.origin,
        "status": session.status,
        "statusNote": session.status_note,
        "seedStart": session.seed_start,
        "seedEnd": session.seed_end,
    }


def session_detail(session, run_id: str | None) -> dict:
    """单个会话的完整内容。"""
    data = session_summary(session, run_id)
    data["effort"] = session.effort
    data["compactUpto"] = session.compact_upto
    data["messages"] = [message_json(message) for message in session.messages]
    return data


def scan_runs(runs_dir: Path) -> list[dict]:
    """列出 runs/ 下的运行。

    标识取目录名，路由与静态文件都按它拼路径。
    meta.json 里的 run_id 只作为展示字段返回——两者不一致时（判据里有一份
    刻意改过 run_id 的样），能取到文件的仍是目录名那一条。
    """
    found: list[dict] = []
    if not runs_dir.is_dir():
        return found
    for entry in sorted(runs_dir.iterdir()):
        meta_path = entry / "meta.json"
        if not entry.is_dir() or not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 一份坏 meta 不该让整个运行清单 500：跳过它，其余照常列出。
            continue
        provenance = meta.get("provenance") or {}
        target = meta.get("target") or {}
        found.append(
            {
                "id": entry.name,
                "runId": meta.get("run_id", ""),
                "name": target.get("name", ""),
                "pdbId": target.get("pdb_id", ""),
                "pocketCount": len(meta.get("pockets") or []),
                "moleculeCount": len(meta.get("molecules") or []),
                "sessionId": provenance.get("session_id"),
            }
        )
    return found


def run_id_for_session(runs: list[dict], session_id: str) -> str | None:
    """按 provenance.session_id 反查这个会话产出的运行。"""
    for item in runs:
        if item["sessionId"] == session_id:
            return item["id"]
    return None


def content_type_for(path: Path) -> str:
    """按扩展名给出 Content-Type。"""
    guessed = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if guessed.startswith("text/") or path.suffix in TEXT_SUFFIXES:
        return f"{guessed}; charset=utf-8"
    return guessed


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonRoute:
    """一个返回 JSON 的路由。

    `prefix` 为真时按前缀匹配，`handler` 收路径剩下的那一段；
    否则要求整段相等，`handler` 收不到参数。
    """

    path: str
    handler: Callable[[Any, str], Any]
    prefix: bool = False


@dataclass(frozen=True)
class RouteSpec:
    """这台服务认识的全部路径。

    **这是路由的唯一真相**：命令行脚本与包内服务都按它分发，
    判据把 `json_routes` 换掉一个再请求一次，两边都应跟着变。
    """

    json_routes: tuple[JsonRoute, ...] = field(default_factory=tuple)

    def match(self, path: str) -> JsonRoute | None:
        """找出处理这条路径的路由；静态文件与未知路径都返回 None。"""
        for route in self.json_routes:
            if route.prefix:
                if path.startswith(route.path):
                    return route
            elif path == route.path:
                return route
        return None


def _session_list(server, _rest: str) -> dict:
    """会话列表，按更新时间倒序（`list_sessions` 已有序）。"""
    manager = load_manager(server.state_path)
    runs = scan_runs(server.runs_dir)
    return {
        "sessions": [
            session_summary(session, run_id_for_session(runs, session.id))
            for session in manager.list_sessions()
        ]
    }


def _session_detail(server, rest: str) -> dict:
    """单个会话的完整内容。"""
    manager = load_manager(server.state_path)
    session = manager.get_session(rest)
    if session is None:
        raise HttpError(404, f"会话不存在：{rest}")
    runs = scan_runs(server.runs_dir)
    return session_detail(session, run_id_for_session(runs, session.id))


def _run_list(server, _rest: str) -> dict:
    """runs/ 下的运行清单。"""
    return {"runs": scan_runs(server.runs_dir)}


SPEC = RouteSpec(
    json_routes=(
        JsonRoute("/api/sessions/", _session_detail, prefix=True),
        JsonRoute("/api/sessions", _session_list),
        JsonRoute("/api/runs", _run_list),
    )
)
"""默认路由表。顺序有意义：前缀路由排在相等路由之后或之前都不冲突，
但 `/api/sessions/xxx` 必须先于静态文件那一支。"""


# ---------------------------------------------------------------------------
# 服务器与处理器
# ---------------------------------------------------------------------------


class WebServer(ThreadingHTTPServer):
    """持有状态文件、运行目录、网页目录与路由表。"""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state_path: Path,
        runs_dir: Path,
        web_root: Path,
        spec: RouteSpec = SPEC,
    ) -> None:
        super().__init__(address, WebHandler)
        self.state_path = state_path
        self.runs_dir = runs_dir
        self.web_root = web_root
        self.spec = spec


class WebHandler(BaseHTTPRequestHandler):
    """只读接口与静态文件。"""

    server_version = "vsagent-web"
    server: WebServer

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            self.route(path)
        except HttpError as error:
            self.send_json({"error": str(error)}, error.status)
        except Exception as error:  # 一次请求出错不该把服务带下去
            self.send_json({"error": f"{type(error).__name__}: {error}"}, 500)

    def route(self, path: str) -> None:
        """按路径分发：先问路由表，再从运行目录取静态文件，最后是网页目录。"""
        matched = self.server.spec.match(path)
        if matched is not None:
            rest = path[len(matched.path):] if matched.prefix else ""
            self.send_json(matched.handler(self.server, rest))
            return
        if path.startswith("/runs/"):
            self.send_from(self.server.runs_dir, path[len("/runs/"):])
            return
        self.send_from(self.server.web_root, path.lstrip("/") or "index.html")

    def resolve_within(self, root: Path, relative: str) -> Path:
        """把相对路径拼到根目录下，超出根目录直接拒绝。"""
        base = root.resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise HttpError(403, f"路径超出允许的范围：{relative}")
        return target

    def send_from(self, root: Path, relative: str) -> None:
        """发送根目录下的一个文件。"""
        target = self.resolve_within(root, relative)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise HttpError(404, f"文件不存在：{relative}")
        self.send_body(target.read_bytes(), content_type_for(target))

    def send_json(self, payload, status: int = 200) -> None:
        """发送 JSON。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_body(self, body: bytes, content_type: str) -> None:
        """发送字节内容。"""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """请求日志照旧写到 stderr。

        判据把子进程的输出重定向到文件（写进管道而不读会填满缓冲区卡住
        服务工作线程），所以这里保持默认行为，只显式写出来让人知道有这一处。
        """
        super().log_message(format, *args)
