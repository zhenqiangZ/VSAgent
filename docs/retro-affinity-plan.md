# 回顾性实验：靶点内活性数据能带来多少排序提升

回答一个问题：**面对一个具体受体，把该靶点已发表的活性数据收进来，到底该用在哪一步？**

候选答案有三个，成本差两个数量级：

1. 微调 DTA（收集 → 训模型 → 评估）
2. 只做**校准**（收集 → 拟合单调映射，不动权重）
3. 只用数据做**局部模型**（收集 → ECFP + GBDT，进共识排序）

这份方案用已有数据把三者放在同一张表上比，**不训练任何生产模型**。结论可能是
「都不值得」，那也是结论。

> 结果已经跑出来了，见 [`docs/retro-affinity-report.md`](retro-affinity-report.md)。
> 一句话：微调那条线按 §9 的规则被否掉了。

先说已核对的事实——下面的判断都建立在这些读数上，不是凭印象。

## 0. 已核对的事实

### 0.1 现状

| 项 | 读数 | 位置 |
|---|---|---|
| 亲和力模型 | VCrossFormer（ESM-2 650M + ChemBERTa 77M + 双图交互，hidden 128 / 4 头 / 4 层） | `models/DTA/model/model.py:312` |
| 训练分布 | PDBbind v2020，19443 个复合物 | `models/DTA/data/INDEX_general_PL_data.2020` |
| 训练集/验证集/测试集划分 | 已有 `split.json`（train/val/test 三段 PDB id） | `models/DTA/data/split.json` |
| 能否从已有权重续训 | 能，`model.load_model=true` + `load_model_path` | `models/DTA/task/task.py:58` |
| 训练数据形态 | LMDB + **预计算**的 ESM/ChemBERTa 嵌入 pkl，不是直接读 CSV | `models/DTA/predata/build_db.py` |
| 原始复合物 | 15700 个，`<id>/<id>_complex_min_ligand.sdf` + `<id>.pocket.pdb`，共 4.7GB，**不在仓里** | `$VSA_DTA_PDB_DIR`（本机见 `.env`） |
| 训练口袋长什么样 | 只有蛋白原子（配体与水都剥掉）；原子数 p25 540 / 中位 630 / p75 722 | `$VSA_DTA_PDB_DIR/*/*.pocket.pdb` |
| 打分口径 | pK 越高越好，默认 `Ki`，口袋半径 10 Å | `affinity.py:72-79` |
| 单次规模 | `limit` 默认 50、上限 500，整批超时 900 秒 | `affinity.py:77-79` |
| 口袋截取规则 | 残基任一原子落在球内就整条保留，HETATM 也保留 | `models/DTA/utils/pocket.py` |
| ChEMBL 取数 | 已有：靶点选型、分页、assay 反查、四类剔除 | `chembl.py:412` / `569` / `592` / `641` |
| ChEMBL 单次上限 | `max_rows` 5000，到顶时**按活性截断** | `chembl.py:103`、`chembl.py:31` |
| 配体准备 | `limit` 默认 200、上限 2000（3D 构象在这里生成） | `ligand.py:64-65` |
| 参考活性聚合 | 同分子取**最强**那条 | `chembl.py:693` |

### 0.2 由此得到的四条结论

1. **数据收集这一半已经建好了。** `search_actives` 的靶点选型、`parent_molecule`
   归并、`standard_relation != "="` 剔除、assay 置信度反查都是现成的，实验不用重写。
2. **「取最强那条」不能直接拿来训练。** 它对筛选用是对的，对训练是系统乐观偏差。
   训练集必须走另一条聚合规则（中位数），并把规则写进快照。
3. **微调的成本主要不在训练，在预处理。** 每个复合物要算一次 ESM-2 650M 的口袋嵌入，
   CPU 上 10–20 秒一个，高数据靶点光这一步就是十小时量级。
