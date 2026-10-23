"""检索评测：方案稿 §8.3 那三条硬门禁。

```
python tools/check_retrieval.py                 # 读 corpus/eval/questions.jsonl
python tools/check_retrieval.py --list          # 只看评测集概况，不跑
python tools/check_retrieval.py --questions x.jsonl --k 5
```

评测集一行一条 JSON：

```json
{"q": "网页抓取接口怎么计费", "ref": "web:d36d762bee", "page": 0, "tag": "cross-lingual"}
```

- `ref` 是源 id（`doc:xxxxxxxxxx` / `web:xxxxxxxxxx`），不是引用号 `doc#…`：
  引用号会随分块变化，源 id 不会。**可以给一组**——同一个问题常常几篇都答得对，
  只标一篇的话，命中另一篇会被算成漏检。
- `page` 可省（网页没有页码）。给了就要求命中的块覆盖这一页。
- `tag` 可省。`identifier` 标记「精确标识符查询」（`4HJO`、`STI`），
  `cross-lingual` 标记「中文问、英文文档答」，两类单独统计。

**评测集不进版本控制**（`corpus/` 整个目录都在 `.gitignore` 里）：它是从真实
使用里攒出来的，与用户手上那批文献绑定。脚本本身进版本控制。

三个组合跑的是**线上同一套代码**（`index.lexical` / `embed.search` /
`corpus_tools.fuse`），不是另写一份检索——另写一份的话，测的是评测脚本而不是
产品，改了线上参数这里还是绿的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opencode_tui.core import corpus, embed, index  # noqa: E402
from opencode_tui.core.agent.domain import corpus as corpus_tools  # noqa: E402

DEFAULT_QUESTIONS = PROJECT_ROOT / "corpus" / "eval" / "questions.jsonl"

TOP_K = 5
"""recall@K 与「引用命中率」都看前 K 条。"""

MRR_DEPTH = 10
"""MRR@10 的深度。与线上返回的 8 组同量级。"""

GROUP_LIMIT = 8
"""线上工具默认返回 8 组（`SearchCorpusTool` 的 `DEFAULT_LIMIT`）。组级指标按这个数。"""

GATE_RECALL_GAIN = 0.08
"""门禁 1：三路要比双词法高 8 个百分点。"""

GATE_IDENTIFIER_DROP = 0.02
"""门禁 2：精确标识符查询的 recall 允许掉 2 个百分点。"""

GATE_LATENCY = 1.0
"""门禁 3：单次检索 1 秒（**热态**，不含首次加载模型）。"""

COMBOS = (
    ("词法单路", ("fts",)),
    ("双词法", ("fts", "cjk")),
    ("三路", ("fts", "cjk", "vec")),
)


def load_questions(path: Path) -> list[dict]:
    """读评测集。**坏行直接报错**：悄悄跳过会让分母变小、指标虚高。"""
    if not path.is_file():
        raise SystemExit(
            f"找不到评测集 {path}。\n"
            "格式见本文件顶部说明；先攒 20～30 条再跑，条数太少时指标没有意义。"
        )
    items: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            item = json.loads(line)
        except ValueError as error:
            raise SystemExit(f"{path}:{number} 不是合法 JSON：{error}") from error
        if not item.get("q") or not item.get("ref"):
            raise SystemExit(f"{path}:{number} 缺 q 或 ref")
        refs = item["ref"] if isinstance(item["ref"], list) else [item["ref"]]
        if not refs or any(":" not in str(ref) for ref in refs):
            raise SystemExit(f"{path}:{number} 的 ref 应该是 `doc:<id>` / `web:<id>`")
        item["refs"] = [str(ref) for ref in refs]
        items.append(item)
    if not items:
        raise SystemExit(f"{path} 是空的")
    return items


def retrieve(conn, query: str, channels_wanted: tuple[str, ...], model: str) -> list[int]:
    """按组合取排好序的 chunk id。与线上 `SearchCorpusTool` 走同一套融合。"""
    channels: dict[str, list[int]] = {}
    if "fts" in channels_wanted or "cjk" in channels_wanted:
        only = tuple(name for name in channels_wanted if name in ("fts", "cjk"))
        channels.update(index.lexical(conn, query, only=only))
    if "vec" in channels_wanted:
        semantic = embed.search(conn, query, model, limit=index.CHANNEL_LIMIT)
        if semantic:
            channels["vec"] = [chunk_id for chunk_id, _ in semantic]
    fused = corpus_tools.fuse(channels)
    return sorted(fused, key=lambda chunk_id: (-fused[chunk_id], chunk_id))


def evaluate(conn, items: list[dict], channels_wanted: tuple[str, ...], model: str) -> dict:
    """跑一遍评测集，返回指标与逐条结果。"""
    recall = 0
    reciprocal = 0.0
    cited = 0
    rows: list[dict] = []
    for item in items:
        ids = retrieve(conn, str(item["q"]), channels_wanted, model)
        found = index.rows_by_id(conn, ids[: max(TOP_K, MRR_DEPTH)])
        top = [found[chunk_id] for chunk_id in ids if chunk_id in found]
        wanted = item["refs"]
        page = int(item.get("page") or 0)

        sources = [_source_of(row) for row in top[:TOP_K]]
        hit = any(ref in sources for ref in wanted)
        recall += int(hit)
        cited += int(any(_cites(top[:TOP_K], ref, page) for ref in wanted))
        rank = next(
            (
                position
                for position, row in enumerate(top[:MRR_DEPTH], 1)
                if _source_of(row) in wanted
            ),
            0,
        )
        if rank:
            reciprocal += 1.0 / rank
        rows.append({"q": item["q"], "hit": hit, "rank": rank, "tag": item.get("tag", "")})

    total = len(items) or 1
    return {
        "recall": recall / total,
        "mrr": reciprocal / total,
        "citation": cited / total,
        "rows": rows,
    }


def evaluate_groups(conn, items: list[dict], channels_wanted: tuple[str, ...], model: str) -> dict:
    """按**工具的真实输出**再量一遍：融合 → 分组 → 600 字预算 → 前 8 组。

    块级指标看「检索到没有」，组级看「模型真的看到什么」：同一篇的碎片会被并成
    一组，片段还会被预算截断——块级命中而片段里没有答案，是真实存在的一档。
    分组用的是线上同一个 `_group`，不另写一份。
    """
    hit = answer = 0
    chars = 0
    rows_out: list[dict] = []
    for item in items:
        channels: dict[str, list[int]] = {}
        if "fts" in channels_wanted or "cjk" in channels_wanted:
            only = tuple(name for name in channels_wanted if name in ("fts", "cjk"))
            channels.update(index.lexical(conn, item["q"], only=only))
        if "vec" in channels_wanted:
            semantic = embed.search(conn, item["q"], model, limit=index.CHANNEL_LIMIT)
            if semantic:
                channels["vec"] = [chunk_id for chunk_id, _ in semantic]
        fused = corpus_tools.fuse(channels)
        order = sorted(fused, key=lambda chunk_id: (-fused[chunk_id], chunk_id))
        candidates = [(cid, fused[cid]) for cid in order[: index.CHANNEL_LIMIT]]
        rows = index.rows_by_id(conn, [cid for cid, _ in candidates])
        groups = corpus_tools._group(conn, [(c, s) for c, s in candidates if c in rows], rows)
        top = groups[: GROUP_LIMIT]
        sources = [group["source_id"] for group in top]
        found = any(ref in sources for ref in item["refs"])
        hit += int(found)
        chars += sum(len(group["text"]) for group in top)
        must = str(item.get("must") or "").lower()
        answered = bool(must) and any(must in group["text"].lower() for group in top)
        answer += int(answered)
        rows_out.append({"q": item["q"], "hit": found, "answered": answered})
    total = len(items) or 1
    answerable = sum(1 for item in items if str(item.get("must") or "")) or 1
    return {
        "recall": hit / total,
        "answer": answer / answerable,
        "chars": chars / total,
        "rows": rows_out,
    }


def _source_of(row) -> str:
    return str(row["source_id"])


def _cites(rows: list, source_id: str, page: int) -> bool:
    """引用是否落到标注的那一页。`page=0`（网页）时只看源对不对。"""
    for row in rows:
        if _source_of(row) != source_id:
            continue
        if not page:
            return True
        low = int(row["page"])
        high = int(row["page_end"]) or low
        if low <= page <= high:
            return True
    return False


def subset(items: list[dict], results: dict, tag: str) -> dict:
    """按 tag 切片重算指标。空切片返回 None。"""
    picked = [row for row in results["rows"] if row["tag"] == tag]
    if not picked:
        return {}
    total = len(picked)
    return {
        "n": total,
        "recall": sum(1 for row in picked if row["hit"]) / total,
        "mrr": sum(1.0 / row["rank"] for row in picked if row["rank"]) / total,
    }


def measure_latency(questions: list[dict], model: str) -> dict:
    """用**真实工具**跑一遍，量端到端耗时（含分组与渲染）。

    先跑一条热身：首次调用要加载模型（本机约 1.2s），那是启动成本，不该算进
    「单次检索」——但它必须在报告里出现，否则用户会以为第一次查询就很快。
    """
    tool = corpus_tools.SearchCorpusTool()
    cold = 0.0
    warm: list[float] = []
    for position, item in enumerate(questions):
        started = time.perf_counter()
        asyncio.run(tool.run({"query": item["q"], "limit": 3}))
        elapsed = time.perf_counter() - started
        if position == 0:
            cold = elapsed
        else:
            warm.append(elapsed)
    ordered = sorted(warm)
    p95 = ordered[int(len(ordered) * 0.95) - 1] if ordered else 0.0
    return {"cold": cold, "p95": p95, "mean": sum(warm) / len(warm) if warm else 0.0}


def footprint() -> dict:
    """依赖占多少盘。**只是参考**：exe 的增量要真打一次包才知道（方案稿 §8.4）。"""
    import importlib.util

    total = 0
    for name in ("fastembed", "onnxruntime", "tokenizers", "huggingface_hub"):
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin:
            continue
        root = Path(spec.origin).parent
        total += sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
    model = embed.cache_bytes()
    return {"packages": total, "model": model}


def render(label: str, result: dict) -> str:
    return (
        f"  {label:8s} recall@{TOP_K}={result['recall']:.3f}  "
        f"MRR@{MRR_DEPTH}={result['mrr']:.3f}  引用命中={result['citation']:.3f}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="检索评测（三条硬门禁）")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--model", default="", help="默认取 corpus.toml / VSA_EMBED_MODEL")
    parser.add_argument("--list", action="store_true", help="只打印评测集概况")
    args = parser.parse_args(argv)

    items = load_questions(args.questions)
    if args.list:
        tags: dict[str, int] = {}
        for item in items:
            tags[str(item.get("tag") or "未标记")] = tags.get(str(item.get("tag") or "未标记"), 0) + 1
        print(f"评测集 {args.questions}：{len(items)} 条")
        for tag, count in sorted(tags.items()):
            print(f"  {tag}: {count}")
        return 0

    from opencode_tui.core import corpus_config

    config = corpus_config.load()
    model = args.model or config.embed_model
    semantic = embed.missing_reason(model)
    if semantic:
        print(f"！{semantic}\n")

    # 先量延迟：第一条必须是**真的冷启动**（加载模型）。放到评测之后再量，
    # 模型早就被 evaluate 加载过了，「首次」会变成一个假的 16ms。
    latency = measure_latency(items, model)

    conn = index.connect()
    try:
        stats = index.stats(conn)
        print(f"语料：{stats['sources']} 个源 / {stats['chunks']} 个块；评测集 {len(items)} 条")
        print(f"模型：{model}（向量覆盖 {embed.coverage(conn, model):.0%}）\n")

        results = {}
        for label, wanted in COMBOS:
            if "vec" in wanted and semantic:
                continue
            results[label] = evaluate(conn, items, wanted, model)
            print(render(label, results[label]))
        if "三路" in results:
            grouped = evaluate_groups(conn, items, ("fts", "cjk", "vec"), model)
            print(
                f"  组级 top{GROUP_LIMIT}  标注源命中={grouped['recall']:.3f}  "
                f"答案在片段里={grouped['answer']:.3f}  平均 {grouped['chars']:.0f} 字"
            )
    finally:
        conn.close()

    lexical = results.get("双词法")
    three = results.get("三路")
    print()
    failures: list[str] = []

    if three is None:
        print("门禁 1/2：无法判定——语义通道没启用，只跑了词法。")
        failures.append("语义通道未启用")
    else:
        gain = three["recall"] - lexical["recall"]
        verdict = "通过" if gain >= GATE_RECALL_GAIN else "未通过"
        print(
            f"门禁 1（三路 recall@{TOP_K} 比双词法高 ≥{GATE_RECALL_GAIN:.0%}）："
            f"{gain:+.3f} → {verdict}"
        )
        if gain < GATE_RECALL_GAIN:
            failures.append("门禁 1")

        for tag, title in (("identifier", "精确标识符"), ("cross-lingual", "中文问英文答")):
            slice_lexical = subset(items, lexical, tag)
            slice_three = subset(items, three, tag)
            if not slice_lexical or not slice_three:
                print(f"  {title}：评测集里没有这一类，跳过")
                continue
            drop = slice_lexical["recall"] - slice_three["recall"]
            if tag == "identifier":
                ok = drop <= GATE_IDENTIFIER_DROP
                print(
                    f"门禁 2（{title} recall@{TOP_K} 掉幅 ≤{GATE_IDENTIFIER_DROP:.0%}）："
                    f"{drop:+.3f}（n={slice_three['n']}）→ {'通过' if ok else '未通过'}"
                )
                if not ok:
                    failures.append("门禁 2")
            else:
                print(
                    f"  {title}：双词法 {slice_lexical['recall']:.3f} → "
                    f"三路 {slice_three['recall']:.3f}（n={slice_three['n']}）"
                )

    ok_latency = latency["p95"] < GATE_LATENCY
    print(
        f"门禁 3（单次检索 <{GATE_LATENCY:.0f}s）：热态 p95={latency['p95'] * 1000:.0f}ms、"
        f"均值={latency['mean'] * 1000:.0f}ms；首次={latency['cold'] * 1000:.0f}ms（含加载模型）"
        f" → {'通过' if ok_latency else '未通过'}"
    )
    if not ok_latency:
        failures.append("门禁 3")

    size = footprint()
    print(
        f"  体积参考：依赖 {size['packages'] / 1024 / 1024:.0f}MB + 模型 "
        f"{size['model'] / 1024 / 1024:.0f}MB（exe 增量需真打包量，见方案稿 §8.4）"
    )

    print()
    if failures:
        print(f"结论：{len(failures)} 条没通过 —— {'、'.join(failures)}")
        return 1
    print("结论：三条门禁全过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
