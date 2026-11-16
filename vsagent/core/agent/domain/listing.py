"""会话登记表的浏览与切换工具。

这几个工具**与领域无关**——它们只操作登记表，手里拿的是受体还是配体集，
它们并不需要知道。放在这里而不是内核那批里的理由：它们是把「收 handle 不收路径」
这条纪律交到模型手上的那一处——从这些工具里拿到 handle，再交给领域工具，全程不碰路径。

**不给模型看登记表的工具，等于登记表白建。** 表建好了却不可见，模型只会退回
用猜的：猜 handle、猜路径，而它猜出来的东西看上去都很合理。

几个工具共用模块级函数，而不是各写一份：

- `render_entries` —— 把一批条目渲染成逐行摘要，超量只报条数
- `path_display` —— 把条目路径渲染成一行文字，目录补一个尾斜杠
- `render_entry_detail` —— 把一个条目展开成几行完整信息，单条详查与批量查询共用

因为**显示与截断的口径只能有一份**：列表工具、切换工具的影响范围、
以及单条详查与批量查询，都走同一套写法。各写一份的话，改了其中一处，
另一处会在长会话里把上下文吃干或给出对不上的路径，而且看上去完全正常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from vsagent.core.agent.state.ledger import (
    _FILE_STATE_NOTES,
    Entry,
    Kind,
    ReceptorHandle,
    SessionLedger,
)
from ..tools import Tool, ToolResult, ToolSpec

# 一次列出的条目上限。超出的只报条数——剩下的该用筛选参数去问，不是靠翻完整清单。
MAX_ENTRIES_INLINE = 50

# `registry` 展开 meta 的字符上限。
#
# meta 是自由形状的，一个配体集往里塞一万条 SMILES 也没人拦——但那属于产物文件
# 的职责，不是回注给模型的内容。
MAX_META_CHARS = 600

# 条目路径为空时显示的那句话。单条详查与列表共用同一个说法。
EMPTY_PATH_NOTE = "（纯内存对象，还没有写盘）"

# 一次批量查询最多展开几个条目的完整信息。留空查全部时按这个数收口，
# 免得一张大登记表把上下文吃光；剩下的编号仍逐条点名，改用单个编号再查。
MAX_LOOKUP_ENTRIES = 20


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


def render_entry_detail(
    entry: Entry,
    ledger: SessionLedger,
    *,
    include_lineage: bool = True,
) -> str:
    """把一个条目展开成几行完整信息：编号、摘要、路径、状态、指纹、血缘、附带字段。

    单条详查（`registry(handles=…)`）都走这里——
    「同一个编号长什么样」只能有一份写法，两处各写一遍迟早会给出对不上的路径
    或文件状态，而那种差异在长会话里看上去完全正常。
    """
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
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class RegistryTool(Tool):
    """登记表的唯一入口：不给编号就列清单，给编号就逐个展开。纯查询。"""

    spec = ToolSpec(
        name="registry",
        description=(
            "登记表里有哪些产物，某个编号指的是哪一份？\n"
            "输入：handles 填编号清单，逐个展开；不填则列清单，可用 kind 与 include_archived 过滤。\n"
            "输出：清单模式报当前受体与全部条目，每行开头就是可直接使用的 handle；"
            "展开模式给出路径、摘要、指纹、血缘、文件状态与附带字段。\n"
            "边界：路径只供核对，传参一律用 handle；文件状态是现核对磁盘的结果，"
            "标出异常的那几条不要直接拿去算。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要展开的编号，例如 [\"receptor#1\", \"artifact#3\"]；"
                        "留空或不给则列出清单"
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["receptor", "ligandset", "artifact"],
                    "description": "清单模式下只看某一类；留空则全看",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": (
                        "清单模式下连被搁置的条目一起列出。切换受体后旧受体的产物"
                        "会被移出默认视图，只有这一项为 true 才看得到它们"
                    ),
                },
                "include_lineage": {
                    "type": "boolean",
                    "description": (
                        "展开模式下是否回溯完整血缘（这个产物由哪些东西一步步算出来的），"
                        "默认 true"
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
                output="内核装配问题：registry 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = arguments.get("handles")
        if raw is not None and not isinstance(raw, list):
            return ToolResult(output="参数 handles 要写成编号清单（数组）。", failed=True)
        wanted = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if wanted:
            return self._detail(ledger, wanted, arguments)
        return self._list(ledger, arguments)

    def _detail(
        self,
        ledger: SessionLedger,
        wanted: list[str],
        arguments: dict[str, Any],
    ) -> ToolResult:
        include_lineage = arguments.get("include_lineage")
        include_lineage = True if include_lineage is None else bool(include_lineage)

        # 与清单模式走同一次核对：两处显示的条目必须来自同一个口径，
        # 否则会出现「清单说还在、展开说没了」这种解释不清的差异。
        ledger.reconcile()

        chunks: list[str] = []
        resolved = 0
        missing: list[str] = []
        overflow: list[str] = []
        for value in wanted:
            try:
                entry = ledger.get(value)
            except Exception as error:  # LedgerError：这个编号没查到，逐条记下
                missing.append(f"  {value}：{error}")
                continue
            resolved += 1
            if len(chunks) >= MAX_LOOKUP_ENTRIES:
                overflow.append(value)
                continue
            chunks.append(render_entry_detail(entry, ledger, include_lineage=include_lineage))

        lines: list[str] = []
        if chunks:
            lines.append(f"查到 {resolved} 个编号，逐个说明如下：")
            lines.append("\n\n".join(chunks))
        else:
            lines.append("一个编号也没查到。先用 registry 列出登记表，确认编号再展开。")
        if missing:
            lines.append(f"以下 {len(missing)} 个编号没有查到：")
            lines.extend(missing)
        if overflow:
            lines.append(
                f"另有 {len(overflow)} 个编号在本次展开上限之外：{'、'.join(overflow)}；"
                "分批再查即可。"
            )
        return ToolResult(
            output="\n".join(lines),
            meta={"requested": len(wanted), "found": resolved, "missing": len(missing)},
        )

    def _list(self, ledger: SessionLedger, arguments: dict[str, Any]) -> ToolResult:
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


class SwitchReceptorTool(Tool):
    """切换当前受体，两阶段：不带 confirm 只预演，带 confirm 才真做。"""

    spec = ToolSpec(
        name="switch_receptor",
        description=(
            "这个会话现在用的是哪一份受体，换一份会作废哪些结果？\n"
            "输入：receptor 填受体 handle；confirm 默认 false，只预演影响范围。\n"
            "输出：预演列出会移出默认视图的条目；confirm=true 才真正切换，文件保留在磁盘上。\n"
            "边界：只改默认作用对象，不删除任何产物；旧产物用 registry(include_archived=true) 仍可查到。"
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
            "用 registry(include_archived=true) 查看。"
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
    return [RegistryTool(), SwitchReceptorTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
