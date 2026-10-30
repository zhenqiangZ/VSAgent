# 外部数据源接入记录（AlphaFold / Europe PMC / PubChem / Open Targets）

本文件记录「给虚拟筛选 Agent 再接一批外部数据源」这件事：**候选清单、每一个的数据源
与工具、落地时踩到的坑、以及明确不做的那些**。它同时是这批工具的清单——想知道
「哪些数据源接进来了、对应哪个工具」，看 §1 那张表就够。

写法沿用 `docs/screening-tools-plan.md`：先说已核对的事实，再说做了什么，实现过程中
与原设想不一致的地方留在原地（§3 各小节的「实现备注」），不另写一份说明。

## 0. 已核对的事实（2026-10-29）

| 项 | 读数 | 位置 |
|---|---|---|
| 领域工具 | 51 个 / 21 族 | `domain/all_tools.py:FAMILIES` |
| 全部工具（含核心 14 个） | 65 个，定义约 15.5K token（51 310 字符） | `app._build_registry` |
| 核心 + 一族的每步成本 | 2 507 + 1 043 ≈ 3.6K token（`structure` 族） | 同上 |
| 网络出口 | 只有 `domain/http.py` 一处，测试整体替换它 | `domain/http.py` |
| 族清单的代价 | `load_tools` 的说明里**每族一行**，常驻在核心工具里 | `toolset.catalog_text()` |
| 检索缓存 | `literature` 与 `opentargets` 走 24h 缓存；`structure` 复用本地坐标（`refresh` 可重下） | `rsearch.cache_path` |
| 本轮新增的族 | `pubchem`、`opentargets`（各 1 个模块 2 个工具） | `all_tools.py:77`、`all_tools.py:82` |

（工具数是 49 而不是 50：`describe_molecule` 在 2026-10-29 并进了 `lookup_compound`，
见 §3.3「合并入口」。体积按请求体里 `tools` 数组的形状量，含 `{"type":"function",...}` 外壳。）

由此得到本轮唯一一条硬约束：**能挂进已有族就不要新立族**。族清单每多一行，就是
每一次请求都多几行 token；而往已有族里加一个工具，成本是零（工具定义只在装进来之后
才发）。四个数据源里只有两个够格单独立族，理由见 §2.1。

## 1. 候选清单与状态

| 数据源 | 能回答什么 | 工具 | 落在哪一族 | 状态 |
|---|---|---|---|---|
| AlphaFold DB | 没有实验结构时，拿预测模型顶上；顺带给出每个残基的可信度 | `fetch_alphafold` | `structure`（已有） | 已接入 |
| Europe PMC | 预印本、开放获取全文正文（PubMed 只有摘要） | `search_europepmc`、`read_fulltext` | `literature`（已有） | 已接入 |
| PubChem | 任意标识 → 化合物身份与计算性质；全库结构相似检索 | `lookup_compound`、`find_similar_compounds` | `pubchem`（新） | 已接入 |
| Open Targets | 靶点的可成药性、化学探针、安全性、临床候选、疾病关联；疾病 → 候选靶点 | `check_druggability`、`disease_targets` | `opentargets`（新） | 已接入 |
| GNINA | 用 CNN 对接到位姿态重打分 | —（计划用 `score_affinity` 覆盖） | — | 不做，见 §4.1 |
| PLIP | 蛋白-配体相互作用指纹（盐桥 / π 堆积 / 疏水 / 卤键 / 水桥 / 金属配位） | `analyze_pose` 的 `interactions=true`（不单开工具） | `analysis`（已有） | 已接入，见 §3.5 |
| ProLIF | 同上（基于 MDAnalysis） | — | — | 不做，见 §4.2 |
| ZINC | 可采购化合物库 | — | — | 不做，见 §4.3 |
| 外部 ADMET 模型 API | 吸收/毒性等端点预测 | —（`predict_admet` 已覆盖规则层） | — | 不做，见 §4.4 |
| 同源建模（SWISS-MODEL / Modeller） | 无实验结构、AlphaFold 也不适用时建模 | — | — | 不做，见 §4.5 |
| 分子动力学（MD） | 结合稳定性、构象变化 | — | — | 不做，见 §4.6 |
| 逆合成（ASKCOS / RetroBioCat） | 「筛出来的分子怎么合成」 | — | — | 不做，见 §4.7 |

