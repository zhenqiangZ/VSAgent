# 搜索增强推理（SAR）改造计划

> 状态：已实施（2026-11-15，路线 B）。代码在 `vsagent/core/agent/search_reasoning/`，
> 测试组是 `search-reasoning` 与 `session-report`，离线评测脚本是 `tools/eval_sar.py`。
> 触发点从「用户提问后」改成「第一批文献、网页、语料结果返回后」：先在结果进入上下文
> 之前写下先验答案，再整理证据。原始检索结果不进入模型上下文，筛选后的证据进入 EVIDENCE 层。
> 应用层默认 `VSA_SAR=shadow`，测试与直接构造 `AgentLoop` 默认 `off`，避免改变既有请求条数。

## 一、目标与边界

目标：检索结果返回后，先在结果进入上下文之前写下先验答案与可核对的说法；再把检索结果整理成证据，补充事实、校正判断、提供证据；最后由模型在先验答案上选择性整合并核对，查询结果不接管回答权。

三条边界：

- 不重写 `AgentLoop`。在现有循环的「工具结果回写后」与「最终答复前」两个入口接入。
- 不替换现有查询工具。文献、网页、语料工具继续取数，`search_reasoning/` 只处理证据。
- 不训练模型。整合规则用提示文字、数据校验与报告字段表达。

触发与模式：

| 项目 | 规则 |
| --- | --- |
| 触发工具 | `search_literature`、`search_europepmc`、`read_fulltext`、`search_web`、`fetch_webpage`、`search_corpus`、`read_source` |
| 触发时机 | 这一步全部工具执行完成之后，且至少转换出一条证据 |
| 触发次数 | 一轮一次 |
| 不触发 | 数据库、结构、计算、模型预测、`agent_status`、失败调用、零命中 |
| 状态模式 | `rag`，基线来源 `prior` |

新增环境开关 `VSA_SAR`，取值 `off`、`shadow`、`enforce`。`off` 与当前行为一致；`shadow` 照常建立状态与评估，但最终答复仍用模型原稿；`enforce` 使用整合后的答复。应用装配层默认 `shadow`，测试与直接构造 `AgentLoop` 默认 `off`。

## 二、代码位置

方案里的文件路径按当前五组结构换算：

| 方案 | 实际位置 |
| --- | --- |
| `core/agent/loop.py` | `vsagent/core/agent/execution/loop.py` |
| `core/agent/context.py` | `vsagent/core/agent/planning/context.py` |
| `core/agent/intent.py` | `vsagent/core/agent/planning/intent.py` |
| `core/agent/toolset.py` | `vsagent/core/agent/toolset.py` |
| `core/agent/domain/all_tools.py` | `vsagent/core/agent/domain/all_tools.py` |
| `core/agent/memory.py` | 长期记忆在 `vsagent/core/memory/`；工具层是 `vsagent/core/agent/state/memory.py` |
| `core/agent/event_log.py` | `vsagent/core/agent/state/event_log.py` |
| `core/agent/ledger.py` | `vsagent/core/agent/state/ledger.py` |
| `core/agent/hooks.py` | `vsagent/core/agent/execution/hooks.py` |
| 新增 `search_reasoning/` | `vsagent/core/agent/search_reasoning/` |

上下文现在是八层：`PREFIX`、`PROFILE_MEMORY`、`PROJECT_MEMORY`、`TASK`、`EVENT`、`RECENT`、`EVIDENCE`、`HISTORY`。方案里的 `MEMORY` 对应其中两层，不合并。

## 三、新增目录

```text
vsagent/core/agent/search_reasoning/
    __init__.py
    models.py          # Claim / SearchEvidence / EvidenceAssessment / AnswerState
    jsonio.py          # 模型 JSON 输出读取
    answer_state.py    # 先验答案建立、状态迁移、文件读写
    evidence.py        # 整理、去重、评估、分类、筛选
    integrator.py      # 说法映射、选择性更新、冲突处理
    verifier.py        # 完整性、来源、改变、任务限制
    policy.py          # 阈值、权重、允许的整合动作
    prompts.py         # 各阶段提示文字
    events.py          # 事件名字与载荷
    adapters/
        __init__.py    # 适配器表与触发工具集合
        base.py
        literature.py
        web.py
        corpus.py
        database.py
```

另外新增 `vsagent/core/memory/admission.py` 与 `tools/eval_sar.py`。

## 四、数据结构

### Claim

