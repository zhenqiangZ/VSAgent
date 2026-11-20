# 意图识别重构计划

日期：2026-11-17

## 一、目标与边界

### 目标

- 用户一句话先解析成五个维度：目标、对象、操作、约束、期望交付。
- 操作直接落到九个子 agent，不再经过能力名。
- 同一个科研任务跨轮延续，任务号不变，已有对象、结构、口袋和计算结果不丢失。
- 进入执行前先过一道规则验证，缺关键信息时先问，不把半成品意图交给主控。
- 上下文只传引用和摘要，不传完整聊天历史。

### 不做

- 不保留 `capabilities.py`、`CapabilityPlan`、`CapabilityRouter`、`role_of`、`capabilities_for`。
- 不把 `operations` 放开成自由动词。取值固定为九个角色名。
- 不让模型直接生成 `TaskSpec`。任务规格继续由 `TaskCompiler` 按规则生成。
- 不把完整对话历史塞给解析器。
- 不把 `dependencies` 放进 `TaskSpec`。依赖属于 `AgentPlan`。
- 不为旧会话写字段映射。

## 二、目标模型

### 2.1 操作就是子 agent

| 角色 | 负责的工作 | 典型选择时机 |
|---|---|---|
| `research` | 靶点身份、可成药性、疾病与临床信息；文献、本机语料与网页证据 | 问靶点本身、问临床或文献证据；筛选前需要先查清靶点 |
| `pocket` | 实验结构检索与目录；候选口袋识别；口袋几何与理化画像 | 要结构、要口袋、要口袋特征；筛选前结构和口袋未定 |
| `ligand` | 已知配体、活性、骨架与化学空间 | 找已知抑制剂、活性数据、骨架分布、可筛选配体集 |
| `prepare` | 受体准备、配体清理与准备 | 已有结构和配体，要整理成可直接对接的输入 |
| `docking` | 对接、构象搜索、位姿质量检查 | 要对接、要位姿、要对接打分 |
| `affinity` | 模型微调与亲和力预测 | 要预测亲和力或训练该靶点模型 |
| `analysis` | 多路结果整合、候选评价、类药性、一致性检查 | 比较结果、整合候选表、评价可靠性 |
| `mechanism` | 结合模式、关键残基、构效关系、支持与反证、假设状态 | 解释机制、找关键残基、验证假设 |
| `visualization` | 把已登记的结构、口袋、位姿、候选表做成视图页 | 要图、要页面、要可视化 |

`principal` 不进入操作列表。

### 2.2 IntentSpec

```python
@dataclass(frozen=True)
class ObjectRef:
    type: str          # target / structure / pocket / ligand / pose / score / artifact / hypothesis
    ref: str           # handle 或编号；新对象可以为空
    label: str         # 用户说的名字，例如“第二个口袋”
    origin: str        # existing / new / current

@dataclass(frozen=True)
class IntentSpec:
    intent_id: str
    task_relation: str          # new / continue / modify / compare / resume / none
    conversation_action: str    # execute / status_query / ask_result / clarify / cancel / chat
    goal: str
    objects: tuple[ObjectRef, ...]
    operations: tuple[str, ...]  # 九个角色名，去重并保持顺序
    constraints: tuple[str, ...]
    exclusions: tuple[str, ...]
    inputs: dict[str, Any]
    expected_outputs: tuple[str, ...]
    missing_information: tuple[str, ...]
    clarification_required: tuple[str, ...]
    confidence: float
    uncertainties: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    schema_version: int
```

约定：

- `constraints` 保留用户原话，逐条记录。结构化的已知输入放进 `inputs`。
- `missing_information` 记录当前还缺什么，可以包含“结构编号未定”。
- `clarification_required` 只记录不回答就无法开始的问题。发现类任务缺编号不算阻塞。
- `intent_id` 由解析器生成，不要求模型输出。
- 协议版本从 4 升到 5。

### 2.3 TaskSpec

字段保持现有形状，只改子目标：

