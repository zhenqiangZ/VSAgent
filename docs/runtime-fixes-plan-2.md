# 运行期修复计划（第二轮）

日期：2026-11-11
依据：`ses_0029`（2026-11-11 10:22–10:25，一条用户请求，24 步，85.2 万 token，退出原因 `exhausted`）。

## 一、这份文件管什么

第一轮计划（`docs/runtime-fixes-plan.md`）处理了重复答复、上下文计量、事件日志等问题。这一轮处理 `ses_0029` 暴露的六件事：工具定义过大、计划被执行动作挤掉、步数用满、单来源口袋被说成全集、假设工具重试、主题重复。

## 二、观测到的数据

| 项 | 数值 |
| --- | --- |
| 核心工具定义 | 第 2 步 8,409 token |
| 装入 10 族后的工具定义 | 第 3 步起 25,870 token，此后每一步都是这个数 |
| 同一时刻的正文内容 | 1 万–2.4 万 token |
| 轮末工具定义 | 26,560 token |
| 累计输入 | 852,354 token |
| 退出原因 | `exhausted`（24 步用满） |

工具定义比正文还大，是这一轮最明显的成本问题。根因有两条：`load_tools` 的说明写「一次把这一轮要用的族都装上」，模型一次装了 10 族；`prune_families` 的 `loaded_at >= topic_index` 又把同一主题装进来的族全部保住，第一个主题结束时一个都没卸。

## 三、六项方案

### 3.1 工具租约：按主题装载，主题结束全部释放

1. 每个领域族在装入时记一个租约：`family_topic[family] = 当前主题号`。
2. `close_topic` 增加 `next_tools`（数组，1–3 个族）。主题封存时所有领域族释放，只预装 `next_tools` 里的族。
3. 没有 `next_tools` 就只留核心工具；模型下一步需要什么再装。
4. 提示词从「这一轮」改成「这一主题」，一次装 1–3 族；`load_tools` 的返回带上这一族大约多少 token。
5. `_after_seal` 调 `release_families(session, keep=next_tools)`，不再用 `loaded_at >= topic_index` 保住整批。

代价：主题边界会改工具清单，但封存本身已经改了事件块与摘要，前缀本来就要失效；真正要避免的是主题内部反复装卸。

### 3.2 交付截止线：计划必须先有文件

1. 一轮开始时记 `deliverable`：`kind`（计划 / 枚举 / 比较 / 决策）、`path`、`coverage`。
2. 剩 4 步时如果计划文件还不存在，收束阶段只允许 `write`、`close_topic`、`task`，并明确要求写计划骨架；剩 1 步时不再发工具。
3. `_finish` 检查交付物：计划类必须有文件，枚举类必须有覆盖账本；缺了标「未完成」。

### 3.3 全集与覆盖：不许把单来源说成「唯一口袋」

1. `detect_pockets` 增加 `sources="all"`，等于 `ligand,complex,predicted`；`limit` 拉到上限。
2. 工具输出列出「未查来源」，例如「ligand 1 处；complex 未查；predicted 未查」。
3. 枚举类交付必须有 `total / reported / excluded / sources`，数字对不上不算完成。
4. 计划类任务也要写清口袋范围，不用「唯一」这种词。

### 3.4 假设工具：放宽两条硬规则

1. `key` 上限从 12 提到 32。
2. `discriminator` 可以提对手的 key、主张或中文名，不再要求逐字出现英文 key。
3. 校验失败时给出可用的 key 或补全建议。

### 3.5 主题卫生：同一目标不要收两次

1. `close_topic` 幂等：当前段没有新产物、且标题与上一条相似时拒绝，提示「已经收过，只是记账请用 task」。
2. 轮末只剩 `bash` / `load_tools` 这类工具、没有产物和正文的段，不单独封主题。
3. `_TOOL_LABELS` 补上 `load_tools`（装载工具）、`list_workspace`（查看工作区）。

### 3.6 确认点：按用户决定不做硬闸门

只改事实表述：没有用户确认时不能写「pocket is confirmed」，应写「口袋已定义，待你确认」。

## 四、实施顺序

1. **3.1 工具租约 + 按主题最小装载**（本轮先做）。
2. 3.2 交付截止线。
3. 3.3 全集与覆盖。
4. 3.4 假设放宽 + 3.5 主题卫生。
5. 3.6 表述修正。

## 五、第一轮验收

- `ses_0029` 同形场景：第一个主题封存后，工具定义从 25,870 降到核心 8,409 加 `next_tools` 的 1–3 族（约 1.2 万–1.5 万）。
- 第二个主题第一步不再背着 10 族。
- 没有 `next_tools` 时，第二主题从核心工具开始，模型按需装载。
- `tests/test_agent.py` 的工具族与主题用例全过。

## 六、第一步实施状态（2026-11-11）

3.1 已实施：

- `Session` 增加 `next_tools`（只在内存里，封存时消费一次）。
- `close_topic` 增加 `next_tools` 参数；族名写错时当场拒绝，不留下半个标题。
- `toolset.release_families` 取代生产路径上的 `prune_families`：主题结束一律释放领域族，只预装 `next_tools` 里的 1–3 族。旧的 `prune_families` 保留给旧测试与对照。
- `loop._seal_topic` 改为走 `_after_seal`：手动 `close_topic` 以前绕过了族收敛，这是 `ses_0029` 里 10 族一直不卸的直接原因。
- `load_tools` 的返回带上每族大约多少 token；本主题超过三族时提示「装得太多」。
- 提示词从「一次把这一轮要用的族都装上」改成「这一主题 1–3 族」，并说明主题结束会卸载。

验收结果：

- `tests/test_agent.py` 全量通过。
- `tests/test_smoke.py` 全量通过。
- `--only tool-family`、`--only topic`、`--only event-log`、`--only loop`、`--only prompt` 通过。
- 预装用例：`close_topic(next_tools=["pocket"])` 之后，下一步请求里出现 `detect_pockets`，没有出现 `search_pdb`。
- 释放用例：第一轮装 `scoring`，轮末 `tool_families == []`，下一轮请求里不再有 `dock`。
- `tools/check_prefix.py` 通过：固定段 1742 token，整段前缀 5286 token。

3.2–3.6 尚未实施。

