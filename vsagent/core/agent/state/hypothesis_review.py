"""假设评审：把"支持"与"反证"摆在一起，给出一条可执行的意见。

评审**不替人下结论**。它只回答三个问题：这条假设有没有证据、证据强到什么程度、
支持与反证是否打架。意见的取值只有五种，与设计稿一致：

- `support`：有实验级支持、没有反证；
- `weaken`：只有较弱的反证，还不足以否掉；
- `revise`：支持与反证同时存在，主张本身要改；
- `defer`：证据不够，先提下一轮问题；
- `refute`：有实验级反证。

**只有实验级证据能改变假设状态。** 计算预测可以支持一条假设，但不能把它标成
"已被证实"——那是实验数据才能做的事。这条规则写死在 `_decide()` 里，不靠调用方
自觉。其余情况一律给 `inconclusive`（或保持 `open`），并写明缺什么。

评审结果同时给出建议状态与理由，是否写回由调用方决定（`apply()`）。分开的理由：
评审可以在"只看一眼"的场景下跑，写回是另一件事，两件事混在一起就没法只跑前一件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from vsagent.core.agent.research.evidence_registry import EXPERIMENTAL, LABELS, evidence
from vsagent.core.agent.research.research_objects import ResearchObject

VERDICTS: tuple[str, ...] = ("support", "weaken", "revise", "defer", "refute")

VERDICT_LABELS: dict[str, str] = {
    "support": "支持",
    "weaken": "削弱",
    "revise": "需要修正",
    "defer": "暂缓",
    "refute": "否定",
}


@dataclass(frozen=True)
class Review:
    """一条假设的评审意见。"""

    hypothesis_id: str
    verdict: str
    reasons: tuple[str, ...] = ()
    support: tuple[ResearchObject, ...] = ()
    refute: tuple[ResearchObject, ...] = ()
    strongest: str = ""
    # 建议写回的假设状态（`supported` / `refuted` / `inconclusive`）。
    # 空串表示"不要动"——证据不足时保持 open，比草率结案好。
    status: str = ""

    @property
    def label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)

    def line(self) -> str:
        bits = [f"{self.hypothesis_id}：{self.label}"]
        if self.strongest:
            bits.append(f"最强证据是{LABELS.get(self.strongest, self.strongest)}")
        if self.support or self.refute:
            bits.append(f"支持 {len(self.support)} 条、反证 {len(self.refute)} 条")
        text = "，".join(bits)
        if self.reasons:
            text += "。" + "；".join(self.reasons)
        return text


def _bare(hypothesis_id: str) -> str:
    """两种写法都认：`hyp#1`（会话任务状态里的编号）与 `hypothesis:<键>`（工作区
    里的对象 ID）。同一条假设在两个地方各有登记，评审时不该要求调用方先换算。"""
    text = str(hypothesis_id or "").strip()
    return text.split(":", 1)[-1] if text.startswith("hypothesis:") else text


def _points_to(refs: Iterable[str], hypothesis_id: str) -> bool:
    wanted = _bare(hypothesis_id)
    return any(_bare(ref) == wanted for ref in refs)


def review(
    workspace: object, hypothesis_id: str, *, objects: Iterable[ResearchObject] | None = None
) -> Review:
    """给一条假设出意见。`objects` 给了就用它，否则读整个工作区。"""
    wanted = str(hypothesis_id or "").strip()
    if not wanted:
        raise ValueError("评审需要假设编号")
    pool = list(objects) if objects is not None else workspace.find("evidence")
    mine = [
        obj
        for obj in pool
        if obj.kind == "evidence"
        and (_points_to(obj.supports, wanted) or _points_to(obj.refutes, wanted))
    ]
    support = tuple(obj for obj in mine if _points_to(obj.supports, wanted))
    refute = tuple(obj for obj in mine if _points_to(obj.refutes, wanted))
    verdict, reasons, status = _decide(support, refute)
    strongest = evidence(mine)[0].evidence_class if mine else ""
    return Review(
        hypothesis_id=wanted,
        verdict=verdict,
        reasons=tuple(reasons),
        support=support,
        refute=refute,
        strongest=strongest,
        status=status,
    )


def _decide(
    support: tuple[ResearchObject, ...], refute: tuple[ResearchObject, ...]
) -> tuple[str, list[str], str]:
    """按证据强度给意见。返回（意见, 理由, 建议状态）。"""
    strong_support = [obj for obj in support if obj.evidence_class in EXPERIMENTAL]
    strong_refute = [obj for obj in refute if obj.evidence_class in EXPERIMENTAL]

    if strong_refute:
        reasons = [
            f"有实验级反证：{obj.id}（{LABELS.get(obj.evidence_class, obj.evidence_class)}）"
            for obj in strong_refute[:3]
        ]
        if strong_support:
            reasons.append("同时也有实验级支持，两者冲突时以更强的证据为准，需要复核")
        return "refute", reasons, "refuted"

    if strong_support:
        reasons = [
            f"有实验级支持：{obj.id}（{LABELS.get(obj.evidence_class, obj.evidence_class)}）"
            for obj in strong_support[:3]
        ]
        if refute:
            reasons.append(
                f"另有 {len(refute)} 条较弱反证，未达到实验级，记录在案但不改变结论"
            )
        return "support", reasons, "supported"

    if not support and not refute:
        return (
            "defer",
            ["没有任何证据指向这条假设：先补证据，或者把它拆成可测的小问题"],
            "",
        )

    if support and refute:
        return (
            "revise",
            [
                "支持与反证都只有较弱证据（预测、记录或推断），互相不能排除",
                "主张需要收窄到能区分两者的那个条件上",
            ],
            "inconclusive",
        )

    if refute:
        return (
            "weaken",
            [
                f"只有较弱反证（最强的是{LABELS.get(refute[0].evidence_class, refute[0].evidence_class)}），"
                "还不足以否掉，但方向不利"
            ],
            "inconclusive",
        )

    return (
        "defer",
        [
            f"只有较弱支持（最强的是{LABELS.get(support[0].evidence_class, support[0].evidence_class)}），"
            "实验级数据才能把它标成已证实"
        ],
        "",
    )


def apply(state: object, result: Review) -> bool:
    """把评审意见写回会话的任务状态。返回是否真的改了。

    `state` 是 `TaskState`。找不到这条假设时返回 False，不新建——评审一条不存在的
    假设，多半是编号写错了，悄悄补一条只会让错误继续传下去。
    """
    if not result.status:
        return False
    for item in getattr(state, "hypotheses", []):
        if item.id != result.hypothesis_id:
            continue
        if item.status == result.status:
            return False
        item.status = result.status
        refs = [obj.id for obj in (*result.support, *result.refute)]
        if refs:
            item.result_ref = "、".join(refs[:5])
        return True
    return False
