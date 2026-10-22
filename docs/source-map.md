# 源码地图（opencode_tui）

这份文件把 `opencode_tui/` 全部源码逐层分开讲：每个文件管什么、每个类管什么、  
每个函数做什么、以及它们之间怎么连起来。

**范围**：`opencode_tui/` 下 56 个 `.py`、约 17,669 行、133 个类、约 882 个函数与方法。  
不含 `viz/`（前端库）、`web/`（网页端）、`runs/`（数据）、`.probe/`（判据）。

**取向**：只读不改。文中标出「值得注意」的地方是**观察**，不是施工单——  
凡是有意为之的写法，注释里都写了理由，这里只做转述与串联，不动一行代码。

---

## 〇、先看全局：四层结构

```
__main__.py  →  app.py  ──────────────────────────────┐   装配 + 命令中枢
                  │                                    │
     ┌────────────┼────────────┬──────────────┐         │
     ▼            ▼            ▼              ▼         │
  screens/     widgets/     core/(状态)   core/agent/   │
  屏幕          可复用部件   会话·命令·导出  智能体内核    │
     │            │            │              │         │
     └────────────┴────────────┴──────────────┘         │
                          事件总线 / Textual 消息         │
```

| 层     | 目录                     | 职责                          | 允许知道什么               |
| ----- | ---------------------- | --------------------------- | -------------------- |
| 入口    | `__main__.py`、`app.py` | 建应用、装内核、接命令                 | 知道全部，但只做「装配」与「转发」    |
| 屏幕    | `screens/`             | 一个屏幕 = 一种界面状态               | 知道部件与内核接口，不知道对方怎么实现  |
| 部件    | `widgets/`             | 可复用的界面单元                    | 只知道自己那块区域            |
| 状态与服务 | `core/`                | 会话、命令表、导出、扫描——**与界面和模型都无关** | 不知道 Textual，也不知道 LLM |
| 智能体内核 | `core/agent/`          | 模型循环、工具、登记表、权限、钩子           | 不知道界面                |

**两条贯穿全仓的分界线**：

1. **`core/` 不认识 Textual。** `session_manager.py`、`command_registry.py`、  
   `models.py` 里没有一行 `from textual`。这样它们能被断言直接调用，也才能被  
   `check_command_coverage.py` 这类脚本单独拉起。
2. **`core/agent/` 不认识界面。** `loop.py` 通过回调（`on_text`、`on_tool`…）往外  
   说话，界面那侧在 `app.py` 里接上。于是「把界面换成网页端」不需要动内核。

---

## 一、入口与装配层

### 1.1 `__main__.py`（10 行 / 1 个函数）

| 函数       | 作用                                                           |
| -------- | ------------------------------------------------------------ |
| `main()` | 唯一入口：`OpenCodeApp().run()`。没有参数解析——窗口尺寸走 `.env`，模型与凭据走应用内 `/model` 填的 `connection.toml` |

刻意保持这么薄：入口里写逻辑，就没法被断言用「建一个 App 敲两个键」的方式覆盖。

### 1.2 `env.py`（23 行 / 2 个函数）

| 函数                         | 作用                                                           |
| -------------------------- | ------------------------------------------------------------ |
| `env_file()`               | 返回项目根目录下的 `.env` 路径（`.env` 已 gitignore）                      |
| `load_env(override=False)` | 把 `.env` 读进 `os.environ`。**已存在的环境变量优先**——CI 与临时调试靠这一条覆盖文件里的值 |

### 1.3 `app.py`（619 行 / 1 个类 / 49 个方法）

`OpenCodeApp(App[None])`：**顶层组装 + 命令中枢**。它一个文件干三件事，  
是三件事里唯一一个「知道全部」的地方，也是最容易长歪的地方——所以它的方法分了四组。

#### 第一组：装配（建好内核的五个部件）

| 方法                  | 作用                              | 备注                            |
| ------------------- | ------------------------------- | ----------------------------- |
| `on_mount()`        | 进主屏、装工具、订阅事件、读状态文件              | 顺序有依赖：先装内核再进屏                 |
| `_build_agent()`    | 建 `AgentLoop`                   | 依赖 provider 与 registry        |
| `_build_registry()` | 建 `ToolRegistry`：内核工具 + 领域工具    | 领域工具一次性从 `domain.all_tools` 拿 |
| `_build_memory()`   | 建 `MemoryStore` 并 `load()` + `revalidate()` | 记忆跨会话，只建一次，见 §三.8        |
| `_build_ledger()`   | 建 `SessionLedger` 并 `load()` 快照 | 登记表按会话 id 隔离                  |
| `_build_policy()`   | 建 `PermissionPolicy` 并读权限文件     | 见 §三.3                        |
| `_build_hooks()`    | 建 `HookRunner` 并读钩子文件           | 见 §三.4                        |
| `_sync_ledger()`    | 会话切换时把登记表换成新会话那份                | 六处写盘点位之一                      |

#### 第二组：命令处理（`_command_*`）

命令表里 23 条命令，每条在这里有一个 `_command_<名字>` 方法。命令名与方法的对应  
由 `command_registry.py` 决定，**不在 app.py 里再写一份**。这类方法一律是  
「解析参数 → 调内核/切屏 → 出错就弹 MessageScreen」三段式。  
`ARGUMENT_COMMANDS`（`remember` / `forget`）是例外：它们吃命令名之后那一段参数，  
`execute_command` 按第一个空格切开，把余下那截传给处理函数。

#### 第三组：事件处理（`_on_*`）

| 方法                     | 触发      | 作用                 |
| ---------------------- | ------- | ------------------ |
| `_on_turn_done()`      | 一轮生成结束  | 刷状态栏、写 trace、必要时压缩 |
| `_on_session_picked()` | 会话选择器返回 | 切会话、重建消息区          |
| `_apply_model()`       | `/model <名字>` | 切模型、重置 provider 缓存 |
| `_on_theme_picked()`   | 主题选择器返回 | 换主题并写回 `.env`      |

#### 第四组：Textual 生命周期与工具方法

`compose()`、`on_key()`（有绑定之外的拦截）、`notify_toast()`、`push_screen()` 包装等。

> **值得注意**：`app.py` 是唯一一个「类数量 1、方法数 49」的文件。  
> 这是有意的——装配如果散到多处，加一个内核部件要改好几个文件，  
> 而漏掉一处的表现是「部件建了但没接上」。但这个规模已经贴到临界：  
> 再加一组命令（比如「会话导出成网页」）就该考虑把 `_command_*` 挪进  
> 一个 `commands/` 包，用注册表把方法名与命令名绑起来。  
> 目前不挪的原因是 49 个方法里有一半是两行的转发，单独成包反而要多跳一层。

---

## 二、状态与服务层（`core/`，不含 `agent/`）

这一层的共同特征：**不认识 Textual，也不认识 LLM**。可以被断言直接拉起。

### 2.1 `models.py`（456 行 / 6 个类 / 28 个方法）

数据模型。全仓最底层——几乎所有模块都 import 它。

| 类/常量         | 作用                                                              |
| ------------ | --------------------------------------------------------------- |
| `PartType`   | 一个 Part 是什么：`text` / `reasoning` / `tool` / `patch` / `error` 等 |
| `ToolStatus` | 工具调用状态：`running` / `done` / `failed`                            |
| `EFFORTS`    | 推理强度档位元组（`low` / `medium` / `high`）                             |
| `Part`       | **最小单位**：一段文字、一段思考、一次工具调用，各有 id                                 |
| `Message`    | 一条消息 = 一串 Part。角色（user/assistant）+ parts 列表                     |
| `TaskState`  | 本会话的任务状态：目标、用户约束、进度、阻塞；整块进上下文的 `TASK` 层，**不参与淘汰**                |
| `Session`    | 一次会话 = 一串 Message + 标题 + 创建时间 + 模型 + 摘要 + 任务状态                    |

`TaskState` 的五个字段与两份字段别名表（`TASK_FIELD_ALIASES` / `TASK_LIST_FIELDS`）都在这里。
更新语义是**两类刻意不同**：`constraints` 追加（用户提的要求不能被模型重写时漏掉），
`done` / `todo` / `blockers` 整段替换（这三类是模型自己的进度记录）。详见 §3.12。

ID 生成（`new_part_id` / `new_message_id` / `new_session_id`）用的是**模块级单调递增  
计数器**，`sync_sequences()` 在读盘之后把计数器抬到已有的最大编号之上去。

> **值得注意**：计数器的单调性靠「读盘后立刻 sync」维持。若某条路径读了盘但没 sync，  
> 新建的 id 会与已有的撞上。`SessionManager.load_from_toml` 里调了，  
> 若有新增加载路径，这条要一起带上。这是**不报错**的一类问题——撞了 id 之后  
> Part 会被覆盖，画面看着只是「少了一段」。

### 2.2 `session_manager.py`（189 行 / 1 个类 / 15 个方法）

`SessionManager`：会话的增删改查 + 存盘。

| 方法组 | 方法                                      | 作用                                        |
| --- | --------------------------------------- | ----------------------------------------- |
| 读   | `load_from_toml()`                      | 读状态文件；**末尾必须 `sync_sequences()`**         |
| 读   | `list_sessions()`                       | 按最近使用排序的会话清单                              |
| 写   | `new_session()` / `switch_session()`    | 新建、切换；两者都写盘                               |
| 写   | `rename_session()` / `delete_session()` | 改名、删除；写盘                                  |
| 写   | `undo()` / `redo()`                     | 撤销 / 重做两道记录                               |
| 写   | `save_to_toml()`                        | **原子写**：临时文件 → flush+fsync → `os.replace` |
| 内部  | `_replace_with_retry()`                 | `os.replace` 遇占用隔 5ms 重试，最多 40 次          |
| 模块级 | `default_state_path()`                  | `~/.opencode_tui/state.toml`              |

**六处写盘点位**（改内存就写盘）：新建会话、重命名、删除会话、撤销、重做、中断生成。

> **值得注意（重要）**：状态文件是**原子换名**写出的，不是 `open("wb")` 分七段写回。  
> 后者会让读的人拿到半截内容，而**被截断在表边界上的那份内容仍是合法 TOML**——  
> 解析成功说明不了完整。这条是踩过的坑，注释里记着。  
> 改动 `save_to_toml` 或 `_replace_with_retry` 之后要跑  
> `tools/check_web_api.py` 的「边写边读」那一条；网页端服务侧另加了一道  
> 「等安静 40 毫秒 + 连读两次一致」的保险（`tools/serve_web.py`）。

### 2.3 `command_registry.py`（138 行 / 2 个类 / 8 个方法）

**界面命令的单一真相。** 命令名、别名、说明、候选排序全在这里。

| 类                 | 成员                                 | 作用                                   |
| ----------------- | ---------------------------------- | ------------------------------------ |
| `Command`         | `name` / `aliases` / `description` | 一条命令（**没有 `keybind` 字段**——命令不带键位，见下） |
| `Command`         | `display()`                        | 渲染成 `/名字  说明`                        |
| `CommandRegistry` | `get(name)`                        | 按名字或别名取命令                            |
| `CommandRegistry` | `is_exact(query)`                  | **名字或别名任一对上就算打全**                    |
| `CommandRegistry` | `complete(query)`                  | `/` 补全用：按「名字前缀 > 名字里含 > 别名/说明里含」排序   |
| `CommandRegistry` | `fuzzy_match(query)`               | 命令面板用：宽松匹配                           |
| `CommandRegistry` | `all()`                            | 全部命令，按登记顺序                           |
| 模块级               | `COMMANDS`                         | 20 条命令的表                             |

> **值得注意**：`complete()` 与 `fuzzy_match()` **不能互换**。实测拿 `fuzzy_match`  
> 给补全用，打 `hel` 会列出 13 条。补全要「越打越窄」，面板要「打错也能找到」，  
> 这是两种需求。
>
> **值得注意**：`is_exact()` 必须把别名算进去。原先判据是 `exact.name == query`，  
> 于是 `/clear` 写成别名 `/new` 时弹窗不收起、回车被截走变成采纳候选，  
> 命令一次都没执行。这是**静默失败**——画面看着正常。

