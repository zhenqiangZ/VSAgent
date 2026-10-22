from __future__ import annotations

from pathlib import Path

from textual.app import App

from .core.agent.domain.all_tools import domain_tools
from .core.agent.domain.export import runs_dir
from .core.web.service import WebService, web_root
from .core.agent.hooks import HookRunner, default_hooks_paths, load_hooks
from .core.agent.ledger import SessionLedger
from .core.agent.loop import AgentLoop, max_steps_from_env
from .core.agent.memory import MemoryStore, RecallTool, RememberTool
from .core.agent.task import TaskTool
from .core.agent.permission import (
    PermissionPolicy,
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
    ToolRegistry,
    WriteTool,
)
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
from .core.models import EFFORTS, Message
from .core.project_scan import merge_agents_md, scan_project
from .core.session_manager import SessionManager
from .env import load_env
from .screens.chat import ChatScreen
from .screens.connection_form import ConnectionFormScreen
from .screens.diff_overlay import DiffOverlayScreen
from .screens.help import HelpScreen
from .screens.home import HomeScreen
from .screens.message import MessageScreen
from .screens.session_picker import SessionPickerScreen
from .screens.theme_selector import ThemeSelectorScreen
from .screens.tools_panel import ToolEntry, ToolsPanelScreen
from .widgets.toast import ToastNotification

