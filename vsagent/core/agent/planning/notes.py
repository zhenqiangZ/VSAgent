"""带机器码的提示。

各层原本只用一段中文交代「发生了什么」，调用方要分流只能匹配文字。这里给每条
提示配一个 `code` 与 `field`，同时**仍然是字符串**：

- 显示处直接 `f"注意：{item}"`、`"；".join(...)` 不用改；
- 事件负载里 `list(warnings)` 仍然是 JSON 字符串数组；
- 现有按子串断言的测试不用改。

需要按类型分流时读 `note.code`，不要匹配中文。代价是 `Note(...) == "那句话"`
会成立——机器分流靠 code，不靠等值比较。
"""

from __future__ import annotations


class Note(str):
    """一条提示。`code` 是机器码，`field` 指出它跟哪个字段或阶段有关。"""

    code: str
    field: str

    def __new__(cls, code: str, field: str, message: str) -> "Note":
        note = super().__new__(cls, str(message))
        note.code = str(code)
        note.field = str(field)
        return note