```python
{
    "goal": "识别候选结合口袋并建立口袋目录",
    "agent": "pocket",
    "stage": "pocket_analysis",
}
```

`agent` 必填，取值必须是九个角色之一。`stage` 可空，有值时必须是五个阶段之一。

### 2.4 AgentPlan

```python
@dataclass
class AgentPlan:
    task_id: str
    revision: int
    selected_agents: tuple[str, ...]
    denied_agents: tuple[str, ...]
    provided_agents: tuple[str, ...]
    dependencies: list[dict[str, str]]
    parallel_groups: list[list[str]]
    unresolved_inputs: tuple[str, ...]
    approval_points: tuple[str, ...]
    rationale: tuple[str, ...]
```

`selected_capabilities`、`denied_capabilities`、`provided_capabilities` 全部删除。

## 三、两轴：任务关系与对话行为

### 3.1 取值

| `task_relation` | 含义 |
|---|---|
| `new` | 新建任务 |
| `continue` | 延续当前任务 |
| `modify` | 修改当前任务的目标或对象 |
| `compare` | 比较当前任务里的两组对象或结果 |
| `resume` | 恢复以前的任务 |
| `none` | 与任务无关 |

| `conversation_action` | 含义 |
|---|---|
| `execute` | 要执行或继续执行任务 |
| `status_query` | 问进度、做到哪一步 |
| `ask_result` | 问上一轮结果说明什么 |
| `clarify` | 回答上一轮待澄清的问题 |
| `cancel` | 终止任务 |
| `chat` | 闲聊或概念问答 |

### 3.2 组合后的行为

| 关系 | 行为 | 处理 |
|---|---|---|
| `new` | `execute` | 新建任务号，编译任务规格，路由 |
| `continue` | `execute`，操作为空 | 只合并新约束、新排除项、新输入，不重新编译 |
| `continue` | `execute`，有新操作 | 合并后版本加一，重新编译并路由 |
| `modify` | `execute` | 替换目标与对象，保留已有产物，版本加一 |
| `compare` | `execute` | 版本加一，增加 `analysis`，输入指向两组已有对象 |
| `resume` | `execute` | 找回旧任务，沿用任务号，不重建 |
| `continue` | `status_query` | 不编译，读任务状态与战役状态 |
| `continue` | `ask_result` | 不编译，读任务规格、报告与产物 |
| `continue` | `clarify` | 合并上一轮问题，重新验证 |
| `continue` | `cancel` | 任务状态改为 `cancelled`，停止正在跑的战役 |
| `none` | `chat` | 不建任务 |

### 3.3 比较任务

- 任务号不变。
- 版本加一。
- 操作里必须有 `analysis`；用户没说时由任务关系处理补上。
- `objects` 指向两个已有对象，`origin = existing`。
- 不重跑上游的口袋识别与对接。
- 缺少两个明确引用时进入 `CLARIFY`。

### 3.4 待澄清跨轮保存

给 `Session` 增加两个字段：

```python
pending_intent: dict[str, Any] | None   # 上一轮未完成的意图、问题、对应任务号、时间
route_note: str                          # 本轮路由提示，只在内存里，每轮重写
```

`pending_intent` 写进 `state.toml`。`route_note` 不写盘。

## 四、四个核心模块

### 4.1 Context Resolver

新建 `planning/context_resolver.py`。

输入：当前会话、用户这一句话、当前任务规格与任务状态、登记表、工作区、待澄清意图。

输出四段：

| 段落 | 内容 | 上限 |
|---|---|---|
| 当前任务 | 任务号、版本、目标、前五条要求、前五条禁止、状态、待澄清问题 | 500 字 |
| 科研对象 | 类型、handle、来源、顺序、一句摘要 | 500 字，最多 20 行 |
| 已有产物 | 产物类型、引用、路径或 handle、一句摘要 | 300 字，最多 10 行 |
| 上一轮对象 | 上一轮实际提到的对象，供“它”“那个”回指 | 150 字 |