# 需要参数的命令：命令名之后那一段交给处理函数，其余命令不吃参数。
ARGUMENT_COMMANDS = frozenset({"remember", "forget", "task", "model"})


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
        self._ledger: SessionLedger | None = None
        self._memory: MemoryStore | None = None
        self.web_service = self._build_web_service()
        self.agent = self._build_agent()
        self._removed_message: Message | None = None
        self._removed_session_id = ""

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
        )

    def _build_agent(self) -> AgentLoop:
        """装配循环层。

        连接参数取自与状态文件同目录的 `connection.toml`，由应用内的 `/model`
        填写；计费与窗口按模型清单兜底。工作目录是项目目录：
        系统提示里写的路径与 BashTool 实际执行的位置是同一个，模型按提示读到的目录
        就是它在工具里操作的那一个。
        """
        load_env()
        config = self._provider_config()
        memory = self._build_memory()
        registry = self._build_registry(memory)
        return AgentLoop(
            self.event_bus,
            OpenAICompatProvider(config),
            registry,
            config,
            context_window=config.context_window,
            workspace_root=self.root,
            policy=self._build_policy(),
            hooks=self._build_hooks(),
            ledger=self._build_ledger(),
            memory=memory,
            viewer=self.web_service,
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

    def _build_ledger(self) -> SessionLedger:
        """装配登记表。

        会话级状态，按当前会话编号写盘到 `~/.opencode_tui/ledgers/`，
        与状态文件同处。会话换掉时由 `_sync_ledger` 重新载入。
        """
        if self._ledger is not None:
            return self._ledger
        self._ledger = SessionLedger(session_id=self.session_manager.current_session_id)
        self._ledger.load()
        return self._ledger

    def _sync_ledger(self) -> None:
        """把登记表切到当前会话。

        会话切换发生在界面里（新建、选中），循环层只拿得到登记表对象本身，
        所以在每一次发问之前校准一次——比在每个切换动作上都埋一次调用稳。
        """
        manager = self.session_manager
        if self._ledger is None:
            self._ledger = self._build_ledger()
            self.agent.ledger = self._ledger
            return
        if self._ledger.session_id == manager.current_session_id:
            return
        self._ledger = SessionLedger(session_id=manager.current_session_id)
        self._ledger.load()
        self.agent.ledger = self._ledger

    def _build_policy(self) -> PermissionPolicy:
        """装配判定层。

        两份配置在这里各读一次，改配置要重启应用。规则文件与状态文件不同：
        `AGENTS.md` 是每次发问之前重新读，那一个改完立刻生效。
        """
        if self._policy is not None:
            return self._policy
        project_path, user_path = default_permission_paths(self.root)
        return PermissionPolicy(
            project_rules=load_permissions(project_path, "项目配置"),
            user_rules=load_permissions(user_path, "用户配置"),
            # 工作目录交给判定层，它据此判「这一步是不是踩到了外面」。
            # 与 BashTool / ReadTool 拿到的 cwd 是同一个目录，边界因此只有一处说法。
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

    def _build_registry(self, memory: MemoryStore, disabled: frozenset[str] | None = None) -> ToolRegistry:
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
        self.agent.registry = self._build_registry(self._build_memory())
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
        self.event_bus.subscribe("turn.done", self._on_turn_done)
        self.push_screen(HomeScreen())

    def on_unmount(self) -> None:
        """退出时把服务停掉、日志文件关掉。

        服务要显式停：`daemon=True` 只保证进程结束时它跟着结束，
        端口要等进程真正退出才释放。不显式停的话，下一次启动会归到
        「上次没退干净」那条路上（靠端口避让兜着，但那是兜底不是常态）。
        """
        self.web_service.stop()
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

    def _command_sessions(self) -> None:
        """列出会话。"""
        sessions = self.session_manager.list_sessions()
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
        session = self.session_manager.current
        if not session.messages:
            self.show_toast("当前会话还没有消息", "warning")
            return
        target = self.root / export_filename(session)
        target.write_text(render_session_markdown(session), encoding="utf-8")
        self.show_toast(f"已导出到 {target}", "success")

    def _command_memory(self) -> None:
        """列出项目记忆。"""
        store = self._build_memory()
        entries = store.recall("")
        if not entries:
            self.show_toast("项目记忆还是空的", "info")
            return
        lines = [store.render_line(entry) for entry in entries]
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
        state = self.session_manager.current.task_state
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
        session = self.session_manager.current
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
        session = self.session_manager.current
        if not session.messages:
            self.show_toast("没有可撤销的消息", "warning")
            return
        self._removed_message = session.messages.pop()
        self._removed_session_id = session.id
        self.refresh_chat()
        self.save_state()
        self.show_toast("已撤销最后一条消息", "success")

    def _command_redo(self) -> None:
        """恢复被撤销的消息。"""
        if self._removed_message is None or self._removed_session_id != self.session_manager.current_session_id:
            self.show_toast("没有可重做的消息", "warning")
            return
        session = self.session_manager.current
        session.messages.append(self._removed_message)
        session.touch()
        self._removed_message = None
        self.refresh_chat()
        self.save_state()
        self.show_toast("已恢复被撤销的消息", "success")

    def _command_compact(self) -> None:
        """把当前会话前面的一段压掉。"""
        session = self.session_manager.current
        if len(session.messages) <= KEEP_MESSAGES:
            self.show_toast(f"消息不足 {KEEP_MESSAGES} 条，不用压缩", "info")
            return
        before = session.compact_upto
        left = self.agent.compact_session(session)
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

    def _on_turn_done(self, data: dict[str, object]) -> None:
        """一轮生成结束后持久化状态。"""
        self.save_state()

    def _command_log(self) -> None:
        """显示当前会话的事件日志路径。"""
        path = self.trace.current_path()
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                state = f"已有 {sum(1 for _ in handle)} 行"
        else:
            state = "还没有内容，发起一轮生成就会出现"
        self.push_screen(MessageScreen("事件日志", [f"日志文件　{path}", f"状态　　　{state}"]))

    def _on_session_picked(self, session_id: str | None) -> None:
        """切换会话。"""
        if not session_id:
            return
        self.session_manager.switch_session(session_id)
        self.agent.policy.clear_session()
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
        session = self.session_manager.current
        if session.model_id == model_id:
            self.show_toast(f"当前已经在用 {model_id}", "info")
            return
        session.model_id = model_id
        config = self.agent.switch_model(model_id)
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
        config = self.agent.reconfigure(self._provider_config())
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
