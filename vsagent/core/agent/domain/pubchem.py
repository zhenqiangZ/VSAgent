"""PubChem：化合物的身份、计算性质与全库相似检索（PUG REST）。

这个模块是**「我手上这个分子是什么」的唯一入口**：名称 / CID / SMILES / InChIKey /
ChEMBL 编号都收，一次调用给出身份、计算性质，以及（在 ChEMBL 里有记录时）成药状态、
类药性、作用靶点与适应症。

**为什么把 ChEMBL 那半并进来。** 原先两个工具：`describe_molecule` 只认 ChEMBL 编号、
`lookup_compound` 认任意标识。模型于是要先判断「手里的是 CHEMBL939 还是一段 SMILES」
再挑工具，判错就是一次白跑——而对出来的对接产物、枚举出来的类似物，模型手里往往
**只有 SMILES**，那条路对它根本不成立。合并之后：给编号走 ChEMBL → PubChem，给结构
走 PubChem → ChEMBL，两个方向在输出里长得一样。

**两半靠 InChIKey 接起来，不靠名字也不靠 SMILES。** PubChem 不给 ChEMBL 编号（同义词里
偶尔混进来一个，不可依赖），而 ChEMBL 的 `molecule_structures__standard_inchi_key__iexact`
实测精确命中（阿司匹林 `BSYNRYMUTXBXSQ-UHFFFAOYSA-N` → 且只 → `CHEMBL25`）。
InChIKey 是哈希，比「两边规范化出来的 SMILES 字符串相等」可靠。

三条口径值得单独说明：

**性质是算出来的，不是测出来的。** XLogP / TPSA / 氢键计数 / 可旋转键都是 PubChem 用
规则算的，跟实验 logP 不是一回事。输出里必须写明「计算值」，否则模型会拿它当实验数据
引用，而这两者在小数点后一位上就可能对不上。

**相似检索没有分数。** PUG 的相似性接口只回 CID 列表（按相似度从高到低排），不给
Tanimoto 值。所以这里也不编一个分数出来，只如实说「按接口顺序」——编一个 0–1 的数
会让模型以为它能拿去和本地 `screen_similarity` 的分数比较，而那是两套指纹。

**相似性走 `fastsimilarity_2d`，不走 `/similarity/`。** 标准路径是异步的：先回 202 与
一个 `ListKey`，再轮询 `/compound/listkey/<key>/cids/JSON`，实测要 1–2 秒、多一次请求；
而 `fastsimilarity_2d` 同步返回，实测两者的条数与顺序完全一致（同一段 SMILES、同一阈值
逐档比对过）。少一次轮询就少一处超时与状态机，所以选同步那条。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import quote

from ..tools import Tool, ToolError, ToolResult, ToolSpec
from . import chembl, http

__all__ = [
    "BASE_URL",
    "CID_PATTERN",
    "DEFAULT_LIMIT",
    "DEFAULT_THRESHOLD",
    "FindSimilarCompoundsTool",
    "INCHIKEY_PATTERN",
    "LookupCompoundTool",
    "MAX_LIMIT",
    "MAX_SYNONYMS",
    "MIN_THRESHOLD",
    "PROPERTIES",
    "PubChemError",
    "domain_tools",
    "find_similar",
    "lookup_compound",
]

BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
COMPOUND_URL = "https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"

PROPERTIES = (
    "Title,MolecularFormula,MolecularWeight,XLogP,TPSA,HBondDonorCount,"
    "HBondAcceptorCount,RotatableBondCount,SMILES,ConnectivitySMILES,InChIKey,"
    "IUPACName,Charge"
)
"""一次要回的全部字段。

