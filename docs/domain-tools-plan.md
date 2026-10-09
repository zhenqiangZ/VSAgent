# 领域工具接入计划（轮 1：工作区 + 既有领域工具）

本文件是「把参考实现 `D:/1Store/VirtualScreenAgent/src/vsa/tools/` 里的领域工具接入本项目
`opencode_tui`」这一轮的实施方案与执行清单。动笔之前先把**已核对过的事实**列清楚，
本轮的每个判断都建立在这些读数上。

## 0. 已核对的事实

### 0.1 本项目现状

| 项 | 现状 | 位置 |
|---|---|---|
| 工具基类 | 有。`ToolSpec` + `Tool`(ABC) + `ToolRegistry` + `argument_error` | `opencode_tui/core/agent/tools.py` |
| 现有工具 | 5 个：`bash` / `read` / `write` / `patch` / `grep` | 同上 |
| 工作目录 | **有，但只做两件事**：给出 root、往系统提示写一段状态文字 | `core/agent/workspace.py`（38 行） |
| 登记表 / handle / 血缘 / 指纹 | **没有** | — |
| 判定层 | 有。`PermissionPolicy` + `Tool.primary_argument` + `Verdict` | `core/agent/permission.py`（193 行） |
| 钩子层 | 有。`HookRunner` 三个事件，接在 `_execute_tool` 上 | `core/agent/hooks.py` |
| 工具执行的唯一出口 | `AgentLoop._execute_tool(session, message, part, explicit)` | `core/agent/loop.py:454` |
| 工具注册处 | `OpenCodeApp._build_registry()`，逐条 `registry.register(...)` | `app.py:132` |
| 系统提示 | `build_system_prompt(workspace, session)`，每 step 重建 | `core/agent/prompt.py:124` |
| 参数校验 | `argument_error(spec, arguments)` 按 JSON Schema 手写校验 | `tools.py:184` |
| **`runs/<run_id>/` 数据约定** | **有**。`meta.json` 的 target / pockets / molecules / contacts；Mol\* 前端与 `serve_web.py` 已按它读 | `runs/1iep-demo/`、`docs/web-frontend-architecture.md` |

### 0.2 参考实现的读数

| 模块 | 行数 | 是否收 `ctx` | 依赖 |
|---|---|---|---|
| `tools/base.py` | 700 | —（框架层） | pydantic |
| `tools/registry.py` | 209 | —（框架层） | — |
| `tools/batch.py` | 154 | —（框架层） | — |
| `tools/decorator.py` | 83 | —（框架层） | — |
| `domain/uniprot.py` | 828 | `fetch_target` **收**，`search_targets` **不收** | httpx |
| `domain/chembl.py` | 2137 | `search_actives` / `find_analogs` **收**，`describe_molecule` **不收** | httpx |
| `domain/structure.py` | 1015 | 两个都**收** | httpx + pdbtext |
| `domain/receptor.py` | 1155 | **收** | RDKit / Meeko / gemmi |
| `domain/pdbtext.py` | 542 | —（纯文本层，不登记工具） | **零第三方依赖** |
| `domain/chemistry.py` | 211 | —（依赖守门人） | 探测用 |

领域工具共 **8 个**：`search_targets` / `fetch_target` / `search_actives` / `find_analogs` /
`describe_molecule` / `fetch_structure` / `import_structure` / `prepare_receptor`。
其中**收 ctx 的 6 个、不收的 2 个**（两个纯查询）。

### 0.3 参考实现的测试做法（决定本轮能否离线验收）

全部通过**替换模块级 HTTP 入口**做到离线，不依赖网络：

- `tests/test_chembl_tools.py`（1690 行）：`monkeypatch.setattr(chembl, "_get_json", transport)`
- `tests/test_uniprot_tools.py`（547 行）：同上
- `conftest.py` 的 `settings` fixture 一律用 `Settings(_env_file=None, ...)`，
  **避免读到机器上真实的 `.env` 而让结果依赖环境**

结论：网络类工具的验收可以在完全没有网络的机器上跑完。

## 1. 命名冲突（必须先解决）

本项目已有一个 `Workspace`（`core/agent/workspace.py`），职责是"工作目录 + 一段状态文字"。
本轮要加的东西也叫 workspace（登记表），**两者同名不同物**。

处理：**新模块不叫 Workspace。**

