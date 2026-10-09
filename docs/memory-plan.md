# 记忆功能实施计划（Memory）

> 配套文档：`memory-design.md`（设计）、`context-design.md`（上下文分层）。
> 本计划只讲**怎么实施**：分期、任务、依赖、验收、回归。

---

## 一、目标与范围

**目标**：把 `memory-design.md` 的分期方案写成代码，**先交付第 1 期最小可用**。

**本期范围：只做 `FACT`（项目事实）**
- 项目级记忆：`<workspace_root>/.vsmemory/`
- `MemoryStore` 数据结构与存储（含指纹校验）
- 两个工具：`remember` / `recall`
- 三个命令：`/remember` / `/forget` / `/memory`
- 上下文接入：`MEMORY` 层注入

**明确不做**（打分与筛选尚未实施，依赖它们的类型一并缓做）
- `PREFERENCE` / `RUN` / `RECIPE` 三种记忆类型（设计保留在 `memory-design.md` 第十一节）
- 用户级记忆（跨项目取向）
- 向量检索、语义召回
- 静默自动抽取记忆
- 子代理

---

## 二、前置约束（项目约定，必须遵守）

| 项 | 约定 |
| --- | --- |
| 解释器 | 跑应用、跑断言、跑判据一律 `.venv/Scripts/python.exe`；打包、抓帧用 pack python |
| 改动后流程 | `test_agent.py` → `test_smoke.py` → `check_banned_chars.py` → 判据四段 → 打包 → 成对抓帧 |
| 命令单一真相 | `opencode_tui/core/command_registry.py`（命令名、别名、说明都在这里） |
| 原子写 | 复用 `SessionManager.save_to_toml` 的临时文件 + `fsync` + `os.replace` + 重试模式 |
| 新增界面元素 | 若引入新的 Part 类型或视图，要同步多处白名单（见 `memory-design` 之外的既有约定） |
| 判据 | 放 `.probe/`，自检要能证明判据本身有效 |
| 跑判据前 | 先查孤儿进程，避免陈旧锁与残留状态文件造成假失败 |

---

## 三、设计要点回顾（决定实现形状）

1. **存路径 + 指纹，不存 handle**——跨会话唯一可用的标识。复用 `ledger.py` 已有的指纹与 `ok / stale / missing` 语义。
2. **本期只有 `FACT` 一种类型**（`PREFERENCE` / `RUN` / `RECIPE` 缓做）。
3. **两条注入路径**：常驻（`MEMORY` 层，≤2k）与按需（`recall` → `EVIDENCE` 层）。
4. **写入必须显式**：工具 / 命令 / run 结束提议，不做静默抽取。
5. **规则优先**：`AGENTS.md` 压过记忆。

---

## 四、分期计划

### 第 0 期 · 定稿（决策）

**定稿结论（2026-10-07）**：

| 决策点 | 结论 |
| --- | --- |
| 存储位置 | 项目级 `<workspace_root>/.vsmemory/` |
| 是否进 git | 提交（记忆随项目走，团队可共享） |
| 常驻上限 | 2k（`RESIDENT_LIMIT = 2000`） |
| 命令名 | `/remember`、`/forget`、`/memory` |
| 索引文件 | 本期不做 `index.json`，`recall` 直接扫内存；条目过千再加 |

### 第 1 期 · 最小可用（核心，本计划主体）

任务清单见 `TaskList`。分四组：

**A. 数据与存储（M1-1 ~ M1-4）**
- 新增 `opencode_tui/core/agent/memory.py`
- `MemoryKind` / `MemoryEntry` / `MemoryStore`
- 存储：`entries.json`（真相）+ `memory.md`（人读）双份，`save()` 保持一致
- 原子写：临时文件 + `fsync` + `os.replace` + 重试
- 指纹：`revalidate()` 重算 `refs` 指纹 → `ok / stale / missing`

**B. 工具与命令（M1-5 ~ M1-6）**
- `RememberTool`（`remember`）、`RecallTool`（`recall`），注册进 `ToolRegistry`
- `/remember` `/forget` `/memory` 加进 `command_registry.py`（含别名与说明）

**C. 上下文接入（M1-7 ~ M1-8）**
- `build_model_messages(...)` 增 `memory_text` 参数：非空时在 system 之后插一条 user 头
- `memory_text` 计入预算（`total` 里加 `estimate_tokens(memory_text)`）
- `app.py` 装配 `MemoryStore`；`loop.py` 装配处传 `self.memory.resident_text(RESIDENT_LIMIT)`

**D. 测试与文档（M1-9 ~ M1-12）**
- `tests/test_agent.py`：循环层用例（写入 → 重载 → 召回；指纹变化 → stale；预算计入）
- `tests/test_smoke.py`：界面层用例（命令入口）
- `.probe/check_memory.py`：判据脚本（含自检）
- 文档：`docs/source-map.md`、`HARNESS.md` 同步

