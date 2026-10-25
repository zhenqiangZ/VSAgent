# 上下文重构建议（v2）

> 分析对象：`opencode_tui` 当前的 `ContextAssembler` 装配链路。
> 代码依据：`core/agent/context.py`(729) / `prompt.py`(485) / `loop.py`(950) / `core/agent/tools.py`(1006)。
> 行号按 2026-10-24 的工作区。外部资料见文末附录。
> 本文只提**建议与改动口径**，不含实施代码。
>
> **后续执行以 [`context-rework-plan.md`](context-rework-plan.md) 为准**：那一份把本文的建议、
> 事件层方案与 memory/evidence 评审合成了一份分阶段计划，本文保留为调研与依据。

---

## 一、结论

现在的实现已经不属于「会话历史裁剪器」那一档：分层、交互组原子、统一预算、按内容压缩、
装不下报错而不是静默截断——这与 Claude Code / Factory 在 2025 年下半年定型的产品做法
是同一条路，所以**不需要推倒重来**，只需要补上它们后来才补的那几块。

真正的差距不在「层数不够」，而在四件事：

1. **预算分出去了，但没花完。** 固定的 50/25/25 让 RECENT 在窗口还很空的时候就被截断，
   空着的 HISTORY/EVIDENCE 额度不回流。按 128k 窗口实测，可用额度 86.9k，RECENT 只能拿
   40.9k——**一半的上下文在大多数会话里是浪费的**。
2. **摘要是全量重写，不是增量合并。** 每次压缩都把 0~upto 的全文重新喂一遍，再整体替换
   `session.summary`；超过 60k 字符还会被 `_cut_middle` 掐掉中间。这正是 ACE 与 Factory
   点名的 *context collapse*：压得越多次，细节掉得越多。
3. **压缩不可逆，而且引用清单在骗模型。** `_refs_block` 写着「可用 list_workspace / read
   按引用取回」，但 `list_workspace` 只列登记表，全库**没有**按 turn 取回历史的工具。
   模型按这句话去取，只会白烧一步。
4. **旧工具结果从不清理，同一段内容发两遍。** RECENT 里还在的工具输出，EVIDENCE 又把
   关键行展开一次；而 Claude Code / Manus 的第一档压缩恰恰是「清旧工具结果」，零 API 成本。

一句话目标：**从「按层切」升级成「按成本分级 + 可逆 + 可度量」**。

---

## 二、外部资料：2025–2026 的产品共识

| 来源 | 关键做法 | 报告中的数字 |
| --- | --- | --- |
| Anthropic《Effective context engineering》(2025-09) | 上下文是**有限注意力预算**；JIT 检索；压缩、结构化笔记、子代理三选一 | 子代理探索数万 token，只回灌 1–2k；压缩保留「决策、未解 bug、实现细节」 |
| Manus 复盘 (2025-07) | **KV-cache 命中率是第一指标**；只追加不改写；工具只遮不删；文件系统当外存；用 todo.md 复述目标；保留错误 | 输入:输出 ≈ 100:1；缓存价 $0.30 vs $3.00 / MTok（10×）；典型任务 ~50 次工具调用 |
| Claude Code 压缩管线（第三方拆解） | 三层：microcompact（清旧工具结果，零 API）→ session memory → full compact；压缩后回灌最近 5 个文件；连续失败熔断 | 200k 窗口在 ~167k 触发；摘要 9 段；熔断 3 次 |
| Cursor (2026-02 官方博客 / InfoQ) | 稳定段之后显式打缓存断点；可变配置放进「幻影用户消息」；MCP 工具**只发名字**、用时再取；大输出落文件；行号每 10 行标一次 | 冷缓存未命中 −20%；MCP 场景总 token −46.9%；缓存读取 −1.6% |
| Factory（厂商自测，2025-12） | **锚定增量摘要**：固定分节、只把新截断的一段合并进已有摘要，不重写 | 3.70 vs Anthropic 3.44 / OpenAI 3.35；artifact trail 全员最低（2.19–2.45） |
| Cognition / Devin（2025 复盘） | 观察到「上下文焦虑」：模型感到接近窗口上限时会提前收尾；结论是**不能把记忆管理交给模型自己**，压缩必须显式、结构化 | 无公开数字 |
| ACE / ICLR 2026 | 上下文是**增量维护的 playbook**；结构化条目 + delta 合并，禁止整体重写 | 18,282 token 的上下文被「总结」成 122 token 后，准确率 66.7% → 57.1% |
| Anthropic + 「Lost in the Middle」一类长上下文研究 | token 越多，检索与长程推理越差；注意力呈 U 形，中段最弱 | 工程上常把名义窗口的 60–70% 当有效容量 |

