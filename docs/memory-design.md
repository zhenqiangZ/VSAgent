# 虚拟筛选 Agent 的记忆设计（Memory）

> 目标：给项目沉淀跨会话的事实，让下一轮筛选不必从零开始。
> 本文与 `context-design.md` 配套：记忆是**来源**，上下文是**产物**，记忆通过第七节接入上下文分层。
>
> **本期范围：只做 `FACT`（项目事实）。** 打分与筛选流程尚未实施，依赖它们的
> `PREFERENCE`（打分/过滤器取向）、`RUN`（每轮筛选记录）、`RECIPE`（流程模板）
> **暂不做**，设计保留在文末「预留」一节，等打分与筛选实施后再启用。

---

## 一、先划清边界：Memory 不是什么

你已经有三样东西，很容易和 Memory 混在一起。先分清：

| 概念 | 回答的问题 | 生命周期 | 存放 | 现有实现 |
| --- | --- | --- | --- | --- |
| `AGENTS.md` | 你**必须怎么做** | 人工维护 | 项目根 | `prompt.project_rules` |
| `TaskState` | 这一轮**要做什么**、约束是什么 | 会话内 | `Session` | 见 `context-design.md` |
| `Ledger` | 这次会话**我手上有哪些产物** | 会话内 | `~/.opencode_tui/ledgers/` | `ledger.py` |
| **Memory** | 这个项目**已知什么** | **跨会话** | 项目 | 本文设计 |
| 上下文 | 这一步**发给模型什么** | 一步 | 内存 | `context-design.md` |

**一条必须记住的坑**：Ledger 的 `receptor#1` 是**会话内**编号，跨会话没有意义。记忆里**不能存 handle**，要存**稳定的东西**——文件路径 + 内容指纹。Ledger 已经有指纹与「文件状态（ok / stale / missing）」的概念（`ledger.py:29`），记忆层直接复用这套判断。

---

## 二、记忆的范围（scope）

本期**只做项目级**。用户级（跨项目取向）属于 `PREFERENCE`，随本期一并缓做。

| 范围 | 存什么 | 位置 | 随谁走 |
| --- | --- | --- | --- |
| 会话内 | 当前任务状态 | `Session.task_state` | 会话 |
| **项目级**（本期） | 这个筛选项目的受体、位点、配体库 | `<workspace_root>/.vsmemory/` | 项目（可进版本库） |

项目级放 `<workspace_root>/.vsmemory/` 的理由：**VS 的记忆天然绑定项目数据**（哪个受体、哪个库），跟项目目录一起走、一起备份、一起进 git，最省心。

---

## 三、记忆的类型（kind）

本期**只有一种**：

| kind | 回答 | VS 例子 | 注入方式 |
| --- | --- | --- | --- |
| `FACT` | 项目里的事实 | 受体 4.2Å 已准备，产物在 `prep/4abc.pdbqt` | 核心常驻 + 详细检索 |

**核心事实 vs 详细事实**：受体清单、配体库清单一类属于"每轮都用得到"，常驻注入；某个受体的完整准备日志属于"要用才取"，检索注入。

> 枚举只放 `FACT` 一个值，不留空占位。后续启用 `PREFERENCE` / `RUN` / `RECIPE` 时再扩枚举（见第十一节）。

---

## 四、`FACT` 具体记什么（本设计的重点）

只记**打分与筛选之前就存在**的东西。

**受体**
- PDB ID、链、分辨率、来源（RCSB / 本地）
- 准备参数：pH、加氢、去水、去配体、力场、保留的辅因子
- 产物：路径 + 指纹（文件变了要能发现）

**结合位点**
- box center / size，或参考配体
- 位点来源（文献 / 共晶配体 / 预测）

**配体库**
- 名称、来源（ChEMBL / ZINC / 自建 / 客户提供）
- SMILES / SDF 路径 + 指纹
- 原始条数、去重后条数、跑过的过滤器
- 已知问题（比如"含大量盐、需先脱盐"）

**参考活性物**（有则记）
- 化合物标识（SMILES / ChEMBL ID）、活性值、单位、来源

> **不记**：打分函数与参数、命中阈值、排序依据——那些属于打分环节，等打分实施后进 `PREFERENCE`。

---

## 五、数据结构

新增 `opencode_tui/core/agent/memory.py`（设计稿）。

```python
class MemoryKind(Enum):
    FACT = "fact"
    # 预留：PREFERENCE / RUN / RECIPE，待打分与筛选实施后再加


@dataclass
class MemoryEntry:
    """一条记忆。

    跨会话可用是它的全部意义，所以引用的必须是稳定标识：
    `refs` 里放**路径**（配 `fingerprint`），**不放会话内的 handle**。
    """

    id: str
    kind: MemoryKind
    text: str                       # 一行事实
    refs: list[str] = field(default_factory=list)      # 文件路径
    fingerprint: str = ""           # refs[0] 的内容指纹，读取时校验
    source: str = ""                # "user" | "agent"
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0         # 0 表示不过期
    state: str = "ok"               # ok / stale / missing，复用 Ledger 的文件状态语义

    def is_live(self, now: float) -> bool:
        """还没过期、且引用仍然有效时才算可用。"""
        ...
```

```python
class MemoryStore:
    """项目级记忆的读写与检索。"""

    def __init__(self, workspace_root: Path) -> None: ...

    def load(self) -> None: ...
    def save(self) -> None: ...

    def remember(self, entry: MemoryEntry) -> MemoryEntry: ...   # 显式写入
    def forget(self, entry_id: str) -> bool: ...
    def recall(self, query: str) -> list[MemoryEntry]: ...

    def resident_text(self, limit: int) -> str:
        """常驻注入的正文：核心 FACT，受 token 上限约束。"""
        ...

    def revalidate(self) -> list[MemoryEntry]:
        """重算指纹，把变化过的条目标成 stale / missing。"""
        ...
```

