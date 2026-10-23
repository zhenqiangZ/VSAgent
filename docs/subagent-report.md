# 子 Agent 派发：实施汇报

日期：2026-10-23
范围：从设计到落地、判据与网页端展示的完整一轮工作。

---

## 一、任务背景与确认的前提

目标是在 TUI 的聊天界面里派发「子 agent」，它有三个与常见实现不同的约束。
开工前确认了三个前提：

1. **「指定某个时间段」= 父会话的一段消息区间**（第 N 条到第 M 条），
   这段内容复制成子 agent 的初始上下文。
2. **后台并行运行**：派发后子 agent 自己跑，父会话可以继续提问。
3. **完全共享**：工作目录、`runs/`、登记表（ledger）与父会话共用。

最终落地为：**一个子 agent = 一条独立持久会话 + 一个独立 `AgentLoop` +
一条独立 asyncio 任务**，深度硬限制 1 层（根会话能派发，子 agent 不能）。

---

## 二、交付总览

- 新增 4 个 Python 文件（`host.py`、`spawn.py`、`subagent.py`、`spawn_agent.py`），
  共约 1100 行。
- 修改 18 个既有文件（核心装配、数据模型、聊天界面、网页、判据）。
- 设计文档 `docs/subagent-plan.md` 记录方案与 13 条落地补充。
- 子 agent 相关的 95 项判据全部通过；完整 `tests/test_smoke.py` 退出码 0。

---

## 三、按层交付明细

### 3.1 数据与持久化

`core/models.py` 的 `Session` 新增：

| 字段 | 含义 |
| --- | --- |
| `parent_id` | 父会话 id；根会话为空，也是「是不是子 agent」的判据 |
| `root_id` | 根会话 id，登记表按它共享 |
| `depth` | 0=根，1=子 agent |
| `origin` | `user` / `agent`，谁派发的 |
| `seed_start` / `seed_end` | fork 的父会话消息区间 `[start, end)` |
| `status` | `idle / running / done / failed / cancelled` |
| `status_note` | 失败或中断的可读原因 |
| `spawned_at` | 右栏按时间排序用的创建时间 |

- `STATE_VERSION` 从 1 提到 2，字段全部带默认值，旧状态文件照常读。
- 载入时把 `running` 复位成 `idle`，写「上次退出时仍在运行」。
- `SessionManager.create_session()` 创建但不切换 current；新增 `children_of()` /
  `root_sessions()`；删除会话级联删除子 agent。
- 所有会话仍在同一份 `state.toml` 里，网页端按 `parentId` 重建树。

### 3.2 运行时：`core/agent/host.py`

`AgentHost` 是这次新增的核心：

- 每个会话一个 `AgentLoop`，惰性创建并缓存。
  循环层里的 `cancelled` / `evidence` / 待答复判定都是会话级状态，不能共用。
- 后台任务用 `asyncio.create_task`，不绑屏幕生命周期；用户返回父会话后子 agent 继续跑。
- 登记表按 `root_id` 共享同一个 `SessionLedger` 对象——不是两个指向同一文件的实例，
  否则两份内存序号会各自从 1 开始、后保存覆盖先保存。
- 提供 `spawn / prompt / run_bash / cancel / wait / is_running / children_of /
  forget / drop_family / shutdown`。
- 状态机：`idle → running → done / failed / cancelled`；
  中断时先落状态再取消任务，避免任务还没被调度就取消导致状态永远停在 running。
- `forget` 清循环层缓存并取消后台任务；`drop_family` 丢共享登记表。

`app.py` 把「一个全局 AgentLoop」改成 `host.loop_for()`；
保留 `app.agent` 属性兼容既有调用点（`/tools` 面板、权限判定、测试）。
模型切换与连接配置变更会扇出到所有已建循环层。

### 3.3 派发与查询工具

`core/agent/spawn.py`：

- `fork_messages`：复制区间内消息，**消息与 Part 全部重新生成 ID**；
  跳过 `STEP_START / STEP_FINISH / RETRY / COMPACTION`；
  按可用上下文 60% 截断，从最早整条消息开始丢，至少留一条。
