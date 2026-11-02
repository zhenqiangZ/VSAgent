# 打分通道与结构呈现（计划）

本文件记录「把 DTA 放到与对接打分同级、把结构呈现变成交付的一部分、把相互作用画到三维里」
这一轮的方案。文中出现的工具名、字段名都是当前代码里的名字。

## 0. 状态

2026-11-01：代码已按本文件执行完，判据全绿。DTA 路径、常驻呈现工具、三维作用线与
口袋深链接都已接上；`fit_affinity` 现在把选中那个 epoch 的验证集 Spearman 一并报出来。
判据见 `tests/test_agent.py`（3610 项）、`tests/test_smoke.py`（4254 项）、
`tools/check_prefix.py`、`tools/check_dta.py`、`tools/check_plip.py`、
`tools/check_web_ui.js`。仍待定的是微调门槛与训练顺序（§12）。

## 1. 已核对的事实

这些数字来自 `~/.vsagent/state.toml` 与 `runs/`，是这一轮改动的依据。

- 6 个根会话，`depth` 全为 0；`spawn_agent` 0 次，`agent_status` 0 次。
- `score_affinity` 0 次，`fit_affinity` 0 次；`affinity` 族一次没装过。
- ses_0005、ses_0006 合计 20 次 `load_tools`，没有一次装 `export`；两次合计
  12 次 `dock`、43 次 `analyze_pose`、3 次 `detect_pockets`、4 次 `define_pocket`，
  `write_run` 与 `open_viewer` 都是 0。
- 助手正文里提到口袋的较长条目有 32 条，带结构页地址的 2 条，正好来自装过
  `export` 的 ses_0001、ses_0002。
- `runs/` 下 25 份 `meta.json` 没有一份带 `contacts`；页面详情固定显示
  「这份运行没有接触数据」。
- 打包版里 `DTA` 路径指向 `/tmp/_MEI.../models/DTA`，该目录不存在；PLIP 导入失败
  （`ValueError: not enough values to unpack`），源码环境的同一次导入正常。

## 2. 三条已定决策

1. DTA 与对接打分同级，DTA 走「微调 → 推理」。
2. 相互作用在三维里呈现。
3. `write_run` 与 `open_viewer` 常驻核心；只要消息里出现具体的结构、口袋、位姿或
   相互作用，就要同时给出结构页地址。

## 3. 打分工具：局限性与适用性

每条说明分三块：适用、局限、结论。工具描述里只放压缩后的一小段，完整内容在本文件
与 `docs/screening-playbook.md`。工具描述每一步都要发给模型，不能写成整页。

### 3.1 `dock(scoring="vina")`

适用：

- 位姿预测与结构过滤：分子能不能进入口袋、朝向是否合理、关键氢键在不在。
- 已准备受体、已准备配体的大批量初步排序。
- 需要与历史结果比较时，同一受体、同一盒子、同一参数的重复运行。

局限：

- 经验打分函数，没有显式的库仑静电项与去溶剂化项；疏水、体积大的分子容易占便宜。
- 标准误差约 2.5 kcal/mol，1 kcal/mol 以内的差距读不出活性高低。
- 分数单位是 kcal/mol，它本身不等于结合自由能；只在本受体、本盒子、本力场、本参数内可比，
  不能跨靶点比较，不能替代实验活性。
- 对金属位点、强极性口袋、水介导的结合，排序容易错。

结论：只作结构证据与初步排序，不作亲和力结论。

### 3.2 `dock(scoring="vinardo")`

适用：

- 需要换一套权重看结论是否稳定：vina 对疏水与氢键方向的权重不合适时，vinardo 是
  一个现成的替代。
- 同一批配体、同一受体的力场敏感性检查。

局限：

- 仍是经验打分，没有显式静电项；对带电口袋、盐桥主导的位点解释力有限。
- 与 vina 的分数不可比；换力场是另一个产物目录，两批分数不能放进同一张表。
- 与 vina 一样，只在本受体、本盒子、本参数内可比。

