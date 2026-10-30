"""药效团：从一组已知活性里提炼「共同的特征排布」，再拿它去筛库。

这是六段流程里最后一个筛选通道，也是**最依赖前提**的一个：药效团说的是「这些原子团在
空间里怎么摆」，所以每个输入分子都必须在**同一个坐标系**里。两种来源都收：

- `docking`：对接产物的位姿。它们本来就在受体坐标系里，不需要再叠合——**优先走这条**。
- `actives`：已备 3D 的配体集。各分子的构象是各自生成的，必须先按最大公共子结构
  （MCS）叠到第一个分子上；叠不上的逐条记下来，不静默丢掉。

**共识怎么取**：把每个分子的疏水 / 氢键供体 / 氢键受体 / 芳香特征提到点上，按「同类型
且相距不超过 `tolerance`」聚类，只留下**至少出现在 `min_support` 比例的分子里**的那些点。
一个只在两个分子里出现的点不是共识，是那两个分子的个性。

**筛选为什么不做叠合**：把每个库分子往药效团上叠需要逐对搜索构象与刚性叠合，几千个
分子就是分钟级，而且失败率高（不同骨架常常根本叠不上）。这里改用**特征间距离直方图**
（药效团指纹）：只看「同类特征之间差多远」，与整体朝向无关。代价与 `screen_shape` 的
USR 一样——分得清拓扑、分不清朝向。要精确的，走 dock。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    ArtifactHandle,
    LigandSetHandle,
    SessionLedger,
    expect_handle,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import docking, ligand, products, screen
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem

__all__ = [
    "BuildPharmacophoreTool",
    "ScreenPharmacophoreTool",
    "domain_tools",
    "pharmacophore_dir",
    "feature_points",
]

PHARMACOPHORE_DIR = "pharmacophore"
MANIFEST_FILE = "pharmacophore.json"
PARAMS_FILE = products.PARAMS_FILE

FEATURE_TYPES = ("Donor", "Acceptor", "Aromatic", "Hydrophobic")
DEFAULT_SUPPORT = 0.6
MIN_ACTIVES = 5
DEFAULT_TOLERANCE = 1.5
DEFAULT_BIN = 1.0
MAX_PAIR_DISTANCE = 20.0
MAX_TOP = 500


def pharmacophore_dir() -> Path:
    """药效团产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / PHARMACOPHORE_DIR


def _factory():
    """RDKit 的特征工厂。**只在函数体里导入**（纪律 8）。"""
    from rdkit import RDConfig
    from rdkit.Chem import ChemicalFeatures

    import os

    path = os.path.join(RDConfig.RDDataDir, "BaseFeatures.fdef")
    return ChemicalFeatures.BuildFeatureFactory(path)


def feature_points(mol: Any) -> list[tuple[str, tuple[float, float, float]]]:
    """一个**带 3D 构象**的分子上的药效特征点。只留四大类，其余（如 ZnBinder）丢掉。"""
    factory = _factory()
    conformer = mol.GetConformer()
    points: list[tuple[str, tuple[float, float, float]]] = []
    for feature in factory.GetFeaturesForMol(mol):
        family = feature.GetFamily()
        if family not in FEATURE_TYPES:
            continue
        position = conformer.GetAtomPosition(feature.GetAtomIds()[0])
        points.append((family, (position.x, position.y, position.z)))
    return points


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.dist(tuple(left), tuple(right))


