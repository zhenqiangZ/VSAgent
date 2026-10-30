# 评测计划（VScreenBench + 系统评测）

本文件回答一个问题：**这套系统要拿去投顶刊，评测该怎么设计。**
先列已核对的事实与现有资产，再给分层方案、任务集规格、指标口径、消融与基线，
最后落到工程改动。判断都建立在读数上，不凭印象。

与 `docs/screening-tools-plan.md` 的关系：那份管「工具够不够」，这份管「怎么证明它有用」。

---

## 0. 已核对的事实

### 0.1 现有评测资产

| 脚本 | 管什么 | 进 CI | 能否当论文证据 |
|---|---|---|---|
| `tests/test_agent.py` | 内核行为，约 3,000 条断言（按组跑，`--only` 秒级） | 是 | 只能进 Supplement：证明实现正确，不证明任务能做成 |
| `tests/test_smoke.py` | 界面 + 内核超集，约 3,600 条 | 是 | 同上 |
| `tools/check_retrieval.py` | 词法单路 / 双词法 / 三路 + 组级指标，三条硬门禁 | 是 | 可作 S3 检索消融的支撑材料 |
| `tools/check_web_api.py` | API 形状、并发、写撕裂读、语料读写 | 是 | 工程附录 |
| `tools/check_demo_data.py` | 纯几何：口袋残基是否真接触共晶配体（≤4.5Å、≥60%） | 是 | 可复用为 L2 的口袋正确性判据 |
| `tools/check_retrieval.py --list` | 评测集概况 | 否 | 数据集描述 |
| `tools/eval_context.py` | 上下文装配、`--sweep`、`--evidence` 证据探针 | 否 | 可直接做「上下文工程」消融 |
| `tools/eval_memory.py` | 记忆台账健康度、recall@k、stale 比例 | 否 | 记忆消融 |
| `tools/replay_cost.py` | 按当前装配器重放历史会话，与 trace 逐层对照 | 否 | 成本一节的主证据 |
| `debug_tools.py` | 42 个领域工具 44 次真实调用，顺序即依赖顺序 | 否 | 只能证明「工具能跑通」 |

**读数（写这份时）**：领域工具 42 个 / 18 族；核心常驻 14 个；合计 56 个工具。
`test_agent.py` 3,036 条、`test_smoke.py` 3,651 条（2026-10-27 读数，随改动漂）。

### 0.2 被评对象是什么

六段流程：查靶点 → 取结构 → 定口袋 → 备受体 → 备配体 → 对接 → 亲和力 → ADMET → SAR → 导出运行目录。
每一段的终态都落在盘上（`runs/<id>/`、`~/.vsagent/ledgers/<sid>/`），
**这是能程序化判分的前提**，比「让模型自述做了什么」可靠得多。

### 0.3 三个结论

1. **现有资产全是「实现正确性」，没有「任务成功率」。** 3,000 条断言保证工具不改坏，
   但回答不了「给一句话需求，它能不能筛出东西」。论文正文需要的是后者。
2. **外部依赖是活的，评测不可复现。** `domain/http.py` 是全仓唯一网络出口，
   UniProt/ChEMBL/RCSB/PubMed 的返回每天都在变。同一任务今天跑通、下月复现不了，
   审稿人一复算就穿帮。
3. **没有基线分离。** 现在无法回答「增益来自 agent 编排，还是来自这 42 个工具本身，
   还是来自底座模型更强」。这是拒稿意见里出现频率最高的一条。

三条结论对应三件必做的事：建任务集、冻结网络、加对照。

---

## 1. 主张与投稿定位

**2026 年「一个能跑虚拟筛选的 LLM agent」本身已经不新。** 评测必须围绕一个可辩护的
主张来设计，否则做得再多也是「又一个系统」。

| 主张 | 需要什么证据 | 可投 |
|---|---|---|
| A. 系统能在真实筛选流程上稳定完成任务 | L1 + L2 + 消融 | Nat. Mach. Intell. / Nat. Commun. / JCIM |
| B. 这套任务集本身是社区缺的基础设施 | 任务集 + 参考实现 + 榜单 + 基线复现 | Nat. Commun. (resource) / JCIM / J. Cheminform. |
| C. 它做出了人类没做出的发现 | L3 湿实验或前瞻性盲测 | Nature / Science / Cell |

