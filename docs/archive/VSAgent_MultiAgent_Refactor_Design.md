# VSAgent 多 Agent 架构渐进式重构设计方案

> 版本：v1.0\
> 基准源码：`VSAgent-main (1).zip`（2026-11-12）\
> 目标：在保留现有 Agent 执行内核和领域工具的前提下，逐步将 VSAgent
> 从"模型自行理解任务并选择工具"的单体
> Agent，升级为"意图驱动、能力路由、专业 Agent
> 协作、证据闭环"的科研系统。

------------------------------------------------------------------------

## 0. 执行摘要

### 0.1 本次重构要解决什么

当前系统并非缺少工具，也不是缺少 Agent
执行能力。主要问题是：用户的自然语言目标、科研任务定义、能力选择、工具族加载和执行状态之间缺少一个稳定的结构化中间层。模型需要在较长的项目规则、较多的工具定义和历史上下文中自行完成这些决策，导致意图偏移、工具误路由、约束遗漏和任务交付不完整。

**进一步判断（2026-11-12 复核）：在中间层之前，还有一个更靠前的矛盾——每一步发给模型的信息量本身。**
按当前装配实测，每一步的固定开销是：常驻核心工具 8,552 token（删重复文字与拆族之后 7,818）、稳定前缀 5,286 token（分档之后 5,112：固定段 1,749 + 项目规则 3,346），而典型一轮实际装入的领域工具族只有 3,100–6,000 token。也就是说，**核心工具与项目规则两项合计，是领域工具定义的两到三倍**。

这一点决定了实施顺序。多 Agent 解决的是上下文隔离，不是信息量本身：把一份过长的规则分给九个 Agent，每个 Agent 仍然要读自己那一份；把一个会话的工具分给九个 Agent，每个 Agent 仍然要带自己的工具。如果单个 Agent 的每步固定开销不先降下来，多 Agent 只会把这个开销乘以 Agent 数量。

因此本次改造的执行顺序调整为三段：

1. **先减量**：只调装配，不加协议。核心工具按会话类型分档、项目规则按是否面向用户切段、把互相冲突的领域工具族拆开（Phase 0.5）。
2. **再约束**：让关键规则在运行时生效，而不是只写在提示词里（Phase 8，其中排除项与工具级限制应在 Phase 7 之后立即接入）。
3. **后多 Agent**：此时每个 Agent 的上下文已经在四千 token 量级，开三到五个不会失控，上下文隔离的收益才成立（Phase 9 之后）。

原方案的结构化中间层（意图、任务规格、能力路由）保持不变，位置从"第一步"后移到"减量之后的第一步"。它解决的是理解得对不对，减量解决的是看得过来不过来，两件事不能互相替代。

在架构层面，改造遵循以下顺序：

1.  先把用户问题解析成结构化意图；
2.  再把意图编译成具有输入、输出和验收标准的 TaskSpec；
3.  根据 TaskSpec 选择专业能力和工具族；
4.  在此基础上引入角色化专业 Agent；
5.  最后加强运行时规则、任务状态、证据验证和动态重规划。

### 0.2 总体架构

``` text
User Message
    |
    v
Intent Parser ------ Conversation / Task Context Resolver
    |
    v
IntentSpec
    |
    v
Task Specification Compiler
    |
    v
TaskSpec + TaskState
    |
    v
Capability Router
    |
    +------ Capability Registry
    |             |
    |             v
    |       Agent Profile / Tool Family Mapping
    |
    v
CapabilityPlan / Research DAG
    |
    v
Principal Agent / Orchestrator
    |
    +------ Structure & Pocket Agent
    +------ Conformation Agent
    +------ Ligand Intelligence Agent
    +------ Literature Agent
    +------ Virtual Screening Agent
    +------ Scoring Agent
    +------ Mechanism Agent
    +------ Hypothesis & Evidence Agent
    |
    v
Research Workspace + Agent Reports + Evidence
    |
    v
Runtime Validation / Scientific Review
    |
    +------ Replan
    +------ Clarify / User Approval
    |
    v
Verified Research Deliverables
```

### 0.3 重要原则

-   不重写 `AgentLoop`、`AgentHost` 和现有领域计算工具。
-   **先减量，再约束，后多 Agent。** 多 Agent
    隔离的是上下文，不减少每一步的信息量；行为可控来自运行时检查，不来自文字约束。
    在单 Agent 的每步固定开销没有降下来之前，不扩成多 Agent。
-   第一阶段只新增意图解析、任务规格和能力路由，不立即启用多 Agent
    执行。
-   保留 `TaskState`，不把它和 `TaskSpec` 合并。
-   保留 `FAMILIES`
    作为底层工具族的唯一事实来源，不再手工维护第二份工具名清单。
-   不允许 LLM 直接决定权限；LLM 提议，确定性代码校验。
-   第一版继续保持子 Agent 不可递归派发（当前深度上限 1），由根
    Principal Agent 统一调度。
-   不把"对接分数高"或"模型预测活性高"直接表述为实验活性或药物有效性。
-   所有专业 Agent 的结论都应能追溯到数据、文献、结构或计算产物。

------------------------------------------------------------------------

## 1. 当前源码基线与关键判断

以下判断基于本次项目压缩包中的实际文件，而非抽象架构假设。

### 1.1 主要调用链

``` text
vsagent/app.py
  ├── _build_loop(session, ledger)
  │     └── AgentLoop(...)
  ├── _build_registry(memory, allow_spawn)
  │     ├── core tools
  │     ├── TaskTool / HypothesisTool
  │     ├── AssessCampaignTool / SurveyStructuresTool
  │     ├── LoadToolsTool
  │     ├── SpawnAgentTool / AgentStatusTool (root only)
  │     └── domain_tools()
  └── AgentHost(...)
          ├── loop_for()
          ├── spawn()
          ├── start_pending()
          ├── wait()
          └── cancel()
```

`AgentLoop`
负责模型请求、上下文装配、工具执行、权限检查、Hook、事件记录和收尾。`AgentHost`
负责会话级 Loop、后台任务和同一 root family 的共享
Ledger。二者应作为本轮重构的稳定执行内核。

### 1.2 当前相关文件的真实职责

  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------
  文件                               当前职责                                                                        重构定位
  ---------------------------------- ------------------------------------------------------------------------------- --------------------------------------------------
  `vsagent/app.py`                   应用装配；创建 Host、Loop 和 ToolRegistry                                       作为新模块的依赖注入入口，避免堆积业务判断

  `core/models.py`                   `Session`、`TaskState`、`Event`、`Step` 等持久化模型                            兼容扩展 Session 元数据；保留 TaskState

  `core/agent/intent.py`             关键词匹配 task/memory/corpus/history 来源提示                                  保留为 IntentHint，不将其误认为科研意图解析器

  `core/agent/task.py`               `TaskTool`，更新 Session.task_state                                             保留；后续让 TaskSpec 与 TaskState 分工

  `core/agent/toolset.py`            根据 `Session.tool_families` 隐藏/加载领域工具族                                复用；新增 Router 到 family 的适配层

  `core/agent/domain/all_tools.py`   `FAMILIES`、`catalog()`、`tool_family()`、`domain_tools()`                      底层工具族唯一来源

  `core/agent/loop.py`               AgentLoop；`_intent_text()`、`tool_specs()`、`_assemble()`、`_execute_tool()`   仅增加薄的 preflight/route hook，不重写循环
                                     等                                                                              

  `core/agent/context.py`            PREFIX、MEMORY、TASK、EVENT、RECENT、EVIDENCE、HISTORY 分层上下文               新增结构化任务摘要层或扩展 TASK 层，控制 token
                                                                                                                     预算

  `core/agent/host.py`               `AgentHost`，维护 Loop、后台任务、共享 Ledger；`MAX_DEPTH=1`                    保留为执行宿主，后续接受结构化 AgentTask

  `core/agent/spawn.py`              `SpawnAgentTool`、历史 fork、任务书、状态查询                                   逐步从自由文本任务书扩展为结构化任务参数

  `core/agent/permission.py`         工具级权限判定、询问与拒绝                                                      后续叠加 Agent role/capability
                                                                                                                     权限，不替代原有权限规则

  `core/agent/hooks.py`              `PreToolUse`、`PostToolUse`、`Stop` Hook                                        后续用于任务前置条件和交付验收

  `core/agent/ledger.py`             受体、配体集合、产物 handle、文件指纹和 lineage                                 保留为计算产物账本；不要把它改造成通用科研数据库

  `core/agent/event_log.py`          主题/步骤事件记录及召回                                                         继续记录执行过程；Campaign/AgentTask 与 Event
                                                                                                                     建立引用即可

  `core/agent/hypothesis.py`         操作 `TaskState.hypotheses` 的假设工具                                          复用，后续增加跨 Agent 证据评估

  `core/agent/workspace.py`          Session 工作目录与状态文本                                                      作为文件工作区；另建结构化 ResearchWorkspace

  `core/agent/domain/survey.py`      `SurveyStructuresTool`，结构全景调查                                            作为 Structure Agent 的底层能力之一

  `core/agent/domain/campaign.py`    `AssessCampaignTool`，筛选前的活动/骨架/数据盘点与策略判断                      保留为领域评估工具，不等同于通用 Planner

  `AGENTS.md`                        当前项目规则入口，约 11 KB                                                      Phase 10 再分级，不在第一阶段整体拆除
  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------

### 1.3 当前系统已经有、不要重复造的能力

1.  **工具族加载**：`domain/all_tools.py` 的 `FAMILIES`
    是唯一族定义；`catalog()` 从真实模块动态生成族内工具名。
2.  **工具按会话隔离**：`Session.tool_families`
    记录已加载族，`toolset.hidden_names()` 控制本轮暴露。
3.  **会话级任务状态**：`Session.task_state` 已持久化，包含
    goal、constraints、done、todo、blockers、hypotheses 和 version。
4.  **上下文分层**：`ContextAssembler` 已有
    PREFIX/MEMORY/TASK/EVENT/RECENT/EVIDENCE/HISTORY 等层级。
