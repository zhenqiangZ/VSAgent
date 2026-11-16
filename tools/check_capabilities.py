"""判据：角色名单与注册表对得上，每个角色的清单在额度之内。

配置决定每个角色装哪些工具（`roles/agents.toml`，见 `roles/agents.py`）。
这条判据查六件事，都能机械判：

1. 每个名字都在注册表里（拼错或工具已改名）；
2. 同一角色名单里没有重复的名字；
3. 每个角色至少一个工具；
4. 子会话名单里没有 `dispatch_campaign`、`agent_status`；
5. 每个领域工具至少出现在一个名单里（漏掉的工具没有任何会话能调）；
6. 每个角色的定义开销：超过 10,000 提示，超过 12,000 判失败。

    python tools/check_capabilities.py
    python tools/check_capabilities.py --verbose
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
from vsagent.core.agent.roles import agents  # noqa: E402
from vsagent.core.agent.roles.capabilities import (  # noqa: E402
    CAPABILITIES,
    ROLES,
    roles_with_capabilities,
    validate,
)
from vsagent.core.agent.roles.prompt import estimate_tokens  # noqa: E402

WARN = 10_000
LIMIT = 12_000
FORBIDDEN_IN_WORKER = frozenset({"dispatch_campaign", "agent_status"})


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


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    problems = validate()
    costs = _costs()
    known = set(costs)
    listed: set[str] = set()
    disabled, _source = load_disabled(ROOT)

    print(f"能力 {len(CAPABILITIES)} 项，角色 {len(ROLES)} 个")
    print(f"  可用的工具名 {len(known)} 个")

    for role in roles_with_capabilities():
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

    unwrapped = sorted(set(all_names()) - listed)
    if unwrapped:
        problems.append(f"这些领域工具不在任何角色名单里：{'、'.join(unwrapped)}")

    for tier, limit in (("orchestrator", 8_000), ("worker", 5_300)):
        names = [name for name in agents.core_tools(tier, ROOT) if name != "*"]
        total = sum(costs.get(name, 0) for name in names)
        print(f"  {tier:14s}{len(names):3d} 件　{total:6d} / {limit} token")
        if total > limit:
            problems.append(f"{tier} 基础档 {total} token，超过 {limit}")

    if problems:
        print("\n不通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
