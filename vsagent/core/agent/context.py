"""结构化上下文：分层模型与装配用的数据结构。

把上下文从「会话历史的裁剪器」改成「面向当前一步的装配器」：完整历史是档案，
发给模型的那一份是为这一步选出来的工作材料，两者不再追求一致。

预算、装配与降级都在这里。压缩按**内容类型**挑着丢，不按字符位置截：
位置截断会把报错切掉一半，或者把结论所在的那一行留在省略号之外，
而削掉的正是要看的那部分。哪一类先丢由 Part 类型决定，见 `_degrade_recent`。

设计见 `docs/design.md` §4，决策过程见 `docs/history.md` §1。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..models import Event, Message, Part, PartType, Session, Step, TaskState, ToolStatus
from ..knowledge.evidence import (
    EVIDENCE_LIMIT,
    EVIDENCE_REF_ARGUMENTS,
    EVIDENCE_REF_LIMIT,
    EVIDENCE_SUMMARY_LIMIT,
    META_COUNT_KEYS,
    REF_VALUE_CHARS,
    Evidence,
    EvidenceStore,
    evidence_ref,
    evidence_ref_of,
    meta_summary,
    one_line,
    ref_value,
)
from .prompt import (
    EMPTY_REASONING,
    EVENT_NOTICE,
    MEMORY_NOTICE,
    OMISSION_MARK,
    OMITTED_NOTICE,
    PROFILE_NOTICE,
    REASONING_PLACEHOLDER,
    SUMMARY_NOTICE,
    compress_tool_output,
    estimate_tokens,
    item_tokens,
    translate_message,
    translate_parts,
)


class Layer(Enum):
    """上下文的一层。装配顺序即枚举顺序。"""

    PREFIX = "prefix"      # 稳定前缀：身份、工作纪律、项目规则
    # 用户习惯单独一层：它只在记忆台账变化时变，而项目情况随问题变。
    # 合成一层的话，问题一换整段就变，装不下时还会把习惯与事实一起砍半。
    PROFILE_MEMORY = "profile_memory"
    PROJECT_MEMORY = "project_memory"
    TASK = "task"          # 任务状态：目标、约束、待办、阻塞；外加工作区索引
    EVENT = "event"        # 事件：已经封存的主题，标题 + 摘要（跨轮）
    RECENT = "recent"      # 近期执行：最近若干个完整交互组
    EVIDENCE = "evidence"  # 按需证据：本步相关的片段与检索回来的记忆
    HISTORY = "history"    # 历史：旧对话摘要与可追溯引用


# 装配顺序。越靠前越稳定、越优先保留。
LAYER_ORDER: tuple[Layer, ...] = (
    Layer.PREFIX,
    Layer.PROFILE_MEMORY,
    Layer.PROJECT_MEMORY,
    Layer.TASK,
    Layer.EVENT,
    Layer.RECENT,
    Layer.EVIDENCE,
    Layer.HISTORY,
)

# 每层的淘汰策略，装配器据此决定「能不能丢、丢到什么程度」。
LAYER_POLICY: dict[Layer, str] = {
    Layer.PREFIX: "pinned",         # 永不丢
    Layer.PROFILE_MEMORY: "pinned", # 用户的长期要求不该在额度紧张时消失
    Layer.PROJECT_MEMORY: "capped", # 整块保留但受 token 上限；超限先砍旧条目
    Layer.TASK: "pinned",           # 永不丢
    Layer.EVENT: "capped",          # 整块保留但受 token 上限；超限先砍最旧的事件
    Layer.RECENT: "atomic",         # 以完整交互组为单位进出，组内不切开
    Layer.EVIDENCE: "soft",         # 先给 summary，正文按预算展开
    Layer.HISTORY: "ref",           # 只留引用与摘要
}

# 用户习惯那一段的软配额。与 `memory.PREFERENCE_LIMIT` 同步：
# 习惯最多 8 条、200 token，超出的由记忆图层自己挡掉，这里只做兜底。
PROFILE_LIMIT = 200
# 项目情况（PROJECT_MEMORY 层）的软配额。上限与淘汰由 MemoryStore 负责，
# 这里只做兜底。
#
# 与 `memory.RESIDENT_LIMIT - PREFERENCE_LIMIT` 同步：那一段每一步都要发，
# 而「与当前问题相关的几条」比「全部旧条目」更有用，其余仍可用 `recall` 取到。
MEMORY_LIMIT = 1000
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
# 事件块的上限。`min(6000, max(200, available // 6))`：窗口大时封在 6000
# （约六个主题铺满流水），窗口很小时按六分之一给，至少 200。
#
# 1800 → 6000 是因为事件从「一条一行」变成了「一个主题一块」：十二步的流水
# 大约两三百 token，旧上限连一个主题都放不下，会立刻触发 `event_layer`
# 把整段历史丢掉。6000 是按「六七个主题」量的，再多就交给
# `event_step_detail` 压成一主题一行。
EVENT_LIMIT_MAX = 6000
EVENT_LIMIT_MIN = 200
# 事件摘要进索引行时的长度上限。完整的摘要留在 `recall_event` 里。
EVENT_SUMMARY_LIMIT = 160
# 一个主题铺多少步。超过 12 步只留「前 3 + 后 3」，中间那些用
# `recall_event evt_0003 step=7` 取——流水的作用是看出「怎么走到这里的」，
# 不是把三十步原样列一遍。
STEPS_INLINE = 12
STEPS_HEAD = 3
STEPS_TAIL = 3
# 给模型输出留的额度，以及协议本身（角色、工具调用外壳）的开销
OUTPUT_RESERVE = 4096
PROTOCOL_OVERHEAD = 256
# 扣掉预留与工具定义之后，至少要有这么多额度，否则前缀都放不下
MIN_AVAILABLE = 512
# 无论如何都要保留的最近交互组数
MIN_RECENT_TURNS = 1
# 本主题走到第几步才在尾部提示「该收口了」。
#
# 2 是 `close_topic` 自己的触发线：两三轮的小事用本地标题就够，提示只会让模型
# 为一次记账多跑一个往返。提示本身是尾部的一行字，不碰前缀缓存。实测一条主题
# 走到 21 步都没收（`evt_0009`），所以提前到第 2 步，并在第 8 步换一句更重的提醒。
CLOSE_TOPIC_AFTER = 2
# 一个主题走到这么多步还没收时，提示改成「先收一次再继续」。
TOPIC_LONG_HINT = 8
# 装配结果的估算值降到窗口的这个比例以内就算合格
CONTEXT_RATIO = 0.8

# 一条证据正文的 token 上限。
#
# 一条工具输出挑出来的关键行通常在十几行以内；给到 600 还装不下，说明那不是
# 「关键行」而是一份完整输出——它该留在 RECENT 或工作目录里，不该挤进证据层。
# 条数上限与摘要长度在 `knowledge/evidence.py`，那边是证据自己的家。
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

# FOCUS 复述的 token 上限。
#
# 它排在最后一条用户消息**前面**，作用是「这一步必须对齐什么」。300 是量出来的：
# 目标一行 + 三条约束 + 下一步一行大约一百来字，300 token 已经能容下最啰嗦的写法；
# 再大就变成第二份 TASK 层，白占额度。
FOCUS_LIMIT = 300
FOCUS_CONSTRAINTS = 3
"""复述里最多带几条约束。约束多的会话按先后顺序取前几条——最早提的通常最硬。"""
# 上一轮的停因写进复述时用的说法。与 `loop.EXIT_LABELS` 同一套文案，
# 放在这里是因为装配器不能 import 循环层（循环层 import 它）。
_EXIT_TEXT = {
    "exhausted": "步数用满",
    "cancelled": "被中断",
    "error": "请求出错",
    "tool_failed": "工具连续失败",
    "topic_full": "这个主题已经足够长，先收了一段",
}

# 降级链的固定顺序。`degraded` 按这个顺序排，读的人一眼看出削到了第几级。
# 先前用的是集合排序，顺序被抹掉了——而顺序正是「先削谁、后削谁」这件事本身。
DEGRADE_ORDER: tuple[str, ...] = (
    "evidence_detail",     # 证据层收掉正文，只留 ref 与摘要
    "history_summary",     # 摘要缩一档
    "recent_tool_clear",   # 旧工具结果正文换成一行占位（可用 read_turn 取回）
    "event_step_detail",   # 主题流水压成一主题一行（标题与结论还在）
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

    `message_index` 记的是它来自哪一条会话消息，`part_start` / `part_end` 是它
    在这一条消息里的区间。三者合起来是**位置**：事件封存到哪儿、RECENT 从哪儿
    开始、`read_turn` 取回哪一段，用的都是这个口径。一条 assistant 消息装着一整轮
    的所有步，只记消息下标表达不了「一轮中间切开」，而位置可以。

    `step_no` 是会话内的步号，与界面上「第 N 步」同一个数。同一段里的模型项
    （助手回复与它的工具结果）共用一个步号，按步取回时整段一起给。
    """

    index: int
    message_index: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    part_start: int = 0
    part_end: int = 0
    step_no: int = 0
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


