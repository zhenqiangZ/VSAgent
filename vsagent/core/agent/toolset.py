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

主 agent 那一边还有一条：这一轮有可执行计划时，`hidden_names` 把**全部族**都藏掉
（`Session.principal_only`）。计划里的族按角色分，装到根会话上只会让它自己把领域
工作做掉；`load_tools` 在同一个会话上也直接退回「派给角色」。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..models import Session
from .domain.all_tools import catalog, families
from vsagent.core.agent.roles.prompt import estimate_tokens
from .tools import Tool, ToolResult, ToolSpec


def hidden_names(session: Session) -> frozenset[str]:
    """这一步**不该发给模型**的工具名。

    两种藏法，理由不同：

    - 领域族没装入这一族：按族藏，装一次就能看见；
    - 这一轮主 agent 只编排（`principal_only`）：**全部领域族都藏**，装过也不发。
      计划里的族是给派出去的角色用的，主 agent 拿到它们只会自己把活干了。
    """
    if getattr(session, "principal_only", False):
        hidden: set[str] = set()
        for names in catalog().values():
            hidden.update(names)
        return frozenset(hidden)
    loaded = set(expand_families(session.tool_families))
    hidden = set()
    for family, names in catalog().items():
        if family not in loaded:
            hidden.update(names)
    return frozenset(hidden)


# 旧族名：只出现在旧会话的 `tool_families` 与 `next_tools` 里。
#
# 2026-11 把 `scoring` 拆成 `docking` 与 `affinity` 之后，读过旧会话的人会带着
# 这个名字回来。不认它的话，那次会话里 dock / fit_affinity / score_affinity 会
# 突然从清单里消失，而模型看不出原因——它会当成「工具没了」，转去用 bash。
LEGACY_FAMILIES: dict[str, tuple[str, ...]] = {
    "scoring": ("docking", "affinity"),
}


def expand_families(names: Iterable[str]) -> list[str]:
    """把旧族名展开成新族名：保留顺序、去重、不认识的照原样留着交给调用方报错。"""
    out: list[str] = []
    for name in names:
        value = str(name or "").strip()
        for item in LEGACY_FAMILIES.get(value, (value,)):
            if item and item not in out:
                out.append(item)
    return out


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

    **生产路径已经改用 `release_families`**：主题结束一律释放，不再看
    「这一轮用过没有」。本函数保留给旧调用与对照测试。

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


def release_families(session: Session, keep: Iterable[str] = ()) -> list[str]:
    """主题结束时释放领域族，只保留 `keep` 里的（下一主题要预装的）。

    与 `prune_families` 的区别是**不再看「这一族用过没有」**：主题就是一次
    工作的边界，过了边界就应当从核心工具重新开始。实测一次装 10 族之后
    每一步的工具定义是 25,870 token，比正文还大；按主题释放能把第二个主题
    压回「核心 8,409 + 1–3 族」。

    `keep` 里的族会被**预装**（即使之前没装过）：下一主题的第一步就能用，
    不必再花一步 `load_tools`。未知族名忽略，不因为一个笔误把整批丢掉。
    """
    table = catalog()
    wanted: list[str] = []
    for value in keep:
        name = str(value or "").strip()
        if name in table and name not in wanted:
            wanted.append(name)
    dropped = [name for name in session.tool_families if name not in wanted]
    previous = list(session.tool_families)
    previous_topics = dict(session.family_topic)
    session.tool_families = wanted
    session.family_topic = {
        name: session.next_topic_index for name in session.tool_families
    }
    if session.tool_families != previous or session.family_topic != previous_topics:
        session.touch()
    return dropped


