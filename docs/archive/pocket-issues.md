# 口袋子 Agent 问题（ses_0039 复盘）

> 状态：**问题 A、问题 B 第 1–5 条已于 2026-11-14 解决**（用户当天先说「暂不修改」，
> 后要求解决 A、B；同日又要求「问题 A 加入 literature，问题 B 剩余 3 条实现」）。
> 本轮补齐：给「口袋发现」能力补 `literature` 类（查外部文献）、新增 `lookup_handles`
> 工具（一次查清一批编号）、工具输出被压短后带取回提示、收尾时单独显示本段产物。
> 问题 D、第六节的跨角色过度依赖，以及各节其余「待选的修法」仍未实施。
> 下面正文的事实与根因分析照旧保留，但引用的 `capabilities.py` 行号是**改动前**的
> 位置（按能力名 `pocket_discovery` 定位即可），族清单也是改动前的样子。
> 事实来自 `~/.vsagent/state.toml` 与 `~/.vsagent/logs/ses_0037.jsonl`（父会话 + 两个
> worker 写在同一份 trace 里），代码出处给到文件行号。

## 一、会话与证据来源

| 项 | 值 |
| --- | --- |
| 父会话 | `ses_0037`，两轮：`我想要针对HA2进行配体筛选，给我一个计划` → `搜索口袋与配体信息` |
| worker | `ses_0038`（`ligand`，配体情报）、`ses_0039`（`structure_pocket`，结构与口袋） |
| 本文对象 | `ses_0039`：24 步跑满、40 次工具调用、5 次失败 |
| 能力计划 | 两轮 `route.plan` 的能力集合完全相同：`ligand_intelligence / pocket_discovery / virtual_screening`（rev 0、rev 1）；计划里**没有** `literature_evidence`，族清单里**没有** `corpus`、`websearch` |

`ses_0039` 任务书原文（`state.toml` 里 `msg_0251` 的 text part）：

```text
[结构化任务]
识别候选结合口袋并建立口袋目录
角色：结构与口袋（structure_pocket）
工具族：pocket、structure、research（没装过的先用 load_tools 装）
边界：结构检索与口袋发现；改共享状态的动作由父会话确认后执行。
输入引用（已经在登记表里，直接用，不要重新建）：
- 总目标：为靶点 HA2 制定一套配体筛选计划
限制：针对 HA2 进行配体筛选 / 给出一个计划 / 搜索口袋信息 / 搜索配体信息
报告里不能空：evidence_refs、artifact_refs
完成后给出：结论、产物、证据出处、没做到的部分。
```

对照 `ses_0038` 的任务书是 `工具族：chembl、pubchem、literature、research`——
`literature` 只给了配体角色，`corpus` 谁都没拿到。

### `ses_0039` 的 40 次工具调用（按名计数）

| 工具 | 次数 | 工具 | 次数 |
| --- | --- | --- | --- |
| `bash` | **18** | `fetch_structure` | 2 |
| `close_topic` | 3 | `detect_pockets` | 2 |
| `open_viewer` | 3 | `define_pocket` | 2 |
| `read` | 2（全失败） | `write_run` | 2 |
| `assess_structure` | 2（全失败） | `recall` | 1 |
| `list_objects` | 1 | `load_tools` | 1 |
| `task` | 1 | | |

`search_corpus` / `read_source` / `search_literature` / `read_fulltext` /
`add_evidence`：**0 次**。`load_tools` 只调过 1 次，参数是
`{"families": ["pocket", "structure", "research"]}`，之后再没装过别的族。

---

## 二、问题 A：只做了记忆召回，没有检索资料库

### 2.1 现象

用户看到的是「只进行记忆召回」——这个印象有依据，但要说准：

- 第 1 步的 `recall` 查询是 `HA2 结构 受体 口袋 3EYM 8VQL`，返回的是**项目记忆**里的
  事实（step 1 reasoning 原话：「I have prior facts: ... PNAS 2024 PMID 38753503 ...
  ChEMBL for P03437 pChEMBL≥7 = 0」）。
- 之后 39 次工具调用里**没有任何一次**走语料库 / 文献检索。worker 的「证据出处」
  是自己用 `bash` 读 `runs/*/meta.json`、`state.toml`、`ledgers/ses_0037.json` 拼出来的，
  以及自己按坐标算的距离。
- worker 自己在 `close_topic` 里承认了这一点：
  - 「关键核对（**自己按坐标算的距离，非工具产物**）」
  - 「未做：受体准备（含预演）、对接、DTA、**TBHQ/A1A 残基级文献核对、A1AD 系列 SAR**」
  - 最终答复：「本轮步数用满，assess_structure 对两个受体的缺口与交替构象核对、
    meta.json 复核都没执行。」

也就是说：`evidence_refs` 被填上了，但来源是记忆 + 本地文件，**不是资料库**；
记忆里的事实也没被语料/文献复核过。

