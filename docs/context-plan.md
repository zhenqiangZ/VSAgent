# 结构化上下文实施计划（ContextBundle）

> 配套文档：`context-design.md`（设计）、`memory-design.md`（`MEMORY` 层的数据来源）、`memory-plan.md`（记忆第 1 期已完成）。
> 本计划只讲**怎么实施**：分期、任务、依赖、验收、回归。

---

## 一、目标与范围

**目标**：把 `context-design.md` 的分层方案写成代码，**先交付第 1 期（正确性）**。

**第 1 期范围**
- 新增 `opencode_tui/core/agent/context.py`：`Layer` / `LAYER_POLICY` / `Turn` / `Evidence` / `ContextBundle` / `ContextAssembler`
- **交互组切分**：`tool_call` 与它的结果以 `Turn` 为单位原子进出
- **前缀稳定分离**：`stable_prefix()`（身份/纪律/规则）与 `task_header()`（记忆/任务状态/摘要/引用）分开；`workspace.state_text` 的当前时间移出 `PREFIX`
- **`MEMORY` 层归位**：把已接入的 `memory_text` 从「直接拼在 `system` 之后」改到 `PREFIX` 之后，纳入统一预算与降级链
- **统一预算**：扣 `tools_tokens` 与 `output_reserve`，装不下报错
- **五级降级链**
- 接入 `loop.py` 两处装配点

**明确不做**（留给后续期）
- `TaskState` 结构化（第 2 期）
- 按 Part 类型的选择性压缩（第 3 期）
- `EVIDENCE` 层与子代理（第 4 期）
- 工具定义按相关性发子集

---

## 二、前置约束（项目约定，必须遵守）

| 项 | 约定 |
| --- | --- |
| 解释器 | 跑应用、跑断言、跑判据一律 `.venv/Scripts/python.exe`；打包、抓帧用 pack python |
| 改动后流程 | `test_agent.py` → `test_smoke.py` → `check_banned_chars.py` → 判据四段 → 打包 → 成对抓帧 |
| 判据 | 放 `.probe/`，自检要能证明判据本身有效；登记进 `run_all_checks.sh` |
| 跑判据前 | 先查孤儿进程，避免陈旧锁与残留状态文件造成假失败 |
| 抓帧 | 七帧都要抓（`verify_exe.py` 六帧 + `verify_modal_transparency.py` 一帧），少跑一侧会假失败 |
| 前缀布局改动 | 会改消息区行数，**必须重抓七帧**；`/help` 那帧不按 `End` |

---

## 三、设计要点回顾（决定实现形状）

1. **交互组原子**是全文唯一的**正确性**问题：`tool` 结果必须与它的 `tool_call` 同组，否则长会话里出现孤儿消息，部分服务端返回 400。
2. **前缀字节稳定**：`system` 只放身份/纪律/规则，**不放时间、不放进度**，才能命中 prompt cache。
3. **`MEMORY` 层已接入**（记忆第 1 期），第 1 期只**归位**，不重做数据结构。
4. **预算硬约束**：`available = window - output_reserve - tools_tokens`；装不下报装配失败并带各层用量，**绝不静默截断**。
5. **`EVIDENCE` 沿用 Ledger 的 handle 模式**：`ref` + `summary` 常给，`detail` 按预算展开。

---

## 四、分期计划

### 第 1 期 · 正确性（核心，本计划主体）

任务清单见 `TaskList`（C1-*）。分三组：

**A. 数据结构（C1-1 ~ C1-2）**
- 新增 `opencode_tui/core/agent/context.py`：`Layer` / `LAYER_POLICY` / `Turn` / `Evidence` / `ContextBundle`
- `Turn` 的切分：`prompt._translate_message` → 按 `STEP_START` 切出 `list[Turn]`，保证 `tool_call` 与结果同组

