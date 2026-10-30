# 打分与评价层（轮 1）：三个力场 + 两个分析工具

本文件记录「把打分函数从 Vina 一家扩开、并把打分结果怎么评价补上」这一轮：
**做了哪些、为什么这么选、踩到的坑、明确不做的**。

起因是一份关于「下一代打分函数怎么补传统疏水建模短板」的报告，它把方法分四层：
物理层（X-Score / HYDE）、溶剂层（WaterMap / GCMC）、数据层（ML 打分）、
评价层（LE / LLE）。这里的判定只按三条硬约束走：**无 license、无 GPU、
单次调用几十秒**。

## 0. 判定

| 报告里的方法 | 判定 | 理由 |
|---|---|---|
| Vinardo 力场 | **做** | vina 1.2.5 自带（`--scoring vinardo`），零安装 |
| AD4 力场 | **做** | vina 自带（`--scoring ad4`），但要多一个 `autogrid4` |
| `autodock4` 原程序 | 不做 | Lamarckian GA 是 1–5 分钟/配体，一轮 50 条就是几小时 |
| AutoDock-GPU | 不做 | 要 CUDA/OpenCL，而「引擎不随 exe 打包」这条约定下没有收益 |
| GNINA | 不做 | `docs/data-sources-plan.md` §4.1 已定 |
| X-Score 原程序 | 不做 | 学术许可的二进制；它的三个疏水定义我们自己做（见 §2） |
| HYDE 的数值 | 不做 | 系数要原文校准；只落它的**判断逻辑**，不编 kcal |
| WaterMap / GCMC | 不做 | 前者要 Desmond + license，后者没有本地引擎 |
| RF-Score / RTMScore / DeepRMSD | 不做 | 权重与许可、GPU、顶层禁 torch；**rescoring 这条线已有 `score_affinity` + `fit_affinity`** |
| LE / LELP / LLE | **做** | RDKit + 算术，零新依赖 |

## 1. `dock(scoring=...)`：三个力场

```
vina / vinardo : vina --receptor R.pdbqt --ligand L.pdbqt --scoring <name> --center_x …
ad4            : vina --ligand L.pdbqt --maps <prefix> --scoring ad4 …
```

**AD4 不吃 `--receptor`，吃 `--maps`**（官方文档的写法）。盒子已经烘进图里，
再给 `--center_x` 之类会被当成参数冲突。这一点如果搞错，命令跑起来会直接报错，
但更麻烦的是另一种错法：以为「加个 `--scoring ad4` 就行」，结果 vina 找不到图。

### 图从哪来

```
receptor.pdbqt ──┐
box.txt ─────────┼─→ meeko.gridbox.get_gpf_string(...) → receptor.gpf
配体类型列 ──────┘                                      ↓
                                          autogrid4 -p receptor.gpf → receptor.maps.fld + *.map
```

- GPF 的 `ligand_types` 是**这一批配体 PDBQT 类型列的并集**（`pdbqt_types()` 去重保序）。
  少一张图，那一类原子就没有势场，autogrid 直接报错；多一张只是白算。
- `receptor_types` 用 meeko 的固定超集（`HD C A N NA OA F P SA S Cl Br I Mg Ca Mn Fe Zn`），
  与 `mk_prepare_receptor.py` 一致。
- 参数文件 `boron-silicon-atom_par.dat` 由 meeko 自带的内容写进同目录——GPF 用
  `parameter_file` 指过去，不写它 autogrid 会去 cwd 找 `ad4_parameters.dat` 然后报错。
- 图缓存在 `~/.vsagent/grids/<stem>-<hash>/`，键是 **受体指纹 + 盒子 + 配体类型集合 + 间距**。
  图只依赖受体与盒子，换一份配体库重跑不该再花一次 autogrid 的时间。

### 盒子限 40 Å（AD4 专有）

网格点数是 `边长 / 0.375`，代价是**三次方**。用 1M17-A 实测（同一份受体、同一批配体类型）：

| 盒子 | 点数 | autogrid 耗时 | 图占盘 | autogrid 峰值内存 |
|---|---|---|---|---|
| 26 Å | 68³ | **5.5 s** | 16 MB | — |
| 39 Å | 104³ | **17.1 s** | 50 MB | **7.8 MB** |
| 126 Å（Vina 上限） | 336³ | ≈10 分钟（按 39 Å 外推） | ≈1.7 GB | 未测 |

