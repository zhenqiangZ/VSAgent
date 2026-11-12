# 运行期问题修复计划

日期：2026-11-11
依据：`ses_0027`（2026-11-10 12:26）与 `ses_0028`（2026-11-10 12:28–12:34）的轨迹、状态文件与登记表。

## 一、这份文件管什么

本文件写本轮要修的运行期问题：每一项改哪里、怎么验、有什么代价。
机制背景见 `docs/design.md` §3.2（上限与重试）、§4.1–4.3（上下文装配与事件层）、§5.1（工具分族）。
本文件不重复这些机制，只写改动与判据。

## 二、范围与决定

| 项 | 观测到的现象 | 本轮决定 |
| --- | --- | --- |
| 确认点被绕过 | `ses_0027` 只有一条用户消息，第 9 步 `define_pocket`、第 18 步 `prepare_receptor(confirm=True)`、第 19 步 `write_run`，没有对应的用户确认 | **不做** |
| 重复答复 | 同一轮出现两份内容几乎相同的答复（`prt_4280` 1190 字、`prt_4287` 1268 字） | **只做 A**：封存时把已答复原文带进下一段；不结束轮次、不合并主题 |
| 步数用满 | `ses_0027` 走满 24 步，只留下一份状态简报，没有回答用户的问题 | 做：改收尾指令 |
| `history` 负数 | `-26`、`-1` | 做：改成逐块计量 |
| 假设同批创建 | `hypothesis` 连续三次失败 | 做：新增一次创建多条的动作 |
| 事件日志过多 | 一轮 18 步写 12,221 条 `part.update`、1.7 MB | 做：缓冲与采样 |
| 兜底标题 | 「P03452-survey-565a0ec1、open_viewer」 | 做：中文标签与编号过滤 |
| 结构数口径 | 同一轮出现 23（UniProt 登记）与 26（RCSB） | 做：标明来源 |
| 计划路径未交付 | `runs/HA2_screening_plan_v2.md` 未出现在答复里 | 做：收尾时补一行 |
| 内部词 | `hyp#2`、`rivals`、`expand`、`回召集` | 做：提示词与工具输出 |
| `load_tools` 单族 | 一轮连发 4 次 | 做：支持一次装多族 |
| token 成本 | 18 步累计输入 45.7 万 | 不单列：随前几项自然下降 |

## 三、逐项方案

### 3.1 重复答复：只做 A

**现象与原因。**
`close_topic` 的语义是「这一步结束时封存」（`vsagent/core/agent/event_log.py:662`）。第 16 步把答复写完，同一步调用 `close_topic`，答复随主题一起被封存；第 17 步 `RECENT` 归零，模型在推理里写「用户没有再给新消息」，于是重写一遍。第 14 步与第 17 步还各重开了一次两个全景页。

**边界。**
一轮里出现多个主题是设计，本项不结束轮次、不合并主题、不改 `close_topic` 的时机；只让下一段知道「上一段已经答复过什么」。

**改法。**

1. `vsagent/core/models.py` 的 `Session` 增加两个只在内存里的字段，与 `topic_title` 同一种生命周期：
   - `topic_reply: str = ""`：上一段最后一条助手正文；
   - `topic_reply_at: tuple[int, int] = (0, 0)`：这段正文在会话里的位置（消息序号、部件序号）。
2. `vsagent/core/agent/event_log.py:seal_topic` 在算出 `end` 之后，从 `end` 往前扫到 `start`（`end` 是开区间，不含它本身），取最后一条非空 `PartType.TEXT`，记下正文与位置。这一段没有正文时两个字段都清空，避免上一段的内容留到下一段。
3. `_compose`（`vsagent/core/agent/context.py:1291`）增加 `reply_text` 参数，在 `header` 之后、事件块之前单独放一条用户消息：

   ```
   本段之前已经给用户的答复（原文，供衔接，不要重复）：
   ……
   ```

