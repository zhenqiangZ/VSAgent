# 子 Agent 依赖与限制总览

生成于 2026-11-13（P-80 之后）。表里的每一项都能在代码里对上，出处见文末。
角色只有九个 + 主 agent；**派发入口只有主 agent 的 `dispatch_campaign`**。

## 依赖图

```
structure_pocket ──> conformation ──> mechanism ──> hypothesis
       │                                  ▲            ▲
       └──> screening ──> scoring         │            │
ligand ──────────────┴───────────────────┘            │
                              literature ─────────────┘
```

- 只在不同角色之间建边；同一角色承担两项能力不产生自环。
- 前置分两类（P-81）：**缺了连输入都没有**的（筛选←口袋、配体；口袋画像←口袋发现）
  补成计划里的节点；**能花已有产物**的（评分←筛选）只写进 `plan.unresolved_inputs`
  并告警。
- 补完还缺的前置不派：`missing_prerequisites()` 按这次要派的角色判，预览标「不可派发」，
  `confirm=true` 抛错（P-80）。
- 依赖成环当场 `RouterError`，一个都不派。

## 总览表

| 角色 | 承担能力 | 工具族（按子目标取） | 等谁 | 权限 | 报告里不能空 | 不能做 |
| --- | --- | --- | --- | --- | --- | --- |
| `principal` | 无（拆解/派发/汇总） | **无**（这一轮有计划时全部隐藏） | — | — | — | 不装领域族、不自己取数；不派计划外的角色 |
| `target` | target_intelligence | uniprot、opentargets、research | — | read | evidence_refs | 不产生产物；不替用户决定 |
| `structure_pocket` | structure_discovery、pocket_discovery | structure、pocket、research、listing + 常驻 `survey_structures` | — | read / execute / write | evidence_refs、artifact_refs | 不准备受体（准备属于 screening） |
| `conformation` | pocket_profiling | pocket、structure、analysis | structure_pocket | read / analyze | artifact_refs、limitations | 不准备受体、不改结构 |
| `ligand` | ligand_intelligence | chembl、pubchem、literature、research + 常驻 `assess_campaign` | — | read | evidence_refs | 不生成新分子 |
| `literature` | literature_evidence | literature、corpus、websearch、research | — | read | evidence_refs | 结论必须带出处 |
| `screening` | virtual_screening | screen、pharmacophore、receptor、ligand、docking、affinity | structure_pocket、ligand | execute / write | artifact_refs、limitations | 准备受体/配体要用户确认（ASK）；不能切换当前受体 |
| `scoring` | scoring_evaluation | affinity、analysis、admet、research | screening | read / analyze | evidence_refs、limitations | 不重新对接 |
| `mechanism` | mechanism_analysis | analysis、pocket、sar、literature、research | conformation、ligand | read / analyze | evidence_refs、unresolved_questions | 不替用户裁决 |
| `hypothesis` | hypothesis_validation | literature、analysis、sar、research（+ 角色专属 `hypothesis`） | mechanism、literature | read / analyze / write | evidence_refs、unresolved_questions | 不替用户裁决 |

## 调用限制

| 限制 | 具体值 | 落在哪 |
| --- | --- | --- |
| 派发入口 | 只有 `dispatch_campaign`；子会话没有此工具，`depth >= 1` 直接报错 | `spawn.py` |
| 前提 | 必须先有 `session.task_spec`，否则拒绝 | `spawn.py` |
| 角色集合 | `roles` 可以从计划里**减**，也可以点名计划外的角色（按能力表补进这次派发）；上游自动补回并告警；`task` 自定义任务书只给 `roles` 指定的那一个角色（一次只能一个） | `capability_router.extend`、`coordinator.build_tasks` |
| 幂等 | 任务号 = `计划号:角色`；同一 `(父会话, task_id)` 只派一次 | `coordinator.task_id_for`、`dispatcher.find` |
| 并发 | 同一会话同时只能有一场战役；并行上限 3 | `spawn.py`、`scheduler.py` |
| 确认 | 先 `confirm=false` 预览，再 `confirm=true` 启动 | `spawn.py` |
| 深度 | `MAX_DEPTH=1`，子 agent 不能再派 | `host.py:32` |
| 工具档位 | 子 agent 用 `worker`：14 个常驻工具 / 5,215 token（上限 5,300）；假设角色额外拿 `hypothesis`，15 个 / 6,257（上限 6,300）。没有 `dispatch_campaign`、`agent_status`、`remember`、`search_history`、`read_turn` | `toolprofiles.py` |
| 领域工具 | 任务书里写上这一角色要的族，没装过的先 `load_tools`；封存时按主题释放 | `dispatcher._task_book`、`toolset.py` |
| 共享状态权限 | 不再对 worker 一刀切：`prepare_receptor` / `prepare_ligands` / `switch_receptor` 都走 `DEFAULT_ASK`（问一次）；`switch_receptor` 由 `structure_pocket` 用 `listing` 族执行 | `permission.py` |
| 缺前置 | 预览标「不可派发」，`confirm=true` 直接拒绝；只派上游角色时不受别的分支影响 | `capability_router.missing_prerequisites` |
| 上下文复制 | 最多复制父会话剩余预算的 60%；只继承用户约束，不继承父会话目标/进度/阻塞；`ROUTE` 块不复制 | `spawn.py:43`、`host.py:232` |
| 上游传递 | 只把**直接依赖**的 `artifact_refs/evidence_refs` 写进下游 `input_refs`，不传聊天原文 | `scheduler._feed` |
| 超时 | 两级：每个角色有安全网（target 20 / structure_pocket 40 / ligand 30 / screening 90 分钟…，到点 `host.cancel` 并记失败）；`dispatch_campaign(timeout=分钟)` 可给整场更短的上限 | `profiles.TIMEOUT_MINUTES`、`scheduler.run(role_timeouts=…)` |
| 失败传播 | 上游失败 → 下游 `blocked`（不是 failed），无关分支继续跑 | `scheduler.py` |
| 报告质量 | 报告缺必填字段记 `partial`，下游仍可跑；`completed` 但没有结论降为 `partial` | `profiles.check_report`、`reports.build` |
| 结果读取 | 派发只返回预览；进度与结论用 `agent_status` 取，返回摘要不是全文 | `spawn.py` |