两点值得记下来：

- **内存几乎不随盒子涨**——autogrid 边算边写盘，39 Å 时峰值只有 7.8 MB。真正涨的是
  **时间与磁盘**。所以这个上限的理由是「等不起 / 盘占满」，不是「会 OOM」。
- 39 Å 的整套跑完（含 1 个配体对接）**18.9 秒**，其中 17.1 秒是 autogrid、1.8 秒是对接。
  图算一次就进缓存，同一受体换配体不再花这 17 秒。

所以 `scoring="ad4"` 时超过 40 Å 直接拒绝，并说清实测数字。

**2026-10-29 决定：先留着 40 Å。** 39 Å 只花 17 秒，说明这个上限偏保守；留着的理由
不是性能，而是「超过 40 Å 绝大多数是盒子画错了」。真遇到跨界面的大口袋，改
`docking.AD4_MAX_BOX_EDGE` 一个常数即可（60 Å 约 1 分钟）。

### 分数不可比：四处改动

1. `manifest.engine` 增加 `scoring`，`params` 也记一份（换力场 = 另一个产物目录）。
2. `export._poses_provenance` 的 `source` 从写死的 `"vina"` 改成 `"<engine>:<scoring>"`。
3. `_render_docked` 的首段与「引擎」那行都带力场名（`AutoDock4 力场` 而不是 `ad4`）。
4. `rank_candidates` 的来源表头带 `dock·vina:ad4`；`meta.sources[].engine` 也记一份。

## 2. `analyze_hydrophobic`：HP / HM / 脱水账

回答「这个高分是疏水原子落进疏水口袋，还是分子大、接触多；有没有极性原子白埋」。

- **HP**：配体疏水原子 × 受体疏水原子 ≤ `radius`（默认 4.0 Å）的对数 + 最近距离。
- **HM**：每个配体疏水原子在 `radius+0.5` 内受体重原子的疏水占比 ≥ 50% 记「匹配」，
  输出匹配率并列出未匹配的那几个（带占比）。X-Score 的基准里 HM 优于 HP 与 HS，
  因为「疏水原子落在疏水口袋里」比「接触数多」更接近化学直觉。
- **脱水账**：被埋（`radius` 内 ≥3 个受体重原子）的极性原子中，没成氢键的逐个列出，
  并给出最近的受体极性原子距离——这就是 HYDE 说的「花了脱水代价却没拿到相互作用」的
  结构证据。

  这里**再分两类**，因为混在一起会得出错结论：

  | 情形 | 说法 |
  |---|---|
  | 最近的极性受体在氢键距离**之外**（> 3.5 Å） | **白埋**：付了脱水代价，什么都没拿到 |
  | 在氢键距离**之内**却没算出氢键 | 「只差 3.19 Å 却没算成氢键」：够近，角度没过本工具的口径（PDB 里氢可能缺、质子化状态也可能不同） |

  这一刀是实测逼出来的：伊马替尼那个位姿的 N6 与 THR766 OG1 相距 3.19 Å，
  本工具的口径没算出氢键、而 PLIP 算出了——把它说成「白埋」是错的。

原子分类（写进返回的口径）：

- 疏水 = `C / F / Cl / Br / I`，且 **1.7 Å 内没有 N/O**（除掉羰基碳、酰胺碳、羧基碳）。
- 极性 = `N / O`。S 两头都不算：它在蛋白里既是疏水（Met/Cys）又是弱受体。
  注意这与 `analysis.POLAR`（N/O/S，给氢键用）**故意不同**。
- HS（埋藏非极性面积）这一轮不做：`rdkit.Chem.rdFreeSASA` 可用，但 HP + HM 已经能回答
  同一个问题，多一个 SASA 只是多一处会漂的读数。

**不给 kcal。** HYDE 的系数要拿原文校准，我们没有；编一个数出来比不给更糟——
它会进模型的推理链，而没有任何东西能证伪它。

## 3. `efficiency_profile`：LE / LELP / LLE