### 2.2 逐层根因（六条，从计划到验收）

1. **能力计划里根本没有 `corpus` / `literature` 给口袋角色。**
   `vsagent/core/agent/roles/capabilities.py:80-84`：
   ```python
   Capability(
       name="pocket_discovery",
       role="structure_pocket",
       families=("pocket", "structure", "research"),
       ...
   )
   ```
   `vsagent/core/agent/roles/capabilities.py:101-105` 把 `corpus` 归给 `literature` 角色：
   ```python
   Capability(
       name="literature_evidence",
       role="literature",
       families=("literature", "corpus", "websearch", "research"),
       ...
   )
   ```
   而本轮 ROUTE 选中的能力是 `ligand_intelligence / pocket_discovery /
   virtual_screening`，`literature_evidence` 从未被选中（两轮 `route.plan` 一致）。
   `research` 族只有 `register_object / add_evidence / list_objects /
   review_hypothesis`（`vsagent/core/agent/domain/all_tools.py` 的 catalog），
   **没有** `search_corpus` / `read_source`。

2. **`CapabilityRouter._families()` 只取「这一次」选中的能力。**
   `vsagent/core/agent/planning/capability_router.py:349-350`：
   ```python
   def _families(self, capabilities: list[str], agent: str) -> list[str]:
       """这个角色**这一次**要用的族：从被选中的能力取，不取角色名下的全部族。"""
   ```
   所以 `structure_pocket` 的 `tool_families` 就是 `[pocket, structure, research]`，
   不会因为「角色名下的全部族」而自动补上什么。

3. **任务书把这份族清单原样写给了 worker。**
   `vsagent/core/agent/execution/dispatcher.py:64`：
   ```python
   lines.append(f"工具族：{'、'.join(wanted)}（没装过的先用 load_tools 装）")
   ```
   `dispatcher.py:51-59` 的注释已经预判过这类问题：不写族，子会话「做不到又不知道
   缺什么」。这次写了族，但写的是一份**不含语料/文献**的族。

4. **子 agent 不跑路由，无法自己补计划。**
   `vsagent/core/agent/execution/loop.py:651`：
   ```python
   if self.route_adapter is None or session.depth > 0:
       return
   ```
   `depth > 0` 的 worker 直接跳过 ROUTE / 能力计划整段。它的工具宇宙 =
   任务书里的族 + 自己调 `load_tools`。它既不能重算计划，也没有「我该找文献」的
   机制化入口。

5. **`intent.hint` 认出了语料，但只渲染成一行提示。**
   trace 里 `intent.hint` 事件：`{"session_id":"ses_0039","sources":["corpus"],
   "matched":["证据","出处"]}`（ses_0038 还多命中 `literature`）。
   `loop.py:648-650` 说明这一行对子会话「照样要给」；但
   `vsagent/core/agent/planning/intent.py:85` 只产出：
   ```python
   line = f"（可能相关的来源：{names}。）"
   ```
   即「（可能相关的来源：本机语料。）」——不装工具、不改计划、不给句柄。
   这与 `docs/history.md` 里「来源提示」的设计一致：它只是提示，不是能力。

6. **两道「验收/守卫」都没把出处卡住。**
   - `capability_guard.check()`（`validators/capability_guard.py:21-39`）若真调
     `search_corpus` 会产出 `code="family_not_planned", severity="deny"`；但本轮
     `route.plan` 的 `mode` 是 `shadow`，而 `loop.py:1605` 只在 `enforce` 下阻断：
     ```python
     if verdict.blocked and self.validator_mode == "enforce":
     ```
     shadow 模式只记一笔。何况 worker 根本没装这些工具（第 5 条）。
   - `validators/delivery.py:3` 开头就写明「它**不阻断**」；
     `roles/profiles.py:61` 只规定 `REPORT_REQUIREMENTS["structure_pocket"] =
     ("evidence_refs", "artifact_refs")`，验收的是**非空**，
     `contracts/agent.py` 的 `AgentReport.from_dict()` 接受任意字符串。
     没有任何一处检查 `evidence_refs` 是否来自 `add_evidence` / 语料 / 文献工具。

补充：`task_context.py:26` 的 `RECOMPILE_MODES = {"new_task","modify_task"}`、
`route_adapter.py:121` 的 `if turn.recompile:` 说明只有新任务/改目标才重新编译规格；
第二轮「搜索口袋与配体信息」无论被判成哪种模式，两轮 `route.plan` 的能力集合都
没变（`rev 0` 与 `rev 1` 的能力、agents、families 三项完全一致）——
也就是说「ROUTE 里识别到语料意图」这件事**没有**变成新的子目标或能力。
trace 没有持久化 `turn_mode` 字段，此处只能确认「结果没变」，不臆断具体模式。

### 2.3 影响