**`SMILES` / `ConnectivitySMILES` 这两个名字不是笔误**：PUG 把「带立体化学的规范 SMILES」
放在 `SMILES` 键下、把不含立体的放在 `ConnectivitySMILES` 键下。按直觉写
`CanonicalSMILES` 也收，但回来的键名仍是这两个，读的时候只能按返回的键取。"""

CID_PATTERN = re.compile(r"^(?:CID[:\s#-]*)?(\d{1,9})$", re.I)
INCHIKEY_PATTERN = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$", re.I)
SMILES_HINT = re.compile(r"[=#\[\]()@+\\/]")
"""看起来像 SMILES 的字符。**只当线索，不当判据**：`CCO`（乙醇）一个提示字符都没有，
所以名称查不到时还要再拿它当 SMILES 试一次（见 `lookup_compound` 的候选顺序）。"""

DEFAULT_THRESHOLD = 90
MIN_THRESHOLD = 50
MAX_THRESHOLD = 100
"""相似度阈值（百分比）。

**下限设 50 而不是 1**：阈值越低命中越多（实测阿司匹林在 90 时有 6966 条），
模型拿到几千个 CID 也没法用；50 以下基本是「同一个骨架但取代基完全不同」，
那是骨架检索的活，不是相似检索。"""

DEFAULT_LIMIT = 10
MAX_LIMIT = 20
"""一次列多少个化合物。每个都要带分子式与 SMILES，再多就该换更高的阈值。"""

MAX_SYNONYMS = 8
MAX_SYNONYM_CIDS = 3
"""别名的上限：一次最多看 8 个名字、最多为 3 个 CID 取别名。

同义词接口**没有分页参数**，实测阿司匹林一次回 23 KB、上千个名字；全部读进来只是
为了印前几个，白占内存与时间。"""


class PubChemError(ToolError):
    """PubChem 的返回不可用（结构串解析不了、响应形状变了）。**不重试到底。**"""


@dataclass
class Compound:
    """一条 PubChem 记录里我们真正用得上的字段。"""

    cid: int
    title: str = ""
    formula: str = ""
    weight: float | None = None
    smiles: str = ""
    connectivity: str = ""
    inchikey: str = ""
    iupac: str = ""
    charge: int | None = None
    xlogp: float | None = None
    tpsa: float | None = None
    donors: int | None = None
    acceptors: int | None = None
    rotatable: int | None = None
    synonyms: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return COMPOUND_URL.format(cid=self.cid)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "title": self.title,
            "formula": self.formula,
            "weight": self.weight,
            "smiles": self.smiles,
            "inchikey": self.inchikey,
            "iupac": self.iupac,
            "charge": self.charge,
            "xlogp": self.xlogp,
            "tpsa": self.tpsa,
            "hbond_donors": self.donors,
            "hbond_acceptors": self.acceptors,
            "rotatable_bonds": self.rotatable,
            "synonyms": list(self.synonyms),
            "url": self.url,
        }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compound_from(row: dict[str, Any]) -> Compound | None:
    cid = _integer(row.get("CID"))
    if cid is None:
        return None
    return Compound(
        cid=cid,
        title=str(row.get("Title") or "").strip(),
        formula=str(row.get("MolecularFormula") or "").strip(),
        weight=_number(row.get("MolecularWeight")),
        smiles=str(row.get("SMILES") or "").strip(),
        connectivity=str(row.get("ConnectivitySMILES") or "").strip(),
        inchikey=str(row.get("InChIKey") or "").strip(),
        iupac=str(row.get("IUPACName") or "").strip(),
        charge=_integer(row.get("Charge")),
        xlogp=_number(row.get("XLogP")),
        tpsa=_number(row.get("TPSA")),
        donors=_integer(row.get("HBondDonorCount")),
        acceptors=_integer(row.get("HBondAcceptorCount")),
        rotatable=_integer(row.get("RotatableBondCount")),
    )


def _property_url(namespace: str, value: str) -> str:
    return f"{BASE_URL}/compound/{namespace}/{quote(value, safe='')}/property/{PROPERTIES}/JSON"


def _rows(payload: Any) -> list[dict[str, Any]]:
    table = payload.get("PropertyTable") if isinstance(payload, dict) else None
    rows = table.get("Properties") if isinstance(table, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


BAD_STRUCTURE_MARKERS = ("Search status indicates failure", "PUGREST.ServerError")
"""PUG 把「这段 SMILES 解析不了」也回成 HTTP 500，只能按正文里的 Fault 码认。

