"""科研工作区的四个工具：登记对象、加证据、看索引、评审假设。

为什么要有这一族，而不是让模型继续在聊天里"记住"：

- **同一结构在不同 Agent 之间要能用同一个 ID 找到。** ID 由身份决定
  （`structure:4HJO`），不是自增号——两个 Agent 各从 1 开始数，得到两个不同的号，
  说的却是同一个晶体结构。
- **证据要能分级。** 把预测写成实验事实是这个项目最不能犯的错，而两者在文字上
  一模一样，只在来源上不同。所以证据登记时必须写明等级，且登记之后不能改。
- **假设的状态只能由证据改。** 评审工具按证据强度给意见，模型不能凭印象把一条
  假设标成"已证实"。

这一族**按根会话共享**：工作区实例由 `AgentHost.workspace_for()` 持有，子 Agent
登记的对象父会话看得见，反过来也一样。工具只调它的方法，不认它的类型，
所以这一层不必导入 host。

**四个工具都不常驻。** 它们只在"整理证据、写结论"那几步用得上，常驻会一直占着
每一步的额度；按族装入的代价是模型多花一步 `load_tools`。
"""

from __future__ import annotations

from typing import Any

from ..evidence_registry import CLASSES, EXPERIMENTAL, LABELS
from ..evidence_registry import add as add_evidence
from ..hypothesis_review import VERDICT_LABELS
from ..hypothesis_review import apply as apply_review
from ..hypothesis_review import review
from ..research_objects import KINDS, KIND_LABELS, ObjectError, make_id
from ..tools import Tool, ToolResult, ToolSpec

__all__ = [
    "AddEvidenceTool",
    "ListObjectsTool",
    "RegisterObjectTool",
    "ReviewHypothesisTool",
    "domain_tools",
]

OBJECT_KINDS: tuple[str, ...] = tuple(kind for kind in KINDS if kind != "evidence")
"""登记对象能用的类别。证据单列，因为它还要定等级、记支持与反驳。"""

# 这几类必须有出处。实测结果与用户数据是"别人做过的实验"，没有数据库编号、
# 文献编号、产物 handle 或数据文件路径，就没法复核——而不带出处的"实测结果"
# 与模型自己编的预测在文字上没有区别。
NEEDS_REF: frozenset[str] = frozenset(EXPERIMENTAL | {"user_data"})


def _save(workspace: Any) -> str:
    """落盘，失败时给一句说明。失败不抛：这一刻的登记已经成立。"""
    if workspace.save() is None:
        return "\n写盘失败：对象只在内存里，重启之后会丢。"
    return ""


class RegisterObjectTool(Tool):
    """登记一个研究对象（结构、口袋、配体、靶点、位姿、评分、假设、报告）。"""

    spec = ToolSpec(
        name="register_object",
        description=(
            "在科研工作区登记一个**研究对象**：靶点、结构、口袋、配体、位姿、评分、"
            "假设或报告。ID 由类别与身份键拼成（structure:4HJO、pocket:receptor#1:p1），"
            "同一个身份键再登记一次是**更新**：追加引用、补空字段、版本加一，不会变成两条。\n"
            "大文件不要放进来：坐标与对接结果留在 runs/ 下，这里只记 handle 或路径。"
            "证据不要用这个工具，走 add_evidence——它还要定等级。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(OBJECT_KINDS),
                    "description": "对象类别",
                },
                "key": {
                    "type": "string",
                    "description": (
                        "身份键：PDB 编号 / UniProt 登录号 / ChEMBL 编号 / "
                        "受体 handle 加口袋序号 / 由输入拼出的位姿键"
                    ),
                },
                "label": {"type": "string", "description": "一句话名称，例如「EGFR 激酶域」"},
                "source": {"type": "string", "description": "谁产出的：工具名、数据库或文献出处"},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指向的登记表 handle 或 runs/ 下的路径",
                },
            },
            "required": ["kind", "key"],
            "additionalProperties": False,
        },
    )
    workspace_argument = "workspace"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = arguments["workspace"]
        kind = str(arguments.get("kind") or "").strip()
        key = str(arguments.get("key") or "").strip()
        refs = [str(item) for item in arguments.get("refs") or () if str(item).strip()]
        try:
            before = workspace.get(make_id(kind, key)) if key else None
            obj = workspace.register(
                kind,
                key,
                label=str(arguments.get("label") or ""),
                source=str(arguments.get("source") or ""),
                refs=refs,
            )
        except ObjectError as error:
            return ToolResult(output=str(error), failed=True)

        action = "已登记" if before is None else "已更新"
        lines = [f"{action} {obj.line()}（v{obj.version}）"]
        if not obj.refs:
            lines.append(
                "提示：这个对象没有引用任何 handle 或文件。结论里引用它时，"
                "别人查不到对应的产物——把产物 handle 补进 refs。"
            )
        lines.append(_save(workspace))
        return ToolResult(output="".join(lines), meta=obj.to_dict())


