"""接口判据：核对服务返回的 JSON 与状态文件里的内容是否一致。

判据挑的是那些「接口通了、字段也有，但内容是错的」的地方：
  - 会话条数对了，但消息数或内容单元数对不上
  - 工具单元在，但输入与输出丢了
  - 补丁单元的增删行数是零
  - 运行标识取了 meta.json 里的 run_id，而那个值可能与目录名不一致
  - 状态文件正在被写入时读到写了一半的内容，接口报错

S2 起还有一组语料库判据：上传 / 改名 / 删除，以及三类「本地无鉴权」必须挡住的东西
——跨站 POST、路径穿越、超限。语料根指到 `.probe/web-corpus/`，不碰真实语料。

**服务跑在包内**（`opencode_tui/core/web/`）。判据原先起的是 `tools/serve_web.py`
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
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opencode_tui.core import index
from opencode_tui.core.models import PartType
from opencode_tui.core.session_manager import SessionManager, default_state_path

FIXTURE_DIR = PROJECT_ROOT / ".probe" / "web-runs"
STATE_COPY = PROJECT_ROOT / ".probe" / "web-state" / "state.toml"

CORPUS_DIR = PROJECT_ROOT / ".probe" / "web-corpus"
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

    用 `-m opencode_tui.core.web` 起，走的是应用里同一个 `WebService`——
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
                "-m", "opencode_tui.core.web",
                "--state", str(state_path),
                "--runs", str(runs_dir),
                "--corpus", str(CORPUS_DIR),
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


def run_checks(base, state_path, runs_dir, link_session):
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

    status, other = request(f"{base}/api/sessions/ses_0001")
    check(
        "没有对应运行的会话 runId 为空",
        status == 200 and other["runId"] is None,
        f"ses_0001 runId={other['runId']}",
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
        status == 200 and {item["id"] for item in items} == {"doc:aaaaaaaaaa", "web:bbbbbbbbbb"},
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


def run_ingest_checks(base, corpus_dir):
    """入库：网页只转交，不自己写索引；独立服务拒绝；重复投递幂等。

    `base` 是**独立进程**起的服务（`-m opencode_tui.core.web`）——它没有入库
    回调，必须回 503，而不是退化成第二个写者。真正入库那一组在这里另起一个
    带 `ingest` 的 `WebServer`（同进程、同线程模型，与应用里一样）。
    """
    import threading

    from opencode_tui.core.agent.domain.corpus import ingest_files
    from opencode_tui.core.web.routes import WebServer

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
    make_link_run(runs_dir, link_session)

    # 干净的一份留在这里。并发检查会把副本写到半途，之后要拿这份干净的恢复。
    pristine = state_path.read_bytes()
    port = free_port()
    process, base = start_service(state_path, runs_dir, port)
    print(f"服务 {base}，状态文件副本 {state_path}")
    print("")

    try:
        run_checks(base, state_path, runs_dir, link_session)
        run_library_checks(base)
        run_ingest_checks(base, CORPUS_DIR)
        run_concurrency_check(base, state_path, pristine)
        run_broken_state_check(base, state_path, pristine)
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
