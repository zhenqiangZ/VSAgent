from __future__ import annotations

import fnmatch
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .tools import Tool

# 项目配置所在目录，与状态文件的位置惯例一致
CONFIG_DIRECTORY = ".opencode_tui"
PERMISSIONS_FILE = "permissions.toml"
# 工具开关所在的文件。与权限规则分开放：权限管「调用时放不放行」，
# 开关管「装配时装不装进来」——后者决定工具在不在请求体的 tools 数组里。
TOOLS_FILE = "tools.toml"
# 默认策略下要问一次的工具：会改动文件或运行命令的那些
DEFAULT_ASK = frozenset({"write", "patch", "bash"})
# Plan 方式下一律拒绝的工具。一条命令会不会改动看不出来，所以 bash 也算在里面
#
# 领域工具里只有 prepare_receptor 与 write_run 进这一档：它们会往磁盘上写一组产物
# （结构、引擎参数、体检报告 / 运行目录）并登记成新条目——Plan 方式是「只看不改」，
# 这两步不该在它下面跑。
# 其余领域工具都不进：浏览登记表、查 UniProt / ChEMBL、取结构、搜索、开结构页
# 都是读或取；取结构固然会写盘，但那是**把远端的东西拿回来**（下载缓存），
# 与「改动用户的工程」不是一回事；把它们一并挡掉会让 Plan 方式连查资料都做不到。
PLAN_DENY = frozenset({"write", "patch", "bash", "prepare_receptor", "write_run"})

# 收「路径类」参数的工具。这几个工具的调用会指向磁盘的某个具体位置，
# 因此可以拿它的参数去判「这一步是不是踩到了工作目录之外」。
# 参数名参照各工具的 `primary_argument`，一个工具可能有多个可选参数。
PATH_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "read": ("path", "filePath"),
    "write": ("path", "filePath"),
    "patch": ("path", "filePath"),
    "grep": ("path", "pattern"),
    "import_structure": ("path",),
    "prepare_receptor": ("path",),
}

# 从 bash 命令里挑绝对路径用的模式。
#
# 只认「明确的绝对路径」：盘符开头（`C:\`、`D:/`）、UNC（`\\主机\共享`）、
# 以及 POSIX 的 `/...`。**不认域名与 URL**——`https://www.ebi.ac.uk/...`
# 里的 `//` 会被误当成 UNC，所以先把 URL 剔掉再找。
#
# POSIX 那一条要求至少两级（`/a/b`），一级的 `/T`、`/S` 是 Windows 命令的开关，
# 不是路径。`dir /T:W`、`ping -n 2` 这类写法在真命令里非常常见，
# 放宽成「斜杠开头即路径」会让每一条带开关的命令都被判成越界。
_ABSOLUTE_PATH = re.compile(
    r"""(?P<path>
          [A-Za-z]:[\\/][^\s"'|&;<>()]*          # C:\... 或 C:/...
        | \\\\[^\s"'|&;<>()]+                    # \\主机\共享
        | /[^\s"'|&;<>():/]+/[^\s"'|&;<>():]*    # /一级/... 至少两级
    )""",
    re.VERBOSE,
)
# URL 先剔掉：这套工具面向的领域（UniProt / ChEMBL / PDB）几乎每一步都在打 URL，
# 不剔的话每一条命令都会被判成越界。
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")

# 判「在工作目录之外」时要放过的前缀。
#
# 这些是「本来就不属于用户工程」的位置：临时目录、Windows 目录、程序自己的
# 数据目录、用户的个人配置目录。模型经常要往临时目录写中间文件，
# 也要读自己下载到 `~/.opencode_tui` 下的结构与靶点，这些都算正常动作。
# 不放过它们的话，越界询问会响得太频繁，用户很快就会条件反射地一直按 y。
_ESCAPE_PREFIXES: tuple[str, ...] = (
    os.environ.get("TEMP", ""),
    os.environ.get("TMP", ""),
)


