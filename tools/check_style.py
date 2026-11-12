"""判据：配色与排版只从一处取值。

两端的样式各有一份唯一的取值处：终端在 `vsagent/palette.py`，网页在
`web/` 下样式文件的 `:root`。判据管的是「有没有出现第二个来源」，样式好坏不在这里。
四条都能机械判：

1. **终端色值只出现在 `palette.py`**：`app.tcss` 与 widget 里的样式文本只写
   `$vsa-*`，富文本的 `style=` 只写 `palette.ACCENT` 这类常量。色值抄进第二处之后，
   改配色就得记得两处，漏掉的那一处不会报错。
2. **字号只认那六档**：网页里每条 `font-size` 都是 `var(--fs-*)`。字号一旦就地写死，
   层级就多出一档，读者分不出这一块比正文大还是小。
3. **间距字面量只留 1–3px**：`margin` / `padding` / `gap` 里大于 3px 的值必须走
   `var(--space-*)`。1–3px 是描边与胶囊标签的上下沿，不属于那八档。
4. **声明过的变量都有人用**：`--fs-*` / `--space-*` / `$vsa-*` 声明了却没被引用，
   说明档位在增加时没有回收；引用了却没声明，网页那边会静默失效。

注释与 docstring 里的色值不算：`app.tcss` 里引用 gruvbox `$surface` 的那句
`#3C3836` 是在讲为什么不用它。三位的短写不查——它与 `#fade` 这种 id 选择器
用正则分不开，而两端都没有在用短写。

    python tools/check_style.py
"""

import ast
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.palette import CSS_VARIABLES  # noqa: E402

WEB_DIR = ROOT / "web"
TUI_DIR = ROOT / "vsagent"
PALETTE = TUI_DIR / "palette.py"

# 六档字号。名字在这里再写一遍是故意的：多出一档要有人来改这张表，
# `--fs-huge` 不能自己生效。
FONT_TOKENS = {
    "--fs-display",
    "--fs-title",
    "--fs-section",
    "--fs-body",
    "--fs-small",
    "--fs-meta",
}
# 八档间距，编号 1 到 8。写成范围：加一档只需改这里。
SPACE_STEPS = 8
# 1–3px 是描边与胶囊标签的上下沿，不参与那套档位。
SPACING_MAX = 3

COMMENT = re.compile(r"/\*.*?\*/", re.S)
WEB_DECLARED = re.compile(r"(--[a-z0-9-]+)\s*:")
WEB_USED = re.compile(r"var\(\s*(--[a-z0-9-]+)")
FONT_SIZE = re.compile(r"(?<![-\w])font-size\s*:\s*([^;{}]+)")
SPACING = re.compile(
    r"(?<![-\w])(margin|padding|gap|row-gap|column-gap)"
    r"(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?"
    r"\s*:\s*([^;{}]+)"
)
PX = re.compile(r"(-?\d*\.?\d+)px")
# 六到八位十六进制。八位形式一起匹配，否则前六位后面还跟着十六进制字符。
HEX = re.compile(r"#[0-9a-fA-F]{6,8}(?![0-9a-zA-Z_-])")
TUI_DECLARED = re.compile(r"^\s*(\$vsa-[a-z0-9-]+)\s*:", re.M)
TUI_USED = re.compile(r"\$vsa-[a-z0-9-]+")


def blank_comments(text: str) -> str:
    """把注释换成等长的空白，行号与偏移保持不变。"""
    return COMMENT.sub(
        lambda match: "".join(
            "\n" if char == "\n" else " " for char in match.group()
        ),
        text,
    )


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def web_sheets() -> list[Path]:
    """网页侧要查的样式文件。

    `web/vendor/` 是第三方库，它不按这六档八档写，也不该由我们改。
    """
    return sorted(
        path
        for path in WEB_DIR.rglob("*.css")
        if "vendor" not in path.relative_to(WEB_DIR).parts
    )