总长度控制在一千五百字以内。超出时先删最旧、最不相关的条目，并保留一行“还有更多对象，可按需查询”。

指代处理：

1. 顺序指代由解析器确定：按对象表顺序把“第一个”“第二个”对应到具体引用。
2. 语义绑定由模型完成：模型从引用表里选一个，写进 `objects`。
3. 引用核对由验证器完成：引用不存在或顺序指代有歧义时进入 `CLARIFY`。

### 4.2 Intent Parser

保留一次模型调用、最多重试一次的做法。

提示词结构：

1. 固定头部：五个维度、两轴、对象引用写法、澄清规则。
2. 角色目录：由 `roles/catalog.py` 生成。
3. 常见组合：完整虚拟筛选、比较、机制解释等。
4. 固定尾部：硬规则。

硬规则：

- `operations` 只能填九个角色名。
- `execute` 至少要有一个操作；只补约束时可以没有操作。
- 用户明确禁止的操作不要选。
- 用户说“虚拟筛选”：选 `ligand`、`docking`；结构和口袋未定时加 `pocket`；`prepare` 由系统自动补。
- 用户说“比较”：`task_relation = compare`，操作里要有 `analysis`。
- 用户问进度：`conversation_action = status_query`，不要选操作。
- 用户说“停”：`conversation_action = cancel`。
- 指代对象时优先写已有 `ref`；新对象写 `origin = new`。

提示词总长度控制在五千字符以内。

### 4.3 Intent Validator

纯规则，不调用模型。

```python
@dataclass(frozen=True)
class Reason:
    code: str
    field: str
    message: str

@dataclass(frozen=True)
class Verdict:
    decision: str   # accept / clarify / update / reject
    reasons: tuple[Reason, ...]
    clarification: tuple[str, ...]
```

检查项：

| 检查项 | 规则 |
|---|---|
| 形状 | 目标非空；执行类至少一个操作；操作都在九个角色里；重复操作去重 |
| 引用 | `origin = existing` 必须有 `ref`，且能在对象表或登记表里找到；顺序指代只能有一个候选 |
| 完整性 | 按操作查必要输入；发现类任务不因缺结构编号、口袋编号而追问 |
| 一致性 | 操作不能同时出现在排除项里；约束与操作不能互相否定 |
| 可执行性 | 角色存在、模板存在、工具名单非空、前置能补齐或已有产物可用 |
| 授权 | 需要确认的操作要有对应确认点；没有时记一条提示，不直接拒绝 |

决策：

| 决策 | 处理 |
|---|---|
| `ACCEPT` | 继续编译与路由 |
| `CLARIFY` | 保存 `pending_intent`，把问题写进 `route_note`，这一轮先问用户 |
| `UPDATE` | 继续走修改或比较分支，先更新任务规格 |
| `REJECT` | 说明原因，不编译、不派发 |

规则层查不了真正的语义正确性。默认不做模型复核；在 `enforce` 模式下，只对“置信度低于 0.6”或“操作超过三个”的解析结果可选复核一次。

### 4.4 Task Specification Builder

继续使用 `planning/task_compiler.py`，保持纯规则、可重复。

| 角色 | 模板步骤（阶段） | 交付物 |
|---|---|---|
| `research` | 靶点与文献调研（无阶段） | `ResearchDossier` |
| `pocket` | 结构检索（结构发现）→ 口袋发现（口袋分析）→ 口袋画像（口袋分析） | `PocketAtlas` |
| `ligand` | 已知配体、活性与骨架盘点（配体分析） | `LigandDossier` |
| `prepare` | 受体准备（筛选执行）→ 配体准备（筛选执行） | `PreparedReceptor`、`PreparedLigandSet` |
| `docking` | 对接与位姿检查（筛选执行） | `CandidateSet` |
| `affinity` | 模型微调与亲和力预测（评价交付） | `AffinityModel`、`AffinityPrediction` |
| `analysis` | 多路结果整合与候选评价（评价交付） | `ScoringReport` |
| `mechanism` | 结合机制、关键残基与假设审查（评价交付） | `MechanismReport` |
| `visualization` | 已登记对象出视图页（无阶段） | `ViewPage` |

