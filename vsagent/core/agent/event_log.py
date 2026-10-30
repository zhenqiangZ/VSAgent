"""事件层：把一轮工作记成「一个 Phase 的 Step 流水」，原文让位。

四层，不是三层：

    Task   —— 一条用户消息
    Phase  —— 一大步（默认 = 一轮），封存后就是一条 Event
    Step   —— 一次模型往返加它的工具调用，内核每步自动记
    Event  —— 一个 Phase 的流水，Phase 结束自动封存

**关键差别**：旧设计里 Event 是「一段工作的摘要」，模型判断「话题变了」才写，
于是它把「记录」挂在了「压缩」上——1M 窗口不需要压缩，记录也就一次都没发生。
新设计里记录每步都做（零请求），压缩只是记录的副产品。模型不再决定「要不要记」，
只决定「这个 Phase 叫什么、结论是什么」，而且那一步也是可选的。

三条规则，写在这里也写在文档里（`docs/session-review-plan.md` §4）：

1. **连续覆盖**。首条事件从 `summary_upto` 开始，后一条接前一条的 `end`，
   RECENT 从最后一条的 `end` 开始。中间不许有缝——有缝就意味着那一段原文
   既不在事件里也不在 RECENT 里，内容无声消失，而上下文看起来完全正常。
2. **边界排除正在写的东西**。封存停在当前这条 assistant 消息之前：
   它还没写完，收进去等于把半句话当成结论。
3. **记录不花请求**。Step 全部由本地从 Part 拼装（工具名、参数、状态、handle），
   不读 reasoning、不请模型写 intent。Phase 的标题与结论模型可以顺手给
   （`close_phase`），不给就用本地拼的那一份，标 `degraded=True`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import (EVENT_KINDS, STEP_INTENT_MAX, STEP_RESULT_MAX, Event, Part,
                      PartType, Session, Step, ToolStatus)
from .prompt import estimate_tokens
from .tools import Tool, ToolResult, ToolSpec

# 标题与结论的长度上限。**代码里截**，不只写在提示词里：
# 模型不会每次都照做，而这两段进的是每步都要发的索引。
EVENT_TITLE_MAX = 40
EVENT_SUMMARY_MAX = 600
# 本地标题里最多拼几段用户诉求。两段够看出「从哪做到哪」，再多就把标题
# 变成一份清单；超出的用「等 N 段」交代，不假装没发生。
TITLE_ASKS = 2
# `recall_event` 一次最多铺多少行原文。
EVENT_RECALL_LIMIT = 120
# 单个 Phase 的原文估算上限。一轮跑了三十步的情况靠它切开：
# 不切的话一个 Phase 会一直长到把 RECENT 撑爆，而流水是「一大步」的粒度，
# 太长就退化成第二份逐条列表。120K 是 §4.8 的成本表里定下的。
PHASE_SOFT_LIMIT = 120_000


@dataclass
class EventRuntime:
    """记录事件时要用到的运行时。

    与登记表、任务状态一样由内核注入，工具自己不持有会话：
    事件是**当前会话**的一部分，会话一换，工具手上的那份就该跟着换。

    **没有 `summarize`**：标题与结论由模型随调用一起给。封存的位置是模型自己
    选的，那一段的内容它刚刚看过，另起一次请求让它「再读一遍原文写摘要」
    只是把同一份上下文发两遍，还多等一个往返。
    """

    session: Session
    # 当前正在写的那条 assistant 消息的下标（不含）。封存收到这里为止。
    current_index: int = 0


def event_boundary(session: Session, index: int) -> int:
    """事件能封存到哪条消息（不含）。

    排掉当前位置之后，再往前跳过连续的 user 消息：那些是「用户刚说的话」，
    是下一步的输入，收进事件就等于把这一步的题目收走了。
    """
    end = max(0, min(index, len(session.messages)))
    while end > 0 and session.messages[end - 1].role == "user":
        end -= 1
    return end


def _user_texts(session: Session, start: int, end: int) -> list[str]:
    """这一段里每一句用户原话，按顺序。

    标题要描述**整段**，不是只描述开头。事件一合并就会跨过好几句诉求，
    只取第一句的话，后几句做的事在索引里一个字都不留（实测：合并到 12 条
    消息、10 个工具时，标题还写着第 1 句）。

    逐字重复的句子只算一句：用户连着说三次「继续」时，标题里出现三个「继续」
    既占地方又不提供信息。
    """
    texts: list[str] = []
    for message in session.messages[start:end]:
        if message.role != "user":
            continue
        for part in message.parts:
            if part.type is PartType.TEXT and part.text.strip():
                flat = " ".join(part.text.split())
                if flat and flat not in texts:
                    texts.append(flat)
    return texts


def _fit_title(text: str, suffix: str = "") -> str:
    """按标题上限裁 `text`，`suffix`（「等 N 段」）优先保住。

    先拼再截会把后缀截掉，读者就看不出这条事件到底覆盖了几段。
    """
    room = EVENT_TITLE_MAX - len(suffix)
    if len(text) > room:
        text = text[: max(1, room - 1)] + "…"
    return text + suffix


def _short(text: Any, limit: int) -> str:
    """压成一行并截到 `limit`。流水里每一段都要求是一行。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


