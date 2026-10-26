from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..event_bus import EventBus
from ..models import Message, Part, PartType, Session, ToolStatus
from .context import (
    ContextAssembler,
    ContextAssemblyError,
    ContextBundle,
    EvidenceStore,
)
from .event_log import EventRuntime, event_boundary, record_local_event
from .toolset import family_of, hidden_names
from .hooks import POST_TOOL_USE, PRE_TOOL_USE, STOP, HookReport, HookRunner
from .permission import PermissionDecision, PermissionPolicy, Verdict
from .prompt import KEEP_MESSAGES, event_messages, stable_prefix, summary_messages
from .provider import (
    ChatProvider,
    OpenAICompatProvider,
    ProviderConfig,
    ProviderError,
    ProviderEventType,
)
from .tools import RetryableToolError, Tool, ToolError, ToolRegistry, argument_error
from .workspace import Workspace

# 一轮里最多问模型这么多次，防止模型在工具之间来回不收敛
MAX_STEPS = 24
# Stop 钩子最多让一轮多走这么多次，单独计数，不吃 MAX_STEPS 的额度
MAX_STOP_RETRIES = 2
# 可以重试的错误最多重发这么多次
MAX_RETRIES = 2
# 工具执行遇到可重试故障时最多再试这么多次。
#
# 比模型请求那层少（那里是 2）：一次工具调用的超时本身就有几十秒，重试两次
# 等于把一次网络抖动拖成三倍等待，而工具失败之后模型还有别的路可走。
# 一次就够把「抖了一下」与「真的不通」分开。
MAX_TOOL_RETRIES = 1
# 连着这么多步「整步工具全失败」就判成卡住，停下来并写收尾总结
MAX_FAILED_STREAK = 3
# 权限询问最多等多久（秒）。等不到按拒绝处理。
#
# 300 秒是「人去倒杯水再回来」的量级；再长就没有意义了——真没人看窗口的话，
# 多等十分钟只是让这一轮白占着。界面正常时会在这个时间之内答复。
PERMISSION_TIMEOUT = 300.0
# 摘要连续失败这么多次就熔断：后面不再试，这一段直接按丢弃处理。
#
# 熔断比「每步都试一次」好：摘要失败通常是接口或模型那一侧的问题，
# 每一步重试一次只是在每一步都多等一个超时。
MAX_SUMMARY_FAILURES = 3
# 用环境变量覆盖步数上限时的名字
MAX_STEPS_ENV = "VSA_MAX_STEPS"
# 留着多少步的上下文度量。`/context` 只显示最近 10 步，留 40 步是为了
# 一倍于显示窗口的余量：用户翻看时前面几步不会刚好已经被挤掉。
CONTEXT_HISTORY_LIMIT = 40


def max_steps_from_env(environ: dict[str, str] | None = None) -> int:
    """从环境变量取步数上限，没配或配得不合法时用默认值。

    固化在代码里的数字改起来要重新打包，而「一轮走多少步」是随任务变的：
    查资料的一轮十来步足够，跑一整条筛选流水线可能要几十步。
    配得不合法时**回到默认值而不是抛错**：这个词是顺手的调参口，
    写错一个字母就让应用起不来，代价与收益不成比例。
    """
    import os

    source = os.environ if environ is None else environ
    raw = source.get(MAX_STEPS_ENV, "").strip()
    if not raw:
        return MAX_STEPS
    try:
        value = int(raw)
    except ValueError:
        return MAX_STEPS
    # 下限给 1：0 或负数会让循环一次都不进，看起来像「什么都没做就结束了」。
    return value if value >= 1 else MAX_STEPS
# 每次重发之前等多久
RETRY_DELAYS = (1.0, 3.0)
# Stop 钩子拦下来时写进会话的那句话
STOP_NOTICE = "收尾检查没有通过："
# PostToolUse 钩子拦下来时接在工具结果后面的那一句
TOOL_HOOK_NOTICE = "工具执行之后的检查没有通过："

# 一轮没跑完就停下时，为什么停的。这几种都会走收尾总结那一条路：
# 不管任务是做完还是半途停下，用户都得拿到一份「这一轮发生了什么」。
EXIT_EXHAUSTED = "exhausted"
EXIT_CANCELLED = "cancelled"
EXIT_ERROR = "error"
EXIT_TOOL_FAILED = "tool_failed"

# 每种停法在前缀里怎么称呼。收尾总结的开头一行用它，用户一眼知道为什么停。
EXIT_LABELS: dict[str, str] = {
    EXIT_EXHAUSTED: "步数用满",
    EXIT_CANCELLED: "被中断",
    EXIT_ERROR: "请求出错",
    EXIT_TOOL_FAILED: "工具没能完成",
}


def missing_tool_reason(
    registry: ToolRegistry, name: str, session: Session | None = None
) -> str:
    """模型叫了一个不在注册表里的工具时，回给它的一句话。

    三种情形要说成三种话，因为模型该做的事不同：

    - 在禁用清单里 → 这个工具是被人为关掉的，**重试无用**，也不该换个方式绕。
    - 属于一个还没装的族 → 装那一族就好，这是按需加载唯一的真实失败路径。
      不说清楚的话，模型最可能做的是绕道 bash 或凭记忆写参数，两条都是错的。
    - 真的没有 → 报名字，让它自己改。

    分开还有个用处：模型偶尔会凭训练印象叫一个我们从来没有的工具
    （历史现场：`unknown tool: read`，当时注册表里确实只有 bash），
    那句话与「被你关掉了」混在一起会让用户以为是自己配错了。

    模块级函数而不是循环层的方法：它只读注册表的禁用清单与会话装过哪几族，
    是纯的，测试直接调它验这一句话即可，不必为此起一整层循环、配总线与模型。
    `session` 留成可选：不给时按「真的没有」处理，与按需加载之前的行为一致。
    """
    if name in registry.disabled:
        # **不许写「改用别的办法」**：那句话是在教模型绕道。实测一次这样的
        # 误判，模型转去 bash + urllib 自己实现了 search_pdb，烧掉 20 步。
        return (
            f"{name} 已被禁用（配置在 .opencode_tui/tools.toml 里，可在 /tools 面板里打开），"
            "本次会话不会提供这个工具，重试无用。\n"
            "不要用 bash 自己实现它，也不要换数据源绕过——"
            "把这件事告诉用户，由用户决定是否打开它。"
        )
    family = family_of(name)
    if family and session is not None and family not in session.tool_families:
        return (
            f"{name} 还没装进来：它属于 {family} 族。"
            f"先调 load_tools family={family}，下一步起它就在清单里；"
            "不要绕道 bash 去调等价的东西，也不要凭记忆写参数。"
        )
    return f"unknown tool: {name}"

