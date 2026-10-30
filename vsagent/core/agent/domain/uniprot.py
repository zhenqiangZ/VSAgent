"""UniProt 靶点检索。

虚拟筛选的第一步几乎总是同一件事：**把用户嘴里的「靶点」变成一个确定的蛋白**。
「找一批 EGFR 抑制剂」里的 EGFR 至少对应三个东西——基因名、蛋白名，以及某个物种
下的一个 UniProt 编号；而下游每一步（准备受体、对接、比对已知活性）要的都是最后那个。

这组工具负责这段收敛：

1. `search_targets` —— 名称 / 基因 → 候选编号（带物种、长度、是否人工审阅）
2. `fetch_target` —— 编号 → 完整注解（功能、定位、结构域、可用实验结构）

两条口径值得单独说明：

**编号只能来自工具，不能来自模型。** 模型能编出 `P00533` 这种看着非常合理的编号
（而且经常编对），但也能编出一个不存在的，或者编成另一个物种的同名蛋白。所以搜索的
返回值里直接把编号摆出来让它复制，取详情前还会先校验格式。

**结构可用性是这个领域的第一等属性。** 一个没有实验结构的靶点，对接就无从谈起；
AlphaFold 模型可能还救得回来。所以取详情时把 PDB 条目（含分辨率与链覆盖范围）
与 AlphaFold 编号一并带出来——「这个靶点能不能做」主要看这一行。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ledger import SessionLedger, fingerprint_text
from ..tools import Tool, ToolResult, ToolSpec
from . import http

BASE_URL = "https://rest.uniprot.org/uniprotkb"

SEARCH_FIELDS = "accession,id,protein_name,gene_primary,organism_name,organism_id,length"
"""检索时只取这几列。

检索的用途是**挑出候选**，不是下载条目——列表里铺开功能注释既没人看，也白占上下文。
要细节就再取一次详情，那一次是完整的。
"""

ACCESSION_PATTERN = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-\d+)?$"
)
"""UniProtKB 编号：6 位（P00533）或 10 位（A0A0B4J2D5），可带同工型后缀（P00533-2）。"""

GENE_SYMBOL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}$")
"""像基因符号的单 token：EGFR、TP53、BRCA1、p53。"""

CITATION_PATTERN = re.compile(r"\s*\((?:PubMed|ECO):[^)]*\)")
"""注释里的文献 / 证据编号。它们能把一整句功能描述挤到截断点之外。"""

ORGANISM_ALIASES: dict[str, int] = {
    # 常见宿主 / 模式物种的中英文说法 → NCBI taxid。
    # 为什么要映射而不是直接查 organism_name：那是**子串匹配**。实测
    # `organism_name:"human"` 的第一条命中是「Human pegivirus」——一个病毒。
    # 用户说的「人的靶点」要的是 taxid 9606，不是「名字里带 human 的东西」。
    "human": 9606,
    "homo sapiens": 9606,
    "人": 9606,
    "人类": 9606,
    "mouse": 10090,
    "mus musculus": 10090,
    "小鼠": 10090,
    "rat": 10116,
    "rattus norvegicus": 10116,
    "大鼠": 10116,
    "zebrafish": 7955,
    "danio rerio": 7955,
    "斑马鱼": 7955,
    "yeast": 559292,
    "saccharomyces cerevisiae": 559292,
    "酿酒酵母": 559292,
    "e. coli": 83333,
    "escherichia coli": 83333,
    "大肠杆菌": 83333,
    "fruit fly": 7227,
    "drosophila melanogaster": 7227,
    "果蝇": 7227,
    "c. elegans": 6239,
    "caenorhabditis elegans": 6239,
    "线虫": 6239,
    "monkey": 9544,
    "macaque": 9544,
    "猴": 9544,
}

MAX_LIMIT = 50
"""一次检索最多返回多少条候选。

