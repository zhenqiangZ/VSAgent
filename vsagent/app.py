from __future__ import annotations

from collections import deque
from pathlib import Path

from textual.app import App

from .core.agent.context import event_line
from .core.agent.domain.all_tools import domain_tools
from .core.agent.event_log import domain_tools as event_tools
from .core.agent.event_log import render_event_detail
from .core.agent.domain.corpus import ingest_files
from .core.agent.domain.export import runs_dir
from .core.web.service import WebService, web_root
from .core.agent.hooks import HookRunner, default_hooks_paths, load_hooks
from .core.agent.host import AgentHost
from .core.agent.ledger import SessionLedger
from .core.agent.loop import AgentLoop, max_steps_from_env
from .core.agent.memory import MemoryStore, RecallTool, RememberTool
from .core.agent.spawn import AgentStatusTool, SpawnAgentTool
from .core.agent.task import TaskTool
from .core.agent.permission import (
    PermissionPolicy,
    Verdict,
    argument_value,
    default_permission_paths,
    load_permissions,
    load_tool_switches,
    save_disabled,
    tools_config_path,
)
from .core.agent.prompt import KEEP_MESSAGES
from .core.agent.provider import (
    OpenAICompatProvider,
    ProviderConfig,
)
from .core.agent.tools import (
    BASH_TIMEOUT,
    BashTool,
    GrepTool,
    PatchTool,
    ReadTool,
    ReadTurnTool,
    ToolRegistry,
    WriteTool,
)
from .core.agent.toolset import LoadToolsTool
from .core.agent.trace import TraceWriter, log_directory_for
from .core.command_registry import CommandRegistry
from .core.connection import Connection, connection_path_for, load_connection, save_connection
from .core.event_bus import EventBus
from .core.file_log import PROJECT_LOG_DIRECTORY, configure as configure_file_log
from .core.exporter import export_filename, render_session_markdown
from .core.external_editor import (
    editor_buffer_path,
    launch,
    read_buffer,
    resolve_editor,
    write_buffer,
)
from .core.keybind_manager import KeybindManager
from .core.models import EFFORTS, Message, Session, ToolStatus
from .core.project_scan import merge_agents_md, scan_project
from .core.session_manager import SessionManager
from .env import load_env
from .screens.chat import ChatScreen
from .screens.connection_form import ConnectionFormScreen
from .screens.diff_overlay import DiffOverlayScreen
from .screens.help import HelpScreen
from .screens.home import HomeScreen
from .screens.message import MessageScreen
from .screens.permission import PermissionAnswer, PermissionDialog
from .screens.session_picker import ConfirmDialog, SessionPickerScreen
from .screens.spawn_agent import SpawnAgentScreen, SpawnRequest
from .screens.subagent import SubagentChatScreen
from .screens.theme_selector import ThemeSelectorScreen
from .screens.tools_panel import ToolEntry, ToolsPanelScreen
from .widgets.status_bar import format_tokens
from .widgets.toast import ToastNotification

# 需要参数的命令：命令名之后那一段交给处理函数，其余命令不吃参数。
ARGUMENT_COMMANDS = frozenset(
    {"remember", "forget", "task", "model", "agent", "rename", "events", "memory"}
)