# 让模型写收尾总结时给的指令。
#
# 四段固定，与压缩摘要的四段刻意用同一套骨架：用户读到的两份总结因此形状一致，
# 不必分辨「这是压缩还是收尾」。第一段要求写「产出在哪」，
# 因为任务做到一半停下时，用户最需要的不是过程复述，而是已经产出了什么、在哪个文件里。
WRAPUP_INSTRUCTION = (
    "这一轮任务还没有完成就停下了，停下来是因为：{reason}。\n"
    "现在写一份收尾总结交给用户，只写四段，每段一个小标题：\n"
    "一、这一轮做了什么（按顺序写清关键动作与用到的工具）\n"
    "二、产出了什么（文件路径、登记条目、得到的关键结论）\n"
    "三、还差什么（没做完的部分，以及卡在哪一步）\n"
    "四、下一步建议（用户接下来可以怎么做，要说具体）\n"
    "第二段的路径要照实写，写不准的宁可说「未产出」，不要编。\n"
    "只写总结本身，不要寒暄，不要复述这段指令，不要再调用工具。"
)


@dataclass
class _PendingPermission:
    """一次等界面答复的判定，连同答复时要用到的上下文。"""

    future: asyncio.Future[PermissionDecision]
    tool: Tool
    arguments: dict[str, Any]