5.  **权限与执行
    Hook**：工具执行已经经过参数校验、PreToolUse、PermissionPolicy、工具执行、PostToolUse。
6.  **后台子 Agent**：`AgentHost.spawn()`
    已支持独立子会话、后台执行、共享工作目录和 root family Ledger。
7.  **科研产物追踪**：`SessionLedger` 已使用稳定 handle、指纹、状态和
    lineage 管理计算产物。
8.  **假设管理**：`HypothesisTool`
    已提供假设提出、分组、更新、关闭和列表操作。

因此，本次缺失的重点不是"再做一套工具系统"，而是增加意图、任务和能力之间的编译层。

### 1.4 每一步固定开销的实测构成

下面这组读数是本方案排期的依据，Phase 0 要重新测量并替换它，不能直接引用本文的数字：

| 项 | 读数 | 来源 |
| --- | --- | --- |
| 常驻核心工具（20 个），删重复文字前 | 8,552 token | 2026-11-12 逐个测量 |
| 常驻核心工具（20 个），删重复文字后 | 7,809 token | 同上（−8.7%） |
| 常驻核心工具（20 个），拆族之后 | 7,818 token | 同上（`load_tools` 的族目录多了一行） |
| 稳定前缀，主 Agent | 5,112 token（固定段 1,749 + 项目规则 3,346） | `tools/check_prefix.py` 当前输出 |
| 稳定前缀，子 Agent | 3,323 token（固定段 1,749 + 项目规则 1,448） | 同上 |
| 领域工具族（19 族、56 个工具） | 全部装入 23,776 token；单族 346–3,300 | 按 `catalog()` 与 `family_tokens()` 现算 |
| 典型一轮实际装入 | 一到三族，3,100–6,000 token | `docs/review.md` §2.1 |

三个由读数直接得到的结论：

1.  **每一步最大的一块是常驻核心工具**，不是项目规则，也不是领域工具族。
    规则拆分段最多省 2,100 token；核心工具分档可以省 4,000 以上。
2.  **按能力合并多个工具族，会比现在实际装入的量更大。**
    `analysis` 一族 3,300 token，`ligand` 2,146、`sar` 2,014、`structure`
    1,972、`pocket` 1,915。任何一个"能力"覆盖三到四个族，合并后就超过
    当前单轮 3,100–6,000 的量。
3.  **`AGENTS.md` 按功能分节，不按学科分节。**
    九节里，任务入口、语言规范、确认机制三节只与"是否直接和用户对话"有关；
    科学判断、工具纪律、禁止事项是全局的；文档职责与流程索引两节是导航。
    所以规则拆分的第一条轴是"是否面向用户"，不是"属于哪一个专业 Agent"。

------------------------------------------------------------------------

## 2. 目标对象与协议边界

本节先定义所有阶段共同遵守的数据协议。协议先于具体 Agent
实现，避免每开发一个 Agent 就修改主流程。

### 2.1 五类对象必须分开

  -------------------------------------------------------------------------------------------------------------------------------
  对象               回答的问题                                     生命周期                     建议存放位置
  ------------------ ---------------------------------------------- ---------------------------- --------------------------------
  `IntentSpec`       用户想做什么、明确说了什么、还缺什么？         单轮解析，可被后续消息修订   `intent_parser.py`

  `TaskSpec`         本次研究任务要完成什么、交付什么、如何验收？   一个 Task/Campaign 生命周期  `task_spec.py`；必要时持久化到
                                                                                                 Session

  `TaskState`        当前任务已经做了什么、还剩什么、卡在哪里？     现有 Session 生命周期        `core/models.py`

  `CapabilityPlan`   为完成任务需要哪些专业能力、工具族和依赖？     一次规划版本                 `capability_router.py`

  `AgentReport`      专业 Agent 实际发现了什么，证据和产物在哪里？  一个 AgentTask 生命周期      `multiagent/contracts.py`
  -------------------------------------------------------------------------------------------------------------------------------

不能把 `TaskSpec` 塞进 `TaskState` 的 `goal/todo` 字符串中，也不能用
`AgentReport` 替代 Event 或 Ledger。

### 2.2 IntentSpec

建议初始字段：

``` python
@dataclass(frozen=True)
class IntentSpec:
    schema_version: int
    primary_goal: str
    intent_types: tuple[str, ...]
    research_objects: dict[str, object]
    explicit_constraints: tuple[str, ...]
    exclusions: tuple[str, ...]
    provided_inputs: dict[str, object]
    missing_information: tuple[str, ...]
    clarification_required: tuple[str, ...]
    expected_deliverables: tuple[str, ...]
    confidence: float
    uncertainties: tuple[str, ...]
    source_message_ids: tuple[str, ...]
```

设计要求：

-   `intent_types` 可以多选，不能强迫一个科研问题只落入一个类别。
-   用户原话和解析结果分开保存；模型不能覆盖原始消息。
-   `explicit_constraints` 只放用户明确提出的要求；推断出来的假设放
    `uncertainties`。
-   `exclusions` 作为强约束进入后续路由和执行检查。
-   `confidence` 是解析置信提示，不作为自动执行高风险动作的唯一依据。
-   解析失败时不得伪造一个"完整意图"，应返回 `needs_clarification`
    类型的结果。

### 2.3 TaskSpec

``` python
@dataclass
class TaskSpec:
    schema_version: int
    task_id: str
    campaign_id: str
    objective: str
    research_scope: dict[str, object]
    subgoals: list[dict[str, object]]
    constraints: list[str]
    exclusions: list[str]
    required_inputs: list[dict[str, object]]
    available_inputs: dict[str, object]
    required_outputs: list[dict[str, object]]
    evidence_requirements: list[dict[str, object]]
    acceptance_criteria: list[dict[str, object]]
    approval_points: list[dict[str, object]]
    status: str
    revision: int
```

验收条件必须尽量可执行。例如"每个口袋都有结构来源、残基列表和坐标文件"可由代码验证；"口袋画像科学合理"则需要证据审查或人工评审，不能伪装成简单布尔条件。

### 2.4 CapabilityPlan

``` python
@dataclass
class CapabilityPlan:
    schema_version: int
    task_id: str
    revision: int
    selected_capabilities: list[str]
    selected_agents: list[str]
    tool_families: dict[str, list[str]]
    standalone_tools: dict[str, list[str]]
    dependencies: list[dict[str, object]]
    parallel_groups: list[list[str]]
    denied_capabilities: list[str]
    unresolved_inputs: list[str]
    approval_points: list[str]
    rationale: list[str]
```

Phase 1 的 `selected_agents` 可以先是逻辑角色标识，并由单 Agent adapter
执行；不能因为计划里出现了 Agent 名称就声称已经完成多 Agent 调度。

### 2.5 AgentTask 与 AgentReport（Phase 2 启用）

``` python
@dataclass
class AgentTask:
    task_id: str
    campaign_id: str
    parent_task_id: str
    role: str
    objective: str
    input_refs: dict[str, object]
    constraints: list[str]
    required_outputs: list[str]
    acceptance_criteria: list[dict[str, object]]
    dependencies: list[str]
    permissions: list[str]
```

``` python
@dataclass
class AgentReport:
    task_id: str
    campaign_id: str
    agent_role: str
    status: str
    findings: list[dict[str, object]]
    evidence_refs: list[str]
    artifact_refs: list[str]
    conclusions: list[dict[str, object]]
    limitations: list[str]
    unresolved_questions: list[str]
    suggested_followups: list[dict[str, object]]
```

------------------------------------------------------------------------

## 3. 分阶段实施路线图

> 每一阶段必须先完成验收，再进入下一阶段。0.5 是前置的减量阶段，不属于协议阶梯，
> 必须最先完成；前 1--7 阶段是当前最优先的改造范围；8--12 阶段是多 Agent
> 和科研闭环的渐进扩展。

### Phase 0：源码基线冻结与回归测试基准

**目的：** 在动代码之前明确当前行为，避免重构把已有能力破坏。

**修改文件：** - 新增 `docs/multi-agent-refactor/baseline.md` - 新增
`tools/eval_intent_router.py`（后续逐步完善） -
现有测试不重写：`tests/test_agent.py`、`tests/test_smoke.py`

**工作内容：** 1. 固定本次重构基准压缩包和 Git commit。 2. 记录
`app.py → AgentHost → AgentLoop → ToolRegistry → domain tools`
的装配链。 3. 统计 `FAMILIES`、常驻工具、禁用工具和工具定义 token。 4.
记录 `intent.detect()` 的命中行为和当前 `load_tools` 的手动路由行为。 5.
记录 root/subagent 两种注册表差异以及 `MAX_DEPTH=1` 限制。 6. 建立
30--50
条真实科研意图样例，保留用户原始输入、期望目标、约束、排除项和预期能力。 7.
**逐个统计常驻核心工具的 token 成本与在真实会话中的使用频率**，作为 Phase
0.5 分档的依据；同时统计 `AGENTS.md` 每一节的 token 占比。

**禁止事项：** - 不修改 AgentLoop 执行语义。 - 不重命名现有工具。 -
不删除旧的 IntentHint。 - 不更改 Session 序列化格式。

**验收：** - `python tests/test_agent.py` -
`python tests/test_smoke.py` - 保存一份基线评测结果。 -
所有测试失败必须记录为既有问题或本次新增问题，不得混淆。

### Phase 0.5：单 Agent 信息量裁剪（前置，不依赖新协议）

**目的：** 在引入任何新协议之前，先把每一步发给模型的信息量降下来。

这一阶段不写意图解析、不写任务规格、不写能力路由，只调整三处装配：常驻核心工具、项目规则、领域工具族的切分方式。

**为什么排在协议之前：** 多 Agent 隔离的是上下文，不减少每一步的信息量。一个只负责取结构的子 Agent，如果仍然带着二十个常驻工具和整份项目规则，那么"分成九个 Agent"只是把同一份开销乘以九。先让单个 Agent 每一步看得过来，多 Agent 才有意义；否则 Phase 1--7 建起来的协议层会被过量的上下文淹没，收益测不出来。

