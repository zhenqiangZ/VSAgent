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
        raise LigandError(f"找不到这个文件，或它不是普通文件：{source}")

    suffix = source.suffix.lower()
    if suffix not in FORMAT_BY_SUFFIX:
        raise LigandError(
            f"不支持的配体文件格式：{suffix or '（没有扩展名）'}；"
            "支持 .sdf / .sd / .smi / .csv"
        )

    payload = source.read_bytes()
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
    items: Sequence[PrepItem], *, limit: int
) -> tuple[list[PrepItem], dict[str, int]]:
    """按 canonical SMILES 去重，再按 `limit` 截断（去重后名额才算数）。"""
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
    kept = unique[:limit]
    remaining = len(unique) - len(kept)
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
            f"另有 {counts['remaining']} 个有效分子没有处理；调大 limit 再跑一次会生成"
            "另一组参数的新目录，不覆盖本次结果。"
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
            f"另有 {counts['remaining']} 个有效分子没有处理；调大 limit 再跑一次会生成"
            "另一组参数的新目录，不覆盖本次结果。"
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
                    "description": "最多准备多少个（去重后按源顺序取），默认 200，上限 2000",
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

        kept, counts = _unique_items(items, limit=limit)
        counts["selected"] = len(kept)
        if not kept:
            return ToolResult(output="去重之后没有可准备的分子。", failed=True)

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


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [ImportLigandsetTool(), PrepareLigandsTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