def _fingerprint(
    points: Sequence[tuple[str, tuple[float, float, float]]],
    *,
    bin_width: float,
    max_distance: float = MAX_PAIR_DISTANCE,
) -> dict[tuple[str, str, int], int]:
    """特征对距离直方图。键是 `(类型A, 类型B, 距离档)`，与朝向无关。"""
    histogram: dict[tuple[str, str, int], int] = {}
    for index, (left_type, left_point) in enumerate(points):
        for right_type, right_point in points[index + 1:]:
            distance = _distance(left_point, right_point)
            if distance > max_distance:
                continue
            pair = tuple(sorted((left_type, right_type)))
            key = (pair[0], pair[1], int(distance // bin_width))
            histogram[key] = histogram.get(key, 0) + 1
    return histogram


def fingerprint_similarity(
    probe: dict[tuple[str, str, int], int],
    target: dict[tuple[str, str, int], int],
) -> float:
    """**模型覆盖率**：模型里那些特征对，有多少在这条分子里也出现。

    不做对称的 Tanimoto：模型只有几个点（三五对距离），而分子动辄几十对，对称公式会
    让「完全满足药效团」的分子只拿到 0.6 分，分差也就跟着失去意义。多出来的特征不该
    扣分——药效团问的是「该有的有没有」，不是「是不是恰好只有这些」。
    """
    total = sum(target.values())
    if not total:
        return 0.0
    hit = sum(min(probe.get(key, 0), count) for key, count in target.items())
    return max(0.0, min(1.0, hit / total))


def _consensus(
    per_molecule: Sequence[Sequence[tuple[str, tuple[float, float, float]]]],
    *,
    tolerance: float,
    min_support: float,
) -> list[dict[str, Any]]:
    """按「同类型 + 距离 ≤ tolerance」聚类，保留出现在足够多分子里的点。

    `support` 数的是**分子**不是点：同一个分子里两个挨得很近的同类型特征只能证明
    「它自己有这两个特征」，不能算两次支持。
    """
    total = len(per_molecule)
    needed = max(1, math.ceil(total * min_support))
    clusters: list[dict[str, Any]] = []
    for index, points in enumerate(per_molecule):
        for kind, position in points:
            target = next(
                (
                    cluster
                    for cluster in clusters
                    if cluster["type"] == kind
                    and _distance(cluster["center"], position) <= tolerance
                ),
                None,
            )
            if target is None:
                clusters.append(
                    {
                        "type": kind,
                        "center": position,
                        "members": [position],
                        "molecules": {index},
                    }
                )
                continue
            target["members"].append(position)
            target["molecules"].add(index)
            count = len(target["members"])
            target["center"] = tuple(
                sum(point[axis] for point in target["members"]) / count
                for axis in range(3)
            )
    kept = [
        {
            "type": cluster["type"],
            "x": round(cluster["center"][0], 3),
            "y": round(cluster["center"][1], 3),
            "z": round(cluster["center"][2], 3),
            "support": len(cluster["molecules"]),
        }
        for cluster in clusters
        if len(cluster["molecules"]) >= needed
    ]
    kept.sort(key=lambda item: (FEATURE_TYPES.index(item["type"]), -item["support"]))
    return kept


def _points_of(feature_list: Sequence[dict[str, Any]]) -> list[tuple[str, tuple[float, float, float]]]:
    return [(item["type"], (item["x"], item["y"], item["z"])) for item in feature_list]


def _render_build(handle: str, features: Sequence[dict[str, Any]], used: int,
                  dropped: Sequence[dict[str, str]], rmsd: float | None,
                  target: Path) -> str:
    counts: dict[str, int] = {}
    for item in features:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    lines = [f"药效团已生成，登记为 {handle}。"]
    lines.append(
        f"从 {used} 个分子的共同特征里取出 {len(features)} 个药效点："
        + "、".join(f"{name}×{count}" for name, count in counts.items())
    )
    if rmsd is not None:
        lines.append(f"平均叠合 RMSD {rmsd:.2f} Å（按最大公共子结构叠到第一个分子上）。")
    else:
        lines.append("位姿来自对接产物，本来就在同一坐标系里，未做额外叠合。")
    if dropped:
        lines.append(f"{len(dropped)} 个分子没进来：")
        for item in dropped[:3]:
            lines.append(f"  · {item['id']}：{item['reason']}")
    if not features:
        lines.append(
            "**一个共识点都没有**：这些分子在空间上没有共同的药效排布。"
            "要么它们本来就不是一个系列，要么 min_support 定得太高。"
        )
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：screen_pharmacophore(pharmacophore={handle}, library=...)。")
    return "\n".join(lines)

def _mol_from_pdb(path: Path):
    from rdkit import Chem

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    mol = Chem.MolFromPDBBlock(text, removeHs=True, sanitize=False)
    if mol is None:
        return None
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        pass
    return mol


class BuildPharmacophoreTool(Tool):
    """从一组已知活性里提炼共识药效团。"""

    spec = ToolSpec(
        name="build_pharmacophore",
        description=(
            "从一组已知活性里提炼**共识药效团**：疏水、氢键供体、氢键受体、芳香四类特征点，"
            "只保留出现在足够多分子同一位置上的那些。" + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "两种 3D 来源：给 `docking` 就用对接位姿（它们已经在同一坐标系里，最稳）；"
            "只给 `actives` 就现场生成构象并按最大公共子结构叠到第一个分子上——叠不上的"
            "分子会被列出来，不静默丢掉。\n"
            "**少于 5 个可用分子直接拒绝**：两三个分子的「共识」是那两个分子的个性。"
            "筛的时候不做叠合（见 screen_pharmacophore），所以这里的共识只用来定特征与距离。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "actives": {"type": "string", "description": "参考活性集的 handle"},
                "docking": {
                    "type": "string",
                    "description": "可选：对接产物 handle，用它里面同名分子的位姿当 3D",
                },
                "min_support": {
                    "type": "number",
                    "description": f"一个点至少要在多少比例的分子里出现，默认 {DEFAULT_SUPPORT}",
                },
                "tolerance": {
                    "type": "number",
                    "description": f"同类型特征聚类的距离上限（埃），默认 {DEFAULT_TOLERANCE:g}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["actives"],
            "additionalProperties": False,
        },
    )
    primary_argument = "actives"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：build_pharmacophore 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("actives") or "").strip()
        if not raw:
            return ToolResult(output="缺少参数 actives。先 build_reference_set。", failed=True)
        try:
            actives = ledger.get(raw)
            expect_handle(actives, LigandSetHandle, "actives")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        support = arguments.get("min_support", DEFAULT_SUPPORT)
        tolerance = arguments.get("tolerance", DEFAULT_TOLERANCE)
        if isinstance(support, bool) or not isinstance(support, (int, float)) or not 0 < support <= 1:
            return ToolResult(output="参数 min_support 要在 0 到 1 之间（不含 0）", failed=True)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0.1 <= tolerance <= 5.0:
            return ToolResult(output="参数 tolerance 要在 0.1 到 5 之间", failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            rows = screen._smiles_of(actives)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        if len(rows) < MIN_ACTIVES:
            return ToolResult(
                output=(
                    f"参考集只有 {len(rows)} 个分子，少于 {MIN_ACTIVES}。"
                    "两三个分子的共同特征说明不了什么——先用 build_reference_set 放宽活性下限，"
                    "或者直接走 screen_similarity。"
                ),
                failed=True,
            )

        preloaded: dict[str, Any] = {}
        rmsd_hint: float | None = None
        # 已经用 align_ligands 叠过的那一份：直接读 SDF，不重新生成构象也不再叠一次
        # ——重新叠会让同一个输入两次跑出不同的药效团，而它明明是同一份坐标。
        aligned_sdf = str((actives.meta or {}).get("sdf") or "")
        if str((actives.meta or {}).get("stage") or "") == "aligned" and aligned_sdf:
            from rdkit import Chem, rdBase

            path = Path(str(actives.path)) / aligned_sdf
            if not path.is_file():
                return ToolResult(
                    output=f"{actives.handle} 说自己是叠合产物，但 {aligned_sdf} 不在了。",
                    failed=True,
                )
            with rdBase.BlockLogs():
                loaded = [mol for mol in Chem.SDMolSupplier(str(path), removeHs=True)
                          if mol is not None]
            for mol in loaded:
                if mol.HasProp("_Name"):
                    preloaded[mol.GetProp("_Name")] = mol
            if len(preloaded) < MIN_ACTIVES:
                return ToolResult(
                    output=f"{actives.handle} 里只有 {len(preloaded)} 个可读的构象（需要 "
                    f"{MIN_ACTIVES}）。",
                    failed=True,
                )
            upstream = (actives.meta or {}).get("mean_rmsd")
            if isinstance(upstream, (int, float)):
                rmsd_hint = float(upstream)

        docking_entry = None
        raw_docking = str(arguments.get("docking") or "").strip()
        if raw_docking:
            try:
                docking_entry = ledger.get(raw_docking)
                expect_handle(docking_entry, ArtifactHandle, "docking")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)

        poses: dict[str, Path] = {}
        if docking_entry is not None:
            manifest = products.read_json(Path(str(docking_entry.path)) / docking.MANIFEST_FILE)
            for item in manifest.get("results") or []:
                relative = str(item.get("pose_pdb") or "").strip()
                if relative:
                    poses[str(item.get("id") or "")] = Path(str(docking_entry.path)) / relative

        molecules: list[Any] = []
        dropped: list[dict[str, str]] = []
        rmsds: list[float] = []
        reference = None
        if rmsd_hint is not None:
            rmsds.append(rmsd_hint)
        for row in rows:
            if row["id"] in preloaded:
                # 已叠好的构象：直接用，不重新嵌、也不再叠
                molecules.append(preloaded[row["id"]])
                continue
            if row["id"] in poses:
                mol = _mol_from_pdb(poses[row["id"]])
                if mol is None:
                    dropped.append({"id": row["id"], "reason": "位姿文件读不出分子"})
                    continue
                molecules.append(mol)
                if reference is None:
                    reference = mol
                continue
            mol, reason = ligand.embed_smiles(row["smiles"])
            if mol is None:
                dropped.append({"id": row["id"], "reason": reason})
                continue
            if reference is not None:
                mol, rmsd, why = ligand.align_to_reference(reference, mol)
                if why:
                    dropped.append({"id": row["id"], "reason": why})
                    continue
                rmsds.append(rmsd)
            else:
                reference = mol
            molecules.append(mol)

        if len(molecules) < MIN_ACTIVES:
            return ToolResult(
                output=(
                    f"只有 {len(molecules)} 个分子成功拿到 3D（需要至少 {MIN_ACTIVES}）。"
                    + "".join(
                        f"\n  · {item['id']}：{item['reason']}" for item in dropped[:5]
                    )
                ),
                failed=True,
            )

        per_molecule = [feature_points(mol) for mol in molecules]
        usable = [points for points in per_molecule if points]
        if len(usable) < MIN_ACTIVES:
            return ToolResult(
                output=f"只有 {len(usable)} 个分子能提出药效特征（很多分子没有任何四大类特征）。",
                failed=True,
            )
        features = _consensus(usable, tolerance=float(tolerance), min_support=float(support))
        if not features:
            return ToolResult(
                output=(
                    f"{len(usable)} 个分子里没有找到出现在 ≥{support:.0%} 分子中的共同特征点。"
                    "这批分子可能不是一个系列；把 min_support 调低会得到一份「谁都不像」的药效团，"
                    "不建议那样用。"
                ),
                failed=True,
            )

        params = {
            "actives": str(actives.handle),
            "docking": str(docking_entry.handle) if docking_entry else "",
            "min_support": float(support),
            "tolerance": float(tolerance),
            "n_actives": len(usable),
        }
        mean_rmsd = sum(rmsds) / len(rmsds) if rmsds else None
        payload = {
            "kind": "pharmacophore",
            "actives": str(actives.handle),
            "n_actives": len(usable),
            "align_rmsd": None if mean_rmsd is None else round(mean_rmsd, 3),
            "features": features,
            "counts": {
                name: sum(1 for item in features if item["type"] == name)
                for name in FEATURE_TYPES
            },
        }
        stem = str(arguments.get("name") or "pharmacophore")
        try:
            target, reused = products.content_dir(
                pharmacophore_dir(), stem, params, (MANIFEST_FILE, PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        handle = ""
        if reused:
            existing = ledger.find(
                kind="artifact", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("kind") or "") == "pharmacophore":
                    handle = str(candidate.handle)
                    break
        if not handle:
            try:
                products.write_json_atomic(target / MANIFEST_FILE, payload)
                products.write_json_atomic(target / PARAMS_FILE, params)
            except OSError as error:
                return ToolResult(output=f"写产物失败：{error}", failed=True)
            registered = ledger.add(
                "artifact",
                path=target,
                summary=f"药效团 · {len(features)} 个点 · 来自 {len(usable)} 个活性分子",
                fingerprint=fingerprint_path(target),
                parents=[actives.handle],
                meta={
                    "kind": "pharmacophore",
                    "stage": "pharmacophore",
                    "tool": "build_pharmacophore",
                    "n_actives": len(usable),
                    "align_rmsd": payload["align_rmsd"],
                    "features": features,
                    "counts": payload["counts"],
                    "params": params,
                    "file": MANIFEST_FILE,
                },
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_build(handle, features, len(usable), dropped, payload["align_rmsd"],
                                 target),
            meta={"handle": handle, "features": len(features), "n_actives": len(usable),
                  "reused": bool(handle) and reused},
        )


class ScreenPharmacophoreTool(Tool):
    """按药效团的距离指纹给一个库排序。"""

    spec = ToolSpec(
        name="screen_pharmacophore",
        description=(
            "用药效团给一个库排序，产出候选表（分数列 pharmacophore，越高越像）。"
            + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "打分用的是**特征间距离直方图**：只看「同类特征之间差多远」，与分子朝向无关，"
            "所以不需要逐个叠合；分数是「模型的几对距离，这条分子满足了多少」，"
            "多出来的特征不扣分。代价和 screen_shape 一样——分得清拓扑、分不清朝向；"
            "要精确排序走 dock。\n"
            "库里的分子会现场生成 3D 构象（种子固定，同样的输入两次跑出同样的分）。"
            "与 Tanimoto 不可比，别混着排序。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pharmacophore": {"type": "string", "description": "build_pharmacophore 的产物"},
                "library": {"type": "string", "description": "待筛配体集 handle"},
                "tolerance": {
                    "type": "number",
                    "description": f"距离直方图的档宽（埃），默认 {DEFAULT_BIN:g}",
                },
                "top": {
                    "type": "integer",
                    "description": f"最多保留多少条，默认 100，上限 {MAX_TOP}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["pharmacophore", "library"],
            "additionalProperties": False,
        },
    )
    primary_argument = "library"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：screen_pharmacophore 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw_pharm = str(arguments.get("pharmacophore") or "").strip()
        raw_library = str(arguments.get("library") or "").strip()
        if not raw_pharm or not raw_library:
            return ToolResult(
                output="缺少参数：pharmacophore（药效团产物）与 library（待筛配体集）都要给。",
                failed=True,
            )
        try:
            pharm_entry = ledger.get(raw_pharm)
            expect_handle(pharm_entry, ArtifactHandle, "pharmacophore")
            library_entry = ledger.get(raw_library)
            expect_handle(library_entry, LigandSetHandle, "library")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        if str((pharm_entry.meta or {}).get("kind") or "") != "pharmacophore":
            return ToolResult(
                output=(
                    f"{pharm_entry.handle} 不是药效团（kind={(pharm_entry.meta or {}).get('kind')}）。"
                ),
                failed=True,
            )

        bin_width = arguments.get("tolerance", DEFAULT_BIN)
        if isinstance(bin_width, bool) or not isinstance(bin_width, (int, float)) or not 0.1 <= bin_width <= 5.0:
            return ToolResult(output="参数 tolerance 要在 0.1 到 5 之间", failed=True)
        limit = arguments.get("top", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TOP:
            return ToolResult(output=f"参数 top 要在 1 到 {MAX_TOP} 之间", failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            rows = screen._smiles_of(library_entry)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        if not rows:
            return ToolResult(output=f"{library_entry.handle} 里没有带结构的分子。", failed=True)

        features = list((pharm_entry.meta or {}).get("features") or [])
        if not features:
            return ToolResult(output=f"{pharm_entry.handle} 里没有药效点。", failed=True)
        target_fp = _fingerprint(_points_of(features), bin_width=float(bin_width))

        scored: list[dict[str, Any]] = []
        failed = 0
        for row in rows[:5000]:
            mol, _reason = ligand.embed_smiles(row["smiles"])
            if mol is None:
                failed += 1
                continue
            points = feature_points(mol)
            if not points:
                failed += 1
                continue
            score = fingerprint_similarity(
                _fingerprint(points, bin_width=float(bin_width)), target_fp
            )
            scored.append({**row, "score": float(score)})
        if not scored:
            return ToolResult(
                output=f"{len(rows)} 个分子里没有一个能提出药效特征（{failed} 个没成功）。",
                failed=True,
            )

        scored.sort(key=lambda item: (-item["score"], item["id"]))
        total = len(scored)
        kept = scored[:limit]
        params = {
            "pharmacophore": str(pharm_entry.handle),
            "library": str(library_entry.handle),
            "bin_width": float(bin_width),
            "top": limit,
        }
        stem = str(arguments.get("name") or "pharmacophore-screen")
        try:
            handle, target, reused = screen.write_candidates(
                ledger=ledger,
                root=screen.candidates_dir(),
                stem=stem,
                tool="screen_pharmacophore",
                params=params,
                column="pharmacophore",
                rows=kept,
                parents=[pharm_entry, library_entry],
                extra_meta={"bin_width": float(bin_width), "truncated": total - len(kept),
                            "failed": failed},
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        lines = [
            f"按药效团筛完，登记为 {handle}。" if not reused
            else f"登记表里已有同一份：{handle}（未重算）。",
            f"命中 {total} 条，保留 {len(kept)} 条"
            + (f"，另有 {total - len(kept)} 条没列出来（调大 top）" if total > len(kept) else "")
            + "。",
            f"最高 {kept[0]['score']:.3f}：{kept[0]['id']}（药效团指纹，与 Tanimoto 不可比）",
        ]
        if failed:
            lines.append(f"{failed} 个分子生成 3D 或提特征失败，未参与排序。")
        lines.append(f"产物目录：{target}")
        lines.append("下一步：rank_candidates 把它与 dock / 相似度的产物并起来。")
        return ToolResult(
            output="\n".join(lines),
            meta={"handle": handle, "count": len(kept), "hit": total, "failed": failed,
                  "reused": reused},
        )


def domain_tools() -> list[Tool]:
    return [BuildPharmacophoreTool(), ScreenPharmacophoreTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    main()