## 2. 四条共同口径

### 2.1 族怎么分：按「问的问题」，不按「数据从哪来」

`chembl` 与 `pubchem` 都是化合物库，但它们回答的问题不同：

- `chembl` 认 ChEMBL 编号，回答「这个分子有什么活性」；
- `pubchem` 认名称 / CID / SMILES / InChIKey，回答「这段结构到底是什么、全库有没有近亲」。

对接产物与枚举出来的类似物**只有 SMILES、没有 ChEMBL 编号**，所以 `chembl` 那条路
对它们根本不成立。这是新立 `pubchem` 族的唯一理由。

**2026-10-29 补一句：`lookup_compound` 现在也吃 ChEMBL 编号，也会回 ChEMBL 的画像，
但族不合并。** 两族的分界线是「谁认得你手里的东西」，不是「请求打到哪个域名」：
从 ChEMBL 编号进来的人本来就装了 `chembl` 族，而只有 SMILES 的人只会装 `pubchem`。
合并成族反而会让「我只想查结构」的会话白背一份活性检索的定义。

`opentargets` 同理：`uniprot` 回答「这是什么蛋白」，`chembl` 回答「有什么已知活性」，
它回答的是更靠前的「这个靶点值不值得做、做下去会撞上什么」。三族不合并。

反过来，AlphaFold 挂进 `structure`（查条目 → 取坐标 → 导进登记表本来就是同一个动作的
三步），Europe PMC 挂进 `literature`（两个源都是「找文献」）。这两个没有新立族。

### 2.2 错误分档：4xx 是结论，5xx 是抖动，但**有一个例外**

`http.py` 的默认口径：4xx → `ToolError`（确定性失败）、429/5xx/连不上 →
`RetryableToolError`（内核重试一次）。这四个数据源里有**两处把「输入写错了」也回成
HTTP 500**：

| 接口 | 现象 | 处理 |
|---|---|---|
| ChEMBL 相似性（既有） | 结构串解析不了 → 500 | `server_5xx_as_error` 开关 |
| PubChem PUG（本轮） | 结构串解析不了 → 500，正文带 `PUGREST.ServerError` | 看正文再决定：命中标记才翻成参数错 |

PubChem 不能用 `server_5xx_as_error`：它忙的时候回的是 `PUGREST.ServerBusy`，那是真抖动。
一律按永久失败处理，模型会被告知「你的结构写错了」——而它写得没错。

### 2.3 输出预算：每条清单都要有上限，且写明还剩多少

四个源里三个会返回「几千条」：PubChem 相似检索（阿司匹林在 90% 阈值下有 6966 条）、
Open Targets 疾病关联（EGFR 有 5860 条）、临床候选（EGFR 有 82 条、其中一半是别人的盐）。
统一做法是**截断 + 一句「还有 N 条、怎么取到」**，而不是把整段塞进上下文。

### 2.4 数字口径：能算的在代码里算，能说清的写清楚

- pLDDT 由坐标的 B-factor 列现算，不抄接口的 `globalMetricValue`（两者对得上：
  75.9 vs 75.94）。
- 相似度分数：PubChem 的相似性接口**不给分数**，所以不编一个 0–1 的数——编了就会被
  拿去和本地 Tanimoto 比，而那是两套指纹。
- Open Targets 的关联分：0–1 的证据加权排序分，**不是概率，跨靶点不可比**。
  输出里必须写这句，否则模型会拿 0.85 和另一个靶点的 0.72 比「谁更相关」。

## 3. 逐个记录

### 3.1 AlphaFold DB（`fetch_alphafold`，`structure` 族）

**接口**：`GET https://alphafold.ebi.ac.uk/api/prediction/{uniprot}` → 条目数组，
每条带 `pdbUrl` / `cifUrl` / `latestVersion` / `sequenceStart` / `sequenceEnd`。
坐标走 `pdbUrl`，没有 `.pdb` 时用 `cifUrl` + gemmi 转。