被反复验证的六条原则：

1. **上下文是预算，不是仓库。** 大窗口救不了注意力衰减。
2. **缓存命中率是生产成本的第一指标。** 代理读写比 100:1，前缀一抖，账单翻倍。
3. **压缩必须可逆。** 丢内容可以，丢「去哪取」不行（路径、URL、turn 号）。
4. **压缩必须增量、结构化。** 自由改写会 brevity bias，整体重写会 context collapse。
5. **分级：规则清理 → 结构化摘要 → 隔离。** 能用规则解决的绝不调模型。
6. **目标要复述到注意力末端。** Manus 反复重写 todo.md，就是为了把目标推到 context 末尾。

---

## 三、现状盘点

### 3.1 现在的装配形状

```
system : PREFIX（身份/纪律/AGENTS.md，字节稳定）
user   : HEADER（MEMORY + TASK + SUMMARY + refs）   ← 位置 2，会变
...    : RECENT（完整交互组，按 50% 配额取）
user   : EVIDENCE（本步证据，最后）
```

每层的去留由类型决定（pinned / capped / atomic / soft / ref），装不下按
`evidence_detail → history_summary → memory → recent_turns → recent_reasoning →
recent_output → recent_output_body` 逐级削，削到底报 `ContextAssemblyError`。

### 3.2 实测占用（`estimate_tokens`，128k 窗口的 deepseek-chat）

| 项 | 估算 token | 说明 |
| --- | --- | --- |
| 工具定义（29 个） | 11,127 | `write_run` 单个 1,147；`prepare_receptor` 744；`dock` 580 |
| 稳定前缀 | 5,141 | 其中 `AGENTS.md` 4,503，占 88% |
| 固定开销合计 | 16,268 | 对话还没开始就花掉的 |
| 可用额度 | 86,921 | `0.8×128k − 4,352 − 11,127` |
| RECENT 软配额 | 40,890 | 剩余额度的 50% |
| HISTORY / EVIDENCE 配额 | 各 20,445 | 两者经常是空的，但额度不回流 |

字符估算对中文尚可，对 JSON / 代码会低估（JSON 约 2.5–3 字符/token），
所以工具定义的真实占用可能比 11.1k 更高。

### 3.3 已经做对的六件事（不要动）

1. `PREFIX` 字节稳定，不放时间与进度（`prompt.py:292`）——缓存友好的前提。
2. `split_turns` 由构造保证 `tool_call` 与结果同组（`context.py:297`）。
3. 装不下报错而不是静默截断（`context.py:581`）——这条比很多产品做得严。
4. `TASK` pinned：用户约束不因对话变长而丢（`LAYER_POLICY`）。
5. 证据层只收「挑过的关键行」，不收整份输出（`EvidenceStore.add_from_part`）。
6. 压缩原语两处共用 `prompt.compress_tool_output`，翻译层不会先按位置截掉报错。

### 3.4 问题清单

