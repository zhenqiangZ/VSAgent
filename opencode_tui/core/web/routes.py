"""网页端的路由与 JSON 形状——**唯一真相**。

这份代码原先是 `tools/serve_web.py` 的全部内容。搬进来有两个原因：

1. **服务要进包**。`opencode_tui.spec` 的 `datas` 早先只有 `app.tcss`，
   `tools/` 不在冻结产物里，exe 上跑不到那个脚本（方案稿 §0.3）。
2. **形状只能一份**。命令行的 `serve_web.py` 与包内的后台服务走同一套接口，
   路由与字段名分成两份时，改一处忘一处会让页面上的字段悄悄少一个。
   两者都从这里导入（判据用注入验这一点：改这里，两边同时变）。

**章程（S2 起）**：查看只读 + 语料投递与管理。

- 会话与运行仍然是纯 GET，没有写入口。
- 语料那三处写入口**只做三件小事**：往 `corpus/inbox/` 落文件、写 `labels.json`
  的显示名、把目录挪进 `corpus/.trash/`。**网页端从不写索引、不建块、不改原件**——
  写者仍然只有 TUI / agent 一个（方案稿 §9）。索引这边只做 `mode=ro` 的只读打开，
  用来显示块数与向量覆盖。
- 只绑 `127.0.0.1`，不做鉴权。本地无鉴权 + 浏览器可跨站发 `multipart/form-data`
  是个真实的口子：所以所有写入口都要求一个**自定义头** `X-VSA-Request: 1`
  （跨站请求带上它就得先过预检，而预检会被拒），并校验 `Origin`。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import Any
from urllib.parse import unquote, urlsplit, urlparse

from .. import corpus, corpus_config, index
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

REQUEST_HEADER = "X-VSA-Request"
"""写入口要求的自定义头。**这不是形式主义**：`multipart/form-data` 属于 CORS 简单
请求，恶意页面不用预检就能往 `127.0.0.1` 发 POST；加一个自定义头之后，浏览器必须
先发 OPTIONS 预检，而这里不回 CORS 头，预检就过不去。"""

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
"""一次上传请求体的上限（含所有文件）。单文件 50MB 对文献够用；再多请分批。"""

MAX_JSON_BYTES = 64 * 1024
"""改名这类 JSON 请求体的上限。给得宽是因为标题可能很长，但没必要无上限。"""

UPLOAD_SUFFIXES = (".pdf", ".md", ".txt")
"""投递口收的扩展名。与 `parse.SUPPORTED_SUFFIXES` 同口径——这里先挡一道，
是为了让用户在网页上就看见「不支持」，而不是等入库时才失败。"""

LIBRARY_ITEM = re.compile(r"^(doc|web):([0-9a-f]{10})$")
"""`<kind>:<id>/<action>` 里那一段的身份。**路径由 id 拼，不接受用户给的路径。**"""


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
# 语料库（S2）
# ---------------------------------------------------------------------------
def archive_directory() -> Path:
    """查询归档的目录。真相在 `domain/rsearch.search_dir()`，这里只读它的位置。

    从 `core/web` 导入 `core/agent/domain` 会把依赖方向倒过来（网页端不该认识
    领域工具），所以这一处按同一个约定拼路径，注释指回那个函数。
    """
    return Path.home() / ".opencode_tui" / "search"


def library_payload(root: Path) -> dict:
    """语料库列表 + 待入库数 + 占用 + 开关现状。**只读，不动任何文件。**"""
    items: list[dict] = []
    for kind in ("doc", "web"):
        base = root / kind
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            item = _library_item(kind, entry)
            if item is not None:
                items.append(item)
    items.sort(key=lambda item: (-float(item["addedAt"] or 0), item["id"]))

    counts = _index_counts(root)
    for item in items:
        item["chunks"], item["vectors"] = counts.get(item["id"], (0, 0))

    inbox = _inbox_files(root)
    settings = corpus_config.load(root.parent)
    return {
        "items": items,
        "inbox": {"count": len(inbox), "names": [path.name for path in inbox[:20]]},
        "archive": {"count": len(list(archive_directory().glob("web-*.json")))},
        "stats": {
            "sources": len(items),
            "chunks": sum(item["chunks"] for item in items),
            "bytes": sum(item["size"] for item in items),
        },
        "config": {
            "indexWeb": settings.index_web,
            "embed": settings.embed,
            "embedModel": settings.embed_model,
            # 浏览器里只显示，不能改：改配置是写操作（方案稿 §10）。
            "source": settings.source,
        },
    }


def _library_item(kind: str, entry: Path) -> dict | None:
    """一份源在列表里的一行。读 `meta.json` 与 `labels.json`，**不读原件内容**。"""
    meta_path = entry / corpus.META_FILE
    try:
        meta = corpus.read_json(meta_path)
    except (OSError, ValueError):
        # 半截 meta（写到一半）不该让整页 500：列出来并标成「元数据缺失」。
        meta = {}
    labels: dict[str, Any] = {}
    try:
        labels = corpus.read_json(entry / corpus.LABELS_FILE)
    except (OSError, ValueError):
        labels = {}

    source_id = f"{kind}:{entry.name}"
    display = str(labels.get("display_title") or "").strip()
    size = 0
    for item in entry.rglob("*"):
        try:
            if item.is_file():
                size += item.stat().st_size
        except OSError:
            continue
    added = meta.get("added_at")
    if not isinstance(added, (int, float)):
        try:
            added = entry.stat().st_mtime
        except OSError:
            added = 0.0
    return {
        "id": source_id,
        "kind": kind,
        "ref": f"{kind}#{entry.name}",
        "title": display or str(meta.get("title") or entry.name),
        "displayTitle": display,
        "rawTitle": str(meta.get("title") or ""),
        "locator": str(meta.get("url") or meta.get("file") or ""),
        "pages": int(meta.get("pages") or 0),
        "structure": str(meta.get("structure") or ""),
        "depth": str(meta.get("depth") or ""),
        "chars": int(meta.get("chars") or 0),
        "size": size,
        "addedAt": float(added),
        "status": "ok" if meta else "bad-meta",
        # `chunks` / `vectors` 由 `library_payload` 从只读索引里补上。
        "chunks": 0,
        "vectors": 0,
    }


def _index_counts(root: Path) -> dict[str, tuple[int, int]]:
    """`source_id -> (块数, 向量数)`。索引不可读时给空表——列表照常显示。"""
    conn = index.open_readonly(root)
    if conn is None:
        return {}
    counts: dict[str, tuple[int, int]] = {}
    try:
        for row in conn.execute("SELECT source_id, COUNT(*) FROM chunks GROUP BY source_id"):
            counts[str(row[0])] = (int(row[1]), 0)
        for row in conn.execute(
            "SELECT c.source_id, COUNT(*) FROM vectors v JOIN chunks c ON c.id = v.chunk_id "
            "GROUP BY c.source_id"
        ):
            known = counts.get(str(row[0]), (0, 0))
            counts[str(row[0])] = (known[0], int(row[1]))
    except Exception:  # noqa: BLE001 - 读数缺了不该让页面挂掉
        return {}
    finally:
        conn.close()
    return counts


def _inbox_files(root: Path) -> list[Path]:
    inbox = root / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(
        item
        for item in inbox.iterdir()
        if item.is_file() and item.suffix.lower() in UPLOAD_SUFFIXES
    )


def _item_identity(rest: str, action: str) -> tuple[str, str]:
    """`doc:ab12cd34ef/rename` → `(doc, ab12cd34ef)`。

    后缀对不上就是 404：`/api/library/doc:xxx/whatever` 不该落到某个默认动作上。
    """
    suffix = f"/{action}"
    if not rest.endswith(suffix):
        raise HttpError(404, f"不认识的语料操作：{rest}")
    matched = LIBRARY_ITEM.match(rest[: -len(suffix)])
    if matched is None:
        raise HttpError(400, "语料标识要写成 doc:xxxxxxxxxx 或 web:xxxxxxxxxx")
    return matched.group(1), matched.group(2)


def _source_directory(root: Path, kind: str, source_id: str) -> Path:
    """按 id 拼目录。**只认 id**：用户给的路径连解释的机会都没有。

    用**这台服务自己的** `corpus_root` 拼，不调 `corpus.source_dir()`——那个函数
    走的是「按代码位置定位到的仓根」，服务被指到别的语料根（判据、`--corpus`）时
    两边会指向不同的目录，而列表读的是这一个、改名找的是那一个。
    """
    if not LIBRARY_ITEM.match(f"{kind}:{source_id}"):
        raise HttpError(400, "语料标识要写成 doc:xxxxxxxxxx 或 web:xxxxxxxxxx")
    return root / kind / source_id


def _guard_write(request: "WebHandler") -> None:
    """写入口的两道门：自定义头 + Origin。见 `REQUEST_HEADER` 的说明。"""
    if request.headers.get(REQUEST_HEADER) != "1":
        raise HttpError(
            403,
            f"写操作要求 {REQUEST_HEADER}: 1（跨站页面发不出这个头：会先被预检拦下）",
        )
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return
    host = (request.headers.get("Host") or "").strip()
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or parsed.netloc != host:
        raise HttpError(403, f"拒绝跨站请求（Origin: {origin}）")


def _read_body(request: "WebHandler", limit: int) -> bytes:
    """读请求体，**按实际读到的字节数卡**，不信 `Content-Length` 之外的任何东西。

    先看声明长度是为了快速拒绝（省得读 50MB 才发现超了）；真正落地的是循环里
    那个 `limit - total`：客户端说 100 却发 1MB，只会读到 100 字节，多出来的
    留在 socket 上，响应完连接就关了。
    """
    declared = request.headers.get("Content-Length")
    if declared is None:
        raise HttpError(411, "请求要带 Content-Length")
    try:
        length = int(declared)
    except ValueError as error:
        raise HttpError(400, f"Content-Length 不是数字：{declared!r}") from error
    if length < 0:
        raise HttpError(400, "Content-Length 不能是负数")
    if length > limit:
        raise HttpError(413, f"请求体超过上限：{length} > {limit} 字节")

    pieces: list[bytes] = []
    total = 0
    while total < length:
        piece = request.rfile.read(min(65536, length - total))
        if not piece:
            break
        pieces.append(piece)
        total += len(piece)
    if total != length:
        raise HttpError(400, f"请求体不完整：声明 {length} 字节，实际只收到 {total} 字节")
    return b"".join(pieces)


def _json_body(request: "WebHandler") -> dict:
    raw = _read_body(request, MAX_JSON_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as error:
        raise HttpError(400, f"请求体不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise HttpError(400, "请求体要是一个 JSON 对象")
    return payload


def _safe_filename(raw: str) -> str:
    """把上传的文件名压成一个能落盘的名字。

    - 只取 basename（`../../etc/passwd` 与 `C:\\\\x.pdf` 都只剩 `passwd` / `x.pdf`）；
    - 去掉控制字符（它们在多数文件系统上建不出来，报错会很难懂）；
    - 不以 `.` 开头（不覆盖隐藏文件，也不造出一个看不见的投递文件）；
    - 扩展名必须是 `.pdf/.md/.txt`。
    """
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").lstrip(".")
    if not name:
        raise HttpError(400, "文件名不能为空")
    stem, suffix = os.path.splitext(name)
    if suffix.lower() not in UPLOAD_SUFFIXES:
        raise HttpError(
            415,
            f"不收 {suffix or '（无扩展名）'}：只收 {' / '.join(UPLOAD_SUFFIXES)}。"
            "扫描版 PDF 需要 OCR，本轮不支持。",
        )
    return f"{stem[:100]}{suffix}"


def _unique_path(directory: Path, name: str) -> Path:
    """不覆盖同名文件：`paper.pdf` → `paper-1.pdf` → `paper-2.pdf`。"""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise HttpError(409, f"{name} 的同名文件已经太多了，先清理 inbox")


def _multipart_files(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """从 `multipart/form-data` 里取出所有带文件名的部分。

    自己解析而不是用 `cgi.FieldStorage`：那个模块在 3.11 起废弃、3.13 移除，
    而这里只需要「按 boundary 切开、看 Content-Disposition」这一件事。
    """
    boundary = ""
    for piece in (content_type or "").split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece[len("boundary="):].strip().strip('"')
    if not boundary:
        raise HttpError(400, "上传要用 multipart/form-data，并带 boundary")

    marker = b"--" + boundary.encode("latin-1", "replace")
    found: list[tuple[str, bytes]] = []
    for chunk in body.split(marker):
        if not chunk or chunk.startswith(b"--"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        filename = _disposition_filename(
            head.decode("utf-8", "replace")
        )
        if filename is not None:
            found.append((filename, data))
    return found


def _disposition_filename(headers: str) -> str | None:
    """`Content-Disposition` 里的文件名。`filename*=`（RFC 5987）优先。"""
    for line in headers.split("\r\n"):
        if not line.lower().startswith("content-disposition:"):
            continue
        disposition = line.split(":", 1)[1]
        for piece in disposition.split(";"):
            key, _, value = piece.strip().partition("=")
            if key.strip().lower() == "filename*":
                _, _, encoded = value.partition("''")
                return unquote(encoded.strip().strip('"'))
        for piece in disposition.split(";"):
            key, _, value = piece.strip().partition("=")
            if key.strip().lower() == "filename":
                return value.strip().strip('"')
    return None


def _library_upload(request: "WebHandler", _rest: str) -> dict:
    """投递口：文件落进 `corpus/inbox/`，**不写索引**。"""
    _guard_write(request)
    body = _read_body(request, request.server.max_upload)
    parts = _multipart_files(body, request.headers.get("Content-Type") or "")
    if not parts:
        raise HttpError(400, "这次上传里没有文件（form 字段名无所谓，但要有 filename）")

    inbox = request.server.corpus_root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for filename, data in parts:
        if not data:
            saved.append({"name": filename, "saved": False, "reason": "文件是空的"})
            continue
        try:
            name = _safe_filename(filename)
        except HttpError as error:
            saved.append({"name": filename, "saved": False, "reason": str(error)})
            continue
        target = _unique_path(inbox, name)
        corpus.atomic_write_bytes(target, data)
        saved.append({"name": target.name, "saved": True, "bytes": len(data)})

    accepted = [item for item in saved if item["saved"]]
    if not accepted:
        first = saved[0]["reason"] if saved else "没有可接收的文件"
        raise HttpError(415, first)
    return {
        "saved": saved,
        "accepted": len(accepted),
        # 入库是 TUI 那一侧的活：网页端只投递，不建块、不碰索引。
        "next": "文件已放进 corpus/inbox/。在 TUI 里调 import_documents 指向这个目录即可入库。",
    }


def _library_rename(request: "WebHandler", rest: str) -> dict:
    """只写 `labels.json`。**原件与索引一概不碰**：改个名字不该让块重建。"""
    _guard_write(request)
    kind, source_id = _item_identity(rest, "rename")
    directory = _source_directory(request.server.corpus_root, kind, source_id)
    if not (directory / corpus.META_FILE).is_file():
        raise HttpError(404, f"没有这一份：{kind}#{source_id}")
    payload = _json_body(request)
    raw = str(payload.get("title") or "")
    title = "".join(ch for ch in raw if ch >= " " and ch != "\x7f").strip()[:200]

    labels_path = directory / corpus.LABELS_FILE
    try:
        labels = corpus.read_json(labels_path)
    except (OSError, ValueError):
        labels = {}
    if title:
        labels["display_title"] = title
    else:
        # 空标题 = 取消改名，回到 meta 里那个原始标题。
        labels.pop("display_title", None)
    if labels:
        corpus.write_json(labels_path, labels)
    else:
        labels_path.unlink(missing_ok=True)

    try:
        meta = corpus.read_json(directory / corpus.META_FILE)
    except (OSError, ValueError):
        # 元数据坏了不该让「改名」这一步 500：显示名照样能写进 labels.json。
        meta = {}
    return {
        "id": f"{kind}:{source_id}",
        "title": title or str(meta.get("title") or source_id),
        "displayTitle": title,
    }


def _library_delete(request: "WebHandler", rest: str) -> dict:
    """挪进 `corpus/.trash/`，**幂等**：已经不在的照样回 200。

    不直接 `rmtree`：误删要能找回来，而且「挪走」是一个原子换名，正在重建索引的
    那一侧不会读到半份目录。
    """
    _guard_write(request)
    kind, source_id = _item_identity(rest, "delete")
    directory = _source_directory(request.server.corpus_root, kind, source_id)
    if not directory.is_dir():
        return {"id": f"{kind}:{source_id}", "deleted": False, "reason": "已经不在了"}

    trash = request.server.corpus_root / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / f"{kind}-{source_id}-{int(time.time())}"
    try:
        os.replace(directory, target)
    except OSError as error:
        raise HttpError(500, f"挪不进回收站：{error}") from error
    return {
        "id": f"{kind}:{source_id}",
        "deleted": True,
        "trash": str(target.relative_to(request.server.corpus_root)),
    }


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonRoute:
    """一个返回 JSON 的路由。

    `prefix` 为真时按前缀匹配，`handler` 收路径剩下的那一段；
    否则要求整段相等，`handler` 收不到参数。`method` 决定它接哪个动词——
    GET 与 POST 混在一张表里，是为了让「全部端点」仍然只有一处可读。
    """

    path: str
    handler: Callable[[Any, str], Any]
    prefix: bool = False
    method: str = "GET"

    def matches(self, path: str, method: str) -> bool:
        if self.method != method:
            return False
        if self.prefix:
            return path.startswith(self.path)
        return path == self.path


@dataclass(frozen=True)
class RouteSpec:
    """这台服务认识的全部路径。

    **这是路由的唯一真相**：命令行脚本与包内服务都按它分发，
    判据把 `json_routes` 换掉一个再请求一次，两边都应跟着变。
    """

    json_routes: tuple[JsonRoute, ...] = field(default_factory=tuple)

    def match(self, path: str, method: str = "GET") -> JsonRoute | None:
        """找出处理这条路径与动词的路由；静态文件与未知路径都返回 None。"""
        for route in self.json_routes:
            if route.matches(path, method):
                return route
        return None


def _session_list(request: "WebHandler", _rest: str) -> dict:
    """会话列表，按更新时间倒序（`list_sessions` 已有序）。"""
    manager = load_manager(request.server.state_path)
    runs = scan_runs(request.server.runs_dir)
    return {
        "sessions": [
            session_summary(session, run_id_for_session(runs, session.id))
            for session in manager.list_sessions()
        ]
    }


def _session_detail(request: "WebHandler", rest: str) -> dict:
    """单个会话的完整内容。"""
    manager = load_manager(request.server.state_path)
    session = manager.get_session(rest)
    if session is None:
        raise HttpError(404, f"会话不存在：{rest}")
    runs = scan_runs(request.server.runs_dir)
    return session_detail(session, run_id_for_session(runs, session.id))


def _run_list(request: "WebHandler", _rest: str) -> dict:
    """runs/ 下的运行清单。"""
    return {"runs": scan_runs(request.server.runs_dir)}


def _library(request: "WebHandler", _rest: str) -> dict:
    """语料库列表。只读：列表、待入库数、占用与开关现状。"""
    return library_payload(request.server.corpus_root)


def _library_item_action(request: "WebHandler", rest: str) -> dict:
    """`<kind>:<id>/rename` 与 `<kind>:<id>/delete` 的分发。

    两个动作的路径形状一样，所以共用一条前缀路由；认不出来的动作回 404，
    不落到某个默认动作上。
    """
    if rest.endswith("/rename"):
        return _library_rename(request, rest)
    if rest.endswith("/delete"):
        return _library_delete(request, rest)
    raise HttpError(404, f"不认识的语料操作：{rest}")


SPEC = RouteSpec(
    json_routes=(
        JsonRoute("/api/sessions/", _session_detail, prefix=True),
        JsonRoute("/api/sessions", _session_list),
        JsonRoute("/api/runs", _run_list),
        JsonRoute("/api/library", _library),
        JsonRoute("/api/library/upload", _library_upload, method="POST"),
        JsonRoute("/api/library/", _library_item_action, prefix=True, method="POST"),
    )
)
"""默认路由表。写入口只有三条，都要求 `X-VSA-Request` 头（见 `_guard_write`）。"""


# ---------------------------------------------------------------------------
# 服务器与处理器
# ---------------------------------------------------------------------------


class WebServer(ThreadingHTTPServer):
    """持有状态文件、运行目录、网页目录、语料根与路由表。"""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state_path: Path,
        runs_dir: Path,
        web_root: Path,
        spec: RouteSpec = SPEC,
        corpus_root: Path | None = None,
        max_upload: int = MAX_UPLOAD_BYTES,
    ) -> None:
        super().__init__(address, WebHandler)
        self.state_path = state_path
        self.runs_dir = runs_dir
        self.web_root = web_root
        self.spec = spec
        self.corpus_root = Path(corpus_root) if corpus_root is not None else corpus.corpus_root()
        self.max_upload = max_upload


class WebHandler(BaseHTTPRequestHandler):
    """只读接口、语料投递与管理、静态文件。"""

    server_version = "vsagent-web"
    server: WebServer

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            self.route(path, method)
        except HttpError as error:
            self.send_json({"error": str(error)}, error.status)
        except Exception as error:  # 一次请求出错不该把服务带下去
            self.send_json({"error": f"{type(error).__name__}: {error}"}, 500)

    def route(self, path: str, method: str = "GET") -> None:
        """按路径分发：先问路由表，再从运行目录取静态文件，最后是网页目录。

        静态文件只服务 GET：`POST /index.html` 没有意义，也不该被当成一次写。
        """
        matched = self.server.spec.match(path, method)
        if matched is not None:
            rest = path[len(matched.path):] if matched.prefix else ""
            self.send_json(matched.handler(self, rest))
            return
        if method != "GET":
            if self.server.spec.match(path, "GET") is not None:
                raise HttpError(405, f"{path} 不接受 {method}")
            raise HttpError(404, f"没有这条接口：{method} {path}")
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
