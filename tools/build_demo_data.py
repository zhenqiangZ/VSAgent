"""把 1IEP 的原始结构整理成前端要的演示数据。

前端只认 runs/<run_id>/ 下的 meta.json 与结构文件。这份脚本做四件事：
写出去掉水分子的受体 PDB、抽取共晶配体、以共晶配体为真值算出真值口袋、
合成若干干扰口袋与若干候选分子位姿。

脚本的输入是 RCSB 下载的原始文件，输出是同一目录下的演示数据，可重复运行。
"""

import argparse
import json
import math
import random
from pathlib import Path

DEFAULT_RUN_DIR = Path(__file__).resolve().parent.parent / "runs" / "1iep-demo"

POCKET_RADIUS = 5.0
PERTURB_SCALE = 0.35
TARGET_CHAIN = "A"
LIGAND_RESNAME = "STI"

random.seed(20260930)


def parse_pdb(path):
    """读出 PDB 里的原子、残基与配体。

    ATOM 行是受体，HETATM 里的 STI 是共晶配体，HOH 与水分子一并丢掉。
    """
    atoms = []
    ligands = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = line[:6].strip()
        if record not in ("ATOM", "HETATM"):
            continue
        name = line[12:16].strip()
        resname = line[17:20].strip()
        chain = line[21].strip()
        resseq = int(line[22:26])
        element = line[76:78].strip() or name[0]
        coord = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        if resname == "HOH":
            continue
        item = {
            "name": name,
            "resname": resname,
            "chain": chain,
            "resseq": resseq,
            "element": element,
            "coord": coord,
            "line": line,
        }
        if record == "ATOM":
            atoms.append(item)
        else:
            ligands.append(item)
    return atoms, ligands


def write_pdb(path, entries):
    """把留下的原子按原样写回去。"""
    path.write_text("".join(entry["line"] + "\n" for entry in entries), encoding="utf-8")


def write_ligand_pdb(path, entries, coords_override=None):
    """把配体写成一份独立的 PDB 文件。

    坐标、原子名与元素都取自晶体结构本身。各字段的位置按 PDB 的固定列写，
    3Dmol 的解析器按列读，错位会读出不完整的原子表。
    """
    lines = []
    for index, entry in enumerate(entries):
        coord = coords_override[index] if coords_override is not None else entry["coord"]
        element = entry["element"]
        lines.append(
            f"HETATM{index + 1:5d} {entry['name']:<4s} {entry['resname']:>3s} "
            f"{entry['chain']}{entry['resseq']:4d}    "
            f"{coord[0]:8.3f}{coord[1]:8.3f}{coord[2]:8.3f}"
            f"{1.00:6.2f}{0.00:6.2f}          {element:>2s}  \n"
        )
    lines.append("END\n")
    path.write_text("".join(lines), encoding="utf-8")


def ligand_center(entries):
    """一串原子的几何中心。"""
    count = len(entries)
    return [
        sum(entry["coord"][axis] for entry in entries) / count
        for axis in range(3)
    ]


def pocket_residues(atoms, ligand_points, radius, chain):
    """找出与配体任一个原子距离在radius以内的残基。

    判据是残基里任一个原子进入配体任一个原子的radius范围内，而不是进入
    配体几何中心的范围内。伊马替尼这类长条形分子，中心到两端超过 10 埃，
    按中心算出来的口袋只有分子中段那几个残基。
    """
    found = {}
    for atom in atoms:
        if atom["chain"] != chain:
            continue
        distance = min(math.dist(atom["coord"], point) for point in ligand_points)
        if distance <= radius:
            key = (atom["chain"], atom["resseq"], atom["resname"])
            found[key] = min(found.get(key, distance), distance)
    ordered = sorted(found.items(), key=lambda item: item[1])
    return [{"chain": key[0], "resseq": key[1], "resname": key[2], "min_distance": round(value, 2)}
            for key, value in ordered]


def residues_in_sphere(atoms, center, radius, chain):
    """两个球心之间夹一个球，用于造干扰口袋。"""
    return pocket_residues(atoms, center, radius, chain)


def cluster_centers(atoms, count, exclude, min_separation):
    """在受体上挑若干个互不相邻、且离真值口袋足够远的位置。

    做法是把每个残基的 CA 原子当成候选点，随机取样后检查间隔。
    """
    candidates = [atom for atom in atoms if atom["name"] == "CA" and atom["chain"] == TARGET_CHAIN]
    chosen = []
    attempts = 0
    while len(chosen) < count and attempts < 20000:
        attempts += 1
        pick = random.choice(candidates)
        point = pick["coord"]
        if min(math.dist(point, other) for other in exclude) < min_separation:
            continue
        if any(math.dist(point, other) < min_separation for other in chosen):
            continue
        chosen.append(point)
    if len(chosen) < count:
        raise RuntimeError(f"只找到 {len(chosen)} 个干扰口袋位置，需要 {count} 个")
    return chosen


