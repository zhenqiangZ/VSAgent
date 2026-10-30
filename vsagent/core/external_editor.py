from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
WINDOWS_FALLBACK = "notepad"
UNIX_FALLBACKS = ("nano", "vi")


def editor_buffer_path() -> Path:
    """外部编辑器使用的暂存文件路径。"""
    return Path.home() / ".opencode_tui" / "editor-buffer.md"


def resolve_editor() -> str | None:
    """按 VISUAL、EDITOR、平台默认的顺序找一个可用的编辑器。"""
    for name in ("VISUAL", "EDITOR"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    candidates = (WINDOWS_FALLBACK,) if WINDOWS else UNIX_FALLBACKS
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    return None


def write_buffer(path: Path, text: str) -> None:
    """把当前输入写入暂存文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_buffer(path: Path) -> str:
    """从暂存文件读回编辑结果，换行统一成 `\\n`。

    编辑器在 Windows 上多半写成 CRLF，而 `read_text` 默认按通用换行解码，
    CRLF 与单独一个 CR 都会解析成 `\\n`，这里不必再替换一次。
    """
    return path.read_text(encoding="utf-8")


def split_editor_command(editor: str) -> list[str]:
    """把编辑器命令分成可执行文件与参数，去掉包裹的引号。

    EDITOR 常见的写法是 `code --wait` 或 `"C:\\Program Files\\X\\ed.exe" -f`，
    两种都要能分开；带空格的路径也可以用单引号括起来写，同样要认。
    这里只处理引号与空格，不做变量展开。
    """
    tokens = shlex.split(editor, posix=False)
    return [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
        else token
        for token in tokens
    ]


def build_command(editor: str, path: Path) -> str:
    """拼出交给 shell 的命令行：编辑器命令分开之后，每个词单独加引号。

    路径里的空格与编辑器自带的参数都靠这一步保住。取出来的词可能本来带引号
    （`"C:\\Program Files\\X\\ed.exe" -f` 这种写法），取的时候已经去掉，
    这里只管加上一层，两边都以本函数的输出为准。
    """
    tokens = [*split_editor_command(editor), str(path)]
    return " ".join(f'"{token}"' for token in tokens)


def launch(editor: str, path: Path) -> None:
    """把终端交给外部编辑器，返回后由调用方恢复界面。

    走 shell 执行：Windows 上不少编辑器是 .cmd 包装脚本（例如 `code`），
    直接创建进程无法运行它们。
    """
    subprocess.run(build_command(editor, path), shell=True, check=False)
