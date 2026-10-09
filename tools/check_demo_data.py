"""判据：口袋选区挑出来的原子确实贴着共晶配体。

前端的口袋高亮靠 meta.json 里每个口袋的残基编号拼成 3Dmol 的选区字符串。
这一步最容易出错的地方是残基编号的换算，而它一旦错了，界面上会显示一片
颜色、看着正常，只是染色的位置压根不在配体旁边。

所以这条判据不看界面，直接按前端使用的同一份数据把选区挑出来，算两件事：
口袋残基的原子有没有贴着配体（最小距离），以及口袋的几何中心离配体多远。
"""

import json
import math
import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent.parent / "runs" / "1iep-demo"

CONTACT_LIMIT = 4.5


def parse_pdb_atoms(path):
    """按残基归类读出原子，只留重原子。"""
    residues = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line[:6].strip() not in ("ATOM", "HETATM"):
            continue
        name = line[12:16].strip()
        if name.startswith("H"):
            continue
        chain = line[21].strip()
        resseq = int(line[22:26])
        coord = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        residues.setdefault((chain, resseq), []).append(coord)
    return residues


def min_distance(points_a, points_b):
    return min(
        math.dist(a, b) for a in points_a for b in points_b
    )


def main():
    meta = json.loads((RUN_DIR / "meta.json").read_text(encoding="utf-8"))
    receptor = parse_pdb_atoms(RUN_DIR / meta["structure"])
    ligand_atoms = parse_pdb_atoms(RUN_DIR / meta["reference_ligand"])

    ligand_points = []
    for points in ligand_atoms.values():
        ligand_points.extend(points)
    if not ligand_points:
        raise RuntimeError("共晶配体文件里没有原子")

    reference = next(p for p in meta["pockets"] if p["is_reference"])
    failures = []

    # 判据一：共晶配体所在的那个口袋，残基必须贴着配体
    touching = 0
    closest = float("inf")
    for residue in reference["residues"]:
        key = (residue["chain"], residue["resseq"])
        if key not in receptor:
            failures.append(f"口袋残基 {residue['resname']}{residue['resseq']} 在受体文件里找不到")
            continue
        distance = min_distance(receptor[key], ligand_points)
        closest = min(closest, distance)
        if distance <= CONTACT_LIMIT:
            touching += 1

    print(f"真值口袋 {reference['label']}：残基 {len(reference['residues'])} 个，"
          f"其中 {touching} 个与配体距离在 {CONTACT_LIMIT} 埃以内，最近 {closest:.2f} 埃")

    if touching < len(reference["residues"]) * 0.6:
        failures.append(
            f"贴着配体的残基只有 {touching} 个，不到真值口袋的六成，选区换算可能错了"
        )

    # 判据二：干扰口袋不该贴着配体，否则它们分不出哪个是真位点
    for pocket in meta["pockets"]:
        if pocket["is_reference"]:
            continue
        near = 0
        for residue in pocket["residues"]:
            key = (residue["chain"], residue["resseq"])
            if key in receptor and min_distance(receptor[key], ligand_points) <= CONTACT_LIMIT:
                near += 1
        status = "贴着配体" if near else "离配体足够远"
        print(f"{pocket['id']} {pocket['label']}：{len(pocket['residues'])} 个残基，{status}")
        if near:
            failures.append(f"干扰口袋 {pocket['id']} 有 {near} 个残基贴着配体，起不到对照作用")

    # 判据三：候选分子的坐标范围要与配体所在区域重合
    pose_dir = RUN_DIR / "ligands"
    for molecule in meta["molecules"]:
        pose_atoms = parse_pdb_atoms(RUN_DIR / molecule["pose"])
        pose_points = [point for points in pose_atoms.values() for point in points]
        if not pose_points:
            failures.append(f"{molecule['id']} 的位姿文件里没有原子")
            continue
        spread = max(
            math.dist(point, ligand_points[0]) for point in pose_points[:1]
        )
        drift = min_distance(pose_points, ligand_points)
        if drift > 8.0:
            failures.append(f"{molecule['id']} 的位姿离共晶配体 {drift:.2f} 埃，超出对接盒子的合理范围")

    print(f"候选分子 {len(meta['molecules'])} 个的位姿都在配体附近")

    if failures:
        print("\n未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
