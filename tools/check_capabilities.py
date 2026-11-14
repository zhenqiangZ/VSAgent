"""判据：能力注册表与真实工具目录一致。

表写死在 `capabilities.py` 里，好处是可读可评审，代价是会和工具目录漂开：
族改名、工具挪族、常驻工具改名，表不会自己跟着动。这条判据把四种漂移查出来：

1. 引用了不存在的族（改名之后最常见的形态）；
2. 引用了不存在的常驻工具；
3. 把一个登记在某族里的工具又声明成常驻工具（两处声明会打架）；
4. 权限取值不认识、或一个能力什么都没声明（读还是写、能做还是不能做都说不清）。

另外报一张"每个角色要用几族、多少 token"的账，供 Phase 6 排计划时参考。

    python tools/check_capabilities.py
    python tools/check_capabilities.py --verbose
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.capabilities import (  # noqa: E402
    CAPABILITIES,
    families_for,
    roles_with_capabilities,
    standalone_for,
    validate,
)
from vsagent.core.agent.domain.all_tools import catalog, domain_tools  # noqa: E402
from vsagent.core.agent.toolset import family_tokens  # noqa: E402


class _Registry:
    """只给 `family_tokens` 用：它要一个带 `specs()` 的对象。"""

    def __init__(self) -> None:
        self._specs = [tool.spec.to_openai() for tool in domain_tools()]

    def specs(self) -> list[dict]:
        return self._specs


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    problems = validate()
    print(f"能力 {len(CAPABILITIES)} 项，角色 {len(roles_with_capabilities())} 个")

    registry = _Registry()
    table = catalog()
    for role in roles_with_capabilities():
        families = families_for(role)
        standalone = standalone_for(role)
        # 注意：`family_tokens` 收的是**工具名**，不是族名（名字有歧义，见 P-20）。
        tool_names = [name for family in families for name in table.get(family, ())]
        cost = family_tokens(registry, tool_names) if tool_names else 0
        print(
            f"  {role:16s} 族 {len(families):2d} 个　约 {cost:5d} token"
            + (f"　常驻：{'、'.join(standalone)}" if standalone else "")
        )
        if verbose and families:
            print(f"      {'、'.join(families)}")

    if problems:
        print("\n不通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