4. **三个候选答案里有两条不需要动权重。** 如果它们已经能吃掉大部分收益，
   整条微调线就不该建——这个判断必须在写训练脚本之前拿到。

## 1. 主问题与预注册判定

判定写在跑之前，跑完只许照着念，不许换指标重看。

| 编号 | 问题 | 对比 | 判定指标 |
|---|---|---|---|
| **H1** | 预训练 DTA 在该靶点上，打得过「与已知活性物最相似」吗？ | B3 vs B1 | Spearman ρ |
| **H2** | 用本靶点数据做校准，回归指标改善吗？ | B4 vs B3 | RMSE（ρ 必须不变） |
| **H3** | 只训最后一层，排序变好吗？ | T1 vs B3 | Δρ 与 ΔBEDROC |

**H1 是前提。** B1 是「ECFP4 与训练集活性物的最大 Tanimoto」，它不需要 GPU、不需要
3D、不需要口袋。如果它和 B3 打平，那么在这个靶点上，整条亲和力链路（含 650M 参数
和 15GB 权重）没有增量价值——这个结论本身比微调成败更重要，必须写进报告。

## 2. 靶点选择与审计

三个数据量档位，每档一个。档位由**审计实测**决定，不按预期挑：

| 档位 | 去重后化合物数（pChEMBL ≥ 6） | 用途 |
|---|---|---|
| L | < 300 | 验证「小样本下这条路走不通」 |
| M | 300–1500 | 主战场 |
| H | ≥ 1500 | 验证「数据够了是否真的翻盘」 |

审计由 `tools/retro_affinity.py select` 完成，它给出七项读数：

1. 活性记录总数、四类剔除前后各多少（与 `_select` 同一套判据）
2. 去重后化合物数、pChEMBL 分布（min / 中位 / max、≥6 与 ≥7 各多少）
3. **置信度扫描**：floor 取 5/6/7/8/9 时各剩多少化合物——数据量对质量门槛有多敏感
4. assay 数量、`assay_type` 分布、年份范围（决定能不能做时间切分）
5. **Murcko 骨架分布**：骨架数、最大组占比、size≥2 的组数、size≥5 的组数
6. **PDBbind 重叠**（两层）：该 UniProt 的 PDB 条目有多少落在 `split.json` 的
   train/val（复合物级），以及**这些化合物本身**有多少出现在训练集里（配体级）
7. 可用性判定：H1 可做 / H3 可做 / 需要换靶点

第 5 项决定骨架切分是否可行：最大骨架组占比超过 35% 时，S2 切分会让测试集只剩
一两个骨架，ρ 的方差直接失控。

第 6 项是**混杂控制**，两层缺一不可：

- **复合物级**回答「预训练时这个靶点见过多少结构」，是旁证；
- **配体级**回答「模型是不是背过这些分子」，这才是判据——见过分子的预测是记忆，
  那样的 Δ 不能拿来论证泛化。超过 5% 就换靶点。

配体级比对的做法：读 `$VSA_DTA_PDB_DIR` 下训练集每个复合物的 `*_min_ligand.sdf`，
取 InChIKey 前 14 位（骨架层），与本靶点化合物的同一字段求交。

> **覆盖率只有约 65%，这是已知限制。** 该数据集的配体 SDF 有相当一部分用「全芳香键」
> 写法（MDL 键型 4 铺满整个环），RDKit 无法 kekulize，也就给不出 InChI。跳过键级、
> 保留芳香标记的兜底路径实测救不回来（`MolToInchiKey` 仍然要求 Kekulé）。
> `pdbid_smiles_dict.pkl` 那条路更差：前 500 条里 43 条解析失败，能解析的那批
> `MolToInchiKey` 还返回 None。
>
> 由此得到两条纪律：**重叠率是下限**（解析失败只会漏判，不会误判），报告里必须同时
> 给出覆盖率；覆盖率低于 80% 时不能只看这个数，要与复合物级一起判。

## 3. 数据构建：五个必须避开的坑

