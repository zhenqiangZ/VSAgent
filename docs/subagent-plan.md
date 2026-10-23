# 子 Agent 派发：设计与实施方案

已确认的三个前提：

1. **「指定某个时间段」= 父会话的一段消息区间**（第 N 条到第 M 条），这段内容复制成子 agent 的初始上下文。
2. **后台并行运行**：派发后子 agent 自己跑，父会话可以继续提问。
3. **完全共享**：工作目录、`runs/`、登记表（ledger）都与父会话共用。

核心结论：一个子 agent = 一条独立持久会话 + 一个独立 `AgentLoop` + 一条独立 asyncio 任务。
深度硬限制为 1 层：根会话可以派发，子 agent 内不能再派发。

---

## 一、界面设计

### 1. 根会话右侧栏

现有 `SidePanel`（`screens/chat.py`）默认隐藏、只放上下文。改造为一个右栏、两段内容：
子 Agent 列表在上，上下文信息在下。**宽度保持 30 列**，现有布局判据依赖这个数字。

```
┌ opencode tui ─────────────────────────────────────────────────────────────┐
│ 消息区                                              │ 子 AGENT (2)          │
│                                                     │ ● 靶点调研   14:32 12条│
│ 你：把 4HJO 这条对接流程跑通                          │ ✓ 参数对比   14:05 31条│
│                                                     │                       │
│ ◆ 子 agent: 靶点调研                                 │ ── 上下文 ──          │
│ 助手：已派发子 agent「靶点调研」，它在后台跑。        │ 会话  4HJO 对接        │
│                                                     │ 消息  18 条 · 12.4k tok│
├─────────────────────────────────────────────────────┴───────────────────────┤
│ > 输入…                                                          Build 模型 │
└─────────────────────────────────────────────────────────────────────────────┘
```

列表项（每行两行高）：

```
● 靶点调研            14:32
  运行中 · 12 条消息
```

- 状态：`●` 运行中、`○` 空闲、`✓` 完成、`✕` 失败、`‖` 已中断。
- 排序：按 `created_at` 升序；新条目出现时滚到底。
- 只显示直接子会话（`parent_id == 当前会话.id`）。
- 空态：整个子 Agent 段不渲染。
- 第一次创建子 agent 时自动把右栏设为可见，之后仍可用 `/sidebar` 收起。
- 用 `OptionList` 而不是 `Static`：需要鼠标点击与键盘上下。

### 2. 创建入口

用户入口两个，都打开同一个模态：输入 `/agent`，或 `ctrl+p` 命令面板选「派发子 agent」。

```
┌ 派发子 Agent ───────────────────────────────────────────┐
│ 名称   [靶点调研                          ]              │
│                                                          │
│ 范围   第 [ 3 ] 条  →  第 [ 12 ] 条   共 10 条            │
│        14:02 你：帮我把 4HJO 的对接流程跑通               │
│        14:31 助手：对接盒子中心是 (12.4, 8.1, 30.2)       │
│                                                          │
│ 任务   ┌──────────────────────────────────────────────┐  │
│        │ 只查 4HJO 的共晶配体与 4.5Å 内的口袋残基，     │  │
│        │ 输出成一张表，不要动别的文件。                 │  │
│        └──────────────────────────────────────────────┘  │
│                                                          │
│ 说明   创建后立即在后台开始，父会话可以继续用             │
│        子 agent 内不能再派发新的 agent                    │
│                                                          │
│        enter 创建并运行      esc 取消      tab 切换字段   │
└──────────────────────────────────────────────────────────┘
```

| 字段 | 控件 | 规则 |
| --- | --- | --- |
| 名称 | `Input` | 空则取任务首行前 20 字；重名自动加 ` (2)` |
| 起点/终点 | 两个数字 `Input` | `1 ≤ start < end ≤ len(messages)`，越界红字提示，不关窗 |
| 任务 | `TextArea` | 必填，成为子 agent 的第一条用户消息 |
| 预览 | `Static` | 显示区间首尾消息的时间与首行 |

范围默认整段会话。区间 token 超过可用预算 60% 时提示将从最早的消息开始截断。

### 3. 子 agent 界面

