"""结构化上下文：分层模型与装配用的数据结构。

把上下文从「会话历史的裁剪器」改成「面向当前一步的装配器」：完整历史是档案，
发给模型的那一份是为这一步选出来的工作材料，两者不再追求一致。

预算、装配与降级都在这里。压缩按**内容类型**挑着丢，不按字符位置截：
位置截断会把报错切掉一半，或者把结论所在的那一行留在省略号之外，
而削掉的正是要看的那部分。哪一类先丢由 Part 类型决定，见 `_degrade_recent`。

设计依据见 `docs/context-design.md`，实施计划见 `docs/context-plan.md`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..models import Event, Message, Part, PartType, Session, TaskState, ToolStatus
from .prompt import (
    EVENT_NOTICE,
    MEMORY_NOTICE,
    OMISSION_MARK,
    OMITTED_NOTICE,
    REASONING_PLACEHOLDER,
    SUMMARY_NOTICE,
    compress_tool_output,
    estimate_tokens,
    is_key_line,
    item_tokens,
    translate_message,
)


class Layer(Enum):
    """上下文的一层。装配顺序即枚举顺序。"""

    PREFIX = "prefix"      # 稳定前缀：身份、工作纪律、项目规则
    MEMORY = "memory"      # 常驻记忆：核心事实（跨会话）
    TASK = "task"          # 任务状态：目标、约束、待办、阻塞；外加工作区索引
    EVENT = "event"        # 事件：已经收口的阶段，标题 + 摘要（跨轮）
    RECENT = "recent"      # 近期执行：最近若干个完整交互组
    EVIDENCE = "evidence"  # 按需证据：本步相关的片段与检索回来的记忆
    HISTORY = "history"    # 历史：旧对话摘要与可追溯引用


# 装配顺序。越靠前越稳定、越优先保留。
LAYER_ORDER: tuple[Layer, ...] = (
    Layer.PREFIX,
    Layer.MEMORY,
    Layer.TASK,
    Layer.EVENT,
    Layer.RECENT,
    Layer.EVIDENCE,
    Layer.HISTORY,
)

# 每层的淘汰策略，装配器据此决定「能不能丢、丢到什么程度」。
LAYER_POLICY: dict[Layer, str] = {
    Layer.PREFIX: "pinned",   # 永不丢
    Layer.MEMORY: "capped",   # 整块保留但受 token 上限；超限先砍旧条目
    Layer.TASK: "pinned",     # 永不丢
    Layer.EVENT: "capped",    # 整块保留但受 token 上限；超限先砍最旧的事件
    Layer.RECENT: "atomic",   # 以完整交互组为单位进出，组内不切开
    Layer.EVIDENCE: "soft",   # 先给 summary，正文按预算展开
    Layer.HISTORY: "ref",     # 只留引用与摘要
}

# 常驻记忆（MEMORY 层）的软配额。上限与淘汰由 MemoryStore 负责，这里只做兜底。
#
# 与 `memory.RESIDENT_LIMIT` 同步降到 1200：那一段每一步都要发，
# 而「与当前问题相关的几条」比「全部旧条目」更有用，其余仍可用 `recall` 取到。
MEMORY_LIMIT = 1200
# 摘要与证据的**上限**，不是份额。
#
# 先前按 50/25/25 分剩余额度，问题是空着的那两份不会回流：RECENT 明明还能装，
# 却因为「不归它」被提前截掉，于是窗口还剩一半就开始摘要。现在只给这两层封顶，
# 剩下的全部给 RECENT——它才是「最近发生了什么」的唯一载体。
HISTORY_MAX = 3000
EVIDENCE_MAX = 6000
# 引用清单的上限：最多列这么多组、整块不超过这么多 token。
#
# 长会话里被移出的交互组可能有几百个，全列出来这一段自己就把窗口吃掉，
# 而它的用处只是「告诉模型有哪些序号可取」——列最近的就够。
REFS_TURN_LIMIT = 20
REFS_TOKEN_LIMIT = 600
# 摘要小于这么多 token 时不再「砍半」：省不下额度，却会把一句结论砍成半句。
SUMMARY_SHRINK_MIN = 300
# 事件块的上限。`min(1800, max(200, available // 6))`：窗口大时封在 1800
# （二十来条事件），窗口很小时按六分之一给，至少 200。
EVENT_LIMIT_MAX = 1800
EVENT_LIMIT_MIN = 200
# 事件摘要进索引行时的长度上限。完整的摘要留在 `recall_event` 里。
EVENT_SUMMARY_LIMIT = 160
# 给模型输出留的额度，以及协议本身（角色、工具调用外壳）的开销
OUTPUT_RESERVE = 4096
PROTOCOL_OVERHEAD = 256
# 扣掉预留与工具定义之后，至少要有这么多额度，否则前缀都放不下
MIN_AVAILABLE = 512
# 无论如何都要保留的最近交互组数
MIN_RECENT_TURNS = 1
# 装配结果的估算值降到窗口的这个比例以内就算合格
CONTEXT_RATIO = 0.8

# 证据层的条数上限。
#
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
# 一条证据正文的 token 上限。
#
# 一条工具输出挑出来的关键行通常在十几行以内；给到 600 还装不下，说明那不是
# 「关键行」而是一份完整输出——它该留在 RECENT 或工作目录里，不该挤进证据层。
EVIDENCE_DETAIL_MAX = 600

# 旧工具结果清理：保留最近这么多条不动，超过这个 token 的才换占位。
#
# 4 条是「模型正在用的那几步」的量级：一次调用、它的结果、紧接着的一次修正。
# 再往前的结果，原文用 `read_turn` 一条就能取回，留着占地不如腾出来。
RECENT_TOOL_KEEP = 4
RECENT_TOOL_CLEAR_MIN = 400
# 被归档的工具结果正文换成这个标记开头的一行。装配层靠它认出「这条已经清过」，
# 证据层靠它判断「原文已经不在了，该展开正文」。
TOOL_ARCHIVE_MARK = "（输出已归档："
# 工具结果的引用名取哪个参数做定位。按这个顺序找第一个非空的。
#
# 同一个文件反复读、同一条命令反复跑，引用名相同就替换而不是堆一摞，
# 否则读三次同一个文件就把证据层占掉三条。
EVIDENCE_REF_ARGUMENTS: tuple[str, ...] = (
    "path", "file", "structure", "receptor", "query", "command",
)

# FOCUS 复述的 token 上限。
#
# 它排在最后一条用户消息**前面**，作用是「这一步必须对齐什么」。300 是量出来的：
# 目标一行 + 三条约束 + 下一步一行大约一百来字，300 token 已经能容下最啰嗦的写法；
# 再大就变成第二份 TASK 层，白占额度。
FOCUS_LIMIT = 300
FOCUS_CONSTRAINTS = 3
"""复述里最多带几条约束。约束多的会话按先后顺序取前几条——最早提的通常最硬。"""

# 降级链的固定顺序。`degraded` 按这个顺序排，读的人一眼看出削到了第几级。
# 先前用的是集合排序，顺序被抹掉了——而顺序正是「先削谁、后削谁」这件事本身。
DEGRADE_ORDER: tuple[str, ...] = (
    "evidence_detail",     # 证据层收掉正文，只留 ref 与摘要
    "history_summary",     # 摘要缩一档
    "recent_tool_clear",   # 旧工具结果正文换成一行占位（可用 read_turn 取回）
    "event_layer",         # 丢掉最旧的事件（可用 recall_event 取回）
    "memory",              # 常驻记忆缩一档
    "recent_turns",        # 整组移出最旧的交互组
    "recent_reasoning",    # 丢中间推理（REASONING 那一类）
    "recent_output",       # 压工具输出正文，保报错原文与结论行（TOOL 那一类）
    "recent_output_body",  # 再压一档：工具输出只留关键行
)


def ordered_degraded(levels: list[str]) -> list[str]:
    """按降级链的顺序排列级别名，重复的只留一次。

    表里没有的名字排在最后、按出现先后保留——多一个级别名不该让这里静默吞掉它，
    但也不该因为不在表里就报错。
    """
    seen = dict.fromkeys(levels)
    known = [name for name in DEGRADE_ORDER if name in seen]
    return known + [name for name in seen if name not in DEGRADE_ORDER]


@dataclass
class Turn:
    """一个完整交互组：一次 assistant step 及它触发的工具结果。

    交互组是 `RECENT` 层的原子单位。`tool_call` 与它的结果必须同进同出——
    切开会给模型留下「调了工具却没有结果」或「结果没有对应调用」的孤儿形状，
    部分服务端直接返回 400。

    `message_index` 记的是它来自哪一条会话消息，供 `compact_upto` 按消息序号定位。
    """

    index: int
    message_index: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 组内工具结果对应的源 Part，与 `messages` 里的 tool 项一一对应。
    #
    # 模型项是派生物，有两件事只能回到 Part 上问：这次调用**失败了吗**（失败的结果
    # 不能清理、必须留在证据层），以及**原文还在不在**（证据层只展开 RECENT 看不见的）。
    # 从文本里猜「是不是报错」在中文输出上并不可靠，Part 上的状态才是准的。
    tool_parts: list[Part] = field(default_factory=list)

    def tokens(self) -> int:
        """估算这个交互组占多少 token。"""
        return sum(item_tokens(item) for item in self.messages)

    def tool_outputs(self) -> list[dict[str, Any]]:
        """组内所有工具结果项。压缩时只动它们的正文，不切开组。"""
        return [item for item in self.messages if item.get("role") == "tool"]

    def failed_tool_ids(self) -> set[str]:
        """组内失败的工具调用 id。清理与证据排序都要按它避开失败项。"""
        return {
            part.id for part in self.tool_parts if part.tool_status is ToolStatus.FAILED
        }

    def visible_tool_ids(self) -> set[str]:
        """组内**原文还完整在 RECENT 里**的工具调用 id。

        已归档的那几条要排除：它们的正文已经换成一行占位，原文只能靠
        `read_turn` 取回。证据层如果把它们当成「还看得见」，就会连关键行
        都不展开——那一段内容在上下文里就彻底消失了。
        """
        visible: set[str] = set()
        for item in self.messages:
            if item.get("role") != "tool" or not item.get("tool_call_id"):
                continue
            if (item.get("content") or "").startswith(TOOL_ARCHIVE_MARK):
                continue
            visible.add(item["tool_call_id"])
        return visible


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


def one_line(text: str, limit: int) -> str:
    """把一段多行文字压成一行并截断。

    摘要与引用名都要求是一行：它们要和别的行并排放在证据块里，
    夹进来一个换行会把那条记录切成两半，读的人分不清下半截属于谁。
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


