# 上下文重构 v2：Memory / Event / Evidence 统一计划

> 状态：**开始执行**（2026-10-25）。
> **本次范围**：按 Phase 0 → 1 → 2 → 3 顺序实施，每阶段结束跑全量
> `test_agent` / `test_smoke`；Phase 4 只在 metrics 有数据后再决定。
> 合并来源：`docs/context-rework-proposal.md`（上下文总体调研）+ 事件层方案 + memory/evidence 评审。
> 本文是执行的唯一依据；执行中的偏差先回写本文件再动手。
>
> 两个已确认的行为选择（沿用事件层方案）：
> 1. 事件在**下一阶段开始时**收口；当前正在写的 assistant 消息与紧邻的用户消息不进事件。
> 2. 溢出且没有手动事件时，用**确定性本地事件**自动兜底（不花模型请求）。
>
> **进度**（2026-10-25）：
>
> - Phase 0 完成——`ContextBundle.metrics`、`AgentLoop.context_history` /
>   `last_input_tokens`、`context.metrics` 事件、`/context` 命令、
>   `check_context_metrics` 与 `check_context_budget`、`tools/eval_context.py`。
> - Phase 1 完成——水填预算（`HISTORY_MAX` / `EVIDENCE_MAX` 取代 50/25/25）、
>   `Turn.tool_parts` 源映射、`Part.tool_meta` 进证据层、`evidence_ref_of`
>   （`primary_argument` + `evidence_key`）、证据去重/排序/RECENT 互斥、
>   `recent_tool_clear` + `read_turn`、引用清单与任务状态封顶。
> - Phase 2 完成——`Event` / `Session.events`、`Layer.EVENT`、`record_event` /
>   `recall_event`、溢出时本地事件兜底、`_summarize` 在事件存在时冻结、
>   锚定增量摘要（八段，只喂新段 + 旧摘要）、摘要熔断与装配失败后强压一次。
>   详见 [`event-log-design.md`](event-log-design.md)。
> - Phase 3 完成——记忆的 `superseded_by` / `valid_to` / `expires_at` 版本关系、
>   写入去重与同主题失效、引用校验与 `unverified`、三段式常驻注入（1200 token）、
>   打分检索、500 条上限与归档。详见 [`memory-design.md`](memory-design.md) §13。
> - 新增检查组：`context-budget` / `evidence-ref` / `evidence-dedupe` / `tool-clear` /
>   `event-log` / `event-summary` / `memory-supersede` / `memory-resident`。
> - **Phase 4 暂缓**：70%/80% 的主动压缩阈值没有做——装配器本身会在超预算时
>   按降级链处理，主动阈值只是把触发点提前几步，收益要用真实会话的
>   `cache_hit` / `dropped` 数据判断。`/context` 已经能把这两项量出来，
>   等有一批真实数据再定。
> - Phase 5 部分完成：`tools/eval_context.py`、`tools/eval_memory.py` 两份手动探针；
>   记忆 golden set 的标注数据仍需人工积累（不进 CI）。
>
> **实测修正**（2026-10-25，8 轮真实会话）：
> `recent_tool_clear` 原先在降级链里，但 `_take_recent` 是「整组装不下就不要」——
> 等到那一步时组早已不在 RECENT 里，所以它一次都没触发（实测 `dropped` 一路
> 6、12 整组跳，`degraded` 里从没出现过它）。现在把清理前移到 `_take_recent`：
> 装不下先清这一组自己的旧正文，再清已装下的旧组，清不动才丢组。
> 同一份会话复测：全程 `dropped=0`，`recent_tool_clear` 正常出现，输入从 528K 降到 471K。
>
> **实测修正 2**（同日）：`_ask_permission` 在没有人订阅 `permission.ask` 时会永远等一个
> 没人回答的 future（实测脚本卡满 420 秒）。现在两处兜底：无订阅者立刻拒绝、300 秒无人
> 答复也拒绝，原因都会回灌给模型。复测同一份会话不再卡死。
>
> **清理成批 vs 逐条**（离线复算，同一份 24 条消息的会话、同一窗口）：
>
> | 策略 | 相邻请求的前缀命中 | 丢组步数 |
> | --- | --- | --- |
> | 不清，装不下就丢组 | 68.1% | 5 |
> | 逐条清 | 74.2% | 0 |
> | 成批清 | **82.3%** | 0 |
>
> 成批清赢在「已清的边界」不再每步往后挪。真实会话两两对比是 87.1% / 80.8%（成批）
> 对 86.3% / 79.5%（逐条）——噪声盖过差异，所以按复算结果定案：**成批**。
>
> **工具定义瘦身的结果**（2026-10-25）：把最长的五个（write_run / prepare_receptor /
> search_actives / find_analogs / 以及 write_run 的第二轮）重写了一遍，
> 工具定义 10,629 → 9,562 token（−10%）。**没到 6K，也不该硬压**：剩下的每条描述
> 都牵着一条会改变行为的规则（「编号必须来自 fetch_target」「62 条链取不回来」
> 「候选不是结论」「默认只预演，confirm=true 才写盘」），再删就是在用工具用错
> 换几百 token。要真正到 6K 只能改成**按需加载**（核心工具常驻 + load_tools），
> 那是装配层的事，不是文案的事。
>
> **窗口大小对前缀命中的影响**（同一份 24 条消息、同一实现，离线复算）：
>
> | 窗口 | 前缀命中 | 清过正文的步 | 丢组的步 |
> | --- | --- | --- | --- |
> | 20K | 54.1% | 17 | 0 |
> | 30K | 69.9% | 11 | 0 |
> | 40K | 82.3% | 5 | 0 |
> | 60K | 91.7% | 0 | 0 |
> | 100K / 200K | 91.7% | 0 | 0 |
>
> 60K 之后不再压缩，命中率停在「只追加」的天花板。真实会话在 100K 窗口下
> 8 轮跑到 **96.3%**、零压缩；40K 窗口下同一套 prompt 是 87.1% / 80.8%。
> 结论：命中率由「前缀多久变一次」决定，而压缩是让它变的唯一原因——
> 窗口够大时什么都不用做，这是最省的一条路。
>
> **按需加载落地**（2026-10-25，Phase 4 的工具那一半）：
>
> 文案压到 9,562 之后仍占每步 9.6K，因为 24 个领域工具**每一步都要全量重发**。
> 改成两层：核心工具（文件、登记表、任务、事件、记忆、`load_tools`，14 个）常驻，
> 24 个领域工具分成 13 族，由 `load_tools` 现装现用。
>
> | | 工具数 | 每步定义 token |
> | --- | --- | --- |
> | 全量 | 38 | 12,313 |
> | 只发核心 | 14 | 3,077 |
> | 核心 + 4 族（structure/receptor/ligand/docking） | 20 | 5,917 |
>
> 同一份 24 条消息的样本、同一实现复算（`tools/eval_context.py --sweep`）：
>
> | 窗口 | 全量工具 | 只发核心 |
> | --- | --- | --- |
> | 20K | 装不下（24/24 步） | 77.9%（3/24 步装不下） |
> | 40K | 77.0% | **78.5%** |
> | 60K | 83.7% | **91.2%** |
> | 100K / 200K | 92.7% | 92.6% |
>
> 40K 那一档只差 1.5 个点，因为那个窗口下本来就在清正文、前缀每步都在动；
> 60K 这一档差 7.5 个点，因为省下的 9.2K 正好让「不用压缩」的边界前移。
> 20K 是全量工具装不下、核心工具能跑——固定开销本身就是能不能跑的门槛。
>
> 两个实现选择：
>
> 1. **排除而不是允许清单**。族记在 `Session.tool_families` 上，`hidden_names()`
>    只藏「属于某个还没装入的族」的工具；没登记族的工具默认常驻。用允许清单的话，
>    新加一个工具忘了登记，现象是「写了却没装、模型看不见」，而各处判据都是绿的。
> 2. **族记在会话上，不记在注册表上**。注册表按根 / 子 agent 共享两份，记在那里
>    会让一个会话装过的工具漏到另一个会话。子 agent 从父会话**继承**已装族
>    （`host.spawn`），否则它得自己再 load 一遍，而它看到的任务说明里没有这一步。
>
> 代价是**装族那一步会击穿一次缓存**（请求体的 `tools` 数组变了）。一族人只装一次，
> 一轮任务通常装 1–2 族，相对每步省下的 9.2K 可以忽略。前缀里加了一句
> `TOOL_DISCIPLINE` 说明这件事，因为模型看不到清单里没有的工具时，
> 最容易做的是凭记忆写参数或绕道 bash。
>
> **真实会话验证**（2026-10-25，deepseek-flash / 1M 窗口，同一组三问跑两遍）：
> 「查 EGFR 有哪些结构 → 取 4HJO 登记成受体 → 写成运行目录」。
>
> 两次都**自己** `load_tools`，没有一次需要提示：按 structure → uniprot → rsearch →
> export（→ listing）装了 4–5 族。每步工具定义 3.1K（核心）→ 4.6K（三族）→ 5.5K
> （四族）→ 6.0K（五族），全量是 12.3K。
>
> 装族那一下的击穿也量到了：第 5 步命中率从 98.9% 掉到 **36.6%**（17.8K 输入里只有
> 6.5K 命中），下一步回到 95.9%。一次装族多付约 11K，每步省 6–9K——一轮两步就回本。
>
> 两次同一组问题，输入 442K 与 135K，差 3.3 倍。差别不在装配层，在**工具**：
> 第一次 `search_pdb(query="P00533")` 失败了一次（同一条命令事后手动重跑是成功的，
> 391 条命中，所以那是瞬时失败），模型随即不信这个工具，转去 bash + urllib 自己拉
> RCSB，把 24 步上限用满；第二次它叫的是 `search_pdb({})` 空参数、当场被挡下，
> 反而没绕。**根因在 `rsearch.search_entries`**：那里的 `except Exception` 把
> `RetryableToolError` 也归成了「这次没搜成」，循环层的重试因此被绕过——一次网络
> 抖动变成了「这个工具坏了」。工具说明再准，也救不回一个被判死的工具。

