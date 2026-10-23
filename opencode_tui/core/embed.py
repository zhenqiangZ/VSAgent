"""本地向量通道：fastembed + ONNX，**不依赖 torch**。

主仓一行都不能 import torch（`check_chemistry_gate` 会炸、exe 会大 2GB），而
`sentence-transformers` 依赖 torch，所以走 fastembed：它只带 onnxruntime + tokenizers。

**模型选的是 `paraphrase-multilingual-MiniLM-L12-v2`（384 维，255MB）**，
不是方案稿里写的 `multilingual-e5-small`：fastembed 0.9 的清单里没有那个模型，
能替代的 `multilingual-e5-large` 是 1024 维、2.24GB，那个体积对本地个人语料不值当。
选它有四个好处——中英都在训练数据里、384 维与方案稿的预算一致、**不需要前缀**
（省掉「漏了 `query:` 静默掉点」那一类故障）、CPU 上单条几十毫秒。
要换成纯中文更准的 `BAAI/bge-small-zh-v1.5`（512 维）就设 `VSA_EMBED_MODEL`。

三条纪律：

1. **可选依赖在函数体里导入**。没装 fastembed 时应用照常起，只是语义通道降级；
   `missing_reason()` 给一句能照着做的话，**不自动下载**（255MB 不该由一次搜索触发）。
2. **向量已经 L2 归一化再存**。余弦就退化成点积，检索那一侧不必再乘一次范数。
3. **模型名随行存**。换模型必须整表重建——两个模型的向量混在一起，距离没有意义。

存储：`vectors(chunk_id, model, dim, vec)`，`vec` 是 `<f4` 小端。**这张表是派生物**，
删掉用 `python -m opencode_tui.core.embed index` 重建。

模型名与仓库名**不是一回事**：ONNX 版在 `Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`。
缓存目录按仓库名拼（`_repo_id`），按模型名拼会永远找不到已下载的文件。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import index

__all__ = [
    "BATCH",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "MIRROR_ENDPOINT",
    "MIN_SCORE",
    "MIN_SCORE_ENV",
    "available",
    "cache_bytes",
    "coverage",
    "download",
    "download_hint",
    "encode_passages",
    "encode_query",
    "index_missing",
    "index_source",
    "min_score",
    "missing_reason",
    "models_dir",
    "purge_other_models",
    "search",
]

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

DEFAULT_ENDPOINT = "https://huggingface.co"
MIRROR_ENDPOINT = "https://hf-mirror.com"
"""直连不通时退到的镜像。**只在国内网络下才用得上**，但它不会覆盖显式设置的
`HF_ENDPOINT`——那个永远排第一。"""

BATCH = 16
"""一次编码多少块。16 在 CPU 上吞吐与内存都稳；再大只是多占内存。"""

MAX_QUERY_CHARS = 512
"""查询截断。嵌入模型的上下文窗口有限，超长查询会被静默截断，不如自己截。"""

MIN_SCORE = 0.30
"""余弦下限。**低于它就不算命中**。

余弦没有「不相关」这个值：随便一句话都能在 53 块里找出 30 个 0.2 分的邻居。
不设下限的话，「本地没有」会变成「命中 29 组」，而且每组都答非所问——比查不到
更糟。本机实测（默认模型）：相关查询 0.44～0.57，无关查询 0.02～0.23，0.30 落在
空档里。换模型要重测，可用 `VSA_EMBED_MIN_SCORE` 覆盖。
"""

MIN_SCORE_ENV = "VSA_EMBED_MIN_SCORE"

_CACHE: dict[str, object] = {}
"""进程内的编码器缓存。加载一次模型要一两秒，一次检索不该付两遍。"""

_MATRIX_CACHE: dict[tuple[str, str, int], tuple[object, list[int]]] = {}
"""`(库路径, 模型, 行数) -> (归一化矩阵, chunk_id 列表)`。

