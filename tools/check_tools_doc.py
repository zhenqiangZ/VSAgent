"""判据：`docs/tools.md` 与注册表一致。

清单由脚本生成，但生成物只有人记得重跑时才是新的。上一次工具从 81 个收到 72 个，
文档里还留着旧名字，读的人照着调，撞的是「没有这个工具」。

比对的是整份文本，逐字节。改工具、改角色配置、改说明文字，都会让它红。

    python tools/check_tools_doc.py
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from gen_tools_doc import render  # noqa: E402

SHOWN = 30


def main() -> int:
    expected, core_count, domain_count = render()
    path = ROOT / "docs" / "tools.md"
    actual = path.read_text(encoding="utf-8")

    if actual == expected:
        print(
            f"docs/tools.md 与注册表一致：{core_count + domain_count} 个工具，"
            f"{len(expected.splitlines())} 行"
        )
        print("通过 1 项检查")
        return 0

    diff = list(
        difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile="docs/tools.md",
            tofile="注册表现状",
            lineterm="",
        )
    )
    print(f"docs/tools.md 落后于注册表：{len(diff)} 行差异")
    print("\n".join(diff[:SHOWN]))
    if len(diff) > SHOWN:
        print(f"……另有 {len(diff) - SHOWN} 行")
    print("\n重跑一次生成器：python tools/gen_tools_doc.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