---

## 〇、一句话

把上下文从「按层裁剪会话历史」升级为「**五类状态各归其位、每次压缩可逆、每一步可度量**」：

```
PREFIX → MEMORY → TASK → EVENT → RECENT → EVIDENCE → HISTORY
```

三者分工：

| 层 | 回答的问题 | 生命周期 | 主要来源 |
| --- | --- | --- | --- |
| MEMORY | 这个项目已知什么 | 跨会话 | `remember` / `/remember` |
| EVENT | 上次做了什么、结果如何 | 跨轮/跨阶段 | `record_event` 自动总结 |
| EVIDENCE | 这一步要看的材料与引用 | 单轮 | 工具结果 + `recall` |

---

## 一、目标布局

```
system : PREFIX（身份/纪律/AGENTS.md，字节稳定）
user   : HEADER（MEMORY + TASK + SUMMARY + refs）
user   : EVENTS（聚合块，仅当有事件；每个事件一行）
...    : RECENT（只含最近事件之后；旧工具结果可换成一行占位）
user   : FOCUS + ENV + EVIDENCE（目标复述、时间/目录、本步证据与引用）
```

暂不把 HEADER 移到尾部：事件块独立成消息，现有 `messages[1]`、`len==8`、roles 断言在无事件会话里
不变。Phase 4 再按 `context.metrics` 的缓存命中率决定是否搬——搬之前不动。

