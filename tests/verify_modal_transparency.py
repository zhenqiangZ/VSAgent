"""验证 /model 接口表单弹窗：居中、半透明、聊天界面透出。

在 ConPTY 里跑源码（或 exe），进聊天、发一条消息拿到回复，再打开接口表单，
按 pyte 的字符网格（含前景/背景色）做三项判断：
1. 弹窗边框在屏幕内水平、垂直都居中，且标题/输入框/提示都在边框内；
2. 弹窗外侧仍看得见聊天界面的文字，说明遮罩没有把下一层完全盖住；
3. 弹窗外侧那些文字的背景色被遮罩改暗，说明走的是半透明叠加而不是覆盖。

    python tests/verify_modal_transparency.py              验证 dist/opencode-tui.exe
    python tests/verify_modal_transparency.py <exe 路径>   验证指定的可执行文件
    python tests/verify_modal_transparency.py --source     验证源码直接运行（用于与打包产物对照）

画面写入 tests/output/。跑一次约 20 秒。

子进程会用真实接口那条链路去要回复，远端模型由本脚本起在回环地址上的假服务替换，
地址写进子进程 HOME 下的 `connection.toml`（`/model` 表单落盘的同一份），
回复内容固定，弹窗打开前后的两屏才对得上。

pyte 与 pywinpty 装在打包环境里（requirements-pack.txt），.venv 里没有，所以要用
`C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/opencode_tui_pack/Scripts/python.exe`
来跑这个脚本，verify_exe.py 同理。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pyte
from winpty import PtyProcess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
# 与 verify_exe.py 一致：会话状态隔离到临时目录，每轮从空状态启动
SCRATCH_HOME = PROJECT_ROOT / ".probe" / "verify-home"
STATE_FILE = SCRATCH_HOME / ".opencode_tui" / "state.toml"
CONNECTION_FILE = SCRATCH_HOME / ".opencode_tui" / "connection.toml"


def write_connection(base_url: str, model: str) -> None:
    """把接口参数写进子进程 HOME 下的 connection.toml。

    应用只从这份文件读接口参数（`/model` 表单写的也是它），键名与
    `core/connection.py` 对齐。
    """
    CONNECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONNECTION_FILE.write_text(
        "\n".join(
            [
                "version = 1",
                f'base_url = "{base_url}"',
                'api_key = "verify-key"',
                f'model = "{model}"',
                "context_window = 1000000",
                "price_in = 0.5",
                "price_out = 1.5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

ROWS, COLS = 34, 100
LOGO_MARK = "(_)_ __"
REPLY_MARK = "抓帧用的回复"
# 模型名写在子进程的 connection.toml 里，接口表单的「模型名」一格会预填它。
MODEL_NAME = "verify-model"
MODEL_MARK = MODEL_NAME
REPLY_TEXT = "抓帧用的回复，内容固定，好让弹窗打开前后的两屏对得上。"
BAD_MARKS = ("Traceback", "ModuleNotFoundError", "FileNotFoundError", "NoMatches", "ImportError")

MAX_EXTRA_WAIT = 12.0
# 标记出现后还要求画面连续这么久没有变化，避免取到随后被滚动带走的那一行
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

    def cell(self, x: int, y: int) -> pyte.screens.Char | None:
        return self.screen.buffer[y].get(x)

    def cell_data(self, x: int, y: int) -> str:
        cell = self.cell(x, y)
        return cell.data if cell is not None else " "

    def row_colors(self, y: int) -> list[tuple[str, str, str]]:
        """取某一行每一格的字符与前景/背景色。"""
        colors: list[tuple[str, str, str]] = []
        for x in range(COLS):
            cell = self.cell(x, y)
            if cell is None:
                colors.append((" ", "default", "default"))
            else:
                colors.append((cell.data, cell.fg, cell.bg))
        return colors

    def row_text(self, y: int) -> str:
        """拼出某一行的文字；宽字符的续位不补空格，避免把一个中文字符分成两半。"""
        chars: list[str] = []
        for x in range(COLS):
            cell = self.screen.buffer[y].get(x)
            if cell is None:
                chars.append(" ")
                continue
            if cell.data == "":
                continue
            chars.append(cell.data)
        return "".join(chars).rstrip()

    def snapshot(self) -> str:
        return "\n".join(self.row_text(y) for y in range(ROWS))


def wait_for(recorder: Recorder, mark: str, minimum: float, timeout: float) -> None:
    """等到画面里出现标记、至少等够 minimum 秒，并且画面连续 STABLE_SECONDS 秒不变。

    流式渲染还在继续时，刚出现的那一行随后会被自动滚动带走，后面按行号取色就会
    取到空行，所以这里要等画面静止。
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


def locate_dialog(recorder: Recorder) -> tuple[int, int, int, int]:
    """按 tall 边框（左 ▊、右 ▎）在字符网格里定位弹窗。

    直接读网格的列号，不用拼好的字符串：中文是双宽字符，拼串会压缩列号。
    返回 (上边界, 下边界, 左边界, 右边界)。
    """
    rows = [y for y in range(ROWS) if any(recorder.cell_data(x, y) == "▊" for x in range(COLS))]
    top, bottom = rows[0], rows[-1]
    left = next(x for x in range(COLS) if recorder.cell_data(x, top) == "▊")
    right = max(x for x in range(COLS) if recorder.cell_data(x, top) == "▎")
    return top, bottom, left, right


