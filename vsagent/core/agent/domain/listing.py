"""会话登记表的浏览与切换工具。

这三个工具**与领域无关**——它们只操作登记表，手里拿的是受体还是配体集，
它们并不需要知道。放在这里而不是内核那批里的理由：它们是把「收 handle 不收路径」
这条纪律交到模型手上的那一处——从这三个工具里拿到 handle，再交给领域工具，全程不碰路径。

**不给模型看登记表的工具，等于登记表白建。** 表建好了却不可见，模型只会退回
用猜的：猜 handle、猜路径，而它猜出来的东西看上去都很合理。

三个工具共用一个中间层：

- `render_entries` —— 把一批条目渲染成逐行摘要，超量只报条数
- `path_display` —— 把条目路径渲染成一行文字，目录补一个尾斜杠

这两个都是模块级函数而不是各自的私有方法，因为**截断口径只能有一份**：
列表工具、切换工具的影响范围、以及将来其它要列条目的地方，都走同一个 `render_entries`。
各写一份的话，改了其中一处，另一处会在长会话里把上下文吃干，而且看上去完全正常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    _FILE_STATE_NOTES,
    Entry,
    Kind,
    ReceptorHandle,
    SessionLedger,
)
from ..tools import Tool, ToolResult, ToolSpec

# 一次列出的条目上限。超出的只报条数——剩下的该用筛选参数去问，不是靠翻完整清单。
MAX_ENTRIES_INLINE = 50

# `describe_entry` 展开 meta 的字符上限。
#
# meta 是自由形状的，一个配体集往里塞一万条 SMILES 也没人拦——但那属于产物文件
# 的职责，不是回注给模型的内容。
MAX_META_CHARS = 600

# 条目路径为空时显示的那句话。单条详查与列表共用同一个说法。
EMPTY_PATH_NOTE = "（纯内存对象，还没有写盘）"


# ---------------------------------------------------------------------------
# 两个中间层
# ---------------------------------------------------------------------------
def render_entries(
    entries: Sequence[Entry],
    *,
    limit: int = MAX_ENTRIES_INLINE,
) -> str:
    """把一批条目渲染成逐行摘要，超量只报条数。

    公开出来是因为**截断口径只能有一份**：列表工具走这里，切换工具的影响范围
    也走这里。两边各写一份的话，改了其中一处，另一处就会在长会话里把上下文吃干
    ——而且看上去完全正常。

    不截断的话，一个「列出全部产物」的工具在长会话里会把上下文吃光，
    而模型从第 50 行往后读到的信息量是零。
    """
    cap = max(1, limit)
    lines = [f"- {entry.brief()}" for entry in entries[:cap]]
    if len(entries) > cap:
        lines.append(f"…（共 {len(entries)} 项，此处只列前 {cap} 项）")
    return "\n".join(lines)


def path_display(path: Path | None, *, empty: str = EMPTY_PATH_NOTE) -> str:
    """把条目路径渲染成一行文字，**目录补一个尾斜杠**。

    这一笔不是装饰。「文件还是目录」是模型要看到的一等信息：`receptor#1`
    是一个 `.pdb` 文件（原始结构，还不能对接）还是一个目录（准备后的受体，
    里面是结构 + 参数 + 体检报告），决定了它能不能交给下一个工具。
    两者在路径字符串上长得一样，所以差异只能由这里补出来。

    单条详查才调用（要 stat 一次）；列表里不调——一屏几十行不值得为它逐个 stat。
    """
    if path is None:
        return empty
    try:
        if path.is_dir():
            return path.as_posix().rstrip("/") + "/"
    except OSError:
        # 路径问不出类型（超长、非法字符）：原样给出，反正对账那一步会把它标出来
        return path.as_posix()
    return path.as_posix()


def _file_state_note(state: str) -> str:
    """把状态码翻成一句话，认不出来的码原样带出去。"""
    return _FILE_STATE_NOTES.get(state, state)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class ListWorkspaceTool(Tool):
    """列出登记表里的条目，并报告当前受体是哪一份。纯查询，不登记任何东西。"""

    spec = ToolSpec(
        name="list_workspace",
        description=(
            "列出当前会话登记表里已登记的资源与产物，并报告**当前受体**是哪一份。"
            "在引用任何产物之前先看它——不要凭记忆写 handle，也不要手写文件路径，"
            "返回的每一行开头就是可以直接拿去用的 handle。"
            "输出里 receptor 那一项决定后续工具的默认作用对象。"
            "本工具会**现核对一遍磁盘**：文件被删或被改的条目会在行尾标出来，"
            "别拿它们去算——那会拿一个不存在的输入去启动一次昂贵的计算。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["receptor", "ligandset", "artifact"],
                    "description": "只看某一类；留空则全看",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": (
                        "是否连被搁置的条目一起列出。切换受体后旧受体的产物会被移出"
                        "默认视图，只有这一项为 true 才看得到它们"
                    ),
                },
            },
        },
    )
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：list_workspace 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        kind = _read_kind(arguments.get("kind"))
        if kind is _BAD_KIND:
            return ToolResult(
                output=(
                    "参数 kind 只认 receptor / ligandset / artifact 之一，"
                    f"收到 {arguments.get('kind')!r}。留空则列出全部。"
                ),
                failed=True,
            )
        include_archived = bool(arguments.get("include_archived"))

        current = ledger.current_receptor()
        # 先对账再列：条目里的 path 是**登记当时**的事实，磁盘上早就可能变了，
        # 而两者在没核对之前长得一模一样。这一步是只读的，不会改存档。
        ledger.reconcile()
        entries = ledger.find(kind=kind, include_archived=include_archived)

        lines = [f"当前受体：{current if current else '（尚未设定）'}"]
        if not entries:
            scope = f"类型为 {kind} 的条目" if kind else "任何条目"
            lines.append(f"登记表里还没有{scope}。先用工具产出一个，再回来引用它。")
            lines.append("引用产物一律用 handle，不要手写路径。")
            return ToolResult(
                output="\n".join(lines),
                meta={"total": 0, "current_receptor": str(current) if current else ""},
            )

        lines.append(f"共 {len(entries)} 项：")
        lines.append(render_entries(entries))

        # 单独再点一遍名：`render_entries` 有条数上限，被截断时恰好可能把出问题的
        # 那几条截掉，而「哪些不能用」是这份清单里最不能漏的信息。
        broken = [entry for entry in entries if entry.file_state != "ok"]
        if broken:
            lines.append(
                f"其中 {len(broken)} 项的产物文件与登记时对不上，**不要**直接拿去用"
                "（重新产出一份，或先确认磁盘上发生了什么）："
            )
            lines.extend(
                f"  {entry.handle}：{_file_state_note(entry.file_state)}" for entry in broken
            )

        if include_archived:
            lines.append("（含被搁置的条目：它们属于已切换掉的旧受体）")
        return ToolResult(
            output="\n".join(lines),
            meta={
                "total": len(entries),
                "broken": len(broken),
                "current_receptor": str(current) if current else "",
            },
        )


class DescribeEntryTool(Tool):
    """查看某一个条目的详细信息。纯查询，不登记任何东西。"""

    spec = ToolSpec(
        name="describe_entry",
        description=(
            "查看登记表里某一条目的详细信息：路径、摘要、指纹、血缘、文件状态与附带字段。"
            "在引用某个产物之前用它确认那确实是你要的那个——同类产物在会话里会越攒越多，"
            "看摘要比看序号可靠。其中的路径仅供核对，**传参请继续用 handle**。"
            "路径末尾带 / 的是**目录**（一组产物，例如准备后的受体）；不带的是单个文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "条目引用，例如 ligandset#3。**不要传文件路径**",
                },
                "include_lineage": {
                    "type": "boolean",
                    "description": "是否回溯完整血缘（这个产物是由哪些东西一步步算出来的）",
                },
            },
            "required": ["handle"],
        },
    )
    primary_argument = "handle"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：describe_entry 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = arguments.get("handle")
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(output="缺少参数 handle（形如 ligandset#3）", failed=True)
        include_lineage = arguments.get("include_lineage")
        include_lineage = True if include_lineage is None else bool(include_lineage)

        # 与 list_workspace 走同一次核对：两处显示的状态必须来自同一个口径，
        # 否则会出现「列表说还在、详查说没了」这种解释不清的差异。
        ledger.reconcile()
        try:
            entry = ledger.get(raw)
        except Exception as error:  # LedgerError：交回去让模型改参数
            return ToolResult(output=str(error), failed=True)

        lines = [
            f"{entry.handle}",
            f"  摘要：{entry.summary or '（无）'}",
            f"  路径：{path_display(entry.path)}",
            # 正常也明说：单条详查就是要「一眼确认能不能用」，
            # 而缺字段与状态正常在输出里长得一样
            f"  文件状态：{_file_state_note(entry.file_state)}",
            f"  指纹：{entry.fingerprint or '（未计算）'}",
        ]
        if entry.parents:
            lines.append("  直接来源：" + "、".join(str(item) for item in entry.parents))

        if include_lineage:
            ancestors = ledger.lineage(entry.handle)
            if ancestors:
                chain = " ← ".join(str(item.handle) for item in [entry, *ancestors])
                lines.append(f"  完整血缘：{chain}")

        if entry.meta:
            payload = json.dumps(entry.meta, ensure_ascii=False, sort_keys=True, default=str)
            if len(payload) > MAX_META_CHARS:
                payload = f"{payload[:MAX_META_CHARS]}…（已截断）"
            lines.append(f"  附带字段：{payload}")

        return ToolResult(
            output="\n".join(lines),
            meta={"handle": str(entry.handle), "file_state": entry.file_state},
        )


class SwitchReceptorTool(Tool):
    """切换当前受体，两阶段：不带 confirm 只预演，带 confirm 才真做。"""

    spec = ToolSpec(
        name="switch_receptor",
        description=(
            "把会话的**当前受体**换成另一个，并报告哪些旧结果会因此失效。"
            "这会改变后续工具的默认作用对象：属于旧受体的产物会被移出登记表的默认视图。"
            "它们**不会被删除**，磁盘上的文件原样保留，之后仍能用 "
            "list_workspace(include_archived=true) 查到。"
            "**默认只做预演**，带 confirm=true 才真正切换。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "receptor": {
                    "type": "string",
                    "description": "目标受体，例如 receptor#2。**不要传文件路径**",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "true 才真正执行；false（默认）只预演影响范围",
                },
            },
            "required": ["receptor"],
        },
    )
    primary_argument = "receptor"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：switch_receptor 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = arguments.get("receptor")
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(output="缺少参数 receptor（形如 receptor#2）", failed=True)
        confirm = bool(arguments.get("confirm"))

        try:
            target = ledger.get(raw)
            if not isinstance(target.handle, ReceptorHandle):
                return ToolResult(
                    output=(
                        f"参数 receptor 要的是受体，但 {target.handle} 属于 "
                        f"{target.handle.kind}。请检查 handle 是否拿错了。"
                    ),
                    failed=True,
                )
        except Exception as error:  # LedgerError：交回去让模型改参数
            return ToolResult(output=str(error), failed=True)

        current = ledger.current_receptor()
        if current is not None and str(current) == str(target.handle):
            return ToolResult(
                output=f"当前受体已经是 {target.handle}（{target.summary or '无摘要'}），无需切换。",
                meta={"changed": False, "affected": 0},
            )

        # 影响范围**由登记表自己算**（`stale_after`），不在这里重写一遍。
        #
        # 理由不是「少写几行」，而是**预演的数字必须等于真做的数字**——两阶段确认
        # 的全部价值就在这一点上。曾经这里只看直接父项，于是隔层的产物
        # （父项是 ligandset、再往上才是受体）会被搁置却从不出现在预演里：
        # 预演说 3 项，真做说 4 项，多出来的那个**从没被宣布过**。
        #
        # 判「会失效」的依据是血缘，不是名字：产物登记时默认挂当前受体为父项，
        # 所以血缘能追溯到旧受体的就是属于它的。没有血缘的产物无法被识别出来，
        # 这也是登记时自动挂父项那条默认值的意义。
        affected = ledger.stale_after(current) if current is not None else []

        if not confirm:
            lines = [
                f"预演：把当前受体 {current or '（未设定）'} 换成 {target.handle}"
                f"（{target.summary or '无摘要'}）。",
                f"以下 {len(affected)} 项会随切换移出默认视图（不删除，磁盘上的文件保留；"
                "含旧受体自身）：",
                render_entries(affected) if affected else "-（没有）",
                "确认无误后，带 confirm=true 重新调用本工具即可完成切换。",
            ]
            return ToolResult(
                output="\n".join(lines),
                meta={"changed": False, "affected": len(affected), "preview": True},
            )

        archived = ledger.switch_receptor(target.handle)
        lines = [f"已切换到 {target.handle}（{target.summary or '无摘要'}）。"]
        if archived:
            lines.append(f"以下 {len(archived)} 项已搁置（文件仍在磁盘上）：")
            lines.append(render_entries(archived))
        else:
            lines.append("之前没有别的受体，因此没有条目被搁置。")
        lines.append(
            "后续工具默认作用在新受体上。如需引用被搁置的旧产物，"
            "用 list_workspace(include_archived=true) 查看。"
        )
        return ToolResult(
            output="\n".join(lines),
            meta={"changed": True, "affected": len(archived)},
        )


class _BadKind:
    """`kind` 参数写错时的哨兵值（可以是 None 之外的任何非字符串标记）。"""

    def __repr__(self) -> str:  # pragma: no cover - 只用于防御性报错
        return "<bad kind>"


_BAD_KIND = _BadKind()


def _read_kind(raw: Any) -> Kind | None | _BadKind:
    """读 `kind` 参数：留空返回 None，认不出来的返回哨兵。

    **不在这里抛异常**：它是模型可能写错的参数，报错要能回灌给它自纠。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        if candidate in ("receptor", "ligandset", "artifact"):
            return candidate  # type: ignore[return-value]
    return _BAD_KIND


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具，按装配顺序。"""
    return [ListWorkspaceTool(), DescribeEntryTool(), SwitchReceptorTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
