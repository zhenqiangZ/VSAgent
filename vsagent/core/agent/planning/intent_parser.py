"""Intent Parser：把用户这一句话解析成 `IntentSpec`。

与 `intent.py` 的分工：那里是关键词提示（"该查哪几类来源"），这里是**一次模型
调用**，产出结构化意图（"这是一个什么科研任务、哪些要求是用户说的、哪些不许做"）。
两者都保留，互相不替代。

四条设计决定：

- **提示词很短**：只写字段与边界，不加载 `AGENTS.md`。解析只需要理解这一句话，
  把项目规则塞进来会让它开始讲流程，而不是回答"用户要什么"。
- **不猜**：用户没说结构编号、突变体、物种，就留空并写进 `missing_information`。
  猜一个出来比留空危险得多——下游会把它当成用户的要求。
- **失败不伪造**：两次仍不合法就抛 `IntentParseError`，由调用方决定是回退到
  旧执行方式还是问用户。绝不返回一份看起来完整的空壳意图。
- **只读一次上下文**：可以带一句当前任务的目标与约束（Phase 3 的连续性由调用方
  决定传什么），除此之外不读会话历史。
"""

from __future__ import annotations

import json
from typing import Any

from vsagent.core.agent.roles.contracts import ContractError, IntentSpec
from vsagent.core.agent.provider import ProviderEventType, ChatProvider

MAX_ATTEMPTS = 2
"""解析最多问两次。第二次只用于"格式不对"，不允许改变内容。"""

PROMPT = """你是科研意图解析器。读用户这一句话，输出一个 JSON 对象，不要输出别的文字。

字段（全部必填，没有内容就给空数组或空对象）：
- primary_goal：一句话说清用户要做什么。
- turn_mode：这一句与当前任务的关系，只能是 new_task、continue_task、
  modify_task、ask_about_result、resume_historical_task、general_chat 之一。
  给了"当前任务"摘要时据此判断；**没给摘要时不可能是在继续、改目标、问结果或恢复**，
  只能在 new_task 与 general_chat 之间选：与科研任务无关的概念问答、闲聊用
  general_chat，其余用 new_task。
  **分界线只有一条：完成的目标本身变没变。**
  · continue_task：目标不变，接着做，或者补一句要求 / 加一条禁止
    （"继续""再多取几个结构，但不要用冷冻电镜的"）。
  · modify_task：目标本身变了——换对象、换突变体、换范围
    （"改成只看 T790M 突变体""换成另一个靶点"）。只加约束不算 modify。
  · ask_about_result：问上一轮的结果（"这个结果说明什么""为什么失败"）。
  · resume_historical_task：明确要恢复以前的任务（"继续上次的 PK-02"）。
  · general_chat：与科研任务无关的闲聊或概念问答。
- intent_types：可多选，取值只能是 target_inquiry、structure_discovery、
  pocket_discovery、pocket_profiling、ligand_discovery、virtual_screening、
  scoring_evaluation、mechanism_analysis、literature_evidence、
  hypothesis_validation、multi_objective、task_continuation、general_conversation、
  needs_clarification。
- research_objects：用户说到的对象，例如 {"target": "EGFR"}。没说到的不要填。
- explicit_constraints：用户明确提出的要求，逐条照抄原意。
- exclusions：用户明确禁止的事，逐条照抄。**这一项不许合并、不许改写。**
- provided_inputs：用户已经给出的输入。**已知类别用固定键**：target（靶点编号）、
  structure（结构编号或文件）、pocket（口袋或口袋集）、ligand_set（配体库/集合）、
  receptor（已准备好的受体）；其它输入用文件路径或短名做键。只写用户真的给了的。
- missing_information：完成任务还缺什么。**用户要求的就是"去发现"的，不算缺。**
- clarification_required：要问用户的问题。没有问题就给空数组。
- expected_deliverables：用户期望拿到什么。
- confidence：0 到 1，你对这份解析的把握。
- uncertainties：你推断出来但用户没明说的东西，写在这里，不要写进 explicit_constraints。
- source_message_ids：留空数组。

三条硬规则：
1. 用户只给了一个对象（例如"EGFR"）、没说想做什么：intent_types 用
   needs_clarification，clarification_required 写一个最关键的问题。
2. 标了 needs_clarification 就必须有 clarification_required；反之不要求。
3. 不要补全用户没说的结构编号、口袋编号、突变体、物种或筛选库。

关于"缺什么"与"问什么"的区别（最容易错的一处）：
- `missing_information` 是**信息状态**：现在还缺哪些输入。可以写"结构与口袋尚未指定"。
- `clarification_required` 是**阻塞**：不回答就无法开始。**能靠检索或计算自己得到的，
  不要问。**
- 典型不要问的情形：用户说"获取某靶点的全部结构 / 所有候选结合口袋"——结构编号与
  口袋编号缺失不是阻塞，任务本身就是去发现它们，不要要求用户先给 PDB 编号、口袋
  编号、检测方法或参数。
- 典型要问的情形：只给了一个名词没说想做什么；说"分析这批分子"却没给分子或文件；
  两个目标互斥无法同时满足。

多目标：一句话里有两个以上目标时（例如"研究 EGFR 并寻找新型抑制剂"），
**具体类型与 multi_objective 都要出现**。分解成子目标由后续阶段做，解析阶段只负责
把"这是一件复合的事"标出来。
"""


