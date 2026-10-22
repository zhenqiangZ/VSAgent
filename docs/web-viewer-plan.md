# 结构上网页（导出 → 常驻服务 → 打开页面）

本文件是「Agent 拿到蛋白与配体之后，把结构导成一个运行目录、由常驻的本地服务呈现到网页上」
这一轮的实施方案与执行清单。动笔前先把**已核对过的事实**列清楚。

> **本文件是详细稿**：记着为什么这么定、否决过什么、实测数据与踩坑记录。
> 只想看「要做什么」的骨架逻辑，读 `docs/PLAN-brief.md`（约 80 行）。
>
> **八步已全部完成**（2026-10-08）。进度记在 §0.5「执行进度」。

## 0. 已核对的事实

### 0.1 已经有的

| 项 | 现状 | 位置 |
|---|---|---|
| 网页前端 | **有，且完整**。三个页面视图（口袋浏览、候选分子叠合、单分子详情），Mol\* 已打包 | `web/`、`runs/1iep-molstar/` |
| 只读服务 | **有**。四个接口 + 静态文件，标准库 `http.server`，只绑 `127.0.0.1` | `tools/serve_web.py`（349 行） |
| 数据形状 | **有**。`meta.json` 的 target / structure / pockets / molecules / contacts / provenance | `runs/1iep-molstar/meta.json` |
| 前端约定 | 前端**只读** `meta.json` + 结构文件；字段缺失时降级（如缺 `provenance.session_id` 就不显示跳转入口） | `docs/web-frontend-architecture.md` |
| 会话归属 | `meta.json` 的 `provenance.session_id`，服务据此反查 | 同上 |
| 登记表 | handle / 血缘 / 指纹 / 对账 / `Entry.meta` | `opencode_tui/core/agent/ledger.py` |
| 零依赖坐标解析 | `pdbtext.py`：`AtomRecord.coord`、`HeteroGroup.center` / `coords`、`hetero_groups()`、`ATOM_*` 列位 | `opencode_tui/core/agent/domain/pdbtext.py` |
| 杂原子组筛选的一半判据 | `structure.py` 已收 `MODRES` 登记名（`modified_names`）与 `ligands` 口径 | `opencode_tui/core/agent/domain/structure.py:339,383` |
| UniProt 位点注释 | **已取回但本轮不当坐标用**：`_domains()` 取 `Domain`、`_binding_ligands()` 取 `Binding site` 的配体名；给的是 UniProt 编号，**不能直接当残基编号**（见 2.3.3 的说明） | `opencode_tui/core/agent/domain/uniprot.py:363,379` |
| **复合物结构检索** | **可用，本轮的主键**：RCSB 属性搜按 UniProt 列出该蛋白**所有**带配体的已发布结构（ABL1 实测 69 个），GraphQL 批量取配体，`files.rcsb.org/download/` 下载坐标。**这是 P0 主来源的第一半**（见 2.3.3） | `https://search.rcsb.org/rcsbsearch/v2/query` |
| **口袋预测结果** | **可网络取回**：prankweb.cz 的 REST API v2 按 PDB 条目给出 p2rank 口袋（残基 + 打分 + 置信度 + 三维中心）。**本轮作为 P1 副选**：只在「该蛋白一个复合物都没有」时才用（见 2.3.4 / 2.3.9） | `https://prankweb.cz/api/v2/...` |
| **检索类接口** | **可用，但项目里还没接**：RCSB 文本/属性搜 + GraphQL、PubMed E-utilities、Europe PMC。HTTP 层现成（`http.py` 走 `httpx`），**本轮做成给模型用的搜索工具**（见 2.3.11） | `opencode_tui/core/agent/domain/http.py` |
| 化学依赖守门人 | `chemistry.require_chem()`（本机**未装**化学依赖） | `opencode_tui/core/agent/domain/chemistry.py` |
| 领域工具装配 | `domain/all_tools.py`：**现 11 个**，`app.py` 一次装完；本轮加到 **15 个**（+`write_run`/`open_viewer`/`search_pdb`/`search_literature`） | 第 7 步 |

### 0.2 缺的三段（本轮要补）

| 缺什么 | 后果 |
|---|---|
| **导出**：handle → `runs/<run_id>/` + `meta.json` | `runs/` 现在只有 `build_demo_data.py` 离线合成的那两份，登记表里的东西进不去 |
| **服务归属**：服务跑在哪个进程里 | `tools/serve_web.py` 是独立 CLI，要人工手敲；且它**不在打包产物里** |
| **动作**：agent 主动打开页面 | 现在没有任何工具能返回 URL，也没有界面提示 |
| **口袋从坐标算** | 项目里**没有**这个能力：演示数据里 4 个口袋只有 1 个是真的。见 2.3 |

### 0.3 打包产物里有什么（关键约束）

`opencode_tui.spec` 的 `datas` **只有一项**：`opencode_tui/styles/app.tcss`。

```
datas = [(str(root / "opencode_tui" / "styles" / "app.tcss"), "opencode_tui/styles")]
```

`tools/`、`web/`、`viz/`、`runs/` **都没有进包**。所以：

- **「agent 起 `tools/serve_web.py` 子进程」这条路在冻结成 exe 之后不存在**——那个脚本
  不在磁盘上。这是选定「包内常驻服务」的直接原因。
- 服务要进包，路由那一段逻辑与 `web/` 静态资产**都得进 `datas`**。
  `web/vendor/molstar-lib.js` 有 3 MB，进包会让产物明显变大，这是要认下来的代价。

## 1. 三个已定的决策

| 决策 | 选定 | 理由 |
|---|---|---|
| 服务由谁提供 | **包内常驻服务**（后台线程，随应用启停） | Agent 不该管进程生命周期。服务是环境的一部分，像会话、像工作目录；让模型负责启动/复用/关闭一个常驻服务，它会在这三件事上各错一次 |
| 服务何时起 | **随应用启动就起** | 模型任何时候要开页面都是即时的，没有「已起未起」的中间状态要管 |
| 端口被占 | **默认端口 + 自动避让** | 上次没退干净是最常见的失败，报错退出会让用户看不出为什么 |

## 2. 数值来源：这一页数据可不可信

`meta.json` 里有一类字段不是路径而是**数**。它们从哪来，决定了页面上的数可不可信。

### 2.1 三类数，三个来源

| 字段 | 谁出 | 为什么 |
|---|---|---|
| `structure` / `reference_ligand` / `molecules[].pose`（路径） | 从 handle 取 | 就是登记表里那个路径 |
| `target.pdb_id` / `chains` / `name` | 从 `Entry.meta` 取 | 上游工具（`fetch_structure`）登记时已经写进去了 |
| `pockets[].residues[]`（残基集合） | **P0 从复合物配体圈出；P1（副选）从 prankweb 预测取** | 见 2.3。来源都写在 `source` 里 |
| `pockets[].center` / `radius` | **P0 由配体聚类算；P1 的 `center` 由 prankweb 直接给** | 见 2.3.5。**配方不同，都要标来源** |
| `pockets[].residues[].min_distance` | **`write_run` 现算，或问 RCSB 的 `rcsb_ligand_neighbors`** | 纯几何，两两求距离；`build_demo_data.py:95` 有同一份算法。两条路实测对得上 |
| `pockets[].score` | **只有 P1 有**（prankweb 的 `score` 与 `probability`） | P0 是实验定位，本来就没有「打分」这回事——**不写 0** |
| `pockets[].complexes[]`（**P0 主结果的来源表**） | **从下载到的复合物结构里读出** | 见 2.3.5 / 2.3.10。每个口袋列出「跨哪几个复合物、各自哪个配体（`chem_comp.id`）、分辨率多少」；**不靠文献人工整理** |
| `molecules[].score` | **本轮不出** | 没接对接程序，就没有真实打分 |
| `contacts[]` | **本轮不出** | 没有真实位姿，接触距离不可信 |

### 2.2 为什么不能「让模型把数传进来」

工具设计第 7 条：**RMSD、Tanimoto、接触距离、配体效率这类绝不交给模型心算——它算不对，
而且算错时看上去很像对的。**

假设 `write_run` 收 `pockets=[{"center":[15.6,53.3,15.4], ...}]`，模型就得自己产出
`min_distance: 2.67`。它会照着见过的例子编一个像样的数。结果是页面上几十个距离值
**没有一个是算出来的，而它们看上去都很合理**——不报错、不崩溃，只是整页数据都是假的。
这是最危险的一类错误。

所以：**上游算过的（p2rank 的残基集合与打分）从接口取；实验测到的（复合物里配体的位置）从坐标算；
纯几何派生量（残基最短距离）由 `write_run` 现算；没有来源的（对接打分、接触）本轮不写。**
四类都不经过模型。

**P0 的实验配体也在「从坐标算」这一类里**，只是坐标不是手上这一份受体，而是搜到的复合物。

### 2.3 口袋从哪来（本轮的新内容）

#### 2.3.1 先认清现状：项目里没有「口袋检测」