- 新模块 `core/agent/ledger.py`
- 新类 `SessionLedger`（登记表）
- 现有 `core/agent/workspace.py` 的 `Workspace` **原样保留、职责不动**

理由：现有那个 `Workspace` 还担着两件在用的职责（`workspace_root()` 与 `state_text()`），
改名会牵动 `app.py` / `prompt.py` / 循环层三处，收益为零。
`Ctx` 注入的类型标注统一写 `SessionLedger`，与旧的 `Workspace` 在代码里一眼分得开。

## 2. 本轮产出

```
新增：
  opencode_tui/core/agent/ledger.py          登记表：Handle / Entry / SessionLedger / 指纹
  opencode_tui/core/agent/domain/__init__.py 领域工具装配入口
  opencode_tui/core/agent/domain/http.py     共用的 HTTP 出口（供测试整体替换）
  opencode_tui/core/agent/domain/chemistry.py 可选化学依赖守门人
  opencode_tui/core/agent/domain/pdbtext.py  PDB 纯文本层（零依赖）
  opencode_tui/core/agent/domain/uniprot.py  2 个工具
  opencode_tui/core/agent/domain/chembl.py   3 个工具
  opencode_tui/core/agent/domain/structure.py 2 个工具
  opencode_tui/core/agent/domain/receptor.py  1 个工具
  opencode_tui/core/agent/domain/listing.py  工作区浏览工具 3 个
  opencode_tui/core/agent/domain/all_tools.py 把各模块的工具合成一份（应用只装一次）

修改：
  opencode_tui/core/agent/tools.py   Ctx 标记 + ToolResult.meta + context 注入
  opencode_tui/core/agent/loop.py    _execute_tool 传 context；会话绑定 ledger
  opencode_tui/app.py                装配 ledger 与领域工具
  opencode_tui/core/agent/prompt.py  身份段加「收 handle 不收路径」纪律
  requirements.txt                   可选化学依赖（单列一个包文件）
```

## 3. 设计要点

### 3.1 登记表（`ledger.py`）

照参考实现的口径，**逐条都有理由**：

- **`Handle` 带类型**：`ReceptorHandle` / `LigandSetHandle` / `ArtifactHandle` 三个子类。
  工具签名写具体子类之后，「模型把 `receptor#1` 传给要配体集的参数」变成**一条可读的中文参数错误**，
  而不是工具内部的一堆 if。
- **只有三类 kind**：受体会被反复引用（一个）、配体集会被增删筛（多个）、产物是算出来的（更多个）。
  **不要再细分**——类型一旦膨胀，模型的判断负担就跟着涨。
- **`Entry.fingerprint`** 是「这是同一件事」的判据。三条算法：
  - `fingerprint_file`：内容 sha256（分块读）
  - `fingerprint_path`：**目录走「清单 + 内容」**，文件名与内容都进哈希。
    只哈希内容时「改了盒子参数又顺手改了文件名」会被算成同一份
  - `fingerprint_parts`：多个上游指纹 + 参数规范化后哈希，作缓存键
- **`Entry.parents`（血缘）**：登记非受体条目时**默认自动挂当前受体为父项**。
  没有血缘的产物在切换受体时无法被识别，会悄悄留在结果里。
- **`reconcile()` 四路规则，没有一路允许静默**：
  - 没有 path（纯内存对象）→ `ok`
  - 路径不在 → `missing`（**条目不删**，"它曾经存在"是事实）
  - 路径在、登记时没记指纹 → `ok`
  - 路径在、指纹不一致 → `stale`
- **`stale_after()` 与 `switch_receptor()` 必须给出同一批清单**。预演与真做不一致时，
  **少报是危险的那一侧**（多出来的条目静默消失）。实现上只写一份逻辑。
- **线程安全**：同步 handler 跑在线程池里，同一轮多个工具并发读写，公开方法一律持 `RLock`。

### 3.2 框架补口（`tools.py`）

四个改动，都为了撑住领域工具：

1. **`Ctx` 标记 + 注入**：`from typing import Annotated`，参数标 `Annotated[SessionLedger, Ctx]`。
   这类参数**不进 schema**，由循环层注入。现有 `argument_error` 的 schema 校验据此跳过它们。
2. **`ToolResult.meta`**：结构化计数（批量结果走这里），供界面与 trace 读取。
3. **`Tool.primary_argument` 已有**，判定层已在用——领域工具填上即可，无需改判定层。
4. **批量部分成功语义**（`BatchOutcome`）：
   一万个分子里总有几个解析不了，整批报失败会逼模型重试。
   **`ok=False` 只在"一项都没成功"时给。**

