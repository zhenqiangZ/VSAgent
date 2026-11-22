from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vsagent.core.event_bus import EventBus
from vsagent.core.models import Event, Message, Part, PartType, Session, ToolStatus
from vsagent.core.agent.planning.context import (
    ContextAssembler,
    ContextAssemblyError,
    ContextBundle,
    EvidenceStore,
)
from vsagent.core.agent.state.event_log import (TOPIC_SOFT_LIMIT, EventRuntime, absorb_tail,
                        event_boundary, open_tokens, record_step, seal_range,
                        seal_topic)
from vsagent.core.agent.planning import intent
from vsagent.core.agent.planning.intent_summary import render_intent
from vsagent.core.agent.roles.contracts import TaskSpec
from vsagent.core.agent.planning.route_adapter import describe as describe_route
from vsagent.core.agent.validators import check_tool
from vsagent.core.agent.execution.hooks import POST_TOOL_USE, PRE_TOOL_USE, STOP, HookReport, HookRunner
from vsagent.core.agent.permission import PermissionDecision, PermissionPolicy, Verdict
from vsagent.core.agent.roles.prompt import (
    KEEP_MESSAGES,
    environment_block,
    estimate_tokens,
    stable_prefix,
    summary_messages,
)
from vsagent.core.agent.provider import (
    ChatProvider,
    OpenAICompatProvider,
    ProviderConfig,
    ProviderError,
    ProviderEventType,
)
from vsagent.core.agent.tools import RetryableToolError, Tool, ToolError, ToolRegistry, argument_error
from vsagent.core.agent.research.workspace import Workspace
from vsagent.core.agent.search_reasoning import (
    AnswerState,
    SearchReasoningError,
    assess,
    capture_prior,
    integrate,
    save_answer,
    select_for_context,
    verify_and_repair,
)
from vsagent.core.agent.search_reasoning.adapters import is_trigger_tool
from vsagent.core.agent.search_reasoning import events as sar_events
from vsagent.core.agent.search_reasoning.evidence import (
    assessment_for,
    is_duplicate,
    items_from,
    merge,
)
from vsagent.core.agent.search_reasoning.models import SearchEvidence

# 一轮里最多问模型这么多次，防止模型在工具之间来回不收敛
MAX_STEPS = 24
# 剩这么多步时进入收束阶段：只放行交付动作，不再展开新工作。
# 等走到 0 再停，这一轮什么交代都没有，用户只能看见一截被截断的过程。
CLOSING_STEPS = 3
# 一个主题走到这么多步还没收，内核自己封一次。
# `close_topic` 是模型判断，实测一条主题能走到 21 步（`evt_0009`），
# 全靠它收不住。封存不花请求，封完原文让位、结论留下。
# 这只对根会话成立：子 agent 整个任务就是一个主题，中途封存会逼它
# 用 `recall_event` 把刚封掉的细节捞回来（2026-11-18 实测 3–5 步）。
TOPIC_STEP_LIMIT = 20
# 收束阶段放行的是「交付动作」：登记、写文件、核对、收主题、记状态。
# 子 agent 与根会话要交的东西不同，名单分开。
#
# 2026-11-18 的四个会话里，子 agent 最后三步想做的依次是
# `add_evidence`、`workspace`、`read`、`registry`，全部被旧名单拦下；
# 而旧名单里的 `write_run` 是把受体与口袋写成运行目录的工具，三个角色
# 一次都没用过。名单按层级与角色算，不再用一张表管所有会话。
WORKER_CLOSING = frozenset(
    {
        "write",
        "patch",
        "read",
        "grep",
        "registry",
        "workspace",
        "add_evidence",
        "close_topic",
        "task",
        "recall_event",
    }
)
ROOT_CLOSING = frozenset(
    {
        "read",
        "grep",
        "write",
        "patch",
        "registry",
        "read_turn",
        "search_history",
        "recall_event",
        "close_topic",
        "task",
    }
)
# 只有产出运行目录的角色才需要它。
RUN_CLOSING = frozenset({"write_run"})
RUN_CLOSING_ROLES = frozenset({"prepare", "docking", "visualization"})

# 收尾被拒时给模型的话。旧文案让所有角色都去调 `write_run`，而它只对
# 产出运行目录的角色有意义；这里按层级说清「现在还能做哪几类事」。
CLOSING_HINT_WORKER = (
    "本轮步数快用完了，不再取新数据、不再开新计算。"
    "把已经做出来的登记好（add_evidence、workspace），"
    "把交付文件写完或核对一遍（write、patch、read、grep），"
    "{runner}"
    "然后收主题、记下一步（close_topic、task）。"
    "最后按任务书给出结论、产物、证据出处、没做到的部分。"
)
CLOSING_HINT_ROOT = (
    "本轮步数快用完了，不再派发新角色、不再开新计算。"
    "子 agent 的结论已经随派发结果带回，核对交付文件（read、grep），"
    "然后收主题、记下一步（close_topic、task），"
    "最后给用户一份结论、产物与没做到的部分。"
)


def closing_tools(session: Session) -> frozenset[str]:
    """这一步只放行哪些工具。子会话按角色算，根会话按编排算。"""
    if int(getattr(session, "depth", 0) or 0) > 0:
        role = str(getattr(session, "agent_role", "") or "").strip()
        if role in RUN_CLOSING_ROLES:
            return WORKER_CLOSING | RUN_CLOSING
        return WORKER_CLOSING
    return ROOT_CLOSING


def closing_hint(session: Session) -> str:
    """收尾阶段拒绝一次调用时，告诉模型现在该做什么。"""
    if int(getattr(session, "depth", 0) or 0) > 0:
        role = str(getattr(session, "agent_role", "") or "").strip()
        runner = "需要运行目录的用 write_run，" if role in RUN_CLOSING_ROLES else ""
        return CLOSING_HINT_WORKER.format(runner=runner)
    return CLOSING_HINT_ROOT


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
# 记忆排序时参考的当前路径条数：当前受体加最近几件产物。
# 再多就会把「什么才算贴着当前任务」冲淡，而登记表里几十条产物的路径
# 也装不进一次记忆检索的词表。
CONTEXT_PATH_LIMIT = 6
# 用环境变量覆盖步数上限时的名字
MAX_STEPS_ENV = "VSA_MAX_STEPS"
# 搜索增强推理的开关与三种取值。
#
# `off` 与原来的路径一致；`shadow` 照常建立答案状态、评估与整合，但最终正文
# 仍用模型自己写的那一份；`enforce` 用整合并核对过的正文。应用装配层默认
# `shadow`，直接构造这一层时默认 `off`，见 `sar_mode_from_env` 的说明。
SAR_ENV = "VSA_SAR"
SAR_MODES = ("off", "shadow", "enforce")
# 「本轮回答状态」那一段放在 TASK 层，最多这么多文字单位。
# 完整状态在独立文件里；上下文每一步只带这一小份。
SAR_ANSWER_LIMIT = 800
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


def sar_mode_from_env(
    environ: dict[str, str] | None = None, *, default: str = "off"
) -> str:
    """从环境变量取搜索增强推理的模式。

    写错时回到 `default`，与步数上限那边同一个理由：这个词是顺手的调参口，
    拼错一个字母不该让应用起不来。取值本身只认三个，认不出的一律按默认。

    `default` 由装配处决定：应用层给 `shadow`（先在真实会话里观察），
    测试与直接构造给 `off`（不改变原有请求条数）。
    """
    import os

    source = os.environ if environ is None else environ
    fallback = default if default in SAR_MODES else "off"
    raw = source.get(SAR_ENV, "").strip().lower()
    return raw if raw in SAR_MODES else fallback


def defers_search_reasoning(session: Session, mode: str) -> bool:
    """research 子会话把搜索增强分成轮首与轮末两段。

    轮首写下先验答案，检索阶段只收结果，轮末再统一评估与整合。逐步评估
    会在每次检索之后插一次模型请求，研究角色一轮要查十几次，这些请求夹在
    检索之间只会打断它继续找；轮末一起看，去重与冲突也只算一遍。
    """
    return mode != "off" and session.agent_role == "research"


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
EXIT_TOPIC_FULL = "topic_full"

# 每种停法在前缀里怎么称呼。收尾总结的开头一行用它，用户一眼知道为什么停。
EXIT_LABELS: dict[str, str] = {
    EXIT_EXHAUSTED: "步数用满",
    EXIT_CANCELLED: "被中断",
    EXIT_ERROR: "请求出错",
    EXIT_TOOL_FAILED: "工具没能完成",
    EXIT_TOPIC_FULL: "这个主题已经足够长，先收一段",
}


def missing_tool_reason(
    registry: ToolRegistry, name: str, session: Session | None = None
) -> str:
    """模型叫了一个不在注册表里的工具时，回给它的一句话。

    三种情形要说成三种话，因为模型该做的事不同：

    - 在禁用清单里 → 这个工具是被人为关掉的，**重试无用**，也不该换个方式绕。
    - 不在这个角色的名单里 → 换角色或改配置，这是角色装配唯一的真实失败路径。
      不说清楚的话，模型最可能做的是绕道 bash 或凭记忆写参数，两条都是错的。
    - 真的没有 → 报名字，让它自己改。

    分开还有个用处：模型偶尔会凭训练印象叫一个我们从来没有的工具
    （历史现场：`unknown tool: read`，当时注册表里确实只有 bash），
    那句话与「被你关掉了」混在一起会让用户以为是自己配错了。

    模块级函数而不是循环层的方法：它只读注册表的禁用清单与会话的角色名单，
    是纯的，测试直接调它验这一句话即可，不必为此起一整层循环、配总线与模型。
    `session` 留成可选：不给时按「真的没有」处理，与按需加载之前的行为一致。
    """
    if name in registry.disabled:
        # **不许写「改用别的办法」**：那句话是在教模型绕道。实测一次这样的
        # 误判，模型转去 bash + urllib 自己实现了 search_pdb，烧掉 20 步。
        return (
            f"{name} 已被禁用（配置在 .vsagent/tools.toml 里，可在 /tools 面板里打开），"
            "本次会话不会提供这个工具，重试无用。\n"
            "不要用 bash 自己实现它，也不要换数据源绕过——"
            "把这件事告诉用户，由用户决定是否打开它。"
        )
    if session is not None:
        from vsagent.core.agent.roles.agents import allowed_tools

        allowed = allowed_tools(session)
        if allowed is not None and name in registry.names() and name not in set(allowed):
            return (
                f"这个角色没有装配 {name}。要它请改 agents.toml，"
                "或把这件事派给装了它的角色。"
            )
    return f"unknown tool: {name}"

