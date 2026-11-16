"""每个角色装哪些工具：读 `agents.toml`，不再按族装卸。

三份配置按顺序读，同一个角色以靠后的整段为准（不按工具逐个合并，避免少写一个
就悄悄丢掉一半）：

1. 包内默认 `vsagent/core/agent/roles/agents.toml`；
2. 项目 `<root>/.vsagent/agents.toml`；
3. 用户 `~/.vsagent/agents.toml`。

合并结果算一次、缓存在进程里。会话建立时按角色取一份名单，运行当中不再改动：
模型不需要判断该装哪一族，换主题也不会改变请求体的前缀。

`core` 只有三档：`orchestrator`（根会话）、`worker`（子会话）、`full`（调试与
回滚，等于全部工具）。角色的 `tools` 并进这一档的名单；`full` 表示不做过滤。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

PACKAGE_FILE = Path(__file__).with_name("agents.toml")
PROJECT_FILE = Path(".vsagent") / "agents.toml"
USER_FILE = Path("~/.vsagent/agents.toml")

TIERS: tuple[str, ...] = ("orchestrator", "worker", "full")
ENV = "VSA_TOOLSET"
ROUTE_ENV = "VSA_ROUTE"


@dataclass(frozen=True)
class Entry:
    """一个角色的配置：基础档 + 额外的工具名。"""

    core: str = "worker"
    tools: tuple[str, ...] = ()


class ConfigError(ValueError):
    """配置写错了。启动时抛，不要等到某一步才发现工具没了。"""


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path} 读不出来：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigError(f"{path} 不是一个对象")
    return payload


def _project_root(root: Path | None) -> Path:
    return Path(root) if root is not None else Path.cwd()


def _merged(root: Path | None) -> dict:
    paths = [PACKAGE_FILE, _project_root(root) / PROJECT_FILE, USER_FILE.expanduser()]
    merged: dict = {"core": {}, "agents": {}}
    for path in paths:
        payload = _read(path)
        for tier, section in (payload.get("core") or {}).items():
            merged["core"][tier] = section
        for role, section in (payload.get("agents") or {}).items():
            merged["agents"][role] = section
    return merged


def _names(section: object, where: str) -> tuple[str, ...]:
    if not isinstance(section, dict):
        raise ConfigError(f"{where} 应当是一张表")
    raw = section.get("tools")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"{where}.tools 应当是字符串数组")
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return tuple(out)


@lru_cache(maxsize=8)
def _table(root: str) -> tuple[dict[str, tuple[str, ...]], dict[str, Entry]]:
    merged = _merged(Path(root) if root else None)
    core: dict[str, tuple[str, ...]] = {}
    for tier, section in merged["core"].items():
        if tier not in TIERS:
            raise ConfigError(f"core 只有 {'、'.join(TIERS)} 三档，收到 {tier!r}")
        core[tier] = _names(section, f"core.{tier}")
    for tier in TIERS:
        core.setdefault(tier, ("*",) if tier == "full" else ())
    # 前两档空了就是配置没读到，不是「这个会话不需要工具」：冻结后漏打包这份
    # 文件时，模型收不到任何工具定义，会把调用写进正文，看着像模型坏了。
    for tier in ("orchestrator", "worker"):
        if not core[tier]:
            raise ConfigError(
                f"core.{tier} 一个工具都没有：三份 agents.toml 里都没读到这一档"
                f"（包内默认是 {PACKAGE_FILE}）。打包时要把这份文件放进 datas。"
            )

    agents: dict[str, Entry] = {}
    for role, section in merged["agents"].items():
        if not isinstance(section, dict):
            raise ConfigError(f"agents.{role} 应当是一张表")
        tier = str(section.get("core") or "worker").strip()
        if tier not in TIERS:
            raise ConfigError(f"agents.{role}.core 只能是 {'、'.join(TIERS)}，收到 {tier!r}")
        agents[role] = Entry(core=tier, tools=_names(section, f"agents.{role}"))
    return core, agents


def core_tools(tier: str, root: Path | None = None) -> tuple[str, ...]:
    """某一档的基础名单。`full` 返回 `("*",)`，装配时不做过滤。"""
    table, _agents = _table(str(_project_root(root)))
    if tier not in table:
        raise ConfigError(f"没有这一档：{tier}")
    return table[tier]


def entry(role: str, root: Path | None = None) -> Entry:
    """一个角色的配置。没写过的角色只拿基础档，不报错。"""
    _core, agents = _table(str(_project_root(root)))
    return agents.get(role, Entry())


def role_tools(role: str, root: Path | None = None) -> tuple[str, ...]:
    """一个角色这一份名单：基础档加它自己的工具，按出现顺序去重。"""
    base = core_tools(entry(role, root).core, root)
    seen: set[str] = set()
    out: list[str] = []
    for name in (*base, *entry(role, root).tools):
        if name == "*" or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return tuple(out)


def allowed_tools(session: object, root: Path | None = None) -> tuple[str, ...] | None:
    """这个会话这一步能看见哪些工具。`None` 表示不过滤（full 档）。

    根会话（没有角色）取 `orchestrator`；子会话取自己角色的名单；`VSA_TOOLSET=full`
    或路由关掉（`VSA_ROUTE=off`）时不过滤，用来调试与回滚。
    """
    if os.environ.get(ENV, "").strip().lower() == "full":
        return None
    role = str(getattr(session, "agent_role", "") or "").strip()
    if role:
        return role_tools(role, root)
    # 没有角色的会话：根会话给 orchestrator，子会话给 worker。
    # 子会话可能还没写角色（刚建出来），按父子关系判，不按空串一律当根。
    nested = bool(getattr(session, "parent_id", "")) or int(getattr(session, "depth", 0) or 0) > 0
    if not nested and os.environ.get(ROUTE_ENV, "").strip().lower() == "off":
        return None
    base = core_tools("worker" if nested else "orchestrator", root)
    return tuple(name for name in base if name != "*")


def known_roles(root: Path | None = None) -> tuple[str, ...]:
    _core, agents = _table(str(_project_root(root)))
    return tuple(sorted(agents))


def problems(names: Iterable[str], *, root: Path | None = None) -> list[str]:
    """核对配置：名字存在由调用方给（它手上才有注册表）。这里查结构与重复。"""
    found: list[str] = []
    table, agents = _table(str(_project_root(root)))
    for tier, items in table.items():
        if not items:
            found.append(f"core.{tier} 是空的")
        if "*" in items and tier != "full":
            found.append(f"core.{tier} 里只能写具体名字，通配留给 full")
    from vsagent.core.agent.roles.capabilities import ROLES

    for role, item in agents.items():
        if role not in ROLES:
            found.append(f"agents.{role} 不是已知角色")
        if not item.tools:
            found.append(f"agents.{role} 一个工具都没有")
        if "*" in item.tools:
            found.append(f"agents.{role} 不能写通配")
        if item.core == "full":
            found.append(f"agents.{role} 不该用 full 档")
    return found


def reset() -> None:
    """清缓存。判据与测试改过配置之后调用。"""
    _table.cache_clear()
