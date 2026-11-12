"""接口判据：核对服务返回的 JSON 与状态文件里的内容是否一致。

判据挑的是那些「接口通了、字段也有，但内容是错的」的地方：
  - 会话条数对了，但消息数或内容单元数对不上
  - 工具单元在，但输入与输出丢了
  - 补丁单元的增删行数是零
  - 运行标识取了 meta.json 里的 run_id，而那个值可能与目录名不一致
  - 状态文件正在被写入时读到写了一半的内容，接口报错

S2 起还有一组语料库判据：上传 / 改名 / 删除，以及三类「本地无鉴权」必须挡住的东西
——跨站 POST、路径穿越、超限。语料根指到 `.probe/web-corpus/`，不碰真实语料。
另有一组切块判据（分页接续、翻过头、`limit` 夹取、删掉的源、索引未建），
它们排在语料组之后，因为那一组会把其中一份删掉。

**服务跑在包内**（`vsagent/core/web/`）。判据原先起的是 `tools/serve_web.py`
这个独立脚本，现在起的是包内服务——那个脚本已经不发请求了，它只做命令行参数解析，
路由全靠导入。判据要盯的是**真正被应用使用的那个实现**，否则测的是壳不是里。

脚本自己准备测试数据、自己起服务、跑完关掉，不依赖已经运行的服务。
状态文件用一份副本，避免影响真实数据。
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core import index, live
from vsagent.core.models import PartType
from vsagent.core.session_manager import SessionManager, default_state_path

FIXTURE_DIR = PROJECT_ROOT / ".probe" / "web-runs"
STATE_COPY = PROJECT_ROOT / ".probe" / "web-state" / "state.toml"

CORPUS_DIR = PROJECT_ROOT / ".probe" / "web-corpus"
SURVEYS_DIR = PROJECT_ROOT / ".probe" / "web-surveys"
LEDGERS_DIR = PROJECT_ROOT / ".probe" / "web-ledgers"
"""登记表根目录指到这里：产物面板与下载读它，判据不该翻用户自己那几份产物。"""
UPLOAD_CAP = 4096
"""上传上限调到 4KB：这样「超限」只要发 5KB，不必真造一个 50MB 的请求。"""

WRITE_HEADER = {"X-VSA-Request": "1"}

# 首选的关联目标会话。这里写的是一个**期望**，实际用谁由副本状态文件决定
# （见 `pick_link_session`）：状态文件是活的，把编号取成固定值，那个会话迟早
# 会不在里面，而「关联断了」与「会话没了」是两回事，判据要能分开报。
LINK_SESSION = "ses_0032"

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"[{'通过' if condition else '失败'}] {name} —— {detail}")


def request(url):
    """发一个请求，返回状态码与解析后的 JSON。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        return error.code, json.loads(body) if body else {}


def request_status(url):
    """只要状态码。结构文件不是 JSON，不能按上面那样解析。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def post_json(url, payload, headers=None):
    """发一个 JSON POST，返回 (状态码, 响应体)。"""
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request_obj = urllib.request.Request(url, data=body, headers=merged, method="POST")
    return send(request_obj)


def post_multipart(url, filename, data, headers=None, boundary="----vsa-boundary"):
    """手搓一个 multipart 请求。只带一个文件，够用且不引额外依赖。"""
    pieces = [
        f"--{boundary}\r\n".encode("utf-8"),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    merged = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    merged.update(headers or {})
    request_obj = urllib.request.Request(
        url, data=b"".join(pieces), headers=merged, method="POST"
    )
    return send(request_obj)


def send(request_obj):
    """发出去，返回 (状态码, JSON)。4xx/5xx 也解析响应体，判据要看那句话。"""
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        try:
            return error.code, json.loads(body) if body else {}
        except ValueError:
            return error.code, {"error": body[:200]}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def prepare_state_copy():
    """复制一份状态文件，测并发时写的是副本。"""
    STATE_COPY.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(default_state_path(), STATE_COPY)
    return STATE_COPY


def prepare_runs():
    """准备运行目录的容器，并放一份受体文件进去。

    web-linked 由真实构建脚本产出，带上会话关联；
    mismatched 由它复制而来，把 meta.json 里的 run_id 改成与目录名不同的值，
    用来核对「标识取目录名」这条设计。

    带会话关联的那一份**不在这里产出**：会话 id 要等副本状态文件读出来才知道
    （见 `pick_link_session` 与 `make_link_run`）。
    """
    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)

    linked = FIXTURE_DIR / "web-linked"
    linked.mkdir(parents=True)
    # 用提交在仓库里的 1IEP 夹具，不依赖 gitignore 的 runs/1iep-demo：
    # 后者在干净 checkout 上不存在，这个脚本会先 FileNotFoundError。
    shutil.copy(
        PROJECT_ROOT / "tests" / "fixtures" / "1iep.pdb",
        linked / "receptor_full.pdb",
    )
    return FIXTURE_DIR


def pick_link_session(state_path):
    """从副本里挑一个真实存在的会话来核对关联。

    挑消息最多的那一个：它同时被后面的内容核对当成核对对象，两处用同一个会话，
    出问题时指向的是同一份数据，不必在两个会话之间来回找。
    """
    reference = load_reference(state_path)
    if not reference:
        return None
    return max(reference.values(), key=lambda session: len(session.messages)).id


def ensure_other_session(state_path, link_session):
    """副本里至少要有一个**没有运行**的会话，返回它的编号。

    「没有对应运行的会话 runId 为空」这条判据原本写死 `ses_0001`。状态文件是活的：
    本机只剩一条会话时，它同时就是关联目标，正反两条断言互相矛盾，报出来的却是
    「关联错了」。所以这里在副本里补一条空会话——判据不依赖真实状态里有几条会话。
    """
    manager = SessionManager(state_path)
    if not manager.load_from_toml():
        return ""
    others = [item for item in manager.sessions.values() if item.id != link_session]
    if others:
        return max(others, key=lambda session: len(session.messages)).id
    extra = manager.create_session("（判据用：没有运行的会话）")
    manager.save_to_toml(state_path)
    return extra.id


def make_link_run(runs_dir, session_id):
    """用真实构建脚本产出 web-linked，并就地复制一份 mismatched。"""
    linked = runs_dir / "web-linked"
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "build_demo_data.py"),
            "--run-dir", str(linked),
            "--session-id", session_id,
        ],
        check=True,
        capture_output=True,
    )

    mismatched = runs_dir / "mismatched"
    shutil.copytree(linked, mismatched)
    meta_path = mismatched / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["run_id"] = "另一个名字"
    meta["provenance"].pop("session_id", None)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return runs_dir


def start_service(state_path, runs_dir, port):
    """起一个**包内服务**并等它就绪。

    用 `-m vsagent.core.web` 起，走的是应用里同一个 `WebService`——
    起独立脚本就测不到「路由只有一份」这件事了。

    服务的输出写进日志文件。写进管道而不读会出事：每处理一次请求，
    BaseHTTPRequestHandler 就往 stderr 写一行，管道缓冲区满了之后服务工作
    线程会一直阻塞在写日志上，之后所有请求都超时。
    """
    log_path = STATE_COPY.parent / "serve.log"
    with log_path.open("wb") as log_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m", "vsagent.core.web",
                "--state", str(state_path),
                "--runs", str(runs_dir),
                "--corpus", str(CORPUS_DIR),
                "--surveys", str(SURVEYS_DIR),
                "--ledgers", str(LEDGERS_DIR),
                "--max-upload", str(UPLOAD_CAP),
                "--port", str(port),
            ],
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError(
                f"服务提前退出：{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            request(f"{base}/api/runs")
            return process, base
        except Exception:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError("服务没有在预期时间内就绪")


def load_reference(state_path):
    """直接读状态文件，作为接口读数的参照。"""
    manager = SessionManager(state_path)
    manager.load_from_toml()
    return {session.id: session for session in manager.list_sessions()}


def prepare_ledgers(session_id):
    """给一个会话造一份登记表：一份文件产物、一份目录产物。

    目录那一项用来核对「下载链接只给文件」：目录没有可下载的字节，页面上只能
    列出名字。返回文件产物的字节数，下载那条判据拿它比对。
    """
    if LEDGERS_DIR.exists():
        shutil.rmtree(LEDGERS_DIR)
    files = LEDGERS_DIR / "files"
    files.mkdir(parents=True)
    target = files / "artifact-1.json"
    data = json.dumps({"target": "P00533", "note": "测试产物"}, ensure_ascii=False).encode("utf-8")
    target.write_bytes(data)

    bundle = files / "docking-4"
    bundle.mkdir()
    (bundle / "poses.pdbqt").write_text("MODEL 1\nENDMDL\n", encoding="utf-8")

    snapshot = {
        "version": 1,
        "session_id": session_id,
        "current_receptor": {"kind": "receptor", "seq": 1},
        "counters": {"receptor": 1, "ligandset": 0, "artifact": 2},
        "archived": [],
        "entries": [
            {
                "handle": {"kind": "artifact", "seq": 1},
                "path": str(target),
                "summary": "靶点 P00533 · 测试产物",
                "fingerprint": "",
                "file_state": "ok",
                "parents": [],
                "meta": {"source": "test"},
                "created_at": 1700000000.0,
            },
            {
                "handle": {"kind": "artifact", "seq": 2},
                "path": str(bundle),
                "summary": "对接产物 · 测试目录",
                "fingerprint": "",
                "file_state": "ok",
                "parents": [{"kind": "artifact", "seq": 1}],
                "meta": {"source": "test"},
                "created_at": 1700000001.0,
            },
        ],
    }
    (LEDGERS_DIR / f"{session_id}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    return len(data)


def request_bytes(url):
    """取原始字节。下载接口回的不是 JSON，`request()` 解析不了。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def pick_event_session(state_path):
    """副本里事件最多的会话。事件、片段、产物三组判据都用它。"""
    reference = load_reference(state_path)
    return max(reference.values(), key=lambda session: len(session.events)).id