```
┌ ◀ 返回  子 AGENT · 靶点调研    父会话：4HJO 对接    ● 运行中 · 14:32 ──────┐
│ 消息区                                              │ 子 AGENT (2)          │
│ 你：[子 agent 任务] 只查 4HJO 的共晶配体…            │ ✓ 参数对比            │
│ 助手：…                                             │ ● 靶点调研（当前）     │
├─────────────────────────────────────────────────────┴───────────────────────┤
│ > 输入…                                              子 agent · Build 模型  │
└─────────────────────────────────────────────────────────────────────────────┘
```

- 顶部 banner 可点击返回；`ctrl+b` 与 `/back` 也能返回。
- `esc` 保持中断语义：运行中按 esc 中断，空闲时按 esc 返回。
- 没有任何创建入口：命令面板过滤 `/agent`，直接执行会提示「子 agent 内不能再派发」。
- 右栏显示同一父会话下的兄弟 agent，点击可切换，返回栈是 A → B → 父会话。
- 正在后台跑时进入：继续接收流式事件，输入框禁用并提示「正在运行，esc 可中断」。

### 4. 后台 agent 的权限询问

权限弹窗上移到 `app` 层统一处理，标题带 agent 名：

```
┌ 需要确认 ─────────────────────────────────────┐
│ 子 Agent  靶点调研（ses_0007）                 │
│ 工具      write                               │
│ 参数      runs/4hjo/table.csv                 │
│ [a] 本会话全部允许  [y] 允许  [n] 拒绝  esc 拒绝│
└───────────────────────────────────────────────┘
```

答复按 `session_id` 路由到对应 loop，不再依赖当前屏幕。

---

## 二、数据模型与持久化

### 1. Session 新字段

```python
parent_id: str = ""
root_id: str = ""          # 根会话 id；根会话自己为空
depth: int = 0             # 0=根，1=子 agent
origin: str = "user"       # user | agent
seed_start: int = 0        # fork 起点（父会话 messages 下标，含）
seed_end: int = 0          # 终点（不含）
status: str = "idle"       # idle | running | done | failed | cancelled
status_note: str = ""
spawned_at: float = 0.0
```

`to_dict` / `from_dict` 全部带默认值，老状态文件照常读。`STATE_VERSION` 从 1 提到 2。
载入时把 `running` 复位成 `idle` 并写 `status_note = "上次退出时仍在运行"`。

### 2. fork 规则

1. 取 `parent.messages[start:end]`。
2. **逐条新建 Message / Part，ID 全部重新生成**。复用 ID 会让两个会话里的 `msg_0007` 撞车。
3. 跳过 `STEP_START` / `STEP_FINISH` / `RETRY` / `COMPACTION`，保留
   `TEXT` / `TOOL` / `FILE` / `PATCH` / `SUMMARY` / `ERROR` / `REASONING`。
4. `task_state` 只继承 `constraints`；`goal` 设为任务名，`done` / `todo` / `blockers` 清空。
5. `compact_upto = 0`、`summary = ""`：不继承父会话的摘要。
6. 末尾追加一条用户消息作为任务书：

```
[子 agent 任务]
你是从父会话「4HJO 对接」在 2026-10-22 14:32 分出的子 agent。
任务：只查 4HJO 的共晶配体与 4.5Å 内的口袋残基，输出成一张表。
父会话第 3–12 条消息已作为上文复制过来。
你不能再创建新的子 agent。完成后给出：结论、产出文件路径、未完成项。
```

7. 预算截断：按 `estimate_tokens` 累加，超过 `ContextAssembler.available(tools) * 0.6`
   就从最早的整条消息开始丢，并在开头插一条 `[说明] 更早的 N 条消息因上下文预算未复制`。
   以整条消息为单位，不拆工具调用与结果。

### 3. 状态机

```
idle ──host.start──> running ──正常结束──> done
                        │
                        ├── 用户中断 ─────> cancelled
                        ├── EXIT_ERROR ──> failed
                        └── 进程重启 ────> idle（带 status_note）
```

`AgentLoop` 增加只读属性 `last_exit_reason`，由 host 落状态，不改 `stream_reply` 返回签名。