| 文件 | 事实 |
|---|---|
| `tools/build_demo_data.py` | P1 是真的（某个复合物里配体 STI 位置圈 5 Å 内的残基）；**P2/P3/P4 是 `cluster_centers()` 随机挑 CA 原子造出来的**（`distractor_labels` 写着「变构位点候选/表面浅槽/二聚界面附近」） |
| `runs/1iep-demo/meta.json` | 4 个口袋，只有 P1 `is_reference: true`；`provenance.pockets = "合成的演示数据"` |

所以本轮要做的是**从零建立「口袋从坐标里算出来」这条链路**，不是把已有能力接上。

#### 2.3.2 「只挑这一个受体的配体」这条路的硬伤（实测确认）

第一版方案定的是「拿手上这个受体结构里的配体聚类」。它有一个**不能靠工程手段绕开**的缺陷：

> **有没有口袋，与手上这一份结构里有没有配体，是两件事。**

实测证据（2026-10-07 现查）：

| 检查项 | 结果 |
|---|---|
| RCSB `1lyz`（溶菌酶 apo 结构） | `nonpolymer_entity_count = 0`，`rcsb_ligand_neighbors` **0 条** → 只盯这一份结构**一个口袋都给不出**（**但该蛋白别的 PDB 条目里有配体**——这正是 P0 要搜出来的） |
| 同一个蛋白的 p2rank 预测 | **5 个口袋**（`pocket1` 覆盖 198 个 PDB 条目） |
| RCSB `3eg3`（ABL1，看起来「有配体」） | 只有 `GOL`（甘油，4 条邻居，最短 3.18 Å）——**冷冻保护剂，不是配体** |

三条结论：

1. **apo 结构是常态不是例外**。实验结构里空载形式占多数，手上这一份「没有配体」是**常事**。
2. **就算有杂原子，也可能不是配体**。`3eg3` 的甘油就是例子——上一版靠 `MONATOMIC_IONS` / `BUFFER_LIKE` 名单筛掉它，运气好时能筛对，但这是**逐名字打补丁**，名单永远收不全。
3. **用户要的是「看到口袋」，我们给的却是「看到这一个结构里的配体」**。这两件事会分岔，且分岔时我们给的是空白。

**所以第一版方案被推翻。** 改法不是把「配体聚类」丢掉，而是**把配体从哪儿来这一层换掉**：
不再只看手上这一份结构，而是**去搜这个蛋白的所有复合物**（2.3.3 的 P0）。

#### 2.3.3 定案：**实验复合物优先，预测兜底**

**优先级是本节的核心**（2026-10-07 按用户意见调整，与之相反的顺序作废）：

> **先找这个蛋白的复合物结构。有复合物 → 从复合物里取真口袋（主来源）。
> 一个复合物都没有 → 才用预测口袋兜底（副选）。**

| 层 | 来源 | 给什么 | 覆盖 | 优先级 |
|---|---|---|---|---|
| **P0 主来源** | **复合物结构库**：搜到该蛋白**所有**带配体的已发布结构 → 下载 → 每个配体位置圈出真口袋 | 实测配体坐标 → 真口袋残基 + 距离 + 该结构的分辨率/方法 | 该蛋白只要有**任一**带配体的复合物 | **首选** |
| **P1 副选** | **prankweb.cz 的 p2rank 预测** | 预测残基集合 + 打分 + 置信度 + 三维中心 | 该 **PDB 条目**被 prankweb 算过即可，**与有无配体无关** | **仅当 P0 为空时用** |
| **P2 补充** | RCSB `rcsb_ligand_neighbors` | 残基—配体原子对的**现成距离** | 有配体的结构 | 作 P0 的**距离来源** |

**为什么反转**：预测口袋是「算出来的猜测」，复合物口袋是「测出来的事实」。
有事实时不该拿猜测当主结果——用户点一个口袋，最想看到的是「这个口袋被人做过哪些复合物、
配体长什么样」，预测给不了这个。**预测只在没有任何事实可用时才顶上。**

**这条链的可行性（全部实测，2026-10-07）：**

| 环节 | 实测 |
|---|---|
| 搜「该蛋白所有带配体的结构」 | RCSB 属性搜一条查询搞定：UniProt + `nonpolymer_entity_count > 0`。ABL1（`P00519`）→ **69 个** |
| 批量拿配体代号+链+分辨率 | 一次 GraphQL 问多个条目即可（`nonpolymer_entities` → `chem_comp.id` + `nonpolymer_entity_instances` 的 `auth_asym_id` + `rcsb_entry_info`） |
| 下载体积 | `https://files.rcsb.org/download/<ID>.pdb`，**平均 0.24 MB**（实测 12 个条目合计 2.9 MB） |
| 拿到坐标后 | 复用第 1 步的几何算法：配体原子 → 圈半径内残基 → 口袋 |

**成本要设上限**：ABL1 有 69 个复合物，Nav1.7 有多几十个。**不逐个下载**——
按 2.3.6 的规则排序后**取前 N 个**（暂定 N = 12，可配），并把「总共几个、取了几个」写进返回。

三层都在 `pockets[].source` / `provenance` 里标明，前端据此显示不同徽标。
`"ligand"`（实验）/ `"p2rank"`（预测）这个区分**现在也决定了它是主结果还是副选**。

**UniProt 位点注释（`Binding site` / `Domain`）在本轮只作「来源标注」，不进 `residues`。**
原因：它给的是 **UniProt 编号**（`P00519` 从 1 数到 1130），与页面要的 **PDB 作者编号**
（`1iep` 只从 225 数到 547）**不是一套坐标系**。要用它就得另做序列比对换算——
这正是 2.3.4 里被推翻的那条 PDBe 路线要付的代价。prankweb 直接给 PDB 作者编号，**没有这一步**。

`uniprot_site` 这个 `source` 值**保留**（2.3.8），留给将来真要做换算时的位置。

#### 2.3.4 P1（副选）：prankweb.cz 的 p2rank 预测

**先记一句被推翻的版本**：本节原写的是「走 PDBe-KB 的注释接口按 UniProt 编号取 p2rank 结果」。那条路实测有**三个绕不开的坑**（28 MB 响应、部分编号稳定 502、编号体系要换算），而下面这条把它们全绕开了。**所以换成 prankweb.cz。**

**接口（全部实测）：**

```
GET https://prankweb.cz/api/v2/prediction/v3-conservation-hmm/<PDBID>              # 任务状态
GET https://prankweb.cz/api/v2/prediction/v3-conservation-hmm/<PDBID>/public/prediction.json   # 口袋数据
GET https://prankweb.cz/api/v2/prediction/v3-conservation-hmm/<PDBID>/public/structure.cif     # 结构文件
POST https://prankweb.cz/api/v2/prediction/v4-user-upload                          # 上传自算（本轮不用）
```

**按 PDB 条目取，不是按 UniProt 取。** 状态端点返回：

```json
{"id":"1IEP","database":"v3-conservation-hmm","status":"successful",
 "metadata":{"predictionName":"1IEP","structureName":"structure.cif"}}
```

`status` 四态：`queued` / `running` / `successful` / `failed`。已算过的条目（如 `1iep`）**10 秒内直接 `successful`**（命中缓存）；没算过的先 `queued`，实测 `1lyz` 约 25 秒翻到 `successful`。

**`prediction.json`（39 KB）一次给全我们要的东西：**

```json
{
  "metadata": {"p2rank_version": "2.5.1"},
  "structure": {
    "indices": ["A_225", "A_226", ...],          // 548 项，PDB 作者编号
    "sequence": ["I", "V", ...],
    "regions": [{"name":"A","start":0,"end":273}, {"name":"B","start":274,"end":547}],
    "scores": {"plddt": [...], "conservation": [...]}
  },
  "pockets": [
    {"name":"pocket1", "rank":1, "score":"39.46", "probability":0.957,
     "center":["16.9132","54.5702","16.1823"],
     "residues":["A_246","A_248","A_249","A_252", ...],
     "surface":["186","202", ...]},
    ...
  ]
}
```

实测 `1iep` 给 5 个口袋，`score` 依次 39.46 / 38.74 / 2.19 / 1.92 / 0.66。

**这几件事让它成为合格的兜底来源（P1）：**

- **`residues` 就是 PDB 作者编号**（`A_246` / `A_360` 这种形式），**不需要任何换算**。验证方式是拿 `A_360` 去 `structure.indices` 里取对应的 `sequence` 字母，得到 `I`（Isoleucine），与 `runs/1iep-demo/meta.json` 的 `A / 360 / ILE` **逐字一致**。上一版 PDBe 路线在这一步要另拉一份 `pdbe_doc` 做序列比对，这里没有这一步。
- **`center` 直接给了口袋的三维中心**，省掉「先算残基坐标再求几何中心」这一段。
- **`probability` 是校准过的置信度**（0.957 对 0.702），比原始 `score` 更便于前端分级展示；`score` 同时保留在 `pockets[].score` 里。
- **与有无配体无关**：`1lyz`（`nonpolymer_entity_count = 0` 的 apo 溶菌酶）照样给出 2 个口袋（`pocket1` score 13.34 / probability 0.702 / 17 残基）。
- **与现有数据交叉验证通过**：prankweb 的 `pocket1`（48 残基）与我们 `meta.json` 的 P1（28 残基）**交集 25 个**——prankweb 在外周多圈了几圈，核心是同一个位点。

**一个必须写进实现的陷阱（实测踩到）：**