### 3.3 与既有机制的接线

| 接缝 | 做法 |
|---|---|
| 工具执行 | `loop.py:496` 的 `tool.run(part.tool_input)` 改成 `tool.run(part.tool_input, context=self.ledger)`（`Ctx` 由 `Tool.run` 自己剔除） |
| 判定层 | **不改**。`primary_argument` 填上即可自动生效 |
| 钩子层 | **不改**。已经在 `_execute_tool` 上 |
| 系统提示 | `prompt.py` 的身份段增加一句「引用产物一律用 handle，不要手写路径」 |
| 工具注册 | `app.py:_build_registry()` 里加 `domain_tools()` |
| Plan 方式 | `PLAN_DENY` 目前是 `{write, patch, bash}`。改动类领域工具（`prepare_receptor`）**要加进去**，否则 Plan 下能跑准备受体 |
| 写盘 | 登记表快照写到 `~/.opencode_tui/ledgers/<session_id>.json`，**原子换名**（沿用 `SessionManager.save_to_toml` 那套：临时文件 + fsync + os.replace + 占用重试） |

### 3.4 与 `runs/` 的接轨（本项目独有，参考实现没有）

登记表与 `runs/<run_id>/` 是**过程与交付**的关系，不是替代：

- 登记表记录**全部中间产物**（含永不给人看的：原始 PDB、去水中版本、PDBQT 目录…）
- `runs/<run_id>/` 只装**最终要交付的那几样**，形状由 `meta.json` 约定写定
- 两者之间靠**终点导出**连接：`receptor#1` 的路径 → `receptor.pdb`，
  `ligandset#2` 里 top-10 → `ligands/pose-NN.pdb`

**本轮不做导出**（那是轮 4 的 `write_run`），但登记表的字段设计要为它留好口子：
`Entry.meta` 里记足够的信息（靶点、口袋、候选排序），让导出时不必回头看消息历史。

### 3.5 依赖策略

化学依赖（RDKit / Meeko / gemmi）走**可选 extra**，与参考实现同款：

- 不装它的人照常能用内核（检索、笔记、会话、界面全不受影响）
- **顶层不许 import rdkit**（一律延迟到函数体），有测试用 AST 扫全仓守住
- 缺依赖时返回**一条带安装指令的错误**，模型能把它原样转述给用户
- 已实测：`scripts/probe_chem_stack.py` 会重测这条不变量

## 4. 执行清单

分 9 步。**每步做完都能单独验证**，且**每步都应保持现有 517 + 1014 项断言全绿**。

### 第 1 步　登记表

产出：`core/agent/ledger.py`（Handle 三个子类 / Entry / SessionLedger / 三条指纹 / reconcile）

验收：
- 三个 kind 各登记一条，`str(handle)` 形态正确
- 把 `receptor#1` 传给要 `LigandSetHandle` 的参数 → **报中文错且说明手上是什么**
- `fingerprint_path` 对目录与文件都正确；**加一个空子目录会改变指纹**
- 血缘：登记时自动挂当前受体；`stale_after` 与 `switch_receptor` 返回**同一批**
- `reconcile` 四路各一条断言；**missing 的条目不删除**
- 快照往返：写出再读回，`isinstance(entry.handle, ReceptorHandle)` 仍成立

**状态：已完成。** 断言写在 `tests/test_agent.py::check_ledger()`（30 条），
随 `run_agent_checks()` 一起跑。agent 层从 517 涨到 547。

实现过程中定下、原方案没写到的两点：

1. **切到当前受体自身是空操作。** 原先 `switch_receptor` 无条件执行「搁置旧受体血缘下的一切」，
   于是 `switch_receptor`(当前受体) 会把它自己连同它下面所有条目一起搁置，
   再把同一个 handle 设成当前——结果是**当前受体显示为已搁置，而默认视图（只列当前受体的条目）
   变成空的**。重新点一次已有的受体是完全正常的动作，不能有「点第二下清空工作区」的后果。
   现在开头多一条 `if self._current_receptor == target: return []`。

2. **默认视图会藏起搁置的条目。** `entries()` 默认 `include_archived=False`，
   切换受体后旧受体及其产物不出现在默认列表里（这是设计，默认只列当前受体的上下文）。
   要看全部得显式传 `include_archived=True`。断言里两个视图都验了。