- 口袋结论里的文献级事实（TBHQ / A1AD 系列、PMID、残基归属）全部来自记忆或本地
  文件，未经语料/文献复核；`evidence_refs` 形式上非空、实质上不是可追溯的资料库出处。
- 与 `project_charter_hypothesis.md` 记的垂直定位冲突：产出应是「假设体系 +
  可反证证据」，而这里证据层是自证（读自己刚写的 `meta.json`）。
- 同轮 `ses_0038` 有 `literature` 族、用了 `search_literature` / `read_fulltext`，
  但也没用 `search_corpus`——`corpus` 在本轮计划里对**所有**角色都不可见。

### 2.4 待选的修法

> **已解决（2026-11-14）**：第 1 条做完，`corpus` 与 `literature` 都补了。
> `pocket_discovery` 的 `families` 现在多了 `corpus` 与 `literature`（`listing`
> 是为问题 B 一起补的，见 3.4）。子会话能用 `search_corpus` / `read_source`
> 查本机语料，用 `search_literature` / `search_europepmc` / `read_fulltext`
> 查外部新文献；两类并存、互不替代：本机语料复核已收录的文献事实，
> 外部检索补新的文献。`capability_guard` 也不会再把这几个工具判成
> `family_not_planned`。第 2–4 条仍未实施（让来源提示进计划、`evidence_refs`
> 出处校验、派发预览提示缺口），它们管的是「一定要让模型去用工具」，改动面更大。

1. 给 `pocket_discovery` 的 `families` 补 `corpus`（必要时补 `literature`），
   让口袋角色自己能查资料；改动落在 `capabilities.py:80-84`。
2. 或让「来源提示命中 corpus/literature」变成计划输入：在 ROUTE / 编译阶段把
   `literature_evidence` 纳入子目标或至少写进 `dispatch_campaign` 的派发预览，
   避免 detect 到了却不下发。
3. 给 `evidence_refs` 加机械的出处校验：要求条目来自 `add_evidence` /
   语料 / 文献工具的返回句柄，而不是任意字符串（`delivery.py` 目前不阻断，
   需要另想一个不破坏「不阻断」约定的落点）。
4. 在派发预览里显式提示「本轮识别到文献/语料意图，但未包含在任何能力计划里」，
   让人一眼看出缺口。

---

## 三、问题 B：为什么 `bash` 调了 18 次

### 3.1 现象：18 次分成三段，几乎全是「找状态」

| # | 段 | 命令意图 | 结果 |
| --- | --- | --- | --- |
| 4 | 开场盘点 | `ls runs/`、`ls .probe` | 列已有运行目录 |
| 5 | 开场盘点 | 循环读 6 个 HA2 目录的 `meta.json` | 输出被压缩 |
| 6 | 开场盘点 | 换更窄的 python 脚本**重读同一批** `meta.json` | 拿到 key/target |
| 7 | 开场盘点 | `ls -la ~/.vsagent`、`ls -R` | 看运行时目录 |
| 8 | 开场盘点 | `grep -i "3EYM\|8VQL\|ha2" state.toml` | 找受体/句柄 |
| 9 | 开场盘点 | `ls receptors`、`ls pockets \| head -50` | 列受体与口袋目录 |
| 10 | 开场盘点 | `grep "8VQL_A1A_HA1_HA2_" state.toml` + `head -c 1500` | 没命中，退回读文件头 |
| 18 | 主题封存后重新盘点 | 又一次 `ls runs/`、`ls -la ~/.vsagent` | 从头再来一遍 |
| 19 | 找登记文件 | `ls *.json *.toml *.db` + `grep -rl 8VQQ` | **FAILED** |
| 20 | 找登记文件 | `ls -la` + `find . -maxdepth 2 -name "*.json"` | 继续找 |
| 21 | 找登记文件 | 再读 6 个 `meta.json`（带 try/except） | 又一次重读 |
| 22 | 找登记文件 | `grep "group1 HA stem\|..." state.toml` | 找自己刚写的口袋名 |
| 23 | 找登记文件 | `ls pockets` + `grep "receptor#1" state.toml` | 找句柄 |
| 24 | 找登记文件 | `ls -la live ledgers` + `grep 'handle = "artifact#..."' state.toml` | 找句柄定义 |
| 25 | 找登记文件 | `grep 'kind = "pocket' state.toml` + `grep current_receptor` | 找口袋登记 |
| 26 | 找登记文件 | `ls -t ledgers` + python 定位最近 3 个 ledger | 找到 `ses_0037.json` |
| 27 | 找登记文件 | python 读 `ledgers/ses_0037.json` 打印 `current_receptor/counters/entries` | **终于拿到登记表** |
| 30 | 收尾复核 | python 读自己刚 `write_run` 写的两个 `meta.json`，打印 center/box | 复核产物 |