**第 1 期验收判据**
1. 写入一条记忆，**重开会话后** `recall` 能取到（跨会话可用）。
2. 常驻记忆出现在请求头部，且**不超过上限**；超限时按「核心事实 > 旧条目」淘汰。
3. 修改被引用的文件后 `revalidate()` 把该条目标成 `stale`。
4. 记忆里**不出现 handle**（判据扫描 `receptor#` 一类字符串）。
5. 既有测试全部不回归（`test_agent` / `test_smoke`）。

### 第 1 期 · 实施结果（2026-10-07 完成）

M1-1 ~ M1-11 已实施并验证：

| 项 | 位置 | 验证 |
| --- | --- | --- |
| 数据结构与存储 | `opencode_tui/core/agent/memory.py` | `check_memory` 往返、对账、原子写 |
| 工具 | `RememberTool` / `RecallTool` | `check_memory` 工具段 + 装配断言 |
| 命令 | `/remember` `/forget` `/memory` | `test_smoke` 命令段（含多条命中、按编号删） |
| 上下文接入 | `prompt.build_model_messages(memory_text=...)` | `check_prompt` 记忆段（含预算计入） |
| 装配 | `app._build_memory` / `loop._memory_text` | `check_prompt` 与 `check_memory` |
| 判据 | `.probe/check_memory.py` | 已登记进 `run_all_checks.sh` |
| 文档 | `source-map.md` §3.8 / `HARNESS.md` §11 | 章节与计数已同步 |

读数：`test_agent` 1106 项、`test_smoke` 1626 项全绿；`check_banned_chars` 0 处；
`check_memory` 退出码 0（自检：把对账判定换成永远 ok，变化就报不出来）。

**比原计划多做的一处**：写入时拦住 handle 引用。原计划只把这条写进纪律、靠判据扫描；
现在 `MemoryStore.remember` 借 `ledger.parse_handle` 判定，命中就抛 `MemoryError`，
工具层再转成一句可读失败回灌给模型。判据另有一份独立复算的扫描，两份不共用实现。

### 第 2 期 · 校验强化

- 注入时对 `stale` 条目带一行提醒
- 同一 `refs` 上新旧事实冲突检测 → 报用户裁决，不静默覆盖
- 判据

> 打分与筛选实施后再启用 `PREFERENCE` / `RUN` / `RECIPE`，届时按 `memory-design.md` 第十一节同步改动。

---

## 五、依赖与执行顺序

```
M1-1 数据结构
  └─ M1-2 存储层
       └─ M1-3 指纹与 revalidate
            ├─ M1-4 remember/recall 工具
            │    └─ M1-5 命令
            └─ M1-6 上下文接入
                 └─ M1-7 装配（app/loop）
                      └─ M1-8 循环层测试
                           └─ M1-9 界面层测试
                                └─ M1-10 判据脚本
                                     └─ M1-11 文档同步
```

**可并行**：M1-5（命令）与 M1-6（上下文接入）在 M1-4 之后可并行。
**串行关键路径**：M1-1 → M1-2 → M1-3 → M1-4 → M1-6 → M1-7 → 测试链。

---

## 六、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 记忆污染上下文 | 挤掉关键历史 | 常驻硬上限 2k；纳入统一预算；降级顺序先砍旧事实 |
| 记忆过期误导 | 基于旧事实继续 | 指纹校验 + `stale` 提醒（第 2 期补注入提醒） |
| 原子写被占用 | 写失败 | 复用既有重试模式（40 次 × 5ms） |
| 工具/命令名冲突 | 注册失败或歧义 | 实施前先查 `registry` 与 `command_registry` 现有名字 |
| 跨会话标识失效 | 记忆指向错对象 | 只存路径 + 指纹，禁止 handle（判据扫描） |
| 测试回归 | 既有功能破坏 | 严格走固定流程；先跑 `test_agent` 再跑 `test_smoke` |

---

## 七、里程碑

| 里程碑 | 内容 | 交付物 |
| --- | --- | --- |
| M-A | 数据与存储可用 | `memory.py` + 单元级验证 |
| M-B | 工具与命令可用 | `remember` / `recall` + 三个命令 |
| M-C | 接进上下文 | `MEMORY` 层注入生效 |
| M-D | 第 1 期完成 | 全部判据通过 + 文档同步 + 打包抓帧 |

---

## 八、一句话

> 先交付「项目级记忆 + 两个工具 + 一个注入层」，把跨会话不再从零开始这件事做实；
> 指纹校验是记忆可信的底线，必须在第 1 期就带上，不能留到以后。
