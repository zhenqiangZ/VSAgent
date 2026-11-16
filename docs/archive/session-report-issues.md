# 会话报告与运行时问题（ses_0037 复盘）

> 状态：**只记录，不改代码**（2026-11-14，用户「暂不修改」）。
> 这一批问题来自本对话第一轮「分析最新的一轮会话」的诊断，此前只出现在
> 口头回复里，**一直没落文档**；现补齐。
> 事实来自 `~/.vsagent/state.toml`、`~/.vsagent/logs/ses_0037.jsonl`，
> 以及 `python tools/report_session.py` 走的那条静态报告管线。
>
> 关联：口袋会话问题 A/B/D 见 [`pocket-issues.md`](pocket-issues.md)；
> 检索结果过度依赖见 [`retrieval-overreliance.md`](retrieval-overreliance.md)。

## 一、范围与数字对照

父会话 `ses_0037`（两轮：`我想要针对HA2进行配体筛选，给我一个计划` →
`搜索口袋与配体信息`），worker `ses_0038`（ligand）、`ses_0039`（structure_pocket）。
三个会话的事件**写在同一份 trace 文件** `~/.vsagent/logs/ses_0037.jsonl` 里，
用 `payload.session_id` 区分。

| 指标 | 静态报告显示 | 实际（父 + 2 worker） | 缺口 |
| --- | --- | --- | --- |
| 步数 | 16 | **64**（16 / 24 / 24） | 少算 48 |
| 工具调用 | 20 | **97**（20 / 37 / 40） | 少算 77 |
| 失败工具 | 2 | **11**（2 / 3 / 6） | 少算 9 |
| 输入 token | 274,701 | **1,356,337** | 少算 ~1.08M |
| 输出 token | 15,380 | **55,176** | 少算 39,796 |
| 成本 | $0.044015 | **$0.190447** | 少算 ~4.3 倍 |
| 缓存复用 | 76.65% | 72.7%（985,600 / 1,356,337） | 口径不同 |

报告显示的两轮：#1 `done`（8 步 / 9 工具 / 2 失败）、
#2 **`running`**（8 步 / 11 工具 / 0 失败）。
`state.toml` 里父会话 `status = "done"`、`phase = delivery`。

四类事件（`REPORT_EVENTS`）全量 83 条，`read_trace(ses_0037)` 只保留 **25** 条，
过滤掉 58 条——全部是 `session_id` 为 `ses_0038` / `ses_0039` 的事件。

## 二、P0：静态报告只算父会话事件，worker 数据全丢

### 现象

`tools/report_session.py` 生成的 HTML 把一场 campaign 记成了一次单会话，
上面表格里的所有数字都偏小，成本差 4 倍多。

### 根因

`vsagent/core/agent/execution/report.py:21`：
```python
REPORT_EVENTS = frozenset({"message.add", "turn.done", "agent.status", "context.metrics"})
```
`report.py:29-53`：
```python
def read_trace(path: Path, session_id: str) -> list[dict]:
    ...
    """文件按会话分，正常不会混进别的会话；`session_id` 仍然要核……"""
    ...
    owner = payload.get("session_id")
    if owner and owner != session_id:
        continue
```
`tools/report_session.py:544`：
```python
trace_path = logs / f"{session_id}.jsonl"
trace = read_trace(trace_path, session_id) if trace_path.exists() else []
```

三处叠起来：

1. worker 事件**确实写进了父会话的 trace 文件**（`ses_0037.jsonl`），
   且带自己的 `session_id`（见 trace 里 `agent.created` / `agent.status` /
   `context.metrics` 的 payload）。
2. `read_trace` 的注释假设「文件按会话分，正常不会混进别的会话」——
   **引入 campaign / 子 agent 之后这个假设不再成立**；它加的 `session_id`
   复核反而把 worker 事件 systematically 丢掉了。
