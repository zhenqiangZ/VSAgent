from __future__ import annotations

import ast
import asyncio
import contextlib
import csv
import inspect
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from textual.widgets import Input, TextArea  # noqa: E402

from vsagent.app import VSAgentApp  # noqa: E402
from vsagent.core.agent.hooks import (  # noqa: E402
    POST_TOOL_USE,
    PRE_TOOL_USE,
    STOP,
    Hook,
    HookRunner,
    load_hooks,
)
from vsagent.core.agent.host import AgentHost  # noqa: E402
from vsagent.core.agent.loop import (  # noqa: E402
    MAX_RETRIES,
    MAX_STEPS,
    MAX_STOP_RETRIES,
    STOP_NOTICE,
    TOOL_HOOK_NOTICE,
    AgentLoop,
    missing_tool_reason,
)
from vsagent.core.agent.permission import (  # noqa: E402
    BILLED_ASK_AFTER,
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
from vsagent.core.agent.memory import (  # noqa: E402
    MEMORY_DIR_NAME,
    RESIDENT_LIMIT,
    MemoryError,
    MemoryKind,
    MemoryStore,
    RecallTool,
    RememberTool,
    memory_root,
)
from vsagent.core.agent.context import (  # noqa: E402
    CONTEXT_RATIO,
    DEGRADE_ORDER,
    EVIDENCE_LIMIT,
    EVIDENCE_MAX,
    EVIDENCE_REF_LIMIT,
    FOCUS_CONSTRAINTS,
    FOCUS_LIMIT,
    HISTORY_MAX,
    REFS_TURN_LIMIT,
    TOOL_ARCHIVE_MARK,
    ContextAssembler,
    ContextAssemblyError,
    Evidence,
    EvidenceStore,
    Layer,
    event_line,
    evidence_ref,
    evidence_ref_of,
    memory_entry_count,
    meta_summary,
    one_line,
    ordered_degraded,
    render_range,
    split_turns,
)
from vsagent.core.agent.event_log import (  # noqa: E402
    TOPIC_SOFT_LIMIT,
    CloseTopicTool,
    EventRuntime,
    RecallEventTool,
    event_boundary,
    open_tokens,
    record_step,
    render_event_detail,
    seal_range,
    seal_topic,
    topic_start,
)
from vsagent.core.agent.task import TaskTool  # noqa: E402
from vsagent.core.agent.topics import step_seconds, topic_views  # noqa: E402
from vsagent.core.agent.hypothesis import HypothesisTool  # noqa: E402
from vsagent.core.agent.spawn import (  # noqa: E402
    REPORT_TOKENS,
    AgentStatusTool,
    SpawnAgentTool,
    _cap_report,
    fork_messages,
    history_end,
    recent_start,
    task_preamble,
)
from vsagent.core.connection import (  # noqa: E402
    Connection,
    connection_path_for,
    load_connection,
    save_connection,
)
from vsagent.core.session_manager import SessionManager  # noqa: E402
from vsagent.core.agent.prompt import (  # noqa: E402
    DEEP_MARK,
    EMPTY_REASONING,
    KEEP_MESSAGES,
    MEMORY_NOTICE,
    MODEL_OUTPUT_LIMIT,
    OMISSION_MARK,
    REASONING_PLACEHOLDER,
    RULES_DIR_ENV,
    SUMMARY_INSTRUCTION,
    SUMMARY_NOTICE,
    EVENT_NOTICE,
    compress_tool_output,
    environment_block,
    estimate_tokens,
    is_key_line,
    platform_note,
    project_rules,
    rules_directory,
    stable_prefix,
    summary_messages,
    translate_message,
    truncate_for_model,
)
from vsagent.core.agent.provider import (  # noqa: E402
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
from vsagent.core.agent.tools import (  # noqa: E402
    BASH_OUTPUT_CHARS,
    BASH_TIMEOUT,
    BatchFailure,
    BatchOutcome,
    BashTool,
    GrepTool,
    PatchTool,
    ReadTool,
    ReadTurnTool,
    RetryableToolError,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    WriteTool,
    argument_error,
    change_counts,
    clip_bash_output,
    decode_output,
    decode_text,
    number_lines,
    resolve_path,
)
from vsagent.core.agent.workspace import Workspace  # noqa: E402
from vsagent.core.agent.ledger import (  # noqa: E402
    DIGEST_LINE_CHARS,
    DIGEST_MAX_LINES,
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
from vsagent.core.agent.domain import uniprot as uniprot_module  # noqa: E402
from vsagent.core.agent.domain import pdbtext  # noqa: E402
from vsagent.core.agent.domain import preprocess as preprocess_module  # noqa: E402
from vsagent.core.agent.domain import structure as structure_module  # noqa: E402
from vsagent.core.agent.domain import chembl as chembl_module  # noqa: E402
from vsagent.core.agent.domain import chemistry as chemistry_module  # noqa: E402
from vsagent.core.agent.domain import receptor as receptor_module  # noqa: E402
from vsagent.core.agent.domain import ligand as ligand_module  # noqa: E402
from vsagent.core.agent.domain import docking as docking_module  # noqa: E402
from vsagent.core.agent.domain import affinity as affinity_module  # noqa: E402
from vsagent.core.agent.domain import listing as listing_module  # noqa: E402
from vsagent.core.agent.domain import export as export_module  # noqa: E402
from vsagent.core.agent.domain import complexes as complexes_module  # noqa: E402
from vsagent.core.agent.domain import prankweb as prankweb_module  # noqa: E402
from vsagent.core.agent.domain import pockets as pockets_module  # noqa: E402
from vsagent.core.agent.domain import align as align_module  # noqa: E402
from vsagent.core.agent.domain import products as products_module  # noqa: E402
from vsagent.core.agent.domain import analysis as analysis_module  # noqa: E402
from vsagent.core.agent.domain import screen as screen_module  # noqa: E402
from vsagent.core.agent.domain import admet as admet_module  # noqa: E402
from vsagent.core.agent.domain import sar as sar_module  # noqa: E402
from vsagent.core.agent.domain import pharmacophore as pharmacophore_module  # noqa: E402
from vsagent.core.agent.domain.all_tools import catalog, families, tool_family  # noqa: E402
from vsagent.core.agent.domain.all_tools import domain_tools as all_domain_tools  # noqa: E402
from vsagent.core.agent.toolset import (  # noqa: E402
    LoadToolsTool,
    catalog_text,
    family_of,
    hidden_names,
    prune_families,
)
from vsagent.core.agent.trace import TraceWriter, log_directory_for  # noqa: E402
from vsagent.core.event_bus import EventBus  # noqa: E402
from vsagent.core.models import (  # noqa: E402
    Event,
    Message,
    Part,
    PartType,
    Session,
    Step,
    TaskState,
    ToolStatus,
)
from vsagent.screens.chat import ChatScreen  # noqa: E402
from vsagent.screens.chat import SidePanel  # noqa: E402
from vsagent.screens.connection_form import ConnectionFormScreen  # noqa: E402
from vsagent.screens.home import HomeScreen  # noqa: E402
from vsagent.screens.permission import PermissionDialog  # noqa: E402
from vsagent.screens.session_picker import ConfirmDialog, SessionPickerScreen  # noqa: E402
from vsagent.screens.spawn_agent import SpawnAgentScreen  # noqa: E402
from vsagent.screens.subagent import StartBar, SubagentChatScreen  # noqa: E402

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
#
# 16K 是 AGENTS.md 加「取证顺序」之前量的。那一段让前缀涨了约 700 token，
# 16K 时事件块正好被挤出降级链（`degraded` 里出现 `event_layer`），
# 「阶段块进了请求体」那条当场红。这里要验的是「溢出时封出的阶段能进请求体」，
# 不是「差几百 token 时先丢谁」，所以窗口跟着前缀一起抬。
#
# 18K 是 2026-10-30 前缀压到 1838 token 之后重新量的：前缀省下 4800 token，
# 事件块再也不会被挤掉，18K 就有了真正的余量（当时 18.0/18.4/18.8/19.3K 全绿）。
# **它仍然是「固定开销 + 余量」而不是阈值**：前缀再涨回四千 token，这条又会假红。
COMPACT_WINDOW = 18_000

# 领域工具的名字，**从装配处算出来**而不是手抄一份：抄一份的话，加了工具而忘了
# 更新这里，断言会一直通过，而它要挡的恰好就是「写了却没装」。
def _debug_tool_modules() -> list[tuple[str, list[Tool]]]:
    """`domain` 包里带 `domain_tools()` 的模块，与 `debug_tools._tool_modules` 同一份遍历。"""
    import importlib

    import vsagent.core.agent.domain as package

    found: list[tuple[str, list[Tool]]] = []
    for name in package.__all__:
        module = importlib.import_module(f"{package.__name__}.{name}")
        maker = getattr(module, "domain_tools", None)
        if callable(maker):
            found.append((name, maker()))
    return found


DOMAIN_TOOL_NAMES = [tool.spec.name for tool in all_domain_tools()]

# `domain` 包里全部模块的工具，比上面多一个：`assess_campaign` 常驻、不登记族
# （它决定这一轮装哪几族，自己先要装族就反了），所以不在按族装的清单里。
# 调试脚本的覆盖判据用它——那个工具同样要端到端跑过一遍。
DOMAIN_PACKAGE_TOOL_NAMES = [
    tool.spec.name for _name, tools in _debug_tool_modules() for tool in tools
]

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


class BoomTool(Tool):
    """抛一个不是 `ToolError` 的程序错误。"""

    spec = ToolSpec(
        name="boom",
        description="抛出程序错误",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        raise RuntimeError("拼 meta 时用了一个不存在的名字")


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
    """项目规则文件的读取与每轮重读。"""
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

    # 规则文件不设大小上限：整份注入，不从开头截一段。
    # 截断处会被模型当成规则的结尾，后面的门禁它根本不知道有。
    lines = [f"第 {index} 行" + "字" * 40 for index in range(819)]
    path.write_text("\n".join(lines), encoding="utf-8")
    big = project_rules(RULES_FIXTURE)
    check(big.startswith("第 0 行"), "大文件从第一行开始注入")
    check(big.endswith("第 818 行" + "字" * 40), "最后一行也在，没有截断")
    check(len(big.encode("utf-8")) > 32 * 1024, f"这份样例确实超过原先的 32KB：{len(big.encode('utf-8'))} 字节")

    # 读不出来只让这一段缺席，不能让整轮挂掉：`stable_prefix` 在装配之前调，那里没有 try。
    path.write_bytes(b"\xff\xfe\x00bad")
    check(project_rules(RULES_FIXTURE) == "", "编码坏了返回空字符串，不抛异常")

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
        with_rules = stable_prefix(Workspace(session.id, root=WORKSPACE_FIXTURE), session)
        check("只许读，不许写" in with_rules, "系统提示里带上规则文件的内容")
    finally:
        os.environ.pop(RULES_DIR_ENV, None)

    path.unlink(missing_ok=True)


def check_prefix() -> None:
    """稳定前缀：规则排最后、平台是真的、日期与状态目录不进前缀、不点名看不见的工具。

    预算、Plan 类别覆盖、越界参数表那三条在 `tools/check_prefix.py`：它们要装全部
    工具、还要读真实的规则文件，跑一次比这一组贵，分开放着更容易查。
    """
    session = Session(id="ses_prefix", title="前缀")
    RULES_FIXTURE.mkdir(parents=True, exist_ok=True)
    rules_path = RULES_FIXTURE / "AGENTS.md"
    rules_path.unlink(missing_ok=True)
    os.environ[RULES_DIR_ENV] = str(RULES_FIXTURE)
    try:
        # 规则缺失时内置那一段就是全貌，用来看「纪律里点了哪些工具」。
        bare = stable_prefix(Workspace(session.id, root=WORKSPACE_FIXTURE), session)
        check("这个项目的规则" not in bare, "没有规则文件时那一段不出现")

        rules_path.write_text("# 前缀判据规则\n", encoding="utf-8")
        with_rules = stable_prefix(Workspace(session.id, root=WORKSPACE_FIXTURE), session)
        check(with_rules.rstrip().endswith("# 前缀判据规则"), "规则排在前缀最后")
    finally:
        os.environ.pop(RULES_DIR_ENV, None)
        rules_path.unlink(missing_ok=True)

    # 平台那句必须是真的，日期与状态目录留在易变头里。
    check(platform_note() in bare, "平台那句在稳定前缀里")
    check("今天 " not in bare, "日期不进前缀（每天变一次会击穿缓存）")
    check("状态目录" not in bare, "状态目录不进前缀")

    # 内置纪律里不点任何领域工具名：它们首步看不见，点名等于教模型去撞墙。
    # 核心工具可以点名（load_tools、recall_event 这些一直在清单里）；项目规则也
    # 可以——门禁写在具体动作上，那里点出工具名是为了说清「这一步调用它之前先做什么」。
    domain = {name for names in catalog().values() for name in names}
    named = sorted(name for name in domain if re.search(rf"\b{re.escape(name)}\b", bare))
    check(not named, f"内置纪律里不出现领域工具名：{named}")

    # 点名了核心工具时，它们必须首步可见——看不见的工具写进前缀就是空话。
    hidden = hidden_names(session)
    core_named = [
        name
        for name in ("load_tools", "recall_event", "close_phase", "remember", "recall")
        if name in bare
    ]
    check(
        all(name not in hidden for name in core_named),
        f"前缀点名的核心工具首步可见：{core_named}",
    )
    check("load_tools" in bare, "前缀说明工具按族加载，否则模型不知道怎么装")
    check(
        "没有地址就不要发这段描述" in bare,
        "呈现纪律写明：具体的结构描述必须带页面地址",
    )
    check(
        "write_run" in bare and "open_viewer" in bare,
        "呈现纪律点名两个常驻工具（它们不属于任何族）",
    )
    check(
        "scoring 族" in bare and "微调" in bare,
        "打分纪律写明两路与 DTA 的微调后推理",
    )


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
    # a 排在 Plan 方式与配置规则之前，这两级也不再拦。
    check(
        judge(session_policy, "bash", {"command": "ls"}, agent_name="Plan").verdict
        is Verdict.ALLOW,
        "全放行之后 Plan 方式不再拦",
    )
    guarded = PermissionPolicy(
        project_rules=[PermissionRule("bash", "rm -rf*", Verdict.DENY, "项目配置")]
    )
    guarded.allow_all()
    check(
        judge(guarded, "bash", {"command": "rm -rf /"}).verdict is Verdict.ALLOW,
        "全放行之后项目 deny 也不再拦",
    )
    check(
        judge(guarded, "bash", {"command": "rm -rf /"}, cancelled=True).verdict is Verdict.DENY,
        "中断排在全部放行之前",
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

    # ---- 计费配额：前 10 次放行，第 11 次起每次都问 ----
    # 两个联网工具用真的（配额是照着工具名判的，名字写错就等于没拦）。
    from vsagent.core.agent.domain import websearch as websearch_module

    for tool in websearch_module.domain_tools():
        registry.register(tool)
    quota = PermissionPolicy()
    for index in range(BILLED_ASK_AFTER):
        decision = judge(quota, "search_web", {"query": f"q{index}"})
        check(
            decision.verdict is Verdict.ALLOW,
            f"配额内的第 {index + 1} 次直接放行：{decision.note()}",
        )
        quota.note_tool_run("search_web")
    check(quota.billed_runs == BILLED_ASK_AFTER, f"计数按执行次数走：{quota.billed_runs}")
    over = judge(quota, "search_web", {"query": "q-over"})
    check(
        over.verdict is Verdict.ASK and "计费" in over.reason,
        f"第 {BILLED_ASK_AFTER + 1} 次开始问，理由说的是钱：{over.reason}",
    )
    check(
        judge(quota, "fetch_webpage", {"url": "https://example.com"}).verdict is Verdict.ASK,
        "抓取与搜索共用一个配额（两种都按次计费）",
    )
    check(
        judge(quota, "read", {"path": "a.py"}).verdict is Verdict.ALLOW,
        "不花钱的工具不受配额影响",
    )
    # 被拒的那一次不算：`check` 只判定，计数由循环层在执行前调 `note_tool_run`。
    before = quota.billed_runs
    check(
        judge(quota, "search_web", {"query": "q-denied"}).verdict is Verdict.ASK,
        "再判定一次仍然是问（没有「问过一次就放行」）",
    )
    check(
        quota.billed_runs == before,
        f"判定本身不推进计数（只有真正执行的那一次才算）：{quota.billed_runs}",
    )
    # 按过 a 之后计费配额也一并跳过：a 的语义是本会话不再询问。
    quota.allow_all()
    check(
        judge(quota, "search_web", {"query": "q-all"}).verdict is Verdict.ALLOW,
        "全放行之后计费工具不再问",
    )
    quota.clear_session()
    check(quota.billed_runs == 0, "换会话把配额清零")
    check(
        judge(quota, "search_web", {"query": "q-new"}).verdict is Verdict.ALLOW,
        "新会话重新从零计",
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

    # 工作目录之外的访问：单独一级判定，排在「按工具记住」之前。
    # 这一级管的是没按过 a 的情况：模型走到工作目录外面时要让用户看一次。
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
    # 按 a 之后越界不再问：a 的授权范围是整个会话，越界与计费都在里面。
    bounded.allow_all()
    check(
        judge(bounded, "bash", {"command": "dir"}).verdict is Verdict.ALLOW,
        "全放行之后 bash 站内不再问",
    )
    check(
        judge(bounded, "bash", {"command": f"type {outside}"}).verdict is Verdict.ALLOW,
        "全放行之后 bash 越界也不再问",
    )
    check(
        judge(bounded, "read", {"path": outside}).verdict is Verdict.ALLOW,
        "全放行之后越界读不再问",
    )
    check(
        judge(bounded, "read", {"path": "notes.md"}).verdict is Verdict.ALLOW,
        "全放行之后站内读不再问",
    )
    # 中断排在 a 之前：按了 a 也不等于按了中断之后还能接着执行。
    check(
        judge(bounded, "read", {"path": outside}, cancelled=True).verdict is Verdict.DENY,
        "全放行之后中断仍然拒绝",
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
    # 「没有越界」，没按过 a 时也会被当成站内命令直接执行。这是这一组最该守的。
    for risky in ("rm -rf /", "rm -rf /usr", "cat /etc", "rm -rf ~", "rm -rf ~/data"):
        check(
            bool(bounded.outside_paths(registry.get("bash"), {"command": risky})),
            f"顶层目录与家目录要判成越界：{risky}",
        )
    check(
        not bounded.outside_paths(
            registry.get("bash"),
            {"command": f"cat {Path.home() / '.vsagent' / 'state.toml'}"},
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
    config = root / ".vsagent"
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
        live.get("search_literature") is not None and live.get("dock") is not None,
        "没在配置里点名的工具照旧装上",
    )
    check(
        "search_pdb" not in {spec["function"]["name"] for spec in live.specs()},
        "被关的工具不在请求体的 tools 数组里（模型看不到它）",
    )
    check(
        {tool.spec.name for tool in all_domain_tools()} - set(live.names()) == {"search_pdb"},
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
        sys.executable = str(exe_dir / "VSAgent.exe")
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
        ".vsagent" in str(Workspace("ses_x").root) and "workspaces" in str(Workspace("ses_x").root),
        "默认根在 ~/.vsagent/workspaces 下",
    )


def check_config_paths() -> None:
    """用户目录改名之后旧数据还能找回来。

    改名前那一份在 `~/.opencode_tui`，改名后代码只认 `~/.vsagent`。不搬的话
    升级一次就是「会话全没了、接口还得重填」，而程序本身一切正常、不会报错。
    """
    from vsagent.core import paths

    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        legacy = home / paths.LEGACY_CONFIG_DIRECTORY
        legacy.mkdir()
        (legacy / "state.toml").write_text("会话", encoding="utf-8")
        moved = paths.migrate_legacy_config(home)
        check(
            moved is not None and moved.target == home / paths.CONFIG_DIRECTORY,
            f"旧目录整体搬到新名字：{moved}",
        )
        check(moved is not None and moved.files == 1, f"迁移结果里报出搬了几个文件：{moved}")
        check(
            (home / paths.CONFIG_DIRECTORY / "state.toml").read_text(encoding="utf-8") == "会话",
            "旧状态文件跟着搬过去，内容不变",
        )
        check(not legacy.exists(), "搬完之后旧目录不再留着")
        # 两边都在：新的说了算，旧的留着不动（合并会覆盖用户已经在新目录里写的东西）。
        legacy.mkdir()
        (legacy / "stale.txt").write_text("旧", encoding="utf-8")
        check(paths.migrate_legacy_config(home) is None, "新目录已经存在时不再搬")
        check(
            (home / paths.CONFIG_DIRECTORY / "state.toml").exists(),
            "新目录没有被旧目录覆盖",
        )
        check((legacy / "stale.txt").exists(), "旧目录原样留着，不删用户的东西")
    # 没有旧目录时什么都不做，也不创建空目录。
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        check(paths.migrate_legacy_config(home) is None, "没有旧目录时返回 None")
        check(not (home / paths.CONFIG_DIRECTORY).exists(), "没有旧目录时不凭空建一个")
    # 同一个函数也用来搬**项目侧**那一份（`<项目>/.opencode_tui` → `<项目>/.vsagent`）：
    # 那里面是工具开关、判定规则与钩子，丢了模型会多出几个本该关掉的工具。
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        legacy = project / paths.LEGACY_CONFIG_DIRECTORY
        legacy.mkdir()
        (legacy / "tools.toml").write_text("[tools]\n", encoding="utf-8")
        check(
            paths.migrate_legacy_config(project) is not None
            and (project / paths.CONFIG_DIRECTORY / "tools.toml").exists(),
            "项目侧配置目录也按同一个规则搬过去",
        )
        check(
            (project / paths.CONFIG_DIRECTORY / "tools.toml").exists(),
            "工具开关跟着搬到项目侧新目录",
        )


def check_config_migrate() -> None:
    """P0-2：项目侧配置也要跟着改名搬过去，而且搬的是 exe 旁边那一份。

    四件必须成立的事：
      · 只存在旧目录时迁移成功，内容逐字节相同，并报出搬了几个文件；
      · 两个目录都在时不动新的，也不删旧的；
      · 打包布局下搬的是**可执行文件旁边**那份，不是启动目录；
      · 搬完之后 `load_tool_switches` 真的读到了被关掉的工具。

    这条是 P0 而不是 P2：漏搬的不只是工具开关，`permissions.toml` 里的 deny
    也一起失效——用户设的安全边界在打包版上等于不存在。
    """
    from vsagent.core import paths

    content = (
        "[tools]\n"
        "search_pdb = false\n"
        "search_literature = false\n"
        "switch_receptor = false\n"
    )

    # ---- 一、只存在旧目录：整体搬过去，内容逐字节相同 ----
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        legacy = project / paths.LEGACY_CONFIG_DIRECTORY
        (legacy / "nested").mkdir(parents=True)
        (legacy / "tools.toml").write_text(content, encoding="utf-8")
        (legacy / "permissions.toml").write_text(
            '[bash]\ndeny = ["rm -rf*"]\n', encoding="utf-8"
        )
        (legacy / "nested" / "hooks.toml").write_text("[pre]\n", encoding="utf-8")

        moved = paths.migrate_legacy_config(project)
        check(moved is not None and moved.files == 3, f"报出搬过去的文件数（含子目录）：{moved}")
        check(
            (project / paths.CONFIG_DIRECTORY / "tools.toml").read_bytes()
            == content.encode("utf-8"),
            "工具开关逐字节搬过去",
        )
        check(
            (project / paths.CONFIG_DIRECTORY / "permissions.toml").exists()
            and (project / paths.CONFIG_DIRECTORY / "nested" / "hooks.toml").exists(),
            "判定规则与子目录里的钩子一起搬过去",
        )
        check(not legacy.exists(), "搬完之后旧目录不在了")

    # ---- 二、两个目录都在：新的说了算，旧的原样留着 ----
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        (project / paths.LEGACY_CONFIG_DIRECTORY).mkdir()
        (project / paths.LEGACY_CONFIG_DIRECTORY / "tools.toml").write_text(
            "[tools]\nsearch_pdb = false\n", encoding="utf-8"
        )
        (project / paths.CONFIG_DIRECTORY).mkdir()
        (project / paths.CONFIG_DIRECTORY / "tools.toml").write_text(
            "[tools]\nbash = false\n", encoding="utf-8"
        )
        check(paths.migrate_legacy_config(project) is None, "新目录已经存在时不再搬")
        check(
            "bash"
            in (project / paths.CONFIG_DIRECTORY / "tools.toml").read_text(encoding="utf-8"),
            "新目录里的开关没有被旧目录覆盖",
        )
        check(
            (project / paths.LEGACY_CONFIG_DIRECTORY / "tools.toml").exists(),
            "旧目录原样留着，不删用户的东西",
        )

    # ---- 三、打包布局：搬的是可执行文件旁边那份，不是启动目录 ----
    # 从别的目录启动 exe 时 cwd ≠ exe 所在目录。搬 cwd 会搬到用户没在看的地方，
    # 而 exe 旁边那份原封不动——正是这次实测到的形态（dist/.opencode_tui 还在）。
    with tempfile.TemporaryDirectory() as raw:
        exe_dir = Path(raw) / "dist"
        exe_dir.mkdir()
        legacy = exe_dir / paths.LEGACY_CONFIG_DIRECTORY
        legacy.mkdir()
        (legacy / "tools.toml").write_text(content, encoding="utf-8")

        frozen, executable = getattr(sys, "frozen", None), sys.executable
        try:
            sys.frozen = True
            sys.executable = str(exe_dir / "VSAgent.exe")
            # 启动目录是另一个地方（模拟从桌面或别的项目目录启动）。
            elsewhere = Path(raw) / "somewhere"
            elsewhere.mkdir()
            target = project_config_directory(elsewhere)
            check(
                target == exe_dir / paths.CONFIG_DIRECTORY,
                f"打包后配置目录取自 exe 所在处，与启动目录无关：{target}",
            )
            check(
                paths.migrate_legacy_config(target.parent) is not None,
                "打包布局下迁移了 exe 旁边那份",
            )
            disabled, source = load_tool_switches(elsewhere)
            check(
                {"search_pdb", "search_literature", "switch_receptor"} <= set(disabled),
                f"迁移后读到了被关掉的三个工具：{sorted(disabled)}",
            )
            check(
                source.get("search_pdb") == exe_dir / paths.CONFIG_DIRECTORY / "tools.toml",
                f"开关的来源指向 exe 旁边那份：{source.get('search_pdb')}",
            )
        finally:
            if frozen is None:
                del sys.frozen
            else:
                sys.frozen = frozen
            sys.executable = executable


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

    system_prompt = stable_prefix(workspace, session)
    prefix = stable_prefix(workspace, session)
    check(
        "引用一律用 handle，不要写文件路径" in system_prompt,
        "系统提示里写着「收 handle 不收路径」那条纪律",
    )
    check(
        "以各自工具的描述为准" in system_prompt,
        "收路径的动作不枚举，改为指向各工具描述（枚举过三份写法，全都漂了）",
    )
    # handle 的出处改指「任务状态里的工作区索引」：那是每步都在的 TASK 层。
    # 原先前缀写「从 list_workspace 里取」，而它属于 listing 族、首步不可见——
    # 模型照着做会先撞一次墙。
    check(
        "工作区索引" in system_prompt,
        "handle 从任务状态的工作区索引里取",
    )
    check(
        "list_workspace" not in system_prompt and "describe_entry" not in system_prompt,
        "前缀不点名首步看不见的工具（撞墙一次就是白花一步）",
    )
    # 复核癖：工具已经把结果写进返回，模型还要 ls → stat → md5sum 再核一遍。
    # 实测五次运行里复核占工具调用的 25–81%，钱不多（多 35 步只多 $0.014），
    # 但会撞满步数上限、还会把要核的那段上下文挤出窗口。
    check(
        "不要再用 bash 复核" in system_prompt,
        "提示里写着：工具已经给出的事实不要再复核一遍",
    )
    check(
        "md5sum" in system_prompt and "read" in system_prompt,
        "点明了不该做的（ls/stat/md5sum 逐项）与该用的（read 一次看完）",
    )
    # 现场来的三条：模型写 pwd、写 cp、把上一条命令的 cd 当基准。它只能从系统提示里知道
    # 「每次调用是一个新解释器」「这里是哪个平台」，否则只能靠一次次失败去猜。
    check(
        "每一次" in system_prompt or "每调用一次" in system_prompt,
        "系统提示里写着每次 bash 调用都是全新的解释器",
    )
    check(
        "cd" in system_prompt and "&&" in system_prompt,
        "换目录要写在同一条命令里，提示里给了写法（cd ... && ...）",
    )
    # 这一条原来是「提示里点明这是 Windows」。那句话写死在 Linux 上跑的时候，
    # 是在**教模型写错命令**（实测 `dir /s /b`、`netstat -ano | findstr`，F2）。
    check(
        platform_note() in system_prompt,
        "提示里点明实际平台（按 sys.platform 现取，不写死）",
    )
    check(
        ("这套环境是 Windows" in system_prompt) == sys.platform.lower().startswith("win"),
        f"平台那句与真实平台一致（sys.platform={sys.platform}）",
    )
    # 子 agent 在 37 步里一次都没被派过：前缀全文不含 spawn_agent，模型只在
    # 工具清单的一行 description 里见过它——那句话只在「已经想到要用」时才有用。
    # 触发条件与盘点写在同一段：什么时候该派取决于口袋是否唯一、候选结构有几个。
    check(
        "候选结构 ≥3" in system_prompt and "spawn_agent" in system_prompt,
        "前缀里有父会话视角的派发规则，且点到具体工具名",
    )
    check(
        "子 agent 看不到你的推理" in system_prompt,
        "写清了「什么时候不该派」：子任务依赖当前推理时不该派",
    )
    # 检索也是同一类问题：三个工具在注册表里、也能装，但项目规则原先只有「不要检索」
    # 这一侧的限制，没有任何一句说「什么时候该查」。实测 α7 那次会话 0 次调用，
    # 模型在 reasoning 里明确权衡过并选择了不查。取证顺序必须写进规则，
    # 否则它只存在于工具描述里，而工具没装族时模型读不到。
    check("search_corpus" in system_prompt, "规则里有取证顺序的第一站：本地语料")
    check("命中就停" in system_prompt, "取证顺序写明命中即止，不是把三层都走一遍")
    check(
        "search_literature" in system_prompt and "search_web" in system_prompt,
        "本地没有时给出去处：PubMed 与联网搜索都点到名字",
    )
    check(
        "明说没查到" in system_prompt,
        "查不到时要说「没查到」，不是「没有这回事」",
    )
    # 规则里点名的检索工具必须真的能装。规则教模型去调一个装不进来的工具，
    # 它撞墙之后会退回「自己做」——那正是 §1.3 要解决的那个问题。
    # 名字从规则文件里现取，不手抄：改规则时不用回来同步这份名单。
    import re as _re

    named = set(_re.findall(r"`(search_[a-z_]+)`", project_rules(rules_directory(ROOT))))
    installable = {name for names in catalog().values() for name in names}
    installable -= set(load_disabled(ROOT)[0])
    check(
        named and named <= installable,
        f"规则里点名的检索工具都能装：{sorted(named - installable)}",
    )
    child = Session(id="ses_child", title="子任务", depth=1)
    child_prefix = stable_prefix(workspace, child)
    check(
        "该派子 agent" not in child_prefix,
        "子 agent 的前缀里没有派发规则（它不能再派，写了只是白占额度）",
    )
    check("你是从父会话分出的子 agent" in child_prefix, "子 agent 有它自己的身份段")
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
    # 引用清单只列**最近** REFS_TURN_LIMIT 组，更早的写一行「再往前还有 N 组」。
    # 原先断言 dropped_turns[0]（最早那组）在清单里，它隐含「移出不超过 20 组」；
    # 而移出多少组取决于前缀长度——AGENTS.md 长一截就会破，破的还不是被测的那条契约。
    check(
        f"- turn {built.dropped_turns[-1]}：" in built.messages[1]["content"],
        "最近被移出的交互组在引用清单里",
    )
    check(
        built.dropped <= REFS_TURN_LIMIT
        or "再往前还有" in built.messages[1]["content"],
        f"移出超过 {REFS_TURN_LIMIT} 组时写明「再往前还有 N 组」，不装作列全了",
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


def check_output_limit() -> None:
    """截断文案要能让模型自己找到全文（P2-3）。

    实测模型第一次自己写了 `[:300]`，只看到前半段，以为「输出被压缩了」，
    于是同一个 `meta.json` 用 bash + python3 读了四次（11.4K / 6.4K / 2.9K / 1.4K
    字符）。原来的文案只说「省略多少」，不说原文多长、不说怎么翻页——
    模型能做的只有换个写法再读一遍。
    """
    # 三行、一万二千字符：行数太少，按内容挑不出东西，必然走按位置兜底那一档。
    single = "甲" * 5000 + "\n" + "乙" * 5000 + "\n" + "丙" * 2000
    check(len(single) > MODEL_OUTPUT_LIMIT, f"样本确实超限：{len(single)}")
    cut = truncate_for_model(single)
    check(f"原文 {len(single)} 字符" in cut, f"写明原文多长：{cut[2000:2300]}")
    check("read <path> offset=0 limit=2000" in cut, "给出拿全文的具体写法")
    check("grep" in cut, "也给了定位关键行的那条路")
    check(
        "重读同一个文件也一样" in cut,
        "点明重读不会变长（那次连读四次的起因就是不知道这一点）",
    )
    check(
        len(cut) <= MODEL_OUTPUT_LIMIT + 300,
        f"说明文字本身不把额度吃回去：{len(cut)}",
    )
    check(truncate_for_model("短输出") == "短输出", "没超上限时一个字都不加")


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

    # 长输出封顶：现场是模型用 `python -c` 把整份产物 JSON 打印出来（27.8 KB /
    # 6,984 token），而那一步只要二十个口袋的摘要。封顶只砍中间，开头与结尾都在，
    # 省略行里写明省了多少、完整输出多大、中间那段怎么取。
    check(clip_bash_output("短输出") == "短输出", "没超限的输出原样返回")
    at_limit = "x" * BASH_OUTPUT_CHARS
    check(clip_bash_output(at_limit) == at_limit, "正好等于上限时不截断")
    flood = await tool.run({"command": f'"{sys.executable}" -c "print(\'x\' * 40000)"'})
    check(len(flood.output) < 40000, f"超长输出被压到预算内：{len(flood.output)} 字符")
    check(flood.output.startswith("x" * 100), "开头那段留着——清单与表头在最前面")
    check(flood.output.endswith("x" * 100), "结尾那段留着——构建与测试的结论在最后")
    marker = next((line for line in flood.output.splitlines() if "中间省略" in line), "")
    check(
        "40,000" in marker,
        f"省略行说清省了多少、原文多大：{marker}",
    )
    check(
        "重跑" in marker and "grep" in marker,
        "省略行告诉模型怎么只看中间那段，不是让它整段重打",
    )
    # 多行日志：省略行要给出省掉多少行，头尾也要正好切在整行之外。
    lines = "\n".join(f"line {index:04d} " + "y" * 80 for index in range(400))
    clipped = clip_bash_output(lines)
    check(clipped.startswith("line 0000"), "多行输出的开头那一行留着")
    check(clipped.rstrip().endswith("y" * 80) and "line 0399" in clipped, "最后一行留着")
    check("约" in clipped and "行" in clipped, "多行输出要报出省略了多少行")

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
    registry.register(BoomTool())
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

    # 程序错误不是「这次调用失败」。卡片要写成失败，错误本身照旧往外抛：
    # 停在「运行中」的话，界面看着像它还在算（一次 NameError 挂了半小时）。
    server.clear()
    recorder.clear()
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[tool_call_delta(0, call_id="c1", name="boom", arguments="{}")]
                ),
                usage_chunk(1, 1),
            ]
        )
    )
    session_boom = Session(id="ses_boom", title="程序错误")
    recorder.session = session_boom
    ask(session_boom, "试一下")
    raised: BaseException | None = None
    try:
        await loop.stream_reply(session_boom, "")
    except BaseException as error:  # 内核的程序错误照旧向外抛，这里只收下来看
        raised = error
    check(
        isinstance(raised, RuntimeError),
        f"程序错误照旧抛出去：{type(raised).__name__}",
    )
    boom_parts = [
        part
        for part in session_boom.messages[-1].parts
        if part.type is PartType.TOOL
    ]
    check(
        bool(boom_parts) and boom_parts[0].tool_status is ToolStatus.FAILED,
        f"卡片写成失败，不停在运行中：{boom_parts[0].tool_status if boom_parts else '没有工具 Part'}",
    )
    check(
        "内核内部错误" in (boom_parts[0].tool_output or "") and "RuntimeError" in (boom_parts[0].tool_output or ""),
        f"失败原因写明是内核错误：{boom_parts[0].tool_output if boom_parts else ''}",
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
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(2, 2)]))
    message = await loop.stream_reply(compact_session, "")
    loop.context_window = 200_000
    assert message is not None
    # 溢出不再请求摘要：那一段被就地封成一个阶段（流水 + 结论），零请求。
    # 摘要机制本身已经退休（§4.7）：它的三段分别由 TASK、流水、阶段结论覆盖。
    compactions = [part for part in message.parts if part.type is PartType.COMPACTION]
    check(compactions == [], "溢出不再写摘要 Part（摘要已退休）")
    check(bool(compact_session.events), "溢出时封出一个主题")
    check(compact_session.events[-1].kind == "auto", "自动封存的主题标成 auto")
    check(
        compact_session.summary == "" and compact_session.summary_upto == 0,
        "摘要不再被写",
    )
    request = server.requests[-1]["messages"]
    check(request[0]["role"] == "system", "第一条仍然是系统提示")
    body = "\n".join(str(item["content"]) for item in request)
    check(EVENT_NOTICE in body, "主题块进了请求体")
    covered = max(compact_session.compact_upto, compact_session.event_cut[0])
    # 按**消息本身**数，不按子串数：主题标题与结论里也会出现「第 N 条消息」，
    # 用子串数会把流水里的引用算成「原文还在」。
    present = sum(
        1
        for item in request
        if str(item.get("content") or "").startswith("第 ")
        and "条消息" in str(item.get("content"))
    )
    check(present == 40 - covered, f"阶段覆盖的那一段原文不再发出去：还剩 {present} 条")
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
    """手动压缩：把当前这一段封成一个阶段，位置往前推，下一次发问只带最近几条。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = build_loop(server, bus, registry)

    session = Session(id="ses_compact", title="手动压缩")
    for index in range(12):
        ask(session, f"第 {index} 句")
    check(session.compact_upto == 0, "新会话的压缩位置是零")
    check(session.events == [], "新会话没有阶段")

    server.script(Reply(chunks=[*text_chunks("收到了", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    sent = len(session.messages)
    full = len(server.requests[-1]["messages"])
    check(full > KEEP_MESSAGES, f"没压缩时发出去的消息远多于保留条数：{full}")

    kept = loop.compact_session(session)
    check(kept == 0, f"压完之后原文全部让位给摘要：还剩 {kept} 条")
    check(session.compact_upto == sent, "压缩位置推到这一段末端")
    check(len(session.messages) == sent, "压缩不动会话里的消息")
    check(session.summary == "", "压缩这一步本身不写摘要")
    check(bool(session.events), "压缩这一步把这一段封成一个主题")
    check(session.events[-1].kind == "local", "用户按出来的主题标成 local")
    check(session.events[-1].cut_message == session.compact_upto, "主题覆盖到压缩位置")

    server.clear()
    server.script(Reply(chunks=[*text_chunks("接着答", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(session.summary == "" and session.summary_upto == 0, "下一次发问也不再写摘要")
    fed = server.requests[-1]["messages"]
    check(fed[0]["role"] == "system", "压缩之后最前面仍然是系统提示")
    body = "\n".join(str(item.get("content") or "") for item in fed)
    check(EVENT_NOTICE in body, "阶段块进了请求体")
    raw = sum(
        1
        for item in fed
        if str(item.get("content") or "").startswith("第 ")
    )
    check(raw == 0, f"原文全部让位给流水：还剩 {raw} 条")
    check(
        [part.text for part in message.parts if part.type is PartType.COMPACTION] == [],
        "这一轮没有摘要 Part",
    )
    check(
        [part.text for part in message.parts if part.type is PartType.TEXT] == ["接着答"],
        "这一步照常问模型",
    )
    check(session.total_tokens() > 0, "用量记进 tokens")

    same = session.compact_upto
    server.clear()
    server.script(Reply(chunks=[*text_chunks("再答", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(session.compact_upto >= same, "再压一次不会把位置往回退")
    check(
        [part.text for part in message.parts if part.type is PartType.COMPACTION] == [],
        "已经封存的那一段不再收一次，也不再重复提示（P3-1）",
    )
    check(len(server.requests) == 1, "不再收摘要时只发一次请求")

    fresh = Session(id="ses_compact_few", title="消息太少")
    ask(fresh, "只有一句")
    check(loop.compact_session(fresh) == 1, "消息不够多时原样返回条数")
    check(fresh.compact_upto == 0, "消息不够多时不记压缩位置")

    # 带工具的轮次在轮末已经自动封存过：这里没有新的一段可封（`_seal_phase`
    # 返回 None），但 `/compact` 仍要把硬边界推到最后一个事件的末端。不推的话
    # 最新一段的原文留在 RECENT 里，用户按下去什么也没腾出来，界面还回
    # 「这一段已经压过了」。
    sealed = Session(id="ses_compact_sealed", title="轮末已封存")
    for index in range(6):
        sealed.add_message(Message(role="user")).add_part(
            Part(type=PartType.TEXT, text=f"第 {index} 句")
        )
        reply = sealed.add_message(Message(role="assistant"))
        reply.add_part(Part(type=PartType.STEP_START))
        reply.add_part(
            Part(
                type=PartType.TOOL,
                tool_name="echo",
                tool_status=ToolStatus.COMPLETED,
                tool_input={},
                tool_output="ok",
            )
        )
        reply.add_part(Part(type=PartType.STEP_FINISH))
        seal_range(
            sealed,
            kind="auto",
            upto=event_boundary(sealed, len(sealed.messages)),
            title=f"主题{index + 1}",
        )
    check(sealed.compact_upto == 0 and len(sealed.events) == 6, "轮末封存不推压缩位置")
    check(
        loop.compact_session(sealed) == 0,
        "已经封存过的会话按下去也要把原文压掉，返回 0 条要发",
    )
    check(
        sealed.compact_upto == len(sealed.messages),
        f"压缩位置推到最后一个事件的切点：{sealed.compact_upto}",
    )
    check(len(sealed.events) == 6, "没有新的一段可封时不重复封")
    check(loop.compact_session(sealed) == 0, "再按一次仍是 0 条，位置不往回退")


async def check_overflow_phase(server: FakeModelServer) -> None:
    """溢出时封存阶段而不是写摘要：原文让位、流水进场、Part 一字不改。"""
    server.clear()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool())
    constraint = "约束：所有报告用中文写，分子编号保留原始大小写。"
    session = Session(id="ses_phase", title="阶段压缩")
    # 窗口按前缀现算，不写固定数：这一份会话要造出「装不下」的局面，而前缀一涨，
    # 固定窗口就会先丢掉事件层（降级链里它排在 RECENT 之前），这条判据于是假红。
    # 规则文件 2026-10-31 从 843 涨到 2059 token，固定 24K 就是这么失效的。
    # 24_900 是这份 31 条消息刚好溢出、又容得下事件块的余量。
    prefix_tokens = estimate_tokens(
        stable_prefix(Workspace(session.id, root=ROOT), session)
    )
    loop = build_loop(server, bus, registry, context_window=prefix_tokens + 24_900)
    ask(session, constraint)
    filler = "分子筛选过程的中间记录，" + "记" * 500
    for index in range(30):
        item = session.add_message(
            Message(role="user" if index % 2 == 0 else "assistant", session_id=session.id)
        )
        item.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条，{filler}"))
    original = [part.text for message in session.messages for part in message.parts]
    check(len(session.messages) == 31, "这一段会话造出了 31 条消息")

    server.script(Reply(chunks=[*text_chunks("接着做", size=2), usage_chunk(1, 1)]))
    message = await loop.stream_reply(session, "")
    assert message is not None
    check(len(server.requests) == 1, f"溢出不再多花一次摘要请求：{len(server.requests)}")
    check(session.summary_upto == 0, "摘要没被写（它已经退休）")
    check(bool(session.events), "溢出封出了主题")
    covered = max(session.compact_upto, session.event_cut[0])
    check(covered > 0, f"主题覆盖到第 {covered} 条")

    fed = server.requests[-1]["messages"]
    check(fed[0]["role"] == "system", "发出去的第一条是系统提示")
    body = "\n".join(str(item.get("content") or "") for item in fed)
    check(EVENT_NOTICE in body, "主题块进了请求体")
    check("结论：" in body, "主题结论也进了请求体")
    check(
        session.events[-1].steps == [],
        "纯文本会话没有工具调用，主题只有标题与结论",
    )
    raw = sum(
        1
        for item in fed
        if str(item.get("content") or "").startswith("第 ")
    )
    check(raw == 31 - covered, f"被主题覆盖的原文不再发出去：还剩 {raw} 条")

    kept = [part.text for message in session.messages[:31] for part in message.parts]
    check(kept == original, "会话里的 Part 内容一条不少、一字不改")
    check(
        len(session.messages[1].parts[0].text) == len(f"第 0 条，{filler}"),
        "被阶段覆盖的那一条在会话里仍是完整长度",
    )
    check(session.total_tokens() > 0, "这一轮的用量记进 tokens")


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

    # 没按过 a 时，越界仍然要问一次。
    server.clear()
    recorder.clear()
    bound_session = Session(id="ses_bound", title="越界要问")
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

    # 按 a 之后越界不再问：授权范围是整个会话，越界与计费都在里面。
    server.clear()
    recorder.clear()
    loop.policy.allow_all()
    free_session = Session(id="ses_free_all", title="全放行之后越界")
    recorder.session = free_session
    ask(free_session, "跑一条带外面的路径的命令")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c6",
                            name="bash",
                            arguments=json.dumps({"command": f"echo {OUTSIDE_PATH}"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("跑完了", size=2), usage_chunk(1, 1)]),
    )
    await loop.stream_reply(free_session, "")
    tools = [part for part in free_session.messages[-1].parts if part.type is PartType.TOOL]
    check(not recorder.names("permission.ask"), "全放行之后越界不再发 permission.ask")
    check(tools[0].tool_status is ToolStatus.COMPLETED, f"越界的命令直接执行：{tools[0].tool_status}")
    loop.policy.clear_session()

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

    # 没有人订阅 permission.ask：必须立刻拒绝，而不是等一个没人会回答的 future。
    # 这一条是实测里踩到的：脚本调 bash 用绝对路径 → 越界要问 → 卡死到超时。
    silent_bus = EventBus()
    silent_loop = build_loop(
        server,
        silent_bus,
        registry,
        policy=PermissionPolicy(workspace_root=WORKSPACE_FIXTURE),
    )
    server.clear()
    silent_session = Session(id="ses_silent", title="无人值守")
    ask(silent_session, "跑一条命令")
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c9",
                            name="bash",
                            arguments=json.dumps({"command": "echo nobody_answers"}),
                        )
                    ]
                ),
                usage_chunk(2, 2),
            ]
        ),
        Reply(chunks=[*text_chunks("没跑", size=2), usage_chunk(1, 1)]),
    )
    answer = await asyncio.wait_for(silent_loop.stream_reply(silent_session, ""), timeout=10)
    tools = [part for part in answer.parts if part.type is PartType.TOOL]
    check(bool(tools) and tools[0].tool_status is ToolStatus.FAILED, "没有人答复时直接判失败")
    check(
        "没有界面" in (tools[0].tool_output or ""),
        f"失败原因写明没有人可以问：{tools[0].tool_output}",
    )
    check(
        [part.text for part in answer.parts if part.type is PartType.TEXT] == ["没跑"],
        "被拒之后这一轮照常走完",
    )
    check(not silent_loop._pending_permissions, "待答复的表清空了")


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


async def wait_idle(app: VSAgentApp, pilot: object, timeout: float = 10.0) -> None:
    """等当前生成任务结束。"""
    waited = 0.0
    while waited < timeout:
        screen = app.screen
        if isinstance(screen, ChatScreen) and not screen._busy:
            return
        await pilot.pause(0.1)
        waited += 0.1
    raise AssertionError("等待生成任务结束超时")


def tool_parts_of(app: VSAgentApp) -> list[Part]:
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

    app = VSAgentApp(root=ROOT, state_path=state)

    async def fake_stream(session: object, prompt: str) -> None:
        """不发请求：这一组验的是会话选择，不是模型链路。"""
        return None

    # 每个会话一个循环层：新会话会现建一个，所以补丁要跟着装配走，
    # 只打在 `app.agent` 上只能盖住当前这一个。
    build_loop = app._build_loop

    def build_with_fake(session: object, ledger: object) -> AgentLoop:
        loop = build_loop(session, ledger)  # type: ignore[arg-type]
        loop.stream_reply = fake_stream  # type: ignore[method-assign]
        return loop

    app.host.factory = build_with_fake  # type: ignore[assignment]
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

    app = VSAgentApp(root=ROOT, state_path=state)
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

    **根目录要用隔离的那一个**（不是 `ROOT`）：`.vsagent/tools.toml` 是
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
    app = VSAgentApp(root=bare_root, state_path=state)
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
            app.agent.registry.get("write_run") is not None
            and app.agent.registry.get("open_viewer") is not None,
            "呈现工具常驻核心，不靠 load_tools 装族",
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
    app = VSAgentApp(root=ROOT, state_path=state, policy=PermissionPolicy(default_ask=frozenset()))
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

    # 一出生就 stale 是 P1-1 的病：登记时按**目录**算指纹，对账就不会立刻否掉它。
    # 这条与上一条的区别在于「登记时用的函数」——上一条本来就用了 fingerprint_path，
    # 而 `write_run` 曾经用的是 payload 的文本哈希，两边口径不同。
    def _state(handle) -> str:
        # `file_state` 是派生值，只有 reconcile() 会更新它；直接读字段永远是登记时的 ok。
        ledger.reconcile()
        return next(item.file_state for item in ledger.entries() if item.handle == handle)

    fresh_dir = root / "fresh_run"
    fresh_dir.mkdir()
    (fresh_dir / "meta.json").write_text("{}", encoding="utf-8")
    fresh_entry = ledger.add(
        "artifact",
        path=fresh_dir,
        fingerprint=fingerprint_path(fresh_dir),
        summary="刚写完的运行目录",
    )
    check(_state(fresh_entry.handle) == "ok", "按目录算的指纹对账之后仍是 ok")
    (fresh_dir / "meta.json").write_text('{"改过": true}', encoding="utf-8")
    check(
        _state(fresh_entry.handle) == "stale",
        "改动目录里任一文件后变 stale",
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


def check_ledger_digest() -> None:
    """工作区索引：空表、行数与长度上限、只列最近的、搁置的不列、进装配头不进前缀。"""
    root = PROBE / "ledger_digest_fixture"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    ledger = SessionLedger(session_id="", snapshot_path=root / "digest.json")

    check(ledger.digest() == "", "空登记表不给索引（一个字也不占额度）")

    ledger.add("receptor", summary="4HJO 的 EGFR 激酶域")
    ledger.add("ligandset", summary="原始库 5000 个")
    for index in range(7):
        ledger.add("artifact", summary=f"第 {index} 号产物")
    digest = ledger.digest()
    lines = digest.splitlines()
    check(len(lines) <= DIGEST_MAX_LINES, f"索引不超过 {DIGEST_MAX_LINES} 行：{len(lines)}")
    check("当前受体：receptor#1" in digest, f"当前受体在索引里：{lines[1]}")
    check("artifact#7" in digest, "最新的产物在索引里")
    check("artifact#1" not in digest, "只列最近 5 个，最早的两个不列")
    check("共 7 个" in digest, "总数说出来了——没列全时模型知道还有")
    check("ligandset#1" in digest, "配体集的 handle 也在")
    check("来自 receptor" not in digest, "索引里不重复写血缘（每个产物都挂同一个受体）")
    check(
        "artifact#" not in ledger.digest(artifact_limit=0),
        "限额为 0 时一个产物都不列（`-0` 就是 `0`，别退化成全列）",
    )

    # 长摘要截断且压成一行：换行会把一条索引拆成两行，读的人分不清下半截属于谁。
    long_entry = ledger.add("artifact", summary="很长的一段说明\n第二行" * 10)
    line = [item for item in ledger.digest().splitlines() if str(long_entry.handle) in item][0]
    check(len(line) <= DIGEST_LINE_CHARS, f"一行不超过 {DIGEST_LINE_CHARS} 字：{len(line)}")
    check("\n" not in line, "摘要里的换行被压掉了")

    # 换受体：旧条目全部搁置，索引里不该再出现它们的编号——列出来等于让模型
    # 去用一个「已经不在当前工作区」的 handle。
    other = ledger.add("receptor", summary="另一个受体")
    ledger.switch_receptor(other.handle)
    switched = ledger.digest()
    check("receptor#2" in switched, "切换之后当前受体换了")
    check("artifact#" not in switched, f"搁置的产物不再列：{switched!r}")
    check("ligandset#" not in switched, "搁置的配体集也不再列")

    # 装配：索引进的是那条易变的 user 头，`system` 前缀一个字节都不能动。
    session = Session(title="索引接线")
    session.add_message(
        Message(role="user", parts=[Part(type=PartType.TEXT, text="开始")])
    )
    prefix = "稳定前缀" * 10
    with_digest = ContextAssembler(200_000).build(
        session, prefix, digest_text="（工作区索引）当前受体：receptor#1"
    )
    without = ContextAssembler(200_000).build(session, prefix)
    check(with_digest.messages[0]["content"] == prefix, "system 那条一个字节都没变")
    check("当前受体：receptor#1" in with_digest.messages[1]["content"], "索引在 user 头里")
    check(
        with_digest.usage[Layer.TASK] > without.usage[Layer.TASK],
        "索引算进 TASK 层的用量（否则它是一段看不见的开销）",
    )

    # 进度提示：本主题够长才出现，而且**只能出现在尾部**。
    # 进 header 的话它每步都变（「已 3 步」→「已 4 步」），第 1 条消息每步都不同，
    # 整条前缀缓存作废——那正是长会话命中率掉下来的原因（§4.13 量过）。
    tick = Session(title="进度")
    tick.add_message(Message(role="user", parts=[Part(type=PartType.TEXT, text="开始")]))
    for count in (1, 2):
        tick.pending_steps.append(Step(seq=count, intent="bash", tools=["bash"]))
    short = ContextAssembler(200_000).build(tick, prefix)
    check("本主题已" not in short.messages[-1]["content"], "两步以内不提示收口")
    tick.pending_steps.append(Step(seq=3, intent="bash", tools=["bash"]))
    long_run = ContextAssembler(200_000).build(tick, prefix)
    check("本主题已 3 步" in long_run.messages[-1]["content"], "第 3 步起在尾部提示收口")
    check("close_topic" in long_run.messages[-1]["content"], "提示里点名要调哪个工具")
    check(
        long_run.messages[1]["content"] == short.messages[1]["content"],
        "提示不进 user 头：前缀一个字节都没动",
    )
    check(
        long_run.usage[Layer.TASK] > short.usage[Layer.TASK],
        "提示算进 TASK 用量（否则它是一段看不见的开销）",
    )

    # 接线：循环层从登记表现取；没装配登记表时给空串，不炸。
    config = ProviderConfig(base_url="http://127.0.0.1:1", api_key="x", model="fake")
    loop = AgentLoop(
        EventBus(),
        SilentProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        config,
        context_window=200_000,
        ledger=ledger,
    )
    check(loop._digest_text() == switched, "循环层发出去的就是登记表现算的那一份")
    bare = AgentLoop(
        EventBus(),
        SilentProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        config,
        context_window=200_000,
    )
    check(bare._digest_text() == "", "没装配登记表时是空串")

    shutil.rmtree(root, ignore_errors=True)


async def check_env_block() -> None:
    """运行环境：平台那句必须是真的，日期只到天，状态目录进易变头不进前缀。"""

    # 平台那句：原先写死成 Windows，在 Linux 上就是在教模型写错命令（F2 的根因）。
    check("这套环境是 Linux" in platform_note("linux"), "linux 上报 Linux")
    check("这套环境是 Windows" in platform_note("win32"), "win32 上报 Windows")
    check("这套环境是 macOS" in platform_note("darwin"), "darwin 上报 macOS")
    check(
        "用 dir 而不是 ls" in platform_note("win32"),
        "Windows 上仍然教 dir——那正是当初写这句话的原因",
    )
    check(
        "用 dir 而不是 ls" not in platform_note("linux"),
        "Linux 上不许教 dir：模型照着写，失败两次才改回 ls",
    )

    # 日期与状态目录只在易变头里；平台在前缀里（它是常量，进前缀不伤缓存）。
    # 那三条在 `prefix` 组里断言，这里只验环境块自己的形状。
    session = Session(title="运行环境")
    prefix = stable_prefix(Workspace(session.id, root=WORKSPACE_FIXTURE), session)

    # 固定一个 23:59 的时刻：只到天，就不该出现时分。
    fixed = time.mktime((2026, 10, 26, 23, 59, 0, 0, 0, -1))
    block = environment_block(Path("/work"), Path("/state"), now=fixed)
    check("2026-10-26" in block, f"日期按当天给：{block.splitlines()[0]}")
    check("/work" in block, "工作目录在环境块里")
    check("/state" in block, "状态目录在环境块里")
    check("23:59" not in block, "只写日期不写时分——带时间的话每一步都和上一步不同")
    check(
        environment_block(None, None, now=fixed).count("\n") == 0,
        "两个路径都不给时只剩一行日期",
    )

    # 装配：环境排在 user 头最前，`system` 那条一个字节不动。
    built = ContextAssembler(200_000).build(
        session, prefix, env_text="（运行环境：今天 2026-10-26）"
    )
    plain = ContextAssembler(200_000).build(session, prefix)
    check(built.messages[0]["content"] == prefix, "system 那条一个字节都没变")
    check(
        built.messages[1]["content"].startswith("（运行环境：今天"),
        f"环境排在 user 头最前：{built.messages[1]['content'][:20]}",
    )
    check(built.usage[Layer.TASK] > plain.usage[Layer.TASK], "环境算进 TASK 层的用量")

    # 接线：循环层拿得到工作目录与状态目录，应用装配时把状态目录交进去。
    config = ProviderConfig(base_url="http://127.0.0.1:1", api_key="x", model="fake")
    loop = AgentLoop(
        EventBus(),
        SilentProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        config,
        context_window=200_000,
        workspace_root=Path("/work"),
        state_dir=Path("/state"),
    )
    text = loop._env_text()
    check("/work" in text and "/state" in text, "循环层的环境块带上了两个目录")
    bare = AgentLoop(
        EventBus(),
        SilentProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        config,
        context_window=200_000,
    )
    check("/work" not in bare._env_text(), "没给目录时不编一个出来")

    root = PROBE / "env_block_app"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state.toml"
    app = VSAgentApp(root=ROOT, state_path=state)
    wired = app.host.loop_for(app.session_manager.current.id)
    check(
        wired.state_dir == app.session_manager.state_path.parent,
        f"应用把状态目录交给了循环层：{wired.state_dir}",
    )
    shutil.rmtree(root, ignore_errors=True)


def check_focus() -> None:
    """FOCUS 复述：只在有任务状态时出现、只插在最后一条用户消息前、三样都带上。"""
    prefix = "稳定前缀" * 10

    def session_with(state: bool, rounds: int = 1) -> Session:
        session = Session(title="复述")
        if state:
            session.task_state.apply(
                goal="对 EGFR 做一轮虚拟筛选",
                add_constraints=["受体必须用 4HJO", "打分只用 Vina"],
            )
            session.task_state.apply(todo=["准备受体", "跑一遍对接"])
        for index in range(rounds):
            user = session.add_message(Message(role="user"))
            user.add_part(Part(type=PartType.TEXT, text=f"第 {index + 1} 轮：继续"))
            assistant = session.add_message(Message(role="assistant"))
            assistant.add_part(
                Part(
                    type=PartType.TOOL,
                    tool_name="bash",
                    tool_status=ToolStatus.COMPLETED,
                    tool_input={"command": f"cmd{index}"},
                    tool_output="ok",
                )
            )
        return session

    def build(session: Session):
        return ContextAssembler(200_000).build(session, prefix)

    empty = build(session_with(False))
    check(
        "当前任务复述" not in "\n".join(str(item.get("content") or "") for item in empty.messages),
        "没有任务状态时不凭空造一段复述",
    )

    filled = build(session_with(True))
    texts = [str(item.get("content") or "") for item in filled.messages]
    check(
        any("当前任务复述" in text for text in texts),
        "有任务状态时插入复述",
    )
    focus_at = next(index for index, text in enumerate(texts) if "当前任务复述" in text)
    check(
        texts[focus_at + 1].startswith("第 1 轮"),
        f"复述紧挨在最后一条用户消息前面：{texts[focus_at + 1][:10]}",
    )
    check(
        filled.messages[focus_at]["role"] == "user",
        "复述是一条 user 消息（不是并进 system）",
    )
    check("目标：对 EGFR 做一轮虚拟筛选" in texts[focus_at], "复述里有目标")
    check("受体必须用 4HJO" in texts[focus_at], "复述里有用户约束")
    check("下一步：准备受体" in texts[focus_at], "复述里有下一步")
    check("已完成" not in texts[focus_at], "不带已完成那一整段（那是 TASK 层的事）")
    check(
        estimate_tokens(texts[focus_at]) <= FOCUS_LIMIT,
        f"复述不超过 {FOCUS_LIMIT} token：{estimate_tokens(texts[focus_at])}",
    )
    check(filled.messages[0]["content"] == prefix, "system 那条一个字节都没变")

    # 多轮：只插一次，而且插在**最后**那条用户消息前。
    many = build(session_with(True, rounds=3))
    bodies = [str(item.get("content") or "") for item in many.messages]
    check(
        sum(1 for text in bodies if "当前任务复述" in text) == 1,
        "多轮会话里只插一条复述",
    )
    focus_at = next(index for index, text in enumerate(bodies) if "当前任务复述" in text)
    check(bodies[focus_at + 1].startswith("第 3 轮"), "复述贴着最后一条用户消息")

    # 约束多了按顺序截断，并说明还有几条——不能悄悄少几条。
    crowded = Session(title="约束多")
    crowded.task_state.apply(
        goal="目标", add_constraints=[f"约束{index}" for index in range(1, 6)]
    )
    user = crowded.add_message(Message(role="user"))
    user.add_part(Part(type=PartType.TEXT, text="继续"))
    body = "\n".join(str(item.get("content") or "") for item in build(crowded).messages)
    check(f"约束{FOCUS_CONSTRAINTS}" in body, "前几条约束都在")
    check(
        f"约束{FOCUS_CONSTRAINTS + 1}" not in body,
        "超出条数的约束不铺开",
    )
    check("另有 2 条" in body, "少掉的约束写明「另有几条」")

    # 没有待办时用阻塞顶上，别留一段空的。
    blocked = Session(title="阻塞")
    blocked.task_state.apply(goal="目标", blockers=["缺受体结构"])
    user = blocked.add_message(Message(role="user"))
    user.add_part(Part(type=PartType.TEXT, text="继续"))
    body = "\n".join(str(item.get("content") or "") for item in build(blocked).messages)
    check("当前阻塞：缺受体结构" in body, "没有待办时复述阻塞")

    check(
        filled.usage[Layer.TASK] > empty.usage[Layer.TASK],
        "复述算进 TASK 层的用量",
    )


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
            ROOT / "vsagent" / "core" / "agent" / "domain" / "docking.py"
        ).read_text(encoding="utf-8")
        check("create_subprocess_shell" not in source, "对接不拼 shell")

        # ---- 七之二、力场：vina / vinardo / ad4 ----
        check(
            (tool.spec.parameters.get("properties") or {}).get("scoring", {}).get("enum")
            == ["vina", "vinardo", "ad4"],
            "scoring 是参数、枚举写进 schema（写错在参数校验那一层就被挡，不必等到跑引擎）",
        )
        vinardo = docking_module.build_command(
            "/fake/vina",
            Path("r.pdbqt"),
            Path("l.pdbqt"),
            Path("o.pdbqt"),
            box,
            docking_module.DockParams(scoring="vinardo"),
        )
        check(
            vinardo[vinardo.index("--scoring") + 1] == "vinardo",
            f"vinardo 只多一个 --scoring：{vinardo[:6]}",
        )
        check("--receptor" in vinardo, "vinardo 仍要受体（它不需要图）")

        ad4 = docking_module.build_command(
            "/fake/vina",
            Path("r.pdbqt"),
            Path("l.pdbqt"),
            Path("o.pdbqt"),
            box,
            docking_module.DockParams(scoring="ad4"),
            maps="/grids/x/receptor",
        )
        check(
            ad4[ad4.index("--maps") + 1] == "/grids/x/receptor",
            f"ad4 走 --maps：{ad4[:8]}",
        )
        check(
            "--receptor" not in ad4 and "--center_x" not in ad4 and "--size_x" not in ad4,
            "ad4 不能给受体或盒子：盒子已经烘进图里，再给一遍会被当成参数冲突",
        )
        check(ad4[ad4.index("--scoring") + 1] == "ad4", "力场显式传给引擎")

        try:
            docking_module._scoring_argument({"scoring": "vina2"})
            check(False, "未知力场应当拒绝")
        except ToolError as error:
            check(
                "vinardo" in str(error) and "不能互相比较" in str(error),
                f"写错时列全三个、并说清分数不可比：{error}",
            )

        # PDBQT 的类型列决定 autogrid 要算哪几张图：少一张图 autogrid 会直接报错
        typed = work / "typed.pdbqt"
        typed.write_text(
            "ATOM      1  C   LIG     1       0.000   0.000   0.000  1.00  0.00    +0.100 C \n"
            "ATOM      2  OA  LIG     1       1.000   0.000   0.000  1.00  0.00    -0.300 OA\n"
            "ATOM      3  C   LIG     1       2.000   0.000   0.000  1.00  0.00    +0.050 C \n",
            encoding="utf-8",
        )
        check(
            docking_module.pdbqt_types(typed) == ["C", "OA"],
            f"类型列读得出来、去重且保序：{docking_module.pdbqt_types(typed)}",
        )

        # ---- 七之三、ad4：图的类型集合、盒子上限、缺 autogrid4 ----
        grid_calls: list[dict[str, Any]] = []
        original_autogrid = docking_module.resolve_autogrid
        original_grids = docking_module.prepare_grids
        original_capture = docking_module.run_vina
        captured_argv: list[list[str]] = []

        async def capture_run(argv, *, cwd, timeout, log_path):  # type: ignore[no-untyped-def]
            captured_argv.append(list(argv))
            return await original_capture(argv, cwd=cwd, timeout=timeout, log_path=log_path)

        async def fake_prepare_grids(**kwargs):  # type: ignore[no-untyped-def]
            grid_calls.append(kwargs)
            return work / "grids" / "hash" / "receptor", False

        try:
            docking_module.resolve_autogrid = lambda: "/fake/autogrid4"
            docking_module.prepare_grids = fake_prepare_grids
            docking_module.run_vina = capture_run
            # 配体的 PDBQT 要有类型列，否则算不出 ligand_types
            (ligand_dir / "ligands" / "A.pdbqt").write_text(
                "ATOM      1  C   LIG     1       0.000   0.000   0.000  1.00  0.00    +0.100 C \n"
                "ATOM      2  OA  LIG     1       1.000   0.000   0.000  1.00  0.00    -0.300 OA\n",
                encoding="utf-8",
            )
            ad4_result = await tool.execute(
                {
                    "receptor": str(receptor.handle),
                    "ligands": str(ligand_set.handle),
                    "scoring": "ad4",
                    "limit": 1,
                },
                context=ledger,
            )
            check(not ad4_result.failed, f"ad4 能跑通（图打桩）：{ad4_result.output}")
            check(
                ad4_result.meta.get("scoring") == "ad4",
                f"meta 里带力场：{ad4_result.meta}",
            )
            check(
                grid_calls and grid_calls[0]["ligand_types"] == ["C", "OA"],
                f"ligand_types 取自这一批配体的类型列：{grid_calls[:1]}",
            )
            check(
                "AutoDock4 力场" in ad4_result.output,
                f"返回里说清是哪一种力场（ad4 不是另一个引擎）：{ad4_result.output[:80]}",
            )
            check(
                "不能" in ad4_result.output and "比较" in ad4_result.output,
                "返回里要写明三种力场的分数不能互相比较",
            )

            ad4_manifest = json.loads(
                (Path(ad4_result.output.split("产物目录：")[1].splitlines()[0]) / "manifest.json")
                .read_text(encoding="utf-8")
            )
            check(
                ad4_manifest["engine"].get("scoring") == "ad4"
                and ad4_manifest["engine"].get("maps"),
                f"manifest 的 engine 记了力场与图目录：{ad4_manifest['engine']}",
            )
            check(
                any(
                    "--maps" in argv and "--receptor" not in argv for argv in captured_argv
                ),
                f"真跑时的命令走 --maps：{captured_argv[:1]}",
            )

            # 盒子超 40 Å 直接拒绝：图的代价是三次方
            (receptor_dir / "big-box.txt").write_text(
                "center_x = 0\ncenter_y = 0\ncenter_z = 0\n"
                "size_x = 50.0\nsize_y = 20.0\nsize_z = 20.0\n",
                encoding="utf-8",
            )
            big = ledger.add(
                "receptor",
                path=receptor_dir,
                summary="大盒子受体",
                fingerprint=fingerprint_path(receptor_dir),
                meta={"stage": "prepared", "pdbqt_file": "receptor.pdbqt", "box_file": "big-box.txt"},
            )
            oversized = await tool.execute(
                {"receptor": str(big.handle), "ligands": str(ligand_set.handle), "scoring": "ad4"},
                context=ledger,
            )
            check(
                oversized.failed and "40" in oversized.output and "三次方" in oversized.output,
                f"ad4 的盒子超限要拒绝并说清代价：{oversized.output}",
            )

            # 缺 autogrid4：只影响 ad4，vina / vinardo 照常
            docking_module.resolve_autogrid = lambda: (_ for _ in ()).throw(
                ToolError(docking_module.AUTOGRID_INSTALL_HINT)
            )
            missing_grid = await tool.execute(
                {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle), "scoring": "ad4"},
                context=ledger,
            )
            check(
                missing_grid.failed
                and "autogrid4" in missing_grid.output
                and "vina" in missing_grid.output,
                f"缺 autogrid4 时给安装指引，并指出还能用哪两种力场：{missing_grid.output}",
            )
        finally:
            docking_module.resolve_autogrid = original_autogrid
            docking_module.prepare_grids = original_grids
            docking_module.run_vina = original_capture

        # ---- 七之四、prepare_grids 里的 GPF 是真的生成的（只有 autogrid4 打桩）----
        # meeko 的 `gridbox.get_gpf_string` 换签名的话，这一段会当场红；不测就只能等
        # 真装机器的用户撞上。
        original_autogrid_run = docking_module.run_autogrid
        original_grid_root = docking_module.grids_dir

        async def fake_autogrid(argv, *, cwd, timeout, log_path):  # type: ignore[no-untyped-def]
            for name in ("A", "C", "OA"):
                (Path(cwd) / f"receptor.{name}.map").write_text("", encoding="utf-8")
            (Path(cwd) / docking_module.GRID_MARKER).write_text("", encoding="utf-8")
            log_path.write_text("autogrid done", encoding="utf-8")
            return docking_module.VinaRun(
                argv=tuple(argv),
                returncode=0,
                output="",
                timed_out=False,
                log_path=log_path,
            )

        try:
            docking_module.grids_dir = lambda: work / "real-grids"
            docking_module.run_autogrid = fake_autogrid
            grid_key = {"receptor": "fp", "box": [1, 2, 3, 20, 21, 22]}
            prefix, grid_reused = await docking_module.prepare_grids(
                autogrid="/fake/autogrid4",
                receptor_pdbqt=receptor_dir / "receptor.pdbqt",
                box=box,
                ligand_types=["A", "C", "OA"],
                cache_key=grid_key,
            )
            gpf_text = (prefix.parent / "receptor.gpf").read_text(encoding="utf-8")
            check(
                "ligand_types A C OA" in gpf_text,
                f"GPF 的 ligand_types 就是 autogrid 要算的那几张图：{gpf_text.splitlines()[:6]}",
            )
            check(
                "gridcenter 1.000 2.000 3.000" in gpf_text,
                "盒子中心写进 GPF",
            )
            check(
                "receptor receptor.pdbqt" in gpf_text,
                "GPF 里是受体文件名（autogrid 在 cwd 里找它）",
            )
            check(
                (prefix.parent / docking_module.GRID_PARAM_FILE).is_file(),
                "B/Si 参数文件与 GPF 同目录（否则 autogrid 找不到参数文件）",
            )
            check(not grid_reused, "第一次是新算的")
            check(
                (prefix.parent / "receptor.pdbqt").is_file(),
                "受体 PDBQT 被复制进了图目录",
            )

            _, second_reused = await docking_module.prepare_grids(
                autogrid="/fake/autogrid4",
                receptor_pdbqt=receptor_dir / "receptor.pdbqt",
                box=box,
                ligand_types=["A", "C", "OA"],
                cache_key=grid_key,
            )
            check(second_reused, "同一套参数直接复用，不再跑一次 autogrid")

            # 缺一张图要报出来（少一张图那一类原子就没有势场，分数是错的）
            async def one_map_missing(argv, *, cwd, timeout, log_path):  # type: ignore[no-untyped-def]
                for name in ("A", "C"):
                    (Path(cwd) / f"receptor.{name}.map").write_text("", encoding="utf-8")
                (Path(cwd) / docking_module.GRID_MARKER).write_text("", encoding="utf-8")
                log_path.write_text("autogrid done", encoding="utf-8")
                return docking_module.VinaRun(
                    argv=tuple(argv), returncode=0, output="", timed_out=False, log_path=log_path
                )

            docking_module.run_autogrid = one_map_missing
            try:
                await docking_module.prepare_grids(
                    autogrid="/fake/autogrid4",
                    receptor_pdbqt=receptor_dir / "receptor.pdbqt",
                    box=box,
                    ligand_types=["A", "C", "OA"],
                    cache_key={"receptor": "fp2"},
                )
                check(False, "缺图应当失败")
            except ToolError as error:
                check("OA" in str(error), f"缺哪张图就点哪张：{error}")
        finally:
            docking_module.grids_dir = original_grid_root
            docking_module.run_autogrid = original_autogrid_run

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


async def check_affinity() -> None:
    """亲和力打分：环境定位、handle 分档、批量计数、产物与复用、失败分档。"""
    work = PROBE / "affinity_tool"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_dir = affinity_module.scorings_dir
    original_python = affinity_module.dta_python
    original_root = affinity_module.dta_root
    original_run = affinity_module.run_batch
    runs: list[tuple[str, ...]] = []
    behaviour = {"mode": "ok"}

    async def fake_run_batch(argv, *, cwd, timeout, log_path):
        runs.append(tuple(argv))
        output_path = Path(argv[argv.index("--output") + 1])
        tasks = json.loads(
            Path(argv[argv.index("--tasks") + 1]).read_text(encoding="utf-8")
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake engine log\n", encoding="utf-8")

        mode = behaviour["mode"]
        if mode == "timeout":
            return affinity_module.BatchRun(tuple(argv), -9, "timed out", True, log_path)
        if mode == "exit":
            return affinity_module.BatchRun(tuple(argv), 3, "boom: no checkpoint", False, log_path)

        rows = []
        lines = ["口袋：从 receptor.pdb 截出 57 个残基（半径 10 A）"]
        for index, task in enumerate(tasks):
            if mode == "all_fail" or task["id"].startswith("BAD"):
                lines.append(f"ERROR {task['id']}: ValueError: 坏的配体")
                continue
            pk = 8.0 - index
            rows.append((task["id"], "CCO", f"{pk:.4f}", f"{10 ** (-pk) * 1e9:.1f}", "Ki", "Ki"))
        header = ("ligand_name", "smiles", "predicted_pK",
                  "predicted_value_nM", "predicted_unit", "condition_unit")
        text = "\n".join(",".join(row) for row in [header, *rows]) + "\n"
        output_path.write_text(text, encoding="utf-8")
        lines.append("SUMMARY " + json.dumps(
            {"input": len(tasks), "scored": len(rows), "failed": len(tasks) - len(rows)}))
        return affinity_module.BatchRun(
            tuple(argv), 0, "\n".join(lines), False, log_path)

    def make_ligandset(name: str, ids: list[str]) -> Path:
        directory = work / "ligands" / name
        (directory / "ligands").mkdir(parents=True, exist_ok=True)
        records = []
        for index, identifier in enumerate(ids, start=1):
            relative = f"ligands/{identifier}.pdbqt"
            (directory / relative).write_text(f"REMARK {identifier}\n", encoding="utf-8")
            records.append({
                "id": identifier,
                "name": identifier.lower(),
                "smiles": "CCO",
                "status": "ok",
                "pdbqt": relative,
            })
        (directory / "manifest.json").write_text(
            json.dumps({"kind": "ligandset", "stage": "prepared",
                        "records": records, "counts": {}}),
            encoding="utf-8",
        )
        return directory

    try:
        affinity_module.scorings_dir = lambda: work / "scorings"
        affinity_module.dta_python = lambda: "/fake/python"
        affinity_module.dta_root = lambda: work / "DTA"
        affinity_module.run_batch = fake_run_batch

        raw_tool = affinity_module.ScoreAffinityTool()
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        # 这一轮起 model 是必填：先造一份模型 artifact，下面所有打分都带上它。
        model_dir = work / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        model_config = model_dir / "config.infer.yaml"
        model_pocket = model_dir / "pocket.pdb"
        model_config.write_text("MODEL\n", encoding="utf-8")
        model_pocket.write_text("ATOM\n", encoding="utf-8")
        model = ledger.add(
            "artifact",
            path=model_dir,
            summary="fine-tuned model",
            fingerprint=fingerprint_path(model_dir),
            meta={
                "kind": "model",
                "config": str(model_config),
                "pocket": str(model_pocket),
            },
        )

        class ScoreWithModel:
            """测试里的便捷外壳：每次打分自动补上 model，省得十几处重复。"""

            def __init__(self, inner):
                self.inner = inner

            @property
            def spec(self):
                return self.inner.spec

            async def execute(self, arguments, context=None):
                return await self.inner.execute(
                    {"model": str(model.handle), **arguments}, context=context
                )

        scored = ScoreWithModel(raw_tool)
        tool = scored

        receptor_dir = work / "receptor"
        receptor_dir.mkdir(parents=True, exist_ok=True)
        (receptor_dir / "receptor.pdb").write_text("ATOM\n", encoding="utf-8")
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
            meta={"stage": "prepared", "pdb_file": "receptor.pdb", "box_file": "box.txt"},
        )
        ligand_dir = make_ligandset("three", ["A", "BAD1", "B"])
        ligand_set = ledger.add(
            "ligandset",
            path=ligand_dir,
            summary="prepared ligands",
            fingerprint=fingerprint_path(ligand_dir),
            meta={"stage": "prepared"},
            link_current_receptor=False,
        )

        # ---- 一、整批：2 成功 1 失败 ----
        result = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(not result.failed, f"打分成功：{result.output}")
        check(
            result.meta.get("scored") == 2
            and result.meta.get("failed") == 1
            and result.meta.get("best_pk") == 8.0
            and result.meta.get("best_id") == "A",
            f"计数与最佳分数正确（pK 取最大）：{result.meta}",
        )
        check("结论：" in result.output, f"首行以「结论：」开头，压缩后仍进证据：{result.output}")
        check("越高越好" in result.output, "输出写清方向（与 dock 相反）")

        handle = result.meta["handle"]
        entry = ledger.get(handle)
        check(
            isinstance(entry.handle, ArtifactHandle)
            and entry.meta.get("kind") == "affinity"
            and entry.meta.get("stage") == "affinity",
            "登记成 artifact + kind=affinity",
        )
        check(
            {str(item) for item in entry.parents}
            == {str(receptor.handle), str(ligand_set.handle)},
            f"父项挂受体与配体集：{[str(item) for item in entry.parents]}",
        )
        manifest = json.loads((entry.path / "manifest.json").read_text(encoding="utf-8"))
        check(
            [item["id"] for item in manifest["results"]] == ["A", "B"],
            f"结果按 pK 降序：{[item['id'] for item in manifest['results']]}",
        )
        check(
            manifest["results"][0]["pk"] == 8.0
            and abs(manifest["results"][0]["nM"] - 10 ** (-8.0) * 1e9) < 1e-6,
            f"pK 与 nM 由代码换算：{manifest['results'][0]}",
        )
        check(
            manifest["counts"]["input"] == 3
            and manifest["counts"]["scored"] == 2
            and manifest["counts"]["failed"] == 1,
            f"manifest 计数：{manifest['counts']}",
        )
        check(
            manifest["pocket"]["residues"] == 57
            and manifest["pocket"]["radius"] == 10.0,
            f"口袋参数进 manifest：{manifest['pocket']}",
        )
        check(
            manifest["failures"] and manifest["failures"][0]["id"] == "BAD1",
            f"单条失败进 manifest：{manifest['failures']}",
        )
        check(
            (entry.path / "scores.csv").is_file()
            and (entry.path / "tasks.json").is_file()
            and (entry.path / "params.json").is_file()
            and (entry.path / "logs" / "run.log").is_file(),
            "产物目录有 scores / tasks / params / 日志",
        )

        # ---- 二、同参数复用：不重跑模型 ----
        before = len(runs)
        again = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(
            not again.failed
            and again.meta.get("handle") == handle
            and again.meta.get("reused") is True,
            f"同参数复用同一 artifact：{again.meta}",
        )
        check(len(runs) == before, "复用不重跑模型")
        check("复用已有结果" in again.output, f"输出说明复用：{again.output}")

        # ---- 三、命令构造 ----
        argv = runs[-1]
        check(argv[0] == "/fake/python", "用解析到的解释器跑")
        check(argv[1].endswith("virtual_screen.py"), f"跑 DTA 的批量入口：{argv[1]}")
        for flag in ("--pocket", "--tasks", "--output", "--aff_unit", "--device"):
            check(flag in argv, f"命令里有 {flag}")
        check(
            all(flag not in argv for flag in ("--receptor", "--pocket-center", "--pocket-radius")),
            "给了 model 就用模型训练时的口袋，不再从 box.txt 现截",
        )
        pocket_path = argv[argv.index("--pocket") + 1]
        check(
            pocket_path == str(model_pocket),
            f"--pocket 指向模型自己的口袋：{pocket_path}",
        )

        # ---- 四、limit ----
        limited = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle),
             "limit": 1},
            context=ledger,
        )
        check(
            not limited.failed
            and limited.meta.get("input") == 1
            and limited.meta.get("handle") != handle,
            f"limit 只打 1 个，且换了产物目录：{limited.meta}",
        )

        # ---- 五、全部失败：仍然失败、但产物留下 ----
        behaviour["mode"] = "all_fail"
        fail_dir = make_ligandset("bad", ["BAD2"])
        fail_set = ledger.add(
            "ligandset",
            path=fail_dir,
            summary="all bad",
            fingerprint=fingerprint_path(fail_dir),
            meta={"stage": "prepared"},
            link_current_receptor=False,
        )
        failed = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(fail_set.handle)},
            context=ledger,
        )
        check(
            failed.failed and "没有配体打分成功" in failed.output,
            f"全失败时失败并说明：{failed.output}",
        )
        check(
            failed.meta.get("scored") == 0 and "日志" in failed.output,
            f"全失败也留日志路径：{failed.output}",
        )
        behaviour["mode"] = "ok"

        # ---- 六、整批超时 / 非零退出 ----
        behaviour["mode"] = "timeout"
        timed = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle),
             "limit": 2},
            context=ledger,
        )
        check(timed.failed and "超时" in timed.output, f"超时被识别：{timed.output}")
        behaviour["mode"] = "exit"
        exited = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle),
             "limit": 2},
            context=ledger,
        )
        check(
            exited.failed and "退出码 3" in exited.output,
            f"非零退出被识别：{exited.output}",
        )
        behaviour["mode"] = "ok"

        # ---- 七、handle 与阶段分档 ----
        raw_ligand = ledger.add(
            "ligandset",
            path=make_ligandset("raw", ["A"]),
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
            path=receptor_dir,
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

        wrong_kind = await tool.execute(
            {"receptor": str(ligand_set.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(
            wrong_kind.failed and "receptor" in wrong_kind.output,
            f"handle 类型拿错时说清参数：{wrong_kind.output}",
        )
        unknown = await tool.execute(
            {"receptor": "receptor#99", "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(unknown.failed and "receptor#99" in unknown.output,
            f"不存在的 handle 被拒：{unknown.output}")

        # ---- 八、docking 产物作为输入 ----
        docking_dir = work / "docking"
        (docking_dir / "poses").mkdir(parents=True, exist_ok=True)
        (docking_dir / "poses" / "A_out.pdbqt").write_text("REMARK A\n", encoding="utf-8")
        (docking_dir / "poses" / "B_out.pdbqt").write_text("REMARK B\n", encoding="utf-8")
        (docking_dir / "manifest.json").write_text(
            json.dumps({
                "kind": "docking", "stage": "docking",
                "results": [
                    {"id": "A", "status": "ok", "pose_pdbqt": "poses/A_out.pdbqt"},
                    {"id": "B", "status": "failed", "pose_pdbqt": "poses/B_out.pdbqt"},
                ],
            }),
            encoding="utf-8",
        )
        docking_artifact = ledger.add(
            "artifact",
            path=docking_dir,
            summary="docking",
            fingerprint=fingerprint_path(docking_dir),
            meta={"kind": "docking", "stage": "docking"},
            link_current_receptor=False,
        )
        rescored = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(docking_artifact.handle)},
            context=ledger,
        )
        check(
            not rescored.failed and rescored.meta.get("input") == 1,
            f"对接产物只取 status=ok 的 pose：{rescored.meta}",
        )
        rescore_manifest = json.loads(
            (Path(ledger.get(rescored.meta["handle"]).path) / "manifest.json")
            .read_text(encoding="utf-8")
        )
        check(
            rescore_manifest["ligands"]["source"] == "docking"
            and [item["id"] for item in rescore_manifest["results"]] == ["A"],
            f"来源记成 docking：{rescore_manifest['ligands']}",
        )

        # ---- 九、缺环境：失败并给安装指引 ----
        def no_python() -> str:
            raise ToolError(
                f"没有找到 DTA 的解释器。用 {affinity_module.PYTHON_ENV} 指定。"
                f"{affinity_module.INSTALL_HINT}")

        affinity_module.dta_python = no_python
        missing = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(
            missing.failed
            and affinity_module.PYTHON_ENV in missing.output
            and "DTA/README.md" in missing.output,
            f"缺解释器时给安装指引：{missing.output}",
        )
        affinity_module.dta_python = lambda: "/fake/python"

        # ---- 十、参数与装配 ----
        properties = tool.spec.parameters.get("properties") or {}
        check(
            tool.spec.parameters.get("required") == ["receptor", "ligands", "model"],
            "receptor、ligands、model 都是必填",
        )
        check("ledger" not in properties, "参数模式里不列 ledger")
        check(
            tool.spec.parameters.get("additionalProperties") is False,
            "不认识的参数直接拒绝",
        )
        check("越高越好" in tool.spec.description, "描述第一段写清分数方向")
        check("torch" in tool.spec.description, "描述里说明要 DTA 的独立环境")

        empty = await tool.execute({}, context=ledger)
        check(
            empty.failed and "缺少参数 receptor" in empty.output,
            f"空调用被拦下：{empty.output}",
        )
        missing_model = await raw_tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle)},
            context=ledger,
        )
        check(
            missing_model.failed and "model" in missing_model.output,
            f"零样本被拦下：{missing_model.output}",
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
            ("pocket_radius", 1.0, "3 到 20"),
            ("limit", 0, "1 到 500"),
            ("timeout", 1, "60 到 7200"),
        ):
            bad = await tool.execute(
                {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle),
                 name: value},
                context=ledger,
            )
            check(
                bad.failed and expected in bad.output,
                f"{name} 越界时给出范围：{bad.output}",
            )

        from vsagent.core.agent.permission import DEFAULT_ASK, PLAN_DENY

        check("score_affinity" in DEFAULT_ASK, "默认策略下要问一次（它会跑外部进程）")
        check("score_affinity" in PLAN_DENY, "Plan 方式下一律拒绝")

        source = (
            ROOT / "vsagent" / "core" / "agent" / "domain" / "affinity.py"
        ).read_text(encoding="utf-8")
        check("create_subprocess_shell" not in source, "打分不拼 shell")
        check("import torch" not in source, "主仓不 import torch（否则启动就炸）")
    finally:
        affinity_module.scorings_dir = original_dir
        affinity_module.dta_python = original_python
        affinity_module.dta_root = original_root
        affinity_module.run_batch = original_run
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


def _cif_fixture() -> str:
    """一份能过 gemmi 的最小 mmCIF：一条链、五个原子、带条目编号。

    没有晶体学与实验方法字段是有意的——转换之后 `parse_pdb` 照样要能给出链清单，
    而这类字段缺失正是「只有 mmCIF 的超大条目」的常态。
    """
    return """data_6VXX
_entry.id 6VXX
_struct.title 'TEST COMPLEX'
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.pdbx_PDB_ins_code
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.auth_seq_id
_atom_site.auth_asym_id
_atom_site.pdbx_PDB_model_num
ATOM 1 N N   . ALA A 1 1 ? 0.000 0.000 0.000 1.00 20.00 1 A 1
ATOM 2 C CA  . ALA A 1 1 ? 1.458 0.000 0.000 1.00 20.00 1 A 1
ATOM 3 C C   . ALA A 1 1 ? 2.009 1.420 0.000 1.00 20.00 1 A 1
ATOM 4 O O   . ALA A 1 1 ? 3.200 1.500 0.000 1.00 20.00 1 A 1
ATOM 5 C CB  . ALA A 1 1 ? 2.000 -0.770 1.200 1.00 20.00 1 A 1
"""


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

    # ---- 缓存只有一套：这一条是合并的判据，不是风格 ----
    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import rsearch as rsearch_module

    for gone in ("search_cache_dir", "_cache_path", "_read_cache", "_write_cache"):
        check(
            not hasattr(complexes_module, gone),
            f"complexes 不再自带第二份搜索缓存（{gone} 已经收掉）",
        )
    check(
        not hasattr(complexes_module, "SEARCH_CACHE_TTL"),
        "时效只有 rsearch 那一处定义，complexes 不再抄一份",
    )
    # 判「转发」要看**去掉 docstring 之后**的源码：文档里本来就写着 `rsearch._ids_from`，
    # 不剥掉的话，抄回一份实现也照样能命中这个字符串（实测：注入一次是绿的）。
    ids_source = inspect.getsource(complexes_module._ids_from).split('"""')[-1]
    check(
        "rsearch._ids_from" in ids_source
        and complexes_module._ids_from({"result_set": [{"identifier": "1ABC"}]}) == ["1ABC"],
        f"解 id 转给 rsearch 那一份（转发，不是第二个实现）：{ids_source.strip()[:60]}",
    )

    tmp = PROBE / "rcsb_search_merged"
    shutil.rmtree(tmp, ignore_errors=True)
    original_search_dir = rsearch_module.search_dir
    original_post = http_module.post_json
    rsearch_module.search_dir = lambda: tmp
    posts: list[dict[str, Any]] = []

    def fake_post(url, payload, **kwargs):
        posts.append(payload)
        return {"result_set": [{"identifier": "1IEP"}], "total_count": 1}

    try:
        http_module.post_json = fake_post
        ids, _body = complexes_module.list_complexes("P00519")
        check(ids == ["1IEP"], f"合并后照样搜得出条目：{ids}")
        check(len(posts) == 1, f"第一次真的发了请求：{len(posts)}")
        files = sorted(p.name for p in tmp.glob("*.json"))
        check(
            len(files) == 1 and files[0].startswith("search-"),
            f"缓存落在 rsearch 的目录里，一份：{files}",
        )

        # 另一条路（`search_pdb` 用的 `search_entries`）拿着**同一条查询**再来一次：
        # 不许再发请求。这条断言就是「两套缓存已经并成一套」的行为证据——
        # 目录合并但键算法不同的话，这里会变成 2。
        complexes_module.list_complexes("P00519")
        rsearch_module.search_entries(complexes_module._accession_query("P00519"))
        check(len(posts) == 1, f"同一条查询换条路走也命中同一份缓存：{len(posts)} 次请求")

        # 零命中也要缓存：同一个 204 不该一天里问很多遍。
        def empty_post(url, payload, **kwargs):
            posts.append(payload)
            raise http_module.EmptyResult("接口正常应答，但结果为空（HTTP 204）")

        http_module.post_json = empty_post
        empty_ids, empty_body = complexes_module.list_complexes("Q99999")
        check(empty_ids == [], f"零命中给空表不抛：{empty_ids}")
        check(
            empty_body == {"result_set": [], "total_count": 0},
            f"空响应归一成与真实响应同形的一份：{empty_body}",
        )
        before = len(posts)
        again_ids, _ = complexes_module.list_complexes("Q99999")
        check(
            again_ids == [] and len(posts) == before,
            f"零命中进缓存，第二次不再打接口：{len(posts) - before} 次请求",
        )
    finally:
        http_module.post_json = original_post
        rsearch_module.search_dir = original_search_dir
        shutil.rmtree(tmp, ignore_errors=True)

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


async def check_pocket_tools() -> None:
    """口袋工具：四层装配、登记形状、三路互斥、盒子进受体。

    全程离线：只走「这份结构自带的共晶配体」那一层，复合物与预测只验**缺参数时
    报什么**，不真的联网。准备受体那一段用假的参数化函数替掉 Meeko——替掉的
    只有模板匹配，文本编辑、算盒子、产物目录、写盘、登记都是真的。
    """
    work = PROBE / "pocket_tools"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_dir = pockets_module.pockets_dir
    original_prepared = receptor_module.prepared_receptor_dir
    original_parameterize = receptor_module.parameterize
    original_probe = chemistry_module._probe_deps

    pockets_module.pockets_dir = lambda: work / "pockets"
    receptor_module.prepared_receptor_dir = lambda: work / "receptors"

    def fake_parameterize(receptor_text: str, *, relax: bool) -> object:
        lines = [
            line
            for line in receptor_text.splitlines()
            if line[:6].strip() in ("ATOM", "HETATM")
        ]
        return receptor_module.Parameterization(
            pdbqt="".join(
                f"ATOM  {index:>5}  CA  ALA A   1\n"
                for index in range(1, len(lines) + 1)
            ),
            atoms=len(lines),
            monomers=len(lines),
            ignored=(),
            residues_with_added_h=len(lines),
            residues_with_excess_h=(),
            excess_bonds=(),
            chosen_by_fewest_missing_h=(),
        )

    receptor_module.parameterize = fake_parameterize
    chemistry_module._probe_deps = lambda: ()

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        text = IEP_FIXTURE.read_text(encoding="utf-8")
        raw = ledger.add(
            "receptor",
            path=IEP_FIXTURE,
            summary="1IEP",
            fingerprint=fingerprint_text(text),
            meta={"stage": "raw", "pdb_id": "1IEP", "chains": [{"chain_id": "A"}]},
        )
        detect = pockets_module.DetectPocketsTool()
        define = pockets_module.DefinePocketTool()

        # ---- 一、这份结构自带的配体 → 口袋 ----
        found = await detect.execute({"receptor": str(raw.handle)}, context=ledger)
        check(not found.failed, f"检测口袋成功：{found.output}")
        check(found.meta.get("count", 0) >= 1, f"1IEP 至少一处口袋：{found.meta}")
        set_entry = ledger.get(found.meta["handle"])
        payload = products_module.read_json(set_entry.path / "pockets.json")
        first = payload["pockets"][0]
        check(first["source"] == "ligand", f"结构自带配体的来源是 ligand：{first['source']}")
        check(
            first["support"]["complexes"] == 1,
            f"支持度记了复合物数：{first['support']}",
        )
        check(
            first["box"]["size"] == [26.0, 26.0, 26.0],
            f"盒子由中心 + 半径 + padding 现算：{first['box']}",
        )
        check(
            [item["resseq"] for item in first["residues"][:3]] == [360, 315, 318],
            f"口袋残基与夹具对得上：{first['residues'][:3]}",
        )
        check(
            "define_pocket" in found.output,
            f"返回里给出下一步：{found.output}",
        )

        # ---- 二、同参复用 ----
        again = await detect.execute({"receptor": str(raw.handle)}, context=ledger)
        check(again.meta.get("reused") is True, "同参第二次调用复用旧产物")
        check(
            again.meta.get("handle") == found.meta.get("handle"),
            "复用时不重复登记",
        )

        # ---- 三、没有配体时留空，不编 ----
        bare = work / "bare.pdb"
        bare.write_text(
            "\n".join(line for line in text.splitlines() if line[:6] != "HETATM") + "\n",
            encoding="utf-8",
        )
        bare_entry = ledger.add(
            "receptor",
            path=bare,
            summary="无配体结构",
            fingerprint=fingerprint_text(bare.read_text(encoding="utf-8")),
            meta={"stage": "raw", "chains": [{"chain_id": "A"}]},
        )
        empty = await detect.execute(
            {"receptor": str(bare_entry.handle), "sources": "ligand"}, context=ledger
        )
        check(not empty.failed, "没有配体不是失败")
        check(empty.meta.get("count") == 0, "空集就是空集")
        check(
            "没有可当配体的杂原子组" in empty.output,
            f"空集要说清原因：{empty.output}",
        )

        # ---- 四、缺参数与坏来源 ----
        no_uni = await detect.execute(
            {"receptor": str(raw.handle), "sources": "complex"}, context=ledger
        )
        check(
            no_uni.failed and "uniprot" in no_uni.output,
            f"复合物来源要 uniprot：{no_uni.output}",
        )
        no_pred = await detect.execute(
            {"receptor": str(bare_entry.handle), "sources": "predicted"}, context=ledger
        )
        check(
            no_pred.failed and "pdb_id" in no_pred.output,
            f"预测来源要 pdb_id：{no_pred.output}",
        )
        bad_src = await detect.execute(
            {"receptor": str(raw.handle), "sources": "bogus"}, context=ledger
        )
        check(
            bad_src.failed and "ligand" in bad_src.output,
            f"坏来源要列出允许值：{bad_src.output}",
        )

        # ---- 五、define_pocket 三路 ----
        picked = await define.execute(
            {"pockets": found.meta["handle"], "index": 1, "name": "STI 位点"},
            context=ledger,
        )
        check(not picked.failed, f"从口袋集选一处：{picked.output}")
        chosen = ledger.get(picked.meta["handle"])
        check(chosen.meta.get("kind") == "pocket", f"登记成单个口袋：{chosen.meta}")
        check(
            picked.meta.get("center") == first["center"],
            "选出来的中心与口袋里的一致",
        )

        both = await define.execute(
            {"pockets": found.meta["handle"], "index": 1, "residues": "A:315"},
            context=ledger,
        )
        check(both.failed and "二选一" in both.output, f"三路互斥：{both.output}")
        no_index = await define.execute(
            {"pockets": found.meta["handle"]}, context=ledger
        )
        check(
            no_index.failed and "index" in no_index.output,
            f"缺 index 要说清：{no_index.output}",
        )

        manual = await define.execute(
            {"receptor": str(raw.handle), "residues": "A:315,A:318,A:319"},
            context=ledger,
        )
        check(not manual.failed, f"残基那一路可用：{manual.output}")
        check(
            manual.meta["size"] != [26.0, 26.0, 26.0],
            f"残基盒子按坐标算，不是固定的 26³：{manual.meta['size']}",
        )
        missing = await define.execute(
            {"receptor": str(raw.handle), "residues": "A:9999"}, context=ledger
        )
        check(
            missing.failed and "225–498" in missing.output,
            f"坏残基要列范围：{missing.output}",
        )
        explicit = await define.execute(
            {"receptor": str(raw.handle), "center": "1,2,3", "size": "20,20,20"},
            context=ledger,
        )
        check(
            not explicit.failed and explicit.meta["size"] == [20.0, 20.0, 20.0],
            "显式坐标可用",
        )

        # ---- 六、盒子进受体 ----
        prepare = receptor_module.PrepareReceptorTool()
        done = await prepare.execute(
            {
                "structure": str(raw.handle),
                "pocket": picked.meta["handle"],
                "chain": "A",
                "altloc": "A",
                "confirm": True,
            },
            context=ledger,
        )
        check(not done.failed, f"口袋盒子能准备受体：{done.output}")
        # 补氢那一行报的是**全部**残基（输入已去氢，全由模板补），
        # 不是「补后仍有缺失」的异常数——后者实测只有 20，而总数是 1970（P3-2）。
        check(
            "补氢：" in done.output and "输入已去氢" in done.output,
            f"补氢总数报全部残基：{done.output[:300]}",
        )
        check(
            "补后仍有缺失" in done.output,
            f"异常数单独说一句：{done.output[:300]}",
        )
        prepared = ledger.get(done.meta["handle"])
        box_text = (prepared.path / "box.txt").read_text(encoding="utf-8")
        check(
            f"center_x = {first['center'][0]:.3f}" in box_text,
            f"box.txt 来自口袋：{box_text}",
        )
        check(
            f"口袋 {picked.meta['handle']}" in done.output,
            f"返回里写明盒子来源：{done.output}",
        )

        mutex = await prepare.execute(
            {
                "structure": str(raw.handle),
                "pocket": picked.meta["handle"],
                "box_center": "1,2,3",
                "box_size": "10,10,10",
            },
            context=ledger,
        )
        check(mutex.failed and "互斥" in mutex.output, f"口袋与显式坐标互斥：{mutex.output}")

        other = ledger.add(
            "receptor",
            path=bare,
            summary="另一条受体",
            fingerprint="other",
            meta={"stage": "raw", "chains": [{"chain_id": "A"}]},
        )
        other_pocket = await define.execute(
            {"receptor": str(other.handle), "residues": "A:315"}, context=ledger
        )
        wrong = await prepare.execute(
            {
                "structure": str(raw.handle),
                "pocket": other_pocket.meta["handle"],
                "chain": "A",
                "altloc": "A",
                "confirm": True,
            },
            context=ledger,
        )
        check(wrong.failed and "不属于" in wrong.output, f"跨受体的口袋要拒绝：{wrong.output}")

        # 不给 pocket 时行为不变：盒子仍然来自共晶配体
        legacy = await prepare.execute(
            {
                "structure": str(raw.handle),
                "ligand": "STI",
                "chain": "A",
                "altloc": "A",
                "confirm": True,
            },
            context=ledger,
        )
        check(
            not legacy.failed and "共晶配体：STI" in legacy.output,
            f"旧路径不变：{legacy.output}",
        )
    finally:
        pockets_module.pockets_dir = original_dir
        receptor_module.prepared_receptor_dir = original_prepared
        receptor_module.parameterize = original_parameterize
        chemistry_module._probe_deps = original_probe


def _chain_group(
    serial: int,
    resname: str,
    number: int,
    count: int,
    step: float,
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> str:
    """沿 x 轴摆一条锯齿链：`count` 个碳，间距 `step`，y/z 上交替偏移。

    y/z 上必须有偏移：全摆成一条直线时包围盒另外两边是 0，形状那一项就没有意义。
    """
    lines = [
        _pdb_atom(
            serial + index,
            "C1",
            resname,
            "A",
            number,
            index * step,
            0.8 if index % 2 else -0.8,
            0.6 if index % 3 else -0.6,
            kind="HETATM",
        )
        for index in range(count)
    ]
    for offset, (name, element) in enumerate(extra):
        lines.append(
            _pdb_atom(
                serial + count + offset,
                name,
                resname,
                "A",
                number,
                offset * 1.2,
                2.4,
                -1.2,
                kind="HETATM",
                element=element,
            )
        )
    return "".join(lines)


def _lipid_fixture() -> str:
    """一份带脂质、磷脂、去垢剂与三种药物的坐标。"""
    receptor = "".join(
        _pdb_atom(
            index * 3 + 1,
            "CA",
            "LEU" if index % 2 else "PHE",
            "A",
            index + 1,
            index * 3.0,
            -5.0,
            0.5 * index,
        )
        for index in range(10)
    )
    receptor += "".join(
        _pdb_atom(
            100 + index,
            "CB",
            "LEU",
            "A",
            index + 1,
            index * 3.0,
            -3.4,
            0.5 * index,
        )
        for index in range(10)
    )
    text = receptor
    text += _chain_group(1000, "CLR", 20, 26, 0.7, extra=(("O1", " O"),))
    text += _chain_group(2000, "STE", 21, 16, 1.2, extra=(("O1", " O"), ("O2", " O")))
    text += _chain_group(
        3000,
        "PC",
        22,
        20,
        1.0,
        extra=(("P1", " P"),) + tuple((f"O{index}", " O") for index in range(2, 9)),
    )
    text += _chain_group(
        4000,
        "DDM",
        23,
        12,
        1.0,
        extra=tuple((f"O{index}", " O") for index in range(2, 10)),
    )
    text += _chain_group(5000, "TES", 24, 17, 0.6, extra=(("O1", " O"), ("O2", " O")))
    text += _chain_group(6000, "IBP", 25, 11, 0.7, extra=(("O1", " O"), ("O2", " O")))
    text += _chain_group(
        7000,
        "LG1",
        26,
        20,
        0.5,
        extra=(("N1", " N"), ("N2", " N"), ("N3", " N"), ("N4", " N"),
               ("O1", " O"), ("O2", " O")),
    )
    return text


async def check_lipid_screen() -> None:
    """脂质筛查：四条判据各挡一类，类固醇药与短链药留下，准备受体不受影响。"""
    text = _lipid_fixture()
    doc = pdbtext.parse(text)
    filtered = export_module.ligand_groups(doc)
    # 名字名单里没有这些脂质，它们一路进到几何筛查——正是「掺进来看不出」的那一类。
    screened = preprocess_module.screen_ligands(doc, filtered.kept)
    check(
        {group.resname for group in screened.kept} >= {"CLR", "STE", "PC", "DDM"},
        f"几何筛查拦不住脂质：{[group.resname for group in screened.kept]}",
    )

    degreased = preprocess_module.screen_lipids(screened.kept)
    kept = [group.resname for group in degreased.kept]
    check(sorted(kept) == ["IBP", "LG1", "TES"], f"脂质筛之后只剩三个药：{kept}")
    check(
        {reason for _name, reason in degreased.excluded} == {"脂质或去垢剂"},
        f"排除理由只有一类：{degreased.excluded}",
    )
    check(
        degreased.counts == {"脂质或去垢剂": 4},
        f"四组被排掉：{degreased.counts}",
    )

    # 碳数那条的边界只有含磷的组能测到：其余组要靠「极性原子 ≤ 2」，
    # 而 16 个重原子、极性原子 ≤ 2 的组必然有 15 个以上碳，永远过得了碳数那一关。
    seven_carbons = pdbtext.parse(
        _chain_group(
            8000,
            "PL7",
            30,
            7,
            2.7,
            extra=(("P1", " P"),) + tuple((f"O{index}", " O") for index in range(2, 10)),
        )
    )
    check(
        [group.resname for group in preprocess_module.screen_lipids(
            export_module.ligand_groups(seven_carbons).kept
        ).kept] == ["PL7"],
        "7 个碳的磷酸化合物不算脂质（碳数那条拦住）",
    )
    eight_carbons = pdbtext.parse(
        _chain_group(
            8500,
            "PL8",
            32,
            8,
            2.4,
            extra=(("P1", " P"),) + tuple((f"O{index}", " O") for index in range(2, 10)),
        )
    )
    check(
        preprocess_module.screen_lipids(export_module.ligand_groups(eight_carbons).kept).kept == (),
        "8 个碳、含磷、够长的组按磷脂排掉",
    )

    compact = pdbtext.parse(_chain_group(9000, "STD", 31, 18, 0.6, extra=(("O1", " O"),)))
    check(
        [group.resname for group in preprocess_module.screen_lipids(
            export_module.ligand_groups(compact).kept
        ).kept] == ["STD"],
        "18 个碳但只有 10 埃的类固醇骨架不算脂质",
    )

    # 磷脂靠「含 P」放行：极性原子 7 个，其余三条都满足。
    check("PC" not in [group.resname for group in degreased.kept], "含磷的组按磷脂排掉")

    # 准备受体这一步不跟着变：胆固醇写着 HETATM，删掉等于让用户看不到膜成分。
    cleaned = preprocess_module.clean_structure(text)
    check("CLR" in cleaned.text, "清洗结构仍然保留胆固醇（脂质筛只管算口袋）")

    # 元素列缺失时按原子名推断。
    fallback = pdbtext.parse(
        _pdb_atom(1, "Cl", "LG1", "A", 1, 0.0, 0.0, 0.0, kind="HETATM", element="")
        + _pdb_atom(2, "C1", "LG1", "A", 1, 1.5, 0.0, 0.0, kind="HETATM", element="")
        + _pdb_atom(3, "O2'", "LG1", "A", 1, 3.0, 0.0, 0.0, kind="HETATM", element="")
        + _pdb_atom(4, "FE", "LG1", "A", 1, 4.5, 0.0, 0.0, kind="HETATM", element=" O")
    )
    check(
        [pdbtext.element_of(atom) for atom in fallback.atoms] == ["CL", "C", "O", "O"],
        f"元素列缺失时按原子名推断：{[pdbtext.element_of(atom) for atom in fallback.atoms]}",
    )

    # ---- 端到端：`detect_pockets` 只把药当成口袋，并把排掉的脂质报出来 ----
    work = PROBE / "lipid_screen"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    original_dir = pockets_module.pockets_dir
    pockets_module.pockets_dir = lambda: work / "pockets"
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        path = work / "receptor.pdb"
        path.write_text(text, encoding="utf-8")
        raw = ledger.add(
            "receptor",
            path=path,
            summary="夹具",
            fingerprint=fingerprint_text(text),
            meta={"stage": "raw", "pdb_id": "1ABC", "chains": [{"chain_id": "A"}]},
        )
        result = await pockets_module.DetectPocketsTool().execute(
            {"receptor": str(raw.handle), "sources": "ligand"}, context=ledger
        )
        check(not result.failed, f"口袋工具跑通：{result.output}")
        # 三个药在夹具里挨得近，聚成一簇；四组脂质一个都没进来。
        check(
            result.meta.get("count") == 1
            and all(code in result.output for code in ("LG1", "TES", "IBP")),
            f"只剩三组药聚成的那一处口袋：{result.output}",
        )
        check(
            not any(code in result.output for code in ("CLR", "STE", "DDM", "PC")),
            f"脂质没有进结果：{result.output}",
        )
        check(
            "脂质或去垢剂 4 个" in result.output,
            f"排掉几个要写在结论里：{result.output}",
        )
    finally:
        pockets_module.pockets_dir = original_dir


def check_pocket_profile() -> None:
    """口袋画像：壁的组成、配体形状、距离缺失时不编「贴得紧」。"""
    text = _lipid_fixture()
    doc = pdbtext.parse(text)
    ligand = next(
        group
        for group in export_module.ligand_groups(doc).kept
        if group.resname == "LG1"
    )
    residues = export_module.pocket_residues(doc, ligand.coords, 5.0)
    rows = [item.as_dict() for item in residues]
    profile = pockets_module.build_profile(
        rows, ligand_atoms=tuple(ligand.atoms), ligand_names=("LG1",)
    )
    walls = profile["walls"]
    check(
        sum(walls[name] for name in ("aromatic", "hydrophobic", "polar", "charged", "other"))
        == profile["residues"],
        f"壁的四类互不重叠、加起来等于残基数：{walls}",
    )
    check(walls["named"] == profile["residues"], f"残基名都在：{walls}")
    check(walls["aromatic"] > 0 and walls["hydrophobic"] > 0, f"夹具里两种壁都有：{walls}")
    check(
        profile["ligand"]["atoms"] == 26 and profile["ligand"]["carbons"] == 20,
        f"配体按元素数出来：{profile['ligand']}",
    )
    check(
        profile["ligand"]["polar"] == 6,
        f"极性原子是 N+O：{profile['ligand']['polar']}",
    )
    check(profile["measured"] == profile["residues"], f"每个残基都有距离：{profile['measured']}")
    check("score" not in profile, "画像里没有分数")
    check(profile["verdict"].startswith("窄长"), f"长链配体判成窄长：{profile['verdict']}")

    lines = pockets_module.profile_lines(profile)
    check(len(lines) == 3, f"画像三行：{lines}")
    check(any("画像：" in line for line in lines), f"最后一行是判断：{lines}")
    check(any("26 个重原子" in line for line in lines), f"配体那一行写重原子数：{lines}")

    # 没有配体：画像只有壁那一半，不编一个空的 ligand 项。
    bare = pockets_module.build_profile(rows)
    check("ligand" not in bare, f"没有配体就没有 ligand 项：{bare}")
    check(len(pockets_module.profile_lines(bare)) == 2, "没有配体时画像两行")

    # 预测口袋只有残基编号：残基名缺失时照实说算不出来，且不谈贴得紧不紧。
    predicted = pockets_module._pockets_from_prediction(
        {
            "pockets": [
                {
                    "name": "p1",
                    "score": "9.1",
                    "residues": ["A_2", "A_3", "A_4"],
                    "center": [1.0, 2.0, 3.0],
                }
            ]
        },
        receptor_chains=("A",),
        radius=5.0,
    )
    predicted_profile = predicted[0]["profile"]
    check(predicted_profile["walls"]["named"] == 0, f"预测没有残基名：{predicted_profile['walls']}")
    predicted_lines = pockets_module.profile_lines(predicted_profile)
    check(
        any("残基名缺失" in line for line in predicted_lines),
        f"算不出组成要说出来：{predicted_lines}",
    )
    check(
        "四周围得紧" not in predicted_profile["verdict"],
        f"没量过距离就不谈贴紧：{predicted_profile['verdict']}",
    )

    # P0 那条路（`_merge`）出来的口袋自带画像，且 P0 不给分。
    cluster = pockets_module._Cluster3D(
        center=export_module.ligand_center(ligand.coords),
        coords=ligand.coords,
        residues=residues,
        origin={"pdb_id": "1ABC", "ligand": "LG1", "resolution": 2.0},
        atoms=tuple(ligand.atoms),
    )
    merged = pockets_module._merge({0: cluster}, radius=5.0)
    check(len(merged) == 1, f"一处口袋：{merged}")
    check("profile" in merged[0] and merged[0]["profile"]["ligand"]["atoms"] == 26,
          f"合并之后画像还在：{merged[0].get('profile')}")
    check("score" not in merged[0], "P0 口袋仍然不给分")

    # 归一化不能把画像丢掉：`detect_pockets` 走的就是这一层。
    normalized = pockets_module._normalize_pockets(
        merged, layer="ligand", radius=5.0, padding=8.0
    )
    check("profile" in normalized[0], f"归一化保留画像：{normalized[0].keys()}")


async def check_rank_candidates() -> None:
    """候选表：方向、平均秩、缺路留空、对齐、分档、复用。

    夹具是两份产物目录（对接 + 亲和力），手写 manifest——`rank_candidates` 只读
    产物，不跑引擎，所以不需要替掉 vina 或 DTA。
    """
    work = PROBE / "rank_candidates"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    original_dir = analysis_module.candidates_dir
    analysis_module.candidates_dir = lambda: work / "candidates"

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")

        def add_source(kind: str, name: str, rows: list[dict]) -> Any:
            directory = work / name
            directory.mkdir(parents=True, exist_ok=True)
            products_module.write_json_atomic(
                directory / "manifest.json",
                {"kind": kind, "stage": kind, "results": rows},
            )
            products_module.write_json_atomic(directory / "params.json", {})
            return ledger.add(
                "artifact",
                path=directory,
                summary=name,
                fingerprint=fingerprint_path(directory),
                meta={"kind": kind, "stage": kind},
            )

        docking_entry = add_source(
            "docking",
            "dock-a",
            [
                # A 与 D 同分：秩要取平均（1.5），不是「谁在前谁第一」
                {"id": "A", "name": "alpha", "smiles": "CCO", "score": -9.0},
                {"id": "B", "name": "beta", "smiles": "CCC", "score": -8.0},
                {"id": "C", "name": "gamma", "smiles": "CCN", "score": -7.0},
                {"id": "D", "name": "delta", "smiles": "CCCl", "score": -9.0},
            ],
        )
        affinity_entry = add_source(
            "affinity",
            "aff-a",
            [
                {"id": "A", "smiles": "CCO", "pk": 8.0},
                {"id": "C", "smiles": "CCN", "pk": 7.5},
                # E 只在亲和力这一路里有：不能因为「另一路没有」就掉到最后
                {"id": "E", "smiles": "CCBr", "pk": 6.0},
            ],
        )

        rank = analysis_module.RankCandidatesTool()
        result = await rank.execute(
            {"sources": f"{docking_entry.handle},{affinity_entry.handle}", "top": 10},
            context=ledger,
        )
        check(not result.failed, f"合成候选表成功：{result.output}")
        payload = products_module.read_json(
            ledger.get(result.meta["handle"]).path / analysis_module.CANDIDATES_FILE
        )
        rows = {row["id"]: row for row in payload["rows"]}
        check(set(rows) == {"A", "B", "C", "D", "E"}, f"五条都在：{sorted(rows)}")

        # ---- 一、方向与秩 ----
        check(rows["A"]["ranks"]["dock"] == 1.5, f"并列取平均秩：{rows['A']['ranks']}")
        check(rows["D"]["ranks"]["dock"] == 1.5, f"同分的另一条也是 1.5：{rows['D']['ranks']}")
        check(rows["A"]["ranks"]["affinity"] == 1, f"pK 高的排前面：{rows['A']['ranks']}")
        check(
            rows["C"]["ranks"]["affinity"] == 2,
            f"affinity 是降序（越高越好），与 dock 相反：{rows['C']['ranks']}",
        )
        check(rows["A"]["consensus_rank"] == 1.25, f"共识取两路平均：{rows['A']}")
        check(
            [row["id"] for row in payload["rows"]] == ["A", "D", "C", "B", "E"],
            f"排序按共识、再按覆盖路数：{[row['id'] for row in payload['rows']]}",
        )

        # ---- 二、缺一路留空，不补 0 ----
        check("affinity" not in rows["B"]["scores"], f"B 没有 affinity 分：{rows['B']['scores']}")
        check(
            rows["B"]["consensus_rank"] == 3,
            f"缺一路时按已有那路算，不是 0 也不是最差：{rows['B']['consensus_rank']}",
        )
        check(
            rows["E"]["consensus_rank"] == 3 and "dock" not in rows["E"]["scores"],
            f"只在 affinity 里的 E 也不会被推到第一：{rows['E']}",
        )

        # ---- 三、分档 ----
        counts = payload["counts"]
        check(sum(counts.values()) == len(payload["rows"]), f"四档之和等于行数：{counts}")
        # A 与 D 的对接分都是 -9.0，落在同一个并列组里；组内档位取最高，所以高档是两条。
        # 1.0 kcal/mol 以内读不出差别（Vina 的标准误约 2.5），这正是并列组的由来。
        check(counts["high"] == 2, f"A 与 D 并列，高档两条：{counts}")
        check(rows["A"]["tier"] == "high", f"榜首是 high：{rows['A']['tier']}")
        check(rows["D"]["tier"] == "high", f"并列组内档位取最高：{rows['D']['tier']}")
        check(
            rows["A"]["tie"] and rows["A"]["tie"] == rows["D"]["tie"],
            f"A 与 D 是同一个并列组：{rows['A']['tie']!r} / {rows['D']['tie']!r}",
        )
        check(
            rows["B"]["tie"] == "",
            f"D 与 B 差 1.0，刚好不到并列的线：{rows['B']['tie']!r}",
        )
        check(payload["tie_delta"] == 1.0, f"并列阈值写进产物：{payload.get('tie_delta')}")
        check(
            analysis_module._gate_failures({"redock_rmsd": 2.4}) == ["redock_rmsd=2.4>2"],
            "门禁阈值是写死的，不由模型给",
        )
        check(
            analysis_module._gate_failures({"pains": True}) == ["pains"],
            "布尔门禁为真即不通过",
        )
        check(
            analysis_module._gate_failures({"pains": False, "redock_rmsd": 0.8}) == [],
            "没过阈值的门禁不算失败",
        )

        # ---- 四、对齐：id 对不上时按结构认 ----
        left = add_source(
            "docking", "dock-b", [{"id": "X1", "name": "x", "smiles": "c1ccccc1", "score": -5.0}]
        )
        right = add_source("affinity", "aff-b", [{"id": "Y2", "smiles": "c1ccccc1", "pk": 5.0}])
        merged = await rank.execute(
            {"sources": f"{left.handle},{right.handle}"}, context=ledger
        )
        check(not merged.failed, f"只有结构相同的两条也能合：{merged.output}")
        merged_payload = products_module.read_json(
            ledger.get(merged.meta["handle"]).path / analysis_module.CANDIDATES_FILE
        )
        check(
            len(merged_payload["rows"]) == 1,
            f"id 不同但结构相同的两条合成一行：{merged_payload['rows']}",
        )

        # ---- 五、拒绝与复用 ----
        duplicate = await rank.execute(
            {"sources": f"{docking_entry.handle},{docking_entry.handle}"}, context=ledger
        )
        check(
            duplicate.failed and "重复" in duplicate.output,
            f"同一路算两次要拒绝：{duplicate.output}",
        )
        bad_cutoffs = await rank.execute(
            {"sources": f"{docking_entry.handle},{affinity_entry.handle}", "cutoffs": "20,5"},
            context=ledger,
        )
        check(bad_cutoffs.failed, f"分位点写反了要拒绝：{bad_cutoffs.output}")

        pocket_like = add_source("pocket", "pocket-a", [{"id": "A", "score": 1.0}])
        wrong_kind = await rank.execute({"sources": str(pocket_like.handle)}, context=ledger)
        check(
            wrong_kind.failed and "docking" in wrong_kind.output,
            f"认不出来的来源不猜方向：{wrong_kind.output}",
        )

        no_ledger = await rank.execute({"sources": str(docking_entry.handle)})
        check(
            no_ledger.failed and "内核装配问题" in no_ledger.output,
            f"没注入登记表是装配问题：{no_ledger.output}",
        )

        again = await rank.execute(
            {"sources": f"{docking_entry.handle},{affinity_entry.handle}", "top": 10},
            context=ledger,
        )
        check(again.meta.get("reused") is True, "同参第二次调用复用旧产物")
        check(
            again.meta.get("handle") == result.meta.get("handle"),
            "复用时不重复登记",
        )

        entry = ledger.get(result.meta["handle"])
        check(entry.meta.get("kind") == "candidates", f"登记成候选表：{entry.meta}")
        check(
            [str(parent) for parent in entry.parents]
            == [str(docking_entry.handle), str(affinity_entry.handle)],
            f"血缘记了两路来源：{entry.parents}",
        )
        csv_text = (entry.path / analysis_module.CANDIDATES_CSV).read_text(encoding="utf-8")
        check(
            csv_text.splitlines()[0].startswith("id,name,smiles,consensus_rank,tier,tie,dock"),
            f"CSV 表头：{csv_text.splitlines()[0]}",
        )

        single = await rank.execute({"sources": str(docking_entry.handle)}, context=ledger)
        check(
            "单路分位数" in single.output,
            f"只有一路时要写明不是共识：{single.output}",
        )
        check(
            "dock·vina:vina" in single.output,
            f"来源里带引擎:力场（换力场就是另一套数，不能只看 dock 这两个字）：{single.output}",
        )
    finally:
        analysis_module.candidates_dir = original_dir


async def check_contact_map() -> None:
    """接触频次表：分母只算得出位姿的分子、按出现次数排、截断写清还剩多少。

    坐标照旧手写，每一处距离都能口算：THR 在原点，PHE 在 20 Å 外，
    三个位姿分别贴 THR 1.0 Å、THR 3.0 Å、PHE 1.0 Å。
    """

    def line(kind: str, serial: int, name: str, resname: str, number: int,
             x: float, y: float, z: float, element: str) -> str:
        return (
            f"{kind:<6}{serial:>5} {name:<4} {resname:>3} A{number:>4}    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{20.00:>6.2f}          "
            f"{element:>2}"
        )

    work = PROBE / "contact_map"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    receptor_path = work / "receptor.pdb"
    receptor_path.write_text(
        "\n".join(
            [
                line("ATOM", 1, "OG1", "THR", 315, 0.0, 0.0, 0.0, "O"),
                line("ATOM", 2, "CE1", "PHE", 331, 20.0, 0.0, 0.0, "C"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    poses = work / "poses"
    poses.mkdir(parents=True, exist_ok=True)
    for name, x in (("near", 0.0), ("far", 0.0), ("phe", 20.0)):
        y = 1.0 if name != "far" else 3.0
        (poses / f"{name}.pdb").write_text(
            line("HETATM", 1, "C1", "LIG", 1, x, y, 0.0, "C") + "\n",
            encoding="utf-8",
        )

    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    receptor = ledger.add(
        "receptor",
        path=receptor_path,
        summary="合成受体",
        fingerprint=fingerprint_text(receptor_path.read_text(encoding="utf-8")),
        meta={"stage": "raw"},
    )
    docking_entry = ledger.add(
        "artifact",
        path=work,
        summary="对接产物",
        fingerprint="fp-map",
        meta={"kind": "docking", "stage": "docking"},
    )
    products_module.write_json_atomic(
        work / analysis_module.docking.MANIFEST_FILE,
        {
            "kind": "docking",
            "stage": "docking",
            "receptor": {"handle": str(receptor.handle)},
            "engine": {"name": "vina", "scoring": "vina"},
            "results": [
                {"id": "near", "name": "near", "score": -9.0, "pose_pdb": "poses/near.pdb"},
                {"id": "far", "name": "far", "score": -8.0, "pose_pdb": "poses/far.pdb"},
                {"id": "phe", "name": "phe", "score": -7.0, "pose_pdb": "poses/phe.pdb"},
                {"id": "nopose", "name": "np", "score": -6.0, "pose_pdb": ""},
            ],
        },
    )

    contact_map = analysis_module.ContactMapTool()
    result = await contact_map.execute(
        {"docking": str(docking_entry.handle)}, context=ledger
    )
    check(not result.failed, f"频次表算得出来：{result.output}")
    check(result.meta.get("poses") == 3, f"分母是算得出位姿的三个：{result.meta}")
    check(result.meta.get("skipped") == 1, f"缺位姿的单独计数：{result.meta}")
    check(result.meta.get("residues") == 2, f"两个残基接触过：{result.meta}")
    check("A:315:THR" in result.output and "A:331:PHE" in result.output, result.output)
    check(
        result.output.index("A:315:THR") < result.output.index("A:331:PHE"),
        f"出现次数多的排前面：{result.output}",
    )
    check("2/3" in result.output, f"THR 在 2 个位姿里出现：{result.output}")
    check("跳过 1 条" in result.output, f"跳过的要写明：{result.output}")

    tight = await contact_map.execute(
        {"docking": str(docking_entry.handle), "cutoff": 2.5}, context=ledger
    )
    check(
        "1/3" in tight.output and "2/3" not in tight.output,
        f"收紧到 2.5 Å 之后 3.0 Å 那条不算：{tight.output}",
    )

    truncated = await contact_map.execute(
        {"docking": str(docking_entry.handle), "top": 1}, context=ledger
    )
    check(
        "另有 1 个残基未显示" in truncated.output,
        f"截断要说清还剩多少：{truncated.output}",
    )

    wrong_kind = await contact_map.execute(
        {"docking": str(receptor.handle)}, context=ledger
    )
    check(wrong_kind.failed, f"受体不是对接产物：{wrong_kind.output}")

    no_ledger = await contact_map.execute({"docking": str(docking_entry.handle)})
    check(
        no_ledger.failed and "内核装配问题" in no_ledger.output,
        f"没注入登记表是装配问题：{no_ledger.output}",
    )

    # 一个都碰不到时不能说「没有残基接触」就完事，要指向口袋或坐标。
    empty = work / "empty"
    empty.mkdir(exist_ok=True)
    far_entry = ledger.add(
        "artifact",
        path=empty,
        summary="远距离产物",
        fingerprint="fp-far",
        meta={"kind": "docking", "stage": "docking"},
    )
    products_module.write_json_atomic(
        empty / analysis_module.docking.MANIFEST_FILE,
        {
            "kind": "docking",
            "stage": "docking",
            "receptor": {"handle": str(receptor.handle)},
            "results": [{"id": "x", "score": -1.0, "pose_pdb": ""}],
        },
    )
    nothing = await contact_map.execute(
        {"docking": str(far_entry.handle)}, context=ledger
    )
    check(
        nothing.failed and "没有一个能算出接触的位姿" in nothing.output,
        f"全是空位姿时要说明白：{nothing.output}",
    )


async def check_hydrophobic() -> None:
    """疏水分账：HP 对数、HM 匹配率、脱水账、没有疏水原子的边界。

    坐标是手写的小体系，每一处距离都能口算：三个碳围着一个配体氧（埋深 3、最近极性
    受体 6.62 Å），三个碳衬着一个配体碳（2.5–2.9 Å，全疏水 → HM 100%）。
    """

    def line(serial: int, name: str, resname: str, number: int,
             x: float, y: float, z: float, element: str) -> str:
        return (
            f"ATOM  {serial:>5} {name:<4} {resname:>3} A{number:>4}    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{20.00:>6.2f}          "
            f"{element:>2}"
        )

    def pose_line(serial: int, name: str, x: float, y: float, z: float, element: str) -> str:
        return (
            f"HETATM{serial:>5} {name:<4} LIG     1    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{20.00:>6.2f}          "
            f"{element:>2}"
        )

    work = PROBE / "hydrophobic"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    poses = work / "poses"
    poses.mkdir(parents=True, exist_ok=True)

    receptor_path = work / "receptor.pdb"
    receptor_path.write_text(
        "\n".join(
            [
                line(1, "CD1", "LEU", 694, 0.0, 0.0, 0.0, "C"),
                line(2, "CG", "LEU", 694, 1.6, 0.0, 0.0, "C"),
                line(3, "CG1", "VAL", 702, -1.6, 0.0, 0.0, "C"),
                line(4, "CD1", "ILE", 700, 0.0, 4.0, 2.0, "C"),
                line(5, "CG1", "ILE", 700, 1.6, 4.0, 2.0, "C"),
                line(6, "CG2", "ILE", 700, -1.6, 4.0, 2.0, "C"),
                line(7, "OG", "SER", 730, 0.0, 6.0, 0.0, "O"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (poses / "mol-greasy.pdb").write_text(
        "\n".join(
            [
                pose_line(1, "C1", 0.0, 4.0, 4.5, "C"),
                pose_line(2, "O1", 0.0, 0.0, 2.8, "O"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # 全是极性原子：HP / HM 应当明说「不适用」，而不是除零
    (poses / "mol-polar.pdb").write_text(
        "\n".join([pose_line(1, "O1", 0.0, 0.0, 2.8, "O"), pose_line(2, "O2", 2.0, 0.0, 2.8, "O")])
        + "\n",
        encoding="utf-8",
    )

    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    receptor = ledger.add(
        "receptor",
        path=receptor_path,
        summary="合成受体",
        fingerprint=fingerprint_text(receptor_path.read_text(encoding="utf-8")),
        meta={"stage": "raw"},
    )
    docking_entry = ledger.add(
        "artifact",
        path=work,
        summary="对接产物",
        fingerprint="fp-hydrophobic",
        meta={"kind": "docking", "stage": "docking"},
    )
    products_module.write_json_atomic(
        work / analysis_module.docking.MANIFEST_FILE,
        {
            "kind": "docking",
            "stage": "docking",
            "engine": {"name": "vina", "version": "1.2.5", "scoring": "vinardo"},
            "receptor": {"handle": str(receptor.handle)},
            "results": [
                {"id": "mol-greasy", "name": "", "score": -9.0, "pose_pdb": "poses/mol-greasy.pdb"},
                {"id": "mol-polar", "name": "", "score": -8.0, "pose_pdb": "poses/mol-polar.pdb"},
            ],
        },
    )

    tool = analysis_module.AnalyzeHydrophobicTool()
    result = await tool.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-greasy"}, context=ledger
    )
    check(not result.failed, f"疏水分账跑通：{result.output}")
    check(
        result.meta.get("hp_pairs") == 3,
        f"三个碳都在 4 Å 内，HP = 3：{result.meta}",
    )
    check(
        result.meta.get("hm_total") == 1 and result.meta.get("hm_matched") == 1,
        f"唯一的疏水原子周围全是碳，HM 匹配 1/1：{result.meta}",
    )
    check(
        result.meta.get("buried_polar") == 1 and result.meta.get("hbonded_polar") == 0,
        f"那个氧被埋了但没成氢键：{result.meta}",
    )
    check(
        "O1" in result.output and "埋深 3 个受体原子" in result.output,
        f"脱水账里指名道姓：{result.output}",
    )
    check(
        "A:730:SER OG 6.62" in result.output,
        f"给出最近的受体极性原子（6.62 Å 太远，所以成不了氢键）：{result.output}",
    )
    check(
        "这不是 HYDE" in result.output and "kcal" in result.output,
        "口径里必须写明这不是 HYDE、不给 kcal",
    )
    check(
        result.meta.get("engine") == "vina:vinardo",
        f"meta 里带引擎:力场（换力场就是另一套数）：{result.meta}",
    )

    polar = await tool.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-polar"}, context=ledger
    )
    check(
        not polar.failed and polar.meta.get("hm_total") == 0,
        f"没有疏水原子时不除零：{polar.meta}",
    )
    check(
        "不适用" in polar.output,
        f"没有疏水原子要说「不适用」，不是「0%」：{polar.output.splitlines()[0]}",
    )

    bad_radius = await tool.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-greasy", "radius": 9.0},
        context=ledger,
    )
    check(bad_radius.failed, f"radius 越界要拒绝：{bad_radius.output}")

    # 「够近但没算成氢键」与「白埋」必须分开说：3.19 Å 的极性对说成白埋是错的，
    # 它只是角度没过口径（PDB 里氢可能缺、质子化状态也可能不同）。实测那个
    # 伊马替尼位姿就有这种情况，所以这一条不是假想。
    fake_pose = analysis_module._Pose(
        molecule="x",
        name="",
        pose=Path("x.pdb"),
        record={},
        entry=None,
        receptor=None,
        receptor_text="",
        pose_text="",
        receptor_atoms=(),
        ligand_atoms=(),
        engine="vina:vina",
    )
    facing = analysis_module._render_hydrophobic(
        pose=fake_pose,
        profile=analysis_module._HydrophobicProfile(
            pairs=2,
            closest=3.4,
            total=2,
            matched=2,
            unmatched=(),
            buried=(("N6", 4, "A:766:THR OG1", 3.19),),
            satisfied=(),
        ),
        radius=4.0,
    )
    check(
        "只差 3.19 Å" in facing and "白埋" not in facing.splitlines()[-1],
        f"够近却没成键不能说成「白埋」：{facing.splitlines()[-1]}",
    )
    check(
        "0 个极性原子白埋" in facing.splitlines()[0],
        f"首行把这一类算作 0 个白埋、另计「够近没成键」：{facing.splitlines()[0]}",
    )

    other = ledger.add("artifact", summary="不是对接产物", fingerprint="fp-other", meta={"kind": "affinity"})
    wrong_kind = await tool.execute(
        {"docking": str(other.handle), "molecule": "mol-greasy"}, context=ledger
    )
    check(
        wrong_kind.failed and "analyze_hydrophobic" in wrong_kind.output,
        f"非对接产物时点的是自己的名字：{wrong_kind.output}",
    )


async def check_efficiency() -> None:
    """效率体检：LE / LELP 的算术、排序、缺 logP 的降级、LLE 的联接与三种失败。"""
    work = PROBE / "efficiency"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    products_module.write_json_atomic(
        work / analysis_module.docking.MANIFEST_FILE,
        {
            "kind": "docking",
            "stage": "docking",
            "engine": {"name": "vina", "version": "1.2.5", "scoring": "vina"},
            "results": [
                # 十二烷：又大又油，原始分不差，但每颗重原子换来的分很少
                {"id": "big", "name": "dodecane", "smiles": "CCCCCCCCCCCC",
                 "heavy_atoms": 12, "score": -6.0},
                {"id": "mid", "name": "benzene", "smiles": "c1ccccc1",
                 "heavy_atoms": 6, "score": -5.0},
                {"id": "small", "name": "ethanol", "smiles": "CCO",
                 "heavy_atoms": 3, "score": -3.0},
                # 没有 SMILES：logP 与 LELP 都算不出来，只能排在最后
                {"id": "nolp", "name": "no smiles", "smiles": "",
                 "heavy_atoms": 8, "score": -4.0},
            ],
        },
    )
    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    docking_entry = ledger.add(
        "artifact",
        path=work,
        summary="对接产物",
        fingerprint="fp-eff",
        meta={"kind": "docking", "stage": "docking"},
    )

    actives_dir = work / "actives"
    actives_dir.mkdir(parents=True, exist_ok=True)
    products_module.write_json_atomic(
        actives_dir / "manifest.json",
        {
            "kind": "ligandset",
            "stage": "raw",
            "records": [
                # 规范 SMILES 与位姿那条对不上大小写/写法也要能联上
                {"id": "CHEMBL1", "smiles": "c1ccccc1", "pchembl_value": 6.5},
                {"id": "CHEMBL2", "smiles": "CCO", "pchembl_value": None},
            ],
        },
    )
    actives = ledger.add(
        "ligandset",
        path=actives_dir,
        summary="已知活性",
        fingerprint="fp-actives",
        meta={"stage": "raw"},
    )
    empty_actives_dir = work / "actives-empty"
    empty_actives_dir.mkdir(parents=True, exist_ok=True)
    products_module.write_json_atomic(
        empty_actives_dir / "manifest.json",
        {"kind": "ligandset", "stage": "raw", "records": [{"id": "X", "smiles": "CCO"}]},
    )
    empty_actives = ledger.add(
        "ligandset",
        path=empty_actives_dir,
        summary="没有活性值",
        fingerprint="fp-actives-empty",
        meta={"stage": "raw"},
    )

    try:
        from rdkit import Chem as _Chem
        from rdkit.Chem import Descriptors as _Descriptors
    except Exception:
        _Chem = None
        _Descriptors = None

    tool = analysis_module.EfficiencyProfileTool()
    result = await tool.execute({"docking": str(docking_entry.handle)}, context=ledger)
    check(not result.failed, f"效率体检跑通：{result.output}")

    if _Chem is None:
        # 没有化学栈时整列都算不出来——这条路径也要说人话，而不是列一堆 —
        check(
            "算不出 logP" in result.output and result.meta.get("without_logp") == 4,
            f"缺 rdkit 时明说这一列没有：{result.output[:120]}",
        )
        return

    logp_benzene = float(_Descriptors.MolLogP(_Chem.MolFromSmiles("c1ccccc1")))
    le_benzene = 5.0 / 6
    check(
        result.meta.get("total") == 4,
        f"四条都进了表：{result.meta}",
    )
    check(
        result.meta.get("without_logp") == 1,
        f"只有没 SMILES 那一条算不出 logP：{result.meta}",
    )
    check(
        result.meta.get("fat") == 1,
        f"只有十二烷算「大而油」（logP ≥ 4 且 LELP ≥ 6）：{result.meta}",
    )
    check(
        result.meta.get("top", [])[0] == "small",
        f"默认按 LELP 升序，乙醇最靠前：{result.meta.get('top')}",
    )
    check(
        result.meta.get("top", [])[-1] == "nolp",
        f"算不出 LELP 的排最后（None 不是 0）：{result.meta.get('top')}",
    )
    check(
        f"{logp_benzene:>8.2f}" in result.output
        and f"{le_benzene:>8.2f}" in result.output
        and f"{logp_benzene / le_benzene:>8.1f}" in result.output,
        f"苯环那一行的 logP / LE / LELP 与 RDKit 算的一致：{result.output}",
    )
    check("← 大而油" in result.output, "「大而油」要标在行上，不是只给个计数")
    check(
        "不等于从 ΔG 算的 LE" in result.output,
        "口径必须写明「对接分数的 LE ≠ ΔG 的 LE」",
    )

    by_le = await tool.execute(
        {"docking": str(docking_entry.handle), "sort": "le"}, context=ledger
    )
    check(
        by_le.meta.get("top", [])[0] == "small",
        f"按 LE 降序，乙醇（1.00）仍然第一：{by_le.meta.get('top')}",
    )
    check(
        by_le.meta.get("top", [])[1] == "mid",
        f"第二位是苯环（0.83）：{by_le.meta.get('top')}",
    )
    check(
        by_le.meta.get("top", [])[-1] == "nolp",
        f"按 LE 降序时缺值的仍然排最后（降序不能把 None 翻到前面）：{by_le.meta.get('top')}",
    )

    with_potency = await tool.execute(
        {"docking": str(docking_entry.handle), "actives": str(actives.handle)}, context=ledger
    )
    check(not with_potency.failed, f"给了活性集就能算 LLE：{with_potency.output}")
    check(
        with_potency.meta.get("has_potency") is True,
        f"meta 说明这次带了活性：{with_potency.meta}",
    )
    check(
        "pChEMBL" in with_potency.output and "LLE" in with_potency.output,
        f"表头多出 pChEMBL 与 LLE 两列：{with_potency.output.splitlines()[-4:]}",
    )
    check(
        f"{6.5 - logp_benzene:>7.1f}" in with_potency.output,
        f"苯环的 LLE = 6.5 − logP：{with_potency.output}",
    )

    no_values = await tool.execute(
        {"docking": str(docking_entry.handle), "actives": str(empty_actives.handle)},
        context=ledger,
    )
    check(
        no_values.failed and "pchembl_value" in no_values.output,
        f"活性集里没有 pChEMBL 时明说算不了：{no_values.output}",
    )

    bad_sort = await tool.execute(
        {"docking": str(docking_entry.handle), "sort": "lelp2"}, context=ledger
    )
    check(bad_sort.failed, f"未知排序要拒绝：{bad_sort.output}")


async def check_analyze_pose() -> None:
    """位姿分析：接触、氢键、冲突、缺位姿的报法、只读。

    坐标是手写的小分子：一个供体（Thr OG1，背后有 CG2 当「第二个原子」）、
    一个疏水残基、一个远处的 Met。这样每一处距离都能口算核对，不需要真跑 Vina。
    """

    def line(kind: str, serial: int, name: str, resname: str, number: int,
             x: float, y: float, z: float, element: str) -> str:
        return (
            f"{kind:<6}{serial:>5} {name:<4} {resname:>3} A{number:>4}    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{20.00:>6.2f}          "
            f"{element:>2}"
        )

    work = PROBE / "analyze_pose"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    receptor_path = work / "receptor.pdb"
    receptor_path.write_text(
        "\n".join(
            [
                line("ATOM", 1, "CG2", "THR", 315, 0.0, 1.5, 0.0, "C"),
                line("ATOM", 2, "OG1", "THR", 315, 0.0, 0.0, 0.0, "O"),
                line("ATOM", 3, "CE1", "PHE", 331, 3.0, -3.0, 0.0, "C"),
                line("ATOM", 4, "SD", "MET", 330, 12.0, 0.0, 0.0, "S"),
                # 用来验角度判据：NZ 的第二个原子在 +z，配体那个原子在 100° 方向上，
                # 距离 3.0 Å 够近、角度 100° 不够——阈值改成 90° 时它就会混进来。
                line("ATOM", 5, "NZ", "LYS", 333, 0.0, 0.0, 3.0, "N"),
                line("ATOM", 6, "CE", "LYS", 333, 0.0, 0.0, 4.5, "C"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    poses = work / "poses"
    poses.mkdir(parents=True, exist_ok=True)
    (poses / "mol-ok.pdb").write_text(
        "\n".join(
            [
                line("HETATM", 1, "O1", "LIG", 1, 0.0, -3.0, 0.0, "O"),
                line("HETATM", 2, "C1", "LIG", 1, 0.0, -4.4, 0.0, "C"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (poses / "mol-clash.pdb").write_text(
        "\n".join(
            [
                line("HETATM", 1, "O1", "LIG", 1, 0.0, -3.0, 0.0, "O"),
                line("HETATM", 2, "C2", "LIG", 1, 0.5, 0.0, 0.0, "C"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (poses / "mol-angle.pdb").write_text(
        "\n".join(
            [
                line("HETATM", 1, "O1", "LIG", 1, 0.0, -3.0, 0.0, "O"),
                # 与 LYS333 NZ 相距 3.0 Å，但与 NZ→CE 的方向成 100°
                line("HETATM", 2, "O2", "LIG", 1, 2.954, 0.0, 2.479, "O"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    receptor = ledger.add(
        "receptor",
        path=receptor_path,
        summary="合成受体",
        fingerprint=fingerprint_text(receptor_path.read_text(encoding="utf-8")),
        meta={"stage": "raw"},
    )
    docking_entry = ledger.add(
        "artifact",
        path=work,
        summary="对接产物",
        fingerprint="fp-dock",
        meta={"kind": "docking", "stage": "docking"},
    )
    products_module.write_json_atomic(
        work / analysis_module.docking.MANIFEST_FILE,
        {
            "kind": "docking",
            "stage": "docking",
            "receptor": {"handle": str(receptor.handle)},
            "results": [
                {
                    "id": "mol-ok",
                    "name": "cmpd_ok",
                    "score": -9.0,
                    "pose_pdb": "poses/mol-ok.pdb",
                },
                {
                    "id": "mol-clash",
                    "name": "cmpd_clash",
                    "score": -8.5,
                    "pose_pdb": "poses/mol-clash.pdb",
                },
                {"id": "mol-nopose", "name": "cmpd_np", "score": -7.0, "pose_pdb": ""},
                {
                    "id": "mol-angle",
                    "name": "cmpd_angle",
                    "score": -7.5,
                    "pose_pdb": "poses/mol-angle.pdb",
                },
            ],
        },
    )

    analyze = analysis_module.AnalyzePoseTool()
    before = len(ledger.find(kind="artifact", include_archived=True))

    result = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-ok"}, context=ledger
    )
    check(not result.failed, f"位姿分析成功：{result.output}")
    check(result.meta.get("contacts") == 2, f"两个残基接触：{result.meta}")
    check(result.meta.get("hbonds") == 1, f"一个氢键：{result.meta}")
    check(result.meta.get("clashes") == 0, f"没有冲突：{result.meta}")
    check("A:315:THR" in result.output, f"接触残基按标签列出：{result.output}")
    check(
        "≤ 3.5 Å" in result.output and "120°" in result.output,
        f"氢键判据写在返回里：{result.output}",
    )
    check(
        "近似" in result.output,
        "没有氢时角度是近似值，要写明",
    )

    # 口径收紧之后同一份位姿的接触数要变少：说明 cutoff 真的在用
    tight = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-ok", "cutoff": 2.5},
        context=ledger,
    )
    check(tight.meta.get("contacts") == 0, f"2.5 Å 内没有接触：{tight.meta}")

    no_hbond = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-ok", "hbond": False},
        context=ledger,
    )
    check(no_hbond.meta.get("hbonds") == 0, "hbond=false 时不报氢键")

    # 3.0 Å 但角度只有 100°：距离够、角度不够，不算氢键。把阈值改成 90° 这条会变红。
    angle = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-angle"}, context=ledger
    )
    check(
        angle.meta.get("hbonds") == 1,
        f"角度不够的极性接触不算氢键（100° < 120°）：{angle.meta}",
    )

    clash = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-clash"}, context=ledger
    )
    check(clash.meta.get("clashes", 0) >= 1, f"穿进受体要报冲突：{clash.meta}")
    check("处冲突" in clash.output, f"首行要提示冲突：{clash.output}")

    missing = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-404"}, context=ledger
    )
    check(
        missing.failed and "mol-ok" in missing.output,
        f"分子不存在时列出可用的 id：{missing.output}",
    )

    no_pose = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-nopose"}, context=ledger
    )
    check(
        no_pose.failed and "没有转出 PDB" in no_pose.output,
        f"缺位姿要说清是没转出来，不是文件丢了：{no_pose.output}",
    )

    bad_cutoff = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-ok", "cutoff": 9.0},
        context=ledger,
    )
    check(bad_cutoff.failed, f"cutoff 越界要拒绝：{bad_cutoff.output}")

    wrong_kind = await analyze.execute(
        {"docking": str(receptor.handle), "molecule": "mol-ok"}, context=ledger
    )
    check(wrong_kind.failed, f"受体不是对接产物：{wrong_kind.output}")

    # ---- PLIP 交互指纹 ----
    # 合并、抽取、渲染三段都是纯函数，用假记录就能验，不需要装 PLIP；
    # 真跑 PLIP 的那一条在最后，装没装都过。
    pose_text = (poses / "mol-ok.pdb").read_text(encoding="utf-8")
    receptor_text = receptor_path.read_text(encoding="utf-8")

    # 受体末尾的 END 是真实文件里就有的（Vina 转出来的位姿也带一个）。直接拼接的话
    # openbabel 读到那一行就停，配体一个原子都读不进去，PLIP 于是报「没有配体」。
    merged, resname = analysis_module._plip_complex_text(
        receptor_text + "MASTER      1    0    0    0    0    0    0    6    1    0    0\nEND\n",
        pose_text + "CONECT    1    2\nEND\n",
    )
    check(
        merged.splitlines()[-1] == "END" and merged.count("END") == 1,
        f"END 只在最末尾出现一次（受体末尾那个必须丢掉）：{merged.splitlines()[-3:]}",
    )
    check("MASTER" not in merged and "CONECT" not in merged, "MASTER / CONECT 一并丢掉")
    check(
        "OG1" in merged and "O1" in merged,
        "受体与位姿的原子都在同一份文件里（拼上了，不是只读了一份）",
    )
    check(resname == "LIG", f"位姿残基名取候选里第一个没被占用的：{resname}")

    # 受体里已经有 LIG（保留下来的共晶配体）时必须换名字：否则 PLIP 会把晶体那个
    # 也当配体，我们可能拿晶体的相互作用去解释对接位姿，而输出看着完全正常。
    collided, collided_name = analysis_module._plip_complex_text(
        line("HETATM", 9, "C1", "LIG", 5, 9.0, 9.0, 9.0, "C") + "\n", pose_text
    )
    check(
        collided_name == "UNL" and collided.count("UNL") == 2,
        f"受体里已有 LIG 时换用 UNL：{collided_name} / {collided.count('UNL')}",
    )

    def plip_row(**kwargs: Any) -> Any:
        base = {"restype": "LYS", "resnr": 721, "reschain": "A"}
        base.update(kwargs)
        return types.SimpleNamespace(**base)

    fake_site = types.SimpleNamespace(
        saltbridge_lneg=[],
        # 同一个 Asp 的两条盐桥：指纹按残基合并，留最近的那个距离
        saltbridge_pneg=[
            plip_row(restype="ASP", resnr=831, distance=5.11),
            plip_row(restype="ASP", resnr=831, distance=4.90),
        ],
        hbonds_ldon=[],
        hbonds_pdon=[plip_row(distance_ad=3.37)],
        pistacking=[plip_row(restype="PHE", resnr=699, distance=3.71, type="P")],
        pication_laro=[],
        pication_paro=[],
        hydrophobic_contacts=[plip_row(restype="LEU", resnr=694, distance=3.46)],
        halogen_bonds=[],
        water_bridges=[],
        metal_complexes=[],
    )
    hits = analysis_module._plip_hits(fake_site)
    check(
        set(hits) == {"saltbridge", "hbond", "pistacking", "hydrophobic"},
        f"只报有记录的那几类（空的不占行）：{sorted(hits)}",
    )
    check(
        len(hits["saltbridge"]) == 1 and hits["saltbridge"][0].count == 2,
        f"同一残基的两条盐桥合成一条、记住条数：{hits.get('saltbridge')}",
    )
    check(
        hits["saltbridge"][0].distance == 4.90,
        f"合并时留最近的那个距离：{hits['saltbridge'][0].distance}",
    )
    check(
        hits["saltbridge"][0].detail == "配体正电",
        f"盐桥方向按属性名认（pneg = 蛋白带负电）：{hits['saltbridge'][0].detail}",
    )
    check(
        hits["hbond"][0].detail == "蛋白供体",
        f"氢键方向按属性名认（pdon = 蛋白是供体）：{hits['hbond'][0].detail}",
    )
    check(
        hits["pistacking"][0].detail == "平行",
        f"P 型 π 堆积译成「平行」（T 型另有其名）：{hits['pistacking'][0].detail}",
    )
    check(
        hits["hbond"][0].distance == 3.37,
        f"氢键的距离字段叫 distance_ad（不是 distance）：{hits['hbond'][0].distance}",
    )

    rendered = analysis_module._render_fingerprint(hits)
    check(
        rendered[0].startswith("  盐桥 1 个残基：A:831:ASP 4.90 Å（×2） 配体正电"),
        f"盐桥那行的格式：{rendered[0]!r}",
    )
    check(
        len(rendered) == 4 and rendered[3].startswith("  疏水 1 个残基：A:694:LEU"),
        f"一类一行、顺序固定：{rendered}",
    )
    check(
        analysis_module._render_fingerprint({})[0].startswith("  一类都没找到"),
        "一条都没有时说清是「没找到」而不是「没算」",
    )

    # 缺依赖的两种形态说法不同：没装 plip 与「plip 在、它的依赖不在」。
    # 后者说成「本机没装 PLIP」会让用户去重装已经装好的东西。
    check(
        "本机没装 PLIP" in analysis_module._plip_missing_message("plip"),
        f"缺 plip 本身：{analysis_module._plip_missing_message('plip')}",
    )
    partial = analysis_module._plip_missing_message("openbabel")
    check(
        "本机没装 PLIP" not in partial and "openbabel" in partial and "plip 本身在" in partial,
        f"plip 在、缺的是它的依赖时要照实说：{partial}",
    )
    check(
        analysis_module.PLIP_INSTALL_HINT in partial,
        "两种形态都给同一条安装指令（装那个文件两样都会补上）",
    )

    # 版本漂移：字段改名时 getattr 不会报错，只会永远返回空——必须先核对再算。
    check(
        analysis_module._plip_missing_attrs(fake_site) == [],
        "字段齐全时不报缺",
    )
    trimmed = types.SimpleNamespace(
        **{k: v for k, v in vars(fake_site).items() if k != "hbonds_pdon"}
    )
    check(
        analysis_module._plip_missing_attrs(trimmed) == ["hbonds_pdon"],
        f"少一个字段要指名道姓：{analysis_module._plip_missing_attrs(trimmed)}",
    )

    # PLIP 在场但一用就炸（openbabel 初始化失败、临时目录不可写、内部断言）：
    # 整段都在 try 里，所以这里拿到的是原因，不是异常。漏出去的话整次调用都会失败，
    # 而接触与氢键早就算完了。
    class _ExplodingPlip:
        def __init__(self) -> None:
            raise RuntimeError("openbabel 初始化失败")

    original_loader = analysis_module._plip_loader
    try:
        analysis_module._plip_loader = lambda: (_ExplodingPlip, "")
        broken_hits, broken_note = analysis_module.plip_fingerprint(receptor_text, pose_text)
    finally:
        analysis_module._plip_loader = original_loader
    check(
        broken_hits == {} and "算不了" in broken_note and "RuntimeError" in broken_note,
        f"PLIP 内部炸掉要降级成一句原因，不能把整次调用带下去：{broken_note}",
    )

    # 开关本身：默认不跑（那 0.3 秒不该白花），打开才追加那一节。
    check("PLIP" not in result.output, "默认不跑 PLIP，输出里不该出现它")

    original_plip = analysis_module.plip_fingerprint
    try:
        analysis_module.plip_fingerprint = lambda *a, **k: (hits, "")
        with_fp = await analyze.execute(
            {"docking": str(docking_entry.handle), "molecule": "mol-ok", "interactions": True},
            context=ledger,
        )
        analysis_module.plip_fingerprint = lambda *a, **k: (
            {},
            analysis_module._plip_missing_message("plip"),
        )
        degraded_fp = await analyze.execute(
            {"docking": str(docking_entry.handle), "molecule": "mol-ok", "interactions": True},
            context=ledger,
        )
    finally:
        analysis_module.plip_fingerprint = original_plip

    check(not with_fp.failed, f"打开 interactions 不影响主结果：{with_fp.output}")
    check(
        "PLIP 交互指纹" in with_fp.output and "A:831:ASP" in with_fp.output,
        f"指纹那一节进了输出：{with_fp.output}",
    )
    check(
        "PLIP：1 盐桥 / 1 氢键 / 1 π 堆积 / 1 疏水。" in with_fp.output,
        f"结论那行带上各类总数（模型只读第一行）：{with_fp.output.splitlines()[0]}",
    )
    check(
        with_fp.meta.get("interactions") == {
            "saltbridge": 1, "hbond": 1, "pistacking": 1, "hydrophobic": 1
        },
        f"meta 里带机器可读的计数：{with_fp.meta.get('interactions')}",
    )
    check(
        not degraded_fp.failed and degraded_fp.meta.get("contacts") == 2,
        "PLIP 缺席时接触照算（可选依赖不能拖垮整条结果）",
    )
    check(
        "本机没装 PLIP" in degraded_fp.output
        and "interactions_error" in degraded_fp.meta,
        f"缺席的原因照实写出来，并进 meta：{degraded_fp.output.splitlines()[-1]}",
    )
    check(
        "interactions" not in degraded_fp.meta,
        "没算成时不写 interactions（空字典与「算了但没有」长得一样，结论却相反）",
    )
    check(
        "PLIP 没算成（原因见末尾）" in degraded_fp.output.splitlines()[0],
        f"第一行就要说没算成——否则「没有盐桥那几行」会被读成「没有盐桥」："
        f"{degraded_fp.output.splitlines()[0]}",
    )

    bad_plip = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-ok", "interactions": "yes"},
        context=ledger,
    )
    check(bad_plip.failed and "interactions" in bad_plip.output, f"类型不对要拒绝：{bad_plip.output}")

    # 真跑一次 PLIP：验证「拼文件 → 读进去 → 抽出指纹」这条链真的通。
    # 用一对平行的苯环（PHE 331 与配体），疏水接触是必然的；不依赖芳香性识别，
    # 所以换 openbabel 版本也不会红。没装 PLIP 时只验那句安装指引。
    loader, reason = analysis_module._plip_loader()
    if loader is None:
        check("requirements-chem.txt" in reason, f"没装 PLIP 时给的是安装入口：{reason}")
    else:
        real_hits, real_note = analysis_module.plip_fingerprint(
            "\n".join(
                line("ATOM", index + 1, name, "PHE", 331,
                     1.39 * math.cos(math.radians(60 * index)),
                     1.39 * math.sin(math.radians(60 * index)), 0.0, "C")
                for index, name in enumerate(["CG", "CD1", "CE1", "CZ", "CE2", "CD2"])
            )
            + "\n",
            "\n".join(
                line("HETATM", index + 1, f"C{index + 1}", "UNL", 1,
                     1.39 * math.cos(math.radians(60 * index)),
                     1.39 * math.sin(math.radians(60 * index)), 3.7, "C")
                for index in range(6)
            )
            + "\n",
        )
        check(real_note == "", f"真跑 PLIP 不报错：{real_note}")
        check(
            real_hits.get("hydrophobic") and real_hits["hydrophobic"][0].residue == "A:331:PHE",
            f"3.7 Å 的平行苯环要认出疏水接触：{real_hits}",
        )

    no_ledger = await analyze.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-ok"}
    )
    check(
        no_ledger.failed and "内核装配问题" in no_ledger.output,
        f"没注入登记表是装配问题：{no_ledger.output}",
    )

    after = len(ledger.find(kind="artifact", include_archived=True))
    check(after == before, f"只读：调用前后产物条目数不变（{before} → {after}）")


async def check_alternative_poses() -> None:
    """替代结合模式：多模式位姿按体积聚簇，报每簇的接触差异。

    坐标手写：模型 1、2 挤在 THR315 旁边（第二套只挪 0.3 埃，算同一个摆法），
    模型 3 挪到 10 埃外的 PHE331 旁边。这样「两簇」与「接触残基互不相同」都能
    口算核对，不需要真跑 Vina。
    """

    def line(kind: str, serial: int, name: str, resname: str, number: int,
             x: float, y: float, z: float, element: str) -> str:
        return (
            f"{kind:<6}{serial:>5} {name:<4} {resname:>3} A{number:>4}    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{20.00:>6.2f}          "
            f"{element:>2}"
        )

    def model(index: int, score: float, x: float, z: float) -> str:
        body = [f"MODEL {index}"]
        body.append(f"REMARK VINA RESULT:    {score:6.1f}      0.000      0.000")
        body.append(line("ATOM", 1, "C1", "UNL", 1, x, -3.0, z, "C"))
        body.append(line("ATOM", 2, "C2", "UNL", 1, x + 1.0, -3.0, z, "C"))
        body.append("ENDMDL")
        return "\n".join(body)

    work = PROBE / "alternative_poses"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    receptor_path = work / "receptor.pdb"
    receptor_path.write_text(
        "\n".join(
            [
                line("ATOM", 1, "OG1", "THR", 315, 0.0, 0.0, 0.0, "O"),
                line("ATOM", 2, "CE1", "PHE", 331, 0.0, 0.0, 10.0, "C"),
                # 远到任何模式都碰不到：用来验「接触表不会凭空多出残基」
                line("ATOM", 3, "SD", "MET", 330, 40.0, 0.0, 0.0, "S"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def write_poses(name: str, models: list[str]) -> None:
        (work / name).write_text("\n".join(models) + "\n", encoding="utf-8")

    write_poses("three.pdbqt", [model(1, -9.0, 0.0, 0.0),
                                model(2, -8.5, 0.3, 0.0),
                                model(3, -8.0, 0.0, 10.0)])
    write_poses("same.pdbqt", [model(1, -9.0, 0.0, 0.0),
                               model(2, -8.5, 0.3, 0.0)])
    write_poses("single.pdbqt", [model(1, -9.0, 0.0, 0.0)])

    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    receptor = ledger.add(
        "receptor",
        path=receptor_path,
        summary="合成受体",
        fingerprint=fingerprint_text(receptor_path.read_text(encoding="utf-8")),
        meta={"stage": "raw"},
    )
    docking_entry = ledger.add(
        "artifact",
        path=work,
        summary="对接产物",
        fingerprint="fp-alt-dock",
        meta={"kind": "docking", "stage": "docking"},
    )
    products_module.write_json_atomic(
        work / analysis_module.docking.MANIFEST_FILE,
        {
            "kind": "docking",
            "stage": "docking",
            "receptor": {"handle": str(receptor.handle)},
            "results": [
                {"id": "mol-three", "name": "cmpd_three", "score": -9.0,
                 "pose_pdbqt": "three.pdbqt", "pose_pdb": ""},
                {"id": "mol-same", "name": "cmpd_same", "score": -9.0,
                 "pose_pdbqt": "same.pdbqt", "pose_pdb": ""},
                {"id": "mol-single", "name": "cmpd_single", "score": -9.0,
                 "pose_pdbqt": "single.pdbqt", "pose_pdb": ""},
                {"id": "mol-nopose", "name": "cmpd_np", "score": -7.0, "pose_pdb": ""},
            ],
        },
    )

    alt = analysis_module.AlternativePosesTool()
    before = len(ledger.find(kind="artifact", include_archived=True))

    result = await alt.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-three"}, context=ledger
    )
    check(not result.failed, f"多模式分析成功：{result.output.splitlines()[0]}")
    check(result.meta.get("modes") == 3, f"三个模式都读到：{result.meta}")
    check(result.meta.get("parsed") == 3, f"三个都能解析：{result.meta}")
    check(result.meta.get("clusters") == 2, f"聚成两簇：{result.meta}")
    check(
        result.meta.get("skipped") == [],
        f"没有读不出的模式：{result.meta.get('skipped')}",
    )
    check(
        "3 个模式聚成 2 簇" in result.output,
        f"第一行写清读了几个、聚成几簇：{result.output.splitlines()[0]}",
    )
    check(
        "模式 1–2（2 个）" in result.output and "最好 -9.00" in result.output,
        f"簇 1 是模型 1、2，分数取代表模式：{result.output}",
    )
    check(
        "多了 A:331:PHE" in result.output and "少了 A:315:THR" in result.output,
        f"第二个摆法换了一处接触面，要逐条写出来：{result.output}",
    )
    check(
        "只有一簇" not in result.output,
        "两簇时不写「只有一簇」那句",
    )

    # 两套模式几乎重合：并成一簇，并明说「没有第二个可区分的摆法」
    same = await alt.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-same"}, context=ledger
    )
    check(not same.failed and same.meta.get("clusters") == 1, f"重合的并成一簇：{same.meta}")
    check(
        "只有一簇：这次对接没有给出第二个可区分的摆法" in same.output,
        f"一簇是读数不是空结果，要说清：{same.output}",
    )

    # 只有一个模式：不聚簇，直接说没有第二个摆法
    single = await alt.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-single"}, context=ledger
    )
    check(
        not single.failed and "只有 1 个模式" in single.output,
        f"只有一个模式时说明白：{single.output}",
    )
    check(single.meta.get("clusters") == 1, f"meta 里也写一簇：{single.meta}")

    # 没有记 pose_pdbqt：说清缺什么，不猜
    no_pose = await alt.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-nopose"}, context=ledger
    )
    check(
        no_pose.failed and "pose_pdbqt" in no_pose.output,
        f"缺多模式位姿要指名缺哪个字段：{no_pose.output}",
    )

    unknown = await alt.execute(
        {"docking": str(docking_entry.handle), "molecule": "nope"}, context=ledger
    )
    check(
        unknown.failed and "mol-three" in unknown.output,
        f"id 打错时列出可用的 id：{unknown.output}",
    )
    bad_modes = await alt.execute(
        {"docking": str(docking_entry.handle), "molecule": "mol-three", "max_poses": 1},
        context=ledger,
    )
    check(bad_modes.failed and "max_poses" in bad_modes.output, f"上限越界要拒绝：{bad_modes.output}")

    # 切段：没有 MODEL 行的整份算一套；三套就切三段
    check(
        len(analysis_module._split_pdbqt_models("ATOM      1  C1\n")) == 1,
        "没有 MODEL 行时整份算一套",
    )
    three_text = "\n".join([model(1, -9.0, 0.0, 0.0),
                            model(2, -8.5, 0.3, 0.0),
                            model(3, -8.0, 0.0, 10.0)])
    check(
        len(analysis_module._split_pdbqt_models(three_text)) == 3,
        "按 MODEL/ENDMDL 切出三段（pdbtext.parse 只留第一段，不切就只剩一个位姿）",
    )

    after = len(ledger.find(kind="artifact", include_archived=True))
    check(after == before, f"只读：调用前后产物条目数不变（{before} → {after}）")


async def check_curate_ligands() -> None:
    """待筛库第一道闸：PAINS/Brenk、去重三种键、窗口闭区间、flag 模式。

    源配体集是手写目录（`manifest.json` + `ligands.csv`），因为 `import_ligandset`
    自己在导入时就会按 canonical SMILES 去重——那样就测不到 `curate_ligands` 的去重了。
    这也不是假想形状：ChEMBL 那种直接落 CSV 的条目就是这样进来的。
    """
    work = PROBE / "curate_ligands"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    rows = [
        # id, name, smiles, input_smiles, heavy_atoms, source_index
        ("caffeine", "caffeine", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C", 14, 1),
        ("ibuprofen", "ibuprofen", "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "CC(C)Cc1ccc(cc1)C(C)C(=O)O", 15, 2),
        ("catechol", "catechol", "Oc1ccccc1O", "Oc1ccccc1O", 8, 3),
        ("nitro", "nitro", "O=[N+]([O-])c1ccccc1", "O=[N+]([O-])c1ccccc1", 9, 4),
        ("long", "long", "C" * 33 + "C(=O)O", "C" * 33 + "C(=O)O", 34, 5),
        ("aspirin_a", "aspirin", "CC(=O)Oc1ccccc1C(=O)O", "OC(=O)c1ccccc1OC(C)=O", 13, 6),
        ("aspirin_b", "aspirin copy", "CC(=O)Oc1ccccc1C(=O)O", "CC(=O)Oc1ccccc1C(=O)O", 13, 7),
        ("ala_l", "L-alanine", "C[C@H](N)C(=O)O", "C[C@H](N)C(=O)O", 6, 8),
        ("ala_d", "D-alanine", "C[C@@H](N)C(=O)O", "C[C@@H](N)C(=O)O", 6, 9),
    ]
    source_dir = work / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    header = "id,name,smiles,input_smiles,heavy_atoms,source_index\n"
    csv_text = header + "".join(
        f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]}\n" for row in rows
    )
    (source_dir / ligand_module.CSV_FILE).write_text(csv_text, encoding="utf-8")
    products_module.write_json_atomic(
        source_dir / ligand_module.MANIFEST_FILE,
        {"kind": "ligandset", "stage": "raw", "tool": "fixture", "counts": {"accepted": len(rows)}},
    )
    before_bytes = (source_dir / ligand_module.CSV_FILE).read_bytes()

    original_dir = ligand_module.curated_ligandset_dir
    ligand_module.curated_ligandset_dir = lambda: work / "curated"

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        source = ledger.add(
            "ligandset",
            path=source_dir,
            summary="夹具库",
            fingerprint=fingerprint_text(csv_text),
            meta={"kind": "ligandset", "stage": "raw"},
            link_current_receptor=False,
        )
        curate = ligand_module.CurateLigandsTool()

        # ---- 一、默认：PAINS + Brenk 都开 ----
        strict = await curate.execute({"ligands": str(source.handle)}, context=ledger)
        check(not strict.failed, f"筛库成功：{strict.output}")
        check(strict.meta.get("kept") == 4, f"干净的四条留下：{strict.meta}")
        check(strict.meta.get("dropped") == 5, f"五条被淘汰：{strict.meta}")
        check("PAINS 1 条" in strict.output, f"PAINS 单独计数：{strict.output}")
        check("catechol" in strict.output, f"淘汰示例里点了名：{strict.output}")
        entry = ledger.get(strict.meta["handle"])
        check(entry.meta.get("stage") == "curated", f"阶段标成 curated：{entry.meta.get('stage')}")
        check(
            [str(parent) for parent in entry.parents] == [str(source.handle)],
            f"血缘记了源配体集：{entry.parents}",
        )
        # 形状必须与 import_ligandset 同形，否则 prepare_ligands 读不了
        items, kind = ligand_module._source_cleaned(entry)
        check(len(items) == 4 and kind == "raw", f"prepare_ligands 能直接读：{len(items)} 条")
        check(
            {item.id for item in items} == {"caffeine", "ibuprofen", "ala_l", "ala_d"},
            f"读出来的正是留下的四条：{sorted(item.id for item in items)}",
        )

        # ---- 二、去重的三种键 ----
        async def run(**extra: Any) -> Any:
            arguments = {"ligands": str(source.handle), "pains": False, "brenk": False}
            arguments.update(extra)
            return await curate.execute(arguments, context=ledger)

        none_kept = await run(dedupe="none")
        check(none_kept.meta.get("kept") == 9, f"不去重时九条全在：{none_kept.meta}")

        canonical = await run(dedupe="canonical")
        check(canonical.meta.get("kept") == 8, f"canonical 去掉一条重复：{canonical.meta}")

        inchikey = await run(dedupe="inchikey")
        check(inchikey.meta.get("kept") == 8, f"完整 InChIKey 也去掉一条：{inchikey.meta}")

        scaffold = await run(dedupe="scaffold")
        check(
            scaffold.meta.get("kept") == 7,
            f"前 14 位把一对对映体算成同一个骨架：{scaffold.meta}",
        )

        # ---- 三、窗口是闭区间 ----
        window = await run(dedupe="none", max_mw=194.2)
        check(window.meta.get("kept") == 7, f"等于上限的保留：{window.meta}")
        tighter = await run(dedupe="none", max_mw=194.0)
        check(tighter.meta.get("kept") == 6, f"上限收 0.2 就少一条：{tighter.meta}")

        # ---- 四、flag 只标注不删 ----
        flag = await run(mode="flag", pains=True, brenk=True, dedupe="inchikey")
        check(flag.meta.get("kept") == 9, f"flag 模式保留全集：{flag.meta}")
        flag_entry = ledger.get(flag.meta["handle"])
        with (flag_entry.path / ligand_module.CSV_FILE).open(encoding="utf-8") as handle:
            flagged = [row for row in csv.DictReader(handle) if row["flags"]]
        check(
            len(flagged) == 5,
            f"命中的五行都带了标记（catechol / nitro / long / 两个 aspirin）：{len(flagged)} 行",
        )
        check(
            any("PAINS" in row["flags"] for row in flagged),
            "标记里写明命中的是哪一类规则",
        )
        # flag 模式下「保留 / 淘汰」不互斥：9 条全在库里，5 条带标记。
        # 原先照 drop 的口径写「保留 4 条，淘汰 5 条」，4 + 5 = 9 看着像算错了（P3-3）。
        check("淘汰" not in flag.output, f"flag 模式不说「淘汰」：{flag.output}")
        check(
            "其中 5 条带标记" in flag.output and "仍在库里" in flag.output,
            f"flag 模式说清「标了几条、还在库里」：{flag.output}",
        )

        # ---- 五、全灭要报失败并说清 ----
        empty = await run(max_mw=50.0)
        check(
            empty.failed and "全部" in empty.output,
            f"一条不剩是失败，不是空表：{empty.output}",
        )

        # ---- 六、源文件一个字节都不动 ----
        check(
            (source_dir / ligand_module.CSV_FILE).read_bytes() == before_bytes,
            "筛完源配体集没被改写",
        )

        # ---- 七、同参复用 ----
        again = await curate.execute({"ligands": str(source.handle)}, context=ledger)
        check(again.meta.get("reused") is True, "同参第二次调用复用旧产物")
        check(
            again.meta.get("handle") == strict.meta.get("handle"),
            "复用时不重复登记",
        )

        no_ledger = await curate.execute({"ligands": str(source.handle)})
        check(
            no_ledger.failed and "内核装配问题" in no_ledger.output,
            f"没注入登记表是装配问题：{no_ledger.output}",
        )
        bad_mode = await curate.execute(
            {"ligands": str(source.handle), "mode": "delete"}, context=ledger
        )
        check(bad_mode.failed and "mode" in bad_mode.output, f"认不出的模式要拒绝：{bad_mode.output}")
    finally:
        ligand_module.curated_ligandset_dir = original_dir


async def check_reference_set() -> None:
    """参考活性集：同一分子取最强、活性下限、PAINS 只标不删、too_small。

    源是 `search_actives` 那种 CSV（`entry.path` 是文件）；也验一遍目录形态，
    因为 `prepare_ligands` 的两侧都要能吃。
    """
    work = PROBE / "reference_set"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    actives = work / "actives.csv"
    actives.write_text(
        "molecule_chembl_id,molecule_pref_name,smiles,pchembl_value\n"
        # 同一个分子两条记录：只留强的 7.5
        "C1,aspirin,CC(=O)Oc1ccccc1C(=O)O,6.5\n"
        "C1,aspirin (strong),CC(=O)Oc1ccccc1C(=O)O,7.5\n"
        "C2,caffeine,CN1C=NC2=C1C(=O)N(C(=O)N2C)C,8.0\n"
        "C3,weak,CC(C)Cc1ccc(cc1)C(C)C(=O)O,5.0\n"
        "C4,catechol,Oc1ccccc1O,6.9\n"
        "C5,broken,not-a-smiles,7.0\n"
        "C6,no-value,CCO,\n",
        encoding="utf-8",
    )

    original_dir = ligand_module.reference_ligandset_dir
    ligand_module.reference_ligandset_dir = lambda: work / "reference"

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        source = ledger.add(
            "ligandset",
            path=actives,
            summary="ChEMBL 活性",
            fingerprint=fingerprint_text(actives.read_text(encoding="utf-8")),
            meta={"kind": "ligandset", "accession": "P00533"},
            link_current_receptor=False,
        )
        build = ligand_module.BuildReferenceSetTool()
        result = await build.execute({"actives": str(source.handle)}, context=ledger)
        check(not result.failed, f"参考集生成成功：{result.output}")
        check(result.meta.get("kept") == 3, f"三条达到 pChEMBL≥6：{result.meta}")
        check(result.meta.get("too_small") is True, f"少于 10 条要标 too_small：{result.meta}")
        check(result.meta.get("pains") == 1, f"catechol 命中 PAINS：{result.meta}")
        check(
            "只标不删" in result.output,
            f"PAINS 只标注的语义要写在返回里：{result.output}",
        )

        entry = ledger.get(result.meta["handle"])
        check(entry.meta.get("stage") == "reference", f"阶段是 reference：{entry.meta.get('stage')}")
        check(entry.meta.get("label") == "active", f"标签是 active：{entry.meta.get('label')}")
        payload = products_module.read_json(entry.path / ligand_module.MANIFEST_FILE)
        check(
            payload["counts"]["pchembl_max"] == 8.0,
            f"最强活性记在产物里：{payload['counts']}",
        )
        rows = {}
        with (entry.path / ligand_module.CSV_FILE).open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows[row["id"]] = float(row["pchembl_value"])
        check(rows.get("C1") == 7.5, f"同一分子取最强那条：{rows}")
        check(rows.get("C3") is None, f"低于下限的不进来：{rows}")
        check(len(rows) == 3, f"三条：{rows}")

        # 形状必须是 prepare_ligands 能直接读的那种
        items, kind = ligand_module._source_cleaned(entry)
        check(len(items) == 3 and kind == "raw", f"可直接准备：{len(items)} 条")

        # 目录形态也认（`import_ligandset` 的产物）
        alt = work / "alt"
        alt.mkdir(parents=True, exist_ok=True)
        (alt / ligand_module.CSV_FILE).write_text(
            "id,name,smiles,pchembl_value\nX1,ethanol,CCO,7.0\n", encoding="utf-8"
        )
        products_module.write_json_atomic(
            alt / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "raw"}
        )
        alt_entry = ledger.add(
            "ligandset", path=alt, summary="目录形态",
            fingerprint="fp-alt", meta={"kind": "ligandset", "stage": "raw"},
            link_current_receptor=False,
        )
        from_dir = await build.execute({"actives": str(alt_entry.handle)}, context=ledger)
        check(
            not from_dir.failed and from_dir.meta.get("kept") == 1,
            f"目录形态也认：{from_dir.output}",
        )

        empty = await build.execute(
            {"actives": str(source.handle), "min_pchembl": 9}, context=ledger
        )
        check(
            empty.failed and "一条都不剩" in empty.output,
            f"全被筛掉要说清，不是空表：{empty.output}",
        )
        again = await build.execute({"actives": str(source.handle)}, context=ledger)
        check(again.meta.get("reused") is True, "同参第二次调用复用旧产物")
        no_ledger = await build.execute({"actives": str(source.handle)})
        check(
            no_ledger.failed and "内核装配问题" in no_ledger.output,
            f"没注入登记表是装配问题：{no_ledger.output}",
        )
    finally:
        ligand_module.reference_ligandset_dir = original_dir


async def check_screen_similarity() -> None:
    """2D 指纹筛选：最近邻、换 metric 不复用、SMARTS、cutoff、top 截断。"""
    work = PROBE / "screen_similarity"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    library_dir = work / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        ("aspirin", "CC(=O)Oc1ccccc1C(=O)O"),
        ("caffeine", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"),
        ("catechol", "Oc1ccccc1O"),
        ("ibuprofen", "CC(C)Cc1ccc(cc1)C(C)C(=O)O"),
    ]
    (library_dir / ligand_module.CSV_FILE).write_text(
        "id,name,smiles\n" + "".join(f"{name},{name},{smiles}\n" for name, smiles in rows),
        encoding="utf-8",
    )
    products_module.write_json_atomic(
        library_dir / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "raw"}
    )

    original_dir = screen_module.candidates_dir
    screen_module.candidates_dir = lambda: work / "candidates"

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        library = ledger.add(
            "ligandset", path=library_dir, summary="库",
            fingerprint="fp-lib", meta={"kind": "ligandset", "stage": "raw"},
            link_current_receptor=False,
        )
        screen = screen_module.ScreenSimilarityTool()

        result = await screen.execute(
            {"smiles": rows[0][1], "library": str(library.handle)}, context=ledger
        )
        check(not result.failed, f"相似度筛选成功：{result.output}")
        entry = ledger.get(result.meta["handle"])
        payload = products_module.read_json(entry.path / "candidates.json")
        check(payload.get("score_column") == "similarity", f"分数列写进产物：{payload.keys()}")
        check(payload.get("higher_is_better") is True, "方向写在产物里，不交给模型")
        top_row = payload["rows"][0]
        check(top_row["id"] == "aspirin", f"查询分子自己排第一：{top_row['id']}")
        check(abs(top_row["scores"]["similarity"] - 1.0) < 1e-9, f"自身相似度为 1：{top_row}")

        # 换 metric 不复用
        other = await screen.execute(
            {"smiles": rows[0][1], "library": str(library.handle), "metric": "maccs"},
            context=ledger,
        )
        check(
            other.meta["handle"] != result.meta["handle"],
            "换 metric 是新产物，不复用旧目录",
        )
        bad_metric = await screen.execute(
            {"smiles": rows[0][1], "library": str(library.handle), "metric": "daylight"},
            context=ledger,
        )
        check(bad_metric.failed, f"认不出的 metric 要拒绝：{bad_metric.output}")

        # SMARTS：只留带苯环的（咖啡因是咪唑环，不算）
        smarts = await screen.execute(
            {"smiles": "c1ccccc1", "library": str(library.handle), "smarts": "c1ccccc1"},
            context=ledger,
        )
        check(not smarts.failed, f"子结构检索成功：{smarts.output}")
        smarts_entry = ledger.get(smarts.meta["handle"])
        smarts_payload = products_module.read_json(smarts_entry.path / "candidates.json")
        check(
            {row["id"] for row in smarts_payload["rows"]}
            == {"catechol", "aspirin", "ibuprofen"},
            f"只有带苯环的命中：{[row['id'] for row in smarts_payload['rows']]}",
        )
        bad_smarts = await screen.execute(
            {"smiles": "CCO", "library": str(library.handle), "smarts": "[C1"},
            context=ledger,
        )
        check(
            bad_smarts.failed and "SMARTS 解析不了" in bad_smarts.output,
            f"坏 SMARTS 要说清哪一段：{bad_smarts.output}",
        )

        # cutoff 与 top
        filtered = await screen.execute(
            {"smiles": rows[0][1], "library": str(library.handle), "cutoff": 0.9},
            context=ledger,
        )
        check(
            filtered.meta.get("count") == 1,
            f"cutoff=0.9 只剩它自己：{filtered.meta}",
        )
        truncated = await screen.execute(
            {"smiles": rows[0][1], "library": str(library.handle), "top": 2},
            context=ledger,
        )
        check(truncated.meta.get("count") == 2, f"top=2 只留两条：{truncated.meta}")
        check(
            "调大 top" in truncated.output,
            f"截断要写明还有多少、怎么取：{truncated.output}",
        )

        both = await screen.execute(
            {"query": str(library.handle), "smiles": "CCO", "library": str(library.handle)},
            context=ledger,
        )
        check(both.failed and "只能给一个" in both.output, f"query 与 smiles 互斥：{both.output}")
        neither = await screen.execute({"library": str(library.handle)}, context=ledger)
        check(neither.failed, f"两个都不给要报错：{neither.output}")

        # 查询集当 query：整组当查询，取最近邻
        as_set = await screen.execute(
            {"query": str(library.handle), "library": str(library.handle)}, context=ledger
        )
        check(
            not as_set.failed and as_set.meta.get("count") == 4,
            f"整组当查询时每个都能找到自己：{as_set.meta}",
        )
    finally:
        screen_module.candidates_dir = original_dir


async def check_screen_shape() -> None:
    """3D 形状：USR 对齐无关、缺 3D 明说、读不出坐标逐条记录。"""
    work = PROBE / "screen_shape"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    def atom(serial: int, name: str, x: float, y: float, z: float, kind: str = "") -> str:
        head = (
            f"ATOM  {serial:>5} {name:<4} LIG A   1    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{0.00:>6.2f}"
        )
        return f"{head}    {0.000:>6.3f} {kind:<2}" if kind else head

    library_dir = work / "prepared"
    (library_dir / ligand_module.PDBQT_DIR).mkdir(parents=True, exist_ok=True)
    files = {
        # 一条直线
        "line": [atom(i + 1, "C", 3.0 * i, 0, 0) for i in range(4)],
        # 同一条直线，整体平移 20 Å：形状描述子与位置无关
        "line_shifted": [atom(i + 1, "C", 20.0 + 3.0 * i, 0, 0) for i in range(4)],
        # 一团
        "blob": [
            atom(1, "C", 0, 0, 0),
            atom(2, "C", 1.4, 0, 0),
            atom(3, "C", 0, 1.4, 0),
            atom(4, "C", 0, 0, 1.4),
        ],
        # 带一个 PDBQT 类型的氢：按类型判氢时它不该参与
        "with_hydrogen": [
            atom(1, "C", 0, 0, 0),
            atom(2, "C", 3.0, 0, 0),
            atom(3, "H1", 6.0, 0, 0, "HD"),
            atom(4, "C", 9.0, 0, 0),
        ],
        # 与上一条的**重原子**完全一样（0 / 3 / 9），没有那个氢。
        # 两条分数相同才说明氢真的没参与；拿它跟 0/3/6/9 比是比不出来的。
        "line_gap": [
            atom(1, "C", 0, 0, 0),
            atom(2, "C", 3.0, 0, 0),
            atom(3, "C", 9.0, 0, 0),
        ],
    }
    records = []
    for name, lines in files.items():
        (library_dir / ligand_module.PDBQT_DIR / f"{name}.pdbqt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        records.append(
            {"id": name, "name": name, "smiles": "C", "status": "ok",
             # **与 `prepare_ligands` 写出来的同形**：带 `ligands/` 前缀、相对产物根。
             # 早先这里是裸文件名，于是「多拼一次 pdbqt_dir」的 bug 一直测不出来。
             "pdbqt": f"{ligand_module.PDBQT_DIR}/{name}.pdbqt", "heavy_atoms": 4}
        )
    records.append(
        {"id": "failed_one", "name": "failed", "smiles": "C", "status": "embed_failed",
         "pdbqt": "", "heavy_atoms": 0}
    )
    products_module.write_json_atomic(
        library_dir / ligand_module.MANIFEST_FILE,
        {"kind": "ligandset", "stage": "prepared", "records": records},
    )

    # 查询侧只要一条「直线」：查询集里如果也含 blob，取最近邻会把它自己算成 1.0，
    # 那样「团状不如线状像」这条断言就永远绿。
    query_dir = work / "query"
    (query_dir / ligand_module.PDBQT_DIR).mkdir(parents=True, exist_ok=True)
    (query_dir / ligand_module.PDBQT_DIR / "line.pdbqt").write_text(
        "\n".join(files["line"]) + "\n", encoding="utf-8"
    )
    products_module.write_json_atomic(
        query_dir / ligand_module.MANIFEST_FILE,
        {
            "kind": "ligandset",
            "stage": "prepared",
            "records": [
                {"id": "line", "name": "line", "smiles": "C", "status": "ok",
                 "pdbqt": f"{ligand_module.PDBQT_DIR}/line.pdbqt", "heavy_atoms": 4}
            ],
        },
    )

    original_dir = screen_module.candidates_dir
    screen_module.candidates_dir = lambda: work / "candidates"

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        prepared = ledger.add(
            "ligandset", path=library_dir, summary="已备 3D",
            fingerprint="fp-3d", meta={"kind": "ligandset", "stage": "prepared",
                                        "pdbqt_dir": ligand_module.PDBQT_DIR},
            link_current_receptor=False,
        )
        screen = screen_module.ScreenShapeTool()
        query_entry = ledger.add(
            "ligandset", path=query_dir, summary="查询（一条直线）",
            fingerprint="fp-query", meta={"kind": "ligandset", "stage": "prepared",
                                          "pdbqt_dir": ligand_module.PDBQT_DIR},
            link_current_receptor=False,
        )

        result = await screen.execute(
            {"query": str(query_entry.handle), "library": str(prepared.handle)}, context=ledger
        )
        check(not result.failed, f"形状筛选成功：{result.output}")
        entry = ledger.get(result.meta["handle"])
        payload = products_module.read_json(entry.path / "candidates.json")
        scores = {row["id"]: row["scores"]["shape"] for row in payload["rows"]}
        check(
            abs(scores["line"] - 1.0) < 1e-9,
            f"自己跟自己形状分是 1：{scores}",
        )
        check(
            abs(scores["line_shifted"] - 1.0) < 1e-6,
            f"整体平移不改变形状分：{scores}",
        )
        check(
            scores["blob"] < scores["line"],
            f"团状比线状更不像线：{scores['blob']:.3f} < {scores['line']:.3f}",
        )
        check(
            abs(scores["with_hydrogen"] - scores["line_gap"]) < 1e-9,
            f"按 PDBQT 类型判氢：那个 HD 不参与，与没有氢的同构分子同分：{scores}",
        )
        check(
            payload.get("score_column") == "shape",
            "形状分单独一列，不混进 similarity",
        )

        # 查询用受体（共晶配体）
        receptor_dir = work / "receptor"
        receptor_dir.mkdir(parents=True, exist_ok=True)
        (receptor_dir / "ligand.pdb").write_text(
            "\n".join(line.replace("ATOM  ", "HETATM") for line in files["line"]) + "\n",
            encoding="utf-8",
        )
        receptor = ledger.add(
            "receptor", path=receptor_dir, summary="带共晶配体的受体",
            fingerprint="fp-rec",
            meta={"stage": "prepared", "ligand_file": "ligand.pdb"},
        )
        by_receptor = await screen.execute(
            {"query": str(receptor.handle), "library": str(prepared.handle)}, context=ledger
        )
        check(
            not by_receptor.failed,
            f"查询可以是受体的共晶配体：{by_receptor.output}",
        )
        rec_entry = ledger.get(by_receptor.meta["handle"])
        rec_payload = products_module.read_json(rec_entry.path / "candidates.json")
        top_id = rec_payload["rows"][0]["id"]
        check(
            top_id in {"line", "line_shifted", "with_hydrogen"},
            f"共晶配体是直线，排第一的也该是直线：{top_id}",
        )

        # 没准备 3D 直接说缺什么
        raw_dir = work / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / ligand_module.CSV_FILE).write_text(
            "id,name,smiles\nA,a,CCO\n", encoding="utf-8"
        )
        products_module.write_json_atomic(
            raw_dir / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "raw"}
        )
        raw = ledger.add(
            "ligandset", path=raw_dir, summary="没准备", fingerprint="fp-raw",
            meta={"kind": "ligandset", "stage": "raw"}, link_current_receptor=False,
        )
        not_prepared = await screen.execute(
            {"query": str(prepared.handle), "library": str(raw.handle)}, context=ledger
        )
        check(
            not_prepared.failed and "prepare_ligands" in not_prepared.output,
            f"缺 3D 时要提示先准备：{not_prepared.output}",
        )
        no_ledger = await screen.execute(
            {"query": str(prepared.handle), "library": str(prepared.handle)}
        )
        check(
            no_ledger.failed and "内核装配问题" in no_ledger.output,
            f"没注入登记表是装配问题：{no_ledger.output}",
        )
    finally:
        screen_module.candidates_dir = original_dir


async def check_validate_docking() -> None:
    """重对接：原地 RMSD（不重新叠合）、阈值、无共晶配体不假装通过、gate 进候选表。"""
    work = PROBE / "validate_docking"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    def line(kind: str, serial: int, name: str, x: float, y: float, z: float,
             element: str) -> str:
        return (
            f"{kind:<6}{serial:>5} {name:<4} {'LIG':>3} A{1:>4}    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.00:>6.2f}{20.00:>6.2f}          {element:>2}"
        )

    crystal = [
        line("HETATM", 1, "C1", 0.0, 0.0, 0.0, "C"),
        line("HETATM", 2, "C2", 1.5, 0.0, 0.0, "C"),
        line("HETATM", 3, "O1", 2.5, 1.0, 0.0, "O"),
        line("HETATM", 4, "N1", -1.0, 0.5, 0.0, "N"),
    ]
    receptor_dir = work / "receptor"
    receptor_dir.mkdir(parents=True, exist_ok=True)
    (receptor_dir / "ligand.pdb").write_text("\n".join(crystal) + "\n", encoding="utf-8")

    docking_dir = work / "docking"
    (docking_dir / "poses").mkdir(parents=True, exist_ok=True)
    # 完全复现晶体位置
    (docking_dir / "poses" / "perfect.pdb").write_text("\n".join(crystal) + "\n",
                                                       encoding="utf-8")
    # 整体平移 2.5 Å（原地 RMSD 应等于 2.5；若重新叠合会变成 0）
    shifted = [
        line("HETATM", 1, "C1", 2.5, 0.0, 0.0, "C"),
        line("HETATM", 2, "C2", 4.0, 0.0, 0.0, "C"),
        line("HETATM", 3, "O1", 5.0, 1.0, 0.0, "O"),
        line("HETATM", 4, "N1", 1.5, 0.5, 0.0, "N"),
    ]
    (docking_dir / "poses" / "shifted.pdb").write_text("\n".join(shifted) + "\n",
                                                       encoding="utf-8")
    # 原子数不同：不是同一个分子
    (docking_dir / "poses" / "other.pdb").write_text(
        "\n".join(crystal[:2]) + "\n", encoding="utf-8"
    )
    # 带显式氢的位姿：Vina 写出来的就是带氢的（真实数据：伊马替尼位姿 68 原子对
    # 共晶配体 37 个重原子）。`MolFromPDBBlock(removeHs=True)` 在 `sanitize=False`
    # 下是空转的，这一条专门盯「氢没删掉 → 每一行都变成原子数不同」。
    hydrogenated = crystal + [
        line("HETATM", 5, "H1", -1.9, 1.0, 0.0, "H"),
        line("HETATM", 6, "H2", 0.6, -1.0, 0.0, "H"),
    ]
    (docking_dir / "poses" / "hydrogenated.pdb").write_text(
        "\n".join(hydrogenated) + "\n", encoding="utf-8"
    )
    products_module.write_json_atomic(
        docking_dir / "manifest.json",
        {
            "kind": "docking",
            "stage": "docking",
            "receptor": {"handle": "PLACEHOLDER"},
            "results": [
                {"id": "perfect", "name": "perfect", "score": -9.0,
                 "pose_pdb": "poses/perfect.pdb"},
                {"id": "shifted", "name": "shifted", "score": -8.0,
                 "pose_pdb": "poses/shifted.pdb"},
                {"id": "other", "name": "other", "score": -7.0,
                 "pose_pdb": "poses/other.pdb"},
                {"id": "hydrogenated", "name": "hydrogenated", "score": -6.0,
                 "pose_pdb": "poses/hydrogenated.pdb"},
            ],
        },
    )
    products_module.write_json_atomic(docking_dir / "params.json", {})

    original_dir = analysis_module.validation_dir
    original_candidates = analysis_module.candidates_dir
    analysis_module.validation_dir = lambda: work / "validation"
    analysis_module.candidates_dir = lambda: work / "candidates"

    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        receptor = ledger.add(
            "receptor", path=receptor_dir, summary="带共晶配体",
            fingerprint="fp-rec",
            meta={"stage": "prepared", "ligand_file": "ligand.pdb", "ligand_label": "STI"},
        )
        # 清单里要写真实 handle：工具会核对「这份对接用的是不是这个受体」
        manifest_path = docking_dir / "manifest.json"
        manifest = products_module.read_json(manifest_path)
        manifest["receptor"]["handle"] = str(receptor.handle)
        products_module.write_json_atomic(manifest_path, manifest)
        docking_entry = ledger.add(
            "artifact", path=docking_dir, summary="对接",
            fingerprint=fingerprint_path(docking_dir),
            parents=[receptor.handle],
            meta={"kind": "docking", "stage": "docking"},
        )

        validate = analysis_module.ValidateDockingTool()
        result = await validate.execute(
            {"receptor": str(receptor.handle), "docking": str(docking_entry.handle)},
            context=ledger,
        )
        check(not result.failed, f"重对接验证成功：{result.output}")
        entry = ledger.get(result.meta["handle"])
        rows = {row["id"]: row for row in entry.meta["results"]}
        check(abs(rows["perfect"]["rmsd"]) < 1e-6, f"复现晶体位置 RMSD=0：{rows['perfect']}")
        check(
            abs(rows["shifted"]["rmsd"] - 2.5) < 1e-6,
            f"平移 2.5 Å 的原地 RMSD 是 2.5（重新叠合会把它算成 0）：{rows['shifted']}",
        )
        check(
            rows["perfect"]["pass"] is True and rows["shifted"]["pass"] is False,
            f"2.0 Å 阈值下只有一个通过：{rows}",
        )
        check(
            rows["other"]["rmsd"] is None and "原子数不同" in rows["other"]["detail"],
            f"不是同一个分子要逐条说明：{rows['other']}",
        )
        check(
            rows["hydrogenated"]["rmsd"] is not None
            and abs(rows["hydrogenated"]["rmsd"]) < 1e-6,
            f"位姿里带显式氢也要先删氢再比（Vina 写出来的就带氢）：{rows['hydrogenated']}",
        )
        # 键级是读 PDB 时猜出来的，两边猜法可以不同：Vina 位姿带 CONECT、会读成
        # 24 根芳香键，共晶配体 `ligand.pdb` 读成 41 根单键。按键级比，同一张图也对不上。
        # 这里造一对真家伙：同一份苯环、同一套坐标，一份按 RDKit 写出（读回来是
        # 交替单双键），一份压成纯拓扑再写出（读回来全单键）。压平之后必须相等。
        from rdkit import Chem as _Chem
        from rdkit.Chem import AllChem as _AllChem

        benzene = _Chem.MolFromSmiles("c1ccccc1")
        _AllChem.EmbedMolecule(benzene, randomSeed=7)
        kekule_pose = work / "benzene-kekule.pdb"
        kekule_pose.write_text(_Chem.MolToPDBBlock(benzene), encoding="utf-8")
        single_reference = work / "benzene-single.pdb"
        single_reference.write_text(
            _Chem.MolToPDBBlock(analysis_module._topology(benzene)), encoding="utf-8"
        )
        order_insensitive, reason = analysis_module._rmsd(kekule_pose, single_reference)
        check(
            order_insensitive is not None and abs(order_insensitive) < 1e-6,
            "只比拓扑：键级读法不同（芳香/交替单双 vs 全单键）不影响判同一个分子，"
            f"否则真位姿永远对不上：{order_insensitive} {reason}",
        )
        check(
            entry.meta.get("kind") == "validation",
            f"登记成 validation：{entry.meta.get('kind')}",
        )
        check(
            [str(parent) for parent in entry.parents]
            == [str(receptor.handle), str(docking_entry.handle)],
            f"血缘记了受体与对接：{entry.parents}",
        )
        check("原地" in entry.meta.get("method", ""), "口径写进产物")

        loose = await validate.execute(
            {"receptor": str(receptor.handle), "docking": str(docking_entry.handle),
             "cutoff": 3.0},
            context=ledger,
        )
        loose_entry = ledger.get(loose.meta["handle"])
        loose_rows = {row["id"]: row for row in loose_entry.meta["results"]}
        check(loose_rows["shifted"]["pass"] is True, "阈值放宽到 3.0 后 2.5 Å 也算通过")

        # gate 进候选表：rank_candidates 读得到 redock_rmsd
        rank = analysis_module.RankCandidatesTool()
        ranked = await rank.execute(
            {"sources": str(docking_entry.handle)}, context=ledger
        )
        check(not ranked.failed, f"候选表能读这份对接：{ranked.output}")
        ranked_entry = ledger.get(ranked.meta["handle"])
        payload = products_module.read_json(
            ranked_entry.path / analysis_module.CANDIDATES_FILE
        )
        gates = {row["id"]: row["gates"] for row in payload["rows"]}
        check(
            gates["shifted"].get("redock_rmsd") == 2.5,
            f"重对接 RMSD 作为门禁进候选表：{gates}",
        )
        check(
            "perfect" in gates and "redock_rmsd" in gates["perfect"],
            "通过的那条也带上门禁值（0.0）",
        )

        # 没有共晶配体的受体：明说做不了
        bare_dir = work / "bare"
        bare_dir.mkdir(parents=True, exist_ok=True)
        bare = ledger.add(
            "receptor", path=bare_dir, summary="无配体", fingerprint="fp-bare",
            meta={"stage": "prepared"},
        )
        no_ligand = await validate.execute(
            {"receptor": str(bare.handle), "docking": str(docking_entry.handle)},
            context=ledger,
        )
        check(
            no_ligand.failed and "没法做重对接" in no_ligand.output,
            f"没共晶配体不能假装通过：{no_ligand.output}",
        )

        raw_receptor = ledger.add(
            "receptor", path=receptor_dir, summary="没准备的受体", fingerprint="fp-raw",
            meta={"stage": "raw"},
        )
        not_prepared = await validate.execute(
            {"receptor": str(raw_receptor.handle), "docking": str(docking_entry.handle)},
            context=ledger,
        )
        check(
            not_prepared.failed and "还没有准备过" in not_prepared.output,
            f"没准备过的受体要拒绝：{not_prepared.output}",
        )
    finally:
        analysis_module.validation_dir = original_dir
        analysis_module.candidates_dir = original_candidates


async def check_batch_offset() -> None:
    """分批：两批合起来覆盖全部、不重叠；越界说清；换 offset 不复用；不给 offset 行为不变。"""
    work = PROBE / "batch_offset"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # ---- 一、先验纯函数：切片不重叠、不漏 ---
    from vsagent.core.agent.domain.ligand import PrepItem, Cleaned

    items = [
        PrepItem(
            id=f"m{index}",
            name=f"m{index}",
            source_index=index,
            cleaned=Cleaned(input_smiles="C", smiles=f"C{index}", heavy_atoms=1,
                            fragments_removed=0),
        )
        for index in range(5)
    ]
    first, first_counts = ligand_module._unique_items(items, limit=2, offset=0)
    second, second_counts = ligand_module._unique_items(items, limit=2, offset=2)
    head_ids = [item.id for item in first]
    tail_ids = [item.id for item in second]
    check(head_ids == ["m0", "m1"], f"第一批取前两个：{head_ids}")
    check(tail_ids == ["m2", "m3"], f"第二批从第 3 个开始：{tail_ids}")
    check(not set(head_ids) & set(tail_ids), "两批不重叠")
    check(
        first_counts["remaining"] == 3 and second_counts["remaining"] == 1,
        f"remaining 是这一批之后还剩多少：{first_counts} / {second_counts}",
    )

    original_dir = docking_module.dockings_dir
    original_resolve = docking_module.resolve_vina
    original_run = docking_module.run_vina
    original_convert = docking_module.best_pose_to_pdb
    original_prepared = ligand_module.prepared_ligandset_dir
    original_probe = chemistry_module._probe_deps

    seen: list[str] = []

    async def fake_run_vina(argv, *, cwd, timeout, log_path) -> Any:
        ligand_path = Path(argv[argv.index("--ligand") + 1])
        out_path = Path(argv[argv.index("--out") + 1])
        seen.append(ligand_path.stem)
        out_path.write_text(
            "MODEL 1\nREMARK VINA RESULT: -7.0 0.0 0.0\nATOM\nENDMDL\n", encoding="utf-8"
        )
        log_path.write_text("ok\n", encoding="utf-8")
        return docking_module.VinaRun(tuple(argv), 0, "ok", False, log_path)

    def fake_convert(pose_pdbqt: Path, dest: Path) -> bool:
        dest.write_text("ATOM      1  C   LIG A   1       0.000   0.000   0.000\n",
                        encoding="utf-8")
        return True

    try:
        docking_module.dockings_dir = lambda: work / "docking"
        docking_module.resolve_vina = lambda: docking_module.VinaBinary("/fake/vina", "1.2.5")
        docking_module.run_vina = fake_run_vina
        docking_module.best_pose_to_pdb = fake_convert
        ligand_module.prepared_ligandset_dir = lambda: work / "prepared"
        chemistry_module._probe_deps = lambda: ()

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
            "receptor", path=receptor_dir, summary="受体",
            fingerprint=fingerprint_path(receptor_dir),
            meta={"stage": "prepared", "pdbqt_file": "receptor.pdbqt", "box_file": "box.txt"},
        )

        ligand_dir = work / "ligands"
        (ligand_dir / "ligands").mkdir(parents=True, exist_ok=True)
        records = []
        for name in ["A", "B", "C", "D", "E"]:
            (ligand_dir / "ligands" / f"{name}.pdbqt").write_text("ATOM\n", encoding="utf-8")
            records.append(
                {"id": name, "name": name, "smiles": "C", "status": "ok",
                 "pdbqt": f"ligands/{name}.pdbqt", "heavy_atoms": 1}
            )
        products_module.write_json_atomic(
            ligand_dir / "manifest.json",
            {"kind": "ligandset", "stage": "prepared", "records": records, "counts": {}},
        )
        ligand_set = ledger.add(
            "ligandset", path=ligand_dir, summary="五个配体",
            fingerprint=fingerprint_path(ligand_dir),
            meta={"stage": "prepared"}, link_current_receptor=False,
        )

        tool = docking_module.DockTool()
        batches = []
        for offset in (0, 2, 4):
            result = await tool.execute(
                {
                    "receptor": str(receptor.handle),
                    "ligands": str(ligand_set.handle),
                    "offset": offset,
                    "limit": 2,
                },
                context=ledger,
            )
            check(not result.failed, f"offset={offset} 对接成功：{result.output}")
            batches.append(result)

        ids = [
            [row["id"] for row in
             products_module.read_json(ledger.get(result.meta["handle"]).path / "manifest.json")["results"]]
            for result in batches
        ]
        check(ids[0] == ["A", "B"], f"第一批是 A/B：{ids[0]}")
        check(ids[1] == ["C", "D"], f"第二批是 C/D：{ids[1]}")
        check(ids[2] == ["E"], f"第三批是 E：{ids[2]}")
        check(
            len({str(ledger.get(result.meta["handle"]).path) for result in batches}) == 3,
            "三批是三个产物目录（offset 进缓存键）",
        )
        check(
            batches[0].meta.get("remaining") == 3
            and batches[1].meta.get("remaining") == 1
            and batches[2].meta.get("remaining") == 0,
            f"remaining 按这一批算：{[b.meta.get('remaining') for b in batches]}",
        )

        beyond = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "ligands": str(ligand_set.handle),
                "offset": 9,
            },
            context=ledger,
        )
        check(
            beyond.failed and "越过末尾" in beyond.output and "5" in beyond.output,
            f"offset 越界要说清只有几条：{beyond.output}",
        )

        again = await tool.execute(
            {
                "receptor": str(receptor.handle),
                "ligands": str(ligand_set.handle),
                "offset": 0,
                "limit": 2,
            },
            context=ledger,
        )
        check(again.meta.get("reused") is True, "同参第二次调用复用旧产物")

        legacy = await tool.execute(
            {"receptor": str(receptor.handle), "ligands": str(ligand_set.handle), "limit": 2},
            context=ledger,
        )
        check(
            not legacy.failed
            and legacy.meta.get("handle") == batches[0].meta.get("handle"),
            "不给 offset 等价于 offset=0（命中同一份产物）",
        )

        # ---- 二、prepare_ligands 的 offset 是真跑 Meeko ----
        raw_dir = work / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / ligand_module.CSV_FILE).write_text(
            "id,name,smiles\nm1,a,CCO\nm2,b,CCCl\nm3,c,CCC\n", encoding="utf-8"
        )
        products_module.write_json_atomic(
            raw_dir / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "raw"}
        )
        raw = ledger.add(
            "ligandset", path=raw_dir, summary="三个分子",
            fingerprint="fp-raw-offset", meta={"kind": "ligandset", "stage": "raw"},
            link_current_receptor=False,
        )
        prepare = ligand_module.PrepareLigandsTool()
        one = await prepare.execute(
            {"ligands": str(raw.handle), "offset": 0, "limit": 1, "confirm": True},
            context=ledger,
        )
        two = await prepare.execute(
            {"ligands": str(raw.handle), "offset": 1, "limit": 1, "confirm": True},
            context=ledger,
        )
        check(not one.failed and not two.failed, f"两批准备都成功：{one.output} / {two.output}")
        first_entry = ledger.get(one.meta["handle"])
        second_entry = ledger.get(two.meta["handle"])
        check(
            first_entry.path != second_entry.path,
            "两批准备落在两个目录（offset 进缓存键）",
        )
        first_ids = [
            row["id"] for row in
            products_module.read_json(first_entry.path / ligand_module.MANIFEST_FILE)["records"]
            if row.get("status") == "ok"
        ]
        second_ids = [
            row["id"] for row in
            products_module.read_json(second_entry.path / ligand_module.MANIFEST_FILE)["records"]
            if row.get("status") == "ok"
        ]
        check(first_ids == ["m1"], f"第一批只准备 m1：{first_ids}")
        check(second_ids == ["m2"], f"第二批只准备 m2：{second_ids}")
        check(not set(first_ids) & set(second_ids), "两批准备的不是同一个分子")

        empty = await prepare.execute(
            {"ligands": str(raw.handle), "offset": 99, "limit": 1, "confirm": True},
            context=ledger,
        )
        check(
            empty.failed and "越过末尾" in empty.output,
            f"准备的 offset 越界也要说清：{empty.output}",
        )
    finally:
        docking_module.dockings_dir = original_dir
        docking_module.resolve_vina = original_resolve
        docking_module.run_vina = original_run
        docking_module.best_pose_to_pdb = original_convert
        ligand_module.prepared_ligandset_dir = original_prepared
        chemistry_module._probe_deps = original_probe


async def check_mmcif() -> None:
    """只有 mmCIF 的条目：装了 gemmi 就转换并登记，转换失败不写盘不登记。"""
    work = PROBE / "mmcif"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    store = work / "structures"

    calls: list[tuple[str, str]] = []

    def get_text_or_none(url, params=None, *, timeout=45.0, headers=None):
        calls.append(("GET", url))
        if url.endswith(".cif"):
            return _cif_fixture()
        return None  # .pdb 一律 404：这就是「只有 mmCIF」

    original_text = structure_module.http.get_text_or_none
    original_dir = structure_module.structure_dir
    original_cif = structure_module._cif_to_pdb
    structure_module.http.get_text_or_none = get_text_or_none
    structure_module.structure_dir = lambda: store
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        fetch = structure_module.FetchStructureTool()

        result = await fetch.execute({"pdb_id": "6VXX"}, context=ledger)
        check(not result.failed, f"mmCIF 能转换并登记：{result.output.splitlines()[0]}")
        entry = ledger.entries()[0]
        check(
            entry.meta.get("source_format") == "mmCIF",
            f"登记时记下来源格式：{entry.meta.get('source_format')}",
        )
        check(entry.meta.get("format") == "pdb", "磁盘上那份确实是 PDB")
        check(
            result.meta.get("chains") == ["A"],
            f"转换后的坐标能读出链：{result.meta.get('chains')}",
        )
        text = (store / "6VXX.pdb").read_text(encoding="utf-8")
        check(
            text.startswith(structure_module.CIF_CONVERTED_MARKER),
            "写盘的文件带一行来源标记（读盘时才认得出它从哪来）",
        )

        before = len(calls)
        again = await fetch.execute({"pdb_id": "6VXX"}, context=ledger)
        check(not again.failed, "第二次取同一个编号成功")
        check(len(calls) == before, f"第二次读盘、不联网：新增 {len(calls) - before} 次")
        check(
            ledger.entries()[0].meta.get("source_format") == "mmCIF",
            "读盘时仍认得出它原本是 mmCIF",
        )
        check(len(ledger.entries()) == 1, "重新读取不重复登记")

        # 转换失败：不登记、不写半截文件
        def broken(_text: str) -> str:
            raise ValueError("没有坐标记录")

        structure_module._cif_to_pdb = broken
        entries_before = len(ledger.entries())
        failed = await fetch.execute({"pdb_id": "7ABC", "refresh": True}, context=ledger)
        check(failed.failed, "转换失败要报失败")
        check(
            "转换成 PDB 失败" in failed.output and "import_structure" in failed.output,
            f"说清失败原因并给出替代做法：{failed.output}",
        )
        check(
            len(ledger.entries()) == entries_before,
            "转换失败不登记（登记了就会指着一个不存在的产物）",
        )
        check(not (store / "7ABC.pdb").exists(), "转换失败不写半截文件")
        structure_module._cif_to_pdb = original_cif

        check(
            structure_module._source_format("ATOM      1  N   ALA A   1\n") == "PDB",
            "没有标记的文件算 PDB",
        )
    finally:
        structure_module.http.get_text_or_none = original_text
        structure_module.structure_dir = original_dir
        structure_module._cif_to_pdb = original_cif


async def check_assess_structure() -> None:
    """结构评估：缺口来自 SEQRES 差集、无 SEQRES 报「无法判断」、NMR 只算第一套。"""
    work = PROBE / "assess_structure"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    full = work / "1M17.pdb"
    full.write_text(_pdb_fixture(), encoding="utf-8")

    bare_text = "".join(
        [
            "EXPDTA    X-RAY DIFFRACTION\n",
            "REMARK   2 RESOLUTION.    2.10 ANGSTROMS.\n",
            _pdb_atom(1, "CA", "ALA", "A", 1, 1.0, 1.0, 1.0),
            _pdb_atom(2, "CA", "GLY", "A", 2, 2.0, 1.0, 1.0),
            # 一个锌离子：金属要单独报，不能混进配体
            _pdb_atom(3, "ZN", "ZN", "A", 900, 3.0, 3.0, 3.0, kind="HETATM", element="ZN"),
            "END\n",
        ]
    )
    bare = work / "bare.pdb"
    bare.write_text(bare_text, encoding="utf-8")

    nmr_text = "".join(
        [
            "EXPDTA    SOLUTION NMR\n",
            "SEQRES   1 A    2  MET ALA\n",
            _pdb_atom(1, "CA", "MET", "A", 1, 1.0, 1.0, 1.0),
            _pdb_atom(2, "CA", "ALA", "A", 2, 2.0, 1.0, 1.0),
            "END\n",
        ]
    )
    nmr = work / "nmr.pdb"
    nmr.write_text(nmr_text, encoding="utf-8")

    prepared_dir = work / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    (prepared_dir / "receptor.pdb").write_text(bare_text, encoding="utf-8")

    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    raw = ledger.add(
        "receptor", path=full, summary="1M17", fingerprint=fingerprint_text(_pdb_fixture()),
        meta={"stage": "raw", "pdb_id": "1M17"},
    )
    bare_entry = ledger.add(
        "receptor", path=bare, summary="裸结构", fingerprint=fingerprint_text(bare_text),
        meta={"stage": "raw"},
    )
    nmr_entry = ledger.add(
        "receptor", path=nmr, summary="NMR", fingerprint=fingerprint_text(nmr_text),
        meta={"stage": "raw"},
    )
    prepared = ledger.add(
        "receptor", path=prepared_dir, summary="准备过的", fingerprint="fp-prep",
        meta={"stage": "prepared", "pdb_file": "receptor.pdb"},
    )
    not_a_receptor = ledger.add(
        "ligandset", path=full, summary="不是受体", fingerprint="fp-not-receptor",
        meta={"stage": "raw"}, link_current_receptor=False,
    )

    assess = structure_module.AssessStructureTool()
    before = len(ledger.entries(include_archived=True))

    result = await assess.execute({"structure": str(raw.handle)}, context=ledger)
    check(not result.failed, f"结构评估成功：{result.output.splitlines()[0]}")
    check(
        result.meta.get("method") == "X-RAY DIFFRACTION",
        f"方法读出来了：{result.meta.get('method')}",
    )
    check(
        result.meta.get("resolution") == 2.6,
        f"分辨率读出来了：{result.meta.get('resolution')}",
    )
    check(result.meta.get("models") == 2, f"模型套数：{result.meta.get('models')}")
    check(
        result.output.count("缺失残基：2（声明 5、观测 3") == 1,
        f"缺口按 SEQRES 差集算，并写明声明/观测：{result.output}",
    )
    check(
        "交替构象标记：B" in result.output,
        f"交替构象写出来（准备时要选一个）：{result.output}",
    )
    check("AQ4" in result.output, "共晶配体列出来")
    check("水原子：1" in result.output, f"水原子数：{result.output}")

    no_seqres = await assess.execute({"structure": str(bare_entry.handle)}, context=ledger)
    check(
        "无法判断" in no_seqres.output,
        f"没有 SEQRES 时说「无法判断」，不报 0：{no_seqres.output}",
    )
    check(
        no_seqres.meta.get("missing_residues") is None,
        f"meta 里也不写成 0：{no_seqres.meta.get('missing_residues')}",
    )
    check(
        "金属：ZN×1" in no_seqres.output and no_seqres.meta.get("metals") == ["ZN"],
        f"金属单独报：{no_seqres.output}",
    )
    check(
        "共晶配体候选" not in no_seqres.output,
        f"金属不算「共晶配体候选」（那会让人以为锌是配体）：{no_seqres.output}",
    )

    nmr_result = await assess.execute({"structure": str(nmr_entry.handle)}, context=ledger)
    check(
        "NMR" in nmr_result.output and "第一套" in nmr_result.output,
        f"NMR 的结论要提第一套模型：{nmr_result.output}",
    )

    prepared_result = await assess.execute(
        {"structure": str(prepared.handle)}, context=ledger
    )
    check(
        not prepared_result.failed,
        f"准备过的受体也收（坐标在目录里）：{prepared_result.output}",
    )

    wrong = await assess.execute({"structure": str(not_a_receptor.handle)}, context=ledger)
    check(wrong.failed, f"不是受体要拒绝：{wrong.output}")
    no_ledger = await assess.execute({"structure": str(raw.handle)})
    check(
        no_ledger.failed and "内核装配问题" in no_ledger.output,
        f"没注入登记表是装配问题：{no_ledger.output}",
    )
    after = len(ledger.entries(include_archived=True))
    check(after == before, f"只读：调用前后登记表条目数不变（{before} → {after}）")


async def check_admet() -> None:
    """类药性规则层：每列的来源、公式进产物、做不了的端点标 unsupported。"""
    work = PROBE / "admet"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    library = work / "library"
    library.mkdir(parents=True, exist_ok=True)
    rows = [
        # 苯：小、疏水、无 TPSA
        ("benzene", "c1ccccc1"),
        # 葡萄糖：TPSA 110 还没到 Veber 的 140，也还没到 BBB 的 120 —— 落在「边界」
        ("glucose", "OCC1OC(O)C(O)C(O)C1O"),
        # 磷酸糖：TPSA 157，Veber 与 BBB 都该被挡下
        ("sugar_phos", "O=P(O)(O)OCC1OC(O)C(O)C(O)C1O"),
        # 苯胺片段：CYP 警示里应该有 aniline
        ("aniline", "Nc1ccccc1"),
        # 长链：可旋转键多
        ("long", "C" * 30 + "C(=O)O"),
    ]
    (library / ligand_module.CSV_FILE).write_text(
        "id,name,smiles\n" + "".join(f"{i},{i},{s}\n" for i, s in rows), encoding="utf-8"
    )
    products_module.write_json_atomic(
        library / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "raw"}
    )

    original_dir = admet_module.admet_dir
    admet_module.admet_dir = lambda: work / "admet"
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        source = ledger.add(
            "ligandset", path=library, summary="库", fingerprint="fp-admet",
            meta={"kind": "ligandset", "stage": "raw"}, link_current_receptor=False,
        )
        tool = admet_module.AdmetTool()
        result = await tool.execute(
            {"ligands": str(source.handle), "endpoints": "solubility,veber,bbb,cyp"},
            context=ledger,
        )
        check(not result.failed, f"类药性预筛成功：{result.output.splitlines()[0]}")
        entry = ledger.get(result.meta["handle"])
        check(entry.meta.get("stage") == "admet", f"阶段是 admet：{entry.meta.get('stage')}")
        payload = products_module.read_json(entry.path / ligand_module.MANIFEST_FILE)

        columns = payload.get("columns") or {}
        check(
            all(item.get("source") in ("rule", "unsupported") for item in columns.values()),
            f"每列都有来源，且只有 rule / unsupported：{columns}",
        )
        check(
            columns.get("hppb", {}).get("source") == "unsupported"
            and columns.get("cyp_inhibition", {}).get("source") == "unsupported",
            f"做不了的端点明确标 unsupported：{columns.get('hppb')}",
        )
        check(
            payload.get("formula_version"),
            f"公式版本写进产物：{payload.get('formula_version')}",
        )
        check(
            admet_module.ESOL_FORMULA in (columns.get("esol_logs", {}).get("method") or ""),
            "ESOL 的公式与系数一起写进产物（结果要能复核）",
        )

        table: dict[str, dict[str, str]] = {}
        with (entry.path / ligand_module.CSV_FILE).open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                table[row["id"]] = row
        check(len(table) == 5, f"五条都进表：{sorted(table)}")
        check(
            table["benzene"]["veber_pass"] == "是"
            and table["sugar_phos"]["veber_pass"] == "否",
            f"Veber：苯通过、磷酸糖被 TPSA 挡下：{table['sugar_phos']['veber_note']}",
        )
        check(
            table["sugar_phos"]["bbb_class"] == "不太可能",
            f"TPSA 157 透不过血脑屏障：{table['sugar_phos']['bbb_class']}",
        )
        check(
            table["glucose"]["bbb_class"] == "边界",
            f"TPSA 110 落在边界（不是「不太可能」）：{table['glucose']['bbb_class']}",
        )
        check(
            table["aniline"]["cyp_alerts"] == "aniline",
            f"CYP 警示子结构命中苯胺：{table['aniline']['cyp_alerts']!r}",
        )
        check(
            table["long"]["veber_pass"] == "否" and "可旋转" in table["long"]["veber_note"],
            f"长链被可旋转键挡下：{table['long']['veber_note']}",
        )
        check(
            float(table["glucose"]["esol_logs"]) > float(table["benzene"]["esol_logs"]),
            f"极性的葡萄糖比苯更溶（logS 更大）：{table['glucose']['esol_logs']} / "
            f"{table['benzene']['esol_logs']}",
        )

        subset = await tool.execute(
            {"ligands": str(source.handle), "endpoints": "veber"}, context=ledger
        )
        check(
            not subset.failed and subset.meta["handle"] != result.meta["handle"],
            "换端点组合是另一份产物",
        )
        unsupported = await tool.execute(
            {"ligands": str(source.handle), "endpoints": "hppb"}, context=ledger
        )
        check(
            unsupported.failed and "hppb" in unsupported.output,
            f"要一个做不到的端点要明确拒绝：{unsupported.output}",
        )
        no_ledger = await tool.execute({"ligands": str(source.handle)})
        check(
            no_ledger.failed and "内核装配问题" in no_ledger.output,
            f"没注入登记表是装配问题：{no_ledger.output}",
        )
    finally:
        admet_module.admet_dir = original_dir


async def check_sar() -> None:
    """构效关系：骨架分组、匹配分子对、类似物枚举、实验回填。"""
    work = PROBE / "sar"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # 候选表夹具：同一个苯环上换取代基，分数差得开
    candidates_dir = work / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": "c1", "name": "ethoxy", "smiles": "CCOc1ccccc1", "scores": {"dock": -9.0}},
        {"id": "c2", "name": "propoxy", "smiles": "CCCOc1ccccc1", "scores": {"dock": -7.0}},
        {"id": "c3", "name": "amino", "smiles": "CCNc1ccccc1", "scores": {"dock": -8.0}},
        {"id": "c4", "name": "chain", "smiles": "CCOCC", "scores": {"dock": -6.0}},
    ]
    products_module.write_json_atomic(
        candidates_dir / "candidates.json",
        {
            "kind": "candidates",
            "score_column": "dock",
            "higher_is_better": False,
            "rows": [
                {"id": row["id"], "name": row["name"], "smiles": row["smiles"],
                 "scores": row["scores"]}
                for row in rows
            ],
        },
    )
    products_module.write_json_atomic(candidates_dir / "params.json", {})

    original_sar = sar_module.sar_dir
    original_ligands = ligand_module.ligands_dir
    original_runs = export_module.runs_dir
    sar_module.sar_dir = lambda: work / "sar"
    ligand_module.ligands_dir = lambda: work / "ligands"
    export_module.runs_dir = lambda: work / "runs"
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        source = ledger.add(
            "artifact", path=candidates_dir, summary="候选表",
            fingerprint=fingerprint_path(candidates_dir),
            meta={"kind": "candidates", "score_column": "dock", "higher_is_better": False},
        )

        # ---- 一、骨架分组 ----
        scaffold_tool = sar_module.ScaffoldAnalysisTool()
        grouped = await scaffold_tool.execute(
            {"source": str(source.handle), "min_size": 1}, context=ledger
        )
        check(not grouped.failed, f"骨架分组成功：{grouped.output.splitlines()[0]}")
        check(grouped.meta.get("groups") == 2, f"两个骨架组：{grouped.meta}")
        entry = ledger.get(grouped.meta["handle"])
        payload = products_module.read_json(entry.path / "manifest.json")
        first = payload["groups"][0]
        check(first["scaffold"] == "c1ccccc1", f"苯环是第一个骨架：{first['scaffold']}")
        check(first["size"] == 3, f"苯环组有三个分子：{first['size']}")
        check(
            first["representative"]["id"] == "c1",
            f"代表分子是组内分数最好的（dock 越低越好）：{first['representative']['id']}",
        )
        check(
            payload["groups"][-1]["scaffold"] == "",
            f"无环分子归成 acyclic：{payload['groups'][-1]}",
        )

        # ---- 二、匹配分子对 ----
        mmp_tool = sar_module.MatchedPairsTool()
        pairs = await mmp_tool.execute(
            {"source": str(source.handle), "min_delta": 0.5}, context=ledger
        )
        check(not pairs.failed, f"匹配分子对成功：{pairs.output.splitlines()[0]}")
        pairs_entry = ledger.get(pairs.meta["handle"])
        pair_rows = products_module.read_json(pairs_entry.path / "manifest.json")["pairs"]
        check(len(pair_rows) >= 1, f"至少找到一对：{pairs.meta}")
        top_pair = pair_rows[0]
        check(top_pair["better"]["id"] == "c1", f"dock 越低越好，c1 是「更优」侧：{top_pair['better']}")
        check(
            abs(top_pair["delta"]) >= 0.5,
            f"delta 按下限过滤，且恒为正：{top_pair['delta']}",
        )
        check(
            pair_rows == sorted(pair_rows, key=lambda item: -item["delta"]),
            "按 delta 降序",
        )
        no_pairs = await mmp_tool.execute(
            {"source": str(source.handle), "min_delta": 99}, context=ledger
        )
        check(
            not no_pairs.failed and no_pairs.meta.get("pairs") == 0,
            f"没有达到差值的对子时给空表并说明：{no_pairs.output.splitlines()[:2]}",
        )

        # ---- 三、类似物枚举 ----
        enum_tool = sar_module.EnumerateAnalogsTool()
        analogs = await enum_tool.execute(
            {"scaffold": str(grouped.meta["handle"]), "group": 1, "max": 20}, context=ledger
        )
        check(not analogs.failed, f"枚举成功：{analogs.output.splitlines()[0]}")
        analog_entry = ledger.get(analogs.meta["handle"])
        check(
            analog_entry.meta.get("stage") == "raw",
            f"枚举产物是 raw 配体集，可直接准备：{analog_entry.meta.get('stage')}",
        )
        with (analog_entry.path / ligand_module.CSV_FILE).open(encoding="utf-8") as handle:
            made = [row for row in csv.DictReader(handle)]
        check(len(made) >= 1, f"生成了分子：{len(made)}")
        originals = {row["smiles"] for row in rows}
        check(
            all(row["smiles"] not in originals for row in made),
            "组内原有的分子不算「新类似物」",
        )
        check(
            len({row["smiles"] for row in made}) == len(made),
            "生成结果不重复",
        )
        check(
            all(row["id"] and row["heavy_atoms"] for row in made),
            "每条都有 id 与重原子数（prepare_ligands 要用）",
        )
        acyclic_group = await enum_tool.execute(
            {"scaffold": str(grouped.meta["handle"]), "group": 2}, context=ledger
        )
        check(
            acyclic_group.failed and "acyclic" in acyclic_group.output,
            f"无环组没有核心，要说清而不是生成空表：{acyclic_group.output}",
        )
        bad_group = await enum_tool.execute(
            {"scaffold": str(grouped.meta["handle"]), "group": 9}, context=ledger
        )
        check(bad_group.failed, f"组号越界要拒绝：{bad_group.output}")

        # ---- 四、共同子结构 ----
        features_tool = sar_module.CommonFeaturesTool()
        features = await features_tool.execute(
            {"source": str(source.handle), "min_atoms": 6}, context=ledger
        )
        check(not features.failed, f"共同子结构成功：{features.output.splitlines()[0]}")
        check(
            features.meta.get("ring") == 3 and features.meta.get("acyclic") == 1,
            f"三个有环、一个无环：{features.meta}",
        )
        check(
            features.meta.get("covered") == 3,
            f"苯环覆盖全部三个有环分子：{features.meta}",
        )
        check(features.meta.get("atoms") == 6, f"共同部分就是苯环：{features.meta}")
        from rdkit import Chem as _Chem_features

        pattern = _Chem_features.MolFromSmarts(str(features.meta.get("smarts") or ""))
        check(pattern is not None, f"报出的 SMARTS 能解析：{features.meta.get('smarts')}")
        check(
            "无环的 1 个" in features.output and "c4" in features.output,
            f"无环分子单独列出，不混进覆盖率：{features.output}",
        )
        # 默认 8 个原子：这三个分子只共有苯环，要如实说「不够」，而不是拿 6 个顶上去
        strict = await features_tool.execute(
            {"source": str(source.handle)}, context=ledger
        )
        check(
            "全局没有达到 8 个原子的共同子结构" in strict.output,
            f"默认阈值下如实报不够：{strict.output.splitlines()[1]}",
        )
        check(
            strict.meta.get("covered") == 3,
            f"覆盖数照给，读的人能自己判断这 6 个原子值不值：{strict.meta}",
        )
        bad_atoms = await features_tool.execute(
            {"source": str(source.handle), "min_atoms": 1}, context=ledger
        )
        check(
            bad_atoms.failed and "min_atoms" in bad_atoms.output,
            f"阈值越界要拒绝：{bad_atoms.output}",
        )

        # ---- 五、实验回填 ----
        assay_csv = work / "assay-in.csv"
        assay_csv.write_text(
            "molecule,value\nc1,>10000\nc2,120\nc3,not-a-number\n", encoding="utf-8"
        )
        assay_tool = sar_module.RecordAssayTool()
        recorded = await assay_tool.execute(
            {"file": str(assay_csv), "campaign": "egfr", "assay": "IC50"}, context=ledger, task_state=TaskState()
        )
        check(not recorded.failed, f"实验回填成功：{recorded.output.splitlines()[0]}")
        check(recorded.meta.get("received") == 2, f"只收可解析的两条：{recorded.meta}")
        check(recorded.meta.get("skipped") == 1, f"跳过一条非数字：{recorded.meta}")
        assay_path = work / "runs" / "egfr" / "assay.csv"
        check(assay_path.is_file(), "落在 runs/<campaign>/assay.csv")
        with assay_path.open(encoding="utf-8") as handle:
            stored = {row["molecule"]: row for row in csv.DictReader(handle)}
        check(
            stored["c1"]["value"] == "10000.0" and stored["c1"]["qualifier"] == ">",
            f"限定符与数值分开存：{stored['c1']}",
        )

        # 同一个分子再报一次：保留最新，旧值进历史
        assay_csv.write_text("molecule,value\nc2,15\n", encoding="utf-8")
        again = await assay_tool.execute(
            {"file": str(assay_csv), "campaign": "egfr", "assay": "IC50"}, context=ledger, task_state=TaskState()
        )
        check(again.meta.get("superseded") == 1, f"覆盖了一条旧值：{again.meta}")
        with assay_path.open(encoding="utf-8") as handle:
            stored = {row["molecule"]: row for row in csv.DictReader(handle)}
        check(stored["c2"]["value"] == "15.0", f"表里是最新的值：{stored['c2']}")
        check(
            (work / "runs" / "egfr" / "assay-history.csv").is_file(),
            "旧值进了历史文件，不丢",
        )

        other = work / "runs" / "other" / "assay.csv"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("molecule,value\nz1,1\n", encoding="utf-8")
        before = other.read_text(encoding="utf-8")
        await assay_tool.execute(
            {"file": str(assay_csv), "campaign": "egfr", "assay": "IC50"}, context=ledger, task_state=TaskState()
        )
        check(other.read_text(encoding="utf-8") == before, "别的 campaign 一个字节都不动")

        bad_campaign = await assay_tool.execute(
            {"file": str(assay_csv), "campaign": "../etc", "assay": "IC50"}, context=ledger, task_state=TaskState()
        )
        check(
            bad_campaign.failed,
            f"campaign 会当目录名，不能带路径：{bad_campaign.output}",
        )
        bad_columns = work / "bad.csv"
        bad_columns.write_text("foo,bar\n1,2\n", encoding="utf-8")
        missing = await assay_tool.execute(
            {"file": str(bad_columns), "campaign": "egfr", "assay": "IC50"}, context=ledger, task_state=TaskState()
        )
        check(
            missing.failed and "表头" in missing.output,
            f"列名对不上要说清认哪些名字：{missing.output}",
        )
    finally:
        sar_module.sar_dir = original_sar
        ligand_module.ligands_dir = original_ligands
        export_module.runs_dir = original_runs


def check_strategy_compare() -> None:
    """盘点给候选策略，不是一条路线。

    四条策略各自判适用条件：结构可用时同系物优化与换骨架并列，结构不可用时
    配体侧建模与借邻近受体并列。推荐仍按固定优先级取第一条适用的，与原来那条
    if 链的结果一致。
    """
    from vsagent.core.agent.domain.campaign import (
        START_BORROW,
        START_EXPAND,
        START_LABELS,
        START_LIGAND,
        START_NARROW,
        AssessCampaignTool,
        _pick_strategies,
        render_strategies,
        render_verdict,
    )

    def split(**facts: Any) -> tuple[str, list[str], list[str]]:
        start, items = _pick_strategies(**facts)
        return (
            start,
            [item.start for item in items if item.applies],
            [item.start for item in items if not item.applies],
        )

    start, ok, blocked = split(
        structure_ok=True, actives=30, largest_scaffold=8, pair_count=13
    )
    check(start == START_NARROW, f"结构可用、活性多时推荐同系物优化：{start}")
    check(
        ok == [START_NARROW, START_EXPAND],
        f"同系物优化与换骨架同时进候选，按成本排序：{ok}",
    )
    check(
        set(blocked) == {START_LIGAND, START_BORROW},
        f"有结构、活性多时另外两条不可行：{blocked}",
    )

    start, ok, _blocked = split(
        structure_ok=True, actives=8, largest_scaffold=2, pair_count=0
    )
    check(start == START_EXPAND, f"结构可用、活性少时推荐换骨架：{start}")
    check(ok == [START_BORROW, START_EXPAND], f"换骨架与借邻近受体并列：{ok}")

    start, ok, _blocked = split(
        structure_ok=False, actives=15, largest_scaffold=3, pair_count=0
    )
    check(start == START_LIGAND, f"无结构、活性够时推荐配体侧建模：{start}")
    check(ok == [START_LIGAND, START_BORROW], f"配体侧建模与借邻近受体并列：{ok}")

    start, ok, _blocked = split(
        structure_ok=False, actives=5, largest_scaffold=1, pair_count=0
    )
    check(start == START_BORROW, f"无结构、活性少时只能借邻近受体：{start}")
    check(ok == [START_BORROW], f"这一档只有一条可行：{ok}")

    start, ok, _blocked = split(
        structure_ok=True, actives=30, largest_scaffold=3, pair_count=13
    )
    check(
        start == START_EXPAND,
        f"骨架组不够大时同系物优化退出候选，推荐换骨架：{start}",
    )
    check(ok == [START_EXPAND], f"只剩换骨架一条：{ok}")

    tool = AssessCampaignTool()
    _start, items = _pick_strategies(
        structure_ok=True, actives=30, largest_scaffold=8, pair_count=13
    )
    viable = [item for item in items if item.applies]
    drafts = tool._strategy_drafts(
        strategies=items, code="P00533", pair_count=13, selectivity={}
    )
    check(
        [item["start"] for item in drafts] == [item.start for item in viable],
        f"草稿只给可行的那几条，顺序与候选段一致：{[item['start'] for item in drafts]}",
    )
    check(
        all(item["kind"] == "strategy" and item["expected"] for item in drafts),
        "每份草稿都是策略假设、都带可判真假的预期",
    )

    # 整段判定文本走一遍：这一段原来长在 run() 里，不起网络就到不了，
    # 一个没定义的变量名在测试里看不出来。
    for facts, want in (
        ({"structure_ok": True, "actives": 30, "largest_scaffold": 8, "pair_count": 13},
         START_NARROW),
        ({"structure_ok": True, "actives": 8, "largest_scaffold": 2, "pair_count": 0},
         START_EXPAND),
        ({"structure_ok": False, "actives": 15, "largest_scaffold": 3, "pair_count": 0},
         START_LIGAND),
        ({"structure_ok": False, "actives": 5, "largest_scaffold": 1, "pair_count": 0},
         START_BORROW),
    ):
        chosen_start, chosen_items = _pick_strategies(**facts)
        text = "\n".join(
            render_verdict(
                start=chosen_start,
                reason="合成理由",
                structure_ok=facts["structure_ok"],
                strategies=chosen_items,
                drafts=tool._strategy_drafts(
                    strategies=chosen_items,
                    code="P00533",
                    pair_count=facts["pair_count"],
                    selectivity={},
                ),
            )
        )
        check(
            f"判定：{START_LABELS[chosen_start]}（{chosen_start}）" in text,
            f"判定行写的是推荐那条：{text.splitlines()[1]}",
        )
        check(
            "候选策略" in text and "假设草稿" in text,
            f"候选段与草稿段都在：{text.splitlines()[:4]}",
        )
        check(
            chosen_start == want,
            f"推荐仍是原来的优先级结果：{chosen_start}",
        )
        check(
            text.count("假设草稿") == 1,
            "只给推荐那条写一份草稿，不把几条草稿一起摊开",
        )
        if not facts["structure_ok"]:
            check(
                "口袋残基要从文献或突变数据来" in text,
                "无结构时提示口袋残基的来源",
            )

    # 候选段是常驻工具的返回文本，长度要有上限。两条可行加两条不可行是最长的一种。
    worst = _pick_strategies(
        structure_ok=True, actives=30, largest_scaffold=8, pair_count=13
    )[1]
    segment = "\n".join(render_strategies(worst, START_NARROW))
    check(
        len(segment) <= 450,
        f"候选策略段不超过 450 字（现在 {len(segment)}）：{segment}",
    )

    # 渲染配额按粒度分开：三条策略不能把机制假设挤出窗口。
    from vsagent.core.models import Hypothesis

    def one(kind: str, index: int) -> Hypothesis:
        return Hypothesis(
            id=f"hyp#{index}",
            kind=kind,
            mode="narrow" if kind == "strategy" else "",
            claim=f"{kind} 第 {index} 条",
            test="测一批",
            expected="IC50 < 100 nM",
        )

    state = TaskState()
    state.hypotheses = [
        one("strategy", 1),
        one("strategy", 2),
        one("strategy", 3),
        one("mechanism", 4),
        one("mechanism", 5),
    ]
    rendered = state.render()
    check(
        "strategy 第 1 条" in rendered and "strategy 第 2 条" in rendered,
        "两条策略都渲染出来",
    )
    check("strategy 第 3 条" not in rendered, "第三条策略被策略配额挡住")
    check(
        "mechanism 第 4 条" in rendered and "mechanism 第 5 条" in rendered,
        f"机制假设没有被策略挤掉：{rendered}",
    )
    check(
        "另有 1 条未列出：策略 1 条" in rendered,
        f"截断说明写清是哪一类：{rendered}",
    )

    ordered = TaskState()
    ordered.hypotheses = [one("mechanism", 1), one("strategy", 2)]
    text = ordered.render()
    check(
        text.index("mechanism 第 1 条") < text.index("strategy 第 2 条"),
        "按粒度挑完之后仍按提出顺序排列",
    )

    only_strategy = TaskState()
    only_strategy.hypotheses = [one("strategy", index) for index in (1, 2, 3)]
    check(
        "另有 1 条未列出：策略 1 条" in only_strategy.render(),
        "只有策略时仍按策略配额截断",
    )


async def check_campaign_run() -> None:
    """`assess_campaign` 整条跑一遍：返回的 meta 与聊天里那段必须是同一份草稿。

    判定文本抽走之后，`meta` 里那两个字段还指着辅助函数的局部名字，整轮跑完在
    最后一步崩掉，而只测 `render_verdict` 看不出来。这里只把 UniProt 那一步换成
    本地记录，其余照真跑。
    """
    from vsagent.core.agent.domain.campaign import AssessCampaignTool

    work = PROBE / "campaign-run"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    actives_csv = work / "actives.csv"
    actives_csv.write_text(
        "molecule_chembl_id,molecule_pref_name,smiles,pchembl_value\n"
        + "".join(
            f"C{index:04d},分子 {index},{'C' * index}O,{6 + index % 5}\n"
            for index in range(1, 31)
        ),
        encoding="utf-8",
    )
    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    actives_handle = ledger.add(
        "ligandset", path=actives_csv, summary="合成活性集", meta={"kind": "ligandset"}
    )

    campaign = AssessCampaignTool()

    async def fake_target_record(
        code: str, notes: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {"accession": code},
            {
                "accession": code,
                "pdb_count": 3,
                "pdb_structures": [
                    {"pdb_id": "1M17", "resolution": 2.6},
                    {"pdb_id": "4HJO", "resolution": 2.5},
                ],
                "alphafold_id": "",
            },
        )

    campaign._target_record = fake_target_record  # type: ignore[method-assign]
    result = await campaign.execute(
        {"accession": "P00533", "actives": actives_handle.handle}, context=ledger
    )
    check(not result.failed, f"给了活性集就能整条跑完：{result.output[:120]}")
    check(
        result.meta["hypothesis"]["start"] == result.meta["start"],
        f"meta 里的草稿就是推荐那条：{result.meta['hypothesis'].get('start')}",
    )
    check(
        f"假设草稿（{result.meta['hypothesis']['kind']}" in result.output,
        "聊天里那段与 meta 里那份是同一份草稿",
    )
    drafts = result.meta["strategies"]
    check(
        all(item.get("claim") and item.get("expected") for item in drafts),
        f"meta.strategies 每份草稿都带主张与预期：{len(drafts)} 份",
    )


async def check_hypothesis() -> None:
    """假设：六要素硬门槛、只渲染未结的、实验回填结案、对照挑选、导出。"""
    work = PROBE / "hypothesis"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    state = TaskState()
    tool = HypothesisTool()

    async def call(payload: dict[str, Any]) -> ToolResult:
        """内核那一侧会把 `ToolError` 转成一条失败结果；这里照做，好断言文本。"""
        try:
            return await tool.execute(payload, task_state=state)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

    # ---- 一、六要素是硬门槛 ----
    no_test = await call(
        {"action": "propose", "kind": "mechanism", "claim": "S1 口袋的极性亚位点决定活性",
         "expected": "IC50 < 100 nM"}
    )
    check(no_test.failed and "test" in no_test.output, f"缺 test 直接拒绝：{no_test.output}")
    vague = await call(
        {"action": "propose", "kind": "mechanism", "claim": "S1 口袋的极性亚位点决定活性",
         "test": "挑 10 个测一下", "expected": "活性会更好"}
    )
    check(
        vague.failed and "含糊" in vague.output,
        f"预期没有数/阈值/对照时拒绝：{vague.output}",
    )
    no_mode = await call(
        {"action": "propose", "kind": "strategy", "claim": "先做多样性库再收窄",
         "test": "看前 20 名落在几个骨架", "expected": "至少 5 个骨架"}
    )
    check(
        no_mode.failed and "mode" in no_mode.output,
        f"策略假设没有方向时拒绝：{no_mode.output}",
    )
    check(not state.hypotheses, "三次被拒都没有留下半条假设")

    # ---- 二、提出、去重、渲染 ----
    created = await call(
        {"action": "propose", "kind": "strategy", "mode": "narrow",
         "claim": "在氨基吡啶骨架上做取代基优化能提高活性",
         "mechanism": "取代基填满疏水口袋",
         "test": "同一骨架合成 6 个类似物测 IC50", "expected": "IC50 < 100 nM"}
    )
    check(created.meta.get("id") == "hyp#1", f"第一条编号是 hyp#1：{created.meta}")
    duplicate = await call(
        {"action": "propose", "kind": "strategy", "mode": "narrow",
         "claim": "在氨基吡啶骨架上做取代基优化能提高活性",
         "test": "再测一遍", "expected": "< 50 nM"}
    )
    check(
        "已经有了" in duplicate.output and len(state.hypotheses) == 1,
        f"同一主张不重复建号（否则旧编号的引用会断）：{duplicate.output}",
    )
    rendered = state.render()
    check("hyp#1" in rendered and "检验：" in rendered and "预期：" in rendered,
          f"未结假设进 TASK 层：{rendered}")

    for index in range(3):
        await call(
            {"action": "propose", "kind": "mechanism",
             "claim": f"机制假设 {index}", "test": "做一次突变", "expected": "活性下降 10 倍"}
        )
    rendered = state.render()
    check(
        rendered.count("- hyp#") == 4,
        f"一条策略与三条机制都在（策略 2 条、机制 3 条的配额）：{rendered}",
    )
    check("另有" not in rendered, f"没超出配额时不写截断说明：{rendered}")

    # 机制再多两条：机制配额已经用满，多出来的不进上下文。
    for index in range(3, 5):
        await call(
            {"action": "propose", "kind": "mechanism",
             "claim": f"机制假设 {index}", "test": "做一次突变", "expected": "活性下降 10 倍"}
        )
    rendered = state.render()
    check(
        rendered.count("- hyp#") == 4 and "hyp#5" not in rendered,
        f"机制配额 3 条，多出来的不进 TASK 层：{rendered}",
    )
    check(
        "另有 2 条未列出：机制 2 条" in rendered,
        f"截断说明写清是哪一类：{rendered}",
    )
    listed = await call({"action": "list"})
    check("共 6 条假设" in listed.output, f"list 能看到全部：{listed.output}")

    # ---- 竞争关系：只在两条假设给出不同预期时填 ----
    competing = await call(
        {"action": "propose", "kind": "mechanism",
         "claim": "活性来自疏水锚定",
         "test": "测一批去掉疏水基团的类似物，与 hyp#1 的骨架优化对照",
         "expected": "活性下降 ≥10 倍", "rivals": ["hyp#1"]}
    )
    check(competing.meta.get("id") == "hyp#7", f"带竞争的假设正常建号：{competing.meta}")
    check(
        "竞争：hyp#1" in state.find_hypothesis("hyp#7").render(),
        f"竞争关系进渲染：{state.find_hypothesis('hyp#7').render()}",
    )
    bad_rival = await call(
        {"action": "propose", "kind": "mechanism",
         "claim": "编号写错的竞争", "test": "测一批", "expected": "IC50 < 100 nM",
         "rivals": ["hyp#99"]}
    )
    check(
        bad_rival.failed and "不存在" in bad_rival.output,
        f"竞争对手编号不存在要拒绝：{bad_rival.output}",
    )
    no_test = await call(
        {"action": "propose", "kind": "mechanism",
         "claim": "没写怎么区分的竞争", "test": "测一批", "expected": "IC50 < 100 nM",
         "rivals": ["hyp#1"]}
    )
    check(
        no_test.failed and "区分" in no_test.output,
        f"test 不提竞争对手要拒绝：{no_test.output}",
    )

    from vsagent.core.models import Hypothesis

    restored_rival = Hypothesis.from_dict(state.find_hypothesis("hyp#7").to_dict())
    check(restored_rival.rivals == ["hyp#1"], f"竞争关系随会话序列化：{restored_rival.rivals}")
    old = Hypothesis.from_dict(
        {"id": "hyp#9", "claim": "旧会话", "test": "测一批", "expected": "< 100"}
    )
    check(old.rivals == [], "旧会话没有 rivals 键时是空表")

    # ---- 三、update / close 的边界 ----
    updated = await call({"action": "update", "id": "hyp#2", "expected": "IC50 > 500 nM"})
    check("expected" in updated.output, f"能改预期：{updated.output}")
    bad_update = await call({"action": "update", "id": "hyp#2", "expected": "会变好"})
    check(bad_update.failed, "改预期也过同一道校验")
    unknown_id = await call({"action": "close", "id": "hyp#99", "status": "refuted"})
    check(unknown_id.failed and "hyp#1" in unknown_id.output,
          f"编号不存在时列出已有的：{unknown_id.output}")
    closed = await call(
        {"action": "close", "id": "hyp#2", "status": "refuted", "result": "突变体仍活跃"}
    )
    check(state.find_hypothesis("hyp#2").status == "refuted", f"close 改了状态：{closed.output}")
    frozen = await call({"action": "update", "id": "hyp#2", "claim": "改个说法"})
    check(frozen.failed and "已经结案" in frozen.output,
          f"结案之后不再改内容：{frozen.output}")

    # ---- 四、实验回填结案：supported / refuted / inconclusive ----
    runs = work / "runs"
    original_runs = export_module.runs_dir
    export_module.runs_dir = lambda: runs
    ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
    assay_tool = sar_module.RecordAssayTool()

    async def feed(text: str, hypothesis: str = "hyp#1") -> Any:
        path = work / "in.csv"
        path.write_text(text, encoding="utf-8")
        return await assay_tool.execute(
            {"file": str(path), "campaign": "egfr", "assay": "IC50", "hypothesis": hypothesis},
            context=ledger,
            task_state=state,
        )

    try:
        supported = await feed("molecule,value\nc1,40\nc2,80\n")
        check(
            state.find_hypothesis("hyp#1").status == "supported",
            f"全部达标时结案 supported：{supported.output}",
        )
        check("达标 2 条" in supported.output, f"报告逐条对照：{supported.output}")
        check(
            state.find_hypothesis("hyp#1").result_ref.startswith("artifact#"),
            "结案时记下证据出处",
        )

        refuted = await feed("molecule,value\nc1,400\nc2,900\n")
        check(
            state.find_hypothesis("hyp#1").status == "refuted",
            f"全部不达标时结案 refuted：{refuted.output}",
        )

        # 实测只给了下界（`>1`），而预期是「< 100」：真值可能 5 也可能 500，
        # 这一条判不了——不能当达标，也不能当不达标。
        unclear = await feed("molecule,value\nc1,>1\n")
        check(
            state.find_hypothesis("hyp#1").status == "inconclusive",
            f"限定符只给出一侧边界时不下结论：{unclear.output}",
        )
        check("判不了 1 条" in unclear.output, f"要说明为什么判不了：{unclear.output}")

        # 反过来 `>10000` 是稳的 fail：真值只会更大，更不可能落在「< 100」这一侧。
        certain = await feed("molecule,value\nc1,>10000\n")
        check(
            state.find_hypothesis("hyp#1").status == "refuted",
            f"单侧边界也能判死的要判死：{certain.output}",
        )

        unknown_hyp = await feed("molecule,value\nc1,10\n", hypothesis="hyp#99")
        check(
            unknown_hyp.failed and "没有写入" in unknown_hyp.output,
            f"假设编号写错时不写盘：{unknown_hyp.output}",
        )
    finally:
        export_module.runs_dir = original_runs

    # ---- 五、对照分子 ----
    cand_dir = work / "candidates"
    products_module.write_json_atomic(
        cand_dir / "candidates.json",
        {
            "kind": "candidates",
            "rows": [
                {"id": "m1", "name": "a", "smiles": "CCO", "scores": {"dock": -10.0},
                 "consensus_rank": 1.0, "tier": "high", "gates": {}},
                {"id": "m2", "name": "b", "smiles": "CCN", "scores": {"dock": -9.0},
                 "consensus_rank": 2.0, "tier": "high", "gates": {"pains": True}},
                {"id": "m3", "name": "c", "smiles": "CCC", "scores": {"dock": -5.0},
                 "consensus_rank": 3.0, "tier": "low", "gates": {}},
                {"id": "m4", "name": "d", "smiles": "CCCl", "scores": {"dock": -4.0},
                 "consensus_rank": 4.0, "tier": "below_cutoff", "gates": {}},
            ],
        },
    )
    entry = ledger.add(
        "artifact", path=cand_dir, summary="候选表", meta={"kind": "candidates"}
    )
    controls = await analysis_module.PickControlsTool().execute(
        {"source": str(entry.handle), "hypothesis": "hyp#1"},
        context=ledger,
        task_state=state,
    )
    check(not controls.failed, f"对照挑选成功：{controls.output}")
    check(
        controls.meta["controls"]["正向对照"] == ["m1"],
        f"正向对照是排名最高且门禁全过的：{controls.meta}",
    )
    check(
        controls.meta["controls"]["阴性对照"] == ["m4"],
        f"阴性对照是排名最低且门禁全过的：{controls.meta}",
    )
    check(
        controls.meta["controls"]["背离对照"] == ["m2"],
        f"背离对照是门禁没过却排在前半的：{controls.meta}",
    )
    check("用来检验 hyp#1" in controls.output, "说明这批对照为哪条假设服务")
    not_candidates = await analysis_module.PickControlsTool().execute(
        {"source": "receptor#1"}, context=ledger, task_state=state
    )
    check(not_candidates.failed, "不存在的 handle 要报错而不是静默")

    # ---- 六、导出：假设挂在 provenance 下，不新增顶层键 ----
    from types import SimpleNamespace

    bundle = SimpleNamespace(
        pockets=[], origin="ligand", note="", total=0, used=0, excluded={}, sources=[]
    )
    meta = export_module._build_meta(
        run_id="r1", name="n", pdb_id="1M17", uniprot="P00533", chains=["A"],
        reference="", bundle=bundle, ligands=[], hypotheses=tuple(state.hypotheses),
    )
    check("hypotheses" not in meta, "不新增顶层键（判据按与演示数据同键核对）")
    check(
        len(meta["provenance"]["hypotheses"]) == len(state.hypotheses),
        "假设整份进 provenance，含已结的",
    )
    check(
        meta["provenance"]["hypotheses"][1]["status"] == "refuted",
        "已结的状态一起导出",
    )
    # 会话编号是网页把运行目录挂回会话的唯一凭据：缺了它，历史页上「有结构」
    # 标记与结构页的来源都只能靠猜。
    linked = export_module._build_meta(
        run_id="r2", name="n", pdb_id="1M17", uniprot="P00533", chains=["A"],
        reference="", bundle=bundle, ligands=[], session_id="ses_test_link",
    )
    check(
        linked["provenance"]["session_id"] == "ses_test_link",
        "provenance 记下产出它的会话",
    )
    check(
        "session_id" not in meta["provenance"],
        "没有会话编号时不写这个键（演示数据与旧调用保持原形状）",
    )

    # 状态要能落盘再读回来：不然重开会话假设就没了。
    restored = TaskState.from_dict(state.to_dict())
    check(
        [item.id for item in restored.hypotheses] == [item.id for item in state.hypotheses],
        "假设随任务状态一起序列化",
    )
    check(
        restored.find_hypothesis("hyp#2").status == "refuted",
        "状态与依据都还原",
    )


async def check_pharmacophore() -> None:
    """药效团：共识点、叠合失败的分子逐条记录、筛选分数能分开活性和诱饵。"""
    work = PROBE / "pharmacophore"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    library = work / "library"
    library.mkdir(parents=True, exist_ok=True)
    actives = [
        ("a1", "c1ccc(OCC(=O)N)cc1"),
        ("a2", "c1ccc(OCCC(=O)N)cc1"),
        ("a3", "Cc1ccc(OCC(=O)N)cc1"),
        ("a4", "Clc1ccc(OCC(=O)N)cc1"),
        ("a5", "c1ccc(OCC(=O)NC)cc1"),
        ("a6", "c1ccc(OCC(=O)NCC)cc1"),
    ]
    decoys = [("z1", "CCOCC"), ("z2", "c1ccncc1")]
    (library / ligand_module.CSV_FILE).write_text(
        "id,name,smiles\n"
        + "".join(f"{i},{i},{s}\n" for i, s in actives + decoys),
        encoding="utf-8",
    )
    products_module.write_json_atomic(
        library / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "reference"}
    )

    small = work / "small"
    small.mkdir(parents=True, exist_ok=True)
    (small / ligand_module.CSV_FILE).write_text(
        "id,name,smiles\n" + "".join(f"{i},{i},{s}\n" for i, s in actives[:3]),
        encoding="utf-8",
    )
    products_module.write_json_atomic(
        small / ligand_module.MANIFEST_FILE, {"kind": "ligandset", "stage": "reference"}
    )

    original_dir = pharmacophore_module.pharmacophore_dir
    original_candidates = screen_module.candidates_dir
    pharmacophore_module.pharmacophore_dir = lambda: work / "pharm"
    screen_module.candidates_dir = lambda: work / "candidates"
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        reference = ledger.add(
            "ligandset", path=library, summary="参考集", fingerprint="fp-ref",
            meta={"kind": "ligandset", "stage": "reference"}, link_current_receptor=False,
        )
        tiny = ledger.add(
            "ligandset", path=small, summary="三条", fingerprint="fp-small",
            meta={"kind": "ligandset", "stage": "reference"}, link_current_receptor=False,
        )

        build = pharmacophore_module.BuildPharmacophoreTool()
        result = await build.execute(
            {"actives": str(reference.handle), "min_support": 0.5}, context=ledger
        )
        check(not result.failed, f"药效团生成成功：{result.output.splitlines()[0]}")
        check(result.meta.get("n_actives") == 6, f"六个分子参与共识：{result.meta}")
        check(result.meta.get("features") >= 3, f"至少三个药效点：{result.meta}")

        entry = ledger.get(result.meta["handle"])
        check(entry.meta.get("kind") == "pharmacophore", f"登记成药效团：{entry.meta.get('kind')}")
        check(
            [str(parent) for parent in entry.parents] == [str(reference.handle)],
            f"血缘记了参考集：{entry.parents}",
        )
        check(
            entry.meta.get("align_rmsd") is not None and entry.meta["align_rmsd"] < 2.5,
            f"叠合 RMSD 记在产物里：{entry.meta.get('align_rmsd')}",
        )
        counts = entry.meta.get("counts") or {}
        check(
            sum(counts.values()) == len(entry.meta.get("features") or []),
            f"按类型计数与特征点数对得上：{counts}",
        )
        check(
            "z1" in result.output and "公共骨架" in result.output,
            f"叠不上的分子要逐条列出原因：{result.output}",
        )
        check(
            all(0 < item["support"] <= 6 for item in entry.meta["features"]),
            "每个点都记了支持度（有几个分子贡献了它）",
        )
        # min_support=0.5、6 个分子 → 每个保留下来的点至少在 3 个分子里出现。
        # 这条是「共识」这个词的全部内容：门槛失效时只出现在一个分子里的点会混进来。
        check(
            all(item["support"] >= 3 for item in entry.meta["features"]),
            f"低于 min_support 的点被丢掉：{[item['support'] for item in entry.meta['features']]}",
        )

        again = await build.execute(
            {"actives": str(reference.handle), "min_support": 0.5}, context=ledger
        )
        check(again.meta.get("reused") is True, "同参第二次调用复用旧产物")

        too_few = await build.execute({"actives": str(tiny.handle)}, context=ledger)
        check(
            too_few.failed and "少于 5" in too_few.output,
            f"不足 5 个分子直接拒绝：{too_few.output}",
        )
        bad_support = await build.execute(
            {"actives": str(reference.handle), "min_support": 1.5}, context=ledger
        )
        check(bad_support.failed, f"min_support 越界要拒绝：{bad_support.output}")

        # ---- 独立叠合工具：叠好之后药效团直接读坐标，不再重复叠 ----
        ligand_module.aligned_ligandset_dir = lambda: work / "aligned"
        aligner = ligand_module.AlignLigandsTool()
        aligned = await aligner.execute({"ligands": str(reference.handle)}, context=ledger)
        check(not aligned.failed, f"叠合成功：{aligned.output.splitlines()[0]}")
        check(aligned.meta.get("count") == 6, f"六个同系列的叠上了：{aligned.meta}")
        check(aligned.meta.get("dropped") == 2, f"两个诱饵叠不上，逐条记下：{aligned.meta}")
        aligned_entry = ledger.get(aligned.meta["handle"])
        check(
            aligned_entry.meta.get("stage") == "aligned",
            f"产物阶段是 aligned：{aligned_entry.meta.get('stage')}",
        )
        sdf_path = aligned_entry.path / aligned_entry.meta.get("sdf")
        check(sdf_path.is_file(), f"写出一份带坐标的 SDF：{sdf_path.name}")
        from rdkit import Chem as _Chem_align

        sdf_mols = [mol for mol in _Chem_align.SDMolSupplier(str(sdf_path), removeHs=True)
                    if mol is not None]
        check(len(sdf_mols) == 6, f"SDF 里六个构象：{len(sdf_mols)}")
        check(
            {mol.GetProp("_Name") for mol in sdf_mols} == {i for i, _ in actives},
            "SDF 里的分子名就是原来的 id（下游靠它对上）",
        )
        with (aligned_entry.path / ligand_module.CSV_FILE).open(encoding="utf-8") as handle:
            aligned_rows = list(csv.DictReader(handle))
        check(
            "align_rmsd" in (aligned_rows[0] if aligned_rows else {}),
            "CSV 里带每个分子的叠合 RMSD",
        )
        reused_aligned = await aligner.execute(
            {"ligands": str(reference.handle)}, context=ledger
        )
        check(reused_aligned.meta.get("reused") is True, "同参第二次叠合复用旧产物")

        from_aligned = await build.execute(
            {"actives": str(aligned.meta["handle"]), "min_support": 0.5}, context=ledger
        )
        check(
            not from_aligned.failed,
            f"药效团可以直接吃叠好的产物：{from_aligned.output.splitlines()[0]}",
        )
        aligned_pharm = ledger.get(from_aligned.meta["handle"])
        check(
            aligned_pharm.meta.get("align_rmsd") == aligned_entry.meta.get("mean_rmsd"),
            f"叠合 RMSD 沿用上游那份，不重新算：{aligned_pharm.meta.get('align_rmsd')} / "
            f"{aligned_entry.meta.get('mean_rmsd')}",
        )

        strict = await aligner.execute(
            {"ligands": str(reference.handle), "min_atoms": 40}, context=ledger
        )
        check(
            strict.failed and "公共骨架" in strict.output,
            f"骨架要求太严时要说清为什么叠不上：{strict.output}",
        )

        screen_tool = pharmacophore_module.ScreenPharmacophoreTool()
        screened = await screen_tool.execute(
            {"pharmacophore": str(result.meta["handle"]), "library": str(reference.handle)},
            context=ledger,
        )
        check(not screened.failed, f"按药效团筛成功：{screened.output.splitlines()[0]}")
        table = ledger.get(screened.meta["handle"])
        payload = products_module.read_json(table.path / "candidates.json")
        check(
            payload.get("score_column") == "pharmacophore",
            f"分数列单独一列：{payload.get('score_column')}",
        )
        scores = {row["id"]: row["scores"]["pharmacophore"] for row in payload["rows"]}
        check(
            min(scores[i] for i, _ in actives) > max(scores[i] for i, _ in decoys),
            f"活性分子的分数普遍高于诱饵：{ {k: round(v, 2) for k, v in scores.items()} }",
        )
        check(
            payload["rows"][0]["id"] in {i for i, _ in actives},
            f"榜首是活性分子：{payload['rows'][0]['id']}",
        )

        truncated = await screen_tool.execute(
            {"pharmacophore": str(result.meta["handle"]), "library": str(reference.handle),
             "top": 3},
            context=ledger,
        )
        check(truncated.meta.get("count") == 3, f"top 生效：{truncated.meta}")
        check("调大 top" in truncated.output, "截断要写明还有多少")

        wrong = await screen_tool.execute(
            {"pharmacophore": str(reference.handle), "library": str(reference.handle)},
            context=ledger,
        )
        check(wrong.failed, f"不是药效团的产物要拒绝：{wrong.output}")
        no_ledger = await screen_tool.execute(
            {"pharmacophore": str(result.meta["handle"]), "library": str(reference.handle)}
        )
        check(
            no_ledger.failed and "内核装配问题" in no_ledger.output,
            f"没注入登记表是装配问题：{no_ledger.output}",
        )
    finally:
        pharmacophore_module.pharmacophore_dir = original_dir
        screen_module.candidates_dir = original_candidates


def check_identity_floor() -> None:
    """同一性口径：截短构建体不能误杀，一小段不能混进来（P0-1）。

    三件必须成立的事：
      · 208 aa 的构建体对 394 aa 的全长链、208 个残基一个不差 → 同一性 1.00、
        覆盖 0.53，判同一个蛋白（旧口径算成 0.528，把这条正确结论判成了「另一个蛋白」）；
      · 同一性 1.00 但只覆盖 15% → 剔除，原因说的是「只覆盖多少」，不是「另一个蛋白」；
      · 等长序列的结果与旧实现（分母取 max）逐位相同——换口径不动已验证的样本。
    """
    abl_text = IEP_FIXTURE.read_text(encoding="utf-8")
    seqs = align_module.sequences(abl_text)
    chain = seqs["A"]

    # ---- 一、事故的精确复现：394 的全长受体对 208 的构建体 ----
    # 后 186 个残基填 X（未知残基）：它不与任何残基匹配，所以前 208 个的对齐不变。
    full_length = chain[:208] + "X" * 186
    same, cover = align_module.identity(full_length, chain[:208])
    check(
        same == 1.0 and round(cover, 3) == 0.528,
        f"208/394 的构建体：同一性 {same:.3f}、覆盖度 {cover:.3f}",
    )
    check(
        align_module.same_protein(same, cover),
        "208/394 的构建体判同一个蛋白（旧口径 0.528 会误杀）",
    )
    check(
        not align_module.same_protein(0.356, 1.0),
        "同家族的另一条链（同一性 0.356）仍然被剔除",
    )
    check(
        not align_module.same_protein(1.0, 0.15),
        "只覆盖 15% 的片段被剔除——覆盖度那道闸门真的在起作用",
    )

    # ---- 二、端到端：align_candidate 两道闸门都用上了 ----
    # 1IEP 的链 A 是 274 个残基（残基号 225–498），只留这一条链再截短。
    def sliced(last_residue: int) -> str:
        kept = []
        for line in abl_text.splitlines():
            if line.startswith(("ATOM", "HETATM", "ANISOU")):
                if line[21] != "A" or int(line[22:26]) > last_residue:
                    continue
            kept.append(line)
        return "\n".join(kept) + "\n"

    # 截到 208 个残基：覆盖 0.76，是「同一个蛋白的一段」，通过，且报出覆盖数。
    partial, partial_transform = align_module.align_candidate(
        abl_text, sliced(432), "T208"
    )
    check(
        partial.ok and partial_transform is not None,
        f"208/274 的构建体通过核对：{partial.state} {partial.reason}",
    )
    check("208/274" in partial.reason, f"结论里写明覆盖了多少个残基：{partial.reason}")

    # 截到 40 个残基：同一性仍是 1.000，但覆盖只有 0.15，剔除。
    # 这里必须说「只覆盖一小段」——说成「另一个蛋白」会把模型引去换编号。
    fragment, fragment_transform = align_module.align_candidate(
        abl_text, sliced(264), "T40"
    )
    check(
        fragment.state == "mismatch" and fragment_transform is None,
        f"40/274 的片段被剔除、且拿不到变换：{fragment.state}",
    )
    check("40/274" in fragment.reason, f"剔除原因写明覆盖数：{fragment.reason}")
    check(
        "另一个蛋白" not in fragment.reason,
        f"同一性够、只是覆盖不足时不能说成「另一个蛋白」：{fragment.reason}",
    )

    # ---- 三、等长序列不回归：新旧分母是同一个数 ----
    def legacy(left: str, right: str) -> float:
        result = align_module._gemmi().align_string_sequences(
            list(left), list(right), []
        )
        matches = sum(1 for char in result.match_string if char == "|")
        return matches / max(len(left), len(right))

    for label, other in (("自己", chain), ("同蛋白的另一条链", seqs["B"]), ("反向序列", chain[::-1])):
        now, _ = align_module.identity(chain, other)
        check(
            now == legacy(chain, other),
            f"{label}：新口径 {now:.3f} 与旧实现 {legacy(chain, other):.3f} 逐位相同",
        )
    # 不等长的样本上两个口径必须分得开——否则说明改动根本没生效。
    check(
        legacy(full_length, chain[:208]) != same,
        f"不等长的样本上新旧口径确实不同：旧 {legacy(full_length, chain[:208]):.3f} / 新 {same:.3f}",
    )


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
    same, cover = align_module.identity(seqs["A"], seqs["A"])
    check(same == 1.0 and cover == 1.0, f"同一条序列的同一性与覆盖度都是 1：{same:.3f}/{cover:.3f}")
    reversed_seq = seqs["A"][::-1]
    low, _low_cover = align_module.identity(seqs["A"], reversed_seq)
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

    from vsagent.core.agent.ledger import SessionLedger

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

    from vsagent.core.agent.ledger import SessionLedger

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
            task_state=TaskState(),
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
            poses["source"] == "vina:vina" and poses["scoring"] == "vina",
            f"来源写成「引擎:力场」（三种力场的分数不可比）：{poses['source']}",
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
            task_state=TaskState(),
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
            task_state=TaskState(),
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
            task_state=TaskState(),
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
            task_state=TaskState(),
        )
        check(
            both.failed and "二选一" in both.output,
            f"docking 与 ligands 同时给时明确拒绝：{both.output}",
        )

        # ---- 没有 docking：molecules 为空、provenance 不带 poses ----
        plain = await tool.execute(
            {"receptor": str(receptor.handle), "run_id": "plain-run"},
            context=ledger,
            task_state=TaskState(),
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
            task_state=TaskState(),
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
        # 任务状态一并给：`write_run` 把假设导出到 `provenance.hypotheses`，
        # 没给就是内核装配问题（与 `task` / `record_assay` 同一种注入）。
        return await tool.execute(
            arguments, context=ledger_ref[0], task_state=TaskState(), viewer=viewer
        )

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
        # 两处「杂原子组」分属两个对象：受体 PDB 里的、与候选复合物配体集里的。
        # 都叫「杂原子组」而不标对象时，10 与 45 并排读着像矛盾（P3-4）。
        check(
            "受体预处理：" in result.output,
            f"预处理那一段标明是受体：{result.output[:400]}",
        )
        check(
            "  筛掉的杂原子组" not in result.output,
            "旧的那句「筛掉的杂原子组」不再出现（它没说对象）",
        )
        check(not calls, "P0 有货时**一次都没调用预测**（优先级真的生效）")
        # 「里面有哪些文件」是 write_run 之后最常被问的一句；返回里不列的话，
        # 模型只能再跑一次 ls —— 四问探针里 artifact 那一问就卡在这里。
        check("结果文件：" in result.output, f"返回里列出目录内容：{result.output[:200]}")
        check("meta.json" in result.output, "列出来 meta.json")
        check("receptor.pdb" in result.output, "列出来 receptor.pdb")
        check("ligands/（" in result.output, f"目录带子项数：{result.output[:300]}")
        file_line = next(
            line for line in result.output.splitlines() if "结果文件：" in line
        )
        check(
            is_key_line(file_line),
            f"这一行算关键行（压缩时逐字留下，否则滚出 RECENT 就没了）：{file_line}",
        )
        # 再走一遍证据层：干净行要真能落进证据的正文里，光「is_key_line 为真」
        # 还不够——`add_from_part` 还要选它。
        from vsagent.core.agent.context import EvidenceStore as _Store

        probe_part = Part(
            type=PartType.TOOL,
            tool_name="write_run",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"receptor": str(entry.handle)},
            tool_output=result.output,
            tool_meta=dict(result.meta or {}),
        )
        probe_store = _Store()
        probe_store.add_from_part(probe_part)
        kept = "\n".join(
            item.summary + "\n" + item.detail for item in probe_store.items()
        )
        check(
            "结果文件：" in kept,
            f"文件清单真的进了证据层：{kept[:200]}",
        )
        check(result.meta.get("origin") == "complex", f"返回 origin=complex：{result.meta.get('origin')}")

        run_dir = runs_root / "p0-check"
        meta_text = (run_dir / export_module.RUN_META).read_text(encoding="utf-8")
        meta = _json.loads(meta_text)
        check((run_dir / "receptor.pdb").is_file(), "写出了 receptor.pdb")
        check((run_dir / "ligands").is_dir(), "写出了 ligands/ 目录")

        # 登记时按**目录**算指纹，handle 一出生就该是 ok；改了目录里的文件才 stale。
        # 以前这里记的是 `json.dumps(payload)` 的哈希，对账走 `fingerprint_path`，
        # 两个永不相等——artifact 从登记那一刻起就恒为 stale（P1-1），
        # 而 `file_state` 是模型用来判断「这个 handle 还作数吗」的唯一信号。
        def artifact_state() -> str:
            # `file_state` 是派生值，只有 reconcile() 会更新它。
            ledger.reconcile()
            return next(
                item.file_state
                for item in ledger.entries()
                if str(item.handle) == result.meta.get("handle")
            )

        check(
            artifact_state() == "ok",
            f"刚写完的 artifact 是 ok，不是一出生就 stale：{artifact_state()}",
        )
        (run_dir / export_module.RUN_META).write_text(meta_text + " ", encoding="utf-8")
        check(
            artifact_state() == "stale",
            f"改了目录里的文件之后变 stale：{artifact_state()}",
        )
        (run_dir / export_module.RUN_META).write_text(meta_text, encoding="utf-8")
        check(
            artifact_state() == "ok",
            f"改回去之后又变回 ok（指纹按内容算，不是按时间）：{artifact_state()}",
        )

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
            "受体预处理" in structure_result.output,
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


async def check_survey() -> None:
    """结构全景：命中总数、全部取回、按配体分组、标题点名优先、产物与登记。

    这一组盯的是 `ses_0014` 那个毛病的反面：拿到「命中 44 条」之后只看了 3 条，
    就按名称（`TTX`）查了一次 0 条，把「没有参考构象」写进结论。所以这里不查
    单个条目，查的是**分母有没有被摊开**：总数、每组套数、谁在标题里被点名。
    """
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import rsearch as rsearch_module
    from vsagent.core.agent.domain import survey as survey_module
    from vsagent.core.agent.ledger import SessionLedger

    tmp = _Path(tempfile.mkdtemp(prefix="vsa-survey-"))
    original_dir = rsearch_module.search_dir
    original_survey = survey_module.surveys_dir
    original_post = http_module.post_json
    original_get = http_module.get_json
    original_text = http_module.get_text
    rsearch_module.search_dir = lambda: tmp / "search"
    survey_module.surveys_dir = lambda: tmp / "surveys"

    ids = ["6J8I", "6J8G", "7W9K", "2EB2"]

    def poly(entity_id, description, accession):
        return {
            "rcsb_id": entity_id,
            "rcsb_polymer_entity": {"pdbx_description": description},
            "rcsb_polymer_entity_container_identifiers": {"uniprot_ids": [accession]},
        }

    def comp(code, name):
        return {
            "nonpolymer_comp": {
                "chem_comp": {
                    "id": code,
                    "name": name,
                    "type": "non-polymer",
                    "formula_weight": 300.0,
                }
            }
        }

    entries = {
        # 河豚毒素那一套：代号 9SR，标题里写着 tetrodotoxin。
        "6J8I": {
            "rcsb_id": "6J8I",
            "struct": {"title": "Nav1.7 with tetrodotoxin"},
            "rcsb_entry_info": {
                "nonpolymer_entity_count": 2,
                "experimental_method": "ELECTRON MICROSCOPY",
                "resolution_combined": [3.2],
            },
            "polymer_entities": [
                poly("6J8I_1", "Sodium channel protein type 9 subunit alpha", "Q15858"),
                poly("6J8I_2", "Sodium channel subunit beta-1", "Q07699"),
            ],
            "nonpolymer_entities": [
                comp("9SR", "(1R,5R)-3-amino-14-(hydroxymethyl)-pentol"),
                comp("NAG", "2-acetamido-2-deoxy-beta-D-glucopyranose"),
            ],
        },
        "6J8G": {
            "rcsb_id": "6J8G",
            "struct": {"title": "Nav1.7 with saxitoxin"},
            "rcsb_entry_info": {
                "nonpolymer_entity_count": 2,
                "experimental_method": "ELECTRON MICROSCOPY",
                "resolution_combined": [3.2],
            },
            "polymer_entities": [
                poly("6J8G_1", "Sodium channel protein type 9 subunit alpha", "Q15858"),
                poly("6J8G_2", "Sodium channel subunit beta-1", "Q07699"),
            ],
            "nonpolymer_entities": [
                comp("9SL", "[(3aS,4R,10aS)-2,6-diamino-purin-4-yl]methyl carbamate"),
                comp("NAG", "2-acetamido-2-deoxy-beta-D-glucopyranose"),
            ],
        },
        # 只有脂质的一套：标题里没有配体名，套数最多但排不到前面。
        "7W9K": {
            "rcsb_id": "7W9K",
            "struct": {"title": "Nav1.7-beta1-beta2 complex"},
            "rcsb_entry_info": {
                "nonpolymer_entity_count": 2,
                "experimental_method": "ELECTRON MICROSCOPY",
                "resolution_combined": [2.2],
            },
            "polymer_entities": [
                poly("7W9K_1", "Sodium channel protein type 9 subunit alpha", "Q15858"),
                poly("7W9K_2", "Sodium channel subunit beta-1", "Q07699"),
            ],
            "nonpolymer_entities": [
                comp("Y01", "CHOLESTEROL HEMISUCCINATE"),
                comp("LPE", "LPC-ETHER"),
            ],
        },
        # 无配体的一套。
        "2EB2": {
            "rcsb_id": "2EB2",
            "struct": {"title": "EGFR kinase domain"},
            "rcsb_entry_info": {
                "nonpolymer_entity_count": 0,
                "experimental_method": "X-RAY DIFFRACTION",
                "resolution_combined": [2.5],
            },
            "polymer_entities": [
                poly("2EB2_1", "Epidermal growth factor receptor", "P00533"),
            ],
            "nonpolymer_entities": [],
        },
    }

    names = {"9SR": "Tetrodotoxin", "9SL": "Saxitoxin"}

    def post_json(url, payload, **kwargs):
        if "rcsbsearch" in url:
            return {
                "total_count": len(ids),
                "result_set": [{"identifier": pid} for pid in ids],
            }
        if "graphql" in url:
            wanted = payload.get("variables", {}).get("ids", [])
            return {"data": {"entries": [entries[pid] for pid in wanted if pid in entries]}}
        raise AssertionError(f"没预料到的 POST：{url}")

    def get_json(url, params=None, **kwargs):
        if "chemcomp" not in url:
            raise AssertionError(f"没预料到的 GET：{url}")
        code = url.rsplit("/", 1)[-1]
        synonyms = (
            [{"type": "Preferred Name", "name": names[code]}] if code in names else None
        )
        return {
            "chem_comp": {"id": code, "name": names.get(code, code)},
            "rcsb_chem_comp_synonyms": synonyms,
        }

    def get_text(url, **kwargs):
        raise AssertionError(f"结构全景不该下载任何坐标：{url}")

    http_module.post_json = post_json
    http_module.get_json = get_json
    http_module.get_text = get_text

    try:
        ledger = SessionLedger(session_id="", snapshot_path=tmp / "ledger.json")
        tool = survey_module.SurveyStructuresTool()

        # ---- 1. 摊开分母：命中几套、取回几套、按配体分几组 ----
        result = await tool.execute({"accession": "Q15858"}, context=ledger)
        check(not result.failed, f"全景跑通：{result.output[:200]}")
        check(result.meta["total"] == 4, f"报命中总数：{result.meta['total']}")
        check(result.meta["count"] == 4, f"全部取回：{result.meta['count']}")
        check("全部取回" in result.output, f"正文写明没有抽样：{result.output[:120]}")
        check(result.meta["groups"] == 4, f"四组配体：{result.meta['groups']}")

        # ---- 2. 名称来自化学组分字典，代号与名称一起给 ----
        check("9SR Tetrodotoxin" in result.output, f"9SR 的常用名要显示：{result.output}")
        check("9SL Saxitoxin" in result.output, f"9SL 的常用名要显示：{result.output}")

        # ---- 3. 标题点名过的排在脂质前面 ----
        check("★" in result.output, f"标题点名的组要有标记：{result.output}")
        star = result.output.index("9SR")
        lipid = result.output.index("Y01")
        check(star < lipid, "标题点名的配体排在按套数排的脂质前面")

        # ---- 4. 添加剂、其他链、无配体各归各的 ----
        check("添加剂与离子" in result.output and "NAG 2 套" in result.output,
              f"糖归到添加剂那一节：{result.output}")
        check("Sodium channel subunit beta-1" in result.output, "其他聚合物链单列")
        check("无配体：1 套" in result.output, f"无配体单独报：{result.output}")

        # ---- 5. 产物落盘、登记成 artifact、按目录名复用 ----
        handle = result.meta["handle"]
        check(handle.startswith("artifact#"), f"登记成 artifact：{handle}")
        payload_path = tmp / "surveys"
        files = list(payload_path.rglob("survey.json"))
        check(len(files) == 1, f"写了一份 survey.json：{files}")
        payload = _json.loads(files[0].read_text(encoding="utf-8"))
        check(payload["accession"] == "Q15858", "产物里记着靶点")
        check(len(payload["entries"]) == 4, f"产物里是全部条目：{len(payload['entries'])}")
        check(payload["groups"][0]["title_hits"] > 0, "产物里也是点名组在前")
        check(payload["groups"][-1]["title_hits"] == 0, "产物末尾是没被点名的组")
        check(payload["apo"][0]["pdb_id"] == "2EB2", f"无配体记的是 2EB2：{payload['apo']}")

        again = await tool.execute({"accession": "Q15858"}, context=ledger)
        check(again.meta["handle"] == handle, f"同一份全景再跑一次复用 handle：{again.meta['handle']}")
        check(again.meta["reused"] is True, "第二次标 reused")

        # ---- 6. ligand_class 只筛不丢：apo 只留无配体的那套 ----
        apo = await tool.execute({"accession": "Q15858", "ligand_class": "apo"}, context=ledger)
        check(apo.meta["count"] == 1, f"apo 筛出 1 套：{apo.meta['count']}")
        check("2EB2" in apo.output, f"正文里出现 2EB2：{apo.output}")

        # ---- 7. 零命中与失败分开写 ----
        # 换一个编号：同一个查询体在缓存里已经有结果了，拿它测零命中会读到旧响应。
        http_module.post_json = lambda url, payload, **kw: {"total_count": 0, "result_set": []}
        empty = await tool.execute({"accession": "P00000"}, context=ledger)
        check(not empty.failed and empty.meta["reason"] == "checked",
              f"零命中不是失败：{empty.output[:120]}")
        check("查了，没有" in empty.output or "没有登记" in empty.output,
              f"零命中的措辞：{empty.output[:120]}")

        def boom(url, payload, **kwargs):
            raise http_module.ToolError("模拟 502")

        http_module.post_json = boom
        failed = await tool.execute({"accession": "P00001"}, context=ledger)
        check(failed.failed and "这次没查成" in failed.output,
              f"接口挂了时明说没查成：{failed.output[:120]}")
    finally:
        http_module.post_json = original_post
        http_module.get_json = original_get
        http_module.get_text = original_text
        rsearch_module.search_dir = original_dir
        survey_module.surveys_dir = original_survey
        shutil.rmtree(tmp, ignore_errors=True)


async def check_rsearch() -> None:
    """RCSB 搜索：文本搜 / 属性搜 / 批量取配体；只给候选不给结论；失败有明确返回。"""
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import rsearch as rsearch_module

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

    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import literature as literature_module
    from vsagent.core.agent.domain import rsearch as rsearch_module

    tmp = _Path(tempfile.mkdtemp(prefix="vsa-lit-"))
    # 缓存的路径拼法在 `rsearch.cache_path` 里，它调的是 **rsearch 的** `search_dir`，
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


def _jats_fixture() -> str:
    """一份最小 JATS 全文：摘要 + 三节正文（Results 里还有一节子章节）。

    字段名与 Europe PMC `fullTextXML` 一致：`sec` 直接挂在 `body` 下、小节再嵌一层，
    摘要与许可在 `front/article-meta` 里。
    """
    return """<?xml version="1.0" encoding="UTF-8"?>
<article article-type="research-article">
  <front>
    <journal-meta><journal-title-group><journal-title>Journal of Stub</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <title-group><article-title>EGFR T790M resistance in a stub</article-title></title-group>
      <pub-date><year>2024</year></pub-date>
      <abstract><title>Abstract</title><p>Background: stub abstract with <b>bold</b> text.</p></abstract>
      <permissions><license license-type="cc by"><license-p>This is a long licence paragraph that must not be printed in full.</license-p></license></permissions>
    </article-meta>
  </front>
  <body>
    <sec><title>1 Introduction</title><p>Intro paragraph.</p></sec>
    <sec><title>2 Methods</title><p>Methods paragraph one.</p><p>Methods paragraph two.</p></sec>
    <sec><title>3 Results</title><p>Results paragraph with IC50 = 12 nM.</p>
      <p>IC50 was 12 nM in the base case and 14 nM in the sensitivity analysis, with a confidence interval wide enough to make this paragraph long, so the truncation path can be exercised without inventing new fields or a second fixture.</p>
      <sec><title>3.1 Sensitivity</title><p>Subsection paragraph.</p></sec>
    </sec>
  </body>
</article>
"""


async def check_europepmc() -> None:
    """Europe PMC：一步检索（含预印本）+ 开放全文的某一节；三条失败路径要能分清。

    全程打桩：`http.get_json` / `http.get_text` 都换掉，缓存目录指到临时目录。
    """
    import tempfile
    from pathlib import Path as _Path

    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import literature as literature_module
    from vsagent.core.agent.domain import rsearch as rsearch_module

    tmp = _Path(tempfile.mkdtemp(prefix="vsa-epmc-"))
    original_dir = rsearch_module.search_dir
    rsearch_module.search_dir = lambda: tmp
    original_json = http_module.get_json
    original_text = http_module.get_text

    searches: list[dict[str, object]] = []
    downloads: list[str] = []
    fail_download = {"value": False}

    def get_json(url, params=None, **kwargs):
        params = params or {}
        searches.append(dict(params))
        if params.get("resultType") == "lite":
            query = str(params.get("query") or "")
            if "PMC99999999" in query:
                return {"hitCount": 0, "resultList": {"result": []}}
            if "PMC5884736" in query:
                return {
                    "hitCount": 1,
                    "resultList": {
                        "result": [
                            {
                                "id": "29551287",
                                "source": "MED",
                                "pmid": "29551287",
                                "pmcid": "PMC5884736",
                                "title": "Closed access stub",
                                "isOpenAccess": "N",
                                "inEPMC": "Y",
                            }
                        ]
                    },
                }
            if "PPR1350376" in query:
                return {
                    "hitCount": 1,
                    "resultList": {
                        "result": [
                            {
                                "id": "PPR1350376",
                                "source": "PPR",
                                "title": "Preprint stub",
                                "isOpenAccess": "N",
                            }
                        ]
                    },
                }
            return {
                "hitCount": 1,
                "resultList": {
                    "result": [
                        {
                            "id": "41142240",
                            "source": "MED",
                            "pmid": "41142240",
                            "pmcid": "PMC12545111",
                            "doi": "10.1/x",
                            "title": "Stub paper",
                            "isOpenAccess": "Y",
                            "inEPMC": "Y",
                        }
                    ]
                },
            }
        return {
            "hitCount": 42,
            "resultList": {
                "result": [
                    {
                        "id": "41142240",
                        "source": "MED",
                        "pmid": "41142240",
                        "pmcid": "PMC12545111",
                        "doi": "10.1/x",
                        "title": "Stub <i>EGFR</i> paper",
                        "authorString": "Li X, Lu Y, Zhou M, Dai Z.",
                        "pubYear": "2024",
                        "citedByCount": 7,
                        "isOpenAccess": "Y",
                        "abstractText": "<h4>Background</h4>Stub abstract &amp; a second-<i>line</i> note.",
                        "journalInfo": {"journal": {"title": "Journal of Stub"}},
                        "pubTypeList": {"pubType": ["Journal Article"]},
                    },
                    {
                        "id": "PPR1350376",
                        "source": "PPR",
                        "title": "Preprint on EGFR",
                        "pubYear": "2026",
                        "authorString": "Doe J",
                        "isOpenAccess": "N",
                        "bookOrReportDetails": {"publisher": "bioRxiv"},
                        "abstractText": "Preprint abstract.",
                    },
                ]
            },
        }

    def get_text(url, params=None, **kwargs):
        downloads.append(url)
        if fail_download["value"]:
            raise http_module.RetryableToolError("模拟 HTTP 500")
        if "fullTextXML" not in url:
            raise AssertionError(f"没预料到的 GET：{url}")
        return _jats_fixture()

    try:
        http_module.get_json = get_json
        http_module.get_text = get_text

        # ---- 1. 检索：字段、预印本、全文入口、标签剥离 ----
        search = literature_module.SearchEuropePmcTool()
        result = await search.run({"query": "EGFR T790M", "limit": 5})
        check(not result.failed, f"Europe PMC 检索跑通：{result.output[:160]}")
        check("命中 42 篇，列出 2 篇" in result.output, "命中总数与列出条数分开报")
        check("PMID 41142240 · PMC12545111 · 2024 · Journal of Stub · 被引 7" in result.output,
              "命中行带 PMID / PMCID / 年份 / 期刊 / 被引")
        check("Stub EGFR paper" in result.output, "标题里的 <i> 被剥掉")
        check("Stub abstract & a second-line note." in result.output,
              "块标签换空格、行内标签不留空格（second-<i>line</i> 不该变成 second- line）")
        check("预印本" in result.output and "bioRxiv" in result.output, "预印本标出来源")
        check(
            "可读全文：read_fulltext article=PMC12545111" in result.output,
            "OA 条目给出全文入口",
        )
        check("被引 0" not in result.output and "预印本 · 被引" not in result.output,
              "没有引用数时不印一个假 0")
        check(result.meta["total"] == 42 and result.meta["pmcids"] == ["PMC12545111"],
              f"meta 里的总数与 PMCID：{result.meta.get('total')} / {result.meta.get('pmcids')}")

        # ---- 2. 缓存：同一条查询第二次不联网（键里带 source / sort）----
        before = len(searches)
        await search.run({"query": "EGFR T790M", "limit": 5})
        check(len(searches) == before, f"同一条查询走缓存：新增 {len(searches) - before} 次请求")

        # ---- 3. 排序：透传成接口表达式，不认的值当场拒绝 ----
        await search.run({"query": "EGFR T790M", "limit": 5, "sort": "cited"})
        check(searches[-1].get("sort") == "CITED desc", f"被引排序转成表达式：{searches[-1].get('sort')}")
        bad_sort = await search.run({"query": "EGFR T790M", "sort": "hot"})
        check(bad_sort.failed and "sort" in bad_sort.output, f"不认识的排序拒绝：{bad_sort.output[:80]}")

        # ---- 4. 全文：默认摘要 + 章节清单，不抓正文 ----
        reader = literature_module.ReadFulltextTool()
        listing = await reader.run({"article": "PMC12545111"})
        check(not listing.failed, f"读全文清单成功：{listing.output[:120]}")
        check("Journal of Stub · 2024 · cc by" in listing.output, "首行给期刊、年份与许可")
        check("3 Results" in listing.output and "2 Methods" in listing.output, "章节清单列出了正文各节")
        check('取某一节：read_fulltext article="PMC12545111" section="results"' in listing.output,
              "清单后面给下一步怎么取")

        # ---- 5. 取一节：子章节的段落也要在 ----
        results = await reader.run({"article": "PMC12545111", "section": "Results"})
        check(not results.failed, f"取 Results 成功：{results.output[:120]}")
        check("IC50 = 12 nM" in results.output, "本节正文在")
        check("[3.1 Sensitivity]" in results.output and "Subsection paragraph." in results.output,
              "子章节标题与段落一起给出来")
        check("章节：3 Results" in results.output, "报出匹配到的标题")

        # ---- 6. 截断：说清原文多长、给了多少 ----
        clipped = await reader.run({"article": "PMC12545111", "section": "results", "max_chars": 200})
        check(not clipped.failed, "小预算也能取")
        check("已截断：本节共" in clipped.output and "这里给前" in clipped.output,
              f"截断要说明白：{clipped.output[-120:]}")
        check(clipped.meta["truncated"] is True, "meta 里也标了截断")

        # ---- 7. 摘要单独取（不在 body 的 sec 里）----
        abstract = await reader.run({"article": "PMC12545111", "section": "abstract"})
        check(not abstract.failed, "摘要能单独取")
        check("章节：Abstract" in abstract.output and "stub abstract" in abstract.output,
              f"摘要内容在：{abstract.output[:160]}")

        # ---- 8. 没有这一节：不假装成功，把可读的列出来 ----
        missing = await reader.run({"article": "PMC12545111", "section": "discussion"})
        check(missing.failed and "没有标题含" in missing.output, f"缺节要照实说：{missing.output[:160]}")
        check("1 Introduction" in missing.output, "缺节时把可读章节列出来")

        # ---- 9. 非 OA：**在下载之前**就拒绝 ----
        before = len(downloads)
        closed = await reader.run({"article": "PMC5884736"})
        check(closed.failed and "不是开放获取" in closed.output, f"非 OA 明确拒绝：{closed.output[:120]}")
        check(len(downloads) == before, "非 OA 不该去请求全文接口（那个端点会回 500，分不清病因）")

        # ---- 10. 预印本没有 PMCID ----
        preprint = await reader.run({"article": "PPR1350376"})
        check(preprint.failed and "没有 PMC 全文编号" in preprint.output,
              f"预印本说明白：{preprint.output[:140]}")

        # ---- 11. 认不出的编号 / 查不到记录 ----
        unknown = await reader.run({"article": "my-paper"})
        check(unknown.failed and "认不出来" in unknown.output, f"认不出的编号要指路：{unknown.output[:140]}")
        absent = await reader.run({"article": "PMC99999999"})
        check(absent.failed and "没有 PMC99999999" in absent.output, f"查不到记录：{absent.output[:120]}")

        # ---- 12. PMID / DOI 也能喂（先换 PMCID）----
        searches.clear()
        await reader.run({"article": "41142240"})
        check(any("EXT_ID:41142240" in str(item.get("query")) for item in searches),
              f"PMID 换成 EXT_ID 查询：{[item.get('query') for item in searches]}")
        searches.clear()
        await reader.run({"article": "10.1/x"})
        check(any('DOI:"10.1/x"' in str(item.get("query")) for item in searches),
              f"DOI 换成 DOI 查询：{[item.get('query') for item in searches]}")

        # ---- 13. 全文接口真的挂了：说「可能暂时，也可能没有全文」 ----
        fail_download["value"] = True
        broken = await reader.run({"article": "PMC12545111"})
        check(broken.failed and "5xx" in broken.output, f"5xx 要说清两种可能：{broken.output[:200]}")
    finally:
        http_module.get_json = original_json
        http_module.get_text = original_text
        rsearch_module.search_dir = original_dir
        shutil.rmtree(tmp, ignore_errors=True)


def _pubchem_row(
    cid: int, title: str = "", *, formula: str = "C9H8O4",
    inchikey: str = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
) -> dict[str, object]:
    """一条 PUG 属性表的行。键名照真实返回写（`SMILES` 而不是 `CanonicalSMILES`）。"""
    return {
        "CID": cid,
        "Title": title,
        "MolecularFormula": formula,
        "MolecularWeight": "180.16",
        "SMILES": "CC(=O)OC1=CC=CC=C1C(=O)O",
        "ConnectivitySMILES": "CC(=O)OC1=CC=CC=C1C(=O)O",
        "InChIKey": inchikey,
        "IUPACName": "2-acetyloxybenzoic acid",
        "Charge": 0,
        "XLogP": 1.2,
        "TPSA": 63.6,
        "HBondDonorCount": 1,
        "HBondAcceptorCount": 4,
        "RotatableBondCount": 3,
    }


async def check_pubchem() -> None:
    """PubChem：标识识别、名称→SMILES 回退、相似检索的截断与无命中。

    全程打桩；断言里带 URL 的命名空间，因为「拿它当名字还是当结构查」正是这层
    最容易错的地方——错的时候接口会回 404，而 404 读起来像「这个化合物不存在」。

    **合并入口那几条单独打 ChEMBL 的桩**（`chembl._request_json`），不打 `get_json`：
    两条库的请求走同一个 `http`，但只按 URL 认的话，PubChem 那半的断言会被 ChEMBL
    的 URL 串进来搅乱（`calls` 记的是全部 GET）。
    """
    from vsagent.core.agent.domain import chembl as chembl_module
    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import pubchem as pubchem_module

    original_json = http_module.get_json
    original_chembl_json = chembl_module._request_json
    chembl_calls: list[tuple[str, dict[str, object]]] = []
    calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def chembl_json(url, params=None, **kwargs):
        chembl_calls.append((url, dict(params or {})))
        if "molecule_structures__standard_inchi_key__iexact" in (params or {}):
            # 只有阿司匹林那把钥匙在 ChEMBL 里有记录；其余一律「没收录」，
            # 用来验证「查不到不是失败，也不许说成接口故障」。
            if (params or {})["molecule_structures__standard_inchi_key__iexact"] == (
                "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
            ):
                return {"molecules": [{"molecule_chembl_id": "CHEMBL25"}]}
            return {"molecules": []}
        if url.endswith(".json"):
            return {
                "molecule_chembl_id": "CHEMBL25",
                "pref_name": "ASPIRIN",
                "molecule_type": "Small molecule",
                "structure_type": "MET",
                "max_phase": 4.0,
                "first_approval": 1950,
                "molecule_structures": {
                    "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
                    "standard_inchi_key": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                },
                "molecule_properties": {},
                "molecule_hierarchy": {"parent_chembl_id": "CHEMBL25"},
                "molecule_synonyms": [{"molecule_synonym": "Aspirin"}],
                "atc_classifications": ["N02BA01"],
            }
        if "mechanism" in url:
            return {"mechanisms": []}
        return {"drug_indications": [], "page_meta": {"total_count": 0}}

    def get_json(url, params=None, **kwargs):
        calls.append((url, dict(params or {}), dict(kwargs)))
        if "fastsimilarity_2d" in url:
            if "nosuch" in url:
                raise http_module.NotFound("资源不存在（HTTP 404）")
            # 30 条：用来验证 limit 的夹取（上限 20）与「只列前几条」。
            return {
                "IdentifierList": {
                    "CID": [2244, 135269, 71586929] + [1000 + index for index in range(27)]
                }
            }
        if "/synonyms/" in url:
            return {
                "InformationList": {
                    "Information": [{"CID": 2244, "Synonym": ["aspirin", "ASA", "50-78-2"]}]
                }
            }
        if "/cid/" in url and "," in url.split("/cid/")[1]:
            ids = [int(item) for item in url.split("/cid/")[1].split("/")[0].split(",")]
            return {"PropertyTable": {"Properties": [_pubchem_row(cid, f"Mol {cid}") for cid in ids]}}
        if "/name/multi/" in url:
            return {
                "PropertyTable": {
                    "Properties": [_pubchem_row(1, "First"), _pubchem_row(2, "Second", formula="C2H6O")]
                }
            }
        if "/name/zzz/" in url:
            raise http_module.NotFound("资源不存在（HTTP 404）")
        if "/name/CCO/" in url:
            raise http_module.NotFound("资源不存在（HTTP 404）")
        if "/smiles/CCO/" in url:
            return {"PropertyTable": {"Properties": [
                _pubchem_row(702, "Ethanol", formula="C2H6O",
                             inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N")
            ]}}
        if "/smiles/zzz/" in url:
            raise http_module.NotFound("资源不存在（HTTP 404）")
        if "/cid/2244/" in url:
            return {"PropertyTable": {"Properties": [_pubchem_row(2244, "Aspirin")]}}
        if "/inchikey/" in url:
            return {"PropertyTable": {"Properties": [_pubchem_row(2244, "Aspirin")]}}
        if "/name/aspirin/" in url or "/smiles/" in url:
            return {"PropertyTable": {"Properties": [_pubchem_row(2244, "Aspirin")]}}
        raise AssertionError(f"没预料到的 GET：{url}")

    try:
        http_module.get_json = get_json
        chembl_module._request_json = chembl_json
        lookup = pubchem_module.LookupCompoundTool()
        similar = pubchem_module.FindSimilarCompoundsTool()

        # ---- 1. 名称 ----
        result = await lookup.run({"compound": "aspirin"})
        check(not result.failed, f"按名称查成功：{result.output[:120]}")
        check("/compound/name/aspirin/property/" in calls[-1][0], "名称走 name 命名空间")
        check("CID 2244 · Aspirin" in result.output, "首行给 CID 与名称")
        check("分子式 C9H8O4 · MW 180.16 · 电荷 0" in result.output, "身份行给分子式/分子量/电荷")
        check("SMILES：CC(=O)OC1=CC=CC=C1C(=O)O" in result.output, "规范 SMILES 在")
        check(
            "计算值（PubChem 按规则算的，不是实验数据）：XLogP 1.2 · TPSA 63.6 · "
            "氢键供体 1 / 受体 4 · 可旋转键 3" in result.output,
            "计算性质单独一行，并写明是算出来的",
        )
        check(result.meta["cids"] == [2244], f"meta 里带 CID：{result.meta.get('cids')}")

        # ---- 2. CID 与 InChIKey 各走自己的命名空间 ----
        await lookup.run({"compound": "CID 2244"})
        check("/compound/cid/2244/property/" in calls[-1][0], "CID 前缀能剥掉")
        await lookup.run({"compound": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"})
        check("/compound/inchikey/" in calls[-1][0], "InChIKey 走 inchikey")

        # ---- 3. SMILES：括号要编码；结构串的 500 要按「解析失败」报 ----
        await lookup.run({"compound": "CC(=O)OC1=CC=CC=C1C(=O)O"})
        smiles_url = calls[-1][0]
        check("/compound/smiles/" in smiles_url and "%28" in smiles_url,
              f"SMILES 走 smiles 且做了 URL 编码：{smiles_url}")

        # ---- 4. 名称查不到 → 退回当 SMILES 查（CCO 这种没有提示字符的串）----
        before = len(calls)
        fallback = await lookup.run({"compound": "CCO"})
        check(not fallback.failed, f"CCO 退回结构查询成功：{fallback.output[:100]}")
        check(len(calls) == before + 2, f"先 name 再 smiles，共两次请求：{len(calls) - before}")
        check("/name/CCO/" in calls[before][0] and "/smiles/CCO/" in calls[before + 1][0],
              "回退顺序是 name → smiles")
        check("CID 702" in fallback.output, "拿到乙醇那条")

        # ---- 5. 两种都查不到：说清试过什么 ----
        missing = await lookup.run({"compound": "zzz"})
        check(missing.failed and "PubChem 里没有" in missing.output, f"查不到照实说：{missing.output[:160]}")
        check("name=zzz" in missing.output, "把试过的命名空间列出来")

        # ---- 5.1 PUG 的两种 500：结构错要翻成人话，真的故障要留给内核重试 ----
        def bad_structure(url, params=None, **kwargs):
            raise http_module.RetryableToolError(
                '接口暂时不可用（HTTP 500）：{"Fault": {"Code": "PUGREST.ServerError", '
                '"Message": "Search status indicates failure"}}'
            )

        http_module.get_json = bad_structure
        broken = await lookup.run({"compound": "CC(=O)O"})
        check(broken.failed and "没能解析" in broken.output,
              f"结构解析失败要翻成人话：{broken.output[:140]}")

        def busy(url, params=None, **kwargs):
            raise http_module.RetryableToolError("接口暂时不可用（HTTP 503）：PUGREST.ServerBusy")

        http_module.get_json = busy
        try:
            await lookup.run({"compound": "aspirin"})
            check(False, "瞬时故障应当抛给内核重试")
        except http_module.RetryableToolError:
            check(True, "瞬时故障原样抛给内核（不伪装成参数错）")
        http_module.get_json = get_json

        # ---- 6. 别名：多一次请求，拿不到不算失败 ----
        named = await lookup.run({"compound": "aspirin", "synonyms": True})
        check("别名：aspirin、ASA、50-78-2" in named.output, "别名列出来了")
        check(any("/synonyms/" in item[0] for item in calls), "确实查了同义词接口")

        def no_synonyms(url, params=None, **kwargs):
            if "/synonyms/" in url:
                raise http_module.ToolError("模拟 503")
            return get_json(url, params, **kwargs)

        http_module.get_json = no_synonyms
        degraded = await lookup.run({"compound": "aspirin", "synonyms": True})
        # 判据只能看 PubChem 那份别名里的具体条目：ChEMBL 那半自己也印「别名 N 个」，
        # 用「输出里有没有『别名』两个字」判会把两半混为一谈。
        check(not degraded.failed and "ASA" not in degraded.output, "别名取不到时不影响主查询")
        http_module.get_json = get_json

        # ---- 7. 同名多义：列全并提醒确认 ----
        multi = await lookup.run({"compound": "multi"})
        check(not multi.failed and "命中 2 条" in multi.output, f"多条命中要说清：{multi.output[:80]}")
        check("同名多义" in multi.output, "提醒同名多义")
        check(multi.meta["cids"] == [1, 2], f"meta 列出全部 CID：{multi.meta.get('cids')}")

        # ---- 8. 相似检索：批量取属性、保持顺序、报总数 ----
        found = await similar.run({"smiles": "CC(=O)OC1=CC=CC=C1C(=O)O", "threshold": 98, "limit": 2})
        check(not found.failed, f"相似检索跑通：{found.output[:120]}")
        check("命中 30 条，列出前 2 条" in found.output, "总数与列出条数分开")
        check(calls[-1][0].endswith("/property/" + pubchem_module.PROPERTIES + "/JSON")
              and "/cid/2244,135269/" in calls[-1][0],
              f"一次批量请求取前几条的属性：{calls[-1][0]}")
        check(found.meta["cids"] == [2244, 135269], f"顺序照接口给的：{found.meta.get('cids')}")
        check("阈值内共 30 条" in found.output, "截断时说明还剩多少")
        check("接口不给分数" in found.output, "不编相似度分数")

        # ---- 9. 无命中：不是失败 ----
        empty = await similar.run({"smiles": "nosuch"})
        check(not empty.failed and "没有与这段结构相似度" in empty.output,
              f"阈值内没有命中不算失败：{empty.output[:120]}")
        check(empty.meta["total"] == 0, "meta 里总数是 0")

        # ---- 9.1 相似检索里结构串解析失败：同样翻成人话 ----
        def bad_similarity(url, params=None, **kwargs):
            raise http_module.RetryableToolError(
                '接口暂时不可用（HTTP 500）：{"Fault": {"Code": "PUGREST.ServerError"}}'
            )

        http_module.get_json = bad_similarity
        bad_smiles = await similar.run({"smiles": "CC(("})
        check(bad_smiles.failed and "没能解析" in bad_smiles.output,
              f"相似检索的结构错也要说清：{bad_smiles.output[:140]}")
        http_module.get_json = get_json

        # ---- 10. 参数边界 ----
        low = await similar.run({"smiles": "CCO", "threshold": 10})
        check(low.failed and "50–100" in low.output, f"阈值下限拦住：{low.output[:80]}")
        clipped = await similar.run({"smiles": "CCO", "limit": 99})
        check(not clipped.failed, "limit 超上限不报错，夹住")
        batch = calls[-1][0].split("/cid/")[1].split("/")[0]
        check(len(batch.split(",")) == pubchem_module.MAX_LIMIT,
              f"limit 夹到 {pubchem_module.MAX_LIMIT}：实际取了 {len(batch.split(','))} 个")
        check(clipped.meta["count"] == pubchem_module.MAX_LIMIT,
              f"只列上限那么多：{clipped.meta.get('count')}")

        # ---- 11. 合并入口：两半靠 InChIKey 对上 ----
        # 名称进来：PubChem 先答，再按 InChIKey 补 ChEMBL 那半。
        chembl_calls.clear()
        merged = await lookup.run({"compound": "aspirin"})
        check(not merged.failed, f"合并入口跑通：{merged.output[:80]}")
        check(
            "（下半来自 ChEMBL，按 InChIKey BSYNRYMUTXBXSQ-UHFFFAOYSA-N 对上同一个结构）"
            in merged.output,
            "两半之间说明是怎么对上的",
        )
        check("ChEMBL 分子 CHEMBL25 · ASPIRIN" in merged.output, "ChEMBL 那半在")
        check("已上市药物" in merged.output, "max_phase 4 翻成已上市")
        check(
            merged.meta["chembl_id"] == "CHEMBL25",
            f"meta 带 ChEMBL 编号：{merged.meta.get('chembl_id')}",
        )
        check(merged.meta["max_phase"] == 4.0, f"meta 带 max_phase：{merged.meta.get('max_phase')}")
        check(
            any(
                any("standard_inchi_key" in key for key in params)
                for _url, params in chembl_calls
            ),
            f"是拿 InChIKey 去反查的：{chembl_calls}",
        )

        # 编号进来：方向反过来，先 ChEMBL 后按 InChIKey 反查 PubChem。
        calls.clear()
        chembl_calls.clear()
        by_code = await lookup.run({"compound": "chembl25"})
        check(not by_code.failed, f"按编号查也跑通：{by_code.output[:80]}")
        check("CID 2244 · Aspirin" in by_code.output, "PubChem 那半照样有")
        check("ChEMBL 分子 CHEMBL25" in by_code.output, "ChEMBL 那半也有")
        check(
            by_code.output.index("CID 2244") < by_code.output.index("ChEMBL 分子"),
            "输出顺序统一：PubChem 在前、ChEMBL 在后（进来的方向不影响排版）",
        )
        check(
            any("/inchikey/" in url for url, _params, _kwargs in calls),
            f"按编号进来时用 InChIKey 去 PubChem 反查：{calls}",
        )

        # 同名多义：不知道该补哪一个，就不补——但要说清下一步怎么补。
        chembl_calls.clear()
        ambiguous = await lookup.run({"compound": "multi"})
        check(chembl_calls == [], f"同名多义时不查 ChEMBL：{chembl_calls}")
        check("拿 CID 再查一次会顺带取 ChEMBL 画像" in ambiguous.output, "指路：用 CID 再查")

        # ChEMBL 里没收录：一句说明，不是失败，也不许说成接口故障。
        chembl_module._request_json = lambda url, params=None, **kwargs: (
            {"molecules": []}
            if any("standard_inchi_key" in key for key in (params or {}))
            else chembl_json(url, params, **kwargs)
        )
        absent = await lookup.run({"compound": "aspirin"})
        check(not absent.failed, "ChEMBL 没收录不算失败")
        check("ChEMBL 里没有这个结构" in absent.output, f"说清是没收录：{absent.output[-200:]}")
        check("不代表没有活性" in absent.output, "别让「没收录」被读成「没活性」")
        check(absent.meta["chembl_id"] == "", "meta 里 ChEMBL 编号是空的")

        # 确定性错误：降级成一句说明（主答案已经拿到），不整次失败。
        def chembl_down(url, params=None, **kwargs):
            raise http_module.ToolError("模拟 400")

        chembl_module._request_json = chembl_down
        degraded_half = await lookup.run({"compound": "aspirin"})
        check(not degraded_half.failed, "ChEMBL 挂了不影响 PubChem 那半")
        check(
            "ChEMBL 那半没取到" in degraded_half.output,
            f"降级要说清：{degraded_half.output[-160:]}",
        )
        check("CID 2244" in degraded_half.output, "身份照旧给出来")

        # 瞬时故障：照旧抛给内核重试——吞掉它会让模型以为「这个分子不在 ChEMBL 里」。
        def chembl_busy(url, params=None, **kwargs):
            raise http_module.RetryableToolError("接口暂时不可用（HTTP 503）")

        chembl_module._request_json = chembl_busy
        try:
            await lookup.run({"compound": "aspirin"})
            check(False, "ChEMBL 的瞬时故障应当抛给内核重试")
        except http_module.RetryableToolError:
            check(True, "ChEMBL 的瞬时故障原样抛给内核")

        # 生物药：ChEMBL 记录里没有结构式，PubChem 那半无从查起——说清楚，不是「查不到」。
        def biological(url, params=None, **kwargs):
            if url.endswith(".json"):
                return {
                    "molecule_chembl_id": "CHEMBL1201583",
                    "pref_name": "BEVACIZUMAB",
                    "molecule_type": "Antibody",
                    "structure_type": "SEQ",
                    "max_phase": 4.0,
                    "molecule_structures": {"canonical_smiles": None, "standard_inchi_key": None},
                    "molecule_properties": {},
                    "molecule_hierarchy": {"parent_chembl_id": "CHEMBL1201583"},
                }
            return chembl_json(url, params, **kwargs)

        chembl_module._request_json = biological
        calls.clear()
        biologic = await lookup.run({"compound": "CHEMBL1201583"})
        check(not biologic.failed, "生物药也能出画像")
        check(
            "PubChem 那半跳过" in biologic.output,
            f"说清为什么没有 PubChem 那半：{biologic.output[:200]}",
        )
        check(calls == [], f"没有结构式就不该去 PubChem 白跑一趟：{calls}")
    finally:
        http_module.get_json = original_json
        chembl_module._request_json = original_chembl_json


def _ot_target(**overrides: object) -> dict[str, object]:
    """一份 GraphQL 的 `data` 块，里面是一个靶点。键名照真实返回写。"""
    target: dict[str, object] = {
        "id": "ENSG00000146648",
        "approvedSymbol": "EGFR",
        "approvedName": "epidermal growth factor receptor",
        "biotype": "protein_coding",
        "functionDescriptions": [
            "Receptor tyrosine kinase (PubMed:12345) that binds EGF. "
            "{ECO:0000269|PubMed:12345}"
        ],
        "genomicLocation": {"chromosome": "7", "start": 55018820, "end": 55211628},
        "tractability": [
            {"label": "Approved Drug", "modality": "SM", "value": True},
            {"label": "High-Quality Pocket", "modality": "SM", "value": True},
            {"label": "Med-Quality Pocket", "modality": "SM", "value": False},
            {"label": "Approved Drug", "modality": "AB", "value": False},
            {"label": "Literature", "modality": "PR", "value": True},
        ],
        # 7 条：用来验证「高质量优先 → 评分从高到低」的排序与「列 5 条 + 还有 2 个」。
        # 顺序故意打乱，而且低质量那条的评分比几条高质量的都高——排序必须先看质量。
        "chemicalProbes": [
            {"id": "LOWQ", "drugId": "CHEMBL1", "isHighQuality": False,
             "mechanismOfAction": ["inverse agonist"], "control": None,
             "scoreInCells": 10, "scoreInOrganisms": 0, "probesDrugsScore": 90},
            # 探针名故意不用 AFATINIB：药物那一段也用它，两处同名会让「盐型只占一条」
            # 与「按阶段排序」那几条断言分不清是哪一段在说话。
            {"id": "GEFITINIB", "drugId": "CHEMBL939", "isHighQuality": True,
             "mechanismOfAction": ["inhibitor"], "control": None,
             "scoreInCells": 67.5, "scoreInOrganisms": 82.5, "probesDrugsScore": 80.4},
            {"id": "DDC-01-163", "drugId": "CHEMBL4468472", "isHighQuality": True,
             "mechanismOfAction": [], "control": None,
             "scoreInCells": None, "scoreInOrganisms": None, "probesDrugsScore": 40},
            {"id": "ACBI3", "drugId": "CHEMBL6195033", "isHighQuality": True,
             "mechanismOfAction": ["degrader"], "control": "Cis-ACBI3",
             "scoreInCells": 82.5, "scoreInOrganisms": 75, "probesDrugsScore": 70},
            {"id": "P4B", "drugId": None, "isHighQuality": True,
             "mechanismOfAction": ["degrader"], "control": None,
             "scoreInCells": None, "scoreInOrganisms": None, "probesDrugsScore": 100},
            {"id": "ARS-1620", "drugId": "CHEMBL4214264", "isHighQuality": False,
             "mechanismOfAction": ["inhibitor"], "control": None,
             "scoreInCells": 50, "scoreInOrganisms": 50, "probesDrugsScore": 67.2},
            {"id": "NOSCORE", "drugId": None, "isHighQuality": False,
             "mechanismOfAction": [], "control": None,
             "scoreInCells": None, "scoreInOrganisms": None, "probesDrugsScore": None},
        ],
        # 7 条：用来验证「列 6 条 + 还有 1 条」；最后一条没有来源也没有方向。
        "safetyLiabilities": [
            {"event": f"event {index}", "datasource": "Brennan et al. (2024)",
             "effects": [{"direction": "Inhibition/Decrease/Downregulation"}]}
            for index in range(6)
        ] + [{"event": "bare event", "datasource": None, "effects": []}],
        "associatedDiseases": {
            "count": 123,
            "rows": [
                # 故意乱序：渲染前要按分数重排。
                {"score": 0.4, "disease": {"id": "MONDO_0004992", "name": "cancer"},
                 "datatypeScores": [
                     {"id": name, "score": score}
                     for name, score in (
                         ("clinical", 0.9), ("literature", 0.8), ("genetic_association", 0.7),
                         ("affected_pathway", 0.6), ("somatic_mutation", 0.5),
                     )
                 ]},
                {"score": 0.85,
                 "disease": {"id": "MONDO_0005233", "name": "non-small cell lung carcinoma"},
                 "datatypeScores": [{"id": "clinical", "score": 0.99}]},
                {"score": 0.7, "disease": {"id": "MONDO_0005061", "name": "lung adenocarcinoma"},
                 "datatypeScores": [{"id": "literature", "score": 0.95}]},
            ],
        },
        "drugAndClinicalCandidates": {
            "count": 4,
            "rows": [
                {"maxClinicalStage": "PRECLINICAL",
                 "drug": {"id": "CHEMBL9", "name": "ZETA", "drugType": "Small molecule",
                          "parentMolecule": None}},
                # 盐型：要并到母体上，且母体自己还有一条（PHASE_4），归并后只留阶段高的那条。
                {"maxClinicalStage": "APPROVAL",
                 "drug": {"id": "CHEMBL2105712", "name": "AFATINIB DIMALEATE",
                          "drugType": "Small molecule",
                          "parentMolecule": {"id": "CHEMBL1173655", "name": "AFATINIB"}}},
                {"maxClinicalStage": "PHASE_3",
                 "drug": {"id": "CHEMBL3678958", "name": "RUSERONTINIB",
                          "drugType": "Small molecule", "parentMolecule": None}},
                {"maxClinicalStage": "PHASE_4",
                 "drug": {"id": "CHEMBL1173655", "name": "AFATINIB",
                          "drugType": "Small molecule", "parentMolecule": None}},
            ],
        },
    }
    target.update(overrides)
    return {"data": {"target": target}}


def _ot_search(*hits: tuple[str, str]) -> dict[str, object]:
    """`search` 的返回。`hits` 是 `(编号, 名字)`。"""
    return {
        "data": {
            "search": {
                "total": len(hits),
                "hits": [
                    {"id": identifier, "name": name, "entity": "target", "description": ""}
                    for identifier, name in hits
                ],
            }
        }
    }


def _ot_disease(*rows: tuple[float, str, str], count: int = 0) -> dict[str, object]:
    """`disease` 的返回。`rows` 是 `(分数, 符号, 编号)`。"""
    return {
        "data": {
            "disease": {
                "id": "MONDO_0005061",
                "name": "lung adenocarcinoma",
                "associatedTargets": {
                    "count": count or len(rows),
                    "rows": [
                        {"score": score,
                         "target": {"id": identifier, "approvedSymbol": symbol,
                                    "approvedName": f"{symbol} full name"},
                         "datatypeScores": [{"id": "literature", "score": score}]}
                        for score, symbol, identifier in rows
                    ],
                },
            }
        }
    }


async def check_opentargets() -> None:
    """Open Targets：名字→编号、可成药性分模态、安全性/药物/疾病的截断与排序、两个方向的工具。

    全程打桩。断言盯三件最容易错的事：**把基因名当 Ensembl 编号发出去**（接口回 null
    而不是报错，沉默失败）、**把未成立的证据也列出来**（25 项全列没人看）、
    **把接口给的任意顺序当排序**（「前 5 个」就成了随便 5 个）。
    """
    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import opentargets as ot_module
    from vsagent.core.agent.domain import rsearch as rsearch_module
    import copy

    original_post = http_module.post_json
    original_dir = rsearch_module.search_dir
    tmp = ROOT / ".probe" / "opentargets-cache"
    shutil.rmtree(tmp, ignore_errors=True)
    # 缓存的路径拼法在 `rsearch.cache_path` 里，它调的是 **rsearch 的** `search_dir`。
    rsearch_module.search_dir = lambda: tmp
    calls: list[tuple[str, dict[str, object], dict[str, object]]] = []
    replies: dict[str, object] = {}

    def clear_cache() -> None:
        """清掉这一轮写下的缓存。**每一节都要从「没查过」开始**，否则第二节起就是读缓存，
        断言里的请求次数会莫名其妙变成 0。"""
        for path in tmp.glob("*.json"):
            path.unlink()

    def post_json(url, payload, **kwargs):
        calls.append((url, dict(payload), dict(kwargs)))
        query = str(payload.get("query") or "")
        for marker, reply in replies.items():
            if f"query {marker}" in query:
                if isinstance(reply, BaseException):
                    raise reply
                # 接口真的会按 page.size 分页，夹具也要照做——不然「截断」那条断言
                # 测的是夹具自己没截断，而不是工具截没截。
                body = copy.deepcopy(reply)
                size = payload["variables"].get("size") or payload["variables"].get("diseases")
                data = body.get("data") if isinstance(body, dict) else None
                if size and isinstance(data, dict):
                    block = None
                    if marker == "Disease":
                        block = (data.get("disease") or {}).get("associatedTargets")
                    elif marker == "Profile":
                        block = (data.get("target") or {}).get("associatedDiseases")
                    if isinstance(block, dict):
                        # 接口默认按关联分排序再分页（`orderByScore` 的默认值），夹具照做。
                        block["rows"] = sorted(
                            block["rows"], key=lambda row: row["score"], reverse=True
                        )[: int(size)]
                return body
        raise AssertionError(f"没预料到的 GraphQL 查询：{query[:80]}")

    async def fresh(tool, arguments: dict[str, object]):
        """清缓存再跑——除了缓存那一节，其余各节验的都是「这一次请求拿到了什么」。"""
        clear_cache()
        return await tool.run(arguments)

    try:
        http_module.post_json = post_json
        druggable = ot_module.CheckDruggabilityTool()
        diseases = ot_module.DiseaseTargetsTool()

        # ---- 1. 基因名先解析成编号，再查档案 ----
        replies.update({"Search": _ot_search(("ENSG00000146648", "EGFR")),
                        "Profile": _ot_target()})
        result = await fresh(druggable, {"target": "EGFR"})
        check(not result.failed, f"按基因名查成功：{result.output[:160]}")
        check(len(calls) == 2, f"先搜再查，共两次请求：{len(calls)}")
        check("query Search" in str(calls[0][1]["query"]), "第一次是检索")
        check(calls[0][1]["variables"] == {"q": "EGFR", "entities": ["target"], "size": 5},
              f"检索参数：{calls[0][1]['variables']}")
        check(calls[1][1]["variables"]["id"] == "ENSG00000146648",
              f"第二次用解析出来的编号：{calls[1][1]['variables']}")
        check(calls[1][1]["variables"]["diseases"] == ot_module.DEFAULT_DISEASE_LIMIT,
              f"疾病条数用默认值：{calls[1][1]['variables']}")

        # ---- 2. 输出：解析行、身份行、功能（剥掉文献与证据编号）----
        check("EGFR → ENSG00000146648" in result.output, "解析结果单独一行，便于复用")
        check("EGFR（ENSG00000146648） · epidermal growth factor receptor" in result.output,
              "身份行给符号、编号与全名")
        check("位置 7 号染色体 55,018,820–55,211,628" in result.output, "位置带千分位")
        check("PubMed:12345" not in result.output and "ECO:" not in result.output,
              f"功能注释里的文献/证据编号剥掉了：{result.output[:200]}")
        check("Receptor tyrosine kinase that binds EGF." in result.output,
              "功能正文保留（剥编号时不能把整句吃掉）")

        # ---- 3. 可成药性：只列成立的，没成立过的模态整行不出现 ----
        check("小分子（SM）：已批准药物、高质量口袋" in result.output,
              f"只列成立的证据：{result.output[:400]}")
        check("中等质量口袋" not in result.output, "value=False 的条目不出现")
        check("抗体（AB）" not in result.output, "一条都没成立的模态不占一行")
        check("PROTAC/降解剂（PR）：文献支持" in result.output, "PR 模态也按代码翻成中文")

        # ---- 3.1 化学探针：高质量优先，再按评分；截断要说还剩几个 ----
        check(
            "化学探针（经选择性验证的阳性对照，共 7 个，高质量优先，列出前 5 个）："
            in result.output,
            f"探针段要说清总数与排序口径：{result.output[400:900]}",
        )
        probe_part = result.output[result.output.index("化学探针"):result.output.index("安全性")]
        check(
            probe_part.index("P4B") < probe_part.index("GEFITINIB") < probe_part.index("ACBI3")
            < probe_part.index("DDC-01-163") < probe_part.index("LOWQ"),
            f"高质量在前、评分从高到低：{probe_part}",
        )
        check(
            "ARS-1620" not in probe_part,
            "超过上限的低质量探针不列（低质量评分再高也排在后面）",
        )
        check("（还有 2 个）" in probe_part, "截断时说明还剩几个")
        check("CHEMBL939" in probe_part, "探针带 ChEMBL 编号，可接着查它是什么药")
        check("抑制剂" in probe_part and "降解剂" in probe_part, "作用方式翻成中文")
        check(
            "inverse agonist" in probe_part,
            "查不到译名的原样印英文（猜一个中文更容易误导）",
        )
        check("对照化合物：Cis-ACBI3" in probe_part, "有对照化合物就单独一行")
        check(
            "复现不出来" in probe_part and "没有基准可校" in probe_part,
            "说清它为什么值得先对接一遍",
        )
        check(result.meta.get("probe_count") == 7, f"meta 里探针总数：{result.meta.get('probe_count')}")
        check(
            [item["name"] for item in result.meta.get("probes") or []][:2] == ["P4B", "GEFITINIB"],
            f"meta 里的探针也按同一顺序：{result.meta.get('probes')}",
        )

        # 没有探针的靶点：说清后果（对接结果没有基准），不是一句「无数据」。
        replies["Profile"] = _ot_target(chemicalProbes=[])
        bare = await fresh(druggable, {"target": "EGFR", "disease_limit": 2, "drug_limit": 2})
        check(
            "化学探针：没有收录" in bare.output and "没有基准可校" in bare.output,
            f"空探针要说出后果：{bare.output[400:800]}",
        )
        check(bare.meta.get("probe_count") == 0, "meta 里探针数是 0")
        replies["Profile"] = _ot_target()

        # ---- 4. 安全性：列 6 条 + 还有几条 ----
        check("安全性（7 条记录，列出前 6 条）" in result.output, "安全性说清总数与列了几条")
        check("（还有 1 条）" in result.output, "剩下的条数要写出来")
        check("- bare event" not in result.output, "被截断的那条不出现")
        check("（Brennan et al. (2024)，抑制/下调）" in result.output, "方向翻成中文")

        # ---- 5. 药物：按阶段排序、盐型并入母体 ----
        check("已知药物与临床候选（共 3 个，接口原始 4 条（盐型已并入母体）" in result.output,
              f"盐型归并后的个数与原始条数都要说：{result.output[-900:]}")
        check(result.output.count("AFATINIB") == 1, "AFATINIB 的盐型没有单独占一条")
        check("AFATINIB DIMALEATE" not in result.output, "盐型名不出现")
        ordered = [result.output.index(name) for name in ("AFATINIB", "RUSERONTINIB", "ZETA")]
        check(ordered == sorted(ordered), f"按最高临床阶段排序：{ordered}")
        check("AFATINIB · Small molecule · 已批准 · CHEMBL1173655" in result.output,
              "母体的编号与阶段都取归并后那条")

        # ---- 6. 疾病：重排、证据分解只列前四路 ----
        check("疾病关联（共 123 条，按关联分排序，列出前 3 条）" in result.output,
              "疾病总数与列出的条数分开写")
        first = result.output.index("non-small cell lung carcinoma")
        second = result.output.index("lung adenocarcinoma")
        third = result.output.index("MONDO_0004992")
        check(first < second < third, "按分数从高到低重排，不照接口顺序")
        check("0.85 non-small cell lung carcinoma（MONDO_0005233）" in result.output,
              "分数保留两位")
        check("另有 1 路" in result.output, "证据多于四路时写明还剩几路")
        check("不是概率，也不跨靶点比较大小" in result.output, "分数的口径要写清")

        # ---- 7. meta ----
        check(result.meta["ensembl_id"] == "ENSG00000146648", "meta 带编号")
        check(result.meta["symbol"] == "EGFR", "meta 带符号")
        check(result.meta["disease_count"] == 123 and result.meta["drug_count"] == 3,
              f"meta 里的计数：{result.meta.get('disease_count')} / {result.meta.get('drug_count')}")
        check(result.meta["source"] == "opentargets", "meta 标来源")

        # ---- 8. 已经是编号就不再搜 ----
        calls.clear()
        await fresh(druggable, {"target": "ensg00000146648"})
        check(len(calls) == 1 and "query Profile" in str(calls[0][1]["query"]),
              f"编号直通，只发一次查询：{[str(c[1]['query'])[:20] for c in calls]}")
        check(calls[0][1]["variables"]["id"] == "ENSG00000146648", "小写编号按大写发出去")

        # ---- 9. 搜不到 / 编号不存在 ----
        replies["Search"] = _ot_search()
        missing = await fresh(druggable, {"target": "FOOBAR1"})
        check(missing.failed and "搜不到" in missing.output, f"搜不到照实说：{missing.output[:120]}")

        replies["Profile"] = {"data": {"target": None}}
        absent = await fresh(druggable, {"target": "ENSG00000000000"})
        check(absent.failed and "没有" in absent.output and "ENSG00000000000" in absent.output,
              f"编号不存在要说清是编号的问题：{absent.output[:160]}")

        # ---- 10. GraphQL 层的两类错误 ----
        replies["Profile"] = {"errors": [{"message": "Cannot query field 'nope' on type 'Target'."}]}
        rejected = await fresh(druggable, {"target": "ENSG00000146648"})
        check(rejected.failed and "Cannot query field" in rejected.output,
              f"200 里的 errors 也要接住：{rejected.output[:160]}")

        replies["Profile"] = http_module.ResponseError(
            "接口拒绝了请求（HTTP 400）：bad request", status_code=400
        )
        refused = await fresh(druggable, {"target": "ENSG00000146648"})
        check(refused.failed and "Open Targets 查询失败" in refused.output,
              f"4xx 翻成工具失败：{refused.output[:160]}")

        replies["Profile"] = http_module.RetryableToolError("接口暂时不可用（HTTP 503）")
        retried = None
        try:
            await fresh(druggable, {"target": "ENSG00000146648"})
        except http_module.RetryableToolError as error:
            retried = str(error)
        check(retried is not None, "瞬时故障原样抛给内核（不伪装成参数错）")

        # ---- 11. 参数边界 ----
        replies["Profile"] = _ot_target()
        await fresh(druggable, {"target": "ENSG00000146648", "disease_limit": 99, "drug_limit": 99})
        check(calls[-1][1]["variables"]["diseases"] == ot_module.MAX_DISEASE_LIMIT,
              f"疾病条数夹到上限：{calls[-1][1]['variables']}")
        bad_limit = await fresh(druggable, {"target": "EGFR", "disease_limit": "x"})
        check(bad_limit.failed and "整数" in bad_limit.output, f"非整数拦住：{bad_limit.output[:80]}")
        empty = await fresh(druggable, {"target": "  "})
        check(empty.failed, "空参数直接失败")

        # ---- 12. 反方向：疾病 → 靶点 ----
        calls.clear()
        replies["Search"] = {
            "data": {"search": {"total": 1, "hits": [
                {"id": "MONDO_0005061", "name": "lung adenocarcinoma", "entity": "disease"}
            ]}}
        }
        replies["Disease"] = _ot_disease(
            (0.5, "TP53", "ENSG00000141510"),
            (0.82, "EGFR", "ENSG00000146648"),
            (0.7, "KRAS", "ENSG00000133703"),
            count=10770,
        )
        found = await fresh(diseases, {"disease": "lung adenocarcinoma", "limit": 2})
        check(not found.failed, f"疾病→靶点跑通：{found.output[:160]}")
        check(calls[0][1]["variables"] == {"q": "lung adenocarcinoma",
                                           "entities": ["disease"], "size": 5},
              f"疾病检索参数：{calls[0][1]['variables']}")
        check("lung adenocarcinoma（MONDO_0005061）：关联靶点共 10770 个" in found.output,
              "首行给疾病名、编号与总数")
        check(found.output.index("EGFR") < found.output.index("KRAS"),
              "按分数排序（EGFR 0.82 在 KRAS 0.70 前）")
        check("（还有 10768 个；调大 limit 再看）" in found.output,
              f"截断要说清还剩多少：{found.output[-200:]}")
        check(found.meta["disease_id"] == "MONDO_0005061", "meta 带疾病编号")
        check(found.meta["total"] == 10770, "meta 带总数")

        # ---- 13. 疾病侧的空结果与参数 ----
        replies["Disease"] = _ot_disease(count=0)
        nothing = await fresh(diseases, {"disease": "MONDO_0005061"})
        check(not nothing.failed and "共 0 个" in nothing.output,
              f"零命中不算失败：{nothing.output[:120]}")
        clipped = await fresh(diseases, {"disease": "MONDO_0005061", "limit": 999})
        check(not clipped.failed and calls[-1][1]["variables"]["size"] == ot_module.MAX_ASSOC_LIMIT,
              f"limit 夹到上限：{calls[-1][1]['variables']}")
        replies["Search"] = _ot_search()
        unknown = await fresh(diseases, {"disease": "nosuch disease"})
        check(unknown.failed and "搜不到" in unknown.output, f"疾病名搜不到照实说：{unknown.output[:120]}")

        # ---- 14. 缓存：键是「查询 + 变量」，失败不落缓存 ----
        replies.update({"Search": _ot_search(("ENSG00000146648", "EGFR")),
                        "Profile": _ot_target()})
        clear_cache()
        calls.clear()
        first = await druggable.run({"target": "EGFR"})
        check(len(calls) == 2, f"第一次联网两次：{len(calls)}")
        cached_files = sorted(path.name for path in tmp.glob("*.json"))
        check(len(cached_files) == 2, f"两次查询各落一份缓存：{cached_files}")
        check(all(name.startswith("opentargets-") for name in cached_files),
              f"缓存文件名带来源前缀，与检索的缓存分得开：{cached_files}")

        again = await druggable.run({"target": "EGFR"})
        check(len(calls) == 2, f"第二次不再联网：{len(calls)}")
        check(again.output == first.output, "缓存命中时输出与第一次一致")

        # 换了疾病条数就是另一次查询——只按编号做键的话这里会拿到 5 条那份。
        await druggable.run({"target": "EGFR", "disease_limit": 2})
        check(len(calls) == 3, f"参数不同不共用缓存：{len(calls)}")
        check(calls[-1][1]["variables"]["diseases"] == 2, "确实按新的条数查了一次")

        # 缓存坏掉不能变成假结果：`read_cache` 认不出就当没有，重新联网。
        for path in tmp.glob("*.json"):
            path.write_text("{ 这不是 JSON", encoding="utf-8")
        calls.clear()
        recovered = await druggable.run({"target": "ENSG00000146648"})
        check(not recovered.failed and len(calls) == 1,
              f"缓存损坏时重新联网：{len(calls)} 次请求，失败={recovered.failed}")

        # 失败的应答不落缓存：先 4xx，再正常——第二次必须真的发请求。
        clear_cache()
        replies["Profile"] = http_module.ResponseError("拒绝了（HTTP 400）", status_code=400)
        calls.clear()
        broken = await druggable.run({"target": "ENSG00000146648"})
        check(broken.failed, "先制造一次失败")
        check(len(list(tmp.glob("*.json"))) == 0, "失败的应答不写缓存")
        replies["Profile"] = _ot_target()
        calls.clear()
        healed = await druggable.run({"target": "ENSG00000146648"})
        check(not healed.failed and len(calls) == 1, f"失败之后重查要真的联网：{len(calls)}")

        # 库层的开关：`use_cache=False` 绕过缓存（工具本身不暴露这个参数）。
        calls.clear()
        ot_module.target_profile("ENSG00000146648", use_cache=False)
        check(len(calls) == 1, f"use_cache=False 时直接联网：{len(calls)}")
    finally:
        http_module.post_json = original_post
        rsearch_module.search_dir = original_dir
        shutil.rmtree(tmp, ignore_errors=True)


async def check_websearch() -> None:
    """联网搜索：凭据三档、查询归档不过期、摘要落库、抓取升全文、空结果不是失败。

    全程离线：`http.post_json` 打桩，语料根与归档目录都指到临时目录。**不碰
    `~/.vsagent/search/`** —— 打桩能挡住网络，却挡不住上一次真实运行留下的
    归档命中，那会让「第二次没联网」这条断言在别人的机器上假绿。
    """
    import re
    import tempfile
    from pathlib import Path as _Path

    from vsagent.core import corpus as corpus_module
    from vsagent.core import corpus_config as corpus_config_module
    from vsagent.core import search_config as search_config_module
    from vsagent.core.agent.domain import corpus as corpus_tools
    from vsagent.core.agent.domain import http as http_module
    from vsagent.core.agent.domain import rsearch as rsearch_module
    from vsagent.core.agent.domain import websearch as websearch_module

    root = _Path(tempfile.mkdtemp(prefix="vsa-web-"))
    archive = root / "search"
    store = root / "corpus"

    original_search_dir = rsearch_module.search_dir
    original_root = corpus_module.corpus_root
    original_resolve = search_config_module.resolve
    original_post = http_module.post_json
    original_request = http_module.httpx.request
    original_built = corpus_config_module.load
    rsearch_module.search_dir = lambda: archive
    corpus_module.corpus_root = lambda: store
    # 入库开关按判据固定，不读仓里那份 corpus.toml：否则别人的配置会让这组假绿。
    corpus_config_module.load = lambda root=None: corpus_config_module.CorpusConfig(
        index_web=True, embed=False
    )

    config = search_config_module.SearchConfig(
        base_url="https://api.deepseek.com/v1", api_key="sk-test-secret", source="test"
    )
    search_config_module.resolve = lambda: config

    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def kind(needle: str) -> int:
        """按 URL 里的路径片断数调用了几次。"""
        return sum(1 for url in (item[0] for item in calls) if needle in url)

    def hit(url: str, title: str, *, summary: str = "摘要", authority: int = 3) -> dict[str, Any]:
        return {
            "rerank_score": 2.4,
            "title": title,
            "url": url,
            "snippet": summary[:100],
            "summary": summary,
            "content": "",
            "site_name": "Example",
            "site_icon": "https://example.com/favicon.ico",
            "date": "2026-06-01T00:00:00+00:00",
            "authority": authority,
        }

    long_summary = "长摘要" * 200  # 400 字，比渲染预算（400）不短，用来验截断
    paper_url = "https://example.com/paper-1"
    egfr_url = "https://example.com/egfr?tab=1#section"
    results = [
        hit(paper_url, "EGFR paper", summary=long_summary),
        hit("https://example.com/paper-2", "Second paper", authority=-1),
        hit(egfr_url, "EGFR page"),
    ]

    def post_json(url: str, payload: Any, **kwargs: Any) -> Any:
        calls.append((url, dict(payload or {}), dict(kwargs.get("headers") or {})))
        if url.endswith("/v1/tools/search"):
            return {"results": list(results)}
        if url.endswith("/v1/tools/fetch"):
            target = str((payload or {}).get("url", ""))
            if "big" in target:
                return {"title": "大页面", "content": "大" * 20001}
            return {"title": "EGFR page", "content": "# EGFR\n\n" + "正文" * 400}
        raise AssertionError(f"没预料到的 POST：{url}")

    http_module.post_json = post_json
    search = websearch_module.SearchWebTool()
    fetch = websearch_module.FetchWebpageTool()
    try:
        # ---- 一、没凭据：不发请求，给三条现在就能走的路 ----
        search_config_module.resolve = lambda: None
        missing = await search.run({"query": "EGFR T790M"})
        check(missing.failed, "没凭据时明确失败，不是静默返回空")
        check(
            "没有可用的搜索凭据" in missing.output
            and search_config_module.SEARCH_ENV_KEY in missing.output,
            f"说清缺什么、怎么配：{missing.output[:120]}",
        )
        check("search_literature" in missing.output, "给出不联网也能走的那条路")
        check(not calls, f"没凭据时一次请求都不发：{calls}")
        search_config_module.resolve = lambda: config

        # ---- 二、query 超长：先拦下来，不花一次 400 的往返 ----
        too_long = await search.run({"query": "x" * 129})
        check(too_long.failed and "128" in too_long.output, f"超长 query 被拦下：{too_long.output[:80]}")
        check(not calls, "拦下来之后也没发请求")

        # ---- 三、正常搜索：请求恒发 10 条，本地按 count 切 ----
        first = await search.run({"query": "EGFR T790M", "count": 2})
        check(not first.failed, f"搜索跑通：{first.output[:200]}")
        check(len(calls) == 1, f"只发了一次请求：{len(calls)}")
        called_url, payload, headers = calls[0]
        check(called_url == "https://api.deepseek.com/v1/tools/search",
              f"base 带 /v1 时不拼成 /v1/v1：{called_url}")
        check(
            payload["count"] == 10 and payload["need_content"] is False,
            f"请求恒发 10 条、不取正文（单价与条数无关）：{payload}",
        )
        check(
            headers.get("Authorization") == "Bearer sk-test-secret",
            f"带上了 Bearer：{headers.get('Authorization')}",
        )
        check(first.meta["count"] == 2, f"只列 count 条：{first.meta['count']}")
        check(first.meta["total"] == 3, f"总数报的是返回条数：{first.meta['total']}")
        check(len(first.meta["stored"]) == 3, f"返回的每一条都入库：{first.meta['stored']}")
        for ref in first.meta["stored"]:
            check(re.match(r"^web#[0-9a-f]{10}$", ref), f"引用号形状固定：{ref}")
        check(
            "长摘要" * 200 not in first.output and "…" in first.output,
            "长摘要按 400 字截断，不把 800 字的整段塞进上下文",
        )

        # 落盘：content.md + meta.json，depth 是 summary
        for ref in first.meta["stored"]:
            source_id = ref.split("#", 1)[1]
            page_dir = store / "web" / source_id
            check(
                (page_dir / "content.md").is_file() and (page_dir / "meta.json").is_file(),
                f"两份文件都落盘了：{page_dir}",
            )
        egfr_id = corpus_module.web_id(egfr_url)
        egfr_dir = store / "web" / egfr_id
        egfr_meta = json.loads((egfr_dir / "meta.json").read_text(encoding="utf-8"))
        check(egfr_meta["depth"] == "summary", f"搜索落的是摘要源：{egfr_meta['depth']}")
        check(egfr_meta["queries"] == ["EGFR T790M"], f"记下是哪个 query 搜到的：{egfr_meta['queries']}")
        check(egfr_meta["authority"] == 3, f"权威度原样存下：{egfr_meta['authority']}")

        # 落盘之后必须**当场可检索**：工具说明写的是「之后免费可查」，而 S0/S1 里
        # 这一步是漏的——要等下一次 reindex 才看得见，模型会以为刚搜到的东西丢了。
        from vsagent.core import index as index_module

        conn = index_module.connect()
        try:
            indexed = conn.execute("SELECT COUNT(*) FROM sources WHERE id LIKE 'web:%'").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            conn.close()
        check(indexed == 3, f"三条搜索结果当场进索引：{indexed}")
        check(chunks >= 3, f"索引里有块：{chunks}")

        # 入库不等于能查：RAG 的契约是「搜到的网页当场能被 search_corpus 检索到」。
        # 上面那两条只验了 sources / chunks 表里有行——表里有行而检索查不到，
        # 症状与「没入库」一样（模型以为刚搜到的东西丢了），但修的地方完全不同。
        found = await corpus_tools.SearchCorpusTool().run({"query": "EGFR T790M"})
        refs = found.meta.get("refs") or []
        check(
            not found.failed and any(ref.startswith("web#") for ref in refs),
            f"刚搜到的网页当场能被本地检索命中：{refs}",
        )

        # ---- 四、归档永久有效：同一 query 再搜不联网、不计费 ----
        again = await search.run({"query": "EGFR T790M"})
        check(len(calls) == 1, f"第二次没发请求（归档不过期）：{len(calls)}")
        check(again.meta["cached"] is True, "标出这次读的是档案")
        check("没有重新联网" in again.output, f"明说这次没花钱：{again.output[:80]}")

        # refresh 是唯一的绕行方式
        await search.run({"query": "EGFR T790M", "refresh": True})
        check(len(calls) == 2, f"refresh 会重新联网：{len(calls)}")

        # ---- 四之二、index_web 关掉：退回纯查询，不给引用号 ----
        before = sorted(path.name for path in (store / "web").iterdir())
        corpus_config_module.load = lambda root=None: corpus_config_module.CorpusConfig(
            index_web=False, embed=False
        )
        plain = await search.run({"query": "另一个 query", "refresh": True})
        check(not plain.failed, f"关掉入库也能搜：{plain.output[:80]}")
        check(plain.meta.get("indexed") is False, "标出这次没有入库")
        check("web#" not in plain.output, f"没入库就不给引用号（给了也读不到）：{plain.output[:80]}")
        check(
            sorted(path.name for path in (store / "web").iterdir()) == before,
            "关掉之后一条都没落盘",
        )
        corpus_config_module.load = lambda root=None: corpus_config_module.CorpusConfig(
            index_web=True, embed=False
        )

        # ---- 五、抓全文：覆盖摘要、保留 labels.json ----
        (egfr_dir / "labels.json").write_text('{"display_title": "我改过的标题"}', encoding="utf-8")
        fetched = await fetch.run({"url": egfr_url, "max_chars": 600})
        check(not fetched.failed, f"抓取跑通：{fetched.output[:200]}")
        check(kind("/v1/tools/fetch") == 1, f"抓取发了一次请求：{kind('/v1/tools/fetch')}")
        check(fetched.meta["ref"] == f"web#{egfr_id}", f"同一 URL 落回同一个源：{fetched.meta['ref']}")
        content = (egfr_dir / "content.md").read_text(encoding="utf-8")
        check(content.startswith("# EGFR") and "正文" in content, f"正文换成了全文：{content[:40]}")
        upgraded = json.loads((egfr_dir / "meta.json").read_text(encoding="utf-8"))
        check(upgraded["depth"] == "full", f"depth 升成 full：{upgraded['depth']}")
        check(
            (egfr_dir / "labels.json").read_text(encoding="utf-8") == '{"display_title": "我改过的标题"}',
            "labels.json 原样保留（那是网页端唯一能写的东西）",
        )

        # 本地已有全文：不再联网、不再计费
        local = await fetch.run({"url": "https://example.com/egfr?tab=1"})
        check(not local.failed and local.meta["cached"] is True, "本地有全文时直接读本地")
        check(kind("/v1/tools/fetch") == 1, f"没发新请求：{kind('/v1/tools/fetch')}")
        check("没有联网" in local.output, f"明说这次没联网：{local.output[:60]}")
        await fetch.run({"url": egfr_url, "refresh": True})
        check(kind("/v1/tools/fetch") == 2, "refresh 才重新抓")

        # ---- 六、搜索不能把全文打回摘要 ----
        await search.run({"query": "EGFR T790M", "refresh": True})
        downgraded = json.loads((egfr_dir / "meta.json").read_text(encoding="utf-8"))
        check(downgraded["depth"] == "full", f"再搜一次仍是全文：{downgraded['depth']}")
        check(
            "正文" in (egfr_dir / "content.md").read_text(encoding="utf-8"),
            "正文没被摘要覆盖掉",
        )

        # ---- 七、存不下就明说 ----
        big = await fetch.run({"url": "https://example.com/big"})
        check(not big.failed and big.meta["truncated"] is True, "超长页面标出截断")
        check(
            str(websearch_module.FETCH_STORE_CHARS) in big.output,
            f"告诉模型存了多少：{big.output[:120]}",
        )

        # ---- 八、空结果不是失败 ----
        def empty_search(url: str, payload: Any, **kwargs: Any) -> Any:
            calls.append((url, dict(payload or {}), {}))
            return {"results": []}

        http_module.post_json = empty_search
        empty = await search.run({"query": "zzz 没有这个组合"})
        check(not empty.failed, "命中 0 条不算失败")
        check(empty.meta["reason"] == "checked", f"reason 是「查了没有」：{empty.meta}")
        check("确实没有" in empty.output, f"正文也这么说：{empty.output[:80]}")
        http_module.post_json = post_json

        # ---- 九、URL 归一化决定身份 ----
        check(
            corpus_module.normalize_url("HTTPS://Example.COM:443/a?b=1#frag")
            == "https://example.com/a?b=1",
            "大小写、默认端口、fragment 都归掉，query 保留",
        )
        check(
            corpus_module.web_id("https://example.com/a") == corpus_module.web_id("https://EXAMPLE.com/a#x"),
            "同一页的不同写法是同一个源",
        )
        check(
            corpus_module.web_id("https://example.com/a?b=1") != corpus_module.web_id("https://example.com/a?b=2"),
            "query 不同的地址是两个源",
        )
        bad = None
        try:
            corpus_module.normalize_url("ftp://example.com/a")
        except ValueError as error:
            bad = str(error)
        check(bad is not None, f"非 http(s) 直接拒绝：{bad}")

        # ---- 十、按状态码分档，且不回显 key ----
        def status(code: int):
            def raise_status(url: str, payload: Any, **kwargs: Any) -> Any:
                raise http_module.ResponseError(f"接口拒绝了这次请求（HTTP {code}）", status_code=code)

            return raise_status

        http_module.post_json = status(401)
        denied = await search.run({"query": "另一个 query", "refresh": True})
        check(denied.failed and "401" in denied.output, f"401 说清是 key 的问题：{denied.output[:100]}")
        check("sk-test-secret" not in denied.output, "错误里绝不回显 key")

        http_module.post_json = status(402)
        broke = await search.run({"query": "又一个 query", "refresh": True})
        check(broke.failed and "余额" in broke.output, f"402 说的是余额：{broke.output[:80]}")

        http_module.post_json = status(403)
        blocked = await fetch.run({"url": "https://example.com/blocked", "refresh": True})
        check(blocked.failed and "403" in blocked.output, f"403 说的是站点策略：{blocked.output[:80]}")

        # 抓取后端的 404 ≠ 页面不存在。实测：本机直连某站点 20 秒超时，后端回的却是 404，
        # 而老文案把它写成「资源不存在（HTTP 404）」，模型据此断定页面没了、换说法重试。
        def not_found(url: str, payload: Any, **kwargs: Any) -> Any:
            raise http_module.NotFound("后端回 404：取不到这个地址")

        http_module.post_json = not_found
        gone = await fetch.run(
            {"url": "https://example.com/backend-cannot-fetch", "refresh": True}
        )
        check(gone.failed, "后端 404 照实失败")
        check(
            "不等于页面不存在" in gone.output and "资源不存在" not in gone.output,
            f"后端 404 不能说成「资源不存在」：{gone.output[:140]}",
        )
        http_module.post_json = post_json

        # ---- 十一、http 层：状态码、408 可重试、OpenAI 形状的报错 ----
        # 这一小段要的是**真的** `post_json`（上面打的是工具层的桩），所以从
        # 捕获下来的原件调——打桩替换的是模块属性，函数对象本身还在。
        def fake_request(method: str, url: str, **kwargs: Any) -> Any:
            return http_module.httpx.Response(
                408, json={"error": {"message": "'count' must be between 1 and 50"}}
            )

        http_module.httpx.request = fake_request
        retried = None
        try:
            original_post("https://example.com/x", {}, retry_statuses=(408,))
        except RetryableToolError as error:
            retried = str(error)
        check(retried is not None and "408" in retried, f"408 按可重试抛：{retried}")

        refused = None
        try:
            original_post("https://example.com/x", {})
        except http_module.ResponseError as error:
            refused = error
        check(refused is not None, "不带 retry_statuses 时仍是 4xx 的默认口径")
        check(
            getattr(refused, "status_code", None) == 408,
            f"带上了状态码：{getattr(refused, 'status_code', None)}",
        )
        check(
            "count" in str(refused) and "must be between" in str(refused),
            f"OpenAI 形状的 error 里那句 message 被抠出来了：{refused}",
        )
        check("invalid_request_error" not in str(refused), "没有把 type 字段一起糊进上下文")
    finally:
        http_module.post_json = original_post
        http_module.httpx.request = original_request
        rsearch_module.search_dir = original_search_dir
        corpus_module.corpus_root = original_root
        search_config_module.resolve = original_resolve
        corpus_config_module.load = original_built
        shutil.rmtree(root, ignore_errors=True)


async def check_corpus() -> None:
    """语料链：解析 → 结构切分 → 双词法索引 → 三个工具，以及「重建两次一致」。"""
    import re as _re
    import tempfile
    from pathlib import Path as _Path

    from vsagent.core import chunker as chunker_module
    from vsagent.core import corpus as corpus_module
    from vsagent.core import index as index_module
    from vsagent.core import parse as parse_module
    from vsagent.core.agent.domain import corpus as corpus_tools

    root = _Path(tempfile.mkdtemp(prefix="vsa-corpus-"))
    store = root / "corpus"
    original_root = corpus_module.corpus_root
    corpus_module.corpus_root = lambda: store
    try:
        # ---- 一、反转义：只认 `\` + ASCII 标点 ----
        check(
            parse_module.unescape_markdown(r"\# heading \- item \/path") == "# heading - item /path",
            "抓回来的网页 Markdown 里 `\\#`、`\\-`、`\\/` 都还原",
        )
        check(
            parse_module.unescape_markdown(r"C:\\temp\\alpha") == r"C:\temp\alpha",
            "`\\\\` 还原成一个反斜杠，`\\alpha`（非标点）不动",
        )
        check(
            parse_module.unescape_markdown("# 正常标题") == "# 正常标题",
            "本来没有转义的 markdown 一个字符都不变",
        )

        # ---- 二、解析：Markdown 的结构与章节路径 ----
        markdown = (
            "# 受体结构\n\n"
            "网格蛋白抑制剂结合在激酶域的 ATP 口袋。\n\n"
            "## 方法\n\n"
            "我们用 4HJO 做对接，IC50 由四参数模型拟合。\n"
        )
        parsed = parse_module.parse_markdown(markdown)
        check(parsed.structure == "markdown", f"markdown 档：{parsed.structure}")
        sections = [block.section for block in parsed.blocks]
        check(
            sections == ["受体结构", "受体结构 / 方法"],
            f"章节路径按标题层级拼：{sections}",
        )
        bad = None
        try:
            parse_module.parse_markdown("")
        except parse_module.ParseError as error:
            bad = str(error)
        check(bad is not None, f"空文件照实报错：{bad}")
        bad = None
        try:
            parse_module.parse_path(_Path("no-such-file.docx"))
        except parse_module.ParseError as error:
            bad = str(error)
        check(bad is not None and "不支持" in bad, f"不猜格式，直接拒绝：{bad}")

        # ---- 三、PDF：书签档 / 标题正则档，两条都要能走 ----
        outline = parse_module.parse_path(ROOT / "tests" / "fixtures" / "paper-outline.pdf")
        check(outline.structure == "outline", f"有书签走 outline 档：{outline.structure}")
        check(len(outline.pages) == 3, f"页数读对：{len(outline.pages)}")
        check(
            "2 Methods / 2.1 In vitro assay" in {block.section for block in outline.blocks},
            f"层级来自书签嵌套，不从标题编号猜：{sorted({b.section for b in outline.blocks})}",
        )
        heading_lines = {
            line.strip()
            for block in outline.blocks
            for line in block.text.splitlines()
        }
        check(
            "2 Methods" not in heading_lines and "2.1 In vitro assay" not in heading_lines,
            "标题行归到 section 列里，正文不再重复它",
        )
        flat = parse_module.parse_path(ROOT / "tests" / "fixtures" / "paper-flat.pdf")
        check(flat.structure == "heading", f"没书签退回标题正则：{flat.structure}")
        check(
            any(block.section.startswith("2 Methods") for block in flat.blocks),
            f"编号标题认出来了：{sorted({b.section for b in flat.blocks})}",
        )
        check(
            any(block.page == 2 for block in flat.blocks),
            "页码跟着块走（不是渲染时猜的）",
        )

        # ---- 四、切分：不跨节、不超硬上限、重叠是整句、可复现 ----
        blocks = list(outline.blocks)
        chunks = chunker_module.chunk_blocks(blocks)
        check(len(chunks) >= 3, f"一节至少一块：{len(chunks)} 块")
        check(
            all(chunk.chars <= chunker_module.HARD_MAX for chunk in chunks),
            f"没有块超过硬上限 {chunker_module.HARD_MAX}",
        )
        seen_sections: list[str] = []
        for chunk in chunks:
            if not seen_sections or seen_sections[-1] != chunk.section:
                check(
                    chunk.section not in seen_sections,
                    f"同一节的块是连续的（没有跨节）：{chunk.section}",
                )
                seen_sections.append(chunk.section)
        again = chunker_module.chunk_blocks(list(outline.blocks))
        check(
            [(c.section, c.page, c.chunk_no, c.text) for c in chunks]
            == [(c.section, c.page, c.chunk_no, c.text) for c in again],
            "同一份输入切两遍，块逐字相同",
        )

        # 长正文：目标区间、句末切点、重叠
        sentence = "这一句用来把块撑长，讨论受体与配体的结合构象。" * 3
        long_blocks = [
            parse_module.Block(page=1, section="Long", level=1, text=f"{i}．{sentence}")
            for i in range(40)
        ]
        pieces = chunker_module.chunk_blocks(long_blocks)
        check(len(pieces) > 1, f"长正文被切成多块：{len(pieces)}")
        check(
            all(len(piece.text) <= chunker_module.HARD_MAX for piece in pieces),
            f"每块都不超过硬上限：{[len(p.text) for p in pieces]}",
        )
        check(
            all(piece.text.rstrip().endswith(("。", "．")) or piece.text.endswith("。")
                for piece in pieces[:-1]),
            "切点落在句末，不在词中间",
        )
        overlap = pieces[1].text.split("\n\n")[0]
        check(
            pieces[0].text.rstrip().endswith(overlap) and overlap != pieces[1].text,
            f"第二块以第一块的最后一句开头（重叠不是从半句开始）：{overlap[:30]}",
        )
        check(
            len(overlap) <= chunker_module.OVERLAP_MAX,
            f"重叠不超过 {chunker_module.OVERLAP_MAX} 字：{len(overlap)}",
        )

        # 不索引的章节：正文还在，但不进块
        ref_blocks = [
            parse_module.Block(page=9, section="References", level=1, text="[1] someone 2020."),
            parse_module.Block(page=9, section="参考文献", level=1, text="[1] 某人 2020。"),
        ]
        check(chunker_module.chunk_blocks(ref_blocks) == [], "References / 参考文献 不进检索")

        # ---- 五、入库：新增 / 重复 / 失败 / 目录 ----
        importer = corpus_tools.ImportDocumentsTool()
        fixture = ROOT / "tests" / "fixtures" / "paper-outline.pdf"
        first = await importer.run({"path": str(fixture)})
        check(not first.failed, f"导入跑通：{first.output[:160]}")
        check(first.meta["added"] == 1, f"新增一份：{first.meta}")
        ref = first.meta["sources"][0]
        check(_re.match(r"^doc#[0-9a-f]{10}$", ref) is not None, f"拿到引用号：{ref}")
        source_id = ref.split("#")[1]
        page_dir = store / "doc" / source_id
        check((page_dir / "source.pdf").is_file(), "原件按内容寻址复制进语料")
        check((page_dir / "text.jsonl").is_file(), "逐页原文写出来了")
        meta = json.loads((page_dir / "meta.json").read_text(encoding="utf-8"))
        check(meta["structure"] == "outline", f"切分方式记在 meta 里：{meta['structure']}")
        check(meta["title"] == "paper-outline", f"没有 PDF 标题时用文件名：{meta['title']}")
        check(meta["pages"] == 3 and meta["chunks"] >= 3, f"页数与块数：{meta}")

        duplicate = await importer.run({"path": str(fixture)})
        check(
            duplicate.meta["duplicate"] == 1 and duplicate.meta["added"] == 0,
            f"同一份内容导两次是重复，不产生第二个源：{duplicate.meta}",
        )
        check(
            duplicate.meta["sources"] == [ref],
            f"重复时给回的还是原来那个引用号：{duplicate.meta['sources']}",
        )

        reject = await importer.run({"path": str(ROOT / "tests" / "fixtures" / "ligands.smi")})
        check(reject.failed and "不支持" in reject.output, f"不支持的格式照实失败：{reject.output[:120]}")

        both = await importer.run({"path": str(ROOT / "tests" / "fixtures")})
        check(both.meta["added"] >= 1, f"目录导入：新增 {both.meta['added']}（另一份 PDF）")
        check(both.meta["failed"] == 0, f"目录里的 .pdb/.smi 是跳过，不是失败：{both.output[:200]}")
        check("已跳过" in both.output, f"跳过了几个要明说，不能悄悄少处理：{both.output[-80:]}")

        # inbox 是网页端的投递口（S2）：导完把那一份删掉，否则下一次它又被看见一遍。
        inbox = store / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        dropped = inbox / "from-web.md"
        dropped.write_text("# 从网页端投进来\n\n这一段只出现在这一份文件里。\n", encoding="utf-8")
        from_inbox = await importer.run({"path": str(dropped)})
        check(not from_inbox.failed, f"从 inbox 导入跑通：{from_inbox.output[:100]}")
        check(from_inbox.meta["added"] == 1, "inbox 里的新文件照常入库")
        check(not dropped.exists(), "导入成功后 inbox 里那一份被清掉")

        # ---- 六、检索：英文词、中文子串、特殊字符 ----
        searcher = corpus_tools.SearchCorpusTool()
        found = await searcher.run({"query": "gefitinib IC50"})
        check(not found.failed and found.meta["count"] >= 1, f"英文词命中：{found.output[:200]}")
        check(
            any("§2.1 In vitro assay" in ref for ref in found.meta["refs"]),
            f"引用落到了小节与页码：{found.meta['refs']}",
        )
        check("组命中" in found.output, f"返回里写了命中几组：{found.output[:80]}")

        chinese = root / "note.md"
        chinese.write_text(
            "# 网格蛋白\n\n网格蛋白抑制剂在低浓度下仍然有效。\n", encoding="utf-8"
        )
        await importer.run({"path": str(chinese)})
        zh = await searcher.run({"query": "抑制剂"})
        check(not zh.failed and zh.meta["count"] >= 1, f"中文子串命中（trigram 那一路）：{zh.output[:160]}")
        check(
            all("web#" not in ref for ref in zh.meta["refs"]),
            "中文命中的是本地 md，不是网页",
        )

        odd = await searcher.run({"query": "T790M-resistant (EGFR)"})
        check(not odd.failed, f"带 FTS 语法字符的查询不会崩：{odd.output[:120]}")

        empty = await searcher.run({"query": "zzz 完全不存在的词 zzz"})
        check(not empty.failed and empty.meta["reason"] == "empty", f"没命中不是失败：{empty.meta}")
        check("search_web" in empty.output, "没命中时提示可以联网查（那条是计费的）")

        # ---- 七、按引用号读原文 ----
        reader = corpus_tools.ReadSourceTool()
        page = await reader.run({"ref": ref, "page": 2})
        check(not page.failed and "gefitinib" in page.output, f"按页读到了正文：{page.output[:120]}")
        check(page.meta["pages"] == 3, f"报出总页数：{page.meta}")
        beyond = await reader.run({"ref": ref, "page": 99})
        check(beyond.failed and "3 页" in beyond.output, f"页数越界照实说：{beyond.output[:80]}")
        bad_ref = await reader.run({"ref": "D:/data/paper.pdf"})
        check(
            bad_ref.failed and "不合法" in bad_ref.output and "路径" in bad_ref.output,
            f"路径不是引用号（这一条挡住模型编造）：{bad_ref.output[:100]}",
        )
        missing = await reader.run({"ref": "doc#0000000000"})
        check(missing.failed and "没有这一份" in missing.output, "不存在的引用号照实说")

        # 网页源：只有摘要时要说清楚
        web_id = "aaaaaaaaaa"
        web_dir = store / "web" / web_id
        web_dir.mkdir(parents=True)
        (web_dir / "content.md").write_text("# 某网页\n\n摘要内容。\n", encoding="utf-8")
        (web_dir / "meta.json").write_text(
            json.dumps({"id": web_id, "kind": "web", "url": "https://example.com/a",
                        "title": "某网页", "depth": "summary", "added_at": 1.0}),
            encoding="utf-8",
        )
        web = await reader.run({"ref": f"web#{web_id}"})
        check(not web.failed and "只有摘要" in web.output, f"摘要源直说没有全文：{web.output[:100]}")
        check(web.meta["depth"] == "summary", "meta 里带着 depth")

        # ---- 八、重建：两次一致，且能回填 S0 落的网页 ----
        conn = index_module.connect(store)
        try:
            index_module.reindex(store)
            before = [
                tuple(row)
                for row in conn.execute(
                    "SELECT source_id, section, page, chunk_no, text FROM chunks "
                    "ORDER BY source_id, chunk_no"
                )
            ]
            index_module.reindex(store)
            after = [
                tuple(row)
                for row in conn.execute(
                    "SELECT source_id, section, page, chunk_no, text FROM chunks "
                    "ORDER BY source_id, chunk_no"
                )
            ]
            check(before and before == after, f"重建两次，块逐字相同（{len(before)} 块）")
            readings = index_module.stats(conn, store)
            check(readings["sources"] >= 3 and readings["chunks"] >= 3, f"读数：{readings}")
            check(readings["bytes"] > 0, "占用按库文件算，不是估的")
        finally:
            conn.close()

        backfill = await searcher.run({"query": "摘要内容"})
        check(
            any(f"web#{web_id}" in ref for ref in backfill.meta.get("refs", [])),
            f"reindex 把 corpus/web 下的网页也收进来了：{backfill.meta.get('refs')}",
        )
    finally:
        corpus_module.corpus_root = original_root
        shutil.rmtree(root, ignore_errors=True)


def _af_model_pdb(*, low: int = 12, high: int = 8) -> str:
    """一份 AlphaFold 形状的 PDB：HEADER 有日期、没有 EXPDTA，B 因子是 pLDDT。

    前 `low` 个残基给 40（低置信），其余给 95（很高）——这样「低置信区段」那条
    断言有一个确定的期望值，而不是靠真实模型碰运气。
    """
    lines = [f"{'HEADER':<6}{'':4}{'':40}{'01-AUG-25':<9}{'':10}"]
    total = low + high
    for index in range(1, total + 1):
        score = 40.0 if index <= low else 95.0
        lines.append(
            "ATOM  "
            + f"{index:5d}"
            + "  CA "
            + " "
            + f"{'ALA':>3s}"
            + " "
            + "A"
            + f"{index:4d}"
            + " "
            + "   "
            + f"{float(index):8.3f}"
            + f"{0.0:8.3f}"
            + f"{0.0:8.3f}"
            + f"{1.0:6.2f}"
            + f"{score:6.2f}"
            + "          "
            + " C"
        )
    return "\n".join(lines) + "\nEND\n"


def _af_payload(**overrides: object) -> list[dict[str, object]]:
    entry: dict[str, object] = {
        "entryId": "AF-P00533-F1",
        "latestVersion": 6,
        "pdbUrl": "https://af.example/AF-P00533-F1-model_v6.pdb",
        "cifUrl": "https://af.example/AF-P00533-F1-model_v6.cif",
        "uniprotStart": 1,
        "uniprotEnd": 20,
        "gene": "EGFR",
        "organismScientificName": "Homo sapiens",
        "modelCreatedDate": "2025-08-01T00:00:00Z",
    }
    entry.update(overrides)
    return [entry]


async def check_alphafold() -> None:
    """AlphaFold 预测模型：编号校验、pLDDT 汇总、坐标缓存、两档失败。"""
    work = PROBE / "alphafold"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    store = work / "structures"

    json_calls: list[str] = []
    text_calls: list[str] = []
    payload = _af_payload()
    pdb_text = _af_model_pdb()

    def get_json(url, params=None, *, timeout=45.0, headers=None, server_5xx_as_error=""):
        json_calls.append(url)
        return payload

    def get_text_or_none(url, params=None, *, timeout=45.0, headers=None):
        text_calls.append(url)
        # cif 也回同一份文本：这一组只关心「没有 pdbUrl 时会不会走转换那条路」，
        # 真正的 mmCIF→PDB 解析由 check_mmcif 负责。
        if url.endswith(".pdb") or url.endswith(".cif"):
            return pdb_text
        return None

    original_json = structure_module.http.get_json
    original_text = structure_module.http.get_text_or_none
    original_dir = structure_module.structure_dir
    original_cif = structure_module._cif_to_pdb
    structure_module.http.get_json = get_json
    structure_module.http.get_text_or_none = get_text_or_none
    structure_module.structure_dir = lambda: store
    try:
        ledger = SessionLedger(session_id="", snapshot_path=work / "ledger.json")
        tool = structure_module.FetchAlphafoldTool()

        # 1. 基因名不是编号：在联网之前就该拦住
        bad = await tool.execute({"uniprot_id": "EGFR"}, context=ledger)
        check(bad.failed and "不是 UniProt 编号" in bad.output, f"基因名被拦下：{bad.output}")
        check(not json_calls, f"格式错不该发请求：{json_calls}")

        # 2. 模型编号与 UniProt 编号都收
        result = await tool.execute({"uniprot_id": "AF-P00533-F1"}, context=ledger)
        check(not result.failed, f"按模型编号取模型成功：{result.output.splitlines()[0]}")
        check(
            "AlphaFold AF-P00533-F1 · AlphaFold 预测模型 v6" in result.output,
            f"首行说清是哪个模型、哪一版：{result.output.splitlines()[0]}",
        )
        check("沉积于" not in result.output, "预测模型不冒充「沉积日期」")
        check(
            "pLDDT（按 CA 逐残基，20 个）：均值 62.0" in result.output,
            "pLDDT 均值按残基算："
            + next(line for line in result.output.splitlines() if "pLDDT" in line),
        )
        check(
            "<50 60% · 50–70 0% · 70–90 0% · ≥90 40%" in result.output,
            "四档占比与夹具一致",
        )
        check(
            "链 A 1–12（12 aa，均值 40）" in result.output,
            "低置信区段报出起止与均值",
        )
        check("没有共晶配体可借" in result.output, "下游影响写出来了")

        entry = ledger.entries()[0]
        check(isinstance(entry.handle, ReceptorHandle), f"登记成 receptor：{entry.handle}")
        check(entry.meta.get("source") == "alphafold", f"来源标成 alphafold：{entry.meta.get('source')}")
        check(entry.meta.get("stage") == "raw", f"状态是原始结构：{entry.meta.get('stage')}")
        check(entry.meta.get("alphafold_id") == "AF-P00533-F1", "meta 里带模型编号")
        check(entry.meta.get("alphafold_version") == 6, "meta 里带版本")
        check(entry.meta.get("coverage") == [1, 20], f"meta 里带覆盖范围：{entry.meta.get('coverage')}")
        check(entry.meta.get("plddt_mean") == 62.0, f"meta 里带均值：{entry.meta.get('plddt_mean')}")
        check(
            entry.meta.get("source_url") == "https://alphafold.ebi.ac.uk/entry/AF-P00533-F1",
            f"来源页是条目页：{entry.meta.get('source_url')}",
        )
        check("AlphaFold 模型 AF-P00533-F1" in entry.summary, f"摘要点明是预测：{entry.summary}")

        # 3. 第二次：坐标不再下载，元数据仍问一次（版本可能变）
        downloads = len(text_calls)
        again = await tool.execute({"uniprot_id": "P00533"}, context=ledger)
        check(not again.failed, "按 UniProt 编号再取一次成功")
        check(len(text_calls) == downloads, f"坐标不重下：新增 {len(text_calls) - downloads} 次")
        check(len(json_calls) == 2, f"元数据每次问一次：{len(json_calls)} 次")
        check(len(ledger.entries()) == 1, "同一份坐标不重复登记")

        # 4. refresh 强制重下
        refreshed = await tool.execute({"uniprot_id": "P00533", "refresh": True}, context=ledger)
        check(not refreshed.failed, "refresh 成功")
        check(len(text_calls) == downloads + 1, f"refresh 真的重下：新增 {len(text_calls) - downloads} 次")

        # 5. 没有 pdbUrl 时走 cifUrl + gemmi 转换，meta 记下来源格式。
        # 转换后给一份**内容不同**的坐标：同一份内容指纹相同，会命中上一次那条登记，
        # 而那条的 source_format 是 PDB——断言就看不见这一次的结果了。
        payload[:] = _af_payload(pdbUrl="")
        structure_module._cif_to_pdb = lambda _text: _af_model_pdb(low=5, high=8)
        converted = await tool.execute({"uniprot_id": "P00533", "refresh": True}, context=ledger)
        check(not converted.failed, f"只有 mmCIF 时能转换：{converted.output.splitlines()[0]}")
        check(
            any(entry.meta.get("source_format") == "mmCIF" for entry in ledger.entries()),
            f"转换过的记下来源格式：{[e.meta.get('source_format') for e in ledger.entries()]}",
        )
        structure_module._cif_to_pdb = original_cif

        # 6. 接口说「没有模型」时给可走的路
        payload[:] = []
        missing = await tool.execute({"uniprot_id": "P69905"}, context=ledger)
        check(missing.failed and "没有可用模型" in missing.output, f"空结果照实说：{missing.output}")

        # 7. 超长蛋白被切片时要说出来
        payload[:] = _af_payload() + [
            {**_af_payload()[0], "entryId": "AF-P00533-F2", "uniprotStart": 21, "uniprotEnd": 40}
        ]
        sliced = await tool.execute({"uniprot_id": "P00533", "refresh": True}, context=ledger)
        check(not sliced.failed, "有片段时仍成功")
        check("切成 2 段" in sliced.output, f"切片说清了：{sliced.output.splitlines()[-1]}")
    finally:
        structure_module.http.get_json = original_json
        structure_module.http.get_text_or_none = original_text
        structure_module.structure_dir = original_dir
        structure_module._cif_to_pdb = original_cif


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
        if "6VXX" in url and url.endswith(".cif"):
            return _cif_fixture()
        # 9ZZZ 两个格式都 404；6VXX 的 .pdb 也是 404（只有 mmCIF）
        return None

    def probe(url, *, timeout=45.0):
        calls.append(("HEAD", url))
        if "1M17" in url or "6VXX" in url:
            return 200
        return 404

    original_text = structure_module.http.get_text_or_none
    original_probe = structure_module.http.probe
    original_dir = structure_module.structure_dir
    original_cif = structure_module._cif_to_pdb
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

        # 只有 mmCIF（超大结构）→ 没装 gemmi 时仍是原来那条错，文案逐字不变。
        # 6VXX 在这个假传输里 .pdb 是 404、.cif 是 200。
        def no_gemmi(_text: str) -> str:
            raise ImportError("No module named 'gemmi'")

        structure_module._cif_to_pdb = no_gemmi
        huge = await fetch.execute({"pdb_id": "6VXX"}, context=ledger)
        check(huge.failed, "超出 PDB 上限且没有 gemmi 时失败")
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
            calls.count(("GET", "https://files.rcsb.org/download/6VXX.pdb")) == 1,
            f"只取过一次 .pdb，失败之后不再重试：{calls}",
        )
        check(
            not (store / "6VXX.pdb").exists(),
            "转换失败不写半截文件（否则下次会被当成有效缓存读进来）",
        )
        structure_module._cif_to_pdb = original_cif

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

        # 收路径的动作只有三个：其余工具的参数里不该出现 path。
        from vsagent.core.agent.domain import corpus as corpus_tools

        path_takers = [
            tool.spec.name
            for tool in (
                *uniprot_module.domain_tools(),
                *structure_module.domain_tools(),
                *ligand_module.domain_tools(),
                *corpus_tools.domain_tools(),
            )
            if "path" in (tool.spec.parameters.get("properties") or {})
        ]
        check(
            path_takers == ["import_structure", "import_ligandset", "import_documents"],
            f"全库只有三个动作收路径（其余一律收 handle 或引用号）：{path_takers}",
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
        structure_module._cif_to_pdb = original_cif
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

        # ---- describe_molecule：库函数（工具入口并进了 pubchem 的 lookup_compound）----
        # 合并之后不再有独立的 describe_molecule 工具，但画像这一段渲染仍归本模块管，
        # 所以在这里按库函数验；两半怎么接起来在 check_pubchem 的第 11 段。
        calls.clear()
        profile_text, profile_meta = chembl_module.describe_molecule("chembl939")
        check(
            "ChEMBL 分子 CHEMBL939 · GEFITINIB" in profile_text,
            f"首行报出编号与药名：{profile_text.splitlines()[0]}",
        )
        check("已上市药物" in profile_text, "max_phase 4 说成已上市")
        check("2015 年首次批准" in profile_text, "报出首次批准年份")
        check("XGALLCVXEZPNRQ-UHFFFAOYSA-N" in profile_text, "报出 InChIKey")
        check("Lipinski 五规则违反 0 项" in profile_text, "报出类药性")
        check("QED 0.63" in profile_text, "报出 QED")
        check(
            "Epidermal growth factor receptor" in profile_text
            and "（INHIBITOR）" in profile_text,
            "作用机制里的靶点名解析出来了",
        )
        check("Carcinoma, Non-Small-Cell Lung" in profile_text, "适应症拼出来了")
        check("IRESSA" in profile_text, "报出别名")
        check("L01EB01" in profile_text, "报出 ATC 分类")
        check("每次调用现查，不写盘缓存" in profile_text, "说明这是现查的，数据会随远端变")
        check(
            profile_meta["inchikey"] == "XGALLCVXEZPNRQ-UHFFFAOYSA-N",
            f"meta 带 InChIKey（合并入口靠它接 PubChem）：{profile_meta}",
        )
        check(
            profile_meta["molecule_chembl_id"] == "CHEMBL939"
            and profile_meta["max_phase"] == 4.0,
            f"meta 带编号与阶段：{profile_meta}",
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
        try:
            not_small, _meta = chembl_module.describe_molecule("CHEMBL1201583")
        finally:
            chembl_module._request_json = transport
        check(
            "没有结构式" in not_small and "不能拿来对接" in not_small,
            f"说清没有结构的后果：{not_small}",
        )

        # 编号格式不对：当场拦下，不去撞 404。
        try:
            chembl_module.describe_molecule("gefitinib")
            check(False, "非编号输入应当被拦下")
        except chembl_module.http.ToolError as error:
            check("CHEMBL939" in str(error), f"非编号输入被拦下并指路：{error}")

        # ---- 装配：模块导出两个工具，两个都要登记表 ----
        # ---- 装配：模块导出三个工具，收登记表的只有前两个 ----
        tools = chembl_module.domain_tools()
        check(
            [tool.spec.name for tool in tools]
            == ["search_actives", "find_analogs"],
            f"模块导出两个工具（画像的入口在 pubchem 族）：{[t.spec.name for t in tools]}",
        )
        check(
            [tool.context_argument for tool in tools] == ["ledger", "ledger"],
            f"两个都会登记，都要登记表：{[t.context_argument for t in tools]}",
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
    # 除了化学栈，深度学习栈也不许进主仓顶层：torch/dgl 只存在于 DTA 自己的
    # 解释器里（affinity 工具走子进程），顶层 import 会让应用起不来、exe 膨胀 2GB。
    # plip / openbabel 同理：35 MB 的 C++ 绑定，只有 analyze_pose 的一个开关要它。
    CHEM_NAMES = {"rdkit", "meeko", "gemmi", "torch", "dgl", "transformers", "plip", "openbabel"}
    offenders: list[str] = []
    scanned = 0
    for path in sorted((ROOT / "vsagent").rglob("*.py")):
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
    source = (ROOT / "vsagent" / "core" / "agent" / "domain" / "chemistry.py").read_text(
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
    # 反向的那一条**刻意不成立**：plip 在这份文件里，但不在 CHEM_DEPS 里。
    # CHEM_DEPS 管的是「准备受体、准备配体」那条链，把 plip 加进去会让没装它的人
    # 连 prepare_receptor 都调不了——两件事没有关系。这里写成断言是为了挡住
    # 后来「看见两份清单不一致就顺手对齐」的改动。
    check(
        "plip" in listed and "plip" not in chemistry_module.CHEM_DEPS,
        "plip 在依赖文件里但不在 CHEM_DEPS 里（prepare_receptor 不该因它失败）",
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
        == ["import_ligandset", "prepare_ligands", "curate_ligands", "build_reference_set",
            "align_ligands"],
        f"配体模块导出五个工具：{[item.spec.name for item in ligand_tools]}",
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
    import_tool, prepare_tool, curate_tool, reference_tool, align_tool = ligand_tools
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
    check(
        "ligands" in (curate_tool.spec.parameters.get("required") or []),
        "筛库的 ligands 是必填",
    )
    check(
        "actives" in (reference_tool.spec.parameters.get("required") or []),
        "参考集的 actives 是必填",
    )
    check(
        "ligands" in (align_tool.spec.parameters.get("required") or []),
        "叠合的 ligands 是必填",
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
        "search_pdb",
        "search_literature",
        "search_web",
        "fetch_webpage",
        "import_documents",
        "search_corpus",
        "read_source",
        "import_ligandset",
        "prepare_ligands",
        "dock",
    ):
        check(expected in names, f"新工具 {expected} 装上了（写了却没装，界面上完全看不出来）")
    check(
        "write_run" not in names and "open_viewer" not in names,
        "呈现工具常驻核心，不占领域族的条目",
    )
    # 搜索类工具是只读的：Plan 方式下必须放行（放进去就等于「查文献都做不到」）。
    from vsagent.core.agent.permission import PLAN_DENY

    for readonly in (
        "search_pdb",
        "search_literature",
        "search_web",
        "fetch_webpage",
        "search_corpus",
        "read_source",
        "open_viewer",
        "analyze_pose",
    ):
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


async def check_memory_supersede() -> None:
    """记忆治理：去重、同主题失效、冲突裁决、引用校验、长度与容量。"""
    root = PROBE / "memory_supersede"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    receptor = root / "receptor.pdb"
    receptor.write_text("ATOM      1  N   ALA A   1\n", encoding="utf-8")
    store = MemoryStore(root)

    first = store.remember("受体在 receptor.pdb", refs=["receptor.pdb"])
    again = store.remember("受体在   receptor.pdb", refs=["receptor.pdb"])
    check(again.id == first.id, "同一句话再写一次不新建条目")
    check(len(store.entries) == 1, f"去重之后只剩一条：{len(store.entries)}")

    # 同主题更新：共用引用 + 更新词 → 旧条目标失效，不硬删。
    newer = store.remember("受体改为 receptor_v2.pdb", refs=["receptor.pdb"])
    check(len(store.entries) == 2, "新值是新的一条，旧值留着")
    check(not first.is_live(), "旧值不再生效")
    check(first.superseded_by == newer.id, "旧值记着被谁取代")
    check(newer.supersedes == [first.id], "新值记着取代了谁")
    check(store.recall("") == [newer], "检索只返回生效的那一条")

    # 只是共用引用、没有更新词：不当成同一主题，两条都留着。
    other = store.remember("受体来自 4HJO 的 A 链", refs=["receptor.pdb"])
    check(first.superseded_by == newer.id, "上一步的失效没有被牵连")
    check(other.is_live() and newer.is_live(), "同一引用上的不同事实共存")
    check(len(store.recall("")) == 2, "两条互不相干的事实都在")

    # 显式 supersede：模型自己判断哪条旧了。
    manual = store.remember(
        "配体库换成 ligandset_v2.sdf", refs=["receptor.pdb"], supersedes=[other.id]
    )
    check(not other.is_live() and other.superseded_by == manual.id, "显式取代生效")

    # agent 写不存在的引用 → 拒绝；用户写 → 标 unverified，排在后面。
    rejected = None
    try:
        store.remember("不存在的文件", refs=["no_such.pdb"])
    except MemoryError as error:
        rejected = str(error)
    check(rejected is not None and "不存在" in rejected, f"agent 写坏引用被拒：{rejected}")
    user_entry = store.remember(
        "用户说受体在别处", refs=["no_such.pdb"], source="user"
    )
    check(user_entry.unverified, "用户写的坏引用标 unverified")
    check(user_entry.is_live(), "标了 unverified 仍然可用，只是排后面")

    # 长度：超过上限拒绝，并提示拆条。
    too_long = None
    try:
        store.remember("长" * 600)
    except MemoryError as error:
        too_long = str(error)
    check(too_long is not None and "拆" in too_long, f"超长条目被拒并给出做法：{too_long}")

    # 失效的条目写盘后还能读回来，字段不丢。
    revived = MemoryStore(root)
    revived.load()
    check(revived.entries[first.id].superseded_by == newer.id, "失效关系随盘保留")
    check(revived.entries[first.id].valid_to > 0, "valid_to 也留着")
    check(revived.entries[user_entry.id].unverified, "unverified 随盘保留")
    check(len(revived.all_entries(include_revoked=True)) == len(revived.entries), "--all 能看全部")
    check(
        len(revived.all_entries()) < len(revived.all_entries(include_revoked=True)),
        "默认只列还生效的",
    )

    # TTL：到点之后不再生效。
    expiring = store.remember("临时结论：今天只跑前 10 个", expires_at=time.time() - 1)
    check(not expiring.is_live(), "过期之后不再生效")

    shutil.rmtree(root, ignore_errors=True)


async def check_memory_resident() -> None:
    """常驻注入：相关度优先、1200 上限、其余仍可 recall。"""
    root = PROBE / "memory_resident"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    receptor = root / "receptor.pdb"
    receptor.write_text("ATOM      1  N   ALA A   1\n", encoding="utf-8")
    store = MemoryStore(root)

    for index in range(20):
        store.remember(
            f"第 {index} 条项目事实：中间记录很多，用来把常驻额度占满" + "细" * 40,
            refs=["receptor.pdb"],
        )
    target = store.remember("配体库在 ligandset_v3.sdf，共 120 个分子", refs=["receptor.pdb"])

    text = store.resident_text(query="配体库有多少个分子")
    check(estimate_tokens(text) <= RESIDENT_LIMIT, f"常驻正文不超过上限：{estimate_tokens(text)}")
    check("ligandset_v3" in text, "与问题相关的那条被选进常驻")
    check(text.splitlines()[0].startswith("- 配体库"), "相关的那条排在最前")
    check(len(text.splitlines()) < 20, f"不是把全部条目都塞进去：{len(text.splitlines())} 条")

    rest = [entry for entry in store.recall("", limit=100) if entry.id != target.id]
    check(len(rest) >= 10, "没进常驻的仍然在台账里")
    check(any("第 3 条" in entry.text for entry in rest), "recall 能取到没进常驻的那些")

    # 空查询不报错，仍然按「有引用优先、新的先」装。
    plain = store.resident_text()
    check(estimate_tokens(plain) <= RESIDENT_LIMIT, "空查询也守上限")
    check(plain != "", "空查询仍有内容")

    # unverified 排在后面：同样的相关度时，有凭据的先注入。
    verified = store.remember("关键参数：盒子 22A，来自 receptor.pdb", refs=["receptor.pdb"])
    guessed = store.remember("关键参数：盒子 24A（用户口述）", refs=["maybe.pdb"], source="user")
    picked = store.recall("关键参数 盒子", limit=5)
    check(picked and picked[0].id == verified.id, "有凭据的排在口述前面")
    check(any(entry.id == guessed.id for entry in picked), "口述的仍然召得回来")

    shutil.rmtree(root, ignore_errors=True)


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
    # 中间段改成「关键行 + 每 8 行留 1 行」之后，过程行不会再被一条不留地扫掉；
    # 判据换成「压掉绝大部分」——留 1/8 是骨架的代价，全丢会把结构化输出一起吃掉。
    kept_progress = sum(1 for index in range(300) if progress.format(index, index) in fed)
    check(kept_progress <= 60, f"成片的过程行被压掉大半：留了 {kept_progress}/300")
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

    # ---- 三之二、结构化清单不能被整段吃掉 ----
    #
    # 实测：20 个口袋的清单（11.4K 字符）里没有一行匹配「报错 / 结论」，
    # 旧写法把它压成 161 字符（4 行），模型下一句是「The output got compressed」，
    # 然后重跑了一遍命令——多花一整步换回它已经拿到手的数据。
    dump = "\n".join(
        f"--- {index}\n   center : [{index}.0, 2.0, 3.0]\n   radius : 5.0\n"
        f"   source : ligand\n   residues : 12 个"
        for index in range(20)
    )
    packed = compress_tool_output(dump)
    check(packed is not None, "结构化清单该压还是要压")
    assert packed is not None
    check(len(packed) < len(dump), f"压短了：{len(dump)} -> {len(packed)}")
    check(len(packed) >= len(dump) * 0.1, f"但没压成一句省略：留了 {len(packed)}/{len(dump)} 字符")
    heads = sum(1 for index in range(20) if f"--- {index}" in packed)
    check(heads >= 5, f"每条记录至少留了个头：{heads}/20")
    check(OMISSION_MARK in packed, "省略的行数写明了")

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

    # ---- 五、封顶与剩余额度：摘要与证据各有限额，剩下的都归 RECENT ----
    budget = ContextAssembler(200_000).budget()
    check(budget.available == ContextAssembler(200_000).available(), "available 与 budget 是同一个口径")
    check(budget.cap(Layer.EVIDENCE) == EVIDENCE_MAX, "证据层有封顶")
    check(budget.cap(Layer.HISTORY) == HISTORY_MAX, "历史层有封顶")
    check(budget.cap(Layer.RECENT) == 0, "RECENT 不封顶：剩余额度都给它")
    check(budget.cap(Layer.PREFIX) == 0, "前缀不参与封顶")

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


def check_context_budget() -> None:
    """预算口径与装配度量：算得对、字段齐全、能原样写进日志。

    这一节钉住的是**当前分法的不变式**，不是某个具体数字：Phase 1 换成水填预算时
    改的是「剩余额度怎么分」，`available` 的算式与这几个字段名都不该跟着变。
    现在先量清楚基线，改完再拿同一组断言对一遍。
    """
    tools = [
        {
            "type": "function",
            "function": {"name": f"tool{index}", "description": "说明" * 20},
        }
        for index in range(3)
    ]
    assembler = ContextAssembler(128_000)
    budget = assembler.budget(tools)
    check(budget.available == assembler.available(tools), "available 与 budget 同口径")
    check(
        budget.available
        == int(128_000 * CONTEXT_RATIO) - budget.reserve - budget.tools_tokens,
        f"可用额度 = 窗口 × 比例 − 输出预留 − 工具定义：{budget.available}",
    )
    check(budget.tools_tokens > 0, f"工具定义占的额度算进来了：{budget.tools_tokens}")

    session = Session(id="ses_budget", title="预算")
    for index in range(6):
        message = session.add_message(Message(role="user"))
        message.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条" + "内容" * 200))

    built = assembler.build(
        session,
        "系统提示。" * 20,
        tools=tools,
        memory_text="- 受体在 runs/1iep/receptor.pdb",
        evidence=[Evidence("receptor#1", "一行摘要", "正文" * 200)],
    )
    metrics = built.metrics
    missing = [
        key
        for key in (
            "window", "ratio", "reserve", "tools", "available", "total", "usage",
            "dropped", "dropped_upto", "degraded", "recent_turns", "summary_tokens",
            "memory_entries", "evidence_entries",
        )
        if key not in metrics
    ]
    check(not missing, f"度量字段齐全（缺：{missing}）")
    check(metrics["available"] == budget.available, "度量里的可用额度与预算同口径")
    check(metrics["tools"] == budget.tools_tokens, "度量里的工具额度与预算同口径")
    check(metrics["total"] == sum(built.usage.values()), "total 等于各层之和")
    check(
        sum(built.usage.values()) <= metrics["available"],
        f"装得下时总用量不超可用额度：{metrics['total']} / {metrics['available']}",
    )
    check(
        metrics["usage"] == {layer.value: tokens for layer, tokens in built.usage.items()},
        "度量里的分层用量与 usage 是同一份",
    )
    check(metrics["window"] == 128_000 and metrics["ratio"] == CONTEXT_RATIO, "窗口与比例照实写")
    check(metrics["memory_entries"] == 1, f"记忆条数数的是「- 」行：{metrics['memory_entries']}")
    check(metrics["evidence_entries"] == 1, "证据条数照实记")
    check(metrics["recent_turns"] >= 1, "至少有一组最近交互")
    check(memory_entry_count("（以下是本项目已知的事实）\n- 甲\n- 乙") == 2, "提示语不算一条记忆")
    check(memory_entry_count("（以下是本项目已知的事实）") == 0, "只有提示语时条数为零")

    # 度量要进事件总线与 trace，序列化不了会在写日志那一步炸掉。
    json.dumps(metrics, ensure_ascii=False)

    # ---- 水填：窗口还空着的时候，RECENT 不该被「份额」提前截断 ----
    #
    # 先前固定 50/25/25：可用 86.9k 里 RECENT 只拿 40.9k，剩下两层的额度空着也不回流。
    # 这一节量的是「剩余额度真的归了 RECENT」，不是某个具体数字。
    wide = Session(id="ses_water", title="水填")
    for index in range(6):
        message = wide.add_message(Message(role="assistant"))
        message.add_part(Part(type=PartType.TEXT, text=f"{index}" + "甲" * 9_900))
    filled = ContextAssembler(128_000).build(wide, "系统提示。" * 20)
    check(
        not filled.dropped_turns,
        f"60k 的 RECENT 一条都不该被挤出去：{filled.dropped_turns}",
    )
    check(
        filled.usage[Layer.RECENT] > 55_000,
        f"RECENT 拿走剩余额度，而不是固定的一半：{filled.usage[Layer.RECENT]}",
    )
    check(
        filled.metrics["summary_tokens"] == 0 and filled.metrics["evidence_entries"] == 0,
        "这一份没有摘要与证据，额度确实空着",
    )
    check(
        sum(filled.usage.values()) <= filled.metrics["available"],
        "水填之后仍然不超可用额度",
    )

    # ---- 封顶：引用清单与任务状态都不能无限长 ----
    long_session = Session(id="ses_refs", title="长清单")
    for index in range(30):
        message = long_session.add_message(Message(role="user"))
        message.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条"))
    long_session.compact_upto = len(long_session.messages)
    compacted = ContextAssembler(128_000).build(long_session, "系统提示。" * 20)
    header = str(compacted.messages[1]["content"])
    check("再往前还有" in header, "超过上限时写明还有多少组没列")
    check(
        header.count("- turn ") == 20,
        f"引用清单只列最近 20 组：{header.count('- turn ')}",
    )

    state = TaskState()
    state.apply(done=[f"第 {index} 步" for index in range(25)])
    rendered = state.render()
    check(rendered.count("- 第 ") == 20, f"已完成最多列 20 条：{rendered.count('- 第 ')}")
    check("另有 5 条未列出" in rendered, "截断之后写明还剩多少条")


async def check_tool_family_loop(server: FakeModelServer) -> None:
    """按族加载真的改了请求体：没装的族不在 tools 里，装完下一步就在。"""
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(LoadToolsTool())
    for tool in all_domain_tools():
        registry.register(tool)
    loop = build_loop(server, bus, registry)
    session = Session(id="ses_family_loop", title="按族加载")
    ask(session, "要跑对接，先把工具装上")
    server.clear()
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="load_tools",
                            arguments=json.dumps({"family": "scoring"}),
                        )
                    ]
                ),
                usage_chunk(100, 10),
            ]
        ),
        Reply(chunks=[*text_chunks("装好了"), usage_chunk(120, 10)]),
    )
    await loop.stream_reply(session, "")

    # 这一条是整组的要害：装配算得再对，请求体没变就是白算。
    sent = [
        {item["function"]["name"] for item in body.get("tools") or []}
        for body in server.requests
    ]
    check(len(sent) >= 2, f"一轮里至少问了两次模型：{len(sent)}")
    check("load_tools" in sent[0], "load_tools 自己在清单里（否则装不回来）")
    check("dock" not in sent[0], "没装的族不在请求体里")
    check("dock" in sent[1], "装过之后下一步就出现在请求体里")
    check("search_pdb" not in sent[1], "装一族只开一族")
    check(session.tool_families == ["scoring"], f"族记在会话上：{session.tool_families}")

    # 下一轮重新问一次，装过的族仍然在：它是会话状态，不是一轮状态。
    ask(session, "接着说")
    server.script(Reply(chunks=[*text_chunks("好"), usage_chunk(130, 10)]))
    await loop.stream_reply(session, "")
    latest = {item["function"]["name"] for item in server.requests[-1].get("tools") or []}
    check("dock" in latest, "跨轮仍然在（族是会话状态，不是一轮状态）")
    check(
        loop.context_history[-1]["tools"] < loop.context_history[-1]["available"],
        "工具定义记进了度量（否则 /context 看不出清单变大了）",
    )


async def check_context_metrics(server: FakeModelServer) -> None:
    """每一步的度量：实际输入、缓存命中、窗口占比，以及按轮清空。"""
    bus = EventBus()
    published: list[dict[str, Any]] = []
    bus.subscribe("context.metrics", lambda data: published.append(dict(data)))
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = build_loop(server, bus, registry, context_window=100_000)

    session = Session(id="ses_metrics", title="度量")
    ask(session, "先看一眼")
    server.clear()
    published.clear()
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0, call_id="c1", name="echo", arguments=json.dumps({"text": "片段"})
                        )
                    ]
                ),
                usage_chunk(100, 20, cached_tokens=80),
            ]
        ),
        Reply(chunks=[*text_chunks("看完了"), usage_chunk(200, 30, cached_tokens=120)]),
    )
    await loop.stream_reply(session, "")

    check(len(loop.context_history) == 2, f"两步各记一条度量：{len(loop.context_history)}")
    first, second = loop.context_history
    check(first["step"] == 1 and second["step"] == 2, "度量带上步号")
    check(first["input"] == 100 and first["cached"] == 80, "第一步的实际用量照实记")
    check(second["input"] == 200 and second["cached"] == 120, "第二步的实际用量照实记")
    check(abs(first["cache_hit"] - 0.8) < 1e-9, f"缓存命中率 = cached / input：{first['cache_hit']}")
    check(abs(second["cache_hit"] - 0.6) < 1e-9, f"第二步命中率：{second['cache_hit']}")
    check(
        abs(first["window_used"] - 100 / 100_000) < 1e-9,
        f"窗口占比按实际输入算：{first['window_used']}",
    )
    check(first["output"] == 20 and second["output"] == 30, "输出用量照实记")
    check(loop.last_input_tokens == 200, "最后一步的实际输入留在循环层上（主动压缩要读它）")
    check(loop.last_cached_tokens == 120, "缓存命中数也留在循环层上")
    check(len(published) == 2, f"每步发一条 context.metrics：{len(published)}")
    check(
        published[1]["cache_hit"] == second["cache_hit"] and published[1]["session_id"] == session.id,
        "事件里的数字与历史一致，并带上会话编号",
    )
    for item in loop.context_history:
        check(
            item["available"] > 0 and item["tools"] > 0 and item["total"] > 0,
            f"度量里带着装配估算：{item['available']} / {item['tools']} / {item['total']}",
        )
        check(isinstance(item["usage"], dict) and item["usage"], "度量里带着分层用量")
        check(item["evidence_entries"] >= 0, "度量里带着证据条数")

    # 新一轮从零开始：跨轮攒着的数字分不清是哪件事的步骤。
    server.clear()
    published.clear()
    server.script(Reply(chunks=[*text_chunks("好"), usage_chunk(50, 5)]))
    ask(session, "再来一次")
    await loop.stream_reply(session, "")
    check(len(loop.context_history) == 1, "新一轮开始时度量清空")
    check(loop.context_history[0]["input"] == 50, "清空之后记的是新一轮的第一步")
    check(len(published) == 1, "新一轮只发自己那一步的事件")

    # 没有用量事件时不能让 0 变成 NaN：服务端不回 usage 是老接口的常态。
    server.clear()
    server.script(Reply(chunks=[*text_chunks("没有用量")]))
    ask(session, "再来")
    await loop.stream_reply(session, "")
    plain = loop.context_history[-1]
    check(plain["input"] == 0 and plain["cache_hit"] == 0.0, "没有用量时记 0，不是 NaN")
    check(plain["window_used"] == 0.0, "没有用量时窗口占比为 0")


def check_reasoning() -> None:
    """跨轮推理只留占位（P1-2）。

    三件必须成立的事：
      · 每条 assistant 消息都带 `reasoning_content` 字段——这是 DeepSeek 的硬协议，
        缺一条就 400，所以「省」只能省内容、不能省字段；
      · 上一轮的推理换成占位符，本轮的是原文（同一轮内要能看见自己上一步的推理）；
      · 总量下降，而 `stable_prefix` 逐字节不变（前缀缓存不能被打断）。
    """
    from vsagent.core.agent import context as context_module

    prefix = "系统提示。" * 20
    session = Session(id="ses_reason", title="推理")

    first = session.add_message(Message(role="user"))
    first.add_part(Part(type=PartType.TEXT, text="先取结构"))
    old = session.add_message(Message(role="assistant"))
    old.add_part(Part(type=PartType.STEP_START, text="1"))
    old.add_part(Part(type=PartType.REASONING, text="旧推理" + "甲" * 4000))
    old.add_part(Part(type=PartType.TEXT, text="取到了"))
    # 一步只有工具调用、没有推理：补的是 `EMPTY_REASONING`，不该被当成「已压缩」。
    silent = session.add_message(Message(role="assistant"))
    silent.add_part(Part(type=PartType.STEP_START, text="2"))
    silent.add_part(Part(type=PartType.TEXT, text="继续"))

    second = session.add_message(Message(role="user"))
    second.add_part(Part(type=PartType.TEXT, text="再算口袋"))
    fresh = session.add_message(Message(role="assistant"))
    fresh.add_part(Part(type=PartType.STEP_START, text="3"))
    fresh.add_part(Part(type=PartType.REASONING, text="新推理" + "乙" * 4000))
    fresh.add_part(Part(type=PartType.TEXT, text="算好了"))

    assembler = ContextAssembler(128_000)
    built = assembler.build(session, prefix)
    assistants = [item for item in built.messages if item.get("role") == "assistant"]
    check(len(assistants) == 3, f"三条 assistant 都在请求体里：{len(assistants)}")
    check(
        all("reasoning_content" in item for item in assistants),
        "每条 assistant 都有 reasoning_content 字段（协议不变）",
    )
    check(
        assistants[0]["reasoning_content"] == REASONING_PLACEHOLDER,
        f"上一轮的推理换成占位符：{assistants[0]['reasoning_content'][:30]}",
    )
    check(
        assistants[1]["reasoning_content"] == EMPTY_REASONING,
        f"本来就没有推理的那一步保持原样：{assistants[1]['reasoning_content'][:30]}",
    )
    check(
        str(assistants[2]["reasoning_content"]).startswith("新推理"),
        "本轮的推理仍是原文",
    )
    check(built.metrics["reasoning_placeholder"] == 1, f"度量里记了换掉几条：{built.metrics['reasoning_placeholder']}")

    # 不清理的那一次：临时把函数换成空实现，比 total 与 system 消息。
    original = context_module.placeholder_stale_reasoning
    try:
        context_module.placeholder_stale_reasoning = lambda turns, messages: 0
        before = assembler.build(session, prefix)
    finally:
        context_module.placeholder_stale_reasoning = original

    check(
        built.metrics["total"] < before.metrics["total"],
        f"清理之后总量下降：{built.metrics['total']} < {before.metrics['total']}",
    )
    check(
        built.messages[0] == before.messages[0] == {"role": "system", "content": prefix},
        "stable_prefix 逐字节不变（前缀缓存不被打断）",
    )
    # 原始 Part 不能被动过：`read_turn` 取回的是逐字节原文。
    reasoning_parts = [
        part for message in session.messages for part in message.parts
        if part.type is PartType.REASONING
    ]
    check(
        all("甲" * 10 in part.text or "乙" * 10 in part.text for part in reasoning_parts),
        "会话里的推理原文一个字都没改",
    )


def check_evidence_ref() -> None:
    """证据引用名：不同的调用不能撞成一条，结构化读数优先于关键行。"""

    class FakeTool:
        """只需要两个类属性，`evidence_ref_of` 也只读这两个。"""

        def __init__(self, primary: str = "", keys: tuple[str, ...] = ()) -> None:
            self.primary_argument = primary
            self.evidence_key = keys

    grep = FakeTool("pattern")

    def grep_part(pattern: str, path: str = "") -> Part:
        return Part(
            type=PartType.TOOL,
            tool_name="grep",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"pattern": pattern, "path": path},
            tool_output="命中一行",
        )

    first = evidence_ref_of(grep_part("class Foo"), {}, grep)
    second = evidence_ref_of(grep_part("def bar"), {}, grep)
    check(first != second, f"换一个正则就是另一条引用：{first} / {second}")
    check("pattern=" in first, f"引用名带上定位参数名：{first}")

    dock = FakeTool("receptor", ("ligands",))

    def dock_part(receptor: str, ligands: str) -> Part:
        return Part(
            type=PartType.TOOL,
            tool_name="dock",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"receptor": receptor, "ligands": ligands},
            tool_output="docked 3",
        )

    check(
        evidence_ref_of(dock_part("receptor#1", "ligandset#1"), {}, dock)
        != evidence_ref_of(dock_part("receptor#1", "ligandset#2"), {}, dock),
        "同一个受体换一份配体集是两条证据",
    )
    check(
        docking_module.DockTool.evidence_key == ("ligands",),
        "dock 真的声明了配体键（不是只有测试里那个假工具）",
    )
    check(
        affinity_module.ScoreAffinityTool.evidence_key == ("ligands",),
        "打分同理：换配体集重跑是另一次",
    )

    # 结构化读数优先：结果里的 handle 比入参更能说明「这次产出了什么」。
    made = Part(
        type=PartType.TOOL,
        tool_name="prepare_receptor",
        tool_status=ToolStatus.COMPLETED,
        tool_input={"structure": "4HJO"},
        tool_output="做好了",
        tool_meta={"handle": "receptor#3", "counts": {"water": 12}},
    )
    check("receptor#3" in evidence_ref_of(made, made.tool_meta), "有 handle 就用 handle 做引用")
    check(meta_summary(made.tool_meta) == "receptor#3；water 12", f"摘要取计数：{meta_summary(made.tool_meta)}")
    composite = {"receptor": "receptor#1", "ligands": "ligandset#2"}
    check(
        evidence_ref_of(made, composite).endswith("receptor#1+ligandset#2"),
        "没有 handle 时把受体与配体集拼成一个引用",
    )

    # 长参数：只按前 20 个字截断的话，两条不同的命令会共用一条 ref，
    # 而 `EvidenceStore.add` 是按 ref **替换**的——前一条证据会无声消失。
    # 证据探针（`tools/eval_context.py --evidence`）第一次跑就撞在这里。
    bash = FakeTool("command")

    def bash_part(command: str) -> Part:
        return Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"command": command},
            tool_output="ok",
        )

    long_one = evidence_ref_of(bash_part("python tools/check.py --round 2"), {}, bash)
    long_two = evidence_ref_of(bash_part("python tools/check.py --round 7"), {}, bash)
    check(long_one != long_two, f"长命令的前缀一样也得分得开：{long_one} / {long_two}")
    check(len(long_one) <= EVIDENCE_REF_LIMIT, f"分开之后仍不超长：{len(long_one)}")
    check(
        "~" in long_one,
        f"截断过的值带一段指纹（不然还是可能撞）：{long_one}",
    )
    short = evidence_ref_of(bash_part("make test"), {}, bash)
    check("~" not in short, f"没截断就不加指纹：{short}")

    store = EvidenceStore()
    store.add_from_part(made)
    item = store.items()[0]
    check(item.ref.endswith("receptor#3") and item.source == made.id, "证据的来源指向那条 Part")
    check(item.summary.startswith("receptor#3"), "摘要来自 meta 而不是关键行")

    failed = Part(
        type=PartType.TOOL,
        tool_name="bash",
        tool_status=ToolStatus.FAILED,
        tool_input={"command": "make"},
        tool_output="缺少参数 text",
    )
    store.add_from_part(failed)
    check(store.items()[-1].failed, "失败的那条标成 failed（正文优先给它）")
    check(not item.failed, "成功的那条不标")


def check_evidence_dedupe() -> None:
    """证据正文：同文只出现一次、RECENT 里看得见就不重复、失败优先。"""
    prefix = "系统提示。" * 20
    session = Session(id="ses_ev", title="证据")
    user = session.add_message(Message(role="user"))
    user.add_part(Part(type=PartType.TEXT, text="跑一下"))
    assistant = session.add_message(Message(role="assistant"))
    tool = Part(
        type=PartType.TOOL,
        tool_name="bash",
        tool_status=ToolStatus.COMPLETED,
        tool_input={"command": "make test"},
        tool_output="结论：全部通过",
    )
    assistant.add_part(tool)

    def render(evidence: list[Evidence]) -> tuple[str, Any]:
        built = ContextAssembler(128_000).build(session, prefix, evidence=evidence)
        body = "\n".join(str(item.get("content") or "") for item in built.messages)
        return body, built

    body, built = render(
        [Evidence("bash make test", "结论：全部通过", "详细正文" * 20, source=tool.id)]
    )
    check("结论：全部通过" in body, "摘要一定给")
    check("详细正文" not in body, "来源还在 RECENT 里时不再展开一份正文")
    check("evidence_detail" not in built.degraded, "不展开是正常做法，不记降级")

    body, _ = render(
        [Evidence("bash make test", "结论：全部通过", "详细正文", source="prt_missing")]
    )
    check("详细正文" in body, "来源已经不在 RECENT 时展开正文")

    body, _ = render(
        [
            Evidence("bash a", "同一句结论", "同一段正文", source="x"),
            Evidence("bash b", "同一句结论", "同一段正文", source="y"),
        ]
    )
    check(body.count("同一句结论") == 1, f"同文只出现一次：{body.count('同一句结论')}")

    many = [
        Evidence(
            f"tool {index}",
            f"摘要 {index}",
            "补充" * 150 + f"结尾{index}",
            failed=index == 0,
            source=f"src{index}",
        )
        for index in range(30)
    ]
    body, _ = render(many)
    check("结尾0" in body, "失败的正文优先展开（它是最旧的一条）")
    check("结尾29" in body, "剩下的额度先给最新的")
    check("结尾5" not in body, "最旧的一批普通证据拿不到正文")
    check(15 <= body.count("结尾") < 30, f"只有一部分拿得到：{body.count('结尾')} 条")


async def check_tool_clear() -> None:
    """旧工具结果清理：清几条、失败不清、占位可读、原文能取回。"""
    session = Session(id="ses_clear", title="清理")
    parts: list[Part] = []
    for index in range(8):
        message = session.add_message(Message(role="user"))
        message.add_part(Part(type=PartType.TEXT, text=f"第 {index} 步"))
        assistant = session.add_message(Message(role="assistant"))
        part = Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=ToolStatus.FAILED if index == 2 else ToolStatus.COMPLETED,
            tool_input={"command": f"cmd{index}"},
            tool_output=f"原文标记{index}" + "输出内容" * 250,
        )
        assistant.add_part(part)
        parts.append(part)

    turns = split_turns(session.messages)
    assembler = ContextAssembler(128_000)
    # 一次清一批：清一条就返回的话，每清一条都会击穿一次缓存。
    cleared = assembler._clear_tool_outputs(turns)
    check(cleared == 3, f"8 条里保留最近 4 条、失败 1 条不清：清了 {cleared}")

    items = [item for turn in turns for item in turn.tool_outputs()]
    archived = [item for item in items if item["content"].startswith(TOOL_ARCHIVE_MARK)]
    check(len(archived) == 3, f"占位条数对得上：{len(archived)}")
    check(
        all("read_turn turn=" in item["content"] for item in archived),
        "占位行写明怎么取回",
    )
    failed_item = next(item for item in items if item["tool_call_id"] == parts[2].id)
    check(
        failed_item["content"].startswith("原文标记2"),
        "失败的结果不清理：原文就是下一步的输入",
    )
    newest = next(item for item in items if item["tool_call_id"] == parts[-1].id)
    check(newest["content"].startswith("原文标记7"), "最近一条不动")

    target = next(
        turn
        for turn in turns
        if turn.tool_outputs() and turn.tool_outputs()[0]["content"].startswith(TOOL_ARCHIVE_MARK)
    )
    restored = await ReadTurnTool().execute({"turn": target.index}, session=session)
    check(not restored.failed, f"read_turn 能按序号取回：{restored.output[:40]}")
    check("原文标记" in restored.output, "取回的是原文，不是占位")
    check(len(restored.output) > 500, "原文长度回来了（不是只剩一行占位）")

    page = await ReadTurnTool().execute(
        {"turn": target.index, "offset": 0, "limit": 1}, session=session
    )
    check("还有" in page.output, f"分页时写明还剩多少行怎么继续：{page.output}")

    missing = await ReadTurnTool().execute({"turn": 999}, session=session)
    check(missing.failed and "1–" in missing.output, f"序号不存在时给出范围：{missing.output}")

    # ---- 装不下时先清正文，而不是整组丢 ----
    #
    # 先前「整组装不下就不要」，等到降级链里的 recent_tool_clear 时那一组早就
    # 不在了，那一步永远轮不到。现在清旧正文腾地方，能多留好几组。
    packed = Session(id="ses_packed", title="先清后丢")
    packed_parts: list[Part] = []
    for index in range(8):
        message = packed.add_message(Message(role="user"))
        message.add_part(Part(type=PartType.TEXT, text=f"第 {index} 步"))
        assistant = packed.add_message(Message(role="assistant"))
        for slot in range(2):
            part = Part(
                type=PartType.TOOL,
                tool_name="bash",
                tool_status=ToolStatus.COMPLETED,
                tool_input={"command": f"c{index}{slot}"},
                tool_output=f"输出{index}{slot}" + "甲" * 500,
            )
            assistant.add_part(part)
            packed_parts.append(part)

    built = ContextAssembler(8_000).build(packed, "系统提示。" * 20)
    check(built.dropped == 0, f"清正文之后一组都不用丢：移出 {built.dropped}")
    check("recent_tool_clear" in built.degraded, f"降级级别里有它：{built.degraded}")
    tool_items = {
        item.get("tool_call_id"): item.get("content") or ""
        for item in built.messages
        if item.get("role") == "tool"
    }
    newest = [part.id for part in packed_parts[-4:]]
    check(
        all(not tool_items[pid].startswith(TOOL_ARCHIVE_MARK) for pid in newest),
        "最近 4 条工具结果不动",
    )
    archived_count = sum(
        1 for content in tool_items.values() if content.startswith(TOOL_ARCHIVE_MARK)
    )
    check(5 <= archived_count < 16, f"旧正文被收成占位：{archived_count} 条")
    first_tool_turn = next(
        turn
        for turn in split_turns(packed.messages)
        if any(part.id == packed_parts[0].id for part in turn.tool_parts)
    )
    restored = await ReadTurnTool().execute({"turn": first_tool_turn.index}, session=packed)
    check("输出00" in restored.output, "被收成占位的那条仍能取回原文")


def check_debug_tools() -> None:
    """调试入口的覆盖度：每个领域工具都要在 `debug_tools.CALLS` 里跑过一遍。

    这条是补出来的：`validate_docking` 从 P1 加进来之后一直没进 CALLS，
    因为它需要夹具里有一个**就是共晶配体本身**的分子，而当时没人写下这个前提，
    于是 42 个工具里唯一没被端到端跑过的，恰好是风险最高的那一个（原地 RMSD）。
    判据原本一条都不管这件事——它只看工具注册得对不对，不看调试脚本覆盖到没有。
    """
    import debug_tools

    names = [name for name, _ in debug_tools.CALLS]
    missing = sorted(set(DOMAIN_PACKAGE_TOOL_NAMES) - set(names))
    check(not missing, f"每个领域工具都在 CALLS 里跑过一遍：缺 {missing}")
    unknown = sorted(set(names) - set(DOMAIN_PACKAGE_TOOL_NAMES))
    check(not unknown, f"CALLS 里没有拼错或改名后剩下的工具名：{unknown}")
    check(
        names.index("dock") < names.index("validate_docking"),
        "重对接验证排在对接之后——它读的是对接产物，顺序反了只会读到上一轮",
    )

    # **一条 CALLS 就是一次调用**，同名工具可以出现多次（三次 import_ligandset 各导入
    # 一份夹具，两次 fetch_structure 各取一套结构）。原先把同名工具从池子里 pop 掉、
    # 参数按名字查，于是三条合成一条、用的是最后那条的参数：链看起来跑满 42 个，
    # 实际 ligands.smi 与 actives.csv 一次都没导入，参考集、指纹筛选、pose 分析全建在错的库上。
    plan = debug_tools._plan(debug_tools._tool_modules())
    check(len(plan) == len(debug_tools.CALLS), f"CALLS 有几条就跑几次：{len(plan)}/{len(debug_tools.CALLS)}")
    repeated = [name for name, _ in debug_tools.CALLS if names.count(name) > 1]
    check(
        len(repeated) == 7
        and set(repeated) == {"import_ligandset", "lookup_compound", "fetch_structure"},
        f"同名的那几条都还在计划里（三次导入 + 两个方向的合并入口 + 两套结构）：{repeated}",
    )
    by_position = [arguments for _label, tool, arguments in plan if tool.spec.name == "import_ligandset"]
    check(
        [item["path"] for item in by_position]
        == [
            "tests/fixtures/ligands.smi",
            "tests/fixtures/actives.csv",
            "tests/fixtures/series.csv",
        ],
        f"参数按位置取，不是按工具名取最后一条：{[i['path'] for i in by_position]}",
    )
    check(
        all(arguments is not None for _label, _tool, arguments in plan),
        "每个工具都有 CALLS 里的参数（漏一行会变成「跳过」而不是悄悄少跑）",
    )

    # 每个模块末尾那段 `if __name__ == "__main__"` 都要把模块名与工具表传进去。
    # 写成裸的 `main()` 时，`python -m vsagent.core.agent.domain.analysis contact_map …`
    # 这条单模块调试入口会抛 `main() missing 2 required positional arguments`——
    # 报错发生在进入工具之前，看起来像脚本本身坏了，实际是那三行没跟上入口的签名。
    import importlib
    import inspect

    import vsagent.core.agent.domain as package

    broken: list[str] = []
    for name in package.__all__:
        module = importlib.import_module(f"{package.__name__}.{name}")
        if not callable(getattr(module, "domain_tools", None)):
            continue
        source = inspect.getsource(module)
        if '__name__ == "__main__"' not in source:
            continue
        if "main(__name__, domain_tools())" not in source:
            broken.append(name)
    check(not broken, f"模块末尾的调试入口把模块名与工具表传了进去：{broken}")


async def check_tool_family() -> None:
    """按族加载：目录覆盖全部领域工具、没装的族不进请求体、装完就出现。"""
    groups = catalog()
    check(len(groups) == len(families()), f"族目录与声明一一对应：{len(groups)}")
    check(
        all(names for names in groups.values()),
        "每一族都至少有一个工具（空族会让 load_tools 装了个寂寞）",
    )
    # 这一条是整组的要害：领域工具漏登记族，它就永远常驻，瘦身白做；
    # 反过来登记了一个不存在的名字，那一族装进来也不会多出任何工具。
    covered = [name for names in groups.values() for name in names]
    check(
        sorted(covered) == sorted(DOMAIN_TOOL_NAMES),
        "每个领域工具正好登记在一族里：缺 "
        f"{sorted(set(DOMAIN_TOOL_NAMES) - set(covered))}，多 "
        f"{sorted(set(covered) - set(DOMAIN_TOOL_NAMES))}",
    )
    check(len(covered) == len(set(covered)), "同一个工具不会被登记进两族")
    check(tool_family("dock") == "scoring", "tool_family 能反查")
    check(tool_family("bash") == "", "核心工具不属于任何族")
    # `search_pdb` 原先单独立一个 `rsearch` 族，族里只有它一个，说明还写着
    # 「反向检索」——名不副实，模型因此先装 rsearch 才发现里面只有它。
    check(
        tool_family("search_pdb") == "structure",
        f"查 PDB 条目与取坐标是同一件事：{tool_family('search_pdb')}",
    )
    check("rsearch" not in groups, "不再有只有一个工具的空壳族")

    # 说明必须从目录现数：手写的那份已经漂过三处（多写一个、漏掉两个），
    # 而模型正是照这句话决定装哪一族。
    listing = catalog_text()
    check(
        all(name in listing for name in covered),
        "说明里列出了每一个领域工具（漏一个就等于让模型盲装）",
    )
    check(
        all(family in listing for family in groups),
        "说明里列出了每一族",
    )
    description = LoadToolsTool.spec.description
    check(
        listing in description and "族与工具" in description,
        "load_tools 的说明带上了族 → 工具那张表",
    )
    check(f"{len(groups)} 族" in description, "说明里的族数就是目录里的族数")
    check(
        "一次把这一轮要用的族都装上" in description,
        "load_tools 的说明里也写了「一次装完」（模型先读说明，再读返回）",
    )
    # 光比对内容抓不到「写死一个恰好正确的数」。原先这条拿 "13 族" 当哨兵，
    # 族数真的涨到 13 之后哨兵永远成立，等于没测——改成看源码：说明必须是
    # `len(families())` 现算，类体里不许出现「数字 + 族」这种字面量。
    class_source = inspect.getsource(LoadToolsTool)
    check(
        "len(families())" in class_source,
        "族数由 families() 现算",
    )
    hard_coded = [n for n in range(len(groups) + 2) if f"{n} 族" in class_source]
    check(not hard_coded, f"族数是数出来的，不是写死的：写死了 {hard_coded}")

    registry = ToolRegistry()
    for tool in (LoadToolsTool(), ReadTurnTool()):
        registry.register(tool)
    for tool in all_domain_tools():
        registry.register(tool)

    session = Session(id="ses_family", title="按族加载")
    hidden = hidden_names(session)
    check(
        hidden == frozenset(covered),
        f"新会话一个领域工具都不发：藏了 {len(hidden)} 个",
    )
    sent = {spec["function"]["name"] for spec in registry.specs(exclude=hidden)}
    check("load_tools" in sent, "load_tools 自己永远可见（否则装不回来）")
    check("read_turn" in sent, "核心工具不受按族加载影响")
    check(not (sent & set(covered)), f"没装的族一个都不在请求体里：{sorted(sent)}")
    check(
        [spec["function"]["name"] for spec in registry.specs()] == registry.names(),
        "不传 exclude 时照旧全量（评测脚本与 /tools 面板靠它）",
    )

    loaded = await LoadToolsTool().execute({"family": "scoring"}, session=session, registry=registry)
    check(not loaded.failed, f"装一族不报错：{loaded.output}")
    check(session.tool_families == ["scoring"], f"族记在会话上：{session.tool_families}")
    check("dock" in loaded.output, "装完告诉模型这一族里有什么")
    # 每装一次都改工具清单，而清单是请求前缀的一部分——实测 8 次装族里有 5 次
    # 让下一步的命中率掉到 0.22–0.39。提示写在第一次装的返回里才有用。
    check(
        "一次" in loaded.output and "缓存" in loaded.output,
        f"第一次装时提示「一次装完，否则下一步缓存失效」：{loaded.output}",
    )
    after = {spec["function"]["name"] for spec in registry.specs(exclude=hidden_names(session))}
    check("dock" in after, "装过的族出现在清单里")
    check(
        "search_pdb" not in after,
        "别的族不会跟着一起冒出来（装一族只开一族）",
    )
    check(
        [spec["function"]["name"] for spec in registry.specs(exclude=hidden_names(session))]
        == [name for name in registry.names() if name not in hidden_names(session)],
        "过滤之后仍是装配顺序，不是集合顺序",
    )

    again = await LoadToolsTool().execute({"family": "scoring"}, session=session, registry=registry)
    check(
        not again.failed and again.meta["already"] and session.tool_families == ["scoring"],
        "重复装同一族不重复记账",
    )
    check("缓存" not in again.output, "装过的族不再重复这句提醒（否则每步都是噪音）")

    unknown = await LoadToolsTool().execute(
        {"family": "nope"}, session=session, registry=registry
    )
    check(unknown.failed and "scoring" in unknown.output, f"未知族列出可选项：{unknown.output}")
    check(session.tool_families == ["scoring"], "未知族不改会话状态")

    # 轮末封存时收敛（P2-1）：族只装不卸的话，装到八族之后每步都发 12K token
    # 的工具定义，而典型的一轮只用得上两三个族。
    prune_session = Session(id="ses_family_prune")
    prune_session.tool_families = ["scoring"]
    prune_session.family_topic = {"scoring": 1}
    check(
        prune_families(prune_session, {"dock"}, 1) == []
        and prune_session.tool_families == ["scoring"],
        "这一轮用过的族不会被移除",
    )
    check(
        prune_families(prune_session, {"fetch_structure"}, 1) == [],
        "刚装的族不卸（还没轮到用）",
    )
    dropped = prune_families(prune_session, {"fetch_structure"}, 2)
    check(
        dropped == ["scoring"] and prune_session.tool_families == [],
        f"下一个阶段没用过就卸掉：{dropped}",
    )
    check(
        prune_families(prune_session, set(), 3) == [],
        "一个工具都没用过时不卸（纯聊天的一轮没有依据）",
    )

    # 被关掉的工具不许报「已装入」：那句话说出去，模型下一步就会去调它，
    # 撞上禁用文案之后转去 bash 自己实现了一遍（实测 20 步）。
    # **名单从 catalog() 现取**：literature 族原先只有一个工具，关一个就等于关整族；
    # 接上 Europe PMC 之后它有三个，写死一个名字时这条会变成「只关了一部分」，
    # 而断言还在等「整族被关掉」——测试自己先漂了。
    literature_tools = set(catalog()["literature"])
    check(len(literature_tools) >= 3, f"literature 族的工具数：{sorted(literature_tools)}")
    dead = await LoadToolsTool().execute(
        {"family": "literature"},
        session=Session(id="ses_family_dead"),
        registry=ToolRegistry(disabled=literature_tools),
    )
    check(dead.failed, f"整族被关掉时装它没有意义：{dead.output[:60]}")
    check("已装入" not in dead.output, "不许说「已装入」")
    check(
        "关掉" in dead.output and "bash" in dead.output,
        "说清是开关问题，并且拦住「自己实现一遍」",
    )
    check(dead.meta["tools"] == [], f"结构化读数里没有可用工具：{dead.meta}")

    part = await LoadToolsTool().execute(
        {"family": "structure"},
        session=Session(id="ses_family_part"),
        registry=ToolRegistry(disabled={"search_pdb"}),
    )
    check(not part.failed, f"只关掉一部分时其余照装：{part.output}")
    check(
        part.meta["tools"]
        == ["fetch_structure", "fetch_alphafold", "import_structure", "assess_structure"],
        f"装进来的只有可用的那些：{part.meta['tools']}",
    )
    check(
        part.meta["disabled"] == ["search_pdb"] and "search_pdb" in part.output,
        "被关掉的那一个要说出来，不能悄悄少一个",
    )

    # 会话存盘再读回来，装过的族要跟着回来：重开一个做过对接的会话，
    # dock 还在，否则模型会以为自己没装过、又去 load 一次。
    restored_session = Session.from_dict(session.to_dict())
    check(
        restored_session.tool_families == ["scoring"],
        f"族跟着会话存盘走：{restored_session.tool_families}",
    )
    check("dock" in hidden_names(Session.from_dict({"id": "ses_x"})), "没存过族时按全藏处理")

    # 模型看不见 dock 时会直接叫它（它记不住清单里有什么）。那一句话必须告诉它
    # 「去装哪一族」——只说 unknown tool 的话，它下一步多半是绕 bash 或编参数。
    check(family_of("dock") == "scoring" and family_of("bash") == "", "family_of 与 tool_family 一致")
    unloaded = Session(id="ses_family2")
    hint = missing_tool_reason(registry, "dock", unloaded)
    check(
        "scoring" in hint and "load_tools" in hint,
        f"没装的族要说清装哪一族：{hint}",
    )
    check("unknown tool" not in hint, "这一句不是「没有这个工具」，是「还没装」")
    check(
        "unknown tool" in missing_tool_reason(registry, "dock"),
        "不给会话时退回旧文案（调用方没改的地方行为不变）",
    )
    check(
        "unknown tool" in missing_tool_reason(registry, "dock", session),
        "装过的族不再提示去装（否则模型会来回 load 同一族）",
    )
    disabled_text = missing_tool_reason(ToolRegistry(disabled={"dock"}), "dock", unloaded)
    check("已被禁用" in disabled_text, "禁用排在按族加载之前：关掉的工具装进来也没用，不能叫它去 load")
    check(
        "别的办法" not in disabled_text,
        f"禁用文案不许教模型绕道（实测「请改用别的办法」换来 20 步 bash）：{disabled_text}",
    )
    check("bash" in disabled_text, "明确说不许自己实现一遍")
    check(
        "unknown tool" in missing_tool_reason(registry, "nonexistent_thing", unloaded),
        "真不存在的工具不编一个族出来",
    )


class FlakyTool(Tool):
    """前几次抛可重试故障，之后成功。验循环层的重试，不必连网。"""

    spec = ToolSpec(
        name="flaky",
        description="前几次抛可重试故障",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise RetryableToolError("连接失败（ConnectError）")
        return ToolResult(output="第二次成了")


class BrokenTool(Tool):
    """总是抛普通 `ToolError`（参数写错、依赖没装那类），用来验「不该重试」。"""

    spec = ToolSpec(
        name="broken",
        description="总是报错",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        self.calls += 1
        raise ToolError("参数写错了")


async def check_tool_retry() -> None:
    """瞬时故障自动重试一次；重试用尽要说清「不是参数错」，并且拦住绕道。

    `RetryableToolError` 的 docstring 一直写着「为了让重试逻辑只重试这一类」，
    但循环层原先用 `except ToolError` 把它一起接住了——工具层一次都不重试，
    那个标签等于白写。这一组盯的就是这条：分档要真的分档。
    """
    from vsagent.core.agent.domain import complexes as complexes_module  # noqa: E402
    from vsagent.core.agent.domain import http as http_module  # noqa: E402
    from vsagent.core.agent.domain import rsearch as rsearch_module  # noqa: E402

    config = ProviderConfig(base_url="http://127.0.0.1:1", api_key="x", model="fake")
    bus = EventBus()

    async def run_once(tool: Tool) -> tuple[Message, Part]:
        registry = ToolRegistry()
        registry.register(tool)
        loop = AgentLoop(
            bus,
            SilentProvider(),  # type: ignore[arg-type]
            registry,
            config,
            context_window=200_000,
            workspace_root=WORKSPACE_FIXTURE,
            policy=PermissionPolicy(default_ask=frozenset()),
        )
        session = Session(title="重试")
        session.add_message(
            Message(role="user", parts=[Part(type=PartType.TEXT, text="跑一下")])
        )
        answer = Message(role="assistant", session_id=session.id)
        session.add_message(answer)
        call = Part(
            type=PartType.TOOL,
            tool_name=tool.spec.name,
            tool_status=ToolStatus.PENDING,
            tool_input={},
        )
        answer.add_part(call)
        await loop._execute_tool(session, answer, call)
        return answer, call

    # ---- 一、抖一下就好的那种：重试一次就过，界面上看得见 ----
    flaky = FlakyTool(failures=1)
    answer, call = await run_once(flaky)
    check(flaky.calls == 2, f"瞬时故障重试了一次：调了 {flaky.calls} 次")
    check(call.tool_status is ToolStatus.COMPLETED, f"重试之后算成功：{call.tool_status}")
    check(call.tool_output == "第二次成了", f"用的是重试那一次的结果：{call.tool_output}")
    check(
        any(part.type is PartType.RETRY for part in answer.parts),
        "重试在界面上留痕（与模型请求重试同一个 Part 类型）",
    )

    # ---- 二、一直不通：只重试一次，然后失败，并且拦着它自己造一个 ----
    dead = FlakyTool(failures=99)
    answer, call = await run_once(dead)
    check(dead.calls == 2, f"重试有上限，不是无限重试：调了 {dead.calls} 次")
    check(call.tool_status is ToolStatus.FAILED, f"重试用尽算失败：{call.tool_status}")
    check("瞬时故障" in call.tool_output, f"说清是瞬时故障而不是参数错：{call.tool_output[:80]}")
    check(
        "bash" in call.tool_output and "不要" in call.tool_output,
        "明确拦住「用 bash 自己实现一遍」这条路（实测那次烧了 20 步）",
    )

    # ---- 三、参数错这类不许重试：重试多少次都一样，白等 ----
    broken = BrokenTool()
    _, call = await run_once(broken)
    check(broken.calls == 1, f"普通 ToolError 不重试：调了 {broken.calls} 次")
    check("参数写错了" in call.tool_output, f"原文回给模型：{call.tool_output}")

    # ---- 四、domain 层不许把「可重试」包成「这次没搜成」 ----
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RetryableToolError("接口暂时不可用（HTTP 503）")

    def raises_retryable(call_it: Any) -> str:
        try:
            call_it()
        except RetryableToolError as error:
            return f"ok {error}"
        except Exception as error:  # noqa: BLE001 - 包成 SearchError / ComplexError 就算失败
            return f"WRAPPED {type(error).__name__}: {error}"
        return "NO ERROR"

    original_post = http_module.post_json
    http_module.post_json = boom
    try:
        search = raises_retryable(
            lambda: rsearch_module.search_entries({"query": {}}, use_cache=False)
        )
        check(not search.startswith("WRAPPED"), f"检索的可重试错误穿得出去：{search}")
        complexes = raises_retryable(
            lambda: complexes_module.list_complexes("P00533", use_cache=False)
        )
        check(not complexes.startswith("WRAPPED"), f"复合物检索同样穿得出去：{complexes}")
    finally:
        http_module.post_json = original_post

    original_get = http_module.get_text
    http_module.get_text = boom
    try:
        download = raises_retryable(
            lambda: complexes_module.download_entry("4HJO", force=True)
        )
        check(not download.startswith("WRAPPED"), f"下载同样穿得出去：{download}")
    finally:
        http_module.get_text = original_get


def _event_fixture(steps: int) -> Session:
    """铺一份「用户说一句、助手跑一次 dock」的会话：每一步两条消息。"""
    session = Session(id=f"ses_evt{steps}", title="事件")
    for index in range(steps):
        user = session.add_message(Message(role="user"))
        user.add_part(Part(type=PartType.TEXT, text=f"第 {index} 步：跑对接"))
        assistant = session.add_message(Message(role="assistant"))
        assistant.add_part(
            Part(
                type=PartType.TOOL,
                tool_name="dock",
                tool_status=ToolStatus.FAILED if index == 1 else ToolStatus.COMPLETED,
                tool_input={"receptor": "receptor#1", "ligands": f"ligandset#{index}"},
                tool_output=f"结果{index}",
                tool_meta={"handle": f"docking#{index}"},
            )
        )
    return session


def check_live_snapshot() -> None:
    """实时快照：只带当前主题、首行预览、过期判定。"""
    from vsagent.core import live

    session = Session(title="实时")
    message = Message(role="assistant", session_id=session.id)
    session.add_message(message)
    message.add_part(Part(type=PartType.STEP_START, text="1 · fake"))
    session.pending_steps.append(
        Step(seq=1, intent="dock(x=1)", tools=["dock"], result="成功：docking#1")
    )
    running_tool = Part(
        type=PartType.TOOL,
        tool_name="analyze_pose",
        tool_status=ToolStatus.RUNNING,
        tool_input={"pose": "docking#1"},
        tool_output="第一行结果\n第二行不该出现在快照里",
    )
    message.add_part(running_tool)

    data = live.snapshot(session, running=True, now=1000.0, pid=4242)
    check(data["topicIndex"] == 1 and data["eventCount"] == 0, "快照的主题号从已封存条数推")
    check(data["steps"][0]["status"] == "completed", "已完成的步按成功标")
    check(data["current"]["tool"] == "analyze_pose", "当前工具取最后一个还在跑的调用")
    check(
        data["current"]["preview"] == "第一行结果",
        f"预览只取首行：{data['current']['preview']}",
    )
    check("第二行" not in json.dumps(data, ensure_ascii=False), "完整输出不进快照")
    check(data["calls"] == 2 and set(data["tools"]) == {"dock", "analyze_pose"}, "调用数与工具名去重")

    running_tool.tool_status = ToolStatus.AWAITING
    waiting = live.snapshot(session, running=True, now=1000.0, pid=4242)
    check(waiting["current"]["phase"] == "waiting", "等确认的那一次单独标出来")

    idle = live.snapshot(session, running=False, now=1000.0, pid=4242)
    check(idle["running"] is False and idle["current"] is None, "不跑的时候没有当前工具")

    root = ROOT / ".probe" / "live-check"
    state = root / "state.toml"
    live.write(state, session, running=True)
    stored = live.read(state, session.id)
    check(stored is not None and stored["running"], "写进去的快照读得回来")
    check(stored["stale"] is False, "刚写完不算过期")
    check(
        live.read(state, session.id, now=stored["updatedAt"] + 60)["stale"] is True,
        "一分钟没更新按可能断开处理",
    )
    check(live.read(state, "ses_missing") is None, "没有快照时返回空")

    cancelled = Event(exit_reason="cancelled")
    check(cancelled.status == "interrupted", "取消过的主题标成中断")
    check(Event(exit_reason="tool_failed").status == "failed", "工具连续失败标成失败")
    check(Event(steps=[Step(issues=["boom"])]).status == "failed", "停在失败上也标成失败")
    check(Event(steps=[Step(result="成功")]).status == "completed", "正常收尾标成完成")
    restored = Event.from_dict(Event(exit_reason="cancelled").to_dict())
    check(restored.exit_reason == "cancelled", "停止原因跟着状态文件一起回来")
    shutil.rmtree(root, ignore_errors=True)


def check_topic_views() -> None:
    """主题视图：边界、计数、状态，以及未封存那条的几种情形。"""
    session = _event_fixture(2)
    first = seal_range(session, upto=2)
    second = seal_range(session, upto=4)
    check(first is not None and second is not None, "两段各封成一条主题")

    views = topic_views(session, running=False)
    check(len(views) == 2, f"封完两条就两行：{len(views)}")
    head, tail = views
    check(head.start == (0, 0) and head.end == (2, 0), f"第一条的端点：{head.start}-{head.end}")
    check(tail.start == (2, 0) and tail.end == (4, 0), f"第二条接着前一条：{tail.start}-{tail.end}")
    check(head.contains((0, 0)) and not head.contains((2, 0)), "位置归属按左闭右开")
    check(head.key != tail.key, "两条主题的键不同")
    check(head.sealed and tail.sealed, "封存过的都标成已封存")
    check(head.steps and head.tools == ["dock"], f"工具从流水聚合：{head.tools}")
    check(head.calls == 1, f"调用数按步里的工具数：{head.calls}")
    check(head.artifacts == ["docking#0"], f"产物带出来：{head.artifacts}")
    check(tail.status == "failed", f"最后一步有未决项就是失败：{tail.status}")
    check(tail.issues and "结果1" in tail.issues[0], f"未决项写在主题上：{tail.issues}")

    # 未封存的尾部：跑着的是进行中，进程不在了是未封存，纯聊天的一轮是已完成。
    running = _event_fixture(1)
    live_view = topic_views(running, running=True)[-1]
    check(live_view.status == "running" and not live_view.sealed, "跑着的尾部标进行中")
    check(live_view.index == 1, f"第一个主题的号是 1：{live_view.index}")
    check(
        (live_view.current or {}).get("phase") == "thinking",
        f"工具跑完之后当前动作是模型在写：{live_view.current}",
    )

    stopped = _event_fixture(1)
    stopped.pending_steps.append(
        Step(seq=1, intent="dock(x=1)", tools=["dock"], result="成功：docking#0")
    )
    stopped_view = topic_views(stopped, running=False)[-1]
    check(stopped_view.status == "unsealed", f"有步却没在跑：{stopped_view.status}")
    check(stopped_view.steps and stopped_view.tools == ["dock"], "未封存的主题也数得出工具")

    chat = Session(id="ses_chat_only")
    chat.add_message(Message(role="user")).add_part(Part(type=PartType.TEXT, text="你好"))
    chat.add_message(Message(role="assistant")).add_part(Part(type=PartType.TEXT, text="在"))
    chat_view = topic_views(chat, running=False)[-1]
    check(chat_view.status == "completed", f"纯聊天的一轮已经说完：{chat_view.status}")

    # 重启回来：pending_steps 空了，步按原文回填，工具数不能写成 0。
    restored = _event_fixture(1)
    restored_view = topic_views(restored, running=False)[-1]
    check(len(restored_view.steps) == 1, f"没有流水时按 Part 回填：{len(restored_view.steps)}")
    check(restored_view.tools == ["dock"], f"回填之后工具名还在：{restored_view.tools}")

    # 步时长：从本步的 STEP_START 到下一个 STEP_START。
    timed = Message(role="assistant")
    timed.add_part(Part(type=PartType.STEP_START, timestamp=100.0))
    timed.add_part(Part(type=PartType.TOOL, tool_name="bash", timestamp=100.0))
    timed.add_part(Part(type=PartType.STEP_START, timestamp=104.0))
    timed.add_part(Part(type=PartType.TOOL, tool_name="bash", timestamp=104.0))
    step = Step(seq=1, tools=["bash"], message_index=0, part_start=1, part_end=2)
    check(
        abs(step_seconds([timed], step) - 4.0) < 1e-6,
        f"步时长到下一步为止：{step_seconds([timed], step)}",
    )
    check(step_seconds([], step) == 0.0, "没有消息时不算时长")


async def check_event_log() -> None:
    """主题流水：每步自动记、封存不花请求、按位置让位、三级召回。"""
    session = _event_fixture(2)  # 4 条消息，2 次 dock（第 2 次失败）
    check(session.events == [] and session.pending_steps == [], "新会话没有主题也没有待封存的步")

    # 第一步：从 Part 折出来，零请求。
    assistant = session.messages[1]
    calls = [part for part in assistant.parts if part.type is PartType.TOOL]
    step = record_step(
        session,
        step_no=1,
        parts=calls,
        message_index=1,
        part_start=0,
        part_end=len(assistant.parts),
    )
    check(step is not None and step.seq == 1, "每步自动记一条 Step")
    check(
        "dock(" in step.intent and "ligandset#0" in step.intent,
        f"intent 从工具名与参数拼：{step.intent}",
    )
    check(
        step.result.startswith("成功") and "docking#0" in step.result,
        f"结果带产出：{step.result}",
    )
    check(step.artifacts == ["docking#0"], f"产物取自 tool_meta：{step.artifacts}")
    check(
        (step.part_start, step.part_end) == (0, 1),
        f"Part 区间记下来了（Step 级召回靠它）：{step.part_start}-{step.part_end}",
    )
    check(len(session.pending_steps) == 1, "待封存的步挂在会话上")
    check(open_tokens(session) > 0, "预算读得到这一段的大小")

    # 第二步是失败的那一条：失败要长在它自己那一步上。
    assistant = session.messages[3]
    calls = [part for part in assistant.parts if part.type is PartType.TOOL]
    failed = record_step(
        session, step_no=2, parts=calls, message_index=3, part_start=0, part_end=1
    )
    check(failed is not None and failed.issues and "结果1" in failed.issues[0], f"失败项取自 Part：{failed.issues}")
    check("失败：" in failed.result, f"结果里写明失败：{failed.result}")

    event = seal_topic(session, cut=(4, 0))
    check(event is not None and event.kind == "auto", "轮末自动封存成一条主题")
    check(event.degraded, "模型没写标题时标成降级")
    check(event.topic_index == 1 and len(event.steps) == 2, f"主题号与步数：{event.topic_index}/{len(event.steps)}")
    check(event.cut == (4, 0), f"切点记在事件上：{event.cut}")
    check(event.span == "第 1–2 步", f"步号区间：{event.span}")
    check(event.tokens > 0, "记下了原文估算")
    check("第 0 步：跑对接" in event.title, f"标题用用户原话：{event.title}")
    check("2 步" in event.summary and "docking#0" in event.summary, f"结论本地拼：{event.summary}")
    check(event.issues and "结果1" in event.issues[0], f"未决从 steps 聚合：{event.issues}")
    check(event.actions == ["dock"], f"动作从 steps 聚合：{event.actions}")
    check(session.pending_steps == [], "封存之后待封存的步清空")
    check(session.event_cut == (4, 0), "事件的切点就是下一段的起点")
    check(session.compact_upto == 0, "自动封存不推手动压缩位置")
    check(seal_topic(session, cut=(4, 0)) is None, "同一段再封一次是 no-op")
    check(topic_start(session) == (4, 0), "下一个主题从切点开始")

    # 纯聊天的一轮没有流水可记，轮末跳过；溢出 / compact 那种强行封存必须盖住。
    chat = Session(id="ses_chat")
    user = chat.add_message(Message(role="user"))
    user.add_part(Part(type=PartType.TEXT, text="你好"))
    chat.add_message(Message(role="assistant"))
    check(seal_range(chat, upto=2) is None, "没有流水的一轮不封主题")
    forced = seal_range(chat, kind="local", upto=2, force=True)
    check(
        forced is not None and forced.cut == (2, 0),
        "强行封存仍要盖住那一段，不能留缝",
    )
    check(
        forced.steps == [] and "覆盖第 1–2 条消息" in forced.summary,
        f"没有流水时结论不假装有步：{forced.summary}",
    )

    # 回填：Step 没记下来（进程重启、旧版本写的历史）时按 Part 现数。
    backfill = _event_fixture(2)
    restored_event = seal_range(backfill, upto=4)
    check(
        restored_event is not None and len(restored_event.steps) == 2,
        "没记过 Step 时按 Part 回填流水",
    )

    # 一条 assistant 消息里装着**一整轮**的多步（`STEP_START` 切开）：封存时不能压成一步。
    # 实测一次四轮会话，第二轮「先 load_tools 再 fetch_target」两步只留下了一步，
    # 判据「每个 Step 恰好被一个 Phase 收走一次」当场不成立。
    def _multi_step() -> Session:
        built = Session(id="ses_multi")
        built.add_message(Message(role="user")).add_part(
            Part(type=PartType.TEXT, text="装族再用")
        )
        message = built.add_message(Message(role="assistant"))
        for name in ("load_tools", "fetch_target"):
            message.add_part(Part(type=PartType.STEP_START))
            message.add_part(
                Part(
                    type=PartType.TOOL,
                    tool_name=name,
                    tool_status=ToolStatus.COMPLETED,
                    tool_input={},
                    tool_output="ok",
                )
            )
        return built

    folded = _multi_step()
    folded_event = seal_range(folded, upto=2)
    check(
        folded_event is not None and [step.tools for step in folded_event.steps]
        == [["load_tools"], ["fetch_target"]],
        f"回填按 STEP_START 分段，一步不丢：{[s.tools for s in folded_event.steps] if folded_event else None}",
    )

    recorded = _multi_step()
    for order, name in enumerate(("load_tools", "fetch_target"), 1):
        parts = recorded.messages[1].parts
        positions = [
            position
            for position, part in enumerate(parts)
            if part.type is PartType.TOOL and part.tool_name == name
        ]
        record_step(
            recorded,
            step_no=order,
            parts=[parts[position] for position in positions],
            message_index=1,
            part_start=min(positions),
            part_end=max(positions) + 1,
        )
    recorded_event = seal_range(recorded, upto=2)
    check(
        recorded_event is not None
        and [step.tools for step in recorded_event.steps]
        == [["load_tools"], ["fetch_target"]],
        f"当场记的那份与回填不重复也不丢：{[s.tools for s in recorded_event.steps] if recorded_event else None}",
    )

    # 软预算把一个 Phase 切成两段时，两边的步号都要从 1 开始。
    split = _event_fixture(4)
    for index in (1, 3, 5, 7):
        message = split.messages[index]
        record_step(
            split,
            step_no=len(split.pending_steps) + 1,
            parts=[part for part in message.parts if part.type is PartType.TOOL],
            message_index=index,
        )
    head = seal_range(split, upto=4)
    check(head is not None and [item.seq for item in head.steps] == [1, 2], "前半段的步号从 1 起")
    check(
        [item.seq for item in split.pending_steps] == [1, 2],
        f"剩下那一段的步号重新从 1 起：{[item.seq for item in split.pending_steps]}",
    )
    check([item.message_index for item in split.pending_steps] == [5, 7], "剩下的步没被前一段收走")

    # close_topic：记下标题、结论与交接，**封存由内核在步末做**。
    # 它自己不算一步，也不该把当前这一步切开。
    manual_session = _event_fixture(2)
    for index in (1, 3):
        message = manual_session.messages[index]
        record_step(
            manual_session,
            step_no=len(manual_session.pending_steps) + 1,
            parts=[part for part in message.parts if part.type is PartType.TOOL],
            message_index=index,
        )
    runtime = EventRuntime(session=manual_session)
    missing = await CloseTopicTool().execute({}, event_runtime=runtime)
    check(missing.failed and "缺少 title" in missing.output, f"没给标题就退回：{missing.output}")
    blank = await CloseTopicTool().execute({"title": "   "}, event_runtime=runtime)
    check(blank.failed and "缺少 title" in blank.output, f"空白标题也算缺：{blank.output}")
    recorded = await CloseTopicTool().execute(
        {
            "title": "跑完两轮对接",
            "summary": "两轮都产出了 docking",
            "handoff": "下一段用 docking#0 做排序",
        },
        event_runtime=runtime,
    )
    check(not recorded.failed and "已记下" in recorded.output, f"记下标题：{recorded.output}")
    check(manual_session.events == [], "记名字不当场封存，封存在步末")
    check(
        (
            manual_session.topic_title,
            manual_session.topic_summary,
            manual_session.topic_handoff,
        )
        == ("跑完两轮对接", "两轮都产出了 docking", "下一段用 docking#0 做排序"),
        "标题、结论与交接挂在会话上，等封存时取",
    )
    again = await CloseTopicTool().execute({"title": "再来一次"}, event_runtime=runtime)
    check(
        "已记下" in again.output and manual_session.topic_title == "再来一次",
        f"再调一次是覆盖，不是报错：{again.output}",
    )
    await CloseTopicTool().execute(
        {
            "title": "跑完两轮对接",
            "summary": "两轮都产出了 docking",
            "handoff": "下一段用 docking#0 做排序",
        },
        event_runtime=runtime,
    )
    manual = seal_range(manual_session, upto=4)
    check(manual is not None and not manual.degraded, "封存时取用模型给的那份，不标降级")
    check(
        manual.title == "跑完两轮对接" and manual.summary.startswith("两轮"),
        "标题与结论照模型给的那份",
    )
    check(manual.handoff.startswith("下一段"), f"交接也写进事件：{manual.handoff}")
    check(manual_session.topic_title == "", "用过一次就清掉，不会顶到下一个主题")
    check(len(manual.steps) == 2 and manual.actions == ["dock"], f"流水仍从 Part 数出来：{manual.actions}")
    closed_again = await CloseTopicTool().execute(
        {"title": "已经封过了"}, event_runtime=runtime
    )
    check(
        closed_again.failed and "还没有记下任何一步" in closed_again.output,
        f"封完之后没有新步，再调就拒绝：{closed_again.output}",
    )

    # 一步都没有时拒绝：空主题封出来只是一条占位，还会把下一个主题的起点往前挪。
    empty = _event_fixture(1)
    refused = await CloseTopicTool().execute(
        {"title": "空主题"}, event_runtime=EventRuntime(session=empty)
    )
    check(refused.failed and "还没有记下任何一步" in refused.output, f"空主题不许封：{refused.output}")

    # close_topic 是记账不是干活：它不进流水，单独调它也不产生一步。
    accounting = _event_fixture(1)
    close_part = Part(
        type=PartType.TOOL,
        tool_name="close_topic",
        tool_status=ToolStatus.COMPLETED,
        tool_input={"title": "定口袋"},
        tool_output="已记下主题",
    )
    check(
        record_step(accounting, step_no=1, parts=[close_part], message_index=1) is None,
        "单独调 close_topic 不产生流水",
    )
    mixed = record_step(
        accounting,
        step_no=1,
        parts=[accounting.messages[1].parts[0], close_part],
        message_index=1,
    )
    check(
        mixed is not None and mixed.tools == ["dock"],
        f"同一步里的记账调用不占流水：{mixed.tools if mixed else None}",
    )

    # 渲染三条规则：每步一行、失败留在流水里、结论与未决是主题级的。
    line = event_line(manual)
    check("主题 1" in line and "跑完两轮对接" in line, f"索引行带主题号与标题：{line}")
    block = ContextAssembler(128_000)._event_block([manual])
    check("  1. " in block and "  2. " in block, f"流水每步一行：{block}")
    check("结论：" in block, "结论是主题级的单独一行")
    check("交接：" in block, "交接单独一行")
    check("失败" in block, "失败留在流水里，不挪进单独的列表")

    # 超过 12 步只留「前 3 + 后 3」，中间用 step 召回。
    long_event = Event(
        id="evt_0099",
        title="很长的主题",
        summary="三十步",
        topic_index=9,
        steps=[Step(seq=index, intent=f"调用 {index}", result="成功") for index in range(1, 31)],
    )
    long_block = ContextAssembler(128_000)._event_block([long_event])
    check("中间省略 24 步" in long_block, f"超过 12 步只留首尾：{long_block}")
    check("  1. " in long_block and "  30. " in long_block, "首尾两步都在")
    check("  15. " not in long_block, "中间那些确实没铺开")
    check("recall_event evt_0099 step=" in long_block, "省略处写明怎么取回来")

    # event_step_detail：压成「一主题一行」，标题与结论还在。
    flat = ContextAssembler(128_000)._event_block([manual], detail=False)
    check("  1. " not in flat and "结论：" in flat, "压流水只丢过程，不丢结论")
    check(
        DEGRADE_ORDER.index("event_step_detail") < DEGRADE_ORDER.index("event_layer"),
        "压流水排在丢事件之前",
    )

    # 事件多到装不下时，被裁掉的那几条必须留一句提示。
    many = [
        Event(
            id=f"evt_{index:04d}",
            title=f"第 {index} 段",
            summary="做完了一段",
            cut_message=index,
            cut_part=0,
        )
        for index in range(1, 9)
    ]
    kept, capped = ContextAssembler(128_000)._cap_events(many, 60)
    check(len(kept) < len(many) and "更早还有" in capped, f"裁掉的事件要留提示：{capped}")

    # 三级召回：清单 / 一个主题 / 某一步的原文。
    listing = await RecallEventTool().execute({}, session=manual_session)
    check(manual.id in listing.output and "跑完两轮对接" in listing.output, "不带参数时列出主题")
    detail = await RecallEventTool().execute({"event": manual.id}, session=manual_session)
    check("流水：" in detail.output and "  1. " in detail.output, "详情里有流水")
    check("第 0 步：跑对接" in detail.output, "recall_event 取回原文")
    one = await RecallEventTool().execute(
        {"event": manual.id, "step": 1}, session=manual_session
    )
    check(
        "第 1 步" in one.output and "结果0" in one.output,
        f"Step 级召回取到那一次工具输出：{one.output}",
    )
    # 单步召回也要分页：一次 bash 的输出可以很长，参数说明里承诺的是「原文按
    # offset / limit 分页」，只对整段生效而漏掉单步的话，模型按说明传 limit 也拦不住。
    long_session = Session(id="ses_long_step")
    long_session.add_message(Message(role="user")).add_part(
        Part(type=PartType.TEXT, text="跑一条长输出")
    )
    long_message = long_session.add_message(Message(role="assistant"))
    long_message.add_part(Part(type=PartType.STEP_START))
    long_message.add_part(
        Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"command": "seq 1 300"},
            tool_output="\n".join(str(index) for index in range(1, 301)),
        )
    )
    long_message.add_part(Part(type=PartType.STEP_FINISH))
    long_event = seal_range(long_session, upto=2)
    assert long_event is not None
    page = await RecallEventTool().execute(
        {"event": long_event.id, "step": 1, "limit": 5}, session=long_session
    )
    check(
        page.output.count("\n") <= 9 and "用 offset=5 继续" in page.output,
        f"单步召回按 limit 分页：{page.output[-80:]!r}",
    )
    tail_page = await RecallEventTool().execute(
        {"event": long_event.id, "step": 1, "offset": 298, "limit": 5}, session=long_session
    )
    check("300" in tail_page.output and "继续" not in tail_page.output, "翻到最后一页时不再提示继续")

    missing_step = await RecallEventTool().execute(
        {"event": manual.id, "step": 99}, session=manual_session
    )
    check("没有第 99 步" in missing_step.output, f"步号不存在时说清楚：{missing_step.output}")
    unknown = await RecallEventTool().execute({"event": "evt_9999"}, session=manual_session)
    check(unknown.failed and manual.id in unknown.output, f"未知编号时列出可用编号：{unknown.output}")

    # 边界一：没有主题的会话。回一句话，不抛。
    bare = Session(id="ses_bare")
    nothing = await RecallEventTool().execute({}, session=bare)
    check(
        not nothing.failed and "还没有主题记录" in nothing.output,
        f"没有主题时给一句话：{nothing.output}",
    )

    # 边界二：参数不合法。四个参数各验一次，都是 failed 而不是异常。
    for bad, label in (
        ({"event": manual.id, "step": -1}, "step 为负"),
        ({"event": manual.id, "step": True}, "step 是布尔"),
        ({"event": manual.id, "offset": -5}, "offset 为负"),
        ({"event": manual.id, "limit": 0}, "limit 为零"),
    ):
        answer = await RecallEventTool().execute(bad, session=manual_session)
        check(
            answer.failed and "整数" in answer.output,
            f"{label}时要退回：{answer.output}",
        )

    # 边界三：翻过原文末尾。不报错，也不谎称还有更多。
    beyond = await RecallEventTool().execute(
        {"event": manual.id, "offset": 9999, "limit": 10}, session=manual_session
    )
    check(
        not beyond.failed and "继续" not in beyond.output,
        f"offset 超过原文长度时不报错也不提示继续：{beyond.output[-40:]!r}",
    )

    # 边界四：返回行数夹到 400。说多少都不多给，否则一次召回能把窗口吃光。
    big = Session(id="ses_big")
    big.add_message(Message(role="user")).add_part(Part(type=PartType.TEXT, text="长输出"))
    big_message = big.add_message(Message(role="assistant"))
    big_message.add_part(Part(type=PartType.STEP_START))
    big_message.add_part(
        Part(
            type=PartType.TOOL,
            tool_name="bash",
            tool_status=ToolStatus.COMPLETED,
            tool_input={"command": "seq 1 900"},
            tool_output="\n".join(str(index) for index in range(1, 901)),
        )
    )
    big_event = seal_range(big, upto=2)
    assert big_event is not None
    capped = await RecallEventTool().execute(
        {"event": big_event.id, "step": 1, "limit": 100_000}, session=big
    )
    check(
        "用 offset=400 继续" in capped.output,
        f"单次最多 400 行，超出的用 offset 接着取：{capped.output[-50:]!r}",
    )

    # 边界五：空主题（强行封存出来的那一条）。流水是空的，结论仍在。
    empty_detail = await RecallEventTool().execute({"event": forced.id}, session=chat)
    check(
        "（没有流水）" in empty_detail.output and "结论：" in empty_detail.output,
        f"空主题的详情写明没有流水：{empty_detail.output[:120]}",
    )
    empty_step = await RecallEventTool().execute(
        {"event": forced.id, "step": 1}, session=chat
    )
    check(
        "没有第 1 步" in empty_step.output and "没有流水" in empty_step.output,
        f"空主题里按步查也说清楚：{empty_step.output}",
    )

    # 边界六：步还在，它指向的那条消息已经不在了。说清是哪一种取不到，
    # 别让人以为「这一步没内容」。
    orphan = Session(id="ses_orphan")
    orphan.add_message(Message(role="user")).add_part(Part(type=PartType.TEXT, text="开头"))
    orphan.add_event(
        Event(
            id="evt_0001",
            title="孤儿",
            steps=[Step(seq=1, intent="bash", result="ok", message_index=9, part_start=0, part_end=1)],
            cut_message=1,
            cut_part=0,
        )
    )
    orphan.sanitize_events()
    lost = await RecallEventTool().execute({"event": "evt_0001", "step": 1}, session=orphan)
    check(
        "已经不在会话里" in lost.output,
        f"原文那条消息不在了时说清楚：{lost.output}",
    )

    # 装配：主题封存之后，那一段原文不再发出去，只剩摘要。
    two = _event_fixture(4)
    first = seal_range(two, upto=4)
    second = seal_range(two, upto=8)
    check(first is not None and second is not None, "两段各封成一个主题")
    built = ContextAssembler(128_000).build(two, "系统提示。" * 20)
    body = "\n".join(str(item.get("content") or "") for item in built.messages)
    check(first.title in body, "封过的那一段以流水形式进上下文")
    check("  1. " in body, "流水进了上下文")
    tool_items = [item for item in built.messages if item.get("role") == "tool"]
    check(tool_items == [], f"封存之后不再发工具原文：{len(tool_items)} 条")
    check("主题 2：" in body, "两条主题都在事件块里")
    check(built.usage[Layer.EVENT] > 0, "事件层有用量")
    check(built.dropped == 0, f"被主题覆盖的组不算「本次移出」：{built.dropped}")
    check(built.metrics["events"] == 2, "度量里数得到已归档的主题")

    # 写盘与读回：流水跟着状态走，派生字段也还原得出来。
    restored = Session.from_dict(manual_session.to_dict())
    check(
        len(restored.events) == 1 and len(restored.events[0].steps) == 2,
        "主题与流水随状态写盘、读回来还在",
    )
    check(
        restored.events[0].steps[0].intent == manual.steps[0].intent,
        "步的意图逐字读回来",
    )
    check(restored.events[0].actions == ["dock"], "派生字段读回来也还在")
    check(restored.events[0].cut == (4, 0), "读回来之后切点仍在")
    check(restored.event_cut == (4, 0), "会话上的起点也从事件算得出来")

    # 旧形状（没有 steps）的事件也要读得回来：升级不该让已有会话丢历史。
    legacy = Event.from_dict(
        {
            "id": "evt_0001",
            "title": "旧格式",
            "summary": "旧的",
            "actions": ["dock"],
            "artifacts": ["docking#1"],
            "issues": ["炸了"],
            "start": 0,
            "end": 2,
        }
    )
    check(
        len(legacy.steps) == 1 and legacy.steps[0].tools == ["dock"],
        "旧形状的事件折成一步，内容不丢",
    )
    check(
        legacy.artifacts == ["docking#1"] and legacy.issues == ["炸了"],
        "旧字段仍读得出来",
    )
    check(legacy.cut == (2, 0), f"旧的消息区间折成切点：{legacy.cut}")

    # 越界与断点：这两类都必须在进装配之前裁掉。
    broken = _event_fixture(2)
    broken.events = [Event(id="evt_0001", cut_message=99, cut_part=0, kind="auto")]
    broken.sanitize_events()
    check(broken.events == [], "越过末尾的事件被丢掉")
    gap = _event_fixture(2)
    gap.events = [
        Event(id="evt_0001", cut_message=2, cut_part=0),
        Event(id="evt_0002", cut_message=2, cut_part=0),
    ]
    gap.sanitize_events()
    check(len(gap.events) == 1, "切点不递增的事件被丢掉，前面那段留着")

    tail = Session(id="ses_tail")
    tail.add_message(Message(role="assistant"))
    tail.add_message(Message(role="user"))
    check(event_boundary(tail, 2) == 1, "紧邻的用户消息不进事件")
    check(event_boundary(_event_fixture(2), 4) == 4, "正常情况收到当前位置")
    check(render_range(tail, 0, 2).count("\n") >= 0, "render_range 可用（read_turn 与 recall 共用）")
    check(TOPIC_SOFT_LIMIT == 120_000, "预算按方案定的 120K")


async def check_topic(server: FakeModelServer) -> None:
    """一轮里多个主题：切点按步算，封一段就让一段原文离开上下文。

    这是「事件按位置切」存在的全部理由：一条 assistant 消息装着一整轮的所有
    往返，按消息切不出轮中途的边界。
    """
    session = Session(id="ses_topic", title="多主题")
    session.add_message(
        Message(role="user", parts=[Part(type=PartType.TEXT, text="先看结构再查配体")])
    )
    message = session.add_message(Message(role="assistant"))
    for name in ("fetch_structure", "detect_pockets", "search_chembl"):
        message.add_part(Part(type=PartType.STEP_START))
        message.add_part(
            Part(
                type=PartType.TOOL,
                tool_name=name,
                tool_status=ToolStatus.COMPLETED,
                tool_input={},
                tool_output=f"{name} 的结果",
                tool_meta={"handle": f"{name}#1"},
            )
        )

    # 每一段都是一个独立的交互组，位置记在组上：切点用的就是它。
    turns = split_turns(session.messages)
    check(
        [(turn.message_index, turn.part_start) for turn in turns]
        == [(0, 0), (1, 0), (1, 2), (1, 4)],
        f"三段各自成一个交互组，位置分开记：{[(t.message_index, t.part_start) for t in turns]}",
    )

    for index, name in ((1, "fetch_structure"), (3, "detect_pockets")):
        record_step(
            session,
            step_no=len(session.pending_steps) + 1,
            parts=[part for part in message.parts if part.tool_name == name],
            message_index=1,
            part_start=index,
            part_end=index + 1,
        )

    first = seal_topic(session, cut=(1, 4), kind="manual", title="结构分析")
    check(first is not None and len(first.steps) == 2, "前两步封成一个主题")
    check(first.cut == (1, 4), f"切点落在第三段之前：{first.cut}")
    check(first.step_start == 1 and first.step_end == 3, f"全局步号：{first.span}")
    check(session.pending_steps == [], "封过的步不再挂在待封存上")

    # 第三个主题继续在同一个消息里跑。
    record_step(
        session,
        step_no=1,
        parts=[part for part in message.parts if part.tool_name == "search_chembl"],
        message_index=1,
        part_start=5,
        part_end=6,
    )
    check(
        [step.seq for step in session.pending_steps] == [1],
        "新主题的步号从 1 重新起",
    )

    built = ContextAssembler(128_000).build(session, "系统提示。")
    tool_items = [item for item in built.messages if item.get("role") == "tool"]
    check(
        len(tool_items) == 1 and "search_chembl" in str(tool_items[0].get("content")),
        f"只有当前主题的工具原文发出去：{[str(i.get('content'))[:20] for i in tool_items]}",
    )
    assistants = [item for item in built.messages if item.get("role") == "assistant"]
    check(len(assistants) == 1, f"封过的那两段 assistant 项也不再发：{len(assistants)} 条")
    body = "\n".join(str(item.get("content") or "") for item in built.messages)
    check("结构分析" in body, "封过的主题以摘要进上下文")
    check("主题 1：" in body, f"索引行写的是主题：{body[:120]}")

    # 后封的主题不回头改前一条。
    snapshot = [step.intent for step in session.events[0].steps]
    second = seal_range(session, kind="auto", upto=2)
    check(second is not None and second.cut == (2, 0), "轮末把剩下的封成一个主题")
    check(second.step_start == 3 and second.step_end == 4, f"步号接着往下排：{second.span}")
    check(
        [step.intent for step in session.events[0].steps] == snapshot,
        "前面那条主题的流水一个字没动",
    )
    check(len(session.events) == 2 and session.event_cut == (2, 0), "两条主题首尾相接")

    # 召回一条主题只铺它自己那一段原文。
    detail = render_event_detail(session, session.events[0])
    check("fetch_structure" in detail and "search_chembl" not in detail, "详情只铺本主题的原文")
    second_detail = render_event_detail(session, session.events[1])
    check("search_chembl" in second_detail, "第二个主题的详情铺它自己的原文")
    check("第 1–2 步" in detail, f"详情里写着步号区间：{detail.splitlines()[1]}")

    # 封存之后再装配：一条原文都不剩，只剩两条摘要。
    built = ContextAssembler(128_000).build(session, "系统提示。")
    check(
        [item for item in built.messages if item.get("role") == "tool"] == [],
        "全部封完之后没有工具原文再发出去",
    )
    check(built.metrics["events"] == 2, "度量里数得到两条主题")

    # 真跑一轮：先调一个工具，再调 close_topic，然后收尾。
    # 这一条验的是接线：工具结果里那段原文，在下一步请求里就没了。
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.subscribe("event.sealed", seen.append)
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(CloseTopicTool())
    loop = build_loop(server, bus, registry)
    live = Session(id="ses_topic_loop", title="多主题")
    ask(live, "查一条记录")
    marker = "RAW_ECHO_口袋在残基45"
    # 标记放在 120 字之后：流水只留结果首行的一百来字，标记落在截断线外的
    # 那一部分，出现就只可能来自工具原文。
    payload = "填充" * 80 + marker
    server.clear()
    server.script(
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c1",
                            name="echo",
                            arguments=json.dumps({"text": payload}),
                        )
                    ]
                ),
                usage_chunk(3, 3),
            ]
        ),
        Reply(
            chunks=[
                delta_chunk(
                    tool_calls=[
                        tool_call_delta(
                            0,
                            call_id="c2",
                            name="close_topic",
                            arguments=json.dumps(
                                {
                                    "title": "查口袋",
                                    "summary": "口袋在残基 45",
                                    "handoff": "下一步用它做盒子",
                                }
                            ),
                        )
                    ]
                ),
                usage_chunk(3, 3),
            ]
        ),
        Reply(chunks=[*text_chunks("记下了", size=2), usage_chunk(1, 1)]),
    )
    await loop.stream_reply(live, "")
    check(len(server.requests) == 3, f"三步各一次请求：{len(server.requests)}")
    sealed = next((event for event in live.events if event.title == "查口袋"), None)
    check(sealed is not None and sealed.kind == "manual", "模型给的名字封成一条主题")
    check(sealed.actions == ["echo"], f"流水里是那一步的工具：{sealed.actions if sealed else None}")
    check(sealed.handoff.startswith("下一步"), f"交接跟着写进去：{sealed.handoff}")
    final_body = json.dumps(server.requests[-1]["messages"], ensure_ascii=False)
    check(marker not in final_body, "封存之后，那一步的工具原文不再随请求发出")
    check("查口袋" in final_body, "摘要顶上了原文的位置")
    recalled = await RecallEventTool().execute(
        {"event": sealed.id, "step": 1}, session=live
    )
    check(marker in recalled.output, "原文一条没删，按步召得回来")
    check(seen and seen[0]["title"] == "查口袋", f"封存时给界面发一条总线事件：{seen}")


async def check_event_capture(server: FakeModelServer) -> None:
    """主题封存：标题随调用给出，没给结论就本地兜底，全程不另发请求。"""
    bus = EventBus()
    loop = build_loop(server, bus, ToolRegistry())
    session = _event_fixture(2)
    session.add_event(
        Event(
            id="evt_0001",
            title="前一段",
            summary="前一段做完了",
            topic_index=1,
            cut_message=2,
            cut_part=0,
            kind="manual",
        )
    )
    session.event_seq = 1
    session.sanitize_events()

    # 当前这一段的流水：第 3 条消息上那一次 dock。
    assistant = session.messages[3]
    record_step(
        session,
        step_no=1,
        parts=[part for part in assistant.parts if part.type is PartType.TOOL],
        message_index=3,
    )

    server.clear()
    runtime = loop._event_runtime(session)
    missing = await CloseTopicTool().execute({}, event_runtime=runtime)
    check(missing.failed and "缺少 title" in missing.output, f"没给标题就退回：{missing.output}")
    blank = await CloseTopicTool().execute({"title": "   "}, event_runtime=runtime)
    check(blank.failed and "缺少 title" in blank.output, f"空白标题也算缺：{blank.output}")
    check(len(server.requests) == 0, f"退回时不发任何请求：{len(server.requests)}")
    check(not session.events[1:], "退回时不落半条事件")

    recorded = await CloseTopicTool().execute({"title": "准备受体"}, event_runtime=runtime)
    check(not recorded.failed, f"只给标题也能用：{recorded.output}")
    check(len(server.requests) == 0, "记名字不花请求")
    check(session.events[-1].id == "evt_0001", "记名字不当场封存（封存在步末）")
    check(session.topic_title == "准备受体", "名字挂在会话上，等封存时取")

    # 轮末封存：取用模型给的那一份，用完就清。
    ended = loop._seal_range(session, kind="auto", upto=4)
    check(ended is not None, "轮末照常封存")
    newest = session.events[-1]
    check(newest.title == "准备受体" and newest.summary, "封存用模型写的标题，结论本地拼一份")
    check(not newest.degraded, "标题是模型写的不算降级")
    check(newest.actions == ["dock"], f"动作仍从 Part 数出来：{newest.actions}")
    check("docking#1" in newest.artifacts, f"产物仍从 tool_meta 取：{newest.artifacts}")
    check(newest.cut == (4, 0), f"封到这一段的末尾：{newest.cut}")
    check(session.events[-2].id == "evt_0001", "前面那条事件没被动过")
    check(session.events[-1].topic_index == 2, "主题号接着往下排")
    check(session.topic_title == "", "名字用过一次就清掉，不会顶到下一个主题")

    # 已有事件时溢出：补一个主题，不额外请求模型。
    crowded = _event_fixture(3)
    crowded.add_event(
        Event(id="evt_0001", title="开头", summary="开头", cut_message=2, cut_part=0, kind="auto")
    )
    for index in range(3, 25):
        user = crowded.add_message(Message(role="user"))
        user.add_part(Part(type=PartType.TEXT, text=f"第 {index} 步：" + "内容" * 100))
        assistant = crowded.add_message(Message(role="assistant"))
        assistant.add_part(Part(type=PartType.TEXT, text="好" + "说明" * 100))
    server.clear()
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(3, 3)]))
    loop.context_window = 20_000
    answer = await loop.stream_reply(crowded, "")
    loop.context_window = 200_000
    check(answer is not None, "小窗口下这一轮照常结束")
    check(len(server.requests) == 1, f"溢出走本地主题，不多发请求：{len(server.requests)}")
    check(crowded.events[-1].kind in ("auto", "local"), f"补上的是本地主题：{crowded.events[-1].kind}")
    check(crowded.event_cut > (2, 0), "切点往前推了")

    # 已经有一份摘要、还没有事件时：新的溢出也交给主题（摘要从此冻结）。
    # 否则事件层永远等不到第一条，长会话只会一路摘要下去。
    switched = _event_fixture(3)
    switched.summary = "一、目标\n早前做过的事"
    switched.summary_upto = 2
    switched.compact_upto = 2
    for index in range(3, 25):
        user = switched.add_message(Message(role="user"))
        user.add_part(Part(type=PartType.TEXT, text=f"第 {index} 步：" + "内容" * 100))
        assistant = switched.add_message(Message(role="assistant"))
        assistant.add_part(Part(type=PartType.TEXT, text="好" + "说明" * 100))
    server.clear()
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(3, 3)]))
    loop.context_window = 20_000
    await loop.stream_reply(switched, "")
    loop.context_window = 200_000
    check(len(server.requests) == 1, f"有摘要之后不再请求摘要：{len(server.requests)}")
    check(bool(switched.events), "补上了第一条事件")
    check(switched.events[0].kind == "auto", "第一条是本地兜底事件")
    check(switched.events[0].cut[0] >= 2, f"事件从摘要覆盖到的位置之后起：{switched.events[0].cut}")
    check(switched.summary_upto == 2, "摘要冻结在原来的位置")


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
    (root / "ligands.sdf").write_text("CCO\n", encoding="utf-8")
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
        # 分子画像的入口现在在 lookup_compound（合并之后没有 describe_molecule 了）
        ("lookup_compound", {"compound": "CHEMBL25"}),
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


async def check_subagent() -> None:
    """子 agent：fork 复制、深度守卫、共享登记表、重启恢复、事件带会话号。"""
    # --- fork：区间、ID 全新、过程标记不进子会话 ---
    parent = Session(id="ses_parent", title="父会话")
    for index in range(3):
        message = Message(role="user", session_id=parent.id)
        message.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条"))
        parent.add_message(message)
    assistant = Message(role="assistant", session_id=parent.id)
    assistant.add_part(Part(type=PartType.STEP_START, text="1"))
    assistant.add_part(Part(type=PartType.TEXT, text="答"))
    parent.add_message(assistant)

    child = Session(
        id="ses_child", title="子", parent_id=parent.id, root_id=parent.id, depth=1
    )
    parent_ids = {message.id for message in parent.messages}
    copied, dropped = fork_messages(parent, child, 1, 4, 10**9)
    check(copied == 3 and dropped == 0, f"fork 复制区间内全部消息：{copied}/{dropped}")
    check(
        all(message.id not in parent_ids for message in child.messages),
        "fork 出来的消息 ID 全部是新的",
    )
    check(
        child.messages[0].parts[0].text == "第 1 条",
        "fork 从起点开始，不是从 0 开始",
    )
    check(
        all(
            part.type is not PartType.STEP_START
            for message in child.messages
            for part in message.parts
        ),
        "过程标记不复制进子会话",
    )
    check(
        (child.seed_start, child.seed_end) == (1, 4),
        f"fork 记下区间：{child.seed_start}–{child.seed_end}",
    )

    # --- fork：预算不够时从最早的消息开始丢 ---
    tiny = Session(
        id="ses_tiny", title="子2", parent_id=parent.id, root_id=parent.id, depth=1
    )
    _, dropped_tiny = fork_messages(parent, tiny, 0, 4, 1)
    check(dropped_tiny > 0, "预算不够时真的丢了消息")
    check(
        len(tiny.messages) < 4,
        f"丢完之后子会话更短：{len(tiny.messages)}",
    )
    notice = task_preamble(parent, 0, 4, dropped_tiny)
    check("不能再创建" in notice, "任务书里写明不能再派发")
    check("已作为上文复制过来" in notice, f"任务书说明历史是否复制：{notice}")

    # fresh（一条历史都不复制）时把工作区索引交过去：子会话没有别的途径知道
    # 父会话手上有哪些 handle，而它要接着用那些东西。
    fresh = task_preamble(parent, 4, 4, 0, "（工作区索引）\n当前受体：receptor#1 · 4HJO")
    check("没有复制父会话的历史" in fresh, "fresh 的任务书明说没有上文")
    check("receptor#1" in fresh, "fresh 的任务书带着父会话的 handle")
    check("handle 可以直接用" in fresh, "并说明这些 handle 是能直接用的")
    forked = task_preamble(parent, 0, 4, 0, "（工作区索引）\n当前受体：receptor#1 · 4HJO")
    check(
        "receptor#1" not in forked,
        "fork 过来的不重复列 handle（历史里已经见过）",
    )

    # 报告预算：八个子 agent 各 800 字最坏约 3K token，等于把结论抄四份进父会话。
    short = "■「甲」（ses_1）　完成\n  结论：做完了"
    check(_cap_report(short) == short, "没超预算的报告原样返回")
    heavy = "\n".join(
        f"■「子{index}」（ses_{index}）　完成 · 12 条 · 900 tok\n  结论："
        + "结论内容" * 200
        for index in range(8)
    )
    capped = _cap_report(heavy)
    check(
        estimate_tokens(capped) <= REPORT_TOKENS,
        f"整份报告不超过 {REPORT_TOKENS} token：{estimate_tokens(capped)}",
    )
    check("报告已截断" in capped, "截断了要写明去哪看全文")
    check(
        all(line in heavy for line in capped.splitlines() if line and "报告已截断" not in line),
        "按行丢，不切在半行上",
    )
    check("子0" in capped, "留下的是靠前的那些（按时间顺序读）")

    # --- 派发工具：根会话能派，子 agent 不能 ---
    class _Spawner:
        def __init__(self) -> None:
            self.calls: list[tuple[Session, dict[str, Any]]] = []

        def spawn(self, session: Session, **kwargs: Any) -> Session:
            self.calls.append((session, kwargs))
            return Session(
                id="ses_spawned",
                title=str(kwargs["title"]),
                parent_id=session.id,
                root_id=session.family_id,
                depth=1,
            )

    spawner = _Spawner()
    tool = SpawnAgentTool()
    root = Session(id="ses_root", title="根")
    result = await tool.execute(
        {"task": "查一下", "title": "调研", "history": "none"},
        session=root,
        spawner=spawner,
    )
    check(not result.failed, f"根会话派发成功：{result.output}")
    check(
        spawner.calls and spawner.calls[0][1]["origin"] == "agent",
        "工具派发的来源记成 agent",
    )

    # history=recent 只复制最近一个用户轮
    for role in ("user", "assistant", "user", "assistant"):
        sample = Message(role=role, session_id=root.id)
        sample.add_part(Part(type=PartType.TEXT, text=role))
        root.add_message(sample)
    check(recent_start(root.messages, 1) == 2, "recent_start 指向最后一个用户轮")
    await tool.execute(
        {"task": "接着查", "title": "最近", "history": "recent", "recent_turns": 1},
        session=root,
        spawner=spawner,
    )
    latest = spawner.calls[-1][1]
    check(
        latest["start"] == 2 and latest["end"] == 4,
        f"recent 派发区间只覆盖最后一轮：{latest['start']}–{latest['end']}",
    )

    blocked = ""
    try:
        await tool.execute({"task": "再派一次"}, session=child, spawner=spawner)
    except ToolError as error:
        blocked = str(error)
    check("子 agent 内不能再创建" in blocked, f"子 agent 派发被拦：{blocked!r}")

    # --- 模型正在写的那条助手消息不该被复制 ---
    in_flight = Message(role="assistant", session_id=parent.id)
    in_flight.add_part(
        Part(
            type=PartType.TOOL,
            tool_name="spawn_agent",
            tool_status=ToolStatus.PENDING,
        )
    )
    parent.add_message(in_flight)
    check(
        history_end(parent.messages) == len(parent.messages) - 1,
        "可复制历史排除了正在写的那条派发消息",
    )

    # --- 应用装配：根有 spawn_agent，子没有；登记表共享；重启复位 ---
    state = ROOT / ".probe" / "state_subagent.toml"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.unlink(missing_ok=True)
    app = VSAgentApp(root=ROOT, state_path=state)
    root_session = app.session_manager.new_session()
    check(
        "spawn_agent" in app.agent.registry.names(),
        "根会话的注册表里有 spawn_agent",
    )
    sub = app.host.spawn(
        root_session, title="子任务", task="任务", start=0, end=0, prompt=False
    )
    check(
        "spawn_agent" not in app.host.loop_for(sub.id).registry.names(),
        "子 agent 的注册表里没有 spawn_agent",
    )
    check(
        "agent_status" in app.agent.registry.names(),
        "根会话的注册表里有 agent_status",
    )
    check(
        "agent_status" not in app.host.loop_for(sub.id).registry.names(),
        "子 agent 不能查询别的 agent",
    )

    # --- 父会话能取回子 agent 的报告 ---
    status_tool = AgentStatusTool()
    report = await status_tool.execute({}, session=root_session, spawner=app.host)
    check("子任务" in report.output, f"报告里有子 agent 的名字：{report.output}")
    check("空闲" in report.output, f"报告里带状态：{report.output}")
    blocked_report = ""
    try:
        await status_tool.execute({}, session=sub, spawner=app.host)
    except ToolError as error:
        blocked_report = str(error)
    check("不能查询其他 agent" in blocked_report, f"子 agent 查询被拦：{blocked_report!r}")

    produced = Message(role="assistant", session_id=sub.id)
    produced.add_part(Part(type=PartType.FILE, file_path="runs/x/table.csv"))
    sub.add_message(produced)
    report_with_files = await status_tool.execute(
        {"session_id": sub.id}, session=root_session, spawner=app.host
    )
    check(
        "table.csv" in report_with_files.output,
        f"报告里带产出文件：{report_with_files.output}",
    )

    check(
        app.host.ledger_for(root_session) is app.host.ledger_for(sub),
        "父子共用同一个登记表对象",
    )
    family = root_session.family_id
    app.host._set_status(sub, "running")
    app.save_state()
    reloaded = SessionManager(state)
    reloaded.load_from_toml()
    saved = reloaded.get_session(sub.id)
    check(saved is not None, "子 agent 写进状态文件后读得回来")
    check(
        saved is not None and saved.parent_id == root_session.id and saved.depth == 1,
        "父会话与深度不丢",
    )
    check(
        saved is not None and saved.status == "idle",
        f"重启后 running 复位成 idle：{saved.status if saved else '缺'}",
    )
    check(
        saved is not None and "仍在运行" in saved.status_note,
        "复位时写明原因",
    )

    # --- 删会话要连运行时一起清 ---
    check(sub.id in app.host._loops, "删除之前子 agent 的循环层是缓存着的")
    check(app.delete_session(root_session.id), "父会话删除成功")
    check(app.session_manager.get_session(sub.id) is None, "子 agent 的数据一起删掉")
    check(sub.id not in app.host._loops, "子 agent 的循环层跟着清掉")
    check(family not in app.host._ledgers, "家族的共享登记表跟着清掉")
    state.unlink(missing_ok=True)

    # --- 事件带 session_id：父屏据此丢弃别的会话的流 ---
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.subscribe("part.add", lambda data: seen.append(data))
    config = ProviderConfig(base_url="http://127.0.0.1:9", api_key="x", model="m")
    loop = AgentLoop(bus, OpenAICompatProvider(config), ToolRegistry(), config)
    message = Message(role="assistant", session_id="ses_evt")
    loop._publish(message, Part(type=PartType.TEXT, text="x"))
    check(
        seen and seen[-1].get("session_id") == "ses_evt",
        f"part.add 带上会话号：{seen[-1] if seen else '没有事件'}",
    )

    # --- 两个会话真的同时在跑；同会话第二次发问被拒；取消落 cancelled ---
    class _GatedLoop:
        """卡在一个事件上的假循环层，用来验调度而不打模型。"""

        context_window = 128_000
        registry = ToolRegistry()
        last_exit_reason = None

        def __init__(self, gate: asyncio.Event) -> None:
            self.gate = gate
            self.cancelled = False

        async def stream_reply(self, session: Session, prompt: str) -> None:
            await self.gate.wait()

        def cancel(self) -> None:
            self.cancelled = True

    host_state = ROOT / ".probe" / "state_subagent_host.toml"
    host_state.unlink(missing_ok=True)
    manager = SessionManager(host_state)
    first = manager.new_session("并发 A")
    second = manager.create_session("并发 B")
    gates = {first.id: asyncio.Event(), second.id: asyncio.Event()}
    host = AgentHost(
        manager,
        lambda session, ledger: _GatedLoop(gates[session.id]),
        EventBus(),
        ROOT,
    )
    check(host.prompt(first.id, "一"), "第一个会话开始跑")
    check(host.prompt(second.id, "二"), "第二个会话同时开始跑")
    check(
        not host.prompt(first.id, "再来"),
        "同一个会话在跑时第二次发问被拒",
    )
    check(
        host.is_running(first.id) and host.is_running(second.id),
        "两个会话同时处于运行中",
    )
    check(
        first.status == "running" and second.status == "running",
        f"状态都落成 running：{first.status}/{second.status}",
    )
    gates[first.id].set()
    gates[second.id].set()
    await host.wait([first.id, second.id], 2)
    check(
        not host.is_running(first.id) and not host.is_running(second.id),
        "两个会话都跑完了",
    )
    check(
        first.status == "done" and second.status == "done",
        f"正常结束落 done：{first.status}/{second.status}",
    )

    third = manager.create_session("并发 C")
    gates[third.id] = asyncio.Event()
    check(host.prompt(third.id, "三"), "第三个会话开始跑")
    # 让任务真正开始等门，再中断：模拟用户在一轮中途按 esc，
    # 而不是在 create_task 还没被调度时就取消。
    await asyncio.sleep(0)
    host.cancel(third.id)
    await host.wait([third.id], 2)
    check(third.status == "cancelled", f"中断落 cancelled：{third.status}")

    # agent_status 的 wait：超时先返回运行中，跑完之后再查是完成
    wait_parent = manager.create_session("等待父")
    wait_child = manager.create_session(
        "等待子",
        parent_id=wait_parent.id,
        root_id=wait_parent.id,
        depth=1,
    )
    gates[wait_child.id] = asyncio.Event()
    check(host.prompt(wait_child.id, "慢慢跑"), "等待用的子 agent 开始跑")
    wait_tool = AgentStatusTool()
    timed = await wait_tool.execute(
        {"session_id": wait_child.id, "wait": True, "timeout": 1},
        session=wait_parent,
        spawner=host,
    )
    check("运行中" in timed.output, f"超时之后如实报运行中：{timed.output}")
    gates[wait_child.id].set()
    await host.wait([wait_child.id], 2)
    finished = await wait_tool.execute(
        {"session_id": wait_child.id}, session=wait_parent, spawner=host
    )
    check("完成" in finished.output, f"跑完之后报完成：{finished.output}")

    host.shutdown()
    host_state.unlink(missing_ok=True)


async def check_subagent_ui() -> None:
    """从界面上派发、看见右栏、进入子 agent、返回。"""
    root = ROOT / ".probe" / "subagent_ui"
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state.toml"
    state.unlink(missing_ok=True)
    # 只为了让「派发」这一个动作被放行；子 agent 真的去请求时连不上，
    # 这一组验的是界面与状态，不是模型链路。
    save_connection(
        connection_path_for(state),
        Connection("http://127.0.0.1:1/v1", "k", "subagent-ui"),
    )
    app = VSAgentApp(root=ROOT, state_path=state)
    async with app.run_test(size=(120, 40)) as pilot:
        app.switch_screen(ChatScreen(ROOT))
        await pilot.pause()
        chat = app.screen
        check(isinstance(chat, ChatScreen), "先进入聊天界面")

        app.execute_command("agent")
        await pilot.pause()
        modal = app.screen
        check(isinstance(modal, SpawnAgentScreen), f"打 /agent 弹出派发模态：{modal}")

        modal.query_one("#spawn-name", Input).value = "调研"
        modal.query_one("#spawn-task", TextArea).text = "查一下 4HJO"
        modal.action_submit()
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "提交之后模态关掉，回到聊天界面")

        session = app.session_manager.current
        children = app.host.children_of(session.id)
        check(len(children) == 1, f"父会话下多了一个子 agent：{len(children)}")
        child = children[0]
        check(
            child.parent_id == session.id and child.depth == 1,
            "子 agent 记着父会话与深度",
        )

        panel = chat.query_one(SidePanel)
        check(panel.has_class("-visible"), "创建之后右栏自动出现")
        await pilot.pause()
        check(panel.region.width == 30, f"右栏保持 30 列：{panel.region.width}")
        check(
            panel.region.right == app.size.width,
            "右栏贴在屏幕右边缘",
        )
        check(
            chat._belongs({"session_id": session.id})
            and not chat._belongs({"session_id": child.id}),
            "父屏只认自己的 session_id",
        )

        # 父会话里的派发标记可点，直接进那个子 agent
        await pilot.click(".part-agent")
        await pilot.pause()
        check(
            isinstance(app.screen, SubagentChatScreen),
            f"点派发标记进入子 agent：{app.screen}",
        )
        app.screen.action_back()
        await pilot.pause()
        check(isinstance(app.screen, ChatScreen), "从标记进去之后能返回")

        listing = panel.query_one("#side-agents")
        check(len(listing.options) == 1, f"右栏列出了子 agent：{len(listing.options)}")

        # 用户派发要在父会话里留一条记录（模型派发有工具卡，用户没有）
        marker = session.messages[-1]
        check(
            len(marker.parts) == 1 and marker.parts[0].type is PartType.AGENT,
            f"父会话留下了一条 AGENT 记录：{[part.type for part in marker.parts]}",
        )
        check(
            marker.parts[0].agent_name == child.title
            and marker.parts[0].text == child.id,
            "记录里带着子 agent 的名字与编号",
        )

        # --- 两个会话同时要权限：排队，不叠模态 ---
        def _pending_ask(target: Session) -> tuple[Message, Part]:
            message = Message(role="assistant", session_id=target.id)
            part = message.add_part(
                Part(
                    type=PartType.TOOL,
                    tool_name="bash",
                    tool_status=ToolStatus.AWAITING,
                    tool_input={"command": "echo q"},
                )
            )
            target.add_message(message)
            return message, part

        root_message, root_part = _pending_ask(session)
        child_message, child_part = _pending_ask(child)
        app.event_bus.publish(
            "permission.ask",
            {
                "session_id": session.id,
                "message_id": root_message.id,
                "part_id": root_part.id,
            },
        )
        app.event_bus.publish(
            "permission.ask",
            {
                "session_id": child.id,
                "message_id": child_message.id,
                "part_id": child_part.id,
            },
        )
        await pilot.pause()
        check(isinstance(app.screen, PermissionDialog), "第一条询问弹出来")
        check(app.screen._agent_name == session.agent_name, "先弹的是先到的那个")
        await pilot.press("y")
        await pilot.pause()
        check(isinstance(app.screen, PermissionDialog), "答完一个，第二个接着弹")
        check(app.screen._agent_name == child.title, "第二个来自子 agent")
        await pilot.press("n")
        await pilot.pause()
        check(not isinstance(app.screen, PermissionDialog), "答完两个之后回到聊天界面")
        check(not app._permission_queue, "队列已经空了")

        # --- 右栏显示失败原因 ---
        child.status = "failed"
        child.status_note = "请求出错"
        chat.refresh_status()
        await pilot.pause()
        prompt = listing.get_option_at_index(0).prompt
        text = getattr(prompt, "plain", str(prompt))
        check("请求出错" in text, f"右栏写出了失败原因：{text}")
        # 每条两行：第一行名字与时间，第二行失败原因或消息条数。
        # 父会话历史区间与派发来源不常驻，它们只在打开之后才要读。
        check(
            len(text.splitlines()) == 2,
            f"右栏每条只有两行：{text}",
        )
        check(
            "第 " not in text and "派发" not in text,
            f"右栏不再常驻历史区间与派发来源：{text}",
        )

        # 后台子 agent 结束时，父会话给一句提示
        app.event_bus.publish(
            "agent.status",
            {"session_id": child.id, "status": "failed", "note": "请求出错"},
        )
        await pilot.pause()
        check(bool(chat.query(".toast")), "后台子 agent 失败时父会话弹出提示")

        # /agents：键盘用户也能进列表
        app.execute_command("agents")
        await pilot.pause()
        check(app.focused is listing, f"/agents 把焦点交给列表：{app.focused}")
        await pilot.press("escape")
        await pilot.pause()
        check(
            app.focused is chat.editor.input,
            f"列表里 esc 回到输入框：{app.focused}",
        )

        app.open_subagent(child.id)
        await pilot.pause()
        sub = app.screen
        check(isinstance(sub, SubagentChatScreen), f"点进子 agent：{sub}")
        banner = sub.query_one("#subagent-banner")
        check(banner.region.height == 1, f"返回栏只有一行：{banner.region.height}")
        check(
            banner.region.width == app.size.width,
            f"返回栏横贯屏幕：{banner.region.width}",
        )
        check(
            "用户派发" in str(banner.render()),
            f"返回栏写出派发来源：{banner.render()}",
        )
        check(
            "第 " not in str(banner.render()),
            f"空会话派发不写 fork 区间：{banner.render()}",
        )
        check(
            "请求出错" in str(sub.query_one("#subagent-banner").render()),
            "返回栏带着失败原因",
        )
        before_agent = sub.session.agent_name
        sub.editor.focus_input()
        await pilot.press("tab")
        await pilot.pause()
        check(
            not sub.editor.agent_popup.is_open,
            "子 agent 界面按 tab 不弹 Agent 列表",
        )
        check(
            sub.session.agent_name == before_agent,
            f"Build/Plan 没有被切走：{sub.session.agent_name}",
        )
        check(not sub.allows_command("agent"), "子 agent 界面不允许 /agent")
        check(
            "spawn_agent" not in app.host.loop_for(child.id).registry.names(),
            "子 agent 的注册表里没有派发工具",
        )
        names = [command.name for command in sub.command_registry.all()]
        check("agent" not in names, f"命令面板里没有 /agent：{names}")
        check("back" in names, "命令面板里保留了 /back")

        app.execute_command("agent")
        await pilot.pause()
        check(
            isinstance(app.screen, SubagentChatScreen),
            "在子 agent 界面执行 /agent 不会打开派发模态",
        )

        # /rename 改的是这一屏对应的子 agent，不是父会话
        app.execute_command("rename 改过的名字")
        await pilot.pause()
        check(child.title == "改过的名字", f"子 agent 被重命名：{child.title}")
        check(
            "改过的名字" in str(sub.query_one("#subagent-banner").render()),
            "返回栏跟着改名",
        )

        sub.action_back()
        await pilot.pause()
        check(
            isinstance(app.screen, ChatScreen)
            and not isinstance(app.screen, SubagentChatScreen),
            "返回之后回到父会话",
        )
        check(
            not app.session_manager.current.is_subagent,
            "返回不会把当前会话切到子 agent 上",
        )

        # /delete：确认之后子 agent 消失并退回父会话
        app.open_subagent(child.id)
        await pilot.pause()
        app.execute_command("delete")
        await pilot.pause()
        check(isinstance(app.screen, ConfirmDialog), f"删除先确认：{app.screen}")
        await pilot.press("y")
        await pilot.pause()
        check(
            isinstance(app.screen, ChatScreen)
            and not isinstance(app.screen, SubagentChatScreen),
            "删掉当前子 agent 后退回父会话",
        )
        check(
            app.session_manager.get_session(child.id) is None,
            "子 agent 的数据已经删掉",
        )
        check(
            not app.host.children_of(session.id),
            "右栏的数据源里也没有它了",
        )

        # --- /sessions 里只有根会话，重命名之后也不会混进子 agent ---
        second = app.session_manager.create_session("第二个根")
        app.execute_command("sessions")
        await pilot.pause()
        picker = app.screen
        check(isinstance(picker, SessionPickerScreen), f"打开会话选择器：{picker}")
        check(
            all(not item.is_subagent for item in picker.sessions),
            f"打开时只列根会话：{[item.title for item in picker.sessions]}",
        )
        picker._apply_rename(second.id, "改名了")
        await pilot.pause()
        check(
            all(not item.is_subagent for item in picker.sessions),
            "重命名后重新填充仍然只有根会话",
        )
        picker.action_dismiss()
        await pilot.pause()

        # --- 仅创建：任务书先存着，不自动跑；开始条与 ctrl+r 是入口 ---
        app.execute_command("agent")
        await pilot.pause()
        modal = app.screen
        check(isinstance(modal, SpawnAgentScreen), f"再次打开派发模态：{modal}")
        modal.query_one("#spawn-name", Input).value = "待开始"
        modal.query_one("#spawn-task", TextArea).text = "查一下 5AFN"
        await pilot.click("#spawn-create-only")
        await pilot.pause()
        idle = app.host.children_of(session.id)[0]
        check(idle.status == "idle", f"仅创建不自动跑：{idle.status}")
        check(idle.has_pending_prompt, "任务书写成了一条待发用户消息")
        check(
            "查一下 5AFN"
            in "".join(p.text for m in idle.messages for p in m.parts),
            "任务书真的落进了子会话",
        )
        check(
            state.exists() and idle.id in state.read_text(encoding="utf-8"),
            "创建当场就落盘，不靠子 agent 跑完",
        )

        # 派发标记不是用户气泡：notice 样式，底色透明、没有边框
        notices = chat.query(".message-notice")
        check(bool(notices), "派发标记走 message-notice 样式")
        check(
            all(notice.styles.background.a == 0 for notice in notices),
            "派发标记没有气泡底色",
        )
        check(
            all(not notice.styles.border.top[0] for notice in notices),
            "派发标记没有气泡边框",
        )

        app.open_subagent(idle.id)
        await pilot.pause()
        sub2 = app.screen
        bar = sub2.query_one(StartBar)
        check(bar.styles.display == "block", "待开始的子 agent 显示开始条")
        check(
            "ctrl+r" in [binding.key for binding in SubagentChatScreen.BINDINGS],
            "开始条有键盘入口",
        )
        sub2.action_start()
        check(app.host.is_running(idle.id), "开始条起了一轮")
        app.host.cancel(idle.id)
        await pilot.pause()
        sub2.action_back()
        await pilot.pause()
        app.open_subagent(idle.id)
        await pilot.pause()
        app.execute_command("delete")
        await pilot.press("y")
        await pilot.pause()
    state.unlink(missing_ok=True)


async def check_embed() -> None:
    """语义通道：可用性判定、缺模型时降级、余弦下限、矩阵缓存按版本失效、三路融合。

    **不依赖真模型**：编码器整个打桩成四维词袋，模型目录指到临时目录。这组判据
    要抓的是「缓存拿旧 id 配新块」「没模型时把 30 条 0.2 分当命中」这类错误——
    它们与模型准不准无关，用真模型反而更难复现。
    """
    import tempfile
    from pathlib import Path as _Path

    from vsagent.core import corpus as corpus_module
    from vsagent.core import corpus_config as corpus_config_module
    from vsagent.core import embed as embed_module
    from vsagent.core import index as index_module
    from vsagent.core.agent.domain import corpus as corpus_tools

    root = _Path(tempfile.mkdtemp(prefix="vsa-embed-"))
    model = "fake/model"
    original_root = corpus_module.corpus_root
    original_models = embed_module.models_dir
    original_available = embed_module.available
    original_passages = embed_module.encode_passages
    original_query = embed_module.encode_query
    original_config = corpus_config_module.load
    corpus_module.corpus_root = lambda: root
    corpus_config_module.load = lambda root=None: corpus_config_module.CorpusConfig(
        index_web=True, embed=True, embed_model=model
    )

    def vector(text: str) -> list[float]:
        """四维词袋 + 一维「其它」：apple / vina / cherry / kinase 各占一维。

        别的词落到第五维，与四维正交——余弦 0，正好用来验下限。给「其它」也塞进
        第一维（比如 0.1）会被归一化放大成 1.0，下限就永远触发不了。
        """
        lowered = text.lower()
        values = [1.0 if word in lowered else 0.0 for word in ("apple", "vina", "cherry", "kinase")]
        if not any(values):
            return [0.0, 0.0, 0.0, 0.0, 1.0]
        norm = sum(value * value for value in values) ** 0.5
        return [value / norm for value in values] + [0.0]

    def fake_passages(texts: list[str], _model: str) -> list[list[float]]:
        return [vector(text) for text in texts]

    def fake_query(text: str, _model: str) -> list[float]:
        return vector(text)

    def store(conn: Any, source_id: str, body: str, title: str) -> int:
        """同时写索引与目录：检索会按目录存在性对账，只写索引的话命中会被判成「已删除」。"""
        directory = corpus_module.web_dir(source_id.split(":", 1)[1])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / corpus_module.META_FILE).write_text(
            json.dumps({"title": title}, ensure_ascii=False), encoding="utf-8"
        )
        return index_module.upsert_web_page(
            conn, source_id, f"# {title}\n\n{body}\n", {"title": title, "added_at": 1.0}
        )

    try:
        # ---- 一、模型不在本机：说明白、不抛、检索降级 ----
        embed_module.models_dir = lambda: root / "models"
        check(not embed_module.available(model), "没有模型文件时 available 为 False")
        reason = embed_module.missing_reason(model)
        check("语义通道未启用" in reason, f"理由说人话：{reason.splitlines()[0][:40]}")
        check(
            "download" in reason or "requirements-embed" in reason,
            "理由里给出能照着做的那一步",
        )
        conn = index_module.connect()
        try:
            skipped = embed_module.index_source(conn, "web:aaaaaaaaaa", model)
            check(
                skipped["enabled"] is False and skipped["embedded"] == 0,
                f"模型没下载时入库不报错，只标未启用：{skipped}",
            )
            check(embed_module.search(conn, "apple", model) == [], "模型没下载时检索返回空")
        finally:
            conn.close()

        # ---- 二、装上之后：入库、检索、余弦下限 ----
        embed_module.available = lambda _model: True
        embed_module.encode_passages = fake_passages
        embed_module.encode_query = fake_query
        conn = index_module.connect()
        try:
            store(conn, "web:aaaaaaaaaa", "apple pie", "A")
            store(conn, "web:bbbbbbbbbb", "vina docking", "B")
            result = embed_module.index_source(conn, "web:aaaaaaaaaa", model)
            check(result["embedded"] == 1, f"给缺向量的块补上：{result}")
            embed_module.index_source(conn, "web:bbbbbbbbbb", model)
            check(embed_module.coverage(conn, model) == 1.0, "覆盖率 100%")
            check(
                embed_module.coverage(conn, "another/model") == 0.0,
                "覆盖率按模型分开算（换模型之后要从 0 重算）",
            )

            apple_id = int(
                conn.execute("SELECT id FROM chunks WHERE text LIKE '%apple%'").fetchone()[0]
            )
            hits = embed_module.search(conn, "apple", model)
            check(hits and hits[0][0] == apple_id, f"余弦把 apple 排第一：{hits}")
            check(
                embed_module.search(conn, "apple", model, limit=1) == hits[:1],
                "limit 生效",
            )

            # 余弦下限：无关查询（0.1 分）不该被当成命中
            check(
                embed_module.search(conn, "zzz", model) == [],
                "低于余弦下限的邻居不算命中（否则「本地没有」会变成「命中 29 组」）",
            )
            os.environ[embed_module.MIN_SCORE_ENV] = "-1"
            check(
                bool(embed_module.search(conn, "zzz", model)),
                "下限可以用 VSA_EMBED_MIN_SCORE 覆盖",
            )
            os.environ[embed_module.MIN_SCORE_ENV] = "不是数字"
            check(embed_module.min_score() == embed_module.MIN_SCORE, "写坏了回落默认值")
            os.environ.pop(embed_module.MIN_SCORE_ENV, None)

            # ---- 三、矩阵缓存按版本失效（同 id 数、不同块）----
            before = embed_module._matrix(conn, model)[1]
            index_module.drop_source(conn, "web:aaaaaaaaaa")
            store(conn, "web:cccccccccc", "cherry tart", "C")
            embed_module.index_source(conn, "web:cccccccccc", model)
            expected = [
                int(row[0])
                for row in conn.execute("SELECT chunk_id FROM vectors ORDER BY chunk_id")
            ]
            check(
                embed_module._matrix(conn, model)[1] == expected,
                f"删源（级联清向量）之后矩阵缓存也要失效：{before} → {expected}",
            )
            check(
                embed_module.search(conn, "cherry", model)[0][0]
                == int(conn.execute("SELECT id FROM chunks WHERE text LIKE '%cherry%'").fetchone()[0]),
                "失效之后查到的是新块",
            )
        finally:
            conn.close()

        # ---- 三之二、下载：显式端点排第一，失败自动退镜像，两个都挂给可执行的提示 ----
        if embed_module._installed():
            seen: list[str] = []

            def fake_snapshot(endpoint: str, _model: str) -> bool:
                seen.append(endpoint)
                return False

            original_snapshot = embed_module._snapshot
            embed_module._snapshot = fake_snapshot
            os.environ["HF_ENDPOINT"] = "https://example.invalid"
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    code = embed_module.download(model)
            finally:
                embed_module._snapshot = original_snapshot
                os.environ.pop("HF_ENDPOINT", None)
            check(code == 1, "两个端点都失败时返回非零，不是假装成功")
            check(
                seen == ["https://example.invalid", embed_module.MIRROR_ENDPOINT],
                f"先试显式端点、再退镜像：{seen}",
            )
            check(
                "下载失败" in buffer.getvalue(),
                f"失败时说清是下载失败：{buffer.getvalue()[:60]}",
            )

        # ---- 四、三路融合：向量与词法各自的第一名都在结果里 ----
        fused = corpus_tools.fuse({"fts": [10, 11], "vec": [11, 12]})
        check(
            set(fused) == {10, 11, 12},
            f"RRF 把三路合成一份分数表：{sorted(fused)}",
        )
        check(
            fused[11] > fused[10] and fused[11] > fused[12],
            "两路都命中的块排在单路命中之前",
        )

        # ---- 五、search_corpus 的降级与 meta ----
        original_reason = embed_module.missing_reason
        embed_module.missing_reason = lambda _model: "语义通道未启用：测试"
        tool = corpus_tools.SearchCorpusTool()
        result = await tool.run({"query": "apple"})
        check(
            "vec" not in (result.meta.get("channels") or {}),
            f"模型不可用时只跑词法两路：{result.meta.get('channels')}",
        )
        check(result.meta.get("vector_coverage") == 0.0, "meta 里如实写 0 覆盖")
        embed_module.missing_reason = original_reason

        result = await tool.run({"query": "cherry"})
        check(
            "vec" in (result.meta.get("channels") or {}),
            f"模型可用时三路都跑：{result.meta.get('channels')}",
        )
        check(
            "web#" in result.output and "cherry" in result.output,
            f"语义那一路把块捞出来了：{result.output[:80]}",
        )
    finally:
        corpus_module.corpus_root = original_root
        embed_module.models_dir = original_models
        embed_module.available = original_available
        embed_module.encode_passages = original_passages
        embed_module.encode_query = original_query
        corpus_config_module.load = original_config
        os.environ.pop(embed_module.MIN_SCORE_ENV, None)
        shutil.rmtree(root, ignore_errors=True)


def check_paths() -> None:
    """数据目录的根：源码运行时是仓根，打包后跟启动目录走。

    这一条防的是「打包后写进解包临时目录」：那种错每一步都成功——文献落了盘、
    索引也建了、网页上当场能查——只有重启之后才发现 `corpus/` 与 `runs/` 空
    了（onefile 退出即删）。原先 `corpus_root()` / `runs_dir()` 都按 `__file__`
    算，正好落在这里面。
    """
    from vsagent.core.agent.domain.export import runs_dir
    from vsagent.core.corpus import corpus_root
    from vsagent.core.paths import project_root

    check(project_root() == ROOT, f"源码运行时项目根是仓根：{project_root()}")
    check(corpus_root() == ROOT / "corpus", f"语料根：{corpus_root()}")
    check(runs_dir() == ROOT / "runs", f"运行目录根：{runs_dir()}")

    frozen_cwd = Path(tempfile.mkdtemp(prefix="vsa-paths-")).resolve()
    had_frozen = hasattr(sys, "frozen")
    old_frozen = getattr(sys, "frozen", None)
    old_executable = sys.executable
    old_cwd = os.getcwd()
    sys.frozen = True
    sys.executable = "/opt/vsagent/bin/VSAgent"
    os.chdir(frozen_cwd)
    try:
        check(
            project_root() == frozen_cwd,
            f"打包后项目根是启动目录：{project_root()}",
        )
        check(corpus_root() == frozen_cwd / "corpus", f"语料跟着这个根：{corpus_root()}")
        check(runs_dir() == frozen_cwd / "runs", f"运行目录也跟着：{runs_dir()}")
    finally:
        os.chdir(old_cwd)
        if had_frozen:
            sys.frozen = old_frozen
        else:
            del sys.frozen
        sys.executable = old_executable
        shutil.rmtree(frozen_cwd, ignore_errors=True)


async def check_id_sequences() -> None:
    """载入状态只把编号往前推，网页端读一次盘不会把正在跑的这一轮拨回去。

    现场出过这个事故：一轮跑到第 13 步，浏览器打开会话页（`/api/sessions`），
    `read_settled` 新建一个 SessionManager 读盘，`sync_sequences` 按磁盘上的
    最大编号重设，于是这一步的 part 又拿到第 1 步用过的号，正文写进了旧工具卡。
    """
    from vsagent.core.models import new_message_id, new_part_id
    from vsagent.core.web.routes import read_settled

    root = PROBE / "id_sequences"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "state.toml"

    def state(max_part: int, message_id: str = "msg_0001") -> dict:
        """一份只带编号的状态：够 `sync_sequences` 取最大值就行。"""
        parts = [
            {"id": f"prt_{index:04d}", "type": "text", "text": "x"}
            for index in range(1, max_part + 1)
        ]
        return {
            "version": 1,
            "current_session_id": "ses_0001",
            "sessions": [
                {
                    "id": "ses_0001",
                    "messages": [{"id": message_id, "role": "user", "parts": parts}],
                }
            ],
        }

    # 计数器是整个进程共用的一份，别的检查组早就发过号，这里只能按相对位置断言。
    serial = int(new_part_id().split("_")[1])
    manager = SessionManager(path)
    manager.load_from_state(state(serial + 5))
    check(new_part_id() == f"prt_{serial + 6:04d}", "载入之后从磁盘最大值之后接着发号")

    taken = [new_part_id(), new_part_id()]
    check(taken == [f"prt_{serial + 7:04d}", f"prt_{serial + 8:04d}"], f"连号：{taken}")

    # 磁盘上那一份还是老样子（状态只在一轮结束时写盘），网页端读它不能把号拨回去。
    manager.save_to_toml(path)
    check(read_settled(path) is not None, "网页端读得动这份状态")
    after = new_part_id()
    check(after == f"prt_{serial + 9:04d}", f"读盘不往回拨号：拿到 {after}")
    check(after not in taken, "读盘之后不会重发已经用过的号")

    # 反过来：磁盘上的编号比手里的大，就该往前推。
    manager.load_from_state(state(serial + 40))
    check(new_part_id() == f"prt_{serial + 41:04d}", "磁盘上的编号更大时往前推")

    message_serial = int(new_message_id().split("_")[1])
    manager.load_from_state(state(1, message_id=f"msg_{message_serial + 5:04d}"))
    check(
        new_message_id() == f"msg_{message_serial + 6:04d}",
        "消息编号也只往前推",
    )


# 检查组：名字给 `--only` 过滤用，函数一律零参数（同步与异步混着，调用处看返回值）。
# 名字取函数名里有意义的那一段，`--only permission` 能直接命中。
GROUPS: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("types", check_agent_types),
    ("arguments", check_arguments),
    ("connection", check_connection_file),
    ("trace", check_trace),
    ("rules", check_rules),
    ("prefix", check_prefix),
    ("permission", check_permission),
    ("hooks-config", check_hook_config),
    ("hooks-runner", check_hook_runner),
    ("workspace", check_workspace),
    ("config-paths", check_config_paths),
    ("config-migrate", check_config_migrate),
    ("id-sequence", check_id_sequences),
    ("paths", check_paths),
    ("ledger", check_ledger),
    ("ledger-digest", check_ledger_digest),
    ("env-block", check_env_block),
    ("focus", check_focus),
    ("products", check_products),
    ("tool-context", check_tool_context),
    ("uniprot", check_uniprot_tools),
    ("pdbtext", check_pdbtext),
    ("preprocess", check_preprocess),
    ("export-geometry", check_export_geometry),
    ("complexes", check_complexes),
    ("prankweb", check_prankweb),
    ("pockets", check_pockets),
    ("pocket-tools", check_pocket_tools),
    ("lipid-screen", check_lipid_screen),
    ("pocket-profile", check_pocket_profile),
    ("rank-candidates", check_rank_candidates),
    ("analyze-pose", check_analyze_pose),
    ("alternative-poses", check_alternative_poses),
    ("contact-map", check_contact_map),
    ("hydrophobic", check_hydrophobic),
    ("efficiency", check_efficiency),
    ("curate-ligands", check_curate_ligands),
    ("reference-set", check_reference_set),
    ("screen-similarity", check_screen_similarity),
    ("screen-shape", check_screen_shape),
    ("validate-docking", check_validate_docking),
    ("batch-offset", check_batch_offset),
    ("mmcif", check_mmcif),
    ("assess-structure", check_assess_structure),
    ("admet", check_admet),
    ("sar", check_sar),
    ("hypothesis", check_hypothesis),
    ("strategy", check_strategy_compare),
    ("campaign-run", check_campaign_run),
    ("pharmacophore", check_pharmacophore),
    ("identity", check_identity_floor),
    ("align-prior", check_align_and_prior),
    ("write-run", check_write_run),
    ("write-run-docking", check_write_run_docking),
    ("survey", check_survey),
    ("rsearch", check_rsearch),
    ("literature", check_literature),
    ("europepmc", check_europepmc),
    ("pubchem", check_pubchem),
    ("opentargets", check_opentargets),
    ("websearch", check_websearch),
    ("corpus", check_corpus),
    ("embed", check_embed),
    ("structure-parse", check_structure_parse),
    ("structure-tools", check_structure_tools),
    ("alphafold", check_alphafold),
    ("chembl", check_chembl_tools),
    ("chemistry-gate", check_chemistry_gate),
    ("receptor", check_receptor_tool),
    ("ligand-import", check_ligand_import),
    ("ligand-prep", check_ligand_prepare),
    ("docking", check_docking),
    ("affinity", check_affinity),
    ("listing", check_listing_tools),
    ("plan-deny", check_plan_deny),
    ("prompt", check_prompt),
    ("memory", check_memory),
    ("memory-supersede", check_memory_supersede),
    ("memory-resident", check_memory_resident),
    ("task-state", check_task_state),
    ("subagent", check_subagent),
    ("subagent-ui", check_subagent_ui),
    ("selective-compression", check_selective_compression),
    ("context-budget", check_context_budget),
    ("reasoning", check_reasoning),
    ("output-limit", check_output_limit),
    ("evidence-ref", check_evidence_ref),
    ("evidence-dedupe", check_evidence_dedupe),
    ("tool-clear", check_tool_clear),
    ("tool-family", check_tool_family),
    ("debug-tools", check_debug_tools),
    ("tool-retry", check_tool_retry),
    ("live-snapshot", check_live_snapshot),
    ("topic-view", check_topic_views),
    ("event-log", check_event_log),
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
    ("context-metrics", check_context_metrics),
    ("tool-family-loop", check_tool_family_loop),
    ("event-capture", check_event_capture),
    ("topic", check_topic),
    ("phase-compaction", check_overflow_phase),
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