def run_event_checks(base, state_path, session_id):
    """事件、片段、产物三条接口：条数、区间、下载。

    区间那几条盯的是「切点算错一个单元就串段」：相邻事件必须首尾相接，
    各段单元数加起来必须正好等于整段。这类错在页面上只表现为「前一个工具调用
    跑到下一个主题里」，肉眼很难发现。
    """
    reference = load_reference(state_path)
    status, listing = request(f"{base}/api/sessions")
    sessions = listing["sessions"]
    expected_events = {item.id: len(item.events) for item in reference.values()}
    check(
        "会话列表带上事件数，且与状态文件一致",
        status == 200
        and all(item["eventCount"] == expected_events.get(item["id"], -1) for item in sessions),
        f"{[(item['id'], item['eventCount']) for item in sessions][:5]}…",
    )

    target = next((item for item in sessions if item["id"] == session_id), None)
    if target is None:
        check("事件判据的目标会话在清单里", False, session_id)
        return
    if target["eventCount"] == 0:
        print("状态文件里没有带事件的会话，跳过事件与片段两组判据")
        return

    origin = reference[target["id"]]
    status, payload = request(f"{base}/api/sessions/{target['id']}/events")
    events = payload.get("events") or []
    check(
        "事件条数与状态文件一致",
        status == 200 and len(events) == len(origin.events),
        f"{target['id']} 接口 {len(events)} 条，状态文件 {len(origin.events)} 条",
    )
    check(
        "任务卡的目标与状态文件一致",
        payload.get("task", {}).get("goal") == origin.task_state.goal,
        f"目标 {payload.get('task', {}).get('goal', '')[:40]!r}",
    )
    check(
        "每条事件的步数与状态文件一致",
        [len(item["steps"]) for item in events] == [len(item.steps) for item in origin.events],
        f"接口 {[len(item['steps']) for item in events][:6]}…",
    )
    check(
        "事件带着切点与产物名",
        all(item["cut"].get("message") is not None and isinstance(item["artifacts"], list) for item in events),
        f"末条切点 {events[-1]['cut']}，产物 {len(events[-1]['artifacts'])} 个",
    )
    check(
        "事件带着显示状态与停止原因",
        [item["status"] for item in events] == [item.status for item in origin.events]
        and all(item.get("status") in {"completed", "failed", "interrupted"} for item in events),
        f"{[item['status'] for item in events][:6]}…",
    )

    # ---- 片段：区间与整段 ----
    _, whole = request(f"{base}/api/sessions/{target['id']}/segment?event=all")
    pieces = []
    for item in events:
        _, piece = request(f"{base}/api/sessions/{target['id']}/segment?event={item['id']}")
        pieces.append((item, piece))

    check(
        "片段的首尾就是事件切点",
        all(
            piece["from"]["message"] is not None
            and piece["to"]["message"] is not None
            and (piece["to"]["message"], piece["to"]["part"]) == (item["cut"]["message"], item["cut"]["part"])
            for item, piece in pieces
        ),
        f"末段 {pieces[-1][1]['to']}，切点 {pieces[-1][0]['cut']}",
    )
    joined = all(
        (pieces[index][1]["to"]["message"], pieces[index][1]["to"]["part"])
        == (pieces[index + 1][1]["from"]["message"], pieces[index + 1][1]["from"]["part"])
        for index in range(len(pieces) - 1)
    )
    check(
        "相邻事件首尾相接，中间没有缝",
        joined and (pieces[0][1]["from"]["message"], pieces[0][1]["from"]["part"]) == (0, 0),
        f"首段起点 {pieces[0][1]['from']}，共 {len(pieces)} 段",
    )
    total_parts = sum(piece["partCount"] for _, piece in pieces)
    check(
        "各事件单元数加起来等于整段（不重不漏）",
        total_parts == whole["partCount"],
        f"各段 {total_parts} 个单元，整段 {whole['partCount']} 个",
    )
    check(
        "整段从第 0 条消息起，条数与状态文件一致",
        whole["from"] == {"message": 0, "part": 0}
        and len(whole["messages"]) == len(origin.messages),
        f"{whole['from']} / {len(whole['messages'])} 条消息",
    )
    check(
        "片段的单元数在消息上是连续的",
        all(
            sum(len(message["parts"]) for message in piece["messages"]) == piece["partCount"]
            for _, piece in pieces
        ),
        f"{[piece['partCount'] for _, piece in pieces][:6]}…",
    )

    status, body = request(f"{base}/api/sessions/{target['id']}/segment?event=evt_9999")
    check(
        "不存在的事件回 404",
        status == 404 and "evt_9999" in body.get("error", ""),
        f"{status} {body.get('error', '')}",
    )

    # ---- 产物：清单、字节、下载 ----
    status, payload = request(f"{base}/api/sessions/{target['id']}/artifacts")
    items = payload.get("items") or []
    snapshot = json.loads((LEDGERS_DIR / f"{target['id']}.json").read_text(encoding="utf-8"))
    check(
        "产物条目与登记表一致",
        status == 200
        and [item["handle"] for item in items]
        == [f"{entry['handle']['kind']}#{entry['handle']['seq']}" for entry in snapshot["entries"]],
        f"接口 {[item['handle'] for item in items]}",
    )
    by_handle = {item["handle"]: item for item in items}
    file_item = by_handle.get("artifact#1")
    dir_item = by_handle.get("artifact#2")
    file_bytes = (LEDGERS_DIR / "files" / "artifact-1.json").stat().st_size
    check(
        "文件产物带字节数与下载链接",
        file_item is not None
        and file_item["bytes"] == file_bytes
        and file_item["download"].endswith("artifact%231"),
        f"{file_item['bytes'] if file_item else '缺失'} 字节，链接 {file_item['download'] if file_item else '无'}",
    )
    check(
        "目录产物不给下载链接（没有可下载的字节）",
        dir_item is not None and dir_item["download"] == "" and dir_item["isDir"] is True,
        f"isDir={dir_item['isDir'] if dir_item else '缺失'} 链接={dir_item['download'] if dir_item else '缺失'!r}",
    )
    check(
        "血缘关系原样带出",
        dir_item is not None and dir_item["parents"] == ["artifact#1"],
        f"parents={dir_item['parents'] if dir_item else '缺失'}",
    )

    status, raw = request_bytes(f"{base}/api/sessions/{target['id']}/artifact/artifact%231")
    check(
        "下载回来的字节与盘上一致",
        status == 200 and raw == (LEDGERS_DIR / "files" / "artifact-1.json").read_bytes(),
        f"{status}，{len(raw)} 字节",
    )

    status, _ = request(f"{base}/api/sessions/{target['id']}/artifact/artifact%2399")
    check("未登记的 handle 回 404", status == 404, str(status))
    status, _ = request(f"{base}/api/sessions/{target['id']}/artifact/..%2F..%2Fstate.toml")
    check("产物路径不接受目录成分", status == 404, str(status))

    # ---- 搜索：标题、主题结论、产物名，三类各一条 ----
    word = origin.events[0].title[:4] if origin.events else ""
    status, found = request(f"{base}/api/search?q={urllib.parse.quote(word)}")
    topics = found.get("topics") or []
    check(
        "搜索能按主题标题命中",
        status == 200 and bool(topics) and all(word in (item["title"] + item["summary"]) for item in topics),
        f"用「{word}」命中 {len(topics)} 条主题",
    )
    status, found = request(f"{base}/api/search?q={urllib.parse.quote('测试产物')}")
    artifacts = found.get("artifacts") or []
    check(
        "搜索能按产物摘要命中，并带上会话编号",
        status == 200
        and [item["handle"] for item in artifacts] == ["artifact#1"]
        and artifacts[0]["sessionId"] == session_id,
        f"{[(item['handle'], item['sessionId']) for item in artifacts]}",
    )
    status, found = request(f"{base}/api/search?q=")
    check(
        "空词不返回任何结果（不把全库铺出来）",
        status == 200 and not found["sessions"] and not found["topics"] and not found["artifacts"],
        f"{found}",
    )


