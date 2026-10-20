"""实验结构（PDB 条目）的取回与导入。

虚拟筛选的第一个具体动作是「拿一套坐标」。UniProt 回答了**这是什么蛋白**、
ChEMBL 回答了**已知答案是什么**，但对接要的是一套**具体的实验结构**——同一个蛋白
可以有几十套，分辨率、覆盖的序列段、带什么配体、什么构象各不相同，选哪一套直接决定
后面所有结果。

`fetch_structure` 做这件事：编号 → 写盘的坐标文件 + 这份「这套结构里有什么」的摘要
（方法、分辨率、每条链的残基范围与缺口、配体、水、交替构象），并登记成会话登记表里的
一个 `receptor` 条目（状态是**原始结构**，还没准备）。

四条口径值得单独说明：

**只认 PDB 编号，不认 UniProt 编号。** 一个 UniProt 编号对应多套结构，选哪套是一个
决定，工具不该替用户做。方向是单向的：`fetch_target` 的返回值里带着可用结构清单
（含分辨率与链覆盖范围），从那里挑一个编号喂进来。

**元数据全部从坐标文件里就地解析，不额外查接口。** 标题 / 分类 / 沉积日期 / 方法 /
分辨率在 HEADER、TITLE、EXPDTA、REMARK 2 里；链与残基范围在 SEQRES 与 ATOM 里；
配体、修饰残基、水在 HETATM 与 MODRES 里。好处不只是省一次请求：**读盘路径与首次
下载路径会走同一段解析**，所以缓存命中也拿得到完整信息，不需要给坐标文件配一份
sidecar 元数据。这些解析都用不上化学依赖——固定列文本而已，所以这个工具在没装
化学依赖的机器上照样能用。

**超大结构取不回来，而且必须说清为什么。** RCSB 对超出 PDB 格式上限的条目不提供
`.pdb`，只给 mmCIF（实测 4YBB 29.5 万原子、3J3Q 244 万原子：`.pdb` 404 而 `.cif`
200）。本模块**只取 `.pdb`**：下游读不了 mmCIF，取回一份用不了的文件、还把它登记成
受体，比当场说清「这个条目取不了、为什么」糟糕得多——前者会在几步之后以另一种面目
炸开。所以那条路是两档错误，各自可操作：两个格式都没有 = 编号不存在；只有 mmCIF =
结构太大，建议换一个覆盖同一蛋白的小结构。

**NMR 结构有多套模型，统计只算第一套。** 不做这一步的话，1D3Z 的 10 套模型会让原子数
变成 10 倍，而「观测到的残基」这类按 (序号, 插入码) 去重的统计又不受影响——两个数
一错一对，比都错更难发现。摘要里会写明模型套数。

最后一条只关于两个导入动作：**只有 `import_structure` 与 `import_ligandset` 收路径，
而且它们都会把输入落盘一份。** 其余工具一律收 handle；但自备结构（同源建模、突变体、
冷冻电镜局部重建）与自备配体库是真实需求，模型也确实编得出 `D:/work/model.pdb`。
折中是把「收路径」压缩成这两个动作：导入时**复制进产物目录**，登记成 receptor / ligandset，
之后它和远端取回的条目完全一样（handle 在手，路径不再出现在任何签名里）。

复制而不是原地引用，理由是**登记表必须拥有自己的输入**：用户那个文件可能在临时目录、
在网盘挂载点、或者下一分钟就被覆盖——那时指纹会变、条目变 `stale`，而准备受体是按
指纹缓存的下游动作，整条链会以一种看不出病因的方式重新跑一遍。顺带还让「远端取的」与
「本地导的」写进同一个目录，用户在磁盘上找东西只需要看一个地方。

**格式只收 PDB 坐标（`.pdb` / `.ent`），mmCIF 明确拒绝。** 这与「只下 `.pdb`」是
同一条理由：下游读不了 mmCIF，收下一个读不了的受体比当场拒绝更糟。拒绝时必须给出可走
的路（先转格式，或换一个小一些的等价条目），否则用户只会觉得「不支持」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ledger import SessionLedger, fingerprint_bytes, fingerprint_text
from ..tools import Tool, ToolResult, ToolSpec
from ...file_log import record as log_read
from . import http
from .pdbtext import (
    ATOM_ALT_LOC,
    ATOM_CHAIN,
    ATOM_INSERTION_CODE,
    ATOM_RESIDUE_NAME,
    ATOM_RESIDUE_NUMBER,
    WATER_RESIDUES,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_URL = "https://files.rcsb.org/download"

RCSB_ENTRY_URL = "https://www.rcsb.org/structure/{pdb_id}"
"""给人看的条目页。本地导入没有这一项，所以它由调用方显式传入，不进 meta 的默认值。"""

DOWNLOAD_TIMEOUT = 90.0

PDB_ID_PATTERN = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
"""PDB 条目编号：四位，首位是数字（1M17、6VXX、4HHB）。"""

PDB_SUFFIXES = frozenset({".pdb", ".ent"})
"""本地导入能收的扩展名。`.ent` 是 PDB 格式的另一个常见后缀（AlphaFold 输出用它）。"""

MMCIF_SUFFIXES = frozenset({".cif", ".mmcif"})
"""认得出、但**故意不收**的扩展名。

单独列出来是为了给一条「你知道这是什么、为什么不收」的错，而不是混进「不认识的格式」。
mmCIF 是超大结构的唯一形态（见模块开头），下游读不了它。"""

MAX_IMPORT_BYTES = 64 * 1024 * 1024
"""本地导入的体积上限（64 MB，PDB 文本格式顶格的十倍左右）。

