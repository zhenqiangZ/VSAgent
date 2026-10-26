# 结构化上下文设计（ContextBundle）

> 目标：把上下文从「会话历史的裁剪器」改成「面向当前一步的装配器」。
> 完整历史是档案，发给模型的那一份是为这一步选出来的工作材料，两者不再追求一致。

本文是设计，不是实现。实施时以本文的结构与不变式为准，改动点见第七节。

> 配套文档：`context-plan.md`（实施计划与任务清单）、`memory-design.md`（`MEMORY` 层的数据来源）、
> `subagent-design.md`（`4b` 子代理上下文隔离的设计，单独成文）。

---

## 一、设计原则

1. **上下文是有限注意力预算，不是存储。** 优化目标不是「塞满窗口」，而是「最小的高信号 token 集合」。token 越多，注意力越被稀释（context rot）。
2. **分层，各层淘汰策略不同。** 稳定前缀不丢、任务状态不丢、近期执行成组进出、证据先摘要后正文、历史只留引用。
3. **前缀字节稳定。** 越靠前、越不动的内容越要稳定，才能命中 prompt cache。
4. **交互组原子。** 一次 `tool_call` 与它的结果不可打散。
5. **压缩按内容类型选择，不按字符位置截断。** 丢冗余输出与中间推理，保决策、结论、约束、报错原文。
6. **可追溯。** 被移出上下文的内容，在历史层留下引用，之后能取回。

---

## 二、分层模型

装配顺序即下表顺序（越靠上越稳定、越优先保留）。

| 层 | 名字 | 内容 | 来源 | 淘汰策略 |
| --- | --- | --- | --- | --- |
| `PREFIX` | 稳定前缀 | 身份、工作纪律、`AGENTS.md` 规则 | `prompt.build_system_prompt` | **不丢**；追求字节稳定 |
| `MEMORY` | 常驻记忆 | 项目核心事实（跨会话） | `MemoryStore.resident_text` | pinned，但受 token 上限 |
| `TASK` | 任务状态 | 目标、用户约束、已完成、待办、阻塞；**工作区索引**（当前受体 + 最近几个产物的 handle）；**运行环境**（日期、工作目录、状态目录） | `Session.task_state` + `SessionLedger.digest()` + `prompt.environment_block()` | **不丢**（pinned） |
| `RECENT` | 近期执行 | 最近若干个完整交互组 | `Session.messages` 翻译层 | 以**交互组**为单位进出 |
| `EVIDENCE` | 按需证据 | 本步相关的文件片段、报错、产物详情，以及检索回来的记忆 | 工具结果 / Ledger / `recall` | 先摘要，正文按预算展开 |
| `HISTORY` | 历史 | 旧对话摘要 + 可追溯引用 | `Session.summary` | 只留引用，正文不驻留 |

**关键差别**：现有实现只有 `PREFIX + RECENT` 两层，外加一段直接拼在 `system` 之后的常驻记忆（`memory_text`）与一段隐式摘要，且 `RECENT` 的淘汰单位是「消息」。分层之后，每层的去留由**类型**决定，而不是由「新旧」和「字符位置」决定。

> `MEMORY` 层的完整设计（范围、类型、存储、护栏）见 `memory-design.md`。记忆是**来源**，上下文是**产物**：常驻记忆进 `MEMORY` 层，检索回来的记忆进 `EVIDENCE` 层。

---

## 三、数据结构

新增文件 `opencode_tui/core/agent/context.py`。以下为设计稿。

### 3.1 层与策略

```python
class Layer(Enum):
    """上下文的一层。装配顺序即枚举顺序。"""
    PREFIX = "prefix"      # 稳定前缀
    MEMORY = "memory"      # 常驻记忆（核心 FACT，跨会话）
    TASK = "task"          # 任务状态
    RECENT = "recent"      # 近期执行
    EVIDENCE = "evidence"  # 按需证据
    HISTORY = "history"    # 历史


# 每层的淘汰策略，装配器据此决定「能不能丢、丢到什么程度」。
LAYER_POLICY: dict[Layer, str] = {
    Layer.PREFIX: "pinned",   # 永不丢
    Layer.MEMORY: "capped",   # 整块保留但受 token 上限；超限先砍旧条目
    Layer.TASK: "pinned",     # 永不丢
    Layer.RECENT: "atomic",   # 以完整交互组为单位进出，组内不切开
    Layer.EVIDENCE: "soft",   # 先给 summary，正文按预算展开
    Layer.HISTORY: "ref",     # 只留引用与摘要
}
```