def write_synthetic_trace(path, session_id, messages, legacy=False):
    """造一份最小的 trace。

    只写分析要用的四类事件：消息写入、轮末、装配读数与状态。时间戳按消息顺序
    铺开，每一轮的装配读数落在该轮的起止之间。判据要核的是「接口有没有把
    trace 里的数读进来」，不是 trace 写得好不好。
    """
    base = 1_700_000_000.0
    lines = []
    for index, message in enumerate(messages):
        at = base + index * 10
        lines.append(
            {
                "at": at,
                "event": "message.add",
                "payload": {"session_id": session_id, "message_id": message.id},
            }
        )
        if message.role != "assistant":
            continue
        lines.append(
            {
                "at": at + 5,
                "event": "turn.done",
                "payload": {"session_id": session_id, "message_id": message.id},
            }
        )
        lines.append(
            {
                "at": at + 2,
                "event": "context.metrics",
                "payload": {
                    "session_id": session_id,
                    "step": 1,
                    "input": 1000,
                    "cached": 800,
                    "output": 100,
                    "cache_hit": 0.8,
                    "total": 1100,
                    "window_used": 0.1,
                    "degraded": [],
                    "events": 1,
                    "evidence_entries": 0,
                    "usage": (
                        {
                            "prefix": 100,
                            "memory": 80,
                            "task": 0,
                            "event": 50,
                            "recent": 200,
                            "evidence": 0,
                            "history": -5,
                        }
                        if legacy
                        else {
                            "prefix": 100,
                            "profile_memory": 20,
                            "project_memory": 30,
                            "task": 0,
                            "event": 50,
                            "recent": 200,
                            "evidence": 0,
                            "history": 10,
                        }
                    ),
                },
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def run_analysis_checks(base, state_path, session_id):
    """会话分析：轮数、trace 有无、逐步读数与文本截断。

    先确认没有 trace 时的形状，再补一份 trace 看读数有没有接上。两种状态都
    要能打开页面：只有最近跑过的会话才有 trace，翻旧会话是常态。
    """
    session = load_reference(state_path)[session_id]
    users = [message for message in session.messages if message.role == "user"]
    trace_path = state_path.parent / "logs" / f"{session_id}.jsonl"
    if trace_path.exists():
        trace_path.unlink()

    status, payload = request(f"{base}/api/sessions/{session_id}/analysis")
    check(
        "分析接口能读到这个会话",
        status == 200 and payload.get("session", {}).get("id") == session_id,
        f"状态 {status}，会话 {payload.get('session', {}).get('id')}",
    )
    check(
        "没有 trace 时轮数仍按用户消息算",
        len(payload.get("turns") or []) == len(users),
        f"分析 {len(payload.get('turns') or [])} 轮，用户消息 {len(users)} 条",
    )
    check(
        "没有 trace 时标明逐步读数缺失",
        payload.get("hasTrace") is False and payload.get("traceBytes") == 0,
        f"hasTrace={payload.get('hasTrace')}，traceBytes={payload.get('traceBytes')}",
    )
    check(
        "没有 trace 时不逐轮重复「缺装配记录」",
        not any(
            "没有 trace 装配记录" in item["text"] for item in payload.get("issues") or []
        ),
        f"{len(payload.get('issues') or [])} 条异常",
    )
    check(
        "分析里的原话与正文不超过 600 字",
        all(
            len(turn["answer_text"]) <= 600 and len(turn["user_text"]) <= 600
            for turn in payload.get("turns") or []
        ),
        f"最长 {max((len(turn['answer_text']) for turn in payload.get('turns') or []), default=0)} 字",
    )
    check(
        "分析的 token 合计与状态文件一致",
        payload["session"]["tokens"]["input"] + payload["session"]["tokens"]["output"]
        == session.total_tokens(),
        f"分析 {payload['session']['tokens']['input'] + payload['session']['tokens']['output']}，"
        f"状态文件 {session.total_tokens()}",
    )

    write_synthetic_trace(trace_path, session_id, session.messages)
    status, payload = request(f"{base}/api/sessions/{session_id}/analysis")
    first = next((turn for turn in payload.get("turns") or [] if turn["has_reply"]), None)
    check(
        "有 trace 时逐步读数跟着来",
        status == 200
        and payload.get("hasTrace") is True
        and bool(first and first["metrics"]),
        f"状态 {status}，首轮 {len(first['metrics']) if first else 0} 条装配记录",
    )
    check(
        "逐步读数取自 trace 的 context.metrics",
        bool(first and first["metrics"])
        and first["metrics"][0]["input"] == 1000
        and first["cache_hit"] == 0.8,
        f"输入 {first['metrics'][0]['input'] if first and first['metrics'] else '—'}，"
        f"命中 {first['cache_hit'] if first else '—'}",
    )
    check(
        "每一步都带出上下文八层的占用",
        bool(first and first["metrics"])
        and set(first["metrics"][0]["usage"])
        == {
            "prefix",
            "profile_memory",
            "project_memory",
            "task",
            "event",
            "recent",
            "evidence",
            "history",
        },
        f"{sorted(first['metrics'][0]['usage']) if first and first['metrics'] else '—'}",
    )

    # 旧 trace 的聚合 memory 单独保留成兼容层，负值截成 0；
    # 不能把旧数据里不存在的习惯 / 项目事实拆分凭空造出来。
    write_synthetic_trace(trace_path, session_id, session.messages, legacy=True)
    status, payload = request(f"{base}/api/sessions/{session_id}/analysis")
    legacy_first = next(
        (turn for turn in payload.get("turns") or [] if turn["has_reply"]), None
    )
    legacy_usage = (
        legacy_first["metrics"][0]["usage"]
        if legacy_first and legacy_first["metrics"]
        else {}
    )
    check(
        "旧口径的聚合记忆保留为兼容层，负值截成 0",
        status == 200
        and set(legacy_usage)
        == {
            "prefix",
            "profile_memory",
            "project_memory",
            "task",
            "event",
            "recent",
            "evidence",
            "history",
            "memory",
        }
        and legacy_usage.get("memory") == 80
        and legacy_usage.get("history") == 0
        and legacy_usage.get("profile_memory") == 0
        and legacy_usage.get("project_memory") == 0,
        f"{legacy_usage}",
    )

    status, _ = request(f"{base}/api/sessions/ses_9999/analysis")
    check("不存在的会话没有分析", status == 404, f"状态 {status}")


def run_live_checks(base, state_path, session_id):
    """实时快照：没有文件、正在跑、过期三态，以及编号不合法时挡住。

    这一条不读状态文件，所以它给的结论不能与状态文件比对；判据比的是
    「文件里写了什么、接口原样带出来」。
    """
    target = live.path_for(state_path, session_id)
    target.unlink(missing_ok=True)

    status, body = request(f"{base}/api/sessions/{session_id}/live")
    check(
        "没有快照时回「没在跑」，不报错",
        status == 200 and body.get("present") is False and body.get("running") is False,
        f"{status} {body}",
    )

    now = time.time()
    payload = {
        "sessionId": session_id,
        "running": True,
        "updatedAt": now,
        "pid": 1,
        "eventCount": 4,
        "topicIndex": 5,
        "title": "对接中",
        "steps": [{"seq": 1, "intent": "dock(x=1)", "tools": ["dock"], "result": "成功", "issues": [], "status": "completed"}],
        "current": {"phase": "tool", "tool": "analyze_pose", "status": "running", "preview": "第一行", "since": now - 3, "calls": 1},
        "tools": ["dock", "analyze_pose"],
        "calls": 2,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    status, body = request(f"{base}/api/sessions/{session_id}/live")
    check(
        "正在跑的时候带出主题号、当前工具与首行结果",
        status == 200
        and body.get("present") is True
        and body.get("running") is True
        and body.get("stale") is False
        and body.get("topicIndex") == 5
        and body.get("current", {}).get("tool") == "analyze_pose"
        and body.get("current", {}).get("preview") == "第一行",
        f"{status} {body.get('topicIndex')} {body.get('current')}",
    )

    payload["updatedAt"] = now - 60
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    status, body = request(f"{base}/api/sessions/{session_id}/live")
    check(
        "一分钟没更新标成可能断开",
        status == 200 and body.get("stale") is True,
        f"{status} stale={body.get('stale')}",
    )

    payload["running"] = False
    payload["current"] = None
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    status, body = request(f"{base}/api/sessions/{session_id}/live")
    check(
        "轮末标成不跑了，页面据此去重取主题",
        status == 200 and body.get("running") is False and body.get("present") is True,
        f"{status} running={body.get('running')}",
    )

    status, _ = request(f"{base}/api/sessions/..%2Fstate/live")
    check("快照编号不接受目录成分", status == 404, str(status))
    target.unlink(missing_ok=True)


def run_checks(base, state_path, runs_dir, link_session, other_session):
    status, listing = request(f"{base}/api/sessions")
    reference = load_reference(state_path)
    sessions = listing["sessions"]

    check(
        "会话列表条数与状态文件一致",
        status == 200 and len(sessions) == len(reference),
        f"接口 {len(sessions)} 条，状态文件 {len(reference)} 条",
    )

    stamps = [item["updatedAt"] for item in sessions]
    check(
        "列表按更新时间倒序",
        all(stamps[i] >= stamps[i + 1] for i in range(len(stamps) - 1)),
        f"首尾 {stamps[0]:.0f} → {stamps[-1]:.0f}",
    )

    # 拿消息最多的那个会话核对内容，条数、工具单元、补丁单元一起看。
    biggest = max(sessions, key=lambda item: item["messageCount"])
    origin = reference[biggest["id"]]
    status, detail = request(f"{base}/api/sessions/{biggest['id']}")

    check(
        "最大会话的消息条数一致",
        status == 200 and len(detail["messages"]) == len(origin.messages),
        f"{biggest['id']} 接口 {len(detail['messages'])} 条，状态文件 {len(origin.messages)} 条",
    )

    # 参照按单元类型数，与接口用的是同一套判定。
    # 按字段是否为空去数会多算：补丁单元的增删行数可以是 0，
    # 而工具单元之外也会有 tool_name 有值的记录。
    expected_tools = [
        part
        for message in origin.messages
        for part in message.parts
        if part.type is PartType.TOOL
    ]
    got_tools = [
        part
        for message in detail["messages"]
        for part in message["parts"]
        if part["type"] == "tool"
    ]
    check(
        "工具单元条数一致",
        len(got_tools) == len(expected_tools),
        f"接口 {len(got_tools)} 个，状态文件 {len(expected_tools)} 个",
    )
    check(
        "工具单元的输入与输出都在",
        all(part["toolName"] for part in got_tools)
        # `toolInput` 只要求是**字典**，不要求非空：有工具本来就不收参数
        # （`list_workspace` 的入参全是可选的），空字典是它正确的输入而不是
        # 丢了字段。按「非空」判会把这一类合法调用当成缺陷，而字段真的丢了
        # 时它是 `None`——下面两处要盯的是这个。
        and all(isinstance(part["toolInput"], dict) for part in got_tools)
        and all(part["toolOutput"] for part in got_tools),
        f"{len(got_tools)} 个工具单元，输入不是字典的 "
        f"{sum(1 for p in got_tools if not isinstance(p['toolInput'], dict))} 个，"
        f"缺输出 {sum(1 for p in got_tools if not p['toolOutput'])} 个",
    )

    expected_patches = [
        part
        for message in origin.messages
        for part in message.parts
        if part.type is PartType.PATCH
    ]
    got_patches = [
        part
        for message in detail["messages"]
        for part in message["parts"]
        if part["type"] == "patch"
    ]
    check(
        "补丁单元的增删行数一致",
        len(got_patches) == len(expected_patches)
        and all(
            (got["patchAdded"], got["patchRemoved"]) == (want.patch_added, want.patch_removed)
            for got, want in zip(got_patches, expected_patches)
        ),
        f"接口 {len(got_patches)} 个补丁，状态文件 {len(expected_patches)} 个",
    )

    check(
        "内容单元的字段齐全",
        all(
            set(part.keys()) >= {
                "id", "type", "text", "toolName", "toolStatus", "toolInput",
                "toolOutput", "filePath", "patchAdded", "patchRemoved",
            }
            for message in detail["messages"]
            for part in message["parts"]
        ),
        f"{sum(len(m['parts']) for m in detail['messages'])} 个单元逐个核对字段",
    )

    # ---- 运行清单 ----
    status, runs_payload = request(f"{base}/api/runs")
    runs = runs_payload["runs"]
    directories = sorted(
        item.name
        for item in runs_dir.iterdir()
        if item.is_dir() and (item / "meta.json").exists()
    )
    check(
        "运行清单条数与目录数一致",
        status == 200 and sorted(item["id"] for item in runs) == directories,
        f"接口 {[item['id'] for item in runs]}，目录 {directories}",
    )

    mismatched = next((item for item in runs if item["id"] == "mismatched"), None)
    check(
        "运行标识取目录名",
        mismatched is not None
        and mismatched["id"] == "mismatched"
        and mismatched["runId"] == "另一个名字",
        f"id={mismatched['id'] if mismatched else '缺失'}，"
        f"meta.json 里的 run_id={mismatched['runId'] if mismatched else '缺失'}",
    )

    status = request_status(f"{base}/runs/mismatched/receptor.pdb")
    check(
        "目录名与 run_id 不一致时静态文件仍按目录名取到",
        status == 200,
        f"GET /runs/mismatched/receptor.pdb 返回 {status}",
    )

    # ---- 会话与运行的关联 ----
    linked = next((item for item in runs if item["id"] == "web-linked"), None)
    check(
        "运行带上了产出它的会话",
        linked is not None and linked["sessionId"] == link_session,
        f"sessionId={linked['sessionId'] if linked else '缺失'}，应为 {link_session}",
    )

    # 先确认关联目标真的在会话清单里。不在时下面那条会在「一个会话都没带运行」
    # 的情况下同样成立（`len(with_run) == 1` 不成立，但报出来的原因会指向别处），
    # 单列一条能把「会话没了」与「关联断了」区分开。
    check(
        "关联目标会话在清单里",
        any(item["id"] == link_session for item in sessions),
        f"{link_session} 在接口返回的 {len(sessions)} 条会话中"
        f"{'存在' if any(item['id'] == link_session for item in sessions) else '不存在'}",
    )

    with_run = [item for item in sessions if item["runId"]]
    check(
        "只有对应会话拿到了运行",
        len(with_run) == 1
        and with_run[0]["id"] == link_session
        and with_run[0]["runId"] == "web-linked",
        f"带运行的会话 {[(item['id'], item['runId']) for item in with_run]}，"
        f"应为 [('{link_session}', 'web-linked')]",
    )

    status, other = request(f"{base}/api/sessions/{other_session}")
    check(
        "没有对应运行的会话 runId 为空",
        status == 200 and other["runId"] is None,
        f"{other_session} runId={other['runId']}",
    )

    # ---- 反向核对 ----
    status, missing = request(f"{base}/api/sessions/ses_9999")
    check(
        "不存在的会话返回 404",
        status == 404 and "error" in missing,
        f"返回 {status}，内容 {missing.get('error', '')}",
    )

    status, traversal = request(f"{base}/runs/%2e%2e/%2e%2e/.env")
    check(
        "路径穿越被拒绝",
        status == 403,
        f"返回 {status}，内容 {traversal.get('error', '')}",
    )


def prepare_surveys():
    """造一份结构全景产物，够核对接口形状与页面取数即可。

    **不放在 `~/.vsagent/surveys`**：判据不该往用户家目录里写东西，
    服务用 `--surveys` 指到这里（与 `--corpus` 同一个理由）。
    """
    if SURVEYS_DIR.exists():
        shutil.rmtree(SURVEYS_DIR)
    target = SURVEYS_DIR / "Q15858-survey-test0001"
    target.mkdir(parents=True)
    payload = {
        "format": 1,
        "accession": "Q15858",
        "query": {"method": "", "ligand_class": "any", "limit": 400},
        "total": 3,
        "fetched": 3,
        "groups": [
            {
                "code": "9SR",
                "name": "Tetrodotoxin",
                "names": ["Tetrodotoxin"],
                "kind": "non-polymer",
                "weight": 319.268,
                "count": 1,
                "title_hits": 1,
                "best": {"pdb_id": "6J8I", "resolution": 3.2, "method": "ELECTRON MICROSCOPY"},
                "pdb_ids": ["6J8I"],
            },
            {
                "code": "Y01",
                "name": "CHOLESTEROL HEMISUCCINATE",
                "names": [],
                "kind": "non-polymer",
                "weight": 486.726,
                "count": 1,
                "title_hits": 0,
                "best": {"pdb_id": "7W9K", "resolution": 2.2, "method": "ELECTRON MICROSCOPY"},
                "pdb_ids": ["7W9K"],
            },
        ],
        "polymers": [
            {
                "description": "Sodium channel subunit beta-1",
                "uniprot_ids": ["Q07699"],
                "count": 2,
                "pdb_ids": ["6J8I", "7W9K"],
            }
        ],
        "additives": [{"code": "NAG", "name": "N-acetylglucosamine", "count": 2}],
        "apo": [{"pdb_id": "2EB2", "resolution": 2.5, "method": "X-RAY DIFFRACTION"}],
        "missing": [],
        "entries": [
            {
                "pdb_id": "6J8I",
                "title": "Nav1.7 with tetrodotoxin",
                "resolution": 3.2,
                "method": "ELECTRON MICROSCOPY",
                "ligands": ["9SR"],
                "additives": ["NAG"],
                "other_polymers": ["Sodium channel subunit beta-1"],
            },
            {
                "pdb_id": "7W9K",
                "title": "Nav1.7-beta1-beta2 complex",
                "resolution": 2.2,
                "method": "ELECTRON MICROSCOPY",
                "ligands": ["Y01"],
                "additives": ["NAG"],
                "other_polymers": ["Sodium channel subunit beta-1"],
            },
            {
                "pdb_id": "2EB2",
                "title": "EGFR kinase domain",
                "resolution": 2.5,
                "method": "X-RAY DIFFRACTION",
                "ligands": [],
                "additives": [],
                "other_polymers": [],
            },
        ],
        "names_unavailable": [],
    }
    (target / "survey.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return SURVEYS_DIR


def run_survey_checks(base):
    """结构全景接口：读得到、形状对、不存在回 404、标识不合法回 400。"""
    status, payload = request(f"{base}/api/survey/Q15858-survey-test0001")
    check(
        "结构全景读得到",
        status == 200 and payload.get("accession") == "Q15858",
        f"{status} / {payload.get('accession')}",
    )
    check(
        "全景带着配体组与全部条目",
        len(payload.get("groups") or []) == 2 and len(payload.get("entries") or []) == 3,
        f"组 {len(payload.get('groups') or [])}，条目 {len(payload.get('entries') or [])}",
    )
    check(
        "全景里的常用名原样转出",
        (payload.get("groups") or [{}])[0].get("name") == "Tetrodotoxin",
        str((payload.get("groups") or [{}])[0].get("name")),
    )

    status, _ = request(f"{base}/api/survey/not-a-survey")
    check("不存在的全景回 404", status == 404, str(status))

    status, _ = request(f"{base}/api/survey/..%2Fetc")
    check("全景标识不合法回 400", status == 400, str(status))


def prepare_corpus():
    """造一份最小语料：一份文献、一份网页、一个空 inbox，再建一次索引。

    建索引这一步是**判据的一部分**：列表里的「块数」来自只读打开的那份库，
    库不存在时页面也要能显示列表（只是没有块数），所以两条路都要能走。
    """
    if CORPUS_DIR.exists():
        shutil.rmtree(CORPUS_DIR)

    doc = CORPUS_DIR / "doc" / "aaaaaaaaaa"
    doc.mkdir(parents=True)
    (doc / "source.md").write_text("# 测试文献\n\nEpidermal growth factor receptor.\n", encoding="utf-8")
    (doc / "text.jsonl").write_text(
        json.dumps({"page": 1, "text": "# 测试文献\n\nEpidermal growth factor receptor."},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (doc / "meta.json").write_text(
        json.dumps(
            {
                "id": "aaaaaaaaaa", "kind": "doc", "title": "测试文献",
                "file": "source.md", "pages": 1, "structure": "markdown",
                "chars": 45, "depth": "full", "added_at": 1.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    web = CORPUS_DIR / "web" / "bbbbbbbbbb"
    web.mkdir(parents=True)
    (web / "content.md").write_text("# 测试网页\n\nSome web content about inhibitors.\n", encoding="utf-8")
    (web / "meta.json").write_text(
        json.dumps(
            {
                "id": "bbbbbbbbbb", "kind": "web", "title": "测试网页",
                "url": "https://example.test/page", "depth": "full", "added_at": 2.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 第三份是**长文献**：45 节，切块数必然多于一页。切块面板的分页要有东西可翻，
    # 否则「下一页」那几条判据永远只测到一张空表。
    long_doc = CORPUS_DIR / "doc" / "cccccccccc"
    long_doc.mkdir(parents=True)
    sections = []
    for section_no in range(1, 46):
        body = " ".join(
            f"Paragraph {section_no}.{step} discusses epidermal growth factor receptor signalling "
            f"in non-small cell lung cancer."
            for step in range(1, 13)
        )
        sections.append(f"## Section {section_no}\n\n{body}\n")
    long_text = "# 长文献\n\n" + "\n".join(sections)
    (long_doc / "source.md").write_text(long_text, encoding="utf-8")
    (long_doc / "text.jsonl").write_text(
        json.dumps({"page": 1, "text": long_text}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (long_doc / "meta.json").write_text(
        json.dumps(
            {
                "id": "cccccccccc", "kind": "doc", "title": "长文献",
                "file": "source.md", "pages": 1, "structure": "markdown",
                "chars": len(long_text), "depth": "full", "added_at": 3.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (CORPUS_DIR / "inbox").mkdir(parents=True, exist_ok=True)
    index.reindex(CORPUS_DIR)
    return CORPUS_DIR


def run_library_checks(base):
    """语料库：列表读数、投递、改名、删除，以及三类必须挡住的东西。"""
    origin = f"http://127.0.0.1:{base.rsplit(':', 1)[-1]}"
    inbox = CORPUS_DIR / "inbox"
    upload_url = f"{base}/api/library/upload"

    status, payload = request(f"{base}/api/library")
    items = payload.get("items", [])
    check(
        "语料列表与目录一致",
        status == 200
        and {item["id"] for item in items}
        == {"doc:aaaaaaaaaa", "web:bbbbbbbbbb", "doc:cccccccccc"},
        f"接口 {[item['id'] for item in items]}",
    )
    check(
        "列表带上了块数（只读打开索引）",
        all(item["chunks"] >= 1 for item in items),
        f"块数 {[(item['id'], item['chunks']) for item in items]}",
    )
    check(
        "开关现状是只读展示",
        payload["config"]["indexWeb"] is True
        and payload["config"]["embedModel"]
        and payload["inbox"]["count"] == 0,
        f"config={payload['config']} inbox={payload['inbox']}",
    )

    # ---- 投递 ----
    status, saved = post_multipart(upload_url, "note.md", b"# hello\n\nbody\n", WRITE_HEADER)
    check(
        "上传落进 corpus/inbox/",
        status == 200 and saved.get("accepted") == 1 and (inbox / "note.md").is_file(),
        f"{status} {saved}",
    )
    status, payload = request(f"{base}/api/library")
    check(
        "待入库计数从 inbox 现数",
        payload["inbox"]["count"] == 1 and payload["inbox"]["names"] == ["note.md"],
        f"inbox={payload['inbox']}",
    )

    post_multipart(upload_url, "note.md", b"# second\n", WRITE_HEADER)
    check(
        "同名不覆盖，另存一份",
        (inbox / "note.md").read_bytes() == b"# hello\n\nbody\n" and (inbox / "note-1.md").is_file(),
        f"inbox 里的文件 {sorted(item.name for item in inbox.iterdir())}",
    )

    # ---- 跨站：本地服务不做鉴权，这两道门必须挡住 ----
    status, body = post_multipart(upload_url, "evil.md", b"x")
    check(
        "不带自定义头的 POST 被拒（换个网站发不出这个头）",
        status == 403 and "X-VSA-Request" in body.get("error", ""),
        f"{status} {body.get('error', '')}",
    )
    status, body = post_multipart(
        upload_url, "evil.md", b"x", {**WRITE_HEADER, "Origin": "http://evil.example"}
    )
    check("异站 Origin 被拒", status == 403, f"{status} {body.get('error', '')}")
    status, _ = post_multipart(
        upload_url, "same-origin.md", b"# ok\n", {**WRITE_HEADER, "Origin": origin}
    )
    check("同源 Origin 放行", status == 200, f"{status}")

    # ---- 路径穿越：文件名里的目录要剥掉，id 里的路径根本不解析 ----
    status, _ = post_multipart(upload_url, "../../escape.md", b"# escaped\n", WRITE_HEADER)
    check(
        "文件名里的路径被剥掉，落点仍在 inbox",
        status == 200
        and (inbox / "escape.md").is_file()
        and not (PROJECT_ROOT / ".probe" / "escape.md").exists(),
        f"{status}，inbox={sorted(item.name for item in inbox.iterdir())}",
    )

    status, body = post_multipart(upload_url, "sheet.csv", b"a,b\n", WRITE_HEADER)
    check(
        "不支持的扩展名当场拒绝",
        status == 415 and "只收" in body.get("error", ""),
        f"{status} {body.get('error', '')}",
    )

    status, body = post_multipart(upload_url, "big.md", b"x" * (UPLOAD_CAP + 128), WRITE_HEADER)
    check(
        "超过上限的请求体被拒（413）",
        status == 413,
        f"{status} {body.get('error', '')}",
    )

    # ---- 改名：只写 labels.json ----
    doc_dir = CORPUS_DIR / "doc" / "aaaaaaaaaa"
    status, renamed = post_json(
        f"{base}/api/library/doc:aaaaaaaaaa/rename", {"title": "我改的名字"}, WRITE_HEADER
    )
    check(
        "改名写进 labels.json",
        status == 200
        and (doc_dir / "labels.json").is_file()
        and json.loads((doc_dir / "labels.json").read_text(encoding="utf-8"))["display_title"]
        == "我改的名字",
        f"{status} {renamed}",
    )
    check("改名不动 meta.json", json.loads((doc_dir / "meta.json").read_text(encoding="utf-8"))["title"] == "测试文献")

    status, payload = request(f"{base}/api/library")
    renamed_item = next(item for item in payload["items"] if item["id"] == "doc:aaaaaaaaaa")
    check(
        "列表显示改名后的标题",
        renamed_item["title"] == "我改的名字" and renamed_item["rawTitle"] == "测试文献",
        f"title={renamed_item['title']} raw={renamed_item['rawTitle']}",
    )

    post_json(f"{base}/api/library/doc:aaaaaaaaaa/rename", {"title": ""}, WRITE_HEADER)
    check(
        "空标题恢复原始标题",
        not (doc_dir / "labels.json").exists(),
        f"labels.json 还在：{(doc_dir / 'labels.json').is_file()}",
    )

    status, body = post_json(
        f"{base}/api/library/doc:../../etc/rename", {"title": "x"}, WRITE_HEADER
    )
    check(
        "id 里的路径不被解释",
        status == 400 and "doc:xxxxxxxxxx" in body.get("error", ""),
        f"{status} {body.get('error', '')}",
    )

    # ---- 删除：挪进 .trash，幂等 ----
    web_dir = CORPUS_DIR / "web" / "bbbbbbbbbb"
    status, deleted = post_json(
        f"{base}/api/library/web:bbbbbbbbbb/delete", {}, WRITE_HEADER
    )
    check(
        "删除挪进 .trash（原件可找回）",
        status == 200
        and deleted.get("deleted") is True
        and not web_dir.exists()
        and (CORPUS_DIR / ".trash").is_dir()
        and any((CORPUS_DIR / ".trash").iterdir()),
        f"{status} {deleted}",
    )

    status, again = post_json(
        f"{base}/api/library/web:bbbbbbbbbb/delete", {}, WRITE_HEADER
    )
    check(
        "删除是幂等的：第二次回 200 但没删东西",
        status == 200 and again.get("deleted") is False,
        f"{status} {again}",
    )

    status, payload = request(f"{base}/api/library")
    check(
        "删完列表就少一条",
        all(item["id"] != "web:bbbbbbbbbb" for item in payload["items"]),
        f"{[item['id'] for item in payload['items']]}",
    )

    # ---- 动词与路由 ----
    status, _ = post_json(f"{base}/api/runs", {})
    check("往只读接口 POST 回 405", status == 405, f"{status}")

    status, _ = request(f"{base}/api/library/doc:aaaaaaaaaa/delete")
    check("GET 写接口不会落到静态文件或某个默认动作", status == 404, f"{status}")


def run_chunk_checks(base):
    """切块面板：分页、边界、非法标识，以及「索引没建时给空表」。

    这一组在 `run_library_checks` **之后**跑：那一组会把 `web:bbbbbbbbbb` 删掉，
    正好用来核对「删掉的源查不到切块」——切块与列表必须同时消失，否则页面上
    点得进一个已经不在的源。
    """
    from vsagent.core.web.routes import CHUNK_PAGE, CHUNK_PAGE_MAX, chunks_payload

    status, payload = request(f"{base}/api/library/doc:cccccccccc/chunks")
    items = payload.get("items") or []
    check(
        "切块默认给一页，并带上总块数",
        status == 200
        and payload.get("total", 0) > CHUNK_PAGE
        and len(items) == CHUNK_PAGE
        and payload.get("offset") == 0
        and payload.get("limit") == CHUNK_PAGE,
        f"{status} total={payload.get('total')} 本页 {len(items)} 块",
    )
    check(
        "每块都有编号、正文，字数与正文一致",
        all(
            item["no"] >= 1 and item["text"] and item["chars"] == len(item["text"])
            for item in items
        ),
        f"首块 #{items[0]['no']} {items[0]['chars']} 字" if items else "一块都没有",
    )
    check(
        "块按原文顺序排（编号从 1 连续递增）",
        [item["no"] for item in items] == list(range(1, len(items) + 1)),
        f"{[item['no'] for item in items][:5]}…",
    )

    status, second = request(f"{base}/api/library/doc:cccccccccc/chunks?offset={CHUNK_PAGE}")
    tail = second.get("items") or []
    check(
        "第二页从上一页之后接着排，不重不漏",
        status == 200
        and bool(tail)
        and tail[0]["no"] == CHUNK_PAGE + 1
        and second.get("total") == payload.get("total"),
        f"{status} 首块 #{tail[0]['no'] if tail else '—'} total={second.get('total')}",
    )

    status, beyond = request(f"{base}/api/library/doc:cccccccccc/chunks?offset=100000")
    check(
        "翻过头给空表而不是报错，总数照旧",
        status == 200
        and beyond.get("items") == []
        and beyond.get("total") == payload.get("total"),
        f"{status} items={beyond.get('items')} total={beyond.get('total')}",
    )

    status, capped = request(f"{base}/api/library/doc:cccccccccc/chunks?limit=100000")
    check(
        "limit 被夹到上限，不按客户端说的给",
        status == 200 and capped.get("limit") == CHUNK_PAGE_MAX,
        f"{status} limit={capped.get('limit')}",
    )

    status, body = request(f"{base}/api/library/doc:ZZZ/chunks")
    check(
        "切块接口的 id 也不解释路径（形状不对就是 400）",
        status == 400 and "doc:xxxxxxxxxx" in body.get("error", ""),
        f"{status} {body.get('error', '')}",
    )

    status, body = request(f"{base}/api/library/web:bbbbbbbbbb/chunks")
    check(
        "删掉的源查不到切块（列表与切块同时消失）",
        status == 404,
        f"{status} {body.get('error', '')}",
    )

    # 索引还没建：`open_readonly` 返回 None，这一层要给空表加一句原因，而不是 500。
    empty = PROJECT_ROOT / ".probe" / "web-corpus-empty"
    shutil.rmtree(empty, ignore_errors=True)
    empty.mkdir(parents=True)
    try:
        payload = chunks_payload(empty, "doc", "aaaaaaaaaa")
        check(
            "索引还没建时给空表并说明原因",
            payload["items"] == [] and payload["total"] == 0 and "索引" in payload.get("reason", ""),
            f"{payload}",
        )
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def run_ingest_checks(base, corpus_dir):
    """入库：网页只转交，不自己写索引；独立服务拒绝；重复投递幂等。

    `base` 是**独立进程**起的服务（`-m vsagent.core.web`）——它没有入库
    回调，必须回 503，而不是退化成第二个写者。真正入库那一组在这里另起一个
    带 `ingest` 的 `WebServer`（同进程、同线程模型，与应用里一样）。
    """
    import threading

    from vsagent.core.agent.domain.corpus import ingest_files
    from vsagent.core.web.routes import WebServer

    status, payload = post_json(f"{base}/api/library/ingest", {}, WRITE_HEADER)
    check(
        "独立进程不接入库：明确 503 而不是自己写索引",
        status == 503 and "入库" in str(payload.get("error", "")),
        f"{status} {payload.get('error', '')}",
    )

    port = free_port()
    server = WebServer(
        ("127.0.0.1", port),
        STATE_COPY,
        FIXTURE_DIR,
        PROJECT_ROOT / "web",
        corpus_root=corpus_dir,
        ingest=ingest_files,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = f"http://127.0.0.1:{port}"

    def wait_done(timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, state = request(f"{local}/api/library")
            if not (state.get("ingest") or {}).get("running"):
                return state
            time.sleep(0.1)
        return {}

    try:
        status, payload = post_json(
            f"{local}/api/library/ingest", {"names": ["../doc/aaaaaaaaaa/meta.json"]}, WRITE_HEADER
        )
        check(
            "入库只认 inbox 里的文件名：带 ../ 的直接拒",
            status == 400,
            f"{status} {payload.get('error', '')}",
        )

        status, payload = post_json(
            f"{local}/api/library/ingest", {"names": ["not-here.pdf"]}, WRITE_HEADER
        )
        check(
            "入库只认已经在 inbox 里的文件",
            status == 404,
            f"{status} {payload.get('error', '')}",
        )

        # 前面的投递判据会在 inbox 里留几个文件；这一组要精确知道入了哪几份，
        # 所以先清空——它测的是「入库」这件事，不是上一组留下的残渣。
        inbox = corpus_dir / "inbox"
        if inbox.exists():
            shutil.rmtree(inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        sample = "# 入库测试\n\nGefitinib is an EGFR inhibitor.\n"
        (inbox / "ingest-me.md").write_text(sample, encoding="utf-8")
        status, payload = post_json(
            f"{local}/api/library/ingest", {"names": ["ingest-me.md"]}, WRITE_HEADER
        )
        check("入库请求当场返回 202，不等切块", status == 202 and payload.get("started") == 1,
              f"{status} {payload}")

        state = wait_done()
        first = (state.get("ingest") or {}).get("results") or []
        check(
            "后台跑完，结果是新增",
            first and first[0].get("status") == "new",
            f"{first[:1]}",
        )
        check("入库之后 inbox 清空", (state.get("inbox") or {}).get("count") == 0,
              f"{(state.get('inbox') or {})}")
        # 标题取文件自己的（Markdown 没有内嵌标题）→ 退回文件名，所以是 ingest-me。
        added = [item for item in state.get("items", []) if item.get("title") == "ingest-me"]
        check(
            "新的一份出现在列表里，并且有块与向量",
            added and int(added[0].get("chunks") or 0) > 0,
            f"{added[:1]}",
        )
        before = len(state.get("items", []))

        (inbox / "same-again.md").write_text(sample, encoding="utf-8")
        post_json(f"{local}/api/library/ingest", {"names": ["same-again.md"]}, WRITE_HEADER)
        state = wait_done()
        last = ((state.get("ingest") or {}).get("results") or [])[-1]
        check(
            "同样的内容再投一次是「重复」，不产生第二份",
            last.get("status") == "duplicate" and len(state.get("items", [])) == before,
            f"{last} / {len(state.get('items', []))} 份",
        )
    finally:
        server.shutdown()
        server.server_close()


def run_write_guard_checks(base):
    """每条标了 `writes` 的路由，缺自定义头时都要回 403。

    按 `SPEC` 逐条走而不是手写一串路径：新加一个写入口忘了接守卫，这条当场
    报出来。前缀路由按它声明的路径原样 POST，处理器第一行就是守卫。
    """
    from vsagent.core.web.routes import SPEC

    misses = []
    for route in SPEC.json_routes:
        if not route.writes:
            continue
        status, body = post_json(f"{base}{route.path}", {})
        if status != 403:
            misses.append(f"{route.path} → {status}")
    check(
        "所有写路由缺 X-VSA-Request 时都回 403",
        not misses,
        "；".join(misses) or f"{sum(1 for r in SPEC.json_routes if r.writes)} 条",
    )

    # Origin 不对时同样拒绝。只挑一条有代表性的：守卫是同一份代码。
    status, body = post_json(
        f"{base}/api/library/folders",
        {"name": "x"},
        {**WRITE_HEADER, "Origin": "https://evil.example"},
    )
    check(
        "Origin 与 Host 不同时同样 403",
        status == 403 and "跨站" in str(body.get("error", "")),
        f"{status} {body.get('error', '')}",
    )


def run_knowledge_read_checks(base, corpus_dir):
    """M1 的五个只读接口：形状、筛选、分页边界、非法参数。"""
    status, stats = request(f"{base}/api/corpus/stats")
    check(
        "统计接口：源数、块数、向量覆盖、占用",
        status == 200
        and stats.get("sources", 0) >= 3
        and stats.get("chunks", 0) > 0
        and 0.0 <= float(stats.get("vectorCoverage") or 0) <= 1.0
        and stats.get("bytes", 0) > 0,
        f"{status} {stats}",
    )

    status, listing = request(f"{base}/api/library?kind=doc&sort=title")
    titles = [item["title"] for item in listing.get("items") or []]
    check(
        "语料列表按类型筛选、按标题排序",
        status == 200
        and titles == sorted(titles)
        and all(item["kind"] == "doc" for item in listing.get("items") or []),
        f"{titles}",
    )

    status, listing = request(f"{base}/api/library?q={urllib.parse.quote('测试文献')}")
    check(
        "语料列表按关键词过滤（标题或地址）",
        status == 200
        and len(listing.get("items") or []) == 1
        and listing["items"][0]["id"] == "doc:aaaaaaaaaa",
        f"{[item['id'] for item in listing.get('items') or []]}",
    )

    status, paged = request(f"{base}/api/library?limit=1&offset=1")
    check(
        "语料列表分页：一页一条，总数不变",
        status == 200
        and len(paged.get("items") or []) == 1
        and paged.get("limit") == 1
        and paged.get("offset") == 1
        and paged.get("total", 0) >= 3,
        f"{status} total={paged.get('total')} 本页 {len(paged.get('items') or [])}",
    )

    status, capped = request(f"{base}/api/library?limit=99999")
    check(
        "语料列表的 limit 被夹到上限",
        status == 200 and capped.get("limit") == 500,
        f"{status} limit={capped.get('limit')}",
    )

    status, payload = request(f"{base}/api/library/doc:aaaaaaaaaa")
    check(
        "语料详情：元数据、标签、块数与文件",
        status == 200
        and payload.get("id") == "doc:aaaaaaaaaa"
        and payload.get("title") == "测试文献"
        and payload.get("chunks", 0) > 0
        and any(item.get("name") == "source.md" for item in payload.get("files") or []),
        f"{status} {payload.get('title')} {payload.get('chunks')} 块",
    )

    status, payload = request(f"{base}/api/library/doc:ffffffffff")
    check("不存在的语料回 404", status == 404, f"{status} {payload.get('error', '')}")

    status, payload = request(f"{base}/api/library/doc:ZZZ")
    check("语料标识形状不对回 400", status == 400, f"{status} {payload.get('error', '')}")

    status, payload = request(
        f"{base}/api/library/doc:cccccccccc/chunks?q=Paragraph%2044&limit=5"
    )
    hits = payload.get("items") or []
    check(
        "切块可以按关键词过滤",
        status == 200
        and hits
        and all("Paragraph 44" in item["text"] for item in hits)
        and 0 < payload.get("total", 0) <= 5,
        f"{status} total={payload.get('total')} 本页 {len(hits)}",
    )

    status, payload = request(f"{base}/api/knowledge/search?q=epidermal&mode=lexical&limit=2")
    corpus = payload.get("corpus") or []
    check(
        "知识检索：条数受 limit 约束，带上名次与诊断",
        status == 200
        and len(corpus) <= 2
        and all("ranks" in item and "sourceId" in item for item in corpus)
        and "channels" in (payload.get("diagnostics") or {}),
        f"{status} {len(corpus)} 组 {payload.get('diagnostics')}",
    )

    status, payload = request(f"{base}/api/knowledge/search?q=epidermal&mode=nope")
    check(
        "检索模式写错回 400，不退回默认值",
        status == 400 and "模式" in str(payload.get("error", "")),
        f"{status} {payload.get('error', '')}",
    )

    status, payload = request(f"{base}/api/knowledge/search?q=epidermal&source=doc:aaaaaaaaaa")
    check(
        "按源限定检索：命中只来自选中的那一份",
        status == 200
        and all(item["sourceId"] == "doc:aaaaaaaaaa" for item in payload.get("corpus") or []),
        f"{[item['sourceId'] for item in payload.get('corpus') or []]}",
    )

    # 切块面板给的是索引里那一份，检索读的也是那一份。命中正文是组内片段（每块
    # 至多 400 字，多块用换行省略号拼起来），所以拿第一段去比前缀：重切一遍的话
    # 这里对不上。
    hit = next(iter(payload.get("corpus") or []), None)
    if hit:
        snippet = hit["text"].split("\n…\n")[0]
        _, detail = request(f"{base}/api/library/{hit['sourceId']}/chunks?limit=100")
        texts = [item["text"] for item in detail.get("items") or []]
        check(
            "切块面板与检索读的是同一份正文",
            any(text.startswith(snippet) for text in texts),
            f"命中片段 {len(snippet)} 字，面板 {len(texts)} 块",
        )

    status, payload = request(f"{base}/api/knowledge/search?q=epidermal&source=bad")
    check("源标识写错回 400", status == 400, f"{status} {payload.get('error', '')}")

    status, payload = request(f"{base}/api/knowledge/search?q=")
    check("空检索词回 400", status == 400, f"{status} {payload.get('error', '')}")

    status, payload = request(f"{base}/api/ingestion/jobs")
    check(
        "入库任务清单能查（独立服务也读同一份台账）",
        status == 200 and isinstance(payload.get("jobs"), list),
        f"{status} {len(payload.get('jobs') or [])} 条",
    )

    # 引用核对给出三种状态（`ok` / `stale` / `missing`），页面按它分组。换算只写
    # 在 `entry_state` 一处，漏掉 `stale` 时「文件变了」会显示成「有效」。
    from vsagent.core.memory.models import MemoryEntry, MemoryKind
    from vsagent.core.web.services.memory import entry_state

    def state_of(state):
        return entry_state(MemoryEntry(id="mem_x", kind=MemoryKind.FACT, text="x", state=state))

    check(
        "记忆状态：引用已变与引用丢失分得开",
        state_of("stale") == "stale" and state_of("missing") == "missing",
        f"stale → {state_of('stale')}，missing → {state_of('missing')}",
    )


def run_knowledge_write_checks(corpus_dir):
    """M2–M6：标签、文件夹、记忆归档、关联、入库任务、使用记录。

    这一组要一个**带记忆与入库回调的进程内服务**：独立服务按章程把这三类写
    入口回 503，能验的只有「拒绝」，验不到真正的写入。工作区指到 `.probe`
    下的临时目录，记忆夹具由这里造。
    """
    import threading

    from vsagent.core import index as index_module
    from vsagent.core.agent.domain.corpus import ingest_files
    from vsagent.core.memory import MemoryError as MemoryStoreError
    from vsagent.core.memory import MemoryStore
    from vsagent.core.web.routes import WebServer

    workspace = PROJECT_ROOT / ".probe" / "web-workspace"
    shutil.rmtree(workspace, ignore_errors=True)
    memory_dir = workspace / ".vsmemory"
    memory_dir.mkdir(parents=True)

    doc_path = str(corpus_dir / "doc" / "aaaaaaaaaa" / "source.md")
    gone_path = str(workspace / "gone.pdf")
    entries = {
        "entries": [
            {
                "id": "mem_20260101000000_aaaaaa",
                "kind": "fact",
                "text": "测试文献里提到 EGFR 抑制剂。",
                "refs": [doc_path],
                "created_at": 1767225600.0,
                "updated_at": 1767225600.0,
                "state": "ok",
            },
            {
                "id": "mem_20260101000000_bbbbbb",
                "kind": "experience",
                "text": "同一篇文献的另一条记录，引用了一份已经不在的文件。",
                "refs": [doc_path, gone_path],
                "created_at": 1767225601.0,
                "updated_at": 1767225601.0,
                "state": "ok",
            },
        ]
    }
    (memory_dir / "entries.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )

    def memory_action(action, entry_id):
        """应用侧那个回调的最小版本：在同一个进程里改 `MemoryStore`。"""
        store = MemoryStore(workspace)
        store.load()
        try:
            getattr(store, action)(entry_id)
        except MemoryStoreError as error:
            return {"error": str(error), "status": 409}
        return {"id": entry_id, "action": action, "ok": True}

    port = free_port()
    server = WebServer(
        ("127.0.0.1", port),
        STATE_COPY,
        FIXTURE_DIR,
        PROJECT_ROOT / "web",
        corpus_root=corpus_dir,
        ingest=ingest_files,
        workspace_root=workspace,
        memory_action=memory_action,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = f"http://127.0.0.1:{port}"

    def version():
        conn = index_module.connect(corpus_dir)
        try:
            row = conn.execute("SELECT value FROM kv WHERE key = 'vectors'").fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

    try:
        # ---- 标签与文件夹：只写 labels.json / folders.json ----
        before = version()
        status, payload = post_json(
            f"{local}/api/library/doc:aaaaaaaaaa/labels",
            {"tags": ["egfr", "文献"], "favorite": True},
            WRITE_HEADER,
        )
        labels = json.loads(
            (corpus_dir / "doc" / "aaaaaaaaaa" / "labels.json").read_text(encoding="utf-8")
        )
        check(
            "写标签落在 labels.json，索引版本号不变",
            status == 200
            and labels.get("tags") == ["egfr", "文献"]
            and labels.get("favorite") is True
            and version() == before,
            f"{status} {labels} 版本 {before!r} → {version()!r}",
        )

        status, payload = post_json(
            f"{local}/api/library/folders", {"name": "论文"}, WRITE_HEADER
        )
        created = payload.get("folder") or {}
        status2, listing = request(f"{local}/api/library/folders")
        check(
            "新建文件夹并读回",
            status == 200
            and created.get("name") == "论文"
            and any(item["id"] == created.get("id") for item in listing.get("folders") or []),
            f"{status} {created} / {listing.get('folders')}",
        )

        status, payload = post_json(
            f"{local}/api/library/folders", {"name": "论文"}, WRITE_HEADER
        )
        check("重名文件夹回 409", status == 409, f"{status} {payload.get('error', '')}")

        # ---- 记忆：列表、详情、归档、恢复 ----
        status, payload = request(f"{local}/api/memory")
        check(
            "记忆列表：两条、计数与类型分组",
            status == 200
            and len(payload.get("entries") or []) == 2
            and (payload.get("counts") or {}).get("live") == 2
            and (payload.get("counts") or {}).get("byKind", {}).get("fact") == 1,
            f"{status} {payload.get('counts')}",
        )

        status, payload = post_json(
            f"{local}/api/memory/mem_20260101000000_aaaaaa/archive", {}, WRITE_HEADER
        )
        live = json.loads((memory_dir / "entries.json").read_text(encoding="utf-8"))
        archived = json.loads((memory_dir / "archive.json").read_text(encoding="utf-8"))
        check(
            "归档：entries.json 少一条、archive.json 多一条",
            status == 200
            and [item["id"] for item in live["entries"]] == ["mem_20260101000000_bbbbbb"]
            and [item["id"] for item in archived["entries"]] == ["mem_20260101000000_aaaaaa"],
            f"{status} 活 {len(live['entries'])} 归档 {len(archived['entries'])}",
        )

        status, payload = post_json(
            f"{local}/api/memory/mem_20260101000000_aaaaaa/restore", {}, WRITE_HEADER
        )
        live = json.loads((memory_dir / "entries.json").read_text(encoding="utf-8"))
        archived = json.loads((memory_dir / "archive.json").read_text(encoding="utf-8"))
        check(
            "恢复：两份文件回到原状",
            status == 200
            and len(live["entries"]) == 2
            and archived["entries"] == [],
            f"{status} 活 {len(live['entries'])} 归档 {len(archived['entries'])}",
        )

        status, payload = post_json(
            f"{local}/api/memory/mem_20260101000000_aaaaaa/restore", {}, WRITE_HEADER
        )
        check("重复恢复回 409", status == 409, f"{status} {payload.get('error', '')}")

        # ---- 关联：文献 ↔ 记忆 ----
        status, payload = request(f"{local}/api/knowledge/doc/aaaaaaaaaa/relations")
        check(
            "文献 → 记忆：两条引用都查得到",
            status == 200
            and {item["id"] for item in payload.get("memory") or []}
            == {"mem_20260101000000_aaaaaa", "mem_20260101000000_bbbbbb"},
            f"{status} {[item['id'] for item in payload.get('memory') or []]}",
        )

        status, payload = request(
            f"{local}/api/knowledge/memory/mem_20260101000000_bbbbbb/relations"
        )
        check(
            "记忆 → 文献：一条落在语料库、一条标 missing",
            status == 200
            and [item["sourceId"] for item in payload.get("documents") or []]
            == ["doc:aaaaaaaaaa"]
            and [item["state"] for item in payload.get("missing") or []] == ["missing"]
            and not payload.get("external"),
            f"{status} 文献 {payload.get('documents')} 丢失 {payload.get('missing')}",
        )

        status, payload = request(f"{local}/api/knowledge/doc/ffffffffff/relations")
        check("关联查询里不存在的文献回 404", status == 404, f"{status}")

        # ---- 使用记录：追加不改索引、不改状态文件 ----
        state_before = STATE_COPY.read_bytes()
        index_before = (corpus_dir / ".index" / "library.db").stat().st_mtime_ns
        status, payload = post_json(
            f"{local}/api/knowledge/usage",
            {"query": "epidermal", "mode": "lexical", "hits": 3, "ms": 12, "empty": False},
            WRITE_HEADER,
        )
        usage_file = corpus_dir / ".usage" / "search.jsonl"
        rows = [
            json.loads(line)
            for line in usage_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        check(
            "追加使用记录：写进 .usage，索引与状态文件都不动",
            status == 200
            and rows[-1]["query"] == "epidermal"
            and rows[-1]["hits"] == 3
            and STATE_COPY.read_bytes() == state_before
            and (corpus_dir / ".index" / "library.db").stat().st_mtime_ns == index_before,
            f"{status} {rows[-1] if rows else None}",
        )

        status, payload = request(f"{local}/api/knowledge/analytics")
        check(
            "使用记录汇总：次数与 p95",
            status == 200 and payload.get("count", 0) >= 1 and "p95" in payload,
            f"{status} {payload}",
        )

        # ---- 入库任务：四个阶段与台账 ----
        inbox = corpus_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "staged.md").write_text("# 阶段测试\n\nKRAS G12C 共价抑制剂。\n", encoding="utf-8")
        status, payload = post_json(
            f"{local}/api/library/ingest", {"names": ["staged.md"]}, WRITE_HEADER
        )
        job_id = str(payload.get("jobId") or "")
        deadline = time.time() + 60
        job = {}
        while time.time() < deadline:
            _, job = request(f"{local}/api/ingestion/jobs/{job_id}")
            if job.get("status") != "running":
                break
            time.sleep(0.05)
        stages = list(job.get("stages") or [])
        check(
            "入库任务：四个阶段都走过，结束状态是完成",
            status == 202
            and job.get("status") == "done"
            and {"解析", "切块", "写索引", "向量"} <= set(stages),
            f"{status} {stages} / {job.get('status')}",
        )

        status, payload = post_json(
            f"{local}/api/ingestion/jobs/{job_id}/retry", {}, WRITE_HEADER
        )
        check("重试已完成的任务回 409", status == 409, f"{status} {payload.get('error', '')}")

        # 上一次进程留下的 running：重启对账要把它标成中断。判据直接改任务文件的
        # pid，因为「另一个进程」在同一个测试进程里造不出来。
        from vsagent.core.web.jobs import JobStore

        stale = JobStore(corpus_dir).create("ingest", note="遗留", total=1, meta={"names": []})
        stale_path = corpus_dir / ".jobs" / f"{stale['id']}.json"
        raw = json.loads(stale_path.read_text(encoding="utf-8"))
        raw["pid"] = 0
        stale_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        changed = JobStore(corpus_dir).mark_interrupted()
        _, stale_job = request(f"{local}/api/ingestion/jobs/{stale['id']}")
        check(
            "进程留下的 running 在下次启动时标成中断",
            changed >= 1 and stale_job.get("status") == "interrupted",
            f"改了 {changed} 条，状态 {stale_job.get('status')}",
        )

        status, payload = request(f"{local}/api/ingestion/jobs/{job_id}")
        check(
            "入库任务详情：带每个文件的结果",
            status == 200
            and (payload.get("result") or {}).get("results")
            and payload["result"]["results"][0]["status"] == "new",
            f"{status} {(payload.get('result') or {}).get('results')}",
        )

        # ---- 评测：脚本不存在时任务失败，错误带子进程的末行 ----
        status, payload = post_json(
            f"{local}/api/knowledge/evaluation/runs",
            {"questions": str(workspace / "no-such.jsonl")},
            WRITE_HEADER,
        )
        eval_id = str(payload.get("jobId") or "")
        deadline = time.time() + 120
        job = {}
        while time.time() < deadline:
            _, job = request(f"{local}/api/knowledge/evaluation/runs/{eval_id}")
            if job.get("status") != "running":
                break
            time.sleep(0.1)
        check(
            "评测跑不起来时任务状态是 failed，错误带子进程输出",
            status == 202
            and job.get("status") == "failed"
            and "退出码" in str(job.get("error") or ""),
            f"{status} {job.get('status')} {job.get('error', '')[:60]}",
        )

        status, payload = request(f"{local}/api/knowledge/evaluation/runs")
        check(
            "评测记录清单里有这一条",
            status == 200 and any(item["id"] == eval_id for item in payload.get("jobs") or []),
            f"{status} {len(payload.get('jobs') or [])} 条",
        )
    finally:
        server.jobs.stop_own()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def run_standalone_refusal_checks(base):
    """独立服务下，只有应用进程能做的写入口要明确回 503。"""
    status, payload = post_json(
        f"{base}/api/memory/mem_20260101000000_aaaaaa/archive", {}, WRITE_HEADER
    )
    check(
        "独立服务不归档记忆：明确 503",
        status == 503 and "记忆" in str(payload.get("error", "")),
        f"{status} {payload.get('error', '')}",
    )

    status, payload = post_json(
        f"{base}/api/ingestion/jobs/job_20260101000000_aaaaaa/retry", {}, WRITE_HEADER
    )
    check(
        "独立服务不重试入库：明确 503",
        status == 503 and "入库" in str(payload.get("error", "")),
        f"{status} {payload.get('error', '')}",
    )


def run_orphan_checks(corpus_dir):
    """删掉的源在 `reindex` 之后不留孤儿行，回收站里重名不覆盖。"""
    from vsagent.core.web.routes import _trash_target

    index.reindex(corpus_dir)
    conn = index.connect(corpus_dir)
    try:
        chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id = ?", ("web:bbbbbbbbbb",)
        ).fetchone()[0]
        vectors = conn.execute(
            "SELECT COUNT(*) FROM vectors WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        ).fetchone()[0]
    finally:
        conn.close()
    check(
        "删掉的源 reindex 之后块与向量都不留孤儿",
        chunks == 0 and vectors == 0,
        f"{chunks} 块 / {vectors} 条孤儿向量",
    )

    trash = corpus_dir / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    first = _trash_target(trash, "web", "bbbbbbbbbb")
    first.mkdir()
    second = _trash_target(trash, "web", "bbbbbbbbbb")
    check(
        "回收站里重名不覆盖（第二次换一个名字）",
        second != first and not second.exists(),
        f"{first.name} → {second.name}",
    )


def run_concurrency_check(base, state_path, pristine):
    """一边重写状态文件一边请求接口，核对没有一次读到写了一半的内容。

    写的那一侧单起一个进程。真实的场景就是这样：TUI 在写、服务在读，
    两个进程各干各的。放在同一个进程里用线程写会让判据自己拖慢自己。

    这里核对的是每次读到的会话条数，不只看状态码。状态文件被写入时中途会
    缩到零字节再长回来，截在某张表结束时那份内容仍然是合法 TOML，接口会
    拿它返回 200，只是里面的会话少了一截——只看状态码看不出这种情况。
    """
    reference = load_reference(state_path)
    expected = len(reference)
    writer = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--write-loop", str(state_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    failures = []
    total = 60
    try:
        for _ in range(total):
            status, payload = request(f"{base}/api/sessions")
            if status != 200:
                failures.append(payload.get("error", f"HTTP {status}"))
                continue
            got = len(payload["sessions"])
            if got != expected:
                failures.append(f"读到 {got} 条，应有 {expected} 条")
    finally:
        writer.terminate()
        writer.wait(timeout=10)
        # 写方是在半途被杀掉的，副本会停在写了一半的位置。留一份干净的不只是
        # 为了后面的检查，手工打开这个文件看的时候也应当是完整的。
        state_path.write_bytes(pristine)

    check(
        "边写边读没有一次读到写了一半的内容",
        not failures,
        f"写入进程持续重写，请求 {total} 次，失败 {len(failures)} 次"
        + (f"：{failures[0]}" if failures else ""),
    )


def write_loop(state_path: Path) -> None:
    """持续重写状态文件，供并发判据当写方使用。"""
    while True:
        manager = SessionManager(state_path)
        manager.load_from_toml()
        manager.save_to_toml()
        time.sleep(0.02)


def run_broken_state_check(base, state_path, pristine):
    """状态文件停在写入过程中的两个瞬间时，接口报错而不是让服务崩溃。

    两个瞬间都取自真实的写入过程：刚清空还没写内容，与才写了个开头。
    这两种内容解析不了，接口应当报错；恢复之后接口要能重新可用。

    恢复用的那份内容由调用方在并发检查之前留好。放在这里现取会出事：
    并发检查的写方是在半途被杀掉的，那一刻副本可能正好是空的或者半截的，
    拿它当原文恢复回去，后面这条「恢复后重新可用」会假失败。
    """
    for label, content in (("刚清空", b""), ("才写了开头", pristine[:10])):
        state_path.write_bytes(content)
        status, payload = request(f"{base}/api/sessions")
        check(
            f"状态文件{label}时返回错误而不是崩溃",
            status == 500 and "error" in payload,
            f"返回 {status}，内容 {payload.get('error', '')}",
        )

    state_path.write_bytes(pristine)
    status, _ = request(f"{base}/api/sessions")
    check(
        "状态文件恢复后接口重新可用",
        status == 200,
        f"返回 {status}",
    )


def run_summary_field_checks(base, event_session):
    """首页最近任务要的两项：产物数与最近主题。

    登记表由 `prepare_ledgers` 造在一个临时目录里，只有 `event_session` 有一份。
    其余会话读不到登记表时产物数按 0 算，这一条顺带验了那个分支。
    """
    status, listing = request(f"{base}/api/sessions")
    sessions = listing.get("sessions") or []
    check(
        "列表每项都有产物数与最近主题",
        status == 200
        and all(isinstance(item.get("artifactCount"), int) for item in sessions)
        and all(
            item.get("lastEvent") is None
            or set(item["lastEvent"]) >= {"id", "title", "status", "topicIndex"}
            for item in sessions
        ),
        f"{len(sessions)} 条",
    )

    target = next((item for item in sessions if item["id"] == event_session), None)
    _, shelf = request(f"{base}/api/sessions/{event_session}/artifacts")
    items = shelf.get("items") or []
    check(
        "有登记表的会话：产物数与产物接口一致",
        target is not None and target["artifactCount"] == len(items) and len(items) > 0,
        f"列表 {target['artifactCount'] if target else '缺行'}，产物接口 {len(items)}",
    )

    others = [item for item in sessions if item["id"] != event_session]
    check(
        "没有登记表的会话：产物数是 0",
        all(item["artifactCount"] == 0 for item in others),
        f"其余 {len(others)} 条",
    )

    _, payload = request(f"{base}/api/sessions/{event_session}/events")
    ids = [event["id"] for event in payload.get("events") or []]
    last = target["lastEvent"] if target else None
    check(
        "最近主题指向事件清单的最后一条",
        last is not None and bool(ids) and last["id"] == ids[-1],
        f"列表 {last['id'] if last else '空'}，事件清单末条 {ids[-1] if ids else '无'}",
    )


def run_task_checks(base, pristine):
    """提交任务：独立进程拒绝；带回调时建会话、写进状态文件、重复提交回 409。

    `base` 是独立进程起的服务，没有 `submit`，只能回 503。真正那条路在这里
    另起一个同进程的 `WebServer`，回调**真的**往一份副本里写一个新会话——
    判据要断言「返回的编号能在 `/api/sessions` 里查到」，假返回一个不存在的
    编号就验不到那一步。
    """
    import threading

    from vsagent.core.web.routes import WebServer, load_manager

    status, payload = post_json(f"{base}/api/tasks", {"text": "列出 P00533 的记录"})
    check(
        "提交任务缺自定义头：403",
        status == 403 and "X-VSA-Request" in str(payload.get("error", "")),
        f"{status} {payload.get('error', '')}",
    )

    status, payload = post_json(
        f"{base}/api/tasks", {"text": "列出 P00533 的记录"}, WRITE_HEADER
    )
    check(
        "独立进程不接受提交：明确 503 而不是自己跑",
        status == 503 and "任务" in str(payload.get("error", "")),
        f"{status} {payload.get('error', '')}",
    )

    # 单独一份状态文件：这一组会真的往里加会话，不能弄脏主判据那份。
    task_state = STATE_COPY.parent / "tasks-state.toml"
    task_state.write_bytes(pristine)

    def fake_submit(text, session_id=""):
        if "这一轮还没结束" in text:
            return {"error": "这一轮还没结束", "status": 409}
        manager = load_manager(task_state)
        if session_id:
            if manager.get_session(session_id) is None:
                return {"error": f"没有这个会话：{session_id}", "status": 404}
            return {"sessionId": session_id, "created": False}
        session = manager.create_session(text[:40])
        manager.save_to_toml()
        return {"sessionId": session.id, "created": True}

    port = free_port()
    server = WebServer(
        ("127.0.0.1", port),
        task_state,
        FIXTURE_DIR,
        PROJECT_ROOT / "web",
        submit=fake_submit,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = f"http://127.0.0.1:{port}"
    try:
        status, payload = post_json(
            f"{local}/api/tasks", {"text": "列出 P00533 的 UniProt 记录"}, WRITE_HEADER
        )
        new_id = str(payload.get("sessionId") or "")
        check(
            "提交任务：新建会话并返回编号",
            status == 200 and payload.get("created") is True and new_id.startswith("ses_"),
            f"{status} {payload}",
        )
        _, listing = request(f"{local}/api/sessions")
        ids = [item["id"] for item in listing.get("sessions") or []]
        check(
            "新会话能在会话列表里查到",
            bool(new_id) and new_id in ids,
            f"提交得到 {new_id}，列表 {len(ids)} 条",
        )

        status, payload = post_json(
            f"{local}/api/tasks", {"text": "接着做", "sessionId": new_id}, WRITE_HEADER
        )
        check(
            "提交到已有会话：created 为 false",
            status == 200 and payload.get("created") is False and payload.get("sessionId") == new_id,
            f"{status} {payload}",
        )

        status, payload = post_json(
            f"{local}/api/tasks", {"text": "这一轮还没结束"}, WRITE_HEADER
        )
        check(
            "会话正在跑时回 409",
            status == 409 and "还没结束" in str(payload.get("error", "")),
            f"{status} {payload.get('error', '')}",
        )

        status, payload = post_json(f"{local}/api/tasks", {"text": "  "}, WRITE_HEADER)
        check(
            "空任务回 400",
            status == 400,
            f"{status} {payload.get('error', '')}",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def main():
    parser = argparse.ArgumentParser(description="网页端接口判据")
    parser.add_argument("--keep", action="store_true", help="跑完不关服务，便于手工查看")
    parser.add_argument("--write-loop", type=Path, help="内部使用：持续重写给定的状态文件")
    args = parser.parse_args()

    if args.write_loop:
        write_loop(args.write_loop)
        return 0

    state_path = prepare_state_copy()
    runs_dir = prepare_runs()
    prepare_corpus()
    prepare_surveys()
    # 用**副本里真实存在**的某个会话当关联目标，而不是取一个固定编号。
    # 状态文件是活的：把编号取成固定值，一旦那个会话不在里面，
    # 「只有对应会话拿到了运行」就不会报出关联断掉——它只查「恰好有一个
    # 带运行的会话」，一个都没有时前半段不成立、后半段的比对也失去意义。
    link_session = pick_link_session(state_path)
    if link_session is None:
        print("状态文件里一个会话都没有，无法核对会话与运行的关联")
        return 1
    if link_session != LINK_SESSION:
        print(f"状态文件里没有 {LINK_SESSION}，本次改用 {link_session} 核对关联")
    other_session = ensure_other_session(state_path, link_session)
    make_link_run(runs_dir, link_session)
    # 产物判据要在临时登记表上跑：真实那几份产物属于用户，判据不该读它们，
    # 更不该断言它们的字节数。
    event_session = pick_event_session(state_path)
    prepare_ledgers(event_session)

    # 干净的一份留在这里。并发检查会把副本写到半途，之后要拿这份干净的恢复。
    pristine = state_path.read_bytes()
    port = free_port()
    process, base = start_service(state_path, runs_dir, port)
    print(f"服务 {base}，状态文件副本 {state_path}")
    print("")

    try:
        run_checks(base, state_path, runs_dir, link_session, other_session)
        run_summary_field_checks(base, event_session)
        run_event_checks(base, state_path, event_session)
        run_analysis_checks(base, state_path, event_session)
        run_live_checks(base, state_path, event_session)
        run_survey_checks(base)
        run_library_checks(base)
        run_chunk_checks(base)
        run_orphan_checks(CORPUS_DIR)
        run_ingest_checks(base, CORPUS_DIR)
        run_write_guard_checks(base)
        run_knowledge_read_checks(base, CORPUS_DIR)
        run_knowledge_write_checks(CORPUS_DIR)
        run_standalone_refusal_checks(base)
        run_concurrency_check(base, state_path, pristine)
        run_broken_state_check(base, state_path, pristine)
        run_task_checks(base, pristine)
    finally:
        if not args.keep:
            process.terminate()
            process.wait(timeout=10)

    failed = [item for item in results if not item[1]]
    print("")
    print(f"共 {len(results)} 条，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败清单：")
        for name, _, detail in failed:
            print(f"  - {name}（{detail}）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