def _escape_prefixes() -> list[Path]:
    """要放过的工作目录之外的前缀，都是绝对路径。"""
    home = Path.home()
    candidates = [
        *_ESCAPE_PREFIXES,
        str(home / CONFIG_DIRECTORY),
        str(home / "AppData" / "Local" / "Temp"),
        os.environ.get("WINDIR", "C:\\Windows"),
        os.environ.get("SystemRoot", ""),
    ]
    out: list[Path] = []
    for item in candidates:
        if not item:
            continue
        try:
            out.append(Path(item).resolve())
        except (OSError, ValueError):
            continue
    return out


def paths_outside(command_or_path: str, root: Path) -> list[str]:
    """挑出这段文本或这条命令里位于工作目录之外的路径。

    返回的是原样摘出来的路径片段（给弹窗展示），没有越界时返回空表。
    判定只做到「这一段里出现过工作目录之外的绝对路径」，
    不去解析命令要在哪个目录里跑——`cd` 之后再操作相对路径这种情形
    交给 bash 自己的行为去暴露，这里管的是最直白也最危险的那一类：
    路径本身就写着要动哪。
    """
    try:
        base = root.resolve()
    except (OSError, ValueError):
        return []
    allowed = [base, *_escape_prefixes()]
    stripped = _URL.sub(" ", command_or_path)
    found: list[str] = []
    seen: set[str] = set()
    for match in _ABSOLUTE_PATH.finditer(stripped):
        raw = match.group("path").rstrip("\\/,.")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            target = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if any(target == item or item in target.parents for item in allowed):
            continue
        found.append(raw)
    return found


