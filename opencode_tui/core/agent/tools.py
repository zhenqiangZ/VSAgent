from __future__ import annotations

import asyncio
import codecs
import os
import re
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..file_log import record as log_read

# bash 一次调用的等待上限。
#
# 这个名字与 `provider.DEFAULT_TIMEOUT` 刻意不同：两处原先都叫 DEFAULT_TIMEOUT，
# 都是 120.0，但管的是两件事——这里管一条命令跑多久，那里管一次模型请求等多久。
# 同名让人改了一处以为改了两处（也可能改错了一处），所以分开命名。
#
# 定 600 秒的理由：对接、编译、装包这类命令轻易越过两分钟，而 120 秒截断的结果是
# 一条命令被判超时、模型拿到半截失败又重来一次，比多等几分钟更费事。
BASH_TIMEOUT = 600.0

DEFAULT_TIMEOUT = 120.0

READ_LIMIT = 2000
GREP_LIMIT = 200
GREP_FILE_LIMIT = 40
CODE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".sql", ".lua", ".pl", ".r", ".m", ".mm",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".html", ".htm", ".css", ".scss", ".less",
    ".vue", ".svelte", ".xml", ".csv", ".tsv", ".env", ".gitignore",
}

LINE_NUMBER_WIDTH = 6


def resolve_path(base: Path | None, raw: str) -> Path:
    """把工具给出的路径归一成绝对路径。

    相对路径按工具自己的基准目录解释，没有基准目录时按当前工作目录解释。
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


def count_lines(text: str) -> int:
    """数一段文本里的行数。空文本算 0 行。"""
    if not text:
        return 0
    return len(text.splitlines())


def number_lines(text: str, start: int = 1) -> str:
    """给每一行前面加上行号。

    行号是文件里的绝对行号，模型据它定位后续的改动参数。
    """
    lines = text.split("\n")
    width = max(LINE_NUMBER_WIDTH, len(str(start + len(lines) - 1)))
    return "\n".join(
        f"{start + offset:>{width}} | {line}" for offset, line in enumerate(lines)
    )


def change_counts(old: str, new: str) -> tuple[int, int]:
    """按行比较两段文本，给出新增行数与删除行数。

    两端相同的前后缀先切掉，中间剩下的部分按最长公共子序列对应起来，
    这样只改一行的场景不会把整段算成一次全删全加。
    """
    before = old.splitlines()
    after = new.splitlines()
    head = 0
    while head < len(before) and head < len(after) and before[head] == after[head]:
        head += 1
    tail = 0
    while (
        tail < len(before) - head
        and tail < len(after) - head
        and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]
    ):
        tail += 1
    left = before[head : len(before) - tail]
    right = after[head : len(after) - tail]
    if not left:
        return len(right), 0
    if not right:
        return 0, len(left)
    common = _lcs_length(left, right)
    return len(right) - common, len(left) - common


def _lcs_length(left: list[str], right: list[str]) -> int:
    """两段行的最长公共子序列长度。只留一行的工作数组。"""
    if len(left) < len(right):
        left, right = right, left
    previous = [0] * (len(right) + 1)
    for row in left:
        current = [0] * (len(right) + 1)
        for index, column in enumerate(right, start=1):
            if row == column:
                current[index] = previous[index - 1] + 1
            else:
                current[index] = max(previous[index], current[index - 1])
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具对模型可见的三样东西：名字、说明、参数模式。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        """转成 /chat/completions 请求体里 tools 数组的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    """一次工具执行的结果。失败时 output 里写的是原因。

    added 与 removed 是一次改动带来的行数，只有改动文件的工具会填。
    其余工具保持 0，界面据此决定要不要在工具那一行上显示增删。

    meta 放结构化读数（命中的条数、批量里成功几条这类），供界面与 trace 取用。
    **它是给机器看的**：模型看到的是 output 那一段文字，meta 不参与拼接，
    这样同一份计数不必为了「给模型看」再格式化一遍文字。
    """

    output: str = ""
    failed: bool = False
    added: int = 0
    removed: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class ToolError(Exception):
    """工具内部可以预料到的失败，原文会被回灌给模型。"""


