# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## 这是什么

`opencode_tui`：一个跑在终端里的「虚拟筛选 Agent」应用。用户在 TUI 里交办一件事，
模型自己决定调用哪些工具去查靶点、取结构、准备受体、算口袋、写运行目录、打开结构查看器，
把结果与推理过程实时流回界面。附带的 `web/` + `viz/` 是同一份数据的只读网页查看端。

没有 README，没有 pyproject / setup.py / pytest / ruff / mypy 配置，也没有 Makefile。
依赖手工装，测试是**直接跑的脚本**。详细逐文件导览见 `docs/source-map.md`（很长但准确，
注意其中「56 个文件 / 17669 行」的统计已过期；具体数字随改动变化，不要拿它当基线，
且不含 `web/` `viz/`）。

CI 只有一个 `.github/workflows/ci.yml`：Linux 上跑两个判据脚本，**两份依赖都装**
（叠合与先验口袋那几条断言要 gemmi，缺了会直接抛 ToolError，不是跳过）。
没有 Windows job——`verify_exe` 要 pywinpty，还没人验过那条路。

代码注释与 docstring 一律用中文写，解释「为什么这么写」而不是复述代码。新增代码跟随这一惯例。

## 命令

依赖：`pip install -r requirements.txt`（textual / httpx / python-dotenv / tomli-w）。
可选化学栈：`pip install -r requirements-chem.txt`（meeko/rdkit/gemmi/numpy/scipy），
不装也能跑，只有 `prepare_receptor` 会返回一条带安装指令的错误。

```bash
# 启动应用（唯一入口，无参数解析）
python -m opencode_tui            # 或 python __main__.py

# 测试：都是独立脚本，不是 pytest。
python tests/test_smoke.py        # 界面 + 内核的超集，末尾会重跑 test_agent 的全部检查
python tests/test_agent.py        # 只跑内核检查，打印「通过 N 项检查」
python tests/test_agent.py --list                 # 列出检查组名
python tests/test_agent.py --only permission      # 只跑名字含该子串的组（秒级）
python tests/test_agent.py --only hooks-in-loop
python tests/dump_layout.py       # 100x30 布局快照，写到 tests/output/

# 领域工具调试：唯一需要真实网络的脚本（真的打 UniProt/ChEMBL/RCSB/PubMed）
python debug_tools.py             # 全跑 10 模块 18 工具，顺序即依赖顺序，零参数
# dock 需要外部 vina：PATH 里有 vina，或设置 VSA_VINA_BIN；没有时这一步照实失败
python debug_tools.py --list
python debug_tools.py structure fetch_structure pdb_id=4HJO        # 显式：单工具单参数
python debug_tools.py structure fetch_structure '{"pdb_id":"4HJO"}'
python -m opencode_tui.core.agent.domain.structure               # 单模块，同一套语义
# 参数支持 JSON / k=v / --file -；--reset 清登记表，--raw 跳过参数校验
# handle 写占位不写编号：{receptor:raw} / {artifact}，开跑前从登记表现取

# 网页端（只读）
python -m opencode_tui.core.web     # 包内实现的独立进程壳，--port 优先于 .env 的 VSA_WEB_PORT
python tools/serve_web.py           # 同一个 WebServer 的 CLI 壳（--state/--runs/--web/--port）

# 判据脚本
python tools/check_web_api.py       # 在副本上起服务，验 API 形状 / 并发 / 写撕裂读
python tools/build_demo_data.py     # 由 RCSB 1IEP 原始 PDB 生成 runs/1iep-demo（可复现）
python tools/check_demo_data.py     # 纯几何：口袋残基是否真的接触共晶配体（≤4.5Å、≥60%）

# 前端（viz/ 是唯一装 npm 包的地方）
cd viz && npm install && npm run build     # esbuild → viz/build/{viewer,markdown}-lib.js
node tools/check_molstar_ui.js             # Playwright，需要先起服务（DEMO_URL，默认 :8851）
node tools/check_web_ui.js                 # WEB_PORT，默认 8866；会自己 spawn serve_web.py
node tools/check_demo_datadriven.js        # 反硬编码证明：改了数据，面板文字必须跟着变
node tools/check_demo_ui.js                # 针对 3Dmol.js 演示页（DEMO_URL，默认 :8848）

# 打包（Windows）
pip install -r requirements-pack.txt
pyinstaller --clean opencode_tui.spec      # → dist/opencode-tui.exe
python tests/verify_exe.py                 # ConPTY + pyte 截图验证，需要 pywinpty/pyte
python tests/verify_exe.py --source
python tests/verify_modal_transparency.py  # 模态居中/半透明，聊天透出
```