def _tool_list(actions: list[str]) -> str:
    """工具名列表：前三个 + 「等 N 个」。

    列全的话，十个工具就占掉半条索引行，把产物挤到截断线外面去。
    """
    head = "、".join(actions[:3])
    return head + (f" 等 {len(actions)} 个" if len(actions) > 3 else "")


def _arg_text(value: Any) -> str:
    """把一个参数压成一句可读的短文本。

    只取前几项：`tool_input` 里偶尔会有几十个键的字典（`write_run` 的 meta），
    全铺出来这一行就没法看了。取前三个已经够认出「这一步在做什么」。
    """
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool) or isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, dict):
        text = "、".join(f"{key}={_arg_text(item)}" for key, item in list(value.items())[:3])
    elif isinstance(value, (list, tuple)):
        text = "、".join(_arg_text(item) for item in list(value)[:3])
    else:
        text = str(value)
    return _short(text, STEP_RESULT_MAX)


def step_from_parts(
    seq: int,
    parts: list[Part],
    message_index: int,
    part_start: int = 0,
    part_end: int = 0,
) -> Step | None:
    """把一次模型往返里的工具 Part 折成一条 Step。不调模型、不读 reasoning。

    这些都是**已经算好的**：`tool_name` / `tool_input` / `tool_status` /
    `tool_meta` 都在 Part 上，纯本地拼装。失败项留在流水里而不是单独一个
    `issues` 列表——「第 1 步失败了，所以第 2 步换了个做法」这个因果只有并排
    才看得出来（§4.5 第 2 条）。
    """
    tools: list[str] = []
    args: list[str] = []
    artifacts: list[str] = []
    issues: list[str] = []
    results: list[str] = []

    def remember(item: str) -> None:
        if item and item not in artifacts:
            artifacts.append(item)

    for part in parts:
        if part.type is PartType.FILE and part.file_path:
            remember(part.file_path)
            continue
        if part.type is PartType.PATCH and part.text:
            remember(part.text)
            continue
        if part.type is not PartType.TOOL or not part.tool_name:
            continue
        # `close_phase` 是记账，不是干活：把它写进流水只会让「第 3 步」那一行
        # 变成「close_phase」，而它封的正是它前面那几步。单独调它时这一步
        # 干脆不产生 Step（下面的 `if not tools` 会返回 None）。
        if part.tool_name == "close_phase":
            continue
        tools.append(part.tool_name)
        given = part.tool_input if isinstance(part.tool_input, dict) else {}
        rendered = "、".join(f"{key}={_arg_text(item)}" for key, item in list(given.items())[:3])
        args.append(f"{part.tool_name}({rendered})" if rendered else part.tool_name)
        meta = part.tool_meta if isinstance(part.tool_meta, dict) else {}
        for key in ("handle", "ref", "run_id"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                remember(value.strip())
        head = _short(part.tool_output, STEP_RESULT_MAX)
        if part.tool_status is ToolStatus.FAILED:
            issues.append(head or "没有写明原因")
            results.append(f"失败：{head or '没有写明原因'}")
        else:
            results.append(f"成功：{head or '（没有输出）'}")

    if not tools:
        return None
    intent = _short(" + ".join(args), STEP_INTENT_MAX)
    result = _short("；".join(dict.fromkeys(results)), STEP_RESULT_MAX)
    if artifacts:
        result = _short(f"{result} → {'、'.join(artifacts[:3])}", STEP_RESULT_MAX + 60)
    return Step(
        seq=seq,
        intent=intent,
        tools=tools,
        args=args,
        result=result,
        artifacts=artifacts,
        issues=issues,
        message_index=message_index,
        part_start=part_start,
        part_end=part_end,
    )


def _steps_from_messages(session: Session, start: int, end: int) -> list[Step]:
    """从会话原文里回填流水：一条 assistant 消息里的**每一段**折成一步。

    什么时候用：这一段的 Step 没记下来（进程重启、或者这段是旧版本写的历史）。
    读的就是 Part，所以回填出来的流水与当场记的那份形状一致，只是没有
    `part_start` 之外的额外信息。

    **必须按 `STEP_START` 分段**：一条 assistant 消息装着一整轮的所有往返
    （一步一次模型往返），整条折成一步会把「第 1 步装了族、第 2 步才用它」
    压成一行。`part_start` 取这一段里第一个工具 Part 的位置，与
    `record_step` 的口径一致——`seal_phase` 按 `(消息, 起始位置)` 合并两份，
    口径不一致就会把同一步记两遍。
    """
    steps: list[Step] = []
    for index in range(max(0, start), min(end, len(session.messages))):
        message = session.messages[index]
        if message.role != "assistant":
            continue
        starts = [
            position
            for position, part in enumerate(message.parts)
            if part.type is PartType.STEP_START
        ] or [0]
        for order, segment in enumerate(starts):
            stop = starts[order + 1] if order + 1 < len(starts) else len(message.parts)
            calls = [
                part
                for part in message.parts[segment:stop]
                if part.type is PartType.TOOL and part.tool_name
            ]
            if not calls:
                continue
            positions = [
                position
                for position, part in enumerate(message.parts)
                if any(part is call for call in calls)
            ]
            step = step_from_parts(
                len(steps) + 1,
                calls,
                index,
                min(positions) if positions else segment,
                (max(positions) + 1) if positions else stop,
            )
            if step is not None:
                steps.append(step)
    return steps


def record_step(
    session: Session,
    *,
    step_no: int,
    parts: list[Part],
    message_index: int,
    part_start: int = 0,
    part_end: int = 0,
) -> Step | None:
    """把这一步的 Part 折成一条 Step，挂到当前 Phase 上。零请求。

    只在内存里累积：每步都重写整份 `state.toml` 不值（一次会话几十步），
    而丢几步可以接受——原文还在 RECENT 里，最坏是这一阶段少几行流水。
    """
    step = step_from_parts(step_no, parts, message_index, part_start, part_end)
    if step is None:
        return None
    session.pending_steps.append(step)
    session.touch()
    return step


def phase_start(session: Session) -> int:
    """当前 Phase 从第几条消息开始：接着上一条事件的末端。"""
    return session.events[-1].end if session.events else session.summary_upto


def _range_tokens(session: Session, start: int, end: int) -> int:
    """一段原文的估算 token。软预算与「已归档多少」都读它。"""
    total = 0
    for message in session.messages[max(0, start) : max(0, end)]:
        for part in message.parts:
            total += estimate_tokens(part.text or "")
            if part.tool_output:
                total += estimate_tokens(part.tool_output)
    return total


def phase_tokens(session: Session) -> int:
    """当前这个还没封存的 Phase 已经有多长（估算）。"""
    return _range_tokens(session, phase_start(session), len(session.messages))


def _next_phase_index(session: Session) -> int:
    """下一个阶段号。取已有最大值 +1：事件被裁掉之后号也不会重复。"""
    return max((event.phase_index for event in session.events), default=0) + 1


def _local_title(session: Session, start: int, end: int, steps: list[Step]) -> str:
    """本地拼标题：有用户原话就用原话，否则列工具，再否则报区间。

    这三条是旧 `build_local_event` 留下的规则，实测够用：模型没写标题时，
    「用户要什么」比「用了什么工具」更能说明这一段在干什么。
    """
    asks = _user_texts(session, start, end)
    if asks:
        return _fit_title(
            "／".join(asks[:TITLE_ASKS]),
            f" 等 {len(asks)} 段" if len(asks) > TITLE_ASKS else "",
        )
    actions: list[str] = []
    for step in steps:
        for name in step.tools:
            if name not in actions:
                actions.append(name)
    if actions:
        suffix = "" if len(actions) <= 3 else f" 等 {len(actions)} 个"
        return _fit_title("工具：" + "、".join(actions[:3]), suffix)
    return f"第 {start + 1}–{end} 条"


def _local_summary(steps: list[Step], start: int, end: int) -> str:
    """本地拼结论：「N 步，M 个工具，产物 K 个，未决 J 项」。

    顺序是「最不能丢的在前」：产物与未决是下一步真的要用到的东西，
    工具名最好补（流水里本来就有）。
    """
    if not steps:
        return f"覆盖第 {start + 1}–{end} 条消息"
    actions: list[str] = []
    artifacts: list[str] = []
    issues: list[str] = []
    for step in steps:
        for name in step.tools:
            if name not in actions:
                actions.append(name)
        for item in step.artifacts:
            if item not in artifacts:
                artifacts.append(item)
        for item in step.issues:
            if item not in issues:
                issues.append(item)
    pieces = [f"{len(steps)} 步"]
    if artifacts:
        pieces.append("产物 " + "、".join(artifacts[:5]))
    if issues:
        pieces.append("未决 " + "；".join(issues[:3]))
    if actions:
        pieces.append("调用 " + _tool_list(actions))
    return "；".join(pieces)[:EVENT_SUMMARY_MAX]


def seal_phase(
    session: Session,
    *,
    kind: str = "auto",
    upto: int | None = None,
    title: str = "",
    summary: str = "",
    force: bool = False,
) -> Event | None:
    """把 `[phase_start, upto)` 封成一个 Phase，零模型请求。

    封存做什么（§4.4）：拼标题与结论（模型没给就本地拼）、算原文 token、
    追加到 `session.events`、把已经用掉的 Step 从待封存列表里摘掉。
    覆盖不变式由 `sanitize_events` 兜底：断点之后的事件一律丢掉。

    `title` 为空表示「模型没写」——那一条会被标成 `degraded=True`，
    读的人一眼看出这份标题是本地拼的。

    `force` 决定「一段里一步都没有」时封不封：
    - 轮末自动封存（`force=False`）：纯聊天的一轮没有流水可记，跳过，
      否则事件层会被一堆空阶段占满；
    - 溢出 / `/compact` / 强行腾地方（`force=True`）：**必须封**。不封的话
      那一段原文既不在事件里也不在 RECENT 里，内容无声消失，
      而上下文看起来完全正常——这正是连续覆盖那条不变式要防的。
    """
    start = phase_start(session)
    end = len(session.messages) if upto is None else min(int(upto), len(session.messages))
    if end <= start:
        return None
    # 流水合并：回填的那一份保证「这一段里每一次模型往返都有一行」，
    # 当场记下来的那一份覆盖上去。只用其中一份的话，中途接手的历史
    # （重启、子 agent fork）会少掉几步，而流水看着完整。
    #
    # 合并单位是**消息**，不是「消息 + 起点」：一条 assistant 消息里有**多步**
    # （`STEP_START` 切开），按消息合并会把同一轮的两步压成一步——实测一次四轮
    # 会话，第二轮「装族 + 用族」只留下了一步，判据「每个 Step 恰好被一个 Phase
    # 收走一次」当场不成立。但键也不能细到 Part 位置：回填按 `STEP_START` 分段，
    # 当场记的按第一个工具 Part，两边口径不同，按精确键去重会把同一步记两遍。
    # 所以「一条消息只要当场记过步，就整条以当场记的为准」——同一条消息的步
    # 是在同一轮里连续记下来的，不会只记一半。
    pending = [step for step in session.pending_steps if start <= step.message_index < end]
    recorded = {step.message_index for step in pending}
    merged = {
        (step.message_index, step.part_start): step
        for step in _steps_from_messages(session, start, end)
        if step.message_index not in recorded
    }
    for step in pending:
        merged[(step.message_index, step.part_start)] = step
    steps = [merged[key] for key in sorted(merged)]
    # 模型给的名字排第一：`close_phase` 只负责记下这一份（见 `ClosePhaseTool`），
    # 封存发生在真正的边界上。名字用过一次就清掉——下一段是另一件事，
    # 不清的话它会顶着上一个阶段的名字被封。
    title = title.strip() or session.phase_title.strip()
    summary = summary.strip() or session.phase_summary.strip()
    session.phase_title = ""
    session.phase_summary = ""
    written = bool(title)
    if not steps and not written:
        if not force:
            return None
        title = f"第 {start + 1}–{end} 条"
    if not written:
        title = _local_title(session, start, end, steps)
    if not summary.strip():
        summary = _local_summary(steps, start, end)
    event = Event(
        title=_short(title, EVENT_TITLE_MAX),
        summary=summary.strip()[:EVENT_SUMMARY_MAX],
        phase_index=_next_phase_index(session),
        steps=steps,
        start=start,
        end=end,
        tokens=_range_tokens(session, start, end),
        kind=kind if kind in EVENT_KINDS else "auto",
        degraded=not written,
    )
    # 步号在 Phase 内连号，1 起。封存时重排一次：软预算会把一个 Phase 切成两段，
    # 不重排的话下一段的步号会从 9 开始，读起来像漏了前八步。
    for position, item in enumerate(steps, 1):
        item.seq = position
    # 已经封进去的步不能再被下一阶段收一次：它们覆盖的消息已经在事件里了。
    session.pending_steps = [
        step for step in session.pending_steps if step.message_index >= end
    ]
    for position, item in enumerate(session.pending_steps, 1):
        item.seq = position
    session.add_event(event)
    session.sanitize_events()
    return event


def _render_step_raw(session: Session, step: Step) -> str:
    """一步的原文：那一次工具调用到底打印了什么。

    这是 Step 级召回存在的理由——旧设计只能召回到「事件」这一层，读到的是
    摘要；这里能一路回到某一次 `bash` 的输出正文。
    """
    from .context import render_part

    if step.message_index >= len(session.messages):
        return "（那一步的消息已经不在会话里了）"
    message = session.messages[step.message_index]
    if step.part_end > step.part_start:
        parts = message.parts[step.part_start : step.part_end]
    else:
        parts = message.parts
    blocks = [render_part(part) for part in parts]
    text = "\n\n".join(block for block in blocks if block)
    return text or "（这一步没有可读的原文）"


def render_event_detail(
    session: Session,
    event: Event,
    offset: int = 0,
    limit: int = EVENT_RECALL_LIMIT,
    step: int = 0,
) -> str:
    """一条事件的详情；`step` 给定时只给那一步的原文。

    `offset` 只作用于原文：流水与结论就那么几行，翻页翻掉它们没有意义。
    """
    from .context import render_range, render_step_line

    if step:
        found = next((item for item in event.steps if item.seq == step), None)
        if found is None:
            available = "、".join(str(item.seq) for item in event.steps) or "（没有流水）"
            return f"{event.id} 没有第 {step} 步。可用的步号：{available}"
        # 分页对这一步同样有效：一次 `bash` 的输出可以很长，召回它不该比
        # 当初读它更贵。参数说明里承诺的是「原文按 offset / limit 分页」，
        # 只对整段生效而漏掉单步，模型按说明传了 limit 也拦不住。
        rows = _render_step_raw(session, found).splitlines()
        page = rows[offset : offset + limit]
        done = offset + len(page)
        lines = [
            f"{event.id} 第 {step} 步：{found.intent}",
            f"结果：{found.result or '（没有记录）'}",
            "原文：",
            *page,
        ]
        if len(rows) > done:
            lines.append(f"（还有 {len(rows) - done} 行，用 offset={done} 继续）")
        return "\n".join(lines)

    lines = [
        f"{event.id} 阶段 {event.phase_index}：{event.title}",
        f"区间：第 {event.start + 1}–{event.end} 条　来源：{event.kind}"
        + ("（本地汇编）" if event.degraded else ""),
        f"步数：{len(event.steps)}　原文约 {event.tokens} token",
        f"结论：{event.summary or '（没有结论）'}",
    ]
    if event.steps:
        lines.append("流水：")
        lines.extend(render_step_line(item) for item in event.steps)
    if event.issues:
        lines.append("未决：" + "；".join(event.issues[:5]))
    lines.append("原文：")
    rows = render_range(session, event.start, event.end).splitlines()
    lines.extend(rows[offset : offset + limit])
    done = offset + min(limit, max(0, len(rows) - offset))
    if len(rows) > done:
        lines.append(f"（还有 {len(rows) - done} 行，用 offset={done} 继续）")
    return "\n".join(lines)


class ClosePhaseTool(Tool):
    """给当前这一大步起名字、写结论。**不切分阶段**，封存仍发生在轮末。"""

    spec = ToolSpec(
        name="close_phase",
        description=(
            "给当前这一大步（Phase）起个名字、写一句结论，封存时用它。\n"
            "时机：本阶段走到第 3 步之后，最后一条消息会多一行「本阶段已 N 步」——"
            "看到它，在给出最终答复**之前**调一次（多一个往返）。\n"
            "它**不会**把阶段切开：阶段仍在一轮结束时封存，这一步之后的调用也还记在"
            "同一个阶段里。所以一次就够，别重复调（后一次会覆盖前一次）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": f"不超过 {EVENT_TITLE_MAX} 字，写**做成了什么**（「定口袋」），不写「调用了什么工具」",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        f"不超过 {EVENT_SUMMARY_MAX} 字，这一大步的结论："
                        "做出了什么（handle、路径、关键数字，写不准就写「未产出」，不要编）、"
                        "还差什么。不写就用本地拼的「N 步、产物、未决」。"
                    ),
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    )
    event_argument = "event_runtime"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """记下标题与结论；封存时由 `seal_phase` 取用。"""
        runtime = arguments.get("event_runtime")
        if not isinstance(runtime, EventRuntime):
            return ToolResult(
                output="内核装配问题：close_phase 需要事件运行时，但这次调用没有拿到。",
                failed=True,
            )
        session = runtime.session
        title = _short(arguments.get("title"), EVENT_TITLE_MAX)
        if not title:
            # 不退回「让模型补一份」：那正是这里要取消的第二次请求。缺什么说清楚，
            # 模型下一步补上就行——这条说明会作为工具结果回灌给它。
            return ToolResult(
                output=(
                    "close_phase 缺少 title：给这一大步起个名字（不超过 40 字），"
                    "例如「准备受体」「对接与打分」。改完再调一次即可。"
                ),
                failed=True,
            )
        session.phase_title = title
        session.phase_summary = str(arguments.get("summary") or "").strip()[:EVENT_SUMMARY_MAX]
        session.touch()
        return ToolResult(
            output=(
                f"已记下这一大步的标题「{title}」（当前 {len(session.pending_steps)} 步）。"
                "封存发生在一轮结束时，这一步之后的调用仍记在同一阶段里；"
                "如果是在收尾，直接给出最终答复即可，不必再调一次。"
            ),
            meta={"title": title, "steps": len(session.pending_steps)},
        )


class RecallEventTool(Tool):
    """按编号取回一个阶段：流水、结论，以及原文或某一步的原文。"""

    spec = ToolSpec(
        name="recall_event",
        description=(
            "不带参数时列出全部阶段；带 event 编号时给出该阶段的流水、结论与原文。"
            "带 step 时只给那一步的原文（工具输出正文）。"
            "原文按 offset / limit 分页，默认 120 行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "阶段编号，例如 evt_0003"},
                "step": {
                    "type": "integer",
                    "description": "只看这一步的原文；步号在流水里每行开头",
                },
                "offset": {"type": "integer", "description": "原文从第几行开始，默认 0"},
                "limit": {
                    "type": "integer",
                    "description": f"最多返回多少行，默认 {EVENT_RECALL_LIMIT}",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    session_argument = "session"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """列清单、读一个阶段，或读某一步。"""
        session = arguments.get("session")
        events: list[Event] = list(session.events)
        if not events:
            return ToolResult(output="这份会话还没有阶段记录。")
        wanted = str(arguments.get("event") or "").strip()
        if not wanted:
            from .context import event_line

            return ToolResult(
                output="\n".join(["本会话的阶段：", *(event_line(item) for item in events)])
            )
        event = next((item for item in events if item.id == wanted), None)
        if event is None:
            available = "、".join(item.id for item in events)
            return ToolResult(
                output=f"没有 {wanted} 这个阶段。可用的编号：{available}",
                failed=True,
            )
        step = arguments.get("step") or 0
        offset = arguments.get("offset") or 0
        limit = arguments.get("limit") or EVENT_RECALL_LIMIT
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            return ToolResult(output="参数 step 应当是非负整数", failed=True)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return ToolResult(output="参数 offset 应当是非负整数", failed=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return ToolResult(output="参数 limit 应当是正整数", failed=True)
        detail = render_event_detail(
            session, event, offset=offset, limit=min(limit, 400), step=step
        )
        return ToolResult(
            output=detail,
            meta={"event": event.id, "start": event.start, "end": event.end},
        )


def domain_tools() -> list[Tool]:
    """内核事件工具，装配顺序：写在前、读在后。"""
    return [ClosePhaseTool(), RecallEventTool()]
