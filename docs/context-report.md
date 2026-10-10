# 上下文组织方式分析报告

> 分析对象：`opencode_tui` 的上下文装配链路（设计第 1~4a 期已完成，4b 待实施）。
> 依据代码：`core/agent/context.py`(729) / `prompt.py`(435) / `loop.py`(897) / `memory.py`(576) / `core/models.py`(456)。
> 设计文档：`docs/context-design.md`、`docs/context-plan.md`、`docs/memory-design.md`、`docs/subagent-design.md`。
> 日期：2026-10-09。

---

## 一、一句话结论

上下文不是「会话历史的裁剪器」，而是**面向当前一步的装配器**：完整会话是档案（`Session.messages` 唯一真相），
发给模型的只是为这一步按**层**选出来的工作材料。每层的去留由**类型**决定（pinned / capped / atomic / soft / ref），
不由新旧和字符位置决定。装配顺序固定 `PREFIX → MEMORY → TASK → RECENT → EVIDENCE → HISTORY`，
装不下时按固定降级链逐级削，削到底仍装不下就**报错而不是静默截断**。

---

## 二、六层模型（L0~L5）

| 序 | 层 | 内容 | 来源（代码位置） | 淘汰策略 | 软配额 |
| --- | --- | --- | --- | --- | --- |
| L0 | `PREFIX` | 身份、工作纪律、shell/登记表/记忆纪律、`AGENTS.md` | `prompt.stable_prefix` | `pinned` 永不丢 | 不限（规则文件 32KB 截断） |
| L1 | `MEMORY` | 常驻记忆（核心事实，跨会话） | `MemoryStore.resident_text(2000)` | `capped` 整块保留、超限砍旧条目 | `MEMORY_LIMIT=2000` |
| L2 | `TASK` | 目标 / 用户约束 / 已完成 / 待办 / 阻塞 | `Session.task_state.render()` | `pinned` 永不丢 | 不限（通常 <2k） |
| L3 | `RECENT` | 最近若干**完整交互组** | `split_turns(session.messages)` | `atomic` 整组进出 | 剩余 50% |
| L4 | `EVIDENCE` | 检索记忆 + 含关键行的工具结果 | `EvidenceStore`（一轮） | `soft` 摘要必给、正文按预算展开 | 剩余 25% |
| L5 | `HISTORY` | 旧对话摘要 + 被移出组的引用清单 | `Session.summary` + `_refs_block` | `ref` 只留引用 | 剩余 25% |

层与顺序是**枚举本身**：`LAYER_ORDER`（`context.py:47`）的顺序即装配顺序，`LAYER_POLICY`（同文件 57）声明策略，
`RECENT_SHARE/EVIDENCE_SHARE/HISTORY_SHARE=0.5/0.25/0.25`（71~73）三份额度只在 `ContextAssembler.budget` 一处汇总。

---

## 三、数据结构（`context.py`）

### 3.1 `Turn`（交互组，RECENT 的原子单位）
```python
@dataclass
class Turn:
    index: int          # 全局序号（引用清单里的 "turn N"）
    message_index: int  # 来自会话的第几条消息，供 compact_upto 定位
    messages: list[dict]  # 已翻译成模型消息的若干项
```
- `index` 从 1 递增（`split_turns` 里的 `serial`），是 HISTORY 引用清单的稳定标识。
- `message_index` 让装配器按「消息序号 < `compact_upto`」判定手动压缩挡掉的部分。
- `tokens()` 只数真正会发出去的字段；`tool_outputs()` 单独取 `role=="tool"` 项，压缩时只动它们。

### 3.2 `Evidence`（一条按需证据，frozen）
```python
class Evidence: ref: str; summary: str; detail: str = ""
```
`ref` 是轻量索引（handle / 路径#行号 / 记忆编号），`summary` 一行摘要**永远给**，`detail` 正文预算允许才展开。
这与 Ledger 的 handle 是同一种思路：**上下文里放索引，不放整份内容**。

### 3.3 `EvidenceStore`（证据收集器，一轮生存）
- `limit=EVIDENCE_LIMIT=12` 按**条数**滚动（不按 token —— 索引是短行，条数卡住就够；正文由装配器按预算展开）。
- `add(ref, summary, detail)`：ref/summary 各自压成一行（`one_line`，上限 120/60），ref 相同则**替换并挪到末尾**。
  同一个文件读三次不会占掉三条。