class Verdict(Enum):
    """一次判定的结论。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionRule:
    """一条判定规则：工具名、参数模式与判定。"""

    tool: str
    pattern: str
    verdict: Verdict
    source: str

    def matches(self, tool: str, value: str) -> bool:
        """这条规则管不管这一次调用。"""
        return self.tool == tool and fnmatch.fnmatchcase(value, self.pattern)

    def describe(self) -> str:
        """给界面与状态文件看的一句话。"""
        return f"{self.source}：{self.tool} {self.pattern}"


@dataclass(frozen=True)
class PermissionDecision:
    """一次判定的结论，以及得出它的依据。"""

    verdict: Verdict
    rule: str = ""
    reason: str = ""

    def note(self) -> str:
        """记进 Part 的那一句话。"""
        return self.rule or self.reason


class PermissionPolicy:
    """按几级来源判定一次工具调用能不能执行。

    顺序从高到低：已中断、Plan 方式、项目配置里的拒绝、项目配置里的询问、
    本会话全放行、工作目录之外的访问、本会话放行的工具、用户配置里的拒绝与询问、
    默认策略。命中一级就出结论。

    `explicit` 为用户直接敲出来的命令，那一类只查是否已中断：命令是他自己打的，
    再让他确认一遍自己刚打的东西是噪音。

    两级「本会话记住」的粒度不同，这是有意为之：

    - `remember_all`（弹窗上的 a）＝**这个会话里所有工具都放行**，之后什么都不问。
    - `remember`（按工具记）＝只放行这一个工具。

    两者都排在「工作目录之外的访问」**之前还是之后**是这个类的关键决定：
    越界询问排在前面，意味着即使用户按了 a，走出工作目录这一步仍然会问一次。
    这正是要的——「放行」是对**工具**的授权，不是对**磁盘任何位置**的授权。
    """

    def __init__(
        self,
        project_rules: list[PermissionRule] | None = None,
        user_rules: list[PermissionRule] | None = None,
        default_ask: frozenset[str] = DEFAULT_ASK,
        workspace_root: Path | None = None,
    ) -> None:
        self.project_rules = list(project_rules or [])
        self.user_rules = list(user_rules or [])
        self.default_ask = default_ask
        self.workspace_root = workspace_root
        # 本会话放行的工具名。用集合而不是规则表：粒度就是工具名，没有参数要匹配。
        self._session_tools: set[str] = set()
        # 本会话是否对所有工具放行（弹窗上按了 a）。
        self._allow_all = False

    def allow_all(self) -> None:
        """本会话对所有工具放行。弹窗上按 a 走这一条。"""
        self._allow_all = True

    @property
    def allows_all(self) -> bool:
        """本会话是不是已经全放行。界面据此换个提示。"""
        return self._allow_all

    def check(
        self,
        tool: Tool,
        arguments: dict[str, object],
        agent_name: str,
        explicit: bool = False,
        cancelled: bool = False,
    ) -> PermissionDecision:
        """判定一次调用。"""
        if cancelled:
            return PermissionDecision(Verdict.DENY, reason="已经中断")
        if explicit:
            return PermissionDecision(Verdict.ALLOW, reason="用户直接给出的命令")
        name = tool.spec.name
        if agent_name == "Plan" and name in PLAN_DENY:
            return PermissionDecision(
                Verdict.DENY,
                reason="当前是 Plan 方式，只能读文件与搜索，改动请交给 Build 方式执行",
            )
        value = argument_value(tool, arguments)
        for verdict in (Verdict.DENY, Verdict.ASK):
            hit = _first(self.project_rules, verdict, name, value)
            if hit is not None:
                return PermissionDecision(hit.verdict, rule=hit.describe())
        # 边界检查排在两级「本会话放行」之前：a 放行的是工具，不是磁盘。
        outside = self.outside_paths(tool, arguments)
        if outside:
            return PermissionDecision(
                Verdict.ASK,
                reason=f"这一步会动到工作目录之外：{outside[0]}",
            )
        if self._allow_all:
            return PermissionDecision(Verdict.ALLOW, rule="本会话已放行所有工具")
        if name in self._session_tools:
            return PermissionDecision(Verdict.ALLOW, rule=f"本会话不再问：{name}")
        for verdict in (Verdict.DENY, Verdict.ASK):
            hit = _first(self.user_rules, verdict, name, value)
            if hit is not None:
                return PermissionDecision(hit.verdict, rule=hit.describe())
        if name in self.default_ask:
            return PermissionDecision(Verdict.ASK, reason=f"{name} 会改动文件或运行命令")
        return PermissionDecision(Verdict.ALLOW)

    def outside_paths(self, tool: Tool, arguments: dict[str, object]) -> list[str]:
        """这次调用里位于工作目录之外的路径，没有越界时返回空表。

        没配工作目录时一律返回空表：那表示这一层管不着边界，
        不能让「没配置」变成「所有路径都算越界」而把每一步都拦下来。
        """
        if self.workspace_root is None:
            return []
        name = tool.spec.name
        if name == "bash":
            command = arguments.get("command")
            if not isinstance(command, str):
                return []
            return paths_outside(command, self.workspace_root)
        found: list[str] = []
        for key in PATH_ARGUMENTS.get(name, ()):
            raw = arguments.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            # 文件类工具的相对路径是相对**工作目录**解释的（装配时 cwd 就是它），
            # 所以这里先补成绝对路径再判：`..\..\Windows\win.ini` 这种写法
            # 光看字符串是相对的，补全之后才知道跑到了外面。
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.workspace_root / candidate
            found.extend(paths_outside(str(candidate), self.workspace_root))
        return found

    def remember(self, tool: Tool, arguments: dict[str, object]) -> PermissionRule:
        """把整个工具记进本会话的放行表，之后这个工具的任何调用都不再问。

        `arguments` 留着不用，但调用方仍要传：判定按哪个参数比对这件事曾经是
        `argument_value` 决定的，保留这个形状，将来若要回到按参数记不必改签名。
        弹窗展示的仍是**这一次的参数**（用户要看清自己放行的是哪条命令），
        记下的却是整个工具——这处不一致由弹窗文案交代。
        """
        del arguments
        name = tool.spec.name
        self._session_tools.add(name)
        return PermissionRule(name, "*", Verdict.ALLOW, "本会话记住")

    def clear_session(self) -> None:
        """清掉本会话放行的工具与全放行标记。切换会话时调用。"""
        self._session_tools.clear()
        self._allow_all = False


def argument_value(tool: Tool, arguments: dict[str, object]) -> str:
    """取出这条规则要比对的那个参数值。

    它同时也是询问弹窗上要展示的那一段：判定按哪个参数比对，
    用户就该看到哪个参数，两者必须是同一个。
    """
    if not tool.primary_argument:
        return ""
    raw = arguments.get(tool.primary_argument)
    return raw if isinstance(raw, str) else ""


def _first(
    rules: list[PermissionRule],
    verdict: Verdict,
    tool: str,
    value: str,
) -> PermissionRule | None:
    """按顺序找出第一条命中且判定相符的规则。"""
    for rule in rules:
        if rule.verdict is verdict and rule.matches(tool, value):
            return rule
    return None


def load_permissions(path: Path, source: str) -> list[PermissionRule]:
    """从一份配置里读规则，文件不存在时返回空表。

    认不出来的键直接抛错：把 deny 写错一个字母的话，这条规则会静默失效，
    那比启动时报错难查得多。
    """
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    rules: list[PermissionRule] = []
    for tool, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f"{path} 里 {tool} 这一段应当是一张表")
        for key, patterns in table.items():
            verdict = _verdict_of(key)
            if verdict is None:
                raise ValueError(f"{path} 里 {tool}.{key} 不是认得的判定，只认 deny、ask、allow")
            for pattern in patterns if isinstance(patterns, list) else [patterns]:
                rules.append(PermissionRule(tool, str(pattern), verdict, source))
    return rules


def _verdict_of(key: str) -> Verdict | None:
    """把配置里的一个键认成判定，认不出来的返回 None。"""
    for verdict in Verdict:
        if verdict.value == key:
            return verdict
    return None


def default_permission_paths(root: Path) -> tuple[Path, Path]:
    """项目配置与用户配置的位置。"""
    return (
        project_config_directory(root) / PERMISSIONS_FILE,
        Path.home() / CONFIG_DIRECTORY / PERMISSIONS_FILE,
    )


def project_config_directory(root: Path) -> Path:
    """项目侧配置目录：打包后跟着可执行文件走，否则跟着启动目录走。

    与 `env.env_file()` 同一套判断。不这样处理的话，配置只在「从仓库根目录启动」
    时才找得到——换个目录、或者把 exe 挪到别处双击，禁用清单就读不到了。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / CONFIG_DIRECTORY
    return root / CONFIG_DIRECTORY


