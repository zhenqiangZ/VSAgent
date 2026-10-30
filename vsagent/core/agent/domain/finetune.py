"""靶点内微调：用已测活性把 VCrossFormer 适配到当前受体。

这个模块是领域工具里**第三个调外部进程**的，也是最重的一个：训练在
`models/DTA/.venv` 里跑（torch + dgl），CPU 上 5–10 分钟。主仓一行都不 import 它们。

## 什么时候该用

只有一种情形：**这个靶点上已经有几百个实测活性，而零样本 `score_affinity` 排不出序。**

回顾性实验（`docs/retro-affinity-report.md` §九）在 PTP1B 上的读数：

| 训练化合物 | 测试集 ρ（26 个留出骨架） | 验证 RMSE |
|---|---|---|
| 零样本 | +0.077 | 1.66 |
| 100 | +0.036 | 0.53 |
| 300 | +0.178 | 0.49 |
| 726 | **+0.385**（3 种子 0.384 ± 0.017） | 0.50 |

三件事必须同时记住：

1. **排序随数据量单调变好，但只有全量微调拿得到。** head-only（只训 `affinity_mlp`）
   把 RMSE 从 1.66 压到 1.16，ρ 却从 0.077 掉到 0.045——它学的是「这个靶点的活性
   大概在什么档位」，不是「谁比谁强」。
2. **它不会超过便宜的 2D 模型。** 微调后与 ECFP + LightGBM 打平（Δρ −0.024，
   CI [−0.27, +0.26]），而后者训练只要几秒。所以「几百个化合物、只要能排序」这个问题，
   先试 2D；微调的价值在于它给的是**结构模型**（可解释、可看口袋、可与对接共用坐标）。
3. **只对训练的这个口袋有效。** 一个靶点一份口袋，换靶点必须重训。
"""

from __future__ import annotations

import asyncio
import csv
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    LigandSetHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_parts,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import affinity, pdbtext, products
from .docking import read_box

CONFORMER_SEED = 42
POCKET_RADIUS = 10.0
"""与训练一致：残基任一原子落在配体 10 Å 内就整条保留。改它模型就见另一个分布。"""

MIN_COMPOUNDS = 40
"""少于这个数直接拒绝。实验里 100 个训练点才是 ρ=0.04 的水平，40 个只够跑通流程。"""

DEFAULT_EPOCHS = 20
DEFAULT_LR = 1e-5
DEFAULT_BATCH = 32
TRAIN_TIMEOUT = 5400.0
VAL_FRACTION = 0.2

def work_root() -> Path:
    """一次微调的全部产物。与 `score_affinity` 的产物同处一个家目录。"""
    return Path.home() / ".vsagent" / "finetune"


# ---------------------------------------------------------------------------
# 数据准备（与实验脚本共用同一份实现）
# ---------------------------------------------------------------------------
def cut_pocket(
    doc: "pdbtext.PdbDocument",
    ligand: Sequence[tuple[float, float, float]],
    radius: float = POCKET_RADIUS,
) -> tuple[str, int, int]:
    """按训练时的规则截口袋：**残基的任一原子落在配体 radius 内，就整条保留**。

    不能用「以质心为球心的球」：细长配体上两者差一倍（实测 1YPK 的 42 重原子配体，
    质心球留 217 个原子、配体表面留 602 个，而训练口袋的原子数中位数是 630）。
    """
    kept: set[Any] = set()
    for atom in doc.atoms:
        if atom.is_hetero:
            continue
        for x, y, z in ligand:
            if (
                abs(atom.x - x) <= radius
                and abs(atom.y - y) <= radius
                and abs(atom.z - z) <= radius
                and ((atom.x - x) ** 2 + (atom.y - y) ** 2 + (atom.z - z) ** 2)
                <= radius * radius
            ):
                kept.add(atom.key)
                break
    doomed = [
        line.index
        for line in doc.lines
        if line.atom is not None and line.atom.key not in kept
    ]
    trimmed = doc.drop(doomed)
    return trimmed.text, len(kept), len(trimmed.atoms)