class RetryableToolError(ToolError):
    """可以重试的瞬时故障（网络抖动、文件一时被占用）。

    与 `ToolError` 分开，是为了让重试逻辑只重试这一类；
    参数写错、依赖没装这些重试多少次都一样，不该白等。
    """


class Ctx:
    """标记：这类参数由内核注入，模型看不到、也不许传。

    工具签名里写 `annotated: Annotated[SessionLedger, Ctx]`，
    参数模式（`ToolSpec.parameters`）里**不要**列它——
    列了模型就会试着编一个值填进去。`Tool.run` 收到 context 之后按标记把它塞进参数。
    """

    __slots__ = ()


@dataclass
class BatchFailure:
    """批量操作里一条失败记录。"""

    index: int
    key: str
    reason: str


@dataclass
class BatchOutcome:
    """批量操作的部分成功语义。

    **只要有一条成功，整批就算成功**（`ok=True`），失败的那些按原因分组写回
    结果里。一万个分子总有几条解析不了，整批报失败会逼模型重试整批——
    而重试救不回那几条坏输入，只会把好的一起重跑。

    只有**一条都没成功**时才是失败：那时重试是有意义的（可能整批的输入都错了），
    所以那一侧才给 `ok=False` 并明确说「重试无用」。
    """

    total: int = 0
    succeeded: list[str] = field(default_factory=list)
    failed: list[BatchFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """至少成功一条就算成功；一条都没成功（或者本来就没有输入）才失败。"""
        return bool(self.succeeded)

    @property
    def succeeded_count(self) -> int:
        return len(self.succeeded)

    def grouped_failures(self) -> dict[str, list[str]]:
        """失败按原因归组：同一句原因下挂着哪些条目。"""
        grouped: dict[str, list[str]] = {}
        for item in self.failed:
            grouped.setdefault(item.reason, []).append(item.key)
        return grouped

    def describe(self, *, noun: str = "条目") -> str:
        """给模型看的一段话：成功多少、失败多少、每类原因各是哪些。"""
        lines = [f"共 {self.total} 个{noun}，成功 {self.succeeded_count} 个，失败 {len(self.failed)} 个。"]
        if not self.ok and self.failed:
            lines.append("这次一个都没成功，重试整批没有用，请先看清失败原因再改输入。")
        for reason, keys in self.grouped_failures().items():
            shown = "、".join(keys[:10])
            more = f" 等 {len(keys)} 个" if len(keys) > 10 else ""
            lines.append(f"  失败原因「{reason}」：{shown}{more}")
        return "\n".join(lines)

    def as_meta(self) -> dict[str, Any]:
        """给 `ToolResult.meta` 用的结构化读数。"""
        return {
            "total": self.total,
            "succeeded": self.succeeded_count,
            "failed": len(self.failed),
            "reasons": {reason: len(keys) for reason, keys in self.grouped_failures().items()},
        }


class Tool(ABC):
    """工具的基类。"""

    spec: ToolSpec
    # 判定规则比对的那个参数名。没有单一定位参数的工具留空。
    primary_argument: str = ""
    # 收 context 的参数名（标了 Ctx 的那个）。没有这类参数时留空。
    context_argument: ClassVar[str] = ""
    # 收任务状态的参数名。没有这类参数时留空。
    #
    # 与 context_argument 分开，是因为两者由内核从不同地方取：context 是会话登记表
    # （循环层持有、会话切换时换掉），任务状态挂在会话自己身上、每次调用现取。
    # 挤进同一个口子的话，取登记表的那些工具会收到一个既不是登记表也不是任务状态的东西。
    task_argument: ClassVar[str] = ""
    # 收当前会话的参数名。只有需要知道「这是谁在调用」的工具才声明。
    #
    # 与 task_argument 分开：任务状态是会话上的一小块数据，这里是会话本身。
    # 子 agent 派发要读 `depth`（判断还能不能派）与 `id`（父会话），
    # 读任务状态拿不到这两样。
    session_argument: ClassVar[str] = ""
    # 收证据收集器的参数名。没有这类参数时留空。
    #
    # 与上两个同样分开：收集器是循环层每轮新建的一个小盒子，工具往里放
    # 「这一步值得留住的材料」（检索回来的记忆之类），由装配器放进 EVIDENCE 层。
    # 只调它的 `add` 方法，不认它的类型，所以这一层不必导入上下文模块。
    evidence_argument: ClassVar[str] = ""
    # 收本地结构服务状态的参数名。没有这类参数时留空。
    #
    # 又一个分开的口子：它是**应用级**的东西（整个进程一个），不是会话级的，
    # 也不是每轮重建的。取登记表的那些工具收不到它，收它的只有一个 open_viewer。
    # 只读它的 `port` / `web_port` 属性，不认它的类型，这一层因此不必导入 web 包。
    viewer_argument: ClassVar[str] = ""
    # 收子 agent 运行时的参数名。没有这类参数时留空。
    #
    # 又一个分开的口子：它是**应用级**的（整个进程一个 AgentHost），
    # 由循环层持有、在调用时注入。派发工具用它 `spawn`，查询工具用它
    # `children_of` / `wait`。工具只调这几个方法，不认它的类型，
    # 这一层因此不必导入 host。
    spawn_argument: ClassVar[str] = ""

    @abstractmethod
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """执行一次工具调用。"""

    async def execute(
        self,
        arguments: dict[str, Any],
        context: Any = None,
        task_state: Any = None,
        session: Any = None,
        evidence: Any = None,
        viewer: Any = None,
        spawner: Any = None,
        on_arguments: Callable[[dict[str, Any]], None] | None = None,
    ) -> ToolResult:
        """内核唯一的调用入口：注入 context、任务状态、证据收集器与结构服务之后再走 `run`。

        **注入放在这一层而不是各工具内部**：注册表里那五个文件工具不需要 context，
        让它们各自记得「有人会传 context 进来」是多余的负担。这里统一按
        `context_argument` / `task_argument` / `evidence_argument` / `viewer_argument`
        决定要不要塞，工具作者只写自己真正用到的东西。

        声明了某一类参数、而这一类又是 None 时**报装配问题**而不是抛参数错：
        这一路只可能是内核漏传，模型改自己的参数救不回来。

        `on_arguments` 是给循环层的一个回执：**注入之前**那份参数（也就是模型
        （或用户）实际给出来的），交给它记下来。界面与网页端看到的必须还是模型
        给的那一份——把登记表这类内核对象塞进 `Part.tool_input`，同一份调用在
        两个入口就会显示成两种样子。
        """
        if on_arguments is not None:
            on_arguments(dict(arguments))
        for name, value, what in (
            (self.context_argument, context, "会话登记表"),
            (self.task_argument, task_state, "任务状态"),
            (self.session_argument, session, "当前会话"),
            (self.evidence_argument, evidence, "证据收集器"),
            (self.viewer_argument, viewer, "本地结构服务"),
            (self.spawn_argument, spawner, "子 agent 派发器"),
        ):
            if not name:
                continue
            if value is None:
                return ToolResult(
                    output=(
                        f"内核装配问题：{self.spec.name} 需要{what}，"
                        "但这次调用没有拿到。这是程序侧的问题，不是参数写错，重试无用。"
                    ),
                    failed=True,
                )
            arguments = {**arguments, name: value}
        return await self.run(arguments)


class ToolRegistry:
    """工具登记表，按名字查找。

    `disabled` 里的工具**根本不进来**：不装进注册表，也就不在 `specs()` 给出的
    工具清单里，模型看不到它。这与「装进来但调用时拒绝」是两件事——
    后者模型会一直重试，上下文里攒下一串失败记录；前者它连这个工具存在都不知道。

    禁用清单的读法见 `permission.load_disabled`，配置在 `.opencode_tui/tools.toml`，
    应用里用 `/tools` 面板改（改完由 `app.toggle_tool` 重装注册表，不必重启）。
    这里只负责一件事：把清单里那些名字挡在门外。放在注册表而不是装配处，
    是因为装配处有八次 `register` 调用，逐个加判断会漏；一个入口收口最省事。
    """

    def __init__(self, disabled: frozenset[str] | set[str] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.disabled: frozenset[str] = frozenset(disabled or ())

    def register(self, tool: Tool) -> None:
        """登记一个工具；它在禁用清单里时不登记，名字重复时直接抛错。"""
        name = tool.spec.name
        if name in self.disabled:
            return
        if name in self._tools:
            raise ValueError(f"工具名字重复：{name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        """按名字取工具，没有登记时返回 None。"""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """已装工具的名字，按装配顺序。"""
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        """全部工具的参数模式，直接作为请求体的 tools 字段。"""
        return [tool.spec.to_openai() for tool in self._tools.values()]


_TYPE_CHECKS: dict[str, tuple[type, str]] = {
    "string": (str, "字符串"),
    "array": (list, "数组"),
    "object": (dict, "对象"),
    "boolean": (bool, "真假值"),
}


def argument_error(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """按参数模式检查必填项、类型、取值范围与嵌套结构，返回错误说明，合格时返回空字符串。

    模型给出的参数不合格时，这条说明会被写成工具结果回灌，是它下一轮自我纠正的输入。
    """
    return _check_object(spec.parameters, arguments)


def _check_object(schema: dict[str, Any], arguments: dict[str, Any]) -> str:
    """检查一层对象的必填项、未知键与每个已知键。"""
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in arguments:
            return f"缺少参数 {name}"
    if schema.get("additionalProperties") is False:
        for name in arguments:
            if name not in properties:
                return f"参数 {name} 不在允许的范围内"
    for name, value in arguments.items():
        declared = properties.get(name)
        if not declared:
            continue
        error = _check_value(name, declared, value)
        if error:
            return error
    return ""


def _check_value(name: str, declared: dict[str, Any], value: Any) -> str:
    """检查一个已知参数的值。没有声明类型的模式不检查这一项。"""
    kinds = declared.get("type")
    types = [kind for kind in (kinds if isinstance(kinds, list) else [kinds]) if kind]
    if types:
        expected = _expected_type(types, value)
        if expected:
            return f"参数 {name} 应当是{expected}"
    choices = declared.get("enum")
    if choices and value not in choices:
        return f"参数 {name} 只能是 {choices} 之一"
    if declared.get("type") == "object" and isinstance(value, dict):
        return _check_object(declared, value)
    if declared.get("type") == "array" and isinstance(value, list):
        items = declared.get("items") or {}
        for index, item in enumerate(value):
            error = _check_value(f"{name}[{index}]", items, item)
            if error:
                return error
    return ""


def _expected_type(types: list[Any], value: Any) -> str:
    """值不符合声明的类型时返回类型的说法，符合时返回空字符串。"""
    if "null" in types and value is None:
        return ""
    for type_name in types:
        if type_name != "null" and _match(type_name, value):
            return ""
    return _describe(types[0])


def _match(type_name: Any, value: Any) -> bool:
    """一个值是否属于声明的那一种类型。"""
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    check = _TYPE_CHECKS.get(type_name)
    if check is None:
        return True
    expected, _ = check
    return isinstance(value, expected)


def _describe(type_name: Any) -> str:
    """把一种声明类型说成一句话里的说法。"""
    if type_name == "integer":
        return "整数"
    if type_name == "number":
        return "数字"
    check = _TYPE_CHECKS.get(type_name)
    return check[1] if check else str(type_name)


def terminate_tree(process: asyncio.subprocess.Process) -> None:
    """终止一条命令以及它自己再起的那些子进程。

    shell 会再起一层子进程，只结束 shell 那一个进程的话，真正干活的那个会留下来。
    Windows 有 `taskkill /T` 能把整棵树带走；POSIX 没有等价的单条命令，所以
    **起进程时就把它放进自己的会话**（`start_new_session=True`，见 `BashTool.run`），
    这里按进程组杀。不建会话的话 `kill()` 只杀掉 shell，孙子进程会被 init 收养、
    继续跑完并写出它那一步的产物——取消看上去生效了，实际上没有。
    """
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        # 建会话之后 pgid 等于 pid，所以拿 pid 当进程组号。
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # 没有以 pid 命名的进程组：这条进程不是按 `start_new_session` 起的
        # （外部调用方漏了那个开关）。退回只杀它自己，至少不让它占着管道继续跑。
        process.kill()
    except OSError:
        process.kill()


def release_pipes(process: asyncio.subprocess.Process) -> None:
    """把子进程那一侧的管道收掉。

    命令是被终止的时候，读不一定走到结尾，管道会留到事件循环关掉之后才被回收，
    那一刻由析构函数报出"没有关闭的传输"。
    """
    transport = process._transport
    if transport is not None:
        transport.close()


# 解码子进程输出时按顺序试的编码。
#
# 为什么不是一个 UTF-8 了事：Windows 上命令解释器的输出走的是**活动代码页**，
# 简体中文环境里是 936（GBK）。`certutil`、`dir /T:W`、`ping`、`where` 这些
# 自带中文提示的命令吐出来的是 GBK 字节，按 UTF-8 解会得到一串 U+FFFD
# （现场：`.probe` 里 `ses_0031` 的 `certutil -hashfile` 那一条，
# 「哈希:」「命令成功完成。」整段变成 ``）。
#
# 顺序是「先严后宽」：UTF-8 严格解码不轻易成功，只有真 UTF-8 才走得通；
# 失败再试 GBK。这样纯 ASCII 输出两种都过（等价），而含中文的输出各归各位。
# 最后仍然失败时用 UTF-8 + replace 兜底，保证这一层不会因为编码把命令结果整个丢掉。
OUTPUT_ENCODINGS = ("utf-8", "gbk", "cp936", "big5")


def decode_output(raw: bytes) -> str:
    """把子进程输出的字节解成文本。

    逐种编码**严格**试一遍，第一种解得通的就用。全试不通时退回
    UTF-8 + `replace`：宁可留几个替换字符，也不能让调用方拿不到任何输出——
    工具输出是模型下一步的依据，抛异常等于把这条命令的结果整条抹掉。
    """
    for encoding in OUTPUT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 字节序标记与它对应的编码。顺序**从长到短**：UTF-32 的标记前两个字节与 UTF-16 一样，
# 短的排在前面会把 UTF-32 认成 UTF-16，解出来满屏 NUL。
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(raw: bytes) -> str | None:
    """把文件字节解成文本；确实不是文本时返回 None。

    为什么要先看字节序标记：Windows 上 `cmd` 的 `>` 重定向写出的是**带标记的 UTF-16**，
    按 UTF-8 解必然失败——而那种文件恰恰是模型自己刚重定向出来的，于是它拿到
    「不是文本文件」之后只好改用 bash 一段段打印，白花好几步
    （现场：`ses_0037` 第 16~22 步，七步里六步耗在这上面）。

    标记认不出来时，再按 `OUTPUT_ENCODINGS` 逐种**严格**试。试之前先挡掉二进制：
    没有标记又夹着 NUL 字节的直接判成不是文本，免得 GBK 那种宽松编码把二进制
    解成一片乱码交出去——那比说一句「读不出来」更误导。

    **换行要归一成 `\\n`**：这是 `Path.read_text` 的通用换行语义，改用 `read_bytes`
    之后必须自己补回来。Windows 上 `write_text` 会把 `\\n` 写成 `\\r\\n`，不归一的话
    `\\r` 就留在每行末尾——`endswith("丙")` 这类判断会莫名其妙地失配
    （改这一处时踩到过：`check_file_tools` 里「文件里的三行都读出来」当场报出来）。
    """
    for mark, encoding in _BOMS:
        if raw.startswith(mark):
            try:
                return _normalize_newlines(raw.decode(encoding))
            except UnicodeDecodeError:
                return None
    if b"\x00" in raw[:8192]:
        return None
    for encoding in OUTPUT_ENCODINGS:
        try:
            return _normalize_newlines(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return None


def _normalize_newlines(text: str) -> str:
    """三种换行都归一成 `\\n`，与 `Path.read_text` 的通用换行一致。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class BashTool(Tool):
    """执行一条 shell 命令，把标准输出与标准错误合并作为结果。

    中断生成时协程会在等待子进程的那一处被取消，两个出口都把整棵进程树收掉。
    """

    spec = ToolSpec(
        name="bash",
        description="执行一条 shell 命令，返回合并后的标准输出与标准错误。",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的命令"}},
            "required": ["command"],
        },
    )
    primary_argument = "command"

    def __init__(self, cwd: Path | None = None, timeout: float = BASH_TIMEOUT) -> None:
        self.cwd = cwd
        self.timeout = timeout

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """执行命令。非零退出码不算异常，它和输出一起交给模型判断。"""
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("缺少参数 command")
        # POSIX 上让子进程自成会话，`terminate_tree` 才能按进程组把孙子进程一起收掉。
        # Windows 不支持这个开关，那边靠 taskkill /T。
        extra: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.cwd) if self.cwd else None,
            **extra,
        )
        stdout = await self._collect(process)
        output = decode_output(stdout).strip()
        return ToolResult(output=output or "（命令没有输出）", failed=process.returncode != 0)

    async def _collect(self, process: asyncio.subprocess.Process) -> bytes:
        """等命令结束并收下它的输出。超时与被取消都把整棵进程树收掉，再原样报出去。

        收输出这一步单独放在一个任务里，外面那层等它时被取消不会连带取消它：它被取消的话
        读不到进程结束，管道要留到事件循环关闭之后才回收，那时由析构函数报出没关的传输。
        """
        collect = asyncio.ensure_future(process.communicate())
        try:
            stdout, _ = await asyncio.wait_for(asyncio.shield(collect), self.timeout)
        except TimeoutError as error:
            terminate_tree(process)
            await collect
            raise ToolError(
                f"命令超过 {self.timeout:g} 秒没有结束，已经终止；"
                "长任务请分小段跑或放到后台，再回来查结果"
            ) from error
        except asyncio.CancelledError:
            terminate_tree(process)
            await collect
            raise
        finally:
            release_pipes(process)
        return stdout


