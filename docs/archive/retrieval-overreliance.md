# 检索结果被当成事实底座：过度依赖问题（ses_0037/38/39 复盘）

> 状态：**只记录，不改代码**（2026-11-14，用户「暂不修改」）。
> 用户观察：「当前的 agent 在知识库或者记忆检索后，会过度依赖检索结果」。
> 结论：**观察成立**，但要说准——不是所有路径都过度依赖，而是
> **只有 `hypothesis` 这一条结构化路径强制了「检索结果 → 待验证假设」**，
> 其余路径（`recall` 直接进证据层、`close_topic` 自由文本、最终答复）没有。
> 复核因此是**偶然发生**的，不是流程保证。
>
> 关联：口袋会话的同类问题见 [`pocket-issues.md`](pocket-issues.md)；
> 会话报告管线与运行时预算问题见
> [`session-report-issues.md`](session-report-issues.md)。
> 事实来自 `~/.vsagent/state.toml` 与 `~/.vsagent/logs/ses_0037.jsonl`。

## 一、先给答案

| 问 | 答 |
| --- | --- |
| 有没有发现 | 有，且证据比「不检索资料库」更根本 |
| 是不是「完全不复核」 | 不是。`ses_0037` 的 `hypothesis` 明确把记忆标成「尚未经核实」并最终 refuted；`ses_0038` 第二轮更正了配体身份 |
| 真正的问题 | 复核只发生在「恰好写了结构化假设」或「恰好有余量去读原始文件」时；**没有一条机制要求结论声明它依赖了哪些检索结果、那些结果是否 stale/二手** |
| 更根本的一点 | 问题 A（口袋 agent 不检索资料库）的深层原因不是「没装工具」，而是**记忆已经把证据层填满了，模型就没再觉得需要查** |

## 二、证据：一条记忆，三个会话，全部照单全收

### 2.1 同一条 `stale` 记忆被命中三次，警告全被无视

三个会话的 `recall` 返回的是**完全相同**的那条 HA2 记忆（`state.toml` 里三个会话的
`recall` tool part）：

```text
- [事实] HA2 是流感 HA0 前体的融合亚基（非独立 UniProt 条目）：group 2 可用
  P03437(H3N2 Aichi) 的 3EYM(2.80 Å) 含 TBHQ(EYK) 的 HA1/HA2 界面口袋；
  group 1 可用 P03452(PR8 H1N1) 的 8VQL/8VQM/8VQN/8VQQ(2.05–2.29 Å) 含
  A1AD* 系列（F0045(S)，PNAS 2024 PMID 38753503）的 HA stem 口袋；
  ChEMBL 对 P03437 的 pChEMBL≥7 记录为 0。（runs/）（引用文件已变更，可能过期）
```

末尾的 `（引用文件已变更，可能过期）` 是记忆系统**主动加的** `stale` 标记
（`vsagent/core/memory/retriever.py:397`）。三个会话没有一个因此去重新取证、
给结论加限定、或把该条降级为线索：

- `ses_0037`（主）：直接拿它做计划与 `hypothesis` 的 `evidence`；
- `ses_0038`（配体）：直接拿它做首轮盘点结论；
- `ses_0039`（口袋）：直接拿它当 pocket 结论的事实底座，直到最终答复。

`vsagent/app.py:939` 的注释说明这个标记要「列之前对一次账」才准；
`vsagent/core/agent/state/memory.py:210` 的 `revalidate()` 也每次 `recall` 前跑一遍——
**标记是算准了的，只是没有下游消费者**。

### 2.2 无关命中也一起进了证据层

`ses_0038` / `ses_0039` 的 `recall` 还返回了第二条**完全无关**的记忆：α7 nAChR
靶点 P36544、8V8A / 7EKP / 5AFN / 9QTN 的结构核对记录。它与 HA2 无关，
是关键词模糊匹配串进来的，同样被收进证据层占用额度（`context.assembled`
里 `evidence` 488–1081 token）。检索没有相关性阈值把关，命中即入库。

