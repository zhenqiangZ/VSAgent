from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# 写进**当前项目**的 `logs/` 目录（已 gitignore），文件名固定，跨会话累积。
# 不放进用户目录：这份日志是给人在项目里随手 grep 的，换项目就该换一份。
LOG_FILE = "file-reads.jsonl"
PROJECT_LOG_DIRECTORY = "logs"

# 单行 detail 的上限。读取失败时异常信息可能很长，整段写进去会把日志读成一团。
MAX_DETAIL = 300
# 单个日志文件的大小上限。跨会话累积，一份跑久了的工程很容易到几十 MB；
# 超过之后改名为 `.1` 再从头写，保留最近一份，旧的那份覆盖掉。
MAX_BYTES = 32 * 1024 * 1024

# 应用启动时把项目根交进来（测试里是临时目录）；`None` 表示还没配置，
# 就落到进程当前目录下的 `logs/`，这样脚本与领域工具的单独调用也能记上。
_directory: Path | None = None


def configure(directory: Path | None) -> None:
    """指定日志目录。传 `None` 恢复成进程当前目录下的 `logs/`。"""
    global _directory
    _directory = directory


def log_directory() -> Path:
    """日志目录：配置过就用配置的，否则是当前项目里的 `logs/`。"""
    if _directory is not None:
        return _directory
    return Path.cwd() / PROJECT_LOG_DIRECTORY


def current_path() -> Path:
    """当前日志文件的路径。"""
    return log_directory() / LOG_FILE


def record(
    path: str | Path,
    *,
    purpose: str,
    ok: bool = True,
    size: int | None = None,
    detail: str = "",
) -> None:
    """追加一条读取记录。

    `purpose` 写清是谁读的（`tool.read` / `import.structure` / `state.load` 之类），
    grep 时按它筛就能只看某一类。`ok=False` 的那几行同样是信息：
    「文件没读到」和「没去读」在界面上长得一样，只有日志分得开。

    日志是旁路：写不进去（目录不可建、磁盘满）就放弃，绝不让一次读取因为它失败。
    """
    # 环境变量置 0 可以整条关掉：批量脚本不想污染日志时用。
    if os.environ.get("VSA_FILE_READ_LOG", "").strip().lower() in {"0", "off", "false"}:
        return
    entry: dict[str, Any] = {
        "at": time.time(),
        "purpose": purpose,
        "path": str(path),
        "ok": ok,
    }
    if size is not None:
        entry["bytes"] = size
    if detail:
        entry["detail"] = detail[:MAX_DETAIL]
    try:
        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / LOG_FILE
        # 轮转放在 try 里：它只是旁路维护，磁盘满、权限不足都不能让一次
        # 正常读取失败。超过上限先改名，旧的那一份 `.1` 被覆盖。
        if target.exists() and target.stat().st_size >= MAX_BYTES:
            backup = target.with_name(target.name + ".1")
            backup.unlink(missing_ok=True)
            target.rename(backup)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return