**为什么挂进 `structure`**：用户要的是「这个靶点有没有结构」，实验结构和预测结构是同一个
问题的两个答案；分开成族会让模型先装一个再装另一个。

**字段与坑**：

| 坑 | 现象 | 处理 |
|---|---|---|
| 一个 UniProt 编号对应多个条目 | 同工型、片段各一条，顺序不保证 | **精确匹配 `AF-<acc>-F1` 优先**；没有精确条目时打印一句警告，不悄悄取第一条 |
| pLDDT 不在 JSON 里 | 只有坐标的 B-factor 列 | 按 CA 原子逐残基取 B-factor 现算均值与分档 |
| 分档阈值 | <50 很低 / 50–70 低 / 70–90 可信 / ≥90 很高 | **70 是唯一有下游含义的线**：低于它的片段不该拿去做对接 |
| 低可信片段 | 一条链里可能有好几段 | 只报 ≥10 残基的片段，最多 3 段 |

**输出**：编号与版本、覆盖范围（`sequenceStart–End` 相对全长）、pLDDT 均值与四档占比、
低可信片段、坐标来源（`.pdb` 还是 mmCIF 转换）。转换过的文件头会写一行 `REMARK 999`，
`assess_structure` 靠这个前缀认出来源格式。

**实现备注**：pLDDT 分档用 CA 原子，而不是所有原子——侧链原子在低可信区会被摊平，
按全部原子算出来的均值会比接口的全局分略高。

### 3.2 Europe PMC（`search_europepmc` / `read_fulltext`，`literature` 族）

**接口**：`GET https://www.ebi.ac.uk/europepmc/webservices/rest/search`（检索）、
`.../{pmcid}/fullTextXML`（全文）。

**为什么挂进 `literature`**：PubMed 只有摘要，且不收录预印本；这两件事是同一个
「找文献」需求的两个缺口，不是两个族。

**字段与坑**：

| 坑 | 现象 | 处理 |
|---|---|---|
| 全文接口按 `Accept` 选格式 | 默认发 `application/json` → **HTTP 406** | 显式 `Accept: application/xml`；406 与「有没有全文」无关 |
| 非开放获取没有 404 | 回 **HTTP 500**，与真的故障同码 | 先查 `isOpenAccess`，为 `N` 时**在下载之前**就拒绝，并说明原因 |
| JATS XML 带命名空间 | `find("body")` 找不到 | 先剥命名空间再解析 |
| 预印本没有 PMCID | `PPR1350376` 这种编号 | 单独认 `PPR` 前缀，回「没有 PMC 全文编号」，而不是「编号认不出来」 |
| 内联标签会切断单词 | `second-<i>line</i>` → `second- line` | 块级标签换空格、内联标签直接删（与 `structure._join_wrapped` 同一口径） |

**输出**：检索给标题、期刊、年份、被引次数、DOI/PMID/PMCID、摘要片段（400 字）；
全文按章节给（`results` / `methods` / `discussion` …），默认 4000 字、上限 12000 字，
截断时说明还有多少。

**实现备注**：`search_literature` 的说明里加了一句「要预印本或要读开放全文的正文，
换 `search_europepmc` / `read_fulltext`」——不写这句，模型会一直在 PubMed 那条路上
找不到全文。

### 3.3 PubChem（`lookup_compound` / `find_similar_compounds`，`pubchem` 族）

**接口**：PUG REST，`https://pubchem.ncbi.nlm.nih.gov/rest/pug`。

**字段与坑**：

| 坑 | 现象 | 处理 |
|---|---|---|
| 属性键名反直觉 | 带立体的规范 SMILES 在 `SMILES` 键下，不带的在 `ConnectivitySMILES` 下 | 按返回的键取，别按直觉写 `CanonicalSMILES` |
| 相似检索有两条路 | `/similarity/` 异步（202 + `ListKey` + 轮询），且不接受 `/cids/` 后缀 | 用同步的 `fastsimilarity_2d`；实测条数与顺序与异步路完全一致 |
| 相似度不给分数 | 只回 CID 列表 | 不编分数，只写「按接口顺序」 |
| 结构错误回 500 | 见 §2.2 | 看正文里的 `PUGREST.ServerError` / `Search status indicates failure` |
| 同名多义 | 一个名字对多个 CID，结构可能完全不同 | 全部列出来，提示按 CID 确认 |
| 同义词接口不分页 | 阿司匹林一次回 23 KB、上千个名字 | 只取前 8 个，且只为前 3 个 CID 取 |

