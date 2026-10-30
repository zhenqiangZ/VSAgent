"""内容寻址的产物目录与原子写盘。

配体准备与对接都会写一组文件（manifest + params + 多个产物），并且要满足两条：
**同参数复用、不同参数绝不覆盖**。这套口径与 `receptor.py` 的产物目录相同，
但 receptor 那份绑定了自己的 `Plan`；这里只放新模块能共用、且不依赖领域对象的
最小实现。

`params.json` 是提交标记：它必须最后写。目录存在但缺它，说明上一次写在中间
崩了，不能被当成完整产物复用。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..ledger import fingerprint_parts

PARAMS_FILE = "params.json"
MAX_PRODUCT_TRIES = 1000

# 目录名只允许字母数字与 . _ -：中文、空格、斜杠都换成下划线。产物名会进
# 网页 URL 与日志，留一个能直接看懂的 ASCII 段比保留原字符更省事。
_UNSAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")

_MISSING = object()


def safe_stem(text: str, *, fallback: str = "product") -> str:
    """把任意显示名压成能当目录名的短串。"""
    stem = _UNSAFE_STEM.sub("_", str(text)).strip("._-")
    return stem[:64] or fallback


def has_product(directory: Path, required: Sequence[str]) -> bool:
    """`required` 里的文件是否都在；只查存在，不查内容。"""
    return all((directory / name).is_file() for name in required)


def content_dir(
    root: Path,
    stem: str,
    params: dict[str, Any],
    required: Sequence[str],
) -> tuple[Path, bool]:
    """返回 `(产物目录, 是否复用)`。

    目录名由「安全 stem + 参数指纹前 8 位」决定。同名目录的参数完全相等、
    且 `required` 齐全时才复用；参数不同或产物不齐就加 `-2`、`-3`，
    **绝不在半成品上续写，也绝不覆盖旧产物**。
    """
    root = Path(root)
    digest = fingerprint_parts(params, length=8)
    base = f"{safe_stem(stem)}-{digest}"

    index = 1
    while True:
        name = base if index == 1 else f"{base}-{index}"
        candidate = root / name
        if not candidate.exists():
            root.mkdir(parents=True, exist_ok=True)
            return candidate, False
        if (
            candidate.is_dir()
            and _read_params(candidate) == params
            and has_product(candidate, required)
        ):
            return candidate, True
        index += 1
        if index > MAX_PRODUCT_TRIES:
            raise OSError(
                f"同名产物目录已经超过 {MAX_PRODUCT_TRIES} 个（{root}），"
                "先清理旧产物或换一个 stem"
            )


def read_json(path: Path) -> Any:
    """读 JSON；读不出时让 `OSError` / `JSONDecodeError` 原样上抛。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_text_atomic(path: Path, text: str) -> None:
    """同目录临时文件 → fsync → `os.replace`。中途失败不会留下半截目标文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: Any) -> None:
    """`indent=2`、`ensure_ascii=False`、不写尾随换行，与 `export._write_json` 一致。"""
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_params(directory: Path) -> Any:
    try:
        return read_json(directory / PARAMS_FILE)
    except (OSError, json.JSONDecodeError):
        return _MISSING