class IntentParseError(RuntimeError):
    """两次都没能得到合法的 `IntentSpec`。"""

    def __init__(self, message: str, *, raw: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.raw = raw
        self.attempts = attempts


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里取出那个 JSON 对象。

    模型常把 JSON 包在 ```json 里，或在前面加一句"好的"。这里只做两件事：
    去掉围栏、取第一个 `{` 到最后一个 `}`。不做宽松修复——修复会把格式错误
    藏起来，而格式错误正是第二次调用要纠正的东西。
    """
    body = text.strip()
    if body.startswith("```"):
        lines = [line for line in body.splitlines() if not line.strip().startswith("```")]
        body = "\n".join(lines).strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出里没有 JSON 对象")
    data = json.loads(body[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层应当是一个对象")
    return data


class IntentParser:
    """一次解析 = 一到两次模型调用，不参与工具循环。"""

    def __init__(self, provider: ChatProvider, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.provider = provider
        self.max_attempts = max(1, int(max_attempts))
        # 最近一次的原始输出与用量，供调用方记账与 trace 用。
        self.last_raw = ""
        self.last_usage: dict[str, int] = {}

    async def parse(self, text: str, *, context: str = "") -> IntentSpec:
        """解析一句话。`context` 是可选的一行当前任务摘要，不要传整段历史。"""
        message = (text or "").strip()
        if not message:
            raise IntentParseError("没有可解析的输入")
        messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
        if context:
            messages.append({"role": "system", "content": f"当前任务：{context.strip()}"})
        messages.append({"role": "user", "content": message})

        problem = ""
        for attempt in range(1, self.max_attempts + 1):
            raw = await self._ask(messages)
            self.last_raw = raw
            try:
                return IntentSpec.from_dict(extract_json(raw))
            except (ValueError, ContractError) as error:
                problem = str(error)
                # 第二次只让它修格式，不许改内容——否则模型会顺手"补全"用户没说的话。
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"这份输出不合法：{problem}。只按报错修正格式与字段名，"
                            "不要改变已经解析出的内容，也不要补充用户没说过的东西。"
                        ),
                    }
                )
        raise IntentParseError(
            f"意图解析失败（{self.max_attempts} 次）：{problem}", raw=self.last_raw, attempts=self.max_attempts
        )

    async def _ask(self, messages: list[dict[str, Any]]) -> str:
        """问一次，把文本增量拼起来。工具清单为空：解析阶段不调工具。"""
        chunks: list[str] = []
        self.last_usage = {}
        async for event in self.provider.stream(messages, []):
            if event.type is ProviderEventType.TEXT_DELTA:
                chunks.append(event.text)
            elif event.type is ProviderEventType.USAGE:
                self.last_usage = dict(event.usage)
        return "".join(chunks)