**标识收敛**：CID（可带 `CID:` 前缀）→ InChIKey → 看起来像 SMILES 的串 → 名称。
`CCO`（乙醇）一个提示字符都没有，所以名称查不到时**再拿它当 SMILES 试一次**；
顺序不能反过来，否则一个叫 `CCO` 的登记名会被当成结构去解析。

**输出**：身份行（分子式 / MW / 电荷）、规范 SMILES、连接式（与规范不同时才印）、
InChIKey / IUPAC、**单独一行**标明的计算性质（XLogP / TPSA / 氢键 / 可旋转键）、
别名、条目 URL。相似检索给总数与前几条，并注明「不含任何判断」。

**实现备注**：相似检索走两步（CID 列表 → 一次批量属性请求），N 个化合物只多一次往返。

**合并入口（2026-10-29）**：`lookup_compound` 现在也吃 ChEMBL 编号，并在 ChEMBL 有记录时
把画像接在 PubChem 那半后面。原先单立的 `describe_molecule` 因此删掉——模型要先判断
「手里是编号还是结构」再挑工具，判错就是一次白跑。

| 进来的标识 | 先问谁 | 再接谁 |
|---|---|---|
| 名称 / CID / SMILES / InChIKey | PubChem | 唯一命中时按 InChIKey 查 ChEMBL |
| ChEMBL 编号 | ChEMBL（画像） | 按画像里的 InChIKey 反查 PubChem 身份 |

三条边界：

- **桥只有 InChIKey 一条。** PubChem 不给 ChEMBL 编号（同义词里偶尔混进来一个，不可
  依赖）；ChEMBL 的 `molecule_structures__standard_inchi_key__iexact` 实测精确命中
  （阿司匹林 `BSYNRYMUTXBXSQ-UHFFFAOYSA-N` → 且只 → `CHEMBL25`）。用 InChIKey 而不是
  SMILES 做键，是因为同一结构在两个库里规范化出来的写法未必逐字相同，而 InChIKey 是哈希。
- **同名多义不补。** 一个名字对多个 CID 时不知道该补哪一个，输出里指路「拿 CID 再查一次」。
- **ChEMBL 那半失败分两档。** 确定性错误（4xx）降级成一句说明，PubChem 那半照旧给；
  429/5xx 照旧抛给内核重试——吞掉它会变成「这个分子不在 ChEMBL 里」，那是误读。
  ChEMBL 里查不到（0 条）**不算失败**，但要写明「没收录不等于没有活性」。

代价是慢了一点：一次调用 2–6 秒。原来 `lookup_compound` 约 1 秒、`describe_molecule`
2–4 秒，现在合成一次——省掉的是模型那一次判断和一次往返，不是网络请求（四个请求都在，
只是串在同一个工具里）。

### 3.4 Open Targets（`check_druggability` / `disease_targets`，`opentargets` 族）

**接口**：`POST https://api.platform.opentargets.org/api/v4/graphql`，查询写在请求体里。

**为什么是新族**：见 §2.1。另外它**不产生产物、不参与后续计算**，只是判断依据，
所以也不跟 `structure` 混。

**字段与坑**：