def check_web(failures: list[str]) -> tuple[int, int, int]:
    """网页端：字号六档、间距八档。返回（样式文件数、字号声明数、间距声明数）。

    声明与引用按全体样式文件合起来算：档位在 `:root` 里声明一处，
    用到它的规则可以在别的文件里。
    """
    sheets = web_sheets()
    declared: set[str] = set()
    used: set[str] = set()
    font_values = {f"var({name})" for name in FONT_TOKENS}
    steps = {f"--space-{step}" for step in range(1, SPACE_STEPS + 1)}

    fonts = 0
    spaces = 0
    for path in sheets:
        label = path.relative_to(ROOT).as_posix()
        text = blank_comments(path.read_text(encoding="utf-8"))
        declared |= set(WEB_DECLARED.findall(text))
        used |= set(WEB_USED.findall(text))

        for match in FONT_SIZE.finditer(text):
            fonts += 1
            value = match.group(1).strip()
            if value not in font_values:
                failures.append(
                    f"{label}:{line_of(text, match.start())} 字号写成 {value}，"
                    "只能用那六档 --fs-*"
                )

        for match in SPACING.finditer(text):
            spaces += 1
            for size in PX.finditer(match.group(2)):
                if abs(float(size.group(1))) > SPACING_MAX:
                    failures.append(
                        f"{label}:{line_of(text, match.start())} "
                        f"{match.group(1)} 用了 {size.group(0)}，"
                        "大于 3px 的间距要用 --space-*"
                    )

    # 档位之外的名字归「多出档位」那一条，不再重复报「声明了没有引用」。
    for name in sorted(declared - used):
        if name in FONT_TOKENS | steps:
            failures.append(f"网页样式：声明了 {name} 却没有一处引用")
    for name in sorted(used - declared):
        if name.startswith(("--fs-", "--space-")):
            failures.append(f"网页样式：引用了未声明的 {name}")

    extra_fonts = sorted(
        name for name in declared
        if name.startswith("--fs-") and name not in FONT_TOKENS
    )
    if extra_fonts:
        failures.append(f"网页样式：多出字号档位：{extra_fonts}")
    extra_spaces = sorted(
        name for name in declared
        if name.startswith("--space-") and name not in steps
    )
    if extra_spaces:
        failures.append(f"网页样式：多出间距档位：{extra_spaces}")

    return len(sheets), fonts, spaces


def scan_python(path: Path) -> tuple[list[tuple[int, str]], set[str]]:
    """源码里的字符串字面量与 `palette.<常量>` 引用。

    注释与独立成句的字符串（docstring）都不算：docstring 里会引述色值与变量名
    （讲「为什么不用它」），它们不参与样式。独立成句的字符串用 AST 认：
    `ast.Expr` 包着一个字符串常量就是。
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                end = node.end_lineno or node.lineno
                doc_lines.update(range(node.lineno, end + 1))

    strings: list[tuple[int, str]] = []
    refs: set[str] = set()
    with path.open("rb") as handle:
        tokens = [
            token for token in tokenize.tokenize(handle.readline)
            if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
        ]
    for index, token in enumerate(tokens):
        if token.type == tokenize.STRING and token.start[0] not in doc_lines:
            strings.append((token.start[0], token.string))
        # `palette.TEXT_1` 这类常量引用：token 序列里是三个词，中间隔着点号。
        elif token.type == tokenize.NAME and token.string == "palette" and index + 2 < len(tokens):
            dot, name = tokens[index + 1], tokens[index + 2]
            if dot.string == "." and name.type == tokenize.NAME:
                refs.add(name.string)
    return strings, refs


def check_tui(failures: list[str]) -> int:
    """终端：色值只在 `palette.py`，变量都有人用。返回扫过的色值数。"""
    used: set[str] = set()
    hexes = 0

    for path in sorted(TUI_DIR.rglob("*.py")):
        if path == PALETTE:
            continue
        strings, refs = scan_python(path)
        for line, text in strings:
            for found in HEX.findall(text):
                hexes += 1
                failures.append(
                    f"{path.relative_to(ROOT)}:{line} 写了色值 {found}，"
                    "终端色值只在 vsagent/palette.py"
                )
            used.update(TUI_USED.findall(text))
        # 富文本的 style= 走常量，不经过 CSS 变量；两者算同一次引用。
        for name in refs:
            key = f"vsa-{name.lower().replace('_', '-')}"
            if key in CSS_VARIABLES:
                used.add(f"${key}")

    for path in sorted(TUI_DIR.rglob("*.tcss")):
        text = blank_comments(path.read_text(encoding="utf-8"))
        for match in HEX.finditer(text):
            hexes += 1
            failures.append(
                f"{path.relative_to(ROOT)}:{line_of(text, match.start())} "
                f"写了色值 {match.group()}，终端色值只在 vsagent/palette.py"
            )
        used.update(TUI_USED.findall(text))
        # 变量声明只能留在 palette.py：这里再声明一份，widget 的 DEFAULT_CSS 拿到的
        # 仍是 `palette.css()` 换出来的值，两份值从此各走各的。
        redeclared = sorted(set(TUI_DECLARED.findall(text)))
        if redeclared:
            failures.append(
                f"{path.relative_to(ROOT)} 又声明了变量 {redeclared}，"
                "色值要留在 palette.py"
            )

    declared = {f"${name}" for name in CSS_VARIABLES}
    for name in sorted(declared - used):
        failures.append(f"palette.py 声明了 {name} 却没有一处引用")
    for name in sorted(used - declared):
        failures.append(f"样式里引用了 palette.py 没有声明的 {name}")

    return hexes


def main() -> int:
    failures: list[str] = []
    sheets, fonts, spaces = check_web(failures)
    hexes = check_tui(failures)
    print(
        f"网页：{sheets} 份样式，字号 {fonts} 处、间距 {spaces} 处，"
        f"档位 {len(FONT_TOKENS)} + {SPACE_STEPS}"
    )
    print(f"终端：色值 {hexes} 处（palette.py 之外应为 0），变量 {len(CSS_VARIABLES)} 个")

    if failures:
        print("\n未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