```python
@dataclass
class Claim:
    id: str
    content: str
    category: str          # method / fact / mechanism / prediction / task
    confidence: float      # 模型内部判断，不用于证据排序
    evidence_ids: list[str]
    status: str            # initial / supported / partially_supported / unverified / conflicted / revised / rejected
    rationale: str = ""
```

`status` 只由整合与核对阶段改动；初始阶段一律 `initial`。

### SearchEvidence

```python
@dataclass
class SearchEvidence:
    id: str
    content: str
    source: str
    source_type: str       # literature / web / corpus / database / computational / model_prediction
    title: str = ""
    url: str = ""
    published_at: str = ""
    retrieval_score: float | None = None
    reliability: float | None = None
    metadata: dict = field(default_factory=dict)
```

`retrieval_score` 是搜索引擎给的相关程度；`reliability` 是证据评估给的来源可靠程度。两个分数分开存。

### EvidenceAssessment

```python
@dataclass
class EvidenceAssessment:
    evidence_id: str
    related_claim_ids: list[str]
    relation: str          # supporting / supplementary / contradictory / irrelevant
    support_level: str     # direct / partial / contextual / none
    integration_action: str  # retain / append / strengthen / revise / reject
    rationale: str = ""
```

一条证据可以对应多条说法，一条说法可以对应多条证据。评估必须写清这条证据支持或反驳的是哪一条说法。

### AnswerState

```python
@dataclass
class AnswerState:
    answer_id: str
    question: str
    mode: str              # augmented / research
    initial_answer: str
    initial_claims: list[Claim]
    knowledge_gaps: list[str]
    evidence_pool: list[SearchEvidence]
    assessments: list[EvidenceAssessment]
    revised_claims: list[Claim]
    final_answer: str = ""
    revision: int = 0
    status: str = "initial"   # initial / searching / integrating / verifying / final / failed
    created_at: float = 0.0
```

`AnswerState` 与 `TaskState` 分开：`TaskState` 记任务进度、待办、阻塞与假设；`AnswerState` 记这一轮回答的初始框架、证据与改动。

存放位置：不写进 `state.toml`，按会话与轮次写成 `~/.vsagent/answers/<session_id>/<answer_id>.json`。`AgentLoop` 在一轮开始时建立内存对象，轮次结束时写文件。事件记录只带 `answer_id` 与计数，正文从文件读。

## 五、准备阶段与十个阶段

### Phase 0：观测底座

**目标**：让报告读到主会话与子会话的全部记录。后续核对与实验都要读完整记录。

**文件**：`execution/report.py`、`tools/report_session.py`。

**做法**：读取主会话记录文件时，保留主会话与它派出的子会话记录；按 `session_id` 分组统计，不再丢弃。一轮是否结束以 `turn.done` 为准，没有时再看最后一条状态记录。

**验收**：`ses_0037` 的报告显示 64 步、97 次工具调用、11 次失败，第二轮显示「已结束」。

### Phase 1：数据协议

**目标**：建立四个记录对象，能序列化与反序列化。

**文件**：新增 `models.py`、`answer_state.py`。

**做法**：

- 四个 dataclass 按第四节定义；每个提供 `to_dict()` 与 `from_dict()`。
- `AnswerState` 提供 `add_evidence()`、`add_assessment()`、`revise_claim()`、`snapshot()`。
- 状态取值用元组固定，写入时校验；不认识的取值抛 `SearchReasoningError`。
- 序列化用普通 JSON；旧会话没有这些文件时按「没有答案状态」处理。

**验收**：四个对象完整往返；同一份状态写入后读回，说法、证据、评估、修订号一致；不改现有 `Session` 字段。

### Phase 2：检索触发

**目标**：第一次文献、网页、语料结果返回后启动 SAR，不再在提问后判断模式。

**文件**：`adapters/__init__.py`、`evidence.py`；在 `execution/loop.py` 的 `_process_search_results` 中接入。

**做法**：

- 触发工具只有三类：文献（`search_literature`、`search_europepmc`、`read_fulltext`）、网页（`search_web`、`fetch_webpage`）、语料（`search_corpus`、`read_source`）。
- 工具状态必须是 `COMPLETED`，且至少转换出一条证据。
- 这一步全部工具执行完成之后统一判断，一轮只触发一次。
- 数据库、结构、计算、模型预测与 `agent_status` 不触发；答案状态已经建立后，它们的证据仍可并入证据池。
- 失败调用与零命中不触发。
- 触发时发布 `retrieval_observed`，并写一条 TUI 过程行。