不是性能考虑，是**误报的拦截网**：PDB 格式的原子数上限是 10 万，对应文本约 8 MB。
一个远超它的「.pdb」多半是别的什么东西（二进制、打包文件、整个数据库导出）被改了后缀。
让这种文件进来，解析会安静地只读到开头一小部分原子，然后登记成一个「有链、但少了一大截」
的受体——那比直接拒绝危险得多。"""

MAX_LIGANDS_IN_DIGEST = 6
"""摘要里最多列几种配体。

列全没有意义——一套带糖基化的结构能凑出十几种杂原子，而摘要的用途是「让人扫一眼判断
这套结构值不值得用」。完整清单在条目 meta 里（它随登记表存档写盘）。"""

MAX_CHAINS_IN_DIGEST = 12
MAX_HETERO_CHAINS_IN_DIGEST = 3
"""摘要里最多列几条链。

**两种链的上限差这么多，因为对下一步的意义完全不同**：聚合物链是准备受体的必选参数，
每条都要看见；而非聚合物链（糖链、离子、游离配体）动辄十几条——实测 6VXX 有 15 条糖链，
逐条列出来会把真正要看的 A/B/C 三聚体挤到视野之外。上限生效时摘要里会说明略去了几条。"""

UNSAFE_STEM = re.compile(r"[^0-9A-Za-z._-]+")
"""导入时文件名里允许留下的字符。

写盘的这份文件会进路径、进 meta、还会被渲染到终端上，所以名字过一遍白名单比事后转义
省事。卷进来的字符通常是空格、中文、括号、引号——用户从邮件或网盘里存下来的文件很常见。"""


def structure_dir() -> Path:
    """结构文件的存放处，与状态文件、靶点缓存同处一个家目录。"""
    return Path.home() / ".opencode_tui" / "structures"


# ---------------------------------------------------------------------------
# 列位
#
# PDB 是**固定列**格式，列位算错不会报错、只会静默读出一段不对的东西，
# 所以每一个切片都单独成常量并标出规范里的列号（列号 1-based，切片 0-based）。
# 原子行的列位在 pdbtext.py 里已经定义过，这里只补本模块独有的那几段——
# 两处各写一份的话，「链分类」与「链选择」迟早对不上。
# ---------------------------------------------------------------------------
HEADER_CLASSIFICATION = slice(10, 50)
HEADER_DEPOSITION_DATE = slice(50, 59)
HEADER_ID_CODE = slice(62, 66)
"""`HEADER` 里的条目编号列（规范里的 63–66 列）。

**实测核对过**：1M17 / 1ATP / 4HHB / 1D3Z / 6VXX / 7ZSH / 2HHB 七套真实记录的这一列
都是各自的编号、且总长 80。本地导入时靠它认回「这份文件其实是哪个条目」——
从 RCSB 官网手下的文件都带着它。"""

WRAPPED_TEXT = slice(10, 80)
"""`TITLE` / `EXPDTA` 的正文列（规范里的 11–80 列）。

**不能用 `line[6:]`**：这两类记录的续行在 **9–10 列放序号**
（`TITLE    2 ANILINOQUINAZOLINE …`），从第 6 列起切会把它一起读进正文，
拼出来的标题就是「…DOMAIN WITH 4- 2 ANILINOQUINAZOLINE…」。"""

SEQRES_CHAIN = 11
SEQRES_LENGTH = slice(13, 17)
MODRES_RESIDUE_NAME = slice(12, 15)

RESOLUTION_PATTERN = re.compile(r"RESOLUTION\.\s+([0-9.]+)\s+ANGSTROM")


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChainFacts:
    """一条链在**第一套模型**里的事实。"""

    chain_id: str
    is_polymer: bool
    """`SEQRES` 里出现过（或整份文件都没有 SEQRES 时的 ATOM 链）= 聚合物链。

    只有 `HETATM` 的链是糖链、孤立配体或离子——它们不代表一段可对接的肽链，
    但列出来有用：糖基化位点常常就在口袋边上。
    """

    seqres_length: int | None
    """`SEQRES` 里声明的残基数。None = 没有声明（这份文件没写 SEQRES）。"""

    observed_residues: int
    """`ATOM` 里实际出现的残基数（按 (序号, 插入码) 去重）。"""

    atom_count: int
    first_residue: int | None
    last_residue: int | None

    @property
    def missing(self) -> int | None:
        """声明了但没观测到的残基数——解析不清或无序区段的直接证据。

        None 表示无从比较。**注意它可能为 0**：「声明 249、观测 249」是最好的一种情况，
        跟「无从比较」完全不同，所以这里不用 0 兼任两种含义。
        """
        if self.seqres_length is None:
            return None
        return max(0, self.seqres_length - self.observed_residues)


@dataclass(frozen=True)
class StructureFacts:
    """一套坐标文件里解析出来的全部事实。"""

    pdb_id: str
    title: str
    classification: str
    deposition_date: str
    method: str
    resolution: float | None
    models: int
    chains: tuple[ChainFacts, ...]
    """聚合物链。"""

    hetero_chains: tuple[ChainFacts, ...]
    """只在 `HETATM` 里出现的链。"""

    ligands: tuple[tuple[str, int], ...]
    """非水、非修饰残基的杂原子残基 → 残基个数，按个数降序。"""

    modified: tuple[tuple[str, int], ...]
    """`MODRES` 里登记过的残基 → 个数。**它们属于聚合物**，不是该被删掉的配体。"""

    water_atoms: int
    total_atoms: int
    alt_locs: tuple[str, ...]

    @property
    def polymer_chains(self) -> tuple[str, ...]:
        return tuple(chain.chain_id for chain in self.chains)


def _join_wrapped(parts: list[str]) -> str:
    """把跨行的字段拼成一整句。

    **上一段以连字符结尾时不补空格**：化学名里的 `4-anilinoquinazoline` 会被 80 列
    宽度硬折成 `…WITH 4-` + `ANILINOQUINAZOLINE…`，补一个空格就成了 `4- ANILINO…`
    ——在化学人眼里像是拼错了。
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("-"):
            out += part
        else:
            out += " " + part
    return out