---

## 二、七条不变量

1. **覆盖连续**：`summary` 覆盖 `[0, S)`，事件连续覆盖 `[S, E)`，RECENT 从 `E` 开始；
   首条事件 `start = summary_upto`，有事件时 `_summarize` 停用、`summary_upto` 冻结。
2. **事件边界**：`end` 排除当前 assistant 消息与紧邻的用户消息；手动事件从最后一条
   `manual` 事件的 `end` 起、吞掉其后的 `auto/local`；自动事件与相邻 `auto/local` 合并。
3. **记忆唯一有效值**：同一主题只有一条 `live`；旧值标 `valid_to/superseded_by`，不硬删；
   冲突由代码按「`valid_from` 新 > 用户写入 > agent 写入 > 有 refs」裁决，不让模型比时间戳。
4. **证据有源**：每条 evidence 必须能说清 `source`（`file:line` / `doc#chunk` / `artifact#` /
   工具调用 id）；失败必留；正文只在来源已不在 RECENT 时展开；同文不重复。
5. **预算水填**：固定上限之后，剩余额度全部给 RECENT；装不下按固定顺序削，削到底报错，
   不静默截断。
6. **压缩可逆**：移出的交互组用 `read_turn` 取回，事件覆盖的用 `recall_event` 取回，
   记忆用 `recall` 取回。三者都不许出现「说能取回但没工具」。
7. **无事件/无记忆时行为不变**：没有事件、没有新记忆字段的会话，消息条数、roles、摘要路径
   与今天一致；回归以此为准。

---

## 三、分阶段

### Phase 0 · 度量与测试基座（先做，后面每一步都靠它）

| 改什么 | 位置 |
| --- | --- |
| `context.metrics` 事件：window / available / tools_tokens / 各层估算 / 本次实际 input / cached / cache_hit / dropped / degraded / events / memory 条数 / evidence 条数 | `loop.py`（`_consume_stream` 记 `last_input_tokens`）、`context.py`（bundle 增加字段） |
| `/context` 命令：最近 10 步表格（不显示到状态栏，避免界面基线失配） | `command_registry.py`、`app.py` |
| 确定性判据：`check_context_metrics`、`check_context_budget` | `tests/test_agent.py` |
| 手动评测脚本骨架：`tools/eval_context.py`（探针四类：recall / artifact / continuation / decision） | 新建 |
| 文档落地：把本文件写入 `docs/context-rework-plan.md`；事件细节写 `docs/event-log-design.md`；`docs/context-rework-proposal.md` 顶部加指针 | `docs/` |