**建议走 A + B 双贡献**：先发 benchmark，再发系统主文，评测工作量只做一遍，
两篇互相引用。C 作为加分项，取决于是否有实验条件。

---

## 2. 五层评测

| 层 | 问的问题 | 规模 | 产出 |
|---|---|---|---|
| L0 回归 | 改坏了没有 | 现有 6,600+ 断言 | Supplement |
| L1 任务级 | 给定需求能不能做成 | 60–100 任务 | 主表 1 |
| L2 领域正确性 | 筛出来的东西对不对 | 8–12 靶点 | 主表 2 |
| L3 科学产出 | 有没有产生新知识 | 1–3 个 case | 主图 |
| L4 可靠性与安全 | 每次都能做成吗、代价多大 | L1 全集 ×5 | 主表 3 |

L0 不进正文。L1–L4 每一层都要有 CI 版本与论文版本两套口径：CI 跑小样本（10 任务 ×1 次），
论文跑全量（×5 次）。

---

## 3. L1：任务集（VScreenBench）

### 3.1 分层

| 层 | 数量 | 例 | 判分方式 |
|---|---|---|---|
| L1 单点 | 20–30 | 「取 4HJO 的 A 链」「这个受体有几个口袋、选最大的」 | 程序化：产物字段 / 文件存在 / 值在容差内 |
| L2 流程 | 30–40 | 「对 EGFR 跑一遍筛选，给我 top 20 并说明依据」 | 程序化终态 + 规则检查 |
| L3 开放 | 10–20 | 「找 KRAS G12C 的候选，按可成药性排序」 | 人工 + 程序混合，双人盲评 |

L3 不能超过 20%——开放式任务判分成本高、方差大，占比一高整篇文章的可信度反而下降。

### 3.2 任务规格

一个任务一个 YAML，放 `tests/eval/tasks/<id>.yaml`：

```yaml
id: l2-egfr-001
level: L2
prompt: "对 EGFR（UniProt P00533）做一次结构基虚拟筛选，给出 top 20 候选与排序依据。"
fixtures:                      # 冻结后的网络响应，见 §9.1
  - uniprot:P00533
  - rcsb:4HJO
budget:
  max_steps: 40
  max_tokens: 300000
  wall_clock_s: 1800
check:                         # 终态校验，不看自述
  - kind: artifact_exists
    path: "runs/*/docking.json"
  - kind: field_range
    path: "runs/*/docking.json:scores[0]"
    max: -6.0
  - kind: residue_overlap
    pocket: "runs/*/pocket.json"
    reference: "4HJO:A:LYS745"
    min_ratio: 0.6
```

三条硬约束：

- **`check` 只能读盘**，不能读 transcript。模型说「我做完了」不算。
- **每个任务有 `budget`**，超了算失败——否则「跑 200 步碰对」和「12 步做对」同分。
- **`fixtures` 必须列全**，缺一个就说明这个任务依赖了未冻结的外部状态。

### 3.3 终态校验的实现位置

复用现有两个真相源，不新造：

- `vsagent/core/agent/ledger.py` 的 `SessionLedger` — handle 与产物路径
- `vsagent/core/agent/domain/products.py` — 原子写盘的运行目录

`check` 的 `residue_overlap` 直接调 `tools/check_demo_data.py` 里那套几何判据
（≤4.5Å、≥60%），不要另写一份——两份口径必然漂。

### 3.4 污染与盲化

这里和 NatureBench 不同：**不能靠禁网**。你的 agent 查的就是活数据库。

真正的污染路径是「答案已经在 ChEMBL / 文献 / 本地语料里」。三道防线：

1. **盲靶点**：选活性数据未进公开集的靶点，或内部数据；
2. **时间切分**：用 2026 年之后才进 PDB 的条目，并在报告中写明截止日；
3. **语料隔离**：跑评测时把 `corpus/` 里含答案的源临时移走，并在方法里写明。

审稿人一定会问「你是不是检索到了答案」。**主动做一次泄漏审计**（把答案源放进语料，
看分数涨多少），比被问出来强。

### 3.5 通过判据

任务成功 = 所有 `check` 通过 且 未超 `budget` 且 无越权操作。
三件事任一不满足都记失败，**不设「部分成功」**——部分成功会让主表无法比较。

---

