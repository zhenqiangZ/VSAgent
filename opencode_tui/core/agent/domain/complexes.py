"""P0 主来源：搜该蛋白的所有复合物结构 → 排序 → 下载坐标。

**这是本轮口袋来源的主路径。** 预测是「算出来的猜测」，共晶配体是「测出来的事实」；
有事实时不该拿猜测当主结果（方案稿 §2.3.3）。所以顺序是：

    P0 复合物（本模块） → 一个都没有才转 P1 预测（`prankweb.py`） → 都不行就留空

三段，各自可以单独验：

1. **搜**（`list_complexes`）：RCSB 属性搜，条件 `UniProt accession == <AC>`
   加 `nonpolymer_entity_count > 0`。实测 ABL1（`P00519`）→ 69 个条目。
2. **排**（`pick_top`）：分辨率 → 实验方法 → 配体像不像真配体 → 同名去重，取前 N（默认 12）。
3. **下**（`download_entry`）：`https://files.rcsb.org/download/<ID>.pdb`，实测平均 0.24 MB。
   走 `http.py`，写进 `~/.opencode_tui/rcsb/`，失败的不计入、继续下一个。

**不编条目。** 搜索/下载任一环失败就留空——**不造一个看着合理的 PDB 编号填进去**
（那是方案稿 §2.2 说的最危险的一类错误：不报错、不崩，只是整页数据都是假的）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools import RetryableToolError, ToolError
from . import http

__all__ = [
    "ComplexEntry",
    "ComplexError",
    "RCSB_SEARCH_URL",
    "RCSB_DOWNLOAD_URL",
    "SEARCH_CACHE_TTL",
    "download_entry",
    "list_complexes",
    "pick_top",
    "rcsb_cache_dir",
    "search_cache_dir",
]

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_DOWNLOAD_URL = "https://files.rcsb.org/download"

SEARCH_CACHE_TTL = 24 * 3600
"""搜索结果的时效（秒）。一天——新结构发布是低频事件，一天足够新鲜。"""

DEFAULT_TOP_N = 12
"""P0 下载上限。按 §7.9 拍定；返回里要报 `total` / `used`，只报 `used`
会让用户以为这个蛋白只有 12 个复合物。"""


class ComplexError(ToolError):
    """这一侧的接口不可用或返回的形状不对。**当「该来源不可用」，不重试到底。**"""


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------
@dataclass
class ComplexEntry:
    """一个候选复合物。字段全部来自 RCSB，**没有一处是我们猜的**。"""

    pdb_id: str
    title: str = ""
    resolution: float | None = None
    method: str = ""
    ligands: tuple[str, ...] = ()
    """非聚合物实体的配体代号（`chem_comp.id`）。用来排「配体像不像真配体」。"""

    ranked_ligands: int = 0
    """非聚合物实体个数。"""

    def sort_key(self, ligand_names: frozenset[str]) -> tuple:
        """排序用的键：分辨率 → 实验方法 → 配体像真配体 → 编号。

        **`resolution` 为 None 的排最后**（`inf`）：没有分辨率的（多半是电镜里的
        低分辨率或理论模型）不该压过 1.1 埃的晶体结构。
        方法排序里 X 射线衍射优先（`X-RAY`）——晶体结构的分辨率与配体建模最可靠。
        配体那一档看「有几个配体不是缓冲液/离子」：全是甘油的结构对人类读者没有价值。

        最后以编号兜底，保证**同一组输入跑两遍结果完全一致**（不能有字典序残留）。
        """
        resolution = float(self.resolution) if self.resolution is not None else float("inf")
        method_rank = _METHOD_RANK.get(self.method.upper(), len(_METHOD_RANK))
        real_ligands = sum(1 for name in self.ligands if name.upper() not in ligand_names)
        return (-real_ligands, resolution, method_rank, self.pdb_id)


_METHOD_RANK = {
    "X-RAY DIFFRACTION": 0,
    "ELECTRON MICROSCOPY": 1,
    "NEUTRON DIFFRACTION": 2,
    "ELECTRON CRYSTALLOGRAPHY": 3,
    "SOLUTION NMR": 4,
    "SOLID-STATE NMR": 5,
}
"""实验方法的好坏次序。电镜结构常有 3 埃以上、配体建模不如晶体靠谱，所以排在晶体之后。"""


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def rcsb_cache_dir() -> Path:
    """下载下来的坐标存放处（§7.9）。与状态文件、结构缓存同处一个家目录。"""
    return Path.home() / ".opencode_tui" / "rcsb"


def search_cache_dir() -> Path:
    """搜索结果的缓存目录（§7.9 第二条）。"""
    return Path.home() / ".opencode_tui" / "rcsb" / "search"


def _cache_path(query: Any) -> Path:
    blob = json.dumps(query, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return search_cache_dir() / f"{digest}.json"


def _read_cache(path: Path) -> Any | None:
    """读缓存。**过期或损坏都当没有**——损坏的缓存不能变成一次假结果。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "saved_at" not in payload:
        return None
    import time

    if time.time() - float(payload["saved_at"]) > SEARCH_CACHE_TTL:
        return None
    return payload.get("body")


