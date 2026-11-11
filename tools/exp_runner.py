from __future__ import annotations

# 论文实验的驱动脚本：在伪终端里跑 dist/VSAgent，把一次交办连同它的全部痕迹
# 收进 result/<exp>/。界面是唯一的入口，所以这里不做任何绕开界面的调用。
#
# 每次实验一个独立目录，HOME 指到目录内的 home/，会话、登记表、trace 互不串。
# 模型缓存与 AD4 图缓存按内容定址，用软链接共用。

import argparse
import fcntl
import hashlib
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tomllib
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "dist" / "VSAgent"
RESULT = REPO / "result"

# 权限询问由驱动按「允许一次」答复（y），次数记进 meta.json。
# 配置文件里的 allow 规则在 check() 里没有被匹配的那一步，写进去不起作用。

# 按内容定址的缓存，跨实验共用不改变结果。
SHARED_CACHES = ("models", "grids")

ROWS, COLS = 44, 140

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# 外部引擎在实验的 HOME 下要真的能启动：PATH 里那份 vina 是包装脚本，
# 靠 $HOME/.local/vina-1.2.5 找 Boost 运行库，HOME 一换就找不到。所以先按
# 「在实验环境里跑一次 --version 且退出码为 0」筛，再把它写成绝对路径。
ENGINE_ENV = {"vina": "VSA_VINA_BIN", "autogrid4": "VSA_AUTOGRID_BIN"}
ENGINE_PREFERRED = {
    "vina": ["/data/work/cd/tools/vina/vina"],
    "autogrid4": [],
}