- `add_from_part(part)`：只收「跑完（COMPLETED/FAILED）且输出含关键行」的工具结果。**失败例外保底收**——
  失败原因就是下一步的输入，且常是短的一句（如「缺少参数 text」），关键词表盖不住。
  收进来的是**挑出的关键行**（`is_key_line`），第一行进 summary，其余进 detail，**正文不重复第一行**。
- `clear()`：两个入口（`stream_reply:222` / `run_bash:266`）各清一次——证据是「本步相关」的。

### 3.4 `Budget`（一步的预算口径，frozen）
```python
available = int(window * ratio) - reserve - tools_tokens   # ratio=0.8
reserve   = min(output_reserve + PROTOCOL_OVERHEAD, window // 4)  # 4096+256，窗口小时封顶四分之一
```
`Budget.share(layer, remaining)` 返回该层软配额。额度只在这里算一处，避免「工具定义与输出预留被漏掉」（旧实现 20% 只是经验值）。

### 3.5 `ContextBundle`（装配结果）
`messages` / `usage`（每层 token）/ `dropped_turns`（移出组序号）/ `dropped`（组数）/
`dropped_upto`（移出内容覆盖到第几条会话消息）/ `degraded`（按降级链排序的级别名）。

---

## 四、装配算法（`ContextAssembler.build`，`context.py:486`）

### 4.1 预算闸门
`available < MIN_AVAILABLE(512)` → 直接 `ContextAssemblyError`（连稳定前缀都放不下），报错带上 `PREFIX` 用量。

### 4.2 分组与手动压缩
```
turns    = split_turns(session.messages)
compacted = [t for t in turns if t.message_index < session.compact_upto]   # 手动 /compact 挡掉的
live      = 其余
```

### 4.3 `split_turns`（`context.py:297`）—— 由构造保证的原子性
遍历每条会话消息 → `translate_message` 展开成模型项 → **若当前项是 `role=="tool"` 且与上一条 Turn 同属一条消息，就并入上一条**，
否则新起一组。于是「工具结果永远与它的调用同组」**是构造出来的，不靠事后检查**；用户消息自成一组，孤儿工具结果也单独成组（正常不出现）。

### 4.4 按层取一份（软配额）
```
fixed     = tokens(prefix) + tokens(memory) + tokens(task_text)
remaining = available - fixed
recent    = _take_recent(live, share(RECENT, remaining))     # 从最近往前填，至少 MIN_RECENT_TURNS=1
summary   = _cap_text(session.summary, share(HISTORY, remaining))
evidence  = _cap_evidence(raw, share(EVIDENCE, remaining))   # 摘要全给，正文装到限额
```
**被软配额削过的层同样记进 `degraded`**（memory / history_summary / evidence_detail）——削了就是削了，
否则 `degraded` 反映不出真实用量，看日志的人会以为窗口还宽裕。

### 4.5 降级链（`DEGRADE_ORDER`，`context.py:103`；实现 553~579）
| 步 | 级别名 | 动作 |
| --- | --- | --- |
| 1 | `evidence_detail` | 证据层收掉正文，只留 ref + summary |
| 2 | `history_summary` | 摘要缩到一半 |
| 3 | `memory` | 常驻记忆缩到一半再按上限裁 |
| 4 | `recent_turns` | 整组移出最旧的交互组（`while len(recent) > MIN_RECENT_TURNS`） |
| 5 | `recent_reasoning` | 丢中间推理：`reasoning_content` 换成 `REASONING_PLACEHOLDER`（**字段留着**） |
| 5b | `recent_output` | 压工具输出正文，保报错原文与结论行（`compress_tool_output(deep=False)`） |
| 5c | `recent_output_body` | 再压一档：只留关键行（`deep=True`） |
| — | 到底 | 仍装不下 → `ContextAssemblyError`，带各层用量 |

每一步都**重算用量、装下就停**。`ordered_degraded()` 按降级链顺序排序并去重（顺序本身=「先削谁后削谁」）。