def event_head(event: Event) -> str:
    """一个主题在上下文里的标题行：编号、主题号、标题、步号区间与归档量。

    不带结论：结论单独一行（`结论：…`），两者分开读才看得出「这一主题
    走到了哪、怎么走到的」。
    """
    title = one_line(event.title or "（未命名）", 40)
    meta = f"{len(event.steps)} 步"
    if event.span:
        meta = f"{event.span} · {meta}"
    if event.tokens:
        meta += f" · 原文 {max(1, event.tokens // 1000)}K token 已归档"
    return f"- {event.id} 主题 {event.topic_index or '?'}：{title}（{meta}）"


def event_line(event: Event) -> str:
    """一条事件的索引行：标题行 + 结论。`/events` 与 `recall_event` 清单用它。"""
    summary = one_line(event.summary, EVENT_SUMMARY_LIMIT)
    return f"{event_head(event)}：{summary}" if summary else event_head(event)


def render_step_line(step: Step) -> str:
    """流水里的一行：步号 + 这一步做了什么 + 结果。

    失败长在它发生的那一步上（「1. 查链 · 失败：目录不存在」），而不是塞进
    一个单独的 `issues` 列表——「第 1 步失败了，所以第 2 步换了个做法」
    这个因果只有并排才看得出来。
    """
    body = step.intent or "（没有记录）"
    if step.result:
        body += " · " + step.result
    return f"  {step.seq}. {body}"


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
    dropped_turns: list[int] = field(default_factory=list)  # 被移出的步号
    dropped: int = 0                                        # 被移出的步数
    # 被移出的内容覆盖到第几条会话消息（不含），供摘要定位用；没移出时为 0
    dropped_upto: int = 0
    degraded: list[str] = field(default_factory=list)       # 这一轮触发的降级步骤
    # 这一步的装配度量，给 `/context` 与 trace 用。
    #
    # 全是可 JSON 序列化的标量/字典：它要进事件总线、进 trace 日志，
    # 放进来一个 Layer 枚举或 dataclass 会让写盘那一步在别处炸掉。
    metrics: dict[str, Any] = field(default_factory=dict)


