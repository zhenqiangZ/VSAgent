"""四层口袋来源的装配点：P0 复合物配体 → P1 预测 → 留空，另有 P2 先验清单。

这一层**不自己算几何**——几何在 `export.py`，网络在 `complexes.py` / `prankweb.py`，
叠合在 `align.py`。它只做一件事：按优先级把各侧的结果装配成 `meta.json` 里
`pockets[]` 那一种形状，并把「这个数是从哪来的」标清楚。

**每个数标来源**（方案稿 §2.1 / §2.3.8）：

| 字段 | P0（复合物） | P1（预测） | P2（先验清单） |
|---|---|---|---|
| `residues[]` | 从共晶配体圈 5 埃 | prankweb 直接给 | 从叠合后的配体圈 5 埃 |
| `center` / `radius` | 配体聚类算，半径固定 5.0 | prankweb 给 center，半径同 5.0 | 同 P0 |
| `score` | **不出现**（实验定位没有打分这回事） | prankweb 的 `score` 与 `probability` | **不出现** |
| `source` | `"ligand"` | `"p2rank"` | `"ligand"` |
| `complexes[]` | **有**（跨哪几个复合物、各哪个配体、分辨率） | 无 | **有**，另带 `rmsd` |

P2 与 P0 的分工在**清单从哪来**：P0 拿 UniProt 编号去检索，返回的是「带配体的
已发布复合物」，覆盖到的口袋窄；P2 拿模型凭先验知识给出的编号清单，覆盖面宽，
代价是清单本身不可信——所以 P2 先做两道核对（编号存在、序列是受体序列），
核对不通过的一律剔除并逐条报出来。

**没有来源就留空。** 四态 `origin = "complex" | "aligned" | "predicted" | "none"` 里，
`none` 还要再分「查了没有」与「没查成」——这两者对用户的含义完全不同（§2.3.9）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import align, complexes, export, http, pdbtext, prankweb, preprocess, structure

__all__ = [
    "PocketBundle",
    "complex_pockets",
    "p2rank_pockets",
    "prior_pockets",
]

POCKET_RADIUS = export.POCKET_RADIUS


# ---------------------------------------------------------------------------
# 装配结果
# ---------------------------------------------------------------------------
@dataclass
class PocketBundle:
    """一侧装出来的口袋，与一句「怎么来的」。"""

    pockets: list[dict[str, Any]] = field(default_factory=list)
    origin: str = "none"
    """`"complex"` / `"predicted"` / `"none"`（方案稿 §4.1）。"""

    note: str = ""
    """给 `provenance.pockets.note` 的一句话。`none` 时要分清「查了没有」与「没查成」。"""

    reason: str = ""
    """空结果的原因，三态：`"checked"` / `"failed"` / `""`（有结果时不填）。

    **这是给调用方分档用的结构化字段**，不让调用方去 `note` 里搜「没查成」这几个字——
    措辞会改，而这两档说出的话相反（`checked` = 换个条件重试也没用；
    `failed` = 稍后重试可能就好），搜措辞的分档会在改文案时静默失效。
    `"checked"` = 查了、确实没有；`"failed"` = 没查成（网络或接口不可用）。
    """

    total: int = 0
    """搜到几个复合物（P0 专用）。只报 `used` 会让用户以为这个蛋白只有这么多。"""

    used: int = 0

    excluded: dict[str, int] = field(default_factory=dict)
    """各类被筛掉的杂原子组各几个（§2.3.7：筛选结果必须报出来）。"""

    sources: list[dict[str, Any]] = field(default_factory=list)
    """P0 用的复合物清单（给人看的汇总）。"""

    @property
    def is_empty(self) -> bool:
        return not self.pockets


@dataclass
class _Cluster3D:
    """一个复合物里算出来的一个口袋：中心 + 重原子 + 圈到的残基 + 来源。"""

    center: tuple[float, float, float]
    coords: tuple[tuple[float, float, float], ...]
    residues: tuple[export.PocketResidue, ...]
    origin: dict[str, Any]


# ---------------------------------------------------------------------------
# P0：从复合物结构算口袋
# ---------------------------------------------------------------------------
def complex_pockets(
    *,
    uniprot: str,
    receptor_chains: tuple[str, ...] = (),
    top_n: int = complexes.DEFAULT_TOP_N,
    radius: float = POCKET_RADIUS,
    timeout: float | None = None,
    search=None,
    fetch=None,
) -> PocketBundle:
    """P0 主来源：搜该蛋白所有带配体的复合物 → 排序取前 N → 下载 → 取口袋。

    **这是本轮的默认路径。** 返回空 `PocketBundle` 不代表出错——它正是
    「P0 为空」这一态，是转 P1 的唯一入口（判错就永远转不到预测）。

    `search` / `fetch` 是给判据打桩留的注入点（默认走 `complexes.py` 的真实实现）。
    打桩的调用方**不改算法**，只是把网络那一段换掉。

    四段：搜 → 排 → 下 → 取口袋。任何一段失败都留空并写明原因，**不编条目**。
    """
    if not (uniprot or "").strip():
        return PocketBundle(
            origin="none", reason="failed", note="没有 UniProt 编号，主来源无所依凭（没查成）"
        )

    search = search or complexes.list_complexes
    fetch = fetch or _default_fetch

    # ---- 1. 搜 ----
    try:
        ids, _body = search(uniprot)
    except complexes.ComplexError as error:
        return PocketBundle(
            origin="none",
            reason="failed",
            note=f"复合物检索没成：{error}（没查成，不是查了没有）",
        )
    if not ids:
        return PocketBundle(
            origin="none",
            reason="checked",
            note=f"{uniprot} 名下没有带配体的已发布结构（查了，确实没有）",
        )

    # ---- 2. 排 ----
    entries = [complexes.ComplexEntry(pdb_id=entry_id) for entry_id in ids]
    chosen, total = complexes.pick_top(
        entries,
        top_n=top_n,
        excluded_ligands=export.MONATOMIC_IONS | export.BUFFER_LIKE,
    )
    if not chosen:
        return PocketBundle(
            origin="none",
            reason="checked",
            total=total,
            note=f"搜到 {total} 个条目，但排序后一个都没选上",
        )

    # ---- 3. 下 ----
    outcome = fetch(chosen, top_n=top_n, timeout=timeout)
    if not outcome.texts:
        reasons = "；".join(f"{pid}: {why}" for pid, why in outcome.failed[:3])
        return PocketBundle(
            origin="none",
            reason="failed",
            total=total,
            note=f"搜到 {total} 个条目但一个都没下下来（{reasons}）",
        )

    # ---- 4. 取口袋 ----
    clusters = _clusters_from_structures(
        outcome.texts, receptor_chains=receptor_chains, radius=radius
    )
    excluded = clusters.pop("_excluded", {})
    sources = clusters.pop("_sources", [])
    if not clusters:
        return PocketBundle(
            origin="none",
            reason="checked",
            total=total,
            used=outcome.used,
            excluded=excluded,
            sources=sources,
            note="下载到的结构里筛完杂原子后没有剩下可当配体的组（查了，确实没有）",
        )

    merged = _merge(clusters, radius=radius)
    note = f"从 {len(outcome.texts)} 个复合物结构的共晶配体圈出来"
    if outcome.failed:
        note += f"；{len(outcome.failed)} 个条目没下下来（{outcome.failed[0][0]} 等）"
    return PocketBundle(
        pockets=merged,
        origin="complex",
        note=note,
        total=total,
        used=outcome.used,
        excluded=excluded,
        sources=sources,
    )


def _default_fetch(entries, **kwargs):
    return complexes.fetch_top(entries, **kwargs)


def _clusters_from_structures(
    texts: list[tuple[str, str]],
    *,
    receptor_chains: tuple[str, ...],
    radius: float,
) -> dict:
    """把下载到的几份复合物结构各自算出口袋。

    回来的是 `{0: _Cluster3D, 1: ..., "_excluded": {...}, "_sources": [...]}`——
    用字典是为了把两个附带信息（排除统计、来源清单）跟口袋一起带出去，
    调用方不必再扫一遍结构。
    """
    out: dict = {}
    excluded: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    index = 0

    for pdb_id, text in texts:
        facts = structure.parse_pdb(text, pdb_id=pdb_id)
        doc = pdbtext.parse(text)
        filtered = export.ligand_groups(
            doc, modified_names=[name for name, _count in facts.modified]
        )
        # 只算受体那几条链（复合物里受体只是其中一条，别把配体伴侣当口袋）。
        chains = receptor_chains or tuple(facts.polymer_chains)
        # 名字名单之后再走一遍几何筛查：缓冲液名单永远收不全，靠「重原子太少 /
        # 共价挂在蛋白上 / 只碰到一两个残基」把剩下的溶剂挡掉。
        # **按复合物自己的全部聚合物链判接触**：不同条目的链号未必与受体一致，
        # 传受体链号会把整条配体误判成「接触不足」。
        screened = preprocess.screen_ligands(doc, filtered.kept)
        for reason, count in filtered.counts.items():
            excluded[reason] = excluded.get(reason, 0) + count
        for reason, count in screened.counts.items():
            excluded[reason] = excluded.get(reason, 0) + count

        for cluster in export.cluster_ligands(screened.kept):
            residues = export.pocket_residues(doc, cluster.coords, radius, chains=chains)
            if not residues:
                continue
            out[index] = _Cluster3D(
                center=cluster.center,
                coords=cluster.coords,
                residues=residues,
                origin={
                    "pdb_id": pdb_id,
                    "ligand": "+".join(cluster.resnames),
                    "resolution": facts.resolution,
                },
            )
            index += 1

        sources.append(
            {
                "pdb_id": pdb_id,
                "resolution": facts.resolution,
                "ligands": [name for name, _ in facts.ligands],
            }
        )

    out["_excluded"] = excluded
    out["_sources"] = sources
    return out


def _merge(clusters: dict, *, radius: float) -> list[dict[str, Any]]:
    """跨复合物合并：中心 6 埃以内算同一处（§2.3.5）。

    合并之后残基集合取**并集里最短距离最小的那个值**——同一个残基在两个复合物里
    各有一个最短距离时，给用户看的应当是「它离配体最近能有多近」，也就是较小的那个。
    """
    items = [clusters[key] for key in sorted(k for k in clusters if isinstance(k, int))]
    if not items:
        return []

    # 并查集：中心距 ≤ MERGE_RADIUS 的连起来。
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            dx = items[i].center[0] - items[j].center[0]
            dy = items[i].center[1] - items[j].center[1]
            dz = items[i].center[2] - items[j].center[2]
            if dx * dx + dy * dy + dz * dz <= export.MERGE_RADIUS**2:
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a

    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)

    pockets: list[dict[str, Any]] = []
    for members in sorted(groups.values(), key=lambda m: (-len(m), m[0])):
        points: list[tuple[float, float, float]] = []
        best: dict[tuple[str, str, str], export.PocketResidue] = {}
        origins: list[dict[str, Any]] = []
        for m in members:
            points.extend(items[m].coords)
            origins.append(items[m].origin)
            for residue in items[m].residues:
                key = (residue.chain, residue.number, residue.icode)
                current = best.get(key)
                if current is None or residue.min_distance < current.min_distance:
                    best[key] = residue

        ordered = sorted(best.values(), key=lambda r: (r.min_distance, r.chain, r.number, r.icode))
        pockets.append(
            {
                "center": export.ligand_center(points) if points else items[members[0]].center,
                "radius": radius,
                # P0 出来的每一条都是「实验配体」——**没有 score**，
                # 实验定位本来就没有打分这回事（§2.1）。
                "source": "ligand",
                "residues": [r.as_dict() for r in ordered],
                "complexes": [_complex_entry(o) for o in origins],
            }
        )

    pockets.sort(key=lambda p: (-len(p["complexes"]), p["center"]))
    return pockets


def _complex_entry(origin: dict[str, Any]) -> dict[str, Any]:
    """一条来源记录。`rmsd` **只在有值时出现**——P0 那一侧没有叠合这回事，
    补一个 `null` 会让「没做叠合」与「叠合出来是空」看起来一样。"""
    entry: dict[str, Any] = {
        "pdb_id": origin.get("pdb_id", ""),
        "ligand": origin.get("ligand", ""),
        "resolution": origin.get("resolution"),
    }
    if origin.get("rmsd") is not None:
        entry["rmsd"] = origin["rmsd"]
    return entry


# ---------------------------------------------------------------------------
# P2：先验清单（模型给出的编号）+ 叠合
# ---------------------------------------------------------------------------
def prior_pockets(
    *,
    receptor_text: str,
    candidates: Sequence[str],
    receptor_chains: tuple[str, ...] = (),
    radius: float = POCKET_RADIUS,
    timeout: float | None = None,
    download=None,
    probe=None,
) -> tuple[PocketBundle, tuple[align.Verdict, ...]]:
    """P2：按模型给出的编号清单取证，再把叠合后的配体聚成口袋。

    四段：取 → 核 → 叠 → 圈。**核对不通过的编号一律剔除并留在结论里**，
    不静默丢掉——「这个编号不是这个蛋白」正是用户要看的那句话。

    与 P0 一样，回来的是 `(PocketBundle, 逐条结论)`；空 `PocketBundle` 不代表出错，
    它正是「清单里没有一个能用的」这一态，是转 P1 的入口之一。

    `download` / `probe` 是给判据打桩留的注入点（默认走 `complexes.py` 与 `http.py`）。
    打桩的调用方**不改算法**，只是把网络那一段换掉。
    """
    download = download or _default_download
    probe = probe or http.probe

    receptor_doc = pdbtext.parse(receptor_text)
    # 受体这一份文件里的聚合物链就是受体自己，所以留空时按它来。
    chains = receptor_chains or tuple(align.sequences(receptor_text))

    clusters: dict = {}
    verdicts: list[align.Verdict] = []
    excluded: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    index = 0
    seen: set[str] = set()

    for raw in candidates:
        pdb_id = (raw or "").strip().upper()
        if not pdb_id or pdb_id in seen:
            continue
        seen.add(pdb_id)

        try:
            text = download(pdb_id, timeout=timeout)
        except complexes.ComplexError as error:
            state, reason = _classify_failure(pdb_id, error, probe, timeout)
            verdicts.append(align.Verdict(pdb_id, state, reason))
            continue

        verdict, transform = align.align_candidate(receptor_text, text, pdb_id)
        if transform is None:
            verdicts.append(verdict)
            continue

        facts = structure.parse_pdb(text, pdb_id=pdb_id)
        doc = pdbtext.parse(text)
        filtered = export.ligand_groups(
            doc, modified_names=[name for name, _count in facts.modified]
        )
        # 候选结构里的链号未必与受体一致（叠合按序列配对，不按链号），
        # 所以这里按**它自己的全部聚合物链**判接触，不传受体链号。
        screened = preprocess.screen_ligands(doc, filtered.kept)
        for reason, count in filtered.counts.items():
            excluded[reason] = excluded.get(reason, 0) + count
        for reason, count in screened.counts.items():
            excluded[reason] = excluded.get(reason, 0) + count

        moved = align.moved_groups(screened.kept, transform)
        for cluster in export.cluster_ligands(moved):
            residues = export.pocket_residues(
                receptor_doc, cluster.coords, radius, chains=chains
            )
            if not residues:
                # 配体搬到受体上之后周围没有受体残基——多半是叠合到了别处。
                # 留空并让这一条进结论，**不编一个空口袋**。
                continue
            clusters[index] = _Cluster3D(
                center=cluster.center,
                coords=cluster.coords,
                residues=residues,
                origin={
                    "pdb_id": pdb_id,
                    "ligand": "+".join(cluster.resnames),
                    "resolution": facts.resolution,
                    "rmsd": round(verdict.rmsd, 3) if verdict.rmsd is not None else None,
                },
            )
            index += 1

        ligands = tuple(name for name, _count in facts.ligands)
        verdicts.append(dataclasses.replace(verdict, ligands=ligands))
        sources.append(
            {
                "pdb_id": pdb_id,
                "resolution": facts.resolution,
                "rmsd": round(verdict.rmsd, 3) if verdict.rmsd is not None else None,
                "ligands": list(ligands),
            }
        )

    merged = _merge(clusters, radius=radius)
    if not merged:
        return (
            PocketBundle(
                origin="none",
                reason="checked",
                total=len(seen),
                used=sum(1 for item in verdicts if item.ok),
                excluded=excluded,
                sources=sources,
                note=_empty_note(verdicts, len(seen)),
            ),
            tuple(verdicts),
        )

    passed = sum(1 for item in verdicts if item.ok)
    note = (
        f"从先验清单里 {passed} 个通过核对的复合物结构、按叠合后的共晶配体圈出来"
    )
    if len(seen) > passed:
        note += f"；清单里另有 {len(seen) - passed} 个被剔除"
    return (
        PocketBundle(
            pockets=merged,
            origin="aligned",
            note=note,
            total=len(seen),
            used=passed,
            excluded=excluded,
            sources=sources,
        ),
        tuple(verdicts),
    )


def _empty_note(verdicts: Sequence[align.Verdict], total: int) -> str:
    """一个都没能用时，把「查了确实没有」与「没查成」分开说。"""
    if not total:
        return "清单是空的，没有编号可取（没查成）"
    if not any(item.ok for item in verdicts):
        return f"清单里 {total} 个编号一个都没通过核对（查了，确实没有）"
    return f"清单里 {total} 个编号通过了核对，但叠合后的配体在受体上圈不到残基（查了，确实没有）"


def _default_download(pdb_id: str, **kwargs):
    return complexes.download_entry(pdb_id, **kwargs)


def _classify_failure(
    pdb_id: str, error: Exception, probe, timeout: float | None
) -> tuple[str, str]:
    """下载没成时，用一次 HEAD 把「编号不存在」与「没查成」分开。

    分这两档是有代价的（多一次请求），但不说清的话模型只会看到「下载失败」，
    然后对着一个编错的编号反复重试——而真实原因是那个编号压根不存在。

    404 的话还要留半句余地：RCSB 对超出 PDB 格式上限的条目只提供 mmCIF，
    那也是 404，与「没有这个编号」同码（`http.probe` 的注释记过这件事）。
    """
    url = f"{complexes.RCSB_DOWNLOAD_URL}/{pdb_id}.pdb"
    try:
        status = probe(url, timeout=timeout or http.DEFAULT_TIMEOUT)
    except Exception:  # 探一下也失败：连编号在不在都答不出来
        return "unreachable", f"{error}；编号在不在也没探出来（没查成）"
    if status == 404:
        return (
            "missing",
            "RCSB 上没有这个编号，或它只提供 mmCIF 而取不到 .pdb（查了，确实没有）",
        )
    if status == 200:
        return "unreachable", f"{error}；编号是存在的，这一次没取下来（没查成）"
    return "unreachable", f"{error}；探编号时回的是 HTTP {status}（没查成）"


# ---------------------------------------------------------------------------
# P1：prankweb 预测（仅在 P0 为空时调用）
# ---------------------------------------------------------------------------
def p2rank_pockets(
    *,
    pdb_id: str,
    receptor_chains: tuple[str, ...] = (),
    radius: float = POCKET_RADIUS,
    client=None,
) -> PocketBundle:
    """P1 副选：prankweb.cz 的 p2rank 预测。

    **只在 `complex_pockets()` 报「P0 为空」时才会被调用**（§2.3.9）——
    主路径不走这里。有实验事实时不该拿预测当主结果。

    `client` 是给判据打桩留的注入点（默认 `prankweb.fetch_prediction`）。
    """
    if not (pdb_id or "").strip():
        return PocketBundle(
            origin="none",
            reason="failed",
            note="没有 PDB 编号，预测这一侧无从查起（没查成）",
        )

    client = client or prankweb.fetch_prediction
    result = client(pdb_id)

    if result.status != prankweb.PredictionStatus.SUCCESSFUL:
        # 四态里只有 successful 能取到数据；其余三态**都是没查成**——
        # 包括服务端明确报的 failed（这一条算不出来，但仍是「没查出东西」，
        # 不是「查了、结果就是空」）。分开的理由见 `PocketBundle.reason`。
        return PocketBundle(origin="none", reason="failed", note=result.describe())

    pockets = _pockets_from_prediction(
        result.payload, receptor_chains=receptor_chains, radius=radius
    )
    if not pockets:
        return PocketBundle(
            origin="none",
            reason="checked",
            note=f"{pdb_id} 的预测里没有可用的口袋（查了，确实没有）",
        )
    return PocketBundle(
        pockets=pockets,
        origin="predicted",
        note=f"该蛋白没有任何带配体的共晶结构，改用 prankweb 的 p2rank 预测（结构 {pdb_id}）",
    )


def _pockets_from_prediction(
    payload: Any, *, receptor_chains: tuple[str, ...], radius: float
) -> list[dict[str, Any]]:
    """把 prankweb 的 `prediction.json` 装成 `pockets[]`。

    **编号就是 PDB 作者编号，不引入任何换算**（§2.3.4）——上一版 PDBe 路线才需要
    把 UniProt 编号换算过来，这里有这一步就是错的。

    只留受体那条链：`1iep` 的 `structure.regions` 有 A / B 两段，受体链是 A 时
    B 段的残基不能进 `residues`（否则页面上会出现一个受体里不存在的残基）。
    """
    if not isinstance(payload, dict):
        return []

    raw_pockets = payload.get("pockets")
    if not isinstance(raw_pockets, list):
        return []

    wanted = set(receptor_chains) if receptor_chains else None
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(raw_pockets, start=1):
        if not isinstance(item, dict):
            continue
        residues = _prediction_residues(item, wanted)
        center = _prediction_center(item)
        pocket: dict[str, Any] = {
            "id": f"P{rank}",
            "label": f"预测口袋 {item.get('name') or rank}",
            "rank": rank,
            "is_reference": rank == 1,
            "center": [round(value, 3) for value in center],
            "radius": radius,
            "source": "p2rank",
            "residues": residues,
        }
        score = _as_float(item.get("score"))
        probability = _as_float(item.get("probability"))
        if score is not None:
            pocket["score"] = score
        if probability is not None:
            pocket["probability"] = probability
        out.append(pocket)
    return out


def _prediction_residues(item: dict[str, Any], wanted: set[str] | None) -> list[dict[str, Any]]:
    """把 `["A_246", "A_248", …]` 分成 `{chain, resseq, resname, min_distance?}`。

    预测给的是 PDB 作者编号（`A_360`），**没有 `min_distance`**——那不是几何量，
    是结构里的原子距离，prankweb 不提供。所以这里的残基条目**不带那个字段**，
    前端按缺失降级（这是「没有来源就留空」的一个具体例子）。

    `resname` 也不在 prankweb 的结果里。位置保留一个空串，不编一个三字母代号——
    编出来的残基名看上去和真的一样，而页面上没有任何东西能暴露它是编的。
    """
    out: list[dict[str, Any]] = []
    for token in item.get("residues") or []:
        if not isinstance(token, str) or "_" not in token:
            continue
        chain, _, number = token.rpartition("_")
        if wanted is not None and chain not in wanted:
            continue
        entry: dict[str, Any] = {"chain": chain}
        entry["resseq"] = int(number) if number.isdigit() else number
        out.append(entry)
    return out


def _prediction_center(item: dict[str, Any]) -> tuple[float, float, float]:
    center = item.get("center")
    if isinstance(center, (list, tuple)) and len(center) >= 3:
        values = [_as_float(v) for v in center[:3]]
        if all(v is not None for v in values):
            return (values[0], values[1], values[2])  # type: ignore[return-value]
    return (0.0, 0.0, 0.0)


def _as_float(value: Any) -> float | None:
    """prankweb 把数字写成**字符串**（`"39.46"`），要转一手。转不了给 None，不猜。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
