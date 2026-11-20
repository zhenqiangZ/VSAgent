"""原子写：同目录临时文件写满、`fsync`、再换名到目标位置。

全仓只留这一份。状态文件、登记快照、连接配置、实时快照各写过一遍时，
重试次数与失败清理的时机并不一致，而没有 fsync 的那几份在断电后会留下
读到一半的文件——网页端每个请求都重新读盘，半截 JSON 就是整页空白。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# 换名在目标文件正被别的进程打开时会被系统拒绝，隔几毫秒重试。
REPLACE_ATTEMPTS = 40
REPLACE_RETRY_DELAY = 0.005


def replace_with_retry(staged: Path, target: Path) -> None:
    """把临时文件换名到目标位置，遇到占用就稍等再试。"""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(staged, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_DELAY)


def _staged_path(target: Path) -> Path:
    """临时文件与目标同目录：跨目录换名不是原子操作，而且可能跨文件系统失败。"""
    return target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")


def _install(staged: Path, target: Path, *, mode: int | None, retry: bool) -> None:
    if mode is not None:
        try:
            os.chmod(staged, mode)
        except OSError:
            # 有些文件系统（以及 Windows）不接受这个位，能用就行。
            pass
    if retry:
        replace_with_retry(staged, target)
    else:
        os.replace(staged, target)


def write_text(
    path: str | Path,
    text: str,
    *,
    fsync: bool = True,
    newline: str | None = None,
    mode: int | None = None,
    retry: bool = True,
) -> Path:
    """原子写一份文本，返回目标路径。

    `newline=None` 按平台翻译换行；要给 Windows 上也是字节一致的 TOML，
    调用方传 `""` 或 `"\\n"`。`fsync=False` 只给显示用的快照：每一步都等
    一次磁盘不划算，丢掉最后一帧可以接受。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = _staged_path(target)
    try:
        with staged.open("w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        _install(staged, target, mode=mode, retry=retry)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return target


def write_bytes(
    path: str | Path,
    data: bytes,
    *,
    fsync: bool = True,
    mode: int | None = None,
    retry: bool = True,
) -> Path:
    """原子写一份字节。上传的 PDF 与语料原件走这一条，按文本写会毁掉二进制。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = _staged_path(target)
    try:
        with staged.open("wb") as handle:
            handle.write(data)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        _install(staged, target, mode=mode, retry=retry)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return target