def turn_position(turn: Turn) -> tuple[int, int]:
    """交互组在会话里的位置，与事件的切点同一套口径。"""
    return (turn.message_index, turn.part_start)


def split_turns(messages: list[Message]) -> list[Turn]:
    """把会话消息切成交互组（Turn）。

    一条 assistant 消息装着一整轮的所有往返，`STEP_START` 把它们分开。
    这里按这些分界把一条消息切成若干段，**逐段**交给 `translate_parts`：
    逐段翻译比整条翻译多知道每段从哪个 Part 开始，`Turn.part_start` 就是它。
    段内再按「assistant 项开头、紧随其后的 tool 项归它」分组，于是工具结果
    永远不会与它的调用分开——这条不变式由构造保证，不靠事后检查。

    `step_no` 按段编号，从 1 开始：一段里的助手项与工具项共用同一个号，
    `read_turn` 按这个号一次取回整段。用户消息也占一个号，它同样会被移出
    RECENT，留号才能按清单取回；这个号是**会话内的段号**，与界面上的
    「第 N 步」在同一轮里通常相等，跨轮之后各自延续（界面每轮从 1 重数）。

    用户消息自成一组；找不到调用方的孤儿工具结果也单独成组（正常不会出现）。
    """
    turns: list[Turn] = []
    serial = 0
    step_no = 0
    for message_index, message in enumerate(messages):
        by_id = {part.id: part for part in message.parts}
        for begin, stop in step_segments(message):
            step_no += 1
            fresh: list[Turn] = []
            current: Turn | None = None
            for item in translate_parts(message.role, message.parts[begin:stop]):
                if item.get("role") == "tool" and current is not None:
                    current.messages.append(item)
                    fresh.append(current)
                    continue
                serial += 1
                current = Turn(
                    index=serial,
                    message_index=message_index,
                    messages=[item],
                    part_start=begin,
                    part_end=stop,
                    step_no=step_no,
                )
                turns.append(current)
                fresh.append(current)
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


def step_segments(message: Message) -> list[tuple[int, int]]:
    """一条消息里的段区间：每个 `STEP_START` 开一段，左闭右开。

    首段之前还可能有 Part（用户消息的正文、旧版本写的助手消息），单独成段。
    """
    starts = [
        position
        for position, part in enumerate(message.parts)
        if part.type is PartType.STEP_START
    ]
    if not starts:
        return [(0, len(message.parts))]
    bounds: list[tuple[int, int]] = []
    if starts[0] > 0:
        bounds.append((0, starts[0]))
    for order, begin in enumerate(starts):
        stop = starts[order + 1] if order + 1 < len(starts) else len(message.parts)
        bounds.append((begin, stop))
    return bounds


def turns_tokens(turns: list[Turn]) -> int:
    """估算一组交互组占多少 token。"""
    return sum(turn.tokens() for turn in turns)


