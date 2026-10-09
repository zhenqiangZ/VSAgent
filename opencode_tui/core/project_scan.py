from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 扫描时跳过的目录，与文件工具那一侧保持同一批名字
SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".idea", ".vscode", ".probe", ".workbuddy-ai",
}

# 按这些名字判断这个项目用什么语言、用什么工具
LANGUAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("setup.py", "Python"),
    ("package.json", "Node.js"),
    ("tsconfig.json", "TypeScript"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java"),
    ("build.gradle", "Java"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
)

TOOL_MARKERS: tuple[tuple[str, str], ...] = (
    ("pytest.ini", "pytest"),
    ("tox.ini", "tox"),
    ("mypy.ini", "mypy"),
    ("ruff.toml", "ruff"),
    (".flake8", "flake8"),
    (".pre-commit-config.yaml", "pre-commit"),
    ("Makefile", "make"),
    ("Dockerfile", "Docker"),
    ("docker-compose.yml", "Docker Compose"),
    ("eslint.config.js", ".eslintrc"),
)

# 扫描时最多列这么多条，避免一份清单长得没法看
MAX_ENTRIES = 60
MAX_DEPTH = 3

BEGIN_MARK = "<!-- 由 /init 生成，这一段的内容会被下一次 /init 覆盖 -->"
END_MARK = "<!-- /init 生成的内容到此为止 -->"


@dataclass
class ProjectSummary:
    """一次扫描的读数。"""

    root: Path
    languages: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    top_level: list[str] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    file_count: int = 0
    test_files: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """把读数写成一段 Markdown。"""
        blocks: list[str] = []
        if self.languages:
            blocks.append("项目用到的语言：" + "、".join(self.languages))
        if self.tools:
            blocks.append("项目里认得出的工具：" + "、".join(self.tools))
        blocks.append(f"文件总数：{self.file_count}")

        if self.entry_points:
            lines = "\n".join(f"- `{name}`" for name in self.entry_points)
            blocks.append("可能的入口：\n" + lines)

        if self.directories:
            lines = "\n".join(f"- `{name}/`" for name in self.directories)
            blocks.append("目录：\n" + lines)

        if self.test_files:
            lines = "\n".join(f"- `{name}`" for name in self.test_files)
            blocks.append("测试：\n" + lines)

        if self.top_level:
            lines = "\n".join(f"- `{name}`" for name in self.top_level[:MAX_ENTRIES])
            extra = len(self.top_level) - MAX_ENTRIES
            if extra > 0:
                lines += f"\n- 另有 {extra} 项没有列出"
            blocks.append("根目录下的文件：\n" + lines)

        return "\n\n".join(blocks)


def scan_project(root: Path) -> ProjectSummary:
    """扫一棵目录，读出语言、工具、目录结构与几个关键文件。"""
    summary = ProjectSummary(root=root)
    if not root.is_dir():
        return summary

    for marker, language in LANGUAGE_MARKERS:
        if (root / marker).is_file() and language not in summary.languages:
            summary.languages.append(language)
    for marker, tool in TOOL_MARKERS:
        if (root / marker).exists() and tool not in summary.tools:
            summary.tools.append(tool)

    for path in sorted(root.iterdir()):
        if path.name in SKIP_DIRECTORIES:
            continue
        if path.is_dir():
            summary.directories.append(path.name)
        else:
            summary.top_level.append(path.name)

    for path in _walk(root):
        summary.file_count += 1
        name = path.name
        relative = path.relative_to(root).as_posix()
        if _looks_like_test(name, relative):
            summary.test_files.append(relative)
        if _looks_like_entry(name, relative):
            summary.entry_points.append(relative)

    summary.test_files = summary.test_files[:MAX_ENTRIES]
    summary.entry_points = summary.entry_points[:MAX_ENTRIES]
    return summary


def _walk(root: Path) -> list[Path]:
    """列出要计入的那些文件，跳过被排除的目录与深度之外的部分。"""
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIRECTORIES for part in parts):
            continue
        if len(parts) > MAX_DEPTH:
            continue
        found.append(path)
    return found


def _looks_like_test(name: str, relative: str) -> bool:
    """按文件名判断它是不是一个测试。"""
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name in ("test.py", "tests.py"):
        return True
    if name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return True
    return relative.startswith(("tests/", "test/"))


def _looks_like_entry(name: str, relative: str) -> bool:
    """按文件名判断它是不是一个入口。"""
    if name in ("__main__.py", "main.py", "app.py", "cli.py", "index.js", "index.ts"):
        return True
    if name.endswith(".spec"):
        return True
    return relative in ("src/main.py", "src/index.js", "src/index.ts")


def merge_agents_md(existing: str, summary: ProjectSummary) -> str:
    """把扫描读数并进一份 AGENTS.md。

    已有的内容一个字不动：只换掉两个标记之间的那一段。
    文件里没有标记时，把这一段接在末尾，并补上文件的标题。
    """
    generated = f"{BEGIN_MARK}\n\n{summary.to_markdown()}\n\n{END_MARK}"
    if BEGIN_MARK in existing and END_MARK in existing:
        head, _, rest = existing.partition(BEGIN_MARK)
        _, _, tail = rest.partition(END_MARK)
        return f"{head}{generated}{tail}"
    if not existing.strip():
        header = f"# {summary.root.resolve().name}\n\n这个文件是项目的约定，交给协作的模型读。\n"
        return f"{header}\n{generated}\n"
    return f"{existing.rstrip()}\n\n{generated}\n"