def parse_pdb(text: str, *, pdb_id: str = "") -> StructureFacts:
    """把一份 PDB 坐标文件解析成事实。**单遍扫描**——最大的 `.pdb` 有十万行，
    每种记录各扫一遍就是五次全量遍历。

    **只读第一套模型。** NMR 结构动辄十套（实测 1D3Z 有 10 套），把十套的原子数加在
    一起会得到一个既不是「这套结构的规模」、也不是任何真实数字的东西；而按 (残基序号,
    插入码) 去重的统计又不受影响——两个数一错一对最难发现。

    `pdb_id` 留空时从 `HEADER` 的编号列认（本地导入走这条路）；显式给的值优先，
    免得一份内容被改过的文件把调用方说的编号顶上。
    """
    title_parts: list[str] = []
    method_parts: list[str] = []
    classification = ""
    deposition_date = ""
    header_id = ""
    resolution: float | None = None

    seqres_lengths: dict[str, int] = {}
    atom_counts: dict[str, int] = {}
    hetero_counts: dict[str, int] = {}
    residue_keys: dict[str, set[tuple[int, str]]] = {}
    residue_bounds: dict[str, list[int]] = {}
    hetero_residues: dict[str, int] = {}
    seen_hetero_residues: set[tuple[str, int, str]] = set()
    modified_names: set[str] = set()
    alt_locs: set[str] = set()

    models = 0
    water_atoms = 0
    total_atoms = 0
    in_first_model = True

    for line in text.splitlines():
        record = line[:6]

        if record == "ATOM  " or record == "HETATM":
            if not in_first_model:
                continue
            # 列位不够就先跳过：这一步只是**出摘要**，它对列位的要求比编辑那一层低。
            # 被截断的行在这里静默丢掉是刻意的——真正要拦的是后面 `pdbtext.parse`，
            # 它会就同一份文件给出一条「按标准列位读不出来」并指路的错。
            # 在这里抛的话，报出来的是 `IndexError`，而它离病因（列位不对）很远。
            if len(line) <= ATOM_ALT_LOC:
                continue
            chain = line[ATOM_CHAIN]
            residue_name = line[ATOM_RESIDUE_NAME].strip()
            if line[ATOM_ALT_LOC] != " ":
                alt_locs.add(line[ATOM_ALT_LOC])
            total_atoms += 1
            if record == "HETATM":
                hetero_counts[chain] = hetero_counts.get(chain, 0) + 1
                if residue_name in WATER_RESIDUES:
                    water_atoms += 1
                else:
                    try:
                        number = int(line[ATOM_RESIDUE_NUMBER])
                    except ValueError:
                        continue
                    key = (chain, number, line[ATOM_INSERTION_CODE])
                    if key not in seen_hetero_residues:
                        seen_hetero_residues.add(key)
                        hetero_residues[residue_name] = hetero_residues.get(residue_name, 0) + 1
                continue
            atom_counts[chain] = atom_counts.get(chain, 0) + 1
            try:
                number = int(line[ATOM_RESIDUE_NUMBER])
            except ValueError:
                continue
            residue_keys.setdefault(chain, set()).add((number, line[ATOM_INSERTION_CODE]))
            bounds = residue_bounds.setdefault(chain, [number, number])
            bounds[0] = min(bounds[0], number)
            bounds[1] = max(bounds[1], number)
            continue

        if record == "ENDMDL":
            in_first_model = False
        elif record == "MODEL ":
            models += 1
        elif record == "HEADER":
            classification = line[HEADER_CLASSIFICATION].strip()
            deposition_date = line[HEADER_DEPOSITION_DATE].strip()
            header_id = line[HEADER_ID_CODE].strip().upper()
        elif record == "TITLE ":
            title_parts.append(line[WRAPPED_TEXT].strip())
        elif record == "EXPDTA":
            method_parts.append(line[WRAPPED_TEXT].strip())
        elif record == "REMARK" and line.startswith("REMARK   2 RESOLUTION."):
            if resolution is None:
                resolution = _parse_resolution(line)
        elif record == "MODRES":
            modified_names.add(line[MODRES_RESIDUE_NAME].strip())
        elif record == "SEQRES":
            try:
                declared = int(line[SEQRES_LENGTH])
            except ValueError:
                continue
            seqres_lengths[line[SEQRES_CHAIN]] = declared

    def build(chain: str, *, is_polymer: bool) -> ChainFacts:
        bounds = residue_bounds.get(chain)
        return ChainFacts(
            chain_id=chain,
            is_polymer=is_polymer,
            seqres_length=seqres_lengths.get(chain) if is_polymer else None,
            observed_residues=len(residue_keys.get(chain, ())),
            atom_count=atom_counts.get(chain, 0) + hetero_counts.get(chain, 0),
            first_residue=bounds[0] if bounds else None,
            last_residue=bounds[1] if bounds else None,
        )

    # 没有 SEQRES 的文件（少见但合法）退回按 ATOM 认链：那些链确实是聚合物，
    # 不能因为文件没写 SEQRES 就把整份结构说成「没有肽链」。
    base_ids = sorted(seqres_lengths) if seqres_lengths else sorted(atom_counts)
    chains = tuple(build(chain, is_polymer=True) for chain in base_ids)
    hetero_only = sorted(set(hetero_counts) - set(base_ids))

    def ranked(names: set[str]) -> tuple[tuple[str, int], ...]:
        return tuple(
            sorted(
                ((name, count) for name, count in hetero_residues.items() if name in names),
                key=lambda item: (-item[1], item[0]),
            )
        )

    return StructureFacts(
        pdb_id=pdb_id or (header_id if PDB_ID_PATTERN.match(header_id) else ""),
        title=_join_wrapped(title_parts),
        classification=classification,
        deposition_date=deposition_date,
        method=_join_wrapped(method_parts),
        resolution=resolution,
        models=models or 1,
        chains=chains,
        hetero_chains=tuple(build(chain, is_polymer=False) for chain in hetero_only),
        ligands=ranked(set(hetero_residues) - modified_names),
        modified=ranked(modified_names),
        water_atoms=water_atoms,
        total_atoms=total_atoms,
        alt_locs=tuple(sorted(alt_locs)),
    )


