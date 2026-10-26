"""会话运行时：每个会话一个循环层，子 agent 在后台并行跑。

为什么需要这一层，而不是继续让 `app.agent` 指一个 `AgentLoop`：

- 循环层持有 `cancelled`、`evidence`、`_pending_permissions` 这些**会话级**状态，
  两个会话共用一个实例会互相踩；
- 子 agent 要能在用户回到父会话之后继续跑，所以运行不能绑在某个屏幕的 worker 上；
- 登记表要按根会话共享（子 agent 直接引用父会话的 `receptor#1`），
  这一份共享关系需要一个进程内唯一的地方来记。

这一层**不 import textual**：后台任务用 `asyncio.create_task`，界面只订阅事件。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from ..file_refs import resolve_file_refs
from ..models import Message, Part, PartType, Session
from ..session_manager import SessionManager
from .context import ContextAssembler
from .ledger import SessionLedger
from .loop import EXIT_CANCELLED, EXIT_ERROR, EXIT_LABELS, EXIT_TOOL_FAILED, AgentLoop
from .spawn import SEED_RATIO, fork_messages, task_preamble

# 子 agent 最多能再往下派几层。0 表示根会话，1 表示子 agent。
MAX_DEPTH = 1


class AgentHost:
    """会话 → 循环层、后台任务、登记表的唯一持有者。

    `factory(session, ledger)` 由应用装配，返回一个已经接好 provider / registry /
    policy / hooks 的 `AgentLoop`。host 不关心那些东西怎么来的，
    只保证同一个会话拿到同一个 loop、同一个家族拿到同一份 ledger。
    """

    def __init__(
        self,
        manager: SessionManager,
        factory: Callable[[Session, SessionLedger], AgentLoop],
        bus: Any,
        workspace_root: Any,
        save_state: Callable[[], None] | None = None,
    ) -> None:
        self.manager = manager
        self.factory = factory
        self.bus = bus
        self.workspace_root = workspace_root
        self.save_state = save_state
        self._loops: dict[str, AgentLoop] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # 按根会话共享：子 agent 的 handle 与父会话是同一份。
        self._ledgers: dict[str, SessionLedger] = {}

    # ------------------------------------------------------------------
    # 循环层与登记表
    # ------------------------------------------------------------------
    def ledger_for(self, session: Session) -> SessionLedger:
        """同一个根会话下的所有会话共用一份登记表。

        **不能各建一个指向同一个文件的实例**：两个实例各有一份内存序号，
        `add()` 会各自从 `receptor#1` 开始，后保存的把先保存的覆盖掉。
        """
        family = session.family_id
        ledger = self._ledgers.get(family)
        if ledger is None:
            ledger = SessionLedger(session_id=family)
            ledger.load()
            self._ledgers[family] = ledger
        return ledger

    def loop_for(self, session_id: str) -> AgentLoop:
        """取会话对应的循环层，没有就现建一个。

        缓存键用**会话自己的 id**，不是传进来的那个字符串：应用启动前
        `current_session_id` 还是空串，按空串缓存会先建一个循环层，
        等状态载入之后同一会话再建一个，两个实例各跑各的。测试里给
        `app.agent.stream_reply` 打的补丁会落到前一个上，看起来像没生效。
        """
        if session_id:
            loop = self._loops.get(session_id)
            if loop is not None:
                return loop
        session = self.manager.get_session(session_id)
        if session is None:
            # 会话被删掉了（例如删根会话时级联删了子 agent），
            # 用一个空会话兜底，至少不让调用方拿到 None。
            session = self.manager.current
        loop = self._loops.get(session.id)
        if loop is not None:
            return loop
        loop = self.factory(session, self.ledger_for(session))
        self._loops[session.id] = loop
        return loop

    def loops(self) -> list[AgentLoop]:
        """已经建过的循环层。换模型时要逐个改。"""
        return list(self._loops.values())

    def items(self) -> list[tuple[str, AgentLoop]]:
        """会话 id 与循环层的配对。换工具清单时要按会话重建注册表。"""
        return list(self._loops.items())

    def forget(self, session_id: str) -> None:
        """会话被删掉时把缓存与后台任务一起清掉。"""
        self._loops.pop(session_id, None)
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    def drop_family(self, family_id: str) -> None:
        """根会话没了，它那一份共享登记表也就不该继续留在内存里。"""
        if family_id:
            self._ledgers.pop(family_id, None)

    async def wait(self, session_ids: list[str], timeout: float = 20.0) -> list[str]:
        """等这些会话跑完，返回超时前结束的那些 id。

        不取消没等到的任务：用户可能只是想先拿一部分结果，
        剩下的继续在后台跑。调用方按返回值自己分辨。
        """
        pending = [
            (session_id, self._tasks[session_id])
            for session_id in session_ids
            if session_id in self._tasks and not self._tasks[session_id].done()
        ]
        if not pending:
            return []
        done, _ = await asyncio.wait(
            [task for _, task in pending], timeout=max(0.0, timeout)
        )
        return [session_id for session_id, task in pending if task in done]

    # ------------------------------------------------------------------
    # 创建子 agent
    # ------------------------------------------------------------------
    def spawn(
        self,
        parent: Session,
        *,
        title: str,
        task: str,
        start: int = 0,
        end: int | None = None,
        origin: str = "user",
        prompt: bool = True,
    ) -> Session:
        """从父会话 fork 一个子 agent，可选立刻开跑。

        深度上限在这里兜底。工具层与界面层都各自拦过一次，但真正决定
        「能不能创建」的是这一处：前两层只是让模型与用户早点看到原因。
        """
        if parent.depth >= MAX_DEPTH:
            raise ValueError("子 agent 内不能再创建 agent")
        end = len(parent.messages) if end is None else end
        title = self._unique_title(parent.id, title or "子 agent")
        child = self.manager.create_session(
            title=title,
            parent_id=parent.id,
            root_id=parent.root_id or parent.id,
            depth=parent.depth + 1,
            origin=origin,
            seed_start=start,
            seed_end=end,
            status="idle",
            spawned_at=time.time(),
        )
        # 子会话沿用父会话当前的 Agent（Build / Plan）与模型，避免 Plan 下派出去的
        # 子 agent 突然能写文件。
        child.agent_name = parent.agent_name
        child.model_id = parent.model_id
        child.effort = parent.effort
        # 装过的工具族一起继承：子 agent 接着干同一件事，父会话为了这件事
        # 装过哪几族，它就得有哪几族。不继承的话它得自己再 load 一遍，
        # 而它看到的任务说明里并没有「先装工具」这一步。
        child.tool_families = list(parent.tool_families)

        loop = self.loop_for(parent.id)
        # 按**父会话的实际清单**算：子会话刚继承过族，两者一致；用全量算会把
        # 可用额度说小一半，塞进去的历史比实际能放下的少。
        budget = int(
            ContextAssembler(loop.context_window).available(loop.tool_specs(parent))
            * SEED_RATIO
        )
        copied, dropped = fork_messages(parent, child, start, end, max(1, budget))
        child.seed_start = start
        child.seed_end = end

        self.bus.publish(
            "agent.created",
            {
                "session_id": child.id,
                "parent_id": parent.id,
                "title": child.title,
                "origin": origin,
            },
        )
        # 用户派发要在父会话里留一条记录：模型派发至少有工具卡，
        # 用户打 /agent 的话，不记下来事后翻聊天记录完全看不出这里分出去过。
        # 记在 fork 之后，所以这条标记不会跟着被复制进子会话。
        if origin == "user":
            self._record_dispatch(parent, child)
        if prompt:
            # 没复制历史时把工作区索引一起交过去：fresh 子会话没有别的途径知道
            # 父会话手上有哪些 handle，而它要接着用那些东西。
            digest = getattr(loop.ledger, "digest", None)
            self.prompt(
                child.id,
                task,
                preamble=task_preamble(
                    parent,
                    start,
                    end,
                    dropped,
                    digest() if callable(digest) else "",
                ),
            )
        return child

    def _unique_title(self, parent_id: str, title: str) -> str:
        """同一父会话下重名时补 ` (2)`，让右栏两行字能分得开。"""
        used = {session.title for session in self.manager.children_of(parent_id)}
        if title not in used:
            return title
        index = 2
        while f"{title} ({index})" in used:
            index += 1
        return f"{title} ({index})"

    def _record_dispatch(self, parent: Session, child: Session) -> None:
        """在父会话里留一条 AGENT Part。

        它只用于显示：`translate_message` 只认 TEXT / FILE，这条消息不会
        进模型上下文，也不会占用窗口。单独成一条消息而不是塞进上一条，
        是为了让它排在这一刻的位置上，而不是漂到上一轮对话中间。
        """
        notice = Message(role="user", session_id=parent.id)
        part = notice.add_part(
            Part(type=PartType.AGENT, agent_name=child.title, text=child.id)
        )
        parent.add_message(notice)
        self.bus.publish(
            "message.add",
            {"session_id": parent.id, "message_id": notice.id},
        )
        self.bus.publish(
            "part.add",
            {"session_id": parent.id, "message_id": notice.id, "part_id": part.id},
        )

    # ------------------------------------------------------------------
    # 发问
    # ------------------------------------------------------------------
    def prompt(
        self,
        session_id: str,
        text: str,
        *,
        preamble: str = "",
    ) -> bool:
        """把一段输入写进会话并开始一轮。已经在跑时返回 False。

        用户消息的写入放在这里而不是界面里：后台派发与前台输入必须走出
        同一种消息形状，否则下一轮重建模型消息时会出现两种历史。
        """
        session = self.manager.get_session(session_id)
        if session is None or self.is_running(session_id):
            return False
        clean, file_parts = resolve_file_refs(text, self.workspace_root)
        body = f"{preamble}\n\n{clean}".strip() if preamble else clean
        message = Message(
            role="user",
            session_id=session.id,
            model_id=session.model_id,
        )
        message.add_part(Part(type=PartType.TEXT, text=body))
        for part in file_parts:
            message.add_part(part)
        session.add_message(message)
        self.bus.publish(
            "message.add",
            {"session_id": session.id, "message_id": message.id},
        )
        for part in message.parts:
            self.bus.publish(
                "part.add",
                {"session_id": session.id, "message_id": message.id, "part_id": part.id},
            )
        self._start(session, self.loop_for(session.id).stream_reply(session, body))
        return True

    def run_bash(self, session_id: str, command: str) -> bool:
        """执行用户直接给出的一条命令，与 `prompt` 一样在后台跑。"""
        session = self.manager.get_session(session_id)
        if session is None or self.is_running(session_id):
            return False
        message = Message(role="user", session_id=session.id)
        message.add_part(Part(type=PartType.TEXT, text=f"!{command}"))
        session.add_message(message)
        self.bus.publish(
            "message.add",
            {"session_id": session.id, "message_id": message.id},
        )
        self.bus.publish(
            "part.add",
            {
                "session_id": session.id,
                "message_id": message.id,
                "part_id": message.parts[0].id,
            },
        )
        self._start(session, self.loop_for(session.id).run_bash(session, command))
        return True

    def _start(self, session: Session, coroutine: Any) -> None:
        """起一条后台任务，并把它记在会话名下。"""
        self._set_status(session, "running")
        task = asyncio.create_task(self._run(session, coroutine))
        self._tasks[session.id] = task

    async def _run(self, session: Session, coroutine: Any) -> None:
        """跑完一轮之后落状态。异常也要落，否则右栏会一直显示运行中。"""
        note = ""
        status = "failed"
        try:
            await coroutine
            loop = self._loops.get(session.id)
            if loop is not None and loop.cancelled:
                status = "cancelled"
            elif loop is not None and loop.last_exit_reason in (
                EXIT_CANCELLED,
                EXIT_ERROR,
                EXIT_TOOL_FAILED,
            ):
                status = "failed"
                # 存可读原因而不是 exit_reason 的英文码：右栏与返回栏直接显示它。
                note = EXIT_LABELS.get(
                    str(loop.last_exit_reason), str(loop.last_exit_reason)
                )
            else:
                status = "done"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as error:  # noqa: BLE001 - 后台任务不能把异常吞进事件循环
            status = "failed"
            note = f"{type(error).__name__}: {error}"
        finally:
            self._tasks.pop(session.id, None)
            self._set_status(session, status, note)
            if self.save_state is not None:
                self.save_state()

    # ------------------------------------------------------------------
    # 中断与查询
    # ------------------------------------------------------------------
    def cancel(self, session_id: str) -> None:
        """中断某个会话当前的生成。只影响它自己。"""
        loop = self._loops.get(session_id)
        if loop is not None:
            loop.cancel()
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            # 任务可能在第一次调度之前就被取消，那时 `_run` 一行都走不到，
            # 状态会一直停在 running。这里先把状态落下来，`_run` 再落一次也一样。
            session = self.manager.get_session(session_id)
            if session is not None:
                self._set_status(session, "cancelled")
            task.cancel()

    def is_running(self, session_id: str) -> bool:
        """这个会话有没有一轮在跑。"""
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def children_of(self, session_id: str) -> list[Session]:
        """直接子 agent，按创建时间升序。"""
        return self.manager.children_of(session_id)

    def shutdown(self) -> None:
        """退出时取消全部后台任务。"""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    def _set_status(self, session: Session, status: str, note: str = "") -> None:
        """落状态并发事件。界面与右栏都只认这一条。"""
        session.status = status
        session.status_note = note
        session.touch()
        self.bus.publish(
            "agent.status",
            {"session_id": session.id, "status": status, "note": note},
        )
