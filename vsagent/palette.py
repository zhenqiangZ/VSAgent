# 终端配色：唯一写色值的地方。
#
# `app.tcss` 用 `$vsa-*` 变量，由 `VSAgentApp.get_css_variables` 从 `CSS_VARIABLES`
# 注入；widget 的 `DEFAULT_CSS` 用 `css()` 在导入时换成色值；Rich 标记里的 `style=`
# 直接用上面那些常量。
#
# 样式表这条路必须走变量：Textual 的变量按样式表隔离，只在 `app.tcss` 里声明的话
# widget 的 `DEFAULT_CSS` 看不见。widget 那条走 `css()`，是因为判据里有用普通 `App`
# 单独挂一个 widget 的用例，那种应用没有 `get_css_variables`。
#
# 语义色按用途分开：同一个语义在深底与实底上要两个值。绿色文字 `#4CAF50` 压在
# 屏幕上够亮，压白字的实底要 `#2E7D32`，反过来两个都不行。

from __future__ import annotations

import re

SCREEN = "#000000"       # 屏幕底
RAIL = "#0A0A0A"         # 栏底：状态栏、侧栏、输入区块、弹窗里的输入框
SUNKEN = "#101010"       # 下沉底：权限弹窗的参数预览，比栏底再暗一档
LAYER = "#1A1A1A"        # 分层底：用户消息、选中项、普通提示、弹窗本体
LINE_SOFT = "#2A2A2A"    # 装饰线：未聚焦的指示线、聚焦时的抬亮
LINE = "#333333"         # 分隔线：侧栏左边界、弹窗里的框线
LINE_STRONG = "#3A3A3A"  # 更亮一档的装饰线：折叠块的左边线
TEXT_4 = "#4A4A4A"       # 四级文字：快捷键提示、侧栏开关、弹窗里的元信息
TEXT_3 = "#5A5A5A"       # 三级文字：状态栏、模式提示、历史提示、弹窗里的提示行
TEXT_2 = "#8B949E"       # 次要文字：分节标题、系统事件、状态行、弹窗里的说明
TEXT_1 = "#E0E0E0"       # 主文字：正文、列表标题、悬停时的文字
CURSOR = "#FFFFFF"       # 输入光标那个方块：落在空格上时只有底色看得见
ON_SOLID = "#FFFFFF"     # 语义色实底上的字
ACCENT = "#4A9EFF"       # 蓝：焦点与选中
OK = "#4CAF50"           # 绿：完成，深底上的文字与字形
OK_SOLID = "#2E7D32"     # 绿：完成，实底，上面压白字
WARN = "#FFB300"         # 橙：等待与中断
ERROR = "#C62828"        # 红：失败

# 样式表用的变量。前缀 `vsa-` 与 Textual 自带的 `$surface` / `$accent` 分开：
# 那批变量跟着 `/themes` 变，应用自己的区块不跟着变。
CSS_VARIABLES: dict[str, str] = {
    "vsa-screen": SCREEN,
    "vsa-rail": RAIL,
    "vsa-sunken": SUNKEN,
    "vsa-layer": LAYER,
    "vsa-line-soft": LINE_SOFT,
    "vsa-line": LINE,
    "vsa-line-strong": LINE_STRONG,
    "vsa-text-4": TEXT_4,
    "vsa-text-3": TEXT_3,
    "vsa-text-2": TEXT_2,
    "vsa-text-1": TEXT_1,
    "vsa-cursor": CURSOR,
    "vsa-on-solid": ON_SOLID,
    "vsa-accent": ACCENT,
    "vsa-ok": OK,
    "vsa-ok-solid": OK_SOLID,
    "vsa-warn": WARN,
    "vsa-error": ERROR,
}

_VARIABLE = re.compile(r"\$vsa-[a-z0-9-]+")


def css(text: str) -> str:
    """把 CSS 文本里的 `$vsa-*` 换成色值。

    widget 的 `DEFAULT_CSS` 用它包一层。Textual 的变量按样式表隔离，`app.tcss`
    那份到不了 widget；只挂一个 widget 的应用也拿不到 `get_css_variables`。
    写错的变量名在这里当场抛出，不必等到渲染。
    """
    return _VARIABLE.sub(lambda match: CSS_VARIABLES[match.group()[1:]], text)