| 坑 | 现象 | 处理 |
|---|---|---|
| 两层错误 | HTTP 4xx 是协议层；HTTP **200 也可能带 `errors`**（字段写错） | 两层都接：4xx 走 `http` 的口径，`errors` 单独抛 |
| 基因名塞进 `ensemblId` | 回 `{"data": {"target": null}}`，**不报错** | 先解析再查；解析结果打印出来供复用 |
| 临床候选接口不分页 | `drugAndClinicalCandidates` 没有 `page` 参数，EGFR 一次回 82 条 | 全部读进来，本地按最高临床阶段排序再截断 |
| 盐型各算一条 | `AFATINIB` 与 `AFATINIB DIMALEATE` 是两条 | 按 `parentMolecule` 归并到母体，保留阶段高的那条 |
| 安全性可能很长 | EGFR 有 21 条 | 列 6 条 + 「还有 N 条」 |
| 可成药性 25 项 | 三个模态各七八个标志位 | **只列成立的**；一个都没成立的模态整行不出现 |
| 探针没有名字字段 | `id` 才是探针名（`AFATINIB`），`drugId` 是 ChEMBL 编号、可能为 null | 按 `id` 渲染，`drugId` 单独列出供 `lookup_compound` 接着查 |
| 质量标记不能一视同仁 | 名录里的条目未必都经过严格的选择性验证 | 只有 `isHighQuality` 排在最前并标出来，其余排在后面 |
| 三个分数不同量纲 | `probesDrugsScore`（0–100）与细胞/整体两栏并存 | 只把 `probesDrugsScore` 当排序键，渲染时原样报数、不当结论 |
| 关联分被当成概率 | 0.85 看着像「85% 会致病」 | 输出里写明：排序分，不是概率，跨靶点不可比 |

**两个工具的分工**：`check_druggability` 从靶点出发（可成药性 / 化学探针 / 安全性 /
临床候选 / 疾病关联），`disease_targets` 从疾病出发（关联靶点 + 证据分解）。方向相反，
所以不能合成一个——合成之后「我要从病找靶点」会变成「先给我一个靶点」。

**输出**：身份与位置、功能首段（剥掉 `PubMed:` 与 `{ECO:...}` 再截 320 字）、
按模态分组的可成药性、化学探针（高质量优先，列 5 条 + 还剩几个）、安全性、
按阶段排序的临床候选、按分数排序的疾病关联
（附证据分解：临床 / 遗传 / 通路 / 文献 / 体细胞突变）。

**实现备注**：疾病关联的 `datatypeScores` 比总分有用——0.8 分里如果全是文献，
那是「研究得多」；有临床与遗传证据才说明「在人身上验证过」。所以渲染时给前四路
并写明还有几路，而不是只印一个总分。

**化学探针（2026-10-29 补）**：`chemicalProbes` 是这个靶点上效力够高、选择性经过验证的
少数几个分子，用途是**阳性对照**——先把其中一个按已知结合模式对接一遍，复现不出来就说明
受体准备、口袋或参数有问题，后面整批结果都不必看。它和「有已知活性」不是一回事：ChEMBL
里几千条 IC50 也不代表有探针（实测 EGFR 12 条、BRAF 2 条、KRAS 7 条、TP53 0 条）。
排序是「高质量优先 → `probesDrugsScore` 从高到低」，列 5 条 + 还剩几个；没有探针时
不说「无数据」，而是说清后果（对接结果没有基准可校）。`drugId` 就是 ChEMBL 编号，
要看它是不是已上市药，直接交给 `lookup_compound`。

**缓存**：应答按「查询 + 变量」缓存一天，落在 `~/.vsagent/search/opentargets-*.json`
（与检索共用目录，文件名带来源前缀所以不会互相顶掉）。三条边界：

- **键必须带变量。** 只按编号做键的话，「先看 5 条疾病、再看 20 条」会拿到同一份 5 条的。
  实测两条查询各落一份文件。
- **失败的应答不落缓存。** 4xx / 5xx / HTTP 200 带 `errors` 都在写缓存之前就抛了；
  缓存是「接口当时这么答的」这份档案，失败的应答不是档案。
- **缓存损坏当没有。** `read_cache` 解不出 JSON 就返回 `None`，于是重新联网——
  坏掉的缓存不能变成一次假结果（有一条断言专门把文件写坏再查）。

工具层**不暴露 `refresh`**：数据按月更新、缓存只有一天，`fetch_alphafold` 那种
「换版本」的诉求在这里不存在；要强制重查可以在库层传 `use_cache=False`。

### 3.5 PLIP 交互指纹（`analyze_pose` 的 `interactions=true`，`analysis` 族）

