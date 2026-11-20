"""判据：角色目录、模板与工具名单对得上，每个角色的清单在额度之内。

配置决定每个角色装哪些工具（`roles/agents.toml`，见 `roles/agents.py`）。
这条判据查九件事，都能机械判：

1. 角色目录自身完整：标题、工作说明、权限、报告要求、时限。
2. 每个名字都在注册表里（拼错或工具已改名）。
3. 同一角色名单里没有重复的名字。
4. 每个角色至少一个工具。
5. 子会话名单里没有 `dispatch_campaign`。
6. 每个领域工具至少出现在一个名单里。
7. 每个角色的定义开销：超过 10,000 提示，超过 12,000 判失败。
8. 角色前置表自身成立，每个角色都有模板，模板阶段都在五步里。
9. 自定义档是 `orchestrator` 与 `worker` 的并集的子集；`full` 是通配。

    python tools/check_agents.py
    python tools/check_agents.py --verbose
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.coretools import build_core_tools  # noqa: E402
from vsagent.core.agent.domain.all_tools import all_names, domain_tools  # noqa: E402
from vsagent.core.agent.permission import load_disabled  # noqa: E402
from vsagent.core.agent.planning.agent_router import validate_prerequisites  # noqa: E402
from vsagent.core.agent.planning.intent_parser import PROMPT  # noqa: E402
from vsagent.core.agent.roles import agents, catalog  # noqa: E402
from vsagent.core.agent.roles.prompt import estimate_tokens  # noqa: E402
from vsagent.core.agent.roles.templates import TEMPLATES, template_for  # noqa: E402
from vsagent.core.agent.roles.profiles import (  # noqa: E402
    REPORT_REQUIREMENTS,
    validate as validate_profiles,
)

WARN = 10_000
LIMIT = 12_000
FORBIDDEN_IN_WORKER = frozenset({"dispatch_campaign"})


def _costs() -> dict[str, int]:
    costs: dict[str, int] = {}
    for tool in build_core_tools(
        root=ROOT, memory=None, sessions=None, bash_timeout=120
    ):
        spec = tool.spec.to_openai()
        costs[tool.spec.name] = estimate_tokens(json.dumps(spec, ensure_ascii=False))
    for tool in domain_tools():
        spec = tool.spec.to_openai()
        costs[tool.spec.name] = estimate_tokens(json.dumps(spec, ensure_ascii=False))
    return costs


def _prompt_problems() -> list[str]:
    problems: list[str] = []
    for role in catalog.AGENTS:
        if role not in PROMPT:
            problems.append(f"意图提示词里没有角色：{role}")
        for duty in catalog.DUTIES[role]:
            if duty not in PROMPT:
                problems.append(f"意图提示词里没有写 {role} 的工作：{duty}")
    if "capability" in PROMPT.lower():
        problems.append("意图提示词里仍出现 capability 这个旧概念")
    if len(PROMPT) > 5_000:
        problems.append(f"意图提示词 {len(PROMPT)} 字符，超过 5000")
    return problems


def _template_problems() -> list[str]:
    problems: list[str] = []
    for role in catalog.AGENTS:
        if role not in TEMPLATES:
            problems.append(f"{role} 没有模板")
            continue
        template = template_for(role)
        if not template.required_outputs:
            problems.append(f"{role} 的模板没有交付物")
        for step in template.subgoals:
            if step.stage and step.stage not in catalog.STAGES:
                problems.append(f"{role} 的模板阶段不认识：{step.stage}")
    extra = sorted(set(TEMPLATES) - set(catalog.AGENTS))
    if extra:
        problems.append(f"模板里有不存在的角色：{'、'.join(extra)}")
    return problems


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    problems = catalog.validate()
    problems.extend(validate_profiles())
    problems.extend(validate_prerequisites())
    problems.extend(_prompt_problems())
    problems.extend(_template_problems())
    costs = _costs()
    known = set(costs)
    listed: set[str] = set()
    disabled, _source = load_disabled(ROOT)

    print(f"角色 {len(catalog.AGENTS)} 个，操作 {len(catalog.OPERATIONS)} 项")
    print(f"  可用的工具名 {len(known)} 个")

    for role in catalog.AGENTS:
        names = agents.role_tools(role, ROOT)
        if not names:
            problems.append(f"{role} 的名单是空的")
            continue
        if len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            problems.append(f"{role} 的名单里有重复：{'、'.join(duplicates)}")
        missing = [name for name in names if name not in known]
        if missing:
            problems.append(f"{role} 引用了不存在的工具：{'、'.join(missing)}")
        forbidden = sorted(set(names) & FORBIDDEN_IN_WORKER)
        if forbidden:
            problems.append(f"{role} 的名单里有编排工具：{'、'.join(forbidden)}")
        if "*" in agents.entry(role, ROOT).tools:
            problems.append(f"{role} 的名单里写了通配 *：通配只留给核心档")
        usable = [name for name in names if name not in disabled]
        if not usable:
            problems.append(f"{role} 的名单被 tools.toml 关空了，派出去一步也走不了")
        total = sum(costs.get(name, 0) for name in names)
        status = "通过" if total <= WARN else ("提示" if total <= LIMIT else "超出")
        print(f"  {role:14s}{len(names):3d} 件　{total:6d} token　{status}")
        if total > LIMIT:
            problems.append(f"{role} 的清单 {total} token，超过 {LIMIT}")
        listed.update(names)
        if verbose:
            print(f"      {'、'.join(names)}")

    for role, fields in REPORT_REQUIREMENTS.items():
        if role == "principal" or "evidence_refs" not in fields:
            continue
        names = agents.role_tools(role, ROOT)
        lack = [name for name in ("add_evidence", "workspace") if name not in names]
        if lack:
            problems.append(
                f"{role} 的报告要求 evidence_refs，但名单里没有 {'、'.join(lack)}"
            )

    unwrapped = sorted(set(all_names()) - listed)
    if unwrapped:
        problems.append(f"这些领域工具不在任何角色名单里：{'、'.join(unwrapped)}")

    builtin = set(agents.core_tools("orchestrator", ROOT)) | set(
        agents.core_tools("worker", ROOT)
    )
    for tier, listed in agents.all_core(ROOT).items():
        if tier == agents.WILDCARD_TIER:
            if listed != ("*",):
                problems.append(
                    f"core.{tier} 应当是通配 *，现在是 {len(listed)} 个名字"
                )
            continue
        names = [name for name in listed if name != "*"]
        total = sum(costs.get(name, 0) for name in names)
        limit = 8_000 if tier == "orchestrator" else 5_300
        print(f"  {tier:14s}{len(names):3d} 件　{total:6d} / {limit} token")
        if total > limit:
            problems.append(f"{tier} 基础档 {total} token，超过 {limit}")
        if tier in agents.BUILTIN_TIERS:
            continue
        # 自定义档是基础档的收窄：名字必须在 orchestrator 与 worker 里出现过。
        # 少了这条，拼错一个字母就会安静地长出一档只有一件工具的基础名单。
        extra = [name for name in names if name not in builtin]
        if extra:
            problems.append(
                f"core.{tier} 里有基础档之外的名字：{'、'.join(extra)}；"
                f"自定义档只能是 orchestrator 与 worker 的并集的子集"
            )

    if problems:
        print("\n不通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