### 第 2 步　框架补口

产出：`tools.py` 的 `Ctx` / `ToolResult.meta` / `context` 注入 / `BatchOutcome`

验收：
- 标了 `Ctx` 的参数**不出现在 schema 里**，且 `argument_error` 不再校验它
- 未注入 context 时调用 → 报「内核装配问题」而不是「参数错误」
- 批量：1 成功 99 失败 → `ok=True` 且失败按原因分组；100 全失败 → `ok=False` 且提示「重试无用」

**状态：已完成。** 断言写在 `tests/test_agent.py::check_tool_context()`（13 条）。

实际做法与上面写法的一处出入：**没有用 `Annotated[SessionLedger, Ctx]` 这种参数标注**
（那需要解析签名的运行时反射，本项目现有工具都是手写 `ToolSpec.parameters` 的，
不引 pydantic 就得自己写一套签名推断）。改成基类上一个 `context_argument: ClassVar[str]`：
工具声明「我收 context 的那个参数叫什么」，`Tool.execute()` 按它把 context 塞进 arguments。
`Ctx` 类仍保留，作该字段的说明性标记（`context_argument` 的取值就是标了 `Ctx` 的那个参数名）。
这样：现有 5 个工具一个字节都不用改（默认空串 = 不收 context），注入点仍然只有一个。

其余按方案：`ToolResult.meta` 默认 factory；`RetryableToolError` 单独立一类；
`BatchOutcome` 的 `ok` 是「至少成功一条」，`total=0` 也算不成功（空批不该被当通过）。

接线只有一处：`loop.py` 的 `tool.run(...)` → `tool.execute(part.tool_input, context=self.ledger)`。
`app.py` 加登记表装配与 `_sync_ledger()`，`screens/chat.py` 发问前调一次。

### 第 3 步　HTTP 出口 + uniprot 2 个工具

产出：`domain/http.py`、`domain/uniprot.py`

验收：用假传输（**不联网**）跑通 `search_targets` / `fetch_target`；
`fetch_target` 登记 `receptor` 条目；**`search_targets` 不登记任何条目**（它是纯查询）。

**状态：已完成。** 断言写在 `tests/test_agent.py::check_uniprot_tools()`（23 条），
用一个替换掉 `http.get_json` 的假传输跑，全程不联网。

两处与原方案的出入：

1. **`fetch_target` 登记的是 `artifact`，不是 `receptor`。** 取回来的是一份**参考记录**
   （功能注释、PDB 清单、结构域），还没有变成可对接的结构文件——把它登记成受体，
   会让后续「准备受体」拿到一份 JSON 当结构用。真正的 `receptor` 条目由第 5 步的
   `fetch_structure` 产生（它取回的是坐标）。靶点记录按 `artifact` 登记。

2. **缓存目录定在 `~/.opencode_tui/targets/`**，与登记表快照同处；测试里把这个函数
   换成临时目录，不污染真实缓存。

### 第 4 步　pdbtext（纯文本层）

产出：`domain/pdbtext.py`（从参考实现移植，零依赖）

验收：选链 / 删水 / 抽共晶配体 / 算盒子四项各一条；
**核心不变量：「没被要求动的行，一个字节都不动」**——取一份 PDB，做一次删水，
断言其余行与原文件逐字节相同。

**状态：已完成。** 断言写在 `tests/test_agent.py::check_pdbtext()`（30 条）。

直接移植，只做两处适配：

- 参考实现从 `structure.py` 导入列位常量（`ATOM_CHAIN` / `WATER_RESIDUES` 等），
  本项目**反过来**由 `pdbtext.py` 定义这些常量、第 5 步的 `structure.py` 从它导入。
  理由：`pdbtext` 是零依赖的那一个，让它当唯一定义处，方向才不会绕。
- 注释里若干处用了本项目用字规则禁用的字，按规则逐个换了词（清单见
  `.probe/check_banned_chars.py`，改动处都在 `pdbtext.py` 的注释与那一份断言的说明里）。

踩到两次「断言预设写错、代码是对的」，都记在这里避免重复：

1. 原以为「先删链 B 再删杂原子」与「只删杂原子」结果一致——**不是**：前者把链 B
   也删了，后者留着。真正成立的不变量是 **同目标集合下编辑顺序无关**：
   「先删链 B 再删杂原子」==「先删杂原子再删链 B」。断言已改成后者。
