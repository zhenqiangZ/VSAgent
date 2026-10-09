# 三处故障修复计划（reasoning 回传 · 权限记忆 · 报错呈现）

> **执行状态：已完成**。
> 第一轮（2026-10-05）做完第 1/2/3/5/7/8/9 步，第 4 步当时按「不做」跳过。
> 第二轮（2026-10-06）用户明确要求「按 a 就是允许本次会话全部运行」，第 4 步补做完毕：
> `PermissionPolicy` 的放行粒度从「单条命令原文」改成「工具名」，
> 新增判据 `.probe/check_permission_remember.py`，第 6 步的断言同步改写
> （原来那两句把「精确匹配」这个实现细节写成了期望，是错的）。
> 最终读数：agent 层、冒烟层全绿；禁用字符 0 处；七帧成对抓帧全部一致。
> 执行记录见 `.workbuddy-ai/memory/2026-10-05.md` 与 `2026-10-06.md`。

本文件是「把用户报告的三处故障修掉」这一轮的实施方案与执行清单。动笔之前先把
**已核对过的事实**列清楚，本轮每个判断都建立在这些读数上。

三个问题来自同一次排查（会话 `ses_0034` + 事件日志 `~/.opencode_tui/logs/ses_0034.jsonl`），
详细现场记在 `.workbuddy-ai/memory/2026-10-05.md`。三者的共同特征是
**症状在一个地方，根因在另一个地方**，所以三处要分开改，不要合并成一个提交。

## 0. 已核对的事实

### 0.1 问题一：缺 `reasoning_content` 导致 400，进而拿不到总结

| 项 | 读数 | 位置 |
|---|---|---|
| 报错原文 | `HTTP 400：The reasoning_content in the thinking mode must be passed back to the API` | 消息 81 `prt_1887`、消息 83 `prt_1902` |
| 服务端规则 | **只要请求带 `tools`，历史里每条 assistant 都要回传 `reasoning_content`**，包括没有工具调用的最终回答 | `prompt.py::_translate_assistant` docstring 第 357-359 行 |
| 漏的那一半 | `flush()` 的 `if tools:` 分支里那句 `if reasoning:` —— 本 step 无推理时不挂 | `prompt.py:384`、`prompt.py:397` |
| 补过一次 | 9-28 只给 `elif content:` 分支补了（纯正文最终回答），`if tools:` 分支没动 | `.workbuddy/memory/2026-09-28.md` 第 95-100 行 |
| 未动过 | `git show HEAD:.../prompt.py` 与当前工作副本的 `_translate_assistant` **逐字节相同** | 不是回归，是一开始只修了一部分 |
| 复现读数 | 重放 `state.toml`：缺 reasoning 且带 tool_calls 的段出现在**消息 81 与 83**，两次都紧接一个 400 | 用当前 `build_model_messages` 重放，`dropped=0` |

**关键**: 两处 `if reasoning:` 是同一个前提（「无推理就不挂」）。改一处不够，要看住这个前提。

### 0.2 问题二：按 a（总是允许）之后仍然继续询问