- `task_preamble`：生成子 agent 的任务书（来源、区间、不能再派发、共享目录与
  handle、交付要求）。任务书写成用户消息，只出现在子会话第一轮。
- 继承规则：只继承父会话的 `task_state.constraints`；`goal` 设为子 agent 名字；
  `done / todo / blockers` 清空；`summary / compact_upto` 不继承。
- `SpawnAgentTool`（`spawn_agent`）：参数 `title / task / history=all|recent|none /
  recent_turns`，立即返回，不阻塞父会话。
- `AgentStatusTool`（`agent_status`）：父会话取回结果的通道。
  可看全部或指定一个子 agent；`wait=true` 最多等 60 秒；
  报告含状态、消息数、token、说明、最后一条助手结论（800 字截断）与产出文件
  （`FILE` / `PATCH` 路径，最多 5 个）。
- `history_end` 会排除「正在写的那条派发消息」，避免把没有结果的工具调用复制进子会话。

工具注入走既有声明式通道，新增两个：

- `session_argument`：注入当前 `Session`（派发要读 `depth`）；
- `spawn_argument`：注入 `AgentHost`（派发与查询共用）。

`AgentLoop` 同时补了 `last_exit_reason`，供 host 区分 `done / failed`。

### 3.4 TUI 界面

**根会话**

- `/agent` 打开派发模态：名称、消息区间（1 起）、首尾预览、预算提示、任务书。
  `/agent <任务>` 用整段历史直接派发。
- 创建后右栏自动出现，条目三行：
  ```
  ● 靶点调研
    运行中 · 12 条 · 14:32
    第 3–12 条 · 用户派发
  ```
  状态图标：`●` 运行、`○` 空闲、`✓` 完成、`✕` 失败、`‖` 中断；
  失败时第二行显示原因而不是条数。
- 用户派发会在父会话追加一条只含 `PartType.AGENT` 的消息，显示
  `◆ 子 agent: 名字`。它不进模型上下文（翻译层只认 TEXT/FILE），
  在 fork 之后追加，不会被复制进子会话；**点击可直接进入该子 agent**。
- 后台子 agent 结束（完成/失败/中断）时，父会话或兄弟界面弹 toast。

**子 agent 界面**

- 顶部返回栏：`◀ 返回  子 AGENT · 名字  父会话：标题  ● 状态 · 原因 · 来源 · 区间`。
  整条可点，`ctrl+b` / `/back` 返回；`esc` 运行中先中断、空闲时返回。
- 不能派发、不能开新会话、不能切会话：命令使用**白名单**，新增命令默认不出现。
- 不能切 Build/Plan：`allow_agent_switch=False`，`tab` 不弹 Agent 列表，
  事件处理里再兜底拒绝一次。
- 右栏显示兄弟 agent，可平级跳转；返回一层退一层。

**命令与输入**

- 新增 `/agent`、`/agents`（聚焦右栏列表，↑↓ 选择、enter 打开、esc 回输入框）、
  `/back`、`/rename`、`/delete`（确认后连运行时一起删）。
- 权限询问上移到应用层，按到达顺序排队，答完一个再弹下一个；
  失效询问（会话已删、工具已中断/拒绝）出队时跳过。
- 所有事件带 `session_id`，聊天界面按会话过滤；后台流不会串进父会话，
  `turn.done` 也不会把父屏的 `_busy` 置错。

### 3.5 网页端

- `session_summary` 增加 `parentId / depth / origin / status / statusNote /
  seedStart / seedEnd`。
- 历史页列表改为**可折叠树**：
  - 父行标题前 `▾ / ▸`，`aria-expanded`、`role=treeitem`、`aria-level`；
  - 子 agent 缩进挂在父行下，行首状态点与状态文字；
  - 父行右侧 `N 子` 计数；
  - 过滤时保留命中节点的祖先并强制展开；
  - 当前选中子 agent 的祖先不会被折叠藏起来。
