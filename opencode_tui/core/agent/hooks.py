from __future__ import annotations

import asyncio
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permission import CONFIG_DIRECTORY
from .tools import release_pipes, terminate_tree

HOOKS_FILE = "hooks.toml"

# 三个事件。名字与外部命令的约定一致，钩子脚本按 event 字段分辨自己被叫来做什么
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, STOP)

# 一条钩子最多跑这么久，超过就收掉整棵进程树，并按拦下来处理
HOOK_TIMEOUT = 30.0
# 这个退出码表示拦截，标准错误是原因
BLOCK_CODE = 2


@dataclass(frozen=True)
class Hook:
    """一条钩子：在哪个事件上、管哪些工具、跑什么命令。"""

    event: str
    matcher: str
    command: str
    source: str

    def matches(self, tool: str) -> bool:
        """这条钩子管不管这一次调用。

        `matcher` 是工具名的正则，`*` 表示全部；Stop 事件没有工具，一律算命中。
        """
        if self.event == STOP or self.matcher in ("", "*"):
            return True
        return re.fullmatch(self.matcher, tool) is not None


@dataclass(frozen=True)
class HookOutcome:
    """一条钩子跑完之后的结果。"""

    command: str
    source: str
    code: int
    blocked: bool
    reason: str


@dataclass(frozen=True)
class HookReport:
    """一个事件上全部钩子的结果。第一条拦下来的就是结论，后面的不再跑。"""

    outcomes: tuple[HookOutcome, ...] = ()

    @property
    def blocked(self) -> bool:
        """这一次是不是被拦下来了。"""
        return any(outcome.blocked for outcome in self.outcomes)

    @property
    def reason(self) -> str:
        """拦下来的原因，来自那条钩子的标准错误。"""
        for outcome in self.outcomes:
            if outcome.blocked:
                return outcome.reason
        return ""


class HookRunner:
    """按事件跑外部命令。

    标准输入收到一行 JSON：

        {"event": "PreToolUse", "tool": "bash", "input": {"command": "ls"}, "cwd": "D:\\..."}

    三个事件的这一行形状一致，Stop 没有工具，`tool` 是空字符串、`input` 是空表，
    钩子脚本读 `data["tool"]` 不会因为键不存在而报错。

    退出码 0 放行；退出码 2 拦截，标准错误当原因；其余退出码记进日志但放行。
    超过 `HOOK_TIMEOUT` 没有结束的按拦下来处理：一条跑不完的钩子什么都没验过，
    放行等于把它当成通过。
    """

    def __init__(
        self,
        hooks: list[Hook] | None = None,
        cwd: Path | None = None,
        timeout: float = HOOK_TIMEOUT,
    ) -> None:
        self.hooks = list(hooks or [])
        self.cwd = cwd or Path.cwd()
        self.timeout = timeout

    def for_event(self, event: str, tool: str = "") -> list[Hook]:
        """这个事件上要跑的全部钩子，按配置顺序。"""
        return [
            hook for hook in self.hooks if hook.event == event and hook.matches(tool)
        ]

    async def run(
        self,
        event: str,
        tool: str = "",
        tool_input: dict[str, Any] | None = None,
    ) -> HookReport:
        """跑一个事件上的钩子，遇到第一条拦下来的就停。"""
        outcomes: list[HookOutcome] = []
        for hook in self.for_event(event, tool):
            code, stderr = await self._invoke(hook, event, tool, tool_input or {})
            reason = stderr.strip()
            if code == BLOCK_CODE:
                if not reason:
                    reason = f"{hook.command} 以退出码 2 结束，但没有在标准错误里写明原因"
                outcomes.append(HookOutcome(hook.command, hook.source, code, True, reason))
                break
            outcomes.append(HookOutcome(hook.command, hook.source, code, False, reason))
        return HookReport(tuple(outcomes))

    async def _invoke(
        self,
        hook: Hook,
        event: str,
        tool: str,
        tool_input: dict[str, Any],
    ) -> tuple[int, str]:
        """跑一条钩子，返回退出码与标准错误。

        收输出这一步单独放在一个任务里，外面那层等它时被取消不会连带取消它：
        它被取消的话读不到进程结束，管道要留到事件循环关闭之后才回收。
        """
        payload = json.dumps(
            {"event": event, "tool": tool, "input": tool_input, "cwd": str(self.cwd)},
            ensure_ascii=False,
        )
        process = await asyncio.create_subprocess_shell(
            hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
        )
        collect = asyncio.ensure_future(
            process.communicate(payload.encode("utf-8") + b"\n")
        )
        try:
            _, stderr = await asyncio.wait_for(asyncio.shield(collect), self.timeout)
        except TimeoutError:
            terminate_tree(process)
            await collect
            return BLOCK_CODE, f"{hook.command} 超过 {self.timeout:g} 秒没有结束，已经终止"
        finally:
            release_pipes(process)
        return process.returncode or 0, stderr.decode("utf-8", errors="replace")


def load_hooks(path: Path, source: str) -> list[Hook]:
    """从一份配置里读钩子，文件不存在时返回空表。

    认不出来的事件名与键直接抛错：钩子写错一个字母之后静默不跑，
    比启动时报错难查得多。
    """
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    entries = data.get("hook")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"{path} 里 hook 应当是一张张表组成的数组")
    hooks: list[Hook] = []
    for index, table in enumerate(entries, start=1):
        if not isinstance(table, dict):
            raise ValueError(f"{path} 里第 {index} 条 hook 应当是一张表")
        event = table.get("event")
        if event not in EVENTS:
            raise ValueError(
                f"{path} 里第 {index} 条 hook 的 event 是 {event!r}，只认 {list(EVENTS)}"
            )
        command = table.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{path} 里第 {index} 条 hook 缺少 command")
        matcher = table.get("matcher", "*")
        if not isinstance(matcher, str):
            raise ValueError(f"{path} 里第 {index} 条 hook 的 matcher 应当是字符串")
        for key in table:
            if key not in ("event", "matcher", "command"):
                raise ValueError(f"{path} 里第 {index} 条 hook 有不认得的键 {key}")
        hooks.append(Hook(str(event), matcher, command, source))
    return hooks


def default_hooks_paths(root: Path) -> tuple[Path, Path]:
    """项目钩子配置与用户钩子配置的位置。"""
    return (
        root / CONFIG_DIRECTORY / HOOKS_FILE,
        Path.home() / CONFIG_DIRECTORY / HOOKS_FILE,
    )