**修改文件：** - `vsagent/app.py`：核心工具按会话类型分档装配 -
`vsagent/core/agent/prompt.py`：项目规则按"是否面向用户"分两段拼装 -
`vsagent/core/agent/domain/all_tools.py`：拆开 `scoring` 族 -
`tools/check_prefix.py`：判据按两档分别出读数 -
`tests/test_agent.py`：新增 `core-tools-profile`、`rules-split`、`family-split` 三个检查组

**工作内容：**

1.  **常驻核心工具按会话类型分档。**

    当前二十个常驻工具每步合计 8,552 token（删重复文字与拆族之后 7,818），占每步固定开销的一半以上；而子 Agent 的注册表与主 Agent 只差 `spawn_agent` 与 `agent_status`（`app.py:266`、`app.py:474`）。按会话类型分两档：

    | 档位 | 面向 | 默认保留 | 默认去掉 |
    | --- | --- | --- | --- |
    | `orchestrator` | 根会话、与用户直接对话的会话 | task、hypothesis、spawn_agent、agent_status、search_history、read_turn、recall_event、close_topic、write_run、open_viewer、read、grep、bash、load_tools | 无 |
    | `worker` | 子 Agent、只做计算的会话 | read、write、patch、grep、bash、recall、load_tools、write_run、open_viewer、close_topic、recall_event、task | spawn_agent、agent_status、search_history、read_turn、remember、hypothesis、assess_campaign、survey_structures |

    上表是初始值，必须按 Phase 0 第 7 项量出的"token 成本 × 使用频率"重新核对，不能照抄。

    **预算按档位分别定，不共用一个数：**

    | 档位 | 现在 | 上限 | 余量 |
    | --- | ---: | ---: | ---: |
    | 主 Agent（`orchestrator`） | 7,818 | **8,000** | 182 |
    | 子 Agent（`worker`） | 7,254 → 4,213 | **4,800** | 587 |

    主 Agent 的上限定在 8,000 而不是 4,000，理由有两层。第一，主 Agent 就是要用全部二十个工具，它的 8,552 已经按"只删重复文字"压到 7,809（−8.7%，拆族后 7,818）；再往下只能删工具或合并工具，那会改变行为，不属于减量。第二，上限的作用是守住"不许悄悄涨回去"，不是逼着把工具删到某个数；8,000 留一百多的余量，新增一个工具就必须同时说明删掉了什么。

    > 2026-11-12 更新：Phase 10 加了第二十一个常驻工具 `dispatch_campaign`（176 token，压到 137），主 Agent 现在是 7,955 / 8,000，余量只剩 45。这次**没有删掉任何工具**，等于把余量一次性用完了。下一个常驻工具必须先合并或删除一个现有的，否则这条判据会红——这是有意的：余量在这里就是"还能不能加"的额度。
    >
    > 同日 Phase 11 再加 `research` 族（四个工具，按族装入，不进常驻），但族清单写在 `load_tools` 的定义里，主 Agent 因此涨到 7,982 / 8,000，余量 18 token。**额度已经见底**：之后任何说明变长都要先腾地方。

    子 Agent 一侧的 4,800 来自实测：按 `worker` 档去掉八个工具后是 4,213，留五百多的余量。

    `worker` 保留 `recall` 而去掉 `remember`：只读的项目事实对每个 Agent 都有用，写入长期记忆只应由主 Agent 在用户明确要求时做。

    `load_tools` 两档都必须保留：它是工具族路由的回退通道，去掉之后子 Agent 遇到计划外的工具族无法自救，只能改用 bash 自己实现，这正是 `docs/review.md` 里 F1 那类二十步弯路。

2.  **项目规则按"是否面向用户"切成两段。**

    `AGENTS.md` 现在 3,527 token。按 1.4 的统计，它按功能分节而不是按学科分节，所以按九个角色切，会把约 1,500 token 塞进每一个角色。按是否直接与用户交互切两段更有效：

    | 段 | 内容 | 主 Agent | 子 Agent |
    | --- | --- | --- | --- |
    | 入口与优先级 | 1 文档职责与导航（本文件优先于 playbook、细则去哪读） | 加载 | 不加载 |
    | 全局不变量 | 2.1 科学可追溯、2.3 最小必要复杂度、2.4 结果可验证、6 科学判断与交付、7 工具调用纪律、8 禁止事项 | 加载 | 加载 |
    | 用户交互 | 2.2 用户决策优先、3 任务入口与意图识别、4 语言表达规范、5 任务确认机制 | 加载 | **不加载** |
    | 导航 | 9 流程索引 | 不加载 | 不加载 |

    实测：主 Agent 3,346 token（3,527 减去流程索引），子 Agent 1,448 token（再少掉入口与交互四节）。子 Agent 比主 Agent 少 57%。

    `AGENTS.md` 仍然是唯一入口文件，不新建第二份规则目录。`tools/check_prefix.py` 只核对切分是否生效（子 Agent 手里确实没有那几节），**不设上限**——规则是许可与判据的来源，少一条的代价大于多花一千 token，体量只报数。

    子 Agent 不加载确认机制，是因为它不能与用户对话。确认只发生在主 Agent，这一点必须与 Phase 8 的确认点判定保持一致，不能两边各说一套。

3.  **拆开 `scoring` 族。**

    `scoring` 现在把 `dock`、`finetune`、`affinity` 三个工具捆在一族（`all_tools.py:96`），任何需要评分的任务都会把 `dock` 带进工具清单。这与 §6.3 的要求直接冲突：用户明确不做对接时，族级加载无法阻止模型调用 `dock`。

    拆成 `docking`（`dock`）与 `affinity`（`fit_affinity`、`score_affinity`）两族。这是对 `FAMILIES` 的修改，要同步三处：

    - 旧会话的 `Session.tool_families` 里保留着 `scoring`：**按别名展开成两族**，而不是当成未知族名丢掉——丢掉会让那次会话里的三个打分工具突然消失，模型会以为工具没了、转去用 bash；
    - `close_topic(next_tools=...)` 与 `load_tools` 的族名校验：同样先展开旧名；
    - 既有测试里关于 `scoring` 的断言。

**预期读数（2026-11-12 实测）：**

| 项 | 主 Agent | 子 Agent |
| --- | ---: | ---: |
| 常驻核心工具，压缩前 | 8,552 | 7,988 |
| 常驻核心工具，删重复文字后 | 7,809 | 7,245 |
| 常驻核心工具，拆族后 / 按 `worker` 档裁剪后 | 7,818 | 4,213 |
| 稳定前缀 | 5,112 | 3,323 |
| 每步固定开销（核心 + 前缀 + 一到三族约 3,000） | 约 15,900 | 约 10,500 |

主 Agent 这一侧的下降只有 8% 量级（8,552 → 7,818），子 Agent 是 47%（7,988 → 4,213）。这是"只删重复文字、切规则、分档、拆族"能做到的全部；再往下必须合并工具或把工具移出常驻，属于后续阶段，不在本阶段的目标里。

**验收：**

- `python tools/check_toolset.py`：主 Agent 不超过 8,000、子 Agent 不超过 4,800，两档都通过。
- 一个根会话与一个子会话各跑一轮真实任务：主 Agent 每步工具定义比基线下降 8% 以上，子 Agent 下降 40% 以上。
- `python tests/test_agent.py` 的检查组全部通过（新增 `core-tools-profile`、`rules-split`、`family-split` 三组）。
- 裁剪后不出现"规则点名的工具看不见"：`check_prefix` 报的"首步不可见工具"数量不增加。
- 子 Agent 在一轮真实任务里没有因为缺少工具而改用 bash 自行实现。

**禁止事项：**

- 不修改 `AgentLoop` 的执行语义、权限、Hook、重试和取消。
- 不删除任何工具，只改变它出现在哪一档。
- 不新建第二份工具清单或第二份规则文件。
- 不在这一阶段引入意图解析或能力路由。

**回滚：** 分档由配置项切换，`full` 档即当前行为（见 §8.3）。

### Phase 1：定义协议与数据模型

**目的：** 先定义意图、任务和路由的数据契约，不接入执行。

**新增文件：** - `vsagent/core/agent/contracts/__init__.py` -
`vsagent/core/agent/contracts/intent.py` -
`vsagent/core/agent/contracts/task.py` -
`vsagent/core/agent/contracts/capability.py` -
`vsagent/core/agent/contracts/agent.py`（Phase 2 使用，可先定义） -
`tests/test_agent_contracts.py`

**修改文件：** - 暂不修改 `core/models.py`。 - 暂不修改
`app.py`、`loop.py`。

**工作内容：** 1. 定义
IntentSpec、TaskSpec、CapabilityPlan、AgentTask、AgentReport。 2.
定义枚举：IntentType、TaskStatus、PlanStatus、AgentTaskStatus、EvidenceType。
3. 定义 `to_dict()` / `from_dict()` 或 Pydantic
等价校验方式（优先沿用项目依赖，不要为协议单独引入大型框架）。 4.
每个协议带 `schema_version`。 5. 对未知字段、缺失必需字段、空
ID、非法状态、重复依赖进行验证。 6.
约束字段区分"用户明确表达""系统推断""尚不确定"。

**验收：** - 协议可序列化和反序列化。 -
缺失字段、非法枚举和无效依赖能给出明确错误。 - 协议模块不 import
app、AgentLoop 或 domain 工具，避免循环依赖。

### Phase 2：实现 Intent Parser

**目的：** 把"来源提示"与"科研意图理解"分离。

**新增文件：** - `vsagent/core/agent/intent_parser.py` -
`vsagent/core/agent/intent_schema.py`（若 JSON Schema 独立维护） -
`tests/test_intent_parser.py` - `tests/fixtures/intent_cases.jsonl`

**修改文件：** - `vsagent/core/agent/intent.py`：保留现有 IntentHint
API，不做破坏性替换。 -
`vsagent/core/agent/loop.py`：暂不接入执行，只允许通过独立调用或测试调用
Parser。