def _write_cache(path: Path, body: Any) -> None:
    """写缓存。**失败不影响主流程**——缓存是加速用的，不是真相。"""
    import time

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"saved_at": time.time(), "body": body}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 1. 搜
# ---------------------------------------------------------------------------
def _accession_query(accession: str, *, with_ligands: bool = True) -> dict[str, Any]:
    """RCSB 属性搜的查询体。

    两个条件：`UniProt accession == <AC>`，以及（`with_ligands` 时）
    `nonpolymer_entity_count > 0`。后者是关键——**不必拉坐标就能判断「这个结构有没有配体」**，
    且它快且稳（不像 PDBe 注释接口会 502）。实测 ABL1 加这个条件后从 1000+ 降到 69。
    """
    nodes: list[dict[str, Any]] = [
        {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                "operator": "exact_match",
                "value": accession,
            },
        }
    ]
    if with_ligands:
        nodes.append(
            {
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_entry_info.nonpolymer_entity_count",
                    "operator": "greater",
                    "value": 0,
                },
            }
        )
    return {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": nodes,
        },
        "return_type": "entry",
        "request_options": {
            "results_content_type": ["experimental"],
            "paginate": {"start": 0, "rows": 1000},
        },
    }


def list_complexes(
    uniprot: str,
    *,
    use_cache: bool = True,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> tuple[list[str], dict[str, Any]]:
    """搜该 UniProt 的所有**带配体**的已发布结构。返回 (条目 id 列表, 原始响应)。

    **这是 P0 的主键**：一个 UniProt 对应几十上百个 PDB 条目，拿手上的这一份受体
    当搜索范围会让 P0 退化成「只看现成结构」。所以这个函数收的是 UniProt 编号。

    返回的第二个值是原始响应（给判据与排查看），**不要写进 `meta.json`**。

    **零命中回 `([], {})` 而不是抛错**——RCSB 在零结果时回 204 无内容（`http.EmptyResult`），
    那是「查了、确实没有」。这条与「没查成」在调用方那里说出的话相反
    （前者该转预测，后者该重试），所以它在同一条路上必须分得开。
    其余失败抛 `ComplexError`——调用方把它读成「这一侧不可用」，不重试到底。
    """
    accession = (uniprot or "").strip()
    if not accession:
        raise ComplexError("没有 UniProt 编号，搜不了复合物")

    query = _accession_query(accession)
    path = _cache_path(query)
    if use_cache:
        cached = _read_cache(path)
        if cached is not None:
            return _ids_from(cached), cached

    try:
        body = http.post_json(RCSB_SEARCH_URL, query, timeout=timeout)
    except http.EmptyResult:
        # 查了、没有。缓存一份空的，省得同一个目标反复去打同一个 204。
        empty: dict[str, Any] = {"result_set": [], "total_count": 0}
        if use_cache:
            _write_cache(path, empty)
        return [], empty
    except RetryableToolError:
        # 429 / 5xx / 连不上是瞬时故障，交给循环层重试；包成 ComplexError 就
        # 等于替它做了「别再试」的决定，而它并不知道网络刚刚抖了一下。
        raise
    except Exception as error:  # 其余（响应形状不对、4xx）归为「这一侧不可用」
        raise ComplexError(f"RCSB 检索没成：{error}") from error

    if use_cache:
        _write_cache(path, body)
    return _ids_from(body), body


def _ids_from(body: Any) -> list[str]:
    """从检索响应里抠出条目 id。

    **只认 `result_set[].identifier`**，不顺手把别的字段也解出来——
    「没解出 `polymer_entity_count` 之类的旁支字段」是一条判据，多解一项就多一种
    把响应形状的偶然当成约定的可能。
    """
    if not isinstance(body, dict):
        return []
    result_set = body.get("result_set")
    if not isinstance(result_set, list):
        return []
    ids: list[str] = []
    for item in result_set:
        if isinstance(item, dict) and isinstance(item.get("identifier"), str):
            ids.append(item["identifier"])
    return ids


# ---------------------------------------------------------------------------
# 2. 排
# ---------------------------------------------------------------------------
def pick_top(
    entries: list[ComplexEntry],
    *,
    top_n: int = DEFAULT_TOP_N,
    excluded_ligands: frozenset[str] = frozenset(),
) -> tuple[list[ComplexEntry], int]:
    """按 §2.3.6 排序取前 N。返回 (选中的条目, 搜到的总数)。

    「同名去重」：同一个配体代号出现过的结构只留**排序最好的那一个**——
    同一个配体在十几个结构里各算一遍口袋，会得到十几个几乎一样的结果，
    把额度吃光却不多给信息。

    **返回总数与选中的一起**：只报 `used` 会让用户以为这个蛋白只有 12 个复合物。
    """
    total = len(entries)
    ranked = sorted(entries, key=lambda entry: entry.sort_key(excluded_ligands))

    chosen: list[ComplexEntry] = []
    seen_ligands: set[str] = set()
    for entry in ranked:
        signature = frozenset(name.upper() for name in entry.ligands)
        if signature and signature in seen_ligands:
            continue
        # 空配体清单不参与去重（没有配体的条目在 P0 里本来就不该出现）。
        if signature:
            seen_ligands.add(signature)
        chosen.append(entry)
        if len(chosen) >= max(1, top_n):
            break
    return chosen, total


# ---------------------------------------------------------------------------
# 3. 下
# ---------------------------------------------------------------------------
def download_entry(
    pdb_id: str,
    *,
    timeout: float = http.DEFAULT_TIMEOUT,
    force: bool = False,
) -> str:
    """下载一个条目的坐标文本。**已缓存就返回缓存**（不自动过期，§7.9）。

    失败抛 `ComplexError`——调用方记进失败清单、继续下一个，**不编条目**。
    """
    entry_id = (pdb_id or "").strip().upper()
    if not entry_id:
        raise ComplexError("没有条目编号，下载不了")

    path = rcsb_cache_dir() / f"{entry_id}.pdb"
    if not force:
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            pass

    url = f"{RCSB_DOWNLOAD_URL}/{entry_id}.pdb"
    try:
        text = http.get_text(url, timeout=timeout)
    except RetryableToolError:
        # 下载是最该重试的一步：一次抖动丢掉整条链，代价比再拉一次大得多。
        raise
    except Exception as error:
        raise ComplexError(f"下载 {entry_id} 没成：{error}") from error

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pdb.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # 缓存写不进去不影响这次调用——文本已经在手上。
        pass
    return text


def cached_entries() -> list[str]:
    """缓存目录里已经下过的条目编号（给 `/pockets` 之类的命令看状态用）。"""
    directory = rcsb_cache_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem.upper() for p in directory.glob("*.pdb"))


@dataclass
class FetchOutcome:
    """一批下载的结果。**用的与没用的分开列**，用户才分得清「搜了没有」与「下载没成」。"""

    texts: list[tuple[str, str]] = field(default_factory=list)
    """(条目编号, 坐标文本)，**按排序后的顺序**。"""

    failed: list[tuple[str, str]] = field(default_factory=list)
    """(条目编号, 失败原因)。"""

    @property
    def used(self) -> int:
        return len(self.texts)


def fetch_top(
    entries: list[ComplexEntry],
    *,
    top_n: int = DEFAULT_TOP_N,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> FetchOutcome:
    """把选中的条目逐个下载下来。**失败的不中断整批**——前两个成功就前两个入库。"""
    outcome = FetchOutcome()
    for entry in entries[: max(1, top_n)]:
        try:
            outcome.texts.append((entry.pdb_id, download_entry(entry.pdb_id, timeout=timeout)))
        except ComplexError as error:
            outcome.failed.append((entry.pdb_id, str(error)))
    return outcome