def resolve_engines(env: dict[str, str]) -> dict[str, dict[str, str]]:
    """找出在实验环境下真能跑的外部引擎，并把绝对路径写进 env。"""
    found: dict[str, dict[str, str]] = {}
    for name, key in ENGINE_ENV.items():
        candidates: list[str] = []
        if env.get(key):
            candidates.append(env[key])
        candidates.extend(ENGINE_PREFERRED[name])
        located = shutil.which(name, path=env.get("PATH"))
        if located:
            candidates.append(located)
        for candidate in candidates:
            try:
                probe = subprocess.run(
                    [candidate, "--version"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode != 0:
                continue
            lines = (probe.stdout or probe.stderr).strip().splitlines()
            found[name] = {
                "path": str(Path(candidate).resolve()),
                "version": lines[0].strip() if lines else "",
                "sha256": sha256_of(Path(candidate).resolve()),
            }
            env[key] = candidate
            break
    return found


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_state() -> dict[str, object]:
    """提交号加工作区状态。

    只记提交号会把「从脏工作区打出来的包」说成那个提交的产物：本仓有一批未提交的
    改动，打包用的正是工作区里的文件。所以把 `git diff HEAD` 的摘要一起记下来，
    同一份源码配同一份差异才对应同一个包。
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "HEAD"], cwd=REPO, capture_output=True, check=True
    ).stdout
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    untracked = [line for line in status.splitlines() if line.startswith("??")]
    return {
        "head": head,
        "dirty": bool(status.strip()),
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked": len(untracked),
    }


def prepare(exp: Path, turns: list[dict], share: bool) -> None:
    """建目录、放连接配置与这一轮的脚本。已存在的实验目录一律拒绝覆盖。"""
    if exp.exists():
        raise SystemExit(f"{exp} 已存在，换一个名字或先移走它")
    home = exp / "home"
    config = home / ".vsagent"
    config.mkdir(parents=True)

    source = Path.home() / ".vsagent" / "connection.toml"
    if not source.is_file():
        raise SystemExit(f"{source} 不存在，先在 TUI 里用 /model 配好模型接口")
    target = config / "connection.toml"
    shutil.copy2(source, target)
    target.chmod(0o600)

    if share:
        for name in SHARED_CACHES:
            shared = Path.home() / ".vsagent" / name
            if shared.is_dir():
                (config / name).symlink_to(shared)

    (exp / "script.json").write_text(
        json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = []
    for position, turn in enumerate(turns, 1):
        lines.append(f"[{turn.get('id', position)}] {turn['send']}")
    (exp / "prompt.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def spawn(
    exp: Path,
) -> tuple[subprocess.Popen[bytes], int, dict[str, dict[str, str]], dict[str, object]]:
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(exp / "home"),
            "TERM": "xterm-256color",
            "COLUMNS": str(COLS),
            "LINES": str(ROWS),
        }
    )
    # 冻结之后 VSA_DTA_ROOT 的默认值指向可执行文件旁边，dist/ 下没有 models/DTA，
    # 不显式给出来，score_affinity 会直接返回安装指引。
    env.setdefault("VSA_DTA_ROOT", str(REPO / "models" / "DTA"))
    engines = resolve_engines(env)
    # 摘要必须在**起进程之前**取。打包一次要两分钟，实验一跑十几分钟，中间重打包
    # 的话文件会被替换，而正在跑的进程用的还是旧的那个 inode——收尾时再算一次
    # 得到的是新文件，记下来的指纹与真正跑过的东西对不上。
    stat = BINARY.stat()
    binary = {
        "path": str(BINARY),
        "sha256": sha256_of(BINARY),
        "bytes": stat.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
    }
    process = subprocess.Popen(
        [str(BINARY)],
        cwd=str(exp),
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    return process, master, engines, binary


class Screen:
    """把伪终端的输出落盘，同时记下最后一次收到字节的时刻。

    单文件打包的程序启动时要先把整包解到临时目录，那几秒里它还没开始读键盘，
    早发的输入会被丢掉。所以判「起好了」看的是画面上出现了输入框的占位文字。
    """

    READY_MARK = "说一件事"

    def __init__(self, master: int, path: Path) -> None:
        self.master = master
        self.path = path
        self.last_output = time.monotonic()
        self.text = ""
        self._handle = path.open("wb")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def ready(self) -> bool:
        return self.READY_MARK in self.text

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self._handle.write(chunk)
            self._handle.flush()
            self.last_output = time.monotonic()
            plain = _ANSI.sub("", chunk.decode("utf-8", "replace"))
            self.text = (self.text + plain)[-40000:]

    def send(self, text: str) -> None:
        os.write(self.master, text.encode())

    def deliver(self, text: str, accepted: "Callable[[], bool]", attempts: int = 4) -> bool:
        """把一句话送进输入框并回车，直到 trace 里真的多出事件。

        正文与回车必须分两次写：同一次写进去，界面按粘贴处理，回车成了正文里的
        换行，那一轮不会提交。是否送达看 trace 有没有变大，不看屏幕（界面只重画
        变化的格子，抓下来的文字里夹着定位序列），也不看 state.toml（要等一轮结束才写）。
        """
        for _ in range(attempts):
            self.send(text)
            time.sleep(0.4)
            self.send("\r")
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if accepted():
                    return True
                time.sleep(0.3)
            print("会话里没多出用户消息，重发")
        return False

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._handle.close()


def read_new_events(path: Path, offset: int) -> tuple[list[dict], int]:
    """按偏移读 trace 的新增行。

    trace 每行是 `{"at": 时间, "event": 事件名, "payload": {...}}`。
    最后一行可能是写了一半的，解析失败就跳过。
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read()
    events: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, offset + len(raw)


def newest_trace(home: Path) -> Path | None:
    logs = home / ".vsagent" / "logs"
    if not logs.is_dir():
        return None
    files = sorted(logs.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
    return files[-1] if files else None


def collect_usage(home: Path) -> dict[str, object]:
    state = home / ".vsagent" / "state.toml"
    if not state.is_file():
        return {}
    data = tomllib.loads(state.read_text(encoding="utf-8"))
    sessions = data.get("sessions") or []
    if not sessions:
        return {}
    session = max(sessions, key=lambda item: item.get("updated_at", 0))
    total = {"input": 0, "output": 0, "cached": 0, "uncached": 0}
    cost = 0.0
    for message in session.get("messages", []):
        for key, value in (message.get("tokens") or {}).items():
            if key in total:
                total[key] += int(value)
        cost += float(message.get("cost") or 0.0)
    return {
        "session_id": session.get("id"),
        "status": session.get("status"),
        "status_note": session.get("status_note"),
        "tool_families": session.get("tool_families"),
        "tokens": total,
        "cost": round(cost, 6),
        "messages": len(session.get("messages", [])),
    }


def trace_state(home: Path) -> tuple[str, int]:
    """当前会话的 trace 是哪个文件、多大。

    一句话提交上去，内核会立刻写事件。既要看大小，也要看文件名：新会话会另起
    一个文件，只比大小会把「新文件 2KB」判成「没长」。
    """
    path = newest_trace(home)
    if path is None:
        return ("", 0)
    return (path.name, path.stat().st_size)


def wait_reply(exp: Path, turn: dict, why: str, text: str, limit: float) -> str | None:
    """停下来等人工答复：写 halt.json，等 reply.txt 出现。

    返回 None 表示等到上限也没有答复，这一场就此结束。
    """
    halt = exp / "halt.json"
    halt.write_text(
        json.dumps(
            {
                "round": turn.get("id"),
                "reason": why,
                "asked": turn["send"],
                "reply_tail": text,
                "waited_s": limit,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"停在第 {turn.get('id')} 轮：{why}；等 {limit:.0f} 秒内的 reply.txt")
    reply = exp / "reply.txt"
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if reply.is_file() and reply.stat().st_mtime >= halt.stat().st_mtime:
            answer = reply.read_text(encoding="utf-8").strip()
            if answer:
                return answer
        time.sleep(1.0)
    return None


def last_assistant_text(home: Path) -> str:
    """会话里最后一条 assistant 消息的全部文字。"""
    state = home / ".vsagent" / "state.toml"
    if not state.is_file():
        return ""
    data = tomllib.loads(state.read_text(encoding="utf-8"))
    sessions = data.get("sessions") or []
    if not sessions:
        return ""
    session = max(sessions, key=lambda item: item.get("updated_at", 0))
    for message in reversed(session.get("messages", [])):
        if message.get("role") != "assistant":
            continue
        return "\n".join(
            part.get("text", "")
            for part in message.get("parts", [])
            if part.get("type") == "text"
        ).strip()
    return ""


# 中文里把选择权交回用户，常常不写问号：「回一句 1 或 2 即可」「配体集请你定」
# 「你把来源说一句就行」。只看问号会把这类真提问判成自己结案，整场就此停下。
# 这一段只在脚本显式写了 `require_ask` 时才用：预检关心的是工具跑没跑通，
# 正式实验的确认点才需要「它确实停下来问了人」。
ASK_PATTERN = re.compile(
    r"[？?]|请回|回一句|回一个|回「|回 [A-Za-z0-9]|告诉我|请给|请你|要不要|选哪|哪一个|回复|确认"
    r"|给我|需要你|就行|即可|说一句"
)


def check_turn(turn: dict, text: str) -> tuple[bool, str]:
    """这一轮结束之后，能不能发下一句。

    一个条件：最后一段要提到这一轮该出现的东西（`expect`）。写了 `require_ask` 的
    轮次再看一条——它是不是真的在向用户提问。没提问说明它自己结案了，这时按脚本
    往下灌会让「它自己决定的」与「用户要求的」混在一起，所以停下来记录现场。
    """
    if not text:
        return False, "这一轮没有文字输出"
    expect = str(turn.get("expect") or "")
    if expect and expect not in text:
        return False, f"最后一段没有提到「{expect}」"
    if turn.get("final"):
        return True, ""
    if turn.get("require_ask") and not ASK_PATTERN.search(text[-600:]):
        return False, "最后一段没有向用户提问，可能已经自己结案"
    return True, ""


def run(
    exp: Path,
    turns: list[dict],
    timeout: float,
    idle: float,
    stage: str = "",
    on_halt: float = 0.0,
) -> int:
    process, master, engines, binary = spawn(exp)
    screen = Screen(master, exp / "screen.log")
    screen.start()

    started = time.time()
    # 墙钟用来写 meta，计时一律用单调时钟：两个时钟混着减，得到的差值没有意义。
    started_mono = time.monotonic()
    trace_path: Path | None = None
    offset = 0
    turn_events = 0
    asks = 0
    last_event = time.monotonic()
    index = 0
    sent = False
    sent_at = 0.0
    retried = False
    accepted = False
    log: list[dict] = []
    status = "timeout"

    try:
        while True:
            if process.poll() is not None:
                print(f"程序自己退出了，退出码 {process.returncode}")
                status = "exited"
                break
            if time.time() - started > timeout:
                print(f"超过 {timeout:.0f} 秒仍未结束，中断")
                status = "timeout"
                break

            if trace_path is None:
                trace_path = newest_trace(exp / "home")
            if trace_path is not None:
                events, offset = read_new_events(trace_path, offset)
                for event in events:
                    last_event = time.monotonic()
                    kind = event.get("event")
                    if kind == "message.add":
                        # 用户消息落进会话就是「提交成功」的信号。此后这一轮跑多久
                        # 都不该再判成没提交——对接一次两分钟、训练一次十分钟，
                        # 期间可能一个事件都没有。
                        accepted = True
                    if kind == "turn.done":
                        turn_events += 1
                    elif kind == "permission.ask":
                        asks += 1
                        print(f"出现权限询问（第 {asks} 次），按允许一次放行")
                        time.sleep(0.5)
                        screen.send("y")

            # 等输入框真的画出来再发；单文件包启动时要解压，那几秒里发出去会被丢掉。
            # 只有第一句要等：之后界面已经在会话里，占位文字早就滚出缓冲了。
            if not sent and index < len(turns):
                if index == 0 and not screen.ready and time.monotonic() - started_mono < 30.0:
                    time.sleep(0.2)
                    continue
                if index == 0 and not screen.ready:
                    print("30 秒没等到输入框，照发一次")
                before = trace_state(exp / "home")
                if not screen.deliver(
                    str(turns[index]["send"]),
                    lambda: trace_state(exp / "home") != before,
                ):
                    print("连续四次都没送进会话，停下")
                    status = "not_delivered"
                    break
                sent = True
                sent_at = time.monotonic()
                last_event = time.monotonic()
                retried = False
                accepted = False
                print(f"[{turns[index].get('id', index + 1)}] 已交办：{turns[index]['send']}")

            # 交办之后 20 秒还没进会话，补一个回车再试一次。
            if sent and not retried and not accepted and time.monotonic() - sent_at > 20.0:
                print("20 秒没有动静，补一个回车")
                screen.send("\r")
                retried = True
                sent_at = time.monotonic()

            if sent and not accepted and time.monotonic() - sent_at > 120.0:
                print("交办之后两分钟都没进会话，判定没有提交上去")
                status = "not_submitted"
                break

            if sent and turn_events > 0 and time.monotonic() - last_event > float(
                turns[index].get("idle") or idle
            ):
                text = last_assistant_text(exp / "home")
                ok, why = check_turn(turns[index], text)
                log.append(
                    {
                        "id": turns[index].get("id", index + 1),
                        "send": turns[index]["send"],
                        "seconds": round(time.monotonic() - sent_at, 1),
                        "reply_tail": text[-300:],
                        "proceed": ok,
                        "reason": why,
                    }
                )
                if not ok:
                    answer = None
                    if on_halt > 0:
                        answer = wait_reply(exp, turns[index], why, text, on_halt)
                    if answer is None:
                        status = "needs_review"
                        break
                    before = trace_state(exp / "home")
                    if not screen.deliver(
                        answer, lambda: trace_state(exp / "home") != before
                    ):
                        status = "not_delivered"
                        break
                    log.append(
                        {
                            "id": f"{turns[index].get('id')}-人工",
                            "send": answer,
                            "seconds": 0.0,
                            "reply_tail": "",
                            "proceed": True,
                            "reason": "人工答复",
                        }
                    )
                    sent = True
                    sent_at = time.monotonic()
                    turn_events = 0
                    last_event = time.monotonic()
                    retried = False
                    accepted = False
                    continue
                index += 1
                sent = False
                turn_events = 0
                if index >= len(turns):
                    status = "finished"
                    break
                print(f"连续 {idle:.0f} 秒没有新事件，发下一轮")
                # 一轮收尾时界面还在写状态、封存阶段，这几秒里发的字会被丢掉。
                time.sleep(6.0)
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            screen.send("\x03")
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait()
        screen.close()
        os.close(master)

    if trace_path is not None and trace_path.is_file():
        shutil.copy2(trace_path, exp / f"session{stage}.jsonl")

    # 收尾再算一次：与起进程时不同就说明中途重打包过，这一场的指纹按起进程那份算。
    now_sha = sha256_of(BINARY) if BINARY.is_file() else ""
    meta = {
        "experiment": exp.name,
        "stage": stage,
        "binary": str(BINARY.relative_to(REPO)),
        "binary_sha256": binary["sha256"],
        "binary_bytes": binary["bytes"],
        "binary_mtime": binary["mtime"],
        "binary_sha256_at_end": now_sha,
        "binary_replaced": bool(now_sha and now_sha != binary["sha256"]),
        "git": git_state(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_s": round(time.time() - started, 1),
        "status": status,
        "timeout_s": timeout,
        "idle_s": idle,
        "rounds_planned": len(turns),
        "rounds_done": len([item for item in log if item["proceed"]]),
        "permission_asks": asks,
        "engines": engines,
        "dta_python": str(REPO / "models" / "DTA" / ".venv" / "bin" / "python"),
        "exit_code": process.returncode,
        "turns": log,
        "usage": collect_usage(exp / "home"),
    }
    (exp / f"meta{stage}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if status == "finished" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="跑一次论文实验，产物写进 result/<name>/")
    parser.add_argument("name", help="实验目录名，落在 result/ 下面")
    parser.add_argument("--prompt", help="单轮交办原文")
    parser.add_argument("--prompt-file", help="单轮交办原文所在的文件")
    parser.add_argument(
        "--script",
        help=(
            "多轮脚本（JSON 数组，每项含 send / expect / final；可选 require_ask 要求"
            "这一轮结尾确实向用户提问，idle 单独设这一轮的静默判定）"
        ),
    )
    parser.add_argument("--timeout", type=float, default=1800.0, help="整场墙钟上限，秒")
    parser.add_argument("--idle", type=float, default=8.0, help="无新事件多久算一轮结束，秒")
    parser.add_argument("--no-share", action="store_true", help="不共用模型与图缓存")
    parser.add_argument(
        "--on-halt",
        type=float,
        default=0.0,
        metavar="秒",
        help="停下时等人工答复的秒数；写成 reply.txt 就接着发，不写就按未审查结束",
    )
    args = parser.parse_args()

    if args.script:
        turns = json.loads(Path(args.script).read_text(encoding="utf-8"))
        if not isinstance(turns, list) or not turns:
            parser.error("--script 应当是一个非空的 JSON 数组")
    else:
        text = args.prompt
        if text is None and args.prompt_file:
            text = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        if not text:
            parser.error("--prompt / --prompt-file / --script 至少要给一个")
        # 单轮交办当作最后一轮：不要求它结尾提问。
        turns = [{"id": "T1", "send": text, "final": True}]

    if not BINARY.is_file():
        raise SystemExit(f"{BINARY} 不存在，先打包")

    exp = RESULT / args.name
    prepare(exp, turns, not args.no_share)
    return run(exp, turns, args.timeout, args.idle, "", args.on_halt)


if __name__ == "__main__":
    sys.exit(main())