排除规则：

```python
(
    (("对接", "分子对接"), ("docking",)),
    (("筛选", "虚拟筛选"), ("prepare", "docking")),
    (("亲和力", "亲和力预测"), ("affinity",)),
    (("机制", "机制分析"), ("mechanism",)),
    (("可视化", "出图", "画图"), ("visualization",)),
)
```

## 五、路由与执行

### 5.1 AgentRouter

保留：

- `PREREQUISITES`：角色级前置。
- `AUTO_PREREQUISITES`：`prepare` 补 `pocket`、`ligand`；`docking`、`affinity` 补 `prepare`。
- `SINK_ROLES`：`visualization` 排在最后。

删除：

- `ENTRY_CAPABILITIES`、`PROVIDED_KEYS`、`_expand()`、`_capabilities_of()`、`role_of()`、`capabilities_for()`。

调整：

- `analysis` 的前置改成空。它读的是已有结果。至少有一份上游产物由验证器的完整性检查负责。
- `DENIAL_RULES` 改成角色级。
- `ROLE_PROVIDED_INPUTS` 只保留两条：`pocket` 由 `pocket` 输入满足，`ligand` 由 `ligand_set` 输入满足。

### 5.2 执行循环

`RouteDecision` 增加：

```python
relation: str
action: str
verdict: IntentVerdict | None
clarification: tuple[str, ...]
```

| 动作 | 执行层处理 |
|---|---|
| `execute` | 写 `task_spec`，记录 `_turn_plan`，照常让模型决定是否派发 |
| `status_query` | 不重写 `task_spec`，`route_note` 写“本轮只回答进度，不要重新派发” |
| `ask_result` | 不重写任务，`route_note` 写“本轮只解释已有结果，不要重新计算” |
| `clarify` | 保存 `pending_intent`，`route_note` 写要问的问题，模型据此发问 |
| `cancel` | `enforce` 模式下调用 `spawner.cancel_campaign(session.id)`，任务状态改为 `cancelled`；`shadow` 模式只记录 |
| `chat` | 不建任务，不派发 |

`route_note` 放进 TASK 层的“当前任务复述”一段，模型能看到。`PartType.ROUTE` 仍然只给界面看。

### 5.3 模式

| 模式 | 行为 |
|---|---|
| `off` | 不解析、不验证、不路由 |
| `shadow` | 照常解析、验证、编译、路由，并记任务规格（否则下一轮没有上下文）；取消与待澄清只在 `enforce` 生效，不拦截派发 |
| `enforce` | 验证通过才编译与派发；`CLARIFY` 先问；`REJECT` 不执行；`cancel` 真正停止战役 |

## 六、文件清单

### 新增

| 文件 | 内容 |
|---|---|
| `vsagent/core/agent/roles/catalog.py` | 角色、工作说明、权限、操作、报告要求、时限、排除规则 |
| `vsagent/core/agent/planning/context_resolver.py` | 四段上下文与引用表 |
| `vsagent/core/agent/planning/intent_validator.py` | 规则验证与四种决策 |
| `vsagent/core/agent/planning/agent_router.py` | 角色前置、依赖、计划装配 |
| `vsagent/core/agent/roles/contracts/plan.py` | `AgentPlan` |
| `tools/check_agents.py` | 角色、操作、模板、阶段、提示词覆盖的判据 |

### 修改