> `MEMORY` 与其余五层不同，**它不是 dataclass**：内容由 `MemoryStore.resident_text(limit)`
> 渲染成一段纯文本，上限与「核心 > 旧条目」的淘汰都由 `MemoryStore` 负责（见 `memory-design.md`）。
> 装配器只把它当一段受上限约束的文本放进 `MEMORY` 层，位置紧随 `PREFIX`。

### 3.2 任务状态

```python
@dataclass
class TaskState:
    """结构化的任务状态，独立于聊天记录。

    它是「用户约束不能因为丢弃就没了」这条要求的归处：约束存在这里，
    而不是靠摘要的第四段兜住。淘汰优先级最低，永远发得出去。
    """

    goal: str = ""
    constraints: list[str] = field(default_factory=list)  # 用户明确给出的要求
    done: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    version: int = 0  # 每次更新 +1，供 trace 与判据比对

    def is_empty(self) -> bool:
        return not (
            self.goal or self.constraints or self.done
            or self.todo or self.blockers
        )

    def render(self) -> str:
        blocks = ["（本会话的任务状态，随进度更新）"]
        if self.goal:
            blocks.append(f"目标：{self.goal}")
        if self.constraints:
            blocks.append(
                "用户约束（每一步都生效）：\n"
                + "\n".join(f"- {item}" for item in self.constraints)
            )
        if self.done:
            blocks.append("已完成：\n" + "\n".join(f"- {item}" for item in self.done))
        if self.todo:
            blocks.append("待办：\n" + "\n".join(f"- {item}" for item in self.todo))
        if self.blockers:
            blocks.append("阻塞：\n" + "\n".join(f"- {item}" for item in self.blockers))
        return "\n".join(blocks)

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState": ...
```

`TaskState` 挂在 `Session` 上（`models.py` 增字段），随状态文件一起原子写出。

> **实际位置与上面这份草稿不同**：`TaskState` 实际放在 `core/models.py`，与 `Session` 同处，
> **不在 `context.py`**。原因是 `context.py` 已经从 `models.py` 导入 `Message` / `Session`，
> 反过来放会成环。`context.py` 只把它当一段文本收（`build(..., task_text=)`），不认这个类型。
> 更新入口（模型工具 `task`）放在 `core/agent/task.py`：数据在数据层，工具在工具层。

### 3.3 按需证据

```python
@dataclass(frozen=True)
class Evidence:
    """一条按需证据。ref 是可追溯的轻量标识，detail 才是正文。

    「按需」体现在两处：只有本步相关的才进上下文；进了之后先给 summary，
    正文 detail 只在预算允许时展开。ref 留在历史层，之后还能取回。
    """

    ref: str          # handle（receptor#1）或 路径#行号 或 URL
    summary: str      # 一行摘要，永远给模型看
    detail: str = ""  # 正文，预算允许时才展开
```

这与 Ledger 的 handle 是同一套思路：**上下文里放索引，不放整份内容**。Ledger 已经是这个模式的雏形，这里把它推广到证据层。

> **实现位置与来源（第 4 期 · C4-1 已实施）**：`Evidence` 与收集器 `EvidenceStore` 都在 `context.py`。
> 收集器**只收两类**：
>
> - 检索回来的记忆（`recall`）——跨会话的知识，`RECENT` 里本来就没有它；
> - 含关键行的工具结果——报错、结论、回溯那几行，判定复用第 3 期的 `is_key_line`。
>
> 每条工具输出都收一份的话，证据层就成了 `RECENT` 的复制品，白占额度还挤掉真正要留的东西。
> 收进来的因此是**挑过的关键行**，它的价值在于 `RECENT` 被压缩或被移出之后这几行仍然在。
> 条数按 `EvidenceStore.limit` 滚动；同一条引用（工具名 + 定位参数）再出现是**替换**而不是追加，
> 同一个文件读三次不会占掉三条。失败的工具结果**保底收**：失败原因就是下一步的输入，
> 而它常常是短的一句（「缺少参数 text」），关键词表盖不住这种。
>
> 工具通过 `Tool.evidence_argument` 拿到收集器，与 `context_argument` / `task_argument` 同一套注入。
> 循环层在 `_execute_tool` 跑完之后提炼、`_assemble` 时带上，两个入口（`stream_reply` / `run_bash`）
> 各清空一次——证据是「本步相关」的，跨轮留着的多半已经不相干。
>
> **本期只接这两类。** Ledger 的产物详情没有接：`list_workspace` 已经能列，
> 而产物详情的体积不可控，收进来容易把证据层的额度吃光。

### 3.4 交互组

