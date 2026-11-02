"""按靶点铺开全部实验结构：先看清有哪些，再谈选哪几套。

`ses_0014` 用 `search_pdb(uniprot=Q15858)` 拿到「命中 44 条，列出 3 条」，后面的判断
都建在这 3 条上：它按 `ligand=TTX`、`ligand=STX` 各查一次，两次都是「查了，确实没有」，
于是写下「TTX 与 STX 共晶各 0 条」，整条孔道外口路线建立在「没有参考构象」上。而同一个
靶点下有 3 套河豚毒素（代号 `9SR`）与 4 套石房蛤毒素（代号 `9SL`）——那两个字符串是
名称，不是代号。

`ses_0002` 是同一个毛病的另一种形状：`.vsmemory` 里写着「51 个实验结构，已取回并核对
4 个候选」，之后读到这句的会话就把 4 当成了全集。

毛病出在**没有任何一步要求它看见全集**：`fetch_target` 的摘要只列 3 套，
`assess_campaign` 只报套数与最佳分辨率。这个工具补的就是那一步——一次把全部结构取回、
按配体分组、把每组的套数与最佳分辨率摆出来。

三条边界：

1. **不下载坐标**。看的是「有哪些」，不是「哪一套能用」。
2. **不算口袋、不核对序列**。那是结构确认点与口袋那一步的事。
3. **不宣称某一组就是真实药物位点**，只写「这一组是候选，口袋那一步确认」。

**常驻，不登记进 `FAMILIES`**：宽泛问题发生在第一轮，那时一族都还没装；现装族会改动
工具清单，把前缀缓存整段打掉。注册位置在 `app.py:_build_registry`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..ledger import SessionLedger
from ..tools import Tool, ToolResult, ToolSpec
from ...paths import SURVEY_DIRECTORY, config_home
from . import export, products, rsearch, uniprot

__all__ = [
    "CHAT_GROUPS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SURVEY_FILE",
    "SurveyStructuresTool",
    "domain_tools",
    "surveys_dir",
]

SURVEY_FILE = "survey.json"
SURVEY_PARAMS_FILE = "params.json"
SURVEY_FORMAT = 1

DEFAULT_LIMIT = 400
MAX_LIMIT = 1000
"""一次最多取回多少套的明细。

默认值要能覆盖绝大多数靶点的全部结构：EGFR 在 RCSB 里有 394 套，按 100 取回等于
把「全景」又做成了抽样——那正是这个工具要修的毛病。取回的是元数据，一套几百字节；
配体常用名按代号缓存，第二次起不再联网。

上限 1000 是给「一个靶点被结构生物学扫了一遍」那种情况留的余量；超过之后正文会
写明还剩多少套，并建议按 `method` 或 `ligand_class` 收一收。
"""

CHAT_GROUPS = 6
"""聊天摘要里最多列几组配体。