| 项 | 读数 | 位置 |
|---|---|---|
| 文案 | `a 本会话总是允许` / `Binding("a", "allow_always", "本会话总是允许")` | `screens/permission.py:34,50` |
| 实际语义 | `remember` 存 **`argument_value()` 的整串文本**（bash 即 `command` 原文） | `permission.py:121-130` |
| 匹配方式 | `fnmatch.fnmatchcase(value, pattern)`，串里含 `&` `"` `\` → **退化成逐字节精确比对** | `permission.py:43-45` |
| 实测 | 同一条命令原样重放 → `allow`；**末尾加一个空格** → `ask` | `.probe/repro_permission.py` |
| 现场 | msg 81 那一轮连续问 5 次（t=3.5 / 38.2 / 54.5 / 77.7 / 168.1s），用户按了 5 次 a | 事件日志 `permission.ask` × 5 |
| 断言锁错了 | `check(judge(...,{"command":"pwd"}).verdict is Verdict.ASK, "本会话记住的只覆盖命中的那一条")` —— **把现状写成了期望** | `tests/test_agent.py:827-830` |
| 冒烟层 | 按 a 之后只验了「同一条命令放行」，没验「换一条命令是否还问」 | `tests/test_smoke.py:374-379` |

### 0.3 问题三：请求失败时错误文本被当模型正文

| 项 | 读数 | 位置 |
|---|---|---|
| 行为 | `_publish(Part(type=PartType.TEXT, text=f"请求没有成功：{error}"))` | `loop.py:412-417` |
| 后果 | 会话里出现一条「正文」，**下一轮会被当正常历史回灌**；用户看到的最后一条文本就是这个报错 | 消息 81 / 83 末尾 |
| 现有类型 | `PartType` 已有 11 个成员，含 `RETRY` / `COMPACTION` 这类「非模型产出」的标记 | `models.py:51-64` |
| 渲染分派 | `render_part_text` 按类型逐条 `if`，认不出已知类型就 `return part.text` | `widgets/messages_pane.py:283-308` |

### 0.4 与本次一并处理的两处 bash 问题

| 项 | 读数 | 位置 |
|---|---|---|
| 超时 | `DEFAULT_TIMEOUT = 120.0`，超时报「命令超过 120 秒没有结束，已经终止」 | `tools.py:12`、`tools.py:453-457` |
| 现场 | `fetch_nav17.py` 分页抓 ChEMBL 超时被截断 | 消息 83 `prt_1900` |
| cwd | `cd` 不跨调用保持（每次独立子进程），模型按「会话级 shell」的直觉写相对路径 → `copy` 找不到文件 | 消息 69 `prt_1835/1839` |
| 系统提示 | **没有**任何一段写这件事 | `prompt.py` 里 grep 不到 |

## 1. 已确认的三个决定

| 问题 | 选项 | 决定 |
|---|---|---|
| a 键语义 | 记住整个工具 / 只改文案 / 按前缀记 | **记住整个工具** |
| 报错呈现 | 独立 Part 类型 / 仍写 TEXT 加标记剥离 / 不改 | **独立 Part 类型** |
| bash 两处 | 都改 / 只改超时 / 只写 cwd / 都不改 | **超时与 cwd 说明都改** |

## 2. 执行清单

### 第 1 步　`_translate_assistant`：无推理的段也补上 `reasoning_content`

`opencode_tui/core/agent/prompt.py`

`flush()` 的两个分支共用 `if reasoning:` 这个前提。改成**两个分支都无条件挂**，
reasoning 为空时写一个占位文案（常量 `EMPTY_REASONING`，写清「本步没有推理输出」）。
这样「每条 assistant 都带 `reasoning_content`」变成结构上的不变式，而不是靠「这次刚好有推理」。

理由：占位文案比「缺字段」安全——服务端要的是「字段存在」，内容它只用来自我一致。
**不能**改成「不带 tools 重发」：那会让模型失去工具能力，等于把一步退化成聊天。

```python
# 无推理的 step（只有工具调用、或只有正文）也要占位：
# DeepSeek 要求带 tools 的请求里每条 assistant 都回传这个字段，缺一条就 400。
EMPTY_REASONING = "（本步没有推理输出）"
```

### 第 2 步　断言：补上「无推理时也必须带上」

`tests/test_agent.py::check_prompt`

现有夹具每个 step 都带 REASONING Part，**永远走不到空分支**——这正是它没拦住故障的原因。
新增夹具：一条 assistant 消息，step 里**只有 TOOL、没有 REASONING**，断言：

1. 那条带 `tool_calls` 的 assistant 项**含** `reasoning_content`；
2. 值是 `EMPTY_REASONING`；
3. 遍历 `built.messages`，**凡带 `tool_calls` 的 assistant 项都必须含该字段**（这一条是主断言，
   前两条是它失败时的诊断信息）。

第 3 条要写成「全量扫一遍」而不是「看某一条」：故障的形状正是某一处被漏掉，
而按索引点名的断言只会盯住自己写的那一处。

### 第 3 步　`_ask_model`：把请求失败换成独立的 Part 类型

`opencode_tui/core/models.py` + `opencode_tui/core/agent/loop.py` + `opencode_tui/widgets/messages_pane.py`

1. `PartType` 加成员 `ERROR = "error"`（放在 `RETRY` 旁边，语义同类：都不是模型说出来的）。
2. `loop.py::_ask_model` 非重试分支改成 `Part(type=PartType.ERROR, text=f"请求没有成功：{error}")`。
   同一个改动应用到 `_summarize` 里那条摘要失败的分支（它现在用 `COMPACTION` 携带失败文案，
   语义已经对了，**不动**）——只改 `_ask_model` 这一处。
3. `render_part_text` 加一条：`if part.type is PartType.ERROR: return f"■ 请求没有成功 {part.text}"`。
   前缀用符号区分于正文，认不出分派时不会退回 `part.text` 而看起来像模型说的话。
4. **回灌侧不用改**：`_translate_assistant` 的 `for part` 循环只认 `STEP_START` / `REASONING` /
   `TEXT` / `TOOL` 四类，新类型天然被跳过。这正是选「独立类型」的理由——
   靠「回灌前剔除」的做法每加一类 Part 都要回到翻译层补一次。

### 第 4 步　`PermissionPolicy.remember`：记住整个工具　【2026-10-06 补做】

`opencode_tui/core/agent/permission.py`

**实装时没有沿用「pattern 改成 `*`」这个做法**，改成了集合：`_session_tools: set[str]`，
`check()` 里一句 `if name in self._session_tools`。理由是这个语义本来就只归到工具名上，
用规则表存等于留一个永远只写 `"*"` 的字段，读的人还得回头确认它有没有别的取值。
`remember()` 仍返回一条 `PermissionRule`（`pattern="*"`、source 为「本会话记住」），
调用方与 Part 上的依据文案因此不用改。

要一起看的地方：
- `check()` 的第五级从 `_first(self._session_rules, Verdict.ALLOW, ...)` 换成
  `if name in self._session_tools`，位置不变（仍在项目 deny/ask 之后、用户配置之前）。
- **`argument_value()` 不能删**：它同时是弹窗上要展示的那一段（`permission.py:137-146`
  的 docstring 明写「判定按哪个参数比对，用户就该看到哪个参数，两者必须是同一个」）。
  改完之后弹窗展示的仍是**这一次的参数**（具体命令），记忆的却是**整个工具**——
  这是一处新的不一致，必须在弹窗文案上交代清楚（见第 5 步）。
  **不要**为了「一致」把弹窗也改成只显示工具名：用户需要看到自己要放行的那条命令是什么。
- `clear_session()` 照旧，切换会话时清空。

风险与边界：bash 是 `DEFAULT_ASK` 里最宽的一个（什么都能干），按 a 等于本会话不再问 bash。
`PLAN_DENY` 那条注释的顾虑（「一条命令会不会改动看不出来」）在这个语义下被放大了，
所以**弹窗必须把「本会话后续所有 bash 都不再问」这句写在键位上**，不能让用户误以为
只针对眼前这一条。这是本轮安全性上唯一需要人工确认的点。

### 第 5 步　弹窗文案：说清 a 键的真实范围

`opencode_tui/screens/permission.py`

| 位置 | 原文案 | 现在 |
|---|---|---|
| `Binding` 描述 | `本会话总是允许` | `本会话不再问本工具` |
| 提示行 | `y 允许一次    a 本会话总是允许    n 拒绝（esc 同）` | `y 允许一次    a 本会话不再问 <工具名>    n 拒绝（esc 同）` |

`PermissionDialog.__init__` 已经收了 `tool_name`，直接插值即可。
两处都要改：`Binding` 的 `description` 会出现在帮助/键位说明里，`#permission-hint` 是弹窗上那行。
`description` 里放的是静态文案（`Binding` 是类属性，插不进实例的 `tool_name`），
放的是工具名那就得到提示行找——提示行才是用户按下去时看着的那一行。