```python
@dataclass
class Turn:
    """一个完整交互组：一次 assistant step 及它触发的工具结果。

    交互组是 RECENT 层的原子单位。tool_call 与它的结果必须同进同出——
    切开会给模型留下「调了工具却没有结果」或「结果没有对应调用」的孤儿形状，
    部分服务端直接返回 400。
    """

    index: int
    messages: list[dict[str, Any]]
    tokens: int = 0
```

### 3.5 装配结果

```python
@dataclass
class ContextBundle:
    """装配结果：发给模型的消息，以及每层用了多少、丢了什么、降了几级。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[Layer, int] = field(default_factory=dict)  # 每层估算 token
    dropped_turns: list[int] = field(default_factory=list)  # 被移出的交互组序号
    dropped: int = 0                                        # 兼容旧字段：丢了几条消息
    degraded: list[str] = field(default_factory=list)       # 这一轮触发的降级步骤
```

---

## 四、装配算法

装配器 `ContextAssembler`：

```python
class ContextAssembler:
    def __init__(
        self,
        context_window: int,
        output_reserve: int = 4096,   # 给模型输出的预留
        ratio: float = 0.8,
    ) -> None: ...

    def build(
        self,
        session: Session,
        stable_prefix: str,
        tools: list[dict[str, Any]],
        evidence: list[Evidence] | None = None,
    ) -> ContextBundle: ...
```

### 4.1 预算（第一步就把工具定义算进去）

现有实现的 `budget = context_window * 0.8` 没有扣工具定义与输出预留，20% 是经验值而非保障。新算法：

```
tools_tokens  = estimate(json(tools))          # 工具定义每步全量重发
reserve       = output_reserve + PROTOCOL_OVERHEAD
available     = context_window - reserve - tools_tokens
if available < MIN_AVAILABLE:  # 前缀都放不下，装配失败并报出原因
    ...
```

### 4.2 按层装配（固定顺序）

| 步 | 层 | 做法 |
| --- | --- | --- |
| 1 | `PREFIX` | 直接放入。规则文件超出上限时按现有 `_cut_rules` 截断（32KB）。 |
| 2 | `MEMORY` | `resident_text(limit)` 非空则放入。受 2k 上限；**不整段丢弃**。 |
| 3 | `TASK` | 任务状态与工作区索引非空则放入。**不参与淘汰**（索引 ≤10 行，每步现算）。 |
| 4 | `RECENT` | 交互组**从最近往前**填，直到达到软配额或撞到「最少保留组数」。 |
| 5 | `EVIDENCE` | 先放全部 `summary`，再按剩余额度展开 `detail`。来源见 3.3：检索回来的记忆 + 含关键行的工具结果。 |
| 6 | `HISTORY` | 放入摘要；被移出的交互组在引用清单里各占一行。 |

软配额（只决定「先给谁」，超预算时按 4.3 降级）：

| 层 | 软配额 |
| --- | --- |
| `PREFIX` | 不限（受规则文件上限约束） |
| `MEMORY` | 上限 2k（核心事实） |
| `TASK` | 不限（通常 < 2k） |
| `RECENT` | 剩余预算的 50% |
| `EVIDENCE` | 剩余预算的 25% |
| `HISTORY` | 剩余预算的 25% |

> 四份额度由 `ContextAssembler.budget` 一处算出（`Budget.share`），各层从同一处取。
> 被软配额削过的层**同样记进 `degraded`**：削了就是削了，不能因为「不是降级链那一步削的」
> 就不算，否则 `degraded` 反映不出真实用量，看日志的人会以为窗口还宽裕。

### 4.3 降级顺序（超预算时按此顺序执行，直到装得下）

```
1. 收 EVIDENCE 的 detail        → 只留 summary + ref（含检索记忆的正文）
2. 缩 HISTORY 的摘要            → 缩到一半
3. 缩 MEMORY 常驻段             → 缩到一半
4. 移出最旧的 RECENT 交互组      → 整组移出，在 HISTORY 留一行引用
5. 压 RECENT 组内的大块输出      → 只压工具输出正文，不切开组
   5a. 丢中间推理（REASONING）   → 换成占位，字段留着（服务端要这个字段在）
   5b. 压工具输出（TOOL）        → 保报错原文与结论行，丢掉成片过程输出
   5c. 再压一档                  → 工具输出只留关键行，连首尾也不要
（到第 5 步仍装不下：报装配失败，带上各层用量，不静默截断）
```

