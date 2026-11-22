"""Intent Parser：把用户这一句话解析成 `IntentSpec`。

一次模型调用，产出五个维度与两条轴：

- 目标、对象、操作、约束、期望交付；
- 任务关系（新建、延续、修改、比较、恢复、无关）；
- 对话行为（执行、查进度、问结果、回答澄清、取消、闲聊）。

四条设计决定：

- **提示词很短**：角色目录由 `roles/catalog.py` 生成，除此之外只写字段与边界。
- **不猜**：用户没说结构编号、突变体、物种，就留空并写进 `missing_information`。
- **失败不伪造**：两次仍不合法就抛 `IntentParseError`。
- **只读一次现场**：可以带任务摘要、对象表、产物表与待澄清问题，不读完整历史。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from vsagent.core.agent.provider import ChatProvider, ProviderEventType
from vsagent.core.agent.roles.catalog import AGENTS, DUTIES, NOTES, ROLE_STAGES, TITLES, WHEN
from vsagent.core.agent.roles.contracts import ContractError, IntentSpec
from vsagent.core.agent.roles.templates import template_for

MAX_ATTEMPTS = 2
"""解析最多问两次。第二次只用于“格式不对”，不允许改变内容。"""

HEAD = """你是科研意图解析器。读用户这一句话，输出一个 JSON 对象，不要输出别的文字。

五个维度（全部必填）：
- goal：一句话说清用户最终想解决什么问题。
- objects：用户说到的科研对象。每项写 type、label、ref、origin。
  · type 只能是 target、structure、pocket、ligand、pose、score、artifact、hypothesis。
  · label 是用户说的名字，例如“EGFR”“第二个口袋”。
  · ref 优先写现场里给出的已有引用；新对象留空。
  · origin 是 existing（已有引用）、new（本轮新提）、current（当前任务正在用的）。
- operations：用户希望执行什么操作。取值只能是九个角色名，可以多选。
- constraints：用户明确提出的要求，逐条照抄原意。
- exclusions：用户明确禁止的事，逐条照抄。**这一项不许合并、不许改写。**
- expected_outputs：用户希望拿到什么。
- subgoals：任务分工。每项写 agent、goal，可选 stage。
  · agent 只能取 operations 里的角色名。
  · goal 是一句可执行的子目标；stage 只能取角色目录里该角色列出的阶段。
  · new、modify、compare 且 operations 非空时，每个操作角色都要有子目标。
- acceptance_criteria：每条写 kind、target、output。
  · kind 只能是 artifact_exists、handle_registered、evidence_ref、approval_recorded、review_required。
  · target 是检查对象；output 是这条检查对应的交付物，必须在该角色的交付物清单里。
  · new、modify、compare 且 operations 非空时至少写一条，每个交付物都要被检查。

两条轴：
- task_relation：new、continue、modify、compare、resume、none。
  · new：新任务。
  · continue：目标不变，接着做，或者补一句要求 / 加一条禁止。
  · modify：目标本身变了——换对象、换突变体、换范围。
  · compare：把两组已有对象或结果放在一起比较。
  · resume：明确要恢复以前的任务。
  · none：与科研任务无关，或者只是查进度、取消、闲聊。
  **分界线只有一条：完成的目标本身变没变。**
- conversation_action：execute、status_query、ask_result、clarify、cancel、chat。
  · execute：要执行或继续执行任务。
  · status_query：问进度、做到哪一步。
  · ask_result：问上一轮结果说明什么。
  · clarify：回答上一轮待澄清的问题。
  · cancel：终止任务。
  · chat：与科研任务无关的闲聊或概念问答。

其余字段：
- missing_information：完成任务还缺什么。**用户要求的就是“去发现”的，不算缺。**
- clarification_required：要问用户的问题。没有问题就给空数组。
- confidence：0 到 1。
- uncertainties：你推断出来但用户没明说的东西，写在这里。
- source_message_ids：留空数组。
"""

TAIL = """四条硬规则：
1. operations 只能填九个角色名，不要输出 principal，也不要输出不存在的名字。
2. 要执行任务时，new、modify、compare 至少要选一个操作；只补约束的 continue 可以不选。
   有操作时就要写子目标与验收条件；continue、resume 新增的角色也要写子目标。
   解析不出来就留空，并在 clarification_required 写一个最关键的问题。
3. 不要补全用户没说的结构编号、口袋编号、突变体、物种或筛选库。
4. 用户明确禁止某类动作时，operations 里不要出现对应的角色；
   每个角色能做什么、不能做什么，以角色目录里的「边界」为准。

常见组合：
- 用户没有确定具体蛋白， 例如没有给出 uniprot id 或者 pdb id，只能使用 research 进行一轮系统的调研，ligand 和 pocket 需要得到具体蛋白信息才可调用。
- 用户说“虚拟筛选”或“筛选一批分子”：选 ligand、docking；结构和口袋没定时加 pocket。
  下游要跑时，把 prepare 一并写进 operations 与 subgoals：它没有模板子目标，
  系统不会替你补一段任务书。