**B. 装配器（C1-3 ~ C1-5）**
- 前缀稳定分离：`stable_prefix()` / `task_header()`；`workspace.state_text` 的时间移出 `PREFIX`
- `ContextAssembler.build`：按 `PREFIX → MEMORY → TASK → RECENT → EVIDENCE → HISTORY` 装配；`MEMORY` 层复用 `memory_text`
- 统一预算（扣 `tools_tokens` + `output_reserve`）与五级降级链；装不下报错

**C. 接入与验证（C1-6 ~ C1-8）**
- `loop.py` 两处装配点改用 `ContextAssembler.build`
- `.probe/check_context.py`：判据脚本（含自检）
- `tests/test_agent.py` / `tests/test_smoke.py` 用例 + `docs/source-map.md` / `HARNESS.md` 同步

**第 1 期验收判据**
1. 任一 `tool` 结果都能在装配结果里找到同组的 `tool_call`，**无孤儿**（长会话构造用例）。
2. 同一会话内 `PREFIX` 的字节内容**不随时间/进度变化**（连续两步装配的 `system` 字节相等）。
3. 装配结果估算 token ≤ `context_window - output_reserve - tools_tokens`；超限时**报错**而非截断。
4. `MEMORY` 层内容 ≤ 2k，且位置在 `PREFIX` 之后、`TASK` 之前。
5. 被移出的交互组在 `HISTORY` 引用清单里各有一行。
6. 既有测试全部不回归（`test_agent` / `test_smoke`）；七帧抓帧一致。

### 第 1 期 · 实施结果（2026-10-07 完成）

C1-1 ~ C1-8 已实施并验证：

| 项 | 位置 | 验证 |
| --- | --- | --- |
| 数据结构 | `opencode_tui/core/agent/context.py` | `check_context` 各节 |
| 交互组切分 | `context.split_turns` | 判据第 1 条 + 自检（切散必报孤儿） |
| 前缀稳定分离 | `prompt.stable_prefix` | 判据第 2 条；`build_system_prompt` 输出逐字节不变 |
| 装配器 | `context.ContextAssembler.build` | 判据第 3~7 条 |
| 接入 | `loop._assemble` / `_run_steps` / `_ask_wrap_up` | `test_agent` 循环层 |
| 判据 | `.probe/check_context.py` | 已登记进 `run_all_checks.sh`（61 → 62 条） |
| 文档 | `source-map.md` §3.2 / §3.11 / §六、`HARNESS.md` | 已同步 |
| 打包 | `dist/opencode-tui.exe`（15:01） | `PYZ-00.toc` 含 `agent.context` / `.prompt` / `.loop` |
| 抓帧 | 七帧成对一致 | 冻结侧 `reply` 帧含真实回复，即新装配器在产物里跑过 |

读数：`test_agent` 1107 项、`test_smoke` 1627 项全绿；判据 62 条全绿；`check_banned_chars` 0 处；
`check_context` 退出码 0（自检：把交互组切散必须报出孤儿）；`compare_frames` 七帧全部一致。

**与原计划的差异**

1. `_translate_message` **没有**改返回类型，改为在 `context.split_turns` 里按「assistant 项开头、
   紧随其后的 tool 项归它」重新分组——这样 `summary_messages` 那条路不用跟着动，
   交互组原子也不靠事后检查，而是由构造保证。
2. `task_header` 归入装配器（`ContextAssembler._task_header`）而不是 `prompt.py`：
   它是输出布局的一部分，与各层内容耦合。
3. `prompt.build_model_messages` / `BuiltContext` / `_shrink_kept` 整条旧装配路径**删除**，
   装配归 `context.py`。`KEEP_MESSAGES` 保留，但它现在只管**手动压缩**保留几条。
4. 预算口径：`available = int(window * ratio) - min(output_reserve + 协议开销, window // 4) - tools_tokens`。
   窗口很小时预留按窗口四分之一封顶，否则前缀都放不下。

**接入后改掉的既有行为**（`test_agent` 已同步）

