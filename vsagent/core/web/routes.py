"""网页端的路由与 JSON 形状——**唯一真相**。

这份代码原先是 `tools/serve_web.py` 的全部内容。搬进来有两个原因：

1. **服务要进包**。`vsagent.spec` 的 `datas` 早先只有 `app.tcss`，
   `tools/` 不在冻结产物里，exe 上跑不到那个脚本（方案稿 §0.3）。
2. **形状只能一份**。命令行的 `serve_web.py` 与包内的后台服务走同一套接口，
   路由与字段名分成两份时，改一处忘一处会让页面上的字段悄悄少一个。
   两者都从这里导入（判据用注入验这一点：改这里，两边同时变）。

**章程（S2 起）**：查看只读 + 语料投递与管理 + 提交任务。

- 会话与运行仍然是纯 GET。
- 语料那三处写入口**只做三件小事**：往 `corpus/inbox/` 落文件、写 `labels.json`
  的显示名、把目录挪进 `corpus/.trash/`。**网页端从不写索引、不建块、不改原件**——
  写者仍然只有 TUI / agent 一个（方案稿 §9）。索引这边只做 `mode=ro` 的只读打开，
  用来显示块数与向量覆盖。
- 提交任务（`POST /api/tasks`）同样只是转交：真正的写入在应用进程的事件循环里做，
  这个进程连状态文件都不碰。独立运行时没有转交对象，回 503。
- 只绑 `127.0.0.1`，不做鉴权。本地无鉴权 + 浏览器可跨站发 `multipart/form-data`
  是个真实的口子：所以所有写入口都要求一个**自定义头** `X-VSA-Request: 1`
  （跨站请求带上它就得先过预检，而预检会被拒），并校验 `Origin`。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlparse

from .. import corpus, knowledge_refs, live
from ..agent.report import build as build_report
from ..agent.report import read_trace
from ..agent.trace import log_directory_for
from ..paths import SURVEY_DIRECTORY, config_home, project_root
from ..session_manager import SessionManager
from .jobs import JobStore
from .services import evaluation as evaluation_service
from .services import library as library_service
from .services import memory as memory_service
from .services import search as search_service

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

CHUNK_PAGE = library_service.CHUNK_PAGE
"""切块列表一页多少块。取值与说明在 `services/library.py`。"""

CHUNK_PAGE_MAX = library_service.CHUNK_PAGE_MAX
"""一页最多给多少块。取值与说明在 `services/library.py`。"""

MAX_JSON_BYTES = 64 * 1024
"""改名这类 JSON 请求体的上限。给得宽是因为标题可能很长，但没必要无上限。"""

UPLOAD_SUFFIXES = library_service.UPLOAD_SUFFIXES
"""投递口收的扩展名。与 `parse.SUPPORTED_SUFFIXES` 同口径。"""

LIBRARY_ITEM = re.compile(r"^(doc|web):([0-9a-f]{10})$")
"""`<kind>:<id>/<action>` 里那一段的身份。**路径由 id 拼，不接受用户给的路径。**"""

MAX_TASK_CHARS = 8000
"""一次提交的任务正文上限。终端里粘一整篇文献也就几万字，这里给到 8000 字；
再长的交给 `import_documents`，不该从任务框里塞进去。"""

MAX_TAGS = 10
"""一份语料最多几个标签。标签是用来筛的，攒到几十个时筛选栏自己就成了一堵墙。"""

MAX_TAG_CHARS = 40
"""单个标签的字数上限。"""

MAX_BATCH_DELETE = 50
"""一次批量删除最多几份。再多就分几次：一次挪五十个目录已经够慢。"""

FOLDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
"""文件夹编号的形状。它只用来在 `labels.json` 里指认，不进路径。"""

LEDGER_DIRECTORY = "ledgers"
"""登记表快照所在目录，与 `ledger.ledger_root()` 同一个名字。"""

LIVE_SESSION = re.compile(r"^[A-Za-z0-9_-]+$")
"""实时快照的文件名用会话编号拼，先按字符集挡一道路径穿越。"""


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


def message_json(message, index: int | None = None) -> dict:
    """一条消息。

    `index` 是它在会话里的位置。分段取回来的消息只带一段单元，页面要按位置把
    它对应到步流水上的 `messageIndex`，所以这个数必须随消息一起给。
    """
    return {
        "id": message.id,
        "index": index,
        "role": message.role,
        "createdAt": message.created_at,
        "modelId": message.model_id,
        "providerId": message.provider_id,
        "cost": message.cost,
        "tokens": message.tokens,
        "parts": [part_json(part) for part in message.parts],
    }


def session_summary(
    session,
    run_id: str | None,
    *,
    artifact_count: int = 0,
    last_event: dict | None = None,
) -> dict:
    """会话列表里的一项。

    `artifact_count` 与 `last_event` 由调用方算好再传进来：列表接口要为每个会话
    读一份登记表，这一层不做 IO。
    """
    return {
        "id": session.id,
        "title": session.title,
        "messageCount": len(session.messages),
        # 左栏要在不取正文的前提下显示「这个任务分成了几段」，所以条数随列表一起给。
        "eventCount": len(session.events),
        "partCount": sum(len(message.parts) for message in session.messages),
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "modelId": session.model_id,
        "agentName": session.agent_name,
        "totalTokens": session.total_tokens(),
        "totalCost": session.total_cost(),
        "runId": run_id,
        # 首页的最近任务列表要显示「几个产物」与「最近一个主题」，两样都在这里给，
        # 免得页面为每一行再发一次请求。
        "artifactCount": artifact_count,
        "lastEvent": last_event,
        # 子 agent：网页端只读展示，靠这几个字段决定缩进与状态点。
        "parentId": session.parent_id,
        "depth": session.depth,
        "origin": session.origin,
        "status": session.status,
        "statusNote": session.status_note,
        "seedStart": session.seed_start,
        "seedEnd": session.seed_end,
    }


def last_event_summary(session) -> dict | None:
    """最近封存的那个主题，只取首页一行要用的四个键。"""
    if not session.events:
        return None
    event = session.events[-1]
    return {
        "id": event.id,
        "title": event.title,
        "status": event.status,
        "topicIndex": event.topic_index,
    }


def session_detail(
    session,
    run_id: str | None,
    *,
    artifact_count: int = 0,
    last_event: dict | None = None,
) -> dict:
    """单个会话的完整内容。"""
    data = session_summary(
        session,
        run_id,
        artifact_count=artifact_count,
        last_event=last_event,
    )
    data["effort"] = session.effort
    data["compactUpto"] = session.compact_upto
    data["messages"] = [
        message_json(message, index) for index, message in enumerate(session.messages)
    ]
    return data


def step_json(step) -> dict:
    """一条步记录。键名与页面同名，页面不再做一层映射。"""
    return {
        "seq": step.seq,
        "intent": step.intent,
        "result": step.result,
        "tools": list(step.tools),
        "args": list(step.args),
        "artifacts": list(step.artifacts),
        "issues": list(step.issues),
        # 位置是回原文的地址：页面点一步就按这两个数找到那次工具调用。
        "messageIndex": step.message_index,
        "partStart": step.part_start,
        "partEnd": step.part_end,
    }


def event_json(event) -> dict:
    """一条已封存的主题。

    `actions` / `artifacts` / `issues` 是 `Event` 上从 steps 聚合出来的属性，
    这里原样转出：页面上「这一步产出了什么」与事件层算的是同一份。
    """
    return {
        "id": event.id,
        "title": event.title,
        "summary": event.summary,
        "handoff": event.handoff,
        "topicIndex": event.topic_index,
        "kind": event.kind,
        "degraded": event.degraded,
        # 状态与停止原因：页面按 status 画徽标，exitReason 只给鼠标悬停看。
        "status": event.status,
        "exitReason": event.exit_reason,
        "createdAt": event.created_at,
        "stepStart": event.step_start,
        "stepEnd": event.step_end,
        "tokens": event.tokens,
        "cut": {"message": event.cut_message, "part": event.cut_part},
        "steps": [step_json(step) for step in event.steps],
        "actions": event.actions,
        "artifacts": event.artifacts,
        "issues": event.issues,
    }


def task_json(state) -> dict:
    """任务状态：用户提的目标与约束、做完的、待办的、卡住的、提出的假设。

    这是页面顶部那张任务卡的唯一来源，与模型每步看到的 TASK 层是同一份，
    页面上写的「目标」不会跟模型理解的目标分叉。
    """
    return {
        "goal": state.goal,
        "constraints": list(state.constraints),
        "done": list(state.done),
        "todo": list(state.todo),
        "blockers": list(state.blockers),
        "hypotheses": [item.to_dict() for item in state.hypotheses],
    }


def event_span(events) -> list[tuple[str, tuple[int, int], tuple[int, int]]]:
    """每条事件覆盖的位置区间 `[start, end)`。

    起点就是上一条的切点，首条从 `(0, 0)` 起——事件层「连续覆盖」那条规则的
    另一处写法。返回的顺序与事件顺序一致，页面按它把消息切开。
    """
    spans: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    start = (0, 0)
    for event in events:
        end = event.cut
        spans.append((event.id, start, end))
        start = end
    return spans


def slice_messages(session, start: tuple[int, int], end: tuple[int, int]) -> list[dict]:
    """取一段消息，按位置裁到 `[start, end)`。

    一条 assistant 消息装着整轮的所有往返，所以区间端点要落在**单元**上：
    只按消息下标切，会把上一条主题的工具调用一起带进来。裁完没有单元的消息
    不返回——页面上一条空消息只会让人以为内容丢了。

    末条消息也在循环里：区间两端可以落在同一条 assistant 消息内部（主题就是
    在一条消息中间结束的），按 `range(first, last)` 写会连这条消息的前半段一起
    丢掉。`end[1]` 为 0 时那一条截出空列表，自然跳过。
    """
    if not session.messages:
        return []
    first = start[0]
    last = min(end[0], len(session.messages) - 1)
    sliced: list[dict] = []
    for index in range(first, last + 1):
        message = session.messages[index]
        low = start[1] if index == start[0] else 0
        high = end[1] if index == end[0] else len(message.parts)
        parts = message.parts[low:high]
        if not parts:
            continue
        data = message_json(message, index)
        data["parts"] = [part_json(part) for part in parts]
        data["partOffset"] = low
        sliced.append(data)
    return sliced


def ledger_payload(root: Path, session_id: str) -> dict:
    """会话登记表：产物清单与血缘。**只读**，不动文件。

    登记表里每一项都带 `path`，那就是「这个 handle 落到盘上哪个文件」的凭据。
    下载接口只认这里出现过的路径，所以浏览器给什么都不会被当成路径解析。
    """
    path = root / f"{session_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"items": [], "note": "这个会话还没有产物登记表"}
    except (OSError, ValueError) as error:
        return {"items": [], "note": f"登记表读不出来：{error}"}

    items: list[dict] = []
    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        handle = entry.get("handle") or {}
        kind = str(handle.get("kind") or "")
        seq = int(handle.get("seq") or 0)
        if not kind or not seq:
            continue
        target = Path(str(entry.get("path") or "")) if entry.get("path") else None
        is_file = bool(target and target.is_file())
        items.append(
            {
                "handle": f"{kind}#{seq}",
                "kind": kind,
                "seq": seq,
                "summary": str(entry.get("summary") or ""),
                "path": str(target) if target else "",
                "name": target.name if target else "",
                "isFile": is_file,
                "isDir": bool(target and target.is_dir()),
                "bytes": target.stat().st_size if is_file else None,
                # 盘上现在还在不在，比登记时记的 file_state 更可信：那个值是上一次
                # 核对写下的，进程重启之后不会自己更新。
                "present": bool(target and target.exists()),
                "fileState": str(entry.get("file_state") or ""),
                "parents": [
                    f"{item.get('kind')}#{item.get('seq')}"
                    for item in entry.get("parents") or []
                    if isinstance(item, dict)
                ],
                "meta": entry.get("meta") or {},
                "createdAt": float(entry.get("created_at") or 0),
                "download": ""
                if not is_file
                else f"/api/sessions/{session_id}/artifact/{kind}%23{seq}",
            }
        )
    receptor = raw.get("current_receptor") or {}
    return {
        "items": items,
        "currentReceptor": (
            f"{receptor.get('kind')}#{receptor.get('seq')}"
            if isinstance(receptor, dict) and receptor
            else ""
        ),
    }


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
# 取数都在 `services/library.py`，这里保留同名入口给判据与旧调用点。
archive_directory = library_service.archive_directory
library_payload = library_service.library_payload
chunks_payload = library_service.chunks_payload
_inbox_files = library_service._inbox_files


def _item_identity(rest: str, action: str) -> tuple[str, str]:
    """`doc:ab12cd34ef/rename` → `(doc, ab12cd34ef)`。

    后缀对不上就是 404：`/api/library/doc:xxx/whatever` 不该落到某个默认动作上。
    `action` 为空串时整段就是身份（详情接口用这一种）。
    """
    suffix = f"/{action}" if action else ""
    if suffix:
        if not rest.endswith(suffix):
            raise HttpError(404, f"不认识的语料操作：{rest}")
        rest = rest[: -len(suffix)]
    matched = LIBRARY_ITEM.match(rest)
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
        # 投递与入库分两步：投递只落文件，入库（切块 + 向量）由应用那个进程做。
        "next": "文件已放进 corpus/inbox/。在页面上点「入库」即可切块进索引"
        "（独立运行的服务只能投递，入库要在应用里做）。",
    }


def _inbox_selection(request: "WebHandler", payload: dict) -> list[Path]:
    """从请求体里挑出要入库的文件。**只认 inbox 里的文件名，不认路径。**

    不传 `names` 就是「inbox 里全部能导的」。传了的话逐个核对：名字必须就是
    basename（`../../x.pdf` 压不成别的目录），而且必须已经在 inbox 里——这里
    不拼路径、也不替用户猜他想指哪个文件。
    """
    inbox = request.server.corpus_root / "inbox"
    raw = payload.get("names")
    if raw is None:
        return _inbox_files(request.server.corpus_root)
    if not isinstance(raw, list):
        raise HttpError(400, "names 要是一个数组（inbox 里的文件名）")
    picked: list[Path] = []
    for item in raw:
        name = str(item or "").strip()
        if not name or name != Path(name).name or "\\" in name:
            raise HttpError(400, f"不认这个文件名：{item!r}")
        path = inbox / name
        if not path.is_file():
            raise HttpError(404, f"inbox 里没有 {name}")
        picked.append(path)
    if not picked:
        raise HttpError(400, "没有要入库的文件")
    return picked


def _run_ingest(server: "WebServer", files: list[Path], job_id: str = "") -> None:
    """后台把一批文件入库，一条一条更新进度。异常按文件隔离。

    `job_id` 是任务台账里那一条。四个阶段（解析 / 切块 / 写索引 / 向量）由
    `ingest_files` 的 `on_stage` 回调报上来；这条线程是 daemon，服务停下时
    没人能中断它，所以状态由 `JobStore.stop_own()` 在关闭时改成中断。
    """
    jobs = server.jobs
    results: list[dict] = []
    seen: list[str] = []

    def stage(name: str, detail: str = "") -> None:
        if not job_id:
            return
        if not seen or seen[-1] != name:
            seen.append(name)
        jobs.update(job_id, stage=name, stages=list(seen), note=detail or name)

    try:
        for position, path in enumerate(files, 1):
            if job_id:
                jobs.update(job_id, note=path.name)
            try:
                # 语料根一起交过去：判据在临时目录上跑，入库不能写回仓根那份。
                outcomes = (
                    server.ingest([path], server.corpus_root, stage) if server.ingest else []
                )
            except Exception as error:  # noqa: BLE001 - 一个坏文件不该带走整批
                outcomes = [
                    {
                        "status": "failed",
                        "name": path.name,
                        "reason": f"{type(error).__name__}: {error}",
                    }
                ]
            results.extend(outcomes)
            with server.ingest_lock:
                server.ingest_state["results"].extend(outcomes)
                server.ingest_state["done"] = position
            if job_id:
                jobs.update(job_id, progress={"done": position, "total": len(files)})
        if job_id:
            failed = [item for item in results if item["status"] == "failed"]
            jobs.update(
                job_id,
                status="done",
                stage="完成",
                result={"results": results},
                error="；".join(
                    f"{item['name']}：{item.get('reason', '')}" for item in failed[:3]
                ),
            )
    except Exception as error:  # noqa: BLE001 - 这条线程没有上级，错误只能写进任务
        if job_id:
            jobs.update(
                job_id,
                status="failed",
                stage="失败",
                error=f"{type(error).__name__}: {error}",
            )
    finally:
        with server.ingest_lock:
            server.ingest_state["running"] = False


def _library_ingest(request: "WebHandler", _rest: str) -> tuple[dict, int]:
    """把 inbox 里的文件送进语料库。

    **写索引的仍然是应用那个进程**：网页只是把「入库」这个动作转交给它，自己
    不碰 `library.db`。「索引只有一个写者」是这条链上唯一不能破的约定——破了
    就会有两个进程同时改 `sources` / `chunks`，级联删除互相踩。

    所以独立运行的服务（`tools/serve_web.py`、`python -m vsagent.core.web`）
    没有可转交的对象，直接回 503 并说清楚去哪儿做，而不是退化成自己写。
    """
    _guard_write(request)
    server = request.server
    if server.ingest is None:
        raise HttpError(
            503,
            "这个进程不负责入库：用 python -m vsagent 启动应用之后再从网页上入库，"
            "或者在 TUI 里让模型 import_documents。",
        )
    files = _inbox_selection(request, _json_body(request))
    with server.ingest_lock:
        if server.ingest_state["running"]:
            raise HttpError(409, "上一批还在入库，等它结束再点")
        server.ingest_state = {
            "running": True,
            "total": len(files),
            "done": 0,
            "results": [],
        }
    job = server.jobs.create(
        "ingest",
        note="入库",
        total=len(files),
        meta={"names": [path.name for path in files]},
    )
    threading.Thread(
        target=_run_ingest,
        args=(server, files, job["id"]),
        name="vsagent-ingest",
        daemon=True,
    ).start()
    # 202：受理了，但切块与向量还在后台跑。页面靠轮询 `/api/library` 的 `ingest`
    # 看当场进度，靠 `/api/ingestion/jobs` 看四个阶段与历史。
    return {
        "started": len(files),
        "names": [path.name for path in files],
        "jobId": job["id"],
    }, 202


def _ingestion_list(request: "WebHandler", _rest: str) -> dict:
    """入库任务清单，新的在前。**只读台账文件**，不碰索引。"""
    return {"jobs": request.server.jobs.list(kind="ingest", limit=50)}


def _ingestion_detail(request: "WebHandler", rest: str) -> dict:
    """单条入库任务：阶段、进度、每个文件的结果。"""
    job_id = rest.strip("/")
    if "/" in job_id:
        raise HttpError(404, f"入库任务下面没有这个接口：{rest}")
    job = request.server.jobs.get(job_id)
    if job is None or str(job.get("kind")) != "ingest":
        raise HttpError(404, f"没有这条入库任务：{job_id}")
    return job


def _ingestion_retry(request: "WebHandler", rest: str) -> tuple[dict, int]:
    """重试一条失败或中断的入库任务。

    文件从 inbox 里重新取：成功的那几个已经被 `_import_one` 删掉，所以只重试
    还在的那几个。一个都不在了就明说「重新上传一次」，不凭空造一份。
    """
    _guard_write(request)
    server = request.server
    if server.ingest is None:
        raise HttpError(
            503,
            "这个进程不负责入库：用 python -m vsagent 启动应用之后再重试，"
            "或者在 TUI 里让模型 import_documents。",
        )
    job_id = rest.strip("/")
    if not job_id.endswith("/retry"):
        raise HttpError(404, f"入库任务下面没有这个动作：{rest}")
    job_id = job_id[: -len("/retry")].strip("/")
    if "/" in job_id:
        raise HttpError(404, f"入库任务下面没有这个动作：{rest}")
    job = server.jobs.get(job_id)
    if job is None or str(job.get("kind")) != "ingest":
        raise HttpError(404, f"没有这条入库任务：{job_id}")
    if job["status"] not in ("failed", "interrupted"):
        raise HttpError(409, f"这一条是 {job['status']}，不用重试")

    inbox = server.corpus_root / "inbox"
    names = [str(item) for item in (job.get("meta") or {}).get("names") or []]
    files = [inbox / name for name in names if name and (inbox / name).is_file()]
    if not files:
        raise HttpError(
            400, "原始文件已经不在 inbox 里了：重新上传一次再入库。"
        )

    with server.ingest_lock:
        if server.ingest_state["running"]:
            raise HttpError(409, "上一批还在入库，等它结束再点")
        server.ingest_state = {
            "running": True,
            "total": len(files),
            "done": 0,
            "results": [],
        }
    retry = server.jobs.create(
        "ingest",
        note="重试",
        total=len(files),
        meta={"names": [path.name for path in files], "retryOf": job_id},
    )
    threading.Thread(
        target=_run_ingest,
        args=(server, files, retry["id"]),
        name="vsagent-ingest",
        daemon=True,
    ).start()
    return {"jobId": retry["id"], "job": retry}, 202


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
    target = _trash_target(trash, kind, source_id)
    try:
        os.replace(directory, target)
    except OSError as error:
        raise HttpError(500, f"挪不进回收站：{error}") from error
    return {
        "id": f"{kind}:{source_id}",
        "deleted": True,
        "trash": str(target.relative_to(request.server.corpus_root)),
    }


def _trash_target(trash: Path, kind: str, source_id: str) -> Path:
    """回收站里的目标目录名。**不覆盖已有的那一份**。

    名字带上秒级时间戳，同一秒删两份时靠编号区分；删过又恢复、同一秒再删时
    会撞上同名目录，这时加序号。直接 `os.replace` 到已存在的非空目录会抛出，
    到空目录则会静默替换掉上一次那份。
    """
    base = f"{kind}-{source_id}-{int(time.time())}"
    candidate = trash / base
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = trash / f"{base}-{index}"
        if not candidate.exists():
            return candidate
    raise HttpError(409, f"{base} 的重名目录已经太多了，先清理 corpus/.trash/")


def _library_labels(request: "WebHandler", rest: str) -> dict:
    """`<kind>:<id>/labels`：写标签、收藏、文件夹。

    **只写 `labels.json`**。这三样都是组织数据，与索引无关：写完之后版本号不变，
    也不需要重新切块。`display_title` 原样保留，改名与标签共用一份文件。
    """
    _guard_write(request)
    kind, source_id = _item_identity(rest, "labels")
    directory = _source_directory(request.server.corpus_root, kind, source_id)
    if not (directory / corpus.META_FILE).is_file():
        raise HttpError(404, f"没有这一份：{kind}#{source_id}")
    payload = _json_body(request)
    labels = corpus.read_labels(directory)

    if "tags" in payload:
        raw = payload["tags"]
        if not isinstance(raw, list):
            raise HttpError(400, "tags 要是一个字符串数组")
        tags: list[str] = []
        for item in raw:
            tag = " ".join(str(item).split())
            if not tag:
                continue
            if len(tag) > MAX_TAG_CHARS:
                raise HttpError(400, f"标签「{tag}」超过 {MAX_TAG_CHARS} 字")
            if tag not in tags:
                tags.append(tag)
        if len(tags) > MAX_TAGS:
            raise HttpError(400, f"一份语料最多 {MAX_TAGS} 个标签，这次给了 {len(tags)} 个")
        if tags:
            labels["tags"] = tags
        else:
            labels.pop("tags", None)

    if "favorite" in payload:
        if payload["favorite"]:
            labels["favorite"] = True
        else:
            labels.pop("favorite", None)

    if "folderId" in payload:
        folder_id = str(payload.get("folderId") or "").strip()
        if folder_id:
            known = {str(item.get("id")) for item in corpus.read_folders(request.server.corpus_root)}
            if folder_id not in known:
                raise HttpError(400, f"没有这个文件夹：{folder_id}")
            labels["folderId"] = folder_id
        else:
            labels.pop("folderId", None)

    corpus.write_labels(directory, labels)
    return {"id": f"{kind}:{source_id}", "labels": labels}


def _library_batch_delete(request: "WebHandler", _rest: str) -> dict:
    """一次删多份网页来源。**只认 `web:`**：文献由用户显式导入，批量删掉之后
    恢复要重新导入，代价不对等。

    逐个挪进 `.trash/`，结果按条给。中途某个失败不影响其余几条——与入库
    「一个坏文件不带走整批」同一个处理。
    """
    _guard_write(request)
    payload = _json_body(request)
    raw = payload.get("ids")
    if not isinstance(raw, list) or not raw:
        raise HttpError(400, "ids 要是一个非空数组")
    if len(raw) > MAX_BATCH_DELETE:
        raise HttpError(413, f"一次最多删 {MAX_BATCH_DELETE} 份，这次给了 {len(raw)} 份")
    picked: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        matched = LIBRARY_ITEM.match(text)
        if matched is None:
            raise HttpError(400, f"语料标识要写成 doc:xxxxxxxxxx 或 web:xxxxxxxxxx：{text}")
        if matched.group(1) != "web":
            raise HttpError(400, f"批量删除只认网页来源：{text}")
        if text not in picked:
            picked.append(text)

    root = request.server.corpus_root
    trash = root / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for text in picked:
        kind, source_id = text.split(":", 1)
        directory = _source_directory(root, kind, source_id)
        if not directory.is_dir():
            results.append({"id": text, "deleted": False, "reason": "已经不在了"})
            continue
        target = _trash_target(trash, kind, source_id)
        try:
            os.replace(directory, target)
        except OSError as error:
            results.append({"id": text, "deleted": False, "reason": f"挪不进回收站：{error}"})
            continue
        results.append(
            {
                "id": text,
                "deleted": True,
                "trash": str(target.relative_to(root)),
            }
        )
    return {"results": results, "deleted": sum(1 for item in results if item["deleted"])}


def _library_folders(request: "WebHandler", _rest: str) -> dict:
    """`GET /api/library/folders`：文件夹清单。"""
    return {"folders": corpus.read_folders(request.server.corpus_root)}


def _library_folder_create(request: "WebHandler", _rest: str) -> dict:
    """`POST /api/library/folders`：新建一个文件夹。

    编号可以由调用方给（`research` 这种），不给就按顺序生成 `folder-1`。
    重名与重号都回 409：文件夹只有一个名字，重名之后筛选栏里会出现两条一样的。
    """
    _guard_write(request)
    payload = _json_body(request)
    name = " ".join(str(payload.get("name") or "").split())
    if not name:
        raise HttpError(400, "文件夹名称不能为空")
    if len(name) > 40:
        raise HttpError(400, "文件夹名称最多 40 字")
    folders = corpus.read_folders(request.server.corpus_root)
    if any(str(item.get("name")) == name for item in folders):
        raise HttpError(409, f"已经有叫「{name}」的文件夹了")

    folder_id = str(payload.get("id") or "").strip()
    if folder_id:
        if FOLDER_ID.match(folder_id) is None:
            raise HttpError(400, "文件夹编号要由小写字母、数字、下划线或连字符组成，最长 32 位")
        if any(str(item.get("id")) == folder_id for item in folders):
            raise HttpError(409, f"已经有编号 {folder_id} 的文件夹了")
    else:
        used = {str(item.get("id")) for item in folders}
        index = len(folders) + 1
        while f"folder-{index}" in used:
            index += 1
        folder_id = f"folder-{index}"

    folder = {"id": folder_id, "name": name, "order": len(folders)}
    folders.append(folder)
    corpus.write_folders(request.server.corpus_root, folders)
    return {"folder": folder, "folders": folders}


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonRoute:
    """一个返回 JSON 的路由。

    `prefix` 为真时按前缀匹配，`handler` 收路径剩下的那一段；
    否则要求整段相等，`handler` 收不到参数。`method` 决定它接哪个动词——
    GET 与 POST 混在一张表里，是为了让「全部端点」仍然只有一处可读。

    `writes` 标记这条路由会改盘上的东西。判据按它逐条验「缺 `X-VSA-Request`
    时回 403」：漏标一条，那个写入口就不会被检查到。
    """

    path: str
    handler: Callable[[Any, str], Any]
    prefix: bool = False
    method: str = "GET"
    writes: bool = False

    def matches(self, path: str, method: str) -> bool:
        if self.method != method:
            return False
        if self.prefix:
            return path.startswith(self.path)
        return path == self.path


@dataclass(frozen=True)
class RawRoute:
    """一条自己写响应的路由。

    `JsonRoute` 的处理函数返回一个对象，由分发处编码成 JSON；产物下载返回的是
    文件字节，编码那一步必须跳过。判别用正则，因为路径里带着 `#` 与编号。
    """

    pattern: re.Pattern
    handler: Callable[[Any, Any], None]
    method: str = "GET"

    def matches(self, path: str, method: str):
        if self.method != method:
            return None
        return self.pattern.match(path)


@dataclass(frozen=True)
class RouteSpec:
    """这台服务认识的全部路径。

    **这是路由的唯一真相**：命令行脚本与包内服务都按它分发，
    判据把 `json_routes` 换掉一个再请求一次，两边都应跟着变。
    """

    json_routes: tuple[JsonRoute, ...] = field(default_factory=tuple)
    raw_routes: tuple[RawRoute, ...] = field(default_factory=tuple)

    def match(self, path: str, method: str = "GET") -> JsonRoute | None:
        """找出处理这条路径与动词的路由；静态文件与未知路径都返回 None。"""
        for route in self.json_routes:
            if route.matches(path, method):
                return route
        return None

    def match_raw(self, path: str, method: str = "GET"):
        """找出要自己写响应的一条，返回 `(路由, 正则匹配)`。"""
        for route in self.raw_routes:
            matched = route.matches(path, method)
            if matched is not None:
                return route, matched
        return None


def _summary_extras(request: "WebHandler", session) -> dict:
    """首页最近任务要的两项：登记表条数与最近一个主题。

    登记表读一份要不到 1 毫秒（本机 23 个会话合计 9 毫秒），跟着列表一起算，
    比让页面为每一行再发一次请求省事。
    """
    items = ledger_payload(request.server.ledgers_root, session.id)["items"]
    return {"artifact_count": len(items), "last_event": last_event_summary(session)}


def _session_list(request: "WebHandler", _rest: str) -> dict:
    """会话列表，按更新时间倒序（`list_sessions` 已有序）。"""
    manager = load_manager(request.server.state_path)
    runs = scan_runs(request.server.runs_dir)
    return {
        "sessions": [
            session_summary(
                session,
                run_id_for_session(runs, session.id),
                **_summary_extras(request, session),
            )
            for session in manager.list_sessions()
        ]
    }


def _session_events(request: "WebHandler", session) -> dict:
    """一个会话的主题清单：事件、任务状态、总账。不含任何工具正文。

    左栏与任务卡只需要这些：标题、结论、步数、产物名。正文由 `/segment`
    按事件一次取一段，所以这里是几十 KB 而不是几 MB。
    """
    events = [event_json(event) for event in session.events]
    artifacts: list[str] = []
    issues: list[str] = []
    for event in events:
        for name in event["artifacts"]:
            if name not in artifacts:
                artifacts.append(name)
        issues.extend(event["issues"])
    return {
        "sessionId": session.id,
        "title": session.title,
        "status": session.status,
        "parentId": session.parent_id,
        "messageCount": len(session.messages),
        "compactUpto": session.compact_upto,
        "task": task_json(session.task_state),
        "events": events,
        "totals": {
            "events": len(events),
            # 按条目数算而不是按 `step_end`：旧会话的事件没有连号步区间
            # （两个字段都是 0），按号码算会把走过的步报成 0。
            "steps": sum(len(event["steps"]) for event in events),
            "artifacts": len(artifacts),
            "issues": len(issues),
        },
    }


def _session_segment(request: "WebHandler", session) -> dict:
    """一个主题的原文：按事件的位置区间切好的消息。

    `event=all` 取整个会话。判断区间用的是 `Event` 上的切点，与装配器读的是
    同一组数，所以页面上「这一段属于哪个主题」跟模型看到的分段一致。
    """
    query = parse_qs(urlsplit(request.path).query)
    wanted = (query.get("event") or ["all"])[0].strip() or "all"
    if wanted == "all":
        start, end = (0, 0), (len(session.messages), 0)
        label = "all"
    else:
        spans = {item[0]: (item[1], item[2]) for item in event_span(session.events)}
        found = spans.get(wanted)
        if found is None:
            raise HttpError(404, f"这个会话里没有这条事件：{wanted}")
        start, end = found
        label = wanted
    messages = slice_messages(session, start, end)
    return {
        "sessionId": session.id,
        "event": label,
        "from": {"message": start[0], "part": start[1]},
        "to": {"message": end[0], "part": end[1]},
        "partCount": sum(len(message["parts"]) for message in messages),
        "messages": messages,
    }


def _session_artifacts(request: "WebHandler", session) -> dict:
    """会话产物：登记表里的条目，加这个会话产出的运行目录。"""
    payload = ledger_payload(request.server.ledgers_root, session.id)
    runs = [
        {
            "id": item["id"],
            "runId": item["runId"],
            "name": item["name"],
            "pdbId": item["pdbId"],
            "pocketCount": item["pocketCount"],
            "moleculeCount": item["moleculeCount"],
        }
        for item in scan_runs(request.server.runs_dir)
        if item["sessionId"] == session.id
    ]
    return {"sessionId": session.id, "runs": runs, **payload}


def _artifact_entry(root: Path, session_id: str, want: str) -> dict | None:
    """按 handle 在登记表里找条目。**只认登记过的**，浏览器给的字符串不拼路径。"""
    for entry in ledger_payload(root, session_id)["items"]:
        if entry["handle"] == want:
            return entry
    return None


ARTIFACT_HANDLE = re.compile(r"^(receptor|ligandset|artifact)#(\d+)$")
ARTIFACT_PATTERN = re.compile(r"^/api/sessions/(?P<id>[^/]+)/artifact/(?P<handle>[^/]+)$")


def serve_artifact(request: "WebHandler", matched) -> None:
    """`GET /api/sessions/<id>/artifact/<kind>%23<seq>`：把产物原样发回去。

    路径来自服务自己读的登记表，不是请求里的字符串——所以这里没有路径解析，
    也就没有穿越可谈。handle 只认三种形状，别的直接 404。
    """
    session_id = matched.group("id")
    manager = load_manager(request.server.state_path)
    if manager.get_session(session_id) is None:
        raise HttpError(404, f"会话不存在：{session_id}")
    raw = matched.group("handle")
    if not ARTIFACT_HANDLE.match(raw):
        raise HttpError(404, f"产物标识要写成 receptor#1 / ligandset#2 / artifact#3：{raw}")
    entry = _artifact_entry(request.server.ledgers_root, session_id, raw)
    if entry is None:
        raise HttpError(404, f"登记表里没有 {raw}")
    path = Path(entry["path"]) if entry["path"] else None
    if path is None or not path.is_file():
        raise HttpError(404, f"{raw} 指向的文件不在了：{entry['path'] or '（没有记录路径）'}")
    request.send_body(path.read_bytes(), content_type_for(path))


def _session_live(request: "WebHandler", session_id: str) -> dict:
    """当前主题的实时快照。**不读状态文件**。

    页面两秒拉一次，而 `state.toml` 有几 MB，解析一遍的时间比这一条要回答的问题
    还长。快照文件由装配根在每一步写、轮末标成不跑；没有这个文件就说明这一轮
    没在跑（独立服务、刚启动、或者上一次没跑过）。
    """
    if not LIVE_SESSION.match(session_id):
        raise HttpError(404, f"会话编号不合法：{session_id}")
    data = live.read(request.server.state_path, session_id)
    if data is None:
        return {
            "sessionId": session_id,
            "present": False,
            "running": False,
            "stale": False,
        }
    return {**data, "present": True}


ANALYSIS_TEXT_LIMIT = 600
"""分析里保留多少字原话与正文。

