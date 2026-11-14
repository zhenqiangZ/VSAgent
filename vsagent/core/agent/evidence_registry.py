"""证据分级：把"怎么知道的"与"知道什么"分开记。

同一个结论可以由四种东西支撑：实验测出来的、数据库里记着的、文献里写的、
模型算出来的。它们在报告里必须能分开——把预测写成实验事实，是这个项目最不能
犯的错，而它恰恰是最容易犯的：两者在文字上一模一样，只在来源上不同。

所以证据对象必带 `evidence_class`，取值只有七种，且**不能事后改等级**：一条证据
登记成 `prediction` 之后，不能再按 `experimental_activity` 记一遍。要更正就换一个
身份键，这样"什么时候改的口径"在对象表里留得下痕迹。

`check()` 只回答一件事：**这个结论的说法，证据撑不撑得住**。它不看文字里有没有
"实验"两个字——那种判断不可靠，还会把"实验证明"这种引述别人的句子误伤。调用方
声明这句话是什么性质（`claim_kind`），这里核对证据等级够不够。
"""

from __future__ import annotations

from typing import Any, Iterable

from .research_objects import EVIDENCE_LABELS as LABELS
from .research_objects import ObjectError, ResearchObject

CLASSES: tuple[str, ...] = (
    "experimental_activity",   # 实测活性（IC50 / Ki / 结合常数）
    "experimental_structure",  # 实验结构（晶体、冷冻电镜、NMR）
    "database",                # 数据库记录（ChEMBL、UniProt、PDB 元数据）
    "literature",              # 文献结论（摘要或正文里的判断）
    "user_data",               # 用户提供的数据
    "prediction",              # 计算预测（对接分数、DTA、口袋预测）
    "inference",               # 模型推断（没有计算支撑的推理）
)
"""从强到弱。顺序有意义：`grade()` 取第一条命中的。"""

# 中文名与对象摘要共用 `research_objects.EVIDENCE_LABELS`（上面按 `LABELS` 导入），
# 避免两处各写一套、慢慢对不上。

EXPERIMENTAL: frozenset[str] = frozenset({"experimental_activity", "experimental_structure"})
"""能被称为"实验"的两种。其余都不是，包括数据库里抄来的活性值——
那是别人测的，对我们来说是**记录**，引用时要写明出处，不能当成自己的实验结果。"""

CLAIM_KINDS: tuple[str, ...] = ("experimental", "observational", "prediction", "inference")
"""一句话的性质。`observational` 是"数据库/文献里记着"，比实验弱、比预测强。"""


def check_class(evidence_class: str) -> str:
    name = str(evidence_class or "").strip()
    if name not in CLASSES:
        raise ObjectError(
            f"不认识的证据类别：{name or '（空）'}。可用：{'、'.join(CLASSES)}"
        )
    return name


def add(
    workspace: Any,
    *,
    evidence_class: str,
    key: str,
    source: str,
    label: str = "",
    refs: Iterable[str] = (),
    supports: Iterable[str] = (),
    refutes: Iterable[str] = (),
    task_id: str = "",
    agent_role: str = "",
) -> ResearchObject:
    """登记一条证据，并记下它支持 / 反驳了哪些假设。

    身份键由调用方给：一条文献证据用它自己的编号（DOI、PMID），一次实测活性用
    "分子@靶点@测定方式"。同一份证据重复登记会合并引用，不会变成两条。
    """
    check_class(evidence_class)
    return workspace.register(
        "evidence",
        key,
        label=label,
        source=source,
        refs=tuple(refs),
        evidence_class=evidence_class,
        supports=tuple(str(item) for item in supports if str(item)),
        refutes=tuple(str(item) for item in refutes if str(item)),
        task_id=task_id,
        agent_role=agent_role,
    )


def evidence(objects: Iterable[ResearchObject]) -> list[ResearchObject]:
    """从一组对象里挑出证据，按类别从强到弱、再按登记顺序。"""
    items = [obj for obj in objects if obj.kind == "evidence"]
    order = {name: index for index, name in enumerate(CLASSES)}
    return sorted(items, key=lambda obj: order.get(obj.evidence_class, len(CLASSES)))


def grade(objects: Iterable[ResearchObject]) -> str:
    """这组证据里最强的那一类。没有证据时返回空串。"""
    items = evidence(objects)
    return items[0].evidence_class if items else ""


def check(claim_kind: str, objects: Iterable[ResearchObject]) -> list[str]:
    """这个说法，证据撑不撑得住。返回问题清单，空列表表示撑得住。"""
    if claim_kind not in CLAIM_KINDS:
        raise ObjectError(
            f"不认识的说法类别：{claim_kind}。可用：{'、'.join(CLAIM_KINDS)}"
        )
    items = evidence(objects)
    if not items:
        return ["这个结论没有挂任何证据：先把依据登记成证据对象，再下结论"]
    strongest = items[0].evidence_class
    if claim_kind == "experimental" and strongest not in EXPERIMENTAL:
        return [
            f"结论按实验事实陈述，但引用的证据里没有实验数据："
            f"最强的一条是{LABELS.get(strongest, strongest)}（{strongest}）。"
            "要么改成按预测/文献陈述，要么补上实测数据"
        ]
    if claim_kind == "observational" and strongest in ("prediction", "inference"):
        return [
            f"结论按已有记录陈述，但证据只有{LABELS.get(strongest, strongest)}："
            "记录类说法要有数据库或文献来源"
        ]
    return []


def summary(objects: Iterable[ResearchObject]) -> str:
    """一句话说明证据构成，给报告与假设评审用。"""
    items = evidence(objects)
    if not items:
        return "没有证据"
    counts: dict[str, int] = {}
    for obj in items:
        counts[obj.evidence_class] = counts.get(obj.evidence_class, 0) + 1
    parts = [f"{LABELS.get(name, name)} {counts[name]} 条" for name in CLASSES if name in counts]
    strongest = items[0].evidence_class
    return f"{'、'.join(parts)}；最强的是{LABELS.get(strongest, strongest)}"
