"""任务之间的先后关系：谁可以先跑、谁要等、谁已经不可能再跑。

这一层只做纯计算，不碰会话、不碰事件，输入是一组 `AgentTask`，输出是分层结果
与"现在能派谁"。把它单独拿出来，是因为下面三件事都容易被写进调度循环里，
写进去之后就没法单独验：

- **图本身成不成立**：重号、指向不存在的任务、自己依赖自己、有环。有环意味着
  调度器永远启动不了任何节点，必须当场拒绝，而不是等到"什么都没跑"再回头查。
- **就绪的判定**：依赖全部 `done` 才算就绪。上游 `failed` 或 `cancelled` 时，
  下游不是"还没轮到你"，而是"永远不会轮到你"，这两件事要分开，否则界面会一直
  显示等待。
- **分层**：同一层的任务互不依赖，可以并行。分层的顺序按输入顺序稳定，
  同样的任务集合两次计算得到同样的结果——否则判据没法比对。

与 `agent_router.py` 的分工：那里从**任务规格**推出角色与依赖（计划阶段），
这里从**已派发的任务**判运行期的先后（执行阶段）。两边都用 Kahn，但一个是
"该有哪些节点"，一个是"这些节点现在能不能跑"。
"""

from __future__ import annotations

from dataclasses import dataclass

from vsagent.core.agent.roles.contracts import AgentTask


@dataclass(frozen=True)
class Graph:
    """一份任务集合的图。`problems` 非空时不要拿它去调度。"""

    tasks: tuple[AgentTask, ...]
    levels: tuple[tuple[str, ...], ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def by_id(self, task_id: str) -> AgentTask | None:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        return None

    def dependents(self, task_id: str) -> list[str]:
        """直接依赖这个任务的那些任务号。"""
        return [
            task.task_id
            for task in self.tasks
            if task_id in task.dependencies
        ]


def build(tasks: list[AgentTask] | tuple[AgentTask, ...]) -> Graph:
    """检查图并分层。有问题时 `levels` 只包含能排出来的那部分。"""
    problems: list[str] = []
    seen: dict[str, AgentTask] = {}
    for task in tasks:
        if task.task_id in seen:
            problems.append(f"任务号重复：{task.task_id}")
            continue
        seen[task.task_id] = task

    for task in seen.values():
        for dep in task.dependencies:
            if dep == task.task_id:
                problems.append(f"{task.task_id} 依赖自己")
            elif dep not in seen:
                problems.append(f"{task.task_id} 依赖不存在的任务：{dep}")

    levels = _levels(list(seen.values()), problems)
    return Graph(tasks=tuple(seen.values()), levels=levels, problems=tuple(problems))


def _levels(tasks: list[AgentTask], problems: list[str]) -> tuple[tuple[str, ...], ...]:
    """Kahn 分层。剩下的节点说明有环，把它们写进问题清单。"""
    known = {task.task_id for task in tasks}
    # 只把存在的依赖算进边，否则"依赖不存在的任务"会顺带变成"有环"，
    # 同一个毛病报两条，读的人会以为是两个问题。
    waiting = {
        task.task_id: {dep for dep in task.dependencies if dep in known and dep != task.task_id}
        for task in tasks
    }
    order = [task.task_id for task in tasks]
    levels: list[tuple[str, ...]] = []
    placed: set[str] = set()
    while waiting:
        level = [task_id for task_id in order if task_id in waiting and not waiting[task_id]]
        if not level:
            problems.append(f"依赖成环：{'、'.join(order_of for order_of in order if order_of in waiting)}")
            break
        levels.append(tuple(level))
        for task_id in level:
            waiting.pop(task_id, None)
            placed.add(task_id)
        for deps in waiting.values():
            deps.difference_update(level)
    return tuple(levels)


def ready(
    graph: Graph,
    done: set[str] | frozenset[str] = frozenset(),
    *,
    failed: set[str] | frozenset[str] = frozenset(),
    running: set[str] | frozenset[str] = frozenset(),
) -> list[AgentTask]:
    """现在可以派发的任务，按输入顺序。

    三个集合之外还有一件事：依赖已经失败的任务不算就绪，也不算"还在等"，
    它由 `blocked()` 报出来。
    """
    out: list[AgentTask] = []
    for task in graph.tasks:
        if task.task_id in done or task.task_id in failed or task.task_id in running:
            continue
        if any(dep in failed for dep in task.dependencies):
            continue
        if all(dep in done for dep in task.dependencies):
            out.append(task)
    return out


def blocked(
    graph: Graph,
    done: set[str] | frozenset[str] = frozenset(),
    failed: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """永远不会再跑的任务号：依赖链上有一个失败了。

    按层往外传：第一轮找出直接依赖失败节点的，第二轮找出依赖这些的，直到不再增加。
    不能只判直接依赖——A 失败时 B 被卡住，C 只依赖 B，C 也不会再跑。
    """
    dead = set(failed)
    out: list[str] = []
    changed = True
    while changed:
        changed = False
        for task in graph.tasks:
            if task.task_id in done or task.task_id in dead or task.task_id in out:
                continue
            if any(dep in dead for dep in task.dependencies):
                out.append(task.task_id)
                dead.add(task.task_id)
                changed = True
    return out
