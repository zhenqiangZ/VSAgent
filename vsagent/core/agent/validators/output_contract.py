"""输出合同：每种交付物**怎么验**。

模板里的交付物是逻辑名（`PocketCatalog`、`CandidateSet`），登记表里存的是句柄
（`ligandset#1`、`artifact#2`），磁盘上是运行目录。两者之间没有天然对应关系——
这一层就是把对应关系写下来，免得交付校验变成猜名字。

三种验法，对应三种真实情况：

- `handle`：登记表里有没有这一类条目（配体集、受体、产物）；
- `path`：工作区里有没有这个目录或文件；
- `manual`：**无法机械验证**——报告类交付物就是这一类。把它标成 manual 而不是
  编一个"回答非空"的假判据，是因为假判据会让交付校验永远通过，等于没有。

`manual` 不是失败：它明确交给评审 Agent 或人，这与 `AGENTS.md` 里"科学结论由
专业判断"的分工一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Finding

NAME = "output_contract"
MANUAL = "manual"


@dataclass(frozen=True)
class Contract:
    output: str
    kind: str
    target: str = ""
    note: str = ""
    # `handle` 的细分类别：登记条目的 `meta["kind"]`。结构全景写 `survey`、
    # 口袋集写 `pockets`；为空时只按句柄类别（receptor/ligandset/artifact）判。
    meta_kind: str = ""


CONTRACTS: dict[str, Contract] = {
    "TargetBrief": Contract("TargetBrief", MANUAL, note="靶点简报是文字结论，由评审判断"),
    "StructureCatalog": Contract(
        "StructureCatalog",
        "handle",
        "artifact",
        "结构全景是 artifact（kind=survey）",
        meta_kind="survey",
    ),
    "PocketCatalog": Contract(
        "PocketCatalog",
        "handle",
        "artifact",
        "口袋集是 artifact（kind=pockets）",
        meta_kind="pockets",
    ),
    "PocketProfile": Contract("PocketProfile", MANUAL, note="口袋画像由评审判断"),
    "LigandCatalog": Contract(
        "LigandCatalog", "handle", "ligandset", "配体清单要有一份配体集"
    ),
    "PreparedReceptor": Contract(
        "PreparedReceptor", "handle", "receptor", note="准备后的受体仍登记成受体"
    ),
    "PreparedLigandSet": Contract(
        "PreparedLigandSet", "handle", "ligandset", note="准备后的配体集仍是配体集"
    ),
    "CandidateSet": Contract("CandidateSet", "handle", "ligandset", note="候选集应当是配体集"),
    "AffinityModel": Contract(
        "AffinityModel",
        "handle",
        "artifact",
        "微调产出的模型是 artifact（kind=model）",
        meta_kind="model",
    ),
    "AffinityPrediction": Contract(
        "AffinityPrediction",
        "handle",
        "artifact",
        "亲和力预测表是 artifact（kind=affinity）",
        meta_kind="affinity",
    ),
    "ScoringReport": Contract("ScoringReport", "path", "runs", note="评分报告写在运行目录下"),
    "MechanismReport": Contract("MechanismReport", MANUAL, note="机制解释由评审判断"),
    "EvidenceReport": Contract("EvidenceReport", MANUAL, note="证据整理由评审判断"),
    "HypothesisReview": Contract("HypothesisReview", MANUAL, note="假设评审必须由人做"),
    "TaskReport": Contract("TaskReport", MANUAL, note="通用模板的交付物"),
}


def contract_for(output: str) -> Contract:
    return CONTRACTS.get(
        output,
        Contract(output, MANUAL, note="没有登记合同，按无法机械验证处理"),
    )


def unverifiable(outputs: list[str]) -> list[str]:
    """这些交付物只能人工或评审判断。交付校验会用它们说明"没验"而不是"验过了"。"""
    return [name for name in outputs if contract_for(name).kind == MANUAL]


def verify(output: str, *, ledger: Any = None, workspace: Path | None = None) -> Finding | None:
    """验一个交付物。返回发现或 None；`manual` 一律返回 None（不算失败）。"""
    contract = contract_for(output)
    if contract.kind == MANUAL:
        return None
    if contract.kind == "handle":
        if ledger is None:
            return Finding(
                validator=NAME,
                code="no_ledger",
                message=f"{output} 要验登记表，但这次没有登记表可用",
                severity="warn",
            )
        try:
            entries = ledger.entries()
        except Exception as error:
            return Finding(
                validator=NAME,
                code="ledger_unreadable",
                message=f"{output} 读登记表失败：{error}",
                severity="warn",
            )
        hit = [entry for entry in entries if getattr(entry, "kind", "") == contract.target]
        if contract.meta_kind:
            hit = [
                entry
                for entry in hit
                if str((getattr(entry, "meta", None) or {}).get("kind") or "")
                == contract.meta_kind
            ]
        if hit:
            return None
        wanted = contract.target
        if contract.meta_kind:
            wanted += f"（kind={contract.meta_kind}）"
        return Finding(
            validator=NAME,
            code="missing_handle",
            message=f"{output} 要求登记表里有 {wanted}，实际一条也没有",
            severity="warn",
        )
    if contract.kind == "path":
        if workspace is None:
            return Finding(
                validator=NAME,
                code="no_workspace",
                message=f"{output} 要验目录，但没有工作区可用",
                severity="warn",
            )
        if (workspace / contract.target).exists():
            return None
        return Finding(
            validator=NAME,
            code="missing_path",
            message=f"{output} 要求 {contract.target} 存在，实际没有",
            severity="warn",
        )
    return Finding(
        validator=NAME,
        code="bad_contract",
        message=f"{output} 的合同写法不认识：{contract.kind}",
        severity="warn",
    )
