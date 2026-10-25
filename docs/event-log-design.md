# 事件层设计（EVENT）

> 实施日期：2026-10-25。代码：`core/agent/event_log.py`、`core/agent/context.py`
> （`Layer.EVENT`）、`core/models.py`（`Event` / `Session.events`）。
> 总计划见 [`context-rework-plan.md`](context-rework-plan.md)。

## 一、为什么需要这一层

上下文里原本只有两种历史：摘要（很粗）与 RECENT（逐条原文）。中间那一档没人管：

- 「刚才调了哪几次工具、产出了哪个 handle」——摘要里只剩一句结论；
- 「哪一步失败了、为什么换了个做法」——RECENT 一旦被挤出就整个消失。

表现是长会话里反复做同一件事、接着一个已经被否掉的方案往下走。
事件层就是那一档：**一段工作收口成标题 + 摘要**，原文让位，但可按编号取回。

```
PREFIX → MEMORY → TASK → EVENT → RECENT → EVIDENCE → HISTORY
```

## 二、数据结构

```python
@dataclass
class Event:
    id: str            # evt_0001，会话内连号
    title: str         # ≤40 字
    summary: str       # ≤600 字
    actions: list[str]      # 用过的工具，代码从 Part 数出来
    artifacts: list[str]    # 产出的文件 / handle / run_id
    issues: list[str]       # 失败项首行
    start: int         # 消息下标，闭
    end: int           # 消息下标，开
    kind: str          # manual / auto / local
    degraded: bool     # 摘要是本地汇编的
```

`actions` / `artifacts` / `issues` 永远由代码从 `Part`（`tool_name` / `tool_meta` /
`tool_status`）数出来，**不让模型转述**：模型写摘要、本地兜底这两条路的
「动作与产物」因此一定一致。

## 三、三条不变式

1. **连续覆盖**。首条 `start = session.summary_upto`，后一条 `start = 前一条.end`，
   RECENT 从 `events[-1].end` 开始。`Session.sanitize_events()` 保证这一点：
   发现越界或断点就把断点之后的事件全部丢掉，并把 `compact_upto` 推到覆盖末端。
   留一道缝的话，那一段原文既不在事件里也不在 RECENT 里，内容无声消失。
2. **边界排除正在写的东西**。`end = min(当前位置, len(messages))`，再往前跳过
   连续的 user 消息：当前那条 assistant 还没写完，用户的下一句是输入而不是历史。
3. **本地兜底不花请求**。模型写不出摘要（请求失败、空文本、被取消）时，
   按 Part 汇编一条 `degraded=True` 的事件。被取消时不写。

## 四、工具

| 工具 | 作用 | 关键行为 |
| --- | --- | --- |
| `record_event(title?, summary?)` | 收口一段 | 两个都给 → 不请求模型；只给一个 → 让模型补；模型失败 → 本地汇编 |
| `recall_event(event?, offset?, limit?)` | 列清单 / 读一条 | 读的是**原 Part**，不受 `compact_upto` 影响 |

`record_event` 的手动事件从最后一条 `manual` 事件的 `end` 起，并吞掉它后面的
`auto` / `local`——那些是同一段内容的不同切法。相邻的 `auto` / `local` 互相合并，
否则事件层会退化成另一份逐条列表。

返回值只回「编号 + 标题 + 区间 + 产物/未决条数」，不回灌整段摘要：
那段摘要下一步就会出现在上下文里，回灌一遍等于发两遍。

## 五、什么时候自动收口

`_run_steps` 里，装配发现这一步新挤出了交互组（`context.dropped > 0`）：

- **有事件**：`record_local_event(..., kind="auto")`，零模型请求，然后重装一次；
- **没有事件**：走原来的摘要路径（`_summarize`）。

`_summarize` 在有事件时直接返回 `False`：摘要与事件不重叠，否则同一段内容
会被说两遍。`/compact` 在有事件时先补一条 `kind="local"` 事件，再推压缩位置。

## 六、降级

事件块单独一条 user 消息（在 header 之后、RECENT 之前），额度
`min(1800, max(200, available // 6))`。降级链里排在 `recent_tool_clear` 之后、
`memory` 之前：丢掉旧事件也可逆（`recall_event`），但比一行占位贵。

## 七、持久化

`Session.to_dict()` 里是 `events` + `event_seq`；`from_dict` 读回之后立刻
`sanitize_events()`。旧状态文件没有这两个字段时按空处理，行为与之前一致。

## 八、不做什么

| 想法 | 为什么不做 |
| --- | --- |
| 单独的 `PartType.EVENT` | 事件只进请求体与 `/events`，不必牵动 TUI / web 两套渲染 |
| 每轮自动让模型写事件 | 会多一次请求；先做「溢出时本地兜底 + 模型显式收口」 |
| 事件与摘要互相覆盖 | 两者覆盖的区间是分开的，合并会让「哪段是结论、哪段是过程」消失 |
| web 端展示事件 | 这一期只做 TUI；接口形状稳定之后再补 |