### 2.3 记忆里的说法怎么被写成结论（逐条对照）

| 记忆原文 | 被写成 | 会话 | 问题 |
| --- | --- | --- | --- |
| `8VQL/8VQM/8VQN/8VQQ(2.05–2.29 Å)`（系列范围） | `8VQQ 2.05 Å` | `ses_0039` 三次 `close_topic` + 最终答复 | 把系列的最小值安到单个结构上 |
| `A1AD* 系列（F0045(S)，PNAS 2024 PMID 38753503）` | `共晶 A1A（33 重原子，F0045 系列）` | `ses_0039` | 把「系列」等同于 8VQQ 的共晶配体本身 |
| `ChEMBL 对 P03437 的 pChEMBL≥7 记录为 0` | `严格筛 conf≥8 且 pChEMBL≥6 命中 0`（实测后确认） | `ses_0038` | 这条**恰好**被实测证实，但理由是巧合，不是流程 |
| 同一条记忆 | `8VQQ/8VQM 为 A1A（每个原体 33–34 拷贝）= F0045(S) 系列` | `ses_0038` 首轮 `close_topic` | 照抄，第二轮才更正 |

`read_fulltext`（`ses_0038`）的 Results 原文里写得很清楚：

> Based on our x-ray crystal structure of H1/PR8 HA in complex with F0045(S)
> (**PDB code 6WCR**) ...

即 **F0045(S) 的共晶结构是 6WCR，不是 8VQQ**。记忆把「F0045(S) 系列」和
「8VQL/8VQM/8VQN/8VQQ 结构组」揉在了一句话里，两个 worker 都没拆开。

`ses_0039` 的 step 11 reasoning 甚至自己撞上了这个矛盾：

> A1A in 8VQQ: 33 heavy atoms, 13.7×11.6×8.5 Å — that's the F0045(S)-like
> small molecule? Actually 33 heavy atoms is big. **Whatever.**

「Whatever」是过度依赖最典型的一刻：**遇到与检索结果冲突的观察，不停下、不复核、
直接跳过，继续沿用记忆里的说法**。

### 2.4 两个 worker 共享工作区，却给出互相矛盾的结论且无人交叉核对

同一轮、同一份登记表、同一条记忆：

- `ses_0038` 第二轮 `close_topic`：「8VQQ 的配体组分代号是 **A1ADC** …
  **不是** RCSB 组分下载里的 `A1A`——后者是 2011 年释放的 6-氨基己酸，与本题无关。
  **凡引用 8VQQ 配体一律写 A1ADC**。」
- `ses_0039` 三次 `close_topic` 与最终答复：始终写「共晶 **A1A**（33 重原子，F0045 系列）」
  「含共晶 **A1A**」「保留共晶 **A1A**」，一次都没改。

`ses_0039` 的 step 16 其实识别出 `state.toml` 里**别的会话**的旧记录与当前索引冲突，
并判定「当前索引为准」——说明它有分辨能力；但它没有、也没有任何机制让它去比对
**同一轮兄弟 worker 的结论**。共享工作区只共享了文件，没共享结论一致性。

### 2.5 知识库（corpus）检索：走的是 `bash grep`，且识别为二手后仍写入结论

三个会话里 `search_corpus` / `read_source` **0 次**（`corpus` 族没进任何计划，
见 `pocket-issues.md` 问题 A）。`ses_0038` 实际是：

- `bash grep -o -E ".{200}F0045.{300}" corpus/doc/d517819c85/text.jsonl`（工具 #28）
- `bash wc -c corpus/web/798852e561/content.md ...`（工具 #25）
- `read runs/ha2-ligand-inventory/ligand_intel.md`（工具 #26）——**上一轮自己写的成稿**

`read_fulltext` 的输出末尾明确写着「以上是原文摘录，**不含任何判断**：结论要自己
读出来」，`search_literature` 只给摘要。但 `ses_0038` 首轮 `close_topic` 就把
「文献锚点 PMID 32690700 与 PMID 38753503」写成定论。第二轮它确实识别出了问题：