- `LE = -分数 / 重原子数`（复用 `docking.ligand_efficiency`）。
- `LELP = logP / LE`（RDKit 的 Wildman-Crippen logP），默认按它升序。
- `LLE = pChEMBL - logP`，**只有给了带 `pchembl_value` 的配体集**时才算，按规范 SMILES 联接。
- 「大而油」= `logP ≥ 4 且 LELP ≥ 6`，写在返回里标明是**经验阈值**。
- 口径必须写：**从对接分数算的 LE 不等于从 ΔG 算的 LE**，只能在同一批、同一引擎、
  同一力场里比较。

`dock` 早就在 `results[].ligand_efficiency` 里写了 LE，但只有榜首那条会显示，
CSV / run / 网页里都没有。这个工具补的是「整批纵向看」，不是重算一遍。

## 4. 顺手抽出来的一件事

`analyze_pose` 里「登记表 → 产物 → 位姿路径 → 受体坐标 → 解析」那 100 行抽成了
`_resolve_pose(ledger, arguments, tool_name=…)`。两个新工具共用它——这段的每一步
（受体要从 manifest 的血缘找、位姿要在产物目录内、重原子要过滤）都漏不得，
复制一份必然有一份少一道校验。

## 5. 判据

| 命令 | 验什么 |
|---|---|
| `python tests/test_agent.py --only docking` | 三个力场的命令形态、AD4 盒子上限、缺 autogrid4 的报错、GPF 生成与图复用 |
| `python tests/test_agent.py --only hydrophobic` | HP/HM/脱水账的算术（手写坐标口算可核）、无疏水原子的边界 |
| `python tests/test_agent.py --only efficiency` | LE/LELP 与 RDKit 一致、排序与缺值、LLE 联接、三种失败 |
| `python debug_tools.py` | 全跑 22 个模块 54 次调用（51 个工具），**真联网**；ad4 那条不在里面（要 autogrid4） |

## 6. AD4 端到端实跑记录（2026-10-29）

本机没有 autogrid4，从 bioconda 取了一份（只解压到 `.probe/`，不动系统）：

```
curl -sL -o autogrid.tar.bz2 \
  https://conda.anaconda.org/bioconda/linux-64/autogrid-4.2.6-h9948957_4.tar.bz2
tar xjf autogrid.tar.bz2 && chmod +x bin/autogrid4
export VSA_AUTOGRID_BIN=$PWD/.probe/adfr/bin/autogrid4
```

用 `1M17-A`（EGFR，26 Å 盒子）与 `series-989b6cfd` 里的 `a1` 跑：

| 步骤 | 结果 |
|---|---|
| `autogrid4`（26 Å 盒子、5 类配体原子） | **5.5 秒**，产出 `receptor.{C,N,OA,HD,A}.map` + `.d.map` / `.e.map` / `.maps.fld` |
| 对接 1 个配体 | **6.8 秒**，AD4 分 **−4.754**，位姿转出 PDB |
| 同一对配体换 `scoring=vina` | **−5.250**，落进另一个产物目录 |
| `analyze_pose` 读 AD4 的产物 | 9 个残基接触、6 个氢键，`meta.engine = vina:ad4` |
| `analyze_hydrophobic` | HP 17 对、HM 5/5、0 个白埋，2 个极性原子成键兑现 |
| `efficiency_profile` | LE 0.43、LELP 1.3 |
| 同参数再跑一次 | `reused=True`，没有重复对接 |
| **39 Å 盒子**（同一受体，边界值附近） | autogrid **17.1 s / 50 MB 图 / 峰值 7.8 MB**；整套含对接 **18.9 s**，分数 −4.42 |

两个数放在一起正好说明为什么必须标力场：**同一对受体/配体/盒子，AD4 是 −4.75、
Vina 是 −5.25**，差 0.5 kcal/mol——而两者根本不是一个尺度。

## 7. 没做但看清楚了的一件事

`prepare_receptor` 已经有 `keep_hetero` 参数，传 `HOH` 就能把结晶水留下。反过来说：
**默认剥掉水之后，PLIP 的 `water_bridges` 永远是 0**——那一行会被读成「没有水桥」。
这是下一轮「溶剂层」的入口：报桥接水 / 被配体占位的结晶水 / 孤立水，
**只报结构，不报 ΔG**（「高能水」要热力学，没有 WaterMap 说不了）。