def _parse_resolution(line: str) -> float | None:
    """`REMARK   2 RESOLUTION.    2.20 ANGSTROMS.` → 2.2

    NMR 结构的这一行写的是 `NOT APPLICABLE.`——那不是缺失数据，而是「分辨率这个
    概念对它不适用」，所以这里安静地返回 None 而不是报错。
    """
    match = RESOLUTION_PATTERN.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - 正则已限定为数字与点
        return None


def normalize_pdb_id(raw: str) -> str:
    """校验并规范化 PDB 编号。大小写不敏感，输出一律大写。"""
    text = str(raw).strip().upper()
    if not PDB_ID_PATTERN.match(text):
        raise ValueError(
            f"{raw!r} 不是 PDB 条目编号。编号是四位、首位为数字，例如 1M17、6VXX。"
            "UniProt 编号（P00533）与 AlphaFold 编号（AF-P00533-F1）都不是它——"
            "要取结构，先用 fetch_target 查这个靶点的可用结构清单，从里面挑一个编号"
        )
    return text


# ---------------------------------------------------------------------------
# 网络与写盘
# ---------------------------------------------------------------------------
def _structure_path(pdb_id: str) -> Path:
    return structure_dir() / f"{pdb_id}.pdb"


def _download(pdb_id: str, *, timeout: float) -> str:
    """取坐标文件。**错误分两档，因为两种 404 的成因完全不同。**

    `.pdb` 与 `.cif` 都没有 → 编号不存在，改参数才有用（不重试）。
    只有 `.cif` → 结构超出 PDB 格式上限，这个编号取不回来（不重试，但要说清原因、
    并给出可走的路）。

    网络那一层（超时、429、5xx）由 `http` 统一翻译成 `RetryableToolError`，
    404 由 `get_text_or_none` 变成 None，这里再按 `.cif` 是否存在分档；其余 4xx 原样抛出。
    """
    url = f"{BASE_URL}/{pdb_id}.pdb"
    text = http.get_text_or_none(url, {}, timeout=timeout, headers={"Accept": "text/plain"})

    if text is None:
        if http.probe(f"{BASE_URL}/{pdb_id}.cif", timeout=timeout) == 200:
            raise http.ToolError(
                f"{pdb_id} 超出了 PDB 格式的上限（约 62 条链 / 10 万原子），RCSB 对这类"
                "条目只提供 mmCIF、不提供 .pdb，而受体准备读不了 mmCIF。可行的做法："
                "换一个覆盖同一蛋白、规模小一些的结构（用 fetch_target 看有没有别的"
                "条目），或者缩小范围（只取其中一个结构域）"
            )
        raise http.ToolError(
            f"RCSB 里没有编号为 {pdb_id} 的条目。请核对编号——"
            "它必须来自 fetch_target 返回的可用结构清单"
        )

    if "ATOM  " not in text and "HETATM" not in text:
        # 200 而内容是别的东西（代理拦截页、配额提示页）。存下去只会变成一份
        # 「有文件、但读不出任何链」的假产物，不如当场失败。
        raise http.ToolError(
            f"RCSB 返回的内容不是坐标文件（HTTP 200、{len(text)} 字节，"
            f"开头是 {(text.strip()[:80] or '空')!r}）"
        )
    return text