- 子目标写这一段要达成什么，不抄用户原话；验收条件的 target 写清检查什么。
- 用户说“比较”：task_relation 用 compare，operations 里要有 analysis，objects 指向两组已有对象。
- 用户要机制解释：选 mechanism；已有对接结果时可以同时选 analysis。
- 用户问靶点本身、问临床或文献证据：选 research。
- 用户要图、要页面：选 visualization。
- 用户问进度：conversation_action 用 status_query，operations 留空。
- 用户说“停”：conversation_action 用 cancel，operations 留空。

关于“缺什么”与“问什么”的区别（最容易错的一处）：
- missing_information 是**信息状态**：现在还缺哪些输入。可以写“结构与口袋尚未指定”。
- clarification_required 是**阻塞**：不回答就无法开始。**能靠检索或计算自己得到的，不要问。**
- 典型不要问的情形：用户说“获取某靶点的全部结构 / 所有候选结合口袋”——结构编号与
  口袋编号缺失不是阻塞，任务本身就是去发现它们。
- 典型要问的情形：只给了一个名词没说想做什么；说“分析这批分子”却没给分子或文件；
  两个目标互斥无法同时满足。
"""


def role_catalog() -> str:
    """九个角色的工作说明、选择时机与边界。由目录生成，不手写第二份。"""
    lines: list[str] = []
    for role in AGENTS:
        title = TITLES[role].split("：", 1)[-1]
        duties = "、".join(DUTIES[role]) or "无"
        when = WHEN[role] or "用户明确要这一类工作时。"
        note = NOTES.get(role) or "无额外边界。"
        outputs = "、".join(template_for(role).required_outputs) or "无"
        stages = "、".join(ROLE_STAGES.get(role, ())) or "无"
        lines.append(
            f"- {role}（{title}）：{duties}。\n"
            f"  什么时候选：{when}\n"
            f"  边界：{note}\n"
            f"  交付物：{outputs}；可用阶段：{stages}"
        )
    return "\n".join(lines)


PROMPT = HEAD + "\n\n可选子 Agent（operations 只能取这些名字）：\n" + role_catalog() + "\n\n" + TAIL


class IntentParseError(RuntimeError):
    """两次都没能得到合法的 `IntentSpec`。"""

    def __init__(self, message: str, *, raw: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.raw = raw
        self.attempts = attempts


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里取出那个 JSON 对象。

    模型常把 JSON 包在 ```json 里，或在前面加一句“好的”。这里只做两件事：
    去掉围栏、取第一个 `{` 到最后一个 `}`。不做宽松修复。
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


EXECUTING_RELATIONS: frozenset[str] = frozenset({"new", "modify", "compare"})


def _plan_problem(spec: IntentSpec) -> str:
    """检查模型有没有写全计划。空串表示通过。

    `IntentSpec` 只在子目标非空时核对覆盖；这里补上「一个都没写」这一种：
    要执行却没有任何子目标，编译阶段只能靠模板兜底，那正是这次要拆掉的部分。
    """
    if not spec.operations:
        return ""
    if spec.task_relation in EXECUTING_RELATIONS:
        if not spec.subgoals:
            return "要执行任务却没有 subgoals：每个操作角色都要有一段可执行的子目标"
        if not spec.acceptance_criteria:
            return "要执行任务却没有 acceptance_criteria：交付物至少要有验收条件"
    elif spec.task_relation in ("continue", "resume") and not spec.subgoals:
        return "新增了操作却没有 subgoals：给新角色补一段可执行的子目标"
    return ""


class IntentParser:
    """一次解析 = 一到两次模型调用，不参与工具循环。"""

    def __init__(self, provider: ChatProvider, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.provider = provider
        self.max_attempts = max(1, int(max_attempts))
        self.last_raw = ""
        self.last_usage: dict[str, int] = {}

    async def parse(self, text: str, *, context: str = "") -> IntentSpec:
        """解析一句话。`context` 是现场摘要，不要传整段历史。"""
        message = (text or "").strip()
        if not message:
            raise IntentParseError("没有可解析的输入")
        messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
        if context:
            messages.append({"role": "system", "content": f"这一轮的现场：\n{context.strip()}"})
        messages.append({"role": "user", "content": message})

        problem = ""
        for attempt in range(1, self.max_attempts + 1):
            raw = await self._ask(messages)
            self.last_raw = raw
            try:
                spec = IntentSpec.from_dict(extract_json(raw))
                problem = _plan_problem(spec)
                if problem:
                    raise ContractError(problem)
                if not spec.intent_id:
                    digest = hashlib.sha1(f"{message}\n{raw}".encode("utf-8")).hexdigest()
                    spec = IntentSpec.from_dict({**spec.to_dict(), "intent_id": f"intent_{digest[:10]}"})
                return spec
            except (ValueError, ContractError) as error:
                problem = str(error)
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"这份输出不合法：{problem}。补齐缺失字段，"
                            "保持已经正确的目标、对象与操作不变；"
                            "不要补充用户没有说过的事实。"
                        ),
                    }
                )
        raise IntentParseError(
            f"意图解析失败（{self.max_attempts} 次）：{problem}",
            raw=self.last_raw,
            attempts=self.max_attempts,
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