def placeholder_stale_reasoning(turns: list[Turn], messages: list[Message]) -> int:
    """把**更早轮次**的推理换成占位符，返回换掉了几条 assistant 项。

    DeepSeek 的不变式是「只要请求带 tools，每条 assistant 消息都要有
    `reasoning_content`」，缺了直接 400——所以字段必须在。但**内容**不必是原文：
    跨轮之后任务目标已经写在 TASK 与 FOCUS 里，旧推理只是冗余。
    实测一次 37 步的会话里，推理占模型可见正文的 62%（63.5K 字符），
    而它每一步都随历史重发一遍。

    **按轮切而不是按步**：同一轮内模型需要看到自己上一步的推理来保持连贯
    （「刚才决定用 A，现在验证它」）；跨轮之后那条链已经断了，留着只是占地方。

    只改 `turns` 里的模型消息（`translate_message` 每次现造的新字典），
    不动 `Message` / `Part`——`read_turn` 取回的仍是逐字节原文。
    """
    current = max(
        (index for index, message in enumerate(messages) if message.role == "user"),
        default=-1,
    )
    replaced = 0
    for turn in turns:
        # 当前轮 = 最后一条 user 消息之后的 assistant；没有 user 消息时全都算旧轮。
        if current >= 0 and turn.message_index > current:
            continue
        for item in turn.messages:
            if item.get("role") != "assistant":
                continue
            value = item.get("reasoning_content")
            # 空推理（`EMPTY_REASONING`）本来就短，换成占位反而失真：这一步确实没推理。
            if not value or value in (EMPTY_REASONING, REASONING_PLACEHOLDER):
                continue
            item["reasoning_content"] = REASONING_PLACEHOLDER
            replaced += 1
    return replaced


def turn_at(session: Session, index: int) -> Turn | None:
    """按 turn 序号取交互组。

    序号就是归档占位里写的那个：`split_turns` 按模型项的出现顺序给的连号。
    消息只会往后追加，所以已有消息的序号不会变。
    """
    for turn in split_turns(session.messages):
        if turn.index == index:
            return turn
    return None


def step_at(session: Session, step_no: int) -> Turn | None:
    """按步号取交互组。步号由 `split_turns` 在 assistant 消息的每一段上递增。"""
    for turn in split_turns(session.messages):
        if turn.step_no == step_no:
            return turn
    return None


def render_part(part: Part, with_reasoning: bool = True) -> str:
    """把一个 Part 还原成给人读的一段文字。

    `with_reasoning=False` 时跳过思考那一段。取回原文的入口默认关掉它：
    推理是当时的推演，跨轮之后由 TASK 与结论承载，铺出来只是重复占地方。
    """
    if part.type is PartType.TEXT:
        return part.text
    if part.type is PartType.REASONING:
        if not with_reasoning:
            return ""
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


def render_range(
    session: Session, start: int, end: int, with_reasoning: bool = True
) -> str:
    """把会话里 `[start, end)` 这几条消息的原文铺成一段文字。

    `read_turn` 与 `recall_event` 共用它：两处的区别只是区间怎么来
    （一条交互组，还是一段事件），铺开的方式必须一致，否则同一段内容
    从两个入口读出来会是两个样子。
    """
    blocks: list[str] = []
    for message in session.messages[max(0, start) : max(0, end)]:
        for part in message.parts:
            block = render_part(part, with_reasoning)
            if block:
                blocks.append(block)
    return "\n\n".join(blocks)


def render_between(
    session: Session,
    start: tuple[int, int],
    end: tuple[int, int],
    with_reasoning: bool = True,
) -> str:
    """把位置区间 `[start, end)` 的原文铺成一段文字。

    事件按位置切，所以召回一段主题要用它：`start` 是上一条事件的切点，
    `end` 是本条的切点。`render_range` 按整条消息铺，`read_turn` 用它；
    两者分开是因为一条 assistant 消息可能横跨两个主题。
    """
    blocks: list[str] = []
    first = max(0, start[0])
    last = min(end[0], len(session.messages) - 1)
    for index in range(first, last + 1):
        message = session.messages[index]
        low = start[1] if index == start[0] else 0
        high = end[1] if index == end[0] else len(message.parts)
        for part in message.parts[low:high]:
            block = render_part(part, with_reasoning)
            if block:
                blocks.append(block)
    return "\n\n".join(blocks)