> **prankweb 对任何编号都返回 `201` + `status: "queued"`。**

不存在的 `9xyz` / `8zzz` 也会被排队：`9XYZ` / `8ZZZ` 过 20 秒翻成 `running`，`1ABC` 最终变 `failed`。**HTTP 状态码完全不能用来判「这个条目有没有结果」，只有 `status` 字段可以**——必须轮询到 `successful` 或 `failed` 才算一次查询结束。写成「拿到 201 就当成功」会拿到一个永远读不到数据的状态。

**与上一版（PDBe-KB 路线）的实测对比：**

| 检查项 | PDBe-KB 注释接口 | **prankweb.cz（现选）** |
|---|---|---|
| 响应大小 | 28~34 MB（所有提供方塞在一起，要的自己只有一项） | **39 KB** |
| 首次耗时 | **120+ 秒** | 缓存命中 **10 秒**；未算条目约 **25 秒** |
| 稳定性 | `P00698` / `P69905` / `P0DTC2` **稳定 502**，重试无用 | 四态 `status` 明确，未遇 502 |
| 编号体系 | UniProt 编号，**要另拉 `pdbe_doc` 做序列比对换算** | **PDB 作者编号，免换算** |
| 给的字段 | 残基 + `rawScore` + 结构 id | 残基 + `score` + **`probability`** + **三维 `center`** |

**降级口径**：`status == "failed"`、或轮询超时、或网络不可达 → P1 视为不可用，`pockets` 写空数组，不报错、不无限重试。空就是空，不编。

#### 2.3.5 从复合物里取口袋：配体聚类（**P0 的执行算法**）

**这套算法现在服务 P0 主来源**：下载一个复合物 → 取它的配体 → 圈出口袋。
（对 P1 的预测残基不做这一步——那时直接用它给的残基集合。）

**算法（纯函数，零依赖）：**

1. 从**该复合物**的坐标里取全部非水杂原子组（`pdbtext.hetero_groups(exclude_names=modres_names)`），
   且**只取与受体同链的**。
2. **按口径筛掉不像配体的**（见 2.3.7）。
3. 剩下的组按**几何邻近聚类**：两组之间只要有任一对原子的距离 ≤ `CLUSTER_GAP`（暂定 4.0 Å），就归为同一簇。用并查集，不做阈值调参。
4. 每一簇就是一个口袋：
   - `center` = 簇内全部原子坐标的算术平均
   - `radius` = 5.0 Å（与 `build_demo_data.py` 的 `POCKET_RADIUS` 一致）
   - `residues` = **受体链**内任一个原子进入**簇内任一个原子**的 `radius` 内的残基，带 `min_distance`
5. `is_reference`：与 `target.ligand` 同名的那个簇标 `true`。
6. 口袋带上**来自哪个复合物**：`pdb_id` / 分辨率 / 实验方法。这是 P0 相对 P1 的关键优势。

**为什么是「任一原子对」而不是「中心距」**：`build_demo_data.py:95` 的注释已经说清楚了——
伊马替尼这类长条分子，中心到两端超过 10 Å，按中心算出来的口袋只有分子中段那几个残基。
聚类判据同理：拿中心距去判「这两个配体是不是同一位点」会把一个狭长位点切成两半。

**为什么用并查集而不是「顺序贪心」**：三个组 A-B-C，A 靠近 B、B 靠近 C、A 不靠近 C。
顺序贪心会把 C 分出去，并查集不会。这不是理论问题，长条位点里很常见。

**跨复合物合并（P0 的收尾）**：不同复合物里同一个位点会出现多次。
全部下载完成后按**口袋中心的距离**（暂定 6 Å 内算同一位点）合并成一条，
合并后的口袋保留**所有**来源复合物（就是 2.3.10 那张表的机器版：`complexes`）。
**这一步让「一个口袋对多个 PDB」成为主结果的天然形态，不再是补充。**

**P2 的价值：距离可以问 RCSB，不必自己算全部。** RCSB 的
`/rest/v1/core/polymer_entity_instance/<entry>/<chain>` 里字段 `rcsb_ligand_neighbors`
直接给出**残基原子—配体原子的距离对**（实测 `1iep` 链 A 有 35 条），字段含
`auth_seq_id` / `comp_id` / `atom_id` / `ligand_comp_id` / `ligand_atom_id` / `distance`。

**实测对得上**：RCSB 给 `ILE 360 ↔ STI N51 = 2.668`，而 `runs/1iep-demo/meta.json` 里
同一个残基写的是 `min_distance: 2.67`——**两条独立路径得到同一个数**，这说明 P2 可信。
它是 P0 的加速与交叉验证（省一次坐标解析），不是替代。

#### 2.3.6 复合物结构怎么排、取几个（**P0 的核心规则**）

P0 会搜到这个蛋白的**所有**复合物（ABL1 有 69 个）。**不逐个下载**，按下面的顺序排，取前 N 个：

1. **分辨率好的优先**：`resolution_combined` 小的排前面（X-ray 与 EM 都认）。
   分辨率是「这个结构可不可信」的直接指标。
2. **实验方法**：X-ray / EM 优先于低分辨的其它方法。
3. **配体像真配体的优先**：先按 2.3.7 的口径筛一遍，只留通过口径的（`3eg3` 只有甘油，
   排序时会被排到很后面甚至滤掉）。
4. **同名的配体去重**：同一个配体在多个结构里出现，只取最好的那一个（省下载）。

**N 暂定 12**，可配。返回里写清「总共几个、按上述规则取了几个、被口径滤掉几个」——
用户看到「只有 3 个口袋」时，要能判断是结构里真只有 3 个，还是我们没取完。

**受体自己那个条目自动入选**（它就在登记表里，不用下载）。

**prankweb 那条路（P1）的「典型结构」问题不存在**：请求本身按 PDB 条目发
（`.../v3-conservation-hmm/<PDBID>`），算的是哪个结构、给的就是哪个结构。
但 P1 **只在 P0 为空时才走**（见 2.3.9）。

**注意**：一个 PDB 条目里可能有**多条链**。下载后**只取与受体同链的配体**，
否则会把另一条链的配体画进受体上根本不存在的口袋。
（prankweb 那边同理：`structure.regions` 按链分段，`A_246` 这种前缀就是链名。）

#### 2.3.7 配体口径（已定）

**「大分子有机配体」才当配体。** 具体排除四类：

| 类别 | 判据 | 例子 |
|---|---|---|
| 水 | `atom.is_water`（`WATER_RESIDUES`） | HOH / DOD / WAT |
| 修饰残基 | 残基名 ∈ `MODRES` 登记名（`structure.py:339` 已在收） | MSE / SEP / TPO |
| 金属离子与单原子离子 | 组里原子数为 1，或残基名在 `MONATOMIC_IONS` | ZN / MG / CA / NA / CL |
| 常见缓冲液与冷冻保护剂 | 残基名在 `BUFFER_LIKE` | SO4 / GOL / EDO / PEG / ACT / MPD |

`MONATOMIC_IONS` 与 `BUFFER_LIKE` 两个常量**新建**，放在 `export.py` 里，每个名字后面写明它为什么被排除。
装金属的酶（碳酸酐酶的 Zn）这一轮会**看不出口袋**——这是选定的代价，记在这里。
将来要放开，把 `MONATOMIC_IONS` 从筛除名单里挪走即可，不用改算法。

**筛选结果要报出来**：`write_run` 的返回里写清「排除了 N 个杂原子组，其中 3 个金属离子、5 个缓冲液」，
否则用户看到「只有 1 个口袋」时无法判断是结构里真的只有一个配体，还是被筛掉了。

**P2 实测给这个口径加了底气**：`3eg3` 只有甘油，RCSB 的 `rcsb_ligand_neighbors`
把它明明白白报出来（`GOL` 4 条，最短 3.18 Å）——**在聚类之前就能看出来「这里没有真配体」**，
不必等几何算完才发现。

#### 2.3.8 `source` 字段与前端徽标（已定：加）

`pockets[]` 新增 `source`：

| 值 | 含义 | 前端徽标 |
|---|---|---|
| `"p2rank"` | prankweb.cz 算的 p2rank 预测（几何 + 保守性） | 「预测」 |
| `"ligand"` | P0：从**该蛋白的复合物结构**取出的配体聚类，坐标是实测的 | 「实验配体」 |
| `"uniprot_site"` | 保留给将来（按 UniProt 位点标注推断的口袋） | 「推断」 |

**前端改动范围**：只在一个组件里加徽标判断；`source` 缺失时**不显示徽标**（沿用已有降级口径）。
同时 `provenance.pockets` 从字符串变对象，读它的那段也要改成「是对象就读 `note`，是字符串就原样显示」——
两种形状都认，免得旧的 `runs/1iep-demo/` 打不开（它是回归基线）。

这是本轮**唯一**动 `web/` 的地方，与 4 节「不改前端」的原计划相抵，以本节为准。

#### 2.3.9 P0 为空时：转 P1 预测（**副选的入口**）

**这是全节的分岔点。** P0 走完发现一个口袋都没有（该蛋白没有任何带配体的已发布复合物，
典型是 AF3 预测结构、或只有 apo 实验结构），**才**转 P1：

