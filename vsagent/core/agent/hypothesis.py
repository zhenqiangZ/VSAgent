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
_EDITABLE = ("claim", "mechanism", "evidence", "test", "expected", "mode", "discriminator")


def _rivals(
    state: TaskState, arguments: dict[str, Any], *, self_id: str = ""
) -> list[str] | None:
    """解析 `rivals` 并确认编号存在。没给这个参数时返回 `None`。

    `None` 与空数组是两件事：前者是「这次不动竞争关系」，后者是「清空」。
    两者混成一个值，`update` 只改 claim 时会把已有的竞争关系抹掉。
    """
    raw = arguments.get("rivals")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ToolError('rivals 要写成数组，例如 ["hyp#2"]。')
    values: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value not in values:
            values.append(value)
    known = {other.id for other in state.hypotheses if other.id and other.id != self_id}
    unknown = [value for value in values if value not in known]
    if unknown:
        raise ToolError(
            f"rivals 里这些编号不存在：{'、'.join(unknown)}。"
            f"现有的：{'、'.join(sorted(known)) or '（还没有）'}。"
        )
    return values


def _check_discriminator(discriminator: str, rivals: list[str], test: str) -> None:
    """有对手时必须写清「什么结果支持谁」。

    优先读独立的 `discriminator`；为了兼容旧调用，`test` 里字面提到对手
    仍然算数。要求提到每一个对手，否则两句话都对得上，等于没区分。
    """
    if discriminator:
        missing = [value for value in rivals if value not in discriminator]
        if missing:
            raise ToolError(
                "discriminator 要提到每个对手：缺少 "
                + "、".join(missing)
                + "。写清什么结果支持本条、什么结果支持对手。"
            )
        return
    if not any(value in test for value in rivals):
        raise ToolError(
            "写了 rivals 就要写 discriminator（或至少在 test 里说清哪个结果能区分它们）："
            f"现在两者都没有提到 {'、'.join(rivals)}。"
        )


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
            "必须给 `mode`（expand 组外找新骨架 / narrow 组内做同系物优化）；"
            "`kind=mechanism` 是这批分子为什么活。两类都要提——只谈机制会在错的库上"
            "做精细分析，只谈策略则永远不知道为什么要这么筛。\n"
            "两条假设给出**不同预期**时，用 `rivals` 写下对方编号，并用 `discriminator`"
            "写清哪个结果支持哪一条。没有竞争关系的假设各自检验，不要互填。\n"
            "**同一批里互相竞争的假设用 action=group 一次提出**：组内 `rivals` 写 key，"
            "不写 hyp#N；2–4 条一起校验，任何一条不合法就整组不写。\n"
            "`action=close` 要说明依据；拿到实验数据后用 `record_assay(hypothesis=...)` "
            "回填，状态会按预期与实际自动改，不必手工关。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["propose", "group", "update", "close", "list"],
                    "description": (
                        "propose 新建一条 / group 一次新建 2–4 条互指假设 / "
                        "update 修订未结的 / close 结案 / list 看全部"
                    ),
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
                    "description": "策略假设必填：expand 组外找新骨架 / narrow 组内优化",
                },
                "claim": {"type": "string", "description": "主张，一句话，必须可判真假"},
                "mechanism": {"type": "string", "description": "机制：你认为它为什么成立"},
                "evidence": {"type": "string", "description": "已有证据与出处（handle / 文献引用号）"},
                "test": {"type": "string", "description": "怎么检验：做什么实验或算什么"},
                "expected": {
                    "type": "string",
                    "description": "预期结果，要含数值、阈值或对照，例如「IC50 < 100 nM」",
                },
                "rivals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "与之竞争的假设编号（如 hyp#2）。只在两条假设给出不同预期时填，"
                        "且要写 discriminator 说明哪个结果能区分它们"
                    ),
                },
                "discriminator": {
                    "type": "string",
                    "description": (
                        "与对手靠什么结果区分：什么结果支持本条、什么结果支持对手。"
                        "写了 rivals 就必填；不要只写在 test 里"
                    ),
                },
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 4,
                    "description": (
                        "action=group 时的一组假设，2–4 条，一次写入。组内用 key 互指，"
                        "不会出现「引用尚未创建的编号」这种失败"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "组内临时名，1–12 位小写字母、数字、下划线或连字符",
                            },
                            "kind": {
                                "type": "string",
                                "enum": list(HYPOTHESIS_KINDS),
                                "description": "strategy / mechanism",
                            },
                            "mode": {
                                "type": "string",
                                "enum": list(HYPOTHESIS_MODES),
                                "description": "策略假设必填：expand / narrow",
                            },
                            "claim": {"type": "string", "description": "主张，一句话"},
                            "mechanism": {"type": "string", "description": "机制"},
                            "evidence": {"type": "string", "description": "已有证据与出处"},
                            "test": {"type": "string", "description": "怎么检验"},
                            "expected": {"type": "string", "description": "预期结果，要含数值或对照"},
                            "rivals": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "本组内的 key，不是 hyp#N",
                            },
                            "discriminator": {
                                "type": "string",
                                "description": "有 rivals 时必填：什么结果支持本条、什么结果支持对手",
                            },
                        },
                        "required": ["key", "kind", "claim", "test", "expected"],
                        "additionalProperties": False,
                    },
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
        if action == "group":
            return self._group(state, arguments)
        if action == "update":
            return self._update(state, arguments)
        if action == "close":
            return self._close(state, arguments)
        if action == "list":
            return self._list(state)
        raise ToolError(
            f"action 只能是 propose / group / update / close / list，收到 {action!r}。"
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
                    "策略假设要说明方向：mode=expand（组外找新骨架）或 "
                    "mode=narrow（组内做同系物优化）。"
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

        rivals = _rivals(state, arguments) or []
        discriminator = str(arguments.get("discriminator") or "").strip()
        if rivals:
            _check_discriminator(discriminator, rivals, test)

        item = Hypothesis(
            id=state.next_hypothesis_id(),
            kind=kind,
            mode=mode,
            claim=claim,
            mechanism=str(arguments.get("mechanism") or "").strip(),
            evidence=str(arguments.get("evidence") or "").strip(),
            test=test,
            expected=expected,
            rivals=rivals,
            discriminator=discriminator,
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
    def _group(self, state: TaskState, arguments: dict[str, Any]) -> ToolResult:
        """一次新建 2–4 条互指的假设。

        组内 `rivals` 只写 `key`：编号要等整组校验通过才分配，先写 `hyp#N`
        必然撞上「编号不存在」，`ses_0028` 就是这样连失败两次。整组任何一条
        不合法都一条不写，避免半组假设指着不存在的对手。
        """
        raw = arguments.get("items")
        if not isinstance(raw, list) or not 2 <= len(raw) <= 4:
            raise ToolError("group 需要 items：一次 2–4 条假设。")

        parsed: list[dict[str, Any]] = []
        keys: set[str] = set()
        claims: set[str] = set()
        for index, entry in enumerate(raw, 1):
            if not isinstance(entry, dict):
                raise ToolError(f"items 第 {index} 条不是对象。")
            key = str(entry.get("key") or "").strip()
            if not re.fullmatch(r"[a-z0-9_-]{1,12}", key):
                raise ToolError(
                    f"items 第 {index} 条的 key 不合法（{key!r}）："
                    "用 1–12 位小写字母、数字、下划线或连字符。"
                )
            if key in keys:
                raise ToolError(f"items 里的 key 重复：{key}。")
            keys.add(key)

            kind = str(entry.get("kind") or "").strip()
            if kind not in HYPOTHESIS_KINDS:
                raise ToolError(
                    f"{key} 需要 kind：strategy（这一轮怎么筛）或 mechanism（这批为什么活）。"
                )
            mode = str(entry.get("mode") or "").strip()
            if kind == "strategy":
                if mode not in HYPOTHESIS_MODES:
                    raise ToolError(
                        f"{key} 是策略假设，要说明方向：mode=expand 或 mode=narrow。"
                    )
            elif mode:
                raise ToolError(f"{key} 是机制假设，不需要 mode。")

            claim = str(entry.get("claim") or "").strip()
            if not claim:
                raise ToolError(f"{key} 需要 claim：一句话说清主张什么。")
            if claim in claims:
                raise ToolError(f"items 里有两条主张一样：{claim!r}。")
            claims.add(claim)

            test = str(entry.get("test") or "").strip()
            if not test:
                raise ToolError(f"{key} 需要 test：说清怎么检验。")
            expected = str(entry.get("expected") or "").strip()
            if not expected:
                raise ToolError(f"{key} 需要 expected：预期结果。")
            if not _EXPECTED_SIGNAL.search(expected):
                raise ToolError(
                    f"{key} 的 expected 太含糊（{expected!r}）：要含数值、阈值或对照。"
                )

            raw_rivals = entry.get("rivals") or []
            if not isinstance(raw_rivals, list):
                raise ToolError(f"{key} 的 rivals 要写成数组，元素是本组 key。")
            rivals: list[str] = []
            for value in raw_rivals:
                rival = str(value or "").strip()
                if rival and rival not in rivals:
                    rivals.append(rival)

            parsed.append(
                {
                    "key": key,
                    "kind": kind,
                    "mode": mode,
                    "claim": claim,
                    "mechanism": str(entry.get("mechanism") or "").strip(),
                    "evidence": str(entry.get("evidence") or "").strip(),
                    "test": test,
                    "expected": expected,
                    "rivals": rivals,
                    "discriminator": str(entry.get("discriminator") or "").strip(),
                }
            )

        # 与已有假设重复的主张不再新建：编号一变，引用了旧编号的实验数据就断了。
        for entry in parsed:
            for existing in state.hypotheses:
                if existing.claim == entry["claim"]:
                    raise ToolError(
                        f"{entry['key']} 的主张已经有了：{existing.id}"
                        f"（{existing.label()}，状态 {existing.status}）。"
                        "要改内容用 update，不要重复提出。"
                    )

        # 对手必须是本组的 key；discriminator 要提到每一个对手。
        for entry in parsed:
            for rival in entry["rivals"]:
                if rival == entry["key"]:
                    raise ToolError(f"{entry['key']} 不能把自己写成对手。")
                if rival not in keys:
                    raise ToolError(
                        f"{entry['key']} 的对手 {rival!r} 不在本组 key 里："
                        f"{'、'.join(sorted(keys))}。组内互指只写 key，不要写 hyp#N。"
                    )
            if entry["rivals"]:
                _check_discriminator(
                    entry["discriminator"], entry["rivals"], entry["test"]
                )

        # 全部校验通过后才分配编号并写入。`next_hypothesis_id` 看得见刚追加的，
        # 所以连续取号不会重复。
        created: list[Hypothesis] = []
        for entry in parsed:
            item = Hypothesis(
                id=state.next_hypothesis_id(),
                kind=entry["kind"],
                mode=entry["mode"],
                claim=entry["claim"],
                mechanism=entry["mechanism"],
                evidence=entry["evidence"],
                test=entry["test"],
                expected=entry["expected"],
                discriminator=entry["discriminator"],
            )
            state.hypotheses.append(item)
            created.append(item)

        by_key = {entry["key"]: item for entry, item in zip(parsed, created)}
        for entry, item in zip(parsed, created):
            item.rivals = [by_key[rival].id for rival in entry["rivals"]]
        state.version += 1

        opened = sum(1 for other in state.hypotheses if other.is_open())
        lines = [f"已记录 {len(created)} 条假设（一次写入）："]
        for item in created:
            compete = "；竞争：" + "、".join(item.rivals) if item.rivals else ""
            lines.append(f"- {item.id}（{item.label()}）：{item.claim}{compete}")
        lines.append(
            f"当前有 {opened} 条未结假设。拿到证据后 close，"
            "或用 record_assay(hypothesis=...) 回填。"
        )
        return ToolResult(
            output="\n".join(lines),
            meta={"ids": [item.id for item in created], "open": opened},
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
        rivals = _rivals(state, arguments, self_id=item.id)
        if rivals is not None:
            test_text = str(arguments.get("test") or item.test or "")
            discriminator = str(
                arguments.get("discriminator") or item.discriminator or ""
            ).strip()
            if rivals:
                _check_discriminator(discriminator, rivals, test_text)
            if item.rivals != rivals:
                item.rivals = rivals
                changed.append("rivals")
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
            if item.discriminator:
                lines.append(f"  区分：{item.discriminator}")
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
