from __future__ import annotations

import asyncio
import importlib
import json
import os
import pkgutil
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import opencode_tui  # noqa: E402
import re  # noqa: E402
from textual import events  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.css.stylesheet import Stylesheet  # noqa: E402
from textual.scrollbar import ScrollTo  # noqa: E402
from textual.widget import Widget  # noqa: E402
from textual.widgets import Input, ListView  # noqa: E402

from opencode_tui.app import OpenCodeApp  # noqa: E402
from opencode_tui.core.agent.memory import MemoryStore  # noqa: E402
from opencode_tui.core.agent.permission import PermissionPolicy, Verdict  # noqa: E402
from opencode_tui.core.agent.prompt import KEEP_MESSAGES  # noqa: E402
from opencode_tui.core.command_registry import CommandRegistry  # noqa: E402
from opencode_tui.core.connection import (  # noqa: E402
    Connection,
    connection_path_for,
    save_connection,
)
from opencode_tui.core.event_bus import EventBus  # noqa: E402
from opencode_tui.core.exporter import export_filename, render_session_markdown  # noqa: E402
from opencode_tui.core.external_editor import (  # noqa: E402
    build_command,
    editor_buffer_path,
    read_buffer,
    resolve_editor,
    split_editor_command,
    write_buffer,
)
from opencode_tui.core.file_refs import resolve_file_refs  # noqa: E402
from opencode_tui.core.models import EFFORTS, Message, Part, PartType, ToolStatus  # noqa: E402
from opencode_tui.core.project_scan import BEGIN_MARK, END_MARK  # noqa: E402
from opencode_tui.core.session_manager import SessionManager  # noqa: E402
from opencode_tui.screens.chat import ChatScreen, SidePanel  # noqa: E402
from opencode_tui.screens.connection_form import ConnectionFormScreen  # noqa: E402
from opencode_tui.screens.help import HelpScreen  # noqa: E402
from opencode_tui.screens.home import HomeInput, HomeScreen  # noqa: E402
from opencode_tui.screens.message import MessageScreen  # noqa: E402
from opencode_tui.screens.permission import PermissionDialog  # noqa: E402
from opencode_tui.screens.session_picker import (  # noqa: E402
    ConfirmDialog,
    RenameDialog,
    SessionPickerScreen,
)
from opencode_tui.screens.theme_selector import ThemeSelectorScreen  # noqa: E402
from opencode_tui.screens.tools_panel import ToolsPanelScreen  # noqa: E402
from opencode_tui.widgets.agent_autocomplete import AGENTS  # noqa: E402
from opencode_tui.widgets.command_palette import CommandPalette  # noqa: E402
from opencode_tui.widgets.editor_pane import (  # noqa: E402
    IGNORED_DIRS,
    EditorPane,
    search_files,
)
from opencode_tui.widgets.messages_pane import (  # noqa: E402
    HISTORY_PAGE,
    MAX_MOUNTED,
    MessagesPane,
    ReasoningPartView,
    SimplePartView,
    TextPartView,
    ToolCard,
)
from opencode_tui.widgets.status_bar import (  # noqa: E402
    StatusBar,
    format_tokens,
    git_branch,
    shorten_path,
)

import opencode_tui.widgets.status_bar as status_bar_module  # noqa: E402

# test_agent 与本文件同目录。按文件路径加载本文件时（`.probe/check_command_coverage.py`
# 就是这么做的）`tests` 不在搜索路径上，这里自己补一次。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent import CHECKS as AGENT_CHECKS  # noqa: E402
from test_agent import run_agent_checks  # noqa: E402

# 界面这一侧的生成走真实接口那条链路，远端模型由回环地址上的假服务替换。
from fake_model_server import (  # noqa: E402
    FakeModelServer,
    Reply,
    delta_chunk,
    reasoning_chunks,
    text_chunks,
    tool_call_delta,
    usage_chunk,
)

STATE_PATH = ROOT / ".probe" / "state_test.toml"

# 一轮回复的脚本。第一步返回思考、正文、一次工具调用，第二步给出收尾正文。
# 界面这一侧的断言都建立在这一段固定的内容上。
REPLY_TEXT = "这是界面测试用的流式回复，分片到达之后再合并。"
SUMMARY_TEXT = "看完了，就这些。"
TOOL_MARK = "echo hi"
TOOL_OUTPUT = "hi"

# 每条分片之间的间隔。窗口合并那一条断言要求窗口里真的攒得住多份增量：分片间隔要明显短于
# 合并窗口，否则每条分片各自进入一个窗口，合并就看不出来。服务把所有分片一次写完的话，
# 界面上只看到一次更新，那条断言同样失去意义。
CHUNK_DELAY = 0.004

CHECKS: list[str] = []


def build_app(state_path: Path, **options: object) -> OpenCodeApp:
    """冒烟测试用的应用：判定层不拦任何工具。

    这一组断言验的是界面。默认策略会让 bash 停下来等答复，而这里排的应答脚本
    带一个 bash 工具调用，界面上没有东西去答复它。判定那一条链由 test_agent.py 验，
    弹窗自己那几条断言在第 4 步单独开，那一条自己传 policy。
    """
    return OpenCodeApp(
        root=ROOT,
        state_path=state_path,
        policy=PermissionPolicy(default_ask=frozenset()),
        **options,  # type: ignore[arg-type]
    )


def script_reply(server: FakeModelServer) -> None:
    """给服务的下一次与下下次请求排好应答。"""
    server.script(
        Reply(
            chunks=[
                *reasoning_chunks("先看一下上下文。", size=4),
                *text_chunks(REPLY_TEXT, size=2),
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="bash",
                            arguments=json.dumps({"command": TOOL_MARK}),
                        )
                    ]
                ),
                delta_chunk(finish_reason="tool_calls"),
                usage_chunk(12, 34),
            ],
            delay=CHUNK_DELAY,
        ),
        Reply(
            chunks=[
                *text_chunks(SUMMARY_TEXT, size=3),
                delta_chunk(finish_reason="stop"),
                usage_chunk(20, 8),
            ],
            delay=CHUNK_DELAY,
        ),
    )


def check(condition: bool, label: str) -> None:
    """记录一条断言结果，失败时直接抛出。"""
    if not condition:
        raise AssertionError(label)
    CHECKS.append(label)


def rendered_lines(widget: Widget) -> list[str]:
    """把一个组件子树画出来的每一行取成文本，空行丢掉。

    Markdown 把正文铺在一棵子组件树上，父组件自己 render() 出来是空的，
    要连着子树一起读才看得到实际文字。
    """
    return [
        stripped
        for child in widget.walk_children(with_self=True)
        for index in range(child.region.height)
        for stripped in (child.render_line(index).text.strip(),)
        if stripped
    ]


async def wait_idle(app: OpenCodeApp, pilot, timeout: float = 8.0) -> None:
    """等待当前生成任务结束。"""
    waited = 0.0
    while waited < timeout:
        screen = app.screen
        if isinstance(screen, ChatScreen) and not screen._busy:
            return
        await pilot.pause(0.1)
        waited += 0.1
    raise AssertionError("等待生成任务结束超时")


async def type_text(pilot, text: str) -> None:
    """逐字符输入文本。"""
    await pilot.press(*list(text))


def latest_toast(app: OpenCodeApp) -> str:
    """取最近一条提示的文字。

    断言带上它才分得清两种情形：命令根本没走到（提示是别的内容），
    还是走到了而当着没东西可撤销（提示会写「没有可撤销的消息」）。
    """
    bodies = [str(toast.content) for toast in app.screen.query(".toast-body")]
    return f"「{bodies[-1]}」" if bodies else "（没有提示）"


def state_on_disk() -> SessionManager:
    """把状态文件读成一份新的会话管理器。

    判据不能是"文件里有某个字符串"，而要跟内存里那一份逐个会话对比：
    改完内存要立刻写盘，等下一个保存点的后果是这里读到的还是旧状态。
    """
    manager = SessionManager(state_path=STATE_PATH)
    manager.load_from_toml()
    return manager


def shape_of(manager: SessionManager) -> list[tuple[str, str, int]]:
    """会话的（编号, 标题, 消息条数），内存与磁盘比对这个形状。"""
    return [
        (session.id, session.title, len(session.messages))
        for session in manager.list_sessions()
    ]


def declared_bindings() -> list[tuple[type, str, str]]:
    """列出包里每条按键绑定声明的（类, 按键, 动作）。

    `Binding("ctrl+c", "quit_app", ...)` 这类绑定如果找不到对应的动作方法，
    Textual 不会报错，按下去也没有任何反应，所以要把这个对应关系静态查一遍。
    """
    found: list[tuple[type, str, str]] = []
    for info in pkgutil.walk_packages(opencode_tui.__path__, "opencode_tui."):
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if not isinstance(value, type) or value.__module__ != info.name:
                continue
            if not hasattr(value, "BINDINGS"):
                continue
            for entry in value.BINDINGS:
                # 两种写法都收：Binding 对象，以及 (按键, 动作, 说明) 三元组
                if isinstance(entry, tuple):
                    found.append((value, entry[0], entry[1]))
                else:
                    found.append((value, entry.key, entry.action))
    return found


def action_exists(cls: type, action: str) -> bool:
    """动作名能不能对应上一个方法。

    动作可以带参数（Textual 自己写成 `action_cursor_up(True)`），也可以带命名空间
    （`app.quit`），两种都要剥掉再查方法名。屏幕上是 `app.` 开头的动作由 App 提供。
    """
    name = action.split("(", 1)[0].rsplit(".", 1)[-1]
    return hasattr(cls, f"action_{name}") or hasattr(OpenCodeApp, f"action_{name}")


# 只在某个弹窗开着时才生效的按键，不算快捷键表里的一行。
CONTEXTUAL_KEYS = {"up", "down", "escape"}

# 走 SimplePartView 的 Part 类型。其余四种各自有专门的组件（正文、思考折叠块、工具卡片、补丁）。
SIMPLE_PART_TYPES = [
    PartType.FILE,
    PartType.AGENT,
    PartType.STEP_START,
    PartType.STEP_FINISH,
    PartType.PATCH,
    PartType.SUBTASK,
    PartType.RETRY,
    PartType.COMPACTION,
    PartType.SUMMARY,
]

# 循环层只产出 TEXT / REASONING / TOOL / STEP_START / STEP_FINISH，剩下的类型靠这里的样本过一遍渲染分支。
PART_SAMPLES: dict[PartType, Part] = {
    PartType.FILE: Part(type=PartType.FILE, file_path="opencode_tui/app.py", text="1\n2\n3"),
    PartType.AGENT: Part(type=PartType.AGENT, agent_name="Build"),
    PartType.STEP_START: Part(type=PartType.STEP_START, text="start"),
    PartType.STEP_FINISH: Part(type=PartType.STEP_FINISH, text="finish"),
    PartType.PATCH: Part(type=PartType.PATCH, text="opencode_tui/app.py", patch_added=12, patch_removed=3),
    PartType.SUBTASK: Part(type=PartType.SUBTASK, agent_name="explore", text="找一下入口"),
    PartType.RETRY: Part(type=PartType.RETRY, text="第 2 次"),
    PartType.COMPACTION: Part(type=PartType.COMPACTION, text="省略了 3 条"),
    PartType.ERROR: Part(type=PartType.ERROR, text="请求没有成功：服务暂时不可用"),
    PartType.SUMMARY: Part(type=PartType.SUMMARY, text="一、这一轮停下了"),
}


async def run_coalesce_checks() -> None:
    """流式文本的合并窗口：连着来的更新只重写一次。

    正文与思考块重写一次都不便宜（Markdown 要重解析，思考块是整段 Static 重排），
    所以这里数「重写了几次」而不是量耗时——次数是确定的，耗时在慢机器上会飘。
    """

    class Demo(App):
        def compose(self) -> ComposeResult:
            self.fold = ReasoningPartView(Part(type=PartType.REASONING))
            self.text = TextPartView(Part(type=PartType.TEXT))
            yield self.fold
            yield self.text

    app = Demo()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        writes: list[str] = []
        real_write = app.fold.write_text

        def counting(text: str) -> None:
            writes.append(text)
            real_write(text)

        app.fold.write_text = counting  # type: ignore[method-assign]

        part = Part(type=PartType.REASONING)
        # 一口气推 20 段、中间不让出事件循环：合并窗口还没到点，一次都不该写。
        for index in range(20):
            part.text += f"第 {index} 句。"
            app.fold.sync(part)
        check(not writes, f"连着来的更新没有一段一次重写：{len(writes)} 次")

        await pilot.pause(0.3)
        check(len(writes) == 1, f"窗口到点之后只重写一次：{len(writes)} 次")
        check(
            str(app.fold._body.render()) == part.text,
            f"写出去的是最后一份文本：{str(app.fold._body.render())!r}",
        )

        # 一轮结束时不等窗口：`flush_text` 立刻把压在里面的最后一段写出来。
        part.text += "收尾。"
        app.fold.sync(part)
        app.fold.flush_text()
        check(len(writes) == 2, f"收尾那次不等窗口：{len(writes)} 次")
        check(
            str(app.fold._body.render()).endswith("收尾。"),
            f"收尾那段也写出来了：{str(app.fold._body.render())!r}",
        )


async def run_tools_panel_checks() -> None:
    """`/tools` 面板：列出全部工具、逐条开关、改完立刻生效。

    单独开一个应用，`root` 指向 `.probe/` 下的一份副本：面板会写
    `root/.opencode_tui/tools.toml`，用仓库根那个会把开发机上的真实开关冲掉。
    """
    root = ROOT / ".probe" / "tools_panel"
    shutil.rmtree(root, ignore_errors=True)
    config = root / ".opencode_tui"
    config.mkdir(parents=True, exist_ok=True)
    tools_file = config / "tools.toml"
    tools_file.write_text("[tools]\nsearch_pdb = false\n", encoding="utf-8")

    app = OpenCodeApp(
        root=root,
        state_path=root / "state.toml",
        policy=PermissionPolicy(default_ask=frozenset()),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.execute_command("/tools")
        await pilot.pause()
        screen = app.screen
        check(isinstance(screen, ToolsPanelScreen), f"/tools 打开的是工具面板：{type(screen).__name__}")
        if not isinstance(screen, ToolsPanelScreen):
            return

        view = screen.query_one("#tools-list", ListView)

        def titles() -> list[str]:
            return [str(item.query_one(".tool-row-title").render()) for item in view.children]

        # 面板要列的是**全部**工具：只列开着的那些，关掉之后就再也打不开了。
        check(
            len(titles()) == len(app.tool_catalog()),
            f"面板列出全部工具：画面 {len(titles())}，全部 {len(app.tool_catalog())}",
        )
        check("○ search_pdb" in titles(), f"关掉的工具是空心圆：{titles()[:4]}")
        check("● bash" in titles(), "开着的工具是实心圆")
        check(
            f"{len(app.tool_catalog()) - 1}/{len(app.tool_catalog())} 已启用"
            in str(screen.query_one("#tools-title").render()),
            f"标题栏报开着几个：{str(screen.query_one('#tools-title').render())!r}",
        )
        check("search_pdb" not in app.agent.registry.names(), "开面板之前它就不在注册表里")

        # 光标移到那一行，按空格打开。
        index = [entry.name for entry in screen.entries].index("search_pdb")
        view.index = index
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        check(
            "● search_pdb" in str(view.children[index].query_one(".tool-row-title").render()),
            f"空格打开被关掉的工具：{str(view.children[index].query_one('.tool-row-title').render())!r}",
        )
        check("search_pdb" in app.agent.registry.names(), "打开之后工具立刻进了注册表，不用重启")
        check(
            "search_pdb" in {spec["function"]["name"] for spec in app.agent.registry.specs()},
            "打开之后它在请求体的 tools 数组里",
        )
        check(
            "search_pdb = false" not in tools_file.read_text(encoding="utf-8"),
            f"打开之后文件里不再有它：{tools_file.read_text(encoding='utf-8')!r}",
        )

        # 再按一次关回去：写盘、注册表、请求体三处要一起变。
        await pilot.press("space")
        await pilot.pause()
        check(
            "○ search_pdb" in str(view.children[index].query_one(".tool-row-title").render()),
            "再按一次关掉",
        )
        check("search_pdb" not in app.agent.registry.names(), "关掉之后它又不在注册表里了")
        check(
            "search_pdb = false" in tools_file.read_text(encoding="utf-8"),
            f"关掉之后写回文件：{tools_file.read_text(encoding='utf-8')!r}",
        )

        # enter 与空格是同一件事：面板上没写「按空格」以外的键位提示，
        # 但回车是这类列表的默认动作，不该按了没反应。
        await pilot.press("enter")
        await pilot.pause()
        check(
            "search_pdb" in app.agent.registry.names(),
            "回车也能开关，不是只有空格",
        )
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()
        check(not isinstance(app.screen, ToolsPanelScreen), "esc 关掉面板，回到原来的界面")

    shutil.rmtree(root, ignore_errors=True)


async def run() -> None:
    server = FakeModelServer()
    server.start()
    # 接口参数写进与状态文件同目录的 connection.toml：所有测试应用的 state_path
    # 都在 `.probe/` 下，因此这一份对它们同时生效。
    save_connection(
        connection_path_for(STATE_PATH),
        Connection(server.base_url, "secret", "smoke-model", context_window=1_000_000),
    )
    try:
        await run_tool_card_checks()
        await run_coalesce_checks()
        await run_tools_panel_checks()
        await run_interface_checks(server)
    finally:
        server.stop()


async def run_tool_card_checks() -> None:
    """工具卡：两级折叠、工具图标、执行计时。

    这几条不依赖会话，单独开一个只放一张卡片的小应用，断言不用绕开别的组件。
    """
    long_output = "\n".join(f"line {i:02d}" for i in range(40))

    def make_part(status: ToolStatus, age: float = 0.0) -> Part:
        return Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=status,
            tool_input={"command": "echo hi"},
            tool_output=long_output,
            timestamp=time.time() - age,
        )

    class Demo(App):
        def compose(self) -> ComposeResult:
            self.card = ToolCard(make_part(ToolStatus.COMPLETED))
            self.card.sync(make_part(ToolStatus.COMPLETED))
            yield self.card

    app = Demo()
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.2)
        card = app.card
        header = str(card.query_one(".tool-header").render())
        check("$ bash" in header, f"卡片头上有工具图标：{header}")

        output = card.query_one(".tool-output")
        tail = card.query_one(".tool-tail")
        more = card.query_one(".tool-more")
        check(
            str(output.render()).splitlines() == ["line 00", "line 01", "line 02"],
            f"折叠时只留开头三行：{str(output.render()).splitlines()[:4]}",
        )
        check(
            str(tail.render()).splitlines() == ["line 37", "line 38", "line 39"],
            f"折叠时末尾三行还在：{str(tail.render()).splitlines()}",
        )
        check("省略 34 行" in str(more.render()), f"省略行写明折了多少行：{more.render()}")

        # 点省略行展开：省略行留在原地（不然展开几百行之后「收起」就滚出屏幕了），
        # 中间那一段接在它后面。
        await pilot.click(more)
        await pilot.pause()
        check("收起" in str(more.render()), f"展开之后省略行变成「收起」：{more.render()}")
        check(
            len(str(tail.render()).splitlines()) == 37,
            f"展开之后中间那段接在省略行后面：{len(str(tail.render()).splitlines())} 行",
        )
        check(
            str(output.render()).splitlines() == ["line 00", "line 01", "line 02"],
            "展开不挪动省略行，开头三行仍在它上面",
        )

        # 点正文不该有任何动作：用户点一下常常只是想读或选中。
        await pilot.click(output)
        await pilot.pause()
        check("收起" in str(more.render()), "点正文不会把展开的输出去掉")
        check(str(output.styles.display) == "block", "点正文不会把输出收起来")

        await pilot.click(more)
        await pilot.pause()
        check("省略 34 行" in str(more.render()), "再点一次折回去")

        # 标题行才是整段输出的开关。
        await pilot.click(card.query_one(".tool-header"))
        await pilot.pause()
        check(str(output.styles.display) == "none", "点标题行收起整段输出")
        check(str(more.styles.display) == "none", "收起时省略行也一起收起")
        check(str(tail.styles.display) == "none", "收起时末尾三行也一起收起")
        await pilot.click(card.query_one(".tool-header"))
        await pilot.pause()
        check(str(output.styles.display) == "block", "再点标题行展开整段输出")

        # 执行计时：看到「执行中」才开始计时，完成后停在最终值。
        card.sync(make_part(ToolStatus.RUNNING, age=5))
        header = str(card.query_one(".tool-header").render())
        check(
            re.search(r"执行中 \d+(\.\d+)?s", header) is not None,
            f"执行中的卡片带耗时：{header}",
        )
        card.sync(make_part(ToolStatus.COMPLETED, age=5))
        header = str(card.query_one(".tool-header").render())
        check(
            re.search(r"完成 \d+(\.\d+)?s", header) is not None,
            f"完成之后耗时停在最终值：{header}",
        )

        # 旧会话里建出来的完成态卡片：时间戳是几天前，不能算出一个几天的耗时。
        old = ToolCard(make_part(ToolStatus.COMPLETED, age=86400))
        old.sync(make_part(ToolStatus.COMPLETED, age=86400))
        header = str(old._header.render())
        check(
            re.search(r"\d+(\.\d+)?s", header) is None,
            f"没亲眼看到执行中的卡片不显示耗时：{header}",
        )