class ReadTool(Tool):
    """读一个文本文件，输出带绝对行号。

    目录、二进制文件、不存在的路径都返回说清楚原因的失败，而不是抛异常：
    模型看到原因之后会自己换个路径再试。
    """

    spec = ToolSpec(
        name="read",
        description=(
            "读取一个文本文件的内容，每行前面带文件里的绝对行号。"
            "用 offset 与 limit 读取其中一段，文件很长时先读开头再决定往下看哪里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对路径按工作目录解释"},
                "offset": {"type": "integer", "description": "从第几行开始读，从 1 数起"},
                "limit": {"type": "integer", "description": "最多读多少行"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    primary_argument = "path"

    def __init__(self, cwd: Path | None = None, limit: int = READ_LIMIT) -> None:
        self.cwd = cwd
        self.limit = limit

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """读出文件内容。"""
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("缺少参数 path")
        path = resolve_path(self.cwd, raw)
        if not path.exists():
            log_read(path, purpose="tool.read", ok=False, detail="文件不存在")
            return ToolResult(output=f"文件不存在：{path}", failed=True)
        if path.is_dir():
            log_read(path, purpose="tool.read", ok=False, detail="是目录")
            return ToolResult(output=f"{path} 是目录，读它需要给出具体的文件名", failed=True)
        offset = arguments.get("offset")
        if offset is None:
            offset = 1
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
            raise ToolError("参数 offset 应当是大于 0 的整数")
        limit = arguments.get("limit")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ToolError("参数 limit 应当是大于 0 的整数")
        if limit is None:
            limit = self.limit
        try:
            blob = path.read_bytes()
        except OSError as error:
            log_read(path, purpose="tool.read", ok=False, detail=f"{type(error).__name__}：{error}")
            return ToolResult(output=f"读 {path} 出错：{error}", failed=True)
        text = decode_text(blob)
        if text is None:
            log_read(path, purpose="tool.read", ok=False, size=len(blob), detail="不是文本文件")
            return ToolResult(output=f"{path} 不是文本文件，读不出来", failed=True)
        log_read(path, purpose="tool.read", size=len(blob))
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        if offset > total and total:
            return ToolResult(
                output=f"{path} 只有 {total} 行，读不到第 {offset} 行", failed=True
            )
        start = offset - 1
        window = lines[start : start + limit]
        if not window:
            return ToolResult(output="")
        body = number_lines("\n".join(window), start=offset)
        suffix = ""
        if start + len(window) < total:
            suffix = f"\n（后面还有 {total - start - len(window)} 行，接着读用 offset={start + len(window) + 1}）"
        return ToolResult(output=body + suffix)


class WriteTool(Tool):
    """把内容写进一个文件，父目录不存在时一并建出来。

    整份覆盖。只想改一处时用 patch。
    """

    spec = ToolSpec(
        name="write",
        description=(
            "把一个文件的全部内容写成给出的文本，原有的内容会被整份替换掉。"
            "父目录不存在时会自动建出来。只改其中一处时用 patch 更省事。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对路径按工作目录解释"},
                "content": {"type": "string", "description": "写进去的完整内容"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )
    primary_argument = "path"

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """写入文件。"""
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("缺少参数 path")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ToolError("缺少参数 content")
        path = resolve_path(self.cwd, raw)
        if path.is_dir():
            return ToolResult(output=f"{path} 是目录，写不进去", failed=True)
        before = ""
        existed = path.exists()
        if existed:
            try:
                before = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                before = ""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as error:
            return ToolResult(output=f"写 {path} 出错：{error}", failed=True)
        added, removed = change_counts(before, content)
        rows = count_lines(content)
        action = "改写" if existed else "新建"
        return ToolResult(
            output=f"已{action} {path}，现在有 {rows} 行",
            added=added,
            removed=removed,
        )


class PatchTool(Tool):
    """把文件里唯一出现的一段文字换成另一段。

    命中零处或多处都算失败：改错地方比不改更麻烦，模型应当给出更长的一段把位置定下来。
    """

    spec = ToolSpec(
        name="patch",
        description=(
            "把一个文件里的一段文字换成另一段。old 必须在文件里唯一出现，"
            "出现零次或多次都会失败；失败时把 old 写长一些，带上前后几行把位置定下来。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对路径按工作目录解释"},
                "old": {"type": "string", "description": "要被替换掉的那段文字，要在文件里唯一出现"},
                "new": {"type": "string", "description": "换上去的那段文字，写空字符串表示删掉这一段"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    )
    primary_argument = "path"

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """替换文件里的一段文字。"""
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("缺少参数 path")
        old = arguments.get("old")
        if not isinstance(old, str) or not old:
            raise ToolError("缺少参数 old")
        new = arguments.get("new")
        if not isinstance(new, str):
            raise ToolError("缺少参数 new")
        path = resolve_path(self.cwd, raw)
        if not path.exists():
            return ToolResult(output=f"文件不存在：{path}", failed=True)
        if path.is_dir():
            return ToolResult(output=f"{path} 是目录，改不了", failed=True)
        try:
            blob = path.read_bytes()
        except OSError as error:
            return ToolResult(output=f"读 {path} 出错：{error}", failed=True)
        before = decode_text(blob)
        if before is None:
            return ToolResult(output=f"{path} 不是文本文件，改不了", failed=True)
        occurrences = before.count(old)
        if occurrences == 0:
            return ToolResult(
                output=f"在 {path} 里没有找到 old 那段文字，用 read 看清原文再改",
                failed=True,
            )
        if occurrences > 1:
            return ToolResult(
                output=f"old 在 {path} 里出现了 {occurrences} 处，把 old 写长一些只命中一处",
                failed=True,
            )
        after = before.replace(old, new, 1)
        try:
            path.write_text(after, encoding="utf-8")
        except OSError as error:
            return ToolResult(output=f"写 {path} 出错：{error}", failed=True)
        added, removed = change_counts(old, new)
        rows = count_lines(after)
        return ToolResult(
            output=f"已改 {path}，这次 +{added} -{removed}，现在有 {rows} 行",
            added=added,
            removed=removed,
        )


class GrepTool(Tool):
    """按正则在一棵目录里找内容，只列出命中的行。

    默认只看代码与常见文本后缀，跳过 .git 之类的目录；
    glob 参数给出时以它为准，那时不再按后缀过滤。
    """

    spec = ToolSpec(
        name="grep",
        description=(
            "按正则表达式在一棵目录里搜索文件内容，输出命中的文件、行号与那一行。"
            "path 可以是目录也可以是单个文件；glob 用来限定文件名，"
            "不给出时只搜代码与常见文本后缀。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "要搜的正则表达式"},
                "path": {"type": "string", "description": "搜哪棵目录或哪个文件，默认当前工作目录"},
                "glob": {"type": "string", "description": "文件名通配，例如 *.py"},
                "ignore_case": {"type": "boolean", "description": "是否不分大小写，默认分"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )
    primary_argument = "pattern"

    def __init__(
        self,
        cwd: Path | None = None,
        limit: int = GREP_LIMIT,
        file_limit: int = GREP_FILE_LIMIT,
    ) -> None:
        self.cwd = cwd
        self.limit = limit
        self.file_limit = file_limit

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """搜一趟目录。"""
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("缺少参数 pattern")
        flags = re.IGNORECASE if arguments.get("ignore_case") else 0
        try:
            matcher = re.compile(pattern, flags)
        except re.error as error:
            return ToolResult(output=f"正则表达式写错了：{error}", failed=True)
        raw = arguments.get("path")
        if raw is None:
            root = self.cwd or Path.cwd()
        elif not isinstance(raw, str) or not raw.strip():
            raise ToolError("参数 path 应当是字符串")
        else:
            root = resolve_path(self.cwd, raw)
        if not root.exists():
            return ToolResult(output=f"路径不存在：{root}", failed=True)
        glob = arguments.get("glob")
        if glob is not None and (not isinstance(glob, str) or not glob.strip()):
            raise ToolError("参数 glob 应当是非空的字符串")
        files = [root] if root.is_file() else self._walk(root, glob)
        matches: list[str] = []
        hit_files = 0
        scanned = 0
        truncated = False
        for path in files:
            if len(matches) >= self.limit:
                truncated = True
                break
            scanned += 1
            found = self._scan(path, matcher)
            if not found:
                continue
            hit_files += 1
            if hit_files > self.file_limit:
                truncated = True
                break
            matches.extend(found[: self.limit - len(matches)])
        # 一次 grep 会读很多文件，逐个记会把日志淹掉；这里只记一条汇总，
        # 看得到「搜了哪儿、扫了几个、命中几个」就够判断读取有没有发生。
        log_read(
            root,
            purpose="tool.grep",
            detail=f"扫描 {scanned} 个文件，命中 {hit_files} 个",
        )
        if not matches:
            return ToolResult(output=f"没有找到匹配 {pattern} 的行")
        tail = "（命中太多，只列出前面的）" if truncated else ""
        return ToolResult(output="\n".join(matches) + (f"\n{tail}" if tail else ""))

    def _walk(self, root: Path, glob: str | None) -> list[Path]:
        """列出一棵目录里该搜的文件。"""
        pattern = glob or "*"
        if glob:
            candidates = sorted(root.rglob(pattern))
        else:
            candidates = sorted(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in CODE_EXTENSIONS
            )
        return [
            path
            for path in candidates
            if path.is_file() and not _skip_path(path, root)
        ]

    def _scan(self, path: Path, matcher: re.Pattern[str]) -> list[str]:
        """扫一个文件，给出 `路径:行号:内容` 形式的命中行。"""
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        found: list[str] = []
        for number, line in enumerate(text.split("\n"), start=1):
            if matcher.search(line):
                found.append(f"{path}:{number}:{line.strip()}")
        return found


SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".idea", ".vscode",
}


def _skip_path(path: Path, root: Path) -> bool:
    """路径里只要有一层是会被跳过的目录名，就跳过它。"""
    try:
        parts = path.relative_to(root if root.is_dir() else root.parent).parts
    except ValueError:
        parts = path.parts
    return any(part in SKIP_DIRECTORIES for part in parts[:-1])