def page_lines(
    lines: list[str], offset: int, limit: int, max_tokens: int
) -> tuple[list[str], int, int]:
    """按行窗口取一页，再按 token 预算截断。

    返回这一页的行、下一页的 offset，以及还剩多少行。`read_turn` 与
    `recall_event` 共用它：两处的额度必须同一套算法，否则同一个预算在
    两个入口给出不同的页长，模型按一处的经验翻另一处会反复撞墙。

    至少保留一行：第一行就超过预算时也给它，单行本身没有可切的位置。
    """
    window = lines[offset : offset + limit]
    used = 0
    kept = 0
    for line in window:
        cost = estimate_tokens(line) + 1
        if kept and used + cost > max_tokens:
            break
        used += cost
        kept += 1
    return window[:kept], offset + kept, len(lines) - offset - kept


def render_turn_text(
    session: Session, turn: Turn, with_reasoning: bool = False
) -> str:
    """把一个交互组还原成可读文本。

    读的是**原 Part**，不是模型项：模型项是翻译层的产物，工具输出超过
    `MODEL_OUTPUT_LIMIT` 时早已被截过，还带着 JSON 外壳。`read_turn` 要给的
    正是原文，所以只能回到会话本身取。

    区间是 `(message_index, part_start)` 到 `(message_index, part_end)`，
    也就是**一步**，不是整条消息：一条 assistant 消息装着一整轮的所有步，
    按整条消息取会把十几步一起倒出来。

    推理默认不给：它是当时的推演，跨轮之后由任务状态与结论承载。
    """
    return render_between(
        session,
        (turn.message_index, turn.part_start),
        (turn.message_index, turn.part_end),
        with_reasoning,
    )


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


BUDGET_ENV = "VSA_CONTEXT_BUDGET"


def context_budget_from_env() -> int:
    """`VSA_CONTEXT_BUDGET` 给的上限；没设、写错或不是正数时返回 0（不封顶）。"""
    raw = os.environ.get(BUDGET_ENV, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, value)


def memory_block(memory_text: str) -> str:
    """项目情况那一段。空串表示本步没有记忆。"""
    if not memory_text:
        return ""
    return f"{MEMORY_NOTICE}\n{memory_text}"