def event_line(event: Event) -> str:
    """一条事件在上下文里占的一行：编号、标题、区间、摘要。"""
    span = f"（第 {event.start + 1}–{event.end} 条）"
    title = one_line(event.title or "（未命名）", 40)
    summary = one_line(event.summary, EVENT_SUMMARY_LIMIT)
    return f"- {event.id} {title}{span}：{summary}"


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


# 结构化读数里值得进摘要的计数键。
#
# 用白名单而不是「把所有数字都拼上」：meta 里还有端口、字节数、运行编号这类
# 与「这次产出了什么」无关的数字，拼进去只会把 120 字的摘要挤满。
META_COUNT_KEYS: tuple[str, ...] = (
    "count", "total", "docked", "pdb", "failed", "remaining", "pages", "chars",
)


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


# 引用里一个参数值留多少字。20 是量出来的——大多数 path / pattern 在这个长度内
# 已经能区分开，再长就把 60 字的整条引用挤满。
REF_VALUE_CHARS = 20


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
        正是证据层最该留的那一句。没有 meta 才回落到关键行。
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


@dataclass(frozen=True)
class Budget:
    """一步的预算口径：可用额度、预留、工具定义，以及各层的封顶。

    额度只在这一处算。先前各处各算各的，改一次口径要跟着改好几处，
    而且很容易漏掉某一路——工具定义与输出预留就曾经漏过，那 20% 于是只是经验值。
    """

    available: int
    reserve: int
    tools_tokens: int
    caps: dict[Layer, int] = field(default_factory=dict)

    def cap(self, layer: Layer) -> int:
        """这一层最多能拿多少 token。

        只有摘要与证据封顶。RECENT 不设上限：它拿的是「其余都归我」，
        再给它一个上限等于把刚才省下来的额度又空在那里。
        """
        return max(0, self.caps.get(layer, 0))


