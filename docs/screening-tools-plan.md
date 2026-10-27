# 筛选流程的领域工具接入计划（P0–P4）

本文件按「靶点结构 → 参考活性库 → 虚拟筛选 → 共识分层 → ADME/Tox → SAR 迭代」
六段流程，规划领域工具的接入。**先说已核对的事实，再说要加什么**——每一条判断都
建立在下面的读数上，不是凭印象。

## 0. 已核对的事实

### 0.1 现状

| 项 | 读数 | 位置 |
|---|---|---|
| 领域工具 | 12 族 24 个（写这份时）→ 现在 14 族 27 个 | `domain/all_tools.py:49` |
| 口袋算法 | 四层叠合已实现（共晶 → 复合物 → p2rank → 先验叠合），但**只在 `write_run` 里跑** | `pockets.py`、`export.py:1015` |
| 结构格式 | 只取 `.pdb`；大条目只有 mmCIF 时直接判「取不回来」 | `structure.py:26` |
| 自备结构 | `import_structure` 收路径，同源模型 / 突变体 / 局部重建可进 | `structure.py:36-37` |
| 参考活性 | ChEMBL 三个工具；只有 MW/logP/TPSA/QED 这类理化性质 | `chembl.py:1597` |
| 筛选通道 | 只有 `dock`（Vina） | `docking.py:715` |
| 对接规模 | `limit` 默认 50、上限 500，**没有 offset** | `docking.py:64-65`、`docking.py:846` |
| 配体准备 | `limit` 默认 200、上限 2000，同样没有 offset | `ligand.py:662` |
| 对接结果 | 返回只报最佳一条，全表在 `manifest.json` | `docking.py:549-592` |
| 接触计算 | `contacts_for` 已写好，但**只有测试调用，没有生产路径用它** | `export.py:667`、`tests/test_agent.py:6723` |
| 打分方向 | dock 越低越好，affinity 的 pK 越高越好 | `docking.py:721`、`affinity.py:388` |
| 结构检索 | `search_pdb` 与 `complexes.list_complexes` 各写一套 RCSB 属性搜与缓存 | `rsearch.py:227`、`complexes.py:204` |
| Plan 禁止名单 | `{write, patch, bash, prepare_receptor, prepare_ligands, dock, score_affinity, write_run}` | `permission.py:34-37` |

### 0.2 由此得到的三个结论

1. **数据源够了，判断层是空的。** 六段流程里第 4 段（共识分层）与第 6 段（SAR）
   一个工具都没有；第 3 段只有对接一路。
2. **规模有硬天花板。** 导入能到 5 万（`ligand.py:59`），准备封顶 2000，
   对接封顶 500，而且没有 offset——后面两部分永远取不到。真实筛选动辄 10^4 起，
   这不是参数问题。
3. **入口没有对象。** 口袋只在导出时算，`prepare_receptor` 的盒子只有
   「共晶配体 / 显式坐标 / whole」三选一。对接前无法回答「有几个口袋、该打哪个」。

`docs/domain-tools-plan.md` §5 把「重排 / 聚类 / 接触残基 / 配体效率」列为轮 3，
实际只落地了打分（`score_affinity`）。**本文件的 P0–P1 就是那一轮的剩余部分**，
P2–P4 才是新增的 ADME、SAR 与药效团。

### 0.3 落地进度

| 期 | 工具 | 状态 |
|---|---|---|
| P0 | `detect_pockets` / `define_pocket` / `prepare_receptor(pocket=)` | 已完成 2026-10-27，`pocket-tools` 组 30 条 |
| P0 | `rank_candidates` | 已完成 2026-10-27，`rank-candidates` 组 21 条 |
| P0 | `analyze_pose` | 已完成 2026-10-27，`analyze-pose` 组 17 条 |
| P0 | `curate_ligands` | 已完成 2026-10-27，`curate-ligands` 组 24 条 |
| P1 | `screen_similarity` / `screen_shape` | 已完成 2026-10-27，两个组共 26 条 |
| P1 | `validate_docking` / `build_reference_set` | 已完成 2026-10-27，两个组共 30 条 |
| P2 | `offset` / mmCIF / `assess_structure` | 已完成 2026-10-27，三个组共 50 条 |
| P3 | `predict_admet` / 骨架 / 匹配对 / 枚举 / 实验回填 | 已完成 2026-10-27，两个组共 47 条 |
| P4 | `build_pharmacophore` / `screen_pharmacophore` / `align_ligands` | 已完成 2026-10-27，`pharmacophore` 组 33 条 |

落地时与原计划的偏差记在各小节末尾的「实现备注」里——计划写得再细，真正写代码时
总会遇到一两个当时没想清楚的地方，把改动留在原地比事后另写一份说明可靠。

## 1. 两个中间对象

后面所有工具都围着这两个对象转。**先定形状，再加工具**，否则每个模块会各造一份。

### 1.1 口袋对象

不新增 handle 类型，登记成 `artifact`：

```
path: ~/.opencode_tui/pockets/<stem>-<hash8>/
  pockets.json      # 口袋集（detect_pockets 写）
  pocket.json       # 单个口袋（define_pocket 写）
  params.json       # 提交标记，最后写
```

`pockets.json`：

```json
{
  "kind": "pockets",
  "receptor": {"handle": "receptor#1", "fingerprint": "ab12..."},
  "params": {"radius": 5.0, "merge": 6.0},
  "pockets": [
    {
      "index": 1,
      "center": [12.31, 4.52, 8.94],
      "radius": 5.0,
      "residues": [{"chain": "A", "seq": 315, "name": "THR", "min_distance": 3.21}],
      "source": "ligand",
      "support": {"complexes": 4, "pdb_ids": ["1IEP", "2HYY"], "ligands": ["STI"]},
      "box": {"center": [12.31, 4.52, 8.94], "size": [22.0, 18.0, 20.0]}
    }
  ],
  "empty_reason": ""
}
```

`pocket.json`（单个，`meta.kind="pocket"`）只保留被选中的那一处，外加
`source`（`ligand` / `residues` / `explicit` / `prior`）与 `receptor`。

**为什么是 artifact 而不是新 handle 类型**：`ledger.py` 只有 receptor / ligandset /
artifact 三类，`__init__.py` 明写「不要再细分——类型一旦膨胀，模型的判断负担就跟着涨」。
口袋与候选表都是「算出来的东西」，artifact 正好是这一类。

### 1.2 候选表

所有筛选通道写同一张表，`rank_candidates` 只合并不重算：

```
path: ~/.opencode_tui/candidates/<stem>-<hash8>/
  candidates.json
  candidates.csv
  params.json
```