整份正文动辄几十万字，一次分析要把它发给页面没有用处；一段引子够定位到
是哪一轮、说了什么。原文在会话页里照样能读。
"""


def _session_analysis(request: "WebHandler", session) -> dict:
    """一个会话的分析：轮、步、token、工具、主题与异常。

    trace 按会话分文件写在状态文件旁边，里面是每一步的装配读数与工具调用现场，
    状态文件里没有这些。没有 trace 时仍然给轮与主题的统计，页面按 `hasTrace`
    标明逐步读数缺失。
    """
    path = log_directory_for(request.server.state_path) / f"{session.id}.jsonl"
    if path.is_file():
        trace = read_trace(path, session.id)
        size = path.stat().st_size
    else:
        trace = []
        size = 0
    payload = build_report(session, trace, text_limit=ANALYSIS_TEXT_LIMIT)
    payload["traceBytes"] = size
    return payload


def _session_detail(request: "WebHandler", rest: str) -> dict:
    """会话本身与它下面的五个只读视图：`/events`、`/segment`、`/artifacts`、`/live`、`/analysis`。

    几条共用一个前缀路由，是因为它们都以会话编号开头；拆成多条前缀路由时，
    `/api/sessions/` 会先把它们全都接住，谁先谁后都一样。
    """
    segments = [item for item in rest.split("/") if item]
    if not segments:
        raise HttpError(404, "缺少会话编号")
    session_id = segments[0]
    # 实时快照先走：它两秒被拉一次，而解析状态文件要几 MB 的时间。
    if len(segments) == 2 and segments[1] == "live":
        return _session_live(request, session_id)
    manager = load_manager(request.server.state_path)
    session = manager.get_session(session_id)
    if session is None:
        raise HttpError(404, f"会话不存在：{session_id}")

    if len(segments) == 1:
        runs = scan_runs(request.server.runs_dir)
        return session_detail(
            session,
            run_id_for_session(runs, session.id),
            **_summary_extras(request, session),
        )
    action = segments[1]
    if len(segments) == 2:
        if action == "events":
            return _session_events(request, session)
        if action == "segment":
            return _session_segment(request, session)
        if action == "artifacts":
            return _session_artifacts(request, session)
        if action == "analysis":
            return _session_analysis(request, session)
    raise HttpError(404, f"会话下面没有这个接口：{'/'.join(segments[1:])}")


def _run_list(request: "WebHandler", _rest: str) -> dict:
    """runs/ 下的运行清单。"""
    return {"runs": scan_runs(request.server.runs_dir)}


SEARCH_LIMIT = 8
"""每一类最多给几条。

