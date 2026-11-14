"""按依赖顺序派发一组任务：能并行的并行，依赖满足再往下。

调度只做四件事，每轮重复：

1. 把"依赖链上已经失败"的任务标成 `blocked`，不再派发；
2. 在并发上限内，按输入顺序把就绪的任务派出去；
3. 等这一批里有会话跑完（`AgentHost.wait`），按它的状态收报告；
4. 没有在跑的、也没有就绪的，收工。

三条刻意的做法：

- **上游的引用直接写进下游的 `input_refs`，不经聊天记录。** 下游要的是
  `receptor#1` 与那条文献链接，不是上游怎么一步步试出来的。这也让下游任务书
  在派发时就是完整的，子 agent 不必先问一句"上游产出了什么"。
- **失败只影响依赖它的节点。** 一个配体任务失败，不应该把并行的结构任务一起停掉；
  但依赖它的筛选任务确实不能跑，标 `blocked` 而不是 `failed`——它自己没做错事。
- **任务状态记在调度结果里，不写回 `AgentTask`。** 任务对象是计划的一部分，
  同一份计划重跑时还要用；把运行状态写进去，第二次跑就带着上一次的痕迹。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from vsagent.core.agent.roles.contracts import AgentTask
from vsagent.core.agent.execution.dependency import Graph, blocked, build, ready
from vsagent.core.agent.execution.dispatcher import AgentDispatcher, DispatcherError
from vsagent.core.agent.execution.reports import Built

# 调度结果里的状态。比 `AgentTask` 多一个 `partial`：任务跑完了，但报告按角色
# 判不合格（缺产物或缺出处），它既不是成功，也不该让下游直接停掉。
SCHEDULE_STATUSES: tuple[str, ...] = (
    "done",
    "partial",
    "failed",
    "cancelled",
    "blocked",
)


class SchedulerError(RuntimeError):
    """任务集合本身不成立（重号、缺依赖、有环）。"""


@dataclass(frozen=True)
class Outcome:
    """一个任务的结局。"""

    task_id: str
    status: str
    session_id: str = ""
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in SCHEDULE_STATUSES:
            raise SchedulerError(f"不认识的状态：{self.status}")


@dataclass(frozen=True)
class Schedule:
    """一次调度的结果。`outcomes` 与 `reports` 都按任务号索引。"""

    outcomes: dict[str, Outcome]
    reports: dict[str, Built]

    def done(self) -> list[str]:
        return [key for key, item in self.outcomes.items() if item.status == "done"]

    def unfinished(self) -> list[str]:
        return [
            key for key, item in self.outcomes.items() if item.status not in ("done", "partial")
        ]


def _feed(task: AgentTask, reports: Mapping[str, Built]) -> AgentTask:
    """把直接上游的产物与证据写进这份任务的输入引用。

    只取**直接**依赖：隔了两层的引用由中间那层负责转交，一次全塞进来会让下游
    分不清哪些是它真正要用的。键名带上游任务号，撞名时不会互相覆盖。
    """
    extra: dict[str, str] = {}
    for dep in task.dependencies:
        built = reports.get(dep)
        if built is None:
            continue
        report = built.report
        if report.artifact_refs:
            extra[f"上游产物 {dep}"] = "、".join(report.artifact_refs)
        if report.evidence_refs:
            extra[f"上游证据 {dep}"] = "、".join(report.evidence_refs[:5])
    if not extra:
        return task
    refs = dict(task.input_refs)
    for key, value in extra.items():
        refs.setdefault(key, value)
    return replace(task, input_refs=refs)


class AgentScheduler:
    """把一组 `AgentTask` 跑完。

    `families` 按**角色**给（来自能力计划的 `tool_families`）：同一份计划里一个角色
    只有一套族，按任务给反而要抄一遍。
    """

    def __init__(
        self,
        dispatcher: AgentDispatcher,
        *,
        max_parallel: int = 3,
        poll: float = 1.0,
    ) -> None:
        self.dispatcher = dispatcher
        self.max_parallel = max(1, int(max_parallel))
        self.poll = max(0.0, float(poll))

    async def run(
        self,
        parent: Any,
        tasks: list[AgentTask] | tuple[AgentTask, ...],
        *,
        families: Mapping[str, Iterable[str]] | None = None,
        timeout: float | None = None,
        role_timeouts: Mapping[str, float] | None = None,
        on_report: Callable[[AgentTask, Built], None] | None = None,
    ) -> Schedule:
        """跑到没有可派的为止。图不成立时抛 `SchedulerError`，一个都不派。

        `timeout` 是**整场**的上限；`role_timeouts` 是每个角色的上限（秒）。
        两者都到点时中断子会话、记失败并写清卡在哪，不静默丢下它继续烧。
        """
        graph: Graph = build(tasks)
        if not graph.ok:
            raise SchedulerError("；".join(graph.problems))

        host = self.dispatcher.host
        plans = dict(families or {})
        budgets = {str(role): float(sec) for role, sec in (role_timeouts or {}).items() if sec}
        done: set[str] = set()
        failed: set[str] = set()
        started: set[str] = set()
        # 会话号 → (任务号, 子会话)。带上会话对象，收报告时不用再回查管理器。
        live: dict[str, tuple[str, Any]] = {}
        outcomes: dict[str, Outcome] = {}
        reports: dict[str, Built] = {}
        deadline = None if timeout is None else time.monotonic() + timeout
        task_deadline: dict[str, float] = {}

        while True:
            for task_id in blocked(graph, done, failed):
                if task_id in started:
                    continue
                outcomes[task_id] = Outcome(
                    task_id=task_id,
                    status="blocked",
                    problems=("上游失败或中断，这个任务没有再派发",),
                )
                failed.add(task_id)
                started.add(task_id)

            while len(live) < self.max_parallel:
                candidates = ready(graph, done, failed=failed, running=started)
                if not candidates:
                    break
                task = candidates[0]
                try:
                    result = self.dispatcher.dispatch(
                        parent, _feed(task, reports), families=plans.get(task.role, ())
                    )
                except DispatcherError as error:
                    started.add(task.task_id)
                    failed.add(task.task_id)
                    outcomes[task.task_id] = Outcome(
                        task_id=task.task_id, status="failed", problems=(str(error),)
                    )
                    continue
                started.add(task.task_id)
                session = result.session
                if result.created or session.status == "running":
                    live[session.id] = (task, session)
                    seconds = budgets.get(task.role)
                    if seconds:
                        task_deadline[task.task_id] = time.monotonic() + seconds
                else:
                    # 之前派过而且已经跑完：直接收报告，不再等。
                    self._settle(task, session, outcomes, reports, done, failed, on_report)

            if not live:
                break
            if deadline is not None and time.monotonic() > deadline:
                for session_id, (task, session) in list(live.items()):
                    live.pop(session_id, None)
                    failed.add(task.task_id)
                    outcomes[task.task_id] = Outcome(
                        task_id=task.task_id,
                        status="failed",
                        session_id=session.id,
                        problems=("调度超时，这一轮不再等它",),
                    )
                break

            finished = await host.wait(list(live), self.poll)
            for session_id in finished:
                entry = live.pop(session_id, None)
                if entry is None:
                    continue
                task, session = entry
                self._settle(task, session, outcomes, reports, done, failed, on_report)

            # 角色自己的时限：到点中断，记失败。与整场超时分开：整场超时是
            # 「这一轮不再等」，角色超时是「这一段卡住了，别的分支照跑」。
            now = time.monotonic()
            for session_id, (task, session) in list(live.items()):
                when = task_deadline.get(task.task_id)
                if when is None or now <= when:
                    continue
                live.pop(session_id, None)
                host.cancel(session.id)
                failed.add(task.task_id)
                seconds = budgets.get(task.role, 0)
                span = f"{seconds:g} 秒" if seconds < 60 else f"{seconds / 60:g} 分钟"
                outcomes[task.task_id] = Outcome(
                    task_id=task.task_id,
                    status="failed",
                    session_id=session.id,
                    problems=(
                        f"{task.role} 超过时限（{span}），已中断；"
                        "产物可能只写了一半，重派前先看它的运行目录",
                    ),
                )

        return Schedule(outcomes=dict(outcomes), reports=dict(reports))

    def _settle(
        self,
        task: AgentTask,
        session: Any,
        outcomes: dict[str, Outcome],
        reports: dict[str, Built],
        done: set[str],
        failed: set[str],
        on_report: Callable[[AgentTask, Built], None] | None,
    ) -> None:
        """一个子会话跑完了：收报告、定状态。"""
        task_id = task.task_id
        problems: list[str] = []
        try:
            built = self.dispatcher.collect(session)
        except DispatcherError as error:
            built = None
            problems.append(str(error))
        if built is not None:
            reports[task_id] = built
            problems.extend(built.problems)
            if on_report is not None:
                on_report(task, built)

        status = str(session.status or "")
        if status == "cancelled":
            outcome = "cancelled"
        elif status == "failed":
            outcome = "failed"
        elif problems:
            # 跑完了但报告不合格：算 partial。下游仍然可以跑——它缺的可能是
            # 一句出处，而不是产物；要不要重做由调用方按 problems 决定。
            outcome = "partial"
        else:
            outcome = "done"

        outcomes[task_id] = Outcome(
            task_id=task_id,
            status=outcome,
            session_id=session.id,
            problems=tuple(problems),
        )
        if outcome in ("done", "partial"):
            done.add(task_id)
        else:
            failed.add(task_id)