| # | 现象 | 位置 | 影响 | 级别 |
| --- | --- | --- | --- | --- |
| F1 | 固定 50/25/25 配额，空层不回流 | `context.py:71-73, 524-527` | RECENT 在窗口半空时就被截断，触发不必要的摘要 | P0 |
| F2 | 摘要每次从 0 重算并**整体替换**；输入超 60k 字符走 `_cut_middle` | `loop.py:584-623`、`prompt.py:340-379` | 多次压缩后细节持续丢失（context collapse）；每次 O(n) 成本 | P0 |
| F3 | refs 说「可按引用取回」，但没有这个工具 | `context.py:668-673`、`domain/listing.py` | 模型按提示取不到，白烧步数 | P0 |
| F4 | 旧工具结果从不清理；RECENT 与 EVIDENCE 内容重复 | `context.py:702-728, 691-700` | 同一段文字每一步发两遍；丢掉的是整组而不是正文 | P0 |
| F5 | refs 清单无上限，一条 turn 一行 | `context.py:668-673` | 长会话里 HISTORY 层被清单吃满 | P0 |
| F6 | 工具定义 11k 全量常发，29 个 | `loop.py:397`、`registry.specs()` | 占可用额度 13%，且稀释工具选择 | P1 |
| F7 | 估算与实测脱节，`usage.input` 不回填预算 | `loop.py:909`、`context.py:463` | 预算永远按字符猜，JSON 场景可能低估 30%+ | P1 |
| F8 | 环境信息缺失：`workspace.state_text` 已不被装配器调用 | `prompt.py:318`、`workspace.py:31` | 模型不知道当天日期与工作目录 | P1 |
| F9 | 目标在位置 2，长 RECENT 之后远离注意力末端 | `context.py:612-641` | 跑长任务容易目标漂移 | P1 |
| F10 | 证据按插入顺序展开正文（老的先占额度） | `context.py:424-442` | 最该给的失败/最新证据反而拿不到正文 | P1 |
| F11 | `TASK` / `constraints` / `summary` 都没有上限 | `context.py:67`、`models.py:239` | pinned 层可无限膨胀，是唯一能撑爆预算的层 | P1 |
| F12 | 装配失败直接终止，没有「强制压缩后重试」 | `loop.py:436-441` | 长任务硬停，只能靠用户 `/compact` | P1 |
| F13 | 没有缓存命中率、每任务 token、重读率等度量 | `loop.py:342-362` | 改了不知道有没有变好 | P2 |
| F14 | 子代理只有 fork（复制父历史），没有隔离模式 | `spawn.py:38` | 长探索反而把父历史复制一份 | P2 |

---

## 四、建议

分四档：**P0 止血**（正确性与浪费）、**P1 质量**（注意力与保真）、**P2 成本**（工具与隔离）、
**横向 度量**（先做，才能判断后面有没有用）。

### P0-1 预算水填：把空层的额度还给 RECENT

**做法**：不再按比例切，改成「先给可变层实际需要的（带上限），剩下的全给 RECENT」。

```python
# context.py: budget()/build()
summary_cap  = min(estimate_tokens(session.summary), HISTORY_MAX)      # 例如 3000
evidence_cap = min(evidence_tokens(raw_evidence), EVIDENCE_MAX)        # 例如 6000
recent_cap   = max(0, remaining - summary_cap - evidence_cap)
recent       = _take_recent(live, recent_cap)
summary      = _cap_text(session.summary, summary_cap)
evidence     = _cap_evidence(raw_evidence, evidence_cap)
```

如果 RECENT 用不完 `recent_cap`，剩余额度再还给 summary/evidence（两者本来就有上限，通常用不着）。
`RECENT_SHARE / EVIDENCE_SHARE / HISTORY_SHARE` 三个常量删除，换成三个上限常量。

**判据**：构造一个「RECENT 有 60k token、summary 与 evidence 都为空」的会话，
装配后 RECENT 应保留 ≈60k，且**不触发**摘要请求；今天的实现在 40.9k 就截断了。

**风险**：低。只改预算分配，不动装配顺序与降级链。注意 `usage` 与 `degraded` 的
「被配额削过」判断要跟着改（现在靠比较 `memory != raw_memory` 这类等式）。

---

### P0-2 摘要改「锚定增量」，禁止整体重写

**做法**：固定分节 + 只合并新段 + 分节各自的合并语义。

摘要固定八节（前四节沿用现有四段，后四节是新增）：

```
一、用户的目标与意图
二、用户明确给出的约束与要求（只增不删）
三、已完成的改动、涉及的文件与产物（路径 / handle 必须保留）
四、做过的决定，以及被否决的方案（否决理由要写）
五、遇到的错误与修复方式
六、当前正在做的事
七、待办的下一步
八、仍然有效的关键数字（阈值、参数、分数）
```

合并语义按节区分（与 `TaskState` 的 `constraints` 追加 / `done/todo` 替换是同一个道理）：

- 二、四、五、八：**增量追加**，重复的合并，不重写；
- 一、三、六、七：**整节替换**，它们描述的是「现在」，旧版本没有价值。

第二节与 `TaskState.constraints` 会重复。约定一句：**摘要只记「任务状态里没有的」约束**，
任务状态已经记着的不再抄一遍。两者冲突时以 `TaskState` 为准（见 P1-2 的优先级行），
这样既避免每步发两遍，也避免摘要漂移后与用户原话对不上。