**绝不丢 `PREFIX` 与 `TASK`。** 第 5 步压的是工具输出正文与中间推理，压缩后仍保留
「调用了什么、成功与否」，只丢冗长的原始输出。助手正文（决策、结论）与 `tool_calls`
（调用记录）**不在可压的两类里**，因此一条都不会被动到。

> **压缩原语在 `prompt.py`，两处共用。** 翻译层给模型的那一份（`truncate_for_model`）
> 也要按内容挑着留，否则一段超长输出会在**装配器看到它之前**先被按字符位置截掉：
> 报错正好出现在被削掉的那一段里，后面再怎么按内容挑也救不回来。装配器与翻译层
> 用同一个 `compress_tool_output`，差别只在「什么时候压、压到多狠」。

### 4.4 输出布局

```
[
  {"role": "system", "content": PREFIX},        # L0 稳定前缀（缓存友好）
  {"role": "user",   "content": TASK_HEADER},   # L1 运行环境 + 常驻记忆 + 任务状态 + 工作区索引 + 摘要 + 引用清单
  ...RECENT 交互组（L2，完整、原子）...           # 从旧到新
  {"role": "user",   "content": EVIDENCE_BLOCK} # L3 本步证据与检索记忆（可选，放最后：recency）
]
```

- `system` 只放稳定前缀，**不放时间、不放进度**，避免易变内容破坏前缀缓存。
- 运行环境、常驻记忆、任务状态、工作区索引、摘要、引用清单合成一条 `user` 头（`TASK_HEADER`）。
  平台那句不在这里：它是本机常量，放在 `PREFIX` 里。
- 证据放最后，紧邻生成，利用近因效应。

`TASK_HEADER` 渲染示例：

```
（本会话的任务状态，随进度更新）
目标：对 EGFR 做一轮虚拟筛选
用户约束（每一步都生效）：
- 受体必须用 4.2Å 的晶体结构
- 打分用 Vina，不要用 Vinardo
已完成：
- 已导入受体 receptor#1
待办：
- 生成配体库并粗筛
（以下是本次对话早前部分的摘要）
一、……
（更早的内容已省略，可用 list_workspace / read 按引用取回）
- turn 3：import_structure 导入受体
- turn 5：prepare_receptor 生成产物 artifact#2
```

---

## 五、各层淘汰策略一览

| 层 | 单位 | 先给 | 超预算时 | 保底 |
| --- | --- | --- | --- | --- |
| `PREFIX` | 整块 | 总是 | 不削（仅规则文件按 32KB 截断） | 身份与纪律必须完整 |
| `MEMORY` | 常驻段 | 总是 | 先砍旧的核心事实 | 核心事实至少留一条 |
| `TASK` | 整块 | 总是 | 不削 | 约束一条不丢 |
| `RECENT` | 交互组 | 最近往前 | 先整组移出，再压组内输出正文 | 至少保留最近 1 组 |
| `EVIDENCE` | 条 | 全给 summary | 收 detail，只留 ref + summary | ref 一定留（索引没了就等于没有这条） |
| `HISTORY` | 摘要 + 引用 | 给摘要 | 缩摘要，引用清单保留 | 引用不丢 |

---

## 六、不变式（可直接写成判据）

1. **交互组原子**：任一 `tool` 结果必须与其 `tool_call` 同组进出。装配结果里不存在孤儿。
2. **任务状态不丢**：`TASK` 层的约束条数在装配前后一致。
3. **前缀稳定**：同一会话内，`PREFIX` 的字节内容只随 `AGENTS.md` 变化，不随时间/进度变化。
4. **常驻记忆有上限**：`MEMORY` 层估算 token ≤ 软配额（默认 2k）；超限按「核心 > 旧条目」淘汰，不整段丢弃，至少留一条核心事实。
5. **预算硬约束**：装配结果的估算 token ≤ `context_window - output_reserve - tools_tokens`；装不下时报错，不静默截断。
6. **可追溯**：每一条被移出的交互组，在 `HISTORY` 的引用清单里都有一行。
7. **降级可观测**：`ContextBundle.degraded` 记录了这一轮触发了哪几级降级，**按降级链的顺序排列**
   （集合排序会把顺序抹掉，而顺序正是「先削谁、后削谁」这件事本身）。循环层在真削过时
   另发一条 `context.assembled` 事件，带上 `dropped` / `degraded` / 各层用量，写进会话日志里。
8. **证据来源可解释**：`EVIDENCE` 层里的每一条都能说清是哪一次调用留下的——引用名要么是
   「工具名 + 定位参数」，要么是记忆编号；条数不超过 `EVIDENCE_LIMIT`；同一条引用只占一条。
   成片的过程输出不会进这一层（`RECENT` 里原样还在，进来只是白占额度）。