- **P0 有货 → 用 P0，不再查 P1。** 有实测事实就不叠加预测，免得主次不分。
- **P0 为空 → 走 P1（prankweb）**。它按 PDB 条目取，与有无配体无关。
  有则出预测口袋，标 `source: "p2rank"`，并在 `provenance` 里写明**为什么转成了预测**。
- **P1 也没有**（无 PDB 条目、`status == "failed"`、网络不可达）→ `pockets` 写空数组，
  页面显示「没有口袋数据」，**不编一个中心出来**。沿用前端已有的降级口径。

**这里的分寸**：宁可显示「没有口袋数据」，也不要拿 P0 的零覆盖去凑一个空数组还说得像有结论。
返回里要写清**三态**——`provenance.pockets.origin = "complex" | "predicted" | "none"`，
以及 P1 那一侧是「查了没有」还是「没查成」（`"absent" | "unavailable"`）：
这三者对用户的含义完全不同，前者是数据性质，后者是我们可以再试的。

#### 2.3.10 参考做法：SCN9A（Nav1.7）那种「口袋 × PDB 清单」**正是 P0 的目标形态**

拿 Nav1.7 当例子看一个人工整理的回答（按口袋分组，每格给 PDB / 配体 / 位点 / 分辨率）。
**2026-10-07 优先级反转之后，那份回答不再是「另一个思路」，而是我们要自动化的目标**：

| | 那份 Nav1.7 回答 | 我们的 P0 |
|---|---|---|
| 口袋从哪来 | **一批已发布复合物**（`6J8G/6J8H/6J8I/6J8J`、`8I5B/8I5X/8I5Y`、`8THG/8THH`、`7XMF`、`7W9K~7W9T`、`8G1A`、`8J4F`…）里的配体 | **同一批复合物**，自动搜出来 |
| 覆盖范围 | **跨很多个 PDB 条目**汇总 | **跨该蛋白所有带配体的结构**，同一个 |
| 每个口袋给什么 | 一个 **PDB ID + 配体名 + 分辨率** | `complexes: [{pdb_id, ligand, resolution}]`，同一个 |
| 「典型结构」怎么定 | 手挑的 | 2.3.6 的排序规则自动挑 |

**P0 就是把这张表自动攒出来**：搜全部复合物 → 下载 → 取配体 → 圈口袋 → 跨结构合并。
搜索工具（2.3.11）本来就在做第一半。

**那份回答里 P0 替不了、要留给模型的部分**（这才是「副选」之外的真实差异）：

1. **位点命名**。它的「Site C / BIG / III–IV 窗口 / VSDII / VSDIV」是**文献里的命名**，
   不是一个可以现算的几何量；接口能说「这个配体绑哪些残基」，说不出「这叫 Site C」。
   **这一层归类由模型看文献判断**（走 2.3.11 的搜索工具），**不进 `pockets[]` 的结构化字段**。
2. **人工核对的取舍**。它把 `6J8I/6J8J` 归 VSDII、`8G1A` 的 CBD 分了两个位点——
   哪些配体算真配体、两个位点算不算同一个，仍要判断。我们按 2.3.7 的口径自动筛一遍，
   **边界情况由模型补**。

**`is_predicted` 与 `is_experimental` 分开标**（沿用那份回答的口径）：`source` 的
`"ligand"`（实验）/ `"p2rank"`（预测）就是这个区分，而且现在它**同时**决定了主结果还是副选（2.3.3）。

**一句话**：那份 Nav1.7 回答的**内容**就是 P0 要自动产出的东西；
它的**位点命名**留在模型那一侧，作为对 `pockets[]` 的解读，不写进结构化字段。

#### 2.3.11 能不能搜网页/文献？能，而且比想的强（实测）

上一节写「我们没有文献链路」，这话要说准：**不是不能搜，是要分清「搜到什么」与「搜到的能不能当数据」。**
2026-10-07 实测了四个接口，**全部可用**，用的还是我们已有的 `http.py`（`httpx`）：

| 接口 | 端点 | 实测结果 | 能给什么 |
|---|---|---|---|
| **RCSB 文本搜** | `POST https://search.rcsb.org/rcsbsearch/v2/query`，`service: full_text` | `"Nav1.7 ProTx-II"` → **8 个条目**：`2N9T 5O0U 6N4I 6N4Q 6N4R 6J8I 6J8J 7W9M` | **全库全文搜**，命中就是那份回答里 ProTx-II 组的那几个 PDB |
| **RCSB 属性搜** | 同上，`service: text` + `attribute` | 按 UniProt `Q15858`（SCN9A）→ **44 个条目**，头几个 `5EK0 6J8G 6J8H …` | 按 UniProt/配体/方法等任意属性筛结构 |
| **RCSB GraphQL** | `POST https://data.rcsb.org/graphql` | 一次问 6 个条目的配体 → `8THH: [PCW LPE Y01 IYJ NAG P5S]`、`7XMF: [NAG G2W]` | **批量**取每个条目的配体代号（`chem_comp.id`）与计数 |
| **PubMed E-utilities** | `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | `Nav1.7 structure cryo-EM` → 3 篇；`SCN9A Nav1.7 cryo-EM complex` → 3 篇 | 文献题目/摘要/DOI；再配 `efetch` 取正文片段 |

**欧洲 PMC**（`https://www.ebi.ac.uk/europepmc/webservices/rest/search`）也通，
`SCN9A` 有 3848 条、`SCN9A AND "PDB:6J8I"` 精确命中 2 篇——**能按 PDB 编号反查文献**。

**这说明那份 Nav1.7 回答里的大部分内容，机器其实也能攒出来**：

- 「哪些 PDB 是 Nav1.7 的」→ RCSB 属性搜按 UniProt 一查就是 44 个。
- 「每个 PDB 里有什么配体」→ RCSB GraphQL 批量一次给全（实测 `8THH → [PCW LPE Y01 IYJ NAG P5S]`），
  再查一次 `chem_comp` 就知道代号是什么分子：`Y01` = 胆固醇半琥珀酸酯、`LPE`/`PCW`/`P5S` = 各类磷脂
  （**按我们的口径全要筛掉**）、`IYJ` = 三嗪二胺、`OJ0` = 左布比卡因、`5P2` = 芳基磺酰胺。
- 「ProTx-II 绑在哪」→ RCSB 文本搜直接命中 `6J8I/6J8J/6N4I/6N4Q/6N4R/7W9M`。

**P0 就是这条链的自动化**（2.3.3 / 2.3.5）：搜全部复合物 → 下载 → 取配体 → 圈口袋 → 跨结构合并。
搜索工具（`search_pdb` + `search_literature`）**既是 P0 的第一半，也是给模型另外用的入口**。

**但 `write_run` 里只放「确定性取数」，不放「开放式搜索」**，三条理由：

1. **搜索给「候选」，不给「确定答案」**。RCSB 文本搜返回的是**条目列表**，
   「`Y01` 这个配体到底算不算真配体」还要过我们的口径（2.3.7）——
   `NAG`（糖基化）、`LPE`/`PCW`（脂质）、`P5S`（去垢剂）按我们的口径**全要筛掉**，
   而它们正是查询结果里排前面的那几个。**P0 用的是属性搜（可枚举、结果确定），
   不是文本搜**——属性搜是「按条件筛」，结果可复现；文本搜是「按猜测找」，结果看输入。
2. **位点命名不在接口里**。那份回答把 `6J8I/6J8J` 归到「VSDII」、`7XMF` 归到「失活态孔」——
   这是人读论文图注写下的，接口给不了。**这一层留给模型**（见 2.3.10）。
3. **导出动作要可预期**。P0 的取数是「一条查询 + N 个下载 + 本地几何」，每一步都能缓存、
   能设上限、能明确失败。混进开放式搜索会让时长与结果都变得不确定。

**所以本轮的分工：**

- **`write_run` 走 P0（属性搜 + 下载 + 几何），拿不到才转 P1（预测）**。全部确定性、可缓存。
- **两个搜索工具独立存在、给模型用**（`search_pdb` / `search_literature`，只读，进 Plan 白名单）。
  模型用它做 P0 覆盖不到的事：**查文献、认位点、给口袋起名字**。
- **搜索结果不写进 `meta.json`**；要进页面，仍走 `write_run` 的口径（筛配体、算残基、标 source）。

**这也回答了「那份 Nav1.7 回答能不能复制」**：能——**内容部分 P0 自动出**（口袋 × PDB 表），
**命名与解读部分由模型用搜索工具补**。我们提供工具与口径，模型负责判断与组织。

## 3. 距离与坐标：纯 Python 自己算（不使用化学依赖）

**决策：`write_run` 不引 RDKit / numpy，用 `pdbtext` 那层纯文本解析加两两求距离。**

理由：

- 算法本身简单到不值得引依赖：`min(|a - b| for a in 残基重原子 for b in 配体重原子)`。
- `pdbtext.py` 已经提供 `AtomRecord.coord` 与 `HeteroGroup.center` / `coords`，
  坐标解析不需要 RDKit。
- **让「导出」这个动作不依赖化学环境**：本机没装化学依赖（`chemistry.py` 的既定事实），
  如果 `write_run` 走 `require_chem()`，本机就只能靠假替身验流程，验不了真的坐标数学。