PERMISSION_STATE_PATH = ROOT / ".probe" / "state_permission.toml"

# 询问弹窗上四个按键各自的期望：工具最终状态，以及卡片上留下的输出。
# escape 与 n 是同一个动作，两条都排进来是为了让"随手按一下不会放过去"这一条有据可查。
PERMISSION_CASES: list[tuple[str, ToolStatus, str]] = [
    ("y", ToolStatus.COMPLETED, TOOL_OUTPUT),
    ("n", ToolStatus.FAILED, "用户拒绝"),
    ("a", ToolStatus.COMPLETED, TOOL_OUTPUT),
    ("escape", ToolStatus.FAILED, "用户拒绝"),
]


async def wait_for_screen(app: OpenCodeApp, pilot, kind: type, timeout: float = 8.0) -> object:
    """等某一类屏幕成为最上面那一层，并且已经铺好内容。

    `App._push_screen` 先把屏幕同步放进 `_screen_stack`，再等挂载完成
    （`textual/app.py:3022` 是那一次放进，`await await_mount()` 在它后面）。
    所以 `app.screen` 变成弹窗的那一刻，弹窗里的组件还没建出来，
    这时候去 `query_one("#permission-title")` 会抛 `NoMatches`。
    要连组件建出来一起等，只看 `isinstance` 会偶发失败。
    """
    waited = 0.0
    while waited < timeout:
        screen = app.screen
        if isinstance(screen, kind) and screen.is_mounted and len(screen.children) > 0:
            return screen
        await pilot.pause(0.1)
        waited += 0.1
    raise AssertionError(
        f"等待 {kind.__name__} 出现超时，当前最上面是 {type(app.screen).__name__}"
    )


def dialog_text(dialog: PermissionDialog) -> str:
    """把弹窗上的文字拼成一段，便于断言。

    原因那一行是条件渲染的（没有原因就不出），用 `query` 而不是 `query_one`，
    少一个组件时这里不会先抛 NoMatches。
    """
    selectors = (
        "#permission-title",
        "#permission-detail",
        "#permission-label",
        "#permission-argument-text",
        "#permission-reason",
        "#permission-hint",
    )
    return " ".join(
        str(node.render()) for selector in selectors for node in dialog.query(selector)
    )


def last_tool_part(app: OpenCodeApp) -> Part:
    """当前会话里最后一张工具卡片对应的 Part。"""
    for message in reversed(app.session_manager.current.messages):
        for part in reversed(message.parts):
            if part.type is PartType.TOOL:
                return part
    raise AssertionError("这一轮没有产生工具 Part")


async def run_permission_round(server: FakeModelServer, key: str) -> tuple[ToolStatus, str]:
    """按一个按键走完一轮生成，返回工具最终状态与卡片上的输出。"""
    server.clear()
    script_reply(server)
    PERMISSION_STATE_PATH.unlink(missing_ok=True)
    app = OpenCodeApp(root=ROOT, state_path=PERMISSION_STATE_PATH)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await type_text(pilot, "run it")
        await pilot.press("enter")
        dialog = await wait_for_screen(app, pilot, PermissionDialog)
        assert isinstance(dialog, PermissionDialog)
        shown = dialog_text(dialog)
        check("bash" in shown, f"弹窗里写着要调用哪个工具：{shown}")
        check(TOOL_MARK in shown, f"弹窗里写着这次调用的参数：{shown}")
        check("原因" in shown, f"弹窗里写着被拦下来的原因：{shown}")
        hint = str(dialog.query_one("#permission-hint").render())
        # a 键放行的是**本会话的所有工具**，提示行要如实写出来。
        # 同时点明它管不到工作目录之外，否则用户会以为 a 是一张通吃整个磁盘的许可证。
        check(
            "放行所有工具" in hint and "工作目录之外" in hint,
            f"提示行写明 a 键的真实范围（所有工具，且管不到工作目录之外）：{hint}",
        )
        preview = dialog.query_one("#permission-argument")
        check(preview.region.height >= 3, f"参数单独放在带边框的预览框里：{preview.region}")
        # 标签跟着工具走：bash 的主参数是命令，不是笼统的「参数」。
        check(
            str(dialog.query_one("#permission-label").render()) == "命令",
            f"参数标签按工具命名：{dialog.query_one('#permission-label').render()!r}",
        )
        check(
            TOOL_MARK in str(dialog.query_one("#permission-argument-text").render()),
            "预览框里是这次要执行的那条命令",
        )
        check(
            dialog.query_one("#permission-hint").region.height >= 2,
            "提示行分成两行，按键与范围说明都完整显示",
        )
        await pilot.press(key)
        await wait_idle(app, pilot)
        check(isinstance(app.screen, ChatScreen), f"按 {key} 之后弹窗关掉，回到聊天界面")
        part = last_tool_part(app)
        if key == "a":
            tool = app.agent.registry.get("bash")
            assert tool is not None
            decision = app.agent.policy.check(tool, {"command": TOOL_MARK}, "Build")
            check(
                decision.verdict is Verdict.ALLOW and "本会话" in decision.rule,
                f"按 a 之后同样的调用不再问：{decision.note()}",
            )
            # 换一条**内容完全不同**的命令也放行：这是「本会话全放行」的关键，
            # 按命令原文记的时候这一条会失败。
            other = app.agent.policy.check(tool, {"command": "echo 完全不同的命令"}, "Build")
            check(
                other.verdict is Verdict.ALLOW,
                f"按 a 之后同工具的另一条命令也放行：{other.note()}",
            )
            # 别的默认要问的工具也一起放行——这就是「所有工具」的含义。
            write = app.agent.registry.get("write")
            assert write is not None
            crossed = app.agent.policy.check(write, {"path": "a.txt", "content": "x"}, "Build")
            check(
                crossed.verdict is Verdict.ALLOW,
                f"按 a 之后别的工具也放行：{crossed.note()}",
            )
            # 但工作目录之外仍然要问：放行的是工具，不是磁盘。
            # 这一条是「范围扩大之后边界还在」的证据，不是可选项。
            # 越界路径按平台取：POSIX 上反斜杠不是分隔符，`D:\x` 只是站内的文件名。
            outside_path = r"D:\Elsewhere\a.txt" if os.name == "nt" else "/etc/passwd"
            outside = app.agent.policy.check(
                write, {"path": outside_path, "content": "x"}, "Build"
            )
            check(
                outside.verdict is Verdict.ASK,
                f"按 a 之后工作目录之外仍要问：{outside.note()}",
            )
        return part.tool_status, part.tool_output


async def run_permission_checks(server: FakeModelServer) -> None:
    """权限弹窗：四个按键各走一轮完整生成。"""
    for key, want_status, want_output in PERMISSION_CASES:
        status, output = await run_permission_round(server, key)
        check(
            status is want_status,
            f"按 {key} 之后工具状态是 {want_status.value}：实际 {status}",
        )
        check(
            output == want_output,
            f"按 {key} 之后卡片上的输出是「{want_output}」：实际「{output}」",
        )
    PERMISSION_STATE_PATH.unlink(missing_ok=True)