```python
# loop.py:_summarize
span = summary_messages(session, session.summary_upto, upto)   # 新增 start 参数，只取新露出的那一段
prompt = [
    {"role": "system", "content": SUMMARY_INSTRUCTION},
    {"role": "user", "content": f"【已有摘要】\n{session.summary or '（空）'}\n\n"
                                f"【需要合并的新对话】\n{span}"},
]
session.summary = text
session.summary_upto = upto
```

配套两条：

1. **摘要输入不再 `_cut_middle`。** 新段本身通常只有几 k；万一超过 `SUMMARY_INPUT_LIMIT`，
   按时间顺序切成 2–3 块**顺序合并**，宁可多一次调用，也不掐掉中间——中段正是
   「被否决的方案」与「错误修复」最常出现的位置。
2. **摘要长度封顶**（例如 3,000 token）。超了让模型「把已完成的旧项合并成一行」，
   而不是整段重写。

**判据**：构造一个含已知事实的 60 轮会话（最初错误、用户约束、被否决的方案、产物路径），
连续压缩三次后，用固定问题探针检查四类事实是否仍在。这条判据不需要真实模型也可以做一半：
断言第二次摘要的输入里含第一次的摘要原文，且 `summary_upto` 单调递增。

**风险**：增量摘要会积累陈旧信息。用「整节替换」的四节兜住「现在」；
另在装配时加一句优先级（见 P1-2）。

---

### P0-3 让压缩真正可逆：`read_turn` + 修正 refs 文案

**做法**：新增一个内核工具（不是领域工具），按 turn 序号取回被移出上下文的交互组。

```python
class ReadTurnTool(Tool):
    spec = ToolSpec(
        name="read_turn",
        description="按 turn 序号取回被移出上下文的交互组原文；"
                    "参数 turn 取自引用清单里的编号。可选 part 只看某一次工具结果。",
        parameters={
            "type": "object",
            "properties": {
                "turn": {"type": "integer", "description": "引用清单里的 turn 编号"},
                "part": {"type": "string", "description": "只看这一次工具结果的正文，可省略"},
            },
            "required": ["turn"],
            "additionalProperties": False,
        },
    )
    session_argument = "session"      # 通道已存在（tools.py:263）

    async def run(self, arguments):
        turns = split_turns(arguments["session"].messages)   # 函数体内 import，避免环
        ...  # 找到 turn.index == n，渲染成文本，按 truncate_for_model 裁剪
```

同时把 `_refs_block` 的说明改成真话：

```
（更早的内容已省略；需要原文用 read_turn turn=12 取回）
```

**判据**：一个交互组被移出后，`read_turn` 返回的正文与 `session.messages` 里那份一致；
refs 文案里出现的工具名必须在注册表里（防再次出现「说了但做不到」）。

**风险**：低。`session_argument` 已存在，不需要新注入通道；`split_turns` 惰性导入即可。

---

### P0-4 旧工具结果清理（Tier-0 压缩，零 API）

**做法**：在降级链里加一步，排在「整组移出」之前：

```
evidence_detail → history_summary → memory → recent_tool_clear → recent_turns
                → recent_reasoning → recent_output → recent_output_body
```

`recent_tool_clear` 的规则：

- 保留最近 `TOOL_KEEP=4` 条工具结果；
- **失败的结果不清理**（Manus：错误留在上下文里，模型会自己避开重复路径）；
- 其余超过 `CLEAR_FLOOR=400` token 的，正文换成一行占位：
  `（输出已归档：turn 12 的 dock；原文用 read_turn turn=12 取回）`；
- 记进 `degraded`，事件里能看到削到了这一级。

这是 Claude Code `microcompact` 的同一招：零调用、可逆、先于任何模型摘要。

**判据**：构造 20 组、每组 2k token 的工具输出，触发一次装配后：
被清理的条数 = 总数 − 4 − 失败数；每一条占位里的 turn 号都能被 `read_turn` 取回；
清理后总 token 明显下降且 `degraded` 含 `recent_tool_clear`。

**实现前提**：`translate_message` 目前把 `tool_status` 丢掉了，`Turn` 里只有模型消息字典。
「失败的不清理」和 P1-1 的「哪些 part 还完整可见」都需要状态与 part id，
所以 `Turn` 要多存一份「模型项 → 源 Part」的映射（在 `split_turns` 里顺手建立即可）。
不建这份映射，就只能靠输出文本猜失败，那是错的。

---

### P0-5 引用清单与固定层封顶

**做法**：

