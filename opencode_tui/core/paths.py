"""项目数据目录的定位：`corpus/` 与 `runs/` 挂在这个根下面。

**打包后跟着启动目录走**（与 `logs/` 一样，见 `app.py` 的 `configure_file_log`），
源码运行时是仓库根。两者都不按 `__file__` 算——冻结之后它指向 PyInstaller 解包
出来的临时目录（onefile 退出即删），语料与运行产物写进去，每一步都返回成功，
重启之后连目录带索引一起消失：表现是「网页上语料库空了，但功能都正常」。

注意这与 `env.env_file()` / `permission.project_config_directory()` 不是同一类：
那两份是**这份安装**的配置（打包后跟可执行文件走），这里放的是**用户的数据**，
用户在哪个项目目录里干活，数据就落在哪个项目目录里。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["project_root"]


def project_root() -> Path:
    """项目根：源码运行时是仓库根，打包后是启动目录。"""
    if getattr(sys, "frozen", False):
        return Path.cwd()
    return Path(__file__).resolve().parents[2]