2. `keep_chains` 的删除计数：样例里链 B 只有 1 个原子（水在链 A 上），我按 2 写了。
   改后的断言反而更能说明问题——删链 A 时计数是 5（2 蛋白 + 1 水 + 2 配体），
   正好证明 `ATOM` 与 `HETATM` 是一起按链判的。

### 第 5 步　structure 2 个工具

产出：`domain/structure.py`

验收：`fetch_structure` 取回结构与「这套结构里有什么」摘要，登记 `receptor`；
**同一编号第二次调用直接读盘、不联网**（用假传输计数请求次数为 0）；
`import_structure` 是**唯一收路径的动作**，重复导入同一文件不重复登记。

**状态：已完成。** 断言写在 `tests/test_agent.py::check_structure_parse()`（17 条，纯解析）
与 `check_structure_tools()`（55 条，两个工具，假传输 + 临时目录，全程不联网、不碰真实家目录）。

四处与原方案的出入：

1. **`http.py` 多了 `probe()` 与 `NotFound`。** 原方案说 `_download` 走 `get_text`、
   404 分两档靠 `ToolError` 的文案判读——那要靠**字符串比对报错里有没有「404」**，
   响应体里凑巧带上这几个字符就会误判。改成：`_request` 遇 404 抛 `NotFound`，
   `get_text_or_none` 把它变成 None 返回；`probe()` 发 HEAD 只问「有没有」
   （超大条目的 mmCIF 能到几十 MB，不能下载）。判读从「比字符串」变成「看类型」。

2. **`import_structure` 的指纹算原始字节。** 下载那条路指纹算文本（它本来就是文本、
   写盘走同一段编码），导入这条路必须算 `fingerprint_bytes(payload)`——
   用「解码再编码」后的文本算指纹，遇到非 UTF-8 字节时 replace 会把不同内容变成同一个
   指纹，「同一份结构」立刻会认错。

3. **链的原子数含 HETATM。** 参考实现里 `build()` 的 `atom_count` 是
   `atom_counts + hetero_counts`，一开始把断言按「只有蛋白原子」写了，实测是 6（含 1 水 + 2 配体）。

4. **修饰残基那一行只列「以 HETATM 出现」的 MODRES 残基。** `ranked(modified_names)`
   是从 `hetero_residues` 里筛的，所以一份把 MSE 写成 ATOM 的文件，`modified` 会是空的
   ——这与参考实现一致。样例里按真实 PDB 的习惯把 MSE 写成 HETATM，两条路都验到了：
   MSE 不混进配体（被 MODRES 筛掉），配体只剩 AQ4 与 NAG。

顺带踩到一件值得记的事：**远端取的 1M17 与本地导入的 1M17 会撞同名**
（两者 HEADER 里都写着 1M17，但字节不同）。`_import_target` 的「同名不同内容加序号、
绝不覆盖」正好接住这个撞车——导进来的写成 `1M17-1.pdb`，远端那份原样不动。
断言已改成按这个真实结果写，并逐字节核对两边都没被对方改过。

### 第 6 步　chembl 3 个工具

产出：`domain/chembl.py`

验收：假传输下 `search_actives` 登记 `ligandset`，`find_analogs` 登记 `ligandset`，
**`describe_molecule` 不登记**；
`pChEMBL 越大活性越强`这条口径写进描述第一段（只有第一段进工具清单）。

**状态：已完成。** 断言写在 `tests/test_agent.py::check_chembl_tools()`（96 条，
假传输 + 临时缓存目录，全程不联网、不碰真实家目录）。

几处值得记的：

1. **`http.py` 多了 `server_5xx_as_error`。** ChEMBL 的相似性检索把「结构解析不了」
   回成 HTTP 500，与服务器故障同码。照默认口径报出去，模型看到的是「服务暂时不可用，
   稍后重试即可」，然后对一个写错的结构照做一遍。填一段说明文字之后，5xx 不再算可重试，
   而是确定性失败并把这段文字带上。判读从「看状态码猜病因」变成「调用方说明病因」。