---

## 六、存储布局

```
<workspace_root>/.vsmemory/          # 项目级：跟着筛选项目走
  memory.md            # 常驻，人可读、可直接编辑（核心事实）
  entries.json         # 结构化条目（含 refs / 指纹 / 状态）
```

**为什么 `memory.md` 与 `entries.json` 并存**：`memory.md` 给人看、给人改（像 `AGENTS.md` 一样自然）；`entries.json` 给程序读，带指纹与状态。两者由 `MemoryStore.save` 保持一致，以 `entries.json` 为真相。

**本期不做检索索引文件**：条目量级是几十到几百，`recall` 直接扫内存即可；等过千再引入索引或向量检索。

---

## 七、怎么接进上下文分层（对接 `context-design.md`）

在分层里**插入一个 `MEMORY` 层**：

```
PREFIX → MEMORY → TASK → RECENT → EVIDENCE → HISTORY
```

| 层 | 内容 | 策略 |
| --- | --- | --- |
| `MEMORY` | 常驻记忆（核心 FACT） | pinned，但受 token 上限 |
| `EVIDENCE` | **检索回来的记忆**（详细 FACT） | 与其他证据同层，带 ref 可追溯 |

- 常驻记忆**注入到 `TASK_HEADER`** 里，紧跟在任务状态之后，这样"约束"与"已知事实"挨着。
- 检索回来的记忆走 `EVIDENCE` 层——它和"本步取回的文件片段"是同一种东西：**按需取回的材料**，带引用、可追溯。

**降级顺序更新**（在原 4.3 基础上）：

```
1. 收 EVIDENCE 的 detail（含检索记忆的正文）
2. 缩 HISTORY 摘要
3. 缩 MEMORY 常驻段        ← 新增：先砍旧的核心事实
4. 移出最旧的 RECENT 交互组
5. 压 RECENT 组内的大块输出
（PREFIX / TASK 永不丢）
```

---

## 八、读写与生命周期

**写（两条入口）**
1. **Agent 显式**：`remember(text, refs)` 工具。禁止静默自动写——自动抽取的记忆错了比没有更糟。
2. **用户显式**：`/remember`、`/forget` 命令。

**读（两种方式）**
- **常驻**：`resident_text()` 在装配时注入，内容 = 核心事实，**有 token 上限**（建议 ≤ 2k），超了按「核心 > 旧条目」淘汰。
- **按需**：`recall(query)` 关键词检索。**先不引入向量库**——条目量级是几十到几百，关键词 + 结构化过滤足够；等真的过千再谈语义检索。

**失效（三把闸）**
- **指纹校验**：`revalidate()` 重算 `refs` 的指纹，变了标 `stale`，注入时带一行提醒（"该受体文件已变更，记忆可能过期"）。
- **过期**：`expires_at` 到点不再注入。
- **撤销**：`forget(id)` 直接删。

---

## 九、护栏（VS 场景尤其重要）

1. **规则优先**：`AGENTS.md` 覆盖任何记忆。记忆与规则冲突时以规则为准，并提示用户。
2. **不存 handle，只存路径 + 指纹**：这是本设计最重要的一条，理由见第一节。  
   已实施为**写入时拦截**：`MemoryStore.remember` 借 `ledger.parse_handle` 判定 `refs`，  
   命中就抛 `MemoryError`，工具层再转成一句可读失败回灌给模型；判据另有一份独立复算的扫描。
3. **指纹校验**：受体文件被重新准备过，旧记忆必须能发现。复用 Ledger 的 `ok / stale / missing`。
4. **冲突检测**：同一 `refs` 上的新旧事实冲突（比如两处记了不同的 box 尺寸）时，报给用户裁决，不静默覆盖。
5. **预算上限**：常驻记忆有硬上限，防止记忆本身把上下文挤爆。
6. **不存凭据、不存大块数据**：记忆存指针（路径），不存结构文件内容、不存 API key。
7. **可撤销**：任何一条记忆用户都能 `forget`。

---

## 十、分期

| 期 | 内容 | 价值 |
| --- | --- | --- |
| **1** | 项目 `memory.md`（核心事实）+ `remember` / `recall` 工具 + 指纹校验 | **最小可用**：跨会话不再从零问"上次的受体在哪" |
| 2 | 冲突检测 + 注入时 `stale` 提醒 | 防止记忆过期误导 |

**不建议一上来做**：向量检索、自动抽取记忆、用户级画像。条目少的时候它们带来的是复杂度而不是收益。

---

## 十一、预留（等打分与筛选实施后再启用）

打分与具体筛选流程尚未实现，以下三类**暂不做**，设计保留在此：

| kind | 依赖 | 内容 |
| --- | --- | --- |
| `PREFERENCE` | 打分 / 过滤环节 | 默认打分函数、过滤器链、输出格式与命名约定 |
| `RUN` | 完整筛选流程 | 每轮筛选的输入、参数、命中数、结论、耗时、失败原因 |
| `RECIPE` | 跑通过若干轮流程 | 成功流程模板（步骤序列 + 参数 + 适用条件） |

启用时要同步改动的地方：`MemoryKind` 枚举、`MemoryStore`（`scope` 字段与用户级目录）、存储布局（`runs/`）、`recall` 的 kind 过滤、以及本文第三/四/七节的表格。

---

## 十二、一句话

> Memory 不是"把聊天记录存起来"，而是**给项目建一份可校验、可撤销、跨会话的事实台账**：
> 存路径与指纹而不是 handle，核心常驻、细节按需，规则永远压过记忆。
