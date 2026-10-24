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

依赖：`pip install -r requirements.txt`（textual / httpx / python-dotenv / tomli-w / pypdf）。
可选化学栈：`pip install -r requirements-chem.txt`（meeko/rdkit/gemmi/numpy/scipy），
不装也能跑，只有 `prepare_receptor` 会返回一条带安装指令的错误。

**语义检索（S3）另有一份可选依赖**：`pip install -r requirements-embed.txt`
（fastembed + onnxruntime + tokenizers，约 78MB），再
`python -m opencode_tui.core.embed download` 拉模型（255MB，**显式下载，不自动**）。
不装时 `search_corpus` 只跑词法两路，其余功能一概不受影响。
模型缓存 `~/.opencode_tui/models/`，**不进仓、不进 exe**——单模型就 255MB，
超过方案稿 §8.4 的 +80MB 预算，所以打包走「用户另装」这条路（与 DTA 同一种处理）。

**亲和力打分（`score_affinity`）另有一套环境**，在 `models/DTA/.venv`（Python 3.10 +
torch 2.2.1 + dgl 2.1.0），装法见 `models/DTA/README.md`。主仓一行都不 import torch——
工具是**起子进程**跑 `models/DTA/virtual_screen.py`，所以没装这套环境时应用照常启动，
只有那一个工具返回安装指引。别把它并进 `.venv`：主仓是 3.12，`pandas==1.5.3` 没有
cp312 轮子，且 MDAnalysis 要求 `numpy<2`。

`models/DTA/` **整个目录不进版本控制**（代码 54MB + venv 1.6GB + 权重 15GB），
`.gitignore` 里一行 `models/DTA/` 挡住。它不能放进 `opencode_tui/` 里面：
`check_chemistry_gate` 用 `rglob("*.py")` 扫整个包，DTA 有 45 处顶层
`import torch/rdkit`，放进去那条断言当场就红。

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
python debug_tools.py             # 全跑 11 模块 19 工具，顺序即依赖顺序，零参数
# dock 需要外部 vina：PATH 里有 vina，或设置 VSA_VINA_BIN；没有时这一步照实失败
# score_affinity 需要 models/DTA/.venv：没有时同样照实失败并给安装指引
python debug_tools.py --list
python debug_tools.py structure fetch_structure pdb_id=4HJO        # 显式：单工具单参数
python debug_tools.py structure fetch_structure '{"pdb_id":"4HJO"}'
python -m opencode_tui.core.agent.domain.structure               # 单模块，同一套语义
# 参数支持 JSON / k=v / --file -；--reset 清登记表，--raw 跳过参数校验
# handle 写占位不写编号：{receptor:raw} / {artifact}，开跑前从登记表现取

# 网页端（会话与结构只读；语料可投递、改名、删除）
python -m opencode_tui.core.web     # 包内实现的独立进程壳，--port 优先于 .env 的 VSA_WEB_PORT
python tools/serve_web.py           # 同一个 WebServer 的 CLI 壳（--state/--runs/--web/--corpus/--port）

# 判据脚本
python tools/check_web_api.py       # 在副本上起服务，验 API 形状 / 并发 / 写撕裂读 / 语料读写
python tools/check_retrieval.py     # 检索评测：词法单路 / 双词法 / 三路 + 组级指标，三条硬门禁
python tools/check_retrieval.py --list          # 只看评测集概况
python tools/build_demo_data.py     # 由 RCSB 1IEP 原始 PDB 生成 runs/1iep-demo（可复现）
python tools/check_demo_data.py     # 纯几何：口袋残基是否真的接触共晶配体（≤4.5Å、≥60%）

# 本地语料（免费离线）
python -m opencode_tui.core.index reindex        # 按盘上原件重建索引，并顺手补向量
python -m opencode_tui.core.embed download       # 拉模型（255MB；直连不通自动走 hf-mirror）
python -m opencode_tui.core.embed index          # 只补向量（换模型后跑这条）

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
——已存在的真实环境变量永远压过文件里的值。

**模型接口不走 `.env`**：地址、密钥与模型名由应用内的 `/model` 命令填写，落在与
`state.toml` 同目录的 `connection.toml`（`core/connection.py`，权限 0600，原子替换）。
没有这份文件时应用照常启动，发消息前提示先 `/model` 配置；`/model <名字>` 只换模型，
连接参数不动。`VSA_LLM_*` 这一组变量已经没有了。