def family_tokens(registry: Any, tool_names: Iterable[str]) -> int:
    """估这些**工具**的定义占多少 token。

    参数是工具名，不是族名——名字原先叫 `names`，调用方很容易把族名传进来，
    结果永远是 1 token（一个都没匹配上）。要用族名，先经 `catalog()` 换成工具名。
    """
    wanted = set(tool_names)
    specs = [
        spec
        for spec in registry.specs()
        if spec.get("function", {}).get("name") in wanted
    ]
    return estimate_tokens(json.dumps(specs, ensure_ascii=False))


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
            f"{len(families())} 族（见参数 enum），要先用这里装进来，"
            "**下一步起**才出现在工具清单里。\n"
            "**不带参数调用一次**列出每族包含哪些工具——先看再装，不要凭族名猜。\n"
            "装一次只管**这一主题**，主题结束自动卸载，只保留"
            " `close_topic(next_tools=...)` 点名的族。通常一次装一到三族：装一次会让下一步的"
            "前缀缓存整段失效，装太多则每一步都占额度——实测十族约 2.6 万 token。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "family": {
                    "type": "string",
                    "enum": [family for family, _modules in families()],
                    "description": "要装入的族（单族）；多族用 families",
                },
                "families": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [family for family, _modules in families()],
                    },
                    "minItems": 1,
                    "description": "要一次装入的族（数组）；与 family 二选一",
                },
            },
            "additionalProperties": False,
        },
    )
    session_argument = "session"
    # 只为读一件事：哪些工具被 `tools.toml` 关掉了。不读它就会对着关掉的工具
    # 说「已装入」——模型照着这句话去调，撞上禁用文案之后转去 bash 自己实现了一遍
    # （实测烧掉 20 步）。
    registry_argument = "registry"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """装入一族或几族，返回**真的装得进来**的工具。

        多族一次装完：每装一次都会改工具清单，而清单是请求前缀的一部分。
        先校验全部族能不能装，再一起写进会话，避免前几族已经生效、后一族失败。
        """
        session = arguments.get("session")
        registry = arguments.get("registry")
        disabled = getattr(registry, "disabled", frozenset())

        if getattr(session, "principal_only", False):
            # 主 agent 这一轮有可执行计划：领域动作已经分给角色。装进来也看不见
            # （`hidden_names` 会藏掉全部族），与其让模型以为装上了，不如当场说清。
            return ToolResult(
                output=(
                    "主 agent 这一轮只做编排，不装领域工具。"
                    "需要取数就把这件事派给承担该能力的角色（dispatch_campaign）；"
                    "角色拿的是能力计划里的族，不需要你预装。"
                ),
                failed=True,
            )

        single = str(arguments.get("family") or "").strip()
        many = arguments.get("families")
        if single and many is not None:
            return ToolResult(output="family 与 families 只能给一个。", failed=True)
        if many is None:
            raw = [single] if single else []
        elif isinstance(many, list):
            raw = many
        else:
            return ToolResult(output="families 要写成数组，例如 [\"structure\", \"chembl\"]。", failed=True)

        wanted: list[str] = []
        for value in raw:
            name = str(value or "").strip()
            if name and name not in wanted:
                wanted.append(name)
        if not wanted:
            # 不带参数 = 看目录。族 → 工具那张表有 60 行，写进每一步都发的说明里
            # 要占 500 多 token；改成调用时现给，模型照样查得到，常驻额度省下来。
            return ToolResult(
                output=(
                    "要装哪一族？下面是每一族包含的工具；装的时候把族名传给 family"
                    "（多族用 families）：\n" + catalog_text()
                )
            )
        # 旧族名先展开：`scoring` 现在是 `docking` 与 `affinity` 两族。
        wanted = expand_families(wanted)

        known = catalog()
        unknown = [name for name in wanted if name not in known]
        if unknown:
            return ToolResult(
                output=f"没有这些族：{'、'.join(unknown)}。可用：{'、'.join(known)}",
                failed=True,
            )

        plans: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        for family in wanted:
            names = known[family]
            available = tuple(name for name in names if name not in disabled)
            skipped = tuple(name for name in names if name in disabled)
            if not available:
                # 被关掉的工具在装配时根本没进注册表，报「已装入」就是说了句做不到的话。
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
            plans.append((family, available, skipped))

        lines: list[str] = []
        tools: list[str] = []
        skipped_all: list[str] = []
        already_map: dict[str, bool] = {}
        any_new = False
        for family, available, skipped in plans:
            already = family in session.tool_families
            already_map[family] = already
            if not already:
                session.tool_families.append(family)
                any_new = True
            # 记下它是第几个主题装进来的：收敛时不卸「刚装、还没轮到用」的族，
            # 否则模型刚装完就被拿走，下一步还得再装一次（那一次缓存又白费）。
            session.family_topic[family] = session.next_topic_index
            prefix = "已经装过" if already else "已装入"
            listed = "、".join(available) + (
                f"（{'、'.join(skipped)} 被项目配置关掉了，没有列进来）"
                if skipped
                else ""
            )
            cost = family_tokens(registry, available)
            lines.append(f"{prefix} {family}（约 {cost} token）：{listed}。")
            tools.extend(available)
            skipped_all.extend(skipped)

        # 装得太多时把价格说出来：这一句是模型决定「还要不要再装一族」的依据。
        loaded_names = [
            name
            for family in session.tool_families
            for name in known.get(family, ())
        ]
        loaded_cost = family_tokens(registry, loaded_names) if loaded_names else 0
        warning = ""
        if len(session.tool_families) > 3:
            warning = (
                f"\n**本主题已装 {len(session.tool_families)} 族，工具定义约 "
                f"{loaded_cost} token**；主题结束会自动卸载没有写进 "
                "`close_topic(next_tools=...)` 的族。"
            )

        # 第一次装才提醒：装过的族再提一次「一次装完」只会变成噪音。
        hint = (
            "\n**这一段还要用别的族的话，现在就一起装**："
            "每装一次都会改动工具清单，下一步的 prompt cache（前缀缓存）会整段失效。"
            if any_new
            else ""
        )
        meta: dict[str, Any] = {
            "families": wanted,
            "tools": tools,
            "disabled": skipped_all,
            "already": already_map,
        }
        if len(wanted) == 1:
            meta["family"] = wanted[0]
            meta["already"] = already_map[wanted[0]]
        return ToolResult(
            output=(
                "\n".join(lines)
                + "下一步起这些工具出现在清单里，参数按各工具的说明给。"
                + hint
                + warning
            ),
            meta=meta,
        )