```json
{
  "kind": "candidates",
  "sources": ["artifact#4", "artifact#5"],
  "tier_method": "percentile",
  "rows": [
    {
      "id": "mol-7",
      "name": "cmpd_7",
      "smiles": "CC(=O)...",
      "inchikey": "AAAA-BBBB-CCCC-DDDD",
      "scores": {"dock": -9.2, "affinity": 8.1, "similarity": 0.62},
      "ranks": {"dock": 1, "affinity": 3, "similarity": 12},
      "consensus_rank": 2,
      "tier": "high",
      "gates": {"redock_rmsd": 0.82, "pains": false},
      "sources": ["artifact#4", "artifact#5"]
    }
  ]
}
```

**对齐键**：先 `id`，对不上再用 canonical SMILES 算 InChIKey。两条都对不上时
该行保留、记一条 warning，**不按行号硬拼**——行号在两个产物里本来就不是一回事。

### 1.3 产物形状的兼容约束

三处必须对齐，否则会出现「工具写出来了，下一步读不了」：

1. **ligandset 类产物**（`curate_ligands` / `build_reference_set` / `enumerate_analogs`）
   必须与 `import_ligandset` 同形：目录里 `manifest.json` + `ligands.csv`，CSV 列名
   `id` / `name` / `smiles` / `input_smiles` / `heavy_atoms` / `source_index`，
   `stage` 不能是 `prepared`。`prepare_ligands` 的 `_source_cleaned`
   （`ligand.py:575-625`）按这个形状读，形状不对会在下一步报「配体集目录不完整」。
2. **候选表**必须带 `id` 与 `smiles`：`rank_candidates` 靠它们对齐，
   `analyze_pose` 靠 `id` 找位姿。
3. **口袋**必须带 `center` / `size`：`prepare_receptor` 只读这两个字段，不重算。

## 2. 分期与依赖

```
P0 闭环      detect_pockets → define_pocket → prepare_receptor(pocket=)
             rank_candidates + analyze_pose + curate_ligands
                │
P1 多路证据   screen_similarity / screen_shape / validate_docking / build_reference_set
                │        （共识打分至少要有两路独立证据才成立）
P2 规模覆盖   dock/prepare_ligands 的 offset、mmCIF 回退、assess_structure
                │
P3 扩展       predict_admet（规则层）、scaffold_analysis、matched_pairs、
             enumerate_analogs、record_assay
                │
P4 药效团     build_pharmacophore / screen_pharmacophore（技术风险最高，放最后）
```

**P0 先做的理由**：它把「跑完一次对接」变成「一张有排序、有接触依据的候选表」。
现在 `dock` 只回一条最佳，模型要看全表只能拿路径去 `read`——那正是
「收 handle 不收路径」要挡的动作。

---

## 3. P0：闭环

### 3.1 `detect_pockets`（族 `pocket`）

**做什么**：把四层口袋来源装配成一个可查的口袋集，登记成 artifact。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `receptor` | string | 必填 | 受体 handle（raw 或 prepared 都收） |
| `radius` | number | 5.0 | 口袋残基判据，3–8 Å |
| `sources` | string | `auto` | `auto` / `ligand` / `complex` / `prior` / `predicted` |
| `uniprot` | string | 空 | P0 复合物检索用；不给则只看手上这一份结构 |
| `candidates` | string | 空 | 先验清单，逗号分隔的 PDB 编号 |
| `limit` | integer | 5 | 最多列几处，上限 20 |

类属性：`primary_argument="receptor"`、`context_argument="ledger"`。

**登记形状**

| 项 | 值 |
|---|---|
| kind | `artifact` |
| path | `~/.opencode_tui/pockets/<stem>-<hash8>/` |
| summary | `口袋集 · 3 处 · 主来源 复合物` |
| fingerprint | `fingerprint_path(目录)` |
| parents | `[receptor]` |
| meta | `{"kind": "pockets", "count": 3, "sources": {...}, "params": {...}}` |

**返回**：前 `limit` 处，每行「序号 · 中心 · 残基数 · 来源 · 支持复合物数 · 盒子边长」；
末尾写明总数与「还有几处未显示」，并给出下一步
`define_pocket(pockets=..., index=...)`。空集时写 `empty_reason`，**不编中心**。

**断言**

1. 同受体同参数第二次调用复用同一产物目录，且假传输计数为 0。
2. schema 里没有 `center` / `size` 这类参数——中心一律现算。
3. 无共晶配体、无复合物、无预测时返回空集与原因，`failed=False`。
4. 每处口袋带 `source` 与 `support.complexes`，数值与 fixtures 对得上。
5. 排序稳定：来源优先级 → support 降序 → 残基数降序。
6. 未注入 ledger 时返回「内核装配问题」，不是「参数错误」。

### 3.2 `define_pocket`（族 `pocket`）

**做什么**：从口袋集里选一处，或用残基 / 显式坐标定义一处，登记成单个口袋。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `pockets` | string | 空 | `detect_pockets` 的产物 handle |
| `index` | integer | 空 | 口袋集里的序号 |
| `residues` | string | 空 | `A:315,A:319,A:330`（链:序号） |
| `center` | string | 空 | `12.3,4.5,8.9`，与 `size` 成对 |
| `size` | string | 空 | `22,18,20` |
| `padding` | number | 8.0 | 由残基算盒子时四周外扩，2–12 Å |
| `name` | string | 空 | 显示名 |

三路互斥：`pockets+index` / `residues` / `center+size`，同时给两路直接报错。

**登记形状**：`artifact`，`meta.kind="pocket"`，`parents=[receptor]`，
`meta` 里记 `center` / `size` / `residues` 条数 / `source`。

**返回**：handle + 中心 + 边长 + 残基数 + 来源。

**断言**

1. 三路互斥，同时给两路时错误里点名是哪两路。
2. `residues` 里有不存在的残基 → 列出该链实际的残基范围，不静默丢。
3. 盒子由残基坐标 + padding 现算；显式给坐标时 `source="explicit"`。
4. 同内容（指纹相同）不重复登记。
5. 只给 `index` 不给 `pockets` → 报错并提示先 `detect_pockets`。

**实现备注（2026-10-27）**

- `sources=auto` 取**第一个非空的层**就停，不叠加：一份自带共晶配体的结构不该因为
  顺带能联网就多打两次请求。要叠就显式写 `ligand,complex`。
- `prepare_receptor` 收到 `pocket` 而没收到 `ligand` 时，`ligand` 按 `NONE` 处理。
  盒子已经由口袋定死，「哪个杂原子组是配体」不再影响结果，但 1IEP 这种带 STI +
  4 个 Cl 的结构会卡在「没法自动认定哪个是配体」上——那是个假阻塞。
