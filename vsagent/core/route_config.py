"""路由模式：配置文件为主，环境变量只做临时覆盖。

配置文件放在项目侧的 `.vsagent/route.toml`，用户目录的
`~/.vsagent/route.toml` 可以再覆盖一次：

```toml
[route]
mode = "shadow"   # off / shadow / enforce
```

读取顺序是「项目 → 用户 → 环境变量」，后面的覆盖前面的。文件不存在、
内容写坏、模式值不认识时都按当前值继续，不抛异常：一个模式开关不该让
应用起不来。每次调用都重新读文件，改完下一轮立即生效。
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .paths import CONFIG_DIRECTORY

ROUTE_FILE = "route.toml"
ENV = "VSA_ROUTE"
MODES: tuple[str, ...] = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
"""默认照常路由，只观察不改变工具清单。"""


@dataclass(frozen=True)
class RouteConfig:
    """当前生效的路由模式，以及它是从哪儿读到的。"""

    mode: str = DEFAULT_MODE
    source: str = "默认值"


def config_paths(
    root: Path | None = None,
    home: Path | None = None,
) -> tuple[Path, Path]:
    """两份配置文件：项目侧在前，用户目录在后。"""
    if getattr(sys, "frozen", False):
        project = Path(sys.executable).parent
    else:
        project = Path(root) if root is not None else Path.cwd()
    return (
        project / CONFIG_DIRECTORY / ROUTE_FILE,
        (home or Path.home()) / CONFIG_DIRECTORY / ROUTE_FILE,
    )


def load(
    root: Path | None = None,
    home: Path | None = None,
) -> RouteConfig:
    """读一次配置。认不出的值直接忽略，留给下一层覆盖。"""
    mode = DEFAULT_MODE
    source = "默认值"
    for path in config_paths(root, home):
        value = _read_mode(path)
        if value is not None:
            mode = value
            source = str(path)

    env = os.environ.get(ENV, "").strip().lower()
    if env in MODES:
        mode = env
        source = ENV
    return RouteConfig(mode=mode, source=source)


def mode(
    root: Path | None = None,
    home: Path | None = None,
) -> str:
    """当前路由模式。`off` / `shadow` / `enforce` 之一。"""
    return load(root, home).mode


def _read_mode(path: Path) -> str | None:
    """从一份文件里取 `[route].mode`。取不到或值不认识时返回 None。"""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get("route")
    if not isinstance(table, dict):
        return None
    value = str(table.get("mode") or "").strip().lower()
    return value if value in MODES else None
