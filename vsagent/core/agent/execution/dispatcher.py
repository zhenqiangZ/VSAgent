"""按 `AgentTask` 派发子会话：没跑完的同一条任务只派一次，报告按角色判。

与 `host.spawn` 的分工：

- `host.spawn` 只负责「起一条子会话」：复制历史、建循环、挂到会话树上；
- 这里是**结构化**入口：任务来自能力计划，带着角色、任务号、输入引用与交付物。
  模型调用 `dispatch_campaign` 时走的就是这一条。

派发前多一道判断——这份任务有没有超出这个角色的授权（`AgentProfile.check_task`），
以及这个任务号下还有没有一场没跑完。

**为什么按任务号幂等，而不是按标题或目标文本**：标题可以重名，目标可以改一个字，
只有任务号是计划里生成、重规划时不变的那一个。用文本判重会让"同一条任务改动
措辞后重派"变成两个子会话，两边都往同一份登记表里写。

**幂等只管没跑完的会话**：已经结束的子会话（done / failed / cancelled）再派一次
不会捡回来沿用，而是带一代代号（`…:ligand#2`）开新的。延续任务里用户补一句
"再多取一些"，计划号与角色都没变；沿用旧会话会让父 agent 把上一轮的结论
原样当成这一轮的结果，实际什么都没做。

**为什么默认不复制父会话历史**：结构化任务的输入已经写在 `input_refs` 里
（上游的 `artifact_refs` / `evidence_refs` 与用户给定的事实）。再把父会话的
聊天记录搬过去，既花额度，又让子 agent 把父会话的思路当成自己的前提。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from vsagent.core.models import Session
from vsagent.core.agent.roles.contracts import AgentTask
from vsagent.core.agent.execution.host import MAX_DEPTH
from vsagent.core.agent.roles.profiles import AgentProfile, profile
from vsagent.core.agent.execution.reports import Built, build


class DispatcherError(RuntimeError):
    """派发被拒绝：深度、角色、授权或任务号冲突。"""


@dataclass(frozen=True)
class Dispatch:
    """一次派发的结果。`created=False` 表示同一条任务还有一场没跑完，返回那个子会话。"""

    task: AgentTask
    session: Session
    created: bool


def _title_for(spec: AgentProfile) -> str:
    """子会话默认标题：说明书标题里冒号前那一段（"结构与口袋：…" → "结构与口袋"）。"""
    return spec.title.split("：")[0]


def _next_task_id(task_id: str, children: Iterable[Session]) -> str:
    """同一条任务再派一次时用的新任务号：在原号后面加一代。

    已结束的子会话要留在会话树里，又不能被下一次 `find` 当成"这条任务已经派过"，
    所以新的一轮必须换一个号。按已有的最大代号加一，同一份会话树里重跑两次
    得到的号一致。
    """
    top = 1
    prefix = f"{task_id}#"
    for child in children:
        if child.task_id == task_id:
            top = max(top, 1)
        elif child.task_id.startswith(prefix) and child.task_id[len(prefix):].isdigit():
            top = max(top, int(child.task_id[len(prefix):]))
    return f"{task_id}#{top + 1}"


def _task_book(task: AgentTask, spec: AgentProfile) -> str:
    """把任务写成子 agent 读得懂的一段话。

    写成用户消息而不是系统提示：它只该在这个子会话里出现一次，进稳定前缀会
    跟着每一次请求，而任务书是这**一个**任务的东西。

    工具清单由角色配置决定，任务书只写「有几件、按配置装配」，不逐个列工具名：
    列出来就是一份会过期的清单，改了配置还要回来改这里。
    """
    lines = ["[结构化任务]", task.objective, f"角色：{spec.title}（{task.role}）"]
    from vsagent.core.agent.roles.agents import role_tools

    lines.append(f"工具：{len(role_tools(task.role))} 件，按角色配置装配，不必自己装")
    if spec.note:
        lines.append(f"边界：{spec.note}")
    if task.background:
        lines.append(f"背景（父任务，只作上下文，不要重复完成）：{task.background}")
    if task.input_refs:
        lines.append("输入引用（已给出的对象与事实，直接用，不要重新建）：")
        lines.extend(f"- {key}：{value}" for key, value in task.input_refs.items())
    if task.required_outputs:
        lines.append(f"要交的东西：{'、'.join(task.required_outputs)}")
    if task.acceptance_criteria:
        lines.append("验收条件：")
        lines.extend(
            f"- {row['kind']}：{row['target']}" for row in task.acceptance_criteria
        )
    if task.constraints:
        lines.append("限制：")
        lines.extend(f"- {item}" for item in task.constraints)
    if spec.report_requirements:
        lines.append(f"报告里不能空：{'、'.join(spec.report_requirements)}")
    lines.extend(
        [
            "剩余步数是上限，不是必须用完的额度。",
            "要交的东西齐了、验收条件满足，就直接写结论并停止；不需要调用工具时直接写结论。",
            "每一步只补验收条件缺的那几件，不要扩大范围。",
        ]
    )
    lines.append("完成后给出：结论、产物、证据出处、没做到的部分。")
    return "\n".join(lines)


class AgentDispatcher:
    """任务号 → 子会话的派发器。

    `host` 只用到三件东西：`spawn`、`children_of`。这样判据里可以用一个假的 host
    验派发规则，不必起整个应用。
    """

    def __init__(self, host: Any) -> None:
        self.host = host

    def find(self, parent: Session, task_id: str) -> Session | None:
        """这个任务号在本会话下已经派出去的子会话。没派过返回 None。

        按子会话上的字段找，不另存一份映射：状态文件里已经有这一份，另存一份
        会在重启后与它不一致——尤其是"派了但还没写完状态"的那一瞬间。
        """
        if not task_id:
            return None
        for child in self.host.children_of(parent.id):
            if child.task_id == task_id:
                return child
        return None

    def dispatch(
        self,
        parent: Session,
        task: AgentTask,
        *,
        title: str = "",
        run: bool = True,
    ) -> Dispatch:
        """派发一份结构化任务。被拒绝时抛 `DispatcherError`，不建空会话。"""
        if parent.depth >= MAX_DEPTH:
            raise DispatcherError("子 agent 内不能再派发：把建议写进结论交回父会话")
        if task.role == "principal":
            raise DispatcherError("主 Agent 不向自己派发：它负责拆解与汇总")

        existing = self.find(parent, task.task_id)
        run_task_id = task.task_id
        if existing is not None:
            if existing.agent_role != task.role:
                raise DispatcherError(
                    f"任务号 {task.task_id} 已经派给 {existing.agent_role}，"
                    f"不能再派给 {task.role}：先给这次改动作换一个新任务号"
                )
            # 还在待发或在跑的会话直接沿用：这一轮已经派过一次了。跑完的会话
            # 不复用——用户再次派发要的是新的结果，把上一轮的结论端回来等于没跑。
            if existing.status in ("idle", "running"):
                return Dispatch(task=task, session=existing, created=False)
            run_task_id = _next_task_id(task.task_id, self.host.children_of(parent.id))

        try:
            spec = profile(task.role)
        except KeyError as error:
            raise DispatcherError(str(error)) from error
        problems = spec.check_task(task)
        if problems:
            raise DispatcherError("；".join(problems))

        # 不复制历史：终点与起点都在末尾，`fork_messages` 拿到的区间是空的。
        end = len(parent.messages)
        child = self.host.spawn(
            parent,
            title=title.strip() or _title_for(spec),
            task=_task_book(task, spec),
            start=end,
            end=end,
            origin="agent",
            run=run,
            agent_role=task.role,
            task_id=run_task_id,
            campaign_id=task.campaign_id,
            # 子会话的任务状态只写它自己那几条约束与自己的目标，不再整份复制
            # 父会话的任务状态。任务书与状态两层看到的是同一份东西。
            constraints=task.constraints,
            goal=task.objective,
        )
        return Dispatch(task=task, session=child, created=True)

    def collect(self, session: Session) -> Built | None:
        """把跑完的子会话转成报告，按它的角色判一遍。

        没有角色（自由文本派发）时返回 None：那种子会话没有说明书可依，
        硬判一遍只会用错标准。
        """
        if not session.agent_role:
            return None
        try:
            spec = profile(session.agent_role)
        except KeyError as error:
            raise DispatcherError(str(error)) from error
        return build(
            session,
            spec,
            task_id=session.task_id,
            campaign_id=session.campaign_id,
        )