2. **服务端过滤只发实测生效的那几个。** `confidence_score` 挂在 assay 表上，
   activity 表不认它——实测 `activity.json?...&confidence_score=9` 返回 200 且命中数与
   不过滤时一模一样。任何参数名写错都不会炸，只会静默返回没筛过的数据。
   所以只发 `pchembl_value__gte` / `standard_type__in` / `potential_duplicate` / `order_by`
   四个，其余（置信度、归并、剔除）一律在本地做。断言里有两条专门挡这个：
   `confidence_score` 与 `assay__*` 都不许出现在请求参数里。

3. **`order_by=-pchembl_value` 是必须的，不是可选优化。** 不排序时返回顺序是任意的
   （实测前三条是 7.39 / 6.52 / 6.77），一旦命中数超过上限，拿到手的就是随便一批。
   排序之后「取回上限」的含义才变成「最强的那些」。断言按「取活性那一页必须带排序」写，
   不按某一页的数值写。

4. **靶点选型只取 `SINGLE PROTEIN`。** 实测 `P00533` 查出 16 个候选，只有一个是 EGFR
   本身，其余是蛋白家族（EGFR 连同 ErbB2/3/4）、嵌合蛋白、蛋白间互作。不选型就会把整个
   家族的活性当成这一个靶点的。假载荷里放了单体 + 家族 + 嵌合三条，断言逐个数
   「排除掉 2 个」。

5. **缓存键不含 ChEMBL 靶点编号。** 靶点编号要先联网解析才知道，把它算进键里就意味着
   缓存永远不可能纯读盘。键只用「UniProt 编号 + 条件」。断言用「第二次调用新增请求数
   为 0」来锁这一条。

6. **换条件写盘到不同文件。** 同一个靶点会被用不同条件问很多次，共用一个文件名会让
   后一次静默盖掉前一次，而登记表里那条旧条目还指着这个路径，摘要写着 10053 个分子、
   文件里却是两万个。断言按「多出一个 CSV」写，不按文件名写。

7. **`describe_molecule` 的 `retrieved_at` 走文件修改时间。** 缓存命中的条目若把
   `retrieved_at` 填成「此刻」，它和真实取回时间能差好几天——而这一栏的用途恰恰是让人
   判断数据有多旧。同一份数据的 `_mtime_label` 两处共用。

8. **分子集去重的自然结果。** `min_pchembl` 从 6 放宽到 8 之后取回 3 条记录，但 `_select`
   归并之后还是一个分子（GEFITINIB），指纹相同 → 登记表里仍是那一条。断言按
   「未重复登记」写，顺带把内容寻址这件事也验了。

### 第 7 步　chemistry 守门人 + receptor 1 个工具

产出：`domain/chemistry.py`、`domain/receptor.py`、`requirements-chem.txt`

验收：
- **AST 扫全仓**：任何模块顶层都不许 import rdkit / meeko / gemmi
- 缺依赖时调用 `prepare_receptor` → 错误里**带安装指令**
- 装上依赖后：准备受体产出目录，登记成 `prepared` 状态；
  **同一参数第二次调用命中缓存**（按 `fingerprint_parts` 判定）

**状态：已完成。** 断言写在 `tests/test_agent.py::check_chemistry_gate()`（32 条）
与 `check_receptor_tool()`（96 条）。agent 层从 803 涨到 **931**，冒烟层同步到 1428。

这一份的形态与原方案的几处出入：

1. **没有做 `requires_chem` 装饰器。** 参考实现里它是包在 `@tool` 里面的函数装饰器，
   而本项目没有装饰器那一层（工具是 `Tool` 子类、`run` 是异步方法）。
   包在 `Tool` 实例外面会包错东西；包在 `run` 外面等于改写一个抽象方法，
   那是框架层的事，不该由一个领域模块来做。所以这一层只给一个同步的
   `require_chem()`，由工具的 `run` 在开头调一次——「要化学依赖」从声明退成了一句语句，
   代价由断言补偿：`描述里必须含 CHEM_TOOL_DESCRIPTION_NOTE`、
   `缺依赖时必须有带安装指令的失败` 两条都是机械守着的。

2. **依赖清单是一个平铺的文件**（`requirements-chem.txt`），不是 `pyproject.toml` 的
   `[chem]` extra。理由：本项目是要冻结成单文件产物的应用、不是可安装的包，
   extra 那形态要多维护一处 `pyproject.toml`。断言里有一条核「文件里列的名字与
   `CHEM_DEPS` 同源」——两份清单不同步时，用户会拿到一条照着做也没用的安装指令。

