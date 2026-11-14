"""一条命令跑完所有判据。

判据分成两组，各自守一件事：

- `tests/test_agent.py`（内核）与 `tests/test_smoke.py`（界面）是主判据，项数最多；
- `tools/check_*.py` 是窄判据：能力表与工具目录是否对得上、族名有没有前缀冲突、
  色值与样式、仓库里有没有把密钥提交上去。

以前这些是「想起来才跑一个」，于是最常见的失败形态不是判据红了，而是**根本没跑**。
这个脚本按固定顺序全跑一遍，任何一条非零退出就停在那儿并打印它的末尾几十行——
后面的判据依赖前面的结论（能力表错了，前缀判据的报错会全是噪声）。

    python tools/check_all.py
    python tools/check_all.py --only smoke
    python tools/check_all.py --list

`--only` 按名字子串过滤，与 `tests/test_agent.py --only` 的习惯一致。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (名字, 相对路径)。顺序有意义：先跑覆盖面最大的两条，再跑窄判据。
JUDGES: tuple[tuple[str, str], ...] = (
    ("agent", "tests/test_agent.py"),
    ("smoke", "tests/test_smoke.py"),
    ("capabilities", "tools/check_capabilities.py"),
    ("toolset", "tools/check_toolset.py"),
    ("prefix", "tools/check_prefix.py"),
    ("style", "tools/check_style.py"),
    ("repo", "tools/check_repo.py"),
)

COUNT = re.compile(r"通过\s*([\d,]+)\s*项检查")
TAIL = 40


def _count(text: str) -> str:
    """从输出里取「通过 N 项检查」的那个 N；没有这一行就留空。"""
    found = COUNT.findall(text)
    return found[-1] if found else ""


def run(name: str, script: str) -> bool:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - started
    output = result.stdout + result.stderr
    count = _count(output)
    detail = f"{count} 项" if count else ""
    status = "ok" if result.returncode == 0 else f"失败（退出码 {result.returncode}）"
    print(f"{name:<14}{detail:>10}{elapsed:>8.1f}s  {status}", flush=True)
    if result.returncode != 0:
        lines = output.rstrip().splitlines()
        print(f"--- {name} 的最后 {min(TAIL, len(lines))} 行 ---")
        print("\n".join(lines[-TAIL:]))
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="跑完所有判据")
    parser.add_argument("--only", default="", help="只跑名字里含这个子串的判据")
    parser.add_argument("--list", action="store_true", help="只列判据，不跑")
    arguments = parser.parse_args()

    chosen = [
        (name, script)
        for name, script in JUDGES
        if not arguments.only or arguments.only in name
    ]
    if not chosen:
        print(f"没有名字含 {arguments.only!r} 的判据。可选：")
        for name, script in JUDGES:
            print(f"  {name:<14}{script}")
        return 2
    if arguments.list:
        for name, script in chosen:
            print(f"{name:<14}{script}")
        return 0

    for name, script in chosen:
        if not run(name, script):
            print(f"\n停在 {name}：后面的判据依赖它的结论，先修这一条。")
            return 1
    print(f"\n{len(chosen)} 条判据全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