## 4. L2：领域正确性

### 4.1 指标

| 指标 | 口径 | 为什么用它 |
|---|---|---|
| EF1% / EF5% | 传统富集因子 | 与文献可比，但**必须注明上限受阴阳比限制** |
| EF_B | Bayes 富集（BayesBind 提出） | 不假设诱饵真的无活性，适合大规模库 |
| BEDROC α=20 | 早期识别 | 活性率 0.02% 时比 ROC-AUC 有意义 |
| PR-AUC | 极端不平衡 | 主指标之一 |
| Top-N 命中数 | N=20/50/100 | 最贴近实验者真正会送样的数量 |
| PoseBusters 通过率 | 键长/键角/ clash/ 立体化学 | 对接分数高但位姿物理上不成立，是常见坑 |
| RMSD<2Å 比例 | 有共晶时 | 位姿正确性 |
| 口袋残基覆盖率 | ≤4.5Å、≥60% | 复用 `check_demo_data.py` |

**不要只用 ROC-AUC。** 活性率低到 0.02% 时它会给出虚假的安心感。

### 4.2 靶点集

8–12 个，避开 LIT-PCBA 已被审计出泄漏的靶点（arXiv 2507.21404：2,491 个 2D 相同阴性
跨 train/val，ALDH1 单靶点 323 对 Tanimoto≥0.6 类似物，无参数记忆基线即可打平 SOTA）。
优先顺序：

1. BayesBind 靶点（按结构相似度切分，专为 ML 设计）
2. 自建时间切分集
3. 有高质量共晶、且活性数据不在训练常用集里的靶点

LIT-PCBA / DUD-E 可以用，但只能作为「与文献可比」的次要表，且在正文写明其局限。

### 4.3 负对照（必须有）

| 对照 | 期望 | 说明什么 |
|---|---|---|
| 已知无结合物的靶点 | 分数不显著高于随机 | 不是靠打分器瞎排 |
| 打乱配体集 | 富集消失 | 排序不是靠分子本身的性质偏置 |
| 空口袋 / 错误口袋 | 失败或显著降分 | 口袋选择是真的在起作用 |
| 去掉共晶配体的受体 | 分数下降但不断崖 | 不过度依赖泄漏 |

负对照失败（agent 在无活性靶点上照样「筛出高分候选」）说明它在编造，
这一条比任何正结果都更能说服审稿人。

---

## 5. L3：科学产出

三档，按条件选：

1. **前瞻性回顾**（最低成本）：先冻结 agent 的 top-50 排序，再与文献/专利已知活性比对。
   关键是**先冻结再比对**，否则就是数据窥探。
2. **人机一致性**：3–5 位专家独立排序，算 Spearman / Kendall + top-10 重合。
   参照 Robin：LLM 与专家 top-10 重合 7.25/10，judge 内部一致性 88% vs 人类 61%。
3. **湿实验**（顶刊门槛）：top-10 送 SPR/ITC/细胞活性。哪怕只验证 10 个，
   也足以把文章从「系统论文」抬到「发现论文」。

配套必须报**负结果**：哪些任务稳定失败、失败在感知（看不见工具）还是判断（选错方法）。
NatureBench 的结论就是「失败主要来自方法选择与算力，不是理解」——这类分析比多刷 2 个点有分量。

### 5.1 主口径：按假设后来被证实还是证伪算

这个 agent 的产出单位是假设（六要素：主张、机制、证据、检验、预期、否证条件），
不是一张排名。所以 L3 的主指标是**结案率**，不是回测 AUC：

| 指标 | 算法 | 说明什么 |
|---|---|---|
| 结案率 | 已结案假设 / 提出的假设 | 假设是否真的被检验过，还是提完就搁着 |
| 支持率 | supported / (supported + refuted) | 判断的质量；判不了的单独算，不混进分母 |
| 判不了比例 | inconclusive / 已结案 | 预期写得含糊的程度——`propose` 只挡「完全没有数值」，挡不住「写了但分不清真假」 |
| 闭环延迟 | 假设提出到 `record_assay` 回填的步数 | 假设是不是事后补写的 |

**不看回测 AUC**：AUC 量的是打分器的排序能力，换个库、换一批诱饵就能刷高，而
「这条路线该不该走」与库无关。同一份对接产物，agent 说得出「这批活是因为埋进 S1 口袋
的疏水通道」，与它只给出前 20 名，AUC 一模一样——前者才是要评的东西。