### 第 6 步　断言：权限记忆的新语义

`tests/test_agent.py::check_permission`

把第 827-830 行那两句重写（现在写定的是「精确匹配」这个实现细节）：

1. `remember` 之后，**同工具的另一条命令也放行**（`{"command": "pwd"}` → `ALLOW`）——
   这是本轮要建立的新语义，是主断言；
2. 规则文案是「本会话已允许：bash \*」；
3. **另一个工具不受影响**（`remember` 了 bash 之后 `write` 仍然 `ASK`）——
   边界要守住，否则等于全放行；
4. 参数为空的情况（`primary_argument` 为空的工具）仍生成可命中的规则。

`tests/test_smoke.py::run_permission_round`：按 a 之后除了验「同一条命令放行」，
**再验一条不同参数的同工具调用**（对应第 1 条，端到端）。

### 第 7 步　BashTool：超时上限可配

`opencode_tui/core/agent/tools.py`

`DEFAULT_TIMEOUT` 是模块常量，`BashTool.__init__` 已经收 `timeout` 参数，
但 `app.py::_build_registry`（第 173 行）装配时没传，实际用的永远是 120 秒。

- 常量值从 `120.0` 提到 `600.0`（`BashTool` 单独一个 `BASH_TIMEOUT`，不要动
  `DEFAULT_TIMEOUT` 这个名字——**`tools.py` 与 `provider.py` 各有一个同名的
  `DEFAULT_TIMEOUT = 120.0`**（`tools.py:12` / `provider.py:11`），后者是网络超时，
  改错会静默影响请求超时）。
- `app.py::_build_registry` 显式传 `timeout=BASH_TIMEOUT`，让装配处看得见这个值。
  测试侧已有三处显式传 `timeout`（`test_agent.py:1500/1523/1716`），不受影响；
  `test_agent.py:742/5692` 那两处走默认值，会跟着变长——它们不跑超时路径，无影响。
- `app.py::_build_registry` 显式传 `timeout=BASH_TIMEOUT`，让装配处看得见这个值。
- 超时文案跟着说清怎么办：「命令超过 600 秒没有结束，已经终止；长任务请分小段或放后台跑」。

### 第 8 步　系统提示：写清「每次调用是新的 shell」

`opencode_tui/core/agent/prompt.py`

在纪律段里加一句（与现有的「收 handle 不收路径」那段同级）：