### 2.4 `event_bus.py`（47 行 / 1 个类 / 5 个方法）

进程内发布订阅，模拟 SSE 的形态。

| 方法                            | 作用                           |
| ----------------------------- | ---------------------------- |
| `subscribe(topic, handler)`   | 订阅某主题；主题 `"*"` 表示全部          |
| `unsubscribe(topic, handler)` | 退订                           |
| `publish(topic, payload)`     | 发布；异常被吞掉并记一条，**不让一个订阅者拖垮其余** |
| `clear()`                     | 清空；换会话时用                     |

### 2.5 `keybind_manager.py`（36 行 / 1 个类 / 3 个方法）

**快捷键的第二份清单**，与实际绑定比对用（冒烟测试逐条核）。

| 方法                | 作用                 |                                              |
| ----------------- | ------------------ | -------------------------------------------- |
| `describe()`      | 聊天界面绑定的列表（供帮助界面展示） |                                              |
| `describe_home()` | 启动界面绑定的列表          |                                              |
| 模块级               | `CONTEXTUAL_KEYS`  | `up`/`down`/`escape` 三个「只在补全弹窗开着时生效」的键；比对时剔除 |

**分两层的事实**（2026-10-06 定）：命令一律走 `/command` 或 `ctrl+p`，**命令不带键位**。  
快捷键只留三个：`ctrl+p` 命令面板、`ctrl+c` 退出、`tab` 切 Agent；中断生成用 `esc`，绑在输入框那一层。  
前导键（`ctrl+x` + 第二击）整套已删除，不要再往回加。

### 2.6 `exporter.py`（95 行 / 5 个函数）

会话导出成 Markdown。

| 函数                                                | 作用                  |
| ------------------------------------------------- | ------------------- |
| `export_filename(session)`                        | 由标题与会话 id 起一个安全的文件名 |
| `render_session_markdown(session)`                | 整篇渲染                |
| `_render_message(message)` / `_render_part(part)` | 单条消息 / 单个 Part      |

### 2.7 `project_scan.py`（177 行 / 1 个类 / 6 个函数）

工作目录扫描：把「这是个什么项目」告诉模型。

| 成员                      | 作用                          |                 |
| ----------------------- | --------------------------- | --------------- |
| `ProjectSummary`        | 扫描结果：语言、入口、依赖文件、AGENTS.md 等 |                 |
| `scan_project(root)`    | 扫描（有深度与条数上限）                |                 |
| `merge_agents_md(root)` | 把各层的 `AGENTS.md` 合成一份规则文本   |                 |
| 模块级                     | 标记表                         | 按文件名/后缀识别语言与工具链 |

### 2.8 `external_editor.py`（81 行 / 7 个函数）

外部编辑器集成（`ctrl+o` 这类走它）。

| 函数                                     | 作用                                 |
| -------------------------------------- | ---------------------------------- |
| `editor_buffer_path()`                 | 临时缓冲文件路径                           |
| `resolve_editor()`                     | 从 `$EDITOR` / `$VISUAL` 找编辑器，带平台默认 |
| `write_buffer(text)` / `read_buffer()` | 写进临时文件、改完读回来                       |
| `split_editor_command(cmd)`            | 分解命令串（**自己分，不走 shell**，避免引号问题）     |
| `build_command(parts, path)`           | 拼最终命令行                             |
| `launch(cmd, path)`                    | 启动编辑器                              |

### 2.9 `file_refs.py`（35 行 / 1 个函数）

| 成员                              | 作用                         |
| ------------------------------- | -------------------------- |
| `FILE_REF`                      | 匹配 `@路径` 的正则               |
| `resolve_file_refs(text, root)` | 把输入里的 `@路径` 展开成真实文件内容（带预算） |

---

## 三、智能体内核（`core/agent/`）

循环的骨架是：**问模型 → 模型要调工具 → 执行工具 → 把结果回灌 → 再问模型**，  
直到模型不再要工具或撞到步数上限。

### 3.1 `provider.py`（502 行 / 7 个类 / 18 个方法）

模型接口层。**所有 HTTP 都收在这个文件里**，换供应商只改这里。

| 类                                     | 作用                                                                   |
| ------------------------------------- | -------------------------------------------------------------------- |
| `ModelSpec`                           | 一个模型的静态信息：名字、上下文窗口、单价                                                |
| `ProviderConfig`                      | 连接配置；`from_values()` 由 `connection.toml` 拼装、`for_model()` 换模型、`cached_price()` 缓存单价 |
| `ProviderEventType` / `ProviderEvent` | 流式事件的类型与载荷（文本增量、工具调用、用量…）                                            |
| `ProviderError`                       | 供应商侧错误                                                               |
| `ChatProvider`                        | 协议：`stream()` 是唯一必须实现的方法                                             |
| `OpenAICompatProvider`                | 默认实现。**兼容 OpenAI 的 `/chat/completions` 流式协议**                        |

`OpenAICompatProvider` 内部：

| 方法                      | 作用                                                                   |
| ----------------------- | -------------------------------------------------------------------- |
| `stream()`              | 发起请求、逐行读 SSE                                                         |
| `_parse_line(line)`     | 一行 `data: {...}`                                                     |
| `_parse_chunk(payload)` | 一个 chunk → 若干 `ProviderEvent`                                        |
| `_collect_calls(...)`   | **把跨 chunk 的工具调用拼起来**（工具名与参数是分批到达的）                                  |
| `_flush_calls()`        | chunk 结束时把攒下的调用发出                                                    |
| `_read_usage(...)`      | 读 token 用量                                                           |
| 模块级                     | `BUILTIN_MODELS` / `lookup_model` / `model_names` / `guess_provider` |

> **值得注意**：`_collect_calls` 与 `_flush_calls` 是流式解析里最容易写错的地方。  
> 工具调用的 `arguments` 是**字符串分片**到达的，按到达顺序拼接才对；  
> 早发一个「参数已完整」的信号，下游就会拿到半截 JSON。  
> 模态帧里能读到 `deepseek-chat` / `deepseek-flash` / `deepseek-reasoner`，  
> 说明 `BUILTIN_MODELS` 真的进了冻结产物——这是抓帧时确认「新代码进包了」的读数。

### 3.2 `prompt.py`（466 行 / 18 个函数）

**稳定前缀**、翻译层、token 估算与**选择性压缩**。装配（分层与预算）已搬到 `context.py`。

| 成员                                                                | 作用                                         |
| ----------------------------------------------------------------- | ------------------------------------------ |
| `estimate_tokens(text)`                                           | 估 token（**不调 tokenizer**：估得准比算得准重要，因为算要联网） |
| `item_tokens(item)`                                               | 估一条模型消息占多少 token，只数要发出去的那些字段               |
| `is_key_line(line)`                                               | 这一行是不是关键行：报错原文按「行里含」判、结论行按「行首是」判            |
| `compress_tool_output(text, deep)`                                | **按内容挑着留**：保关键行与首尾，丢掉成片过程输出；返回 None 表示压不动   |
| `truncate_for_model(text)`                                        | 给模型的那一份：先按内容挑，挑完仍超长才按位置兜底                  |
| `rules_directory(root)`                                           | 规则文件在哪个目录：源码运行取包的上级目录、打包后取 exe 所在目录，**都不看启动目录**；`VSA_RULES_DIR` 可顶掉 |
| `project_rules(root)`                                             | 读该目录下的 `AGENTS.md`，超 32KB 时按行截断           |
| `stable_prefix(workspace, session)`                               | 稳定前缀（`PREFIX` 层）：身份 / 纪律 / 规则，**不含时间与进度**   |
| `build_system_prompt()`                                           | 稳定前缀 + 工作目录的当前状态（保留的兼容入口）                  |
| `summary_messages()`                                              | 压缩时用的「请总结以下对话」请求                           |
| `translate_message` / `_translate_user` / `_translate_assistant` | **翻译层**：内部 Part 结构 → 模型消息                  |

> **值得注意（易错点）**：内部模型与模型 API 的模型**不是一套**。内部一条消息有  
> 多个 Part（文字、思考、工具调用、工具结果），而 API 里「工具调用」是 assistant 消息  
> 上的一个字段、「工具结果」是独立的 role。翻译层就是干这个的。  
> 改 `Part` 结构时**必须同步改翻译层**——这里出错不会崩，只是模型看到的历史变形。

> `stable_prefix` 与 `build_system_prompt` 的差别就是「前缀稳定」这条不变式：前者只随
> `AGENTS.md` 变化，后者多带一段含当前时间的 `state_text`。装配器只用前者。

> **规则的位置不跟工作目录走**：`stable_prefix` 取 `rules_directory(workspace.root)`，
> 而不是 `workspace.root` 本身。`rules_directory` 与 `env.env_file()` /
> `permission.project_config_directory` 是同一套判断，只有源码运行那一支不同——
> 那两个跟着启动目录走（用户在哪就认哪），规则这一份跟着代码走，因为规则是写给
> 这个项目本身的。所以从任何目录启动，读到的都是开发中那份 `AGENTS.md`。
> 打包后读 exe 旁边那份，**要记得把它放在 exe 旁边**（与 `.env`、`.opencode_tui/` 同一批）。

> **压缩原语为什么在这一层**：`context.py` 从 `prompt.py` 导入，反过来不行，所以
> 两处都要用的东西只能放这里。而这两处**必须**共用一套——翻译层给模型的那一份
> （`truncate_for_model`）如果仍按字符位置截，报错正好出现在被削掉的那一段里，
> 装配器连见都没见到它，后面再怎么按内容挑也救不回来。  
> 压缩过的正文带 `OMISSION_MARK` / `DEEP_MARK`，与 `_cut_middle` 的省略说明
> **刻意用词不同**：同词的话翻译层裁过的输出会被装配器当成「已经压过」，再也压不动。

### 3.3 `permission.py`（工具准入判定 + 工具开关）

工具调用的准入判定。

| 类                    | 作用                            |
| -------------------- | ----------------------------- |
| `Verdict`            | 判定档位：`ALLOW` / `ASK` / `DENY` |
| `PermissionRule`     | 一条规则：匹配什么工具、什么参数 → 什么判定       |
| `PermissionDecision` | 判定结果 + 命中的规则 + 给用户看的话         |
| `PermissionPolicy`   | 策略主体                          |

`PermissionPolicy` 方法：

| 方法                                   | 作用                                                                 |                      |
| ------------------------------------ | ------------------------------------------------------------------ | -------------------- |
| `check(tool, arguments)`             | 查规则给出判定；**取最严的那条**（层级：DENY > ASK > ALLOW）                          |                      |
| `remember(tool, arguments, verdict)` | 记住这次选择（「本次会话都允许」）                                                  |                      |
| `clear_session()`                    | 清掉本会话记住的选择                                                         |                      |
| 模块级                                  | `argument_value` / `load_permissions` / `default_permission_paths` / `project_config_directory` / **`load_disabled`** / `load_tool_switches` / `save_disabled` / `tools_config_path` |                      |
| 常量                                   | `DEFAULT_ASK` / `PLAN_DENY` / **`TOOLS_FILE`**                     | 默认要问；Plan 模式下默认拒绝写操作 |

> **值得注意**：多档判定要取**最严**。若按「第一条命中」来判，规则表的顺序就定义了  
> 安全语义——那是没人会去维护的一处隐式依赖。层级化之后，加一条 DENY 无论如何都生效。

**两件事别混**（这是本模块最容易搞错的地方）：

| | `permissions.toml`（`load_permissions`） | `tools.toml`（`load_disabled`） |
| --- | --- | --- |
| 管什么 | 调用时放不放行 | **装配时装不装** |
| 工具在不在 `tools` 数组里 | **在**，模型看得见 | **不在**，模型不知道有它 |
| 模型被拒后 | 会一直重试，上下文里攒一串失败 | 不会去想它 |
| 配置写错 | **抛 `ValueError`**（静默失效更难查） | 按「没关」处理（不让应用起不来） |

要「彻底不让模型用某个工具」用 `tools.toml`，不要用 `deny`。

