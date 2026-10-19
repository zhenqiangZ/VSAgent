# 配体准备与对接闭环 — 实施计划

> 状态：第 1–7 步已实现并验证（2026-10-19）；第 8–9 步的全量回归与真实 Vina 小闭环已各跑过一次。
> 执行时按 §8 的顺序推进。

## 0. 目标与验收

把「配体导入 → 配体准备 → AutoDock Vina 对接 → 写运行目录 → 网页看排名与 pose」接通。

验收口径：

- `tests/test_agent.py` 新增 4 组判据全绿，原有断言只按预期改动。
- `tests/test_smoke.py` 全绿（它会重跑 `test_agent` 的全部断言）。
- 没有 Vina 的机器上测试仍全绿：所有对接测试替换模块级 `run_vina` / `resolve_vina`，不依赖真实二进制。
- 有 Vina 的机器上能手工跑通一次真实闭环：1 个受体 + ≤5 个配体。
- 网页端旧 `runs/1iep-demo` 的数据仍能打开，排序行为不变。

## 1. 已定口径（执行时不再讨论）

1. **引擎**：外部 AutoDock Vina。`VSA_VINA_BIN` 优先，否则 `PATH` 找 `vina`；缺失时返回带安装说明的失败。不进 exe，不加 Python 依赖。
2. **配体输入**：既接现有 `ligandset`（ChEMBL 结果），也支持文件导入。`import_ligandset` 是全库第二个收路径的动作，格式 `.sdf/.sd/.smi/.csv`。
3. **不新增 handle 类型**：`ledger.KINDS` 保持 `receptor/ligandset/artifact`。
   - raw 配体集：`ligandset` + `meta.stage="raw"`
   - prepared 配体集：`ligandset` + `meta.stage="prepared"`
   - 对接产物：`artifact` + `meta.kind="docking"`, `meta.stage="docking"`
4. **配体库不挂当前受体为父项**：否则换靶点时 `switch_receptor` 会把配体库归档。给 `SessionLedger.add` 增加 `link_current_receptor: bool = True`，两个配体工具传 `False`；`dock` 与 `write_run` 仍挂受体。
5. **`meta.json` 顶层键不变**。对接信息写 `provenance.poses`（已有键的内层）。
6. **只把最佳 pose 转成 PDB**；全部 mode 留在 `*_out.pdbqt`。前端仍只认 `format="pdb"`，不改 `viz/`。
7. **分数越低越好**。`rank` 与 `ligand_efficiency` 由代码算，绝不让模型算。
8. **`prepare_ligands` 两阶段**：`confirm=false` 只做解析/去盐/去重/性质统计（便宜），`confirm=true` 才做 3D 与 Meeko。预演要说明「实际成功数可能更少」。
9. **`dock` 默认要问一次**（`DEFAULT_ASK`），因为它跑外部二进制并写产物；`prepare_ligands` 与 `prepare_receptor` 一样只进 `PLAN_DENY`，靠 `confirm` 把关。
10. **`import_ligandset` 只收文件，不直接收 SMILES 字符串**（已确认）。单条 SMILES 写成 `.smi` 一行 `SMILES 名字` 再导入；这样工具接口、权限与来源记录都只有一种形状。

## 2. 数据流

```text
用户文件 ──import_ligandset──▶ ligandset#1 (raw)
ChEMBL CSV ───────────────────┘
                                   │
                          prepare_ligands (confirm)
                                   ▼
                         ligandset#2 (prepared)
                         ~/.opencode_tui/ligands/prepared/<tag>/ligands/*.pdbqt
                                   │
receptor#2 (prepared) ─────── dock ──▶ artifact#3 (docking)
  receptor.pdbqt + box.txt              poses/*.pdbqt + poses/*.pdb + logs/
                                   │
                          write_run(docking=artifact#3)
                                   ▼
                         runs/<id>/{meta.json, receptor.pdb, ligands/pose-XX.pdb}
                                   │
                             open_viewer → #/structure/<id>
```

## 3. 文件级改动

### 新增