class AddEvidenceTool(Tool):
    """登记一条证据，并记下它支持或反驳哪条假设。"""

    spec = ToolSpec(
        name="add_evidence",
        description=(
            "登记一条**证据**，并记下它支持（supports）或反驳（refutes）哪条假设。\n"
            "等级从强到弱只有七种，登记之后**不能改**：要更正就换一个身份键，"
            "让「什么时候改的口径」留在表里。\n"
            "实测活性、实验结构、用户数据必须带至少一个引用（数据库编号、DOI/PMID、"
            "产物 handle、数据文件路径）：没有出处的「实测结果」与模型自己编的预测"
            "在文字上分不出来。\n"
            "假设的两种写法都认：会话任务状态里的 hyp#1，工作区对象里的 hypothesis:<键>。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "evidence_class": {
                    "type": "string",
                    "enum": list(CLASSES),
                    "description": (
                        "证据类别，从强到弱：experimental_activity、experimental_structure、"
                        "database、literature、user_data、prediction、inference。"
                        "只有前两类能让假设结案"
                    ),
                },
                "key": {
                    "type": "string",
                    "description": (
                        "身份键：文献用 DOI/PMID，活性用「分子@靶点@测定方式」，"
                        "预测用产物 handle"
                    ),
                },
                "source": {"type": "string", "description": "出处：数据库、文献、工具或用户"},
                "label": {"type": "string", "description": "一句话说明这条证据说了什么"},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "编号、handle 或文件路径",
                },
                "supports": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "支持的假设编号，例如 [\"hyp#1\"]",
                },
                "refutes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "反驳的假设编号",
                },
            },
            "required": ["evidence_class", "key", "source"],
            "additionalProperties": False,
        },
    )
    workspace_argument = "workspace"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = arguments["workspace"]
        evidence_class = str(arguments.get("evidence_class") or "").strip()
        key = str(arguments.get("key") or "").strip()
        refs = [str(item) for item in arguments.get("refs") or () if str(item).strip()]
        if evidence_class in NEEDS_REF and not refs:
            return ToolResult(
                output=(
                    f"{LABELS.get(evidence_class, evidence_class)}必须有出处："
                    "refs 里给数据库编号、文献 DOI/PMID、产物 handle 或数据文件路径。"
                    "拿不出出处的，按 prediction 或 inference 记。"
                ),
                failed=True,
            )
        try:
            obj = add_evidence(
                workspace,
                evidence_class=evidence_class,
                key=key,
                source=str(arguments.get("source") or ""),
                label=str(arguments.get("label") or ""),
                refs=refs,
                supports=arguments.get("supports") or (),
                refutes=arguments.get("refutes") or (),
            )
        except ObjectError as error:
            return ToolResult(output=str(error), failed=True)

        lines = [f"已登记 {obj.line()}（v{obj.version}）"]
        if obj.supports:
            lines.append(f"支持：{'、'.join(obj.supports)}")
        if obj.refutes:
            lines.append(f"反驳：{'、'.join(obj.refutes)}")
        lines.append(_save(workspace))
        return ToolResult(output="".join(lines), meta=obj.to_dict())


