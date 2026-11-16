"""阶段：按当前任务规格判断「这一步按哪一份阶段规则做」。

五个阶段与设计稿一致：结构发现 → 口袋分析 → 配体分析 → 筛选执行 → 评价交付。
它们描述的是**计算流程走到哪一步**，不描述会话或战役的生死（那在
`campaign_state.py`）。

两条刻意的取舍：

- **阶段取自任务规格的子目标，不取自模型自述。** 模型说「我现在在选口袋」不算数；
  子目标里写着 `pocket_discovery` 才算。否则阶段规则会跟着模型的措辞漂移。
- **认不出的能力不猜。** 靶点情报与文献证据不属于这五步里的任何一步，
  它们没有阶段规则（返回空串），而不是硬塞进相邻的一步。
"""

from __future__ import annotations

from typing import Any

STAGES: tuple[str, ...] = (
    "structure_discovery",
    "pocket_analysis",
    "ligand_analysis",
    "screening",
    "evaluation",
)

CAPABILITY_STAGE: dict[str, str] = {
    "structure_discovery": "structure_discovery",
    "pocket_discovery": "pocket_analysis",
    "pocket_profiling": "pocket_analysis",
    "ligand_intelligence": "ligand_analysis",
    "receptor_preparation": "screening",
    "docking_run": "screening",
    "affinity_prediction": "evaluation",
    "scoring_evaluation": "evaluation",
    "mechanism_analysis": "evaluation",
    "hypothesis_validation": "evaluation",
}
"""能力 → 阶段。`target_intelligence` 与 `literature_evidence` 不在表里：
它们跨阶段，硬归到某一步会让那一步的规则多出一半不相干的内容。"""


def stage_for(session: Any) -> str:
    """这个会话现在处在哪个阶段。判断不出来时返回空串。

    取第一个能认出来的子目标：子目标按执行顺序排列，排在前面的就是当前这一步。
    """
    raw = getattr(session, "task_spec", None)
    if not isinstance(raw, dict):
        return ""
    for row in raw.get("subgoals") or []:
        if not isinstance(row, dict):
            continue
        stage = CAPABILITY_STAGE.get(str(row.get("capability") or "").strip())
        if stage:
            return stage
    return ""


def stage_label(stage: str) -> str:
    """阶段的中文名，给提示与日志用。"""
    return {
        "structure_discovery": "结构发现",
        "pocket_analysis": "口袋分析",
        "ligand_analysis": "配体分析",
        "screening": "筛选执行",
        "evaluation": "评价交付",
    }.get(stage, stage)