**验收**：第一次检索之前没有 SAR 请求；只有数据库或计算工具的轮次不建立答案状态。

### Phase 3：先验答案

**目标**：在检索结果进入上下文之前，让模型按已有理解写下一份先验答案、说法清单、知识缺口与后续查询。

**文件**：`answer_state.py`、`prompts.py`；由 `execution/loop.py` 的 `_start_from_retrieval` 调用。

**做法**：

- 一次模型请求，输入是用户问题、任务状态、工具调用之前已经写下的叙述、上一轮助手答复、已有记忆摘要。
- 不把当前这一步的工具输出放进请求。
- 输出 JSON：先验答案、3 到 7 条说法、知识缺口、查询问题。
- 建立 `AnswerState`，`mode="rag"`、`baseline_source="prior"`，记录 `trigger_tool` 与 `trigger_step`，写法一律 `initial`。
- 立刻写答案状态文件；发布 `answer_initialized`；写一条 TUI 过程行。
- 文献、网页、语料的原始结果写 `sar_suppressed` 与 `sar_model_output`，模型只看到「已整理为证据、原文按步号取回」的提示；数据库与计算结果保留原文。
- SAR 失败或轮末时解除抑制，原始结果回到上下文。

**验收**：先验请求不含当前工具输出；下一步请求里没有三类检索的原始正文；状态文件在下一步请求之前存在；说法数在 3 到 7 条之间；知识缺口至少一条。

### Phase 4：查询结果处理

**目标**：把不同查询工具的结果整理成统一的证据对象，去重、评估、分类，并映射到具体说法。

**文件**：新增 `evidence.py`、`adapters/*`；在 `execution/loop.py` 的工具结果回写处调用。

**做法**：

- 适配层按工具名分四类：
  - 文献：`search_literature`、`search_europepmc`、`read_fulltext`。
  - 网页：`search_web`、`fetch_webpage`。
  - 语料：`search_corpus`、`read_source`。
  - 数据库：`uniprot`、`chembl`、`pubchem`、`opentargets`、`pdb`。
- `docking`、`affinity`、`analysis` 属于计算产物，登记成 `computational` 或 `model_prediction`，不套用文献证据的评价规则，也不直接修改文献类说法。
- 整理：保留标题、正文片段、来源、时间、链接、编号；正文只留与当前说法相关的片段。
- 去重：按 URL、DOI、标题、内容指纹依次判断；同一来源的多次命中合并。
- 评估：相关程度、来源可靠程度、时间适用性、直接支持程度四项。相关程度先用词面重合，装了向量模型时再加语义相似度；来源可靠程度按类型给基线，实验论文高于综述，综述高于预印本，预印本高于普通网页；时间适用性按问题是否要求近期；直接支持程度看证据里有没有对应说法的主体与结论。
- 综合分 `Q = αR + βS + γT + δD`，权重放在 `policy.py`，按模式给两组，不在代码里固定一组覆盖所有任务。
- 分类：先按规则判支持、补充、冲突、无关；规则判不出的，批量交给一次模型请求，要求输出关系与理由。规则能判的不交给模型。
- 数量：每个查询方向保留 3 到 5 条高价值结果；无关与低质量结果留在证据池里标 `irrelevant`，不进入最终生成上下文。
- 发事件 `evidence_collected` 与 `evidence_assessed`。

**验收**：同一批结果里四类都能分开；相关但不支持具体说法的结果不会被算成支持；无关结果不进入最终上下文；每条评估都能指到一条说法。

### Phase 5：证据整合

**目标**：只改动受有效证据影响的说法，保留未受影响的说法，处理冲突。

**文件**：新增 `integrator.py`、`policy.py`、`prompts.py`。

**做法**：

- 输入：用户问题、先验答案、说法清单、筛选后的证据、证据评估。
- 输出：修订后的说法清单与最终答案；每条改动写明依据的证据编号。
- 五种动作：`RETAIN` 原说法不动；`APPEND` 增加新说法；`STRENGTHEN` 补上依据；`REVISE` 修改说法；`REJECT` 不进入答案。
- 规则校验：
  - 没有挂到具体说法的证据，不允许改动说法。
  - 只有相关程度高、没有相反结论的证据，不允许判成反证。
  - 来源可靠程度低于阈值的证据，不允许单独推翻原说法。
  - 预测与推断不能写成实验事实；说法类别与证据类别要对得上。
  - 冲突证据必须保留双方来源，结论写成有条件的表述。
