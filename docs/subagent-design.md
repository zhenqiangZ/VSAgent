# 子代理上下文隔离设计（C4-2）

> 目标：让「会产生大量中间输出的子任务」从主上下文里挪出去。
> 主代理把自包含的子任务交给一个**独立上下文**的子代理，只拿回一份结论。
>
> 配套文档：`context-design.md`（分层上下文总设计）、`context-plan.md`（实施计划）。
> 本文是设计，不是实现。实施时以本文的边界与不变式为准。

---

## 一、问题

主上下文里有一类子任务，它的**过程**远大于**结论**：

- 「把仓库里所有 EGFR 相关的结构文件找出来，逐个看一眼分辨率」——十几步、几十次工具调用、
  几千行输出，最后要的只是「12 个里 4 个优于 2.5Å」这一句；
- 「把这条流水线从头到尾跑一遍，出问题就把日志翻出来」——中间日志可能上万字符，
  有用的只有末尾那段报错。

这类子任务在主上下文里做，会同时踩三件事：

1. `RECENT` 的额度被这一件事占满，主任务真正需要的材料被挤出去；
2. 中间输出里没有「值得长期留住的结论」，进 `EVIDENCE` 也是白占额度；
3. 主代理的注意力被这几十条工具结果稀释，越往后越容易跑题。

第 3 期的选择性压缩能**减轻**第 1、2 条，但压不掉的仍然留在上下文里：
结论只有一句，过程却有几千 token。这一节解决的是「那几千 token 根本不该进主上下文」。

---

## 二、办法

主代理调用一个 `delegate` 工具，参数是**自包含**的任务描述（可选再带一段背景）。
内核起一个**独立上下文**的子代理去跑，只把结论回灌：

```
主上下文（这一件事只占两格）：
  [tool_call]   delegate(task="找出仓库里全部 EGFR 结构，列出分辨率")
  [tool_result] "共 12 个，其中 4 个优于 2.5Å：3ert 1.9、4hjo 2.1 …"

子代理自己的上下文（跑完即丢）：
  step1 read …  →  step2 grep …  →  …  →  step9 写结论
```

**隔离的边界画在 `Session` 上。** `ContextAssembler` 本来就是以 `Session` 为单位装配的，
所以子代理只需要一个**新建的临时会话**：它只装任务描述与自己这几步。
主会话里一个字都不写子代理的中间过程。

---

## 三、子代理不是第二个进程，也不是第二个实例

子代理是 `AgentLoop` 上的一个方法（`run_subagent`），**同一个实例**跑，
共用 `provider` / `registry` / `policy` / `ledger` / `memory` / `bus`。
只有三样东西换掉：

| 换掉 | 换成 | 为什么 |
| --- | --- | --- |
| `Session` | 新建的临时会话 | 隔离的全部来源 |
| `EvidenceStore` | 新开一个 | 证据是「本步相关」的，子代理的证据不该混进主上下文 |
| 步数上限与窗口 | `SUBAGENT_MAX_STEPS` / `SUBAGENT_WINDOW` | 子代理是干一件事的，不该吃满主代理的额度 |

**共用同一个实例**换来的是一串免费的正确性：

- `ctrl+c` 的 `cancelled` 是同一个标志，中断天然传导；
- 权限询问的 `_pending_permissions` 是同一本账，`resolve_permission` 不用为子代理另写一套；
- `_run_steps` / `_execute_tool` 本来就是「会话当参数传」的形状，递归调用天然成立。

反过来说，**另起一个 `AgentLoop` 实例**要额外解决中断、权限路由、事件过滤三件事，
而这三件事都不产生任何收益。

---

## 四、四件必须专门处理的事

### 4.1 权限询问要冒泡到主界面

子代理里 `bash` / `write` / `patch` 这类调用仍然要问一次。
询问事件由同一个循环发出，`_pending_permissions` 也在同一本账上，
所以 `resolve_permission` 那一侧不用改。要改的是**界面那一侧**：

`ChatScreen._on_permission_ask` 现在从「当前会话里的 Part」上取工具名与参数，
而子代理的 Part 不在当前会话里（`_find_message` 找不到），于是这一条会被静默丢掉、
子代理一直停在等答复上直到超时。做法是**把这几样放进事件里**：
`permission.ask` 补 `tool` / `argument` / `agent` 三个字段，界面优先读事件里的值，
读不到再回退到「查当前会话的 Part」——既有行为一个字不变。

### 4.2 子代理发的事件要能被忽略

子代理的 `message.add` / `part.add` / `part.update` 带的 `message_id` 不在当前会话里，
界面那三个处理函数用 `_find_message` 查不到就直接返回——**天然被忽略**，不用改。

`turn.done` 是唯一一个界面不看 `session_id` 的事件（它把 `_busy` 置假）。
但 `turn.done` 由 `_finish` 发出，而子代理走的是 `_run_steps`、**不调 `_finish`**，
所以这条根本不会发。为稳妥起见，界面那一条仍然补一个会话判断——
将来若有人给子代理加上 `_finish`，这一条会先报出来。

### 4.3 子代理不能再委派

深度上限 1：子代理的工具清单里没有 `delegate`。没有上限的话，
模型可以一路委派下去，步数、耗时、费用都不可控。

### 4.4 结论要有上限

子代理最后一条助手正文作为 `delegate` 的工具结果回灌，按 `truncate_for_model` 压一道。
子代理的角色说明里明确要求「只写结论，不写过程」。

---

## 五、共享登记表：索引共享，正文隔离