18 次里 17 次是「盘目录 / 读 meta / grep state.toml / 找 ledger / 复核产物」，
只有 1 次（#30）是真正的产物校验——而且是 `bash` 手算，不是工具产物。

### 3.2 根因（六条）

1. **缺 `listing` 族，根因是 `structure_discovery` 能力本轮没被选中。**
   `list_workspace / describe_entry / switch_receptor` 属于 `listing` 族，
   它归在 `structure_discovery`（`capabilities.py:68-78`，同能力还带常驻工具
   `survey_structures`），**不在** `pocket_discovery` 的
   `("pocket", "structure", "research")` 里。本轮两轮 `route.plan` 的能力集合
   只有 `ligand_intelligence / pocket_discovery / virtual_screening`，
   所以 `structure_discovery` 没进计划，`listing` 族与 `survey_structures`
   对 worker 一律不可见（更准确的归因见问题 D）。
   worker 要盘 `runs/`、`receptors/`、`pockets/`、`ledgers/`、`live/`，
   没有对应工具，只能 `bash ls / find / for`。它自己在 step 2 reasoning 里
   就发现了：「I don't have `list_workspace` tool in the list? ... Tools
   available: `list_objects` (research family).」而 `list_objects`
   （research 族，工具 #3）返回「工作区里还没有这一类对象」，
   查的是对象登记表，帮不上文件/运行目录的盘点。

2. **缺 `corpus` / `literature` 族（问题 A 的延续）。** 「找资料/出处/来源」这条
   需求没有工具承接，于是退化成 `bash grep state.toml`。#8、#10、#22–#25 这六次
   本质上是**手搓登记表查询**——在 10MB 的 `state.toml` 里 grep 句柄、口袋名、
   `current_receptor`，替代 `search_corpus` / `read_source`。

3. **共享登记表对 worker 不直接可见。** 任务书写着「你与父会话共享工作目录、runs/
   与登记表，handle 可以直接引用」「输入引用（已经在登记表里，直接用，不要重新建）」
   （`dispatcher.py:68-71`），但 worker 拿到的是**父会话的句柄**（`receptor#1`、
   `artifact#1`…），登记表实际在 `~/.vsagent/ledgers/ses_0037.json` 和 `state.toml`
   里。worker 没有「读当前 ledger / 解析 handle」的工具，于是 step 9 说
   「I don't know the handle IDs (artifact#N) ... I can't guess」，step 17 说
   「The registry is per-session ledger (ledgers/ses_00XX.json). Let me find the
   current session ledger」——从 #23 到 #27 花了 5 次 bash 才把登记表定位出来。

4. **工具输出压缩 → 反复收窄重跑。** stage 7 reasoning：「The grep output was
   compressed. Let me read `state.toml` relevant sections differently」；
   stage 15：「The second bash output got compressed」。trace 里
   `context.assembled` 对 `ses_0039` 全程标 `degraded: ["evidence_detail"]`。
   #5→#6 是同一批 `meta.json` 读两遍（第一次输出被压，第二次改脚本只打 key/target）；
   #4 / #18 / #21 又是 `ls runs/` 与读 meta 反复出现。压一次，补一条更窄的命令。
   （这一条的现象已在翻译层补上取回提示，见 3.4 第 4 条；上面记录的是当时的情形。）

5. **`close_topic` 封存后状态不连续，重新盘点。** 工具 #17 第一次 `close_topic`
   封存了「定义口袋」主题，#18 起进入新主题。按工具租约设计，封存会释放领域族、
   压缩上下文，worker 对「我刚产的 `artifact#3/#5`、`receptor#1`」的即时记忆也淡了，
   于是又从 `ls runs/` 开始。trace 里 `context.assembled` 在封存那一步 `recent`
   掉到 `0`、`event` 从 1014 涨到 1757，正是这次压缩。18 次 bash 里 #18–#27
   这 10 次都发生在封存之后。

6. **`bash` 是核心工具，永远在，成为零阻力逃生口。**
   `vsagent/core/agent/toolset.py:200` 写明「文件、登记表、任务、事件、记忆这些核心
   工具一直在」；`capability_guard` 也把 `bash` 当常驻工具放行
   （`capability_guard.py:26`：「常驻工具（bash、read、load_tools……）不属于任何族」）。
   任何「缺族」的读需求，模型都能立刻用 `bash` 兜底，没有阻力，也没有任何校验器会
   提醒「你该用 `list_workspace` / `search_corpus`」。

### 3.3 与 24 步预算的相互放大

18 次 `bash` 占掉 40 次工具调用的 45%，把步数预算吃在「找状态」上，而不是「做口袋」上。
step 8 reasoning 已经在算「I have 17 steps left. I need to be efficient」，
step 18 变「7 steps left」，step 24 是「Step budget is exhausted」。
尾部 #34–#38 连着 5 次 FAILED，输出全是同一句：