结论：用来验证结论是否依赖某一个力场；不作亲和力。

### 3.3 `dock(scoring="ad4")`

适用：

- 需要静电、去溶剂化、氢键方向的项目；带电口袋、金属位点、盐桥主导的结合。
- 要检验一个静电或阳离子-π 假设时，ad4 的力场比 vina 更合适。

局限：

- 需要 `autogrid4` 先算亲和力图；图缓存按受体指纹、盒子、配体原子类型做键。
- 盒子边长限 40 Å，图的代价是边长的三次方；39 Å 实测约 17 秒算图，比 vina 慢。
- 图的原子类型来自这一批配体；换一批带新原子类型的配体要重算。
- 分数与 vina、vinardo 不可比；原 `autodock4` 程序没有接，1–5 分钟/配体，
  不适合大批量。

结论：小批量、带静电假设的复核；不作全库初筛。

### 3.4 `fit_affinity`

适用：

- 靶点有 ≥40 个带 pChEMBL 的实测化合物。
- 受体保留了与目标口袋对应的共晶配体；微调的口袋从这里截出。
- 要用结构模型给出亲和力排序，并愿意等 5–10 分钟。
- DTA 通道已打开，环境里能找到解释器与权重。

局限：

- 只对训练时的口袋有效；换口袋或换受体要重训，旧模型作废。
- 40–100 个活性时排序力有限：回顾性曲线是 100 个→ρ 0.04、300 个→0.18、
  726 个→0.39。
- 与「ECFP4 + LightGBM」打平，不超越；它的价值是与口袋、位姿共用一套结构解释。
- `mode=head` 只修绝对尺度，排序不变好；默认 `full`。
- 验证集按骨架切分，点少时 RMSE 波动大；RMSE 不代表排序力。
- 训练数据来自 ChEMBL，测定条件混杂；模型输出属于预测值，实验值另有来源。

结论：DTA 通道的固定前置步骤；它不保证比对接打分更准。

### 3.5 `score_affinity`（微调后）

适用：

- 与 `dock` 同一批位姿、同一受体、同一口袋，对前 50 条做 pK 预测。
- 与 dock 名次对照，找两路一致与背离的分子。
- 判断「同一批位姿换一套结构打分器，排序是否变化」。

局限：

- pK 是预测值，实验值另有来源；只对训练时的口袋有效，不能跨靶点或跨口袋比较。
- 对训练集覆盖不到的化学型外推弱；相似分子容易排成一片。
- 40–100 个活性时排序力有限，结论要标注样本量。
- 方向与 dock 相反：pK 越高越好，dock 的 kcal/mol 越低越好；两种分数不能直接比大小。
- 推理仍要 DTA 环境；整批默认 900 秒超时。

结论：第二路证据；与 dock 名次合并看，不单独当结论。

### 3.6 `score_affinity`（零样本）

零样本从流程与工具层一起移除：`score_affinity` 的 `model` 改为必填，不给就报错。
检查 DTA 环境是否可用，用 `tools/check_dta.py`。

局限：

- 没有靶点特异校准，分数容易挤在一起，排序弱。
- 不能当亲和力，不能与微调后的模型结果混在一张表里。

### 3.7 `rank_candidates`

适用：

- 把 `dock` 与 `affinity` 两路合并成一张候选表。
- 看两路一致性、分层、对照分子在哪一档。

局限：

- 平均秩只是一个排序量，没有物理单位；只对两路都出现的分子可靠，缺一路不补 0。
- 分层阈值是 1.0 kcal/mol 的并列带，分档间隔较宽。
- 排名只作原料，结论要另外写，不能替代实验。

结论：排序工具，不产生新的证据。

### 3.8 打分之后的评价工具

这些工具不产生结合分数，只回答「为什么」「像不像」「效率如何」。

