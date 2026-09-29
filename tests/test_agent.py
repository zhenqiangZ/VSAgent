from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from opencode_tui.app import OpenCodeApp  # noqa: E402
from opencode_tui.core.agent.loop import MAX_RETRIES, AgentLoop  # noqa: E402
from opencode_tui.core.agent.prompt import (  # noqa: E402
    KEEP_MESSAGES,
    build_model_messages,
    build_system_prompt,
    estimate_tokens,
    truncate_for_model,
)
from opencode_tui.core.agent.provider import (  # noqa: E402
    OpenAICompatProvider,
    ProviderConfig,
    ProviderError,
    ProviderEvent,
    ProviderEventType,
)
from opencode_tui.core.agent.tools import (  # noqa: E402
    BashTool,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    argument_error,
)
from opencode_tui.core.agent.workspace import Workspace  # noqa: E402
from opencode_tui.core.event_bus import EventBus  # noqa: E402
from opencode_tui.core.models import Message, Part, PartType, Session, ToolStatus  # noqa: E402
from opencode_tui.screens.chat import ChatScreen  # noqa: E402

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

WORKSPACE_FIXTURE = ROOT / ".probe" / "workspace_fixture"

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

    config = ProviderConfig.from_env(
        {
            "VSA_LLM_BASE_URL": "http://127.0.0.1:1/v1",
            "VSA_LLM_API_KEY": "k",
            "VSA_LLM_MODEL": "m",
            "VSA_LLM_PRICE_INPUT": "0.5",
            "VSA_LLM_PRICE_OUTPUT": "1.5",
            "VSA_LLM_CONTEXT_WINDOW": "1000000",
        }
    )
    check(config.model == "m" and config.price_out == 1.5, "ProviderConfig 从环境变量读出连接参数")
    check(config.price_in == 0.5 and config.context_window == 1_000_000, "单价与上下文窗口一并读出来")
    bare = ProviderConfig.from_env(
        {"VSA_LLM_BASE_URL": "u", "VSA_LLM_API_KEY": "k", "VSA_LLM_MODEL": "m"}
    )
    check(bare.price_in == 0.0 and bare.price_out == 0.0, "没给单价时按零计")
    check(bare.context_window == 128_000, "没给上下文窗口时用默认值")

    missing = ""
    try:
        ProviderConfig.from_env({})
    except RuntimeError as error:
        missing = str(error)
    check("VSA_LLM_BASE_URL" in missing, "缺少环境变量时直接抛错并点名缺了哪一个")


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
    built = build_model_messages(session, system_prompt, context_window=200_000)
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
    built = build_model_messages(session, system_prompt, context_window=200_000)
    plain_item = next(item for item in built.messages if item.get("content") == "就一句话，没有思考。")
    check("reasoning_content" not in plain_item, "没有推理的正文不带上空的 reasoning 字段")

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
    built = build_model_messages(session, system_prompt, context_window=200_000)
    check(len(built.messages) == 6, "还停在 pending 或 running 的工具不发给模型")

    long_session = Session(id="ses_long", title="长会话")
    for index in range(40):
        item = Message(role="user" if index % 2 == 0 else "assistant", session_id=long_session.id)
        item.add_part(Part(type=PartType.TEXT, text=f"第 {index} 条消息，" + "内" * 400))
        long_session.add_message(item)
    budget = int(2000 * 0.8)
    built = build_model_messages(long_session, system_prompt, context_window=2000)
    kept = estimate_tokens(system_prompt) + sum(
        estimate_tokens(str(item["content"]))
        for item in built.messages
        if item["role"] != "system"
    )
    check(built.dropped > 0, f"超长历史开始丢弃更早的消息：丢了 {built.dropped} 条")
    check(kept <= budget, f"丢完之后的估算值在窗口的八折以内：{kept} <= {budget}")
    check(
        len(built.messages) - 1 == 40 - built.dropped,
        "留在消息列表里的条数与被丢掉的条数相加正好是原来的条数",
    )
    check(built.dropped <= 40 - KEEP_MESSAGES, "最近六条不参与丢弃")
    kept_text = json.dumps([item["content"] for item in built.messages[1:]], ensure_ascii=False)
    present = sum(1 for index in range(40) if f"第 {index} 条消息" in kept_text)
    check(present == 40 - built.dropped, "丢掉的是开头那一段，留下的条数逐个对得上")
    check("第 39 条消息" in kept_text, "最后一条消息永远留着")

    long_output = "甲" * 3000 + "乙" * 2000 + "丙" * 3000
    truncated = truncate_for_model(long_output)
    check(len(truncated) < len(long_output), "超长输出被裁短")
    check(truncated.startswith("甲" * 2000), "留住了开头 2000 个字符")
    check(truncated.endswith("丙" * 2000), "留住了结尾 2000 个字符")
    check("… 省略 4000 字符" in truncated, "中间写明省掉了多少字符")
    check(truncate_for_model("短输出") == "短输出", "不超限的输出原样返回")


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
    check(usage[0].usage == {"input": 11, "output": 22}, "用量归一成 input 与 output")
    check(events[-1].type is ProviderEventType.USAGE, "用量事件排在末尾")

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
) -> AgentLoop:
    """按本地服务装配一个循环层。"""
    config = ProviderConfig(
        base_url=server.base_url,
        api_key="secret",
        model="fake-model",
        price_in=price_in,
        price_out=price_out,
    )
    return AgentLoop(
        bus,
        OpenAICompatProvider(config),
        registry,
        config,
        context_window=context_window,
        workspace_root=WORKSPACE_FIXTURE,
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
    check(message.tokens == {"input": 10, "output": 20}, "用量记在这一轮的消息上")
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
    check(message.tokens == {"input": 30, "output": 12}, "两次发问的用量累加")
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
    check(len([part for part in message.parts if part.type is PartType.RETRY]) == MAX_RETRIES, "重试次数用满")
    check(
        texts and texts[-1].startswith("请求没有成功：") and "503" in texts[-1],
        f"错误原文写进最后一条 TEXT：{texts[-1] if texts else '没有正文'}",
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
    texts = [part.text for part in message.parts if part.type is PartType.TEXT]
    check(texts and "401" in texts[-1], f"凭据被拒的错误原文写上：{texts[-1] if texts else '没有正文'}")
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
    loop.context_window = 2000
    server.script(Reply(chunks=[*text_chunks("好", size=1), usage_chunk(2, 2)]))
    message = await loop.stream_reply(compact_session, "")
    loop.context_window = 200_000
    assert message is not None
    compactions = [part for part in message.parts if part.type is PartType.COMPACTION]
    check(len(compactions) == 1, "历史超过窗口时补一条 COMPACTION Part")
    request = server.requests[-1]["messages"]
    content = json.dumps([item["content"] for item in request], ensure_ascii=False)
    present = sum(1 for index in range(40) if f"第 {index} 条消息" in content)
    check(
        compactions[0].text == f"已丢弃 {40 - present} 条更早的消息",
        f"丢弃条数与文案一致：{compactions[0].text}",
    )
    total = sum(estimate_tokens(str(item["content"])) for item in request)
    check(total <= int(2000 * 0.8), f"发出去的历史降到窗口的八折以内：{total}")

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


async def check_live_app(server: FakeModelServer) -> None:
    """从界面提交一句话到消息区出现完整回复。"""
    state = ROOT / ".probe" / "state_live.toml"
    os.environ["VSA_LLM_BASE_URL"] = server.base_url
    os.environ["VSA_LLM_API_KEY"] = "secret"
    os.environ["VSA_LLM_MODEL"] = "fake-model"
    os.environ["VSA_LLM_CONTEXT_WINDOW"] = "1000000"
    os.environ["VSA_LLM_PRICE_INPUT"] = "0.5"
    os.environ["VSA_LLM_PRICE_OUTPUT"] = "1.5"

    state.unlink(missing_ok=True)
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
    app = OpenCodeApp(root=ROOT, state_path=state)
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
        check(assistant.tokens == {"input": 9, "output": 11}, "用量写在这一轮的消息上")
        check(assistant.model_id == "fake-model", "消息上写着实际使用的模型")
        check(app.current_model_label == "fake-model", "状态行上的模型名取自 .env")
        check(app.context_window == 1_000_000, "上下文窗口取自 .env")
        check(
            [model.name for model in app.available_models] == ["fake-model"],
            "模型列表里只有 .env 给出的那一个",
        )
        check(
            abs(assistant.cost - 0.000021) < 1e-9,
            f"这一轮的金额按 .env 里的单价算：{assistant.cost}",
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
    app = OpenCodeApp(root=ROOT, state_path=state)
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
        await pilot.press("ctrl+c")
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


async def run_agent_checks() -> None:
    """循环层与 provider 的全部断言。"""
    check_agent_types()
    check_workspace()
    check_prompt()
    await check_bash_tool()
    server = FakeModelServer()
    server.start()
    try:
        await check_provider(server)
        await check_loop(server)
        await check_live_app(server)
    finally:
        server.stop()


if __name__ == "__main__":
    asyncio.run(run_agent_checks())
    print(f"通过 {len(CHECKS)} 项检查：")
    for item in CHECKS:
        print(f"  - {item}")