- `_refs_block` 只列最近 20 组，末尾一行 `（另有 N 组更早的已归档，用 read_turn 按序号取回）`；
  整块按 600 token 截断。
- `TaskState.render` 每个字段设上限：`constraints` 最多 20 条、`done` 最多 20 条、
  `todo`/`blockers` 各 10 条；超了在渲染时截断并写「另有 N 条未显示」。
  写入时不做限制（不丢用户数据），只在进上下文时截。
- `session.summary` 按 P0-2 封顶 3,000。

**判据**：200 组被移出的会话，refs ≤ 21 行；`constraints` 写 50 条时装配后 ≤ 20 条且有省略说明。

---

### P1-1 证据层只放「RECENT 已经看不见的东西」

**现状的问题**：证据是从本步工具结果提炼的，而那条结果此刻还在 RECENT 里；
EVIDENCE 又把关键行展开一次，同一段文字每一步发两遍。

**做法**：

1. `Evidence` 增加 `source_part: str = ""`；`add_from_part` 记下 `part.id`。
2. `_compose` 接收 `visible_parts`（RECENT 里**未被清理/压缩**的工具结果 id 集合）。
3. `_evidence_block`：`source_part` 在 `visible_parts` 里的条目不展开正文，只留索引行；
   不在的（已被移出或已清理）才展开。
4. `_cap_evidence` 按「失败优先、新的优先」排序后分配正文额度，而不是按插入顺序。

这样证据层的语义就和设计稿写的一致了：**它是 RECENT 被削弱之后的备份，不是 RECENT 的副本**。

**判据**：正常一步装配出来的文本里，任何一条工具错误原文只出现一次；
把该组移出后，同样的错误原文改由 EVIDENCE 提供，仍然只出现一次。

---

### P1-2 环境块 + 目标复述（把注意力拉回末端）

**做法**：

1. 恢复环境信息，但**放在 HEADER，不放 PREFIX**（缓存友好）：
   `当前时间：2026-10-24 14:05（Asia/Shanghai）｜工作目录：…｜平台：Linux`。
   只占 ~40 token，却决定了模型查文献、写日期、拼路径时的正确性。
2. 在最后一条 user 消息前加一段 `FOCUS`（与证据块合并成一条，避免多条 user）：

```
（以下是这一步要继续做的事，不是新的用户请求）
目标：找出 4HJO 的共晶口袋并完成 20 个配体的对接
必须遵守：- 只用 prepare_ligands 产出的 ligandset#2
下一步：- 跑 dock，比较最佳分数
发生冲突时：本段 > 项目记忆 > 早前摘要 > 工具结果
```

内容取自 `TaskState`，上限 300 token；`TaskState` 为空时整段不出现。

这就是 Manus 的 todo.md 复述：目标本来在位置 2，RECENT 一长就到了中段（U 形注意力的谷底）；
复述一次到末尾，成本 200–300 token，收益是长任务不跑偏。

**判据**：装配结果的最后一条 user 消息含 `goal`；`PREFIX` 字节在连续两步中相等。

---

### P1-3 主动分级压缩 + 熔断 + 失败兜底

**做法**：

1. **主动触发**：每步开始时看上一轮的实测 `input / context_window`。
   ≥70% 先跑 `recent_tool_clear`；≥80% 在**本轮第一步之前**生成摘要，
   而不是等装配器削到一半再同步插入一次摘要请求。压缩放在轮次边界，
   延迟可预期，也不会在工具链中间打断。
2. **熔断**：摘要连续失败 3 次就停止尝试（Claude Code 线上曾经一天浪费 25 万次调用），
   发一条 `COMPACTION` Part 说明，之后只做规则清理。
3. **失败兜底**：摘要失败时不要只写「这一段按丢弃处理」，用本地数据拼一份确定性摘要：
   `TaskState.render()` + Ledger 产物清单 + 最后一条用户消息。它不如模型写的好，
   但一定出得来，且不会让模型在「没有摘要也没有原文」的真空中继续。
4. **装配失败不再直接终止**：`ContextAssemblyError` 时先强制 `compact_session(keep=2)`
   + 摘要 + 重新装配一次，仍失败才报错退出。

**判据**：脚本化 200 步会话不出现 `EXIT_ERROR`；摘要调用次数 ≤ 压缩次数；
连续失败注入 3 次后不再发请求。

---

### P1-4 产物索引（artifact trail 是公认最弱的一项）

