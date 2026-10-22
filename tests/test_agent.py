from __future__ import annotations

import ast
import asyncio
import csv
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from textual.widgets import Input  # noqa: E402

from opencode_tui.app import OpenCodeApp  # noqa: E402
from opencode_tui.core.agent.hooks import (  # noqa: E402
    POST_TOOL_USE,
    PRE_TOOL_USE,
    STOP,
    Hook,
    HookRunner,
    load_hooks,
)
from opencode_tui.core.agent.loop import (  # noqa: E402
    MAX_RETRIES,
    MAX_STEPS,
    MAX_STOP_RETRIES,
    STOP_NOTICE,
    TOOL_HOOK_NOTICE,
    AgentLoop,
    missing_tool_reason,
)
from opencode_tui.core.agent.permission import (  # noqa: E402
    CONFIG_DIRECTORY,
    PLAN_DENY,
    TOOLS_FILE,
    PermissionPolicy,
    PermissionRule,
    Verdict,
    load_disabled,
    load_permissions,
    load_tool_switches,
    project_config_directory,
    save_disabled,
)
from opencode_tui.core.agent.memory import (  # noqa: E402
    MEMORY_DIR_NAME,
    MemoryError,
    MemoryKind,
    MemoryStore,
    RecallTool,
    RememberTool,
    memory_root,
)
from opencode_tui.core.agent.context import (  # noqa: E402
    DEGRADE_ORDER,
    EVIDENCE_LIMIT,
    EVIDENCE_SHARE,
    HISTORY_SHARE,
    ContextAssembler,
    ContextAssemblyError,
    Evidence,
    EvidenceStore,
    Layer,
    evidence_ref,
    one_line,
    ordered_degraded,
)
from opencode_tui.core.agent.task import TaskTool  # noqa: E402
from opencode_tui.core.connection import (  # noqa: E402
    Connection,
    connection_path_for,
    load_connection,
    save_connection,
)
from opencode_tui.core.session_manager import SessionManager  # noqa: E402
from opencode_tui.core.agent.prompt import (  # noqa: E402
    DEEP_MARK,
    EMPTY_REASONING,
    KEEP_MESSAGES,
    MEMORY_NOTICE,
    MODEL_OUTPUT_LIMIT,
    OMISSION_MARK,
    REASONING_PLACEHOLDER,
    RULES_DIR_ENV,
    RULES_LIMIT,
    SUMMARY_INSTRUCTION,
    SUMMARY_NOTICE,
    build_system_prompt,
    compress_tool_output,
    estimate_tokens,
    is_key_line,
    project_rules,
    rules_directory,
    stable_prefix,
    summary_messages,
    translate_message,
    truncate_for_model,
)
from opencode_tui.core.agent.provider import (  # noqa: E402
    BUILTIN_MODELS,
    MODEL_ALIASES,
    DEFAULT_CONTEXT_WINDOW,
    ModelSpec,
    OpenAICompatProvider,
    ProviderConfig,
    ProviderError,
    ProviderEvent,
    ProviderEventType,
    guess_provider,
    lookup_model,
    model_names,
)
from opencode_tui.core.agent.tools import (  # noqa: E402
    BASH_TIMEOUT,
    BatchFailure,
    BatchOutcome,
    BashTool,
    GrepTool,
    PatchTool,
    ReadTool,
    RetryableToolError,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    WriteTool,
    argument_error,
    change_counts,
    decode_output,
    decode_text,
    number_lines,
    resolve_path,
)
from opencode_tui.core.agent.workspace import Workspace  # noqa: E402
from opencode_tui.core.agent.ledger import (  # noqa: E402
    ArtifactHandle,
    Entry,
    LedgerError,
    LigandSetHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_parts,
    fingerprint_path,
    fingerprint_text,
    make_handle,
    parse_handle,
)
from opencode_tui.core.agent.domain import uniprot as uniprot_module  # noqa: E402
from opencode_tui.core.agent.domain import pdbtext  # noqa: E402
from opencode_tui.core.agent.domain import preprocess as preprocess_module  # noqa: E402
from opencode_tui.core.agent.domain import structure as structure_module  # noqa: E402
from opencode_tui.core.agent.domain import chembl as chembl_module  # noqa: E402
from opencode_tui.core.agent.domain import chemistry as chemistry_module  # noqa: E402
from opencode_tui.core.agent.domain import receptor as receptor_module  # noqa: E402
from opencode_tui.core.agent.domain import ligand as ligand_module  # noqa: E402
from opencode_tui.core.agent.domain import docking as docking_module  # noqa: E402
from opencode_tui.core.agent.domain import listing as listing_module  # noqa: E402
from opencode_tui.core.agent.domain import export as export_module  # noqa: E402
from opencode_tui.core.agent.domain import complexes as complexes_module  # noqa: E402
from opencode_tui.core.agent.domain import prankweb as prankweb_module  # noqa: E402
from opencode_tui.core.agent.domain import pockets as pockets_module  # noqa: E402
from opencode_tui.core.agent.domain import align as align_module  # noqa: E402
from opencode_tui.core.agent.domain import products as products_module  # noqa: E402
from opencode_tui.core.agent.domain.all_tools import domain_tools as all_domain_tools  # noqa: E402
from opencode_tui.core.agent.trace import TraceWriter, log_directory_for  # noqa: E402
from opencode_tui.core.event_bus import EventBus  # noqa: E402
from opencode_tui.core.models import (  # noqa: E402
    Message,
    Part,
    PartType,
    Session,
    TaskState,
    ToolStatus,
)
from opencode_tui.screens.chat import ChatScreen  # noqa: E402
from opencode_tui.screens.connection_form import ConnectionFormScreen  # noqa: E402
from opencode_tui.screens.home import HomeScreen  # noqa: E402

from fake_model_server import (  # noqa: E402
    FakeModelServer,
    Reply,
    argument_fragments,
    delta_chunk,
    reasoning_chunks,
    text_chunks,
    tool_call_delta,
    usage_chunk,
)

PROBE = ROOT / ".probe"
WORKSPACE_FIXTURE = ROOT / ".probe" / "workspace_fixture"
RULES_FIXTURE = ROOT / ".probe" / "rules_fixture"
HOOK_FIXTURE = ROOT / ".probe" / "hook_fixture"

# 「确定在工作目录之外」的两条样例路径，按平台取。
# 反斜杠在 POSIX 上只是文件名里的普通字符，`D:\x` 会被拼成站内的合法文件名，
# 也确实逃不出工作目录——那一边不算越界，不是缺陷，所以夹具要跟着平台换。
OUTSIDE_PATH = r"D:\Elsewhere\secrets.txt" if os.name == "nt" else "/etc/passwd"
OUTSIDE_TRAVERSAL = r"..\..\Elsewhere\x.txt" if os.name == "nt" else "../../Elsewhere/x.txt"

# 真实坐标夹具：1IEP（ABL1 激酶域 + STI-571）。P0 口袋、叠合、write_run 三块都靠它。
# 原先读 `.probe/1iep.pdb`——那是 gitignore 的本地文件，干净 checkout 上不存在，
# 于是这三块在任何新克隆上都以 `check(True, "跳过…")` 收场，看着全绿实际没跑。
# 这里放 RCSB 的原样文件（两条链、STI、CL、水都在）：残基编号 360/315/318 与
# 最短距离 2.67 那几条断言是对着这份原始坐标写死的，裁掉任何原子数字都会变。
IEP_FIXTURE = ROOT / "tests" / "fixtures" / "1iep.pdb"

# 演示数据的 meta.json 形状（`tools/build_demo_data.py` 的产出，原样提交）。
# 原来是读 `runs/1iep-molstar/meta.json`——那是 gitignore 的本地产物，新克隆上没有，
# 于是 write_run 里「顶层键与演示数据一致」那两条一起被跳过。改 demo 的顶层形状时
# 要重新生成这份文件，否则这两条会红；那正是它们该做的事。
DEMO_META_FIXTURE = ROOT / "tests" / "fixtures" / "demo-meta.json"

# 压缩那几条断言用的上下文窗口。要明显大于系统提示的用量，否则量出来的超支
# 来自系统提示本身（它是固定开销，压缩机制缩不动），而不是压缩没起作用。
COMPACT_WINDOW = 16_000

# 领域工具的名字，**从装配处算出来**而不是手抄一份：抄一份的话，加了工具而忘了
# 更新这里，断言会一直通过，而它要挡的恰好就是「写了却没装」。
DOMAIN_TOOL_NAMES = [tool.spec.name for tool in all_domain_tools()]

CHECKS: list[str] = []


def check(condition: bool, label: str) -> None:
    """记录一条断言结果，失败时直接抛出。"""
    if not condition:
        raise AssertionError(label)
    CHECKS.append(label)


class EchoTool(Tool):
    """把 text 参数原样返回，用来验证工具登记表与参数检查。"""

    spec = ToolSpec(
        name="echo",
        description="把参数原样返回",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(output=str(arguments["text"]))


class SlowTool(Tool):
    """睡一会儿再返回，专门给「在工具执行期间按中断」留一个稳定的窗口。

    `EchoTool` 是瞬时的：测试看到 TOOL 那条 Part 再调 `cancel()` 时，循环往往
    已经走到下一步、甚至已经问完了模型，于是中断落在步与步之间而不是工具执行期间，
    这条断言就变成看机器速度。工具自己慢下来，窗口才是确定的。
    """

    spec = ToolSpec(
        name="slow",
        description="等一小会儿再返回",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        await asyncio.sleep(0.3)
        return ToolResult(output="慢完了")


class FailTool(Tool):
    """按 reason 参数抛出 ToolError。"""

    spec = ToolSpec(
        name="fail",
        description="按 reason 参数失败",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    )

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        raise ToolError(str(arguments["reason"]))


class PresetTool(Tool):
    """按预设返回一段输出，用来验工具结果怎么进证据层。"""

    spec = ToolSpec(
        name="preset",
        description="返回预设输出",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )

    def __init__(self, preset: str) -> None:
        self.preset = preset

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(output=self.preset)


class SilentProvider:
    """一次也不产出事件的假模型。

    用它跑完整的一轮，验的是「一轮开始时清空证据」这件事本身，
    不必为此起一个本地服务。
    """

    timeout = 1.0

    async def stream(self, messages: object, tools: object, effort: object) -> object:
        return
        yield  # pragma: no cover


class Recorder:
    """把经过总线的事件按顺序记下来，并在事件发生的当下抄一份工具状态。

    Part 是就地改的，事后再读只能看到最后的状态，判 PENDING → RUNNING → COMPLETED
    这条推进要在事件的当口把值抄下来。
    """

    def __init__(self, bus: EventBus) -> None:
        self.events: list[dict[str, object]] = []
        self.tool_states: list[tuple[str, str, ToolStatus]] = []
        self.session: Session | None = None
        bus.subscribe("*", self._record)

    def _record(self, payload: dict[str, object]) -> None:
        self.events.append(payload)
        if payload["type"] not in ("part.add", "part.update") or self.session is None:
            return
        part = self.part_by_id(str(payload["part_id"]))
        if part is not None and part.type is PartType.TOOL:
            self.tool_states.append(
                (str(payload["type"]), part.id, part.tool_status or ToolStatus.PENDING)
            )

    def part_by_id(self, part_id: str) -> Part | None:
        """在当前这条会话里按 ID 找 Part。"""
        assert self.session is not None
        for message in self.session.messages:
            part = message.part_by_id(part_id)
            if part is not None:
                return part
        return None

    def clear(self) -> None:
        """清掉已经记下的事件。"""
        self.events.clear()
        self.tool_states.clear()

    def names(self, *names: str) -> list[dict[str, object]]:
        """取某几类事件。"""
        return [event for event in self.events if event["type"] in names]


def check_agent_types() -> None:
    """工具登记表的形状与 provider 的数据类型。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    specs = registry.specs()
    check(len(specs) == 1, "登记一个工具后 specs() 有一项")
    entry = specs[0]
    check(
        list(entry) == ["type", "function"] and entry["type"] == "function",
        "specs() 的每一项是 type 为 function 的对象",
    )
    function = entry["function"]
    check(
        set(function) == {"name", "description", "parameters"},
        "function 里只有名字、说明、参数模式三个键",
    )
    check(function["name"] == "echo", "function.name 取自 ToolSpec")
    check(function["parameters"]["required"] == ["text"], "参数模式原样带进请求体")

    duplicated = False
    try:
        registry.register(EchoTool())
    except ValueError:
        duplicated = True
    check(duplicated, "同一个名字重复登记要抛错")
    check(registry.get("echo") is not None, "按名字取得到已登记的工具")
    check(registry.get("nope") is None, "没有登记的名字返回 None，不抛错")

    check(argument_error(EchoTool.spec, {}) == "缺少参数 text", "缺必填参数时报出参数名字")
    check(
        argument_error(EchoTool.spec, {"text": 3}) == "参数 text 应当是字符串",
        "参数类型不符时报出参数名字",
    )
    check(argument_error(EchoTool.spec, {"text": "hi"}) == "", "参数合格时返回空字符串")
    check_arguments()

    event = ProviderEvent(
        ProviderEventType.TOOL_CALL,
        tool_name="bash",
        tool_args={"command": "ls"},
        tool_call_id="call_1",
    )
    check(event.type is ProviderEventType.TOOL_CALL, "ProviderEvent 带得住事件类型")
    check(event.usage == {}, "没有用量的那种事件 usage 是空字典")
    check(
        [item.value for item in ProviderEventType]
        == ["text_delta", "reasoning_delta", "tool_call", "usage"],
        "四类流式事件齐全",
    )

    error = ProviderError("连接中断", status_code=429, retryable=True)
    check(error.status_code == 429 and error.retryable, "ProviderError 带得住状态码与可否重试")
    check(str(error) == "连接中断", "ProviderError 的文本就是错误原文")

    config = ProviderConfig.from_values(
        "http://127.0.0.1:1/v1",
        "k",
        "m",
        price_in=0.5,
        price_out=1.5,
        context_window=1_000_000,
    )
    check(config.model == "m" and config.price_out == 1.5, "ProviderConfig 从连接参数读出地址、密钥与模型")
    check(config.price_in == 0.5 and config.context_window == 1_000_000, "显式给出的单价与上下文窗口一并读出来")
    check(config.cached_price() == 0.05, "没有单独给缓存单价时按未命中价的一折算")
    bare = ProviderConfig.from_values("u", "k", "m")
    check(bare.price_in == 0.0 and bare.price_out == 0.0, "没给单价时按零计")
    check(bare.context_window == 128_000, "没给上下文窗口时用默认值")
    check(bare.configured, "三项连接参数都有时算配置完整")
    check(not ProviderConfig(base_url="", api_key="", model="").configured, "缺项时标着没配置")
    check(
        not ProviderConfig.from_values("u", "", "m").configured,
        "密钥为空也算没配置",
    )
    check_models()


def check_models() -> None:
    """内置模型清单，以及它带出来的计价与推理强度判断。"""
    check(len(BUILTIN_MODELS) >= 10, "内置清单里有十来个模型")
    flash = lookup_model("deepseek-flash")
    assert flash is not None
    check(flash.context_window == 1_000_000, "deepseek-flash 的窗口是一百万")
    check(flash.provider == "deepseek", "清单里写着发行方")
    check(flash.cached_price() == 0.03, "没单独给缓存单价时按输入价的一折算")
    reasoner = lookup_model("deepseek-reasoner")
    assert reasoner is not None
    check(reasoner.reasoning, "推理模型在清单里标着接受推理强度")
    check(not flash.reasoning, "普通模型在清单里标着不接受推理强度")
    check(reasoner.cached_price() == 0.14, "清单里写了缓存单价时用它")
    check(lookup_model("deepseek-v3") is flash, "别名归一到清单里的那一个")
    check(lookup_model("从来没有过的模型") is None, "清单里没有的名字返回 None")
    check(lookup_model("") is None, "空名字返回 None")
    check(model_names("deepseek") == ["deepseek-chat", "deepseek-flash", "deepseek-reasoner"], "按发行方筛出模型名")
    check(len(model_names()) == len(BUILTIN_MODELS), "不给发行方时列全部")
    check(guess_provider("gpt-4o-mini") == "openai", "按名字前缀猜出发行方")
    check(guess_provider("claude-3-5-haiku-20241022") == "anthropic", "按名字前缀猜出发行方")
    check(guess_provider("纳豆模型") == "", "猜不出来时给空字符串")

    config = ProviderConfig.from_values("u", "k", "deepseek-flash")
    check(config.price_in == 0.3 and config.price_out == 1.2, "只写模型名时单价取自内置清单")
    check(config.context_window == 1_000_000, "只写模型名时窗口取自内置清单")
    check(config.provider == "deepseek", "只写模型名时发行方取自内置清单")
    check(config.known and not config.supports_reasoning, "认得出来的模型标着在清单里")

    unknown = ProviderConfig.from_values("u", "k", "自研模型")
    check(not unknown.known, "清单里没有的模型标着不在清单里")
    check(unknown.price_in == 0.0 and unknown.context_window == DEFAULT_CONTEXT_WINDOW, "清单里没有的模型用兜底值")
    check(not unknown.supports_reasoning, "清单里没有的模型按不接受推理强度处理")

    size = ModelSpec(name="m", provider="p", context_window=1, price_in=2.0, price_out=3.0)
    check(size.cached_price() == 0.2, "ModelSpec 自己也算得出缓存单价")
    check(size.temperature is None and size.max_tokens is None, "没写采样参数时两个都是空")
    check(flash.temperature == 0.0, "清单里写了采样参数时带上")
    check(reasoner.max_tokens == 32_000, "清单里写了输出上限时带上")

    check(config.temperature == 0.0, "只写模型名时采样参数也取自清单")
    check(unknown.temperature is None and unknown.max_tokens is None, "清单外的模型不写采样参数")

    swapped = config.for_model("gpt-4o")
    check(swapped.model == "gpt-4o" and swapped.provider == "openai", "换模型时名字与发行方一起换")
    check(swapped.context_window == 128_000, "换模型时窗口跟着换")
    check(swapped.price_in == 2.5 and swapped.price_out == 10.0, "换模型时单价跟着换")
    check(swapped.base_url == config.base_url and swapped.api_key == config.api_key, "换模型时连接参数照旧")
    check(swapped.temperature == 1.0, "换模型时采样参数跟着换")
    check(config.for_model("deepseek-flash") is config, "换成当前这一个时原样返回")
    check(config.for_model("") is config, "换空名字时原样返回")
    stranger = config.for_model("自研模型")
    check(stranger.model == "自研模型" and not stranger.known, "换到清单外的模型时标着不在清单里")
    check(stranger.base_url == config.base_url, "换到清单外的模型时连接参数照旧")
    check(stranger.temperature is None, "换到清单外的模型时不写采样参数")


def check_connection_file() -> None:
    """连接配置的读写：与状态文件同目录、权限 0600、坏文件不炸启动。"""
    path = ROOT / ".probe" / "connection_roundtrip.toml"
    path.unlink(missing_ok=True)
    check(load_connection(path) is None, "文件不存在时读出来是 None")

    saved = save_connection(
        path,
        Connection("https://api.example.com/v1", "sk-test", "自研模型", context_window=200_000),
    )
    check(saved == path and path.is_file(), "保存之后文件落在给定路径上")
    loaded = load_connection(path)
    check(
        loaded == Connection("https://api.example.com/v1", "sk-test", "自研模型", context_window=200_000),
        f"读回来的连接参数与写进去的一致：{loaded}",
    )
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        check(mode == 0o600, f"密钥文件权限收到 0600：{oct(mode)}")

    path.write_text("这不是 TOML [[[", encoding="utf-8")
    check(load_connection(path) is None, "坏文件当作没配置，不让启动失败")
    path.write_text('base_url = "u"\napi_key = "k"\n', encoding="utf-8")
    check(load_connection(path) is None, "缺模型名时当作没配置")
    path.unlink(missing_ok=True)

    check(
        connection_path_for(ROOT / ".probe" / "state.toml")
        == ROOT / ".probe" / "connection.toml",
        "连接配置与状态文件同目录",
    )


def check_arguments() -> None:
    """参数模式的各种声明类型都能检查出来。"""
    spec = ToolSpec(
        name="probe",
        description="各种类型的参数",
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "force": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["read", "write"]},
                "options": {"type": "object", "properties": {"depth": {"type": "integer"}}},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["count"],
        },
    )
    check(argument_error(spec, {"count": 3}) == "", "整数参数合格")
    check(argument_error(spec, {"count": 1.5}) == "参数 count 应当是整数", "小数值不是整数")
    check(argument_error(spec, {"count": True}) == "参数 count 应当是整数", "真假值不是整数")
    check(argument_error(spec, {"count": "3"}) == "参数 count 应当是整数", "字符串不是整数")
    check(argument_error(spec, {"count": 1, "ratio": 0.5}) == "", "数字参数合格")
    check(argument_error(spec, {"count": 1, "ratio": "0.5"}) == "参数 ratio 应当是数字", "字符串不是数字")
    check(argument_error(spec, {"count": 1, "force": True}) == "", "真假值参数合格")
    check(argument_error(spec, {"count": 1, "force": "yes"}) == "参数 force 应当是真假值", "字符串不是真假值")
    check(argument_error(spec, {"count": 1, "mode": "read"}) == "", "取值在允许范围内")
    check(
        argument_error(spec, {"count": 1, "mode": "drop"}) == "参数 mode 只能是 ['read', 'write'] 之一",
        "取值不在允许范围内时报出来",
    )

    nested = ToolSpec(
        name="nested",
        description="嵌套对象与数组",
        parameters={
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"depth": {"type": "integer"}},
                    "required": ["depth"],
                    "additionalProperties": False,
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    )
    check(argument_error(nested, {"options": {"depth": 2}}) == "", "嵌套对象合格")
    check(
        argument_error(nested, {"options": {}}) == "缺少参数 depth",
        "嵌套对象的必填项缺了要报出来",
    )
    check(
        argument_error(nested, {"options": {"depth": 2, "extra": 1}})
        == "参数 extra 不在允许的范围内",
        "不允许额外键时多出来的键要报出来",
    )
    check(
        argument_error(nested, {"tags": ["a", "b"]}) == "",
        "数组里每一项类型相符时合格",
    )
    check(
        argument_error(nested, {"tags": ["a", 2]}) == "参数 tags[1] 应当是字符串",
        "数组里错的那一项要带上下标",
    )

    loose = ToolSpec(name="loose", description="没有类型声明的参数", parameters={"type": "object"})
    check(argument_error(loose, {"anything": object()}) == "", "没有声明类型的参数不做检查")


async def check_file_tools() -> None:
    """四个文件工具的读写与搜索。"""
    root = WORKSPACE_FIXTURE / "file_tools"
    root.mkdir(parents=True, exist_ok=True)
    for leftover in root.rglob("*"):
        if leftover.is_file():
            leftover.unlink()

    check(resolve_path(root, "a.txt") == (root / "a.txt").resolve(), "相对路径按工具自己的基准解释")
    check(resolve_path(root, "/tmp/x") == Path("/tmp/x").resolve(), "绝对路径不再拼接")
    check(number_lines("甲\n乙", start=1) == "     1 | 甲\n     2 | 乙", "行号补到六位宽")
    check(number_lines("甲", start=1000).startswith("  1000 | "), "行号位数不够时按实际宽度撑开")
    check(change_counts("a\nb\n", "a\nb\n") == (0, 0), "内容没变时增删都是零")
    check(change_counts("a\nb\n", "a\nb\nc\n") == (1, 0), "末尾加一行算新增一行")
    check(change_counts("a\nb\n", "a\n") == (0, 1), "末尾删一行算删除一行")
    check(change_counts("a\nb\nc\n", "a\nx\nc\n") == (1, 1), "改中间一行算一增一删")
    check(change_counts("a\nb\n", "x\ny\n") == (2, 2), "整段换掉时两边的行都算")

    read = ReadTool(cwd=root, limit=5000)
    write = WriteTool(cwd=root)
    patch = PatchTool(cwd=root)
    grep = GrepTool(cwd=root)

    missing = await read.run({"path": "没有这个文件.txt"})
    check(missing.failed and "文件不存在" in missing.output, "读不存在的文件返回说清楚的失败")
    check(
        argument_error(ReadTool.spec, {"path": "a.txt", "列": 1}) == "参数 列 不在允许的范围内",
        "读工具不接受没声明的参数",
    )
    check(argument_error(WriteTool.spec, {"path": "a.txt"}) == "缺少参数 content", "写工具要两个参数都有")
    check(
        argument_error(PatchTool.spec, {"path": "a.txt", "old": "x"}) == "缺少参数 new",
        "改工具三个参数缺一不可",
    )
    check(argument_error(GrepTool.spec, {}) == "缺少参数 pattern", "搜工具要给出要搜的东西")

    wrote = await write.run({"path": "notes/a.txt", "content": "甲\n乙\n丙\n"})
    check(not wrote.failed, "写新文件不报错")
    check(wrote.added == 3 and wrote.removed == 0, "新写的三行都算新增")
    check((root / "notes" / "a.txt").exists(), "父目录不存在时一并建出来")
    check("3 行" in wrote.output, "写了多少行写在结果里")

    again = await write.run({"path": "notes/a.txt", "content": "甲\n乙\n丙\n"})
    check(again.added == 0 and again.removed == 0, "内容一样时再写一遍增删都是零")

    read_back = await read.run({"path": "notes/a.txt"})
    check(not read_back.failed, "读刚写的文件不报错")
    check("     1 | 甲" in read_back.output, "读出来的每一行带文件里的绝对行号")
    check(read_back.output.endswith("丙"), "文件里的三行都读出来")

    window = await read.run({"path": "notes/a.txt", "offset": 2, "limit": 1})
    check("     2 | 乙" in window.output and "甲" not in window.output, "offset 与 limit 只取中间一段")
    check("offset=3" in window.output, "还有剩的时候告诉模型接着从哪里读")

    beyond = await read.run({"path": "notes/a.txt", "offset": 99})
    check(beyond.failed and "只有 3 行" in beyond.output, "越过文件末尾时说明文件有多长")

    directory = await read.run({"path": "notes"})
    check(directory.failed and "是目录" in directory.output, "读一个目录时给的是说明不是异常")
    bad_offset = ""
    try:
        await read.run({"path": "notes/a.txt", "offset": 0})
    except ToolError as error:
        bad_offset = str(error)
    check("offset" in bad_offset, "offset 不合法时抛出能回灌的失败")

    binary = root / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01\xff")
    unreadable = await read.run({"path": "blob.bin"})
    check(unreadable.failed and "不是文本文件" in unreadable.output, "二进制文件读不出来时说明原因")

    # Windows 上 `cmd` 的 `>` 重定向写出的是**带标记的 UTF-16**。模型自己重定向出来的
    # 文件读不了，只好改用 bash 一段段打印——现场 `ses_0037` 第 16~22 步，七步里六步
    # 耗在这上面。所以「读得出来」这件事要单独盯住。
    utf16 = root / "out.txt"
    utf16.write_bytes("甲\n乙\n丙\n".encode("utf-16"))
    wide = await read.run({"path": "out.txt"})
    check(not wide.failed, f"带标记的 UTF-16 要读得出来：{wide.output[:80]}")
    check("     1 | 甲" in wide.output and wide.output.endswith("丙"), "UTF-16 的内容按行读出来")
    patched = await patch.run({"path": "out.txt", "old": "乙", "new": "改过的"})
    check(not patched.failed, f"UTF-16 的文件也改得了：{patched.output[:80]}")
    check("改过的" in utf16.read_text(encoding="utf-8"), "改完写回盘上的是改后的内容")

    oneline = root / "one.txt"
    oneline.write_text("就这一行", encoding="utf-8")
    check("     1 | 就这一行" in (await read.run({"path": "one.txt"})).output, "没有换行符的文件也算一行")

    empty = await write.run({"path": "empty.txt", "content": ""})
    check(not empty.failed and empty.added == 0, "写空内容不算一行")
    check((await read.run({"path": "empty.txt"})).output == "", "空文件读出来是空字符串")

    onto_directory = await write.run({"path": "notes", "content": "x"})
    check(onto_directory.failed and "是目录" in onto_directory.output, "往目录上写时返回失败")

    changed = await patch.run({"path": "notes/a.txt", "old": "乙", "new": "乙改"})
    check(not changed.failed, "唯一命中的替换成功")
    check(changed.added == 1 and changed.removed == 1, "改一行算一增一删")
    check("+1 -1" in changed.output, "增删行数写在结果里")
    check("乙改" in (root / "notes" / "a.txt").read_text(encoding="utf-8"), "改完之后文件里是新内容")

    absent = await patch.run({"path": "notes/a.txt", "old": "没有这段", "new": "x"})
    check(absent.failed and "没有找到" in absent.output, "原文里没有这一段时报失败")

    (root / "dup.txt").write_text("同一个词\n同一个词\n", encoding="utf-8")
    duplicated = await patch.run({"path": "dup.txt", "old": "同一个词", "new": "换掉"})
    check(duplicated.failed and "出现了 2 处" in duplicated.output, "命中多处时报失败并给出次数")
    check((root / "dup.txt").read_text(encoding="utf-8").count("同一个词") == 2, "命中多处时文件没被动过")

    on_missing = await patch.run({"path": "没有这个文件.txt", "old": "a", "new": "b"})
    check(on_missing.failed and "文件不存在" in on_missing.output, "改不存在的文件返回失败")

    deleted = await patch.run({"path": "notes/a.txt", "old": "丙\n", "new": ""})
    check(not deleted.failed and deleted.removed == 1, "换上空字符串表示删掉这一段")

    (root / "search").mkdir(exist_ok=True)
    (root / "search" / "one.py").write_text("import os\n价值 = 42\n", encoding="utf-8")
    (root / "search" / "two.py").write_text("价值 = 43\n另一个 = 42\n", encoding="utf-8")
    (root / "search" / "three.txt").write_text("价值在文本里\n", encoding="utf-8")
    (root / "search" / "skip.md").write_text("价值在 md 里\n", encoding="utf-8")

    hits = await grep.run({"pattern": "价值", "path": "search"})
    check(not hits.failed, "搜一趟目录不报错")
    check("one.py:2:价值 = 42" in hits.output, "命中行带路径、行号与内容")
    check("two.py:1:价值 = 43" in hits.output, "另一个文件里的命中也在")
    check("three.txt:1:价值在文本里" in hits.output, "常见文本后缀也搜")
    check("skip.md" in hits.output, "md 也在要搜的后缀里")

    by_glob = await grep.run({"pattern": "价值", "path": "search", "glob": "*.txt"})
    check("three.txt" in by_glob.output and "one.py" not in by_glob.output, "glob 给出时只搜对得上的文件名")

    single = await grep.run({"pattern": "价值", "path": "search/one.py"})
    check(single.output.startswith("one.py:2") or "one.py:2" in single.output, "path 给单个文件时只搜它")

    none = await grep.run({"pattern": "找不到的词", "path": "search"})
    check(not none.failed and "没有找到" in none.output, "一条都没命中时给一句说明而不是失败")

    broken = await grep.run({"pattern": "[未闭合", "path": "search"})
    check(broken.failed and "正则表达式写错了" in broken.output, "正则写错时给出说明而不是抛异常")

    absent_path = await grep.run({"pattern": "x", "path": "没有这个目录"})
    check(absent_path.failed and "路径不存在" in absent_path.output, "搜一个不存在的路径时返回失败")

    caps = await grep.run({"pattern": "价值", "path": "search"})
    check("价值" in caps.output, "默认区分大小写")
    (root / "search" / "case.txt").write_text("Value = 1\n", encoding="utf-8")
    strict = await grep.run({"pattern": "value", "path": "search"})
    check("case.txt" not in strict.output, "默认不分大小写之外的写法不会命中")
    loose_case = await grep.run({"pattern": "value", "path": "search", "ignore_case": True})
    check("case.txt:1:Value = 1" in loose_case.output, "ignore_case 打开后不分大小写")

    many = GrepTool(cwd=root, limit=2, file_limit=10)
    capped = await many.run({"pattern": "价值", "path": "search"})
    check(len([line for line in capped.output.split("\n") if ":1:" in line or ":2:" in line]) <= 2, "命中条数按上限截住")
    check("只列出前面的" in capped.output, "截住的时候告诉模型还有更多")

    git_dir = root / "search" / ".git"
    git_dir.mkdir(exist_ok=True)
    (git_dir / "config").write_text("价值在版本库里\n", encoding="utf-8")
    skipped = await grep.run({"pattern": "价值", "path": "search"})
    check(".git" not in skipped.output, "版本库目录默认跳过")

    (root / "search" / "noise.log").write_text("价值在日志里\n", encoding="utf-8")
    only_code = await grep.run({"pattern": "价值", "path": "search"})
    check("noise.log" not in only_code.output, "没给 glob 时只搜认得出的文本后缀")

    sub = root / "search" / "deep" / "inner"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "deeper.py").write_text("价值在深处\n", encoding="utf-8")
    deep = await grep.run({"pattern": "价值", "path": "search"})
    check("deeper.py" in deep.output, "子目录里的文件也要搜到")

    empty_args = ""
    try:
        await grep.run({"pattern": "x", "path": ""})
    except ToolError as error:
        empty_args = str(error)
    check("path" in empty_args, "path 给空字符串时报的是参数问题")

    no_pattern = ""
    try:
        await read.run({"path": "  "})
    except ToolError as error:
        no_pattern = str(error)
    check("path" in no_pattern, "path 给空白时报的是参数问题")


def check_trace() -> None:
    """事件日志把每条事件写成一行，按会话分开。"""
    directory = ROOT / ".probe" / "trace_fixture"
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.iterdir():
        stale.unlink()

    bus = EventBus()
    current = {"session": "ses_trace_a"}
    writer = TraceWriter(bus, lambda: current["session"], directory)
    writer.start()

    bus.publish("message.add", {"message_id": "msg_1", "session_id": "ses_trace_a"})
    bus.publish("part.add", {"message_id": "msg_1", "part_id": "prt_1"})
    bus.publish("turn.done", {"message_id": "msg_1", "session_id": "ses_trace_a"})

    first = writer.current_path()
    rows = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
    check(len(rows) == 3, f"三条事件写成三行，实际 {len(rows)} 行")
    check(
        [row["event"] for row in rows] == ["message.add", "part.add", "turn.done"],
        "事件名按顺序记下",
    )
    check(rows[0]["payload"]["message_id"] == "msg_1", "负载原样记下")
    check("type" not in rows[0]["payload"], "负载里不再重复事件名")
    check(isinstance(rows[0]["at"], float), "每行带一个时间戳")

    current["session"] = "ses_trace_b"
    bus.publish("part.add", {"message_id": "msg_2", "part_id": "prt_2"})
    second = writer.current_path()
    check(second != first, "换会话之后写的是另一个文件")
    check(
        second.is_file() and len(second.read_text(encoding="utf-8").splitlines()) == 1,
        "新会话的文件里只有换过去之后的那一条",
    )
    check(
        len(first.read_text(encoding="utf-8").splitlines()) == 3,
        "换会话之后原来那个文件不再增长",
    )

    writer.stop()
    called = bus.publish("part.add", {"message_id": "msg_3", "part_id": "prt_3"})
    check(called == 0, f"停止之后不再收到事件，实际打到 {called} 个处理函数")
    check(
        len(second.read_text(encoding="utf-8").splitlines()) == 1,
        "停止之后没有新的行",
    )


def check_rules() -> None:
    """项目规则文件的读取、截断与每轮重读。"""
    RULES_FIXTURE.mkdir(parents=True, exist_ok=True)
    path = RULES_FIXTURE / "AGENTS.md"
    path.unlink(missing_ok=True)

    check(project_rules(RULES_FIXTURE) == "", "规则文件不存在时读出空字符串")

    path.write_text("# 项目规则\n\n不许用某个字。\n", encoding="utf-8")
    text = project_rules(RULES_FIXTURE)
    check("不许用某个字。" in text, "规则文件的内容读了出来")
    check(not text.endswith("\n"), "读出来之后去掉了末尾的换行")

    path.write_text("# 改过之后的规则\n", encoding="utf-8")
    check("改过之后的规则" in project_rules(RULES_FIXTURE), "每轮重新读，改动立刻生效")

    lines = [f"第 {index} 行" + "字" * 40 for index in range(819)]
    body = "\n".join(lines)
    path.write_text(body, encoding="utf-8")
    cut = project_rules(RULES_FIXTURE)
    kept = cut.splitlines()
    check("省略了" in kept[-1], f"超长规则文件末尾写明省略：{kept[-1]}")
    check(
        kept[:-1] == lines[: len(kept) - 1],
        "截断之后留下的正好是开头的那些行",
    )
    check(len(kept) - 1 < len(lines), "确实截掉了后面的行")
    kept_bytes = sum(len(line.encode("utf-8")) + 1 for line in kept[:-1])
    check(kept_bytes <= RULES_LIMIT, f"留下的部分没有超过上限：{kept_bytes} 字节")
    check(
        kept_bytes + len(lines[len(kept) - 1].encode("utf-8")) + 1 > RULES_LIMIT,
        "再多留一行就会超过上限",
    )

    session = Session(id="ses_rules", title="规则")
    path.write_text("# 只许读，不许写\n", encoding="utf-8")

    # 规则的位置由代码定，不由工作目录定：传进去的工作目录是别处，
    # 读到的仍是 rules_directory() 指的那一份。
    check(
        rules_directory(WORKSPACE_FIXTURE) == ROOT,
        "没设 VSA_RULES_DIR 时，规则目录固定是开发项目根，与工作目录无关",
    )

    os.environ[RULES_DIR_ENV] = str(RULES_FIXTURE)
    try:
        check(rules_directory(WORKSPACE_FIXTURE) == RULES_FIXTURE, "VSA_RULES_DIR 能顶掉规则目录")
        with_rules = build_system_prompt(Workspace(session.id, root=WORKSPACE_FIXTURE), session)
        check("只许读，不许写" in with_rules, "系统提示里带上规则文件的内容")
        check(
            with_rules.index("只许读，不许写") < with_rules.index("工作目录："),
            "规则排在工作目录之前",
        )

        os.environ[RULES_DIR_ENV] = str(RULES_FIXTURE / "empty")
        without = build_system_prompt(Workspace("ses_rules_none", root=WORKSPACE_FIXTURE), session)
        check("这个项目的规则" not in without, "规则文件不存在时系统提示里没有那一段")
    finally:
        os.environ.pop(RULES_DIR_ENV, None)

    path.unlink(missing_ok=True)


def build_permission_registry(disabled: frozenset[str] | None = None) -> ToolRegistry:
    """判定那一组断言用的工具表，五个工具都用真的。

    `disabled` 交给注册表：被关的工具连 `register` 都不该进去。
    """
    registry = ToolRegistry(disabled=disabled)
    registry.register(BashTool(cwd=WORKSPACE_FIXTURE))
    registry.register(ReadTool(cwd=WORKSPACE_FIXTURE))
    registry.register(WriteTool(cwd=WORKSPACE_FIXTURE))
    registry.register(PatchTool(cwd=WORKSPACE_FIXTURE))
    registry.register(GrepTool(cwd=WORKSPACE_FIXTURE))
    registry.register(TaskTool())
    return registry


def check_permission() -> None:
    """判定链逐级实测：命中的是哪一级要分得出来。"""
    registry = build_permission_registry()

    def judge(policy: PermissionPolicy, name: str, arguments: dict[str, object], **options: object) -> object:
        options.setdefault("agent_name", "Build")
        return policy.check(registry.get(name), arguments, **options)  # type: ignore[arg-type]

    for name, argument in (
        ("bash", "command"),
        ("read", "path"),
        ("write", "path"),
        ("patch", "path"),
        ("grep", "pattern"),
    ):
        check(
            registry.get(name).primary_argument == argument,
            f"{name} 的主要参数是 {argument}",
        )

    plain = PermissionPolicy()
    check(
        judge(plain, "read", {"path": "a.py"}, cancelled=True).verdict is Verdict.DENY,
        "第一级：已中断时一律拒绝",
    )
    for name, arguments in (
        ("write", {"path": "a.py", "content": ""}),
        ("patch", {"path": "a.py", "old": "a", "new": "b"}),
        ("bash", {"command": "ls"}),
    ):
        decision = judge(plain, name, arguments, agent_name="Plan")
        check(
            decision.verdict is Verdict.DENY and "Plan" in decision.reason,
            f"第二级：Plan 方式下 {name} 直接拒绝",
        )
    for name, arguments in (("read", {"path": "a.py"}), ("grep", {"pattern": "x"})):
        check(
            judge(plain, name, arguments, agent_name="Plan").verdict is Verdict.ALLOW,
            f"Plan 方式下 {name} 照常放行",
        )

    project = PermissionPolicy(
        project_rules=[PermissionRule("bash", "rm -rf*", Verdict.DENY, "项目配置")]
    )
    decision = judge(project, "bash", {"command": "rm -rf /"})
    check(
        decision.verdict is Verdict.DENY and decision.rule == "项目配置：bash rm -rf*",
        f"第三级：项目配置里的 deny 命中：{decision.rule}",
    )
    check(
        judge(project, "bash", {"command": "ls"}).verdict is Verdict.ASK,
        "项目 deny 没命中的走默认询问",
    )

    ordered = PermissionPolicy(
        project_rules=[
            PermissionRule("bash", "git push*", Verdict.ASK, "项目配置"),
            PermissionRule("bash", "git push --force*", Verdict.DENY, "项目配置"),
        ]
    )
    check(
        judge(ordered, "bash", {"command": "git push --force origin"}).verdict is Verdict.DENY,
        "项目配置里 deny 排在 ask 之前",
    )

    session_policy = PermissionPolicy()
    check(
        judge(session_policy, "bash", {"command": "ls"}).verdict is Verdict.ASK,
        "第五级之前：默认策略下 bash 要问一次",
    )
    # 两级「本会话记住」：remember 记一个工具，allow_all 记整个会话。
    rule = session_policy.remember(registry.get("bash"), {"command": "ls"})
    check(
        rule.describe() == "本会话记住：bash *",
        f"按工具记住的是一条工具级规则（参数位是 *）：{rule.describe()}",
    )
    decision = judge(session_policy, "bash", {"command": "ls"})
    check(
        decision.verdict is Verdict.ALLOW and "本会话" in decision.rule,
        f"本会话记住之后同样的调用放行：{decision.rule}",
    )
    check(
        judge(session_policy, "bash", {"command": "pwd"}).verdict is Verdict.ALLOW,
        "按工具记住的是整个工具，换一条命令也放行",
    )
    check(
        judge(session_policy, "bash", {"command": "ls "}).verdict is Verdict.ALLOW,
        "命令末尾多一个空格照样放行（不再逐字比对原文）",
    )
    # 按工具的粒度只到那一个工具，别的工具照旧要问。
    check(
        judge(session_policy, "write", {"path": "a.txt", "content": "x"}).verdict is Verdict.ASK,
        "按工具记住不会把别的工具也带过去：write 照旧要问",
    )

    # 弹窗上的 a 走的是这一条：本会话所有工具一次放行。
    session_policy.allow_all()
    check(session_policy.allows_all, "allow_all 之后标记置上")
    for name, arguments in (
        ("write", {"path": "a.txt", "content": "x"}),
        ("patch", {"path": "a.py", "old": "a", "new": "b"}),
        ("bash", {"command": "rm -rf build"}),
        ("read", {"path": "notes.md"}),
    ):
        decision = judge(session_policy, name, arguments)
        check(
            decision.verdict is Verdict.ALLOW and "所有工具" in decision.rule,
            f"全放行之后 {name} 不再问：{decision.rule}",
        )
    session_policy.clear_session()
    check(not session_policy.allows_all, "切换会话把全放行标记一并清掉")
    check(
        judge(session_policy, "bash", {"command": "ls"}).verdict is Verdict.ASK,
        "切换会话之后记住的允许清掉了",
    )
    check(
        judge(session_policy, "write", {"path": "a.txt", "content": "x"}).verdict is Verdict.ASK,
        "切换会话之后全放行也清掉了",
    )

    user = PermissionPolicy(
        user_rules=[PermissionRule("read", "secret*", Verdict.DENY, "用户配置")]
    )
    check(
        judge(user, "read", {"path": "secret.txt"}).verdict is Verdict.DENY,
        "第六级：用户配置里的 deny 命中",
    )
    layered = PermissionPolicy(
        project_rules=[PermissionRule("read", "notes*", Verdict.ASK, "项目配置")],
        user_rules=[PermissionRule("read", "*", Verdict.ALLOW, "用户配置")],
    )
    check(
        judge(layered, "read", {"path": "notes.md"}).verdict is Verdict.ASK,
        "项目配置盖过用户配置",
    )

    check(judge(plain, "grep", {"pattern": "x"}).verdict is Verdict.ALLOW, "第七级：默认策略下 grep 放行")
    check(
        judge(plain, "read", {"path": "a.py"}).verdict is Verdict.ALLOW,
        "第七级：默认策略下 read 放行",
    )
    for name, arguments in (
        ("write", {"path": "a", "content": ""}),
        ("patch", {"path": "a", "old": "x", "new": "y"}),
        ("bash", {"command": "ls"}),
    ):
        check(
            judge(plain, name, arguments).verdict is Verdict.ASK,
            f"第七级：默认策略下 {name} 要问一次",
        )

    # 工作目录之外的访问：单独一级判定，排在两级「本会话放行」之前。
    # 这一级的存在意义就是让「按 a 全放行」不等于「整个磁盘随便动」。
    bounded = PermissionPolicy(workspace_root=WORKSPACE_FIXTURE)
    outside = OUTSIDE_PATH
    check(
        judge(bounded, "read", {"path": "notes.md"}).verdict is Verdict.ALLOW,
        "工作目录内的读照旧放行",
    )
    check(
        judge(bounded, "read", {"path": outside}).verdict is Verdict.ASK,
        "工作目录之外的读要问一次",
    )
    check(
        judge(bounded, "write", {"path": outside, "content": "x"}).verdict is Verdict.ASK,
        "工作目录之外的写要问一次",
    )
    decision = judge(bounded, "read", {"path": outside})
    check(
        "工作目录之外" in decision.reason and outside in decision.reason,
        f"拦下来的原因写明越界，并把那个路径带出来：{decision.reason}",
    )
    check(
        judge(bounded, "bash", {"command": f"type {outside}"}).verdict is Verdict.ASK,
        "bash 命令里出现工作目录之外的绝对路径也要问一次",
    )
    check(
        judge(bounded, "bash", {"command": "dir"}).verdict is Verdict.ASK,
        "bash 站内命令走它自己那一档（默认要问）",
    )
    # 关键的不变式：按 a 之后越界仍然要问。
    bounded.allow_all()
    check(
        judge(bounded, "bash", {"command": "dir"}).verdict is Verdict.ALLOW,
        "全放行之后 bash 站内不再问",
    )
    check(
        judge(bounded, "bash", {"command": f"type {outside}"}).verdict is Verdict.ASK,
        "全放行之后 bash 越界仍然要问（a 放行的是工具，不是磁盘）",
    )
    check(
        judge(bounded, "read", {"path": outside}).verdict is Verdict.ASK,
        "全放行之后越界读仍然要问",
    )
    check(
        judge(bounded, "read", {"path": "notes.md"}).verdict is Verdict.ALLOW,
        "全放行之后站内读不再问",
    )
    bounded.clear_session()

    # 判定用的路径要先补成绝对路径：`..\..\x` 看字符串是相对的，
    # 补全之后才知道跑到了外面。分隔符跟着平台走——POSIX 上反斜杠不是分隔符。
    #
    # 往上几级不能写死。仓库本身可能就落在 /tmp 下（CI 模拟常见），那时往上两级
    # 还在 `tempfile.gettempdir()` 这条放行前缀里，解析出来是「站内」，断言就假失败。
    # POSIX 上按 /etc 反算相对路径：目标固定在工作目录之外，与仓库被 clone 到哪无关。
    # Windows 保留原样——跨盘符算不出相对路径，那边的 `..\..\` 本来就指着别的盘。
    traversal = (
        OUTSIDE_TRAVERSAL
        if os.name == "nt"
        else os.path.relpath(Path(OUTSIDE_PATH).parent / "x.txt", WORKSPACE_FIXTURE)
    )
    check(
        judge(bounded, "read", {"path": traversal}).verdict is Verdict.ASK,
        f"相对路径补全之后跑到外面的，也要问：{traversal}",
    )
    check(
        judge(bounded, "read", {"path": "sub/notes.md"}).verdict is Verdict.ALLOW,
        "工作目录内的子路径属于站内",
    )
    # 没配工作目录时这一级整条不生效：不能让「没配置」变成「所有路径都越界」。
    check(
        judge(plain, "read", {"path": outside}).verdict is Verdict.ALLOW,
        "没配工作目录时不做越界判定（否则每一步都会被拦）",
    )

    # 命令行的开关不是路径：`dir /T:W` 里的 /T 和 `/S` 都被当成过越界。
    # 这条只在 Windows 成立——POSIX 上 `/T` 就是一个根下的普通路径，本来就该拦。
    check(
        judge(bounded, "bash", {"command": "dir /T:W"}).verdict is Verdict.ASK,
        "带斜杠开关的命令按 bash 那一档处理，不因开关误报越界",
    )
    if os.name == "nt":
        check(
            not bounded.outside_paths(registry.get("bash"), {"command": "dir /T:W /S"}),
            "斜杠开关不被当成绝对路径",
        )
    # URL 里有 `//`，不剔掉的话每条打接口的命令都会被判越界。
    check(
        not bounded.outside_paths(
            registry.get("bash"),
            {"command": 'curl -s "https://www.ebi.ac.uk/chembl/api/x?limit=1"'},
        ),
        "URL 里的双斜杠不被当成 UNC 路径",
    )
    check(
        not bounded.outside_paths(registry.get("bash"), {"command": "echo hello"}),
        "普通命令没有越界路径",
    )
    # 临时目录与标准流转接必须放行，否则这两类每天都在写的命令会次次弹窗，
    # 用户很快就会条件反射按 y，越界询问整体失效。
    check(
        not bounded.outside_paths(
            registry.get("bash"),
            {"command": f"ls {tempfile.gettempdir()}"},
        ),
        "平台临时目录不算越界",
    )
    if os.name != "nt":
        check(
            not bounded.outside_paths(registry.get("bash"), {"command": "echo hi > /dev/null"}),
            "POSIX 上重定向到 /dev/null 不算越界",
        )

    # 根与顶层目录：以前只认两级以上的路径，`rm -rf /usr`、`rm -rf /` 都判成
    # 「没有越界」——按过 a（本会话全放行）之后就直接执行了。这是这一组最该守的。
    for risky in ("rm -rf /", "rm -rf /usr", "cat /etc", "rm -rf ~", "rm -rf ~/data"):
        check(
            bool(bounded.outside_paths(registry.get("bash"), {"command": risky})),
            f"顶层目录与家目录要判成越界：{risky}",
        )
    check(
        not bounded.outside_paths(
            registry.get("bash"),
            {"command": f"cat {Path.home() / '.opencode_tui' / 'state.toml'}"},
        ),
        "程序自己的数据目录仍然放行（`~` 展开之后要落回放行前缀）",
    )

    # 长路径不能把判定卡住。这不是性能基准，是挂起防护：判断「在不在前缀之内」
    # 原先走 `item in target.parents`，深度两万时要把每一级父目录都造一遍，
    # 实测 9 秒；而命令内容由模型给。上限给得很宽，只挡平方级那种实现。
    started = time.monotonic()
    deep = "/" + "a/" * 20000
    check(
        bool(bounded.outside_paths(registry.get("bash"), {"command": deep})),
        "两万级的深路径仍然判出越界",
    )
    elapsed = time.monotonic() - started
    check(elapsed < 3.0, f"深路径不把判定卡住：{elapsed:.2f}s")

    # 反过来：相对路径不能被摘出「斜杠开头的片段」。误报比漏判更伤——
    # 用户被无意义地拦几次之后，就会对越界询问条件反射地按 y。
    for harmless in ("ls a/b/c", "./scripts/run.sh", "python src/main.py", "sed 's/a/b/'"):
        check(
            not bounded.outside_paths(registry.get("bash"), {"command": harmless}),
            f"相对路径不算越界：{harmless}",
        )

    check(
        judge(plain, "bash", {"command": "rm -rf /"}, explicit=True).verdict is Verdict.ALLOW,
        "用户直接给出的命令跳过全部询问级",
    )
    check(
        judge(
            plain, "write", {"path": "a", "content": ""}, agent_name="Plan", explicit=True
        ).verdict
        is Verdict.ALLOW,
        "用户直接给出的命令在 Plan 方式下也放行",
    )
    check(
        judge(plain, "bash", {"command": "ls"}, explicit=True, cancelled=True).verdict
        is Verdict.DENY,
        "已中断时用户直接给出的命令也拒绝",
    )
    check(
        judge(project, "bash", {"command": "rm -rf /"}, explicit=True).verdict is Verdict.ALLOW,
        "用户直接给出的命令不被项目 deny 拦下",
    )

    config = ROOT / ".probe" / "permission_fixture"
    config.mkdir(parents=True, exist_ok=True)
    path = config / "permissions.toml"
    path.unlink(missing_ok=True)
    check(load_permissions(path, "项目配置") == [], "配置文件不存在时读出空表")

    path.write_text('[bash]\ndeny = ["rm -rf*"]\nask = ["git push*"]\n', encoding="utf-8")
    rules = load_permissions(path, "项目配置")
    check(len(rules) == 2, f"两条规则都读了出来，实际 {len(rules)} 条")
    check({rule.pattern for rule in rules} == {"rm -rf*", "git push*"}, "模式读了出来")
    check(
        {rule.verdict for rule in rules} == {Verdict.DENY, Verdict.ASK},
        "判定读了出来",
    )
    check(all(rule.source == "项目配置" for rule in rules), "来源写在规则上")

    path.write_text('[bash]\ndenny = ["x"]\n', encoding="utf-8")
    raised = ""
    try:
        load_permissions(path, "项目配置")
    except ValueError as error:
        raised = str(error)
    check("denny" in raised, f"判定写错一个字母时直接报错：{raised}")
    path.unlink(missing_ok=True)

    check_disabled_tools()


def check_disabled_tools() -> None:
    """工具开关：标了关的工具根本不装进注册表。

    这一组与 `check_permission` 刻意分开立题：那边验的是「调用时放不放行」，
    这边验的是「装不装进来」。两者看起来都像「不让用某个工具」，实际差得很远——
    权限层拦下来之后那个工具**照旧在请求体的 tools 数组里**，模型会一直重试；
    开关关掉之后它不在数组里，模型连它存在都不知道。
    """
    root = ROOT / ".probe" / "tools_fixture"
    config = root / ".opencode_tui"
    config.mkdir(parents=True, exist_ok=True)
    path = config / "tools.toml"
    path.unlink(missing_ok=True)

    check(load_disabled(root)[0] == frozenset(), "开关文件不存在时读出空清单")
    check(len(load_disabled(root)) == 2, "开关读出来是「清单 + 来源」两样")

    path.write_text('[tools]\nbash = false\nlist_workspace = false\n', encoding="utf-8")
    disabled, source = load_disabled(root)
    check(
        disabled == frozenset({"bash", "list_workspace"}),
        f"标了 false 的工具都读了出来：{sorted(disabled)}",
    )
    check(source == path, f"来源指向那份配置文件：{source}")

    registry = build_permission_registry(disabled=disabled)
    names = registry.names()
    check("bash" not in names, "被关的工具不进注册表")
    check("list_workspace" not in names, "领域工具一样能被关掉")
    check("read" in names, "没在清单里的工具照旧装上")
    sent = {item["function"]["name"] for item in registry.specs()}
    check(
        "bash" not in sent and "list_workspace" not in sent,
        "被关的工具不在请求体的 tools 数组里（这才叫「不出现在上下文中」）",
    )
    check(
        len(sent) == len(names),
        "工具数组与注册表一致，没有哪个被漏掉或多出来",
    )

    # 只认 false。写别的值等于没关：这一层的语义是「关」，不是「配」，
    # 认不出的键按「没关」处理，比让整个应用起不来强。
    path.write_text('[tools]\nbash = "no"\nwrite = true\n', encoding="utf-8")
    check(load_disabled(root)[0] == frozenset(), "只有 false 才算关，别的值都不算")
    path.write_text("这不是 TOML [[[\n", encoding="utf-8")
    check(load_disabled(root)[0] == frozenset(), "配置文件坏掉时当作没读到，不连累启动")

    # 关掉的工具真被模型叫到时，回给它的话要与「压根没这个工具」分得开：
    # 前者重试无用、也不该换个方式绕，后者让它自己改名字。
    #
    # 这里直接调判定函数，不起整个循环层：它是纯函数（只看注册表的禁用清单），
    # 起一层要配事件总线、模型配置、工作目录，为验一句话不值当。
    path.write_text("[tools]\nbash = false\n", encoding="utf-8")
    disabled, _ = load_disabled(root)
    registry = build_permission_registry(disabled=disabled)
    reason = missing_tool_reason(registry, "bash")
    check("禁用" in reason and "重试无用" in reason, f"被关的工具给出的是「已被禁用」：{reason}")
    check(
        "unknown tool" in missing_tool_reason(registry, "nonexistent_thing"),
        "真没有的工具照旧报 unknown tool",
    )
    check(
        "unknown tool" in missing_tool_reason(registry, "list_workspace"),
        "没被关的工具即使不在这一份注册表里也报 unknown tool，不误报成禁用",
    )

    # 用户配置那一侧同样生效，且「任一处说了关就关」。
    path.unlink(missing_ok=True)
    user_dir = Path.home() / CONFIG_DIRECTORY
    user_file = user_dir / TOOLS_FILE
    existed = user_file.is_file()
    backup = user_file.read_text(encoding="utf-8") if existed else ""
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        user_file.write_text("[tools]\ngrep = false\n", encoding="utf-8")
        check(
            load_disabled(root)[0] == frozenset({"grep"}),
            "用户配置里的开关读得到（对所有项目生效）",
        )
        path.write_text("[tools]\nbash = false\ngrep = true\n", encoding="utf-8")
        check(
            load_disabled(root)[0] == frozenset({"bash", "grep"}),
            "用户说关、项目说开时仍然是关（任一处说了关就关）",
        )
    finally:
        path.unlink(missing_ok=True)
        if existed:
            user_file.write_text(backup, encoding="utf-8")
        else:
            user_file.unlink(missing_ok=True)

    # 这一条是整组的要害：把配置读出来的清单真的拿去装配，断言被关的工具确实少了。
    # 上面那几条都只验了「清单读出来了」，清单读出来却没用上一样是白读。
    config.mkdir(parents=True, exist_ok=True)
    path.write_text("[tools]\nsearch_pdb = false\n", encoding="utf-8")
    disabled, _ = load_disabled(root)
    live = ToolRegistry(disabled=disabled)
    for tool in all_domain_tools():
        live.register(tool)
    check(live.get("search_pdb") is None, "配置里关掉的领域工具，装配时真没进来")
    check(
        live.get("search_literature") is not None and live.get("write_run") is not None,
        "没在配置里点名的工具照旧装上",
    )
    check(
        "search_pdb" not in {spec["function"]["name"] for spec in live.specs()},
        "被关的工具不在请求体的 tools 数组里（模型看不到它）",
    )
    check(
        set(DOMAIN_TOOL_NAMES) - set(live.names()) == {"search_pdb"},
        "少的正好是那一个，没有连带别的",
    )
    path.unlink(missing_ok=True)

    # `/tools` 面板写盘的那条路：写出去的必须读得回来，而且只写 false。
    #
    # 单独用一份 fixture，不碰上面那份：上面几条验的是「读」的语义，
    # 这里验的是「面板改完立刻生效」靠不靠得住。
    panel_root = ROOT / ".probe" / "tools_panel_write"
    shutil.rmtree(panel_root, ignore_errors=True)
    written = save_disabled(panel_root, ["bash", "search_pdb"])
    check(
        written == panel_root / CONFIG_DIRECTORY / TOOLS_FILE,
        f"写的是项目侧那份 tools.toml：{written}",
    )
    names, sources = load_tool_switches(panel_root)
    check(
        {"bash", "search_pdb"} <= names,
        f"面板写出去的开关读得回来：{sorted(names)}",
    )
    check(sources.get("bash") == written, f"来源指向刚写的那份：{sources.get('bash')}")
    text = written.read_text(encoding="utf-8")
    check("bash = false" in text, f"关掉的工具写成 false：{text!r}")
    check("= true" not in text, "只写 false——写 true 会让人以为本文件能把它打开")

    # 一条都不关时也要留一份文件：面板把最后一个开关关掉再打开，读到的必须是空清单，
    # 而不是「文件没了所以不知道」。
    save_disabled(panel_root, [])
    names, sources = load_tool_switches(panel_root)
    check("bash" not in sources, "清空之后不再关任何工具")
    check("bash = false" not in written.read_text(encoding="utf-8"), "清空之后文件里没有残留")
    shutil.rmtree(panel_root, ignore_errors=True)

    # 打包之后项目侧配置跟着**可执行文件**走，不跟着启动目录走：换个目录启动、
    # 或把 exe 挪到别处，禁用清单照样读得到。这里直接改 `sys.frozen` 与
    # `sys.executable` 模拟打包环境（不真起 exe）。
    exe_dir = ROOT / ".probe" / "frozen_fixture"
    (exe_dir / CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (exe_dir / CONFIG_DIRECTORY / TOOLS_FILE).write_text(
        "[tools]\nsearch_pdb = false\n", encoding="utf-8"
    )
    frozen, executable = getattr(sys, "frozen", None), sys.executable
    try:
        sys.frozen = True
        sys.executable = str(exe_dir / "opencode-tui.exe")
        check(
            project_config_directory(Path("/nowhere")) == exe_dir / CONFIG_DIRECTORY,
            "打包后配置目录取自可执行文件所在处，与启动目录无关",
        )
        disabled, source = load_disabled(Path("/nowhere"))
        check(
            disabled == frozenset({"search_pdb"}),
            f"换个启动目录仍读得到 exe 旁边的开关：{sorted(disabled)}",
        )
        check(source == exe_dir / CONFIG_DIRECTORY / TOOLS_FILE, f"来源指向 exe 旁那份：{source}")
    finally:
        if frozen is None:
            del sys.frozen
        else:
            sys.frozen = frozen
        sys.executable = executable
        (exe_dir / CONFIG_DIRECTORY / TOOLS_FILE).unlink(missing_ok=True)


# 钩子夹具脚本。测试自己写出来，内容里不能出现用字规则禁用的那些字：
# 它们写在 .probe 下，会被 check_banned_chars.py 一起扫到。
RECORD_SCRIPT = """import json
import sys
from pathlib import Path

data = json.loads(sys.stdin.readline())
Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
if sys.argv[2]:
    print(sys.argv[2], file=sys.stderr)
raise SystemExit(int(sys.argv[3]))
"""

# 只拦一次：标记文件还在就拦下来并把它删掉，之后放行。
# 用来验"拦下来之后这一轮又走一步、补完就收尾"。
ONCE_SCRIPT = """import json
import sys
from pathlib import Path

data = json.loads(sys.stdin.readline())
Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
marker = Path(sys.argv[2])
if marker.is_file():
    marker.unlink()
    print(sys.argv[4], file=sys.stderr)
    raise SystemExit(int(sys.argv[3]))
"""

SLOW_SCRIPT = """import sys
import time

sys.stdin.readline()
time.sleep(30)
"""

# 把用字规则检查接成 Stop 钩子：只扫参数给的那一个文件，命中就按钩子的约定
# 用退出码 2 拦下来，标准错误里放 check_banned_chars.py 的报告。
# 扫全仓库会让断言随工作区里别的目录一起变，这一组断言验的是钩子层，
# 所以范围收在这一个探测文件上。
BANNED_STOP_SCRIPT = """import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".probe"))

import check_banned_chars

sys.stdin.readline()
probe = Path(sys.argv[1])
hits = check_banned_chars.scan(probe.read_text(encoding="utf-8", errors="replace"))
if hits:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        check_banned_chars.report(probe, hits)
    print(buffer.getvalue().strip(), file=sys.stderr)
    raise SystemExit(2)
"""

# 故意写进探测文件的那个禁用字。按码位拼出来：直接写成字面量的话，
# 这个字会出现在本文件里，检查脚本扫到这里就命中了。
BANNED_CHAR = chr(0x843D)


def hook_command(script: str, *args: str) -> str:
    """拼一条钩子命令：用当前解释器跑夹具脚本。

    参数逐个加引号，空的写成一对空引号。命令是交给 cmd.exe 解析的，
    参数里有空格或反斜杠时不加引号会被切开。
    """
    parts = [f'"{sys.executable}"', f'"{HOOK_FIXTURE / script}"']
    parts.extend(f'"{arg}"' if arg else '""' for arg in args)
    return " ".join(parts)


async def check_hook_runner() -> None:
    """钩子的运行方式：标准输入那一行 JSON、退出码约定、matcher、超时。"""
    HOOK_FIXTURE.mkdir(parents=True, exist_ok=True)
    (HOOK_FIXTURE / "record.py").write_text(RECORD_SCRIPT, encoding="utf-8")
    (HOOK_FIXTURE / "slow.py").write_text(SLOW_SCRIPT, encoding="utf-8")
    first = HOOK_FIXTURE / "first.json"
    second = HOOK_FIXTURE / "second.json"
    for path in (first, second):
        path.unlink(missing_ok=True)

    def one(command: str, timeout: float = 30.0) -> HookRunner:
        return HookRunner(
            [Hook(PRE_TOOL_USE, "*", command, "项目配置")], HOOK_FIXTURE, timeout
        )

    report = await one(hook_command("record.py", str(first), "", "0")).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    payload = json.loads(first.read_text(encoding="utf-8"))
    check(payload["event"] == PRE_TOOL_USE, f"标准输入那一行里有事件名：{payload}")
    check(payload["tool"] == "bash", "标准输入那一行里有工具名")
    check(payload["input"] == {"command": "ls"}, "标准输入那一行里有工具参数")
    check(payload["cwd"] == str(HOOK_FIXTURE), "标准输入那一行里有工作目录")
    check(not report.blocked and report.outcomes[0].code == 0, "退出码 0 放行")

    report = await one(hook_command("record.py", str(first), "这里不许动", "2")).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    check(report.blocked, "退出码 2 拦下来")
    check(report.reason == "这里不许动", f"拦下来的原因是标准错误：{report.reason}")

    report = await one(hook_command("record.py", str(first), "", "2")).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    check(
        report.blocked and "没有在标准错误里写明原因" in report.reason,
        f"退出码 2 而标准错误为空时也给出原因：{report.reason}",
    )

    report = await one(hook_command("record.py", str(first), "只写日志", "3")).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    check(not report.blocked, "退出码 3 放行")
    check(
        report.outcomes[0].code == 3 and report.outcomes[0].reason == "只写日志",
        "退出码 3 的标准错误留在结果里，交给日志",
    )

    report = await one(hook_command("slow.py"), timeout=0.5).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    check(report.blocked, "跑不完的钩子按拦下来处理")
    check("超过" in report.reason, f"超时的原因写明是超时：{report.reason}")

    allow = Hook(PRE_TOOL_USE, "*", hook_command("record.py", str(first), "", "0"), "项目配置")
    block = Hook(PRE_TOOL_USE, "*", hook_command("record.py", str(second), "第二条拦下了", "2"), "项目配置")
    report = await HookRunner([allow, block], HOOK_FIXTURE).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    check(report.blocked and report.reason == "第二条拦下了", "第一条放行之后接着跑第二条")
    check(len(report.outcomes) == 2, f"两条的结果都留下来，实际 {len(report.outcomes)} 条")
    first.unlink(missing_ok=True)
    report = await HookRunner([block, allow], HOOK_FIXTURE).run(
        PRE_TOOL_USE, "bash", {"command": "ls"}
    )
    check(report.blocked and report.reason == "第二条拦下了", "拦下来的那一条排在前时结论就是它")
    check(not first.exists(), "第一条拦下来之后后面的钩子不再跑")

    empty = await HookRunner([], HOOK_FIXTURE).run(PRE_TOOL_USE, "bash", {"command": "ls"})
    check(empty.outcomes == () and not empty.blocked, "没有配钩子时结果为空")

    matcher = HookRunner([Hook(PRE_TOOL_USE, "write|patch", "x", "项目配置")], HOOK_FIXTURE)
    check(bool(matcher.for_event(PRE_TOOL_USE, "write")), "matcher 命中 write")
    check(bool(matcher.for_event(PRE_TOOL_USE, "patch")), "matcher 命中 patch")
    check(not matcher.for_event(PRE_TOOL_USE, "bash"), "matcher 没命中的工具不跑")
    check(not matcher.for_event(POST_TOOL_USE, "write"), "事件不同不跑")
    star = HookRunner([Hook(POST_TOOL_USE, "*", "x", "项目配置")], HOOK_FIXTURE)
    check(bool(star.for_event(POST_TOOL_USE, "bash")), "* 管全部工具")
    stop = HookRunner([Hook(STOP, "write", "x", "项目配置")], HOOK_FIXTURE)
    check(bool(stop.for_event(STOP, "")), "Stop 事件忽略 matcher")


def check_hook_config() -> None:
    """钩子配置的读取与写错时的报错。"""
    HOOK_FIXTURE.mkdir(parents=True, exist_ok=True)
    path = HOOK_FIXTURE / "hooks.toml"
    path.unlink(missing_ok=True)
    check(load_hooks(path, "项目配置") == [], "配置不存在时读出空表")

    path.write_text(
        '[[hook]]\nevent = "PostToolUse"\nmatcher = "write|patch"\ncommand = "echo ok"\n',
        encoding="utf-8",
    )
    hooks = load_hooks(path, "项目配置")
    check(len(hooks) == 1, f"读出一条钩子，实际 {len(hooks)} 条")
    check(hooks[0].event == POST_TOOL_USE, "事件读了出来")
    check(hooks[0].matcher == "write|patch", "matcher 读了出来")
    check(hooks[0].command == "echo ok", "命令读了出来")
    check(hooks[0].source == "项目配置", "来源写在钩子上")

    path.write_text('[[hook]]\nevent = "Stop"\ncommand = "echo ok"\n', encoding="utf-8")
    check(load_hooks(path, "项目配置")[0].matcher == "*", "没写 matcher 时按全部算")

    for body, needle in (
        ('[[hook]]\nevent = "StopTool"\ncommand = "echo ok"\n', "StopTool"),
        ('[[hook]]\nevent = "Stop"\n', "command"),
        ('[[hook]]\nevent = "Stop"\ncommand = "echo ok"\nmatcherr = "*"\n', "matcherr"),
        ('hook = "echo ok"\n', "数组"),
    ):
        path.write_text(body, encoding="utf-8")
        raised = ""
        try:
            load_hooks(path, "项目配置")
        except ValueError as error:
            raised = str(error)
        check(needle in raised, f"配置写错时直接报错（{needle}）：{raised}")
    path.unlink(missing_ok=True)


def tool_parts(message: Message) -> list[Part]:
    """一条消息里的工具 Part。"""
    return [part for part in message.parts if part.type is PartType.TOOL]


def stop_notices(session: Session) -> list[str]:
    """会话里 Stop 钩子留下的那几句。"""
    return [
        part.text
        for message in session.messages
        if message.role == "user"
        for part in message.parts
        if part.type is PartType.TEXT and part.text.startswith(STOP_NOTICE)
    ]


async def check_hooks_in_loop(server: FakeModelServer) -> None:
    """钩子接在循环上：三个事件各走一遍。"""
    bus = EventBus()
    recorder = Recorder(bus)
    registry = build_permission_registry()
    WORKSPACE_FIXTURE.mkdir(parents=True, exist_ok=True)
    HOOK_FIXTURE.mkdir(parents=True, exist_ok=True)
    report_path = HOOK_FIXTURE / "loop.json"
    report_path.unlink(missing_ok=True)
    marker = HOOK_FIXTURE / "block_once"
    marker.unlink(missing_ok=True)
    (HOOK_FIXTURE / "record.py").write_text(RECORD_SCRIPT, encoding="utf-8")
    (HOOK_FIXTURE / "once.py").write_text(ONCE_SCRIPT, encoding="utf-8")
    (HOOK_FIXTURE / "banned_stop.py").write_text(BANNED_STOP_SCRIPT, encoding="utf-8")

    def runner(*entries: Hook) -> HookRunner:
        return HookRunner(list(entries), WORKSPACE_FIXTURE)

    def open_policy() -> PermissionPolicy:
        """判定层不拦任何工具：这一组断言验的是钩子。"""
        return PermissionPolicy(default_ask=frozenset())

    # PreToolUse 拦下来：调用根本不执行，回灌给模型的是钩子的标准错误
    server.clear()
    recorder.clear()
    blocked = build_loop(
        server,
        bus,
        registry,
        policy=PermissionPolicy(),
        hooks=runner(
            Hook(PRE_TOOL_USE, "bash", hook_command("record.py", str(report_path), "这一条命令不许跑", "2"), "项目配置")
        ),
    )
    session = Session(id="ses_hook_pre", title="钩子拦在前面")
    recorder.session = session
    ask(session, "跑一条命令")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="bash",
                            arguments=json.dumps({"command": "echo hook_should_not_run"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("那就不跑", size=2), usage_chunk(1, 1)]),
    )
    message = await blocked.stream_reply(session, "")
    tools = tool_parts(message)
    check(tools[0].tool_status is ToolStatus.FAILED, "PreToolUse 拦下之后工具标成失败")
    check(
        tools[0].tool_output == "钩子拦下了这次调用：这一条命令不许跑",
        f"回灌给模型的是钩子的标准错误：{tools[0].tool_output}",
    )
    check("hook_should_not_run" not in tools[0].tool_output, "被拦下的命令没有执行")
    check(not recorder.names("permission.ask"), "钩子排在判定之前，用户不会被问一遍")
    hook_events = recorder.names("hook.run")
    check(len(hook_events) == 1, f"钩子的结果写进事件日志，实际 {len(hook_events)} 条")
    check(
        hook_events[0]["event"] == PRE_TOOL_USE and hook_events[0]["blocked"] is True,
        "日志里写明是哪个事件、拦没拦下",
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    check(payload["cwd"] == str(WORKSPACE_FIXTURE), "钩子的工作基准是项目目录")

    # PostToolUse 拦下来：命令照跑，标准错误接在结果后面，工具标成失败
    server.clear()
    recorder.clear()
    after = build_loop(
        server,
        bus,
        registry,
        policy=open_policy(),
        hooks=runner(
            Hook(POST_TOOL_USE, "bash", hook_command("record.py", str(report_path), "改完之后检查没过", "2"), "项目配置")
        ),
    )
    session = Session(id="ses_hook_post", title="钩子拦在后面")
    recorder.session = session
    ask(session, "跑一条会留下痕迹的命令")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="bash",
                            arguments=json.dumps({"command": "echo hook_ran"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("跑完了", size=2), usage_chunk(1, 1)]),
    )
    message = await after.stream_reply(session, "")
    tools = tool_parts(message)
    check("hook_ran" in tools[0].tool_output, "PostToolUse 拦下时命令已经跑过了")
    check(
        tools[0].tool_output.endswith(f"{TOOL_HOOK_NOTICE}改完之后检查没过"),
        f"标准错误接在工具结果后面：{tools[0].tool_output}",
    )
    check(tools[0].tool_status is ToolStatus.FAILED, "PostToolUse 拦下之后工具标成失败")

    # Stop 拦下来：这一轮不结束，原因是发给模型的下一句输入；补完之后照常收尾
    server.clear()
    recorder.clear()
    marker.write_text("拦一次", encoding="utf-8")
    stopping = build_loop(
        server,
        bus,
        registry,
        policy=open_policy(),
        hooks=runner(
            Hook(
                STOP,
                "*",
                hook_command("once.py", str(report_path), str(marker), "2", "收尾还差一份报告"),
                "项目配置",
            )
        ),
    )
    session = Session(id="ses_hook_stop", title="收尾拦下来")
    recorder.session = session
    ask(session, "把活干完")
    server.script(
        Reply(chunks=[*text_chunks("干完了", size=2), usage_chunk(1, 1)]),
        Reply(chunks=[*text_chunks("再补一份", size=2), usage_chunk(1, 1)]),
    )
    message = await stopping.stream_reply(session, "")
    check(
        len(server.requests) == 2,
        f"Stop 拦下来之后这一轮又走了一步，实际发了 {len(server.requests)} 次请求",
    )
    notices = stop_notices(session)
    check(len(notices) == 1, f"原因写进会话一条，实际 {len(notices)} 条")
    check(
        notices[0] == f"{STOP_NOTICE}收尾还差一份报告",
        f"写进去的是收尾检查没有通过：{notices[0]}",
    )
    sent = json.dumps(server.requests[-1]["messages"], ensure_ascii=False)
    check(f"{STOP_NOTICE}收尾还差一份报告" in sent, "原因是作为用户消息发给模型的")
    check(message is session.messages[-1], "收尾收在最后那一条助手消息上")
    check(bool(recorder.names("turn.done")), "补完之后照常收尾")
    check(not marker.exists(), "钩子只拦了第一次")

    # 一直拦：最多多走 MAX_STOP_RETRIES 步，之后照常结束，不会一直转下去
    server.clear()
    recorder.clear()
    always = build_loop(
        server,
        bus,
        registry,
        policy=open_policy(),
        hooks=runner(Hook(STOP, "*", hook_command("record.py", str(report_path), "永远不合格", "2"), "项目配置")),
    )
    session = Session(id="ses_hook_always", title="一直拦")
    recorder.session = session
    ask(session, "干不完的活")
    for _ in range(MAX_STOP_RETRIES + 2):
        server.script(Reply(chunks=[*text_chunks("干完了", size=2), usage_chunk(1, 1)]))
    await always.stream_reply(session, "")
    check(
        len(server.requests) == MAX_STOP_RETRIES + 1,
        f"一直拦也最多多走 {MAX_STOP_RETRIES} 步，实际发了 {len(server.requests)} 次请求",
    )
    check(
        len(stop_notices(session)) == MAX_STOP_RETRIES + 1,
        f"每一次都记下原因，实际 {len(stop_notices(session))} 条",
    )
    check(bool(recorder.names("turn.done")), "用满重试次数之后照常收尾")

    # 用字规则这一段接的是本机的判据脚本 `.probe/check_banned_chars.py`——它被
    # gitignore，不在仓库里，干净 checkout 上从来就没有。缺了它整段跳过并**明说**：
    # 「Stop 钩子能把一轮拦下来」这件事上面用 once.py 已经验过，这里只是把本地
    # 那把尺子接上去再走一遍；不能因为本机没放这个文件就让整个套件红着。
    if not (ROOT / ".probe" / "check_banned_chars.py").is_file():
        print("  （跳过用字规则那一段：本机没有 .probe/check_banned_chars.py）")
        return

    # 用字规则检查接成 Stop 钩子：故意写一个禁用字，这一轮不结束并给出原因
    probe = ROOT / ".probe" / "banned_probe.md"
    server.clear()
    recorder.clear()
    guard = build_loop(
        server,
        bus,
        registry,
        policy=open_policy(),
        hooks=runner(Hook(STOP, "*", hook_command("banned_stop.py", str(probe)), "项目配置")),
    )
    session = Session(id="ses_hook_banned", title="用字规则")
    recorder.session = session
    ask(session, "写点东西")
    try:
        probe.write_text(f"这一行里有一个{BANNED_CHAR}字\n", encoding="utf-8")
        server.script(
            Reply(chunks=[*text_chunks("写好了", size=2), usage_chunk(1, 1)]),
            Reply(chunks=[*text_chunks("改好了", size=2), usage_chunk(1, 1)]),
        )
        await guard.stream_reply(session, "")
        check(
            len(server.requests) == MAX_STOP_RETRIES + 1,
            f"检查没过时这一轮不结束，一路补到重试上限，实际发了 {len(server.requests)} 次请求",
        )
        notices = stop_notices(session)
        check(
            len(notices) == MAX_STOP_RETRIES + 1,
            f"每一次没过都给出原因，实际 {len(notices)} 条",
        )
        check(
            "banned_probe.md" in notices[0] and "命中" in notices[0],
            f"原因里写着是哪个文件的哪一行：{notices[0][:160]}",
        )
        check(BANNED_CHAR in notices[0], "原因里带着命中的那个字")

        server.clear()
        recorder.clear()
        probe.write_text("这一行全是干净的文字\n", encoding="utf-8")
        ask(session, "再写一次")
        server.script(Reply(chunks=[*text_chunks("这次没问题", size=2), usage_chunk(1, 1)]))
        await guard.stream_reply(session, "")
        check(len(server.requests) == 1, "改回来之后一次请求就收尾")
        check(bool(recorder.names("turn.done")), "改回来之后正常结束这一轮")
    finally:
        probe.unlink(missing_ok=True)


def check_workspace() -> None:
    """会话工作目录与它写进系统提示的那一段。"""
    session = Session(id="ses_ws", title="受体 A")
    workspace = Workspace(session.id, root=WORKSPACE_FIXTURE / session.id)
    text = workspace.state_text(session)
    check("受体 A" in text, "状态描述里有会话标题")
    check(str(workspace.root) in text, "状态描述里有工作目录")
    check("当前时间：" in text, "状态描述里有当前时间")
    check(workspace.ensure().exists(), "ensure() 建出工作目录")
    check(
        ".opencode_tui" in str(Workspace("ses_x").root) and "workspaces" in str(Workspace("ses_x").root),
        "默认根在 ~/.opencode_tui/workspaces 下",
    )


def check_prompt() -> None:
    """Session 到模型消息的翻译，以及上下文预算。"""
    session = Session(id="ses_prompt", title="筛选")
    workspace = Workspace(session.id, root=WORKSPACE_FIXTURE / session.id)
    user = Message(role="user", session_id=session.id)
    user.add_part(Part(type=PartType.TEXT, text="帮我看一下这批分子"))
    user.add_part(Part(type=PartType.FILE, file_path="candidates.csv", text="smiles\nCCO\n"))
    session.add_message(user)

    assistant = Message(role="assistant", session_id=session.id)
    assistant.add_part(Part(type=PartType.STEP_START, text="1 · fake-model"))
    assistant.add_part(Part(type=PartType.REASONING, text="先读文件"))
    assistant.add_part(Part(type=PartType.TEXT, text="我先读一下文件。"))
    tool = assistant.add_part(
        Part(
            type=PartType.TOOL,
            tool_name="read",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"filePath": "candidates.csv"},
            tool_output="2 行",
        )
    )
    assistant.add_part(Part(type=PartType.STEP_START, text="2 · fake-model"))
    assistant.add_part(Part(type=PartType.REASONING, text="读到两行，都是候选分子。"))
    assistant.add_part(Part(type=PartType.TEXT, text="文件里有两个分子。"))
    assistant.add_part(Part(type=PartType.STEP_FINISH, text="2 · 12 tok · 30 ms"))
    session.add_message(assistant)

    system_prompt = build_system_prompt(workspace, session)
    prefix = stable_prefix(workspace, session)
    check(
        "引用它们一律用 handle，不要写文件路径" in system_prompt,
        "系统提示里写着「收 handle 不收路径」那条纪律",
    )
    check(
        "import_structure" in system_prompt,
        "并点明全库只有 import_structure 一个动作收路径（规则要能指到具体名字上）",
    )
    check(
        "list_workspace" in system_prompt and "describe_entry" in system_prompt,
        "handle 从哪儿取、详情在哪儿查，也写在同一段里",
    )
    # 现场来的三条：模型写 pwd、写 cp、把上一条命令的 cd 当基准。它只能从系统提示里知道
    # 「每次调用是一个新解释器」「这里是 Windows」，否则只能靠一次次失败去猜。
    check(
        "每一次" in system_prompt or "每调用一次" in system_prompt,
        "系统提示里写着每次 bash 调用都是全新的解释器",
    )
    check(
        "cd" in system_prompt and "&&" in system_prompt,
        "换目录要写在同一条命令里，提示里给了写法（cd ... && ...）",
    )
    check(
        "Windows" in system_prompt and "pwd" in system_prompt,
        "提示里点明这是 Windows，并点名不要用 pwd",
    )
    built = ContextAssembler(context_window=200_000).build(session, prefix)
    roles = [item["role"] for item in built.messages]
    check(roles == ["system", "user", "assistant", "tool", "assistant"], f"按角色翻译成消息序列：{roles}")
    check(built.dropped == 0, "窗口足够大时一条都不丢")

    user_item = built.messages[1]
    check("帮我看一下这批分子" in user_item["content"], "用户正文进 user 消息")
    check('<file path="candidates.csv">' in user_item["content"], "引用文件拼进同一条 user 消息")

    call_item = built.messages[2]
    check(call_item["content"] == "我先读一下文件。", "工具调用之前的正文进 assistant 的 content")
    check(call_item["reasoning_content"] == "先读文件", "带工具调用的那一条把同一 step 的推理一并带上")
    check(call_item["tool_calls"][0]["id"] == tool.id, "工具调用的 id 用的是 Part 的 id")
    check(call_item["tool_calls"][0]["function"]["name"] == "read", "工具名进了 tool_calls")
    check(
        json.loads(call_item["tool_calls"][0]["function"]["arguments"]) == {"filePath": "candidates.csv"},
        "工具参数序列化成 JSON 字符串",
    )
    check(built.messages[3]["tool_call_id"] == tool.id, "工具结果与调用配对")
    check(built.messages[4]["content"] == "文件里有两个分子。", "下一个 step 的正文另起一条 assistant 消息")
    check(built.messages[4]["reasoning_content"] == "读到两行，都是候选分子。", "没有工具调用的最终回答也把推理一并带上")

    flattened = json.dumps(built.messages, ensure_ascii=False)
    check("fake-model" not in flattened and "30 ms" not in flattened, "step 类 Part 不发给模型")

    plain = Message(role="assistant", session_id=session.id)
    plain.add_part(Part(type=PartType.TEXT, text="就一句话，没有思考。"))
    session.add_message(plain)
    built = ContextAssembler(context_window=200_000).build(session, prefix)
    plain_item = next(item for item in built.messages if item.get("content") == "就一句话，没有思考。")
    check(
        plain_item.get("reasoning_content") == EMPTY_REASONING,
        f"没有推理的正文也回传 reasoning 字段（占位）：{plain_item.get('reasoning_content')!r}",
    )

    bracket = Message(role="assistant", session_id=session.id)
    bracket.add_part(Part(type=PartType.STEP_START, text="1 · fake"))
    bare = Part(
        type=PartType.TOOL,
        tool_name="bash",
        tool_status=ToolStatus.COMPLETED,
        tool_input={"command": "ls"},
        tool_output="a\nb",
    )
    bracket.add_part(bare)
    session.add_message(bracket)
    built = ContextAssembler(context_window=200_000).build(session, prefix)
    bare_item = next(
        item
        for item in built.messages
        if item["role"] == "assistant" and item.get("tool_calls")
        and item["tool_calls"][0]["id"] == bare.id
    )
    check(
        bare_item.get("reasoning_content") == EMPTY_REASONING,
        f"只有工具调用、没有推理的那一步也回传占位：{bare_item.get('reasoning_content')!r}",
    )

    absent = [
        index
        for index, item in enumerate(built.messages)
        if item["role"] == "assistant" and "reasoning_content" not in item
    ]
    check(
        not absent,
        f"凡发给模型的 assistant 项都带 reasoning_content（缺字段的那条会被服务端退回）：{absent}",
    )

    pending = Message(role="assistant", session_id=session.id)
    pending.add_part(
        Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=ToolStatus.RUNNING,
            tool_input={"command": "sleep 1"},
        )
    )
    session.add_message(pending)
    built = ContextAssembler(context_window=200_000).build(session, prefix)
    check(len(built.messages) == 8, "还停在 pending 或 running 的工具不发给模型")

    long_session = Session(id="ses_long", title="长会话")
    for index in range(40):
        item = Message(role="user" if index % 2 == 0 else "assistant", session_id=long_session.id)
        item.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条消息，" + "内" * 400))
        long_session.add_message(item)
    tight = ContextAssembler(context_window=24_000)
    built = tight.build(long_session, prefix)
    check(built.dropped > 0, f"超长历史开始移出更早的交互组：移出 {built.dropped} 组")
    check(
        sum(built.usage.values()) <= tight.available(),
        f"装配后的估算值在可用额度以内：{sum(built.usage.values())} <= {tight.available()}",
    )
    check(
        len(built.messages) == 2 + (40 - built.dropped),
        "留在消息列表里的条数与被移出的组数相加正好是原来的条数",
    )
    check(built.dropped_upto == built.dropped, "被移出的内容正好覆盖开头那一段")
    kept_text = json.dumps([item["content"] for item in built.messages[2:]], ensure_ascii=False)
    check("第 39 条消息" in kept_text, "最后一条消息永远留着")
    check("第 0 条消息" not in kept_text, "丢掉的是开头那一段")
    check(
        f"- turn {built.dropped_turns[0]}：" in built.messages[1]["content"],
        "被移出的交互组在引用清单里各占一行",
    )
    try:
        ContextAssembler(context_window=2_000).build(long_session, prefix)
    except ContextAssemblyError:
        check(True, "窗口装不下时明确报错，不静默截断")
    else:
        check(False, "窗口装不下时明确报错，不静默截断")

    long_output = "甲" * 3000 + "乙" * 2000 + "丙" * 3000
    truncated = truncate_for_model(long_output)
    check(len(truncated) < len(long_output), "超长输出被裁短")
    check(truncated.startswith("甲" * 2000), "留住了开头 2000 个字符")
    check(truncated.endswith("丙" * 2000), "留住了结尾 2000 个字符")
    check("… 省略 4000 字符" in truncated, "中间写明省掉了多少字符")
    check(truncate_for_model("短输出") == "短输出", "不超限的输出原样返回")

    # 常驻记忆（MEMORY 层）：紧接稳定前缀之后单独成一条，且计入预算。
    memory_text = "- 受体结构在 receptor.pdb（receptor.pdb）"
    with_memory = ContextAssembler(context_window=200_000).build(
        session, prefix, memory_text=memory_text
    )
    check(
        with_memory.messages[1]["role"] == "user"
        and MEMORY_NOTICE in with_memory.messages[1]["content"]
        and memory_text in with_memory.messages[1]["content"],
        "常驻记忆紧接稳定前缀之后单独成一条，带一行说明",
    )
    check(
        with_memory.messages[0]["role"] == "system"
        and with_memory.messages[2]["role"] == "user",
        "记忆没有把稳定前缀与随后保留的消息挤掉",
    )
    without_memory = ContextAssembler(context_window=200_000).build(session, prefix)
    check(
        len(with_memory.messages) == len(without_memory.messages) + 1,
        "给了记忆就多一条消息，不给就不多",
    )

    # 记忆参与预算：窗口收紧时它只让移出变多，不会变少；并且确实存在某个窗口，
    # 加进一段记忆之后开始多移出——这才证明它算进了 token 预算，而不是白送的。
    memory_heavy = "- 受体与配体的事实" * 60
    monotone = True
    forced = False
    for window in range(20_000, 28_000, 250):
        plain_build = ContextAssembler(context_window=window).build(long_session, prefix)
        loaded_build = ContextAssembler(context_window=window).build(
            long_session, prefix, memory_text=memory_heavy
        )
        if loaded_build.dropped < plain_build.dropped:
            monotone = False
        if loaded_build.dropped > plain_build.dropped:
            forced = True
    check(monotone, "记忆只让移出变多，不会变少")
    check(forced, "把记忆塞进小窗口会真的多移出几组（证明记忆计入预算）")


def delayed_command(seconds: float, marker: Path) -> str:
    """造一条"睡一会儿再写个文件"的命令，用来观察它有没有被真的终止。"""
    code = f"import time;time.sleep({seconds});open(r'{marker}','w').write('x')"
    return f'"{sys.executable}" -c "{code}"'


async def check_bash_tool() -> None:
    """bash 工具：输出、退出码、超时、被取消时连子进程一起终止。"""
    WORKSPACE_FIXTURE.mkdir(parents=True, exist_ok=True)
    tool = BashTool(cwd=WORKSPACE_FIXTURE, timeout=30)
    result = await tool.run({"command": "echo hi"})
    check(result.output == "hi" and not result.failed, f"普通命令的输出作为结果返回：{result.output!r}")
    check(result.failed is False, "退出码为零时不算失败")

    empty = await tool.run({"command": "definitely-not-a-command-xyz"})
    check(empty.failed, "非零退出码置为失败")
    check(empty.output.strip() != "", "失败时也把输出带回来")

    # 编码：系统代码页是 936 时，带中文的 Windows 命令吐的是 GBK 字节。
    # 现场是 `certutil -hashfile` 的「哈希:」「命令成功完成。」被解成 U+FFFD。
    check(decode_output("中文测试".encode("utf-8")) == "中文测试", "UTF-8 字节按 UTF-8 解")
    check(decode_output("中文测试".encode("gbk")) == "中文测试", "GBK 字节按 GBK 解")
    check(decode_output(b"plain ascii") == "plain ascii", "纯 ASCII 两种编码下等价")
    check(
        "\ufffd" not in decode_output("命令成功完成。".encode("gbk")),
        f"GBK 的中文不能留下替换字符：{decode_output('命令成功完成。'.encode('gbk'))!r}",
    )
    check(
        decode_output(b"\x00\x01\xff\xfe") != "",
        "两种编码都解不通时退回替换字符，而不是抛异常把整条输出抹掉",
    )
    # 文件那一侧的解码（`decode_text`）：先认字节序标记，再按同一串编码严格试，
    # 全试不通才判成不是文本。与 `decode_output` 的差别只有两处：认标记、能返回 None。
    check(decode_text("甲".encode("utf-8")) == "甲", "decode_text 认 UTF-8")
    check(decode_text("甲".encode("gbk")) == "甲", "decode_text 认 GBK")
    check(decode_text("甲\n乙".encode("utf-16")) == "甲\n乙", "decode_text 认带标记的 UTF-16")
    check(decode_text("甲".encode("utf-8-sig")) == "甲", "decode_text 认带标记的 UTF-8")
    check(decode_text(b"\x7fELF\x00\x00\x00\x00") is None, "夹着 NUL 的二进制不当文本")
    check(decode_text(bytes(range(256))) is None, "随机字节不当文本")
    # 换行归一：`Path.read_text` 默认做通用换行翻译，改用 `read_bytes` 之后要自己补回来。
    # 少了这一条，Windows 上 `write_text` 写出的 `\r\n` 会留下 `\r`，行尾判断全部失配。
    check(
        decode_text("甲\r\n乙\r\n".encode("utf-8")) == "甲\n乙\n",
        "decode_text 把 CRLF 归一成 LF",
    )
    check(
        decode_text("甲\r乙".encode("utf-16")) == "甲\n乙",
        "UTF-16 里的换行同样归一",
    )
    # 真机取一条会吐中文的命令，读它实际回来的字节，逐字检查没有替换字符。
    # 编码对不对不能只靠自造的字节：这里跑的是真命令，回来的是真代码页。
    #
    # Windows 专门用 certutil：它是让「代码页 936 → GBK 字节 → 解成 U+FFFD」
    # 这条真实故障路径走一遍的现成命令。POSIX 没有代码页这回事（默认 UTF-8），
    # 那一段换成 echo，验的是「真命令的中文原样回来」，不假装在验 GBK。
    if os.name == "nt":
        certutil = await tool.run(
            {"command": f'certutil -hashfile "{ROOT / "requirements.txt"}" SHA256'}
        )
        check(
            "\ufffd" not in certutil.output,
            f"certutil 的中文提示不能被解坏：{certutil.output[-90:]!r}",
        )
        check(
            "CertUtil" in certutil.output
            and ("成功完成" in certutil.output or "哈希" in certutil.output),
            f"certutil 的中文提示要还原成原字：{certutil.output[-90:]!r}",
        )
    else:
        echoed = await tool.run({"command": "echo 中文测试"})
        check(
            echoed.output == "中文测试",
            f"真命令输出的中文原样回来：{echoed.output!r}",
        )

    raised = ""
    try:
        await tool.run({})
    except ToolError as error:
        raised = str(error)
    check(raised == "缺少参数 command", "缺少 command 参数时抛 ToolError")

    blank = ""
    try:
        await tool.run({"command": "   "})
    except ToolError as error:
        blank = str(error)
    check(blank == "缺少参数 command", "只有空白字符的命令也抛 ToolError")

    short = BashTool(cwd=WORKSPACE_FIXTURE, timeout=0.5)
    timed = ""
    try:
        await short.run({"command": delayed_command(5, WORKSPACE_FIXTURE / "never.txt")})
    except ToolError as error:
        timed = str(error)
    check("已经终止" in timed, f"超时之后命令被终止：{timed}")
    check(
        "分小段" in timed or "后台" in timed,
        f"超时文案告诉模型接下来怎么办，不然它会原样重发：{timed}",
    )
    check(
        BashTool(cwd=WORKSPACE_FIXTURE).timeout == BASH_TIMEOUT,
        f"bash 的默认等待取自 BASH_TIMEOUT（与模型请求的 DEFAULT_TIMEOUT 分开）："
        f"{BashTool(cwd=WORKSPACE_FIXTURE).timeout}",
    )
    check(
        BASH_TIMEOUT > 120.0,
        f"bash 的等待比两分钟宽松，对接与装包才跑得完：{BASH_TIMEOUT}",
    )

    marker = WORKSPACE_FIXTURE / "cancelled_marker.txt"
    marker.unlink(missing_ok=True)
    running = asyncio.create_task(tool.run({"command": delayed_command(2, marker)}))
    await asyncio.sleep(0.5)
    running.cancel()
    cancelled = False
    try:
        await running
    except asyncio.CancelledError:
        cancelled = True
    check(cancelled, "取消协程时 CancelledError 照常抛出来")
    await asyncio.sleep(2.5)
    check(not marker.exists(), "子进程被一起终止了，它后面那一步没有发生")


async def check_provider(server: FakeModelServer) -> None:
    """provider 对本地服务的解析：增量、工具调用、用量、错误分类。"""
    config = ProviderConfig(base_url=server.base_url, api_key="secret", model="fake-model")
    provider = OpenAICompatProvider(config)
    tools = [
        {
            "type": "function",
            "function": {"name": "bash", "description": "跑命令", "parameters": {"type": "object"}},
        }
    ]
    fragments = argument_fragments({"command": "ls -la"}, pieces=3)
    check(len(fragments) == 3, f"参数被切成三段：{fragments}")

    server.clear()
    server.script(
        Reply(
            chunks=[
                *text_chunks("你好世界", size=2),
                *reasoning_chunks("先想一下", size=2),
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(0, call_id="call_1", name="bash", arguments=fragments[0])
                    ]
                ),
                delta_chunk(tool_calls=[tool_call_delta(0, arguments=fragments[1])]),
                delta_chunk(tool_calls=[tool_call_delta(0, arguments=fragments[2])]),
                delta_chunk(tool_calls=[tool_call_delta(1, call_id="call_2", name="echo")]),
                delta_chunk(finish_reason="tool_calls"),
                usage_chunk(11, 22),
            ]
        )
    )
    events = [
        event async for event in provider.stream([{"role": "user", "content": "你好"}], tools)
    ]
    texts = [event.text for event in events if event.type is ProviderEventType.TEXT_DELTA]
    reasoning = [event.text for event in events if event.type is ProviderEventType.REASONING_DELTA]
    check("".join(texts) == "你好世界", "文本增量按序拼回原文")
    check(texts == ["你好", "世界"], f"文本按分片逐条产出：{texts}")
    check("".join(reasoning) == "先想一下", "推理增量按序拼回原文")

    calls = [event for event in events if event.type is ProviderEventType.TOOL_CALL]
    check(len(calls) == 2, "一次响应里的两个工具调用都发了出来")
    check(
        [call.tool_name for call in calls] == ["bash", "echo"],
        "工具调用按 index 排序",
    )
    check(calls[0].tool_args == {"command": "ls -la"}, "横跨三段的参数拼成一个完整的 JSON 对象")
    check(calls[1].tool_args == {}, "没有参数的那一个给出空字典")
    check(calls[0].tool_call_id == "call_1", "工具调用的 id 带了出来")

    usage = [event for event in events if event.type is ProviderEventType.USAGE]
    check(len(usage) == 1, "用量只发一次")
    check(
        usage[0].usage == {"input": 11, "output": 22, "cached": 0, "uncached": 11},
        "用量归一成输入输出的总数与缓存两类",
    )
    check(events[-1].type is ProviderEventType.USAGE, "用量事件排在末尾")

    server.clear()
    server.script(
        Reply(
            chunks=[
                *text_chunks("好"),
                delta_chunk(finish_reason="stop"),
                usage_chunk(100, 20, cached_tokens=80),
            ]
        )
    )
    cached_events = [event async for event in provider.stream([{"role": "user", "content": "hi"}], tools)]
    cached_usage = [event for event in cached_events if event.type is ProviderEventType.USAGE]
    check(
        cached_usage[0].usage == {"input": 100, "output": 20, "cached": 80, "uncached": 20},
        "缓存命中的输入单独计数",
    )

    recorded = server.requests[-1]
    check(recorded["stream"] is True, "请求体打开了流式")
    check(recorded["stream_options"] == {"include_usage": True}, "请求体要求返回用量")
    check(recorded["tools"] == tools, "工具清单原样进请求体")
    check(recorded["model"] == "fake-model", "请求体带上了模型名")

    server.clear()
    server.script(Reply(status=429, body='{"error": "请求太快"}'))
    throttled = ""
    try:
        async for _ in provider.stream([{"role": "user", "content": "hi"}], tools):
            pass
    except ProviderError as error:
        throttled = f"{error.status_code} {error.retryable} {error}"
    check(
        throttled.startswith("429 True"),
        f"429 抛出的错误带状态码且可以重试：{throttled}",
    )

    server.clear()
    server.script(Reply(status=401, body='{"error": "密钥不对"}'))
    rejected = ""
    try:
        async for _ in provider.stream([{"role": "user", "content": "hi"}], tools):
            pass
    except ProviderError as error:
        rejected = f"{error.status_code} {error.retryable}"
    check(rejected == "401 False", "401 抛出的错误不可以重试")

    server.clear()
    server.script(Reply(chunks=text_chunks("断在这里"), disconnect_after=1))
    partial = ""
    broken = ""
    try:
        async for event in provider.stream([{"role": "user", "content": "hi"}], tools):
            if event.type is ProviderEventType.TEXT_DELTA:
                partial += event.text
    except ProviderError as error:
        broken = f"{error.retryable} {error}"
    check(partial == "断在这里", "连接断开之前已经收到的增量照常产出")
    check(broken.startswith("True"), f"流中途断开抛出可以重试的错误：{broken}")


def build_loop(
    server: FakeModelServer,
    bus: EventBus,
    registry: ToolRegistry,
    context_window: int = 200_000,
    price_in: float = 0.0,
    price_out: float = 0.0,
    supports_reasoning: bool = True,
    policy: PermissionPolicy | None = None,
    hooks: HookRunner | None = None,
) -> AgentLoop:
    """按本地服务装配一个循环层。

    判定层默认不拦任何工具：这一组断言验的是循环本身，
    判定那一条链由 `check_permission` 单独验。钩子默认一条都没有，
    要验钩子的那几处自己传一份进来。
    """
    config = ProviderConfig(
        base_url=server.base_url,
        api_key="secret",
        model="fake-model",
        price_in=price_in,
        price_out=price_out,
        supports_reasoning=supports_reasoning,
    )
    return AgentLoop(
        bus,
        OpenAICompatProvider(config),
        registry,
        config,
        context_window=context_window,
        workspace_root=WORKSPACE_FIXTURE,
        policy=policy or PermissionPolicy(default_ask=frozenset()),
        hooks=hooks,
    )


def ask(session: Session, text: str) -> None:
    """按界面的做法先把用户输入写进会话，再让循环层应答。"""
    message = session.add_message(Message(role="user", session_id=session.id))
    message.add_part(Part(type=PartType.TEXT, text=text))


async def check_loop(server: FakeModelServer) -> None:
    """循环层一轮的执行过程：Part 与事件、工具、中断、重试、记账。"""
    WORKSPACE_FIXTURE.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    recorder = Recorder(bus)
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(FailTool())
    registry.register(BashTool(cwd=WORKSPACE_FIXTURE, timeout=30))
    loop = build_loop(server, bus, registry)

    server.clear()
    recorder.clear()
    server.script(
        Reply(
            chunks=[
                *reasoning_chunks("先想一下", size=2),
                *text_chunks("这是答案", size=2),
                delta_chunk(finish_reason="stop"),
                usage_chunk(10, 20),
            ]
        )
    )
    session = Session(id="ses_answer", title="问答")
    recorder.session = session
    ask(session, "你好")
    message = await loop.stream_reply(session, "你好")
    assert message is not None
    kinds = [part.type for part in message.parts]
    check(
        kinds
        == [PartType.STEP_START, PartType.REASONING, PartType.TEXT, PartType.STEP_FINISH],
        f"一轮的 Part 顺序：{[kind.value for kind in kinds]}",
    )
    check(message.parts[0].text == "1 · fake-model", f"STEP_START 写明第几步与模型名：{message.parts[0].text}")
    check(message.parts[2].text == "这是答案", "文本增量拼成正文")
    check("30 tok" in message.parts[-1].text, f"STEP_FINISH 写明用量：{message.parts[-1].text}")
    check(
        message.tokens == {"input": 10, "output": 20, "cached": 0, "uncached": 10},
        "用量记在这一轮的消息上",
    )
    check(len(session.messages) == 2 and session.messages[-1] is message, "一轮只产生一条 assistant 消息")
    check(len(recorder.names("turn.done")) == 1, "一轮结束发一次 turn.done")
    check(recorder.names("message.add")[0]["message_id"] == message.id, "先建消息再通知界面")
    check(message.cost == 0.0, "单价为零时费用为零")

    priced = build_loop(server, bus, registry, price_in=1.0, price_out=2.0)
    server.clear()
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(10, 20)]))
    session_price = Session(id="ses_price", title="计价")
    recorder.session = session_price
    ask(session_price, "你好")
    priced_message = await priced.stream_reply(session_price, "")
    assert priced_message is not None
    check(abs(priced_message.cost - 0.00005) < 1e-9, f"成本按每百万 token 的单价算：{priced_message.cost}")

    fragments = argument_fragments({"text": "第一段"}, pieces=3)
    server.clear()
    recorder.clear()
    server.script(
        Reply(
            chunks=[
                *reasoning_chunks("我得先看看", size=3),
                *text_chunks("我先看一眼。", size=3),
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(0, call_id="c1", name="echo", arguments=fragments[0])
                    ]
                ),
                delta_chunk(tool_calls=[tool_call_delta(0, arguments=fragments[1])]),
                delta_chunk(tool_calls=[tool_call_delta(0, arguments=fragments[2])]),
                delta_chunk(finish_reason="tool_calls"),
                usage_chunk(10, 5),
            ]
        ),
        Reply(
            chunks=[
                *text_chunks("看完了。", size=3),
                delta_chunk(finish_reason="stop"),
                usage_chunk(20, 7),
            ]
        ),
    )
    session_tool = Session(id="ses_tool", title="调用工具")
    recorder.session = session_tool
    ask(session_tool, "看看")
    message = await loop.stream_reply(session_tool, "")
    assert message is not None
    steps = [part for part in message.parts if part.type is PartType.STEP_START]
    check(len(steps) == 2, f"工具执行完再问一次模型，step 数为 {len(steps)}")
    tools = [part for part in message.parts if part.type is PartType.TOOL]
    check(len(tools) == 1, "这一轮只有一个工具调用")
    check(tools[0].tool_input == {"text": "第一段"}, "横跨三段的参数拼成一个完整的 JSON 对象")
    check(tools[0].tool_output == "第一段", "工具的输出写在 Part 上")
    check(
        [part.text for part in message.parts if part.type is PartType.TEXT]
        == ["我先看一眼。", "看完了。"],
        "两次响应的正文各成一段，都挂在同一条消息上",
    )
    check(
        message.tokens == {"input": 30, "output": 12, "cached": 0, "uncached": 30},
        "两次发问的用量累加",
    )
    check(
        [(kind, status) for kind, _, status in recorder.tool_states]
        == [
            ("part.add", ToolStatus.PENDING),
            ("part.update", ToolStatus.RUNNING),
            ("part.update", ToolStatus.COMPLETED),
        ],
        "工具状态按 PENDING → RUNNING → COMPLETED 推进",
    )
    second_request = server.requests[-1]["messages"]
    check(second_request[0]["role"] == "system", "每一次发问都带上系统提示")
    check(second_request[-1]["role"] == "tool", "工具结果作为一条 tool 消息回灌")
    check(second_request[-1]["content"] == "第一段", "回灌的内容就是工具输出")
    check(
        second_request[-2]["role"] == "assistant"
        and second_request[-2]["tool_calls"][0]["id"] == tools[0].id,
        "回灌的上一句是带着 tool_calls 的 assistant 消息",
    )
    check(
        second_request[-2].get("reasoning_content") == "我得先看看",
        f"这条消息把同一 step 的推理一并回灌：{second_request[-2].get('reasoning_content')}",
    )

    server.clear()
    recorder.clear()
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0, call_id="c1", name="echo", arguments=json.dumps({"text": "第一个"})
                        )
                    ]
                ),
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            1, call_id="c2", name="echo", arguments=json.dumps({"text": "第二个"})
                        )
                    ]
                ),
                usage_chunk(5, 5),
            ]
        ),
        Reply(chunks=[*text_chunks("都做完了", size=3), usage_chunk(5, 5)]),
    )
    session_two = Session(id="ses_two", title="两个工具")
    recorder.session = session_two
    ask(session_two, "两个都做一下")
    message = await loop.stream_reply(session_two, "")
    assert message is not None
    tools = [part for part in message.parts if part.type is PartType.TOOL]
    check(len(tools) == 2, "一次响应里的两个工具调用都建了 Part")
    check(
        [part.tool_input for part in tools] == [{"text": "第一个"}, {"text": "第二个"}],
        "两个调用的参数各自对得上",
    )
    order = [
        part_id for _, part_id, status in recorder.tool_states if status is ToolStatus.RUNNING
    ]
    check(order == [tools[0].id, tools[1].id], "两次执行按模型给出的顺序开始")

    for label, name, arguments, expected in (
        ("未知工具名", "nope", "{}", "unknown tool: nope"),
        ("缺参数", "echo", "{}", "缺少参数 text"),
        ("工具抛错", "fail", json.dumps({"reason": "打分程序不在 PATH 上"}), "打分程序不在 PATH 上"),
    ):
        server.clear()
        recorder.clear()
        server.script(
            Reply(
                chunks=[
                    delta_chunk(
                        tool_calls=[
                            tool_call_delta(0, call_id="c1", name=name, arguments=arguments)
                        ]
                    ),
                    usage_chunk(1, 1),
                ]
            ),
            Reply(chunks=[*text_chunks("换个办法", size=3), usage_chunk(1, 1)]),
        )
        session_fail = Session(id=f"ses_fail_{name}", title=label)
        recorder.session = session_fail
        ask(session_fail, "试一下")
        message = await loop.stream_reply(session_fail, "")
        assert message is not None
        parts = [part for part in message.parts if part.type is PartType.TOOL]
        check(
            len(parts) == 1
            and parts[0].tool_status is ToolStatus.FAILED
            and parts[0].tool_output == expected,
            f"{label}：{parts[0].tool_output if parts else '没有工具 Part'}",
        )
        fed_back = server.requests[-1]["messages"][-1]
        check(
            fed_back["role"] == "tool" and expected in fed_back["content"],
            f"{label}的失败原因回灌给模型",
        )

    server.clear()
    recorder.clear()
    server.script(
        Reply(status=429, body='{"error": "请求太快"}'),
        Reply(chunks=[*text_chunks("好", size=1), usage_chunk(3, 4)]),
    )
    session_retry = Session(id="ses_retry", title="重试")
    recorder.session = session_retry
    ask(session_retry, "你好")
    message = await loop.stream_reply(session_retry, "")
    assert message is not None
    retries = [part for part in message.parts if part.type is PartType.RETRY]
    check(len(retries) == 1, "429 之后重发一次就成功")
    check(
        "429" in retries[0].text and "第 1 次" in retries[0].text,
        f"重试 Part 写明原因与次数：{retries[0].text}",
    )
    check(
        [part.text for part in message.parts if part.type is PartType.TEXT] == ["好"],
        "重试成功之后正文照常写上",
    )
    check(len(recorder.names("turn.done")) == 1, "重试成功的一轮照常收尾")

    server.clear()
    recorder.clear()
    server.script(*[Reply(status=503, body="服务暂时不可用") for _ in range(MAX_RETRIES + 1)])
    session_dead = Session(id="ses_dead", title="重试耗尽")
    recorder.session = session_dead
    ask(session_dead, "你好")
    message = await loop.stream_reply(session_dead, "")
    assert message is not None
    texts = [part.text for part in message.parts if part.type is PartType.TEXT]
    errors = [part.text for part in message.parts if part.type is PartType.ERROR]
    check(len([part for part in message.parts if part.type is PartType.RETRY]) == MAX_RETRIES, "重试次数用满")
    check(
        errors and errors[-1].startswith("请求没有成功：") and "503" in errors[-1],
        f"错误原文写进 ERROR Part：{errors[-1] if errors else '没有错误 Part'}",
    )
    check(
        not [text for text in texts if text.startswith("请求没有成功：")],
        "报错不是模型说出来的正文，不能写进 TEXT（否则下一轮会当正常历史回灌）",
    )
    check(len(recorder.names("turn.done")) == 1, "重试耗尽也照常收尾，界面不会停在忙状态")

    server.clear()
    recorder.clear()
    server.script(Reply(status=401, body='{"error": "密钥不对"}'))
    session_auth = Session(id="ses_auth", title="凭据被拒")
    recorder.session = session_auth
    ask(session_auth, "你好")
    message = await loop.stream_reply(session_auth, "")
    assert message is not None
    check(not [part for part in message.parts if part.type is PartType.RETRY], "凭据被拒的错误不重试")
    errors = [part.text for part in message.parts if part.type is PartType.ERROR]
    check(
        errors and "401" in errors[-1],
        f"凭据被拒的错误原文写进 ERROR Part：{errors[-1] if errors else '没有错误 Part'}",
    )
    check(len(recorder.names("turn.done")) == 1, "不可重试的错误照样收尾")

    server.clear()
    recorder.clear()
    server.script(Reply(chunks=[delta_chunk(finish_reason="stop")]))
    session_empty = Session(id="ses_empty", title="空响应")
    recorder.session = session_empty
    ask(session_empty, "你好")
    message = await loop.stream_reply(session_empty, "")
    assert message is not None
    texts = [part.text for part in message.parts if part.type is PartType.TEXT]
    check(texts == ["（模型没有返回内容）"], "模型返回空时补一条说明，界面不会停在忙状态")

    cancelled_marker = WORKSPACE_FIXTURE / "loop_cancelled_marker.txt"
    cancelled_marker.unlink(missing_ok=True)
    server.clear()
    recorder.clear()
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="bash",
                            arguments=json.dumps(
                                {"command": delayed_command(3, cancelled_marker)}
                            ),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("第二次响应", size=3), usage_chunk(1, 1)]),
    )
    session_cancel = Session(id="ses_cancel", title="中断")
    recorder.session = session_cancel
    ask(session_cancel, "跑一条慢命令")
    running = asyncio.create_task(loop.stream_reply(session_cancel, ""))
    await asyncio.sleep(0.6)
    loop.cancel()
    running.cancel()
    interrupted = False
    try:
        await running
    except asyncio.CancelledError:
        interrupted = True
    check(interrupted, "中断时生成任务被摘掉")
    check(not recorder.names("turn.done"), "中断之后不发 turn.done")
    last = session_cancel.messages[-1]
    check(
        not [part for part in last.parts if part.type is PartType.STEP_FINISH],
        "中断之后不补 STEP_FINISH",
    )
    tools = [part for part in last.parts if part.type is PartType.TOOL]
    check(
        bool(tools) and tools[0].tool_status is ToolStatus.RUNNING,
        "被取消时工具还停在执行中，这个状态交给界面那一步写",
    )
    before = len(recorder.events)
    await asyncio.sleep(1.0)
    check(len(recorder.events) == before, "中断之后一秒内不再有任何事件")
    check(not cancelled_marker.exists(), "中断把子进程也一起收掉了")

    server.clear()
    recorder.clear()
    compact_session = Session(id="ses_compact", title="长会话")
    for index in range(40):
        item = compact_session.add_message(
            Message(role="user" if index % 2 == 0 else "assistant", session_id=compact_session.id)
        )
        item.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条消息，" + "内" * 400))
    recorder.session = compact_session
    # 窗口要留出三块固定开销：系统提示、给模型输出的预留、工具定义。
    # 装配器把后两块也从窗口里扣掉，所以窗口比「能塞下多少条消息」高出一截。
    # 断言量的是「总量在可用额度以内」这个不变量，窗口多大不影响它的含义，
    # 但要大到扣完固定开销之后仍有余量——否则量出来的超支来自固定开销，与压缩无关。
    loop.context_window = COMPACT_WINDOW
    server.script(
        Reply(chunks=[*text_chunks("这一段收成摘要", size=3), usage_chunk(3, 3)]),
        Reply(chunks=[*text_chunks("好", size=1), usage_chunk(2, 2)]),
    )
    message = await loop.stream_reply(compact_session, "")
    loop.context_window = 200_000
    assert message is not None
    compactions = [part for part in message.parts if part.type is PartType.COMPACTION]
    check(len(compactions) == 1, "历史超过窗口时补一条 COMPACTION Part")
    check(
        compactions[0].text == f"已把前 {compact_session.summary_upto} 条消息收成摘要",
        f"压缩那一条写着收了多少条：{compactions[0].text}",
    )
    check(compact_session.summary == "这一段收成摘要", f"摘要写进会话：{compact_session.summary}")
    check(compact_session.summary_upto > 0, "摘要记下覆盖到第几条")
    request = server.requests[-1]["messages"]
    check(request[0]["role"] == "system", "第一条仍然是系统提示")
    check(
        str(request[1]["content"]).startswith(SUMMARY_NOTICE),
        f"摘要插在系统提示之后：{str(request[1]['content'])[:40]}",
    )
    check("这一段收成摘要" in str(request[1]["content"]), "摘要正文进了请求体")
    content = json.dumps([item["content"] for item in request], ensure_ascii=False)
    present = sum(1 for index in range(40) if f"第 {index} 条消息" in content)
    check(present == 40 - compact_session.summary_upto, f"摘要覆盖的那一段原文不再发出去：还剩 {present} 条")
    total = sum(estimate_tokens(str(item["content"])) for item in request)
    check(
        total <= int(COMPACT_WINDOW * 0.8),
        f"发出去的历史降到窗口的八折以内：{total}",
    )

    server.clear()
    recorder.clear()
    payload = "甲" * 8000
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="echo",
                            arguments=json.dumps({"text": payload}, ensure_ascii=False),
                        )
                    ]
                ),
                usage_chunk(1, 1),
            ]
        ),
        Reply(chunks=[*text_chunks("收到了", size=3), usage_chunk(1, 1)]),
    )
    session_long = Session(id="ses_longout", title="长输出")
    recorder.session = session_long
    ask(session_long, "给我看一段长输出")
    message = await loop.stream_reply(session_long, "")
    assert message is not None
    tools = [part for part in message.parts if part.type is PartType.TOOL]
    check(len(tools[0].tool_output) == 8000, f"Part 上存的是完整输出：{len(tools[0].tool_output)} 字符")
    fed_back = [
        item for item in server.requests[-1]["messages"] if item["role"] == "tool"
    ][-1]["content"]
    check(len(fed_back) < 8000, "发给模型的那一份被裁短")
    check(
        fed_back.startswith("甲" * 2000) and fed_back.endswith("甲" * 2000),
        "发给模型的那一份留了前后各 2000 个字符",
    )
    check("… 省略 4000 字符" in fed_back, "裁掉的那一段写明省略了多少字符")

    await check_effort(server, loop, recorder)
    await check_identity(server, loop, recorder)
    await check_step_limit(server)
    await check_wrap_up_fallbacks(server)
    await check_run_bash(server, loop, recorder)
    await check_sampling(server)
    await check_switch_model(server)
    await check_compact(server)


async def check_sampling(server: FakeModelServer) -> None:
    """采样参数按配置写进请求体，没配的就不写。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    plain = build_loop(server, bus, registry)
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
    session = Session(id="ses_sample_plain", title="没有采样参数")
    await plain.stream_reply(session, "")
    body = server.requests[-1]
    check("temperature" not in body, "没有配采样参数时不写 temperature")
    check("max_tokens" not in body, "没有配采样参数时不写 max_tokens")

    server.clear()
    tuned = AgentLoop(
        EventBus(),
        OpenAICompatProvider(
            ProviderConfig(
                base_url=server.base_url,
                api_key="secret",
                model="fake-model",
                temperature=0.25,
                max_tokens=4096,
            )
        ),
        registry,
        ProviderConfig(
            base_url=server.base_url,
            api_key="secret",
            model="fake-model",
            temperature=0.25,
            max_tokens=4096,
        ),
        workspace_root=WORKSPACE_FIXTURE,
        policy=PermissionPolicy(default_ask=frozenset()),
    )
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
    await tuned.stream_reply(Session(id="ses_sample_tuned", title="配了采样参数"), "")
    body = server.requests[-1]
    check(body.get("temperature") == 0.25, "配了 temperature 就写进请求体")
    check(body.get("max_tokens") == 4096, "配了 max_tokens 就写进请求体")


async def check_switch_model(server: FakeModelServer) -> None:
    """换模型之后，请求体里的模型与计价参数跟着换。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = build_loop(server, bus, registry, price_in=1.0, price_out=2.0)

    server.script(Reply(chunks=[*text_chunks("第一句", size=2), usage_chunk(100, 50)]))
    session = Session(id="ses_switch", title="换模型")
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(server.requests[-1]["model"] == "fake-model", "换之前请求体里是原来那个模型")
    check(abs(message.cost - (100 * 1.0 + 50 * 2.0) / 1e6) < 1e-12, "换之前按原来的单价算")

    before = loop.config
    config = loop.switch_model("gpt-4o-mini")
    check(config.model == "gpt-4o-mini", "换模型之后配置上的名字变了")
    check(config.base_url == before.base_url and config.api_key == before.api_key, "换模型不动连接参数")
    check(loop.context_window == 128_000, "换模型之后窗口跟着换")
    check(loop.provider.config is config, "换模型之后 provider 拿的是新配置")

    server.clear()
    server.script(Reply(chunks=[*text_chunks("第二句", size=2), usage_chunk(100, 50)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(server.requests[-1]["model"] == "gpt-4o-mini", "换之后再问，请求体里是新模型")
    check(server.requests[-1].get("temperature") == 1.0, "换之后再问，采样参数按新模型写")
    check(abs(message.cost - (100 * 0.15 + 50 * 0.6) / 1e6) < 1e-12, "换之后再问，按新模型单价算")
    check(message.model_id == "gpt-4o-mini", "消息上写的也是新模型")
    check(
        "1 · gpt-4o-mini" in [part.text for part in message.parts if part.type is PartType.STEP_START],
        "step 那一行的模型名跟着换",
    )

    server.clear()
    loop.switch_model("自研模型")
    server.script(Reply(chunks=[*text_chunks("第三句", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(server.requests[-1]["model"] == "自研模型", "换到清单外的模型照样发出去")
    check("temperature" not in server.requests[-1], "清单外的模型不带采样参数")
    check(loop.context_window == DEFAULT_CONTEXT_WINDOW, "清单外的模型窗口退回兜底值")


async def check_compact(server: FakeModelServer) -> None:
    """手动压缩：位置推前面，紧接着发问时把丢掉的那一段收成摘要。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = build_loop(server, bus, registry)

    session = Session(id="ses_compact", title="手动压缩")
    for index in range(12):
        ask(session, f"第 {index} 句")
    check(session.compact_upto == 0, "新会话的压缩位置是零")
    check(session.summary == "" and session.summary_upto == 0, "新会话没有摘要")

    server.script(Reply(chunks=[*text_chunks("收到了", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    sent = len(session.messages)
    full = len(server.requests[-1]["messages"])
    check(full > KEEP_MESSAGES, f"没压缩时发出去的消息远多于保留条数：{full}")

    kept = loop.compact_session(session)
    check(kept == KEEP_MESSAGES, "压完之后只留最近几条")
    check(session.compact_upto == sent - KEEP_MESSAGES, "压缩位置记在会话上")
    check(len(session.messages) == sent, "压缩不动会话里的消息")
    check(session.summary == "", "压缩这一步本身不生成摘要")

    server.clear()
    server.script(
        Reply(chunks=[*text_chunks("这一段收成摘要", size=3), usage_chunk(5, 5)]),
        Reply(chunks=[*text_chunks("接着答", size=2), usage_chunk(1, 1)]),
    )
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(
        session.summary == "这一段收成摘要",
        f"下一次发问时把丢掉的那一段收成摘要：{session.summary}",
    )
    check(
        session.summary_upto == session.compact_upto,
        f"摘要覆盖到压缩位置：{session.summary_upto} 对 {session.compact_upto}",
    )
    fed = server.requests[-1]["messages"]
    check(fed[0]["role"] == "system", "压缩之后最前面仍然是系统提示")
    check(
        str(fed[1]["content"]).startswith(SUMMARY_NOTICE),
        f"摘要插在系统提示之后：{str(fed[1]['content'])[:30]}",
    )
    check("这一段收成摘要" in str(fed[1]["content"]), "摘要正文进了请求体")
    check(len(fed) == KEEP_MESSAGES + 2, f"压完之后只带最近几条：{len(fed)} 条")
    check(
        [part.text for part in message.parts if part.type is PartType.COMPACTION]
        == [f"已把前 {session.summary_upto} 条消息收成摘要"],
        "压缩这一轮在界面上留一条记录",
    )
    check(
        [part.text for part in message.parts if part.type is PartType.TEXT] == ["接着答"],
        "摘要之后这一步照常问模型",
    )
    check(session.total_tokens() > 0, "摘要那一次请求的用量也记进 tokens")

    same = session.compact_upto
    server.clear()
    server.script(Reply(chunks=[*text_chunks("再答", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(session.compact_upto >= same, "再压一次不会把位置往回退")
    check(
        [part.text for part in message.parts if part.type is PartType.COMPACTION]
        == [f"已丢弃 {session.summary_upto} 条更早的消息"],
        "已经收进摘要的那一段不再收一次",
    )
    check(len(server.requests) == 1, "不再收摘要时只发一次请求")

    fresh = Session(id="ses_compact_few", title="消息太少")
    ask(fresh, "只有一句")
    check(loop.compact_session(fresh) == 1, "消息不够多时原样返回条数")
    check(fresh.compact_upto == 0, "消息不够多时不记压缩位置")


async def check_summary_compaction(server: FakeModelServer) -> None:
    """摘要真的进了请求体：用户给出的约束留住、原文不再发、Part 上的完整内容不动。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = build_loop(server, bus, registry, context_window=20_000)

    constraint = "约束：所有报告用中文写，分子编号保留原始大小写。"
    session = Session(id="ses_summary", title="摘要压缩")
    ask(session, constraint)
    filler = "分子筛选过程的中间记录，" + "记" * 500
    for index in range(30):
        item = session.add_message(
            Message(role="user" if index % 2 == 0 else "assistant", session_id=session.id)
        )
        item.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条，{filler}"))
    original = [part.text for message in session.messages for part in message.parts]
    check(len(session.messages) == 31, "这一段会话造出了 31 条消息")

    summary_text = (
        "一、用户的目标与意图\n筛选分子。\n"
        "二、已经完成的改动与涉及的文件\n还没有改动。\n"
        "三、待办的下一步\n继续筛选。\n"
        f"四、用户明确给出的约束与要求\n{constraint}"
    )
    server.script(
        Reply(chunks=[*text_chunks(summary_text, size=5), usage_chunk(7, 7)]),
        Reply(chunks=[*text_chunks("接着做", size=2), usage_chunk(1, 1)]),
    )
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(session.summary_upto > 0, f"超窗之后生成了摘要，覆盖到第 {session.summary_upto} 条")

    # 摘要那一次请求：指令四段齐全，用户给出的约束原样喂给模型
    summary_request = server.requests[0]["messages"]
    check(summary_request[0]["content"] == SUMMARY_INSTRUCTION, "摘要请求的第一条是摘要指令")
    for section in (
        "一、用户的目标与意图",
        "二、已经完成的改动与涉及的文件",
        "三、待办的下一步",
        "四、用户明确给出的约束与要求",
    ):
        check(section in summary_request[0]["content"], f"摘要指令里有 {section}")
    check(
        constraint in summary_request[1]["content"],
        "摘要请求把用户给出的约束一起交给模型",
    )
    check(constraint in session.summary, "摘要正文里带着用户给出的约束")

    fed = server.requests[-1]["messages"]
    check(fed[0]["role"] == "system", "发出去的第一条是系统提示")
    check(
        str(fed[1]["content"]).startswith(SUMMARY_NOTICE),
        f"第二条是摘要那一条：{str(fed[1]['content'])[:30]}",
    )
    check(
        str(fed[1]["content"]).split(SUMMARY_NOTICE, 1)[1].strip().startswith(session.summary),
        "摘要那一条的正文以会话上的摘要开头",
    )
    check(constraint in str(fed[1]["content"]), "摘要那一条的正文含用户给出的约束")

    sent = json.dumps(fed, ensure_ascii=False)
    check("第 0 条" not in sent, "被摘要覆盖的那一段原文不再发出去")
    check("第 29 条" in sent, "保留的那几条原文照常发出去")

    kept = [part.text for message in session.messages[:31] for part in message.parts]
    check(kept == original, "会话里的 Part 内容一条不少、一字不改")
    check(
        len(session.messages[1].parts[0].text) == len(f"第 0 条，{filler}"),
        "被摘要覆盖的那一条在会话里仍是完整长度",
    )
    check(session.total_tokens() > 0, "摘要那一次请求的用量也记进 tokens")


async def check_effort(
    server: FakeModelServer,
    loop: AgentLoop,
    recorder: Recorder,
) -> None:
    """推理强度一路进到请求体。"""
    for effort in ("low", "medium", "high"):
        server.clear()
        recorder.clear()
        session = Session(id=f"ses_effort_{effort}", title="推理强度", effort=effort)
        recorder.session = session
        server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
        await loop.stream_reply(session, "")
        check(
            server.requests[-1].get("reasoning_effort") == effort,
            f"推理强度 {effort} 进了请求体",
        )

    server.clear()
    recorder.clear()
    plain = Session(id="ses_effort_none", title="没有推理强度", effort="")
    recorder.session = plain
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
    await loop.stream_reply(plain, "")
    check("reasoning_effort" not in server.requests[-1], "推理强度为空时不带这个字段")

    server.clear()
    recorder.clear()
    bus = EventBus()
    registry = ToolRegistry()
    loop_without = build_loop(server, bus, registry, supports_reasoning=False)
    session = Session(id="ses_effort_unsupported", title="不认这个字段的模型", effort="high")
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
    await loop_without.stream_reply(session, "")
    check(
        "reasoning_effort" not in server.requests[-1],
        "模型不接受这个字段时不写进请求体",
    )


async def check_identity(
    server: FakeModelServer,
    loop: AgentLoop,
    recorder: Recorder,
) -> None:
    """Agent 名字进系统提示。"""
    prompts: dict[str, str] = {}
    for agent in ("Build", "Plan"):
        server.clear()
        recorder.clear()
        session = Session(id=f"ses_agent_{agent}", title="身份", agent_name=agent)
        recorder.session = session
        server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
        await loop.stream_reply(session, "")
        prompts[agent] = server.requests[-1]["messages"][0]["content"]
    check("Build" in prompts["Build"], "Build 的系统提示按 Build 写")
    check("Plan" in prompts["Plan"], "Plan 的系统提示按 Plan 写")
    check(prompts["Build"] != prompts["Plan"], "两种 Agent 的系统提示不一样")

    session = Session(id="ses_agent_unknown", title="身份", agent_name="Nope")
    recorder.session = session
    server.clear()
    recorder.clear()
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(1, 1)]))
    await loop.stream_reply(session, "")
    check("Build" in server.requests[-1]["messages"][0]["content"], "没见过的 Agent 名字按 Build 处理")


async def check_step_limit(server: FakeModelServer) -> None:
    """走满步数上限时给出一份收尾总结，而不是只留一句「先停在这里」。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = build_loop(server, bus, registry)
    session = Session(id="ses_limit", title="步数上限")
    ask(session, "一直调用工具")
    server.clear()
    for _ in range(MAX_STEPS):
        server.script(
            Reply(
                chunks=[
                    delta_chunk(
                        tool_calls=[
                            tool_call_delta(
                                0,
                                call_id="c1",
                                name="echo",
                                arguments=json.dumps({"text": "再来"}, ensure_ascii=False),
                            )
                        ]
                    ),
                    usage_chunk(1, 1),
                ]
            )
        )
    # 走满之后的那一次请求是**收尾总结**：它不带工具，模型在这里写的正文就是总结。
    summary_text = "一、这一轮反复调用了 echo。二、没有产出文件。三、任务未完成。四、可以把目标分小。"
    server.script(Reply(chunks=[*text_chunks(summary_text, size=8), usage_chunk(9, 9)]))
    watcher = Recorder(bus)
    watcher.session = session
    message = await loop.stream_reply(session, "")
    assert message is not None
    summaries = [part.text for part in message.parts if part.type is PartType.SUMMARY]
    check(
        len(summaries) == 1,
        f"步数用满时留一条 SUMMARY Part：{[p.type.value for p in message.parts][-4:]}",
    )
    check(
        "二、没有产出文件" in summaries[0],
        f"收尾总结带上了模型写的四段正文：{summaries[0][:120]}",
    )
    # 收尾请求本身不带工具：它是让模型写字，不是让它接着干活。
    wrap_up_request = server.requests[-1]
    check(
        not wrap_up_request.get("tools"),
        "收尾总结那一次请求不带工具",
    )
    check(
        "停下来是因为" in wrap_up_request["messages"][-1]["content"],
        f"收尾请求把停下来的原因写进指令：{wrap_up_request['messages'][-1]['content'][:80]}",
    )
    check(bool(watcher.names("turn.done")), "走满步数上限之后照常发 turn.done")
    check(
        bool([part for part in message.parts if part.type is PartType.STEP_FINISH]),
        "走满步数上限之后照样收尾，界面不会停在忙状态",
    )
    check(
        len([part for part in message.parts if part.type is PartType.STEP_START]) == MAX_STEPS,
        f"正好问了 {MAX_STEPS} 次工具步",
    )
    check(
        not [part for part in message.parts if part.type is PartType.TEXT],
        "步数用满时不再往正文里插一句「先停在这里」，交代由 SUMMARY 承担",
    )


async def check_wrap_up_fallbacks(server: FakeModelServer) -> None:
    """收尾总结「任何情况下都有」：模型写不出来时退回本地汇编，被中断时也留下。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(SlowTool())
    loop = build_loop(server, bus, registry)

    # 一、收尾请求失败 -> 本地按已有 Part 汇编，不能空手收场。
    session = Session(id="ses_wrap_fail", title="收尾请求失败")
    ask(session, "一直调用工具")
    server.clear()
    for _ in range(MAX_STEPS):
        server.script(
            Reply(
                chunks=[
                    delta_chunk(
                        tool_calls=[
                            tool_call_delta(
                                0, call_id="c1", name="echo",
                                arguments=json.dumps({"text": "再来"}),
                            )
                        ]
                    ),
                    usage_chunk(1, 1),
                ]
            )
        )
    # 收尾那一次请求连着重试一起打回 503，模型这条路彻底走不通。
    server.script(*[Reply(status=503, body="服务暂时不可用") for _ in range(MAX_RETRIES + 1)])
    message = await loop.stream_reply(session, "")
    assert message is not None
    summaries = [part.text for part in message.parts if part.type is PartType.SUMMARY]
    check(len(summaries) == 1, "收尾请求失败时本地照样补出一条 SUMMARY")
    check(
        "步数用满" in summaries[0],
        f"本地汇编的总结写明为什么停下：{summaries[0][:80]}",
    )
    check(
        "echo" in summaries[0],
        f"本地汇编读的是真实的工具记录：{summaries[0][:160]}",
    )
    check(
        "本地按已有记录汇编" in summaries[0],
        "本地汇编的那一份自己标明来源，用户能分辨它不是模型写的",
    )

    # 二、被中断 -> 也留总结（中断是「任何情况」里最需要考虑的一种）。
    server.clear()
    session_cancel = Session(id="ses_wrap_cancel", title="中断也要总结")
    ask(session_cancel, "调用工具")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0, call_id="c1", name="slow",
                            arguments=json.dumps({}),
                        )
                    ]
                ),
                usage_chunk(1, 1),
            ]
        )
    )
    running = asyncio.ensure_future(loop.stream_reply(session_cancel, ""))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if [p for p in session_cancel.messages[-1].parts if p.type is PartType.TOOL]:
            break
    loop.cancel()
    message = await asyncio.wait_for(running, timeout=5)
    assert message is not None
    summaries = [part.text for part in message.parts if part.type is PartType.SUMMARY]
    check(
        len(summaries) == 1,
        f"被中断时也留下收尾总结：{len(summaries)} 份（"
        f"{[part.type.value for part in message.parts]}）",
    )
    check(
        "被中断" in summaries[0],
        f"中断那一份写明是中断：{summaries[0][:160]}",
    )
    check(
        "本地按已有记录汇编" in summaries[0],
        "中断之后不再发请求，直接走本地汇编",
    )

    # 三、模型报错 -> 走总结那一条，而不是当成「模型说完了」。
    server.clear()
    session_err = Session(id="ses_wrap_err", title="报错也要总结")
    ask(session_err, "你好")
    server.script(*[Reply(status=503, body="服务暂时不可用") for _ in range(MAX_RETRIES + 1)])
    # 报错之后的收尾请求，同样打回失败 -> 走本地汇编。
    server.script(*[Reply(status=503, body="服务暂时不可用") for _ in range(MAX_RETRIES + 1)])
    message = await loop.stream_reply(session_err, "")
    assert message is not None
    errors = [part.text for part in message.parts if part.type is PartType.ERROR]
    summaries = [part.text for part in message.parts if part.type is PartType.SUMMARY]
    check(bool(errors), f"报错仍然留下 ERROR Part：{errors[:1]}")
    check(len(summaries) == 1, "模型报错时也留下收尾总结")
    check("请求出错" in summaries[0], f"报错那一份写明原因：{summaries[0][:80]}")


async def check_run_bash(
    server: FakeModelServer,
    loop: AgentLoop,
    recorder: Recorder,
) -> None:
    """用户直接给出的命令和模型发的工具调用走同一条路。"""
    server.clear()
    recorder.clear()
    session = Session(id="ses_run_bash", title="直接命令")
    recorder.session = session
    message = await loop.run_bash(session, "echo hi")
    tools = [part for part in message.parts if part.type is PartType.TOOL]
    check(len(tools) == 1 and tools[0].tool_status is ToolStatus.COMPLETED, "直接命令照样有工具 Part")
    check(tools[0].tool_output.strip() == "hi", "直接命令的输出写进 Part")
    check(
        bool([part for part in message.parts if part.type is PartType.STEP_FINISH]),
        "直接命令照样补 STEP_FINISH",
    )
    check(bool(recorder.names("turn.done")), "直接命令照样发 turn.done")
    check(bool(recorder.names("status.update")), "直接命令照样发 status.update")

    server.clear()
    recorder.clear()
    empty = Session(id="ses_run_bash_empty", title="空命令")
    recorder.session = empty
    message = await loop.run_bash(empty, "   ")
    tools = [part for part in message.parts if part.type is PartType.TOOL]
    check(tools[0].tool_status is ToolStatus.FAILED, "空命令被参数检查拦下")
    check("command" in tools[0].tool_output, f"失败原因写明缺了哪个参数：{tools[0].tool_output}")
    check(bool(recorder.names("turn.done")), "被拦下之后照常发 turn.done")


async def check_permission_in_loop(server: FakeModelServer) -> None:
    """判定接在工具执行上：拒绝与询问两条路都走一遍。"""
    bus = EventBus()
    recorder = Recorder(bus)
    registry = build_permission_registry()
    loop = build_loop(
        server,
        bus,
        registry,
        # 带上工作目录：这一组里有「越界仍然要问」那一条，没有根目录那一级就不生效。
        policy=PermissionPolicy(workspace_root=WORKSPACE_FIXTURE),
    )

    target = WORKSPACE_FIXTURE / "plan_write.txt"
    target.unlink(missing_ok=True)
    server.clear()
    recorder.clear()
    plan_session = Session(id="ses_plan_write", title="只读")
    plan_session.agent_name = "Plan"
    recorder.session = plan_session
    ask(plan_session, "把这个文件改一下")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="write",
                            arguments=json.dumps({"path": str(target), "content": "写进去了"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("改不了", size=2), usage_chunk(1, 1)]),
    )
    message = await loop.stream_reply(plan_session, "")
    tools = [part for part in message.parts if part.type is PartType.TOOL]
    check(len(tools) == 1 and tools[0].tool_status is ToolStatus.FAILED, "Plan 方式下 write 被拒")
    check("Plan" in tools[0].tool_output, f"拒绝原因写明是 Plan 限制：{tools[0].tool_output}")
    check(not target.exists(), "被拒之后文件没有被写出来")

    # 询问：循环停在等答复上，界面把允许交回来之后才真的执行
    server.clear()
    recorder.clear()
    ask_session = Session(id="ses_ask", title="要问一次")
    recorder.session = ask_session
    ask(ask_session, "跑一条命令")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="bash",
                            arguments=json.dumps({"command": "echo asked_then_ran"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("跑完了", size=2), usage_chunk(1, 1)]),
    )
    running = asyncio.create_task(loop.stream_reply(ask_session, ""))
    for _ in range(50):
        await asyncio.sleep(0.05)
        waiting = [
            part
            for part in ask_session.messages[-1].parts
            if part.type is PartType.TOOL and part.tool_status is ToolStatus.AWAITING
        ]
        if waiting:
            break
    check(bool(waiting), "默认策略下 bash 停下来等答复")
    check(
        bool(recorder.names("permission.ask")),
        "等答复时发出 permission.ask",
    )
    part_id = waiting[0].id
    check(loop.resolve_permission(part_id, Verdict.ALLOW, remember=True), "把允许交回给等着的那个判定")
    await running
    tools = [part for part in ask_session.messages[-1].parts if part.type is PartType.TOOL]
    check(
        tools[0].tool_status is ToolStatus.COMPLETED and "asked_then_ran" in tools[0].tool_output,
        f"允许之后命令真的执行了：{tools[0].tool_output}",
    )
    check("本会话" in tools[0].permission, f"放行的依据记在 Part 上：{tools[0].permission}")

    # 按 a 之后整个工具都放行：换一条**内容完全不同**的命令也不该再问。
    # 按命令原文记的时候这一条会失败，正是用户体感的来源。
    server.clear()
    recorder.clear()
    ask(ask_session, "再跑一条别的")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c2",
                            name="bash",
                            arguments=json.dumps({"command": "echo 换成完全不同的命令"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("又跑完了", size=2), usage_chunk(1, 1)]),
    )
    await loop.stream_reply(ask_session, "")
    tools = [part for part in ask_session.messages[-1].parts if part.type is PartType.TOOL]
    check(
        tools[0].tool_status is ToolStatus.COMPLETED,
        f"放行之后同工具的另一条命令也直接执行：{tools[0].tool_status}",
    )
    check(not recorder.names("permission.ask"), "放行之后这个工具不再发 permission.ask")

    # 拒绝：工具标成失败，原因写用户拒绝。
    #
    # 必须换一个**新会话**：上面那次按 a 放行的是整个会话的所有工具，
    # 同一个会话里 bash 和 write 之后都不问了，走不到弹窗那一步。
    server.clear()
    recorder.clear()
    deny_session = Session(id="ses_deny", title="要拒绝")
    recorder.session = deny_session
    loop.policy.clear_session()
    ask(deny_session, "写一个文件")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c3",
                            name="write",
                            arguments=json.dumps({"path": "别写.txt", "content": "x"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("那就不写", size=2), usage_chunk(1, 1)]),
    )
    running = asyncio.create_task(loop.stream_reply(deny_session, ""))
    for _ in range(50):
        await asyncio.sleep(0.05)
        waiting = [
            part
            for part in deny_session.messages[-1].parts
            if part.type is PartType.TOOL and part.tool_status is ToolStatus.AWAITING
        ]
        if waiting:
            break
    check(bool(waiting), "换一个会话之后 write 照旧停下来等答复")
    loop.resolve_permission(waiting[0].id, Verdict.DENY)
    await running
    tools = [part for part in deny_session.messages[-1].parts if part.type is PartType.TOOL]
    check(tools[0].tool_status is ToolStatus.FAILED, "拒绝之后工具标成失败")
    check(tools[0].tool_output == "用户拒绝", f"拒绝原因回灌给模型：{tools[0].tool_output}")

    # 全放行之后，越界仍然要问：a 放行的是工具，不是磁盘。
    # 这一条是「放行范围扩大之后边界还在」的证据。
    server.clear()
    recorder.clear()
    bound_session = Session(id="ses_bound", title="越界仍要问")
    recorder.session = bound_session
    ask(bound_session, "读一个外面的文件")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c5",
                            name="read",
                            arguments=json.dumps({"path": OUTSIDE_PATH}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("那不读", size=2), usage_chunk(1, 1)]),
    )
    running = asyncio.create_task(loop.stream_reply(bound_session, ""))
    for _ in range(50):
        await asyncio.sleep(0.05)
        waiting = [
            part
            for part in bound_session.messages[-1].parts
            if part.type is PartType.TOOL and part.tool_status is ToolStatus.AWAITING
        ]
        if waiting:
            break
    check(bool(waiting), "步骤停下来等越界的答复")
    loop.resolve_permission(waiting[0].id, Verdict.DENY)
    await running
    tools = [part for part in bound_session.messages[-1].parts if part.type is PartType.TOOL]
    check(tools[0].tool_status is ToolStatus.FAILED, "越界被拒之后工具标成失败")

    # 等答复的时候按中断，判定要被放掉，循环不能停在等待上
    server.clear()
    recorder.clear()
    # 同样要换新会话：全放行是按会话记的，被放行过的会话走不到等待那一步。
    cancel_session = Session(id="ses_ask_cancel", title="等着的时候中断")
    recorder.session = cancel_session
    loop.policy.clear_session()
    ask(cancel_session, "写一个等着的时候中断的文件")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c4",
                            name="write",
                            arguments=json.dumps({"path": "不会写.txt", "content": "x"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        )
    )
    running = asyncio.create_task(loop.stream_reply(cancel_session, ""))
    for _ in range(50):
        await asyncio.sleep(0.05)
        waiting = [
            part
            for part in cancel_session.messages[-1].parts
            if part.type is PartType.TOOL and part.tool_status is ToolStatus.AWAITING
        ]
        if waiting:
            break
    check(bool(waiting), "新会话里的 write 停下来等答复")
    loop.cancel()
    await asyncio.wait_for(running, timeout=5)
    check(not recorder.names("turn.done"), "等答复时中断，不发 turn.done")
    tools = [part for part in cancel_session.messages[-1].parts if part.type is PartType.TOOL]
    check(tools[0].tool_status is ToolStatus.FAILED, "中断把等着的工具标成失败")


def rendered_lines(widget: object) -> list[str]:
    """把一个组件子树画出来的每一行取成文本，空行丢掉。

    Markdown 把正文铺在一棵子组件树上，父组件自己 render() 出来是空的。
    """
    return [
        stripped
        for child in widget.walk_children(with_self=True)
        for index in range(child.region.height)
        for stripped in (child.render_line(index).text.strip(),)
        if stripped
    ]


async def wait_idle(app: OpenCodeApp, pilot: object, timeout: float = 10.0) -> None:
    """等当前生成任务结束。"""
    waited = 0.0
    while waited < timeout:
        screen = app.screen
        if isinstance(screen, ChatScreen) and not screen._busy:
            return
        await pilot.pause(0.1)
        waited += 0.1
    raise AssertionError("等待生成任务结束超时")


def tool_parts_of(app: OpenCodeApp) -> list[Part]:
    """当前会话里全部的 TOOL Part。"""
    return [
        part
        for message in app.session_manager.current.messages
        for part in message.parts
        if part.type is PartType.TOOL
    ]


async def check_home_new_session() -> None:
    """主页面提问默认开新会话：有内容就另起，空会话复用。"""
    root = ROOT / ".probe" / "home_session"
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state.toml"
    state.unlink(missing_ok=True)
    save_connection(
        connection_path_for(state),
        Connection("https://api.example.com/v1", "k", "deepseek-flash"),
    )

    app = OpenCodeApp(root=ROOT, state_path=state)

    async def fake_stream(session: object, prompt: str) -> None:
        """不发请求：这一组验的是会话选择，不是模型链路。"""
        return None

    app.agent.stream_reply = fake_stream  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        empty_id = app.session_manager.current_session_id
        app.start_chat_with("第一句")
        await pilot.pause()
        check(
            app.session_manager.current_session_id == empty_id,
            "当前会话还是空的时候直接复用，不另开一个",
        )
        check(len(app.session_manager.current.messages) == 1, "第一句写进复用的会话")

        app.switch_screen(HomeScreen())
        await pilot.pause()
        app.start_chat_with("第二句")
        await pilot.pause()
        check(
            app.session_manager.current_session_id != empty_id,
            "主页面再提问时另开一个新会话",
        )
        check(
            [message.parts[0].text for message in app.session_manager.current.messages]
            == ["第二句"],
            "新会话里只有这一句，旧会话的内容不会串进来",
        )
        check(
            len(app.session_manager.sessions) == 2,
            f"旧会话还留在列表里：{len(app.session_manager.sessions)} 个",
        )


async def check_unconfigured_app() -> None:
    """没有 connection.toml 时：应用照常起来、发消息被拦、/model 填完即用。"""
    root = ROOT / ".probe" / "unconfigured"
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state.toml"
    state.unlink(missing_ok=True)
    connection_path_for(state).unlink(missing_ok=True)

    app = OpenCodeApp(root=ROOT, state_path=state)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        check(not app.is_configured, "没有连接配置时应用照常启动")
        check(app.current_model_label == "（未配置）", "状态行显示未配置")

        app.start_chat_with("hello")
        await pilot.pause()
        check(
            isinstance(app.screen, ConnectionFormScreen),
            "未配置时提问直接弹出接口表单",
        )
        chat = next(screen for screen in app.screen_stack if isinstance(screen, ChatScreen))
        check(chat.editor.text == "hello", "被拦下的问题放回输入框，配好即可重发")
        check(
            len(app.session_manager.current.messages) == 0,
            "未配置时发消息被拦下，不写进会话",
        )

        form = app.screen
        form.query_one("#connection-base-url", Input).value = "https://api.example.com/v1"
        form.query_one("#connection-api-key", Input).value = "k"
        form.query_one("#connection-model", Input).value = "deepseek-flash"
        form.query_one("#connection-window", Input).value = "1000000"
        form.action_save()
        await pilot.pause()
        check(app.is_configured, "表单保存之后接口就绪")
        saved = load_connection(connection_path_for(state))
        check(
            saved is not None and saved.model == "deepseek-flash",
            "表单把配置写进 connection.toml",
        )
        check(app.agent.config.context_window == 1_000_000, "表单里的窗口跟着生效")
        check(
            app.session_manager.current.model_id == "deepseek-flash",
            "保存后会话记下新模型",
        )

        app.execute_command("model gpt-4o")
        await pilot.pause()
        check(app.agent.config.model == "gpt-4o", "/model 带参数只换模型")
        check(
            app.agent.config.base_url == "https://api.example.com/v1",
            "换模型时接口地址不动",
        )


async def check_live_app(server: FakeModelServer) -> None:
    """从界面提交一句话到消息区出现完整回复。

    **根目录要用隔离的那一个**（不是 `ROOT`）：`.opencode_tui/tools.toml` 是
    用户自己会改的本地配置，拿仓库根当 root 的话，本机把某个工具关掉之后
    这一组断言会当场响——那是配置生效，不是产品坏了。这里要验的是
    「没人配置时全装」，所以给一个空目录当 root。
    """
    state = ROOT / ".probe" / "state_live.toml"
    bare_root = ROOT / ".probe" / "live_root"
    bare_root.mkdir(parents=True, exist_ok=True)
    save_connection(
        connection_path_for(state),
        Connection(
            server.base_url,
            "secret",
            "fake-model",
            context_window=1_000_000,
            price_in=0.5,
            price_out=1.5,
        ),
    )

    state.unlink(missing_ok=True)
    logs = log_directory_for(state)
    if logs.exists():
        for stale in logs.iterdir():
            stale.unlink()
    server.clear()
    server.script(
        Reply(
            chunks=[
                *text_chunks("界面上的回答。", size=3),
                delta_chunk(finish_reason="stop"),
                usage_chunk(9, 11),
            ]
        )
    )
    app = OpenCodeApp(root=bare_root, state_path=state)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press(*list("hello"))
        await pilot.press("enter")
        await wait_idle(app, pilot)
        session = app.session_manager.current
        check(len(session.messages) == 2, "提交一句话之后会话里有用户消息与回复各一条")
        assistant = session.messages[-1]
        check(assistant.role == "assistant", "最后一条是助手的回复")
        kinds = [part.type for part in assistant.parts]
        check(
            kinds == [PartType.STEP_START, PartType.TEXT, PartType.STEP_FINISH],
            f"界面这一侧看到的 Part 顺序：{[kind.value for kind in kinds]}",
        )
        check(assistant.parts[1].text == "界面上的回答。", "流式文本拼成完整回复")
        check(
            assistant.tokens == {"input": 9, "output": 11, "cached": 0, "uncached": 9},
            "用量写在这一轮的消息上",
        )
        check(assistant.model_id == "fake-model", "消息上写着实际使用的模型")
        log_path = app.trace.current_path()
        check(log_path.parent == logs, f"日志目录跟着状态文件走：{log_path.parent}")
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        events = [row["event"] for row in rows]
        for name in ("message.add", "part.add", "turn.done"):
            check(name in events, f"一轮跑完日志里有 {name}")
        check(
            events.index("message.add") < events.index("turn.done"),
            "message.add 排在 turn.done 之前",
        )
        check(
            any(row["payload"].get("session_id") == session.id for row in rows),
            "日志里的 session_id 是这一轮那个会话",
        )
        check(app.current_model_label == "fake-model", "状态行上的模型名取自连接配置")
        check(app.context_window == 1_000_000, "上下文窗口取自连接配置")
        check(
            app.agent.registry.get("read") is not None
            and app.agent.registry.get("write") is not None
            and app.agent.registry.get("patch") is not None
            and app.agent.registry.get("grep") is not None,
            "界面这一侧组装出来了四个文件工具",
        )
        check(
            all(app.agent.registry.get(name) is not None for name in DOMAIN_TOOL_NAMES),
            "界面这一侧把领域工具也装上了（写了却没装 = 模型看不见，"
            f"而这在界面上完全看不出来）：缺 {[n for n in DOMAIN_TOOL_NAMES if app.agent.registry.get(n) is None]}",
        )
        check(
            [spec["function"]["name"] for spec in app.agent.registry.specs()][:3]
            == ["bash", "read", "write"],
            "文件工具排在前面，领域工具跟在后面（装配顺序就是请求体里的顺序）",
        )
        check(
            not app.agent.registry.disabled,
            "没配 tools.toml 时禁用清单是空的，一个工具都不少",
        )
        check(
            abs(assistant.cost - 0.000021) < 1e-9,
            f"这一轮的金额按连接配置里的单价算：{assistant.cost}",
        )
        check(
            "界面上的回答。" in " ".join(rendered_lines(app.screen.messages)),
            "消息区把这段回复画了出来",
        )

    marker = WORKSPACE_FIXTURE / "live_cancelled_marker.txt"
    marker.unlink(missing_ok=True)
    state.unlink(missing_ok=True)
    server.clear()
    turns: list[dict[str, object]] = []
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="bash",
                            arguments=json.dumps({"command": delayed_command(3, marker)}),
                        )
                    ]
                ),
                usage_chunk(3, 3),
            ]
        )
    )
    app = OpenCodeApp(root=ROOT, state_path=state, policy=PermissionPolicy(default_ask=frozenset()))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.event_bus.subscribe("turn.done", turns.append)
        await pilot.press(*list("run"))
        await pilot.press("enter")
        for _ in range(80):
            started = tool_parts_of(app)
            if started and started[0].tool_status is ToolStatus.RUNNING:
                break
            await pilot.pause(0.1)
        started = tool_parts_of(app)
        check(
            bool(started) and started[0].tool_status is ToolStatus.RUNNING,
            "命令开始执行时工具卡片进入执行中",
        )
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        check(not turns, "从界面中断之后没有 turn.done")
        finished = tool_parts_of(app)
        check(
            bool(finished)
            and finished[0].tool_status is ToolStatus.FAILED
            and finished[0].tool_output == "interrupted",
            f"界面把没结束的工具标成失败并写明原因：{finished[0].tool_output if finished else '没有工具 Part'}",
        )
        check(app.screen._busy is False, "中断之后界面回到可用状态")
        await pilot.pause(1.0)
        check(not marker.exists(), "中断把那条命令一起收掉了")

    state.unlink(missing_ok=True)


def check_ledger() -> None:
    """登记表：handle 类型、血缘、指纹、对账、快照往返。"""
    root = PROBE / "ledger_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    # 类型化 handle：拿错了必须是一条可读的中文错误，而不是内部判断。
    ledger = SessionLedger(session_id="", snapshot_path=root / "basic.json")
    receptor = ledger.add("receptor", summary="受体 A")
    ligandset = ledger.add("ligandset", summary="原始库 5000 个")
    check(str(receptor.handle) == "receptor#1", "第一个受体是 receptor#1")
    check(str(ligandset.handle) == "ligandset#1", "配体集与受体各自计数")
    check(
        ledger.current_receptor() == receptor.handle,
        "第一个受体自动成为当前受体",
    )
    check(
        [str(item) for item in ligandset.parents] == ["receptor#1"],
        "非受体条目登记时自动挂当前受体为父项",
    )
    detached = ledger.add(
        "ligandset", summary="跨靶点配体库", link_current_receptor=False
    )
    check(
        detached.parents == (),
        "link_current_receptor=False 时不把当前受体挂成父项",
    )

    wrong = None
    try:
        expect_handle(ligandset.handle, ReceptorHandle, "receptor")
    except LedgerError as error:
        wrong = str(error)
    check(
        wrong is not None and "要的是 receptor" in wrong and "ligandset#1" in wrong,
        f"类型错配报出「手上那个是什么」：{wrong}",
    )

    bad = None
    try:
        parse_handle("docking/out.csv")
    except LedgerError as error:
        bad = str(error)
    check(
        bad is not None and "不是合法的 handle" in bad,
        f"路径当 handle 传进来时给格式说明：{bad}",
    )

    # 收 Entry 也能当父项：add 返回的就是 Entry，直接传回去不该被拦。
    from_entry = ledger.add("artifact", parents=[ligandset], summary="打分表")
    check(
        [str(item) for item in from_entry.parents][0] == "ligandset#1",
        "add 返回的 Entry 可以直接当父项传回去",
    )

    # 血缘隔层隔离：一个不相关的受体下的产物，不该被算成本受体的失效项。
    other_receptor = ledger.add("receptor", summary="受体 B")
    check(
        ledger.current_receptor() == receptor.handle,
        "再登记一个受体不会自动把当前受体换掉（切换是 switch_receptor 的事）",
    )

    # 预演 == 真做：同一时刻两份清单必须逐条相同。
    preview = sorted(str(item.handle) for item in ledger.stale_after(receptor.handle))
    switched = sorted(str(item.handle) for item in ledger.switch_receptor(other_receptor.handle))
    check(
        preview == switched and "receptor#1" in preview,
        f"预演与真做返回同一批：{preview} 对 {switched}",
    )
    check(
        receptor.handle in [item.handle for item in ledger.entries(include_archived=True)],
        "切换后旧受体仍留在表里（搁置而不是删除）",
    )
    check(
        len(ledger) == len(ledger.entries(include_archived=True)),
        "搁置的条目仍然在表里计数，只是默认视图把它藏起来",
    )
    check(
        ledger.is_archived(receptor.handle) and ledger.is_archived(from_entry.handle),
        "属于旧受体的条目被搁置",
    )
    check(
        not ledger.is_archived(other_receptor.handle),
        "新受体自己不被搁置",
    )
    check(
        not ledger.is_archived(detached.handle),
        "跨靶点配体库不随受体切换被搁置",
    )
    check(
        ledger.current_receptor() == other_receptor.handle,
        "切换后当前受体是新那个",
    )

    # 再切一次当前受体是空操作：不能因为重复点一次就把工作区清空。
    again = ledger.switch_receptor(other_receptor.handle)
    check(
        again == [] and not ledger.is_archived(other_receptor.handle),
        "切到当前受体自身是空操作，不会把它自己搁置起来",
    )

    # 对账四路：没有 path / 路径不在 / 没有指纹 / 指纹不一致。
    memory_only = ledger.add("artifact", summary="纯内存对象")
    gone = ledger.add("artifact", path=root / "never_written.txt", summary="只登记不写盘", fingerprint="deadbeef")
    no_fp = root / "no_fingerprint.txt"
    no_fp.write_text("内容", encoding="utf-8")
    no_fp_entry = ledger.add("artifact", path=no_fp, summary="没记指纹")
    changed_path = root / "changing.txt"
    changed_path.write_text("第一版", encoding="utf-8")
    recorded = fingerprint_path(changed_path)
    ledger.add("artifact", path=changed_path, fingerprint=recorded, summary="记了指纹")
    changed_path.write_text("第二版", encoding="utf-8")

    changed = {str(item.handle): item.file_state for item in ledger.reconcile()}
    check(
        str(memory_only.handle) not in changed,
        "纯内存对象（无 path）不会因对账而改变状态",
    )
    check(
        str(gone.handle) in changed and changed[str(gone.handle)] == "missing",
        f"路径不在的条目判成 missing：{changed}",
    )
    check(
        str(gone.handle) in [str(item.handle) for item in ledger.entries()],
        "missing 的条目不从表里删掉（它曾经存在是事实）",
    )
    check(
        str(no_fp_entry.handle) not in changed,
        "有 path 但没记指纹的条目只能确认「还在」，不算状态变化",
    )
    stale_entry = next(item for item in ledger.entries() if item.path == changed_path)
    check(
        stale_entry.file_state == "stale",
        f"内容变了判成 stale：{stale_entry.file_state}",
    )

    # 目录指纹：文件名与内容都进哈希，空子目录也算一条。
    group = root / "prepared"
    group.mkdir()
    (group / "receptor.pdb").write_text("ATOM\n", encoding="utf-8")
    (group / "box.txt").write_text("20 20 20\n", encoding="utf-8")
    dir_fp = fingerprint_path(group)
    (group / "logs").mkdir()
    check(
        fingerprint_path(group) != dir_fp,
        "往产物目录里加一个空子目录，指纹跟着变",
    )
    (group / "logs").rmdir()
    check(
        fingerprint_path(group) == dir_fp,
        "空子目录拿掉之后指纹回到原值",
    )
    (group / "box.txt").rename(group / "box2.txt")
    check(
        fingerprint_path(group) != dir_fp,
        "只改文件名不改内容，指纹也变（盒子那类静默错误靠这条挡住）",
    )

    # 快照往返：子类还原、计数器不撞号、坏记录跳过。
    snapshot_path = root / "roundtrip.json"
    keeper = SessionLedger(session_id="ses_ledger", snapshot_path=snapshot_path)
    r1 = keeper.add("receptor", summary="受体甲")
    ls1 = keeper.add("ligandset", summary="库")
    artifact = keeper.add("artifact", parents=[ls1], summary="结果")
    r2 = keeper.add("receptor", summary="受体乙")
    keeper.switch_receptor(r2.handle)

    restored = SessionLedger(session_id="ses_ledger", snapshot_path=snapshot_path)
    check(restored.load(), "快照能读回来")
    entries = {str(item.handle): item for item in restored.entries(include_archived=True)}
    check(
        isinstance(entries["receptor#1"].handle, ReceptorHandle)
        and isinstance(entries["ligandset#1"].handle, LigandSetHandle)
        and isinstance(entries["artifact#1"].handle, ArtifactHandle),
        "往返之后 handle 还是各自的子类",
    )
    check(
        [str(item.handle) for item in entries["artifact#1"].parents] == ["ligandset#1", "receptor#1"],
        "往返之后血缘还在（显式父项与自动挂上的当前受体都在）",
    )
    check(
        restored.current_receptor() == r2.handle,
        "往返之后当前受体还原成同一子类与同一个编号",
    )
    check(
        {str(item.handle) for item in restored.entries(include_archived=True) if restored.is_archived(item.handle)}
        == {"receptor#1", "ligandset#1", "artifact#1"},
        "往返之后搁置集合原样还原",
    )
    fresh = restored.add("ligandset", summary="新增")
    check(
        str(fresh.handle) == "ligandset#2",
        f"往返之后新登记的序号接着旧计数：{fresh.handle}",
    )

    bad_snapshot = {
        "version": 1,
        "counters": {"receptor": 1, "ligandset": 1, "artifact": 1},
        "entries": [
            {"handle": {"kind": "receptor", "seq": 1}, "summary": "好记录"},
            {"handle": {"kind": "不存在", "seq": 9}, "summary": "坏记录"},
            {"handle": {"kind": "ligandset", "seq": 1}, "summary": "好记录二"},
        ],
    }
    tolerant = SessionLedger(session_id="", snapshot_path=root / "tolerant.json")
    count = tolerant.load_snapshot(bad_snapshot)
    check(
        count == 2 and len(tolerant) == 2,
        f"一条坏记录只跳它自己，不废掉整份索引：读进 {count} 条",
    )

    shutil.rmtree(root, ignore_errors=True)


def check_products() -> None:
    """内容寻址产物目录：复用、参数变化、半成品、原子写。"""
    root = PROBE / "products_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        params = {"seed": 42, "limit": 10}
        required = ("manifest.json", "params.json")
        target, reused = products_module.content_dir(root, "lib", params, required)
        check(
            not reused and not target.exists(),
            "第一次给的是新目录，且不替调用方建目录",
        )
        products_module.write_json_atomic(target / "manifest.json", {"ok": True})
        products_module.write_json_atomic(target / "params.json", params)

        again, reused = products_module.content_dir(root, "lib", params, required)
        check(reused and again == target, "同参数且产物齐全时复用同一目录")

        changed, reused = products_module.content_dir(
            root, "lib", {"seed": 7, "limit": 10}, required
        )
        check(
            not reused and changed != target,
            "参数不同时换新目录，不复用旧产物",
        )

        (target / "manifest.json").unlink()
        incomplete, reused = products_module.content_dir(root, "lib", params, required)
        check(
            not reused and incomplete.name.endswith("-2"),
            f"参数相同但产物不齐时不复用半成品，改用 -2：{incomplete.name}",
        )

        atomic = root / "atomic.json"
        products_module.write_json_atomic(atomic, {"中文": "值"})
        check(
            json.loads(atomic.read_text(encoding="utf-8")) == {"中文": "值"},
            "原子写出的 JSON 能原样读回",
        )
        check(
            not list(root.glob(".atomic.json.*.tmp")),
            "原子写不留下临时文件",
        )
        check(
            products_module.safe_stem("lib 01/x") == "lib_01_x",
            "显示名里的空格与斜杠被压成下划线",
        )
        check(
            products_module.safe_stem("中文") == "product",
            "全是不安全字符时回落到默认 stem",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def check_ligand_import() -> None:
    """导入配体集：格式、去盐、去重、截断、失败与复用。

    只替换化学清洗那一段（`canonicalize_smiles`）与 SDF 读取：这两处要真 RDKit，
    而本机判据不依赖具体化学实现。文件读取、产物目录、写盘、登记全部是真的。
    """
    work = PROBE / "ligand_import"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_dir = ligand_module.ligands_dir
    original_canon = ligand_module.canonicalize_smiles
    original_sdf = ligand_module._read_sdf
    original_probe = chemistry_module._probe_deps

    def fake_canonical(raw: str) -> ligand_module.Cleaned:
        text = str(raw).strip()
        if not text or text.startswith("-") or "bad" in text:
            raise ligand_module.LigandError("SMILES 解析失败")
        parts = text.split(".")
        main = parts[0]
        heavy = sum(1 for char in main if char.isalpha() and char.isupper())
        return ligand_module.Cleaned(
            input_smiles=text,
            smiles=main,
            heavy_atoms=heavy,
            fragments_removed=len(parts) - 1,
            warnings=(f"去掉了 {len(parts) - 1} 个片段",) if len(parts) > 1 else (),
        )

    try:
        ligand_module.ligands_dir = lambda: work / "ligands"
        ligand_module.canonicalize_smiles = fake_canonical
        chemistry_module._probe_deps = lambda: ()

        tool = ligand_module.ImportLigandsetTool()
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        # ---- 一、CSV：ChEMBL 列、去重、无效 ----
        library = work / "library.csv"
        library.write_text(
            "molecule_chembl_id,molecule_pref_name,smiles,pchembl_value\n"
            "CHEMBL1,aspirin,CCO,5.0\n"
            "CHEMBL2,caffeine,CCN,6.0\n"
            "CHEMBL3,duplicate,CCO,4.0\n"
            "CHEMBL4,broken,bad-smiles,3.0\n",
            encoding="utf-8",
        )
        result = await tool.execute({"path": str(library)}, context=ledger)
        check(not result.failed, f"CSV 导入成功：{result.output}")
        check(
            result.meta["count"] == 2
            and result.meta["duplicate"] == 1
            and result.meta["invalid"] == 1,
            f"可用 2、重复 1、无效 1：{result.meta}",
        )
        handle = result.meta["handle"]
        entry = ledger.get(handle)
        check(
            isinstance(entry.handle, LigandSetHandle),
            "导入登记成 ligandset",
        )
        check(
            entry.meta.get("stage") == "raw" and entry.path is not None and entry.path.is_dir(),
            "原始配体集登记成 raw，path 是产物目录",
        )
        check(
            entry.parents == (),
            "配体库不挂当前受体（换靶点不该被搁置）",
        )
        rows = list(csv.DictReader((entry.path / "ligands.csv").open(encoding="utf-8")))
        check(
            len(rows) == 2,
            f"CSV 只写可用分子：{len(rows)} 行",
        )
        check(
            rows[0]["id"] == "CHEMBL1" and rows[0]["name"] == "aspirin",
            f"保留 ChEMBL 的 id 与名称：{rows[0]}",
        )
        check(
            rows[0]["smiles"] == "CCO" and rows[0]["heavy_atoms"] == "3",
            f"canonical SMILES 与重原子数写进 CSV：{rows[0]}",
        )
        manifest = json.loads((entry.path / "manifest.json").read_text(encoding="utf-8"))
        check(
            manifest["counts"]["input"] == 4 and manifest["counts"]["accepted"] == 2,
            f"manifest 计数与 CSV 一致：{manifest['counts']}",
        )
        check(
            {item["reason"] for item in manifest["skipped"]} == {"duplicate", "invalid_smiles"},
            f"跳过原因逐条记下：{manifest['skipped']}",
        )

        # ---- 二、SMI：盐被去掉、注释与空行跳过 ----
        salts = work / "salts.smi"
        salts.write_text(
            "# 注释行\n"
            "CC(=O)[O-].[Na+] sodium_acetate\n"
            "\n"
            "CCO ethanol\n",
            encoding="utf-8",
        )
        salt_result = await tool.execute({"path": str(salts)}, context=ledger)
        check(not salt_result.failed, f"SMI 导入成功：{salt_result.output}")
        salt_entry = ledger.get(salt_result.meta["handle"])
        salt_rows = list(
            csv.DictReader((salt_entry.path / "ligands.csv").open(encoding="utf-8"))
        )
        salt_row = next(row for row in salt_rows if row["id"] == "sodium_acetate")
        check(
            salt_row["smiles"] == "CC(=O)[O-]" and "Na" not in salt_row["smiles"],
            f"盐只留最大片段：{salt_row}",
        )
        check(
            len(salt_rows) == 2,
            f"注释与空行不占记录：{len(salt_rows)} 行",
        )

        # ---- 三、limit：截断文案与计数 ----
        limited = await tool.execute({"path": str(library), "limit": 1}, context=ledger)
        check(
            not limited.failed and limited.meta["count"] == 1 and limited.meta["remaining"] == 1,
            f"limit 截断后只留 1 个、还剩 1 个：{limited.meta}",
        )
        check(
            "没有导入" in limited.output and "limit" in limited.output,
            f"截断时说清还剩多少、怎么接着做：{limited.output}",
        )

        # ---- 四、全部无效：失败且不登记 ----
        before = len(ledger)
        broken = work / "broken.csv"
        broken.write_text("smiles\nbad-1\nbad-2\n", encoding="utf-8")
        failed = await tool.execute({"path": str(broken)}, context=ledger)
        check(
            failed.failed and "没有可导入的分子" in failed.output,
            f"全部无效时失败并说清：{failed.output}",
        )
        check(len(ledger) == before, "全部无效时登记表不增加")

        # ---- 五、后缀与列名：失败要能指出可走的路 ----
        notes = work / "notes.txt"
        notes.write_text("hello\n", encoding="utf-8")
        wrong_ext = await tool.execute({"path": str(notes)}, context=ledger)
        check(
            wrong_ext.failed and ".sdf" in wrong_ext.output and ".csv" in wrong_ext.output,
            f"不认识的扩展名列清能收的：{wrong_ext.output}",
        )
        wrong_column = await tool.execute(
            {"path": str(library), "smiles_column": "nope"}, context=ledger
        )
        check(
            wrong_column.failed and "没有指定的 SMILES 列" in wrong_column.output,
            f"指定列不存在时列实际表头：{wrong_column.output}",
        )
        custom = work / "custom.csv"
        custom.write_text("id,name,molecule\nX,ex,CCO\n", encoding="utf-8")
        custom_result = await tool.execute(
            {"path": str(custom), "smiles_column": "molecule"}, context=ledger
        )
        check(
            not custom_result.failed and custom_result.meta["count"] == 1,
            f"smiles_column 能指定非默认列：{custom_result.output}",
        )

        # ---- 六、复用：同内容同参数不重复登记 ----
        count_before = len(
            [item for item in ledger.entries(include_archived=True)
             if isinstance(item.handle, LigandSetHandle)]
        )
        again = await tool.execute({"path": str(library)}, context=ledger)
        count_after = len(
            [item for item in ledger.entries(include_archived=True)
             if isinstance(item.handle, LigandSetHandle)]
        )
        check(
            again.meta["handle"] == handle,
            f"同内容同参数回到同一个 handle：{again.meta['handle']} 对 {handle}",
        )
        check(
            count_after == count_before and "未重复导入" in again.output,
            f"不重复登记，输出也说明：{again.output.splitlines()[0]}",
        )

        # ---- 七、SDF 分派：替换读取函数即可覆盖，不依赖真 RDKit ----
        def fake_read_sdf(path: Path) -> list[ligand_module.RawLigand]:
            return [
                ligand_module.RawLigand(
                    index=1, id="SDF1", name="sdf-one", smiles="CCO", source_ref="第 1 条记录"
                ),
                ligand_module.RawLigand(
                    index=2, id="SDF2", name="sdf-two", smiles="bad", source_ref="第 2 条记录"
                ),
            ]

        ligand_module._read_sdf = fake_read_sdf
        sdf = work / "library.sdf"
        sdf.write_text("fake sdf\n", encoding="utf-8")
        sdf_result = await tool.execute({"path": str(sdf)}, context=ledger)
        check(
            not sdf_result.failed
            and sdf_result.meta["count"] == 1
            and sdf_result.meta["invalid"] == 1,
            f"SDF 走同一条清洗链：{sdf_result.meta}",
        )
        _, sdf_skipped, sdf_counts = ligand_module._clean_records(
            [
                ligand_module.RawLigand(
                    index=1,
                    id="X",
                    name="",
                    smiles="",
                    source_ref="第 1 条记录",
                    problem="SDF 记录解析失败（sanitize 没通过）",
                )
            ],
            limit=10,
        )
        check(
            sdf_counts["invalid"] == 1
            and sdf_skipped[0]["reason"] == "sdf_parse_failed",
            f"读取阶段就坏掉的记录单独一档原因：{sdf_skipped}",
        )

        # ---- 八、接口与装配 ----
        properties = tool.spec.parameters.get("properties") or {}
        check(
            tool.spec.parameters.get("required") == ["path"],
            "path 是唯一必填参数",
        )
        check(
            "ledger" not in properties,
            "参数模式里不列 ledger（那是内核注入的）",
        )
        check(
            tool.spec.parameters.get("additionalProperties") is False,
            "不认识的参数直接拒绝",
        )
        check(
            chemistry_module.CHEM_TOOL_DESCRIPTION_NOTE in tool.spec.description,
            "描述里带了「需要化学依赖」那句",
        )
        missing = await tool.execute({"path": str(library)}, context=None)
        check(
            missing.failed and "内核装配问题" in missing.output,
            f"没拿到登记表时报装配问题：{missing.output}",
        )
        no_file = await tool.execute({"path": str(work / "nope.csv")}, context=ledger)
        check(
            no_file.failed and "找不到" in no_file.output,
            f"文件不存在时说清：{no_file.output}",
        )
        as_dir = await tool.execute({"path": str(work)}, context=ledger)
        check(
            as_dir.failed and "不是目录" in as_dir.output,
            f"传目录时说清要的是文件：{as_dir.output}",
        )
        bad_limit = await tool.execute({"path": str(library), "limit": 0}, context=ledger)
        check(
            bad_limit.failed and "1 到" in bad_limit.output,
            f"limit 越界时给出范围：{bad_limit.output}",
        )
    finally:
        ligand_module.ligands_dir = original_dir
        ligand_module.canonicalize_smiles = original_canon
        ligand_module._read_sdf = original_sdf
        chemistry_module._probe_deps = original_probe
        shutil.rmtree(work, ignore_errors=True)


async def check_ligand_prepare() -> None:
    """准备配体：预演、ETKDG/Meeko 失败分档、复用、seed、limit、提交标记。"""
    work = PROBE / "ligand_prepare"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_dir = ligand_module.ligands_dir
    original_prepare = ligand_module.prepare_molecule
    original_probe = chemistry_module._probe_deps
    calls: list[tuple[str, int]] = []

    def fake_prepare(cleaned: ligand_module.Cleaned, *, seed: int) -> ligand_module.PreparedMolecule:
        calls.append((cleaned.smiles, seed))
        if cleaned.smiles == "CCN":
            raise ligand_module.MeekoError("Meeko 没有生成任何 setup")
        if cleaned.smiles == "CCCl":
            raise ligand_module.EmbedError("3D 构象生成失败（ETKDGv3 两次都没嵌进去）")
        return ligand_module.PreparedMolecule(
            pdbqt=f"REMARK SMILES {cleaned.smiles}\nTORSDOF 0\n",
            heavy_atoms=cleaned.heavy_atoms,
            forcefield="MMFF94",
            setups=1,
        )

    def make_raw(name: str, rows: list[tuple[str, str]]) -> Path:
        directory = work / "raw" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(
            json.dumps({"kind": "ligandset", "stage": "raw"}), encoding="utf-8"
        )
        lines = [
            "id,name,smiles,input_smiles,heavy_atoms,pchembl_value,source_index",
            *[
                f"{identifier},{identifier},{smiles},{smiles},3,5.0,{index}"
                for index, (identifier, smiles) in enumerate(rows, start=1)
            ],
        ]
        (directory / "ligands.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return directory

    try:
        ligand_module.ligands_dir = lambda: work / "ligands"
        ligand_module.prepare_molecule = fake_prepare
        chemistry_module._probe_deps = lambda: ()

        tool = ligand_module.PrepareLigandsTool()
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        source_dir = make_raw(
            "library-abc",
            [
                ("A", "CCO"),
                ("B", "CCN"),
                ("C", "CCO"),
                ("D", "CCC"),
            ],
        )
        source = ledger.add(
            "ligandset",
            path=source_dir,
            summary="原始库",
            fingerprint=fingerprint_path(source_dir),
            meta={"stage": "raw"},
            link_current_receptor=False,
        )

        # ---- 一、预演：不写盘、不登记、不跑 3D/Meeko ----
        preview = await tool.execute({"ligands": str(source.handle)}, context=ledger)
        check(not preview.failed, f"预演成功：{preview.output}")
        check(
            preview.meta.get("preview") is True and preview.meta.get("count") == 3,
            f"预演报出去重后的条数：{preview.meta}",
        )
        check(
            "预演" in preview.output and "confirm=true" in preview.output,
            f"预演说清下一步：{preview.output}",
        )
        check(calls == [], "预演不跑 3D/Meeko")
        check(
            not (work / "ligands" / "prepared").exists(),
            "预演不建产物目录",
        )

        # ---- 二、confirm：成功与失败分档，重复不占名额 ----
        result = await tool.execute(
            {"ligands": str(source.handle), "confirm": True}, context=ledger
        )
        check(not result.failed, f"准备成功：{result.output}")
        check(
            result.meta.get("count") == 2 and result.meta.get("failed") == 1,
            f"成功 2、失败 1：{result.meta}",
        )
        handle = result.meta["handle"]
        entry = ledger.get(handle)
        check(
            isinstance(entry.handle, LigandSetHandle)
            and entry.meta.get("stage") == "prepared",
            "准备后登记成 prepared ligandset",
        )
        check(entry.parents == (), "准备后的配体库仍不挂当前受体")
        check(
            entry.path is not None
            and (entry.path / "manifest.json").is_file()
            and (entry.path / "params.json").is_file(),
            "产物目录有 manifest 与 params 提交标记",
        )
        manifest = json.loads((entry.path / "manifest.json").read_text(encoding="utf-8"))
        counts = manifest["counts"]
        check(
            counts["input"] == 4
            and counts["unique"] == 3
            and counts["duplicate"] == 1
            and counts["selected"] == 3
            and counts["prepared"] == 2
            and counts["meeko_failed"] == 1,
            f"计数覆盖去重、截断与失败：{counts}",
        )
        pdbqts = sorted(item.name for item in (entry.path / "ligands").glob("*.pdbqt"))
        check(
            pdbqts == ["A.pdbqt", "D.pdbqt"],
            f"只给成功的分子写 PDBQT：{pdbqts}",
        )
        statuses = {item["id"]: item["status"] for item in manifest["records"]}
        check(
            statuses == {"A": "ok", "B": "meeko_failed", "D": "ok"},
            f"逐条记下成功与失败原因：{statuses}",
        )
        check(
            calls == [("CCO", 42), ("CCN", 42), ("CCC", 42)],
            f"按源顺序准备、重复的跳过：{calls}",
        )

        # ---- 三、同参数复用：不重跑 3D/Meeko ----
        before_calls = len(calls)
        again = await tool.execute(
            {"ligands": str(source.handle), "confirm": True}, context=ledger
        )
        check(
            not again.failed and again.meta["handle"] == handle,
            f"同参数回到同一个 handle：{again.meta}",
        )
        check(len(calls) == before_calls, "同参数复用不重跑 3D/Meeko")
        check("未重复准备" in again.output, f"输出说明复用：{again.output}")

        # ---- 四、换 seed：新目录、旧产物还在 ----
        seeded = await tool.execute(
            {"ligands": str(source.handle), "confirm": True, "seed": 7}, context=ledger
        )
        check(
            not seeded.failed and seeded.meta["handle"] != handle,
            f"换 seed 生成另一份准备产物：{seeded.meta}",
        )
        check(
            len(calls) > before_calls and all(seed == 7 for _, seed in calls[before_calls:]),
            f"新 seed 真的传下去了：{calls[before_calls:]}",
        )
        check(entry.path.is_dir(), "换 seed 不覆盖旧产物")

        # ---- 五、limit：只准备前 N 个，并说明还剩多少 ----
        limited = await tool.execute(
            {"ligands": str(source.handle), "confirm": True, "limit": 1}, context=ledger
        )
        check(
            not limited.failed and limited.meta.get("count") == 1,
            f"limit 只准备 1 个：{limited.meta}",
        )
        limited_entry = ledger.get(limited.meta["handle"])
        limited_manifest = json.loads(
            (limited_entry.path / "manifest.json").read_text(encoding="utf-8")
        )
        check(
            limited_manifest["counts"]["remaining"] == 2
            and "没有处理" in limited.output,
            f"截断说清还剩 2 个：{limited_manifest['counts']}",
        )

        # ---- 六、已经 prepared 的 handle 拒绝重复准备 ----
        reject = await tool.execute({"ligands": handle}, context=ledger)
        check(
            reject.failed and "已经准备过" in reject.output,
            f"prepared handle 被拒：{reject.output}",
        )

        # ---- 七、全部失败：不登记 ----
        fail_dir = make_raw("only-bad", [("E", "CCCl")])
        fail_source = ledger.add(
            "ligandset",
            path=fail_dir,
            summary="全失败",
            fingerprint=fingerprint_path(fail_dir),
            meta={"stage": "raw"},
            link_current_receptor=False,
        )
        before_entries = len(ledger)
        fail_result = await tool.execute(
            {"ligands": str(fail_source.handle), "confirm": True}, context=ledger
        )
        check(
            fail_result.failed and "没有准备出任何 PDBQT" in fail_result.output,
            f"全部失败时失败并给出分档：{fail_result.output}",
        )
        check(len(ledger) == before_entries, "全部失败时不登记")

        # ---- 八、删掉 params.json：提交标记失效，不复用 ----
        assert entry.path is not None
        (entry.path / "params.json").unlink()
        rebuilt = await tool.execute(
            {"ligands": str(source.handle), "confirm": True}, context=ledger
        )
        check(
            not rebuilt.failed and rebuilt.meta["handle"] != handle,
            f"缺 params.json 时不复用旧目录：{rebuilt.meta}",
        )

        # ---- 九、参数与装配 ----
        properties = tool.spec.parameters.get("properties") or {}
        check(
            "ligands" in (tool.spec.parameters.get("required") or []),
            "ligands 是必填",
        )
        check(
            "ledger" not in properties,
            "参数模式里不列 ledger（那是内核注入的）",
        )
        check(
            tool.spec.parameters.get("additionalProperties") is False,
            "不认识的参数直接拒绝",
        )
        check(
            chemistry_module.CHEM_TOOL_DESCRIPTION_NOTE in tool.spec.description,
            "描述里带了「需要化学依赖」那句",
        )
        no_handle = await tool.execute({"ligands": ""}, context=ledger)
        check(no_handle.failed and "缺少参数" in no_handle.output, "空 handle 被拦下")
        missing = await tool.execute({"ligands": str(source.handle)}, context=None)
        check(
            missing.failed and "内核装配问题" in missing.output,
            f"没拿到登记表时报装配问题：{missing.output}",
        )
        bad_limit = await tool.execute(
            {"ligands": str(source.handle), "limit": 0}, context=ledger
        )
        check(
            bad_limit.failed and "1 到" in bad_limit.output,
            f"limit 越界时给出范围：{bad_limit.output}",
        )
        bad_seed = await tool.execute(
            {"ligands": str(source.handle), "seed": -1}, context=ledger
        )
        check(
            bad_seed.failed and "0 到" in bad_seed.output,
            f"seed 越界时给出范围：{bad_seed.output}",
        )
        bad_confirm = await tool.execute(
            {"ligands": str(source.handle), "confirm": "yes"}, context=ledger
        )
        check(
            bad_confirm.failed and "true 或 false" in bad_confirm.output,
            f"confirm 不是布尔时拒绝：{bad_confirm.output}",
        )

        # ---- 十、缺化学依赖：失败且不写盘 ----
        chemistry_module._probe_deps = lambda: ("rdkit",)
        before = len(ledger)
        blocked = await tool.execute(
            {"ligands": str(source.handle), "confirm": True}, context=ledger
        )
        check(
            blocked.failed and "本机缺化学依赖" in blocked.output,
            f"缺依赖时给出安装指引：{blocked.output}",
        )
        check(len(ledger) == before, "缺依赖时不登记")
    finally:
        ligand_module.ligands_dir = original_dir
        ligand_module.prepare_molecule = original_prepare
        chemistry_module._probe_deps = original_probe
        shutil.rmtree(work, ignore_errors=True)


async def check_docking() -> None:
    """对接：Vina 定位、盒子、命令构造、失败分档、分数排序、artifact 与复用。"""
    work = PROBE / "docking_tool"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_dir = docking_module.dockings_dir
    original_resolve = docking_module.resolve_vina
    original_run = docking_module.run_vina
    original_convert = docking_module.best_pose_to_pdb
    original_probe = chemistry_module._probe_deps
    runs: list[tuple[str, ...]] = []

    async def fake_run_vina(
        argv, *, cwd, timeout, log_path
    ) -> docking_module.VinaRun:
        runs.append(tuple(argv))
        ligand_path = Path(argv[argv.index("--ligand") + 1])
        out_path = Path(argv[argv.index("--out") + 1])
        stem = ligand_path.stem
        if stem == "B":
            log_path.write_text("boom\n", encoding="utf-8")
            return docking_module.VinaRun(tuple(argv), 1, "boom", False, log_path)
        if stem == "C":
            log_path.write_text("timeout\n", encoding="utf-8")
            return docking_module.VinaRun(tuple(argv), -9, "timeout", True, log_path)
        if stem == "D":
            out_path.write_text("ATOM\n", encoding="utf-8")
            log_path.write_text("no result\n", encoding="utf-8")
            return docking_module.VinaRun(tuple(argv), 0, "no result", False, log_path)
        score = -8.0 if stem == "A" else -6.0
        out_path.write_text(
            f"MODEL 1\nREMARK VINA RESULT: {score} 0.0 0.0\nATOM\nENDMDL\n"
            "MODEL 2\nREMARK VINA RESULT: -5.0 0.1 0.2\nATOM\nENDMDL\n",
            encoding="utf-8",
        )
        log_path.write_text("ok\n", encoding="utf-8")
        return docking_module.VinaRun(tuple(argv), 0, "ok", False, log_path)

    def fake_convert(pose_pdbqt: Path, dest: Path) -> bool:
        if pose_pdbqt.stem.startswith("E"):
            return False
        dest.write_text(
            "ATOM      1  C   LIG A   1       0.000   0.000   0.000  1.00  0.00           C\n",
            encoding="utf-8",
        )
        return True

    def make_ligandset(name: str, ids: list[str]) -> Path:
        directory = work / "ligands" / name
        (directory / "ligands").mkdir(parents=True, exist_ok=True)
        records = []
        for index, identifier in enumerate(ids, start=1):
            relative = f"ligands/{identifier}.pdbqt"
            (directory / relative).write_text(f"REMARK {identifier}\n", encoding="utf-8")
            records.append(
                {
                    "id": identifier,
                    "name": identifier.lower(),
                    "smiles": "CCO",
                    "heavy_atoms": 3,
                    "source_index": index,
                    "status": "ok",
                    "pdbqt": relative,
                    "forcefield": "MMFF94",
                    "setups": 1,
                    "error": "",
                    "warnings": [],
                }
            )
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "kind": "ligandset",
                    "stage": "prepared",
                    "records": records,
                    "counts": {},
                }
            ),
            encoding="utf-8",
        )
        return directory

    try:
        docking_module.dockings_dir = lambda: work / "docking"
        docking_module.resolve_vina = lambda: docking_module.VinaBinary("/fake/vina", "1.2.5")
        docking_module.run_vina = fake_run_vina
        docking_module.best_pose_to_pdb = fake_convert
        chemistry_module._probe_deps = lambda: ()

        tool = docking_module.DockTool()
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        receptor_dir = work / "receptor"
        receptor_dir.mkdir(parents=True, exist_ok=True)
        (receptor_dir / "receptor.pdbqt").write_text("REMARK receptor\n", encoding="utf-8")
        (receptor_dir / "box.txt").write_text(
            "center_x = 1.0\ncenter_y = 2.0\ncenter_z = 3.0\n"
            "size_x = 20.0\nsize_y = 21.0\nsize_z = 22.0\n",
            encoding="utf-8",
        )
        receptor = ledger.add(
            "receptor",
            path=receptor_dir,
            summary="prepared receptor",
            fingerprint=fingerprint_path(receptor_dir),
            meta={
                "stage": "prepared",
                "pdbqt_file": "receptor.pdbqt",
                "box_file": "box.txt",
            },
        )
        ligand_dir = make_ligandset("five", ["A", "B", "C", "D", "E"])
        ligand_set = ledger.add(
            "ligandset",
            path=ligand_dir,
            summary="prepared ligands",
            fingerprint=fingerprint_path(ligand_dir),
            meta={"stage": "prepared"},
            link_current_receptor=False,
        )

        # ---- 一、整批：成功、无 PDB、非零退出、超时、无结果 ----
        result = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "ligands": str(ligand_set.handle),
                "limit": 5,
            },
            context=ledger,
        )
        check(not result.failed, f"对接成功：{result.output}")
        check(
            result.meta.get("docked") == 2
            and result.meta.get("pdb") == 1
            and result.meta.get("failed") == 4
            and result.meta.get("best_score") == -8.0,
            f"计数与最佳分数正确：{result.meta}",
        )
        handle = result.meta["handle"]
        entry = ledger.get(handle)
        check(
            isinstance(entry.handle, ArtifactHandle)
            and entry.meta.get("kind") == "docking"
            and entry.meta.get("stage") == "docking",
            "登记成 artifact + kind=docking + stage=docking",
        )
        check(
            {str(item) for item in entry.parents}
            == {str(receptor.handle), str(ligand_set.handle)},
            f"父项挂受体与配体集：{[str(item) for item in entry.parents]}",
        )
        check(
            entry.path is not None
            and (entry.path / "manifest.json").is_file()
            and (entry.path / "params.json").is_file(),
            "产物目录有 manifest 与 params 提交标记",
        )
        manifest = json.loads((entry.path / "manifest.json").read_text(encoding="utf-8"))
        counts = manifest["counts"]
        check(
            counts
            == {
                "input": 5,
                "docked": 2,
                "pdb": 1,
                "failed": 4,
                "remaining": 0,
                "best_score": -8.0,
            },
            f"计数口径：{counts}",
        )
        results = manifest["results"]
        check(
            [item["id"] for item in results] == ["A", "E"],
            f"结果按分数升序：{[item['id'] for item in results]}",
        )
        check(
            results[0]["rank"] == 1
            and results[0]["ligand_efficiency"] == round(8.0 / 3, 3),
            f"rank 与配体效率由代码算：{results[0]}",
        )
        check(
            results[1]["status"] == "no_pdb" and results[1]["pose_pdb"] == "",
            "转不成 PDB 的记录保留分数但标 no_pdb",
        )
        reasons = {item["reason"] for item in manifest["failures"]}
        check(
            reasons == {"vina_exit_1", "timeout", "no_result", "no_pdb"},
            f"失败原因分档：{reasons}",
        )
        check(
            (entry.path / "poses" / "A_out.pdbqt").is_file()
            and (entry.path / "poses" / "A.pdb").is_file()
            and (entry.path / "logs" / "B.log").is_file(),
            "每个配体留下 PDBQT、最佳 PDB 与日志",
        )
        check(
            not (entry.path / "poses" / "E.pdb").exists(),
            "没转出 PDB 的记录不写空文件",
        )
        check(
            manifest["receptor"]["box"]["center"] == [1.0, 2.0, 3.0]
            and manifest["receptor"]["box"]["size"] == [20.0, 21.0, 22.0],
            f"盒子原样写进 manifest：{manifest['receptor']['box']}",
        )
        check(
            manifest["engine"]["version"] == "1.2.5",
            f"引擎版本进 manifest：{manifest['engine']}",
        )

        # ---- 二、同参数复用：不重跑 Vina ----
        before_runs = len(runs)
        again = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "ligands": str(ligand_set.handle),
                "limit": 5,
            },
            context=ledger,
        )
        check(
            not again.failed
            and again.meta.get("handle") == handle
            and again.meta.get("reused") is True,
            f"同参数复用同一 artifact：{again.meta}",
        )
        check(len(runs) == before_runs, "复用不重跑 Vina")
        check("未重复对接" in again.output, f"输出说明复用：{again.output}")

        # ---- 三、limit：只跑前 N 个并说明未处理 ----
        limited = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "ligands": str(ligand_set.handle),
                "limit": 1,
            },
            context=ledger,
        )
        check(
            not limited.failed
            and limited.meta.get("docked") == 1
            and limited.meta.get("remaining") == 4,
            f"limit 只跑 1 个、剩 4 个：{limited.meta}",
        )
        check("未处理 4" in limited.output, f"截断说清剩多少：{limited.output}")

        # ---- 四、全部失败：不登记 ----
        fail_dir = make_ligandset("only-b", ["B"])
        fail_set = ledger.add(
            "ligandset",
            path=fail_dir,
            summary="only bad",
            fingerprint=fingerprint_path(fail_dir),
            meta={"stage": "prepared"},
            link_current_receptor=False,
        )
        before_entries = len(ledger)
        failed = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(fail_set.handle)},
            context=ledger,
        )
        check(
            failed.failed and "没有产生可用的对接结果" in failed.output,
            f"全部失败时失败并给原因：{failed.output}",
        )
        check(len(ledger) == before_entries, "全部失败时不登记 artifact")

        # ---- 五、阶段检查：raw 受体 / raw 配体集 ----
        raw_ligand = ledger.add(
            "ligandset",
            path=work / "raw-ligands",
            summary="raw",
            meta={"stage": "raw"},
            link_current_receptor=False,
        )
        raw_result = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(raw_ligand.handle)},
            context=ledger,
        )
        check(
            raw_result.failed and "prepared 状态" in raw_result.output,
            f"raw 配体集被拒：{raw_result.output}",
        )
        raw_receptor = ledger.add(
            "receptor",
            path=work / "raw.pdb",
            summary="raw receptor",
            meta={"stage": "raw"},
        )
        raw_receptor_result = await tool.execute(
            {"receptor": str(raw_receptor.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(
            raw_receptor_result.failed and "prepared 状态" in raw_receptor_result.output,
            f"raw 受体被拒：{raw_receptor_result.output}",
        )

        # ---- 六、缺 Vina：失败并给安装指引 ----
        def no_vina() -> docking_module.VinaBinary:
            raise ToolError(f"本机没有找到 AutoDock Vina。用 {docking_module.VINA_ENV} 指定。")

        docking_module.resolve_vina = no_vina
        missing_engine = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(
            missing_engine.failed and "VSA_VINA_BIN" in missing_engine.output,
            f"缺 Vina 时给安装指引：{missing_engine.output}",
        )
        docking_module.resolve_vina = lambda: docking_module.VinaBinary("/fake/vina", "1.2.5")

        # ---- 七、盒子与命令构造 ----
        box = docking_module.read_box(receptor_dir, receptor.meta)
        check(
            box.center == (1.0, 2.0, 3.0) and box.size == (20.0, 21.0, 22.0),
            f"box.txt 解析正确：{box}",
        )
        fallback = docking_module.read_box(
            receptor_dir,
            {
                "box_file": "missing.txt",
                "params": {"box_center": [1, 2, 3], "box_size": [10, 11, 12]},
            },
        )
        check(fallback.source == "meta.params", "box.txt 不在时回退 meta.params")
        bad_box = receptor_dir / "bad-box.txt"
        bad_box.write_text("center_x = 1\ncenter_y = 2\ncenter_z = 3\n", encoding="utf-8")
        try:
            docking_module.read_box(receptor_dir, {"box_file": "bad-box.txt"})
            check(False, "缺盒子字段应当失败")
        except ToolError as error:
            check("size_x" in str(error), f"缺字段时说清缺哪个：{error}")
        zero_box = receptor_dir / "zero-box.txt"
        zero_box.write_text(
            "center_x=1\ncenter_y=2\ncenter_z=3\nsize_x=0\nsize_y=1\nsize_z=1\n",
            encoding="utf-8",
        )
        try:
            docking_module.read_box(receptor_dir, {"box_file": "zero-box.txt"})
            check(False, "零边长应当失败")
        except ToolError as error:
            check("大于 0" in str(error), f"零边长时说清：{error}")

        params = docking_module.DockParams()
        argv = docking_module.build_command(
            "/fake/vina",
            Path("receptor.pdbqt"),
            Path("ligand.pdbqt"),
            Path("out.pdbqt"),
            box,
            params,
        )
        check(isinstance(argv, list) and argv[0] == "/fake/vina", "命令是参数数组，不是字符串")
        for flag in (
            "--receptor",
            "--ligand",
            "--center_x",
            "--center_y",
            "--center_z",
            "--size_x",
            "--size_y",
            "--size_z",
            "--exhaustiveness",
            "--num_modes",
            "--seed",
            "--cpu",
            "--energy_range",
            "--out",
        ):
            check(flag in argv, f"命令里有 {flag}")
        source = (
            ROOT / "opencode_tui" / "core" / "agent" / "domain" / "docking.py"
        ).read_text(encoding="utf-8")
        check("create_subprocess_shell" not in source, "对接不拼 shell")

        modes = docking_module.parse_vina_results(
            "REMARK VINA RESULT: -8.0 0.1 0.2\nREMARK VINA RESULT: -7.0 0.3 0.4\n"
        )
        check(
            [item.score for item in modes] == [-8.0, -7.0],
            f"多 mode 分数解析正确：{modes}",
        )
        check(docking_module.parse_vina_results("nothing") == [], "没有结果行时返回空表")

        # ---- 八、真实子进程：超时与进程树终止 ----
        timeout_run = await original_run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=work,
            timeout=0.2,
            log_path=work / "timeout.log",
        )
        check(timeout_run.timed_out, "超时被识别")
        check((work / "timeout.log").is_file(), "超时也留下日志文件")

        # ---- 九、参数与装配 ----
        properties = tool.spec.parameters.get("properties") or {}
        check(
            {"receptor", "ligands"}.issubset(
                set(tool.spec.parameters.get("required") or [])
            ),
            "receptor 与 ligands 都是必填",
        )
        check(
            "ledger" not in properties,
            "参数模式里不列 ledger（那是内核注入的）",
        )
        check(
            tool.spec.parameters.get("additionalProperties") is False,
            "不认识的参数直接拒绝",
        )
        check(
            chemistry_module.CHEM_TOOL_DESCRIPTION_NOTE in tool.spec.description,
            "描述里带了「需要化学依赖」那句",
        )
        check(
            "越低越好" in tool.spec.description,
            "描述第一段写清分数方向",
        )
        empty = await tool.execute({}, context=ledger)
        check(
            empty.failed and "缺少参数 receptor" in empty.output,
            f"空调用被拦下：{empty.output}",
        )
        missing_ledger = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle)},
            context=None,
        )
        check(
            missing_ledger.failed and "内核装配问题" in missing_ledger.output,
            f"没拿到登记表时报装配问题：{missing_ledger.output}",
        )
        for name, value, expected in (
            ("exhaustiveness", 0, "1 到 64"),
            ("num_modes", 0, "1 到 20"),
            ("limit", 0, "1 到 500"),
            ("timeout", 1, "10 到 3600"),
        ):
            bad = await tool.execute(
                {
                    "receptor": str(receptor.handle),
                    "ligands": str(ligand_set.handle),
                    name: value,
                },
                context=ledger,
            )
            check(
                bad.failed and expected in bad.output,
                f"{name} 越界时给出范围：{bad.output}",
            )
    finally:
        docking_module.dockings_dir = original_dir
        docking_module.resolve_vina = original_resolve
        docking_module.run_vina = original_run
        docking_module.best_pose_to_pdb = original_convert
        chemistry_module._probe_deps = original_probe
        shutil.rmtree(work, ignore_errors=True)


async def check_tool_context() -> None:
    """框架补口：Ctx 注入、ToolResult.meta、批量部分成功语义。"""
    injected: list[object] = []

    class NeedsContext(Tool):
        """标了 context_argument 的工具，验证内核注入。"""

        spec = ToolSpec(
            name="needs_context",
            description="需要一个由内核注入的对象",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
        context_argument = "ledger"

        async def run(self, arguments: dict[str, object]) -> ToolResult:
            injected.append(arguments.get("ledger"))
            return ToolResult(output=f"收到 {arguments.get('text')}")

    class Plain(Tool):
        """不声明 context_argument 的工具，验证不会被塞进多余参数。"""

        spec = ToolSpec(
            name="plain",
            description="不需要内核注入",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

        async def run(self, arguments: dict[str, object]) -> ToolResult:
            injected.append(arguments)
            return ToolResult(output=str(arguments["text"]))

    needs = NeedsContext()
    plain = Plain()

    result = await needs.execute({"text": "甲"}, context={"标记": 1})
    check(
        not result.failed and result.output == "收到 甲",
        "标了 context_argument 的工具拿到注入的对象，其余参数照旧",
    )
    check(
        injected[-1] == {"标记": 1},
        "注入的是内核交过来的那个对象本身，没有被包一层",
    )
    check(
        needs.context_argument not in needs.spec.parameters.get("properties", {}),
        "context_argument 那个名字不出现在参数模式里（模型看不到它）",
    )

    missing = await needs.execute({"text": "乙"})
    check(
        missing.failed and "内核装配问题" in missing.output,
        f"该注入却没注入时报装配问题而不是参数错误：{missing.output[:40]}",
    )
    check(
        "参数" not in missing.output.split("：", 1)[0],
        "装配问题的措辞与参数错误分得开，模型不会去改自己的参数",
    )

    await plain.execute({"text": "丙"}, context={"标记": 2})
    check(
        injected[-1] == {"text": "丙"},
        "不声明 context_argument 的工具，context 不会混进它的参数",
    )

    # ToolResult.meta 是给机器看的结构化读数，默认空、可填。
    plain_result = await plain.execute({"text": "丁"})
    check(plain_result.meta == {}, "ToolResult.meta 默认是空字典")

    enriched = ToolResult(output="x", meta={"count": 3})
    check(enriched.meta["count"] == 3, "ToolResult.meta 带着结构化读数走")

    # 批量部分成功：只要有一条成功就算成功。
    partial = BatchOutcome(total=100)
    partial.succeeded.extend(["lig1"])
    for index in range(99):
        partial.failed.append(BatchFailure(index=index, key=f"lig{index + 2}", reason="解析失败"))
    check(partial.ok, "100 个里只有 1 个成功，整批仍算成功")
    check(
        partial.as_meta() == {"total": 100, "succeeded": 1, "failed": 99, "reasons": {"解析失败": 99}},
        f"批量的结构化读数按原因归组：{partial.as_meta()}",
    )

    # 一条都没成功才算失败，并且要明说重试无用。
    total_failure = BatchOutcome(total=3)
    for index in range(3):
        total_failure.failed.append(BatchFailure(index=index, key=f"x{index}", reason="坏输入"))
    check(not total_failure.ok, "一条都没成功时整批算失败")
    text = total_failure.describe()
    check(
        "重试整批没有用" in text and "坏输入" in text,
        f"全失败时写明重试无用并带上原因：{text.splitlines()[-1]}",
    )

    empty = BatchOutcome(total=0)
    check(not empty.ok, "没有输入时不算成功（避免空批被当成通过）")


def _uniprot_search_payload() -> dict[str, object]:
    """一条 UniProt 检索响应：一条命中，字段按 SEARCH_FIELDS 取。"""
    return {
        "results": [
            {
                "primaryAccession": "P00533",
                "uniProtkbId": "EGFR_HUMAN",
                "proteinDescription": {
                    "recommendedName": {"fullName": {"value": "Epidermal growth factor receptor"}}
                },
                "genes": [{"geneName": {"value": "EGFR"}}],
                "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
                "sequence": {"length": 1210},
                "entryType": "UniProtKB reviewed (Swiss-Prot)",
            }
        ]
    }


def _uniprot_entry_payload() -> dict[str, object]:
    """一条 UniProt 完整条目，含结构、结构域、功能注释与 AlphaFold 交叉引用。"""
    return {
        "primaryAccession": "P00533",
        "uniProtkbId": "EGFR_HUMAN",
        "proteinDescription": {
            "recommendedName": {"fullName": {"value": "Epidermal growth factor receptor"}}
        },
        "genes": [{"geneName": {"value": "EGFR"}}],
        "organism": {"scientificName": "Homo sapiens", "commonName": "Human", "taxonId": 9606},
        "sequence": {"length": 1210},
        "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "comments": [
            {
                "commentType": "FUNCTION",
                "texts": [{"value": "Receptor tyrosine kinase (PubMed:12345)."}],
            }
        ],
        "features": [
            {
                "type": "Domain",
                "description": "Protein kinase",
                "location": {"start": {"value": 712}, "end": {"value": 979}},
            },
            {"type": "Binding site", "ligand": {"name": "ATP"}},
        ],
        "uniProtKBCrossReferences": [
            {
                "database": "PDB",
                "id": "1M17",
                "properties": [
                    {"key": "Method", "value": "X-ray"},
                    {"key": "Resolution", "value": "2.60 A"},
                    {"key": "Chains", "value": "A=702-1002"},
                ],
            },
            {"database": "AlphaFoldDB", "id": "P00533"},
        ],
        "keywords": [{"name": "Kinase"}],
        "entryAudit": {"entryVersion": 250, "lastAnnotationUpdateDate": "2024-01-01"},
    }


async def check_uniprot_tools() -> None:
    """靶点检索与取详情，全程用假传输（不联网）。"""
    cache_dir = PROBE / "uniprot_cache"
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    requests: list[tuple[str, dict[str, str]]] = []

    def transport(
        url: str, params: dict[str, object] | None = None, *, timeout: float = 45.0, headers=None
    ) -> object:
        requests.append((url, {str(key): str(value) for key, value in (params or {}).items()}))
        if "/search" in url:
            return _uniprot_search_payload()
        return _uniprot_entry_payload()

    original_json = uniprot_module.http.get_json
    original_dir = uniprot_module.target_cache_dir
    uniprot_module.http.get_json = transport
    uniprot_module.target_cache_dir = lambda: cache_dir
    try:
        search = uniprot_module.SearchTargetsTool()
        ledger = SessionLedger(session_id="", snapshot_path=cache_dir / "ledger.json")

        result = await search.execute({"query": "EGFR", "organism": "human"})
        check(not result.failed, f"检索成功：{result.output.splitlines()[0]}")
        check(
            requests[-1][1]["query"] == "(gene:EGFR) AND organism_id:9606 AND reviewed:true",
            f"单 token 走基因名精确匹配、物种走 taxid：{requests[-1][1]['query']}",
        )
        check("P00533" in result.output and "EGFR" in result.output, "结果里摆出编号与基因名")
        check(
            result.meta.get("count") == 1,
            f"命中的条数写进 meta：{result.meta}",
        )
        check(ledger.entries() == [], "纯查询的 search_targets 一条都不登记")

        empty_result = await search.execute({"query": ""})
        check(
            empty_result.failed and "不能为空" in empty_result.output,
            "空的 query 直接给一句可读的失败，不发请求",
        )
        check(
            len([item for item in requests if "/search" in item[0]]) == 1,
            "空 query 那一次没有发出请求",
        )

        # 物种别名走 taxid，不让 UniProt 按名字做子串匹配。
        await search.execute({"query": "kinase inhibitor", "organism": "小鼠"})
        check(
            requests[-1][1]["query"] == "(kinase inhibitor) AND organism_id:10090 AND reviewed:true",
            f"多词走全文检索、中文物种名映射到 taxid：{requests[-1][1]['query']}",
        )

        # limit 上限收口。
        await search.execute({"query": "EGFR", "limit": 999})
        check(requests[-1][1]["size"] == str(uniprot_module.MAX_LIMIT), "limit 被收在上限之内")

        # 取详情：登记、指纹、缓存。
        fetch = uniprot_module.FetchTargetTool()
        before = len(requests)
        detail = await fetch.execute({"accession": "p00533"}, context=ledger)
        check(not detail.failed, f"取详情成功：{detail.output.splitlines()[0]}")
        check(len(requests) == before + 1, "第一次取详情联网一次")
        check(
            "AF-P00533-F1" in detail.output,
            "AlphaFold 编号按 AF-<编号>-F1 拼出来，不是直接拿 UniProt 编号",
        )
        check("1M17" in detail.output and "2.60 Å" in detail.output, "结构那一行带分辨率")
        check("(PubMed:12345)" not in detail.output, "注释里的文献编号被去掉")
        entries = ledger.entries()
        check(
            len(entries) == 1 and entries[0].handle.kind == "artifact",
            f"取详情登记成一条 artifact：{[str(item.handle) for item in entries]}",
        )
        check(bool(entries[0].fingerprint), "登记的条目带上内容指纹")
        check(
            entries[0].meta.get("pdb_count") == 1 and entries[0].meta.get("alphafold_id") == "AF-P00533-F1",
            f"meta 里带着结构可用性：{entries[0].meta.get('pdb_count')} 个 PDB",
        )

        # 第二次：读缓存、不联网、不重复登记。
        before = len(requests)
        again = await fetch.execute({"accession": "P00533"}, context=ledger)
        check(len(requests) == before, "同一编号第二次取详情不联网（读本地缓存）")
        check("读自本地缓存" in again.output, "结果里写明这一次读的是缓存")
        check(
            "已有同一份" in again.output and len(ledger.entries()) == 1,
            "同一份不重复登记（按指纹判同一件事）",
        )

        # 坏编号在联网之前就被拦下。
        before = len(requests)
        bad = await fetch.execute({"accession": "1M17"}, context=ledger)
        check(
            bad.failed and "不是 UniProt 编号" in bad.output,
            f"PDB 编号当编号传进来时直接指路：{bad.output[:40]}",
        )
        check(len(requests) == before, "坏编号那一次没有发出请求")

        # 没注入登记表 → 装配问题。
        unassigned = await fetch.execute({"accession": "P00533"})
        check(
            unassigned.failed and "内核装配问题" in unassigned.output,
            "没拿到登记表时报装配问题而不是参数错",
        )
    finally:
        uniprot_module.http.get_json = original_json
        uniprot_module.target_cache_dir = original_dir
        shutil.rmtree(cache_dir, ignore_errors=True)


def _pdb_atom(
    serial: int,
    name: str,
    resname: str,
    chain: str,
    number: int,
    x: float,
    y: float,
    z: float,
    *,
    kind: str = "ATOM  ",
    altloc: str = " ",
    element: str = " C",
) -> str:
    """按标准 PDB 列位造一行坐标，供 pdbtext 的断言用。"""
    return (
        f"{kind}{serial:>5} {name:^4}{altloc}{resname:>3} {chain}{number:>4}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2}\n"
    )


def check_pdbtext() -> None:
    """PDB 纯文本编辑层：列位、增删行、逐字节不变、盒子。"""
    text = "".join(
        [
            "HEADER    TEST STRUCTURE\n",
            _pdb_atom(1, "N", "ALA", "A", 1, 1.0, 1.0, 1.0, element=" N"),
            _pdb_atom(2, "CA", "ALA", "A", 1, 2.0, 1.0, 1.0),
            _pdb_atom(3, "N", "GLY", "B", 1, 5.0, 5.0, 5.0, element=" N"),
            _pdb_atom(100, "O", "HOH", "A", 500, 9.0, 9.0, 9.0, kind="HETATM", element=" O"),
            _pdb_atom(200, "C1", "LIG", "A", 600, 3.0, 3.0, 3.0, kind="HETATM"),
            _pdb_atom(201, "C2", "LIG", "A", 600, 3.5, 3.0, 3.0, kind="HETATM"),
            "CONECT  200  201  100\n",
            "CONECT    1    2\n",
            "END\n",
        ]
    )

    doc = pdbtext.parse(text)
    check(len(doc.atoms) == 6, f"六行坐标都读进来了：{len(doc.atoms)}")
    check(
        [atom.label for atom in doc.atoms if atom.is_water] == ["A:500:HOH"],
        "按残基名认得水",
    )
    check(
        [atom.chain for atom in doc.atoms] == ["A", "A", "B", "A", "A", "A"],
        "链那一列读对了",
    )
    check(
        doc.atoms[1].coord == (2.0, 1.0, 1.0),
        f"坐标按列位读出来：{doc.atoms[1].coord}",
    )
    check(
        [atom.element for atom in doc.atoms[:3]] == ["N", "C", "N"],
        "元素读的是 77-78 列，不是从原子名猜",
    )

    # 非水杂原子分组，按原子数降序。
    groups = pdbtext.hetero_groups(doc)
    check(
        [group.label for group in groups] == ["LIG A:600"],
        f"水不算配体组：{[g.label for g in groups]}",
    )
    check(groups[0].size == 2, f"配体组里是两个原子：{groups[0].size}")

    # 核心不变量：删掉水与配体后，留下的行逐字节等于原文。
    stripped, removed = pdbtext.strip_hetero(doc)
    check(
        sorted(group.label for group in removed) == ["HOH A:500", "LIG A:600"],
        f"删掉的是水与配体两组：{[g.label for g in removed]}",
    )
    original_lines = text.splitlines(keepends=True)
    for line in stripped.lines:
        check(
            line.text == original_lines[line.index],
            f"留下的第 {line.index + 1} 行逐字节等于原文",
        )
    check(
        [atom.serial for atom in stripped.atoms] == ["1", "2", "3"],
        "只留下蛋白原子",
    )

    # CONECT：引用了已删原子（200/201/100）的整条丢掉，干净的那条留下。
    conect = [line.text.strip() for line in stripped.lines if line.kind == "conect"]
    check(
        conect == ["CONECT    1    2"],
        f"CONECT 不做半条：引用已删原子的整条丢掉，剩 {conect}",
    )

    # 序号不重编：删完之后 ATOM 序号仍有空洞。
    check(
        [atom.serial for atom in stripped.atoms][-1] == "3",
        "删行不重编号，序号保持原样",
    )

    # 行号是原文件里的：两次编辑按任意顺序做，只要目标集合一样，结果就一样。
    chains_first = pdbtext.strip_hetero(pdbtext.keep_chains(doc, {"A"})[0])[0]
    hetero_first = pdbtext.keep_chains(pdbtext.strip_hetero(doc)[0], {"A"})[0]
    check(
        chains_first.text == hetero_first.text,
        "「先删链 B 再删杂原子」与「先删杂原子再删链 B」结果一致（行号不回退）",
    )

    # keep_chains：ATOM 与 HETATM 一起按链判（链 A 的 5 个原子含 1 个水、2 个配体原子）。
    kept_a, removed_chains = pdbtext.keep_chains(doc, {"A"})
    check(
        removed_chains == {"B": 1},
        f"只留链 A 时删掉链 B 的那一个原子：{removed_chains}",
    )
    _, removed_a = pdbtext.keep_chains(doc, {"B"})
    check(
        removed_a == {"A": 5},
        f"按链删会把该链的 HETATM 一起算上（链 A 有 2 蛋白 + 1 水 + 2 配体）：{removed_a}",
    )

    # altLoc：残基内退回字典序最小的那个，并把列擦干净。
    alt_text = "".join(
        [
            _pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0, altloc="A"),
            _pdb_atom(2, "CA", "ALA", "A", 1, 2.0, 1.0, 1.0, altloc="B"),
            _pdb_atom(3, "CB", "SER", "A", 2, 3.0, 1.0, 1.0, altloc="B"),
            _pdb_atom(4, "N", "ALA", "A", 1, 0.0, 1.0, 1.0),
        ]
    )
    alt_doc = pdbtext.parse(alt_text)
    picked, chosen, fell_back = pdbtext.select_altloc(alt_doc, "A")
    check(
        chosen == {pdbtext.ResidueKey("A", "1"): "A", pdbtext.ResidueKey("A", "2"): "B"},
        f"每个残基各选一个构象：{ {str(k): v for k, v in chosen.items()} }",
    )
    check(
        [str(key) for key in fell_back] == ["A:2"],
        f"残基内没有想要的构象时退回并报出来：{[str(k) for k in fell_back]}",
    )
    check(
        all(atom.altloc == "" for atom in picked.atoms),
        "留下那些原子的 altLoc 列被擦干净（否则引擎会拒收）",
    )
    check(
        [line.text[16] for line in picked.lines if line.atom is not None] == [" ", " ", " "],
        "只动第 17 列，其余逐字节不动",
    )
    check(
        [atom.serial for atom in picked.atoms] == ["1", "3", "4"],
        "A:2 的 B 构象被留下，A:1 的 B 构象被删掉，无构象的骨架保留",
    )

    # 坏列位要在读的时候就报出来，不静默跳过。
    broken = "".join(["ATOM      1  CA  ALA A   1       1.0\n"])
    bad = None
    try:
        pdbtext.parse(broken)
    except pdbtext.PdbEditError as error:
        bad = str(error)
    check(
        bad is not None and "列" in bad,
        f"太短的行直接报列位问题：{bad}",
    )

    # 盒子：与引擎口径一致的轴平行包围盒。
    center, size = pdbtext.calc_box([(0.0, 0.0, 0.0), (2.0, 4.0, 6.0)], 0.0)
    check(center == (1.0, 2.0, 3.0) and size == (2.0, 4.0, 6.0), f"无留白的盒子：{center} {size}")
    center, size = pdbtext.calc_box([(0.0, 0.0, 0.0), (2.0, 4.0, 6.0)], 1.0)
    check(size == (4.0, 6.0, 8.0), f"四周各留 1 埃：{size}")
    rendered = pdbtext.format_box(center, size)
    check(
        rendered.startswith("center_x = ") and "size_z = " in rendered and "#" not in rendered,
        f"盒子文件按引擎格式、不带注释行：{rendered.splitlines()[0]}",
    )

    empty = None
    try:
        pdbtext.calc_box([], 1.0)
    except pdbtext.PdbEditError as error:
        empty = str(error)
    check(empty is not None and "一个坐标都没有" in empty, "空坐标算盒子直接报错")


def check_export_geometry() -> None:
    """从坐标算口袋的那一层：距离、口径筛选、并查集聚类、圈残基、合并。"""
    # ---- 距离：手算对得上 ----
    check(
        export_module.min_distance([(0.0, 0.0, 0.0)], [(3.0, 4.0, 0.0)]) == 5.0,
        "最短距离按勾股算（3-4-5 直角三角形）",
    )
    check(
        export_module.min_distance(
            [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)], [(1.0, 0.0, 0.0), (20.0, 0.0, 0.0)]
        )
        == 1.0,
        "取的是任一对原子的最短值，不是首原子之间的距离",
    )
    for label, left, right in (
        ("左边", [], [(0.0, 0.0, 0.0)]),
        ("右边", [(0.0, 0.0, 0.0)], []),
    ):
        bad = None
        try:
            export_module.min_distance(left, right)
        except export_module.ExportGeometryError as error:
            bad = str(error)
        check(bad is not None and "空" in bad, f"{label}一组是空的时候报错，不返回 0")

    center = export_module.ligand_center([(0.0, 0.0, 0.0), (2.0, 4.0, 6.0)])
    check(center == (1.0, 2.0, 3.0), f"配体中心是算术平均，不是包围盒中心：{center}")
    bad = None
    try:
        export_module.ligand_center([])
    except export_module.ExportGeometryError as error:
        bad = str(error)
    check(bad is not None and "中心" in bad, "空坐标定不出中心，报错不返回原点")

    # ---- 重原子才参与 ----
    with_h = "".join(
        [
            _pdb_atom(1, "C1", "LIG", "A", 600, 0.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(2, "H1", "LIG", "A", 600, 0.5, 0.0, 0.0, kind="HETATM", element=" H"),
            _pdb_atom(3, "CA", "ALA", "A", 1, 3.0, 0.0, 0.0),
            _pdb_atom(4, "HA", "ALA", "A", 1, 2.0, 0.0, 0.0, element=" H"),
        ]
    )
    without_h = "".join(
        [
            _pdb_atom(1, "C1", "LIG", "A", 600, 0.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(3, "CA", "ALA", "A", 1, 3.0, 0.0, 0.0),
        ]
    )
    doc_h = pdbtext.parse(with_h)
    doc_noh = pdbtext.parse(without_h)
    lig_h = export_module.ligand_groups(doc_h).kept
    lig_noh = export_module.ligand_groups(doc_noh).kept
    heavy_h = tuple(
        atom.coord for atom in lig_h[0].atoms if export_module.is_heavy(atom)
    )
    heavy_noh = tuple(atom.coord for atom in lig_noh[0].atoms if export_module.is_heavy(atom))
    check(
        heavy_h == heavy_noh == ((0.0, 0.0, 0.0),),
        f"带氢与不带氢抽出同一组重原子：{heavy_h} vs {heavy_noh}",
    )
    residues_h = export_module.pocket_residues(doc_h, heavy_h)
    residues_noh = export_module.pocket_residues(doc_noh, heavy_noh)
    check(
        [r.min_distance for r in residues_h] == [r.min_distance for r in residues_noh],
        f"带氢与不带氢算出的最短距离相同：{[r.min_distance for r in residues_h]}",
    )
    check(
        residues_h[0].min_distance == 3.0,
        f"手算：C1(0,0,0) 到 CA(3,0,0) 是 3.0，得 {residues_h[0].min_distance}",
    )
    check(residues_h[0].as_dict()["min_distance"] == 3.0, "写进 meta 的那份是两位小数的数")

    # ---- 空配体组要报错 ----
    bad = None
    try:
        export_module.pocket_residues(doc_noh, ())
    except export_module.ExportGeometryError as error:
        bad = str(error)
    check(bad is not None and "圈不出" in bad, "空配体组圈残基报错，不返回空表装没事")

    # ---- 聚类是并查集，不是顺序贪心 ----
    # A-B 相距 3（<4），B-C 相距 3（<4），A-C 相距 6（>4）：三点必须归一簇。
    chain_text = "".join(
        [
            _pdb_atom(1, "C1", "AAA", "A", 1, 0.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(2, "C1", "BBB", "A", 2, 3.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(3, "C1", "CCC", "A", 3, 6.0, 0.0, 0.0, kind="HETATM"),
        ]
    )
    chain_doc = pdbtext.parse(chain_text)
    chain_groups = export_module.ligand_groups(chain_doc).kept
    clusters = export_module.cluster_ligands(chain_groups)
    check(
        len(clusters) == 1 and clusters[0].size == 3,
        f"A 近 B、B 近 C、A 远 C 仍归一簇（顺序贪心会切错）：{[c.describe() for c in clusters]}",
    )
    # 换遍历顺序：反着给一组，簇的构成不变。
    reversed_clusters = export_module.cluster_ligands(tuple(reversed(chain_groups)))
    check(
        sorted(sorted(c.resnames) for c in reversed_clusters)
        == sorted(sorted(c.resnames) for c in clusters),
        "换一个遍历顺序，簇的划分不变（并查集的性质，不是巧合）",
    )
    # 真分开的：相距 10 埃的两组必须是两簇。
    apart_text = "".join(
        [
            _pdb_atom(1, "C1", "AAA", "A", 1, 0.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(2, "C1", "BBB", "A", 2, 10.0, 0.0, 0.0, kind="HETATM"),
        ]
    )
    apart = export_module.cluster_ligands(
        export_module.ligand_groups(pdbtext.parse(apart_text)).kept
    )
    check(len(apart) == 2, f"相距 10 埃的两组是两簇：{len(apart)}")

    # ---- 配体口径：四类各排各的 ----
    mixed_text = "".join(
        [
            _pdb_atom(1, "O", "HOH", "A", 500, 0.0, 0.0, 0.0, kind="HETATM", element=" O"),
            _pdb_atom(2, "ZN", "ZN", "A", 501, 1.0, 0.0, 0.0, kind="HETATM", element="ZN"),
            _pdb_atom(3, "S", "SO4", "A", 502, 2.0, 0.0, 0.0, kind="HETATM", element=" S"),
            _pdb_atom(4, "SE", "MSE", "A", 503, 3.0, 0.0, 0.0, kind="HETATM", element="SE"),
            _pdb_atom(5, "C1", "STI", "A", 504, 4.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(6, "C2", "STI", "A", 504, 4.5, 0.0, 0.0, kind="HETATM"),
        ]
    )
    filtered = export_module.ligand_groups(pdbtext.parse(mixed_text), modified_names=["MSE"])
    check(
        [g.resname for g in filtered.kept] == ["STI"],
        f"水/锌/硫酸根/修饰残基各排掉，只有 STI 进聚类：{[g.resname for g in filtered.kept]}",
    )
    check(
        sorted(name for name, _reason in filtered.excluded) == ["MSE", "SO4", "ZN"],
        f"被排除的三个名字都记在清单里（水由 hetero_groups 排掉、不算这层）："
        f"{sorted(n for n, _ in filtered.excluded)}",
    )
    check(
        set(filtered.counts) == {"修饰残基", "单原子离子", "缓冲液或糖"},
        f"每条排除都带理由：{filtered.counts}",
    )
    check("排除了 3 个" in filtered.describe(), f"排除要报出来：{filtered.describe()}")
    none_excluded = export_module.ligand_groups(pdbtext.parse(
        _pdb_atom(1, "C1", "STI", "A", 1, 4.0, 0.0, 0.0, kind="HETATM")
    ))
    check(
        "没有排除" in none_excluded.describe(),
        f"一个都没排除时也要明说，不然读者以为这段没跑：{none_excluded.describe()}",
    )
    # 金属酶看不到口袋是认下来的代价——但要说清楚是「被筛掉」而不是「没有」。
    check(
        "ZN" in export_module.MONATOMIC_IONS and "SO4" in export_module.BUFFER_LIKE,
        "两个名单收的正是这两类（名单在 export.py 顶部，每条有理由）",
    )

    # ---- 圈残基：只算聚合物，不看别的杂原子 ----
    pocket_text = "".join(
        [
            _pdb_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
            _pdb_atom(2, "CA", "GLY", "A", 2, 20.0, 0.0, 0.0),
            _pdb_atom(3, "CA", "SER", "B", 3, 1.0, 0.0, 0.0),
            _pdb_atom(4, "C1", "LIG", "A", 600, 2.0, 0.0, 0.0, kind="HETATM"),
            _pdb_atom(5, "C1", "LG2", "A", 601, 2.5, 0.0, 0.0, kind="HETATM"),
        ]
    )
    pocket_doc = pdbtext.parse(pocket_text)
    found = export_module.pocket_residues(pocket_doc, [(1.0, 0.0, 0.0)], radius=5.0)
    check(
        [f"{r.chain}:{r.number}:{r.resname}" for r in found] == ["B:3:SER", "A:1:ALA"],
        f"5 埃以内只有这两个残基（20 埃那个在外、别的配体不算残基），按最短距离降序："
        f"{[r.as_dict() for r in found]}",
    )
    check(
        found[0].min_distance == 0.0 and found[1].min_distance == 1.0,
        f"各残基的最短距离都算出来了：{[r.min_distance for r in found]}",
    )
    only_a = export_module.pocket_residues(
        pocket_doc, [(1.0, 0.0, 0.0)], radius=5.0, chains={"A"}
    )
    check(
        [r.chain for r in only_a] == ["A"],
        f"给了链就把别的链排掉（复合物里受体只是一条链）：{[r.chain for r in only_a]}",
    )
    check(
        found == export_module.pocket_residues(pocket_doc, [(1.0, 0.0, 0.0)], radius=5.0),
        "同一份坐标跑两遍，残基顺序完全一致（排序确定，没有字典序残留）",
    )

    # ---- 跨复合物合并 ----
    merged = export_module.merge_pockets(
        [
            ((0.0, 0.0, 0.0), [(0.0, 0.0, 0.0)], {"pdb_id": "1AAA", "ligand": "STI"}),
            ((3.0, 0.0, 0.0), [(3.0, 0.0, 0.0)], {"pdb_id": "1BBB", "ligand": "STI"}),
        ]
    )
    check(
        len(merged) == 1 and len(merged[0]["origins"]) == 2,
        f"中心相距 3 埃（<6）合并成一处，两个来源都留着：{merged}",
    )
    apart_merged = export_module.merge_pockets(
        [
            ((0.0, 0.0, 0.0), [(0.0, 0.0, 0.0)], {"pdb_id": "1AAA", "ligand": "STI"}),
            ((10.0, 0.0, 0.0), [(10.0, 0.0, 0.0)], {"pdb_id": "1BBB", "ligand": "ABC"}),
        ]
    )
    check(
        len(apart_merged) == 2 and all(len(m["origins"]) == 1 for m in apart_merged),
        f"中心相距 10 埃（>6）是两处，各一个来源：{len(apart_merged)}",
    )
    check(
        export_module.merge_pockets([]) == [],
        "没有输入时返回空表，不编一处口袋出来",
    )

    # ---- 接触计数（本轮不进 meta，但同样是纯几何） ----
    check(
        export_module.contact_counts([(0.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], 4.0) == 1,
        "4 埃以内算一对接触",
    )
    check(
        export_module.contact_counts([(0.0, 0.0, 0.0)], [(5.0, 0.0, 0.0)], 4.0) == 0,
        "超出阈值不算",
    )
    tally = export_module.contacts_for(pocket_doc, [(1.0, 0.0, 0.0)], 4.0)
    check(
        tally == (("A:1:ALA", 1), ("B:3:SER", 1)),
        f"每个残基各贴了几个原子，按数量降序：{tally}",
    )


def check_preprocess() -> None:
    """结构预处理：清洗（选链/构象/去水/去杂/去氢）与配体几何筛查。"""
    # ---- 清洗：拿一份带两套模型、两条链、水、修饰残基、交替构象的夹具 ----
    cleaned = preprocess_module.clean_structure(
        _pdb_fixture(), chains=("A", "B"), modified_names=("MSE",)
    )
    report = cleaned.report
    check(report.chains == ("A", "B"), f"只留指定链：{report.chains}")
    check(report.dropped_chains == (("C", 1),), f"去掉链 C 并计数：{report.dropped_chains}")
    check(report.waters == 1, f"去水计数是残基数：{report.waters}")
    check(report.models == 1, f"第二套模型被丢掉并计数：{report.models}")
    check(
        report.altloc == (("A:1", "B"),),
        f"残基里只有 B 构象时退回 B 并报出来：{report.altloc}",
    )
    check(
        [name for name, _reason in report.removed] == ["AQ4"],
        f"只有 AQ4 被当杂原子组删掉：{report.removed}",
    )
    check(
        "重原子太少" in report.removed_counts,
        f"AQ4 只有两个重原子，按「重原子太少」排除：{report.removed_counts}",
    )
    check("HOH" not in cleaned.text, "写出的坐标里没有水")
    check("NAG" not in cleaned.text, "链 C 上的 NAG 随链一起去掉")
    check("MSE" in cleaned.text, "修饰残基写着 HETATM 也保留（它属于聚合物）")
    check("AQ4" not in cleaned.text, "被筛掉的杂原子组不在输出里")
    check("去水 1 个" in report.describe(), f"报告里写了去水：{report.describe()}")
    payload = report.as_dict()
    check(payload["chains"] == ["A", "B"], f"as_dict 的链是列表：{payload['chains']}")
    check(payload["removed"].get("重原子太少") == 1, f"as_dict 按原因计数：{payload['removed']}")

    # 不给 chains 时保留全部聚合物链；纯 HETATM 的链号不算聚合物链。
    keep_all = preprocess_module.clean_structure(_pdb_fixture(), modified_names=("MSE",))
    check(keep_all.report.chains == ("A", "B"), f"不指定链时保留全部聚合物链：{keep_all.report.chains}")
    hetero_only = pdbtext.parse(
        _pdb_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0)
        + _pdb_atom(2, "C1", "LIG", "Z", 1, 5.0, 0.0, 0.0, kind="HETATM")
    )
    check(
        preprocess_module.polymer_chains(hetero_only) == ("A",),
        "只有 HETATM 的链号不算聚合物链",
    )
    alt_text = _pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0, altloc="A") + _pdb_atom(
        2, "CA", "ALA", "A", 1, 1.0, 2.0, 1.0, altloc="B"
    ) + _pdb_atom(
        3, "CA", "GLY", "A", 2, 3.0, 1.0, 1.0, altloc="B"
    ) + _pdb_atom(
        4, "CA", "GLY", "A", 2, 3.0, 2.0, 1.0, altloc="A"
    )
    alt = preprocess_module.clean_structure(alt_text)
    check(
        alt.report.altloc == (("A:1", "A"), ("A:2", "A")),
        f"两个残基各选 A，并按残基键排序：{alt.report.altloc}",
    )
    check(
        alt.text.count("  CA  ALA A   1") == 1,
        "选完构象只剩一份坐标",
    )

    # ---- 几何筛查：四条判据各拦一类，真配体留下 ----
    protein = "".join(
        [
            _pdb_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
            _pdb_atom(2, "CA", "GLY", "A", 2, 2.0, 0.0, 0.0),
            _pdb_atom(3, "CA", "SER", "A", 3, 4.0, 0.0, 0.0),
        ]
    )
    real = "".join(
        _pdb_atom(
            100 + index, "C1", "LG1", "A", 10, 1.0 + index * 0.5, 3.0, 0.0,
            kind="HETATM",
        )
        for index in range(4)
    )
    tiny = "".join(
        _pdb_atom(
            200 + index, "C1", "TNY", "A", 11, 10.0 + index, 10.0, 10.0,
            kind="HETATM",
        )
        for index in range(2)
    )
    far = "".join(
        _pdb_atom(
            300 + index, "C1", "FAR", "A", 12, 30.0 + index, 30.0, 30.0,
            kind="HETATM",
        )
        for index in range(4)
    )
    covalent = "".join(
        _pdb_atom(
            400 + index, "C1", "COV", "A", 13, index * 0.5, 0.0, 1.5,
            kind="HETATM",
        )
        for index in range(4)
    )
    screen_doc = pdbtext.parse(protein + real + tiny + far + covalent)
    groups = export_module.ligand_groups(screen_doc)
    screen = preprocess_module.screen_ligands(screen_doc, groups.kept)
    check(
        [group.resname for group in screen.kept] == ["LG1"],
        f"只有真配体留下：{[group.resname for group in screen.kept]}",
    )
    reasons = {name: reason for name, reason in screen.excluded}
    check(reasons.get("TNY") == "重原子太少", f"两个重原子的组被拦下：{reasons}")
    check(reasons.get("FAR") == "蛋白接触不足", f"离蛋白 30 埃的组被拦下：{reasons}")
    check(reasons.get("COV") == "与蛋白共价连接", f"1.5 埃贴住蛋白的组被拦下：{reasons}")
    check(
        screen.counts == {"重原子太少": 1, "蛋白接触不足": 1, "与蛋白共价连接": 1},
        f"每类原因各计一个：{screen.counts}",
    )
    check("几何筛查排除了 3 个" in screen.describe(), f"排除要报出来：{screen.describe()}")

    # 空输入不报错、也不编东西。
    empty = preprocess_module.screen_ligands(screen_doc, ())
    check(empty.kept == () and empty.excluded == (), "没有候选组时返回空结果")


def _pdb_fixture() -> str:
    """一份像真的坐标文件：两套模型、两条聚合物链、配体、水、修饰残基、交替构象。

    它刻意同时带上「解析必须处理对」的几样东西：第二套模型（统计不该把它算进去）、
    链 B 的缺口（声明 5 观测 3）、MODRES 里的修饰残基（属于聚合物，不是配体）、
    HEADER 里的编号（本地导入靠它认回条目）。
    """
    lines = [
        "HEADER    TRANSFERASE                             17-JAN-98   1M17\n",
        "TITLE     EPIDERMAL GROWTH FACTOR RECEPTOR KINASE DOMAIN\n",
        "TITLE    2 WITH 4-ANILINOQUINAZOLINE\n",
        "EXPDTA    X-RAY DIFFRACTION\n",
        "REMARK   2 RESOLUTION.    2.60 ANGSTROMS.\n",
        "SEQRES   1 A    5  MET ALA GLY LYS SER\n",
        "SEQRES   1 B    5  MET ALA GLY LYS SER\n",
        "MODRES 1M17 MSE A    3  SELENOMETHIONINE\n",
        "MODEL        1\n",
        _pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0),
        _pdb_atom(2, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0, altloc="B"),
        _pdb_atom(3, "CB", "MSE", "A", 3, 2.0, 1.0, 1.0, kind="HETATM"),
        _pdb_atom(4, "CA", "GLY", "B", 1, 5.0, 5.0, 5.0),
        _pdb_atom(5, "CA", "GLY", "B", 3, 5.5, 5.0, 5.0),
        _pdb_atom(6, "CA", "GLY", "B", 4, 5.7, 5.0, 5.0),
        _pdb_atom(100, "O", "HOH", "A", 500, 9.0, 9.0, 9.0, kind="HETATM", element=" O"),
        _pdb_atom(200, "C1", "AQ4", "A", 600, 3.0, 3.0, 3.0, kind="HETATM"),
        _pdb_atom(201, "C2", "AQ4", "A", 600, 3.5, 3.0, 3.0, kind="HETATM"),
        _pdb_atom(202, "C1", "NAG", "C", 1, 7.0, 7.0, 7.0, kind="HETATM"),
        "ENDMDL\n",
        "MODEL        2\n",
        _pdb_atom(1, "CA", "ALA", "A", 1, 9.0, 9.0, 9.0),
        "ENDMDL\n",
        "END\n",
    ]
    return "".join(lines)


def check_complexes() -> None:
    """P0 主来源：搜 / 排 / 下三段，以及「不编条目」这条底线。"""
    # ---- 搜：解出条目 id，不顺手解旁支字段 ----
    body = {
        "result_set": [
            {"identifier": "1IEP", "score": 1.0},
            {"identifier": "2HYY", "score": 1.0},
        ],
        "total_count": 2,
    }
    ids = complexes_module._ids_from(body)
    check(ids == ["1IEP", "2HYY"], f"从检索响应里解出条目 id：{ids}")
    check(
        complexes_module._ids_from({"total_count": 0}) == [],
        "没有 result_set 时给空表，不猜",
    )
    check(complexes_module._ids_from("不是字典") == [], "响应形状不对时给空表，不抛")

    # 查询体：两个条件（UniProt 编号 + 有非聚合物实体）。
    query = complexes_module._accession_query("P00519")
    nodes = query["query"]["nodes"]
    attrs = [n["parameters"]["attribute"] for n in nodes]
    check(
        any("database_accession" in a for a in attrs),
        f"查询体里有 UniProt 编号那一项：{attrs}",
    )
    check(
        any("nonpolymer_entity_count" in a for a in attrs),
        f"查询体里有「有配体」那一项（省掉它会拉回上千个 apo 结构）：{attrs}",
    )
    check(query["return_type"] == "entry", "搜的是条目不是别的")

    # 空编号报错，不发一个查不到东西的请求。
    bad = None
    try:
        complexes_module.list_complexes("")
    except complexes_module.ComplexError as error:
        bad = str(error)
    check(bad is not None and "UniProt" in bad, f"空编号直接报错：{bad}")

    # ---- 排：顺序确定、可复现 ----
    entries = [
        complexes_module.ComplexEntry(
            pdb_id="AAAA", resolution=3.0, method="X-RAY DIFFRACTION", ligands=("STI",)
        ),
        complexes_module.ComplexEntry(
            pdb_id="BBBB", resolution=1.5, method="X-RAY DIFFRACTION", ligands=("STI",)
        ),
        complexes_module.ComplexEntry(
            pdb_id="CCCC", resolution=2.0, method="X-RAY DIFFRACTION", ligands=("AQ4",)
        ),
    ]
    chosen, total = complexes_module.pick_top(entries, top_n=3)
    check(total == 3, f"总数报的是搜到的个数：{total}")
    check(
        [e.pdb_id for e in chosen] == ["BBBB", "CCCC"],
        f"同一套配体（都是 STI）只留排序最好的那个——AAAA 被去重掉，"
        f"不同配体（AQ4）保留：{[e.pdb_id for e in chosen]}",
    )
    check(
        "AAAA" not in [e.pdb_id for e in chosen],
        "被去重的是同一套配体的第二个，不是分辨率差的那个",
    )
    again, _ = complexes_module.pick_top(list(reversed(entries)), top_n=3)
    check(
        [e.pdb_id for e in again] == [e.pdb_id for e in chosen],
        "同一组输入换个顺序跑两遍，结果完全一致（排序里没有字典序残留）",
    )
    # 分辨率优先：没有分辨率（None）的排在最后。
    with_none = [
        complexes_module.ComplexEntry(pdb_id="NONE", resolution=None, ligands=("AAA",)),
        complexes_module.ComplexEntry(pdb_id="LOWR", resolution=4.0, ligands=("BBB",)),
    ]
    ordered, _ = complexes_module.pick_top(with_none, top_n=2)
    check(
        [e.pdb_id for e in ordered] == ["LOWR", "NONE"],
        f"没有分辨率的排最后（不压过 4.0 埃的晶体结构）：{[e.pdb_id for e in ordered]}",
    )
    # 上限生效。
    many = [
        complexes_module.ComplexEntry(pdb_id=f"X{i:03d}", resolution=float(i), ligands=(f"L{i}",))
        for i in range(30)
    ]
    capped, _ = complexes_module.pick_top(many, top_n=12)
    check(len(capped) == 12, f"取前 N 个（N=12）：{len(capped)}")

    # ---- 下：缓存与容错 ----
    original_get_text = complexes_module.http.get_text
    original_cache = complexes_module.rcsb_cache_dir
    cache_dir = PROBE / "rcsb_check"
    shutil.rmtree(cache_dir, ignore_errors=True)
    complexes_module.rcsb_cache_dir = lambda: cache_dir
    try:
        calls: list[str] = []

        def fake_get_text(url, **kwargs):
            calls.append(url)
            if "FAIL" in url:
                raise complexes_module.ToolError("模拟 404")
            return "ATOM      1  CA  ALA A   1       1.0   1.0   1.0\n"

        complexes_module.http.get_text = fake_get_text
        check(
            complexes_module.download_entry("OK01").startswith("ATOM"),
            "下载成功时拿到坐标文本",
        )
        check(len(calls) == 1, f"第一次下载发了一次请求：{len(calls)}")
        complexes_module.download_entry("OK01")
        check(len(calls) == 1, f"第二次走缓存，不再发请求：{len(calls)}")
        bad = None
        try:
            complexes_module.download_entry("FAIL")
        except complexes_module.ComplexError as error:
            bad = str(error)
        check(bad is not None and "没成" in bad, f"下载失败报错而不是返回空文本：{bad}")

        # 一批下载：第 3 个失败，前两个仍入库、总数不超过 N。
        class _Entry:
            def __init__(self, pid): self.pdb_id = pid

        outcome = complexes_module.fetch_top(
            [_Entry("OK01"), _Entry("OK02"), _Entry("FAIL"), _Entry("OK03")], top_n=4
        )
        check(outcome.used == 3, f"三个成功入库、一个失败不影响整批：{outcome.used}")
        check(
            [pid for pid, _ in outcome.failed] == ["FAIL"],
            f"失败的那个记进清单：{outcome.failed}",
        )
        outcome_capped = complexes_module.fetch_top(
            [_Entry(f"OK0{i}") for i in range(1, 6)], top_n=2
        )
        check(outcome_capped.used == 2, f"一批的个数不超过 N：{outcome_capped.used}")
    finally:
        complexes_module.http.get_text = original_get_text
        complexes_module.rcsb_cache_dir = original_cache
        shutil.rmtree(cache_dir, ignore_errors=True)


def check_prankweb() -> None:
    """P1 副选：四态、假 201 陷阱、轮询上限、降级、缓存。"""
    original_get_json = prankweb_module.http.get_json
    original_cache = prankweb_module.prankweb_cache_dir
    cache_dir = PROBE / "prankweb_check"
    shutil.rmtree(cache_dir, ignore_errors=True)
    prankweb_module.prankweb_cache_dir = lambda: cache_dir
    try:
        # ---- 假 201 陷阱：HTTP 200 但 status 是 queued，不能判成成功 ----
        calls: list[str] = []

        def queued_forever(url, **kwargs):
            calls.append(url)
            return {"status": "queued"}

        prankweb_module.http.get_json = queued_forever
        result = prankweb_module.fetch_prediction("9XYZ", poll_limit=3, poll_interval=0.0)
        check(
            result.status is prankweb_module.PredictionStatus.QUEUED,
            f"一直 queued 时不能判成成功（照 HTTP 码判就会在这里无限等）：{result.status}",
        )
        check(not result.ok, "没算好就不算 ok")
        check(len(calls) == 3, f"轮询到上限就停，不无限循环：{len(calls)} 次")

        # ---- 四态 ----
        for raw, expected in (
            ("queued", prankweb_module.PredictionStatus.QUEUED),
            ("running", prankweb_module.PredictionStatus.RUNNING),
            ("successful", prankweb_module.PredictionStatus.SUCCESSFUL),
            ("failed", prankweb_module.PredictionStatus.FAILED),
        ):
            check(
                prankweb_module.PredictionStatus.parse(raw) is expected,
                f"status {raw!r} 认成 {expected.value}",
            )
        check(
            prankweb_module.PredictionStatus.parse("莫名其妙") is prankweb_module.PredictionStatus.FAILED,
            "认不出的 status 当失败，不当成功",
        )

        # failed 立刻返回，不轮询。
        calls.clear()

        def failed(url, **kwargs):
            calls.append(url)
            return {"status": "failed", "message": "结构算不出来"}

        prankweb_module.http.get_json = failed
        result = prankweb_module.fetch_prediction("FAILED", poll_limit=5, poll_interval=0.0)
        check(
            result.status is prankweb_module.PredictionStatus.FAILED and len(calls) == 1,
            f"failed 立刻返回，不重试到底：{result.status} 取了 {len(calls)} 次",
        )
        check("结构算不出来" in result.describe(), f"把服务端给的原因带上：{result.describe()}")

        # 网络异常 → unavailable，不抛。
        def broken(url, **kwargs):
            raise prankweb_module.ToolError("连接失败")

        prankweb_module.http.get_json = broken
        result = prankweb_module.fetch_prediction("NETDOWN", poll_interval=0.0)
        check(
            result.status is prankweb_module.PredictionStatus.UNAVAILABLE,
            f"网络异常当「没查成」：{result.status}",
        )
        check("没查成" in result.describe(), f"「没查成」与「查了没有」要分开：{result.describe()}")

        # ---- successful 取数据 + 缓存 ----
        payload = {"structure": {"indices": ["A_1"]}, "pockets": [{"name": "pocket1"}]}
        calls.clear()

        def success(url, **kwargs):
            calls.append(url)
            if url.endswith("/public/prediction.json"):
                return payload
            return {"status": "successful"}

        prankweb_module.http.get_json = success
        result = prankweb_module.fetch_prediction("1IEP", poll_interval=0.0)
        check(
            result.status is prankweb_module.PredictionStatus.SUCCESSFUL
            and result.payload == payload,
            f"successful 时取回结果文件：{result.status}",
        )
        check(not result.cached, "第一次不是缓存命中")
        check(len(calls) == 2, f"状态 + 数据各一次：{len(calls)}")

        # 缓存命中：第二次不发请求。
        calls.clear()
        result = prankweb_module.fetch_prediction("1IEP", poll_interval=0.0)
        check(result.cached and result.payload == payload, "第二次走缓存")
        check(len(calls) == 0, f"缓存命中时一个请求都不发：{len(calls)}")

        report = prankweb_module.cache_report()
        check(
            [name for name, _ in report] == ["1IEP"],
            f"缓存状态能列出来（给 /prankweb 命令用）：{report}",
        )
        check(prankweb_module.clear_cache("1IEP") == 1, "能手动清一个条目的缓存")

        # ---- 解析：只留受体那条链、不引入编号换算 ----
        sample = {
            "pockets": [
                {
                    "name": "pocket1",
                    "rank": "1",
                    "score": "39.46",
                    "probability": "0.957",
                    "center": ["16.9132", "54.5702", "16.1823"],
                    "residues": ["A_246", "B_999", "A_360"],
                }
            ]
        }
        pockets = pockets_module._pockets_from_prediction(
            sample, receptor_chains=("A",), radius=5.0
        )
        check(len(pockets) == 1, f"解出一个口袋：{len(pockets)}")
        pocket = pockets[0]
        check(pocket["source"] == "p2rank", f"标 source=p2rank：{pocket['source']}")
        check(pocket["center"] == [16.913, 54.57, 16.182], f"center 转成数：{pocket['center']}")
        check(pocket["score"] == 39.46, f"score 是数不是字符串：{pocket['score']}")
        check(pocket["probability"] == 0.957, f"probability 一并写：{pocket['probability']}")
        numbers = [r["resseq"] for r in pocket["residues"]]
        check(
            numbers == [246, 360],
            f"B 段的残基不进受体（受体链是 A），且编号直接用、不做换算：{numbers}",
        )
        check(
            all("resname" not in r for r in pocket["residues"]),
            "预测不给残基名，就不写（编一个看上去和真的一样）",
        )
        check(
            all("min_distance" not in r for r in pocket["residues"]),
            "预测不给 min_distance，就不写（那不是几何量）",
        )

        # center 缺了不能编一个出来。
        blank = pockets_module._pockets_from_prediction(
            {"pockets": [{"name": "pocket1", "residues": ["A_1"]}]},
            receptor_chains=("A",),
            radius=5.0,
        )
        check(blank[0]["center"] == [0.0, 0.0, 0.0], f"center 缺了给零点而不是编一个：{blank[0]['center']}")
    finally:
        prankweb_module.http.get_json = original_get_json
        prankweb_module.prankweb_cache_dir = original_cache
        shutil.rmtree(cache_dir, ignore_errors=True)


def check_pockets() -> None:
    """两层来源的装配：优先级、三态、complexes 表、不编条目。"""
    text = IEP_FIXTURE.read_text(encoding="utf-8")

    class _Outcome:
        def __init__(self, texts, failed=()):
            self.texts = list(texts)
            self.failed = list(failed)

        @property
        def used(self):
            return len(self.texts)

    # ---- P0 有货：origin=complex、source=ligand、complexes 有来源 ----
    bundle = pockets_module.complex_pockets(
        uniprot="P00519",
        receptor_chains=("A",),
        search=lambda ac: (["1IEP"], {}),
        fetch=lambda entries, **kw: _Outcome([("1IEP", text)]),
    )
    check(bundle.origin == "complex", f"P0 出结果时 origin=complex：{bundle.origin}")
    check(len(bundle.pockets) >= 1, f"从 1IEP 圈出口袋：{len(bundle.pockets)}")
    first = bundle.pockets[0]
    check(first["source"] == "ligand", f"P0 的口袋 source=ligand：{first['source']}")
    check("score" not in first, "P0 不写 score（实验定位没有打分这回事，写 0 会被当成零分）")
    check(
        [r["resseq"] for r in first["residues"][:3]] == [360, 315, 318],
        f"圈出的残基与演示数据对得上（ILE360 最短 2.67）：{first['residues'][:3]}",
    )
    check(
        first["residues"][0]["min_distance"] == 2.67,
        f"最短距离是现算的 2.67：{first['residues'][0]['min_distance']}",
    )
    check(
        isinstance(first.get("complexes"), list) and first["complexes"][0]["pdb_id"] == "1IEP",
        f"complexes 是主结果的来源表：{first.get('complexes')}",
    )
    check(bundle.total == 1 and bundle.used == 1, f"报 total/used：{bundle.total}/{bundle.used}")
    check(
        "单原子离子" in bundle.excluded,
        f"被筛掉的杂原子组按类报出来：{bundle.excluded}",
    )

    # ---- P0 为空：明确报「查了没有」 ----
    empty = pockets_module.complex_pockets(
        uniprot="P61626",
        search=lambda ac: ([], {}),
        fetch=lambda entries, **kw: _Outcome([]),
    )
    check(empty.origin == "none" and empty.is_empty, f"该蛋白没有复合物时留空：{empty.origin}")
    check("确实没有" in empty.note, f"分得清「查了没有」：{empty.note}")

    # ---- 搜索失败：报「没查成」，与「查了没有」分开 ----
    def boom(ac):
        raise complexes_module.ComplexError("模拟服务端 502")

    broken = pockets_module.complex_pockets(uniprot="P00519", search=boom)
    check(broken.origin == "none", "搜索失败时留空")
    check(
        "没查成" in broken.note,
        f"与「查了没有」分开（两者对用户的含义完全不同）：{broken.note}",
    )

    # ---- 下载全失败：留空，不编条目 ----
    no_fetch = pockets_module.complex_pockets(
        uniprot="P00519",
        search=lambda ac: (["1IEP", "2HYY"], {}),
        fetch=lambda entries, **kw: _Outcome([], [("1IEP", "404"), ("2HYY", "404")]),
    )
    check(
        no_fetch.origin == "none" and "一个都没下下来" in no_fetch.note,
        f"下载全失败时留空并说明：{no_fetch.note}",
    )
    check(no_fetch.total == 2, f"总数仍然报出来（让用户知道搜到了几个）：{no_fetch.total}")

    # ---- 优先级：P0 有货时不该去查预测 ----
    p2rank_calls: list[str] = []

    def spy_p2rank(pdb_id, **kwargs):
        p2rank_calls.append(pdb_id)
        raise AssertionError("P0 有货时不该调用预测")

    check(
        bundle.origin == "complex" and not p2rank_calls,
        "有复合物就不查预测（顺序是 P0 → P1，不是两个都查）",
    )

    # ---- P1：P0 为空时才转 ----
    class _FakePredict:
        status = prankweb_module.PredictionStatus.SUCCESSFUL
        payload = {
            "pockets": [
                {"name": "pocket1", "rank": "1", "score": "10.0", "probability": "0.8",
                 "center": ["1.0", "2.0", "3.0"], "residues": ["A_10", "A_20"]}
            ]
        }

        def describe(self):
            return "stub"

    predicted = pockets_module.p2rank_pockets(
        pdb_id="1LYZ", receptor_chains=("A",), client=lambda pid: _FakePredict()
    )
    check(predicted.origin == "predicted", f"P1 出结果时 origin=predicted：{predicted.origin}")
    check(predicted.pockets[0]["source"] == "p2rank", "P1 的口袋 source=p2rank")
    check("score" in predicted.pockets[0], "P1 有 score（预测真实存在的数）")
    check(
        "没有任何带配体的共晶结构" in predicted.note,
        f"note 说明为什么走了预测：{predicted.note}",
    )

    # 预测不可用 → 留空 + 说明
    class _BrokenPredict:
        status = prankweb_module.PredictionStatus.UNAVAILABLE
        payload = None

        def describe(self):
            return "预测这一侧没查成：连接失败"

    dead = pockets_module.p2rank_pockets(pdb_id="1LYZ", client=lambda pid: _BrokenPredict())
    check(dead.origin == "none" and dead.is_empty, "预测不可用时留空")
    check("没查成" in dead.note, f"三态都不编，说明是「没查成」：{dead.note}")

    # 没有 PDB 编号 → 直接说没查成
    no_id = pockets_module.p2rank_pockets(pdb_id="")
    check(no_id.origin == "none" and "没查成" in no_id.note, f"没有编号时说明白：{no_id.note}")


def check_align_and_prior() -> None:
    """`align` + `pockets.prior_pockets`：序列核对、叠合、按先验清单算口袋。

    三件必须成立的事：
      · 同一个蛋白的两份结构比对得上，另一个蛋白的比对不上（门槛真的在起作用）；
      · 核对不通过的编号**拿不到变换**，也就进不了口袋；
      · 逐条结论里「编号不存在」与「没查成」分得开。
    """
    real_1iep = IEP_FIXTURE
    abl_text = real_1iep.read_text(encoding="utf-8")

    # ---- 一、取序列 ----
    seqs = align_module.sequences(abl_text)
    check(set(seqs) == {"A", "B"}, f"读出两条聚合物链：{sorted(seqs)}")
    check(len(seqs["A"]) == 274, f"链 A 的序列长度：{len(seqs['A'])}")
    check(align_module.identity(seqs["A"], seqs["A"]) == 1.0, "同一条序列的同一性为 1")
    reversed_seq = seqs["A"][::-1]
    low = align_module.identity(seqs["A"], reversed_seq)
    check(low < align_module.IDENTITY_FLOOR, f"与无关序列的同一性低于门槛：{low:.3f}")

    pair = align_module.best_pair(seqs, {"X": seqs["B"]})
    check(pair is not None and pair[2] == 1.0, f"配对取同一性最高的一对：{pair}")

    # ---- 二、核对与门槛 ----
    verdict, transform = align_module.align_candidate(abl_text, abl_text, "SELF")
    check(verdict.ok and transform is not None, f"自己跟自己核对通过：{verdict.reason}")
    check(
        verdict.rmsd is not None and verdict.rmsd < 0.01,
        f"自己跟自己叠合 rmsd≈0：{verdict.rmsd}",
    )
    check("→" in verdict.pair, f"结论里报出配的是哪两条链：{verdict.pair}")

    strict, no_transform = align_module.align_candidate(
        abl_text, abl_text, "SELF", floor=1.01
    )
    check(
        not strict.ok and no_transform is None,
        "门槛抬高之后同一个结构也被拒——说明门槛不是在空转",
    )

    # 另一个蛋白：本机有 1M17（EGFR）就用它，没有就跳过这一小段。
    egfr_path = ROOT / "runs" / "1M17" / "receptor.pdb"
    egfr_text = egfr_path.read_text(encoding="utf-8") if egfr_path.is_file() else ""
    if egfr_text:
        cross, cross_transform = align_module.align_candidate(egfr_text, abl_text, "1IEP")
        check(
            not cross.ok and cross_transform is None,
            f"另一个蛋白被剔除、且拿不到变换：{cross.state}",
        )
        check(
            cross.identity is not None and cross.identity < align_module.IDENTITY_FLOOR,
            f"跨蛋白同一性低于门槛：{cross.identity}",
        )

    # ---- 三、搬坐标：返回新的组，不动原来那一份 ----
    doc = pdbtext.parse(abl_text)
    filtered = export_module.ligand_groups(doc)
    check(len(filtered.kept) >= 2, f"1IEP 里有两个配体组：{len(filtered.kept)}")
    moved_transform, moved_rmsd = align_module.superpose(abl_text, abl_text, "A", "B")
    moved = align_module.moved_groups(filtered.kept, moved_transform)
    check(moved[0] is not filtered.kept[0], "返回的是新的组，不是改原来那一份")
    check(
        moved[0].atoms[0].coord != filtered.kept[0].atoms[0].coord,
        "坐标真的被搬动了（两条链的构象不同，变换不是恒等）",
    )
    check(moved_rmsd >= 0.0, f"叠合给出了 rmsd：{moved_rmsd:.3f}")

    # ---- 四、先验清单：通过 / 剔除 / 去重 / 两种「没有」 ----
    texts = {"1IEP": abl_text}

    def download(pdb_id, **_kwargs):
        if pdb_id in texts:
            return texts[pdb_id]
        raise complexes_module.ComplexError(f"下载 {pdb_id} 没成：假的失败")

    bundle, verdicts = pockets_module.prior_pockets(
        receptor_text=abl_text,
        candidates=["1iep", "1IEP", "9ZZZ"],
        download=download,
        probe=lambda _url, **_kw: 404,
    )
    states = {item.pdb_id: item.state for item in verdicts}
    check(len(verdicts) == 2, f"重复的编号只留一条：{[i.pdb_id for i in verdicts]}")
    check(states.get("1IEP") == "ok", f"通过的编号：{states}")
    check(states.get("9ZZZ") == "missing", f"不存在的编号标 missing：{states}")
    check(bundle.origin == "aligned", f"来源标成 aligned：{bundle.origin}")
    check(bundle.total == 2 and bundle.used == 1, f"报了 total/used：{bundle.total}/{bundle.used}")
    check(len(bundle.pockets) >= 1, f"算出了口袋：{len(bundle.pockets)}")
    check(
        bundle.pockets[0]["source"] == "ligand",
        f"先验这一侧也是实测事实，source=ligand：{bundle.pockets[0]['source']}",
    )
    check("score" not in bundle.pockets[0], "先验这一侧不写 score")
    check(
        bundle.pockets[0]["complexes"][0].get("rmsd") == 0.0,
        f"来源里带上 rmsd：{bundle.pockets[0]['complexes'][0]}",
    )
    check(
        bundle.pockets[0]["residues"] and "min_distance" in bundle.pockets[0]["residues"][0],
        "残基带着现算的最短距离",
    )

    # 探编号时回 200：编号存在，这一次是没取下来 —— 与「不存在」分开说
    _bundle2, verdicts2 = pockets_module.prior_pockets(
        receptor_text=abl_text,
        candidates=["9ZZZ"],
        download=download,
        probe=lambda _url, **_kw: 200,
    )
    check(verdicts2[0].state == "unreachable", f"取不下来标 unreachable：{verdicts2[0].state}")
    check(
        verdicts2[0].state != states.get("9ZZZ"),
        "「编号不存在」与「没查成」是两种状态，不混成一个",
    )

    # 一个都没通过：留空，且说明是「查了确实没有」而不是「没查成」
    empty_bundle, empty_verdicts = pockets_module.prior_pockets(
        receptor_text=abl_text,
        candidates=["9ZZZ", "8YYY"],
        download=download,
        probe=lambda _url, **_kw: 404,
    )
    check(empty_bundle.origin == "none", f"一个都没通过时 origin=none：{empty_bundle.origin}")
    check(empty_bundle.pockets == [], "口袋留空，不编条目")
    check(empty_bundle.reason == "checked", f"reason 标 checked：{empty_bundle.reason}")
    check(len(empty_verdicts) == 2, "两个编号都留了结论")

    # 清单本身是空的：与「查了没有」又不是一回事
    none_bundle, none_verdicts = pockets_module.prior_pockets(
        receptor_text=abl_text, candidates=[], download=download
    )
    check(none_bundle.origin == "none" and not none_verdicts, "空清单不产出结论")


async def check_write_run() -> None:
    """`write_run`：装配点。优先级、三态、meta 形状、登记成 artifact、不编字段。"""
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from opencode_tui.core.agent.ledger import SessionLedger

    real_1iep = IEP_FIXTURE
    text = real_1iep.read_text(encoding="utf-8")

    # 运行目录写到临时处，别污染仓库根的 runs/。
    runs_root = _Path(tempfile.mkdtemp(prefix="vsa-runs-"))
    original_runs_dir = export_module.runs_dir
    export_module.runs_dir = lambda: runs_root

    try:
        await _check_write_run_body(export_module, pockets_module, prankweb_module,
                                    complexes_module, SessionLedger, text, runs_root,
                                    _json)
    finally:
        export_module.runs_dir = original_runs_dir
        shutil.rmtree(runs_root, ignore_errors=True)


async def check_write_run_docking() -> None:
    """`write_run` 接入对接产物：复制 pose、填 molecules、provenance.poses、清理旧 pose。"""
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from opencode_tui.core.agent.ledger import SessionLedger

    runs_root = _Path(tempfile.mkdtemp(prefix="vsa-run-dock-"))
    original_runs_dir = export_module.runs_dir
    original_complex = pockets_module.complex_pockets
    original_p2rank = pockets_module.p2rank_pockets

    def stub_pockets(**kwargs):
        return pockets_module.PocketBundle(origin="none", note="stub")

    try:
        export_module.runs_dir = lambda: runs_root
        pockets_module.complex_pockets = stub_pockets
        pockets_module.p2rank_pockets = stub_pockets

        ledger = SessionLedger(
            session_id="probe", snapshot_path=runs_root / "ledger.json"
        )
        receptor_dir = runs_root / "receptor-art"
        receptor_dir.mkdir()
        (receptor_dir / "receptor.pdb").write_text(
            IEP_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        receptor = ledger.add(
            "receptor",
            path=receptor_dir,
            summary="prepared 1IEP",
            fingerprint=fingerprint_path(receptor_dir),
            meta={
                "stage": "prepared",
                "pdb_file": "receptor.pdb",
                "pdb_id": "1IEP",
                "chains": ["A"],
                "title": "ABL kinase",
            },
        )

        def make_docking(name: str, results: list[dict], *, receptor_fp: str = ""):
            directory = runs_root / name
            (directory / "poses").mkdir(parents=True)
            for item in results:
                relative = str(item.get("pose_pdb") or "")
                if relative:
                    (directory / relative).write_text(
                        f"REMARK {item['id']}\n", encoding="utf-8"
                    )
            manifest = {
                "kind": "docking",
                "stage": "docking",
                "engine": {"name": "vina", "version": "AutoDock Vina v1.2.5"},
                "receptor": {
                    "handle": str(receptor.handle),
                    "fingerprint": receptor_fp or str(receptor.fingerprint),
                },
                "ligands": {"handle": "ligandset#1", "fingerprint": "lig-fp"},
                "params": {"exhaustiveness": 8, "num_modes": 9, "seed": 42},
                "counts": {
                    "docked": sum(1 for item in results if item.get("status") == "ok"),
                    "failed": 1,
                },
                "results": results,
                "failures": [{"id": "D", "reason": "timeout"}],
            }
            (directory / "manifest.json").write_text(
                _json.dumps(manifest), encoding="utf-8"
            )
            (directory / "params.json").write_text("{}", encoding="utf-8")
            return ledger.add(
                "artifact",
                path=directory,
                summary=f"docking {name}",
                fingerprint=fingerprint_path(directory),
                meta={"kind": "docking", "stage": "docking"},
            )

        base_results = [
            {
                "id": "A",
                "name": "a",
                "score": -9.0,
                "rank": 1,
                "status": "ok",
                "pose_pdb": "poses/A.pdb",
                "ligand_efficiency": 0.7,
            },
            {
                "id": "B",
                "name": "b",
                "score": -8.0,
                "rank": 2,
                "status": "ok",
                "pose_pdb": "poses/B.pdb",
                "ligand_efficiency": 0.6,
            },
            {
                "id": "C",
                "name": "c",
                "score": -7.0,
                "rank": 3,
                "status": "no_pdb",
                "pose_pdb": "",
                "ligand_efficiency": 0.5,
            },
        ]
        docking = make_docking("docking-art", base_results)
        tool = export_module.WriteRunTool()

        result = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "docking": str(docking.handle),
                "run_id": "dock-run",
                "name": "docked",
            },
            context=ledger,
        )
        check(not result.failed, f"write_run(docking=...) 跑通：{result.output[:300]}")
        run_dir = runs_root / "dock-run"
        meta = _json.loads(
            (run_dir / export_module.RUN_META).read_text(encoding="utf-8")
        )
        demo = _json.loads(DEMO_META_FIXTURE.read_text(encoding="utf-8"))
        expected = set(demo) - {"contacts"}
        check(
            set(meta) == expected,
            f"顶层键没有新增：多 {set(meta) - expected}，缺 {expected - set(meta)}",
        )
        check("contacts" not in meta, "对接结果不写假的 contacts")
        molecules = meta["molecules"]
        check(len(molecules) == 2, f"只写 status=ok 的 pose：{len(molecules)}")
        check(
            [item["id"] for item in molecules] == ["A", "B"],
            f"按 rank 顺序写：{[item['id'] for item in molecules]}",
        )
        check(
            [item["pose"] for item in molecules]
            == ["ligands/pose-01.pdb", "ligands/pose-02.pdb"],
            f"pose 路径密集编号：{[item['pose'] for item in molecules]}",
        )
        check(
            [item["rank"] for item in molecules] == [1, 2]
            and all(item["chain"] == "A" for item in molecules),
            f"rank 与 chain 写对：{molecules}",
        )
        check(
            (run_dir / "ligands" / "pose-01.pdb").read_text(encoding="utf-8")
            == "REMARK A\n",
            "pose 内容来自对接产物",
        )
        check(
            not (run_dir / "ligands" / "pose-03.pdb").exists(),
            "no_pdb 的记录不写空文件",
        )
        poses = meta["provenance"]["poses"]
        check(
            poses["score_direction"] == "lower_is_better"
            and poses["score_unit"] == "kcal/mol"
            and poses["docking"] == str(docking.handle),
            f"provenance.poses 说清方向与来源：{poses}",
        )
        check(
            poses["counts"]["written"] == 2
            and poses["counts"]["skipped"] == 1
            and poses["counts"]["docked"] == 2,
            f"计数口径：{poses['counts']}",
        )
        check(
            str(poses["engine"]).startswith("AutoDock Vina"),
            f"引擎版本进 provenance：{poses['engine']}",
        )
        check(
            result.meta.get("molecules") == 2
            and result.meta.get("best_score") == -9.0,
            f"返回 meta 带上写出数量与最佳分数：{result.meta}",
        )

        # ---- 同 run_id 二次写更少 pose：旧的 pose-02 必须清掉 ----
        fewer = make_docking("docking-art-fewer", [base_results[0]])
        again = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "docking": str(fewer.handle),
                "run_id": "dock-run",
            },
            context=ledger,
        )
        check(not again.failed, f"同 run_id 重写成功：{again.output[:200]}")
        meta2 = _json.loads(
            (run_dir / export_module.RUN_META).read_text(encoding="utf-8")
        )
        check(
            len(meta2["molecules"]) == 1 and meta2["molecules"][0]["id"] == "A",
            f"molecules 同步变少：{meta2['molecules']}",
        )
        check(
            (run_dir / "ligands" / "pose-01.pdb").is_file()
            and not (run_dir / "ligands" / "pose-02.pdb").exists(),
            "旧 pose 被清理，没有新旧混在一起",
        )

        # ---- 坏 pose 路径：失败且旧 meta.json 一个字节都不动 ----
        before_meta = (run_dir / export_module.RUN_META).read_bytes()
        bad = make_docking(
            "docking-art-bad",
            [{**base_results[0], "pose_pdb": "poses/missing.pdb"}],
        )
        # 造出「manifest 指向一个不存在的 pose」：文件建完再删掉。
        (bad.path / "poses" / "missing.pdb").unlink()
        bad_result = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "docking": str(bad.handle),
                "run_id": "dock-run",
            },
            context=ledger,
        )
        check(
            bad_result.failed and "不存在或越界" in bad_result.output,
            f"坏 pose 路径被拦下：{bad_result.output}",
        )
        check(
            (run_dir / export_module.RUN_META).read_bytes() == before_meta,
            "坏 pose 不改旧 meta.json",
        )

        # ---- 受体指纹不匹配 ----
        wrong = make_docking(
            "docking-art-wrong",
            [base_results[0]],
            receptor_fp="other-fp",
        )
        wrong_result = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "docking": str(wrong.handle),
                "run_id": "dock-run",
            },
            context=ledger,
        )
        check(
            wrong_result.failed and "另一份受体" in wrong_result.output,
            f"跨受体混用被拦下：{wrong_result.output}",
        )

        # ---- docking 与 ligands 同时给：二选一 ----
        ligand_dir = runs_root / "ligandset"
        (ligand_dir / "ligands").mkdir(parents=True)
        (ligand_dir / "manifest.json").write_text(
            _json.dumps({"kind": "ligandset", "stage": "prepared"}),
            encoding="utf-8",
        )
        ligandset = ledger.add(
            "ligandset",
            path=ligand_dir,
            summary="prepared ligands",
            meta={"stage": "prepared"},
            link_current_receptor=False,
        )
        both = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "docking": str(docking.handle),
                "ligands": str(ligandset.handle),
                "run_id": "dock-run",
            },
            context=ledger,
        )
        check(
            both.failed and "二选一" in both.output,
            f"docking 与 ligands 同时给时明确拒绝：{both.output}",
        )

        # ---- 没有 docking：molecules 为空、provenance 不带 poses ----
        plain = await tool.execute(
            {"receptor": str(receptor.handle), "run_id": "plain-run"},
            context=ledger,
        )
        check(not plain.failed, f"无对接的旧路径仍能跑：{plain.output[:200]}")
        plain_meta = _json.loads(
            (runs_root / "plain-run" / export_module.RUN_META).read_text(
                encoding="utf-8"
            )
        )
        check(plain_meta["molecules"] == [], "没有对接时 molecules 为空数组")
        check(
            "poses" not in plain_meta["provenance"],
            "没有对接时不写 provenance.poses",
        )

        # ---- 同一个 run_id 从「有对接」改写成「没对接」：旧 pose 也要清掉 ----
        cleared = await tool.execute(
            {"receptor": str(receptor.handle), "run_id": "dock-run"},
            context=ledger,
        )
        check(not cleared.failed, f"同一 run_id 改写成无对接成功：{cleared.output[:200]}")
        cleared_meta = _json.loads(
            (run_dir / export_module.RUN_META).read_text(encoding="utf-8")
        )
        check(cleared_meta["molecules"] == [], "改写成无对接后 molecules 为空")
        check(
            not list((run_dir / "ligands").glob("pose-*.pdb")),
            "改写成无对接后旧 pose 文件被清掉",
        )
    finally:
        export_module.runs_dir = original_runs_dir
        pockets_module.complex_pockets = original_complex
        pockets_module.p2rank_pockets = original_p2rank
        shutil.rmtree(runs_root, ignore_errors=True)


async def _check_write_run_body(export_module, pockets_module, prankweb_module,
                                complexes_module, SessionLedger, text, runs_root,
                                _json) -> None:
    """`check_write_run` 的主体，跑完把 `runs_dir` 复原的事交给外层。"""

    def _make_ledger():
        ledger_file = runs_root / "ledger.json"
        return SessionLedger(session_id="probe", snapshot_path=ledger_file)

    def _register_receptor(ledger, path, meta):
        return ledger.add("receptor", path=path, summary="ABL1 1IEP", meta=meta)

    ledger_ref: list = [None]

    async def _run_tool(tool, arguments, viewer=None):
        return await tool.execute(arguments, context=ledger_ref[0], viewer=viewer)

    # ---- 早退：P0 有货时必须不去查预测（本步最要紧的一条）----
    calls: list[str] = []

    def spy_p2rank(**kwargs):
        calls.append(kwargs.get("pdb_id", ""))
        raise AssertionError("P0 有货时 write_run 不该调用预测")

    real_complex = pockets_module.complex_pockets

    def stub_complex(**kwargs):
        return real_complex(
            uniprot=kwargs["uniprot"],
            receptor_chains=kwargs.get("receptor_chains", ()),
            search=lambda ac: (["1IEP"], {}),
            fetch=lambda entries, **kw: _OutcomeStub([("1IEP", text)]),
        )

    original_complex = pockets_module.complex_pockets
    original_p2rank = pockets_module.p2rank_pockets
    try:
        pockets_module.complex_pockets = stub_complex
        pockets_module.p2rank_pockets = spy_p2rank

        ledger = _make_ledger()
        ledger_ref[0] = ledger
        receptor_path = runs_root / "src-1iep.pdb"
        receptor_path.write_text(text, encoding="utf-8")
        entry = _register_receptor(
            ledger,
            receptor_path,
            {"pdb_id": "1IEP", "title": "ABL kinase", "ligand": "STI",
             "chains": [{"chain_id": "A", "polymer": True}]},
        )
        tool = export_module.WriteRunTool()
        result = await _run_tool(
            tool,
            {"receptor": str(entry.handle), "run_id": "p0-check", "uniprot": "P00519"},
        )
        check(not result.failed, f"write_run 跑通：{result.output[:200]}")
        check(not calls, "P0 有货时**一次都没调用预测**（优先级真的生效）")
        check(result.meta.get("origin") == "complex", f"返回 origin=complex：{result.meta.get('origin')}")

        run_dir = runs_root / "p0-check"
        meta = _json.loads((run_dir / export_module.RUN_META).read_text(encoding="utf-8"))
        check((run_dir / "receptor.pdb").is_file(), "写出了 receptor.pdb")
        check((run_dir / "ligands").is_dir(), "写出了 ligands/ 目录")

        # meta.json 键集合与演示数据一致。**`contacts` 是刻意的例外**：
        # 本轮没有真实位姿，接触距离不可信，字段本身不出（方案稿 §2.1 第 78 行）。
        demo = _json.loads(DEMO_META_FIXTURE.read_text(encoding="utf-8"))
        expected = set(demo) - {"contacts"}
        check(
            set(meta) == expected,
            f"meta.json 顶层键与演示数据一致（除刻意不出的 contacts）：多 {set(meta) - expected}，缺 {expected - set(meta)}",
        )
        check("contacts" not in meta, "contacts 字段不出（本轮没有真实位姿）")
        check("score" not in meta["pockets"][0], "P0 口袋不写 score")
        check(meta["run_id"] == "p0-check", f"run_id 写对：{meta['run_id']}")
        check(meta["structure"] == "receptor.pdb", "structure 指向 receptor.pdb")
        check(meta["pockets"], "P0 的口袋写进了 meta")
        pocket = meta["pockets"][0]
        check(pocket["source"] == "ligand", f"P0 口袋 source=ligand：{pocket.get('source')}")
        check("score" not in pocket, "P0 不写 score 字段")
        check("contacts" not in pocket, "contacts 字段不出现（本轮没有真实位姿）")
        check(
            pocket["residues"][0]["min_distance"] == 2.67,
            f"min_distance 是现算的：{pocket['residues'][0]['min_distance']}",
        )
        check(
            pocket["complexes"][0]["pdb_id"] == "1IEP"
            and pocket["complexes"][0].get("ligand") == "STI"
            and pocket["complexes"][0].get("resolution") is not None,
            f"complexes 来源表带齐 pdb_id/ligand/resolution：{pocket['complexes'][0]}",
        )
        check(
            pocket.get("is_reference") is True,
            "与 target.ligand（STI）同名的簇标了 is_reference",
        )
        prov = meta["provenance"]["pockets"]
        check(isinstance(prov, dict), "provenance.pockets 是对象（不是字符串）")
        check(prov["origin"] == "complex" and prov["method"] == "rcsb-complex",
              f"provenance 记方法与 origin：{prov['origin']}/{prov['method']}")
        check(prov["total"] == 1 and prov["used"] == 1, f"provenance 报 total/used：{prov['total']}/{prov['used']}")
        check("total=1" in result.output or "搜到 1 个" in result.output,
              f"返回里报「搜到几个、用了几个」：{result.output[:300]}")

        # 登记成 artifact，父项是受体
        check(result.meta.get("handle", "").startswith("artifact#"),
              f"登记成 artifact：{result.meta.get('handle')}")
        artifact_entry = ledger.get(result.meta["handle"])
        check(
            any(str(p) == str(entry.handle) for p in artifact_entry.parents),
            f"artifact 的父项挂着受体：{artifact_entry.parents}",
        )
        check(artifact_entry.meta.get("kind") == "run", "artifact meta.kind=run")

        # ---- view=structure：只展示结构，不碰任何口袋来源 ----
        # 把两条来源都换成「一调就炸」的桩：结构模式只要碰了它们，这里就会红。
        def _explode(**kwargs):
            raise AssertionError("view=structure 不该调用口袋来源")

        saved_complex = pockets_module.complex_pockets
        saved_p2rank = pockets_module.p2rank_pockets
        pockets_module.complex_pockets = _explode
        pockets_module.p2rank_pockets = _explode
        try:
            structure_result = await _run_tool(
                tool,
                {
                    "receptor": str(entry.handle),
                    "run_id": "structure-check",
                    "view": "structure",
                    "uniprot": "P00519",
                },
            )
        finally:
            pockets_module.complex_pockets = saved_complex
            pockets_module.p2rank_pockets = saved_p2rank
        check(not structure_result.failed, f"结构模式跑通：{structure_result.output[:200]}")
        check(
            structure_result.meta.get("view") == "structure",
            f"返回里标了 view=structure：{structure_result.meta.get('view')}",
        )
        structure_meta = _json.loads(
            (runs_root / "structure-check" / export_module.RUN_META).read_text(encoding="utf-8")
        )
        check(structure_meta["pockets"] == [], "结构模式不写口袋")
        check(
            structure_meta["provenance"]["view"] == "structure",
            "provenance.view 记下展示模式",
        )
        pre = structure_meta["provenance"].get("preprocess")
        check(isinstance(pre, dict), "provenance.preprocess 是一份结构化报告")
        check(pre["waters"] > 0, f"预处理报告里写了去水数量：{pre.get('waters')}")
        check("STI" in pre["kept_ligands"], f"真配体留在结构里：{pre.get('kept_ligands')}")
        cleaned = (runs_root / "structure-check" / export_module.RUN_RECEPTOR).read_text(
            encoding="utf-8"
        )
        check(
            not any(
                line.startswith("HETATM") and line[17:20].strip() == "HOH"
                for line in cleaned.splitlines()
            ),
            "写出的 receptor.pdb 里没有水的坐标行（HETNAM 等说明行不算）",
        )
        check("STI" in cleaned, "写出的 receptor.pdb 里保留了共晶配体")
        check(
            any(
                line.startswith("HETATM") and line[17:20].strip() == "STI"
                for line in cleaned.splitlines()
            ),
            "保留的是 STI 的坐标行，不只是说明行",
        )
        check(
            "结构预处理" in structure_result.output,
            f"返回里报预处理做了什么：{structure_result.output[:200]}",
        )

        # ---- 两个相距 20 埃的配体 → 两个口袋 ----
        two = runs_root / "two.pdb"
        two.write_text(_two_ligand_pdb(), encoding="utf-8")
        pockets_module.complex_pockets = lambda **kw: real_complex(
            uniprot=kw["uniprot"],
            receptor_chains=kw.get("receptor_chains", ()),
            search=lambda ac: (["9TWO"], {}),
            fetch=lambda entries, **k: _OutcomeStub([("9TWO", two.read_text(encoding="utf-8"))]),
        )
        entry2 = _register_receptor(ledger, two, {"pdb_id": "9TWO", "chains": [{"chain_id": "A", "polymer": True}]})
        result2 = await _run_tool(tool, {"receptor": str(entry2.handle), "run_id": "two-check", "uniprot": "P00000"})
        check(not result2.failed, f"两个配体的结构也跑通：{result2.output[:200]}")
        meta2 = _json.loads((runs_root / "two-check" / export_module.RUN_META).read_text(encoding="utf-8"))
        check(len(meta2["pockets"]) == 2, f"两个相距 20 埃的配体算成两个口袋：{len(meta2['pockets'])}")

        # ---- P0 为空 → 转 P1，origin=predicted、source=p2rank、有 score ----
        pockets_module.complex_pockets = lambda **kw: pockets_module.PocketBundle(
            origin="none", note="P61626 名下没有带配体的已发布结构（查了，确实没有）"
        )
        pockets_module.p2rank_pockets = lambda **kw: _predicted_bundle(pockets_module)
        entry3 = _register_receptor(ledger, receptor_path, {"pdb_id": "1IEP"})
        result3 = await _run_tool(tool, {"receptor": str(entry3.handle), "run_id": "p1-check",
                                   "uniprot": "P61626", "pdb_id": "1IEP"})
        check(result3.meta.get("origin") == "predicted", f"P0 为空时转预测：{result3.meta.get('origin')}")
        check(result3.meta.get("p2rank") == "ok", f"p2rank 三态=ok：{result3.meta.get('p2rank')}")
        meta3 = _json.loads((runs_root / "p1-check" / export_module.RUN_META).read_text(encoding="utf-8"))
        check(meta3["pockets"][0]["source"] == "p2rank", "P1 口袋 source=p2rank")
        check("score" in meta3["pockets"][0], "P1 有 score（预测真实存在的数）")
        check(meta3["provenance"]["pockets"]["origin"] == "predicted", "provenance.origin=predicted")
        check("主来源为空" in result3.output, f"返回里说明为什么走了预测：{result3.output[:300]}")

        # ---- 两者都不可用 → origin=none，区分「查了没有」与「没查成」 ----
        #
        # 分档读的是 `reason` 这个结构化字段，不再从 note 里搜措辞
        # （改文案不该影响分档；以前搜「没查成」四个字，prankweb 服务端报失败时
        #  note 写的是「服务端没给原因」，搜不到，于是把「没查成」说成了「查了没有」）。
        # 这个桩要跟着填 `reason`，否则是在验一句文案而不是验行为。
        pockets_module.p2rank_pockets = lambda **kw: pockets_module.PocketBundle(
            origin="none",
            reason="failed",
            note="预测这一侧没查成：连接失败（没查成，不是查了没有）",
        )
        result4 = await _run_tool(tool, {"receptor": str(entry3.handle), "run_id": "none-check",
                                   "uniprot": "P61626", "pdb_id": "1IEP"})
        check(result4.meta.get("origin") == "none", f"两者都不可用时 origin=none：{result4.meta.get('origin')}")
        check(result4.meta.get("p2rank") == "unavailable", f"p2rank 标 unavailable：{result4.meta.get('p2rank')}")
        meta4 = _json.loads((runs_root / "none-check" / export_module.RUN_META).read_text(encoding="utf-8"))
        check(meta4["pockets"] == [], "口袋留空，不编条目")
        check("没查成" in result4.output or "确实没有" in result4.output,
              f"三态都不编，说明白是哪种：{result4.output[:300]}")

        # ---- 没有 uniprot 也没有 pdb_id → 不编 ----
        result5 = await _run_tool(tool, {"receptor": str(entry3.handle), "run_id": "bare-check"})
        check(result5.meta.get("origin") == "none", "两个主键都没有时 origin=none")

        # ---- 运行目录名非法 → 明确拒绝 ----
        bad = await _run_tool(tool, {"receptor": str(entry3.handle), "run_id": "../escape"})
        check(bad.failed and "不合法" in bad.output, f"拒绝带 .. 的目录名：{bad.output[:120]}")

        # ---- open_viewer：两种拿不到服务的路要分开说 ----
        #
        # 这两条以前混在一条里（都靠 `_probe_port` 返回 0），加了注入通道之后
        # 它们的触发条件已经不同了：
        #   没拿到服务对象 → 程序侧装配问题（内核没把服务接进来），重试无用；
        #   拿到了但没在跑（port=0）→ 用户可操作（先把应用跑起来）。
        # 混成一条的话，任一侧出问题都会以另一侧的名义报出来。
        viewer_tool = export_module.OpenViewerTool()
        original_probe = export_module._probe_port

        unwired = await _run_tool(viewer_tool, {"run": str(result.meta["handle"])})
        check(unwired.failed and "内核装配问题" in unwired.output,
              f"没拿到服务对象时报装配问题：{unwired.output[:160]}")

        class _Stopped:
            """服务对象在、但没在跑：`port=0` 是「没启动」那个状态。"""

            port = 0
            web_port = 0

        export_module._probe_port = lambda: 0
        try:
            no_service = await _run_tool(
                viewer_tool, {"run": str(result.meta["handle"])}, viewer=_Stopped()
            )
            check(no_service.failed and "没有在监听" in no_service.output,
                  f"服务没起时给可操作失败：{no_service.output[:160]}")
        finally:
            export_module._probe_port = original_probe

        # ---- 服务在跑：URL 带 run_id，且不擅自开浏览器 ----
        class _Running:
            """在跑的服务：端口从它身上取，`_probe_port` 只是没有它时的回退。"""

            port = 8860
            web_port = 8860

        opened = await _run_tool(
            viewer_tool, {"run": str(result.meta["handle"])}, viewer=_Running()
        )
        check(not opened.failed and opened.meta.get("url", "").endswith("#/structure/p0-check"),
              f"open_viewer 返回带 run_id 的 URL：{opened.meta.get('url')}")
        check("未打开浏览器" in opened.output, f"默认不擅自开浏览器：{opened.output[:120]}")

        # ---- write_run 的文件里没有 contacts / 没有编造的 score ----
        raw = (runs_root / "p0-check" / export_module.RUN_META).read_text(encoding="utf-8")
        check('"contacts"' not in raw, "meta.json 全文不含 contacts 字段")

        # ---- 给了 candidates 就走先验这一侧，不再按 UniProt 检索复合物 ----
        seen_calls: list[dict] = []

        def stub_prior(**kwargs):
            seen_calls.append(kwargs)
            return (
                pockets_module.PocketBundle(
                    pockets=[
                        {
                            "center": (1.0, 2.0, 3.0),
                            "radius": 5.0,
                            "source": "ligand",
                            "residues": [
                                {
                                    "chain": "A",
                                    "number": "10",
                                    "icode": "",
                                    "resname": "LEU",
                                    "min_distance": 3.0,
                                }
                            ],
                            "complexes": [
                                {
                                    "pdb_id": "1IEP",
                                    "ligand": "STI",
                                    "resolution": 2.1,
                                    "rmsd": 0.4,
                                }
                            ],
                        }
                    ],
                    origin="aligned",
                    note="从先验清单里 1 个通过核对的复合物结构、按叠合后的共晶配体圈出来",
                    total=3,
                    used=1,
                ),
                (
                    align_module.Verdict(
                        "1IEP", "ok", "同一性 1.000（A→A）", pair="A→A", identity=1.0, rmsd=0.4
                    ),
                    align_module.Verdict(
                        "1M17", "mismatch", "链序列与受体对不上", pair="A→A", identity=0.356
                    ),
                    align_module.Verdict("9ZZZ", "missing", "RCSB 上没有这个编号"),
                ),
            )

        uniprot_calls: list[str] = []

        def spy_complex(**kwargs):
            uniprot_calls.append(kwargs.get("uniprot", ""))
            raise AssertionError("给了 candidates 就不该按 UniProt 检索复合物")

        original_prior = getattr(pockets_module, "prior_pockets", None)
        pockets_module.prior_pockets = stub_prior
        pockets_module.complex_pockets = spy_complex
        try:
            wired = await _run_tool(
                tool,
                {
                    "receptor": str(entry3.handle),
                    "run_id": "prior-check",
                    "uniprot": "P00519",
                    "candidates": "1IEP, 1M17;9ZZZ",
                },
            )
            check(not wired.failed, f"给了 candidates 能跑通：{wired.output[:200]}")
            check(not uniprot_calls, "给了 candidates 时一次都没按 UniProt 检索复合物")
            got = seen_calls[0].get("candidates") if seen_calls else None
            check(
                got == ("1IEP", "1M17", "9ZZZ"),
                f"清单按逗号/分号/空格分开并去重：{got}",
            )
            check(wired.meta.get("origin") == "aligned", f"origin=aligned：{wired.meta.get('origin')}")
            check(
                "通过 1IEP" in wired.output
                and "剔除 1M17" in wired.output
                and "剔除 9ZZZ" in wired.output,
                f"逐条结论都念出来了：{wired.output[:400]}",
            )
            prior_meta = _json.loads(
                (runs_root / "prior-check" / export_module.RUN_META).read_text(encoding="utf-8")
            )
            check(
                prior_meta["pockets"][0]["source"] == "ligand",
                "先验这一侧的口袋 source=ligand（实测事实，不是预测）",
            )
            check(
                prior_meta["pockets"][0]["complexes"][0]["rmsd"] == 0.4,
                "来源里留着 rmsd",
            )
            check(
                prior_meta["pockets"][0].get("id") and prior_meta["pockets"][0].get("label"),
                f"口袋补上了 id 与 label：{prior_meta['pockets'][0].get('id')}"
                f" / {prior_meta['pockets'][0].get('label')}",
            )
            stored = prior_meta["provenance"]["pockets"].get("prior")
            check(
                isinstance(stored, list) and len(stored) == 3,
                f"取证结论进了 meta（挂在已有的 provenance 下，不加顶层键）：{stored}",
            )
            check(stored[1]["state"] == "mismatch", "meta 里带着每一条的状态")
            check(
                set(prior_meta) == set(_json.loads(
                    DEMO_META_FIXTURE.read_text(encoding="utf-8")
                )) - {"contacts"},
                "先验这一侧也没有多出顶层键",
            )
        finally:
            pockets_module.complex_pockets = stub_complex
            if original_prior is not None:
                pockets_module.prior_pockets = original_prior
    finally:
        pockets_module.complex_pockets = original_complex
        pockets_module.p2rank_pockets = original_p2rank


class _OutcomeStub:
    """`fetch_top` 的最小替身：只要 `texts` 与 `used`。"""

    def __init__(self, texts, failed=()):
        self.texts = list(texts)
        self.failed = list(failed)

    @property
    def used(self):
        return len(self.texts)


def _predicted_bundle(pockets_module):
    """P1 出来的一份 `PocketBundle`，用来验 write_run 的转侧逻辑。"""
    return pockets_module.PocketBundle(
        pockets=[
            {
                "center": (1.0, 2.0, 3.0),
                "radius": 5.0,
                "source": "p2rank",
                "score": 39.46,
                "probability": 0.957,
                "residues": [{"chain": "A", "resseq": 10, "resname": "LEU", "min_distance": 3.0}],
            }
        ],
        origin="predicted",
        note="主来源为空，改取预测：pocket1 等 1 个口袋",
    )


def _two_ligand_pdb() -> str:
    """造一份有两个相距 20 埃的配体的最小结构（用来验「多配体出多口袋」）。

    每个配体给 4 个重原子、离蛋白 3 埃左右：预处理会筛掉重原子太少的组，
    单原子配体在这条判据里已经不是合法输入了。蛋白沿 x 每 2 埃一个 CA，
    配体放在 y=3，正好被三个以上残基围住。
    """
    lines = []
    for number in range(1, 41):
        lines.append(
            _pdb_atom(
                number, "CA", "ALA", "A", number, float((number - 1) * 2), 0.0, 0.0
            )
        )
    for serial, x in ((201, 10.0), (202, 11.0), (203, 12.0), (204, 13.0)):
        lines.append(
            _pdb_atom(serial, "C1", "LG2", "A", 101, x, 3.0, 0.0, kind="HETATM")
        )
    for serial, x in ((211, 30.0), (212, 31.0), (213, 32.0), (214, 33.0)):
        lines.append(
            _pdb_atom(serial, "C1", "LG2", "A", 102, x, 3.0, 0.0, kind="HETATM")
        )
    lines.append("END")
    return "".join(lines)


async def check_rsearch() -> None:
    """RCSB 搜索：文本搜 / 属性搜 / 批量取配体；只给候选不给结论；失败有明确返回。"""
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from opencode_tui.core.agent.domain import http as http_module
    from opencode_tui.core.agent.domain import rsearch as rsearch_module

    # 缓存目录指到临时处：别读也别写家目录，否则上一次的结果会让这一次「命中」。
    tmp = _Path(tempfile.mkdtemp(prefix="vsa-search-"))
    original_dir = rsearch_module.search_dir
    rsearch_module.search_dir = lambda: tmp

    original_post = http_module.post_json
    original_get_json = http_module.get_json
    original_get_text = http_module.get_text
    calls: list[str] = []

    def _body(ids, total=None):
        return {
            "total_count": total if total is not None else len(ids),
            "result_set": [{"identifier": pid} for pid in ids],
        }

    try:
        # ---- 1. 文本搜：喂打桩响应，解出 8 个条目 ----
        def post_json(url, payload, **kwargs):
            calls.append(url)
            if "rcsbsearch" in url:
                if payload.get("query", {}).get("service") == "full_text":
                    return _body(["2N9T", "5O0U", "6N4I", "6N4Q", "6N4R", "6J8I", "6J8J", "7W9M"])
                # 属性搜
                return _body(
                    ["5EK0", "6J8G", "6J8H"] + [f"9{i:03d}" for i in range(41)],
                    total=44,
                )
            if "graphql" in url:
                return _graphql_stub(payload)
            raise AssertionError(f"没预料到的 POST：{url}")

        http_module.post_json = post_json
        tool = rsearch_module.SearchPdbTool()
        result = await tool.run({"query": "Nav1.7 ProTx-II"})
        check(not result.failed, f"文本搜跑通：{result.output[:200]}")
        check(
            result.meta["ids"] == ["2N9T", "5O0U", "6N4I", "6N4Q", "6N4R", "6J8I", "6J8J", "7W9M"],
            f"文本搜解出 8 个条目：{result.meta['ids']}",
        )
        check(result.meta["total"] == 8, f"命中总数报出来：{result.meta['total']}")

        # ---- 2. 属性搜：按 UniProt 解出 44 个 ----
        result2 = await tool.run({"uniprot": "Q15858", "limit": 3})
        check(not result2.failed, f"属性搜跑通：{result2.output[:200]}")
        check(result2.meta["total"] == 44, f"属性搜命中 44 个：{result2.meta['total']}")
        check(
            result2.meta["ids"][:3] == ["5EK0", "6J8G", "6J8H"],
            f"头几个条目对得上：{result2.meta['ids'][:3]}",
        )
        check(result2.meta["truncated"] is True, "命中多于列出条数时标 truncated")
        check("44" in result2.output and "3" in result2.output,
              f"返回里同时报「命中几个、列出几个」：{result2.output[:200]}")

        # ---- 3. 属性搜可复现：同条件跑两遍，条目集合完全一致 ----
        again = await tool.run({"uniprot": "Q15858", "limit": 3})
        check(again.meta["ids"] == result2.meta["ids"], "同一属性条件跑两遍结果一致")

        # ---- 4. 批量取配体：8THH → [PCW LPE Y01 IYJ NAG P5S]、7XMF → [NAG G2W] ----
        hits = rsearch_module.fetch_ligands(["8THH", "7XMF"])
        check(
            list(hits["8THH"].ligands) == ["PCW", "LPE", "Y01", "IYJ", "NAG", "P5S"],
            f"8THH 的配体代号原样列出：{list(hits['8THH'].ligands)}",
        )
        check(list(hits["7XMF"].ligands) == ["NAG", "G2W"], f"7XMF 的配体：{list(hits['7XMF'].ligands)}")
        check(
            hits["8THH"].resolution == 1.9,
            f"resolution_combined 是数组也解得对：{hits['8THH'].resolution}",
        )
        check(
            hits["8THH"].ligand_names and hits["8THH"].ligand_names[0][0] == "PCW",
            f"配体代号带名称：{hits['8THH'].ligand_names[:1]}",
        )

        # ---- 5. 不带结论：返回结构里没有 pocket / site / is_ligand 这类字段 ----
        blob = _json.dumps(result.meta, ensure_ascii=False)
        for banned in ("pocket", "site", "is_ligand"):
            check(banned not in blob, f"返回里不带归类字段 {banned}")

        # ---- 6. 失败有明确返回，不抛异常 ----
        def boom(url, payload, **kwargs):
            raise http_module.ToolError("模拟 502")

        http_module.post_json = boom
        failed = await tool.run({"query": "whatever"})
        check(failed.failed and "这次没搜成" in failed.output,
              f"接口挂了时返回「这次没搜成」：{failed.output[:160]}")
        check(not failed.meta.get("ids"), "失败时不编条目 id")

        # 命中 0 条：也是「这次没搜成」，不是异常
        http_module.post_json = lambda url, payload, **kw: _body([])
        empty = await tool.run({"uniprot": "P00000"})
        check(not empty.failed and empty.meta["count"] == 0, f"命中 0 条时给空清单：{empty.output[:120]}")

        # 一个条件都不给 → 明确要求
        http_module.post_json = post_json
        bare = await tool.run({})
        check(bare.failed and "至少要给一个条件" in bare.output, f"无条件时明确要求：{bare.output[:120]}")

        # ---- 7. 搜索不写 runs/ 下的文件 ----
        runs_root = PROBE.parent / "runs"
        before = {p.name for p in runs_root.iterdir()} if runs_root.is_dir() else set()
        await tool.run({"query": "Nav1.7 ProTx-II"})
        after = {p.name for p in runs_root.iterdir()} if runs_root.is_dir() else set()
        check(before == after, f"搜索不往 runs/ 写东西：新增 {after - before}")
    finally:
        http_module.post_json = original_post
        http_module.get_json = original_get_json
        http_module.get_text = original_get_text
        rsearch_module.search_dir = original_dir
        shutil.rmtree(tmp, ignore_errors=True)


def _graphql_stub(payload):
    """GraphQL 打桩：按 `variables.ids` 给配体清单（与实测一致）。"""
    table = {
        "8THH": [("PCW", "PHOSPHATIDYLCHOLINE"), ("LPE", "LYSOPHOSPHATIDYLETHANOLAMINE"),
                 ("Y01", "CHOLESTEROL HEMISUCCINATE"), ("IYJ", "TRIAZINE DIAMINE"),
                 ("NAG", "N-ACETYL-D-GLUCOSAMINE"), ("P5S", "DETERGENT")],
        "7XMF": [("NAG", "N-ACETYL-D-GLUCOSAMINE"), ("G2W", "GLYCEROL DERIVATIVE")],
    }
    entries = []
    for pid in payload.get("variables", {}).get("ids", []):
        comps = table.get(pid, [])
        entries.append(
            {
                "rcsb_id": pid,
                "struct": {"title": f"stub {pid}"},
                "rcsb_entry_info": {
                    "nonpolymer_entity_count": len(comps),
                    "experimental_method": "X-RAY DIFFRACTION",
                    "resolution_combined": [1.9],
                },
                "nonpolymer_entities": [
                    {"nonpolymer_comp": {"chem_comp": {"id": code, "name": name}}}
                    for code, name in comps
                ],
            }
        )
    return {"data": {"entries": entries}}


async def check_literature() -> None:
    """PubMed 文献检索：esearch + efetch；只给候选不给判断；失败有明确返回。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from opencode_tui.core.agent.domain import http as http_module
    from opencode_tui.core.agent.domain import literature as literature_module
    from opencode_tui.core.agent.domain import rsearch as rsearch_module

    tmp = _Path(tempfile.mkdtemp(prefix="vsa-lit-"))
    # 缓存的路径拼法在 `rsearch._cache_path` 里，它调的是 **rsearch 的** `search_dir`，
    # 所以改 `literature.search_dir` 没用——两个搜索工具共用一个缓存目录（§7.11）。
    original_dir = rsearch_module.search_dir
    rsearch_module.search_dir = lambda: tmp
    original_get_json = http_module.get_json
    original_get_text = http_module.get_text

    def get_json(url, params=None, **kwargs):
        if "esearch" not in url:
            raise AssertionError(f"没预料到的 GET：{url}")
        term = (params or {}).get("term", "")
        if "PDB:6J8I" in term:
            return {"esearchresult": {"count": "2", "idlist": ["30001", "30002"]}}
        return {"esearchresult": {"count": "3", "idlist": ["10001", "10002", "10003"]}}

    def get_text(url, params=None, **kwargs):
        if "efetch" not in url:
            raise AssertionError(f"没预料到的 GET：{url}")
        # 按请求的 id 造文章：真实接口也是只返回你问的那几条。
        ids = [item for item in str((params or {}).get("id", "")).split(",") if item]
        return _pubmed_xml(ids)

    try:
        http_module.get_json = get_json
        http_module.get_text = get_text

        tool = literature_module.SearchLiteratureTool()
        result = await tool.run({"query": "SCN9A Nav1.7 cryo-EM complex"})
        check(not result.failed, f"文献检索跑通：{result.output[:200]}")
        check(result.meta["ids"] == ["10001", "10002", "10003"], f"拿到 3 篇：{result.meta['ids']}")
        check(result.meta["total"] == 3, f"命中总数报出来：{result.meta['total']}")
        first = result.meta["articles"][0]
        check(bool(first["title"]), f"解出标题：{first['title']}")
        check(bool(first["abstract"]), f"解出摘要片段：{first['abstract'][:40]}")
        check("pubmed.ncbi.nlm.nih.gov/10001" in result.output, "返回里带链接")

        # ---- 按 PDB 编号反查：2 篇 ----
        by_pdb = await tool.run({"query": 'SCN9A AND "PDB:6J8I"'})
        check(by_pdb.meta["ids"] == ["30001", "30002"], f"按 PDB 编号反查 2 篇：{by_pdb.meta['ids']}")

        # ---- 不带结论 ----
        import json as _json

        blob = _json.dumps(result.meta, ensure_ascii=False)
        for banned in ("pocket", "site", "is_ligand", "relevant", "可信"):
            check(banned not in blob, f"文献返回里不带判断字段 {banned}")

        # ---- 失败有明确返回 ----
        def boom(url, params=None, **kwargs):
            raise http_module.ToolError("模拟 503")

        http_module.get_json = boom
        failed = await tool.run({"query": "x"})
        check(failed.failed and "这次没搜成" in failed.output,
              f"接口挂了时返回「这次没搜成」：{failed.output[:160]}")

        # ---- 详情取不到 → 退回只给 id，不把整次判失败 ----
        http_module.get_json = get_json

        def bad_text(url, params=None, **kwargs):
            raise http_module.ToolError("模拟 efetch 502")

        http_module.get_text = bad_text
        partial = await tool.run({"query": "SCN9A Nav1.7 cryo-EM complex"})
        check(not partial.failed, "详情取不到时仍算搜成")
        check(partial.meta["ids"] == ["10001", "10002", "10003"], f"id 仍然给出来：{partial.meta['ids']}")

        # ---- 空检索式 ----
        http_module.get_text = get_text
        blank = await tool.run({"query": "   "})
        check(blank.failed and "不能为空" in blank.output, f"空检索式明确拒绝：{blank.output[:120]}")
    finally:
        http_module.get_json = original_get_json
        http_module.get_text = original_get_text
        rsearch_module.search_dir = original_dir
        shutil.rmtree(tmp, ignore_errors=True)


def _pubmed_xml(ids=None) -> str:
    """一份最小 PubMed XML，字段名与真实返回一致。`ids` 为空时给默认那三篇。"""
    wanted = list(ids) if ids else ["10001", "10002", "10003"]
    titles = {
        "10001": ("Cryo-EM structure of Nav1.7", "2019", "We report a structure."),
        "10002": ("ProTx-II bound Nav1.7", "2020", "The toxin binds VSDII."),
        "10003": ("Voltage sensor pharmacology", "2021", "A review."),
    }

    def article(pmid):
        title, year, abstract = titles.get(
            pmid, (f"Paper {pmid}", "2022", "Stub abstract.")
        )
        return f"""
        <PubmedArticle>
          <MedlineCitation>
            <PMID Version="1">{pmid}</PMID>
            <Article>
              <Journal>
                <Title>Journal of Stub</Title>
                <JournalIssue><PubDate><Year>{year}</Year></PubDate></JournalIssue>
              </Journal>
              <ArticleTitle>{title}</ArticleTitle>
              <Abstract>
                <AbstractText Label="BACKGROUND">Background of {title}.</AbstractText>
                <AbstractText>{abstract}</AbstractText>
              </Abstract>
              <AuthorList>
                <Author><LastName>Doe</LastName><Initials>J</Initials></Author>
                <Author><LastName>Roe</LastName><Initials>A</Initials></Author>
              </AuthorList>
            </Article>
          </MedlineCitation>
        </PubmedArticle>"""

    return "<PubmedArticleSet>" + "".join(article(pid) for pid in wanted) + "</PubmedArticleSet>"


def check_structure_parse() -> None:
    """坐标文件的就地解析：只算第一套模型、链分类、配体/修饰残基分开。"""
    facts = structure_module.parse_pdb(_pdb_fixture(), pdb_id="1M17")

    check(facts.pdb_id == "1M17", f"显式给的编号优先：{facts.pdb_id}")
    check("EPIDERMAL" in facts.title, f"标题拼出来了：{facts.title}")
    check(
        "4-ANILINOQUINAZOLINE" in facts.title,
        f"跨行拼标题不补空格（连字符结尾时）：{facts.title}",
    )
    check(facts.method == "X-RAY DIFFRACTION", f"实验方法读对了：{facts.method}")
    check(facts.resolution == 2.60, f"分辨率读对了：{facts.resolution}")
    check(facts.classification == "TRANSFERASE", f"分类读对了：{facts.classification}")
    check(facts.deposition_date == "17-JAN-98", f"沉积日期读对了：{facts.deposition_date}")

    # 只算第一套模型：两套模型的原子数一样，第二套不该让计数翻倍。
    check(facts.models == 2, f"认出两套模型：{facts.models}")
    check(
        facts.total_atoms == 10,
        f"原子数只算第一套模型（第一套 10 个原子，第二套的 1 个不算）：{facts.total_atoms}",
    )

    check(
        facts.polymer_chains == ("A", "B"),
        f"SEQRES 里声明过的两条链是聚合物：{facts.polymer_chains}",
    )
    check(
        [chain.chain_id for chain in facts.hetero_chains] == ["C"],
        f"只在 HETATM 里出现的链算杂链：{[c.chain_id for c in facts.hetero_chains]}",
    )

    chain_a, chain_b = facts.chains
    check(
        chain_a.observed_residues == 1,
        f"链 A 只观测到一个残基（另两行是同一个残基的两个构象）：{chain_a.observed_residues}",
    )
    check(chain_a.first_residue == 1 and chain_a.last_residue == 1, "链 A 的残基范围")
    check(chain_b.observed_residues == 3, f"链 B 观测到三个残基：{chain_b.observed_residues}")
    check(chain_b.missing == 2, f"链 B 声明 5 观测 3，缺 2 个：{chain_b.missing}")
    check(chain_a.missing == 4, f"链 A 声明 5 观测 1，缺 4 个：{chain_a.missing}")
    check(
        chain_a.atom_count == 6,
        f"链 A 的原子数含 HETATM（3 蛋白 + 1 水 + 2 配体）：{chain_a.atom_count}",
    )

    # 修饰残基属于聚合物，不能混进配体——混进去会让人以为 MSE 是该删掉的杂原子。
    check(
        [name for name, _ in facts.ligands] == ["AQ4", "NAG"],
        f"配体是非水、非修饰的杂原子（AQ4 与糖链 C 上的 NAG，MSE 不算）：{[n for n, _ in facts.ligands]}",
    )
    check(
        [name for name, _ in facts.modified] == ["MSE"],
        f"修饰残基单独一类：{[n for n, _ in facts.modified]}",
    )
    check(facts.water_atoms == 1, f"水算原子数：{facts.water_atoms}")
    check(facts.alt_locs == ("B",), f"交替构象标记收全了：{facts.alt_locs}")

    # 没有 SEQRES 的文件退回按 ATOM 认链——不能因为文件没写 SEQRES 就说它没有肽链。
    bare = "".join([_pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0)])
    bare_facts = structure_module.parse_pdb(bare)
    check(
        bare_facts.polymer_chains == ("A",),
        f"没有 SEQRES 时按 ATOM 认链：{bare_facts.polymer_chains}",
    )
    check(
        bare_facts.chains[0].seqres_length is None,
        "没有 SEQRES 的链声明长度是 None（与「声明 0 个」不同）",
    )
    check(bare_facts.chains[0].missing is None, "无从比较时缺口是 None，不是 0")

    # HEADER 里的编号列：本地导入靠它认回条目。
    header_id = structure_module.parse_pdb(_pdb_fixture()).pdb_id
    check(header_id == "1M17", f"不给编号时从 HEADER 认：{header_id}")

    # NMR 那一行写的是 NOT APPLICABLE——那不是缺失数据，安静返回 None。
    nmr = "".join(["REMARK   2 RESOLUTION. NOT APPLICABLE.\n", _pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0)])
    check(structure_module.parse_pdb(nmr).resolution is None, "NMR 的「不适用」读成 None")

    bad = None
    try:
        structure_module.normalize_pdb_id("P00533")
    except ValueError as error:
        bad = str(error)
    check(
        bad is not None and "四位" in bad and "fetch_target" in bad,
        f"UniProt 编号被拦下并给出可走的路：{bad}",
    )
    check(structure_module.normalize_pdb_id("1m17") == "1M17", "编号大小写不敏感")


async def check_structure_tools() -> None:
    """取结构与导结构，全程用假传输与临时目录（不联网、不碰真实家目录）。"""
    work = PROBE / "structure_tools"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    store = work / "structures"

    calls: list[tuple[str, str]] = []

    def get_text_or_none(url, params=None, *, timeout=45.0, headers=None):
        calls.append(("GET", url))
        if "1M17" in url:
            return _pdb_fixture()
        # 6VXX 与 9ZZZ 都取不到 .pdb（404），区别只在 .cif 在不在——那由 probe 决定。
        return None

    def probe(url, *, timeout=45.0):
        calls.append(("HEAD", url))
        if "1M17" in url or "6VXX" in url:
            return 200
        return 404

    original_text = structure_module.http.get_text_or_none
    original_probe = structure_module.http.probe
    original_dir = structure_module.structure_dir
    structure_module.http.get_text_or_none = get_text_or_none
    structure_module.http.probe = probe
    structure_module.structure_dir = lambda: store
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        fetch = structure_module.FetchStructureTool()

        result = await fetch.execute({"pdb_id": "1m17"}, context=ledger)
        check(not result.failed, f"取结构成功：{result.output.splitlines()[0]}")
        check("PDB 1M17" in result.output, "摘要首行报出编号")
        check(
            "配体（非水杂原子，2 种）：AQ4×1、NAG×1" in result.output,
            "配体那一行报出名称与个数",
        )
        check(
            "修饰残基（属于聚合物，别当成配体删）：MSE×1" in result.output,
            "修饰残基单独一行，并写明它属于聚合物",
        )
        check("交替构象标记：B" in result.output, "交替构象写进摘要")
        check("缺 2 个残基" in result.output, "链 B 的缺口写进摘要")

        entries = ledger.entries()
        check(len(entries) == 1, f"登记了一条：{len(entries)}")
        entry = entries[0]
        check(
            isinstance(entry.handle, ReceptorHandle),
            f"登记成 receptor（不是别的类型）：{entry.handle}",
        )
        check(
            entry.meta.get("stage") == "raw",
            f"状态是「原始结构」，还没准备：{entry.meta.get('stage')}",
        )
        check(
            len(entry.meta.get("chains") or []) == 3,
            f"meta 里放完整链清单（A/B/C 三条）：{len(entry.meta.get('chains') or [])}",
        )
        check(
            entry.meta.get("source_url") == "https://www.rcsb.org/structure/1M17",
            f"远端取的带来源页：{entry.meta.get('source_url')}",
        )
        check(
            result.meta.get("chains") == ["A", "B"],
            f"返回值的 meta 里带聚合物链清单：{result.meta.get('chains')}",
        )

        # 核心不变量：同一编号第二次调用直接读盘、一次都不联网。
        before = len(calls)
        again = await fetch.execute({"pdb_id": "1M17"}, context=ledger)
        check(not again.failed, "第二次取同一个编号成功")
        check(
            len(calls) == before,
            f"第二次调用一次请求都没发（读的是写盘那份）：新增 {len(calls) - before} 次",
        )
        check("读自本地文件" in again.output and "未联网" in again.output, "摘要说明这次是读盘")
        check(
            "未重复登记" in again.output,
            f"同一份结构不会重复登记：{again.output.splitlines()[-2]}",
        )
        check(len(ledger.entries()) == 1, "登记表里还是那一条")

        # refresh=true 才重新联网。
        calls.clear()
        refreshed = await fetch.execute({"pdb_id": "1M17", "refresh": True}, context=ledger)
        check(
            len([item for item in calls if item[0] == "GET"]) == 1,
            f"refresh=true 时重新下载一次：{calls}",
        )
        check("已下载并写盘" in refreshed.output, "摘要说明这次是重新下的")
        check(len(ledger.entries()) == 1, "重新下载仍不重复登记")

        # 编号不存在：两个格式都没有 → 一档错，不重试。
        missing = await fetch.execute({"pdb_id": "9ZZZ"}, context=ledger)
        check(missing.failed, "不存在的编号失败")
        check("没有编号为 9ZZZ 的条目" in missing.output, f"说清是编号不存在：{missing.output}")
        check(
            "超出 PDB 格式的上限" not in missing.output,
            "编号不存在那一档不要说成「太大」",
        )
        check(
            not missing.output.startswith("内核装配问题"),
            "业务失败不该被当成装配问题",
        )

        # 只有 mmCIF（超大结构）→ 另一档错，说清原因并给出可走的路。
        # 6VXX 在这个假传输里 .pdb 是 404、.cif 是 200，正是「超出 PDB 上限」那条路。
        huge = await fetch.execute({"pdb_id": "6VXX"}, context=ledger)
        check(huge.failed, "超出 PDB 上限的编号失败")
        check(
            "超出了 PDB 格式的上限" in huge.output and "mmCIF" in huge.output,
            f"说清是超出了 PDB 上限、只提供 mmCIF：{huge.output.splitlines()[0]}",
        )
        check(
            "换一个覆盖同一蛋白" in huge.output,
            "给出可走的路（换一个等价的小结构），而不是只说「不支持」",
        )
        check(
            "没有编号为" not in huge.output,
            "「太大」那一档不能说成「编号不存在」",
        )
        check(
            calls.count(("HEAD", "https://files.rcsb.org/download/6VXX.cif")) == 1,
            f"探 .cif 用的是 HEAD：{calls}",
        )
        check(
            calls.count(("GET", "https://files.rcsb.org/download/6VXX.pdb")) == 1,
            f"只取过一次 .pdb，探 .cif 时不再下载：{calls}",
        )

        # 参数校验失败时一次请求都不发。
        calls.clear()
        bad_id = await fetch.execute({"pdb_id": "P00533"}, context=ledger)
        check(bad_id.failed and "四位" in bad_id.output, "非 PDB 编号被拦下")
        check(calls == [], f"编号非法时一次请求都不发：{calls}")

        # 没拿到登记表时是装配问题，不是参数错。
        unassigned = await fetch.execute({"pdb_id": "1M17"})
        check(
            unassigned.failed and "内核装配问题" in unassigned.output,
            "没拿到登记表时报装配问题",
        )

        # ---- import_structure ----
        imported = work / "model.pdb"
        # 刻意与远端那份**字节不同**（多一行 REMARK），但 HEADER 仍是 1M17。
        # 原来靠 `write_text` 在 Windows 上把 \n 翻成 \r\n 来凑这个差异——那是平台
        # 特性，POSIX 上两份字节完全相同，会走「内容已在登记表里」那条分支，
        # 「同名不同内容绝不覆盖」这一组断言就整段验不到了。
        imported.write_text(
            _pdb_fixture() + "REMARK 999 LOCAL IMPORT COPY\n", encoding="utf-8"
        )
        importer = structure_module.ImportStructureTool()

        calls.clear()
        first = await importer.execute({"path": str(imported)}, context=ledger)
        check(not first.failed, f"导入成功：{first.output.splitlines()[0]}")
        check(calls == [], f"导入一次网络请求都不发：{calls}")
        check("已复制进结构目录" in first.output, "摘要说明复制了一份")
        check("原文件" in first.output and "未被动过" in first.output, "说明原文件没动")

        # 这一份与远端取回的是同一个条目（HEADER 里都写着 1M17），但字节不同，
        # 所以复制到 1M17-1.pdb——**同名不同内容绝不覆盖**，覆盖掉的是另一个结构。
        copy_path = store / "1M17-1.pdb"
        check(copy_path.is_file(), f"复制进结构目录：{sorted(p.name for p in store.glob('*.pdb'))}")
        check(
            copy_path.read_bytes() == imported.read_bytes(),
            "复制是逐字节保真的（否则指纹会变）",
        )
        check(
            (store / "1M17.pdb").is_file(),
            "远端取回的那一份还在（导入没有覆盖它）",
        )
        check(
            len(list(store.glob("*.pdb"))) == 2,
            f"结构目录里有两份（远端一份、本地一份）：{sorted(p.name for p in store.glob('*.pdb'))}",
        )

        receptors = [item for item in ledger.entries() if isinstance(item.handle, ReceptorHandle)]
        check(len(receptors) == 2, f"两个受体条目（远端一个、本地一个）：{len(receptors)}")
        local_entry = receptors[-1]
        check(
            local_entry.meta.get("source") == "local",
            f"本地导入的来源标成 local：{local_entry.meta.get('source')}",
        )
        check(
            local_entry.meta.get("imported_from") == str(imported.resolve()),
            f"meta 里记下原文件位置：{local_entry.meta.get('imported_from')}",
        )
        check(
            "source_url" not in local_entry.meta,
            "本地导入不写 source_url（不写空串，免得界面上是个空链接）",
        )

        # 同一个文件再导一次：不重复登记、不在磁盘上留第二份。
        second = await importer.execute({"path": str(imported)}, context=ledger)
        check(not second.failed, "重复导入成功")
        check("未复制、未重复登记" in second.output, f"明说没复制、没重复登记：{second.output.splitlines()[-3]}")
        check(
            len([item for item in ledger.entries() if isinstance(item.handle, ReceptorHandle)]) == 2,
            "登记表里还是两个受体",
        )
        check(
            len(list(store.glob("*.pdb"))) == 2,
            f"磁盘上还是两份（重复导入没有多留一份）：{sorted(p.name for p in store.glob('*.pdb'))}",
        )

        # 同名不同内容 → 加序号，绝不覆盖（覆盖掉的是另一个结构）。
        other = work / "copy" / "1M17.pdb"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(_pdb_fixture().replace("GLY", "SER"), encoding="utf-8")
        third = await importer.execute({"path": str(other)}, context=ledger)
        check(not third.failed, "同名不同内容也能导进来")
        check(
            (store / "1M17-2.pdb").is_file(),
            f"同名不同内容加序号，不覆盖：{sorted(p.name for p in store.glob('*.pdb'))}",
        )
        check(
            copy_path.read_bytes() == imported.read_bytes(),
            "先导入的那份没被覆盖掉",
        )

        # 扩展名与格式的准入：mmCIF 单独一档错，其余列出能收的。
        mmcif = work / "big.cif"
        mmcif.write_text("data_x\n", encoding="utf-8")
        mm_result = await importer.execute({"path": str(mmcif)}, context=ledger)
        check(mm_result.failed, "mmCIF 被拒收")
        check(
            "读不了 mmCIF" in mm_result.output and "转成 .pdb" in mm_result.output,
            f"给出可走的路而不是只说「不支持」：{mm_result.output}",
        )

        wrong_ext = work / "notes.txt"
        wrong_ext.write_text("hello\n", encoding="utf-8")
        ext_result = await importer.execute({"path": str(wrong_ext)}, context=ledger)
        check(
            ext_result.failed and ".pdb" in ext_result.output,
            f"不认识的扩展名列清能收的：{ext_result.output}",
        )

        # 目录、不存在、只有杂原子、空文件、超限：五种都要在解析之前（或刚解析完）挡住。
        dir_result = await importer.execute({"path": str(store)}, context=ledger)
        check(dir_result.failed and "目录" in dir_result.output, "目录被拦下")

        gone_result = await importer.execute({"path": str(work / "nope.pdb")}, context=ledger)
        check(
            gone_result.failed and "绝对路径" in gone_result.output,
            "不存在的路径给出可读的提示",
        )

        ligand_only = work / "ligand.pdb"
        ligand_only.write_text(
            "".join([_pdb_atom(1, "C1", "NAG", "C", 1, 1.0, 1.0, 1.0, kind="HETATM")]),
            encoding="utf-8",
        )
        lig_result = await importer.execute({"path": str(ligand_only)}, context=ledger)
        check(
            lig_result.failed and "没有聚合物链" in lig_result.output,
            f"只有杂原子的文件被拦下并指出它该去哪：{lig_result.output}",
        )

        empty_file = work / "empty.pdb"
        empty_file.write_text("   \n", encoding="utf-8")
        empty_result = await importer.execute({"path": str(empty_file)}, context=ledger)
        check(empty_result.failed and "空文件" in empty_result.output, "空文件被拦下")

        text_file = work / "page.pdb"
        text_file.write_text("<html>下载页</html>\n", encoding="utf-8")
        page_result = await importer.execute({"path": str(text_file)}, context=ledger)
        check(
            page_result.failed and "没有 ATOM" in page_result.output,
            f"HTML 下载页改了后缀被拦下：{page_result.output.splitlines()[0]}",
        )

        unassigned_import = await importer.execute({"path": str(imported)})
        check(
            unassigned_import.failed and "内核装配问题" in unassigned_import.output,
            "导入没拿到登记表时报装配问题",
        )

        # 收路径的动作只有两个：其余工具的参数里不该出现 path。
        path_takers = [
            tool.spec.name
            for tool in (
                *uniprot_module.domain_tools(),
                *structure_module.domain_tools(),
                *ligand_module.domain_tools(),
            )
            if "path" in (tool.spec.parameters.get("properties") or {})
        ]
        check(
            path_takers == ["import_structure", "import_ligandset"],
            f"全库只有 import_structure 与 import_ligandset 收路径：{path_takers}",
        )

        # 纯查询/取结构的工具都收登记表，且参数模式里不列它。
        for tool in structure_module.domain_tools():
            check(
                tool.context_argument == "ledger",
                f"{tool.spec.name} 会登记条目，所以收登记表",
            )
            check(
                "ledger" not in (tool.spec.parameters.get("properties") or {}),
                f"{tool.spec.name} 的参数模式里不列 ledger（那是内核注入的）",
            )
    finally:
        structure_module.http.get_text_or_none = original_text
        structure_module.http.probe = original_probe
        structure_module.structure_dir = original_dir
        shutil.rmtree(work, ignore_errors=True)


def _chembl_target_payload() -> dict[str, object]:
    """`P00533` 的靶点候选：一个干净的单体，外加一个家族条目和一个复合物。

    这正是 `_resolve_target` 要挡的那种局面——不挡的话，EGFR 连同 ErbB2/3/4 的活性
    都会被当成 EGFR 的。
    """
    return {
        "targets": [
            {
                "target_chembl_id": "CHEMBL203",
                "pref_name": "Epidermal growth factor receptor",
                "target_type": "SINGLE PROTEIN",
                "target_components": [{"accession": "P00533"}],
            },
            {
                "target_chembl_id": "CHEMBL2363049",
                "pref_name": "EGFR/ErbB2/ErbB4 family",
                "target_type": "PROTEIN FAMILY",
                "target_components": [{"accession": "P00533"}],
            },
            {
                "target_chembl_id": "CHEMBL2111466",
                "pref_name": "MER/EGFR chimeric protein",
                "target_type": "CHIMERIC PROTEIN",
                "target_components": [{"accession": "P00533"}],
            },
        ]
    }


_CHEMBL_ACTIVITIES: tuple[dict[str, object], ...] = (
    # 最强的一条：精确测量，置信度 9。
    {
        "molecule_chembl_id": "CHEMBL939",
        "parent_molecule_chembl_id": "CHEMBL939",
        "molecule_pref_name": "GEFITINIB",
        "canonical_smiles": "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1",
        "pchembl_value": "9.00",
        "standard_type": "IC50",
        "standard_value": "1.0",
        "standard_units": "nM",
        "standard_relation": "=",
        "assay_chembl_id": "CHEMBL_ASSAY_1",
        "document_year": 2004,
        "ligand_efficiency": {"le": "0.42"},
    },
    # 同一个母体的另一种盐型：pChEMBL 更低，应当归并掉。
    {
        "molecule_chembl_id": "CHEMBL1201583",
        "parent_molecule_chembl_id": "CHEMBL939",
        "molecule_pref_name": "",
        "canonical_smiles": "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1.CS(=O)(=O)O",
        "pchembl_value": "7.10",
        "standard_type": "IC50",
        "standard_value": "80.0",
        "standard_units": "nM",
        "standard_relation": "=",
        "assay_chembl_id": "CHEMBL_ASSAY_1",
        "document_year": 2006,
        "ligand_efficiency": {"le": "0.28"},
    },
    # 不等式记录：意思是「没测到抑制」，pChEMBL 是下界不是活性值。
    {
        "molecule_chembl_id": "CHEMBL25",
        "parent_molecule_chembl_id": "CHEMBL25",
        "molecule_pref_name": "ASPIRIN",
        "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
        "pchembl_value": "5.00",
        "standard_type": "IC50",
        "standard_value": "10000",
        "standard_units": "nM",
        "standard_relation": ">",
        "assay_chembl_id": "CHEMBL_ASSAY_2",
        "document_year": 1990,
        "ligand_efficiency": {"le": "0.20"},
    },
    # 置信度只有 5：靶点归属是同源蛋白推定，属于别的蛋白的数据。
    {
        "molecule_chembl_id": "CHEMBL1234",
        "parent_molecule_chembl_id": "CHEMBL1234",
        "molecule_pref_name": "",
        "canonical_smiles": "c1ccccc1",
        "pchembl_value": "8.50",
        "standard_type": "Ki",
        "standard_value": "3.0",
        "standard_units": "nM",
        "standard_relation": "=",
        "assay_chembl_id": "CHEMBL_ASSAY_3",
        "document_year": 2010,
        "ligand_efficiency": {"le": "0.60"},
    },
    # 没有结构串：拿不到结构就没法对接。
    {
        "molecule_chembl_id": "CHEMBL9999",
        "parent_molecule_chembl_id": "CHEMBL9999",
        "molecule_pref_name": "NOSTRUCT",
        "canonical_smiles": "",
        "pchembl_value": "8.00",
        "standard_type": "IC50",
        "standard_value": "10.0",
        "standard_units": "nM",
        "standard_relation": "=",
        "assay_chembl_id": "CHEMBL_ASSAY_1",
        "document_year": 2011,
        "ligand_efficiency": {},
    },
    # 第二条真正可用的分子（另一个母体）。
    {
        "molecule_chembl_id": "CHEMBL553",
        "parent_molecule_chembl_id": "CHEMBL553",
        "molecule_pref_name": "ERLOTINIB",
        "canonical_smiles": "C#Cc1cccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)c1",
        "pchembl_value": "7.80",
        "standard_type": "Ki",
        "standard_value": "16.0",
        "standard_units": "nM",
        "standard_relation": "=",
        "assay_chembl_id": "CHEMBL_ASSAY_1",
        "document_year": 2012,
        "ligand_efficiency": {"le": "0.31"},
    },
)

_CHEMBL_ASSAYS: tuple[dict[str, object], ...] = (
    {"assay_chembl_id": "CHEMBL_ASSAY_1", "confidence_score": 9, "assay_type": "B"},
    {"assay_chembl_id": "CHEMBL_ASSAY_2", "confidence_score": 9, "assay_type": "B"},
    {"assay_chembl_id": "CHEMBL_ASSAY_3", "confidence_score": 5, "assay_type": "B"},
)

_CHEMBL_ANALOGS: tuple[dict[str, object], ...] = (
    # 查询分子自己（按母体编号判等，应当被排除）。
    {
        "molecule_chembl_id": "CHEMBL939",
        "similarity": 100.0,
        "pref_name": "GEFITINIB",
        "molecule_hierarchy": {"parent_chembl_id": "CHEMBL939"},
        "molecule_structures": {"canonical_smiles": "QUERY"},
        "molecule_type": "Small molecule",
        "max_phase": 4,
        "molecule_properties": {"mw_freebase": "446.9", "alogp": "3.2", "num_ro5_violations": 0},
    },
    # 查询分子的盐型：相似度 100，与上面同一个母体——两条都会消失。
    {
        "molecule_chembl_id": "CHEMBL1201583",
        "similarity": 100.0,
        "pref_name": "",
        "molecule_hierarchy": {"parent_chembl_id": "CHEMBL939"},
        "molecule_structures": {"canonical_smiles": "QUERY.SALT"},
        "molecule_type": "Small molecule",
        "max_phase": None,
        "molecule_properties": {},
    },
    # 一个真正的新分子。
    {
        "molecule_chembl_id": "CHEMBL553",
        "similarity": 91.9,
        "pref_name": "ERLOTINIB",
        "molecule_hierarchy": {"parent_chembl_id": "CHEMBL553"},
        "molecule_structures": {"canonical_smiles": "C#Cc1cccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)c1"},
        "molecule_type": "Small molecule",
        "max_phase": 4,
        "molecule_properties": {
            "mw_freebase": "393.4",
            "alogp": "3.1",
            "qed_weighted": "0.62",
            "num_ro5_violations": 0,
        },
    },
    # 没有结构式的记录：进不了配体集。
    {
        "molecule_chembl_id": "CHEMBL8888",
        "similarity": 88.0,
        "pref_name": "",
        "molecule_hierarchy": {"parent_chembl_id": "CHEMBL8888"},
        "molecule_structures": {"canonical_smiles": None},
        "molecule_type": "Unknown",
        "max_phase": None,
        "molecule_properties": {},
    },
    # 第三条可用分子（相似度最低）。
    {
        "molecule_chembl_id": "CHEMBL1234",
        "similarity": 86.5,
        "pref_name": "",
        "molecule_hierarchy": {"parent_chembl_id": "CHEMBL1234"},
        "molecule_structures": {"canonical_smiles": "c1ccc2c(c1)cccc2"},
        "molecule_type": "Small molecule",
        "max_phase": 0,
        "molecule_properties": {
            "mw_freebase": "128.2",
            "alogp": "2.1",
            "qed_weighted": "0.44",
            "num_ro5_violations": 1,
        },
    },
)

_CHEMBL_MOLECULE: dict[str, object] = {
    "molecule_chembl_id": "CHEMBL939",
    "pref_name": "GEFITINIB",
    "molecule_type": "Small molecule",
    "structure_type": "MOL",
    "molecule_structures": {
        "canonical_smiles": "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1",
        "standard_inchi_key": "XGALLCVXEZPNRQ-UHFFFAOYSA-N",
    },
    "molecule_properties": {
        "mw_freebase": "446.9",
        "alogp": "3.20",
        "hbd": 2,
        "hba": 8,
        "psa": "68.7",
        "rtb": 8,
        "aromatic_rings": 3,
        "heavy_atoms": 32,
        "full_molformula": "C22H24ClFN4O3",
        "qed_weighted": "0.63",
        "num_ro5_violations": 0,
    },
    "molecule_hierarchy": {"parent_chembl_id": "CHEMBL939"},
    "max_phase": 4,
    "first_approval": 2015,
    "withdrawn_flag": False,
    "black_box_warning": 0,
    "oral": True,
    "parenteral": False,
    "topical": False,
    "prodrug": False,
    "natural_product": False,
    "orphan": False,
    "molecule_synonyms": [{"molecule_synonym": "IRESSA"}, {"molecule_synonym": "ZD1839"}],
    "atc_classifications": ["L01EB01"],
    "chirality": 0,
}


async def check_chembl_tools() -> None:
    """ChEMBL 三个工具，全程用假传输与临时缓存目录（不联网、不碰真实家目录）。"""
    work = PROBE / "chembl_tools"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    store = work / "chembl"

    calls: list[tuple[str, dict[str, str]]] = []

    def transport(
        url: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 45.0,
        headers=None,
        server_5xx_as_error: str = "",
    ) -> object:
        clean = {str(key): str(value) for key, value in (params or {}).items()}
        calls.append((url, clean))
        if url.endswith("/target.json"):
            if "target_chembl_id__in" in clean:
                return {
                    "targets": [
                        {"target_chembl_id": "CHEMBL203", "pref_name": "Epidermal growth factor receptor"}
                    ]
                }
            return _chembl_target_payload()
        if url.endswith("/activity.json"):
            # 计数那两次只取 page_meta：一条记录都不带。
            if clean.get("limit") == "1":
                total = 4 if "pchembl_value__gte" in clean else 120
                return {"page_meta": {"total_count": total}, "activities": []}
            return {"activities": [dict(item) for item in _CHEMBL_ACTIVITIES]}
        if url.endswith("/assay.json"):
            return {"assays": [dict(item) for item in _CHEMBL_ASSAYS]}
        if url.endswith("/mechanism.json"):
            return {
                "mechanisms": [
                    {
                        "target_chembl_id": "CHEMBL203",
                        "mechanism_of_action": "Epidermal growth factor receptor erbB1 inhibitor",
                        "action_type": "INHIBITOR",
                    }
                ]
            }
        if url.endswith("/drug_indication.json"):
            return {
                "page_meta": {"total_count": 2},
                "drug_indications": [
                    {"mesh_heading": "Carcinoma, Non-Small-Cell Lung", "max_phase_for_ind": 4},
                    {"mesh_heading": "Breast Neoplasms", "max_phase_for_ind": 2},
                ],
            }
        if "/similarity/" in url:
            return {
                "page_meta": {"total_count": 5},
                "molecules": [dict(item) for item in _CHEMBL_ANALOGS],
            }
        if "/molecule/" in url:
            return dict(_CHEMBL_MOLECULE)
        raise AssertionError(f"假传输没准备这个地址：{url}")

    original_json = chembl_module._request_json
    original_dir = chembl_module.chembl_cache_dir
    chembl_module._request_json = transport
    chembl_module.chembl_cache_dir = lambda: store
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        # ---- search_actives ----
        search = chembl_module.SearchActivesTool()
        result = await search.execute({"accession": "P00533"}, context=ledger)
        check(not result.failed, f"取已知活性成功：{result.output.splitlines()[0]}")
        check(
            "ChEMBL 靶点 CHEMBL203" in result.output and "单一蛋白" in result.output,
            f"摘要首行报出选中的靶点：{result.output.splitlines()[0]}",
        )
        check(
            "2 个同编号的复合物/家族条目已排除" in result.output,
            f"排除掉的家族/复合物要交代：{result.output}",
        )
        check("IC50" in result.output, "报出测定类型")
        check("GEFITINIB" in result.output, "报出已知药名")

        # 四类剔除各算各的，且四类都在这一次里出现过。
        check(
            "未测到抑制的不等式" in result.output,
            f"不等式记录单独一类：{result.output}",
        )
        check(
            "测定置信度不足" in result.output,
            f"低置信度单独一类（它是别的蛋白的数据）：{result.output}",
        )
        check("没有结构" in result.output, f"缺结构串单独一类：{result.output}")
        check(
            "合并掉 1 条记录" in result.output,
            f"同一母体的盐型被归并：{result.output}",
        )

        # 服务端筛选：只允许实测生效的那几个参数进去。
        activity_params = [
            item[1] for item in calls if item[0].endswith("/activity.json")
        ]
        for params in activity_params:
            check(
                not any(key.startswith("confidence_score") for key in params),
                f"不往服务端发 confidence_score（ChEMBL 会静默忽略它）：{params}",
            )
            check(
                not any(key.startswith("assay__") for key in params),
                f"不往服务端发跨表过滤（同样会被静默忽略）：{params}",
            )
        check(
            any(params.get("order_by") == "-pchembl_value" for params in activity_params),
            f"取活性那一页必须排序（否则到上限时留下的是随意一批）：{activity_params}",
        )
        check(
            any(params.get("potential_duplicate") == "0" for params in activity_params),
            "重复测定在服务端就挡住",
        )

        entries = ledger.entries()
        check(len(entries) == 1, f"登记了一条：{len(entries)}")
        entry = entries[0]
        check(
            isinstance(entry.handle, LigandSetHandle),
            f"登记成 ligandset（不是 receptor）：{entry.handle}",
        )
        check(
            entry.meta.get("target_chembl_id") == "CHEMBL203",
            f"meta 里记下选中哪个靶点：{entry.meta.get('target_chembl_id')}",
        )
        check(
            entry.meta.get("activity_records_total") == 120,
            f"meta 里记下该靶点总记录数：{entry.meta.get('activity_records_total')}",
        )
        check(
            len(entry.meta.get("top_molecules") or []) == 2,
            f"top_molecules 只放取回的这几个（不给一千个）：{len(entry.meta.get('top_molecules') or [])}",
        )
        check(
            entry.meta.get("pchembl_max") == 9.0 and entry.meta.get("pchembl_min") == 7.8,
            f"meta 里两条可用分子的活性两端：{entry.meta.get('pchembl_min')}–{entry.meta.get('pchembl_max')}",
        )
        check(entry.path is not None and entry.path.is_file(), "条目指着写盘那份 CSV")
        written = entry.path.read_text(encoding="utf-8").split("\n")
        check(
            written[0].startswith("molecule_chembl_id,parent_molecule_chembl_id"),
            f"CSV 表头是那几列：{written[0][:40]}",
        )
        check(len([line for line in written if line]) == 3, "两条分子加一行表头")
        check(
            "GEFITINIB" in written[1] and "ERLOTINIB" in written[2],
            f"按活性降序写盘：{written[1][:30]} / {written[2][:30]}",
        )

        # 核心不变量：同一组条件第二次调用纯读盘，一次都不联网。
        before = len(calls)
        again = await search.execute({"accession": "P00533"}, context=ledger)
        check(not again.failed, "第二次取同一组条件成功")
        check(
            len(calls) == before,
            f"第二次调用一次请求都没发（纯读盘）：新增 {len(calls) - before} 次",
        )
        check("读自本地缓存" in again.output and "未联网" in again.output, "摘要说明这次是读缓存")
        check("未重复登记" in again.output, "同一份配体集不重复登记")
        check(len(ledger.entries()) == 1, "登记表里还是那一条")

        # 换一组条件要另起一份文件，不能盖掉前一份。
        before_paths = len(list(store.glob("*_actives_*.csv")))
        strict = await search.execute({"accession": "P00533", "min_pchembl": 8.0}, context=ledger)
        check(not strict.failed, "换一组条件也能跑")
        check(
            len(list(store.glob("*_actives_*.csv"))) == before_paths + 1,
            f"条件不同写盘到不同文件（不互相覆盖）：{sorted(p.name for p in store.glob('*_actives_*.csv'))}",
        )
        check(
            "读自本地缓存" not in strict.output,
            "条件变了缓存不命中，要重新联网",
        )
        check(
            "未重复登记" in strict.output,
            f"内容相同的分子集不重复登记（同一批分子换个阈值取回还是它）：{strict.output.splitlines()[-1]}",
        )
        check(
            "2 个同编号的复合物/家族条目已排除" in strict.output,
            f"家族与嵌合蛋白两条都算进排除数：{strict.output}",
        )

        # 孤儿靶点：总记录数为零要单独说成「孤儿靶点」。
        def empty_transport(
            url: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float = 45.0,
            headers=None,
            server_5xx_as_error: str = "",
        ) -> object:
            clean = {str(key): str(value) for key, value in (params or {}).items()}
            if url.endswith("/target.json"):
                return {
                    "targets": [
                        {
                            "target_chembl_id": "CHEMBL6000",
                            "pref_name": "Orphan kinase",
                            "target_type": "SINGLE PROTEIN",
                            "target_components": [{"accession": "Q9Y6K9"}],
                        }
                    ]
                }
            return {"page_meta": {"total_count": 0}, "activities": []}

        chembl_module._request_json = empty_transport
        orphan = await search.execute({"accession": "Q9Y6K9"}, context=ledger)
        chembl_module._request_json = transport
        check(not orphan.failed, "孤儿靶点不是失败，是一条要先说清楚的结论")
        check(
            "孤儿靶点" in orphan.output,
            f"总记录数为零时说成孤儿靶点：{orphan.output}",
        )
        check(
            "可信度差一个量级" in orphan.output,
            "说明孤儿靶点的后果，而不只是报个零",
        )
        check(
            len(ledger.entries()) == 1,
            f"没有分子就不登记（登记表里还是那一条）：{len(ledger.entries())}",
        )

        # 参数校验与装配问题。
        calls.clear()
        bad = await search.execute({"accession": "EGFR"}, context=ledger)
        check(bad.failed and "UniProt" in bad.output, f"基因名被拦下：{bad.output[:40]}")
        check(calls == [], f"编号非法时一次请求都不发：{calls}")

        unassigned = await search.execute({"accession": "P00533"})
        check(
            unassigned.failed and "内核装配问题" in unassigned.output,
            "没拿到登记表时报装配问题",
        )

        # ---- find_analogs ----
        analogs = chembl_module.FindAnalogsTool()
        calls.clear()
        found = await analogs.execute({"query": "CHEMBL939"}, context=ledger)
        check(not found.failed, f"找类似物成功：{found.output.splitlines()[0]}")
        check(
            "CHEMBL939（GEFITINIB）" in found.output,
            f"摘要里报出查询是谁：{found.output.splitlines()[0]}",
        )
        check("91.9%" in found.output, f"报出相似度：{found.output}")
        check(
            "已按母体归并" not in found.output and "合并掉 0 条" not in found.output,
            "没有归并掉的时候不多写一句",
        )
        check(
            "没有结构式，已剔除" in found.output,
            f"缺结构串的那条单独交代：{found.output}",
        )
        check(
            "相似性只看结构，与活性无关" in found.output,
            "必须写明这批分子不代表有活性",
        )

        similar_calls = [item for item in calls if "/similarity/" in item[0]]
        check(len(similar_calls) == 1, f"取类似物只发一次请求：{len(similar_calls)}")
        check(
            similar_calls[0][1].get("limit") == "200",
            f"默认只取一页：{similar_calls[0][1]}",
        )
        check(
            "/85.json" in similar_calls[0][0],
            f"默认阈值 85 拼进 URL 路径：{similar_calls[0][0]}",
        )

        analog_entries = [
            item for item in ledger.entries() if isinstance(item.handle, LigandSetHandle)
        ]
        check(len(analog_entries) == 2, f"现在两个配体集（活性那一个 + 类似物这一个）：{len(analog_entries)}")
        analog_entry = analog_entries[-1]
        check(
            analog_entry.meta.get("kind") == "analogs",
            f"meta 里标出这是类似物：{analog_entry.meta.get('kind')}",
        )
        check(
            analog_entry.meta.get("molecule_count") == 2,
            f"查询分子自己与它的盐型都排掉了（5 条记录里去重与剔除后剩 2 个）："
            f"{analog_entry.meta.get('molecule_count')}",
        )
        check(
            analog_entry.meta.get("records_without_structure") == 1,
            f"缺结构串的计数：{analog_entry.meta.get('records_without_structure')}",
        )
        check(
            analog_entry.meta.get("similarity_threshold") == 85,
            f"meta 里记下阈值：{analog_entry.meta.get('similarity_threshold')}",
        )
        analog_rows = analog_entry.path.read_text(encoding="utf-8").split("\n")
        check(
            analog_rows[0].startswith("molecule_chembl_id,parent_molecule_chembl_id"),
            f"类似物表的表头：{analog_rows[0][:40]}",
        )
        check(
            "91.9" in analog_rows[1] and "86.5" in analog_rows[2],
            f"按相似度降序写盘：{analog_rows[1][:30]} / {analog_rows[2][:30]}",
        )

        before = len(calls)
        again_analogs = await analogs.execute({"query": "CHEMBL939"}, context=ledger)
        check(
            len(calls) == before,
            f"第二次找同一批类似物一次请求都不发：新增 {len(calls) - before} 次",
        )
        check("读自本地缓存" in again_analogs.output, "摘要说明这次是读缓存")

        # 阈值越界要拒绝，不是悄悄截断——截断之后「我要 30」与「我要 85」拿到同一份。
        calls.clear()
        low = await analogs.execute({"query": "CHEMBL939", "min_similarity": 30}, context=ledger)
        check(low.failed and "40" in low.output, f"阈值低于 40 被拒：{low.output[:60]}")
        check(calls == [], f"阈值越界时一次请求都不发：{calls}")

        # 给结构串那条路：不联网解析编号，直接拼进 URL 路径。
        calls.clear()
        by_smiles = await analogs.execute({"query": "c1ccccc1"}, context=ledger)
        check(not by_smiles.failed, f"按结构串查也能跑：{by_smiles.output.splitlines()[0]}")
        check(
            len([item for item in calls if "/molecule/" in item[0]]) == 0,
            f"给结构串时不去解析编号：{calls}",
        )
        smiles_calls = [item for item in calls if "/similarity/" in item[0]]
        check(
            "c1ccccc1" in smiles_calls[0][0],
            f"结构串拼进路径：{smiles_calls[0][0]}",
        )

        # 结构串写坏：只在本地拦，不联网（ChEMBL 对这类输入回 HTTP 500）。
        calls.clear()
        broken = await analogs.execute({"query": "XYZ123"}, context=ledger)
        check(broken.failed, "写坏的结构串被拦下")
        check(calls == [], f"结构串写坏时一次请求都不发（不让它去撞 500）：{calls}")

        unassigned_analogs = await analogs.execute({"query": "CHEMBL939"})
        check(
            unassigned_analogs.failed and "内核装配问题" in unassigned_analogs.output,
            "找类似物没拿到登记表时报装配问题",
        )

        # ---- describe_molecule ----
        describe = chembl_module.DescribeMoleculeTool()
        before_entries = len(ledger.entries())
        calls.clear()
        profile = await describe.execute({"molecule": "chembl939"}, context=ledger)
        check(not profile.failed, f"查画像成功：{profile.output.splitlines()[0]}")
        check(
            "ChEMBL 分子 CHEMBL939 · GEFITINIB" in profile.output,
            f"首行报出编号与药名：{profile.output.splitlines()[0]}",
        )
        check("已上市药物" in profile.output, f"max_phase 4 说成已上市：{profile.output}")
        check("2015 年首次批准" in profile.output, "报出首次批准年份")
        check("XGALLCVXEZPNRQ-UHFFFAOYSA-N" in profile.output, "报出 InChIKey")
        check(
            "Lipinski 五规则违反 0 项" in profile.output,
            f"报出类药性：{profile.output}",
        )
        check(
            "QED 0.63" in profile.output,
            f"报出 QED：{profile.output}",
        )
        check(
            "Epidermal growth factor receptor" in profile.output
            and "（INHIBITOR）" in profile.output,
            f"作用机制里的靶点名解析出来了：{profile.output}",
        )
        check(
            "Carcinoma, Non-Small-Cell Lung" in profile.output,
            f"适应症拼出来了：{profile.output}",
        )
        check("IRESSA" in profile.output, "报出别名")
        check("L01EB01" in profile.output, "报出 ATC 分类")
        check(
            "每次调用现查，不写盘缓存" in profile.output,
            "说明这是现查的，数据会随远端变",
        )

        check(
            len(ledger.entries()) == before_entries,
            f"describe_molecule 不登记条目（纯查询）：{len(ledger.entries())} == {before_entries}",
        )
        check(
            describe.context_argument == "",
            "纯查询不声明 context_argument（挂了登记表会让模型以为它留下了东西）",
        )
        check(
            "ledger" not in (describe.spec.parameters.get("properties") or {}),
            "参数模式里不列 ledger",
        )

        # 生物药（只有序列）要单独一档说明，不能带着空串去撞 500。
        def biological_transport(
            url: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float = 45.0,
            headers=None,
            server_5xx_as_error: str = "",
        ) -> object:
            return {
                "molecule_chembl_id": "CHEMBL1201583",
                "pref_name": "BEVACIZUMAB",
                "molecule_type": "Antibody",
                "structure_type": "SEQ",
                "molecule_structures": {"canonical_smiles": None},
                "molecule_hierarchy": {"parent_chembl_id": "CHEMBL1201583"},
            }

        chembl_module._request_json = biological_transport
        not_small = await describe.execute({"molecule": "CHEMBL1201583"}, context=ledger)
        chembl_module._request_json = transport
        check(not not_small.failed, "生物药也能出画像（结构那一段说明它不能对接）")
        check(
            "没有结构式" in not_small.output and "不能拿来对接" in not_small.output,
            f"说清没有结构的后果：{not_small.output}",
        )

        bad_id = await describe.execute({"molecule": "gefitinib"}, context=ledger)
        check(
            bad_id.failed and "CHEMBL939" in bad_id.output,
            f"非编号输入被拦下并指路：{bad_id.output[:60]}",
        )

        # ---- 装配：模块导出三个工具，收登记表的只有前两个 ----
        tools = chembl_module.domain_tools()
        check(
            [tool.spec.name for tool in tools]
            == ["search_actives", "find_analogs", "describe_molecule"],
            f"模块导出三个工具：{[t.spec.name for t in tools]}",
        )
        check(
            [tool.context_argument for tool in tools] == ["ledger", "ledger", ""],
            f"只有会登记的两个收登记表：{[t.context_argument for t in tools]}",
        )
        for tool in tools:
            check(
                "ledger" not in (tool.spec.parameters.get("properties") or {}),
                f"{tool.spec.name} 的参数模式里不列 ledger（那是内核注入的）",
            )
            check(
                tool.primary_argument in (tool.spec.parameters.get("required") or []),
                f"{tool.spec.name} 的定位参数是必填：{tool.primary_argument}",
            )
    finally:
        chembl_module._request_json = original_json
        chembl_module.chembl_cache_dir = original_dir
        shutil.rmtree(work, ignore_errors=True)


def check_chemistry_gate() -> None:
    """化学依赖守门人：AST 扫全仓、真导入探测、缺依赖时的说法。"""
    # ---- 一、AST 扫全仓：任何模块顶层都不许 import 化学依赖 ----
    # 这一条是整个守门人的前提。顶层的 import 会把「可选依赖」变成「启动前提」：
    # 没装化学依赖的人连会话都开不起来，而它们本该只影响一个工具。
    CHEM_NAMES = {"rdkit", "meeko", "gemmi"}
    offenders: list[str] = []
    scanned = 0
    for path in sorted((ROOT / "opencode_tui").rglob("*.py")):
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            # 只扫模块顶层（tree.body 是顶层语句），函数体里的 import 是允许的
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in CHEM_NAMES:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    check(scanned >= 40, f"AST 扫描真的走了全仓：扫到 {scanned} 个模块")
    check(
        not offenders,
        "顶层不许 import 化学依赖（一律延迟到函数体里）："
        + ("、".join(offenders) if offenders else "没有一处"),
    )

    # 反面证据：扫得到的东西真的会被认出来。**没有这一条，上面那条在
    # 「AST 遍历写错了、一个节点都没取到」时也会报通过。**
    fake_source = "import rdkit\nfrom meeko import MoleculePreparation\n"
    fake_tree = ast.parse(fake_source)
    fake_hits = [
        node.lineno
        for node in fake_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    check(
        len(fake_hits) == 2,
        f"顶层 import 的识别本身有效（两行坏输入都被取到）:{fake_hits}",
    )
    nested = ast.parse("def f():\n    import rdkit\n")
    check(
        not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in nested.body),
        "函数体里的 import 不算顶层（延迟导入这条路要放行）",
    )

    # ---- 二、真导入探测，不是 find_spec ----
    source = (ROOT / "opencode_tui" / "core" / "agent" / "domain" / "chemistry.py").read_text(
        encoding="utf-8"
    )
    check(
        "importlib.import_module" in source,
        "探测走真导入（find_spec 看不见「装了却导不进来」）",
    )
    check(
        "exc.name" in source,
        "报的是真正缺席的那个模块（否则会让用户重装已有的东西）",
    )
    check(
        "装了但导入失败" in source,
        "区分「缺」与「坏了」两种情形，措辞不同",
    )
    # 行为侧的证据：造一个「装了但导入就炸」的模块，探测要报「坏了」而不是报「缺」。
    # 两句措辞的区别是有用的：前者要用户去查环境，后者要用户去装。
    checks_before = len(CHECKS)
    original_deps = chemistry_module.CHEM_DEPS
    broken_module = PROBE / "broken_dep"
    broken_module.mkdir(parents=True, exist_ok=True)
    (broken_module / "__init__.py").write_text(
        "raise RuntimeError('这个模块装上了，但一导入就炸')\n", encoding="utf-8"
    )
    sys.path.insert(0, str(broken_module.parent))
    try:
        chemistry_module.CHEM_DEPS = ("broken_dep",)
        chemistry_module._probe_deps.cache_clear()
        broken = chemistry_module.missing_chem()
        check(
            len(broken) == 1 and "装了但导入失败" in broken[0],
            f"「装了但导入失败」单独一档，带上原因：{broken}",
        )
        check(
            "RuntimeError" in broken[0],
            f"原因带上异常类型（不然没法查）：{broken[0]}",
        )
        # 缺 vs 坏：不存在的模块只报名字，不带那半句说明。
        chemistry_module.CHEM_DEPS = ("definitely_not_installed_pkg_zzz",)
        chemistry_module._probe_deps.cache_clear()
        absent = chemistry_module.missing_chem()
        check(
            absent == ["definitely_not_installed_pkg_zzz"],
            f"「缺」只报裸名字（那是要装的东西）：{absent}",
        )
    finally:
        chemistry_module.CHEM_DEPS = original_deps
        chemistry_module._probe_deps.cache_clear()
        sys.path.remove(str(broken_module.parent))
        shutil.rmtree(broken_module, ignore_errors=True)
    check(len(CHECKS) > checks_before, "上面这几条真的记进去了")

    # 缓存：一个会话里只探一遍。第一次调用之后就命中缓存。
    chemistry_module._probe_deps.cache_clear()
    first = chemistry_module.missing_chem()
    info = chemistry_module._probe_deps.cache_info()
    check(info.currsize == 1, f"探测结果有缓存：{info}")
    second = chemistry_module.missing_chem()
    check(first == second, f"两次调用结果一致：{first} == {second}")
    chemistry_module._probe_deps.cache_clear()

    # ---- 三、缺依赖时的说法 ----
    message = chemistry_module.chem_missing_message(["rdkit", "meeko"])
    check("rdkit" in message and "meeko" in message, f"列出缺的名字：{message}")
    check(
        chemistry_module.CHEM_INSTALL_HINT in message,
        f"带安装指令（模型的原话就是把它念给用户）：{message}",
    )
    check("重启会话" in message, f"说清什么时候生效：{message}")
    check(
        "其余功能不受影响" in message,
        f"说清影响范围（不装也能用内核）：{message}",
    )

    # 安装指令指向的依赖文件真的在，而且列的名字与 CHEM_DEPS 同源。
    chem_file = ROOT / "requirements-chem.txt"
    check(chem_file.is_file(), f"依赖文件在：{chem_file}")
    listed = chem_file.read_text(encoding="utf-8")
    for name in chemistry_module.CHEM_DEPS:
        check(name in listed, f"{name} 列在依赖文件里（两份清单不能不同步）")
    check(
        chem_file.name in chemistry_module.CHEM_INSTALL_HINT,
        "安装指令指的就是这个文件",
    )

    # ---- 四、工具照常出现在清单里，只是调用时报错 ----
    # 这是设计的一部分：缺依赖就让工具从清单里消失的话，模型面对的是一个它无从解释的
    # 缺口——要么编一个工具调用，要么对用户说「我做不到」。而这里它拿到的是可操作的错。
    tools = receptor_module.domain_tools()
    check(
        [tool.spec.name for tool in tools] == ["prepare_receptor"],
        f"模块导出一个工具：{[t.spec.name for t in tools]}",
    )
    tool = tools[0]
    check(tool.context_argument == "ledger", "准备受体要收登记表")
    check(
        chemistry_module.CHEM_TOOL_DESCRIPTION_NOTE in tool.spec.description,
        "描述里带了「需要化学依赖」那句（描述第一段进工具清单）",
    )
    check(
        "prepared" in tool.spec.description,
        "描述里说清了产物会登记成 prepared 状态",
    )
    check(
        "顶层的 import" not in tool.spec.description,
        "描述是给模型看的，不该混进内部实现的话",
    )
    check(
        "ledger" not in (tool.spec.parameters.get("properties") or {}),
        "参数模式里不列 ledger（那是内核注入的）",
    )
    check(
        "structure" in (tool.spec.parameters.get("required") or []),
        "定位参数 structure 是必填",
    )

    ligand_tools = ligand_module.domain_tools()
    check(
        [item.spec.name for item in ligand_tools]
        == ["import_ligandset", "prepare_ligands"],
        f"配体模块导出导入与准备两个工具：{[item.spec.name for item in ligand_tools]}",
    )
    for item in ligand_tools:
        check(
            chemistry_module.CHEM_TOOL_DESCRIPTION_NOTE in item.spec.description,
            f"{item.spec.name} 的描述里带了「需要化学依赖」那句",
        )
        check(
            item.context_argument == "ledger",
            f"{item.spec.name} 会登记条目，所以要收登记表",
        )
        check(
            "ledger" not in (item.spec.parameters.get("properties") or {}),
            f"{item.spec.name} 的参数模式里不列 ledger",
        )
    import_tool, prepare_tool = ligand_tools
    check(
        "path" in (import_tool.spec.parameters.get("required") or []),
        "配体导入的 path 是必填",
    )
    check(
        "ligands" in (prepare_tool.spec.parameters.get("required") or []),
        "配体准备的 ligands 是必填",
    )
    check(
        prepare_tool.spec.parameters.get("properties", {})
        .get("confirm", {})
        .get("type")
        == "boolean",
        "配体准备的 confirm 是布尔（两阶段开关）",
    )

    docking_tools = docking_module.domain_tools()
    check(
        [item.spec.name for item in docking_tools] == ["dock"],
        f"对接模块导出一个工具：{[item.spec.name for item in docking_tools]}",
    )
    dock_tool = docking_tools[0]
    check(
        chemistry_module.CHEM_TOOL_DESCRIPTION_NOTE in dock_tool.spec.description,
        "对接的描述里带了「需要化学依赖」那句",
    )
    check(
        dock_tool.context_argument == "ledger",
        "对接会登记 artifact，所以要收登记表",
    )
    check(
        "ledger" not in (dock_tool.spec.parameters.get("properties") or {}),
        "对接的参数模式里不列 ledger",
    )
    check(
        {"receptor", "ligands"}.issubset(set(dock_tool.spec.parameters.get("required") or [])),
        "对接的 receptor 与 ligands 都是必填",
    )
    check(
        "越低越好" in dock_tool.spec.description,
        "描述第一段写清分数方向（模型最先看到的那一行）",
    )


async def check_receptor_tool() -> None:
    """准备受体：依赖闸、编辑计划、两阶段确认、按参数缓存。

    全程用**假的参数化函数**替掉 Meeko（本机没装化学依赖，而且真跑一遍要一分多钟）。
    替掉的只有「模板匹配 → PDBQT」那一段，其余的文本编辑、算盒子、产物目录、
    写盘、登记全部是真的——那些才是这个工具自己的逻辑。
    """
    work = PROBE / "receptor_tool"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_parameterize = receptor_module.parameterize
    original_dir = receptor_module.prepared_receptor_dir
    original_probe = chemistry_module._probe_deps

    calls: list[tuple[str, bool]] = []

    def fake_parameterize(receptor_text: str, *, relax: bool) -> object:
        calls.append((receptor_text, relax))
        atom_lines = [line for line in receptor_text.splitlines() if line[:6].strip() in ("ATOM", "HETATM")]
        if "@BAD@" in receptor_text:
            raise receptor_module.ChemRejection(
                "Residues with alternate location: A:1",
                "Fix them manually.\nOr use relax mode.",
            )
        return receptor_module.Parameterization(
            pdbqt="".join(f"ATOM  {index:>5}  CA  ALA A   1\n" for index in range(1, len(atom_lines) + 1)),
            atoms=len(atom_lines),
            monomers=len(atom_lines),
            ignored=(),
            residues_with_added_h=len(atom_lines),
            residues_with_excess_h=(),
            excess_bonds=(),
            chosen_by_fewest_missing_h=(),
        )

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        # ---- 一、缺依赖时：一条带安装指令的错，什么都不写盘 ----
        tool = receptor_module.PrepareReceptorTool()

        # 先造一条原始结构条目（不经过 fetch_structure：这里只关心准备受体那一侧）。
        source_pdb = work / "1M17.pdb"
        source_pdb.write_text(_pdb_fixture(), encoding="utf-8")
        raw_entry = ledger.add(
            "receptor",
            path=source_pdb,
            summary="原始结构 PDB 1M17",
            fingerprint=fingerprint_text(source_pdb.read_text(encoding="utf-8")),
            meta={"stage": "raw"},
        )

        chemistry_module._probe_deps.cache_clear()
        chemistry_module._probe_deps = lambda: ("rdkit", "meeko")  # type: ignore[assignment]
        blocked = await tool.execute({"structure": str(raw_entry.handle)}, context=ledger)
        check(blocked.failed, "缺依赖时准备受体失败")
        check(
            chemistry_module.CHEM_INSTALL_HINT in blocked.output,
            f"错误里带着安装指令：{blocked.output}",
        )
        check(
            chemistry_module.CHEM_DEPS[0] in blocked.output,
            f"说清缺的是哪一个：{blocked.output}",
        )
        check(
            not (work / "out").exists(),
            "缺依赖时一个文件都没写（在写盘之前就停住）",
        )
        check(
            len(ledger.entries()) == 1,
            f"缺依赖时没有新条目：{len(ledger.entries())}",
        )

        # ---- 装上依赖之后：整条路走通 ----
        # 本机真的没装化学依赖，所以这里把探测结果换成「都在场」——
        # 替掉的是「依赖在不在」这一个读数，工具自己的逻辑一行都没改。
        chemistry_module._probe_deps = lambda: ()  # type: ignore[assignment]
        check(chemistry_module.chem_available(), "（测试内）把环境假装成装好了依赖")
        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        receptor_module.prepared_receptor_dir = lambda: out_dir  # type: ignore[assignment]
        receptor_module.parameterize = fake_parameterize  # type: ignore[assignment]

        # ---- 二、参数校验 ----
        bad_padding = await tool.execute(
            {"structure": str(raw_entry.handle), "padding": 99}, context=ledger
        )
        check(
            bad_padding.failed and "笔误" in bad_padding.output,
            f"padding 超上限被拦（并说清后果）：{bad_padding.output}",
        )
        bad_altloc = await tool.execute(
            {"structure": str(raw_entry.handle), "altloc": "AB"}, context=ledger
        )
        check(bad_altloc.failed and "单个字符" in bad_altloc.output, "altloc 只能一个字符")

        missing_entry = await tool.execute({"structure": "receptor#99"}, context=ledger)
        check(
            missing_entry.failed and "receptor#99" in missing_entry.output,
            f"不存在的 handle 被拦住并指路：{missing_entry.output[:60]}",
        )

        # 拿错类型：artifact 传给要受体的参数
        artifact = ledger.add("artifact", path=work / "notes.json", summary="靶点记录")
        wrong_kind = await tool.execute({"structure": str(artifact.handle)}, context=ledger)
        check(
            wrong_kind.failed and "receptor" in wrong_kind.output,
            f"类型拿错时说清要的是什么：{wrong_kind.output}",
        )

        # ---- 三、多链且没指定链 → 拒绝并列出清单 ----
        no_chain = await tool.execute({"structure": str(raw_entry.handle)}, context=ledger)
        check(
            no_chain.failed and "必须显式指定" in no_chain.output,
            f"多链时不给 chain 直接拒绝：{no_chain.output[:80]}",
        )
        check(
            "残基" in no_chain.output,
            f"拒绝时列出各条链的残基范围（用户据它选链）：{no_chain.output}",
        )

        # ---- 四、交替构象必须显式选 ----
        altloc_needed = await tool.execute(
            {"structure": str(raw_entry.handle), "chain": "A"}, context=ledger
        )
        check(
            altloc_needed.failed and "交替构象" in altloc_needed.output,
            f"有交替构象而不给 altloc 时拒绝：{altloc_needed.output[:80]}",
        )
        check(
            "不替你做" in altloc_needed.output,
            f"说明这是刻意的（构象是化学决定）：{altloc_needed.output}",
        )

        # ---- 五、配体歧义必须显式指定 ----
        # 造一份「一条链上有两组非水杂原子」的结构：晶体学结构里常有好几张小分子
        # （缓冲液、离子、冷冻保护剂），挑中体积最大的那个恰好是配体的概率并不高。
        ambiguous_pdb = work / "ambiguous.pdb"
        ambiguous_pdb.write_text(
            "".join(
                [
                    "HEADER    TEST                                    01-JAN-00   9ZYA\n",
                    _pdb_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0),
                    _pdb_atom(100, "O", "HOH", "A", 500, 9.0, 9.0, 9.0, kind="HETATM", element=" O"),
                    _pdb_atom(200, "C1", "AQ4", "A", 600, 3.0, 3.0, 3.0, kind="HETATM"),
                    _pdb_atom(201, "C2", "AQ4", "A", 600, 3.5, 3.0, 3.0, kind="HETATM"),
                    _pdb_atom(210, "P", "PO4", "A", 700, 12.0, 12.0, 12.0, kind="HETATM", element=" P"),
                    "END\n",
                ]
            ),
            encoding="utf-8",
        )
        ambiguous = ledger.add(
            "receptor",
            path=ambiguous_pdb,
            summary="有两组杂原子的结构",
            fingerprint=fingerprint_text(ambiguous_pdb.read_text(encoding="utf-8")),
            meta={"stage": "raw"},
        )
        ligand_ambiguous = await tool.execute(
            {"structure": str(ambiguous.handle), "chain": "A"}, context=ledger
        )
        check(
            ligand_ambiguous.failed and "没法自动认定" in ligand_ambiguous.output,
            f"两组杂原子时要求指定配体：{ligand_ambiguous.output[:100]}",
        )
        check(
            "AQ4" in ligand_ambiguous.output and "PO4" in ligand_ambiguous.output,
            f"把两组都列出来（用户据它选）：{ligand_ambiguous.output}",
        )
        check(
            "ligand=whole" in ligand_ambiguous.output and "ligand=none" in ligand_ambiguous.output,
            "拒绝时给出两条可走的路（盲对接 / 不取配体）",
        )
        # 指定一个不存在的残基名：要说清「这套结构里有什么」
        not_found = await tool.execute(
            {"structure": str(ambiguous.handle), "chain": "A", "ligand": "ZZZ"}, context=ledger
        )
        check(
            not_found.failed and "没找到残基名是 'ZZZ'" in not_found.output,
            f"不存在的配体名要说清有哪些：{not_found.output[:120]}",
        )

        # ---- 六、预演：说清要做什么，一个文件都不写，一条条目都不登记 ----
        entries_before = len(ledger.entries())
        preview = await tool.execute(
            {"structure": str(raw_entry.handle), "chain": "A", "altloc": "A"},
            context=ledger,
        )
        check(not preview.failed, f"预演成功：{preview.output.splitlines()[0]}")
        check(
            "预演" in preview.output and "什么都没有写盘" in preview.output,
            f"首行就说清这是预演：{preview.output.splitlines()[0]}",
        )
        check("MEKO" not in preview.output.upper()[:20], "首行不是一句英文")
        check(
            "去掉的水：1 个残基 / 1 个原子" in preview.output,
            f"水按残基与原子两个数报出：{preview.output}",
        )
        check(
            "AQ4" in preview.output and "盒子中心" in preview.output,
            f"配体与盒子都摆出来了：{preview.output}",
        )
        check(
            "盒子边长" in preview.output and "共晶配体" in preview.output,
            "说清盒子是从哪个配体算的",
        )
        check(
            "交替构象（1 个残基）" in preview.output,
            f"交替构象的选择逐条列出：{preview.output}",
        )
        # 样例里那个残基只有 B 构象（没有 A），所以会退回它自己最小的那个。
        # **这一条要单独验**：退回是「我指定 A 但实际取了别的」——不让它说出来，
        # 用户会以为自己拿到的就是 A 构象。
        check(
            "A:1→B" in preview.output,
            f"退回的那个构象也要报出来：{preview.output}",
        )
        check(
            "没有 A 构象，退回了它自己最小的那个" in preview.output,
            f"并说清是为什么：{preview.output}",
        )
        check(
            "残基 2 个" in preview.output or "受体：2 个残基" in preview.output,
            f"报出受体规模（链 A 上 ALA 1 与 MSE 3）：{preview.output}",
        )
        # 链 C（NAG）与链 B 都被去掉，且要交代
        check(
            "去掉的链" in preview.output and "B" in preview.output,
            f"去掉的链要交代：{preview.output}",
        )
        check(
            "丢掉的多余模型：1 套" in preview.output,
            f"第二套模型被丢掉并报出：{preview.output}",
        )
        check(
            "Meeko 不做的事" in preview.output,
            f"每次都带上能力边界（别指望它做没有的事）：{preview.output}",
        )
        check(
            "confirm=true" in preview.output,
            f"告诉模型下一步要带什么：{preview.output}",
        )
        check(
            len(ledger.entries()) == entries_before,
            f"预演不登记任何条目：{len(ledger.entries())} == {entries_before}",
        )
        check(
            not list(out_dir.iterdir()),
            f"预演不写盘：{list(out_dir.iterdir())}",
        )

        # ---- 七、真做：产出目录齐了，登记成 prepared ----
        done = await tool.execute(
            {
                "structure": str(raw_entry.handle),
                "chain": "A",
                "altloc": "A",
                "confirm": True,
            },
            context=ledger,
        )
        check(not done.failed, f"真做成功：{done.output.splitlines()[0]}")
        check(
            "受体准备（完成）" in done.output,
            f"首行说清这是完成（不是预演）：{done.output.splitlines()[0]}",
        )
        product = Path(str(done.meta.get("product_dir") or ""))
        check(product.is_dir(), f"产物目录建出来了：{done.meta.get('product_dir')}")
        for name in (
            receptor_module.RECEPTOR_PDB,
            receptor_module.RECEPTOR_PDBQT,
            receptor_module.LIGAND_PDB,
            receptor_module.BOX_FILE,
            receptor_module.PARAMS_FILE,
            receptor_module.REPORT_FILE,
        ):
            check((product / name).is_file(), f"产物齐了：{name}")

        # 产物内容：receptor.pdb 里没有水、没有配体、没有链 B；ligand.pdb 是抽出来的 AQ4
        receptor_text = (product / receptor_module.RECEPTOR_PDB).read_text(encoding="utf-8")
        check("HOH" not in receptor_text, "receptor.pdb 里没有水")
        check("AQ4" not in receptor_text, "receptor.pdb 里没有共晶配体")
        check("NAG" not in receptor_text, "receptor.pdb 里没有链 C 的糖")
        check("MSE" in receptor_text, "修饰残基留在受体里（它属于聚合物）")
        check("HEADER" in receptor_text, "原文件的表头保留")
        ligand_text = (product / receptor_module.LIGAND_PDB).read_text(encoding="utf-8")
        check("AQ4" in ligand_text, "ligand.pdb 是抽出来的那个配体")
        check(
            "HETATM" in ligand_text,
            "ligand.pdb 保持 HETATM 记录（下游的配体准备认这个）",
        )
        box_text = (product / receptor_module.BOX_FILE).read_text(encoding="utf-8")
        check(
            box_text.count("=") == 6 and "center_x" in box_text,
            f"盒子文件是六行标准格式：{box_text!r}",
        )
        params = json.loads((product / receptor_module.PARAMS_FILE).read_text(encoding="utf-8"))
        check(params["chains"] == ["A"], f"参数里记着选了哪些链：{params['chains']}")
        check(params["altloc"] == "A", f"参数里记着交替构象的选择：{params['altloc']}")
        check(
            params["source_fingerprint"] == raw_entry.fingerprint,
            "参数里记着来源指纹（换了来源就不是同一份产物）",
        )
        check("box_center" in params and "meeko" in params, "盒子与 Meeko 版本都在参数里")
        report = json.loads((product / receptor_module.REPORT_FILE).read_text(encoding="utf-8"))
        check(report["kind"] == "receptor-prep", "体检报告有类型标记")
        check(
            report["edited"]["waters_removed"] == 1,
            f"报告里记着删了几个水：{report['edited']['waters_removed']}",
        )
        check(
            report["box"]["source"].startswith("共晶配体"),
            f"报告里记着盒子是从哪来的：{report['box']['source']}",
        )
        check(
            report["params_fingerprint"] == fingerprint_parts(params, length=16),
            "报告里的参数指纹与参数本身对得上",
        )
        check(
            report["source"]["stage"] == "raw" and report["source"]["pdb_id"] == "1M17",
            f"报告里记着来源的状态与编号：{report['source']}",
        )

        # 登记：一条新的 receptor，状态 prepared，父项是原始结构
        prepared = [entry for entry in ledger.entries() if entry.meta.get("stage") == "prepared"]
        check(len(prepared) == 1, f"新登记一条 prepared 受体：{len(prepared)}")
        entry = prepared[0]
        check(
            isinstance(entry.handle, ReceptorHandle),
            f"它是受体类型：{type(entry.handle).__name__}",
        )
        check(
            entry.parents == (raw_entry.handle,),
            f"血缘挂到原始结构上：{entry.parents}",
        )
        check(entry.path == product, f"条目的路径就是产物目录：{entry.path}")
        check(
            entry.fingerprint == fingerprint_path(product),
            "指纹按目录算（清单 + 内容）",
        )
        check(
            "prepared" in entry.summary or "准备后的受体" in entry.summary,
            f"摘要能独立读懂：{entry.summary}",
        )
        check(
            entry.meta.get("chains") == ["A"] and entry.meta.get("params") == params,
            "条目 meta 里带着链与完整参数（导出时不必回头看消息历史）",
        )
        check(
            "switch_receptor" in done.output,
            f"告诉模型下一步怎么让它成为当前受体：{done.output}",
        )

        # ---- 八、同一组参数第二次调用：命中缓存，不重做 ----
        parameterize_calls_before = len(calls)
        again = await tool.execute(
            {
                "structure": str(raw_entry.handle),
                "chain": "A",
                "altloc": "A",
                "confirm": True,
            },
            context=ledger,
        )
        check(not again.failed, f"第二次调用成功：{again.output.splitlines()[0]}")
        check(
            "直接复用，没有重做" in again.output,
            f"第二次直接复用：{again.output.splitlines()[0]}",
        )
        check(
            len(calls) == parameterize_calls_before,
            f"复用时不重跑参数化（调用次数没涨）：{len(calls)} == {parameterize_calls_before}",
        )
        check(
            len([e for e in ledger.entries() if e.meta.get("stage") == "prepared"]) == 1,
            "复用时不重复登记",
        )
        check(
            again.meta.get("reused") is True,
            f"结果里带出「这次是复用」这个读数：{again.meta}",
        )

        # ---- 九、换一个参数：另一个目录，绝不覆盖 ----
        other = await tool.execute(
            {
                "structure": str(raw_entry.handle),
                "chain": "A",
                "altloc": "A",
                "box_center": "3.25, 3.0, 3.0",
                "box_size": "10, 10, 10",
                "confirm": True,
            },
            context=ledger,
        )
        check(not other.failed, f"换盒子参数成功：{other.output.splitlines()[0]}")
        other_dir = Path(str(other.meta.get("product_dir") or ""))
        check(other_dir != product, f"换参数写到另一个目录：{other_dir.name} != {product.name}")
        check(other_dir.is_dir(), "另一个目录也建出来了")
        check(
            (product / receptor_module.PARAMS_FILE).is_file(),
            "旧产物没被覆盖（它可能已经派出去做过对接了）",
        )
        other_params = json.loads(
            (other_dir / receptor_module.PARAMS_FILE).read_text(encoding="utf-8")
        )
        check(
            other_params["box_source"] == "显式给出",
            f"新产物记着盒子是显式给的：{other_params['box_source']}",
        )
        check(
            other_params["box_center"] == [3.25, 3.0, 3.0],
            f"新产物记着新的盒子中心：{other_params['box_center']}",
        )
        check(
            len([e for e in ledger.entries() if e.meta.get("stage") == "prepared"]) == 2,
            "两组参数各一条条目（它们是两份不同的产物）",
        )

        # ---- 十、已经准备过的受体不能再准备一次 ----
        re_prepare = await tool.execute(
            {"structure": str(entry.handle), "chain": "A"}, context=ledger
        )
        check(
            re_prepare.failed and "已经准备过" in re_prepare.output,
            f"prepared 的受体不能再准备：{re_prepare.output[:80]}",
        )
        check(
            "回到原始结构" in re_prepare.output,
            f"拒绝时指路（换参数要去原始结构上重做）：{re_prepare.output}",
        )

        # ---- 十一、Meeko 拒绝：可操作的清单，不登记、不写盘 ----
        bad_source = work / "bad.pdb"
        bad_source.write_text("@BAD@\n" + _pdb_fixture(), encoding="utf-8")
        bad_entry = ledger.add(
            "receptor",
            path=bad_source,
            summary="一份 Meeko 会拒收的结构",
            fingerprint=fingerprint_text(bad_source.read_text(encoding="utf-8")),
            meta={"stage": "raw"},
        )
        entries_before_reject = len(ledger.entries())
        rejected = await tool.execute(
            {
                "structure": str(bad_entry.handle),
                "chain": "A",
                "altloc": "A",
                "confirm": True,
            },
            context=ledger,
        )
        check(rejected.failed, "Meeko 拒绝时算失败（没有产物、没有登记）")
        check(
            "Meeko 拒绝了这份输入" in rejected.output,
            f"说清这不是内部错误：{rejected.output[:60]}",
        )
        check(
            "Residues with alternate location" in rejected.output,
            "把 Meeko 列的具体问题原样带出来（那是可操作的清单）",
        )
        check(
            "altloc=\"A\"" in rejected.output or 'altloc="A"' in rejected.output,
            f"给出下一步怎么改：{rejected.output}",
        )
        check(
            "Meeko 不做的事" in rejected.output,
            "被拒这一条路上最该出现能力边界（模板匹配为什么失败就在那几句里）",
        )
        check(
            len(ledger.entries()) == entries_before_reject,
            f"被拒时不登记：{len(ledger.entries())} == {entries_before_reject}",
        )
        check(
            rejected.meta.get("rejected") is True,
            f"结果里带出「被拒」这个读数：{rejected.meta}",
        )

        # ---- 十二、没有配体可算盒子且没显式给 → 拒绝 ----
        no_ligand_pdb = work / "naked.pdb"
        no_ligand_pdb.write_text(
            "".join(
                [
                    "HEADER    TEST                                    01-JAN-00   9ZZZ\n",
                    _pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0),
                    _pdb_atom(2, "CA", "GLY", "A", 2, 2.0, 1.0, 1.0),
                    "END\n",
                ]
            ),
            encoding="utf-8",
        )
        naked = ledger.add(
            "receptor",
            path=no_ligand_pdb,
            summary="没有配体的结构",
            fingerprint=fingerprint_text(no_ligand_pdb.read_text(encoding="utf-8")),
            meta={"stage": "raw"},
        )
        boxless = await tool.execute({"structure": str(naked.handle)}, context=ledger)
        check(
            boxless.failed and "没有盒子可用" in boxless.output,
            f"没有盒子依据时拒绝：{boxless.output[:80]}",
        )
        check(
            "box_center" in boxless.output,
            "拒绝时给出两条路（显式给盒子 / ligand=whole）",
        )
        # 单链结构可以不写 chain
        single_ok = await tool.execute(
            {
                "structure": str(naked.handle),
                "ligand": "whole",
                "confirm": False,
            },
            context=ledger,
        )
        check(
            not single_ok.failed,
            f"单链结构不写 chain 也能走（只有一条链时不必显式指定）：{single_ok.output[:80]}",
        )
        check(
            "整份受体（盲对接）" in single_ok.output,
            f"盲对接的盒子来源要说清：{single_ok.output}",
        )

        # ---- 十三、非标准列位：给一条指路的错 ----
        broken = work / "broken.pdb"
        broken.write_text("ATOM  1 CA  ALA\n", encoding="utf-8")
        broken_entry = ledger.add(
            "receptor",
            path=broken,
            summary="读不出来的坐标",
            fingerprint=fingerprint_text(broken.read_text(encoding="utf-8")),
            meta={"stage": "raw"},
        )
        unreadable = await tool.execute(
            {"structure": str(broken_entry.handle)}, context=ledger
        )
        check(unreadable.failed, "读不出来的坐标文件失败")
        check(
            "fetch_structure" in unreadable.output and "import_structure" in unreadable.output,
            f"指路：重新取一份，或规整后再导入：{unreadable.output[:100]}",
        )

        # ---- 十四、来源文件不在了 ----
        gone_pdb = work / "gone.pdb"
        gone_pdb.write_text(_pdb_fixture(), encoding="utf-8")
        gone = ledger.add(
            "receptor",
            path=gone_pdb,
            summary="稍后被删掉的结构",
            fingerprint=fingerprint_text(gone_pdb.read_text(encoding="utf-8")),
            meta={"stage": "raw"},
        )
        gone_pdb.unlink()
        vanished = await tool.execute({"structure": str(gone.handle)}, context=ledger)
        check(
            vanished.failed and "不在了" in vanished.output,
            f"来源文件丢了要说清：{vanished.output[:80]}",
        )
        check(
            "fetch_structure" in vanished.output,
            "并指路（重新取一份）",
        )
    finally:
        receptor_module.parameterize = original_parameterize  # type: ignore[assignment]
        receptor_module.prepared_receptor_dir = original_dir  # type: ignore[assignment]
        chemistry_module._probe_deps = original_probe  # type: ignore[assignment]
        shutil.rmtree(work, ignore_errors=True)


async def check_listing_tools() -> None:
    """三个浏览工具：现核对磁盘、目录补尾斜杠、两阶段切换（预演 == 真做）。"""
    root = PROBE / "listing_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    ledger = SessionLedger(session_id="", snapshot_path=root / "listing.json")
    tools = {tool.spec.name: tool for tool in listing_module.domain_tools()}
    check(
        set(tools) == {"list_workspace", "describe_entry", "switch_receptor"},
        f"浏览那组登记了三个工具：{sorted(tools)}",
    )
    for name in tools:
        check(
            tools[name].context_argument == "ledger",
            f"{name} 要收登记表（它是那个表的浏览入口）",
        )
    check(
        tools["describe_entry"].primary_argument == "handle"
        and tools["switch_receptor"].primary_argument == "receptor",
        "判定层比对的那个参数填上了（主参数就是「这一次在操作哪一个条目」）",
    )

    # ---- 一、中间层：截断口径与目录尾斜杠 ----
    check(listing_module.render_entries([]) == "", "没有条目时渲染出空串")
    many = [
        Entry(handle=make_handle("artifact", index), summary=f"第 {index} 项")
        for index in range(1, 8)
    ]
    rendered = listing_module.render_entries(many, limit=3)
    check(
        rendered.count("\n") == 3 and "共 7 项，此处只列前 3 项" in rendered,
        f"超量时只报条数并写明「还有多少没显示」：{rendered}",
    )
    check(
        listing_module.path_display(root) == f"{root.as_posix().rstrip('/')}/",
        "目录补一个尾斜杠（文件还是目录决定了它能不能交给下一个工具）",
    )
    file_path = root / "one.pdb"
    file_path.write_text("ATOM\n", encoding="utf-8")
    check(
        listing_module.path_display(file_path) == file_path.as_posix(),
        "单个文件不带尾斜杠",
    )
    check(
        listing_module.path_display(None) == listing_module.EMPTY_PATH_NOTE,
        "没有路径的条目给一句说明",
    )

    # ---- 二、造一份有内容的登记表 ----
    raw_dir = root / "cache"
    raw_dir.mkdir()
    pdb = raw_dir / "1abc.pdb"
    pdb.write_text(_pdb_fixture(), encoding="utf-8")

    receptor = ledger.add(
        "receptor",
        path=pdb,
        summary="1ABC 原始结构",
        fingerprint=fingerprint_path(pdb),
        meta={"stage": "raw", "pdb_id": "1ABC"},
    )
    ligandset = ledger.add(
        "ligandset",
        path=root / "actives.csv",
        summary="已知活性 10053 个",
        meta={"target": "EGFR"},
    )
    artifact = ledger.add("artifact", summary="打分表（纯内存，还没写盘）")
    check(
        sorted(str(item.handle) for item in ledger.entries())
        == ["artifact#1", "ligandset#1", "receptor#1"],
        "登记表里有三个条目，各归各的序号",
    )

    listing = tools["list_workspace"]

    listed = await listing.execute({}, context=ledger)
    check(not listed.failed, f"列全表不失败：{listed.output[:80]}")
    check("当前受体：receptor#1" in listed.output, "开头报告当前受体是哪一份")
    for handle in ("receptor#1", "ligandset#1", "artifact#1"):
        check(handle in listed.output, f"列表里看得到 {handle}（模型从这里取 handle）")
    check(listed.meta.get("total") == 3, f"meta 里报了条数：{listed.meta}")

    # 只看一类。
    only_receptors = await listing.execute({"kind": "receptor"}, context=ledger)
    check(
        "receptor#1" in only_receptors.output and "ligandset#1" not in only_receptors.output,
        f"按类型筛：{only_receptors.output}",
    )
    wrong_kind = await listing.execute({"kind": "protein"}, context=ledger)
    check(
        wrong_kind.failed and "receptor" in wrong_kind.output and "artifact" in wrong_kind.output,
        f"类型写错时把三个合法取值说出来：{wrong_kind.output}",
    )

    # ---- 三、现核对磁盘：对不上的行尾标出来 ----
    changed = root / "actives.csv"
    changed.write_text("smiles\nCCO\n", encoding="utf-8")
    stale_entry = ledger.get(ligandset.handle)
    ledger.remove(ligandset.handle)
    stale_entry = ledger.add(
        "ligandset",
        path=changed,
        summary="已知活性 10053 个",
        fingerprint=fingerprint_path(changed),
        meta={"target": "EGFR"},
    )
    changed.write_text("smiles\nCCC\n", encoding="utf-8")  # 登记之后再改
    missing_pdb = root / "vanished" / "gone.pdb"
    ledger.add(
        "artifact",
        path=missing_pdb,
        summary="指向不存在的文件",
        fingerprint="deadbeef",
    )

    listed = await listing.execute({}, context=ledger)
    check("（文件在，但内容与登记时不同）" in listed.output, "内容被改的条目行尾标出来")
    check("（产物文件不在了）" in listed.output, "文件没了的条目行尾标出来")
    check(
        "项的产物文件与登记时对不上" in listed.output and "**不要**直接拿去用" in listed.output,
        f"单独再点一遍名（列表可能把出问题的那几条截掉）：{listed.output}",
    )
    check(
        listed.meta.get("broken") == 2,
        f"meta 里报了对不上的条数：{listed.meta}",
    )

    # ---- 四、describe_entry：路径补尾斜杠、血缘、meta 截断 ----
    describe = tools["describe_entry"]
    detail = await describe.execute({"handle": "receptor#1"}, context=ledger)
    check(not detail.failed, f"查详情不失败：{detail.output[:80]}")
    check("1ABC 原始结构" in detail.output, "摘要进详情")
    check(
        pdb.as_posix() in detail.output and f"{pdb.as_posix()}/" not in detail.output,
        f"路径照原样给出（单个文件不补尾杠）：{detail.output}",
    )
    check("文件状态：文件在，内容与登记时一致" in detail.output, "正常状态也明说（缺字段与正常长得一样）")
    check('"stage": "raw"' in detail.output, "meta 展开在附带字段里")

    handle_lost = await describe.execute({"handle": "ligandset#99"}, context=ledger)
    check(
        handle_lost.failed and "list_workspace" in handle_lost.output,
        f"handle 不存在时指向 list_workspace：{handle_lost.output[:100]}",
    )
    path_as_handle = await describe.execute({"handle": str(pdb)}, context=ledger)
    check(
        path_as_handle.failed and "不是合法的 handle" in path_as_handle.output,
        f"把路径当 handle 传时给格式说明：{path_as_handle.output[:100]}",
    )
    blank_handle = await describe.execute({"handle": "  "}, context=ledger)
    check(blank_handle.failed, "handle 只有空白也算缺参数")

    # 血缘：产物由配体集算出来，配体集由受体算出来。
    scored = ledger.add("artifact", parents=[stale_entry], summary="打分结果")
    chain = await describe.execute({"handle": str(scored.handle)}, context=ledger)
    check(
        "←" in chain.output and "ligandset" in chain.output and "receptor#1" in chain.output,
        f"完整血缘回溯到最远的祖先：{chain.output}",
    )
    no_lineage = await describe.execute(
        {"handle": str(scored.handle), "include_lineage": False}, context=ledger
    )
    check("完整血缘" not in no_lineage.output, "include_lineage=false 时不回溯")

    # meta 截断：一个配体集往 meta 里塞长清单也不会把上下文吃光。
    fat = ledger.add("artifact", summary="meta 很长的产物")
    fat.meta["smiles"] = "C" * 2000
    trimmed = await describe.execute({"handle": str(fat.handle)}, context=ledger)
    check("…（已截断）" in trimmed.output, "meta 超长时截断并说明")

    # ---- 五、switch_receptor：两阶段，预演 == 真做 ----
    second_dir = root / "prepared-2"
    second_dir.mkdir()
    (second_dir / "receptor.pdb").write_text("ATOM\n", encoding="utf-8")
    second = ledger.add(
        "receptor",
        path=second_dir,
        summary="2DEF 已准备",
        fingerprint=fingerprint_path(second_dir),
        meta={"stage": "prepared"},
    )
    switch = tools["switch_receptor"]

    # 预演那一次不许动任何东西。
    before_current = ledger.current_receptor()
    preview = await switch.execute({"receptor": str(second.handle)}, context=ledger)
    check(not preview.failed, f"预演不失败：{preview.output[:80]}")
    check("预演" in preview.output and "confirm=true" in preview.output, "默认只预演并说明怎么真做")
    check(
        ledger.current_receptor() == before_current,
        "预演不换当前受体（它只算影响范围）",
    )
    check(preview.meta.get("changed") is False, f"预演的 meta 里写着没改：{preview.meta}")

    preview_count = preview.meta.get("affected")
    done = await switch.execute({"receptor": str(second.handle), "confirm": True}, context=ledger)
    check(not done.failed, f"真做不失败：{done.output[:80]}")
    check(f"已切换到 {second.handle}" in done.output, "真做之后报告切到了哪一份")
    check(
        done.meta.get("affected") == preview_count,
        f"预演条数 == 真做条数：{preview_count} 对 {done.meta.get('affected')}",
    )
    check(
        ledger.current_receptor() == second.handle,
        "真做之后当前受体换成新的",
    )

    # 切换的后果：旧受体那一支被移出默认视图，但不删除。
    after = await listing.execute({}, context=ledger)
    check("receptor#1" not in after.output, "旧受体移出默认视图")
    check("receptor#2" in after.output, "新受体在默认视图里")
    with_archived = await listing.execute({"include_archived": True}, context=ledger)
    check(
        "receptor#1" in with_archived.output and "属于已切换掉的旧受体" in with_archived.output,
        "include_archived=true 时才看得到被搁置的条目",
    )
    check(
        str(receptor.handle) in [str(item.handle) for item in ledger.entries(include_archived=True)],
        "被搁置的条目还在表里（搁置不是删除）",
    )

    # 再切一次当前受体是空操作：不能因为重复点一次就把工作区清空。
    again = await switch.execute({"receptor": str(second.handle)}, context=ledger)
    check(
        "无需切换" in again.output and again.meta.get("changed") is False,
        f"切到当前受体自身是空操作：{again.output[:80]}",
    )

    # 拿一个配体集当受体传：说出「手上那个是什么」。
    live_ligandset = next(
        item for item in ledger.entries(include_archived=True) if item.handle.kind == "ligandset"
    )
    wrong_type = await switch.execute({"receptor": str(live_ligandset.handle)}, context=ledger)
    check(
        wrong_type.failed and "要的是受体" in wrong_type.output,
        f"类型拿错时给一句可读的中文：{wrong_type.output[:120]}",
    )
    blank = await switch.execute({"receptor": ""}, context=ledger)
    check(blank.failed, "受体参数为空也算缺参数")

    # ---- 六、装配：全部领域工具一次装完，名字不重 ----
    all_tools = all_domain_tools()
    names = [tool.spec.name for tool in all_tools]
    check(len(names) == len(set(names)), f"领域工具的名字不重复：{names}")
    check(
        names[:3] == ["list_workspace", "describe_entry", "switch_receptor"],
        f"浏览工具排在最前（模型的第一件事是看清手上有哪些条目）：{names[:3]}",
    )
    check(
        len(all_tools) == len(DOMAIN_TOOL_NAMES) and len(DOMAIN_TOOL_NAMES) >= 18,
        f"领域工具条数与 DOMAIN_TOOL_NAMES 一致、且不少于 18 个：{len(all_tools)} 个 {names}",
    )
    for expected in (
        "write_run",
        "open_viewer",
        "search_pdb",
        "search_literature",
        "import_ligandset",
        "prepare_ligands",
        "dock",
    ):
        check(expected in names, f"新工具 {expected} 装上了（写了却没装，界面上完全看不出来）")
    # 两个搜索工具是只读的：Plan 方式下必须放行（放进去就等于「查文献都做不到」）。
    from opencode_tui.core.agent.permission import PLAN_DENY

    for readonly in ("search_pdb", "search_literature", "open_viewer"):
        check(readonly not in PLAN_DENY, f"只读工具 {readonly} 不在 PLAN_DENY 里（Plan 下要放行）")
    ledger.reset()

    shutil.rmtree(root, ignore_errors=True)


async def check_memory() -> None:
    """项目记忆：跨会话往返、指纹对账、常驻注入、工具接口。"""
    root = PROBE / "memory_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    # 项目里有一个真实产物，记忆引用它。引用的必须是路径一类稳定标识，
    # 不能是会话内的 handle——那东西换个会话就指向别处。
    receptor = root / "receptor.pdb"
    receptor.write_text("ATOM      1  N   ALA A   1\n", encoding="utf-8")

    store = MemoryStore(root)
    check(store.root == memory_root(root), "记忆目录由 memory_root 定，挂在项目根下")
    check(store.root.name == MEMORY_DIR_NAME, f"目录名是 {MEMORY_DIR_NAME}")

    entry = store.remember("受体结构在 receptor.pdb", refs=["receptor.pdb"])
    check(entry.kind is MemoryKind.FACT, "本期写进去的都是 FACT")
    check(entry.id.startswith("mem_"), f"id 带时间前缀：{entry.id}")
    check(entry.fingerprint != "", "引用文件存在时记下了内容指纹")
    check(store.entries_path.is_file(), "条目文件已写出")
    check(store.memory_path.is_file(), "给人看的 memory.md 也写出来")
    check(
        "受体结构在 receptor.pdb" in store.memory_path.read_text(encoding="utf-8"),
        "memory.md 里有这条事实",
    )

    # 跨会话：换一个 store 实例读回来，等同于下一次开会话。
    later = MemoryStore(root)
    check(later.load(), "下一次打开能读回条目文件")
    check(len(later.entries) == 1, "读回来的条数对得上")
    check(
        next(iter(later.entries.values())).text == "受体结构在 receptor.pdb",
        "读回来的正文一致",
    )
    check(len(later.recall("receptor")) == 1, "按关键词能召回")
    check(later.recall("查无此词") == [], "关键词不命中时返回空")

    blank = None
    try:
        later.remember("   ")
    except MemoryError as error:
        blank = str(error)
    check(blank is not None, f"空正文被挡下：{blank}")

    # 引用不许是 handle：那是会话内的编号，换个会话就指向别的东西。
    handle_ref = None
    try:
        later.remember("受体是 receptor#1", refs=["receptor#1"])
    except MemoryError as error:
        handle_ref = str(error)
    check(
        handle_ref is not None and "handle" in handle_ref,
        f"handle 当引用被挡下：{handle_ref}",
    )
    check(len(later.entries) == 1, "被挡下的那条没有留在台账里")

    # 指纹对账：文件改了 → stale（仍可用）；文件删了 → missing（不可用）。
    receptor.write_text("ATOM      2  CA  ALA A   1\n", encoding="utf-8")
    changed = later.revalidate()
    check(len(changed) == 1, "引用文件变了，对账能发现")
    stale = next(iter(later.entries.values()))
    check(stale.state == "stale", f"状态是 stale：{stale.state}")
    check(stale.is_live(), "stale 仍算可用（变化是信号，不是作废）")
    check("已变更" in later.render_line(stale), "stale 的行上带一行提醒")
    check(later.recall("") != [], "stale 的条目仍然召得回来")

    receptor.unlink()
    later.revalidate()
    check(next(iter(later.entries.values())).state == "missing", "引用文件没了 → missing")
    check(not next(iter(later.entries.values())).is_live(), "missing 不再可用")
    check(later.recall("") == [], "missing 的条目召回时被跳过")
    check(later.resident_text() == "", "全部不可用时常驻注入为空")

    # 常驻注入：有引用的排前面；额度太小时宁可什么都不装，也不塞半条。
    receptor.write_text("ATOM      3  CB  ALA A   1\n", encoding="utf-8")
    fresh = MemoryStore(root)
    fresh.remember("受体来自 P00533", refs=["receptor.pdb"])
    fresh.remember("没有引用的一句旧话")
    text = fresh.resident_text()
    check("受体来自 P00533" in text, "有引用的事实进常驻正文")
    check(
        text.splitlines()[0].startswith("- 受体来自 P00533"),
        f"有引用的排在前面：{text.splitlines()[0]!r}",
    )
    check(fresh.resident_text(limit=1) == "", "额度只够一行的一小截时，宁可什么都不装")

    # 删一条。
    victim = next(iter(fresh.entries))
    check(fresh.forget(victim), "按 id 删得掉")
    check(victim not in fresh.entries, "删掉之后内存里也没了")
    check(not fresh.forget("mem_不存在"), "删不存在的 id 返回 False")

    # 工具：remember 写、recall 读。
    tool_root = PROBE / "memory_tools"
    shutil.rmtree(tool_root, ignore_errors=True)
    tool_root.mkdir(parents=True, exist_ok=True)
    tool_store = MemoryStore(tool_root)
    remember_tool = RememberTool(tool_store)
    recall_tool = RecallTool(tool_store)
    check(remember_tool.spec.name == "remember", "写记忆的工具名是 remember")
    check(recall_tool.spec.name == "recall", "读记忆的工具名是 recall")
    check(
        remember_tool.spec.parameters["required"] == ["text"],
        "remember 只强制 text，refs 可选",
    )
    written = await remember_tool.run({"text": "配体库在 ligands.sdf"})
    check("已记住" in written.output, f"写记忆返回一句确认：{written.output}")
    found = await recall_tool.run({"query": "配体"})
    check("ligands.sdf" in found.output, "召回工具把刚写的取回来")
    check("ligands.sdf" in (await recall_tool.run({})).output, "留空 query 返回全部")
    miss = await recall_tool.run({"query": "查无此物"})
    check(miss.output == "没有匹配的记忆。", f"没命中时给一句明确的答复：{miss.output}")
    bad = None
    try:
        await remember_tool.run({"text": ""})
    except ToolError as error:
        bad = str(error)
    check(bad is not None, f"工具层挡下空正文：{bad}")

    bad_ref = None
    try:
        await remember_tool.run({"text": "受体是 receptor#1", "refs": ["receptor#1"]})
    except ToolError as error:
        bad_ref = str(error)
    check(bad_ref is not None, f"工具层把 handle 引用转成一句可读失败：{bad_ref}")

    # 原子写：写完不留临时文件。
    leftovers = [item.name for item in tool_root.glob("*.tmp")]
    check(leftovers == [], f"写完不留临时文件：{leftovers}")

    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(tool_root, ignore_errors=True)


async def check_task_state() -> None:
    """任务状态：结构、更新语义、写盘往返、工具接口、装配时不丢。"""

    # 结构：空状态只渲染标题，五个字段各占一段，空的整段不出现。
    state = TaskState()
    check(state.is_empty(), "新建的任务状态是空的")
    check(state.version == 0, "新建的任务状态版本是 0")
    check(
        state.render() == "（本会话的任务状态，随进度更新）",
        f"空状态只渲染标题：{state.render()!r}",
    )

    check(state.apply(goal="对 EGFR 做一轮虚拟筛选"), "设目标算一次变更")
    check(state.version == 1, "变更之后版本加一")
    check("目标：对 EGFR 做一轮虚拟筛选" in state.render(), "目标渲染出来了")
    check("用户约束" not in state.render(), "还没约束时那一段不出现")

    check(state.apply(goal="对 EGFR 做一轮虚拟筛选") is False, "目标没变就不算变更")
    check(state.version == 1, "没变更就不涨版本")

    # 约束是**追加**：这一条是本模块存在的全部理由——模型每轮重写一遍，
    # 漏抄一条就等于把用户的要求删了。
    check(state.apply(add_constraints=["受体必须用 4.2 的晶体结构"]), "加一条约束")
    check(state.apply(add_constraints=["打分用 Vina，不要用 Vinardo"]), "再加一条约束")
    check(state.constraints == ["受体必须用 4.2 的晶体结构", "打分用 Vina，不要用 Vinardo"], "两条约束按先后顺序在")
    check(state.apply(add_constraints=["打分用 Vina，不要用 Vinardo"]) is False, "同一条约束不重复加")
    check(len(state.constraints) == 2, "重复的那条没进去")
    check(
        state.apply(add_constraints=["受体必须用 4.2 的晶体结构"]) is False,
        "再报一次旧约束也不会把它挪到后面",
    )
    check(state.constraints[0] == "受体必须用 4.2 的晶体结构", "约束保持原来的顺序")

    # done / todo / blockers 是**整段替换**：这三类是模型自己的进度记录。
    check(state.apply(todo=["生成配体库并粗筛", "跑一遍对接"]), "写待办")
    check(state.todo == ["生成配体库并粗筛", "跑一遍对接"], "待办整段写进去了")
    check(state.apply(todo=["跑一遍对接"]), "待办写短了也算变更")
    check(state.todo == ["跑一遍对接"], "替换是整段替换，不是追加")
    check(state.apply(done=[]) is False, "本来就空的这一类传空数组不算变更")
    check(state.apply(todo=[]) and state.todo == [], "传空数组清掉这一类")
    check("待办：" not in state.render(), "清掉之后那一段不再出现")

    # None 与空列表不是一回事：None 是不动，空列表是清空。
    version_before = state.version
    check(state.apply() is False, "一个字段都不给等于没改")
    check(state.version == version_before, "没给字段不涨版本")

    # 空白条目被跳过，不许拿空白把表撑起来。
    check(state.apply(goal="   ") is False, "空白目标不算变更")
    check(state.apply(add_constraints=["   ", "真约束"]), "一批里混着空白时，真的那条仍进去")
    check(state.constraints[-1] == "真约束", "空白那条没进约束表")
    check(state.apply(blockers=["卡在缺受体结构"]), "写阻塞")
    check(state.apply(done=["已导入受体"]), "写已完成")
    check(state.apply(todo=["生成配体库"]), "再把待办写回来")
    rendered = state.render()
    for label in ("目标：", "用户约束（每一步都生效）：", "已完成：", "待办：", "阻塞："):
        check(label in rendered, f"渲染里有「{label}」")

    # 命令那条路：打一个字段名加一句内容，往里加一条。
    fresh = TaskState()
    check(fresh.add("goal", "  目标一  "), "命令设目标")
    check(fresh.goal == "目标一", "首尾空白被去掉")
    check(fresh.add("constraint", "约束一"), "命令加约束")
    check(fresh.add("constraints", "约束二"), "复数写法也认")
    check(fresh.constraints == ["约束一", "约束二"], "两种写法进的是同一张表")
    check(fresh.add("blocker", "卡在没有受体"), "命令加阻塞")
    check(fresh.blockers == ["卡在没有受体"], "单数写法进 blockers")
    check(fresh.add("todo", "待办一") and fresh.add("todo", "待办二"), "命令加待办")
    check(fresh.todo == ["待办一", "待办二"], "命令这条路是追加，不重写整张表")

    unknown = None
    try:
        fresh.add("goalx", "x")
    except ValueError as error:
        unknown = str(error)
    check(unknown is not None and "goalx" in unknown, f"不认识的字段被挡下：{unknown}")
    blank = None
    try:
        fresh.add("goal", "   ")
    except ValueError as error:
        blank = str(error)
    check(blank is not None, f"空内容被挡下：{blank}")

    check(fresh.clear(), "清空返回 True")
    check(fresh.is_empty(), "清空之后五个字段都空了")
    check(fresh.clear() is False, "已经空的再清一次返回 False")

    # 序列化：进 TOML 的是字典，老状态文件里没有这一段也要能读。
    payload = state.to_dict()
    check(payload["version"] == state.version, "版本进字典")
    check(TaskState.from_dict(payload).to_dict() == payload, "字典往返一致")
    check(TaskState.from_dict({}).is_empty(), "空字典还原成空状态")
    check(TaskState.from_dict({"constraints": None}).constraints == [], "字段是 None 时按空处理")
    check(TaskState.from_dict({"todo": "不是数组"}).todo == [], "字段类型不对时按空处理")
    check(TaskState.from_dict({"constraints": ["  ", "真"]}).constraints == ["真"], "读回来的表也清掉空白条目")
    check(TaskState.from_dict({"version": "3"}).version == 3, "版本从字符串也认")

    session = Session(title="任务状态往返")
    session.task_state = state
    restored = Session.from_dict(session.to_dict())
    check(restored.task_state.to_dict() == state.to_dict(), "会话往返带着任务状态")
    legacy = Session.from_dict({"id": "ses_9999", "title": "老文件"})
    check(legacy.task_state.is_empty(), "老状态文件里没有这一段也不炸")

    # 写盘：走的是状态文件那条原子写，与摘要在同一份文件里。
    root = PROBE / "task_state_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "state.toml"
    manager = SessionManager(path)
    current = manager.current
    current.task_state.apply(
        goal="对 EGFR 做一轮虚拟筛选",
        add_constraints=["受体必须用 4.2 的晶体结构"],
    )
    current.task_state.apply(todo=["生成配体库并粗筛"])
    manager.save_to_toml()
    check(path.is_file(), "状态文件写出来了")
    check("task_state" in path.read_text(encoding="utf-8"), "状态文件里有任务状态这一段")
    reloaded = SessionManager(path)
    check(reloaded.load_from_toml(), "状态文件读得回来")
    back = reloaded.current.task_state
    check(back.to_dict() == current.task_state.to_dict(), "写盘往返一致")
    check(back.goal == "对 EGFR 做一轮虚拟筛选", "目标读回来了")
    check(back.constraints == ["受体必须用 4.2 的晶体结构"], "约束读回来了")
    check(back.todo == ["生成配体库并粗筛"], "待办读回来了")
    leftovers = [item.name for item in root.glob("*.tmp")]
    check(leftovers == [], f"写完不留临时文件：{leftovers}")

    # 工具：模型那条路。
    tool = TaskTool()
    check(tool.spec.name == "task", "工具名是 task")
    check(tool.task_argument == "state", "声明了收任务状态的参数名")
    check(
        tool.task_argument not in tool.spec.parameters.get("properties", {}),
        "那个名字不出现在参数模式里（模型看不到它）",
    )
    check(tool.context_argument == "", "它不声明 context_argument——它不碰登记表")

    target = TaskState()
    written = await tool.execute(
        {"goal": "目标一", "add_constraints": ["约束一"]}, task_state=target
    )
    check(not written.failed and "已更新" in written.output, f"改完给一句确认：{written.output[:40]}")
    check(target.goal == "目标一" and target.constraints == ["约束一"], "工具改的是交进来的那个对象")
    check(written.meta.get("version") == target.version, "meta 里的版本与对象一致")
    check(written.meta.get("constraints") == 1, "meta 里有约束条数")

    same = await tool.execute({"goal": "目标一"}, task_state=target)
    check(not same.failed and "没有变化" in same.output, f"没变化时明说：{same.output}")
    check(target.version == written.meta["version"], "没变化时不涨版本")

    unassigned = await tool.execute({"goal": "目标一"})
    check(
        unassigned.failed and "内核装配问题" in unassigned.output,
        f"该注入却没注入时报装配问题：{unassigned.output[:40]}",
    )

    # 参数名必须与模型看得到的那一处一致。说明文字、渲染出来的标题、`/task` 的别名表
    # 用的都是 `constraints`，而参数模式原来是 `add_constraints`——模型照着说明写，
    # 被 `additionalProperties: False` 挡下，白烧一步（现场 4 个会话各撞过一次）。
    properties = tool.spec.parameters["properties"]
    check("constraints" in properties, "参数模式里有 constraints（模型看得到的就是这个名字）")
    check("add_constraints" in properties, "旧名字也留着，老会话历史里的调用照旧认")

    by_new_name = TaskState()
    added = await tool.execute({"constraints": ["约束甲", "约束乙"]}, task_state=by_new_name)
    check(not added.failed, f"用 constraints 追加不再被挡：{added.output[:60]}")
    check(
        by_new_name.constraints == ["约束甲", "约束乙"],
        f"constraints 收进了状态：{by_new_name.constraints}",
    )

    by_old_name = TaskState()
    legacy = await tool.execute({"add_constraints": ["旧写法"]}, task_state=by_old_name)
    check(not legacy.failed and by_old_name.constraints == ["旧写法"], "旧名字 add_constraints 仍然生效")

    both = TaskState()
    merged = await tool.execute(
        {"constraints": ["新写法"], "add_constraints": ["旧写法"]}, task_state=both
    )
    check(
        not merged.failed and both.constraints == ["新写法", "旧写法"],
        f"两个名字都给就都追加：{both.constraints}",
    )

    # 参数模式这一层也要放行新名字（内核在 execute 之前先过它）。
    check(
        argument_error(tool.spec, {"constraints": ["一条"]}) == "",
        "constraints 能过参数模式这一道",
    )
    check(
        argument_error(tool.spec, {"add_constraints": ["一条"]}) == "",
        "add_constraints 也仍能过参数模式这一道",
    )
    check(
        "不在允许的范围内" in argument_error(tool.spec, {"constraint": ["一条"]}),
        "真写错的名字（单数 constraint）照旧被挡下",
    )

    for label, arguments in (("一个字段都没给", {}), ("todo 不是数组", {"todo": "不是数组"})):
        rejected = None
        try:
            await tool.execute(arguments, task_state=TaskState())
        except ToolError as error:
            rejected = str(error)
        check(rejected is not None, f"{label} 被挡下：{rejected}")

    # 循环层取给模型看的那一段：空状态给空串，不塞一个只有标题的空段。
    empty_session = Session(title="空任务状态")
    check(AgentLoop._task_text(empty_session) == "", "没有任务状态时 TASK 层给空串")
    filled_session = Session(title="有任务状态")
    filled_session.task_state = state
    check(AgentLoop._task_text(filled_session) == state.render(), "有任务状态时给的是渲染结果")
    check(AgentLoop._task_text(filled_session).endswith(rendered.splitlines()[-1]), "渲染结果原样交出去")

    # 不变式 2：任务状态不丢。窗口一路收小，逼装配器往外挤交互组，
    # 用户提的约束必须一条不少——这是 TASK 层存在的全部理由。
    long_session = Session(title="长会话")
    long_session.task_state.apply(
        goal="对 EGFR 做一轮虚拟筛选",
        add_constraints=["受体必须用 4.2 的晶体结构", "打分用 Vina，不要用 Vinardo"],
    )
    long_session.task_state.apply(todo=["生成配体库并粗筛"])
    for index in range(30):
        long_session.add_message(
            Message(role="user", parts=[Part(type=PartType.TEXT, text=f"第 {index} 问：" + "啰嗦" * 120)])
        )
        long_session.add_message(
            Message(role="assistant", parts=[Part(type=PartType.TEXT, text=f"第 {index} 答：" + "内容" * 120)])
        )
    task_text = AgentLoop._task_text(long_session)
    expected = list(long_session.task_state.constraints)
    squeezed = 0
    for window in (200_000, 20_000, 8_000, 4_000):
        built = ContextAssembler(window).build(long_session, "稳定前缀" * 10, task_text=task_text)
        header = built.messages[1]["content"]
        for item in expected:
            check(
                f"- {item}" in header,
                f"窗口 {window} 下约束「{item}」还在（移出 {built.dropped} 组、降级 {built.degraded}）",
            )
        check(built.usage[Layer.TASK] > 0, f"窗口 {window} 下 TASK 层有用量")
        check("task" not in built.degraded, f"窗口 {window} 下 TASK 层没被降级削过")
        squeezed = max(squeezed, built.dropped)
    check(squeezed > 0, "至少有一个窗口真的挤出东西了，否则上面那几条是空转的")

    blank_session = Session(title="没有任务状态")
    for index in range(5):
        blank_session.add_message(
            Message(role="user", parts=[Part(type=PartType.TEXT, text=f"第 {index} 问")])
        )
    blank_built = ContextAssembler(200_000).build(
        blank_session, "前缀", task_text=AgentLoop._task_text(blank_session)
    )
    joined = "\n".join(str(item.get("content") or "") for item in blank_built.messages)
    check("本会话的任务状态" not in joined, "没有任务状态时不会凭空多出一段")

    shutil.rmtree(root, ignore_errors=True)


async def check_selective_compression() -> None:
    """选择性压缩：按内容挑着丢，报错与结论逐字留下；降级顺序与软配额可观测。

    着眼点全是「压缩之后什么还在」，不是「压短了多少」——按字符位置截也能压短，
    那正是这一期要换掉的做法。
    """
    mid_error = "Error: 第 150 个配体打分中途失败"
    conclusion = "结论：只得到 3 个候选"
    progress = "过程行 {} 正在处理第 {} 个配体，参数用 --exhaustiveness 8 处理中"

    def long_output(rows: int) -> str:
        """超长工具输出：中段藏一条报错，末尾一条结论，其余都是过程行。"""
        lines = [progress.format(index, index) for index in range(rows)]
        lines[rows // 2] = mid_error
        lines.append(conclusion)
        return "\n".join(lines)

    # ---- 一、关键行判定 ----
    for line in (
        "Error: 打分失败",
        "Traceback (most recent call last):",
        '  File "run.py", line 3, in <module>',
        "错误：找不到受体",
        "ValueError: 参数不合法",
        "caused by: 上游超时",
        "  - Error: 带项目符号的报错",
    ):
        check(is_key_line(line), f"报错行被认成关键行：{line}")
    for line in ("结论：命中 12 个", "结果：只得到 3 个", "score: -7.3", "总计 40 个配体", "RMSD 1.2"):
        check(is_key_line(line), f"结论行被认成关键行：{line}")
    for line in (progress.format(12, 12), "过程行 7 正在处理中", "Reading input file...", "  第 3 行内容"):
        check(not is_key_line(line), f"过程行没被认成关键行：{line}")

    # ---- 二、翻译层：报错在中段也不被切掉 ----
    #
    # 这是本期最实际的收益：旧写法按字符留首尾，报错正好出现在被削掉的那一段里，
    # 装配器连见都没见到它，后面再怎么按内容挑也救不回来。
    text = long_output(300)
    fed = truncate_for_model(text)
    check(len(text) > MODEL_OUTPUT_LIMIT, f"造的样例确实超限：{len(text)}")
    check(len(fed) <= MODEL_OUTPUT_LIMIT, f"给模型的那一份没超限：{len(fed)}")
    check(mid_error in fed, "中段的报错原文保住了")
    check(conclusion in fed, "结论行保住了")
    check(progress.format(50, 50) not in fed, "成片的过程行被丢掉了")
    check(truncate_for_model("短输出") == "短输出", "不超限的输出原样返回")

    # 整段只有一行时没有行结构可挑，退回按位置留首尾——不能因为换了压缩就把信息丢掉。
    single = "甲" * 3000 + "乙" * 2000 + "丙" * 3000
    cut = truncate_for_model(single)
    check(cut.startswith("甲" * 2000) and cut.endswith("丙" * 2000), "没有行结构时按位置留首尾")

    # ---- 三、两档压缩 ----
    mixed = "\n".join(
        [f"过程行 {index} 正在处理中" for index in range(30)]
        + [f"Error: 第 {index} 条记录不合法" for index in range(3)]
        + [conclusion]
        + [f"过程行 尾 {index} 正在处理中" for index in range(30)]
    )
    first = compress_tool_output(mixed)
    check(first is not None, "非深档该压的时候压了")
    assert first is not None
    check(len(first) < len(mixed), f"非深档确实压短了：{len(mixed)} -> {len(first)}")
    check("过程行 0 正在处理中" in first, "非深档把开头补了回来")
    check("过程行 尾 29 正在处理中" in first, "非深档把结尾补了回来")
    check("过程行 15 正在处理中" not in first, "非深档丢掉了成片的过程行")
    for index in range(3):
        check(f"Error: 第 {index} 条记录不合法" in first, f"非深档保住了第 {index} 条报错")
    check(conclusion in first, "非深档保住了结论行")
    check(OMISSION_MARK in first, f"压过的地方写明了省略：{OMISSION_MARK}")
    check(compress_tool_output(first) is None, "非深档压过之后不再压（同一档不空转）")

    # 中间整段都是关键行时非深档压不动（首尾加上去正好等于原文），只有深档能压下来。
    allkey = "\n".join(
        ["过程行 甲", "过程行 乙"]
        + [f"Error: 第 {index} 条记录不合法" for index in range(20)]
        + ["过程行 丙", "过程行 丁"]
    )
    check(compress_tool_output(allkey) is None, "整段关键行时非深档不动它")
    deeper = compress_tool_output(allkey, deep=True)
    check(deeper is not None, "深档把这一档压下来了")
    assert deeper is not None
    check("过程行 甲" not in deeper and "过程行 丁" not in deeper, "深档连首尾也不要了")
    check(
        all(f"Error: 第 {index} 条记录不合法" in deeper for index in range(20)),
        "深档一条报错都没丢",
    )
    check(DEEP_MARK in deeper, f"深档有自己的标记：{DEEP_MARK}")
    check(compress_tool_output(deeper, deep=True) is None, "深档压过之后不再压")
    check(compress_tool_output(deeper) is None, "深档压过之后非深档也不会再动它")
    check(compress_tool_output("很短") is None, "短到不值一压的正文不动它")

    # ---- 四、装配器：只动推理与工具输出正文 ----
    #
    # 助手正文是决策与结论、tool_calls 是调用记录，两者都不在可压的两类里。
    # 判据不是「压缩函数没碰它们」，而是**装配结果里逐字还在**。
    session = Session(title="选择性压缩")
    session.add_message(
        Message(role="user", parts=[Part(type=PartType.TEXT, text="跑一轮打分")])
    )
    assistant = Message(role="assistant")
    assistant.add_part(Part(type=PartType.STEP_START, text="1 · fake"))
    assistant.add_part(Part(type=PartType.REASONING, text="先定参数再跑。" * 6))
    assistant.add_part(
        Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"command": "vina --config conf.txt"},
            tool_output=long_output(85),
        )
    )
    assistant.add_part(Part(type=PartType.TEXT, text=conclusion))
    session.add_message(assistant)

    # 这一段必须**压在翻译层上限之下**：超了就先被翻译层压掉，装配器看到的
    # 已经是一小段，再也压不出东西，下面那几条断言会变成空转。
    check(
        len(long_output(85)) <= MODEL_OUTPUT_LIMIT,
        f"装配那一段的样例压在翻译层上限之下：{len(long_output(85))}",
    )
    source = [item for message in session.messages for item in translate_message(message)]
    source_texts = [
        item["content"]
        for item in source
        if item.get("role") == "assistant" and item.get("content")
    ]
    source_calls = [call["id"] for item in source for call in item.get("tool_calls") or []]
    check(len(source_calls) == 1, f"样例里有一次工具调用：{source_calls}")

    prefix = "系统提示。" * 20
    levels: set[str] = set()
    squeezed = 0
    seen_texts: set[str] = set()
    seen_calls: set[str] = set()
    for window in range(2_500, 30_000, 250):
        try:
            built = ContextAssembler(window).build(session, prefix)
        except ContextAssemblyError:
            continue
        levels.update(built.degraded)
        if "recent_output" in built.degraded or "recent_output_body" in built.degraded:
            squeezed += 1
        body = "\n".join(
            str(item.get(key) or "")
            for item in built.messages
            for key in ("content", "reasoning_content")
        )
        seen_texts.update(item for item in source_texts if item in body)
        for item in built.messages:
            for call in item.get("tool_calls") or []:
                seen_calls.add(call["id"])
        check(
            built.degraded == ordered_degraded(built.degraded),
            f"窗口 {window} 下降级级别按降级链的顺序排：{built.degraded}",
        )
        check(
            all(level in DEGRADE_ORDER for level in built.degraded),
            f"窗口 {window} 下的级别名都在降级链表里：{built.degraded}",
        )
        if "recent_reasoning" in built.degraded and "recent_output" in built.degraded:
            check(
                built.degraded.index("recent_reasoning") < built.degraded.index("recent_output"),
                f"窗口 {window} 下先丢推理、后压输出：{built.degraded}",
            )
    check("recent_reasoning" in levels, f"量到了「丢中间推理」这一级：{sorted(levels)}")
    check("recent_output" in levels, f"量到了「压工具输出」这一级：{sorted(levels)}")
    check(squeezed > 0, "至少有一个窗口真的压过工具输出，否则上面那几条是空转的")
    check(
        seen_texts == set(source_texts),
        f"助手正文一字不动：{sorted(set(source_texts) - seen_texts)}",
    )
    check(
        seen_calls == set(source_calls),
        f"调用记录一条不少：{sorted(set(source_calls) - seen_calls)}",
    )

    # ---- 五、软配额真的用上了 ----
    budget = ContextAssembler(200_000).budget()
    check(budget.available == ContextAssembler(200_000).available(), "available 与 budget 是同一个口径")
    check(
        budget.share(Layer.EVIDENCE, 10_000) == int(10_000 * EVIDENCE_SHARE),
        "证据层按份额拿额度",
    )
    check(
        budget.share(Layer.HISTORY, 10_000) == int(10_000 * HISTORY_SHARE),
        "历史层按份额拿额度",
    )
    check(budget.share(Layer.PREFIX, 10_000) == 0, "前缀不参与份额分配")

    evidence = [Evidence("receptor#1", "一行摘要", "正文" * 4000)]
    capped = ContextAssembler(12_000).build(session, prefix, evidence=evidence)
    capped_body = "\n".join(str(item.get("content") or "") for item in capped.messages)
    check(
        "evidence_detail" in capped.degraded,
        f"证据正文被软配额削过，也记进 degraded：{capped.degraded}",
    )
    check("receptor#1" in capped_body, "证据摘要还在（正文收了，索引不能一起收）")
    check("一行摘要" in capped_body, "证据摘要那一行也还在")

    # ---- 六、装配失败：消息里带各层用量 ----
    huge = Session(title="装不下")
    huge.add_message(
        Message(role="user", parts=[Part(type=PartType.TEXT, text="甲" * 200_000)])
    )
    try:
        ContextAssembler(8_000).build(huge, "前缀")
    except ContextAssemblyError as error:
        check("各层用量" in str(error), f"装配失败的消息里带各层用量：{str(error)[:60]}")
        check(bool(error.usage), "装配失败带上 usage")
        check(
            error.reason and "各层用量" not in error.reason,
            "reason 存的是没加用量那一段原文，另加的一行不算进去",
        )
    else:
        check(False, "窗口装不下时没有报错")

    # ---- 七、降级可观测：削过就发一条事件，没削就不发 ----
    bus = EventBus()
    published: list[dict[str, object]] = []
    bus.subscribe("context.assembled", lambda data: published.append(dict(data)))

    class Publisher:
        """`_trace_context` 只用得上 `bus`。为验一条事件去装一整个循环层不划算。"""

        def __init__(self, bus: EventBus) -> None:
            self.bus = bus

    clean = ContextAssembler(200_000).build(session, prefix)
    check(not clean.degraded and not clean.dropped, "宽窗口下这一份什么都没削")
    AgentLoop._trace_context(Publisher(bus), session, clean)  # type: ignore[arg-type]
    check(published == [], f"没削过就不发事件：{published}")

    tight = ContextAssembler(3_000).build(session, prefix)
    AgentLoop._trace_context(Publisher(bus), session, tight)  # type: ignore[arg-type]
    check(len(published) == 1, f"削过就发一条：{len(published)}")
    payload = published[0]
    check(payload["degraded"] == tight.degraded, "事件里的降级级别与装配结果一致")
    check(payload["dropped"] == tight.dropped, "事件里的移出组数一致")
    check(payload["dropped_upto"] == tight.dropped_upto, "事件里的覆盖位置一致")
    check(payload["session_id"] == session.id, "事件带上会话编号")
    check(
        set(payload["usage"]) == {layer.value for layer in tight.usage},  # type: ignore[arg-type]
        "事件里的各层用量齐全",
    )


async def check_evidence_layer() -> None:
    """证据层：收什么、留什么、发得出去。

    本期的工作量全在**接线**上——收集器与装配器在第 3 期就写好了，
    缺的是「工具跑完往里放」与「装配时带上」这两处。
    所以最后两节走的是真的循环层，不是把零件单独拎出来量。
    """
    mixed = "\n".join(
        [
            "过程行 1 正在处理配体",
            "过程行 2 正在处理配体",
            "Error: 第 150 个配体打分中途失败",
            "过程行 3 正在处理配体",
            "结论：只得到 3 个候选",
        ]
    )
    plain = "\n".join(
        f"过程行 {index} 正在处理配体，参数用 --exhaustiveness 8" for index in range(6)
    )

    def tool_part(name: str, output: str, status: ToolStatus = ToolStatus.COMPLETED) -> Part:
        return Part(
            type=PartType.TOOL,
            tool_name=name,
            tool_status=status,
            tool_input={"path": "result.txt"},
            tool_output=output,
        )

    # ---- 一、收集器：替换、滚动、清空 ----
    store = EvidenceStore(limit=3)
    check(EvidenceStore().limit == EVIDENCE_LIMIT, "默认条数上限取自模块常量")
    store.add("bash vina", "第一行摘要", "正文一")
    store.add("read a.txt", "第二行摘要")
    check(
        [item.ref for item in store.items()] == ["bash vina", "read a.txt"],
        "按收进来的先后排列",
    )

    store.add("bash vina", "换了内容的摘要")
    check(len(store) == 2, "同一条引用是替换而不是追加")
    check(store.items()[-1].ref == "bash vina", "替换之后挪到末尾（最近用到的排后面）")
    check(store.items()[-1].summary == "换了内容的摘要", "替换成新的摘要")

    store.add("   ", "空引用")
    store.add("read b.txt", "   ")
    check(len(store) == 2, "空引用与空摘要都不占位置")

    store.add("read c.txt", "第三条")
    check(len(store) == 3, "正好等于上限时不挤掉谁")
    store.add("read d.txt", "第四条")
    check(len(store) == 3, "超过上限之后条数不再涨")
    check("read a.txt" not in [item.ref for item in store.items()], "满了挤掉最旧的那条")

    store.clear()
    check(len(store) == 0 and store.items() == [], "清空之后一条不剩")

    # 摘要与引用名都压成一行：夹进来一个换行会把那条记录切成两半。
    check(one_line("甲\n乙\t丙", 20) == "甲 乙 丙", "多行压成一行")
    check(len(one_line("甲" * 30, 10)) == 10, "超长截到上限")
    check(one_line("甲" * 30, 10).endswith("…"), "截断留个尾巴，一眼看出被截过")

    # ---- 二、引用名：同一条材料要能认出是同一条 ----
    check(
        evidence_ref("bash", {"command": "vina --config c.txt"}).startswith("bash "),
        "引用名带工具名",
    )
    check(
        evidence_ref("read", {"path": "a.txt", "file": "b.txt"}) == "read a.txt",
        "定位参数按固定顺序取第一个",
    )
    check(evidence_ref("list_workspace", {}) == "list_workspace", "没有定位参数时只留工具名")
    check(evidence_ref("bash", None) == "bash", "输入不是字典时只留工具名")
    check(len(evidence_ref("bash", {"command": "甲" * 200})) <= 80, "引用名有长度上限")

    # ---- 三、提炼口径：只收含关键行的 ----
    mixed_store = EvidenceStore()
    mixed_store.add_from_part(tool_part("bash", mixed))
    check(len(mixed_store) == 1, "含关键行的输出进证据层")
    item = mixed_store.items()[0]
    check(item.summary == "Error: 第 150 个配体打分中途失败", f"摘要取第一条关键行：{item.summary}")
    check("结论：只得到 3 个候选" in item.detail, "结论行进正文")
    check(item.summary not in item.detail, "正文里不重复摘要那一行")
    check("过程行 1" not in item.detail and "过程行 3" not in item.detail, "过程行不进证据层")

    plain_store = EvidenceStore()
    plain_store.add_from_part(tool_part("bash", plain))
    check(
        len(plain_store) == 0,
        "一行关键行都没有的输出不收——收了证据层就是 RECENT 的复制品",
    )

    failed_store = EvidenceStore()
    failed_store.add_from_part(tool_part("script", "缺少参数 text", ToolStatus.FAILED))
    check(len(failed_store) == 1, "失败的那一类保底收：失败原因就是下一步的输入")
    check(failed_store.items()[0].summary == "缺少参数 text", "保底收的摘要取输出第一行")

    for status in (ToolStatus.PENDING, ToolStatus.RUNNING, ToolStatus.AWAITING):
        pending = EvidenceStore()
        pending.add_from_part(tool_part("bash", mixed, status))
        check(len(pending) == 0, f"状态是 {status.value} 时结果还不存在，不该收")

    blank = EvidenceStore()
    blank.add_from_part(tool_part("bash", ""))
    blank.add_from_part(Part(type=PartType.TEXT, text=mixed))
    check(len(blank) == 0, "没有输出的、以及不是工具结果的，都不收")

    # ---- 四、检索回来的记忆进证据层 ----
    #
    # 记忆是跨会话的知识，RECENT 里本来就没有它。不给证据层一份，
    # 下一步的 RECENT 一被压缩，这段知识就跟着没了。
    root = PROBE / "evidence_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "receptor.pdb").write_text("ATOM      1  N   ALA A   1\n", encoding="utf-8")
    mem = MemoryStore(root)
    mem.remember("受体结构在 receptor.pdb", refs=["receptor.pdb"])
    mem.remember("配体库在 ligands.sdf", refs=["ligands.sdf"])
    recall = RecallTool(mem)
    check(recall.evidence_argument == "evidence", "recall 声明了收证据收集器的参数名")
    check(
        recall.evidence_argument not in recall.spec.parameters.get("properties", {}),
        "那个名字不出现在参数模式里（模型看不到它）",
    )
    check(recall.context_argument == "", "它仍然不声明 context_argument——纯查询工具不挂登记表")

    collector = EvidenceStore()
    found = await recall.run({"query": "受体", "evidence": collector})
    check("receptor.pdb" in found.output, "检索本身照常返回")
    check(len(collector) == 1, f"命中的记忆进证据层：{len(collector)}")
    entry = collector.items()[0]
    check(entry.ref.startswith("记忆 mem_"), f"记忆的引用名带它的编号：{entry.ref}")
    check(entry.summary == "受体结构在 receptor.pdb", "摘要就是记忆正文")
    check("receptor.pdb" in entry.detail, "正文放引用文件——那才是能取回东西的凭据")

    all_hits = EvidenceStore()
    await recall.run({"evidence": all_hits})
    check(len(all_hits) == 2, "留空 query 时全部记忆都进证据层")

    none_hit = EvidenceStore()
    missed = await recall.run({"query": "查无此物", "evidence": none_hit})
    check(
        missed.output == "没有匹配的记忆。" and len(none_hit) == 0,
        "没命中时不往证据层放东西",
    )
    check(
        "receptor.pdb" in (await recall.run({"query": "受体"})).output,
        "没有收集器时（工具单独被调）检索照常工作",
    )

    # ---- 五、注入通道本身 ----
    class NeedsEvidence(Tool):
        """只声明收证据收集器，用来验内核的注入与漏传守卫。"""

        spec = ToolSpec(
            name="needs_evidence",
            description="验注入通道",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )
        evidence_argument = "evidence"

        async def run(self, arguments: dict[str, object]) -> ToolResult:
            return ToolResult(output=f"收到={arguments.get('evidence') is not None}")

    ok = await NeedsEvidence().execute({}, evidence=EvidenceStore())
    check(ok.output == "收到=True", f"内核把收集器塞进参数里：{ok.output}")
    missing = await NeedsEvidence().execute({})
    check(missing.failed, "漏传收集器时报装配问题而不是参数错")
    check("内核装配问题" in missing.output, f"报的是装配问题：{missing.output}")

    # ---- 六、接线：工具跑完自动提炼、装配时自动带上 ----
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(PresetTool(mixed))
    config = ProviderConfig(base_url="http://127.0.0.1:1", api_key="x", model="fake")
    loop = AgentLoop(
        bus,
        SilentProvider(),  # type: ignore[arg-type]
        registry,
        config,
        context_window=200_000,
        workspace_root=root,
        policy=PermissionPolicy(default_ask=frozenset()),
    )
    check(len(loop.evidence) == 0, "新装配的循环层证据层是空的")

    session = Session(title="证据接线")
    session.add_message(
        Message(role="user", parts=[Part(type=PartType.TEXT, text="跑一条脚本")])
    )
    answer = Message(role="assistant", session_id=session.id)
    session.add_message(answer)
    call = Part(
        type=PartType.TOOL,
        tool_name="preset",
        tool_status=ToolStatus.PENDING,
        tool_input={"path": "result.txt"},
    )
    answer.add_part(call)
    await loop._execute_tool(session, answer, call)
    check(call.tool_status is ToolStatus.COMPLETED, f"工具正常跑完：{call.tool_status}")
    check(len(loop.evidence) == 1, f"工具跑完自动提炼出一条证据：{len(loop.evidence)}")

    built = loop._assemble(session, "系统提示。")
    body = "\n".join(str(item.get("content") or "") for item in built.messages)
    check("preset result.txt" in body, f"装配时自动带上证据层：{body[-160:]}")
    check(built.usage[Layer.EVIDENCE] > 0, "证据层的用量记上了")
    check("Error: 第 150 个配体打分中途失败" in body, "摘要出现在发给模型的消息里")

    # 纯过程行的工具结果不收，装配里也不冒出证据块。
    plain_registry = ToolRegistry()
    plain_registry.register(PresetTool(plain))
    plain_loop = AgentLoop(
        bus,
        SilentProvider(),  # type: ignore[arg-type]
        plain_registry,
        config,
        context_window=200_000,
        workspace_root=root,
        policy=PermissionPolicy(default_ask=frozenset()),
    )
    plain_answer = Message(role="assistant", session_id=session.id)
    plain_call = Part(
        type=PartType.TOOL,
        tool_name="preset",
        tool_status=ToolStatus.PENDING,
        tool_input={"path": "plain.txt"},
    )
    plain_answer.add_part(plain_call)
    await plain_loop._execute_tool(session, plain_answer, plain_call)
    check(len(plain_loop.evidence) == 0, "纯过程行的工具结果不进证据层")
    plain_body = "\n".join(
        str(item.get("content") or "")
        for item in plain_loop._assemble(session, "系统提示。").messages
    )
    check("本步相关的材料" not in plain_body, "没有证据时装配结果里不出现证据块")

    # ---- 七、一轮开始时清空 ----
    #
    # 上一轮 recall 出来的记忆，到这一轮多半已经不相干，留着只会占证据层的额度。
    loop.evidence.clear()
    loop.evidence.add("bash 上一轮", "上一轮留下的证据")
    check(len(loop.evidence) == 1, "先往证据层塞一条")
    await loop.stream_reply(session, "")
    check(len(loop.evidence) == 0, "新的一轮开始时证据被清空")

    shutil.rmtree(root, ignore_errors=True)


def check_plan_deny() -> None:
    """Plan 方式下的工具准入：改动的挡住，只读与只取的放行。"""
    registry = ToolRegistry()
    registry.register(WriteTool(cwd=WORKSPACE_FIXTURE))
    registry.register(ReadTool(cwd=WORKSPACE_FIXTURE))
    registry.register(BashTool(cwd=WORKSPACE_FIXTURE))
    for tool in all_domain_tools():
        registry.register(tool)

    policy = PermissionPolicy()

    def judge(name: str, arguments: dict[str, object], agent_name: str = "Plan") -> object:
        return policy.check(registry.get(name), arguments, agent_name)  # type: ignore[arg-type]

    # 会往磁盘写一组产物的那一个：Plan 下拒绝。
    decision = judge("prepare_receptor", {"structure": "receptor#1"})
    check(
        decision.verdict is Verdict.DENY and "Plan" in decision.reason,
        f"Plan 方式下 prepare_receptor 被拒：{decision.reason}",
    )
    check(
        "prepare_receptor" in PLAN_DENY,
        "它确实在计划拒绝那一档里（不是碰巧被别的规则挡住的）",
    )

    # 只读 / 只取的照常放行——挡掉它们会让 Plan 方式连查资料都做不到。
    for name, arguments in (
        ("list_workspace", {}),
        ("describe_entry", {"handle": "receptor#1"}),
        ("search_targets", {"query": "EGFR"}),
        ("fetch_target", {"uniprot_id": "P00533"}),
        ("fetch_structure", {"pdb_id": "1M17"}),
        ("import_structure", {"path": "model.pdb"}),
        ("search_actives", {"uniprot_id": "P00533"}),
        ("find_analogs", {"smiles": "CCO"}),
        ("describe_molecule", {"chembl_id": "CHEMBL25"}),
    ):
        decision = judge(name, arguments)
        check(
            decision.verdict is Verdict.ALLOW,
            f"Plan 方式下 {name} 仍放行（它是只读或只取）：{decision.verdict} {decision.reason}",
        )

    # Build 方式下 prepare_receptor 不额外拦（它本来要走默认判定）。
    build = judge("prepare_receptor", {"structure": "receptor#1"}, agent_name="Build")
    check(
        build.verdict is not Verdict.DENY,
        f"Build 方式下 prepare_receptor 不因方式被拒：{build.verdict}",
    )

    # switch_receptor 会改变后续工具的默认作用对象，但不写文件也不删东西，
    # 两阶段确认本身就把这一步交给了用户，所以不进 Plan 那一档。
    check(
        "switch_receptor" not in PLAN_DENY,
        "switch_receptor 不进计划拒绝那一档（它不写文件，两阶段确认已经问过用户）",
    )


# 检查组：名字给 `--only` 过滤用，函数一律零参数（同步与异步混着，调用处看返回值）。
# 名字取函数名里有意义的那一段，`--only permission` 能直接命中。
GROUPS: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("types", check_agent_types),
    ("arguments", check_arguments),
    ("connection", check_connection_file),
    ("trace", check_trace),
    ("rules", check_rules),
    ("permission", check_permission),
    ("hooks-config", check_hook_config),
    ("hooks-runner", check_hook_runner),
    ("workspace", check_workspace),
    ("ledger", check_ledger),
    ("products", check_products),
    ("tool-context", check_tool_context),
    ("uniprot", check_uniprot_tools),
    ("pdbtext", check_pdbtext),
    ("preprocess", check_preprocess),
    ("export-geometry", check_export_geometry),
    ("complexes", check_complexes),
    ("prankweb", check_prankweb),
    ("pockets", check_pockets),
    ("align-prior", check_align_and_prior),
    ("write-run", check_write_run),
    ("write-run-docking", check_write_run_docking),
    ("rsearch", check_rsearch),
    ("literature", check_literature),
    ("structure-parse", check_structure_parse),
    ("structure-tools", check_structure_tools),
    ("chembl", check_chembl_tools),
    ("chemistry-gate", check_chemistry_gate),
    ("receptor", check_receptor_tool),
    ("ligand-import", check_ligand_import),
    ("ligand-prep", check_ligand_prepare),
    ("docking", check_docking),
    ("listing", check_listing_tools),
    ("plan-deny", check_plan_deny),
    ("prompt", check_prompt),
    ("memory", check_memory),
    ("task-state", check_task_state),
    ("selective-compression", check_selective_compression),
    ("evidence", check_evidence_layer),
    ("bash", check_bash_tool),
    ("file-tools", check_file_tools),
    ("unconfigured", check_unconfigured_app),
    ("home-session", check_home_new_session),
)

# 需要假模型服务的组。只有命中其中之一才起服务，否则不占端口、也不等它。
SERVER_GROUPS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("provider", check_provider),
    ("loop", check_loop),
    ("permission-in-loop", check_permission_in_loop),
    ("hooks-in-loop", check_hooks_in_loop),
    ("summary-compaction", check_summary_compaction),
    ("live-app", check_live_app),
)


def _wanted(name: str, only: str | None) -> bool:
    """`only` 为空时全选，否则按子串匹配组名。"""
    return only is None or only in name


async def run_agent_checks(only: str | None = None) -> None:
    """循环层与 provider 的全部断言。

    `only` 是组名子串：传 `"permission"` 就只跑名字里带它的那些组。
    **不传时行为与以前完全一样**，`test_smoke.py` 就是按这个形状调的。
    """
    selected = [(name, group) for name, group in GROUPS if _wanted(name, only)]
    with_server = [
        (name, group) for name, group in SERVER_GROUPS if _wanted(name, only)
    ]
    if only is not None and not selected and not with_server:
        raise SystemExit(f"没有匹配 {only!r} 的检查组，用 --list 看全部名字")

    for name, group in selected:
        result = group()
        if inspect.isawaitable(result):
            await result
    if not with_server:
        return
    server = FakeModelServer()
    server.start()
    try:
        for name, group in with_server:
            await group(server)
    finally:
        server.stop()


def _main() -> None:
    """命令行入口：`--list` 看组名，`--only <子串>` 只跑匹配的组。"""
    argv = sys.argv[1:]
    if "--list" in argv:
        for name, _ in (*GROUPS, *SERVER_GROUPS):
            print(name)
        return
    only: str | None = None
    if "--only" in argv:
        index = argv.index("--only")
        if index + 1 >= len(argv):
            raise SystemExit("--only 需要一个组名子串，用 --list 看全部名字")
        only = argv[index + 1]
    asyncio.run(run_agent_checks(only))
    print(f"通过 {len(CHECKS)} 项检查：")
    for item in CHECKS:
        print(f"  - {item}")


if __name__ == "__main__":
    _main()