`chembl.py` 的模块 docstring 已经记了这些坑，实验里每条都要有对应动作：

| 坑 | 现状 | 实验里的动作 |
|---|---|---|
| 一个 UniProt 映射到多个 ChEMBL 靶点（P00533 → 16 个） | 已只取 `SINGLE PROTEIN` | 审计时打印实际用的 `target_chembl_id`，写进快照 |
| 盐型/共晶各算一个分子编号 | 已按 `parent_molecule_chembl_id` 归并 | 归并后再按 InChIKey 前 14 位二次去重 |
| `IC50 > 10000 nM` 被算成 pChEMBL | 已剔除 `relation != "="` | 保留该过滤，但**它当前不筛掉任何东西**（见下） |
| 到上限时按 `-pchembl_value` 截断 | 生产路径如此 | **实验不能这样取**：截在高效端会让数据集没有弱化合物，R² 与校准全是假的。实验按 `activity_id` 排序全量取 |
| 活性类型混杂（细胞 vs 生化） | 已记录 `assay_type` | 训练与测试**只用单一 assay_type**（默认 `B`） |

**聚合规则**：按母体分子取 pChEMBL **中位数**，并记下测量条数。中位数对离群测量
稳健；取最大值会让每一条训练标签偏向「作者最想强调的那个数」。

> **关于 `relation != "="` 这条过滤（2026-10-29 实测）**：`chembl.py` 的模块 docstring
> 说 `IC50 > 10000 nM` 会被 ChEMBL 算出一个 pChEMBL，事实不是这样——查询
> `standard_relation__in=['>','<','>=','<=']` 且 `pchembl_value__isnull=false`，
> 在 CHEMBL204 上四种全部回 0 条；`standard_relation__isnull=true` 也是 0 条。
> **ChEMBL 只给 `=` 的记录填 `pchembl_value`。** 所以这条过滤在当前 API 上是
> 防御性的（换成本地库或用户自备表时才有意义），剔除计数里它恒为 0，不要据此
> 判断数据脏不脏。

输出快照（后面一切步骤的唯一真相）：

```
.probe/retro/select/<uniprot>.json
  { uniprot, target {chembl_id, name, alternatives},
    fetched_at, cached,
    filters {min_pchembl, measure_types, min_confidence, assay_type, relation},
    aggregation: "median",
    counts {rows_total, rows_matched, fetched, truncated, dropped{...}, compounds},
    potency {min, median, max, ge6, ge7, suspect},
    assays {count, types{B/F/A/T}},
    years {min, max},
    confidence_sweep {5:n, 6:n, ...},
    compounds [{id, smiles, pchembl, pchembl_max, n_measurements,
                assays[], years[], assay_types[], inchikey14}],
    scaffolds {available, parsed, failed, n_scaffolds, largest, largest_size,
               largest_share, groups_ge2, groups_ge5, by_compound{}},
    pdb {ok, entries, in_train[], in_val[], in_test[], sample[],
         ligands {ok, index_size, coverage, keyed, parsed_failed, checked,
                  matched, share, examples[]}},
    tier, verdict {tier, h1, h3, notes[]} }
```

配体索引单独缓存在 `.probe/retro/cache/ligands-<hash>.json`（键 = 目录 + 训练集规模 +
`split.json` mtime + 索引版本），所以第二个靶点不再重扫 1.1 万个 SDF。

## 4. 切分

三个切分，同一次结果三种都报——差距本身是信息：

| 切分 | 做法 | 看什么 |
|---|---|---|
| S1 随机 | 80/10/10，固定 seed | 上界（有骨架泄漏，必然虚高） |
| **S2 骨架** | Murcko 骨架分组，整组进同一侧 | **主终点** |
| S3 时间 | 按 `document_year`，老 → 新 | 最接近前瞻，年份跨度够才做 |

