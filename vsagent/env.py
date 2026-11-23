from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv


def env_file() -> Path:
    """`.env` 的位置。

    源码运行读仓库根。打包产物还在源码仓库里（`dist/VSAgent` 这类布局）时同样读
    仓库根，与源码运行共用一份配置；按 `vsagent.spec` 认仓库根，部署到别处的 exe
    读旁边那一份，不随启动目录变化。
    """
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).parent
        for candidate in (beside, *beside.parents):
            if (candidate / "vsagent.spec").is_file():
                return candidate / ".env"
        return beside / ".env"
    return Path(__file__).resolve().parents[1] / ".env"


def load_env() -> None:
    """把 `.env` 里的变量读进环境。

    文件不存在时什么都不做。已经存在的环境变量优先，`override=False`。
    """
    path = env_file()
    if path.is_file():
        load_dotenv(path, override=False)
