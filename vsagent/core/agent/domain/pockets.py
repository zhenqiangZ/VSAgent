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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..ledger import (
    ArtifactHandle,
    ReceptorHandle,
    SessionLedger,
    expect_handle,
    fingerprint_path,
)
from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import align, complexes, export, http, pdbtext, prankweb, preprocess, products, structure

__all__ = [
    "PocketBundle",
    "DetectPocketsTool",
    "DefinePocketTool",
    "complex_pockets",
    "domain_tools",
    "p2rank_pockets",
    "pockets_dir",
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


# ---------------------------------------------------------------------------
# 工具：口袋集与单个口袋
#
# 上面那几段只在 `write_run` 里跑，模型在对接**之前**看不到口袋；而
# `prepare_receptor` 的盒子只有「共晶配体 / 显式坐标 / whole」三选一。
# 没有可查的口袋对象，就回答不了「这个蛋白有几个口袋、该打哪一个」。
#
# 这里不新增 handle 类型：口袋集与单个口袋都登记成 artifact，靠 `meta.kind`
# 区分（`pockets` / `pocket`）。`__init__.py` 那条「只有三类 kind」的纪律继续守。
# ---------------------------------------------------------------------------
POCKETS_DIR_NAME = "pockets"
POCKET_SET_FILE = "pockets.json"
POCKET_FILE = "pocket.json"
SET_PARAMS_FILE = products.PARAMS_FILE

DEFAULT_POCKET_PADDING = 8.0
MAX_POCKET_PADDING = 20.0
MIN_POCKET_RADIUS = 3.0
MAX_POCKET_RADIUS = 8.0
MAX_POCKETS_SHOWN = 5
MAX_POCKETS_LISTED = 20

# 层的优先级只影响**排序**，不改变来源判定。结构自带的配体最可信，预测最次。
LAYER_PRIORITY = {"ligand": 0, "complex": 1, "prior": 2, "predicted": 3}
LAYER_SOURCE = {"ligand": "ligand", "complex": "complex", "prior": "prior", "predicted": "p2rank"}
KNOWN_LAYERS = tuple(LAYER_PRIORITY)


def pockets_dir() -> Path:
    """口袋产物的根目录。与状态文件、结构缓存同处一个家目录。"""
    return Path.home() / ".opencode_tui" / POCKETS_DIR_NAME


def _number_argument(
    arguments: dict[str, Any], name: str, default: float, low: float, high: float
) -> float:
    value = arguments.get(name)
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"参数 {name} 应当是数字")
    number = float(value)
    if not low <= number <= high:
        raise ToolError(f"参数 {name} 要在 {low:g} 到 {high:g} 之间（现在是 {number:g}）")
    return number


def _int_argument(
    arguments: dict[str, Any], name: str, default: int, low: int, high: int
) -> int:
    value = arguments.get(name)
    if value is None:
        return int(default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"参数 {name} 应当是整数")
    if not low <= value <= high:
        raise ToolError(f"参数 {name} 要在 {low} 到 {high} 之间（现在是 {value}）")
    return value


def _split_tokens(text: Any) -> list[str]:
    return [token for token in re.split(r"[,\s]+", str(text or "").strip()) if token]