- 口袋与受体的血缘按「同一条或互为祖先」判：在 prepared 受体上定义的口袋，换成
  raw 受体重新准备也认，否则改一次准备参数就得重选口袋。

### 3.3 `prepare_receptor` 加 `pocket` 参数（改现有工具）

新增 `pocket`（string，可选）：给了就从口袋读盒子。

**与现有参数的关系**：`pocket` 与 `box_center` / `box_size` 互斥；`pocket` 与 `ligand`
**可以共存**——`ligand` 仍负责把共晶配体从受体里抽走，盒子以 `pocket` 为准。
返回里要写明这次盒子的来源。

**断言**

1. `pocket` 与 `box_center` 同时给 → 报错二选一。
2. `box.txt` 与返回里的 `box_source` 都来自口袋，不是重新算的。
3. 口袋的父受体与本次 `structure` 不是同一条 → 拒绝，并说明两条各是什么。
4. 不给 `pocket` 时行为与现在逐字节一致（回归断言）。

### 3.4 `rank_candidates`（族 `analysis`）

**做什么**：把多路分数合并成一张候选表，按共识排名，按分位数分档。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `sources` | string | 必填 | 逗号分隔的 artifact handle，如 `artifact#4,artifact#5` |
| `tiers` | string | `percentile` | `percentile` / `reference` |
| `reference` | string | 空 | 参考集 ligandset；`tiers=reference` 时必填 |
| `cutoffs` | string | `5,20` | 两档分位点（百分位），第一个是 high，第二个是 medium |
| `top` | integer | 20 | 返回里列几行，1–200 |
| `name` | string | 空 | 显示名 |

**方向不交给模型**：dock 升序、affinity 降序、similarity 降序、LE 降序，由
来源的 `meta.kind` 决定；未知来源直接拒绝，不猜方向。

**合并方式**：每路先转成秩，再取平均秩（Borda）。**不做 z-score**——来源少于三路时
标准差没有意义，z-score 会把两路的分差放大成假信号。

**登记形状**：`artifact`，`meta.kind="candidates"`，`parents` = 各来源 +
参考集；`meta` 记 `sources` / `tier_method` / `counts`（四档各多少）/ `alignment_warnings`。

**返回**：top-N 表（id、名称、各分、共识秩、档位、gate 失败原因），
加一行「共 N 条，四档分布 high/medium/low/below_cutoff = a/b/c/d」。

**断言**

1. 方向统一：dock 分更低者秩更前，affinity 分更高者秩更前。
2. 缺一路的分子保留，该列 `null`，**不补 0**。
3. 对齐：先 id、后 InChIKey；对不上的行数写进 `alignment_warnings`。
4. 四档之和等于行数（边界 `>=` 归入高档）。
5. `tiers=reference` 但没给参考集 → 报错并说明怎么建。
6. 只有一路来源时仍出表，但文案写明「单路分位数，不是共识」。
7. 分位数由代码算，schema 里没有「每档阈值」这种可让模型填的数。

**实现备注（2026-10-27）**

- **`tiers=reference` 这一版没做**，参数表里没有 `reference`，`tiers` 也不收：
  参考集是「没有分数的配体集」，拿它分档要么得先给它打分（那是另一条链），
  要么得把「活性阈值」当成外部输入——两条都不是 P0 该顺手做的。现在只做
  `percentile` 一种，认不出的 `tiers` 值直接拒绝，不静默退回默认。
- **分档按名次，不按分数百分位**：`rank / n * 100` 在 20 条的表上会把 top 5%
  算成 0.25 个位置，于是一条 high 都没有。改成「前 ceil(n·5%) 名是 high，至少 1 名」，
  20 条的表就是 1 high / 3 medium / 16 low。
- 能当来源的只有 `meta.kind` 为 `docking` / `affinity` 的产物，其余直接拒绝。
  方向表 `SOURCE_KINDS` 是**唯一**决定升降序的地方，`P1` 的相似度通道加进来时
  在这里补一行，而不是让模型传 `ascending=true`。
- 缺一路的分子按已有那几路取平均，平局时覆盖路数多的在前。**不补 0**：
  0 比任何真实秩都靠前，补 0 等于把「只出现在一路里」的分子推到第一。

### 3.5 `analyze_pose`（族 `analysis`）

**做什么**：给定对接产物与分子，算接触残基、氢键、冲突。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `docking` | string | 必填 | 对接产物 handle |
| `molecule` | string | 必填 | 分子 id（不是序号，也不是路径） |
| `cutoff` | number | 4.0 | 接触判据，2.5–6.0 Å |
| `hbond` | boolean | true | 是否算氢键 |

**只读，不登记**。声明 `context_argument="ledger"` 是因为要沿血缘取受体坐标——
这是纪律 2 的两处例外之一（另一处是 `assess_structure`）。理由写进模块注释：
它读登记表但不写，模型不会因此以为手上多了一个 handle。

**返回**：接触残基表（残基、最短距离、接触原子数）、氢键（供体-受体对、距离、角度）、
冲突（< 2.0 Å 的原子对，单列）。首行给一句结论，例如「12 个残基接触，3 个氢键，无冲突」。

**断言**

1. 接触距离 ≤ cutoff，按最短距离升序。
2. 氢键判据写死：供体-受体 ≤ 3.5 Å 且角度 ≥ 120°；返回里写明口径。
3. 冲突 < 2.0 Å 单独列出；有冲突时首行提示。
4. 分子不存在 → 列出可用 id（最多 20 个），不取第一个。
5. `pose_pdb` 缺失 → 说「这条没有转出 PDB」，不是「文件不存在」。
6. 调用前后 ledger 条目数不变。

**实现备注（2026-10-27）**

- **氢键角度用重原子近似**：结构里通常没有氢（Vina 的输入常常只留极性氢），
  `D–H···A` 的角度算不出来。改成优先找共价氢（≤ 1.3 Å），找不到就取供体最近的
  重原子 `D'`，用 `D'–D···A` 代替——氢在 `D'` 的反方向，两者只差几度。返回里对
  近似的那几条写明「角度按重原子近似」，不混在真值里。
- 受体坐标沿 `manifest.receptor.handle` → 登记表 → `export._receptor_coordinate`
  取。这是**唯一**让只读工具挂 `context_argument` 的理由：坐标只在登记表里，
  而产物目录里存的是相对路径。
