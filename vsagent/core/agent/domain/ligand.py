"""配体集：从本机文件导入，以及把 SMILES 准备成可对接的 PDBQT。

本模块只认识「配体」这一侧的概念：SMILES、盐、canonical 串、重原子数、构象。
它不读受体、不调对接引擎——那些在 `receptor.py` 与 `docking.py`。

导入与准备都写「一组文件」，所以产物目录与提交标记的口径统一走 `products.py`。
配体库是跨靶点的，登记时不挂当前受体为父项（`link_current_receptor=False`），
否则换一个靶点就会把整库搁置。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    LigandSetHandle,
    SessionLedger,
    expect_handle,
    fingerprint_parts,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from ...file_log import record as log_read
from . import products
from .chemistry import CHEM_TOOL_DESCRIPTION_NOTE, require_chem

RAW_DIR = "raw"
PREPARED_DIR = "prepared"
MANIFEST_FILE = "manifest.json"
PARAMS_FILE = products.PARAMS_FILE
CSV_FILE = "ligands.csv"
PDBQT_DIR = "ligands"

CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "smiles",
    "input_smiles",
    "heavy_atoms",
    "pchembl_value",
    "source_index",
)

# 后缀 → 内部格式名。`.sd` 是 `.sdf` 的常见别名。
FORMAT_BY_SUFFIX: dict[str, str] = {
    ".sdf": "sdf",
    ".sd": "sdf",
    ".smi": "smi",
    ".csv": "csv",
}

DEFAULT_IMPORT_LIMIT = 20_000
MAX_IMPORT_LIMIT = 50_000
MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_SKIPPED_INLINE = 5

DEFAULT_PREPARE_LIMIT = 200
MAX_PREPARE_LIMIT = 2_000
# offset 是「去重后的第几条」，上限跟导入上限同量级（库最多 5 万条）。
# 不跟 limit 同量级：`limit ⊆ 2000` 说的是「一批多大」，offset 说的是「库有多深」，
# 两者绑在一起会让 1000 条之后的分子永远取不到——那正是这次要修的问题。
MAX_OFFSET = MAX_IMPORT_LIMIT
DEFAULT_SEED = 42
MAX_SEED = 2_147_483_647


class LigandError(ValueError):
    """配体解析/清洗中的可预期失败；文本会进 `manifest.skipped`，不是给用户看的堆栈。"""


class EmbedError(LigandError):
    """3D 构象生成失败；与 Meeko 失败分开计数，方便判断该换 seed 还是换分子。"""


class MeekoError(LigandError):
    """Meeko 参数化失败（没有 setup、或 PDBQT 没写出来）。"""


@dataclass(frozen=True, slots=True)
class RawLigand:
    """文件里读出来的一行，还没做化学清洗。"""

    index: int
    id: str
    name: str
    smiles: str
    source_ref: str
    pchembl_value: float | None = None
    problem: str = ""  # 读取阶段就知道坏掉的记录（如 SDF 解析失败）


@dataclass(frozen=True, slots=True)
class Cleaned:
    """去盐、canonical 化之后的一条。"""

    input_smiles: str
    smiles: str
    heavy_atoms: int
    fragments_removed: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PrepItem:
    """准备阶段的一条：化学清洗结果 + 它在源里的身份。"""

    id: str
    name: str
    source_index: int
    cleaned: Cleaned


@dataclass(frozen=True, slots=True)
class PreparedMolecule:
    """Meeko 参数化之后的一条。"""

    pdbqt: str
    heavy_atoms: int
    forcefield: str
    setups: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoadedLigands:
    records: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]
    counts: dict[str, int]
    format: str
    sha256: str
    size: int
    path: str


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
def ligands_dir() -> Path:
    """配体产物的根。测试替换它，避免写用户家目录。"""
    return Path.home() / ".opencode_tui" / "ligands"


def raw_ligandset_dir() -> Path:
    return ligands_dir() / RAW_DIR


def prepared_ligandset_dir() -> Path:
    return ligands_dir() / PREPARED_DIR


# ---------------------------------------------------------------------------
# 化学清洗
# ---------------------------------------------------------------------------
def canonicalize_smiles(raw: str) -> Cleaned:
    """SMILES → 去盐后的单分子 + canonical isomeric SMILES。

    只留最大片段（按重原子数，重原子数相同再按精确分子量）；盐、溶剂、抗衡离子
    都在这一步丢掉。重原子少于 3 的片段不当配体——它对接不出有意义的结果。
    """
    from rdkit import Chem, rdBase
    from rdkit.Chem import Descriptors

    text = str(raw).strip()
    if not text:
        raise LigandError("SMILES 为空")

    # RDKit 默认把解析失败打到 stderr。TUI 里那会直接糊在画面上，而这条失败
    # 本来就要作为 `skipped` 回灌给模型——日志只留一份，不要漏到终端。
    with rdBase.BlockLogs():
        mol = Chem.MolFromSmiles(text)
        if mol is None:
            raise LigandError("SMILES 解析失败")

        try:
            fragments = list(Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True))
        except Exception as error:  # RDKit 的异常类型不稳定，统一转成可回灌的文本
            raise LigandError(f"拆片段失败（{type(error).__name__}：{error}）") from error

        removed = 0
        warnings: list[str] = []
        if len(fragments) > 1:
            def key(item: Any) -> tuple[int, float]:
                heavy = int(item.GetNumHeavyAtoms())
                try:
                    mass = float(Descriptors.ExactMolWt(item))
                except Exception:
                    mass = 0.0
                return heavy, mass

            main = max(fragments, key=key)
            removed = len(fragments) - 1
            warnings.append(f"去掉了 {removed} 个片段（盐/溶剂）")
        else:
            main = mol

        heavy_atoms = int(main.GetNumHeavyAtoms())
        if heavy_atoms < 3:
            raise LigandError(f"重原子数只有 {heavy_atoms}，不像可对接的配体")

        try:
            smiles = Chem.MolToSmiles(main, isomericSmiles=True, canonical=True)
        except Exception as error:
            raise LigandError(
                f"canonical SMILES 生成失败（{type(error).__name__}：{error}）"
            ) from error
        if not smiles:
            raise LigandError("canonical SMILES 为空")

    return Cleaned(
        input_smiles=text,
        smiles=smiles,
        heavy_atoms=heavy_atoms,
        fragments_removed=removed,
        warnings=tuple(warnings),
    )


def prepare_molecule(cleaned: Cleaned, *, seed: int) -> PreparedMolecule:
    """SMILES → ETKDGv3 3D 构象 → MMFF94（不可用回退 UFF）→ Meeko PDBQT。

    `seed=0` 表示随机（转成 RDKit 的 `-1`）；其它值固定，保证同一组参数可复现。
    嵌入失败会再用 `useRandomCoords` 试一次——有些大环/柔性分子第一次嵌不进去。
    """
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    from rdkit import Chem, rdBase
    from rdkit.Chem import AllChem

    with rdBase.BlockLogs():
        mol = Chem.MolFromSmiles(cleaned.smiles)
        if mol is None:
            raise LigandError("SMILES 解析失败")
        try:
            mol = Chem.AddHs(mol)
        except Exception as error:
            raise LigandError(f"加显式氢失败（{type(error).__name__}：{error}）") from error

        random_seed = -1 if seed == 0 else int(seed)
        params = AllChem.ETKDGv3()
        params.randomSeed = random_seed
        params.clearConfs = True
        if AllChem.EmbedMolecule(mol, params) == -1:
            params.useRandomCoords = True
            params.randomSeed = -1 if random_seed == -1 else random_seed + 1
            if AllChem.EmbedMolecule(mol, params) == -1:
                raise EmbedError("3D 构象生成失败（ETKDGv3 两次都没嵌进去）")

        forcefield = "none"
        try:
            if AllChem.MMFFHasAllMoleculeParams(mol):
                if AllChem.MMFFOptimizeMolecule(mol, maxIters=200) >= 0:
                    forcefield = "MMFF94"
            if forcefield == "none":
                if AllChem.UFFOptimizeMolecule(mol, maxIters=200) >= 0:
                    forcefield = "UFF"
        except Exception:
            # 力场优化失败不阻断：ETKDG 的坐标已经可用，只是没走到局部极小。
            forcefield = "none"

        try:
            setups = MoleculePreparation().prepare(mol)
        except Exception as error:
            raise MeekoError(f"Meeko 准备失败（{type(error).__name__}：{error}）") from error
        if not setups:
            raise MeekoError("Meeko 没有生成任何 setup")
        try:
            pdbqt, ok, error = PDBQTWriterLegacy.write_string(setups[0])
        except Exception as raised:
            raise MeekoError(
                f"Meeko 写 PDBQT 失败（{type(raised).__name__}：{raised}）"
            ) from raised
        if not ok or not pdbqt.strip():
            raise MeekoError(error or "Meeko 没有写出 PDBQT 内容")

    warnings = list(cleaned.warnings)
    if len(setups) > 1:
        warnings.append(f"Meeko 生成了 {len(setups)} 个 setup，只用了第一个")
    return PreparedMolecule(
        pdbqt=pdbqt,
        heavy_atoms=cleaned.heavy_atoms,
        forcefield=forcefield,
        setups=len(setups),
        warnings=tuple(warnings),
    )


def _clean_records(
    raw_records: Sequence[RawLigand], *, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """逐条清洗、按 canonical SMILES 去重，再按 `limit` 截断。

    截断放在去重之后：重复分子不该占名额。`accepted` 是**写进 CSV 的条数**，
    因此 `input == accepted + invalid + duplicate + truncated`。
    """
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    invalid = 0
    duplicate = 0

    for item in raw_records:
        if item.problem:
            invalid += 1
            skipped.append(_skip(item, "sdf_parse_failed", item.problem))
            continue
        try:
            cleaned = canonicalize_smiles(item.smiles)
        except LigandError as error:
            invalid += 1
            skipped.append(_skip(item, "invalid_smiles", str(error)))
            continue

        if cleaned.smiles in seen:
            duplicate += 1
            skipped.append(
                _skip(
                    item,
                    "duplicate",
                    f"与 {seen[cleaned.smiles]} 的 canonical SMILES 相同",
                )
            )
            continue

        seen[cleaned.smiles] = item.id
        records.append(
            {
                "id": item.id,
                "name": item.name,
                "smiles": cleaned.smiles,
                "input_smiles": cleaned.input_smiles,
                "heavy_atoms": cleaned.heavy_atoms,
                "pchembl_value": item.pchembl_value,
                "source_index": item.index,
                "fragments_removed": cleaned.fragments_removed,
                "warnings": list(cleaned.warnings),
            }
        )

    total_valid = len(records)
    kept = records[:limit]
    remaining = total_valid - len(kept)
    return kept, skipped, {
        "input": len(raw_records),
        "accepted": len(kept),
        "invalid": invalid,
        "duplicate": duplicate,
        "truncated": remaining,
        "remaining": remaining,
    }


def _skip(item: RawLigand, reason: str, detail: str) -> dict[str, Any]:
    return {
        "index": item.index,
        "id": item.id,
        "raw": item.smiles[:120],
        "source_ref": item.source_ref,
        "reason": reason,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 文件读取
# ---------------------------------------------------------------------------
def load_ligand_file(
    path: Path,
    *,
    smiles_column: str = "",
    limit: int = DEFAULT_IMPORT_LIMIT,
) -> LoadedLigands:
    """读一份配体文件并清洗。失败抛 `LigandError`，调用方转成 `failed=True`。"""
    source = Path(path).expanduser()
    if not source.is_file():
        log_read(source, purpose="import.ligands", ok=False, detail="文件不存在或不是普通文件")
        raise LigandError(f"找不到这个文件，或它不是普通文件：{source}")

    suffix = source.suffix.lower()
    if suffix not in FORMAT_BY_SUFFIX:
        log_read(source, purpose="import.ligands", ok=False, detail=f"不支持的格式：{suffix}")
        raise LigandError(
            f"不支持的配体文件格式：{suffix or '（没有扩展名）'}；"
            "支持 .sdf / .sd / .smi / .csv"
        )

    payload = source.read_bytes()
    log_read(source, purpose="import.ligands", size=len(payload))
    if not payload:
        raise LigandError("文件是空的")
    if len(payload) > MAX_IMPORT_BYTES:
        raise LigandError(
            f"文件 {len(payload) / 1024 / 1024:.1f} MB，超过 64 MB 上限；先拆分或先用 find_analogs 缩小集合"
        )
    if b"\x00" in payload:
        raise LigandError("文件里有 NUL 字节，看起来不是文本格式")

    fmt = FORMAT_BY_SUFFIX[suffix]
    if fmt == "sdf":
        raw_records = _read_sdf(source)
    else:
        text = _decode_text(payload)
        raw_records = _read_csv(text, smiles_column=smiles_column) if fmt == "csv" else _read_smi(text)

    records, skipped, counts = _clean_records(raw_records, limit=limit)
    return LoadedLigands(
        records=tuple(records),
        skipped=tuple(skipped),
        counts=counts,
        format=fmt,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        path=str(source.resolve()),
    )


def _decode_text(payload: bytes) -> str:
    """中文用户给的 CSV 常见 GBK；先 UTF-8，再 GBK，都不行就明说。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LigandError("文件不是 UTF-8 或 GBK 编码的文本")