Factory 的评测里，三家产品的 artifact trail 都只有 2.19–2.45 / 5——模型经常不知道
自己改过哪些文件。我们的对应物是 Ledger：受体、配体集、对接/亲和力产物。

**做法**：在 HEADER 里加一小块 `ARTIFACTS`（≤10 行、≤400 token）：

```
当前受体：receptor#1（4HJO，已准备）
配体集：ligandset#2（120 个，已准备 118）
产物：artifact#3 dock（最佳 −9.2）｜artifact#4 affinity（最佳 pK 7.8）
```

数据直接来自 `SessionLedger.render_digest()`，不需要模型调 `list_workspace`。
只列当前受体 + 最近 5 个产物，其余仍在工具里查。

**判据**：装配结果里出现当前受体的 handle；`list_workspace` 的调用次数在脚本任务里下降。

---

### P2-1 工具定义瘦身（先做无损的，再做按需的）

**第一档（零缓存风险）**：只改文案与 schema 结构。

- `write_run` 1,147 token 里大半是重复的字段说明，可压到 ~500；
- `prepare_receptor` / `dock` / `score_affinity` 的参数描述互相抄，抽成一句共用说明；
- 目标：29 个工具从 11.1k 降到 6k 左右。

**第二档（按需加载，收益更大但要动协议形状）**：

- 常驻只发「5 个内核工具 + `list_workspace` / `describe_entry` / `task` / `read_turn`」
  与一份 `list_tools` 索引（名字 + 一行，~1.2k）；
- 新增 `load_tools(family)`，返回该族的 schema，循环层把它**追加**进本会话的工具清单；
- **只增不减**（Manus：中途删工具会同时击穿缓存并让模型困惑于「它刚才调过的工具哪去了」），
  所以一次加载只让缓存断一次，之后稳定；
- 我们的协议是 OpenAI 兼容，**没有 logit masking**，所以「遮住」做不到，只能「增删」；
  这也是为什么必须坚持只增不减。

**判据**：`/context` 显示工具 token ≤6k（第一档）/ ≤4k（第二档）；
脚本任务里每个族只加载一次；不出现「未知工具」的重试。

---

### P2-2 子代理补一个「隔离模式」

**现状**：`spawn.py` 的 `fork_messages` 是把父会话的一段**复制**给子会话。
这是「上下文传递」，不是「上下文隔离」——探索过程虽然不回灌，但父历史被复制了一份。

**做法**：给 `SpawnAgentTool` 加 `mode`：

- `fork`（现有）：需要连续性时用，复制一段父历史；
- `fresh`（新增）：只给任务书 + 相关 handle/路径，子会话从空开始；
  回报按 `truncate_for_model` 压到 ≤1,500 token。

适用于「文献检索」「配体库初筛」这类探索量大、结论短的任务。
不要在系统提示里鼓励模型到处派子代理——Cursor 2026 年的做法反而是**删掉**这类诱导，
因为模型已经原生会用，过度鼓励会带来协调成本。

**判据**：`fresh` 模式下父会话只多一条消息（回报），子会话的中间工具输出不进父历史。

---

### P2-3 常驻记忆分级

**现状**：`resident_text(2000)` 每步全量注入，按「有引用 > 新」排序，不是按相关性。

**做法**：

- 记忆分两级：`core`（≤5 条、≤600 token，每步常驻）与 `archival`（其余，走 `recall`）。
  `MemoryEntry` 增加两个可选字段（`to_dict` / `from_dict` 给默认值，老文件照常读）：
  `core: bool = False`、`last_used_at: float = 0.0`。
- 升级规则：被 `recall` 命中时更新 `last_used_at`；带 `refs` 且近 30 天被命中过的升为 core。
- 降级规则：`last_used_at` 超过 90 天的 core 退回 archival；`expires_at` 已经能处理
  「文件没了/过期」这一类，不用另造一套。
- 现有 `resident_text` 的排序（有引用优先、同优先级新的优先）保留，只把 `limit`
  从 2,000 降到 600，其余靠 `recall` 取。

**判据**：记忆 20 条时，常驻段 ≤600 token；`recall` 仍能取到 archival 的全部条目；
老格式的记忆文件读进来不报错（新字段取默认值）。

---

### 横向：可观测与评测（建议最先做最小版）

没有度量，上面每一条都无法判断「改好了没有」。分两层：