3. **`prepared` 状态的守卫在「坐标文件在不在」之前。** 原本按参考实现的顺序写
   （先查文件），结果是「已经准备过的受体」报出「坐标文件不在了」——
   而它指向的是一条**产物目录**，明明在。诊断说错，用户就会去错的地方找。
   现在 `prepared` 先拦，剩下的分支里再把「指向目录」与「文件不在」分开说。

4. **`structure.parse_pdb` 对短行改成了宽容。** 这是实测撞出来的：喂一份
   `ATOM  1 CA  ALA`（列位被截断）时，`parse_pdb` 先在 `line[ATOM_CHAIN]` 上抛
   `IndexError`，而它离病因（列位不对）很远。改成跳过列位不够的行之后，
   `pdbtext.parse` 接手并给出「按标准 PDB 列位读不出来 + 该怎么走」那一条。
   另加一条：**有坐标行但一条聚合物链都没认出来**时，报列位那条而不是
   「没有聚合物链」——后者听起来像「这份文件就是这样」，而真相通常是列位错了。

5. **辨别力实测五处**（注入之后还原，全部响）：

   | 注入 | 响的断言 |
   |---|---|
   | 顶层 `import gemmi`（放进 `domain/zzprobe.py`） | 顶层不许 import 化学依赖 |
   | `_read_params(target) == params` 改成假 | 第二次直接复用 |
   | 摘掉 `require_chem()` | 错误里带着安装指令 |
   | Meeko 拒绝改成 `failed=False` | Meeko 拒绝时算失败 |
   | 摘掉 `stage == "prepared"` 那条 | prepared 的受体不能再准备 |

   第一处**没能在整轮断言里验**：往 `receptor.py` 里加顶层 `import rdkit` 会让
   测试文件自己导入就炸（`ModuleNotFoundError`），红在生产那一侧、不在断言那一侧。
   所以另用一份临时坏文件（`domain/zzprobe.py`）验的扫描本身。这个现象值得记：
   **「整轮跑不过」不等于「断言有效」**，两者要分开证。

6. **替掉的只有「参数化」那一段。** 断言里把 `receptor_module.parameterize` 换成假的
   （本机没装化学依赖，真跑一遍要一分多钟），文本编辑、链选择、交替构象、算盒子、
   产物目录、写盘、登记全部是真的——那些才是这个工具自己的逻辑。
   依赖闸那条单独验：把 `chemistry._probe_deps` 换成一个返回名字的函数。

### 第 8 步　装配与接线

产出：`domain/listing.py` 三个浏览工具、`domain/all_tools.py`、`app.py`、`prompt.py`、
`permission.py`

验收：
- `list_workspace` 列出条目并**现核对磁盘**，对不上的行尾标出来
- `switch_receptor` 两阶段：不带 `confirm` 只预演，**预演条数 == 真做条数**
- 系统提示里含「收 handle 不收路径」那段
- Plan 方式下 `prepare_receptor` **被拒**
- Plan 方式下 `fetch_structure` / `search_actives` **仍放行**（它们只读/只取）

**状态：已完成。** 五条验收逐条实测通过（见下）。断言写在
`tests/test_agent.py::check_listing_tools()`（54 条）与 `check_plan_deny()`（12 条），
另在 `check_prompt()` 补 3 条、`check_live_app()` 补 2 条。
agent 层从 931 涨到 **1001**，冒烟层同步到 1498。

几处与原方案的出入与定下来的口径：

1. **多了一个 `domain/all_tools.py`。** 原方案是让 `app.py` 直接展开各模块。
   真的写起来时发现 `app.py` 里会出现五行 `from .domain import xxx`，
   而那个文件的本职是管屏幕与状态；往后每加一个领域模块都要回去改一次装配，
   改动分散在两处，漏掉一处的结果就是「工具写了但模型看不见」。
   现在 `all_tools.py` 一处汇总，`app.py` 只多两行。
   装配顺序有意如此：**浏览工具排最前**（模型的第一件事是看清手上有哪些条目），
   这个顺序同时也决定了工具在请求体里的先后。

2. **拦的不是「会写盘」，是「会改动用户的工程」。** `PLAN_DENY` 加了 `prepare_receptor`
   一个。`fetch_structure` 同样写盘，但那是**把远端的东西拿回来**（下载缓存），
   与改动工程不是一回事；把它一并挡掉会让 Plan 方式连查资料都做不到。
   所以验收里那两条「仍放行」不是顺带，是**有意留下的口子**，各配了一条断言。