**配置放哪儿**：项目侧那份跟着**可执行文件**走——打包后读 `exe 同目录/.opencode_tui/tools.toml`，
源码运行时读 `<启动目录>/.opencode_tui/tools.toml`（`project_config_directory()`，与 `env.env_file()` 同一套判断）。
用户侧始终是 `~/.opencode_tui/tools.toml`，两份都读、任一处说关就关。
这样 exe 挪到哪、从哪个目录启动，开关都读得到。

**改开关不必手写文件**：`/tools` 面板（`screens/tools_panel.py`）列出全部工具逐条开关，
写的就是项目侧那份 `tools.toml`（`save_disabled`，原子替换）。面板改完立刻重装注册表
（`app.toggle_tool`），不必重启——工具在不在请求体的 `tools` 数组里，下一次发问就生效。
只写 `false` 条目：写 `true` 会在用户侧那份也写了 `false` 时给出「本文件能打开它」的错觉。
用户侧关掉的工具在面板上标「由用户配置关闭」，面板改不动它。


### 3.4 `hooks.py`（207 行 / 4 个类 / 9 个函数）

外部命令钩子。`PreToolUse` / `PostToolUse` / `Stop` 三类事件。

| 类             | 作用            |
| ------------- | ------------- |
| `Hook`        | 一条钩子：事件、命令、超时 |
| `HookOutcome` | 单条钩子的结果       |
| `HookReport`  | 一批结果的汇总       |
| `HookRunner`  | 执行器           |

| 方法                       | 作用                                   |                        |
| ------------------------ | ------------------------------------ | ---------------------- |
| `for_event(event)`       | 取某事件下要跑的钩子                           |                        |
| `run(event, payload)`    | 跑全部，汇总                               |                        |
| `_invoke(hook, payload)` | 拉起子进程、喂 stdin、收退出码                   |                        |
| 常量                       | **退出码 2 = 拦截**                       | 约定成这样：非零退出报错，2 是「明确拒绝」 |
| 模块级                      | `load_hooks` / `default_hooks_paths` |                        |

### 3.5 `loop.py`（874 行 / 2 个类 / 34 个方法）

**整个项目的心脏。** `AgentLoop` 持有 provider、registry、ledger、policy、hooks，  
对外暴露几个 async 方法。

| 方法                           | 作用                       |
| ---------------------------- | ------------------------ |
| `stream_reply(prompt)`       | 主入口：跑完一整轮，通过回调把事件推给界面    |
| `run_bash(command)`          | `!命令` 直通（不经模型）           |
| `cancel()`                   | 中断当前生成（`esc`）；**并写盘** |
| `resolve_permission(answer)` | 把权限弹窗的答案交回挂起的那个 `await`  |
| `switch_model(spec)`         | 换模型                      |
| `compact_session()`          | 手动触发一次上下文压缩              |

内部：

| 方法                          | 作用                                    |
| --------------------------- | ------------------------------------- |
| `_run_steps()`              | 循环本体：步数上限 `MAX_STEPS=24`              |
| `_ask_model(messages)`      | 一次请求，**失败重试 `MAX_RETRIES=2`**         |
| `_consume_stream(...)`      | 消费流式事件，边收边回调                          |
| `_execute_tool(call)`       | 查 registry → 过 policy → 跑钩子 → 执行 → 回灌；跑完把含关键行的输出提炼进证据层 |
| `_ask_permission(decision)` | 弹权限对话框并 `await` 答案                    |
| `_summarize(...)`           | 上下文超预算时压缩                             |
| `_trace_context(...)`       | 真削过时发一条 `context.assembled`，把移出组数与降级级别记进日志 |
| `_run_hooks(...)`           | 钩子统一入口                                |
| `_cost(...)`                | 累计花费                                  |

| 辅助类                  | 作用                      |
| -------------------- | ----------------------- |
| `_PendingPermission` | 挂起中的权限询问（`await` 的等答案处） |

> **值得注意（三处）**：
>
> 1. **`MAX_STEPS=24` 是硬上限。** 到顶时循环必须给模型一句可解释的话结束，  
>    不能静默停。否则模型「以为还在跑」而用户以为早就结束了。
> 2. **权限询问是 `await` 挂起的**，靠 `_PendingPermission` 把答案送回来。  
>    期间 `cancel()` 必须能把它唤醒（否则按 `esc` 中断会卡住）——这是取消路径上最易漏的一处。
> 3. **`_execute_tool` 的四个动作有固定顺序**：查表 → 策略 → 钩子 → 执行。  
>    顺序换了语义就变（比如钩子在策略之前就绕过权限，策略在执行之后就没有意义）。
>
> **`self.evidence`（`EvidenceStore`）是「一轮」的**：`stream_reply` 与 `run_bash` 开头各清空一次。
> 它是运行时状态，与 `cancelled` 同类，因此不进 `__init__` 的参数表——装配处不必知道它。
> 提炼排在 POST_TOOL_USE 钩子**之后**：钩子可能把这一条改成失败，那时它更该进证据层。

### 3.6 `tools.py`（869 行 / 14 个类 / 40 个方法）

工具框架 + 五个内核工具。

框架：

| 类                               | 作用                                                    |
| ------------------------------- | ----------------------------------------------------- |
| `ToolSpec`                      | 工具的静态描述：名字、参数 JSON Schema、描述文本                        |
| `ToolResult`                    | 执行结果：`output` / `failed` / `meta`                     |
| `ToolError`                     | 确定性失败：参数错、依赖缺——**重试无用**                               |
| `RetryableToolError`            | 瞬时失败：网络抖动——**交给重试策略**                                 |
| `Ctx`                           | 内核注入参数的标记类（不参与参数模式）                                  |
| `BatchFailure` / `BatchOutcome` | 批处理的失败项与汇总                                            |
| `Tool(ABC)`                     | 基类：`spec` + `async run(arguments)` + `execute(arguments, context, task_state, evidence)` |
| `ToolRegistry`                  | 注册表：`register()` / `get()` / `specs()` / `dispatch()` |

`Tool` 上四个「哪个参数由内核注入」的声明：

| 字段                  | 作用                                    |
| ------------------- | ------------------------------------- |
| `primary_argument`  | 哪个参数是「主参数」——权限规则与进度显示靠它               |
| `context_argument`  | 哪个参数收**会话登记表**（`ledger`）              |
| `task_argument`     | 哪个参数收**任务状态**（`state`，只有 `TaskTool` 用） |
| `evidence_argument` | 哪个参数收**证据收集器**（只有 `RecallTool` 用）     |

> **值得注意**：三个注入通道各塞各的，`execute` 里按顺序走一遍。
> 声明了某一类却收到 `None` 时报的是「内核装配问题」而**不是**参数错——这一路只可能是
> 内核漏传，模型改自己的参数救不回来，措辞必须让模型知道「重试无用」。
>
> `evidence_argument` 与另两个分开，是因为它收的东西**不改任何状态**：工具只是把结果里
> 值得留的那几行放进一个小盒子，由装配器放进 `EVIDENCE` 层。它只调收集器的 `add`，
> 不认它的类型——所以工具层不必反过来依赖上下文模块（`context.py` 从 `models.py` 导入，
> 反向会成环）。

参数校验（`argument_error(...)`）：按 `ToolSpec.parameters` 的 JSON Schema 校验，  
**报错要能回灌给模型自纠**，所以措辞说「收到什么、应该是什么」。

五个内核工具：

| 类           | 工具名     | 作用                   |
| ----------- | ------- | -------------------- |
| `BashTool`  | `bash`  | 跑 shell 命令           |
| `ReadTool`  | `read`  | 读文件（带行号与预算）          |
| `WriteTool` | `write` | 写文件                  |
| `PatchTool` | `patch` | 按片段替换（**不走行号**，行号会漂） |
| `GrepTool`  | `grep`  | 内容检索                 |

进程管理（模块级）：

| 函数                    | 作用                                   |
| --------------------- | ------------------------------------ |
| `terminate_tree(pid)` | **连子进程一起收**（Windows 上只杀父进程会留下孤儿占着管道） |
| `release_pipes(proc)` | 显式关管道，否则读端会一直等                       |

> **值得注意**：`terminate_tree` 是 Windows 上必须显式处理的一处。  
> 这个仓库的判据脚本吃过孤儿子进程的亏（`check_command_coverage.py` 的孤儿写了  
> `.probe/state_test.toml` 并占 `.probe/logs/*.jsonl`，让两个正常测试假失败）。  
> 排查用 `Get-CimInstance Win32_Process`（`wmic` 被安全策略拦）。

### 3.7 `ledger.py`（880 行 / 7 个类 / 48 个方法）

**领域资源登记表。** 全仓最大的一个文件，也是整套「收 handle 不收路径」纪律的集中体现。

#### 句柄（handle）

| 类                 | 形式            | 管什么                          |
| ----------------- | ------------- | ---------------------------- |
| `Handle`          | 基类            | `kind` + 序号，渲染成 `receptor#1` |
| `ReceptorHandle`  | `receptor#N`  | 受体（原始结构或准备后的产物目录）            |
| `LigandSetHandle` | `ligandset#N` | 配体集                          |
| `ArtifactHandle`  | `artifact#N`  | 其余产物（靶点信息、报告…）               |
| `Kind`            | —             | 三个 kind 的名字常量                |

| 函数                                   | 作用                            |
| ------------------------------------ | ----------------------------- |
| `make_handle(kind, seq)`             | 造句柄                           |
| `parse_handle(text)`                 | 解析 `receptor#3`；**格式不对抛错**    |
| `coerce_handle(value)`               | 宽容地转：字符串 / 字典 / 句柄都能收         |
| `expect_handle(entry, cls, argname)` | 类型核对，**报错文案是中文且说清「要什么、收到什么」** |

#### 条目与指纹

| 成员                                       | 作用                            |
| ---------------------------------------- | ----------------------------- |
| `Entry`                                  | 一条登记：句柄、路径、摘要、指纹、父项、meta、文件状态 |
| `Entry.brief()`                          | 单行摘要（列表工具用）                   |
| `entry_from_dict(...)`                   | 从快照反序列化                       |
| `fingerprint_bytes/text/file/path/parts` | 五种指纹口径                        |
| `_FILE_STATE_NOTES`                      | 文件状态码 → 中文说明                  |

**指纹口径要配对**：`fingerprint_text` 用于**下载来的文本**（本来就是文本，  
写盘走同一段编码，必然一致）；`fingerprint_bytes` 用于**导入的原始字节**  
（逐字节保真，用文本算会在遇到非 UTF-8 字节时把两份不同内容算成同一个指纹）。  
`fingerprint_path` 用于**目录**（把目录里的文件清单与各自内容一起算）。

#### `SessionLedger`

| 方法组 | 方法                                               | 作用                            |
| --- | ------------------------------------------------ | ----------------------------- |
| 增删  | `add(kind, ...)`                                 | 登记一条；**默认挂当前受体为父项**           |
| 增删  | `remove(handle)` / `reset()`                     | 删一条 / 清空                      |
| 查   | `get(handle)`                                    | 取一条；取不到抛错                     |
| 查   | `find(...)`                                      | 筛选：kind / fingerprint / 是否含搁置 |
| 查   | `entries()` / `lineage(handle)`                  | 全量 / 完整血缘链                    |
| 受体  | `current_receptor()` / `switch_receptor(handle)` | 当前受体 / 切换                     |
| 受体  | `stale_after(handle)`                            | **切换后会失效的条目**（按血缘算，不看名字）      |
| 搁置  | `is_archived(handle)` / `discard_archived()`     | 是否搁置 / 清掉搁置                   |
| 对账  | `reconcile()`                                    | **拿磁盘现状核对每条登记**；只读，不改存档       |
| 存档  | `save()` / `load()` / `snapshot()`               | 原子写 / 读 / 取快照                 |

模块级：`ledger_root()` / `default_snapshot_path()` / `looks_like_ledger()` / `_replace_with_retry()`