**不单开工具，做成已有工具的一个开关。** 同一个位姿有两套互不相干的解读最难用：
接触残基、氢键、冲突由本地几何算（4 Å 重原子、3.5 Å / 120°），盐桥、π 堆积、疏水、
卤键、水桥、金属配位由 PLIP 算（它先认键级与质子化，阈值是 PLIP 的默认值）。分成两个
工具，模型得自己对齐两份结果，而且很容易只调一个。合在一次调用里，口径差异写在输出
里（"两套口径，数量对不上属正常"）。

默认 **关**：多 0.3–1 秒（实测 2 512 原子的受体 0.37 s），而多数时候「碰到谁 + 有没有
冲突」已经够回答。描述里写清了什么时候该打开——「凭什么强」而不是「碰到了谁」。

**合并成一份 PDB 有两个坑，都会让结果安静地错**：

- **`END` 必须丢。** 受体文件末尾就有 `END`，直接拼上配体，openbabel 读到那一行就停，
  配体一个原子都没进去，PLIP 报「没有配体」。文件看起来是完整的，错在中间那一行。
  `MASTER` 同理。
- **`CONECT` 也必须丢。** 两份文件的原子序号各自从 1 开始，拼在一起后受体的 CONECT
  会指到配体原子上。丢掉之后由 openbabel 按距离认键——实测苯环照样被认成芳香环、
  羧基照样被认成负电中心。
- **位姿的残基名要换成受体里没用过的**（`LIG` / `UNL` / …）：受体里可能还留着共晶
  配体，同名时 PLIP 会把两个都当配体，挑错一个就是拿晶体里的相互作用去解释对接位姿。

**依赖位置**：`plip` 进 `requirements-chem.txt`，但**不进 `chemistry.CHEM_DEPS`**。
后者管的是「准备受体、准备配体」那条链，把 plip 加进去会让没装它的人连
`prepare_receptor` 都调不了——两件事没有关系。测试里有一条断言专门挡住「看见两份
清单不一致就顺手对齐」。

**降级路径（2026-10-29 逐条验过）**。任何一种情形都**不置 `failed`**：接触、氢键、
冲突照给，PLIP 那一段换成一句原因。

| 情形 | 说法 |
|---|---|
| 没装 plip | 「本机没装 PLIP。装它：`requirements-chem.txt`……」 |
| plip 在、缺 `openbabel` | 「PLIP 用不了：它的依赖 openbabel 没装上（plip 本身在）……」 |
| 导入就炸（`.so` 坏了） | 「PLIP 装上了但导入失败（ImportError: …）」 |
| 构造 / 读文件 / `analyze()` 炸 | 「PLIP 算不了这份复合物（RuntimeError: …）」 |
| 没把位姿认成配体 | 「PLIP 没把位姿认成配体（合并后的复合物里找不到它）」 |
| 字段名对不上（版本漂移） | 「PLIP 的接口与这个版本对不上（缺字段 hbonds_pdon…）」 |

四条边界：

- **报的是 `exc.name`，不是 `plip`**。装了 plip 却缺 openbabel 时，说「本机没装
  PLIP」会让用户去重装已经装好的东西——与 `chemistry._probe_deps` 同一个理由。
- **`loader()` 与写临时文件都在 `try` 里**。第一版只包了 `load_pdb` / `analyze`，
  而 openbabel 的初始化在 `PDBComplex()` 的构造里——那里炸掉会穿出去，整次
  `analyze_pose` 失败，可接触与氢键早就算完了。
- **第一行写「PLIP 没算成（原因见末尾）」**。下面没有「盐桥」那几行，读起来像
  「没有盐桥」，而真实原因是没算——这两种结论对决策的意义相反。
- **算没成时不写 `meta.interactions`，只写 `meta.interactions_error`**。空字典与
  「算了但一类都没有」长得一模一样。

**属性名是照 `PLInteraction.__init__` 抄的，不是猜的**：`hbonds` 不存在（是
`hbonds_ldon` / `hbonds_pdon`），`saltbridge` 也不存在（是 `saltbridge_lneg` /
`saltbridge_pneg`）。用 `getattr(site, "hbonds", ())` 不会报错，只会永远返回空——
指纹安静地少一整类，而输出毫无异样。