**工作内容：** 1. Intent Parser 使用独立、短小的解析提示词，不加载完整
`AGENTS.md`。 2.
解析目标、对象、显式约束、禁止事项、输入、缺失信息和交付物。 3.
支持多意图组合，例如结构发现 + 口袋分析 + 配体画像。 4. 对输出进行 JSON
Schema / dataclass
校验；解析不合格时有限重试一次，仍失败则返回结构化解析错误。 5.
对实体做规范化，但不猜测用户未提供的突变体、物种、结构 ID 或筛选库。 6.
为每条字段记录来源：用户本轮明确提出、历史任务继承、系统推断。 7. 将
IntentHint 继续用于 memory/corpus/history
来源提醒；不要让它承担科研任务分类。

**重要行为：** -
用户只输入"EGFR"：不能自动启动结构检索和筛选，应澄清研究目的。 -
用户说"获取 EGFR 所有候选结合口袋"：结构 ID 和 pocket ID
缺失不构成阻塞，应规划结构发现。 -
用户说"不要对接，只比较骨架和已知活性"：`exclusions`
必须保留并影响后续路由。 - 用户说"继续分析
PK-02"：必须进入上下文解析阶段，继承当前任务中的
PK-02，而不是创建新任务。

**验收：** - 意图类型多标签准确。 - 显式约束召回率目标 ≥ 0.95。 -
禁止事项召回率目标 = 1.00（测试集内）。 -
对缺少非必要信息不应过度追问。 -
对真正缺少研究目标的输入能提出一个关键澄清问题。

### Phase 3：上下文解析与任务连续性

**目的：**
区分新任务、当前任务追问、任务修改和历史任务恢复，避免每轮重新解析导致目标漂移。

**新增文件：** - `vsagent/core/agent/task_context.py` -
`tests/test_task_context.py`

**修改文件：** -
`vsagent/core/agent/loop.py`：在新用户消息入口处获取解析上下文。 -
`vsagent/core/agent/context.py`：按需提供精简的任务摘要，不重复注入整段历史。 -
`vsagent/core/models.py`：本阶段先不增加持久字段，先利用现有
`Session.task_state`、Events 和 Message IDs。

**工作内容：** 1. 定义
`TurnIntentMode`：`new_task`、`continue_task`、`modify_task`、`ask_about_result`、`resume_historical_task`、`general_chat`。
2. 输入"这个结果说明什么"时继承最近相关 AgentReport/证据，不新建
TaskSpec。 3. 输入"改成
T790M"时只修改研究对象中突变体字段，保留其他约束。 4.
输入"不要做对接"时追加 exclusion，并使尚未执行的冲突计划失效。 5.
输入"继续上次的 PK-02"时优先根据当前 Session 的 TaskState、Event、Ledger
和历史消息解析引用。 6. 对无法唯一解析的引用提出澄清，不得自行选择对象。

**验收：** - 多轮追问不丢失目标。 - 用户修改一个约束不会清除其他约束。 -
新任务不会错误继承旧任务的对象和禁止事项。 - 历史恢复必须记录来源
Session/Event/Artifact 引用。

### Phase 4：Task Specification Compiler

**目的：** 把 IntentSpec 编译为可执行、可验收的 TaskSpec。

**新增文件：** - `vsagent/core/agent/task_spec.py` -
`vsagent/core/agent/task_compiler.py` - `tests/test_task_compiler.py`

**修改文件：** - `vsagent/core/models.py`：在评估完兼容策略后，为
Session 增加可选 `task_spec` 快照或
`active_task_id`；旧状态文件缺少该字段时默认空值。 -
`vsagent/core/agent/task.py`：保留 TaskTool；必要时增加 TaskSpec
查看/确认工具，不让模型通过旧 TaskTool 任意改写 TaskSpec。 -
`vsagent/core/agent/context.py`：在 TASK 层呈现精简 TaskSpec 摘要和现有
TaskState。

**工作内容：** 1. 由 IntentSpec 生成
objective、research_scope、subgoals。 2. 将 explicit_constraints 和
exclusions 原样继承，不允许编译阶段"优化掉"。 3. 将交付物转换为
required_outputs。 4. 为每个交付物生成 acceptance_criteria。 5.
区分机器可验收条件和需要科学评审的条件。 6.
将必需输入与可选输入区分；缺少可选输入不应阻塞任务。 7.
为多阶段任务建立初步依赖，但暂不创建子 Agent。 8. 生成 TaskSpec
revision；用户修改目标后递增 revision，保留变更记录。

**TaskSpec 与 TaskState 分工：** -
TaskSpec：目标、范围、子目标、输入/输出合同、验收标准、用户确认点。 -
TaskState：运行进度、已完成、待办、阻塞、假设。 - 不把完整 TaskSpec
每一步重复塞进模型上下文；只注入精简摘要和当前子任务。

**验收：** - 同一个 IntentSpec 可稳定生成结构一致的 TaskSpec。 - 每个
required_output 至少对应一个 acceptance criterion。 - exclusions
不会在任务编译后丢失。 - 旧 Session 文件可正常加载。

### Phase 5：Capability Registry

**目的：** 建立"科研能力 → Agent 角色 → 工具族/常驻工具"的可查询注册表。

**新增文件：** - `vsagent/core/agent/capabilities.py` -
`vsagent/core/agent/capability_profiles.py` -
`tests/test_capability_registry.py`

**修改文件：** - `vsagent/core/agent/domain/all_tools.py`：原则上不改
`FAMILIES`；只允许提供更稳定的读取接口。 -
`vsagent/core/agent/toolset.py`：可增加 family availability
查询接口，不改变当前隐藏/加载语义。

**建议初始能力映射：**

  --------------------------------------------------------------------------------------------
  Capability              Agent Role         FAMILIES           常驻工具/说明
  ----------------------- ------------------ ------------------ ------------------------------
  target_intelligence     target             `uniprot`,         视目标信息而定
                                             `opentargets`      

  structure_discovery     structure_pocket   `structure`        `survey_structures` 常驻

  pocket_discovery        structure_pocket   `pocket`,          口袋检索/预测工具
                                             `structure`        

  pocket_profiling        conformation       `pocket`,          只读分析优先
                                             `structure`,       
                                             `analysis`         

  ligand_intelligence     ligand             `chembl`,          已知活性和分子身份
                                             `pubchem`,         
                                             `literature`       

  literature_evidence     literature         `literature`,      文献/本地语料
                                             `corpus`,          
                                             `websearch`        

  virtual_screening       screening          `screen`,          依输入和策略选择，不默认全装
                                             `pharmacophore`,   
                                             `receptor`,        
                                             `ligand`,          
                                             `scoring`          

  scoring_evaluation      scoring            `scoring`,         优先读取已有产物
                                             `analysis`,        
                                             `admet`            

  mechanism_analysis      mechanism          `analysis`,        结合证据解释
                                             `pocket`, `sar`,   
                                             `literature`       

  hypothesis_validation   hypothesis         `literature`,      `HypothesisTool` 常驻
                                             `analysis`, `sar`  
  --------------------------------------------------------------------------------------------

**注意事项：** - 这是能力映射，不是一次性加载清单。Router
应按子目标裁剪工具族。 - `survey_structures`、`assess_campaign` 是 app
注册的常驻工具，不属于 FAMILIES；必须在 Registry 中单独声明，不要伪造
family。 - `FAMILIES` 中的实际工具名必须通过 `catalog()`
动态读取，不能手工重复维护。 - 需要区分
`read`、`analyze`、`execute`、`write` 权限；同一个 family
内工具不一定具有相同风险。 - 被 `.vsagent/tools.toml` 禁用的工具不得被
Router 计划为可用能力。

**验收：** - Registry 能校验所有 family 名称来自 `catalog()`。 -
不存在的 family、被禁用的工具或能力依赖能够给出明确不可用原因。 -
工具族定义与实际 `domain_tools()` 保持一致。

### Phase 6：Capability Router 与 Research DAG

**目的：** 将 TaskSpec 转化为明确的能力分配和依赖计划。

**新增文件：** - `vsagent/core/agent/capability_router.py` -
`vsagent/core/agent/research_dag.py` - `tests/test_capability_router.py`

**修改文件：** - `toolset.py`：提供加载/释放 family 的可复用函数，避免
Router 直接修改 Session 内部字段。 - `core/models.py`：暂不持久化复杂
DAG；先保存 plan revision 和摘要，完整计划可落到 campaign JSON。 -
`loop.py`：暂不让 LLM 直接决定工具族。

**路由策略：** 1. 先按 TaskSpec 的 subgoals 匹配 capabilities。 2.
再通过 Registry 找到可承担该能力的 Agent role。 3. 由 role profile
映射到 family 和 standalone tools。 4. 根据输入/输出依赖生成有向无环图。
5. 合并可并行子任务。 6. 删除与 exclusions 冲突的能力。 7.
检查工具是否被禁用、是否需要用户确认、是否缺少必需输入。 8. 输出
CapabilityPlan，并说明选择和未选择的原因。

> 2026-11-13（P-81 / P-83）更新：第 7 步里「缺少必需输入」分成两类。**缺了连输入都没有**
> 的前置补成节点（`AUTO_PREREQUISITES`：筛选 ← 口袋 + 配体，口袋画像 ← 口袋发现）；
> **能花上一次产物**的（评分 ← 筛选）只记 `unresolved_inputs`。用户已经给了
> `pocket` / `ligand_set` / `structure` / `receptor` 时，对应能力记进
> `plan.provided_capabilities`，不补节点、也不算缺。派发时点名的计划外角色由
> `CapabilityRouter.extend` 按能力表补进来。判据在 `router` 组。

**示例：** 用户："获取 EGFR
的所有候选结合口袋，并分析每个口袋可能适配的活性分子特征。"

预期计划： - Structure/Pocket：结构全景和口袋发现。 - Ligand
Intelligence：已知配体和活性画像。 - Literature：相关结合证据。 -
Conformation：依赖 Structure、Ligand 和 Literature
的结果，进行口袋画像。 - 不启动 Virtual
Screening：用户没有要求筛选新分子。 - 不启动 Scoring：当前没有待评价的
docking/affinity 结果。

**路由输出示意：**