def _write_structure(path: Path, data: str | bytes) -> None:
    """写盘坐标文件。

    先写 `.tmp` 再替换：中途写坏留下的是半截坐标文件，而下次调用会把它当成有效缓存
    读进来（解析出来少几条链、或者一条链都没有）。

    **用字节写入而不是 `write_text`**：后者默认把换行翻译成 `os.linesep`，于是同一份
    结构在 Windows 上写盘是 CRLF、Linux 上是 LF——内容相同、字节不同，内容指纹也就
    不同，跨平台的「这是同一份结构吗」判断失效。读写两侧都走字节，指纹才是内容的函数
    而不是平台的函数。这也是本模块不需要 sidecar 元数据的前提：文件本身就是全部事实。

    收 bytes 是因为**本地导入必须逐字节保真**：用户给的那份文件算出来的指纹，得和复制
    进来之后算出来的一致，否则「同一份结构」立刻变成两份。文本调用方（下载回来的响应
    文本）走同一段编码，不受影响。
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _read_structure(path: Path) -> str | None:
    """读本地坐标文件。**读坏了当作没有**——一份半截文件不该把工具永久堵住。"""
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except UnicodeDecodeError:
        return None


def _relative(path: Path) -> str:
    """相对家目录展示，一律 POSIX 分隔符——反斜杠容易被原样抄进别处去拼接。"""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# 摘要渲染
# ---------------------------------------------------------------------------
def _span(chain: ChainFacts) -> str:
    if chain.first_residue is None:
        return "无观测残基"
    return f"{chain.first_residue}–{chain.last_residue}"


def _chain_line(chain: ChainFacts) -> str:
    """一条链一行。**三种链的措辞完全不同**，因为它们对下一步的意义不同：

    - 非聚合物链（糖链、孤立配体）：不是可对接的肽链
    - 没写 SEQRES 的聚合物链：观测到了残基，但无从比较缺口
    - 常规聚合物链：声明 vs 观测，缺口数是最要紧的那个数字
    """
    if not chain.is_polymer:
        return f"链 {chain.chain_id}：非聚合物（{chain.atom_count} 原子，全为杂原子）"

    atom_text = f"{chain.atom_count} 原子"
    if chain.seqres_length is None:
        return (
            f"链 {chain.chain_id}：未声明 SEQRES · 观测到 {chain.observed_residues} 残基"
            f"（{_span(chain)}）· {atom_text}"
        )

    gap = chain.missing or 0
    # 「缺 N 个残基」是这一行里最要紧的信息，不能因为 N 是 0 就变成一句模糊的好话
    gap_text = f"缺 {gap} 个残基（无序或解析不清）" if gap else "无缺口"
    return (
        f"链 {chain.chain_id}：声明 {chain.seqres_length} 残基 · "
        f"观测到 {chain.observed_residues}（{_span(chain)}）· {atom_text} · {gap_text}"
    )


def _chain_lines(
    chains: tuple[ChainFacts, ...],
    *,
    limit: int,
    omitted: str = "",
) -> list[str]:
    """列出链，超上限时**说明略去了几条**。

    略过而不吭声是这一层最容易犯的错：读者会以为「这份结构就这几条链」，然后在准备
    受体时选了一条不属于最近邻的链。说了「另有 N 条」至少让人知道要去找。
    """
    lines = [f"  {_chain_line(chain)}" for chain in chains[:limit]]
    extra = len(chains) - limit
    if extra > 0:
        lines.append(
            f"  （另有 {extra} 条链未列出{omitted}；完整链清单在条目 meta 里，"
            "用 describe_entry 展开）"
        )
    return lines


def _format_counts(items: tuple[tuple[str, int], ...], *, limit: int) -> str:
    shown = "、".join(f"{name}×{count}" for name, count in items[:limit])
    extra = len(items) - limit
    return f"{shown} 等 {extra} 种" if extra > 0 else shown


def _render(
    facts: StructureFacts,
    *,
    path: Path,
    fingerprint: str,
    origin: str,
    registered: str,
    size: int,
    label: str | None = None,
) -> str:
    """给模型看的摘要。**下载来的与本地导入的走同一段代码**。

    `label` 是首行那个「这套坐标是谁」的标识：远端来的写 `PDB 1M17`，本地导入的写
    文件里的编号（有的话）或「本地结构」。两类来源的事实字段完全一样，差别只在这一个
    词上——那就不该有第二份渲染函数。
    """
    head = label if label else (f"PDB {facts.pdb_id}" if facts.pdb_id else "结构")
    resolution = f" · {facts.resolution:g} Å" if facts.resolution is not None else ""
    lines = [
        f"{head} · {facts.method or '方法未知'}{resolution} · "
        f"{len(facts.chains)} 条聚合物链 · 共 {facts.total_atoms} 原子"
    ]
    if facts.title:
        lines.append(f"  标题：{facts.title}")

    meta = " · ".join(
        part
        for part in (
            facts.classification,
            f"沉积于 {facts.deposition_date}" if facts.deposition_date else "",
            f"{facts.models} 套模型（统计只算第一套）" if facts.models > 1 else "",
        )
        if part
    )
    if meta:
        lines.append(f"  {meta}")

    lines.extend(_chain_lines(facts.chains, limit=MAX_CHAINS_IN_DIGEST))
    lines.extend(
        _chain_lines(
            facts.hetero_chains,
            limit=MAX_HETERO_CHAINS_IN_DIGEST,
            omitted="（糖链 / 离子 / 游离配体——通常不是对接要用的链）",
        )
    )

    if facts.ligands:
        lines.append(
            f"  配体（非水杂原子，{len(facts.ligands)} 种）："
            f"{_format_counts(facts.ligands, limit=MAX_LIGANDS_IN_DIGEST)}"
        )
    else:
        lines.append("  配体：没有非水杂原子——这套结构里没有可借用的共晶配体")
    if facts.modified:
        lines.append(
            f"  修饰残基（属于聚合物，别当成配体删）："
            f"{_format_counts(facts.modified, limit=MAX_LIGANDS_IN_DIGEST)}"
        )
    if facts.water_atoms:
        lines.append(f"  水：{facts.water_atoms} 个原子（准备受体时默认去掉）")
    if facts.alt_locs:
        lines.append(f"  交替构象标记：{'/'.join(facts.alt_locs)}（准备受体时要选定一个）")
    if not facts.title and not facts.method:
        # 空着不说，读者会以为「标题没解析出来」是工具的问题；而它往往是文件本身
        # 就没有这些段——同源建模、AlphaFold 输出、冷冻电镜的局部重建都这样
        lines.append(
            "  这份文件没有 HEADER / TITLE / EXPDTA 段（建模与预测输出的常见情况）："
            "标题、方法、分辨率无从得知，上面的链与原子统计是直接从坐标算出来的"
        )

    lines.append(f"  → {origin} {_relative(path)}（{size / 1024:.0f} KB，指纹 {fingerprint}）")
    lines.append(f"  → {registered}")
    lines.append(
        "  准备受体时必须显式指定用哪条链——上面这份链清单要留着看。同源二聚体会给出"
        "几条几乎一样的链，选错不报错，只是口袋没了。"
    )
    return "\n".join(lines)


def _entry_summary(facts: StructureFacts, *, source: str = "rcsb") -> str:
    """登记表条目的一行摘要。它会被别的工具在列表里复读，所以要能独立读懂。

    `source` 进摘要而不只进 meta：列表里可能同时躺着「远端取的 1M17」和「本地导入的
    突变体」，而它们都是 receptor、都是 stage="raw"，只靠序号分不出哪个是哪个。
    """
    head = f"原始结构 PDB {facts.pdb_id}" if facts.pdb_id else "原始结构（本地文件）"
    parts = [head, facts.method or "方法未知"]
    if source != "rcsb":
        parts.append(f"来源 {source}")
    if facts.resolution is not None:
        parts.append(f"{facts.resolution:g} Å")
    parts.append(f"{len(facts.chains)} 链")
    if facts.ligands:
        parts.append("配体 " + "/".join(name for name, _ in facts.ligands[:3]))
    return " · ".join(parts)


def _entry_meta(
    facts: StructureFacts,
    path: Path,
    *,
    source: str = "rcsb",
    source_url: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """挂在登记表条目上的结构化字段。

    链的**完整**清单放在这里（摘要里只放条数与配体名）：准备受体要按链选，而 meta 是
    那份清单在工具返回值之外的唯一去处——登记表存档里存的就是它，所以刷新一次会话、
    隔天再来，链清单仍然在。

    `stage="raw"` 是给「准备后的受体」留的对照位：两者都是 receptor 类型、在列表里
    长得差不多，而「哪个能直接喂给对接」是模型最容易搞错的一件事。准备后的受体用
    `stage="prepared"`。

    `source_url` 对本地导入是空的，此时**不写这个键**而不是写空串：一个空 URL 在界面
    上会渲染成可点的空链接，而「没有来源页」才是事实。
    """
    try:
        retrieved_at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        retrieved_at = ""
    meta: dict[str, Any] = {
        "source": source,
        "pdb_id": facts.pdb_id,
        "stage": "raw",
        "format": "pdb",
        "title": facts.title,
        "classification": facts.classification,
        "deposition_date": facts.deposition_date,
        "method": facts.method,
        "resolution": facts.resolution,
        "models": facts.models,
        "chains": [
            {
                "chain_id": chain.chain_id,
                "polymer": chain.is_polymer,
                "seqres_length": chain.seqres_length,
                "observed_residues": chain.observed_residues,
                "missing_residues": chain.missing,
                "first_residue": chain.first_residue,
                "last_residue": chain.last_residue,
                "atom_count": chain.atom_count,
            }
            for chain in (*facts.chains, *facts.hetero_chains)
        ],
        "ligands": [name for name, _ in facts.ligands],
        "modified_residues": [name for name, _ in facts.modified],
        "water_atoms": facts.water_atoms,
        "alt_locs": list(facts.alt_locs),
        "total_atoms": facts.total_atoms,
        "retrieved_at": retrieved_at,
        "file": _relative(path),
    }
    if source_url:
        meta["source_url"] = source_url
    if extra:
        meta.update(extra)
    return meta


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class FetchStructureTool(Tool):
    """按 PDB 编号取一套实验结构，登记成 receptor。"""

    spec = ToolSpec(
        name="fetch_structure",
        description=(
            "取一套实验结构的坐标文件（PDB 条目），写盘并登记成一个**受体**（状态：原始"
            "结构，还没准备）。返回这套结构里有什么：方法、分辨率、每条链声明与观测的残基"
            "范围（缺口就是对接用不上的那几段）、共晶配体、水、交替构象。**同一个编号"
            "第二次调用直接读盘、不联网**（要重新下载用 refresh=true）。"
            "编号形如 1M17，必须来自 fetch_target 返回的可用结构清单——UniProt 编号"
            "（P00533）与 AlphaFold 编号（AF-P00533-F1）都不是它。**超出 PDB 格式上限的"
            "巨大结构（约 62 条链 / 10 万原子）取不回来**：RCSB 对那类条目只提供 mmCIF，"
            "而受体准备读不了 mmCIF，所以这里会明确报错并建议换一个小一些的结构。"
            "返回的链清单是准备受体的必要输入（同源二聚体会给出几条几乎一样的链），"
            "用哪条链必须显式指定。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pdb_id": {
                    "type": "string",
                    "description": "四位的 PDB 条目编号，例如 1M17（大小写不敏感）",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "true 时忽略本地文件强制重新下载（认为远端条目已修订时才用）",
                },
            },
            "required": ["pdb_id"],
        },
    )
    primary_argument = "pdb_id"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：fetch_structure 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        try:
            code = normalize_pdb_id(str(arguments.get("pdb_id") or ""))
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        refresh = bool(arguments.get("refresh"))
        path = _structure_path(code)

        text = None if refresh else _read_structure(path)
        if text is None:
            try:
                text = _download(code, timeout=DOWNLOAD_TIMEOUT)
            except http.RetryableToolError as error:
                return ToolResult(output=str(error), failed=True)
            except http.ToolError as error:
                return ToolResult(output=str(error), failed=True)
            try:
                _write_structure(path, text)
            except OSError as error:
                return ToolResult(
                    output=f"写盘 {_relative(path)} 失败（{type(error).__name__}：{error}）",
                    failed=True,
                )
            origin = "已下载并写盘"
        else:
            origin = "读自本地文件（未联网；需要刷新用 refresh=true）"

        facts = parse_pdb(text, pdb_id=code)
        # 指纹从内存里的文本算，不再读一遍文件：_write_structure 用字节写入，两者必然
        # 一致（见那里的说明），所以这不会让同一份结构算出两个指纹。
        fingerprint = fingerprint_text(text)
        size = len(text.encode("utf-8"))

        # 登记而不是每次新建：同一个编号被反复取用是常态（换着问链、问配体）。
        # include_archived 是必要的：切换受体会把旧条目移出默认视图，那时若看不见它，
        # 同一份结构会被重复登记一份——而重复的受体条目会让「产物属于哪个受体」变形。
        existing = ledger.find(kind="receptor", fingerprint=fingerprint, include_archived=True)
        if existing:
            registered = f"登记表里已有同一份：{existing[0].handle}（未重复登记）"
        else:
            entry = ledger.add(
                "receptor",
                path=path,
                summary=_entry_summary(facts),
                fingerprint=fingerprint,
                meta=_entry_meta(
                    facts, path, source_url=RCSB_ENTRY_URL.format(pdb_id=code)
                ),
            )
            registered = f"已登记 {entry.handle}"

        return ToolResult(
            output=_render(
                facts,
                path=path,
                fingerprint=fingerprint,
                origin=origin,
                registered=registered,
                size=size,
                label=f"PDB {code}",
            ),
            meta={
                "pdb_id": code,
                "fingerprint": fingerprint,
                "chains": list(facts.polymer_chains),
            },
        )


# ---------------------------------------------------------------------------
# 本地导入
# ---------------------------------------------------------------------------
def _import_name(source: Path, facts: StructureFacts) -> str:
    """给复制进来的文件起名。

    能从 `HEADER` 认回条目编号就用编号——从 RCSB 官网手下的文件都带着它，用编号当名字
    可以直接对上「这套结构是哪个条目」。认不出来才退回原文件名。
    """
    if facts.pdb_id:
        return facts.pdb_id
    stem = UNSAFE_STEM.sub("_", source.stem).strip("._-")
    return stem or "imported"


def _import_target(directory: Path, name: str, payload: bytes) -> Path:
    """挑一个写盘位置。

    同名且**内容相同** → 复用：同一个文件被导入两次是常态（换个会话再导一次、或者用户
    重跑了一遍自己的建模流程），这时不该在磁盘上留下第二份。

    同名但内容不同 → 加序号，**绝不覆盖**。覆盖掉的是另一个结构，而它在登记表里还有
    条目——那条条目会变成 stale，而用户回到磁盘上已经找不到原来那份了。
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{name}.pdb"
    if candidate.exists():
        try:
            if candidate.read_bytes() == payload:
                return candidate
        except OSError:
            # 读不了（占用中、权限）就当作「内容不同」，走加序号那条路
            pass
        for index in range(1, 1000):
            candidate = directory / f"{name}-{index}.pdb"
            if not candidate.exists():
                return candidate
        raise http.ToolError(
            f"{directory} 下已经有 {name}.pdb 到 {name}-999.pdb 这一批同名文件，"
            "无法安全写盘。请清理这个目录，或把要导入的文件改个名字"
        )
    return candidate