> 每次 `bash` 调用都是新开的 shell：上一条命令里的 `cd` 不会带到下一条。
> 需要固定目录时，在同一条命令里 `cd` 之后再执行，或用绝对路径。

**同时要说清这份提示的副作用**：纪律段约 174 token，加这一句会让 `check_loop` 里
「发出去的历史降到窗口的八折以内」那条比例断言再次超支。上次加纪律段时已经把窗口
提成了具名常量（`COMPACT_WINDOW = 2600`，定义在 **`tests/test_agent.py:131`**，
不在 `prompt.py` 里——它是测试侧的夹具常量），这次要按同样的做法把这个常量再提一档
（连同常量旁边的注释一并更新），**不是**去放宽那条断言的八折阈值（`test_agent.py:2069`）。
提常量而不是放阈值：量出来的超支来自提示的固定开销，压缩机制缩不动它，
放阈值等于把这条断言改成永远通过。

### 第 9 步　`.probe/repro_permission.py` 归档

本轮排查用的复现脚本。改成断言形式后归入 `.probe/`（那里已有一批同类脚本），
或确认 `tests/test_agent.py` 的新断言已覆盖它要证的每一条之后删掉。
**不留在根目录**。

### 第 10 步　回归与打包

```
.venv/Scripts/python.exe tests/test_agent.py            （当前 1001）
.venv/Scripts/python.exe tests/test_smoke.py            （当前 1498）
.venv/Scripts/python.exe .probe/check_banned_chars.py
.probe/run_all_checks.sh 分四段（0 15 / 15 15 / 30 15 / 45 15）
```

界面有改动（多了 ERROR 类型的 Part、弹窗文案变了），**必须重新打包 + 成对抓帧七帧**：

```
mv dist/opencode-tui.exe .probe/opencode-tui-YYYYMMDD.exe
C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/opencode_tui_pack/Scripts/pyinstaller.exe \
    --clean opencode_tui.spec
<pack python> tests/verify_exe.py --source
<pack python> tests/verify_exe.py
<pack python> tests/verify_modal_transparency.py --source
<pack python> tests/verify_modal_transparency.py
.venv/Scripts/python.exe .probe/compare_frames.py
```

**四个抓帧命令都要跑**（少跑一侧会让帧比对假失败）。

## 3. 辨别力实测（这套改动是否真的修好了的唯一证据）

每处改动都要**注入回旧行为**，确认新断言会响，然后还原：

| 注入 | 应当响的断言 |
|---|---|
| `if tools:` 分支改回 `if reasoning:` | 无推理的段也带 `reasoning_content` |
| `_ask_model` 改回 `PartType.TEXT` | 请求失败时不写成模型正文 |
| `remember` 的 pattern 改回参数原文 | 同工具的**另一条**命令也放行 |
| 弹窗文案改回「本会话总是允许」 | 弹窗上写着 a 键的真实范围 |
| `BASH_TIMEOUT` 改回 120 | 装配处传的是加长后的超时 |
| 系统提示删掉新那句 | 系统提示里写着「每次调用是新的 shell」 |

最后一行**要额外验一次压缩**：删掉那句后 `check_loop` 的比例断言应当回到通过，
说明超支确实来自提示本身，而不是压缩机制坏了。

## 4. 本轮不做

- **`cd` 跨调用保持**（给 bash 维护一个常驻 shell）——那要引入会话级进程与回收策略，
  与本轮「写清事实」是两种做法。本轮只写提示。
- **bash 的「命令级」记忆**（按命令前缀记）——已确认不走这条。
- **权限规则持久化到配置文件**（现在的 a 键只活在本会话）——`permissions.toml`
  已经提供了持久化的正规入口，按 a 仍然只记本会话。
- **`_summarize` 失败分支的 Part 类型**——它用 `COMPACTION` 带失败文案，语义已经对。
- 问题一的 B 类（`unknown tool: read`）与 `pwd` 那两条**不再改代码**：前者是注册表
  当时只有 bash（现已是五个），后者是环境的客观事实，提示写清即可。

## 5. 待确认

1. **`EMPTY_REASONING` 的文案**：写「（本步没有推理输出）」是否合适？
   它是给服务端做一致性校验用的，模型也会读到。倾向上面的写法。
2. **占位文案要不要按 step 区分**：例如「第 2 步没有推理输出」。倾向不带序号——
   序号在翻译层拿不到可靠的对应。
3. **`BASH_TIMEOUT` 提到多少**：600 秒（10 分钟）还是 1800 秒（30 分钟）？
   倾向 600——再长的话用户在中途没有任何反馈，反而像失去响应。
4. **弹窗上要不要显示「这条记忆的范围是 bash 全部」**：倾向只改键位描述与提示行，
   不在弹窗正文里再写一遍，避免把「这一次的参数」挤下去。