3. 报告只读 `ses_0037.jsonl` 这一个文件，也不会去 `children_of()` 找子会话。

### 影响

不只是数字难看：**报告自己的诊断逻辑也建立在被过滤后的数据上**。
`report.py:356-358` 有「第 N 轮走了 X 步，达到主题步数上限」的判据，
但父会话每轮只有 8 步，远没到 24，所以「两个 worker 都跑满步数上限」
这条本该最醒目的告警**根本没触发**。

报告 `issues` 里实际只剩 5 条（全是父会话可见的）：

```text
error  turn 1  第 1 轮 hypothesis 失败：items 第 1 条的 key 不合法（'interface_series'）……
error  turn 1  第 1 轮 hypothesis 失败：discriminator 要提到每个对手：缺少 hop……
warn   turn 1  第 1 轮缓存命中低于 0.5：第 8 步
info   —       evt_0001 的标题与结论由本地拼出：我想要针对HA2进行配体筛选，给我一个计划
info   —       evt_0002 的标题与结论由本地拼出：搜索口袋与配体信息
```

### 待选的修法（**均未实施**）

1. `report_session.py` 用 `SessionManager.children_of(session_id)` 把子会话
   一并读进来，按 `session_id` 分栏展示（父 / 各 worker / 合计），不再只给一份。
2. 或让 `read_trace` 在 campaign 模式下接受一组 `session_id`，
   保留子会话事件但打标，别静默丢。
3. `report.py` 的诊断（步数上限、缓存、失败）改成按**全部**事件算，
   至少别让 worker 的收束失败从告警里消失。
4. 修 `read_trace` 那段注释：trace 文件在 campaign 下是父子共写的。

## 三、P1：第 2 轮显示 `running`，但 state 是 `done`

### 现象

报告 `#2 status = running`；`state.toml` 里 `ses_0037.status = "done"`，
`campaign.done` 在 trace 第 1101 行，`unfinished: []`。

### 根因

`report.py:241` 取窗口内最后一个 `agent.status` 当轮次状态：
```python
statuses = [status for at, status in status_at if start <= at <= end + 2.0]
...
"status": (statuses[-1] if statuses else "done") if reply is not None else "no-reply",
```

trace 里父会话的 `agent.status` 序列是：

```text
line    2  ses_0037 -> running
line  201  ses_0037 -> done      （第 1 轮收尾）
line  204  ses_0037 -> running   （第 2 轮开始）
line 1090  ses_0039 -> done      （worker，被 P0 过滤）
line 1095  ses_0038 -> done      （worker，被 P0 过滤）
line 1101  campaign.done          （unfinished 为空）
line 1195  turn.done  ses_0037
```

**父会话在第 2 轮结束后没有补一条 `agent.status = done`**：
campaign 关闭时只发了 `campaign.done`，`done` 状态只落在 worker 上。
而 worker 的 `done` 又被 P0 的 `session_id` 过滤掉。
于是窗口里最后一个可见 status 是第 204 行的 `running`。
报告读 trace、界面读 state，两边结论不一致。

### 待选的修法（**均未实施**）

1. campaign 关闭时给父会话补发 `agent.status = done`（与 `campaign.done` 同一处）。
2. 或报告用 `turn.done` 作为轮次终态来源，而不是 `agent.status` 的最后一个值；
   `turn.done` 对每个会话都有（第 199 / 1088 / 1093 / 1195 行）。
3. 或状态回退到 `session.status`（state 里是 `done`）为准。

## 四、P1：两个 worker 都跑满 24 步，尾部工具全被收束阶段拒掉

### 现象

| 会话 | 步数 | 工具 | 失败 | 尾部失败输出 |
| --- | --- | --- | --- | --- |
| `ses_0038` | 24 | 37 | 3 | 「本轮步数快用完了……」×2 + `write` 写盘被拒 |
| `ses_0039` | 24 | 40 | 6 | 「本轮步数快用完了……」×5 |