def _check_importable(source: Path) -> bytes:
    """读入并做**格式与体量的准入检查**，返回原始字节。

    三样都要在解析之前挡住，理由各不相同：

    - 目录 / 不存在 / 读不动：路径错，早点说清楚，别让它变成解析出来的空结构
    - 扩展名不在白名单：**认得出的**（mmCIF）单独给一条可操作的错，其余列出能收的
    - 体量超限：PDB 文本格式顶格约 8 MB，超得太多基本是别的文件改了后缀。放它进来的
      后果是解析安静地只读到开头一部分，然后登记成一个「有链、但少了一大截」的受体
      ——比直接拒绝危险得多
    """
    try:
        if not source.exists():
            log_read(source, purpose="import.structure", ok=False, detail="文件不存在")
            raise http.ToolError(
                f"找不到文件 {source}。请用**绝对路径**，并确认它在这台机器上"
                "（登记表里已有的结构请直接用 handle，不要再传路径）"
            )
        if source.is_dir():
            log_read(source, purpose="import.structure", ok=False, detail="是目录")
            raise http.ToolError(
                f"{source} 是一个目录。导入要的是**一个坐标文件**——"
                "若这是一份准备后的受体产物，它已经作为 receptor 条目登记过，"
                "用 list_workspace 找到它的 handle 即可"
            )
        payload = source.read_bytes()
        log_read(source, purpose="import.structure", size=len(payload))
    except http.ToolError:
        raise
    except OSError as error:
        log_read(source, purpose="import.structure", ok=False, detail=f"{type(error).__name__}：{error}")
        raise http.ToolError(f"读不到 {source}（{type(error).__name__}：{error}）") from error

    suffix = source.suffix.lower()
    if suffix in MMCIF_SUFFIXES:
        raise http.ToolError(
            f"{source.name} 是 mmCIF 格式，本工具不收：下游的受体准备读不了 mmCIF。"
            "可行的做法：用 gemmi / PyMOL 之类先转成 .pdb 再导入，"
            "或者换一个同样覆盖这个蛋白的小一些的条目（用 fetch_target 看可用结构清单）"
        )
    if suffix not in PDB_SUFFIXES:
        raise http.ToolError(
            f"{source.name} 的扩展名 {suffix or '（无）'} 不在可导入之列。"
            f"只收 PDB 坐标格式：{' / '.join(sorted(PDB_SUFFIXES))}"
            "（同源建模与 AlphaFold 的输出通常就是 .pdb）"
        )

    if len(payload) > MAX_IMPORT_BYTES:
        raise http.ToolError(
            f"{source.name} 有 {len(payload) / 1024 / 1024:.1f} MB，超过"
            f" {MAX_IMPORT_BYTES / 1024 / 1024:.0f} MB 的上限。PDB 文本格式的原子数上限是"
            " 10 万、对应约 8 MB，所以这么大的「坐标文件」大概率不是坐标文件"
            "（或者是把多条链 / 多套模型堆在一起的文件）。请确认来源"
        )
    if b"\x00" in payload:
        raise http.ToolError(f"{source.name} 里有空字节，它不是文本坐标文件（可能被压缩或加密过）")
    if not payload.strip():
        raise http.ToolError(f"{source.name} 是空文件")
    return payload