@dataclass
class ContextBundle:
    """装配结果：发给模型的消息，以及每层用了多少、丢了什么、降了几级。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[Layer, int] = field(default_factory=dict)   # 每层估算 token
    dropped_turns: list[int] = field(default_factory=list)  # 被移出的交互组序号
    dropped: int = 0                                        # 被移出的交互组数
    # 被移出的内容覆盖到第几条会话消息（不含），供摘要定位用；没移出时为 0
    dropped_upto: int = 0
    degraded: list[str] = field(default_factory=list)       # 这一轮触发的降级步骤
    # 这一步的装配度量，给 `/context` 与 trace 用。
    #
    # 全是可 JSON 序列化的标量/字典：它要进事件总线、进 trace 日志，
    # 放进来一个 Layer 枚举或 dataclass 会让写盘那一步在别处炸掉。
    metrics: dict[str, Any] = field(default_factory=dict)


def split_turns(messages: list[Message]) -> list[Turn]:
    """把会话消息切成交互组（Turn）。

    翻译层已经按 `STEP_START` 把一条 assistant 消息分成了若干段，
    这里只按「assistant 项开头、紧随其后的 tool 项归它」重新分组。
    于是工具结果永远不会与它的调用分开——这正是交互组要保证的那条不变式，
    而且它是**由构造保证**的，不靠事后检查。

    用户消息自成一组；找不到调用方的孤儿工具结果也单独成组（正常不会出现）。
    """
    turns: list[Turn] = []
    serial = 0
    for message_index, message in enumerate(messages):
        by_id = {part.id: part for part in message.parts}
        fresh: list[Turn] = []
        for item in translate_message(message):
            if (
                item.get("role") == "tool"
                and turns
                and turns[-1].message_index == message_index
            ):
                turns[-1].messages.append(item)
                fresh.append(turns[-1])
                continue
            serial += 1
            turn = Turn(index=serial, message_index=message_index, messages=[item])
            turns.append(turn)
            fresh.append(turn)
        # 工具项自带 `tool_call_id`（就是 Part 的 id），照着取源 Part 即可：
        # 翻译层已经把两边的 id 对齐了，这里不必再复述一遍它的分组规则。
        for turn in fresh:
            for item in turn.messages:
                if item.get("role") != "tool":
                    continue
                part = by_id.get(item.get("tool_call_id"))
                if part is not None and all(
                    existing.id != part.id for existing in turn.tool_parts
                ):
                    turn.tool_parts.append(part)
    return turns


def turns_tokens(turns: list[Turn]) -> int:
    """估算一组交互组占多少 token。"""
    return sum(turn.tokens() for turn in turns)


def turn_at(session: Session, index: int) -> Turn | None:
    """按 turn 序号取交互组。

    序号就是 `_refs_block` 与归档占位里写的那个：`split_turns` 按模型项的
    出现顺序给的连号。消息只会往后追加，所以已有消息的序号不会变。
    """
    for turn in split_turns(session.messages):
        if turn.index == index:
            return turn
    return None


def render_part(part: Part) -> str:
    """把一个 Part 还原成给人读的一段文字。"""
    if part.type is PartType.TEXT:
        return part.text
    if part.type is PartType.REASONING:
        return f"[思考]\n{part.text}" if part.text else ""
    if part.type is PartType.TOOL:
        status = f"（{part.tool_status.value}）" if part.tool_status else ""
        head = f"[工具] {part.tool_name}{status}"
        return f"{head}\n{part.tool_output}" if part.tool_output else head
    if part.type is PartType.FILE:
        return f"[文件] {part.file_path}\n{part.text}".strip()
    if part.type is PartType.PATCH:
        return f"[改动] {part.text}"
    if part.type is PartType.ERROR:
        return f"[错误] {part.text}"
    if part.type is PartType.SUMMARY:
        return f"[总结]\n{part.text}"
    if part.type is PartType.COMPACTION:
        return f"[压缩] {part.text}"
    if part.type is PartType.RETRY:
        return f"[重试] {part.text}"
    return ""


def render_range(session: Session, start: int, end: int) -> str:
    """把会话里 `[start, end)` 这几条消息的原文铺成一段文字。

    `read_turn` 与 `recall_event` 共用它：两处的区别只是区间怎么来
    （一条交互组，还是一段事件），铺开的方式必须一致，否则同一段内容
    从两个入口读出来会是两个样子。
    """
    blocks: list[str] = []
    for message in session.messages[max(0, start) : max(0, end)]:
        for part in message.parts:
            block = render_part(part)
            if block:
                blocks.append(block)
    return "\n\n".join(blocks)


def render_turn_text(session: Session, turn: Turn) -> str:
    """把一个交互组还原成可读文本。

    读的是**原 Part**，不是模型项：模型项是翻译层的产物，工具输出超过
    `MODEL_OUTPUT_LIMIT` 时早已被截过，还带着 JSON 外壳。`read_turn` 要给的
    正是原文，所以只能回到会话本身取。
    """
    return render_range(session, turn.message_index, turn.message_index + 1)


# ---- 选择性压缩 ----
#
# 压缩原语在 `prompt.py`：翻译层给模型的那一份（`truncate_for_model`）也要用同一套，
# 否则一段超长输出会在**装配器看到它之前**先被按字符位置截掉，报错原文正好在
# 被削掉的那一段里，后面再怎么按内容挑也救不回来。放在下面那一层，两处共用。
#
# 翻译层已经把 Part 类型映到了模型消息的字段上，压缩据此决定动哪一类：
#
#   PartType.REASONING  -> item["reasoning_content"]     中间推理，可以先丢
#   PartType.TEXT       -> item["content"]（assistant）  决策与结论，一条都不压
#   PartType.TOOL       -> item["content"]（role=tool）  工具输出，按内容挑着留
#                          item["tool_calls"]            调用记录，不压
#
# 于是「可压的两类」就是推理与工具输出正文；助手正文不在表里，因此永远不动。
# 这不是靠自觉，是这一节只认这两处字段。


class ContextAssemblyError(RuntimeError):
    """装配失败：按降级链削到底仍然装不下。

    带上各层用量，便于直接看出是哪一层把窗口占满了——静默截断比报错更危险，
    因为它会让模型在不自知的情况下丢掉要害。各层的数字也写进消息本身，
    这样界面上那条 ERROR Part 一读就知道该腾哪一层。
    """

    def __init__(self, reason: str, usage: dict[Layer, int]) -> None:
        super().__init__(f"{reason}（各层用量：{describe_usage(usage)}）")
        self.reason = reason
        self.usage = usage


def describe_usage(usage: dict[Layer, int]) -> str:
    """把各层用量排成一行，从占得多的排到占得少的。"""
    items = sorted(usage.items(), key=lambda pair: -pair[1])
    return "、".join(f"{layer.value} {tokens}" for layer, tokens in items if tokens)


def tools_tokens_of(tools: list[dict[str, Any]] | None) -> int:
    """估算工具定义占多少 token。工具定义每一步都要全量重发，必须算进预算。"""
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, ensure_ascii=False))


def memory_block(memory_text: str) -> str:
    """常驻记忆那一段。空串表示本步没有记忆。"""
    if not memory_text:
        return ""
    return f"{MEMORY_NOTICE}\n{memory_text}"


def memory_entry_count(block: str) -> int:
    """常驻记忆里有几条。

    数的是「- 」开头的行——那是 `MemoryStore.render_line` 的格式。
    提示语那一行也算进去的话，没记忆时会显示成 1 条，度量就成了假数字。
    """
    return sum(1 for line in block.splitlines() if line.startswith("- "))


def _cap_memory(block: str) -> str:
    """常驻记忆超过上限时按行累加，最后一行截到剩余额度。

    上限通常由 `MemoryStore.resident_text` 保证；这里是兜底，免得调用方
    直接塞一段超长的记忆进来。截断而不是整条丢弃——留半条事实也比一条不留强。
    """
    if estimate_tokens(block) <= MEMORY_LIMIT:
        return block
    kept: list[str] = []
    used = 0
    for line in block.splitlines():
        size = estimate_tokens(line) + 1
        if used + size <= MEMORY_LIMIT:
            kept.append(line)
            used += size
            continue
        room = MEMORY_LIMIT - used - 2
        if room > 8:
            kept.append(line[:room] + "…")
        break
    result = "\n".join(kept)
    while estimate_tokens(result) > MEMORY_LIMIT and len(result) > 1:
        result = result[: int(len(result) * 0.9)]
    return result


def _cap_text(text: str, limit: int) -> str:
    """把一段文本压到限额以内：留开头，末尾写一行省略说明。

    它**保证**结果不超限额——省略说明本身也占额度，所以是边缩边试，
    而不是缩到差不多就交出去。限额为 0 时整段不留。
    """
    if not text or estimate_tokens(text) <= limit:
        return text
    if limit <= 0:
        return ""
    kept = text[: max(1, int(len(text) * limit / estimate_tokens(text)))]
    while len(kept) > 1:
        note = f"\n{OMISSION_MARK} {len(text) - len(kept)} 字符"
        if estimate_tokens(kept + note) <= limit:
            return kept + note
        kept = kept[: int(len(kept) * 0.9)]
    return kept


def _evidence_key(item: Evidence) -> tuple[str, str]:
    """判重用的键：摘要与正文都归一化后比较。

    同一条报错可能被两个工具各说一遍（一个说「失败：超时」，另一个把原文
    又抄一次）。不判重的话同一句话在证据块里出现两遍，白占额度还让模型
    以为出了两次问题。
    """
    return (" ".join(item.summary.split()), " ".join(item.detail.split()))


def _dedupe_evidence(evidence: list[Evidence]) -> list[Evidence]:
    """同文只留一条，保留第一次出现的顺序。"""
    unique: list[Evidence] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        key = _evidence_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _cap_evidence(
    evidence: list[Evidence],
    limit: int,
    visible: set[str] | None = None,
) -> list[Evidence]:
    """证据层按预算展开正文：摘要永远给，正文按「值不值得看」的顺序分配。

    三条规则，都是有代价换来的：

    - **摘要一行都不能少**：它是「这里有东西可取回」的索引，收了索引就等于
      连线索也没了。所以先扣掉索引行的开销，剩下的才给正文。
    - **正文优先给失败与最新的**：按插入顺序展开的话，最早那几条过程性结果
      会抢走额度，而模型下一步要用的是「刚才为什么失败」。
    - **RECENT 里还看得见的不展开**：`visible` 是这一步仍在 RECENT 里、
      且没被压缩过的工具调用 id。证据层的价值在于「原文已经看不见了，
      这几行还在」，重复一份就等于把工具输出发两遍。
    """
    if not evidence:
        return []
    visible = visible or set()
    unique = _dedupe_evidence(evidence)

    used = estimate_tokens("\n".join(f"- {item.ref}：{item.summary}" for item in unique))
    # 分配顺序：失败优先，其次按收进来的先后倒序（最新的先拿），其余按原顺序。
    order = sorted(range(len(unique)), key=lambda index: (not unique[index].failed, -index))
    details: dict[int, str] = {}
    for index in order:
        item = unique[index]
        if not item.detail:
            continue
        if item.source and item.source in visible:
            continue
        room = min(limit - used, EVIDENCE_DETAIL_MAX)
        if room <= 0:
            continue
        detail = _cap_text(item.detail, room)
        if not detail:
            continue
        details[index] = detail
        used += estimate_tokens(detail)

    return [
        Evidence(
            item.ref,
            item.summary,
            details.get(index, ""),
            failed=item.failed,
            source=item.source,
        )
        for index, item in enumerate(unique)
    ]


class ContextAssembler:
    """按层装配发给模型的消息。

    装配顺序固定：`PREFIX → MEMORY → TASK → RECENT → EVIDENCE → HISTORY`。
    超预算时按固定的降级链逐级削，削到底仍装不下就报 `ContextAssemblyError`，
    绝不静默截断。
    """

    def __init__(
        self,
        context_window: int,
        output_reserve: int = OUTPUT_RESERVE,
        ratio: float = CONTEXT_RATIO,
    ) -> None:
        self.context_window = context_window
        self.output_reserve = output_reserve
        self.ratio = ratio

    def budget(self, tools: list[dict[str, Any]] | None = None) -> Budget:
        """这一步的预算：可用额度与各层软配额。

        额度只在这一处算。窗口很小时，预留不可能整份扣掉（否则前缀都放不下），
        按窗口的四分之一封顶。
        """
        tools_tokens = tools_tokens_of(tools)
        reserve = min(self.output_reserve + PROTOCOL_OVERHEAD, self.context_window // 4)
        return Budget(
            available=int(self.context_window * self.ratio) - reserve - tools_tokens,
            reserve=reserve,
            tools_tokens=tools_tokens,
            caps={Layer.HISTORY: HISTORY_MAX, Layer.EVIDENCE: EVIDENCE_MAX},
        )

    def available(self, tools: list[dict[str, Any]] | None = None) -> int:
        """扣掉输出预留与工具定义之后，可供上下文使用的额度。"""
        return self.budget(tools).available

    def build(
        self,
        session: Session,
        stable_prefix: str,
        tools: list[dict[str, Any]] | None = None,
        memory_text: str = "",
        task_text: str = "",
        digest_text: str = "",
        env_text: str = "",
        evidence: list[Evidence] | None = None,
    ) -> ContextBundle:
        """装配这一轮发给模型的消息。

        `stable_prefix` 由 `prompt.stable_prefix` 给出；`task_text` 是任务状态渲染出的一段，
        `digest_text` 是工作区索引（当前受体 + 最近的产物 handle），`env_text` 是
        运行环境（日期、工作目录、状态目录）。摘要与压缩位置从 `session` 上取。

        各层先按软配额取一份，装不下再按降级链逐级削；削到底仍装不下就报
        `ContextAssemblyError`，绝不静默截断。
        """
        raw_evidence = list(evidence or [])
        # 复述只从任务状态来；状态空着就没有这一段，不凭空造一句。
        focus_text = self._focus_block(session.task_state)
        budget = self.budget(tools)
        available = budget.available
        if available < MIN_AVAILABLE:
            raise ContextAssemblyError(
                f"窗口 {self.context_window} 扣掉输出预留与工具定义之后只剩 {available}，"
                f"连稳定前缀都放不下",
                {Layer.PREFIX: estimate_tokens(stable_prefix)},
            )

        turns = split_turns(session.messages)
        # 事件覆盖到哪儿，RECENT 就从哪儿开始。两者取大：`compact_upto` 是手动
        # 压缩推出来的位置，事件是自动/手动收口推出来的，谁在后面听谁的。
        compact = max(session.compact_upto, session.event_end)
        compacted = [turn for turn in turns if turn.message_index < compact]
        live = [turn for turn in turns if turn.message_index >= compact]

        # 各层先按软配额取一份。软配额只决定「先给谁」，装不下由降级链兜。
        # 被配额削过的层一样记进 `degraded`——削了就是削了，不能因为
        # 「不是降级链那一步削的」就不算，否则 `degraded` 反映不出真实用量。
        raw_memory = memory_block(memory_text)
        memory = _cap_memory(raw_memory)
        # 事件块按可用额度的六分之一封顶：它比 RECENT 稳定，但也只是索引，
        # 不该把「最近发生了什么」挤掉。上限 1800 是量出来的——二十来条事件。
        event_limit = min(EVENT_LIMIT_MAX, max(EVENT_LIMIT_MIN, available // 6))
        events, event_block = self._cap_events(list(session.events), event_limit)
        fixed = (
            estimate_tokens(stable_prefix)
            + estimate_tokens(memory)
            + estimate_tokens(task_text)
            + estimate_tokens(digest_text)
            + estimate_tokens(env_text)
            + estimate_tokens(focus_text)
            + estimate_tokens(event_block)
        )
        remaining = max(0, available - fixed)
        # 摘要与证据先按**实际需要**占额度、各自封顶，剩下的全给 RECENT。
        # 顺序在这里有语义：先给「有多少就得放多少」的两层，再让 RECENT 消化剩余。
        summary = _cap_text(
            session.summary, min(estimate_tokens(session.summary), budget.cap(Layer.HISTORY))
        )
        # 证据先只放摘要。正文是「还有余量才展开」的东西，不参与结构决策：
        # 让它参与的话，展开一次就可能把刚放进 RECENT 的组又挤出去，来回震荡。
        evidence = _cap_evidence(raw_evidence, 0)
        evidence_need = min(
            estimate_tokens(self._evidence_block(evidence)), budget.cap(Layer.EVIDENCE)
        )
        recent_quota = max(0, remaining - estimate_tokens(summary) - evidence_need)
        recent, trimmed = self._take_recent(live, recent_quota)

        degraded: list[str] = []
        if trimmed:
            # 旧工具正文在装 RECENT 时就被收成占位了：那一步同样算削过。
            degraded.append("recent_tool_clear")
        if memory != raw_memory:
            degraded.append("memory")
        if summary != session.summary:
            degraded.append("history_summary")
        if len(events) < len(session.events):
            degraded.append("event_layer")

        live_ids = {turn.index for turn in recent}
        moved = [turn for turn in live if turn.index not in live_ids]

        def moved_out() -> list[Turn]:
            """被移出上下文的交互组：手动压缩挡掉的 + 装不下而挤出去的。"""
            return compacted + moved

        def compose() -> tuple[list[dict[str, Any]], dict[Layer, int]]:
            return self._compose(
                stable_prefix,
                memory,
                task_text,
                digest_text,
                env_text,
                focus_text,
                event_block,
                recent,
                moved_out(),
                summary,
                evidence,
                bool(session.events),
            )

        messages, usage = compose()
        total = sum(usage.values())

        # 降级链。每一步都重算用量，装下就停。
        #
        # 摘要要够大才值得缩：一句话的摘要砍半只省下几个 token，却把那一句
        # 结论变成半句——省不出地方，还丢了内容。所以先看值不值得，再动手。
        if total > available and estimate_tokens(summary) > SUMMARY_SHRINK_MIN:
            summary = _cap_text(summary, max(1, estimate_tokens(summary) // 2))
            degraded.append("history_summary")
            messages, usage = compose()
            total = sum(usage.values())
        # 旧工具结果换成一行占位：**可逆**（read_turn 按序号取回原文），
        # 比缩记忆、移出整组都便宜，所以排在它们前面。
        while total > available:
            if not self._clear_tool_outputs(recent):
                break
            degraded.append("recent_tool_clear")
            messages, usage = compose()
            total = sum(usage.values())
        # 旧事件丢掉：它也「可逆」（recall_event 取回原文），但丢掉的是整段结论，
        # 比一行占位贵，所以排在工具结果清理之后、记忆之前。
        while total > available and events:
            events.pop(0)
            event_block = self._event_block(events)
            degraded.append("event_layer")
            messages, usage = compose()
            total = sum(usage.values())
        if total > available and memory:
            memory = _cap_memory(_cap_text(memory, max(1, estimate_tokens(memory) // 2)))
            degraded.append("memory")
            messages, usage = compose()
            total = sum(usage.values())
        while total > available and len(recent) > MIN_RECENT_TURNS:
            moved.insert(0, recent.pop(0))
            degraded.append("recent_turns")
            messages, usage = compose()
            total = sum(usage.values())
        while total > available:
            level = self._degrade_recent(recent)
            if level is None:
                break
            degraded.append(level)
            messages, usage = compose()
            total = sum(usage.values())

        if total > available:
            raise ContextAssemblyError(
                f"装配失败：降级到底仍需 {total} token，可用额度只有 {available}",
                usage,
            )

        # 结构定下来了，最后才把余量给证据正文：这时才知道哪几组留在 RECENT 里
        # （它们的正文已经在上下文里，不必再展开一份），也才知道还剩多少额度。
        visible = {tool_id for turn in recent for tool_id in turn.visible_tool_ids()}
        unique_evidence = _dedupe_evidence(raw_evidence)
        room = max(0, available - total)
        limit = estimate_tokens(self._evidence_block(evidence)) + min(
            room, budget.cap(Layer.EVIDENCE)
        )
        filled = _cap_evidence(unique_evidence, limit, visible=visible)
        # 「因为额度不够被截」才算降级；与 RECENT 重复而不展开是正常做法，
        # 记成降级会让每一步都平白多一条 trace。
        if [item.detail for item in _cap_evidence(unique_evidence, limit)] != [
            item.detail for item in unique_evidence
        ]:
            degraded.append("evidence_detail")
        if filled != evidence:
            evidence = filled
            messages, usage = compose()
            total = sum(usage.values())

        out = moved_out()
        # `dropped` 只数**这一步新挤出去的**：被手动压缩或事件挡掉的那些早就
        # 不在上下文里了，把它们算进来会让「这一步有没有溢出」永远为真。
        dropped_now = len(moved)
        dropped_upto = max((turn.message_index for turn in out), default=-1) + 1
        levels = ordered_degraded(degraded)
        return ContextBundle(
            messages=messages,
            usage=usage,
            dropped_turns=[turn.index for turn in out],
            dropped=dropped_now,
            dropped_upto=dropped_upto,
            degraded=levels,
            metrics={
                "window": self.context_window,
                "ratio": self.ratio,
                "reserve": budget.reserve,
                "tools": budget.tools_tokens,
                "available": available,
                "total": total,
                "usage": {layer.value: tokens for layer, tokens in usage.items()},
                "dropped": dropped_now,
                "dropped_upto": dropped_upto,
                "degraded": list(levels),
                "recent_turns": len(recent),
                "summary_tokens": estimate_tokens(summary),
                "memory_entries": memory_entry_count(memory),
                "evidence_entries": len(evidence),
                "events": len(events),
            },
        )

    def _take_recent(self, turns: list[Turn], quota: int) -> tuple[list[Turn], bool]:
        """从最近往前取交互组；装不下时先清旧工具正文，腾出地方再装。

        返回取到的组，以及这一次有没有收过正文。

        先前是「装不下就整组不要」：等到降级链里的 `recent_tool_clear` 时，
        那一组早就不在 RECENT 里了，于是那一步实际上永远轮不到——实测的长会话里
        它一次都没触发，一直在整组往外丢。

        现在按「新 → 旧」一组组往下走，装不下就把「最近 keep 条之外」的旧正文
        **一次清一批**，再算一次大小：先轮到这一组自己的（它还没进来，清完变小
        才可能挤进来），再轮到已装下的。代价是模型看不到旧输出的原文
        （`read_turn` 取得回来），换来的是它还知道「那一步调过什么」。

        为什么成批：清掉的集合稳定下来之后，相邻两次请求只在 RECENT 边界处不同，
        前缀命中最高（复算 82.3% 对逐条清的 74.2%）。

        最近 `RECENT_TOOL_KEEP` 条工具结果始终不动：那是模型正在用的几步，
        把眼前的输出抽走比少一组历史更糟。
        """
        taken: list[Turn] = []
        total = 0
        index = len(turns) - 1
        while index >= 0:
            turn = turns[index]
            size = turn.tokens()
            if taken and total + size > quota:
                # 一次清一批「最近 keep 条之外」的旧正文。
                #
                # 离线复算（同一份会话、三种策略）显示成批明显更稳：
                # 前缀命中 82.3%，逐条清 74.2%，清不动就丢组 68.1%。
                # 原因是从旧到新一次清完，之后每一趟装配清掉的还是同一批，
                # 请求内容只在 RECENT 的边界处变；逐条清则「已清的边界」每步都在挪。
                if self._clear_tool_outputs([turn, *reversed(taken)]):
                    total = sum(item.tokens() for item in taken)
                    size = turn.tokens()
            if taken and total + size > quota:
                break
            taken.append(turn)
            total += size
            index -= 1
        while len(taken) < min(MIN_RECENT_TURNS, len(turns)):
            taken.append(turns[len(turns) - len(taken) - 1])
        taken.reverse()
        # 只有**最终留在 RECENT 里**的占位才算真清过：上面可能清了一个
        # 后来又没装下的组，那是白清，不该在 `degraded` 里报成一步。
        trimmed = any(
            str(item.get("content") or "").startswith(TOOL_ARCHIVE_MARK)
            for turn in taken
            for item in turn.tool_outputs()
        )
        return taken, trimmed

    def _archive_tool_item(self, turn: Turn, item: dict[str, Any], name: str) -> bool:
        """把一条工具结果正文换成占位。已经换过、或者不够长时返回 False。"""
        body = item.get("content") or ""
        if body.startswith(TOOL_ARCHIVE_MARK):
            return False
        if estimate_tokens(body) <= RECENT_TOOL_CLEAR_MIN:
            return False
        label = name or "工具"
        item["content"] = (
            f"{TOOL_ARCHIVE_MARK}turn {turn.index} 的 {label}；"
            f"原文用 read_turn turn={turn.index} 取回）"
        )
        return True

    def _clear_tool_outputs(self, recent: list[Turn], keep: int = RECENT_TOOL_KEEP) -> int:
        """把旧的、较大的工具输出换成一行占位，返回清掉的条数。

        **一次清一批**：从旧到新，把「最近 `keep` 条之外」的正文全部换掉。
        逐条清会让「已清的边界」每一步都往后挪一点，相邻两次请求到处都不一样；
        一批清完之后，清掉的集合就固定了，请求只在 RECENT 的边界处变。
        离线复算：成批 82.3% 的前缀命中，逐条 74.2%，不清 68.1%。

        只清**成功**且**够长**的那些：

        - 失败结果的原文就是下一步的输入，清掉等于把线索删了；
        - 短输出清不出多少额度，反而多一行占位。

        最近 `keep` 条一律不动——那是模型正在用的几步。清掉的原文仍在
        `Session.messages` 里，`read_turn` 按 turn 序号能原样取回。

        `recent` 必须是**时间顺序**：它从列表开头开始清、把末尾 `keep` 条留住。
        """
        entries: list[tuple[Turn, dict[str, Any], str]] = []
        for turn in recent:
            names = {
                call["id"]: call["function"]["name"]
                for item in turn.messages
                for call in item.get("tool_calls") or []
            }
            failed = turn.failed_tool_ids()
            for item in turn.tool_outputs():
                call_id = item.get("tool_call_id") or ""
                if call_id in failed:
                    continue
                entries.append((turn, item, names.get(call_id, "")))
        cleared = 0
        for turn, item, name in entries[: max(0, len(entries) - keep)]:
            if self._archive_tool_item(turn, item, name):
                cleared += 1
        return cleared

    def _focus_block(self, state: TaskState) -> str:
        """最后一条用户消息前面那段「目标复述」。

        为什么要有：任务状态在 TASK 层（窗口最上面），而长会话里 RECENT 能有几万
        token，模型读到「最近几步」时目标早就不在手边了。这一段把同一份状态复述到
        新请求的正前方，让「现在要做成什么」紧挨着「用户刚说了什么」。

        为什么不直接再读一遍 `render()`：那一段带「已完成 / 待办 / 阻塞」全表，
        是给会话看的状态；这里只要三样能让人立刻对齐的东西——目标、约束、下一步。

        只复述、不新增：这个块里出现的每一句都来自 `TaskState`，没有它自己的判断。
        """
        if state.is_empty():
            return ""
        lines = ["（当前任务复述：下面这些是前面已经定下的，不是新指令）"]
        if state.goal:
            lines.append(f"目标：{state.goal}")
        if state.constraints:
            shown = state.constraints[:FOCUS_CONSTRAINTS]
            text = "；".join(shown)
            if len(state.constraints) > len(shown):
                text += f"；另有 {len(state.constraints) - len(shown)} 条"
            lines.append(f"用户约束：{text}")
        if state.todo:
            lines.append(f"下一步：{state.todo[0]}")
        elif state.blockers:
            # 没有待办时，「卡在哪」就是下一步要处理的，别把这一段空着。
            lines.append(f"当前阻塞：{state.blockers[0]}")
        return _cap_text("\n".join(lines), FOCUS_LIMIT)

    def _compose(
        self,
        stable_prefix: str,
        memory: str,
        task_text: str,
        digest_text: str,
        env_text: str,
        focus_text: str,
        event_block: str,
        recent: list[Turn],
        moved: list[Turn],
        summary: str,
        evidence: list[Evidence],
        has_events: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[Layer, int]]:
        """按固定布局把各层拼成消息，并给出每层的估算用量。"""
        header = self._task_header(
            env_text, memory, task_text, digest_text, moved, summary, has_events
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": stable_prefix}]
        if header:
            messages.append({"role": "user", "content": header})
        # 事件单独一条消息，不并进 header：header 每一步都可能变，事件块一旦
        # 定下来就只在新事件出现时变，分开才有机会命中前缀缓存。
        if event_block:
            messages.append({"role": "user", "content": event_block})
        # 复述插在**最后一条用户消息之前**：新请求一到，先看见「这件事原本要做成
        # 什么」，再看用户这次说的话。同一轮里位置固定（用户消息就是这一轮的开头），
        # 所以不会因为步数增加而挪动、把后面的缓存全打断。
        last_user = -1
        for position, turn in enumerate(recent):
            if turn.messages and turn.messages[0].get("role") == "user":
                last_user = position
        for position, turn in enumerate(recent):
            if focus_text and position == last_user:
                messages.append({"role": "user", "content": focus_text})
            messages.extend(turn.messages)
        evidence_block = self._evidence_block(evidence)
        if evidence_block:
            messages.append({"role": "user", "content": evidence_block})

        usage = {
            Layer.PREFIX: estimate_tokens(stable_prefix),
            Layer.MEMORY: estimate_tokens(memory),
            # 工作区索引算进 TASK 层：它与任务状态同为「这一步的工作前提」，
            # 同样 pinned、同样每步重算。单开一层要改 `Layer` 与降级链，
            # 而它的额度只有百来个 token，不值那一圈。
            Layer.TASK: (
                estimate_tokens(task_text)
                + estimate_tokens(digest_text)
                + estimate_tokens(env_text)
                + estimate_tokens(focus_text)
            ),
            Layer.EVENT: estimate_tokens(event_block),
            Layer.RECENT: turns_tokens(recent),
            Layer.EVIDENCE: estimate_tokens(evidence_block),
            Layer.HISTORY: (
                estimate_tokens(header)
                - estimate_tokens(memory)
                - estimate_tokens(task_text)
                - estimate_tokens(digest_text)
                - estimate_tokens(env_text)
                - estimate_tokens(focus_text)
            ),
        }
        return messages, usage

    def _task_header(
        self,
        env_text: str,
        memory: str,
        task_text: str,
        digest_text: str,
        moved: list[Turn],
        summary: str,
        has_events: bool = False,
    ) -> str:
        """运行环境 + 常驻记忆 + 任务状态 + 工作区索引 + 摘要 + 引用清单，合成一条 user 头。

        放在 `system` 之后单独一条，是因为 `system` 要保持字节稳定（缓存友好），
        而这一头的内容每一步都可能变。
        """
        blocks: list[str] = []
        # 环境排最前：它是这一头里唯一的「机器事实」，读的时候先知道自己在哪台机器、
        # 哪个目录下，后面那些「任务 / 索引 / 摘要」才落得到实处。
        if env_text.strip():
            blocks.append(env_text.strip())
        if memory:
            blocks.append(memory)
        if task_text.strip():
            blocks.append(task_text.strip())
        # 索引排在任务状态之后、摘要之前：它和任务状态一样是「这一步的工作前提」，
        # 而摘要与引用清单是「前面发生过什么」，两类的读法不同，不该夹在一起。
        if digest_text.strip():
            blocks.append(digest_text.strip())
        # 有事件时也要给摘要：事件只覆盖到某个位置，摘要覆盖它之前的那一段。
        # 只判断 `moved` 的话，事件把 RECENT 之前的内容全接走时摘要会凭空消失。
        if moved or (summary and has_events):
            if summary:
                blocks.append(f"{SUMMARY_NOTICE}\n{summary}")
            else:
                blocks.append(OMITTED_NOTICE)
            if moved:
                blocks.append(self._refs_block(moved))
        return "\n\n".join(blocks)

    def _refs_block(self, moved: list[Turn]) -> str:
        """被移出的交互组各占一行，之后能按序号取回原文。

        这里只说真话：`read_turn` 确实能按下面的序号把原文取回来
        （它读的是会话本身，不受 `compact_upto` 限制）。

        只列最近的 20 组：一份长会话可能移出几百组，全列出来这一段自己就把
        预算吃掉。更早的没有专门入口，所以宁可写清「还有多少组」也不装作列全了。
        """
        lines = ["（更早的内容已省略，可用 read_turn turn=<序号> 取回原文）"]
        shown = moved[-REFS_TURN_LIMIT:]
        hidden = len(moved) - len(shown)
        if hidden > 0:
            lines.append(f"（再往前还有 {hidden} 组，未列出）")
        for turn in shown:
            lines.append(f"- turn {turn.index}：{self._turn_label(turn)}")
        return _cap_text("\n".join(lines), REFS_TOKEN_LIMIT)

    def _turn_label(self, turn: Turn) -> str:
        """给一个交互组起一行标签：调用了哪些工具，或是一句「用户消息 / 助手回复」。

        标签里**不放正文**：引用清单要轻，而且正文一旦写进来就等于没被移出。
        要读原文就按序号回会话里取。
        """
        names: list[str] = []
        for item in turn.messages:
            for call in item.get("tool_calls") or []:
                name = call["function"]["name"]
                if name not in names:
                    names.append(name)
        if names:
            return "、".join(names)
        return "用户消息" if turn.messages[0].get("role") == "user" else "助手回复"

    def _event_block(self, events: list[Event]) -> str:
        """事件层：每个事件一行，从旧到新。空列表时是空串。"""
        if not events:
            return ""
        return "\n".join([EVENT_NOTICE, *(event_line(event) for event in events)])

    def _cap_events(self, events: list[Event], limit: int) -> tuple[list[Event], str]:
        """按 token 上限保留事件，从新往旧留。

        丢掉的是**最旧的**：它们离当前最远，而 `recall_event` 仍然取得回来。
        第一条无论多大都留下——一条都不给的话，事件层就成了「说有事件却看不见」。
        """
        kept: list[Event] = []
        used = 0
        for event in reversed(events):
            cost = estimate_tokens(event_line(event)) + 1
            if kept and used + cost > limit:
                break
            kept.append(event)
            used += cost
        kept.reverse()
        return kept, self._event_block(kept)

    def _evidence_block(self, evidence: list[Evidence]) -> str:
        """证据层：先给 summary，再按剩余额度展开 detail。"""
        if not evidence:
            return ""
        blocks = ["（本步相关的材料，按需取回）"]
        for item in evidence:
            blocks.append(f"- {item.ref}：{item.summary}")
            if item.detail:
                blocks.append(item.detail)
        return "\n".join(blocks)

    def _degrade_recent(self, recent: list[Turn]) -> str | None:
        """按 Part 类型从最该丢的那类开始压，一次压一处。压到了返回级别名。

        顺序是刻意的：**中间推理（REASONING）先丢，工具输出正文（TOOL）后压**。
        推理是过程，答案在助手正文里；工具输出里则可能夹着报错原文，压之前要先
        把关键行挑出来。助手正文是决策与结论、`tool_calls` 是调用记录，
        两者都不在可压的两类里，所以一条都不会被动到——这不是靠自觉，
        是这一节只认那两处字段。

        一处都压不动时返回 None，降级链据此走到「报装配失败」。
        """
        for turn in recent:
            for item in turn.messages:
                if item.get("role") == "tool":
                    continue
                reasoning = item.get("reasoning_content")
                if reasoning and reasoning != REASONING_PLACEHOLDER:
                    item["reasoning_content"] = REASONING_PLACEHOLDER
                    return "recent_reasoning"
        for deep, level in ((False, "recent_output"), (True, "recent_output_body")):
            for turn in recent:
                for item in turn.tool_outputs():
                    shrunk = compress_tool_output(item.get("content") or "", deep=deep)
                    if shrunk is not None:
                        item["content"] = shrunk
                        return level
        return None