``` json
{
  "selected_agents": [
    "structure_pocket",
    "ligand",
    "literature",
    "conformation"
  ],
  "tool_families": {
    "structure_pocket": ["structure", "pocket"],
    "ligand": ["chembl", "pubchem"],
    "literature": ["literature", "corpus"],
    "conformation": ["pocket", "structure", "analysis"]
  },
  "dependencies": [
    {"from": "structure_pocket", "to": "conformation"},
    {"from": "ligand", "to": "conformation"},
    {"from": "literature", "to": "conformation"}
  ],
  "not_selected": [
    {"agent": "screening", "reason": "用户未要求筛选新分子"},
    {"agent": "scoring", "reason": "当前无待评价计算产物"}
  ]
}
```

**验收：** - 同一任务在同一 Registry 版本下路由结果稳定。 -
排除项优先于能力匹配。 - 不存在无依赖的下游任务提前启动。 -
不因"用户没有指定 PDB/pocket"而错误判定结构发现任务缺少必要输入。 -
Router 必须解释为何某个 Agent 未被选中。

### Phase 7：接入 AgentLoop 的兼容执行适配器

**目的：** 先让新意图和路由层真正影响工具暴露，但不启用多 Agent 调度。

**新增文件：** - `vsagent/core/agent/route_adapter.py` -
`tests/test_route_adapter.py`

**修改文件：** - `vsagent/app.py`：在应用装配时注入
IntentParser、TaskCompiler、CapabilityRouter。 -
`vsagent/core/agent/loop.py`：在新一轮用户输入进入模型前增加轻量
preflight；保留 `_run_steps()`、`_execute_tool()` 的既有执行语义。 -
`vsagent/core/agent/toolset.py`：由 adapter 将 CapabilityPlan 的 family
结果转换为 `Session.tool_families` 的加载动作。 -
`vsagent/core/agent/context.py`：仅注入 Intent/Task/Plan 的短摘要。 -
`vsagent/core/agent/intent.py`：原 IntentHint 仍负责来源提示，不与新
Parser 混淆。

**建议采用三种模式：** 1. `off`：完全使用旧逻辑，作为回滚开关。 2.
`shadow`：运行 Parser/Router 并记录结果，但不改变工具清单。 3.
`enforce`：通过 Router 自动加载本轮所需 family，并在执行前检查
exclusions。

先运行 shadow 模式，比较 Router
计划和模型实际调用的工具族；达到指标后再切 enforce。

**关于工具加载：** - 现有 `tool_specs()` 通过 `hidden_names(session)`
隐藏未加载族。 - 现有 `load_tools`
是模型可见的常驻工具，会改变后续请求的工具清单和 prompt cache。 -
enforce 模式可以在本轮模型请求前由 adapter 预加载计划
family，减少模型自行判断和调用 `load_tools` 的步骤。 - 不要在每个 step
重新加载/释放工具族；以 Task/Topic 边界为单位，尽量稳定工具定义前缀。 -
仍保留 `load_tools` 作为用户交互、非标准任务和 Router 无法覆盖情形的
fallback，但需记录 fallback 原因。

**验收：** - `off` 模式与当前行为一致。 - `shadow`
不改变工具可见性和执行结果。 - `enforce` 模式只加载计划中的 family。 -
旧工具调用、权限询问、Hook、取消和重试行为不变。 - Router
与实际工具使用情况写入 trace，便于计算路由准确率。

### Phase 8：任务验收与运行时约束

**目的：** 从"Prompt
告诉模型应该做什么"转为"运行时检查任务是否允许、结果是否合格"。

**第一步（2026-11-12 已实施）：子 agent 的领域动作边界。**

用 worker 档跑真实任务时发现：工具档位只裁常驻工具，不管 `load_tools` 装进来的
五十六个领域工具。任务只要求"登记一个结构"，模型自己走到了准备受体、切换受体——
换掉的是**整个会话家族共享的当前受体**，父会话原来挂着的三项产物被搁置。并行跑
多个子 agent 时，这会互相踩。

已实施：`permission.WORKER_DENY` 拦住三个动作——`switch_receptor`、
`prepare_receptor`、`prepare_ligands`；循环层按 `session.depth > 0` 传
`worker=True`。拒绝文案写明"交给父会话，由它决定下一步"，避免模型以为工具坏了
转而用 bash 绕道。

> 2026-11-13（P-80）更新：主 agent 改成纯编排之后，父会话也不再有领域族，这三条
> 禁令等于让动作**无人可做**。改成按角色开放：准备类归 `screening`、走
> `DEFAULT_ASK`；切换受体归 `structure_pocket`（新增 `listing` 族）。共享状态仍是一份，
> 变的是「谁有权改」有据可依。
>
> 2026-11-13（P-83）再改一处：`switch_receptor` 也进 `DEFAULT_ASK`。交给结构角色
> 执行不等于不用问——换掉的是整个会话家族的当前受体。

同一次实测还留下两条：**同一套工具、同一个模型，任务书写得松是 12 步用满上限、
写到运行目录；写明排除项是 4 步收工、只做要求的两件事。** 行为不由档位决定，
由任务书与运行时约束共同决定。

尚未处理：

- ~~`write_run` 仍在 worker 档里……多个子 agent 用同一个 `run_id` 会撞车~~
  （2026-11-13 P-83 已处理：按 `meta.json` 的 `provenance.session_id` 判归属，
  同一会话重写是更新，别的会话改存 `-2` 并说明）；
- 领域工具的读与写仍没有区分：`import_structure` 会复制文件、`fetch_structure` 会
  联网，目前只有上面三个动作被单独拦下；
- TaskSpec 的 `exclusions` 还没有传进子任务书。这是 Phase 4 与 Phase 9 的接口，
  现在靠父 agent 自由文本交代。


**新增文件：** - `vsagent/core/agent/validators/__init__.py` -
`validators/task_precondition.py` - `validators/output_contract.py` -
`validators/capability_guard.py` - `validators/delivery.py` -
`tests/test_task_validators.py`

**修改文件：** - `hooks.py`：可将 validator 作为 Hook
的内部调用对象；不要把所有业务逻辑写成 shell hook。 -
`permission.py`：增加 capability/role 判定入口，但最终工具权限仍走
PermissionPolicy。 - `loop.py`：在 `_execute_tool()` 的
PreToolUse/工具执行/Stop 边界接入 validator。 - `ledger.py`：为
validator 提供真实产物状态、文件指纹和 lineage 查询。

**校验层次：** -
Precondition：任务输入是否齐全、上游依赖是否完成、用户确认是否已获得。 -
Capability Guard：当前 Agent/任务是否允许使用该工具。 - Output
Contract：工具产物是否满足指定格式、是否真实存在、是否登记。 - Delivery
Validator：TaskSpec 的必需交付物是否齐全，结论是否有证据引用。 -
Scientific Review：需要专业判断的内容由评审
Agent/人工完成，不用硬编码规则假装科学正确。

**验收：** - 被禁止的能力即使模型调用也会被拦截。 -
子 agent 调用 `switch_receptor`、`prepare_receptor`、`prepare_ligands` 按角色开放，
三个动作都走 `DEFAULT_ASK`，不再一律拒绝（判据组 `worker-guard`；
2026-11-13 P-80 / P-83）。 -
没有受体/口袋确认时，受限操作按规则阻断或请求用户确认。 -
不存在的产物不能被登记为成功。 - 交付缺少必需字段时不能标记 TaskSpec
完成。

> 2026-11-13（P-83）更新：输出合同多了 `meta_kind`——结构全景验
> `artifact(kind=survey)`、口袋集验 `artifact(kind=pockets)`、配体清单验
> `ligandset`；能机械验的从 2 件变成 5 件，`manual` 从 9 个降到 6 个。判据在
> `delivery` 组。

### Phase 9：专业 Agent Profile 与统一任务/报告协议

**目的：** 把 CapabilityPlan 中的角色变成真正可执行的专业 Agent。

**新增目录：**

``` text
vsagent/core/agent/multiagent/
  __init__.py
  profiles.py
  registry.py
  contracts.py
  reports.py
  dispatcher.py
  scheduler.py
  dependency.py
  coordinator.py
```

**修改文件：** - `spawn.py`：增加结构化 AgentTask 的参数适配。 -
`host.py`：保留 `spawn()`，增加由 Dispatcher 调用的结构化派发方法。 -
`app.py`：注册 MultiAgentCoordinator 和 AgentRegistry。 -
`prompt.py`：后续按 Agent role 加载专属 prompt（完整规则分层在 Phase
10）。

**首批 Agent：** 1. Principal
Agent：全局目标、问题分解、调度、结果整合和重规划。 2. Structure &
Pocket Agent：结构调查、口袋发现、口袋目录。 3. Conformation
Agent：口袋几何/理化特征、构象比较、配体适配画像。 4. Ligand
Intelligence Agent：已知配体、活性、骨架和分子画像。 5. Scoring
Agent：独立评价已有计算产物、评分分歧和不确定性。

第二批再加入 Virtual
Screening、Literature、Mechanism、Hypothesis/Evidence Agent。Literature
能力可先由现有工具支持，不必一开始就单独创建会话 Agent。

**Agent Profile 至少包含：** - role / description； - capabilities； -
allowed tool families； - standalone tools； - required inputs； -
required outputs； - system prompt； - read/write permissions； -
timeout / retry policy； - output schema。

**Agent 约束：** - 不要求每个 Agent 使用不同模型；初期共用 Provider
配置即可。 - 子 Agent 不得修改用户原始目标。 - 子 Agent
不得自行扩展任务范围。 - 子 Agent 只能写入授权的 workspace 对象。 - 子
Agent 不递归 spawn；维持 `MAX_DEPTH=1`。 - Agent
报告只回传摘要和引用，不把完整会话历史复制到父上下文。

**验收：** - 同一 role 的 AgentTask 输入输出一致且可验证。 - 子 Agent
的工具集合不超过 Profile 授权范围。 - Agent
失败可独立标记，不导致父任务状态被错误标记完成。 - 子 Agent 报告可被父
Agent 引用和复核。

