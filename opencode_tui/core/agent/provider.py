from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Protocol

import httpx

DEFAULT_TIMEOUT = 120.0


@dataclass
class ProviderConfig:
    """模型接口的连接参数与计费参数。

    密钥只从环境变量读，不写进状态文件。
    """

    base_url: str
    api_key: str
    model: str
    provider: str = "openai"
    price_in: float = 0.0
    price_out: float = 0.0
    context_window: int = 128_000

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ProviderConfig:
        """从 `VSA_LLM_` 开头的环境变量读出连接参数与计费参数。"""
        source = os.environ if environ is None else environ
        missing = [
            name
            for name in ("VSA_LLM_BASE_URL", "VSA_LLM_API_KEY", "VSA_LLM_MODEL")
            if not source.get(name)
        ]
        if missing:
            raise RuntimeError("缺少环境变量：" + "、".join(missing))
        return cls(
            base_url=source["VSA_LLM_BASE_URL"],
            api_key=source["VSA_LLM_API_KEY"],
            model=source["VSA_LLM_MODEL"],
            price_in=float(source.get("VSA_LLM_PRICE_INPUT") or 0.0),
            price_out=float(source.get("VSA_LLM_PRICE_OUTPUT") or 0.0),
            context_window=int(source.get("VSA_LLM_CONTEXT_WINDOW") or 128_000),
        )


class ProviderEventType(Enum):
    """provider 流式事件的类型。"""

    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL = "tool_call"
    USAGE = "usage"


@dataclass
class ProviderEvent:
    """流式过程中的一条事件。

    每种类型只用到其中几个字段：工具调用用 tool_name / tool_args / tool_call_id，
    用量用 usage（键是 input 与 output），两类增量用 text。
    """

    type: ProviderEventType
    text: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class ProviderError(Exception):
    """模型接口返回的错误。可否重试由循环层据此决定。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ChatProvider(Protocol):
    """模型接口：把消息与工具清单交给模型，逐条产出流式事件。"""

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[ProviderEvent]:
        """请求一次补全。"""
        ...


class OpenAICompatProvider:
    """走 /chat/completions 的模型接口实现。

    工具调用的 arguments 在流里是逐段拼出来的 JSON 字符串，要等整个流结束才解析；
    中途解析必然得到半个对象。用量同样攒到流结束再发，让它在事件序列里排在最后。
    """

    def __init__(self, config: ProviderConfig, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[ProviderEvent]:
        """请求一次补全，逐条产出文本、推理、工具调用与用量。"""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        collected: dict[int, dict[str, str]] = {}
        pending_usage: list[ProviderEvent] = []
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            try:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise self._error(response)
                    async for line in response.aiter_lines():
                        for event in self._parse_line(line, collected):
                            if event.type is ProviderEventType.USAGE:
                                pending_usage.append(event)
                            else:
                                yield event
            except httpx.HTTPError as error:
                raise ProviderError(f"连接失败：{error}", retryable=True) from error
        for event in self._flush_calls(collected):
            yield event
        for event in pending_usage:
            yield event

    @staticmethod
    def _error(response: httpx.Response) -> ProviderError:
        """把非 200 的应答翻成错误，429 与 5xx 归到可以重试的那一类。"""
        status = response.status_code
        detail = " ".join(response.text.split())[:200]
        return ProviderError(
            f"HTTP {status}：{detail}",
            status_code=status,
            retryable=status == 429 or status >= 500,
        )

    def _parse_line(self, line: str, collected: dict[int, dict[str, str]]) -> list[ProviderEvent]:
        """解析一行 SSE，非数据行与收尾标记都返回空列表。"""
        if not line.startswith("data:"):
            return []
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return []
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as error:
            raise ProviderError(f"SSE 分片不是合法的 JSON：{error}") from error
        return self._parse_chunk(chunk, collected)

    def _parse_chunk(
        self,
        chunk: dict[str, Any],
        collected: dict[int, dict[str, str]],
    ) -> list[ProviderEvent]:
        """解析一条分片，把工具调用的分片累加进 collected。"""
        events: list[ProviderEvent] = []
        usage = chunk.get("usage")
        if usage:
            events.append(
                ProviderEvent(
                    ProviderEventType.USAGE,
                    usage={
                        "input": int(usage.get("prompt_tokens") or 0),
                        "output": int(usage.get("completion_tokens") or 0),
                    },
                )
            )
        choices = chunk.get("choices") or []
        if not choices:
            return events
        delta = choices[0].get("delta") or {}
        self._collect_calls(delta.get("tool_calls") or [], collected)
        if delta.get("reasoning_content"):
            events.append(
                ProviderEvent(ProviderEventType.REASONING_DELTA, text=str(delta["reasoning_content"]))
            )
        if delta.get("content"):
            events.append(ProviderEvent(ProviderEventType.TEXT_DELTA, text=str(delta["content"])))
        return events

    @staticmethod
    def _collect_calls(
        fragments: list[dict[str, Any]],
        collected: dict[int, dict[str, str]],
    ) -> None:
        """按 index 累加工具调用：id 与名字只在第一段给出，arguments 每段都要拼上去。"""
        for fragment in fragments:
            index = int(fragment.get("index") or 0)
            entry = collected.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if fragment.get("id"):
                entry["id"] = str(fragment["id"])
            function = fragment.get("function") or {}
            if function.get("name"):
                entry["name"] = str(function["name"])
            if function.get("arguments"):
                entry["arguments"] += str(function["arguments"])

    @staticmethod
    def _flush_calls(collected: dict[int, dict[str, str]]) -> list[ProviderEvent]:
        """流结束后把攒下来的工具调用解析成事件，顺序按 index。"""
        events: list[ProviderEvent] = []
        for index in sorted(collected):
            entry = collected[index]
            text = entry["arguments"] or "{}"
            try:
                arguments = json.loads(text)
            except json.JSONDecodeError as error:
                raise ProviderError(f"工具 {entry['name']} 的参数不是合法的 JSON：{error}") from error
            events.append(
                ProviderEvent(
                    ProviderEventType.TOOL_CALL,
                    tool_name=entry["name"],
                    tool_args=arguments,
                    tool_call_id=entry["id"],
                )
            )
        return events