**不能一律把 5xx 当成参数错**：PubChem 忙的时候回的是 `PUGREST.ServerBusy`，
那一条是瞬时故障，交给内核重试一次才有意义。用 `server_5xx_as_error` 会把两种
都压成永久失败——模型于是被告知「你的结构写错了」，而它写得没错。"""


def _is_bad_structure(error: BaseException) -> bool:
    text = str(error)
    return any(marker in text for marker in BAD_STRUCTURE_MARKERS)


def _lookup_rows(namespace: str, value: str, *, timeout: float) -> list[dict[str, Any]]:
    """按一个命名空间取属性表。

    **结构串的 500 要翻成人话，但只翻那一种 500**（见 `BAD_STRUCTURE_MARKERS`）；
    其余瞬时故障原样抛给内核重试。
    """
    try:
        payload = http.get_json(_property_url(namespace, value), timeout=timeout)
    except http.NotFound:
        raise
    except http.RetryableToolError as error:
        if namespace in {"smiles", "inchi"} and _is_bad_structure(error):
            raise PubChemError(
                "PubChem 没能解析这段结构串（它把结构错误也回成 500）。"
                "检查括号/环闭合是否配平，或换一个 InChIKey 再试"
            ) from error
        raise
    except http.ToolError as error:
        raise PubChemError(f"PubChem 查询失败（{namespace}={value}）：{error}") from error
    return _rows(payload)


def _candidates(compound: str) -> list[tuple[str, str]]:
    """按优先级排出「拿它当什么查」的候选。

    **名称在前、SMILES 在后**：`CCO` 这种没有提示字符的串既可能是名字也可能是结构串，
    而名称查询在命中时更便宜（SMILES 查询对坏结构会回 500）。顺序反过来，
    一个叫 `CCO` 的登记名会被当成结构去解析。
    """
    text = compound.strip()
    cid = CID_PATTERN.match(text)
    if cid:
        return [("cid", cid.group(1))]
    if INCHIKEY_PATTERN.match(text):
        return [("inchikey", text.upper())]
    if SMILES_HINT.search(text) and not any(char.isspace() for char in text):
        return [("smiles", text)]
    return [("name", text), ("smiles", text)]


def lookup_compound(
    compound: str, *, synonyms: bool = False, timeout: float = http.DEFAULT_TIMEOUT
) -> list[Compound]:
    """查化合物。返回命中的全部记录（同名多义时会有多条）。"""
    text = (compound or "").strip()
    if not text:
        raise PubChemError("compound 不能为空。填名称、CID、SMILES 或 InChIKey。")

    rows: list[dict[str, Any]] = []
    attempts: list[str] = []
    for namespace, value in _candidates(text):
        try:
            rows = _lookup_rows(namespace, value, timeout=timeout)
        except http.NotFound:
            attempts.append(f"{namespace}={value}")
            continue
        if rows:
            attempts.append(f"{namespace}={value}")
            break

    if not rows:
        raise PubChemError(
            f"PubChem 里没有 {text!r}（试过 {'、'.join(attempts) or '—'}）。"
            "名称要写英文常用名（aspirin / imatinib）；结构串用 SMILES 或 InChIKey；"
            "也可以直接给 CID（2244）"
        )

    compounds = [item for item in (_compound_from(row) for row in rows) if item is not None]
    if not compounds:
        raise PubChemError(f"PubChem 回了 {text!r} 的记录，但里面没有可读的 CID")
    if synonyms:
        for item in compounds[:MAX_SYNONYM_CIDS]:
            item.synonyms = _synonyms(item.cid, timeout=timeout)
    return compounds


def _synonyms(cid: int, *, timeout: float) -> tuple[str, ...]:
    try:
        payload = http.get_json(
            f"{BASE_URL}/compound/cid/{cid}/synonyms/JSON", timeout=timeout
        )
    except http.ToolError:
        # 别名是锦上添花：拿不到就不给，不把整次查询判失败。
        return ()
    items = payload.get("InformationList") if isinstance(payload, dict) else None
    items = items.get("Information") if isinstance(items, dict) else None
    for item in items or []:
        if isinstance(item, dict) and _integer(item.get("CID")) == cid:
            names = item.get("Synonym")
            if isinstance(names, list):
                return tuple(str(name) for name in names[:MAX_SYNONYMS])
    return ()


def _chembl_code(text: str) -> str:
    """看起来是 ChEMBL 编号就归一化，否则回空串。

    借用 `chembl.normalize_molecule_id` 做判别，不另写一条正则：编号格式只该有一份实现，
    两份迟早分叉，表现成「一个工具收这个编号、另一个不收」。
    """
    try:
        return chembl.normalize_molecule_id(text)
    except http.ToolError:
        return ""


def _chembl_half(inchikey: str) -> tuple[tuple[str, dict[str, Any]] | None, str]:
    """按 InChIKey 找 ChEMBL 里的同一个结构，返回 `(画像, 说明)`。

    **查不到不算错。** 对接产物与枚举出来的类似物在 ChEMBL 里本来就可能没有记录，那是
    一条有用的结论（「没被收录」不等于「没有活性」），所以给一句说明而不是失败。

    **确定性的接口错误降级成说明，瞬时故障照旧抛。** 主答案（PubChem 那半）已经拿到了，
    不该因为附加的那半挂掉就整次失败；但 429/5xx 是抖动，交给内核重试一次才有意义——
    吞掉它会变成「这个分子不在 ChEMBL 里」，而那是误读。
    """
    key = (inchikey or "").strip()
    if not key:
        return None, ""
    try:
        code = chembl.find_by_inchikey(key)
    except http.RetryableToolError:
        raise
    except http.ToolError as error:
        return None, f"（ChEMBL 那半没取到：{error}）"
    if not code:
        return None, (
            "（ChEMBL 里没有这个结构：按 InChIKey 精确匹配查不到，说明它没被收录——"
            "不代表没有活性，也不代表没人做过。）"
        )
    try:
        return chembl.describe_molecule(code), ""
    except http.RetryableToolError:
        raise
    except http.ToolError as error:
        return None, f"（ChEMBL 里对上了 {code}，但画像没取到：{error}）"


def _similarity_cids(smiles: str, *, threshold: int, timeout: float) -> list[int]:
    """按结构相似度取 CID 列表（按相似度从高到低，接口不给分数）。"""
    url = (
        f"{BASE_URL}/compound/fastsimilarity_2d/smiles/{quote(smiles, safe='')}/cids/JSON"
    )
    try:
        payload = http.get_json(url, {"Threshold": str(threshold)}, timeout=timeout)
    except http.NotFound:
        return []
    except http.RetryableToolError as error:
        if _is_bad_structure(error):
            raise PubChemError(
                "PubChem 没能解析这段 SMILES（它把结构错误也回成 500）。"
                "检查括号/环闭合是否配平"
            ) from error
        raise
    except http.ToolError as error:
        raise PubChemError(f"PubChem 相似检索失败：{error}") from error

    identifiers = payload.get("IdentifierList") if isinstance(payload, dict) else None
    cids = identifiers.get("CID") if isinstance(identifiers, dict) else None
    values = [_integer(item) for item in cids or []]
    return [value for value in values if value is not None]


def _batch_properties(cids: Sequence[int], *, timeout: float) -> list[Compound]:
    if not cids:
        return []
    joined = ",".join(str(cid) for cid in cids)
    try:
        payload = http.get_json(
            f"{BASE_URL}/compound/cid/{joined}/property/{PROPERTIES}/JSON",
            timeout=timeout,
        )
    except http.ToolError as error:
        raise PubChemError(f"PubChem 取化合物属性失败：{error}") from error
    return [item for item in (_compound_from(row) for row in _rows(payload)) if item is not None]


def find_similar(
    smiles: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    timeout: float = http.DEFAULT_TIMEOUT,
) -> tuple[list[Compound], int]:
    """按结构相似度找已知化合物。返回 `(前 limit 条, 阈值内的总命中数)`。"""
    text = (smiles or "").strip()
    if not text:
        raise PubChemError("smiles 不能为空。先 lookup_compound 拿到 SMILES，或从登记表里取。")
    size = max(1, min(limit, MAX_LIMIT))
    cids = _similarity_cids(text, threshold=threshold, timeout=timeout)
    if not cids:
        return [], 0
    return _batch_properties(cids[:size], timeout=timeout), len(cids)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _render_one(item: Compound, *, index: int | None = None) -> list[str]:
    head = f"  [{index}] CID {item.cid}" if index is not None else f"CID {item.cid}"
    if item.title:
        head += f" · {item.title}"
    lines = [head]
    identity = []
    if item.formula:
        identity.append(f"分子式 {item.formula}")
    if item.weight is not None:
        identity.append(f"MW {item.weight:g}")
    if item.charge is not None:
        # `+0` 是 f-string 的产物，看着像数据坏了；中性就写 0。
        identity.append("电荷 " + (f"{item.charge:+d}" if item.charge else "0"))
    if identity:
        lines.append("      " + " · ".join(identity))
    if item.smiles:
        lines.append(f"      SMILES：{item.smiles}")
    if item.connectivity and item.connectivity != item.smiles:
        lines.append(f"      连接式（不含立体）：{item.connectivity}")
    tail = []
    if item.inchikey:
        tail.append(f"InChIKey {item.inchikey}")
    if item.iupac:
        tail.append(f"IUPAC {item.iupac}")
    if tail:
        lines.append("      " + " · ".join(tail))
    computed = []
    if item.xlogp is not None:
        computed.append(f"XLogP {item.xlogp:g}")
    if item.tpsa is not None:
        computed.append(f"TPSA {item.tpsa:g}")
    if item.donors is not None or item.acceptors is not None:
        computed.append(f"氢键供体 {item.donors if item.donors is not None else '?'}"
                        f" / 受体 {item.acceptors if item.acceptors is not None else '?'}")
    if item.rotatable is not None:
        computed.append(f"可旋转键 {item.rotatable}")
    if computed:
        lines.append("      计算值（PubChem 按规则算的，不是实验数据）：" + " · ".join(computed))
    if item.synonyms:
        lines.append("      别名：" + "、".join(item.synonyms))
    lines.append(f"      {item.url}")
    return lines


class LookupCompoundTool(Tool):
    """查一个化合物的身份：PubChem 那半 + ChEMBL 那半。纯查询，不产生产物。"""

    spec = ToolSpec(
        name="lookup_compound",
        description=(
            "查一个化合物的**身份**：PubChem 给结构、计算性质与 CID，ChEMBL 有记录时再补上"
            "「是不是药、类药性如何、作用于哪些靶点、治什么病」。输入可以是名称（aspirin）、"
            "CID（2244）、SMILES、InChIKey 或 ChEMBL 编号（CHEMBL25）——对接产物与枚举出来的"
            "新结构没有 ChEMBL 编号，但一定有 SMILES，这是把它们对上「已知化合物」的入口。\n\n"
            "两半靠 InChIKey 接起来，所以给编号与给结构拿到的是同一份东西。PubChem 那半给"
            "CID、分子式、分子量、电荷、规范 SMILES（含 / 不含立体各一份）、InChIKey、IUPAC 名，"
            "以及 XLogP / TPSA / 氢键供受体 / 可旋转键（**这些是算出来的，不是实验值**）。"
            "ChEMBL 那半给 max_phase（**到 4 就是已上市**，那这个「新发现」并不新）、已撤市与"
            "黑框警告、Lipinski 违反项与 QED、作用靶点、适应症。\n\n"
            "**同名多义时不补 ChEMBL**：一个名字对多个 CID 时先确认是哪个，再拿 CID 查一次。"
            "ChEMBL 里没有这个结构时会明说——那不是失败，只说明它没被收录。"
            "每次调用 2–6 秒（两个库各一两次请求，ChEMBL 那边偏慢）。"
            "要找这个结构的近亲用 find_similar_compounds。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "compound": {
                    "type": "string",
                    "description": "名称、CID（2244）、SMILES、InChIKey 或 ChEMBL 编号",
                },
                "synonyms": {
                    "type": "boolean",
                    "description": "true 时多取一次别名（常用名/商品名），最多列 8 个",
                },
            },
            "required": ["compound"],
        },
    )
    primary_argument = "compound"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        compound = str(arguments.get("compound") or "").strip()
        if not compound:
            return ToolResult(
                output="compound 不能为空。填名称、CID、SMILES、InChIKey 或 ChEMBL 编号。",
                failed=True,
            )

        code = _chembl_code(compound)
        items: list[Compound] = []
        profile: tuple[str, dict[str, Any]] | None = None
        note = ""
        # PubChem 那半没产出时的说明。**不能与「PubChem 里没有这个分子」混为一谈**：
        # 生物药没有结构式、接口临时失败，都不是「查不到」。
        missing = ""

        if code:
            # 给的是编号：ChEMBL 这半就是主答案，它出错就整次失败。
            try:
                profile = chembl.describe_molecule(code)
            except http.ToolError as error:
                return ToolResult(output=str(error), failed=True)
            key = str(profile[1].get("inchikey") or "")
            if not key:
                missing = (
                    "PubChem 那半跳过：ChEMBL 记录里没有结构式（生物药只有序列），"
                    "没有可以拿去比对的键。"
                )
            else:
                try:
                    items = lookup_compound(key)
                except PubChemError as error:
                    # `lookup_compound` 查不到会抛，不会回空表——所以「空」这一档
                    # 只有上面那种「没有键可比」的情况，不必另设一个分支。
                    missing = f"PubChem 那半没取到：{error}"
        else:
            try:
                items = lookup_compound(compound, synonyms=bool(arguments.get("synonyms")))
            except PubChemError as error:
                return ToolResult(output=str(error), failed=True)
            # 唯一命中才补 ChEMBL：同名多义时不知道该补哪一个。
            if len(items) == 1:
                profile, note = _chembl_half(items[0].inchikey)

        lines: list[str] = []
        if items:
            if len(items) == 1:
                lines.extend(_render_one(items[0]))
            else:
                lines.append(f"PubChem「{compound}」命中 {len(items)} 条，按 CID 区分：")
                for item in items:
                    lines.extend(_render_one(item))
        elif missing:
            lines.append(missing)
        if profile is not None:
            key = str(profile[1].get("inchikey") or "")
            lines.append("")
            lines.append(
                f"（下半来自 ChEMBL，按 InChIKey {key} 对上同一个结构）"
                if key
                else "（下半来自 ChEMBL）"
            )
            lines.extend(profile[0].splitlines())
        elif note:
            lines.append(note)

        if len(items) > 1:
            lines.append("")
            lines.append(
                "  同名多义：确认是哪个 CID 再继续——它们的结构可能完全不同。"
                "拿 CID 再查一次会顺带取 ChEMBL 画像。"
            )
        elif len(items) == 1:
            lines.append("")
            lines.append(
                "  要按结构找近亲：find_similar_compounds smiles=<上面的 SMILES>；"
                "要这个靶点的已知活性：search_actives。"
            )

        meta: dict[str, Any] = {
            "count": len(items),
            "cids": [item.cid for item in items],
            "compounds": [item.as_dict() for item in items],
            "chembl_id": profile[1].get("molecule_chembl_id") if profile else "",
            "chembl_name": profile[1].get("pref_name") if profile else "",
            "max_phase": profile[1].get("max_phase") if profile else None,
        }
        return ToolResult(output="\n".join(lines), meta=meta)


class FindSimilarCompoundsTool(Tool):
    """按结构相似度在 PubChem 全库找已知化合物。纯查询。"""

    spec = ToolSpec(
        name="find_similar_compounds",
        description=(
            "按结构相似度在 **PubChem 全库**里找已知化合物（2D 指纹，阈值 50–100，默认 90）。"
            "用在「这个结构是不是别人早就做过」与「找一批可买的类似物」这两步："
            "本地 screen_similarity 只能比登记表里的分子，这条能比到上亿条记录。\n\n"
            "返回命中总数与前几条的身份（CID、名称、分子式、MW、XLogP、SMILES）。"
            "**接口不给相似度分数**，所以这里也不编：结果按 PubChem 的相似度从高到低排列，"
            "但它与本地的 Tanimoto 分数不是一套指纹，不能互相比较。\n\n"
            "**与 find_analogs 的分工**：那条在 ChEMBL 里按 ChEMBL 编号找类似物（带活性）；"
            "这条只认 SMILES、覆盖全库（带结构但不保证有活性）。阈值调低命中会迅速变多"
            "（90 时阿司匹林已有近七千条），所以默认只列前 10 条。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "smiles": {
                    "type": "string",
                    "description": "查询结构的 SMILES（可从 lookup_compound 或登记表拿到）",
                },
                "threshold": {
                    "type": "integer",
                    "description": f"相似度阈值百分比，{MIN_THRESHOLD}–{MAX_THRESHOLD}，默认 {DEFAULT_THRESHOLD}",
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多列几个化合物，上限 {MAX_LIMIT}",
                },
            },
            "required": ["smiles"],
        },
    )
    primary_argument = "smiles"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        smiles = str(arguments.get("smiles") or "").strip()
        if not smiles:
            return ToolResult(
                output="smiles 不能为空。先用 lookup_compound 查，或从登记表里取结构串。",
                failed=True,
            )
        try:
            threshold = int(arguments.get("threshold") or DEFAULT_THRESHOLD)
        except (TypeError, ValueError):
            return ToolResult(output="threshold 应当是整数（50–100）", failed=True)
        if not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
            return ToolResult(
                output=f"threshold 要在 {MIN_THRESHOLD}–{MAX_THRESHOLD} 之间，收到 {threshold}",
                failed=True,
            )
        try:
            requested = int(arguments.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult(output="limit 应当是整数", failed=True)
        size = max(1, min(requested, MAX_LIMIT))

        try:
            items, total = find_similar(smiles, threshold=threshold, limit=size)
        except PubChemError as error:
            return ToolResult(output=str(error), failed=True)

        if not items:
            return ToolResult(
                output=(
                    f"PubChem 里没有与这段结构相似度 ≥{threshold}% 的化合物。"
                    "降低 threshold 再看，或确认 SMILES 写对了（括号与环闭合）"
                ),
                meta={"count": 0, "total": 0, "compounds": []},
            )

        lines = [
            f"PubChem 相似检索（阈值 ≥{threshold}%）：命中 {total} 条，列出前 {len(items)} 条。"
            "**按 PubChem 的相似度从高到低；接口不给分数**，不要拿它和本地 Tanimoto 比。"
        ]
        for index, item in enumerate(items, 1):
            lines.extend(_render_one(item, index=index))
        if total > len(items):
            lines.append(
                f"（阈值内共 {total} 条，这里只列了 {len(items)} 条；提高 threshold 收窄，"
                "或调大 limit）"
            )
        lines.append("以上是候选清单，**不含任何判断**：哪些值得跟进，由读的人定。")
        return ToolResult(
            output="\n".join(lines),
            meta={
                "count": len(items),
                "total": total,
                "threshold": threshold,
                "truncated": total > len(items),
                "cids": [item.cid for item in items],
                "compounds": [item.as_dict() for item in items],
            },
        )


def domain_tools() -> list[Tool]:
    """这组模块登记的全部工具。"""
    return [LookupCompoundTool(), FindSimilarCompoundsTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