> **值得注意（三处）**：
>
> 1. **`stale_after` 必须按血缘算。** 曾经只看直接父项，隔层产物（父项是 ligandset、  
>    再往上才是受体）会被搁置却从不出现在预演里——预演说 3 项、真做说 4 项，  
>    多出来那个**从没被宣布过**。两阶段确认的全部价值就在「预演数字 == 真做数字」。
> 2. **`reconcile()` 是只读的。** 它的产出是「条目的 `file_state` 字段被更新」，  
>    但**不删条目、不改路径**。于是「文件没了」这件事在列表里可见（行尾标注），  
>    而存档仍保留原始信息。这是有意的：登记的是「我当时产出了什么」，  
>    磁盘变化是另一件事，不该顺手抹掉历史。
> 3. **`fingerprint_*` 五个口径不能混用。** 混了的后果是「同一份东西算出两个指纹」  
>    或「两份东西算成一个」——前者重复登记，后者**静默用错东西**。

### 3.8 `memory.py`（576 行 / 6 个类 / 29 个方法 / 4 个函数）

**项目级记忆。** 与登记表分工不同：登记表记「这次会话手上有哪些产物」，记忆记  
「这个项目已知什么」。前者是会话内的（handle 换个会话就指向别处），后者跨会话——  
这是它全部的意义所在。

本期只有一种记忆：`MemoryKind.FACT`。打分与具体筛选流程尚未实现，依赖它们的  
PREFERENCE / RUN / RECIPE 等枚举值等到那一部分做完再扩。

| 成员             | 作用                                                          |
| -------------- | ----------------------------------------------------------- |
| `MemoryKind`   | 记忆类型；本期只有 `FACT`                                            |
| `MemoryEntry`  | 一条记忆：正文、引用路径、内容指纹、来源、时间、状态                                  |
| `MemoryStore`  | 读写与检索；`remember` / `forget` / `recall` / `revalidate` / `resident_text` |
| `RememberTool` | `remember` 工具，把一条事实写进项目记忆                                    |
| `RecallTool`   | `recall` 工具，按关键词检索；命中同时放进证据层（`evidence_argument`）           |
| `MemoryError`  | 记忆自身的错误，调用方只需要处理这一种                                         |
| 模块级            | `memory_root()` / `new_memory_id()` / `_reject_handle()` / `_replace_with_retry()` |

两份文件：`entries.json`（程序读的那一份，是真相）与 `memory.md`（给人看、给人改的  
那一份）。两份都用**原子换名**写出（临时文件 → flush + fsync → `os.replace`，  
遇占用隔 5 毫秒重试最多 40 次）。

> **值得注意**：
> 1. **引用只能是路径，不能是 handle。** 记忆跨会话，而 `receptor#1` 这种编号换个  
>    会话就指向别的东西。写错了不会报错，只会在下一个会话里指向一份不相干的东西。  
>    这条**在写入时就拦**：`remember` 借 `ledger.parse_handle` 判定，命中就抛  
>    `MemoryError`，工具层再转成一句可读失败回灌给模型。
> 2. **对账三态：`ok` / `stale` / `missing`**，与登记表用同一套说法。`stale`（文件变了）  
>    **仍算可用**——变化本身是个信号，注入时带一行提醒交给模型判断；只有 `missing`  
>    （引用文件不在了）才不可用。
> 3. **常驻注入按 token 上限裁剪**，且排序把「有引用的」放前面：引用指向项目里真实  
>    存在的产物，比一句没有凭据的陈述更值得占用常驻额度。额度不够时宁可整条不装，  
>    也不装半条。
> 4. **检索结果同时进证据层。** 记忆是跨会话的知识，`RECENT` 里本来就没有它——  
>    不给证据层一份，下一步的 `RECENT` 一被压缩，这段知识就跟着没了。  
>    它**仍然不声明 `context_argument`**（纯查询工具不挂登记表，那条约定没变），  
>    用的是第 4 期新开的 `evidence_argument` 通道。

### 3.9 `trace.py`（94 行 / 1 个类 / 8 个方法）

JSONL 事件日志。

| 成员            | 作用                                          |        |
| ------------- | ------------------------------------------- | ------ |
| `TraceWriter` | 一行一个 JSON 事件；`write()` / `close()` / `path` |        |
| 模块级           | `log_directory_for(session_id)`             | 按会话分目录 |

### 3.10 `workspace.py`（38 行 / 1 个类 / 4 个方法）

| 成员          | 作用                                           |      |
| ----------- | -------------------------------------------- | ---- |
| `Workspace` | 工作目录；`ensure()` 建目录、`state_text()` 给模型的工作区描述 |      |
| 模块级         | `workspace_root()`                           | 工作区根 |

### 3.11 `context.py`（729 行 / 8 个类 / 31 个函数）

结构化上下文：把「会话历史的裁剪器」换成「面向当前一步的装配器」。设计见
`docs/context-design.md`，实施计划见 `docs/context-plan.md`。

| 成员                       | 作用                                                          |
| ------------------------ | ----------------------------------------------------------- |
| `Layer`                  | 上下文的一层：`PREFIX` / `MEMORY` / `TASK` / `RECENT` / `EVIDENCE` / `HISTORY` |
| `LAYER_ORDER`            | 装配顺序，即枚举顺序                                                  |
| `LAYER_POLICY`           | 每层的淘汰策略：`pinned` / `capped` / `atomic` / `soft` / `ref`       |
| `Turn`                   | **交互组**：一次 assistant step 及它触发的工具结果，是 `RECENT` 的原子单位         |
| `Evidence`               | 一条按需证据：`ref` + `summary` + 按预算展开的 `detail`                  |
| `EvidenceStore`          | 本轮的证据收集器：条数滚动、同引用替换；`add_from_part()` 把含关键行的工具结果提炼成一条 |
| `evidence_ref(...)` / `one_line(...)` | 引用名 = 工具名 + 定位参数；摘要与引用名都压成一行           |
| `Budget`                 | 一步的预算口径：可用额度、预留、工具定义、各层软配额（`share()`）                       |
| `ContextBundle`          | 装配结果：消息、每层用量、被移出的组、**有序的**降级级别                             |
| `DEGRADE_ORDER` / `ordered_degraded` | 降级链的固定顺序；`degraded` 按它排，顺序就是「先削谁、后削谁」          |
| `ContextAssembler`       | 装配主体：`budget()` 算额度、`build()` 按层装配并降级、`_degrade_recent()` 按 Part 类型压 |
| `ContextAssemblyError`   | 削到底仍装不下时抛出，消息里带各层用量（`describe_usage`）；**不静默截断**         |
| `split_turns(messages)`  | 按「assistant 项开头、其后的 tool 项归它」切交互组，工具结果由构造保证同组进出             |

> **值得注意（易错点）**：`split_turns` 不改翻译层，而是在装配侧重新分组——这样
> `summary_messages` 那条路不用跟着动，交互组原子也不靠事后检查，而是由构造保证。  
> `MEMORY` 层**不是 dataclass**：内容是 `MemoryStore.resident_text()` 渲染的纯文本，
> 上限与淘汰归 `MemoryStore`，装配器只把它当一段受上限约束的文本。  
> `TASK` 层同理**不是 dataclass**：装配器收的是 `loop._task_text(session)` 渲染出的
> 一段文本（空状态给空串），它不认 `TaskState` 这个类型——`context.py` 从 `models.py`
> 导入 `Session`，反过来认就会成环。  
> 降级链的顺序是固定的：收证据正文 → 缩摘要 → 缩常驻记忆 → 移出最旧交互组 → 压组内输出
> （丢推理 → 压工具输出 → 再压一档）。

> **被软配额削过的层同样记进 `degraded`**：削了就是削了，不能因为「不是降级链那一步削的」
> 就不算，否则日志里看不出真实用量。`degraded` 用 `ordered_degraded` 排序而不是
> `sorted(set(...))`——集合排序会把顺序抹掉，而顺序正是这一串要表达的东西。

> **压缩只认两处字段**：`reasoning_content`（中间推理）与 `role=tool` 的 `content`
> （工具输出正文）。助手正文是决策与结论、`tool_calls` 是调用记录，都不在可压的两类里，
> 所以一条都不会被动到——这不是靠自觉，是 `_degrade_recent` 只扫这两处。

> **证据层不是 `RECENT` 的副本**：收集器只收两类——检索回来的记忆，与**含关键行的**
> 工具结果（判定复用第 3 期的 `is_key_line`）。每条工具输出都收一份的话，证据层就成了
> `RECENT` 的复制品，白占额度还挤掉真正要留的东西。失败的工具结果保底收：失败原因
> 就是下一步的输入，而它常常是短的一句，关键词表盖不住这种。
>
> **提炼放在循环层而不是收集器里被谁定时调用**：`EvidenceStore.add_from_part` 只看
> 一条 `Part` 的最终状态，所以它必须排在 POST_TOOL_USE 钩子之后——钩子可能把这一条
> 改成失败，那时它更该进证据层。

### 3.12 `task.py`（116 行 / 1 个类 / 1 个方法）

本会话的任务状态**更新入口**（模型那条路）。数据在 `core/models.py`（`Session` 持有它），
这里只有工具——数据在数据层，工具在工具层。用户那条路是 `/task` 命令，在 `app.py`。

| 成员                | 作用                                                       |
| ----------------- | -------------------------------------------------------- |
| `TaskTool`        | 工具名 `task`；`task_argument = "state"`，内核按它把 `session.task_state` 塞进来 |
| 参数 `goal`         | 覆盖目标                                                     |
| 参数 `constraints`  | **追加**约束（去重）；已有约束不会被这次调用删掉                             |
| 参数 `done` / `todo` / `blockers` | **整段替换**；传空数组即清空这一类                        |

> **参数名要和状态字段一致**：这个参数原来叫 `add_constraints`，而说明文字、渲染出来的
> 标题、`/task` 的别名表用的都是 `constraints`——模型照着说明写 `constraints`，
> 被 `additionalProperties: False` 挡下，白烧一步（4 个会话里各撞过一次）。
> 现以 `constraints` 为准，`add_constraints` 作为旧写法继续收（老会话的历史里
> 还有那些调用，模型会学）。**加新工具时，参数名要照模型看得到的那个名字取。**

> **值得注意**：这是全仓唯一声明 `task_argument` 的工具。它与 `context_argument`
> 是两个通道，因为内核从不同地方取：`context` 是循环层持有的登记表、会话切换时整体换掉；
> 任务状态挂在会话自己身上、每次调用现取（`loop._execute_tool` 传 `session.task_state`）。
> 挤进同一个口子的话，取登记表的那些工具会收到一个既不是登记表也不是任务状态的东西。


---

## 四、领域工具层（`core/agent/domain/`）

内核那批工具（bash / read / write / patch / grep）不认识任何业务概念，这组认识。  
**分界线按「它需要知道什么」划**：需要知道「UniProt 编号长什么样」「口袋残基怎么算」  
的在这边；只需要知道「有个文件、有段文字」的在内核那边。

### 4.0 八条纪律（`__init__.py` 的文档字符串）

这八条写在包文档里，是这一层所有工具的准绳。逐条转述：

| # | 纪律                            | 为什么                                                        |
| - | ----------------------------- | ---------------------------------------------------------- |
| 1 | **收 handle，不收路径**             | 模型编不出 `ligandset#3`，却编得出 `D:/data/lib.csv`——后者可能不存在可能是上次那个 |
| 2 | **纯查询不声明 `context_argument`** | 查完不留东西的工具挂了登记表，会让模型以为它留下了什么                                |
| 3 | **失败返回结果，不抛异常**               | `ToolResult(failed=True, output=原因)`，错误文本要能回灌给模型自纠         |
| 4 | **只重试瞬时故障**                   | 用 `RetryableToolError` 标网络抖动；参数错重试多少次都一样                   |
| 5 | **返回值要么有预算，要么就别返回**           | 截断了必须说「还有多少条、怎么取到」                                         |
| 6 | **描述的第一段就是工具清单里那一行**          | 关键语义（打分方向、成本量级）必须在第一段                                      |
| 7 | **要算数就用代码算**                  | RMSD、Tanimoto、配体效率绝不交给模型心算                                 |
| 8 | **可选依赖不许在顶层 import**          | RDKit / Meeko 在函数体里延迟导入                                    |

> 第 8 条有**机检**：`tests/test_agent.py::check_chemistry_gate` 用 AST 扫全仓。  
> 这条不是风格喜好——顶层 import 炸掉的是整个程序起不来，而它本该只影响一个工具。