S2 的硬断言：训练集与测试集的骨架**交集为空**。这条破了 `build` 直接拒绝写盘——
它一旦有交集，这份数据衡量的就是同系物插值而不是泛化。

骨架切分是**贪心**：小于 `min_group` 的骨架组整组进训练集，剩下的大组按「大 → 小」
依次填训练 / 验证，余下的全进测试。大组在前是有意的——大骨架落进测试集，测试集就
退化成一道单骨架的内部排序题。

`min_group` 默认 5，但**切不出来会自动降**（先 3 后 2）：小靶点上 5 会把测试集压到
十来个分子，ρ 的区间宽到 ±0.6。降门槛只影响「同骨架 2–4 个分子」的组留在哪一侧，
不破坏互斥。用了哪个值会打印出来并写进 `build.json` 的 `min_group`（原值记在
`requested_min_group`）。

S3 **切在年份边界上**，不按分子数硬切：按分子数切会把同一年劈成两半，而同年同批
发表的常常是一系列类似物——那等于把骨架泄漏又放了回来。

三个档位的测试集规模大致是 20 / 100 / 600 —— 直接决定统计功效（§6）。

## 5. 实验臂

同一个受体、同一个口袋中心、同一批测试化合物，**只有打分函数不同**：

| 臂 | 内容 | 依赖 | 说明 |
|---|---|---|---|
| **B0** | 随机排序 | 无 | 下界，跑 100 次报均值与标准差 |
| **B1** | ECFP4 与训练集活性物的**最大 Tanimoto** | RDKit | 筛选式打分：像不像任何已知活性物 |
| **B1k** | ECFP4 **kNN 加权预测 pK**（k=5） | RDKit | 活性式打分：像的那个有多强 |
| **B2** | ECFP4 + LightGBM 回归 | RDKit + LightGBM | 轮数用训练集内 5 折早停选，测试集不参与 |
| **B3** | 预训练 `score_affinity` | DTA 环境 | 测试分子先 ETKDG 生成 3D（`mol_to_graph` 用坐标） |
| **B4** | B3 + isotonic 校准（训在训练集上） | sklearn | **只影响回归指标** |
| **T1** | head-only 微调（冻结编码器，只训 `affinity_mlp`） | 见 §8 | 主实验臂 |
| **T2** | 全量微调 | 见 §8 | 只在 H 档、且 T1 为正时跑 |
| **N1** | 阴性对照：B2 用打乱的标签训练 | — | ρ 的 95% CI 必须含 0 |

**B1 与 B1k 的差别是有意的。** 原方案只写了「最大 Tanimoto」，但那个分数衡量的是
「像不像任何已知活性物」——而测试集里全是活性物，它区分不出强弱。kNN 加权预测的是
「最近的那些有多强」，才是同一任务下的对照。两条都跑，差多少本身就是答案：
PTP1B 上 B1 是 +0.111、B1k 是 +0.451，说明「像」几乎不带信息，「像谁」才带。


N1 很便宜，但它能抓住最贵的错误：切分泄漏、SMILES 与 pK 错位、骨架计算的 bug。

### 5.1 受体与口袋（B3 / B4 / T1 共用，`pocket` 子命令）

B 系列里只有 B3 起需要口袋，而口袋一旦不一致，整组对比就废了。所以每个靶点**只定一次**，
写进 `.probe/retro/<label>/pocket/`：

1. 取该 UniProt 的 PDB 条目（`complexes.list_complexes`，只含有非聚合组分的），
   等距抽 20 个来看。
2. 每个条目用生产路径的同一套判据找「像配体的东西」（`hetero_groups` +
   `preprocess.screen_ligands`），重原子 < 12 的不要。
3. 把**所有**候选配体按「接触残基的 Jaccard 重合度 ≥ 0.30」做单链接聚类。
   跨条目投票找的是**主结合位点**：同一个位点会在几十个结构里重复出现，结晶添加剂和
   别构位点通常只出现一两次。
