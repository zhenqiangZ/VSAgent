from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Protocol

import httpx

DEFAULT_TIMEOUT = 120.0

PRICE_UNKNOWN = -1.0
DEFAULT_CONTEXT_WINDOW = 128_000
# 采样参数没有单独给出时不写进请求体，由接口那一侧用它自己的默认值
UNSET = None


@dataclass(frozen=True)
class ModelSpec:
    """一个模型的已知参数。

    `reasoning` 指这个模型接不接受请求体里的 reasoning_effort 字段。
    单价按每百万 token 的美元价，与各家定价页给的口径一致。
    `temperature` 与 `max_tokens` 为 None 时不写进请求体：
    各家对这两个字段的默认值与合法范围不一致，没有把握的值不替模型做决定。
    """

    name: str
    provider: str
    context_window: int
    price_in: float
    price_out: float
    price_cached: float = PRICE_UNKNOWN
    reasoning: bool = False
    temperature: float | None = UNSET
    max_tokens: int | None = UNSET
    note: str = ""

    def cached_price(self) -> float:
        """缓存命中的输入单价。没有单独给出时按未命中价的一折算。"""
        if self.price_cached >= 0:
            return self.price_cached
        return self.price_in / 10


def _spec(
    name: str,
    provider: str,
    context_window: int,
    price_in: float,
    price_out: float,
    price_cached: float = PRICE_UNKNOWN,
    reasoning: bool = False,
    temperature: float | None = UNSET,
    max_tokens: int | None = UNSET,
    note: str = "",
) -> ModelSpec:
    return ModelSpec(
        name=name,
        provider=provider,
        context_window=context_window,
        price_in=price_in,
        price_out=price_out,
        price_cached=price_cached,
        reasoning=reasoning,
        temperature=temperature,
        max_tokens=max_tokens,
        note=note,
    )


BUILTIN_MODELS: dict[str, ModelSpec] = {
    spec.name: spec
    for spec in (
        _spec(
            "deepseek-flash",
            "deepseek",
            1_000_000,
            0.3,
            1.2,
            temperature=0.0,
            note="默认模型。上下文一百万，2025 年的定价页口径。写代码时用官方建议的 0。",
        ),
        _spec(
            "deepseek-reasoner",
            "deepseek",
            128_000,
            0.55,
            2.19,
            price_cached=0.14,
            reasoning=True,
            max_tokens=32_000,
            note="推理模型，接受 reasoning_effort。旧名，官方已收进 flash 的开关里。",
        ),
        _spec("deepseek-chat", "deepseek", 128_000, 0.27, 1.1, price_cached=0.07, temperature=1.0),
        _spec("gpt-4o", "openai", 128_000, 2.5, 10.0, price_cached=1.25, temperature=1.0),
        _spec("gpt-4o-mini", "openai", 128_000, 0.15, 0.6, price_cached=0.075, temperature=1.0),
        _spec("gpt-4.1", "openai", 1_047_576, 2.0, 8.0, price_cached=0.5, temperature=1.0),
        _spec("gpt-4.1-mini", "openai", 1_047_576, 0.4, 1.6, price_cached=0.1, temperature=1.0),
        _spec("o3-mini", "openai", 200_000, 1.1, 4.4, price_cached=0.55, reasoning=True),
        _spec(
            "claude-sonnet-4-20250514",
            "anthropic",
            200_000,
            3.0,
            15.0,
            price_cached=0.3,
            reasoning=True,
            max_tokens=16_000,
        ),
        _spec(
            "claude-3-5-haiku-20241022",
            "anthropic",
            200_000,
            0.8,
            4.0,
            price_cached=0.08,
            max_tokens=8_192,
        ),
        _spec(
            "qwen3-coder-plus",
            "dashscope",
            1_000_000,
            1.0,
            5.0,
            note="国内接口的上下文与单价时常调整，这里的数字只作计价起点。",
        ),
        _spec("glm-4.6", "zhipu", 200_000, 0.6, 2.2, reasoning=True, temperature=0.6),
        _spec("kimi-k2-0905-preview", "moonshot", 256_000, 0.6, 2.5, temperature=0.6),
    )
}

MODEL_ALIASES: dict[str, str] = {
    "deepseek-v3": "deepseek-flash",
    "gpt-4o-2024-11-20": "gpt-4o",
    "o1": "o3-mini",
}


def lookup_model(name: str) -> ModelSpec | None:
    """按模型名查内置清单。别名先归一，查不到返回 None。"""
    key = (name or "").strip()
    if not key:
        return None
    if key in BUILTIN_MODELS:
        return BUILTIN_MODELS[key]
    alias = MODEL_ALIASES.get(key)
    return BUILTIN_MODELS.get(alias) if alias else None


def model_names(provider: str = "") -> list[str]:
    """内置清单里的模型名。给出 provider 时只列这一家的。"""
    names = [
        spec.name
        for spec in BUILTIN_MODELS.values()
        if not provider or spec.provider == provider
    ]
    return sorted(names)