class ListObjectsTool(Tool):
    """看工作区里有哪些对象。"""

    spec = ToolSpec(
        name="list_objects",
        description=(
            "当你要看科研工作区里有哪些研究对象时用它。"
            "与 `list_workspace` 的分工：那个列登记表里的产物 handle，这里列研究对象。\n"
            "返回一行汇总（总数与每类几个），再一行一个对象：ID、名称、来源、引用；"
            "超过 limit 条时末尾写「另有 N 个」。不给 kind 时列全部；停用的默认不列。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS), "description": "只看这一类"},
                "include_retired": {
                    "type": "boolean",
                    "description": "连已经停用的对象一起列，默认不列",
                },
                "limit": {"type": "integer", "description": "最多列多少条，默认 40"},
            },
            "additionalProperties": False,
        },
    )
    workspace_argument = "workspace"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = arguments["workspace"]
        kind = str(arguments.get("kind") or "").strip() or None
        try:
            items = workspace.find(kind)
        except ObjectError as error:
            return ToolResult(output=str(error), failed=True)
        if not arguments.get("include_retired"):
            items = [obj for obj in items if obj.status == "active"]
        if not items:
            return ToolResult(output="工作区里还没有这一类对象。")

        limit = arguments.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = 40
        counts: dict[str, int] = {}
        for obj in items:
            counts[obj.kind] = counts.get(obj.kind, 0) + 1
        head = "、".join(
            f"{KIND_LABELS.get(name, name)} {counts[name]}" for name in KINDS if name in counts
        )
        lines = [f"共 {len(items)} 个对象（{head}）"]
        lines.extend(obj.line() for obj in items[:limit])
        if len(items) > limit:
            lines.append(f"另有 {len(items) - limit} 个，缩小 kind 或调大 limit")
        return ToolResult(output="\n".join(lines), meta={"counts": counts, "total": len(items)})


class ReviewHypothesisTool(Tool):
    """把一条假设的支持与反证摆在一起，给出意见。"""

    spec = ToolSpec(
        name="review_hypothesis",
        description=(
            "把一条假设的支持证据与反证摆在一起，给出五种意见之一：支持、削弱、"
            "需要修正、暂缓、否定。**只有实验级证据能改假设状态**；预测与推断只记录，"
            "不结案。\n"
            "apply=true 才写回会话任务状态里的假设；找不到这个编号就不写，也不会新建。"
            "证据不足时它会建议暂缓并说明缺什么，这时该提下一轮问题，而不是换个说法下结论。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "hypothesis": {
                    "type": "string",
                    "description": "假设编号，例如 hyp#1 或 hypothesis:selectivity-hinge",
                },
                "apply": {
                    "type": "boolean",
                    "description": "是否把意见写回任务状态里的假设，默认 false（只看不改）",
                },
            },
            "required": ["hypothesis"],
            "additionalProperties": False,
        },
    )
    workspace_argument = "workspace"
    task_argument = "task_state"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        workspace = arguments["workspace"]
        state = arguments.get("task_state")
        try:
            result = review(workspace, str(arguments.get("hypothesis") or ""))
        except ValueError as error:
            return ToolResult(output=str(error), failed=True)

        lines = [f"{result.hypothesis_id}：{VERDICT_LABELS.get(result.verdict, result.verdict)}"]
        for reason in result.reasons:
            lines.append(f"- {reason}")
        if result.status:
            lines.append(f"建议状态：{result.status}")
        else:
            lines.append("建议状态：不动（证据还不够结案）")

        if arguments.get("apply"):
            if not result.status:
                lines.append("没有写回：这条假设还没有可写回的结论。")
            elif apply_review(state, result):
                lines.append("已写回任务状态。")
            else:
                lines.append(
                    "没有写回：任务状态里没有这个编号的假设（编号写错了？），或者状态本来就是它。"
                )
        return ToolResult(
            output="\n".join(lines),
            meta={
                "hypothesis": result.hypothesis_id,
                "verdict": result.verdict,
                "status": result.status,
                "support": [obj.id for obj in result.support],
                "refute": [obj.id for obj in result.refute],
            },
        )


def domain_tools() -> list[Tool]:
    return [
        RegisterObjectTool(),
        AddEvidenceTool(),
        ListObjectsTool(),
        ReviewHypothesisTool(),
    ]


if __name__ == "__main__":  # pragma: no cover
    from debug_tools import main

    raise SystemExit(main(__name__, domain_tools()))