### 4.1 `http.py`（213 行 / 1 个类 / 8 个函数）

**全仓唯一的网络出口。** 抽出来是为了测试能整体替换（散在各工具里的话，  
替换点从一处变七八处，且新工具作者得记住「要留那个函数」）。

| 成员                           | 作用                          |
| ---------------------------- | --------------------------- |
| `NotFound(ToolError)`        | HTTP 404，**单独一档**           |
| `get_json(url, params, ...)` | GET + 解 JSON。**测试替换的就是它**   |
| `get_text(...)`              | GET + 取文本（坐标文件这类）           |
| `get_text_or_none(...)`      | **404 返回 None 而不是抛错**       |
| `probe(url)`                 | 只问「有没有东西」，返回状态码，**不按状态码抛错** |
| `_request(...)`              | 发请求 + 分档处理失败                |
| `_clean_params(...)`         | 去掉值为 None 的参数               |
| `_message(response)`         | 从错误响应里抠出能照着改的那句话            |
| `join_query(path, params)`   | 拼 URL（测试核对请求地址用）            |

**错误分两档**：

| 情况              | 抛什么                  | 理由           |
| --------------- | -------------------- | ------------ |
| 4xx（编号不存在、语法错）  | `ToolError`          | 重试一百次还是同一个结果 |
| 429 / 5xx / 连不上 | `RetryableToolError` | 交给内核重试策略     |

> **值得注意（三处）**：
>
> 1. **httpx 的网络异常不是 `OSError`。** 内核默认那条重试策略只认 `OSError`，  
>    所以这里必须**显式转换**。不转的话，断网与超时反而成了一次性的——  
>    而它们恰恰最该重试。
> 2. **`server_5xx_as_error` 这个参数。** 填了它，5xx 就不再算「可重试」。  
>    ChEMBL 的相似性检索需要它：那个端点把「这段结构解析不了」也回成 HTTP 500，  
>    与真的服务器故障同码。照默认口径报出去，模型会看到「稍后重试即可」，  
>    然后对一个写错的结构照做一遍。
> 3. **`_message` 先挡 HTML。** ChEMBL 的 Django 路由在几种情况下回错误页而不是 API  
>    消息（相似度阈值写成 `0.85`、分子编号写成 `gefitinib`，都是 404 加一页 HTML）。  
>    把 HTML 截一段塞进错误里，模型读不出病因——不如直说「这是网页，不是接口消息」。

### 4.2 `uniprot.py`（819 行 / 2 个类 / 33 个函数）

靶点检索与取详情。2 个工具。

| 工具                  | 参数                                               | 作用                        |
| ------------------- | ------------------------------------------------ | ------------------------- |
| `SearchTargetsTool` | `query` / `organism` / `reviewed_only` / `limit` | 按名称或基因检索靶点，返回候选编号列表       |
| `FetchTargetTool`   | `accession` / `refresh`                          | 取编号的完整信息并**登记成 artifact** |

关键内部逻辑：

| 函数                                              | 作用                         |
| ----------------------------------------------- | -------------------------- |
| `normalize_accession(raw)`                      | 编号规范化；**格式不对抛 ValueError** |
| `_term_clause(text)`                            | 判断「这是基因名还是自由词」，返回检索式与说法    |
| `_search_query(query, organism, reviewed_only)` | 拼 UniProt 检索语法             |
| `_fetch_record(...)`                            | 请求并缓存                      |
| `_pdb_structures(record)`                       | 抽可用实验结构（**编号、分辨率、覆盖范围**）   |
| `_alphafold_id(record)`                         | 取 AlphaFold 模型编号           |
| `_render_search` / `_render_entry`              | 渲染给模型看                     |
| `_entry_summary` / `_entry_meta`                | 登记表摘要与 meta                |

`SearchTargetsTool` 的**两级回退**（值得单独说）：  
先按「基因名精确匹配 + 只取人工审阅（Swiss-Prot）」搜；没命中就退到  
「原文全文检索 + 放宽到未审阅（TrEMBL）」，**并且在输出里说明退过**。  
理由是「没这个东西」对模型是个走不通的答案，而「可能是别名、可信度低一档」给了它下一步。

`FetchTargetTool` 的去重：按指纹在登记表里查（`include_archived=True`），  
已有就报「已有同一份：artifact#N（未重复登记）」。`include_archived` 是必要的——  
靶点信息登记时会挂当前受体为父项，切换受体后会被搁置，那时若看不见它，  
同一份数据会被重复登记一份。

### 4.3 `structure.py`（1083 行 / 4 个类 / 29 个函数）

取结构与导结构。2 个工具。

| 工具                    | 参数                   | 作用                                     |
| --------------------- | -------------------- | -------------------------------------- |
| `FetchStructureTool`  | `pdb_id` / `refresh` | 按 PDB 编号取坐标文件，登记成 **receptor**（状态 raw） |
| `ImportStructureTool` | `path`               | 导入本机坐标文件，登记成 receptor。**全库唯一收路径的动作**   |

| 类                | 作用                         |
| ---------------- | -------------------------- |
| `ChainFacts`     | 一条链的事实：声明/观测残基范围、缺口数、是否聚合物 |
| `StructureFacts` | 一套结构的事实：方法、分辨率、链、配体、水、交替构象 |

| 函数                                           | 作用                     |
| -------------------------------------------- | ---------------------- |
| `parse_pdb(text, pdb_id)`                    | 解析（按固定列位置切片，不用 split）  |
| `_parse_resolution(line)`                    | 从 `REMARK 2` 抽分辨率      |
| `normalize_pdb_id(raw)`                      | `1m17` → `1M17`；格式不对抛错 |
| `_download(pdb_id)`                          | 从 RCSB 取               |
| `_write_structure` / `_read_structure`       | 写盘（字节）/ 读盘             |
| `_check_importable(source)`                  | **格式与体量的准入检查**         |
| `_import_target(dir, name, payload)`         | 挑写盘位置                  |
| `_render` / `_entry_summary` / `_entry_meta` | 输出与登记                  |

> **值得注意（三处）**：
>
> 1. **`_check_importable` 挡三样，理由各不相同**：目录/不存在/读不动 → 路径错，  
>    早点说清楚，别让它变成解析出来的空结构；扩展名不在白名单 → 认得出的（mmCIF）  
>    单独给一条可操作的错；体量超限 → PDB 文本顶格约 8 MB，放它进来的后果是  
>    **解析安静地只读到开头一部分**，然后登记成一个「有链、但少了一大截」的受体  
>    ——比直接拒绝危险得多。
> 2. **`_import_target` 同名同内容就复用，同名不同内容加序号绝不覆盖。**  
>    覆盖掉的是另一个结构，而它在登记表里还有条目——那条会变 stale，  
>    而用户回到磁盘上已经找不到原来那份了。
> 3. **导入这条路指纹算原始字节。** 用「解码再编码」后的文本算，会在遇到非 UTF-8  
>    字节时（`replace` 会把它们变成替代符）让两份不同内容的文件算出同一个指纹。

### 4.4 `pdbtext.py`（546 行 / 6 个类 / 27 个函数）

**PDB 纯文本编辑层。零第三方依赖**（不登记工具，被 `receptor.py` 用）。

| 类                          | 作用                                                         |
| -------------------------- | ---------------------------------------------------------- |
| `PdbEditError(ValueError)` | 编辑失败                                                       |
| `ResidueKey`               | 残基身份：链 + 残基号 + 插入码                                         |
| `AtomRecord`               | 一个原子：名字、坐标、元素、是否杂原子/水、`key()`、`label()`                    |
| `HeteroGroup`              | 一组非水杂原子（一个配体）：`size()` / `center()` / `coords()`           |
| `_Line`                    | 一行原文 + 归属的模型编号（改行时保留原文）                                    |
| `PdbDocument`              | 整份文档：`atoms()` / `text()` / `group(key)` / `drop(indices)` |


| 函数                           | 作用                          |
| ---------------------------- | --------------------------- |
| `parse(text)`                | 解析全文                        |
| `keep_chains(doc, chains)`   | 只留指定链，返回文档与被丢的计数            |
| `hetero_groups(doc)`         | 归类非水杂原子                     |
| `strip_hetero(doc, keep)`    | 去杂原子（保留 `keep`）             |
| `take_group(doc, group)`     | 抽出一组作为单独文档（生成 `ligand.pdb`） |
| `altloc_residues(doc)`       | 列出有交替构象的残基                  |
| `select_altloc(doc, choice)` | 选一个交替构象，其余清空格位              |
| `calc_box(points, padding)`  | 算盒子中心与边长                    |
| `format_box(center, size)`   | 渲染盒子文本                      |
| `is_heavy(atom)`             | 判一个原子是否重原子（去氢与几何筛查共用）      |

列位置全是模块级常量（`ATOM_CHAIN = 21`、`ATOM_X = slice(30, 38)`…）。  
**按列切，不按空格 split**：PDB 是固定列格式，空格 split 在负坐标（坐标挤在一起）  
时会错位。

> **值得注意**：`is_heavy` 原本在 `export.py` 里，清洗与算口袋都要用之后搬到这一层。
> 两份的话，「重原子」的定义会在某次改动里分叉，而分叉后的症状是口袋数与配体数对不上——
> 看起来像数据问题，实际是两处口径不一致。

### 4.5 `preprocess.py`（422 行 / 4 个类 / 8 个函数）

**结构清洗与杂原子筛查。零第三方依赖**（不登记工具，被 `export.py` 与 `pockets.py` 用）。

它是「展示」与「算口袋」共用的**同一道闸**：两者都要先拿到一份干净、只剩聚合物加真配体的坐标，
否则会把溶剂、缓冲液、单原子离子当成配体，最后掺进一堆假口袋。

| 类                   | 作用                                                        |
| ------------------- | --------------------------------------------------------- |
| `LigandScreen`      | 筛查结果：`kept` / `excluded`（原因 → 计数）/ `describe()`        |
| `PreprocessReport`  | 清洗账本：链、丢掉的链、水、杂原子组、交替构象、氢、模型；`as_dict()` 进 `meta` |
| `PreprocessResult`  | 清洗后的文本 + 报告                                              |
| `_ProteinIndex`     | 蛋白原子的网格索引，`near(atom, cutoff)` 只查相邻 27 格              |

| 函数                                                | 作用                                                    |
| ------------------------------------------------- | ----------------------------------------------------- |
| `polymer_chains(doc)`                             | 只认 `ATOM` 记录的链（`HETATM` 上的链号不算聚合物链）                |
| `screen_ligands(doc, groups, chains=())`          | 三条几何判据：重原子太少 / 与蛋白共价连接 / 蛋白接触不足，命中即排除           |
| `clean_structure(text, chains=(), modified_names=(), keep_ligands=True)` | 一条流水线：选链 → 选交替构象 → 名字筛 → 几何筛 → 删杂原子 → 删水 → 去氢 |

常量 `MIN_LIGAND_HEAVY_ATOMS=4` / `COVALENT_CUTOFF=1.9` / `CONTACT_CUTOFF=4.5` /
`MIN_CONTACT_RESIDUES=3`，四个数一起表达同一个取向：**宁可漏，不要错杀**。

> **值得注意（四处）**：
>
> 1. **名单永远收不全，所以名单之后必须还有一道几何判据。** `export.BUFFER_LIKE` 列了上百个
>    常见溶剂/缓冲液/离子，但 PDB 里能出现的杂分子没有上限（不同实验室的不同沉淀剂）。
>    几何判据只问三件与化学无关的事，因此对没见过的分子同样成立。
> 2. **三条判据是「或」不是「且」，且每条都要能报出原因。** 报给用户的不是一个数字，
>    而是「重原子太少 6 个、蛋白接触不足 3 个」——不然用户只会看到配体凭空消失。
> 3. **网格索引不是优化，是可用性前提。** 十万原子的结构上全表扫蛋白原子要几分钟；
>    按 `CONTACT_CUTOFF` 分格后只看相邻 27 格。结果与全表扫完全一致（判据里有对照用例）。
> 4. **交替构象按残基选，并把 altLoc 列擦干净。** 按文件选会在「A 链选 A、B 链只有 B」时
>    把整条链删空；留着 `altLoc` 字母则会被 Meeko 直接拒收。同一残基的多个构象必须整体二选一。
>    修饰残基（`MODRES`）虽然写着 `HETATM`，却是聚合物的一部分，必须按名字保留。

