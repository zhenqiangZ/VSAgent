"""判据：工具说明按四段体例写。

说明是每一步都要重发的那一段固定开销：领域工具 61 条合计两万五千 token，
比子会话的全部常驻工具还多。它涨起来是无声的——补一句举例、加一段背景，
每条只多几十 token，乘上条数与步数就是几千。体例把这件事变成可机械检查的：

1. **问题**：第一段是一句问句，写它回答什么；
2. **输入**：必填项，以及最容易弄错的那一个；
3. **输出**：产物是什么，登记成什么类型的条目；
4. **边界**：分数方向、失败怎么办、这件事做不到什么。

写法固定成四行，标签在行首：「输入：」「输出：」「边界：」，问句在它们之前。
行首标签是给判据认的，模型读到的也是同样的四段。

六项检查，都能机械判：

- 四段齐全；
- 第一段是一句问句（以「？」结尾，中间没有句号）；
- 每条不超过 900 字符；
- 每个参数说明不超过 160 字符；
- 不出现内部词（缓存键名、文件名、内部函数名这类）；
- 同一句话不在两个工具里重复出现——分工只写一处。

只管 54 个领域工具。常驻工具（读写、命令、派发这类）不套这四段：它们回答的是
通用操作，没有「产物登记成什么」这一段；它们的额度由 `check_toolset.py` 管。

默认拦截；`--report` 只报数、退出码为零，供改写途中看进度。

    python tools/check_tool_descriptions.py
    python tools/check_tool_descriptions.py --report
    python tools/check_tool_descriptions.py --verbose
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.domain.all_tools import domain_tools  # noqa: E402
from vsagent.core.agent.roles.prompt import estimate_tokens  # noqa: E402

# 四段的名字与顺序。问句没有标签，排在第一个标签之前。
LABELS: tuple[str, ...] = ("输入", "输出", "边界")
QUESTION = "问题"

# 单条说明与单条参数说明的字符上限。900 字符约合两百多个汉字，
# 够写清边界；超过它多半是在写教程，而教程对模型没有用。
MAX_DESCRIPTION = 900
MAX_PARAMETER = 160

# 重复句的最短长度。短于这个数的句子多是「不要传文件路径」这类固定提醒，
# 重复出现是合理的；长句重复才是分工没写在一处。
MIN_SENTENCE = 12

# 内部词。这些词对模型没有意义，只会把实现细节带进上下文。
INTERNAL_WORDS: tuple[str, ...] = (
    "cache",
    "manifest",
    "_MEIPASS",
    "sqlite",
    "session_state",
    ".json",
)

# 句子的切分：句号、问号、叹号、分号、换行都算。
SENTENCE = re.compile(r"[^。！？；\n]+[。！？；]?")
# 去掉加粗标记与空白之后比较，避免同一句话因为排版不同而漏判。
NOISE = re.compile(r"[\s*`]")


class Row(NamedTuple):
    name: str
    origin: str  # 「领域」或「常驻」
    description: str
    parameters: dict
    cost: int  # 整条定义的 token：额度按这个数算，不按说明本身


class Finding(NamedTuple):
    category: str
    tool: str
    detail: str


def collect() -> list[Row]:
    """收齐领域工具的说明。"""
    rows: list[Row] = []
    for tool in domain_tools():
        spec = tool.spec
        rows.append(
            Row(
                spec.name,
                "领域",
                spec.description,
                spec.parameters,
                estimate_tokens(json.dumps(spec.to_openai(), ensure_ascii=False)),
            )
        )
    return rows


def split_sections(text: str) -> tuple[str, dict[str, str]]:
    """按行首标签切成四段。标签之前的部分是问句。"""
    question: list[str] = []
    parts: dict[str, list[str]] = {label: [] for label in LABELS}
    current: str | None = None
    for line in text.strip().splitlines():
        stripped = line.strip()
        hit = next(
            (label for label in LABELS if stripped.startswith(label + "：")),
            None,
        )
        if hit is not None:
            current = hit
            rest = stripped[len(hit) + 1 :].strip()
            if rest:
                parts[hit].append(rest)
            continue
        bucket = question if current is None else parts[current]
        if stripped:
            bucket.append(stripped)
    return (
        "".join(question).strip(),
        {label: "".join(items).strip() for label, items in parts.items()},
    )


def sentences(text: str) -> list[str]:
    """切出可以跨工具比较的句子。去掉标签行与过短的片段。"""
    found: list[str] = []
    for line in text.splitlines():
        if any(line.strip().startswith(label + "：") for label in LABELS):
            continue
        for piece in SENTENCE.findall(line):
            cleaned = NOISE.sub("", piece).strip("。！？；")
            if len(cleaned) >= MIN_SENTENCE:
                found.append(cleaned)
    return found


def check(rows: list[Row]) -> list[Finding]:
    findings: list[Finding] = []
    holders: dict[str, list[str]] = {}

    for row in rows:
        question, sections = split_sections(row.description)

        missing = [label for label in LABELS if not sections[label]]
        if not question:
            missing.insert(0, QUESTION)
        if missing:
            findings.append(Finding("四段齐全", row.name, "缺 " + "、".join(missing)))

        if question:
            if not question.endswith("？"):
                findings.append(Finding("第一段问句", row.name, "结尾不是问号"))
            elif "。" in question.rstrip("？"):
                findings.append(Finding("第一段问句", row.name, "问句里出现了句号"))

        size = len(row.description)
        if size > MAX_DESCRIPTION:
            findings.append(
                Finding("单条长度", row.name, f"{size} 字符，超过 {MAX_DESCRIPTION}")
            )

        for name, spec in (row.parameters.get("properties") or {}).items():
            note = spec.get("description") if isinstance(spec, dict) else None
            if isinstance(note, str) and len(note) > MAX_PARAMETER:
                findings.append(
                    Finding(
                        "参数说明",
                        row.name,
                        f"{name} 的说明 {len(note)} 字符，超过 {MAX_PARAMETER}",
                    )
                )

        lowered = row.description.lower()
        for word in INTERNAL_WORDS:
            if word.lower() in lowered:
                findings.append(Finding("内部词", row.name, f"出现 {word}"))
                break

        for sentence in sentences(row.description):
            holders.setdefault(sentence, []).append(row.name)

    for sentence, names in holders.items():
        distinct = sorted(set(names))
        if len(distinct) > 1:
            findings.append(
                Finding("重复句", "、".join(distinct), f"「{sentence[:40]}」出现在多件工具里")
            )
    return findings


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    strict = "--report" not in sys.argv[1:]

    rows = collect()
    findings = check(rows)
    definition = sum(row.cost for row in rows)
    described = sum(estimate_tokens(row.description) for row in rows)

    print(
        f"领域工具说明 {len(rows)} 条；定义合计 {definition:,} token，"
        f"其中说明本身 {described:,}"
    )

    categories = ("四段齐全", "第一段问句", "单条长度", "参数说明", "内部词", "重复句")
    failed_by: dict[str, set[str]] = {name: set() for name in categories}
    for item in findings:
        failed_by[item.category].add(item.tool)
    for name in categories:
        passed = len(rows) - len(failed_by[name])
        print(f"  {name:8s}{passed:4d} / {len(rows)}")

    if findings:
        print()
    for name in categories:
        items = [item for item in findings if item.category == name]
        if not items:
            continue
        print(f"{name}：{len(items)} 处")
        shown = items if verbose else items[:8]
        for item in shown:
            print(f"  {item.tool:24s}{item.detail}")
        if len(items) > len(shown):
            print(f"  ……另有 {len(items) - len(shown)} 处")
        print()

    if not findings:
        print("\n全部通过")
        return 0
    if strict:
        return 1
    print("只报数，不拦。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