- `cutoff` 只影响接触与冲突，不影响氢键（氢键有自己的 3.5 Å）。所以把 `cutoff`
  收到 2.5 Å 时仍可能报出氢键——这是对的，返回里的「口径」一行写明了两个判据。

### 3.6 `curate_ligands`（族 `ligand`）

**做什么**：待筛库的第一道闸——PAINS/Brenk、去重、性质窗口。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ligands` | string | 必填 | 原始 ligandset handle |
| `max_mw` / `min_mw` | number | 空 | 分子量窗口 |
| `max_logp` / `max_tpsa` | number | 空 | 理化窗口 |
| `max_hbd` / `max_hba` / `max_rotb` | integer | 空 | 氢键与柔性窗口 |
| `pains` | boolean | true | PAINS_A/B/C |
| `brenk` | boolean | true | Brenk 不稳定/反应性片段 |
| `dedupe` | string | `inchikey` | `none` / `canonical` / `inchikey` / `scaffold` |
| `mode` | string | `drop` | `drop` 真删；`flag` 只标注 |
| `name` | string | 空 | 显示名 |

**登记形状**：`ligandset`，`meta.stage="curated"`，`parents=[源配体集]`，
`meta` 记 `kept` / `dropped`（按规则分组）/ `params`。

**返回**：保留数、淘汰数、按规则分组的计数、前 5 条淘汰例子（分子 + 命中规则）。

**断言**

1. PAINS 用 `FilterCatalog`，按 catalog 名分组计数。
2. 去重键 `inchikey` 用完整键，`scaffold` 用前 14 位，两条各验一次。
3. 窗口闭区间：等于上限的保留。
4. `mode=flag` 时不删，输出全集 + 标记列。
5. 全部淘汰 → `failed=True`，写清每类规则淘汰了多少。
6. 源 ligandset 的文件字节不变。

**实现备注（2026-10-27）**

- `import_ligandset` 在导入时已经按 canonical SMILES 去过一次重，所以 `curate_ligands`
  的去重只在两种源上才起作用：ChEMBL 那种直接落 CSV 的条目，以及手工整理的目录。
  测试因此手写目录而不是走导入——否则「去重」这一条永远绿，测不到东西。
- `scaffold` 用 **InChIKey 前 14 位**（骨架段），不是 Murcko 骨架：后者会把
  「苯环 + 不同取代基」算成两个骨架，而那正是要去掉的那类重复。代价是一对对映体
  会被并成一条——对筛选来说这是想要的（同一骨架先留一个代表）。
- `mode=flag` 时 `flags` 列写进 `ligands.csv`。`prepare_ligands` 的 `_source_cleaned`
  只读 `id/name/smiles/input_smiles/heavy_atoms/source_index`，多一列不影响它，
  所以筛过的产物可以直接接准备。
- 每条只按**命中的第一条规则**计入 `dropped`（一行可能同时命中 PAINS 与 Brenk），
  否则各类计数之和会大于淘汰数。每一行的全部命中写在示例的 `flags` 里。

---

## 4. P1：多路证据

### 4.1 `screen_similarity`（族 `screen`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | 二选一 | 参考集 ligandset handle |
| `smiles` | string | 二选一 | 单个查询分子 |
| `library` | string | 必填 | 待筛 ligandset |
| `metric` | string | `ecfp4` | `ecfp4` / `maccs` / `atompair` |
| `smarts` | string | 空 | 给了就只做子结构检索 |
| `cutoff` | number | 0.0 | 相似度下限，0–1 |
| `top` | integer | 100 | 最多返回多少条 |

**登记形状**：`artifact`，`meta.kind="candidates"`，`parents` = 查询集 + 待筛库，
`meta` 记 `metric` / `fingerprint` / `cutoff` / `counts`；分数列 `similarity`。

**断言**

1. Tanimoto 由 RDKit 算；manifest 记指纹类型、半径、位数。
2. 换 `metric` 不复用旧产物（参数进 `fingerprint_parts`）。
3. 非法 SMARTS → 报「哪一段解析不了」，不抛到外层。
4. 参考集为空 / 全无效 → 失败并说明。
5. top 截断写明总数与「调大 top」。

**实现备注（2026-10-27）**

- 多个查询分子时取**最近邻**（max Tanimoto），不是平均：问的是「像不像已知活性里的
  某一个」，平均下来一个跟所有人都不像的分子会被算成中等，与直觉相反。
- `top` 也进缓存键：`top=100` 与 `top=50` 是两份不同内容，共用目录会让后一次静默
  读到前一次的条数。
- SMARTS 模式下分数恒为 1，列名仍是 `similarity`。返回里写明了，但这条容易误读——
  它只回答「匹配 / 不匹配」，`cutoff` 在这条路上没有意义。

### 4.2 `screen_shape`（族 `screen`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | 必填 | 已备 3D 的 ligandset，或受体的共晶配体 |
| `library` | string | 必填 | 已备 3D 的 ligandset |
| `metric` | string | `shape_tanimoto` | `shape_tanimoto` / `overlap` |
| `top` | integer | 100 | 最多返回多少条 |

**登记形状**：`artifact`，`meta.kind="candidates"`，`parents` = 查询集 + 待筛库，
`meta` 记 `metric` / `counts` / `alignment_failures`；分数列 `shape`。

**断言**

1. 3D 缺失 → 明确提示先 `prepare_ligands`，不静默跳过。
2. 形状分与 2D 分字段分开，不混进同一列。
3. 对齐失败（原子数/元素不匹配）逐条记录。
4. 单批上限（2000）超出时说明还有多少未处理。

**实现备注（2026-10-27）**

- **算法换成了 USR**，不是计划里的体积重叠。体积重叠要先把两个分子叠到一起，
  叠合本身要找最大公共子结构，几千个分子就是分钟级；USR 只看 12 维距离分布，
  **对齐无关**、一个分子几微秒。代价是它分得清「摊开还是团着」、分不清细节——
  所以它是粗筛，精细排序仍靠 dock 与 2D。
- `metric` 收 `shape_tanimoto` 与 `overlap` 两个值，但**两种走同一个实现**：留第二个
  只是为了不让按计划写参数的调用直接报错，返回与产物里都写明用的是 USR。
- 坐标从 PDBQT 读、不从 SMILES 重新生成：准备产物里现成的就是 PDBQT，重生成等于
  再跑一次 ETKDG，同一份输入可能得到不同构象。**元素列要按原子类型读**——PDBQT 在
  77–78 列放的是 `HD` / `OA` / `A` 这类类型，按元素读会把 `HD` 当成重原子，于是
  「加没加氢」会改变形状分（测试里用一对「只差一个 HD」的分子守这一条）。
- 查询可以是准备过的受体（用它的共晶配体）：这条路径不经过 `prepare_ligands`，
  所以单独读 `ligand.pdb`。

### 4.3 `validate_docking`（族 `analysis`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `receptor` | string | 必填 | prepared 受体（含 `ligand.pdb`） |
| `docking` | string | 必填 | 对接产物 |
| `cutoff` | number | 2.0 | 通过阈值，0.5–5.0 Å |

**登记形状**：`artifact`，`meta.kind="validation"`，`parents=[receptor, docking]`，
`meta` 记每个分子的 RMSD 与 pass。

**断言**

1. 共晶配体取自受体产物目录的 `ligand.pdb`；不存在时明说「没有共晶配体，
   无法做重对接」，**不假装通过**。
2. RMSD 用 RDKit `GetBestRMS`，只算重原子；口径写进 manifest。
3. `pass = rmsd <= cutoff`。
4. 结果能被 `rank_candidates` 当 gate 读（`gates.redock_rmsd`）。

**实现备注（2026-10-27）**

- **用原地 RMSD，不用 `GetBestRMS`**：后者先做刚体叠合再算 RMSD，于是「整体平移
  2.5 Å」会被算成 0——而那正是重对接要否掉的情况。实测：同一对位姿，`GetBestRMS`
  给 0.0，本实现给 2.5。这里的实现是对称等价映射取最小、只配对不旋转平移。
- 工具会核对「对接产物记的受体 handle」与传进来的受体是否一致，不一致直接拒绝：
  拿另一份受体的共晶配体当参照，算出来的数看着正常但没有任何意义。
- `gates.redock_rmsd` 不是候选表自己去读验证产物，而是 `rank_candidates` 在展开
  docking 来源时**反查同会话里 `meta.docking` 指向它的 validation 条目**。这样
  验证是一次性的协议体检，不需要让用户把它也塞进 `sources`；只有真的是共晶配体
  本人的那条会带上这个门禁。
- 原子数不同 / 连接关系对不上时逐条记 `detail`，`rmsd` 留空——不给一个假的数。

### 4.4 `build_reference_set`（族 `ligand`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `actives` | string | 必填 | `search_actives` 的产物 |
| `min_pchembl` | number | 6.0 | 活性下限 |
| `max` | integer | 500 | 最多保留多少条 |
| `pains` | boolean | true | 是否标注 PAINS |
| `name` | string | 空 | 显示名 |

**登记形状**：`ligandset`，`meta.stage="reference"`，`meta.label="active"`，
产物按 §1.3 的形状写（`manifest.json` + `ligands.csv`）。
`actives` 既可以是 `search_actives` 直接落的 CSV（`entry.path` 是文件），
也可以是 `import_ligandset` 的目录——与 `prepare_ligands` 的入口口径一致。

**断言**

1. 同一分子多条活性取最强 pChEMBL。
2. PAINS **只标不删**——被 PAINS 命中的已知活性是真的活性，删掉会把参考分布打歪。
3. 少于 10 条时 `meta.too_small=True`，`rank_candidates` 用它会警告。

**实现备注（2026-10-27）**

- **不经过 `load_ligand_file`**：那个函数按 canonical SMILES 去重是「先到先得」，
  而这里要「同一分子取最强那条」。先去重再想取最大值已经晚了，所以自己读一遍 CSV
  （复用 `_read_csv` 的列名猜测，不用它的去重）。
- `too_small` 只是标记，不拦：3 条的参考集照样能拿去筛，只是分档会不稳，返回里
  明说了这一点。
- 产物同时写 `flags` 列（PAINS 命中），与 `curate_ligands` 同一套列，`prepare_ligands`
  两边都读得动。

---

## 5. P2：规模与结构覆盖

### 5.1 `dock` / `prepare_ligands` 加 `offset`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `offset` | integer | 0 | 从第几条开始取，上限与 `limit` 同量级 |

`selected = records[offset : offset + limit]`，`remaining = 总数 - offset - len(selected)`。
`offset` 进缓存参数——不同批次是不同的产物，不能复用。

**断言**

1. 两批（0–499、500–999）合起来覆盖 1000 条，无重叠、无遗漏。
2. `offset` 越界返回空并说明「只有 N 条」。
3. 换 `offset` 不复用旧产物目录。
4. 旧调用（不给 offset）行为不变。

**实现备注（2026-10-27）**

- **`offset` 的上限取 50000（导入上限），不是「与 limit 同量级」。** 计划里那句是照
  「防止乱填」写的，但真按 500 封顶就会得到一个荒谬的结果：库能装 5 万条，而第 501
  条之后的分子永远取不到——那正是这次要修的问题。`limit ⊆ 2000` 说的是「一批多大」，
  `offset` 说的是「库有多深」，两者不该绑在一起。
- `remaining` 的含义跟着改了：现在是「这一批之后还剩多少」（`总数 - offset - 本批`），
  否则分批跑时每一批都会说「还剩 N」而 N 一直不变。返回里的截断提示也从「调大 limit」
  改成「接着处理用 `offset=N`」，两个选项都留给用户。
- 不给 `offset` 与 `offset=0` 是同一组参数，会命中同一份产物——这条是回归断言的写法。

### 5.2 `fetch_structure` 的 mmCIF 回退

`.pdb` 404 且 `.cif` 200 时：装了 `gemmi` 就用它读 mmCIF → 写 PDB → 走原有解析与登记；
没装时保持现在的两档错误（编号不存在 / 结构太大）。

**断言**

1. 无 gemmi 时错误文案与现在逐字一致（回归）。
2. `gemmi` **只能在函数体里导入**（纪律 8），顶层 import 会被 `check_chemistry_gate` 抓住。
3. 有 gemmi 时 mmCIF 能登记成 receptor，`meta.source_format="mmCIF"`。
4. 转换失败不登记、不写半截文件。
5. 冷冻电镜条目的方法字段能读出来。

**实现备注（2026-10-27）**

- 转换成功的标记**写在 PDB 正文的第一行**（`REMARK 999 本文件由 RCSB mmCIF 转换而来`），
  不另存 sidecar：缓存只按 `<编号>.pdb` 一份文件存放，多一个旁挂文件就多一种「两者
  不同步」的状态。读盘时 `_source_format` 读的就是这一行，所以刷新会话之后
  `meta.source_format` 仍是 `mmCIF`。
- 没装 gemmi 的路径靠 `_cif_to_pdb` 抛 `ImportError` 区分，错误文案与改动前**逐字一致**
  （测试里就是拿一条假的 `ImportError` 守这一条）。
- 转换失败（gemmi 装了就可能是坏 mmCIF）走第三档错：说清是转换失败、建议用
  `import_structure` 喂一份转好的 PDB。**失败不写盘、不登记**——半截 PDB 会被下次
  调用当成有效缓存读进来，那是最难查的一种。
- `import_structure` 仍然拒收 mmCIF：远端那条能转是因为文件是我们自己下的、格式可信；
  用户手里的 `.cif` 可能是任意变体，静默转换失败比当场拒绝更糟。

### 5.3 `assess_structure`（族 `structure`）

**做什么**：结构质量与适用性——方法、分辨率、链、缺失残基、altloc、杂原子、金属。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `structure` | string | 必填 | 受体 handle |

纯查询，不登记。与 `analyze_pose` 同理声明 `context_argument="ledger"` 以取坐标路径。

**断言**

1. 缺失残基 = SEQRES 与 ATOM 的差集，按链列出。
2. NMR 多模型只统计第一套。
3. 没有 SEQRES → 写「无法判断缺失」，不报 0。

**实现备注（2026-10-27）**

- **不另写解析，直接复用 `parse_pdb` 的 `StructureFacts`**：方法、分辨率、SEQRES/ATOM
  缺口、交替构象、配体、水、模型套数全在里面，另写一份只会让「缺失残基怎么算」在
  两个地方各有一套定义。工具做的事是把这些事实**排成一段能直接转述的话**，外加金属。
- 缺口沿用 `ChainFacts.missing` 的三态：`None`（没有 SEQRES，无从比较）/ `0`（声明与
  观测一致）/ 正数。摘要里三种写法都不同，「没声明」被写成 0 会让用户以为结构完整。
- 金属从 HETATM 的**元素列**判，判出来之后要从「共晶配体候选」里剔除：锌离子也走
  HETATM，把它列成配体会让人去准备一个离子。
- 坐标路径不在这里重写：函数体里 import `export._receptor_coordinate`（`export` 顶层
  import 了本模块，顶层反向 import 会成环）。
4. 电镜条目没给分辨率 → 写「未给出」，不写 0。

---

## 6. P3：ADME 与 SAR

### 6.1 `predict_admet`（族 `admet`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ligands` | string | 必填 | ligandset |
| `endpoints` | string | `solubility,veber,bbb` | 逗号分隔；`cyp` 只做警示子结构 |
| `name` | string | 空 | 显示名 |

