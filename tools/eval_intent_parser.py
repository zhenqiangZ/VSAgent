"""意图解析的对照评测：拿真实模型跑一组标注样例，报四类判断。

四条判据对应方案里的四条硬要求：

1. **操作**：期望的角色必须出现在结果里（允许多选、允许多出）。
2. **排除项**：用户说“不要做某件事”时，`exclusions` 里必须出现那个说法。
3. **澄清**：只有“不回答就无法开始”才追问。发现类任务缺编号不算阻塞。
4. **两轴**：任务关系与对话行为要判对。

    python tools/eval_intent_parser.py
    python tools/eval_intent_parser.py --only 3
    python tools/eval_intent_parser.py --verbose

它不检查措辞，只检查这四件事——措辞会随模型变，这四件事是方案对解析器的要求。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.planning.intent_parser import IntentParseError, IntentParser
from vsagent.core.agent.provider import OpenAICompatProvider, ProviderConfig  # noqa: E402


@dataclass(frozen=True)
class Case:
    text: str
    operations: tuple[str, ...] = ()
    exclusion: str = ""
    clarify: bool | None = None
    relation: str = ""
    action: str = ""
    context: str = ""
    why: str = ""


CASES: tuple[Case, ...] = (
    Case("EGFR", clarify=True, why="只有名词，没说想做什么"),
    Case(
        "获取 EGFR 的全部实验结构",
        operations=("pocket",),
        clarify=False,
        why="缺编号不是阻塞：任务就是去发现",
    ),
    Case(
        "获取 EGFR 的所有候选结合口袋",
        operations=("pocket",),
        clarify=False,
        why="同上，发现口袋不追问 PDB 编号",
    ),
    Case("找出 EGFR 已知的抑制剂和它们的活性", operations=("ligand",), clarify=False),
    Case(
        "对这批分子做虚拟筛选",
        operations=("docking",),
        clarify=True,
        why="说了这批分子但没给分子",
    ),
    Case("比较这几个候选分子的对接结果可靠性", operations=("analysis",)),
    Case("分析候选分子与口袋的结合机制", operations=("mechanism",)),
    Case(
        "帮我分析这批 EGFR 抑制剂，不要进行分子对接，只比较化学骨架和已知活性",
        operations=("ligand",),
        exclusion="对接",
        clarify=True,
        why="禁止对接必须留下；分子没给所以允许追问",
    ),
    Case(
        "研究 EGFR，并寻找新型抑制剂",
        operations=("research", "ligand"),
        why="复合任务要同时出现调研与配体",
    ),
    Case("继续之前的研究", relation="resume"),
    Case("什么是变构调节", relation="none", action="chat"),
    # 轮次类型：给了当前任务摘要才判得出来。
    Case(
        "继续",
        relation="continue",
        action="execute",
        context="目标：获取 EGFR 的全部实验结构；禁止：不得联网取预测结构；状态：running",
    ),
    Case(
        "再多取几个结构，但不要用冷冻电镜的",
        relation="continue",
        action="execute",
        context="目标：获取 EGFR 的全部实验结构；状态：running",
        exclusion="冷冻电镜",
    ),
    Case(
        "改成只看 T790M 突变体",
        relation="modify",
        action="execute",
        context="目标：获取 EGFR 的全部实验结构；状态：running",
    ),
    Case(
        "这个结果说明什么",
        action="ask_result",
        context="目标：获取 EGFR 的全部实验结构；状态：done",
    ),
    Case(
        "现在到哪一步了",
        action="status_query",
        context="目标：获取 EGFR 的全部实验结构；状态：running",
    ),
    Case(
        "算了，别做了",
        action="cancel",
        context="目标：获取 EGFR 的全部实验结构；状态：running",
    ),
    Case(
        "把第二个口袋的结果和第一个比较一下",
        operations=("analysis",),
        relation="compare",
        action="execute",
        context="目标：获取 EGFR 的全部实验结构；状态：done",
        why="比较是任务关系，不重跑上游",
    ),
)


def config() -> ProviderConfig:
    path = Path.home() / ".vsagent" / "connection.toml"
    if not path.is_file():
        raise SystemExit(f"没有连接配置：{path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return ProviderConfig(
        base_url=data["base_url"],
        api_key=data["api_key"],
        model=data["model"],
        context_window=int(data.get("context_window") or 128_000),
    )


async def run(cases: list[Case], verbose: bool) -> int:
    parser = IntentParser(OpenAICompatProvider(config()))
    passed = 0
    for index, case in enumerate(cases, start=1):
        try:
            spec = await parser.parse(case.text, context=case.context)
        except IntentParseError as error:
            print(f"[{index}] 解析失败：{error}")
            continue
        problems: list[str] = []
        missing = [item for item in case.operations if item not in spec.operations]
        if missing:
            problems.append(f"缺操作 {missing}，实际 {list(spec.operations)}")
        if case.exclusion:
            hit = any(case.exclusion in item for item in spec.exclusions)
            if not hit:
                problems.append(f"排除项没留下（期望含「{case.exclusion}」）：{list(spec.exclusions)}")
        if case.relation and spec.task_relation != case.relation:
            problems.append(f"任务关系不对：期望 {case.relation}，实际 {spec.task_relation}")
        if case.action and spec.conversation_action != case.action:
            problems.append(f"对话行为不对：期望 {case.action}，实际 {spec.conversation_action}")
        if case.clarify is not None:
            asked = bool(spec.clarification_required)
            if asked != case.clarify:
                problems.append(
                    f"澄清判断不对：期望{'' if case.clarify else '不'}追问，实际"
                    f"{'追问了 ' + str(len(spec.clarification_required)) + ' 条' if asked else '没追问'}"
                )
        if problems:
            print(f"[{index}] 不通过　{case.text}")
            for item in problems:
                print(f"      {item}")
            if verbose:
                print(f"      操作 {list(spec.operations)}　排除 {list(spec.exclusions)}")
                print(f"      关系 {spec.task_relation}　行为 {spec.conversation_action}")
                print(f"      追问 {list(spec.clarification_required)}")
        else:
            passed += 1
            print(f"[{index}] 通过　　{case.text[:28]}")
            if verbose:
                print(
                    f"      操作 {list(spec.operations)}　关系 {spec.task_relation}　"
                    f"行为 {spec.conversation_action}　置信 {spec.confidence}"
                )
    print(f"\n通过 {passed}/{len(cases)}")
    return 0 if passed == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="意图解析对照评测")
    parser.add_argument("--only", type=int, default=0, help="只跑第几条（从 1 数）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    selected = list(CASES)
    if args.only:
        if not 1 <= args.only <= len(CASES):
            raise SystemExit(f"--only 超出范围：1–{len(CASES)}")
        selected = [CASES[args.only - 1]]
    return asyncio.run(run(selected, args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
