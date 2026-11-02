"""把一份位姿的接触、氢键与 PLIP 指纹整理成网页要的形状。

`analyze_pose` 与 `write_run` 共用这里的计算：前者只读、把结果写进返回文本；
后者把同一份结果写进 `meta.json`。两处各写一份的话，用户看到的页面与聊天里的
距离、残基、作用类型会慢慢对不上。

**端点原子在写入时确定。** 页面只拿到 `StructureElement.Schema`，不再读坐标：
几何氢键用 `_hbonds` 的供体/受体原子；PLIP 各类用行里的残基，取该残基与位姿
最近的一对重原子；π 堆积与阳离子-π 取环上的代表原子。距离与角度照原计算写，
三维线只负责指出方向。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from . import pdbtext
from .analysis import (
    CONTACT_CUTOFF,
    plip_fingerprint,
    pose_clashes,
    pose_contacts,
    pose_hbonds,
)

MOL_KIND = {
    "saltbridge": ("ionic", "盐桥"),
    "hbond": ("hydrogen-bond", "氢键"),
    "pistacking": ("pi-stacking", "π 堆积"),
    "pication": ("cation-pi", "π-阳离子"),
    "hydrophobic": ("hydrophobic", "疏水"),
    "halogen": ("halogen-bond", "卤键"),
    "waterbridge": ("water-bridge", "水桥"),
    "metal": ("metal-coordination", "金属配位"),
}

KIND_ORDER = {key: index for index, key in enumerate(MOL_KIND.values())}


@dataclass(frozen=True)
class PoseInteractions:
    contacts: tuple[dict[str, Any], ...]
    interactions: tuple[dict[str, Any], ...]
    clashes: int
    plip_ok: bool
    plip_reason: str


def _schema(atom: pdbtext.AtomRecord, *, with_chain: bool) -> dict[str, Any] | None:
    """一个原子的 `StructureElement.Schema`。残基号不是数字时返回 None。"""
    try:
        seq = int(str(atom.number).strip())
    except (TypeError, ValueError):
        return None
    item: dict[str, Any] = {
        "auth_seq_id": seq,
        "auth_comp_id": atom.resname,
        "auth_atom_id": atom.name.strip(),
    }
    if with_chain and atom.chain.strip():
        item["auth_asym_id"] = atom.chain
        item["label_asym_id"] = atom.chain
    if atom.icode.strip():
        item["pdbx_PDB_ins_code"] = atom.icode
    return item


def _protein_atoms(label: str, by_label: dict[str, list[pdbtext.AtomRecord]],
                   all_atoms: Sequence[pdbtext.AtomRecord]) -> list[pdbtext.AtomRecord]:
    exact = by_label.get(label)
    if exact:
        return exact
    parts = label.split(":")
    if len(parts) != 3:
        return []
    chain, number, _resname = parts
    if chain == "·":
        chain = ""
    return [
        atom for atom in all_atoms
        if atom.chain == chain and atom.number == number
    ]


def _find_atom(atoms: Sequence[pdbtext.AtomRecord], name: str) -> pdbtext.AtomRecord | None:
    wanted = name.strip().upper()
    for atom in atoms:
        if atom.name.strip().upper() == wanted:
            return atom
    return None


def _nearest(
    protein_atoms: Sequence[pdbtext.AtomRecord],
    ligand_atoms: Sequence[pdbtext.AtomRecord],
) -> tuple[pdbtext.AtomRecord, pdbtext.AtomRecord] | None:
    best: tuple[float, pdbtext.AtomRecord, pdbtext.AtomRecord] | None = None
    for protein in protein_atoms:
        for ligand in ligand_atoms:
            distance = math.dist(protein.coord, ligand.coord)
            if best is None or distance < best[0]:
                best = (distance, protein, ligand)
    if best is None:
        return None
    return best[1], best[2]


def _record(
    *,
    kind: str,
    source: str,
    protein: pdbtext.AtomRecord,
    ligand: pdbtext.AtomRecord,
    distance: float,
    angle: float | None = None,
    detail: str = "",
    count: int = 1,
) -> dict[str, Any] | None:
    mol_kind, label = MOL_KIND[kind]
    a = _schema(protein, with_chain=True)
    b = _schema(ligand, with_chain=False)
    if a is None or b is None:
        return None
    item: dict[str, Any] = {
        "kind": mol_kind,
        "label": label,
        "source": source,
        "distance": round(float(distance), 2),
        "residue": protein.label,
        "resname": protein.resname,
        "ligand_atom": ligand.name.strip(),
        "a": a,
        "b": b,
    }
    if angle is not None and math.isfinite(float(angle)):
        item["angle"] = round(float(angle), 1)
    if detail:
        item["detail"] = detail
    if count > 1:
        item["count"] = int(count)
    return item


def compute(
    receptor_text: str,
    pose_text: str,
    *,
    cutoff: float = CONTACT_CUTOFF,
    want_hbond: bool = True,
    want_plip: bool = True,
) -> PoseInteractions:
    """算一份位姿的接触、氢键、冲突与 PLIP 指纹。"""
    receptor_atoms = tuple(
        atom for atom in pdbtext.parse(receptor_text).atoms
        if not atom.is_hetero and pdbtext.is_heavy(atom)
    )
    ligand_atoms = tuple(
        atom for atom in pdbtext.parse(pose_text).atoms if pdbtext.is_heavy(atom)
    )
    if not ligand_atoms:
        return PoseInteractions((), (), 0, False, "位姿里没有重原子")

    contacts = tuple(
        {
            "residue": item.label,
            "resname": item.resname,
            "distance": round(float(item.distance), 2),
            "atoms": int(item.atoms),
        }
        for item in pose_contacts(receptor_atoms, ligand_atoms, cutoff)
    )
    clashes = len(pose_clashes(receptor_atoms, ligand_atoms))
    hbonds = pose_hbonds(receptor_atoms, ligand_atoms) if want_hbond else []

    fingerprint: dict[str, list[Any]] = {}
    reason = ""
    if want_plip:
        fingerprint, reason = plip_fingerprint(receptor_text, pose_text)

    by_label: dict[str, list[pdbtext.AtomRecord]] = {}
    for atom in receptor_atoms:
        by_label.setdefault(atom.label, []).append(atom)
    ligand_by_name = {atom.name.strip().upper(): atom for atom in ligand_atoms}

    records: dict[tuple[str, str, str], dict[str, Any]] = {}

    for bond in hbonds:
        if bond.donor_label == "配体":
            protein_label = bond.acceptor_label
            protein_name = bond.acceptor_atom
            ligand_name = bond.donor_atom
        else:
            protein_label = bond.donor_label
            protein_name = bond.donor_atom
            ligand_name = bond.acceptor_atom
        protein_atoms = _protein_atoms(protein_label, by_label, receptor_atoms)
        protein = _find_atom(protein_atoms, protein_name)
        ligand = ligand_by_name.get(ligand_name.strip().upper())
        if protein is None or ligand is None:
            pair = _nearest(protein_atoms, ligand_atoms)
            if pair is None:
                continue
            protein, ligand = pair
        item = _record(
            kind="hbond",
            source="geometry",
            protein=protein,
            ligand=ligand,
            distance=bond.distance,
            angle=bond.angle,
        )
        if item is not None:
            records[(item["kind"], item["residue"], item["ligand_atom"])] = item

    for kind, hits in fingerprint.items():
        if kind not in MOL_KIND:
            continue
        for hit in hits:
            protein_atoms = _protein_atoms(hit.residue, by_label, receptor_atoms)
            pair = _nearest(protein_atoms, ligand_atoms)
            if pair is None:
                continue
            protein, ligand = pair
            item = _record(
                kind=kind,
                source="plip",
                protein=protein,
                ligand=ligand,
                distance=hit.distance,
                detail=hit.detail,
                count=hit.count,
            )
            if item is not None:
                records[(item["kind"], item["residue"], item["ligand_atom"])] = item

    ordered = sorted(
        records.values(),
        key=lambda item: (
            KIND_ORDER.get(item["kind"], 99),
            float(item["distance"]),
            item["residue"],
        ),
    )
    return PoseInteractions(
        contacts=contacts,
        interactions=tuple(ordered),
        clashes=clashes,
        plip_ok=bool(fingerprint) and not reason,
        plip_reason=reason,
    )
