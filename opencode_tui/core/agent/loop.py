from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from ..event_bus import EventBus
from ..models import Message, Part, PartType, Session, ToolStatus
from .prompt import build_model_messages, build_system_prompt
from .provider import (
    ChatProvider,
    ProviderConfig,
    ProviderError,
    ProviderEventType,
)
from .tools import ToolError, ToolRegistry, argument_error
from .workspace import Workspace

# 一轮里最多问模型这么多次，防止模型在工具之间来回不收敛
MAX_STEPS = 24
# 可以重试的错误最多重发这么多次
MAX_RETRIES = 2
# 每次重发之前等多久
RETRY_DELAYS = (1.0, 3.0)


class AgentLoop:
    """一轮生成的驱动：模型 → 工具 → 结果回灌 → 再问模型。

    界面只认 stream_reply / run_bash / cancel 三个方法。
    数据真相留在 Session 上，每次发问之前现场重建模型消息，这里不维护第二份历史。
    """

    def __init__(
        self,
        bus: EventBus,
        provider: ChatProvider,
        registry: ToolRegistry,
        config: ProviderConfig,
        context_window: int = 128_000,
        workspace_root: Path | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.config = config
        self.context_window = context_window
        self.workspace_root = workspace_root
        self.cancelled = False
        self.last_latency_ms = 0

    def cancel(self) -> None:
        """请求中断当前生成。"""
        self.cancelled = True

    async def stream_reply(self, session: Session, prompt: str) -> Message | None:
        """对一段用户输入生成流式回复。

        用户的输入已经作为一条 user Message 写进会话，这里的 prompt 只是同一个签名的占位。
        """
        self.cancelled = False
        started = time.time()
        message = Message(
            role="assistant",
            session_id=session.id,
            model_id=self.config.model,
            provider_id=self.config.provider,
        )
        session.add_message(message)
        self.bus.publish("message.add", {"message_id": message.id, "session_id": session.id})
        workspace = Workspace(session.id, self.workspace_root)
        workspace.ensure()
        try:
            step, elapsed_ms = await self._run_steps(session, message, workspace)
        finally:
            self.last_latency_ms = int((time.time() - started) * 1000)
        if self.cancelled:
            return message
        self._finish(session, message, step, elapsed_ms)
        return message

    async def run_bash(self, session: Session, command: str) -> Message:
        """执行一条用户直接给出的命令，把输出作为工具结果写入会话。"""
        self.cancelled = False
        message = Message(
            role="assistant",
            session_id=session.id,
            model_id=self.config.model,
            provider_id=self.config.provider,
        )
        session.add_message(message)
        self.bus.publish("message.add", {"message_id": message.id, "session_id": session.id})
        part = self._publish(
            message,
            Part(
                type=PartType.TOOL,
                tool_name="bash",
                tool_status=ToolStatus.PENDING,
                tool_input={"command": command},
            ),
        )
        await self._execute_tool(message, part)
        self.bus.publish("turn.done", {"message_id": message.id, "session_id": session.id})
        return message

    async def _run_steps(
        self,
        session: Session,
        message: Message,
        workspace: Workspace,
    ) -> tuple[int, int]:
        """按 step 推进，直到模型不再要求调用工具。返回最后走到第几步与耗时。"""
        started = time.time()
        step = 0
        for step in range(1, MAX_STEPS + 1):
            if self.cancelled:
                break
            self._publish(message, Part(type=PartType.STEP_START, text=f"{step} · {self.config.model}"))
            context = build_model_messages(
                session,
                build_system_prompt(workspace, session),
                self.context_window,
            )
            if context.dropped:
                self._publish(
                    message,
                    Part(type=PartType.COMPACTION, text=f"已丢弃 {context.dropped} 条更早的消息"),
                )
            calls = await self._ask_model(message, context.messages)
            if self.cancelled or not calls:
                break
            if not await self._run_tools(message, calls):
                break
        return step, int((time.time() - started) * 1000)

    async def _run_tools(self, message: Message, calls: list[Part]) -> bool:
        """按模型给出的顺序逐个执行。中途被取消时返回 False。

        不并发：打分类工具会争同一块 GPU 与同一份输入文件，并发还会让结果写回历史时
        的顺序与模型给出的顺序不一致。
        """
        for part in calls:
            if self.cancelled:
                return False
            await self._execute_tool(message, part)
            if self.cancelled:
                return False
        return True

    async def _ask_model(self, message: Message, messages: list[dict[str, Any]]) -> list[Part]:
        """问一次模型，返回本 step 新建的工具 Part。可以重试的错误在这里重发。"""
        attempt = 0
        while True:
            try:
                return await self._consume_stream(message, messages)
            except ProviderError as error:
                if not error.retryable or attempt >= MAX_RETRIES:
                    self._publish(
                        message,
                        Part(type=PartType.TEXT, text=f"请求没有成功：{error}"),
                    )
                    return []
                attempt += 1
                self._publish(
                    message,
                    Part(type=PartType.RETRY, text=f"{error} · 第 {attempt} 次"),
                )
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])

    async def _consume_stream(
        self,
        message: Message,
        messages: list[dict[str, Any]],
    ) -> list[Part]:
        """消费一次模型响应，把内容写成 Part，返回本 step 的工具调用。"""
        calls: list[Part] = []
        async for event in self.provider.stream(messages, self.registry.specs()):
            if self.cancelled:
                break
            if event.type is ProviderEventType.TEXT_DELTA:
                self._append_text(message, PartType.TEXT, event.text)
            elif event.type is ProviderEventType.REASONING_DELTA:
                self._append_text(message, PartType.REASONING, event.text)
            elif event.type is ProviderEventType.TOOL_CALL:
                calls.append(
                    self._publish(
                        message,
                        Part(
                            type=PartType.TOOL,
                            tool_name=event.tool_name,
                            tool_status=ToolStatus.PENDING,
                            tool_input=event.tool_args,
                        ),
                    )
                )
            elif event.type is ProviderEventType.USAGE:
                self._add_usage(message, event.usage)
        return calls

    async def _execute_tool(self, message: Message, part: Part) -> None:
        """执行一次工具调用，把结果或失败原因写回 Part。"""
        tool = self.registry.get(part.tool_name)
        if tool is None:
            self._fail_tool(message, part, f"unknown tool: {part.tool_name}")
            return
        error = argument_error(tool.spec, part.tool_input)
        if error:
            self._fail_tool(message, part, error)
            return
        part.tool_status = ToolStatus.RUNNING
        self.bus.publish("part.update", {"message_id": message.id, "part_id": part.id})
        try:
            result = await tool.run(part.tool_input)
        except ToolError as error:
            self._fail_tool(message, part, str(error))
            return
        part.tool_output = result.output
        part.tool_status = ToolStatus.FAILED if result.failed else ToolStatus.COMPLETED
        self.bus.publish("part.update", {"message_id": message.id, "part_id": part.id})

    def _fail_tool(self, message: Message, part: Part, reason: str) -> None:
        """把工具标成失败并写明原因，这条原因会被回灌给模型。"""
        part.tool_status = ToolStatus.FAILED
        part.tool_output = reason
        self.bus.publish("part.update", {"message_id": message.id, "part_id": part.id})

    def _append_text(self, message: Message, part_type: PartType, text: str) -> None:
        """把增量接到当前这一类 Part 上，最后一个是别的类型时另起一个。"""
        last = message.parts[-1] if message.parts else None
        if last is not None and last.type is part_type:
            last.text += text
            self.bus.publish("part.update", {"message_id": message.id, "part_id": last.id})
            return
        self._publish(message, Part(type=part_type, text=text))

    @staticmethod
    def _add_usage(message: Message, usage: dict[str, int]) -> None:
        """把一次请求的用量累加进这一轮的消息。"""
        tokens = dict(message.tokens)
        tokens["input"] = int(tokens.get("input", 0)) + int(usage.get("input") or 0)
        tokens["output"] = int(tokens.get("output", 0)) + int(usage.get("output") or 0)
        message.tokens = tokens

    def _publish(self, message: Message, part: Part) -> Part:
        """把 Part 挂到消息上并通知界面。"""
        message.add_part(part)
        self.bus.publish("part.add", {"message_id": message.id, "part_id": part.id})
        return part

    def _finish(self, session: Session, message: Message, step: int, elapsed_ms: int) -> None:
        """补上收尾的 Part、记账，并发出这一轮结束的信号。"""
        if not any(part.type in (PartType.TEXT, PartType.TOOL) for part in message.parts):
            self._publish(message, Part(type=PartType.TEXT, text="（模型没有返回内容）"))
        tokens = message.total_tokens()
        self._publish(
            message,
            Part(type=PartType.STEP_FINISH, text=f"{step} · {tokens} tok · {elapsed_ms} ms"),
        )
        message.cost = self._cost(message.tokens)
        self.bus.publish("turn.done", {"message_id": message.id, "session_id": session.id})
        self.bus.publish("status.update", {"session_id": session.id})

    def _cost(self, tokens: dict[str, int]) -> float:
        """按每百万 token 的单价算这一轮的花费。"""
        return (
            int(tokens.get("input", 0)) / 1e6 * self.config.price_in
            + int(tokens.get("output", 0)) / 1e6 * self.config.price_out
        )