代价：上万原子时纯 Python 大约几百毫秒到一秒。可接受——这是导出动作，不是交互动作。
真到慢得不能忍时再分层（比如只用重原子、按残基先做包围盒粗筛），不现在先猜一个阈值。

**氢原子不参与距离**：PDB 里氢的记录位置不齐（有的结构没有氢、有的只有部分），
把它算进去会让同一份结构在不同来源下算出不同的 `min_distance`。按重原子算。

**P2 可以先问 RCSB，不必自己算**：`rcsb_ligand_neighbors` 直接给距离对（见 2.3.5）。自己算与问 RCSB 两条路都要有——前者离线可用、后者是交叉验证。**实测两者对得上**
（`ILE 360 ↔ STI N51`：RCSB 2.668 / 本仓库 meta 2.67），所以哪条先走都行。

## 4. 产出与改动

```
新增：
  opencode_tui/core/agent/domain/export.py      write_run 工具 + 几何算法（纯 Python）
  opencode_tui/core/agent/domain/pockets.py     P0/P1/P2 三层口袋来源
  opencode_tui/core/agent/domain/prankweb.py    prankweb.cz 客户端：状态轮询 + 取结果 + 磁盘缓存
  opencode_tui/core/agent/domain/complexes.py   P0 复合物：RCSB 属性搜 + 排序 + 下载（2.3.6）
  opencode_tui/core/agent/domain/rsearch.py     搜索工具：RCSB 文本/属性搜 + GraphQL 批量取配体
  opencode_tui/core/agent/domain/literature.py  文献工具：PubMed E-utilities 检索（2.3.11）
  opencode_tui/core/web/__init__.py
  opencode_tui/core/web/service.py              包内只读服务（后台线程）
  opencode_tui/core/web/routes.py               路由与 JSON 形状（唯一真相）
  opencode_tui/core/agent/domain/viewer.py      open_viewer 工具

修改：
  opencode_tui/app.py                       启动时起服务，退出时停；把 URL 交给领域工具
  opencode_tui/core/agent/domain/__init__.py 模块表加 export / pockets / prankweb / complexes / rsearch / literature / viewer
  opencode_tui/core/agent/domain/all_tools.py 装配加 write_run / open_viewer / search_pdb / search_literature
  opencode_tui/core/agent/permission.py      PLAN_DENY 加 write_run（它往磁盘写交付物）；搜索两件只读，**放行**
  opencode_tui/core/agent/prompt.py          身份段提一句「结构可以导出到网页看；找候选结构/文献走搜索工具」
  tools/serve_web.py                        改为调用 core/web/routes.py，不另写一份
  opencode_tui.spec                         datas 加 web/ 静态资产

不改：
  runs/1iep-molstar/（演示数据原样留着，当回归基线）

前端只动一处：
  web/ 的口袋组件加 source 徽标，且 provenance.pockets 两种形状都认（见 2.3.8）
```

### 4.1 `write_run` 的签名

```
write_run(
    receptor: ReceptorHandle,        # 必填，产物目录或坐标文件
    ligands: LigandSetHandle | None  # 位姿文件集合；本轮可空
    run_id: str | None               # 留空时按受体的 pdb_id 生成
    uniprot: str | None              # P0 用：搜该蛋白所有复合物；留空时从 Entry.meta 找
    pdb_id: str | None               # P1 用（仅当 P0 为空）；留空时从 Entry.meta 找
    name: str | None                 # 运行显示名
    view: "pockets" | "structure"    # 展示什么；默认 pockets
)
```

- **`view` 让同一个工具负责两种展示。** `structure` 只写受体与结构，**完全不碰口袋来源**
  （不联网、不转 P1），两种模式共用同一套 `preprocess.clean_structure`——预处理只写一份，
  否则「展示的口袋」和「展示的结构」会来自两份不同的坐标。
- **收 handle 不收路径**（`import_structure` 是唯一例外）。
- **不再有 `pocket_ligand` 参数**：口袋由三层来源给出（见 2.3.3），
  不再「挑一个参考配体」。P0 由复合物配体聚类自己定，P1 由 `pocket1/pocket2` 排序定。
- 新增 `uniprot: str | None`：**P0 的主键**。拿它去 RCSB 属性搜
  （`UniProt accession + nonpolymer_entity_count > 0`）列出该蛋白**所有**带配体的已发布结构，
  按 2.3.6 排序取前 N 个下载。留空时从 `Entry.meta` 里找（`fetch_structure` / `import_structure`
  登记时写过）；找不到就等于 P0 只覆盖手上这一份结构。
  **注意这不是 PDB 编号**——一个 UniProt 对应几十上百个 PDB 条目，混淆会让 P0 退化成「只看手上这一份」。
- `pdb_id: str | None`：P1（prankweb）**按 PDB 条目取**，**只在 P0 为空时才用到**。
  留空时从 `Entry.meta` 里找；找不到就等于 P1 也不可用。
  **注意这里不是 UniProt 编号**——prankweb 的接口按 PDB 条目分片，一版设计错当成 UniProt 会让整条路走歪。
- 顺序：**先 P0（属性搜 + 下载 + 几何），P0 一个口袋都没有才转 P1（预测）**（2.3.9）。
- 返回：写出的目录、`meta.json` 里有哪些段、**排除了几个杂原子组及各类各几个**、
  **P0 搜到几个复合物 / 下载了几个（`total` / `used`）**、
  缺了哪几项及原因、**`complexes` 写了几条来源（跨哪几个复合物）**。
- 登记：产物目录登记成 `artifact`，父项挂受体；`meta` 里写 `run_id` 与 `kind: "run"`。

### 4.2 服务层的三处细节

1. **路由逻辑只有一份。** `tools/serve_web.py` 现在有 349 行，其中路由与 JSON 形状这一段
   要搬进 `core/web/routes.py`。`serve_web.py` 改成从包里导入它——**不能两边各写一遍**，
   否则接口形状会漂。
2. **线程模型。** `ThreadingHTTPServer` 是纯线程，不走 asyncio，与 Textual 的事件循环
   不冲突。`serve_forever()` 跑在 `daemon=True` 的线程里，进程退出时随主线程结束。
3. **端口避让。** 从 8860 起逐个试，最多试 20 个；实际端口写进日志与 `open_viewer` 的返回值，
   并显示在界面上（状态栏或 toast）。

### 4.3 `open_viewer` 的形态

```
open_viewer(run: ArtifactHandle | None, open_browser: bool = False)
```

- 返回完整 URL（结构页面直接指向 `#/structure/<run_id>`）。
- `open_browser=true` 时才 `webbrowser.open()`——**默认不开**：无头环境会静默失败，
  在 TUI 里 suspend 去开浏览器会把界面顶掉。默认让用户自己点 URL。
- 服务没起来时返回一条可操作的失败（「服务未启动，检查端口占用」），不是抛异常。

## 5. 执行清单

