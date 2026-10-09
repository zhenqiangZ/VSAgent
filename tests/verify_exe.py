"""在真实伪终端里运行应用，把屏幕内容抓下来验证。

用 pywinpty 起一个 ConPTY，用 pyte 当终端模拟器。这样能在没有真实终端的环境里
确认打包产物的界面与交互仍然正常（样式文件、懒加载的 widget 是否进包）。

    python tests/verify_exe.py              验证 dist/opencode-tui.exe
    python tests/verify_exe.py <exe 路径>   验证指定的可执行文件
    python tests/verify_exe.py --source     验证源码直接运行（用于与打包产物对照）

最后一步会打 /editor 打开外部编辑器，子进程的 EDITOR 指向
tests/fixtures/marker_editor.cmd，用它判断挂起、外部进程、恢复这条链路是否走通。

中间那几步（/sidebar、/help）排在外部编辑器之前：/editor 回来之后输入框里是编辑器
写进去的那行内容，再往下打命令会接在它后面，就不是一条命令了。

子进程的 HOME 与 USERPROFILE 指到 .probe/verify-home，每轮都从空状态启动，
这样两轮抓到的画面才是同一个会话长度。

子进程会用真实接口那条链路去要回复，远端模型由本脚本起在回环地址上的假服务替换，
地址通过 `VSA_LLM_*` 交给子进程，回复内容固定，画面才复现得了。

跑一次约 25 秒，画面写入 tests/output/。

pyte 与 pywinpty 装在打包环境里（requirements-pack.txt），.venv 里没有，所以要用
`C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/opencode_tui_pack/Scripts/python.exe`
来跑这个脚本。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pyte
from winpty import PtyProcess

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model_server import (  # noqa: E402
    FakeModelServer,
    Reply,
    delta_chunk,
    reasoning_chunks,
    text_chunks,
    usage_chunk,
)

DEFAULT_EXE = PROJECT_ROOT / "dist" / "opencode-tui.exe"
OUTPUT_DIR = PROJECT_ROOT / "tests" / "output"
EDITOR_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "marker_editor.cmd"
# 应用把会话持久化到 ~/.opencode_tui/state.toml。抓帧要复现同一个画面，就得每次都从空状态
# 启动：不隔离的话历史会话越攒越长，源码那一轮与冻结产物那一轮看到的滚动窗口差几行。
SCRATCH_HOME = PROJECT_ROOT / ".probe" / "verify-home"
STATE_FILE = SCRATCH_HOME / ".opencode_tui" / "state.toml"

ROWS, COLS = 34, 100
LOGO_MARK = "(_)_ __"
REPLY_MARK = "抓帧用的回复"
EDITOR_MARK = "[marker-editor]"
SIDEBAR_MARK = "引用文件"
HELP_MARK = "/connect"
BAD_MARKS = ("Traceback", "ModuleNotFoundError", "FileNotFoundError", "NoMatches", "ImportError")

# 抓帧要复现固定画面，回复内容得是固定的一段。子进程走真实接口那条链路，
# 远端模型由回环地址上的假服务替换：脚本先把这个服务起在某个端口上，
# 再把它的地址通过 `VSA_LLM_*` 交给子进程。每次提交一句话之前排好应答。
REPLY_TEXT = "抓帧用的回复，内容固定，好让源码运行与冻结产物得到同一屏。"

# 每一步：写什么按键、至少等多久、抓出的画面叫什么、画面里必须出现的标记。
# 按键可以写成 (间隔秒数, 字符串) 的列表，把输入与提交分开送，更接近真实操作节奏。
# 提交一句话的那两步要先排好应答，第四项为 True 表示这一步会用到模型。
STEPS = [
    (None, 5.0, "home", LOGO_MARK, False),
    ([(0.0, "从启动界面进入"), (0.6, "\r")], 8.0, "chat", REPLY_MARK, True),
    ([(0.0, "解释一下这个仓库"), (0.6, "\r")], 8.0, "reply", REPLY_MARK, True),
    ([(0.0, "/sidebar"), (0.6, "\r")], 6.0, "sidebar", SIDEBAR_MARK, False),
    # 帮助界面按注册表渲染全部命令。拿它当 connect 登记的凭证，因为 toast 三秒就消失，
    # 等画面静止的抓帧逻辑等不到它。
    ([(0.0, "/help"), (0.6, "\r")], 6.0, "help", HELP_MARK, False),
    # 外部编辑器放在最后：这一步回来之后输入框里是编辑器写进去的那行内容。
    ([(0.0, "\x1b"), (0.6, "/editor"), (1.2, "\r")], 6.0, "editor", EDITOR_MARK, False),
]

# 冻结产物与源码运行的启动速度不同，固定等待会假失败；标记出现后最多再等这么久
MAX_EXTRA_WAIT = 12.0
# 标记出现后还要求画面连续这么久没有变化，避免抓到滚动中途的帧
STABLE_SECONDS = 1.0


class Recorder:
    """把 pty 输出喂进 pyte，并保留完整字节用于排查。"""

    def __init__(self) -> None:
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.raw = bytearray()

    def feed(self, data: bytes | str) -> None:
        chunk = data.encode("utf-8", "replace") if isinstance(data, str) else data
        self.raw.extend(chunk)
        self.stream.feed(chunk)

    def snapshot(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)


def wait_for(recorder: Recorder, mark: str, minimum: float, timeout: float) -> None:
    """等到画面里出现标记、至少等够 minimum 秒，并且画面连续 STABLE_SECONDS 秒不变。

    流式渲染刚结束时消息区还在自动滚动，只按时间等会抓到滚动中途的画面，
    源码运行与冻结产物的启动速度不同就稳定差一行，所以这里等画面静止。
    """
    started = time.monotonic()
    previous: str | None = None
    stable_since: float | None = None
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            return
        snapshot = recorder.snapshot()
        if elapsed >= minimum and mark in snapshot and snapshot == previous:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STABLE_SECONDS:
                return
        else:
            stable_since = None
        previous = snapshot
        time.sleep(0.25)


def script_reply(server: FakeModelServer) -> None:
    """给服务的下一次请求排好一段固定正文的应答。

    抓帧不关心工具调用，一次请求就把整段正文返回，收尾补上用量。
    """
    server.script(
        Reply(
            chunks=[
                *reasoning_chunks("先看一眼。", size=4),
                *text_chunks(REPLY_TEXT, size=4),
                delta_chunk(finish_reason="stop"),
                usage_chunk(12, 34),
            ]
        )
    )


def parse_args() -> tuple[list[str], str]:
    """返回要执行的命令与服务前缀。"""
    if len(sys.argv) > 1 and sys.argv[1] == "--source":
        return [sys.executable, "-m", "opencode_tui"], "source"
    exe = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_EXE
    return [str(exe)], "frozen"


def main() -> int:
    command, prefix = parse_args()
    if command[0] != sys.executable and not Path(command[0]).exists():
        print(f"找不到 {command[0]}，请先执行 pyinstaller --clean opencode_tui.spec")
        return 1

    child_env = dict(os.environ)
    child_env["EDITOR"] = str(EDITOR_FIXTURE)
    STATE_FILE.unlink(missing_ok=True)
    child_env["USERPROFILE"] = str(SCRATCH_HOME)
    child_env["HOME"] = str(SCRATCH_HOME)

    server = FakeModelServer()
    server.start()
    child_env["VSA_LLM_BASE_URL"] = server.base_url
    child_env["VSA_LLM_API_KEY"] = "verify-key"
    child_env["VSA_LLM_MODEL"] = "verify-model"
    child_env["VSA_LLM_CONTEXT_WINDOW"] = "1000000"
    child_env["VSA_LLM_PRICE_INPUT"] = "0.5"
    child_env["VSA_LLM_PRICE_OUTPUT"] = "1.5"

    process = PtyProcess.spawn(
        command,
        cwd=str(PROJECT_ROOT),
        env=child_env,
        dimensions=(ROWS, COLS),
    )
    recorder = Recorder()
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            try:
                data = process.read(65536)
            except Exception:
                break
            if data:
                recorder.feed(data)

    threading.Thread(target=pump, daemon=True).start()

    shots: dict[str, str] = {}
    for keys, minimum, name, mark, uses_model in STEPS:
        if keys is not None:
            if uses_model:
                script_reply(server)
            pairs = keys if isinstance(keys, list) else [(0.0, keys)]
            for delay, text in pairs:
                if delay:
                    time.sleep(delay)
                process.write(text)
        wait_for(recorder, mark, minimum, minimum + MAX_EXTRA_WAIT)
        # 源码与冻结产物的自动滚动会稳定差一行：抓帧前按 End 让消息区统一滚到底。
        # 但 /help 那一步焦点在对话框的滚动容器里，End 会被它吃掉、把对话框滚到底，
        # 顶部的快捷键表与「斜杠命令」标题于是被挤出可视区。那一帧要的是对话框本身，
        # 这个键只对消息区有意义，所以那一帧不按。
        if name != "help":
            process.write("\x1b[F")
        time.sleep(2.0)
        shots[name] = recorder.snapshot()

    stop.set()
    process.terminate(force=True)
    server.stop()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in shots.items():
        (OUTPUT_DIR / f"{prefix}_{name}.txt").write_text(text + "\n", encoding="utf-8")
    (OUTPUT_DIR / f"{prefix}_raw.bin").write_bytes(bytes(recorder.raw))

    failures = [
        f"{name} 画面里没有出现 {mark!r}"
        for _, _, name, mark, _ in STEPS
        if mark not in shots.get(name, "")
    ]
    tail = bytes(recorder.raw).decode("utf-8", "replace")
    failures.extend(f"输出里出现 {mark}" for mark in BAD_MARKS if mark in tail)

    print(f"画面已写入 {OUTPUT_DIR}（前缀 {prefix}_）")
    if failures:
        for item in failures:
            print("失败：", item)
        return 1
    print(f"{prefix} 验证通过：" + " -> ".join(name for _, _, name, _, _ in STEPS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