| 文件 | 职责 |
| --- | --- |
| `opencode_tui/core/agent/domain/products.py` | 内容寻址产物目录、`params.json` 提交标记、原子写 JSON/文本。只放库函数，不登记工具。 |
| `opencode_tui/core/agent/domain/ligand.py` | `ImportLigandsetTool`、`PrepareLigandsTool`；文件解析、去盐、去重、ETKDGv3、Meeko。 |
| `opencode_tui/core/agent/domain/docking.py` | `DockTool`；定位 Vina、读盒子、构造命令、跑子进程、解析分数、PDBQT→PDB。 |
| `tests/fixtures/ligands.smi` | 给 `debug_tools.py` 的固定配体样例（含一条盐、一条无效 SMILES）。 |
| `tools/check_docking_ui.js` | 对接排名与详情降级的前端判据（Playwright，手工跑）。 |

`products.py` 关键函数：

```python
def content_dir(root: Path, stem: str, params: dict, required: Sequence[str]) -> tuple[Path, bool]
def has_product(directory: Path, required: Sequence[str]) -> bool
def write_json_atomic(path: Path, payload: dict) -> None
def write_text_atomic(path: Path, text: str) -> None
```

目录名 `<safe_stem>-<fingerprint_parts(params, 8)>`；同参数且 `required` 齐全 → 复用；参数不同 → 加 `-2`、`-3`，绝不覆盖。

### 修改

| 文件 | 改动 |
| --- | --- |
| `domain/ledger.py` | `SessionLedger.add(..., link_current_receptor: bool = True)`；自动挂当前受体的条件加上这个开关。 |
| `domain/__init__.py` | 纪律 1 改成「两个动作收路径」；模块表、`__all__` 加 `docking/ligand/products`。 |
| `domain/all_tools.py` | 顺序：`… receptor → ligand → docking → export`。 |
| `domain/export.py` | `write_run` 增加 `docking` 参数；`_write_docking_poses`、`_clean_pose_files`、`_docking_molecules`、`_docking_provenance`；`_build_meta` 增加可选 `poses`；`_write_json` 改原子写；报告加对接段。 |
| `domain/chemistry.py` | 缺依赖文案改成「准备受体、准备配体、对接结果转换要用」；文件头同步。 |
| `domain/structure.py` | `import_structure` 的 docstring/description 从「全库唯一」改成「与 import_ligandset 并列」。 |
| `core/agent/permission.py` | `PLAN_DENY` 加 `prepare_ligands`、`dock`；`DEFAULT_ASK` 加 `dock`；`PATH_ARGUMENTS` 加 `import_ligandset: ("path",)`，删掉死条目 `prepare_receptor: ("path",)`。 |
| `core/agent/prompt.py` | 第 253 行「全库只有 import_structure」改成两个动作。 |
| `web/pages/structure.js` | 新增 `rankedMolecules()`；替换 4 处排序（表格、绘制、Top3、loadRun）；详情页对 `contacts/pockets[0]/score/chain/pose` 降级；无 pose 的分子跳过。 |
| `debug_tools.py` | 调用 `load_env()`；`CALLS` 加三条与 `{ligandset:raw}`、`{ligandset:prepared}`、`{artifact:docking}`；模块/工具数量改成 10/18。 |
| `AGENTS.md` | 新增「第 4 关：对接」；137/171 行的「唯一收路径」改两个动作。 |
| `CODEBUDDY.md` | 工具数 15→18、模块表、`VSA_VINA_BIN`、`write_run(docking=...)`、Vina 不进 exe。 |
| `requirements-chem.txt` | 头注释说明配体准备与 pose 转换也要它；Vina 是外部二进制。 |
| `tools/check_web_api.py` | `prepare_runs()` 改用 `tests/fixtures/1iep.pdb` 作为 `receptor_full.pdb`，修掉对不存在的 `runs/1iep-demo` 的依赖。 |

不改：`core/web/routes.py`（`/runs/<id>/poses/*` 已能静态服务）、`viz/`、`opencode_tui.spec`。

## 4. 工具接口