### 4.6 `chembl.py`（2290 行 / 7 个类 / 61 个函数）

已知活性与类似物。3 个工具。**全仓最大的文件。**

| 工具                     | 作用                                    |
| ---------------------- | ------------------------------------- |
| `SearchActivesTool`    | 按 UniProt 编号取已知活性分子，登记成 **ligandset** |
| `FindAnalogsTool`      | 按相似度找类似物                              |
| `DescribeMoleculeTool` | 取一个分子的详情（机制、适应症、性质）                   |

关键内部类：

| 类                               | 作用                              |
| ------------------------------- | ------------------------------- |
| `_TargetChoice`                 | 靶点解析结果：ChEMBL 编号、名字、备选项、跳过的复合物数 |
| `_Query`                        | 一次分子查询的输入                       |
| `_Snapshot` / `_AnalogSnapshot` | 数据快照（含合并/去重计数）                  |

关键函数组：

| 组   | 函数                                                                              |
| --- | ------------------------------------------------------------------------------- |
| 靶点  | `_resolve_target` / `_sole_component_accession`                                 |
| 活性  | `_count` / `_fetch_activities` / `_assay_facts` / `_normalize` / `_select`      |
| 类似物 | `_similarity_url` / `_fetch_analogs` / `_normalize_analog` / `_select_analogs`  |
| 分子  | `_molecule_query` / `_mechanisms` / `_indications`                              |
| 校验  | `sanity_check_smiles` / `normalize_molecule_id`                                 |
| 缓存  | `_cache_path` / `_write_cache` / `_read_cache` / `_snapshot_from_cache`         |
| 渲染  | `_render_digest` / `_render_analog_digest` / `_render_molecule` / `_render_csv` |

> **值得注意（四处）**：
>
> 1. **服务端确实生效的条件只有四个。** 注释里明写「别往这里加 `confidence_score`  
>    或 `assay__*`——ChEMBL 会**静默忽略**它们，结果看起来筛过了其实没筛」。  
>    这是一类最危险的外部接口行为：不报错、不警告、结果看着正常。
> 2. **缓存键不含 ChEMBL 靶点编号。** 靶点编号得先联网解析才知道，算进键里就意味着  
>    缓存永远不可能纯读盘（每次调用都得先打一次网络）。键只用「UniProt 编号 + 条件」。
> 3. **`POTENCY_FLOOR_HINT = 10.5`。** pChEMBL 是摩尔浓度的负对数，越大活性越强。  
>    接近 10.5 意味着到了检测手段的分辨极限——这个读数写进摘要，是为了让模型知道  
>    「再往上比大小已经没有实际意义」。
> 4. **`sanity_check_smiles` 在发请求之前**。相似性检索那个端点对写错的结构回 HTTP 500，  
>    所以本地先做基本合法性检查，能把一部分错误挡在出网之前。

### 4.7 `chemistry.py`（153 行 / 5 个函数）

可选化学依赖（RDKit / Meeko / gemmi）的守门人。不登记工具。

| 成员                              | 作用                                     |
| ------------------------------- | -------------------------------------- |
| `CHEM_DEPS`                     | 需要真导入探一遍的依赖名                           |
| `CHEM_INSTALL_HINT`             | `pip install -r requirements-chem.txt` |
| `CHEM_TOOL_DESCRIPTION_NOTE`    | 化学工具描述里该跟的那半句                          |
| `_probe_deps()`                 | **真导入一遍**，返回用不了的那些（`lru_cache`）        |
| `missing_chem()`                | 当前环境缺什么                                |
| `chem_available()`              | 能不能用                                   |
| `chem_missing_message(missing)` | 给模型看的话：**缺什么 + 影响什么 + 怎么装**            |
| `require_chem()`                | 不在场就抛一条能照着做的 `ToolError`               |

> **值得注意（三处）**：
>
> 1. **判定「在不在场」用真导入，不用 `find_spec`。** 第一版用 `find_spec`，  
>    理由是「探测永远安全」——那个理由本身没错，但它**看不见「装了却导不进来」**：  
>    实测本机 `find_spec("meeko")` 正常返回，而 `import meeko` 抛  
>    `ModuleNotFoundError: No module named 'gemmi'`。于是闸门放行、工具在函数体里炸，  
>    报错现场离病因（依赖清单里少一行）很远——**探不出问题的闸门等于没有闸门**。
> 2. **报的是 `exc.name`（真正缺席的那个），不是正在探测的名字。** 用户需要装的是 gemmi，  
>    说「缺 meeko」只会让他把已经装好的东西再装一遍。
> 3. **做成「工具照常出现在清单里、调用时才报错」。** 另一种做法是缺依赖就让工具消失，  
>    但那会让模型面对一个它无从解释的缺口——要么编一个工具调用，要么对用户说「我做不到」。  
>    这里返回的是一条**可操作**的错：模型的下一步动作就是把安装指令念给用户。

依赖清单里**把 numpy / scipy / gemmi 这类传递依赖也列了进来**。一般不该这么写，  
但 meeko 0.8.0 的元数据里 `requires_dist` 是空的——它不声明依赖。gemmi 那一项  
不是理论上的：`meeko/__init__.py` 一路 `import gemmi`，是导入 meeko 的硬前提。

### 4.8 `receptor.py`（1351 行 / 6 个类 / 37 个函数）

准备受体。1 个工具（`PrepareReceptorTool`），但它是全仓最长的单个工具。

| 类                          | 作用                              |
| -------------------------- | ------------------------------- |
| `Plan`                     | 准备方案：链、配体、盒子、`params()` 化成 dict |
| `Parameterization`         | 参数化结果：pdbqt 文本                  |
| `ChemRejection(Exception)` | **可操作的拒绝**：错误 + 建议              |
| `Built`                    | 构建中间产物：文档、链、配体文本、去杂统计           |
| `Situation`                | 展示状况：源、链、水、杂原子、配体、盒子、产物目录       |

关键函数：

| 组   | 函数                                                                   |
| --- | -------------------------------------------------------------------- |
| 参数化 | `_meeko` / `_meeko_version` / `_templates` / `parameterize`          |
| 构建  | `_select_chains` / `_resolve_ligand` / `_build`                      |
| 产物  | `_product_dir` / `_write_product` / `has_product` / `_read_params`   |
| 渲染  | `_render_preview` / `_render_done` / `_render_rejection` / `_render` |
| 报告  | `_report` / `_prepared_summary` / `_find_prepared`                   |
| 校验  | `_tokens` / `_chain_token` / `_vector` / `_safe_stem`                |

**两阶段确认**：默认只做预演（什么都不写盘），`confirm=true` 才真做。

> **值得注意（四处）**：
>
> 1. **依赖闸放在参数校验之前。** 理由是它更早：缺依赖时连参数都不必看，  
>    用户要做的事只有一件（装依赖）。但要把「参数写错」与「依赖没装」分开，  
>    所以单独成一档失败。
> 2. **`padding` 上限 `MAX_PADDING = 20.0`。** 报错文案里写了理由：  
>    「盒子尺寸随它平方增长，笔误的代价是对接慢上几个数量级」。
> 3. **严格起步**：模板匹配不上的残基会被**拒绝并列出具体哪些**，由模型决定  
>    要不要用 `relax=true` 丢掉。交替构象也必须显式选（`altloc`）。  
>    这是「宁可停下问，不要悄悄改」的取向。
> 4. **复用只发生在真要写盘的那一次**：预演仍然把 Meeko 跑一遍。  
>    反过来的话，「预演说会产出多少原子」就会变成一句没法验证的话。

### 4.9 `listing.py`（406 行 / 4 个类 / 9 个函数）

登记表的浏览与切换。3 个工具。**与领域无关**——手里拿的是受体还是配体集，  
它们并不需要知道。

| 工具                   | 作用                     | 是否收登记表                         |
| -------------------- | ---------------------- | ------------------------------ |
| `ListWorkspaceTool`  | 列出条目 + 报告当前受体          | 是（`context_argument="ledger"`） |
| `DescribeEntryTool`  | 查单条详查：路径、摘要、指纹、血缘、文件状态 | 是                              |
| `SwitchReceptorTool` | **两阶段**切换当前受体          | 是                              |

| 中间层                              | 作用                                    |
| -------------------------------- | ------------------------------------- |
| `render_entries(entries, limit)` | 渲染逐行摘要，超量只报条数。**列表工具与切换工具共用**         |
| `path_display(path)`             | 渲染路径，**目录补尾斜杠**                       |
| `_read_kind(raw)`                | 读 `kind` 参数，认不出来返回哨兵（**不抛异常**，要能回灌自纠） |

> **值得注意（三处）**：
>
> 1. **`render_entries` 必须是模块级共用的。** 截断口径只能有一份：列表工具走这里，  
>    切换工具的影响范围也走这里。两边各写一份的话，改了其中一处，另一处会在长会话里  
>    把上下文吃干——而且看上去完全正常。
> 2. **`path_display` 补尾斜杠不是装饰。**「文件还是目录」是模型要看到的一等信息：  
>    `receptor#1` 是一个 `.pdb` 文件（还不能对接）还是一个目录（准备后的受体，  
>    里面是结构 + 参数 + 体检报告），决定了它能不能交给下一个工具。  
>    两者在路径字符串上长得一样，差异只能由这里补出来。
> 3. **`ListWorkspaceTool` 里单独再点一遍「坏掉了的」条目名。** 因为 `render_entries`  
>    有条数上限，被截断时恰好可能把出问题的那几条截掉，而「哪些不能用」  
>    是这份清单里最不能漏的信息。

### 4.10 `all_tools.py`（27 行 / 1 个函数）

把上面各模块的工具合成一份，供应用一次装完。

| 函数               | 作用                                                                 |
| ---------------- | ------------------------------------------------------------------ |
| `domain_tools()` | 按固定顺序拼：`listing` → `uniprot` → `structure` → `chembl` → `rsearch` → `literature` → `receptor` → `ligand` → `docking` → `export` |

> **值得注意**：顺序**有意如此**——先浏览、再检索、最后改动。浏览排最前，  
> 因为模型的第一件事应当是看清手上有哪些条目，而不是先动手。  
> 同一份清单也决定了工具在请求体里的先后，**靠前的更容易被选中**。
>
> 单独一个文件而不是让 `app.py` 逐个模块展开：装配处只该知道一件事  
> （有一组领域工具要装）。否则每加一个领域模块都要回去改装配，而 `app.py`  
> 的本职是管屏幕与状态——改动分散在两处，漏掉一处的结果是「工具写了但模型看不见」。

---

## 五、界面层

### 5.1 `screens/`（10 个文件 / 14 个类）

一个屏幕 = 一种界面状态。

#### `home.py`（257 行 / 2 个类）

| 类            | 作用                   |
| ------------ | -------------------- |
| `HomeInput`  | 启动界面的输入框（带按键拦截）      |
| `HomeScreen` | 启动界面：logo、输入框、命令补全弹窗 |

`HomeScreen` 关键成员：

| 成员                        | 作用                                          |
| ------------------------- | ------------------------------------------- |
| `BINDINGS`                | 启动界面的绑定（另有 `enter` 发送）                      |
| `_update_command_popup()` | 输入变化时刷新命令候选，**用 `registry.is_exact()` 判收起** |
| `action_cycle_agent()`    | 切 Agent                                     |
| `action_quit_app()`       | **屏幕自己的退出动作**——不是绑 App 层的 `quit`            |

> **值得注意**：启动界面的退出键（现为 `ctrl+c`）原来绑动作名 `quit`（动作在 App 层），  
> 按下去**没有任何反应**——因为 `TextArea.check_consume_key` 先把按键接住了，  
> App 层的绑定轮不到。改成屏幕自己的 `action_quit_app()` 走  
> `execute_command("exit")` 才生效。**新加屏幕级快捷键时先按一次确认。**