- 冲突处理：把相反证据按数据集、指标、实验条件、适用范围分组，提示模型逐项比较；输出可以是「在某种条件下成立」，不能简单选最新一篇。
- 只更新有动作的说法，其余原样保留；最终答案按原结构组织，不把证据摘要直接拼在末尾。
- 发事件 `claim_updated`。

**验收**：只改动一条说法时，其余说法原样保留；每条修订都能指到证据；冲突场景输出有条件结论，双方来源都保留。

### Phase 6：接入 AgentLoop

**目标**：在不改变权限、钩子、工具执行顺序的前提下，接入两个入口。

**文件**：`execution/loop.py`、`execution/dispatcher.py`、`roles/capabilities.py`、`planning/task_context.py`。

**做法**：

- 在工具结果回写之后调用 `_process_search_results`：只处理适配层认得的查询工具；文献、网页、语料第一次出证据时建立先验答案。
- 在最终答复产生后调用 `_finalize_answer`：整合、核对、决定发布哪份正文。
- `VSA_SAR=shadow` 时，整合与核对照常跑并写记录，最终正文仍用模型原来的答复；`enforce` 时用整合后的正文。
- 最终的呈现：SAR 生效的轮次，模型最后一步的正文先当作草稿，不立即作为用户可见答复；整合与核对完成后一次性发布。事件流里保留草稿与最终稿的编号，网页报告能对照。这样用户不会先看到一份草稿、再看到一份改写的答案。
- 工具可见性：确认需要查资料的子会话能看见 `corpus`、`literature`、`websearch` 族；主会话不加领域族。工具族按主题借用、主题结束收回。
- 失败处理：先验答案、整合、核对任一步抛 `SearchReasoningError`，按当前轮的错误处理显示，不静默降级；`VSA_SAR=off` 时不进入这些代码。

**验收**：没有检索的轮次与当前版本的请求次数、工具调用、权限检查一致；检索触发的轮次能看到先验答案与最终整合两个入口；工具执行顺序、钩子与权限没有被绕过；enforce 的最终正文来自整合结果。

### Phase 7：上下文装配

**目标**：把答案状态与筛选后的证据放进现有八层，不增加新层，不重复注入完整查询历史。

**文件**：`planning/context.py`、`execution/loop.py`。

**做法**：

- `TASK` 层增加一段「本轮回答基线」：先验答案摘要（不超过 400 个文字单位）、说法清单（最多 6 条，一行一条）、知识缺口（最多 5 条）。
- `EVIDENCE` 层放筛选后的证据：每条带来源、证据类别、关系、支持到哪条说法；最多 5 条，正文按预算展开。
- 文献、网页、语料的原始结果不进入模型上下文，只剩一句「已整理为证据、原文按步号取回」的提示；原文留在 Part 上，`read_turn` 可以取回。
- 数据库与计算结果的原始正文保留；它们不触发 SAR。
- 完整证据池留在独立文件里；上下文只放当前说法需要的部分。
- 预算：`ANSWER_LIMIT = 800`；`EVIDENCE_MAX` 保持 6000；装不下时先压回答状态正文，再压证据详情，最后按现有降级链处理。
- `PREFIX` 一个字节不动；答案状态属于易变内容，放 `TASK`，不进 `system`。
- 发 `context.metrics` 时带上 `answer` 与 `evidence` 两段用量。

**验收**：上下文里能看到先验答案、说法、缺口与筛选后的证据；原始检索结果不出现；完整证据池不出现在每一步的请求里；前缀用量不变。

### Phase 7.1：TUI 过程可见

**目标**：检索、先验答案、证据整理、整合、核对五个阶段在终端界面可见。

**文件**：`core/models.py`、`widgets/messages_pane.py`、`widgets/status_bar.py`、`screens/chat.py`。

**做法**：

- 增加 `PartType.SAR`，只给界面看，不进入模型上下文。
- 每个阶段写一条过程行：检索条数、先验说法与缺口、进入上下文的证据与四类关系、整合动作、核对结果。证据行在后续批次里原地更新。
- `StatusBar` 增加阶段文字，显示为「● 生成中 · 整理检索结果」；`turn.done` 或状态变成非运行状态时清空。
- 被单通道接走的工具卡显示「已整理为 N 条证据 · 原文可展开」。

**验收**：一轮检索触发的对话里能看到五条过程行；状态栏阶段随事件变化；`turn.done` 后阶段文字清空。