数据来源：`write_run` 的 `provenance.hypotheses`（每条带 `status` 与 `result_ref`），
`record_assay` 是唯一写入口；`trace.py` 的 Step 流水给闭环延迟。

判据：`inconclusive` 超过 30% 说明预期写得不够硬，先改 `hypothesis` 的描述，
不要先调评测集。反例也要报：只提一条策略假设、实验数据回来不挂 `hypothesis`
——结案率为零，这套东西就只是文案。

消融配套：去掉 `hypothesis` 工具（其余不动）跑同一批任务，看结案率与「排名里有没有
方向」这两项，而不是看分数高低。

---

## 6. L4：可靠性与安全

| 维度 | 指标 | 数据来源 |
|---|---|---|
| 稳定性 | pass@1、**pass^5**、Wilson 95% CI | L1 每任务 5 次 |
| 成本 | 每成功任务 token / 美元 / 墙钟 / 工具调用数 | `trace.py` 的 jsonl 聚合 |
| 效率 | 步骤数、无效调用率、重试率 | 事件层 Step 流水 |
| 恢复力 | 注入 API 超时 / 文件缺失 / vina 不存在 后的成功率 | `RetryableToolError` 路径 |
| 长会话 | >50 步任务在压缩后的保持率 | `eval_context.py --sweep` |
| 安全 | 越权写盘拦截率、危险 bash 拦截率、Plan 模式遵守率 | 20 条攻击性任务 |
| 幻觉 | 引用不存在的 PDB / 文献 / 数值的比例 | 与 Robin 的 44.5% 同口径 |
| 人机 | 人工介入次数/任务、专家盲评、完成时间比 | ≥10 位专家，报 ICC / Krippendorff's α |

pass^k 的意义：单次 75% 成功率的 agent，连续 3 次全对只有约 42%。
顶刊审稿现在基本会问这个数。

---

## 7. 消融清单

每个消融**同一底座模型、同一 harness、同一任务集，只改一个变量**。

| 去掉什么 | 预期观测 | 代码位置 |
|---|---|---|
| 按族加载 → 56 工具全常驻 | 每步 token ↑；工具选择准确率变化 | `toolset.py`、`prune_families` |
| 事件层（Phase→Step） | 长会话保持率 ↓、`read_turn` 调用 ↑ | `event_log.py` |
| 证据层 / FOCUS 复述 | 重复取数、handle 推错次数 ↑ | `context.py` |
| 记忆 | 跨会话任务成功率 ↓ | `memory.py` |
| 三路检索 → 只跑词法 | 文献类任务命中率 ↓ | `check_retrieval.py` |
| 子 agent | 并行任务墙钟 ↑ | `subagent*` |
| 工具重试 | 单次抖动导致的整轮失败率 ↑ | `MAX_TOOL_RETRIES` |
| `close_phase` 命名 | Phase 标题/结论质量 ↓（可人工评） | `event_log.py` |

前四条是「上下文工程」这条线的核心卖点，**必须做**；后四条可放 Supplement。

---

## 8. 基线四级

按「能分离出什么」排序，从弱到强：

| # | 基线 | 分离出什么 |
|---|---|---|
| B1 | 裸模型 + ReAct+Code（无领域工具） | 领域工具的价值 |
| B2 | 通用 agent（Claude Code / Codex）+ 同一套 tool schema | 编排层的价值 |
| B3 | **同工具、无 agent 的脚本化 pipeline**（固定顺序调 42 个工具） | **agent 决策本身的价值** |
| B4 | 经典方法：Vina / Gnina / DiffDock / 2D 相似性 / 共晶重对接 | 领域 SOTA |

**B3 是决定性的**。只有它能回答「增益是 agent 带来的，还是把工具串起来就有的」。
缺 B3，审稿人会说你的贡献只是「把现成工具包了一层壳」。

另外必须做「同模型开关工具」的配对对照（Biomni 的 Biomni-ReAct、ToolUniverse 的
with/without 都是这个逻辑），否则模型能力与系统能力混在一起。

---

## 9. 工程落点

### 9.1 网络冻结（第一优先）

在 `domain/http.py` 的四个出口（`get_json` / `post_json` / `get_text` / `probe`）加一层
VCR 式记录/回放：

