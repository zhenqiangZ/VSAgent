from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..agent.prompt import is_key_line
from ..models import Part, PartType, ToolStatus

# 限额按**条数**而不是 token 定：证据的价值在「索引还在」，索引是短的一行行，
# 条数卡住就够；正文那一部分由装配器按预算展开，不必在这里再算一遍 token，
# 也算不准——同一批证据在不同窗口下展开的程度本来就不同。
#
# 12 → 24 是量出来的（`tools/eval_context.py --evidence --evidence-runs 40`）：
# 覆盖能力约等于「这一条数 + RECENT 还能放几条」，所以拐点就在第十三次调用，
# 之后每多一次调用线性丢一条。而整层只用了 330 token / 6000 的封顶——
# **卡住的是条数不是额度**，翻倍只多约 330 token，比加窗口划算得多
# （12K → 16K 多花 4K 上下文，只买回一两次覆盖）。
EVIDENCE_LIMIT = 24
# 一条证据的摘要与引用名各自的长度上限。两者都要塞进一行里。
EVIDENCE_SUMMARY_LIMIT = 120
EVIDENCE_REF_LIMIT = 60

# 工具结果的引用名取哪个参数做定位。按这个顺序找第一个非空的。
#
# 同一个文件反复读、同一条命令反复跑，引用名相同就替换而不是堆一摞，
# 否则读三次同一个文件就把证据层占掉三条。
EVIDENCE_REF_ARGUMENTS: tuple[str, ...] = (
    "path", "file", "structure", "receptor", "query", "command",
)

# 引用里一个参数值留多少字。20 是量出来的——大多数 path / pattern 在这个长度内
# 已经能区分开，再长就把 60 字的整条引用挤满。
REF_VALUE_CHARS = 20

# 结构化读数里值得进摘要的计数键。
#
# 用白名单而不是「把所有数字都拼上」：meta 里还有端口、字节数、运行编号这类
# 与「这次产出了什么」无关的数字，拼进去只会把 120 字的摘要挤满。
META_COUNT_KEYS: tuple[str, ...] = (
    "count", "total", "docked", "pdb", "failed", "remaining", "pages", "chars",
)