---

## 三、技术架构

### 1. `core/agent/host.py`：AgentHost

不 import textual，可被测试直接驱动。

```python
class AgentHost:
    def loop_for(self, session_id: str) -> AgentLoop
    def spawn(self, parent, *, title, task, start, end, origin) -> Session
    def prompt(self, session_id: str, text: str, *, preamble: str = "") -> bool
    def run_bash(self, session_id: str, command: str) -> bool
    def cancel(self, session_id: str) -> None
    def is_running(self, session_id: str) -> bool
    def children_of(self, session_id: str) -> list[Session]
    def loops(self) -> list[AgentLoop]
    def shutdown(self) -> None
```

- 每个会话一个 `AgentLoop`，惰性创建并缓存。`cancelled` / `evidence` / `_pending_permissions`
  都是 loop 级状态，不能共用。
- 运行用 `asyncio.create_task`，不用 `ChatScreen.run_worker`：worker 绑屏幕生命周期，
  返回父会话时会把后台任务一起收掉。
- `prompt()` 把「建用户消息 → 解析文件引用 → 发 message.add/part.add → 起流」整体收进 host。
- `spawn()` 只创建数据，`prompt()` 负责开跑。

### 2. 事件路由

所有事件带 `session_id`：

| 事件 | 字段 |
| --- | --- |
| `part.add` / `part.update` | `session_id` |
| `message.add` / `turn.done` / `status.update` | 已有 |
| `agent.created` | `session_id, parent_id, title` |
| `agent.status` | `session_id, status, note` |

`ChatScreen` 每个 handler 开头按 `session_id` 过滤。这一句同时修掉三个隐患：
别人的 `turn.done` 把 `_busy` 置 false、`part.update` 去别的会话找 Part、
`permission.ask` 弹到错误的人头上。

### 3. ChatScreen 参数化

```python
ChatScreen(root=None, initial_prompt="", session_id: str | None = None)
```

- `session_id=None`：跟随 `session_manager.current`，根会话行为不变。
- `session` 属性按 id 取会话；`loop` 属性取 `host.loop_for(session.id)`。
- 所有 `self.app.session_manager.current` 换成 `self.session`，
  `self.app.agent` 换成 `self.loop`。
- `SubagentChatScreen(ChatScreen)`：`allow_spawn=False`、命令白名单、`ctrl+b` 返回。

`app.active_session`：栈顶是 ChatScreen 就用它的会话，否则用 current。
`/export` `/undo` `/redo` `/compact` `/task` `/effort` 全部改用它。

### 4. `spawn_agent` 工具

`core/agent/spawn.py`，与 `task.py` / `memory.py` 同级（内核工具）。

```python
class SpawnAgentTool(Tool):
    spawn_argument = "spawner"
```

依赖注入走现有声明式通道：`Tool.execute` 增加 `spawner_argument`，`AgentLoop` 持有
`self.spawner`，`_execute_tool` 时注入。工具不 import host、不 import app。

守卫：`session.depth >= 1` 直接 `ToolError`。参数 `history` 取 `all | recent | none`，
`recent` 默认 6 个交互轮。

### 5. 三层禁止再创建

| 层 | 做法 |
| --- | --- |
| 注册表 | 子会话 registry 不注册 `spawn_agent`，模型看不到 |
| 工具 | `depth >= 1` 返回 `ToolError` |
| 界面 | `allow_spawn=False`，命令面板过滤 `/agent` |

### 6. 完全共享的两个坑

**登记表**：子会话必须拿到**同一个 `SessionLedger` 对象**，不能各建一个指向父文件的实例
（两份内存序号会各自从 1 开始，后保存的覆盖先保存的）。`AgentHost` 按
`root_id or id` 缓存 ledger；`add()` / `save()` 外加 `threading.RLock` 兜底。

**模型配置**：`/model` 遍历 `host.loops()` 逐个 `reconfigure()`；之后新建的 loop
从 `app._provider_config()` 取当前值。

### 7. 右栏组件

`SidePanel` 里插入 `OptionList(id="side-agents")`，刷新时机：`on_mount`、
`agent.created` / `agent.status`、`on_screen_resume`。行文本 `Option(prompt, id=session_id)`，
选中回调 `app.open_subagent(session_id)`。标题按显示宽度截到 16 格加 `…`。

