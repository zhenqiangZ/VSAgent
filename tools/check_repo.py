"""判据：版本库里不许出现凭据与本地运行状态。

2026-11-14 在仓库根发现一份未跟踪的 `test.py`，第 11 行写着一个真实的模型接口 key。
它没进 git 是运气，不是规矩——`git add .` 一次就带进去了。这条判据把「靠运气」
换成「提交前扫一遍」：

1. 模型接口 key（`sk-…` 开头，长度够长）；
2. 常见云凭据（AWS `AKIA…`、GitHub `ghp_…`）；
3. 赋给 `api_key` / `secret` / `password` 的长字面量；
4. 被跟踪的 `.env` 一类文件本身。

只扫**仓库里被跟踪的文件**（`git ls-files`），不扫 `.probe`、`runs` 这些忽略项：
它们本来就不进版本库，扫进来只会天天报警，报警多了就没人看。

    python tools/check_repo.py
    python tools/check_repo.py --staged     # 只扫暂存区（提交前用）
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只看这些后缀：二进制文件读成文本会误报，也不该把 key 写进二进制里。
SUFFIXES = {
    ".py", ".md", ".toml", ".txt", ".cfg", ".ini", ".yaml", ".yml",
    ".json", ".js", ".css", ".html", ".sh", ".spec", ".env",
}

# 一个文件最多读这么多字节：超过的（数据集、压缩包）不是凭据的常见藏身处。
MAX_BYTES = 512 * 1024

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("模型接口 key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    (
        "硬编码密钥",
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|secret|password|token)\b
            \s*[:=]\s*
            ["'][^"'\s]{16,}["']
            """
        ),
    ),
)

# 明显是占位符的值：示例、文档、测试夹具里出现不算泄露。
PLACEHOLDERS = ("sk-test", "sk-xxx", "sk-your", "your-", "example", "<", "${", "changeme")

# 这些文件本身不该被跟踪。
FORBIDDEN_FILES = {".env"}


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return [line for line in out.stdout.splitlines() if line.strip()]


def files_to_scan(staged: bool) -> list[str]:
    if staged:
        return _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return _git("ls-files")


def is_candidate(path: str) -> bool:
    item = Path(path)
    return item.suffix.lower() in SUFFIXES or item.name in {".env", ".env.local"}


def scan(path: str) -> list[str]:
    target = ROOT / path
    try:
        if target.stat().st_size > MAX_BYTES:
            return []
        text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    hits: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        lowered = line.lower()
        if any(token in lowered for token in PLACEHOLDERS):
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                hits.append(f"{path}:{line_no}  {label}")
                break
    return hits


def main() -> int:
    staged = "--staged" in sys.argv[1:]
    files = files_to_scan(staged)
    problems: list[str] = []

    for path in files:
        if Path(path).name in FORBIDDEN_FILES:
            problems.append(f"{path}  本地凭据文件被跟踪了")
            continue
        if is_candidate(path):
            problems.extend(scan(path))

    scope = "暂存区" if staged else "版本库"
    print(f"扫描{scope} {len(files)} 个文件")
    if problems:
        print(f"\n发现 {len(problems)} 处可疑凭据：")
        for item in problems:
            print(f"  {item}")
        print("\n先把它们改成环境变量或删掉，再提交。")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