# 让模型写收尾总结时给的指令。
#
# 四段固定，写的是「用户还能接着做什么」：停在哪里、已经产出什么、还差什么、
# 下一轮从哪继续。第一版要求复述「这一轮做了什么」，实测写出来是一份过程报告，
# 而过程本来就在工具卡里，用户重读一遍得不到新东西。
WRAPUP_INSTRUCTION = (
    "这一轮任务还没有完成就停下了，停下来是因为：{reason}。\n"
    "现在写一份简报交给用户。第一段必须是「给你的答复」：直接回答用户这一轮问的事，"
    "能做多少答多少；没做完就把目前能确定的结论、还缺什么写清楚，"
    "不要让这一段变成纯粹的过程说明。\n"
    "然后写四段状态，每段一行短标题加正文：\n"
    "停在哪里：一句话，写清是步数用满、请求出错、工具连续失败还是被中断。\n"
    "已经产出：handle、文件路径、关键数字，最多 6 条；没有就写「没有新产物」。\n"
    "还没做完：对照任务状态里的待办，写 1–4 条。\n"
    "下一轮从这里继续：一句可执行的动作。\n"
    "路径与数字要照实写，写不准的宁可说「未产出」，不要编。\n"
    "不要用 Markdown 语法（不用 #、不用加粗、不用列表符号、不用表格）。\n"
    "不要向用户提问：停下来不是让用户做决定的时刻。\n"
    "只写简报本身，不要寒暄，不要复述这段指令，不要再调用工具。"
)

