from __future__ import annotations

import json
from datetime import datetime

from .models import Message, Part, PartType, Session

ROLE_TITLE = {"user": "你", "assistant": "assistant"}

FENCE = "```"


def export_filename(session: Session) -> str:
    """导出对话时使用的文件名。"""
    return f"opencode-export-{session.id}.md"


def render_session_markdown(session: Session) -> str:
    """把整个会话渲染为 Markdown 文本。"""
    lines = [
        f"# {session.title}",
        "",
        f"- 会话 ID：{session.id}",
        f"- 模型：{session.model_id or '未指定'}",
        f"- Agent：{session.agent_name}",
        f"- 创建时间：{_timestamp(session.created_at)}",
        f"- 更新时间：{_timestamp(session.updated_at)}",
        f"- 用量：{session.total_tokens()} tokens · ${session.total_cost():.3f}",
        "",
    ]
    for message in session.messages:
        lines.extend(_render_message(message))
    return "\n".join(lines).rstrip() + "\n"


def _timestamp(value: float) -> str:
    """把时间戳格式化为可读形式。"""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _render_message(message: Message) -> list[str]:
    """渲染一条消息。"""
    lines = [f"## {ROLE_TITLE.get(message.role, message.role)}", ""]
    for part in message.parts:
        lines.extend(_render_part(part))
    lines.append("")
    return lines


def _render_part(part: Part) -> list[str]:
    """按 Part 类型渲染。"""
    if part.type is PartType.TEXT:
        return [part.text, ""]
    if part.type is PartType.REASONING:
        return [
            "<details>",
            "<summary>思考过程</summary>",
            "",
            part.text,
            "",
            "</details>",
            "",
        ]
    if part.type is PartType.TOOL:
        status = part.tool_status.value if part.tool_status else "pending"
        lines = [f"**工具 `{part.tool_name}` · {status}**", ""]
        if part.tool_input:
            lines.extend(
                [
                    FENCE + "json",
                    json.dumps(part.tool_input, ensure_ascii=False, indent=2),
                    FENCE,
                    "",
                ]
            )
        if part.tool_output:
            lines.extend([FENCE, part.tool_output, FENCE, ""])
        return lines
    if part.type is PartType.FILE:
        return [f"引用文件 `{part.file_path}`", ""]
    if part.type is PartType.AGENT:
        return [f"Agent `{part.agent_name}`", ""]
    if part.type is PartType.STEP_START:
        return [f"── 步骤开始 {part.text}".rstrip(), ""]
    if part.type is PartType.STEP_FINISH:
        return [f"── 步骤结束 {part.text}".rstrip(), ""]
    if part.type is PartType.PATCH:
        return [f"补丁 `{part.file_path or part.text}`", ""]
    if part.type is PartType.SUBTASK:
        return [f"子任务 `{part.agent_name or 'subagent'}` {part.text}", ""]
    if part.type is PartType.RETRY:
        return [f"重试 {part.text}", ""]
    if part.type is PartType.COMPACTION:
        return [f"上下文已压缩 {part.text}", ""]
    return [part.text, ""]
