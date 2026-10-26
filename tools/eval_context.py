"""上下文探针：离线看一份真实会话在每一步的装配结果。

```
python tools/eval_context.py                    # 列出最近的会话
python tools/eval_context.py --session ses_xxx  # 逐个截断点打印装配估算
python tools/eval_context.py --session ses_xxx --window 32000
python tools/eval_context.py --session ses_xxx --every 4   # 每 4 条消息量一次

# 窗口扫描：同一份会话换个窗口，看「前缀命中」怎么变（决定该不该压、该压多少）
python tools/eval_context.py --sweep
python tools/eval_context.py --session ses_xxx --sweep
python tools/eval_context.py --sweep --windows 20000,40000,100000

# 证据探针：造 20 次工具运行，查关键行 / 重复 / 引用 / artifact 四条
python tools/eval_context.py --evidence
python tools/eval_context.py --evidence --window 16000 --evidence-runs 40
```

**它不调模型、不联网。** 回答的是「额度分给了谁、削到哪一层、削的时候还剩多少」，
这几件事是确定性的，可以离线量。至于「模型压缩之后还记不记得」——那要问模型，
所以脚本末尾只打印四类问题模板（recall / artifact / continuation / decision），
由人拿着它去应用里问一遍：脚本自己出题自己打分，测的是脚本不是产品。

`--sweep` 量的是另一个问题：**相邻两次装配之间，有多少内容原样留在了前缀里**。
服务端缓存按前缀命中，所以这个比例就是「缓存最多能命中多少」的估计——压缩改动了
靠前的内容，比例就掉下来。它不需要请求模型，也不受模型走哪条路影响。

`--session` 不给时列出最近 10 个会话，照抄 id 再跑一次。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opencode_tui.core.agent.context import (  # noqa: E402
    EVIDENCE_REF_LIMIT,
    TOOL_ARCHIVE_MARK,
    ContextAssembler,
    ContextAssemblyError,
    EvidenceStore,
    Layer,
    evidence_ref_of,
)
from opencode_tui.core.agent.domain.all_tools import domain_tools  # noqa: E402
from opencode_tui.core.agent.event_log import domain_tools as event_tools  # noqa: E402
from opencode_tui.core.agent.ledger import SessionLedger  # noqa: E402
from opencode_tui.core.agent.memory import MemoryStore, RecallTool, RememberTool  # noqa: E402
from opencode_tui.core.agent.prompt import (  # noqa: E402
    estimate_tokens,
    is_key_line,
    stable_prefix,
)
from opencode_tui.core.agent.spawn import AgentStatusTool, SpawnAgentTool  # noqa: E402
from opencode_tui.core.agent.task import TaskTool  # noqa: E402
from opencode_tui.core.agent.toolset import LoadToolsTool, hidden_names  # noqa: E402
from opencode_tui.core.agent.tools import (  # noqa: E402
    BashTool,
    GrepTool,
    PatchTool,
    ReadTool,
    ReadTurnTool,
    Tool,
    ToolRegistry,
    WriteTool,
)
from opencode_tui.core.agent.workspace import Workspace  # noqa: E402
from opencode_tui.core.models import Message, Part, PartType, Session, ToolStatus  # noqa: E402
from opencode_tui.core.session_manager import SessionManager, default_state_path  # noqa: E402

DEFAULT_WINDOW = 128_000
"""与常见模型的窗口一致；量别的模型时用 `--window` 覆盖。"""

WINDOW_SWEEP: tuple[int, ...] = (20_000, 30_000, 40_000, 60_000, 100_000, 200_000)
"""默认扫这些窗口：从「明显要压」到「完全不用压」，看清拐点在哪。"""

SAMPLE_TURNS = 12
"""没有会话可量时造多少轮样本。每轮 = 用户一句 + 两次工具 + 一段结论。"""

PROBES: tuple[tuple[str, str], ...] = (
    ("recall", "刚才那次 <工具> 用的 <参数> 是什么？结果如何？"),
    ("artifact", "这一步产出的文件在哪？内容的关键数字是多少？"),
    ("continuation", "按目前的进度，接下来本来要做哪一步？"),
    ("decision", "当时为什么选了 A 而不是 B？被否掉的方案是什么？"),
)
"""压缩之后最该还能答上来的四类问题。尖括号里的部分要照着会话换。"""


def tool_specs(
    store: MemoryStore | None = None, families: tuple[str, ...] = ()
) -> list[dict]:
    """按应用的装配顺序拼出**这一步真会发出去**的工具定义。

    2026-10 起工具按族加载：核心那一批（文件、登记表、任务、事件、记忆、
    `load_tools`）常驻，领域工具要模型自己装。所以这里不能直接拿全量去量——
    全量 34 个约 11.3K token，默认只发 10 个约 2.1K，差了 5 倍，
    拿全量算出来的「固定开销」会把预算说小一大截。
    `families` 模拟「这一轮已经装过哪几族」。

    顺序与 `all_tools.domain_tools` 一致。这里量的是 token 数量级，
    不关心模型会选中哪一个——那件事得真跑一轮才看得出来。
    """
    registry = ToolRegistry()
    core: list[Tool] = [
        BashTool(),
        ReadTool(),
        WriteTool(),
        PatchTool(),
        GrepTool(),
        ReadTurnTool(),
        TaskTool(),
        LoadToolsTool(),
        SpawnAgentTool(),
        AgentStatusTool(),
        *event_tools(),
    ]
    if store is not None:
        core.extend([RememberTool(store), RecallTool(store)])
    for tool in core:
        registry.register(tool)
    for tool in domain_tools():
        registry.register(tool)
    session = Session(id="ses_eval")
    session.tool_families = list(families)
    return registry.specs(exclude=hidden_names(session))


def build_at(
    session,
    index: int,
    window: int,
    prefix: str,
    specs: list[dict],
    memory: str,
):
    """把消息截到第 `index` 条，装配一次。

    截断的是**副本**：这份会话还要接着往下量，就地改会把后面的结果污染掉，
    而且用户的状态文件就在旁边，改错了就是真的改错了。
    """
    sliced = copy.deepcopy(session)
    sliced.messages = sliced.messages[:index]
    task_text = "" if sliced.task_state.is_empty() else sliced.task_state.render()
    return ContextAssembler(window).build(
        sliced,
        prefix,
        tools=specs,
        memory_text=memory,
        task_text=task_text,
    )


def parse_families(raw: str) -> tuple[str, ...]:
    """解析 `--families docking,structure`；空串表示只发核心工具。

    族名不校验：写错了 `tool_specs` 里那一族照旧不出现，量出来的数字会偏小。
    这里宁可偏小也不擅自补——补上了会让人以为「装过」，而实际没装。
    """
    return tuple(piece.strip() for piece in raw.split(",") if piece.strip())


def parse_windows(raw: str) -> tuple[int, ...]:
    """解析 `--windows 20000,40000`；空串用默认那组。"""
    if not raw.strip():
        return WINDOW_SWEEP
    values: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            values.append(int(piece))
        except ValueError:
            raise SystemExit(f"窗口要写整数：{piece!r}") from None
    return tuple(values) or WINDOW_SWEEP


def sample_session(turns: int = SAMPLE_TURNS) -> Session:
    """造一份形状接近真实长会话的样本：每轮读两份文档，工具输出偏大。

    没有真实会话可量时用它。它只回答「窗口与压缩的关系」——比例与真实会话
    接近就够了，绝对值不必当真。
    """
    session = Session(id="ses_sample", title="窗口扫描样本")
    for index in range(turns):
        user = session.add_message(Message(role="user"))
        user.add_part(Part(type=PartType.TEXT, text=f"第 {index} 轮：读一下文档再总结。"))
        assistant = session.add_message(Message(role="assistant"))
        for slot in range(2):
            assistant.add_part(
                Part(
                    type=PartType.TOOL,
                    tool_name="read",
                    tool_status=ToolStatus.COMPLETED,
                    tool_input={"path": f"docs/doc{index}-{slot}.md"},
                    tool_output=f"文件 {index}-{slot}：" + "正文内容" * 350,
                )
            )
        assistant.add_part(Part(type=PartType.TEXT, text="读完了，结论是" + "结论" * 100))
    return session


def without_compaction(session: Session) -> Session:
    """同一批消息，去掉摘要、事件与压缩位置。

    窗口扫描要回答的是「这个窗口下会怎么压」，带上已经产生的摘要与事件，
    量出来的就是历史包袱而不是当前窗口。所以扫描前把它们清零。
    """
    clean = Session(id=session.id, title=session.title)
    clean.messages = list(session.messages)
    clean.task_state = session.task_state
    return clean


def request_blocks(messages: list[dict]) -> list[str]:
    """把一次请求拍成「每条消息一段」，供逐条比前缀。"""
    blocks: list[str] = []
    for item in messages:
        pieces = [str(item.get("role") or "")]
        if item.get("content"):
            pieces.append(str(item["content"]))
        if item.get("tool_calls"):
            pieces.append(json.dumps(item["tool_calls"], ensure_ascii=False))
        blocks.append("\n".join(pieces))
    return blocks


def common_prefix_tokens(left: list[str], right: list[str]) -> int:
    """两次请求从开头起相同的那一段折成 token 数。

    服务端缓存按前缀命中，所以这个数就是「这次能命中多少」的估计。
    同一条消息内部也可能只改了后半段（摘要在变长），所以逐字符找分叉点。
    """
    total = 0
    for before, after in zip(left, right):
        if before == after:
            total += estimate_tokens(before)
            continue
        limit = 0
        for left_char, right_char in zip(before, after):
            if left_char != right_char:
                break
            limit += 1
        total += estimate_tokens(before[:limit])
        break
    return total


def measure_window(
    session,
    window: int,
    prefix: str,
    specs: list[dict],
    memory: str,
) -> dict:
    """在一份会话上跑某个窗口：逐条追加消息，量前缀命中与压缩次数。"""
    assembler = ContextAssembler(window)
    task_text = "" if session.task_state.is_empty() else session.task_state.render()
    previous: list[str] | None = None
    previous_marks = 0
    hit = total = 0
    cleared = dropped = failed = 0
    for index in range(1, len(session.messages) + 1):
        sliced = Session(id=session.id)
        sliced.messages = session.messages[:index]
        sliced.task_state = session.task_state
        try:
            built = assembler.build(
                sliced, prefix, tools=specs, memory_text=memory, task_text=task_text
            )
        except ContextAssemblyError:
            failed += 1
            continue
        blocks = request_blocks(built.messages)
        size = estimate_tokens("\n".join(blocks))
        total += size
        if previous is not None:
            hit += common_prefix_tokens(previous, blocks)
        previous = blocks
        # 「清过正文」数的是**请求里真的多了一条占位**的步：只看 degraded 会虚高，
        # 因为清了一个后来没装下的组也算进过那一级。
        marks = sum(
            1
            for item in built.messages
            if item.get("role") == "tool"
            and str(item.get("content") or "").startswith(TOOL_ARCHIVE_MARK)
        )
        if marks > previous_marks:
            cleared += 1
        previous_marks = marks
        if built.dropped:
            dropped += 1
    return {
        "window": window,
        "rate": hit / total if total else 0.0,
        "cleared": cleared,
        "dropped": dropped,
        "failed": failed,
        "input": total,
    }


def sweep_table(
    session,
    prefix: str,
    specs: list[dict],
    memory: str,
    windows: tuple[int, ...],
    label: str,
    families: tuple[str, ...] = (),
) -> int:
    """把若干窗口的结果排成一张表。"""
    budget = ContextAssembler(windows[0]).budget(specs)
    print(label)
    print(
        f"固定开销：稳定前缀 {estimate_tokens(prefix)}　工具定义 {budget.tools_tokens}"
        f"　常驻记忆 {estimate_tokens(memory)}"
    )
    print(
        "工具清单："
        + (f"{len(specs)} 个（已装族 {'、'.join(families)}）" if families else f"{len(specs)} 个（只有核心）")
    )
    print("窗口　　前缀命中　换占位步　丢过组　装不下　总输入")
    starred = False
    for window in windows:
        result = measure_window(session, window, prefix, specs, memory)
        rate = f"{result['rate'] * 100:>6.1f}%"
        if result["failed"]:
            # 有装不下的步时，命中率只覆盖装得下的那些，不能与其它行直接比。
            rate += "*"
            starred = True
        print(
            f"{result['window'] // 1000:>4}K"
            f"　{rate:>7}"
            f"　{result['cleared']:>8}"
            f"　{result['dropped']:>6}"
            f"　{result['failed']:>6}"
            f"　{result['input'] / 1000:>7.1f}K"
        )
    print("\n前缀命中 = 相邻两次请求共同前缀的 token 占比，是缓存命中率的上界估计。")
    print("它只由「前缀多久变一次」决定，而压缩是让它变的唯一原因。")
    print("「换占位步」数的是**这一步请求里多了一条占位**；`degraded` 里的")
    print("`recent_tool_clear` 是状态（这一份上下文中含有占位），会一直挂着，别拿它当次数。")
    if starred:
        print("带 * 的行有装不下的步骤（固定开销 + 内容超出窗口），")
        print("那里的命中率只统计装得下的那些，不能与其它行直接比。")
    return 0


# ---------------------------------------------------------------------------
# 证据探针
# ---------------------------------------------------------------------------
#
# 要回答的四件事（计划 §Phase 5），全部**对着装配结果**核，不看中间变量：
#
# 1. 关键结果 / 报错还在不在——RECENT 里已经看不见的那几条，关键行该由证据层接住；
# 2. 证据层有没有在复制 RECENT——同一条原文在上下文里出现两遍就是重复；
# 3. 这次任务用过哪个 artifact——handle 还答不答得出来，多少只能靠工作区索引；
# 4. 引用能不能回到出处——ref 必须是从这次调用的入参或产物里来的，不是一句「工具名」。
#
# 不联网、不调模型：造 N 次工具运行，按应用的方式逐条喂给 `EvidenceStore`，
# 每几步装配一次，然后读那次装配出来的消息。

EVIDENCE_RUNS = 20
"""固定跑这么多次工具：够把证据层的 12 条滚动窗口与 RECENT 的压缩都撑开。"""

EVIDENCE_WINDOW = 12_000
"""证据探针默认的窗口：要小到能挤出 RECENT，又要大到装得下稳定前缀。