- 小窗口由「静默截断」改为**报 `ContextAssemblyError`**；循环层捕获后写一条 ERROR Part
  并以 `EXIT_ERROR` 走收尾总结。`COMPACT_WINDOW` 2600 → 16000、
  `check_summary_compaction` 的窗口 2000 → 20000。
- `RECENT` 改按软配额（剩余预算 50%），不再沿用「最近 6 条消息」。
- 摘要那一条的断言由「正文 == 摘要」改成「以摘要开头」——后面还跟着引用清单。

### 第 2 期 · `TaskState` 结构化

- `TaskState` dataclass + `Session.task_state` 字段，随状态文件原子写出
- 更新路径：模型工具 / 命令写入 `goal / constraints / done / todo / blockers`；渲染进 `TASK_HEADER`
- 第 2 期判据与测试

#### 第 2 期 · 实施结果（2026-10-07 完成）

| 项 | 位置 | 验证 |
| --- | --- | --- |
| 数据结构 | `core/models.py`：`TaskState` + `Session.task_state` | `test_agent::check_task_state`、`check_task_state` 第 1~3 条 |
| 更新语义 | `TaskState.apply` / `.add` / `.clear` | 判据第 2 条 |
| 写盘 | `Session.to_dict` / `from_dict` → `SessionManager.save_to_toml` | 判据第 3 条（含不留 `.tmp`） |
| 模型那条路 | `core/agent/task.py::TaskTool`，`task_argument = "state"` | 判据第 6 条、`test_agent` |
| 用户那条路 | `/task`（`app.ARGUMENT_COMMANDS` 已加）、`command_registry` 一条 | `test_smoke`、`check_command_coverage` |
| 进 TASK 层 | `loop._task_text` → `ContextAssembler.build(task_text=)` | 判据第 4 条 |
| 判据 | `.probe/check_task_state.py` | 已登记进 `run_all_checks.sh`（62 → 63 条） |
| 打包 | `dist/opencode-tui.exe`（18:36） | `PYZ-00.toc` 含 `agent.task` / `agent.context` / `core.models` |
| 抓帧 | 七帧成对一致 | 与第 1 期同法 |
| 产物功能 | `.probe/capture_frozen_task.py` | 冻结产物与源码两侧都通过：`/task` 真写进了状态文件 |

读数：`test_agent` 1208 项、`test_smoke` 1744 项全绿；判据 63 条全绿；`check_banned_chars` 0 处；
`check_command_coverage` 41 条命令全覆盖（含新增的 `/task`）；`compare_frames` 七帧全部一致。

> 七帧一致只覆盖**画面**。`/task` 这种只改数据的功能画面几乎不动，两侧看不出区别，
> 所以另加 `.probe/capture_frozen_task.py`：在 ConPTY 里起产物、打一条命令，
> 再读它写出来的状态文件。**这条是本期的产物侧凭证，别只报「七帧一致」。**

**两条字段语义刻意不同**（这是本期的核心决定，别顺手改齐）：

- `constraints` 是**追加**。约束是用户提的要求，模型每轮重写一遍的话，漏抄一条就等于
  把它删了——而这正是 `TASK` 层存在的理由。要删只能由用户走 `/task clear`。
- `done` / `todo` / `blockers` 是**整段替换**。这三类是模型自己的进度记录，随进度重写才对。

**与设计稿的一处差异**：`TaskState` 归入 `core/models.py` 而不是 `context.py`——
`context.py` 已经从 `models.py` 导入 `Session`，反过来放会成环。装配器只把它当一段文本收。

**工具层的改动**：`Tool` 新增 `task_argument` 通道（与 `context_argument` 并列）。
两者分开是因为内核从不同地方取：`context` 是循环层持有的登记表、会话切换时整体换掉；
任务状态挂在会话自己身上、每次调用现取。挤进同一个口子的话，取登记表的那些工具会
收到一个既不是登记表也不是任务状态的东西。

### 第 3 期 · 选择性压缩 + 统一预算

- 按 **Part 类型**选择性压缩，替换 `_shrink_kept` 的字符位置三级削（保决策、结论、约束、报错原文）
- 统一预算与装配失败上报（`ContextBundle.degraded` 可观测）
- 第 3 期判据与测试

