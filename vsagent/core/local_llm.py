from __future__ import annotations

import json
import os

DEFAULT_URL = "http://127.0.0.1:8001"
DEFAULT_MODEL = "local"
DEFAULT_TIMEOUT = 20.0

URL_ENV = "VSA_LOCAL_LLM_URL"
MODEL_ENV = "VSA_LOCAL_LLM_MODEL"
TIMEOUT_ENV = "VSA_LOCAL_LLM_TIMEOUT"


def endpoint() -> str:
    return (os.environ.get(URL_ENV) or "").strip() or DEFAULT_URL


def model_name() -> str:
    """llama.cpp 不校验模型名，给一个占位即可，用户想换名字可以覆盖。"""
    return (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL


def default_timeout() -> float:
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def complete_json(
    prompt: str, *, schema_hint: str, timeout: float | None = None
) -> dict | None:
    """向本地 OpenAI 兼容端点要一段 JSON。连不上或解析失败返回 None。

    不 import `provider.py`：那边是流式对话客户端，抽取要的是一次完整响应，
    共用会多出流式状态。也不重试：本地服务没起来时重试多少次都一样。
    """
    import httpx

    payload = {
        "model": model_name(),
        "messages": [
            {
                "role": "system",
                "content": "只输出 JSON，不要解释，不要 Markdown 代码块。字段：" + schema_hint,
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        response = httpx.post(
            endpoint().rstrip("/") + "/v1/chat/completions",
            json=payload,
            timeout=default_timeout() if timeout is None else timeout,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