- `analyze_pose`：接触、氢键、冲突、PLIP 指纹。局限是接触与氢键按重原子几何算，
  PLIP 依赖 openbabel；PLIP 没算成时要写明缺哪几类，不能给空表。
- `contact_map`：一份产物的全部位姿里残基接触频次。局限是只回答「挤不挤在同一个
  亚口袋」，不回答亲和力。
- `analyze_hydrophobic`：HP/HM 与脱水账。局限是几何计数，没有计算熵与脱水代价。
- `efficiency_profile`：LE / LELP / LLE。局限是同样没有计算熵与脱水代价。
- `validate_docking`：重对接到晶体位姿的 RMSD。局限是只对同一受体、同一参考位姿
  有意义；RMSD 小不等于打分对。

### 3.9 工具描述里的压缩文本

工具描述在装族之后每一步都要出现在请求里，只放下面这几段。长文留在 §3 与 playbook。

`dock` 的 `scoring` 段：

```
vina：经验打分，没有显式静电与去溶剂化，疏水、体积大的分子容易拿到更好的分数；只作位姿与结构过滤。
vinardo：vina 的权重变体，仍没有显式静电；分数与 vina 不可比。
ad4：有静电与去溶剂化，需 autogrid4 与 ≤40 Å 盒子，比 vina 慢；分数与 vina、vinardo 不可比。
```

`score_affinity` 的第一段：

```
DTA 通道用 fit_affinity 微调后的模型推理（model 必填）；pK 越高越好，只对训练时的口袋有效，
不能跨靶点或跨口袋比较。40–100 个活性时排序力有限。与 dock 比较名次，不比数值大小。
```

`fit_affinity` 的第一段：

```
DTA 通道的固定前置：用该靶点的实测活性微调，产出 kind=model，之后用 score_affinity(model=…) 推理。
少于 40 个带 pChEMBL 的化合物不训；只对训练时的口袋有效。
```

`rank_candidates` 的末段：

```
平均秩只是排序量；两路都出现的分子才可靠；分层间隔较宽；排名只作原料，结论要另外写。
```


## 4. 流程与确认点

### 4.1 顺序

1. `assess_campaign`：报告受体侧、配体侧与打分环境（vina、autogrid4、DTA 解释器、
   权重、带 pChEMBL 的条数、共晶配体在不在）。
2. 结构确认点：用户没有指定结构时先 `survey_structures` 把实验结构铺开，再由候选结构逐个核对。
3. 口袋确认点：`write_run(view=pockets)` 写运行目录，`open_viewer(pocket=…)` 给出
   直接打开该口袋局部视图的地址；用户看页面后选定口袋。
4. 准备受体：`prepare_receptor` 先预演再确认；保留与选定口袋对应的共晶配体。
5. 取活性：`search_actives` 产出带 pChEMBL 的配体集。
6. 微调：`fit_affinity` 先预演，报样本量、pK 范围、训练/验证切分、5–10 分钟耗时；
   用户确认后训练，产出 `kind=model` 的 artifact。
7. 准备候选配体，`dock` 对接。
8. 推理：`score_affinity(model=artifact#M, ligands=dock 产物)`，默认前 50 条。
9. 写运行目录：`write_run(docking=…, affinity=…, interactions="top")`；
   `open_viewer` 给出页面地址；`rank_candidates` 合并两路并给一致性。

### 4.2 四条必须满足的条件

- 微调口袋与对接口袋必须是同一处；换口袋或换受体，旧模型作废。
- 带 pChEMBL 的化合物少于 40 个时，DTA 通道不可用，只走 dock，并在消息里说明原因。
- 40–100 个活性时，DTA 结果要标注「排序力有限」，并报样本量与验证指标。
- 两路分数写进同一份运行目录；比较名次与背离的分子，不比数值大小。

### 4.3 确认点四：对接与打分

