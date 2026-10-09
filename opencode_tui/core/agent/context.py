"""结构化上下文：分层模型与装配用的数据结构。

把上下文从「会话历史的裁剪器」改成「面向当前一步的装配器」：完整历史是档案，
发给模型的那一份是为这一步选出来的工作材料，两者不再追求一致。

预算、装配与降级都在这里。压缩按**内容类型**挑着丢，不按字符位置截：
位置截断会把报错切掉一半，或者把结论所在的那一行留在省略号之外，
而削掉的正是要看的那部分。哪一类先丢由 Part 类型决定，见 `_degrade_recent`。

设计依据见 `docs/context-design.md`，实施计划见 `docs/context-plan.md`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..models import Message, Part, PartType, Session, ToolStatus
from .prompt import (
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
    TASK = "task"          # 任务状态：目标、约束、待办、阻塞
    RECENT = "recent"      # 近期执行：最近若干个完整交互组
    EVIDENCE = "evidence"  # 按需证据：本步相关的片段与检索回来的记忆
    HISTORY = "history"    # 历史：旧对话摘要与可追溯引用


# 装配顺序。越靠前越稳定、越优先保留。
LAYER_ORDER: tuple[Layer, ...] = (
    Layer.PREFIX,
    Layer.MEMORY,
    Layer.TASK,
    Layer.RECENT,
    Layer.EVIDENCE,
    Layer.HISTORY,
)

# 每层的淘汰策略，装配器据此决定「能不能丢、丢到什么程度」。
LAYER_POLICY: dict[Layer, str] = {
    Layer.PREFIX: "pinned",   # 永不丢
    Layer.MEMORY: "capped",   # 整块保留但受 token 上限；超限先砍旧条目
    Layer.TASK: "pinned",     # 永不丢
    Layer.RECENT: "atomic",   # 以完整交互组为单位进出，组内不切开
    Layer.EVIDENCE: "soft",   # 先给 summary，正文按预算展开
    Layer.HISTORY: "ref",     # 只留引用与摘要
}

# 常驻记忆（MEMORY 层）的软配额。上限与淘汰由 MemoryStore 负责，这里只做兜底。
MEMORY_LIMIT = 2000
# 剩余预算里分给各层的比例。PREFIX 与 TASK 不限，MEMORY 单独卡上限。
# 这三个数在 `ContextAssembler.budget` 里汇总成一份预算，各层从同一处取，
# 免得改一个口径要跟着改好几处。
RECENT_SHARE = 0.5
EVIDENCE_SHARE = 0.25
HISTORY_SHARE = 0.25
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
EVIDENCE_LIMIT = 12
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

# 降级链的固定顺序。`degraded` 按这个顺序排，读的人一眼看出削到了第几级。
# 先前用的是集合排序，顺序被抹掉了——而顺序正是「先削谁、后削谁」这件事本身。
DEGRADE_ORDER: tuple[str, ...] = (
    "evidence_detail",     # 证据层收掉正文，只留 ref 与摘要
    "history_summary",     # 摘要缩一档
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

    def tokens(self) -> int:
        """估算这个交互组占多少 token。"""
        return sum(item_tokens(item) for item in self.messages)

    def tool_outputs(self) -> list[dict[str, Any]]:
        """组内所有工具结果项。压缩时只动它们的正文，不切开组。"""
        return [item for item in self.messages if item.get("role") == "tool"]


@dataclass(frozen=True)
class Evidence:
    """一条按需证据。ref 是可追溯的轻量标识，detail 才是正文。

    「按需」体现在两处：只有本步相关的才进上下文；进了之后先给 summary，
    正文 detail 只在预算允许时展开。ref 留在历史层，之后还能取回。
    """

    ref: str          # handle（receptor#1）或 路径#行号 或 URL
    summary: str      # 一行摘要，永远给模型看
    detail: str = ""  # 正文，预算允许时才展开


def one_line(text: str, limit: int) -> str:
    """把一段多行文字压成一行并截断。

    摘要与引用名都要求是一行：它们要和别的行并排放在证据块里，
    夹进来一个换行会把那条记录切成两半，读的人分不清下半截属于谁。
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


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

    def add(self, ref: str, summary: str, detail: str = "") -> None:
        """收一条证据。ref 相同则替换，并把它挪到末尾（最近用到的排在后面）。"""
        name = ref.strip()
        if not name or not summary.strip():
            return
        item = Evidence(
            one_line(name, EVIDENCE_REF_LIMIT),
            one_line(summary, EVIDENCE_SUMMARY_LIMIT),
            detail.strip(),
        )
        self._items = [existing for existing in self._items if existing.ref != item.ref]
        self._items.append(item)
        if len(self._items) > self.limit:
            del self._items[: len(self._items) - self.limit]

    def add_from_part(self, part: Part) -> None:
        """把一次工具调用的结果提炼成一条证据。

        收的条件是「跑完了，且输出里有关键行」。**失败的那一类例外**：
        即便一行关键行都没认出来也收——失败原因就是下一步的输入，
        而它常常是短的一句（「缺少参数 text」），关键词表盖不住这种。

        只收挑出来的那几行，不收整份输出：整份收进来等于把工具输出发两遍。
        第一行放进摘要（摘要永远给），其余的行放进正文——**正文里不重复第一行**，
        否则同一句话会在证据块里连着出现两次。
        """
        if part.type is not PartType.TOOL or not part.tool_name:
            return
        if part.tool_status not in (ToolStatus.COMPLETED, ToolStatus.FAILED):
            return
        text = (part.tool_output or "").strip()
        if not text:
            return
        lines = [line.strip() for line in text.splitlines() if is_key_line(line)]
        if not lines and part.tool_status is ToolStatus.FAILED:
            lines = [text.splitlines()[0].strip()]
        if not lines:
            return
        self.add(
            evidence_ref(part.tool_name, part.tool_input),
            lines[0],
            "\n".join(lines[1:]),
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
    """一步的预算口径：可用额度、预留、工具定义，以及各层的软配额。

    额度只在这一处算。先前各处各算各的，改一次口径要跟着改好几处，
    而且很容易漏掉某一路——工具定义与输出预留就曾经漏过，那 20% 于是只是经验值。
    """

    available: int
    reserve: int
    tools_tokens: int
    shares: dict[Layer, float] = field(default_factory=dict)

    def share(self, layer: Layer, remaining: int) -> int:
        """某一层在剩余额度里分到多少。软配额只决定「先给谁」，超了走降级链。"""
        return int(max(0, remaining) * self.shares.get(layer, 0.0))


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
        for item in translate_message(message):
            if (
                item.get("role") == "tool"
                and turns
                and turns[-1].message_index == message_index
            ):
                turns[-1].messages.append(item)
                continue
            serial += 1
            turns.append(Turn(index=serial, message_index=message_index, messages=[item]))
    return turns


def turns_tokens(turns: list[Turn]) -> int:
    """估算一组交互组占多少 token。"""
    return sum(turn.tokens() for turn in turns)


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


def _cap_evidence(evidence: list[Evidence], limit: int) -> list[Evidence]:
    """证据层按软配额展开正文：摘要永远给，正文装到限额为止。

    摘要一行都不能少——它是「这里有东西可取回」的索引，收了就等于连索引也没了。
    正文是可选展开的部分，装不下就少给几条，先给前面的。
    """
    if not evidence:
        return []
    used = estimate_tokens("\n".join(f"- {item.ref}：{item.summary}" for item in evidence))
    kept: list[Evidence] = []
    for item in evidence:
        room = limit - used
        if not item.detail or room <= 0:
            kept.append(Evidence(item.ref, item.summary))
            continue
        detail = _cap_text(item.detail, room)
        kept.append(Evidence(item.ref, item.summary, detail))
        used += estimate_tokens(detail)
    return kept


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
            shares={
                Layer.RECENT: RECENT_SHARE,
                Layer.EVIDENCE: EVIDENCE_SHARE,
                Layer.HISTORY: HISTORY_SHARE,
            },
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
        evidence: list[Evidence] | None = None,
    ) -> ContextBundle:
        """装配这一轮发给模型的消息。

        `stable_prefix` 由 `prompt.stable_prefix` 给出；`task_text` 是任务状态渲染出的一段。
        摘要与压缩位置从 `session` 上取。

        各层先按软配额取一份，装不下再按降级链逐级削；削到底仍装不下就报
        `ContextAssemblyError`，绝不静默截断。
        """
        raw_evidence = list(evidence or [])
        budget = self.budget(tools)
        available = budget.available
        if available < MIN_AVAILABLE:
            raise ContextAssemblyError(
                f"窗口 {self.context_window} 扣掉输出预留与工具定义之后只剩 {available}，"
                f"连稳定前缀都放不下",
                {Layer.PREFIX: estimate_tokens(stable_prefix)},
            )

        turns = split_turns(session.messages)
        compact = session.compact_upto
        compacted = [turn for turn in turns if turn.message_index < compact]
        live = [turn for turn in turns if turn.message_index >= compact]

        # 各层先按软配额取一份。软配额只决定「先给谁」，装不下由降级链兜。
        # 被配额削过的层一样记进 `degraded`——削了就是削了，不能因为
        # 「不是降级链那一步削的」就不算，否则 `degraded` 反映不出真实用量。
        raw_memory = memory_block(memory_text)
        memory = _cap_memory(raw_memory)
        fixed = estimate_tokens(stable_prefix) + estimate_tokens(memory) + estimate_tokens(task_text)
        remaining = max(0, available - fixed)
        recent = self._take_recent(live, budget.share(Layer.RECENT, remaining))
        summary = _cap_text(session.summary, budget.share(Layer.HISTORY, remaining))
        evidence = _cap_evidence(raw_evidence, budget.share(Layer.EVIDENCE, remaining))

        degraded: list[str] = []
        if memory != raw_memory:
            degraded.append("memory")
        if summary != session.summary:
            degraded.append("history_summary")
        if [item.detail for item in evidence] != [item.detail for item in raw_evidence]:
            degraded.append("evidence_detail")

        live_ids = {turn.index for turn in recent}
        moved = [turn for turn in live if turn.index not in live_ids]

        def moved_out() -> list[Turn]:
            """被移出上下文的交互组：手动压缩挡掉的 + 装不下而挤出去的。"""
            return compacted + moved

        def compose() -> tuple[list[dict[str, Any]], dict[Layer, int]]:
            return self._compose(
                stable_prefix, memory, task_text, recent, moved_out(), summary, evidence
            )

        messages, usage = compose()
        total = sum(usage.values())

        # 降级链。每一步都重算用量，装下就停。
        if total > available and any(item.detail for item in evidence):
            evidence = [Evidence(item.ref, item.summary) for item in evidence]
            degraded.append("evidence_detail")
            messages, usage = compose()
            total = sum(usage.values())
        if total > available and summary:
            summary = _cap_text(summary, max(1, estimate_tokens(summary) // 2))
            degraded.append("history_summary")
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

        out = moved_out()
        return ContextBundle(
            messages=messages,
            usage=usage,
            dropped_turns=[turn.index for turn in out],
            dropped=len(out),
            dropped_upto=max((turn.message_index for turn in out), default=-1) + 1,
            degraded=ordered_degraded(degraded),
        )

    def _take_recent(self, turns: list[Turn], quota: int) -> list[Turn]:
        """从最近往前取交互组，直到超过软配额；至少保留 `MIN_RECENT_TURNS` 组。"""
        taken: list[Turn] = []
        total = 0
        for turn in reversed(turns):
            size = turn.tokens()
            if taken and total + size > quota:
                break
            taken.append(turn)
            total += size
        while len(taken) < min(MIN_RECENT_TURNS, len(turns)):
            taken.append(turns[len(turns) - len(taken) - 1])
        taken.reverse()
        return taken

    def _compose(
        self,
        stable_prefix: str,
        memory: str,
        task_text: str,
        recent: list[Turn],
        moved: list[Turn],
        summary: str,
        evidence: list[Evidence],
    ) -> tuple[list[dict[str, Any]], dict[Layer, int]]:
        """按固定布局把各层拼成消息，并给出每层的估算用量。"""
        header = self._task_header(memory, task_text, moved, summary)
        messages: list[dict[str, Any]] = [{"role": "system", "content": stable_prefix}]
        if header:
            messages.append({"role": "user", "content": header})
        for turn in recent:
            messages.extend(turn.messages)
        evidence_block = self._evidence_block(evidence)
        if evidence_block:
            messages.append({"role": "user", "content": evidence_block})

        usage = {
            Layer.PREFIX: estimate_tokens(stable_prefix),
            Layer.MEMORY: estimate_tokens(memory),
            Layer.TASK: estimate_tokens(task_text),
            Layer.RECENT: turns_tokens(recent),
            Layer.EVIDENCE: estimate_tokens(evidence_block),
            Layer.HISTORY: estimate_tokens(header) - estimate_tokens(memory) - estimate_tokens(task_text),
        }
        return messages, usage

    def _task_header(
        self,
        memory: str,
        task_text: str,
        moved: list[Turn],
        summary: str,
    ) -> str:
        """常驻记忆 + 任务状态 + 摘要 + 引用清单，合成一条 user 头。

        放在 `system` 之后单独一条，是因为 `system` 要保持字节稳定（缓存友好），
        而这一头的内容每一步都可能变。
        """
        blocks: list[str] = []
        if memory:
            blocks.append(memory)
        if task_text.strip():
            blocks.append(task_text.strip())
        if moved:
            if summary:
                blocks.append(f"{SUMMARY_NOTICE}\n{summary}")
            else:
                blocks.append(OMITTED_NOTICE)
            blocks.append(self._refs_block(moved))
        return "\n\n".join(blocks)

    def _refs_block(self, moved: list[Turn]) -> str:
        """被移出的交互组各占一行，之后能按引用取回。"""
        lines = ["（更早的内容已省略，可用 list_workspace / read 按引用取回）"]
        for turn in moved:
            lines.append(f"- turn {turn.index}：{self._turn_label(turn)}")
        return "\n".join(lines)

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

