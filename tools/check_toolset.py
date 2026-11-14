"""判据：常驻核心工具的每一步开销，两档各在预算之内。

为什么要单独一条判据：工具定义是请求体里最大的一块固定开销（实测二十一个常驻
工具 7,809 token，比典型一轮装入的领域工具族还多），而它涨起来是无声的——
加一个字段、补两句说明、给一个工具多开三个参数，每次只涨几十 token，谁也不会
在评审里看出来。等到发现时，每一步都在多付几千。

判据只管四件事，都能机械判：

1. 两档各自的总量在预算内（主 8,000 / 子 5,300）；
2. 两档的工具数与档位名单一致，没有工具被漏登记或重复登记；
3. 每个工具的定义都算得出来（名字、说明、参数齐全）；
4. 超预算时按涨幅排序指出该动谁，而不是只说一句「超了」。

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
from vsagent.core.agent.roles.prompt import estimate_tokens
from vsagent.core.agent.roles.toolprofiles import (  # noqa: E402
    ORCHESTRATOR,
    WORKER,
    WORKER_CORE,
    keeps,
)

# 预算。主 agent 的 8,000 是「压到不能再压之后 + 余量」：二十一个工具压缩后
# 7,955，余量只剩四十五。再加工具就得先删或合并——那是行为改动，不归这条判据管。
# 子 agent 的 4,800 来自 worker 档实测 4,204，留六百的余量。
#
# 2026-11-13（P-79）：开工盘点按能力分给角色之后，`assess_campaign`（483）与
# `survey_structures`（475）从主 agent 移到 worker。这是能力分工的直接代价，
# 两边的数都跟着换：主 7,991 → 7,033，子 4,257 → 5,215。上限按同样的余量重定：
# 主仍留 8,000（现在余量反而更大），子 4,800 → 5,300。
LIMITS: dict[str, int] = {ORCHESTRATOR: 8_000, WORKER: 5_300}

# 按角色放行的常驻工具单列一条预算：目前只有假设角色要 `hypothesis`
# （1,042 token）。它不进 `LIMITS[WORKER]`，否则所有子 agent 的额度会被这一个
# 角色的需要一起抬高；也不做特殊分支，就按「哪个角色、多少」列出来。
ROLE_LIMITS: dict[tuple[str, str], int] = {(WORKER, "hypothesis"): 6_300}


def tool_tokens() -> list[tuple[str, int]]:
    """按装配顺序返回（工具名，定义 token）。"""
    tools = build_core_tools(
        root=ROOT,
        memory=None,
        sessions=None,
        bash_timeout=120,
    )
    rows: list[tuple[str, int]] = []
    for tool in tools:
        spec = tool.spec.to_openai()
        name = spec.get("function", {}).get("name") or ""
        if not name:
            raise SystemExit(f"工具没有名字：{tool!r}")
        rows.append((name, estimate_tokens(json.dumps(spec, ensure_ascii=False))))
    return rows


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    rows = tool_tokens()
    names = [name for name, _ in rows]
    duplicated = {name for name in names if names.count(name) > 1}

    print(f"常驻工具 {len(rows)} 个，定义合计 {sum(tok for _, tok in rows)} token")
    if duplicated:
        print(f"重名工具：{'、'.join(sorted(duplicated))}")
    print()

    header = f"{'工具':18s}{'token':>7s}{'主':>4s}{'子':>4s}{'假设':>6s}"
    print(header)
    for name, tok in sorted(rows, key=lambda item: item[1], reverse=True):
        main_mark = "√" if keeps(ORCHESTRATOR, name) else "·"
        worker_mark = "√" if keeps(WORKER, name) else "·"
        role_mark = "√" if keeps(WORKER, name, "hypothesis") else "·"
        print(f"{name:18s}{tok:7d}{main_mark:>4s}{worker_mark:>4s}{role_mark:>6s}")
    print()

    failed = False
    if duplicated:
        failed = True
    for profile, limit in LIMITS.items():
        kept = [(name, tok) for name, tok in rows if keeps(profile, name)]
        total = sum(tok for _, tok in kept)
        status = "通过" if total <= limit else "超出"
        print(f"{profile:14s} {len(kept):2d} 个　{total:5d} / {limit:5d} token　{status}")
        if total > limit:
            failed = True
            over = total - limit
            print(f"  超出 {over} token；按体量排序，先考虑这几个：")
            for name, tok in sorted(kept, key=lambda item: item[1], reverse=True)[:5]:
                print(f"    {name:18s}{tok:5d}")
        if verbose:
            dropped = [name for name, _ in rows if not keeps(profile, name)]
            if dropped:
                print(f"  不装：{'、'.join(dropped)}")
    for (profile, role), limit in ROLE_LIMITS.items():
        kept = [(name, tok) for name, tok in rows if keeps(profile, name, role)]
        total = sum(tok for _, tok in kept)
        status = "通过" if total <= limit else "超出"
        print(
            f"{profile}/{role:11s} {len(kept):2d} 个　{total:5d} / {limit:5d} token　{status}"
        )
        if total > limit:
            failed = True
    print()

    full_total = sum(tok for _, tok in rows)
    print(f"不分档（full）　{len(rows):2d} 个　{full_total:5d} token")
    # 明确留给子 agent 的那几个：单列一行，改档位时看得见它们在哪一边。
    if WORKER_CORE:
        core_tokens = {name: tok for name, tok in rows}
        listed = [(name, core_tokens.get(name, 0)) for name in sorted(WORKER_CORE)]
        print(
            "worker 核心　"
            + "、".join(f"{name}（{tok}）" for name, tok in listed)
        )
        if any(name not in core_tokens for name in WORKER_CORE):
            failed = True
            print("  worker 核心名单里有装配表中不存在的工具")
    if not failed:
        print("全部通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
