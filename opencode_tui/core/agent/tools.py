from __future__ import annotations

import asyncio
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 120.0


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
    """一次工具执行的结果。失败时 output 里写的是原因。"""

    output: str = ""
    failed: bool = False


class ToolError(Exception):
    """工具内部可以预料到的失败，原文会被回灌给模型。"""


class Tool(ABC):
    """工具的基类。"""

    spec: ToolSpec

    @abstractmethod
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """执行一次工具调用。"""


class ToolRegistry:
    """工具登记表，按名字查找。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """登记一个工具，名字重复时直接抛错。"""
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"工具名字重复：{name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        """按名字取工具，没有登记时返回 None。"""
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        """全部工具的参数模式，直接作为请求体的 tools 字段。"""
        return [tool.spec.to_openai() for tool in self._tools.values()]


def argument_error(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """按参数模式检查必填项与已声明的类型，返回错误说明，合格时返回空字符串。

    模型给出的参数不合格时，这条说明会被写成工具结果回灌，是它下一轮自我纠正的输入。
    """
    properties = spec.parameters.get("properties") or {}
    for name in spec.parameters.get("required") or []:
        if name not in arguments:
            return f"缺少参数 {name}"
    for name, value in arguments.items():
        declared = properties.get(name)
        if not declared:
            continue
        if declared.get("type") == "string" and not isinstance(value, str):
            return f"参数 {name} 应当是字符串"
        if declared.get("type") == "array" and not isinstance(value, list):
            return f"参数 {name} 应当是数组"
    return ""


def terminate_tree(process: asyncio.subprocess.Process) -> None:
    """终止一条命令以及它自己再起的那些子进程。

    shell 会再起一层子进程，只结束 shell 那一个进程的话，真正干活的那个会留下来。
    """
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        process.kill()


def release_pipes(process: asyncio.subprocess.Process) -> None:
    """把子进程那一侧的管道收掉。

    命令是被终止的时候，读不一定走到结尾，管道会留到事件循环关掉之后才被回收，
    那一刻由析构函数报出"没有关闭的传输"。
    """
    transport = process._transport
    if transport is not None:
        transport.close()


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

    def __init__(self, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.cwd = cwd
        self.timeout = timeout

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """执行命令。非零退出码不算异常，它和输出一起交给模型判断。"""
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("缺少参数 command")
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.cwd) if self.cwd else None,
        )
        stdout = await self._collect(process)
        output = stdout.decode("utf-8", errors="replace").strip()
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
            raise ToolError(f"命令超过 {self.timeout:g} 秒没有结束，已经终止") from error
        except asyncio.CancelledError:
            terminate_tree(process)
            await collect
            raise
        finally:
            release_pipes(process)
        return stdout
