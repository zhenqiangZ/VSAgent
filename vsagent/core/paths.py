"""项目数据目录的定位：`corpus/` 与 `runs/` 挂在这个根下面。

**打包后跟着启动目录走**（与 `logs/` 一样，见 `app.py` 的 `configure_file_log`），
源码运行时是仓库根。两者都不按 `__file__` 算——冻结之后它指向 PyInstaller 解包
出来的临时目录（onefile 退出即删），语料与运行产物写进去，每一步都返回成功，
重启之后连目录带索引一起消失：表现是「网页上语料库空了，但功能都正常」。

注意这与 `env.env_file()` / `permission.project_config_directory()` 不是同一类：
那两份是**这份安装**的配置（打包后跟可执行文件走），这里放的是**用户的数据**，
用户在哪个项目目录里干活，数据就落在哪个项目目录里。

`CONFIG_DIRECTORY` 是**用户目录**下那一份（`~/.vsagent`）的名字，状态文件、
连接配置、登记表快照、模型缓存都挂在它下面；项目侧也用它当配置目录名
（`<项目>/.vsagent/tools.toml` 那一份）。改这个名字等于换一个用户，
所以旧的 `.opencode_tui` 由 `migrate_legacy_config()` 在启动时一次性搬过来。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

__all__ = [
    "CONFIG_DIRECTORY",
    "LEGACY_CONFIG_DIRECTORY",
    "Migration",
    "SURVEY_DIRECTORY",
    "config_home",
    "migrate_legacy_config",
    "project_root",
]

# 用户目录下的配置/状态目录名。
CONFIG_DIRECTORY = ".vsagent"
# 改名之前用的名字。只在迁移时读，代码里不再往这里写任何东西。
LEGACY_CONFIG_DIRECTORY = ".opencode_tui"

SURVEY_DIRECTORY = "surveys"
"""`~/.vsagent/surveys`：结构全景产物。

写它的是 `domain/survey.py`，读它的是网页端的 `/api/survey/<id>`。两侧各写一份
字面量的话，改名之后工具照样写、页面照样 404，而两边都不会报错。
"""


def project_root() -> Path:
    """项目根：源码运行时是仓库根，打包后是启动目录。"""
    if getattr(sys, "frozen", False):
        return Path.cwd()
    return Path(__file__).resolve().parents[2]


def config_home(home: Path | None = None) -> Path:
    """用户目录下那一份数据目录：`~/.vsagent`。"""
    return (home or Path.home()) / CONFIG_DIRECTORY


@dataclasses.dataclass(frozen=True)
class Migration:
    """一次旧配置目录的搬迁。

    `files` 是搬过去的文件数（不含目录）。**它存在的唯一理由是日志**：
    迁移是静默的，出问题（搬错目录、只搬了一半）时没有这个数就没有线索。
    """

    source: Path
    target: Path
    files: int

    def describe(self) -> str:
        return (
            f"已从 {self.source.name} 迁移到 {self.target.name}"
            f"（{self.files} 个文件）：{self.target}"
        )


def migrate_legacy_config(base: Path | None = None) -> Migration | None:
    """把 `base` 下的 `.opencode_tui` 整体改名成 `.vsagent`。

    `base` 有两种，**两种都要调**：

    - 用户目录（`~`）：状态、连接配置、登记表快照、模型缓存。
    - 项目侧（仓根，或打包后 exe 所在目录）：工具开关、判定规则、钩子。

    项目侧那份漏搬的后果比家目录更隐蔽——模型会突然多出几个本该关掉的工具，
    `permissions.toml` 里的 deny 也一起失效，而界面上一切正常。

    **两边都在就什么都不做**：那时用户已经在新目录里跑过，再合并会把旧的
    覆盖到新的上面；而搬迁失败（权限、跨盘、被别的进程占着）也只是留在原地——
    丢一份旧配置可以接受，为它拦住启动不行。
    """
    root = base or Path.home()
    legacy = root / LEGACY_CONFIG_DIRECTORY
    target = root / CONFIG_DIRECTORY
    if target.exists() or not legacy.is_dir():
        return None
    # 先数再搬：搬完之后 legacy 这个路径就不存在了。
    files = sum(1 for item in legacy.rglob("*") if item.is_file())
    try:
        legacy.rename(target)
    except OSError:
        return None
    return Migration(legacy, target, files)