## 已解决的四处（P-80，2026-11-13）

1. **准备类动作没有执行者** → 按角色开放：`prepare_receptor` / `prepare_ligands`
   归 `screening`，走默认询问；`switch_receptor` 归 `structure_pocket`（新增 `listing` 族），
   P-83 起也走默认询问（换当前受体是改共享状态）。
2. **`hypothesis` 角色拿不到 `hypothesis` 工具** → `keeps(profile, name, role)` 按能力表
   的 `standalone_tools` 放行；只有假设角色的注册表多这一个（6,257 / 6,300 token）。
3. **`AgentTask.permissions` 没填** → `build_tasks` 从角色说明书填入，派发前的
   `check_task()` 真正在判权限。
4. **缺前置只告警** → `missing_prerequisites()` 按这次要派的角色判；预览标「不可派发」，
   `confirm=true` 抛错，不建会话。

## 计划自己补齐（P-81，2026-11-13）

P-80 的硬闸门只有在「计划本身能补齐」时才成立。2026-11-13 的 HA2 会话暴露了反例：
`配体筛选` 解析成 `ligand_discovery + virtual_screening`，计划只有 `ligand`、`screening`，
主 agent 想派 `structure_pocket` 被拒两次，最后结构盘点谁也做不了。现在
`capability_router.AUTO_PREREQUISITES` 把**没有输入就开不了跑**的前置补成节点：
`virtual_screening ← pocket_discovery + ligand_intelligence`、`pocket_profiling ← pocket_discovery`。
可以吃已有产物的（`scoring_evaluation ← virtual_screening`）照旧只记未决。

## 计划外的角色与没派的角色（P-83，2026-11-13）

- **加角色**：`roles=["structure_pocket"]` 在只含 `ligand`、`screening` 的计划上也能派——
  `CapabilityRouter.extend` 按角色的能力表补能力、补前置、重算族与依赖，预览会写
  「补派计划外的角色：…」。
- **减角色**：按子集派发时，预演写明「计划里还有没派的角色：筛选执行（screening）」，
  `Campaign.pending` 进 `campaign.done`，并写进待办。
- **自带输入**：`available_inputs` 里有 `pocket` / `ligand_set` / `structure` / `receptor` 时，
  对应的上游不补，记进 `plan.provided_capabilities`，缺前置判据也认它。

## 还剩一处

- **`assess_campaign` 没有能力声明**：它不在任何 `Capability.standalone_tools` 里。
  P-83 起至少不再是「靠没被排除碰巧留下」——`toolprofiles.WORKER_CORE` 明确写着它归
  worker，`check_toolset` 单列一行。但「哪些角色该有盘点」仍然没有单一事实来源。

## 出处

- 角色 / 能力 / 族 / 权限：`vsagent/core/agent/capabilities.py`
- 前置关系 / 缺前置判据：`vsagent/core/agent/capability_router.py`
- 报告必填：`vsagent/core/agent/profiles.py`（`REPORT_REQUIREMENTS`）
- 任务翻译 / 权限字段 / 角色子集：`vsagent/core/agent/coordinator.py`
- 派发、幂等与缺前置拦截：`vsagent/core/agent/dispatcher.py`、`spawn.py`
- 并行 / 失败传播：`vsagent/core/agent/scheduler.py`
- 深度 / 上下文复制：`vsagent/core/agent/host.py`
- 权限（准备类 ASK、Plan 禁令）：`vsagent/core/agent/permission.py`
- 两档工具与角色例外：`vsagent/core/agent/toolprofiles.py`

核对命令：`python tools/check_capabilities.py`、`python tools/check_toolset.py`、
`python tests/test_agent.py --only profiles`、`--only capabilities`、`--only scheduler`。