**登记形状**：`ligandset`，`meta.stage="admet"`，`parents=[源配体集]`，
`meta` 记 `endpoints` / `unsupported` / `formula_version`。

**只做规则层**：ESOL 溶解度、Veber 规则、BBB 启发式、CYP 警示 SMARTS。
每列都带 `source="rule"`，manifest 记公式与系数。

**hPPB 与 CYP 抑制明确标 `unsupported`**——规则层给不出可信值，宁可说没有。
模型层（ADMET-AI 这类）将来像 DTA 一样走子进程 + 独立环境，不进主仓依赖。

**断言**

1. 每列都有 `source`，且只允许 `rule` / `unsupported`。
2. 输入缺 logP/MW 时该行留空，不猜。
3. 全部端点都不支持 → 失败，不是空表。
4. 顶层不 import rdkit（`check_chemistry_gate` 覆盖）。

**实现备注（2026-10-27）**

- ESOL 用 Delaney 2002 的系数，`AP` 是「芳香原子 / 重原子」。**分母必须是重原子**：
  用原子总数会让「加不加氢」改变溶解度，而溶解度跟氢原子上不上坐标没有关系。
- BBB 是四条件启发式（TPSA<90、HBD≤3、MW<450、logP 1–3），输出只有
  「可能 / 边界 / 不太可能」三档，**每一档都带一句依据**（被哪一项挡下的）。给一个
  「0.62 的透过概率」既没法追问也没法复核，所以不给。
