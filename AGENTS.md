# AGENTS.md

This file provides guidance to the AI agent when working with code in this repository.

本文件是编码指引。运行时规则在 `dist/AGENTS.md` 与 `dist/rules/`（主控以 `dist/rules/agents/principal.md` 为准），由 `roles/prompt.py:rules_directory()` 定位、`vsagent.spec` 打包；两份不要互相复制或移动。用户说「AGENTS.md」指根目录这份，重读再继续。

## 强制规则

细则见 `CODEBUDDY.md`；下面是最容易违反、违反后返工代价最大的部分。

- 回答、文档、注释用中文，identifier 保留英文原名；用两个及以上汉字的完整词（崩溃、终止、判定、推断、抛出、挂起），不用单字版本。不用「不是…而是…」「要…而不是…」对比句式，不写总结与总起，不用「落地」「钉死」「对齐」这类黑话；禁用字：栈、落、死、拆、契约、偏。
- 注释解释「为什么这么写」，不重复代码做了什么。
- Python 文件不写文件头 docstring，不写 shebang。
- import 不包 try-except，需要的库直接 import；可选依赖在函数体里延迟导入（见「架构红线」）。
- 出错就地崩溃，不捕获、不做降级；不写 mock、假数据，或只为让测试通过的绕过。
- 修改代码只用文件编辑工具，不用 sed / perl / heredoc / Python 脚本改代码；不在 Bash 里内联超长命令或脚本，脚本先写成文件。
- 临时产物写 `.probe/`（已 gitignore），不读写 `/tmp`。
- 功能实现后立刻跑相关判据并迭代到通过，停在「请用户测试」不算完成。
- 以疑问句结尾的消息是问题，只回答；不追加方案，不反问。
- 不用 Git 回滚代码；要恢复到上一个状态，用编辑工具手动改。
- 不手写 parser 解析成熟文件格式；不为省依赖自己实现已有库。
- 不用 ASCII Art 画图；需要图时用 mermaid。
- 先把代码读完再动手，不擅自进入 plan mode。

## 运行与判据

Python 3.12。没有 pyproject / setup.py / pytest / ruff / mypy / Makefile，测试是直接跑的脚本。

- 全量判据：`python tools/check_all.py`，按 agent → smoke → agents → toolset → descriptions → toolsdoc → prefix → style → repo 的固定顺序跑，遇错即停。`--list` 列判据，`--only <子串>` 过滤。
- 内核判据：`python tests/test_agent.py`；`--list` 看组名，`--only permission`、`--only hooks-in-loop` 只跑匹配组。`python tests/test_smoke.py` 是界面加内核的超集，改内核后跑它一个就够。
- 测试依赖要装两份：`requirements.txt` 与 `requirements-chem.txt`。部分断言需要 gemmi，缺依赖会当场抛错；用「缺依赖就跳过」掩盖过去等于让出覆盖。
- 测试全程离线：`test_agent.py` 默认设 `VSA_ROUTE=off`、`VSA_VALIDATE=off`、`VSA_SAR=off`，模型走 `FakeModelServer`；只有 `python debug_tools.py` 打真实网络。
- 启动：`python -m vsagent`。网页：`python -m vsagent.core.web`（默认端口 8860）。
- 前端：只有 `viz/` 装 npm 包，`cd viz && npm install && npm run build`；`web/` 无构建步骤。`NODE_PATH=viz/node_modules node tools/check_web_ui.js`，playwright 要单独装，不在 `viz/package.json` 里。
- 打包只在 Windows 验证：`pip install -r requirements-pack.txt && pyinstaller --clean vsagent.spec`；`tests/verify_exe.py` 需要 pywinpty。

## 配置

- 没有必填环境变量，应用可直接启动。`.env` 在仓库根、已 gitignore、没有示例文件；`vsagent/env.py:load_env()` 用 `override=False`，真实环境变量优先。打包产物留在仓库里时（`dist/VSAgent`）读仓库根这一份，被挪出仓库后读 exe 旁边那一份，`vsagent.spec` 打包时会把仓库根 `.env` 复制过去。
- 模型接口不读 `.env`：应用内 `/model` 写入 `~/.vsagent/connection.toml`（权限 0600）。
- 可选依赖：`requirements-chem.txt`（缺失时准备受体、准备配体等少数工具返回安装指令）；`requirements-embed.txt` 加显式下载 `python -m vsagent.core.embed download`（约 255MB，不自动下载）；亲和力打分用 `models/DTA/.venv`（Python 3.10，独立环境，不并进主仓 `.venv`）。
- 外部程序用 `VSA_VINA_BIN`、`VSA_AUTOGRID_BIN`、`VSA_DTA_*` 指定；缺失时对应工具返回安装指引。网页端口用 `VSA_WEB_PORT`。

## 架构红线

- `core/` 不 import textual；`core/agent/` 不认识界面，只通过 `EventBus` 发事件，界面翻译在 `ChatScreen`。
- 领域层可选依赖（rdkit / meeko / gemmi / fastembed / torch）不在顶层 import；`domain/chemistry.py` 是守门人，`tests/test_agent.py` 用 AST 断言这一条。DTA 只在 `models/DTA/.venv` 里通过子进程调用，主仓不 import torch。
- `domain/http.py` 是全仓唯一网络出口，测试整体替换它；新增网络请求走这里。
- `core/paths.py:project_root()`：源码运行是仓库根，打包后是启动目录，不能按 `__file__` 算；`corpus/`、`runs/`、`logs/` 都以它为根。
- 状态真相是 `~/.vsagent/state.toml`，写入走原子替换；网页端每个请求重新读磁盘，没有 websocket。
- Web 路由的唯一真相是 `core/web/routes.py` 的 `SPEC`，包内服务与 `tools/serve_web.py` 都从它取，不另写一份。
- 能力层已删除：路由直接指向九个角色名，工具名单在 `roles/agents.toml`，角色说明在 `roles/catalog.py`；不再引入能力名或能力路由。

## 工具与产物约定

- 新增领域工具：模块里加 `domain_tools()` → 需要时补 `domain/__init__.py` 的 `__all__` → 在 `roles/agents.toml` 加进角色名单 → 在 `debug_tools.py` 的 `CALLS` 按依赖顺序补一行 → 逐条对 `domain/__init__.py` 的九条纪律。`tools/check_agents.py` 会抓出没进任何名单的领域工具。
- `domain/all_tools.py:domain_tools()` 的拼接顺序会影响模型的选择，改动前先想清楚。
- 依赖注入是声明式的（`primary_argument`、`context_argument`、`task_argument`、`evidence_argument` 等类属性）；不要在手写 `run()` 里断言并自行抛出，内核统一返回「内核装配问题」。
- 失败返回 `ToolResult(failed=True, output=...)`，不抛异常；`RetryableToolError` 只标瞬时故障，工具层只重试 1 次，domain 层不许用 `except Exception` 把它包成永久失败。
- 返回值要有预算：长清单按条数截断，并在末尾写明还剩多少、怎么取到；截断实现只保留一份。
- 登记工具只收 handle，不收路径（少数以工具 schema 为准）；纯查询不声明 `context_argument`；RMSD、Tanimoto、接触距离这类数用代码算。
- `docs/tools.md` 是生成物，不要手改；改完工具或角色配置跑 `python tools/gen_tools_doc.py`，`check_tools_doc.py` 逐字节比对。
- `dist/AGENTS.md` 与 `dist/rules/` 已纳入版本控制并由 `vsagent.spec` 打包；改规则后跑 `python tests/test_agent.py --only rules` 核对编码与分档。