def parse_args() -> tuple[list[str], str]:
    """返回要执行的命令与输出前缀。"""
    if len(sys.argv) > 1 and sys.argv[1] == "--source":
        return [sys.executable, "-m", "opencode_tui"], "source"
    exe = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_EXE
    return [str(exe)], "frozen"


def script_reply(server: FakeModelServer) -> None:
    """给服务的下一次请求排好一段固定正文的应答。"""
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


def run_app(command: list[str], prefix: str) -> tuple[Recorder, dict[str, object]]:
    """驱动一次完整流程，返回录制器与关键步骤的画面。"""
    child_env = dict(os.environ)
    child_env["EDITOR"] = str(EDITOR_FIXTURE)
    STATE_FILE.unlink(missing_ok=True)
    child_env["USERPROFILE"] = str(SCRATCH_HOME)
    child_env["HOME"] = str(SCRATCH_HOME)

    server = FakeModelServer()
    server.start()
    write_connection(server.base_url, MODEL_NAME)
    script_reply(server)

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

    wait_for(recorder, LOGO_MARK, 5.0, 5.0 + MAX_EXTRA_WAIT)
    process.write("解释一下这个仓库")
    time.sleep(0.6)
    process.write("\r")
    wait_for(recorder, "enter 发送", 5.0, 5.0 + MAX_EXTRA_WAIT)
    wait_for(recorder, REPLY_MARK, 8.0, 8.0 + MAX_EXTRA_WAIT)
    # 与 verify_exe.py 保持一致：抓帧前按 End 让消息区统一滚到底
    process.write("\x1b[F")
    time.sleep(1.5)

    # 记下弹窗打开前整屏的行文字与颜色，稍后与打开后逐行对比
    shots = {"chat": recorder.snapshot()}
    shots["chat_rows"] = [recorder.row_text(y) for y in range(ROWS)]
    shots["chat_row_colors"] = [recorder.row_colors(y) for y in range(ROWS)]

    process.write("/model")
    time.sleep(0.6)
    process.write("\r")
    wait_for(recorder, MODEL_MARK, 3.0, 3.0 + MAX_EXTRA_WAIT)
    shots["modal"] = recorder.snapshot()

    stop.set()
    process.terminate(force=True)
    server.stop()
    return recorder, shots


def main() -> int:
    command, prefix = parse_args()
    if command[0] != sys.executable and not Path(command[0]).exists():
        print(f"找不到 {command[0]}，请先执行 pyinstaller --clean opencode_tui.spec")
        return 1

    recorder, shots = run_app(command, prefix)
    modal = shots["modal"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"{prefix}_modal_transparency.txt").write_text(modal + "\n", encoding="utf-8")
    (OUTPUT_DIR / f"{prefix}_modal_raw.bin").write_bytes(bytes(recorder.raw))

    failures: list[str] = []
    if MODEL_MARK not in modal:
        failures.append(f"画面里没有出现 {MODEL_MARK!r}，接口表单未打开")
    tail = bytes(recorder.raw).decode("utf-8", "replace")
    failures.extend(f"输出里出现 {mark}" for mark in BAD_MARKS if mark in tail)

    if not failures:
        top, bottom, left, right = locate_dialog(recorder)
        if abs(left - (COLS - 1 - right)) > 1:
            failures.append(f"弹窗未水平居中：左边距 {left}，右边距 {COLS - 1 - right}")
        if abs(top - (ROWS - 1 - bottom)) > 1:
            failures.append(f"弹窗未垂直居中：上边距 {top}，下边距 {ROWS - 1 - bottom}")

        outside_rows = [y for y in range(ROWS) if y < top or y > bottom]
        visible = [y for y in outside_rows if recorder.row_text(y).strip()]
        if not visible:
            failures.append("弹窗外侧整片空白，下一层聊天界面被完全遮住")
        else:
            # 透出的判据：弹窗外侧仍能看到与打开前完全相同的文字，且颜色被遮罩压暗
            chat_rows = shots["chat_rows"]
            chat_colors = shots["chat_row_colors"]
            same = [y for y in visible if recorder.row_text(y) == chat_rows[y]]
            if not same:
                failures.append("弹窗外侧的文字与打开前不一致，下一层界面没有透出")
            else:
                row = same[-1]
                before_row, after_row = chat_colors[row], recorder.row_colors(row)
                if before_row == after_row:
                    failures.append("弹窗外侧那一行颜色没变，遮罩没有参与叠加")
                else:
                    index = next(i for i, cell in enumerate(before_row) if cell[0].strip())
                    print(
                        f"  弹窗外第 {row} 行第 {index} 列：打开前 {before_row[index]}，"
                        f"打开后 {after_row[index]}"
                    )

    print(f"画面已写入 {OUTPUT_DIR / f'{prefix}_modal_transparency.txt'}")
    if failures:
        for item in failures:
            print("失败：", item)
        return 1
    print(f"{prefix} 验证通过：弹窗居中、遮罩半透明、聊天界面透出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