#### 第 3 期 · 实施结果（2026-10-07 完成）

| 项 | 位置 | 验证 |
| --- | --- | --- |
| 压缩原语 | `core/agent/prompt.py`：`compress_tool_output` / `is_key_line` / `ERROR_MARKERS` / `RESULT_MARKERS` | `check_compression` 第 1、4 条 |
| 翻译层改用同一套 | `prompt.truncate_for_model` | `check_compression` 第 2 条、`test_agent::check_selective_compression` |
| 装配器按 Part 类型逐级压 | `context.ContextAssembler._degrade_recent` | 判据第 3 条（`recent_reasoning` → `recent_output` → `recent_output_body`） |
| 统一预算 | `context.Budget` + `ContextAssembler.budget`，各层按 `Budget.share` 取软配额 | 判据第 6 条 |
| 降级有序可观测 | `context.DEGRADE_ORDER` / `ordered_degraded` | 判据第 5 条（顺序 + 级别名都在表里） |
| 削过就记进 `degraded` | 软配额削的也算（`memory` / `history_summary` / `evidence_detail`） | 判据第 6 条 |
| 装配失败上报 | `ContextAssemblyError` 消息带各层用量（`describe_usage`） | 判据第 7 条 |
| 事件可观测 | `loop._trace_context` → `context.assembled`（真削过才发） | `test_agent::check_selective_compression` 第七节 |
| 判据 | `.probe/check_compression.py` | 已登记进 `run_all_checks.sh`（63 → 64 条） |
| 打包 | `dist/opencode-tui.exe`（19:31:54，15,516,996 字节） | `PYZ-00.toc` 含 `agent.prompt` / `agent.context` / `agent.loop`（agent 子模块共 13 个都在） |
| 抓帧 | 七帧成对一致 | 本期只动数据，画面应当一个字不变——一致本身就是结论 |
| 产物功能 | `.probe/capture_frozen_task.py` | 冻结产物与源码两侧都通过：`/task` 真写进了状态文件 |

读数：`test_agent` 1502 项、`test_smoke` 2038 项（界面层 536 + 循环层 1502）全绿；判据 64 条全绿；
`check_banned_chars` 0 处；
`compare_frames` 七帧全部一致（`home` 与弹窗区逐行一致，其余关键内容齐全、非空行数相等）；
`capture_frozen_task` 两侧都通过。

**本期最实际的收益在翻译层，不在装配器**：`truncate_for_model` 原先按字符位置留首尾，
一段 14000 字符的输出里，中段那条报错**在装配器看到它之前**就被切掉了——
后面再怎么按内容挑也救不回来。改用同一套压缩之后，同一段输出从 4015 字符降到 190 字符，
而报错原文与结论行都在里面。压缩原语因此下沉到 `prompt.py`，两处共用。

**装配器那一级其实很难走到**：降级链第 4 步是整组移出，比压组内输出更早生效，
所以只有「只剩一组可保留、而这一组自己就装不下」时才会压。判据要专门造这个形状
（一组、且工具输出正好压在翻译层上限之下），否则那几条断言是空转的。

**两处刻意不压**：助手正文是决策与结论、`tool_calls` 是调用记录，两者都不在可压的
两类里。这不是靠自觉——`_degrade_recent` 只认 `reasoning_content` 与 `role=tool` 的
`content` 两处字段，别的想压也压不到。

**没有行结构时不硬挑**：整段只有一行（例如 minify 过的 JSON）时 `compress_tool_output`
返回 None，由 `truncate_for_model` 按位置留首尾兜底。没有行结构就没有内容可挑，
硬按字符切反而会把唯一那点信息切碎。

**没做的一件**：`degraded` 还没有显示到状态栏上。设计稿第六节提到「供 trace 与状态栏显示」，
本期只做了 trace（`context.assembled` 事件）。状态栏加一段会让七帧抓帧全部失配，
而本期改的是数据、画面本应一个字不变——把「七帧一致」这条干净凭证留到下次动界面时再用。

