"""语料工具：入库、检索、按页读原文。**三个都是「本地的、免费的、离线的」那一侧。**

与 `websearch.py` 的分工一句话：那边**花钱换新内容**，这边**不花钱用手上的内容**。
所以 `search_corpus` 的描述第一段必须写「先查这里，不够再联网」——顺序反了的话，
模型每问一句都要付一次钱，而答案可能早就在盘上了。

三条纪律（`domain/__init__.py` 那八条里最相关的三条）：

1. **`import_documents` 是第三个收路径的动作。** 全库只有它和 `import_structure` /
   `import_ligandset` 收路径——用户手上确实有一个文件时，编不出 handle 来指它。
   其余一律收引用号：模型编得出 `D:/data/paper.pdf`，编不出 `doc#ab12cd34ef`。
2. **纯查询不声明 `context_argument`。** 语料跨会话存在，不属于这一轮的登记表；
   挂上登记表会让模型以为它留下了什么 handle。
3. **引用号只从工具返回里来。** `read_source` 只认 `(doc|web)#[0-9a-f]{10}`，
   不认路径——这一条把「模型编一个不存在的引用」挡在外面。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ... import corpus, corpus_config, embed, index
from ...parse import ParseError, SUPPORTED_SUFFIXES
from ..tools import Tool, ToolResult, ToolSpec

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "RRF_K",
    "ImportDocumentsTool",
    "ReadSourceTool",
    "SearchCorpusTool",
    "domain_tools",
    "fuse",
    "ingest_files",
]

DEFAULT_LIMIT = 8
MAX_LIMIT = 20

RRF_K = 60
"""RRF 的平滑常数。60 是原论文的经验值：名次差在 1～3 名之间时排序温和，
不会让某一路的第 1 名无条件压过另一路的前 10 个。"""

SAME_GROUP_CHARS = 600
"""同一节最多拼多少字给模型看。8 组 × 600 ≈ 4800 字，在压缩线以内。"""

GROUP_CHUNK_CHARS = 400
"""组内单块的上限。超过就截断——组的意义是「同节上下文」，不是把整节搬过来。"""

MAX_IMPORTS = 50
"""一次最多导入多少个文件。再多就该分批——一次调用把上下文和界面都撑爆。"""

MAX_PAGE_CHARS = 8000
DEFAULT_PAGE_CHARS = 3000

REF_PATTERN = re.compile(r"^(doc|web)#([0-9a-f]{10})$")
"""引用号的唯一合法形状。**没有路径这一档。**"""


# ---------------------------------------------------------------------------
# 一、入库
# ---------------------------------------------------------------------------
class ImportDocumentsTool(Tool):
    """把本机文献放进语料库。**会写盘、会建索引**，不是纯查询。"""

    spec = ToolSpec(
        name="import_documents",
        description=(
            "把**本机的文献**（.pdf / .md / .txt）放进项目语料库，之后用 `search_corpus` "
            "检索、用 `read_source` 按页读。**不收 PDF 以外的格式猜测**：其它扩展名直接拒绝。\n\n"
            "`path` 可以是一个文件，也可以是一个目录（只收该目录**直接**包含的文献，不递归）。"
            "逐条报「新增 / 重复 / 失败」：同一份文件（按内容哈希）导两次是**重复**，"
            "不会产生第二份。\n\n"
            "这是全套工具里**第三个收路径的动作**（另两个是 import_structure / "
            "import_ligandset）——其余一律收引用号。扫描件（没有文本层）会照实失败，"
            "需要 OCR，本轮不支持。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "本机文件或目录的路径，建议给绝对路径",
                }
            },
            "required": ["path"],
        },
    )
    primary_argument = "path"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return ToolResult(output="path 不能为空", failed=True)
        target = Path(raw).expanduser()
        if not target.exists():
            return ToolResult(output=f"找不到 {target}", failed=True)

        if target.is_file():
            files = [target]
            ignored = 0
        elif target.is_dir():
            files = sorted(
                item
                for item in target.iterdir()
                if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
            )
            ignored = sum(
                1
                for item in target.iterdir()
                if item.is_file() and item.suffix.lower() not in SUPPORTED_SUFFIXES
            )
            if not files:
                return ToolResult(
                    output=(
                        f"目录 {target} 里没有可导入的文件"
                        f"（只收 {' / '.join(SUPPORTED_SUFFIXES)}）。"
                    ),
                    failed=True,
                )
        else:
            return ToolResult(output=f"{target} 既不是文件也不是目录", failed=True)

        capped = files[:MAX_IMPORTS]
        outcomes = ingest_files(capped)
        return ToolResult(
            output=_render_import(outcomes, skipped=len(files) - len(capped), ignored=ignored),
            failed=not any(item["status"] != "failed" for item in outcomes),
            meta={
                "total": len(files),
                "added": sum(1 for item in outcomes if item["status"] == "new"),
                "duplicate": sum(1 for item in outcomes if item["status"] == "duplicate"),
                "failed": sum(1 for item in outcomes if item["status"] == "failed"),
                "sources": [item["ref"] for item in outcomes if item.get("ref")],
            },
        )


def _import_one(path: Path, root: Path | None = None) -> dict[str, Any]:
    """一个文件走完整条管线。**失败只影响这一个文件。**

    `root` 是语料根。应用里传 `None`（按代码位置定位到仓根）；网页服务有自己
    的 `corpus_root`（判据会在临时目录上跑），这时必须传进来，否则入库会写进
    仓根那份语料——投递到 A、入库进 B，盘上看起来像丢了一份。
    """
    try:
        digest = _sha256(path)
    except OSError as error:
        return {"status": "failed", "name": path.name, "reason": f"读不出这个文件：{error}"}

    source_id = digest[:10]
    directory = (Path(root) / "doc" / source_id) if root is not None else corpus.doc_dir(source_id)
    if (directory / corpus.META_FILE).is_file():
        # 已经在库里了，inbox 里那份同样是多余的：不清掉的话，下一次会再看见它一遍。
        _drop_inbox(path, root)
        return {
            "status": "duplicate",
            "name": path.name,
            "ref": f"doc#{source_id}",
            "reason": "内容相同的一份已经在语料库里",
        }

    try:
        document, chunks = index.chunks_for(path)
    except ParseError as error:
        return {"status": "failed", "name": path.name, "reason": str(error)}
    except Exception as error:  # noqa: BLE001 - 任何解析器异常都只该毁掉这一个文件
        return {
            "status": "failed",
            "name": path.name,
            "reason": f"解析时出了预料之外的错（{type(error).__name__}: {error}）",
        }

    title = _title_of(document, path)
    original = f"{corpus.FILE_PREFIX}{path.suffix.lower()}"
    now = time.time()
    meta: dict[str, Any] = {
        "id": source_id,
        "kind": "doc",
        "title": title,
        "file": original,
        "locator": str(path),
        "sha256": digest,
        "pages": len(document.pages),
        "structure": document.structure,
        "chars": document.chars,
        "chunks": len(chunks),
        "added_at": now,
        "origin": str(path),
        "warnings": list(document.warnings),
    }

    try:
        corpus.copy_file_atomic(path, directory / original)
        corpus.atomic_write_text(
            directory / corpus.TEXT_FILE,
            "\n".join(json.dumps(record, ensure_ascii=False) for record in document.text_records())
            + "\n",
        )
        # meta.json 最后写：它是「这份源已经完整」的提交标记。
        corpus.write_json(directory / corpus.META_FILE, meta)
    except OSError as error:
        return {"status": "failed", "name": path.name, "reason": f"写语料目录失败：{error}"}

    # 写索引这一段排队：网页端的「入库」与 TUI 里的导入是同一个进程的两个线程，
    # 谁也不该在 SQLite 的写锁上白等十秒。
    with index.writing():
        conn = index.connect(root)
        try:
            index.upsert_source(
                conn, index.source_row(f"doc:{source_id}", "doc", directory, meta), chunks
            )
            # 向量与词法索引分开算：没装 fastembed 时这一段返回 enabled=False，
            # 导入本身照样成功——语义通道是可选增强，不是入库的前置条件。
            vectors = _embed_source(conn, f"doc:{source_id}")
        finally:
            conn.close()

    # 从 inbox 投进来的那一份用完就删：留着的话下一次导入会再看见它一遍。
    _drop_inbox(path, root)

    return {
        "status": "new",
        "name": path.name,
        "ref": f"doc#{source_id}",
        "title": title,
        "pages": len(document.pages),
        "structure": document.structure,
        "chunks": len(chunks),
        "vectors": vectors,
        "warnings": list(document.warnings),
    }


def ingest_files(paths: list[Path], root: Path | None = None) -> list[dict[str, Any]]:
    """把一批文件逐个走完整条入库管线，返回每个文件的结果。

    **网页端的「入库」与 TUI 里的 `import_documents` 共用这一个入口**：网页那边
    没有模型、没有会话，也不该自己再拼一遍「复制原件 → 切块 → 建索引 → 算向量」。
    两份实现迟早有一份忘了写 `meta.json` 那个提交标记。

    只做逐个调度：失败按文件隔离（`_import_one` 内部已经兜住），一个坏文件
    不该让同批的另外九个白导。条数上限由调用方先切（`MAX_IMPORTS`）。
    """
    return [_import_one(path, root) for path in paths]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            piece = handle.read(1024 * 1024)
            if not piece:
                break
            digest.update(piece)
    return digest.hexdigest()


def _drop_inbox(path: Path, root: Path | None = None) -> None:
    """投递口里的那一份用完就删。**只删 inbox 里的**：别处的文件是用户的。"""
    if _inside_inbox(path, root):
        path.unlink(missing_ok=True)


def _inside_inbox(path: Path, root: Path | None = None) -> bool:
    base = Path(root) if root is not None else corpus.corpus_root()
    try:
        return (base / "inbox").resolve() in path.resolve().parents
    except OSError:
        return False


def _embed_source(conn: Any, source_id: str) -> dict[str, Any]:
    """给一个源补向量。**失败不抛**：词法索引已经写好了，检索不至于什么都查不到。

    这里现读 `corpus.toml` 而不是在工具构造时读一次：`/library` 上改完开关，
    下一次导入就该按新值走。
    """
    config = corpus_config.load()
    if not config.embed:
        return {"enabled": False, "embedded": 0, "reason": "语义通道已在 corpus.toml 里关掉"}
    try:
        return embed.index_source(conn, source_id, config.embed_model)
    except Exception as error:  # noqa: BLE001 - 向量是增强，任何异常都不该毁掉入库
        return {
            "enabled": False,
            "embedded": 0,
            "reason": f"向量入库失败（{type(error).__name__}: {error}）",
        }


def _title_of(document: Any, path: Path) -> str:
    """标题取文件自己写的（PDF 的 `/Title`），没有就用文件名。

    **不拿第一个章节名当标题**：那样每篇论文都会叫 `Introduction`。
    名字不好看由用户改（S2 的 `labels.json`），不靠猜。
    """
    return str(getattr(document, "title", "") or "").strip() or path.stem


def _render_import(
    outcomes: list[dict[str, Any]], *, skipped: int, ignored: int = 0
) -> str:
    added = [item for item in outcomes if item["status"] == "new"]
    duplicates = [item for item in outcomes if item["status"] == "duplicate"]
    failures = [item for item in outcomes if item["status"] == "failed"]

    lines = [f"导入完成：新增 {len(added)}、重复 {len(duplicates)}、失败 {len(failures)}。"]
    for item in added:
        detail = f"{item['pages']} 页 / {item['structure']} / {item['chunks']} 块"
        vectors = item.get("vectors") or {}
        if vectors.get("embedded"):
            detail += f" / 向量 {vectors['embedded']} 块"
        lines.append(f"  {item['ref']}  {item['title']}（{detail}）")
        for warning in item["warnings"]:
            lines.append(f"      注意：{warning}")
    for item in duplicates:
        lines.append(f"  已在库里：{item['ref']}（{item['name']}，{item['reason']}）")
    for item in failures:
        lines.append(f"  失败：{item['name']} —— {item['reason']}")
    if ignored:
        lines.append(
            f"（目录里另有 {ignored} 个不是 {' / '.join(SUPPORTED_SUFFIXES)} 的文件，已跳过。）"
        )
    if skipped:
        lines.append(f"（还有 {skipped} 个文件这次没处理，分批再导。）")
    if added:
        reason = _disabled_reason(added)
        if reason:
            lines.append(f"（{reason}检索只跑词法两路。）")
        lines.append("之后用 search_corpus 检索，用 read_source 按页读原文。")
    return "\n".join(lines)


def _disabled_reason(items: list[dict[str, Any]]) -> str:
    """语义通道没跑起来时的**一行**说明。三行安装指引留给命令行，不占模型上下文。"""
    for item in items:
        vectors = item.get("vectors") or {}
        if vectors.get("enabled") or vectors.get("embedded"):
            return ""
        reason = str(vectors.get("reason") or "")
        if reason:
            return reason.splitlines()[0].rstrip("。") + "；"
    return ""


# ---------------------------------------------------------------------------
# 二、检索
# ---------------------------------------------------------------------------
class SearchCorpusTool(Tool):
    """本地语料检索。**免费、离线**，所以它是「先查这里」的那一个。"""

    spec = ToolSpec(
        name="search_corpus",
        description=(
            "在**本地语料库**里检索（用户导入的文献 + 联网搜过并抓下来的网页）。"
            "**免费、离线、不联网**——所以先查这里，不够再 `search_web`（计费）。\n\n"
            "词法两路（英文词/标识符 + 中文子串）与语义向量（本机装了模型才跑）"
            "用 RRF 融合，命中块前后各带一块，再按「同一篇的同一节」聚成一组。"
            "返回每组一个引用号（`doc#… §2.1 p.7` / `web#…`）与 600 字以内的片段。\n\n"
            "**中文提问也能命中英文文献**：语义那一路跨语言，不必先把术语翻成英文。"
            "**引用号只能来自这里的返回**，不要自己拼。要读某一段的上下文用 `read_source`。"
            "本地没有匹配不等于没有这篇文献；也不等于网上没有——那是 search_web 的事。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词。中英文都行；查具体术语、方法名、编号比查整句更准",
                },
                "limit": {
                    "type": "integer",
                    "description": f"返回几组（按「源 + 章节」聚合），默认 {DEFAULT_LIMIT}，最多 {MAX_LIMIT}",
                },
            },
            "required": ["query"],
        },
    )
    primary_argument = "query"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(output="query 不能为空", failed=True)
        limit = _clamp(arguments.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT)

        conn = index.connect()
        try:
            config = corpus_config.load()
            channels = index.lexical(conn, query)
            coverage = 0.0
            # 模型没装/没下载时**静默降级**：技术细节进 meta，不进正文——
            # 模型看到的应该是「本地没有」，而不是一段 pip install 指引。
            # 覆盖率也一并报 0：这一路没跑，报个 100% 只会让人以为语义召回声效正常。
            if config.embed and not embed.missing_reason(config.embed_model):
                coverage = embed.coverage(conn, config.embed_model)
                semantic = embed.search(
                    conn, query, config.embed_model, limit=index.CHANNEL_LIMIT
                )
                if semantic:
                    channels["vec"] = [chunk_id for chunk_id, _ in semantic]
            fused = fuse(channels)
            if not fused:
                return ToolResult(
                    output=(
                        f"本地语料里没有匹配「{query}」的内容。\n"
                        "换几个更具体的词（术语、方法名、编号）再试；本地确实没有的话，"
                        "可以用 search_web 联网查（计费），或 import_documents 把文献导进来。"
                    ),
                    meta={
                        "count": 0,
                        "hits": 0,
                        "reason": "empty",
                        "channels": list(channels),
                        "vector_coverage": coverage,
                    },
                )

            order = sorted(fused, key=lambda cid: (-fused[cid], cid))
            candidates = [(cid, fused[cid]) for cid in order[: index.CHANNEL_LIMIT]]
            rows = index.rows_by_id(conn, [cid for cid, _ in candidates])
            # 源被删掉的那些直接从候选里去掉：删除是「换名进 .trash/」，索引不知道，
            # 这里按目录存在性对一次账。
            alive = [
                (cid, score)
                for cid, score in candidates
                if cid in rows and _alive(rows[cid])
            ]
            groups = _group(conn, alive, rows)
        finally:
            conn.close()

        if not groups:
            return ToolResult(
                output=(
                    f"本地语料里没有匹配「{query}」的内容"
                    "（索引里有命中，但那几个源已经被删了）。"
                ),
                meta={
                    "count": 0,
                    "hits": 0,
                    "reason": "stale",
                    "channels": list(channels),
                    "vector_coverage": coverage,
                },
            )

        chosen = groups[:limit]
        return ToolResult(
            output=_render_search(query, chosen, total_groups=len(groups)),
            meta={
                "count": len(chosen),
                "groups": len(groups),
                "hits": len(fused),
                "channels": {name: len(ids) for name, ids in channels.items()},
                "vector_coverage": coverage,
                "refs": [group["ref"] for group in chosen],
            },
        )


def fuse(channels: dict[str, list[int]]) -> dict[int, float]:
    """RRF：`Σ 1/(k + rank)`。**不把 BM25 分数与别的分数相加**——量纲不同。"""
    scores: dict[int, float] = {}
    for ids in channels.values():
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def _alive(row: Any) -> bool:
    """这个块的源还在不在盘上。

    删除在语料侧是「换名进 `.trash/`」，索引侧不通知任何人。这里查一下目录：
    目录没了就跳过——对用户来说删除就是「查不到」，不必等下一次重建。
    """
    try:
        return corpus.source_dir(str(row["kind"]), str(row["source_id"]).split(":", 1)[1]).is_dir()
    except (ValueError, IndexError):
        return False


def _group(
    conn: Any, scored: list[tuple[int, float]], rows: dict[int, Any]
) -> list[dict[str, Any]]:
    """按「源 + 章节」聚组，并把命中块的前后邻块并进来。

    不做这一步的话，top 8 会变成「同一页的五个碎片 + 三篇无关论文」——
    RRF 只看块，而人看的是「这一段出自哪里」。
    """
    scores = dict(scored)
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    for chunk_id, _score in scored:
        row = rows.get(chunk_id)
        if row is None:
            continue
        key = (str(row["source_id"]), str(row["section"]))
        entry = groups.setdefault(
            key,
            {"best": 0.0, "rows": {}, "kind": str(row["kind"]), "title": str(row["title"])},
        )
        entry["best"] = max(entry["best"], scores.get(chunk_id, 0.0))
        entry["rows"][int(row["chunk_no"])] = row
        for neighbor in index.neighbors(conn, row):
            entry["rows"].setdefault(int(neighbor["chunk_no"]), neighbor)

    out: list[dict[str, Any]] = []
    for (source_id, section), entry in groups.items():
        ordered = [entry["rows"][number] for number in sorted(entry["rows"])]
        kind, sid = source_id.split(":", 1)
        out.append(
            {
                "score": entry["best"],
                "ref": _citation(kind, sid, section, ordered),
                "source_id": source_id,
                "kind": kind,
                "title": entry["title"],
                "section": section,
                "text": _joined(ordered),
            }
        )
    out.sort(key=lambda group: (-group["score"], group["source_id"], group["section"]))
    return out


def _joined(rows: list[Any]) -> str:
    """组内各块拼起来，**按 chunk_no 去重并按顺序**，总长受预算限制。"""
    pieces: list[str] = []
    seen: set[str] = set()
    total = 0
    for row in rows:
        text = str(row["text"]).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        snippet = text[:GROUP_CHUNK_CHARS]
        if total + len(snippet) > SAME_GROUP_CHARS:
            break
        pieces.append(snippet)
        total += len(snippet)
    return "\n…\n".join(pieces)


def _citation(kind: str, source_id: str, section: str, rows: list[Any]) -> str:
    """`doc#ab12cd34ef §2.1 In vitro assay p.2`。

    节用路径的**最后一段**：`§2 Methods / 2.1 In vitro assay` 读起来太长，
    而引用只需要能对回原文的那一节。
    """
    ref = f"{kind}#{source_id}"
    leaf = section.split(" / ")[-1].strip()
    if leaf:
        ref += f" §{leaf}"
    pages = [int(row["page"]) for row in rows if int(row["page"])]
    if pages and kind == "doc":
        low, high = min(pages), max(pages)
        ref += f" p.{low}" if low == high else f" p.{low}-{high}"
    return ref


def _render_search(query: str, groups: list[dict[str, Any]], *, total_groups: int) -> str:
    lines = [f"本地语料检索「{query}」：{total_groups} 组命中，列出 {len(groups)} 组。"]
    for group in groups:
        lines.append("")
        lines.append(f"  {group['ref']}  {group['title']}")
        for piece in group["text"].split("\n…\n"):
            # 块内还有换行，逐行缩进；只缩第一行的话第二行会顶到最左边，读起来像两段。
            lines.extend(f"    {line}" for line in piece.splitlines())
    if total_groups > len(groups):
        lines.append("")
        lines.append(f"（还有 {total_groups - len(groups)} 组没显示，调大 limit 或把检索词写细。）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 三、按页读原文
# ---------------------------------------------------------------------------
class ReadSourceTool(Tool):
    """按引用号读原文。**只认引用号，不认路径。**"""

    spec = ToolSpec(
        name="read_source",
        description=(
            "按**引用号**读本地那份原文：`doc#ab12cd34ef`（可按页）、`web#ab12cd34ef`。"
            "免费、离线，不联网。\n\n"
            "`page` 只对文献有意义（网页没有页码）；不给页就给开头一段，并告诉你有多少页。"
            "文献的参考文献、致谢等章节**不进检索**，但这里读得到。\n\n"
            "网页若只存了摘要（`depth: summary`），这里会直说「只有摘要」，"
            "要全文用 `fetch_webpage` 抓（计费）。**只接受工具返回过的引用号**，"
            "不接受文件路径——路径可能是你编的，引用号编不出来。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "引用号，形如 doc#ab12cd34ef 或 web#ab12cd34ef",
                },
                "page": {
                    "type": "integer",
                    "description": "第几页（从 1 起）。只对文献有效；不给自己取开头",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        f"这次最多返回多少字，默认 {DEFAULT_PAGE_CHARS}，"
                        f"最多 {MAX_PAGE_CHARS}"
                    ),
                },
            },
            "required": ["ref"],
        },
    )
    primary_argument = "ref"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        raw = str(arguments.get("ref") or "").strip()
        matched = REF_PATTERN.match(raw)
        if matched is None:
            return ToolResult(
                output=(
                    f"引用号「{raw}」不合法。格式只有两种：`doc#xxxxxxxxxx` 或 "
                    "`web#xxxxxxxxxx`（10 位十六进制，来自 search_corpus / search_web / "
                    "fetch_webpage 的返回）。**这里不收文件路径**。"
                ),
                failed=True,
            )
        kind, source_id = matched.group(1), matched.group(2)
        limit = _clamp(arguments.get("max_chars"), DEFAULT_PAGE_CHARS, 500, MAX_PAGE_CHARS)

        if kind == "web":
            return _read_web(source_id, limit)
        return _read_doc(source_id, arguments.get("page"), limit)


def _read_web(source_id: str, limit: int) -> ToolResult:
    page = corpus.read_web_page(source_id)
    if page is None:
        return ToolResult(output=f"web#{source_id} 本地没有这一份（可能已删除）。", failed=True)
    depth = str(page.meta.get("depth") or "")
    lines = [f"{page.ref}  {page.meta.get('title', '')}", f"  {page.meta.get('url', '')}"]
    if depth != "full":
        lines.append(
            "（本地只有摘要，不是全文。要正文用 fetch_webpage 抓这个 URL，会计费。）"
        )
    lines.append("")
    lines.append(page.content[:limit])
    if len(page.content) > limit:
        lines.append("")
        lines.append(f"（只给了前 {limit} 字，本地共 {len(page.content)} 字；调大 max_chars 看后面）")
    return ToolResult(
        output="\n".join(lines),
        meta={"ref": page.ref, "depth": depth, "chars": len(page.content)},
    )


def _read_doc(source_id: str, page: Any, limit: int) -> ToolResult:
    directory = corpus.doc_dir(source_id)
    if not (directory / corpus.META_FILE).is_file():
        return ToolResult(output=f"doc#{source_id} 本地没有这一份（可能已删除）。", failed=True)
    try:
        meta = corpus.read_json(directory / corpus.META_FILE)
        records = [
            json.loads(line)
            for line in (directory / corpus.TEXT_FILE).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as error:
        return ToolResult(output=f"doc#{source_id} 的原文读不出来：{error}", failed=True)

    total = len(records)
    wanted = _clamp(page, 0, 0, 10**6)
    if wanted:
        hit = next((item for item in records if int(item.get("page", 0)) == wanted), None)
        if hit is None:
            return ToolResult(
                output=f"doc#{source_id} 只有 {total} 页，没有第 {wanted} 页。",
                failed=True,
            )
        text = str(hit.get("text") or "")
        head = f"{meta.get('title', '')} — 第 {wanted}/{total} 页"
    else:
        text = "\n\n".join(str(item.get("text") or "") for item in records)
        head = f"{meta.get('title', '')} — 共 {total} 页（这次给开头）"

    lines = [f"doc#{source_id}  {head}", ""]
    lines.append(text[:limit] or "（这一页没有文字）")
    if len(text) > limit:
        lines.append("")
        lines.append(f"（只给了前 {limit} 字；用 page 指定页，或调大 max_chars）")
    return ToolResult(
        output="\n".join(lines),
        meta={
            "ref": f"doc#{source_id}",
            "pages": total,
            "page": wanted or 1,
            "chars": len(text),
        },
    )


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    """夹取整数参数。**写错类型用默认值**，不为此失败一次调用。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def domain_tools() -> list[Tool]:
    """这一组按「入库 → 检索 → 读取」的顺序登记。"""
    return [ImportDocumentsTool(), SearchCorpusTool(), ReadSourceTool()]


if __name__ == "__main__":
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
