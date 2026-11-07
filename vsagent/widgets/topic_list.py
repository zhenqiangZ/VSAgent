"""左栏的主题列表：这一轮分成了哪几段，各自什么结局。

列表只读消息区算出来的那一份（`MessagesPane.topics`），不自己再算一遍主题边界；
点一条把主题键交回消息区，由它负责打开并滚过去。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Static

from .. import palette
from ..core.agent.topics import TopicView
from .session_list import fit
from .topic_card import STATUS_COLOR, STATUS_MARK, topic_state


class TopicRow(Static):
    """左栏的一行主题。"""

    # 与 SessionRow 同一个理由：一行就是一行，标题截断不折行。
    DEFAULT_CSS = """
    TopicRow {
        height: 1;
        padding: 0 1;
    }
    """

    class Picked(TextualMessage):
        """用户点了这一条。"""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(self, topic: TopicView, width: int = 28) -> None:
        super().__init__(classes="topic-row")
        self.key = topic.key
        self._topic = topic
        self._width = width

    def sync(self, topic: TopicView) -> None:
        """换一份主题数据，重画这一行。"""
        self._topic = topic
        self.update(self.render())

    def render(self) -> Text:
        topic = self._topic
        # 等待确认画成半圆：这一轮卡在一个没人回答的确认上，与还在跑的不一样。
        state = topic_state(topic)
        mark = STATUS_MARK.get(state, "○")
        color = STATUS_COLOR.get(state, palette.TEXT_3)
        # 序号 3 格、状态 2 格（空格加字形），其余都给标题。短标题补空格，
        # 状态字形才能一律停在最右边那一列；不补的话每一行的字形位置都不一样，
        # 一列竖着扫下去看不出哪一条还在跑。
        title_width = max(4, self._width - 2 - 5)
        title = fit(topic.title or "新主题", title_width)
        text = Text()
        text.append(
            f"{topic.index:02d} ",
            style=palette.ACCENT if topic.status == "running" else palette.TEXT_4,
        )
        text.append(title, style=palette.TEXT_1)
        text.append(" " * (title_width - cell_len(title)))
        text.append(f" {mark}", style=color)
        return text

    def on_click(self) -> None:
        self.post_message(self.Picked(self.key))


class TopicList(VerticalScroll):
    """主题清单：序号、标题、结局。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._topics: list[TopicView] = []
        # 主题条数、每行的键与画出来的宽度。三者都没变时只改文字，不重建行：
        # 重建会把滚动位置推回顶部，用户正翻着更早的主题就会被弹回去。
        self._keys: tuple[str, ...] = ()
        self._width = 0
        self._header: Static | None = None
        self._rows: list[TopicRow] = []

    def refresh_topics(self, topics: list[TopicView]) -> None:
        self._topics = topics
        width = max(12, self.content_size.width or 28)
        steps = sum(len(topic.steps) for topic in topics)
        header = f"主题 {len(topics)}" + (f" · {steps} 步" if steps else "")
        keys = tuple(topic.key for topic in topics)
        if keys == self._keys and width == self._width and self._rows and self._header:
            self._header.update(header)
            for row, topic in zip(self._rows, topics):
                row.sync(topic)
            return

        # 主题多出来时才跟着滚到底：用户翻着更早的主题时不打断他。
        follow = self._keys == () or self.scroll_y >= self.max_scroll_y - 1
        self._keys = keys
        self._width = width
        self._header = Static(header, classes="rail-section -first")
        self._rows = [TopicRow(topic, width=width) for topic in topics]
        widgets: list[Static] = [self._header, *self._rows]
        if not self._rows:
            widgets.append(Static("这一轮还没有主题", classes="rail-empty"))
        self.remove_children()
        self.mount_all(widgets)
        if follow and topics:
            self.call_after_refresh(self.scroll_end, animate=False)

    def on_resize(self) -> None:
        """宽度变了重截标题，与左栏的会话列表同一条理由。"""
        self.refresh_topics(self._topics)