`ses_0039` 尾部 #34–#38 连着 5 次 FAILED：`read` ×2、`assess_structure` ×2、
`open_viewer` ×1，输出全是同一句：

```text
本轮步数快用完了，不再展开新工作。把已经做出来的写进 write_run，
用 close_topic 收这一段，再用 task 记下下一步。
```

结果：两处 `meta.json` 复核、两个受体的缺口/交替构象核对都没执行，
最终答复只能写「页面地址在第二轮取得过但本次记录不完整，未产出可靠地址」。
`ses_0038` 的第二轮成稿在 `write` 前被运行时挡下，没落盘。

### 根因

`vsagent/core/agent/execution/loop.py:43` `MAX_STEPS = 24`（可用
`VSA_MAX_STEPS` 覆盖）、`:46` `CLOSING_STEPS = 3`、`:943-948`：
```python
if tool_steps >= self.max_steps: ...
steps_left = self.max_steps - tool_steps
self._closing = steps_left <= CLOSING_STEPS
```
`loop.py:1575-1582`：收束阶段只放行 `CLOSING_TOOLS`，其余一律 `_fail_tool`。
设计意图是「把还剩几步交给模型自己安排」，但当**开场盘点就吃掉大半步数**时
（见 `pocket-issues.md` 问题 B 的 18 次 `bash`），收束期只剩复核动作可做，
而复核动作恰好不在 `CLOSING_TOOLS` 里，于是全被拒。

### 影响

形成闭环：`bash` 多 → 预算紧 → 收尾复核做不完 → 产物可信度下降
（呼应 `retrieval-overreliance.md` 三、机制层根因第 7 条
「步数预算让『回原文核对』最先被砍」）。

### 待选的修法（**均未实施**）

1. 把「只读复核」类工具（`read` / `assess_structure` / `open_viewer`）
   放进 `CLOSING_TOOLS`，或至少让它们在收束期可放行。
2. 预算按「新工作 / 复核」分池，别让盘点挤掉复核额度。
3. 收束期给出「还剩几步、哪些工具还能用」的明确清单，
   现在模型是撞了 `_fail_tool` 才知道。

## 五、P2：`hypothesis` 校验两次失败，第三次才写入

`ses_0037` 工具 #7–#9 连续三次 `hypothesis`，前两次被校验器挡回：

```text
items 第 1 条的 key 不合法（'interface_series'）：用 1–12 位小写字母、数字、下划线或连字符。
discriminator 要提到每个对手：缺少 hop。写清什么结果支持本条、什么结果支持对手。
```

第三次改成 `series` / `hop` 并在 `discriminator` 里点名双方才成功。
两条规则见 `docs/archive/runtime-fixes-plan-2.md:47-52`（3.4 放宽了 key 上限
与 discriminator 措辞，但本轮仍按旧口径拦下）。

**影响**：模型自纠成功，属于「可恢复」而非错误结论；但两次无效请求各带完整
items 载荷，白耗步数与 token。报告把这二条如实记成了 `error`。

待选（**未实施**）：校验失败时把可用的 key / 补全建议直接回给模型
（3.4 第 3 条已有此设计），减少重试轮数；确认本轮为何仍走旧口径。

## 六、报告自己给出的另外三条提示

这三条不是 bug，是报告如实记录的观察，但值得留在案：

1. **`warn` 缓存命中低于 0.5：第 1 轮第 8 步**（`report.py:350`）。
   整体缓存复用 76.65%，单步掉到 0.5 以下多半是 `load_tools` 改了工具清单、
   前缀缓存整段失效（`toolset.py:207` 记的代价），属于已知权衡，不是异常。