**判据**：20 步假会话能拿到每步 metrics；`cache_hit = cached / input`；现有测试全绿。
**为什么先做**：预算、事件、记忆排序的效果，没有 before/after 数字就无法判断。

---

### Phase 1 · 预算、证据与可逆基础

这一期解决四处浪费：固定 50/25/25、旧工具结果不清、证据重复、引用不可回查。

**1.1 预算水填**（`context.py`）

```
available  = int(window * ratio) - reserve - tools_tokens
fixed      = PREFIX + MEMORY + TASK + EVENT
remaining  = available - fixed
history_cap  = min(tokens(summary),  HISTORY_MAX)     # 3000
evidence_cap = min(tokens(evidence), EVIDENCE_MAX)    # 6000
recent_cap   = max(0, remaining - history_cap - evidence_cap)
```

删除 `RECENT_SHARE / EVIDENCE_SHARE / HISTORY_SHARE`，换成上限常量。
`MEMORY_LIMIT` 暂时保持 2000，Phase 3 再降到 1200。

**1.2 交互组源映射**（`context.py` / `models.py`）

`Turn` 增加「模型项 → 源 Part」映射（`part_id`、`tool_name`、`tool_status`）。
这是后续三件事的共同前提：

- 证据层判断「这条工具结果是否还完整地在 RECENT 里」；
- 工具结果清理知道哪些是失败（失败不清理）；
- 事件 `build_local_event` 从 Part 提取 actions/artifacts/issues，而不是解析文本。

**1.3 结构化 meta 进证据层**（`models.py` / `loop.py` / `context.py`）

- `Part` 增加 `tool_meta: dict`；`_execute_tool` 把 `result.meta` 存进去（`to_dict/from_dict` 加 dict 守卫，限 4KB）。
- `EvidenceStore.add_from_part` 优先用 meta：`handle` → `ref`，`counts` → `summary`，
  `receptor/ligands` → 复合键；没有 meta 才回落 `is_key_line`。
- `docking.py:606` 的 `receptor/ligands/counts` 这类数据因此第一次真正进入上下文。

**1.4 证据 ref 修正**（`context.py` / `tools.py`）

`evidence_ref` 不再只看 6 个固定参数名，改为：

```
tool.primary_argument（每个工具已声明）→ 第一个非空值
tool.evidence_key（可选的第二/第三个参数）→ 复合键
```

例如 `grep pattern@path`、`dock receptor#1+ligandset#2`、`find_analogs <smiles 前 40 字>`。
这修掉「省略 path 的所有 grep 共用 `grep` 一条、互相覆盖」这类系统性撞车。

**1.5 证据排序、去重、与 RECENT 互斥**（`context.py`）

- `_cap_evidence` 按「失败 > 最新 > 其余」分配正文，而不是插入顺序。
- 文本归一化后去重；同 ref 仍替换。
- `source_part` 还在 `visible_parts` 里时，只留索引行、不展开正文——证据层只放
  「RECENT 已经看不见的东西」。
- 单条正文上限 600 token。

**1.6 旧工具结果清理 + `read_turn`**（`context.py` / `tools.py` / `loop.py`）

降级链插入 `recent_tool_clear`（排在 `recent_turns` 之前）：

- 保留最近 4 条工具结果；**失败的结果不清理**；
- 超过 400 token 的旧结果，正文换成
  `（输出已归档：turn 12 的 dock；原文用 read_turn turn=12 取回）`；
- 新增内核工具 `read_turn(turn, part?, offset?, limit?)`，用 `session_argument`，
  共享 `render_range(session, start, end)`；
- 顺手把 `_refs_block` 的「可用 list_workspace / read 取回」改成真话：
  非事件移出的内容没有专门召回工具。

**降级顺序**（`DEGRADE_ORDER`，实现时以这一条为准）：

```
evidence_detail → history_summary → recent_tool_clear → event_layer →
memory → recent_turns → recent_reasoning → recent_output → recent_output_body
```

先削「重复/可逆」的（证据正文、旧工具结果、旧事件），再削「记忆」，最后才整组移出。

**1.7 封顶**（`context.py` / `models.py`）

- refs 只列最近 20 组 + 「另有 N 组」；整块 600 token。
- `TaskState.render` 每个字段上限：constraints 20、done 20、todo/blockers 各 10。
- summary 3000 token。

