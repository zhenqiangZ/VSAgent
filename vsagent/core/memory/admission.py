"""长期记忆的准入规则。

查询结果与长期记忆分开：原始查询结果、临时说法、没有来源的推断都不写进
长期记忆。长期记忆只留经过确认的稳定事实、实验事实与带来源的数据库记录。

`RememberTool` 与本地抽取共用这一份判据。两处各写一套的话，模型这条路上
挡住了，后台抽取那条路会把同样的内容写进去。
"""

from __future__ import annotations

from dataclasses import dataclass

# 原始查询结果的标记。命中这些词说明这一段是从检索结果里抄来的，
# 它属于这一轮的答案状态，不属于跨会话的项目事实。
RAW_SEARCH_MARKERS = (
    "搜索结果显示", "检索结果显示", "查询结果显示", "网页显示", "网页摘要",
    "搜索命中", "本次检索", "查询到", "搜到",
)

# 临时说法的标记。判断还没定下来时不能写成长期事实。
TEMPORARY_MARKERS = (
    "可能", "也许", "推测", "初步判断", "待核对", "尚不确定", "预计",
    "据估计", "不一定", "尚未确认",
)

# 外部事实的标记。出现这些说法时必须有引用，且来源类别要对得上。
EXTERNAL_MARKERS = (
    "文献显示", "文献表明", "研究显示", "研究表明", "论文指出", "论文显示",
    "数据库记录", "数据库显示", "实验显示", "临床结果显示",
)

# 可以承载外部事实的来源类别。
EVIDENCE_SOURCES = frozenset(
    {"literature", "database", "corpus", "experiment", "user"}
)


@dataclass(frozen=True)
class Decision:
    """一条记忆能不能写，以及不能写的原因。"""

    allowed: bool
    reason: str = ""


def check(
    text: str,
    refs: list[str] | None,
    kind: str = "fact",
    *,
    source_type: str = "user",
) -> Decision:
    """判一条记忆能不能写进长期记忆。

    `source_type` 说明这条内容从哪来：`user`（用户确认）、`experiment`、
    `literature`、`database`、`corpus`，或 `search`（原始查询结果）。
    """
    body = (text or "").strip()
    values = [str(item).strip() for item in (refs or []) if str(item).strip()]
    if not body:
        return Decision(False, "内容为空")

    if source_type == "search":
        return Decision(False, "原始查询结果不写长期记忆")

    if any(marker in body for marker in RAW_SEARCH_MARKERS):
        return Decision(False, "原始查询结果不写长期记忆")

    if any(marker in body for marker in TEMPORARY_MARKERS):
        return Decision(False, "临时说法或未确认的推断不写长期记忆")

    if any(marker in body for marker in EXTERNAL_MARKERS):
        if not values:
            return Decision(False, "外部事实没有引用来源")
        if source_type not in EVIDENCE_SOURCES:
            return Decision(False, f"来源类别 {source_type} 不能承载外部事实")

    # 习惯来自用户明确表态，不要求引用；其余类别没有引用时按「未核实」写入，
    # 由记忆层把可信度压低并排在后面。一律拒收会丢掉本来可用的项目事实。
    if kind != "preference" and source_type == "search":
        return Decision(False, "查询来源不写长期记忆")
    return Decision(True)