注意：现有判据断言侧栏打开后 `_written` 恰好是四个键，子 agent 段**不要**走 `_write`。

---

## 四、改动清单

| 文件 | 改动 |
| --- | --- |
| `core/models.py` | Session 加 9 个字段与序列化 |
| `core/session_manager.py` | `STATE_VERSION=2`；`create_session`（不切 current）；载入复位 running |
| `core/agent/host.py` | 新增：loop 缓存、spawn、prompt、cancel、children_of、共享 ledger |
| `core/agent/spawn.py` | 新增：`SpawnAgentTool` + fork 复制 |
| `core/agent/tools.py` | `Tool.execute` 加 spawner 通道 |
| `core/agent/loop.py` | `spawner`、`last_exit_reason`、事件带 session_id |
| `core/agent/prompt.py` | 子 agent 身份段（不写时间，保持前缀稳定） |
| `core/command_registry.py` | 加 `agent`、`back` |
| `app.py` | 装配 host；`agent`/`back` 命令；权限弹窗上移；`active_session`；模型扇出 |
| `screens/chat.py` | session_id/allow_spawn、事件过滤、右栏列表、发问走 host |
| `screens/subagent.py` | 新增 `SubagentChatScreen` |
| `screens/spawn_agent.py` | 新增创建模态 |
| `styles/app.tcss` | 右栏列表、banner、模态 |
| `core/web/routes.py` | 会话 JSON 增加 parentId/depth/status/origin/seedStart/seedEnd |
| `tests/test_agent.py` | 新增 subagent 检查组 |
| `tests/test_smoke.py` | 右栏与 banner 的布局断言 |

---

## 五、边界与风险

1. **state.toml 会变大**：父子会话写同一个文件，每次 `turn.done` 全量重写。短期可接受；
   超过约 20 个子会话或文件超过 5MB 时再考虑拆 `sessions/<id>.json`。
2. **两个 agent 同时 `turn.done`**：`save_to_toml` 是同步的，单事件循环下天然串行，
   不要挪进线程池。
3. **后台权限询问**：默认继承父会话的「本会话全部允许」，否则子 agent 会停在弹窗上；
   规则层（`AGENTS.md` / `permissions.toml`）不继承。
4. **上下文预算**：fork 长会话容易一创建就接近窗口上限，必须按 60% 阈值截断。
5. **中断语义**：`esc` 先中断、空闲时才返回；不要直接绑成返回。
6. **`isinstance(..., ChatScreen)`**：`SubagentChatScreen` 是子类，凡是「切到聊天界面」
   的命令必须在子 agent 界面被白名单挡掉。
7. **registry 不能共享**：根与子 agent 的注册表差一个 `spawn_agent`。

---

## 六、测试计划

`python tests/test_agent.py --only subagent`：

1. fork 正确性：区间端点、条数、ID 全新、过程标记被跳过、任务消息在末尾。
2. 深度守卫：子会话 registry 无 `spawn_agent`；直接调用返回失败。
3. 状态机：`idle → running → done`；中断 `cancelled`；载入把 `running` 复位。
4. 持久化往返：`save → load` 后字段不丢；旧版 state 能载入。
5. 事件隔离：父屏丢弃子会话的 `part.add`；`turn.done` 不把父屏 `_busy` 置 false。
6. 共享登记表：两个 loop 交替 `add()`，序号连续且快照里两条都在。
7. 并发发问：同一子会话第二次 `prompt()` 返回 False。
8. 命令白名单：子 agent 界面 `/agent` 被拒，`/back` 能弹栈。

---

## 七、实施顺序

- **P0**：Session 字段 + host 骨架 + `SubagentChatScreen` + 右栏列表 + `/agent` 模态。
- **P1**：事件带 session_id + 后台任务 + 权限弹窗上移 + `spawn_agent` 工具 + 深度守卫。
- **P2**：区间预算截断、状态机与重启恢复、共享 ledger 并发保护、web 字段、兄弟切换。

---

## 八、实现后补充（2026-10-22）