class OpenCodeApp(App[None]):
    """opencode TUI 的应用入口，持有核心层并管理屏幕切换。"""

    CSS_PATH = "styles/app.tcss"
    TITLE = "opencode tui"
    TOAST_SECONDS = 3.0
    # 关掉 Textual 自带的命令面板，改用本项目实现的 CommandPalette
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        root: Path | None = None,
        state_path: Path | None = None,
        log_directory: Path | None = None,
        policy: PermissionPolicy | None = None,
        hooks: HookRunner | None = None,
    ) -> None:
        super().__init__()
        self.root = root or Path.cwd()
        self.session_manager = SessionManager(state_path)
        # 接口参数与状态文件同目录。没有这份文件时应用照常起来，
        # 只是发消息之前要先用 `/model` 填一遍。
        self.connection_path = connection_path_for(self.session_manager.state_path)
        self._connection = load_connection(self.connection_path)
        self.command_registry = CommandRegistry()
        self.keybind_manager = KeybindManager(self.command_registry)
        self.event_bus = EventBus()
        trace_directory = log_directory or log_directory_for(self.session_manager.state_path)
        self.trace = TraceWriter(
            self.event_bus,
            lambda: self.session_manager.current_session_id,
            trace_directory,
        )
        # 文件读取日志落在项目自己的 logs/ 下：它跟着仓库走，换项目就换一份，
        # 不像 trace 那样跟着 state.toml 跑去用户目录。
        configure_file_log(self.root / PROJECT_LOG_DIRECTORY)
        self._policy = policy
        self._hooks = hooks
        self._disabled: frozenset[str] | None = None
        # 全部工具的名字与说明，含被关掉的那些。只有 `/tools` 面板要得到它们，
        # 建一次就够——工具清单在进程里是固定的。
        self._catalog: list[tuple[str, str]] | None = None
        self._memory: MemoryStore | None = None
        # 工具注册表按「根 / 子 agent」缓存两份：两者只差一个 spawn_agent。
        self._registries: dict[bool, ToolRegistry] = {}
        # 判定规则读一次，每个循环层各拿一份新策略：本会话的「全部允许」
        # 不能顺着共享对象漏到子 agent 头上。
        self._policy_rules: tuple[dict, dict] | None = None
        self.web_service = self._build_web_service()
        # 会话运行时：每个会话一个循环层，子 agent 在后台并行跑。
        # 循环层里那些 cancelled / evidence / 待答复判定都是会话级的，
        # 共用一份会互相踩，所以不能再像以前那样整个应用只留一个 AgentLoop。
        self.host = AgentHost(
            self.session_manager,
            self._build_loop,
            self.event_bus,
            self.root,
            save_state=self.save_state,
        )
        self._removed_message: Message | None = None
        self._removed_session_id = ""
        # 权限询问排队。多个后台子 agent 同时要权限时，模态不能叠在一起：
        # 用户答完一个才看得见下一个，否则最上面那个关掉之前下面那个一直灰着。
        self._permission_queue: deque[dict[str, object]] = deque()
        self._permission_active = False

    @property
    def agent(self) -> AgentLoop:
        """当前会话的循环层。

        保留这个名字是为了让 `/tools` 面板、权限判定与既有测试不必跟着改；
        真正的多实例由 `host` 管，切会话时这里自然拿到另一个实例。
        """
        return self.host.loop_for(self.session_manager.current_session_id)

    @property
    def active_session(self) -> Session:
        """用户正看着的那个会话。

        栈顶是聊天界面就用它的会话，否则用状态里的 current。
        `/export`、`/undo` 这类命令必须按这个取，否则在子 agent 界面里
        操作的是父会话，而屏幕上写着子 agent 的标题。
        """
        top = self.screen
        if isinstance(top, ChatScreen):
            return top.session
        return self.session_manager.current

    def _build_web_service(self) -> WebService:
        """本地结构页的服务。

        **随应用启停，不交给模型**：起、复用、关这三件事让模型各错一次，
        而它是环境的一部分，不是某一步动作。这里只装配，真正的 `start()`
        放在 `on_mount`——构造期就监听的话，`--help` 这类只跑一下就退的
        路径也会占一瞬端口。
        """
        return WebService(
            state_path=self.session_manager.state_path,
            runs_dir=runs_dir(),
            web_root=web_root(),
            # 网页上的「入库」转交到这里：写索引的仍然是这个进程，网页自己不碰库。
            # 与模型调 `import_documents` 走的是同一个 `ingest_files`。
            ingest=lambda files, root: ingest_files(files, root=root),
        )

    def _build_loop(self, session: Session, ledger: SessionLedger) -> AgentLoop:
        """按会话装配一个循环层。

        每个会话一份，不共用：循环层里的 `cancelled` / `evidence` /
        待答复判定都是会话级状态。注册表按「根 / 子 agent」复用两份，
        子 agent 那份**不注册 spawn_agent**——模型连这个工具存在都不知道。
        """
        load_env()
        config = self._provider_config()
        # 会话里记着的模型是它上次用的那个。子 agent 是从某个会话分出去的，
        # 启动恢复时不能一律拿连接配置里的默认模型顶上去。
        if session.model_id and session.model_id != config.model:
            config = config.for_model(session.model_id)
        memory = self._build_memory()
        root = not session.is_subagent
        registry = self._registries.get(root)
        if registry is None:
            registry = self._build_registry(memory, allow_spawn=root)
            self._registries[root] = registry
        return AgentLoop(
            self.event_bus,
            OpenAICompatProvider(config),
            registry,
            config,
            context_window=config.context_window,
            workspace_root=self.root,
            state_dir=self.session_manager.state_path.parent,
            policy=self._policy_for(session),
            hooks=self._build_hooks(),
            ledger=ledger,
            memory=memory,
            viewer=self.web_service,
            # 子 agent 拿到 None：就算历史里学过这个名字，工具本身也会拒。
            spawner=self.host if root else None,
            max_steps=max_steps_from_env(),
        )

    def _provider_config(self) -> ProviderConfig:
        """由当前连接配置拼出 provider 参数。

        没有配置时给一份三项全空的配置：应用照常起来，发消息那一步再拦，
        提示用户先走 `/model`。
        """
        connection = self._connection
        if connection is None:
            return ProviderConfig(base_url="", api_key="", model="")
        return ProviderConfig.from_values(
            connection.base_url,
            connection.api_key,
            connection.model,
            context_window=connection.context_window,
            price_in=connection.price_in,
            price_out=connection.price_out,
        )

    @property
    def is_configured(self) -> bool:
        """模型接口的三项是否都填了。"""
        return self.agent.config.configured

    def _policy_for(self, session: Session) -> PermissionPolicy:
        """每个循环层一份判定策略。

        规则来自同一份配置，但「本会话全部允许」这种运行期状态必须各自一份：
        共享对象会让父会话的一次放行悄悄作用到后台子 agent 上。
        测试注入的那份照旧共用，它本来就是拿来断言调用的。
        """
        if self._policy is not None:
            return self._policy
        if self._policy_rules is None:
            project_path, user_path = default_permission_paths(self.root)
            self._policy_rules = (
                load_permissions(project_path, "项目配置"),
                load_permissions(user_path, "用户配置"),
            )
        project_rules, user_rules = self._policy_rules
        return PermissionPolicy(
            project_rules=project_rules,
            user_rules=user_rules,
            workspace_root=self.root,
        )

    def _build_hooks(self) -> HookRunner:
        """装配钩子层。

        钩子的工作基准与工具一致，都是项目目录：钩子脚本里的相对路径、
        它读到的那个文件，与模型在工具里操作的是同一个。
        """
        if self._hooks is not None:
            return self._hooks
        project_path, user_path = default_hooks_paths(self.root)
        return HookRunner(
            load_hooks(project_path, "项目配置") + load_hooks(user_path, "用户配置"),
            self.root,
        )

    def _build_memory(self) -> MemoryStore:
        """装配项目级记忆。

        记忆是**跨会话**的，与当前会话无关，所以只建一次、切换会话时不重建。
        载入之后先对一次账：引用的文件在磁盘上可能早就变了，标出来之后
        注入时才会带上提醒。
        """
        if self._memory is not None:
            return self._memory
        self._memory = MemoryStore(self.root)
        self._memory.load()
        self._memory.revalidate()
        return self._memory

    def _build_registry(
        self,
        memory: MemoryStore,
        disabled: frozenset[str] | None = None,
        *,
        allow_spawn: bool = True,
    ) -> ToolRegistry:
        """装配全部工具。每个工具的工作基准都是项目目录，
        与系统提示里写出来的路径是同一个。

        领域工具由 `domain.all_tools` 一次给全：这个文件的本职是管屏幕与状态，
        每加一个领域模块都回来改一次装配，漏掉一处的结果就是「工具写了但模型看不见」。
        记忆工具与任务状态工具同属内核，排在领域工具之前。

        `.opencode_tui/tools.toml` 里标了关的工具**根本不装进来**：不装进注册表就
        不会出现在请求体的 `tools` 数组里，模型连它存在都不知道。这与权限层的
        `deny` 是两件事——那一层工具照旧可见，只是调用时被拦。

        `disabled` 传空集时装配一份「一个都不关」的注册表，`/tools` 面板靠它
        拿到完整清单：面板要能列出被关掉的那些，否则关掉之后就再也打不开了。
        """
        if disabled is None:
            disabled = self._disabled_tools()
        registry = ToolRegistry(disabled=disabled)
        # 超时显式传进来，而不是靠 BashTool 的默认值：改这一个数字就能看见装配处跟着动，
        # 不必翻回 tools.py 猜当前生效的是哪一个。
        registry.register(BashTool(cwd=self.root, timeout=BASH_TIMEOUT))
        registry.register(ReadTool(cwd=self.root))
        registry.register(WriteTool(cwd=self.root))
        registry.register(PatchTool(cwd=self.root))
        registry.register(GrepTool(cwd=self.root))
        registry.register(RememberTool(memory))
        registry.register(RecallTool(memory))
        # 任务状态不用传东西进来：它挂在会话上，由循环层每次调用现取。
        registry.register(TaskTool())
        # 取回被归档/被移出的原文。与任务状态同理：会话由内核注入。
        registry.register(ReadTurnTool())
        # 事件收口与召回：把做完的一段压成标题 + 摘要，需要细节再按编号取回。
        for tool in event_tools():
            registry.register(tool)
        # 按族加载领域工具：核心那一批常驻，其余由模型自己装。
        registry.register(LoadToolsTool())
        # 派发子 agent 是**根会话专属**的能力。子 agent 的注册表里没有它，
        # 请求体的 tools 数组里也就没有它，模型连这个名字都看不到。
        if allow_spawn:
            registry.register(SpawnAgentTool())
            # 查询与等待是同一条能力的另一半：只派不取，派发就没有闭环。
            registry.register(AgentStatusTool())
        for tool in domain_tools():
            registry.register(tool)
        return registry

    def _disabled_tools(self) -> frozenset[str]:
        """`.opencode_tui/tools.toml` 里关掉的工具名。

        启动时读一次就缓存下来：装配路径上一个会话要走几十次，每次重读文件没有必要。
        `/tools` 面板改完会自己把缓存换掉（见 `toggle_tool`），所以不必重启。
        """
        if self._disabled is None:
            self._disabled = load_tool_switches(self.root)[0]
        return self._disabled

    def tool_catalog(self) -> list[tuple[str, str]]:
        """全部工具的名字与说明，按装配顺序，**含被关掉的那些**。

        拿完整清单的办法是装配一份「一个都不关」的注册表，而不是去问当前注册表：
        当前注册表里根本没有被关掉的那些，面板要能列出它们才谈得上打开。
        """
        if self._catalog is None:
            full = self._build_registry(self._build_memory(), disabled=frozenset())
            catalog: list[tuple[str, str]] = []
            for name in full.names():
                tool = full.get(name)
                if tool is not None:
                    catalog.append((name, tool.spec.description))
            self._catalog = catalog
        return self._catalog

    def tool_entries(self) -> list[ToolEntry]:
        """面板要的每一行：名字、说明、开没开、关它的配置是不是项目侧那一份。"""
        disabled, sources = load_tool_switches(self.root)
        project = tools_config_path(self.root)
        entries: list[ToolEntry] = []
        for name, description in self.tool_catalog():
            source = sources.get(name)
            entries.append(
                ToolEntry(
                    name=name,
                    description=description,
                    enabled=name not in disabled,
                    # 只有「用户目录那份关的」才标出来：项目侧那份就是面板自己写的，
                    # 标一句「由项目配置关闭」等于什么都没说。
                    origin="" if source is None or source == project else "用户配置",
                )
            )
        return entries

    def toggle_tool(self, name: str, enabled: bool) -> list[ToolEntry]:
        """打开或关掉一个工具，写盘并立刻重装注册表，返回刷新后的面板数据。

        重装注册表而不是只改 `disabled` 集合：工具的可见性就是注册表的成员资格，
        只改集合的话，这一轮里的请求体仍带着旧清单，得重启才生效。

        写盘之后重新读一遍再判断：用户目录那份也写了 `false` 时，本文件改成 true
        也不管用，那时要如实说一句，而不是弹「已启用」把人骗过去。
        """
        disabled, sources = load_tool_switches(self.root)
        project = tools_config_path(self.root)
        names = {tool for tool, source in sources.items() if source == project}
        if enabled:
            names.discard(name)
        else:
            names.add(name)
        try:
            save_disabled(self.root, names)
        except OSError as error:
            self.show_toast(f"写 {tools_config_path(self.root)} 出错：{error}", "error")
            return self.tool_entries()

        self._disabled = load_tool_switches(self.root)[0]
        # 两份缓存都作废，再按每个会话重建：子 agent 那份不能因为重建
        # 就把 spawn_agent 一起装回去。
        self._registries.clear()
        for session_id, loop in self.host.items():
            session = self.session_manager.get_session(session_id)
            loop.registry = self._build_registry(
                self._build_memory(),
                allow_spawn=not (session is not None and session.is_subagent),
            )
        actual, sources = load_tool_switches(self.root)
        if enabled and name in actual:
            self.show_toast(f"{name} 仍被 {sources[name]} 关着，先把那份文件里的 false 去掉", "warning")
        else:
            self.show_toast(f"{name} 已{'启用' if enabled else '关闭'}", "success")
        return self.tool_entries()

    def on_mount(self) -> None:
        """启动时载入状态、起本地结构页的服务，并进入启动界面。"""
        loaded = self.session_manager.load_from_toml()
        if loaded:
            self.theme = self.session_manager.theme
        if not self.session_manager.sessions:
            self.session_manager.new_session()
        # 会话里记着的模型是上一次选的那个：地址与密钥按连接配置走，模型名以会话为准。
        # 不同步的话状态行写着 A、请求体里发的却是连接配置里的 B。
        session = self.session_manager.current
        if (
            self.is_configured
            and session.model_id
            and session.model_id != self.agent.config.model
        ):
            self.agent.switch_model(session.model_id)
        self.trace.start()
        self.web_service.start()
        # 权限询问由应用层统一处理，而不是聊天界面：子 agent 在后台跑、
        # 用户停在父会话时，那条询问没有屏幕认领，挂在界面上会永远等下去。
        self.event_bus.subscribe("permission.ask", self._on_permission_ask)
        self.push_screen(HomeScreen())

    def on_unmount(self) -> None:
        """退出时把服务停掉、日志文件关掉。

        服务要显式停：`daemon=True` 只保证进程结束时它跟着结束，
        端口要等进程真正退出才释放。不显式停的话，下一次启动会归到
        「上次没退干净」那条路上（靠端口避让兜着，但那是兜底不是常态）。
        """
        self.web_service.stop()
        self.host.shutdown()
        self.trace.stop()

    @property
    def current_model_label(self) -> str:
        """当前模型的展示名。

        会话里选过模型就以会话为准：界面上显示的那一个，
        与下一次请求体里 `model` 字段是同一个。接口还没配置时显示占位文字。
        """
        session = self.session_manager.current
        return session.model_id or self.agent.config.model or "（未配置）"

    @property
    def current_model_provider(self) -> str:
        """当前模型的提供方。"""
        return self.agent.config.provider

    @property
    def context_window(self) -> int:
        """上下文窗口的容量。"""
        return self.agent.context_window

    def start_chat_with(self, prompt: str) -> None:
        """带着一句初始输入进入聊天界面，由聊天界面负责发出。

        主页面提问默认开新会话：当前会话已经有内容就新建一个，空会话直接复用，
        免得每问一句都在列表里留下一个空壳。没配接口时不新建——这一问会被拦下，
        建了也只是一份空记录。
        """
        session = self.session_manager.current
        if self.is_configured and session.messages:
            self.session_manager.new_session()
        self.switch_screen(ChatScreen(self.root, initial_prompt=prompt))

    def save_state(self) -> None:
        """把会话与显示设定写入磁盘。"""
        self.session_manager.last_model_id = self.agent.config.model
        self.session_manager.theme = self.theme
        self.session_manager.save_to_toml()

    def show_toast(self, text: str, kind: str = "info") -> None:
        """显示一条会自动消失的通知。"""
        toast = ToastNotification(text, kind)
        target = self.screen
        if target.is_mounted:
            target.mount(toast)
        else:
            self.call_after_refresh(lambda: target.mount(toast))
        self.set_timer(self.TOAST_SECONDS, lambda: self._remove_toast(toast))

    def _remove_toast(self, toast: ToastNotification) -> None:
        """移除已经挂载过的通知。"""
        if toast.is_mounted:
            toast.remove()

    def execute_command(self, name: str) -> None:
        """按名字执行斜杠命令。

        命令名之后的那一段是参数。只有 `ARGUMENT_COMMANDS` 里的命令吃参数，
        其余命令后面的内容忽略——这样 `/new` 后面多打一个词不会变成「未知命令」。
        """
        raw = name.lstrip("/").strip()
        head, _, rest = raw.partition(" ")
        command = self.command_registry.get(head)
        if command is None:
            self.show_toast(f"未知命令：{name}", "error")
            return
        handler = getattr(self, f"_command_{command.name}", None)
        if handler is None:
            self.show_toast(f"/{command.name} 尚未接入", "warning")
            return
        top = self.screen
        if isinstance(top, ChatScreen) and not top.allows_command(command.name):
            self.show_toast(f"子 agent 界面不支持 /{command.name}", "warning")
            return
        if command.name in ARGUMENT_COMMANDS:
            handler(rest.strip())
        else:
            handler()

    def _command_new(self) -> None:
        """新建会话。"""
        self.session_manager.new_session()
        self._ensure_chat_screen()
        self.save_state()
        self.show_toast("已创建新会话", "success")

    def _command_agent(self, arguments: str) -> None:
        """派发子 agent：不带参数开模态，带一段文字就按整段历史直接派发。

        `/agent <任务>` 是快捷键式的用法：常用的「把刚才这段活分出去」，
        不必每次都过一遍范围选择。
        """
        top = self.screen
        if isinstance(top, SubagentChatScreen):
            self.show_toast("子 agent 内不能再派发新的 agent", "warning")
            return
        if not self.is_configured:
            self.show_toast("还没有配置模型接口，先输入 /model 填写地址与密钥", "warning")
            return
        parent = self.session_manager.current
        task = arguments.strip()
        if task:
            child = self.host.spawn(
                parent,
                title=task.splitlines()[0][:20],
                task=task,
                start=0,
                end=len(parent.messages),
                origin="user",
            )
            self.show_toast(f"已派发子 agent「{child.title}」", "success")
            return
        self.push_screen(
            SpawnAgentScreen(
                parent,
                self.agent.context_window,
                # 子 agent 继承父会话装过的族，所以预览要按父会话的实际清单算：
                # 拿全量算会把可用额度说小一半，用户填的范围看着「放不下」。
                self.agent.tool_specs(parent),
            ),
            self._on_spawn_requested,
        )

    def _on_spawn_requested(self, request: "SpawnRequest | None") -> None:
        """创建模态交回结果：建数据、开后台任务、刷新右栏。"""
        if request is None:
            return
        parent = self.session_manager.get_session(request.parent_id)
        if parent is None:
            self.show_toast("父会话已经不在了", "error")
            return
        try:
            child = self.host.spawn(
                parent,
                title=request.title,
                task=request.task,
                start=request.start,
                end=request.end,
                origin="user",
                run=request.run,
            )
        except ValueError as error:
            self.show_toast(str(error), "warning")
            return
        if request.run:
            self.show_toast(f"已派发子 agent「{child.title}」，在后台运行", "success")
        else:
            self.show_toast(
                f"已创建子 agent「{child.title}」，进去后按 ctrl+r 开始", "success"
            )

    def open_subagent(self, session_id: str) -> None:
        """进入子 agent 界面。当前会话不切换，返回时父会话还在原处。

        兄弟之间也是平级压栈：A → B → 父会话，返回一层退一层。
        """
        session = self.session_manager.get_session(session_id)
        if session is None:
            self.show_toast("这个子 agent 已经不在了", "warning")
            return
        self.push_screen(SubagentChatScreen(self.root, session_id=session_id))

    def _command_back(self) -> None:
        """从子 agent 界面返回父会话。"""
        if isinstance(self.screen, SubagentChatScreen):
            self.pop_screen()
            return
        self.show_toast("当前不在子 agent 界面", "info")

    def _command_agents(self) -> None:
        """把焦点交给右栏的子 agent 列表。

        命令而不是快捷键：输入框常年拿着焦点，屏幕上的键位基本按不到；
        `/agents` 之后 ↑↓ 选择、enter 打开、esc 回到输入框。
        """
        top = self.screen
        if not isinstance(top, ChatScreen):
            self.show_toast("请先进入聊天界面", "warning")
            return
        session = top.session
        anchor = session.parent_id or session.id
        if not self.host.children_of(anchor):
            self.show_toast("本会话还没有子 agent，用 /agent 派发一个", "info")
            return
        if not top.side_panel.has_class("-visible"):
            top.side_panel.set_class(True, "-visible")
            top.refresh_status()
        if top.side_panel.focus_agents():
            self.show_toast("↑↓ 选择，enter 打开，esc 回到输入框", "info")

    def _command_rename(self, arguments: str) -> None:
        """重命名当前会话。根会话与子 agent 都走这一条。"""
        title = arguments.strip()
        if not title:
            self.show_toast("用法：/rename 新名字", "warning")
            return
        session = self.active_session
        if not self.session_manager.rename_session(session.id, title):
            self.show_toast("会话已经不在了", "warning")
            return
        self.refresh_chat()
        self.save_state()
        self.show_toast(f"已重命名为「{session.title}」", "success")

    def _command_delete(self) -> None:
        """删除当前会话，先确认。

        子 agent 界面删的是它自己；根会话删除会连子 agent 一起删，
        确认文案里把这一句写出来，免得点完才发现整棵子树都没了。
        """
        target = self.active_session
        children = self.session_manager.children_of(target.id)
        if target.is_subagent:
            question = f"删除子 agent「{target.title}」？它的对话会一起删掉。"
        elif children:
            question = (
                f"删除会话「{target.title}」？"
                f"它的 {len(children)} 个子 agent 也会一起删掉。"
            )
        else:
            question = f"删除会话「{target.title}」？"
        self.push_screen(
            ConfirmDialog(question),
            lambda ok: self._delete_confirmed(target, ok),
        )

    def _delete_confirmed(self, target: Session, confirmed: bool) -> None:
        """确认之后真删，子 agent 界面删完自己退回去。"""
        if not confirmed:
            return
        was_subagent = target.is_subagent
        self.delete_session(target.id)
        if was_subagent and isinstance(self.screen, SubagentChatScreen):
            self.pop_screen()

    def _command_sessions(self) -> None:
        """列出会话。子 agent 不进这个列表，它们在右栏里。"""
        sessions = self.session_manager.root_sessions()
        self.push_screen(SessionPickerScreen(sessions), self._on_session_picked)

    def _command_model(self, arguments: str) -> None:
        """配置模型接口，或者只换模型名。

        不带参数时打开表单填地址、密钥与模型名；带一个词时把它当模型名，
        连接参数不动。带参数那条要求接口已经配好，没配会先提示去填表。
        """
        name = arguments.strip()
        if name:
            self._apply_model(name)
            return
        self.push_screen(ConnectionFormScreen(self._connection), self._on_connection_entered)

    def _command_tools(self) -> None:
        """打开工具管理面板。"""
        self.push_screen(ToolsPanelScreen(self.tool_entries(), self.toggle_tool))

    def _command_themes(self) -> None:
        """列出主题。"""
        themes = sorted(self.available_themes)
        self.push_screen(ThemeSelectorScreen(themes), self._on_theme_picked)

    def _command_help(self) -> None:
        """显示帮助。"""
        self.push_screen(HelpScreen(self.command_registry, self.keybind_manager, self._help_origin()))

    def _help_origin(self) -> str:
        """找出帮助是从哪个界面打开的：屏幕层级里最靠上的启动界面或聊天界面。

        从命令面板选 /help 时，最上面那一层是命令面板自己，所以要看它下面那一层。
        """
        for screen in reversed(self.screen_stack):
            if isinstance(screen, HomeScreen):
                return "home"
            if isinstance(screen, ChatScreen):
                return "chat"
        return "chat"

    def _command_exit(self) -> None:
        """退出应用。"""
        self.save_state()
        self.exit()

    def _command_export(self) -> None:
        """把当前会话导出为 Markdown 文件。"""
        session = self.active_session
        if not session.messages:
            self.show_toast("当前会话还没有消息", "warning")
            return
        target = self.root / export_filename(session)
        target.write_text(render_session_markdown(session), encoding="utf-8")
        self.show_toast(f"已导出到 {target}", "success")

    def _command_memory(self, arguments: str) -> None:
        """列出项目记忆；`/memory --all` 连已经被取代的一起列。

        默认只列还生效的：失效的条目留在盘上是给审计与回滚用的，
        混在日常列表里只会让人以为「这条还算数」。
        """
        store = self._build_memory()
        # 长会话里文件可能已经被重新生成过：列之前对一次账，
        # 「引用文件已变更」这类提醒才是当下的，不是启动那一刻的。
        store.revalidate()
        show_all = arguments.strip() in ("--all", "all")
        entries = store.all_entries(include_revoked=show_all)
        if not entries:
            self.show_toast("项目记忆还是空的", "info")
            return
        lines: list[str] = []
        for entry in entries:
            lines.extend(store.describe_line(entry).splitlines())
        self.push_screen(MessageScreen("项目记忆", lines))

    def _command_remember(self, arguments: str) -> None:
        """把一段文字写进项目记忆。"""
        text = arguments.strip()
        if not text:
            self.show_toast("用法：/remember 一段事实", "warning")
            return
        entry = self._build_memory().remember(text, source="user")
        self.show_toast(f"已记住（{entry.id}）", "success")

    def _command_forget(self, arguments: str) -> None:
        """按编号或关键词删掉一条记忆。"""
        query = arguments.strip()
        if not query:
            self.show_toast("用法：/forget 编号或关键词", "warning")
            return
        store = self._build_memory()
        if query in store.entries:
            store.forget(query)
            self.show_toast(f"已删除 {query}", "success")
            return
        hits = store.recall(query)
        if not hits:
            self.show_toast(f"没有匹配「{query}」的记忆", "warning")
            return
        if len(hits) > 1:
            lines = [store.render_line(entry) for entry in hits]
            self.push_screen(MessageScreen("匹配到多条，请用编号 /forget", lines))
            return
        store.forget(hits[0].id)
        self.show_toast(f"已删除 {hits[0].id}", "success")

    def _command_task(self, arguments: str) -> None:
        """查看或更新本会话的任务状态。

        三种形式：`/task` 查看、`/task clear` 清空、
        `/task <字段> <内容>` 追加一条（`goal` 是单值，等同于覆盖）。

        写的是当前会话的 `task_state`，与模型调 `task` 工具改的是同一份东西；
        两者都随状态文件写盘，重启之后还在。
        """
        state = self.active_session.task_state
        text = arguments.strip()
        if not text:
            if state.is_empty():
                self.show_toast("本会话还没有任务状态", "info")
                return
            self.push_screen(MessageScreen("任务状态", state.render().splitlines()))
            return
        head, _, rest = text.partition(" ")
        if head.strip().lower() == "clear":
            if state.clear():
                self.save_state()
                self.show_toast("任务状态已清空", "success")
            else:
                self.show_toast("任务状态本来就是空的", "info")
            return
        try:
            changed = state.add(head, rest)
        except ValueError as error:
            self.show_toast(
                f"{error}。可用字段：goal / constraint / done / todo / blocker",
                "warning",
            )
            return
        if not changed:
            self.show_toast("与已有内容相同，没有变化", "info")
            return
        self.save_state()
        self.show_toast(f"任务状态已更新（v{state.version}）", "success")

    def _command_editor(self) -> None:
        """用外部编辑器撰写当前输入。"""
        top = self.screen
        if not isinstance(top, ChatScreen):
            self.show_toast("请先进入聊天界面", "warning")
            return
        editor = resolve_editor()
        if editor is None:
            self.show_toast("未找到外部编辑器，请设置 EDITOR 环境变量", "warning")
            return
        buffer = editor_buffer_path()
        write_buffer(buffer, top.editor.text)
        with self.suspend():
            launch(editor, buffer)
        top.editor.text = read_buffer(buffer).rstrip("\n")

    def _command_details(self) -> None:
        """切换工具执行详情。"""
        self.session_manager.show_details = not self.session_manager.show_details
        self._apply_display_toggles()
        state = "显示" if self.session_manager.show_details else "隐藏"
        self.show_toast(f"工具详情：{state}", "info")

    def _command_effort(self) -> None:
        """按 low → medium → high 轮换推理强度。"""
        session = self.active_session
        index = EFFORTS.index(session.effort) if session.effort in EFFORTS else 0
        session.effort = EFFORTS[(index + 1) % len(EFFORTS)]
        self.refresh_chat()
        self.save_state()
        self.show_toast(f"推理强度：{session.effort}", "info")

    def _command_sidebar(self) -> None:
        """切换聊天界面右侧的上下文侧栏。"""
        top = self.screen
        if not isinstance(top, ChatScreen):
            self.show_toast("请先进入聊天界面", "warning")
            return
        visible = top.toggle_sidebar()
        state = "显示" if visible else "隐藏"
        self.show_toast(f"上下文侧栏：{state}", "info")

    def _command_thinking(self) -> None:
        """切换思考块可见性。"""
        self.session_manager.show_thinking = not self.session_manager.show_thinking
        self._apply_display_toggles()
        state = "显示" if self.session_manager.show_thinking else "隐藏"
        self.show_toast(f"思考块：{state}", "info")

    def _command_undo(self) -> None:
        """撤销最后一条消息。"""
        session = self.active_session
        if not session.messages:
            self.show_toast("没有可撤销的消息", "warning")
            return
        self._removed_message = session.messages.pop()
        self._removed_session_id = session.id
        # 事件是按下标覆盖消息的，删掉一条之后覆盖范围可能越界。
        # 这里立刻裁一次，别等装配时才发现事件指向了一条不存在的消息。
        session.sanitize_events()
        self.refresh_chat()
        self.save_state()
        self.show_toast("已撤销最后一条消息", "success")

    def _command_redo(self) -> None:
        """恢复被撤销的消息。"""
        if self._removed_message is None or self._removed_session_id != self.active_session.id:
            self.show_toast("没有可重做的消息", "warning")
            return
        session = self.active_session
        session.messages.append(self._removed_message)
        session.touch()
        session.sanitize_events()
        self._removed_message = None
        self.refresh_chat()
        self.save_state()
        self.show_toast("已恢复被撤销的消息", "success")

    def _command_compact(self) -> None:
        """把当前会话前面的一段压掉。"""
        session = self.active_session
        if len(session.messages) <= KEEP_MESSAGES:
            self.show_toast(f"消息不足 {KEEP_MESSAGES} 条，不用压缩", "info")
            return
        before = session.compact_upto
        left = self.host.loop_for(session.id).compact_session(session)
        self.save_state()
        if session.compact_upto == before:
            self.show_toast("这一段已经压过了", "info")
            return
        self.refresh_chat()
        self.show_toast(f"已压缩，之后这一轮只带最近 {left} 条发给模型", "success")

    def _command_connect(self) -> None:
        """显示当前连的是哪一家，以及配置存在哪里。"""
        config = self.agent.config
        path = self.connection_path
        lines = [
            f"提供商　{config.provider or '（没写）'}",
            f"接口　　{config.base_url or '（还没配置）'}",
            f"模型　　{config.model or '（还没配置）'}",
            f"密钥　　{'已设置' if config.api_key else '没有'}（存在配置文件里，权限 0600）",
            f"配置文件{path}",
            f"　　　　{'存在' if path.is_file() else '还没有'}　用 /model 填写接口地址、密钥与模型",
        ]
        self.push_screen(MessageScreen("当前提供商", lines))

    def _command_init(self) -> None:
        """扫描项目，写一份或更新一份 AGENTS.md。"""
        summary = scan_project(self.root)
        target = self.root / "AGENTS.md"
        existed = target.is_file()
        try:
            text = merge_agents_md(target.read_text(encoding="utf-8") if existed else "", summary)
            target.write_text(text, encoding="utf-8")
        except OSError as error:
            self.show_toast(f"写 {target} 出错：{error}", "error")
            return
        action = "更新" if existed else "创建"
        self.show_toast(f"已{action} {target.name}，扫到 {summary.file_count} 个文件", "success")

    def _on_permission_ask(self, data: dict[str, object]) -> None:
        """某个会话的工具调用需要用户答复。

        排在应用层而不是聊天界面，是为了让后台子 agent 的询问也能弹出来：
        用户可能正停在父会话，屏幕上没有那个子 agent 的任何东西。

        **排队而不是直接弹**：两个子 agent 同时要权限时，两个模态会叠在一起，
        下面那个在用户关掉上面那个之前一直看不见也点不到。这里按到达顺序
        依次弹，答完一个再弹下一个。
        """
        self._permission_queue.append(dict(data))
        self._show_next_permission()

    def _show_next_permission(self) -> None:
        """弹队列里的下一个询问。已经失效的条目直接跳过。"""
        if self._permission_active:
            return
        while self._permission_queue:
            data = self._permission_queue.popleft()
            session_id = str(data.get("session_id", ""))
            session = self.session_manager.get_session(session_id)
            if session is None:
                continue
            message_id = str(data.get("message_id", ""))
            part_id = str(data.get("part_id", ""))
            message = next(
                (item for item in session.messages if item.id == message_id), None
            )
            part = message.part_by_id(part_id) if message is not None else None
            # 会话被删、工具已经被中断或拒绝：这条询问已经没有对象了。
            if part is None or part.tool_status is not ToolStatus.AWAITING:
                continue
            loop = self.host.loop_for(session_id)
            tool = loop.registry.get(part.tool_name)
            argument = (
                argument_value(tool, part.tool_input) if tool is not None else ""
            )
            label = session.title if session.is_subagent else session.agent_name
            self._permission_active = True
            self.push_screen(
                PermissionDialog(part.tool_name, argument, label, part.permission),
                lambda answer, sid=session_id, pid=part_id: self._answer_permission(
                    sid, pid, answer
                ),
            )
            return

    def _answer_permission(
        self,
        session_id: str,
        part_id: str,
        answer: PermissionAnswer | None,
    ) -> None:
        """把弹窗的答复交回**发起询问的那个会话**，然后弹下一个。"""
        loop = self.host.loop_for(session_id)
        if answer is None:
            loop.resolve_permission(part_id, Verdict.DENY)
        else:
            loop.resolve_permission(part_id, answer.verdict, answer.remember)
        self._permission_active = False
        # 等这一帧的卸屏动作走完再挂下一个，否则两个模态会在同一帧里交接。
        self.call_after_refresh(self._show_next_permission)

    def _command_log(self) -> None:
        """显示当前会话的事件日志路径。"""
        path = self.trace.current_path()
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                state = f"已有 {sum(1 for _ in handle)} 行"
        else:
            state = "还没有内容，发起一轮生成就会出现"
        self.push_screen(MessageScreen("事件日志", [f"日志文件　{path}", f"状态　　　{state}"]))

    def _command_context(self) -> None:
        """显示最近几步的上下文用量。

        每步两行：第一行是实际用量（服务端记账的那一份，含缓存命中），
        第二行是装配器的估算分层。两行并排看才回答得了「估得准不准」——
        只看估算会把「额度分给谁」当成「真的花了多少」。
        """
        loop = self.agent
        history = loop.context_history[-10:]
        if not history:
            self.show_toast("还没有上下文度量，发起一轮生成后就有了", "info")
            return
        latest = history[-1]
        lines = [
            f"窗口 {format_tokens(int(latest['window']))}"
            f"　可用 {format_tokens(int(latest['available']))}"
            f"　工具定义 {format_tokens(int(latest['tools']))}"
            f"　输出预留 {format_tokens(int(latest['reserve']))}"
            "（下表「装配」不含工具定义，实际输入 = 装配 + 工具 + 协议开销）"
        ]
        for item in history:
            lines.append(
                f"第 {item['step']} 步　实际输入 {format_tokens(int(item['input']))}"
                f"（缓存 {format_tokens(int(item['cached']))}"
                f" · {float(item['cache_hit']) * 100:.1f}%）"
                f"　占窗口 {float(item['window_used']) * 100:.1f}%"
                f"　装配 {format_tokens(int(item['total']))}"
            )
            usage = item.get("usage") or {}
            layers = [
                f"{name} {format_tokens(int(tokens))}"
                for name, tokens in usage.items()
                if tokens
            ]
            lines.append("　　" + (" · ".join(layers) if layers else "（没有内容）"))
            if item.get("degraded") or item.get("dropped"):
                lowered = "、".join(str(level) for level in item.get("degraded") or [])
                lines.append(
                    f"　　削过：{lowered or '无'}　移出 {int(item.get('dropped', 0))} 组"
                )
        self.push_screen(MessageScreen("上下文用量", lines))

    def _command_events(self, arguments: str) -> None:
        """列出本会话的事件；带编号时看详情。

        事件是「已经收口的阶段」：原文不再进上下文，这里看得到它被压成了什么。
        想看原文用 `/events <编号>` 或让模型调 `recall_event`。
        """
        session = self.active_session
        if not session.events:
            self.show_toast("这份会话还没有事件", "info")
            return
        wanted = arguments.strip()
        if not wanted:
            lines = [event_line(event) for event in session.events]
            self.push_screen(MessageScreen("会话事件", lines))
            return
        event = next((item for item in session.events if item.id == wanted), None)
        if event is None:
            available = "、".join(item.id for item in session.events)
            self.show_toast(f"没有 {wanted}；可用：{available}", "warning")
            return
        detail = render_event_detail(session, event)
        self.push_screen(MessageScreen(event.id, detail.splitlines()))

    def delete_session(self, session_id: str) -> bool:
        """删掉一个会话，连它的子 agent 与运行时一起清。

        只调 `session_manager.delete_session` 是不够的：循环层、后台任务与
        共享登记表都还留在 `host` 里。正在跑的子 agent 会在数据已经不在
        manager 之后继续写，右栏也会继续列一个已经不存在的会话。
        """
        session = self.session_manager.get_session(session_id)
        if session is None:
            return False
        doomed = [session_id]
        doomed.extend(child.id for child in self.session_manager.children_of(session_id))
        family = session.family_id
        was_current = self.session_manager.current_session_id in doomed
        if not self.session_manager.delete_session(session_id):
            return False
        for sid in doomed:
            self.host.forget(sid)
        self.host.drop_family(family)
        self.save_state()
        if was_current:
            self.refresh_chat()
        return True

    def _on_session_picked(self, session_id: str | None) -> None:
        """切换会话。"""
        if not session_id:
            return
        self.session_manager.switch_session(session_id)
        if self._policy is not None:
            self._policy.clear_session()
        self._ensure_chat_screen()
        session = self.session_manager.get_session(session_id)
        self.show_toast(f"已切换到 {session.title if session else session_id}", "success")

    def _apply_model(self, model_id: str) -> None:
        """换模型：写进会话、换掉循环层配置、刷新界面。

        窗口、单价、采样参数与推理能力都按新模型重取，下一次请求体的 `model` 就是它。
        连接参数（地址与密钥）不动，那是 `/model` 表单管的事。
        """
        if not self.is_configured:
            # 没配接口时换模型没有意义：请求照样发不出去，还会弹一句「已切换」误导人。
            self.show_toast("还没有配置接口，先输入 /model 填写地址与密钥", "warning")
            return
        session = self.active_session
        if session.model_id == model_id:
            self.show_toast(f"当前已经在用 {model_id}", "info")
            return
        session.model_id = model_id
        current = self.host.loop_for(session.id)
        config = current.switch_model(model_id)
        # 其余已经建过的循环层也一起换：子 agent 不换的话，父会话用新模型、
        # 后台子 agent 还在打旧模型，两边看到的历史窗口与单价都对不上。
        for loop in self.host.loops():
            if loop is not current:
                loop.reconfigure(config)
        self.refresh_chat()
        self.save_state()
        if config.known:
            self.show_toast(f"模型已切换为 {config.model}", "success")
        else:
            self.show_toast(f"模型已切换为 {config.model}，窗口与单价按兜底值算", "warning")

    def _on_connection_entered(self, connection: Connection | None) -> None:
        """表单填完：写盘、重建 provider、把界面刷到新配置上。"""
        if connection is None:
            return
        try:
            save_connection(self.connection_path, connection)
        except OSError as error:
            self.show_toast(f"写 {self.connection_path} 出错：{error}", "error")
            return
        self._connection = connection
        current = self.host.loop_for(self.session_manager.current_session_id)
        config = current.reconfigure(self._provider_config())
        for loop in self.host.loops():
            if loop is not current:
                loop.reconfigure(config)
        self.session_manager.current.model_id = config.model
        self.refresh_chat()
        self.save_state()
        self.show_toast(f"接口已配置：{config.model} @ {config.base_url}", "success")

    def _on_theme_picked(self, theme: str | None) -> None:
        """切换主题。"""
        if not theme:
            return
        self.theme = theme
        self.session_manager.theme = theme
        self.save_state()
        self.show_toast(f"主题已切换为 {theme}", "success")

    def _ensure_chat_screen(self) -> None:
        """保证当前显示的是聊天界面。"""
        top = self.screen
        if isinstance(top, ChatScreen):
            top.rebuild()
        elif isinstance(top, HomeScreen):
            self.switch_screen(ChatScreen(self.root))
        else:
            self.push_screen(ChatScreen(self.root))

    def refresh_chat(self) -> None:
        """重建聊天界面的消息列表。

        当前屏幕可能是盖在上面的弹窗（例如删掉当前会话之后，列表还开着），
        所以要沿着屏幕层级往下找聊天界面。
        """
        for screen in reversed(self.screen_stack):
            if isinstance(screen, ChatScreen):
                screen.rebuild()
                return

    def _apply_display_toggles(self) -> None:
        """把显示开关应用到消息列表。"""
        top = self.screen
        if isinstance(top, ChatScreen):
            top.messages.set_details(self.session_manager.show_details)
            top.messages.set_thinking(self.session_manager.show_thinking)

    def show_diff(self, diff_text: str | None = None) -> None:
        """打开 Diff 查看器。"""
        self.push_screen(DiffOverlayScreen(diff_text))