#### `chat.py`（411 行 / 2 个类）

| 类            | 作用              |
| ------------ | --------------- |
| `SidePanel`  | 右侧栏（登记表条目 / 状态） |
| `ChatScreen` | 聊天主界面           |

`ChatScreen` 关键成员：

| 成员                           | 作用                                                      |
| ---------------------------- | ------------------------------------------------------- |
| `on_mount()`                 | 订阅事件总线                                                  |
| `_submit_prompt(text, mode)` | 按 mode 分流：`chat` 走模型、`bash` 走 `run_bash`、`command` 走命令表 |
| `rebuild()`                  | 从会话数据整体重建消息区                                            |
| `refresh_status()`           | 刷状态栏                                                    |
| `toggle_sidebar()`           | 开关右栏                                                    |
| `action_*`                   | 屏幕级快捷键动作                                                |
| `_on_*`                      | 事件处理                                                    |

#### 其余屏幕

| 文件                  | 类                                                        | 作用                                                                     |
| ------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------- |
| `permission.py`     | `PermissionAnswer` / `PermissionDialog`                  | 权限询问对话框；`PermissionAnswer` 是答案枚举，`PermissionDialog` 把答案 `dismiss()` 回去 |
| `session_picker.py` | `ConfirmDialog` / `RenameDialog` / `SessionPickerScreen` | 会话列表 + 两个小对话框（确认删除 / 改名）                                               |
| `connection_form.py` | `ConnectionFormScreen`                                  | `/model` 的接口表单（地址、密钥、模型名、窗口）                                           |
| `theme_selector.py` | `ThemeSelectorScreen`                                    | 主题列表                                                                   |
| `tools_panel.py`    | `ToolEntry` / `ToolsPanelScreen`                         | `/tools` 工具开关面板：空格/回车逐条开关，改完立刻生效                             |
| `help.py`           | `HelpScreen`                                             | 帮助：命令表 + 快捷键表（快捷键来自 `keybind_manager`）                                 |
| `diff_overlay.py`   | `DiffOverlayScreen`                                      | 补丁的 diff 展示                                                            |
| `message.py`        | `MessageScreen`                                          | 只读文本弹窗。`esc` / `enter` / `q` 关掉                                        |

> **值得注意**：`MessageScreen` 的 `ident` 参数让同一个类服务多个弹窗  
> （id 前缀不同），帮助界面、diff 弹窗与它同一类：只显示、不收输入。

### 5.2 `widgets/`（8 个文件 / 20 个类）

#### `messages_pane.py`（1172 行 / 10 个类）

**界面层最大的文件。** 消息列表，支持增量更新、虚拟滚动与自动滚到底。

| 类                   | 作用                          |
| ------------------- | --------------------------- |
| `CoalescedText`     | 流式文本的合并窗口（正文与思考块共用）        |
| `TextPartView`      | 一段正文                        |
| `FoldBlock`         | 可折叠区块（长内容折叠）                |
| `ReasoningPartView` | 一段思考                        |
| `ToolCard`          | 一次工具调用（参数、结果、耗时、状态）         |
| `SimplePartView`    | 其余 Part 的通用渲染               |
| `PatchPartView`     | 补丁（点开进 diff 弹窗）             |
| `MessageView`       | **一条消息** = 若干 Part 视图       |
| `NewMessagesHint`   | 底部「有新内容」提示条（`dock: bottom`） |
| `MessagesPane`      | 列表本体（`ScrollableContainer`） |

`MessagesPane` 的两个机制：

| 机制       | 常量/字段                                                       | 说明                             |
| -------- | ----------------------------------------------------------- | ------------------------------ |
| **虚拟滚动** | `HISTORY_PAGE=30` / `MAX_MOUNTED=90` / `LOAD_MORE_MARGIN=3` | 只挂载最近的 N 条；滚到顶再挂上一页            |
| **增量更新** | `_part_views` / `_message_views`                            | Part 变化只改对应的那个视图，不重建整条消息       |
| **流式合并** | `TEXT_REFRESH_SECONDS=0.05` / `CoalescedText`                | 正文与思考块都按 50ms 合批重写，不是每个 token 刷一次 |
| **焦点重绘** | `_on_focus` / `_on_blur` / `watch_has_focus`                | 焦点变化不重画整块                      |

| 方法组 | 方法                                         |
| --- | ------------------------------------------ |
| 加内容 | `add_message` / `add_part` / `update_part` |
| 整体  | `rebuild` / `clear_messages`               |
| 窗口  | 挂载窗口维护相关私有方法                               |

> **值得注意（两处）**：
>
> 1. **`check_toggle_registry` 判据会摆动**（14 次里通过 9 次），失败有两种样子：  
>    `IndexError`，或自检段报「这条不变式是空转的」。两种都源于  
>    「生成结束后消息区挂载的思考块视图是 0 个」——消息区是虚拟滚动，  
>    `pane.query(...)` **只数得到当前挂载的那些**，最后一次自动滚动与虚拟高度重算  
>    谁先谁后不固定。单独重跑能过，**不用当成产品问题查**。
> 2. **同一目标重复抓帧时，消息区滚到的行数会差一到两行**（同一个原因）。  
>    所以七帧里六帧判「关键内容都在 + 非空行数相近」，只有 `home` 与弹窗区域  
>    逐行比。只抓一轮就下「产物与源码不同」的结论，会把力气花错地方。

#### `editor_pane.py`（467 行 / 3 个类 / 39 个方法）

输入编辑器：多行、模式切换、三套补全。

| 类                   | 作用                     |
| ------------------- | ---------------------- |
| `ScrollingTextArea` | 内容超出可见高度后滚动跟着光标走的多行输入框 |
| `PromptInput`       | 输入框：焦点进出不重画整块，滚动跟着光标走  |
| `EditorPane`        | 容器：输入框 + 状态行 + 三个补全弹窗  |

模块级：

| 成员                                        | 作用                            |
| ----------------------------------------- | ----------------------------- |
| `search_files(root, query, limit)`        | 工作目录内模糊文件搜索                   |
| `FILE_TOKEN = re.compile(r"@([^\s@]*)$")` | `@` 文件引用                      |
| `COMMAND_TOKEN = re.compile(r"^/(\S*)$")` | 斜杠命令（**整段输入以 `/` 开头且后面没有空格**） |
| `IGNORED_DIRS` / `MAX_SCAN_FILES`         | 忽略目录与扫描上限                     |
| `MODE_LABEL`                              | 三种模式的中文名                      |

`EditorPane` 内嵌两个消息类：`Submitted(text, mode)`、`AgentChanged(agent)`。

| 方法组  | 方法                                                                                       |
| ---- | ---------------------------------------------------------------------------------------- |
| 输入变化 | `on_text_area_changed` → `_update_mode` / `_update_file_popup` / `_update_command_popup` |
| 提交   | `action_submit`（**弹窗开着时改为采纳候选项**）                                                        |
| 补全   | `action_popup_up/down` / `action_agent_popup` / `action_close_popup`                     |
| 历史   | `action_history_prev/next`                                                               |
| 采纳   | `_accept(popup)`                                                                         |
| 状态行  | `update_context` / `refresh_status_line` / `_write`                                      |
| 焦点   | `on_descendant_focus` / `on_descendant_blur` / `focus_input`                             |

> **值得注意（五处，全是踩过的坑）**：
>
> 1. **`search_files` 用 `os.walk` 而非 `rglob`。** 换成 `rglob` 的话，忽略是在  
>    取到文件**之后**才判断的，`rglob` 仍然会走遍 `build/` 与 `.venv` 里的每一个条目  
>    ——那些条目照样占用 `MAX_SCAN_FILES` 的名额，名额用完就 break，  
>    后来的文件一个都看不见，候选列表于是取决于目录遍历顺序。
> 2. **`ScrollingTextArea` 要补滚一次。** `TextArea._watch_selection` 在光标移动时  
>    立即滚一次，用的还是插入之前算出的滚动上限：刚插进去的那行还没进虚拟高度，  
>    `max_scroll_y` 少一，`clamp_delta` 就把滚动量截掉一行。内容还在可见高度以内时  
>    看不出来；一旦超出，**光标停在输入区外面一行**，此后每次插入都差这一行。
> 3. **`PromptInput` 拦掉基类的整块重绘。** `Widget._on_focus` 会调一次不带区域的  
>    `self.refresh()`，把输入区整块（115 列 × 8 行）标成脏区域。焦点从输入框挪到  
>    消息区是「点一下聊天界面」这条路上字节数的主要来源——实测这一块整块重画  
>    占掉那一次点击 3587 字节里的绝大一部分。
> 4. **`_update_mode` 只在模式真变了才动类。** 原来的写法每敲一个字符都把三个类  
>    删一遍再加回一个，即使模式一个字都没变，也会让这一块连同子组件重算样式。
> 5. **`action_close_popup` 关掉命令补全后要设 `_command_dismissed`。** 只收起弹窗的话，  
>    接着打下一个字母又会去匹配一次，`esc` 等于没按。抑制到「输入不再是命令前缀」为止。  
>    **`_accept` 采纳命令时只填 `/名字`、不带尾空格**——带空格会让下一次输入变化  
>    又去匹配一次，弹窗刚收起来就又弹开。


#### `file_autocomplete.py`（206 行 / 2 个类 / 18 个方法）

| 类                   | 作用                 |
| ------------------- | ------------------ |
| `AutocompletePopup` | **补全弹窗基类**（三套补全共用） |
| `FileAutocomplete`  | 文件补全（`@` 触发）       |

`AutocompletePopup` 关键成员：

| 成员                                 | 作用                                                     |
| ---------------------------------- | ------------------------------------------------------ |
| `MAX_ROWS = 6`                     | 一次显示几个候选。**高度由它算出来**（见 `__init__`）                     |
| `DEFAULT_CSS`                      | `overlay: screen` + `offset: 0 -100%`（浮层，不占布局流）        |
| `__init__(id, title, keep_below)`  | 设 `styles.offset` 与 `styles.max_height = MAX_ROWS + 3` |
| `set_items(items)`                 | 设候选；刷新后 `call_after_refresh(_clamp_to_screen)`         |
| `_clamp_to_screen()`               | 把高度压到这一屏放得下                                            |
| `_available_rows` / `visible_rows` | 可用行数 / 真实可见行数（**只能由 `region.bottom` 算**）               |
| `_window()`                        | 返回（起始下标，这次显示的候选段）——**窗口跟着高亮走**                         |
| `_title_line()`                    | 标题行，候选多于一屏时附「这一段 / 共几条」                                |
| `_render_text()`                   | 正文；候选走 `escape()`（路径里的方括号不能被当标签）                       |

> **值得注意（三条，都是浮层带来的）**：
>
> 1. **浮层不占布局流。** 占流那会儿打一个 `/` 会把输入区块撑高 11 行、把消息区顶掉，  
>    终端只有 16 行时连输入框都被挤出屏幕。但 overlay 只挡住「推进后续兄弟的 y」，  
>    **高度仍算进父容器的内容总高**——启动界面的弹窗挂在居中的 `#home-center` 里  
>    会把 logo 与输入框整块挪 5 行，所以必须挂到 `#home-frame` 下。
> 2. **可见行数只能由 `region.bottom` 算，不能由 `region.height` 算。**  
>    高度由内容行数定、内容行数又由可见行数定，两个互相牵住会锁成 1 条候选。  
>    `offset: -100%` 让底边只取决于摆放位置、与高度无关。
> 3. **高度必须由 `MAX_ROWS` 算出来，写在 CSS 里会走散。** 子类的 `DEFAULT_CSS`  
>    盖不掉基类的 `max-height`（实测命令弹窗写了 10，`styles.max_height` 量出来仍是  
>    基类的 8），容器 8 行减去上下边框只剩 6 行内容区，只显示标题 + 5 条，  
>    而窗口按 `MAX_ROWS=8` 切，`▸` 标记从第 6 条起就画在容器外面看不见，  
>    **回车却照样采纳那条看不见的命令**。

#### 其余部件