```text
本轮步数快用完了，不再展开新工作。把已经做出来的写进 write_run，用 close_topic
收这一段，再用 task 记下下一步。
```

于是 `read`（读两个 `meta.json`）、`assess_structure`（两个受体的缺口/交替构象核对）、
再次 `open_viewer` 全没执行——最终答复里只能写「页面地址在第二轮取得过但本次记录
不完整，未产出可靠地址，下一轮用 open_viewer 重取」。
**bash 用得多 → 挤占预算 → 连收尾复核都做不完 → 产物可信度下降**，是个闭环。

另一个副作用：#30 的坐标/距离核对是 `bash` 里 python 手算的，worker 自己在
`close_topic` 里标了「非工具产物」。这既是缺 `assess_structure` 预算的结果，
也让「证据出处」更不像工具溯源、更像自证（呼应问题 A）。

### 3.4 待选的修法

> **已解决（2026-11-14）**：第 1–5 条全部做完。
> 第 1、2 条：`pocket_discovery` 的 `families` 多了 `listing` 与 `corpus`。
> `listing` 让子会话用 `list_workspace` / `describe_entry` 盘 runs/、receptors/、
> ledgers/ 与找句柄，不必再反复 `bash ls` / `grep state.toml`；`corpus` 把「找资料」
> 从 `grep state.toml` 收回到 `search_corpus` / `read_source`。注意
> `.vsagent/tools.toml` 关掉了 `switch_receptor`，但 `list_workspace` /
> `describe_entry` 没关，够用。
> 第 3 条：新增 `lookup_handles` 工具（仍在 `listing` 族），给一批编号一次取回每个
> 对象的完整信息，与 `describe_entry`（单个编号）共用一套展开写法；留空则列出全部。
> 没有选「任务书里带句柄表」那条备选路径。
> 第 4 条：`prompt.py` 的 `truncate_for_model` 在两条压缩路径的省略说明里，
> 统一带上一句提示：写清压过了、完整内容还在会话记录里、用哪条更窄的命令取回。
> 第 5 条：收尾时从现成的 `Event.artifacts` 派生，在「当前任务复述」与事件块里
> 各单独显示一行本段产物；不拼进 `Event.handoff`、不调大交接的字数上限。

1. 给 `pocket_discovery` 补 `listing` 族（或把 `list_workspace / describe_entry`
   做成对所有 worker 常驻的只读工具），让盘目录不再走 `bash`。
2. 同问题 A 的第 1 条：补 `corpus`，把「找资料」从 `grep state.toml` 收回工具。
3. 把「当前 ledger 的 handle → 对象」做成一个 worker 可直读的只读工具，或在任务书里
   直接带上句柄表，兑现「handle 可以直接引用」这句话。
4. 针对压缩重跑：让被压的输出带「压缩了什么 / 用哪条窄命令能取回」的提示，
   减少同内容反复读。
5. 主题封存时把「本主题产出的句柄/产物清单」写进 handoff（`next_tools` 之外再带一段
   产物索引），避免新主题从 `ls runs/` 重新开局。

---

## 四、问题 D：记忆给了两个口袋，后续就按两个口袋交付（覆盖不全）

> 用户观察（2026-11-14）：「recall 返回了两个口袋的信息，后续就直接使用这两个口袋」。
> **成立，且比表面更严重**：不只是「用了两个」，是**没有找全的机制，还把没查的
> 来源说成了理由**。编号跳过 C：C 是跨角色的「检索结果被当成事实底座」，
> 已独立成篇 [`retrieval-overreliance.md`](retrieval-overreliance.md)。
> 仓库里其实早有针对这件事的判据：
> `docs/archive/runtime-fixes-plan-2.md:41` 的「3.3 全集与覆盖：不许把单来源
> 说成『唯一口袋』」。

### 4.1 现象：从记忆到交付，一条直线

| 环节 | 事实 |
| --- | --- |
| `recall` | 记忆只点名**两处**：group 2 的 3EYM/TBHQ HA1-HA2 界面口袋、group 1 的 8VQL…8VQQ/A1AD* HA stem 口袋 |
| 结构取数 | `fetch_structure` 只调 **2 次**：`3EYM`、`8VQQ`——正是记忆点名的两个 PDB；`survey_structures` / `search_pdb` / `fetch_alphafold` / `import_structure` **0 次** |
| 口袋发现 | `detect_pockets` 只调 **2 次**（`receptor#1`、`receptor#2`），两次都显式写 `sources: "ligand"` |
| 定义 | `define_pocket` 只调 **2 次**，各取 `index=1`；`compare_pockets` / `similar_pockets` **0 次** |
| 交付 | 三次 `close_topic` 与最终答复一律「**两处**候选口袋」，无一处说明「还有哪些来源/结构没查、可能还有别的口袋」 |

`ses_0039` 的 `close_topic` 原话：