### 4.6 输出布局（`_compose`，`context.py:612`）
```
[ {"role":"system", "content": PREFIX}            # L0，字节稳定、缓存友好
  {"role":"user",   "content": TASK_HEADER},      # L1+L2+L5：记忆+任务状态+摘要+引用清单
  ...RECENT 交互组（从旧到新）...                   # L3
  {"role":"user",   "content": EVIDENCE_BLOCK} ]  # L4 放最后，利用近因效应
```
- `system` **只放稳定前缀**，不放时间/进度——`workspace.state_text()`（含当前时间）已移出前缀，进 `TASK_HEADER`。
- `TASK_HEADER` 由 `_task_header` 拼：`memory` → `task_text` → （有 moved 时）`SUMMARY_NOTICE`+摘要 → `_refs_block`。
- `_refs_block` 每行一条「turn N：工具名、工具名 / 用户消息 / 助手回复」，**标签里不放正文**（否则等于没移出）。

### 4.7 `usage` 的算法
`PREFIX/MEMORY/TASK/RECENT/EVIDENCE` 各层单独估，`HISTORY = 头部总 token - memory - task_text`（即摘要+引用清单的份额）。

---

## 五、翻译层（`prompt.py`）—— 决定发给模型的那一份长什么样

`translate_message(message)` → 用户消息正文/引用文件拼成一条 content；助手消息按 `PartType.STEP_START` 切成若干 step，
每个 step 把 REASONING / TEXT / TOOL 合成**一条 assistant 消息**（带 `tool_calls`）+ 紧随的若干 `role=tool` 消息。

**关键不变式（DeepSeek 思考模式）：** 只要请求带 `tools`，历史里**每一条** assistant 消息都必须回传 `reasoning_content`，
没有推理输出时补 `EMPTY_REASONING` 占位而非省字段——「一步只有工具调用、没有推理」缺字段会被服务端整条退回 400。
两个分支（有工具调用 / 只有正文）都挂这个字段，这是结构不变式，不靠「这一步刚好有推理」成立。

**选择性压缩两处共用 `compress_tool_output`**（`prompt.py:134`）：
- 翻译层：`truncate_for_model`（`prompt.py:168`）——本来给模型的工具输出**先按内容挑**，挑完仍超长才按位置兜底。
  否则超长输出会在装配器看到它之前先被按字符截掉，报错正好在被削的那段里，后面按内容挑也救不回来。
- 装配器：降级链第 5 步调同一函数，差别只在「什么时候压、压到多狠」。
- 判定：**报错按「行里含」**（`ERROR_MARKERS`），**结论按「行首是」**（`RESULT_MARKERS`）——「处理第 12 个配体完成」这种过程行含「完成」，
  按含判定等于没压。两档标记刻意不同（`OMISSION_MARK` / `DEEP_MARK`），免得互相把对方当「已压过」。

`estimate_tokens`（`prompt.py:65`）按字符类别估：汉字/全角 ~1 字 1 token，其余 ~4 字符 1 token。中文长对话若统一按 2 字 1 token 会低估到三分之一。

---

## 六、循环层如何驱动（`loop.py`）

| 位置 | 作用 |
| --- | --- |
| `__init__:150` | `self.evidence = EvidenceStore()` —— **一轮**的运行时状态，不进构造参数 |
| `stream_reply:222` / `run_bash:266` | 每轮开头 `evidence.clear()` |
| `_memory_text:322` | 现取 `memory.resident_text()`（会话中途可能被 remember 改过，不缓存） |
| `_task_text:333` | `state.is_empty()` 时空串（空的就不塞，免得白占额度还让模型以为「本该有东西」） |
| `_assemble:343` | `ContextAssembler(window).build(...)`；收尾总结那条请求传 `with_tools=False`（不带工具就不占工具定义额度） |
| `_run_steps:393` | 捕获 `ContextAssemblyError` → 写 ERROR Part + `EXIT_ERROR`，中途停下 |
| `_run_steps:400` | `context.dropped` 时 `_summarize(session, dropped_upto)` 生成摘要后**重新装配一次** |
| `_execute_tool:794` | 跑完工具 → `evidence.add_from_part(part)`（放在状态定稿之后：钩子可能改成失败） |
| `_trace_context:300` | 只在真削过时发 `context.assembled` 事件（dropped / dropped_upto / degraded / usage） |