再多的候选对「挑一个靶点」没有帮助——需要更多结果时应当细化检索式（加基因名、
加物种、加功能关键词），而不是翻页。上限同时也是对 UniProt 的礼貌。
"""

MAX_PDB_IN_DIGEST = 3
MAX_PDB_IN_META = 20
MAX_DOMAINS_IN_DIGEST = 4
MAX_LIGANDS_IN_DIGEST = 4
FUNCTION_CHARS = 260
LOCATION_CHARS = 160


# ---------------------------------------------------------------------------
# 检索式
# ---------------------------------------------------------------------------
def _organism_clause(organism: str) -> str:
    """把物种写法转成 UniProt 的检索子句。

    数字按 taxid、别名表按 taxid、其余按 `organism_name` 子串匹配——
    最后这条留给表外物种（`Sus scrofa` 这类学名是可用的）。
    """
    text = organism.strip()
    if text.isdigit():
        return f"organism_id:{text}"
    taxid = ORGANISM_ALIASES.get(text.lower())
    if taxid is not None:
        return f"organism_id:{taxid}"
    return f'organism_name:"{text}"'


def _search_query(query: str, organism: str | None, *, reviewed_only: bool) -> str:
    """拼检索式。**整体加一层括号**，这不是美观问题。

    去掉括号的话 `gene:EGFR AND organism_id:9606` 与用户的原文之间会按优先级
    重新结合，用户写的 or 会被吞掉。
    """
    clauses = [f"({query.strip()})"]
    if organism and organism.strip():
        clauses.append(_organism_clause(organism))
    if reviewed_only:
        clauses.append("reviewed:true")
    return " AND ".join(clauses)


def _looks_like_gene_symbol(text: str) -> bool:
    """判断单 token 是不是基因符号。

    要求含大写字母或数字：`EGFR` / `TP53` / `p53` 是，`insulin` / `kinase` 不是——
    后两个是普通词，按基因名去找反而找不到东西。
    """
    if not GENE_SYMBOL_PATTERN.match(text):
        return False
    return any(ch.isupper() for ch in text) or any(ch.isdigit() for ch in text)


def _term_clause(text: str) -> tuple[str, str]:
    """把用户给的词转成检索子句，并说明是按什么找的。

    **单 token 优先走字段精确匹配，这一步是必须的。** UniProt 的默认全文检索会命中
    「注释里提到过这个词」的条目——实测查 `EGFR` 的前五条里只有第一条是 EGFR 本身，
    另外四条是 CLEC14A、CBLC 之类在功能描述里提过 EGFR 的蛋白。基因名字段里它是唯一的，
    换成 `gene:EGFR` 就只剩一条。多词或已带字段限定的写法一律原样交给 UniProt，不做猜测。
    """
    stripped = text.strip()
    if ":" in stripped or " " in stripped:
        return stripped, "全文检索"
    if ACCESSION_PATTERN.match(stripped.upper()):
        return f"accession:{stripped.upper()}", "按编号精确匹配"
    if _looks_like_gene_symbol(stripped):
        return f"gene:{stripped}", "按基因名精确匹配"
    return stripped, "全文检索"


def normalize_accession(raw: str) -> str:
    """校验编号格式。**在联网之前拦一道。**

    模型最常见的错不是拼错编号，而是**把基因名或 PDB 编号当编号交上来**。
    UniProt 收到这种输入只会回一句 `invalid format`，而它没告诉模型该怎么办；
    这里直接指路，且省掉一次注定失败的请求。
    """
    text = (raw or "").strip().upper()
    if not text:
        raise ValueError(
            "accession 不能为空。它应当是 UniProt 编号，例如 P00533；"
            "如果你手上是基因名或蛋白名，先用 search_targets 换成编号"
        )
    if not ACCESSION_PATTERN.match(text):
        raise ValueError(
            f"{raw!r} 不是 UniProt 编号。编号形如 P00533（6 位）或 A0A0B4J2D5（10 位），"
            "可带同工型后缀（P00533-2）。基因名、蛋白名、PDB 编号（如 1M17）都不是编号，"
            "请先用 search_targets 查出对应编号"
        )
    return text


# ---------------------------------------------------------------------------
# 条目的读取与写盘
# ---------------------------------------------------------------------------
def target_cache_dir() -> Path:
    """靶点记录的本地缓存目录，与状态文件同处。"""
    return Path.home() / ".opencode_tui" / "targets"


def _record_path(accession: str) -> Path:
    return target_cache_dir() / f"{accession}.json"


def _serialize(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, indent=2)


def _read_cached(path: Path) -> tuple[dict[str, Any], str] | None:
    """读本地缓存。**读坏了当作没有**，不能让一份半截文件把工具永久堵住。"""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text), fingerprint_text(text)
    except (OSError, ValueError):
        return None


def _write_cached(path: Path, record: dict[str, Any]) -> str:
    """写盘并返回内容指纹。

    先写 `.tmp` 再替换：中途写坏的话，留下的是一个半截 JSON，而下次启动会把它
    当作有效缓存读进来（然后解析失败）。替换是原子的，读到的要么是旧的完整版本，
    要么是新的完整版本。
    """
    text = _serialize(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return fingerprint_text(text)


def _fetch_record(accession: str, *, timeout: float) -> dict[str, Any]:
    """取完整条目。

    刻意**不加 fields 过滤**：这是一份存下来备查的参考记录，下载时就按需要裁剪
    等于不可逆地丢掉信息（比如后面想用条目里的已知活性配体），而它就是个几百 KB
    的 JSON。要控制上下文，靠的是下面那份摘要，不是靠删数据。
    """
    url = f"{BASE_URL}/{accession}.json"
    payload = http.get_json(url, {}, timeout=timeout, headers={"Accept": "application/json"})
    if not isinstance(payload, dict) or "primaryAccession" not in payload:
        raise ValueError(f"UniProt 返回的内容不是预期的条目格式：{url}")
    return payload


# ---------------------------------------------------------------------------
# 条目字段的摘取
# ---------------------------------------------------------------------------
def _protein_name(record: dict[str, Any]) -> str:
    description = record.get("proteinDescription") or {}
    recommended = ((description.get("recommendedName") or {}).get("fullName") or {}).get("value")
    if recommended:
        return str(recommended)
    # 未审阅条目常常只有提交者给的名字
    for submission in description.get("submissionNames") or []:
        value = ((submission or {}).get("fullName") or {}).get("value")
        if value:
            return str(value)
    return "(未命名)"


def _gene_names(record: dict[str, Any]) -> list[str]:
    """基因名：首位是主名，其余是同义词。下游按基因名检索时主名最要紧。"""
    names: list[str] = []
    for gene in record.get("genes") or []:
        primary = ((gene or {}).get("geneName") or {}).get("value")
        if primary:
            names.append(str(primary))
        for key in ("synonyms", "orfNames"):
            for item in (gene or {}).get(key) or []:
                value = (item or {}).get("value")
                if value:
                    names.append(str(value))
    return list(dict.fromkeys(names))


def _is_reviewed(record: dict[str, Any]) -> bool:
    return "reviewed" in str(record.get("entryType") or "")


def _resolution(raw: Any) -> float | None:
    """`"2.60 A"` → `2.6`；NMR 这类没有分辨率的返回 None。"""
    if raw is None:
        return None
    match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", str(raw))
    return float(match.group(1)) if match else None


def _pdb_structures(record: dict[str, Any]) -> list[dict[str, Any]]:
    """把 PDB 交叉引用排成「好用的在前」。

    排序不是装饰：模型看的是前几条，而它要挑的正是分辨率够高的那个。没有分辨率的
    （NMR 结构、部分电镜结构）排在最后——它们不是不能用，只是不适合做对接的起点。
    """
    rows: list[dict[str, Any]] = []
    for xref in record.get("uniProtKBCrossReferences") or []:
        if (xref or {}).get("database") != "PDB":
            continue
        props = {
            str(item.get("key")): item.get("value")
            for item in (xref or {}).get("properties") or []
            if isinstance(item, dict)
        }
        rows.append(
            {
                "id": xref.get("id"),
                "method": props.get("Method") or "",
                "resolution": _resolution(props.get("Resolution")),
                "chains": str(props.get("Chains") or "")[:60],
            }
        )
    rows.sort(key=lambda row: (row["resolution"] is None, row["resolution"] or 0.0, row["id"] or ""))
    return rows


def _alphafold_id(record: dict[str, Any]) -> str | None:
    """AlphaFold 模型编号。

    交叉引用里的 `id` 是 **UniProt 编号**，不是模型编号（模型编号的约定是
    `AF-<编号>-F1`），所以要拼一下——直接把 `P00533` 当模型号丢给下游，
    它拿到的是一份不存在的文件。
    """
    for xref in record.get("uniProtKBCrossReferences") or []:
        if (xref or {}).get("database") == "AlphaFoldDB":
            model = str(xref.get("id") or "").strip()
            if not model:
                return None
            return model if model.startswith("AF-") else f"AF-{model}-F1"
    return None


def _comment_texts(record: dict[str, Any], comment_type: str) -> list[str]:
    texts: list[str] = []
    for comment in record.get("comments") or []:
        if (comment or {}).get("commentType") != comment_type:
            continue
        for item in (comment or {}).get("texts") or []:
            value = (item or {}).get("value")
            if value:
                texts.append(CITATION_PATTERN.sub("", str(value)))
    return texts


def _subcellular_text(record: dict[str, Any]) -> str:
    """亚细胞定位。它决定「这个靶点是不是可及的」——膜蛋白与胞内蛋白的筛选策略不同。"""
    parts: list[str] = []
    for comment in record.get("comments") or []:
        if (comment or {}).get("commentType") != "SUBCELLULAR LOCATION":
            continue
        for location in (comment or {}).get("subcellularLocations") or []:
            value = ((location or {}).get("location") or {}).get("value")
            if value:
                parts.append(str(value))
            topology = ((location or {}).get("topology") or {}).get("value")
            if topology:
                parts.append(str(topology))
        for item in (comment or {}).get("texts") or []:
            value = (item or {}).get("value")
            if value:
                parts.append(CITATION_PATTERN.sub("", str(value)))
    return "；".join(dict.fromkeys(parts))


def _domains(record: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feature in record.get("features") or []:
        if (feature or {}).get("type") != "Domain":
            continue
        location = (feature or {}).get("location") or {}
        out.append(
            {
                "description": str((feature or {}).get("description") or "结构域"),
                "start": (location.get("start") or {}).get("value"),
                "end": (location.get("end") or {}).get("value"),
            }
        )
    return out


def _binding_ligands(record: dict[str, Any]) -> list[str]:
    """结合位点上的配体名（ATP、Mg(2+)…）。

    这是「口袋里有什么」的直接线索：知道天然配体是谁，才知道能拿什么当参照物，
    也才知道哪些残基必须保住。
    """
    names: list[str] = []
    for feature in record.get("features") or []:
        if (feature or {}).get("type") != "Binding site":
            continue
        ligand = ((feature or {}).get("ligand") or {}).get("name")
        if ligand:
            names.append(str(ligand))
    return list(dict.fromkeys(names))


def _keywords(record: dict[str, Any]) -> list[str]:
    return [str(item.get("name")) for item in record.get("keywords") or [] if item.get("name")]


def _entry_audit(record: dict[str, Any]) -> tuple[int | None, str]:
    """返回（条目版本号，最后注解更新日期）。结果对不上时要能回溯到「当时是哪一版」。"""
    audit = record.get("entryAudit") or {}
    version = audit.get("entryVersion")
    return (
        int(version) if isinstance(version, int) else None,
        str(audit.get("lastAnnotationUpdateDate") or ""),
    )


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    """压平空白再截断。条目里的注释是长句，换行会打散成很难读的多行。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else f"{flat[:limit].rstrip()}…"


