# 意图识别与任务接线

日期：2026-11-17

这份文档说明一件事：用户的一句话怎样变成任务规格、角色计划与一次具体派发。

## 一、一条完整路径

```text
用户这一句
  → Principal 第一步调用 route_intent
      Context Resolver：当前任务 + 科研对象表 + 产物表 + 待澄清问题
      五个维度 + 两条轴 + 结构化对象引用
  → Intent Validator
      accept / clarify / update / reject
  → Task Context Resolver
      new / continue / modify / compare / resume / status / cancel / clarify
  → Task Compiler
      IntentSpec → TaskSpec
  → Agent Router
      TaskSpec → AgentPlan
  → Principal
```

对应文件：

| 环节 | 文件 |
|---|---|
| 上下文解析 | `planning/context_resolver.py` |
| 意图工具 | `planning/route_tool.py` |
| 意图提示词 | `planning/intent_prompt.py` |
| 意图验证 | `planning/intent_validator.py` |
| 任务关系 | `planning/task_context.py` |
| 任务编译 | `planning/task_compiler.py` |
| 角色路由 | `planning/agent_router.py` |
| 协议 | `roles/contracts/intent.py`、`plan.py`、`task.py` |

## 二、意图识别看到什么

意图由主控在每轮第一步调用 `route_intent` 完成，所以它看到的是本轮装配给主控的
上下文：用户这一句、当前任务、对象与产物表、待澄清问题，以及主控本来就有的历史
与规则。`ContextResolver` 仍拼一份现场摘要供验证器使用；工具参数只要求五个维度
与两条轴，不要求模型复述整段历史。

模型给出的是结构化参数，不是自由文本。写了不认识的字段或取值时，参数检查与
`IntentSpec` 会当场退回，下一步在同一轮里重填。

## 三、五个维度

| 字段 | 含义 |
|---|---|
| `goal` | 用户最终想解决什么问题 |
| `objects` | 涉及的科研对象，每项带类型、名称、引用与来源 |
| `operations` | 要哪些子 Agent，取值就是九个角色名 |
| `constraints` / `exclusions` | 用户明确提出的要求与禁止 |
| `expected_outputs` | 用户希望拿到什么 |

另外保留 `inputs`，记录用户已经给出的靶点、结构、口袋、配体集等。
它直接决定要不要补前置角色。

## 四、两条轴

### 4.1 任务关系 `task_relation`

| 取值 | 含义 | 处理 |
|---|---|---|
| `new` | 新任务 | 新任务号，编译并路由 |
| `continue` | 延续当前任务 | 沿用任务号；只补约束不重编译，出现新角色才重编译 |
| `modify` | 改目标或对象 | 沿用任务号，版本加一，保留已有产物 |
| `compare` | 比较两组对象或结果 | 沿用任务号，版本加一，补 `analysis`，不重跑上游 |
| `resume` | 恢复以前的任务 | 找回原任务号与产物 |
| `none` | 与任务无关 | 不建任务 |

### 4.2 对话行为 `conversation_action`

| 取值 | 含义 | 处理 |
|---|---|---|
| `execute` | 执行或继续执行 | 编译与路由 |
| `status_query` | 问进度 | 不编译，只读任务状态与战役状态 |
| `ask_result` | 问结果说明什么 | 不编译，只读任务规格、报告与产物 |
| `clarify` | 回答上一轮问题 | 合并待澄清意图，再验证一次 |
| `cancel` | 终止任务 | 任务状态改为 `cancelled`，停止战役 |
| `chat` | 闲聊或概念问答 | 不建任务 |

两条轴分开之后，“问上一轮结果”不会再被当成新任务，比较也不会把已有对象丢掉。

## 五、意图验证

验证器是纯规则，不调用模型。检查六件事：

| 检查项 | 内容 |
|---|---|
| 形状 | 目标非空；执行类有操作；操作都在九个角色里 |
| 引用 | 已有对象必须能在对象表或登记表里找到 |
| 完整性 | 按操作查必要输入；发现类任务不因缺编号而追问 |
| 一致性 | 操作不能同时出现在禁止项里 |
| 可执行性 | 角色与模板存在 |
| 授权 | 需要确认的操作有确认点 |

四种结果：

| 结果 | 处理 |
|---|---|
| `accept` | 继续编译与路由 |
| `clarify` | 保存待澄清意图，先把问题问清楚 |
| `update` | 解析有效，但必须先更新任务规格 |
| `reject` | 说明原因，不编译、不派发 |

规则层查不了真正的语义正确性。默认不做模型复核；需要时只在 `enforce` 模式下，
对置信度低或操作很多的结果可选复核一次。