子代理产出的 handle 仍然登记进**同一个** Ledger。这不是漏了隔离，而是隔离的要点：
主上下文拿到的是**索引**（`receptor#1` 存在、叫什么、在哪），不是**正文**。
要正文时用 `describe_entry` 现取——取回来的那一段才进主上下文，而且是主代理主动要的。

这与 `EVIDENCE` 层是同一条思路（`context-design.md` 第 3.3 节）：
上下文里放索引，不放整份内容。

---

## 六、角色

首期只做一个角色 `explore`：**只读检索**。

- 它拒绝 `permission.PLAN_DENY` 里的全部工具（`write` / `patch` / `bash` /
  `prepare_receptor` / `write_run`），再拒绝 `delegate` 本身。
  这份名单**复用现成的那个集合**，不另抄一份——领域工具增删时它跟着动。
- 身份说明由 `prompt._identity` 多一个分支给出：「你是探索子代理，只做检索与阅读，
  把结论带回去，不要改动工程」。

工具清单与身份说明**两处都要**：只改身份说明是「靠自觉」，只改清单是「模型不知道自己是谁」。
两者合起来才是这个角色的定义。

---

## 七、接口

### 7.1 注入通道

`Tool` 新增第四个通道：

```python
# 收「子代理入口」的参数名。没有这类参数时留空。
#
# 与前三个分开：它收的是一个可调用的入口（`run_subagent`），不是数据。
# 只认签名不认类型——工具层因此不必导入循环层。
subagent_argument: ClassVar[str] = ""
```

`execute(arguments, context, task_state, evidence, subagent)`，收到 `None` 同样报「内核装配问题」。

### 7.2 工具

`core/agent/subagent.py`（新文件）：

```python
class DelegateTool(Tool):
    spec = ToolSpec(
        name="delegate",
        description=(
            "把一个自包含的子任务交给一个独立上下文的子代理去做，只把结论拿回来。"
            "适合「过程很长、结论很短」的事：大范围检索、批量核对、长流水线试跑。"
            "子代理看不到本次对话的历史，所以 task 要写全：要什么、给谁、什么算做完。"
            "需要交代的背景（编号、路径、已经确认的结论）写在 background 里。"
        ),
        parameters={"type": "object", "properties": {
            "task": {...}, "background": {...}, "agent": {...},
        }, "additionalProperties": False},
    )
    subagent_argument = "run_subagent"
```

### 7.3 循环

```python
async def run_subagent(
    self,
    task: str,
    background: str = "",
    agent: str = "explore",
    workspace: Workspace | None = None,
) -> SubagentReport:
    """在一个独立上下文的临时会话里跑一个自包含的子任务，只把结论交回来。"""
```

实现要点（全部是「换掉、跑、还原」）：

1. 建临时 `Session`（`agent_name=agent`），把 `task` 与 `background` 写成一条 user 消息；
2. 保存并换掉 `self.evidence` / `self.max_steps` / `self.context_window`，
   跑完在 `finally` 里还原——漏了还原，主代理下一轮就会用子代理的证据与步数；
3. 用**主代理的工作目录**（`workspace`），这样相对路径解释一致、产物写在同一处；
4. 工具清单按角色过滤（`explore` 去掉 `PLAN_DENY ∪ {delegate}`）；
5. 收集子会话里最后一条助手消息的正文，按 `truncate_for_model` 压一道，装进 `SubagentReport`。

### 7.4 回灌形状

```python
@dataclass(frozen=True)
class SubagentReport:
    text: str            # 结论正文，进 tool_output
    agent: str           # 角色名，进 meta
    steps: int
    tokens: int
    failed: bool = False
```

放在 `tools.py`：循环层与工具层都要引用它，而 `loop.py` 已经导入 `tools.py`，不会成环。

---

## 八、显示

委派在消息区**显示成一次普通工具调用**（工具名 `delegate`，参数是任务描述，结果是结论）。
子代理内部的步骤**不显示**。

这样做的好处是**画面基本不动**：抓帧用的假后端不会主动调 `delegate`，
七帧应当仍与本期之前一致。真跑起来时，用户看到的是一张工具卡片，
而不是几十行子代理的过程。

`PartType.SUBTASK` 这个预留类型**本期仍然不用**：工具卡片已经说明了「这里发生过一次委派」，
再用一条 `◇ 子任务` 是同一件事显示两遍，还白白让画面多一处变化。

---

## 九、不变式（可写成判据）

1. **主会话不被污染**：一次委派之后，主会话只多一条助手消息；子代理的中间消息
   （read / grep / bash 的调用与结果）在主会话里一条都没有。
2. **主上下文不含子代理过程**：装配主上下文时，子代理内部那些工具输出的文本
   不出现在 `RECENT` / `EVIDENCE` 里。
3. **结论回灌**：子代理最后一条助手正文出现在 `delegate` 的工具结果里。
4. **索引共享**：子代理登记进 Ledger 的 handle，主会话用 `list_workspace` 看得见。
5. **深度受限**：子代理的工具清单里没有 `delegate`。
6. **中断传导**：主循环 `cancel()` 之后，正在跑的子代理停下，委派返回失败原因。
7. **权限不放宽**：子代理里需要询问的调用仍然询问；`explore` 下写盘工具被拒。

---

## 十、明确不做

- 不做多角色子代理（首期只有 `explore`）。
- 不做子代理过程的展开与折叠显示。
- 不做子代理之间的并发（与 `_run_tools` 不并发的理由相同：会争同一块磁盘与同一份输入）。
- 不把子代理的会话写进状态文件（它是临时的，跑完即丢）。

---

## 十一、一句话

> 主上下文与子代理之间交换的不是「历史」，是「一份任务描述进去、一句结论出来」。
> 隔离不是把内容藏起来，是让那些内容**从一开始就没进过主上下文**。