- 键：`(方法, URL, 关键 query, 请求体摘要)`
- 录制目录：`tests/eval/fixtures/<host>/<hash>.json`，**进仓**
- 环境变量：`VSA_HTTP_MODE=live|record|replay`，默认 `live`（生产行为不变）
- `replay` 命中不到就**报错而不是回落 live**——回落会让评测悄悄依赖网络

这一步同时解决可复现、CI 可跑、评审可离线复算三件事，成本最低收益最高。

### 9.2 `tools/eval_agent.py`

```
python tools/eval_agent.py --list
python tools/eval_agent.py --task l2-egfr-001 --repeat 5
python tools/eval_agent.py --level L1 --repeat 1 --json .probe/eval.json
python tools/eval_agent.py --matrix          # 消融矩阵，逐项关组件
```

不做新框架，直接复用：`AgentLoop` + `FakeModelServer`（测试已有）+ `TraceWriter`
+ `SessionLedger`。判分器单独一个模块 `tests/eval/checks.py`，按 `kind` 注册，
**不依赖 agent 的内部状态**。

### 9.3 报表字段

一次运行一行 JSONL，至少包含：
`task_id / level / run_index / success / failed_checks / steps / tokens_in / tokens_out /
wall_clock_s / tool_calls / tool_errors / retries / interventions / cost_usd /
model / harness_version / fixture_hash`。

`harness_version` 与 `fixture_hash` 必须写——没有它们，跨时间比较不成立。

### 9.4 与现有探针的分工

| 探针 | 管 |
|---|---|
| `replay_cost.py` | 成本随会话长度的变化（离线，不调模型） |
| `eval_context.py` | 上下文装配是否退化 |
| `eval_memory.py` | 记忆召回质量 |
| `check_retrieval.py` | 检索三路的组级指标 |
| **`eval_agent.py`** | **任务能否做成、代价多大** |

前四个是组件级，最后一个是系统级。论文里分开报，不要混成一张表。

---

## 10. 统计与报告

- 每个成功率给 **95% CI**（Wilson 或 bootstrap），不只给点估计。
- 配对比较用 **McNemar**（同一任务集 A vs B），不要用两个独立比例的卡方。
- EF 类指标的 CI 用 **对活性集 bootstrap**，不要对分子 bootstrap。
- LLM judge 必须报与人工标注的一致率（≥50–100 条校准集），且 judge 模型与 agent 不同家族。
- 公开：prompt 全文、tool schema、任务定义、判分脚本、trace 样例、镜像 tag。
  **不公开答案**。
- 明确区分「自己复现的数字」与「引用他人报告的数字」。

---

## 11. 本轮不做

- 不做多 agent 编排对比（当前形态是单 agent + 可选子 agent，硬凑会失真）
- 不做蛋白结构预测质量评测（不是本系统的能力边界）
- 不追求 LIT-PCBA 上的 SOTA（该基准已被证明有泄漏，刷分没有意义）
- 不建在线榜单服务（先出静态任务集 + 参考实现）

---

## 12. 判据

按现有习惯，每条都要能「一次注入就变红」：

1. `eval_agent.py --task <id> --repeat 1` 在 `replay` 模式下全绿，断网也能跑完。
2. 任一任务的 `check` 里出现读取 transcript 的调用 → 判据失败。
3. 同一任务连跑 5 次，`fixture_hash` 与 `harness_version` 一致；不一致 → 失败。
4. 删除某个消融组件后，报表里对应指标必须变化；不变说明消融没接上。
5. B3 基线（脚本化 pipeline）必须能跑通同一批任务；跑不通说明任务定义依赖了
   agent 的自主决策，L2 层要重写。
6. 负对照靶点上的 EF1% 不得显著高于随机（bootstrap 95% CI 覆盖 1.0）。
7. 报表缺 `cost_usd` / `interventions` 字段 → 失败（成本是主表的一部分，不是可选）。

---

## 附：三条最优先

1. `http.py` 加 record/replay（§9.1）——没有它，后面所有数字都不可复现。
2. `tools/eval_agent.py` + 首批 10 个 L1 任务（§9.2）——先把链路跑通，再谈规模。
3. B3 基线（§8）——把「系统工程」与「科学贡献」分开的唯一办法。
