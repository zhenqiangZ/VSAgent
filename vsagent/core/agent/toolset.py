"""按族加载领域工具：核心工具常驻，其余现装现用。

38 个工具的完整定义约 12.3K token，每一步都要发。一轮任务通常只用一两族
（查靶点 + 取结构，或准备受体 + 对接），把没用到的二十几个工具每步都带上，
既占额度也占缓存。所以分成两层：

- **不属于任何族的工具**常驻（14 个，约 3.1K）：文件、登记表、任务、事件、记忆、
  子 agent，加 `load_tools` 自己；
- **领域工具的 12 个族**按需装入：`load_tools("docking")` 之后，这一族从下一步
  起出现在清单里。

装过的族记在 `Session.tool_families` 上，不是记在注册表上：注册表按「根 / 子 agent」
共享两份，记在那里会让一个会话装过的工具漏到另一个会话。会话恢复时它也一起回来，
所以重开一个做过对接的会话，`dock` 还在。

**默认可见**是刻意的：只有登记进 `FAMILIES` 的才算「可以被藏起来」。新加一个工具
忘了登记族，它仍然每步都在，不会出现「写了却没装、模型看不见」那种查不出来的毛病。
"""

from __future__ import annotations

from typing import Any

from ..models import Session
from .domain.all_tools import catalog, families
from .tools import Tool, ToolResult, ToolSpec


def hidden_names(session: Session) -> frozenset[str]:
    """这一步**不该发给模型**的工具名：属于还没装入的那几族。"""
    hidden: set[str] = set()
    for family, names in catalog().items():
        if family not in session.tool_families:
            hidden.update(names)
    return frozenset(hidden)


def family_of(name: str) -> str:
    """工具名属于哪一族；常驻工具返回空串。

    给「模型叫了一个看不见的工具」那一句提示用：它看不见 `dock` 多半是因为
    `docking` 族还没装，而不是这个工具不存在——两句话该让模型做的事不一样。
    """
    for family, names in catalog().items():
        if name in names:
            return family
    return ""


def catalog_text() -> str:
    """一族一行「族名：这一族里有哪些工具」。

    从 `catalog()` 现数，**不手写**。原先这段说明只列族名，模型得靠猜：它以为
    结构检索在 `structure` 里，装完发现只有 `fetch_structure`；而手写的那份说明
    也已经漂过三处（`structure` 多写了 `search_pdb`、`listing` 与 `chembl` 各漏一个）。
    模型是照着这句话决定装哪一族的，它错一次就是几步。
    """
    return "\n".join(
        f"- {family}：{'、'.join(names)}" for family, names in catalog().items()
    )


class LoadToolsTool(Tool):
    """把一族领域工具装进本会话的工具清单。"""

    spec = ToolSpec(
        name="load_tools",
        description=(
            "按族装入领域工具。文件、登记表、任务、事件、记忆这些核心工具一直在；"
            "其余工具分成 "
            f"{len(families())} 族，要先用这里装进来，**下一步起**才出现在工具清单里。\n"
            "族与工具：\n"
            + catalog_text()
            + "\n装一次对整个会话有效；已经装过的再装一次只会把清单再列一遍。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "family": {
                    "type": "string",
                    "enum": [family for family, _modules in families()],
                    "description": "要装入的族",
                },
            },
            "required": ["family"],
            "additionalProperties": False,
        },
    )
    session_argument = "session"
    # 只为读一件事：哪些工具被 `tools.toml` 关掉了。不读它就会对着关掉的工具
    # 说「已装入」——模型照着这句话去调，撞上禁用文案之后转去 bash 自己实现了一遍
    # （实测烧掉 20 步）。
    registry_argument = "registry"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """装一族，返回这一族里**真的装得进来**的工具。"""
        session = arguments.get("session")
        registry = arguments.get("registry")
        disabled = getattr(registry, "disabled", frozenset())
        family = str(arguments.get("family") or "").strip()
        names = catalog().get(family)
        if names is None:
            return ToolResult(
                output=f"没有这一族：{family}。可用：{'、'.join(catalog())}",
                failed=True,
            )
        # 被关掉的工具在装配时根本没进注册表，报「已装入」就是说了句做不到的话。
        available = tuple(name for name in names if name not in disabled)
        skipped = tuple(name for name in names if name in disabled)
        if not available:
            return ToolResult(
                output=(
                    f"{family} 族里的工具都被项目配置关掉了（{ '、'.join(skipped) }），"
                    "装进来也用不了。\n"
                    "这是 `.opencode_tui/tools.toml` 里的开关，不是网络或参数问题；"
                    "不要用 bash 自己实现它，也不要换数据源绕过——"
                    "要么请用户在 /tools 面板里打开，要么按用户的指示换方案。"
                ),
                failed=True,
                meta={"family": family, "tools": [], "disabled": list(skipped)},
            )
        already = family in session.tool_families
        if not already:
            session.tool_families.append(family)
        prefix = "已经装过" if already else "已装入"
        listed = "、".join(available) + (
            f"（{'、'.join(skipped)} 被项目配置关掉了，没有列进来）" if skipped else ""
        )
        return ToolResult(
            output=(
                f"{prefix} {family}：{listed}。"
                "下一步起这些工具出现在清单里，参数按各工具的说明给。"
            ),
            meta={
                "family": family,
                "tools": list(available),
                "disabled": list(skipped),
                "already": already,
            },
        )