### Phase 8：答案核对

**目标**：交付之前检查四件事：说法是否完整、外部事实是否有来源、答案是否发生没有依据的改变、是否违反任务限制。

**文件**：新增 `verifier.py`、`prompts.py`；在 `_finalize_answer` 中调用。

**做法**：

- 完整性：初始说法每一条都要有去向（保留、修订、拒绝），拒绝要写理由；没有去向的列为遗漏。
- 来源：外部事实类说法必须有证据编号；没有来源的标 `unverified`，不能写成文献已经证明。
- 改变：按说法集合计算保留比例；按任务覆盖计算改变程度；按「有证据支持的改动比例」计算没有理由的改变。两个数字写进答案状态与报告，只用于观察，不单独判定合格。
- 任务限制：读取 `TaskState.constraints` 与任务书里的限制；出现违反时直接判不通过，例如用户要求不对接，答案里不能出现对接结果。
- 语义核对：可选的模型请求，检查证据与说法的方向是否一致、有没有把推断写成事实；能机械判的先机械判。
- 不通过时最多修复一轮：缺来源的补来源或降级为推断；没有依据的改变恢复原说法；遗漏的补回；违反限制的直接改回任务范围。修复后再核对一次，仍不通过就把不确定处写清楚，交给用户。
- 发事件 `answer_verified` 与 `answer_finalized`。

**验收**：故意去掉一条来源时，来源检查能指出具体说法；故意大幅改写没有证据的说法时，改变数字升高；违反「不要对接」时核对不通过；修复只跑一轮。

### Phase 9：记忆与事件

**目标**：查询证据与长期记忆分开；原始查询结果不自动写进长期记忆；回答演化能在记录里回溯。

**文件**：新增 `core/memory/admission.py`；改 `state/memory.py`、`core/memory/extractor.py`、`state/trace.py`、`state/event_log.py`。

**做法**：

- 记忆准入：原始查询结果、临时说法、没有来源的推断都不写长期记忆；实验事实、用户确认的稳定事实、带来源的数据库记录可以写；写得进去的条目必须带来源与证据类别。
- `remember` 工具调用准入策略；本地抽取也调用同一份策略，默认仍然关闭。
- 查询证据留在答案状态、`corpus/web/` 与 `ResearchWorkspace`；长期记忆只留经过确认的稳定事实。
- 事件：`retrieval_observed`、`answer_initialized`、`evidence_collected`、`evidence_assessed`、`claim_updated`、`answer_verified`、`answer_finalized` 走事件总线，`TraceWriter` 写成 JSONL。`EventLog` 仍记录主题与步骤，只增加一个 `answer_id` 引用，不把完整证据写进去。
- 网页报告按 `answer_id` 读答案状态文件，展示先验答案、缺口、查询、证据、评估、改动、核对结果与最终答案。

**验收**：查询一次资料后，长期记忆没有新增原始结果；回答轨迹能按 `answer_id` 还原；日志体积没有随证据正文线性增长。

### Phase 10：多 Agent 整合

**目标**：子会话不直接提交整场答案，只提交说法、证据、产物、局限与未决；主会话按统一证据协议汇总。

**文件**：`roles/contracts/agent.py`、`execution/reports.py`、`execution/scheduler.py`、`execution/coordinator.py`、`search_reasoning/integrator.py`。

**做法**：

- `AgentReport` 增加 `evidence: list[dict]`，每条是证据对象的字典；保留现有 `evidence_refs` 供旧报告读取；`SCHEMA_VERSION` 升到 3。
- 子会话结束时，把工具返回的查询结果经适配层转成证据；子会话的最后一段答复只作为结论，不作为整场答案。
- 主会话收到各份报告后，建立一场的答案状态：先验答案来自任务规格与计划，证据池来自各报告，评估由证据处理层统一做。
- 同一实体在不同子会话里取值不同时，增加到答案状态的冲突列表，由整合阶段处理；不静默取一份。
- 主会话的最终答复由整合结果产生；子会话之间不互相覆盖答案。
- `event_log.py` 记录每个子会话的 `answer_id` 与报告编号，网页端能追到原始来源。

**验收**：两个子会话对同一配体给出不同代号时，主会话的答案状态里能看到冲突；最终答复保留双方来源并给出条件；报告能追到子会话的证据对象。

## 六、测试与实验

固定测试集：