> F0045(S) 的 IC50 1.6–22.8 µM 只见于本机综述语料 corpus/web/798852e561 的转述
> （引文 66），**属二手，需回原文核对**。
> PMC11145270 Abstract 只给「subnanomolar」这一形容词，**无具体数值**，
> 不可写成 IC50=x nM。

识别对了，但**因为步数用尽没回原文**，数值仍留在结论里、只挂了个「待验证」。
而且用 `bash grep` 读语料，连 `read_source` 那种工具级 provenance（来源句柄）都没有，
「这是二手」只能靠模型自己看出来。

### 2.6 取证类工具调用统计（三个会话合计）

| 工具 | 次数 | 工具 | 次数 |
| --- | --- | --- | --- |
| `recall` | 3 | `add_evidence` | **0** |
| `search_literature` | 1 | `register_object` | **0** |
| `read_fulltext` | 2 | `review_hypothesis` | **0** |
| `search_corpus` | **0** | `list_objects` | 2 |
| `read_source` | **0** | `hypothesis` | 4（全在 `ses_0037`） |

检索结果进得多，**登记/反证类动作一次都没有**。

## 三、机制层根因：为什么系统设计让它容易发生

1. **检索结果在上下文里的身份是「证据」，不是「线索」。**
   `vsagent/core/agent/state/memory.py:183-188`：
   ```python
   # 收证据收集器。
   # 检索结果要进 EVIDENCE 层：记忆是跨会话的知识，RECENT 里本来就没有它，
   # 不给一份到证据层，下一步的 RECENT 一被压缩这段知识就没了。
   evidence_argument = "evidence"
   ```
   `vsagent/core/agent/execution/loop.py:269-273` 把「检索回来的记忆」和
   「含关键行的工具结果」并列为「按需证据」。
   设计意图是**别让知识被压缩掉**，副作用是模型把它读成「已经查证过的东西」。

2. **措辞把它定性为「事实」。**
   `state/memory.py:161`：「按关键词检索项目记忆，返回之前记住的项目**事实**」；
   `vsagent/core/agent/state/task.py:5`：「记忆是跨会话的项目**事实**」。
   工具层没有「这是待验证线索」的任何提示。

3. **五种状态信号只渲染成文本后缀，没有结构化字段、没有校验器消费。**
   `vsagent/core/memory/retriever.py:385-400` 会拼出
   `（引用文件已变更，可能过期）` / `（引用的文件不在了）` / `（引用未确认）` /
   `（已被 X 取代）` / `（与 X 冲突，未解决）`。这些是**拼在事实那一行的字符串**，
   不是可查询的置信度字段；`add_evidence` / `review_hypothesis` 0 次，
   也没有任何 validator 检查「结论是否依赖了 stale / unverified 的条目」。

4. **交付验收不查 provenance。**
   `vsagent/core/agent/validators/delivery.py:3` 开头写明「它**不阻断**」；
   `vsagent/core/agent/roles/profiles.py:61` 只要求 `evidence_refs` 非空。
   所以「一条 `stale` 记忆 + 一句 `Whatever`」完全可以满足验收（呼应
   `pocket-issues.md` 问题 A 第 6 条）。

5. **反证机制只覆盖 `hypothesis` 对象。**
   `vsagent/core/agent/state/hypothesis.py:96` 的 `hypothesis` 要求
   `evidence` / `test` / `expected` / `rivals` / `discriminator`，
   `vsagent/core/agent/domain/research.py:278` 的 `review_hypothesis` 能结案。
   但 `close_topic` 的 `summary`、最终答复、`recall` 的直接消费都是自由文本，
   不经过这套结构。**约束是局部的**。

