"""记忆评测：台账健康度 + 一份人工标注的召回集。

```
python tools/eval_memory.py                      # 只看台账健康度
python tools/eval_memory.py --cases corpus/eval/memory.jsonl --k 5
```

**它不进 CI。** 召回集要人工标注（一条问题对应「应该是哪几条记忆」），
而台账是用户自己的项目事实 —— 与 `corpus/` 一样，属于本地数据。
脚本本身进版本控制，评测集不进。

评测集一行一条 JSON：

```json
{"q": "配体库有多少个分子", "expect": ["ligandset_v3"]}
```

`expect` 里放**正文里的可辨片段**，不放 id：id 带时间戳，条目重建一次就全变了。
一条问题可以标多个片段，命中任意一个就算召回成功。

两类指标：

- `recall@k`：期望的片段出现在前 k 条里；
- `stale recall`：命中的条目里有多少 `state == "stale"` 或 `unverified` ——
  它们不是错，但「靠过期记忆答对了」不值得当成成绩。目标是 ≤3%。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from opencode_tui.core.agent.memory import (  # noqa: E402
    MAX_ENTRIES,
    RESIDENT_LIMIT,
    MemoryStore,
)
from opencode_tui.core.agent.prompt import estimate_tokens  # noqa: E402


def normalize(text: str) -> str:
    """判重用的归一化：空白压成一个空格。与 `remember` 的口径一致。"""
    return " ".join(text.split())


def report_stats(store: MemoryStore) -> int:
    """台账健康度：多少条、多少还生效、多少重复、常驻占多少。"""
    entries = list(store.entries.values())
    live = [entry for entry in entries if entry.is_live()]
    states = Counter(entry.state for entry in entries)
    sources = Counter(entry.source or "未知" for entry in entries)
    duplicates = [
        text for text, count in Counter(normalize(entry.text) for entry in entries).items()
        if count > 1
    ]
    resident = store.resident_text()

    print(f"记忆目录　{store.root}")
    print(f"条目　　　{len(entries)} / 上限 {MAX_ENTRIES}　生效 {len(live)}")
    print(
        "来源　　　"
        + "　".join(f"{name} {count}" for name, count in sources.most_common())
    )
    print(
        "引用状态　"
        + "　".join(f"{name} {count}" for name, count in states.most_common())
    )
    print(f"被取代　　{sum(1 for entry in entries if entry.superseded_by)}")
    print(f"未验证　　{sum(1 for entry in entries if entry.unverified)}")
    print(
        f"常驻注入　{estimate_tokens(resident)} / {RESIDENT_LIMIT} token"
        f"（{len(resident.splitlines())} 条）"
    )
    print(f"重复正文　{len(duplicates)} 组")
    for text in duplicates[:5]:
        print(f"  · {text[:60]}")
    if len(entries) > MAX_ENTRIES:
        print("注意：条目数超过上限，下一次写入会把最旧的挪进 archive.json")
    return 0


def load_cases(path: Path) -> list[dict]:
    """读评测集；坏行跳过并报出来。"""
    cases: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as error:
            print(f"第 {number} 行不是 JSON，跳过：{error}")
            continue
        query = str(item.get("q") or "").strip()
        expect = [str(part) for part in item.get("expect") or [] if str(part).strip()]
        if not query or not expect:
            print(f"第 {number} 行缺 q 或 expect，跳过")
            continue
        cases.append({"q": query, "expect": expect})
    return cases


def run_cases(store: MemoryStore, cases: list[dict], k: int) -> int:
    """跑召回集：recall@k 与 stale 比例。"""
    hits = 0
    stale_hits = 0
    total_hits = 0
    missed: list[str] = []
    for case in cases:
        found = store.recall(case["q"], limit=k)
        total_hits += len(found)
        stale_hits += sum(1 for entry in found if entry.state == "stale" or entry.unverified)
        text = "\n".join(entry.text for entry in found)
        if any(piece in text for piece in case["expect"]):
            hits += 1
        else:
            missed.append(case["q"])

    total = len(cases)
    print(f"评测集　{total} 条　k={k}")
    if not total:
        return 1
    print(f"recall@{k}　{hits}/{total} = {hits / total:.1%}")
    rate = stale_hits / total_hits if total_hits else 0.0
    print(f"stale 比例 {rate:.1%}（目标 ≤3%）")
    if missed:
        print("没召回的：")
        for query in missed[:10]:
            print(f"  · {query}")
    return 0 if hits / total >= 0.85 and rate <= 0.03 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆台账评测（离线）")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="项目根，默认当前仓")
    parser.add_argument("--cases", default="", help="人工标注的 JSONL 评测集")
    parser.add_argument("--k", type=int, default=5, help="recall@k 的 k")
    args = parser.parse_args()

    store = MemoryStore(Path(args.root))
    if not store.load():
        print(f"还没有记忆：{store.entries_path}")
        return 1
    store.revalidate()
    report_stats(store)
    if not args.cases:
        print("\n给一份 --cases 才跑召回指标（格式见本文件开头）。")
        return 0
    cases = load_cases(Path(args.cases))
    print()
    return run_cases(store, cases, args.k)


if __name__ == "__main__":
    raise SystemExit(main())