> **进度**：第 1~5 步已完成（`export.py` 含纯几何 + `write_run` / `open_viewer`；
> `complexes.py` / `prankweb.py` / `pockets.py`；`rsearch.py` / `literature.py`；
> 判据 `check_export_geometry.py` / `check_pockets.py` / `check_write_run.py` /
> `check_search_tools.py`；`test_agent` 1556 → **1743** 项，`test_smoke` 2092 → **2279** 项；
> 禁用字 0 处；判据五段全绿，领域工具 11 → **15** 个）。
> **P0 全链路已用真坐标跑通并与 `runs/1iep-molstar/meta.json` 对拍一致**
> （中心 15.614/53.380/15.455、28 个残基、`ILE360` 最短 2.67）。
> `write_run` 的 `meta.json` 顶层键与演示数据一致，**`contacts` 是刻意不出的例外**（§2.1）。
> **两个搜索工具已联真网验证**：属性搜 `Q15858` → 44 条、文本搜 `Nav1.7 ProTx-II` → 8 条、
> 文献 `SCN9A Nav1.7 cryo-EM complex` → 3 篇，与方案稿实测一致。
>
> **第 6 步已完成**：新建 `core/web/{__init__,routes,service,__main__}.py`，
> 路由与 JSON 形状搬进 `routes.py`（唯一真相），`tools/serve_web.py` 由 349 行缩到 74 行的命令行壳，
> `tools/check_web_api.py` 改指向 `python -m opencode_tui.core.web`（**19 → 20 条，全绿**）。
> 新增判据 `.probe/check_web_service.py`（形状 / 端口避让 / 线程收尾 / 路由注入 / 默认端口 五节），
> 已进 `run_all_checks.sh` 注册表（**总数 67 → 70**）。
>
> 第 6 步抓出的两个真问题（都不是判据自己的毛病）：
>
> 1. **`pick_port` 的端口探测在 Windows 上是失效的**。原先用「bind 得上 = 空闲」判断，
>    而 Windows 的 `SO_REUSEADDR` 语义与 POSIX 不同：另一个 socket 正在该端口上 listen 时，
>    带这个选项的 bind 照样成功。于是「上次没退干净、端口还占着」这个**最需要避让**的场景
>    反而一路绑过去，`WebServer` 在同一端口上起第二个监听。改成 `connect_ex` 连一次
>    （与 `export._probe_port` 同一口径）。
> 2. **内核注入污染了 `Part.tool_input`**。`Tool.execute` 把登记表按 `context_argument`
>    塞进 `arguments`，那份被写回 `Part.tool_input`；网页端于是显示出一份带内核对象的调用。
>    现在加 `on_arguments` 回执，记的是**注入之前**那一份。
>
> 另有一处判据假失败（已修）：`check_web_api.py` 里 `LINK_SESSION = "ses_0032"` 取的是
> 一个**已经不在状态文件里**的会话（现在只有 `ses_0001`~`ses_0033`，且最大会话是 `ses_0033`）。
> 取一个不存在的编号时，「只有对应会话拿到了运行」不会报出关联断掉。现在改成从副本里
> 现挑消息最多的那个会话，并单列一条「关联目标会话在清单里」把两种原因分开。
>
> **第 7 步已完成**（接线与应用集成）：
>
> - `export.runs_dir()` 从 `opencode_tui/runs/` 改指**仓根** `runs/`——两个服务入口默认都是仓根，
>   两处不一致会造出「工具写进去、网页看不见」的运行目录；已把 10 个写错位置的目录迁过去，
>   服务可见运行数 2 → 12。
> - `Tool` 加第四个注入通道 `viewer_argument`，`AgentLoop` 收 `viewer=`，`app.py` 装配
>   `WebService` 并在 `on_mount` / `on_unmount` 启停（`__init__` 里只装配不启动，
>   因为 `on_mount` 之前事件循环还没接上）。
> - `opencode_tui.spec` 的 `datas` 加 `web/`；`service.web_root()` 按 `sys._MEIPASS` 分支取目录。
> - `web/pages/structure.js` 按 `pockets[].source` 挂来源徽标（三态），
>   `provenance.pockets` 的**对象与字符串两种形状都能读**；`web/app.css` 加对应样式。
> - `tools/check_web_ui.js` 加两条：`sourced`（新形状，两个口袋「实验配体」、两个「预测」）
>   与 `legacy`（旧形状，**一个徽标都不挂**且页面不报错）。**32 → 44 条，全绿**。
>   自检已做：给 `POCKET_SOURCE_LABEL[...]` 补一个 `|| "未知"`，legacy 那三条必须报出来（实测报出）。
>
> 第 7 步抓出的真问题：
>
> 1. **注入通道把 `open_viewer` 的两条失败路并成了一条**。加了 `viewer` 注入之后，
>    「没拿到服务对象」会先撞上「内核装配问题」，`check_write_run` 与 `test_agent.py` 里
>    盯着「没有在监听」那条断言就永久失败。两条路的**触发条件已经不同**，现在分开断言：
>    没拿到对象 → 装配问题（重试无用）；拿到了但 `port=0` → 用户可操作（先把服务跑起来）。
> 2. `runs/` 有两份（上面第 1 条）。原先 `1iep-molstar` 与 `1iep-demo` 是**同一份旧数据**，
>    都没有 `source` 字段也没有新的 `provenance` 形状，光靠真实目录验不出新形状——
>    前端判据里那一条是从旧数据现改成新形状（`makeSourcedRun`）。
>
> **第 8 步已完成**（端到端与回归）：
>
> - **三个真目标各走一遍**（`.probe/e2e_scenario.py p0|p1|none`，从 `import_structure`
>   一路跑到 `meta.json`，走的是真网）：
>   1. **P0 有货**（`1IEP` / `P00519`）：`total=69, used=12`、`origin=complex`、
>      **22 个口袋**；来源表 12 条（PDB + 配体 + 分辨率）；筛掉三类杂物
>      （修饰残基 2 / 单原子离子 4 / 缓冲液或糖 11）。与 §2.3 的要求严丝合缝。
>   2. **P0 为空但有预测**（`Q8N158` + `1LYZ`）：`origin=predicted`、`p2rank=ok`、
>      2 个口袋、`source=p2rank`；note 里带上「主来源为空（…查了，确实没有），改取预测」。
>   3. **两者都不可用**（`P00000` + `9ZZZ`）：`origin=none`、`p2rank=unavailable`、
>      0 个口袋、`provenance.pockets.method=""`。
> - **端到端界面核对** `.probe/check_web_e2e.js`（新建，16 条全绿）：把四个运行目录
>   （三个真产出 + 旧形状 `1iep-demo`）喂给结构页，逐一核对徽标条数/逐条文字/来源说明行；
>   **旧形状那份 0 个徽标且页面不报错**，是「降级口径」的回归。
> - `tools/check_web_ui.js` 44 条全绿（自检已做）；`run_all_checks.sh` 注册了这两条前端判据。
> - 两层断言 `test_agent` / `test_smoke` 退出码 0；禁用字 0 处；重新打包成功
>   （产物时间 15:43:46，比所有源码新）；**成对抓帧七帧再次全部一致**。
> - 判据 70 条按五段逐条跑（`run_all_checks.sh` 整批会被外壳回收，见 §8 备忘）。
>
> 第 8 步抓出的两个真问题：
>
> 1. **RCSB 零命中被当成「没查成」**。`post_json` 把 **HTTP 204 无响应体**当成
>    「返回的不是 JSON」抛出去，于是 `complex_pockets` 报 `origin="none"` 并说
>    「没查成，不是查了没有」——**话正好反了**。实际语义是「接口正常应答、只是没有
>    符合条件的记录」（`Q8N158` 就是这个）。新增 `http.EmptyResult` 专门接住 204，
>    `complexes.list_complexes` 读成 `([], {})` 并缓存一份空的。修完 `Q8N158` 报的是
>    「Q8N158 名下没有带配体的已发布结构（查了，确实没有）」。
> 2. **`p1_state` 的分档在搜措辞**。`export.write_run` 原来判
>    `"没查成" in predicted.note`，而 prankweb 服务端报 failed 时 note 写的是
>    「prankweb 对这个条目报失败：服务端没给原因」——**搜不到那四个字**，于是把
>    「没查成」说成了 `absent`（查了没有）。现在 `PocketBundle` 加结构化字段
>    `reason`（三态 `"checked"` / `"failed"` / `""`），调用方按字段分档，
>    **不再从 `note` 里搜措辞**——文案会改，而这两档说出的话相反。
>
> 另有一处真缺陷（前端）：**零口袋的运行目录页面打不开**。`structure.js` 的
> `setMode("pockets")` 里 `meta.pockets[0].id` 在零口袋时抛 TypeError，异常在 async 里
> 被吞掉、`window.__ready` 永不置真——页面看着加载完了其实没就绪。`write_run` 报
> `origin=none` 时写的就是这一态，所以这是条真路不是极端情况。已按 `pockets.length`
> 分支并给一行空态说明。**自检已做**：把分支改回旧写法，`.probe/check_web_e2e.js`
> 立刻报出「零口袋那份：初始化没跑完（多半是抛了异常）」与「清单区是空的」。

分 8 步。每步做完都能单独验证，且**每步都应保持现有断言全绿**。

（第 5 步「搜索工具」与前四步可以并行做——它不改 `write_run`，也不进 `meta.json`。
**第 2、3 步有先后**：P1 只在第 2 步报「P0 为空」时才被调用；两步的代码可以并行写，
但集成到第 4 步时必须按这个顺序接。）

### 第 1 步　几何算法（纯函数，零依赖）

产出：`export.py` 里的 `min_distance` / `pocket_residues` / `cluster_ligands` / `ligand_center` / `contacts_for`。

验收：
- 各纯函数一条最小样例（手算的距离对得上）
- **重原子才参与**：造一份带氢与不带氢的同结构，`min_distance` 相同
- 空的配体组 → 报「没有可用的配体」，不返回 0
- **聚类是并查集不是顺序贪心**：造 A-B-C 三点（A 近 B、B 近 C、A 远 C），三个必须归一簇，
  且换一个遍历顺序结果不变
- **配体口径**：造一份带 HOH / MSE(MODRES) / ZN / SO4 / STI 的结构，
  只有 STI 进聚类；四个被排除的名字各出现在排除清单里

### 第 2 步　P0 主来源：搜该蛋白的所有复合物 → 排序 → 下载

产出：`complexes.py` 的 `list_complexes()` / `pick_top()` / `download_entry()`，
`pockets.py` 的 `complex_pockets()`。

**这一步是本轮的主路径**（2.3.3）。整条链分四段，每段都能单独打桩验：

1. **搜**：RCSB 属性搜（`service: text` + `attribute`），条件 `UniProt accession == <AC>`
   加 `nonpolymer_entity_count > 0`。实测 ABL1（`P00519`）→ 69 个条目。
2. **排序取前 N**：按 2.3.6 的规则（分辨率 → 实验方法 → 配体像真配体 → 同名去重），N 默认 12、可配。
3. **下载**：`https://files.rcsb.org/download/<ID>.pdb`，实测平均 0.24 MB。
   走已有的 `http.py`，写进缓存目录，失败的不计入、继续下一个。
4. **取口袋**：复用第 1 步的几何算法——筛掉四类杂原子（2.3.7）→ 剩下的配体并查集聚类 →
   每簇圈 5 Å 内的残基 → 跨复合物合并（中心 6 Å 内算同一个，`complexes` 表记全来源）。

**离线真样已经备好**（`.probe/`，gitignore）：