6. **缺 `corpus` 族 → 语料检索走 `bash`，连工具级 provenance 都没有。**
   `vsagent/core/agent/domain/research.py:126` 的 `add_evidence` 与
   `corpus` 族的 `search_corpus` / `read_source` 在本轮计划里对每个角色都不可见
   （两轮 `route.plan` 能力集合一致，均无 `literature_evidence`）。

7. **步数预算让「回原文核对」最先被砍。**
   `ses_0038` 明确写了「属二手，需回原文核对」，但 24 步用满、写盘被拒；
   `ses_0039` 尾部 5 次 FAILED 输出全是「本轮步数快用完了，不再展开新工作」。
   复核是最容易被牺牲的那一步。

## 四、边界：哪里其实做对了（避免过度归因）

这一节很重要——问题不是「模型不会复核」，而是**复核没有机制保证**。

- **`ses_0037` 的 `hypothesis` 是正面样本。** 工具 #7–#9 的 `evidence` 字段原话：
  > 项目记忆 mem_20261109132137_1c2923：group 2 一侧有含共晶配体的实验结构；
  > 同一记忆记录 ChEMBL 对该靶点的 pChEMBL≥7 记录为 0
  > （**数据库级，尚未经本轮配体盘点核实**）。

  它给记忆打了「未经核实」的标注、写了 `test` 与 `discriminator`、设了对手
  `rivals`，最后用 `ses_0038` 的盘点把 `hyp#1` 记为 **`refuted`**（工具 #19）：
  「两者都不成立」。也就是说，**只要走结构化假设这条路，检索结果确实被当成
  待验证线索并接受了反证**。

- **`ses_0038` 第二轮主动纠偏。** 更正 A1A → A1ADC、指出 A1A 是无关的
  6-氨基己酸、识别二手语料、指出 Abstract 无具体数值不可写成 IC50。
  代价是它自己多跑了 `bash` 读 mmCIF 与全文——**因为有余量才发生**。

- **`ses_0039` 的 step 16 也分辨过。** 它发现 `state.toml` 里别的会话的旧记录
  与当前索引编号相反，判定「当前索引为准」。

结论：模型有能力识别过期、二手、矛盾；但触发条件是「恰好写了 `hypothesis`」或
「恰好有步数去读原始文件」。**没有机制要求每条结论都声明证据来源与状态**，
所以同一条 `stale` 记忆在 `hypothesis` 里被标「未核实」、在 `close_topic`
里却被写成定论。

## 五、与口袋会话两个问题的关系

- **对问题 A（不检索资料库）的修正**：根因不只「没装 `corpus` 族」。
  `recall` 一上来就把记忆塞进 EVIDENCE 层，模型据此认为「事实已经有了」，
  于是既没动机装 `corpus`，也没动机 `add_evidence`。**过度依赖是 A 的深层原因**，
  补族只是必要条件之一。
- **对问题 B（`bash` 18 次）的补充**：`ses_0039` 那 6 次
  `grep state.toml` 找句柄/口袋名，本质是在给记忆里的说法**找补**；
  `ses_0038` 的 `bash grep corpus/*` 则是用本地文件替代语料检索。
  两处都是「检索结果不够／不确定 → 用 `bash` 兜底」，而不是「重新取证」。
- **一个更具体、也更严重的实例（问题 D）**：记忆只点名两处口袋，
  `ses_0039` 就只取记忆点名的两个 PDB、只跑 `sources="ligand"` 单来源，
  最终按「两处候选口袋」交付，全程没有做覆盖检查——连
  `structure_discovery`（带 `survey_structures` 全景工具）都没进能力计划。
  即「检索结果给了**全集框架**，模型就把它当全集」，而不只是当**事实**。
  详见 [`pocket-issues.md`](pocket-issues.md) 问题 D。
- **共同的收口点**：`delivery.check()` 不查 provenance、
  `evidence_refs` 只查非空、`add_evidence` / `review_hypothesis` 0 次。

## 六、影响