**判据**：
- 60k RECENT + 空 summary/evidence 的会话，RECENT 保留 ≈60k，不触发摘要（今天 40.9k 就截）。
- 装配文本里同一条工具错误原文只出现一次。
- 被清理的每条都能 `read_turn` 取回。
- 20 组工具输出的会话，清理条数 = 总数 − 4 − 失败数。
- 无事件会话的消息条数/roles 与今天一致。

---

### Phase 2 · 事件层（episodic）

**2.1 数据**（`models.py`）

```python
@dataclass
class Event:
    id: str                 # evt_0001
    title: str              # ≤40 字
    summary: str            # ≤600 字
    actions: list[str]      # 工具名，代码提取
    artifacts: list[str]    # FILE / PATCH，代码提取
    issues: list[str]       # FAILED / ERROR 首行
    start: int              # 消息下标，闭
    end: int                # 开
    kind: str               # manual / auto / local
    degraded: bool          # 摘要来自本地汇编
    created_at: float
```

`Session` 增加 `events`、`event_seq`、`add_event`、`event_end`、`sanitize_events`。
`sanitize_events` 丢弃越界/非连续/首条 `start != summary_upto` 的事件；`compact_upto` 夹到最后一条 `end`。
`to_dict/from_dict` 加字段，旧会话 `events=[]`。

**2.2 装配**（`context.py`）

- `Layer.EVENT` 插在 `TASK` 与 `RECENT` 之间，策略 `capped`。
- `recent_floor = events[-1].end`；有事件时 `compacted=[]`，`dropped` 只统计本次新移出的 live turn。
- 事件块单独一条聚合 user 消息，插在 HEADER 之后、RECENT 之前；每个事件一行：
  `evt_0001 准备受体（第 3–7 条）：去水加氢，盒子 22Å → receptor#1`。
- `EVENT_LIMIT = min(1800, max(200, available // 6))`；从新到旧保留，削掉时 `degraded += ["event_layer"]`。
- `_task_header` 的摘要条件改成 `moved or (events and summary)`。
- `usage[Layer.EVENT]` 单列；`HISTORY` 残差不变（事件块不在 header 里）。

**2.3 工具**（新建 `core/agent/event_log.py`）

- `EventRuntime{session, current_index, summarize, is_cancelled}`，通过 `Tool.event_argument` 注入。
- `record_event(title?, summary?)`：
  - `end = current_index`；`while messages[end-1].role == "user": end -= 1`；当前消息永不覆盖。
  - `start = 最后一条 manual 的 end，否则 summary_upto`；`end <= start` → no-op。
  - 删除 `start >= 锚点` 的 `auto/local` 事件（手动吞自动）。
  - 两者都不给 → 调摘要器；`ProviderError/空文本/取消` → 确定性本地事件（`degraded=True`），
    `failed=False`；取消不写。
  - 输出只回 `evt_id + 标题 + 区间 + 产物/未决条数`，不回灌整段摘要。
- `recall_event(event?, offset?, limit?)`：无参列清单；有 id 返回详情 + 原文；未知 id 列可用编号。
- 自动事件：溢出时 `record_local_event(session, min(dropped_upto, 边界), kind="auto")`，
  零模型请求；相邻 `auto/local` 合并；只重装一次。
- `/compact`：有事件且 `target > events[-1].end` 时先补 `kind="local"` 事件。
- `/events`：无参列事件（id / 标题 / 区间 / 一行摘要），有参看详情；`ARGUMENT_COMMANDS` 加 `events`，
  `subagent.ALLOWED_COMMANDS` 也加。Web 端暂不展示事件。

**2.4 摘要切换**

- 有事件时 `_summarize` 顶部 `return False`，`summary_upto` 冻结。
- 无事件时用**锚定增量摘要**：保留现有四段，新增「决定与被否方案 / 错误与修复 / 当前在做 / 关键数字」；
  只喂 `[summary_upto, upto)` 新段，与已有摘要合并；不再 `_cut_middle`；上限 3000。
- 首条事件 `start = summary_upto`，所以摘要与事件不重叠。

**2.5 主动压缩与熔断**（`loop.py`）

- 每步开始读 `last_input_tokens / window`：≥70% 先跑 `recent_tool_clear`；≥80% 在第一步前生成摘要/事件。
- 摘要连续失败 3 次熔断；失败用本地确定性摘要兜底。
- `ContextAssemblyError` 时先强制 `compact_session(keep=2)` + 摘要 + 重装一次，仍失败才报错。

**判据**：
- 事件覆盖 `[0,6)` 时原文不在 `built.messages`，事件标题在，`usage[EVENT] > 0`，`dropped==0`，N 个事件只多 1 条消息。
- `summary + events + moved=0` 时摘要仍在。
- `record_event` 显式 title/summary 不调模型；取消不写；`ProviderError` 落本地事件且 `failed=False`。
- 小窗口 + 已有事件溢出：请求数不增加，`_summarize` 未被调用。
- 原有 `summary-compaction` 仍绿（无事件会话走旧路径；四段保留）。

