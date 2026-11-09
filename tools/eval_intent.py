import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from vsagent.core.agent import intent  # noqa: E402

DEFAULT_CASES = PROJECT_ROOT / ".probe/intent-cases.jsonl"


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as error:
            print(f"第 {number} 行不是合法 JSON：{error}")
            continue
        query = str(item.get("q") or "").strip()
        expected = [str(part) for part in item.get("sources") or [] if str(part).strip()]
        if not query:
            print(f"第 {number} 行缺 q，跳过")
            continue
        cases.append({"q": query, "sources": expected})
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="来源提示的规则表评测")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="标注集路径")
    args = parser.parse_args()
    path = Path(args.cases)
    if not path.is_file():
        print(f"没有这份标注集：{path}")
        return 1
    cases = load_cases(path)
    if not cases:
        print("标注集是空的")
        return 1

    hit = 0
    for case in cases:
        hint = intent.detect(case["q"])
        got = list(hint.sources)
        want = case["sources"]
        ok = set(got) == set(want)
        if ok:
            hit += 1
        mark = "命中" if ok else "漏判"
        print(f"{mark}　{case['q']}\n　　期望 {want or '（不提示）'}　实际 {got or '（不提示）'}"
              f"　词 {list(hint.matched)}")

    print(f"\n判断正确　{hit}/{len(cases)} = {hit / len(cases):.1%}")
    print("这只说明规则表准不准；提示有没有改变行为，要在已有会话上比对工具调用次数与步数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
