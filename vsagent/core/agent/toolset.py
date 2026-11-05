"""按族加载领域工具：核心工具常驻，其余现装现用。

领域工具的完整定义合计上万 token，每一步都要发。一轮任务通常只用一两族
（查靶点 + 取结构，或准备受体 + 对接），把没用到的几十个工具每步都带上，
既占额度也占缓存。所以分成两层：

- **不属于任何族的工具**常驻：文件、登记表、任务、假设、事件、记忆、子 agent、
  开工前的盘点、呈现（`write_run` / `open_viewer`），加 `load_tools` 自己；
- **领域工具按族装入**：`load_tools("scoring")` 之后，这一族从下一步起出现在清单里。

呈现工具常驻是有原因的：它们属于交付通道，而口袋阶段装的是 pocket / structure，
把 `write_run` 放进领域族时，模型看不见规则点名的那个工具。核心工具多两个的定义，
换掉的是一次「看得见规则、摸不到工具」的失败步。

装过的族记在 `Session.tool_families` 上，不是记在注册表上：注册表按「根 / 子 agent」
共享两份，记在那里会让一个会话装过的工具漏到另一个会话。会话恢复时它也一起回来，
所以重开一个做过对接的会话，`dock` 还在。

**默认可见**是刻意的：只有登记进 `FAMILIES` 的才算「可以被藏起来」。新加一个工具
忘了登记族，它仍然每步都在，不会出现「写了却没装、模型看不见」那种查不出来的毛病。
"""

from __future__ import annotations

from typing import Any, Iterable

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
    `scoring` 族还没装，这个工具本身存在——两句话该让模型做的事不一样。
    """
    for family, names in catalog().items():
        if name in names:
            return family
    return ""


def prune_families(session: Session, used: Iterable[str], topic_index: int) -> list[str]:
    """轮末封存时收敛工具族：这一轮没用过的族卸掉，返回卸了哪些。

    只有「装」没有「卸」的话，一次会话装到八族之后每一步都发 12K token 的
    工具定义，而典型的一轮只用得上两三个族（准备受体的那一轮不需要
    `docking`）。轮末封存天然是「换事情」的地方，所以收敛挂在那里。

    **主题封存不收敛**：一轮里换三个主题是常事，每次装卸都改动请求体，
    整段缓存失效，省下的定义抵不过反复打断缓存。

    三条边界：

    - **这一轮一个工具都没用过就不卸**。那多半是纯聊天的一轮，没有依据说
      「这些族用不上」，而卸了要模型下一步重新装一次，那一步的缓存全废。
    - **这一轮刚装的族不卸**。`session.family_topic` 记着每一族是第几个主题
      装进来的；模型常常先装后几步才用，装完立刻被收走等于白装一次。
    - **卸掉不等于忘掉**：`load_tools` 随时能装回来。这是刻意的取舍——
      省下的是每步都发的固定成本，代价是换轮时一次缓存失效。
    """
    used = set(used)
    if not used:
        return []
    table = catalog()
    keep: list[str] = []
    dropped: list[str] = []
    for family in session.tool_families:
        names = set(table.get(family, ()))
        loaded_at = session.family_topic.get(family, 0)
        if names & used or loaded_at >= topic_index:
            keep.append(family)
        else:
            dropped.append(family)
    if dropped:
        session.tool_families = keep
        session.family_topic = {
            name: value for name, value in session.family_topic.items() if name in keep
        }
        session.touch()
    return dropped


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
            + "\n装一次对整个会话有效；已经装过的再装一次只会把清单再列一遍。\n"
            "**建议一次把这一轮要用的族都装上**：每装一次都会改动工具清单，"
            "而工具清单是请求前缀的一部分，下一步的 prompt cache（前缀缓存）会整段失效。"
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
                    "这是 `.vsagent/tools.toml` 里的开关，不是网络或参数问题；"
                    "不要用 bash 自己实现它，也不要换数据源绕过——"
                    "要么请用户在 /tools 面板里打开，要么按用户的指示换方案。"
                ),
                failed=True,
                meta={"family": family, "tools": [], "disabled": list(skipped)},
            )
        already = family in session.tool_families
        if not already:
            session.tool_families.append(family)
        # 记下它是第几个主题装进来的：收敛时不卸「刚装、还没轮到用」的族，
        # 否则模型刚装完就被拿走，下一步还得再装一次（那一次缓存又白费）。
        session.family_topic[family] = session.next_topic_index
        prefix = "已经装过" if already else "已装入"
        listed = "、".join(available) + (
            f"（{'、'.join(skipped)} 被项目配置关掉了，没有列进来）" if skipped else ""
        )
        # 第一次装才提醒：装过的族再提一次「一次装完」只会变成噪音。
        hint = (
            ""
            if already
            else (
                "\n**这一段还要用别的族的话，现在就一起装**："
                "每装一次都会改动工具清单，下一步的 prompt cache（前缀缓存）会整段失效。"
            )
        )
        return ToolResult(
            output=(
                f"{prefix} {family}：{listed}。"
                "下一步起这些工具出现在清单里，参数按各工具的说明给。" + hint
            ),
            meta={
                "family": family,
                "tools": list(available),
                "disabled": list(skipped),
                "already": already,
            },
        )