def _parse_imported(source: Path, payload: bytes) -> tuple[StructureFacts, str]:
    """解析导入的坐标文件，并挡住「解析得出来但没意义」的几种内容。"""
    text = payload.decode("utf-8", errors="replace")
    if "ATOM  " not in text and "HETATM" not in text:
        raise http.ToolError(
            f"{source.name} 里没有 ATOM / HETATM 记录，读不出任何坐标。"
            f"开头是 {(text.strip()[:80] or '空')!r}——请确认它是 PDB 坐标文件"
            "（HTML 下载页、压缩包、日志文件改了后缀都会长成这样）"
        )
    facts = parse_pdb(text)
    if not facts.chains:
        names = "、".join(name for name, _ in facts.ligands[:5]) or "（无）"
        raise http.ToolError(
            f"{source.name} 里只有杂原子、没有聚合物链（见到的残基：{names}），"
            "它不是一份受体结构。对接的受体需要 ATOM 记录——"
            "如果你手上的是配体文件，它该进的是配体集，不是这里"
        )
    return facts, text


def _label(facts: StructureFacts) -> str:
    """首行那个「这套坐标是谁」的标识。

    认得出编号就报编号（从 RCSB 手下的文件都带 `HEADER`），认不出就明说这是本地结构
    ——**不要编一个空串或占位符**：那是摘要的第一行，模型靠它认「我手上是哪一套」。
    """
    return f"PDB {facts.pdb_id}" if facts.pdb_id else "本地结构（文件里没有条目编号）"


