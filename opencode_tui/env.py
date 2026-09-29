from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv


def env_file() -> Path:
    """`.env` 的位置：打包之后与可执行文件同目录，否则项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / ".env"
    return Path(__file__).resolve().parents[1] / ".env"


def load_env() -> None:
    """把 `.env` 里的变量读进环境。

    文件不存在时什么都不做。已经存在的环境变量优先，`override=False`。
    """
    path = env_file()
    if path.is_file():
        load_dotenv(path, override=False)