4. 取最大的一簇，在这里面挑代表：先看分辨率，但**口袋大小要落在训练分布附近**
   （离中位数 630 最近，进 p25–p75 就停）。只看分辨率会选到重原子 90 的巨分子，
   截出 873 个原子的口袋。
5. 只留与配体 5 Å 内有接触的链，剥掉全部杂原子，按
   **「残基任一原子落在配体 10 Å 内就整条保留」** 截口袋。

> **聚类不能用质心距离。** 同一个活性位点里两个大小相近的配体，质心能差 5–8 Å，
> 按 4 Å 距离聚会把它们拆成两个「位点」。实测 PTP1B：21 个候选聚出 20 个位点，
> 簇大小 2，看起来毫无共识——而它们大多就在同一个催化口袋里。换成残基重合度之后
> 是 14 个位点、最大一簇 6 个。

> **截口袋不能用 `pocket_from_center`。** 那个函数截的是「以质心为球心的球」，
> 而训练用的是「到配体任意原子的距离」。细长配体上两者差很远：1YPK 的 42 重原子配体
> 用质心球只留 26 个残基 / 217 个原子，用配体表面留 76 个 / 602 个。

> **口袋大小要对着训练集看。** 训练口袋原子数 p25 540 / 中位 630 / p75 722，
> 全距 20–1554。出四分位区间不等于不可用（STING 的 cGAMP 位点在二聚体界面上，
> 天然就大，814 个原子、跨 A/B 两条链），但读分数时要知道这个靶点的位点比典型的大。

## 6. 指标与统计功效

**主终点**：测试集内部的 **Spearman ρ**（对连续 pChEMBL）。不需要 decoy，n=20 也能算。

**次终点**：

- **BEDROC(α=20)**：需要 decoy。用 property-matched decoy，**所有臂共用同一套**
  ——绝对值不可信，横向比较公平。
- **top-10% 命中率**：排序前 10% 里真 pChEMBL ≥ 7 的比例。最接近用户的决策
  「先做哪几个」。
- RMSE / R²：只有 B3/B4/T1/T2 有绝对尺度。

**不用 EF1%**：L 档测试集 20 个化合物，前 1% 是 0.2 个分子，这个数没有意义。
EF 系列只在 n_test ≥ 50 时报告，且用 EF5%。

**统计**：对测试化合物做 1000 次 bootstrap，报 Δ 的 95% CI；学习类模型跑 5 个
seed，**主指标取「各种子打分的算术平均」的 ρ**，种子间的 sd 单独报出来当稳定性。

> 为什么主指标不是「各种子 ρ 的平均」：两个数会不同，但排序工具部署时用的是集成分，
> 让指标对准实际用法才不会被单次抽样的运气带偏。PTP1B 上两者是 0.410 与 0.414，
> 差得不多；但阴性对照跨种子的 ρ 从 -0.13 波动到 +0.35，单次报一个数等于报运气。

| 档位 | n_test | ρ 的 95% CI 半宽 | 能检出的最小 Δρ |
|---|---|---|---|
| L | ~20 | ±0.45 | 0.45 |
| M | ~100 | ±0.20 | 0.20 |
| H | ~600 | ±0.08 | 0.08 |

L 档几乎检不出任何合理效应——**这本身是结论**，不能拿一个不显著的 ρ 去论证
「微调没用」。

## 7. 三个内建自检

**自检 1：单调变换不改变排序。** isotonic 是单调映射，所以 ρ(B4) 与 ρ(B3) 必须
逐位相同。如果变了，`calibrate` 的实现一定错了（或者并列名次被打散）。这条断言免费，
能抓到一整类 bug。

**自检 2：阴性对照。** N1 的 ρ 的 **95% CI 必须包含 0**，否则判为泄漏，整个实验作废。