## 六、任务规格与角色计划

`IntentSpec` 只表达用户意图，不直接当任务书。`TaskCompiler` 把它编译成 `TaskSpec`：

- 目标、范围、可用输入；
- 交付物与验收条件；
- 确认点；
- 子目标与负责角色。

每个子目标带 `agent` 与可选的 `stage`。`AgentRouter` 按角色补齐前置、排依赖、
算并行组，产出 `AgentPlan`。计划里只有角色、依赖、未决输入与确认点，没有能力名。

计划到任务书之间只有一次投影：`execution/projection.py` 的 `project()` 把
`TaskSpec` 与 `AgentPlan` 合成每个角色的 `RoleSlice`，再由 `RoleSlice.to_task()`
生成 `AgentTask`。交付物认领、约束分发、子目标归属、验收条件裁剪都在这一处，
`coordinator.build_tasks()` 只是它的两元组包装。模板本身在 `roles/templates.py`，
是冻结数据类，字段名与 `TaskSpec` 对齐。

## 七、哪些东西进入模型上下文

- `session.task_spec`：任务合同，下一轮判延续、修改与比较的依据。
- `session.task_state`：模型维护的进度，进 TASK 层。
- `session.route_note`：本轮路由提示，只在内存里，进 TASK 层。
- `session.pending_intent`：上一轮没问完的问题，下一轮解析时带回现场。

`PartType.ROUTE` 只给界面看，`translate_parts()` 不翻译它，所以意图解析结果与计划
不会原文进入模型上下文。模型看到的是任务合同、进度与本轮路由提示。

## 八、一轮对话里，子 agent 的调用取决于什么

一句话：**模型决定要不要派，任务规格决定派谁，前置与确认点决定能不能真派，调度器决定怎么跑。**

### 8.1 路由层不会自动派发

执行类请求里，`_prepare_route` 最多做四件事：

1. 解析这一句话；
2. 验证意图；
3. 写 `session.task_spec`；
4. 算一份 `AgentPlan`，写进本轮的过程记录。

查进度、问结果、取消、闲聊不做第 3、4 步，只写本轮 `route_note`。

它不会调用 `dispatch_campaign`。真正创建子会话的入口只有一个：主控模型调用
`dispatch_campaign` 工具。这个工具只装在根会话的编排档里，子会话的工具名单没有它；
子会话的深度守卫也会拒绝再次派发。所以子 agent 不能再创建子 agent。

`TaskSpec` 与 `AgentPlan` 都不原文进入模型上下文。模型看到的是任务状态（目标、
约束、待办、阻塞）、由 `task_spec` 推出来的阶段规则，以及本轮 `route_note`。
完整任务规格由工具在服务端读取；计划预览要等模型调用工具之后才拿到。

### 8.2 模型会不会调用派发工具

取决于五件事：

| 条件 | 说明 |
|---|---|
| 系统提示 | `PRINCIPAL_CAMPAIGN` 要求：任务可独立执行、输入明确、依赖可表达、确实需要专业角色时才派；一次能做完的简单工作不拆；先 `confirm=false` 预览，用户同意再 `confirm=true` |
| 用户这一句 | 执行类请求才可能需要派发；查进度、问结果、取消、闲聊都不派 |
| 任务状态与本轮提示 | `route_note` 会写“本轮只回答进度，不要派发”“本轮只解释已有结果”等 |
| 工具说明 | 工具自己写明：一次调用能做完的事、或下一步要用到当前推理时，不要派；子 agent 看不到主控的推理 |
| 任务规格 | 没有 `session.task_spec` 时工具直接拒绝；规格读不出来也拒绝 |

还有一个硬条件：本轮被 `IntentValidator` 判为 `reject` 且路由处于 `enforce` 时，
`session.route_blocked_reason` 会被写上，`dispatch_campaign` 直接拒绝。

### 8.3 工具选哪些角色

选角色是机械计算，模型不逐个点名。顺序如下：

1. **从任务规格取基础角色**：`TaskSpec.subgoals[].agent`，去重并保持顺序。
2. **去掉被禁止的角色**：`exclusions` 经 `EXCLUSION_RULES` 映射成角色，写进
   `AgentPlan.denied_agents`。
3. **补必要的前置角色**（`AUTO_PREREQUISITES`）：
   - `prepare` 缺 `pocket`、`ligand` 时补上；
   - `docking`、`affinity` 缺 `prepare` 时补上。
4. **用户已经给出的输入不再补**（`ROLE_PROVIDED_INPUTS`）：
   - 给了 `pocket`，不再补 `pocket`；
   - 给了 `ligand_set`，不再补 `ligand`。