---

### Phase 3 · 记忆治理（semantic）

**3.1 数据结构**（`models.py`）

`MemoryEntry` 增加：

```
supersedes: list[str]      # 新条目取代了哪些旧条目
superseded_by: str         # 被哪条取代；非空即不再 live
valid_from / valid_to: float
last_used_at: float = 0.0  # recall 命中时更新，用于 core 判定与衰减
confidence: float = 1.0
session_id: str = ""       # 来源会话
turn: int = 0              # 来源消息下标
```

`is_live` = 未过期 + `state != "missing"` + `superseded_by == ""`。

**3.2 写入治理**（`memory.py`）

- **精确去重**：归一化文本 hash 相同 → 只更新 `updated_at`，不新建 id。
- **同主题更新**：同 refs 且文本相似度高（或新文本含「改为/现在/不再/替换」）→ 旧条
  `valid_to=now, superseded_by=新id`，新条 `supersedes=[旧id]`；不硬删。
- **显式 supersede**：`remember(..., supersedes=[id])`；模型可用 `recall` 先找旧 id。
- **冲突裁决**（代码，不是模型）：`valid_from` 新者胜；同时间用户写入 > agent 写入；
  有 refs > 无 refs；仍相同取 `updated_at`。
- **写入校验**：refs 必须存在（agent 写入）；不存在则拒绝并提示用真实路径；
  用户 `/remember` 可写但标 `unverified`，不优先注入。
- **长度**：单条 ≤500 字；超了拒绝并提示拆成多条（避免大条目在 `resident_text` 里被静默跳过）。

**3.3 相关性常驻**（`memory.py` / `loop.py`）

`resident_text(query="")` 改成三段，受 1200 token 限制：

1. `core`：有 refs 且近 30 天使用过，最多 5 条；
2. `relevant`：用「最后一条用户消息 + `TaskState.goal`」做 `recall(query)`，取 top 5；
3. `recent`：最近更新 3 条。

按 id 去重，填满为止。`loop._memory_text(query=...)` 从会话里取最后一条用户正文。

**3.4 `recall` 升级**（`memory.py`）

- 字段加权：正文 2 分、refs 1 分；时间衰减 `exp(-age/90d)`；排序后取 top 20。
- 中文用 bigram、英文按词切；不做全库向量。
- 可选：`VSA_MEMORY_EMBED=1` 时用现有 `fastembed` 对 top 50 重排；默认关。
- `revalidate` 不只启动时：`/memory`、`recall` 前各做一次（长会话里 stale 不会一直挂着）。

**3.5 生命周期**（`memory.py` / `app.py`）

- `RememberTool` 暴露可选 `ttl_days`；无 refs 的低置信事实建议 90 天，但不强制。
- 磁盘上限 500 条；超出归档到 `.vsmemory/archive.json`（不注入，`/memory --all` 可查）。
- `/memory` 显示 `source / refs / state / updated_at / 是否已失效`；`/forget` 仍按 id 或关键词。
- `memory.md` 明确只读（程序每次 `save` 会覆盖），或加 `/memory import`；不做「看似可编辑实际无效」的界面。

**判据**：
- 同文写两次只剩 1 条；同 refs 新值让旧条不 live；两条矛盾事实只注入赢家。
- 不存在的 refs 被拒（agent）或标 unverified（用户）。
- 20 条记忆时常驻 ≤1200 token，相关的 5 条在；`recall` 能取到其余。
- golden set 上 stale recall ≤3%（Phase 5 建）。

---

### Phase 4 · 注意力与成本

| 项 | 做法 | 判据 |
| --- | --- | --- |
| ENV 块 | 时间/工作目录/平台放进 HEADER（不放 PREFIX） | `PREFIX` 连续两步字节相等；模型能答出当天日期 |
| FOCUS 复述 | 最后一条 user 前置「目标/约束/下一步」，取 `TaskState`，≤300 token | 最后一条 user 含 goal；长 RECENT 下不漂 |
| 产物索引 | `SessionLedger.render_digest()` 当前受体 + 最近 5 个产物，≤10 行 | 不调 `list_workspace` 也知道当前受体 |
| 工具瘦身 | 先压 schema 文案（11.1k → ~6k）；再评估「核心工具 + load_tools」按需加载 | `/context` 工具 token ≤6k；脚本任务每族只加载一次 |
| 子代理 | `spawn_agent` 增加 `mode=fork/fresh`；`fresh` 只给任务书 + handle，回报 ≤1500 token | 父会话只多 1 条消息 |
| HEADER 后移（可选） | 只有 metrics 证明缓存失配严重才做 | 搬前/搬后 cache_hit 对比 |