def _relative(path: Path) -> str:
    """相对家目录展示，一律 POSIX 分隔符——反斜杠容易被原样抄进别处去拼接。"""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _render_search(rows: list[dict[str, Any]], *, query_text: str, notes: list[str]) -> str:
    if not rows:
        lines = [f"UniProtKB 没有命中（检索式：{query_text}）"]
        lines.append(
            "可以试着：换用官方基因名或蛋白全名；organism 留空以跨物种找；"
            "reviewed_only=false 把 TrEMBL 自动注释条目也算上"
        )
        lines.extend(f"（{note}）" for note in notes)
        return "\n".join(lines)

    lines = [f"UniProtKB 命中 {len(rows)} 条（检索式：{query_text}）"]
    for row in rows:
        accession = str(row.get("primaryAccession") or "?")
        genes = _gene_names(row)
        gene = genes[0] if genes else "（无基因名）"
        organism = (row.get("organism") or {}).get("scientificName") or "未知物种"
        length = (row.get("sequence") or {}).get("length")
        state = "reviewed" if _is_reviewed(row) else "unreviewed"
        lines.append(
            f"- {accession} · {gene} · {length} aa · {state} · "
            f"{_clip(_protein_name(row), 70)}（{organism}）"
        )
    lines.extend(f"（{note}）" for note in notes)
    lines.append(
        "下一步：用 fetch_target 取某个编号的详情与可用实验结构。"
        "命中不够准时可以加字段限定收窄，例如 gene:EGFR、organism_id:9606。"
    )
    return "\n".join(lines)