4. 注入条件不能只看字段是否为空。`stream_reply` 开头会补封上一轮中断的主题，而那时新用户消息已经写进会话；仅靠「用户消息清空字段」会被这次封存重新写回。所以再加一条位置判断：只有 `topic_reply_at` 晚于最后一条用户消息的位置时才注入。
5. `Session.add_message` 在 `role == "user"` 时清空这两个字段，作为第二道保险。
6. 长度上限 1200 字。超过时取前 600 字加后 600 字，中间写省略号；保留开头结论与结尾要用户决定的事项。

**验收。**

- 复现 `ses_0028` 的形状：一条用户消息、答复写完、`close_topic`、模型再走一步。新一步的请求里能看到「本段之前已经给用户的答复」，模型不再重写同一段。
- 新用户消息进来后，这一段不再出现。
- 中断后补封上一轮时，不把上一轮的答复带进新的一轮。

**代价与风险。**

- 每一步多 300–500 token；`ses_0028` 那样剩 4–5 步时约多 2,000 token，换掉了一次完整重写与两次重复开页面。
- 截断可能丢掉中间段落；开头结论与结尾待决事项都保留。

### 3.2 步数用满时给出答复

**现象。**
`_wrap_up` 的指令（`vsagent/core/agent/loop.py:168`）只要求四段简报：停在哪里、已经产出、还没做完、下一轮从哪继续，并且明确「不要向用户提问」。所以 `ses_0027` 的收尾只有状态清单，没有回答「给我一个可行的计划」。

**改法。**

1. 在 `WRAPUP_INSTRUCTION` 最前面加一段「给你的答复」：直接回答用户这一轮问的事；没做完就给出目前能给的结论和缺口，再写原来的四段。
2. 保留「停止原因写进指令」「收尾请求不带工具」这两条现有约束，不改 `_wrap_up` 的请求形状。
3. 更新 `tests/test_agent.py:check_step_limit`：增加一条断言，收尾指令里含「先回答」或「给你的答复」；原有的「收尾请求不带工具」「有一条 SUMMARY」保持不变。

**验收。**

- 走满步数上限时，`SUMMARY` 的第一段能回答用户的问题，而不是只有状态清单。
- 原有 `check_step_limit` 与 `check_wrap_up_fallbacks` 全过。

**不在本项内。**
「收束阶段连续被拒后强制停」另列，见第六节。

### 3.3 `history` 出现负数

**现象与原因。**
`context.py:1372` 用 `header` 减去 `memory`、`task`、`digest`、`env`、`focus` 得到 `HISTORY`。但 `focus_text` 是单独追加的消息（`context.py:1334`），不在 `header` 里；`hint` 与 `progress` 在 `header` 里却没有被减。两个方向都错，长 `focus` 加短摘要时 `history` 就成了负数。`total = sum(usage.values())` 又是降级链与「装配失败」的判据，负数会连带少算窗口占用。

**改法。**

1. `_task_header`（`context.py:1383`）改为返回 `(text, parts)`，其中 `parts` 是每一块的 `(Layer, 文本)` 列表：环境、常驻记忆、任务状态、工作区索引、摘要、引用清单、进度提示。
2. `_compose` 按实际拼进去的块累加：`PROFILE_MEMORY` 只算 `profile`，`TASK` 算任务、索引、环境、`focus`、`hint`、`progress`、答复承接块，`HISTORY` 只算摘要与引用清单。
3. 删除 `context.py:1372–1379` 的相减。
4. 保留 `estimate_tokens` 作为唯一估算函数，不引入第二套计数。

**验收。**

- 所有层的用量不小于 0。
- 长 `focus`（约 800 字）加短摘要（约 10 字）的场景下，`history` 不再为负。
- `sum(usage.values())` 与逐块相加的结果一致。
- `check_focus`、`check_compact`、`check_overflow_phase` 全过，并补一条上述长 `focus` 的用例。

**依赖。**
3.1 的承接块要算进 `TASK`，所以本项先做，或者与 3.1 同一次改动实施。

### 3.4 假设同批创建