> 原方案写的是「ρ 落在 ±0.15 内」，那条在小样本上是错的：零点附近 ρ 的标准差约
> 1/√(n-1)，n=15 时一个纯噪声的 ρ 有近六成概率越界。实测 STING 的阴性对照就是
> +0.20——而它是**打乱标签**训出来的，不可能是泄漏。改成「CI 含 0」既保留原意
> （没有信号），又自然随样本量伸缩。

**自检 3：模型自检（B3 专用）。** 拿训练集里的复合物、用它**自己的**口袋给自己那张
配体 SDF 打分，预测值必须随真实 pK 单调。这一步只验「管道接对了没有」，不验准确性。

实测五个（真实 pK 0.6 / 5.0 / 6.71 / 8.1 / 13.0）：

```
预测      4.06   5.44   5.63   8.39   10.19      单调 ✓
偏差     +3.46  +0.44  -1.08  +0.29  -2.81      两端压缩
```

**两端压缩是模型的已知性质**（训练靶点的 pK 3–11 居多，极端值被拉回中段），不是管道
问题。真正要抓的是**不单调**——那说明口袋、配体或构象喂错了，B3 的所有分数都不能用。

> 踩过的坑：第一次把 5 个配体**全都喂给同一个口袋**（1zsb 的），得到一排 4.0–5.2，
> 差点误判成「模型坏了」。每个配体必须配它自己那套复合物。这条检查的前提是
> 「配体与口袋同源」，用错了口袋，测的就是别的东西。

## 8. 微调臂的数据与配置

DTA 的训练路径是「LMDB + 预计算嵌入」，不直接读 CSV。每个靶点要生成：

```
.probe/retro/<target>/dta/
  <id>_ligand.sdf        单个 3D 构象（ETKDGv3）
  <id>_pocket.pdb        同一口袋中心、10 Å 截出（复用 utils/pocket.pocket_from_center）
  INDEX_general_PL_data  <id>  <pK>     （PDBbind INDEX 格式）
  pocket_embeddings.pkl  predata/esm2embedding_650m.py 产出
  smi_embeddings.pkl     predata/smi2pkl.py 产出
  split.json             就是 §4 的 S2
  config.yaml            复制 configall.yaml，改 data.* 路径
```

这一整块由 `run` 的 T 阶段准备，**不是 `build` 的产物**——它要先定受体与口袋中心。
`build` 只交切分。

微调配置的关键几行：

```yaml
model:
  load_model: true
  load_model_path: <基座 checkpoint>
train:
  lr: 1e-5          # 不是 1e-4：小样本 + 已收敛权重，大了直接崩
  epoch: 30
  batch_size: 16
```

**成本在预处理不在训练**：每个复合物要算一次 ESM-2 650M 的口袋嵌入，CPU 上
10–20 秒。H 档 2500 个就是 7–14 小时纯预处理。所以 L / M 两档先跑，H 档只在
L/M 出正结果后才付这笔钱。

## 9. 判定规则

| 决策 | 条件 |
|---|---|
| 建 `fit_surrogate`（B2） | Δρ(B2 − B1) ≥ 0.10 且 CI 下界 > 0，≥2 个靶点 |
| 建 `calibrate_affinity`（B4） | ΔRMSE 的 CI 上界 < 0，且 ρ 与 B3 逐位相同 |
| 建 head-only 微调（T1） | Δρ(T1 − B3) ≥ 0.10 且 CI 下界 > 0，≥2 个靶点 |
| 建全量微调（T2） | 只在 H 档上 Δρ(T2 − T1) 的 CI 下界 > 0 时考虑 |
| **停止整条微调线** | B2 已经在 ≥2 个靶点上打赢 B3 |

「≥2 个靶点」是刻意的：三个里挑一个显著的，是标准的 p-hacking。

**校准不能改善排序**（自检 1），所以 H2 只能在 RMSE 上判定。它的价值是绝对尺度和
可解释性，不是排序——如果报告里出现「校准后 EF 提升」，那一定是实现错了。

