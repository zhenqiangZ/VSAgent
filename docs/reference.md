# 资料与判据

> 角色：资料（外部依据、判据提炼、外部依赖与路径）
> 状态：随代码更新，最后核对 2026-11-06
> 配套：`design.md`（现行设计）、`history.md`（为什么这么定）、`review.md`（实测读数）

这份文件收三类东西：判断「什么算数」的外部依据、外部服务与本机路径、以及环境变量。
新增工具、改系统提示、做评测之前先对一遍第一节。

---

## 一、判据（Roche 2016 复盘）

> 来源：Kuhn, Guba, Hert, … Stahl. *A Real-World Perspective on Molecular Design*.
> J. Med. Chem. 2016, 59, 4087−4102。10 个前瞻性案例，13 个新 PDB 条目。
> 原文 PDF 在 `docs/jm5b01875.pdf`（8.8 MB）。

### 1.1 两条效用判据

任何分子设计手段，只按这两条判：

1. 它是否导向可实验检验的预测；
2. 无论预测对错，实验结果是否增进对优化空间的理解。

原文原话是「The primary deliverable of molecular design is an idea」——交付物是想法，
成功是「对改进化合物有实质贡献」。

### 1.2 五条原则

| 原则 | 原文要点 | 对本项目意味着 |
| --- | --- | --- |
| 定性优于定量 | 项目影响多来自一个洞见或一个方向，排名是原料 | 排名之后必须有「为什么活」这一步 |
| 塑造化学空间 | 先判断当前是组外还是组内，再选工具 | 选工具本身是决策 |
| 简约 | 能用实验数据（CSD、已知配体、HTS）就不上第一性原理 | 方法升级要有门槛与理由 |
| 注释即一半的仗 | 前置加工把复杂查询退化成一次查找或一次可视化 | 注释层与可视化是交付的一部分 |
| 贴近实验 | 用实测参数、现有砌块、已知配体与 SAR | 先问「有没有现成的」，再问「要不要算」 |

### 1.3 三个筛选案例

| 案例 | 做法 | 可复用的判据 |
| --- | --- | --- |
| SST5R | 不做 HTS、无 SBDD；按跨膜口袋局部序列相似性排序已知配体的受体，测一批生物胺配体 → astemizole | 数据基础（注释库 + 已比对的口袋序列）比算法更决定成败 |
| L-CPT1 | 约 5000 个初筛阳性 → 八点 IC50 留约 2200 个（IC50 < 50 μM）→ 叠合发现共享手性形状包络 → 同源模型区分两个对映包络 → 一对对映体验证 | 从结果里归纳假设，用一对化合物证伪；刻意停在形状分析 |
| PDE10A | 多结构热点网络分析 → 预测 6 位氯占据卤素热点 → 合成验证 >10 倍 | 把结构注释成一张热点图，查询退化成查找 |

另外三个（FABP4/5、CatS/L、HSL）说明：残基差异、静电互补、甚至 12−14% 同源度的模型都能
产出可检验假设，但每一个都靠一对化合物验证（同系物、电荷对、对映体）。

### 1.4 原文明确反对的

- 把分数当结论：「any ranked list should be seen as raw input」。
- 对接分数当亲和力：正确用法是尽可能多的约束加大量人工检视，任何结合模式未经审视不能接受。
- 过度建模：L-CPT1 主动停在形状分析。
- 不可验证的计算：MD 轨迹、水网络熵、适用域外的 FEP；至少要能说清基线是什么。
- 自我一致的 QSAR：只反映手头数据，没有预测力，且是黑箱，只能当过滤器。
- 单一结合模式：β-tryptase 的骨架替换成功了，晶体却显示中央片段翻转成镜像——
  「never trust a binding mode hypothesis unless all conceivable alternatives have been explored」。

### 1.5 本项目的五条判据

1. 新工具的价值：能不能产出可证伪的方向。只加一个打分器，不算。
2. 排名是原料：`rank_candidates` 之后必须有「这批为什么活」。
3. 升级门槛：2D 相似 → 形状 → 药效团 → 对接 → 亲和力，每升一级要说明简单方法为什么不够。
4. 每批结果带对照：对映体、同系物，或「应该不结合」的分子，`pick_controls` 的三类至少用两类。
5. 评测按前瞻影响力：假设后来被实验证实或证伪，而不是回测 AUC 与富集因子。

### 1.6 判据落到代码里的位置