**A. 确定性判据（进 CI）**——CI 现在只跑 `tests/test_agent.py` 与 `tests/test_smoke.py`，
所以这些检查要加进 `test_agent.py` 的检查组（`GROUPS`），不是另起一个没人跑的脚本：

- 新增 `check_context_budget`：预算分配与封顶；
- 扩 `check_selective_compression`：清理后的占位、`degraded` 顺序；
- 扩 `check_evidence_layer`：去重与排序。

具体六条：

1. 无孤儿：任一 `tool` 结果与它的 `tool_call` 同组。
2. 不重复：装配文本里同一条工具错误原文只出现一次（P1-1）。
3. 可逆：每条被清理/移出的内容都能被 `read_turn` 取回（P0-3/P0-4）。
4. 有界：refs ≤21 行、summary ≤3k、TASK 各字段 ≤上限。
5. 预算：估算 ≤ `available`，且 `degraded` 顺序符合 `DEGRADE_ORDER`。
6. 缓存：连续两步的 `system` 字节相等；HEADER 在 TaskState 不变时字节相等。

要快速单跑可以用 `python tests/test_agent.py --only context`（`GROUPS` 的名字按现有惯例取）。

**B. 功能探针（手动，接真实模型）**——`tools/eval_context.py`：

按 Factory 的四类探针，在压缩后提问：

| 探针 | 问题 | 测什么 |
| --- | --- | --- |
| Recall | 最初那个报错原文是什么？ | 事实保真 |
| Artifact | 现在用的是哪个受体/配体集？ | 产物追踪 |
| Continuation | 下一步该做什么？ | 任务连续性 |
| Decision | 为什么否决了那个参数？ | 决策链 |

再加三个**成本指标**：每任务 token、缓存命中率（`cached / input`）、
同一文件/同一工具的重读率。重读率高说明压缩丢了「已经做过什么」，
这是比 token 数更贴近「用户体感」的指标。

**C. 事件**：`context.assembled` 目前只在真削过时发。加一个 `context.metrics`
（每步或变化时发）带 `estimated / actual_input / cached / cache_hit / usage / degraded`；
界面上先不加（会让界面截图基线全部失配），改用 `/context` 命令打印最近 10 步的表格。

---

## 五、目标装配布局

```
system : PREFIX                       ← 字节稳定，缓存起点
user   : ANCHORS                      ← MEMORY + TASK + ARTIFACTS + SUMMARY + refs
...    : RECENT（旧工具结果可换成一行占位）
user   : FOCUS + ENV + EVIDENCE       ← 目标复述、时间/目录、本步证据
```

与现在的差别只有两处：

1. **可变内容整体后移**。HEADER 现在在位置 2，TaskState 一改，整段历史缓存全废；
   移到 RECENT 之后，同样的修改只废掉尾部。头部的注意力位置也不浪费——
   `PREFIX` 本来就该独占。
2. **最后一条消息承担「这一步做什么」**。目标复述、环境、证据放在一起，
   正好落在 U 形注意力最强的末端。

代价：模型可能把末尾的背景块误当成新的用户请求。用明确标签
（「以下是背景资料，不是新的用户请求」）即可，现有 EVIDENCE 块已经是这个形状。

---

## 六、分期与依赖

| 期 | 内容 | 依赖 | 预期收益 |
| --- | --- | --- | --- |
| 0 | 度量：`context.metrics` + `/context` + 确定性判据 | 无 | 后面每一步可验证 |
| 1 | P0-1 预算水填、P0-5 封顶 | 期 0 | 可用上下文从 ~41k 恢复到 ~80k（128k 窗口） |
| 2 | P0-3 `read_turn`、P0-4 工具清理 | 期 0 | 压缩可逆；零 API 的第一档压缩 |
| 3 | P0-2 锚定增量摘要、P1-3 主动压缩 + 熔断 | 期 2（`read_turn` 是兜底） | 多次压缩不塌；长任务不硬停 |
| 4 | P1-1 证据去重、P1-2 环境 + 复述、P1-4 产物索引 | 期 3 | 每步少 1–3k 重复 token；目标不漂 |
| 5 | P2-1 工具瘦身、P2-2 隔离模式、P2-3 记忆分级 | 期 4 | 固定开销从 16k 降到 ~8k |

期 1 与期 2 互不依赖，可以并行；期 3 必须等 `read_turn`，否则清理掉的内容取不回来。

### 改动面速查