def load_disabled(root: Path) -> tuple[frozenset[str], Path | None]:
    """读工具开关，返回「装配时不要装进来的工具名」与它的来源文件。

    与权限规则是两件事，这是本函数存在的全部理由：

    - **权限规则管调用**：工具照旧装进注册表、照旧出现在模型看到的工具清单里，
      只是模型真去调的时候被拦下来。模型会一直重试，上下文里会攒下一串失败记录。
    - **开关管装配**：工具根本不进注册表，也就不在请求体的 `tools` 数组里。
      模型看不到它，不会去想它，连失败的机会都没有。

    要彻底不让模型用某个工具时用这个，不要用 `permissions.toml` 里的 `deny`。

    读法（TOML 里一张 `[tools]` 表，键是工具名、值是真假值）：

        [tools]
        bash = false        # 不装 bash
        list_workspace = false

    值为真或键写错都不报错——**这份文件的语义是「关」，不是「配」**：
    没有把握的键按「没关」处理，比让整个应用起不来强。这与 `load_permissions`
    刻意相反，那边写错一个字母会让规则静默失效、更难查，所以选择启动即报错。

    项目配置与用户配置都读，按 set 收：**任一处说了关就关**。
    项目侧那份跟着可执行文件走（见 `project_config_directory`），
    所以 exe 挪到哪、从哪个目录启动都一样生效。
    返回的来源文件用于启动时打一行日志，让用户知道这条是哪儿来的。
    """
    names: set[str] = set()
    sources: list[Path] = []
    for path in (
        project_config_directory(root) / TOOLS_FILE,
        Path.home() / CONFIG_DIRECTORY / TOOLS_FILE,
    ):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            # 配置文件坏了不应连累启动：当作没读到，继续走。
            continue
        table = data.get("tools")
        if not isinstance(table, dict):
            continue
        found = [name for name, value in table.items() if value is False]
        if found:
            sources.append(path)
            names.update(found)
    return frozenset(names), (sources[0] if sources else None)