**现象。**
`ses_0028` 第 10 步一次发起三条 `propose`，前两条的 `rivals` 指向尚未创建的编号，失败；第 11、12 步补建；第 13 步 `update` 又因为 `test` 里没有字面出现 `hyp#3` 被拒。工具要求同批互指，接口却不支持同批引用。

**改法。**

1. `hypothesis` 的 `action` 增加 `group`，参数 `items` 是数组（2–4 条）。每条含 `key`、`kind`、`claim`、`mechanism`、`evidence`、`test`、`expected`，策略条要带 `mode`，可选 `rivals`（本组内的 `key` 数组）与 `discriminator`。
2. `key` 只在本组内有效，格式 `[a-z0-9_-]{1,12}`，组内唯一。
3. 先校验整组，再分配编号并写入：任何一条不过，整组都不写，避免半组假设互相指向。
4. `rivals` 里有内容时，`discriminator` 必填，写清「什么结果支持本条、什么结果支持对手」；工具检查它提到了每个对手的 `key`。
5. 保留单条 `propose`；`discriminator` 作为新字段，旧的「`test` 里必须出现编号」保留一版作为兼容回退。
6. `vsagent/core/models.py` 的 `Hypothesis` 增加 `discriminator`，同步 `to_dict` / `from_dict`。
7. 更新提示词里关于假设的段落：两条以上互相竞争时优先用 `group` 一次提出。

**验收。**

- 一次 `group` 调用创建三条假设，`rivals` 正确解析成本组编号，没有任何失败重试。
- 组内任一字段不合法时，`state.hypotheses` 的数量不变。
- `hypothesis(action="list")` 能显示每条的 `discriminator`。
- 补一条 `check_hypothesis_group` 用例，覆盖成功、整组回滚、`discriminator` 缺失三种情况。

### 3.5 事件日志过多

**现象。**
`ses_0028` 一轮 18 步写了 12,221 条 `part.update`、1.7 MB；`TraceWriter._write`（`vsagent/core/agent/trace.py:64`）每条都写并刷盘。`logs/file-reads.jsonl` 已经 27.8 MB。

**改法。**

1. `TraceWriter` 增加缓冲：`flush_interval=0.2` 秒、`max_buffer=64 KB`；`_write` 只把行追加到缓冲区，达到时间或大小再刷盘。
2. `part.update` 采样：同一个 `part_id` 每 250 毫秒最多写一条，其余事件不采样。`part.update` 的负载只有三个编号，正文在状态文件里，丢掉中间帧不影响回放与成本分析。
3. `current_path()` 与 `stop()` 先刷缓冲区；`_close()` 同样先刷。
4. `vsagent/core/file_log.py` 增加按大小的轮转：写之前如果文件超过 32 MB，改名为 `file-reads.jsonl.1`（覆盖旧的那一份），再继续追加。

**验收。**

- `check_trace` 里原有的三条事件仍然按顺序落盘（读取前显式刷一次）。
- 连续发 1,000 条 `part.update`，落盘不超过约 5 条；`part.add`、`turn.done` 等事件一条不少。
- 换会话时旧文件不再增长。
- `file-reads.jsonl` 超过阈值后出现 `.1` 文件，新文件从零开始。

**代价。**
丢失毫秒级的流式时间点，保留每秒四帧；回放不需要更细的精度。

### 3.6 兜底标题暴露内部编号

**现象。**
`ses_0028` 第三个主题叫「P03452-survey-565a0ec1、open_viewer」，`degraded=True`。`_local_title`（`event_log.py:370`）在有产物时直接取「产物名 + 工具名」。

**改法。**

1. 增加一张工具中文名表：`open_viewer` → 查看结构，`hypothesis` → 记录假设，`survey_structures` → 结构全景，`task` → 更新任务，`write_run` → 写运行目录；表里没有的保留原名。
2. 产物名先去掉末尾的 `-8 位十六进制` 编号，再判断是否可读；仍然像内部编号的（含 `artifact#`、`receptor#`、`ligandset#`）一律不取。
3. 没有可用信息时写「过程记录（N 步）」，并保持 `degraded=True`，让界面标明是自动命名。

**验收。**