口袋、受体、配体集确认过才许调 `dock`。`dock` 跑完，对同一份产物调
`score_affinity(model=…)`。两路结果都进运行目录。训练与推理各会弹一次权限询问，
这条确认消息里要写明这两次询问。

## 5. 规则与前缀

### 5.1 前缀新增两段

`SCORING_DISCIPLINE`，放在 `CAMPAIGN_DISCIPLINE` 之后：

```
**打分是两路**：对接给位姿与结构过滤，DTA 给亲和力；两路都在 scoring 族里，装一次一起看。
DTA 通道固定走「先用该靶点的实测活性微调，再用微调后的模型推理」，不用零样本。
靶点有 ≥40 个带 pChEMBL 的化合物、受体留了共晶配体时这条通道才可用；少于此数只走对接单路，并说明原因。
微调 5–10 分钟，先预演再确认；训练完对同一批位姿打分。
两路分数写进同一份运行目录；比较名次与背离的分子，不比数值大小。
```

`PRESENT_DISCIPLINE`，放在 `HYPOTHESIS_CONTRACT` 之后：

```
**呈现**：消息里只要出现具体的结构、口袋、位姿、残基接触或相互作用，就同时给出对应的页面地址；没有地址就不要发这段描述。
写运行目录用 write_run，取地址用 open_viewer；地址必须来自工具，不要自己拼接。
只讲结构用 write_run(view=structure)；讲口袋用 view=pockets，地址直接打开选定口袋的局部视图。
对接与相互作用：三维页是主件，文字只写结论与最关键的 2–3 个作用；残基距离与作用类型由页面呈现。
同一受体、同一口袋的后续结果写回同一份运行目录，链接保持不变；换口袋或换受体才开新目录。
页面起不来时说明原因并给出编号与画像，不要用文字表冒充页面。
```

固定段现在是 1472 token（预算 1900），这两段在预算之内。`AGENTS.md` 不设上限，
确认点与判据写在那里；前缀只保留上面这些。

### 5.2 `AGENTS.md`

- 原则 2 改成「对接分数只回答位姿与结构过滤；亲和力由 DTA 通道给，先微调再推理」。
- 原则 5 的方法阶梯改成「2D 相似 → 形状 → 药效团 → 结构打分」。
- 原则 6 改成「具体的结构、口袋、位姿、相互作用描述必须带页面地址；没有地址不算交付」。
- 确认点二点名 `write_run(view=pockets)` 与 `open_viewer`，地址要直接打开该口袋的局部视图。
- 确认点四改名「对接与打分」，写清微调在推理前、两路分数进同一份运行目录。
- 新增「打分」与「相互作用」两节，细则放 `docs/screening-playbook.md` §4.6、§4.7。

### 5.3 `docs/screening-playbook.md`

- §2.2 展示：写 `write_run` → `open_viewer(pocket=…)`，给深链接。
- §4.4 上网页：两路分数一起写；`interactions="top"`。
- 新增 §4.6 亲和力打分：输入、预演、训练、推理、读法、失效条件。
- 新增 §4.7 相互作用：三维线、表格、PLIP 缺失时的说明。

## 6. 工具与族

- `docking`、`affinity`、`finetune` 合成 `scoring` 族，里面是 `dock`、
  `fit_affinity`、`score_affinity`。装一次族，三个工具一起可见。
- `write_run` 与 `open_viewer` 移出 `export` 族，常驻核心。`export` 族去掉。
- 核心工具从 16 个变成 18 个；每步多约 600 token 的工具定义。
- `write_run` 增加 `affinity` 与 `interactions` 两个参数。
- `open_viewer` 增加 `pocket`、`molecule`、`view` 三个参数，负责拼深链接。
- 每个打分工具的描述加一段压缩后的「适用 / 局限 / 结论」，长文留在本文件与
  playbook。

## 7. 网页

### 7.1 口袋局部视图

