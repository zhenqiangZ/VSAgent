"""数据库类与计算类工具的结果适配。

数据库工具（靶点、化合物、结构、口袋）给的是取值与编号，按「一条记录、
一段可核对文字」收成证据。计算工具（对接、亲和力、结构分析、ADMET）
的结果属于计算产物，单列成 `computational` 或 `model_prediction`。
"""

from __future__ import annotations

from typing import Any

from ..models import SearchEvidence
from .base import clip, meta_of, one

DATABASE_TOOLS = (
    "target_lookup",
    "search_actives", "search_analogs",
    "fetch_compound", "search_similar_compounds",
    "disease_targets", "check_druggability",
    "search_pdb", "fetch_structure", "fetch_alphafold", "assess_structure",
    "define_pocket", "detect_pockets", "compare_pockets", "similar_pockets",
)

COMPUTATIONAL_TOOLS = (
    "dock",
    "fit_affinity", "score_affinity",
    "analyze_pose", "analyze_hydrophobic", "contact_map", "alternative_poses",
    "efficiency_profile", "validate_docking", "rank_candidates",
    "rank_library", "pick_diverse",
    "build_pharmacophore",
    "enumerate_analogs", "matched_pairs", "scaffold_analysis",
)

# 预测类结果是模型给的判断，不能按实验事实评价。
PREDICTION_TOOLS = ("predict_admet",)


class DatabaseAdapter:
    names = DATABASE_TOOLS
    source_type = "database"

    def convert(self, part: Any) -> list[SearchEvidence]:
        meta = meta_of(part)
        title = str(meta.get("title") or meta.get("name") or meta.get("handle") or "")
        body = clip(str(getattr(part, "tool_output", "") or ""))
        return [
            one(
                part,
                source_type="database",
                source=str(getattr(part, "tool_name", "") or "数据库"),
                content=body,
                title=title,
                url=str(meta.get("url") or ""),
                metadata=meta,
            )
        ]


class ComputationalAdapter:
    names = COMPUTATIONAL_TOOLS
    source_type = "computational"

    def convert(self, part: Any) -> list[SearchEvidence]:
        return [
            one(
                part,
                source_type="computational",
                source=str(getattr(part, "tool_name", "") or "计算"),
                metadata=meta_of(part),
            )
        ]


class PredictionAdapter:
    names = PREDICTION_TOOLS
    source_type = "model_prediction"

    def convert(self, part: Any) -> list[SearchEvidence]:
        return [
            one(
                part,
                source_type="model_prediction",
                source=str(getattr(part, "tool_name", "") or "模型预测"),
                metadata=meta_of(part),
            )
        ]