| 变量 | 说明 |
| --- | --- |
| `VSA_MAX_STEPS` | 一轮步数上限，写错**回落到 24 而不是抛错** |
| `VSA_WEB_PORT` | 本地网页端口，默认 8860，不合法同样回落 |
| `VSA_CORPUS_INDEX_WEB` | `0/1`，`search_web` 的摘要是否自动入库（默认开）。关掉后它退回纯查询，不给引用号 |
| `VSA_CORPUS_EMBED` | `0/1`，语义通道总开关（默认开）。关掉只跑词法两路 |
| `VSA_EMBED_MODEL` | 嵌入模型名，默认 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 维）。**换模型要整表重建**：`python -m opencode_tui.core.embed index` |
| `VSA_EMBED_MIN_SCORE` | 余弦下限，默认 `0.30`。低于它不算命中——余弦没有「不相关」这个值，不设下限的话「本地没有」会变成「命中 29 组」 |
| `HF_ENDPOINT` | 模型下载端点，**排第一**。不设时先试 `huggingface.co`，失败**自动退到 `hf-mirror.com`**（会打印实际用的端点，不悄悄换源）；镜像的 Xet 后端会回 401，所以下载子进程里固定 `HF_HUB_DISABLE_XET=1` |
| `VSA_VINA_BIN` | 外部 AutoDock Vina 可执行文件；不设时从 PATH 找 `vina`，缺失时 `dock` 返回安装指引。引擎**不随 exe 打包** |
| `VSA_DTA_PYTHON` | 跑 `models/DTA/virtual_screen.py` 的解释器；不设时依次试 `models/DTA/.venv/bin/python` 与旧布局的 `<repo>/.venv-dta/bin/python`，都没有时 `score_affinity` 返回安装指引 |
| `VSA_DTA_ROOT` | `models/DTA`；不设时按**代码位置**定位到仓根下的 `models/DTA` |
| `VSA_DTA_DEVICE` | `cpu`（默认）/ `cuda`，写进 DTA 的 hydra 配置 |
| `VSA_DTA_WEIGHTS` | 预训练编码器目录，默认 `models/DTA/weights/`。可被更细的 `VSA_ESM_CHECKPOINT` / `VSA_CHEMBERTA_CHECKPOINT` 单独覆盖 |
| `EDITOR` / `VISUAL` | `/editor` 外部编辑器往返 |
| `VSA_FILE_READ_LOG` | 设为 `0` / `off` / `false` 关掉文件读取日志。默认写**项目根**的 `logs/file-reads.jsonl`（已 gitignore），一行一次读取：`purpose`（`tool.read` / `tool.grep` / `import.structure` / `import.ligands` / `receptor.prepare` / `prompt.file_ref` / `state.load` / `rules.load` / `tools.load` / `memory.load`）、绝对路径、字节数、`ok`，失败时带 `detail`。`app.py` 启动时按 `root` 配置目录；没有应用可问的脚本与领域工具落到当前目录的 `logs/` |

测试不读 `.env`：`FakeModelServer` 起在 `127.0.0.1:0`，测试把地址写进
`.probe/connection.toml`（与测试用的 `state_path` 同目录），**全程离线**。

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
  同模块的 `load_tool_switches` / `save_disabled` 管的是**另一件事**：`tools.toml` 里标关的工具
  根本不进注册表，模型连它存在都不知道（权限层是「装进来但拦下来」）。改开关用 `/tools` 面板，
  它写项目侧那份文件并当场重装注册表，不必重启。
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
  （listing, uniprot, structure, chembl, rsearch, literature, receptor, ligand, docking,
  affinity, export）。这个顺序同时决定了工具在请求体里的先后，靠前的更容易被模型选中
  ——**顺序是语义，不是排版**。
- 配体链路：`import_ligandset`（文件 → raw ligandset）→ `prepare_ligands`（ETKDGv3 +
  MMFF/UFF + Meeko → prepared ligandset）→ `dock`（Vina → docking artifact）→
  `score_affinity`（VCrossFormer → affinity artifact）→ `write_run(docking=...)`（pose 进运行目录）。
  `ligand.py` 与 `docking.py` 是这条链，`products.py` 是共用的产物目录/原子写盘。
- `affinity.py` 是**第二个调外部进程的工具**，但外面是一个 Python 解释器而不是二进制，
  所以它比 `docking.py` 多两件事：`dta_python()` / `dta_root()` 两个环境变量入口，
  以及**现截口袋**（受体的 `box.txt` 只给中心，模型要的是中心 10Å 内的残基）。
  分数方向与 dock **相反**（pK 越高越好），两者不可比较——描述第一段必须写明这一句。
  它不 import torch，也**不能**：`check_chemistry_gate` 的黑名单与
  `test_smoke.walk_packages` 都会当场炸，PyInstaller 还会把 2GB 依赖拖进 exe。
