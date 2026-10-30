"""假设：这个 agent 的产出单位。

没有这个工具的时候，模型交上来的是一张排名表——「前 10 个分子、各自的分数」。
那种产出有两个毛病：**没法被证伪**（分数高低不是一个可以被推翻的判断），
**没法指导下一步**（不知道该合哪个分子、该换哪条路线）。假设把它变成一件可查的事：
主张是什么、靠什么机制、怎么检验、预期什么结果、什么情况下算被否掉。

数据在 `core/models.py`（`Hypothesis` / `TaskState.hypotheses`）——那里是数据层，
这里是模型能调用的那一层，两者分开的理由与 `task.py` 相同：`models.py` 不该反过来
依赖工具层。

三条边界，都是刻意的：

- **`test` 与 `expected` 是硬门槛。** 缺了它们的「假设」只是印象，而印象最容易被
  当成结论。`expected` 还要求含数值、阈值或对照——「活性会更好」不算预期，
  「IC50 < 100 nM」才算。
- **状态不由 `update` 改。** 只有 `close` 与实验数据回填（`record_assay`）能改，
  否则模型可以绕过证据把一条假设标成 supported。
- **编号不复用。** 删过也不回到旧号，否则 trace 与实验数据里那些 `hyp#3` 会指向
  两条不同的假设。
"""

from __future__ import annotations

import re
from typing import Any

from ..models import (
    HYPOTHESIS_KINDS,
    HYPOTHESIS_MODES,
    HYPOTHESIS_STATUSES,
    Hypothesis,
    TaskState,
)
from .tools import Tool, ToolError, ToolResult, ToolSpec

# 预期里必须能看出「怎么算达标」：一个数、一个比较号，或者一个对照。
# 只写「会更好」「应该有效」的，回填实验数据时没法判真假，也就没法闭环。
_EXPECTED_SIGNAL = re.compile(r"[<>≤≥~=]|\d|对照|不低于|不高于|以上|以下")

# `update` 能改的字段。`status` / `result_ref` 不在里面：那两样只能由 `close`
# 或实验回填写，见模块说明。
_EDITABLE = ("claim", "mechanism", "evidence", "test", "expected", "mode")