3. **`switch_receptor` 不进 `PLAN_DENY`。** 它会换后续工具的默认作用对象，但不写文件、
   不删东西，两阶段确认本身就把这一步交给了用户。断言里专门核了一条「它不在那一档里」。

4. **`render_entries` / `path_display` 放在 `listing.py` 里，不在框架层。**
   原方案说参考实现里它们是框架层的辅助。本项目里两个函数目前只有浏览工具用，
   放进 `tools.py` 会让框架层多两个业务侧才用的东西。截断口径仍然只有一份。

5. **系统提示多了一段，牵动一条旧断言的窗口。** 加进去的纪律段约 174 token，
   让 `check_loop` 里「发出去的历史降到窗口的八折以内」那条超支了 20 token。
   那条断言量的是**比例**，与窗口的绝对值无关，但要大到让系统提示之后仍有余量
   ——否则量出来的超支来自提示本身（固定开销，压缩机制缩不动它），与压缩无关。
   把窗口提成一个具名常量 `COMPACT_WINDOW = 2600` 并在两处共用，顺带把理由写在那儿。

**辨别力实测六处**（注入之后还原，全部响）：

| 注入 | 响的断言 |
|---|---|
| `list_workspace` 里摘掉 `reconcile()` | 内容被改的条目行尾标出来 |
| 预演的 `affected` 少报一项（`[:-1]`） | 预演条数 == 真做条数：5 对 6 |
| `path_display` 对目录不补尾斜杠 | 目录补一个尾斜杠 |
| `PLAN_DENY` 摘掉 `prepare_receptor` | Plan 方式下 prepare_receptor 被拒 |
| 系统提示里去掉「不要写文件路径」 | 系统提示里写着「收 handle 不收路径」那条纪律 |
| `app.py` 摘掉 `domain_tools()` 那两行 | 界面这一侧把领域工具也装上了（缺 11 个） |

第二处正是参考实现特意写下的那个教训：**预演少报是危险的那一侧**——多出来的条目
静默消失，从没被宣布过。注入把它变成 5 对 6，断言立刻报出来。

第六处补上了一个原先没人守的缺口：`check_live_app` 原本只核四个文件工具，
没有一条断言管领域工具**装没装**。而「写了却没装」在界面上完全看不出来
（工具清单少几行，谁也不会去数）。现在 `DOMAIN_TOOL_NAMES` 从 `all_domain_tools()`
现算，断言失败时把缺的那几个名字逐个报出来。**手抄一份名字是错的**——
加了工具而忘了更新它，断言会一直通过，而它要挡的恰好就是这件事。

### 第 9 步　回归与打包

```
.venv/Scripts/python.exe tests/test_agent.py        （当前 517）
.venv/Scripts/python.exe tests/test_smoke.py        （当前 1014）
.venv/Scripts/python.exe .probe/check_banned_chars.py
.probe/run_all_checks.sh 分五段（0 15 / 15 15 / 30 15 / 45 15 / 60 3）
```

界面有改动（工具卡片多一态），**重新打包后成对抓帧七帧**。

## 5. 本轮不做

- **对接（Vina / gnina）与长任务 job 层** —— 轮 2
- **打分重排 / 聚类 / 接触残基 / 配体效率** —— 轮 3
- **`write_run` 导出到 `runs/<run_id>/`** —— 轮 4（本轮只为它留字段）
- **Screening Agent profile 与领域分级权限** —— 轮 5
- AF3 预测结构（`fetch_structure` 只认实验结构，mmCIF 不收）

## 6. 待确认

1. **登记表写盘位置**：`~/.opencode_tui/ledgers/<session_id>.json`（与状态文件同处）
   还是项目内 `runs/<run_id>/workspace.json`？
   倾向前者——它是会话级状态，与 `state.toml` 同类；`runs/` 只收交付物。
2. **HTTP 出口的测试注入点**：照参考实现用「模块级 `_get_json` + monkeypatch」，
   还是抽一个独立的 `domain/http.py` 供整体替换？
   倾向后者——注入点只有一个，将来换客户端也只用改一处。
3. **`pdbtext.py` 直接移植还是重写**：参考实现 542 行，逻辑已经过测试。
   倾向**直接移植 + 补本项目口径的断言**，但要注意它的注释里可能有本项目用字规则禁用的字。