def profile_block(profile_text: str) -> str:
    """用户习惯那一段。空串表示还没有习惯。"""
    if not profile_text:
        return ""
    return f"{PROFILE_NOTICE}\n{profile_text}"


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

        `VSA_CONTEXT_BUDGET` 再封一道顶：128K 窗口按比例算出九万多 token，
        装得下就全装，账单跟着涨；设了这个数之后，可用额度取两者中较小的一个。
        没设或写错按不封顶处理——它是省钱的开关，不该因为少打一个数字让应用起不来。
        """
        tools_tokens = tools_tokens_of(tools)
        reserve = min(self.output_reserve + PROTOCOL_OVERHEAD, self.context_window // 4)
        available = int(self.context_window * self.ratio) - reserve - tools_tokens
        cap = context_budget_from_env()
        if cap > 0:
            available = min(available, cap)
        return Budget(
            available=available,
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
        profile_text: str = "",
        hint_text: str = "",
        task_text: str = "",
        digest_text: str = "",
        env_text: str = "",
        evidence: list[Evidence] | None = None,
        steps_left: int | None = None,
    ) -> ContextBundle:
        """装配这一轮发给模型的消息。

        `stable_prefix` 由 `prompt.stable_prefix` 给出；`profile_text` 是用户习惯，
        `memory_text` 是项目事实与经验，两者分开装：习惯只在台账变化时变，
        项目情况随最后一条用户消息变。`task_text` 是任务状态渲染出的一段，
        `digest_text` 是工作区索引（当前受体 + 最近的产物 handle），`env_text` 是
        运行环境（日期、工作目录、状态目录）。摘要与压缩位置从 `session` 上取。
        `steps_left` 是本轮还剩几步，写进尾部提示；不给就不写这一项（探针脚本
        与测试按默认值跑）。

        各层先按软配额取一份，装不下再按降级链逐级削；削到底仍装不下就报
        `ContextAssemblyError`，绝不静默截断。
        """
        raw_evidence = list(evidence or [])
        # 复述只从任务状态来；状态空着就没有这一段，不凭空造一句。
        focus_text = self._focus_block(session)
        budget = self.budget(tools)
        available = budget.available
        if available < MIN_AVAILABLE:
            raise ContextAssemblyError(
                f"窗口 {self.context_window} 扣掉输出预留与工具定义之后只剩 {available}，"
                f"连稳定前缀都放不下",
                {Layer.PREFIX: estimate_tokens(stable_prefix)},
            )

        turns = split_turns(session.messages)
        # 跨轮的推理只留占位。**在估算之前**做：否则预算按原文算、实际发出去的是
        # 占位，`total` 会虚高，降级链可能为了「装不下」白削一层。
        reasoning_placeholder = placeholder_stale_reasoning(turns, session.messages)
        # RECENT 从哪儿开始：事件封存到的位置，与手动 / 强行压缩留下的位置，
        # 取更靠后的那一个。事件按**位置**切（一条 assistant 消息里可能只封了前
        # 三步），所以 `compact_upto` 这个消息下标要当成「那条消息的第 0 个 Part
        # 之前」来比较，否则半条消息的边界会被整条消息的边界吞掉。
        cut = max((session.compact_upto, 0), session.event_cut)
        compacted = [turn for turn in turns if turn_position(turn) < cut]
        live = [turn for turn in turns if turn_position(turn) >= cut]
        archived = list(session.events)

        # 各层先按软配额取一份。软配额只决定「先给谁」，装不下由降级链兜。
        # 被配额削过的层一样记进 `degraded`——削了就是削了，不能因为
        # 「不是降级链那一步削的」就不算，否则 `degraded` 反映不出真实用量。
        raw_memory = memory_block(memory_text)
        memory = _cap_memory(raw_memory)
        raw_profile = profile_block(profile_text)
        profile = _cap_text(raw_profile, PROFILE_LIMIT)
        # 进度提示只在本主题够长时出现（见 `CLOSE_TOPIC_AFTER`）。
        progress = self._progress_block(session, steps_left)
        # 事件块按可用额度的六分之一封顶：它比 RECENT 稳定，但也只是索引，
        # 不该把「最近发生了什么」挤掉。上限 6000 是量出来的——六七个主题的流水。
        event_limit = min(EVENT_LIMIT_MAX, max(EVENT_LIMIT_MIN, available // 6))
        events, event_block = self._cap_events(archived, event_limit)
        fixed = (
            estimate_tokens(stable_prefix)
            + estimate_tokens(profile)
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
        if len(events) < len(archived):
            degraded.append("event_layer")

        live_ids = {turn.index for turn in recent}
        moved = [turn for turn in live if turn.index not in live_ids]

        def moved_out() -> list[Turn]:
            """被移出上下文的交互组：手动压缩挡掉的 + 装不下而挤出去的。"""
            return compacted + moved

        def compose() -> tuple[list[dict[str, Any]], dict[Layer, int]]:
            return self._compose(
                stable_prefix,
                profile,
                memory,
                hint_text,
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
                progress_text=progress,
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
        # 主题流水先压成「一主题一行」：标题与结论还在，丢的是过程。
        # 它比整块丢掉事件温和，所以排在 `event_layer` 之前。
        if total > available and any(event.steps for event in events):
            events, event_block = self._cap_events(archived, event_limit, detail=False)
            degraded.append("event_step_detail")
            messages, usage = compose()
            total = sum(usage.values())
        # 旧事件丢掉：它也「可逆」（recall_event 取回原文），但丢掉的是整段结论，
        # 比一行占位贵，所以排在工具结果清理之后、记忆之前。
        while total > available and events:
            events.pop(0)
            event_block = self._event_block(events, len(archived) - len(events))
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
            dropped_turns=[turn.step_no for turn in out],
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
                "reasoning_placeholder": reasoning_placeholder,
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
            f"{TOOL_ARCHIVE_MARK}step {turn.step_no} 的 {label}；"
            f"原文用 read_turn step={turn.step_no} 取回）"
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

    def _progress_block(self, session: Session, steps_left: int | None = None) -> str:
        """当前主题走到哪儿——够长了才提示收口。

        为什么需要这一句：模型要判断「这一主题是不是已经做完」，手上得有两个数：
        已经走了几步、攒下了哪些产物。实测四轮真跑、三次机会、一次都没调
        `close_topic`（§4.13）：光在提示词里写「几步以上就考虑」，模型手上没有这个数。

        这一行同时就是运行中的主题摘要：步数、产物、未决全由本地从 `pending_steps`
        现算，零请求。主题没有封存，模型也已经能看见它积累了些什么。

        `steps_left` 是本轮还剩多少步（由循环层给）。不说上限时模型只能看见
        「已经走了几步」，看不出还剩几步，于是不会为收尾留额度。

        为什么放尾部：它每步都变，进 header 就等于每步改写第 1 条消息。
        尾部是纯追加，不碰前缀缓存（这一条在 §4.13 量过）。
        """
        steps = len(session.pending_steps)
        if steps < CLOSE_TOPIC_AFTER:
            return ""
        artifacts: list[str] = []
        issues: list[str] = []
        for step in session.pending_steps:
            for item in step.artifacts:
                if item not in artifacts:
                    artifacts.append(item)
            for item in step.issues:
                if item not in issues:
                    issues.append(item)
        parts = [f"本主题已 {steps} 步"]
        if steps_left is not None:
            parts.append(f"本轮还剩 {max(0, steps_left)} 步")
        if artifacts:
            parts.append("产物 " + "、".join(artifacts[:4]))
        if issues:
            parts.append("未决 " + "；".join(issues[:2]))
        head = "（" + " · ".join(parts) + "。"
        if steps >= TOPIC_LONG_HINT:
            return (
                head
                + "这一段已经很长，先调 close_topic 收一次再继续："
                "title 写做成了什么，summary 写产物、关键数字与放弃过的路，"
                "handoff 写后续依赖什么。封存之后这一段原文不再随请求发出，"
                "要看细节用 recall_event。）"
            )
        return (
            head
            + "目标做完或工作方向实质改变时调一次 close_topic："
            "title 写做成了什么，summary 写结论与关键数字，handoff 写后续依赖什么。"
            "封存之后这一段的原文不再随请求发出，只留摘要，要看细节用 recall_event。）"
        )

    def _focus_block(self, session: Session) -> str:
        """最后一条用户消息前面那段「目标复述」。

        为什么要有：任务状态在 TASK 层（窗口最上面），而长会话里 RECENT 能有几万
        token，模型读到「最近几步」时目标早就不在手边了。这一段把同一份状态复述到
        新请求的正前方，让「现在要做成什么」紧挨着「用户刚说了什么」。

        为什么不直接再读一遍 `render()`：那一段带「已完成 / 待办 / 阻塞」全表，
        是给会话看的状态；这里只要几样能让人立刻对齐的东西——目标、约束、下一步，
        外加「上一轮停在哪儿」与「上一段交接了什么」。

        只复述、不新增：这个块里出现的每一句都来自 `TaskState` 或事件记录，
        没有它自己的判断。
        """
        state = session.task_state
        if state.is_empty() and not session.last_exit_reason and not session.events:
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
        if session.last_exit_reason:
            lines.append(f"上一轮停在：{_EXIT_TEXT.get(session.last_exit_reason, session.last_exit_reason)}")
        if session.events and session.events[-1].handoff.strip():
            lines.append(f"上一段交接：{session.events[-1].handoff.strip()}")
        return _cap_text("\n".join(lines), FOCUS_LIMIT)

    def _compose(
        self,
        stable_prefix: str,
        profile: str,
        memory: str,
        hint_text: str,
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
        progress_text: str = "",
    ) -> tuple[list[dict[str, Any]], dict[Layer, int]]:
        """按固定布局把各层拼成消息，并给出每层的估算用量。"""
        header = self._task_header(
            env_text, memory, task_text, digest_text, moved, summary, has_events
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": stable_prefix}]
        # 用户习惯紧跟在系统提示之后：它只在记忆台账变化时才变，
        # 放在这一头能让「系统提示 + 习惯」在一轮里保持同一段字节。
        if profile:
            messages.append({"role": "user", "content": profile})
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
        # 主题切在轮中间之后，RECENT 里可能一条用户消息都没有——那段被主题收走了。
        # 这时复述放在 RECENT 最前面：它仍然紧挨着「接下来要做的事」，而不是掉到
        # 事件块前面去。
        if focus_text and last_user < 0:
            messages.append({"role": "user", "content": focus_text})
        if hint_text and last_user < 0:
            messages.append({"role": "user", "content": hint_text})
        for position, turn in enumerate(recent):
            if focus_text and position == last_user:
                messages.append({"role": "user", "content": focus_text})
            if hint_text and position == last_user:
                messages.append({"role": "user", "content": hint_text})
            messages.extend(turn.messages)
        evidence_block = self._evidence_block(evidence)
        if evidence_block:
            messages.append({"role": "user", "content": evidence_block})
        # 进度提示放**最后一条**：它每步都变（「已 3 步」→「已 4 步」），
        # 放进 header 就等于每步把第 1 条消息改一遍，整条前缀缓存作废
        # （§4.13 量过：那正是长会话命中率掉下来的原因）。放在尾部是纯追加，
        # 前面一个字节都不动；而它是模型做决定的最后一屏，位置反而最对。
        if progress_text:
            messages.append({"role": "user", "content": progress_text})

        usage = {
            Layer.PREFIX: estimate_tokens(stable_prefix),
            Layer.PROFILE_MEMORY: estimate_tokens(profile),
            Layer.PROJECT_MEMORY: estimate_tokens(memory),
            # 工作区索引算进 TASK 层：它与任务状态同为「这一步的工作前提」，
            # 同样 pinned、同样每步重算。单开一层要改 `Layer` 与降级链，
            # 而它的额度只有百来个 token，不值那一圈。进度提示同理。
            Layer.TASK: (
                estimate_tokens(task_text)
                + estimate_tokens(digest_text)
                + estimate_tokens(env_text)
                + estimate_tokens(focus_text)
                + estimate_tokens(hint_text)
                + estimate_tokens(progress_text)
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
        """被移出的步各占一行，之后能按步号取回原文。

        同一段里的助手项与工具项共用一个步号，这里按步号去重，只列第一项
        （助手项带着这一步全部的工具名，标签最全）。用户消息组没有步号，
        不列：用户说了什么已经由 TASK 与事件标题承载。

        这里只说真话：`read_turn` 确实能按下面的步号把原文取回来
        （它读的是会话本身，不受 `compact_upto` 限制）。

        只列最近的 20 步：一份长会话可能移出几百步，全列出来这一段自己就把
        预算吃掉。更早的没有专门入口，所以宁可写清「还有多少步」也不装作列全了。
        """
        lines = ["（更早的内容已省略，可用 read_turn step=<步号> 取回原文）"]
        entries: list[Turn] = []
        seen: set[int] = set()
        for turn in moved:
            if turn.step_no <= 0 or turn.step_no in seen:
                continue
            seen.add(turn.step_no)
            entries.append(turn)
        shown = entries[-REFS_TURN_LIMIT:]
        hidden = len(entries) - len(shown)
        if hidden > 0:
            lines.append(f"（再往前还有 {hidden} 步，未列出）")
        for turn in shown:
            lines.append(f"- step {turn.step_no}：{self._turn_label(turn)}")
        return _cap_text("\n".join(lines), REFS_TOKEN_LIMIT)

    def _turn_label(self, turn: Turn) -> str:
        """给一步起一行标签：调用了哪些工具，或是一句「用户消息 / 助手回复」。

        标签里**不放正文**：引用清单要轻，而且正文一旦写进来就等于没被移出。
        要读原文就按步号回会话里取。
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

    def _event_lines(self, event: Event, detail: bool = True) -> list[str]:
        """一个主题铺成几行：标题、流水、结论、交接、未决。

        `detail=False` 时只剩标题与结论——那是 `event_step_detail` 那一档，
        丢掉的是「怎么走到的」，留下的是「走到了哪」。
        """
        lines = [event_head(event)]
        if detail and event.steps:
            steps = event.steps
            if len(steps) <= STEPS_INLINE:
                lines.extend(render_step_line(step) for step in steps)
            else:
                hidden = len(steps) - STEPS_HEAD - STEPS_TAIL
                lines.extend(render_step_line(step) for step in steps[:STEPS_HEAD])
                lines.append(
                    f"  … 中间省略 {hidden} 步（recall_event {event.id} step=<步号> 取回）"
                )
                lines.extend(render_step_line(step) for step in steps[-STEPS_TAIL:])
        if event.summary:
            lines.append("  结论：" + one_line(event.summary, EVENT_SUMMARY_LIMIT))
        if event.handoff:
            # 交接是「下一个主题依赖什么」，产物与未决由内核算不出来：那几句话
            # 说的是后续怎么用这些结果，只有模型知道。
            lines.append("  交接：" + one_line(event.handoff, EVENT_SUMMARY_LIMIT))
        if detail and event.issues:
            lines.append("  未决：" + one_line("；".join(event.issues[:3]), EVENT_SUMMARY_LIMIT))
        return lines

    def _event_block(
        self, events: list[Event], dropped: int = 0, detail: bool = True
    ) -> str:
        """事件层：每个主题一块，从旧到新。空列表时是空串。

        `dropped` 是被裁掉的条数。**必须说出来**：被裁的事件覆盖的消息既不在
        事件块里，也不在 RECENT 里，不提示的话那一段就是无声消失，
        而上下文看起来完全正常。
        """
        if not events:
            return ""
        lines = [EVENT_NOTICE]
        for event in events:
            lines.extend(self._event_lines(event, detail))
        if dropped:
            lines.append(
                f"（更早还有 {dropped} 个主题没放下；recall_event 不带参数可以列清单）"
            )
        return "\n".join(lines)

    def _cap_events(
        self, events: list[Event], limit: int, detail: bool = True
    ) -> tuple[list[Event], str]:
        """按 token 上限保留事件，从新往旧留。

        丢掉的是**最旧的**：它们离当前最远，而 `recall_event` 仍然取得回来。
        第一条无论多大都留下——一条都不给的话，事件层就成了「说有事件却看不见」。
        """
        kept: list[Event] = []
        used = 0
        for event in reversed(events):
            cost = estimate_tokens("\n".join(self._event_lines(event, detail))) + 1
            if kept and used + cost > limit:
                break
            kept.append(event)
            used += cost
        kept.reverse()
        return kept, self._event_block(kept, len(events) - len(kept), detail)

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