def perturb(coords, scale):
    """给每个原子加一个小的随机位移，模拟不同的对接位姿。"""
    result = []
    for coord in coords:
        offset = [random.gauss(0.0, scale) for _ in range(3)]
        result.append([coord[0] + offset[0], coord[1] + offset[1], coord[2] + offset[2]])
    return result


def main(run_dir: Path, session_id: str | None) -> None:
    (run_dir / "ligands").mkdir(parents=True, exist_ok=True)
    atoms, ligand_atoms = parse_pdb(run_dir / "receptor_full.pdb")
    crystal = [
        entry for entry in ligand_atoms
        if entry["chain"] == TARGET_CHAIN and entry["resname"] == LIGAND_RESNAME
    ]
    if not crystal:
        raise RuntimeError(f"没有找到 {TARGET_CHAIN} 链的 {LIGAND_RESNAME} 配体")

    write_pdb(run_dir / "receptor.pdb", atoms)

    crystal_center = ligand_center(crystal)
    crystal_points = [entry["coord"] for entry in crystal]
    write_ligand_pdb(run_dir / "ligands" / "crystal-sti.pdb", crystal)

    truth = pocket_residues(atoms, crystal_points, POCKET_RADIUS, TARGET_CHAIN)
    pockets = [
        {
            "id": "P1",
            "label": "ATP 结合位点",
            "score": 0.91,
            "rank": 1,
            "is_reference": True,
            "center": [round(value, 3) for value in crystal_center],
            "radius": POCKET_RADIUS,
            "residues": truth,
        }
    ]

    distractors = cluster_centers(
        atoms, count=3, exclude=crystal_points, min_separation=18.0
    )
    distractor_labels = ["变构位点候选", "表面浅槽", "二聚界面附近"]
    for index, center in enumerate(distractors):
        residues = pocket_residues(atoms, [center], POCKET_RADIUS, TARGET_CHAIN)
        pockets.append(
            {
                "id": f"P{index + 2}",
                "label": distractor_labels[index],
                "score": round(0.74 - index * 0.11, 2),
                "rank": index + 2,
                "is_reference": False,
                "center": [round(value, 3) for value in center],
                "radius": POCKET_RADIUS,
                "residues": residues,
            }
        )

    # 结合的残基清单：与配体任一重原子距离在阈值内的残基，按最小距离排序
    contacts = []
    for residue in truth:
        contacts.append(
            {
                "residue": f"{residue['resname']}{residue['resseq']}",
                "resname": residue["resname"],
                "resseq": residue["resseq"],
                "chain": residue["chain"],
                "kind": "疏水接触" if residue["min_distance"] < 4.0 else "范德华接触",
                "distance": residue["min_distance"],
            }
        )

    molecules = []
    scores = [round(9.42 - index * 0.31 + random.uniform(-0.08, 0.08), 2) for index in range(10)]
    base_coords = [entry["coord"] for entry in crystal]
    for index, score in enumerate(scores):
        pose_path = run_dir / "ligands" / f"pose-{index + 1:02d}.pdb"
        write_ligand_pdb(
            pose_path, crystal, perturb(base_coords, PERTURB_SCALE + index * 0.06)
        )
        molecules.append(
            {
                "id": f"L{index + 1:02d}",
                "name": f"candidate-{index + 1:02d}",
                "score": score,
                "pose": f"ligands/{pose_path.name}",
                "chain": TARGET_CHAIN,
            }
        )

    provenance = {
        "structure_source": "RCSB PDB 1IEP",
        "pockets": "合成的演示数据",
        "poses": "合成的演示数据",
    }
    # 产出这次运行的会话。由命令行给出，没有就不写这一项，
    # 网页端据此决定要不要显示「回到会话」的入口。
    if session_id:
        provenance["session_id"] = session_id

    meta = {
        "run_id": run_dir.name,
        "target": {
            "name": "ABL kinase domain",
            "pdb_id": "1IEP",
            "chains": ["A", "B"],
            "reference_chain": TARGET_CHAIN,
            "ligand": "STI",
        },
        "structure": "receptor.pdb",
        "reference_ligand": "ligands/crystal-sti.pdb",
        "pockets": pockets,
        "molecules": molecules,
        "contacts": contacts,
        "provenance": provenance,
    }

    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"受体原子 {len(atoms)}")
    print(f"真值口袋 {pockets[0]['label']} 残基 {len(truth)} 个：{truth[0]['resname']}{truth[0]['resseq']} 起")
    print(f"口袋总数 {len(pockets)}")
    print(f"候选分子 {len(molecules)}，最高分 {scores[0]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成筛选结果的演示数据")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help="输出目录，meta.json 里的 run_id 取这个目录的名字",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="产出这次运行的会话 id，写进 provenance.session_id",
    )
    args = parser.parse_args()
    main(args.run_dir.resolve(), args.session_id or None)