| 文件                        | 类                     | 作用                                                            |
| ------------------------- | --------------------- | ------------------------------------------------------------- |
| `command_autocomplete.py` | `CommandAutocomplete` | 命令补全。`MAX_ROWS = 8`；候选项底下挂着 `Command` 对象（采纳要名字，展示要「/名字 + 说明」） |
| `agent_autocomplete.py`   | `AgentAutocomplete`   | Agent 补全（`tab` 触发）。`AGENTS = ["Build", "Plan"]`               |
| `command_palette.py`      | `CommandPalette`      | `ctrl+p` 命令面板：输入框 + 列表 + `fuzzy_match`                        |
| `status_bar.py`           | `StatusBar`           | 底部状态栏：左端路径+分支，右端 token/花费/耗时                                  |
| `toast.py`                | `ToastNotification`   | 右下角浮层通知，数秒后自动消失                                               |

`status_bar.py` 的三个模块级函数：

| 函数                          | 作用                     |
| --------------------------- | ---------------------- |
| `shorten_path(path, limit)` | 路径截断到指定长度，`…` 收尾       |
| `git_branch(cwd)`           | 读 git 分支（子进程，失败返回 `-`） |
| `format_tokens(tokens)`     | `157800` → `157.8K`    |

> **值得注意**：`StatusBar.on_mount` **先把路径画出来，git 分支交给另一个线程去问**。  
> 问分支要起一个 git 子进程，实测一次 110 到 220 毫秒。在事件循环上同步等它回来，  
> 这期间键盘输入与重绘全部排队；进聊天界面那段路上要问好几次，累计将近一秒。
>
> `StatusBar` 与 `EditorPane` 都有 `_write(selector, text)` &#x7684;**「内容没变就不写」**&#x4F18;化。  
> 理由相同：`Static.update()` 每次都带走一遍整屏重新布局，而状态刷新在一轮生成里  
> 会被调到很多次，内容往往一个字都没变。

---

## 六、连接关系：一次「问一句话」经过了哪些文件

这是把所有层串起来看的最好方式。以用户在聊天界面输入 `帮我看一下 EGFR` 并回车为例：

```
1. EditorPane.on_text_area_changed      输入变化 → 收起了补全弹窗
2. EditorPane.action_submit             Enter → 记历史、清输入框
3. 发出 Submitted(text, mode="chat")     Textual 消息
4. ChatScreen._submit_prompt            收到消息，按 mode 分流
5. AgentLoop.stream_reply(prompt)       进入内核
   ├─ prompt.stable_prefix              身份 / 纪律 / 规则（每步重建，字节稳定）
   ├─ context.ContextAssembler.build    分层装配：前缀 → 记忆 → 任务 → 近期 → 证据 → 历史
   │    └─ loop._task_text(session)     任务状态渲染成一段（空状态给空串，不占额度）
   ├─ prompt.estimate_tokens            估预算，超了按降级链削（削不动就报错）
   ├─ provider.OpenAICompatProvider.stream()
   │    └─ _parse_chunk / _collect_calls   流式事件往回推
   ├─ 回调 on_text → MessagesPane.add_part / update_part
   ├─ 模型要调 search_targets
   │    ├─ permission.PermissionPolicy.check   判定
   │    ├─ hooks.HookRunner.run(PreToolUse)    外部钩子
   │    ├─ uniprot.SearchTargetsTool.run
   │    │    └─ http.get_json                 唯一的网络出口
   │    ├─ hooks.HookRunner.run(PostToolUse)
   │    └─ ToolResult 回灌进 messages
   ├─ 模型要调 task（记录目标与用户约束）
   │    └─ task.TaskTool.run                 改的是 session.task_state，随会话写盘
   ├─ 再问模型 …… 循环到不再要工具
   └─ trace.TraceWriter.write                每一句事件写进 JSONL
6. 回调 on_turn_done → ChatScreen.refresh_status → StatusBar.update_status
7. app._on_turn_done → SessionManager.save_to_toml    原子写状态文件
```

**把同一件事分开看，各层各自的分工**：

| 环节         | 谁负责                                           | 不该由谁负责          |
| ---------- | --------------------------------------------- | --------------- |
| 按键怎么变成文本   | `EditorPane` / `PromptInput`                  | 内核不知道键盘         |
| 文本怎么变成一次生成 | `ChatScreen._submit_prompt`                   | 屏幕不知道怎么问模型      |
| 一次生成怎么跑完   | `AgentLoop._run_steps`                        | 循环不知道怎么显示       |
| 怎么显示       | `MessagesPane` 一族视图                           | 视图不知道数据从哪来      |
| 模型怎么调      | `provider`                                    | provider 不知道有工具 |
| 工具怎么被选中与执行 | `tools.ToolRegistry` + `permission` + `hooks` | 注册表不知道工具体里是什么   |
| 领域知识在哪     | `domain/*`                                    | 内核不含一处业务概念      |
| 「我手上有哪些东西」 | `ledger.SessionLedger`                        | 登记表不含一处业务概念     |
| 「这次要做成什么」  | `models.TaskState`（模型走 `task` 工具、用户走 `/task`） | 内核不替模型判断目标是什么   |
| 「这个项目已知什么」 | `memory.MemoryStore`                          | 记忆只收路径与指纹，不收会话内的 handle |
| 会话怎么存      | `session_manager`                             | 不知道屏幕，也不知道模型    |

---

## 七、跨层的五套「双份清单」，各自怎么保持一致

这是全仓最容易出问题的一类结构：**同一件事在两处各写了一份**。它们都有意保留，  
因为合并的代价更大，但**每一套都需要一条机械的守门方式**。

| 双份清单   | 两处                                                  | 守门方式                                                 |
| ------ | --------------------------------------------------- | ---------------------------------------------------- |
| 命令名    | `command_registry.COMMANDS` / 各 `app._command_*` 方法 | `check_command_coverage.py` 判断据：两边逐条核对               |
| 快捷键    | 真实 `BINDINGS` / `keybind_manager.describe()`        | `tests/test_smoke.py` 逐条与真实绑定比对，剔除 `CONTEXTUAL_KEYS` |
| 化学依赖   | `chemistry.CHEM_DEPS` / `requirements-chem.txt`     | 断言机械守着「两边对不上时用户会拿到照做也没用的安装指令」                        |
| 化学工具描述 | 各工具描述字面量 / `CHEM_TOOL_DESCRIPTION_NOTE`             | 断言守着「每个要化学依赖的工具，描述里都含这句话」                            |
| 记忆两份   | `entries.json`（程序读的真相）/ `memory.md`（给人看的渲染）          | `check_memory.py` 守着「两份都写出来、都以原子换名写出」                 |

> **值得注意**：这四套里，**命令名那一套最容易漏**——加一条命令只改注册表、  
> 忘了加 `_command_*` 方法，表现为「命令在补全里看得到，回车没反应」。  
> 这是**静默失败**，只能靠判据抓。

---

## 八、值得注意的地方（汇总）

下面这些是读源码时标出的观察点。**都不是施工单**——凡是有意为之的写法，  
注释里都写了理由，这里只是集中列出来方便回头查。

### 8.1 静默失败类（不报错、画面看着正常）

| 位置                            | 现象                                     | 为什么危险                      |
| ----------------------------- | -------------------------------------- | -------------------------- |
| `session_manager` 的 id 计数器    | 读了盘但没 `sync_sequences()` → 新建 id 撞上已有的 | Part 被覆盖，画面只是「少了一段」        |
| `command_registry.is_exact`   | 别名没算「打全」→ 回车被弹窗截走                      | 命令一次都没执行，看着像没反应            |
| `AutocompletePopup` 的高度       | 写在 CSS 里与 `MAX_ROWS` 走散                | 高亮项画在容器外，**回车照样采纳看不见的那条**  |
| `chembl` 的筛选条件                | 往参数里加 `confidence_score` / `assay__*`  | 服务端**静默忽略**，结果看着筛过了其实没筛    |
| `ledger.fingerprint_*`        | 五个口径混用                                 | 「两份东西算成一个」→ **静默用错东西**     |
| `structure._check_importable` | 体量超限不拦                                 | 解析安静地只读开头一部分，登记成「少了一大截」的受体 |
| `prompt._translate_*`         | `Part` 结构改了但翻译层没跟                      | 不崩，只是模型看到的历史变形             |
| `memory` 的引用                | 写进 handle 而不是路径                         | 换个会话指向别的东西，**不报错**          |

### 8.2 顺序不能换的地方

| 位置                                 | 顺序                | 换了的后果                    |
| ---------------------------------- | ----------------- | ------------------------ |
| `loop._execute_tool`               | 查表 → 策略 → 钩子 → 执行 | 钩子在策略前就绕过权限；策略在执行后没有意义   |
| `receptor.PrepareReceptorTool.run` | 依赖闸 → 参数校验 → 业务   | 缺依赖时用户还得先看一堆参数错，焦点错位     |
| `chembl.sanity_check_smiles`       | 本地校验 → 出网         | 写错结构白跑一次网络，还拿到误导性的 500   |
| `all_tools.domain_tools`           | 浏览 → 检索 → 改动      | 靠前的更容易被选中，顺序变了模型的默认动作就变了 |

### 8.3 「两处必须同时改」的地方

| 改这里                          | 必须同步改那里                                        |
| ---------------------------- | ---------------------------------------------- |
| `command_registry.COMMANDS`  | `app._command_*` 方法                            |
| `app.ARGUMENT_COMMANDS`      | 对应的 `_command_*` 方法签名（要收一个参数）                  |
| 任何 `BINDINGS`                | `keybind_manager.describe()`                   |
| `chemistry.CHEM_DEPS`        | `requirements-chem.txt`                        |
| `Part` / `Message` 结构        | `prompt._translate_*`                          |
| `ToolSpec.parameters`        | 工具 `run` 里的参数读取（**JSON Schema 校验与实际读的参数要对得上**） |
| `TaskState` 加字段              | `to_dict` / `from_dict` / `render` / `TASK_LIST_FIELDS` 四处（别名表 `TASK_FIELD_ALIASES` 也要带上） |
| `AutocompletePopup.MAX_ROWS` | 无需改别处（高度由它算出）——这是做对了的一处                        |

### 8.4 已知会摆动、不用当产品问题查的

| 判据                         | 现象                                                       | 原因                                                           |
| -------------------------- | -------------------------------------------------------- | ------------------------------------------------------------ |
| `check_toggle_registry`    | 14 次里通过 9 次；`IndexError` 或「不变式是空转的」                      | 消息区虚拟滚动，`pane.query(...)` 只数得到当前挂载的视图；最后一次自动滚动与虚拟高度重算谁先谁后不固定 |
| 七帧抓帧比对                     | 消息区滚到的行数差一到两行                                            | 同上                                                           |
| `check_toggle_registry` 之外 | `check_command_coverage.py` 的孤儿进程占 `.probe/logs/*.jsonl` | 外壳 bash 被回收、python 子进程变孤儿继续跑                                 |

### 8.5 贴到临界规模的

| 位置                 | 现状                    | 观察                                    |
| ------------------ | --------------------- | ------------------------------------- |
| `app.py`           | 1 个类 / 50 个方法         | 其中一半是两行的转发；再加一组命令就该考虑分出 `commands/` 包 |
| `chembl.py`        | 2290 行 / 61 个函数       | 最大的文件；渲染函数与取数函数混在一起，是可以先分开的一处         |
| `ledger.py`        | 880 行 / 48 个方法        | 句柄、指纹、条目、登记表四块概念挤在一个文件里               |
| `messages_pane.py` | 893 行 / 8 个类 / 70 个方法 | 8 个视图类 + 窗口机制；机制部分可以单独成文件             |

---

## 九、这份文件本身的维护

- **改动源码之后**：受影响的章节要一起改。最容易忘的是 §七（双份清单）与  
  §八.3（两处必须同时改）——这两处列的就是「改了 A 忘了 B」的高发组合。
- **新增领域模块**：§四 加一节，§四.9 的顺序表也可能要动。
- **行数与计数**：本文开头引的数字可以用  
  `for f in $(find opencode_tui -name "*.py" -not -path "*__pycache__*"); do wc -l $f; done`  
  重新核一遍。数字会漂，结论（哪几处贴到临界）不会。