- 结论里混入未经核实、已标记过期、甚至无关的记忆；`stale` 警告形同虚设。
- 同一条记忆被两个 worker 消费后得出**互相矛盾**的结论（A1A vs A1ADC），
  共享工作区没有一致性检查，父会话汇总时无从发现。
- 与 `project_charter_hypothesis.md` 记的垂直定位冲突：产出应是「假设体系 +
  可反证证据」，而现在证据层里混着上一轮的结论、二手转述和自己手算的数字。
- 「首轮照抄 → 第二轮纠正」的模式意味着：**如果只有一轮（很多任务就是一轮），
  错误结论会直接交付**。

## 七、待选的修法（**均未实施**）

1. **改定性**：`recall` 的输出在证据层里标成「线索 / 待验证」，而不是「事实」；
   工具描述与 `state/task.py` 的「项目事实」措辞一并改。
2. **结构化状态**：`retriever.py` 把 `stale / missing / unverified / superseded_by /
   conflicts` 作为字段返回（不只拼文本后缀），让下游能按状态过滤或降级。
3. **机械检查**：结论交付前检查「所依赖的证据里有没有 stale / unverified 条目」，
   有则要求重新取证或显式标注。落点要避开 `delivery.check()` 现有的
   「不阻断」约定——另设一个只读的提示/记分，而不是硬拦。
4. **把 `hypothesis` 的约束外溢**：`close_topic` 的 `summary` 与最终答复里，
   凡引用记忆/语料/文献，要求带来源句柄与状态（类似 `hypothesis.evidence`
   里那句「尚未经核实」）。
5. **交叉一致性**：父会话汇总时比对同一轮各 worker 对同一实体的命名
   （如 8VQQ 的配体代号），不一致就提示，而不是静默取一份。
6. **给 `corpus` 族一个入口**：让 `search_corpus` / `read_source` 至少在派发预览里
   可见，避免知识库检索退化成 `bash grep`（同时补上工具级 provenance）。
7. **预算保护**：把「回原文核对」列为收束阶段的放行工具之一，
   别让它成为步数不足时第一个被砍的动作。

## 八、核对命令 / 出处

```bash
python3 - <<'PY'
from vsagent.core.session_manager import SessionManager, default_state_path
sm = SessionManager(default_state_path()); sm.load_from_toml()
for sid in ("ses_0037", "ses_0038", "ses_0039"):
    for m in sm.sessions[sid].messages:
        for p in m.parts:
            if getattr(p, "tool_name", None) == "recall":
                print("====", sid, "IN", p.tool_input)
                print(p.tool_output)
PY
```

代码出处：

- `recall` 进证据层：`vsagent/core/agent/state/memory.py:183`、`:188`、`:210`
- `recall` 描述「项目事实」：`vsagent/core/agent/state/memory.py:161`
- 证据层注释：`vsagent/core/agent/execution/loop.py:269`
- 记忆状态后缀：`vsagent/core/memory/retriever.py:385`、`:397`
- 记忆重校验：`vsagent/app.py:939`
- 「记忆是项目事实」：`vsagent/core/agent/state/task.py:5`
- 假设 / 证据 / 反证工具：`vsagent/core/agent/state/hypothesis.py:96`、
  `vsagent/core/agent/domain/research.py:126`、`:278`
- 交付验收不阻断、报告只查非空：`vsagent/core/agent/validators/delivery.py:3`、
  `vsagent/core/agent/roles/profiles.py:61`
- 来源提示只渲染一行：`vsagent/core/agent/planning/intent.py:85`

## 九、不改的边界

按用户 2026-11-14 的指示，本文**不**改代码、**不**改规则文件。
上面七条「待选」只作下次开工的候选，实现前需重新评估两点：
一是主 agent 纯编排（`project_principal_orchestrator.md`）不装领域族，
新增的取证动作该落在哪个角色；二是 `load_tools` 的前缀缓存代价
（`vsagent/core/agent/toolset.py:207` 记的「十族约 2.6 万 token」），
别把修法又变成新的 token 负担。