- 详情头部：`子 agent · 父会话 <可点>`、状态、来源、说明、fork 区间。
- `agent` 类型 Part 文案改为「子 agent …」。
- `tools/check_web_ui.js` 的列表顺序期望改为树序，过滤期望包含祖先，
  并新增折叠/展开判据。

---

## 四、关键设计决策与理由

1. **独立 `AgentLoop` 而不是共享一个**：`cancelled`、`evidence`、
   待答复 future 都是会话级状态，共用会互相踩。
2. **后台任务由 host 持有，不用 Textual worker**：worker 绑屏幕生命周期，
   用户从子 agent 界面返回时会把任务一起收掉。
3. **登记表共享同一个对象**：满足「完全共享」前提，同时避免两份内存序号冲突。
4. **权限策略每个循环层一份**：规则共享，但「本会话全部允许」不能漏到后台子 agent。
5. **子 agent 不能再派发用三层拦截**：注册表不装工具、工具检查 `depth`、
   界面白名单。只做一层都不够——模型可能从历史里学到工具名，用户可能绕过面板。
6. **用户派发记录只用于显示**：AGENT Part 不进上下文，避免白占窗口；
   模型派发以工具卡为记录。
7. **结果不自动灌进父会话**：`agent_status` 只取短报告，全文留在子会话，
   避免子 agent 的长输出吃掉父会话上下文。

---

## 五、并发与一致性

- 同一事件循环里多个 `asyncio.Task` 并行；所有共享写入（状态文件、登记表）
  都是同步调用，天然串行，不引入线程。
- 同一会话第二个 `prompt()` 在运行期间返回 `False`，界面提示先中断。
- 取消只作用于目标会话；`_run` 的 `finally` 一定落状态，异常也会落 `failed`。
- 删除会话先记下子会话，删数据后逐一 `host.forget` 并 `drop_family`，
  后台任务一起取消。
- 已知规模风险：`state.toml` 每次 `turn.done` 全量重写，子 agent 很多时会变大。
  目前的量级可接受，超过约 20 个子会话或文件超过 5MB 时建议拆分会话分片。

---

## 六、判据与验证

- `python tests/test_smoke.py` → `EXIT:0`，`AssertionError` 0 处。
- `python tests/test_agent.py --only subagent` → **95 项检查全部通过**，覆盖：
  - fork 区间、ID 全新、过程标记过滤、预算截断、任务书；
  - `recent_start` 与 `history=recent` 的实际区间；
  - 深度守卫（注册表 / 工具 / 界面）；
  - 状态机、重启复位、删除清理（数据、循环层、ledger）；
  - 两个会话真正并行、同会话第二次发问被拒、取消落 `cancelled`；
  - `agent_status` 的报告、产出文件、`wait` 超时与完成；
  - 界面派发、右栏、标记点击、`/agents` 焦点与 esc、权限排队、
    失败原因、tab 不切 Build/Plan、重命名/删除、会话选择器只列根会话、
    右栏宽度 30、返回栏高度 1、事件按 `session_id` 过滤。
- `python tools/check_web_api.py` → 20/20 通过。
- 网页端用真实 Chromium 独立验证：父子缩进、状态点颜色、详情父会话入口、
  折叠/展开与 `aria-expanded`、点击折叠按钮不会选中会话。
  （完整 `tools/check_web_ui.js` 因本机缺 `pngjs` 未跑。）

---

## 七、过程中发现并修复的问题

1. `host.loop_for("")` 在状态载入前按空串缓存循环层，导致同一会话出现两个实例，
   测试打给 `app.agent` 的补丁落到旧实例上。
2. `loop._fail_tool` 在批量补 `session_id` 时引用了不存在的 `session`，改为
   `message.session_id`。