搜索的目标是「找到那一条再点进去」，不是把全库铺出来；每类八条在顶部一层
放得下，也不至于为了一个常见词把几 MB 的正文翻一遍。
"""


def _search(request: "WebHandler", _rest: str) -> dict:
    """按词找会话、主题、产物。

    **只搜标题与结论**，不搜工具正文：正文在状态文件里有几 MB，逐字符匹配
    既慢又吵（一个 `PDB` 能命中几百条），而真正要定位的是「哪次任务、哪一段」。
    正文的检索属于语料库那条链，两者不是一回事。
    """
    query = parse_qs(urlsplit(request.path).query)
    term = (query.get("q") or [""])[0].strip()
    if not term:
        return {"query": "", "sessions": [], "topics": [], "artifacts": []}
    needle = term.lower()

    manager = load_manager(request.server.state_path)
    sessions: list[dict] = []
    topics: list[dict] = []
    for session in manager.list_sessions():
        if needle in (session.title or "").lower():
            sessions.append(
                {
                    "id": session.id,
                    "title": session.title,
                    "updatedAt": session.updated_at,
                    "eventCount": len(session.events),
                    "messageCount": len(session.messages),
                }
            )
        for event in session.events:
            if needle not in f"{event.title} {event.summary}".lower():
                continue
            topics.append(
                {
                    "sessionId": session.id,
                    "sessionTitle": session.title,
                    "eventId": event.id,
                    "title": event.title,
                    "summary": event.summary,
                    "createdAt": event.created_at,
                }
            )

    artifacts: list[dict] = []
    for path in sorted(request.server.ledgers_root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 一份坏快照不该让搜索整体失败：跳过它，其余照常。
            continue
        for entry in raw.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            handle = entry.get("handle") or {}
            text = f"{entry.get('summary') or ''} {Path(str(entry.get('path') or '')).name}"
            if needle not in text.lower():
                continue
            artifacts.append(
                {
                    "sessionId": path.stem,
                    "handle": f"{handle.get('kind')}#{handle.get('seq')}",
                    "summary": str(entry.get("summary") or ""),
                    "name": Path(str(entry.get("path") or "")).name,
                    "kind": str(handle.get("kind") or ""),
                }
            )

    return {
        "query": term,
        "sessions": sessions[:SEARCH_LIMIT],
        "topics": topics[:SEARCH_LIMIT],
        "artifacts": artifacts[:SEARCH_LIMIT],
        "truncated": {
            "sessions": len(sessions) > SEARCH_LIMIT,
            "topics": len(topics) > SEARCH_LIMIT,
            "artifacts": len(artifacts) > SEARCH_LIMIT,
        },
    }


SURVEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _survey(request: "WebHandler", rest: str) -> dict:
    """`/api/survey/<id>`：一份结构全景的完整产物。

    只读。id 是 `survey_structures` 写产物时的目录名（安全 stem + 参数指纹），
    这里只认这个形状——**只认 id，不认路径**，于是没有逃逸的可能。
    产物不存在时回 404，不返回空表：空表会被读成「这个靶点没有结构」。
    """
    survey_id = unquote(rest).strip("/")
    if not SURVEY_ID.match(survey_id):
        raise HttpError(400, "全景标识不合法")
    path = request.server.surveys_root / survey_id / "survey.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HttpError(404, f"没有这份结构全景：{survey_id}") from None
    except (OSError, ValueError) as error:
        raise HttpError(500, f"全景产物读不出来：{error}") from error
    if not isinstance(payload, dict):
        raise HttpError(500, "全景产物不是一份对象")
    return payload


def _library(request: "WebHandler", _rest: str) -> dict:
    """语料库列表。只读：列表、待入库数、占用、开关现状与入库进度。

    `q` / `kind` / `status` / `sort` / `offset` / `limit` 都可省略；一个都不给时
    返回全部，与 M0 之前的行为一致。`limit` 不传就不分页——判据与旧页面按
    「一次拿到全部」写的，默认值一改，它们的条数断言就会跟着变。

    `ingest` 是**内存里的进度**，不写盘：它只描述「这一批跑到哪了」，页面靠
    轮询这一项把按钮从「入库中」变回「入库」。服务重启后这批任务就没了，
    而盘上的语料是完整的——进度不该被当成持久状态。
    """
    query = parse_qs(urlsplit(request.path).query)
    offset = _query_int(query, "offset", 0, 0, 1_000_000)
    raw_limit = (query.get("limit") or [""])[0].strip()
    limit = None if not raw_limit else _query_int(
        query, "limit", library_service.LIBRARY_PAGE_MAX, 1, library_service.LIBRARY_PAGE_MAX
    )
    payload = library_payload(
        request.server.corpus_root,
        query=(query.get("q") or [""])[0].strip(),
        kind=(query.get("kind") or [""])[0].strip(),
        status=(query.get("status") or [""])[0].strip(),
        sort=(query.get("sort") or ["added"])[0].strip() or "added",
        offset=offset,
        limit=limit,
    )
    state = request.server.ingest_state
    with request.server.ingest_lock:
        payload["ingest"] = {
            "running": bool(state["running"]),
            "total": int(state["total"]),
            "done": int(state["done"]),
            "results": list(state["results"]),
        }
    payload["can_ingest"] = request.server.ingest is not None
    return payload


def _query_int(query: dict, name: str, default: int, low: int, high: int) -> int:
    """从查询串里取一个整数并夹取。写错就当没给——分页参数不该让请求失败。"""
    raw = (query.get(name) or [""])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def _library_chunks(request: "WebHandler", rest: str) -> dict:
    """`<kind>:<id>/chunks?offset=&limit=&q=`：只读地把切块列出来。

    给的是索引里那份文本（检索看到的就是它），不是按原件重切一遍。分页是为了
    不让一次响应变成几 MB——切块本身没有大小上限，一份长文献几百块很正常。
    """
    kind, source_id = _item_identity(rest, "chunks")
    directory = _source_directory(request.server.corpus_root, kind, source_id)
    if not (directory / corpus.META_FILE).is_file():
        raise HttpError(404, f"没有这一份：{kind}#{source_id}")
    query = parse_qs(urlsplit(request.path).query)
    offset = _query_int(query, "offset", 0, 0, 1_000_000)
    limit = _query_int(query, "limit", CHUNK_PAGE, 1, CHUNK_PAGE_MAX)
    payload = chunks_payload(
        request.server.corpus_root,
        kind,
        source_id,
        offset=offset,
        limit=limit,
        query=(query.get("q") or [""])[0].strip(),
    )
    payload["id"] = f"{kind}:{source_id}"
    return payload


def _library_detail(request: "WebHandler", rest: str) -> dict:
    """`<kind>:<id>`：一份源的详情。

    `/api/library/<kind>:<id>/chunks` 与它共用一条前缀路由：两者都以身份开头，
    拆成两条前缀路由时，谁先谁后都会把对方接住。
    """
    if rest.endswith("/chunks"):
        return _library_chunks(request, rest)
    if "/" in rest:
        raise HttpError(404, f"不认识的语料操作：{rest}")
    kind, source_id = _item_identity(rest, "")
    payload = library_service.source_detail(request.server.corpus_root, kind, source_id)
    if payload is None:
        raise HttpError(404, f"没有这一份：{kind}#{source_id}")
    return payload


def _corpus_stats(request: "WebHandler", _rest: str) -> dict:
    """整库读数：源数、块数、向量覆盖、元数据缺失条数与占用。"""
    return library_service.corpus_stats(request.server.corpus_root)


def _knowledge_search(request: "WebHandler", _rest: str) -> dict:
    """`/api/knowledge/search?q=&mode=&limit=&source=`：语料与记忆两段结果。

    `mode` 只认 `hybrid` / `lexical` / `semantic`；写错了当场回 400，
    不退回默认值——一个拼错的参数让页面看起来「搜到了」，比报错更难查。
    `source` 是逗号分隔的源标识，用来把检索限在选中的几份语料里。
    """
    query = parse_qs(urlsplit(request.path).query)
    term = (query.get("q") or [""])[0].strip()
    if not term:
        raise HttpError(400, "检索词不能为空")
    mode = (query.get("mode") or ["hybrid"])[0].strip() or "hybrid"
    if mode not in search_service.MODES:
        raise HttpError(400, f"不认识的检索模式：{mode}；可用 {'、'.join(search_service.MODES)}")
    limit = _query_int(query, "limit", SEARCH_LIMIT, 1, 20)
    raw_sources = (query.get("source") or [""])[0].strip()
    source_ids: tuple[str, ...] = ()
    if raw_sources:
        picked = [item.strip() for item in raw_sources.split(",") if item.strip()]
        for item in picked:
            if LIBRARY_ITEM.match(item) is None:
                raise HttpError(400, f"源标识要写成 doc:xxxxxxxxxx 或 web:xxxxxxxxxx：{item}")
        source_ids = tuple(picked)
    return search_service.knowledge_payload(
        request.server.workspace_root,
        term,
        limit=limit,
        mode=mode,
        source_ids=source_ids,
        corpus_root=request.server.corpus_root,
    )


MEMORY_ID = re.compile(r"^mem_[0-9]{14}_[0-9a-f]{6}$")
"""记忆编号的形状，与 `memory.new_memory_id` 一致。它只用来查字典，不进路径。"""


def _knowledge_relations(request: "WebHandler", rest: str) -> dict:
    """`/api/knowledge/<type>/<id>/relations`：文献与记忆之间的引用关系。

    两侧都要能查：从文献出发看「哪几条记忆引用了它」，从记忆出发看「它引用的
    文件里哪些在语料库中」。引用是文件路径，语料是 `doc:<哈希>`，换算在
    `knowledge_refs` 里。

    记忆引用的路径分三种：语料库里的一篇（`documents`）、库外仍然存在的文件
    （`external`）、已经不在盘上的（`missing`）。后两种不报错——引用本来就是
    用户写下的，文件被移走不该让整条记忆查不了。
    """
    parts = [item for item in rest.strip("/").split("/") if item]
    if len(parts) != 3 or parts[2] != "relations":
        raise HttpError(404, f"不认识的关联查询：{rest}")
    kind, ident = parts[0], parts[1]
    if kind == "doc":
        return _doc_relations(request.server, ident)
    if kind == "memory":
        return _memory_relations(request.server, ident)
    raise HttpError(404, f"关联查询只认 doc 与 memory：{kind}")


def _doc_relations(server: "WebServer", ident: str) -> dict:
    """一篇文献 → 引用它的记忆。"""
    source_id = ident[len("doc:") :] if ident.startswith("doc:") else ident
    if LIBRARY_ITEM.match(f"doc:{source_id}") is None:
        raise HttpError(400, "文献标识要写成 doc:xxxxxxxxxx 或直接给那 10 位哈希")
    handle = f"doc:{source_id}"
    info = knowledge_refs.documents(server.corpus_root).get(handle)
    if info is None:
        raise HttpError(404, f"语料库里没有这篇文献：{handle}")

    store = memory_service.load_store(server.workspace_root)
    rows = list(store.entries.values()) + store.archived_entries()
    linked = [
        {
            "id": entry.id,
            "kind": entry.kind.value,
            "text": entry.text,
            "state": memory_service.entry_state(entry),
            "refs": [ref for ref in entry.refs if knowledge_refs.source_of(server.corpus_root, ref) == handle],
        }
        for entry in rows
        if any(knowledge_refs.source_of(server.corpus_root, ref) == handle for ref in entry.refs)
    ]
    linked.sort(key=lambda item: (-len(item["refs"]), item["id"]))
    return {
        "type": "doc",
        "id": source_id,
        "sourceId": handle,
        "ref": info["ref"],
        "title": info["title"],
        "memory": linked,
    }


def _memory_relations(server: "WebServer", entry_id: str) -> dict:
    """一条记忆 → 它引用的文献，加上共享同一篇文献的其他记忆。"""
    if MEMORY_ID.match(entry_id) is None:
        raise HttpError(400, "记忆编号要写成 mem_xxxxxxxxxxxxxx_xxxxxx")
    store = memory_service.load_store(server.workspace_root)
    entry = store.entries.get(entry_id)
    archived = False
    if entry is None:
        entry = next((item for item in store.archived_entries() if item.id == entry_id), None)
        archived = entry is not None
    if entry is None:
        raise HttpError(404, f"没有这条记忆：{entry_id}")

    catalogue = knowledge_refs.documents(server.corpus_root)
    documents: list[dict] = []
    external: list[dict] = []
    missing: list[dict] = []
    for ref in entry.refs:
        handle = knowledge_refs.source_of(server.corpus_root, ref)
        if handle:
            info = catalogue[handle]
            documents.append(
                {
                    "sourceId": handle,
                    "ref": info["ref"],
                    "title": info["title"],
                    "path": ref,
                    "state": "ok",
                }
            )
            continue
        exists = Path(ref).expanduser().exists() if ref else False
        row = {"path": ref, "state": "ok" if exists else "missing"}
        (external if exists else missing).append(row)

    related: list[dict] = []
    handles = {item["sourceId"] for item in documents}
    if handles:
        rows = list(store.entries.values()) + store.archived_entries()
        for other in rows:
            if other.id == entry.id:
                continue
            shared = {
                knowledge_refs.source_of(server.corpus_root, ref) for ref in other.refs
            } & handles
            if not shared:
                continue
            related.append(
                {
                    "id": other.id,
                    "kind": other.kind.value,
                    "text": other.text,
                    "state": memory_service.entry_state(other),
                    "shared": sorted(shared),
                }
            )
        related.sort(key=lambda item: (-len(item["shared"]), item["id"]))

    return {
        "type": "memory",
        "id": entry.id,
        "kind": entry.kind.value,
        "text": entry.text,
        "state": "archived" if archived else memory_service.entry_state(entry),
        "documents": documents,
        "external": external,
        "missing": missing,
        "memory": related,
    }


def _memory_list(request: "WebHandler", _rest: str) -> dict:
    """`/api/memory?kind=&state=&q=`：活的与归档的都在一份清单里。"""
    query = parse_qs(urlsplit(request.path).query)
    return memory_service.list_entries(
        request.server.workspace_root,
        kind=(query.get("kind") or [""])[0].strip(),
        state=(query.get("state") or [""])[0].strip(),
        query=(query.get("q") or [""])[0].strip(),
    )


def _memory_detail(request: "WebHandler", rest: str) -> dict:
    """`/api/memory/<id>`：单条。归档里的也能打开。"""
    entry_id = rest.strip("/")
    if "/" in entry_id:
        raise HttpError(404, f"记忆下面没有这个接口：{rest}")
    if MEMORY_ID.match(entry_id) is None:
        raise HttpError(400, "记忆编号的形状不对")
    payload = memory_service.entry_detail(request.server.workspace_root, entry_id)
    if payload is None:
        raise HttpError(404, f"没有这条记忆：{entry_id}")
    return payload


def _memory_action(request: "WebHandler", rest: str) -> dict:
    """`/api/memory/<id>/archive` 与 `/restore`：转交给应用进程。

    网页进程不写 `entries.json`：那个 `MemoryStore` 由应用持有，两边同时写会
    互相覆盖。独立运行的服务没有可转交的对象，明确回 503。
    """
    _guard_write(request)
    segments = [item for item in rest.split("/") if item]
    if len(segments) != 2 or segments[1] not in ("archive", "restore"):
        raise HttpError(404, f"不认识的记忆操作：{rest}")
    entry_id = segments[0]
    if MEMORY_ID.match(entry_id) is None:
        raise HttpError(400, "记忆编号的形状不对")
    server = request.server
    if server.memory_action is None:
        raise HttpError(
            503,
            "这个进程不负责改记忆：用 python -m vsagent 启动应用之后再操作，"
            "或者在终端里用 /memory。",
        )
    outcome = server.memory_action(segments[1], entry_id)
    status = int(outcome.get("status") or 200)
    if status != 200:
        raise HttpError(status, str(outcome.get("error") or "操作失败"))
    return {"id": entry_id, "action": segments[1], "ok": True}


def _evaluation_start(request: "WebHandler", _rest: str) -> tuple[dict, int]:
    """起一次检索评测，当场回 202 与任务编号。

    评测在子进程里跑：它要加载嵌入模型、跑几十条查询，放在服务进程里会把
    网页卡住几十秒。
    """
    _guard_write(request)
    payload = _json_body(request)
    try:
        job = evaluation_service.start_run(
            project_root(),
            request.server.jobs,
            questions=str(payload.get("questions") or "").strip(),
            model=str(payload.get("model") or "").strip(),
            rerank=bool(payload.get("rerank")),
        )
    except ValueError as error:
        raise HttpError(400, str(error)) from error
    return {"jobId": job["id"], "job": job}, 202


def _evaluation_list(request: "WebHandler", _rest: str) -> dict:
    """历次评测，新的在前。"""
    return {"jobs": request.server.jobs.list(kind="evaluation", limit=50)}


def _evaluation_detail(request: "WebHandler", rest: str) -> dict:
    """单次评测的完整报告。"""
    job_id = rest.strip("/")
    if "/" in job_id:
        raise HttpError(404, f"评测下面没有这个接口：{rest}")
    job = request.server.jobs.get(job_id)
    if job is None or str(job.get("kind")) != "evaluation":
        raise HttpError(404, f"没有这次评测：{job_id}")
    return job


def _usage_append(request: "WebHandler", _rest: str) -> dict:
    """追加一行搜索记录。**不改索引，不改状态文件。**"""
    _guard_write(request)
    try:
        item = search_service.append_usage(
            request.server.corpus_root, _json_body(request)
        )
    except ValueError as error:
        raise HttpError(400, str(error)) from error
    return {"record": item}


def _analytics(request: "WebHandler", _rest: str) -> dict:
    """使用记录汇总：次数、p95、模式分布、空结果率。"""
    return search_service.analytics(request.server.corpus_root)


def _library_item_action(request: "WebHandler", rest: str) -> dict:
    """`<kind>:<id>/rename` 与 `<kind>:<id>/delete` 的分发。

    两个动作的路径形状一样，所以共用一条前缀路由；认不出来的动作回 404，
    不落到某个默认动作上。守卫写在这里而不是每个分支里：判据按 `SPEC` 里
    标了 `writes` 的路由逐条验 403，漏一处那条就验不到。
    """
    _guard_write(request)
    if rest.endswith("/rename"):
        return _library_rename(request, rest)
    if rest.endswith("/delete"):
        return _library_delete(request, rest)
    if rest.endswith("/labels"):
        return _library_labels(request, rest)
    raise HttpError(404, f"不认识的语料操作：{rest}")


def _create_task(request: "WebHandler", _rest: str) -> dict:
    """`POST /api/tasks`：把一个任务交给应用进程去跑。

    **这个进程不写状态文件**。会话的创建、用户消息的写入、轮次的启动全在应用
    那个事件循环里做（`SessionManager` 不是线程安全的），这里只是等一个结果。
    独立运行的服务没有可转交的对象，明确回 503，而不是自己开一个写者。

    回调返回两种形状：成功是 `{"sessionId": ..., "created": ...}`；被拒绝是
    `{"error": ..., "status": 400|404|409}`，状态码由它决定。
    """
    _guard_write(request)
    server = request.server
    if server.submit is None:
        raise HttpError(
            503,
            "这个进程不负责跑任务：用 python -m vsagent 启动应用之后再从网页上提交，"
            "或者在终端里直接输入。",
        )
    payload = _json_body(request)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HttpError(400, "任务内容不能为空")
    if len(text) > MAX_TASK_CHARS:
        raise HttpError(413, f"任务内容超过 {MAX_TASK_CHARS} 字，请先存成文件再让模型读")
    session_id = str(payload.get("sessionId") or "").strip()
    outcome = server.submit(text, session_id)
    status = int(outcome.get("status") or 200)
    if status != 200:
        raise HttpError(status, str(outcome.get("error") or "提交失败"))
    return {"sessionId": outcome["sessionId"], "created": bool(outcome.get("created"))}


SPEC = RouteSpec(
    json_routes=(
        JsonRoute("/api/sessions/", _session_detail, prefix=True),
        JsonRoute("/api/sessions", _session_list),
        JsonRoute("/api/runs", _run_list),
        JsonRoute("/api/search", _search),
        JsonRoute("/api/survey/", _survey, prefix=True),
        JsonRoute("/api/library", _library),
        JsonRoute("/api/corpus/stats", _corpus_stats),
        JsonRoute(
            "/api/ingestion/jobs/",
            _ingestion_detail,
            prefix=True,
        ),
        JsonRoute("/api/ingestion/jobs", _ingestion_list),
        JsonRoute(
            "/api/ingestion/jobs/",
            _ingestion_retry,
            prefix=True,
            method="POST",
            writes=True,
        ),
        JsonRoute("/api/knowledge/search", _knowledge_search),
        # 评测的详情路由放在列表之前：前缀 `/api/knowledge/evaluation/runs/` 也会
        # 匹配到空尾巴，顺序反了会把列表请求当成一次编号为空的详情。
        JsonRoute(
            "/api/knowledge/evaluation/runs/",
            _evaluation_detail,
            prefix=True,
        ),
        JsonRoute("/api/knowledge/evaluation/runs", _evaluation_list),
        JsonRoute(
            "/api/knowledge/evaluation/runs",
            _evaluation_start,
            method="POST",
            writes=True,
        ),
        JsonRoute(
            "/api/knowledge/usage",
            _usage_append,
            method="POST",
            writes=True,
        ),
        JsonRoute("/api/knowledge/analytics", _analytics),
        # 前缀路由放最后：`/api/knowledge/search` 与评测那几条都是精确路径，
        # 顺序反了会被这一条先吃掉。
        JsonRoute("/api/knowledge/", _knowledge_relations, prefix=True),
        JsonRoute("/api/memory", _memory_list),
        JsonRoute("/api/memory/", _memory_detail, prefix=True),
        JsonRoute(
            "/api/memory/",
            _memory_action,
            prefix=True,
            method="POST",
            writes=True,
        ),
        JsonRoute("/api/library/folders", _library_folders),
        JsonRoute(
            "/api/library/folders",
            _library_folder_create,
            method="POST",
            writes=True,
        ),
        JsonRoute(
            "/api/library/batch/delete",
            _library_batch_delete,
            method="POST",
            writes=True,
        ),
        JsonRoute("/api/library/upload", _library_upload, method="POST", writes=True),
        # 放在前缀路由之前：`/api/library/ingest` 也会被 `/api/library/` 前缀匹配到，
        # 那条是给 `<kind>:<id>/<action>` 用的，顺序反了会先撞上它。
        JsonRoute("/api/library/ingest", _library_ingest, method="POST", writes=True),
        JsonRoute("/api/library/", _library_detail, prefix=True, method="GET"),
        JsonRoute(
            "/api/library/",
            _library_item_action,
            prefix=True,
            method="POST",
            writes=True,
        ),
        JsonRoute("/api/tasks", _create_task, method="POST", writes=True),
    ),
    raw_routes=(
        RawRoute(ARTIFACT_PATTERN, serve_artifact),
    ),
)
"""默认路由表。写入口都要 `X-VSA-Request` 头（见 `_guard_write`）。"""


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
        # 入库回调的形状：`(files, root, on_stage)`。第三项是进度回调，
        # 独立实现可以不收（判据里的假实现就只收两个）。
        ingest: Callable[..., list[dict[str, Any]]] | None = None,
        surveys_root: Path | None = None,
        ledgers_root: Path | None = None,
        submit: Callable[[str, str], dict[str, Any]] | None = None,
        workspace_root: Path | None = None,
        memory_action: Callable[[str, str], dict[str, Any]] | None = None,
        reconcile_jobs: bool = True,
    ) -> None:
        super().__init__(address, WebHandler)
        self.state_path = state_path
        self.runs_dir = runs_dir
        self.web_root = web_root
        self.spec = spec
        self.corpus_root = Path(corpus_root) if corpus_root is not None else corpus.corpus_root()
        # 记忆挂在项目根下（`.vsmemory/`），与语料根不是同一个位置：`--corpus`
        # 指向别处时，记忆仍然属于这个项目。判据把两者一起指到临时目录。
        self.workspace_root = (
            Path(workspace_root) if workspace_root is not None else project_root()
        )
        # 结构全景的根与语料根同一个道理：判据要在一个临时目录上跑，
        # 否则会读到、也可能动到用户家目录里那一份。
        self.surveys_root = (
            Path(surveys_root) if surveys_root is not None
            else config_home() / SURVEY_DIRECTORY
        )
        # 登记表与全景同样处理：默认是 `~/.vsagent/ledgers/`（`ledger.ledger_root()`
        # 的同一处），判据指到临时目录，免得把用户那几份产物当成夹具读。
        self.ledgers_root = (
            Path(ledgers_root) if ledgers_root is not None
            else config_home() / LEDGER_DIRECTORY
        )
        self.max_upload = max_upload
        # 入库的**唯一写者**就在这个进程里：应用装配时把它传进来。独立运行的
        # 服务没有这一项（None），`/api/library/ingest` 会明确回 503，而不是
        # 自己开第二个写者。
        self.ingest = ingest
        # 提交任务同样只能转交：真正写状态文件的是应用进程里的那个事件循环。
        # 独立运行的服务没有这一项（None），`/api/tasks` 会明确回 503。
        self.submit = submit
        # 记忆的归档与恢复要改 `entries.json`，而那个 `MemoryStore` 在应用进程里。
        # 网页只把动作交过去，独立运行时没有可交的对象，回 503。
        self.memory_action = memory_action
        self.ingest_lock = threading.Lock()
        self.ingest_state: dict[str, Any] = {
            "running": False,
            "total": 0,
            "done": 0,
            "results": [],
        }
        # 任务台账与语料同根。启动时先把别的进程留下的 running 标成中断：
        # 进程被关掉时没有人能执行清理，只能在这里对账。
        #
        # **只有能起任务的进程才对账**：独立运行的只读服务也读同一份台账，
        # 让它去改状态的话，应用进程正在跑的那一条会被它标成中断。
        self.jobs = JobStore(self.corpus_root)
        if reconcile_jobs:
            self.jobs.mark_interrupted()


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
        产物下载排在最前面：它发的是字节，不能落到「返回对象再编码成 JSON」那条路上。
        """
        raw = self.server.spec.match_raw(path, method)
        if raw is not None:
            route, matched = raw
            route.handler(self, matched)
            return
        matched = self.server.spec.match(path, method)
        if matched is not None:
            rest = path[len(matched.path):] if matched.prefix else ""
            result = matched.handler(self, rest)
            # 处理函数默认回 200；要别的状态码就返回 `(payload, status)`。
            # 只有「已经受理、但活还没干完」这一类需要它（入库回 202）。
            if isinstance(result, tuple):
                payload, status = result
                self.send_json(payload, int(status))
            else:
                self.send_json(result)
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