| 建议 | 主要文件 | 改动性质 |
| --- | --- | --- |
| P0-1 预算水填 | `context.py`（`budget` / `build` / 常量） | 改分配，不动布局 |
| P0-2 锚定摘要 | `prompt.py`（指令与输入构造）、`loop.py:_summarize` | 改提示词与输入范围；`session.summary` 格式不变 |
| P0-3 `read_turn` | `tools.py`（新工具）、`app.py`（注册）、`context.py:_refs_block` | 新增工具 + 改一句文案 |
| P0-4 工具清理 | `context.py`（`DEGRADE_ORDER` / `_degrade_recent`）、`prompt.py`（占位串） | 降级链插一步 |
| P0-5 封顶 | `context.py:_refs_block`、`models.py:TaskState.render` | 只改渲染 |
| P1-1 证据去重 | `context.py`（`Evidence` / `_cap_evidence` / `_compose`）、`loop.py` | 加字段 + 传一个可见集合 |
| P1-2 环境 + 复述 | `workspace.py`、`context.py`（`_task_header` / `_compose`）、`loop.py` | 改头部组装 |
| P1-3 主动压缩 | `loop.py`（`_run_steps` / `_summarize`）、`context.py`（metrics） | 改触发时机与兜底 |
| P1-4 产物索引 | `ledger.py`（`render_digest`）、`loop.py` / `context.py` | 新渲染函数 |
| P2-1 工具瘦身 | `domain/*.py` 的 `ToolSpec`、`tools.py`（`specs` 过滤）、`all_tools.py` | 文案 + 注册表过滤 |
| P2-2 隔离模式 | `spawn.py`、`host.py` / `loop.py` | 加 `mode` |
| P2-3 记忆分级 | `memory.py`（字段与 `resident_text`） | 加两个可选字段 |
| 横向度量 | `loop.py`（`context.metrics`）、`app.py`（`/context`）、`tests/test_agent.py`（新检查组）、`tools/eval_context.py`（新） | 新事件 + 新命令 + 扩判据 |

---

## 七、不建议做的事

| 想法 | 为什么不做 |
| --- | --- |
| 给会话内记忆上向量库 | 会话规模只有几十条，`recall` 的词法/记忆库已经够；多一层检索只会增加不一致 |
| 用 logit masking 限制工具 | OpenAI 兼容协议没有这个能力；只增不减的加载是同一目的下的可行替代 |
| 把所有工具输出写文件 | 目录爆炸、I/O 变慢；只对「超过 400 token 且会被清理」的那一类落盘/归档 |
| 每次压缩都调模型 | 先规则清理再摘要；ACE 的教训是「模型擅长用知识，不擅长整理知识」 |
| 依赖更大的窗口 | context rot 是模型结构问题，不是容量问题；名义 200k 的有效容量常按 60–70% 估 |
| 把 `AGENTS.md` 再写长 | 它已经占前缀 88%；项目规则要精简，否则每步都在为旧规则付费 |
| 为了缓存永不压缩 | 缓存省的是钱，不是注意力；该压还得压，只是要可逆、要增量 |

---

## 附录：资料来源

1. Anthropic, *Effective context engineering for AI agents*（2025-09）
   https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
2. Manus / 季逸超，*Context Engineering for AI Agents: Lessons from Building Manus*（2025-07）
   中文转述：https://news.qq.com/rain/a/20250721A06D1G00
3. Claude Code 压缩管线拆解（第三方，2026）：
   https://harrisonsec.com/blog/claude-code-context-engineering-compression-pipeline
   https://claude-wiki.com/auto-compact.html
4. Cursor, *Improving token efficiency for long-running agents*（2026-02）
   https://cursor.com/blog/improved-token-efficiency
   InfoQ 转述（动态上下文发现，MCP 工具 −46.9%）：
   https://www.infoq.cn/article/WJL8IKHd99G4zrEyTO99
5. Factory, *Evaluating Context Compression for AI Agents*（厂商自测，2025-12）
   https://factory.com/news/evaluating-compression
6. Zhang et al., *Agentic Context Engineering*（ICLR 2026, arXiv:2510.04618）
   https://academy.dair.ai/papers/agentic-context-engineering-ace
7. LangChain 的 write / select / compress / isolate 四策略综述（2026 转述）
   https://www.reactify-solutions.com/articles/context-engineering-ai-agents-2026
8. Cognition / Devin 的「上下文焦虑」复盘（中文转述，非一手）
   https://juejin.cn/post/7604037348607115302