> 本期同样是「只动数据、画面看不出来」的一类，所以按 §10.5 补跑了
> `.probe/capture_frozen_task.py`（两侧各一遍）。它跑一条完整模型轮次（会走到新的
> 装配与 `_trace_context`），再去读状态文件——七帧一致只能证明画面没变，
> 证明不了新装配路径在冻结产物里没崩。

### 第 4 期 · 证据层接入（C4-1，已完成）

> 子代理上下文隔离（C4-2）**本期不动**，先只做证据层这一半。

**做了什么**：给第 3 期就建好的 `EVIDENCE` 层接上来源。层的装配、软配额、降级链
（`evidence_detail`）在第 3 期已经写好，缺的一直是「谁往里放东西」——
`build(evidence=...)` 从来没有被传过实参。

| 项 | 位置 | 内容 |
| --- | --- | --- |
| 收集器 | `context.py` | `EvidenceStore`：条数滚动（`EVIDENCE_LIMIT=12`）、同引用替换、`add_from_part` 提炼 |
| 提炼口径 | `context.py` | 复用第 3 期的 `is_key_line`：含关键行的工具结果才收；失败的那一类保底收 |
| 注入通道 | `tools.py` | `Tool.evidence_argument`，与 `context_argument` / `task_argument` 并列 |
| 记忆进层 | `memory.py` | `RecallTool` 把命中塞进收集器（它仍不声明 `context_argument`） |
| 循环接线 | `loop.py` | `_execute_tool` 跑完提炼、`_assemble` 带上、两个入口各清空一次 |
| 判据 | `.probe/check_evidence.py` | 七节（含反空转自检） |
| 测试 | `tests/test_agent.py::check_evidence_layer` | 七节 |

**两个来源，不是三个。** 设计稿把来源写成「工具结果 / Ledger / `recall`」，
本期接的是其中两类：**检索回来的记忆**与**含关键行的工具结果**。

- 检索回来的记忆：跨会话的知识，`RECENT` 里本来就没有它。不给证据层一份，
  下一步 `RECENT` 一被压缩，这段知识就跟着没了；
- 含关键行的工具结果：报错、结论、回溯那几行。它的价值在于 `RECENT` 被压缩
  或被移出之后，这几行仍然在上下文里。

**Ledger 的产物详情没有接**：`list_workspace` 已经能列，而产物详情的体积不可控，
收进来容易把证据层的额度吃光。与第 3 期「没做的一件」同样处理。

**为什么不收每条工具输出。** 收了的话证据层就是 `RECENT` 的复制品：同一段文字
发两遍，还挤掉真正要留的那几行。判据里有一条专门盯这个（纯过程行的输出必须
一条都不收），自检则把 `is_key_line` 换成永远为真，要求那一条报出来。

**同一条引用是替换不是追加。** 引用名 = 工具名 + 定位参数（按 `EVIDENCE_REF_ARGUMENTS`
的顺序找第一个非空字符串）。同一个文件读三次占一条，不是三条。

**证据是「一轮」的**：`stream_reply` 与 `run_bash` 开头各清空一次。上一轮 `recall`
出来的记忆，到这一轮多半已经不相干，留着只会占证据层的额度。

**提炼排在 POST_TOOL_USE 钩子之后**：钩子可能把这一条改成失败，那时它更该进证据层。
判据里那条「失败的保底收」量到的正是这个顺序。

**没有动画面。** 与第 3 期同类：证据只进请求体，界面上一个字都不变。
因此按 §10.5 同样补跑 `.probe/capture_frozen_task.py`（两侧各一遍）。