> 两处都来自实验结构的共晶配体，**不是预测口袋**……
> **未用 p2rank 预测做主依据（两处均有共晶配体）**。
> group 1 的 8VQL/8VQM/8VQN 与 8VQQ 属同一系列，**可选作对照结构**。

### 4.2 根因：根本没有「找全」的入口

1. **`structure_discovery` 能力本轮没被选中，结构空间被记忆框死。**
   `vsagent/core/agent/roles/capabilities.py:69-78`：
   ```python
   Capability(
       name="structure_discovery",
       role="structure_pocket",
       families=("structure", "research", "listing"),
       standalone_tools=("survey_structures",),
       note="结构全景与条目检索；survey_structures 常驻，不登记族",
   )
   ```
   两轮 `route.plan` 的能力集合只有
   `ligand_intelligence / pocket_discovery / virtual_screening`，
   `structure_discovery` 从未入选。而 `CapabilityRouter._families()`
   （`capability_router.py:349`）只取被选中能力的族，子 agent 又跳过 ROUTE
   （`loop.py:651`，见问题 A 第 4 条）——于是 `survey_structures` 与
   `listing` 族对 worker 不可见，它**只能拿记忆点名的两个结构**。
   这同时是问题 B 第 1 条「缺 `listing` 族」的更准确归因。

2. **口袋来源被主动缩到单来源，且工具默认也是「够一个就停」。**
   `detect_pockets` 的参数枚举（`vsagent/core/agent/domain/pockets.py:1533`）
   只有 `auto / ligand / complex / prior / predicted`，**没有 `all`**；
   `auto` 的实现（`:1610-1618`、`:1656`）：
   ```python
   plan = ["ligand"]
   if candidates: plan.append("prior")
   if uniprot:    plan.append("complex")
   if pdb_id:     plan.append("predicted")
   ...
   for layer in plan:
       if auto and collected:
           break          # 取第一个非空来源就收工
   ```
   工具描述也写明「`auto` 只取第一个非空的来源」「要叠加来源就显式写
   `ligand,complex`」。worker 两次都显式写 `sources: "ligand"`，
   连 `auto` 的按需追加都没用，`complex / prior / predicted` 三层全没查。

3. **归档判据只落地了一部分。** `docs/archive/runtime-fixes-plan-2.md:41-45`
   要求：`sources="all"`、工具输出列「未查来源」、枚举类交付必须带
   `total / reported / excluded / sources`。当前代码里：
   - 没有 `sources="all"`（全仓 grep 无匹配）；
   - `detect_pockets` 输出**没有**「未查来源」清单，只有在空结果时给一句
     「要给复合物来源就补 uniprot……」（`pockets.py:1695` 附近）——
     本轮两次都拿到非空结果，连这句都没出现；
   - `total / used / excluded / sources` 确实在 dataclass 里
     （`pockets.py:83-88`），但那是**复合物来源的账本**，不是「口袋全集」的
     `total / reported / excluded`。

4. **缓存命中，连「重新算一遍」都没发生。**
   `detect_pockets(receptor#1)` 返回「登记表里已有同一份口袋集：
   artifact#1（**未重复计算**）」——这个口袋集是之前会话算好缓存的，
   本轮直接取用。两次输出都只写「来源 ligand」「支持 1 个复合物」。

5. **验收条件下发缺失，覆盖账本连入口都没有。**
   `dispatch_campaign` 预览（`ses_0037` 工具 #5/#13）原话：
   > 注意：计划有 2 个终点（ligand、structure_pocket），交付物
   > LigandCatalog、CandidateSet **不知道该挂给谁，这次不下发**。

   于是 `ses_0039` 的任务书里**没有**「要交的东西」与「验收条件」两行
   （`dispatcher.py:64/68/81` 都会写，只是本轮 `required_outputs` 与
   `acceptance_criteria` 为空），`delivery.check()` 只看 `task.required_outputs`
   （`validators/delivery.py:3`），自然不会去要 `total/reported/excluded/sources`。

6. **工具给了 3 处，被收敛成 1 处。** 3EYM 的口袋集实际返回 **3 处**
   EYK 位点（输出里的 `#1/#2/#3`），worker 归为「三处等价口袋」并只
   `define_pocket(index=1)` 取一处；8VQQ 是 1 处。最终「两处」是记忆给的
   **位点框架**，不是结构空间里的全集。

### 4.3 确实存在第三个口袋的线索，但没查

本机语料 `corpus/doc/d517819c85/text.jsonl` 原句：

> 三者都是 HA 三聚体茎部结合小分子抑制剂的结构
> （**TBHQ、Arbidol、F0045 分别对应**），常被一起讨论，
> 因为它们结合在 HA 茎部的**相近口袋**。