| 文件 | 内容 | 用途 |
|---|---|---|
| `rcsb_search_abl1.json` | ABL1 属性搜的 69 个条目 | **搜索段的正面样**（可截断成小份存进 `tests/`） |
| `rcsb_gq_ligands.json` | 6 个条目的 GraphQL 配体清单 | **批量取配体段的正面样** |
| `rcsb_1iep_A.json` | `1iep` 链 A 的 35 条 `rcsb_ligand_neighbors` | 与 P2 距离对拍 |
| `rcsb_1lyz_A.json` | `1lyz` 链 A 的 **0 条**邻居 | **P0 为空这一态的样**（转 P1 的入口） |
| `1iep.pdb` | 下载下来的真坐标（0.24 MB 量级） | 端到端几何段的输入 |

验收：
- **一条属性搜查询拿到条目集**：喂打桩响应，断言解出 69 个 id，且**没解出 `polymer_entity_count` 之类的旁支字段**
- **排序确定**：给一组带分辨率/方法/配体的条目，断言前 N 个的顺序与 2.3.6 的规则逐条对得上；
  同一组输入跑两遍结果**完全一致**（不能有随机或字典序残留）
- **下载有上限、有容错**：打桩让第 3 个条目 404，断言前两个仍入库、第 3 个记进失败清单、总下载数 **≤ N**
- **配体口径串起来**：喂 `8THH` 那份 `[PCW LPE Y01 IYJ NAG P5S]`，
  断言 `PCW`/`LPE`/`P5S`/`NAG` 被筛掉、只剩 `Y01`/`IYJ`（2.3.7 的口径，不是逐名字打补丁）
- **跨复合物合并**：造两个复合物、配体中心相距 3 Å 的样 → **1 个口袋**、`complexes` 有 2 条；
  相距 10 Å → **2 个口袋**、各 1 条
- **P0 为空要能判出来**：喂 `1lyz` 那种「该 UniProt 下全是 apo」的样，断言 `complex_pockets()` 返回空
  并**明确报「P0 为空」**（这是转 P1 的唯一入口，判错就永远转不到预测）
- **不编条目**：搜索/下载任一环失败 → 该来源留空，**不造一个看着合理的 PDB ID 填进去**

### 第 3 步　P1（副选）：prankweb 口袋预测的拉取、轮询与缓存

产出：`prankweb.py`（状态轮询 + 取 `prediction.json` + 磁盘缓存 + 降级）、`pockets.py` 的 `p2rank_pockets()`。

**这一层只在第 2 步报「P0 为空」时才会被调用**（2.3.9）。主路径不走这里。

**这一步的坑全在协议上，不在解析上**——`prediction.json` 本身很好解析（39 KB），危险的是
**「任何编号都返回 201」**这条陷阱（见 2.3.4）。全部用**离线样本 + 打桩**验，判据不打真实网络。

**离线真样已经备好**（`.probe/`，gitignore）：

| 文件 | 内容 | 用途 |
|---|---|---|
| `prankweb_1iep_prediction.json` | `1iep` 的 5 个口袋，39 KB，含 `center`/`residues`/`score`/`probability` | **解析的正面样**，且能与 `meta.json` 的 P1 交叉验证 |
| `prankweb_1lyz_prediction.json` | apo 溶菌酶的 2 个口袋，9.4 KB | 「P0 为空（该蛋白没有任何带配体的复合物）才转预测」这一态的样 |

（`rcsb_1iep_A.json` / `rcsb_1lyz_A.json` 两份 P2 的样已挪到第 2 步的表里。）

重取样品的脚本：`.probe/dump_p2rank_sample.py`（**这份脚本是上一版 PDBe 路线留下的，本轮只作参考**——
prankweb 的响应 39 KB 可直接存盘，不需要筛）。

验收：
- **解析正面样**：`prankweb_1iep_prediction.json` 解出 5 个口袋，`pocket1` 的 `residues` 非空、
  `center` 是三个可转 float 的字符串、`probability` 是 0~1 的数
- **`status` 四态全覆盖**：分别喂 `queued`/`running`/`successful`/`failed` 四种响应，
  只有 `successful` 继续取数据，其余**返回明确状态**（`queued`/`running` 是「等」，`failed` 是「不可用」）
- **「假 201」陷阱**（**本步最要紧的一条**）：用一个总是返回 `201 + queued` 的打桩，
  断言**不能**把它判成成功——必须看 `status`
- **轮询有上限**：打桩一直返回 `queued`，断言在有限次后停并返回「超时/不可用」，**不无限循环**
- **降级**：`failed` 或网络异常 → 返回 `unavailable`，**不抛异常、不重试到底**
- **编号对接**：`pocket1` 的残基编号是 **PDB 作者编号**。造一个位移已知的样，
  断言我们取到的 `A_360` 在 `runs/1iep-demo/meta.json` 里对应的就是 `ILE`——
  **不引入任何换算逻辑**（换算是上一版 PDBe 路线才需要的，这里有这一步就是错的）
- **只留受体的那条链**：`1iep` 的 `structure.regions` 有 A / B 两段，
  受体链是 A 时，B 段的残基**不能**进 `residues`
- **缓存命中**：同一条目第二次调用不发起请求（打桩计数），缓存文件写在 `~/.opencode_tui/prankweb/<PDBID>.json`

### 第 4 步　`write_run`（把 P0/P1 的结果写成一个运行目录）

产出：`write_run` 工具。

**这一步是装配点，不是新算法**：P0（第 2 步）与 P1（第 3 步）都已成形，
`write_run` 负责按优先级调用、把结果写成一个运行目录、登记成 `artifact`。

验收：
- 从 `receptor#1`（原始结构）+ 参考配体产出完整目录：`meta.json` / `receptor.pdb` / `ligands/`
- `meta.json` 的形状与 `runs/1iep-molstar/` 的那份**字段一致**（逐项核 key 集合，`pockets[].source` 是新增项）
- `pockets[].residues[].min_distance` 是算出来的（换一份结构，数跟着变）
- **多配体结构算出多个口袋**：造一份有两个相距 20 Å 的配体的结构，得到两个口袋，不是一个
- **`is_reference` 在正确的那个簇上**：与 `target.ligand` 同名的簇才标 `true`
- **`pockets[].source` 各是哪一种**：P0 出的是 `"ligand"`；P1 出的是 `"p2rank"`
- **优先级真的生效**（**本步最要紧的一条**）：造一份 P0 有货的样，打桩统计 `p2rank_pockets()`
  **零次调用**——有复合物就不该再去查预测；再造一份 P0 为空的样，断言 P1 被调用且结果标 `"p2rank"`
- **`origin` 三态写对**：`provenance.pockets.origin = "complex" | "predicted" | "none"`，
  三份样各断言一次；且 `predicted` 那份要带「P0 为空」的理由，`none` 那份要区分「查了没有」与「没查成」
- `score` **只在有来源时出现**：`source == "p2rank"` 时是 prankweb 的 `score`（`probability` 一并写）；
  `source == "ligand"` 时**不出现**（没有打分就是没有，不写 0）
- **`pockets[].complexes` 是主结果的来源表（2.3.5）**：P0 的口袋要带齐
  「跨哪几个复合物、各自哪个配体、分辨率多少」，多个来源复合物时全写上（不是只留一个）
- **P0 的 `complexes` 值原样来自下载到的结构**：配体代号、分辨率都是从真文件里读的，**不是编的**
- **取不到就留空**：P0 搜不到/下载不下时 `complexes: []`，**不编条目**
- `contacts` 字段不出现（本轮没有真实位姿）
- `provenance.pockets` 是对象，含 `method`、P0 的 `total`/`used`、P1 的 `p2rank` 三态（`ok`/`absent`/`unavailable`）
- **返回里报清「搜到几个、用了几个」**：`total=69, used=12` 这类数字要在返回值里；
  只报 `used` 会让用户以为这个蛋白只有 12 个复合物
- 登记成 `artifact`，父项是受体
- **三态都不编**：P0 为空且 P1 不可用 → `pockets` 空数组 + 返回里说明是「查了没有」还是「没查成」

### 第 5 步　搜索工具（RCSB + 文献）

产出：`rsearch.py`（`search_pdb`）、`literature.py`（`search_literature`）。**两个都是只读工具，给模型用。**

**这一步的定性很要紧**：它返回的是**候选清单，不是口袋结论**（见 2.3.11）。
工具不筛配体、不归位点，那些判断留给模型。

验收：
- **`search_pdb` 走 RCSB 文本搜**：喂 `"Nav1.7 ProTx-II"` 的打桩响应，断言解出 8 个条目 id
  （`2N9T 5O0U 6N4I 6N4Q 6N4R 6J8I 6J8J 7W9M`）
- **`search_pdb` 走 RCSB 属性搜**：按 UniProt `Q15858` 断言解出 44 个条目（头几个含 `5EK0 6J8G 6J8H`）
- **属性搜的结果可复现**：同一条件跑两遍，条目集合完全一致（这是它与文本搜的分工界限，2.3.11）
- **批量取配体走 GraphQL**：一次问 6 个条目，断言 `8THH → [PCW LPE Y01 IYJ NAG P5S]`、`7XMF → [NAG G2W]`
- **`search_literature` 走 PubMed E-utilities**：喂 `SCN9A Nav1.7 cryo-EM complex` 的打桩，断言拿到 id 列表
  （真样：3 篇）；再验 `SCN9A AND "PDB:6J8I"` 能按 PDB 编号反查（真样：2 篇）