`verify_exe.py` / `verify_modal_transparency.py` 依赖 `pywinpty`，只能在 Windows 跑。
`tools/check_*.js` 依赖 `playwright` 与 `pngjs`，但它们**不在** `viz/package.json` 里，要自己装。

## 配置

`.env` 在仓库根（已 gitignore，没有 example 文件）。`env.py:load_env()` 用 `override=False`
——已存在的真实环境变量永远压过文件里的值。密钥只从 env 读，**不写进 `state.toml`**。

| 变量 | 说明 |
| --- | --- |
| `VSA_LLM_BASE_URL` / `VSA_LLM_API_KEY` / `VSA_LLM_MODEL` | 必填，缺任一个 `ProviderConfig.from_env` 直接抛错并指名 |
| `VSA_LLM_PROVIDER` / `_TEMPERATURE` / `_MAX_TOKENS` / `_CONTEXT_WINDOW` / `_PRICE_INPUT` / `_PRICE_OUTPUT` / `_PRICE_CACHED` | 可选，其余字段从模型目录 `lookup_model()` 兜底 |
| `VSA_MAX_STEPS` | 一轮步数上限，写错**回落到 24 而不是抛错** |
| `VSA_WEB_PORT` | 本地网页端口，默认 8860，不合法同样回落 |
| `VSA_VINA_BIN` | 外部 AutoDock Vina 可执行文件；不设时从 PATH 找 `vina`，缺失时 `dock` 返回安装指引。引擎**不随 exe 打包** |
| `EDITOR` / `VISUAL` | `/editor` 外部编辑器往返 |

测试不读 `.env`：`FakeModelServer` 起在 `127.0.0.1:0`，测试进程内自己设
`VSA_LLM_BASE_URL/API_KEY/MODEL` 指过去，**全程离线**。

## 架构

四层，`app.py` 是唯一的装配根：

```
__main__.py → app.py ────────────────────────────┐  装配 + 命令中枢
                │                                 │
  screens/    widgets/    core/    core/agent/    │
  屏幕        部件        状态     智能体内核      │
                └──── 事件总线 / Textual 消息 ────┘
```

两条贯穿全仓的分界线，改代码前先确认自己在哪一侧：

1. **`core/` 不 import textual。** `session_manager.py` / `models.py` / `command_registry.py`
   都能被测试直接调用。往 `core/` 里加 `from textual...` 是破坏架构。
2. **`core/agent/` 不认识界面。** 内核只往 `EventBus` 发事件，`ChatScreen` 是唯一的
   翻译层。「把界面换成网页端」因此不需要动内核。

### 内核（`core/agent/`）

- `loop.py` — `AgentLoop` 是唯一的驱动，界面只认它的 `stream_reply / run_bash / cancel`。
  它**不维护第二份历史**：每一步从 `Session` 现场重建模型消息。硬上限
  `MAX_STEPS=24` / `MAX_STOP_RETRIES=2` / `MAX_RETRIES=2` / `MAX_FAILED_STREAK=3`。
- `tools.py` — `Tool` ABC + `ToolRegistry` + 五个内核文件工具（bash/read/write/patch/grep）
  与 `ToolSpec` / `ToolResult` / `ToolError` / `RetryableToolError` / `argument_error` 校验器。
  **依赖注入是声明式的**：工具在类属性上写 `primary_argument`（权限匹配用）、
  `context_argument`（登记表）、`task_argument`、`evidence_argument`、`viewer_argument`，
  `Tool.execute` 在 `on_arguments` 记下模型原始参数之后注入它们；声明了却收到 `None`
  直接返回「内核装配问题」，不抛。
- `context.py` — `ContextAssembler.build` 按 `PREFIX → MEMORY → TASK → RECENT → EVIDENCE → HISTORY`
  分层装配。`PREFIX` 与 `TASK` 是 `pinned`，压缩时永不驱逐（用户的约束必须活过压缩）；
  `RECENT` 整 `Turn` 搬移，`tool_call` 与它的结果永远不拆散。装不下就抛
  `ContextAssemblyError` 明说，**不静默截断**。
- `prompt.py` — `stable_prefix` 组装身份 + 工作纪律 + 项目规则。**前缀里不放时间不放进度**，
  否则每步都击穿 prompt cache。`project_rules()` 每次发问重读 `AGENTS.md`（`RULES_FILE`，32KB 上限）。