class AgentLoop:
    """一轮生成的驱动：模型 → 工具 → 结果回灌 → 再问模型。

    界面只认 stream_reply / run_bash / cancel 三个方法。
    数据真相留在 Session 上，每次发问之前现场重建模型消息，这里不维护第二份历史。
    """

    def __init__(
        self,
        bus: EventBus,
        provider: ChatProvider,
        registry: ToolRegistry,
        config: ProviderConfig,
        context_window: int = 128_000,
        workspace_root: Path | None = None,
        policy: PermissionPolicy | None = None,
        hooks: HookRunner | None = None,
        ledger: Any = None,
        memory: Any = None,
        viewer: Any = None,
        spawner: Any = None,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.config = config
        self.context_window = context_window
        self.workspace_root = workspace_root
        self.policy = policy or PermissionPolicy()
        self.hooks = hooks or HookRunner([], workspace_root)
        self.ledger = ledger
        # 跨会话的项目记忆。与登记表不同，它不随会话切换而变。
        self.memory = memory
        # 本地结构服务的状态。与记忆一样是**应用级**的：整个进程一个，
        # 不随会话切换而变，也不每轮重建。open_viewer 只从中读实际端口。
        self.viewer = viewer
        # 子 agent 派发器。应用级对象，由 app 装配、按会话注入给 spawn_agent。
        # 根会话的循环层拿得到它，子 agent 的注册表里没有那个工具，
        # 所以「子 agent 不能再派发」在装配层就成立，不靠工具自己判断。
        self.spawner = spawner
        # 本轮的按需证据（检索回来的记忆、含关键行的工具结果）。
        #
        # 它是**一轮**的：每次发问之前清空。上一轮 recall 出来的记忆，
        # 到这一轮多半已经不相干，留着只会占证据层的额度。
        self.evidence = EvidenceStore()
        # 一轮最多问模型多少次。默认取模块常量，装配处可以按场景放宽或收紧。
        self.max_steps = max_steps
        self.cancelled = False
        self.last_latency_ms = 0
        # 最近几步的上下文度量。每步一条，`/context` 与主动压缩都读它。
        #
        # 一轮开始时清空：跨轮攒下来的数字分不清是哪件事的步骤，
        # 而用户按 `/context` 想知道的是「这一轮把窗口花在哪了」。
        self.context_history: list[dict[str, Any]] = []
        # 上一步实际发给模型的输入 token（含重试）。主动压缩的阈值看**实际值**，
        # 不看装配器的估算：估算回答的是「额度分给了谁」，服务端记账的是另一份。
        self.last_input_tokens = 0
        self.last_cached_tokens = 0
        # 摘要连续失败几次。到 `MAX_SUMMARY_FAILURES` 就熔断到下一轮。
        self._summary_failures = 0
        # 这一轮为什么停下。`None` 表示模型自己说完了；其余取值见 EXIT_*。
        # 子 agent 的状态机靠它区分 done / failed，不必再从消息的 Part 里猜。
        self.last_exit_reason: str | None = None
        self._pending_permissions: dict[str, _PendingPermission] = {}

    def cancel(self) -> None:
        """请求中断当前生成，并把等在那里的判定一并拒掉。

        不拒的话按 esc 中断之后循环会停在等答复上，界面看着已经回到可用状态，
        这一轮的 worker 却一直没有结束。
        """
        self.cancelled = True
        for pending in list(self._pending_permissions.values()):
            if not pending.future.done():
                pending.future.set_result(PermissionDecision(Verdict.DENY, reason="已经中断"))

    def resolve_permission(self, part_id: str, verdict: Verdict, remember: bool = False) -> bool:
        """界面把用户的选择交回来，返回是否交到了等着的那个判定上。

        `remember` 为真且判定为允许时，把**本会话的所有工具**一次性放行
        （弹窗上的 a 就是这一条）。放行的是工具，不是磁盘：工作目录之外的访问
        在那之前已经判过一次，仍然会来问，所以全放行不会把边界一起放掉。
        """
        pending = self._pending_permissions.get(part_id)
        if pending is None or pending.future.done():
            return False
        if verdict is Verdict.ALLOW:
            if remember:
                self.policy.allow_all()
                decision = PermissionDecision(Verdict.ALLOW, rule="本会话已放行所有工具")
            else:
                decision = PermissionDecision(Verdict.ALLOW, reason="用户允许")
        else:
            decision = PermissionDecision(Verdict.DENY, reason="用户拒绝")
        pending.future.set_result(decision)
        return True

    def switch_model(self, name: str) -> ProviderConfig:
        """换到另一个模型，返回换过之后的配置。

        连接参数照旧，窗口、单价、采样参数与推理能力按新模型重取。
        窗口跟着一起换：它决定上下文预算，留在旧模型的数值上会让预算算错。
        """
        return self.reconfigure(self.config.for_model(name))

    def reconfigure(self, config: ProviderConfig) -> ProviderConfig:
        """整套换掉连接参数：地址、密钥、模型都可能变。

        `/model` 改完接口之后走这条路径。窗口跟着一起换，理由与 `switch_model`
        相同；provider 用新的配置重建，旧的那个持有的是旧地址与旧密钥。
        """
        self.config = config
        self.context_window = config.context_window
        self.provider = OpenAICompatProvider(self.config, self.provider.timeout)
        return self.config

    def compact_session(self, session: Session, keep: int = KEEP_MESSAGES) -> int:
        """手动把会话前面的一段压掉，返回压掉之后还剩几条。

        这里只推位置，不在这里生成摘要：摘要是给下一次发问用的，
        而下一次发问会自己按 `dropped` 判据跑一遍。自动压缩是窗口装不下时才发生，
        这一条是用户按出来的，两种触发的产物因此长得一样。

        已经有事件时，被推掉的那一段先用**本地事件**盖住：事件必须连续，
        中间留缝的话那一段原文就既不在事件里、也不在 RECENT 里。
        """
        total = len(session.messages)
        if total <= keep:
            return total
        target = max(session.compact_upto, total - keep)
        if session.events and target > session.event_end:
            record_local_event(session, target, "local")
        session.compact_upto = max(session.compact_upto, target)
        session.sanitize_events()
        self.bus.publish("status.update", {"session_id": session.id})
        return keep

    async def stream_reply(self, session: Session, prompt: str) -> Message | None:
        """对一段用户输入生成流式回复。

        用户的输入已经作为一条 user Message 写进会话，这里的 prompt 只是同一个签名的占位。
        """
        self.cancelled = False
        self.last_exit_reason = None
        # 证据是「本步相关」的，一轮开始时清空：上一轮 recall 出来的记忆，
        # 到这一轮多半已经不相干，留着只会占掉证据层的额度。
        self.evidence.clear()
        # 度量同理按轮清空：这一轮的每一步各记一条，上一轮的不混进来。
        self.context_history.clear()
        self.last_input_tokens = 0
        self.last_cached_tokens = 0
        self._summary_failures = 0
        started = time.time()
        message = Message(
            role="assistant",
            session_id=session.id,
            model_id=self.config.model,
            provider_id=self.config.provider,
        )
        session.add_message(message)
        self.bus.publish("message.add", {"message_id": message.id, "session_id": session.id})
        workspace = Workspace(session.id, self.workspace_root)
        workspace.ensure()
        try:
            message, step, elapsed_ms, exit_reason = await self._run_steps(
                session, message, workspace
            )
            self.last_exit_reason = exit_reason
            # 收尾总结在 `_finish` 之前：它是一次真实请求，用量与耗时
            # 要算进这一轮，状态栏上的数字才对得上。
            #
            # 无论这一轮是跑完还是中途停下，都留下总结。用户的要求是
            # 「任何情况下都要给出总结」——停下时尤其需要，因为那正是
            # 用户最不知道发生了什么的时候。
            if exit_reason:
                await self._wrap_up(session, message, exit_reason)
        finally:
            self.last_latency_ms = int((time.time() - started) * 1000)
        # 中断那一条不收尾：`_finish` 会发 turn.done，而这一轮是用户按停的，
        # 界面已经在中断那一步回到可用状态了，再发一次收尾事件会把状态机推错。
        # 总结已经在上面留下了，所以「中断也有交代」不靠这一条。
        if self.cancelled:
            return message
        self._finish(session, message, step, elapsed_ms)
        return message

    async def run_bash(self, session: Session, command: str) -> Message:
        """执行一条用户直接给出的命令，把输出作为工具结果写入会话。

        与 stream_reply 走同一条装配路径：同样建工作目录、同样按参数模式校验、
        同样在收尾时发 status.update。两条路走出来消息形状一致，
        下一轮重建模型消息时翻译层不必分辨它是哪一条路产生的。
        """
        self.cancelled = False
        self.last_exit_reason = None
        # 与 stream_reply 一样，把这一轮算作新的起点：这一条命令的产物不该
        # 混进下一轮发问的证据里。
        self.evidence.clear()
        started = time.time()
        message = Message(
            role="assistant",
            session_id=session.id,
            model_id=self.config.model,
            provider_id=self.config.provider,
        )
        session.add_message(message)
        self.bus.publish("message.add", {"message_id": message.id, "session_id": session.id})
        workspace = Workspace(session.id, self.workspace_root)
        workspace.ensure()
        part = self._publish(
            message,
            Part(
                type=PartType.TOOL,
                tool_name="bash",
                tool_status=ToolStatus.PENDING,
                tool_input={"command": command},
            ),
        )
        try:
            await self._execute_tool(session, message, part, explicit=True)
        finally:
            self.last_latency_ms = int((time.time() - started) * 1000)
        self._publish(
            message,
            Part(type=PartType.STEP_FINISH, text=f"1 · {message.total_tokens()} tok · {self.last_latency_ms} ms"),
        )
        message.cost = self._cost(message.tokens)
        self.bus.publish("turn.done", {"message_id": message.id, "session_id": session.id})
        self.bus.publish("status.update", {"session_id": session.id})
        return message

    def _trace_context(self, session: Session, context: ContextBundle) -> None:
        """把这一轮装配削了什么记进事件流。

        只在真削过（移出过交互组，或者触发过降级）时才发，正常装配不发——
        每一步都发一条会把日志里真正要看的那几条淹掉。

        记下来是为了出问题时能回答「窗口是被哪一层吃满的、削到了第几级」，
        这两个问题光看消息区看不出来。`degraded` 是有序的，顺序就是削的先后。
        """
        if not context.dropped and not context.degraded:
            return
        self.bus.publish(
            "context.assembled",
            {
                "session_id": session.id,
                "dropped": context.dropped,
                "dropped_upto": context.dropped_upto,
                "degraded": list(context.degraded),
                "usage": {layer.value: tokens for layer, tokens in context.usage.items()},
            },
        )

    def _record_step_metrics(
        self,
        session: Session,
        step: int,
        context: ContextBundle,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
    ) -> None:
        """把这一步的装配估算与实际用量合成一条记录。

        估算（`context.metrics`）回答「额度分给了谁」，实际值回答「真的花了多少」。
        两者分开留着，`/context` 才能看出估算准不准；合成一个数就再也分不开了。

        `input_tokens` 是这一步**所有**请求之和（重试也算）：重试的每一次都重新
        发了整份上下文，把它们漏掉会让「窗口快满了」这件事晚一步才被发现。
        """
        metrics = dict(context.metrics)
        metrics.update(
            {
                "step": step,
                "input": input_tokens,
                "cached": cached_tokens,
                "output": output_tokens,
                # 分母为零时不给 0/0：写成 0.0，读的人一眼知道「这一步没有输入」，
                # 而不是「一次都没命中缓存」。
                "cache_hit": (cached_tokens / input_tokens) if input_tokens else 0.0,
                "window_used": (input_tokens / self.context_window) if self.context_window else 0.0,
            }
        )
        self.context_history.append(metrics)
        if len(self.context_history) > CONTEXT_HISTORY_LIMIT:
            del self.context_history[: len(self.context_history) - CONTEXT_HISTORY_LIMIT]
        self.last_input_tokens = input_tokens
        self.last_cached_tokens = cached_tokens
        self.bus.publish("context.metrics", {"session_id": session.id, **metrics})

    def _event_runtime(self, session: Session, message: Message) -> EventRuntime:
        """造一份事件运行时：收口位置、摘要请求与取消状态。

        摘要请求借用 `_ask_summary`，用量因此记在这一轮的消息上——收口花的钱
        与这一轮的其他花费记在同一处，状态栏上的数字才对得上。
        """
        return EventRuntime(
            session=session,
            # 当前正在写的那条 assistant 消息，收口必须停在它前面。
            current_index=max(0, len(session.messages) - 1),
            summarize=lambda target, start, end: self._ask_summary(
                message, event_messages(target, start, end)
            ),
            is_cancelled=lambda: self.cancelled,
        )

    def _memory_text(self, session: Session) -> str:
        """常驻注入的记忆正文；没装配记忆时是空串。

        每次发问都现取一次：记忆可能在会话中途被 remember 改过，
        缓存一份的话要额外记得在哪里失效，不如现取。

        带查询词是这一期的新东西：1200 token 装不下全部记忆，装「与当前问题
        相关的那几条」比装「最近改过的那几条」有用得多。
        """
        if self.memory is None:
            return ""
        return self.memory.resident_text(query=self._memory_query(session))

    @staticmethod
    def _memory_query(session: Session) -> str:
        """检索词：最后一条用户消息 + 任务目标。

        只看最后一条：更早的用户消息已经由 RECENT 或事件代表，
        而记忆要回答的是「现在这件事需要知道什么」。
        """
        pieces: list[str] = []
        for message in reversed(session.messages):
            if message.role != "user":
                continue
            text = " ".join(
                part.text
                for part in message.parts
                if part.type is PartType.TEXT and part.text
            )
            if text.strip():
                pieces.append(text.strip())
                break
        if session.task_state.goal:
            pieces.append(session.task_state.goal)
        return " ".join(pieces)

    @staticmethod
    def _task_text(session: Session) -> str:
        """任务状态渲染出的一段；还没有任务状态时是空串。

        与记忆同理，每次发问现取：会话中途可能被 `task` 工具或 `/task` 命令改过。
        空的就不塞——`TASK_HEADER` 里出现一个只有标题、下面什么都没有的段，
        白占额度还会让模型以为「这里本该有东西」。
        """
        state = session.task_state
        return "" if state.is_empty() else state.render()

    def tool_specs(self, session: Session) -> list[dict[str, Any]]:
        """这一步该发出去的工具定义：全部减去还没 `load_tools` 装过的族。

        按族加载把 34 个工具的 11.3K 定义压到核心那 10 个（约 2.1K），没装的
        那几族连名字都不出现——模型不会去调它不存在的东西。用排除而不是允许
        清单：新工具忘了登记族时它仍然每步都在（白占额度），而不是悄悄消失。
        """
        return self.registry.specs(exclude=hidden_names(session))

    def _assemble(self, session: Session, prefix: str, with_tools: bool = True) -> ContextBundle:
        """按分层装配这一步发给模型的消息。

        工具定义每一步都要全量重发，因此算进预算——不算的话，工具一多，
        真正留给对话的额度就被悄悄吃掉了。收尾总结那一条请求不带工具，
        所以传 `with_tools=False`，免得白占一份额度。

        证据层现取一份：工具是上一步跑的，这一步装配时才看得见它们留下了什么。
        """
        return ContextAssembler(self.context_window).build(
            session,
            prefix,
            tools=self.tool_specs(session) if with_tools else [],
            memory_text=self._memory_text(session),
            task_text=self._task_text(session),
            evidence=self.evidence.items(),
        )

    async def _run_steps(
        self,
        session: Session,
        message: Message,
        workspace: Workspace,
    ) -> tuple[Message, int, int, bool]:
        """按 step 推进，直到模型不再要求调用工具，且 Stop 钩子不再拦下来。

        返回最后那一条助手消息、走到第几步、耗时，以及**为什么停下**。
        最后那一条不一定是进来的那一条：Stop 钩子拦下之后会另起一条助手消息，
        模型消息按会话顺序重建，所以那一步必须是新的消息，不能接着往上写。

        最后那个返回值是 `None` 或 `EXIT_*` 里的一个：`None` 表示模型自己说完了，
        正常收尾；其余取值表示这一轮是停下来的，调用方据此补一份收尾总结。
        """
        started = time.time()
        step = 0
        tool_steps = 0
        stop_retries = 0
        failed_streak = 0
        exit_reason: str | None = None
        recovered = False
        while True:
            if self.cancelled:
                exit_reason = EXIT_CANCELLED
                break
            if tool_steps >= self.max_steps:
                exit_reason = EXIT_EXHAUSTED
                break
            step += 1
            self._publish(message, Part(type=PartType.STEP_START, text=f"{step} · {self.config.model}"))
            prefix = stable_prefix(workspace, session)
            try:
                context = self._assemble(session, prefix)
            except ContextAssemblyError as error:
                # 装不下就明说，不静默截断：模型在缺了材料的情况下继续往下做，
                # 比停下来让用户知道「要腾地方」糟得多。
                #
                # 但先救一次：把压缩位置往前推一段再装一遍。一次失败就放弃，
                # 等于把一个明明能救的场景直接变成错误——这是长会话最常见的死法。
                if not recovered and self._recover_context(session):
                    recovered = True
                    continue
                self._publish(message, Part(type=PartType.ERROR, text=f"上下文装不下：{error}"))
                exit_reason = EXIT_ERROR
                break
            if context.dropped:
                if session.events or session.summary_upto > 0:
                    # 已经有一份摘要（或已有事件）时，新的溢出交给事件，摘要冻结：
                    # 再让摘要往前推会与事件重叠，同一段内容被说两遍。
                    # 这一段补一条本地事件，零模型请求。
                    boundary = event_boundary(session, max(0, len(session.messages) - 1))
                    covered = min(context.dropped_upto, boundary)
                    if record_local_event(session, covered, "auto"):
                        context = self._assemble(session, prefix)
                elif await self._summarize(session, message, context.dropped_upto):
                    # 第一次溢出：先让模型写一份摘要，把「很久以前」压成一份结论。
                    # 之后的溢出才交给事件——两者覆盖的区间是分开的。
                    context = self._assemble(session, prefix)
            elif context.dropped_upto > 0:
                # 判据是「上下文里有没有已经不在的内容」，不是「这一步新丢了没有」：
                # 用户手动 /compact 推的边界不在当步产生新移出，但那一段同样要摘要；
                # 已经收进摘要的会由 `_summarize` 自己判掉，只留一条界面记录。
                if await self._summarize(session, message, context.dropped_upto):
                    context = self._assemble(session, prefix)
            self._trace_context(session, context)
            before = dict(message.tokens)
            calls = await self._ask_model(
                message, context.messages, self.tool_specs(session), session.effort
            )
            self._record_step_metrics(
                session,
                step,
                context,
                input_tokens=int(message.tokens.get("input", 0)) - int(before.get("input", 0)),
                cached_tokens=int(message.tokens.get("cached", 0)) - int(before.get("cached", 0)),
                output_tokens=int(message.tokens.get("output", 0)) - int(before.get("output", 0)),
            )
            if self.cancelled:
                exit_reason = EXIT_CANCELLED
                break
            if not calls:
                # 模型没有要走工具，还要看它是不是报错停下了：`_ask_model`
                # 在重试用尽时会把错误写成一条 ERROR Part 再返回空表，
                # 那种情况不能当成「模型说完了」，得走总结那一条。
                if self._has_error_part(message):
                    exit_reason = EXIT_ERROR
                    break
                report = await self._run_hooks(STOP, "", {})
                if not report.blocked:
                    break
                self._record_stop_notice(session, report.reason)
                if stop_retries >= MAX_STOP_RETRIES:
                    break
                stop_retries += 1
                message = self._start_continuation(session)
                continue
            tool_steps += 1
            if not await self._run_tools(session, message, calls):
                exit_reason = EXIT_CANCELLED
                break
            # 连续这么多次「整步工具全失败」才算卡住。
            #
            # 不能一次全失败就停下：工具失败的原因是模型下一步的输入，
            # 「未知工具名」「缺参数」这类它看一眼就会改。真正该停的是
            # **反复**换着法试同一件事都不成——那说明它没有能走通的路了。
            # 计数在成功那一步清零，所以中间只要对上过一次就重新算。
            if self._all_tools_failed(calls):
                failed_streak += 1
                if failed_streak >= MAX_FAILED_STREAK:
                    exit_reason = EXIT_TOOL_FAILED
                    break
            else:
                failed_streak = 0
        return message, step, int((time.time() - started) * 1000), exit_reason

    @staticmethod
    def _all_tools_failed(calls: list[Part]) -> bool:
        """这一步调用的工具是不是一个都没成功。"""
        if not calls:
            return False
        return all(part.tool_status is ToolStatus.FAILED for part in calls)

    @staticmethod
    def _has_error_part(message: Message) -> bool:
        """这条消息上是不是已经有一条 ERROR Part。

        `_ask_model` 在重试用尽时把错误写进 Part 并返回空表，
        从外面看与「模型答完了」一模一样。这一句用来区分两者。
        """
        return any(part.type is PartType.ERROR for part in message.parts)

    async def _wrap_up(self, session: Session, message: Message, reason: str) -> None:
        """一轮停下来时补一份收尾总结。

        先试让模型写：它看过整段会话，能说清「做了什么、产出在哪、还差什么」，
        这是本地信息拼不出来的。模型写不出来（请求失败、被取消、返回空）
        就退回本地汇编的要点——**总结这一条路不能因为再请求一次失败而整条断掉**，
        否则「任何情况下都有总结」就成了空话。

        总结是这一轮的最后一段，写进同一条助手消息：它在消息区排在最下面，
        正好是用户停手之后眼睛正好停住的位置。

        **被中断时不再发请求**：用户刚按了 esc 中断，再发一次请求等着它流回来
        与「停下来」这个意图正好相反，而且那一轮的请求会被立刻取消、什么也拿不到。
        这种情形直接走本地汇编——中断时用户要的就是马上看到「刚才做了什么」。
        """
        text = ""
        if not self.cancelled:
            try:
                text = await self._ask_wrap_up(session, message, reason)
            except (ProviderError, ContextAssemblyError):
                text = ""
        if not text.strip():
            text = self._local_wrap_up(session, message, reason)
        self._publish(
            message,
            Part(type=PartType.SUMMARY, text=text.strip()),
        )

    async def _ask_wrap_up(self, session: Session, message: Message, reason: str) -> str:
        """让模型写收尾总结。形状与压缩摘要一致：不带工具，只要一段文字。

        压缩摘要那一条路把会话前段收成摘要喂给模型；这一条**把会话原样交给它**——
        总结写的是刚过去的这一轮，它自己就在上下文里，再喂一份反而重复。
        超窗口时仍按同一套预算裁剪，走的是同一条 `ContextAssembler`。
        """
        workspace = Workspace(session.id, self.workspace_root)
        context = self._assemble(session, stable_prefix(workspace, session), with_tools=False)
        self._trace_context(session, context)
        messages = [
            *context.messages,
            {
                "role": "user",
                "content": WRAPUP_INSTRUCTION.format(
                    reason=EXIT_LABELS.get(reason, reason)
                ),
            },
        ]
        return await self._ask_summary(message, messages)

    def _local_wrap_up(self, session: Session, message: Message, reason: str) -> str:
        """模型写不出来时，本地把已有事实汇成一份总结。

        读的是这条消息上真实的 Part，不是凭空写的：用过的工具、产出的文件、
        失败的项都取自现场。读起来不如模型写的那一份顺，但它**一定出得来**。
        """
        tools: list[str] = []
        files: list[str] = []
        failures: list[str] = []
        for part in message.parts:
            if part.type is PartType.TOOL and part.tool_name:
                label = part.tool_name
                if label not in tools:
                    tools.append(label)
                if part.tool_status is ToolStatus.FAILED:
                    failures.append(f"{part.tool_name}：{part.tool_output or '没有写明原因'}")
            if part.type is PartType.FILE and part.file_path:
                if part.file_path not in files:
                    files.append(part.file_path)
            if part.type is PartType.PATCH and part.text and part.text not in files:
                files.append(part.text)
        lines = [f"一、这一轮停下来是因为：{EXIT_LABELS.get(reason, reason)}。"]
        lines.append(
            "二、用到的工具：" + ("、".join(tools) if tools else "（一个都没用上）")
        )
        lines.append(
            "三、产出的文件：" + ("、".join(files) if files else "（没产出文件）")
        )
        lines.append(
            "四、失败的步骤：" + ("；".join(failures) if failures else "（没有失败项）")
        )
        lines.append("（这份总结由本地按已有记录汇编，模型那次收尾请求没有成功。）")
        return "\n".join(lines)

    def _recover_context(self, session: Session) -> bool:
        """装不下时强行腾一次地方；腾不动返回 False。

        手段只有一种：把压缩位置推到会话的一半。有事件时先用本地事件把
        那一段盖住——否则事件与 RECENT 之间会留一道缝，那一段原文两边都不在。
        腾完由调用方再装一次；仍装不下才报错。
        """
        total = len(session.messages)
        target = max(session.compact_upto, total // 2)
        if total < 2 or target <= session.compact_upto:
            return False
        if session.events:
            record_local_event(session, target, "local")
        session.compact_upto = max(session.compact_upto, target)
        session.sanitize_events()
        return True

    async def _summarize(self, session: Session, message: Message, upto: int) -> bool:
        """丢掉的那一段还没进摘要时跑一次摘要，并把这次压缩记成一条 Part。

        返回是否新写了一份摘要。已经覆盖到的不再重复收：`summary_upto` 记着上一次收到哪里，
        所以一轮里丢掉的范围一路变大时，摘要只补新露出来的那一段。
        摘要是一次真实请求，用量累加在当前这条消息上，成本在状态栏上看得见。
        """
        upto = min(upto, len(session.messages))
        if session.events:
            # 事件与摘要不重叠：首条事件从 `summary_upto` 起，摘要再往前走
            # 就会把已经被事件替代的那一段重新收一遍。
            return False
        if upto <= session.summary_upto:
            self._publish(
                message,
                Part(type=PartType.COMPACTION, text=f"已丢弃 {upto} 条更早的消息"),
            )
            return False
        if self._summary_failures >= MAX_SUMMARY_FAILURES:
            # 熔断：接口或模型那一侧坏了，每一步再试一次只是每步多等一个超时。
            self._publish(
                message,
                Part(
                    type=PartType.COMPACTION,
                    text=f"摘要连续失败 {MAX_SUMMARY_FAILURES} 次，本轮不再尝试；这一段按丢弃处理",
                ),
            )
            return False
        failure = ""
        text = ""
        try:
            text = await self._ask_summary(message, summary_messages(session, upto))
        except ProviderError as error:
            failure = str(error)
        if self.cancelled:
            return False
        if not failure and not text:
            failure = "模型没有返回内容"
        if failure:
            self._summary_failures += 1
            self._publish(
                message,
                Part(
                    type=PartType.COMPACTION,
                    text=f"摘要没有生成（{failure}）；这一段按丢弃处理",
                ),
            )
            return False
        self._summary_failures = 0
        session.summary = text
        session.summary_upto = upto
        self._publish(
            message,
            Part(type=PartType.COMPACTION, text=f"已把前 {upto} 条消息收成摘要"),
        )
        return True

    async def _ask_summary(self, message: Message, messages: list[dict[str, Any]]) -> str:
        """跑一次摘要请求，返回模型写出来的正文。

        摘要不带工具：它只要一段文字。用量记在当前这条消息上，
        压缩花的钱因此与这一轮的其他花费记在同一处。
        """
        chunks: list[str] = []
        async for event in self.provider.stream(messages, [], ""):
            if self.cancelled:
                break
            if event.type is ProviderEventType.TEXT_DELTA:
                chunks.append(event.text)
            elif event.type is ProviderEventType.USAGE:
                self._add_usage(message, event.usage)
        return "".join(chunks).strip()

    def _record_stop_notice(self, session: Session, reason: str) -> Message:
        """把 Stop 钩子拦下来的原因作为一条用户消息写进会话。

        原因是发给模型的下一句输入，所以它的角色是用户：写成助手正文的话，
        模型会把这句话当成自己说过的，看不出是有人在纠正它。
        """
        notice = Message(role="user", session_id=session.id)
        part = Part(type=PartType.TEXT, text=f"{STOP_NOTICE}{reason}")
        notice.add_part(part)
        session.add_message(notice)
        self.bus.publish("message.add", {"message_id": notice.id, "session_id": session.id})
        self.bus.publish("part.add", {"session_id": session.id, "message_id": notice.id, "part_id": part.id})
        return notice

    def _start_continuation(self, session: Session) -> Message:
        """另起一条助手消息接着走这一轮。

        模型消息按会话顺序重建，钩子那一条之后的内容必须写进新的消息，
        接着往原来那条写的话，这一步会排在原因之前发出去。
        """
        continuation = Message(
            role="assistant",
            session_id=session.id,
            model_id=self.config.model,
            provider_id=self.config.provider,
        )
        session.add_message(continuation)
        self.bus.publish("message.add", {"message_id": continuation.id, "session_id": session.id})
        return continuation

    async def _run_hooks(
        self,
        event: str,
        tool: str,
        tool_input: dict[str, Any],
    ) -> HookReport:
        """跑一个事件上的钩子，把每一条的结果写进事件日志。

        钩子拦下、退出码不是 0 与 2、跑超时，看这一份日志就知道是哪一条、为什么。
        """
        report = await self.hooks.run(event, tool, tool_input)
        for outcome in report.outcomes:
            self.bus.publish(
                "hook.run",
                {
                    "event": event,
                    "tool": tool,
                    "source": outcome.source,
                    "command": outcome.command,
                    "code": outcome.code,
                    "blocked": outcome.blocked,
                    "reason": outcome.reason,
                },
            )
        return report

    async def _run_tools(self, session: Session, message: Message, calls: list[Part]) -> bool:
        """按模型给出的顺序逐个执行。中途被取消时返回 False。

        不并发：打分类工具会争同一块 GPU 与同一份输入文件，并发还会让结果写回历史时
        的顺序与模型给出的顺序不一致。

        返回 False **只表示被取消**。工具执行失败（拒绝、参数错、命令报错）
        不算：那些是模型下一步要读的输入，循环本来就该继续。
        失败由模型自己决定重试还是改法，不是判定「这一轮该停」的依据。
        「工具全失败」那一种停下是另一回事——由 `_ask_model` 那边判。
        """
        for part in calls:
            if self.cancelled:
                return False
            await self._execute_tool(session, message, part)
            if self.cancelled:
                return False
        return True

    async def _ask_model(
        self,
        message: Message,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str = "",
    ) -> list[Part]:
        """问一次模型，返回本 step 新建的工具 Part。可以重试的错误在这里重发。

        彻底失败时写成 `ERROR` 而不是 `TEXT`：这条内容**不是模型说出来的**，
        写成正文会让下一轮把报错当正常历史回灌（翻译层只认 TEXT 进 content）。
        界面上仍然看得见，只是带上出错标记。

        `tools` 由调用方给（`tool_specs(session)`），不在这一层现算：
        重试必须发同一份清单，否则「第几次尝试」会换一套工具。
        """
        attempt = 0
        while True:
            try:
                return await self._consume_stream(message, messages, tools, reasoning_effort)
            except ProviderError as error:
                if not error.retryable or attempt >= MAX_RETRIES:
                    self._publish(
                        message,
                        Part(type=PartType.ERROR, text=f"请求没有成功：{error}"),
                    )
                    return []
                attempt += 1
                self._publish(
                    message,
                    Part(type=PartType.RETRY, text=f"{error} · 第 {attempt} 次"),
                )
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])

    async def _consume_stream(
        self,
        message: Message,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str = "",
    ) -> list[Part]:
        """消费一次模型响应，把内容写成 Part，返回本 step 的工具调用。"""
        calls: list[Part] = []
        async for event in self.provider.stream(messages, tools, reasoning_effort):
            if self.cancelled:
                break
            if event.type is ProviderEventType.TEXT_DELTA:
                self._append_text(message, PartType.TEXT, event.text)
            elif event.type is ProviderEventType.REASONING_DELTA:
                self._append_text(message, PartType.REASONING, event.text)
            elif event.type is ProviderEventType.TOOL_CALL:
                calls.append(
                    self._publish(
                        message,
                        Part(
                            type=PartType.TOOL,
                            tool_name=event.tool_name,
                            tool_status=ToolStatus.PENDING,
                            tool_input=event.tool_args,
                        ),
                    )
                )
            elif event.type is ProviderEventType.USAGE:
                self._add_usage(message, event.usage)
        return calls

    async def _execute_tool(
        self,
        session: Session,
        message: Message,
        part: Part,
        explicit: bool = False,
    ) -> None:
        """执行一次工具调用，把结果或失败原因写回 Part。

        `explicit` 为用户直接敲出来的命令，判定链对那一类只查是否已中断。
        判定为询问时在这里停下来等界面答复，用户思考的时间不计入成本。

        PreToolUse 钩子排在判定之前：它是机械拦截，用户点允许也过不去。
        """
        tool = self.registry.get(part.tool_name)
        if tool is None:
            self._fail_tool(
                message,
                part,
                missing_tool_reason(self.registry, part.tool_name, session),
            )
            return
        error = argument_error(tool.spec, part.tool_input)
        if error:
            self._fail_tool(message, part, error)
            return
        report = await self._run_hooks(PRE_TOOL_USE, tool.spec.name, part.tool_input)
        if report.blocked:
            self._fail_tool(message, part, f"钩子拦下了这次调用：{report.reason}")
            return
        decision = self.policy.check(
            tool,
            part.tool_input,
            session.agent_name,
            explicit=explicit,
            cancelled=self.cancelled,
        )
        if decision.verdict is Verdict.ASK:
            decision = await self._ask_permission(session, message, part, tool, decision)
        if decision.verdict is not Verdict.ALLOW:
            self._fail_tool(message, part, decision.note())
            return
        part.permission = decision.note()
        part.tool_status = ToolStatus.RUNNING
        # 配额在**放行之后**才记：被拒的那一次不是一次调用（见 `note_tool_run`）。
        self.policy.note_tool_run(tool.spec.name)
        self.bus.publish("part.update", {"session_id": session.id, "message_id": message.id, "part_id": part.id})

        def record_arguments(given: dict[str, Any]) -> None:
            """把模型实际给出来的参数记回 Part。

            内核往 `arguments` 里注入登记表这类运行时对象，注入之后那一份不是
            模型给的东西：写进 `Part.tool_input` 会让界面与网页端看到一份带内核
            对象的调用。这里只记注入之前的那一份。

            **空字典不覆盖已有值**：模型可能真的一次都没传参数（`list_workspace`
            就是这样，`tool_input` 在发出请求时就已经是 `{}`），也可能什么都没
            给、由流式层写成 `None`。后者才需要补成空字典——否则网页端按
            「有没有输入」判断时会把它当成缺字段。
            """
            if not given and part.tool_input:
                return
            part.tool_input = given

        # 可重试的瞬时故障在这里重试，**不重跑权限与钩子**：那两步是本地判定，
        # 用户已经点过允许，重跑只会把同一个问题问第二遍，还会让钩子看见两次调用。
        attempt = 0
        while True:
            try:
                result = await tool.execute(
                    part.tool_input,
                    context=self.ledger,
                    task_state=session.task_state,
                    session=session,
                    evidence=self.evidence,
                    viewer=self.viewer,
                    spawner=self.spawner,
                    event_runtime=self._event_runtime(session, message),
                    registry=self.registry,
                    on_arguments=record_arguments,
                )
                break
            except RetryableToolError as error:
                if attempt >= MAX_TOOL_RETRIES:
                    # 重试用尽也要说清「这是瞬时故障」：模型分不清「工具坏了」与
                    # 「刚才网络抖了一下」时，最省事的做法是绕道 bash 自己实现一遍
                    # （实测一次这样的误判烧掉 20 步）。所以这里连「别绕」一起说。
                    self._fail_tool(
                        message,
                        part,
                        f"{tool.spec.name} 连续 {attempt + 1} 次都是瞬时故障（{error}）。\n"
                        "这不是参数写错，也不是这个工具不可用，是网络或接口那一侧的问题。\n"
                        "不要用 bash 自己实现它，也不要换数据源绕过：把情况告诉用户，"
                        "按用户的指示决定稍后再试还是换方案。",
                    )
                    return
                attempt += 1
                self._publish(
                    message,
                    Part(
                        type=PartType.RETRY,
                        text=f"{tool.spec.name} 瞬时故障，重试第 {attempt} 次：{error}",
                    ),
                )
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])
            except ToolError as error:
                self._fail_tool(message, part, str(error))
                return
        part.tool_output = result.output
        part.patch_added = result.added
        part.patch_removed = result.removed
        # 结构化读数留在 Part 上：证据层据此认出「这次产出了哪个 handle」，
        # 界面与网页端也能直接取数字，不必去正则解析中文输出。
        part.tool_meta = dict(result.meta or {})
        part.tool_status = ToolStatus.FAILED if result.failed else ToolStatus.COMPLETED
        report = await self._run_hooks(POST_TOOL_USE, tool.spec.name, part.tool_input)
        if report.blocked:
            part.tool_output = f"{part.tool_output}\n\n{TOOL_HOOK_NOTICE}{report.reason}"
            part.tool_status = ToolStatus.FAILED
        # 提炼放在状态定稿之后：钩子可能把这一条改成失败，那时它更该进证据层。
        self.evidence.add_from_part(part, tool)
        self.bus.publish("part.update", {"session_id": session.id, "message_id": message.id, "part_id": part.id})

    async def _ask_permission(
        self,
        session: Session,
        message: Message,
        part: Part,
        tool: Tool,
        decision: PermissionDecision,
    ) -> PermissionDecision:
        """发一个询问事件，等界面把用户的选择交回来。

        两条兜底，缺一条就会有人卡死：

        - **没有人订阅 `permission.ask`**：脚本、测试壳、以后可能出现的无头调用方
          都走这条路。等下去是等一个永远不会有人回答的 future，整轮就停在那里，
          连中断都传不进去。这时直接拒，原因写清楚给模型看。
        - **有人订阅但一直不答**：界面排队时那一条可能已经被取消、或者窗口被关掉，
          队列会跳过它。超时之后同样按拒绝处理，至少让这一轮走完。

        两处都用 `DENY` 而不是 `ALLOW`：没人答复不等于用户同意。
        """
        part.tool_status = ToolStatus.AWAITING
        part.permission = decision.note()
        self.bus.publish("part.update", {"session_id": session.id, "message_id": message.id, "part_id": part.id})
        future: asyncio.Future[PermissionDecision] = asyncio.get_running_loop().create_future()
        self._pending_permissions[part.id] = _PendingPermission(future, tool, part.tool_input)
        delivered = self.bus.publish(
            "permission.ask",
            {"session_id": session.id, "message_id": message.id, "part_id": part.id},
        )
        if delivered == 0:
            self._pending_permissions.pop(part.id, None)
            return PermissionDecision(
                Verdict.DENY,
                reason="没有界面可以询问权限（无人值守运行），这次调用被拒绝；"
                "需要放行的话请在 TUI 里手动跑，或改项目规则。",
            )
        try:
            return await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            return PermissionDecision(
                Verdict.DENY,
                reason=f"权限询问等了 {int(PERMISSION_TIMEOUT)} 秒没有人答复，这次调用被拒绝。",
            )
        finally:
            self._pending_permissions.pop(part.id, None)

    def _fail_tool(self, message: Message, part: Part, reason: str) -> None:
        """把工具标成失败并写明原因，这条原因会被回灌给模型。"""
        part.tool_status = ToolStatus.FAILED
        part.tool_output = reason
        self.bus.publish(
            "part.update",
            {
                "session_id": message.session_id,
                "message_id": message.id,
                "part_id": part.id,
            },
        )

    def _append_text(self, message: Message, part_type: PartType, text: str) -> None:
        """把增量接到当前这一类 Part 上，最后一个是别的类型时另起一个。"""
        last = message.parts[-1] if message.parts else None
        if last is not None and last.type is part_type:
            last.text += text
            self.bus.publish("part.update", {"session_id": message.session_id, "message_id": message.id, "part_id": last.id})
            return
        self._publish(message, Part(type=part_type, text=text))

    @staticmethod
    def _add_usage(message: Message, usage: dict[str, int]) -> None:
        """把一次请求的用量累加进这一轮的消息。"""
        tokens = dict(message.tokens)
        for key in ("input", "output", "cached", "uncached"):
            tokens[key] = int(tokens.get(key, 0)) + int(usage.get(key) or 0)
        message.tokens = tokens

    def _publish(self, message: Message, part: Part) -> Part:
        """把 Part 挂到消息上并通知界面。"""
        message.add_part(part)
        self.bus.publish("part.add", {"session_id": message.session_id, "message_id": message.id, "part_id": part.id})
        return part

    def _finish(self, session: Session, message: Message, step: int, elapsed_ms: int) -> None:
        """补上收尾的 Part、记账，并发出这一轮结束的信号。"""
        if not any(part.type in (PartType.TEXT, PartType.TOOL) for part in message.parts):
            self._publish(message, Part(type=PartType.TEXT, text="（模型没有返回内容）"))
        tokens = message.total_tokens()
        self._publish(
            message,
            Part(type=PartType.STEP_FINISH, text=f"{step} · {tokens} tok · {elapsed_ms} ms"),
        )
        message.cost = self._cost(message.tokens)
        self.bus.publish("turn.done", {"message_id": message.id, "session_id": session.id})
        self.bus.publish("status.update", {"session_id": session.id})

    def _cost(self, tokens: dict[str, int]) -> float:
        """按每百万 token 的单价算这一轮的花费。

        输入分两类：缓存命中的那部分单价只有未命中的十分之一。
        服务端没有给出分类时，全部输入按未命中的单价算。
        """
        uncached = tokens.get("uncached")
        cached = tokens.get("cached")
        if uncached is None and cached is None:
            uncached = int(tokens.get("input", 0))
            cached = 0
        return (
            int(uncached or 0) / 1e6 * self.config.price_in
            + int(cached or 0) / 1e6 * self.config.cached_price()
            + int(tokens.get("output", 0)) / 1e6 * self.config.price_out
        )