### 4.1 `import_ligandset`

```json
{
  "path": "必填，.sdf/.sd/.smi/.csv，建议绝对路径",
  "name": "可选，显示名；留空用文件名",
  "smiles_column": "可选，CSV 列名；默认依次找 smiles/canonical_smiles/SMILES",
  "limit": "可选，默认 20000，上限 50000"
}
```

`primary_argument="path"`，`context_argument="ledger"`，`additionalProperties=false`。

行为：

- 只收文件；单条 SMILES 请写成 `.smi` 的一行（`SMILES 名字`，名字可省）。不做「直接粘贴 SMILES」的参数，避免多一条来源形状。
- 后缀白名单；文件 ≤64 MB、记录 ≤50000。
- CSV 用 `utf-8-sig`，失败回退 `gbk`；列名大小写不敏感；识别 `molecule_chembl_id`、`molecule_pref_name`、`pchembl_value`。
- SDF 用 `Chem.SDMolSupplier`；SMI 按「SMILES + 可选名字」，跳过空行与 `#`。
- 每条做去盐（最大片段）、重原子 ≥3、canonical isomeric SMILES 去重；失败逐条记 `reason`。
- 至少一条成功才登记；全部失败返回 `failed=True`，不登记。
- 同一内容（sha256）+ 同参数再次导入：内容寻址命中，返回原 handle，不重复登记。

### 4.2 `prepare_ligands`

```json
{
  "ligands": "必填，ligandset handle；stage=raw，或 ChEMBL 结果那种没有 stage 的",
  "limit": "默认 200，上限 2000；去重后按源顺序取",
  "seed": "默认 42，0 表示随机",
  "confirm": "默认 false；true 才写盘与登记"
}
```

`primary_argument="ligands"`，`context_argument="ledger"`。

行为：

- 源可以是 raw ligandset 目录（`meta.stage` 缺失或 `"raw"`），也可以是 ChEMBL 直接产出的 CSV 文件（`entry.path` 是文件，没有 stage）。`meta.stage=="prepared"` 的 handle 拒绝，并提示直接给 `dock`。
- 统一走 canonicalize + 去重，再按 `limit` 取。
- `confirm=false`：只解析/去盐/去重/算 MW、重原子、可旋转键；不建目录、不写盘、不登记；输出预演报告并说明实际 prepared 数可能更少。
- `confirm=true`：`Chem.AddHs` → ETKDGv3（失败回退 `useRandomCoords`）→ MMFF94（不可用回退 UFF）→ `MoleculePreparation().prepare(mol)` → `PDBQTWriterLegacy.write_string(setups[0])`。
- `prepare()` 返回 list：空 list 算失败；长度 >1 取第一个并在 manifest 记 `setups` 与警告。
- 单条失败不中断整批；至少一条成功才登记 prepared ligandset。
- 传 prepared handle → 失败并说明「已经准备过，直接给 dock」。

### 4.3 `dock`

```json
{
  "receptor": "必填，receptor handle，stage=prepared",
  "ligands": "必填，ligandset handle，stage=prepared",
  "exhaustiveness": "默认 8，1–64",
  "num_modes": "默认 9，1–20",
  "seed": "默认 42，0–2147483647",
  "cpu": "默认 0（全部），0–256",
  "energy_range": "默认 3.0，0.1–100",
  "limit": "默认 50，1–500",
  "timeout": "单配体秒数，默认 300，10–3600"
}
```

`primary_argument="receptor"`，`context_argument="ledger"`，`additionalProperties=false`。

不暴露：`ledger`、`VSA_VINA_BIN`、`vina_bin`、`box_center/box_size`、输出目录、内部文件路径。

描述第一段必须写：分数越低越好、只能同一受体/盒子内比较、不是结合自由能、缺 Vina 时返回安装指引。

## 5. 产物与 manifest

### 5.1 raw 配体集

```text
~/.opencode_tui/ligands/raw/<stem>-<sha8>/
  ligands.csv
  manifest.json
  params.json          # 最后写
```