# 最后一步的提示。步数用满之前还有一次请求，这一次不带工具定义：
# 模型看到「还剩 1 步」仍会拿去调 close_topic 或再跑一个工具，真正想说的话
# 只能挤进收尾总结里（2026-11-20 那一轮，第 24 步还在调 task 与 close_topic）。
FINAL_STEP_INSTRUCTION = (
    "这是本次允许的最后一步，工具已经收回。不要再调用工具，"
    "把目前能确定的结论、产出与还差什么直接写给用户。"
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
        state_dir: Path | None = None,
        policy: PermissionPolicy | None = None,
        hooks: HookRunner | None = None,
        ledger: Any = None,
        memory: Any = None,
        viewer: Any = None,
        spawner: Any = None,
        workspace: Any = None,
        route_adapter: Any = None,
        validator_mode: str = "off",
        max_steps: int = MAX_STEPS,
        sar_mode: str | None = None,
    ) -> None:
        self.bus = bus
        self.provider = provider
        self.registry = registry
        self.config = config
        self.context_window = context_window
        self.workspace_root = workspace_root
        # 状态目录（`state.toml` / `ledgers/` / `connection.toml` 所在）。
        # 它**不在工作目录下**，而模型默认它在那儿——实测它会 `cd .vsagent`
        # 找东西，失败之后才去 `~`。所以这一段要写进每一步的上下文。
        self.state_dir = state_dir
        self.policy = policy or PermissionPolicy()
        self.hooks = hooks or HookRunner([], workspace_root)
        self.ledger = ledger
        # 跨会话的项目记忆。与登记表不同，它不随会话切换而变。
        self.memory = memory
        # 本地结构服务的状态。与记忆一样是**应用级**的：整个进程一个，
        # 不随会话切换而变，也不每轮重建。open_viewer 只从中读实际端口。
        self.viewer = viewer
        # 子 agent 派发器。应用级对象，由 app 装配、按会话注入给 dispatch_campaign。
        # 根会话的循环层拿得到它，子 agent 的注册表里没有这个工具，
        # 所以「子 agent 不能再派发」在装配层就成立，不靠工具自己判断。
        self.spawner = spawner
        # 科研工作区：与登记表一样是**按根会话共享**的一份，由 host 持有。
        # 循环层只负责把它转交给声明了 `workspace_argument` 的那几个工具；
        # 自己既不读也不写，所以这里是 None 也不影响其余工具。
        self.workspace = workspace
        # 路由适配器：意图 → 任务 → 能力计划。None 表示不接（子 agent 与测试）。
        self.route_adapter = route_adapter
        # 校验器模式：off / shadow / enforce。由应用层按环境变量取值。
        self.validator_mode = validator_mode
        # 搜索增强推理：off / shadow / enforce。默认按环境变量取。
        self.sar_mode = sar_mode if sar_mode in SAR_MODES else sar_mode_from_env()
        # 这一轮的答案状态。`None` 表示这一轮没有进入 SAR（DIRECT 或开关关闭）。
        self.answer_state: AnswerState | None = None
        self._sar_active = False
        # research 子会话轮首写先验，检索阶段只收集，轮末再评估整合。
        self._research_batch = False
        # 模型最后一步的正文先留在这里，核对完成后再一次性发布。
        # 只有 SAR 生效的轮次才用得到；其余轮次正文照常边生成边发。
        self._held_parts: list[tuple[Message, Part]] = []
        # 这一轮的 SAR 过程行，按阶段索引，证据行会原地更新。
        self._sar_parts: dict[str, Part] = {}
        # 被单通道压掉的检索结果。失败或轮末全部放开，原始结果回到上下文。
        self._sar_suppressed: list[tuple[Message, Part]] = []
        # 这一轮的计划与任务，供校验器读；每轮开始时清空。
        self._turn_plan: Any = None
        self._turn_task: TaskSpec | None = None
        # 这一轮的关键词提示（`intent.detect`）。在前置流程里算一次，过程记录与
        # 上下文里那一行用的是同一份，免得两处各扫一遍、还扫出两种结论。
        self._turn_hint: Any = None
        # 本轮的按需证据（检索回来的记忆、含关键行的工具结果）。
        #
        # 它是**一轮**的：每次发问之前清空。上一轮 recall 出来的记忆，
        # 到这一轮多半已经不相干，留着只会占证据层的额度。
        self.evidence = EvidenceStore()
        # 一轮最多问模型多少次。默认取模块常量，装配处可以按场景放宽或收紧。
        self.max_steps = max_steps
        self.cancelled = False
        # 收束阶段：剩 `CLOSING_STEPS` 步时打开，只放行收尾型工具。
        self._closing = False
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

        `remember` 为真且判定为允许时，把**本会话的所有工具调用**一次性放行
        （弹窗上的 a 就是这一条）。放行之后 Plan 限制、工作目录之外的访问与
        联网计费配额都不再拦，只有用户中断排在它前面；切换会话时清掉。
        """
        pending = self._pending_permissions.get(part_id)
        if pending is None or pending.future.done():
            return False
        if verdict is Verdict.ALLOW:
            if remember:
                self.policy.allow_all()
                decision = PermissionDecision(Verdict.ALLOW)
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

    def compact_session(self, session: Session) -> int:
        """手动把当前这一段封存掉，返回还剩几条原文要发。

        语义是「封存当前这一段」：不再按 `total - keep` 算要丢几条——那个口径
        按**条数**压，压掉的是最便宜的那几条，最贵的那条原样留着（P1-4）。
        现在整段封成一个主题，切点推到它的末尾，原文全部让位给摘要。

        与自动封存的区别只在**位置**：自动封存在轮末发生，封到这一轮的末尾；
        `/compact` 是用户主动要腾地方，封到当前能封的地方。

        所以「没有新的一段可封」不等于「什么都不用做」：带工具的轮次在轮末已经
        自动封存过，这里 `_seal_range` 会返回 `None`，但压缩位置仍要推到最后一个
        事件的切点。不推的话 `/compact` 就是个空操作——界面回「这一段已经压过了」，
        而那一轮的原文还在发。
        """
        total = len(session.messages)

        def remaining() -> int:
            """还剩几条原文会发出去。

            从**最靠后的边界**算起：装配时取 `compact_upto` 与事件切点的较晚者。
            只减 `compact_upto` 会把已经归档的那几段也报成「还要发」。
            """
            live = max(session.compact_upto, session.event_cut[0])
            return total - live

        if total <= KEEP_MESSAGES:
            return remaining()
        event = self._seal_range(
            session, kind="local", upto=event_boundary(session, total), force=True
        )
        if event is not None:
            target = event.cut_message
        elif session.events:
            target = session.events[-1].cut_message
        else:
            target = session.compact_upto
        session.compact_upto = max(session.compact_upto, target)
        session.sanitize_events()
        self.bus.publish("status.update", {"session_id": session.id})
        return remaining()

    async def stream_reply(self, session: Session, prompt: str) -> Message | None:
        """对一段用户输入生成流式回复。

        用户的输入已经作为一条 user Message 写进会话，这里的 prompt 只是同一个签名的占位。
        """
        self.cancelled = False
        self.last_exit_reason = None
        self._closing = False
        # 答案状态按轮清空：上一轮的说法与证据不进入这一轮。
        self.answer_state = None
        self._sar_active = False
        self._research_batch = defers_search_reasoning(session, self.sar_mode)
        self._held_parts = []
        self._sar_parts = {}
        self._sar_suppressed = []
        # 上一轮如果被中断，抑制标记可能留在状态文件里；新一轮没有证据层接手，
        # 先清掉，保证原文可见。
        self._release_stale_suppressed(session)
        # 证据是「本步相关」的，一轮开始时清空：上一轮 recall 出来的记忆，
        # 到这一轮多半已经不相干，留着只会占掉证据层的额度。
        self.evidence.clear()
        # 度量同理按轮清空：这一轮的每一步各记一条，上一轮的不混进来。
        self.context_history.clear()
        self.last_input_tokens = 0
        self.last_cached_tokens = 0
        self._summary_failures = 0
        # 上一轮如果是被中断的，它那一段的 Step 还在内存里：新消息进来时先封掉。
        # 不封的话这些步会跟着下一轮一起算进新的主题，流水的主题号就错位了。
        # 只封「真有没封存的步」的那一段：没有 pending 时按历史回填会把整份旧会话
        # 收成一个主题，那不是「补上一轮」，是改写历史。
        if session.pending_steps:
            self._seal_range(
                session,
                kind="local",
                upto=event_boundary(session, len(session.messages)),
            )
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
        self._turn_plan = None
        self._turn_task = None
        self._turn_hint = None
        # 每轮先退回「主 agent 可以自己干」：只有路由真的给出计划才置位。路由关上
        # 或解析失败时保持 False，否则新加的这一层会把旧路径一起锁死。
        await self._prepare_route(session, message)
        try:
            if self._research_batch:
                await self._begin_research_turn(session, message)
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
            # 整合与核对排在收尾总结之后、整轮收尾之前：核对要看的是最终
            # 交给用户的那一段正文；异常结束的轮次不整合，只把状态记成失败。
            await self._finalize_answer(session, message, exit_reason)
            # 整轮的停因记在会话上，供下一轮的复述使用。事件上那一份记的是
            # 「这一段是在什么结局下结束的」，两者回答的问题不同，都保留。
            session.last_exit_reason = exit_reason or ""
            # 轮末自动封存当前主题。这是**默认边界**，也是「记录不依赖模型判断」
            # 的落点——模型一次 close_topic 都没调，流水与摘要照样有。
            self._seal_range(
                session,
                kind="auto",
                upto=event_boundary(session, len(session.messages)),
                # 这一轮怎么停的随主题一起存下来：取消之后同样会走到这里，
                # 不存的话事后分不出「做完的」与「被停下的」。
                exit_reason=exit_reason or "",
            )
            # 轮末封存只切到消息边界；模型在最后一步调过 close_topic 时，
            # 切点落在最后一条消息中间，收尾总结就成了切点之后的一小段。
            # 并回上一条事件，界面上才不会多出一个只有一行答复的主题。
            absorb_tail(session)
        finally:
            # 兜底：中途抛错时，已经生成但还没发布的正文也要让用户看见。
            # 正常情况下由 `_finalize_answer` 发过一次，这里再调是空操作。
            self._flush_held()
            self._release_suppressed()
            self.last_latency_ms = int((time.time() - started) * 1000)
        # 中断那一条不收尾：`_finish` 会发 turn.done，而这一轮是用户按停的，
        # 界面已经在中断那一步回到可用状态了，再发一次收尾事件会把状态机推错。
        # 总结已经在上面留下了，所以「中断也有交代」不靠这一条。
        if self.cancelled:
            return message
        self._finish(session, message, step, elapsed_ms)
        self._note_delivery(session)
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
        self._check_delivery(session)
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
        if self._sar_active:
            # 回答状态与筛选后的证据各占多少，单独报一份：它们混在 TASK 与
            # EVIDENCE 两层里，不单列就看不出搜索增强这一步花了多少额度。
            metrics["answer_tokens"] = estimate_tokens(self._answer_text())
            metrics["sar_evidence_tokens"] = sum(
                estimate_tokens(item.detail)
                for item in self.evidence.items()
                if item.ref.startswith("sar:")
            )
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

    def _event_runtime(self, session: Session) -> EventRuntime:
        """造一份事件运行时。

        收口不另发请求（标题与结论由模型随调用给出），所以这里只需要会话本身。
        """
        return EventRuntime(session=session)

    def _profile_text(self) -> str:
        """用户习惯那一段；没装配记忆时是空串。

        与项目情况分开取：习惯只在用户改口时变，装在系统提示之后，
        一轮之内字节稳定；项目情况随问题变，装在工作区那头。
        """
        if self.memory is None:
            return ""
        return self.memory.profile_text()

    def _memory_text(self, session: Session) -> str:
        """项目情况那一段；没装配记忆时是空串。

        每次发问都现取一次：记忆可能在会话中途被 remember 改过，
        缓存一份的话要额外记得在哪里失效，不如现取。

        带查询词是这一期的新东西：1200 token 装不下全部记忆，装「与当前问题
        相关的那几条」比装「最近改过的那几条」有用得多。
        """
        if self.memory is None:
            return ""
        return self.memory.project_text(
            query=self._memory_query(session),
            context_paths=self._context_paths(),
        )

    def _context_paths(self) -> list[str]:
        """当前受体与最近几件产物的路径，用来判断一条记忆是不是贴着当前任务。

        登记表没装配时给空表：这时所有条目按相关度与新旧排，
        不会因为「当前路径取不到」把某一类全部压下去。
        """
        if self.ledger is None:
            return []
        items = list(self.ledger.entries())
        paths: list[str] = []
        current = self.ledger.current_receptor()
        if current is not None:
            for entry in items:
                if entry.handle == current and entry.path is not None:
                    paths.append(str(entry.path))
                    break
        for entry in reversed(items):
            if entry.path is None:
                continue
            text = str(entry.path)
            if text not in paths:
                paths.append(text)
            if len(paths) >= CONTEXT_PATH_LIMIT:
                break
        return paths

    async def _prepare_route(self, session: Session, message: Message) -> None:
        """在模型请求之前做一次意图解析与路由。

        三种模式见 `route_adapter.py`。这里只负责五件事：算一次关键词提示
        （`intent.detect`，与路由无关）、把结果写进 trace、在消息开头留一块可折叠的
        过程记录（`PartType.ROUTE`）、把任务规格记到会话上（shadow 也记，否则下一轮
        没有上下文）、把这一轮标记成「主 agent 只编排」。

        **工具清单不随计划变**：角色在 `agents.toml` 里定死了自己那一份，派发时由
        `coordinator` 交给子会话。装到根会话上等于让主 agent 自己去做领域动作，
        那正是这一层要避免的。

        过程记录先于模型请求写：用户看到「这一轮按什么理解」与「这一轮要交什么」
        不用等答复出来；解析失败也留一块，写明这一轮退回原路径的原因。

        任何异常都不往外抛：路由是新加的一层，不能成为新的故障点。
        """
        text = self._last_user_text(session)
        # 这三个字段只在内存里，每轮重写。`route_note` 是给模型的短提示，
        # `route_intent` 是解析结果的摘要，两者都进 TASK 层；
        # `route_blocked_reason` 由派发工具用来拦一次。
        session.route_note = ""
        session.route_intent = ""
        session.route_blocked_reason = ""
        if text.strip():
            # 关键词提示与路由无关：路由关掉、或者是子会话，上下文里那一行照样要给。
            self._turn_hint = intent.detect(text)
        if self.route_adapter is None or session.depth > 0:
            return
        if not text.strip():
            return
        current = None
        if session.task_spec:
            try:
                current = TaskSpec.from_dict(session.task_spec)
            except Exception as error:  # 旧数据或手工改坏的状态：当作没有当前任务
                self.bus.publish(
                    "route.error",
                    {"session_id": session.id, "error": f"读不出当前任务规格：{error}"},
                )
        try:
            decision = await self.route_adapter.prepare(
                text,
                current=current,
                session=session,
                ledger=self.ledger,
                workspace=self.workspace,
            )
        except Exception as error:
            self.bus.publish("route.error", {"session_id": session.id, "error": str(error)})
            return
        if decision.mode == "off":
            # 关掉时不写 trace：免得每一轮多一条什么也没发生的记录，把日志冲淡。
            return
        # 这一块是给人看的过程记录：两种界面都按折叠块画，模型上下文里没有它。
        hint_line = intent.summary(self._turn_hint) if self._turn_hint is not None else ""
        self._publish(
            message,
            Part(type=PartType.ROUTE, text=describe_route(decision, hint=hint_line)),
        )
        if decision.error:
            self.bus.publish("route.error", {"session_id": session.id, "error": decision.error})
        plan = decision.plan
        self.bus.publish(
            "route.plan",
            {
                "session_id": session.id,
                "mode": decision.mode,
                "relation": decision.relation,
                "action": decision.action,
                "task_id": decision.task.task_id if decision.task else "",
                "revision": decision.task.revision if decision.task else 0,
                "operations": list(decision.intent.operations) if decision.intent else [],
                "agents": list(plan.selected_agents) if plan else [],
                "verdict": decision.verdict.decision if decision.verdict else "",
                "warnings": list(decision.warnings),
            },
        )
        if decision.error:
            # 出错就什么都不改：宁可这一轮退回原来的路径，也不要把一份半成品
            # 计划写进会话状态，下一轮再按它继续。
            if (
                getattr(self.route_adapter, "mode", "shadow") == "enforce"
                and decision.verdict is not None
                and decision.verdict.decision == "reject"
            ):
                session.route_blocked_reason = decision.error
                session.route_note = (
                    f"这一轮路由明确拒绝：{decision.error}。不要派发，先向用户说明原因。"
                )
            return
        if decision.clarification:
            if getattr(self.route_adapter, "mode", "shadow") == "enforce":
                session.pending_intent = {
                    "intent": decision.intent.to_dict() if decision.intent else {},
                    "clarification": list(decision.clarification),
                    "task_id": decision.task.task_id if decision.task else (current.task_id if current else ""),
                    "goal": decision.intent.goal if decision.intent else "",
                }
                session.route_note = (
                    "本轮先向用户澄清："
                    + "；".join(decision.clarification)
                    + "。不要派发。"
                )
            return
        if decision.action == "status_query":
            session.route_note = "本轮只回答当前进度，不要重新编译任务，也不要派发新的角色。"
            return
        if decision.action == "ask_result":
            session.route_note = "本轮只解释已有结果与产物，不要重新计算，也不要派发新的角色。"
            return
        if decision.action == "cancel":
            if getattr(self.route_adapter, "mode", "shadow") == "enforce":
                if self.spawner is not None and hasattr(self.spawner, "cancel_campaign"):
                    try:
                        self.spawner.cancel_campaign(session.id)
                    except Exception as error:  # noqa: BLE001 - 取消失败也要给模型一句说明
                        session.route_note = f"取消战役时出错：{error}"
                if current is not None:
                    cancelled = current.with_updates(status="cancelled")
                    session.task_spec = cancelled.to_dict()
                session.pending_intent = None
            session.route_note = (
                session.route_note
                or "本轮用户要求终止任务。说明已经停止的部分；不要再派发。"
            )
            return
        if decision.action == "chat" or decision.relation == "none":
            session.route_note = "本轮是闲聊或概念问答，不要建任务，也不要派发。"
            return
        if decision.task is not None:
            previous = session.task_spec or {}
            session.task_spec = decision.task.to_dict()
            self._note_task_change(session, previous, decision.task)
            # 解析结果压成一页摘要，进 TASK 层。模型据此核对系统理解与用户原话；
            # 派发工具仍然从 `task_spec` 读完整规格，不依赖这一段。
            session.route_intent = render_intent(
                decision.intent,
                decision.task,
                decision.plan,
                mode=decision.mode,
            )
        session.pending_intent = None
        self._turn_plan = decision.plan
        self._turn_task = decision.task
        session.route_note = {
            "new": "本轮是新任务；按当前任务规格执行。",
            "continue": "本轮延续当前任务；不要新建任务号。",
            "modify": "本轮修改了任务目标；已有产物可以继续使用。",
            "compare": "本轮是比较任务：只比较已有对象或结果，不要重跑上游。",
            "resume": "本轮恢复旧任务；沿用原任务号与已有产物。",
        }.get(decision.relation, "")

    def _note_task_change(self, session: Session, previous: dict, task: Any) -> None:
        """任务规格变了，把战役状态推到「定规格」。

        两种情况分开：

        - 任务号变了 = 另一件事，前面那一轮的进度不再适用，直接重置；
        - 同一个任务改了要求 = 原来那一轮被打断。能前进就前进；正在研究中
          退不回去，这时不硬改——那份计划该不该作废由计划戳记判（`PlanStamp`），
          状态机只回答「现在在哪一格」。
        """
        from vsagent.core.agent.execution import campaign_state

        if previous == task.to_dict():
            return
        old_id = str(previous.get("task_id") or "")
        if old_id and old_id != task.task_id:
            session.campaign_state = campaign_state.SPECIFICATION
            return
        state, _reason = campaign_state.soft_advance(
            session.campaign_state, campaign_state.SPECIFICATION
        )
        session.campaign_state = state

    # ------------------------------------------------------------------
    # 搜索增强推理：查询前、工具结果回写后、最终答复前三个入口
    # ------------------------------------------------------------------

    def _sar_ask(self, message: Message):
        """造一个「问一次模型」的闭包，用量记在当前这条助手消息上。"""

        async def ask(messages: list[dict[str, Any]]) -> str:
            return await self._ask_plain(message, messages)

        return ask

    async def _ask_plain(self, message: Message, messages: list[dict[str, Any]]) -> str:
        """跑一次不带工具的请求，只返回正文。先验答案、整合与核对都走它。"""
        chunks: list[str] = []
        async for event in self.provider.stream(messages, [], ""):
            if self.cancelled:
                break
            if event.type is ProviderEventType.TEXT_DELTA:
                chunks.append(event.text)
            elif event.type is ProviderEventType.USAGE:
                self._add_usage(message, event.usage)
        return "".join(chunks).strip()

    def _sar_failed(
        self,
        session: Session,
        message: Message,
        error: Exception,
        *,
        part_type: PartType = PartType.ERROR,
    ) -> None:
        """SAR 某一步走不通：保存失败状态，放开原始结果，退回原路径。

        轮首失败传 `SAR`：这一行不是模型请求出错，写成 `ERROR` 会让循环
        把它当成「模型报错停下」，整轮按异常结束处理。
        """
        state = self.answer_state
        if state is not None:
            state.status = "failed"
            try:
                save_answer(state, self.state_dir, session.id)
            except Exception:
                # 写盘失败不遮住真正的错误；事件里仍然有原因。
                pass
        self._release_suppressed()
        self.answer_state = None
        self._sar_active = False
        self.bus.publish(
            "search_reasoning.error",
            {"session_id": session.id, "error": str(error)},
        )
        self._publish(
            message,
            Part(
                type=part_type,
                text=f"检索增强没有走通，这一轮按原路径回答：{error}",
            ),
        )

    def _sar_line(
        self, message: Message, stage: str, text: str, **extra: Any
    ) -> None:
        """写一条 TUI 可见的过程行；同一阶段再次调用时原地更新。"""
        part = self._sar_parts.get(stage)
        if part is None:
            part = Part(
                type=PartType.SAR,
                text=text,
                tool_meta={"stage": stage, **extra},
            )
            self._sar_parts[stage] = part
            self._publish(message, part)
            return
        part.text = text
        part.tool_meta.update(extra)
        self.bus.publish(
            "part.update",
            {
                "session_id": message.session_id,
                "message_id": message.id,
                "part_id": part.id,
            },
        )

    def _pre_tool_text(self, message: Message) -> str:
        """当前助手消息里、第一个工具调用之前写下的内容。"""
        chunks: list[str] = []
        for part in message.parts:
            if part.type is PartType.TOOL:
                break
            if part.type is PartType.TEXT and part.text:
                chunks.append(part.text)
        return "".join(chunks).strip()

    def _prior_answer_text(self, session: Session, message: Message) -> str:
        """上一轮助手答复，截短之后给先验答案当背景。"""
        for item in reversed(session.messages):
            if item is message or item.role != "assistant":
                continue
            text = " ".join(
                part.text
                for part in item.parts
                if part.type is PartType.TEXT and part.text
            ).strip()
            if text:
                return text[:800]
        return ""

    @staticmethod
    def _trigger_parts(calls: list[Part]) -> list[Part]:
        """这一步里能启动 SAR 的检索结果：文献、网页、语料，且调用成功。"""
        return [
            part
            for part in calls
            if is_trigger_tool(part.tool_name)
            and part.tool_status is ToolStatus.COMPLETED
        ]

    async def _start_from_retrieval(
        self,
        session: Session,
        message: Message,
        trigger: Part,
        count: int,
        step: int,
    ) -> bool:
        """第一次检索结果返回：先写先验答案，再让结果进入证据层。"""
        tool_name = trigger.tool_name or "检索"
        self.bus.publish(
            sar_events.RETRIEVAL_OBSERVED,
            sar_events.observed(session.id, tool_name, step, count, count),
        )
        self._sar_line(
            message,
            "retrieval",
            f"检索 · {tool_name} 返回 {count} 条候选",
            tool=tool_name,
            step=step,
            count=count,
        )
        question = self._last_user_text(session)
        if not question.strip():
            return False
        try:
            state = await capture_prior(
                question,
                self._sar_ask(message),
                task_text=self._task_text(session),
                narration=self._pre_tool_text(message),
                recent=self._prior_answer_text(session, message),
                memory=self._memory_text(session),
                trigger_tool=tool_name,
                trigger_step=step,
            )
        except SearchReasoningError as error:
            self._sar_failed(session, message, error)
            return False
        except Exception as error:
            self._sar_failed(session, message, error)
            return False
        self.answer_state = state
        self._sar_active = True
        self.bus.publish(
            sar_events.ANSWER_INITIALIZED, sar_events.initialized(state, session.id)
        )
        save_answer(state, self.state_dir, session.id)
        self._sar_line(
            message,
            "baseline",
            f"先验答案 · {len(state.initial_claims)} 条说法，"
            f"{len(state.knowledge_gaps)} 个知识缺口",
            claims=len(state.initial_claims),
            gaps=len(state.knowledge_gaps),
            detail=self._prior_detail(state),
        )
        return True

    def _turn_tool_parts(self, session: Session, message: Message) -> list[Part]:
        """这一轮已经完成的工具结果，从最后一条用户消息之后算起。

        research 可能在轮末换过一条助手消息（Stop 钩子拦住后另起一条），
        只看最后一条会漏掉前面几步的检索。
        """
        start = 0
        for index in range(len(session.messages) - 1, -1, -1):
            if session.messages[index].role == "user":
                start = index + 1
                break
        parts: list[Part] = []
        for item in session.messages[start:]:
            if item.role != "assistant":
                continue
            parts.extend(
                part
                for part in item.parts
                if part.type is PartType.TOOL
                and part.tool_status is ToolStatus.COMPLETED
            )
        return parts

    async def _begin_research_turn(
        self, session: Session, message: Message
    ) -> bool:
        """research 轮首写下先验答案，作为本轮回答的基线。

        检索还没开始，输入只有问题、任务、记忆与上一轮答复。先验与本轮
        证据无关，后面的评估只看它需要补什么。
        """
        question = self._last_user_text(session)
        if not question.strip():
            return False
        try:
            state = await capture_prior(
                question,
                self._sar_ask(message),
                task_text=self._task_text(session),
                recent=self._prior_answer_text(session, message),
                memory=self._memory_text(session),
            )
        except SearchReasoningError as error:
            self._sar_failed(session, message, error, part_type=PartType.SAR)
            return False
        except Exception as error:
            self._sar_failed(session, message, error, part_type=PartType.SAR)
            return False
        self.answer_state = state
        self._sar_active = True
        self.bus.publish(
            sar_events.ANSWER_INITIALIZED, sar_events.initialized(state, session.id)
        )
        save_answer(state, self.state_dir, session.id)
        self._sar_line(
            message,
            "baseline",
            f"先验答案 · {len(state.initial_claims)} 条说法，"
            f"{len(state.knowledge_gaps)} 个知识缺口",
            claims=len(state.initial_claims),
            gaps=len(state.knowledge_gaps),
            detail=self._prior_detail(state),
        )
        return True

    async def _finish_research_batch(
        self, session: Session, message: Message
    ) -> bool:
        """轮末把整轮检索结果并进先验答案，评估一次。

        检索过程中不评估；这里统一去重、评估并同步证据层，整合与核对
        由收尾继续跑。
        """
        state = self.answer_state
        if state is None:
            return False
        tools = self._turn_tool_parts(session, message)
        initial = items_from(tools)
        if not initial:
            return True
        trigger = next((part for part in tools if is_trigger_tool(part.tool_name)), None)
        tool_name = (trigger.tool_name if trigger is not None else "") or "检索汇总"
        self.bus.publish(
            sar_events.RETRIEVAL_OBSERVED,
            sar_events.observed(session.id, tool_name, 0, len(initial), len(initial)),
        )
        self._sar_line(
            message,
            "retrieval",
            f"检索 · 本轮 {len(tools)} 次调用，{len(initial)} 条候选，轮末统一整理",
            tool=tool_name,
            count=len(initial),
        )
        added = merge(initial, state)
        state.status = "searching"
        self.bus.publish(
            sar_events.EVIDENCE_COLLECTED,
            sar_events.collected(state, session.id, added, len(state.evidence_pool)),
        )
        try:
            counts = await assess(
                state, question=state.question, ask=self._sar_ask(message), refine=True
            )
        except SearchReasoningError as error:
            self._sar_failed(session, message, error)
            return False
        except Exception as error:
            self._sar_failed(session, message, error)
            return False
        self.bus.publish(
            sar_events.EVIDENCE_ASSESSED, sar_events.assessed(state, session.id, counts)
        )
        self._sync_sar_evidence()
        selected = len(select_for_context(state))
        self._sar_line(
            message,
            "evidence",
            f"证据 · {len(state.evidence_pool)} 条候选，{selected} 条进入上下文"
            f"（支持 {counts.get('supporting', 0)}、补充 {counts.get('supplementary', 0)}、"
            f"冲突 {counts.get('contradictory', 0)}）",
            total=len(state.evidence_pool),
            selected=selected,
            relations=dict(counts),
        )
        save_answer(state, self.state_dir, session.id)
        return True

    def _suppress_retrieval(self, message: Message, step: int) -> None:
        """把文献、网页、语料的原始结果挡在模型上下文之外。

        数据库与计算结果不压：它们的表格与分数需要按原文看。原始正文仍留在
        Part 上，界面与 `read_turn` 都能取回。
        """
        for part in message.parts:
            if part.type is not PartType.TOOL:
                continue
            if part.tool_status is not ToolStatus.COMPLETED:
                continue
            if not is_trigger_tool(part.tool_name):
                continue
            meta = dict(part.tool_meta or {})
            if meta.get("sar_suppressed"):
                continue
            count = len(items_from([part]))
            if count <= 0:
                continue
            meta["sar_suppressed"] = True
            meta["sar_evidence_count"] = count
            meta["sar_model_output"] = (
                f"（这次检索返回 {count} 条候选，已按说法筛选后放进证据层；"
                f"原文用 read_turn step={step} 取回）"
            )
            part.tool_meta = meta
            self._sar_suppressed.append((message, part))
            self.bus.publish(
                "part.update",
                {
                    "session_id": message.session_id,
                    "message_id": message.id,
                    "part_id": part.id,
                },
            )

    def _release_suppressed(self) -> None:
        """SAR 没走通或这一轮结束：把原始结果放回模型上下文。"""
        released, self._sar_suppressed = self._sar_suppressed, []
        for message, part in released:
            meta = dict(part.tool_meta or {})
            meta.pop("sar_suppressed", None)
            meta.pop("sar_evidence_count", None)
            meta.pop("sar_model_output", None)
            part.tool_meta = meta
            self.bus.publish(
                "part.update",
                {
                    "session_id": message.session_id,
                    "message_id": message.id,
                    "part_id": part.id,
                },
            )

    @staticmethod
    def _release_stale_suppressed(session: Session) -> None:
        """清掉上一轮遗留的抑制标记。

        正常轮末会解除；进程中途退出时标记会留在状态文件里。新一轮开始时
        没有证据层接手这些结果，留着标记会让模型永远看不到原文。
        """
        for message in session.messages:
            for part in message.parts:
                if part.type is not PartType.TOOL:
                    continue
                meta = part.tool_meta if isinstance(part.tool_meta, dict) else {}
                if not meta.get("sar_suppressed"):
                    continue
                cleaned = dict(meta)
                cleaned.pop("sar_suppressed", None)
                cleaned.pop("sar_evidence_count", None)
                cleaned.pop("sar_model_output", None)
                part.tool_meta = cleaned

    async def _process_search_results(
        self, session: Session, message: Message, calls: list[Part], step: int
    ) -> None:
        """检索结果回写之后：第一次触发先写先验答案，然后整理证据。"""
        if self.sar_mode == "off" or self._research_batch:
            return
        state = self.answer_state
        if state is None:
            trigger = self._trigger_parts(calls)
            if not trigger:
                return
            trigger_items = items_from([trigger[0]])
            if not trigger_items:
                return
            # 初始扫描把同一轮早先的计算结果一起收进来，避免漏掉触发之前的对接或分析。
            completed = [
                part
                for part in message.parts
                if part.type is PartType.TOOL
                and part.tool_status is ToolStatus.COMPLETED
            ]
            initial = items_from(completed)
            if not initial:
                return
            started = await self._start_from_retrieval(
                session, message, trigger[0], len(trigger_items), step
            )
            if not started:
                return
            state = self.answer_state
            if state is None:
                return
            added = merge(initial, state)
            self._suppress_retrieval(message, step)
        else:
            added = merge(items_from(calls), state)
            if added:
                self._suppress_retrieval(message, step)
            # 多 Agent：子会话报告带着证据对象，状态已经存在时并进同一场证据池。
            # 认 `tool_meta["evidence"]`，不认具体工具名：派发结果与以后的
            # 其它汇总工具都可能带证据，按名字白名单会让新工具悄悄漏掉。
            for part in calls:
                if not (part.tool_meta or {}).get("evidence"):
                    continue
                for row in (part.tool_meta or {}).get("evidence") or []:
                    if not isinstance(row, dict):
                        continue
                    try:
                        item = SearchEvidence.from_dict(row)
                    except SearchReasoningError:
                        continue
                    if is_duplicate(item, state.evidence_pool):
                        continue
                    state.add_evidence(item)
                    added += 1

        if not added:
            return
        state.status = "searching"
        self.bus.publish(
            sar_events.EVIDENCE_COLLECTED,
            sar_events.collected(state, session.id, added, len(state.evidence_pool)),
        )
        try:
            counts = await assess(
                state, question=state.question, ask=self._sar_ask(message), refine=False
            )
        except SearchReasoningError as error:
            self._sar_failed(session, message, error)
            return
        except Exception as error:
            self._sar_failed(session, message, error)
            return
        self.bus.publish(
            sar_events.EVIDENCE_ASSESSED, sar_events.assessed(state, session.id, counts)
        )
        self._sync_sar_evidence()
        selected = len(select_for_context(state))
        self._sar_line(
            message,
            "evidence",
            f"证据 · {len(state.evidence_pool)} 条候选，{selected} 条进入上下文"
            f"（支持 {counts.get('supporting', 0)}、补充 {counts.get('supplementary', 0)}、"
            f"冲突 {counts.get('contradictory', 0)}）",
            total=len(state.evidence_pool),
            selected=selected,
            relations=dict(counts),
        )
        save_answer(state, self.state_dir, session.id)

    def _sync_sar_evidence(self) -> None:
        """把筛选后的证据放进本步的证据层，最多五条。

        完整证据池留在答案状态文件里；上下文只带当前说法需要的那几条。
        每次覆盖同一批引用，避免多步之后同一批结果在证据层堆成几十条。
        """
        state = self.answer_state
        if state is None:
            return
        self.evidence.drop("sar:")
        for item in select_for_context(state):
            assessment = assessment_for(state, item.id)
            relation = assessment.relation if assessment else "supplementary"
            level = assessment.support_level if assessment else "contextual"
            claims = "、".join(assessment.related_claim_ids) if assessment else ""
            title = item.title or item.content[:60]
            summary = (
                f"{item.source}｜{item.source_type}｜{relation}/{level}"
                f"→{claims or '未挂说法'}｜{title}"
            )
            self.evidence.add(f"sar:{item.id}", summary, item.content[:600])

    @staticmethod
    def _prior_detail(state: AnswerState) -> str:
        """先验答案展开后的正文：答复、说法与知识缺口。界面默认收起这一块。"""
        lines = [state.initial_answer]
        if state.initial_claims:
            lines.extend(["", "说法："])
            lines.extend(
                f"- {claim.id}（{claim.category}）：{claim.content}"
                for claim in state.initial_claims
            )
        if state.knowledge_gaps:
            lines.extend(["", "知识缺口：" + "；".join(state.knowledge_gaps)])
        return "\n".join(lines)

    def _answer_text(self) -> str:
        """把答案状态的摘要渲染成 TASK 层的一段。没有状态时是空串。"""
        state = self.answer_state
        if not self._sar_active or state is None:
            return ""
        snap = state.snapshot(answer_limit=400, claim_limit=6, gap_limit=5)
        origin = "轮首建立" if self._research_batch else "第一次检索结果返回前建立"
        lines = [f"本轮回答基线（{origin}，供对照）"]
        lines.append(f"先验答案：{snap['initial_answer']}")
        if snap["claims"]:
            lines.append("说法：")
            for item in snap["claims"]:
                lines.append(
                    f"- {item['id']}（{item['category']}/{item['status']}）：{item['content']}"
                )
        if snap["knowledge_gaps"]:
            lines.append("知识缺口：" + "；".join(snap["knowledge_gaps"]))
        lines.append(
            f"已收证据 {snap['evidence_count']} 条，已评估 {snap['assessment_count']} 条，"
            f"冲突 {snap['conflict_count']} 组。"
        )
        text = "\n".join(lines)
        if len(text) > SAR_ANSWER_LIMIT:
            text = text[: SAR_ANSWER_LIMIT - 1] + "…"
        return text

    async def _finalize_answer(
        self, session: Session, message: Message, exit_reason: str | None
    ) -> None:
        """整合、核对，然后决定交给用户的是哪一份正文。

        `shadow` 照常跑整合与核对，但最终正文仍是模型自己写的那一份；
        `enforce` 用整合并核对过的正文替换草稿。异常结束的轮次不整合。
        """
        if self.sar_mode == "off":
            return
        if exit_reason:
            state = self.answer_state
            if state is not None:
                state.status = "failed"
                save_answer(state, self.state_dir, session.id)
            if self._sar_active or self._research_batch:
                # 交代已经由收尾总结给了。这里再把压着的草稿放出去，用户看到的
                # 最后一段会是一句「本轮写入：…」的半截话，总结被挤到它前面。
                self._drop_held()
                self._release_suppressed()
            return

        if self._research_batch:
            if self.answer_state is None:
                # 轮首先验没有建立：这一轮按原路径回答，不在轮末补建。
                self._flush_held()
                self._release_suppressed()
                return
            if not await self._finish_research_batch(session, message):
                # 证据评估没有走通：草稿照原样交给用户。
                self._flush_held()
                self._release_suppressed()
                return

        state = self.answer_state
        if not self._sar_active or state is None:
            self._flush_held()
            return

        draft = "".join(
            part.text for owner, part in self._held_parts if owner is message and part.text
        ).strip()
        if not draft:
            draft = "".join(
                part.text for part in message.parts if part.type is PartType.TEXT
            ).strip()
        limits = list(session.task_state.constraints)
        if session.task_spec:
            try:
                limits.extend(
                    item for item in TaskSpec.from_dict(session.task_spec).constraints
                    if item not in limits
                )
            except Exception as error:
                # 旧数据或手工改坏的任务规格：读不出时记一条事件，不因此拦住这一轮。
                self.bus.publish(
                    "search_reasoning.error",
                    {"session_id": session.id, "error": f"任务规格读不出来：{error}"},
                )

        if not select_for_context(state):
            # 这一轮没有收到可用证据：不调用整合，保留原答案，只做机械核对。
            state.final_answer = draft
            state.status = "final"
            report = await verify_and_repair(state, limits=limits, ask=None)
            self.bus.publish(
                sar_events.ANSWER_VERIFIED,
                sar_events.verified(state, session.id, report.passed, report.issues),
            )
            self._sar_line(
                message,
                "verify",
                "核对 · 通过" if report.passed else f"核对 · {len(report.issues)} 处待核实",
                passed=report.passed,
                issues=len(report.issues),
            )
            self._publish_final(session, message, state, draft, report)
            return

        ask = self._sar_ask(message)
        try:
            if not self._research_batch:
                # 收尾时允许模型复核规则判不准的证据；这一步只做一次。
                counts = await assess(
                    state, question=state.question, ask=ask, refine=True
                )
                self.bus.publish(
                    sar_events.EVIDENCE_ASSESSED,
                    sar_events.assessed(state, session.id, counts),
                )
                selected = len(select_for_context(state))
                self._sar_line(
                    message,
                    "evidence",
                    f"证据 · {len(state.evidence_pool)} 条候选，{selected} 条进入上下文"
                    f"（支持 {counts.get('supporting', 0)}、补充 {counts.get('supplementary', 0)}、"
                    f"冲突 {counts.get('contradictory', 0)}）",
                    total=len(state.evidence_pool),
                    selected=selected,
                    relations=dict(counts),
                )
            actions, _notes = await integrate(state, ask, limits=limits)
            self.bus.publish(
                sar_events.CLAIM_UPDATED, sar_events.updated(state, session.id, actions)
            )
            changed = (
                actions.get("append", 0)
                + actions.get("strengthen", 0)
                + actions.get("revise", 0)
            )
            self._sar_line(
                message,
                "integrate",
                f"整合 · 修改 {changed} 条，保留 {actions.get('retain', 0)} 条，"
                f"冲突 {len(state.conflicts)} 组",
                actions=dict(actions),
                conflicts=len(state.conflicts),
            )
            report = await verify_and_repair(state, limits=limits, ask=ask)
        except SearchReasoningError as error:
            self._sar_failed(session, message, error)
            self._flush_held()
            return
        except Exception as error:
            self._sar_failed(session, message, error)
            self._flush_held()
            return

        self.bus.publish(
            sar_events.ANSWER_VERIFIED,
            sar_events.verified(state, session.id, report.passed, report.issues),
        )
        self._sar_line(
            message,
            "verify",
            "核对 · 通过" if report.passed else f"核对 · {len(report.issues)} 处待核实",
            passed=report.passed,
            issues=len(report.issues),
        )
        if self.sar_mode == "shadow":
            # 观察模式：整合与核对的结果写进文件与事件，正文仍是模型原稿。
            self._publish_final(session, message, state, draft, report)
            return
        final = state.final_answer.strip() or draft
        if not report.passed:
            # 修过一轮仍不通过：把不确定处写清楚，交给用户判断。
            notes = "；".join(report.texts()[:3])
            if notes:
                final = f"{final}\n\n（未核实的部分：{notes}）"
        self._replace_held(message, final)
        self._publish_final(session, message, state, final, report)

    def _publish_final(
        self,
        session: Session,
        message: Message,
        state: AnswerState,
        published: str,
        report: Any,
    ) -> None:
        """收尾：发布正文、写答案状态、发最后一条事件。"""
        state.final_answer = state.final_answer or published
        state.status = "final"
        self._flush_held()
        # 这一轮结束，原始检索结果放回上下文；下一轮追问还能看到原文。
        self._release_suppressed()
        save_answer(state, self.state_dir, session.id)
        self.bus.publish(
            sar_events.ANSWER_FINALIZED,
            {
                **sar_events.finalized(state, session.id, len(published)),
                "shadow": self.sar_mode != "enforce",
                "repaired": bool(getattr(report, "repaired", False)),
                "passed": bool(getattr(report, "passed", False)),
            },
        )

    def _replace_held(self, message: Message, text: str) -> None:
        """丢掉草稿，发布整合后的正文。"""
        held, self._held_parts = self._held_parts, []
        for owner, part in held:
            owner.parts = [item for item in owner.parts if item is not part]
        self._publish(message, Part(type=PartType.TEXT, text=text))

    def _note_delivery(self, session: Session) -> None:
        """这一轮回答已经发出去了：收齐的结果就此交付。

        只在「汇总结果」那一格之后推进，普通追问不动状态机。复核与交付连着走：
        回答发出之前已经没有别的动作可做，两格记的是同一件事的两面。
        复核本身不是这段代码在做——它是主 Agent 在正文里做的判断，这里只记账。
        """
        from vsagent.core.agent.execution import campaign_state

        state = session.campaign_state
        if state != campaign_state.INTEGRATION:
            return
        for target in (campaign_state.CRITICAL_REVIEW, campaign_state.DELIVERY):
            next_state, _reason = campaign_state.soft_advance(session.campaign_state, target)
            session.campaign_state = next_state

    def _check_delivery(self, session: Session) -> None:
        """轮末的交付验收：只记录，不阻断。

        回答已经发出去了，这里能做的只有把"少交了哪一件"写进 trace。真要阻断，
        得再来一轮补交，那是另一件事（见 `validators/delivery.py` 的说明）。
        """
        if self.validator_mode == "off" or self._turn_task is None:
            return
        from vsagent.core.agent.validators import check_delivery
        from vsagent.core.agent.validators.output_contract import unverifiable

        findings = check_delivery(
            self._turn_task, ledger=self.ledger, workspace=self.workspace_root
        )
        names = list(self._turn_task.required_outputs)
        self.bus.publish(
            "validator.delivery",
            {
                "session_id": session.id,
                "mode": self.validator_mode,
                "task_id": self._turn_task.task_id,
                "outputs": names,
                "manual": unverifiable(names),
                "findings": [item.as_dict() for item in findings],
            },
        )

    def _validate_tool(self, session: Session, name: str) -> Any:
        """按当轮计划与任务判一次。`off` 时不做任何事，连对象都不构造。"""
        if self.validator_mode == "off":
            return check_tool(name)  # 空计划、空任务：必然没有发现
        task = self._turn_task
        if task is None and session.task_spec:
            try:
                task = TaskSpec.from_dict(session.task_spec)
            except Exception:
                task = None
        from vsagent.core.agent.roles.agents import allowed_tools

        return check_tool(
            name,
            allowed=allowed_tools(session, self.workspace_root),
            task=task,
        )

    def _intent_text(self, session: Session) -> str:
        """按最后一条用户消息给一行「可能在问哪几类来源」。

        只在有命中时返回，且只点名来源、不点名工具：工具名写进提示等于在
        提示里存一份工具清单，工具改名或调整分组之后就过期了。

        提示在前置流程里已经算过（过程记录那一块用的就是它），这里直接用；
        没算过（没装配路由、或者是子会话以前的调用）就现扫一遍。
        """
        hint = self._turn_hint or intent.detect(self._last_user_text(session))
        text = intent.render(hint)
        if text:
            self.bus.publish(
                "intent.hint",
                {
                    "session_id": session.id,
                    "sources": list(hint.sources),
                    "matched": list(hint.matched),
                },
            )
        return text

    @staticmethod
    def _last_user_text(session: Session) -> str:
        """最后一条用户消息里的文字；没有时是空串。

        记忆检索与来源提示读的是同一段，共用一个函数：各写一份的话，
        两边的「最后一条」迟早不是同一条。
        """
        for message in reversed(session.messages):
            if message.role != "user":
                continue
            text = " ".join(
                part.text
                for part in message.parts
                if part.type is PartType.TEXT and part.text
            )
            if text.strip():
                return text.strip()
        return ""

    @classmethod
    def _memory_query(cls, session: Session) -> str:
        """检索词：最后一条用户消息 + 任务目标。

        只看最后一条：更早的用户消息已经由 RECENT 或事件代表，
        而记忆要回答的是「现在这件事需要知道什么」。
        """
        pieces: list[str] = []
        text = cls._last_user_text(session)
        if text:
            pieces.append(text)
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

    def _task_with_answer(self, session: Session) -> str:
        """任务状态加上本轮回答状态，合成 TASK 层的一段。

        两者回答的是同一类问题：这一步要做什么、已经立了哪些说法。放在一段里
        减少标题数；回答状态为空时原样返回任务状态。
        """
        return "\n\n".join(
            piece for piece in (self._task_text(session), self._answer_text()) if piece
        )

    def _env_text(self) -> str:
        """易变区的运行环境：日期、工作目录、状态目录。

        日期每天变一次，所以只能放这一头；平台是常量，放在 `PREFIX` 里
        （见 `prompt.platform_note`）。
        """
        return environment_block(self.workspace_root, self.state_dir)

    def _digest_text(self) -> str:
        """工作区索引：当前受体 + 最近几个产物的 handle；没装配登记表时是空串。

        每次发问现取：上一步刚登记的产物这一步就该出现。模型正是在「隔了几步」之后
        才记串编号的——那时原来的工具结果已经被清走，它手上只剩记忆。
        """
        digest = getattr(self.ledger, "digest", None)
        return digest() if callable(digest) else ""

    def tool_specs(self, session: Session) -> list[dict[str, Any]]:
        """这一步该发出去的工具定义：按角色名单过滤。

        名单来自 `agents.toml`（见 `roles/agents.py`），会话建立时定稿、运行当中
        不变。名单外的工具连名字都不出现，模型不会去调它不存在的东西；真去调时
        执行处还会按同一份名单拒绝（`tool_guard`）。
        """
        from vsagent.core.agent.roles.agents import allowed_tools

        only = allowed_tools(session, self.workspace_root)
        return self.registry.specs(only=only)

    def _assemble(
        self,
        session: Session,
        prefix: str,
        with_tools: bool = True,
        steps_left: int | None = None,
        hint_text: str = "",
    ) -> ContextBundle:
        """按分层装配这一步发给模型的消息。

        工具定义每一步都要全量重发，因此算进预算——不算的话，工具一多，
        真正留给对话的额度就被悄悄吃掉了。收尾总结那一条请求不带工具，
        所以传 `with_tools=False`，免得白占一份额度。

        `steps_left` 写进尾部的进度提示。模型看得见「还剩几步」才会为收尾
        留额度；只说「已经走了几步」时，它会一路做到底。

        证据层现取一份：工具是上一步跑的，这一步装配时才看得见它们留下了什么。
        """
        return ContextAssembler(self.context_window).build(
            session,
            prefix,
            tools=self.tool_specs(session) if with_tools else [],
            memory_text=self._memory_text(session),
            profile_text=self._profile_text(),
            hint_text=hint_text,
            task_text=self._task_with_answer(session),
            digest_text=self._digest_text(),
            env_text=self._env_text(),
            evidence=self.evidence.items(),
            steps_left=steps_left,
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
        hint_text = self._intent_text(session)
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
            steps_left = self.max_steps - tool_steps
            self._closing = steps_left <= CLOSING_STEPS
            # 最后一步不再发工具定义：发了它就会用掉，然后一步不剩地撞上限。
            # 这一步要的是一段答复，收尾总结只是兜底。
            final_step = steps_left <= 1
            step_hint = (
                f"{hint_text}\n\n{FINAL_STEP_INSTRUCTION}" if final_step else hint_text
            )
            tools = [] if final_step else self.tool_specs(session)
            self._publish(message, Part(type=PartType.STEP_START, text=f"{step} · {self.config.model}"))
            prefix = stable_prefix(workspace, session)
            try:
                context = self._assemble(
                    session,
                    prefix,
                    with_tools=not final_step,
                    steps_left=steps_left,
                    hint_text=step_hint,
                )
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
                # 装不下时先把当前这一段封成主题：摘要比原文便宜得多，
                # 封完再装一次通常就够了。封不动（这一段已经全在事件里）
                # 才退回摘要那一条路——那是旧机制，只给「首轮就溢出」兜底。
                boundary = event_boundary(session, max(0, len(session.messages) - 1))
                covered = min(context.dropped_upto, boundary)
                if self._seal_range(session, kind="auto", upto=covered, force=True):
                    # 装不下是必须解决的约束：这一段的原文必须让位。
                    session.compact_upto = max(session.compact_upto, covered)
                    context = self._assemble(
                        session,
                        prefix,
                        with_tools=not final_step,
                        steps_left=steps_left,
                        hint_text=step_hint,
                    )
                elif await self._summarize(session, message, context.dropped_upto):
                    context = self._assemble(
                        session,
                        prefix,
                        with_tools=not final_step,
                        steps_left=steps_left,
                        hint_text=step_hint,
                    )
            elif context.dropped_upto > 0:
                # 判据是「上下文里有没有已经不在的内容」，不是「这一步新丢了没有」：
                # 用户手动 /compact 推的边界不在当步产生新移出，但那一段同样要摘要；
                # 已经收进摘要的会由 `_summarize` 自己判掉，只留一条界面记录。
                if await self._summarize(session, message, context.dropped_upto):
                    context = self._assemble(
                        session,
                        prefix,
                        with_tools=not final_step,
                        steps_left=steps_left,
                        hint_text=step_hint,
                    )
            self._trace_context(session, context)
            before = dict(message.tokens)
            calls = await self._ask_model(
                message, context.messages, tools, session.effort
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
                # 这一条已经写完但被 Stop 拦下；正文先放出去，接着另起一条。
                self._flush_held()
                message = self._start_continuation(session)
                continue
            tool_steps += 1
            if not await self._run_tools(session, message, calls):
                exit_reason = EXIT_CANCELLED
                break
            # 查询结果在写回 Part 之后立刻整理成证据。只处理适配层认得的查询
            # 工具，其余工具照原样；SAR 没生效时这一步立即返回。
            await self._process_search_results(session, message, calls, tool_steps)
            # 这一步做完了，立刻记进当前主题的流水。**零请求**：读的是 Part 上
            # 已经算好的工具名、参数、状态与 handle，不碰 reasoning。
            self._record_step(session, message, calls)
            # 连续这么多次「整步工具全失败」才算卡住。
            #
            # 不能一次全失败就停下：工具失败的原因是模型下一步的输入，
            # 「未知工具名」「缺参数」这类它看一眼就会改。真正该停的是
            # **反复**换着法试同一件事都不成——那说明它没有能走通的路了。
            # 计数在成功那一步清零，所以中间只要对上过一次就重新算。
            if self._all_tools_failed(calls):
                # 收束阶段的拒绝是内核自己的决定，不算模型卡住：把它计进
                # 失败连续数，会在还剩几步的时候改判成「工具没能完成」。
                if not self._closing:
                    failed_streak += 1
                    if failed_streak >= MAX_FAILED_STREAK:
                        exit_reason = EXIT_TOOL_FAILED
                        break
            else:
                failed_streak = 0
            # 一个主题走到预算上限、且最近几步没有新产物时，内核自己收一次。
            # 不靠模型判断：实测一条主题能走到 21 步，提示出现了它也没收。
            if self._topic_over_budget(session):
                self._seal_range(
                    session,
                    kind="auto",
                    upto=event_boundary(session, len(session.messages)),
                    force=True,
                )
                exit_reason = EXIT_TOPIC_FULL
                break
        return message, step, int((time.time() - started) * 1000), exit_reason

    @staticmethod
    def _topic_over_budget(session: Session) -> bool:
        """当前主题是否该由内核收掉：步数到上限，且最近 4 步没有新产物。

        只看步数会让正常推进的长主题也被切断；只看「没有新产物」会让刚起步
        还没产出的主题被误收。两个条件同时成立才算卡住。

        子 agent 不适用：它的整个任务就是一个主题，封存会把还要接着用的
        原文拿走。它装不下时由装配层按需封存（`context.dropped` 那条路）。
        """
        if int(getattr(session, "depth", 0) or 0) > 0:
            return False
        steps = session.pending_steps
        if len(steps) < TOPIC_STEP_LIMIT:
            return False
        earlier = {
            item
            for step in steps[:-4]
            for item in step.artifacts
        }
        return not any(
            item not in earlier
            for step in steps[-4:]
            for item in step.artifacts
        )

    def _seal_range(self, session: Session, **options: Any) -> Event | None:
        """在消息边界封存一个主题。

        封存只改事件层怎么分组：这一段折成一条流水，主题视图就此定下来。
        工具清单不随主题变，它由角色配置决定，会话一开始就固定。
        """
        return self._after_seal(session, seal_range(session, **options))

    def _seal_topic(
        self, session: Session, message: Message, calls: list[Part]
    ) -> Event | None:
        """模型要求换主题时封存：切点取这一步的末尾。

        切在步末而不是调用那一刻：这一步的工具结果刚写完，这一段原文到这里
        就齐了；下一段从下一个 `STEP_START` 开始，中间不留半句。
        """
        event = seal_topic(
            session, cut=self._step_cut(session, message, calls), kind="manual"
        )
        self._after_seal(session, event)
        return event

    def _announce_seal(self, session: Session, event: Event | None) -> None:
        """告诉界面一个主题封存了。

        轮末、溢出、压缩、手动收起都会封存，每条路各发一次的话总会漏掉其中一条，
        那时界面上的主题卡就停在「进行中」。
        """
        if event is None:
            return
        self.bus.publish(
            "event.sealed",
            {
                "session_id": session.id,
                "event_id": event.id,
                "topic_index": event.topic_index,
                "title": event.title,
                "steps": len(event.steps),
            },
        )

    def _after_seal(self, session: Session, event: Event | None) -> Event | None:
        """封存之后的收尾：先说一声，再交给记忆抽取。

        工具清单不随主题变：它由角色配置在会话建立时定稿，封存只影响事件块与
        摘要，不再改动请求体里的工具数组。
        """
        if event is None:
            return None
        # 这一段的结论由哪一份答案状态产生。只记编号，正文在答案状态文件里。
        if self.answer_state is not None:
            event.answer_id = self.answer_state.answer_id
        self._announce_seal(session, event)
        if not event.steps:
            return event
        # 抽取排在封存之后：五条封存路径都在这里汇合，不必各接一次。
        # 开关默认关闭，没打开时这一行立即返回。
        from vsagent.core.memory import extractor as memory_extractor

        memory_extractor.submit(event, self.memory, session_id=session.id)
        return event

    def _record_step(
        self, session: Session, message: Message, calls: list[Part]
    ) -> None:
        """把这一步折成一条 Step；模型要换主题就当场封存，超出预算时也切一刀。

        切点取**这一步的末尾**，不取消息末尾：一条 assistant 消息装着一整轮的
        所有步，切在消息末尾会把后面还没写的步也算进这一段，那些步的位置落在
        切点之前，下一个主题再也收不到它们。

        软预算（120K token）管的是「一轮跑了三十步」那种情况：不切的话单个
        主题会一直长到把 RECENT 撑满，而流水是「一个主题」的粒度。
        """
        positions = [
            index
            for index, part in enumerate(message.parts)
            if any(part is call for call in calls)
        ]
        step = record_step(
            session,
            # 步号是主题内连号，不是循环层的 step 计数：那一个会把
            # 「没有工具调用、因此没记步」的那几步也算进去，读起来像漏了号。
            step_no=len(session.pending_steps) + 1,
            parts=calls,
            message_index=max(0, len(session.messages) - 1),
            part_start=min(positions) if positions else 0,
            part_end=(max(positions) + 1) if positions else 0,
        )
        if step is not None:
            # 界面要一步一行。步是这一步的工具跑完之后才折出来的，追加 Part 那一刻
            # 界面还不知道它存在，所以在这里说一声。
            self.bus.publish(
                "step.recorded",
                {
                    "session_id": session.id,
                    "seq": step.seq,
                    "message_index": step.message_index,
                    "part_start": step.part_start,
                },
            )
        if session.topic_title:
            self._seal_topic(session, message, calls)
        if open_tokens(session) > TOPIC_SOFT_LIMIT:
            self._after_seal(
                session,
                seal_topic(session, cut=self._step_cut(session, message, calls), kind="auto"),
            )

    @staticmethod
    def _step_cut(
        session: Session, message: Message, calls: list[Part]
    ) -> tuple[int, int]:
        """这一步结束时的位置：下一个主题从这里开始。"""
        positions = [
            index
            for index, part in enumerate(message.parts)
            if any(part is call for call in calls)
        ]
        return (
            max(0, len(session.messages) - 1),
            (max(positions) + 1) if positions else 0,
        )

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
        """模型写不出来时，本地把已有事实汇成一份简报。

        读的是这条消息上真实的 Part，不是凭空写的：产出的文件、失败的项都取自现场。
        四段的形状与模型写的那一份一致，缺哪段就照实说缺，不从别处补一句。
        """
        files: list[str] = []
        failures: list[str] = []
        for part in message.parts:
            if part.type is PartType.TOOL and part.tool_name:
                if part.tool_status is ToolStatus.FAILED:
                    failures.append(f"{part.tool_name}：{part.tool_output or '没有写明原因'}")
            if part.type is PartType.FILE and part.file_path:
                if part.file_path not in files:
                    files.append(part.file_path)
            if part.type is PartType.PATCH and part.text and part.text not in files:
                files.append(part.text)
        artifacts: list[str] = []
        for step in session.pending_steps:
            for item in step.artifacts:
                if item not in artifacts:
                    artifacts.append(item)
        for event in session.events[-2:]:
            for item in event.artifacts:
                if item not in artifacts:
                    artifacts.append(item)
        produced = [*files, *artifacts]
        todo = session.task_state.todo[:4]
        blockers = session.task_state.blockers[:2]
        if todo:
            next_step = todo[0]
        elif blockers:
            next_step = f"先处理阻塞：{blockers[0]}"
        else:
            next_step = "确认上一段产物是否可用，再从那里接着做。"
        lines = [f"停在哪里：{EXIT_LABELS.get(reason, reason)}。"]
        lines.append(
            "已经产出：" + ("、".join(produced[:6]) if produced else "没有新产物")
        )
        if failures:
            lines.append("还没做完：" + "；".join(failures[:3]))
        elif todo:
            lines.append("还没做完：" + "；".join(todo))
        elif blockers:
            lines.append("还没做完：" + "；".join(blockers))
        else:
            lines.append("还没做完：（任务状态里没有记下待办）")
        lines.append(f"下一轮从这里继续：{next_step}")
        lines.append("（这份简报由本地按已有记录汇编，模型那次收尾请求没有成功。）")
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
        self._seal_range(session, kind="local", upto=target, force=True)
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
            # 这一段已经在摘要里了，**不发 Part**。`/compact` 之后 `summary_upto`
            # 就等于 `upto`，于是每一步都会走到这里——实测一条消息里堆了 6 条
            # 「已丢弃 N 条更早的消息」，占地方又没有任何新信息。
            # 界面要知道「压过了」的话，`/compact` 自己那条 toast 已经说了。
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
                calls = await self._consume_stream(message, messages, tools, reasoning_effort)
                # 有工具调用说明这一步不是最终答复，压着的正文可以放了。
                if calls:
                    self._flush_held()
                return calls
            except ProviderError as error:
                if not error.retryable or attempt >= MAX_RETRIES:
                    # 不再重试：已经流出来的正文交给用户，不能因为末尾出错
                    # 把前半段一起吞掉。
                    self._flush_held()
                    self._publish(
                        message,
                        Part(type=PartType.ERROR, text=f"请求没有成功：{error}"),
                    )
                    return []
                # 还要重试：这一版的半截正文丢掉，下一次从空开始，
                # 免得两次生成的文字接在同一段里。
                self._drop_held()
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
                # 这一步已经有工具调用了，先把还没发布的正文放出去：
                # 既保持正文在工具卡之前的顺序，也说明这一步不是最终答复。
                self._flush_held()
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
        if self._closing and not explicit and tool.spec.name not in closing_tools(session):
            # 收束阶段：不再开新工作，只允许登记、写交付物、核对、收主题与记状态。
            # 与走到 0 步直接断开相比，这一步把「还剩几步」交给模型自己安排。
            self._fail_tool(message, part, closing_hint(session))
            return
        error = argument_error(tool.spec, part.tool_input)
        if error:
            self._fail_tool(message, part, error)
            return
        report = await self._run_hooks(PRE_TOOL_USE, tool.spec.name, part.tool_input)
        if report.blocked:
            self._fail_tool(message, part, f"钩子拦下了这次调用：{report.reason}")
            return
        verdict = self._validate_tool(session, tool.spec.name)
        if verdict.findings:
            self.bus.publish(
                "validator.finding",
                {
                    "session_id": session.id,
                    "tool": tool.spec.name,
                    "mode": self.validator_mode,
                    "findings": verdict.as_dicts(),
                },
            )
        if verdict.blocked and self.validator_mode == "enforce":
            reason = "；".join(item.message for item in verdict.findings if item.blocking)
            self._fail_tool(message, part, f"校验器拦下了这次调用：{reason}")
            return
        decision = self.policy.check(
            tool,
            part.tool_input,
            session.agent_name,
            explicit=explicit,
            cancelled=self.cancelled,
            # 子 agent 的标记：它改不了共享的当前受体，也准备不了受体与配体。
            worker=session.depth > 0,
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

            **空字典不覆盖已有值**：模型可能真的一次都没传参数（`registry`
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
                    event_runtime=self._event_runtime(session),
                    registry=self.registry,
                    workspace=self.workspace,
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
            except Exception as error:
                # 工具里的程序错误不是「这次调用失败」。状态先写成失败再往外抛：
                # 否则那张卡片一直停在「运行中」，重启之后还停在运行中——
                # 一次 NameError 让卡片挂了半小时，看起来像工具还在算。
                # 往外抛是为了让错误在当场可见，吞掉它，用户看到的只是
                # 「这一轮什么都没做」。
                self._fail_tool(
                    message,
                    part,
                    f"内核内部错误：{type(error).__name__}: {error}",
                )
                raise
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
        """把增量接到当前这一类 Part 上，最后一个是别的类型时另起一个。

        SAR 生效的轮次，`TEXT` 先不发布：模型最后一步的正文只是草稿，要等
        整合与核对完成之后一次性交给用户。research 的先验在轮首建立，
        `enforce` 用整合正文替换草稿；`shadow` 到收尾时把草稿原样放出，
        观察整合结果与草稿的差别。
        `REASONING` 照常流式发布——用户仍能看见正在做什么，只是最终答复
        不会先出一版再被改写。
        """
        hold = (
            part_type is PartType.TEXT
            and (
                self._sar_active
                or (self._research_batch and self.sar_mode == "enforce")
            )
        )
        last = message.parts[-1] if message.parts else None
        if last is not None and last.type is part_type:
            last.text += text
            if hold:
                if not any(item[1] is last for item in self._held_parts):
                    self._held_parts.append((message, last))
                return
            self.bus.publish("part.update", {"session_id": message.session_id, "message_id": message.id, "part_id": last.id})
            return
        part = Part(type=part_type, text=text)
        if hold:
            message.add_part(part)
            self._held_parts.append((message, part))
            return
        self._publish(message, part)

    def _flush_held(self) -> None:
        """把压着的正文一次性发布出去。"""
        held, self._held_parts = self._held_parts, []
        for message, part in held:
            self.bus.publish(
                "part.add",
                {
                    "session_id": message.session_id,
                    "message_id": message.id,
                    "part_id": part.id,
                },
            )

    def _drop_held(self) -> None:
        """丢掉还没发布的半截正文，用于重试之前清场。"""
        held, self._held_parts = self._held_parts, []
        for message, part in held:
            message.parts = [item for item in message.parts if item is not part]

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
        # 写过文件但答复没提路径时补一行：模型经常只把路径留在工具卡里，
        # 用户翻不到。只补缺失的，已经提到的不重复。
        written = []
        for part in message.parts:
            if part.type is not PartType.TOOL or part.tool_name != "write":
                continue
            if part.tool_status is not ToolStatus.COMPLETED:
                continue
            raw = (part.tool_input or {}).get("path")
            if isinstance(raw, str) and raw.strip():
                value = raw.strip()
                if value not in written:
                    written.append(value)
        if written:
            answer = "".join(
                part.text for part in message.parts if part.type is PartType.TEXT
            )
            missing = [
                path
                for path in written
                if path not in answer and Path(path).name not in answer
            ]
            if missing:
                self._publish(
                    message,
                    Part(type=PartType.TEXT, text="本轮写入：" + "、".join(missing)),
                )
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