- **返回里带原始 id 与代号，不带结论**：断言返回结构里**没有** `pocket` / `site` / `is_ligand` 这类字段——
  一旦工具开始替模型下结论，「搜索结果当结论」的错就会从这里长出来
- **失败有明确返回**：网络不可达 / 4xx / 5xx → 返回「这次没搜成」并带原因，**不抛异常、不重试到底**
- **搜索不进 `meta.json`**：断言这两个工具的任何调用都**不写** `runs/` 下的文件
- **Plan 下放行**：`PLAN_DENY` 里**不能**有这两个名字（它们是只读）

### 第 6 步　包内服务

产出：`core/web/routes.py` + `core/web/service.py`，`tools/serve_web.py` 改为调用。

验收：
- 四个接口的返回与现有 `tools/check_web_api.py` 的读数一致（**那份判据改指向包内服务**
- 端口避让：占掉 8860 之后起服务，实际端口是 8861
- 服务在后台线程里，主线程停掉之后端口跟着释放
- **`serve_web.py` 与包内服务走同一份路由**：改一处，两边都变（用一个注入验）

### 第 7 步　接线与应用集成

产出：`app.py` 启停服务、`open_viewer`、`all_tools` 装配、`PLAN_DENY`、系统提示、前端 `source` 徽标、
**搜索两件工具的装配与放行**。

验收：
- 应用起来之后服务在监听
- `open_viewer` 返回的 URL 能打开、且指向正确的运行
- Plan 方式下 `write_run` 被拒、`open_viewer` / `search_pdb` / `search_literature` 放行
- **领域工具清单涨到 15 个**（原 11 + `write_run` / `open_viewer` / `search_pdb` / `search_literature`）；
  `tests/test_agent.py` 的 `DOMAIN_TOOL_NAMES` 从 `all_domain_tools()` 现算，**别手抄**
- **前端认 `source`**：`"p2rank"` 显示「预测」、`"ligand"` 显示「实验配体」；
  把 `source` 删掉，徽标消失而页面不报错
- **前端认 `provenance.pockets` 的两种形状**：对象（读 `note`）与字符串（原样显示）
  都能打开；`runs/1iep-demo/`（旧形状）与新一轮产出的目录（新形状）都能打开
- 打包产物里带上 `web/`（查 `build/opencode_tui/Analysis-00.toc`，不是 `strings`）

### 第 8 步　端到端与回归

- 拿三个真目标各走一遍，覆盖三种情况：**P0 有货**（`1iep`，搜到 ABL1 的 69 个复合物→取前 12）、
  **P0 为空但有预测**（`1lyz`，该蛋白无带配体复合物 → 转 P1）、
  **两者都不可用**（AF3 类结构，P1 标 `unavailable`）
- **P0 那一遍要能看出它真去搜了**：返回里报出 `total=69, used=12`，
  且页面上的口袋带 `complexes` 来源表（多个 PDB + 配体 + 分辨率）
- 从 `fetch_structure` / `import_structure` 拿到的结构，一路走到页面里看到口袋与 `source` 徽标
- 复用 `tools/check_web_api.py`、`tools/check_web_ui.js`（改指向包内服务）
- 两层断言 + 禁用字 + `.probe/run_all_checks.sh` 分五段
- 界面有改动 → **重新打包 + 成对抓帧七帧**
- **旧演示目录仍能打开**：`runs/1iep-demo/`（`provenance.pockets` 是字符串、`pockets[]` 无 `source`）
  在改过前端之后照常渲染，这是「降级口径」的回归

## 6. 本轮不做

- **对接（Vina / gnina）与真实打分**：[第 2 轮]。本轮 `molecules[].score` 与 `contacts`
  都不出，页面显示「尚未打分」
- **自己跑 p2rank / fpocket**：依赖外部程序。本轮**只消费 prankweb.cz 已算好的结果**，
  且只在 P0 为空时消费
- **金属离子参与口袋**：本轮把单原子离子与常见缓冲液一起筛掉（2.3.7）。代价是金属酶看不到口袋
- **P0 的自定义筛选**：本轮 P0 的搜索条件固定为「同 UniProt + 有非聚合物实体」，
  不开放「按配体类型/方法/年份筛复合物」这类参数；要筛由模型用 `search_pdb` 自己找线索
- **跨结构的预测一致性统计**：prankweb 是按单个 PDB 条目算的，本轮**就取这个条目的结果**
  （2.3.6），不做「同一蛋白在几十个结构上被预测到同一口袋」的跨条目聚合。
  **P0 的跨复合物合并（2.3.5）不是这件事**——它合的是实验配体，不是预测
- **按文献整理口袋清单**（**部分做**）：那份 Nav1.7 回答里的「Site C / BIG / III–IV 窗口」表
  需要「读了文献、认清位点、再按位点归类」这三步。**搜索结果本身可以拿到**（2.3.11 实测），
  但**机器不下「位点归类」这个结论**——本轮把搜索做成给模型用的工具，归类由模型做；
  **`write_run` 里不放搜索，搜索结果也不进 `meta.json`**
- **搜索结果入档**：本轮搜索只作「线索」给模型看，**不写盘、不进运行目录**。要进页面，
  须由模型走一遍 `write_run` 的口径（筛配体、算残基、标 source）
- **自建结构的口袋预测**：prankweb 有 `POST /v4-user-upload` 可上传自算，本轮**不用**；
  没有 PDB 条目的结构（AF3 预测）P1 直接标不可用（2.3.9）
- **同源结构配体迁移**：超范围
- **多人访问 / 鉴权**：只绑 `127.0.0.1`，与现有服务一致
- **`viz/` 的打包链路**：Mol\* 的产物已经在了，本轮只是把 `web/` 复制进打包产物

## 7. 已定（原「待确认」，2026-10-07 用户授权按默认值拍定）

**这一节原为待确认项，用户已明确「没有确定的部分按你的默认来」。以下 11 条即为实施口径，
执行时直接照做，不再回头讨论。**

1. **`web/` 进包**：`datas` 直接带 `web/` 整个目录（含 3 MB 的 Mol\*）。
   代价是产物大 3 MB，换来「exe 拿到别的机器上也能看结构」——值。
2. **服务端口可配**：应用侧加 `.env` 项 `VSA_WEB_PORT`，默认 8860；命令行 `--port` 仍保留、优先级更高。
3. **`open_viewer` 进 Plan 白名单**：它只读、不改文件，放行。
4. **`CLUSTER_GAP = 4.0 Å`**：两个配体原子相距 4 Å 以内算同一簇。经验值，本期先用；
   实际数据上碰到反例再调，**调它不动算法，只改这一个常量**。
5. **`MONATOMIC_IONS` / `BUFFER_LIKE` 收十来个最常见的**：宁可漏（漏了会把缓冲液当配体、多出一个口袋、
   看得见）也不要错杀（错杀会把真配体丢掉、看不见）。名单放在 `export.py` 顶部，每条后面写理由。
   **按这个下限先收，不追求收全。**
6. **prankweb 缓存放 `~/.opencode_tui/prankweb/<PDBID>.json`**，默认不自动过期
   （p2rank 的预测结果不常变）；另给一个 `/prankweb` 命令看缓存状态与手动清。
7. **超时与同步**：`write_run` 里同步查但设 **15 秒**超时，超时就写 `unavailable` 并降级；
   另给 `/pockets refresh` 命令走长超时慢慢补齐。**不引入后台线程池**。
   P0 那一侧：一次属性搜（秒级）+ 12 次下载（各 0.24 MB），首次约 10~20 秒，之后走缓存。
   界面上的等待提示要能区分这两段。
8. **`radius` 沿用 `POCKET_RADIUS = 5.0`**（与 `build_demo_data.py` 一致），使 P0 与 P1 的口袋
   在页面上视觉尺度一致；**不用** prankweb 的 `surface` 原子集反推半径（那是另一套概念）。
9. **P0 的下载上限与缓存**：**N = 12**，按 2.3.6 排序取前 N，返回里报 `total=69, used=12`；
   **不设按需下载**（口袋要靠这些坐标算出来）。缓存放 `~/.opencode_tui/rcsb/`，按条目 id 存 `.pdb`，
   不自动过期。搜索那一步也缓存：按查询条件哈希存 `~/.opencode_tui/rcsb/search/<哈希>.json`，时效一天。
10. **搜索工具做两个**：`search_pdb`（收 `{"query": "..."}` 或属性条件如 `{"uniprot": "Q15858"}`，
    内部决定走文本搜还是属性搜）与 `search_literature`（收 `{"query": "..."}`）。
    返回条目 id 列表 + 每个条目的配体代号与名称 + 命中总数；文献则是 id + 标题 + 摘要片段。
    **不带任何归类或「是否真配体」的判断**。**不**为「按 UniProt 列结构」单列工具。
11. **搜索结果缓存到磁盘**：`~/.opencode_tui/search/<哈希>.json`，时效一天，理由同 prankweb 缓存。