3. `/sessions` 重命名/删除后重新填充用 `list_sessions()`，子 agent 混进会话列表。
4. 空会话派发时把 0 起点又减 1，`seed_start` 存成 `-1`，界面显示「第 0–0 条」。
5. 删除会话只删数据，后台任务与缓存的循环层、ledger 仍活着。
6. 权限弹窗直接 `push_screen`，多个后台 agent 同时询问时模态叠加。
7. 子 agent 界面仍能按 `tab` 切 Build/Plan。
8. 一处测试导入被误拼进注释（`POST_TOOL_USE`），导致 `check_hook_config` NameError。
9. 旧的 `home-session` 判据假设全应用只有一个 loop，改为给工厂注入假流。

---

## 八、已知限制与风险

1. **重启后不自动续跑**：`running` 复位成 `idle`，需要进入界面再发一句。
   这是有意的（避免启动即花钱），但「持久化 agent」容易被误解为自动继续。
2. **深度硬限 1 层**，不支持孙 agent。
3. **`agent_status` 的产出清单**只从会话里的 `FILE` / `PATCH` 推断，
   没有把 ledger 条目按子 agent 归属区分。
4. **TUI 右栏宽度 30 列**：第三行已放区间与来源，更长信息放不下；
   完整信息在子 agent 返回栏与网页详情里。
5. **权限队列 FIFO**：没有按「用户当前所在会话」优先。
6. **登记表没有显式锁**：单事件循环下安全；将来若用线程驱动需要补锁。
7. **网页详情不内嵌子 agent 对话**：列表是树，详情只给父会话入口；
   子 agent 的完整对话需要点进去看它自己的页面。
8. **完整网页判据未在本机跑**：缺 `pngjs`，用独立 Chromium 脚本替代验证。

---

## 九、后续建议

优先级从高到低：

1. **可选续跑**：新增 `/resume`，在子 agent 界面手动让它接着上次的任务跑一轮；
   默认仍不自动续跑。
2. **网页详情内嵌子树**：在父会话的派发位置展开子 agent 的完整消息，
   而不是只能跳到子页面。
3. **结构化产出**：`agent_status` 从 ledger 里取该子 agent 登记的
   receptor / ligandset / artifact，按 handle 列出。
4. **状态文件分片**：子 agent 数量上来后把 `state.toml` 拆成索引 + 每会话一份。
5. **权限优先级**：当前屏幕所属会话的询问排到队首。

---

## 十、文件清单

新增：

| 文件 | 作用 |
| --- | --- |
| `opencode_tui/core/agent/host.py` | 每会话循环层、后台任务、共享 ledger、状态机 |
| `opencode_tui/core/agent/spawn.py` | fork、任务书、`spawn_agent`、`agent_status` |
| `opencode_tui/screens/subagent.py` | 子 agent 聊天界面与返回栏 |
| `opencode_tui/screens/spawn_agent.py` | 派发模态 |
| `docs/subagent-plan.md` | 设计与 13 条落地补充 |

主要修改：

- 核心：`models.py`、`session_manager.py`、`agent/loop.py`、`agent/tools.py`、
  `agent/prompt.py`、`app.py`、`command_registry.py`、`web/routes.py`
- 界面：`screens/chat.py`、`screens/session_picker.py`、
  `widgets/messages_pane.py`、`widgets/editor_pane.py`、`styles/app.tcss`
- 网页：`web/pages/history.js`、`web/app.css`
- 判据：`tests/test_agent.py`、`tests/test_smoke.py`、`tools/check_web_ui.js`

---

## 附：使用方式

```
/agent                  打开派发模态（选区间、写任务）
/agent 只查 4HJO 的共晶配体   用整段历史直接派发
/agents                 聚焦右栏子 agent 列表（↑↓ / enter / esc）
/back                   从子 agent 返回（也可 ctrl+b 或点顶部栏）
/rename 新名字           重命名当前会话或子 agent
/delete                 删除当前会话（确认后连子 agent 与运行时一起删）
```

模型侧：`spawn_agent` 派发，`agent_status` 查进度/等结果/看产出。

网页端：`#/history` 左侧是可折叠树，父行可收起，子行带状态点。
