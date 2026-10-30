#!/usr/bin/env python3
"""回顾性实验：靶点内活性数据能带来多少排序提升。方案见 `docs/retro-affinity-plan.md`。

```
python tools/retro_affinity.py select --target P00533 --target P00734 --target Q86WV6
python tools/retro_affinity.py build  --target P18031 [--min-group 5] [--seed 42]
python tools/retro_affinity.py run    --target P18031 [--split s2] [--arm b2] [--repeats 5]
python tools/retro_affinity.py report [--split s2] [--baseline b2]
```

四步各管一件事，**顺序不能反**：`select` 判数据够不够（不够就别写后面的）、
`build` 切出 S1/S2/S3 并做骨架互斥的硬断言、`run` 跑一条条臂、`report` 用配对
bootstrap 出 Δ 并按 §9 的预注册规则下判定。

**不进 CI**：只有 `select` 联网（ChEMBL + RCSB），`build` / `run` / `report` 全在本地。

`select` 里有四件事必须做对，否则审计数字看着漂亮但没意义：

- **取数按 `activity_id` 排序，不按 `-pchembl_value`。** 生产路径（`search_actives`）
  为了「到上限时留下最强的」才按活性排；实验要的是完整分布，截在高效端会让数据集
  没有弱化合物，R² 与校准全是假的。`activity_id` 还是唯一键，翻页不会重复或漏行。
- **聚合用中位数，不用最大值。** `chembl._select` 取「同分子最强那条」对筛选用是对的，
  对训练是系统乐观偏差。这里自己聚合。
- **置信度在本地应用，不在请求里。** 一次取回、按 5/6/7/8/9 各算一遍，才看得见
  数据量对质量门槛有多敏感。缓存也按「取数下限」判断能不能复用。
- **骨骼切分可行性先算。** 最大骨架组占比超过 35% 时，骨架切分会让测试集只剩一两个
  骨架，ρ 的方差失控——这个靶点不能用来回答 H3。

产物全在 `.probe/retro/` 下（已 gitignore）：`select/` 审计快照、`cache/` 取数与配体
索引、`<label>/dataset.json` 与 `splits/`、`<label>/runs/<arm>_<split>.json`、
`report_<split>.json`。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core.agent.domain import (  # noqa: E402
    affinity,
    chembl,
    complexes,
    finetune,
    pdbtext,
    preprocess,
)
from vsagent.env import load_env  # noqa: E402

PROBE_DIR = PROJECT_ROOT / ".probe" / "retro"
SELECT_DIR = PROBE_DIR / "select"
CACHE_DIR = PROBE_DIR / "cache"
DTA_DATA = PROJECT_ROOT / "models" / "DTA" / "data"
SPLIT_FILE = DTA_DATA / "split.json"
PDB_DIR_ENV = "VSA_DTA_PDB_DIR"
"""原始复合物（`<id>/<id>_complex_min_ligand.sdf` + `<id>.pocket.pdb`）的根。

它**不在仓里**：15.7k 个复合物、4.7GB，与 `models/DTA` 一样由 `.gitignore` 挡住。
默认依次找 `--pdb-dir`、环境变量、`models/DTA/data/pdb`；都没有时配体级重叠报「跳过」，
复合物级照常做。
"""

DEFAULT_MIN_PCHEMBL = 6.0
DEFAULT_MIN_CONFIDENCE = 7
DEFAULT_ASSAY_TYPE = "B"
ALL_ASSAY_TYPES = "all"

FETCH_CAP = 30_000
"""一次最多取回多少条活性记录。EGFR 在 pChEMBL≥6 上是 1.6 万条（17 页），这里留足余量。

到顶时如实写进快照（`truncated=True`）——截断过的分布不能拿来定档位。
"""

CONFIDENCE_FLOORS: tuple[int, ...] = (5, 6, 7, 8, 9)
"""审计要看的数据量-质量曲线。生产默认是 8（`search_actives`），实验默认 7。"""

TIER_LOW = 300
TIER_HIGH = 1500

MIN_FOR_H1 = 80
"""H1（B1 vs B3）只需要测试集，不需要训练集；80 个化合物切完还剩十几个。"""

MIN_FOR_H3 = 150
MIN_SCAFFOLD_GROUPS = 20
LARGEST_SHARE_LIMIT = 0.35

SUSPECT_POTENCY = chembl.POTENCY_FLOOR_HINT
"""pChEMBL 超过这个量级多半不是精确测量（见 chembl.POTENCY_FLOOR_HINT）。只统计，不剔除。"""

TRAIN_OVERLAP_FLAG = 50
"""该靶点在 DTA 训练集里的复合物超过这个数，就把「预训练早已见过」标成混杂因素。

这是**复合物级**的旁证。真正的判据是下面那个配体级重叠率。
"""

LIGAND_OVERLAP_LIMIT = 0.05
"""化合物级重叠率超过这个值就换靶点：预训练已经背下了答案，微调的 Δ 无法解释。"""

LIGAND_INDEX_VERSION = 2
"""配体索引的算法版本。改了解析逻辑就要涨它，否则会命中旧缓存里的残缺索引。"""

RCSB_SAMPLE = 8
"""报告里列几个 PDB 编号给人看，全量在 JSON 里。"""

DROP_LABELS: dict[str, str] = {
    **chembl.DROP_REASONS,
    "wrong_assay_type": "实验体系不符（细胞/生化混用）",
    "below_floor": "中位活性低于下限",
    "salt_duplicate": "盐型/立体变体并回同一分子",
}


# ---------------------------------------------------------------------------
# 取数（带缓存）
# ---------------------------------------------------------------------------
def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_file(accession: str) -> Path:
    """缓存按 UniProt 编号命名，**不按过滤条件**。

    过滤条件（下限、测定类型）写进文件头。复用规则见 `fetch_activities`：缓存是
    「下限更低的一份」就能直接过滤出来，反之要重取。这样换 `--min-pchembl` 时
    只有往更松的方向调才会联网。
    """
    return CACHE_DIR / f"{accession.upper()}-activities.json"


def _slim(row: dict[str, Any], facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把一条 activity 压到审计与后续切分需要的那几列。

    原始行有四十多个字段、其中一半是嵌套结构，1.6 万条存下来 30MB；压完不到 5MB，
    而且是唯一一份「下游需要的形状」——`_normalize` 那套扁平表是给 CSV 用的，不在这里复用。
    """
    assay = str(row.get("assay_chembl_id") or "")
    fact = facts.get(assay) or {}
    year = row.get("document_year")
    return {
        "molecule": str(row.get("molecule_chembl_id") or ""),
        "parent": str(row.get("parent_molecule_chembl_id") or row.get("molecule_chembl_id") or ""),
        "smiles": str(row.get("canonical_smiles") or ""),
        "pchembl": chembl._as_float(row.get("pchembl_value")),
        "relation": str(row.get("standard_relation") or ""),
        "measure": str(row.get("standard_type") or ""),
        "assay": assay,
        "year": year if isinstance(year, int) else None,
        "confidence": fact.get("confidence_score"),
        "assay_type": str(fact.get("assay_type") or ""),
    }


def fetch_activities(
    accession: str,
    *,
    min_pchembl: float,
    measure_types: str,
    refresh: bool,
    timeout: float,
) -> dict[str, Any]:
    """取一个靶点的全部活性记录，返回可缓存的结构。

    命中数超上限时**不按活性截断**：按 `activity_id` 升序取到 `FETCH_CAP` 为止，
    并把 `truncated` 标出来。按活性排序会把弱化合物整段切掉，那样的数据集既不能
    训练也不能评估。
    """
    cached = _read_cache(accession)
    if cached is not None and not refresh:
        same_measure = cached.get("measure_types") == measure_types
        # 缓存的下限更低 => 它是本次请求的超集，本地过滤即可
        wide_enough = float(cached.get("fetch_floor") or 99) <= min_pchembl
        if same_measure and wide_enough:
            cached["cached"] = True
            return cached

    choice = chembl._resolve_target(accession, timeout=timeout)
    base = {"target_chembl_id": choice.chembl_id}
    params = {
        **base,
        "pchembl_value__gte": f"{min_pchembl:g}",
        "standard_type__in": measure_types,
        # 服务端确实生效的过滤条件就这几个（见 chembl 模块 docstring）
        "potential_duplicate": "0",
        # 唯一键，翻页才稳定；按活性排会让弱化合物在超上限时整段消失
        "order_by": "activity_id",
    }

    total_all = chembl._count(chembl.ACTIVITY_URL, base, timeout=timeout)
    matched = chembl._count(chembl.ACTIVITY_URL, params, timeout=timeout)
    want = min(matched, FETCH_CAP)
    pages = (want + chembl.PAGE_SIZE - 1) // chembl.PAGE_SIZE
    print(f"  取数：靶点 {choice.chembl_id}，命中 {matched} 条"
          f"（该靶点全部 {total_all} 条），约 {pages} 页…")
    started = time.monotonic()
    rows = chembl._fetch_activities(params, want=want, timeout=timeout) if want else []
    facts = chembl._assay_facts(
        (str(item.get("assay_chembl_id") or "") for item in rows), timeout=timeout
    )
    print(f"  取回 {len(rows)} 条，{time.monotonic() - started:.1f}s")

    payload = {
        "uniprot": accession.upper(),
        "target": {
            "chembl_id": choice.chembl_id,
            "name": choice.name,
            "alternatives": choice.alternatives,
            "skipped_complexes": choice.skipped_complexes,
        },
        "fetch_floor": min_pchembl,
        "measure_types": measure_types,
        "params": params,
        "total_all": total_all,
        "total_matched": matched,
        "truncated": matched > want,
        "fetched_at": _stamp(),
        "rows": [_slim(row, facts) for row in rows],
    }
    _write_json(_cache_file(accession), payload)
    payload["cached"] = False
    return payload


def _read_cache(accession: str) -> dict[str, Any] | None:
    path = _cache_file(accession)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 缓存坏了就当没有：重取一次比在这里报错有用
        return None


