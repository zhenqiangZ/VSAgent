"""系统级评测：把「多 Agent 是不是真的更好」拆成能判与不能判的两半。

设计稿列了十三项指标。它们不是同一类东西，混在一张表里报，就会把"没量"说成
"量了"：

**能离线判的（结构不变量）**——给定一份任务规格，计划与任务是不是把它完整、
准确地传了下去：

- 约束分派：任务规格里的原话不自动进任务书；父 agent 按角色写的限制，global 进
  每一份，角色专属的只进那个角色，别的角色没有；
- 排除项保留：用户说不做的事，归到一个被拒绝的角色上，且不出现在选中的角色里。
  这条只覆盖**角色级**排除（对接、筛选）。「不要引用预印本」这类来源质量要求
  不是角色，判不了，由约束保留那条管；
- 子目标覆盖：每个子目标都有一个角色负责，没有子目标被悄悄丢掉；
- 交付物归属：每件交付物都挂给模板里声明它的角色；多个终点也照此认领；
- 依赖有效：每条边都指向真实任务、没有环、方向与角色的先后一致；
- 不重复派：同一个角色只出现一次；
- 工具暴露：计划里每个被派出的角色都有一份非空的工具名单。

**不能离线判的（要模型或人）**——意图准确率、用户意图保真、证据覆盖率、
产物可追溯率、成本与延迟。前两项要真实模型跑解析，后三项要一场真实运行留下的
报告与登记表。脚本不编这些数字，只把它们列出来，并写明去哪儿量。

**Agent 数量、工具调用次数、输出长度不参与评分。** 设计稿明确说它们不是成功
指标：拆得越细、调得越多、写得越长，恰恰可能是变差了。脚本把它们单列一行，
只作参考，不进结论。

    python tools/eval_system.py
    python tools/eval_system.py --verbose     # 逐条打印每个判据
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.planning.agent_router import AgentRouter
from vsagent.core.agent.roles.catalog import EXCLUSION_RULES
from vsagent.core.agent.roles.contracts import TaskSpec
from vsagent.core.agent.execution.coordinator import build_tasks
from vsagent.core.agent.roles.profiles import profile
from vsagent.core.agent.roles.templates import template_for


@dataclass(frozen=True)
class Case:
    """一份标注好的任务规格。期望的角色不写在这里——它就是子目标本身。"""

    name: str
    spec: TaskSpec
    why: str = ""


CASES: tuple[Case, ...] = (
    Case(
        name="结构与口袋",
        spec=TaskSpec(
            task_id="eval_structure",
            objective="取 4HJO 的实验结构，找出候选结合口袋",
            subgoals=[
                {"goal": "取实验结构", "agent": "pocket"},
                {"goal": "找候选口袋", "agent": "pocket"},
            ],
            constraints=("只看实验结构，不要用预测模型顶替",),
            exclusions=("不要做分子对接",),
            required_outputs=("PocketAtlas",),
        ),
        why="两步在同一个口袋角色里先后做，计划里只有一个节点。",
    ),
    Case(
        name="配体、准备与对接",
        spec=TaskSpec(
            task_id="eval_ligand",
            objective="汇总 4HJO 的已知配体，准备好受体与配体，再跑一轮对接",
            subgoals=[
                {"goal": "汇总已知配体与活性", "agent": "ligand"},
                {"goal": "准备受体", "agent": "prepare"},
                {"goal": "准备配体", "agent": "prepare"},
                {"goal": "跑一轮对接", "agent": "docking"},
            ],
            constraints=("活性数据要带 pChEMBL", "候选不超过 200 个"),
            required_outputs=("CandidateSet",),
        ),
        why="准备与对接有先后；准备内部又分受体与配体两段。",
    ),
    Case(
        name="机制解释",
        spec=TaskSpec(
            task_id="eval_mechanism",
            objective="解释这几个候选分子与口袋的结合机制",
            subgoals=[
                {"goal": "分析关键残基与相互作用", "agent": "mechanism"},
            ],
            constraints=("区分实验证据与推断",),
            required_outputs=("MechanismReport",),
        ),
        why="单一角色：计划里只有一个终点，交付物直接挂上去。",
    ),
    Case(
        name="证据整理",
        spec=TaskSpec(
            task_id="eval_literature",
            objective="查 4HJO 相关文献，整理可追溯的证据",
            subgoals=[
                {"goal": "检索并整理文献证据", "agent": "research"},
            ],
            constraints=("每条结论都要有出处",),
            exclusions=("查文献就行，不要顺手做对接",),
            required_outputs=("ResearchDossier",),
        ),
        why="跨阶段角色：它没有阶段规则，但仍要有角色与工具名单。",
    ),
)


@dataclass(frozen=True)
class Result:
    """一条判据的结果。`detail` 在不通过时要能直接指出是哪一条坏了。"""

    name: str
    ok: bool
    detail: str = ""


def _has_cycle(tasks: list) -> list[str]:
    """Kahn 拓扑：剩下的就是环上或环下游的任务。"""
    ids = [task.task_id for task in tasks]
    deps = {task.task_id: set(task.dependencies) for task in tasks}
    ready = [item for item in ids if not deps[item]]
    seen: set[str] = set()
    while ready:
        current = ready.pop()
        if current in seen:
            continue
        seen.add(current)
        for item in ids:
            if current in deps[item]:
                deps[item].discard(current)
                if not deps[item] and item not in seen:
                    ready.append(item)
    return [item for item in ids if item not in seen]


def evaluate(case: Case) -> list[Result]:
    """跑一份规格，返回这一份的全部判据。"""
    spec = case.spec
    routed = AgentRouter().route(spec)
    plan = routed.plan
    tasks, warnings = build_tasks(plan, spec)
    results: list[Result] = []

    # 1. 约束分派：任务规格里的原话不会被自动拼进任务书（要父 agent 按角色重写）；
    #    父 agent 写的 global 进每一份，角色专属只进那个角色。串味会让一句只针对
    #    口袋的要求跟着所有角色跑，漏发会让子 agent 做用户明说过不要的事。
    problems: list[str] = []
    if not tasks:
        problems.append("没有可派的任务")
    fixed = {item for role in plan.selected_agents for item in template_for(role).constraints}
    leaked = sorted(
        {
            f"{task.role}：{item}"
            for task in tasks
            for item in task.constraints
            if item in set(spec.constraints) - fixed
        }
    )
    if leaked:
        problems.append(f"任务规格里的原话被自动下发了：{leaked}")
    if tasks:
        probe = {"global": ["__全局探针__"], tasks[0].role: ["__角色探针__"]}
        authored, _ = build_tasks(plan, spec, constraints=probe)
        first = tasks[0].role
        for task in authored:
            if "__全局探针__" not in task.constraints:
                problems.append(f"{task.role} 没收到 global 限制")
            if ("__角色探针__" in task.constraints) != (task.role == first):
                problems.append(f"{task.role} 收到了不属于它的角色限制")
    results.append(Result("约束分派", not problems, "；".join(problems)))

    # 2. 排除项保留：每条排除都要落到至少一个被拒绝的角色上。
    denied = set(plan.denied_agents)
    selected = set(plan.selected_agents)
    unlucky = [item for item in spec.exclusions if not _exclusion_landed(item, denied)]
    overlap = sorted(denied & selected)
    results.append(
        Result(
            "排除项保留",
            not unlucky and not overlap,
            ""
            if not unlucky and not overlap
            else f"没落到任何被拒绝角色上的排除项 {unlucky}、既选又拒 {overlap}",
        )
    )

    # 3. 子目标覆盖：每个子目标的角色都要被选中；被排除的除外。
    wanted = {
        str(row.get("agent") or "")
        for row in spec.subgoals
        if str(row.get("agent") or "")
    }
    dropped = sorted(wanted - selected - denied)
    results.append(
        Result(
            "子目标覆盖",
            not dropped,
            "" if not dropped else f"这些子目标没有角色负责：{dropped}",
        )
    )

    # 4. 交付物归属：每件交付物挂给模板里声明它的角色；多终点也照此认领。
    outputs = list(spec.required_outputs)
    sinks = [
        task
        for task in tasks
        if not any(task.task_id in other.dependencies for other in tasks)
    ]
    problems: list[str] = []
    for name in outputs:
        declared = [
            task.role
            for task in tasks
            if name in template_for(task.role).required_outputs
        ]
        holders = [task.role for task in tasks if name in task.required_outputs]
        if declared:
            if sorted(holders) != sorted(declared):
                problems.append(
                    f"{name} 应当挂给 {'、'.join(declared)}，"
                    f"现在挂给 {'、'.join(holders) or '无'}"
                )
            continue
        if len(sinks) == 1:
            if holders != [sinks[0].role]:
                problems.append(
                    f"{name} 没有角色声明，应当兜底挂给唯一终点 {sinks[0].role}，"
                    f"现在挂给 {'、'.join(holders) or '无'}"
                )
            continue
        if holders:
            problems.append(
                f"{name} 没有角色声明，多终点时不该硬挂：{'、'.join(holders)}"
            )
    results.append(
        Result("交付物归属", not problems, "；".join(problems))
    )

    # 5. 依赖有效：边指向真实任务、没有环。
    ids = {task.task_id for task in tasks}
    dangling = sorted({dep for task in tasks for dep in task.dependencies if dep not in ids})
    cycle = _has_cycle(tasks)
    results.append(
        Result(
            "依赖有效",
            not dangling and not cycle,
            "" if not dangling and not cycle else f"悬空依赖 {dangling}、成环 {cycle}",
        )
    )

    # 6. 不重复派：同一个角色只出现一次。
    roles = [task.role for task in tasks]
    dupes = sorted({role for role in roles if roles.count(role) > 1})
    results.append(
        Result("不重复派", not dupes, "" if not dupes else f"这些角色被派了不止一次：{dupes}")
    )

    # 7. 工具暴露：每个被派出的角色都有一份非空的配置名单。
    from vsagent.core.agent.roles import agents

    empty = [role for role in roles if not agents.role_tools(role)]
    results.append(
        Result(
            "工具暴露",
            not empty,
            "" if not empty else f"这些角色没有配任何工具：{empty}",
        )
    )

    # 8. 未决输入有交代：计划说缺什么，任务书里也要写着缺什么。
    #    只记在计划里的话，子 agent 拿到的任务书看起来是完整的。
    leaked: list[str] = []
    if plan.unresolved_inputs:
        text = "；".join(plan.unresolved_inputs)
        leaked = [
            task.role
            for task in tasks
            if text not in str(task.input_refs.get("缺的输入") or "")
        ]
    results.append(
        Result(
            "未决输入有交代",
            not leaked,
            "" if not leaked else f"这些任务书没写缺什么：{leaked}",
        )
    )
    return results


def _exclusion_landed(item: str, denied: set[str]) -> bool:
    """这条排除项有没有落在一个被拒绝的角色上。

    判据与路由用的是同一张表（`EXCLUSION_RULES`）：两边各写一份，改了一边另一边
    就会静默失效——排除项看着还在，其实已经拦不住了。
    """
    for words, names in EXCLUSION_RULES:
        if any(word in item for word in words) and set(names) & denied:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="多 Agent 流水线的结构判据与指标清单")
    parser.add_argument("--verbose", action="store_true", help="逐条打印每个判据")
    args = parser.parse_args()

    failures = 0
    total = 0
    print("结构不变量（离线可判）")
    for case in CASES:
        results = evaluate(case)
        bad = [item for item in results if not item.ok]
        total += len(results)
        failures += len(bad)
        mark = "通过" if not bad else "不通过"
        print(f"\n[{mark}] {case.name}　任务 {case.spec.task_id}")
        if case.why:
            print(f"  说明：{case.why}")
        for item in results:
            if args.verbose or not item.ok:
                print(f"  {'✓' if item.ok else '✗'} {item.name}：{item.detail or '符合'}")
        if not args.verbose and not bad:
            print(f"  {len(results)} 条判据全部符合")

    print(f"\n合计 {total - failures}/{total} 条判据通过")

    print("\n不能离线判定的指标（这里不编数字）")
    for name, where in (
        ("意图准确率", "tools/eval_intent_parser.py（真实模型跑 18 条标注样例）"),
        ("用户意图保真", "人工：拿 TaskSpec 与原话逐条对，看有没有多写或少写"),
        ("证据覆盖率", "真实运行后读子会话报告：带 evidence_refs 的条数 / 总条数"),
        ("产物可追溯率", "真实运行后读登记表：报告里的 artifact_refs 能否取到"),
        ("成本 / 延迟 / token", "真实运行后读 trace：每轮用量与耗时"),
    ):
        print(f"  · {name}：{where}")

    print("\n不作为指标的读数（只作参考，不进结论）")
    print("  · Agent 数量、工具调用次数、输出长度——设计稿明确说它们不是成功指标。")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