| 文件 | 改动 |
|---|---|
| `roles/contracts/intent.py` | 五维字段、两轴、`ObjectRef`、空操作规则 |
| `roles/contracts/task.py` | 子目标必须有 `agent`，`stage` 受限 |
| `roles/profiles.py` | 能力改成工作说明，权限直接读目录 |
| `roles/prompt.py` | 角色身份段改成“负责的工作” |
| `planning/context.py` | TASK 层加入 `route_note` |
| `planning/intent_parser.py` | 新提示词、角色目录、对象表、待澄清 |
| `planning/task_context.py` | `new/continue/modify/compare/resume/status/cancel/clarify` 分支 |
| `planning/task_compiler.py` | 按角色取模板，子目标写 `agent` 与 `stage` |
| `planning/stages.py` | 直接读子目标的 `stage` |
| `planning/route_adapter.py` | 接入上下文解析、验证器、两轴与待澄清 |
| `execution/loop.py` | 按动作处理，写 `route_note`，取消战役，记录新 trace 字段 |
| `execution/coordinator.py` | 直接按 `subgoals[].agent` 匹配角色 |
| `execution/spawn.py` | 改用 `AgentRouter` |
| `models.py` | `Session.pending_intent`、`Session.route_note` 与序列化 |
| `validators/__init__.py` | 删除未使用的 `CapabilityPlan` 导入 |
| `tools/check_all.py` | 判据名 `capabilities` 改成 `agents` |
| `tools/eval_system.py` | 能力字段改成角色字段 |
| `tools/eval_intent_parser.py` | 样例期望改成九个角色名与两轴 |
| `tests/test_agent.py` | 协议、阶段、编译、路由、解析、协调器、前缀断言 |
| `docs/design.md`、`docs/ten-agent-design.md`、`docs/intent-flow.md`、`dist/AGENTS.md` | 删除能力层描述，补五维与两轴 |

### 删除

| 文件 | 原因 |
|---|---|
| `roles/capabilities.py` | 能力层取消 |
| `planning/capability_router.py` | 由 `agent_router.py` 替代 |
| `roles/contracts/capability.py` | 由 `plan.py` 替代 |
| `validators/capability_guard.py` | 改成 `tool_guard.py` |
| `tools/check_capabilities.py` | 由 `check_agents.py` 替代 |

## 七、测试与判据

新增单元测试：

1. `IntentSpec` 两轴组合：合法组合通过，非法组合被拒。
2. 空操作只在 `continue`、`clarify`、`status_query`、`ask_result`、`cancel`、`chat` 下允许。
3. `ObjectRef`：已有对象必须有 `ref`，新对象可以没有。
4. 上下文解析：对象表、产物表、待澄清段都在预算内；顺序指代只有一个候选。
5. 验证器：六个检查项各有正例与反例；四种决策各有覆盖。
6. `compare`：任务号不变、版本加一、增加 `analysis`、不重跑上游。
7. `continue` 加新操作：版本加一；只加约束：版本不变。
8. `status_query`、`ask_result`、`chat` 不编译任务。
9. `cancel`：任务状态改为 `cancelled`，战役停止。
10. `clarify`：待澄清写入、下一轮合并、验证通过后清空。
11. 路由：自动补前置、拒绝角色、用户自带口袋时跳过补角色。
12. `AgentPlan` 往返一致，依赖不指向未选角色。

修改现有测试：

- 阶段测试：删除 `CAPABILITY_STAGE`，改为检查子目标 `stage`。
- 编译测试：能力名换成角色名。
- 协调器测试：`selected_capabilities` 断言删除，改成 `selected_agents`。
- 意图解析测试：提示词包含九个角色与各自工作；长度上限从 4000 调到 5000。
- 前缀测试：角色身份段从“注册能力”改成“负责的工作”。

新增判据：

1. 九个角色各有一份模板。
2. 每个模板的 `agent` 都是已知角色。
3. 每个模板的 `stage` 都在五个阶段里。
4. 每个角色的工作说明都写进意图提示词。
5. 任务规格里不再出现 `capability` 键。
6. 代码里不再出现 `CapabilityPlan`、`CapabilityRouter`、`role_of`、`capabilities_for`。
7. `PREREQUISITES` 无环，自动补的前置是前置表的子集。
8. `ROLE_PROVIDED_INPUTS` 的键都在输入键表里。