async def run_interface_checks(server: FakeModelServer) -> None:
    STATE_PATH.unlink(missing_ok=True)
    script_reply(server)
    app = build_app(STATE_PATH)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        check(isinstance(app.screen, HomeScreen), "启动后进入 HomeScreen")
        logo = str(app.screen.query_one("#home-logo").content)
        check(len(logo.splitlines()) >= 3, "HomeScreen 显示多行 ASCII 标题")
        check(logo.rstrip("\n").splitlines()[-1].strip() != "", "标题最后一行不是空行")

        home = app.screen
        box = home.query_one("#home-box").region
        help_line = home.query_one("#home-help").region
        footer = home.query_one("#home-footer").region
        version = home.query_one("#home-footer-right").region
        path_label = home.query_one("#home-footer-left").content
        check(2 * box.x + box.width == app.size.width, "核心文本框在屏幕内水平居中")
        check(help_line.right == box.right, "帮助行与文本框右边缘位置相同")
        check(footer.bottom == app.size.height, "底部状态栏贴在屏幕最底")
        check(version.right == app.size.width - 2, "版本号靠右显示")
        check(str(path_label) == str(ROOT), "底部状态栏左侧显示项目路径")
        check(app.focused is not None and app.focused.id == "home-input", "启动后焦点在输入框")

        bar = home.query_one("#home-accent-bar")
        check(bar.region.width == 1, "左侧竖条宽度为 1 列")
        check(bar.region.x == box.x, "左侧竖条贴在文本框最左边缘")
        check(bar.region.height == box.height, "左侧竖条贯穿文本框整个高度")
        check(bar.rich_style.bgcolor.name == "#4a9eff", "输入框聚焦时竖条为亮蓝色")
        check(
            home.query_one("#home-input").rich_style.bgcolor.name
            == home.query_one("#home-box").rich_style.bgcolor.name,
            "聚焦时输入区底色与区块一致（区块被 focus-within 抬亮，输入区要跟着抬）",
        )
        check(
            home.query_one("#home-input", HomeInput).highlight_cursor_line is False,
            "启动界面输入框关掉了光标行底色",
        )
        home.query_one("#home-input", HomeInput).blur()
        await pilot.pause()
        await pilot.pause()
        check(bar.rich_style.bgcolor.name == "#2a2a2a", "输入框失焦后竖条变暗")
        home.query_one("#home-input", HomeInput).focus()
        await pilot.pause()
        await pilot.pause()
        check(bar.rich_style.bgcolor.name == "#4a9eff", "重新聚焦后竖条恢复亮蓝色")

        field = home.query_one("#home-input", HomeInput)
        row = home.query_one("#home-prompt-row")
        info = home.query_one("#home-info")
        check(info.region.y - row.region.bottom == 1, "输入行与状态行之间留出间距")
        check(field.region.height == 1, "空输入时文本框只有一行")
        field.text = "a" * 300
        await pilot.pause()
        check(field.region.height > 1, "输入变长后文本框换行增高")
        check(home.query_one("#home-box").region.height > box.height, "文本框整体跟着输入增高")
        check(
            home.query_one("#home-prompt-mark").region.y == field.region.y,
            "提示符与输入框首行位置一致",
        )
        check(bar.region.height == home.query_one("#home-box").region.height, "左侧竖条跟着文本框一起增高")
        field.text = ""
        await pilot.pause()
        check(field.region.height == 1, "清空输入后文本框收回一行")
        await pilot.press(*("abcdefghij" * 60))
        await pilot.pause()
        check(len(field.text) == 600, "连续输入全部写入输入框")
        check(field.region.height == 6, "输入超过高度上界后文本框不再增高")
        check(field.scroll_y == field.max_scroll_y and field.scroll_y > 0, "超出上界后文本框滚到底")
        top_line = field.render_line(0).text.rstrip()
        check(
            top_line != field.document.get_line(0)[: len(top_line)],
            "滚到底后文本框首行已不是输入的最开始",
        )
        field.text = ""
        await pilot.pause()
        check(field.region.height == 1, "清空后文本框复位")

        await pilot.press("tab")
        await pilot.pause()
        check(app.session_manager.last_agent == "Plan", "tab 在启动界面切换 Agent")
        check("Plan" in str(home.query_one("#home-info").content), "状态行跟着切换 Agent")
        await pilot.press("tab")
        await pilot.pause()
        check(app.session_manager.last_agent == "Build", "再按 tab 切回原来的 Agent")

        await pilot.press("ctrl+p")
        await pilot.pause()
        check(isinstance(app.screen, CommandPalette), "ctrl+p 打开命令面板")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, HomeScreen), "esc 关闭命令面板回到启动界面")

        await type_text(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, ConnectionFormScreen), "启动界面上 /model 打开接口表单")
        check(
            app.screen.query_one("#connection-model", Input).value == app.agent.config.model,
            "表单里预填了当前模型名",
        )
        check(
            app.screen.query_one("#connection-base-url", Input).value
            == app.agent.config.base_url,
            "表单里预填了当前接口地址",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, HomeScreen), "关闭接口表单回到启动界面")

        await type_text(pilot, "from home")
        await pilot.press("enter")
        for _ in range(50):
            if isinstance(app.screen, ChatScreen):
                break
            await pilot.pause(0.1)
        check(isinstance(app.screen, ChatScreen), "在启动界面输入回车进入 ChatScreen")
        check(
            app.session_manager.current.messages[0].parts[0].text == "from home",
            "启动界面的输入作为首条消息发出",
        )
        await wait_idle(app, pilot)

        chat = app.screen
        editor = chat.query_one(EditorPane)
        pane = chat.query_one(MessagesPane)

        # 刚进来还没在编辑器里提交过任何输入，历史是空的：往上往下翻都要什么都不做。
        # 上一条消息是从启动界面发出去的，走的是另一个组件，不会进这里的历史。
        check(editor.history == [], f"刚进聊天界面时历史为空：{editor.history}")
        await pilot.press("ctrl+up")
        await pilot.pause()
        check(editor.text == "" and editor._history_index == -1, "历史为空时往上翻不动")
        await pilot.press("ctrl+down")
        await pilot.pause()
        check(editor.text == "" and editor._history_index == -1, "历史为空时往下翻不动")

        # 输入框区块：最左侧那条 1 列宽的竖线在聚焦时点亮成亮蓝，失焦转暗。
        # 颜色读的是渲染后的样式：只写后代选择器时焦点变化不会触发重算，读不到新颜色。
        accent = editor.query_one("#editor-accent")
        check(accent.region.width == 1, "输入框左侧指示线宽 1 列")
        check(
            editor.input.highlight_cursor_line is False,
            "输入框关掉了光标行底色（那一层 $boost 随主题取值，会让光标所在行整行变一个颜色）",
        )
        check(
            accent.region.y == editor.input.content_region.y,
            f"指示线首行与正文首行同一行：线 {accent.region.y}，文字 {editor.input.content_region.y}",
        )
        accent_bottom = accent.region.y + accent.region.height - 1
        content = editor.content_region
        check(
            accent.region.y == content.y and accent_bottom == content.y + content.height - 1,
            f"竖线首行是区块内容区的首行、末行是内容区的末行："
            f"线 {accent.region.y}..{accent_bottom}，内容 {content.y}..{content.y + content.height - 1}"
            "（竖线与上界线之间夹一行空白时，那一行空白会被当成输入行）",
        )
        check(
            editor.query_one("#editor-input").rich_style.bgcolor.name
            == editor.rich_style.bgcolor.name,
            "输入区底色与区块一致（不写的话用的是 TextArea 自带的 $surface，换成 gruvbox 就亮出一档）",
        )
        check(
            accent.rich_style.bgcolor.name == "#4a9eff",
            f"输入框聚焦时指示线为亮蓝：{accent.rich_style.bgcolor.name}",
        )
        editor.input.blur()
        await pilot.pause()
        await pilot.pause()
        check(
            accent.rich_style.bgcolor.name == "#2a2a2a",
            f"输入框失焦后指示线转暗：{accent.rich_style.bgcolor.name}",
        )
        editor.focus_input()
        await pilot.pause()
        await pilot.pause()
        check(accent.rich_style.bgcolor.name == "#4a9eff", "重新聚焦后指示线恢复亮蓝")

        # 光标是白底深字的方块。光标落在空格上时只有底色看得见，蓝底会和强调色混在一起，
        # 看不出现在打到哪一格。
        cursor_style = editor.input.get_component_rich_style("text-area--cursor")
        check(
            cursor_style.bgcolor.name == "#ffffff",
            f"输入光标为白色方块：{cursor_style.bgcolor.name}",
        )
        check(
            cursor_style.color.name == "#0a0a0a",
            f"光标下的字用深色，白底上才看得清：{cursor_style.color.name}",
        )
        # 消息区右侧的滚动条取纯白。默认档跟随主题的 $scrollbar，在纯黑底上偏暗。
        # 这里读的是 `styles` 上的 textual 颜色对象，取 `.hex`（大写、带 #），不是 rich 的 `.name`。
        check(
            pane.styles.scrollbar_color.hex.lower() == "#ffffff",
            f"消息区滚动条为白色：{pane.styles.scrollbar_color.hex}",
        )

        # 输入区的高度跟着内容走，与启动界面 `#home-input` 同一套：随内容长高、
        # 6 行封顶，再多出来的行由输入框自己滚动。区别是这里给了 3 行底高——
        # 聊天界面空着的时候框太窄，贴着上界线看起来像没地方输入。
        # 区块贴着底边、高度 auto，所以长高时是上边缘往上退、消息区被压短，
        # 状态行与状态栏不动；提交清空之后整块退回 6 行。
        editor.input.text = ""
        await pilot.pause()
        await pilot.pause()
        empty_height = editor.input.region.height
        check(empty_height == 3, f"空输入时输入区占 3 行底高：{empty_height}")
        check(
            editor.region.height == 6,
            f"空输入时区块共 6 行（上界线 1 + 输入 3 + 间距 1 + 状态行 1）：{editor.region.height}",
        )
        pane_bottom = editor.region.bottom
        status_bar = app.screen.query_one("#status-bar")
        status_height = status_bar.region.height

        editor.input.text = "第一行\n第二行\n第三行\n第四行\n第五行"
        await pilot.pause()
        await pilot.pause()
        check(
            editor.input.region.height == 5,
            f"输入 5 行时输入区跟着长到 5 行：{editor.input.region.height}",
        )
        check(
            editor.region.bottom == pane_bottom,
            f"长高时区块贴着底边往上长：底边 {editor.region.bottom}（原本 {pane_bottom}）",
        )

        editor.input.text = "\n".join(f"第 {index} 行" for index in range(20))
        await pilot.pause()
        await pilot.pause()
        rows_needed = editor.input.virtual_size.height
        check(
            editor.input.region.height == 6,
            f"输入 20 行时输入区 6 行封顶：{editor.input.region.height}",
        )
        check(
            rows_needed > editor.input.region.height,
            f"封顶之后多出来的行由输入框自己滚动："
            f"内容 {rows_needed} 行，输入区 {editor.input.region.height} 行",
        )
        check(
            editor.region.bottom == pane_bottom,
            f"封顶后区块底边仍然不动：{editor.region.bottom}（原本 {pane_bottom}）",
        )
        check(
            status_bar.region.height == status_height,
            f"状态栏不被长高的输入区挤掉：{status_bar.region.height}（原本 {status_height}）",
        )

        editor.input.text = ""
        await pilot.pause()
        await pilot.pause()
        check(
            editor.input.region.height == empty_height,
            f"清空后输入区退回 3 行底高：{editor.input.region.height}",
        )

        # 光标要跟着滚动走到底。TextArea 在光标移动时只滚一次，用的还是插入之前算出来的
        # 滚动上限（刚插进去的那一行还没进虚拟高度），滚动量被截掉一行；内容超出可见高度
        # 之后光标就停在输入区外面一行，此后每插入一行都差这一行，屏幕上找不到光标。
        editor.input.clear()
        for index in range(12):
            editor.input.insert(f"第 {index} 行\n")
        await pilot.pause()
        await pilot.pause()
        cursor = editor.input.cursor_screen_offset
        content = editor.input.content_region
        check(
            editor.input.virtual_size.height > editor.input.region.height,
            f"这一段的输入内容确实超过了输入区的高度，下一条断言才有意义："
            f"内容 {editor.input.virtual_size.height} 行，输入区 {editor.input.region.height} 行",
        )
        check(
            content.y <= cursor.y <= content.y + content.height - 1,
            f"输入内容超出可见高度之后光标仍在输入区里：光标 y {cursor.y}，"
            f"输入区内容区 y {content.y}..{content.y + content.height - 1}",
        )

        editor.input.text = ""
        await pilot.pause()
        await pilot.pause()

        agent_label = editor.query_one("#editor-agent")
        model_label = editor.query_one("#editor-model")
        effort_label = editor.query_one("#editor-effort")
        hint_label = editor.query_one("#editor-hint")
        check(
            editor.query_one("#editor-status").region.y > editor.input.region.y,
            "模式与模型信息排在输入区下方",
        )
        check(str(agent_label.render()) == "Build", f"状态行显示当前 Agent：{agent_label.render()}")
        check(
            accent.region.y + accent.region.height - 1 == agent_label.region.y,
            f"状态行与指示线末行同一行：线末行 {accent.region.y + accent.region.height - 1}，"
            f"状态行 {agent_label.region.y}",
        )
        check(
            agent_label.rich_style.color.name == "#4a9eff",
            f"Agent 标签为蓝色：{agent_label.rich_style.color.name}",
        )
        check(
            str(model_label.render()) == app.current_model_label,
            f"状态行显示当前模型：{model_label.render()}，实际 {app.current_model_label}",
        )
        check(
            model_label.rich_style.color.name == "#c9d1d9",
            f"模型名为浅灰：{model_label.rich_style.color.name}",
        )
        check(str(effort_label.render()) == "low", f"状态行显示推理强度：{effort_label.render()}")
        check(
            effort_label.rich_style.color.name == "#e3b341",
            f"推理强度为橙黄：{effort_label.rich_style.color.name}",
        )
        check(str(hint_label.render()) == "chat", f"状态行右端显示输入模式：{hint_label.render()}")
        check(
            hint_label.rich_style.color.name == "#5a5a5a",
            f"模式提示为暗灰：{hint_label.rich_style.color.name}",
        )
        # 输入每变一个字符都会走到 _update_mode，而模式绝大多数时候一个字都没变。
        # 原来的写法每次按键都把三个模式类删一遍再加回一个，并且把状态行四个标签全写一遍；
        # Static.update() 每次都带走一遍整屏重新布局，实测每个字符 13.9 毫秒。
        hint_writes: list[str] = []
        real_hint_update = hint_label.update

        def counting_hint_update(text: object = "", **kwargs: object) -> object:
            hint_writes.append(str(text))
            return real_hint_update(text, **kwargs)

        hint_label.update = counting_hint_update  # type: ignore[method-assign]
        try:
            await type_text(pilot, "abcde")
            check(
                hint_writes == [],
                f"模式没变时状态行一个字都不重写：{hint_writes}",
            )
            editor.text = "/help"
            await pilot.pause()
            check(
                str(hint_label.render()) == "命令",
                f"斜杠开头时模式提示换成命令：{hint_label.render()}",
            )
            check(hint_writes == ["命令"], f"模式真变了才重写状态行：{hint_writes}")
        finally:
            hint_label.update = real_hint_update  # type: ignore[method-assign]

        editor.text = ""
        await pilot.pause()
        check(str(hint_label.render()) == "chat", "清空之后模式提示回到 chat")

        # 底部状态栏：左端项目路径，右端用量与快捷键提示，整体暗灰。
        bar = chat.query_one(StatusBar)
        left = bar.query_one("#status-left")
        usage = bar.query_one("#status-usage")
        keys = bar.query_one("#status-keys")
        check(
            str(left.render()).startswith(shorten_path(ROOT)),
            f"状态栏左端显示项目路径：{left.render()}",
        )
        check(
            left.rich_style.color.name == "#5a5a5a",
            f"路径为暗灰：{left.rich_style.color.name}",
        )
        check(
            keys.region.right == app.size.width - 1,
            f"快捷键提示靠右贴边：右端 {keys.region.right}，屏幕宽 {app.size.width}",
        )
        check(
            str(keys.render()) == "ctrl+p commands",
            f"右端提示为命令面板快捷键：{keys.render()}",
        )
        usage_text = str(usage.render())
        check(
            "(" in usage_text and "%)" in usage_text and "$" in usage_text and usage_text.endswith("s"),
            f"右端用量是 用量(百分比) · 费用 · 耗时 的形式：{usage_text}",
        )
        check(
            usage.region.right <= keys.region.x,
            f"用量排在快捷键提示左边：用量右端 {usage.region.right}，提示左端 {keys.region.x}",
        )
        chat._busy = True
        chat.refresh_status()
        check("● 生成中" in str(left.render()), f"生成中时路径后加标记：{left.render()}")
        chat._busy = False
        chat.refresh_status()
        check("● 生成中" not in str(left.render()), "生成结束后标记撤掉")

        # 刷新状态栏不能在事件循环上同步起 git 子进程：进聊天界面这段路上要刷好几次，
        # 每次同步等 110 到 220 毫秒，这段时间键盘与重绘全部排队。
        branch_calls: list[Path] = []
        real_git_branch = status_bar_module.git_branch

        def counting_git_branch(cwd: Path) -> str:
            branch_calls.append(cwd)
            return real_git_branch(cwd)

        status_bar_module.git_branch = counting_git_branch  # type: ignore[assignment]
        try:
            chat.refresh_status()
            chat.refresh_status()
            chat.refresh_status()
            check(
                branch_calls == [],
                f"刷新状态栏不再同步查 git 分支：刷了 3 次，查了 {len(branch_calls)} 次",
            )
        finally:
            status_bar_module.git_branch = real_git_branch  # type: ignore[assignment]

        # 分支改由后台线程去问，问到了再补进左端；路径本身在第一帧就画出来了。
        for _ in range(40):
            if bar._branch:
                break
            await pilot.pause(0.1)
        expected_branch = real_git_branch(bar._cwd)
        check(
            bar._branch == expected_branch,
            f"后台线程把分支写回了状态栏：{bar._branch!r}，期望 {expected_branch!r}",
        )
        check(
            str(left.render()).startswith(shorten_path(bar._cwd)),
            f"补进分支之后左端仍以路径开头：{left.render()}",
        )

        await pilot.press("enter")
        await pilot.pause()
        check(not chat._busy, "空输入回车不会开始生成")
        check(editor.history == [], f"空输入回车不写进历史：{editor.history}")

        # 流式文本每来一块就把整段 Markdown 重新解析一遍，一段回复下来是平方级的开销。
        # 两次重解析之间隔一个短窗口，窗口里连着来的更新合并成一次。
        script_reply(server)
        parses: list[int] = []
        stream_updates: list[str] = []
        real_text_update = TextPartView.update

        def counting_text_update(self: TextPartView, markdown: object = "", **kwargs: object) -> object:
            parses.append(len(str(markdown)))
            return real_text_update(self, markdown, **kwargs)

        app.event_bus.subscribe(
            "part.update", lambda data: stream_updates.append(str(data.get("part_id")))
        )
        TextPartView.update = counting_text_update  # type: ignore[method-assign]
        try:
            await type_text(pilot, "hello")
            check(editor.mode == "chat", "普通输入处于 chat 模式")
            await pilot.press("enter")
            await pilot.pause()
            check(len(pane._message_views) >= 1, "提交后出现消息")
            check(
                app.session_manager.current.messages[0].role == "user",
                "提交后会话首条消息为 user 消息",
            )

            # 用户消息画成一条对话框：浅灰底 + 最左侧一条蓝线，不再写"你"这样的角色标签。
            # 先等这一轮跑完再看尺寸：回复还在流的时候消息区的高度正在长，
            # 纵向滚动条出现的那一帧与子视图重新排布的那一帧不是同一帧，
            # 中间读到的宽度会差滚动条的那两列。
            await wait_idle(app, pilot)
            user_view = pane.query(".message-user").first()
            check(
                user_view.styles.background.hex.lower() == "#1e1e1e",
                f"用户消息带浅灰底色：{user_view.styles.background}",
            )
            check(
                user_view.styles.border_left[1].hex.lower() == "#4a9eff",
                f"用户消息最左侧是蓝色竖线：{user_view.styles.border_left}",
            )
            # 按可滚动内容区的宽度比对：内容够长时消息区会出现纵向滚动条，
            # 滚动条占掉的那两列不算气泡该铺的范围。
            check(
                user_view.region.width == pane.scrollable_content_region.width,
                f"用户消息横贯消息区的整行：气泡 {user_view.region.width} 列，"
                f"可滚动内容区 {pane.scrollable_content_region.width} 列，"
                f"消息区 {pane.region.width} 列",
            )
            check(len(user_view.query(".message-header")) == 0, "用户消息不画角色标签")
            user_text = user_view.query(".part-text").first()
            check(
                user_text.region.y - user_view.region.y == 1,
                f"正文不贴着气泡顶边，蓝线高过文字：气泡 {user_view.region.y} 正文 {user_text.region.y}",
            )
            check(
                user_view.region.height == 1 + user_text.region.height,
                f"气泡只在顶部多一行内边距，正文的后边距不再叠一层："
                f"气泡 {user_view.region.height} 行，正文 {user_text.region.height} 行",
            )
            user_lines = rendered_lines(user_text)
            check(user_lines == ["from home"], f"用户消息只显示提问本身：{user_lines}")

            await wait_idle(app, pilot)
            check(
                bool(stream_updates) and len(parses) <= len(stream_updates) // 2,
                f"流式文本合并之后再解析：{len(stream_updates)} 次更新只解析了 {len(parses)} 次",
            )
        finally:
            TextPartView.update = real_text_update  # type: ignore[method-assign]

        # 合并窗口里最后一份文本必须写出来，否则屏幕上会比会话里少一段。
        pane_by_part = pane._part_views
        stale = [
            part.id
            for message in app.session_manager.current.messages
            for part in message.parts
            if isinstance(pane_by_part.get(part.id), TextPartView)
            and pane_by_part[part.id]._shown_text != part.text
        ]
        check(not stale, f"一轮结束后屏幕上的文本是最新的一份，没有压在窗口里：{stale}")

        session = app.session_manager.current
        assistant = session.messages[-1]
        check(assistant.role == "assistant", "生成结束后追加 assistant 消息")
        # 一步里第二次问模型之前已经写过的正文与它之后新写的那一段是两个 Part，
        # 都挂在这一条消息上。
        text_parts = [part for part in assistant.parts if part.type is PartType.TEXT]
        check(
            [part.text for part in text_parts] == [REPLY_TEXT, SUMMARY_TEXT],
            f"两步的正文各成一段，都挂在同一条消息上：{[part.text for part in text_parts]}",
        )
        tool_parts = [part for part in assistant.parts if part.type is PartType.TOOL]
        check(bool(tool_parts), "assistant 消息包含工具 Part")
        check(tool_parts[0].tool_status is ToolStatus.COMPLETED, "工具 Part 最终为 completed")
        check(tool_parts[0].tool_name == "bash", f"工具名来自模型：{tool_parts[0].tool_name}")
        check(
            TOOL_OUTPUT in tool_parts[0].tool_output,
            f"工具卡片上写着真实输出：{tool_parts[0].tool_output!r}",
        )
        check(assistant.total_tokens() > 0, "生成结束后写入 token 统计")
        check(
            any(part.type is PartType.STEP_FINISH for part in assistant.parts),
            "一轮结束补上 STEP_FINISH",
        )

        # 回复消息不铺底也不画竖线，只靠左内边距与用户气泡里的正文起始列保持一致。
        reply_view = pane.query(".message-assistant").first()
        check(len(reply_view.query(".message-header")) == 0, "回复消息不画角色标签")
        check(
            reply_view.styles.background.a == 0,
            f"回复消息不铺底色：{reply_view.styles.background}",
        )
        check(
            reply_view.styles.border_left[0] in ("", "none"),
            f"回复消息不画竖线：{reply_view.styles.border_left}",
        )
        user_text = user_view.query(".part-text").first()
        reply_text = reply_view.query(".part-text").first()
        check(
            user_text.region.x == reply_text.region.x,
            f"用户提问与回复正文起始列相同：用户 {user_text.region.x}，回复 {reply_text.region.x}",
        )
        # 上面比的是组件位置，这里比真正画出来的文字起始列：`region` 含内边距，
        # 而两边的内边距来源不同（正文是 Markdown 自带的，气泡与回复是外层给的）。
        check(
            user_text.content_region.x == reply_text.content_region.x,
            f"用户提问与回复正文的文字起始列相同：{user_text.content_region.x}，{reply_text.content_region.x}",
        )
        # 走 SimplePartView 的那几类（step 分隔线、引用文件、补丁摘要）自己补上与正文同一档的
        # 两列内边距，否则它们的文字会比同一条消息里别的行左移两列。
        dividers = list(reply_view.query(".part-step_start"))
        check(bool(dividers), "这一轮回复里出现了 step 分隔线，才谈得上比对它的文字起始列")
        check(
            dividers[0].content_region.x == reply_text.content_region.x,
            f"step 分隔线的文字起始列与正文一致：{dividers[0].content_region.x}，{reply_text.content_region.x}",
        )

        # 思考块默认折叠，标题上写着"点击展开"，点一下要真的展开。这条路径由 Textual
        # 按消息名回调，方法签名不带事件参数，能不能被调到只能实测。
        # 这一步趁消息区只有这一条回复、内容还放得下一屏时做，点的是屏幕坐标。
        fold = pane.query(ReasoningPartView).first()
        check(not fold._expanded, "思考块默认是折叠的")
        check(
            str(fold._body.styles.display) == "none",
            f"折叠时正文不显示：display={fold._body.styles.display}",
        )
        # 消息区这时是滚到底的，靠前的标题可能已经在可视区之上。点的是屏幕坐标，
        # 先等画面稳住再把它滚进视野：生成结束时 `_on_turn_done` 排过一次"滚到底并
        # 恢复跟随"（`call_after_refresh`），那次滚动若挤在下面这几步里跑掉，标题就被
        # 推出视口，而 `pilot.click` 是在发事件之前把坐标算好的，点击于是打到别的组件上
        # （实测报出滚动量从 4 变成 20、标题跑到视口上方 16 行）。滚动量连着两次读数
        # 相同就说明排着的那次已经跑完。
        fold.scroll_visible(animate=False)
        await pilot.pause()
        window = pane.region
        for _ in range(10):
            settled = pane.scroll_y
            await pilot.pause()
            if pane.scroll_y == settled:
                break
        for _ in range(5):
            header_region = fold._header.region
            if window.y <= header_region.y and header_region.bottom <= window.y + window.height:
                break
            pane.scroll_relative(y=header_region.y - window.y - 1, animate=False)
            await pilot.pause()
        header_region = fold._header.region
        check(
            header_region.height > 0
            and window.y <= header_region.y
            and header_region.bottom <= window.y + window.height,
            f"思考块标题整行都在可视区里：标题 {header_region}，消息区 {window}",
        )
        # 点的是屏幕坐标：`pilot.click` 取标题左上角那一格的屏幕位置，然后依次发下按、抬起、
        # 单击三个事件。这三个事件之间布局只要挪动一行，点到的就是上一行——折叠状态的标题
        # 只有一行高，上一行是它上面那个 Part。判据于是要分两步：先问点击有没有点到标题上
        # （`pilot.click` 的返回值报的就是这件事），再看展开没有。合成一条的话，点空与
        # "点击没生效"报出来是同一句话，看不出该往哪查。
        check(not chat._busy, "点击之前这一轮已经跑完，画面上不会再往上加内容")
        # 跟随本身在别处单独验，这一步测的是"点一下标题会不会展开"，先把跟随关掉，
        # 免得标题展开之后的高度变化又把画面挪走。
        pane._follow = False
        before_click = (pane.scroll_y, fold._header.region)
        landed = await pilot.click(fold._header)
        await pilot.pause()
        check(
            isinstance(app.screen, ChatScreen),
            f"点击没有打开别的界面（点到补丁摘要上会推出 Diff 覆盖层）：{type(app.screen).__name__}",
        )
        check(
            landed,
            f"点击打在思考块标题上：点击前 scroll_y={before_click[0]} 标题 {before_click[1]}，"
            f"点击后 scroll_y={pane.scroll_y} following={pane.following} "
            f"标题 {fold._header.region}，消息区 {pane.region}",
        )
        check(fold._expanded, "点一下标题之后展开")
        check(
            str(fold._body.styles.display) == "block",
            f"展开之后正文显示出来：display={fold._body.styles.display}",
        )
        # 正文走的是合并窗口，最后一段由 `turn.done` 的 flush 写出来。这条断言盯的
        # 就是那次 flush：漏了它，思考块平时收起着看不出问题，一展开就是空的。
        check(
            "先看一下上下文" in str(fold._body.render()),
            f"展开之后看得到思考正文：{str(fold._body.render())!r}",
        )
        check(
            "点击折叠" in str(fold._header.content),
            f"标题上的提示跟着换：{str(fold._header.content)!r}",
        )
        # 点正文不能把展开的思考收起来：展开之后点它，多半是想读或者选中。
        await pilot.click(fold._body)
        await pilot.pause()
        check(fold._expanded, "点正文不会把展开的思考收起来")
        await pilot.click(fold._header)
        await pilot.pause()
        check(not fold._expanded, "再点一下收回折叠")
        editor.focus_input()
        await pilot.pause()

        # 两张"登记表"必须与画面上真实存在的视图对得上。组件有两条创建路径：
        # 流式追加走 add_part，会话重建时由 MessageView.compose 直接建；
        # 只在一条路里登记，另一条路建出来的视图就进不了表，`/thinking` 与 `/details`
        # 只会改数据与提示，画面一动不动。
        reasoning_views = pane.query(ReasoningPartView)
        tool_cards = pane.query(ToolCard)
        check(bool(reasoning_views) and bool(tool_cards), "思考块与工具卡都画出来了才验得了登记表")
        check(
            len(pane._reasoning_views) == len(reasoning_views),
            f"思考块视图与登记表对得上：画面 {len(reasoning_views)}，登记 {len(pane._reasoning_views)}",
        )
        check(
            len(pane._tool_cards) == len(tool_cards),
            f"工具卡与登记表对得上：画面 {len(tool_cards)}，登记 {len(pane._tool_cards)}",
        )

        # 工具输出：短的照原样显示，长的留开头与末尾各几行、中间折成一行，
        # 免得长输出把消息区刷过去，同时不让末尾的报错与退出码被切掉。
        card = tool_cards[0]
        original_output = card._part.tool_output
        card._part.tool_output = "只有一行输出"
        card.sync(card._part)
        check(
            str(card._output.render()) == "只有一行输出",
            f"短输出完整显示：{card._output.render()!r}",
        )
        card._part.tool_output = "\n".join(f"第 {index} 行" for index in range(1, 21))
        card.sync(card._part)
        long_shown = str(card._output.render())
        check(
            long_shown.startswith("第 1 行")
            and "第 3 行" in long_shown
            and "第 4 行" not in long_shown,
            f"长输出开头留到上界以内：{long_shown!r}",
        )
        long_tail = str(card._tail.render())
        check(
            long_tail.rstrip().endswith("第 20 行") and "第 18 行" in long_tail,
            f"长输出末尾照原样留到最后一行：{long_tail!r}",
        )
        check(
            "中间省略 14 行" in str(card._more.render()),
            f"中间折成一行写明省略多少行：{card._more.render()!r}",
        )
        card._part.tool_output = original_output
        card.sync(card._part)

        # 点标题行可以把这一条输出单独收起，再点一下展开。点的是屏幕坐标，先把头部滚进视野。
        # 这时思考块刚被点过，焦点在消息区里，点完要把焦点交还输入框。
        window = pane.region
        card.scroll_visible(animate=False)
        await pilot.pause()
        for _ in range(5):
            card_header = card._header.region
            if window.y <= card_header.y and card_header.bottom <= window.y + window.height:
                break
            pane.scroll_relative(y=card_header.y - window.y - 1, animate=False)
            await pilot.pause()
        card_header = card._header.region
        check(
            card_header.height > 0
            and window.y <= card_header.y
            and card_header.bottom <= window.y + window.height,
            f"工具卡头部整行都在可视区里：头部 {card_header}，消息区 {window}",
        )
        await pilot.click(card._header)
        await pilot.pause()
        check(str(card._output.styles.display) == "none", "点卡片把这条输出收起")
        check("▸" in str(card._header.render()), f"收起后头部标记换成 ▸：{card._header.render()!r}")
        await pilot.click(card._header)
        await pilot.pause()
        check(str(card._output.styles.display) == "block", "再点一下把这条输出展开")
        check("▾" in str(card._header.render()), f"展开后头部标记换成 ▾：{card._header.render()!r}")

        # 焦点进出消息区时只该重算消息区自己。Textual 的 `Widget.watch_has_focus` 走
        # `App.update_styles(self)`，把整棵子树都放进重算名单；消息区里每一段正文与每一张卡
        # 各自还有子树，一段会话下来是几千个组件（实测 296 条消息、4435 个组件时一次 2.6 到
        # 3.9 秒），而消息区点一下就获得焦点、点回输入框又失去焦点。样式表里没有一条规则靠
        # 焦点取值，所以只该重算它自己。
        sizes: list[int] = []
        original_update_nodes = Stylesheet.update_nodes

        def recording_update_nodes(stylesheet, nodes, animate=False):
            batch = list(nodes)
            sizes.append(len(batch))
            return original_update_nodes(stylesheet, batch, animate=animate)

        editor.focus_input()
        await pilot.pause()
        sizes.clear()
        Stylesheet.update_nodes = recording_update_nodes
        try:
            pane.focus()
            await pilot.pause()
        finally:
            Stylesheet.update_nodes = original_update_nodes
        check(sizes, "消息区获得焦点时确实重算了一次样式")
        check(
            max(sizes) <= 50,
            f"消息区获得焦点只重算它自己，不重算整棵子树：每次重算的组件数 {sizes}",
        )
        editor.focus_input()
        await pilot.pause()

        # `/thinking` 是唯一一条既接上了处理函数、又没有别处会顺带执行的命令。
        thinking_view = reasoning_views[0]
        check(str(thinking_view.styles.display) == "block", "开关打开时思考块显示在画面上")
        app.execute_command("thinking")
        await pilot.pause()
        check(not app.session_manager.show_thinking, "/thinking 关掉思考块开关")
        check(
            str(thinking_view.styles.display) == "none",
            f"/thinking 之后思考块从画面消失：display={thinking_view.styles.display}",
        )
        app.execute_command("thinking")
        await pilot.pause()
        check(app.session_manager.show_thinking, "/thinking 再执行一次打开开关")
        check(str(thinking_view.styles.display) == "block", "/thinking 之后思考块回到画面")

        await type_text(pilot, "/help")
        check(editor.mode == "command", "/ 开头进入命令模式")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, HelpScreen), "/help 打开帮助界面")
        keys_help = str(app.screen.query_one("#help-keys").content)
        check(
            "ctrl+p" in keys_help and "ctrl+c" in keys_help and "escape" in keys_help,
            f"帮助界面列出聊天界面的快捷键：{keys_help.splitlines()[:5]}",
        )
        check(
            "ctrl+x" not in keys_help,
            f"帮助界面不再列出前导键：{keys_help.splitlines()[:5]}",
        )
        check(
            "/sidebar" in str(app.screen.query_one("#help-commands").content),
            "帮助界面列出全部斜杠命令",
        )
        home_help = str(app.screen.query_one("#help-home").content)
        check("ctrl+p" in home_help and "tab" in home_help, "帮助界面单列启动界面的快捷键")
        check("ctrl+c" in home_help, "启动界面的退出键也在这一列")
        check(
            app.screen.query_one("#help-keys").region.y
            < app.screen.query_one("#help-home").region.y,
            "从聊天界面打开帮助时聊天界面那一段排在前",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "escape 关闭帮助界面")

        await type_text(pilot, "!echo hi")
        check(editor.mode == "bash", "! 开头进入 bash 模式")
        await pilot.press("enter")
        await wait_idle(app, pilot)
        bash_message = app.session_manager.current.messages[-1]
        bash_tools = [part for part in bash_message.parts if part.type is PartType.TOOL]
        check(
            bool(bash_tools) and TOOL_OUTPUT in bash_tools[0].tool_output,
            "bash 模式写入工具输出",
        )

        # 历史翻阅：提交过的内容会进 history，ctrl+up / ctrl+down 在它上面移动。
        editor.text = ""
        check(len(editor.history) >= 2, f"历史里至少两条才验证得了来回翻阅：{len(editor.history)}")
        await pilot.press("ctrl+up")
        await pilot.pause()
        check(editor.text == editor.history[-1], "ctrl+up 取回最近一条输入")
        await pilot.press("ctrl+up")
        await pilot.pause()
        check(editor.text == editor.history[-2], "再按一次取回更早的一条")
        await pilot.press("ctrl+down")
        await pilot.pause()
        check(editor.text == editor.history[-1], "ctrl+down 回到后一条")
        editor.text = ""

        await pilot.press("ctrl+p")
        await pilot.pause()
        check(isinstance(app.screen, CommandPalette), "ctrl+p 打开命令面板")
        await type_text(pilot, "model")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "escape 关闭命令面板")

        # 面板里移动高亮再回车执行：这一段接线（move / execute / 回调）在别处都没走过。
        await pilot.press("ctrl+p")
        await pilot.pause()
        palette = app.screen
        check(isinstance(palette, CommandPalette), "再次打开命令面板")
        total_commands = len(palette.registry.all())
        listed = len(palette._results)
        check(
            listed > 1,
            f"打开时列出多条命令，才验得了高亮移动：列了 {listed} 条（注册表共 {total_commands} 条）",
        )
        first_index = palette.query_one("#palette-list", ListView).index
        await pilot.press("down")
        await pilot.pause()
        moved_index = palette.query_one("#palette-list", ListView).index
        check(
            moved_index == (first_index or 0) + 1,
            f"down 把高亮往后挪一格：{first_index} -> {moved_index}",
        )
        await pilot.press("up")
        await pilot.pause()
        check(
            palette.query_one("#palette-list", ListView).index == first_index,
            "up 把高亮挪回原来那一格",
        )

        await type_text(pilot, "thinking")
        await pilot.pause()
        names = [command.name for command in palette._results]
        check(
            0 < len(names) < total_commands,
            f"输入之后候选被筛过：注册表 {total_commands} 条，筛完 {len(names)} 条 {names}",
        )
        check(
            bool(names) and names[0] == "thinking",
            f"输入 thinking 之后它排在第一条：{names}",
        )
        thinking_before = app.session_manager.show_thinking
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "面板里执行完命令之后回到聊天界面")
        check(
            app.session_manager.show_thinking is not thinking_before,
            f"面板里选中的命令真的执行了：thinking 之后开关从 {thinking_before} 变成 "
            f"{app.session_manager.show_thinking}",
        )
        app.execute_command("thinking")
        await pilot.pause()
        check(
            app.session_manager.show_thinking is thinking_before,
            "把思考块开关恢复成原样，后面几条检查不受影响",
        )

        await type_text(pilot, "/themes")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, ThemeSelectorScreen), "/themes 打开主题选择器")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "escape 关闭主题选择器")
        check(editor.text == "", "提交命令之后输入框清空")

        await type_text(pilot, "@app")
        await pilot.pause()
        check(editor.file_popup.is_open, "输入 @ 触发文件补全")
        check(bool(editor.file_popup.visible_items), "文件补全给出候选")
        # 候选按匹配程度排，头一个是谁随仓库里有什么文件而变，这里不假定某一个路径，
        # 记下真的采纳了哪一个，后面按它断言。
        accepted = editor.file_popup.current()
        check(bool(accepted), f"补全弹窗上有高亮候选：{accepted}")
        await pilot.press("enter")
        await pilot.pause()
        check("@" in editor.text, "enter 采纳文件补全候选")
        check(not editor.file_popup.is_open, "采纳后关闭文件补全")
        await pilot.press("enter")
        await wait_idle(app, pilot)
        user_parts = [
            part
            for message in app.session_manager.current.messages
            if message.role == "user"
            for part in message.parts
            if part.type is PartType.FILE
        ]
        check(bool(user_parts), "引用的文件写入 File Part")
        check(
            any(str(part.file_path).endswith(str(accepted)) for part in user_parts),
            f"引用的是采纳的那一个：{accepted}，实际 {[part.file_path for part in user_parts]}",
        )

        await type_text(pilot, "@app")
        await pilot.pause()
        check(editor.file_popup.is_open, "再次输入 @ 重新打开文件补全")
        await pilot.press("escape")
        await pilot.pause()
        check(not editor.file_popup.is_open, "escape 关闭文件补全")
        editor.text = ""

        # 候选多于一屏（弹窗一次只显示 6 行）时要一直能看到高亮在哪一项。
        # 只画前 6 个候选、标记又拿绝对下标去比，下移到第 7 个之后标记会整行消失，
        # 而回车仍然会插入那个看不见的候选。
        await pilot.press("@")
        await pilot.pause()
        popup = editor.file_popup
        popup_items = popup.visible_items
        check(len(popup_items) > 6, f"候选要多于一屏才验证得了高亮跟随：{len(popup_items)}")
        marks = []
        for _ in range(len(popup_items)):
            body = str(popup.query_one(".ac-body").content)
            marked = [
                line.strip() for line in body.splitlines() if line.strip().startswith("▸")
            ]
            marks.append((popup.current() in marked[0]) if len(marked) == 1 else False)
            await pilot.press("down")
            await pilot.pause()
        bad = [position for position, ok in enumerate(marks) if not ok]
        check(not bad, f"每个下标都有一行高亮且与选中项一致，异常下标：{bad}")
        await pilot.press("up")
        await pilot.pause()
        body = str(popup.query_one(".ac-body").content)
        marked = [line.strip() for line in body.splitlines() if line.strip().startswith("▸")]
        check(
            len(marked) == 1 and popup.current() in marked[0],
            "上移一格（会绕到最后一项）后高亮仍然可见",
        )
        await pilot.press("escape")
        await pilot.pause()
        editor.text = ""

        await pilot.press("tab")
        await pilot.pause()
        check(editor.agent_popup.is_open, "tab 打开 Agent 补全")
        await pilot.press("enter")
        await pilot.pause()
        check(app.session_manager.current.agent_name in ("Build", "Plan"), "enter 切换 Agent")

        # 焦点在消息区里时 EditorPane 的 tab 绑定不在按键链上，由屏幕那一条接住再转给编辑器。
        # 两条路都写在代码里，实际走到哪一条只能实测。
        editor.agent_popup.hide()
        await pilot.pause()
        pane.focus()
        await pilot.pause()
        check(app.focused is pane, f"焦点已经移到消息区：{app.focused!r}")
        await pilot.press("tab")
        await pilot.pause()
        check(editor.agent_popup.is_open, "焦点不在编辑器里时 tab 仍然打开 Agent 补全")
        # 焦点在消息区时编辑器不在按键链上，它的 escape 也轮不到，所以先把焦点交回去再关。
        editor.focus_input()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        check(not editor.agent_popup.is_open, "焦点回到编辑器后 escape 关掉 Agent 补全")

        check(editor.styles.border_top[1].hex.lower() == "#333333", "普通输入时编辑器上边框是灰色")

        # 斜杠命令补全：打一个 / 就把全部命令列出来，接着打字按名字筛，上下键挪高亮，
        # 回车采纳。这份清单原先只有 ctrl+p 命令面板与帮助界面里有，输入框里看不到。
        await type_text(pilot, "/")
        await pilot.pause()
        popup = editor.command_popup
        check(popup.is_open, "输入 / 打开命令补全")
        listed = len(popup.commands)
        total = len(app.command_registry.all())
        check(listed == total, f"打一个 / 列出全部命令：列了 {listed} 条，注册表 {total} 条")
        check(
            bool(popup.visible_items) and popup.visible_items[0].startswith("/compact"),
            f"候选按命令名排序，头一条是 /compact：{popup.visible_items[:2]}",
        )
        await type_text(pilot, "hel")
        await pilot.pause()
        names = [command.name for command in popup.commands]
        check(bool(names) and names[0] == "help", f"接着打字只留下匹配的命令：{names}")
        check(len(names) < total, f"筛完之后少于全部：{len(names)} / {total}")
        first_index = popup.index
        await pilot.press("down")
        await pilot.pause()
        check(
            popup.index == (first_index + 1) % len(names),
            f"down 把高亮往后挪一格：{first_index} -> {popup.index}",
        )
        await pilot.press("up")
        await pilot.pause()
        check(popup.index == first_index, "up 把高亮挪回原来那一格")
        await pilot.press("enter")
        await pilot.pause()
        check(editor.text == "/help", f"回车采纳高亮的命令名：{editor.text!r}")
        check(not popup.is_open, "命令名打全之后补全收起（不然回车永远只采纳候选）")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, HelpScreen), "再按一次回车执行补出来的命令")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "escape 关闭帮助界面")

        await type_text(pilot, "/")
        await pilot.pause()
        check(popup.is_open, "再次输入 / 重新打开命令补全")
        await pilot.press("escape")
        await pilot.pause()
        check(not popup.is_open, "escape 关闭命令补全")
        editor.text = ""
        await pilot.pause()

        # /details：切换工具详情。原先是前导键第二击，现在只有斜杠命令这一条路。
        details_before = app.session_manager.show_details
        await type_text(pilot, "/details")
        await pilot.press("enter")
        await pilot.pause()
        check(app.session_manager.show_details is not details_before, "/details 切换工具详情")
        card_output = pane.query(ToolCard)[0].query_one(".tool-output")
        expected = "block" if app.session_manager.show_details else "none"
        check(
            str(card_output.styles.display) == expected,
            f"工具详情开关作用到画面上：开关 {app.session_manager.show_details}，输出 display={card_output.styles.display}",
        )
        await type_text(pilot, "/details")
        await pilot.press("enter")
        await pilot.pause()
        check(str(card_output.styles.display) == "block", "再执行一次工具输出回到画面")

        panel = chat.query_one(SidePanel)
        pane_width = pane.region.width
        check(not panel.has_class("-visible"), "上下文侧栏默认隐藏")
        # 侧栏关着的时候底下两份清单（引用文件、工具调用）要把会话里全部 Part 走一遍，
        # 算出来只会写进一个不显示的组件，所以整块跳过。
        check(
            panel._written == {},
            f"侧栏关着时不计算内容：{sorted(panel._written)}",
        )
        await type_text(pilot, "/sidebar")
        await pilot.press("enter")
        await pilot.pause()
        check(panel.has_class("-visible"), "/sidebar 打开上下文侧栏")
        check(str(panel.styles.display) == "block", "侧栏打开后参与布局")
        check(panel.region.width == 30, "侧栏宽 30 列")
        check(panel.region.right == app.size.width, "侧栏贴在屏幕右边缘")
        check(pane.region.width == pane_width - 30, "消息区让出侧栏占用的宽度")
        check("New Session" in str(chat.query_one("#side-session").content), "侧栏显示会话标题")
        check(
            sorted(panel._written)
            == ["#side-files", "#side-session", "#side-stats", "#side-tools"],
            f"打开侧栏时把内容补齐：{sorted(panel._written)}",
        )
        check(
            len(panel.query("#side-meta")) == 0,
            "侧栏不再重复显示模型与 Agent（那一对已经在输入框下方的状态行上）",
        )
        side_lines = rendered_lines(panel)
        check(
            not any(app.current_model_label in line for line in side_lines),
            f"侧栏画出来的文字里没有模型名：{side_lines}",
        )
        session_now = app.session_manager.current
        check(
            f"{len(session_now.messages)} 条消息" in str(chat.query_one("#side-stats").content),
            "侧栏用量行按当前会话的消息条数统计",
        )
        check("◆ bash" in str(chat.query_one("#side-tools").content), "侧栏列出调用过的工具与状态")
        check(
            str(accepted) in str(chat.query_one("#side-files").content),
            f"侧栏列出引用过的文件：{accepted}",
        )
        await type_text(pilot, "/sidebar")
        await pilot.press("enter")
        await pilot.pause()
        check(not panel.has_class("-visible"), "/sidebar 关闭上下文侧栏")
        check(pane.region.width == pane_width, "关闭侧栏后消息区恢复原宽")
        check(
            "上下文侧栏：隐藏" in latest_toast(app),
            f"切换侧栏给出提示：{latest_toast(app)}",
        )

        app.execute_command("compact")
        await pilot.pause()
        check(bool(app.screen.query(".toast")), "压缩命令给出提示")
        check("尚未接入" not in latest_toast(app), f"压缩已经接上：{latest_toast(app)}")

        # 快捷键那一层不再挂着命令：包里不该再有按键去触发某一条命令，
        # 命令全靠 /名字 与 ctrl+p 面板这两条路。
        check(
            not [name for name in dir(ChatScreen) if name.startswith("action_leader_")],
            f"聊天界面上不再有前导键动作：{[name for name in dir(ChatScreen) if name.startswith('action_leader_')]}",
        )
        chat_keys = {entry.key for entry in ChatScreen.BINDINGS}
        check(
            chat_keys == {"ctrl+p", "ctrl+c", "tab"},
            f"聊天界面只留三个快捷键：{sorted(chat_keys)}",
        )

        await type_text(pilot, "/connect")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, MessageScreen), "/connect 打开提供商信息弹窗")
        shown = " ".join(
            str(getattr(node, "renderable", node.render()))
            for node in app.screen.query(".message-line")
        )
        check(app.agent.config.base_url in shown, f"弹窗里写着当前接口地址：{shown[:80]}")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "关掉提供商信息弹窗后回到聊天界面")

        log_path = app.trace.current_path()
        app.execute_command("log")
        await pilot.pause()
        check(isinstance(app.screen, MessageScreen), "/log 打开事件日志弹窗")
        shown_log = " ".join(
            str(getattr(node, "renderable", node.render()))
            for node in app.screen.query(".message-line")
        )
        check(str(log_path) in shown_log, f"弹窗里写着日志文件路径：{shown_log[:90]}")
        check(log_path.is_file(), "日志文件已经存在")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "关掉事件日志弹窗后回到聊天界面")

        # 记忆命令：/remember 写、/forget 删、/memory 列。
        # 三条都往项目根下的 .vsmemory 写，这里把记忆目录换到 .probe 下，
        # 免得跑一次测试就把仓库根写脏；跑完删掉。
        mem_fixture = ROOT / ".probe" / "smoke_memory"
        shutil.rmtree(mem_fixture, ignore_errors=True)
        mem_fixture.mkdir(parents=True, exist_ok=True)
        app._memory = MemoryStore(mem_fixture)

        app.execute_command("memory")
        await pilot.pause()
        check(
            "还是空的" in latest_toast(app),
            f"没有记忆时 /memory 给一句提示：{latest_toast(app)}",
        )

        app.execute_command("remember 受体结构在 receptor.pdb")
        await pilot.pause()
        check("已记住" in latest_toast(app), f"/remember 写入一条：{latest_toast(app)}")
        check(app._memory.entries_path.is_file(), "/remember 之后条目文件已写出")
        check(
            app._memory.memory_path.is_file()
            and "受体结构在 receptor.pdb" in app._memory.memory_path.read_text(encoding="utf-8"),
            "给人看的 memory.md 也写出来，正文在里面",
        )

        app.execute_command("remember")
        await pilot.pause()
        check(
            "用法" in latest_toast(app),
            f"/remember 不带参数给用法提示：{latest_toast(app)}",
        )

        app.execute_command("memory")
        await pilot.pause()
        check(isinstance(app.screen, MessageScreen), "/memory 打开记忆弹窗")
        listed = " ".join(
            str(getattr(node, "renderable", node.render()))
            for node in app.screen.query(".message-line")
        )
        check("受体结构在 receptor.pdb" in listed, f"弹窗里列出这条记忆：{listed[:90]}")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "关掉记忆弹窗后回到聊天界面")

        # 关键词只命中一条时直接删掉。
        app.execute_command("remember 配体库在 ligands.sdf")
        await pilot.pause()
        app.execute_command("forget 配体")
        await pilot.pause()
        check("已删除" in latest_toast(app), f"/forget 用关键词删一条：{latest_toast(app)}")

        app.execute_command("forget 查无此物")
        await pilot.pause()
        check(
            "没有匹配" in latest_toast(app),
            f"/forget 没命中时给提示：{latest_toast(app)}",
        )

        app.execute_command("forget")
        await pilot.pause()
        check("用法" in latest_toast(app), f"/forget 不带参数给用法提示：{latest_toast(app)}")

        # 清到零，后面数「多条命中」才数得准。
        for entry_id in list(app._memory.entries):
            app.execute_command(f"forget {entry_id}")
            await pilot.pause()
        app.execute_command("memory")
        await pilot.pause()
        check("还是空的" in latest_toast(app), f"删完 /memory 又空了：{latest_toast(app)}")

        # 关键词命中多条时不猜，列出来让人用编号指定，不擅自删。
        app.execute_command("remember 甲受体在 a.pdb")
        await pilot.pause()
        app.execute_command("remember 乙受体在 b.pdb")
        await pilot.pause()
        before = len(app._memory.entries)
        check(before == 2, f"这时有两条记忆：{before}")
        app.execute_command("forget 受体")
        await pilot.pause()
        check(
            isinstance(app.screen, MessageScreen),
            "关键词命中多条时列出候选，而不是随便删一条",
        )
        check(len(app._memory.entries) == before, "列出候选时一条都没删")
        await pilot.press("escape")
        await pilot.pause()

        # 用编号删：编号就是 /memory 里那一行开头的那个。
        numbered = list(app._memory.entries)
        app.execute_command(f"forget {numbered[0]}")
        await pilot.pause()
        check("已删除" in latest_toast(app), f"/forget 用编号删：{latest_toast(app)}")
        check(
            numbered[0] not in app._memory.entries and len(app._memory.entries) == 1,
            "编号删除只删掉指定的那一条",
        )

        shutil.rmtree(mem_fixture, ignore_errors=True)

        # 任务状态命令：/task 看、/task <字段> <内容> 加、/task clear 清。
        # 写的是当前会话的 task_state，随状态文件写盘，所以这里只看内存与提示。
        state = app.session_manager.current.task_state
        app.execute_command("task")
        await pilot.pause()
        check(
            "还没有任务状态" in latest_toast(app),
            f"没有任务状态时 /task 给一句提示：{latest_toast(app)}",
        )

        app.execute_command("task goal 对 EGFR 做一轮虚拟筛选")
        await pilot.pause()
        check("已更新" in latest_toast(app), f"/task 设目标：{latest_toast(app)}")
        check(state.goal == "对 EGFR 做一轮虚拟筛选", "目标写进了当前会话的任务状态")

        app.execute_command("task constraint 受体必须用 4.2 的晶体结构")
        await pilot.pause()
        app.execute_command("task constraint 打分用 Vina，不要用 Vinardo")
        await pilot.pause()
        check(len(state.constraints) == 2, f"约束逐条追加：{state.constraints}")
        app.execute_command("task constraint 打分用 Vina，不要用 Vinardo")
        await pilot.pause()
        check(
            len(state.constraints) == 2 and "没有变化" in latest_toast(app),
            f"同一条约束不重复加：{state.constraints} / {latest_toast(app)}",
        )

        app.execute_command("task todo 生成配体库并粗筛")
        await pilot.pause()
        check(state.todo == ["生成配体库并粗筛"], "待办也加得进去")

        # 查看：非空时开弹窗，内容在里面。
        app.execute_command("task")
        await pilot.pause()
        check(isinstance(app.screen, MessageScreen), "/task 非空时打开任务状态弹窗")
        shown = " ".join(
            str(getattr(node, "renderable", node.render()))
            for node in app.screen.query(".message-line")
        )
        check("对 EGFR 做一轮虚拟筛选" in shown, f"弹窗里列出目标：{shown[:90]}")
        check("受体必须用 4.2 的晶体结构" in shown, "弹窗里列出约束")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "关掉任务状态弹窗后回到聊天界面")

        # 字段名写错与内容为空都不改东西，只给提示。
        snapshot = state.to_dict()
        app.execute_command("task goalx 随便写点")
        await pilot.pause()
        check("可用字段" in latest_toast(app), f"/task 字段名写错时列出可用字段：{latest_toast(app)}")
        app.execute_command("task goal")
        await pilot.pause()
        check("内容不能为空" in latest_toast(app), f"/task 内容为空时给提示：{latest_toast(app)}")
        check(state.to_dict() == snapshot, "这两种情况都没有改到任务状态")

        app.execute_command("task clear")
        await pilot.pause()
        check("已清空" in latest_toast(app), f"/task clear 清空：{latest_toast(app)}")
        check(state.is_empty(), "清空之后任务状态是空的")
        app.execute_command("task clear")
        await pilot.pause()
        check("本来就是空的" in latest_toast(app), f"再清一次给另一句提示：{latest_toast(app)}")

        unresolved = [
            f"{cls.__name__}.{key} -> action_{action}"
            for cls, key, action in declared_bindings()
            if not action_exists(cls, action)
        ]
        check(not unresolved, f"每条按键绑定都有对应的动作方法，未对应上方法的：{unresolved}")

        # 帮助界面那两列快捷键是手写的表，容易与真实绑定走散：改了绑定，帮助界面还印着旧键。
        # 两边逐个对比，任何一边多了或少了都会当场失败。
        for cls, table, label in [
            (ChatScreen, app.keybind_manager.direct_bindings, "聊天界面"),
            (HomeScreen, app.keybind_manager.home_bindings, "启动界面"),
        ]:
            real = {(entry[0] if isinstance(entry, tuple) else entry.key) for entry in cls.BINDINGS}
            # 上下键与 esc 只在命令补全弹窗开着时才归弹窗，不进帮助界面那张快捷键表；
            # esc 中断同样绑在输入框那一层（`EditorPane`），帮助界面仍要把它列出来，
            # 所以这一边减掉、那一边留着单独断言。
            real -= CONTEXTUAL_KEYS
            listed = set(table) - CONTEXTUAL_KEYS
            check(
                real == listed,
                f"帮助界面为{label}列出的按键与真实绑定一致，差异：{sorted(listed ^ real)}",
            )
            if cls is ChatScreen:
                check("escape" in table, f"帮助界面列出 esc 中断：{sorted(table)}")

        # 每种 Part 都要归入一条渲染路径。多出一种类型而没决定它怎么渲染，界面会把内容整个吞掉。
        routed = set(SIMPLE_PART_TYPES) | {
            PartType.TEXT,
            PartType.REASONING,
            PartType.TOOL,
            PartType.PATCH,
            PartType.ERROR,
            PartType.SUMMARY,
        }
        unrouted = sorted(item.value for item in set(PartType) - routed)
        check(not unrouted, f"每种 Part 都归入了渲染路径，未归入的：{unrouted}")

        blank = [
            part_type.value
            for part_type in SIMPLE_PART_TYPES
            if not SimplePartView.render_part_text(PART_SAMPLES[part_type]).strip()
        ]
        check(not blank, f"每种 Part 都渲染出内容，渲染为空的：{blank}")
        check(
            "▤" in SimplePartView.render_part_text(PART_SAMPLES[PartType.FILE]),
            "文件引用渲染成引用卡",
        )
        # 补丁那行的增删行数必须来自 Part 自己的字段，渲染层不能自己编一个常量：
        # 编出来的数字不会随补丁变化，接上真后端之后还会继续显示同一个错数。
        stat_part = Part(type=PartType.PATCH, text="a.py", patch_added=7, patch_removed=2)
        check("(+7 -2)" in SimplePartView.render_part_text(stat_part), "补丁统计读的是 Part 字段")
        check(
            "(" not in SimplePartView.render_part_text(Part(type=PartType.PATCH, text="a.py")),
            "没有增删行数的补丁不显示统计",
        )
        restored = Part.from_dict(stat_part.to_dict())
        check(
            (restored.patch_added, restored.patch_removed) == (7, 2),
            "补丁统计随会话一起存取",
        )

        # 文件补全要跳过生成目录。造一棵自己的小目录树，免得这条检查随仓库里有什么而变。
        # 这棵树建好就不再删：刚建出来的目录马上删除，会被杀毒软件的实时扫描占住，
        # 删除随机失败，检查就变成看运气了。留在 .probe 下，既在忽略名单里也在 gitignore 里。
        fixture = ROOT / ".probe" / "search_fixture"
        (fixture / "src").mkdir(parents=True, exist_ok=True)
        (fixture / "src" / "keep_me.py").write_text("x", encoding="utf-8")
        for ignored in ("build", ".venv", "dist"):
            (fixture / ignored).mkdir(exist_ok=True)
            (fixture / ignored / "generated.py").write_text("x", encoding="utf-8")
        check({".probe", "build", "dist"} <= set(IGNORED_DIRS), "生成目录在补全的忽略名单里")
        planted = [path.relative_to(fixture).as_posix() for path in fixture.rglob("generated.py")]
        check(len(planted) == 3, f"三个忽略目录里各有一个文件，才有得跳：{sorted(planted)}")
        found = search_files(fixture, "")
        check("src/keep_me.py" in found, "正常目录里的文件出现在候选里")
        skipped = sorted(item for item in found if item.endswith("generated.py"))
        check(not skipped, f"忽略目录里的文件不进候选：{skipped}")
        check(
            search_files(fixture, "keep_me") == ["src/keep_me.py"],
            "按名字能搜到正常目录里的文件",
        )

        before = len(app.session_manager.current.messages)
        check(before > 0, f"要撤销就得先有消息：{before} 条")
        await type_text(pilot, "/undo")
        await pilot.press("enter")
        await pilot.pause()
        undone = len(app.session_manager.current.messages)
        check(
            undone == before - 1,
            f"/undo 撤销最后一条消息：撤销前 {before} 条，撤销后 {undone} 条，"
            f"最近的提示是{latest_toast(app)}",
        )
        # 撤销只改内存里的消息表，要当场写盘，否则退出前磁盘那一份还是旧的。
        # 这一步必须紧接撤销之后：后面那次重做会写盘，把漏掉的写盘盖过去。
        check(
            shape_of(state_on_disk()) == shape_of(app.session_manager),
            f"撤销立刻写进状态文件：磁盘 {shape_of(state_on_disk())}，"
            f"内存 {shape_of(app.session_manager)}",
        )
        await type_text(pilot, "/redo")
        await pilot.press("enter")
        await pilot.pause()
        redone = len(app.session_manager.current.messages)
        check(
            redone == before,
            f"/redo 恢复被撤销的消息：恢复前 {undone} 条，恢复后 {redone} 条，"
            f"最近的提示是{latest_toast(app)}",
        )
        check(
            shape_of(state_on_disk()) == shape_of(app.session_manager),
            f"重做立刻写进状态文件：磁盘 {shape_of(state_on_disk())}，"
            f"内存 {shape_of(app.session_manager)}",
        )

        app.execute_command("sessions")
        await pilot.pause()
        check(isinstance(app.screen, SessionPickerScreen), "/sessions 打开会话选择器")
        check(
            "共" in str(app.screen.query_one("#picker-title").render()),
            f"标题栏写着会话总数：{app.screen.query_one('#picker-title').render()}",
        )
        titles = app.screen.query(".session-row-title")
        check(
            len(titles) == len(app.session_manager.sessions),
            f"列表一行一个会话：{len(titles)} 行 / {len(app.session_manager.sessions)} 个",
        )
        check(
            len(app.screen.query(".session-row-title.-current")) == 1,
            "当前会话那一行带 ▸ 标记",
        )
        metas = [str(item.render()) for item in app.screen.query(".session-row-meta")]
        check(
            bool(metas) and all("条消息" in text for text in metas),
            f"每行元信息写着消息数：{metas[:2]}",
        )
        box = app.screen.query_one("#session-picker").region
        check(
            abs(box.x - (app.size.width - box.right)) <= 1,
            f"会话选择器在屏幕中间：左 {box.x} 右 {app.size.width - box.right}，"
            f"盒子 {box}，屏宽 {app.size.width}",
        )

        await pilot.press("r")
        await pilot.pause()
        check(isinstance(app.screen, RenameDialog), "r 打开重命名对话框")
        await type_text(pilot, "重命名测试")
        await pilot.press("enter")
        await pilot.pause()
        titles = [session.title for session in app.session_manager.list_sessions()]
        check(any("重命名测试" in title for title in titles), "重命名写入会话标题")
        disk_titles = [session.title for session in state_on_disk().list_sessions()]
        check(
            disk_titles == titles,
            f"重命名立刻写进状态文件：磁盘 {disk_titles}，内存 {titles}",
        )

        count_before = len(app.session_manager.sessions)
        await pilot.press("d")
        await pilot.pause()
        check(isinstance(app.screen, ConfirmDialog), "d 打开删除确认框")
        await pilot.press("n")
        await pilot.pause()
        check(len(app.session_manager.sessions) == count_before, "取消删除不改变会话数量")

        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "选择会话后回到 ChatScreen")

        # 切换会话会把消息区整个重建，视图是 `MessageView.compose` 里新建的。
        # 重建之后再对一次两张登记表，防止新视图进不了表、旧视图留在表里。
        rebuilt_reasoning = pane.query(ReasoningPartView)
        rebuilt_cards = pane.query(ToolCard)
        check(
            len(pane._reasoning_views) == len(rebuilt_reasoning),
            f"重建后思考块登记表仍然对得上：画面 {len(rebuilt_reasoning)}，登记 {len(pane._reasoning_views)}",
        )
        check(
            len(pane._tool_cards) == len(rebuilt_cards),
            f"重建后工具卡登记表仍然对得上：画面 {len(rebuilt_cards)}，登记 {len(pane._tool_cards)}",
        )

        app.execute_command("model")
        await pilot.pause()
        check(isinstance(app.screen, ConnectionFormScreen), "/model 命令打开接口表单")
        box = app.screen.query_one("#connection-form")
        region = box.region
        check(
            2 * region.x + region.width == app.size.width,
            "接口表单在屏幕内水平居中",
        )
        check(
            abs(2 * region.y + region.height - app.size.height) <= 1,
            "接口表单在屏幕内垂直居中",
        )
        check(app.screen.styles.background.a < 1, "接口表单的遮罩带透明度")
        check(box.styles.background.a < 1, "接口表单弹窗本体带透明度")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "关闭接口表单后回到聊天界面")
        model_label_now = chat.query_one("#editor-model")
        check(
            str(model_label_now.render()) == app.current_model_label,
            f"输入框状态行与当前模型一致：{model_label_now.render()}",
        )

        app.execute_command("themes")
        await pilot.pause()
        theme_before = app.theme
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        check(app.theme != theme_before, "选择主题后应用新主题")

        app.show_diff()
        await pilot.pause()
        check(len(app.screen_stack) >= 3, "Diff 覆盖层可以打开")
        await pilot.press("escape")
        await pilot.pause()

        # 自带的样例 diff 只有十几行，放不满一屏，滚动键按下去画面不会动。
        # 换一段超过一屏的再看上下键，这两条断言才说明得了问题。
        long_diff = "\n".join(
            [
                "--- a/opencode_tui/app.py",
                "+++ b/opencode_tui/app.py",
                "@@ -1,3 +1,3 @@",
                *[f"+第 {index} 行新增的内容" for index in range(1, 80)],
            ]
        )
        app.show_diff(long_diff)
        await pilot.pause()
        overlay = app.screen.query_one("#diff-overlay")
        check(overlay.max_scroll_y > 0, f"这段 Diff 内容超过一屏：max={overlay.max_scroll_y}")
        await pilot.press("down")
        await pilot.pause()
        check(overlay.scroll_y == 1, f"Diff 覆盖层 down 往下滚一行：scroll_y={overlay.scroll_y}")
        await pilot.press("up")
        await pilot.pause()
        check(overlay.scroll_y == 0, f"Diff 覆盖层 up 滚回顶部：scroll_y={overlay.scroll_y}")
        await pilot.press("escape")
        await pilot.pause()

        app.show_toast("测试通知", "success")
        await pilot.pause()
        top = app.screen
        check(bool(top.query(".toast")), "toast 挂载成功")
        await pilot.pause(3.3)
        check(not app.screen.query(".toast"), "toast 在 3 秒后移除")

        app.save_state()
        check(STATE_PATH.exists(), "状态写入 TOML 文件")

        export_target = ROOT / f"opencode-export-{app.session_manager.current_session_id}.md"
        export_target.unlink(missing_ok=True)
        app.execute_command("export")
        await pilot.pause()
        check(export_target.exists(), "export 生成 Markdown 文件")
        exported = export_target.read_text(encoding="utf-8")
        check("## 你" in exported, "导出内容含 user 消息标题")
        check(REPLY_TEXT in exported, "导出内容含 assistant 正文")
        check("<details>" in exported, "导出内容把思考块折叠起来")
        check("工具 `bash`" in exported, "导出内容含工具调用")
        check(TOOL_MARK in exported, "导出内容含工具输出")
        export_target.unlink()

        previous_visual_env = os.environ.get("VISUAL")
        previous_editor_env = os.environ.get("EDITOR")
        os.environ["EDITOR"] = "my-editor"
        os.environ.pop("VISUAL", None)
        check(resolve_editor() == "my-editor", "只有 EDITOR 时用它")
        os.environ["VISUAL"] = "my-visual"
        check(resolve_editor() == "my-visual", "VISUAL 比 EDITOR 优先")
        os.environ["VISUAL"] = "   "
        check(resolve_editor() == "my-editor", "VISUAL 只有空白时退回 EDITOR")
        os.environ.pop("VISUAL", None)
        if previous_visual_env is not None:
            os.environ["VISUAL"] = previous_visual_env
        if previous_editor_env is None:
            del os.environ["EDITOR"]
        else:
            os.environ["EDITOR"] = previous_editor_env

        # 编辑器命令行：编辑器自带的引号要去掉，路径里的空格要保住，两边各来一层。
        split_cases = [
            ("notepad", ["notepad"]),
            ("code --wait", ["code", "--wait"]),
            ("nvim -f", ["nvim", "-f"]),
            (
                '"C:\\Program Files\\Microsoft VS Code\\Code.exe" --wait',
                ["C:\\Program Files\\Microsoft VS Code\\Code.exe", "--wait"],
            ),
            ("'/usr/local/bin/my editor' -w", ["/usr/local/bin/my editor", "-w"]),
            ("  nano  ", ["nano"]),
        ]
        for spec, expected in split_cases:
            actual = split_editor_command(spec)
            check(actual == expected, f"分开 EDITOR={spec!r}：得到 {actual}，期望 {expected}")

        spaced_buffer = Path("C:/Users/me/.opencode_tui/editor-buffer.md")
        command_cases = [
            ("notepad", f'"notepad" "{spaced_buffer}"'),
            ("code --wait", f'"code" "--wait" "{spaced_buffer}"'),
            (
                '"C:\\Program Files\\Microsoft VS Code\\Code.exe" --wait',
                f'"C:\\Program Files\\Microsoft VS Code\\Code.exe" "--wait" "{spaced_buffer}"',
            ),
        ]
        for spec, expected in command_cases:
            actual = build_command(spec, spaced_buffer)
            check(actual == expected, f"拼 EDITOR={spec!r}：得到 {actual}，期望 {expected}")
            check(
                actual.count('""') == 0,
                f"拼出来的命令行没有贴在一起的两层引号：{actual}",
            )

        buffer_probe = ROOT / ".probe" / "editor_buffer_test.md"
        write_buffer(buffer_probe, "第一行\n第二行\n")
        check(read_buffer(buffer_probe) == "第一行\n第二行\n", "编辑器暂存文件往返一致")
        buffer_probe.write_bytes("第一行\r\n第二行\r".encode("utf-8"))
        check(
            read_buffer(buffer_probe) == "第一行\n第二行\n",
            f"读回暂存文件时换行统一成 \\n：{read_buffer(buffer_probe)!r}",
        )
        buffer_probe.unlink()

        check(
            editor_buffer_path() == Path.home() / ".opencode_tui" / "editor-buffer.md",
            f"暂存文件路径：{editor_buffer_path()}",
        )
        os.environ.pop("VISUAL", None)
        os.environ.pop("EDITOR", None)
        fallback = resolve_editor()
        expected_fallback = "notepad" if sys.platform == "win32" else None
        if expected_fallback is None:
            check(fallback in ("nano", "vi") or fallback is None, f"两个环境变量都没设时按平台找一个：{fallback}")
        else:
            check(fallback == expected_fallback, f"两个环境变量都没设时按平台找一个：{fallback}")

        # 用固定长度的路径，不用 ROOT：这条断言验的是「没超上界就原样返回」，
        # 拿 ROOT 会让它顺带断言 checkout 路径的长度，换个目录深一点就假失败。
        plain_path = Path("proj")
        check(
            shorten_path(plain_path) == str(plain_path),
            f"路径没超过上界时原样返回：{shorten_path(plain_path)}",
        )
        long_path = Path("D:/1Store/VSAgent/opencode_tui/widgets/messages_pane.py")
        shortened = shorten_path(long_path)
        check(len(shortened) == 28, f"路径过长时截到 28 个字符：{len(shortened)}")
        check(shortened == "…" + str(long_path)[-27:], f"截出来的样子：{shortened}")

        nowhere = ROOT / "no-such-directory-for-git"
        check(not nowhere.exists(), "拿一个确实不存在的目录去取 git 分支")
        check(git_branch(nowhere) == "-", f"目录不存在时返回 -：{git_branch(nowhere)}")

        check(format_tokens(0) == "0", f"个位数 token 原样显示：{format_tokens(0)}")
        check(format_tokens(999) == "999", f"不到一千时原样显示：{format_tokens(999)}")
        check(format_tokens(1000) == "1.0K", f"一千以上换成 K：{format_tokens(1000)}")
        check(format_tokens(2500) == "2.5K", f"2500 显示成 2.5K：{format_tokens(2500)}")
        check(format_tokens(1_000_000) == "1.0M", f"一百万以上换成 M：{format_tokens(1_000_000)}")
        check(format_tokens(2_500_000) == "2.5M", f"250 万显示成 2.5M：{format_tokens(2_500_000)}")

        registry = CommandRegistry()
        check(registry.get("/help") is not None and registry.get("/help").name == "help", "get 去掉前导斜杠")
        check(registry.get("quit") is not None and registry.get("quit").name == "exit", "get 认别名")
        check(registry.get("no-such-command") is None, "get 查不到时返回 None")
        check(registry.get("clear") is not None and registry.get("clear").name == "new", "get 认另一个别名")
        every_command = registry.all()
        check(
            all(command.display().startswith(f"/{command.name}") for command in every_command),
            "每条命令的展示文本以 /名字 开头（补全弹窗靠它回名字）",
        )
        check(
            len(registry.fuzzy_match("")) == min(12, len(every_command)),
            f"空查询按上限列出全部命令：{len(registry.fuzzy_match(''))}",
        )
        matched = [command.name for command in registry.fuzzy_match("thinking")]
        check(
            0 < len(matched) < len(every_command),
            f"给一个词之后候选被筛过：{len(every_command)} 条里剩 {len(matched)} 条 {matched}",
        )

        bus = EventBus()
        seen: list[dict[str, object]] = []
        bus.subscribe("part.add", lambda data: seen.append(dict(data)))
        wildcard: list[dict[str, object]] = []
        bus.subscribe("*", lambda data: wildcard.append(dict(data)))
        called = bus.publish("part.add", {"part_id": "prt_9999"})
        check(called == 2, f"publish 返回被调用的处理函数个数（含通配订阅者）：{called}")
        check(seen == [{"part_id": "prt_9999"}], f"订阅者收到原始载荷：{seen}")
        check(
            wildcard == [{"type": "part.add", "part_id": "prt_9999"}],
            f"通配订阅者额外收到事件名：{wildcard}",
        )
        check(bus.publish("nobody.listens") == 1, "没有专属订阅者时只有通配那一个被调用")
        bus.clear()
        check(bus.publish("part.add", {"part_id": "prt_9999"}) == 0, "clear 之后没有人再收到事件")

        located = ROOT / "opencode_tui" / "app.py"
        text, refs = resolve_file_refs(f"看一下 @{located} 与 @ 还有 @no-such-file.txt", ROOT)
        check(
            [part.file_path for part in refs] == [str(located)],
            f"只有真实存在的引用才成为 Part：{[part.file_path for part in refs]}",
        )
        check(refs[0].type is PartType.FILE and refs[0].text, "引用 Part 带上文件内容")
        check(text.startswith("看一下 ") and text.endswith("@no-such-file.txt"), f"原文照常返回：{text}")

        exported_session = SessionManager(state_path=STATE_PATH).new_session()
        sample_message = Message(role="assistant")
        for sample in PART_SAMPLES.values():
            sample_message.add_part(sample)
        exported_session.add_message(sample_message)
        check(export_filename(exported_session) == f"opencode-export-{exported_session.id}.md", "导出文件名用会话编号")
        exported_text = render_session_markdown(exported_session)
        check(exported_text.startswith(f"# {exported_session.title}"), f"导出以会话标题开头：{exported_text.splitlines()[0]}")
        check(exported_text.endswith("\n") and not exported_text.endswith("\n\n"), "导出正文以单个换行收尾")
        # 这些 Part 类型由样本喂给渲染分支，导出里的这几行没走过真实一轮。
        exported_markers = {
            PartType.FILE: "引用文件 `opencode_tui/app.py`",
            PartType.AGENT: "Agent `Build`",
            PartType.STEP_START: "── 步骤开始 start",
            PartType.STEP_FINISH: "── 步骤结束 finish",
            PartType.PATCH: "补丁 `opencode_tui/app.py`",
            PartType.SUBTASK: "子任务 `explore` 找一下入口",
            PartType.RETRY: "重试 第 2 次",
            PartType.COMPACTION: "上下文已压缩 省略了 3 条",
            PartType.ERROR: "请求没有成功：服务暂时不可用",
            PartType.SUMMARY: "## 收尾总结",
        }
        for part_type, marker in exported_markers.items():
            check(marker in exported_text, f"{part_type.value} 这一类 Part 也导出了内容：{marker!r}")

        await pilot.pause()
        app.execute_command("/no-such-command")
        await pilot.pause()
        check(
            "未知命令" in latest_toast(app),
            f"执行一条注册表里没有的命令给出提示：{latest_toast(app)}",
        )

        app.execute_command("help")
        await pilot.pause()
        app.execute_command("editor")
        await pilot.pause()
        check(bool(app.screen.query(".toast")), "非聊天界面执行 editor 给出提示")
        app.execute_command("sidebar")
        await pilot.pause()
        check(
            "请先进入聊天界面" in latest_toast(app),
            f"非聊天界面执行 sidebar 给出提示：{latest_toast(app)}",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "escape 回到聊天界面")

        pane.focus()
        await pilot.pause()
        check(pane.max_scroll_y > 0, "消息列表内容超过一屏")
        await pilot.press("up")
        await pilot.pause()
        check(not pane.following, "向上滚动后停止自动跟随")
        await pilot.press("end")
        await pilot.pause()
        check(pane.scroll_y >= pane.max_scroll_y - 1, "end 跳到消息列表底部")
        check(pane.following, "跳到底部后恢复自动跟随")

        # 窗口维护自己也会改滚动量：裁掉上端之后布局把 scroll_y 夹回一行，
        # 这一下看起来像「用户往上翻」，但用户根本没动。夹回之后跟随必须还开着，
        # 否则用户什么都没做，新消息就再也不跟到底了（实测偶发，跑十次挂三次）。
        pane.scroll_to(y=pane.max_scroll_y - 1, animate=False, immediate=True)
        await pilot.pause()
        check(pane.following, "维护把滚动量夹回一行时跟随不被关掉")

        # 滚轮与拖动滚动条是另外两条入口，都不走 action_scroll_*。
        # 滚轮往上滚一格落点仍在底部（容差内），必须由滚轮自己的处理关掉跟随。
        pane.post_message(events.MouseScrollUp(pane, 0, 0, 0, 0, 0, False, False, False))
        await pilot.pause()
        check(not pane.following, "滚轮往上滚一格就停止跟随")

        # 拖动滚动条：没拖到底就不跟，拖到底就恢复。
        await pilot.press("end")
        await pilot.pause()
        pane.post_message(ScrollTo(x=None, y=pane.max_scroll_y - 2, animate=False))
        await pilot.pause()
        check(not pane.following, "滚动条停在离底部两行处不跟随")
        pane.post_message(ScrollTo(x=None, y=pane.max_scroll_y, animate=False))
        await pilot.pause()
        check(pane.following, "滚动条拖到底恢复跟随")
        await pilot.press("end")
        await pilot.pause()

        # 离开底部之后有新内容进来：底部亮一条提示，点一下回到底部并恢复跟随。
        # 不亮提示的话，长回合里用户在上面翻历史，界面上一点动静都没有。
        await pilot.press("home")
        await pilot.pause()
        check(not pane.following, "回到顶部之后跟随是关着的")
        check(not pane._new_hint.has_class("-visible"), "没有新内容时不亮提示")
        visible_part = pane._messages[pane._mounted_start].parts[-1]
        pane.update_part(visible_part)
        await pilot.pause()
        check(pane._new_hint.has_class("-visible"), "离开底部时新内容进来会亮提示")
        await pilot.click("#new-messages")
        await pilot.pause()
        check(pane.following, "点提示条回到底部并恢复跟随")
        check(not pane._new_hint.has_class("-visible"), "回到底部之后提示收起来")
        await pilot.press("end")
        await pilot.pause()

        # 另外四个滚动键沿用 ScrollableContainer 自带的绑定（本类只覆盖了动作方法），
        # 按下去会不会真的动，只能实测。
        await pilot.press("home")
        await pilot.pause()
        check(pane.scroll_y == 0, f"home 跳到消息列表顶部：scroll_y={pane.scroll_y}")
        check(not pane.following, "跳到顶部后不再自动跟随")
        await pilot.press("down")
        await pilot.pause()
        after_one = pane.scroll_y
        check(after_one == 1, f"down 往下滚一行：scroll_y={after_one}")
        await pilot.press("pagedown")
        await pilot.pause()
        after_page = pane.scroll_y
        check(after_page > after_one, f"pagedown 往下翻一屏：{after_one} -> {after_page}")
        await pilot.press("pageup")
        await pilot.pause()
        check(
            pane.scroll_y < after_page,
            f"pageup 往上翻一屏：{after_page} -> {pane.scroll_y}",
        )
        # 往下翻到最底之后要自己恢复自动跟随，这是 action_page_down / action_scroll_down 里的分支。
        for _ in range(20):
            await pilot.press("pagedown")
            await pilot.pause()
            if pane._at_bottom():
                break
        check(
            pane._at_bottom(),
            f"一路往下翻到底：scroll_y={pane.scroll_y}，max={pane.max_scroll_y}",
        )
        check(pane.following, "翻到最底之后恢复自动跟随")
        # 一行一行往下滚也是同一条分支：回到顶部先把跟随关掉，再滚到底，看它自己恢复没有。
        await pilot.press("home")
        await pilot.pause()
        check(not pane.following, "回到顶部之后跟随是关着的")
        for _ in range(200):
            await pilot.press("down")
            await pilot.pause()
            if pane._at_bottom():
                break
        check(
            pane._at_bottom(),
            f"一路往下滚到底：scroll_y={pane.scroll_y}，max={pane.max_scroll_y}",
        )
        check(pane.following, "一行一行滚到最底之后恢复自动跟随")
        await pilot.press("end")
        await pilot.pause()

        # shift+enter 换行而不提交：换行之后输入框里应当多一行，消息条数不变。
        # 上一步焦点在消息区，先交回输入框，字符才写得进去。
        editor.focus_input()
        await pilot.pause()
        editor.text = ""
        messages_before_newline = len(app.session_manager.current.messages)
        await type_text(pilot, "第一行")
        await pilot.press("shift+enter")
        await pilot.pause()
        await type_text(pilot, "第二行")
        await pilot.pause()
        check(
            editor.text == "第一行\n第二行",
            f"shift+enter 在输入框里换行：{editor.text!r}",
        )
        check(
            len(app.session_manager.current.messages) == messages_before_newline,
            f"换行不算提交：消息条数 {messages_before_newline} -> "
            f"{len(app.session_manager.current.messages)}",
        )
        check(editor.mode == "chat", f"换行之后仍是 chat 模式：{editor.mode}")
        editor.text = ""
        await pilot.pause()

        # 另外几条命令在别处都没走过。model/sessions 打开各自的界面，export 写出导出文件，
        # compact 与 init 会真改东西，share 是还没接入的命令。
        # editor 走外部编辑器，测试环境不支持挂起，不在这一轮里按。
        for name, screen_type, label in [
            ("model", ConnectionFormScreen, "接口表单"),
            ("sessions", SessionPickerScreen, "会话选择器"),
            ("help", HelpScreen, "帮助界面"),
        ]:
            await type_text(pilot, f"/{name}")
            await pilot.press("enter")
            await pilot.pause()
            check(isinstance(app.screen, screen_type), f"/{name} 打开{label}")
            await pilot.press("escape")
            await pilot.pause()
            check(isinstance(app.screen, ChatScreen), f"关闭{label}后回到聊天界面")

        export_target = ROOT / f"opencode-export-{app.session_manager.current_session_id}.md"
        export_target.unlink(missing_ok=True)
        await type_text(pilot, "/export")
        await pilot.press("enter")
        await pilot.pause()
        check(export_target.exists(), "/export 导出 Markdown 文件")
        export_target.unlink()

        # /compact：消息够多时压掉前面一段，位置记在会话上。
        session = app.session_manager.current
        while len(session.messages) <= KEEP_MESSAGES:
            filler = session.add_message(Message(role="user", session_id=session.id))
            filler.add_part(Part(type=PartType.TEXT, text=f"凑第 {len(session.messages)} 条"))
        await type_text(pilot, "/compact")
        await pilot.press("enter")
        await pilot.pause()
        check(session.compact_upto > 0, f"/compact 把压缩位置往前推：{session.compact_upto}")
        kept_now = len(session.messages) - session.compact_upto
        check(kept_now == KEEP_MESSAGES, f"压完之后留下的条数：{kept_now}")

        # /init：往项目根写一份 AGENTS.md，已有内容一个字都不动。
        # 项目里本来就有一份，先备份再恢复，测试跑完那一个字节也不能变。
        agents_target = ROOT / "AGENTS.md"
        agents_backup = agents_target.read_bytes() if agents_target.is_file() else None
        try:
            if agents_target.is_file():
                agents_target.unlink()
            await type_text(pilot, "/init")
            await pilot.press("enter")
            await pilot.pause()
            check(agents_target.is_file(), "/init 在项目根写出 AGENTS.md")
            written = agents_target.read_text(encoding="utf-8")
            check(BEGIN_MARK in written and END_MARK in written, "写出来的文件里带着那两个标记")
            check("Python" in written, "写出来的文件里认出了项目用的语言")
            await type_text(pilot, "/init")
            await pilot.press("enter")
            await pilot.pause()
            again = agents_target.read_text(encoding="utf-8")
            check(again.count(BEGIN_MARK) == 1, "再按一次不会把这一段写两遍")
        finally:
            if agents_backup is None:
                agents_target.unlink(missing_ok=True)
            else:
                agents_target.write_bytes(agents_backup)

        for name in ("share",):
            await type_text(pilot, f"/{name}")
            await pilot.press("enter")
            await pilot.pause()
            check(
                "尚未接入" in latest_toast(app),
                f"/{name} 是未接入的命令，给出提示：{latest_toast(app)}",
            )
            check(isinstance(app.screen, ChatScreen), f"/{name} 之后仍停在聊天界面")

        # unshare 也只能走斜杠命令。它与上面几条走同一个分支，
        # 但没有任何用例走过它（`.probe/check_command_coverage.py` 点名列出来的）。
        app.execute_command("unshare")
        await pilot.pause()
        check("尚未接入" in latest_toast(app), f"斜杠命令 unshare 给出提示：{latest_toast(app)}")

        # 推理强度：轮换一圈回到原值，输入框状态行跟着换，并写进状态文件。
        check(EFFORTS == ("low", "medium", "high"), f"推理强度按 low → medium → high 排列：{EFFORTS}")
        start_effort = app.session_manager.current.effort
        rotated: list[str] = []
        for _ in range(len(EFFORTS)):
            app.execute_command("effort")
            await pilot.pause()
            rotated.append(app.session_manager.current.effort)
        expected_rotation = [
            EFFORTS[(EFFORTS.index(start_effort) + step) % len(EFFORTS)]
            for step in range(1, len(EFFORTS) + 1)
        ]
        check(
            rotated == expected_rotation,
            f"轮换一圈回到原值：{start_effort} 之后依次 {rotated}，期望 {expected_rotation}",
        )
        check(
            str(editor.query_one("#editor-effort").render()) == app.session_manager.current.effort,
            f"输入框状态行跟着换：{editor.query_one('#editor-effort').render()!r}，"
            f"会话里 {app.session_manager.current.effort}",
        )
        disk_effort = state_on_disk().sessions[app.session_manager.current_session_id].effort
        check(
            disk_effort == app.session_manager.current.effort,
            f"推理强度写进状态文件：磁盘 {disk_effort}，内存 {app.session_manager.current.effort}",
        )
        check("推理强度" in latest_toast(app), f"切换推理强度给出提示：{latest_toast(app)}")

        # 删掉正在看的那个会话。会话管理器会切到另一个会话，而列表当时还盖在上面，
        # 聊天界面得跟着重建，否则画出来的仍是被删掉的那份对话。
        # 先开一个空会话当作待删对象：删掉之后要切回去的那个会话本来有消息，
        # 重建漏掉的话消息区会空着，与切换过去的会话对不上。
        app.execute_command("new")
        await pilot.pause()
        doomed = app.session_manager.current_session_id
        check(len(app.session_manager.current.messages) == 0, "待删的那个新会话没有消息")
        app.execute_command("sessions")
        await pilot.pause()
        check(isinstance(app.screen, SessionPickerScreen), "再次打开会话选择器")
        await pilot.press("d")
        await pilot.pause()
        check(isinstance(app.screen, ConfirmDialog), "d 打开删除确认框")
        await pilot.press("y")
        await pilot.pause()
        check(
            doomed not in {session.id for session in app.session_manager.list_sessions()},
            f"确认删除移除了当前会话 {doomed}",
        )
        check(
            app.session_manager.current_session_id != doomed,
            "删掉当前会话之后切到别的会话",
        )
        disk_after_delete = state_on_disk()
        check(
            doomed not in {session.id for session in disk_after_delete.list_sessions()},
            f"删除立刻写进状态文件：磁盘上还剩 {[s.id for s in disk_after_delete.list_sessions()]}",
        )
        check(
            disk_after_delete.current_session_id == app.session_manager.current_session_id,
            f"切换后的当前会话也写进状态文件：磁盘 {disk_after_delete.current_session_id}，"
            f"内存 {app.session_manager.current_session_id}",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "退出会话列表后回到聊天界面")
        await pilot.pause()
        drawn = list(pane._message_views)
        expected_order = [message.id for message in app.session_manager.current.messages]
        check(
            bool(expected_order),
            f"切换过去的会话本来有消息，这条检查才说明得了问题：{len(expected_order)} 条",
        )
        check(
            drawn == expected_order[-len(drawn) :],
            f"画出来的是当前会话最后那一段：画面 {len(drawn)} 条，当前会话 {len(expected_order)} 条，"
            f"删掉的是 {doomed}",
        )

        # 聊天界面用 /new 新建会话；启动界面那一次在文件末尾单独开的会话里验。
        # 这一步放在最后：新建会话会清空消息区，前面几条按历史长度断言的检查会受影响。
        session_before_new = app.session_manager.current_session_id
        await type_text(pilot, "/new")
        await pilot.press("enter")
        await pilot.pause()
        check(
            app.session_manager.current_session_id != session_before_new,
            "/new 在聊天界面新建会话",
        )
        check(isinstance(app.screen, ChatScreen), "新建会话后仍停在聊天界面")
        check(len(app.session_manager.current.messages) == 0, "新会话没有历史消息")
        check(
            state_on_disk().current_session_id == app.session_manager.current_session_id,
            f"新建会话立刻写进状态文件：磁盘 {state_on_disk().current_session_id}，"
            f"内存 {app.session_manager.current_session_id}",
        )

        # 空会话上三条命令的提示分支。空手撤销与重做要先被拦住，导出要有东西可导。
        await type_text(pilot, "/undo")
        await pilot.press("enter")
        await pilot.pause()
        check(
            "没有可撤销的消息" in latest_toast(app),
            f"空会话上撤销被拦住：{latest_toast(app)}",
        )
        await type_text(pilot, "/redo")
        await pilot.press("enter")
        await pilot.pause()
        check(
            "没有可重做的消息" in latest_toast(app),
            f"空会话上重做被拦住：{latest_toast(app)}",
        )
        app.execute_command("export")
        await pilot.pause()
        check(
            "还没有消息" in latest_toast(app),
            f"空会话上导出被拦住：{latest_toast(app)}",
        )

        await pilot.press("ctrl+c")
        await pilot.pause()
        check(app.return_code == 0, "ctrl+c 退出应用")

    # 启动界面上那几条命令原先各有各的按键（ctrl+n/l/m/t/h/q），现在统一走 /命令。
    # 单独开一个会话逐个走一遍：焦点在输入框里，按键都被输入框先接住。
    STATE_PATH.unlink(missing_ok=True)
    second = build_app(STATE_PATH)
    async with second.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        check(isinstance(second.screen, HomeScreen), "新会话从启动界面开始")

        # 启动界面上也打 / 列出全部命令，并且能上下选、回车执行。
        await type_text(pilot, "/")
        await pilot.pause()
        home_popup = second.screen.command_popup
        check(home_popup.is_open, "启动界面上输入 / 打开命令补全")
        check(
            len(home_popup.commands) == len(second.command_registry.all()),
            f"启动界面上列出全部命令：{len(home_popup.commands)} 条",
        )
        await type_text(pilot, "the")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        check(second.screen.input.text == "/themes", f"回车采纳命令名：{second.screen.input.text!r}")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(second.screen, ThemeSelectorScreen), "启动界面上 /themes 打开主题选择器")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(second.screen, HomeScreen), "关闭主题选择器后回到启动界面")

        for name, screen_type, opened, closed in [
            ("help", HelpScreen, "/help 打开帮助界面", "帮助界面"),
            ("sessions", SessionPickerScreen, "/sessions 打开会话选择器", "会话选择器"),
            ("model", ConnectionFormScreen, "/model 打开接口表单", "接口表单"),
        ]:
            await type_text(pilot, f"/{name}")
            await pilot.press("enter")
            await pilot.pause()
            check(isinstance(second.screen, screen_type), opened)
            if name == "help":
                check(
                    second.screen.query_one("#help-home").region.y
                    < second.screen.query_one("#help-keys").region.y,
                    "从启动界面打开帮助时启动界面那一段排在前",
                )
            await pilot.press("escape")
            await pilot.pause()
            check(isinstance(second.screen, HomeScreen), f"关闭{closed}后回到启动界面")

        # 启动界面的输入框：shift+enter 换行，回车提交；斜杠开头走命令。
        home_input = second.screen.input
        await pilot.press(*list("第一行"))
        await pilot.pause()
        await pilot.press("shift+enter")
        await pilot.pause()
        await pilot.press(*list("第二行"))
        await pilot.pause()
        check(
            home_input.text == "第一行\n第二行",
            f"启动界面 shift+enter 换行：{home_input.text!r}",
        )
        check(isinstance(second.screen, HomeScreen), "换行不会离开启动界面")

        home_input.text = ""
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(second.screen, HomeScreen), "空输入回车不会跳到聊天界面")

        home_input.text = "/help"
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(second.screen, HelpScreen), "启动界面上输入 /help 打开帮助界面")
        check(home_input.text == "", "提交之后输入框清空")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(second.screen, HomeScreen), "关闭帮助界面后回到启动界面")

        # 启动界面的命令面板：选中并执行这一步走的是面板关掉之后才跑的回调，
        # 前面只验了打开与 esc 关闭。候选为空那一路也要按得下去：高亮移动与回车都靠列表长度取模。
        await pilot.press("ctrl+p")
        await pilot.pause()
        start_palette = second.screen
        check(isinstance(start_palette, CommandPalette), "启动界面上 ctrl+p 打开命令面板")
        await type_text(pilot, "zzzz")
        await pilot.pause()
        check(start_palette._results == [], f"没有匹配时候选为空：{start_palette._results}")
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        check(isinstance(second.screen, CommandPalette), "候选为空时按上下键不会把面板弄坏")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(second.screen, HomeScreen), "候选为空时回车只把面板关掉")
        check("未知命令" not in latest_toast(second), f"没有命令被执行：{latest_toast(second)}")

        await pilot.press("ctrl+p")
        await pilot.pause()
        start_palette = second.screen
        await type_text(pilot, "help")
        await pilot.pause()
        start_names = [command.name for command in start_palette._results]
        check(bool(start_names) and start_names[0] == "help", f"输入 help 之后它排在第一条：{start_names}")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(second.screen, HelpScreen), "在启动界面的命令面板里选中 help 真的打开了帮助界面")
        await pilot.press("escape")
        await pilot.pause()
        check(isinstance(second.screen, HomeScreen), "关掉帮助界面回到启动界面")

        session_before = second.session_manager.current_session_id
        await type_text(pilot, "/new")
        await pilot.press("enter")
        await pilot.pause()
        check(isinstance(second.screen, ChatScreen), "启动界面上 /new 进入聊天界面")
        check(
            second.session_manager.current_session_id != session_before,
            "/new 在启动界面新建了会话",
        )

    # 启动界面的 ctrl+c 退出单独跑：按下去应用就结束了，同一会话里后面几步没法再走。
    STATE_PATH.unlink(missing_ok=True)
    fourth = build_app(STATE_PATH)
    async with fourth.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        check(isinstance(fourth.screen, HomeScreen), "又一次从启动界面开始")
        await pilot.press("ctrl+c")
        await pilot.pause()
        check(fourth.return_code == 0, "启动界面 ctrl+c 退出应用")
        STATE_PATH.unlink(missing_ok=True)

    # 界面开始收拾自己的时候要立刻断掉后端事件。Textual 卸载一棵子树时先把消息区摘掉、
    # 再把卸载事件派发给界面本身，中间这一小段里生成任务那个协程可能正好从 await 里醒过来
    # 发一次事件，处理函数按类型去找消息区就抛 NoMatches，被记成 worker 失败（实测生成
    # 中途退出，四十轮里两次）。消息区自己卸载时通知界面断订阅，这件事就断在它离开的那一刻。
    STATE_PATH.unlink(missing_ok=True)
    script_reply(server)
    fifth = build_app(STATE_PATH)
    async with fifth.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press(*list("退出时的事件"))
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(fifth.screen, ChatScreen):
                break
            await pilot.pause(0.1)
        await pilot.pause(0.3)
        bus = fifth.event_bus
        payload = {"message_id": "msg_不存在", "part_id": "prt_不存在"}
        # 事件日志订的是通配事件，每一发都会打到它一次。这里量的是消息区那一份订阅，
        # 先把日志停掉，读到的数字才只反映消息区。
        fifth.trace.stop()
        before = bus.publish("part.update", payload)
        check(before == 1, f"消息区还在的时候，界面接得住后端事件：{before} 个处理函数")
        await fifth.screen.query_one(MessagesPane).remove()
        after = bus.publish("part.update", payload)
        check(after == 0, f"消息区走了之后，事件不再打到它上面：{after} 个处理函数")
        STATE_PATH.unlink(missing_ok=True)

    # 上次选的主题与 Agent 记在状态文件里，启动时要读回来。
    restore_path = ROOT / ".probe" / "state_restore.toml"
    restore_path.unlink(missing_ok=True)
    restored = build_app(restore_path)
    last_theme = sorted(restored.available_themes)[-1]
    last_agent = AGENTS[-1]
    prepared = SessionManager(state_path=restore_path)
    prepared.new_session()
    prepared.theme = last_theme
    prepared.last_agent = last_agent
    prepared.save_to_toml()
    async with restored.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        check(restored.theme == last_theme, f"启动时把上次选的主题读回来：{restored.theme}")
        check(
            restored.current_model_label == restored.agent.config.model,
            f"状态行上的模型取自连接配置：{restored.current_model_label}",
        )
        info_line = str(restored.screen.query_one("#home-info").content)
        check(
            restored.agent.config.model in info_line and last_agent in info_line,
            f"启动界面的状态行显示模型与读回来的 Agent：{info_line}",
        )
        check(
            restored.session_manager.current_session_id in restored.session_manager.sessions,
            f"接着用状态文件里记着的那个会话：{restored.session_manager.current_session_id}",
        )
        check(
            len(restored.session_manager.sessions) == 1,
            f"状态文件里已经有会话时不再新建：{len(restored.session_manager.sessions)} 个会话",
        )
        restore_path.unlink(missing_ok=True)

    # 中断生成要趁这一轮还在跑的时候按下去。等它自己跑完再按 esc，界面早就回到可用状态，
    # 那条断言就变成永远通过。所以先跑完一轮作为对照，再在下一轮的工具执行期间打断。
    # 单独开一个会话：被打断的那一轮会留在消息区，影响前面几条按历史长度断言的检查。
    # 对照那一轮要跑完两步（工具执行完再问一次），打断那一轮要在工具执行期间按下去，
    # 各排一条脚本：打断的那一条只返回工具调用，其后没有第二步。
    STATE_PATH.unlink(missing_ok=True)
    script_reply(server)
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c2",
                            name="bash",
                            arguments=json.dumps({"command": "sleep 30"}),
                        )
                    ]
                ),
                usage_chunk(5, 5),
            ]
        )
    )
    third = build_app(STATE_PATH)
    async with third.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press(*list("第一轮"))
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(third.screen, ChatScreen):
                break
            await pilot.pause(0.1)
        chat = third.screen
        pane = chat.query_one(MessagesPane)

        def last_parts() -> list[Part]:
            """最后一轮的 Part 清单。"""
            return third.session_manager.current.messages[-1].parts

        for _ in range(200):
            if not chat._busy and any(part.type is PartType.STEP_FINISH for part in last_parts()):
                break
            await pilot.pause(0.1)
        check(
            any(part.type is PartType.STEP_FINISH for part in last_parts()),
            f"作为对照的那一轮跑完了：{[part.type.value for part in last_parts()]}",
        )

        await pilot.press(*list("这一轮要打断"))
        await pilot.press("enter")
        unfinished: list[Part] = []
        for _ in range(400):
            unfinished = [
                part
                for part in last_parts()
                if part.type is PartType.TOOL
                and part.tool_status in (ToolStatus.PENDING, ToolStatus.RUNNING)
            ]
            if unfinished:
                break
            await pilot.pause(0.002)
        check(
            bool(unfinished),
            f"打断的时机取在工具执行期间，这条检查才说明得了问题："
            f"{[(part.tool_status or ToolStatus.PENDING).value for part in last_parts() if part.type is PartType.TOOL]}",
        )
        check(chat._busy, "按 esc 之前这一轮还在跑")
        kinds_before = [part.type.value for part in last_parts()]

        await pilot.press("escape")
        await pilot.pause()
        check(not chat._busy, "中断之后界面回到可用状态")
        check("已中断当前生成" in latest_toast(third), f"中断给出提示：{latest_toast(third)}")
        settled = [part for part in last_parts() if part.type is PartType.TOOL]
        check(
            all(part.tool_status is ToolStatus.FAILED for part in settled),
            f"中断把还在执行的工具标成失败：{[part.tool_status.value for part in settled]}",
        )
        check(
            all(part.tool_output == "interrupted" for part in settled),
            f"失败的工具写明是被中断的：{[part.tool_output for part in settled]}",
        )
        cards = [card for card in pane.query(ToolCard) if card.part_id in {part.id for part in settled}]
        check(bool(cards), "被打断的工具卡片在画面上")
        check(
            all("-failed" in card.classes for card in cards),
            f"卡片刷成失败态：{[sorted(card.classes) for card in cards]}",
        )

        await pilot.pause(1.2)
        check(
            [part.type.value for part in last_parts()] == kinds_before,
            f"中断之后没有内容再冒出来：中断时 {kinds_before}，"
            f"等一秒后 {[part.type.value for part in last_parts()]}",
        )
        check(
            shape_of(state_on_disk()) == shape_of(third.session_manager),
            f"中断立刻写进状态文件：磁盘 {shape_of(state_on_disk())}，"
            f"内存 {shape_of(third.session_manager)}",
        )

        # 聊天界面的 /exit 退出：与 ctrl+c 走的是同一条命令，都要真的结束应用。
        await type_text(pilot, "/exit")
        await pilot.press("enter")
        await pilot.pause()
        check(third.return_code == 0, "聊天界面 /exit 退出应用")

    # 一段几百条消息的会话不能在打开时把几千个组件一次建出来：建完还要等布局算完，
    # 这几秒里界面是停住的。打开时只建最后一批，更早的等滚到顶端再补，补的时候
    # 把滚动量加上新内容的行数，画面才不会跳。补进来的消息多了之后，滚出视口太远的
    # 那一端会被裁掉（数据仍在内存），挂载条数有上界，滚动成本于是不随历史长度增长。
    STATE_PATH.unlink(missing_ok=True)
    script_reply(server)
    paging = build_app(STATE_PATH)
    async with paging.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await type_text(pilot, "/new")
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(paging.screen, ChatScreen):
                break
            await pilot.pause(0.1)
        chat = paging.screen
        session = paging.session_manager.current
        total = 5 * HISTORY_PAGE
        for index in range(total):
            message = Message(
                role="user" if index % 2 == 0 else "assistant", session_id=session.id
            )
            message.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条消息的正文，占上两行。"))
            session.messages.append(message)
        chat.rebuild()
        await pilot.pause(0.3)

        pane = chat.query_one(MessagesPane)
        check(
            len(pane._message_views) == HISTORY_PAGE,
            f"打开时只建最后一批：建了 {len(pane._message_views)} 条，每批 {HISTORY_PAGE} 条",
        )
        check(
            pane._mounted_start == total - HISTORY_PAGE,
            f"其余 {total - HISTORY_PAGE} 条留着没建：_mounted_start={pane._mounted_start}",
        )
        check(pane._mounted_end == total, f"窗口下沿贴着会话末尾：_mounted_end={pane._mounted_end}")
        check(pane._history_hint is not None, "顶端有一条写着还剩多少条的提示")
        check(
            str(pane._history_hint.render()) == f"↑ 上方还有 {total - HISTORY_PAGE} 条更早的消息",
            f"提示里的条数：{str(pane._history_hint.render())!r}",
        )
        check(
            pane.scroll_y >= pane.max_scroll_y - 1,
            f"打开之后停在最新一条：scroll_y={pane.scroll_y}，max={pane.max_scroll_y}",
        )

        def mounted_count() -> int:
            """当前挂载的消息条数。"""
            return pane._mounted_end - pane._mounted_start

        check(mounted_count() == HISTORY_PAGE, f"打开时挂载条数就是一批：{mounted_count()} 条")

        pane.focus()
        await pilot.pause()
        check(paging.focused is pane, f"焦点在消息区上，滚动键才轮得到它处理：{paging.focused!r}")

        async def settle() -> None:
            """等窗口维护（补挂/裁剪）走完。"""
            for _ in range(200):
                if not pane._loading:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.05)

        # 一路往上滚，直到看到第一条消息。中间窗口要滑动：上端补、下端裁，
        # 挂载条数始终不超过上界，且每补一批之后原来看的那条停在原处。
        rounds = 0
        while pane._mounted_start > 0 and rounds < 40:
            hint = pane._history_hint
            hint_height = hint.region.height if hint is not None else 0
            anchor = pane._message_views[session.messages[pane._mounted_start].id]
            pane.scroll_to(y=0, animate=False, immediate=True)
            await settle()
            check(
                mounted_count() <= MAX_MOUNTED,
                f"第 {rounds + 1} 轮之后挂载条数不超上界：{mounted_count()} 条（上限 {MAX_MOUNTED}）",
            )
            check(
                abs(anchor.region.y - hint_height) <= 1,
                f"第 {rounds + 1} 轮补批之后原来那条停在原处："
                f"屏幕 y={anchor.region.y}，提示行高 {hint_height}",
            )
            rounds += 1
        check(pane._mounted_start == 0, f"一路往上滚能看到最早的消息：_mounted_start={pane._mounted_start}")
        check(
            pane.query_one("MessageView").message.id == session.messages[0].id,
            "最上面一条就是最早那条消息",
        )
        check(
            mounted_count() <= MAX_MOUNTED,
            f"滚到最顶端时挂载条数仍有上界：{mounted_count()} 条",
        )
        check(
            len(pane._message_views) < total,
            f"滚到顶也没有把全部消息都建出来（远端已裁）：{len(pane._message_views)} 比 {total}",
        )
        check(not pane.following, "往上翻过之后跟随是关着的，新消息不会把画面拽回底部")

        # 往上滚之后恢复跟随：滚到底部应当重新打开跟随，新消息再到达时自动跟到底。
        pane.scroll_to(y=pane.max_scroll_y, animate=False, immediate=True)
        await settle()
        check(pane.following, "滚回底部之后跟随恢复")

        # 滚回底部之后窗口又滑向末尾，最早那几条被裁掉，最新几条挂回来。
        check(
            pane._mounted_end == total,
            f"滚回底部后窗口下沿贴着末尾：_mounted_end={pane._mounted_end}",
        )
        check(
            pane.query("MessageView").last().message.id == session.messages[-1].id,
            "最下面一条就是最新那条消息",
        )

        STATE_PATH.unlink(missing_ok=True)

    # 判定层要问一次，所以这几轮各自开一个应用，界面上按不同的键答复。
    await run_permission_checks(server)

    # 循环层与 provider 的断言放在最后：它们会造出不少 Message 与 Part，
    # 把 ID 序号推上去，前面那些按编号断言的检查会跟着变。
    await run_agent_checks()

    print(f"通过 {len(CHECKS) + len(AGENT_CHECKS)} 项检查：")
    for item in CHECKS + AGENT_CHECKS:
        print(f"  - {item}")


if __name__ == "__main__":
    asyncio.run(run())