`ligands.csv`：`id,name,smiles,input_smiles,heavy_atoms,pchembl_value,source_index`。

`manifest.json`：`kind,stage,tool,source{path,format,sha256,size},params,counts{input,accepted,invalid,duplicate,truncated,remaining},skipped[]`。

**所有 manifest / params 不写 `created_at` 这类时间戳**：登记表用 `fingerprint_path(产物目录)` 做内容指纹，时间戳会让同一份产物每次指纹都不同、重复导入无法命中。时间看目录 mtime。

### 5.2 prepared 配体集

```text
~/.opencode_tui/ligands/prepared/<stem>-<sha8>/
  ligands/<id>.pdbqt
  manifest.json
  params.json          # 最后写
```

`manifest.json`：

```json
{
  "kind": "ligandset", "stage": "prepared", "tool": "prepare_ligands",
  "source": {"handle": "ligandset#1", "fingerprint": "...", "path": "...", "count": 100},
  "engine": {"rdkit": "...", "meeko": "0.8.0"},
  "params": {"limit": 200, "seed": 42, "strip_salts": true, "dedupe": true,
             "forcefield": "MMFF94", "etkdg": "ETKDGv3"},
  "params_fingerprint": "...",
  "counts": {"input": 100, "prepared": 92, "invalid": 2, "duplicate": 1,
             "embed_failed": 2, "meeko_failed": 3, "truncated": 0, "remaining": 0},
  "records": [
    {"id": "CHEMBL25", "name": "aspirin", "smiles": "...", "heavy_atoms": 13,
     "source_index": 1, "status": "ok", "pdbqt": "ligands/CHEMBL25.pdbqt",
     "forcefield": "MMFF94", "setups": 1, "fragments_removed": 0, "error": ""}
  ]
}
```

### 5.3 docking artifact

```text
~/.opencode_tui/docking/<receptor-tag>-<ligandset-tag>-<sha8>/
  poses/<id>_out.pdbqt
  poses/<id>.pdb
  logs/<id>.log
  manifest.json
  params.json          # 最后写
```

`manifest.json`：

```json
{
  "kind": "docking", "stage": "docking", "tool": "dock",
  "engine": {"name": "vina", "binary": "/usr/bin/vina", "version": "1.2.5"},
  "receptor": {"handle": "receptor#2", "fingerprint": "...",
               "box": {"center": [x,y,z], "size": [sx,sy,sz], "source": "box.txt"}},
  "ligands": {"handle": "ligandset#2", "fingerprint": "...", "requested": 50, "used": 50},
  "params": {"exhaustiveness": 8, "num_modes": 9, "seed": 42, "cpu": 0,
             "energy_range": 3.0, "limit": 50, "timeout": 300.0},
  "params_fingerprint": "...",
  "counts": {"input": 50, "docked": 48, "pdb": 47, "failed": 2, "remaining": 0,
             "best_score": -9.1},
  "results": [
    {"id": "CHEMBL25", "name": "aspirin", "smiles": "...", "heavy_atoms": 13,
     "score": -9.1, "rmsd_lb": 0.0, "rmsd_ub": 0.0, "ligand_efficiency": 0.7,
     "rank": 1, "modes": 9, "pose_pdbqt": "poses/CHEMBL25_out.pdbqt",
     "pose_pdb": "poses/CHEMBL25.pdb", "log": "logs/CHEMBL25.log",
     "status": "ok", "error": ""}
  ],
  "failures": [
    {"id": "LIG-0002", "reason": "timeout", "detail": "...", "log": "...", "log_excerpt": "..."}
  ]
}
```

`params.json` 放受体/配体指纹、盒子、全部参数、引擎路径与版本、`params_fingerprint`，不放时间戳。

计数口径：`results` 放每一次跑过 Vina 的配体（`status=ok` 或 `no_pdb`）；`failures` 放所有没有可用 pose 的配体（Vina 失败与 `no_pdb`）；`counts.docked` = Vina 成功数，`counts.pdb` = 成功转出 PDB 的数，`counts.failed` = `failures` 长度。