- `hppb` / `cyp_inhibition` 写在 manifest 的 `unsupported` 里，同时进 `columns` 并标
  `source="unsupported"`。传这两个端点进 `endpoints` 会直接拒绝（不会静默忽略）。
- 端点列与配体集的列**并在一张 CSV 里**，id/SMILES 原样保留，所以产物能直接喂给
  `prepare_ligands`，也还能被 `curate_ligands` 再筛一遍。

### 6.2 `scaffold_analysis`（族 `sar`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `source` | string | 必填 | 候选表或 ligandset |
| `level` | string | `murcko` | `murcko` / `generic` |
| `min_size` | integer | 2 | 小于它的骨架不单列 |
| `top` | integer | 20 | 最多列几组 |

**登记形状**：`artifact`，`meta.kind="scaffolds"`，组内成员与代表分子（分数最高者）。

**断言**：同骨架分组正确；无环分子归 `acyclic`；按组大小降序；代表分子取组内最高分。

**实现备注（2026-10-27）**

- 代表分子的「最高分」按**来源的分数方向**取：dock 是越低越好，直接取最小值会是错的。
  候选表没有 `score_column`（也就是 `rank_candidates` 合并出来的那张）时退回
  `consensus_rank`，越低越好。
- `generic` 走 `MakeScaffoldGeneric`，把杂原子抹成碳——看的是「环怎么连」，不是
  「环里有几个氮」。跨化学型的骨架比较用它，同系列的 SAR 用默认的 `murcko`。
- 小于 `min_size` 的组不单列，但成员数计入 `small`：一个骨架只有 1 个分子说明不了
  什么，可它确实占着库里的位置，静默丢掉会让「分成了几组」对不上总数。

### 6.3 `matched_pairs`（族 `sar`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `source` | string | 必填 | 带分数的候选表 |
| `max_cut` | integer | 10 | 单侧最多切几个重原子 |
| `min_delta` | number | 0.5 | 分数变化下限 |
| `top` | integer | 50 | 最多列几对 |

**登记形状**：`artifact`，`meta.kind="mmp"`。

**断言**：只配对单侧切；delta 方向按分数方向；同一对去重；无配对时返回空并说明。

**实现备注（2026-10-27）**

- **不用 `rdMMPA.FragmentMol`**：它按「最多切几刀」组织结果，`maxCuts=1` 时返回的是
  `('', '左手[*:1].右手[*:1]')`——核心是空的、两半都塞在 chains 里，还得再猜哪半是哪半。
  这里改成自己切非环单键：切开之后**较大的那半是核心、较小的是变体**，与匹配分子对的
  定义一致。
- `max_cut` 是**变体的重原子数上限**（计划原话是「单侧最多切几个重原子」）。一个只剩
  两三个原子的取代基换来换去说明不了 SAR，而它会让对子数量爆炸。