- 同样的产物与工具组合，标题不再出现十六进制编号与英文工具名。
- 有用户原话时仍然优先用原话，`degraded=False`。
- 补一条 `_local_title` 的单元用例。

### 3.7 结构数标明来源

**现象。**
`fetch_target` 报 P03452「23 个实验结构」（UniProt 登记），`survey_structures` 报「RCSB 命中 26 套」。计划与答复只写 26，没有说明差异。

**改法。**

1. `vsagent/core/agent/domain/uniprot.py:494` 与 `:553` 的数字后加「（UniProt 登记）」。
2. `vsagent/core/agent/domain/campaign.py:598` 同样加来源。
3. 在结构那一行补一句「RCSB 当前数量以 `survey_structures` 为准」，不额外发请求。
4. 计划与答复里引用结构数时带来源；两个数字不一致就并列写。

**验收。**

- `fetch_target` 与 `assess_campaign` 的输出都能看出数字来自 UniProt。
- `check_uniprot_tools` 增加一条来源断言。

### 3.8 计划路径必须出现在答复里

**现象。**
`runs/HA2_screening_plan_v2.md` 写了 109 行，两段答复都没有给出路径。

**改法。**

1. `_finish`（`vsagent/core/agent/loop.py:1583`）在写 `STEP_FINISH` 之前，收集本消息里成功的 `write` 部件，取 `file_path`。
2. 把最终答复的所有 `TEXT` 拼起来，检查每个文件名是否出现；缺失的补一条 `TEXT` 部件：「本轮写入：<路径>」，多个路径用顿号分隔。
3. 只补文件名缺失的那些，不重复已有的；路径按写入顺序。

**验收。**

- 有 `write` 且答复没提路径时，最终消息多一条路径说明。
- 答复里已经提到文件名时，不重复追加。
- 补一条 `check_write_delivery` 用例。

### 3.9 面向用户不使用内部词

**现象。**
答复里出现 `hyp#2`、`hyp#3`、`rivals`、`expand`、`回召集`。

**改法。**

1. `vsagent/core/agent/prompt.py` 的表达规范加一条：面向用户不写 `hyp#N`、`rivals`、`expand`、`回召集`；假设用中文名称指代（「假设二（组外筛有效）」），第一次出现要给一句解释。
2. `hypothesis` 的工具输出改为「已记录 假设二（策略·组外）」，编号只放在 `meta` 里；`update` / `close` 仍然接受编号，模型从 `list` 取。
3. 「回召集」统一改成「召回检验」。

**验收。**

- `check_prompt` 增加上述规则的断言。
- 用一组固定的三条假设跑一遍，用户可见文本里不出现 `hyp#` 与 `rivals`。

### 3.10 `load_tools` 支持一次装多族

**现象。**
说明写「一次把这一轮要用的族都装上」，参数只收一个 `family`（`vsagent/core/agent/toolset.py:107`），`ses_0028` 连发 4 次。

**改法。**

1. 参数增加 `families`（字符串数组，至少一项），`family` 保留为单族写法；两者只能给一个。
2. `run()` 按顺序装入每一族，合并输出；已经装过的族只列一遍。
3. 说明改成「一次装完；`families` 与 `family` 二选一」。

**验收。**

- 一次 `families=["listing","structure"]` 之后，两族都在 `session.tool_families` 里。
- 两个参数同时给时明确报错，不静默取其一。
- 补一条 `check_tool_context` 用例。

## 四、实施顺序

1. **3.3 上下文计量**：3.1 的承接块要算进去，先改计量，避免新块把负数放大。
2. **3.1 重复答复 A**：直接对应本轮最明显的浪费。
3. **3.2 收尾答复**：只改指令，风险最低，先让「步数用满」有可用产出。
4. **3.4 假设同批创建**：改动集中在 `hypothesis.py` 与 `models.py`。
5. **3.5 事件日志**：独立改动，放后面。
6. **3.6–3.10**：彼此独立，按顺序做，每项带自己的判据。

## 五、回归与验收

