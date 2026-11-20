"""阶段：按当前任务规格判断「这一步按哪一份阶段规则做」。

五个阶段与设计稿一致：结构发现 → 口袋分析 → 配体分析 → 筛选执行 → 评价交付。
它们描述的是**计算流程走到哪一步**，不描述会话或战役的生死。

两条刻意的取舍：

- **阶段取自任务规格的子目标，不取自模型自述。** 模型说「我现在在选口袋」不算数；
  子目标里写着 `stage = pocket_analysis` 才算。
- **没有阶段就不加载阶段规则。** 靶点情报与视图展示不属于这五步，返回空串，
  不硬塞进相邻的一步。
"""

from __future__ import annotations

from typing import Any

from vsagent.core.agent.roles.catalog import STAGES

STAGE_LABELS: dict[str, str] = {
    "structure_discovery": "结构发现",
    "pocket_analysis": "口袋分析",
    "ligand_analysis": "配体分析",
    "screening": "筛选执行",
    "evaluation": "评价交付",
}


def stage_for(session: Any) -> str:
    """这个会话现在处在哪个阶段。判断不出来时返回空串。

    取第一个带阶段的子目标：子目标按执行顺序排列，排在前面的就是当前这一步。
    """
    raw = getattr(session, "task_spec", None)
    if not isinstance(raw, dict):
        return ""
    for row in raw.get("subgoals") or []:
        if not isinstance(row, dict):
            continue
        stage = str(row.get("stage") or "").strip()
        if stage in STAGES:
            return stage
    return ""


def stage_label(stage: str) -> str:
    """阶段的中文名，给提示与日志用。"""
    return STAGE_LABELS.get(stage, stage)