**摘要路径**（`_summarize:542`）：`summary_upto` 记上次收到哪，只补新露出的那一段；摘要是一次真实请求，用量累加在当前消息。
四段固定，第四段「用户明确给出的约束」是重点——**但结构性约束已改由 `TASK` 层兜底**（见第七节）。

---

## 七、`TASK` 层与 `MEMORY` 层（数据层 `core/models.py`）

### 7.1 `TaskState`（`models.py:224`）—— 不在 `context.py`
放在 `models.py` 与 `Session` 同处，因为 `context.py` 已从 `models.py` 导入 `Message/Session`，反向放会成环。
`context.py` 只把它当一段**渲染文本**收（`build(task_text=)`），不认这个类型。

- 字段 `goal / constraints / done / todo / blockers / version`，`version` 只在**真变了**时 +1。
- **两条语义刻意不同**：`constraints` 追加重写会丢（模型漏抄一条等于删了，这正是要防的）、
  `done/todo/blockers` **整段替换**（进度记录随进度重写才对）；`None`=不动 ≠ 空列表=清空。
- 渲染 `render()` 空字段整段不出现；进 `TASK` 层**不参与淘汰**（pinned），聊多长用户的要求都还在。
- 入口：模型走 `core/agent/task.py::TaskTool`，用户走 `/task` 命令；写盘 `[sessions.task_state]` 嵌套表，与摘要**同一条原子换名写**。

### 7.2 `MemoryStore.resident_text(limit=2000)`（`memory.py:344`）—— `MEMORY` 层来源
- 排序 `(not entry.refs, -updated_at)`：**有引用的排前面**（引用指向项目真实产物，比无凭据陈述更值得占额度），同优先级新的在前。
- 超限时 `continue` 跳过装不下的条目（**不整段丢弃**），额度由 store 负责，装配器只做 `_cap_memory` 兜底。
- 记忆是**来源**，上下文是**产物**：常驻记忆进 `MEMORY` 层；`recall` 检索回来的进 `EVIDENCE` 层
  （`memory.py:542` 声明 `evidence_argument="evidence"`，`_collect` 把命中的记忆写进收集器——跨会话知识，RECENT 里本就没有，不给一份到证据层，RECENT 一压就没了）。

---

## 八、不变式清单（可直接作判据）

1. **交互组原子**：任一 `tool` 结果必与其 `tool_call` 同组进出，装配结果无孤儿（由 `split_turns` 构造保证）。
2. **任务状态不丢**：`TASK` 层约束条数装配前后一致。
3. **前缀稳定**：同一会话内 `PREFIX` 只随 `AGENTS.md` 变，不随时间/进度变（缓存友好）。
4. **常驻记忆有上限**：`MEMORY ≤ 2000`，超限按「核心 > 旧条目」淘汰、不整段丢弃。
5. **预算硬约束**：结果 ≤ `window - output_reserve - tools_tokens`；装不下报错，不静默截断。
6. **可追溯**：每条被移出的交互组在 HISTORY 引用清单里都有一行。
7. **降级可观测**：`degraded` 按降级链顺序排列；真削过时发 `context.assembled` 事件。
8. **证据可解释**：每条证据能说清是哪次调用留下的，条数 ≤ `EVIDENCE_LIMIT`，同 ref 只占一条，成片过程输出不进这层。

---

## 九、观察与遗留

- **设计→实现之间的出入均已回填文档**：`TaskState` 位置从 `context.py` 改为 `models.py`；
  `MEMORY` 非 dataclass（纯文本，来源是 store）；`dropped` 语义从「消息条数」改为「交互组数」，另加 `dropped_upto`。
- **未做**：`degraded` 未显示到状态栏（`docs` 注明：会让成对抓帧的七帧全失配）——
  目前只通过 `context.assembled` 事件与日志可观测。
- **未实施**：`4b` 子代理上下文隔离（`docs/subagent-design.md`），对应 `Tool` 的第四个注入通道 `subagent_argument`。
- **扩展点**：新增证据来源时，应只接「RECENT 容易被压掉/移出且体积可控」的东西；
  Ledger 产物详情**刻意没接**（`list_workspace` 已能列，且产物体积不可控，收进来会吃光证据层额度）。
- **两处消费同一压缩原语**（`prompt.compress_tool_output`）：改压缩逻辑时翻译层与装配器必须同步，
  否则会出现「翻译层先按位置截掉报错、装配器再挑也救不回」。