> **2026-10-29 追记：目标换了。** 上面这套规则回答的是「作为产品功能要不要建」，
> 结果按规则否掉了。之后的需求变成「为论文提供微调的证据」——**协议一个字没改**
> （同一套 S2 切分、同一个口袋、同一批测试化合物、配对 bootstrap），只是追问从
> 「值不值得建」换成「能做到多少」。结果见
> [`docs/retro-affinity-report.md`](retro-affinity-report.md) §十：全量微调把 ρ 从
> 0.077 拉到 0.385（Δ +0.31，CI [+0.05, +0.54]），追平但不超越 2D 基线。
>
> 换句话说，**这条规则判的是「先做哪个」，不是「微调没用」**。先做 B2 仍然对：
> 它便宜二十倍、效果一样好。

## 10. 实施

新增一个脚本，四个子命令：

```
python tools/retro_affinity.py select --target P00533 --target P00734
python tools/retro_affinity.py build  --target P18031 [--min-group 5] [--seed 42]
python tools/retro_affinity.py run    --target P18031 --split s2 [--arm b2]
python tools/retro_affinity.py pocket --target P18031 [--sample 20] [--radius 10]
python tools/retro_affinity.py fit    --target P18031 [--full] [--limit N] [--epochs 20]
python tools/retro_affinity.py report --split s2 [--baseline b2]
```

`fit` 生成单靶点数据集并调 `tools/fit_target.py`（在 `models/DTA/.venv` 里跑）。
默认只训 `affinity_mlp`（head-only，`head-*` 目录）；`--full` 训全部参数（`full-*`）。
产物在 `<label>/fit/<tag>/`：`data/`（每个化合物一个 SDF + 一份口袋）、`INDEX`、
`split.json`、`out/config.yaml`（训练用，指向基座权重）与 `out/config.infer.yaml`
（推理用，指向微调出来的 checkpoint）。

> **两份配置不能混。** 训练配置的 `model.load_model_path` 是基座权重；拿它去推理，
> 相当于把微调结果丢掉再跑一遍，而且不报错——表现是「微调前后完全一致」。
> `run --arm t1/t2` 只认 `config.infer.yaml`。

**已实现**：`select`、`build`、`run` 的 `b0` / `b1` / `b1k` / `b2` / `b3` / `n1`、
`pocket`、`report`。还没写的是 `run` 的 `b4`（isotonic 校准）与 `t1` / `t2`（微调）。

`b3` 会在 `.probe/retro/<label>/b3/` 下生成 `conformers/`（每个分子一个 SDF，按 SMILES
判缓存）、`tasks.json`、`scores.csv` 与 `run.log`。构象用 ETKDGv3 + MMFF，种子固定。
**这是一处已知的分布差异**：训练用的是复合物里抽出来的**结合构象**，而这里喂的是自由
构象；模型的距离头（MDN over pairwise distances）对 3D 几何敏感，所以 B3 的分数偏低
部分可能来自这一点，而不是靶点本身。

`report` 的 Δ 一律是**配对 bootstrap**：同一批重采样上同时算两条臂的 ρ 再相减。
**不要用「两个 95% CI 有没有重叠」判断差异**——两条臂看的是同一批化合物，独立区间
既丢掉了这部分相关性，又在样本越小的时候越容易把真差异判成不显著。配对 Δ 把
「这批分子本身有多难排」这一项消掉了，剩下的才是臂与臂的差别。

| 输出 | 位置 |
|---|---|
| 逐臂打分 | `.probe/retro/<label>/runs/<arm>_<split>.json` |
| 汇总表 | `.probe/retro/report_<split>.json` |

每份逐臂结果含指标、种子间波动与逐分子打分（`predictions`），后者是 `report` 做
配对 bootstrap 的输入。

`build` 只做两件事——切分与体检，**不建 `dta/` 目录**：DTA 的训练输入要 3D 构象、
要同一个口袋、要预计算嵌入，而口袋要先定受体。那些属于 `run` 的阶段，现在建出来
只会是一堆空目录。产物是：

