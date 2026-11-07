from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Static

from .. import palette

KIND_ICON = {
    "info": "●",
    "success": "✓",
    "warning": "▲",
    "error": "✕",
}


class ToastRack(Container):
    """右下角的提示条容器。

    提示条宽度取 auto，直接挂到屏幕上会贴在左边。容器竖向排列，整块由 `constrain` 拉到
    屏幕右下角，同时挂几条时往上叠，不会压在同一条线上。没有提示条时容器跟着被收掉，不占行。

    容器浮在界面之上，不占布局。`dock: bottom` 会把底部这几行从屏幕的布局里切走，
    提示条出现的那几秒消息区与状态栏整体上移，消失之后再退回去——看起来像界面抖了一下。
    绝对定位让容器脱离布局流；Textual 的绝对定位只能给一个偏移量，没有「贴右下角」这种写法，
    所以偏移量取一个远大于任何终端尺寸的值，再由 `constrain: inside` 把它拉回屏幕内，
    实际落点就是右下角，与容器自身的高度无关。

    宽度收到提示条自己那块，不铺满整行：铺满时容器虽然看不见，却实实在在占着屏幕最底
    那几行，点击落在它身上就到不了底下的组件。消息区底部的「有新内容」提示正好在那几行，
    提示条挂着的那三秒里点它没有任何反应。宽度由 `fit()` 按最宽的那条回填——写 auto 时
    Textual 量的是提示条正文折行之前的宽度，长提示被 `max-width` 收窄之后容器还留在原尺寸，
    提示条会浮在离右边缘老远的地方。

    各条的宽度由提示条自己报上来，不在这里遍历子组件：`remove()` 的摘除是延后的，
    遍历的那一刻被摘掉的那条还在 `children` 里，宽度会停在一个已经不存在的大值上。
    """

    DEFAULT_CSS = """
    ToastRack {
        overlay: screen;
        position: absolute;
        offset: 999 999;
        constrain: inside inside;
        width: auto;
        height: auto;
        layout: vertical;
        align: right bottom;
        /* 底部留 1 行：状态栏是 1 行高，提示条要浮在它上方，不能盖住占比数字与开关。 */
        margin: 0 2 1 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._widths: dict[ToastNotification, int] = {}
        self._fitted = 0

    def note_width(self, toast: ToastNotification, width: int) -> None:
        """记下一条提示条的宽度，按最宽的那条重排容器。"""
        self._widths[toast] = width
        self.fit()

    def forget(self, toast: ToastNotification) -> None:
        """一条提示条走掉：去掉它的宽度，剩下的重新排一遍。"""
        self._widths.pop(toast, None)
        self.fit()

    def fit(self) -> None:
        """把容器宽度收到最宽的那条提示条，右侧边缘与屏幕留出两列。"""
        if not self._widths:
            return
        width = max(self._widths.values())
        if width == self._fitted:
            return
        self._fitted = width
        self.styles.width = width


class ToastNotification(Container):
    """非阻塞通知浮层，右下角显示并在数秒后自动消失。"""

    DEFAULT_CSS = palette.css("""
    ToastNotification {
        height: auto;
        width: auto;
        /* 上限按屏幕宽度算，不按父容器：容器的宽度由提示条自己撑出来，
           百分比挂在它身上是两头互相等的死结，量出来的宽度会小得离谱。 */
        max-width: 60vw;
        margin: 0;
        padding: 0 1;
        background: $vsa-layer;
        color: $vsa-text-1;
    }
    /* 正文取自动宽度并放开到外层内宽：只写 `width: auto` 的话它按整段文字的
       自然宽度量（长提示能到八九十列），外层被 `max-width` 收窄之后正文不跟着换行，
       超出的部分直接被裁掉。放开到 100% 之后，短提示仍按文字宽度收窄，
       长提示在 60% 处折成多行。 */
    ToastNotification .toast-body {
        width: auto;
        max-width: 100%;
        height: auto;
    }
    /* 只有需要人注意的两档带底色：完成与失败。普通提示换成一层浅底就够了，
       四档都铺满一层彩色，屏幕上同时挂几条时反而分不出哪条要紧。 */
    ToastNotification.success { background: $vsa-ok-solid; color: $vsa-on-solid; }
    ToastNotification.warning { background: $vsa-warn; color: $vsa-screen; }
    ToastNotification.error { background: $vsa-error; color: $vsa-on-solid; }
    """)

    def __init__(self, text: str, kind: str = "info") -> None:
        super().__init__(classes=f"toast {kind}")
        self._text = text
        self._kind = kind

    def on_resize(self) -> None:
        """自己的宽度量出来之后，让容器按新宽度重排。

        宽度是排版算出来的（正文有多长、有没有撞上 `max-width`），挂载那一刻读不到，
        等自己收到尺寸变化才是准的。
        """
        parent = self.parent
        if isinstance(parent, ToastRack):
            parent.note_width(self, self.outer_size.width)

    def compose(self) -> ComposeResult:
        icon = KIND_ICON.get(self._kind, "●")
        # 字形与正文之间不留空格：折行是按空格切词的，字形后面跟一个空格就成了
        # 单独一个词，正文那一段整体放不进第一行剩下的宽度，于是被推到下一行，
        # 结果一条两行的提示占三行（第一行只有一个字形）。
        yield Static(f"{icon}{self._text}", classes="toast-body", markup=False)

    @property
    def kind(self) -> str:
        """通知类型。"""
        return self._kind