落地后补上的缺口：

1. **父会话取回结果的通道**：新增 `agent_status` 工具（仅根会话注册）。
   可看本会话派出的全部子 agent 或指定一个，`wait=true` 时最多等 60 秒；
   每条报告带状态、消息数、token、说明与最后一条助手结论（800 字截断）。
   它**不把子 agent 的全文塞进父会话**，要细节仍然进子 agent 界面看。
2. **删除会话的运行时清理**：新增 `app.delete_session()`，先记下子会话，
   删数据后逐一 `host.forget()` 并 `host.drop_family()`，正在跑的后台任务一起取消。
3. **会话选择器只列根会话**：重命名/删除后重新填充改用 `root_sessions()`，
   子 agent 不会再混进 `/sessions`。
4. **父会话派发记录**：用户派发（`origin=user`）时在父会话追加一条只含
   `PartType.AGENT` 的消息，消息区显示 `◆ 子 agent: <名字>`。
   翻译层只认 TEXT/FILE，所以它不进模型上下文；记录在 fork 之后追加，
   不会被复制进子会话。模型派发仍以工具卡为准。
5. **管理入口**：`/agents` 把焦点交给右栏列表（↑↓ 选择、enter 打开、
   esc 回输入框）；`/rename <名字>` 改当前会话；`/delete` 确认后删除
   当前会话（根会话会提示子 agent 一起删）。子 agent 界面里这两条命令
   作用在子 agent 自己身上。
6. **权限询问排队**：`app._permission_queue` 按到达顺序逐个弹，答完一个再弹下一个；
   失效的询问（会话已删、工具已被中断或拒绝）在出队时跳过。
7. **子 agent 不能切 Build/Plan**：`EditorPane.action_agent_popup` 检查
   `screen.allow_agent_switch`，子 agent 界面为 False；事件处理里再兜底拒绝一次。
8. **失败原因上屏**：`AgentHost` 把 `EXIT_*` 映射成中文写进 `status_note`，
   右栏第二行与子 agent 返回栏都显示它，不再只有一个「失败」。
9. **网页端父子缩进与状态点**：`web/pages/history.js` 按 `parentId` 把会话重建成树，
   根会话按接口顺序、子 agent 按 `createdAt` 紧跟在父行下面；子行带状态点
   （运行/完成/失败/中断/空闲）与状态文字，父行带「N 子」计数。
   详情头部显示「子 agent · 父会话（可点）」、状态、来源与 fork 区间。
   过滤时保留命中节点的祖先，`tools/check_web_ui.js` 的期望条数与顺序改成树序。
10. **收尾**：子 agent 界面改用命令白名单（`ALLOWED_COMMANDS`），新增命令默认不出现；
    返回栏补上派发来源与 fork 区间；`check_subagent` 补了两个会话同时运行、
    同会话第二次发问被拒、取消落 `cancelled` 的判据；`check_subagent_ui` 补了
    右栏宽度 30、返回栏高度 1、事件按 `session_id` 过滤的判据。
11. **取回结果的细节与提醒**：`agent_status` 每条报告多列「产出」（会话里出现过的
    FILE/PATCH 路径，最多 5 个）；后台子 agent 结束（完成/失败/中断）时，
    父会话或兄弟界面弹一条 toast；父会话里的 `◆ 子 agent: ...` 标记可点击，
    直接用 `Part.text` 里的会话编号打开对应界面。
12. **右栏第三行与派发区间判据**：右栏每条子 agent 从两行改成三行，第三行写
    `第 N–M 条 · 用户/模型派发`（空区间写「无父会话历史」）；行高 2→3。
    补 `recent_start`、`spawn_agent` 的 `history=recent` 区间、`agent_status`
    的 `wait` 超时路径三组判据。
13. **网页列表改为可折叠树**：父会话标题前有 `▾/▸` 按钮（`aria-expanded`、
    `role=treeitem`），收起后子树不渲染；过滤时强制展开命中路径，
    当前选中的子 agent 的祖先也不会被折叠藏起来。标题文字单独包在
    `.session-name` 里，`check_web_ui.js` 的过滤比较不再被按钮与计数徽标干扰。