- 不登记工具的模块（`pockets` / `complexes` / `prankweb` / `align` / `chemistry` / `pdbtext` / `http` / `products`）
  是库层，被 `WriteRunTool` 之类调用。口袋来源因此是四层叠合：共晶配体 → 复合物 → p2rank 预测 → 先验叠合。
- `http.py` 是**全仓唯一的网络出口**，测试整体替换的就是它。4xx → `ToolError`，
  429/5xx/网络 → `RetryableToolError`。
- `chemistry.py` 是不许在顶层 import 可选依赖的守门人，测试用 AST 断言这一条
  （`test_agent.py:check_chemistry_gate`）。装依赖后要**重启会话**，探测只在进程启动后第一次调用时做一遍。

`domain/__init__.py` 顶部有**八条纪律**，是踩坑攒的而非风格偏好。新加领域工具逐条对一遍，
其中最容易违反的三条：收 handle 不收路径（全库只 `import_structure` / `import_ligandset` /
`import_documents` 收路径）、
纯查询不声明 `context_argument`、要算数就用代码算（RMSD / Tanimoto / 接触距离绝不交给模型心算）。

### 本地语料与检索（`core/` 里的五个纯库）

```
原件  corpus/doc/<id>/  corpus/web/<id>/      ← 网页端只往 inbox/ 落文件
  ↓ parse.py      抽取文字、认结构（outline → heading → page / markdown）、反转义
  ↓ chunker.py    按节切块：目标 1200、硬上限 2000、重叠=上一块最后一整句
  ↓ index.py      SQLite：chunks + 两张 FTS5（unicode61 / trigram）+ vectors
  ↓ embed.py      fastembed + ONNX（**不依赖 torch**），向量按模型名存
检索  lexical 两路（各 40 条）+ 语义一路 → RRF(k=60) → 邻块 → 按「源 + 章节」聚组
```

**`corpus/` 与 `runs/` 的根由 `core/paths.py:project_root()` 定**：源码运行时是仓根，
打包后是**启动目录**（与 `logs/` 一样）。这两个都**不能按 `__file__` 算**——冻结之后它
指向 PyInstaller 解包出来的临时目录，写进去的文献、索引与运行目录在退出时一起消失，
而每一步都返回成功；表现是「网页上语料库空了、结构页也空了，但功能都正常」。

- **每路 40 条是量出来的**（`index.CHANNEL_LIMIT`）：30 → 40 让「标注源进前 8 组」
  从 0.927 升到 0.945、「答案在片段里」从 0.796 升到 0.815，50/80 不涨。RRF 权重、
  k、章节加权、References 过滤、邻块范围都扫过，均无增益（见 `docs/knowledge-plan.md` §11）。

- **解析与切块只有一条路**：`index.chunks_for`。入库与 `reindex` 各写一份的话，
  重建出来的块跟当初写进去的不一样，而「重建两次一致」的判据正好抓不到这种漂移。
- **网页落盘当场入索引**（`websearch._index_pages` → `index.upsert_web_page`）：
  少了这一步，刚搜到的东西要等下一次 `reindex` 才查得到，而工具说明写的是「之后免费可查」。
- **向量是派生物**：`vectors` 与 `kv`（版本号）都在 `library.db` 里，删掉能重建。
  版本号由**触发器**维护，因为级联删除（删源、整源重建）也会清向量，那条路径没人会记得 bump。
  `embed._matrix` 的缓存按 `(库, 模型, 版本)` 做键——只按行数做键会拿旧 id 配新块。
- **可选依赖在函数体里导入**：`fastembed` 与 `pypdf` 都不在顶层。没装时应用照常起，
  `search_corpus` 静默降级成词法两路（技术细节只进 `meta`，不进正文），`import_documents`
  报一句带 `pip install` 的话。
- **模型不自动下载**：`embed.missing_reason()` 给一句能照着做的话，下载要人显式跑
  `python -m opencode_tui.core.embed download`。

### 界面（`screens/` + `widgets/`）

单栈，没有 router。`on_mount` 进 `HomeScreen`，提交后 `switch_screen(ChatScreen)`；
`refresh_chat` 反向遍历 `screen_stack` 找到模态下面的 Chat。

- `ChatScreen` 是主表面，**唯一**订阅 EventBus 并翻译成 `MessagesPane` 改动的屏幕，
  退订用 `on_mount` 存下来的 unsubscribe 闭包。