def _read_csv(text: str, *, smiles_column: str) -> list[RawLigand]:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = "\t" if "\t" in first_line and "," not in first_line else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fieldnames = [name for name in (reader.fieldnames or []) if name]
    if not fieldnames:
        raise LigandError("CSV 没有表头")
    lookup = {name.strip().lower(): name for name in fieldnames}

    if smiles_column:
        smiles_key = lookup.get(smiles_column.strip().lower())
        if smiles_key is None:
            raise LigandError(
                f"CSV 里没有指定的 SMILES 列 {smiles_column!r}；实际表头：{', '.join(fieldnames)}"
            )
    else:
        smiles_key = _pick(lookup, ("smiles", "canonical_smiles", "smiles_std"))
        if smiles_key is None:
            raise LigandError(
                f"CSV 里找不到 SMILES 列；实际表头：{', '.join(fieldnames)}。"
                "用 smiles_column 指定列名，或把列改名为 smiles"
            )

    id_key = _pick(lookup, ("molecule_chembl_id", "chembl_id", "id", "ligand_id"))
    name_key = _pick(lookup, ("molecule_pref_name", "name", "pref_name", "title"))
    value_key = _pick(lookup, ("pchembl_value", "pic50", "activity"))

    records: list[RawLigand] = []
    for data_index, row in enumerate(reader, start=1):
        if data_index > MAX_RECORDS:
            raise LigandError(f"记录超过 {MAX_RECORDS} 条；先拆分文件")
        line_no = data_index + 1
        smiles = str(row.get(smiles_key) or "").strip()
        identifier = str(row.get(id_key) or "").strip() if id_key else ""
        name = str(row.get(name_key) or "").strip() if name_key else ""
        identifier = identifier or f"LIG-{data_index:04d}"
        records.append(
            RawLigand(
                index=data_index,
                id=identifier,
                name=name or identifier,
                smiles=smiles,
                source_ref=f"第 {line_no} 行",
                pchembl_value=_as_float(row.get(value_key)) if value_key else None,
            )
        )
    return records


def _read_smi(text: str) -> list[RawLigand]:
    records: list[RawLigand] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(records) >= MAX_RECORDS:
            raise LigandError(f"记录超过 {MAX_RECORDS} 条；先拆分文件")
        parts = stripped.split(maxsplit=1)
        smiles = parts[0]
        name = parts[1].strip() if len(parts) > 1 else ""
        index = len(records) + 1
        identifier = name or f"LIG-{index:04d}"
        records.append(
            RawLigand(
                index=index,
                id=identifier,
                name=name or identifier,
                smiles=smiles,
                source_ref=f"第 {line_no} 行",
            )
        )
    return records


def _read_sdf(path: Path) -> list[RawLigand]:
    from rdkit import Chem, rdBase

    records: list[RawLigand] = []
    # 同 canonicalize_smiles：坏记录要在 skipped 里说，不要让 RDKit 先往 stderr 打一份。
    with rdBase.BlockLogs():
        try:
            supplier = Chem.SDMolSupplier(str(path), removeHs=True, sanitize=True)
        except Exception as error:
            raise LigandError(f"SDF 打不开（{type(error).__name__}：{error}）") from error

        for index, mol in enumerate(supplier, start=1):
            if index > MAX_RECORDS:
                raise LigandError(f"记录超过 {MAX_RECORDS} 条；先拆分文件")
            if mol is None:
                records.append(
                    RawLigand(
                        index=index,
                        id=f"LIG-{index:04d}",
                        name="",
                        smiles="",
                        source_ref=f"第 {index} 条记录",
                        problem="SDF 记录解析失败（sanitize 没通过）",
                    )
                )
                continue
            problem = ""
            try:
                smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
            except Exception:
                smiles = ""
                problem = "SDF 记录转不成 SMILES"
            name = mol.GetProp("_Name").strip() if mol.HasProp("_Name") else ""
            identifier = ""
            for key in ("molecule_chembl_id", "chembl_id", "id"):
                if mol.HasProp(key):
                    identifier = mol.GetProp(key).strip()
                    break
            identifier = identifier or name or f"LIG-{index:04d}"
            records.append(
                RawLigand(
                    index=index,
                    id=identifier,
                    name=name or identifier,
                    smiles=smiles,
                    source_ref=f"第 {index} 条记录",
                    problem=problem,
                )
            )
    return records


def _pick(lookup: dict[str, str], candidates: Sequence[str]) -> str | None:
    for candidate in candidates:
        key = lookup.get(candidate.strip().lower())
        if key is not None:
            return key
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _source_cleaned(entry: Any) -> tuple[list[PrepItem], str]:
    """把 ligandset 条目读成待准备的清单。

    - raw 产物目录：读 `manifest.json` + `ligands.csv`（导入时已经去盐/去重）
    - ChEMBL 那种直接落 CSV 的条目：走 `load_ligand_file`，读的时候顺手清洗
    """
    path = getattr(entry, "path", None)
    if path is None:
        raise LigandError("这条配体集没有产物路径")
    path = Path(path)
    if path.is_dir():
        manifest_path = path / MANIFEST_FILE
        csv_path = path / CSV_FILE
        if not manifest_path.is_file() or not csv_path.is_file():
            raise LigandError(
                f"配体集目录不完整：需要 {MANIFEST_FILE} 与 {CSV_FILE}（{path}）"
            )
        try:
            manifest = products.read_json(manifest_path)
        except (OSError, json.JSONDecodeError) as error:
            raise LigandError(f"读 {MANIFEST_FILE} 失败（{type(error).__name__}：{error}）") from error
        stage = str(manifest.get("stage") or "")
        if stage == "prepared":
            raise LigandError("这条配体集已经准备过；直接把它给 dock，不要重复准备")
        try:
            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except OSError as error:
            raise LigandError(f"读 {CSV_FILE} 失败（{type(error).__name__}：{error}）") from error

        items: list[PrepItem] = []
        for index, row in enumerate(rows, start=1):
            smiles = str(row.get("smiles") or "").strip()
            if not smiles:
                continue
            identifier = str(row.get("id") or "").strip() or f"LIG-{index:04d}"
            items.append(
                PrepItem(
                    id=identifier,
                    name=str(row.get("name") or "").strip() or identifier,
                    source_index=_as_int(row.get("source_index")) or index,
                    cleaned=Cleaned(
                        input_smiles=str(row.get("input_smiles") or smiles),
                        smiles=smiles,
                        heavy_atoms=_as_int(row.get("heavy_atoms")),
                        fragments_removed=0,
                    ),
                )
            )
        return items, "raw"

    if path.is_file():
        loaded = load_ligand_file(path, limit=MAX_IMPORT_LIMIT)
        items = [
            PrepItem(
                id=str(row["id"]),
                name=str(row["name"]),
                source_index=_as_int(row.get("source_index")),
                cleaned=Cleaned(
                    input_smiles=str(row["input_smiles"]),
                    smiles=str(row["smiles"]),
                    heavy_atoms=_as_int(row.get("heavy_atoms")),
                    fragments_removed=_as_int(row.get("fragments_removed")),
                ),
            )
            for row in loaded.records
        ]
        return items, loaded.format

    raise LigandError(f"配体集的产物不在：{path}")


def _unique_items(
    items: Sequence[PrepItem], *, limit: int, offset: int = 0
) -> tuple[list[PrepItem], dict[str, int]]:
    """按 canonical SMILES 去重，再取 `[offset, offset+limit)`（去重后名额才算数）。

    `remaining` 是**这一批之后还剩多少**，不是「总数减 limit」：分批跑的时候，
    前一批的 offset 也算在已处理里，否则每批都会说「还剩 N」而 N 一直不变。
    """
    seen: set[str] = set()
    unique: list[PrepItem] = []
    duplicate = 0
    for item in items:
        key = item.cleaned.smiles
        if key in seen:
            duplicate += 1
            continue
        seen.add(key)
        unique.append(item)
    kept = unique[offset : offset + limit]
    remaining = max(len(unique) - offset - len(kept), 0)
    return kept, {
        "input": len(items),
        "unique": len(unique),
        "duplicate": duplicate,
        "truncated": remaining,
        "remaining": remaining,
    }