- `permission.py` — `PermissionPolicy.check` 的判定顺序是有讲究的：取消 → 用户手打 bash →
  Plan 模式 `PLAN_DENY` → 项目规则 → **越界路径检查** → 会话 allow_all → 单工具允许 → 默认
  `{write, patch, bash, dock}` 要问。越界检查刻意排在会话「全部允许」之前：**允许一个工具不等于允许整个磁盘**。
- `hooks.py` — `hooks.toml` 里的外部命令，stdin 收一行 JSON。退出码 2 拦截（stderr 当原因），
  0 放行，30s 超时算拦截。
- `ledger.py` — 会话登记表，`receptor#1` / `ligandset#3` 这类 handle 的真相所在。
- `memory.py` — 跨会话项目记忆 `.vsmemory/`。记忆里写**文件路径不写 handle**（handle 只在本次会话有效）。
- `provider.py` — 只实现了一种协议：`OpenAICompatProvider`，OpenAI 兼容的 SSE。
  模型**名字**决定 context window / 价格 / 是否支持 reasoning；`switch_model` 会连带重算上下文窗口。
- `trace.py` — 订阅 EventBus 的 `"*"`，把事件落成 `~/.opencode_tui/logs/<session>.jsonl`。

一轮的流程：清证据表 → `stable_prefix` → 装配 context → 问模型 → 消费流
（TEXT/REASONING/TOOL_CALL/REASONING_DELTA 分流到不同 `PartType`）→ 无工具调用则跑 STOP 钩子 →
逐个（**故意不并发**）执行工具：registry 查找 → 参数校验 → PreToolUse 钩子 → `policy.check`
→ 可选的 UI 询问（发 `permission.ask`，界面弹模态）→ 执行 → PostToolUse 钩子 → 收证据 →
连续 3 步整步全失败则 `EXIT_TOOL_FAILED`。任何非正常退出都会触发一次**不带工具的收尾总结**
（模型写，失败则本地兜底）。

### 领域工具（`core/agent/domain/`）

分界线按「需要知道什么」划，不按「做什么」划：知道 UniProt 编号长什么样、口袋残基怎么算
→ 放这里；只需要知道「有个文件」→ 放内核那批。

- 每个登记工具的模块暴露一个模块级 `domain_tools() -> list[Tool]`，文件末尾带
  `if __name__ == "__main__": from debug_tools import main` 块。
- `all_tools.py:domain_tools()` 按**浏览 → 检索 → 改动**的顺序拼装
  （listing, uniprot, structure, chembl, rsearch, literature, receptor, ligand, docking, export）。
  这个顺序同时决定了工具在请求体里的先后，靠前的更容易被模型选中——**顺序是语义，不是排版**。
- 配体链路：`import_ligandset`（文件 → raw ligandset）→ `prepare_ligands`（ETKDGv3 +
  MMFF/UFF + Meeko → prepared ligandset）→ `dock`（Vina → docking artifact）→
  `write_run(docking=...)`（pose 进运行目录）。`ligand.py` 与 `docking.py` 是这条链，
  `products.py` 是两者共用的产物目录/原子写盘。
- 不登记工具的模块（`pockets` / `complexes` / `prankweb` / `align` / `chemistry` / `pdbtext` / `http` / `products`）
  是库层，被 `WriteRunTool` 之类调用。口袋来源因此是四层叠合：共晶配体 → 复合物 → p2rank 预测 → 先验叠合。
- `http.py` 是**全仓唯一的网络出口**，测试整体替换的就是它。4xx → `ToolError`，
  429/5xx/网络 → `RetryableToolError`。
- `chemistry.py` 是不许在顶层 import 可选依赖的守门人，测试用 AST 断言这一条
  （`test_agent.py:check_chemistry_gate`）。装依赖后要**重启会话**，探测只在进程启动后第一次调用时做一遍。

`domain/__init__.py` 顶部有**八条纪律**，是踩坑攒的而非风格偏好。新加领域工具逐条对一遍，
其中最容易违反的三条：收 handle 不收路径（全库只 `import_structure` 与
`import_ligandset` 收路径）、
纯查询不声明 `context_argument`、要算数就用代码算（RMSD / Tanimoto / 接触距离绝不交给模型心算）。

### 界面（`screens/` + `widgets/`）

单栈，没有 router。`on_mount` 进 `HomeScreen`，提交后 `switch_screen(ChatScreen)`；
`refresh_chat` 反向遍历 `screen_stack` 找到模态下面的 Chat。

- `ChatScreen` 是主表面，**唯一**订阅 EventBus 并翻译成 `MessagesPane` 改动的屏幕，
  退订用 `on_mount` 存下来的 unsubscribe 闭包。