| 项 | 结果 | 凭证 |
| --- | --- | --- |
| `test_agent` | 1556 项全绿（+54） | |
| `test_smoke` | 2092 项全绿（界面层 536 + 循环层 1556） | |
| 判据 | 65 条全绿 | `.probe/check_evidence.py` 新增 |
| 禁用字 | 0 处（304 文件） | |
| 打包 | `dist/opencode-tui.exe`：2026-10-07 21:31:49，15,521,616 字节（旧产物归档到 `.probe/opencode-tui-20261007d.exe`） | `PYZ-00.toc` 含 `agent.prompt` / `agent.context` / `agent.loop` 等 13 个子模块 |
| 抓帧 | 七帧成对一致 | 本期只动数据，画面应当一个字不变——一致本身就是结论 |
| 产物功能 | 两侧都通过 | `.probe/capture_frozen_task.py`（会走到新的提炼与装配） |

### 第 4 期 · 子代理上下文隔离（C4-2）

> 设计见 `subagent-design.md`（问题、边界、接口、不变式）。这里只讲怎么实施。
>
> 一句话：给主代理一个 `delegate` 工具，把自包含的子任务交给一个**独立会话**的子代理去跑，
> 只把结论回灌。隔离的边界就是「另一个 `Session`」——子代理的中间过程
> 从头到尾没进过主会话，因此也就不需要任何「事后清理」。

**改动清单**

| 项 | 位置 | 内容 |
| --- | --- | --- |
| 委派工具 | `core/agent/subagent.py`（新） | `DelegateTool`：`task` / `background` / `agent`；`subagent_argument = "run_subagent"` |
| 注入通道 | `core/agent/tools.py` | 第四个通道 `subagent_argument`；`SubagentReport` 也放这里（两层都要引用，放循环层会成环） |
| 子代理入口 | `core/agent/loop.py` | `run_subagent(...)`：建临时会话、换证据收集器与步数/窗口、复用 `_run_steps`、`finally` 还原 |
| 装配参数化 | `core/agent/loop.py` | `_assemble` 收 `tools=`、`_run_steps` 收 `max_steps=`，主代理走默认值 |
| 角色 | `core/agent/prompt.py` + `loop.py` | `explore`：身份说明多一个分支；工具清单去掉 `PLAN_DENY ∪ {delegate}` |
| 权限冒泡 | `loop.py` + `screens/chat.py` | `permission.ask` 补 `tool` / `argument` / `agent`；界面优先读事件、回退查 Part |
| 收工误判 | `screens/chat.py` | `_on_turn_done` 补会话判断（防将来给子代理加 `_finish`） |
| 装配 | `opencode_tui/app.py` | `_build_registry` 注册 `DelegateTool`；`permission.PLAN_DENY` 加 `delegate` |
| 判据 | `.probe/check_subagent.py` | 七节（含反空转自检） |
| 测试 | `tests/test_agent.py::check_subagent_isolation` | 七节 |
| 文档 | `source-map.md` / `HARNESS.md` | 新增文件与通道；判据清单 65 → 66 条 |

**风险与对策**

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 子代理的事件漏进主界面 | `_busy` 提前置假、卡片错位 | 子代理走 `_run_steps`、不发 `turn.done`；界面那一条仍补会话判断 |
| 权限询问被静默丢掉 | 子代理停在等答复上直到超时 | 事件里带上 `tool` / `argument` / `agent`；判据专门盯这一条 |
| 递归委派 | 步数、耗时、费用不可控 | 深度上限 1：子代理的工具清单里没有 `delegate` |
| 结论过长 | 回灌把主上下文撑满 | 按 `truncate_for_model` 压；角色说明要求只写结论 |
| 换掉的运行时状态没还原 | 主代理下一轮用了子代理的证据与步数 | 全部在 `finally` 里还原；判据断言委派前后主代理的 `evidence` 与 `max_steps` 未变 |

**验收判据**：`subagent-design.md` 第九节那七条。

**这一期会动界面。** 与第 3 期、C4-1 不同，权限冒泡要改 `screens/chat.py` 的两处，
所以「七帧一致」这条凭证的性质变了：画面**应当仍是一致**（假后端不会调 `delegate`），
但一旦假后端或抓帧脚本碰到委派那条路，就要重抓并按新画面定基线。