5. **`roles` 只能从计划里减**：
   - 只给一部分角色：`build_tasks` 会把这些角色的上游闭包一起带上，计划里其余角色
     记成“没派”；
   - 给了计划外的角色：直接报错，提示先改任务规格、下一轮重新解析再派；
   - 给了不存在的名字：直接报错。
6. **一个角色都没有**：工具报“这份任务规格里没有可派的角色”。

角色之间的依赖来自 `PREREQUISITES`：

```text
prepare    ← pocket、ligand
docking    ← prepare
affinity   ← prepare
mechanism  ← pocket、ligand、docking
analysis   无硬前置
visualization 等同计划内其他角色（汇点）
```

依赖决定谁先跑；互不依赖的角色进入同一个并行组。

### 8.4 是否真正启动

工具入口先查三件事，任何一件不成立都直接拒绝：

1. 当前会话是不是根会话（子会话不能再派）；
2. 本轮有没有被路由拒绝（`route_blocked_reason`）；
3. `session.task_spec` 在不在、能不能读出来。

通过之后，`confirm` 分两种：

| `confirm` | 行为 |
|---|---|
| `false` | 只返回预览：派谁、谁等谁、缺什么。不建会话，不跑计算；前置齐全时战役状态推进到“排计划” |
| `true` | 先过前置闸门，再检查是否已有一场在跑，然后真正启动 |

`confirm=true` 时再查两件事：

1. **缺前置产物**：`missing_prerequisites` 非空就拒绝。只判这次要派的角色，
   不拿计划里另一个分支的缺口拦这一场。
2. **是否已有一场在跑**：同一会话同时只能有一场；如果正在跑且计划已过期，
   提示先停下再按新规格发起。

通过之后，`host.start_campaign` 把 `Coordinator` 放到后台：

```text
AgentPlan
  → build_tasks：一个角色一份 AgentTask
  → AgentScheduler：按依赖分层
  → AgentDispatcher：建子会话、判重
  → 子会话跑完交 AgentReport
```

每份 `AgentTask` 带：

- `task_id`：`计划号:角色`，同一份计划重跑不会重复建会话；
- `role`：角色名；
- `objective`：只写这个角色负责的子目标；
- `background`：父任务目标，只作上下文，明确不要重复完成；
- `constraints`、`input_refs`：这个角色的限制（父 agent 在派发时写进
  `dispatch_campaign.constraints`，键是角色名或 `global`；不写不下发）、不做的事、缺的输入；
- `required_outputs`、`acceptance_criteria`：只挂给汇点角色；
- `dependencies`：上游任务号；
- `permissions`：角色说明书里的动作类别。

`dispatch_campaign(confirm=true)` 会一直等到所有子 agent 结束，再把每个角色的结论、
报告判定与产物带回；用户中断时这场执行连子会话一起停。

### 8.5 一轮里可能一个子 agent 都不派

以下情况都会让这一轮停在派发之前：

- 模型判断一次调用就能做完，不调用工具；
- 只调用了 `confirm=false` 预览；
- 路由判定为查进度、问结果、取消或闲聊；
- 路由判定需要澄清，先把问题问用户；
- `enforce` 模式下验证器拒绝；
- 没有任务规格，或任务规格读不出来；
- 计划里没有角色；
- 缺前置产物；
- 同一会话已经有一场在跑。

所以“有 AgentPlan”不等于“这一轮一定有子 agent”。

### 8.6 计划会重新计算，不复用上一轮的快照

`dispatch_campaign` 每次都从 `session.task_spec` 重新跑一遍 `AgentRouter.route`。
`_turn_plan` 只是本轮的过程记录，不参与派发。

这样做的后果：

- 预览与确认如果分在两轮，中间任务规格被修改，确认时派出的角色可能和预览不同；
- 任务规格是唯一合同，计划是它的计算结果；
- 计划戳记只在一场正在跑时用于判断“这份计划是不是过期了”。

### 8.7 决定者一览

| 环节 | 谁决定 | 依据 |
|---|---|---|
| 要不要派 | 主控模型 | 系统提示、用户这一句、任务状态、阶段规则、`route_note`、工具说明 |
| 派哪些角色 | `AgentRouter` | 子目标的 `agent`、禁止项、自动前置、用户自带输入 |
| 只派其中几个 | 主控模型 | `roles` 参数 |
| 能不能真派 | `DispatchCampaignTool` | 任务规格、前置闸门、战役状态、路由拒绝标记 |
| 怎么跑 | `AgentScheduler` | 依赖边、并行组、每个角色的时限 |
| 结果怎么取 | 主控模型 | `dispatch_campaign` 的返回（阻塞到全部结束） |