- `MessagesPane` 是虚拟化的：只维护一个 `[mounted_start, mounted_end)` 挂载窗口，
  滚动时裁剪/扩展。它有 `on_removed` 回调让 ChatScreen 在拆除前先退订（注释里记着一次真实的 `NoMatches` 竞态）。
  多个 handler `prevent_default()` 之后要调 `stylesheet.update_nodes([self])`，否则整棵子树重排要 2.6–3.9 秒。
- `EditorPane` 有三种模式 `mode-chat` / `mode-command` / `mode-bash`（CSS 类切换），配三个自动补全弹窗。
- 命令表 `core/command_registry.py` 有约 24 个斜杠命令。**命令刻意没有键位**，键位单独放
  `core/keybind_manager.py`，且那两个表只用于渲染帮助——smoke 测试会比对它们和 `HomeScreen.BINDINGS`。
- `app.py:execute_command` 解析 `/name args`，查表，然后按约定分发到 `self._command_<name>`。
  只有 `ARGUMENT_COMMANDS = {"remember","forget","task"}` 会拿到参数尾巴。
  命令加了但没接处理器，会显示「尚未接入」。

### 状态与持久化

真相在 `~/.opencode_tui/state.toml`（`session_manager.py:default_state_path()`），TOML + `STATE_VERSION=1`。
同目录还有 `ledgers/`（按会话 id 隔离），traces 在 `logs/`，运行产物在仓库根的 `runs/`。

写入是**原子替换**：同目录临时文件 → flush → `os.fsync` → `_replace_with_retry`（40 次 × 5ms 重试 `PermissionError`）。
读回来调 `sync_sequences` 把 ID 计数器推到已加载 ID 之前。网页端**每个请求都重新从磁盘读**，
所以网页看到的永远和 `state.toml` 一致——这是它跟 TUI 之间唯一的数据通路，没有 SSE、没有 websocket。

### 网页端

`core/web/routes.py` 的 `SPEC` 是路由的**唯一真相**，包内常驻服务和 `tools/serve_web.py`
都从它拿，别在第二处写路由。端点就三个：`GET /api/sessions`、`GET /api/sessions/<id>`、`GET /api/runs`，
外加 `/runs/...` 静态与 `web_root` 静态。路径逃逸用 `resolve_within` 拒绝（403）。

`web/` 无构建步骤，`app.js` 是 hash router（`#/history`、`#/structure/<run_id>`），
vendored 大包（3MB Mol\*）按页懒加载。`viz/` 是唯一装 npm 包的地方，用 esbuild 把 Mol\* 和 marked
打成 IIFE 全局 `vsViewer` / `vsMarkdown`——页面代码不 import 任何 npm 包，换 3D 库只动 `viz/`。

`opencode_tui/themes/default.json` **目前没有任何代码读它**，是设计参考稿；真实颜色硬编码在
`styles/app.tcss`，主题切换走的是 Textual 自带的 `available_themes`。

## 约定

- **新增领域工具**：在模块里加 `domain_tools()` 返回值 → 需要的话在 `domain/__init__.py` 的
  `__all__` 补名字 → 在 `domain/all_tools.py` 按浏览/检索/改动定位插入 → 在 `debug_tools.py`
  的 `CALLS` 补一行（顺序是依赖顺序）→ 过一遍 `domain/__init__.py` 的八条纪律。
  漏了 `all_tools.py` 那一处，现象是「工具写了但模型看不见」，且各处判据都还是绿的。
- **依赖注入通道是声明式的**：工具靠 `Tool` 的类属性声明要什么，不要在 `run()` 里
  `assert ledger is not None` 然后自己抛——内核统一返回「内核装配问题」。
- **失败返回 `ToolResult(failed=True)`，不抛异常**：错误文本要能回灌给模型自纠。
- **返回值要么有预算要么别返回**：长清单按条数截断并在末尾写明「还剩多少、怎么取到」。
- **截断口径只能有一份**：像 `domain/listing.py:render_entries` 这种中间层要公开出去共用，
  各写一份的话改了其中一处会在长会话里把上下文吃干，而看上去完全正常。
- **`.probe/` 是所有测试与脚本的临时产物目录**，已 gitignore，可以放心写。
- `AGENTS.md` **已纳入版本控制**（`.gitignore` 里它是唯一例外），`prompt.py:rules_directory()`
  读的就是它，且按**代码位置**定位而不是启动目录——源码运行时是仓库根，打包后是 exe 所在目录。
  它在每个 step 注入系统提示的「项目规则」段，改完下一个 step 即生效；32KB 上限，超了按整行截断。