| 组 | 问题 | 预期 |
| --- | --- | --- |
| A 普通知识 | 「什么是分子动力学模拟？」 | 不调用检索工具；不建答案状态；不加模型请求 |
| B 查询补充 | 「目前有哪些新的蛋白质-配体重打分方法？」 | 第一次文献或网页检索后建立先验答案；证据进入上下文；答案不成为论文标题清单 |
| C 结果冲突 | 「某方法能不能提高预测准确率？」 | 两篇结论不同时写成有条件结论；双方来源保留 |
| D 任务限制 | 「比较 EGFR 不同结合口袋的结构特征，不要进行分子对接。」 | 可以引用对接文献当背景；不执行对接；限制不因查询改变 |
| E 过期记忆 | 「继续上次那个配体清单。」 | 命中过期记忆时说法带「待核对」；不把过期内容写成定论 |

三种做法跑同一批任务：当前版本、查询主导、SAR。

六个指标：检索相关度、证据对应、说法有来源比例、原说法保留比例、任务覆盖、没有理由的改变。前两项由证据处理记录直接算，后四项由核对器与固定期望项算。`tools/eval_sar.py` 打印数字与失败项，不替人下结论。

## 七、观测与存储

每次回答记录一条 `answer_id`，完整对象写在 `~/.vsagent/answers/<session_id>/<answer_id>.json`，内容包括先验答案、知识缺口、查询问题、原始结果、证据评估、整合动作、修订说法、核对结果与最终答案。

事件写八类：`retrieval_observed`、`answer_initialized`、`evidence_collected`、`evidence_assessed`、`claim_updated`、`answer_verified`、`answer_finalized`，出错时另有 `search_reasoning.error`。事件只带编号与计数，正文从答案状态文件读，避免日志膨胀。

## 八、代价与风险

- 额外请求：文献、网页、语料第一次出证据时多一次先验答案请求，整合一次，核对可选一次；没有检索的轮次不加请求。数据库与计算工具不触发。
- 上下文：先验答案摘要最多 800 个文字单位，证据最多 5 条；原始检索结果不进入模型上下文。
- 延迟：第一次检索之后要等先验答案写完才进入下一步；收尾要等整合与核对完成，状态栏显示当前阶段。
- 误判：触发只看工具类别与结果，没有模式分类器；风险在适配器名单漏登记新工具，固定测试集覆盖。
- 整合改写：规则校验与核对器限制；最多修复一轮。
- 工具可见性：需要查资料的子会话要能看见对应工具族；主会话不加领域族。
- 失败处理：先验答案、整合、核对出错时按当前轮错误显示，不静默降级。

## 九、实施顺序

Phase 0 先做，后续核对与实验都依赖完整记录。Phase 1 建数据协议，Phase 2 接检索触发，Phase 3 建先验答案，Phase 4 处理证据，Phase 5 整合，Phase 6 接主循环，Phase 7 改上下文，Phase 8 核对，Phase 9 接记忆与事件，Phase 10 接多 Agent。每完成一个阶段，先跑该阶段验收，再进入下一个阶段。

## 十、实施记录

- 开关默认值分两处：应用装配层用 `shadow`，`AgentLoop` 直接构造用 `off`。测试脚本按固定请求条数断言，默认 `off` 才能保持原有行为；产品侧仍然按计划先在真实会话里观察。
- Phase 6 的 `dispatcher.py`、`capabilities.py`、`task_context.py` 没有改动：需要查资料的角色已经声明 `corpus`、`literature`、`websearch` 三族（2026-11-14 的能力表），SAR 不新增权限路径，也不改派发顺序。
- Phase 10 的父会话汇总走 `agent_status` 工具结果的 `meta.evidence`：子会话报告里的证据对象随报告返回。`agent_status` 不触发 SAR，根会话已经有证据池时才并入。报告协议保留 `evidence_refs`，`SCHEMA_VERSION` 升到 3。
- 网页端新增只读接口 `/api/sessions/<编号>/answer`，按最近一份答案状态文件返回先验答案、说法、证据、评估、冲突与最终答复。
- TUI 增加 `PartType.SAR` 过程行与状态栏阶段文字；`SarPartView` 只负责显示，不进入模型上下文。
- 原始检索结果用 `sar_suppressed` 与 `sar_model_output` 挡在模型上下文之外；数据库与计算工具保留原文。失败或轮末解除抑制。
- 证据评估先用规则；规则判不准的少数几条在收尾阶段批量交给一次模型请求。整合前的规则校正、核对后的单轮修复都已接上。