---

### Phase 5 · 评测与文档

**记忆 golden set**（`tools/eval_memory.py`，手动，不进 CI）：

- 30–50 条项目事实，刻意混入「事实更新」「互相矛盾」「引用文件被删」「过期」四类；
- 指标：`recall@5`、`stale recall`（≤3%）、重复率、每回答 token、LLM judge 正确率（≥85%）。

**证据探针**（`tools/eval_context.py`）：

- 固定 20 次工具运行，检查「关键结果/报错是否在证据里」「是否与 RECENT 重复」
  「能否回答用了哪个 artifact」「引用能否回到 file:line / doc#chunk」。

**上下文探针**（同脚本）：压缩后问 recall / artifact / continuation / decision 四类问题。

**文档**：

- `docs/context-rework-plan.md`（本文件落地）
- `docs/event-log-design.md`（事件层细节）
- `docs/memory-design.md`（supersede/TTL/相关性常驻）
- `CODEBUDDY.md`（架构段补 EVENT 层与 `record_event/recall_event/read_turn`）

---

## 四、文件改动总表

| 文件 | 涉及阶段 | 改动 |
| --- | --- | --- |
| `core/models.py` | 1,2,3 | `Part.tool_meta`；`Event`；`Session.events/event_seq`；`MemoryEntry` 新字段；`TaskState.render` 上限 |
| `core/agent/context.py` | 1,2 | 水填预算；`Turn` 源映射；`Layer.EVENT`；`recent_floor`；事件块；证据排序/去重/可见性；`recent_tool_clear`；refs/summary 封顶 |
| `core/agent/prompt.py` | 1,2 | `EVENT_INSTRUCTION`；`EVENT_DISCIPLINE`；`_transcript(start=)`；`event_messages`；锚定摘要指令；`_refs_block` 文案 |
| `core/agent/event_log.py`（新） | 2 | `EventRuntime` / `RecordEventTool` / `RecallEventTool` / 本地事件 / 渲染 |
| `core/agent/tools.py` | 1,2 | `Part.tool_meta` 落盘；`Tool.event_argument`；`event_runtime` 注入；`evidence_key` |
| `core/agent/loop.py` | 0,1,2,3 | `last_input_tokens`；`context.metrics`；`_execute_tool` 存 meta/构造 runtime；自动归档；`_summarize` 事件时停用；`compact_session` 补本地事件；`_memory_text(query)` |
| `core/agent/memory.py` | 3 | 去重/supersede/冲突裁决；写入校验；相关性常驻；recall 打分；TTL/容量 |
| `app.py` | 0,2,3 | 注册事件工具；`/context` `/events`；`/memory` 增强；`undo` 后 `sanitize_events` |
| `core/command_registry.py` | 0,2 | `/context` `/events` |
| `screens/subagent.py` | 2 | `ALLOWED_COMMANDS` 加 `events` |
| `tests/test_agent.py` | 0–4 | 新组：metrics / budget / evidence-ref / tool-clear / event-log / event-summary / memory-supersede / memory-resident；更新 `check_selective_compression` 的份额断言 |
| `tools/eval_context.py`（新） | 0,5 | 上下文/证据探针 |
| `tools/eval_memory.py`（新） | 5 | 记忆 golden set |
| `docs/*` | 5 | 计划、事件、记忆、CODEBUDDY 同步 |

---

## 五、判据与回归

**确定性（进 `tests/test_agent.py` 的 `GROUPS`，CI 会跑）**

1. `check_context_metrics`：每步 metrics 字段齐全；`cache_hit = cached/input`。
2. `check_context_budget`：60k RECENT 保留；水填后 `sum(usage) ≤ available`；装不下仍报错。
3. `check_evidence_ref`：grep/dock/find_analogs 的 ref 不撞车；结构化 meta 优先。
4. `check_evidence_dedupe`：同文只出现一次；RECENT 可见时不展开 detail；失败优先。
5. `check_tool_clear`：清理条数、占位、`read_turn` 取回。
6. `check_event_log`：持久化、连续覆盖、no-op、手动吞自动、`recall_event`。
7. `check_event_summary`（SERVER）：摘要请求无 tools、只含新段、失败落本地事件、溢出零额外请求。
8. `check_memory_supersede`：同文去重、旧值失效、冲突裁决、refs 校验。
9. `check_memory_resident`：core/relevant/recent 三段、1200 上限、`recall` 覆盖其余。

**回归（必须全绿）**