> 2026-11-13（P-83）更新：Profile 字段里 `timeout` 已落地（`AgentProfile.timeout_minutes`，
> 每个会跑的角色都有值，`principal` 为 `None`），由调度器执行到点中断；`retry` 仍有意
> 不做——它会把「失败」变成「慢失败」，先让时限把卡住的情形暴露出来。

### Phase 10：Dispatcher、DAG Scheduler 与 Host 集成

**目的：** 让 Principal Agent 按 CapabilityPlan 真正调度多个专业 Agent。

**新增/修改文件：** - 新增 `multiagent/dispatcher.py` - 新增
`multiagent/scheduler.py` - 新增 `multiagent/dependency.py` - 新增
`multiagent/coordinator.py` - 修改 `host.py`：增加结构化 AgentTask 到子
Session 的转换。 - 修改 `spawn.py`：保留旧 `spawn_agent`
兼容路径，内部可转成 AgentTask。 - 修改 `models.py`：必要时增加
`agent_role`、`task_id`、`campaign_id`、`plan_revision`
等可选字段，并实现旧状态默认值。

**调度原则：** 1. 只有 DAG 中依赖已满足的节点可以启动。 2.
同一任务节点只能成功派发一次；使用 task_id 幂等。 3. 可并行的 Agent 由
Host 后台启动。 4. 下游任务只接收上游
`artifact_refs/evidence_refs`，不复制上游全部消息。 5.
失败重试只重试失败节点及其必要下游，不重复执行已成功节点。 6. 用户修改
TaskSpec 后，旧 plan 标记为 stale，尚未执行的节点重新计算。 7. 取消
Campaign 时，Host 取消其所有未完成子会话。 8. 仍由 Principal Agent
统一协调，子 Agent 不开放递归派发。

**与现有 Host 的关系：** - `AgentHost.loop_for()`：继续按 Session ID
缓存 AgentLoop。 - `ledger_for()`：继续按 root family 共享
SessionLedger。 - `spawn()`：继续负责创建子 Session、fork
选定历史和启动后台任务。 - 新 Dispatcher 只负责把 AgentTask 转换成 Host
可执行的 session/task，不复制 Host 的生命周期管理。 - `MAX_DEPTH=1`
继续作为硬限制。

**验收：** - Structure、Ligand、Literature 三个独立任务可并行。 -
Conformation 在依赖完成前不能启动。 - 相同 task_id
重复调度不产生重复子会话。 - 取消、超时、失败、重启恢复均有明确状态。 -
子 Agent 结果回传不使父上下文 token 线性膨胀。

> 2026-11-13（P-83）更新：`roles` 双向可调。**减**：上游自动补回，预演点名
> 「计划里还有没派的角色：…」，`Campaign.pending` 进 `campaign.done` 并写进待办；
> **加**：计划外的角色按能力表补进这次派发。自定义任务书只给 `roles` 点名的
> 那一个角色。超时两级：角色安全网（`profiles.TIMEOUT_MINUTES`，screening 90 分钟）
> 到点 `host.cancel` 并记失败；`dispatch_campaign(timeout=分钟)` 给整场上限。
> 重启时 `delegating` / `research` / `integration` 归一为 `failed` 并写明需要重新派发，
> 不再假装还在跑。判据在 `campaign-dispatch` / `scheduler` / `campaign-state`。

### Phase 11：Research Workspace、证据整合与假设闭环

**目的：** 让 Agent 协作围绕科研对象和证据，而不是围绕聊天记录。

**新增文件：** - `vsagent/core/agent/research_workspace.py` -
`vsagent/core/agent/research_objects.py` -
`vsagent/core/agent/evidence_registry.py` -
`vsagent/core/agent/hypothesis_review.py` -
`tests/test_research_workspace.py`

**修改文件：** - `ledger.py`：保持计算产物 handle 机制；增加
ResearchObject 到 Ledger handle/file
的引用关系，不把所有科研对象都硬塞成 receptor/ligand/artifact。 -
`hypothesis.py`：复用现有
HypothesisTool，增加证据引用和反证状态的管理接口。 -
`event_log.py`：Event/Step 记录 task_id、agent_task_id、artifact_refs
等关联信息。 - `models.py`：持久化 Campaign/AgentTask
索引或轻量状态，不在 Session 主记录中内嵌大体积结构数据。

**Research Workspace 对象：** - Target - Structure - Pocket - Ligand -
Pose - Score - Evidence - Hypothesis - AgentReport

每个对象具有稳定 ID、来源、版本、创建任务、文件/handle
引用和状态。结构坐标、对接结果等大文件仍保存在 runs/，Workspace
保存引用和元数据。

**证据分类：** - 实验结构/实验活性； - 数据库记录； - 文献证据； -
计算预测； - 模型推断； - 用户提供的数据。

不能把计算预测与实验事实混成同一证据等级。

**假设闭环：** 1. Principal Agent 提出可检验研究问题。 2. 专业 Agent
分别收集支持证据和反证。 3. Hypothesis Review
汇总证据来源、质量、冲突和不确定性。 4. 更新现有
`TaskState.hypotheses`。 5. 决定支持、削弱、修正、暂缓或否定假设。 6.
证据不足时提出下一轮研究问题，而不是强行给肯定结论。 7.
超出原始目标或需要昂贵计算/外部实验时请求用户确认。

**验收：** - 任意结论可追溯到 Evidence/Artifact。 -
同一结构/口袋/配体在不同 Agent 间使用相同 ID。 -
支持证据和反证都可记录。 - 假设状态变化有事件记录。 -
报告明确区分计算预测、文献证据和实验验证。

### Phase 12：规则分级、状态机与系统级评测

**目的：**
最后再处理规则文件过载和全局行为控制，使规则与实际角色/阶段一一对应。

**新增目录：**

``` text
rules/
  global/
    scientific_integrity.md
    evidence_policy.md
    permission_policy.md
  agents/
    principal.md
    structure_pocket.md
    conformation.md
    ligand.md
    screening.md
    scoring.md
    mechanism.md
    literature.md
    hypothesis.md
  stages/
    structure_discovery.md
    pocket_analysis.md
    ligand_analysis.md
    screening.md
    evaluation.md
  references/
    structure_methods.md
    docking_methods.md
    scoring_methods.md
```

**修改文件：** -
`AGENTS.md`：保留为项目入口和规则索引，不再承担所有角色的全部细则。 -
`core/agent/prompt.py`：提供按角色和阶段拼装规则的函数。 -
`context.py`：把角色规则放在稳定前缀/专属层，把阶段信息放在动态上下文。 -
`hooks.py`、`permission.py`：接入硬约束检查。 -
`host.py`、`loop.py`：Campaign/AgentTask 状态变化时同步状态机。

**规则加载策略：** - Global Invariants：所有 Agent 加载，保持短小。 -
Agent Policies：按 role 加载。 - Stage Policies：按当前 TaskSpec
子阶段加载。 - Reference Knowledge：按需检索，不默认注入完整正文。 -
规则文件是指导和解释；关键禁止行为必须有 Runtime Validator 兜底。

**Campaign 状态机建议：**
`UNDERSTANDING → CLARIFICATION → SPECIFICATION → PLANNING → DELEGATING → RESEARCH → INTEGRATION → CRITICAL_REVIEW → DELIVERY`

允许： - `CRITICAL_REVIEW → REPLANNING → DELEGATING` - 任意非终态 →
`BLOCKED / CANCELLED / FAILED` - 用户修改目标时，当前计划版本标记
stale，重新编译 TaskSpec/CapabilityPlan。

**系统级评测：** - Intent Accuracy - Constraint Recall - Exclusion
Preservation - Clarification Precision - Capability Routing
Precision/Recall - Unnecessary Tool Exposure - Task Completion Rate -
Agent Duplication Rate - Dependency Violation Rate - Evidence Coverage -
Artifact Traceability - User Intent Fidelity - Cost / Latency / Token
Consumption

**验收：** - 与原单 Agent Baseline 做对照。 - 不以 Agent
数量、工具调用次数或输出长度作为成功指标。 -
重点证明：同样的科研问题下，意图约束保留更好、无关工具更少、任务交付更完整、证据链更可靠。

------------------------------------------------------------------------

## 4. 关键接口和代码接入点

### 4.1 `app.py`：只负责装配，不承担路由逻辑

当前 `_build_loop()` 和 `_build_registry()` 已经是应用装配入口。建议在
App 初始化时构造 Parser、Compiler、Registry、Router，并注入到 Loop
或单独的 PreflightService。

不要在 `_build_registry()` 里写"如果用户说口袋就注册 pocket
family"这类逻辑；Registry 负责装配，Router 负责决策，Adapter
负责执行决策。

### 4.2 `loop.py`：增加薄前置流程

当前 `_run_steps()` 负责模型请求循环，`_execute_tool()`
负责工具调用、Hook
和权限。新增流程应该在"新用户轮次开始、尚未请求模型"时运行：

``` python
async def prepare_turn(self, session: Session, user_message: Message):
    turn_context = self.task_context.resolve(session, user_message)
    intent = await self.intent_parser.parse(turn_context)
    task_spec = self.task_compiler.compile(intent, turn_context)
    plan = self.capability_router.route(task_spec)
    return self.route_adapter.apply(session, task_spec, plan)
```

这是概念接口，不要求照抄函数签名。需要保证： -
只在新任务/目标修改时重新编译； - 普通追问复用当前 TaskSpec； - shadow
模式不改变工具清单； - 解析或路由故障时可安全回退到旧执行方式； -
用户明确 exclusions 不允许因 fallback 而丢失。

### 4.3 `toolset.py`：提供稳定的 family API

不要让 Router 直接操作
`Session.tool_families`、`family_topic`。建议新增类似接口：

``` python
def ensure_families_loaded(
    session: Session,
    registry: ToolRegistry,
    families: Iterable[str],
    *,
    topic_index: int,
) -> FamilyLoadResult:
    ...
```

此函数内部复用 `catalog()`、disabled 状态和当前 family
生命周期规则。`LoadToolsTool` 与 RouteAdapter
都调用同一个底层函数，避免两套加载逻辑。

### 4.4 `models.py`：保持向后兼容