def one_line(text: str, limit: int) -> str:
    """把一段多行文字压成一行并截断。

    摘要与引用名都要求是一行：它们要和别的行并排放在证据块里，
    夹进来一个换行会把那条记录切成两半，读的人分不清下半截属于谁。
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


@dataclass(frozen=True)
class Evidence:
    """一条按需证据。ref 是可追溯的轻量标识，detail 才是正文。

    「按需」体现在两处：只有本步相关的才进上下文；进了之后先给 summary，
    正文 detail 只在预算允许时展开。ref 留在历史层，之后还能取回。
    """

    ref: str          # handle（receptor#1）或 路径#行号 或 URL
    summary: str      # 一行摘要，永远给模型看
    detail: str = ""  # 正文，预算允许时才展开
    # 这条来自一次失败的调用。失败原因就是下一步的输入，所以展开正文时
    # 它排在最新之前——先让模型看见「为什么不行」，再看「刚才跑出了什么」。
    failed: bool = False
    # 来自哪条 Part。空串表示不是工具结果（`recall` 取回来的记忆）。
    # 装配时拿它与 RECENT 里还完整可见的工具结果比对，避免同一段文字发两遍。
    source: str = ""


def ref_value(value: str, width: int = REF_VALUE_CHARS) -> str:
    """引用里的一个参数值：超长截断，**截断了就带一段指纹**。

    只截断不带指纹，两条长命令的前二十个字往往一模一样（`python tools/check.py
    --round 2` 与 `--round 7`），ref 就撞成同一条；而 `EvidenceStore.add` 是按
    ref **替换**的，撞了等于前一条证据无声消失。实测 20 次工具运行里，4 次 bash
    只剩最后一条的证据——这正是证据探针（`tools/eval_context.py --evidence`）
    第一次跑就抓到的。
    """
    flat = " ".join(value.split())
    if len(flat) <= width:
        return flat
    digest = hashlib.sha256(flat.encode("utf-8")).hexdigest()[:4]
    return f"{one_line(flat, width)}~{digest}"


def evidence_ref(tool_name: str, tool_input: Any) -> str:
    """一条工具结果的引用名：工具名 + 它的定位参数。

    定位参数按 `EVIDENCE_REF_ARGUMENTS` 的顺序找第一个非空字符串。
    一个都找不到时只留工具名——那时同名工具的不同调用会互相替换，
    这是可接受的：没有定位参数就意味着这次调用本身没有可区分的对象。
    """
    if not isinstance(tool_input, dict):
        return tool_name
    for key in EVIDENCE_REF_ARGUMENTS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name} {one_line(value, EVIDENCE_REF_LIMIT)}"
    return tool_name


def meta_summary(meta: dict[str, Any]) -> str:
    """把工具返回的结构化读数压成一行摘要；没有可说的返回空串。"""
    if not meta:
        return ""
    parts: list[str] = []
    handle = meta.get("handle")
    if isinstance(handle, str) and handle.strip():
        parts.append(handle.strip())
    counts = meta.get("counts")
    if isinstance(counts, dict):
        pairs = [
            f"{name} {value}"
            for name, value in counts.items()
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        if pairs:
            parts.append("，".join(pairs))
    for key in META_COUNT_KEYS:
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            parts.append(f"{key} {value}")
    return "；".join(parts)


def evidence_ref_of(part: Part, meta: dict[str, Any], tool: Any = None) -> str:
    """一次工具调用在证据层里的引用名。

    三档，从「最说明问题」到「最通用」：

    1. 结果里的 handle —— 这次调用**产出了什么**。两个不同的入参可能产出同一个
       产物，那时它们本该是同一条证据；反过来，换受体重跑对接会产出新 handle，
       引用名因此不同，两条材料不会被互相覆盖。
    2. 工具自己声明的定位参数（`primary_argument` + `evidence_key`）。
       先前的固定六个参数名盖不住 `grep`（pattern）与 `dock`（receptor+ligands），
       于是所有省略了 path 的 grep 共用一条 `grep`，互相替换。
    3. 没有定位参数时只剩工具名——可接受，那意味着这次调用本身没有可区分的对象。
    """
    handle = meta.get("handle")
    if isinstance(handle, str) and handle.strip():
        return f"{part.tool_name} {one_line(handle.strip(), EVIDENCE_REF_LIMIT)}"
    receptor = meta.get("receptor")
    ligands = meta.get("ligands")
    if (
        isinstance(receptor, str)
        and receptor.strip()
        and isinstance(ligands, str)
        and ligands.strip()
    ):
        joined = f"{receptor.strip()}+{ligands.strip()}"
        return f"{part.tool_name} {one_line(joined, EVIDENCE_REF_LIMIT)}"
    if tool is not None and isinstance(part.tool_input, dict):
        names: list[str] = []
        primary = str(getattr(tool, "primary_argument", "") or "")
        if primary:
            names.append(primary)
        for key in getattr(tool, "evidence_key", ()) or ():
            if key not in names:
                names.append(key)
        pieces: list[str] = []
        for key in names:
            value = part.tool_input.get(key)
            if isinstance(value, str) and value.strip():
                pieces.append(f"{key}={ref_value(value)}")
        if pieces:
            return f"{part.tool_name} {'+'.join(pieces)}"
    return evidence_ref(part.tool_name, part.tool_input)


@dataclass
class EvidenceStore:
    """本轮攒下的按需证据：检索回来的记忆，以及含关键行的工具结果。

    **它不是 RECENT 的副本。** 只收两类东西：

    - 检索回来的记忆（`recall`）：跨会话的知识，RECENT 里本来就没有；
    - 含关键行的工具结果：报错、结论、回溯那几行。

    每条工具输出都收一份的话，证据层就成了 RECENT 的复制品，白占额度还挤掉
    真正要留的东西。收进来的是**挑过的关键行**，它的价值在于 RECENT 被压缩或
    被移出之后，这几行仍然在上下文里。

    条数按 `limit` 滚动，新的挤掉旧的；同一条 ref 再出现是**替换**而不是追加，
    同一个文件读三次不会占掉三条。
    """

    limit: int = EVIDENCE_LIMIT
    _items: list[Evidence] = field(default_factory=list)

    def add(
        self,
        ref: str,
        summary: str,
        detail: str = "",
        failed: bool = False,
        source: str = "",
    ) -> None:
        """收一条证据。ref 相同则替换，并把它挪到末尾（最近用到的排在后面）。"""
        name = ref.strip()
        if not name or not summary.strip():
            return
        item = Evidence(
            one_line(name, EVIDENCE_REF_LIMIT),
            one_line(summary, EVIDENCE_SUMMARY_LIMIT),
            detail.strip(),
            failed=failed,
            source=source,
        )
        self._items = [existing for existing in self._items if existing.ref != item.ref]
        self._items.append(item)
        if len(self._items) > self.limit:
            del self._items[: len(self._items) - self.limit]

    def add_from_part(self, part: Part, tool: Any = None) -> None:
        """把一次工具调用的结果提炼成一条证据。

        收的条件是「跑完了，且说得清留下了什么」。**失败的那一类例外**：
        即便一行关键行都没认出来也收——失败原因就是下一步的输入，
        而它常常是短的一句（「缺少参数 text」），关键词表盖不住这种。

        优先用 `Part.tool_meta`：`handle` 决定引用名，计数决定摘要。工具返回的
        结构化读数比从中文输出里认关键行可靠得多，而「这次产出了哪个 handle」
        正是证据层最该留的那一句。没有 meta 才退到关键行。
        """
        if part.type is not PartType.TOOL or not part.tool_name:
            return
        if part.tool_status not in (ToolStatus.COMPLETED, ToolStatus.FAILED):
            return
        meta = part.tool_meta if isinstance(part.tool_meta, dict) else {}
        text = (part.tool_output or "").strip()
        lines = [line.strip() for line in text.splitlines() if is_key_line(line)] if text else []
        if not lines and text and part.tool_status is ToolStatus.FAILED:
            lines = [text.splitlines()[0].strip()]

        summary = meta_summary(meta)
        if summary:
            # 关键行里与摘要相同的那一句不再进正文：同一句话连着出现两次，
            # 读的人会以为工具跑了两遍。
            detail = "\n".join(line for line in lines if line != summary)
        elif lines:
            summary, detail = lines[0], "\n".join(lines[1:])
        else:
            return
        self.add(
            evidence_ref_of(part, meta, tool),
            summary,
            detail,
            failed=part.tool_status is ToolStatus.FAILED,
            source=part.id,
        )

    def clear(self) -> None:
        """一轮开始时清空。证据是「本步相关」的，跨轮留着的多半已经不相干。"""
        self._items = []

    def items(self) -> list[Evidence]:
        """当前攒下的证据，按收进来的先后排列。返回的是副本。"""
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)