class ImportStructureTool(Tool):
    """导入本机的一个坐标文件，登记成 receptor。**全库两个收路径的动作之一。**"""

    spec = ToolSpec(
        name="import_structure",
        description=(
            "导入**本机的一份坐标文件**（.pdb / .ent），登记成一个**受体**（状态：原始"
            "结构，还没准备）。自备结构的入口：同源建模的模型、突变体、冷冻电镜的局部"
            "重建、从别的渠道拿到的结构文件都从这里进。"
            "这份文件会被**复制进结构目录**：用户那个文件可能待会儿就被覆盖或删掉，而"
            "准备受体是按指纹缓存的，原地引用会让整条链在下游以看不出病因的方式重跑一遍。"
            "导入后它和远端取回的条目完全一样——用返回的 handle，不要再传路径。"
            "**这是全套工具里两个收路径的动作之一**（另一个是 import_ligandset；其余"
            "一律收 handle）：模型编不出 ligandset#3，却编得出 D:/work/model.pdb，"
            "而自备结构是真实需求，所以把「收路径」压缩到这两个动作上。若同一个文件已经"
            "导入过（内容相同），"
            "**不会重复登记**，直接告诉你已有哪一个。想从 RCSB 取编号对应的结构请用"
            " fetch_structure；mmCIF 不收（下游读不了）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "本机坐标文件的路径，建议给绝对路径",
                }
            },
            "required": ["path"],
        },
    )
    primary_argument = "path"
    context_argument = "ledger"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ledger = arguments.get("ledger")
        if not isinstance(ledger, SessionLedger):
            return ToolResult(
                output="内核装配问题：import_structure 需要一个会话登记表，但这次调用没有拿到。",
                failed=True,
            )
        source = Path(str(arguments.get("path") or "")).expanduser()
        try:
            payload = _check_importable(source)
            facts, _ = _parse_imported(source, payload)
        except http.ToolError as error:
            return ToolResult(output=str(error), failed=True)

        # 指纹算的是**原始字节**：导入这条路要求逐字节保真（见 _write_structure），
        # 用「解码再编码」后的文本算指纹，会在遇到非 UTF-8 字节时（replace 会把它们
        # 变成替代符）让两份不同内容的文件算出同一个指纹。下载那条路用文本算，
        # 因为它本来就是文本，而且写盘走同一段编码。
        fingerprint = fingerprint_bytes(payload)
        size = len(payload)

        # 先按指纹查重再写盘：重复导入是常态（换个会话、重跑一遍建模流程），
        # 而每导一次就在磁盘上多留一份拷贝，久了没人分得清哪份是哪次的结果。
        # include_archived 与 fetch_structure 同理：切换受体后旧条目会移出默认视图。
        existing = ledger.find(kind="receptor", fingerprint=fingerprint, include_archived=True)
        if existing:
            return ToolResult(
                output=_render(
                    facts,
                    path=existing[0].path or source,
                    fingerprint=fingerprint,
                    origin=(
                        f"内容与登记表里已有的 {existing[0].handle} 完全相同"
                        "（未复制、未重复登记）"
                    ),
                    registered=f"继续用 {existing[0].handle} 即可",
                    size=size,
                    label=_label(facts),
                ),
                meta={"handle": str(existing[0].handle), "fingerprint": fingerprint},
            )

        try:
            target = _import_target(structure_dir(), _import_name(source, facts), payload)
            _write_structure(target, payload)
        except http.ToolError as error:
            return ToolResult(output=str(error), failed=True)
        except OSError as error:
            return ToolResult(
                output=f"写盘结构目录失败（{type(error).__name__}：{error}）",
                failed=True,
            )

        entry = ledger.add(
            "receptor",
            path=target,
            summary=_entry_summary(facts, source="local"),
            fingerprint=fingerprint,
            meta=_entry_meta(
                facts,
                target,
                source="local",
                extra={"imported_from": str(source.resolve())},
            ),
        )
        return ToolResult(
            output=_render(
                facts,
                path=target,
                fingerprint=fingerprint,
                origin=f"已复制进结构目录并写盘（原文件 {source} 未被动过）",
                registered=f"已登记 {entry.handle}",
                size=size,
                label=_label(facts),
            ),
            meta={"handle": str(entry.handle), "fingerprint": fingerprint},
        )


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具，按装配顺序。"""
    return [FetchStructureTool(), ImportStructureTool()]

if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
