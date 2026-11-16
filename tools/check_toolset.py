"""判据：每一档基础名单的开销在预算之内。

工具定义是请求体里最大的一块固定开销，而它涨起来是无声的——加一个字段、
补两句说明、给一个工具多开三个参数，每次只涨几十 token，谁也不会在评审里
看出来。等到发现时，每一步都在多付几千。

三档名单来自 `roles/agents.toml`（见 `roles/agents.py`）。判据只管四件事：

1. 根会话档 `orchestrator` 在 8,000 之内；
2. 子会话档 `worker` 在 5,300 之内；
3. 名单里的每个名字都真的装得出来；
4. 超预算时按体量排序指出该动谁，而不是只说一句「超了」。

`full` 档不做额度判定：它是调试与回滚用的，等于全部工具。

    python tools/check_toolset.py
    python tools/check_toolset.py --verbose
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.coretools import build_core_tools  # noqa: E402
from vsagent.core.agent.domain.all_tools import domain_tools  # noqa: E402
from vsagent.core.agent.roles import agents  # noqa: E402
from vsagent.core.agent.roles.prompt import estimate_tokens  # noqa: E402

# 预算。主会话 8,000、子会话 5,300：都是「压到不能再压之后 + 余量」的数。
# 超了就说明这一步的固定开销又涨了一截，先看是谁涨的。
LIMITS: dict[str, int] = {"orchestrator": 8_000, "worker": 5_300}


def tool_tokens() -> dict[str, int]:
    """按名字返回（工具名，定义 token）。"""
    rows: dict[str, int] = {}
    for tool in build_core_tools(
        root=ROOT, memory=None, sessions=None, bash_timeout=120
    ):
        spec = tool.spec.to_openai()
        rows[tool.spec.name] = estimate_tokens(json.dumps(spec, ensure_ascii=False))
    for tool in domain_tools():
        spec = tool.spec.to_openai()
        rows[tool.spec.name] = estimate_tokens(json.dumps(spec, ensure_ascii=False))
    return rows


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    tokens = tool_tokens()
    failed = False

    for tier, limit in LIMITS.items():
        names = [name for name in agents.core_tools(tier, ROOT) if name != "*"]
        missing = [name for name in names if name not in tokens]
        if missing:
            failed = True
            print(f"{tier}：名单里有装不出来的名字 {missing}")
        kept = [(name, tokens.get(name, 0)) for name in names if name in tokens]
        total = sum(value for _name, value in kept)
        status = "通过" if total <= limit else "超出"
        print(f"{tier:14s} {len(kept):2d} 个　{total:5d} / {limit:5d} token　{status}")
        if total > limit:
            failed = True
            print(f"  超出 {total - limit} token；按体量排序，先考虑这几个：")
            for name, value in sorted(kept, key=lambda item: item[1], reverse=True)[:5]:
                print(f"    {name:18s}{value:5d}")
        if verbose:
            print(f"  {'、'.join(name for name, _ in kept)}")

    full_names = agents.core_tools("full", ROOT)
    if full_names and full_names != ("*",):
        print(f"full 档写了具体名字：{len(full_names)} 个（应为通配）")
        failed = True
    print(f"不分档（full）　{len(tokens):2d} 个　{sum(tokens.values()):5d} token")

    print()
    if not failed:
        print("全部通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