- `MessagesPane` 是虚拟化的：只维护一个 `[mounted_start, mounted_end)` 挂载窗口，
  滚动时裁剪/扩展。它有 `on_removed` 回调让 ChatScreen 在拆除前先退订（注释里记着一次真实的 `NoMatches` 竞态）。
  多个 handler `prevent_default()` 之后要调 `stylesheet.update_nodes([self])`，否则整棵子树重排要 2.6–3.9 秒。
- `EditorPane` 有三种模式 `mode-chat` / `mode-command` / `mode-bash`（CSS 类切换），配三个自动补全弹窗。
- 命令表 `core/command_registry.py` 有 25 个斜杠命令。**命令刻意没有键位**，键位单独放
  `core/keybind_manager.py`，且那两个表只用于渲染帮助——smoke 测试会比对它们和 `HomeScreen.BINDINGS`。
- `app.py:execute_command` 解析 `/name args`，查表，然后按约定分发到 `self._command_<name>`。
  只有 `ARGUMENT_COMMANDS = {"remember","forget","task","model"}` 会拿到参数尾巴。
  命令加了但没接处理器，会显示「尚未接入」。

### 状态与持久化

真相在 `~/.opencode_tui/state.toml`（`session_manager.py:default_state_path()`），TOML + `STATE_VERSION=1`。
同目录还有 `connection.toml`（模型接口，权限 0600）、`ledgers/`（按会话 id 隔离），traces 在 `logs/`，运行产物在仓库根的 `runs/`。

写入是**原子替换**：同目录临时文件 → flush → `os.fsync` → `_replace_with_retry`（40 次 × 5ms 重试 `PermissionError`）。
读回来调 `sync_sequences` 把 ID 计数器推到已加载 ID 之前。网页端**每个请求都重新从磁盘读**，
所以网页看到的永远和 `state.toml` 一致——这是它跟 TUI 之间唯一的数据通路，没有 SSE、没有 websocket。

### 网页端

`core/web/routes.py` 的 `SPEC` 是路由的**唯一真相**，包内常驻服务和 `tools/serve_web.py`
都从它拿，别在第二处写路由。会话与运行仍是纯 GET：`GET /api/sessions`、
`GET /api/sessions/<id>`、`GET /api/runs`，外加 `/runs/...` 静态与 `web_root` 静态。
路径逃逸用 `resolve_within` 拒绝（403）。

**S2 起网页端多了一组语料入口**（`GET /api/library`、`GET /api/library/<kind>:<id>/chunks`、
`POST /api/library/upload`、`POST /api/library/ingest`、
`POST /api/library/<kind>:<id>/{rename,delete}`）。章程因此从「只读」改成「查看只读 +
语料投递与管理」：网页端自己只往 `corpus/inbox/` 落文件、只写 `labels.json` 的显示名、
只把目录挪进 `corpus/.trash/`，**从不直接写索引**。索引只以 `mode=ro` 打开，取块数、
向量覆盖与切块正文。

**切块面板给的是索引里那一份**，不是按原件重切一遍——用户要看的就是「检索实际看到的
东西」，重切出来的那份解释不了「为什么命中了别的段落」。一份长文献几百块，所以
`offset` / `limit` 分页（默认 40、上限 100，客户端说什么都夹到上限）；索引还没建时
回空表加一句 `reason`，不是 500。

**「入库」是转交，不是自己写**：应用里网页服务就是 TUI 进程里的一个线程，
`/api/library/ingest` 把 inbox 里的文件交给装配时注入的 `ingest` 回调（应用传
`ingest_files`，与模型调 `import_documents` 同一个入口），回 202 之后在后台线程跑，
页面轮询 `GET /api/library` 的 `ingest` 字段收进度。独立运行的服务
（`serve_web.py`、`python -m opencode_tui.core.web`）没有这个回调，明确回 503——
否则就有两个进程同时改 `sources` / `chunks`。同进程内两处写由 `index.WRITE_LOCK`
串行化，索引库开 WAL，读列表不会被入库挡住。

本地服务不做鉴权，所以四个写入口都要求 `X-VSA-Request: 1`（逼出 CORS 预检）并校验
`Origin`——`multipart` 是简单请求，少了这一道，任何网页都能往 `127.0.0.1` 投文件。
入库还额外只认 **inbox 里已有的文件名**（不认路径），避免这个入口变成「让索引读任意路径」。

`web/` 无构建步骤，`app.js` 是 hash router（`#/history`、`#/structure/<run_id>`、
`#/library`），vendored 大包（3MB Mol\*）按页懒加载。`viz/` 是唯一装 npm 包的地方，
用 esbuild 把 Mol\* 和 marked 打成 IIFE 全局 `vsViewer` / `vsMarkdown`——页面代码不
import 任何 npm 包，换 3D 库只动 `viz/`。

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