def _describe(item: PrepItem) -> dict[str, Any] | None:
    """预演用的性质统计；解析失败返回 `None`（源已清洗过，正常不会发生）。"""
    from rdkit import Chem, rdBase
    from rdkit.Chem import Descriptors

    with rdBase.BlockLogs():
        mol = Chem.MolFromSmiles(item.cleaned.smiles)
        if mol is None:
            return None
        try:
            return {
                "mw": float(Descriptors.MolWt(mol)),
                "heavy_atoms": int(mol.GetNumHeavyAtoms()),
                "rotatable": int(Descriptors.NumRotatableBonds(mol)),
                "logp": float(Descriptors.MolLogP(mol)),
            }
        except Exception:
            return None


def _meeko_version() -> str:
    import meeko

    return str(getattr(meeko, "__version__", "unknown"))


def _unique_filename(item: PrepItem, used: set[str]) -> str:
    """ID 清洗成文件名；同一批里重名就加序号，绝不覆盖前一个。"""
    base = products.safe_stem(item.id or item.name or "ligand")
    candidate = base
    index = 2
    while candidate.lower() in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate.lower())
    return candidate


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
def _csv_text(records: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for record in records:
        row: dict[str, Any] = {}
        for key in CSV_COLUMNS:
            value = record.get(key, "")
            row[key] = "" if value is None else value
        writer.writerow(row)
    return buffer.getvalue()


def _manifest(loaded: LoadedLigands, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "ligandset",
        "stage": "raw",
        "tool": "import_ligandset",
        "source": {
            "path": loaded.path,
            "format": loaded.format,
            "sha256": loaded.sha256,
            "size": loaded.size,
        },
        "params": params,
        "counts": loaded.counts,
        "skipped": list(loaded.skipped),
    }


def _summary(name: str, loaded: LoadedLigands) -> str:
    return f"原始配体集 · {name} · {loaded.counts['accepted']} 个（{loaded.format}）"


def _skip_digest(skipped: Sequence[dict[str, Any]]) -> str:
    if not skipped:
        return ""
    lines = ["跳过的前几条："]
    for item in skipped[:MAX_SKIPPED_INLINE]:
        label = item.get("id") or item.get("raw") or "（空）"
        lines.append(
            f"  · {item.get('source_ref', '')} {label}：{item.get('detail') or item.get('reason')}"
        )
    rest = len(skipped) - MAX_SKIPPED_INLINE
    if rest > 0:
        lines.append(f"  还有 {rest} 条，完整记录见 manifest.json。")
    return "\n".join(lines)


def _render_import(
    *,
    handle: str,
    reused: bool,
    target: Path,
    loaded: LoadedLigands,
) -> str:
    counts = loaded.counts
    if reused:
        lines = [f"登记表里已有同一份：{handle}（未重复导入）。"]
    else:
        lines = [f"已导入 {counts['accepted']} 个配体，登记为 {handle}。"]

    lines.append(f"来源：{loaded.path}（{loaded.format}，{counts['input']} 条记录）。")
    lines.append(
        f"可用 {counts['accepted']}、重复 {counts['duplicate']}、无效 {counts['invalid']}。"
    )
    if counts["remaining"]:
        lines.append(
            f"只导入前 {counts['accepted']} 个（另有 {counts['remaining']} 个有效分子没有导入）；"
            "要接着做，把 limit 调大再跑一次，这会生成另一组参数的新目录，不覆盖本次结果。"
        )
    digest = _skip_digest(loaded.skipped)
    if digest:
        lines.append(digest)
    lines.append(f"产物目录：{target}")
    return "\n".join(lines)


def _preview_text(
    *,
    counts: dict[str, int],
    kept: Sequence[PrepItem],
    descriptions: Sequence[dict[str, Any]],
) -> str:
    lines = [
        f"预演：源配体集 {counts['input']} 条，去重后 {counts['unique']} 个，"
        f"本次取 {len(kept)} 个（重复 {counts['duplicate']} 个已跳过）。"
    ]
    if descriptions:
        heavy = [item["heavy_atoms"] for item in descriptions]
        mw = [item["mw"] for item in descriptions]
        rotatable = [item["rotatable"] for item in descriptions]
        lines.append(
            f"重原子 {min(heavy)}–{max(heavy)}，MW {min(mw):.1f}–{max(mw):.1f}，"
            f"可旋转键 {min(rotatable)}–{max(rotatable)}（统计 {len(descriptions)} 个）。"
        )
    lines.append("confirm=false：只做解析/去重/性质统计，没有写盘、没有登记。")
    lines.append(
        "confirm=true 才会生成 3D 构象与 PDBQT；实际成功数可能少于本次取的个数"
        "（3D 或 Meeko 可能失败）。"
    )
    if counts["remaining"]:
        lines.append(
            f"另有 {counts['remaining']} 个有效分子没有处理；接着处理用 "
            f"offset={counts.get('next_offset', 0)}，或调大 limit。"
            "换 offset / limit 都会生成另一组参数的新目录，不覆盖本次结果。"
        )
    return "\n".join(lines)


def _prepared_manifest(
    *,
    source_handle: str,
    source_fingerprint: str,
    source_path: str,
    counts: dict[str, int],
    params: dict[str, Any],
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": "ligandset",
        "stage": "prepared",
        "tool": "prepare_ligands",
        "source": {
            "handle": source_handle,
            "fingerprint": source_fingerprint,
            "path": source_path,
            "count": counts["input"],
        },
        "engine": {
            "rdkit": _rdkit_version(),
            "meeko": _meeko_version(),
        },
        "params": params,
        "params_fingerprint": fingerprint_parts(params, length=16),
        "counts": counts,
        "records": list(records),
    }


def _prepared_meta(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "ligandset",
        "stage": "prepared",
        "tool": "prepare_ligands",
        "source": manifest.get("source") or {},
        "counts": manifest.get("counts") or {},
        "engine": manifest.get("engine") or {},
        "params": manifest.get("params") or {},
        "file": MANIFEST_FILE,
        "pdbqt_dir": PDBQT_DIR,
    }


def _prepared_entry_complete(entry: Any) -> bool:
    """登记表里那条 prepared 条目的产物是否真的齐全。

    内容指纹只看文件内容，不看目录名：同参数的 `-2` 目录与基准目录内容可以完全
    相同，指纹也就相同。若基准目录被删掉了 `params.json`（上一次崩在写盘中间），
    这时不能把旧 handle 当成可复用——它指着一个半成品。
    """
    path = getattr(entry, "path", None)
    if path is None:
        return False
    directory = Path(path)
    if not directory.is_dir():
        return False
    if not (directory / MANIFEST_FILE).is_file() or not (directory / PARAMS_FILE).is_file():
        return False
    return any((directory / PDBQT_DIR).glob("*.pdbqt"))


def _register_prepared(
    ledger: SessionLedger,
    *,
    target: Path,
    fingerprint: str,
    summary: str,
    manifest: dict[str, Any],
) -> tuple[str, bool]:
    existing = ledger.find(
        kind="ligandset", fingerprint=fingerprint, include_archived=True
    )
    for candidate in existing:
        if _prepared_entry_complete(candidate):
            return str(candidate.handle), True
    entry = ledger.add(
        "ligandset",
        path=target,
        summary=summary,
        fingerprint=fingerprint,
        meta=_prepared_meta(manifest),
        link_current_receptor=False,
    )
    return str(entry.handle), False


def _failure_digest(
    records: Sequence[dict[str, Any]], *, limit: int = MAX_SKIPPED_INLINE
) -> str:
    failed = [item for item in records if item.get("status") != "ok"]
    if not failed:
        return ""
    lines = ["没准备成功的："]
    for item in failed[:limit]:
        lines.append(
            f"  · {item.get('id') or '（空）'}：{item.get('status')}：{item.get('error') or ''}"
        )
    rest = len(failed) - limit
    if rest > 0:
        lines.append(f"  还有 {rest} 条，完整记录见 {MANIFEST_FILE}。")
    return "\n".join(lines)


def _render_prepared(
    *,
    handle: str,
    reused: bool,
    target: Path,
    counts: dict[str, int],
    records: Sequence[dict[str, Any]],
) -> str:
    if reused:
        lines = [f"登记表里已有同一份：{handle}（未重复准备）。"]
    else:
        lines = [f"已准备 {counts['prepared']} 个配体，登记为 {handle}。"]
    lines.append(
        f"源配体集 {counts['input']} 条；去重后 {counts['unique']} 个，"
        f"本次取 {counts['selected']} 个。"
    )
    lines.append(
        f"成功 {counts['prepared']}、3D 失败 {counts['embed_failed']}、"
        f"Meeko 失败 {counts['meeko_failed']}、无效 {counts['invalid']}。"
    )
    if counts["remaining"]:
        lines.append(
            f"另有 {counts['remaining']} 个有效分子没有处理；接着处理用 "
            f"offset={counts.get('next_offset', 0)}，或调大 limit。"
            "换 offset / limit 都会生成另一组参数的新目录，不覆盖本次结果。"
        )
    digest = _failure_digest(records)
    if digest:
        lines.append(digest)
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：dock(receptor=..., ligands={handle})。")
    return "\n".join(lines)


def _render_no_prepared(
    *, target: Path, counts: dict[str, int], records: Sequence[dict[str, Any]]
) -> str:
    lines = [
        f"没有准备出任何 PDBQT：3D 失败 {counts['embed_failed']}、"
        f"Meeko 失败 {counts['meeko_failed']}、无效 {counts['invalid']}。"
    ]
    digest = _failure_digest(records)
    if digest:
        lines.append(digest)
    if counts["remaining"]:
        lines.append(f"另有 {counts['remaining']} 个有效分子没有处理（limit 截断）。")
    lines.append(f"产物目录（未登记）：{target}")
    return "\n".join(lines)


def _rdkit_version() -> str:
    from rdkit import rdBase

    return str(rdBase.rdkitVersion)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class ImportLigandsetTool(Tool):
    """导入本机的一份配体文件，登记成原始配体集。"""

    spec = ToolSpec(
        name="import_ligandset",
        description=(
            "导入**本机的一份配体文件**（.sdf / .sd / .smi / .csv），登记成一个**原始配体集**。"
            "CSV 至少要有 smiles 列（也认 canonical_smiles；ChEMBL 结果的 molecule_chembl_id / "
            "molecule_pref_name / pchembl_value 会一并保留）。每条会去盐（只留最大片段）、"
            "canonical SMILES 去重；无效 SMILES 跳过并逐条报原因。"
            f"{CHEM_TOOL_DESCRIPTION_NOTE}"
            "**这是与 import_structure 并列的第二个收路径动作**：导入后一律用返回的 handle，"
            "不要再传路径。同一份内容（sha256）与同参数重复导入不会重复登记。"
            "单条 SMILES 请写成 .smi 的一行（`SMILES 名字`），本工具不直接收 SMILES 字符串。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "本机配体文件的路径，建议给绝对路径；支持 .sdf / .sd / .smi / .csv",
                },
                "name": {"type": "string", "description": "配体集显示名；留空用文件名"},
                "smiles_column": {
                    "type": "string",
                    "description": "CSV 里 SMILES 的列名；留空依次尝试 smiles / canonical_smiles",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"最多保留多少个有效分子，默认 {DEFAULT_IMPORT_LIMIT}，"
                        f"上限 {MAX_IMPORT_LIMIT}"
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    primary_argument = "path"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：import_ligandset 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        raw_path = str(arguments.get("path") or "").strip()
        if not raw_path:
            return ToolResult(
                output="缺少参数 path。给一份 .sdf / .sd / .smi / .csv 的路径。",
                failed=True,
            )
        source = Path(raw_path).expanduser()
        if not source.exists():
            return ToolResult(
                output=f"找不到这个文件：{source}。给绝对路径更稳。", failed=True
            )
        if not source.is_file():
            return ToolResult(
                output=f"导入要的是一个配体文件，不是目录：{source}", failed=True
            )

        limit = arguments.get("limit")
        if limit is None:
            limit = DEFAULT_IMPORT_LIMIT
        if isinstance(limit, bool) or not isinstance(limit, int):
            return ToolResult(output="参数 limit 应当是整数", failed=True)
        if not 1 <= limit <= MAX_IMPORT_LIMIT:
            return ToolResult(
                output=f"limit 要在 1 到 {MAX_IMPORT_LIMIT} 之间（现在是 {limit}）",
                failed=True,
            )

        smiles_column = str(arguments.get("smiles_column") or "").strip()
        name = str(arguments.get("name") or "").strip() or source.stem

        try:
            loaded = load_ligand_file(
                source, smiles_column=smiles_column, limit=limit
            )
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        except OSError as error:
            return ToolResult(
                output=f"读文件失败（{type(error).__name__}：{error}）", failed=True
            )

        if not loaded.records:
            digest = _skip_digest(loaded.skipped)
            detail = f"\n{digest}" if digest else ""
            return ToolResult(
                output=(
                    f"没有可导入的分子：{loaded.counts['input']} 条记录里，"
                    f"无效 {loaded.counts['invalid']}、重复 {loaded.counts['duplicate']}。"
                    f"{detail}"
                ),
                failed=True,
            )

        params: dict[str, Any] = {
            "source_sha256": loaded.sha256,
            "source_format": loaded.format,
            "smiles_column": smiles_column,
            "limit": limit,
            "strip_salts": True,
            "dedupe": True,
            "rdkit": _rdkit_version(),
        }
        required = (CSV_FILE, MANIFEST_FILE, PARAMS_FILE)
        try:
            target, content_reused = products.content_dir(
                raw_ligandset_dir(), name, params, required
            )
            if not content_reused:
                target.mkdir(parents=True, exist_ok=True)
                products.write_text_atomic(target / CSV_FILE, _csv_text(loaded.records))
                products.write_json_atomic(target / MANIFEST_FILE, _manifest(loaded, params))
                # 最后写：目录存在但缺它，说明上一次写在中间崩了，不能当完整产物复用。
                products.write_json_atomic(target / PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(
                output=f"写配体集失败（{type(error).__name__}：{error}）", failed=True
            )

        fingerprint = fingerprint_path(target)
        existing = ledger.find(
            kind="ligandset", fingerprint=fingerprint, include_archived=True
        )
        if existing:
            entry = existing[0]
            handle = str(entry.handle)
            ledger_reused = True
        else:
            entry = ledger.add(
                "ligandset",
                path=target,
                summary=_summary(name, loaded),
                fingerprint=fingerprint,
                meta={
                    "kind": "ligandset",
                    "stage": "raw",
                    "tool": "import_ligandset",
                    "source": {
                        "path": loaded.path,
                        "format": loaded.format,
                        "sha256": loaded.sha256,
                        "size": loaded.size,
                    },
                    "params": params,
                    "counts": loaded.counts,
                    "file": CSV_FILE,
                    "manifest": MANIFEST_FILE,
                },
                # 配体库跨靶点：换受体时不该被搁置。
                link_current_receptor=False,
            )
            handle = str(entry.handle)
            ledger_reused = False

        return ToolResult(
            output=_render_import(
                handle=handle, reused=ledger_reused, target=target, loaded=loaded
            ),
            meta={
                "handle": handle,
                "fingerprint": fingerprint,
                "count": loaded.counts["accepted"],
                "duplicate": loaded.counts["duplicate"],
                "invalid": loaded.counts["invalid"],
                "remaining": loaded.counts["remaining"],
            },
        )


class PrepareLigandsTool(Tool):
    """把原始配体集准备成可对接的 PDBQT。"""

    spec = ToolSpec(
        name="prepare_ligands",
        description=(
            "把原始配体集准备成**可以直接对接的 PDBQT**：去盐后的 SMILES → ETKDGv3 3D 构象 → "
            "MMFF94（不可用时 UFF）→ Meeko 指派 AutoDock 原子类型与部分电荷。"
            f"{CHEM_TOOL_DESCRIPTION_NOTE}"
            "**默认只预演（confirm=false）**：只做解析/去重/性质统计，不写盘、不登记；"
            "confirm=true 才生成 3D 与 PDBQT，并登记成**准备后的配体集**。"
            "同一组参数按内容指纹复用，不覆盖旧产物。单条失败（3D 或 Meeko）不中断整批，"
            "但整批一个都没成功时不登记。源可以是 import_ligandset 的产物，也可以是 "
            "search_actives / find_analogs 直接落下的 CSV。"
            "\n**大库用 offset 分批**：`limit` 是这一批取多少，`offset` 是从第几条开始。"
            "两批之间不会重叠，也不会漏——`offset` 进缓存键，所以两批是两个产物目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ligands": {
                    "type": "string",
                    "description": "原始配体集 handle，例如 ligandset#2。**不要传文件路径**",
                },
                "limit": {
                    "type": "integer",
                    "description": "这一批最多准备多少个（去重后按源顺序取），默认 200，上限 2000",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几条开始取（去重后的序号），默认 0；大库分批用",
                },
                "seed": {
                    "type": "integer",
                    "description": "ETKDGv3 的随机种子，默认 42；0 表示随机。换 seed 会生成另一组构象",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "true 才真正写盘与登记；默认 false 只预演",
                },
            },
            "required": ["ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "ligands"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：prepare_ligands 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        raw_handle = str(arguments.get("ligands") or "").strip()
        if not raw_handle:
            return ToolResult(
                output="缺少参数 ligands。先 list_workspace 看清有哪份配体集，再把它的 handle 传进来。",
                failed=True,
            )
        try:
            entry = ledger.get(raw_handle)
            expect_handle(entry, LigandSetHandle, "ligands")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)
        if str(entry.meta.get("stage") or "") == "prepared":
            return ToolResult(
                output="这条配体集已经准备过；直接把它给 dock，不要重复准备。",
                failed=True,
            )

        limit = arguments.get("limit")
        if limit is None:
            limit = DEFAULT_PREPARE_LIMIT
        if isinstance(limit, bool) or not isinstance(limit, int):
            return ToolResult(output="参数 limit 应当是整数", failed=True)
        if not 1 <= limit <= MAX_PREPARE_LIMIT:
            return ToolResult(
                output=f"limit 要在 1 到 {MAX_PREPARE_LIMIT} 之间（现在是 {limit}）",
                failed=True,
            )

        offset = arguments.get("offset")
        if offset is None:
            offset = 0
        if isinstance(offset, bool) or not isinstance(offset, int):
            return ToolResult(output="参数 offset 应当是整数", failed=True)
        if not 0 <= offset <= MAX_OFFSET:
            return ToolResult(
                output=f"offset 要在 0 到 {MAX_OFFSET} 之间（现在是 {offset}）",
                failed=True,
            )

        seed = arguments.get("seed")
        if seed is None:
            seed = DEFAULT_SEED
        if isinstance(seed, bool) or not isinstance(seed, int):
            return ToolResult(output="参数 seed 应当是整数", failed=True)
        if seed < 0 or seed > MAX_SEED:
            return ToolResult(
                output=f"seed 要在 0 到 {MAX_SEED} 之间（0 表示随机）", failed=True
            )

        confirm = arguments.get("confirm", False)
        if not isinstance(confirm, bool):
            return ToolResult(output="参数 confirm 应当是 true 或 false", failed=True)

        try:
            items, source_format = _source_cleaned(entry)
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        except OSError as error:
            return ToolResult(
                output=f"读配体集失败（{type(error).__name__}：{error}）", failed=True
            )
        if not items:
            return ToolResult(
                output="源配体集里没有可用分子；先用 import_ligandset 或 search_actives 建一份。",
                failed=True,
            )

        kept, counts = _unique_items(items, limit=limit, offset=offset)
        counts["selected"] = len(kept)
        counts["offset"] = offset
        counts["next_offset"] = offset + len(kept)
        if not kept:
            beyond = offset >= counts["unique"]
            return ToolResult(
                output=(
                    f"offset={offset} 已经越过末尾：去重后有 {counts['unique']} 个分子，"
                    f"从第 {offset + 1} 条开始一条也没有。"
                    if beyond
                    else "去重之后没有可准备的分子。"
                ),
                failed=True,
            )

        if not confirm:
            descriptions: list[dict[str, Any]] = []
            for item in kept:
                described = _describe(item)
                if described:
                    descriptions.append(described)
            return ToolResult(
                output=_preview_text(counts=counts, kept=kept, descriptions=descriptions),
                meta={
                    "preview": True,
                    "count": len(kept),
                    "unique": counts["unique"],
                    "remaining": counts["remaining"],
                    "source": source_format,
                },
            )

        source_path = Path(entry.path) if entry.path is not None else None
        source_stem = (
            source_path.stem if source_path is not None and source_path.is_file()
            else (source_path.name if source_path is not None else "ligands")
        )
        params: dict[str, Any] = {
            "source_handle": str(entry.handle),
            "source_fingerprint": str(entry.fingerprint or ""),
            "source_path": str(source_path or ""),
            "limit": limit,
            "offset": offset,
            "seed": seed,
            "strip_salts": True,
            "dedupe": True,
            "forcefield": "MMFF94/UFF",
            "etkdg": "ETKDGv3",
            "rdkit": _rdkit_version(),
            "meeko": _meeko_version(),
        }
        try:
            target, content_reused = products.content_dir(
                prepared_ligandset_dir(),
                f"{source_stem}-prepared",
                params,
                (MANIFEST_FILE, PARAMS_FILE),
            )
        except OSError as error:
            return ToolResult(
                output=f"准备配体的产物目录失败（{type(error).__name__}：{error}）",
                failed=True,
            )

        if content_reused:
            try:
                manifest = products.read_json(target / MANIFEST_FILE)
            except (OSError, json.JSONDecodeError) as error:
                return ToolResult(
                    output=f"复用旧产物时读 {MANIFEST_FILE} 失败（{type(error).__name__}：{error}）",
                    failed=True,
                )
            counts = {**counts, **(manifest.get("counts") or {})}
            records = list(manifest.get("records") or [])
            fingerprint = fingerprint_path(target)
            handle, ledger_reused = _register_prepared(
                ledger,
                target=target,
                fingerprint=fingerprint,
                summary=f"准备后的配体集 · {source_stem} · {counts.get('prepared', 0)} 个 PDBQT",
                manifest=manifest,
            )
            return ToolResult(
                output=_render_prepared(
                    handle=handle,
                    reused=ledger_reused,
                    target=target,
                    counts=counts,
                    records=records,
                ),
                meta={
                    "handle": handle,
                    "fingerprint": fingerprint,
                    "count": counts.get("prepared", 0),
                    "failed": (
                        counts.get("embed_failed", 0)
                        + counts.get("meeko_failed", 0)
                        + counts.get("invalid", 0)
                    ),
                    "remaining": counts.get("remaining", 0),
                    "reused": True,
                },
            )

        target.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        used_names: set[str] = set()
        prepared_count = 0
        embed_failed = 0
        meeko_failed = 0
        invalid = 0

        def failed_record(item: PrepItem, status: str, error: str) -> dict[str, Any]:
            return {
                "id": item.id,
                "name": item.name,
                "smiles": item.cleaned.smiles,
                "heavy_atoms": item.cleaned.heavy_atoms,
                "source_index": item.source_index,
                "status": status,
                "pdbqt": "",
                "forcefield": "",
                "setups": 0,
                "error": error,
                "warnings": [],
            }

        for item in kept:
            try:
                prepared = prepare_molecule(item.cleaned, seed=seed)
            except EmbedError as error:
                embed_failed += 1
                records.append(failed_record(item, "embed_failed", str(error)))
                continue
            except MeekoError as error:
                meeko_failed += 1
                records.append(failed_record(item, "meeko_failed", str(error)))
                continue
            except LigandError as error:
                invalid += 1
                records.append(failed_record(item, "invalid", str(error)))
                continue

            filename = f"{_unique_filename(item, used_names)}.pdbqt"
            try:
                products.write_text_atomic(
                    target / PDBQT_DIR / filename, prepared.pdbqt
                )
            except OSError as error:
                return ToolResult(
                    output=f"写 PDBQT 失败（{type(error).__name__}：{error}）",
                    failed=True,
                )
            prepared_count += 1
            records.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "smiles": item.cleaned.smiles,
                    "heavy_atoms": item.cleaned.heavy_atoms,
                    "source_index": item.source_index,
                    "status": "ok",
                    "pdbqt": f"{PDBQT_DIR}/{filename}",
                    "forcefield": prepared.forcefield,
                    "setups": prepared.setups,
                    "error": "",
                    "warnings": list(prepared.warnings),
                }
            )

        counts.update(
            {
                "prepared": prepared_count,
                "invalid": invalid,
                "embed_failed": embed_failed,
                "meeko_failed": meeko_failed,
            }
        )
        if prepared_count == 0:
            return ToolResult(
                output=_render_no_prepared(
                    target=target, counts=counts, records=records
                ),
                failed=True,
            )

        manifest = _prepared_manifest(
            source_handle=str(entry.handle),
            source_fingerprint=str(entry.fingerprint or ""),
            source_path=str(source_path or ""),
            counts=counts,
            params=params,
            records=records,
        )
        try:
            products.write_json_atomic(target / MANIFEST_FILE, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(
                output=f"写准备产物失败（{type(error).__name__}：{error}）", failed=True
            )

        fingerprint = fingerprint_path(target)
        handle, ledger_reused = _register_prepared(
            ledger,
            target=target,
            fingerprint=fingerprint,
            summary=f"准备后的配体集 · {source_stem} · {prepared_count} 个 PDBQT",
            manifest=manifest,
        )
        return ToolResult(
            output=_render_prepared(
                handle=handle,
                reused=ledger_reused,
                target=target,
                counts=counts,
                records=records,
            ),
            meta={
                "handle": handle,
                "fingerprint": fingerprint,
                "count": prepared_count,
                "failed": embed_failed + meeko_failed + invalid,
                "remaining": counts["remaining"],
                "reused": ledger_reused,
            },
        )


# ---------------------------------------------------------------------------
# 待筛库的第一道闸：PAINS / Brenk / 去重 / 性质窗口
# ---------------------------------------------------------------------------
CURATED_DIR = "curated"
CURATED_COLUMNS: tuple[str, ...] = CSV_COLUMNS + ("flags",)

DEFAULT_MAX_MW = 500.0
MAX_MW_LIMIT = 2000.0
DEDUPE_MODES = ("none", "canonical", "inchikey", "scaffold")
MODE_MODES = ("drop", "flag")
MAX_DROP_EXAMPLES = 5
INCHIKEY_SKELETON = 14


def curated_ligandset_dir() -> Path:
    """筛过的配体集的根。测试替换它，避免写用户家目录。"""
    return ligands_dir() / CURATED_DIR


def _optional_number(
    arguments: dict[str, Any], name: str, low: float, high: float
) -> float | None:
    """可选窗口参数：不给就是 `None`（这一条不设限），不是 0。"""
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LigandError(f"参数 {name} 应当是数")
    number = float(value)
    if not low <= number <= high:
        raise LigandError(f"参数 {name} 要在 {low:g} 到 {high:g} 之间（现在是 {number:g}）")
    return number


def _optional_int(arguments: dict[str, Any], name: str, low: int, high: int) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LigandError(f"参数 {name} 应当是整数")
    if not low <= value <= high:
        raise LigandError(f"参数 {name} 要在 {low} 到 {high} 之间（现在是 {value}）")
    return value


def _filter_catalog(kinds: Sequence[str]):
    """把若干 `FilterCatalogParams.FilterCatalogs` 拼成一个目录。"""
    try:  # rdkit ≥ 2023.09 把 FilterCatalog 挪进了 rdfiltercatalog
        from rdkit.Chem import rdfiltercatalog as catalog_module
        from rdkit.Chem.rdfiltercatalog import FilterCatalogParams
    except ImportError:  # 老版本仍在 rdkit.Chem 下
        from rdkit.Chem import FilterCatalog as catalog_module
        from rdkit.Chem.FilterCatalog import FilterCatalogParams

    params = FilterCatalogParams()
    for kind in kinds:
        params.AddCatalog(getattr(FilterCatalogParams.FilterCatalogs, kind))
    return catalog_module.FilterCatalog(params)


def _properties(mol: Any) -> dict[str, float]:
    from rdkit.Chem import Descriptors, Lipinski

    return {
        "mw": float(Descriptors.MolWt(mol)),
        "logp": float(Descriptors.MolLogP(mol)),
        "tpsa": float(Descriptors.TPSA(mol)),
        "hbd": float(Lipinski.NumHDonors(mol)),
        "hba": float(Lipinski.NumHAcceptors(mol)),
        "rotb": float(Lipinski.NumRotatableBonds(mol)),
    }


def _dedupe_key(mol: Any, smiles: str, mode: str) -> str:
    """去重键。`scaffold` 用 InChIKey 的前 14 位（骨架段），不是 Murcko 骨架。"""
    from rdkit import Chem

    if mode == "canonical":
        return Chem.MolToSmiles(mol)
    inchikey = Chem.MolToInchiKey(mol)
    if mode == "scaffold":
        return inchikey[:INCHIKEY_SKELETON] or smiles
    return inchikey or smiles


def _window_hits(properties: dict[str, float], windows: dict[str, tuple[float | None, float | None]]) -> list[str]:
    """闭区间判据：正好等于上限的保留，超过才算命中。"""
    hits: list[str] = []
    for name, (low, high) in windows.items():
        value = properties.get(name)
        if value is None:
            continue
        if low is not None and value < low:
            hits.append(f"{name}<{low:g}")
        elif high is not None and value > high:
            hits.append(f"{name}>{high:g}")
    return hits


def _curated_csv_text(records: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=CURATED_COLUMNS, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for record in records:
        row = {key: record.get(key, "") for key in CURATED_COLUMNS}
        row["flags"] = ";".join(record.get("flags") or [])
        writer.writerow(row)
    return buffer.getvalue()


def _render_curated(
    *,
    handle: str,
    reused: bool,
    kept: int,
    dropped: dict[str, int],
    examples: Sequence[dict[str, Any]],
    total: int,
    mode: str,
    target: Path,
) -> str:
    removed = sum(dropped.values())
    lines = [
        f"已筛过一遍，登记为 {handle}。" if not reused else f"登记表里已有同一份：{handle}（未重筛）。",
        f"输入 {total} 条，保留 {kept} 条，淘汰 {removed} 条（mode={mode}）。",
    ]
    if dropped:
        detail = "、".join(f"{name} {count} 条" for name, count in dropped.items() if count)
        lines.append(f"按规则：{detail}。")
    else:
        lines.append("按规则：没有一条命中——这套库在这一组判据下是干净的。")
    if examples:
        lines.append("淘汰示例：")
        for item in examples[:MAX_DROP_EXAMPLES]:
            lines.append(
                f"  · {item['id']}（{item['smiles'][:40]}）：{'、'.join(item['flags'])}"
            )
        rest = removed - min(len(examples), MAX_DROP_EXAMPLES)
        if rest > 0:
            lines.append(f"  ……另有 {rest} 条未显示")
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：prepare_ligands(ligands={handle}, confirm=true)。")
    return "\n".join(lines)


class CurateLigandsTool(Tool):
    """把待筛库过一遍 PAINS / Brenk / 去重 / 性质窗口。"""

    spec = ToolSpec(
        name="curate_ligands",
        description=(
            "把**待筛库**过第一道闸：PAINS / Brenk 结构警报、去重、理化窗口，"
            "产出一份筛过的配体集（可直接交给 prepare_ligands）。"
            + CHEM_TOOL_DESCRIPTION_NOTE
            + "\n"
            "窗口都是**闭区间**（等于上限的保留），不给就不设限。`mode=drop`（默认）"
            "真删，`mode=flag` 只标注、把命中的规则写进 `flags` 列——想先看看会删掉什么"
            "就用它。\n"
            "判据由代码算（RDKit FilterCatalog + 描述符），schema 里没有让模型自己判的开关。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ligands": {
                    "type": "string",
                    "description": "原始配体集的 handle（已经 prepare 过的不能筛）",
                },
                "min_mw": {"type": "number", "description": "分子量下限"},
                "max_mw": {
                    "type": "number",
                    "description": f"分子量上限，常用 {DEFAULT_MAX_MW:g}",
                },
                "max_logp": {"type": "number", "description": "logP 上限"},
                "max_tpsa": {"type": "number", "description": "TPSA 上限（Å²）"},
                "max_hbd": {"type": "integer", "description": "氢键供体数上限"},
                "max_hba": {"type": "integer", "description": "氢键受体数上限"},
                "max_rotb": {"type": "integer", "description": "可旋转键数上限"},
                "pains": {
                    "type": "boolean",
                    "description": "是否查 PAINS_A/B/C，默认 true",
                },
                "brenk": {
                    "type": "boolean",
                    "description": "是否查 Brenk 不稳定/反应性片段，默认 true",
                },
                "dedupe": {
                    "type": "string",
                    "description": "去重键：none / canonical / inchikey（默认）/ scaffold（InChIKey 前 14 位）",
                },
                "mode": {
                    "type": "string",
                    "description": "drop（默认，真删）/ flag（只标注，保留全集）",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "ligands"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：curate_ligands 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw = str(arguments.get("ligands") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 ligands。先 import_ligandset 或 list_workspace 看清有哪份配体集。",
                failed=True,
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, LigandSetHandle, "ligands")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        dedupe = str(arguments.get("dedupe") or "inchikey").strip().lower()
        if dedupe not in DEDUPE_MODES:
            return ToolResult(
                output=f"参数 dedupe 只能是 {', '.join(DEDUPE_MODES)}（现在是 {dedupe}）",
                failed=True,
            )
        mode = str(arguments.get("mode") or "drop").strip().lower()
        if mode not in MODE_MODES:
            return ToolResult(
                output=f"参数 mode 只能是 {', '.join(MODE_MODES)}（现在是 {mode}）", failed=True
            )
        want_pains = arguments.get("pains")
        want_brenk = arguments.get("brenk")
        for name, value in (("pains", want_pains), ("brenk", want_brenk)):
            if value is not None and not isinstance(value, bool):
                return ToolResult(output=f"参数 {name} 应当是 true / false", failed=True)

        try:
            windows = {
                "mw": (
                    _optional_number(arguments, "min_mw", 0.0, MAX_MW_LIMIT),
                    _optional_number(arguments, "max_mw", 0.0, MAX_MW_LIMIT),
                ),
                "logp": (None, _optional_number(arguments, "max_logp", -20.0, 40.0)),
                "tpsa": (None, _optional_number(arguments, "max_tpsa", 0.0, 1000.0)),
                "hbd": (None, _optional_int(arguments, "max_hbd", 0, 50)),
                "hba": (None, _optional_int(arguments, "max_hba", 0, 50)),
                "rotb": (None, _optional_int(arguments, "max_rotb", 0, 100)),
            }
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        if windows["mw"][0] is not None and windows["mw"][1] is not None:
            if windows["mw"][0] > windows["mw"][1]:
                return ToolResult(
                    output=(
                        f"min_mw（{windows['mw'][0]:g}）大于 max_mw（{windows['mw'][1]:g}），"
                        "这个窗口是空的"
                    ),
                    failed=True,
                )

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        try:
            items, source_kind = _source_cleaned(entry)
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        if not items:
            return ToolResult(output=f"{entry.handle} 里没有可筛的分子。", failed=True)

        from rdkit import Chem, rdBase

        catalogs = []
        if want_pains is not False:
            catalogs.append(("PAINS", _filter_catalog(["PAINS_A", "PAINS_B", "PAINS_C"])))
        if want_brenk is not False:
            catalogs.append(("Brenk", _filter_catalog(["BRENK"])))

        params = {
            "source": str(entry.handle),
            "windows": {
                name: [low, high] for name, (low, high) in windows.items()
                if low is not None or high is not None
            },
            "pains": want_pains is not False,
            "brenk": want_brenk is not False,
            "dedupe": dedupe,
            "mode": mode,
        }
        stem = str(arguments.get("name") or entry.meta.get("name") or "curated")
        try:
            target, reused = products.content_dir(
                curated_ligandset_dir(), stem, params, (MANIFEST_FILE, CSV_FILE, products.PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="ligandset", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("stage") or "") == "curated":
                    counts = dict((candidate.meta or {}).get("counts") or {})
                    return ToolResult(
                        output=_render_curated(
                            handle=str(candidate.handle),
                            reused=True,
                            kept=int(counts.get("kept", 0)),
                            dropped=dict(counts.get("dropped_by_rule") or {}),
                            examples=[],
                            total=int(counts.get("input", 0)),
                            mode=mode,
                            target=target,
                        ),
                        meta={"handle": str(candidate.handle), "reused": True,
                              "kept": counts.get("kept", 0)},
                    )

        records: list[dict[str, Any]] = []
        dropped: dict[str, int] = {}
        examples: list[dict[str, Any]] = []
        seen: dict[str, str] = {}

        with rdBase.BlockLogs():
            for item in items:
                mol = Chem.MolFromSmiles(item.cleaned.smiles)
                if mol is None:
                    dropped["无法解析"] = dropped.get("无法解析", 0) + 1
                    examples.append({"id": item.id, "smiles": item.cleaned.smiles,
                                     "flags": ["无法解析"]})
                    continue
                flags: list[str] = []
                for label, catalog in catalogs:
                    matches = catalog.GetMatches(mol)
                    for match in matches:
                        flags.append(f"{label}:{match.GetDescription()}")
                properties = _properties(mol)
                flags.extend(_window_hits(properties, windows))
                if dedupe != "none":
                    key = _dedupe_key(mol, item.cleaned.smiles, dedupe)
                    first = seen.get(key)
                    if first is not None:
                        flags.append(f"重复:{first}")
                    else:
                        seen[key] = item.id
                if flags:
                    rule = flags[0].split(":", 1)[0].split(">", 1)[0].split("<", 1)[0]
                    dropped[rule] = dropped.get(rule, 0) + 1
                    examples.append({"id": item.id, "smiles": item.cleaned.smiles, "flags": flags})
                    if mode == "drop":
                        continue
                records.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "smiles": item.cleaned.smiles,
                        "input_smiles": item.cleaned.input_smiles,
                        "heavy_atoms": item.cleaned.heavy_atoms,
                        "source_index": item.source_index,
                        "flags": flags,
                    }
                )

        if not records:
            detail = "、".join(f"{name} {count} 条" for name, count in dropped.items() if count)
            return ToolResult(
                output=(
                    f"全部 {len(items)} 条都被淘汰了（{detail}）。"
                    "换一组判据，或者先用 mode=flag 看一眼会删掉什么。"
                ),
                failed=True,
            )

        manifest = {
            "kind": "ligandset",
            "stage": "curated",
            "tool": "curate_ligands",
            "source": {"handle": str(entry.handle), "format": source_kind},
            "params": params,
            "counts": {
                "input": len(items),
                "kept": len(records),
                "dropped": sum(dropped.values()),
            },
            "dropped": dropped,
            "columns": list(CURATED_COLUMNS),
        }
        try:
            products.write_text_atomic(target / CSV_FILE, _curated_csv_text(records))
            products.write_json_atomic(target / MANIFEST_FILE, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / products.PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写筛选产物失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="ligandset", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("stage") or "") == "curated":
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "ligandset",
                path=target,
                summary=f"筛过的配体集 · {len(records)} 个（{stem}）",
                fingerprint=fingerprint,
                parents=[entry.handle],
                meta={
                    "kind": "ligandset",
                    "stage": "curated",
                    "tool": "curate_ligands",
                    "source": {"handle": str(entry.handle), "format": source_kind},
                    "params": params,
                    "counts": {
                        "input": len(items),
                        "kept": len(records),
                        "dropped": sum(dropped.values()),
                        "dropped_by_rule": dropped,
                    },
                    "file": CSV_FILE,
                    "manifest": MANIFEST_FILE,
                },
                # 配体库跨靶点：换受体时不该被搁置。
                link_current_receptor=False,
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_curated(
                handle=handle,
                reused=False,
                kept=len(records),
                dropped=dropped,
                examples=examples,
                total=len(items),
                mode=mode,
                target=target,
            ),
            meta={
                "handle": handle,
                "kept": len(records),
                "dropped": sum(dropped.values()),
                "reused": False,
            },
        )


# ---------------------------------------------------------------------------
# 参考活性集：从已知活性里挑出「像药」的那一批
# ---------------------------------------------------------------------------
REFERENCE_DIR = "reference"
MIN_REFERENCE_SIZE = 10
MAX_REFERENCE_KEEP = 5000


def reference_ligandset_dir() -> Path:
    """参考集的根。测试替换它，避免写用户家目录。"""
    return ligands_dir() / REFERENCE_DIR


def _activities_of(entry: Any) -> list[RawLigand]:
    """读活性来源：`search_actives` 落的 CSV（`path` 是文件）或配体集目录。

    **不经过 `load_ligand_file`**：那个函数按 canonical SMILES 去重时是「先到先得」，
    而参考集要的是**同一分子取最强的那条**。先去重再想取最大值已经晚了。
    """
    path = Path(str(entry.path))
    if path.is_dir():
        path = path / CSV_FILE
    if not path.is_file():
        raise LigandError(f"活性来源的产物不在：{path}")
    try:
        text = _decode_text(path.read_bytes())
    except OSError as error:
        raise LigandError(f"读活性来源失败（{type(error).__name__}：{error}）") from error
    return _read_csv(text, smiles_column="")


def _render_reference(
    *,
    handle: str,
    reused: bool,
    kept: int,
    pchembl_min: float | None,
    pchembl_max: float | None,
    dropped: dict[str, int],
    flagged: int,
    too_small: bool,
    target: Path,
) -> str:
    lines = [
        f"参考活性集已生成，登记为 {handle}。" if not reused
        else f"登记表里已有同一份：{handle}（未重做）。",
        f"保留 {kept} 条"
        + (
            f"，最强 pChEMBL {pchembl_max:.2f}、最弱 {pchembl_min:.2f}"
            if pchembl_min is not None and pchembl_max is not None
            else ""
        )
        + "。",
    ]
    if dropped:
        detail = "、".join(f"{name} {count} 条" for name, count in dropped.items() if count)
        if detail:
            lines.append(f"没进来：{detail}。")
    if flagged:
        lines.append(
            f"其中 {flagged} 条命中 PAINS，**只标不删**：已知活性是真的活性，"
            "删掉会把参考分布打歪（要不要排除由下游决定）。"
        )
    if too_small:
        lines.append(
            f"只有 {kept} 条（少于 {MIN_REFERENCE_SIZE}），"
            "当参考分布用会不稳——放宽 min_pchembl 或换一个靶点。"
        )
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：screen_similarity(query={handle}, library=...) 拿它当查询集。")
    return "\n".join(lines)


class BuildReferenceSetTool(Tool):
    """从已知活性里挑出一批当参考集（可按活性下限截断）。"""

    spec = ToolSpec(
        name="build_reference_set",
        description=(
            "把 `search_actives` 的产物变成**参考活性集**：按 pChEMBL 下限过滤、"
            "同一分子取最强的那条、按活性排序后截断。"
            + CHEM_TOOL_DESCRIPTION_NOTE
            + "\n"
            "产物是一个配体集（`stage=reference`、`label=active`），可直接当 "
            "`screen_similarity` 的查询集。PAINS 命中的**只标不删**——已知活性是真的活性，"
            "删掉会把参考分布打歪。"
            "\n少于 10 条时会标 `too_small`：那样的参考分布不稳，先看一眼再拿去分档。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "actives": {
                    "type": "string",
                    "description": "search_actives 的产物 handle（也接受 import_ligandset 的目录）",
                },
                "min_pchembl": {
                    "type": "number",
                    "description": "活性下限，默认 6（≈1 μM）；越大越严",
                },
                "max": {
                    "type": "integer",
                    "description": f"最多保留多少条，默认 500，上限 {MAX_REFERENCE_KEEP}",
                },
                "pains": {
                    "type": "boolean",
                    "description": "是否标注 PAINS，默认 true（只标不删）",
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
                output="内核装配问题：build_reference_set 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw = str(arguments.get("actives") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 actives。先 search_actives 拿到已知活性。", failed=True
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, LigandSetHandle, "actives")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        try:
            floor = _optional_number(arguments, "min_pchembl", -20.0, 20.0)
            keep_max = _optional_int(arguments, "max", 1, MAX_REFERENCE_KEEP)
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        floor = 6.0 if floor is None else floor
        keep_max = 500 if keep_max is None else keep_max
        want_pains = arguments.get("pains")
        if want_pains is not None and not isinstance(want_pains, bool):
            return ToolResult(output="参数 pains 应当是 true / false", failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            rows = _activities_of(entry)
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        if not rows:
            return ToolResult(output=f"{entry.handle} 里一条记录都没有。", failed=True)

        dropped = {"低于活性下限": 0, "没有活性值": 0, "结构解析失败": 0}
        best: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.pchembl_value is None:
                dropped["没有活性值"] += 1
                continue
            if row.pchembl_value < floor:
                dropped["低于活性下限"] += 1
                continue
            try:
                cleaned = canonicalize_smiles(row.smiles)
            except LigandError:
                dropped["结构解析失败"] += 1
                continue
            current = best.get(cleaned.smiles)
            if current is None or row.pchembl_value > current["pchembl_value"]:
                best[cleaned.smiles] = {
                    "id": row.id,
                    "name": row.name,
                    "smiles": cleaned.smiles,
                    "input_smiles": cleaned.input_smiles,
                    "heavy_atoms": cleaned.heavy_atoms,
                    "source_index": row.index,
                    "pchembl_value": float(row.pchembl_value),
                }
        if not best:
            detail = "、".join(f"{name} {count} 条" for name, count in dropped.items() if count)
            return ToolResult(
                output=(
                    f"按 min_pchembl={floor:g} 筛完一条都不剩（{detail}）。"
                    "放宽下限再试；不要因为空的参考集就说这个靶点没有活性分子。"
                ),
                failed=True,
            )

        kept = sorted(best.values(), key=lambda item: (-item["pchembl_value"], item["id"]))
        kept = kept[:keep_max]
        flagged = 0
        if want_pains is not False:
            from rdkit import Chem, rdBase

            catalog = _filter_catalog(["PAINS_A", "PAINS_B", "PAINS_C"])
            with rdBase.BlockLogs():
                for item in kept:
                    mol = Chem.MolFromSmiles(item["smiles"])
                    hits = [
                        match.GetDescription() for match in catalog.GetMatches(mol)
                    ] if mol is not None else []
                    item["flags"] = [f"PAINS:{name}" for name in hits]
                    if hits:
                        flagged += 1
        else:
            for item in kept:
                item["flags"] = []

        too_small = len(kept) < MIN_REFERENCE_SIZE
        params = {
            "actives": str(entry.handle),
            "min_pchembl": floor,
            "max": keep_max,
            "pains": want_pains is not False,
        }
        stem = str(arguments.get("name") or entry.meta.get("accession") or "reference")
        try:
            target, reused = products.content_dir(
                reference_ligandset_dir(),
                stem,
                params,
                (MANIFEST_FILE, CSV_FILE, products.PARAMS_FILE),
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        if reused:
            existing = ledger.find(
                kind="ligandset", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("stage") or "") == "reference":
                    counts = dict((candidate.meta or {}).get("counts") or {})
                    return ToolResult(
                        output=_render_reference(
                            handle=str(candidate.handle),
                            reused=True,
                            kept=int(counts.get("kept") or 0),
                            pchembl_min=counts.get("pchembl_min"),
                            pchembl_max=counts.get("pchembl_max"),
                            dropped={},
                            flagged=int(counts.get("pains") or 0),
                            too_small=bool((candidate.meta or {}).get("too_small")),
                            target=target,
                        ),
                        meta={"handle": str(candidate.handle), "kept": counts.get("kept", 0),
                              "reused": True},
                    )

        manifest = {
            "kind": "ligandset",
            "stage": "reference",
            "label": "active",
            "tool": "build_reference_set",
            "source": {"handle": str(entry.handle)},
            "params": params,
            "counts": {
                "input": len(rows),
                "kept": len(kept),
                "dropped": sum(dropped.values()),
                "pains": flagged,
                "pchembl_min": min(item["pchembl_value"] for item in kept),
                "pchembl_max": max(item["pchembl_value"] for item in kept),
            },
            "too_small": too_small,
            "columns": list(CURATED_COLUMNS),
        }
        try:
            products.write_text_atomic(target / CSV_FILE, _curated_csv_text(kept))
            products.write_json_atomic(target / MANIFEST_FILE, manifest)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / products.PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(output=f"写参考集失败：{error}", failed=True)

        fingerprint = fingerprint_path(target)
        handle = ""
        existing = ledger.find(kind="ligandset", fingerprint=fingerprint, include_archived=True)
        for candidate in existing:
            if str((candidate.meta or {}).get("stage") or "") == "reference":
                handle = str(candidate.handle)
                break
        if not handle:
            registered = ledger.add(
                "ligandset",
                path=target,
                summary=f"参考活性集 · {len(kept)} 条 · pChEMBL ≥ {floor:g}",
                fingerprint=fingerprint,
                parents=[entry.handle],
                meta={
                    "kind": "ligandset",
                    "stage": "reference",
                    "label": "active",
                    "tool": "build_reference_set",
                    "source": {"handle": str(entry.handle)},
                    "params": params,
                    "counts": dict(manifest["counts"]),
                    "too_small": too_small,
                    "file": CSV_FILE,
                    "manifest": MANIFEST_FILE,
                },
                # 参考集跨靶点：换受体时不该被搁置。
                link_current_receptor=False,
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_reference(
                handle=handle,
                reused=False,
                kept=len(kept),
                pchembl_min=min(item["pchembl_value"] for item in kept),
                pchembl_max=max(item["pchembl_value"] for item in kept),
                dropped=dropped,
                flagged=flagged,
                too_small=too_small,
                target=target,
            ),
            meta={"handle": handle, "kept": len(kept), "too_small": too_small,
                  "pains": flagged, "reused": False},
        )



# ---------------------------------------------------------------------------
# 叠合：把一组分子放进同一个参考系
# ---------------------------------------------------------------------------
ALIGNED_DIR = "aligned"
ALIGNED_SDF = "aligned.sdf"
DEFAULT_ALIGN_RMSD = 2.5
MIN_ALIGN_ATOMS = 6


def aligned_ligandset_dir() -> Path:
    """叠合产物的根。测试替换它，避免写用户家目录。"""
    return ligands_dir() / ALIGNED_DIR


def embed_smiles(smiles: str, *, seed: int = DEFAULT_SEED) -> tuple[Any, str]:
    """从 SMILES 生成一个 3D 构象，返回 `(分子, 失败原因)`。种子固定，两次跑同一套坐标。"""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, "SMILES 解析失败"
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        return None, "生成 3D 构象失败"
    return Chem.RemoveHs(mol), ""


def align_to_reference(
    reference: Any,
    mol: Any,
    *,
    min_atoms: int = MIN_ALIGN_ATOMS,
    max_rmsd: float = DEFAULT_ALIGN_RMSD,
) -> tuple[Any, float, str]:
    """按最大公共子结构把 `mol` 叠到 `reference` 上（原地改构象）。

    返回 `(分子, rmsd, 失败原因)`。失败时不抛——叠不上是很正常的事（不是同一个系列），
    调用方要逐条记下来而不是整批失败。
    """
    from rdkit import Chem, rdBase
    from rdkit.Chem import rdFMCS, rdMolAlign

    with rdBase.BlockLogs():
        try:
            mcs = rdFMCS.FindMCS(
                [reference, mol], timeout=5, ringMatchesRingOnly=True, completeRingsOnly=True
            )
        except Exception as error:
            return mol, 0.0, f"MCS 搜索失败（{type(error).__name__}）"
    if mcs.numAtoms < min_atoms:
        return mol, 0.0, f"与参考分子的公共骨架只有 {mcs.numAtoms} 个原子"
    pattern = Chem.MolFromSmarts(mcs.smartsString)
    if pattern is None:
        return mol, 0.0, "MCS 的 SMARTS 解析不了"
    probe = mol.GetSubstructMatch(pattern)
    target = reference.GetSubstructMatch(pattern)
    if not probe or not target:
        return mol, 0.0, "公共骨架在这两个分子里都对不上"
    try:
        rmsd = float(
            rdMolAlign.AlignMol(mol, reference, atomMap=list(zip(probe, target)))
        )
    except Exception as error:
        return mol, 0.0, f"叠合失败（{type(error).__name__}）"
    if rmsd > max_rmsd:
        return mol, rmsd, f"叠合后 RMSD {rmsd:.2f} Å 超过 {max_rmsd:g} Å"
    return mol, rmsd, ""


def _render_aligned(
    *, handle: str, kept: int, dropped: Sequence[dict[str, str]], mean_rmsd: float | None,
    target: Path,
) -> str:
    lines = [f"叠合完成，登记为 {handle}。"]
    lines.append(
        f"{kept} 个分子叠到同一个参考系里"
        + (f"，平均 RMSD {mean_rmsd:.2f} Å。" if mean_rmsd is not None else "。")
    )
    if dropped:
        lines.append(f"{len(dropped)} 个没叠上（不是同一个系列）：")
        for item in dropped[:3]:
            lines.append(f"  · {item['id']}：{item['reason']}")
    lines.append(f"产物目录：{target}")
    lines.append(f"下一步：build_pharmacophore(actives={handle}) 直接用这份坐标，不再重新叠合。")
    return "\n".join(lines)


class AlignLigandsTool(Tool):
    """把一组配体叠到同一个参考系里。"""

    spec = ToolSpec(
        name="align_ligands",
        description=(
            "把一组配体**叠到同一个参考系**：每个分子生成 3D 构象，再按最大公共子结构"
            "（MCS）叠到参考分子上。" + CHEM_TOOL_DESCRIPTION_NOTE + "\n"
            "叠不上的逐条列出原因（公共骨架太小、RMSD 超阈值），不静默丢掉——"
            "它们本来就不是同一个系列，硬叠出来的药效团没有意义。\n"
            "产物是一份 `stage=aligned` 的配体集（`aligned.sdf` + `ligands.csv`），"
            "`build_pharmacophore` 认这个阶段，直接读坐标、不再重复叠合。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ligands": {"type": "string", "description": "待叠合的配体集 handle"},
                "reference": {
                    "type": "string",
                    "description": "可选：用哪个分子当参考（另一个配体集 handle）；默认用第一个能叠的分子",
                },
                "rmsd_cutoff": {
                    "type": "number",
                    "description": f"叠合后 RMSD 上限（埃），默认 {DEFAULT_ALIGN_RMSD:g}",
                },
                "min_atoms": {
                    "type": "integer",
                    "description": f"公共骨架至少多少个原子，默认 {MIN_ALIGN_ATOMS}",
                },
                "name": {"type": "string", "description": "显示名，只影响产物目录名"},
            },
            "required": ["ligands"],
            "additionalProperties": False,
        },
    )
    primary_argument = "ligands"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：align_ligands 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        raw = str(arguments.get("ligands") or "").strip()
        if not raw:
            return ToolResult(output="缺少参数 ligands。", failed=True)
        try:
            entry = ledger.get(raw)
            expect_handle(entry, LigandSetHandle, "ligands")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        cutoff = arguments.get("rmsd_cutoff", DEFAULT_ALIGN_RMSD)
        min_atoms = arguments.get("min_atoms", MIN_ALIGN_ATOMS)
        if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not 0.1 <= cutoff <= 10:
            return ToolResult(output="参数 rmsd_cutoff 要在 0.1 到 10 之间", failed=True)
        if isinstance(min_atoms, bool) or not isinstance(min_atoms, int) or not 3 <= min_atoms <= 50:
            return ToolResult(output="参数 min_atoms 要在 3 到 50 之间", failed=True)

        try:
            require_chem()
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            rows = _source_cleaned(entry)[0]
        except LigandError as error:
            return ToolResult(output=str(error), failed=True)
        if len(rows) < 2:
            return ToolResult(output="少于两个分子，没有可叠合的对象。", failed=True)

        reference_mol = None
        raw_reference = str(arguments.get("reference") or "").strip()
        if raw_reference:
            try:
                reference_entry = ledger.get(raw_reference)
                expect_handle(reference_entry, LigandSetHandle, "reference")
                reference_rows = _source_cleaned(reference_entry)[0]
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            if not reference_rows:
                return ToolResult(output="参考配体集里没有分子。", failed=True)
            reference_mol, reason = embed_smiles(reference_rows[0].cleaned.smiles)
            if reference_mol is None:
                return ToolResult(output=f"参考分子生成 3D 失败：{reason}", failed=True)

        from rdkit import Chem

        aligned: list[dict[str, Any]] = []
        dropped: list[dict[str, str]] = []
        rmsds: list[float] = []
        for item in rows:
            mol, reason = embed_smiles(item.cleaned.smiles)
            if mol is None:
                dropped.append({"id": item.id, "reason": reason})
                continue
            if reference_mol is None:
                reference_mol = mol
                aligned.append({"id": item.id, "name": item.name, "mol": mol, "rmsd": 0.0,
                                "smiles": item.cleaned.smiles,
                                "input_smiles": item.cleaned.input_smiles,
                                "heavy_atoms": item.cleaned.heavy_atoms,
                                "source_index": item.source_index})
                continue
            mol, rmsd, why = align_to_reference(
                reference_mol, mol, min_atoms=min_atoms, max_rmsd=float(cutoff)
            )
            if why:
                dropped.append({"id": item.id, "reason": why})
                continue
            rmsds.append(rmsd)
            aligned.append({"id": item.id, "name": item.name, "mol": mol, "rmsd": rmsd,
                            "smiles": item.cleaned.smiles,
                            "input_smiles": item.cleaned.input_smiles,
                            "heavy_atoms": item.cleaned.heavy_atoms,
                            "source_index": item.source_index})
        if len(aligned) < 2:
            return ToolResult(
                output=(
                    f"只有 {len(aligned)} 个分子叠上了（至少要有 2 个）。"
                    + "".join(f"\n  · {item['id']}：{item['reason']}" for item in dropped[:5])
                ),
                failed=True,
            )

        params = {
            "source": str(entry.handle),
            "reference": raw_reference,
            "rmsd_cutoff": float(cutoff),
            "min_atoms": min_atoms,
        }
        stem = str(arguments.get("name") or "aligned")
        try:
            target, reused = products.content_dir(
                aligned_ligandset_dir(), stem, params,
                (MANIFEST_FILE, CSV_FILE, ALIGNED_SDF, PARAMS_FILE),
            )
        except OSError as error:
            return ToolResult(output=f"建产物目录失败：{error}", failed=True)

        handle = ""
        if reused:
            existing = ledger.find(
                kind="ligandset", fingerprint=fingerprint_path(target), include_archived=True
            )
            for candidate in existing:
                if str((candidate.meta or {}).get("stage") or "") == "aligned":
                    handle = str(candidate.handle)
                    break
        mean_rmsd = sum(rmsds) / len(rmsds) if rmsds else None
        if not handle:
            manifest = {
                "kind": "ligandset",
                "stage": "aligned",
                "tool": "align_ligands",
                "source": {"handle": str(entry.handle)},
                "reference": raw_reference,
                "params": params,
                "counts": {"input": len(rows), "aligned": len(aligned),
                           "dropped": len(dropped)},
                "mean_rmsd": None if mean_rmsd is None else round(mean_rmsd, 3),
                "results": [
                    {"id": item["id"], "rmsd": round(item["rmsd"], 3),
                     "atoms": item["heavy_atoms"]}
                    for item in aligned
                ],
                "dropped": dropped,
                "file": ALIGNED_SDF,
            }
            try:
                # content_dir 只建了根目录：SDWriter 不会替我们建叶子目录
                target.mkdir(parents=True, exist_ok=True)
                writer = Chem.SDWriter(str(target / ALIGNED_SDF))
                try:
                    for item in aligned:
                        item["mol"].SetProp("_Name", item["id"])
                        writer.write(item["mol"])
                finally:
                    writer.close()
                # 多一列 align_rmsd：`_csv_text` 的列是固定的（CSV_COLUMNS），这里自己写，
                # 否则那一列会被静默丢掉——而它正是「这个分子叠得好不好」的唯一记录。
                columns = list(CSV_COLUMNS) + ["align_rmsd"]
                buffer = io.StringIO()
                writer_csv = csv.DictWriter(
                    buffer, fieldnames=columns, lineterminator="\n", extrasaction="ignore"
                )
                writer_csv.writeheader()
                for item in aligned:
                    writer_csv.writerow(
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "smiles": item["smiles"],
                            "input_smiles": item["input_smiles"],
                            "heavy_atoms": item["heavy_atoms"],
                            "source_index": item["source_index"],
                            "align_rmsd": round(item["rmsd"], 3),
                        }
                    )
                products.write_text_atomic(target / CSV_FILE, buffer.getvalue())
                products.write_json_atomic(target / MANIFEST_FILE, manifest)
                products.write_json_atomic(target / PARAMS_FILE, params)
            except OSError as error:
                return ToolResult(output=f"写产物失败：{error}", failed=True)
            registered = ledger.add(
                "ligandset",
                path=target,
                summary=f"叠合后的配体集 · {len(aligned)} 个（{stem}）",
                fingerprint=fingerprint_path(target),
                parents=[entry.handle],
                meta={
                    "kind": "ligandset",
                    "stage": "aligned",
                    "tool": "align_ligands",
                    "counts": manifest["counts"],
                    "mean_rmsd": manifest["mean_rmsd"],
                    "sdf": ALIGNED_SDF,
                    "file": CSV_FILE,
                    "manifest": MANIFEST_FILE,
                },
                link_current_receptor=False,
            )
            handle = str(registered.handle)

        return ToolResult(
            output=_render_aligned(
                handle=handle, kept=len(aligned), dropped=dropped,
                mean_rmsd=None if mean_rmsd is None else round(mean_rmsd, 3),
                target=target,
            ),
            meta={"handle": handle, "count": len(aligned), "dropped": len(dropped),
                  "reused": bool(handle) and reused},
        )


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [
        ImportLigandsetTool(),
        PrepareLigandsTool(),
        CurateLigandsTool(),
        BuildReferenceSetTool(),
        AlignLigandsTool(),
    ]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