def embed_conformers(
    records: Sequence[dict[str, Any]], directory: Path, *, seed: int = CONFORMER_SEED
) -> tuple[dict[str, Path], list[str]]:
    """每个分子生成一个 3D 构象落盘；已有且 SMILES 没变就复用。

    **必须落盘**：训练脚本是子进程，只能读文件。ETKDGv3 嵌一次、MMFF 优化一次
    （没有 MMFF 参数时退 UFF，再不行就用嵌入出来的构象——模型要的是坐标，不要求
    构象是能量极小点）。
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}

    from rdkit import Chem
    from rdkit.Chem import AllChem

    paths: dict[str, Path] = {}
    failed: list[str] = []
    for record in records:
        cid = str(record["id"])
        smiles = str(record["smiles"])
        cached = manifest.get(cid) or {}
        target = directory / f"{_safe_name(cid)}.sdf"
        if cached.get("smiles") == smiles and target.is_file():
            paths[cid] = target
            continue

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            failed.append(cid)
            continue
        mol = Chem.AddHs(mol)
        params = AllChem.ETKDGv3()
        params.randomSeed = seed
        if AllChem.EmbedMolecule(mol, params) != 0:
            params.useRandomCoords = True
            if AllChem.EmbedMolecule(mol, params) != 0:
                failed.append(cid)
                continue
        try:
            AllChem.MMFFOptimizeMolecule(mol, maxIters=200)
        except Exception:  # 没有 MMFF 参数：退 UFF，再不行保留嵌入构象
            try:
                AllChem.UFFOptimizeMolecule(mol, maxIters=200)
            except Exception:
                pass
        writer = Chem.SDWriter(str(target))
        writer.write(mol)
        writer.close()
        manifest[cid] = {"smiles": smiles, "file": target.name}
        paths[cid] = target

    products.write_json_atomic(manifest_path, manifest)
    return paths, failed


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)


def read_actives(path: Path) -> list[dict[str, Any]]:
    """从 `search_actives` 写下的活性表里取 (id, SMILES, pChEMBL)。

    只认有 `pchembl_value` 列的表。`find_analogs` 与 `import_ligandset` 的产物没有
    实测活性，训不了——这里如实返回空表，由调用方给出一句能照着做的话。
    """
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "pchembl_value" not in reader.fieldnames:
            return []
        for row in reader:
            smiles = str(row.get("smiles") or "").strip()
            raw = row.get("pchembl_value")
            try:
                pk = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not smiles or pk != pk:
                continue
            identifier = str(row.get("parent_molecule_chembl_id")
                             or row.get("molecule_chembl_id") or "").strip()
            rows.append({"id": identifier or smiles, "smiles": smiles, "pchembl": pk})
    return rows


def scaffold_split(
    records: Sequence[dict[str, Any]], *, fraction: float = VAL_FRACTION, seed: int = 42
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 Murcko 骨架整组切开，取一份做验证集。

    **不能随机切。** 随机切会让同一骨架同时出现在两侧，验证指标虚高 0.11–0.51
    （实验 §5.5）。这里按骨架分组、整组进同一侧，验证数字才对得上「新骨架」。
    """
    from rdkit import Chem
    from rdkit.Chem.Scaffolds import MurckoScaffold

    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        mol = Chem.MolFromSmiles(str(record["smiles"]))
        key = "（无环）"
        if mol is not None:
            try:
                key = MurckoScaffold.MurckoScaffoldSmiles(mol=mol, includeChirality=False) or key
            except Exception:
                key = "（无环）"
        groups.setdefault(key, []).append(record)

    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    total = len(records)
    train: list[dict[str, Any]] = []
    val: list[dict[str, Any]] = []
    for _key, items in ordered:
        if len(val) < total * fraction:
            val.extend(items)
        else:
            train.extend(items)
    if not train or not val:  # 骨架太少时退化成按分子切，并让调用方知道
        ordered_records = sorted(records, key=lambda item: item["id"])
        cut = max(1, int(len(ordered_records) * (1 - fraction)))
        return ordered_records[:cut], ordered_records[cut:]
    return train, val