- delta 的方向第一次写反了（`higher` 与 `lower` 两个分支减反了），测试里「dock 越低
  越好，c1 是更优侧」当场变红。两条方向写反的后果一样：把差的说成更优，结论整个倒过来。

### 6.4 `enumerate_analogs`（族 `sar`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `scaffold` | string | 必填 | `scaffold_analysis` 的产物 + 组号 |
| `rgroups` | string | 必填 | R 基来源（同一产物里的替换片段） |
| `max` | integer | 200 | 最多生成多少 |
| 性质窗口 | — | 空 | 与 `curate_ligands` 同名同义 |
| `name` | string | 空 | 显示名 |

**登记形状**：`ligandset`，`meta.stage="raw"`，可直接进 `prepare_ligands`。

**断言**：SMILES 合法才保留；去重；窗口过滤；数量上限；非法组合跳过并计数。

**实现备注（2026-10-27）**

- **组合靠手工接键，不用 `ReplaceSubstructs`**：后者要求替换片段本身能匹配上，而 R 基
  常常只有两三个原子，换个写法就匹配不到。改成 `RWMol`：删掉两段的虚原子、在它们的
  邻原子之间连一根单键、再 sanitize——不成立就返回 None 计入跳过。
- **组内原有的分子不算「新类似物」**：把输入原样吐回来只会挤占名额。生成池一开始就装进
  这些分子的 canonical SMILES，撞上就跳过。
- 产物落在 `ligands/analogs/`，与 `import_ligandset` 同一个父目录，便于一起清理。

### 6.5 `record_assay`（族 `sar`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file` | string | 必填 | CSV 路径（**第四个收路径的动作**，理由见下） |
| `campaign` | string | 必填 | 项目名，写进 `runs/<campaign>/` |
| `assay` | string | 必填 | 测定类型，如 `IC50` |

**为什么破例收路径**：实验数据来自用户的文件，模型编不出来源；这是四个动作里
唯一一个「数据由用户提供、不是模型拼的」。其余仍一律收 handle。

**登记形状**：`artifact`，`meta.kind="assay"`，落 `runs/<campaign>/assay.csv`。

**断言**：数值或限定符（`>10000`、`<1`）才收；重复按 (molecule, assay) 保留最新并留历史；
原子写；不覆盖别的 campaign。

**实现备注（2026-10-27）**

- `campaign` 会当目录名，所以先校验（只允许字母数字与 `. _ -`）：传 `../etc` 不是
  「参数写得怪」，是往仓库外面写文件。
- 数值与限定符**分两列存**：`>10000` 是一个右删失观测，把它写成 10000 会让下游把它当
  精确值算均值。历史行原样进 `assay-history.csv`，主表只留最新。
- 主表用原子写；历史是追加（追加失败最多丢一条历史，不会毁掉主表）。

---

## 7. P4：药效团（技术风险最高）

- `build_pharmacophore`：从对齐后的参考活性提取共识特征（供体/受体/芳香/疏水），
  登记 `artifact`，`meta.kind="pharmacophore"`，`parents=[参考集]`，
  `meta` 记 `features`（类型计数）/ `n_actives` / `align_rmsd`。
- `screen_pharmacophore`：对已备 3D 的库做匹配，写候选表，分数列 `pharmacophore`；
  登记形状与 `screen_shape` 相同。

**先决条件**：参考集至少 5 条、且已完成 3D 与叠合。不满足时明确拒绝，
不拿单分子特征冒充共识药效团。

**实现备注（2026-10-27）**

- **叠合先做进了 `build_pharmacophore`，之后拆成了独立工具 `align_ligands`。**
  第一版图省事，把「嵌构象 + MCS 叠合」塞在药效团构建里——结果是同一个输入换个
  `min_support` 就重跑一遍叠合，而且叠合结果没法复用。现在 `align_ligands` 出
  `stage=aligned` 的产物（`aligned.sdf` + `ligands.csv`，CSV 里带 `align_rmsd` 列），
  `build_pharmacophore` 认这个阶段就直接读坐标、不再重算。叠不上的**逐条列出原因**
  （"公共骨架只有 3 个原子"）；另一条路仍是直接给 `docking` 产物的位姿。
- **筛选不做叠合**：改成特征对距离直方图。把每个库分子往药效团上叠要逐对搜索构象 +
  刚性叠合，几千个分子就是分钟级，而且不同骨架常常根本叠不上。
- **分数是覆盖率，不是对称相似度。** 第一版用了 Tanimoto 式 `1 − Σ|差|/Σ和`，
  结果「完全满足药效团」的分子只拿到 0.6：模型只有三五对距离，分子动辄几十对，
  多出来的部分全在扣分。药效团问的是「该有的有没有」，不是「是不是恰好只有这些」，
  所以改成 `Σ min(模型, 分子) / Σ 模型`。
- 聚类的 `support` 数的是**分子**不是点：同一个分子里两个挨得近的同类型特征只能证明
  它自己有这两个特征，不能算两次支持。
- **已知局限**：共识点是若干位置的**平均值**，由此产生的距离对可能任何单个分子都不
  完全具备，所以榜首也拿不到 1.0（实测 0.6）。这不影响排序，但不能把这个分数当
  「匹配度百分比」念给用户。

---

## 8. 族与装配

`all_tools.py` 的 `FAMILIES` 变更：

| 族 | 现在 | 变更 |
|---|---|---|
| `structure` | search_pdb / fetch_structure / import_structure | + `assess_structure` |
| `pocket` | — | `detect_pockets` / `define_pocket` |
| `ligand` | import_ligandset / prepare_ligands | + `curate_ligands` / `build_reference_set` |
| `screen` | — | `screen_similarity` / `screen_shape` |
| `analysis` | — | `rank_candidates` / `analyze_pose` / `validate_docking` |
| `admet` | — | `predict_admet` |
| `sar` | — | `scaffold_analysis` / `matched_pairs` / `enumerate_analogs` / `record_assay` |
| `docking` / `affinity` / `export` | 不变 | `dock` 加 `offset` |

表里是**最终状态**，不表示同一期上线——`build_reference_set` 属 P1，`assess_structure`
属 P2，其余按 §2 的分期走。

**新增模块**：`analysis.py` / `screen.py` / `admet.py` / `sar.py`。
`detect_pockets` 与 `define_pocket` 放 `pockets.py`（算法已在那里，照 `export.py`
「库层 + 工具同文件」的先例）。

**工具总数**：24 → 41。单族最大 4 个工具，装一族仍是 3K token 量级，
不改变「核心 14 个常驻、其余按族加载」的预算结构。