| 位置 | 内容 |
| --- | --- |
| `AGENTS.md` 的「判据」一节 | 十条，逐条能判真假 |
| `vsagent/core/models.py` 的 `Hypothesis` | 六要素字段，`test` 与 `expected` 必填 |
| `vsagent/core/agent/prompt.py` | 八条假设规则，紧跟身份 |
| `vsagent/core/agent/hypothesis.py` | `close` / `record_assay` 才能改状态 |

---

## 二、外部论文与数据源

| 来源 | 用途 | 在本项目里的位置 |
| --- | --- | --- |
| Kuhn 等, *J. Med. Chem.* 2016, 59, 4087 | 判据来源 | 本文件第一节；`AGENTS.md` |
| WLS 抑制剂 AL-Glide 筛选论文（Enamine REAL 约 5 亿分子、3 轮主动学习、86 个下单、68 个合成、2 个活性、IC50 6–8 µM） | 挑选规则与退出条件 | 只取了规则，算力规模不在讨论范围 |
| LIT-PCBA 泄漏审计（arXiv 2507.21404） | 说明基准不可直接刷分：2,491 个 2D 相同阴性跨 train/val，ALDH1 单靶点 323 对 Tanimoto≥0.6 | `history.md` §9.1 的评测方案 |
| BayesBind | 按结构相似度切分、为机器学习设计的靶点集 | 评测候选靶点集的首选 |
| Robin（LLM 与专家 top-10 重合 7.25/10，judge 一致性 88% vs 人类 61%） | 人机一致性口径 | 评测的 L3 一节 |
| PDBbind v2020 | DTA 微调的预训练数据 | `models/DTA/` |

---

## 三、外部服务

全部网络请求走 `vsagent/core/agent/domain/http.py`，测试整体替换这一个模块。

| 服务 | 工具 | 是否计费 | 备注 |
| --- | --- | --- | --- |
| UniProt | `fetch_target`、`search_targets` | 免费 | 靶点身份、序列、结构计数 |
| RCSB PDB | `fetch_structure`、`search_pdb`、`describe_entry` | 免费 | 实验结构、`chem_comp` 与同义词 |
| ChEMBL | `search_actives`、`find_analogs` | 免费 | 活性数据要 `spread=True` 才能拿到完整分布 |
| PubChem | `lookup_compound`、`find_similar_compounds` | 免费 | |
| Europe PMC / PubMed | `search_europepmc`、`search_literature` | 免费 | |
| Open Targets | `search_targets`、`disease_targets` | 免费 | 疾病与靶点关联 |
| AlphaFold | `fetch_alphafold` | 免费 | 无实验结构时顶替，要看口袋的 pLDDT |
| p2rank | 预测口袋 | 本地进程 | 口袋的第三层来源 |
| 网页搜索 | `search_web`、`fetch_webpage` | 约 0.005 元一次 | 前 10 次不问；抓到的页面当场进本地语料 |

---

## 四、本机路径与外部程序

| 项 | 位置 | 说明 |
| --- | --- | --- |
| AutoDock Vina | `VSA_VINA_BIN`，或 `PATH` 里的 `vina` | 不随 exe 打包；缺失时 `dock` 返回安装指引 |
| AutoGrid4 | `VSA_AUTOGRID_BIN`，或 `PATH` 里的 `autogrid4` | 只有 `dock(scoring="ad4")` 用它 |
| DTA 环境 | `models/DTA/.venv`（Python 3.10 + torch 2.2.1 + dgl 2.1.0） | 主仓一行 torch 都不 import，工具起子进程 |
| DTA 权重 | `models/DTA/weights/` | 15 GB，不进版本控制 |
| 嵌入模型 | `~/.vsagent/models/` | 255 MB，显式下载，不进仓也不进 exe |
| 状态目录 | `~/.vsagent/` | `state.toml`、`connection.toml`、`ledgers/`、`logs/`、`grids/`、`surveys/` |
| 项目根 | 源码运行时是仓根，打包后是启动目录 | `core/paths.py:project_root()`；`corpus/` 与 `runs/` 都按它算 |
| 服务网格缓存 | `~/.vsagent/grids/` | 键是受体指纹 + 盒子 + 配体原子类型 |

`models/DTA/` 不能放进 `vsagent/` 里面：`check_chemistry_gate` 用 `rglob("*.py")` 扫整个包，
DTA 里有 45 处顶层 `import torch/rdkit`，那条判据当场失败。

---

## 五、环境变量

`.env` 在仓库根，已 gitignore。`env.py:load_env()` 用 `override=False`，已存在的真实环境变量
永远压过文件里的值。

模型接口不走 `.env`：地址、密钥与模型名由应用内的 `/model` 命令写入 `connection.toml`
（与 `state.toml` 同目录，权限 0600，原子替换）。

