from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from ..models import Message, Part, PartType, Session, ToolStatus
from .workspace import Workspace

# 上下文预算：最近这么多条会话消息不参与丢弃
KEEP_MESSAGES = 6
# 估算值降到窗口的这个比例以内就算合格
CONTEXT_RATIO = 0.8
# 单个工具输出发给模型的那一份总长度，超过就留开头与结尾
MODEL_OUTPUT_LIMIT = 4000
MODEL_OUTPUT_HALF = 2000


@dataclass
class BuiltContext:
    """一次发问的输入，以及为塞进窗口丢掉了多少条会话消息。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0


def estimate_tokens(text: str) -> int:
    """按字符数估算 token 数。中文占多数时取字符数的一半是保守估计。"""
    return math.ceil(len(text) / 2)


def truncate_for_model(text: str) -> str:
    """把工具输出裁成发给模型的那一份，留开头与结尾。

    给模型看的那一份与给人看的那一份要求不同：人要看结论，模型要能接上下一句。
    """
    if len(text) <= MODEL_OUTPUT_LIMIT:
        return text
    omitted = len(text) - 2 * MODEL_OUTPUT_HALF
    return f"{text[:MODEL_OUTPUT_HALF]}\n… 省略 {omitted} 字符\n{text[-MODEL_OUTPUT_HALF:]}"


def build_system_prompt(workspace: Workspace, session: Session) -> str:
    """系统提示：身份、工作方式与工作目录的当前状态。"""
    return (
        "你是虚拟筛选工作的助手。用户交办的事情需要动文件或跑程序时，调用工具完成，"
        "不要凭猜测写出结果。\n"
        "工具给出的失败原因是你下一轮的输入：参数被拒就改参数，命令报错就看报错内容。\n"
        "生成的中间文件与结果表写进工作目录，报告里说明每一项使用了什么参数。\n\n"
        f"{workspace.state_text(session)}"
    )


def build_model_messages(
    session: Session,
    system_prompt: str,
    context_window: int,
) -> BuiltContext:
    """把会话翻译成 provider 的消息列表，超出窗口时丢掉更早的消息。

    翻译的唯一真相是 Session 里的 Part，循环层不维护第二份历史。因此中断、撤销、
    切换会话之后重发一轮，模型看到的历史与界面显示的内容一致。
    """
    units = [_translate_message(message) for message in session.messages]
    budget = int(context_window * CONTEXT_RATIO)
    total = estimate_tokens(system_prompt) + sum(_unit_tokens(unit) for unit in units)
    keep_from = max(0, len(units) - KEEP_MESSAGES)
    dropped = 0
    while total > budget and dropped < keep_from:
        total -= _unit_tokens(units[dropped])
        dropped += 1
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for unit in units[dropped:]:
        messages.extend(unit)
    return BuiltContext(messages=messages, dropped=dropped)


def _unit_tokens(unit: list[dict[str, Any]]) -> int:
    """估算一条会话消息翻出来之后占多少 token。"""
    return sum(_message_tokens(item) for item in unit)


def _message_tokens(item: dict[str, Any]) -> int:
    """估算一条模型消息占多少 token，只数要发出去的那些字段。"""
    total = 0
    content = item.get("content")
    if isinstance(content, str):
        total += len(content)
    reasoning = item.get("reasoning_content")
    if isinstance(reasoning, str):
        total += len(reasoning)
    for call in item.get("tool_calls") or []:
        function = call["function"]
        total += len(function["name"]) + len(function["arguments"])
    return math.ceil(total / 2)


def _translate_message(message: Message) -> list[dict[str, Any]]:
    """按消息的角色选翻译方式。"""
    if message.role == "user":
        return _translate_user(message)
    return _translate_assistant(message)


def _translate_user(message: Message) -> list[dict[str, Any]]:
    """用户消息：正文与引用文件拼成同一条 content。"""
    chunks: list[str] = []
    for part in message.parts:
        if part.type is PartType.TEXT and part.text:
            chunks.append(part.text)
        elif part.type is PartType.FILE and part.file_path:
            chunks.append(f'<file path="{part.file_path}">\n{part.text}\n</file>')
    if not chunks:
        return []
    return [{"role": "user", "content": "\n".join(chunks)}]


def _translate_assistant(message: Message) -> list[dict[str, Any]]:
    """助手消息：一个 step 里的推理、正文与工具调用合成一条模型消息。

    DeepSeek 思考模式下，只要请求携带 tools 参数，历史里每一条 assistant 消息都要
    回传 reasoning_content，包括没有工具调用的最终回答；缺少任意一条都会被拒绝。
    所以本 step 有推理的 assistant 消息一律带上，纯聊天的请求里多带的推理会被忽略。
    """
    translated: list[dict[str, Any]] = []
    reasoning: list[str] = []
    text: list[str] = []
    tools: list[Part] = []

    def flush() -> None:
        content = "".join(text)
        if tools:
            item: dict[str, Any] = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {
                            "name": part.tool_name,
                            "arguments": json.dumps(part.tool_input, ensure_ascii=False),
                        },
                    }
                    for part in tools
                ],
            }
            if reasoning:
                item["reasoning_content"] = "".join(reasoning)
            translated.append(item)
            for part in tools:
                translated.append(
                    {
                        "role": "tool",
                        "tool_call_id": part.id,
                        "content": truncate_for_model(part.tool_output),
                    }
                )
        elif content:
            item = {"role": "assistant", "content": content}
            if reasoning:
                item["reasoning_content"] = "".join(reasoning)
            translated.append(item)
        reasoning.clear()
        text.clear()
        tools.clear()

    for part in message.parts:
        if part.type is PartType.STEP_START:
            flush()
        elif part.type is PartType.REASONING:
            reasoning.append(part.text)
        elif part.type is PartType.TEXT:
            text.append(part.text)
        elif part.type is PartType.TOOL and part.tool_status in (
            ToolStatus.COMPLETED,
            ToolStatus.FAILED,
        ):
            tools.append(part)
    flush()
    return translated
