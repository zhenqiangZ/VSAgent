"""Task Specification Compiler：把 `IntentSpec` 编译成 `TaskSpec`。

**纯规则，不调用模型。** 理由不是"规则更聪明"，而是三条可验证的：

1. 意图类型只有十四种，且是本项目自己定义的，不存在"没写过的类型"这种常态；
   模板数量可控。
2. `TaskSpec` 的验收条件必须机器可查（Phase 8 的交付校验按它判）。模板天然产出
   `kind` + `target`，模型天然产出句子。
3. 合同要稳定：同一份意图编译两次必须完全一致。模型做不到这一点，而它是后续
   路由、派发、交付校验共同依据的那份文件。

未覆盖的意图**不静默降级**：用一份最小的通用模板，记一条 `uncovered_intent`
警告，让调用方与 trace 都看得见。等这类任务多到值得处理，再补模板或改路线。

排除项的处理有两条：原样继承；以及**删掉与它冲突的模板**——说了"不要对接"，
计划里就不该有筛选这一段。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .contracts import IntentSpec, TaskSpec

# 四个确认点，名称与 `AGENTS.md` §5 一致。顺序也是渲染顺序，不要按集合去重后排序，
# 要按这里的固定顺序输出，否则同一份输入两次编译的文本会不同。
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

EVIDENCE_REQUIREMENT = "每条结论都要能追溯到结构、文献或计算产物（handle、编号或路径）"

# 一个意图类型 = 一个子目标 + 若干交付物 + 每条交付物的验收条件。
# `checks` 的第三项是它覆盖的交付物名：`TaskSpec.uncovered_outputs()` 按它判覆盖。
TEMPLATES: dict[str, dict[str, object]] = {
    "target_inquiry": {
        "capability": "target_intelligence",
        "subgoal": "查清靶点的基本信息、可成药性与临床证据",
        "outputs": ("TargetBrief",),
        "checks": (
            ("evidence_ref", "靶点身份与可成药性结论的证据来源", "TargetBrief"),
        ),
        "approvals": (),
    },
    "structure_discovery": {
        "capability": "structure_discovery",
        "subgoal": "获取符合范围的实验结构并建立结构目录",
        "outputs": ("StructureCatalog",),
        "checks": (
            ("handle_registered", "StructureCatalog", "StructureCatalog"),
            ("evidence_ref", "每套结构的来源与质量信息", "StructureCatalog"),
        ),
        "approvals": (STRUCTURE_APPROVAL,),
    },
    "pocket_discovery": {
        "capability": "pocket_discovery",
        "subgoal": "识别候选结合口袋并建立口袋目录",
        "outputs": ("PocketCatalog",),
        "checks": (
            ("handle_registered", "PocketCatalog", "PocketCatalog"),
            ("evidence_ref", "每个口袋的结构来源与残基", "PocketCatalog"),
        ),
        "approvals": (STRUCTURE_APPROVAL, POCKET_APPROVAL),
    },
    "pocket_profiling": {
        "capability": "pocket_profiling",
        "subgoal": "给每个口袋做结构画像与适配特征分析",
        "outputs": ("PocketProfile",),
        "checks": (
            ("artifact_exists", "PocketProfile", "PocketProfile"),
            ("review_required", "口袋画像的科学合理性", "PocketProfile"),
        ),
        "approvals": (POCKET_APPROVAL,),
    },
    "ligand_discovery": {
        "capability": "ligand_intelligence",
        "subgoal": "汇总已知配体、活性与骨架分布",
        "outputs": ("LigandCatalog",),
        "checks": (
            ("handle_registered", "LigandCatalog", "LigandCatalog"),
            ("evidence_ref", "每条活性的来源与测定条件", "LigandCatalog"),
        ),
        "approvals": (),
    },
    "virtual_screening": {
        "capability": "virtual_screening",
        "subgoal": "按确认的受体与口袋执行筛选并给出候选集",
        "outputs": ("CandidateSet",),
        "checks": (
            ("handle_registered", "CandidateSet", "CandidateSet"),
            ("artifact_exists", "CandidateSet", "CandidateSet"),
            ("approval_recorded", "受体准备与对接参数已确认", "CandidateSet"),
        ),
        "approvals": (PREPARE_APPROVAL, DOCKING_APPROVAL),
    },
    "scoring_evaluation": {
        "capability": "scoring_evaluation",
        "subgoal": "对已有计算产物做独立评分与一致性检查",
        "outputs": ("ScoringReport",),
        "checks": (
            ("artifact_exists", "ScoringReport", "ScoringReport"),
            ("evidence_ref", "每条评分的输入产物", "ScoringReport"),
            ("review_required", "评分方法与适用性是否成立", "ScoringReport"),
        ),
        "approvals": (),
    },
    "mechanism_analysis": {
        "capability": "mechanism_analysis",
        "subgoal": "解释候选分子的结合机制与关键残基",
        "outputs": ("MechanismReport",),
        "checks": (
            ("artifact_exists", "MechanismReport", "MechanismReport"),
            ("evidence_ref", "机制判断依据的位姿或文献", "MechanismReport"),
            ("review_required", "机制解释是否被证据支持", "MechanismReport"),
        ),
        "approvals": (),
    },
    "literature_evidence": {
        "capability": "literature_evidence",
        "subgoal": "检索并整理相关文献证据",
        "outputs": ("EvidenceReport",),
        "checks": (("evidence_ref", "每条结论的文献出处", "EvidenceReport"),),
        "approvals": (),
    },
    "hypothesis_validation": {
        "capability": "hypothesis_validation",
        "subgoal": "汇总支持与反证，更新假设状态",
        "outputs": ("HypothesisReview",),
        "checks": (
            ("evidence_ref", "每条假设的支持与反证来源", "HypothesisReview"),
            ("review_required", "假设是否被证据支持或推翻", "HypothesisReview"),
        ),
        "approvals": (),
    },
}
"""意图类型到模板的映射。`multi_objective` 是标记，不单独占一段。"""

# 用户说"不要 X"时，删掉与之冲突的模板。只写会真正冲突的：
# 说了不要对接，就不该有筛选；说了只要结构，也不该有筛选。
EXCLUSION_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("对接", "分子对接"), ("virtual_screening",)),
    (("筛选", "虚拟筛选"), ("virtual_screening",)),
)

GENERIC_TEMPLATE: dict[str, object] = {
    "subgoal": "按用户要求完成这件事，并给出可追溯的结论",
    "outputs": ("TaskReport",),
    "checks": (
        ("artifact_exists", "TaskReport", "TaskReport"),
        ("review_required", "结论是否回答了用户的问题", "TaskReport"),
    ),
    "approvals": (),
}

MINIMAL_TASK_ID_LENGTH = 8


class CompileError(RuntimeError):
    """这份意图不该被编译成任务。"""


@dataclass(frozen=True)
class Compilation:
    """编译结果：任务本身，加上"哪些意图没覆盖"这类过程信息。"""

    task: TaskSpec
    warnings: tuple[str, ...] = ()
    covered: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()


def default_task_id(intent: IntentSpec) -> str:
    """由意图内容派生任务号，保证同一份意图两次编译得到同一个号。"""
    body = json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
    return f"task_{digest[:MINIMAL_TASK_ID_LENGTH]}"


class TaskCompiler:
    """把意图编译成任务合同。无状态，可重复调用。"""

    def compile(
        self,
        intent: IntentSpec,
        *,
        current: TaskSpec | None = None,
        task_id: str = "",
        campaign_id: str = "",
    ) -> Compilation:
        if intent.needs_clarification:
            raise CompileError("意图还没解析完（needs_clarification），先问用户再编译")
        if intent.turn_mode == "general_chat":
            raise CompileError("闲聊不编译任务")
        if intent.turn_mode == "task_continuation" or "task_continuation" in intent.intent_types:
            raise CompileError("任务延续不该走编译：沿用当前任务规格即可")

        kinds, dropped = self._kinds(intent)
        warnings: list[str] = []
        if dropped:
            warnings.append(f"这些意图与用户的禁止项冲突，已从计划里去掉：{'、'.join(dropped)}")

        covered = [kind for kind in kinds if kind in TEMPLATES]
        uncovered = [kind for kind in kinds if kind not in TEMPLATES and kind != "multi_objective"]
        if not covered:
            warnings.append("没有专用模板可编译，退回通用模板")
        elif uncovered:
            warnings.append(
                f"这些意图类型没有专用模板，未纳入计划：{'、'.join(uncovered)}"
            )

        subgoals, outputs, checks, approvals = self._merge(covered, intent)
        objective = intent.primary_goal
        constraints = list(intent.explicit_constraints)
        exclusions = list(intent.exclusions)
        revision = 0
        status = "ready"
        identifier = task_id or default_task_id(intent)
        campaign = campaign_id

        if current is not None and intent.turn_mode == "modify_task":
            identifier = current.task_id
            campaign = current.campaign_id or campaign
            revision = current.revision + 1
            # 改目标不等于重新开工：正在跑或卡住的任务保留状态，完成过的才回到 ready。
            status = current.status if current.status in ("running", "blocked") else "ready"
            constraints = list(current.constraints) + constraints
            exclusions = list(current.exclusions) + exclusions

        task = TaskSpec(
            task_id=identifier,
            campaign_id=campaign,
            objective=objective,
            research_scope=dict(intent.research_objects),
            subgoals=subgoals,
            constraints=constraints,
            exclusions=exclusions,
            available_inputs=dict(intent.provided_inputs),
            required_outputs=outputs,
            evidence_requirements=[{"name": EVIDENCE_REQUIREMENT}],
            acceptance_criteria=checks,
            approval_points=approvals,
            status=status,
            revision=revision,
        )
        return Compilation(task=task, warnings=tuple(warnings), covered=tuple(covered), uncovered=tuple(uncovered))

    @staticmethod
    def _kinds(intent: IntentSpec) -> tuple[list[str], list[str]]:
        """按模板表的固定顺序取意图类型，并去掉与禁止项冲突的那些。"""
        excluded: set[str] = set()
        for words, kinds in EXCLUSION_RULES:
            if any(word in item for item in intent.exclusions for word in words):
                excluded.update(kinds)
        ordered = [kind for kind in TEMPLATES if kind in intent.intent_types]
        kept = [kind for kind in ordered if kind not in excluded]
        dropped = [kind for kind in ordered if kind in excluded]
        return kept, dropped

    @staticmethod
    def _merge(
        kinds: list[str], intent: IntentSpec
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, str]]]:
        """合并模板：子目标按文本去重，交付物按名字去重，验收条件按 (kind,target) 去重。

        子目标上带 `capability` 标签：Phase 6 的路由按它查能力表，不做文字匹配。
        模板是唯一知道"这条子目标是什么"的地方，让它在生成时顺手写下来。
        """
        subgoals: list[dict[str, object]] = []
        outputs: list[dict[str, object]] = []
        checks: list[dict[str, object]] = []
        approvals: list[dict[str, str]] = []
        seen_subgoals: set[str] = set()
        seen_outputs: set[str] = set()
        seen_checks: set[tuple[str, str]] = set()
        seen_approvals: set[str] = set()

        if "multi_objective" in intent.intent_types:
            text = "把复合目标拆成可并行的子任务，并说明各自依赖"
            subgoals.append({"goal": text})
            seen_subgoals.add(text)

        if not kinds:
            plans = [("", GENERIC_TEMPLATE)]
        else:
            plans = [(kind, TEMPLATES[kind]) for kind in kinds]
        for kind, item in plans:
            goal = str(item["subgoal"])
            if goal not in seen_subgoals:
                row: dict[str, object] = {"goal": goal}
                # 能力名由模板给，不拿意图类型顶替：`ligand_discovery` 对应的能力
                # 叫 `ligand_intelligence`，两者不是同一套命名。
                capability = str(item.get("capability") or "")
                if capability:
                    row["capability"] = capability
                subgoals.append(row)
                seen_subgoals.add(goal)
            for name in item["outputs"]:  # type: ignore[union-attr]
                if name not in seen_outputs:
                    outputs.append({"name": name})
                    seen_outputs.add(name)
            for check_kind, target, output in item["checks"]:  # type: ignore[union-attr]
                key = (check_kind, target)
                if key in seen_checks:
                    continue
                seen_checks.add(key)
                checks.append({"kind": check_kind, "target": target, "output": output})
            for name in item["approvals"]:  # type: ignore[union-attr]
                if name not in seen_approvals:
                    approvals.append({"name": name})
                    seen_approvals.add(name)

        # 确认点按固定顺序输出，不按模板顺序：两份不同的模板组合可能以不同顺序
        # 触到同一个确认点，顺序一变，编译结果就不再字节稳定。
        approvals.sort(key=lambda item: APPROVAL_ORDER.index(item["name"]) if item["name"] in APPROVAL_ORDER else 99)
        return subgoals, outputs, checks, approvals
