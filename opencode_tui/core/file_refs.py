from __future__ import annotations

import re
from pathlib import Path

from .models import Part, PartType

FILE_REF = re.compile(r"@([^\s@]+)")
MAX_FILE_CHARS = 2000


def resolve_file_refs(text: str, root: Path) -> tuple[str, list[Part]]:
    """把输入里的 @路径 引用解析成 File Part。

    返回去掉引用占位后的文本与解析出的 Part 列表。
    """
    parts: list[Part] = []
    for match in FILE_REF.finditer(text):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = root / candidate
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        parts.append(
            Part(
                type=PartType.FILE,
                file_path=candidate,
                text=content,
            )
        )
    return text, parts