当前 `Session.to_dict()` / `Session.from_dict()` 已显式保存/恢复
`task_state`、`tool_families`、`family_topic`、父子 Agent 字段等。

新增字段必须： - 有默认值； - `from_dict()` 对旧状态缺字段安全； -
对未知 schema version 有明确处理； - 不把大型 AgentReport 或结构数据写入
Session 主 TOML； - 为计划/任务数据建立独立文件或轻量引用。

### 4.5 `host.py` / `spawn.py`：后期扩展，不在 Phase 1 改

`AgentHost.spawn()` 已经处理子 Session 创建、模型/Agent
模式继承、工具族继承、历史 fork、共享 Ledger 和后台启动。后续 Dispatcher
应调用 Host，不复制这些机制。

`spawn.py` 的自由文本 `task` 参数可以作为兼容入口保留；新路径通过
`AgentTask` 生成明确任务书，并将 task_id/campaign_id/role
等元数据绑定到子 Session。

------------------------------------------------------------------------

## 5. 首批专业 Agent 的职责边界

  ---------------------------------------------------------------------------------------------------------------------------------------
  Agent                 负责                                          不负责                         主要交付
  --------------------- --------------------------------------------- ------------------------------ ------------------------------------
  Principal             意图、任务分解、调度、整合、重规划            亲自执行所有底层计算           TaskSpec、CapabilityPlan、综合报告

  Structure & Pocket    结构检索、候选口袋发现、目录构建              擅自替用户确定最终受体/口袋    StructureCatalog、PocketCatalog

  Conformation          口袋几何/理化特征、构象差异、适配画像         将 docking score 当实验亲和力  PocketProfile、ConformationReport

  Ligand Intelligence   已知配体、活性、骨架、分子属性                未经授权扩大筛选库或生成分子   LigandCatalog、LigandProfile

  Literature            文献检索、证据定位、冲突整理                  将摘要推断当原文事实           EvidenceReport

  Virtual Screening     根据确认的受体/口袋和筛选库执行筛选           自行更改研究目标或确认节点     ScreeningRun、CandidateSet

  Scoring               独立评价已有结果、检查评分分歧和不确定性      无输入产物时声称完成评分       ScoringReport

  Mechanism             结合模式、关键残基、SAR 机制解释              把计算相关性写成已验证机制     MechanismReport

  Hypothesis/Evidence   支持/反证整理、假设更新、下一步判别实验建议   无证据地替假设背书             HypothesisReview
  ---------------------------------------------------------------------------------------------------------------------------------------

首批开发建议为 Principal、Structure & Pocket、Conformation、Ligand
Intelligence、Scoring。Literature
可以先以能力服务形式提供，等证据管理稳定后再独立成 Agent。

------------------------------------------------------------------------

## 6. 端到端示例与预期行为

用户输入：

> 获取 EGFR
> 的所有候选结合口袋，分析每个口袋的结构特征和可能适配的活性分子，不要直接进行分子对接。

### 6.1 IntentSpec 预期结果

``` json
{
  "primary_goal": "系统调查 EGFR 候选结合口袋并分析潜在配体适配特征",
  "intent_types": [
    "structure_discovery",
    "pocket_discovery",
    "pocket_profiling",
    "ligand_intelligence"
  ],
  "research_objects": {
    "target": "EGFR",
    "structure_id": null,
    "pocket_id": null,
    "ligand_set": null
  },
  "explicit_constraints": [
    "覆盖符合范围的候选结合口袋",
    "分析口袋结构特征",
    "分析潜在活性分子适配特征"
  ],
  "exclusions": [
    "不得直接执行分子对接"
  ],
  "missing_information": [
    "具体结构和口袋尚未指定"
  ],
  "clarification_required": []
}
```

注意：未指定结构和口袋不是阻塞条件，因为用户要求的正是发现它们。

### 6.2 TaskSpec 预期子目标

1.  调查 EGFR 相关实验结构和必要的预测结构。
2.  识别候选口袋并建立口袋目录。
3.  获取已知配体和活性证据。
4.  分析每个口袋的几何、理化和构象特征。
5.  建立口袋与已知/潜在配体的适配画像。
6.  输出可追溯的比较报告。

### 6.3 CapabilityPlan 预期结果

-   Structure & Pocket：启用 `structure`、`pocket`；使用常驻
    `survey_structures`。
-   Ligand Intelligence：按需启用 `chembl`、`pubchem`。
-   Literature：按需启用 `literature`、`corpus`。
-   Conformation：依赖结构、口袋和配体画像。
-   Virtual Screening：不选择。
-   Scoring：不选择。
-   `docking` 工具不应进入可执行计划；如果其所在 `scoring` family
    因其他必要能力被加载，也必须通过工具级 capability guard 阻止实际
    docking 操作。

### 6.4 最终交付标准

-   结构目录：每个结构有来源、分辨率/质量信息和 ID。
-   口袋目录：每个口袋有结构来源、残基、坐标/边界和发现方法。
-   配体画像：区分实验活性记录、文献报告和计算推断。
-   构象画像：说明结构状态、构象差异和不确定性。
-   比较报告：覆盖范围、未覆盖范围、排除原因和推荐后续研究。
-   明确说明本轮没有执行 docking。

------------------------------------------------------------------------

## 7. 测试与评测计划

### 7.1 测试集构成

建议先建立 50 条测试用例：

  类别                      数量 重点
  ----------------------- ------ --------------------
  单一研究对象/目标不明        5 不擅自启动复杂流程
  明确单任务                   8 正确识别单一能力
  多目标研究任务              10 多标签和子目标拆分
  明确约束/禁止事项            8 不遗漏 exclusions
  缺失信息与澄清               6 区分必要和可选输入
  多轮追问/任务修改            8 正确继承和修改状态
  历史任务恢复                 5 可靠引用旧任务产物

### 7.2 关键指标

  指标                         定义
  ---------------------------- ------------------------------------
  Intent Exact/Partial Match   主要意图与人工标注的一致性
  Constraint Recall            用户明确约束被保留的比例
  Exclusion Preservation       禁止事项保留率
  Clarification Precision      真正需要澄清时才追问的准确性
  Capability Precision         路由出的能力中真正需要的比例
  Capability Recall            应选能力被覆盖的比例
  Unnecessary Family Load      无关工具族加载数量
  Tool Violation Rate          违反 exclusions/权限的工具执行次数
  Dependency Violation         上游未完成却启动下游的次数
  Deliverable Completeness     必需交付物满足比例
  Evidence Coverage            重要结论具备证据引用的比例
  User Intent Fidelity         最终交付与原始用户目标的一致性

### 7.3 分阶段测试命令

先沿用当前项目已有测试入口：

``` bash
python tests/test_agent.py
python tests/test_smoke.py
```

Phase 0.5 的检查组沿用同一入口，不新建文件：

``` bash
python tests/test_agent.py --only core-tools-profile
python tests/test_agent.py --only rules-split
python tests/test_agent.py --only family-split
python tools/check_prefix.py
```

新增测试建议：

``` bash
python tests/test_agent_contracts.py
python tests/test_intent_parser.py
python tests/test_task_context.py
python tests/test_task_compiler.py
python tests/test_capability_registry.py
python tests/test_capability_router.py
python tests/test_route_adapter.py
python tests/test_task_validators.py
python tests/test_research_workspace.py
```

新增评测脚本：

``` bash
python tools/eval_intent_router.py --mode shadow
python tools/eval_intent_router.py --mode enforce
```

所有模型调用评测应支持固定模型、固定输入集、保存原始解析结果、保存路由计划和可复现的人工标注。测试不能只依赖"看起来回答合理"。

------------------------------------------------------------------------

## 8. 风险、回滚和版本兼容

### 8.1 主要风险

  ------------------------------------------------------------------------------------
  风险                    影响                     缓解
  ----------------------- ------------------------ -----------------------------------
  Parser 过度推断         用户没要求的工作被启动   显式/推断字段分离；缺失目标时澄清

  Parser 漏约束           执行违背用户要求         exclusions 独立字段；工具级 Guard

  Router 过度加载         工具定义仍然过多         子目标级路由；按 Topic 稳定加载

  Router 漏能力           任务无法完成             shadow 对比；fallback 机制

  TaskSpec 与 TaskState   状态不一致               明确静态合同/动态进度分工
  重复                                             

  Agent 报告膨胀上下文    多 Agent 比单 Agent 更贵 引用式报告、摘要预算

  Agent 重复派发          资源浪费、产物冲突       task_id 幂等和 DAG 节点状态

  多 Agent 结论冲突       错误综合                 Evidence Registry 和独立评审

  Session 格式不兼容      历史会话无法恢复         可选字段默认值和旧状态测试
  ------------------------------------------------------------------------------------

与 Phase 0.5 有关的两项风险单独列出，它们影响的是"减量"本身，不进入上表：

-   **核心工具裁剪过窄。** 子 Agent 缺少必要工具，转而用 bash 自行实现——
    `docs/review.md` 的 F1 就是这样绕了二十步。缓解：两档都保留
    `load_tools` 与 `bash`；分档表按"token 成本 × 使用频率"核对；裁剪后跑一轮
    真实任务验收，而不是只看工具定义变小。
-   **拆族影响旧会话。** 旧会话的 `tool_families` 与 `next_tools` 里可能留着
    `scoring`。读取与校验都要对未知族名容错，并补一条"旧状态文件仍能加载"的测试；
    否则会出现会话打不开，或者装族静默失效。

再加一条来自真实任务的：

-   **子 agent 会改共享状态。** 登记表按会话家族共享，"当前受体"只有一份；子 agent
    换掉它，父会话跟着变。实测已经发生过一次（准备受体 + 切换受体，父会话的三项
    产物被搁置）。缓解：`permission.WORKER_DENY` 先拦下这三个动作（2026-11-13 起改为
    按角色开放 + 准备类 ASK，见 P-80）；`write_run` 的
    `run_id` 冲突与领域工具的读写区分留给 Phase 9--10。**不要用"任务书写清楚"
    来代替这一条**——同一次实测里，任务书从松改到紧，步数从 12 降到 4，但那是因为
    恰好写了；没写到的动作它照样会做。

