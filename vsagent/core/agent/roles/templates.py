"""角色模板：一个角色一份，写明它做哪几步、交什么、怎么验收、要谁确认。

**这是模板数据的唯一定义处。** 字段名与 `TaskSpec` 对齐，形状是冻结数据类：
编译器与投影都按属性读，按字典键读的那一套已经删掉。

一段模板内部可以有多步；步骤只决定阶段规则与确认点，不单独派角色。
`acceptance_criteria` 的 `output` 指向 `required_outputs` 里的名字，
`TaskSpec.uncovered_outputs()` 按它判覆盖。
`constraints` 是这个角色的固定约束，只进它自己的任务书；临时约束写在任务
规格里，用「角色名：要求」的前缀指定收件角色，不带前缀的发给所有角色。
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
class Subgoal:
    """模板里的一步。`stage` 为空表示这一步不挂科研阶段。"""

    stage: str
    goal: str


@dataclass(frozen=True)
class Criterion:
    """一条验收条件。`output` 指向它管的交付物。"""

    kind: str
    target: str
    output: str


@dataclass(frozen=True)
class RoleTemplate:
    """一个角色的模板。字段名与 `TaskSpec` 同名同类型。"""

    subgoals: tuple[Subgoal, ...] = ()
    required_outputs: tuple[str, ...] = ()
    acceptance_criteria: tuple[Criterion, ...] = ()
    approval_points: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


TEMPLATES: dict[str, RoleTemplate] = {
    "research": RoleTemplate(
        subgoals=(
            Subgoal("", "查清靶点的基本信息、可成药性与临床证据，并整理文献与本机语料证据"),
        ),
        required_outputs=("ResearchDossier",),
        acceptance_criteria=(
            Criterion("evidence_ref", "靶点身份、可成药性与临床结论的证据来源", "ResearchDossier"),
        ),
    ),
    "pocket": RoleTemplate(
        subgoals=(
            Subgoal("structure_discovery", "获取符合范围的实验结构并建立结构目录"),
            Subgoal("pocket_analysis", "识别候选结合口袋并建立口袋目录"),
            Subgoal("pocket_analysis", "给每个口袋做结构画像与适配特征分析"),
        ),
        required_outputs=("PocketAtlas",),
        acceptance_criteria=(
            Criterion("artifact_exists", "PocketAtlas", "PocketAtlas"),
            Criterion("evidence_ref", "结构来源、口袋残基与画像依据", "PocketAtlas"),
            Criterion("review_required", "口袋选择与画像的科学合理性", "PocketAtlas"),
        ),
        approval_points=(STRUCTURE_APPROVAL, POCKET_APPROVAL),
    ),
    "ligand": RoleTemplate(
        subgoals=(
            Subgoal("ligand_analysis", "汇总已知配体、活性、骨架分布与化学空间"),
        ),
        required_outputs=("LigandDossier",),
        acceptance_criteria=(
            Criterion("handle_registered", "LigandDossier", "LigandDossier"),
            Criterion("evidence_ref", "每条活性的来源与测定条件", "LigandDossier"),
        ),
    ),
    "prepare": RoleTemplate(
        subgoals=(
            Subgoal("screening", "把受体准备成可以直接对接的产物"),
            Subgoal("screening", "把配体集清理并准备成可以直接对接的产物"),
        ),
        required_outputs=("PreparedReceptor", "PreparedLigandSet"),
        acceptance_criteria=(
            Criterion("handle_registered", "PreparedReceptor", "PreparedReceptor"),
            Criterion("artifact_exists", "PreparedLigandSet", "PreparedLigandSet"),
            Criterion("approval_recorded", "受体准备与配体准备已确认", "PreparedReceptor"),
        ),
        approval_points=(PREPARE_APPROVAL,),
    ),
    "docking": RoleTemplate(
        subgoals=(
            Subgoal("screening", "按确认的口袋与参数执行对接，产出带分数的位姿表"),
        ),
        required_outputs=("CandidateSet",),
        acceptance_criteria=(
            Criterion("handle_registered", "CandidateSet", "CandidateSet"),
            Criterion("artifact_exists", "CandidateSet", "CandidateSet"),
            Criterion("approval_recorded", "对接参数已确认", "CandidateSet"),
        ),
        approval_points=(DOCKING_APPROVAL,),
    ),
    "affinity": RoleTemplate(
        subgoals=(
            Subgoal("evaluation", "用该靶点的实测活性微调模型，并给出亲和力预测"),
        ),
        required_outputs=("AffinityModel", "AffinityPrediction"),
        acceptance_criteria=(
            Criterion("artifact_exists", "AffinityModel", "AffinityModel"),
            Criterion("artifact_exists", "AffinityPrediction", "AffinityPrediction"),
            Criterion("evidence_ref", "每条预测所用的模型与输入配体", "AffinityPrediction"),
        ),
        approval_points=(DOCKING_APPROVAL,),
    ),
    "analysis": RoleTemplate(
        subgoals=(
            Subgoal("evaluation", "对已有计算产物做独立评分、整合与一致性检查"),
        ),
        required_outputs=("ScoringReport",),
        acceptance_criteria=(
            Criterion("artifact_exists", "ScoringReport", "ScoringReport"),
            Criterion("evidence_ref", "每条评分的输入产物", "ScoringReport"),
            Criterion("review_required", "评分方法与适用性是否成立", "ScoringReport"),
        ),
    ),
    "mechanism": RoleTemplate(
        subgoals=(
            Subgoal("evaluation", "解释候选分子的结合机制与关键残基，并整理支持与反证"),
        ),
        required_outputs=("MechanismReport",),
        acceptance_criteria=(
            Criterion("artifact_exists", "MechanismReport", "MechanismReport"),
            Criterion("evidence_ref", "机制判断依据的位姿或文献", "MechanismReport"),
            Criterion("review_required", "机制解释是否被证据支持", "MechanismReport"),
        ),
    ),
    "visualization": RoleTemplate(
        subgoals=(
            Subgoal("", "把已登记的结构、口袋、位姿与候选表做成视图页"),
        ),
        required_outputs=("ViewPage",),
        acceptance_criteria=(
            Criterion("artifact_exists", "ViewPage", "ViewPage"),
            Criterion("evidence_ref", "视图页引用的对象或产物", "ViewPage"),
        ),
    ),
}
"""角色到模板的映射。每个角色一份，模板内部可以有多步。"""

GENERIC_TEMPLATE = RoleTemplate(
    subgoals=(Subgoal("", "按用户要求完成这件事，并给出可追溯的结论"),),
    required_outputs=("TaskReport",),
    acceptance_criteria=(
        Criterion("artifact_exists", "TaskReport", "TaskReport"),
        Criterion("review_required", "结论是否回答了用户的问题", "TaskReport"),
    ),
)
"""没有专用模板的角色用这一份。"""


def template_for(role: str) -> RoleTemplate:
    """认得的角色给它的模板，认不得的给通用模板。模板的唯一读取入口。"""
    return TEMPLATES.get(role, GENERIC_TEMPLATE)
