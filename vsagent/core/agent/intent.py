from __future__ import annotations

from dataclasses import dataclass

from .prompt import estimate_tokens

HINT_LIMIT = 60
"""提示整行的 token 上限。一行来源名最多四个短语，60 够写完整。

超了就整句丢掉、不截半句：半句开头读起来像乱码，与 `chunker._last_sentence`
宁可放弃这一句的理由一样。
"""

SOURCE_ORDER = ("task", "memory", "corpus", "history")

SOURCE_NAMES = {
    "task": "任务状态与工作区索引",
    "memory": "常驻记忆",
    "corpus": "本机语料",
    "history": "本会话的历史主题",
}

RULES: dict[str, tuple[str, ...]] = {
    "task": ("进度", "到哪了", "下一步", "还没做", "待办", "剩什么"),
    "memory": ("我说过", "习惯", "记住", "约定", "以后都", "我喜欢"),
    "corpus": (
        "文献",
        "论文",
        "资料",
        "研究",
        "证据",
        "原文",
        "出处",
        "paper",
        "literature",
    ),
    "history": (
        "上次",
        "之前",
        "刚才",
        "前面",
        "讨论过",
        "上一轮",
        "那次",
        "为什么失败",
        "回顾",
        "previously",
        "last time",
    ),
}


@dataclass(frozen=True)
class IntentHint:
    """一句话可能在问哪几类来源。"""

    sources: tuple[str, ...] = ()   # 按 SOURCE_ORDER 排好、去重
    matched: tuple[str, ...] = ()   # 命中的词，进 trace


def detect(text: str) -> IntentHint:
    """按关键词表判断这句话可能在问哪几类来源。

    只看词面，不做分类器：规则认不出绕开这些词的说法，就什么都不提示。
    猜错的代价比不提示高——提示指向一个不该看的地方，模型会跟着走。
    """
    lowered = text.lower()
    found: list[str] = []
    matched: list[str] = []
    for source in SOURCE_ORDER:
        for word in RULES[source]:
            if word in lowered:
                if source not in found:
                    found.append(source)
                if word not in matched:
                    matched.append(word)
    return IntentHint(sources=tuple(found), matched=tuple(matched))


def render(hint: IntentHint) -> str:
    """把判断结果写成一行；没有命中或写不下时给空串。"""
    if not hint.sources:
        return ""
    names = "、".join(SOURCE_NAMES[source] for source in hint.sources)
    line = f"（可能相关的来源：{names}。）"
    if estimate_tokens(line) > HINT_LIMIT:
        return ""
    return line