同一份语料列出的结构是 `3EYM`（TBHQ，H3N2 Aichi）、`5T6N`（Arbidol，
H3N2 Hong Kong）、`6WCR`（F0045(S)，H1N1 PR8）；`runs/` 里也确实有
`ha2_g1_6wcr`、`HA2_H1PR8_6WCR`、`HA2_stem_8VQM` 等目录。
`ses_0039` 只把 8VQL/8VQM/8VQN 当「对照结构」，没当潜在新口袋来源；
`compare_pockets` / `similar_pockets` 0 次。

### 4.4 措辞：把「没查」说成了「理由」

归档 3.3 第 4 条要求「不用『唯一』这种词」、3.6 要求「没有用户确认时
不能写 pocket is confirmed」。本轮三次 `close_topic` 与最终答复都没有用
「唯一」，但都把**未查来源**写成了**一个已做的选择**：

- 「未用 p2rank 预测做主依据（**两处均有共晶配体**）」——
  这是用「有共晶配体」为「不查其他来源」辩护，而不是报告
  「complex / predicted 未查」这个事实。
- 三次交付都直接给「两处候选口袋」交用户确认**链选择**，
  从没把「口袋是否找全」列为待确认项。

这正是 3.3 要防的「把单来源说成唯一口袋」，只是换了个更体面的说法。

### 4.5 与前面三个问题的关系

- 是 [`retrieval-overreliance.md`](retrieval-overreliance.md) 的**具体实例**：
  记忆把「两处口袋」这个框架塞进 EVIDENCE 层，模型据此认为全集已经有了，
  没去做覆盖检查。
- 与问题 A / B **同源**：能力计划决定工具宇宙，`structure_discovery`
  没入选 → 既没有全景工具（D），也没有 `listing` 族（B），
  更没有 `corpus` 族（A）。
- `hypothesis` 4 次全在 `ses_0037`，且都是「组内优化 vs 换骨架」的
  **策略假设**，没有一条关于「口袋是否找全」——
  连结构化反证通道都没覆盖到「覆盖度」这件事。

### 4.6 待选的修法（**均未实施**）

1. 口袋任务里把 `structure_discovery` 与 `pocket_discovery` 一起选中，
   或把 `survey_structures` 做成 worker 常驻（它本来就是常驻工具，
   只是被能力计划挡住了），让「先做结构全景」成为必走一步。
2. 落地归档 3.3：`sources="all"`、工具输出列「未查来源」、
   枚举类交付强制 `total / reported / excluded / sources`。
3. `dispatch_campaign` 把 `required_outputs` / `acceptance_criteria`
   真正下发给 worker（本轮「不下发」）；否则任何覆盖账本判据都进不了验收。
4. `detect_pockets` 的 `auto` 至少报「哪些层没查」，别拿到第一个来源就
   静默 `break`；缓存命中时提示「该口袋集由哪个会话、哪层来源算出」。
5. 交付措辞：凡只用了部分来源，写「已查 X、未查 Y」，
   不许写成「因为有 X 所以不用查 Y」。

### 4.7 核对命令

```bash
python3 - <<'PY'
from vsagent.core.session_manager import SessionManager, default_state_path
sm = SessionManager(default_state_path()); sm.load_from_toml()
s = sm.sessions["ses_0039"]
for m in s.messages:
    for p in m.parts:
        if getattr(p, "tool_name", None) in ("fetch_structure", "detect_pockets", "define_pocket"):
            print(p.tool_name, p.tool_input)
PY
grep -n "3.3 全集与覆盖" -A6 docs/archive/runtime-fixes-plan-2.md
```

## 五、核对命令 / 出处

```bash
# 状态与任务书
python3 - <<'PY'
from vsagent.core.session_manager import SessionManager, default_state_path
sm = SessionManager(default_state_path()); sm.load_from_toml()
s = sm.sessions["ses_0039"]
print(s.messages[0].parts[0].text)
tools = [p for m in s.messages for p in m.parts if getattr(p, "tool_name", None)]
from collections import Counter
print(Counter(t.tool_name for t in tools))
PY

# 计划与来源提示
python3 - <<'PY'
import json
from pathlib import Path
for l in (Path.home()/".vsagent/logs/ses_0037.jsonl").read_text().splitlines():
    if not l.strip(): continue
    r = json.loads(l)
    if r.get("event") in ("route.plan", "intent.hint", "context.assembled"):
        print(r["event"], json.dumps(r.get("payload"), ensure_ascii=False)[:300])
PY
```

代码出处：

- 能力 → 角色 → 族：`vsagent/core/agent/roles/capabilities.py:69`（`structure_discovery`）、
  `:80`（`pocket_discovery`）、`:101`（`literature_evidence`）、`:154`
- 计划只取被选中能力：`vsagent/core/agent/planning/capability_router.py:349`
- 任务书写族与输入引用：`vsagent/core/agent/execution/dispatcher.py:51`、`:64`、`:68`、`:81`
- 子 agent 跳过 ROUTE（来源提示对子会话照给）：
  `vsagent/core/agent/execution/loop.py:649`、`:651`