# ---------------------------------------------------------------------------
# 聚合：同分子取中位数，盐型按 InChIKey 前 14 位再合一次
# ---------------------------------------------------------------------------
def aggregate(
    rows: Sequence[dict[str, Any]],
    *,
    min_pchembl: float,
    min_confidence: int,
    assay_type: str,
    merge_salts: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """过掉不干净的数据、按母体归并、算每个化合物的中位 pChEMBL。

    剔除顺序与 `chembl._select` 的一致性是有意的（同一条记录在两边该有同样的命运），
    唯一的差别是聚合法：这里中位数，那里最大值。
    """
    dropped = dict.fromkeys(
        (*chembl.DROP_REASONS, "wrong_assay_type", "below_floor", "salt_duplicate"), 0
    )
    grouped: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        if row.get("pchembl") is None:
            dropped["no_value"] += 1
            continue
        if row.get("relation") != chembl.RELATION_EQUAL:
            dropped["unmeasured"] += 1
            continue
        if not row.get("smiles"):
            dropped["no_structure"] += 1
            continue
        score = row.get("confidence")
        if not isinstance(score, int) or score < min_confidence:
            dropped["low_confidence"] += 1
            continue
        if assay_type != ALL_ASSAY_TYPES and row.get("assay_type") != assay_type:
            dropped["wrong_assay_type"] += 1
            continue
        grouped.setdefault(row["parent"], []).append(row)

    compounds: list[dict[str, Any]] = []
    for parent, items in grouped.items():
        values = [float(item["pchembl"]) for item in items]
        median = statistics.median(values)
        if median < min_pchembl:
            dropped["below_floor"] += 1
            continue
        years = sorted({item["year"] for item in items if isinstance(item.get("year"), int)})
        compounds.append(
            {
                "id": parent,
                "smiles": items[0]["smiles"],
                "pchembl": round(float(median), 3),
                "pchembl_max": round(max(values), 3),
                "n_measurements": len(items),
                "assays": sorted({item["assay"] for item in items if item["assay"]}),
                "years": years,
                "assay_types": sorted({item["assay_type"] for item in items if item["assay_type"]}),
                "inchikey14": "",
            }
        )

    compounds, duplicates = _merge_salts(compounds) if merge_salts else (compounds, 0)
    dropped["salt_duplicate"] = duplicates
    compounds.sort(key=lambda item: (-item["pchembl"], item["id"]))
    return compounds, dropped


def _merge_salts(compounds: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按 InChIKey 前 14 位（骨架层）把盐型/立体变体合回同一个分子。

    ChEMBL 的 `parent_molecule_chembl_id` 已经处理了大部分盐型，但同一母体仍会因为
    立体化学标注不同被拆成两条。骨架层（前 14 位）相同就当作同一个化合物，取中位数。
    """
    rdkit = _rdkit()
    if rdkit is None:
        return compounds, 0
    Chem, _ = rdkit
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in compounds:
        mol = Chem.MolFromSmiles(item["smiles"])
        if mol is None:
            item["inchikey14"] = ""
            by_key.setdefault(f"id:{item['id']}", []).append(item)
            continue
        item["inchikey14"] = Chem.MolToInchiKey(mol)[:14]
        by_key.setdefault(item["inchikey14"], []).append(item)

    merged: list[dict[str, Any]] = []
    duplicates = 0
    for key, group in by_key.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        duplicates += len(group) - 1
        # 保留 id 最小的那条作为代表，测量条数与 assay 合并
        group.sort(key=lambda item: item["id"])
        keeper = dict(group[0])
        keeper["pchembl"] = round(
            float(statistics.median([item["pchembl"] for item in group])), 3
        )
        keeper["n_measurements"] = sum(item["n_measurements"] for item in group)
        keeper["assays"] = sorted({assay for item in group for assay in item["assays"]})
        keeper["years"] = sorted({year for item in group for year in item["years"]})
        merged.append(keeper)
    return merged, duplicates


def confidence_sweep(rows: Sequence[dict[str, Any]], *, min_pchembl: float,
                     assay_type: str) -> dict[str, int]:
    """置信度门槛 5..9 各剩多少化合物。数据量对质量门槛的敏感度要摆在报告第一屏。

    这里跳过盐型归并：它只改变计数不改变结论（是同一批分子的骨架层合并），
    而 InChIKey 是这轮里最贵的一步，扫五遍没有意义。
    """
    table: dict[str, int] = {}
    for floor in CONFIDENCE_FLOORS:
        kept, _ = aggregate(
            rows,
            min_pchembl=min_pchembl,
            min_confidence=floor,
            assay_type=assay_type,
            merge_salts=False,
        )
        table[str(floor)] = len(kept)
    return table


# ---------------------------------------------------------------------------
# 骨架分布
# ---------------------------------------------------------------------------
def _rdkit() -> tuple[Any, Any] | None:
    """延迟导入 RDKit：没装化学栈时审计照跑，只是骨架那一节标成不可用。"""
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem.Scaffolds import MurckoScaffold
    except ImportError:
        return None
    # 这个数据集里有 1/8 的配体 SDF 是「全芳香键」写法，RDKit 会逐条打 kekulize 警告；
    # 它们是数据本身的写法问题，不是我们要读的信息，逐条打在 stderr 上会把报告冲掉。
    RDLogger.DisableLog("rdApp.*")
    return Chem, MurckoScaffold


def scaffold_stats(compounds: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Murcko 骨架分组：决定骨架切分能不能做，以及主终点还剩多少自由度。

    `largest_share` 是最大的那个骨架占了全体的多少。占比过高时，骨架切分会把整个
    测试集压成一两个骨架，ρ 的方差大到测不出任何差异——这个靶点就只能答 H1，答不了 H3。
    """
    rdkit = _rdkit()
    if rdkit is None:
        return {"available": False, "reason": "没有安装 RDKit（pip install -r requirements-chem.txt）"}
    Chem, MurckoScaffold = rdkit

    groups: dict[str, list[str]] = {}
    by_compound: dict[str, str] = {}
    parsed = 0
    failed = 0
    for item in compounds:
        mol = Chem.MolFromSmiles(item["smiles"])
        if mol is None:
            failed += 1
            continue
        try:
            scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=mol, includeChirality=False)
        except Exception:  # RDKit 对个别畸形结构会抛，跳过并计数
            failed += 1
            continue
        parsed += 1
        key = scaffold or "（无环）"
        by_compound[item["id"]] = key
        groups.setdefault(key, []).append(item["id"])

    if not groups:
        return {"available": False, "reason": "没有一个结构能算出骨架"}

    sizes = sorted((len(ids) for ids in groups.values()), reverse=True)
    largest = max(groups.items(), key=lambda kv: len(kv[1]))
    return {
        "available": True,
        "parsed": parsed,
        "failed": failed,
        "n_scaffolds": len(groups),
        "largest": largest[0],
        "largest_size": sizes[0],
        "largest_share": round(sizes[0] / parsed, 4),
        "groups_ge2": sum(1 for size in sizes if size >= 2),
        "groups_ge5": sum(1 for size in sizes if size >= 5),
        "by_compound": by_compound,
    }


# ---------------------------------------------------------------------------
# PDBbind 重叠
# ---------------------------------------------------------------------------
def dta_pdb_dir(explicit: str | None) -> Path | None:
    """原始复合物的根。**不写死机器路径**：先参数、再环境变量、最后仓内默认。

    给的是数据集的**父目录**（里面有 `pdb/`、`db/`、各个 `.pkl`）时自动下沉一层——
    用户嘴里的「DTA 数据在这儿」指的通常是父目录，而这里要的是复合物那一层。
    判据是「它下面有个 `pdb/` 子目录」，真实复合物目录不会有这种结构。
    """
    for candidate in (explicit, os.environ.get(PDB_DIR_ENV), DTA_DATA / "pdb"):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_dir():
            continue
        nested = path / "pdb"
        return nested if nested.is_dir() else path
    return None


def ligand_index(pdb_dir: Path, train_ids: Iterable[str], *, refresh: bool) -> dict[str, Any]:
    """训练集里每个复合物的配体骨架（InChIKey 前 14 位）→ PDB 编号。

    **用 SDF 解析，不用 `pdbid_smiles_dict.pkl`。** 那份 pkl 里的 SMILES 用了
    `C(:O):[O-]` 这种非常规写法，实测前 500 条里 43 条解析不了，能解析的那批
    `MolToInchiKey` 还返回 None——拿它建索引会静默丢掉一成数据。SDF 是带键级的原始
    记录，快（200 个 0.07 秒）。

    **覆盖率约 65%，这是数据本身的限制，不是实现问题。** 该数据集有一部分配体用
    「全芳香键」写法（MDL 键型 4 铺满整个环），RDKit 判不出合法的 Kekulé 结构，
    InChI 也就给不出来；跳过 kekulize 的兜底路径救不回这批（InChI 强制要求 Kekulé）。

    解析失败的不猜：`failed` 单独计数，因为「配体没解析出来」与「配体不在训练集里」
    在重叠率上都表现为 0，但含义相反——前者只能漏判，后者是确凿的「没见过」。
    """
    rdkit = _rdkit()
    if rdkit is None:
        return {"available": False, "reason": "没有安装 RDKit"}
    Chem, _ = rdkit

    ids = sorted({str(item).lower() for item in train_ids})
    signature = hashlib.sha1(
        f"v{LIGAND_INDEX_VERSION}|{pdb_dir}|{len(ids)}|"
        f"{SPLIT_FILE.stat().st_mtime if SPLIT_FILE.is_file() else 0}".encode()
    ).hexdigest()[:12]
    cache = CACHE_DIR / f"ligands-{signature}.json"
    if cache.is_file() and not refresh:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["cached"] = True
            return payload
        except (OSError, json.JSONDecodeError):
            pass

    print(f"  建配体索引：{len(ids)} 个训练复合物，读 {pdb_dir} …")
    started = time.monotonic()
    index: dict[str, str] = {}
    keyed = 0
    failed = 0
    missing = 0
    for pdb_id in ids:
        path = _ligand_sdf(pdb_dir, pdb_id)
        if path is None:
            missing += 1
            continue
        key = _ligand_key(Chem, path)
        if key is None:
            failed += 1
            continue
        keyed += 1
        index.setdefault(key, pdb_id)
    coverage = keyed / len(ids) if ids else 0.0
    print(f"  索引完成：{len(index)} 个骨架，覆盖 {keyed}/{len(ids)} 条记录"
          f"（{coverage:.0%}），失败 {failed}、缺文件 {missing}，"
          f"{time.monotonic() - started:.1f}s")

    payload = {
        "available": True,
        "pdb_dir": str(pdb_dir),
        "records": len(ids),
        "keyed": keyed,
        "coverage": round(coverage, 4),
        "unique": len(index),
        "failed": failed,
        "missing": missing,
        "index": index,
    }
    _write_json(cache, payload)
    payload["cached"] = False
    return payload


def _ligand_key(Chem: Any, path: Path) -> str | None:
    """一个配体 SDF 的骨架键（InChIKey 前 14 位）；给不出来就是 None。

    **没有兜底路径，因为兜底在原理上救不回来。** 这个数据集的 `*_min_ligand.sdf`
    有三分之一用「全芳香键」写法（MDL 键型 4 铺满整个环），RDKit 判不出合法 Kekulé，
    严格 sanitize 直接返回 None。试过跳过 kekulize 再算 InChI——没用：InChI 自己
    也要求 Kekulé 结构，同一个图它一样失败（实测 300 条里三条路径都是 193 条成功，
    一条不多）。所以这里只报「拿不到键」，由调用方计入覆盖率。
    """
    try:
        mol = next(Chem.SDMolSupplier(str(path), removeHs=True, sanitize=True), None)
    except Exception:  # RDKit 对个别畸形记录会抛
        return None
    if mol is None:
        return None
    try:
        key = Chem.MolToInchiKey(mol)
    except Exception:
        return None
    return key[:14] if key else None


def _ligand_sdf(pdb_dir: Path, pdb_id: str) -> Path | None:
    """复合物目录里的配体 SDF。命名在不同批次里不统一，所以两头都认。"""
    directory = pdb_dir / pdb_id
    if not directory.is_dir():
        return None
    exact = directory / f"{pdb_id}_complex_min_ligand.sdf"
    if exact.is_file():
        return exact
    candidates = sorted(directory.glob("*_ligand.sdf"))
    return candidates[0] if candidates else None


def pdb_overlap(
    accession: str,
    *,
    timeout: float,
    compounds: Sequence[dict[str, Any]],
    pdb_dir: Path | None,
    refresh: bool,
) -> dict[str, Any]:
    """两层：该靶点有多少复合物在训练集里，以及**这些化合物本身**有多少在训练集里。

    第一层是旁证（靶点被预训练覆盖的程度）；第二层才是判据——模型见过这个分子，
    它的预测就不是「泛化」而是「记忆」，那样的 Δ 不能拿来论证微调有用。
    """
    try:
        ids, body = complexes.list_complexes(accession, timeout=timeout)
    except Exception as error:  # 检索失败与「零命中」在报告里是两句不同的话
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    total = None
    if isinstance(body, dict):
        total = body.get("total_count")
    split = _read_split()
    lowered = [item.lower() for item in ids]
    result: dict[str, Any] = {
        "ok": True,
        "entries": total if isinstance(total, int) else len(lowered),
        "listed": len(lowered),
        "in_train": sorted(set(lowered) & split.get("train", set())),
        "in_val": sorted(set(lowered) & split.get("val", set())),
        "in_test": sorted(set(lowered) & split.get("test", set())),
        "sample": lowered[:RCSB_SAMPLE],
    }
    result["ligands"] = _ligand_matches(
        compounds, pdb_dir=pdb_dir, train_ids=split.get("train", set()), refresh=refresh
    )
    return result


def _ligand_matches(
    compounds: Sequence[dict[str, Any]],
    *,
    pdb_dir: Path | None,
    train_ids: set[str],
    refresh: bool,
) -> dict[str, Any]:
    """化合物骨架与训练集配体骨架的交集。没有原始数据时如实报「跳过」。"""
    if pdb_dir is None:
        return {
            "ok": False,
            "reason": f"跳过：找不到原始复合物目录（用 --pdb-dir 或 {PDB_DIR_ENV} 指定）",
        }
    index = ligand_index(pdb_dir, train_ids, refresh=refresh)
    if not index.get("available"):
        return {"ok": False, "reason": index.get("reason", "配体索引建不起来")}

    known = set(index["index"])
    keyed = [item for item in compounds if item.get("inchikey14")]
    matched = [item for item in keyed if item["inchikey14"] in known]
    return {
        "ok": True,
        "index_size": len(known),
        "coverage": index["coverage"],
        "records": index["records"],
        "keyed": index["keyed"],
        "parsed_failed": index["failed"],
        "missing_files": index["missing"],
        "checked": len(keyed),
        "unkeyed": len(compounds) - len(keyed),
        "matched": len(matched),
        "share": round(len(matched) / len(keyed), 4) if keyed else None,
        "examples": sorted(item["id"] for item in matched)[:RCSB_SAMPLE],
    }


def _read_split() -> dict[str, set[str]]:
    """DTA 的划分文件。缺文件时返回三个空集合——审计仍然能跑，只是这一项报 0。"""
    try:
        payload = json.loads(SPLIT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"train": set(), "val": set(), "test": set()}
    return {
        key: {str(item).lower() for item in payload.get(key) or []}
        for key in ("train", "val", "test")
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def tier_of(count: int) -> str:
    if count < TIER_LOW:
        return "L"
    if count < TIER_HIGH:
        return "M"
    return "H"


def verdict_of(audit: dict[str, Any]) -> dict[str, Any]:
    """这个靶点能回答哪个问题。不能回答时把原因写成人话，别只给一个 False。"""
    compounds = audit["counts"]["compounds"]
    scaffolds = audit["scaffolds"]
    notes: list[str] = []

    h1 = compounds >= MIN_FOR_H1
    if not h1:
        notes.append(f"化合物只有 {compounds} 个（H1 需要 ≥{MIN_FOR_H1}），切完测试集太小")

    share = scaffolds.get("largest_share") if scaffolds.get("available") else None
    h3 = True
    if compounds < MIN_FOR_H3:
        h3 = False
        notes.append(f"化合物 {compounds} 个（H3 需要 ≥{MIN_FOR_H3}），训练集不足以训任何东西")
    if not scaffolds.get("available"):
        h3 = False
        notes.append(f"骨架算不出来：{scaffolds.get('reason')}")
    else:
        if share is not None and share > LARGEST_SHARE_LIMIT:
            h3 = False
            notes.append(
                f"最大骨架占 {share:.0%}（上限 {LARGEST_SHARE_LIMIT:.0%}），"
                "骨架切分后测试集只剩一两个骨架"
            )
        if scaffolds.get("groups_ge2", 0) < MIN_SCAFFOLD_GROUPS:
            h3 = False
            notes.append(
                f"size≥2 的骨架组只有 {scaffolds.get('groups_ge2')} 个"
                f"（需要 ≥{MIN_SCAFFOLD_GROUPS}），切不出像样的训练/测试对比"
            )

    pdb = audit["pdb"]
    if pdb.get("ok") and len(pdb.get("in_train") or []) >= TRAIN_OVERLAP_FLAG:
        notes.append(
            f"该靶点在 DTA 训练集里有 {len(pdb['in_train'])} 个复合物——"
            "「预训练早已见过」是混杂因素，本靶点上任何 Δ 都不能直接归因到微调"
        )
    ligands = pdb.get("ligands") or {}
    if ligands.get("ok") and (ligands.get("share") or 0) > LIGAND_OVERLAP_LIMIT:
        notes.append(
            f"**化合物级重叠 {ligands['share']:.1%}**（{ligands['matched']}/"
            f"{ligands['checked']}，上限 {LIGAND_OVERLAP_LIMIT:.0%}）——"
            "这些分子模型训练时见过，Δ 是记忆不是泛化，换靶点"
        )

    return {"tier": audit["tier"], "h1": h1, "h3": h3, "notes": notes}


# ---------------------------------------------------------------------------
# 审计一个靶点
# ---------------------------------------------------------------------------
def audit_target(accession: str, args: argparse.Namespace) -> dict[str, Any]:
    payload = fetch_activities(
        accession,
        min_pchembl=args.min_pchembl,
        measure_types=args.measure_types,
        refresh=args.refresh,
        timeout=args.timeout,
    )
    rows = payload["rows"]
    compounds, dropped = aggregate(
        rows,
        min_pchembl=args.min_pchembl,
        min_confidence=args.min_confidence,
        assay_type=args.assay_type,
    )
    scaffolds = scaffold_stats(compounds)
    pdb = pdb_overlap(
        accession,
        timeout=args.timeout,
        compounds=compounds,
        pdb_dir=dta_pdb_dir(args.pdb_dir),
        refresh=args.refresh,
    )

    values = [item["pchembl"] for item in compounds]
    years = [year for item in compounds for year in item["years"]]
    audit = {
        "uniprot": payload["uniprot"],
        "target": payload["target"],
        "fetched_at": payload["fetched_at"],
        "cached": bool(payload.get("cached")),
        "filters": {
            "min_pchembl": args.min_pchembl,
            "measure_types": args.measure_types,
            "min_confidence": args.min_confidence,
            "assay_type": args.assay_type,
            "relation": chembl.RELATION_EQUAL,
        },
        "aggregation": "median",
        "counts": {
            "rows_total": payload["total_all"],
            "rows_matched": payload["total_matched"],
            "fetched": len(rows),
            "truncated": payload["truncated"],
            "dropped": dropped,
            "compounds": len(compounds),
        },
        "potency": {
            "min": round(min(values), 3) if values else None,
            "median": round(float(statistics.median(values)), 3) if values else None,
            "max": round(max(values), 3) if values else None,
            "ge6": sum(1 for value in values if value >= 6),
            "ge7": sum(1 for value in values if value >= 7),
            "suspect": sum(1 for value in values if value >= SUSPECT_POTENCY),
        },
        "assays": {
            "count": len({assay for item in compounds for assay in item["assays"]}),
            "types": _assay_type_breakdown(rows),
        },
        "years": {"min": min(years) if years else None, "max": max(years) if years else None},
        "confidence_sweep": confidence_sweep(
            rows, min_pchembl=args.min_pchembl, assay_type=args.assay_type
        ),
        "scaffolds": scaffolds,
        "pdb": pdb,
        "compounds": compounds,
        "tier": tier_of(len(compounds)),
    }
    audit["verdict"] = verdict_of(audit)
    return audit


def _assay_type_breakdown(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """原始记录里各实验体系各多少条（B 生化 / F 细胞 / A 动物 / T 组织 / P 物理）。"""
    table: dict[str, int] = {}
    for row in rows:
        key = str(row.get("assay_type") or "?")
        table[key] = table.get(key, 0) + 1
    return dict(sorted(table.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render(audits: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(
        "| 靶点 | 档位 | 化合物 | 中位 pK | 骨架 | 最大骨架 | "
        "PDB train/val/test | 配体重叠 | H1 | H3 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for audit in audits:
        scaffold = audit["scaffolds"]
        if scaffold.get("available"):
            scaffold_text = f"{scaffold['n_scaffolds']}"
            share_text = f"{scaffold['largest_share']:.0%}"
        else:
            scaffold_text = "—"
            share_text = "—"
        pdb = audit["pdb"]
        if pdb.get("ok"):
            pdb_text = (
                f"{len(pdb['in_train'])}/{len(pdb['in_val'])}/{len(pdb['in_test'])}"
                f"（共 {pdb['entries']}）"
            )
            ligands = pdb.get("ligands") or {}
            share = ligands.get("share") if ligands.get("ok") else None
            ligand_text = "—" if share is None else f"{share:.1%}"
        else:
            pdb_text = "检索失败"
            ligand_text = "—"
        verdict = audit["verdict"]
        lines.append(
            f"| {audit['uniprot']} {audit['target']['name']} | {audit['tier']} | "
            f"{audit['counts']['compounds']} | {audit['potency']['median']} | "
            f"{scaffold_text} | {share_text} | {pdb_text} | {ligand_text} | "
            f"{'✓' if verdict['h1'] else '✗'} | {'✓' if verdict['h3'] else '✗'} |"
        )

    lines.append("")
    for audit in audits:
        lines.extend(_render_detail(audit))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_detail(audit: dict[str, Any]) -> list[str]:
    counts = audit["counts"]
    dropped = counts["dropped"]
    lines = [
        f"### {audit['uniprot']} · {audit['target']['name']}（{audit['target']['chembl_id']}，"
        f"档位 {audit['tier']}）",
        "",
        f"- 活性记录：全部 {counts['rows_total']} 条，过滤后 {counts['rows_matched']} 条，"
        f"取回 {counts['fetched']} 条"
        + ("（读自本地缓存）" if audit["cached"] else "（本次联网取回）")
        + ("（**已截断，档位不可信**）" if counts["truncated"] else ""),
        "- 剔除：" + "、".join(
            f"{DROP_LABELS.get(key, key)} {value}"
            for key, value in dropped.items()
            if value
        ),
        f"- 化合物 {counts['compounds']} 个，pK 区间 "
        f"{audit['potency']['min']}–{audit['potency']['max']}（中位 {audit['potency']['median']}），"
        f"≥7 的 {audit['potency']['ge7']} 个，"
        f"≥{SUSPECT_POTENCY:g} 的可疑值 {audit['potency']['suspect']} 个",
        "- 置信度门槛 → 化合物数："
        + "，".join(f"{floor}→{count}" for floor, count in audit["confidence_sweep"].items()),
        f"- assay {audit['assays']['count']} 个，实验体系分布 "
        + "，".join(f"{key} {value} 条" for key, value in audit["assays"]["types"].items()),
        f"- 年份 {audit['years']['min']}–{audit['years']['max']}",
    ]
    scaffold = audit["scaffolds"]
    if scaffold.get("available"):
        lines.append(
            f"- 骨架 {scaffold['n_scaffolds']} 个，最大一组 {scaffold['largest_size']} 个"
            f"（{scaffold['largest_share']:.0%}），size≥2 的 {scaffold['groups_ge2']} 组，"
            f"size≥5 的 {scaffold['groups_ge5']} 组"
            + (f"；{scaffold['failed']} 个结构没算出骨架" if scaffold.get("failed") else "")
        )
    else:
        lines.append(f"- 骨架：{scaffold.get('reason')}")

    pdb = audit["pdb"]
    if pdb.get("ok"):
        lines.append(
            f"- PDB 复合物：该靶点 {pdb['entries']} 个带配体的条目，"
            f"其中 train {len(pdb['in_train'])} / val {len(pdb['in_val'])} / "
            f"test {len(pdb['in_test'])}"
        )
        ligands = pdb.get("ligands") or {}
        if ligands.get("ok"):
            share = ligands.get("share")
            lines.append(
                f"- PDB 配体：训练集 {ligands['index_size']} 个骨架，"
                f"覆盖 {ligands['keyed']}/{ligands['records']} 个复合物"
                f"（{ligands['coverage']:.0%}；失败 {ligands['parsed_failed']}），"
                f"本靶点 {ligands['checked']} 个化合物里**命中 {ligands['matched']} 个**"
                + (f"（{share:.1%}）" if share is not None else "")
                + (f"，例：{'、'.join(ligands['examples'])}" if ligands.get("examples") else "")
                + "。重叠率是**下限**：解析失败的配体只能漏判，不会误判"
            )
            if ligands["coverage"] < 0.8:
                lines.append(
                    f"  - ⚠ 覆盖率只有 {ligands['coverage']:.0%}，这一项偏弱；"
                    "该数据集有相当一部分配体用全芳香键写法，RDKit 无法 kekulize。"
                    "判混杂时以复合物级 + 覆盖率一起看，不要只信重叠率"
                )
        else:
            lines.append(f"- PDB 配体：{ligands.get('reason')}")
    else:
        lines.append(f"- PDB：检索失败（{pdb['error']}）")

    verdict = audit["verdict"]
    lines.append(
        f"- 判定：H1 {'可做' if verdict['h1'] else '不可做'}，"
        f"H3 {'可做' if verdict['h3'] else '不可做'}"
    )
    for note in verdict["notes"]:
        lines.append(f"  - {note}")
    return lines


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------
def _write_json(path: Path, payload: Any) -> None:
    """与包内 `products.write_json_atomic` 同一个动作，只是这里在包外，不引它。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)


# ---------------------------------------------------------------------------
# 切分（build）
# ---------------------------------------------------------------------------
MIN_SCAFFOLD_GROUP = 5
"""骨架组小于这个数就整组进训练集。

切碎了不划算：一个 2 个分子的骨架进测试集，既撑不起 ρ，又白占掉一个「没见过的骨架」
位置。代价是测试集只由少数几个大骨架组成，所以 `build` 会把 min_group 取 2/3/5 的
结果并排列出来，让选择有依据而不是默认值说了算。
"""

TIME_MIN_SPAN = 3
"""时间切分至少要横跨这么多年，否则「老→新」只是把同一年的化合物随机劈开。"""

MIN_TEST_COMPOUNDS = 15
"""测试集少于这个数就不算能评估：ρ 的置信区间会宽到 ±0.5 以上。"""

POWER_Z = 1.96

POTENCY_SHIFT_LIMIT = 0.5
"""两侧中位 pK 差超过这么多就说明切分带来了分布偏移。

时间切分几乎必然有这个偏移（越晚发表的越强），但**偏移本身不是错误**——它是时间
切分要模拟的真实情形。关键是它必须被看见：一个「测试集整体更强」的切分会让任何
打分函数的 RMSE 变差、排序指标未必变差，两者混在一起解读就会得出反的结论。
"""


def load_audit(uniprot: str, select_dir: Path) -> dict[str, Any]:
    """读 `select` 的快照。**build 不重新取数**：切分必须建立在审计过的那一份上。"""
    path = select_dir / f"{uniprot.upper()}.json"
    if not path.is_file():
        raise FileNotFoundError(f"找不到审计快照 {path}。先跑 select --target {uniprot}。")
    return json.loads(path.read_text(encoding="utf-8"))


def random_split(ids: Sequence[str], *, seed: int) -> dict[str, str]:
    """S1：随机 80/10/10。**这是上界不是结果**——同一骨架会同时出现在两侧。

    两侧各取 10%，余下全给训练集：小数据集上按 0.8/0.1/0.1 逐个取整会把验证集与
    测试集取成 0，而「测试集 1 个分子」算出来的 ρ 是 ±∞，不是上界。
    """
    pool = sorted(ids)
    total = len(pool)
    random.Random(seed).shuffle(pool)
    n_test = max(1, round(total * 0.1))
    n_val = max(1, round(total * 0.1))
    n_train = max(total - n_val - n_test, 1)
    assign: dict[str, str] = {}
    for index, cid in enumerate(pool):
        assign[cid] = "train" if index < n_train else ("val" if index < n_train + n_val else "test")
    return assign


def scaffold_split(
    compounds: Sequence[dict[str, Any]],
    by_compound: dict[str, str],
    *,
    min_group: int,
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """S2：按 Murcko 骨架整组切分，**主终点用的就是这一份**。

    贪心：小组先整组进训练集，剩下的大组按「大 → 小」依次填训练 / 验证，其余全进测试。
    大组在前是有意的——大骨架如果落进测试集，测试集就变成一个骨架的内部排序题，
    衡量的是同系物插值能力，不是泛化。
    """
    groups: dict[str, list[str]] = {}
    unkeyed: list[str] = []
    for item in compounds:
        scaffold = by_compound.get(item["id"])
        if not scaffold:
            unkeyed.append(item["id"])
            continue
        groups.setdefault(scaffold, []).append(item["id"])

    total = len(compounds)
    want = {"train": total * 0.8, "val": total * 0.1}
    assign: dict[str, str] = {cid: "train" for cid in unkeyed}
    counts = {"train": len(unkeyed), "val": 0, "test": 0}

    small = [(sc, ids) for sc, ids in groups.items() if len(ids) < min_group]
    big = [(sc, ids) for sc, ids in groups.items() if len(ids) >= min_group]
    if not big:
        return None, {
            "reason": f"没有一个骨架组达到 {min_group} 个分子，骨架切分无从谈起",
            "groups_total": len(groups),
            "groups_big": 0,
        }

    for _scaffold, ids in small:
        for cid in ids:
            assign[cid] = "train"
        counts["train"] += len(ids)

    ordered = sorted(big, key=lambda kv: (-len(kv[1]), kv[0]))
    for _scaffold, ids in ordered:
        side = "train" if counts["train"] < want["train"] else (
            "val" if counts["val"] < want["val"] else "test")
        for cid in ids:
            assign[cid] = side
        counts[side] += len(ids)

    # 贪心可能把测试集吃空（大组偏少时）：从验证集、再从严训练集各挪一个最大的组过去。
    moved: list[str] = []
    for _ in range(2):
        if counts["test"] > 0:
            break
        donor = "val" if counts["val"] > 0 else "train"
        candidates = [
            (sc, ids) for sc, ids in ordered if all(assign[cid] == donor for cid in ids)
        ]
        if not candidates:
            break
        scaffold, ids = min(candidates, key=lambda kv: len(kv[1]))
        for cid in ids:
            assign[cid] = "test"
        counts[donor] -= len(ids)
        counts["test"] += len(ids)
        moved.append(scaffold)

    info = {
        "groups_total": len(groups),
        "groups_big": len(big),
        "groups_small": len(small),
        "big_compounds": sum(len(ids) for _sc, ids in big),
        "small_compounds": sum(len(ids) for _sc, ids in small),
        "unkeyed": len(unkeyed),
        "moved_to_test": moved,
    }
    return assign, info


def time_split(compounds: Sequence[dict[str, Any]]) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """S3：按首次报道年份切，老 → 新。**切在年份边界上，不按分子数硬切**。

    按分子数切会把同一年劈成两半，而同年同批发表的往往是一系列类似物——那等于把
    骨架泄漏又放回来了，正是时间切分要避免的东西。
    """
    dated: list[tuple[int, str]] = []
    undated: list[str] = []
    for item in compounds:
        years = item.get("years") or []
        if years:
            dated.append((min(years), item["id"]))
        else:
            undated.append(item["id"])

    if not dated:
        return None, {"reason": "没有任何化合物带年份"}
    year_counts: dict[int, int] = {}
    for year, _cid in dated:
        year_counts[year] = year_counts.get(year, 0) + 1
    years = sorted(year_counts)
    span = years[-1] - years[0]
    if len(years) < 3 or span < TIME_MIN_SPAN:
        return None, {"reason": f"年份只有 {len(years)} 个、跨度 {span} 年，太窄"}

    total = len(dated)
    cut_train = cut_val = None
    cumulative = 0
    for year in years:
        cumulative += year_counts[year]
        if cut_train is None and cumulative >= total * 0.8:
            cut_train = year
        elif cut_train is not None and cut_val is None and cumulative >= total * 0.9:
            cut_val = year
    if cut_val is None:
        cut_val = years[-1]

    assign = {cid: "train" for cid in undated}
    for year, cid in dated:
        assign[cid] = "train" if year < cut_train else ("val" if year < cut_val else "test")
    info = {
        "cut_train": cut_train,
        "cut_val": cut_val,
        "year_min": years[0],
        "year_max": years[-1],
        "undated": len(undated),
    }
    return assign, info


def sides_of(assign: dict[str, str]) -> dict[str, list[str]]:
    sides: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    for cid, side in assign.items():
        sides[side].append(cid)
    for key in sides:
        sides[key].sort()
    return sides


def check_split(
    name: str,
    assign: dict[str, str],
    *,
    scaffold_of: dict[str, str],
    pchembl_of: dict[str, float],
) -> dict[str, Any]:
    """切分体检：两侧骨架是否互斥、活性分布是否可比、测试集够不够。

    骨架互斥是 S2 的**硬约束**，不是提示——破了就说明切分逻辑错了，整份数据作废。
    """
    sides = sides_of(assign)
    scaffolds = {
        side: {scaffold_of[cid] for cid in ids if scaffold_of.get(cid)}
        for side, ids in sides.items()
    }
    overlap = {
        "train_test": len(scaffolds["train"] & scaffolds["test"]),
        "train_val": len(scaffolds["train"] & scaffolds["val"]),
    }
    medians = {
        side: (round(float(statistics.median([pchembl_of[cid] for cid in ids])), 3)
               if ids else None)
        for side, ids in sides.items()
    }
    test_n = len(sides["test"])
    return {
        "name": name,
        "counts": {side: len(ids) for side, ids in sides.items()},
        "scaffold_overlap": overlap,
        "exclusive": overlap["train_test"] == 0 and overlap["train_val"] == 0,
        "pchembl_median": medians,
        "test_ci": round(POWER_Z / math.sqrt(test_n - 1), 3) if test_n > 1 else None,
        "test_enough": test_n >= MIN_TEST_COMPOUNDS,
    }


def _fmt_median(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def min_group_candidates(requested: int) -> list[int]:
    """S2 的 min_group 候选：先按要求的，切不出来再往下降。

    小靶点上 5 这个默认值会把测试集压到十来个分子（ρ 的区间宽到 ±0.6），而模型对
    骨架大小的偏好没有证据支持——降门槛只是把「同骨架 2–4 个分子」的组留在训练集，
    仍然不跨组。降了会在报告里写出来，并记进 `build.json`。
    """
    return sorted({value for value in (requested, 3, 2) if value <= requested}, reverse=True)


def _render_build(checks: Sequence[dict[str, Any]]) -> str:
    lines = [
        "| 切分 | train | val | test | 骨架重叠（train/test） | 中位 pK（train / test） | ρ 的 95% CI 半宽 |",
        "|---|---|---|---|---|---|---|",
    ]
    for check in checks:
        counts = check["counts"]
        medians = check["pchembl_median"]
        ci = check["test_ci"]
        lines.append(
            f"| {check['name']} | {counts['train']} | {counts['val']} | {counts['test']} | "
            f"{check['scaffold_overlap']['train_test']} | "
            f"{_fmt_median(medians['train'])} / {_fmt_median(medians['test'])} | "
            f"{'—' if ci is None else f'±{ci:.2f}'} |"
        )
    lines.append("")
    for check in checks:
        if not check["exclusive"]:
            lines.append(
                f"- {check['name']}：训练集与测试集共享 "
                f"{check['scaffold_overlap']['train_test']} 个骨架"
                + ("（**S2 不该出现，切分有问题**）" if check["name"] == "s2"
                   else "（预期之内：只有 S2 按骨架分组）")
            )
        if not check["test_enough"]:
            lines.append(
                f"- {check['name']}：测试集只有 {check['counts']['test']} 个"
                f"（门槛 {MIN_TEST_COMPOUNDS}），ρ 的区间宽到不可用"
            )
        train_median = check["pchembl_median"]["train"]
        test_median = check["pchembl_median"]["test"]
        if train_median is not None and test_median is not None:
            gap = test_median - train_median
            if abs(gap) > POTENCY_SHIFT_LIMIT:
                lines.append(
                    f"- {check['name']}：测试集中位 pK 比训练集"
                    f"{'高' if gap > 0 else '低'} {abs(gap):.2f}——切分有分布偏移"
                    + ("（时间切分常见：越晚发表的化合物活性越强）"
                       if check["name"] == "s3" else "")
                )
    return "\n".join(lines)


def _scaffold_with_fallback(
    compounds: Sequence[dict[str, Any]],
    by_compound: dict[str, str],
    *,
    requested: int,
) -> tuple[dict[str, str] | None, dict[str, Any], int]:
    """按 `min_group_candidates` 依次试，取第一个测试集够大的。

    返回 `(划分, 说明, 实际用的 min_group)`。降门槛这件事必须看得见——它改变了
    训练集里放进了多少个「同骨架 2–4 个分子」的组，而那是要写进 build.json 的。
    """
    best: tuple[dict[str, str] | None, dict[str, Any], int] = (None, {}, requested)
    for value in min_group_candidates(requested):
        assign, info = scaffold_split(compounds, by_compound, min_group=value)
        if assign is None:
            best = (None, info, value)
            continue
        test_n = sum(1 for side in assign.values() if side == "test")
        if best[0] is None:
            best = (assign, info, value)
        if test_n >= MIN_TEST_COMPOUNDS:
            return assign, info, value
    return best


def _min_group_table(compounds, by_compound) -> str:
    """min_group 取 2/3/5 各切一次，让「测试集多大」是可选的而不是默认值说了算。"""
    rows = ["| min_group | 骨架组 | 进训练集的小组分子数 | train/val/test |",
            "|---|---|---|---|"]
    for value in (2, 3, 5):
        assign, info = scaffold_split(compounds, by_compound, min_group=value)
        if assign is None:
            rows.append(f"| {value} | 0 | — | 切不出来 |")
            continue
        sides = sides_of(assign)
        rows.append(
            f"| {value} | {info['groups_big']} 大 / {info['groups_small']} 小 | "
            f"{info['small_compounds']} | "
            f"{len(sides['train'])}/{len(sides['val'])}/{len(sides['test'])} |"
        )
    return "\n".join(rows)


def cmd_build(args: argparse.Namespace) -> int:
    uniprot = (args.target or "").strip().upper()
    if not uniprot:
        print("要给一个 --target（UniProt 编号），就是 select 用过的那个", file=sys.stderr)
        return 2

    try:
        audit = load_audit(uniprot, Path(args.select_dir))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1

    compounds = audit["compounds"]
    by_compound = (audit.get("scaffolds") or {}).get("by_compound") or {}
    if not by_compound:
        print("审计快照里没有骨架（select 时 RDKit 不可用？），先重跑 select", file=sys.stderr)
        return 1

    pchembl_of = {item["id"]: item["pchembl"] for item in compounds}
    label = (args.label or uniprot).strip()
    out_dir = Path(args.out) / label

    random_assign = random_split([item["id"] for item in compounds], seed=args.seed)
    scaffold_assign, scaffold_info, used_group = _scaffold_with_fallback(
        compounds, by_compound, requested=args.min_group
    )
    time_assign, time_info = time_split(compounds)

    plans: list[tuple[str, dict[str, str] | None]] = [
        ("s1", random_assign),
        ("s2", scaffold_assign),
        ("s3", time_assign),
    ]
    checks = []
    for name, assign in plans:
        if assign is None:
            continue
        check = check_split(name, assign, scaffold_of=by_compound, pchembl_of=pchembl_of)
        checks.append(check)

    s2 = next((check for check in checks if check["name"] == "s2"), None)
    if s2 is not None and not s2["exclusive"]:
        print("S2 的骨架不互斥——切分逻辑有问题，没有写盘", file=sys.stderr)
        return 1

    # 写盘
    for name, assign in plans:
        if assign is None:
            continue
        _write_json(out_dir / "splits" / f"{name}.json", sides_of(assign))

    dataset = {
        "uniprot": uniprot,
        "target": audit["target"],
        "source": str(Path(args.select_dir) / f"{uniprot}.json"),
        "filters": audit["filters"],
        "aggregation": audit["aggregation"],
        "seed": args.seed,
        "min_group": used_group,
        "compounds": [
            {
                **item,
                "scaffold": by_compound.get(item["id"], ""),
                "splits": {
                    name: assign[item["id"]]
                    for name, assign in plans
                    if assign is not None
                },
            }
            for item in compounds
        ],
    }
    _write_json(out_dir / "dataset.json", dataset)

    manifest = {
        "uniprot": uniprot,
        "label": label,
        "generated_at": _stamp(),
        "seed": args.seed,
        "min_group": used_group,
        "requested_min_group": args.min_group,
        "compounds": len(compounds),
        "checks": checks,
        "scaffold": scaffold_info,
        "time": time_info,
    }
    _write_json(out_dir / "build.json", manifest)

    print(f"\n=== {uniprot} · {audit['target']['name']} · 档位 {audit['tier']} ===")
    print(f"化合物 {len(compounds)} 个，骨架 {scaffold_info.get('groups_total')} 组"
          f"（≥{used_group} 个的 {scaffold_info.get('groups_big')} 组）\n")
    if used_group != args.min_group:
        print(f"注意：min_group {args.min_group} 切出来的测试集不到 {MIN_TEST_COMPOUNDS} 个，"
              f"已降到 {used_group}（记在 build.json 的 min_group 里）\n")
    print(_render_build(checks))
    print("\nmin_group 的影响：")
    print(_min_group_table(compounds, by_compound))
    if time_assign is None:
        print(f"\nS3 跳过：{time_info.get('reason')}")
    else:
        print(f"\nS3 切点：<{time_info['cut_train']} 训练、<{time_info['cut_val']} 验证、"
              f"其余测试（无年份的 {time_info['undated']} 个进训练）")
        if sum(1 for side in time_assign.values() if side == "val") == 0:
            print(f"  S3 的验证集为空：{time_info['cut_train']} 年之后的化合物占了九成，"
                  "年份太集中，这一份只能当训练/测试两段用")

    print(f"\n写出：{out_dir}/dataset.json、splits/、build.json")
    usable = [c for c in checks if c["exclusive"] and c["test_enough"] and c["name"] != "s1"]
    if usable:
        print(f"可用于主终点：{', '.join(c['name'] for c in usable)}")
    else:
        print("没有一份切分同时满足「骨架互斥 + 测试集够大」——这个靶点只能当辅助")
    return 0


# ---------------------------------------------------------------------------
# run：B 系列基线
# ---------------------------------------------------------------------------
ECFP_RADIUS = 2
ECFP_BITS = 2048
DEFAULT_K = 5
RANDOM_REPEATS = 100
"""B0 是随机排序，单跑一次等于抛硬币。跑 100 次报均值和标准差，才看得出「随机」长什么样。"""

BOOTSTRAP_ROUNDS = 1000
TOP_FRACTION = 0.1
HIT_PCHEMBL = 7.0
"""top-10% 命中率的「命中」定义：pK ≥ 7（约 100 nM）。"""

MIN_TRAIN_FOR_MODEL = 30
"""训练集少于这个数就不训 B2：5 折里每折只剩几个点，任何模型都只是记住了训练集。"""

DEFAULT_SEEDS = 5
"""模型臂跑几个种子。单次 LightGBM 在 n_test=75 上 ρ 能差 ±0.15，报一次等于报运气。"""

ARM_LABELS = {
    "b0": "随机排序（下界）",
    "b1": "ECFP4 与训练集的最大 Tanimoto",
    "b1k": "ECFP4 kNN 加权预测 pK",
    "b2": "ECFP4 + LightGBM",
    "b3": "VCrossFormer（预训练亲和力）",
    "t1": "VCrossFormer（head-only 微调，只训 affinity_mlp）",
    "t2": "VCrossFormer（全量微调，所有参数）",
    "n1": "B2 + 打乱标签（阴性对照）",
}

ALL_ARMS = ("b0", "b1", "b1k", "b2", "b3", "n1")

MODEL_ARMS = ("b2", "n1")


def load_dataset(target: str, root: Path, label: str | None) -> tuple[dict[str, Any], Path]:
    path = root / (label or target) / "dataset.json"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 {path}。先跑 build --target {target}。")
    return json.loads(path.read_text(encoding="utf-8")), path


def fingerprint(smiles: str, *, cache: dict[str, frozenset[int] | None]) -> frozenset[int] | None:
    """ECFP4（Morgan, radius 2）的「置位集合」。

    存成集合而不是 2048 位数组：Tanimoto 用交并比算，集合运算比遍历数组快一个量级，
    而 B1/B1k 要对每个测试分子比一遍训练集。
    """
    if smiles in cache:
        return cache[smiles]
    rdkit = _rdkit()
    if rdkit is None:
        raise RuntimeError("需要 RDKit（pip install -r requirements-chem.txt）")
    Chem, _ = rdkit
    from rdkit.Chem import AllChem

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        cache[smiles] = None
        return None
    bits = AllChem.GetMorganFingerprintAsBitVect(mol, ECFP_RADIUS, nBits=ECFP_BITS)
    value = frozenset(bits.GetOnBits())
    cache[smiles] = value
    return value


def tanimoto(left: frozenset[int], right: frozenset[int]) -> float:
    if not left and not right:
        return 0.0
    shared = len(left & right)
    return shared / (len(left) + len(right) - shared)


def _ranks(values: Sequence[float]) -> list[float]:
    """平均秩。并列必须给同一个秩，否则 ρ 会被排序的先后顺序左右。"""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        stop = index
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
            stop += 1
        average = (index + stop) / 2 + 1
        for position in range(index, stop + 1):
            ranks[order[position]] = average
        index = stop + 1
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    n = len(left)
    if n < 3:
        return None
    mean_l = sum(left) / n
    mean_r = sum(right) / n
    dev_l = [value - mean_l for value in left]
    dev_r = [value - mean_r for value in right]
    norm_l = math.sqrt(sum(value * value for value in dev_l))
    norm_r = math.sqrt(sum(value * value for value in dev_r))
    if norm_l == 0 or norm_r == 0:
        return None
    return sum(a * b for a, b in zip(dev_l, dev_r)) / (norm_l * norm_r)


def spearman(scores: Sequence[float], truth: Sequence[float]) -> float | None:
    return _pearson(_ranks(scores), _ranks(truth))


def bootstrap_ci(
    scores: Sequence[float], truth: Sequence[float], *, seed: int
) -> tuple[float | None, float | None]:
    """对**测试化合物**重采样 1000 次，取 ρ 的 2.5 / 97.5 分位。

    重采样单位是化合物而不是名次：同一批分子里的名次不是独立观测。
    """
    if len(scores) < 4:
        return None, None
    generator = random.Random(seed)
    values: list[float] = []
    for _ in range(BOOTSTRAP_ROUNDS):
        picks = [generator.randrange(len(scores)) for _ in scores]
        rho = spearman([scores[i] for i in picks], [truth[i] for i in picks])
        if rho is not None:
            values.append(rho)
    if not values:
        return None, None
    values.sort()

    def percentile(q: float) -> float:
        position = q * (len(values) - 1)
        low = int(math.floor(position))
        high = min(low + 1, len(values) - 1)
        weight = position - low
        return values[low] * (1 - weight) + values[high] * weight

    return percentile(0.025), percentile(0.975)


def top_hit_rate(scores: Sequence[float], truth: Sequence[float]) -> float | None:
    """按打分取前 10%，其中 pK ≥ 7 的比例。最接近「先做哪几个」这个决策。"""
    n = len(scores)
    take = max(1, round(n * TOP_FRACTION))
    order = sorted(range(n), key=lambda index: -scores[index])
    top = [truth[index] for index in order[:take]]
    return sum(1 for value in top if value >= HIT_PCHEMBL) / len(top)


def rmse_r2(pred: Sequence[float], truth: Sequence[float]) -> tuple[float | None, float | None]:
    n = len(pred)
    if n < 3:
        return None, None
    mse = sum((p - t) ** 2 for p, t in zip(pred, truth)) / n
    mean = sum(truth) / n
    total = sum((t - mean) ** 2 for t in truth)
    residual = sum((p - t) ** 2 for p, t in zip(pred, truth))
    r2 = None if total == 0 else 1 - residual / total
    return math.sqrt(mse), r2


def _fingerprints(records: Sequence[dict[str, Any]], cache) -> tuple[list, list[dict], int]:
    """返回 (置位集合列表, 能解析的记录, 解析失败数)。"""
    frames, kept, failed = [], [], 0
    for record in records:
        bits = fingerprint(record["smiles"], cache=cache)
        if bits is None:
            failed += 1
            continue
        frames.append(bits)
        kept.append(record)
    return frames, kept, failed


def _b1_scores(train_fp, train_pk, test_fp) -> list[float]:
    """B1：与训练集里最像的那个有多像。**这是筛选式打分**，不看活性大小。"""
    scores = []
    for bits in test_fp:
        scores.append(max((tanimoto(bits, other) for other in train_fp), default=0.0))
    return scores


def _b1k_scores(train_fp, train_pk, test_fp, *, k: int) -> list[float]:
    """B1k：取最近的 k 个邻居，按 Tanimoto 加权平均它们的 pK。

    与 B1 的差别就是「像不像」和「像的那个有多强」——同一批测试分子全是活性物，
    前者区分不出强弱，后者可以。两条都跑，差多少本身就是答案。
    """
    scores = []
    fallback = sum(train_pk) / len(train_pk)
    for bits in test_fp:
        sims = sorted(
            ((tanimoto(bits, other), pk) for other, pk in zip(train_fp, train_pk)),
            reverse=True,
        )[:k]
        weight = sum(sim for sim, _pk in sims)
        scores.append(
            sum(sim * pk for sim, pk in sims) / weight if weight > 0 else fallback
        )
    return scores


def _b2_scores(
    train_fp, train_pk, test_fp, *, seed: int, shuffle: bool
) -> tuple[list[float], dict[str, Any]]:
    """B2：ECFP4 位向量 + LightGBM。轮数用训练集内的 5 折早停选，测试集不参与。"""
    try:
        import numpy as np
        import lightgbm as lgb
    except ImportError as error:  # 可选依赖，缺了只影响这一条臂
        raise RuntimeError(
            "B2 需要 numpy 与 lightgbm："
            "uv pip install --python .venv/bin/python lightgbm"
        ) from error

    def matrix(frames):
        out = np.zeros((len(frames), ECFP_BITS), dtype=np.uint8)
        for row, bits in enumerate(frames):
            out[row, list(bits)] = 1
        return out

    X = matrix(train_fp)
    Xt = matrix(test_fp)
    y = np.asarray(train_pk, dtype=np.float32)
    if shuffle:
        y = np.random.default_rng(seed).permutation(y)

    params = {
        "objective": "regression",
        "metric": "l2",
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 5,
        "feature_fraction": 0.3,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbose": -1,
        "seed": seed,
        "num_threads": 4,
        "deterministic": True,
        "force_row_wise": True,
    }

    folds = np.array_split(np.random.default_rng(seed + 1).permutation(len(X)), 5)
    rounds: list[int] = []
    for hold in range(len(folds)):
        valid = folds[hold]
        rest = np.concatenate([folds[i] for i in range(len(folds)) if i != hold])
        booster = lgb.train(
            params,
            lgb.Dataset(X[rest], label=y[rest]),
            num_boost_round=600,
            valid_sets=[lgb.Dataset(X[valid], label=y[valid])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        rounds.append(int(booster.best_iteration or 100))

    chosen = max(int(statistics.mean(rounds)) if rounds else 300, 50)
    model = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=chosen)
    return [float(value) for value in model.predict(Xt)], {
        "rounds_cv": rounds,
        "rounds_used": chosen,
        "shuffled": shuffle,
    }


def _score(
    arm: str,
    train_fp: Sequence[frozenset[int]],
    train_pk: Sequence[float],
    test_fp: Sequence[frozenset[int]],
    *,
    seed: int,
    k: int,
) -> tuple[list[float], dict[str, Any]]:
    """单次打分。只看训练/测试两边，不碰评级——评级在 `_evaluate` 里单独做。"""
    if arm == "b0":
        generator = random.Random(seed)
        return [generator.random() for _ in test_fp], {}
    if arm == "b1":
        if not train_fp:
            raise RuntimeError("训练集里没有一个分子能解析出指纹")
        return _b1_scores(train_fp, train_pk, test_fp), {
            "note": "相似度打分，没有绝对尺度，RMSE 不适用"
        }
    if arm == "b1k":
        if not train_fp:
            raise RuntimeError("训练集里没有一个分子能解析出指纹")
        return _b1k_scores(train_fp, train_pk, test_fp, k=k), {"k": k}
    if arm in MODEL_ARMS:
        if len(train_fp) < MIN_TRAIN_FOR_MODEL:
            raise RuntimeError(
                f"训练集只有 {len(train_fp)} 个可解析分子（门槛 {MIN_TRAIN_FOR_MODEL}），"
                "不训模型"
            )
        return _b2_scores(train_fp, train_pk, test_fp, seed=seed, shuffle=(arm == "n1"))
    raise RuntimeError(f"未知的臂 {arm}")


def _evaluate(
    arm: str,
    scores: Sequence[float],
    truth: Sequence[float],
    *,
    seed: int,
    note: dict[str, Any],
    n_train: int,
    n_test: int,
    skipped: int,
) -> dict[str, Any]:
    rho = spearman(scores, truth)
    low, high = bootstrap_ci(scores, truth, seed=seed)
    if arm == "b0":
        # 单次随机的 ρ 是抛硬币（实测 STING 上它给出 +0.29，而 100 次的均值是 +0.01）。
        # 表格里报重复的均值，区间按「单次抽样」的 ±1.96σ 给——那是解读随机基线时
        # 真正该看的量，而不是某一次抽到的数。
        rho = note["mean_rho"]
        low = rho - 1.96 * note["sd_rho"]
        high = rho + 1.96 * note["sd_rho"]
    rmse, r2 = rmse_r2(scores, truth) if arm in ("b1k", "b2", "b3", "t1", "t2", "n1") else (None, None)
    metrics = {
        "spearman": None if rho is None else round(rho, 4),
        "spearman_ci": [
            None if low is None else round(low, 4),
            None if high is None else round(high, 4),
        ],
        "top_hit": None if arm == "b0" else round(top_hit_rate(scores, truth), 4),
        "rmse": None if rmse is None else round(rmse, 4),
        "r2": None if r2 is None else round(r2, 4),
        "n_train": n_train,
        "n_test": n_test,
        "skipped": skipped,
        "hits_in_test": sum(1 for value in truth if value >= HIT_PCHEMBL),
    }
    return {"arm": arm, "label": ARM_LABELS.get(arm, arm), "metrics": metrics, "note": note}


def run_arm(
    arm: str,
    train: Sequence[dict[str, Any]],
    test: Sequence[dict[str, Any]],
    *,
    seeds: Sequence[int],
    k: int,
    cache: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一条臂。

    **模型臂跑多个种子**：单次 LightGBM 的 ρ 在 n_test=75 上能差出 ±0.15（实测阴性
    对照跨种子是 -0.13 到 +0.35），报一次等于报运气。主指标取「各种子打分的平均」
    的 ρ，种子间的离散度单独报出来当稳定性。

    `b3` 走另一条路（要构象、要子进程），而且推理是确定性的，不按种子集成。
    """
    if arm in ("b3", "t1", "t2"):
        return _run_model_arm(arm, test, context or {}, seeds=seeds)
    train_fp, train_kept, train_failed = _fingerprints(train, cache)
    test_fp, test_kept, test_failed = _fingerprints(test, cache)
    if not test_kept:
        raise RuntimeError("测试集里没有一个分子能解析出指纹")
    truth = [record["pchembl"] for record in test_kept]
    train_pk = [record["pchembl"] for record in train_kept]

    if arm == "b0":
        generator = random.Random(seeds[0])
        rhos: list[float] = []
        for _ in range(RANDOM_REPEATS):
            rho = spearman([generator.random() for _ in test_kept], truth)
            if rho is not None:
                rhos.append(rho)
        note = {
            "repeats": len(rhos),
            "mean_rho": statistics.mean(rhos) if rhos else 0.0,
            "sd_rho": statistics.pstdev(rhos) if len(rhos) > 1 else 0.0,
        }
        scores, extra = _score(arm, train_fp, train_pk, test_fp, seed=seeds[0], k=k)
        note.update(extra)
    elif arm in MODEL_ARMS:
        runs: list[list[float]] = []
        per_seed: list[float | None] = []
        for seed in seeds:
            scores_i, extra = _score(arm, train_fp, train_pk, test_fp, seed=seed, k=k)
            runs.append(scores_i)
            per_seed.append(spearman(scores_i, truth))
        scores = [statistics.mean(column) for column in zip(*runs)]
        clean = [value for value in per_seed if value is not None]
        note = {
            "seeds": list(seeds),
            "per_seed_rho": [None if v is None else round(v, 4) for v in per_seed],
            "seed_mean": round(statistics.mean(clean), 4) if clean else None,
            "seed_sd": round(statistics.pstdev(clean), 4) if len(clean) > 1 else None,
            "ensemble": "各种子打分的算术平均",
            **extra,
        }
    else:
        scores, note = _score(arm, train_fp, train_pk, test_fp, seed=seeds[0], k=k)

    result = _evaluate(
        arm,
        scores,
        truth,
        seed=seeds[0],
        note=note,
        n_train=len(train_kept),
        n_test=len(test_kept),
        skipped=train_failed + test_failed,
    )
    result["predictions"] = [
        {"id": record["id"], "pchembl": record["pchembl"], "score": round(score, 5)}
        for record, score in zip(test_kept, scores)
    ]
    return result


# ---------------------------------------------------------------------------
# B3：VCrossFormer
# ---------------------------------------------------------------------------
B3_DIRNAME = "b3"
B3_AFF_UNIT = "Ki"
"""与 `score_affinity` 的默认一致。换成 Kd / IC50 会得到另一套数值，彼此不可比。"""

B3_TIMEOUT = 3600.0
EMBED_SEED = 42
"""构象生成的种子。固定它，重跑才得到同一份输入——变的是别的东西时才看得清。"""


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)


def ensure_conformers(records, directory, *, seed: int = EMBED_SEED):
    """生成 3D 构象落盘。**实现在 `domain/finetune.embed_conformers`**，这里只做转发。

    复用与新建的分界在 manifest 里，这一层看不到——所以只报「可用多少、失败多少」，
    不编一个自己也算不准的「新建 N 个」。
    """
    paths, failed = finetune.embed_conformers(records, directory, seed=seed)
    print(f"    构象：{len(paths)} 个可用、{len(failed)} 个失败")
    return paths, failed


def _run_model_arm(
    arm: str,
    test: Sequence[dict[str, Any]],
    context: dict[str, Any],
    *,
    seeds: Sequence[int],
) -> dict[str, Any]:
    """把测试集交给 `virtual_screen.py`，读回 pK。B3 用预训练权重，T1 用微调后的。

    **走子进程而不是 import**：torch / dgl 只在 `models/DTA/.venv` 里，主仓一行都不
    引它们（`check_chemistry_gate` 与 PyInstaller 都盯着这一条）。
    """
    out_dir = Path(context.get("out_dir") or ".")
    work = out_dir / arm
    pocket = out_dir / "pocket" / "pocket.pdb"
    if not pocket.is_file():
        raise RuntimeError(f"没有口袋文件 {pocket}。先跑 pocket 子命令。")

    config = _model_config(arm, out_dir, context)
    paths, failed = ensure_conformers(
        test, work / "conformers", seed=EMBED_SEED
    )
    if not paths:
        raise RuntimeError("B3：一个分子都没生成出构象")

    smiles_of = {str(record["id"]): record["smiles"] for record in test}
    tasks = [
        {"id": cid, "path": str(path), "smiles": smiles_of.get(cid, "")}
        for cid, path in paths.items()
    ]
    tasks_path = work / "tasks.json"
    _write_json(tasks_path, tasks)

    try:
        python = affinity.dta_python()
        root = affinity.dta_root()
    except Exception as error:
        raise RuntimeError(f"B3 需要 DTA 的独立环境：{error}") from error

    scores_csv = work / "scores.csv"
    argv = [
        python,
        str(root / "virtual_screen.py"),
        "--config",
        str(config),
        "--pocket",
        str(pocket),
        "--tasks",
        str(tasks_path),
        "--output",
        str(scores_csv),
        "--aff_unit",
        B3_AFF_UNIT,
        "--device",
        str(context.get("device") or "auto"),
    ]
    log_path = work / "run.log"
    print(f"    {arm} 推理：{len(tasks)} 个分子，口袋 {pocket.name}，配置 {config.name} …")
    started = time.monotonic()
    run = asyncio.run(
        affinity.run_batch(
            argv, cwd=root, timeout=float(context.get("timeout") or B3_TIMEOUT),
            log_path=log_path,
        )
    )
    elapsed = time.monotonic() - started
    if run.timed_out:
        raise RuntimeError(f"B3 推理超时（{elapsed:.0f}s）。日志：{log_path}")
    if run.returncode != 0:
        tail = "\n".join(run.output.splitlines()[-8:])
        raise RuntimeError(f"B3 退出码 {run.returncode}。日志：{log_path}\n{tail}")
    if not scores_csv.is_file():
        raise RuntimeError(f"B3 没有产出 {scores_csv.name}。日志：{log_path}")

    rows = affinity.read_scores(scores_csv)
    scores_of = {str(row["id"]): float(row["pk"]) for row in rows}
    print(f"    {arm} 完成（{elapsed:.1f}s）：{len(scores_of)}/{len(tasks)} 打上分")

    kept = [record for record in test if str(record["id"]) in scores_of]
    if not kept:
        raise RuntimeError(f"B3 一个分子都没打上分。日志：{log_path}")
    scores = [scores_of[str(record["id"])] for record in kept]
    truth = [float(record["pchembl"]) for record in kept]
    result = _evaluate(
        arm,
        scores,
        truth,
        seed=seeds[0],
        note={
            "pocket": str(pocket),
            "config": str(config),
            "fit": config.parent.parent.name,
            "aff_unit": B3_AFF_UNIT,
            "failed": failed,
            "seconds": round(elapsed, 1),
            "engine": "vcrossformer",
        },
        n_train=0,
        n_test=len(kept),
        skipped=len(test) - len(kept) + len(failed),
    )
    result["predictions"] = [
        {"id": str(record["id"]), "pchembl": float(record["pchembl"]), "score": round(score, 5)}
        for record, score in zip(kept, scores)
    ]
    return result


def _render_runs(results: Sequence[dict[str, Any]]) -> str:
    lines = [
        "| 臂 | 说明 | ρ | ρ 的 95% CI | top-10% 命中 | RMSE | R² |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        m = result["metrics"]
        ci = m["spearman_ci"]
        rho = "—" if m["spearman"] is None else f"{m['spearman']:+.3f}"
        ci_text = "—" if ci[0] is None else f"[{ci[0]:+.2f}, {ci[1]:+.2f}]"
        hit = "—" if m["top_hit"] is None else f"{m['top_hit']:.0%}"
        rmse = "—" if m["rmse"] is None else f"{m['rmse']:.3f}"
        r2 = "—" if m["r2"] is None else f"{m['r2']:+.3f}"
        lines.append(
            f"| {result['arm']} | {result['label']} | {rho} | {ci_text} | "
            f"{hit} | {rmse} | {r2} |"
        )
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    target = (args.target or "").strip().upper()
    if not target:
        print("要给一个 --target（UniProt 编号）", file=sys.stderr)
        return 2

    try:
        dataset, path = load_dataset(target, Path(args.root), args.label)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1

    split_path = path.parent / "splits" / f"{args.split}.json"
    if not split_path.is_file():
        print(f"找不到 {split_path}。先跑 build。", file=sys.stderr)
        return 1
    sides = json.loads(split_path.read_text(encoding="utf-8"))

    by_id = {item["id"]: item for item in dataset["compounds"]}
    train = [by_id[cid] for cid in sides["train"] if cid in by_id]
    test = [by_id[cid] for cid in sides["test"] if cid in by_id]
    if not test:
        print("测试集为空，没什么可跑的", file=sys.stderr)
        return 1

    arms = args.arm or ["all"]
    if "all" in arms:
        arms = list(ALL_ARMS)
    unknown = [arm for arm in arms if arm not in ARM_LABELS]
    if unknown:
        print(f"未知的臂：{'、'.join(unknown)}；可选 {'、'.join(ARM_LABELS)}", file=sys.stderr)
        return 2

    print(f"\n=== {target} · {dataset['target']['name']} · {args.split} ===")
    print(f"训练 {len(train)} / 测试 {len(test)}（切分来自 {split_path.name}）\n")

    results: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}
    seeds = list(range(args.seed, args.seed + args.repeats))
    context = {
        "out_dir": path.parent,
        "device": args.device,
        "timeout": args.b3_timeout,
        "fit_tag": args.fit_tag,
    }
    for arm in arms:
        started = time.monotonic()
        try:
            result = run_arm(arm, train, test, seeds=seeds, k=args.k, cache=cache, context=context)
        except RuntimeError as error:
            print(f"{arm} 跳过：{error}", file=sys.stderr)
            continue
        elapsed = time.monotonic() - started
        result["split"] = args.split
        result["target"] = target
        result["seconds"] = round(elapsed, 1)
        _write_json(path.parent / "runs" / f"{arm}_{args.split}.json", result)
        results.append(result)
        print(f"  {arm} 完成（{elapsed:.1f}s）：ρ={result['metrics']['spearman']}")

    if not results:
        print("一条臂都没跑成", file=sys.stderr)
        return 1

    print()
    print(_render_runs(results))
    b0 = next((item for item in results if item["arm"] == "b0"), None)
    if b0:
        print(f"\nB0 参照：{b0['note']['repeats']} 次随机的 ρ 均值 "
              f"{b0['note']['mean_rho']:+.3f}，标准差 {b0['note']['sd_rho']:.3f}")
    for item in results:
        if item["arm"] in MODEL_ARMS and item["note"].get("seed_sd") is not None:
            print(f"{item['arm']} 种子间：均值 {item['note']['seed_mean']:+.3f}，"
                  f"标准差 {item['note']['seed_sd']:.3f}"
                  f"（{len(item['note']['seeds'])} 个种子，主指标用的是集成打分）")

    n1 = next((item for item in results if item["arm"] == "n1"), None)
    if n1:
        rho = n1["metrics"]["spearman"]
        low, high = n1["metrics"]["spearman_ci"]
        # 判据是「置信区间是否包含 0」，不是固定带宽。固定 ±0.15 在小样本上是错的：
        # n=15 时纯噪声的 ρ 有近六成概率越界（实测 STING 的阴性对照就是 +0.20，
        # 而它是打乱标签训出来的）。
        if rho is None or low is None:
            print("\n阴性对照的 ρ 算不出来（打分没有方差）——先查管道")
        elif low <= 0.0 <= high:
            print(f"\n阴性对照 ρ = {rho:+.3f}，95% CI [{low:+.2f}, {high:+.2f}] 含 0，管道干净")
        else:
            print(f"\n⚠ 阴性对照 ρ = {rho:+.3f}，95% CI [{low:+.2f}, {high:+.2f}] 不含 0"
                  "——切分或标签对齐有泄漏，本次结果作废")
    print(f"\n写出：{path.parent}/runs/")
    return 0


# ---------------------------------------------------------------------------
# report：配对 bootstrap
# ---------------------------------------------------------------------------
DEFAULT_BASELINE = "b2"
REPORT_MIN_TARGETS = 2
"""方案 §9：一条规则要在 ≥2 个靶点上成立才执行。一个靶点上显著是 p-hacking 的温床。"""

RULE_TABLE = (
    ("fit_surrogate（B2）", "b2", "b1", 0.10,
     "Δρ(B2 − B1) ≥ 0.10 且 CI 下界 > 0"),
    ("head-only 微调（T1）", "t1", "b3", 0.10,
     "Δρ(T1 − B3) ≥ 0.10 且 CI 下界 > 0"),
    ("全量微调（T2，仅 H 档）", "t2", "t1", 0.0,
     "Δρ(T2 − T1) 的 CI 下界 > 0"),
    ("停止整条微调线", "b2", "b3", 0.0,
     "B2 已经在 ≥2 个靶点上打赢 B3"),
)


def discover_runs(
    root: Path, split: str, targets: Sequence[str] | None
) -> dict[str, dict[str, dict[str, Any]]]:
    """扫 `root/*/runs/*_<split>.json`，按 UniProt 归组。

    目录名可能被 `--label` 改过，所以归属认的是文件里的 `target` 字段，不是路径。
    """
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(root.glob(f"*/runs/*_{split}.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        target = str(payload.get("target") or "").upper()
        if not target or (targets and target not in targets):
            continue
        payload["_path"] = str(path)
        found.setdefault(target, {})[str(payload.get("arm"))] = payload
    return found


def _aligned(
    first: dict[str, Any], second: dict[str, Any]
) -> tuple[list[float], list[float], list[float]] | None:
    """把两条臂按化合物 id 对齐，返回 (first 分, second 分, 真值)。

    按 id 对齐而不是按下标：两条臂的 `test_kept` 顺序本应相同，但一旦哪天不同，
    按下标就会静默地把 A 的预测配到 B 的分子上，算出一个看着很正常的分。
    """
    left = {item["id"]: item for item in first.get("predictions") or []}
    right = {item["id"]: item for item in second.get("predictions") or []}
    shared = sorted(set(left) & set(right))
    if len(shared) < 4:
        return None
    return (
        [left[item]["score"] for item in shared],
        [right[item]["score"] for item in shared],
        [left[item]["pchembl"] for item in shared],
    )


def paired_bootstrap(
    stat,
    treat: Sequence[float],
    base: Sequence[float],
    truth: Sequence[float],
    *,
    seed: int,
) -> dict[str, Any] | None:
    """同一批重采样上同时算两条臂的统计量，再相减。

    **配对是这里唯一正确的做法**：两条臂看的是同一批化合物，Δ 的方差远小于两个
    独立 CI 各自的方差。用「两个 95% CI 有没有重叠」来判断差异，在小样本上会系统性
    地把真差异判成不显著。
    """
    observed_t = stat(treat, truth)
    observed_b = stat(base, truth)
    if observed_t is None or observed_b is None:
        return None
    generator = random.Random(seed)
    deltas: list[float] = []
    for _ in range(BOOTSTRAP_ROUNDS):
        picks = [generator.randrange(len(truth)) for _ in truth]
        sample_truth = [truth[index] for index in picks]
        one = stat([treat[index] for index in picks], sample_truth)
        two = stat([base[index] for index in picks], sample_truth)
        if one is None or two is None:
            continue
        deltas.append(one - two)
    if not deltas:
        return None
    deltas.sort()

    def percentile(q: float) -> float:
        position = q * (len(deltas) - 1)
        low = int(math.floor(position))
        high = min(low + 1, len(deltas) - 1)
        weight = position - low
        return deltas[low] * (1 - weight) + deltas[high] * weight

    return {
        "delta": round(observed_t - observed_b, 4),
        "ci": [round(percentile(0.025), 4), round(percentile(0.975), 4)],
        # 双尾 p：两侧各取一半，取较小的那侧乘 2。只报「≤0 的比例」的话，
        # Δ 为负时那一列会显示 1.000，读起来像「极显著反向」，其实是同一件事。
        "p_two_sided": round(
            min(1.0, 2 * min(
                sum(1 for value in deltas if value <= 0) / len(deltas),
                sum(1 for value in deltas if value >= 0) / len(deltas),
            )), 4),
        "rounds": len(deltas),
    }


def _rho_stat(scores: Sequence[float], truth: Sequence[float]) -> float | None:
    return spearman(scores, truth)


def _rmse_stat(scores: Sequence[float], truth: Sequence[float]) -> float | None:
    return rmse_r2(scores, truth)[0]


def _judge(result: dict[str, Any] | None) -> str:
    if result is None:
        return "无法比较"
    low, high = result["ci"]
    if low > 0:
        return "优于"
    if high < 0:
        return "差于"
    return "无法区分"


def _render_target(
    target: str, arms: dict[str, dict[str, Any]], *, baseline: str, seed: int
) -> tuple[list[str], dict[str, Any]]:
    """一个靶点的表：每行一条臂，最后两列是相对基线（默认 b2）的配对 Δ。"""
    base = arms.get(baseline)
    lines = [f"### {target}（{len(arms)} 条臂）", ""]
    header = (
        f"| 臂 | ρ | ρ 的 95% CI | Δρ vs {baseline} | Δ 的 95% CI | p（双尾） | 读法 |"
        if base
        else "| 臂 | ρ | ρ 的 95% CI |"
    )
    lines.append(header)
    lines.append("|---|---|---|---|---|---|---|" if base else "|---|---|---|")

    summary: dict[str, Any] = {"n_test": None, "pairs": {}}
    for arm in ("b0", "b1", "b1k", "b2", "b3", "b4", "t1", "t2", "n1"):
        if arm not in arms:
            continue
        metrics = arms[arm]["metrics"]
        summary["n_test"] = metrics["n_test"]
        rho = metrics["spearman"]
        low, high = metrics["spearman_ci"]
        cell_rho = "—" if rho is None else f"{rho:+.3f}"
        cell_ci = "—" if low is None else f"[{low:+.2f}, {high:+.2f}]"
        if base is None:
            lines.append(f"| {arm} | {cell_rho} | {cell_ci} |")
            continue
        if arm == baseline:
            lines.append(f"| {arm} | {cell_rho} | {cell_ci} | — | — | — | 基线 |")
            continue
        aligned = _aligned(arms[arm], base)
        if aligned is None:
            lines.append(f"| {arm} | {cell_rho} | {cell_ci} | — | — | — | 无法比较 |")
            continue
        treat, base_scores, truth = aligned
        result = paired_bootstrap(_rho_stat, treat, base_scores, truth, seed=seed)
        summary["pairs"][arm] = result
        if result is None:
            lines.append(f"| {arm} | {cell_rho} | {cell_ci} | — | — | — | 无法比较 |")
            continue
        lines.append(
            f"| {arm} | {cell_rho} | {cell_ci} | {result['delta']:+.3f} | "
            f"[{result['ci'][0]:+.2f}, {result['ci'][1]:+.2f}] | "
            f"{result['p_two_sided']:.3f} | {_judge(result)} |"
        )
    return lines, summary


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.root)
    targets = [item.strip().upper() for item in args.target if item.strip()]
    runs = discover_runs(root, args.split, targets or None)
    if not runs:
        print(f"{root} 下没有 {args.split} 的结果。先跑 run。", file=sys.stderr)
        return 1

    baseline = args.baseline
    sections: list[str] = [f"## 切分 {args.split} · 基线 {baseline}\n"]
    per_target: dict[str, dict[str, Any]] = {}
    for target in sorted(runs, key=lambda name: -(next(iter(runs[name].values()))["metrics"]["n_test"] or 0)):
        lines, summary = _render_target(target, runs[target], baseline=baseline, seed=args.seed)
        sections.extend(lines)
        sections.append("")
        per_target[target] = summary
        if summary["n_test"] is not None and summary["n_test"] < MIN_TEST_COMPOUNDS:
            sections.append(
                f"- ⚠ 测试集只有 {summary['n_test']} 个"
                f"（门槛 {MIN_TEST_COMPOUNDS}），这个靶点上的任何 Δ 都不该单独下结论"
            )
            sections.append("")

    # 跨靶点：同一条对比在几个靶点上成立
    pairs = sorted({pair for summary in per_target.values() for pair in summary["pairs"]})
    if pairs:
        sections.append("### 跨靶点")
        sections.append("")
        sections.append(f"| 对比 | 优于 {baseline} 的靶点 | 明细 |")
        sections.append("|---|---|---|")
        for arm in pairs:
            wins = [
                target
                for target, summary in per_target.items()
                if summary["pairs"].get(arm) and _judge(summary["pairs"][arm]) == "优于"
            ]
            detail = "、".join(
                f"{target} {summary['pairs'][arm]['delta']:+.2f}"
                for target, summary in per_target.items()
                if summary["pairs"].get(arm)
            )
            sections.append(f"| {arm} vs {baseline} | {len(wins)} | {detail or '—'} |")
        sections.append("")

    # 判定规则（§9），只在两条臂都存在时才评
    sections.append("### 判定（方案 §9，需 ≥2 个靶点）")
    sections.append("")
    sections.append("| 决策 | 条件 | 成立的靶点 | 结论 |")
    sections.append("|---|---|---|---|")
    for name, treat, base, floor, condition in RULE_TABLE:
        hits = []
        for target, summary in per_target.items():
            arms = runs[target]
            if treat not in arms or base not in arms:
                continue
            aligned = _aligned(arms[treat], arms[base])
            if aligned is None:
                continue
            one, two, truth = aligned
            result = paired_bootstrap(_rho_stat, one, two, truth, seed=args.seed)
            if result and result["ci"][0] > 0 and result["delta"] >= floor:
                hits.append(f"{target} {result['delta']:+.2f}")
        ready = len(hits) >= REPORT_MIN_TARGETS
        sections.append(
            f"| {name} | {condition} | {'、'.join(hits) if hits else '—'} | "
            f"{'**成立**' if ready else ('数据不足' if not hits else '仅 1 个靶点')} |"
        )
    sections.append("")

    text = "\n".join(sections).rstrip() + "\n"
    print(text)
    _write_json(
        root / f"report_{args.split}.json",
        {
            "generated_at": _stamp(),
            "split": args.split,
            "baseline": baseline,
            "targets": per_target,
        },
    )
    print(f"写出：{root / f'report_{args.split}.json'}")
    return 0


# ---------------------------------------------------------------------------
# pocket：定受体结构与口袋
# ---------------------------------------------------------------------------
POCKET_RADIUS = 10.0
"""与训练时一致：残基任一原子落在配体 10 Å 内就整条保留。改它就不是同一个分布了。"""

MIN_LIGAND_HEAVY = 12
"""重原子少于这个数的杂原子组不当配体看。糖、缓冲液、单原子离子都在这一刀下面。"""

CHAIN_CONTACT = 5.0
"""配体 5 Å 内的链算「接触链」。同源二聚体只留其中一条，否则口袋有两份表面。"""

SITE_OVERLAP = 0.30
"""两个配体的接触残基 Jaccard 重合度超过这个值就算同一个位点。

用残基重合度而不是质心距离：同一个活性位点里两个大小相近的配体，质心能差 5–8 Å，
按距离聚会把它们拆开（实测 PTP1B：21 个候选聚出 20 个「位点」）。
"""

SAMPLE_ENTRIES = 20
"""每个靶点抽多少个条目来看。全下（几百个）没必要：同一个位点会被反复命中。"""

TRAIN_POCKET_P25 = 540
TRAIN_POCKET_MEDIAN = 630
TRAIN_POCKET_P75 = 722
"""训练集 15700 个口袋的原子数：p25 540 / 中位 630 / p75 722，全距 20–1554。

落在四分位区间外**不等于不可用**——训练集里就有 1554 个原子的口袋。它只是说明
这个靶点的位点比典型的大/小，读分数时要记得这一点。真正要警觉的是相差一倍以上。
"""


@dataclass(frozen=True)
class _Site:
    """一个条目里的一组「像配体的东西」以及它所在的位点。"""

    pdb_id: str
    resname: str
    chains: tuple[str, ...]
    centroid: tuple[float, float, float]
    coords: tuple[tuple[float, float, float], ...]
    residues: frozenset[str]
    heavy: int
    resolution: float | None


def _resolution_angstrom(text: str) -> float | None:
    """从 `REMARK   2 RESOLUTION.` 取分辨率。拿不到就是 None，不猜。"""
    for line in text.splitlines():
        if not line.startswith("REMARK   2 RESOLUTION."):
            continue
        for token in line.split():
            try:
                value = float(token)
            except ValueError:
                continue
            if 0.5 <= value <= 20:
                return value
    return None


def _prepare_pocket(
    text: str, site: "_Site", radius: float
) -> tuple["pdbtext.PdbDocument", str, int, int]:
    """从一个条目文本走到「受体 + 口袋」。选代表与最终写盘走同一条路径。

    返回 (受体文档, 口袋文本, 残基数, 原子数)。选代表时要比较不同条目的口袋大小，
    如果那一步另写一份，最终写出来的口袋和比较时看到的就不是同一个。
    """
    doc = pdbtext.parse(text)
    protein_only, _groups = pdbtext.strip_hetero(doc)
    receptor, _removed = pdbtext.keep_chains(protein_only, site.chains)
    pocket_text, residues, atoms = _cut_pocket(receptor, site.coords, radius)
    return receptor, pocket_text, residues, atoms


def _cut_pocket(doc, ligand, radius: float) -> tuple[str, int, int]:
    """按训练规则截口袋。**实现在 `domain/finetune.cut_pocket`**，这里只做转发。

    实验脚本与产品工具必须用同一份截法：一个是「以配体任意原子为基准」，另一个如果
    写成「以质心为球心」，两边挖出来的口袋原子数能差一倍（217 vs 602），
    而训练分布的中位数是 630——那 B3 的分数就没有解释价值。
    """
    return finetune.cut_pocket(doc, ligand, radius)


def _prepare_pocket(
    text: str, site: "_Site", radius: float
) -> tuple["pdbtext.PdbDocument", str, int, int]:
    """从一个条目文本走到「受体 + 口袋」。选代表与最终写盘走同一条路径。

    返回 (受体文档, 口袋文本, 残基数, 原子数)。选代表时要比较不同条目的口袋大小，
    如果那一步另写一份，最终写出来的口袋和比较时看到的就不是同一个。
    """
    doc = pdbtext.parse(text)
    protein_only, _groups = pdbtext.strip_hetero(doc)
    receptor, _removed = pdbtext.keep_chains(protein_only, site.chains)
    pocket_text, residues, atoms = _cut_pocket(receptor, site.coords, radius)
    return receptor, pocket_text, residues, atoms


def _cut_pocket(
    doc: "pdbtext.PdbDocument",
    ligand: Sequence[Any],
    radius: float,
) -> tuple[str, int, int]:
    """按训练时的规则截口袋：**残基的任一原子落在配体 radius 内，就整条保留**。

    返回 (口袋文本, 残基数, 原子数)。

    为什么不用 `models/DTA/utils/pocket.pocket_from_center`：那个函数截的是「以**一个
    中心点**为球心的球」，而训练用的是「到**配体的任意原子**的距离」。两者在细长配体上
    差得很远——实测 1YPK（42 重原子配体）用质心球 10 Å 只留下 26 个残基 / 217 个原子，
    用配体表面则留下 76 个 / 602 个。训练口袋的原子数中位数是 630（p25 540、p75 722），
    所以质心球那一份**落在训练分布之外**，模型见到的是一个比它训练时小一半的口袋。
    口径对不上，B3 的分数就没有解释价值，这比复用一行代码重要。
    """
    coords = list(ligand)
    kept: set[Any] = set()
    for atom in doc.atoms:
        if atom.is_hetero:
            continue
        for x, y, z in coords:
            if (
                abs(atom.x - x) <= radius
                and abs(atom.y - y) <= radius
                and abs(atom.z - z) <= radius
                and math.dist((atom.x, atom.y, atom.z), (x, y, z)) <= radius
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


def _ligand_sites(text: str, pdb_id: str) -> tuple[list[_Site], dict[str, int]]:
    """一个条目里所有「像配体」的杂原子组 → 位点。

    过滤用的是生产路径那一套（`hetero_groups` + `screen_ligands`）：水、离子、
    与蛋白共价的、接触残基太少的都被筛掉。**自己另写一份的话，两边对「什么算配体」
    的定义会分叉**，而这里的分叉直接决定口袋挖在哪。
    """
    doc = pdbtext.parse(text)
    groups = pdbtext.hetero_groups(doc)
    screen = preprocess.screen_ligands(doc, groups)
    dropped: dict[str, int] = {}
    for _name, reason in screen.excluded:
        dropped[reason] = dropped.get(reason, 0) + 1

    resolution = _resolution_angstrom(text)
    protein = [atom for atom in doc.atoms if not atom.is_hetero]

    sites: list[_Site] = []
    for group in screen.kept:
        heavy = [atom for atom in group.atoms if pdbtext.is_heavy(atom)]
        if len(heavy) < MIN_LIGAND_HEAVY:
            dropped["重原子太少"] = dropped.get("重原子太少", 0) + 1
            continue
        residues: set[str] = set()
        for other in protein:
            for atom in heavy:
                if (
                    abs(atom.x - other.x) <= CHAIN_CONTACT
                    and abs(atom.y - other.y) <= CHAIN_CONTACT
                    and abs(atom.z - other.z) <= CHAIN_CONTACT
                    and math.dist((atom.x, atom.y, atom.z), (other.x, other.y, other.z))
                    <= CHAIN_CONTACT
                ):
                    residues.add(f"{other.chain}:{other.number}{other.icode}")
                    break
        chains = sorted({key.split(":", 1)[0] for key in residues})
        centroid = (
            sum(atom.x for atom in heavy) / len(heavy),
            sum(atom.y for atom in heavy) / len(heavy),
            sum(atom.z for atom in heavy) / len(heavy),
        )
        sites.append(
            _Site(
                pdb_id=pdb_id,
                resname=group.resname,
                chains=tuple(chains) or ("",),
                centroid=centroid,
                coords=tuple(atom.coord for atom in heavy),
                residues=frozenset(residues),
                heavy=len(heavy),
                resolution=resolution,
            )
        )
    return sites, dropped


def _cluster_sites(sites: Sequence[_Site]) -> list[list[_Site]]:
    """单链接聚类：**接触残基的重合度**（Jaccard）≥ 0.30 就算同一个位点。

    跨条目投票是这个步骤的全部意义：同一个位点会在几十个结构里重复出现，
    而结晶添加剂、别构位点、对称相关的那一份通常只出现一两次。

    **不能用质心距离聚。** 同一个活性位点里两个分子量相当的配体，质心可以差 5–8 Å
    （一个贴着 P-loop，一个伸向溶剂），按 4 Å 距离聚会把它们拆成两个位点——实测
    PTP1B 就是这样：21 个候选聚出 20 个「位点」，簇大小 2 看着像毫无共识，而实际上
    它们大多在同一个催化口袋里。残基重合度衡量的是「围着同一批残基」，这才是
    「同一个位点」的定义。
    """
    parent = list(range(len(sites)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(sites)):
        for j in range(i + 1, len(sites)):
            left, right = sites[i].residues, sites[j].residues
            if not left or not right:
                continue
            shared = len(left & right) / len(left | right)
            if shared >= SITE_OVERLAP:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    buckets: dict[int, list[_Site]] = {}
    for index, site in enumerate(sites):
        buckets.setdefault(find(index), []).append(site)
    return sorted(buckets.values(), key=lambda group: -len(group))


def _site_score(site: _Site) -> tuple[float, int]:
    """同一簇里挑代表：分辨率优先（没有分辨率排最后），再看重原子数。"""
    resolution = site.resolution if site.resolution is not None else 99.0
    return (resolution, -site.heavy)


def cmd_pocket(args: argparse.Namespace) -> int:
    root = Path(args.root)
    targets = [item.strip().upper() for item in args.target if item.strip()]
    if not targets:
        print("要给 --target（UniProt 编号），可重复", file=sys.stderr)
        return 2

    rows: list[str] = []
    summary: dict[str, Any] = {}
    for target in targets:
        print(f"\n=== {target} ===")
        try:
            ids, _body = complexes.list_complexes(target, timeout=args.timeout)
        except Exception as error:
            print(f"  条目检索失败：{type(error).__name__}: {error}", file=sys.stderr)
            continue
        ids = sorted({item.upper() for item in ids})
        if not ids:
            print("  这个靶点没有带配体的 PDB 条目", file=sys.stderr)
            continue

        step = max(1, len(ids) // args.sample)
        sampled = ids[::step][: args.sample]
        print(f"  条目 {len(ids)} 个，抽 {len(sampled)} 个来看位点")

        sites: list[_Site] = []
        texts: dict[str, str] = {}
        dropped: dict[str, int] = {}
        failed = 0
        for pdb_id in sampled:
            try:
                text = complexes.download_entry(pdb_id, timeout=args.timeout)
            except Exception:
                failed += 1
                continue
            texts[pdb_id] = text
            found, why = _ligand_sites(text, pdb_id)
            sites.extend(found)
            for key, value in why.items():
                dropped[key] = dropped.get(key, 0) + value
        if not sites:
            print(f"  {len(sampled)} 个条目里没有可用的配体（下载失败 {failed}）", file=sys.stderr)
            continue

        clusters = _cluster_sites(sites)
        best_cluster = clusters[0]
        # 簇内挑代表：先按分辨率，但**口袋大小必须落在训练分布里**。
        # 只看分辨率会选到重原子 90 的巨分子，截出 873 个原子的口袋（超过 p75），
        # 模型见到的是训练时从未有过的大输入。所以按「离中位数最近」挑，
        # 一旦落进 p25–p75 就停。
        chosen = None
        residues = atoms = 0
        receptor_text = pocket_text = ""
        for site in sorted(best_cluster, key=_site_score)[: args.trials]:
            text = texts.get(site.pdb_id)
            if text is None:
                text = complexes.download_entry(site.pdb_id, timeout=args.timeout)
            receptor, pocket, res, count = _prepare_pocket(text, site, args.radius)
            if count == 0:
                continue
            if chosen is None or abs(count - TRAIN_POCKET_MEDIAN) < abs(
                atoms - TRAIN_POCKET_MEDIAN
            ):
                chosen, residues, atoms = site, res, count
                receptor_text, pocket_text = receptor.text, pocket
            if TRAIN_POCKET_P25 <= count <= TRAIN_POCKET_P75:
                break
        if chosen is None:
            print("  这一簇里没有一个能截出口袋", file=sys.stderr)
            continue
        print(f"  候选配体 {len(sites)} 个，聚成 {len(clusters)} 个位点；"
              f"最大一簇 {len(best_cluster)} 个 → {chosen.pdb_id} {chosen.resname}"
              f"（口袋 {atoms} 原子）")

        out_dir = root / (args.label or target) / "pocket"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "receptor.pdb").write_text(receptor_text, encoding="utf-8")
        (out_dir / "pocket.pdb").write_text(pocket_text, encoding="utf-8")

        payload = {
            "uniprot": target,
            "pdb_id": chosen.pdb_id,
            "resname": chosen.resname,
            "chains": list(chosen.chains),
            "centroid": [round(value, 3) for value in chosen.centroid],
            "radius": args.radius,
            "rule": "残基任一原子落在配体 radius 内即整条保留（与训练一致）",
            "residues": residues,
            "atoms": atoms,
            "resolution": chosen.resolution,
            "heavy_atoms": chosen.heavy,
            "cluster_size": len(best_cluster),
            "candidates": len(sites),
            "clusters": [len(group) for group in clusters],
            "sampled": len(sampled),
            "dropped": dropped,
            "download_failed": failed,
            "generated_at": _stamp(),
        }
        _write_json(out_dir / "pocket.json", payload)
        summary[target] = payload
        rows.append(
            f"| {target} | {chosen.pdb_id} {chosen.resname} | "
            f"{'—' if chosen.resolution is None else f'{chosen.resolution:.2f}'} | "
            f"{chosen.heavy} | {len(best_cluster)}/{len(sites)} | "
            f"{len(clusters)} | {residues} | {atoms} |"
        )
        if atoms < TRAIN_POCKET_P25 or atoms > TRAIN_POCKET_P75:
            print(f"  ⚠ 口袋 {atoms} 个原子，在训练集的 p25–p75"
                  f"（{TRAIN_POCKET_P25}–{TRAIN_POCKET_P75}）之外"
                  f"（中位 {TRAIN_POCKET_MEDIAN}，全距 20–1554）。"
                  "仍在支撑集内，但这个靶点的位点比典型的大/小，读分数时要记得")
        if len(best_cluster) == 1:
            print("  ⚠ 这个位点只在一个条目里出现，跨条目没有任何佐证——"
                  "可能是别构位点或结晶接触")

    if rows:
        print("\n| 靶点 | 受体 | 分辨率 | 重原子 | 主簇/候选 | 位点数 | 口袋残基 | 口袋原子 |")
        print("|---|---|---|---|---|---|---|---|")
        for row in rows:
            print(row)
        _write_json(root / "pockets.json", {"generated_at": _stamp(), "targets": summary})
    return 0


# ---------------------------------------------------------------------------
# fit：单靶点微调
# ---------------------------------------------------------------------------
FIT_DIRNAME = "fit"
FIT_SCRIPT = "tools/fit_target.py"
"""在 `models/DTA/.venv` 里跑的训练脚本。主仓不 import torch，只能起子进程。"""


def fit_tag(*, head_only: bool, epochs: int, lr: float, seed: int, limit: int | None) -> str:
    """一次微调的身份。**参数即目录名**：同一组参数重跑会复用，换了参数就是另一份。"""
    tag = f"{'head' if head_only else 'full'}-e{epochs}-lr{lr:g}-s{seed}"
    return tag if limit is None else f"{tag}-n{limit}"


def _write_index(path: Path, records: Sequence[dict[str, Any]]) -> None:
    """写 PDBbind INDEX 格式。

    `read_affinity` 只认三件事：第一列是 id、第四列是 pK、行里要出现 `Ki=`/`Kd=`/`IC50=`
    这样的标注。第三列的年份用 2020 占位（本实验不按年份切）。
    """
    lines = ["# 单靶点微调数据集（tools/retro_affinity.py fit 生成）"]
    for record in records:
        pk = float(record["pchembl"])
        value = 10 ** (-pk) * 1e9
        lines.append(
            f"{record['id']}  2.00  2020  {pk:.3f}  Ki={value:.1f}nM      "
            f"// {record['id']}.pdf (LIG)"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_fit(args: argparse.Namespace) -> int:
    target = (args.target or "").strip().upper()
    if not target:
        print("要给一个 --target（UniProt 编号）", file=sys.stderr)
        return 2

    try:
        dataset, path = load_dataset(target, Path(args.root), args.label)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    out_dir = path.parent
    split_path = out_dir / "splits" / f"{args.split}.json"
    if not split_path.is_file():
        print(f"找不到 {split_path}。先跑 build。", file=sys.stderr)
        return 1
    sides = json.loads(split_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in dataset["compounds"]}

    train_ids = list(sides["train"])
    if args.limit and args.limit < len(train_ids):
        rng = random.Random(args.seed)
        train_ids = rng.sample(train_ids, args.limit)
    val_ids = list(sides["val"])[: max(1, len(train_ids) // 5)]
    records = [by_id[cid] for cid in train_ids + val_ids if cid in by_id]

    tag = fit_tag(
        head_only=args.head_only, epochs=args.epochs, lr=args.lr,
        seed=args.seed, limit=args.limit,
    )
    work = out_dir / FIT_DIRNAME / tag
    work.mkdir(parents=True, exist_ok=True)
    print(f"\n=== {target} · 微调 {tag} ===")
    print(f"训练 {len(train_ids)} / 验证 {len(val_ids)}（{args.split} 切分）")

    conformers, failed = ensure_conformers(records, work / "conformers", seed=EMBED_SEED)
    if failed:
        print(f"  {len(failed)} 个分子构象生成失败，已从训练集剔除")

    data_dir = work / "data"
    pocket = out_dir / "pocket" / "pocket.pdb"
    if not pocket.is_file():
        print(f"没有口袋 {pocket}。先跑 pocket 子命令。", file=sys.stderr)
        return 1
    used: list[dict[str, Any]] = []
    for record in records:
        cid = record["id"]
        if cid not in conformers:
            continue
        directory = data_dir / cid
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(conformers[cid], directory / f"{cid}_ligand.sdf")
        shutil.copyfile(pocket, directory / f"{cid}_pocket.pdb")
        used.append(record)

    index_path = work / "INDEX"
    _write_index(index_path, used)
    _write_json(
        work / "split.json",
        {
            "train": [cid for cid in train_ids if cid in conformers],
            "val": [cid for cid in val_ids if cid in conformers],
            "test": [],
        },
    )

    try:
        python = affinity.dta_python()
    except Exception as error:
        print(f"找不到 DTA 解释器：{error}", file=sys.stderr)
        return 1

    spec = {
        "target": target,
        "data_dir": str(data_dir),
        "index": str(index_path),
        "split": str(work / "split.json"),
        "pocket": str(pocket),
        "out_dir": str(work / "out"),
        "base_checkpoint": str(affinity.dta_root() / "out" / "model"
                               / "best_model_08-25-14-54-57_rmse.pth"),
        "smiles": {record["id"]: record["smiles"] for record in used},
        "epochs": args.epochs,
        "lr": args.lr,
        "batch": args.batch,
        "seed": args.seed,
        "head_only": args.head_only,
        "device": args.device,
    }
    spec_path = work / "spec.json"
    _write_json(spec_path, spec)
    if not Path(spec["base_checkpoint"]).is_file():
        print(f"基座 checkpoint 不在：{spec['base_checkpoint']}", file=sys.stderr)
        return 1

    print(f"  起训练子进程（{python}）…")
    started = time.monotonic()
    completed = subprocess.run(
        [python, str(PROJECT_ROOT / FIT_SCRIPT), str(spec_path)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - started
    tail = (completed.stdout or "").strip().splitlines()
    for line in tail[-6:]:
        print("  " + line)
    if completed.returncode != 0:
        print((completed.stderr or "")[-2000:], file=sys.stderr)
        print(f"训练失败（{elapsed:.0f}s）", file=sys.stderr)
        return 1

    result = None
    for line in tail:
        if line.startswith("FIT_RESULT "):
            result = json.loads(line[len("FIT_RESULT "):])
    if result is None:
        print("训练跑完了但没拿到 FIT_RESULT，检查 " + str(work / "out"), file=sys.stderr)
        return 1

    print(f"\n完成（{elapsed:.0f}s）：验证 RMSE {result['best_val_rmse']}，"
          f"checkpoint {result['checkpoint']}")
    print(f"下一步：python tools/retro_affinity.py run --target {target} --arm t1 --fit-tag {tag}")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def cmd_select(args: argparse.Namespace) -> int:
    targets = [item.strip().upper() for item in args.target if item.strip()]
    if not targets:
        print("至少要给一个 --target（UniProt 编号）", file=sys.stderr)
        return 2

    audits: list[dict[str, Any]] = []
    for accession in targets:
        print(f"\n=== {accession} ===")
        try:
            audit = audit_target(accession, args)
        except Exception as error:  # 一个靶点取数失败不该让其余靶点白跑
            print(f"  失败：{type(error).__name__}: {error}", file=sys.stderr)
            continue
        out = Path(args.out) / f"{audit['uniprot']}.json"
        _write_json(out, audit)
        print(f"  写出 {out}")
        audits.append(audit)

    if not audits:
        print("没有一个靶点审计成功", file=sys.stderr)
        return 1

    _write_json(Path(args.out) / "index.json",
                {"generated_at": _stamp(), "targets": [a["uniprot"] for a in audits]})
    print()
    print(render(audits))

    ready = [a for a in audits if a["verdict"]["h3"]]
    print(f"能回答 H3 的靶点：{len(ready)} 个"
          + ("" if ready else "——一个都没有，先换靶点，别急着写 build"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retro_affinity",
        description="回顾性实验：select 审计 → build 切分 → run 跑臂 → report 判定。方案见 docs/retro-affinity-plan.md",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    select = sub.add_parser("select", help="审计靶点的活性数据够不够做实验")
    select.add_argument("--target", action="append", default=[], metavar="UNIPROT",
                        help="UniProt 编号，可重复给；三个档位各一个")
    select.add_argument("--min-pchembl", type=float, default=DEFAULT_MIN_PCHEMBL,
                        help=f"活性下限（默认 {DEFAULT_MIN_PCHEMBL:g}；6≈1μM、7≈100nM）")
    select.add_argument("--min-confidence", type=int, default=DEFAULT_MIN_CONFIDENCE,
                        help=f"测定置信度下限（默认 {DEFAULT_MIN_CONFIDENCE}；"
                             "生产工具默认 8，实验放宽到 7 是为了统计功效）")
    select.add_argument("--measure-types", default=chembl.DEFAULT_MEASURE_TYPES,
                        help=f"定量测定类型（默认 {chembl.DEFAULT_MEASURE_TYPES}）")
    select.add_argument("--assay-type", default=DEFAULT_ASSAY_TYPE,
                        help=f"只保留哪种实验体系（默认 {DEFAULT_ASSAY_TYPE}；"
                             f"{ALL_ASSAY_TYPES} 表示不过滤）")
    select.add_argument("--timeout", type=float, default=60.0, help="单次 HTTP 超时秒数")
    select.add_argument("--pdb-dir", default=None,
                        help="原始复合物目录（<id>/<id>_complex_min_ligand.sdf）；"
                             f"不给出时依次找环境变量 {PDB_DIR_ENV} 与 models/DTA/data/pdb")
    select.add_argument("--refresh", action="store_true", help="忽略本地缓存重新下载")
    select.add_argument("--out", default=str(SELECT_DIR), help="快照写到哪（默认 .probe/retro/select）")
    select.set_defaults(func=cmd_select)

    build = sub.add_parser("build", help="由审计快照切出 s1/s2/s3 三套划分")
    build.add_argument("--target", required=True, metavar="UNIPROT",
                       help="select 用过的那个 UniProt 编号")
    build.add_argument("--label", default=None,
                       help="产物目录名（默认就用 UniProt 编号）")
    build.add_argument("--seed", type=int, default=42,
                       help="S1 随机切分的种子（默认 42）")
    build.add_argument("--min-group", type=int, default=MIN_SCAFFOLD_GROUP,
                       help=f"S2 里小于这么多分子的骨架整组进训练集（默认 {MIN_SCAFFOLD_GROUP}）")
    build.add_argument("--select-dir", default=str(SELECT_DIR),
                       help="select 的快照目录（默认 .probe/retro/select）")
    build.add_argument("--out", default=str(PROBE_DIR),
                       help="产物写到哪（默认 .probe/retro/<label>）")
    build.set_defaults(func=cmd_build)

    run = sub.add_parser("run", help="跑 B 系列基线（b0 / b1 / b1k / b2 / n1）")
    run.add_argument("--target", required=True, metavar="UNIPROT",
                     help="build 用过的那个 UniProt 编号")
    run.add_argument("--label", default=None, help="产物目录名（build 时给的那个）")
    run.add_argument("--split", default="s2", choices=("s1", "s2", "s3"),
                     help="用哪套切分（默认 s2，即骨架切分）")
    run.add_argument("--arm", action="append", default=[],
                     choices=[*ARM_LABELS, "all"],
                     help="跑哪几条臂，可重复；不给就是全部")
    run.add_argument("--k", type=int, default=DEFAULT_K,
                     help=f"b1k 的邻居数（默认 {DEFAULT_K}）")
    run.add_argument("--seed", type=int, default=42, help="随机种子起点（默认 42）")
    run.add_argument("--repeats", type=int, default=DEFAULT_SEEDS,
                     help=f"模型臂跑几个种子（默认 {DEFAULT_SEEDS}），主指标取这些打分的平均")
    run.add_argument("--fit-tag", default=None,
                     help="t1 用哪一次微调（默认取最新的一份）")
    run.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"),
                     help="B3 / T1 的推理设备（默认 auto）")
    run.add_argument("--b3-timeout", type=float, default=B3_TIMEOUT,
                     help=f"B3 子进程超时秒数（默认 {B3_TIMEOUT:g}）")
    run.add_argument("--root", default=str(PROBE_DIR),
                     help="build 的产物根（默认 .probe/retro）")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="跨靶点出表：配对 bootstrap 与 §9 判定")
    report.add_argument("--target", action="append", default=[], metavar="UNIPROT",
                        help="只看这些靶点（可重复）；不给就是全部")
    report.add_argument("--split", default="s2", choices=("s1", "s2", "s3"),
                        help="用哪套切分的结果（默认 s2）")
    report.add_argument("--baseline", default=DEFAULT_BASELINE,
                        help=f"Δ 相对的基线臂（默认 {DEFAULT_BASELINE}，因为 B3 要越过它）")
    report.add_argument("--seed", type=int, default=42, help="bootstrap 种子（默认 42）")
    report.add_argument("--root", default=str(PROBE_DIR),
                        help="run 的产物根（默认 .probe/retro）")
    report.set_defaults(func=cmd_report)

    pocket = sub.add_parser("pocket", help="定受体结构与口袋（跨条目投票找主结合位点）")
    pocket.add_argument("--target", action="append", default=[], metavar="UNIPROT",
                        help="UniProt 编号，可重复")
    pocket.add_argument("--sample", type=int, default=SAMPLE_ENTRIES,
                        help=f"每个靶点抽多少个条目（默认 {SAMPLE_ENTRIES}）")
    pocket.add_argument("--radius", type=float, default=POCKET_RADIUS,
                        help=f"口袋半径（默认 {POCKET_RADIUS:g} Å，与训练一致）")
    pocket.add_argument("--trials", type=int, default=6,
                        help="簇内最多试几个代表来找合适大小的口袋（默认 6）")
    pocket.add_argument("--label", default=None,
                        help="产物目录名（默认用 UniProt 编号）")
    pocket.add_argument("--timeout", type=float, default=60.0, help="单次 HTTP 超时秒数")
    pocket.add_argument("--root", default=str(PROBE_DIR),
                        help="产物根（默认 .probe/retro）")
    pocket.set_defaults(func=cmd_pocket)

    fit = sub.add_parser("fit", help="在单个靶点上微调 VCrossFormer")
    fit.add_argument("--target", required=True, metavar="UNIPROT",
                     help="build 用过的那个 UniProt 编号")
    fit.add_argument("--label", default=None, help="产物目录名（build 时给的那个）")
    fit.add_argument("--split", default="s2", choices=("s1", "s2", "s3"),
                     help="用哪套切分的训练集（默认 s2）")
    fit.add_argument("--limit", type=int, default=None,
                     help="只用随机 N 个训练化合物（做学习曲线的 N-shot）")
    fit.add_argument("--epochs", type=int, default=30, help="训练轮数（默认 30）")
    fit.add_argument("--lr", type=float, default=1e-5,
                     help="学习率（默认 1e-5；小样本 + 已收敛权重，大了会崩）")
    fit.add_argument("--batch", type=int, default=16, help="批大小（默认 16）")
    fit.add_argument("--seed", type=int, default=444, help="训练种子（默认 444，与 DTA 一致）")
    fit.add_argument("--full", dest="head_only", action="store_false",
                     help="全量微调（默认只训 affinity_mlp）")
    fit.add_argument("--device", default="cpu", choices=("cpu", "cuda"),
                     help="训练设备（默认 cpu）")
    fit.add_argument("--root", default=str(PROBE_DIR),
                     help="build 的产物根（默认 .probe/retro）")
    fit.set_defaults(func=cmd_fit, head_only=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # `.env` 里的 VSA_DTA_PDB_DIR 要生效；与 debug_tools 同一套读法（override=False）
    load_env()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
