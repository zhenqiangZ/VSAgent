"""模型输出的 JSON 读取。

先验答案、证据评估、整合与核对都要求模型只写 JSON；实际输出偶尔带代码块
围栏或前后说明。这里提供同一个读取入口，失败时抛 `SearchReasoningError`，
由调用方按出错处理，不静默降级。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from .models import SearchReasoningError

# 一次问答请求：给一组消息，返回模型写出的正文。
Ask = Callable[[list[dict[str, str]]], Awaitable[str]]


def parse_json(text: str, label: str) -> dict[str, Any]:
    """从模型输出里取出一个 JSON 对象。

    模型偶尔会加代码块围栏或前后说明。先试整段，失败再取第一对花括号
    之间的内容；两次都不行就抛错。
    """
    raw = (text or "").strip()
    if not raw:
        raise SearchReasoningError(f"{label}没有返回内容")
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        candidates.insert(1, fenced.group(1))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise SearchReasoningError(f"{label}的输出无法解析成 JSON：{raw[:200]}")