- `python tests/test_agent.py`：重点看 `check_step_limit`、`check_wrap_up_fallbacks`、`check_trace`、`check_focus`、`check_compact`、`check_overflow_phase`、`check_loop`、`check_prompt`、`check_tool_context`。
- `python tests/test_smoke.py`：界面侧不因新增部件或标题变化出错。
- `python tools/check_prefix.py`：3.1 的承接块放在 `header` 之后、事件块之前，前缀字节要保持稳定；这一条最容易因为位置放错而变红。
- `python tools/check_style.py`：新增注释与文档继续用中文。
- 复现场景：用 `tests/fake_model_server.py` 模拟一次「答复写完 → `close_topic` → 再走一步」，断言没有第二份相同答复；模拟 1,000 条 `part.update`，断言落盘行数；模拟长 `focus` 加短摘要，断言 `history` 非负。

## 六、本轮不做

1. **确认点硬闸门**（用户决定）。`ses_0027` 的 `receptor#3`（`~/.vsagent/receptors/3EYM-A+B+C+D-7a6060d9`）与 `runs/HA2_3EYM_TBHQ_pocket` 仍在磁盘上，本轮不把它们当作已确认产物，也不改动它们。
2. **重复答复 B/C**：`close_topic` 之后结束整轮、或增加 `finish` 标志，都不做。一轮内多主题是设计，A 只补上下文，不改轮次边界。
3. **跨会话产物扫描**：`list_workspace` 在本会话为空时提示磁盘上的其他运行目录，属于确认点那一条的配套，本轮不做。
4. **收束阶段连续被拒后强制停**：`ses_0027` 在第 21、22、24 步重试 `hypothesis` 白烧 3 步，这一条留到后续单独评估。

## 七、遗留风险

- 确认点仍然只靠提示词约束。下一次仍可能出现「没有确认就写盘」，本轮接受这个风险。
- 3.1 的承接块会让每一步的 `TASK` 层增加约 300–500 token；如果以后把上限放宽到 2000 字，需要重新量成本。
- 3.5 的采样会丢掉同一部件的中间帧与最后一帧；这些帧不带正文，但如果有分析依赖「最后一次 `part.update` 的时刻」，需要改读 `STEP_FINISH` 的时间。

## 八、实施状态（2026-11-11）

| 项 | 状态 | 落点 |
| --- | --- | --- |
| 3.1 答复承接 A | 已实施 | `models.py` 的 `Session` 字段、`event_log.py` 的 `_last_reply_text` / `_carry_text`、`context.py` 的 `reply_carry` |
| 3.2 收尾答复 | 已实施 | `loop.py` 的 `WRAPUP_INSTRUCTION` |
| 3.3 逐块计量 | 已实施 | `context.py` 的 `_task_header` / `_compose` |
| 3.4 假设 group | 已实施 | `hypothesis.py` 的 `_group`、`models.py` 的 `discriminator` |
| 3.5 事件日志 | 已实施 | `trace.py` 的缓冲与采样、`file_log.py` 的轮转 |
| 3.6 兜底标题 | 已实施 | `event_log.py` 的 `_TOOL_LABELS` / `_artifact_label` |
| 3.7 结构数来源 | 已实施 | `uniprot.py`、`campaign.py` |
| 3.8 计划路径 | 已实施 | `loop.py` 的 `_finish` |
| 3.9 内部用词 | 已实施（提示词） | `prompt.py` 的 `PRESENT_DISCIPLINE`；工具输出保留编号供 `update` / `close`，不再在用户答复里展开 |
| 3.10 多族装载 | 已实施 | `toolset.py` 的 `families` |

回归结果：

- `tests/test_agent.py` 全量通过。
- `tools/check_prefix.py` 通过：固定段 1686 token，整段前缀 5230 token。
- `tests/test_smoke.py` 首次运行在「流式文本合并之后再解析」断言处失败，重跑通过。该断言在界面层，本次没有改动界面代码，按既有改动留下的偶发失败记录。
- `tools/check_style.py` 未通过：`web/app.css:1307` 字号写成 10px。这一行属于工作区里原有的网页改动，不在本次范围内。