**同步要改的四处**（漏一处就是「写了但模型看不见」）：

1. `domain/__init__.py` 的 `__all__` 加四个模块名。
2. `all_tools.py` 的 `FAMILIES`。
3. `debug_tools.py` 的 `CALLS`（按依赖顺序，新增工具插在对应位置）。
4. `permission.py` 的 `PLAN_DENY`：产出新产物的工具全部加入；
   只读的 `analyze_pose` / `assess_structure` 不进。

---

## 9. 断言清单

### 9.1 新增检查组

| 组名 | 函数 | 条数 | 覆盖 |
|---|---|---|---|
| `pocket-tools` | `check_pocket_tools` | 30 | detect / define / prepare_receptor(pocket) |
| `rank-candidates` | `check_rank_candidates` | 29 | 合并、方向、对齐、分档 |
| `analyze-pose` | `check_analyze_pose` | 18 | 接触、氢键、冲突、缺位姿 |
| `curate-ligands` | `check_curate_ligands` | 24 | PAINS、去重、窗口、flag 模式 |
| `screen` | `check_screen_similarity` / `check_screen_shape` | 16 + 10 | 指纹、形状（USR）、SMARTS、换参不复用 |
| `validate-docking` | `check_validate_docking` | 14 | 原地 RMSD、阈值、无共晶配体、gate 进表 |
| `reference-set` | `check_reference_set` | 16 | 最强活性、PAINS 只标、too_small |
| `batch-offset` | `check_batch_offset` | 19 | 两批无重叠、越界、换参不复用 |
| `mmcif` | `check_mmcif` | 14 | 有/无 gemmi 两路、失败不登记、来源格式记住 |
| `assess-structure` | `check_assess_structure` | 17 | 缺失残基、NMR 只算第一套、金属不算配体 |
| `admet` | `check_admet` | 16 | source 标注、unsupported、公式进产物 |
| `sar` | `check_sar` | 31 | 骨架、配对方向、枚举去重、实验回填 |
| `pharmacophore` | `check_pharmacophore` | 33 | 共识点、叠合失败逐条记、分数分得开活性与诱饵、叠合产物可直接复用 |

合计约 106 条，分五批进 `GROUPS`，与现有断言一起跑。

### 9.2 跨工具的六条

这六条每个新工具都要过一遍，**不是可选**：

1. **收 handle 不收路径**：参数表里除 `record_assay.file` 外不出现 `path`。
2. **顶层不 import rdkit**：AST 扫描覆盖新模块。
3. **族登记完整**：`catalog()` 能查到每个新工具；漏登记时它常驻而不是装不进来，
   断言抓不到，所以另加一条「`FAMILIES` 覆盖全部 `domain_tools()`」。
4. **装配可见**：`check_live_app` 的 `DOMAIN_TOOL_NAMES` 从 `all_domain_tools()` 现算，
   缺一个就逐个报名字。
5. **失败返回结果**：每个工具至少一条「坏输入 → `failed=True` 且文案可操作」的断言。
6. **不静默截断**：每个会截断的工具至少一条「末尾写明还剩多少、怎么取到」。

### 9.3 辨别力要求

每条断言都要能通过一次注入变红。P0 的四处已实测（2026-10-27），逐条记在这里：

| 注入 | 应响的断言 | 实测 |
|---|---|---|
| `pockets.py` 里 `support.complexes` 改成 `0` | 支持度记了复合物数 | 红：`{'complexes': 0, ...}` |
| `analysis.py` 的 `SOURCE_KINDS["affinity"]` 方向写反 | pK 高的排前面 | 红：`{'dock': 1.5, 'affinity': 3.0}` |
| `analysis.py` 的 `HBOND_ANGLE` 改成 `90` | 角度不够的极性接触不算氢键 | 红：`_hbonds` 从 1 条变 2 条（100° 那对被算了进去） |
| `ligand.py` 的 `catalog.GetMatches` 恒返回空 | PAINS 单独计数 | 红：`kept` 从 4 变 8 |
| `docking.py` 的 `selected` 改回 `records[:limit]`（忽略 offset） | 第二批是 C/D | 红：第二批仍是 `['A', 'B']` |
| `structure.py` 的「无法判断」改成「0」 | 没有 SEQRES 时报「无法判断」 | 红：输出里出现「缺失残基：0（没有 SEQRES）」 |
| `admet.py` 把 `hppb` 的来源改成 `rule` | 做不了的端点明确标 unsupported | 红：`{'source': 'rule'}` |
| `sar.py` 的 `matched_pairs` 方向写反（两个分支减反） | dock 越低越好，c1 是更优侧 | 红：更优侧变成 c2（这条不是事后注入，是写的时候真错了，测试当场抓住） |
| `pharmacophore.py` 去掉 `min_support` 过滤 | 低于 min_support 的点被丢掉 | 红：`[4, 2, 6, 3, 3, 6]`（support=2 < 门槛 3 的点混进来了） |

第三处一开始没抓住：夹具里那对原子正好在 90° 上，阈值降到 90° 时浮点比较
（`90.0 < 90.0` 为假）仍然算通过。把夹具挪到 100° 之后才成为真正的判据——
**辨别力要求逼出的是夹具的问题，不是断言的问题**，这类偏差只有真注入一次才看得见。

---

## 10. 本轮不做

- 同源建模本身（只收导入 + 质量评估）
- 冷冻电镜密度图拟合（只取沉积模型）
- decoy 生成（DUD-E 式）
- 可购买性与逆合成路线
- ADMET 模型层（只做规则层）
- 自训练打分、多受体集合对接
- 药效团构象搜索（P4 只做特征匹配）

## 11. 待确认

1. **产物目录**：`~/.opencode_tui/{pockets,candidates,scaffolds}/` 还是放 `runs/` 下？
   倾向前者——它们是过程产物，`runs/` 只收交付物。
2. **`record_assay` 收路径**：作为第四个收路径的动作是否可接受？
   倾向可以，理由写在 §6.5。
3. **Plan 模式**是否允许 `rank_candidates`（只读已有产物、不联网）？
   倾向允许，但 `detect_pockets` 这类会联网取复合物的仍拒绝。
4. **分档命名**：最低档叫 `inactive` 还是 `below_cutoff`？
   倾向后者——计算侧只能证明「没达门槛」，证明不了「不结合」。
5. **RCSB 缓存合并**：`search_pdb` 与 `complexes` 的两套缓存目录是否在 P0 一并合并？
   倾向合并，否则同一个 UniProt 在一次会话里会被查两次。