def _review_state(record: dict[str, Any]) -> str:
    if str(record.get("entryType") or "") == "Inactive":
        # 已删除的条目连 entryType 都不是 reviewed/unreviewed 那一套，
        # 对它说「未审阅」是错的，而这两条路的下一步动作完全不同
        return "已删除（Inactive）"
    return "reviewed（Swiss-Prot）" if _is_reviewed(record) else "unreviewed（TrEMBL）"


def _render_entry(record: dict[str, Any]) -> str:
    accession = str(record.get("primaryAccession") or "")
    entry_name = str(record.get("uniProtkbId") or "")
    organism = record.get("organism") or {}
    scientific = organism.get("scientificName")
    common = organism.get("commonName")
    taxid = organism.get("taxonId")
    length = (record.get("sequence") or {}).get("length")

    head = " · ".join(part for part in (accession, entry_name, _protein_name(record)) if part)
    lines = [head]

    # 物种这一行逐项拼接：已删除 / 不完整的条目根本没有 organism 与 sequence，
    # 硬拼出来就是「未知物种 · taxid None · None aa」——三个字段全是废话
    facts: list[str] = []
    if scientific:
        facts.append(f"{scientific}{f'（{common}）' if common else ''}")
    if taxid:
        facts.append(f"taxid {taxid}")
    if length:
        facts.append(f"{length} aa")
    facts.append(_review_state(record))
    lines.append("  物种：" + " · ".join(facts))

    if str(record.get("entryType") or "") == "Inactive":
        reason = (record.get("inactiveReason") or {}).get("deletedReason") or "未说明"
        lines.append(f"  注意：该条目已从 UniProtKB 删除（{reason}），不能作为靶点使用。")

    structures = _pdb_structures(record)
    if structures:
        lines.append(f"  结构：{len(structures)} 个实验结构，分辨率最好的几个（这决定对接能不能做）：")
        for row in structures[:MAX_PDB_IN_DIGEST]:
            resolution = f"{row['resolution']:.2f} Å" if row["resolution"] else "无分辨率"
            chains = f" · 链 {row['chains']}" if row["chains"] else ""
            lines.append(f"    {row['id']} · {row['method'] or '方法未知'} · {resolution}{chains}")
    else:
        lines.append(
            "  结构：没有实验结构。没有实验结构的靶点不能直接对接——"
            "下面的 AlphaFold 模型或同源建模是目前唯一的结构来源。"
        )

    alphafold = _alphafold_id(record)
    lines.append(f"  AlphaFold 模型：{alphafold}" if alphafold else "  AlphaFold 模型：无")

    functions = _comment_texts(record, "FUNCTION")
    if functions:
        lines.append(f"  功能：{_clip(functions[0], FUNCTION_CHARS)}")

    location = _subcellular_text(record)
    if location:
        lines.append(f"  定位：{_clip(location, LOCATION_CHARS)}")

    domains = _domains(record)
    ligands = _binding_ligands(record)
    if domains or ligands:
        parts: list[str] = []
        if domains:
            rendered = "、".join(
                f"{item['description']}（{item['start']}-{item['end']}）"
                for item in domains[:MAX_DOMAINS_IN_DIGEST]
            )
            if len(domains) > MAX_DOMAINS_IN_DIGEST:
                rendered += f"，共 {len(domains)} 个"
            parts.append(f"结构域：{rendered}")
        if ligands:
            rendered = "、".join(ligands[:MAX_LIGANDS_IN_DIGEST])
            if len(ligands) > MAX_LIGANDS_IN_DIGEST:
                rendered += f"，共 {len(ligands)} 种"
            parts.append(f"已知结合配体：{rendered}")
        lines.append("  " + "；".join(parts))

    return "\n".join(lines)