class HypothesisTool(Tool):
    """提出、修订、关闭一条假设。"""

    spec = ToolSpec(
        name="hypothesis",
        description=(
            "**你的产出单位是假设，不是排名。** 用这个工具提出一条可检验的判断，"
            "或在拿到证据后修订、关闭它。\n"
            "一条假设写六件事：主张（claim）、机制（mechanism）、已有证据（evidence）、"
            "怎么检验（test）、预期结果（expected）、什么算被否掉。"
            "`test` 与 `expected` 是硬门槛：没有它们就不是假设，是印象。"
            "`expected` 里要有数、阈值或对照（例如「IC50 < 100 nM」「对照组无活性」）。\n"
            "两种粒度：`kind=strategy` 是这一轮该怎么筛（聚焦库还是多样性库），"
            "必须给 `mode`（expand 找新骨架 / narrow 在已有骨架里优化）；"
            "`kind=mechanism` 是这批分子为什么活。两类都要提——只谈机制会在错的库上"
            "做精细分析，只谈策略则永远不知道为什么要这么筛。\n"
            "`action=close` 要说明依据；拿到实验数据后用 `record_assay(hypothesis=...)` "
            "回填，状态会按预期与实际自动改，不必手工关。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["propose", "update", "close", "list"],
                    "description": "propose 新建 / update 修订未结的 / close 结案 / list 看全部",
                },
                "id": {"type": "string", "description": "假设编号，如 hyp#1；update / close 必填"},
                "kind": {
                    "type": "string",
                    "enum": list(HYPOTHESIS_KINDS),
                    "description": "strategy（这一轮怎么筛）/ mechanism（为什么活）",
                },
                "mode": {
                    "type": "string",
                    "enum": list(HYPOTHESIS_MODES),
                    "description": "策略假设必填：expand 扩张找新骨架 / narrow 收窄",
                },
                "claim": {"type": "string", "description": "主张，一句话，必须可判真假"},
                "mechanism": {"type": "string", "description": "机制：你认为它为什么成立"},
                "evidence": {"type": "string", "description": "已有证据与出处（handle / 文献引用号）"},
                "test": {"type": "string", "description": "怎么检验：做什么实验或算什么"},
                "expected": {
                    "type": "string",
                    "description": "预期结果，要含数值、阈值或对照，例如「IC50 < 100 nM」",
                },
                "status": {
                    "type": "string",
                    "enum": [item for item in HYPOTHESIS_STATUSES if item != "open"],
                    "description": "close 时的结论：supported / refuted / inconclusive",
                },
                "result": {"type": "string", "description": "close 的依据：实验产物 handle 或一句话"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    )
    task_argument = "state"

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        state: TaskState = arguments["state"]
        action = str(arguments.get("action") or "").strip()
        if action == "propose":
            return self._propose(state, arguments)
        if action == "update":
            return self._update(state, arguments)
        if action == "close":
            return self._close(state, arguments)
        if action == "list":
            return self._list(state)
        raise ToolError(
            f"action 只能是 propose / update / close / list，收到 {action!r}。"
        )

    # ------------------------------------------------------------------
    def _propose(self, state: TaskState, arguments: dict[str, Any]) -> ToolResult:
        kind = str(arguments.get("kind") or "").strip()
        if kind not in HYPOTHESIS_KINDS:
            raise ToolError(
                "propose 需要 kind：strategy（这一轮怎么筛）或 mechanism（这批为什么活）。"
            )
        mode = str(arguments.get("mode") or "").strip()
        if kind == "strategy":
            if mode not in HYPOTHESIS_MODES:
                raise ToolError(
                    "策略假设要说明方向：mode=expand（找新骨架）或 mode=narrow（收窄优化）。"
                    "没有方向就没有筛选策略，只有一句愿望。"
                )
        elif mode:
            raise ToolError("mode 只用于 kind=strategy；机制假设不需要方向。")

        claim = str(arguments.get("claim") or "").strip()
        if not claim:
            raise ToolError("propose 需要 claim：一句话说清你主张什么。")
        # 同一条主张重复提交时不再新建：编号一变，之前引用了旧编号的实验数据就断了。
        for item in state.hypotheses:
            if item.claim == claim:
                return ToolResult(
                    output=(
                        f"这条主张已经有了：{item.id}（{item.label()}，状态 {item.status}）。"
                        "要改内容用 hypothesis(action=\"update\", id=...)。"
                    )
                )

        test = str(arguments.get("test") or "").strip()
        if not test:
            raise ToolError(
                "propose 需要 test：说清怎么检验。"
                "「跑一遍对接看看」不算——要写出做什么、看哪个量。"
            )
        expected = str(arguments.get("expected") or "").strip()
        if not expected:
            raise ToolError(
                "propose 需要 expected：预期结果。"
                "写不出预期，说明这条主张还没到可以检验的程度。"
            )
        if not _EXPECTED_SIGNAL.search(expected):
            raise ToolError(
                f"expected 太含糊（{expected!r}）：要含数值、阈值或对照，"
                "例如「IC50 < 100 nM」「前 10 名里至少 3 个落在同一骨架」「对照组无活性」。"
                "没有可判真假的预期，实验数据回来也没法结案。"
            )

        item = Hypothesis(
            id=state.next_hypothesis_id(),
            kind=kind,
            mode=mode,
            claim=claim,
            mechanism=str(arguments.get("mechanism") or "").strip(),
            evidence=str(arguments.get("evidence") or "").strip(),
            test=test,
            expected=expected,
        )
        state.hypotheses.append(item)
        state.version += 1
        opened = sum(1 for other in state.hypotheses if other.is_open())
        return ToolResult(
            output=(
                f"已记录 {item.id}（{item.label()}）：{item.claim}\n"
                f"检验：{item.test}；预期：{item.expected}\n"
                f"当前有 {opened} 条未结假设。拿到证据后 close，或用 record_assay(hypothesis=...) 回填。"
            ),
            meta={"id": item.id, "open": opened},
        )

    # ------------------------------------------------------------------
    def _update(self, state: TaskState, arguments: dict[str, Any]) -> ToolResult:
        item = self._require(state, arguments)
        if not item.is_open():
            raise ToolError(
                f"{item.id} 已经结案（{item.status}），不再改内容。"
                "它现在是证据的一部分；要提新判断就 propose 一条新的。"
            )
        changed: list[str] = []
        for name in _EDITABLE:
            if arguments.get(name) is None:
                continue
            value = str(arguments.get(name) or "").strip()
            if name == "mode":
                if item.kind != "strategy":
                    raise ToolError("mode 只用于策略假设。")
                if value not in HYPOTHESIS_MODES:
                    raise ToolError("mode 只能是 expand 或 narrow。")
            if name == "expected" and value and not _EXPECTED_SIGNAL.search(value):
                raise ToolError(
                    f"expected 太含糊（{value!r}）：要含数值、阈值或对照。"
                )
            if getattr(item, name) != value:
                setattr(item, name, value)
                changed.append(name)
        if not changed:
            return ToolResult(output=f"{item.id} 与这次给的一样，没有变化。")
        state.version += 1
        return ToolResult(
            output=f"{item.id} 已更新（{', '.join(changed)}）：{item.render()}",
            meta={"id": item.id, "changed": changed},
        )

    # ------------------------------------------------------------------
    def _close(self, state: TaskState, arguments: dict[str, Any]) -> ToolResult:
        item = self._require(state, arguments)
        status = str(arguments.get("status") or "").strip()
        if status not in HYPOTHESIS_STATUSES or status == "open":
            raise ToolError(
                "close 需要 status：supported（被证实）/ refuted（被否掉）/ inconclusive（判不了）。"
            )
        if item.status == status:
            return ToolResult(output=f"{item.id} 已经是 {status}，没有变化。")
        item.status = status
        item.result_ref = str(arguments.get("result") or "").strip() or item.result_ref
        state.version += 1
        tail = f"（依据：{item.result_ref}）" if item.result_ref else "（没有写依据）"
        return ToolResult(
            output=f"{item.id} 记为 {status}{tail}：{item.claim}",
            meta={"id": item.id, "status": status},
        )

    # ------------------------------------------------------------------
    def _list(self, state: TaskState) -> ToolResult:
        if not state.hypotheses:
            return ToolResult(output="还没有提出任何假设。")
        lines = [f"共 {len(state.hypotheses)} 条假设："]
        for item in state.hypotheses:
            lines.append(f"- {item.render()}")
        opened = sum(1 for item in state.hypotheses if item.is_open())
        lines.append(f"其中未结 {opened} 条。")
        return ToolResult(output="\n".join(lines), meta={"total": len(state.hypotheses)})

    def _require(self, state: TaskState, arguments: dict[str, Any]) -> Hypothesis:
        hypothesis_id = str(arguments.get("id") or "").strip()
        if not hypothesis_id:
            raise ToolError("需要 id（例如 hyp#1）。先用 hypothesis(action=\"list\") 看编号。")
        item = state.find_hypothesis(hypothesis_id)
        if item is None:
            known = "、".join(other.id for other in state.hypotheses) or "（还没有任何假设）"
            raise ToolError(f"没有 {hypothesis_id} 这条假设。现有：{known}。")
        return item