## 八、数据迁移

1. 备份 `~/.vsagent/state.toml`。
2. 找出 `task_spec.subgoals` 里仍带 `capability` 的会话。
3. 删除这些会话，或清空它们的 `task_spec` 与 `pending_intent`。
4. 不写字段映射，不读旧能力名。
5. 协议版本升到 5，旧版本任务规格在读取时明确报错。

## 九、实施阶段

### 第一阶段：数据模型与目录

交付：`roles/catalog.py`、`contracts/intent.py`、`contracts/plan.py`、`contracts/task.py`、`Session.pending_intent`、`Session.route_note`、协议测试。

完成标准：九个操作、九个角色、五个阶段、四个确认点的常量都能互相核对；新协议往返一致；旧能力名不再出现在协议层。

### 第二阶段：上下文解析

交付：`planning/context_resolver.py`、`RouteAdapter` 接入、预算测试与指代测试。

完成标准：对象表、产物表、待澄清段都有上限；“第二个口袋”只有一个候选时能落到具体引用；解析器输入中不出现完整聊天历史。

### 第三阶段：意图验证

交付：`planning/intent_validator.py`、六类检查与四种决策、待澄清保存与恢复。

完成标准：非法操作、失效引用、冲突约束都能被拦住；发现类任务不因缺编号而追问；`shadow` 只记录，`enforce` 才拦截。

### 第四阶段：任务关系与编译

交付：`task_context.py` 新分支、`task_compiler.py` 按角色模板、`stages.py` 直接读阶段。

完成标准：八种行为各有测试；比较任务不重跑上游；同一份意图编译两次结果一致。

### 第五阶段：路由与执行

交付：`agent_router.py`、`coordinator.py`、`spawn.py`、`loop.py`、`route_adapter.py`、取消战役与路由提示。

完成标准：自动补前置正确；用户自带口袋时不再补 `pocket`；`route_note` 能让模型看到本轮行为；`cancel` 能真正停下正在跑的战役。

### 第六阶段：提示词、判据、文档、迁移

交付：新意图提示词、`check_agents.py`、`check_all.py`、`eval_intent_parser.py`、`eval_system.py`、文档更新、旧会话清理。

完成标准：九条判据全部通过；意图提示词覆盖九个角色与九项操作；文档里不再出现能力层描述；旧会话不再带 `capability` 字段。

## 十、风险与应对

| 风险 | 应对 |
|---|---|
| 提示词变长，解析成本上升 | 角色目录控制在 1500 字，总提示词不超过 5000 字 |
| 上下文表变长，挤占窗口 | 四段各有上限，总量不超过 1500 字 |
| 解析器把“它”指错 | 顺序指代只给一个候选；多个候选一律澄清 |
| 比较任务重复跑上游 | 比较只增加 `analysis`，输入指向已有引用 |
| 取消误伤其他会话 | 只取消当前根会话的战役 |
| 模型看不到路由结果，仍然派发 | `route_note` 进入 TASK 层；派发前仍按任务状态与前置判据拦一次 |
| 旧会话读不出任务 | 备份后删除旧会话 |
| `analysis` 前置放宽后出现空跑 | 验证器要求至少一份上游产物 |

## 十一、完成标准

1. `operations` 只有九个角色名，代码里没有能力名。
2. `task_relation` 与 `conversation_action` 两轴完整实现。
3. 比较、状态查询、取消、澄清四种行为都有测试。
4. 上下文解析能提供对象表与产物表，且不超过预算。
5. 验证器能输出 ACCEPT、CLARIFY、UPDATE、REJECT。
6. 任务号跨轮保持稳定，修改与比较只增加版本号。
7. `AgentPlan` 只记角色、依赖、未决输入与确认点。
8. 九条判据全部通过。
9. 文档与 `dist/AGENTS.md` 不再出现能力层描述。
10. 旧会话清理完成，不保留兼容读取。