| 变量 | 说明 |
| --- | --- |
| `VSA_MAX_STEPS` | 一轮步数上限，写错时按 24 处理 |
| `VSA_WEB_PORT` | 本地网页端口，默认 8860 |
| `VSA_CORPUS_INDEX_WEB` | `0/1`，`search_web` 的摘要是否自动入库（默认开） |
| `VSA_CORPUS_EMBED` | `0/1`，语义通道总开关（默认开） |
| `VSA_EMBED_MODEL` | 嵌入模型名，默认 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 维）；换模型要整表重建 |
| `VSA_EMBED_MIN_SCORE` | 余弦下限，默认 0.30；不设下限时「本地没有」会变成「命中 29 组」 |
| `HF_ENDPOINT` | 模型下载端点，排第一；不设时先试 `huggingface.co`，失败自动退到 `hf-mirror.com` 并打印实际端点 |
| `VSA_VINA_BIN` | 外部 Vina 可执行文件 |
| `VSA_AUTOGRID_BIN` | 外部 `autogrid4` |
| `VSA_DTA_PYTHON` | 跑 `models/DTA/virtual_screen.py` 的解释器；不设时依次试 `models/DTA/.venv/bin/python` 与旧布局的 `.venv-dta/bin/python` |
| `VSA_DTA_ROOT` | `models/DTA`；不设时按代码位置定位 |
| `VSA_DTA_DEVICE` | `cpu`（默认）或 `cuda` |
| `VSA_DTA_WEIGHTS` | 预训练编码器目录；可被 `VSA_ESM_CHECKPOINT` / `VSA_CHEMBERTA_CHECKPOINT` 单独覆盖 |
| `VSA_FILE_READ_LOG` | 设为 `0` / `off` / `false` 关掉文件读取日志（默认写项目根的 `logs/file-reads.jsonl`） |
| `EDITOR` / `VISUAL` | `/editor` 外部编辑器往返 |

测试不读 `.env`：`FakeModelServer` 起在 `127.0.0.1:0`，测试把地址写进 `.probe/connection.toml`，
全程离线。

---

## 六、依赖

| 文件 | 内容 | 缺了会怎样 |
| --- | --- | --- |
| `requirements.txt` | textual / httpx / python-dotenv / tomli-w / pypdf | 应用起不来 |
| `requirements-chem.txt` | meeko / rdkit / gemmi / numpy / scipy + plip | `prepare_receptor` 返回一条带安装指令的错误；`plip` 只影响 `analyze_pose(interactions=true)` |
| `requirements-embed.txt` | fastembed + onnxruntime + tokenizers（约 78 MB） | `search_corpus` 只跑词法两路 |
| `requirements-pack.txt` | pyinstaller 及打包依赖 | 打不出 exe |
| `models/DTA/` 自带环境 | Python 3.10 + torch 2.2.1 + dgl 2.1.0 | `score_affinity` 与 `fit_affinity` 返回安装指引 |

主仓是 Python 3.12。DTA 单独一套环境：`pandas==1.5.3` 没有 cp312 轮子，MDAnalysis 要求
`numpy<2`，合并不了。

---

## 七、常用脚本

| 脚本 | 管什么 | 进 CI |
| --- | --- | --- |
| `tests/test_agent.py` | 内核判据，按组跑（`--only` 秒级） | 是 |
| `tests/test_smoke.py` | 界面加内核超集 | 是 |
| `tools/check_prefix.py` | 前缀预算、点名工具首步可见、枚举残留、字节稳定 | 是 |
| `tools/check_web_api.py` | 网页 API 形状、并发、边写边读、语料读写 | 是 |
| `tools/check_retrieval.py` | 检索三路与组级指标 | 是 |
| `tools/check_demo_data.py` | 口袋残基是否真的接触共晶配体（≤4.5 Å、≥60%） | 是 |
| `debug_tools.py` | 56 个领域工具的真实调用，顺序即依赖顺序 | 否 |
| `tools/eval_context.py` | 上下文装配与多种窗口的扫描 | 否 |
| `tools/eval_memory.py` | 记忆台账健康度、recall@k、stale 比例 | 否 |
| `tools/replay_cost.py` | 按当前装配器重放历史会话，与 trace 对照 | 否 |
| `tools/retro_affinity.py` | 回顾性亲和力实验的 select / build / pocket / run / report | 否 |
| `tests/verify_exe.py` | 打包产物验证（需要 pywinpty，只能在 Windows 跑） | 否 |