## 4. 明确不做的（附理由）

### 4.1 GNINA

要 GPU、要编译，与「引擎不随 exe 打包」的约定冲突（`dock` 对 vina 也是这么处理的）。
更关键的是它解决的那一环——重打分——`score_affinity`（VCrossFormer）已经覆盖，
再接一个重打分器只会让「用哪个分数」变成一个新问题。

### 4.2 ProLIF

**PLIP 已做，见 §3.5。ProLIF 不做。** 它基于 MDAnalysis + RDKit，而 MDAnalysis 要求
`numpy<2`，主环境是 2.5.4——为了一个指纹把整个环境的 numpy 降级不值。PLIP 只多一个
openbabel，装得干净（`plip` + `openbabel` + `lxml` 三个轮子，与 numpy 不冲突）。

也没有照搬 PLIP 的命令行用法（`plip -i file -o txt`）：那要起子进程、解析它自己的报表
格式，而库 API 在 0.3 秒内就返回结构化记录。

### 4.3 ZINC

可采购库是几十 GB 的量级，而本地已有两条不看靶点的通道（`screen_similarity` /
`screen_shape`）。采购清单更适合离线准备成 SDF 再 `import_ligandset`，不必进会话。

### 4.4 外部 ADMET 模型 API

`predict_admet` 已覆盖规则层（ESOL / Veber / BBB / CYP 警示）。外部模型 API 要么收费、
要么稳定性不保证，而规则层的结论本来就是「先粗筛」。

### 4.5 同源建模

与 AlphaFold 重叠，且一次建模以小时计。AlphaFold 覆盖不到的（例如突变体、复合物界面），
`import_structure` 允许用户自己把模型导进来——这条路已经通了。

### 4.6 分子动力学

算力与时间都不在一个量级。会话式工具的前提是「一次调用几十秒内回来」，MD 不是。

### 4.7 逆合成

属于「筛完之后怎么合成」，不在当前漏斗（靶点 → 结构 → 口袋 → 对接 → 排序）里。
真要做，它应该是 `export` 之后的一个独立环节。

## 5. 判据与调试

| 命令 | 验什么 |
|---|---|
| `python tests/test_agent.py --only alphafold` | 条目选择、pLDDT 分档、缓存与 `refresh`、mmCIF 转换 |
| `python tests/test_agent.py --only europepmc` | 检索排序、非 OA 拒在下载之前、JATS 解析与截断 |
| `python tests/test_agent.py --only pubchem` | 命名空间回退、两种 500 的区分、批量属性、limit 夹取、合并入口的两个方向 |
| `python tests/test_agent.py --only opentargets` | 名字→编号、两层错误、模态分组、化学探针排序与截断、盐型归并、排序与截断 |
| `python tests/test_agent.py --only analyze-pose` | 接触/氢键/冲突、PLIP 指纹的合并与抽取、开关与缺席时的降级 |
| `python debug_tools.py` | 全跑 22 个模块 52 次调用（49 个工具），**真联网** |
| `python debug_tools.py opentargets check_druggability target=EGFR` | 单工具单参数 |

四个检查组共 29 + 35 + 62 + 78 = 204 条断言，全部打桩、不联网；`debug_tools.py`
才是真打接口的那一层。`--only chembl` 另有 91 条，其中「画像」那一段现在是按
`chembl.describe_molecule()` 这个库函数验的（工具入口在 `pubchem` 族）。

## 6. 待确认

没有了。原先两条都在 2026-10-29 做掉了：

1. ~~`check_druggability` 要不要带化学探针~~ → 带了，见 §3.4「化学探针」。
2. ~~PubChem 与 ChEMBL 要不要合并成一个入口~~ → 合并了「身份查询」这一半
   （`lookup_compound`），相似检索保持两个：ChEMBL 的阈值是 40–100 的整数百分比、
   超界直接拒，PubChem 的根本不给分数，两套语义塞进一个参数只会变成「这个数是谁的」。
   见 §3.3「合并入口」。