### 8.2 回滚策略

-   Phase 0.5：环境变量 `VSA_TOOLSET=full` 把工具档位与规则档位一起切回现状；
    拆族保留旧族名别名，读到 `scoring` 时按 `docking` 与 `affinity` 两族展开。
-   Phase 1--7：通过配置开关 `off/shadow/enforce` 回滚。
-   Phase 8：Validator 先 shadow 记录，再 enforce。
-   Phase 9--11：保留原 `spawn_agent` 自由文本兼容入口。
-   Phase 12：规则加载器保留旧 `AGENTS.md` fallback。
-   每次新增持久字段均可缺省读取；不要求一次性迁移全部旧会话。

### 8.3 建议配置项

``` toml
[agent.toolset]
# 常驻核心工具的分档：full 保持现状，orchestrator / worker 见 Phase 0.5。
# **尚未接入配置加载**：现在用环境变量 VSA_TOOLSET 顶（full / orchestrator / worker），
# 不设时按会话类型自动取 orchestrator 与 worker。
core_profile = "auto"
# 项目规则的切分：auto 跟随档位，full 一律加载整份
rules_profile = "auto"

[agent.intent]
enabled = true
mode = "shadow"  # off | shadow | enforce
parser_model = ""  # 空值表示继承当前模型配置
max_retries = 1

[agent.routing]
mode = "shadow"  # shadow | enforce
auto_load_families = false
allow_fallback = true

[agent.multiagent]
enabled = false
max_depth = 1
max_parallel_agents = 3
max_reports_in_parent = 5

[agent.validation]
mode = "shadow"  # shadow | enforce
require_output_contract = true
require_evidence_refs = true
```

配置字段最终应融入项目既有配置加载方式，不应为本次改造另建一套不受管理的配置入口。

------------------------------------------------------------------------

## 9. 第一轮实际开发的范围（建议严格控制）

**当前不要直接开始 Phase 8--12。** 第一轮只做 Phase
0.5--7，且按下列顺序合并 PR：

### PR-0：单 Agent 减量（前置，必须最先合并）

-   Phase 0.5：核心工具按会话类型分档、项目规则按是否面向用户切两段、拆开 `scoring` 族
-   不引入任何新协议，不改变执行语义
-   验收：主 Agent 核心工具不超过 8,000 token（下降 8% 以上，实测 8,552 → 7,818），
    子 Agent 不超过 4,800 token（下降 40% 以上，实测 7,988 → 4,213），全部检查组通过
-   这一条不合并，后续 PR 不开工

### PR-1：协议和基线

-   Phase 0 基线记录
-   Phase 1 contracts
-   不改变运行时行为

### PR-2：意图解析

-   Phase 2 Intent Parser
-   Phase 3 Task Context Resolver
-   独立测试，不改变工具清单

### PR-3：任务规格

-   Phase 4 TaskSpec Compiler
-   旧 TaskState 继续工作
-   Session 持久化只增加可选字段

### PR-4：能力路由

-   Phase 5 Capability Registry
-   Phase 6 Capability Router / DAG
-   Router 输出可解释的 CapabilityPlan

### PR-5：兼容接入

-   Phase 7 Route Adapter
-   先 shadow，再 enforce
-   不改变 AgentLoop 的工具执行、权限、Hook、重试和取消语义

每个 PR 都要求： 1. 单元测试通过； 2. 现有 `test_agent.py` 和
`test_smoke.py` 通过； 3. 记录新增/修改文件； 4.
提供一个端到端输入和解析/路由结果； 5. 不混入下一阶段的大规模改动。

------------------------------------------------------------------------

## 10. 最终文件改造地图

### 10.1 第一轮新增文件（Phase 0.5--7）

Phase 0.5 只改装配与判据，不引入协议。**已完成：**

``` text
vsagent/core/agent/toolprofiles.py          新增：两档工具名单与环境变量开关（纯数据）
vsagent/core/agent/coretools.py             新增：常驻工具的装配清单（顺序只写一处）
vsagent/app.py                              核心工具按档位过滤；按档位缓存注册表
vsagent/core/agent/prompt.py                项目规则按"是否面向用户"分两段；打分纪律改写
vsagent/core/agent/domain/all_tools.py      scoring 拆成 docking 与 affinity
vsagent/core/agent/toolset.py               旧族名展开；按装入的族算隐藏清单
vsagent/core/agent/event_log.py             next_tools 先展开旧族名再校验
vsagent/core/agent/permission.py            权限边界（Plan 禁令、准备类 ASK、worker 标记）
vsagent/core/agent/loop.py                  调用判定时按会话深度传 worker=True
tools/check_toolset.py                      新增：两档工具定义的预算判据
tools/check_prefix.py                       两档规则读数；打分族拆分后的断言
tests/test_agent.py                         新增 core-tools-profile / rules-split / family-split / worker-guard 组
```

Phase 1--7 新增模块：

``` text
docs/
  multi-agent-refactor/
    baseline.md
    architecture.md
    protocol-spec.md
    rollout-and-evaluation.md

vsagent/core/agent/
  contracts/
    __init__.py
    intent.py
    task.py
    capability.py
    agent.py
  intent_parser.py
  intent_schema.py
  task_context.py
  task_spec.py
  task_compiler.py
  capabilities.py
  capability_profiles.py
  capability_router.py
  research_dag.py
  route_adapter.py

tests/
  test_agent_contracts.py
  test_intent_parser.py
  test_task_context.py
  test_task_compiler.py
  test_capability_registry.py
  test_capability_router.py
  test_route_adapter.py
  fixtures/
    intent_cases.jsonl

tools/
  eval_intent_router.py
```

### 10.2 后续新增文件（Phase 8--12）

``` text
vsagent/core/agent/
  validators/
    __init__.py
    task_precondition.py
    output_contract.py
    capability_guard.py
    delivery.py
  multiagent/
    __init__.py
    profiles.py
    registry.py
    contracts.py
    reports.py
    dispatcher.py
    scheduler.py
    dependency.py
    coordinator.py
  research_workspace.py
  research_objects.py
  evidence_registry.py
  hypothesis_review.py

rules/
  global/
  agents/
  stages/
  references/
```

### 10.3 现有文件修改顺序

  -------------------------------- ------------- ----------------------------------
  文件                               预计阶段          改动原则
  -------------------------------- ------------- ----------------------------------
  `core/agent/domain/all_tools.py` Phase 0.5     scoring 拆成 docking 与 affinity

  `app.py`                         Phase 0.5/7/9 先分档装配核心工具，后续只做依赖注入

  `core/agent/prompt.py`           Phase 0.5/12  先按是否面向用户分两段，再按角色/阶段加载

  `core/agent/intent.py`           Phase 2       保留 IntentHint，只补充注释/兼容

  `core/agent/loop.py`             Phase 7       增加 preflight 调用，不重写执行循环

  `core/agent/context.py`          Phase 3/4/7   注入精简摘要，控制 token

  `core/agent/toolset.py`          Phase 6/7     增加 family API，复用现有机制

  `core/models.py`                 Phase 4/10    可选字段、默认值、旧状态兼容

  `core/agent/host.py`             Phase 10      复用生命周期和共享 Ledger

  `core/agent/spawn.py`            Phase 9/10    结构化任务书兼容现有自由文本入口

  `core/agent/permission.py`       Phase 8/10    增加角色能力检查，不替换用户权限

  `core/agent/hooks.py`            Phase 8/12    接入 validator

  `core/agent/ledger.py`           Phase 8/11    维持产物账本，补充引用关系

  `core/agent/event_log.py`        Phase 10/11   记录 task/agent/artifact 关联

  `core/agent/hypothesis.py`       Phase 11      复用现有假设状态，增加证据评审

  `AGENTS.md`                      Phase 0.5/12  保留唯一入口，先分两段，再按角色细化
  -------------------------------- ------------- ----------------------------------

------------------------------------------------------------------------

## 11. 完成定义（Definition of Done）

第一轮 Phase 0.5--7 完成，必须同时满足：

**减量（Phase 0.5）：**

-   根会话与子 Agent 的常驻核心工具分成两档：主 Agent 不超过 8,000 token，
    子 Agent 不超过 4,800 token，由 `tools/check_toolset.py` 守住。
-   项目规则分成两段，子 Agent 不加载"面向用户"的那一段，包括确认机制与语言规范；
    实测主 3,346 token、子 1,448 token。
-   `scoring` 族已经拆开，`dock` 不会因为评分能力被自动带进工具清单。
-   读数来自 `tools/check_toolset.py`、`tools/check_prefix.py` 与一轮真实会话，
    不来自估算：主 Agent 的工具定义下降 8% 以上（实测 8,552 → 7,818），
    子 Agent 下降 40% 以上（实测 7,988 → 4,213）。
-   旧会话仍能正常加载，旧族名 `scoring` 展开成 `docking` 与 `affinity` 两族。

**协议与路由（Phase 1--7）：**

-   用户输入能够被解析为结构化 IntentSpec。
-   新任务、追问、目标修改和历史恢复能够区分。
-   用户显式约束和禁止事项完整传递到 TaskSpec。
-   TaskSpec 包含明确的输入、交付物和验收条件。
-   Capability Router 能输出可解释的 Agent/能力/工具族计划。
-   用户未指定结构和口袋时，系统能够把"发现结构/口袋"作为任务，而不是错误地要求用户先提供
    ID。
-   用户明确禁止 docking 时，路由计划不启动 docking；执行侧也有
    exclusions guard 的后续实现计划。
-   Router 可以以 shadow 模式运行，不影响现有 AgentLoop。
-   enforce 模式可以由 Adapter 预加载正确工具族。
-   原有
    `TaskTool`、`TaskState`、`load_tools`、`spawn_agent`、权限、Hook、Ledger
    和领域工具继续可用。
-   所有既有测试通过，新增测试有固定样例和可复现结果。

最终目标不是让 VSAgent 拥有更多
Agent，而是让它能够准确理解科研问题，将其编译为明确的研究任务，选择最少但足够的专业能力，并通过证据和验收机制保证交付真正回答了用户的问题。