```
.probe/retro/<label>/
  dataset.json    每个化合物的骨架 + 三套切分归属
  splits/s1.json  s2.json  s3.json     {train:[], val:[], test:[]}
  build.json      参数、体检结果、min_group 实际值、是否自动降过
```

体检报告给的是：两侧分子数、骨架交集、两侧中位 pK、ρ 的 95% 区间半宽。两条会
单独提出来说：**骨架交集非 0**（S2）与**两侧中位 pK 差超过 0.5**（分布偏移，时间
切分几乎必然有，但必须看见）。

复用关系（不重写）：

| 需要的 | 用哪个 |
|---|---|
| ChEMBL 靶点选型 / 分页 / assay 反查 | `chembl._resolve_target` / `_fetch_activities` / `_assay_facts` |
| PDB 条目检索 | `complexes.list_complexes` |
| 口袋截取 | `models/DTA/utils/pocket.pocket_from_center` |
| 训练集配体骨架 | `$VSA_DTA_PDB_DIR/<id>/<id>_complex_min_ligand.sdf` |
| B3 打分 | 直接调 `virtual_screen.py --pocket --tasks`，**不经过 agent** |
| 骨架、ECFP4 | RDKit（`MurckoScaffold`、`AllChem.GetMorganFingerprintAsBitVect`） |

原始复合物的位置由 `--pdb-dir` 或环境变量 `VSA_DTA_PDB_DIR` 给（本机写在 `.env`）。
找不到时配体级重叠明确报「跳过」，复合物级照常做。

绕过 agent 是刻意的：一个靶点的审计算一次要几十页请求、几分钟，塞进 agent 循环会
撞上 `MAX_STEPS=24` 和上下文预算，而它本来也不是给人交互用的。

产物全部落 `.probe/retro/`（已 gitignore），脚本本身进版本控制。

**跑序**（每步都有可以停下的点）：

1. `select` 三个靶点 → 任一档位空着就换靶点
2. `build` 三个靶点 → S2 的测试集不到 15 个就换靶点或降 min_group
3. B0 / B1 / B2 / N1（不需要 DTA 环境）→ **H1 的一半答案在这里**
4. B3 / B4 → H1 的最终答案
5. T1（L、M）→ H3
6. T2（仅 H 档，且仅在第 5 步为正时）

## 11. 风险

| 风险 | 影响 | 处理 |
|---|---|---|
| 单晶结构不能代表全部化学型 | 所有臂一起偏；Δ 仍有效，绝对值不可外推 | 报告里写明「结论仅对该口袋」 |
| decoy 选择主导 BEDROC | 绝对值不可比 | 所有臂共用一套 decoy；主终点用 ρ |
| 高数据靶点 = PDBbind 覆盖好 | 「预训练已见过」被误认为「微调收益」 | 审计第 6 项必须与 Δ 并列报告 |
| 配体级重叠只覆盖 65% | 剩下的 35% 只能漏判，重叠率偏低 | 报告里同时给覆盖率；低于 80% 时与复合物级一起判，别只信一个数 |
| 训练集只有百来个点 | T1 仍会过拟合 | 冻结编码器 + dropout 0.3 + 早停 + 5 seed 报方差 |
| 指标不稳导致结论反复 | 换指标重看 = p-hacking | 主终点预注册在 §1，不追改 |
| ChEMBL 全量拉取很慢（EGFR 1.6 万行 = 17 页） | 审计一轮十几分钟 | 原始行落盘缓存，`--refresh` 才重下 |

## 12. 一句话

这个实验里最便宜、信息量最大的部分是 **B0/B1/B2 那三行 baseline**：不需要 GPU，
不需要 3D，不需要口袋，几小时就能跑完，并且直接回答「深度模型相对最笨的相似度
方法到底值多少」。如果那三行就把答案说清楚了，后面千万级的预处理成本可以整段省掉。