- 口袋清单点一行：上球棍与表面，镜头进入局部取景。
- `focusLoci` 传 `extraRadius: 6`、`minRadius: 12`、`zoomOut: true`，避免近裁剪面
  切开口袋。
- 保留「全景」按钮，回答「口袋在蛋白的哪一面」。
- `open_viewer(pocket="P2")` 给出
  `#/structure/<run_id>?pocket=P2&view=local`，打开就选中 P2 并放大。
- `web/app.js` 的 `parseHash` 同时解析查询串；`structure.js` 读 `pocket`、
  `molecule`、`view`。

### 7.2 三维相互作用

- 用 Mol* 的 `CustomInteractions` 与 `InteractionsShape`，接 `ShapeRepresentation3D`。
- 作用记录挂在 `meta.molecules[i].interactions`，不新增顶层键。
- 端点原子由写入时确定：几何氢键用 `_hbonds` 的供体/受体原子；PLIP 各类用行里的
  原子或该残基最近的重原子；π 堆积与阳离子-π 取环上的代表原子。
- 页面把 `aStructureRef` 设成受体结构引用、`bStructureRef` 设成该分子的位姿结构引用。
- 选中分子后画线，作用残基按类型着色，侧面板列出类型、残基、原子、距离、角度。
- 点表里的残基，镜头对到那个残基并高亮。
- 切换分子先清掉旧的作用线，再画新的。
- `open_viewer(molecule="mol-7")` 给出 `#/structure/<run_id>?molecule=mol-7`。
- PLIP 算不成时仍画几何接触与几何氢键，`provenance.poses.interactions.reason`
  写明缺哪几类。

## 8. 数据形状

`meta.json` 的顶层键保持不变，新增字段挂在已有键下面。

```
molecules[i].affinity_pk         pK 预测值，越高越好
molecules[i].contacts[]          residue / distance / atoms
molecules[i].interactions[]      kind / label / source / distance / angle / a / b
provenance.affinity              handle / model / aff_unit / pocket_radius / device
provenance.poses.interactions    mode / plip / computed / failed / reason
```

`interactions[]` 的 `a`、`b` 各是一个 `StructureElement.Schema`：

```
a: auth_asym_id / auth_seq_id / auth_comp_id / auth_atom_id
b: auth_asym_id / auth_seq_id / auth_comp_id / auth_atom_id
```

`kind` 用 Mol* 的取值：`hydrogen-bond`、`ionic`、`pi-stacking`、`cation-pi`、
`hydrophobic`、`halogen-bond`、`water-bridge`、`metal-coordination`。

## 9. 环境与打包

### 9.1 DTA 路径

`default_dta_root()` 按四级找：

1. `VSA_DTA_ROOT`
2. `<exe 所在目录>/models/DTA`
3. 源码仓根的 `models/DTA`
4. 当前工作目录的 `models/DTA`

找不到时报出试过的每一个路径与安装命令。venv 与权重不随 exe 打包；安装时把
`models/DTA` 放在 `dist/` 旁边。

`tools/check_dta.py` 打印解释器、代码路径、权重路径，并检查 `virtual_screen.py`
与 `fit_train.py` 能在该解释器下导入所需模块。

### 9.2 PLIP

`vsagent.spec` 收进 plip 与 openbabel 的数据、动态库；用一个真实复合物在冻结产物里
跑一次。跑不成时页面显示几何接触与几何氢键，并说明盐桥、π 堆积、卤键、水桥、
金属配位这几类没有算。

## 10. 判据与测试

- 前缀：两段都在；`write_run`、`open_viewer` 不属于任何族；`scoring` 族里三个工具
  齐全。
- DTA：一次 45 个位姿的运行，先产出 `kind=model`，再产出 `kind=affinity`；
  `meta.molecules` 同时有 `score` 与 `affinity_pk`；`rank_candidates` 输出
  Spearman、前 10 重合数、背离分子。