def write_index(path: Path, records: Sequence[dict[str, Any]]) -> None:
    """写 PDBbind INDEX 格式：第一列 id、第四列 pK、行里带 `Ki=` 标注。"""
    lines = ["# 单靶点微调数据集（fit_affinity 生成）"]
    for record in records:
        pk = float(record["pchembl"])
        lines.append(
            f"{record['id']}  2.00  2020  {pk:.3f}  Ki={10 ** (-pk) * 1e9:.1f}nM      "
            f"// {record['id']}.pdf (LIG)"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class FitAffinityTool(Tool):
    """在某个靶点上微调 VCrossFormer。"""

    spec = ToolSpec(
        name="fit_affinity",
        description=(
            "用**已测活性**把 VCrossFormer 微调到这个受体上，产出登记成 artifact"
            "（kind=model），之后用 `score_affinity(model=…)` 打分。"
            "训练在 DTA 的独立环境里跑，CPU 上 5–10 分钟。默认只预演，confirm=true 才真跑。\n"
            "**什么时候用**：零样本 `score_affinity` 在这个靶点上排不出序（分数挤成一团、"
            "与实测对不上），且该靶点已有几百个精确测量的化合物。"
            "回顾性实验的曲线是 100 个→ρ 0.04、300 个→0.18、726 个→0.39，单调，"
            "所以少于 40 个直接拒绝。\n"
            "**不要指望它超过便宜的方法**：实测微调后与「ECFP4 + LightGBM」打平"
            "（Δρ −0.02，CI 含 0），而后者几秒训完。几百个化合物时先试 2D；"
            "微调的独有价值是它给的是结构模型——可与口袋、对接坐标共用一套解释。\n"
            "**只有全量微调能恢复排序**：只训最后一层（mode=head）会把 RMSE 从 1.66 压到 "
            "1.16，但排序反而略降（ρ 0.077→0.045），因为它学的是「这个靶点大概在什么档位」。\n"
            "**只对这个口袋有效**，换靶点必须重训。口袋从准备受体留下的共晶配体截出"
            "（残基任一原子落在配体 10 Å 内即整条保留，与训练一致）。\n"
            "需要 `search_actives` 产出的带 pchembl 的配体集；`find_analogs` / "
            "`import_ligandset` 的产物没有实测值，训不了。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "receptor": {
                    "type": "string",
                    "description": "已准备的受体 handle（receptor#2），必须留了共晶配体",
                },
                "ligands": {
                    "type": "string",
                    "description": (
                        "带实测活性的配体集 handle（ligandset#3），来自 search_actives；"
                        "必须有 pchembl 列"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["full", "head"],
                    "description": (
                        "full=训全部参数（默认，能恢复排序）；"
                        "head=只训最后一层（只修绝对尺度，排序不变好）"
                    ),
                },
                "epochs": {
                    "type": "integer",
                    "description": f"训练轮数，默认 {DEFAULT_EPOCHS}；600 个化合物约 20 轮收敛",
                },
                "lr": {
                    "type": "number",
                    "description": f"学习率，默认 {DEFAULT_LR:g}；再大一点就会把预训练权重冲掉",
                },
                "seed": {"type": "integer", "description": "训练种子，默认 444"},
                "confirm": {
                    "type": "boolean",
                    "description": "false（默认）只预演并报告将训练多少条；true 才真跑",
                },
            },
            "required": ["receptor", "ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "receptor"
    # 训练用的是「受体 × 配体集 × 超参」这一组东西，换任何一项都是另一条证据。
    evidence_key = ("ligands",)
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：fit_affinity 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw_receptor = str(arguments.get("receptor") or "").strip()
        raw_ligands = str(arguments.get("ligands") or "").strip()
        if not raw_receptor or not raw_ligands:
            return ToolResult(
                output="缺少参数：receptor 与 ligands 都要给。"
                       "先 list_workspace 看清手上有什么。",
                failed=True,
            )

        mode = str(arguments.get("mode") or "full").strip().lower()
        if mode not in ("full", "head"):
            return ToolResult(output=f"mode 只能是 full / head，收到 {mode!r}", failed=True)
        try:
            epochs = int(arguments.get("epochs") or DEFAULT_EPOCHS)
            lr = float(arguments.get("lr") or DEFAULT_LR)
            seed = int(arguments.get("seed") or 444)
        except (TypeError, ValueError) as error:
            return ToolResult(output=f"参数不是数字：{error}", failed=True)
        if not 1 <= epochs <= 200:
            return ToolResult(output=f"epochs 要在 1–200 之间（现在是 {epochs}）", failed=True)
        if not 1e-6 <= lr <= 1e-2:
            return ToolResult(output=f"lr 要在 1e-6 到 1e-2 之间（现在是 {lr}）", failed=True)
        confirm = bool(arguments.get("confirm"))

        try:
            receptor = ledger.get(raw_receptor)
            expect_handle(receptor, ReceptorHandle, "receptor")
            ligands = ledger.get(raw_ligands)
            expect_handle(ligands, LigandSetHandle, "ligandset")
            receptor_pdb, _center, ligand_coords = _receptor_inputs(receptor)
            actives = read_actives(Path(ligands.path))
        except Exception as error:  # LedgerError / ToolError / OSError
            return ToolResult(output=str(error), failed=True)

        if not actives:
            return ToolResult(
                output=(
                    f"{ligands.handle} 里没有可用的实测活性（需要 pchembl 列）。"
                    "用 search_actives 取已知活性；find_analogs 与 import_ligandset "
                    "的产物只有结构、没有活性值，训不了。"
                ),
                failed=True,
            )
        if len(actives) < MIN_COMPOUNDS:
            return ToolResult(
                output=(
                    f"{ligands.handle} 只有 {len(actives)} 个带活性的化合物，"
                    f"少于 {MIN_COMPOUNDS} 个不做微调。"
                    "实验里 100 个训练点才到 ρ≈0.04，更少的量只会过拟合。"
                ),
                failed=True,
            )

        if not confirm:
            values = [item["pchembl"] for item in actives]
            return ToolResult(
                output=(
                    f"预演：{ligands.handle} 有 {len(actives)} 个带实测活性的化合物"
                    f"（pK {min(values):.2f}–{max(values):.2f}，中位 "
                    f"{sorted(values)[len(values) // 2]:.2f}），受体 {receptor.handle}。\n"
                    f"将按 Murcko 骨架切开：约 {int(len(actives) * (1 - VAL_FRACTION))} 训练、"
                    f"{int(len(actives) * VAL_FRACTION)} 验证（验证集与训练集骨架互斥）。\n"
                    f"模式 {mode}、{epochs} 轮、lr {lr:g}、种子 {seed}；"
                    "CPU 上预计 5–10 分钟。\n"
                    "确认无误后用同一个调用加 confirm=true 真跑。"
                )
            )

        return await self._fit(
            ledger,
            receptor=receptor,
            ligands=ligands,
            receptor_pdb=receptor_pdb,
            ligand_coords=ligand_coords,
            actives=actives,
            mode=mode,
            epochs=epochs,
            lr=lr,
            seed=seed,
        )

    async def _fit(
        self,
        ledger: SessionLedger,
        *,
        receptor: Any,
        ligands: Any,
        receptor_pdb: Path,
        ligand_coords: Sequence[tuple[float, float, float]],
        actives: Sequence[dict[str, Any]],
        mode: str,
        epochs: int,
        lr: float,
        seed: int,
    ) -> ToolResult:
        try:
            python = affinity.dta_python()
            dta = affinity.dta_root()
        except Exception as error:
            return ToolResult(output=f"微调需要 DTA 的独立环境：{error}", failed=True)

        fingerprint = fingerprint_parts(
            {
                "receptor": str(receptor.fingerprint or ""),
                "ligands": str(ligands.fingerprint or ""),
                "mode": mode,
                "epochs": epochs,
                "lr": lr,
                "seed": seed,
                "pocket_radius": POCKET_RADIUS,
            },
            length=16,
        )
        existing = [
            item
            for item in ledger.find(
                kind="artifact", fingerprint=fingerprint, include_archived=True
            )
            if str(item.meta.get("kind") or "") == "model"
        ]
        if existing:
            return ToolResult(
                output=(
                    f"同一组输入已经微调过：{existing[0].handle}（未重跑）。"
                    "换 epochs / lr / 种子才会得到另一份。"
                ),
                meta={"handle": str(existing[0].handle), "reused": True},
            )

        work = work_root() / fingerprint
        try:
            work.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return ToolResult(output=f"建工作目录失败：{error}", failed=True)

        # 1) 口袋：从准备受体留下的共晶配体截
        doc = pdbtext.parse(receptor_pdb.read_text(encoding="utf-8", errors="replace"))
        protein_only, _groups = pdbtext.strip_hetero(doc)
        pocket_path = work / "pocket.pdb"
        pocket_text, residues, atoms = cut_pocket(protein_only, ligand_coords)
        if atoms == 0:
            return ToolResult(output="截出来的口袋是空的——先跑 prepare_receptor。", failed=True)
        pocket_path.write_text(pocket_text, encoding="utf-8")

        # 2) 切分与构象
        train, val = scaffold_split(actives, seed=seed)
        try:
            paths, failed = embed_conformers(train + val, work / "conformers", seed=CONFORMER_SEED)
        except ImportError as error:
            return ToolResult(
                output=f"生成构象需要化学栈（{error}）。pip install -r requirements-chem.txt",
                failed=True,
            )
        usable = [item for item in train + val if item["id"] in paths]
        train_ids = [item["id"] for item in train if item["id"] in paths]
        val_ids = [item["id"] for item in val if item["id"] in paths]
        if not train_ids or not val_ids:
            return ToolResult(output="构象生成后训练或验证集为空，放弃。", failed=True)

        data_dir = work / "data"
        for item in usable:
            directory = data_dir / item["id"]
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(paths[item["id"]], directory / f"{item['id']}_ligand.sdf")
            shutil.copyfile(pocket_path, directory / f"{item['id']}_pocket.pdb")

        index_path = work / "INDEX"
        write_index(index_path, usable)
        split_path = work / "split.json"
        products.write_json_atomic(
            split_path, {"train": train_ids, "val": val_ids, "test": []}
        )

        spec = {
            "target": str(receptor.handle),
            "data_dir": str(data_dir),
            "index": str(index_path),
            "split": str(split_path),
            "pocket": str(pocket_path),
            "out_dir": str(work / "out"),
            "base_checkpoint": str(
                dta / "out" / "model" / "best_model_08-25-14-54-57_rmse.pth"
            ),
            "smiles": {item["id"]: item["smiles"] for item in usable},
            "epochs": epochs,
            "lr": lr,
            "batch": DEFAULT_BATCH,
            "seed": seed,
            "head_only": mode == "head",
            "device": "cpu",
        }
        spec_path = work / "spec.json"
        products.write_json_atomic(spec_path, spec)
        if not Path(spec["base_checkpoint"]).is_file():
            return ToolResult(
                output=f"基座权重不在：{spec['base_checkpoint']}",
                failed=True,
            )

        # 3) 训练（子进程，DTA 环境）
        script = Path(__file__).with_name("fit_train.py")
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [python, str(script), str(spec_path)],
                capture_output=True,
                text=True,
                timeout=TRAIN_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                output=f"训练超过 {TRAIN_TIMEOUT:g} 秒仍未结束，已放弃。产物在 {work}。",
                failed=True,
            )
        except OSError as error:
            return ToolResult(output=f"起训练进程失败：{error}", failed=True)

        result = _parse_fit_result(completed.stdout or "")
        if completed.returncode != 0 or result is None or not result.get("checkpoint"):
            tail = "\n".join((completed.stderr or "").strip().splitlines()[-8:])
            return ToolResult(
                output=(
                    f"训练失败（退出码 {completed.returncode}）。日志尾部：\n{tail}\n"
                    f"完整产物在 {work}"
                ),
                failed=True,
            )

        config = Path(result["config"]).parent / "config.infer.yaml"
        summary = (
            f"微调完成：{len(train_ids)} 训练 / {len(val_ids)} 验证，"
            f"验证 RMSE {result['best_val_rmse']}，模式 {mode}，{epochs} 轮。"
        )
        entry = ledger.add(
            "artifact",
            path=work,
            summary=summary,
            fingerprint=fingerprint,
            parents=[receptor.handle, ligands.handle],
            meta={
                "kind": "model",
                "stage": "finetuned",
                "tool": "fit_affinity",
                "engine": {"name": "vcrossformer", "mode": mode, "epochs": epochs,
                           "lr": lr, "seed": seed},
                "checkpoint": str(result["checkpoint"]),
                "config": str(config),
                "pocket": str(pocket_path),
                "counts": {"train": len(train_ids), "val": len(val_ids),
                           "skipped": len(failed)},
                "metrics": {"val_rmse": result["best_val_rmse"]},
                "pocket_atoms": atoms,
                "pocket_residues": residues,
            },
        )
        return ToolResult(
            output=(
                f"结论：{summary}登记为 {entry.handle}。\n"
                f"用法：score_affinity(receptor=…, ligands=…, model={entry.handle})。\n"
                "提醒：这个模型只对本次的口袋有效；换受体或换靶点要重训。"
                "另外，验证集是骨架互斥切出来的，但只有 "
                f"{len(val_ids)} 个点，RMSE 的波动不小——别拿它当精确指标。"
            ),
            meta={
                "handle": str(entry.handle),
                "val_rmse": result["best_val_rmse"],
                "train": len(train_ids),
                "val": len(val_ids),
                "mode": mode,
            },
        )


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def _receptor_inputs(entry: Any) -> tuple[Path, tuple[float, float, float], list[tuple[float, float, float]]]:
    """从 prepared 受体里取 (receptor.pdb, 盒子中心, 共晶配体坐标)。

    口袋要按共晶配体截，所以 `prepare_receptor` 必须留下了 `ligand.pdb`；
    没有它就只能退回「以盒子中心为球心」那种截法，而那个与训练分布对不上，
    所以这里直接拒绝并说清怎么办。
    """
    meta = dict(entry.meta or {})
    if str(meta.get("stage") or "") != "prepared":
        raise ToolError(
            "受体还不处于 prepared 状态。先用 prepare_receptor 的 confirm=true 写盘。"
        )
    directory = Path(entry.path) if entry.path is not None else None
    if directory is None or not directory.is_dir():
        raise ToolError("prepared 受体的产物目录不在了；重新跑 prepare_receptor。")

    name = str(meta.get("pdb_file") or "receptor.pdb")
    if Path(name).name != name:
        raise ToolError(f"受体 meta 里的 pdb_file 不是文件名：{name}")
    pdb = directory / name
    if not pdb.is_file():
        raise ToolError(f"受体的 PDB 不在：{pdb}；重新跑 prepare_receptor。")

    ligand_name = str(meta.get("ligand_file") or "")
    if not ligand_name or Path(ligand_name).name != ligand_name:
        raise ToolError(
            "这份准备受体没有留下共晶配体（ligand.pdb），截不出与训练一致的口袋。"
            "重新跑 prepare_receptor，它在有共晶配体时会一并写出 ligand.pdb。"
        )
    ligand_pdb = directory / ligand_name
    if not ligand_pdb.is_file():
        raise ToolError(f"受体目录里的 {ligand_name} 不见了；重新跑 prepare_receptor。")

    doc = pdbtext.parse(ligand_pdb.read_text(encoding="utf-8", errors="replace"))
    coords = [
        (atom.x, atom.y, atom.z)
        for atom in doc.atoms
        if atom.is_hetero and pdbtext.is_heavy(atom)
    ]
    if not coords:
        raise ToolError(f"{ligand_name} 里没有重原子，截不出口袋。")

    box = read_box(directory, meta)
    return pdb, box.center, coords


def _parse_fit_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if not line.startswith("FIT_RESULT "):
            continue
        try:
            payload = json.loads(line[len("FIT_RESULT "):])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [FitAffinityTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