前缀（身份 + 工作纪律 + 本仓 `AGENTS.md`）实测约 5.4K token，再加 2K 输出预留，
8K 窗口连前缀都放不下——那样量到的全是「装不下」，什么也说明不了。"""

EVIDENCE_EVERY = 4
"""每这么多次运行量一次。每次都量会把表撑得很长，而变化的只是边界。"""


@dataclass(frozen=True)
class RunSpec:
    """一次工具运行的形状。造样本用，不参与装配。"""

    name: str
    inputs: dict
    output: str
    primary: str = ""
    keys: tuple[str, ...] = ()
    failed: bool = False


def _filler(tag: str, count: int = 30) -> str:
    """一段过程输出。**故意比关键行长得多**，这样「留下哪些行」才有意义。"""
    return "\n".join(
        f"{tag} 第 {row} 行：读取中，累计 {row * 7} 条。" for row in range(count)
    )


def evidence_runs(count: int = EVIDENCE_RUNS) -> list[RunSpec]:
    """造五种工具运行，轮着来：读文件、grep、失败的 bash、对接、语料检索。

    每一类的关键行都**带上序号**：关键行在样本里重复的话，「这条还在不在」
    就分不清是哪一条还在了。
    """
    runs: list[RunSpec] = []
    for index in range(count):
        slot = index % 5
        if slot == 0:
            runs.append(
                RunSpec(
                    name="read",
                    inputs={"path": f"docs/design-{index}.md"},
                    output=_filler(f"文件 {index}")
                    + f"\n结论：第 {index} 次读取共 42 条记录，其中 3 条异常",
                    primary="path",
                )
            )
        elif slot == 1:
            runs.append(
                RunSpec(
                    name="grep",
                    inputs={"pattern": "TODO", "path": f"src/mod-{index}"},
                    output=_filler(f"src/mod-{index}")
                    + f"\n命中第 {index} 批：2 处\nsrc/mod-{index}.py:12: TODO 补测试",
                    primary="pattern",
                    keys=("path",),
                )
            )
        elif slot == 2:
            runs.append(
                RunSpec(
                    name="bash",
                    inputs={"command": f"python tools/check.py --round {index}"},
                    output=(
                        "Traceback (most recent call last):\n"
                        f'  File "tools/check.py", line {index}, in <module>\n'
                        f"Error: 第 {index} 次检查缺少参数 --state"
                    ),
                    primary="command",
                    failed=True,
                )
            )
        elif slot == 3:
            runs.append(
                RunSpec(
                    name="dock",
                    inputs={"receptor": "receptor#1", "ligands": "ligandset#1"},
                    output=_filler(f"对接 {index}")
                    + f"\n结论：第 {index} 批产出 12 个姿态，最优 -9.8 kcal/mol",
                    primary="receptor",
                    keys=("ligands",),
                )
            )
        else:
            runs.append(
                RunSpec(
                    name="search_corpus",
                    inputs={"query": f"EGFR 第 {index} 批"},
                    output=_filler(f"检索 {index}")
                    + f"\n命中第 {index} 批：3 段\ndoc:egfr#chunk-{index}",
                    primary="query",
                )
            )
    return runs


def _tool_stub(spec: RunSpec) -> SimpleNamespace:
    """只带定位参数声明的假工具：`evidence_ref_of` 只需要这两个属性。"""
    return SimpleNamespace(primary_argument=spec.primary, evidence_key=spec.keys)


def _probe_line(spec: RunSpec) -> str:
    """这次运行里最该被留住的那一行，**而且必须只属于这一次**。

    取最长的那条关键行，不取第一条：报错的回溯每一条都以
    `Traceback (most recent call last):` 开头，拿它当指纹的话，两次失败互相
    就算成了「重复」——量到的是样本的写法，不是产品的问题。
    """
    keys = [line.strip() for line in spec.output.splitlines() if is_key_line(line)]
    if not keys:
        return spec.output.splitlines()[0].strip()
    return max(keys, key=len)


def _visible_tool_ids(messages: list[dict]) -> set[str]:
    """这一步仍在 RECENT 里、且没被换成占位的工具调用 id。"""
    visible: set[str] = set()
    for item in messages:
        if item.get("role") != "tool" or not item.get("tool_call_id"):
            continue
        if (item.get("content") or "").startswith(TOOL_ARCHIVE_MARK):
            continue
        visible.add(item["tool_call_id"])
    return visible


def _traceable(ref: str, part: Part, spec: RunSpec) -> bool:
    """引用是不是「这次调用的那一处」：必须点到一个入参键，或者点到产物 handle。

    只剩工具名（`grep`、`bash`）就不算——同一个工具跑二十次会共用一条，
    而那意味着证据层里互相覆盖，翻回去根本不知道是哪一次。
    """
    if not ref or "\n" in ref or len(ref) > EVIDENCE_REF_LIMIT:
        return False
    body = ref[len(part.tool_name) :].strip()
    if not body:
        return False
    handle = str((part.tool_meta or {}).get("handle") or "")
    if handle and handle in body:
        return True
    return any(body.startswith(f"{key}=") or f"+{key}=" in body for key in spec.inputs)


def _build_sample(specs: list[RunSpec]) -> tuple[Session, SessionLedger, list[Part]]:
    """造出这 N 次运行的消息与登记表；dock 的 handle 现登记现取。"""
    session = Session(id="ses_evidence", title="证据探针")
    opening = session.add_message(Message(role="user"))
    opening.add_part(Part(type=PartType.TEXT, text="按顺序把这些都跑一遍，最后给结论。"))
    ledger = SessionLedger(session_id="")
    ledger.add("receptor", summary="4HJO 的 EGFR 激酶域")
    ledger.add("ligandset", summary="原始配体库 5000 个")
    parts: list[Part] = []
    for index, spec in enumerate(specs, start=1):
        meta: dict = {}
        if spec.name == "dock":
            entry = ledger.add("artifact", summary=f"第 {index} 批对接结果 12 个姿态")
            meta["handle"] = str(entry.handle)
        assistant = session.add_message(Message(role="assistant"))
        part = Part(
            type=PartType.TOOL,
            tool_name=spec.name,
            tool_input=dict(spec.inputs),
            tool_output=spec.output,
            tool_meta=meta,
            tool_status=ToolStatus.FAILED if spec.failed else ToolStatus.COMPLETED,
        )
        assistant.add_part(part)
        parts.append(part)
    return session, ledger, parts


def _ref_collisions(refs: list[str], specs: list[RunSpec]) -> list[str]:
    """两次不同的调用用了同一条 ref。按 ref 替换的证据层会因此丢条目。"""
    seen: dict[str, str] = {}
    problems: list[str] = []
    for ref, spec in zip(refs, specs):
        if ref in seen:
            problems.append(f"引用撞车：{ref!r}（{spec.name} 与更早的 {seen[ref]} 共用一条）")
        else:
            seen[ref] = spec.name
    return problems


def evidence_table(window: int = EVIDENCE_WINDOW, count: int = EVIDENCE_RUNS) -> int:
    """跑一遍证据探针，打印每个检查点的覆盖情况；有违规返回 1。"""
    specs = evidence_runs(max(1, count))
    session, ledger, parts = _build_sample(specs)
    stubs = [_tool_stub(spec) for spec in specs]
    refs = [
        evidence_ref_of(part, dict(part.tool_meta or {}), stub)
        for part, stub in zip(parts, stubs)
    ]

    prefix = stable_prefix(Workspace(session.id, PROJECT_ROOT), session)
    store = EvidenceStore()

    failures: list[str] = []
    untraceable = [
        f"引用不可追溯：{ref!r}（{spec.name}）"
        for ref, part, spec in zip(refs, parts, specs)
        if not _traceable(ref, part, spec)
    ]
    failures.extend(untraceable)
    failures.extend(_ref_collisions(refs, specs))

    print(f"窗口 {window}　工具运行 {len(specs)} 次　证据层上限 {store.limit} 条")
    print("运行　消息　证据　在RECENT　证据保住　只留摘要　让位　重复　摘要重合　artifact")
    duplicates = echoes = 0
    counts = {"recent": 0, "kept": 0, "summary": 0, "yield": 0}

    for index, (part, spec) in enumerate(zip(parts, specs), start=1):
        store.add_from_part(part, _tool_stub(spec))
        if index % EVIDENCE_EVERY and index != len(parts):
            continue
        sliced = copy.deepcopy(session)
        sliced.messages = sliced.messages[: 1 + index]
        try:
            built = ContextAssembler(window).build(
                sliced,
                prefix,
                tools=[],
                digest_text=ledger.digest(),
                evidence=store.items(),
            )
        except ContextAssemblyError as error:
            print(f"{index:>4}　装不下：{error}")
            failures.append(f"第 {index} 次之后装配失败")
            continue

        text = "\n".join(str(item.get("content") or "") for item in built.messages)
        visible = _visible_tool_ids(built.messages)
        counts = {"recent": 0, "kept": 0, "summary": 0, "yield": 0}
        duplicates = echoes = 0
        for done, done_ref, done_spec in zip(parts[:index], refs[:index], specs[:index]):
            line = _probe_line(done_spec)
            seen = text.count(line)
            if done.id in visible:
                # 来源还在 RECENT 里：那一行出现两遍是「原文 + 证据摘要」。摘要一行
                # 都不给的话，它滚出去之后连索引都没有了——这是刻意的。
                echoes += 1 if seen > 1 else 0
                counts["recent"] += 1
            elif seen > 1:
                duplicates += 1
                failures.append(f"第 {index} 次时「{line[:20]}」出现了 {seen} 遍")
                counts["kept"] += 1
            elif seen:
                counts["kept"] += 1
            elif done_ref and done_ref in text:
                counts["summary"] += 1
            else:
                counts["yield"] += 1

        handles = [
            f"artifact#{number}"
            for number in range(1, len(ledger.find(kind="artifact")) + 1)
        ]
        answerable = sum(1 for handle in handles if handle in text)
        by_index = sum(
            1
            for handle in handles
            if handle not in text and handle in ledger.digest()
        )
        print(
            f"{index:>4}　{len(sliced.messages):>4}"
            f"　{len(store.items()):>4}"
            f"　{counts['recent']:>8}"
            f"　{counts['kept']:>8}"
            f"　{counts['summary']:>8}"
            f"　{counts['yield']:>4}"
            f"　{duplicates:>4}"
            f"　{echoes:>8}"
            f"　{answerable}/{len(handles)}"
            + (f"（索引 +{by_index}）" if by_index else "")
        )

    archived = counts["kept"] + counts["summary"] + counts["yield"]
    print()
    print(
        f"已经不在 RECENT 里的 {archived} 次调用："
        f"关键行保住 {counts['kept']}，只留摘要 {counts['summary']}，"
        f"让位 {counts['yield']}"
    )
    print("重复原文　" + ("0 处" if not duplicates else f"{duplicates} 处"))
    print("摘要重合　" + f"{echoes} 处（来源还在 RECENT 里时，摘要与原文各一行，属于设计）")
    print("引用不可追溯　" + ("0 条" if not untraceable else f"{len(untraceable)} 条"))
    if failures:
        print("\n不合规：")
        for line in failures[:10]:
            print(f"  · {line}")
        if len(failures) > 10:
            print(f"  … 还有 {len(failures) - 10} 条")
        return 1
    print("\n四条都过：没有重复、引用都能追溯且不撞车、artifact 答得出来。")
    print("「让位」是证据层 12 条的滚动窗口挤掉的，属于设计内；要它别掉，加证据层的额度。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="上下文装配探针（离线）")
    parser.add_argument("--state", default=None, help="state.toml 路径，默认用户目录那一份")
    parser.add_argument("--session", default="", help="会话 id；不给则列出最近的会话")
    parser.add_argument(
        "--window",
        type=int,
        default=0,
        help=f"模型窗口；不给时按模式取默认（装配 {DEFAULT_WINDOW} / 证据探针 {EVIDENCE_WINDOW}）",
    )
    parser.add_argument("--every", type=int, default=1, help="每 N 条消息量一次")
    parser.add_argument("--sweep", action="store_true", help="扫一组窗口，看前缀命中")
    parser.add_argument(
        "--evidence",
        action="store_true",
        help=f"证据探针：造 {EVIDENCE_RUNS} 次工具运行，查关键行/重复/引用/artifact",
    )
    parser.add_argument(
        "--evidence-runs", type=int, default=EVIDENCE_RUNS, help="配合 --evidence：跑多少次"
    )
    parser.add_argument("--windows", default="", help="配合 --sweep：逗号分隔的窗口列表")
    parser.add_argument(
        "--families",
        default="",
        help="模拟已经 load_tools 装过的族，逗号分隔（默认只有核心工具）",
    )
    parser.add_argument(
        "--turns", type=int, default=SAMPLE_TURNS, help="没有会话可量时造多少轮样本"
    )
    args = parser.parse_args()

    window = args.window or (EVIDENCE_WINDOW if args.evidence else DEFAULT_WINDOW)
    if args.evidence:
        return evidence_table(window, args.evidence_runs)

    manager = SessionManager(Path(args.state) if args.state else default_state_path())
    # 构造只记下路径，读盘要显式来一次：应用也是启动时才 load，
    # 这里省掉的话任何状态文件都会显示成「没有会话」。
    manager.load_from_toml()
    sessions = sorted(manager.sessions.values(), key=lambda item: item.updated_at, reverse=True)
    if not args.session and not args.sweep:
        if not sessions:
            print(f"没有会话（状态文件：{manager.state_path}）")
            return 1
        print(f"状态文件　{manager.state_path}")
        for session in sessions[:10]:
            print(
                f"  {session.id}　{session.updated_at:.0f}"
                f"　{len(session.messages):>3} 条　{session.title or '（无标题）'}"
            )
        print("\n用 --session <id> 看某一份的装配估算，或加 --sweep 扫窗口。")
        return 0

    if args.sweep:
        windows = parse_windows(args.windows)
        if args.session:
            session = manager.get_session(args.session)
            if session is None:
                print(f"没有这个会话：{args.session}")
                return 1
            workspace = Workspace(session.id, PROJECT_ROOT)
            workspace.ensure()
            target = without_compaction(session)
            label = f"会话 {session.id}（{len(target.messages)} 条消息，已忽略既有摘要/事件）"
        else:
            target = sample_session(max(1, args.turns))
            workspace = Workspace(target.id, PROJECT_ROOT)
            workspace.ensure()
            label = f"内置样本（{len(target.messages)} 条消息，{max(1, args.turns)} 轮）"
        prefix = stable_prefix(workspace, target)
        store = MemoryStore(PROJECT_ROOT)
        store.load()
        families = parse_families(args.families)
        specs = tool_specs(store, families)
        return sweep_table(
            target, prefix, specs, store.resident_text(), windows, label, families
        )

    session = manager.get_session(args.session)
    if session is None:
        print(f"没有这个会话：{args.session}")
        return 1

    workspace = Workspace(session.id, PROJECT_ROOT)
    workspace.ensure()
    prefix = stable_prefix(workspace, session)
    store = MemoryStore(PROJECT_ROOT)
    store.load()
    families = parse_families(args.families)
    specs = tool_specs(store, families)
    memory = store.resident_text()

    budget = ContextAssembler(window).budget(specs)
    print(f"会话　{session.id}　{session.title or '（无标题）'}")
    print(
        f"窗口 {window}　消息 {len(session.messages)} 条"
        f"　可用 {budget.available}"
        f"　工具定义 {budget.tools_tokens}"
        f"　常驻记忆 {len(memory)} 字符"
    )
    print("条数　装配　recent　evidence　history　移出　降级")

    step = max(1, args.every)
    for index in range(1, len(session.messages) + 1):
        if index % step and index != len(session.messages):
            continue
        try:
            built = build_at(session, index, window, prefix, specs, memory)
        except ContextAssemblyError as error:
            print(f"{index:>5}　装不下：{error}")
            continue
        usage = built.usage
        levels = "、".join(built.degraded) or "-"
        print(
            f"{index:>5}　{built.metrics['total']:>7}"
            f"　{usage.get(Layer.RECENT, 0):>7}"
            f"　{usage.get(Layer.EVIDENCE, 0):>8}"
            f"　{usage.get(Layer.HISTORY, 0):>7}"
            f"　{built.dropped:>4}"
            f"　{levels}"
        )

    print("\n四类探针（拿去应用里照着问一遍，看压缩之后还答不答得上）：")
    for name, question in PROBES:
        print(f"  [{name}] {question}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