- 来源提示只渲染一行：`vsagent/core/agent/planning/intent.py:80`、`:85`
- 重编译模式：`vsagent/core/agent/planning/task_context.py:26`、
  `vsagent/core/agent/planning/route_adapter.py:121`
- 能力守卫 / 阻断模式：`vsagent/core/agent/validators/capability_guard.py:21`、
  `:26`、`:39`；`vsagent/core/agent/execution/loop.py:1605`
- 交付验收不阻断、报告必填：`vsagent/core/agent/validators/delivery.py:3`、
  `vsagent/core/agent/roles/profiles.py:61`
- `load_tools` / 核心工具常驻：`vsagent/core/agent/toolset.py:200`、`:207`、`:357`
- 工具族目录：`vsagent/core/agent/domain/all_tools.py`（`catalog()`）
- 口袋来源四层与 `auto` 提前收工：`vsagent/core/agent/domain/pockets.py:1533`、
  `:1594`、`:1610`、`:1656`；覆盖账本 dataclass：`:83-88`
- 「不许把单来源说成唯一口袋」判据：`docs/archive/runtime-fixes-plan-2.md:41-45`
- 验收条件下发：`vsagent/core/agent/execution/dispatcher.py:64`、`:81`、
  `vsagent/core/agent/validators/delivery.py:3`

## 六、相关问题：检索结果被当成事实底座

问题 A 的深层原因不只是「没装 `corpus` 族」——同一条带
`（引用文件已变更，可能过期）` 的 `recall` 记忆被三个会话照单全收，
`recall` 的输出被系统主动放进 EVIDENCE 层（身份是「证据」而非「线索」），
模型据此认为事实已经有了，于是既没动机检索资料库，也没动机登记证据。
完整证据、机制根因、正面样本（`ses_0037` 的 `hypothesis` 确实标了
「尚未经核实」并最终 refuted）与待选修法见
[`retrieval-overreliance.md`](retrieval-overreliance.md)。

该文档同时补上了问题 B 的一处解释：`ses_0039` 那 6 次 `grep state.toml`
找句柄/口袋名，本质是在给记忆里的说法找补，而不是重新取证。

会话报告管线与运行时预算/派发的问题（报告只算父会话、第 2 轮显示
`running`、worker 跑满 24 步、交付物不下发）另见
[`session-report-issues.md`](session-report-issues.md)。其中「步数预算」一节
与本文问题 B 的 3.3 是同一个闭环：盘点吃掉步数 → 收束期复核全被拒。

## 七、改动范围

2026-11-14 用户先说「暂不修改」，本文据此写成只记录；同一天先要求「解决
pocket-issues.md 中的问题 A、B」，又要求「问题 A 加入 literature，问题 B 剩余
3 条实现」。两轮合起来改了这些代码（外加本文的状态说明）：

- `capabilities.py`：给 `pocket_discovery` 补 `listing`、`corpus`、`literature` 三类。
- `domain/listing.py`：新增 `lookup_handles` 工具，抽公共的 `render_entry_detail`。
- `debug_tools.py`：调用清单补 `lookup_handles` 一行。
- `roles/prompt.py`：`truncate_for_model` 两条压缩路径统一带取回提示。
- `planning/context.py`：收尾时在复述与事件块各单独显示一行本段产物。
- `tests/test_agent.py`：同步写死的族断言，补新工具、压缩提示、产物承接的检查。

规则文件（`dist/rules/agents/structure_pocket.md`、
`dist/rules/stages/pocket_analysis.md`）没动；没有给 `close_topic` 加参数、
没有调大交接的字数上限、没有改稳定前缀（前缀检查只量不变的那一段）；
问题 D、第六节的跨角色过度依赖、其余待选修法都没动。

补族确实多占一点工具定义：`check_capabilities.py` 实测 `structure_pocket`
从 4 类 6072 token，先到 5 类 7041 token，再到 6 类 8097 token。但主题租约
（`project_tool_lease.md`）会在封存时释放领域族、只预装 `next_tools` 点名的几族，
所以「能用的类别变多」不等于「每一步都多发六类」。主 agent 纯编排
（`project_principal_orchestrator.md`）那条约束也没破：改的是派给子会话的能力表，
根会话仍然不装领域工具。

还剩的事（这次没做）：第 3 条没选的备选做法（任务书里直接带完整清单）、
§2.4 第 2–4 条（让来源提示进计划、`evidence_refs` 出处校验、派发预览提示缺口）、
问题 D 的覆盖账本、第六节的跨角色过度依赖、另外三份复盘文档里其余的修法；
以及用一轮真实会话确认子会话确实改用 `search_corpus` / `list_workspace` /
`lookup_handles`，而不是继续凭记忆、用 `bash`。改动还没提交到版本库。