- 打包：`dist/models/DTA` 存在时 `tools/check_dta.py` 通过；不存在时给出一条
  可照做的安装说明。
- 口袋：深链接打开后 P2 是选中态，近裁剪面不切开残基，口袋占画面一半以上。
- 相互作用：三维作用线条数与 `meta.molecules[i].interactions` 一致；点表里的残基
  能对到三维；PLIP 缺失时页面写明缺哪几类。
- 呈现：`tools/check_prefix.py` 断言「没有地址就不要发这段描述」这句在；
  `tests/test_agent.py` 覆盖 `write_run` 的 `affinity` 与 `interactions`、
  `open_viewer` 的三种地址。
- 数据驱动：`tools/check_web_ui.js` 改掉 `meta.json` 里的靶点名、口袋标签与两处分值，
  重新打开后画面跟着变。写死一份演示数据时这一条会红。

## 11. 改动清单

- `vsagent/core/agent/prompt.py`：新增 `SCORING_DISCIPLINE` 与 `PRESENT_DISCIPLINE`。
- `AGENTS.md`：原则 2、5、6；确认点二、四；新增打分与相互作用两节。
- `docs/screening-playbook.md`：§2.2、§4.4、§4.6、§4.7。
- `vsagent/core/agent/domain/all_tools.py`：`scoring` 族；去掉 `export` 与 `finetune`。
- `vsagent/app.py`：`write_run`、`open_viewer` 常驻注册。
- `vsagent/core/agent/domain/docking.py`：`dock` 描述加三个力场的适用与局限。
- `vsagent/core/agent/domain/finetune.py`：`fit_affinity` 描述改成 DTA 通道前置；
  输出补验证集 Spearman（能算时）。
- `vsagent/core/agent/domain/affinity.py`：`score_affinity` 描述加适用与局限；
  `model` 改为必填，零样本移除；路径四级查找。
- `vsagent/core/agent/domain/export.py`：`affinity` 与 `interactions` 参数；
  每分子接触与作用写入 `meta.json`。
- `vsagent/core/agent/domain/interactions.py`：新建，接触、氢键、PLIP 的共用实现。
- `vsagent/core/agent/domain/analysis.py`：改用共用模块；`rank_candidates` 增加一致性统计。
- `vsagent/core/agent/domain/campaign.py`：盘点加「打分环境」一行。
- `vsagent.spec`：plip 与 openbabel 的数据、动态库。
- `web/app.js`：查询串与多段路径。
- `web/pages/structure.js`：口袋局部/全景、深链接、分子详情与作用线。
- `viz/src/viewer.ts`：带边距的局部取景、作用线绘制与清除、按类型着色。
- `tools/check_prefix.py`、`tools/check_web_ui.js`、`tools/check_dta.py`、
  `tools/check_plip.py`、`tests/test_agent.py`。数据驱动那一条（改 `meta.json` 看画面
  跟不跟着变）原本在 `tools/check_demo_datadriven.js` 里，那份脚本面向已经不再产出的
  静态演示页，已并入 `check_web_ui.js`。
- `CODEBUDDY.md`、`docs/source-map.md`：工具数、族数、核心工具数的说明。

## 12. 待定

- 微调样本门槛是否保持 40。当前工具里的 `MIN_COMPOUNDS` 是 40。
- 微调放在对接前还是对接后。当前方案放在对接前，推理在对接后；训练集与对接位姿
  没有依赖，两个顺序都成立。

## 13. 明确不做

- 不做二维相互作用图；三维线是主件，表格只作索引。
- 不新建独立的相互作用页；结构页的分子模式就是入口，地址为
  `#/structure/<run_id>?molecule=<id>`。
- 不把 DTA 的 venv（1.6GB）与权重（15GB）打进 exe；只找外部目录。
- 不接 `autodock4` 原程序、GNINA、WaterMap / GCMC；理由见
  `docs/scoring-tools-plan.md` §0 与 `docs/data-sources-plan.md` §4。