```
python tests/test_agent.py --only prompt
python tests/test_agent.py --only task-state
python tests/test_agent.py --only selective-compression
python tests/test_agent.py --only evidence
python tests/test_agent.py --only summary-compaction
python tests/test_agent.py
python tests/test_smoke.py
python tools/check_web_api.py
```

**已知需要同步改的断言**

- `check_selective_compression` 里 `budget.share(EVIDENCE/HISTORY)` 的断言 → 改成上限常量。
- 预算水填后长会话的 `dropped` 可能变小；`len == 2 + (40 - dropped)` 仍自洽，但要复核。
- `check_summary_compaction` 的四段保留（新摘要指令前四段不变），`fed[1]` 仍以 `SUMMARY_NOTICE` 开头。
- `test_smoke` 命令数量是动态比较，新增 `/context` `/events` 不会红；`subagent.ALLOWED_COMMANDS` 要补 `events`。

---

## 六、风险与回滚

| 风险 | 触发点 | 缓解 |
| --- | --- | --- |
| 摘要与事件重叠 | 事件存在但 `_summarize` 仍跑 | 有事件时 `return False`；首条 start = summary_upto |
| 覆盖正在写的消息 | 自动归档用 `dropped_upto` | `end = min(dropped_upto, 边界)`；边界排除当前消息与用户消息 |
| 预算水填饿死证据/摘要 | RECENT 拿走全部剩余 | 先给 summary/evidence 实际额度（带上限），剩余才给 RECENT |
| 证据 ref 改动打破旧断言 | `evidence_ref` 换算法 | 先加 `evidence_key` 与 primary 兜底，保留旧参数名兼容 |
| `tool_meta` 撑大 state.toml | 工具返回大 meta | 序列化限 4KB；超了丢弃并记 trace |
| 记忆误判 supersede | 相似度高但主题不同 | 只在同 refs + 更新标记/显式 `supersedes` 时失效；保留审计 |
| 旧版本回写丢字段 | 旧 exe 打开新 state | 单版本本地可接受；升级前备份 state.toml |
| 缓存失配 | HEADER 变化 | 事件块独立；Phase 0 先测 cache_hit，Phase 4 再决定是否搬 HEADER |

**回滚点**：

- Phase 1 完成后，无事件/无新记忆的会话行为不变；把 `recent_floor` 改回 `compact_upto` 即退回旧装配。
- Phase 2 完成后，`session.events=[]` 的会话走旧摘要；删事件字段不伤消息。
- Phase 3 完成后，新字段默认空；旧记忆文件读进来仍是 `live`。

---

## 七、不做

| 想法 | 为什么不做 |
| --- | --- |
| 引入 Mem0 / Zep / Letta / 图数据库 | 单用户、项目级、几十到几百条事实；确定性的文件台账足够，平台会引入运维与锁 |
| 给记忆上向量库 | 规模不需要；先做字段加权 + 时间衰减 + 中文 n-gram，语义重排可选 |
| 每轮自动抽取记忆 | 会多一次模型请求；先做「显式 remember + 写入时去重/失效」 |
| 硬删被取代的记忆 | 审计与回滚会断；用 `valid_to/superseded_by` |
| 把证据落盘 | 证据是本轮派生索引；原文在 `Session.messages`，重启后重建即可 |
| 新增 `PartType.EVENT` | 事件只进请求体与 `/events`，不牵动 TUI/web 两套渲染 |
| logit masking 限制工具 | OpenAI 兼容协议没有这个能力；只增不减的按需加载是替代 |
| 把 HEADER 立刻移到尾部 | 会动大量精确断言；先度量，Phase 4 再决定 |

---

## 八、执行顺序（本次照此推进）

**Phase 0（先做）**
1. `context.metrics` + `loop.last_input_tokens`（可度量）
2. `/context` 命令（`command_registry.py` / `app.py`）
3. 把本文件写入 `docs/context-rework-plan.md`，`docs/context-rework-proposal.md` 顶部加指针
4. 跑 `tests/test_agent.py` 留基线

**Phase 1**：1.1 水填 → 1.2 源映射 → 1.3 tool_meta → 1.4 evidence_ref → 1.5 证据排序/去重 → 1.6 recent_tool_clear + `read_turn` → 1.7 封顶
**Phase 2**：2.1 数据 → 2.2 装配 → 2.3 工具 → 2.4 摘要切换 → 2.5 主动压缩
**Phase 3**：3.1 字段 → 3.2 写入治理 → 3.3 相关性常驻 → 3.4 recall → 3.5 生命周期

每阶段收尾：对应新测试组 + 全量回归（第五节），绿了再进下一阶段；红了先修，不跳过。