行数参与键：入库之后行数变了，缓存自然失效——比维护一个版本号省事，也不会漏。
"""


def models_dir() -> Path:
    """模型缓存目录：`~/.opencode_tui/models/`。不进 exe、不进仓。"""
    return Path.home() / ".opencode_tui" / "models"


def _repo_id(model: str) -> str:
    """模型在 HuggingFace 上的**真实仓库名**。

    两者常常不一样：`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
    的 ONNX 版在 `Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`。按模型名
    拼缓存目录会永远找不到文件，`available()` 就会一直报「没下载」——而模型其实
    就在盘上。仓库名只从 fastembed 的清单里取，不在这里抄一份。
    """
    try:
        from fastembed import TextEmbedding

        for entry in TextEmbedding.list_supported_models():
            if entry.get("model") == model:
                return str((entry.get("sources") or {}).get("hf") or model)
    except Exception:  # noqa: BLE001 - 没装 fastembed 时调用方已经先返回了
        pass
    return model


def _model_path(model: str) -> Path:
    """fastembed / huggingface_hub 的缓存目录名：`models--org--name`。"""
    return models_dir() / ("models--" + _repo_id(model).replace("/", "--"))


def available(model: str) -> bool:
    """能不能**离线**用这个模型：fastembed 装上了，且模型文件已经在本机。"""
    if not _installed():
        return False
    directory = _model_path(model)
    if not directory.is_dir():
        return False
    return any(directory.rglob("*.onnx"))


def missing_reason(model: str) -> str:
    """不能用的原因 + 一句能照着做的话。**模型没下载时不自动下**。"""
    if not _installed():
        return (
            "语义通道未启用：没装 fastembed。\n"
            "  pip install -r requirements-embed.txt\n"
            "（它带 onnxruntime，约 60MB；不装的话检索只跑两路词法，其它功能不受影响。）"
        )
    if not available(model):
        return (
            f"语义通道未启用：模型 {model} 还没下载（约 220MB）。\n"
            "  python -m opencode_tui.core.embed download\n"
            "（不会自动下载：不该让一次检索顺手拉一个 220MB 的文件。）"
        )
    return ""


def _installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("fastembed") is not None


def _encoder(model: str):
    """拿到（并缓存）一个编码器。**只在确定能离线用之后才调它。**"""
    cached = _CACHE.get(model)
    if cached is not None:
        return cached
    from fastembed import TextEmbedding

    # `local_files_only=True`：这里只负责**加载**。缺文件时宁可报错，也不能在
    # 一次检索里顺手拉 220MB——那会让一次查询卡住几分钟，用户还以为程序死了。
    encoder = TextEmbedding(
        model_name=model, cache_dir=str(models_dir()), local_files_only=True
    )
    _CACHE[model] = encoder
    return encoder


def _prefixes(model: str) -> tuple[str, str]:
    """`(查询前缀, 文档前缀)`。

    E5 系列必须带 `query:` / `passage:`，漏了不报错、只掉点；其余模型（含默认这个）
    不需要前缀，带上反而会让输入分布偏离训练时的样子。**前缀只写在这一处。**
    """
    if "e5" in model.lower():
        return "query: ", "passage: "
    return "", ""


def encode_query(text: str, model: str) -> list[float]:
    """把查询编成一个归一化向量。"""
    prefix, _ = _prefixes(model)
    return _encode([prefix + text[:MAX_QUERY_CHARS]], model)[0]


def encode_passages(texts: list[str], model: str) -> list[list[float]]:
    """把一批文档编成归一化向量。"""
    _, prefix = _prefixes(model)
    return _encode([prefix + text for text in texts], model)


def _encode(texts: list[str], model: str) -> list[list[float]]:
    import numpy as np

    encoder = _encoder(model)
    out: list[list[float]] = []
    for vector in encoder.embed(texts, batch_size=BATCH):
        array = np.asarray(vector, dtype="<f4")
        norm = float(np.linalg.norm(array))
        if norm > 0:
            array = array / norm
        out.append(array.tolist())
    return out


def _pack(vector: list[float]) -> bytes:
    import numpy as np

    return np.asarray(vector, dtype="<f4").tobytes()


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------
def index_source(conn: sqlite3.Connection, source_id: str, model: str) -> dict:
    """给一个源里**还没有向量**的块补上。返回读数，**不抛异常**。

    编码失败只跳过那一块并记数：一块文本让整次入库失败，用户会以为文件坏了，
    而实际可能只是一个奇怪的字符。没装模型时返回 `enabled: False`。
    """
    if not available(model):
        return {"enabled": False, "embedded": 0, "failed": 0, "reason": missing_reason(model)}

    rows = conn.execute(
        "SELECT c.id, c.section, c.text FROM chunks c "
        "LEFT JOIN vectors v ON v.chunk_id = c.id AND v.model = ? "
        "WHERE c.source_id = ? AND v.chunk_id IS NULL ORDER BY c.chunk_no",
        (model, source_id),
    ).fetchall()
    if not rows:
        return {"enabled": True, "embedded": 0, "failed": 0, "seconds": 0.0}

    started = time.monotonic()
    embedded = failed = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        texts = [_sectioned(row["section"], row["text"]) for row in batch]
        try:
            vectors = encode_passages(texts, model)
        except Exception:  # noqa: BLE001 - 整批编码失败时逐条重试，尽量多补几块
            vectors = []
            for text in texts:
                try:
                    vectors.append(encode_passages([text], model)[0])
                except Exception:  # noqa: BLE001
                    vectors.append(None)
        for row, vector in zip(batch, vectors):
            if vector is None:
                failed += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO vectors (chunk_id, model, dim, vec) VALUES (?, ?, ?, ?)",
                (int(row["id"]), model, len(vector), _pack(vector)),
            )
            embedded += 1
        conn.commit()
    return {
        "enabled": True,
        "embedded": embedded,
        "failed": failed,
        "seconds": round(time.monotonic() - started, 2),
    }


def index_missing(conn: sqlite3.Connection, model: str) -> dict:
    """给库里所有缺向量的源补上。换模型之后跑这一条就是整表重建。"""
    if not available(model):
        return {"enabled": False, "sources": 0, "embedded": 0, "failed": 0}
    sources = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT c.source_id FROM chunks c "
            "LEFT JOIN vectors v ON v.chunk_id = c.id AND v.model = ? "
            "WHERE v.chunk_id IS NULL",
            (model,),
        )
    ]
    totals = {"enabled": True, "sources": len(sources), "embedded": 0, "failed": 0, "seconds": 0.0}
    for source_id in sources:
        result = index_source(conn, str(source_id), model)
        totals["embedded"] += result.get("embedded", 0)
        totals["failed"] += result.get("failed", 0)
        totals["seconds"] = round(totals["seconds"] + result.get("seconds", 0.0), 2)
    return totals


def _sectioned(section: str, text: str) -> str:
    """编码输入带上章节路径。

    同一段文字在 "Methods" 与 "Discussion" 里，意思本来就不一样；把章节名拼进去，
    向量才分得开。**存进库的 `text` 不带这一段**——正文要保持可引用、可原样展示。
    """
    leaf = (section or "").split(" / ")[-1].strip()
    return f"{leaf}\n{text}" if leaf else text


def purge_other_models(conn: sqlite3.Connection, model: str) -> int:
    """删掉别的模型留下的向量。

    换模型之后不清理的话，`vectors` 里两套向量混着，谁都不会去用旧的，但它们
    永远占着空间，而且「覆盖了多少」这个读数会一直是错的。
    """
    with conn:
        cursor = conn.execute("DELETE FROM vectors WHERE model <> ?", (model,))
    return int(cursor.rowcount or 0)


def coverage(conn: sqlite3.Connection, model: str) -> float:
    """向量覆盖率：有多少块算过向量。0～1；一块都没有时是 0。"""
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not total:
        return 0.0
    done = conn.execute("SELECT COUNT(*) FROM vectors WHERE model = ?", (model,)).fetchone()[0]
    return round(int(done) / int(total), 3)


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def search(
    conn: sqlite3.Connection, query: str, model: str, *, limit: int = 30
) -> list[tuple[int, float]]:
    """全表暴力扫一遍余弦，返回 `[(chunk_id, 分数)]`，按分数降序。

    5 万块 × 384 维 = 77MB、两千万次乘加，CPU 几十毫秒。个人语料用不上向量库，
    多引一个 Chroma/FAISS 只是多一份要跟着升级的东西。
    """
    if not available(model) or not query.strip():
        return []
    matrix, ids = _matrix(conn, model)
    if not ids:
        return []
    import numpy as np

    vector = np.asarray(encode_query(query, model), dtype="<f4")
    scores = matrix @ vector
    floor = min_score()
    order = np.argsort(-scores)[:limit]
    return [
        (ids[int(i)], float(scores[int(i)]))
        for i in order
        if float(scores[int(i)]) >= floor
    ]


def min_score() -> float:
    """余弦下限。环境变量写坏了就用默认值——一个数字不该让检索罢工。"""
    raw = (os.environ.get(MIN_SCORE_ENV) or "").strip()
    if not raw:
        return MIN_SCORE
    try:
        return float(raw)
    except ValueError:
        return MIN_SCORE


def _matrix(conn: sqlite3.Connection, model: str):
    """取出该模型的全部向量，拼成矩阵。按 `(库, 模型, 版本)` 缓存。"""
    import numpy as np

    path = _database_path(conn)
    key = (path, model, _version(conn))
    cached = _MATRIX_CACHE.get(key)
    if cached is not None:
        return cached
    rows = conn.execute(
        "SELECT chunk_id, vec FROM vectors WHERE model = ? ORDER BY chunk_id", (model,)
    ).fetchall()
    if not rows:
        empty = np.zeros((0, 1), dtype="<f4")
        _MATRIX_CACHE.clear()
        _MATRIX_CACHE[key] = (empty, [])
        return empty, []
    row = conn.execute("SELECT dim FROM vectors WHERE model = ? LIMIT 1", (model,)).fetchone()
    dim = int(row[0])
    matrix = np.frombuffer(b"".join(row[1] for row in rows), dtype="<f4").reshape(len(rows), dim)
    ids = [int(row[0]) for row in rows]
    # 只留最新的一份：语料换过之后旧矩阵再也用不上，留着只是占内存。
    _MATRIX_CACHE.clear()
    _MATRIX_CACHE[key] = (matrix, ids)
    return matrix, ids


def _version(conn: sqlite3.Connection) -> str:
    """向量表的版本号（`index.DDL` 的触发器维护）。没有这一行时当 0。"""
    row = conn.execute("SELECT value FROM kv WHERE key = 'vectors'").fetchone()
    return str(row[0]) if row is not None else "0"


def _database_path(conn: sqlite3.Connection) -> str:
    """库文件路径。`:memory:` 这类没有文件，用它的字符串形式当键。"""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return str(row[2])
    except sqlite3.Error:
        pass
    return "?"


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def cache_bytes() -> int:
    """模型缓存占多少盘。**按真实文件去重**：huggingface 的 snapshots 里全是
    指向 blobs 的软链接，直接 `stat()` 会把同一份 235MB 数两遍。"""
    seen: set[Path] = set()
    total = 0
    for item in models_dir().rglob("*"):
        if not item.is_file():
            continue
        try:
            real = item.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            total += real.stat().st_size
        except OSError:
            continue
    return total


def _snapshot(endpoint: str, model: str) -> bool:
    """从指定端点拉一遍模型文件。返回是否成功。

    **必须开子进程**：`huggingface_hub` 在 import 时就把 `HF_ENDPOINT` 读成模块常量了，
    进程内再改环境变量不生效。子进程还能让「镜像 + 关 Xet」这两个开关只作用于这一次，
    不影响后续任何网络行为。
    """
    env = dict(os.environ)
    env["HF_ENDPOINT"] = endpoint
    # 镜像的 Xet 后端会回 401（cas-server.xethub.hf.co），关掉它走普通 HTTP。
    env["HF_HUB_DISABLE_XET"] = "1"
    code = (
        "import sys\n"
        "from huggingface_hub import snapshot_download\n"
        "snapshot_download(sys.argv[1], cache_dir=sys.argv[2],\n"
        "                  allow_patterns=['*.onnx', '*.json', '*.txt', '*.model'])\n"
    )
    print(f"  从 {endpoint} 取 …")
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, _repo_id(model), str(models_dir())],
            env=env,
            check=False,
        )
    except OSError as error:
        print(f"  起不来下载进程：{error}")
        return False
    return result.returncode == 0


def download(model: str = DEFAULT_MODEL) -> int:
    """把模型拉到本机并打印体积。**这是唯一会联网的地方。**

    顺序：`HF_ENDPOINT`（或直连）→ 镜像。直连不通时**自动退到镜像**，并打印
    实际用的是哪个端点——不是悄悄换源，使用者得知道这 255MB 是从哪来的。
    """
    if not _installed():
        print(missing_reason(model))
        return 1
    from fastembed import TextEmbedding

    print(f"下载 {model} 到 {models_dir()} …")
    started = time.monotonic()
    preferred = (os.environ.get("HF_ENDPOINT") or "").strip() or DEFAULT_ENDPOINT
    endpoints = [preferred]
    if MIRROR_ENDPOINT not in endpoints:
        endpoints.append(MIRROR_ENDPOINT)
    for endpoint in endpoints:
        if _snapshot(endpoint, model):
            break
    else:
        print("下载失败：直连与镜像都没成。")
        print(download_hint())
        return 1

    try:
        # 用**离线加载**验一遍：文件不全时这里会当场失败，而不是等到第一次检索。
        encoder = TextEmbedding(
            model_name=model, cache_dir=str(models_dir()), local_files_only=True
        )
    except Exception as error:  # noqa: BLE001 - 下载失败要说人话，不能甩一段栈
        print(f"下载完成但加载失败（{type(error).__name__}: {error}）")
        print(download_hint())
        return 1
    # 编码一条是让 fastembed 真正把 ONNX 会话建起来的最短路径。
    _CACHE[model] = encoder
    list(encoder.embed(["warmup"]))
    print(f"完成：{cache_bytes() / 1024 / 1024:.0f} MB，用时 {time.monotonic() - started:.1f}s")
    return 0


def download_hint() -> str:
    """两个端点都失败时那句「怎么办」。

    自动退镜像已经做过了（见 `download`），这里给的是**再失败**时的排查方向：
    换一个端点、检查代理，或者手动指定。
    """
    endpoint = (os.environ.get("HF_ENDPOINT") or "").strip()
    if endpoint:
        return (
            f"当前 HF_ENDPOINT={endpoint}。换一个端点，或检查网络与代理后重试；\n"
            "镜像走 Xet 后端时常回 401，那就再关掉它：\n"
            "  HF_HUB_DISABLE_XET=1 python -m opencode_tui.core.embed download"
        )
    return (
        "连不上 huggingface.co 时（国内常见），先设镜像再重试：\n"
        "  HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \\\n"
        "    python -m opencode_tui.core.embed download\n"
        "（镜像的 Xet 后端会回 401，所以要关掉它，走普通 HTTP 下载。）"
    )


def _main(argv: list[str]) -> int:
    model = (os.environ.get("VSA_EMBED_MODEL") or "").strip() or DEFAULT_MODEL
    if not argv or argv[0] not in ("download", "index"):
        print("用法：python -m opencode_tui.core.embed download | index")
        return 2
    if argv[0] == "download":
        return download(model)
    conn = index.connect()
    try:
        print(f"模型 {model}，清理别的模型留下的向量：{purge_other_models(conn, model)} 行")
        result = index_missing(conn, model)
        print(result)
        print(f"向量覆盖：{coverage(conn, model)}")
        return 0 if result.get("enabled") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