def _inherited_meta(ledger: SessionLedger, entry: Any, key: str) -> str:
    """条目自己没记的字段，沿血缘往上找。

    prepared 受体的 meta 里没有 `pdb_id`（它只记 stage / 链 / 盒子），
    而它的父项 raw 结构有。不往上找的话，对一份准备后的受体做口袋只能靠
    调用方重新给一遍编号，那正是「模型凭记忆编编号」的入口。
    """
    value = str((getattr(entry, "meta", {}) or {}).get(key) or "").strip()
    if value:
        return value
    for parent in ledger.lineage(entry.handle):
        value = str((parent.meta or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def _chains_of(entry: Any, facts: structure.StructureFacts) -> tuple[str, ...]:
    """受体要算哪几条链。

    raw 结构的 meta.chains 是**字典列表**（带 seqres_length 等），prepared 的
    是**字符串列表**——两种形状都要认，否则一份结构会按「全部链」算，
    把复合物里的伴侣链也圈进口袋。
    """
    raw = (getattr(entry, "meta", {}) or {}).get("chains")
    chains: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                chains.append(item.strip())
            elif isinstance(item, dict):
                chain_id = str(item.get("chain_id") or "").strip()
                if chain_id:
                    chains.append(chain_id)
    ordered = tuple(dict.fromkeys(chains))
    return ordered or tuple(facts.polymer_chains)


def _coordinate_of(entry: Any) -> Path:
    path = export._receptor_coordinate(entry)
    if path is None:
        raise ToolError(export._no_coordinate_note(entry))
    return path


def _ligand_bundle(
    doc: pdbtext.PdbDocument,
    *,
    ligand_text: str,
    ligand_label: str,
    chains: tuple[str, ...],
    radius: float,
    pdb_id: str,
) -> PocketBundle:
    """准备后的受体：共晶配体已被抽到 `ligand.pdb`，拿它圈口袋。

    不这么做的话，对 prepared 受体做口袋永远只能转复合物或预测——而它手上
    明明留着当初那个配体。圈不到残基时留空，不编中心。
    """
    try:
        ligand_doc = pdbtext.parse(ligand_text)
    except pdbtext.PdbEditError as error:
        return PocketBundle(
            origin="none", reason="failed", note=f"ligand.pdb 读不出来：{error}（没算成）"
        )
    coords = tuple(
        atom.coord for atom in ligand_doc.atoms if pdbtext.is_heavy(atom)
    )
    if not coords:
        return PocketBundle(
            origin="none", reason="checked", note="ligand.pdb 里没有重原子（查了，确实没有）"
        )
    residues = export.pocket_residues(doc, coords, radius, chains=chains)
    if not residues:
        return PocketBundle(
            origin="none",
            reason="checked",
            note="共晶配体周围没有受体残基（查了，确实没有）",
        )
    pocket = {
        "center": export.ligand_center(coords),
        "radius": radius,
        "source": "ligand",
        "residues": [item.as_dict() for item in residues],
        "complexes": [
            {"pdb_id": pdb_id, "ligand": ligand_label, "resolution": None}
        ],
    }
    return PocketBundle(
        pockets=[pocket],
        origin="complex",
        note=f"从准备受体留下的共晶配体（{ligand_label or 'ligand.pdb'}）圈出来",
        total=1,
        used=1,
    )


def _structure_bundle(
    text: str,
    *,
    chains: tuple[str, ...],
    radius: float,
    pdb_id: str,
) -> PocketBundle:
    """这份结构自带的杂原子组 → 口袋。离线、不联网。"""
    clusters = _clusters_from_structures(
        [(pdb_id, text)], receptor_chains=chains, radius=radius
    )
    excluded = clusters.pop("_excluded", {})
    sources = clusters.pop("_sources", [])
    if not clusters:
        return PocketBundle(
            origin="none",
            reason="checked",
            excluded=excluded,
            sources=sources,
            note="这份结构里没有可当配体的杂原子组（水、单原子离子、缓冲液都筛掉了）",
        )
    return PocketBundle(
        pockets=_merge(clusters, radius=radius),
        origin="complex",
        note="从这份结构的共晶配体圈出来",
        total=1,
        used=1,
        excluded=excluded,
        sources=sources,
    )


def _center_of(pocket: dict[str, Any]) -> tuple[float, float, float]:
    raw = pocket.get("center")
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        values: list[float] = []
        for item in raw[:3]:
            number = _as_float(item)
            if number is None:
                return (0.0, 0.0, 0.0)
            values.append(round(number, 3))
        return (values[0], values[1], values[2])
    return (0.0, 0.0, 0.0)


def _normalize_pockets(
    pockets: Sequence[dict[str, Any]], *, layer: str, radius: float, padding: float
) -> list[dict[str, Any]]:
    """把四层各自的形状压成同一种。

    四层的字段本来就不同（P0 有 `complexes`、预测有 `score`/`probability`、
    先验有 `rmsd`），这里只统一**下游真正要读的**那几个：中心、半径、残基、
    支持度、盒子。其余字段原样带上，不丢也不编。
    """
    out: list[dict[str, Any]] = []
    size = round(2.0 * (radius + padding), 3)
    for item in pockets:
        if not isinstance(item, dict):
            continue
        center = _center_of(item)
        complexes = [
            dict(entry)
            for entry in (item.get("complexes") or [])
            if isinstance(entry, dict)
        ]
        ligands = sorted(
            {str(entry.get("ligand")) for entry in complexes if entry.get("ligand")}
        )
        pdb_ids = sorted(
            {str(entry.get("pdb_id")) for entry in complexes if entry.get("pdb_id")}
        )
        pocket: dict[str, Any] = {
            "center": list(center),
            "radius": float(item.get("radius") or radius),
            "source": LAYER_SOURCE[layer],
            "layer": layer,
            "residues": [
                dict(residue)
                for residue in (item.get("residues") or [])
                if isinstance(residue, dict)
            ],
            "support": {
                "complexes": len(complexes),
                "pdb_ids": pdb_ids,
                "ligands": ligands,
            },
            # 盒子只由「中心 + 半径 + padding」算：四层里只有结构那一层的残基
            # 一定落在受体自己的编号上，其余层的残基编号未必对得上受体，
            # 拿它们的坐标算包围盒会得到一个看着合理、实际圈错位置的盒子。
            "box": {
                "center": list(center),
                "size": [size, size, size],
                "source": "radius",
            },
        }
        for key in ("score", "probability", "label", "id"):
            if key in item:
                pocket[key] = item[key]
        out.append(pocket)
    _sort_pockets(out)
    return out


def _sort_pockets(pockets: list[dict[str, Any]]) -> None:
    """层优先级 → 支持复合物数 → 残基数 → 中心。排序稳定，index 重新编号。"""
    pockets.sort(
        key=lambda pocket: (
            LAYER_PRIORITY.get(str(pocket.get("layer") or ""), 9),
            -int((pocket.get("support") or {}).get("complexes") or 0),
            -len(pocket.get("residues") or []),
            pocket.get("center") or [0.0, 0.0, 0.0],
        )
    )
    for index, pocket in enumerate(pockets, start=1):
        pocket["index"] = index


def _set_summary(pockets: Sequence[dict[str, Any]]) -> str:
    layers = {str(pocket.get("layer") or "") for pocket in pockets}
    labels = "、".join(
        {"ligand": "共晶配体", "complex": "复合物", "prior": "先验", "predicted": "预测"}[layer]
        for layer in sorted(layers, key=lambda name: LAYER_PRIORITY.get(name, 9))
        if layer in LAYER_PRIORITY
    )
    return f"口袋集 · {len(pockets)} 处 · {labels or '未知来源'}"


def _register_set(
    ledger: SessionLedger,
    *,
    target: Path,
    fingerprint: str,
    summary: str,
    pockets: Sequence[dict[str, Any]],
    receptor: ReceptorHandle,
    params: dict[str, Any],
) -> tuple[str, bool]:
    existing = ledger.find(
        kind="artifact", fingerprint=fingerprint, include_archived=True
    )
    for candidate in existing:
        if str(candidate.meta.get("kind") or "") == "pockets":
            return str(candidate.handle), True
    entry = ledger.add(
        "artifact",
        path=target,
        summary=summary,
        fingerprint=fingerprint,
        parents=[receptor],
        meta={
            "kind": "pockets",
            "stage": "pockets",
            "count": len(pockets),
            "layers": sorted(
                {str(pocket.get("layer") or "") for pocket in pockets},
                key=lambda name: LAYER_PRIORITY.get(name, 9),
            ),
            "params": params,
            "receptor": str(receptor),
        },
    )
    return str(entry.handle), False


def _render_pockets(
    *,
    handle: str,
    pockets: Sequence[dict[str, Any]],
    total: int,
    shown: int,
    reused: bool,
    note: str,
) -> str:
    lines: list[str] = []
    if reused:
        lines.append(f"登记表里已有同一份口袋集：{handle}（未重复计算）。")
    else:
        lines.append(f"已算出 {total} 处口袋，登记为 {handle}。")
    if note:
        lines.append(note)
    for pocket in pockets[:shown]:
        center = pocket.get("center") or [0.0, 0.0, 0.0]
        residues = pocket.get("residues") or []
        support = pocket.get("support") or {}
        size = (pocket.get("box") or {}).get("size") or [0.0, 0.0, 0.0]
        line = (
            f"  #{pocket.get('index')} 中心 ({center[0]:g}, {center[1]:g}, {center[2]:g})"
            f" · {len(residues)} 个残基 · 来源 {pocket.get('source')}"
        )
        if support.get("complexes"):
            line += f" · 支持 {support['complexes']} 个复合物"
        line += f" · 盒子 {size[0]:g}×{size[1]:g}×{size[2]:g}"
        lines.append(line)
    rest = total - min(shown, total)
    if rest > 0:
        lines.append(
            f"另有 {rest} 处未显示（limit={shown}）；调大 limit，或直接按 index 取。"
        )
    lines.append(
        f"下一步：define_pocket(pockets={handle}, index=1) 选一处，"
        "再 prepare_receptor(pocket=...)。"
    )
    return "\n".join(lines)


class DetectPocketsTool(Tool):
    """把一个受体的结合口袋列出来，登记成口袋集。"""

    spec = ToolSpec(
        name="detect_pockets",
        description=(
            "把一个受体的**结合口袋**列出来，登记成口袋集。四层来源：这份结构自带的"
            "共晶配体 → 用户给的先验编号（candidates）→ 该蛋白的复合物（uniprot）→ "
            "p2rank 预测（pdb_id）。中心、半径与盒子一律现算，**不接受模型传的坐标**。\n"
            "sources=auto（默认）按上述顺序取**第一个非空的来源**，不会白跑网络；"
            "要叠加来源就显式写 `ligand,complex`。\n"
            "空集不是失败：会分清「查了确实没有」与「没查成」，并说明缺哪个参数才能再试。\n"
            "下一步用 define_pocket 选一处，把它的 handle 交给 prepare_receptor 的 pocket。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "receptor": {
                    "type": "string",
                    "description": "受体 handle（raw 或 prepared 都收），例如 receptor#1",
                },
                "radius": {
                    "type": "number",
                    "description": (
                        f"口袋残基判据（埃），默认 {POCKET_RADIUS:g}，"
                        f"范围 {MIN_POCKET_RADIUS:g}–{MAX_POCKET_RADIUS:g}"
                    ),
                },
                "padding": {
                    "type": "number",
                    "description": (
                        f"由中心算盒子时四周留多少埃，默认 {DEFAULT_POCKET_PADDING:g}，"
                        f"上限 {MAX_POCKET_PADDING:g}"
                    ),
                },
                "sources": {
                    "type": "string",
                    "description": (
                        "来源，逗号分隔：auto / ligand / complex / prior / predicted。"
                        "auto 只取第一个非空的来源"
                    ),
                },
                "uniprot": {
                    "type": "string",
                    "description": "蛋白编号（如 P00519），复合物来源要用；不给就只能看手上这份结构",
                },
                "candidates": {
                    "type": "string",
                    "description": "先验清单：PDB 编号，逗号或空格分隔（如 \"1M17,2ITY\"）",
                },
                "pdb_id": {
                    "type": "string",
                    "description": "预测来源用的 PDB 编号；不给时沿血缘找，再没有就不走预测",
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多列几处，默认 {MAX_POCKETS_SHOWN}，上限 {MAX_POCKETS_LISTED}",
                },
            },
            "required": ["receptor"],
            "additionalProperties": False,
        },
    )
    primary_argument = "receptor"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：detect_pockets 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw = str(arguments.get("receptor") or "").strip()
        if not raw:
            return ToolResult(
                output="缺少参数 receptor。先 list_workspace 看清有哪份受体。",
                failed=True,
            )
        try:
            entry = ledger.get(raw)
            expect_handle(entry, ReceptorHandle, "receptor")
        except Exception as error:
            return ToolResult(output=str(error), failed=True)

        try:
            radius = _number_argument(
                arguments, "radius", POCKET_RADIUS, MIN_POCKET_RADIUS, MAX_POCKET_RADIUS
            )
            padding = _number_argument(
                arguments, "padding", DEFAULT_POCKET_PADDING, 0.0, MAX_POCKET_PADDING
            )
            limit = _int_argument(
                arguments, "limit", MAX_POCKETS_SHOWN, 1, MAX_POCKETS_LISTED
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        sources_raw = str(arguments.get("sources") or "auto").strip().lower()
        uniprot = str(arguments.get("uniprot") or "").strip()
        candidates = _split_tokens(arguments.get("candidates"))
        pdb_id_raw = str(arguments.get("pdb_id") or "").strip()
        pdb_id = _inherited_meta(ledger, entry, "pdb_id")
        if pdb_id_raw:
            try:
                pdb_id = structure.normalize_pdb_id(pdb_id_raw)
            except ValueError as error:
                return ToolResult(output=str(error), failed=True)
        if pdb_id:
            try:
                pdb_id = structure.normalize_pdb_id(pdb_id)
            except ValueError:
                pdb_id = ""

        if sources_raw == "auto":
            plan = ["ligand"]
            if candidates:
                plan.append("prior")
            if uniprot:
                plan.append("complex")
            if pdb_id:
                plan.append("predicted")
            auto = True
        else:
            plan = [token.lower() for token in _split_tokens(sources_raw)]
            unknown = [token for token in plan if token not in KNOWN_LAYERS]
            if unknown:
                return ToolResult(
                    output=(
                        f"sources 里有不认识的值：{'、'.join(unknown)}。"
                        f"只能是 {' / '.join(KNOWN_LAYERS)}，或 auto"
                    ),
                    failed=True,
                )
            if not plan:
                return ToolResult(output="sources 不能是空的。", failed=True)
            auto = False

        try:
            coordinate = _coordinate_of(entry)
            text = coordinate.read_text(encoding="utf-8", errors="replace")
            doc = pdbtext.parse(text)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        except OSError as error:
            return ToolResult(
                output=f"读受体文件失败（{type(error).__name__}：{error}）", failed=True
            )
        except pdbtext.PdbEditError as error:
            return ToolResult(
                output=f"受体坐标无法按标准 PDB 列位解析：{error}", failed=True
            )

        facts = structure.parse_pdb(text, pdb_id=pdb_id)
        chains = _chains_of(entry, facts)

        collected: list[dict[str, Any]] = []
        notes: list[str] = []
        for layer in plan:
            if auto and collected:
                break
            bundle = await self._run_layer(
                layer,
                text=text,
                doc=doc,
                entry=entry,
                chains=chains,
                radius=radius,
                uniprot=uniprot,
                candidates=candidates,
                pdb_id=pdb_id,
            )
            if isinstance(bundle, ToolResult):
                return bundle
            if bundle.note:
                notes.append(bundle.note)
            if bundle.pockets:
                collected.extend(
                    _normalize_pockets(
                        bundle.pockets, layer=layer, radius=radius, padding=padding
                    )
                )

        if not collected:
            return ToolResult(
                output=(
                    "没有算出口袋。\n"
                    + "\n".join(f"  · {note}" for note in notes if note)
                    + "\n要给复合物来源就补 uniprot，要给先验来源就补 candidates，"
                    "要给预测来源就补 pdb_id。"
                ),
                meta={"count": 0, "layers": plan},
            )

        _sort_pockets(collected)
        params = {
            "receptor": str(entry.fingerprint or entry.path or ""),
            "radius": radius,
            "padding": padding,
            "sources": plan,
            "uniprot": uniprot,
            "candidates": candidates,
            "pdb_id": pdb_id,
            "format": 1,
        }
        stem = f"{entry.handle}-pockets"
        try:
            target, reused = products.content_dir(
                pockets_dir(), stem, params, (POCKET_SET_FILE, SET_PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(
                output=f"口袋产物目录失败（{type(error).__name__}：{error}）", failed=True
            )

        if reused:
            try:
                payload = products.read_json(target / POCKET_SET_FILE)
            except (OSError, ValueError) as error:
                return ToolResult(
                    output=f"复用旧产物时读 {POCKET_SET_FILE} 失败（{type(error).__name__}：{error}）",
                    failed=True,
                )
            pockets = payload.get("pockets") or []
            if not pockets:
                return ToolResult(
                    output=f"旧产物 {target} 里没有口袋，删掉它重跑一次。", failed=True
                )
            fingerprint = fingerprint_path(target)
            handle, ledger_reused = _register_set(
                ledger,
                target=target,
                fingerprint=fingerprint,
                summary=_set_summary(pockets),
                pockets=pockets,
                receptor=entry.handle,
                params=params,
            )
            return ToolResult(
                output=_render_pockets(
                    handle=handle,
                    pockets=pockets,
                    total=len(pockets),
                    shown=limit,
                    reused=True,
                    note="",
                ),
                meta={
                    "handle": handle,
                    "count": len(pockets),
                    "reused": ledger_reused,
                },
            )

        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "pockets",
            "receptor": {
                "handle": str(entry.handle),
                "fingerprint": str(entry.fingerprint or ""),
            },
            "params": params,
            "pockets": collected,
            "empty_reason": "",
        }
        try:
            products.write_json_atomic(target / POCKET_SET_FILE, payload)
            # 最后写：缺它说明上一次写在中间崩了，不能当完整产物复用。
            products.write_json_atomic(target / SET_PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(
                output=f"写口袋产物失败（{type(error).__name__}：{error}）", failed=True
            )

        fingerprint = fingerprint_path(target)
        handle, ledger_reused = _register_set(
            ledger,
            target=target,
            fingerprint=fingerprint,
            summary=_set_summary(collected),
            pockets=collected,
            receptor=entry.handle,
            params=params,
        )
        return ToolResult(
            output=_render_pockets(
                handle=handle,
                pockets=collected,
                total=len(collected),
                shown=limit,
                reused=False,
                note="；".join(note for note in notes if note),
            ),
            meta={
                "handle": handle,
                "count": len(collected),
                "reused": ledger_reused,
            },
        )

    async def _run_layer(
        self,
        layer: str,
        *,
        text: str,
        doc: pdbtext.PdbDocument,
        entry: Any,
        chains: tuple[str, ...],
        radius: float,
        uniprot: str,
        candidates: Sequence[str],
        pdb_id: str,
    ) -> PocketBundle | ToolResult:
        """跑一层来源。缺参数时返回一条可操作的失败，不静默跳过。"""
        if layer == "ligand":
            ligand_text = ""
            meta = getattr(entry, "meta", {}) or {}
            ligand_file = str(meta.get("ligand_file") or "").strip()
            path = getattr(entry, "path", None)
            if ligand_file and path is not None and Path(path).is_dir():
                candidate = Path(path) / ligand_file
                if candidate.is_file():
                    try:
                        ligand_text = candidate.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        ligand_text = ""
            if ligand_text:
                return _ligand_bundle(
                    doc,
                    ligand_text=ligand_text,
                    ligand_label=str(meta.get("ligand_label") or ""),
                    chains=chains,
                    radius=radius,
                    pdb_id=pdb_id,
                )
            return _structure_bundle(
                text, chains=chains, radius=radius, pdb_id=pdb_id
            )

        if layer == "complex":
            if not uniprot:
                return ToolResult(
                    output=(
                        "要走复合物来源就得给 uniprot（如 P00519）。"
                        "不知道编号就先 search_targets 查。"
                    ),
                    failed=True,
                )
            return complex_pockets(
                uniprot=uniprot, receptor_chains=chains, radius=radius
            )

        if layer == "prior":
            if not candidates:
                return ToolResult(
                    output=(
                        "要走先验来源就得给 candidates（PDB 编号清单）。"
                        "编号要来自用户或 search_pdb 的结果，不要凭记忆写。"
                    ),
                    failed=True,
                )
            bundle, verdicts = prior_pockets(
                receptor_text=text,
                candidates=candidates,
                receptor_chains=chains,
                radius=radius,
            )
            rejected = [item for item in verdicts if not item.ok]
            if rejected:
                bundle.note += "；剔除：" + "、".join(
                    f"{item.pdb_id}（{item.state}）" for item in rejected[:5]
                )
            return bundle

        if layer == "predicted":
            if not pdb_id:
                return ToolResult(
                    output=(
                        "要走预测来源就得给 pdb_id（或让受体自己带着编号）。"
                        "预测只在前几层都为空时才有意义——有实验事实时不该用预测。"
                    ),
                    failed=True,
                )
            return p2rank_pockets(
                pdb_id=pdb_id, receptor_chains=chains, radius=radius
            )

        return ToolResult(output=f"不认识的来源：{layer}", failed=True)


def _parse_residue_tokens(
    doc: pdbtext.PdbDocument, tokens: Sequence[str]
) -> tuple[list[pdbtext.ResidueKey], list[str]]:
    """把 `A:315` / `A:315A` 解析成残基键。

    序号与插入码分开匹配：`A:315A` 是「315 号、插入码 A」，与 `A:315` 不是同一个
    残基。只给 `A:315` 而实际只有 `315A` 时**不自动猜**——列出同号的候选，
    让调用方自己说清是哪一个。
    """
    names: dict[pdbtext.ResidueKey, str] = {}
    for atom in doc.atoms:
        names.setdefault(atom.key, atom.resname)

    exact = {str(key): key for key in names}
    by_number: dict[tuple[str, str], list[pdbtext.ResidueKey]] = {}
    for key in names:
        by_number.setdefault((key.chain, key.number), []).append(key)

    resolved: list[pdbtext.ResidueKey] = []
    problems: list[str] = []
    for token in tokens:
        key = exact.get(token)
        if key is None and ":" in token:
            chain, _, number = token.partition(":")
            candidates = by_number.get((chain, number), [])
            if len(candidates) == 1:
                key = candidates[0]
            elif len(candidates) > 1:
                listed = "、".join(str(item) for item in candidates)
                problems.append(f"{token} 有多个插入码：{listed}（请写全，如 {listed.split('、')[0]}）")
                continue
        if key is None:
            problems.append(f"{token} 在受体里不存在")
            continue
        resolved.append(key)
    return resolved, problems


def _chain_ranges(doc: pdbtext.PdbDocument) -> str:
    bounds: dict[str, tuple[str, str]] = {}
    for atom in doc.atoms:
        if atom.is_hetero:
            continue
        current = bounds.get(atom.chain)
        if current is None:
            bounds[atom.chain] = (atom.number, atom.number)
        else:
            low, high = current
            bounds[atom.chain] = (min(low, atom.number), max(high, atom.number))
    return "、".join(f"{chain or '·'} {low}–{high}" for chain, (low, high) in bounds.items())


def _residue_atoms(
    doc: pdbtext.PdbDocument, keys: Sequence[pdbtext.ResidueKey]
) -> tuple[tuple[float, float, float], ...]:
    wanted = set(keys)
    return tuple(
        atom.coord
        for atom in doc.atoms
        if atom.key in wanted and not atom.is_hetero and pdbtext.is_heavy(atom)
    )


def _vector(raw: str, *, label: str) -> tuple[float, float, float]:
    parts = [part for part in re.split(r"[,\s]+", raw.strip()) if part]
    if len(parts) != 3:
        raise ToolError(f"{label} 要三个数（x,y,z），收到的是 {raw!r}")
    values: list[float] = []
    for part in parts:
        try:
            values.append(float(part))
        except ValueError:
            raise ToolError(f"{label} 里的 {part!r} 不是数字") from None
    return (values[0], values[1], values[2])


def _register_pocket(
    ledger: SessionLedger,
    *,
    target: Path,
    fingerprint: str,
    summary: str,
    receptor: ReceptorHandle,
    pocket: dict[str, Any],
    source: str,
) -> tuple[str, bool]:
    existing = ledger.find(
        kind="artifact", fingerprint=fingerprint, include_archived=True
    )
    for candidate in existing:
        if str(candidate.meta.get("kind") or "") == "pocket":
            return str(candidate.handle), True
    entry = ledger.add(
        "artifact",
        path=target,
        summary=summary,
        fingerprint=fingerprint,
        parents=[receptor],
        meta={
            "kind": "pocket",
            "stage": "pocket",
            "source": source,
            "center": pocket.get("center"),
            "size": (pocket.get("box") or {}).get("size"),
            "residue_count": len(pocket.get("residues") or []),
            "receptor": str(receptor),
        },
    )
    return str(entry.handle), False


class DefinePocketTool(Tool):
    """把一处口袋定义成可复用的对象。"""

    spec = ToolSpec(
        name="define_pocket",
        description=(
            "从口袋集里选一处，或用残基 / 显式坐标定义一处，登记成**单个口袋**，"
            "供 prepare_receptor 的 pocket 参数使用。\n"
            "三路互斥：pockets+index（选现成的）/ residues（链:序号，逗号分隔）/ "
            "center+size（显式坐标）。同时给两路会直接报错。\n"
            "**盒子由代码算**：残基那一路取残基重原子的包围盒再四周外扩 padding，"
            "不接受模型心算的边长。\n"
            "residues 里写错一个残基会被列出来（连同该链的实际编号范围），不会静默丢掉。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pockets": {
                    "type": "string",
                    "description": "detect_pockets 的口袋集 handle，例如 artifact#3",
                },
                "index": {
                    "type": "integer",
                    "description": "口袋集里的序号（从 1 开始）",
                },
                "receptor": {
                    "type": "string",
                    "description": "受体 handle；用 residues / center+size 时必填",
                },
                "residues": {
                    "type": "string",
                    "description": "残基清单，链:序号，逗号或空格分隔，例如 \"A:315,A:319,A:330\"",
                },
                "center": {
                    "type": "string",
                    "description": "盒子中心，例如 \"12.3,4.5,8.9\"，与 size 成对给",
                },
                "size": {
                    "type": "string",
                    "description": "盒子边长（埃），例如 \"22,18,20\"，与 center 成对给",
                },
                "padding": {
                    "type": "number",
                    "description": (
                        f"由残基算盒子时四周留多少埃，默认 {DEFAULT_POCKET_PADDING:g}，"
                        f"上限 {MAX_POCKET_PADDING:g}"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "显示名，例如 ATP 位点",
                },
            },
            "additionalProperties": False,
        },
    )
    primary_argument = "pockets"
    evidence_key = ("receptor",)
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：define_pocket 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )

        raw_pockets = str(arguments.get("pockets") or "").strip()
        raw_residues = str(arguments.get("residues") or "").strip()
        center_raw = str(arguments.get("center") or "").strip()
        size_raw = str(arguments.get("size") or "").strip()
        index_raw = arguments.get("index")
        raw_receptor = str(arguments.get("receptor") or "").strip()
        name = str(arguments.get("name") or "").strip()

        if bool(center_raw) != bool(size_raw):
            return ToolResult(
                output="center 与 size 必须成对给：只给一个算不出盒子。", failed=True
            )
        manual = bool(raw_residues) or bool(center_raw)
        if raw_pockets and manual:
            return ToolResult(
                output="pockets 与 residues / center+size 只能二选一：前者是选现成的，后者是现定义。",
                failed=True,
            )
        if not raw_pockets and not manual:
            return ToolResult(
                output=(
                    "三路要给一路：pockets+index（选现成的）/ residues（链:序号）/ "
                    "center+size（显式坐标）。先用 detect_pockets 看有哪些口袋。"
                ),
                failed=True,
            )
        if raw_pockets and index_raw is None:
            return ToolResult(
                output="给了 pockets 还要给 index（从 1 开始），说明选哪一处。",
                failed=True,
            )
        if index_raw is not None and not raw_pockets:
            return ToolResult(
                output="index 只能和 pockets 一起用；现定义口袋请用 residues 或 center+size。",
                failed=True,
            )

        try:
            padding = _number_argument(
                arguments, "padding", DEFAULT_POCKET_PADDING, 0.0, MAX_POCKET_PADDING
            )
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)

        receptor_entry = None
        pockets_entry = None
        if raw_pockets:
            try:
                pockets_entry = ledger.get(raw_pockets)
                expect_handle(pockets_entry, ArtifactHandle, "pockets")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)
            if str(pockets_entry.meta.get("kind") or "") != "pockets":
                return ToolResult(
                    output=(
                        f"{pockets_entry.handle} 不是口袋集（meta.kind="
                        f"{pockets_entry.meta.get('kind') or '空'}）。"
                        "先用 detect_pockets 算一份。"
                    ),
                    failed=True,
                )
            for parent in pockets_entry.parents:
                if isinstance(parent, ReceptorHandle):
                    receptor_entry = ledger.get(parent)
                    break
            if receptor_entry is None:
                return ToolResult(
                    output=f"{pockets_entry.handle} 没有挂受体，找不到它的坐标来源。",
                    failed=True,
                )
        else:
            if not raw_receptor:
                return ToolResult(
                    output="用 residues / center+size 时要给 receptor（这份口袋属于哪条受体）。",
                    failed=True,
                )
            try:
                receptor_entry = ledger.get(raw_receptor)
                expect_handle(receptor_entry, ReceptorHandle, "receptor")
            except Exception as error:
                return ToolResult(output=str(error), failed=True)

        try:
            coordinate = _coordinate_of(receptor_entry)
            text = coordinate.read_text(encoding="utf-8", errors="replace")
            doc = pdbtext.parse(text)
        except ToolError as error:
            return ToolResult(output=str(error), failed=True)
        except OSError as error:
            return ToolResult(
                output=f"读受体文件失败（{type(error).__name__}：{error}）", failed=True
            )
        except pdbtext.PdbEditError as error:
            return ToolResult(
                output=f"受体坐标无法按标准 PDB 列位解析：{error}", failed=True
            )

        facts = structure.parse_pdb(text)
        chains = _chains_of(receptor_entry, facts)
        source = ""
        pocket: dict[str, Any] = {}

        if raw_pockets:
            try:
                index = int(index_raw)
            except (TypeError, ValueError):
                return ToolResult(output="index 应当是整数（从 1 开始）。", failed=True)
            if pockets_entry.path is None or not Path(pockets_entry.path).is_dir():
                return ToolResult(
                    output=(
                        f"{pockets_entry.handle} 的产物目录不在了（{pockets_entry.path}）。"
                        "重新跑一次 detect_pockets。"
                    ),
                    failed=True,
                )
            try:
                payload = products.read_json(Path(pockets_entry.path) / POCKET_SET_FILE)
            except (OSError, ValueError) as error:
                return ToolResult(
                    output=(
                        f"读口袋集失败（{type(error).__name__}：{error}）。"
                        "重新跑一次 detect_pockets。"
                    ),
                    failed=True,
                )
            pockets = payload.get("pockets") or []
            chosen = next(
                (
                    item
                    for item in pockets
                    if isinstance(item, dict) and int(item.get("index") or 0) == index
                ),
                None,
            )
            if chosen is None:
                return ToolResult(
                    output=(
                        f"{pockets_entry.handle} 里没有第 {index} 处口袋；"
                        f"现有 {len(pockets)} 处（1–{len(pockets)}）。"
                    ),
                    failed=True,
                )
            pocket = dict(chosen)
            source = str(chosen.get("source") or "ligand")
            pocket["defined_from"] = str(pockets_entry.handle)

        elif raw_residues:
            tokens = _split_tokens(raw_residues)
            if not tokens:
                return ToolResult(output="residues 不能是空的。", failed=True)
            keys, problems = _parse_residue_tokens(doc, tokens)
            if problems:
                return ToolResult(
                    output=(
                        "这些残基对不上：\n  · "
                        + "\n  · ".join(problems)
                        + f"\n这份受体的链与编号范围：{_chain_ranges(doc) or '（没有聚合物残基）'}"
                    ),
                    failed=True,
                )
            coords = _residue_atoms(doc, keys)
            if not coords:
                return ToolResult(
                    output="这些残基上没有重原子，算不出盒子。", failed=True
                )
            try:
                center, size = pdbtext.calc_box(coords, padding)
            except pdbtext.PdbEditError as error:
                return ToolResult(output=str(error), failed=True)
            residues: list[dict[str, Any]] = []
            names = {atom.key: atom.resname for atom in doc.atoms}
            for key in keys:
                entry_dict: dict[str, Any] = {
                    "chain": key.chain,
                    "resseq": int(key.number) if key.number.isdigit() else key.number,
                    "resname": names.get(key, ""),
                }
                if key.icode:
                    entry_dict["icode"] = key.icode
                residues.append(entry_dict)
            pocket = {
                "center": [round(value, 3) for value in center],
                "radius": 0.0,
                "source": "residues",
                "layer": "manual",
                "residues": residues,
                "support": {"complexes": 0, "pdb_ids": [], "ligands": []},
                "box": {
                    "center": [round(value, 3) for value in center],
                    "size": [round(value, 3) for value in size],
                    "source": "residues",
                },
            }
            source = "residues"

        else:
            try:
                center = _vector(center_raw, label="center")
                size = _vector(size_raw, label="size")
            except ToolError as error:
                return ToolResult(output=str(error), failed=True)
            if any(value <= 0 for value in size):
                return ToolResult(
                    output="size 的三个数都要大于 0（单位是埃）。", failed=True
                )
            pocket = {
                "center": [round(value, 3) for value in center],
                "radius": 0.0,
                "source": "explicit",
                "layer": "manual",
                "residues": [],
                "support": {"complexes": 0, "pdb_ids": [], "ligands": []},
                "box": {
                    "center": [round(value, 3) for value in center],
                    "size": [round(value, 3) for value in size],
                    "source": "explicit",
                },
            }
            source = "explicit"

        if name:
            pocket["name"] = name
        pocket["receptor"] = str(receptor_entry.handle)
        pocket["chains"] = list(chains)

        params = {
            "receptor": str(receptor_entry.fingerprint or ""),
            "source": source,
            "center": pocket["center"],
            "size": pocket["box"]["size"],
            "residues": [
                f"{item.get('chain')}:{item.get('resseq')}{item.get('icode') or ''}"
                for item in pocket["residues"]
            ],
            "defined_from": pocket.get("defined_from") or "",
            "format": 1,
        }
        stem = f"{receptor_entry.handle}-{name or source}-pocket"
        try:
            target, reused = products.content_dir(
                pockets_dir(), stem, params, (POCKET_FILE, SET_PARAMS_FILE)
            )
        except OSError as error:
            return ToolResult(
                output=f"口袋产物目录失败（{type(error).__name__}：{error}）", failed=True
            )

        if reused:
            try:
                payload = products.read_json(target / POCKET_FILE)
            except (OSError, ValueError) as error:
                return ToolResult(
                    output=f"复用旧产物时读 {POCKET_FILE} 失败（{type(error).__name__}：{error}）",
                    failed=True,
                )
            pocket = payload
            fingerprint = fingerprint_path(target)
            handle, ledger_reused = _register_pocket(
                ledger,
                target=target,
                fingerprint=fingerprint,
                summary=str(pocket.get("summary") or "口袋"),
                receptor=receptor_entry.handle,
                pocket=pocket,
                source=source,
            )
            return self._render(handle, pocket, reused=True, ledger_reused=ledger_reused)

        pocket["summary"] = self._summary(pocket, source, name)
        target.mkdir(parents=True, exist_ok=True)
        try:
            products.write_json_atomic(target / POCKET_FILE, pocket)
            products.write_json_atomic(target / SET_PARAMS_FILE, params)
        except OSError as error:
            return ToolResult(
                output=f"写口袋产物失败（{type(error).__name__}：{error}）", failed=True
            )

        fingerprint = fingerprint_path(target)
        handle, ledger_reused = _register_pocket(
            ledger,
            target=target,
            fingerprint=fingerprint,
            summary=pocket["summary"],
            receptor=receptor_entry.handle,
            pocket=pocket,
            source=source,
        )
        return self._render(handle, pocket, reused=False, ledger_reused=ledger_reused)

    @staticmethod
    def _summary(pocket: dict[str, Any], source: str, name: str) -> str:
        center = pocket.get("center") or [0.0, 0.0, 0.0]
        size = (pocket.get("box") or {}).get("size") or [0.0, 0.0, 0.0]
        head = f"口袋 · {name}" if name else f"口袋 · {source}"
        return (
            f"{head} · 中心 ({center[0]:g}, {center[1]:g}, {center[2]:g})"
            f" · 盒子 {size[0]:g}×{size[1]:g}×{size[2]:g}"
            f" · {len(pocket.get('residues') or [])} 个残基"
        )

    @staticmethod
    def _render(
        handle: str, pocket: dict[str, Any], *, reused: bool, ledger_reused: bool
    ) -> ToolResult:
        center = pocket.get("center") or [0.0, 0.0, 0.0]
        size = (pocket.get("box") or {}).get("size") or [0.0, 0.0, 0.0]
        residues = pocket.get("residues") or []
        head = (
            f"登记表里已有同一处口袋：{handle}（未重复登记）。"
            if reused
            else f"已定义口袋，登记为 {handle}。"
        )
        lines = [
            head,
            f"中心 ({center[0]:g}, {center[1]:g}, {center[2]:g})，"
            f"盒子 {size[0]:g}×{size[1]:g}×{size[2]:g}，{len(residues)} 个残基，"
            f"来源 {pocket.get('source')}。",
        ]
        if residues:
            shown = "、".join(
                f"{item.get('chain')}:{item.get('resseq')}"
                for item in residues[:8]
            )
            rest = len(residues) - 8
            lines.append(f"残基：{shown}" + (f" 等 {len(residues)} 个" if rest > 0 else ""))
        lines.append(
            f"下一步：prepare_receptor(structure=<raw 受体>, pocket={handle}, confirm=...)"
        )
        return ToolResult(
            output="\n".join(lines),
            meta={
                "handle": handle,
                "reused": ledger_reused,
                "center": list(center),
                "size": list(size),
                "residue_count": len(residues),
            },
        )


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [DetectPocketsTool(), DefinePocketTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