---

## 五、依赖与执行顺序

```
C1-1 数据结构（context.py）
  └─ C1-2 交互组切分（Turn）
       ├─ C1-3 前缀稳定分离（stable_prefix / task_header）
       └─ C1-4 装配器（按层装配 + MEMORY 归位 + 预算）
            └─ C1-5 降级链
                 └─ C1-6 接入 loop.py
                      └─ C1-7 判据脚本
                           └─ C1-8 测试与文档
```

**可并行**：C1-3（前缀分离）与 C1-4（装配器）在 C1-2 之后可并行。
**串行关键路径**：C1-1 → C1-2 → C1-4 → C1-5 → C1-6 → 测试链。

第 4 期的 C4-1（证据层接入）挂在 C1-4 与 C1-5 之后：它要的是「层已经能装、
降级链已经能收正文」这两件事先成立，剩下的是给它接来源。
`C4-2`（子代理上下文隔离）动的是循环层的分支结构，与 C4-1 无先后依赖；
它只借「装配器以 `Session` 为单位」这一条，不借证据层，设计见 `subagent-design.md`。

---

## 六、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 交互组切分错 | 孤儿消息，服务端 400 | 以 `STEP_START` 为唯一切点；判据构造长会话用例 |
| 前缀被易变内容污染 | prompt cache 全失效 | `system` 只放稳定段；判据比对连续两步字节 |
| 装配改动影响既有断言 | `check_prompt` 假失败 | 先读既有断言再改；同步更新期望 |
| 预算估算过小 | 实际超窗被截断 | 估算留 `PROTOCOL_OVERHEAD`；超限报错不截断 |
| `MEMORY` 层重复注入 | 记忆出现两处 | 归位时删掉旧的「直接拼在 system 之后」路径 |
| 前缀布局改动 | 抓帧七帧失配 | 改完必重抓七帧，`/help` 帧不按 `End` |
| 证据层收得太宽 | 成了 `RECENT` 的复制品，白占额度 | 只收含关键行的工具结果与检索回来的记忆；判据盯「纯过程输出一条都不收」 |
| 证据跨轮留着 | 上一轮的材料挤占这一轮的额度 | 两个入口（`stream_reply` / `run_bash`）各清空一次 |
| 子代理的隔离没做到 | 中间过程漏进主会话，主上下文照样被撑满 | 子代理用独立 `Session`；判据盯「主会话只多一条消息」（详见 C4-2 一节） |
| 测试回归 | 既有功能破坏 | 严格走固定流程；先 `test_agent` 再 `test_smoke` |

---

## 七、里程碑

| 里程碑 | 内容 | 交付物 |
| --- | --- | --- |
| C-A | 数据结构可用 | `context.py` + `Turn` 切分 |
| C-B | 装配器可用 | `ContextAssembler.build`（含 `MEMORY` 层） |
| C-C | 接进循环 | `loop.py` 两处装配点切换 |
| C-D | 第 1 期完成 | 全部判据通过 + 文档同步 + 打包抓帧 |
| C-E | 第 3 期完成 | 选择性压缩 + 统一预算；`check_compression` |
| C-F | 第 4 期 · C4-1 完成 | 证据层接上来源；`check_evidence` + `check_evidence_layer` |
| C-G | 第 4 期 · C4-2 完成 | 子代理独立上下文；`check_subagent` + `check_subagent_isolation` |

---

## 八、一句话

> 先交付「交互组原子 + 前缀稳定」——它是唯一一条不改就会在长会话里出错的正确性问题；
> `MEMORY` 层已经在，第 1 期只是把它摆到正确的位置、纳入统一预算，别重做。
>
> 第 4 期给 `EVIDENCE` 层接来源时守住一条：**只收挑过的关键行，不收整份输出**。
> 这一层的价值在「索引还在」——`RECENT` 被压缩或被移出之后，那几行仍然看得见；
> 内容更全不归它管，全量收进来只是把同一段文字发两遍。