---

## 七、与现有代码的映射

| 现状 | 位置 | 改动 |
| --- | --- | --- |
| `build_system_prompt` 混装稳定段与易变段 | `prompt.py:165` | 分成 `stable_prefix()`（身份/纪律/规则）与 `task_header()`（常驻记忆/任务状态/摘要/引用） |
| `Workspace.state_text` 带当前时间进系统提示 | `workspace.py:31` | 移出 `PREFIX`，并入 `TASK_HEADER`，避免破坏前缀缓存 |
| `memory_text` 直接拼在 `system` 之后 | `prompt.py:196` | 归入 `MEMORY` 层，位置紧随 `PREFIX`，受 2k 上限 |
| `build_model_messages` | `prompt.py:196` | 升级为 `ContextAssembler.build`，返回 `ContextBundle` |
| `_translate_message` 逐条翻译，无组概念 | `prompt.py:373` | 改为按 `STEP_START` 切交互组，返回 `list[Turn]` |
| `_shrink_kept` 按字符位置三级削 | 已删除 | 第 1 期整条旧装配路径删除；压缩原语重建在 `prompt.compress_tool_output`，按内容挑着留 |
| `truncate_for_model` 按字符位置留首尾 | `prompt.py` | 改为先按内容挑（保报错原文与结论行），挑完仍超长才按位置兜底 |
| `budget = window * 0.8`（`CONTEXT_RATIO`）未扣工具定义与输出 | 已删除 | 统一预算：`ContextAssembler.budget` 一处算，扣 `tools` 与 `output_reserve`，各层按 `Budget.share` 取软配额 |
| `Session` 无任务状态 | `models.py:208` | 增 `task_state: TaskState` 字段，随状态文件写出 |
| 装配处 | `loop.py:312`、`loop.py:417` | 改用 `ContextAssembler.build` |
| `recall` 的结果只回灌进 `RECENT` 的 tool 消息 | `memory.py` | 加 `evidence_argument`，命中同时进 `EVIDENCE` 层——跨会话的知识，`RECENT` 一压就没了 |
| 工具结果只留在 `RECENT` 里 | `loop.py:_execute_tool` | 跑完提炼含关键行的输出进收集器；`_assemble` 带上；两个入口各清空一次 |
| 工具定义每步全量重发 | `loop.py:638` → `tools.py:293` | 纳入预算；长会话可考虑按相关性发子集 |

> 行号为 2026-10-07 实测值（记忆第 1 期实施后已变动），实施时以实际为准。

**不动的部分**：`Session` 作为唯一真相、现场重建、Ledger 的 handle 模式——这三条是对的，保留。

---

## 八、迁移分期

| 期 | 内容 | 性质 | 收益 |
| --- | --- | --- | --- |
| 1 | 交互组原子 + 前缀稳定分离 | 正确性 | 消除长会话里的孤儿消息与缓存失效 |
| 2 | `TaskState` 结构化 | 能力 | 用户约束不再依赖摘要兜底 |
| 3 | 选择性压缩 + 统一预算 | 质量 | 压缩不再误删要害；预算成为硬保障 |
| 4a | 证据层接入（`recall` 回来的记忆 + 含关键行的工具结果） | 扩展 | 检索回来的知识与关键报错不再随 `RECENT` 一起被压掉 |
| 4b | 子代理上下文隔离 | 扩展 | 大检索/长流水线不污染主上下文 |

**建议从第 1 期开始**：它是唯一一条「不改就可能在长会话里出错」的正确性问题，其余是效率与能力问题。

> **`MEMORY` 层已在记忆第 1 期接入，第 1 期已把它归位**：现在由 `loop._memory_text()` 取
> `MemoryStore.resident_text()`，交给 `ContextAssembler.build(memory_text=)` 放进 `PREFIX` 之后的
> `MEMORY` 层，纳入统一预算与降级链。旧那条「直接拼在 `system` 之后」的路径已随整条旧装配路径删除。

> **`4b`（子代理上下文隔离）的设计单独成文**：见 `subagent-design.md`。
> 本文前八节讲的是「主上下文怎么装配」，那一份讲的是「有些子任务根本不该进主上下文」——
> 两件事互补，改动面也不重叠（前者在装配器与翻译层，后者在循环层的分支结构）。

实施计划与任务清单见 `context-plan.md`。

---

## 九、一句话

> 分层之后，上下文不再问「历史装不装得下」，而是问「这一步需要哪几层、每层给多少」。
> 每层的去留由**类型**决定，而不是由新旧和字符位置决定。