def _entry_summary(record: dict[str, Any]) -> str:
    """登记表条目的一行摘要。它会被别的工具在列表里复读，所以要能独立读懂。"""
    genes = _gene_names(record)
    parts = [
        f"靶点 {record.get('primaryAccession')}",
        genes[0] if genes else _protein_name(record),
    ]
    length = (record.get("sequence") or {}).get("length")
    if length:
        parts.append(f"{length} aa")

    structures = _pdb_structures(record)
    if structures:
        first = structures[0]
        best = f"，最佳 {first['resolution']:.2f} Å" if first["resolution"] else ""
        parts.append(f"实验结构 {len(structures)} 个{best}")
    else:
        # 「无实验结构」是这一行里最要紧的信息，不能因为数字是 0 就省掉
        parts.append("无实验结构")

    alphafold = _alphafold_id(record)
    if alphafold:
        parts.append(f"AlphaFold {alphafold}")
    if str(record.get("entryType") or "") == "Inactive":
        parts.insert(0, "已删除条目")
    return " · ".join(str(part) for part in parts)


def _entry_meta(record: dict[str, Any], path: Path) -> dict[str, Any]:
    """挂在登记表条目上的结构化字段。

    `pdb_structures` 只放前若干条而另给 `pdb_count`：meta 会被别的工具整段回注，
    385 条 PDB 记录塞进去的结果是它被截断，前面的字段也跟着看不见了。
    完整清单在那一份写盘文件里。
    """
    structures = _pdb_structures(record)
    entry_version, updated = _entry_audit(record)
    try:
        retrieved_at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        retrieved_at = ""
    return {
        "source": "uniprot",
        "accession": record.get("primaryAccession"),
        "entry_name": record.get("uniProtkbId"),
        "protein_name": _protein_name(record),
        "genes": _gene_names(record),
        "organism": (record.get("organism") or {}).get("scientificName"),
        "taxon_id": (record.get("organism") or {}).get("taxonId"),
        "length": (record.get("sequence") or {}).get("length"),
        "reviewed": _is_reviewed(record),
        "entry_type": record.get("entryType"),
        "pdb_count": len(structures),
        "pdb_structures": structures[:MAX_PDB_IN_META],
        "alphafold_id": _alphafold_id(record),
        "domains": _domains(record),
        "binding_ligands": _binding_ligands(record),
        "keywords": _keywords(record),
        "uniprot_entry_version": entry_version,
        "last_annotation_update": updated,
        "retrieved_at": retrieved_at,
        "source_url": f"{BASE_URL}/{record.get('primaryAccession')}",
        "file": _relative(path),
    }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class SearchTargetsTool(Tool):
    """按名称 / 基因检索 UniProt 靶点。纯查询，不产生产物，所以不收登记表。"""

    spec = ToolSpec(
        name="search_targets",
        description=(
            "按蛋白名、基因名或关键词检索 UniProt 靶点，返回候选编号列表。"
            "这是把「用户说的靶点」变成确定蛋白的入口：**先在这里查到编号，再用 fetch_target "
            "取它的详情与可用实验结构**。不要凭记忆写编号——模型记得的编号看着都很合理，"
            "但可能是别的物种或别的蛋白。单个词（EGFR、TP53）会优先按基因名精确匹配，命不中"
            "才退回全文检索：全文检索会把「注释里提到过它」的蛋白一起捞上来。"
            "每条依次是 编号、主基因名、序列长度、是否人工审阅、蛋白名与物种。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索词，例如 EGFR、epidermal growth factor receptor、kinase inhibitor。"
                        "可带 UniProt 检索语法（gene:EGFR、organism_id:9606）"
                    ),
                },
                "organism": {
                    "type": "string",
                    "description": (
                        "限定物种，可填 taxid（9606）、英文/中文常见名（human、小鼠）"
                        "或学名（Sus scrofa）；留空则跨物种检索"
                    ),
                },
                "reviewed_only": {
                    "type": "boolean",
                    "description": (
                        "只取人工审阅的条目（Swiss-Prot）。默认 true；"
                        "这类条目里没有命中时会自动放宽并说明，不会让你空手而归"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多返回多少条候选，上限 {MAX_LIMIT}（再多就该细化检索式，而不是翻页）",
                },
            },
            "required": ["query"],
        },
    )
    primary_argument = "query"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        text = str(arguments.get("query") or "").strip()
        if not text:
            return ToolResult(
                output=(
                    "query 不能为空。填靶点的名称或基因名，例如 EGFR；"
                    "如果你已经有 UniProt 编号，直接用 fetch_target 取详情"
                ),
                failed=True,
            )

        organism = arguments.get("organism")
        reviewed_only = arguments.get("reviewed_only")
        reviewed_only = True if reviewed_only is None else bool(reviewed_only)
        try:
            requested = int(arguments.get("limit") or 10)
        except (TypeError, ValueError):
            return ToolResult(output="limit 应当是整数", failed=True)

        size = max(1, min(requested, MAX_LIMIT))

        def _search(query_text: str) -> tuple[list[dict[str, Any]], str]:
            params = {
                "query": query_text,
                "fields": SEARCH_FIELDS,
                "format": "json",
                "size": str(size),
            }
            payload = http.get_json(f"{BASE_URL}/search", params, timeout=45.0)
            rows = list(payload.get("results") or []) if isinstance(payload, dict) else []
            return rows, query_text

        clause, how = _term_clause(text)
        effective = _search_query(clause, organism, reviewed_only=reviewed_only)
        rows, effective = _search(effective)

        notes: list[str] = []
        if requested != size:
            notes.append(f"limit 上限为 {MAX_LIMIT}，已按 {size} 处理")

        if not rows:
            # 退到最宽的一次尝试：原文全文检索 + 不限制审阅状态。
            # 单 token 被当作基因名去找却没找到（可能是别名、旧名，或者就是个普通词），
            # 这两个方向各退一步比直接告诉模型「没这个东西」有用得多。
            fallback = _search_query(text, organism, reviewed_only=False)
            attempted = [effective]
            if fallback != effective:
                rows, effective = _search(fallback)
                attempted.append(effective)
            if rows:
                notes.append(
                    f"先按{how}并在人工审阅条目里找过，没命中；下面是退回全文检索、"
                    "并放宽到未审阅条目（TrEMBL）的结果，可信度低于 Swiss-Prot"
                )
            else:
                notes.append("已试过：" + " 与 ".join(attempted))

        return ToolResult(
            output=_render_search(rows, query_text=effective, notes=notes),
            meta={"count": len(rows)},
        )