2. **`info` ×2：`event.sealed` 的标题与结论由本地拼出**（`report.py:365`）。
   父会话两条 sealed 事件的标题直接用了**用户原话**
   （「我想要针对HA2进行配体筛选，给我一个计划」「搜索口袋与配体信息」），
   没有概括本轮做了什么。对照 worker 的 sealed 标题
   （「定义 HA2 两处候选口袋并建立目录」「HA2 共晶配体身份核对与活性取证」）
   是模型在 `close_topic` 里给的、信息量明显更高。
   `tools/report_session.py:474` 把这类事件标成「本地拼」tag。
   即：**降级只发生在父会话的事件封存**，worker 那边没降级。

## 七、派发层：交付物不下发

`dispatch_campaign` 预览（`ses_0037` 工具 #5 / #13）原话：

> 注意：计划有 2 个终点（ligand、structure_pocket），交付物
> LigandCatalog、CandidateSet **不知道该挂给谁，这次不下发**。

于是两个 worker 的任务书里没有「要交的东西」「验收条件」两行，
`delivery.check()` 的 `required_outputs` 为空。这条既是
`pocket-issues.md` 问题 D（口袋覆盖账本连入口都没有）的根因之一，
也解释了为什么本轮 report 的 `failed` 只有 2 条却看不出交付缺口。
详见 `pocket-issues.md` 4.2 第 5 条。

待选（**未实施**）：多终点计划里给每个终点指派交付物归属，
或至少在预览里给出「未指派的交付物」清单并让调用方显式确认。

## 八、核对命令 / 出处

```bash
# 复现报告数字
python3 - <<'PY'
from pathlib import Path
from vsagent.core.session_manager import SessionManager, default_state_path
from vsagent.core.agent.execution.report import read_trace, build
sm = SessionManager(default_state_path()); sm.load_from_toml()
s = sm.sessions["ses_0037"]
data = build(s, read_trace(Path.home()/".vsagent/logs/ses_0037.jsonl", "ses_0037"))
print(data["session"]["steps"], data["session"]["tools"], data["session"]["failed"],
      data["session"]["cost"], data["session"]["cache_reuse"])
for t in data["turns"]:
    print(t["index"], t["status"], len(t["steps"]), len(t["tools"]), t["failed"])
print(data["issues"])
PY

# 全量对照
python3 - <<'PY'
from vsagent.core.session_manager import SessionManager, default_state_path
sm = SessionManager(default_state_path()); sm.load_from_toml()
for sid in ("ses_0037","ses_0038","ses_0039"):
    s = sm.sessions[sid]
    v = lambda x: x() if callable(x) else x
    print(sid, v(s.total_tokens), v(s.total_output), v(s.total_cost))
PY
```

代码出处：

- 报告事件白名单与过滤：`vsagent/core/agent/execution/report.py:21`、`:29-53`
- 轮次状态取最后一个 `agent.status`：`:241`、`:261`
- 报告诊断（步数上限 / 缓存 / 本地拼 / issues）：`:350`、`:356-365`
- 缓存复用口径：`:391`
- 报告入口只读一个 trace：`tools/report_session.py:544`、`:548`；
  「本地拼」tag：`:474`
- 步数上限与收束：`vsagent/core/agent/execution/loop.py:43`、`:46`、
  `:943-948`、`:1575-1582`
- 假设校验规则：`docs/archive/runtime-fixes-plan-2.md:47-52`
- `load_tools` 前缀缓存代价：`vsagent/core/agent/toolset.py:207`

## 九、不改的边界

按用户 2026-11-14 的指示，本文**不**改代码、**不**改既有文档。
上述「待选」只作下次开工候选。注意本文与另外两份文档的分工：

- `pocket-issues.md`：口袋 worker 的**行为**问题（A 不检索资料库 / B bash 多 /
  D 覆盖不全）；
- `retrieval-overreliance.md`：**跨角色**的检索结果过度依赖；
- 本文：**会话报告管线 + 运行时预算/派发**的问题，主语是框架而非某个 worker。

三者共用同一份 `ses_0037` 证据，结论互相引用，不重复记账。