其余折成一行：前十个带套数，再往后只写「还有 N 组见页面」。全表在页面里——
摘要是给人一眼看个大概，不是给人做判断的唯一依据。
"""

WATER_CODES = frozenset({"HOH", "DOD", "WAT"})
"""水。RCSB 的 `nonpolymer_entities` 通常已经不含水，这里留一道保险。"""

_METHOD_SHORT = {
    "X-RAY DIFFRACTION": "X-RAY",
    "ELECTRON MICROSCOPY": "EM",
    "NEUTRON DIFFRACTION": "NEUTRON",
    "SOLUTION NMR": "NMR",
    "SOLID-STATE NMR": "SSNMR",
}

_NAME_LIMIT = 52


def surveys_dir() -> Path:
    """结构全景产物的根目录。与口袋产物、状态文件同处一个家目录。"""
    return config_home() / SURVEY_DIRECTORY


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------
@dataclass
class _Group:
    """一组配体（按 `chem_comp` 代号）。"""

    code: str
    name: str = ""
    kind: str = ""
    weight: float | None = None
    pdb_ids: list[str] = field(default_factory=list)
    best_id: str = ""
    best_resolution: float | None = None
    best_method: str = ""
    names: list[str] = field(default_factory=list)
    title_hits: int = 0
    """有几套的**标题里点了这个配体的名**。

    膜蛋白的晶体/电镜结构里，胆固醇、磷脂、去垢剂按套数永远排在最前（Nav1.7 的
    44 套里胆固醇占 26 套），按套数排序会把真正被研究的配体挤到看不见的地方。
    标题是最便宜也最可靠的区分：研究者在标题里写的是「和河豚毒素的复合物」，
    不会写「和胆固醇的复合物」。
    """

    def add(self, hit: rsearch.PdbHit) -> None:
        self.pdb_ids.append(hit.pdb_id)
        if not self.best_id:
            self.best_id = hit.pdb_id
            self.best_method = hit.method
        if hit.resolution is None:
            return
        if self.best_resolution is None or hit.resolution < self.best_resolution:
            self.best_resolution = hit.resolution
            self.best_id = hit.pdb_id
            self.best_method = hit.method

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "names": list(self.names),
            "kind": self.kind,
            "weight": self.weight,
            "count": len(self.pdb_ids),
            "title_hits": self.title_hits,
            "best": {
                "pdb_id": self.best_id,
                "resolution": self.best_resolution,
                "method": self.best_method,
            },
            "pdb_ids": list(self.pdb_ids),
        }


def _is_additive(code: str) -> bool:
    upper = code.upper()
    return upper in export.BUFFER_LIKE or upper in export.MONATOMIC_IONS


def _short(text: str, limit: int = _NAME_LIMIT) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _method(text: str) -> str:
    upper = str(text or "").strip().upper()
    return _METHOD_SHORT.get(upper, _short(text, 14))


def _weight(value: float | None) -> str:
    return f"{value:.1f} Da" if value is not None else "分子量未知"


def _classify(
    hits: Sequence[rsearch.PdbHit],
    *,
    accession: str,
    ligand_class: str,
) -> dict[str, Any]:
    """把取回的条目分成：小分子配体组、其他聚合物链、添加剂与离子、无配体。

    **一套结构可以出现在多组**：6J8I 同时有 `9SR` 与 `NAG`。组内计数之和因此
    大于结构总数，摘要里要写明这一点，否则读者会以为结构数量对不上。
    """
    groups: dict[str, _Group] = {}
    additives: dict[str, dict[str, Any]] = {}
    polymers: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    apo: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    missing: list[str] = []

    for hit in hits:
        if not hit.title and not hit.comps and not hit.polymers:
            # 明细没取到。**不能当成无配体**：两者对「有没有参考构象」的含义相反。
            missing.append(hit.pdb_id)
            continue

        target_present = any(accession in chain.uniprot_ids for chain in hit.polymers)
        others = (
            [
                chain
                for chain in hit.polymers
                if accession not in chain.uniprot_ids
            ]
            if target_present
            else []
        )
        small = [
            comp
            for comp in hit.comps
            if comp.code.upper() not in WATER_CODES and not _is_additive(comp.code)
        ]
        additive = [
            comp
            for comp in hit.comps
            if comp.code.upper() not in WATER_CODES and _is_additive(comp.code)
        ]

        has_small = bool(small)
        has_other = bool(others)
        if ligand_class == "small_molecule" and not has_small:
            continue
        if ligand_class == "other_polymer" and not has_other:
            continue
        if ligand_class == "apo" and (has_small or has_other):
            continue

        for comp in small:
            group = groups.get(comp.code)
            if group is None:
                group = _Group(
                    code=comp.code, name=comp.name, kind=comp.kind, weight=comp.weight
                )
                groups[comp.code] = group
            group.add(hit)

        for comp in additive:
            row = additives.setdefault(
                comp.code, {"code": comp.code, "name": comp.name, "count": 0}
            )
            row["count"] += 1

        for chain in others:
            key = (chain.description, chain.uniprot_ids)
            row = polymers.setdefault(
                key,
                {
                    "description": chain.description,
                    "uniprot_ids": list(chain.uniprot_ids),
                    "count": 0,
                    "pdb_ids": [],
                },
            )
            row["count"] += 1
            row["pdb_ids"].append(hit.pdb_id)

        if not has_small and not has_other:
            apo.append(
                {
                    "pdb_id": hit.pdb_id,
                    "resolution": hit.resolution,
                    "method": hit.method,
                }
            )

        entries.append(
            {
                "pdb_id": hit.pdb_id,
                "title": hit.title,
                "resolution": hit.resolution,
                "method": hit.method,
                "ligands": [comp.code for comp in small],
                "additives": [comp.code for comp in additive],
                "other_polymers": [chain.description for chain in others],
            }
        )

    ordered = sorted(
        groups.values(),
        key=lambda item: (
            -len(item.pdb_ids),
            item.best_resolution if item.best_resolution is not None else 1e9,
            item.code,
        ),
    )
    return {
        "groups": ordered,
        "polymers": sorted(polymers.values(), key=lambda row: -row["count"]),
        "additives": sorted(additives.values(), key=lambda row: -row["count"]),
        "apo": sorted(
            apo,
            key=lambda row: (
                row["resolution"] if row["resolution"] is not None else 1e9,
                row["pdb_id"],
            ),
        ),
        "entries": entries,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _mark_titles(groups: Sequence[_Group], entries: Sequence[dict[str, Any]]) -> None:
    """数一数每组的配体名在标题里出现过几次。

    只认长度 5–48 的名字：太短容易误中（`NA`、`SO4` 这种本来也在添加剂那一档），
    太长的是系统命名，标题里不会出现。
    """
    titles = {entry["pdb_id"]: entry["title"].lower() for entry in entries}
    for group in groups:
        needles = [
            name.lower()
            for name in group.names
            if 5 <= len(name) <= 48 and not name.lower().startswith("(")
        ]
        if not needles:
            group.title_hits = 0
            continue
        group.title_hits = sum(
            1
            for pdb_id in group.pdb_ids
            if any(needle in titles.get(pdb_id, "") for needle in needles)
        )


def _group_line(group: _Group, *, mark: str = "-") -> str:
    label = f"{group.code} {_short(group.name)} {_weight(group.weight)}"
    detail = f"{len(group.pdb_ids)} 套"
    if group.best_resolution is not None:
        detail += f"，最佳 {group.best_resolution:.2f} Å（{group.best_id}"
        if group.best_method:
            detail += f"，{_method(group.best_method)}"
        detail += "）"
    elif group.best_id:
        detail += f"，代表 {group.best_id}（无分辨率）"
    return f"{mark} {label}：{detail}"


def _rest_line(groups: Sequence[_Group]) -> str:
    """折叠行。**每组都带套数**，超过十组就只列前十个并指向页面。

    不写「共 N 套」：一套结构常常出现在多组（Nav1.7 的 44 套结构在 36 个配体组里
    覆盖了一百八十多次），把组内计数加起来当成结构数会让人以为总数对不上。
    """
    parts = [f"{group.code} {len(group.pdb_ids)} 套" for group in groups[:10]]
    tail = f"，还有 {len(groups) - 10} 组见页面" if len(groups) > 10 else ""
    return f"- 其余 {len(groups)} 组：{'、'.join(parts)}{tail}"


def _render(
    *,
    accession: str,
    total: int,
    fetched: int,
    ligand_class: str,
    survey: dict[str, Any],
    handle: str,
) -> str:
    groups: list[_Group] = survey["groups"]
    kept = len(survey["entries"])

    head = f"{accession}：RCSB 命中 {total} 套实验结构"
    if total > fetched:
        head += f"，取回 {fetched} 套（其余 {total - fetched} 套没取明细，调大 limit 可取）"
    else:
        head += "，全部取回"
    if ligand_class != "any":
        head += f"；按条件筛出 {kept} 套"

    named = [group for group in groups if group.title_hits]
    others = [group for group in groups if not group.title_hits]
    if named:
        head += "。★ 是标题里点了名的配体（一套结构可能出现在多组）："
    else:
        head += "。按配体分组（一套结构可能出现在多组）："
    lines = [head]

    if named:
        for group in named[:CHAT_GROUPS]:
            lines.append(_group_line(group, mark="★"))
        # 超出的点名组并进折叠行时按套数重排：否则一个只有 1 套的点名组会排到
        # 26 套的胆固醇前面，读起来像排序坏了。
        overflow = named[CHAT_GROUPS:]
        if overflow:
            others = sorted(
                overflow + others,
                key=lambda group: (-len(group.pdb_ids), group.code),
            )
        if others:
            lines.append(_rest_line(others))
    elif others:
        for group in others[:CHAT_GROUPS]:
            lines.append(_group_line(group))
        rest = others[CHAT_GROUPS:]
        if rest:
            lines.append(_rest_line(rest))
    else:
        lines.append("- 没有小分子配体组。")

    if survey["polymers"]:
        parts = []
        for row in survey["polymers"][:6]:
            label = _short(row["description"], 40) or "未命名链"
            if row["uniprot_ids"]:
                label += f"（{'、'.join(row['uniprot_ids'])}）"
            parts.append(f"{label} {row['count']} 套")
        if len(survey["polymers"]) > 6:
            parts.append(f"另有 {len(survey['polymers']) - 6} 条链")
        lines.append("其他聚合物链：" + "；".join(parts))

    if survey["additives"]:
        parts = [f"{row['code']} {row['count']} 套" for row in survey["additives"][:8]]
        if len(survey["additives"]) > 8:
            parts.append(f"另有 {len(survey['additives']) - 8} 种")
        lines.append("添加剂与离子（不当配体看）：" + "、".join(parts))

    if survey["apo"]:
        first = survey["apo"][0]
        tail = ""
        if first["resolution"] is not None:
            tail = f"，最佳 {first['resolution']:.2f} Å（{first['pdb_id']}"
            if first["method"]:
                tail += f"，{_method(first['method'])}"
            tail += "）"
        lines.append(f"无配体：{len(survey['apo'])} 套{tail}")
    else:
        lines.append("无配体：0 套")

    if survey["missing"]:
        lines.append(
            f"明细没取到 {len(survey['missing'])} 套："
            + "、".join(survey["missing"][:8])
            + "（这些不算无配体，要单独确认）"
        )

    unreadable = [group.code for group in survey["groups"] if len(group.name) > 40]
    tail = f"（另有 {len(unreadable)} 个代号只查到系统命名）" if unreadable else ""
    lines.append(f"全表在 {handle}{tail}；页面地址用 open_viewer(run={handle})。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class SurveyStructuresTool(Tool):
    """把一个靶点的全部实验结构铺开，按配体分组。"""

    spec = ToolSpec(
        name="survey_structures",
        description=(
            "把一个靶点的**全部实验结构**铺开：命中多少套、每一套里有什么，按配体分组，"
            "每组给套数、最佳分辨率与代表条目。**这是全景，不是挑几套。**\n\n"
            "用户没有指定具体结构（只说了靶点名、或「看看这个靶点」）时先调它；"
            "用户已经点名某个编号时不必调。\n\n"
            "分组口径：小分子配体按 `chem_comp` 代号分组，附常用名（`9SR` 就是 "
            "Tetrodotoxin）；结晶添加剂、离子、糖单列一节；不属于本靶点的聚合物链"
            "（毒素肽、抗体片段、辅助亚基）单列一节；两者都没有的算无配体。"
            "**一套结构可能出现在多组**，组内计数之和会大于结构总数。\n\n"
            "不做的事：不下载坐标、不算口袋、不核对序列、不判哪个配体算「真配体」。"
            "产物是一份 `kind=survey` 的 artifact，全表在页面里（用 `open_viewer` 取地址）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "accession": {
                    "type": "string",
                    "description": "靶点的 UniProt 编号，例如 Q15858",
                },
                "method": {
                    "type": "string",
                    "description": (
                        "只看某种实验方法，例如 ELECTRON MICROSCOPY、X-RAY DIFFRACTION；"
                        "不给就全部"
                    ),
                },
                "ligand_class": {
                    "type": "string",
                    "enum": ["any", "small_molecule", "other_polymer", "apo"],
                    "description": (
                        "只看某一类结构：any 全部（默认）；small_molecule 含小分子配体"
                        "（已排除离子、缓冲液、糖）；other_polymer 含不属于本靶点的聚合物链；"
                        "apo 两者都没有"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"最多取回多少套的明细，默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}"
                    ),
                },
            },
            "required": ["accession"],
            "additionalProperties": False,
        },
    )
    primary_argument = "accession"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：survey_structures 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            accession = uniprot.normalize_accession(str(arguments.get("accession") or ""))
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        method = str(arguments.get("method") or "").strip()
        ligand_class = str(arguments.get("ligand_class") or "any").strip().lower()
        if ligand_class not in ("any", "small_molecule", "other_polymer", "apo"):
            return ToolResult(
                output="ligand_class 只能是 any / small_molecule / other_polymer / apo。",
                failed=True,
            )
        try:
            requested = int(arguments.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult(output="limit 应当是整数", failed=True)
        limit = max(1, min(requested, MAX_LIMIT))

        try:
            query = rsearch.attribute_query(
                uniprot=accession, method=method, rows=limit
            )
        except rsearch.SearchError as error:
            return ToolResult(output=str(error), failed=True)
        try:
            ids, total, _raw = await asyncio.to_thread(rsearch.search_entries, query)
        except rsearch.SearchError as error:
            return ToolResult(
                output=f"这次没查成（RCSB 属性搜 {accession}）：{error}", failed=True
            )

        if not ids:
            return ToolResult(
                output=(
                    f"RCSB 里 {accession} 没有登记实验结构（命中 0 条）。\n"
                    "（这是「查了没有」，不是「没查成」。下一步考虑 AlphaFold 模型，"
                    "或去邻近受体借配体。）"
                ),
                meta={"count": 0, "total": total, "reason": "checked"},
            )

        wanted = ids[:limit]
        try:
            hits = await asyncio.to_thread(rsearch.fetch_ligands, wanted)
        except rsearch.SearchError as error:
            return ToolResult(
                output=(
                    f"结构清单拿到了 {len(wanted)} 套，但配体明细没取到：{error}\n"
                    "明细取不到就没法分组，这次先不写产物。"
                ),
                failed=True,
            )

        entries = [hits.get(pid, rsearch.PdbHit(pdb_id=pid)) for pid in wanted]
        survey = _classify(entries, accession=accession, ligand_class=ligand_class)

        codes = [group.code for group in survey["groups"]]
        names, missed = await asyncio.to_thread(rsearch.ligand_synonym_map, codes)
        for group in survey["groups"]:
            found = names.get(group.code) or []
            group.names = found or ([group.name] if group.name else [])
            if found:
                group.name = found[0]
        _mark_titles(survey["groups"], survey["entries"])
        # 标题点名过的排前面：膜蛋白结构里胆固醇与磷脂按套数永远最多，
        # 按套数排序会把真正被研究的配体挤到折叠行里（Nav1.7 的 9SR/9SL 就是这样）。
        survey["groups"] = sorted(
            survey["groups"],
            key=lambda group: (
                0 if group.title_hits else 1,
                -group.title_hits,
                -len(group.pdb_ids),
                group.best_resolution if group.best_resolution is not None else 1e9,
                group.code,
            ),
        )

        params = {
            "accession": accession,
            "method": method,
            "ligand_class": ligand_class,
            "limit": limit,
            "format": SURVEY_FORMAT,
        }
        try:
            target, _reused = products.content_dir(
                surveys_dir(),
                f"{accession}-survey",
                params,
                (SURVEY_FILE, SURVEY_PARAMS_FILE),
            )
        except OSError as error:
            return ToolResult(
                output=f"全景产物目录失败（{type(error).__name__}：{error}）", failed=True
            )

        payload = {
            "format": SURVEY_FORMAT,
            "accession": accession,
            "query": {
                "method": method,
                "ligand_class": ligand_class,
                "limit": limit,
            },
            "total": total,
            "fetched": len(wanted),
            "groups": [group.as_dict() for group in survey["groups"]],
            "polymers": survey["polymers"],
            "additives": survey["additives"],
            "apo": survey["apo"],
            "missing": survey["missing"],
            "entries": survey["entries"],
            "names_unavailable": missed,
        }
        products.write_json_atomic(target / SURVEY_FILE, payload)
        products.write_json_atomic(target / SURVEY_PARAMS_FILE, params)

        handle, ledger_reused = self._register(
            ledger,
            target=target,
            survey=survey,
            accession=accession,
            total=total,
        )

        return ToolResult(
            output=_render(
                accession=accession,
                total=total,
                fetched=len(wanted),
                ligand_class=ligand_class,
                survey=survey,
                handle=handle,
            ),
            meta={
                "handle": handle,
                "accession": accession,
                "count": len(survey["entries"]),
                "total": total,
                "groups": len(survey["groups"]),
                "apo": len(survey["apo"]),
                "reused": ledger_reused,
            },
        )

    def _register(
        self,
        ledger: SessionLedger,
        *,
        target: Path,
        survey: dict[str, Any],
        accession: str,
        total: int,
    ) -> tuple[str, bool]:
        """登记成 artifact。**按目录名复用 handle**：同一份全景再跑一次不该多一条。"""
        survey_id = target.name
        for candidate in ledger.find(
            kind="artifact",
            meta_match={"kind": "survey", "survey_id": survey_id},
            include_archived=True,
        ):
            return str(candidate.handle), True
        entry = ledger.add(
            "artifact",
            path=target,
            summary=(
                f"{accession} 的全部实验结构 {total} 套，"
                f"按配体分 {len(survey['groups'])} 组"
            ),
            meta={
                "kind": "survey",
                "survey_id": survey_id,
                "accession": accession,
                "count": len(survey["entries"]),
                "total": total,
            },
            # 全景不属于某个受体：它回答的是「这个靶点有什么」，换靶点时不该被搁置。
            link_current_receptor=False,
        )
        return str(entry.handle), False


def domain_tools() -> list[Tool]:
    return [SurveyStructuresTool()]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