class FetchTargetTool(Tool):
    """取 UniProt 编号的完整信息，并把条目登记进登记表。"""

    spec = ToolSpec(
        name="fetch_target",
        description=(
            "取一个 UniProt 编号的完整靶点信息：功能、亚细胞定位、结构域、可用实验结构。"
            "返回里最要紧的是**结构那一行**：有几套实验结构、分辨率多少、各覆盖哪段序列，"
            "以及有没有 AlphaFold 模型——这决定了这个靶点能不能直接对接。"
            "编号必须来自 search_targets 的结果，不要自己拼；基因名或 PDB 编号都不是编号。"
            "条目会写盘并登记成登记表里的 artifact，同一个编号再次调用直接读本地缓存、不再联网。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "accession": {
                    "type": "string",
                    "description": "UniProt 编号，例如 P00533；可带同工型后缀（P00533-2）",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true 时忽略本地缓存强制重新下载（认为远端注解已更新时才用）",
                },
            },
            "required": ["accession"],
        },
    )
    primary_argument = "accession"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：fetch_target 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            code = normalize_accession(str(arguments.get("accession") or ""))
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        refresh = bool(arguments.get("refresh"))
        path = _record_path(code)

        cached = None if refresh else _read_cached(path)
        if cached is not None:
            record, fingerprint = cached
            origin = f"读自本地缓存 {_relative(path)}（未联网；需要刷新用 refresh=true）"
        else:
            record = await asyncio.to_thread(_fetch_record, code, timeout=60.0)
            fingerprint = _write_cached(path, record)
            origin = f"已下载并写盘 {_relative(path)}"

        # 登记而不是每次新建：同一个编号被反复取用是常态（换着问功能、结构、定位），
        # 内容寻址之后「同一份东西」只该在登记表里出现一次。
        # include_archived 是必要的：靶点信息属于参考数据，但它登记时会被挂上当前受体
        # 作为父项，切换受体后会被搁置——那时若看不见它，同一份数据会被重复登记一份。
        existing = ledger.find(kind="artifact", fingerprint=fingerprint, include_archived=True)
        if existing:
            entry = existing[0]
            registered = f"登记表里已有同一份：{entry.handle}（未重复登记）"
        else:
            entry = ledger.add(
                "artifact",
                path=path,
                summary=_entry_summary(record),
                fingerprint=fingerprint,
                meta=_entry_meta(record, path),
            )
            registered = f"已登记 {entry.handle}"

        digest = _render_entry(record)
        entry_version, updated = _entry_audit(record)
        provenance = " · ".join(
            part
            for part in (
                origin,
                registered,
                f"指纹 {fingerprint}",
                f"UniProt 条目版本 {entry_version}" if entry_version else "",
                f"注解更新于 {updated}" if updated else "",
            )
            if part
        )
        return ToolResult(
            output=f"{digest}\n  {provenance}",
            meta={
                "accession": record.get("primaryAccession"),
                "handle": str(entry.handle),
                "fingerprint": fingerprint,
            },
        )


async def _to_thread(function, *args, **kwargs):
    """把同步的取数丢到线程池，别把事件循环占住。"""
    return await asyncio.to_thread(function, *args, **kwargs)


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具，按装配顺序。"""
    return [SearchTargetsTool(), FetchTargetTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
