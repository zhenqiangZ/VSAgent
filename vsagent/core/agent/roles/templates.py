"""角色模板：一个角色一份，写明它交什么、要谁确认、带哪些固定限制。

**这是模板数据的唯一定义处。** 字段名与 `TaskSpec` 一致，形状是冻结数据类：
编译器与投影都按属性读，按字典键读的那一套已经删掉。

子目标与验收条件由意图模型写，模板只保留不随用户改口变化的资产清单：
交付物、确认点与固定限制。`required_outputs` 是投影认领交付物的依据，
自动补出来的上游角色也从这里取交付物。
"""

from __future__ import annotations

from dataclasses import dataclass

STRUCTURE_APPROVAL = "受体结构确认"
POCKET_APPROVAL = "结合口袋确认"
PREPARE_APPROVAL = "受体准备确认"
DOCKING_APPROVAL = "对接与打分确认"
APPROVAL_ORDER: tuple[str, ...] = (
    STRUCTURE_APPROVAL,
    POCKET_APPROVAL,
    PREPARE_APPROVAL,
    DOCKING_APPROVAL,
)


@dataclass(frozen=True)
class RoleTemplate:
    """一个角色的模板。字段名与 `TaskSpec` 同名同类型。"""

    required_outputs: tuple[str, ...] = ()
    approval_points: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


TEMPLATES: dict[str, RoleTemplate] = {
    "research": RoleTemplate(required_outputs=("ResearchDossier",)),
    "pocket": RoleTemplate(
        required_outputs=("PocketAtlas",),
        approval_points=(STRUCTURE_APPROVAL, POCKET_APPROVAL),
    ),
    "ligand": RoleTemplate(required_outputs=("LigandDossier",)),
    "prepare": RoleTemplate(
        required_outputs=("PreparedReceptor", "PreparedLigandSet"),
        approval_points=(PREPARE_APPROVAL,),
    ),
    "docking": RoleTemplate(
        required_outputs=("CandidateSet",),
        approval_points=(DOCKING_APPROVAL,),
    ),
    "affinity": RoleTemplate(
        required_outputs=("AffinityModel", "AffinityPrediction"),
        approval_points=(DOCKING_APPROVAL,),
    ),
    "analysis": RoleTemplate(required_outputs=("ScoringReport",)),
    "mechanism": RoleTemplate(required_outputs=("MechanismReport",)),
    "visualization": RoleTemplate(required_outputs=("ViewPage",)),
}
"""角色到模板的映射。每个角色一份。"""

GENERIC_TEMPLATE = RoleTemplate(required_outputs=("TaskReport",))
"""没有专用模板的角色用这一份。"""


def template_for(role: str) -> RoleTemplate:
    """认得的角色给它的模板，认不得的给通用模板。模板的唯一读取入口。"""
    return TEMPLATES.get(role, GENERIC_TEMPLATE)