## 6. 关键实现细节

### 6.0 SMILES → 结构：四段转换与边界

```text
SMILES → MolFromSmiles（拓扑图）
       → 去盐（最大片段）+ canonical isomeric SMILES 去重
       → AddHs → ETKDGv3（固定 seed）→ MMFF94/UFF
       → Meeko：AutoDock 原子类型 + 可旋转键 + 部分电荷 → PDBQT
       → Vina → 最佳 pose PDBQT → RDKit 重建 → PDB → 网页
```

- 网页显示的是**对接 pose**，不是 ETKDG 生成的输入构象；输入构象只是 Vina 的起点。
- 手性中心与双键构型按 SMILES 原样：没写 `@` / `/` / `\` 的不会被补全，对接前应给明确的异构体。
- 质子化状态按 SMILES 原样，不做 pH 推断；互变异构体不枚举；盐只保留最大片段。
- SDF 里自带的 3D 坐标不保留，统一用固定 seed 重新生成，保证可复现。

### 6.1 配体解析与准备

- 所有 RDKit / Meeko import 都在函数体内，模块顶层只 import stdlib 与本包；`require_chem()` 在 `run()` 开头。
- 去盐：`Chem.GetMolFrags(asMols=True)`，取重原子数最多（并列取精确分子量更大）的片段。
- 去重键：`Chem.MolToSmiles(main, isomericSmiles=True, canonical=True)`；重复保留第一条，记 `duplicate_of`。
- 3D：`ETKDGv3` + `randomSeed=seed`；`EmbedMolecule == -1` 时 `useRandomCoords=True` 再试一次；仍失败记 `embed_failed`。
- 力场：`MMFFHasAllMoleculeParams` → `MMFFOptimizeMolecule(maxIters=200)`；否则 `UFFOptimizeMolecule`；都不可用保留 ETKDG 坐标并记 `forcefield="none"`。
- Meeko：`MoleculePreparation()` 循环外建一次；`setups = prep.prepare(mol)`；空列表失败；`pdbqt, ok, err = PDBQTWriterLegacy.write_string(setups[0])`。
- ID 文件名清洗：非 `[A-Za-z0-9._-]` 替换为 `_`，截 64 字符；重复 ID 加 `-2`；空 ID 用 `LIG-0001`。
- SDF 里的 3D 坐标不保留：导入只留化学结构，`prepare_ligands` 用固定 seed 重新生成，保证可复现。

### 6.2 Vina 调用

- `resolve_vina()`：`VSA_VINA_BIN` 非空时校验文件与可执行位，不可用直接失败，不静默回退；否则 `shutil.which("vina")`；都没有返回安装指引。
- `vina_version()`：`[binary, "--version"]`，5 秒超时；失败记 `"unknown"`，不阻断。
- `read_box()`：优先 `receptor.meta["box_file"]` 的 `box.txt`，回退 `meta.params` 的 `box_center/box_size`；六个值必须有限、`size > 0` 且 ≤126 Å。
- 命令用 `asyncio.create_subprocess_exec(*argv)`，绝不拼 shell；参数全部数组化。
- `argv` 固定包含 `--receptor/--ligand/--center_x..z/--size_x..z/--exhaustiveness/--num_modes/--seed/--cpu/--energy_range/--out`。
- POSIX `start_new_session=True`；超时/取消都走 `terminate_tree` + `await collect` + `release_pipes`（复用 `tools.py`）。
- 单配体失败不中断整批：`timeout`、`vina_exit_<code>`、`no_output`、`no_result`、`score_parse_failed`、`no_pdb` 分别记录。
- 分数解析正则匹配 `REMARK VINA RESULT:`，每行一个 mode；第一条是最佳。按 score 升序（越负越前），`id` 做稳定 tie-break；`rank` 从 1 开始。
- `ligand_efficiency = round(-score / heavy_atoms, 3)`；`heavy_atoms <= 0` 或分数非有限时 `None`。
- PDBQT→PDB：`PDBQTMolecule.from_file(path, skip_typing=True)`（Vina 输出已是 AutoDock 类型，跳过重新指派）→ `RDKitMolCreate.from_pdbqt_mol(...)[0]` → `Chem.MolToPDBBlock(mol, confId=0)`；`skip_typing=True` 失败时再用默认值重试一次；仍失败记 `no_pdb`，保留 PDBQT 与日志。
- 日志单文件上限 512 KB（保留前 64 KB + 后 64 KB）；`log_excerpt` 取最后 20 行、≤2000 字符；工具返回每组失败只列前 5 条。

### 6.3 `write_run` 扩展

- 新参数 `docking`：`ArtifactHandle`，`meta.kind=="docking"`，`manifest.json` 可读。
- `docking` 与 `ligands` 同时给 → 失败。
- 校验 `manifest.receptor.fingerprint == receptor_entry.fingerprint`；handle 不同但指纹相同允许，并在报告里说明。
- 至少一条 `status=="ok"` 且 `pose_pdb` 存在，否则失败。
- 复制：只复制 `pose_pdb`，按 rank 重命名为 `ligands/pose-01.pdb`、`pose-02.pdb`；源路径必须 `is_relative_to(artifact_root)`。
- 清理：先复制到 `ligands/.staging-<pid>/`，全部成功后再删旧 `ligands/pose-*.pdb` 并 `os.replace` 就位；失败不动旧 meta/旧 pose。
- `meta.molecules`：`{id,name,score,pose,chain,rank}`；`score` 非有限则跳过并计数；`chain` 取受体 `meta.chains[0]`，没有就 `""`。
- `provenance.poses`：`{source:"vina", engine, score_direction:"lower_is_better", score_unit:"kcal/mol", ranked_by:"score", receptor, ligandset, docking, params, counts}`。
- 无 `docking` 时 `molecules` 写 `[]`（不再写前端无法消费的 `{handle,name,file:""}` 占位）；`ligands` 仍用于找参考配体。
- 报告加一段：写入多少 pose、最佳分数与分子、失败多少、分数方向与可比范围。
- `_write_json` 改成同目录临时文件 + `os.replace`，保证 `meta.json` 是最后提交。

### 6.4 前端

新增：

```javascript
function rankedMolecules() {
  // 有 rank 按 rank；否则按 provenance.poses.score_direction；
  // 旧数据没有该字段时沿用「高分在前」
}
```

- 表格、`drawMolecules`、Top3、`loadRun` 四处都用它。
- `showDetail`：`score` 非有限显示 `—`；`chain/pose` 缺失显示 `—`；`meta.pockets[0]` 不存在显示 `—`；`meta.contacts` 不是数组时按空表处理并显示「这份运行没有接触数据」。
- `loadRun` 跳过 `molecule.pose` 缺失的分子，避免一个坏条目让 `window.__ready` 永远为假。
- 旧 demo（`provenance.poses` 是字符串、没有 `rank`）仍按分数从高到低。

### 6.5 权限与提示

- `PLAN_DENY`：`prepare_receptor`、`prepare_ligands`、`dock`、`write_run`。
- `DEFAULT_ASK`：`write`、`patch`、`bash`、`dock`。
- `import_ligandset` 像 `import_structure` 一样放行，但 `PATH_ARGUMENTS` 必须登记 `("path",)`，越界时仍会问。
- `AGENTS.md` 第 4 关：口袋确认 + 受体 confirm + 配体 prepare confirm 之后才能 dock；先报盒子/配体数/参数让用户确认；分数只来自引擎；必须报最佳若干条、LE、失败/超时；最后 `write_run(docking=...)` + `open_viewer`。

## 7. 测试计划

### 新增检查组（`tests/test_agent.py`）

`("ligand-import", check_ligand_import)`
替换 `ligand_module.ligands_dir`、`canonicalize_smiles`、`_read_sdf`、`chemistry._probe_deps`。断言：
1. CSV 导入成功，登记 `LigandSetHandle`，`meta.stage=="raw"`，`path` 是目录。
2. `molecule_chembl_id` / `molecule_pref_name` 成为 `id` / `name`。
3. canonical 重复只留一条，`counts.duplicate==1`。
4. 无效 SMILES 跳过，其余成功。
5. 全部无效 → `failed=True`，不登记。
6. 不支持后缀失败，错误列出 `.sdf/.sd/.smi/.csv`。
7. 同文件同参数二次导入返回同一 handle。
8. `limit` 截断文案含「还剩」与具体条数。
9. `ledger` 不在 schema，`path` 是 required。

`("ligand-prep", check_ligand_prepare)`
替换 `ligands_dir`、`canonicalize_smiles`、`prepare_molecule`、`chemistry._probe_deps`。断言：
1. `confirm=false` 不建目录、不写文件、不登记，输出含「预演」。
2. `confirm=true` 写出 `manifest.json`、`params.json`、`ligands/*.pdbqt`。
3. 盐被去掉，`fragments_removed==1`。
4. canonical 重复只准备一次。
5. 一条 Meeko 失败其余仍成功，`counts.meeko_failed==1`，整批成功。
6. 全部失败 → `failed=True`，不登记。
7. `limit` 截断文案含「还剩」。
8. 同参数二次调用复用，`prepare_molecule` 调用次数不增加。
9. 换 `seed` 生成新目录，旧产物仍在。
10. 删掉 `params.json` 后不复用旧目录。
11. raw handle 可准备；prepared handle 被拒。
12. 缺化学依赖时失败，不写盘。

`("docking", check_docking)`
替换 `dockings_dir`、`resolve_vina`、`run_vina`、`best_pose_to_pdb`、`chemistry._probe_deps`。断言：
1. 缺 Vina 时失败，错误含 `VSA_VINA_BIN` 与安装说明。
2. raw 受体/raw 配体集被拒绝。
3. `box.txt` 解析正确；缺键或 `size<=0` 失败。
4. `build_command()` 返回 list，含全部必需参数；源码无 `create_subprocess_shell`。
5. 多 mode 解析与升序 rank 正确。
6. 非零退出/超时/无 REMARK 各一条，其余成功；`failures` 原因正确。
7. 全部失败 → `failed=True`，不登记。
8. `ligand_efficiency` 计算正确。
9. 同参数二次调用复用，`run_vina` 调用次数不增加。
10. 产物目录含 `poses/*.pdbqt`、`poses/*.pdb`、`logs/*.log`、`manifest.json`、`params.json`。
11. `run_vina([python, "-c", "time.sleep(5)"], timeout=0.2)` 返回 `timed_out=True`。
12. artifact `meta.kind=="docking"`、`meta.stage=="docking"`，父项含受体与配体集。

`("write-run-docking", check_write_run_docking)`
替换 `export_module.runs_dir` 与 `pockets_module`，手工构造 docking artifact。断言：
1. `meta.json` 顶层键与 `demo-meta.json` 去掉 `contacts` 后完全一致（无新顶层键）。
2. `meta.molecules` 字段为 `{id,name,score,pose,chain,rank}`，`pose` 指向真实文件。
3. `provenance.poses.score_direction=="lower_is_better"`，含 engine/receptor/ligandset/docking/counts。
4. 同 `run_id` 二次写更少分子 → 旧 `pose-02/03.pdb` 被删，molecules 数量同步。
5. manifest 指向不存在的 pose → 失败，旧 `meta.json` 字节不变。
6. 受体指纹不匹配 → 失败，不写 meta。
7. 同时给 `ligands` 与 `docking` → 失败。
8. 无 docking 的旧路径仍写 `molecules==[]`，`provenance` 无 `poses`。
9. `status="no_pdb"` 的记录被跳过并计数。
10. `receptor.pdb` 与 `meta.json` 仍按原逻辑写出。

### 既有断言修改

- `check_workspace`：工具数 `>=18`；新增三个工具名必须出现。
- `check_structure_tools`：`path_takers == ["import_structure", "import_ligandset"]`。
- `check_plan_deny`：`import_ligandset` 放行；`prepare_ligands`/`dock` 拒绝。
- `check_prompt`：系统提示同时出现两个收路径动作。
- `check_chemistry_gate`：新工具描述含 `CHEM_TOOL_DESCRIPTION_NOTE`；新模块顶层无 RDKit/Meeko。
- `check_permission` / smoke 权限轮次：`dock` 默认会问，`prepare_ligands` 不额外问。

### 前端

- 旧 `node tools/check_demo_ui.js`、`check_demo_datadriven.js` 必须仍绿。
- 新 `node tools/check_docking_ui.js` 自建 `.probe/docking-ui/` 运行目录：`meta.molecules` 里 `rank` 与 `score` 故意乱序、`score` 为负、`provenance.poses.score_direction="lower_is_better"`，且不给 `contacts`、不给 `pockets`、`chain` 缺失；用 `web/` 静态服务打开，断言：
  1. 首行是 rank 1（或最负分数）；
  2. 点详情不抛异常，缺失字段显示 `—`；
  3. 旧 demo（无 `rank`、`provenance.poses` 为字符串）仍按高分在前。

## 8. 实施顺序与验收

1. **ledger 开关 + products.py**：`add(link_current_receptor=False)` 与新工具函数。跑 `--only workspace`。
2. **import_ligandset**：`ligand.py` 的导入半部 + `ligand-import` 判据。跑 `--only ligand-import`。
3. **prepare_ligands**：3D/Meeko/复用 + `ligand-prep` 判据。跑 `--only ligand-prep`。
4. **dock**：`docking.py` + `docking` 判据。跑 `--only docking`。
5. **write_run 接入**：`docking` 参数、复制/清理、`provenance.poses` + `write-run-docking` 判据。跑 `--only write-run`。
6. **前端**：排序与降级 + `check_docking_ui.js`。跑旧的两个 node 判据与 `node tools/check_docking_ui.js`。
7. **权限/提示/文档/调试**：`permission.py`、`prompt.py`、`structure.py`、`chemistry.py`、`debug_tools.py`、`AGENTS.md`、`CODEBUDDY.md`、`requirements-chem.txt`；修 `check_web_api.py` 的 1IEP 夹具依赖。跑 `--only plan-deny`、`--only prompt`、`--only loop`、`debug_tools.py --list`。如果 `check_loop` 的「历史降到窗口八折以内」因提示变长而超支，按惯例提高 `tests/test_agent.py` 里的 `COMPACT_WINDOW`，**不要**放宽那条比例断言。
8. **全量回归**：`tests/test_agent.py`、`tests/test_smoke.py`、`tools/check_web_api.py`。
9. **真实 Vina 手工闭环**（有二进制时）：`import_ligandset` → `prepare_ligands(confirm=true)` → `dock(limit≤5)` → `write_run(docking=...)` → `open_viewer`。没有 Vina 时 `debug_tools.py` 全跑会在 `dock` 这一步失败，这是预期的环境缺失，不是代码回归；`--list` 仍应正常。

## 9. 风险与规避

1. **Meeko `prepare()` 返回 list**：空 list 失败，`[0]` 才交给 `write_string`；长度 >1 记警告。
2. **Vina 子进程泄漏**：`create_subprocess_exec` + POSIX `start_new_session`；超时/取消都 `terminate_tree` + `await collect` + `release_pipes`。
3. **前端旧格式**：`provenance.poses` 可能是字符串；所有缺失字段降级；无 pose 的分子跳过；旧排序不变。
4. **`write_run` 顶层键锁死 + 同名目录残留**：对接只写 `provenance.poses`；staging 复制成功后删旧 `pose-*.pdb`；`meta.json` 原子写。
5. **路径与依赖闸**：`import_ligandset` 进 `PATH_ARGUMENTS`；新模块顶层不 import RDKit/Meeko；`prepare_ligands`/`dock` 进 `PLAN_DENY`，`dock` 进 `DEFAULT_ASK`。
6. **配体库被受体切换归档**：`add(link_current_receptor=False)`，并用判据覆盖「换受体后 raw/prepared 配体集仍在」。