def guess_provider(name: str) -> str:
    """按模型名的前缀猜发行方。猜不出时给空串。"""
    key = (name or "").strip().lower()
    for spec in BUILTIN_MODELS.values():
        if spec.provider and key.startswith(spec.provider):
            return spec.provider
    if key.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if key.startswith("claude-"):
        return "anthropic"
    if key.startswith("qwen"):
        return "dashscope"
    if key.startswith("glm"):
        return "zhipu"
    if key.startswith(("kimi", "moonshot")):
        return "moonshot"
    return ""


@dataclass
class ProviderConfig:
    """模型接口的连接参数、计费参数与采样参数。

    密钥只从环境变量读，不写进状态文件。
    计费参数、上下文窗口与采样参数先按内置清单取默认值，环境变量里显式给出的项再覆盖。
    """

    base_url: str
    api_key: str
    model: str
    provider: str = "openai"
    price_in: float = 0.0
    price_out: float = 0.0
    price_cached: float = -1.0
    context_window: int = 128_000
    supports_reasoning: bool = False
    known: bool = False
    temperature: float | None = UNSET
    max_tokens: int | None = UNSET

    def cached_price(self) -> float:
        """缓存命中的输入单价。没有单独给出时按未命中价的一折算。"""
        if self.price_cached >= 0:
            return self.price_cached
        return self.price_in / 10

    def for_model(self, name: str) -> ProviderConfig:
        """换到另一个模型，连接参数照旧，其余项按新模型重取。

        用户在选择界面上换模型时走这条路径：换成清单里的模型就按清单里的
        窗口、单价、采样参数与推理能力走；换成清单里没有的就退回兜底值，
        与 `from_env` 给一个陌生模型的处理完全一致。
        """
        key = (name or "").strip()
        if not key or key == self.model:
            return self
        spec = lookup_model(key)
        if spec is None:
            return ProviderConfig(
                base_url=self.base_url,
                api_key=self.api_key,
                model=key,
                provider=guess_provider(key),
            )
        return ProviderConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=spec.name,
            provider=spec.provider,
            price_in=spec.price_in,
            price_out=spec.price_out,
            price_cached=spec.price_cached,
            context_window=spec.context_window,
            supports_reasoning=spec.reasoning,
            known=True,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ProviderConfig:
        """从 `VSA_LLM_` 开头的环境变量读出连接参数与计费参数。

        base_url、api_key、model 三个必须有；
        其余项先查内置清单，环境变量里写了的以环境变量为准。
        """
        source = os.environ if environ is None else environ
        missing = [
            name
            for name in ("VSA_LLM_BASE_URL", "VSA_LLM_API_KEY", "VSA_LLM_MODEL")
            if not source.get(name)
        ]
        if missing:
            raise RuntimeError("缺少环境变量：" + "、".join(missing))
        model = source["VSA_LLM_MODEL"]
        spec = lookup_model(model)
        cached = source.get("VSA_LLM_PRICE_CACHED")
        price_cached = float(cached) if cached else (spec.price_cached if spec else PRICE_UNKNOWN)
        context_window = source.get("VSA_LLM_CONTEXT_WINDOW")
        temperature = source.get("VSA_LLM_TEMPERATURE")
        max_tokens = source.get("VSA_LLM_MAX_TOKENS")
        return cls(
            base_url=source["VSA_LLM_BASE_URL"],
            api_key=source["VSA_LLM_API_KEY"],
            model=model,
            provider=source.get("VSA_LLM_PROVIDER") or (spec.provider if spec else guess_provider(model)),
            price_in=float(
                source.get("VSA_LLM_PRICE_INPUT") or (spec.price_in if spec else 0.0)
            ),
            price_out=float(
                source.get("VSA_LLM_PRICE_OUTPUT") or (spec.price_out if spec else 0.0)
            ),
            price_cached=price_cached,
            context_window=int(context_window) if context_window else (
                spec.context_window if spec else DEFAULT_CONTEXT_WINDOW
            ),
            supports_reasoning=spec.reasoning if spec else False,
            known=spec is not None,
            temperature=float(temperature) if temperature else (
                spec.temperature if spec else UNSET
            ),
            max_tokens=int(max_tokens) if max_tokens else (
                spec.max_tokens if spec else UNSET
            ),
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


def _read_usage(usage: dict[str, Any]) -> dict[str, int]:
    """读出一次请求的用量。

    缓存命中的输入 token 单独计数：它的单价只有未命中的十分之一。
    DeepSeek 用 prompt_cache_hit_tokens 与 prompt_cache_miss_tokens，
    OpenAI 用 prompt_tokens_details.cached_tokens。两者都没有时按全部未命中算。
    """
    total = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("prompt_cache_hit_tokens") or 0)
    if not cached:
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0)
    missed = usage.get("prompt_cache_miss_tokens")
    uncached = int(missed) if missed is not None else max(total - cached, 0)
    return {
        "input": total,
        "output": int(usage.get("completion_tokens") or 0),
        "cached": cached,
        "uncached": uncached,
    }


class ChatProvider(Protocol):
    """模型接口：把消息与工具清单交给模型，逐条产出流式事件。"""

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str = "",
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
        reasoning_effort: str = "",
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
        if reasoning_effort and self.config.supports_reasoning:
            payload["reasoning_effort"] = reasoning_effort
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
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
            events.append(ProviderEvent(ProviderEventType.USAGE, usage=_read_usage(usage)))
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
