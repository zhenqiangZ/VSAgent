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

from vsagent.core.file_refs import resolve_file_refs
from vsagent.core.models import Message, Part, PartType, Session
from vsagent.core.session_manager import SessionManager
from vsagent.core import live
from vsagent.core.agent.execution import campaign_state
from vsagent.core.agent.planning.context import ContextAssembler
from vsagent.core.agent.state.ledger import SessionLedger
from vsagent.core.agent.execution.loop import EXIT_CANCELLED, EXIT_ERROR, EXIT_LABELS, EXIT_TOOL_FAILED, AgentLoop
from vsagent.core.agent.research.research_workspace import ResearchWorkspace
from vsagent.core.agent.execution.spawn import SEED_RATIO, fork_messages, task_preamble

# 子 agent 最多能再往下派几层。0 表示根会话，1 表示子 agent。
MAX_DEPTH = 1

LIVE_THROTTLE = 0.5
"""实时快照两次写之间的最短间隔，单位秒。"""


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
        # 科研工作区同样按根会话共享，理由与登记表一样：结构 Agent 登记的口袋，
        # 机制 Agent 要按同一个 ID 找到；各建一个实例会各存一份，后写的那份
        # 把先写的整份覆盖掉（快照是整份写的）。
        self._workspaces: dict[str, ResearchWorkspace] = {}
        # 实时快照的节流时刻。一次模型往返里有几百个 `part.update`（每个文本增量
        # 都发一次），每个都写一遍文件没有意义；半秒一次足够页面显示「正在做什么」。
        self._live_at: dict[str, float] = {}
        # 每条正在跑的会话各有一对订阅（`part.add` / `part.update`）。
        # 只在跑的时候挂：不跑的时候总线上多两个处理函数，界面那几条「订阅数」的
        # 判据会跟着变，而它们在空闲时验的正是「没有多余的东西挂着」。
        self._live_subs: dict[str, list[Callable[[], None]]] = {}
        # 正在跑的多角色执行，按发起它的会话记。与 `_tasks` 分开：一场战役
        # 包含多个子会话，塞进 `_tasks` 会让"这个会话在跑吗"多算一次。
        self._campaigns: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # 循环层、登记表与工作区
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

    def workspace_for(self, session: Session) -> ResearchWorkspace:
        """同一个根会话下的所有会话共用一份科研工作区。

        与 `ledger_for` 同一条理由，但后果更重：登记表丢一条只是少一个 handle，
        工作区丢一条会让"这个结论的依据是哪条证据"断掉。所以这里必须**返回同一个
        实例**，让子 Agent 的登记直接落在父会话正在看的那份表里；各建一个指向同一
        文件的实例，会在各自保存时用整份快照覆盖对方。

        按 `family_id` 取，不按会话 id：子 Agent 与父会话是同一件事的两个执行者。
        """
        family = session.family_id
        workspace = self._workspaces.get(family)
        if workspace is None:
            workspace = ResearchWorkspace(session_id=family)
            workspace.load()
            self._workspaces[family] = workspace
        return workspace

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
        """根会话没了，它那一份共享登记表与工作区也就不该继续留在内存里。"""
        if family_id:
            self._ledgers.pop(family_id, None)
            self._workspaces.pop(family_id, None)

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
        run: bool = True,
        agent_role: str = "",
        task_id: str = "",
        campaign_id: str = "",
    ) -> Session:
        """从父会话 fork 一个子 agent，可选立刻开跑。

        `prompt=False` 连任务书都不写（工具层校验失败之类的场景用）；
        `run=False` 写任务书但不立刻跑，留给用户在子 agent 界面里手动开始。

        `agent_role` / `task_id` / `campaign_id` 只有结构化派发（`dispatcher.py`）
        才给：它们写在**创建时**而不是创建之后补，因为 `run=True` 时子会话可能
        在下一轮事件循环就开始跑，补写会与第一轮抢同一个会话。

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
        child.agent_role = agent_role
        child.task_id = task_id
        child.campaign_id = campaign_id
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
                "agent_role": agent_role,
                "task_id": task_id,
            },
        )
        # 手动建的子会话（旧版本 /agent 留下的）在父会话里留一条记录：
        # 模型派发至少有工具卡，手动建的不记下来，事后翻聊天记录看不出这里分出去过。
        # 记在 fork 之后，所以这条标记不会跟着被复制进子会话。
        if origin == "user":
            self._record_dispatch(parent, child)
        if prompt:
            # 没复制历史时把工作区索引一起交过去：fresh 子会话没有别的途径知道
            # 父会话手上有哪些 handle，而它要接着用那些东西。
            digest = getattr(loop.ledger, "digest", None)
            _message, body = self._append_user_message(
                child,
                task,
                task_preamble(
                    parent,
                    start,
                    end,
                    dropped,
                    digest() if callable(digest) else "",
                ),
            )
            # 仅创建：任务书已经写成子会话的第一条用户消息，只是先不发。
            # 用户在子 agent 界面里按开始（`start_pending`）再跑这一轮。
            if run:
                self._start(child, self.loop_for(child.id).stream_reply(child, body))
        # 创建本身就落一次盘：子会话与父会话里的派发标记都得活过重启。
        # 以前只靠「子 agent 跑完时 `_run` 顺手存一次」，不跑（或跑到一半进程没了）
        # 就整个消失，而界面上它明明已经出现了。
        if self.save_state is not None:
            self.save_state()
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
        _message, body = self._append_user_message(session, text, preamble)
        self._start(session, self.loop_for(session.id).stream_reply(session, body))
        return True

    def _append_user_message(
        self, session: Session, text: str, preamble: str = ""
    ) -> tuple[Message, str]:
        """写一条用户消息并广播，返回 (消息, 正文)。

        只写不发：`prompt` 与「仅创建」都要走出同一种消息形状，
        区别只在写完要不要立刻起一轮。文件引用也在这里解析，
        两条路拿到的 Part 才一致。
        """
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
        return message, body

    def start_pending(self, session_id: str) -> bool:
        """开始一个「仅创建」留下的子 agent。

        任务书在创建时已经作为用户消息写进会话（`spawn(run=False)`），
        这里只起一轮，不再写第二条——再写一遍会让模型看到两份同样的任务。
        """
        session = self.manager.get_session(session_id)
        if session is None or self.is_running(session_id):
            return False
        if not session.has_pending_prompt:
            return False
        self._start(session, self.loop_for(session.id).stream_reply(session, ""))
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
        # 先把「开始跑了」写出去：网页端在第一次工具调用之前也要知道这一轮起来了。
        self._live_at.pop(session.id, None)
        live.write(self.manager.state_path, session, running=True)
        if session.id not in self._live_subs:
            self._live_subs[session.id] = [
                self.bus.subscribe("part.add", self._on_live_update),
                self.bus.subscribe("part.update", self._on_live_update),
            ]
        task = asyncio.create_task(self._run(session, coroutine))
        self._tasks[session.id] = task

    def _on_live_update(self, data: dict[str, Any]) -> None:
        """把当前主题的进展写一份快照，页面按两秒一次来取。

        只写正在跑的会话：子 agent 的事件也走同一条总线，没在跑就跳过。
        节流放在这里而不是写入侧，因为文本增量一秒钟能发几十次。
        """
        session_id = str(data.get("session_id") or "")
        if session_id not in self._tasks:
            return
        now = time.time()
        if now - self._live_at.get(session_id, 0.0) < LIVE_THROTTLE:
            return
        session = self.manager.get_session(session_id)
        if session is None:
            return
        self._live_at[session_id] = now
        live.write(self.manager.state_path, session, running=True)

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
            # 状态文件写完才标「不跑了」：页面看到这一句就去重取主题清单，
            # 反过来的话它会拉到还不含刚封存那一段的旧状态。
            live.write(self.manager.state_path, session, running=False)
            self._live_at.pop(session.id, None)
            for unsubscribe in self._live_subs.pop(session.id, []):
                unsubscribe()

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

    # ------------------------------------------------------------------
    # 战役（一次多角色执行）
    # ------------------------------------------------------------------
    def start_campaign(
        self,
        session: Session,
        coroutine: Any,
        *,
        on_done: Callable[[Any], None] | None = None,
    ) -> bool:
        """在后台跑一次多角色执行。同一个会话同时只允许一次。

        与 `_start` 分开是因为生命周期不同：一轮回答以"最后一个 token"结束，
        一次战役以"所有子会话跑完"结束，中间可能隔着几分钟。放进 `_tasks`
        会让"这个会话在跑吗"的判定把整场战役算成一轮回答。
        """
        if session.id in self._campaigns:
            return False
        task = asyncio.create_task(self._campaign(session, coroutine, on_done))
        self._campaigns[session.id] = task
        self._set_campaign_state(session, campaign_state.RESEARCH)
        return True

    def _set_campaign_state(self, session: Session, target: str) -> None:
        """推进战役状态。

        跳不过去只记一条，不让后台任务挂掉：状态机是记账，不是闸门。
        真出现跳不过去，说明调用点写错了，事件里带着两头的名字。
        """
        next_state, reason = campaign_state.soft_advance(session.campaign_state, target)
        if reason:
            self.bus.publish(
                "campaign.state_error",
                {"session_id": session.id, "target": target, "error": reason},
            )
            return
        session.campaign_state = next_state
        self.bus.publish(
            "campaign.state",
            {
                "session_id": session.id,
                "state": session.campaign_state,
                "label": campaign_state.label(session.campaign_state),
            },
        )

    def campaign_running(self, session_id: str) -> bool:
        task = self._campaigns.get(session_id)
        return task is not None and not task.done()

    async def _campaign(
        self,
        session: Session,
        coroutine: Any,
        on_done: Callable[[Any], None] | None,
    ) -> None:
        try:
            campaign = await coroutine
        except asyncio.CancelledError:
            self._set_campaign_state(session, campaign_state.CANCELLED)
            self.bus.publish("campaign.cancelled", {"session_id": session.id})
            raise
        except Exception as error:  # noqa: BLE001 - 后台任务不能把异常吞进事件循环
            self._set_campaign_state(session, campaign_state.FAILED)
            self.bus.publish(
                "campaign.error",
                {"session_id": session.id, "error": f"{type(error).__name__}: {error}"},
            )
            return
        finally:
            self._campaigns.pop(session.id, None)
        # 子会话都跑完了，结果回到父会话手上：下一步是汇总与复核，不是交付。
        self._set_campaign_state(session, campaign_state.INTEGRATION)
        if on_done is not None:
            on_done(campaign)
        self.bus.publish(
            "campaign.done",
            {
                "session_id": session.id,
                "task_id": getattr(getattr(campaign, "plan", None), "task_id", ""),
                "done": list(campaign.done()) if hasattr(campaign, "done") else [],
                "unfinished": (
                    list(campaign.unfinished()) if hasattr(campaign, "unfinished") else []
                ),
                # 这次按子集派发、计划里还剩谁。事件订阅方据此知道事情没完。
                "pending": list(getattr(campaign, "pending", ()) or ()),
            },
        )
        if self.save_state is not None:
            self.save_state()

    def cancel_campaign(self, session_id: str) -> int:
        """取消一次战役：它自己、它派出的子会话，返回停掉的会话数。

        只取消带任务号的子会话：那是这次战役派出去的。旧版本用 `/agent`
        建的子 agent 没有任务号，不该被连坐。
        """
        stopped = 0
        task = self._campaigns.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            stopped += 1
        # 任务还没跑到第一个 await 就被取消时，`_campaign` 里那段不会执行，
        # 所以这里补一次。同一状态写两次是允许的（状态是「当前是什么」）。
        owner = self.manager.get_session(session_id)
        if task is not None and owner is not None:
            self._set_campaign_state(owner, campaign_state.CANCELLED)
        for child in self.children_of(session_id):
            if not child.task_id:
                continue
            if self.is_running(child.id):
                self.cancel(child.id)
                stopped += 1
        return stopped

    def shutdown(self) -> None:
        """退出时取消全部后台任务。"""
        for task in list(self._campaigns.values()):
            if not task.done():
                task.cancel()
        self._campaigns.clear()
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
