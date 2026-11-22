# 工具清单

> 生成物，不要手改。改完工具或角色配置之后跑 `python tools/gen_tools_doc.py`；`tools/check_tools_doc.py` 会逐字节比对。

本文列出 VSAgent 当前装配的全部工具，按「常驻」与「领域」两层分开写。每一条给出用途、参数与关键限制。

数据来源是 `vsagent/core/agent/coretools.py` 的 `build_core_tools()` 与 `vsagent/core/agent/domain/all_tools.py` 的 `domain_tools()`，共 **71 个**：常驻 17 个，领域 54 个（23 个模块）。模型实际能看见哪些，由 `roles/agents.toml` 里的角色名单决定。

---

## 0. 怎么读这份清单

**清单由角色决定。** 每个角色能看见哪些工具，写在 `vsagent/core/agent/roles/agents.toml` 里：每个角色一条名单，会话开始时就定下来，中途不能自己装卸。常驻工具按三档装配（`orchestrator` / `worker` / `full`），领域工具逐条列出。改名单只改这个文件，不用动代码。

**根会话只编排。** 有可执行计划时，根会话拿到的只有 `orchestrator` 档的常驻工具，领域结果随 `dispatch_campaign` 的返回带回。要算东西就派给对应角色，不要指望根会话自己调领域工具。

**参数表的读法。** 「必填」指请求里必须带这个键，缺了工具直接报错；「选填」指可以不带，不带时按说明里的默认值处理。类型取自工具声明的 JSON Schema，`array` 表示字符串数组。

**引用一律用编号。** 登记表里的产物用 `receptor#1`、`ligandset#3`、`artifact#4` 这样的编号引用。领域工具里只有 `import_structure`、`import_ligandset`、`import_documents`、`record_assay` 收本机路径，因为这几样的来源在磁盘上，模型编不出来。其余领域工具收路径一律视为错误用法。

**写盘类工具有预演。** `prepare_receptor`、`prepare_ligands`、`fit_affinity`、`switch_receptor` 默认只报告会发生什么，带 `confirm=true` 才真正执行；`dispatch_campaign` 的 `confirm` 是必填项，传 `false` 只返回计划预览。第一次调用不带 `confirm`，看过影响范围再决定。

**需要本机依赖的工具。** 化学相关的工具（准备受体、准备配体、相似度、形状、对接、类药性、构效关系、药效团）需要 RDKit、Meeko、Vina 等依赖；亲和力那两件需要独立的 DTA 环境；`analyze_pose` 的相互作用指纹需要 PLIP。缺依赖时工具返回安装指引，不抛异常。

---

## 1. 常驻工具（17 个）

常驻工具的顺序就是请求体里的顺序。子会话看不到 `dispatch_campaign`。

### 1.1 文件与命令

#### bash

当没有专用工具可用时（要查环境、跑项目自带脚本），执行一条 shell 命令，返回合并后的标准输出与标准错误。读文件、搜文本、改文件不要用它——`read` / `grep` / `write` / `patch` 会留下可核查的记录。每调用一次都是一个全新的解释器，换目录要写在同一句里（`cd 目录 && 命令`）；默认 600 秒超时，超时会杀掉整棵进程树。命令会改动磁盘，执行前先确认；非零退出码不算异常，按输出判断。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `command` | string | 是 | 要执行的命令。换目录写 `cd 目录 && 命令`，不要拆成两次调用；只读命令也走这里。示例：`ls -la runs \| head` |

#### read

当你要看一个文件的内容时用它。要看已经移出上下文的会话某一步，用 `read_turn`；要读本地语料原文用 `read_source`；要读论文正文用 `read_fulltext`。读取时每行带文件里的绝对行号，默认最多 2000 行；用 offset 与 limit 读其中一段。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 文件路径，相对路径按工作目录解释 |
| `offset` | integer | 否 | 从第几行开始读，从 1 数起；默认 1 |
| `limit` | integer | 否 | 最多读多少行，默认 2000 |

#### write

新建文件、或整份重写一个文件时用它：原有的内容会被整份替换掉，覆盖之后不可撤销。父目录不存在时会自动建出来。只改其中一处时用 `patch` 更安全。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 文件路径，相对路径按工作目录解释 |
| `content` | string | 是 | 写进去的完整内容 |

#### patch

改文件里的一处时用它，不要整份重写（那是 `write`，覆盖后不可撤销）。old 必须在文件里唯一出现，出现零次或多次都会失败；失败时把 old 写长一些，带上前后几行把位置定下来。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 文件路径，相对路径按工作目录解释 |
| `old` | string | 是 | 要被替换掉的那段文字，要在文件里唯一出现 |
| `new` | string | 是 | 换上去的那段文字，写空字符串表示删掉这一段 |

#### grep

当你要在一段代码或文本里找某个标识符或模式时用它；要看命中处的上下文，再用 `read` 按行号读。按正则表达式搜索文件内容，输出命中的文件、行号与那一行。path 可以是目录也可以是单个文件；glob 用来限定文件名，不给出时只搜代码与常见文本后缀。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pattern` | string | 是 | 要搜的正则表达式 |
| `path` | string | 否 | 搜哪棵目录或哪个文件，默认当前工作目录 |
| `glob` | string | 否 | 文件名通配，例如 *.py |
| `ignore_case` | boolean | 否 | 是否不分大小写，默认分 |

### 1.2 记忆、任务与假设

#### remember

把一条项目事实写进项目记忆，跨会话保留。只记打分与筛选之前就存在的东西：受体、结合位点、配体库、参考活性物。**引用一律写文件路径**（相对路径按工作目录解释），不要写 handle——handle 只在本次会话有效，换个会话就指向别的东西。kind=preference 只记用户明确说过的长期做法，没有明确表态时用 fact；kind=experience 记带条件与结果的教训。只在用户明确要求、或一条事实确实值得跨会话复用时才调用。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | 是 | 一行事实，写清楚是什么、在哪个文件里 |
| `kind` | string | 否 | 可选值：`fact` / `preference` / `experience`。记忆类型，默认 fact。习惯不写引用文件 |
| `refs` | array[string] | 否 | 这条事实引用的文件路径，可以给多个；不给也行 |
| `ttl_days` | number | 否 | 多少天后自动过期；不给就是长期有效。适合会变的临时结论 |
| `conflicts` | array[string] | 否 | 与哪几条已有记忆矛盾（写它们的编号）。两条都会保留，显示时带一行「冲突，未解决」；只有确实对不上时才标，数值更新用 supersedes 那种说法写新的一条即可 |
| `source_type` | string | 否 | 可选值：`user` / `experiment` / `literature` / `database` / `corpus`。这条内容的来源类别，默认 user（用户确认）。文献、数据库、语料与实验结果要带 refs；原始查询结果不要写进长期记忆。 |

#### recall

按关键词检索项目记忆，返回之前记住的项目事实。在向用户追问「上次那个受体在哪」之前先查一次——记忆是跨会话的，上一轮做过的事情这里可能已经有记录。留空 query 则返回全部记忆。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 否 | 关键词，例如受体编号或文件名；留空则返回全部 |
| `kind` | string | 否 | 可选值：`fact` / `preference` / `experience`。只看某一类：fact 项目事实 / preference 用户长期做法 / experience 带条件的负结果；不给则三类都返回 |

#### task

当用户提出新要求、完成一步、或遇到阻塞时，更新本会话的任务状态。记四类：目标、用户提出的约束、已完成、待办、阻塞。这段状态每一步都会完整发给模型，不会因为对话变长被丢掉，所以**用户提的要求一律记进 `constraints` 参数**，别指望聊天记录还记得住。只填这次真正要改的字段，没填的字段不动。`constraints` 是累加的：这次没写出来的旧约束不会被删掉。这里记的是本会话自己的要求。派发子 agent 时不要把整份清单抄过去；按每个角色这一段的任务，在 dispatch_campaign 的 constraints 里写它该遵守的那几条。done / todo / blockers 是整段替换：要改就把这一类完整重写一遍，传空数组表示这一类清空。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `goal` | string | 否 | 本会话要做成什么，一句话；传了就覆盖原来的目标 |
| `constraints` | array[string] | 否 | 用户新提出的要求，逐条追加；已有的约束不会被覆盖。 |
| `add_constraints` | array[string] | 否 | 与 constraints 相同，保留的旧写法 |
| `done` | array[string] | 否 | 已完成的步骤，整段替换 |
| `todo` | array[string] | 否 | 待办步骤，整段替换 |
| `blockers` | array[string] | 否 | 当前卡住的地方，整段替换 |

#### hypothesis

**你的产出单位是假设，不是排名。** 一条假设必须能判真假：主张、机制、证据、检验方法、可量化的预期、否证条件。

`test` 与 `expected` 是硬门槛，缺了就是印象；`expected` 要有数、阈值或对照（例如「IC50 < 100 nM」）。

两类都要提：`kind=strategy` 是这一轮该怎么筛，`kind=mechanism` 是这批分子为什么活。只谈机制会在错的库上做精细分析，只谈策略则永远不知道为什么要这么筛。

两条假设给出**不同预期**时写 `rivals` 与 `discriminator`；同一批竞争假设用 `action=group` 一次提出，组内用 key 互指，任何一条不合法就整组不写。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `action` | string | 是 | 可选值：`propose` / `group` / `update` / `close` / `list`。propose 新建一条 / group 一次新建 2–4 条互指假设 / update 修订未结的 / close 结案 / list 看全部 |
| `id` | string | 否 | 假设编号，如 hyp#1；update / close 必填 |
| `kind` | string | 否 | 可选值：`strategy` / `mechanism`。strategy（这一轮怎么筛）/ mechanism（为什么活） |
| `mode` | string | 否 | 可选值：`expand` / `narrow`。策略假设必填：expand 组外找新骨架 / narrow 组内优化 |
| `claim` | string | 否 | 主张，一句话，必须可判真假 |
| `mechanism` | string | 否 | 机制：你认为它为什么成立 |
| `evidence` | string | 否 | 已有证据与出处（handle / 文献引用号） |
| `test` | string | 否 | 怎么检验：做什么实验或算什么 |
| `expected` | string | 否 | 预期结果，要含数值、阈值或对照，例如「IC50 < 100 nM」 |
| `rivals` | array[string] | 否 | 与之竞争的假设编号（如 hyp#2）。只在两条假设给出不同预期时填，且要写 discriminator 说明哪个结果能区分它们 |
| `discriminator` | string | 否 | 与对手靠什么结果区分：什么结果支持本条、什么结果支持对手。写了 rivals 就必填；不要只写在 test 里 |
| `items` | array[object] | 否 | action=group 时的一组假设，2–4 条，一次写入；组内用 key 互指 |
| `status` | string | 否 | 可选值：`supported` / `refuted` / `inconclusive`。close 时的结论：supported / refuted / inconclusive |
| `result` | string | 否 | close 的依据：实验产物 handle 或一句话 |

### 1.3 会话历史

#### read_turn

按步号取回会话里某一步的原文（那一步的助手正文与工具输出）。工具输出被归档成一行占位、或整步已经移出上下文时，用占位行与省略清单里的 step 序号读回。默认不含思考过程，要当时的推演传 reasoning=true。offset / limit 按行分页，默认 200 行；每次最多返回 4000 token。要看文件当前内容用 `read`，按主题找历史用 `recall_event`。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `step` | integer | 是 | 步号（省略清单里 step N 的 N） |
| `offset` | integer | 否 | 从第几行开始，默认 0 |
| `limit` | integer | 否 | 最多返回多少行，默认 200，上限 400 |
| `reasoning` | boolean | 否 | 是否带上当时的思考过程，默认 false |

#### search_history

按关键词检索已封存的历史主题，返回主题编号、结论、产物与原文位置。用户提到「上次」「之前」「刚才讨论过的」而你说不出是哪一段时用它；默认只查本会话；scope=all 查这台机器上的全部会话（不含子 agent），命中行带会话编号，原文用 recall_event session_id=… event=… 取回。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 要查的词，例如「结合位点」「对接分数」「4HJO」 |
| `scope` | string | 否 | 可选值：`current` / `all`。current 只查本会话（默认）；all 查全部会话 |
| `limit` | integer | 否 | 最多返回几个主题，默认 8，上限 20 |

#### close_topic

一个主题做完时调一次。**这一步结束时封存**，这一段的原文不再随请求发出，只留标题、结论与流水；要看细节用 recall_event。原文取不回来，依据只能写进结论。

时机：当前目标已经达成，或者工作要换到一件不相干的事上。步数多不代表必须收；只有目标完成或方向改变时才收。还有未决事项或失败工具时，summary 要写明没做到的部分。

它自己不算一步。同一个目标还没做完就别调——切成两半会让两个主题都读不通。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | string | 是 | 不超过 40 字，写**做成了什么**（「定口袋」），不写「调用了什么工具」 |
| `summary` | string | 否 | 不超过 1000 字，这个主题的结论：做出了什么（handle、路径、关键数字，写不准就写「未产出」，不要编）、放弃过什么、还差什么。不写就用本地拼的「N 步、产物、未决」。 |
| `handoff` | string | 否 | 不超过 200 字，后续依赖这个主题的什么：哪个产物、哪个数字、哪条限制。没有依赖就写「无」。 |
| `session_title` | string | 否 | 不超过 20 字，整个会话的名字；只在第一次封存时传，会话已经有名字就不传。 |

#### recall_event

不带参数时列出全部主题；带 event 编号时给出该主题的流水、结论与原文。带 step 时只给那一步的原文（工具输出正文）。封存之后只有摘要留在上下文里，要核对参数或原始输出时用它。默认读当前会话；session_id 指向别的会话，跨会话检索的命中行里带着它。默认不含思考过程，要当时的推演传 reasoning=true。原文按 offset / limit 分页，默认 120 行，每次最多 4000 token。不知道编号时先用 `search_history` 找主题；文件内容用 `read`。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `event` | string | 否 | 主题编号，例如 evt_0003 |
| `session_id` | string | 否 | 另一个会话的编号，例如 ses_0019；不带时读当前会话 |
| `step` | integer | 否 | 只看这一步的原文；步号在流水里每行开头 |
| `offset` | integer | 否 | 原文从第几行开始，默认 0 |
| `limit` | integer | 否 | 最多返回多少行，默认 120 |
| `reasoning` | boolean | 否 | 是否带上当时的思考过程，默认 false |

### 1.4 开工盘点

#### survey_structures

把一个靶点的**全部实验结构**铺开：命中多少套、每一套里有什么，按配体分组，每组给套数、最佳分辨率与代表条目。**这是全景，不是挑几套。**

用户没有指定具体结构（只说了靶点名、或「看看这个靶点」）时先调它；用户已经点名某个编号时不必调。

一套结构可能出现在多组：小分子配体按 `chem_comp` 代号分组，结晶添加剂、离子、糖与不属于本靶点的聚合物链各自单列，组内计数之和会大于结构总数。

不下载坐标、不算口袋、不核对序列、不判哪个配体算「真配体」。产物是一份 `kind=survey` 的 artifact，全表在页面里（用 `open_viewer` 取地址）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `accession` | string | 是 | 靶点的 UniProt 编号，例如 Q15858 |
| `method` | string | 否 | 只看某种实验方法，例如 ELECTRON MICROSCOPY、X-RAY DIFFRACTION；不给就全部 |
| `ligand_class` | string | 否 | 可选值：`any` / `small_molecule` / `other_polymer` / `apo`。只看某一类结构：any 全部（默认）；small_molecule 含小分子配体（已排除离子、缓冲液、糖）；other_polymer 含不属于本靶点的聚合物链；apo 两者都没有 |
| `limit` | integer | 否 | 最多取回多少套的明细，默认 400，上限 1000 |

### 1.5 呈现与交付

#### write_run

把一个受体（可选的对接产物）写成**运行目录**（`meta.json` / `receptor.pdb` / `ligands/`），登记成 artifact，网页端据此渲染结构页；两个 view 都会先做预处理。

口袋来源三选一：给了 `pockets` 直接用那份口袋集（`detect_pockets` 的产物）；给了 `candidates` 就走先验（核对编号与链序列，按 CA 叠合后用共晶配体圈口袋，`source=ligand`）；都没给就搜该蛋白的带配体复合物，一个都没有才转 p2rank。三路都拿不到就写空口袋并说明原因。

`uniprot` 是蛋白编号（如 P00519），**不是** PDB 编号；只给 `pdb_id` 会让主来源只剩手上这一份结构。center / score / min_distance 一律现算，不要自己编。

返回运行目录的 artifact handle 与结构页地址，附本次处理摘要；失败时说明卡在哪一步，旧 meta.json 不会被改动。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 受体条目，例如 receptor#1 |
| `ligands` | string | 否 | 配体集条目，例如 ligandset#2；给了 docking 就别传 |
| `docking` | string | 否 | 对接产物 handle，例如 artifact#4；会写入最佳位姿与分数 |
| `run_id` | string | 否 | 运行目录名（字母数字与 . _ -），留空按 pdb_id 生成 |
| `uniprot` | string | 否 | 蛋白的 UniProt 编号（如 P00519），主来源据此搜全部复合物 |
| `pdb_id` | string | 否 | PDB 条目号（如 1IEP），只在主来源为空时查预测 |
| `name` | string | 否 | 运行显示名 |
| `candidates` | string | 否 | 先验清单：PDB 编号，逗号或空格分隔（如 "1M17,2ITY,4HJO"）。给了它就不再按 UniProt 检索复合物 |
| `pockets` | string | 否 | 口袋集 handle（detect_pockets 的产物）。给了就直接用它，不再自己算口袋——四路叠合的结果原样上网页 |
| `affinity` | string | 否 | 亲和力产物 handle（kind=affinity，score_affinity 的产物）。与 docking 一起给；按分子 id 把 pK 写进 molecules[].affinity_pk |
| `interactions` | string | 否 | 可选值：`none` / `top` / `all`。接触与作用：top（默认）所有位姿算几何接触与几何氢键、前 8 个再算 PLIP；all 给全部位姿加 PLIP；none 不算 |
| `view` | string | 否 | 可选值：`pockets` / `structure`。pockets（默认）算口袋；structure 只展示结构，不联网 |

#### open_viewer

给一个已写出的产物返回它在**本地网页**上的地址。运行目录形如 `http://127.0.0.1:<port>/#/structure/<run_id>`；`survey_structures` 写的结构全景形如 `#/survey/<survey_id>`。

消息里出现具体的结构、口袋、位姿或相互作用时，必须用这个工具取地址；不要自己拼接。默认只返回地址，需要打开浏览器时传 open_browser=true。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `run` | string | 否 | 产物条目引用，例如 artifact#3（运行目录或结构全景）。留空则指向首页 |
| `open_browser` | boolean | 否 | 是否顺手用系统默认浏览器打开。默认不开 |
| `pocket` | string | 否 | 口袋编号，例如 P2；给了就打开该口袋的视图 |
| `molecule` | string | 否 | 分子 id，例如 mol-7；给了就打开该分子的详情与作用线 |
| `view` | string | 否 | 可选值：`local` / `whole`。pocket 的取景，默认 local（局部）；whole 是整份结构 |

### 1.6 多 agent

#### dispatch_campaign

当任务规格已经明确、需要专业角色分工时，用这个工具排一次计划并派发。它按任务规格里的子目标决定派谁、谁等谁，能并行的并行。`roles` 只能从计划里选子集（上游会自动补上），不能加计划外的角色；只选一个角色时 `task` 可以改写任务书。

`constraints` 由你按每个角色这一段的任务写：键是角色名或 global，值是要求原文。它不从父会话自动抄；没写的角色只带角色说明书里的固定限制。confirm=true 时必填，没有额外限制就传 {}。

路由自动补出来的上游角色在任务规格里没有子目标；给它们写 `plans`（每个角色一段 goals 与 acceptance_criteria），否则 confirm=true 会拒绝派发。任务规格里已经有子目标的角色也可以补写，条目只增不减。

不适用：一次调用就能做完的事、或下一步要用到你此刻的推理时，不要派（子 agent 看不到你的推理）。缺任务规格时先让用户把目标、对象与交付物说清楚。

confirm=false 只返回计划预览（角色、谁等谁），不建会话、不跑计算；用户同意后再用 confirm=true。同一会话同时只能有一场；计划里已经跑完的角色再派一次会开新的子会话，不会把上一轮的结论原样带回来。

confirm=true 会一直等到所有子 agent 结束，再带回每个角色的结论与报告判定；用户中断时这场执行连子 agent 一起停。

副作用：每个角色新建一个子会话，产物写进共享的运行目录与登记表。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `confirm` | boolean | 是 | false=只预览计划（推荐第一次这样调用）；true=真正启动并等全部结束。必填，没有默认值。示例：{"confirm": false} |
| `roles` | array[string] | 否 | 只派这几种角色（括号内的名字，如 pocket）；上游会自动补上。只能从当前计划里选，计划外的角色会被拒绝。不给则整份计划都派 |
| `constraints` | object | 否 | 每个子 agent 的限制。键是角色名（如 pocket）或 global（写给所有角色），值是字符串数组。按这个角色这一段的任务写它该遵守什么；不写就没有额外限制。confirm=true 时必填，没有就传 {} |
| `task` | string | 否 | 自定义任务书，只给 roles 指定的那一个角色用；不给则按子目标自动拼。同一个角色不要再写 plans.goals |
| `plans` | object | 否 | 按角色补写的计划。键是这次要派的角色名；每格给 goals 与 acceptance_criteria 至少一项。它只影响这一次投影，不写回任务规格 |
| `timeout` | number | 否 | 整场最多等多少分钟（1–720）。留空按每个角色说明书里的时限；到点未完成记失败，不再等 |

---

## 2. 领域工具（23 个模块，54 个）

领域工具按模块列，模块顺序与装配顺序一致。

### 2.1 `listing` 浏览登记表（2 个）

#### registry

登记表里有哪些产物，某个编号指的是哪一份？

**输入：** handles 填编号清单，逐个展开；不填则列清单，可用 kind 与 include_archived 过滤。

**输出：** 清单模式报当前受体与全部条目，每行开头就是可直接使用的 handle；展开模式给出路径、摘要、指纹、血缘、文件状态与附带字段。

**边界：** 路径只供核对，传参一律用 handle；文件状态是现核对磁盘的结果，标出异常的那几条不要直接拿去算。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `handles` | array[string] | 否 | 要展开的编号，例如 ["receptor#1", "artifact#3"]；留空或不给则列出清单 |
| `kind` | string | 否 | 可选值：`receptor` / `ligandset` / `artifact`。清单模式下只看某一类；留空则全看 |
| `include_archived` | boolean | 否 | 清单模式下连被搁置的条目一起列出。切换受体后旧受体的产物会被移出默认视图，只有这一项为 true 才看得到它们 |
| `include_lineage` | boolean | 否 | 展开模式下是否回溯完整血缘（这个产物由哪些东西一步步算出来的），默认 true |

#### switch_receptor

这个会话现在用的是哪一份受体，换一份会作废哪些结果？

**输入：** receptor 填受体 handle；confirm 默认 false，只预演影响范围。

**输出：** 预演列出会移出默认视图的条目；confirm=true 才真正切换，文件保留在磁盘上。

**边界：** 只改默认作用对象，不删除任何产物；旧产物用 registry(include_archived=true) 仍可查到。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 目标受体，例如 receptor#2。**不要传文件路径** |
| `confirm` | boolean | 否 | true 才真正执行；false（默认）只预演影响范围 |

### 2.2 `uniprot` 靶点信息（1 个）

#### target_lookup

这个靶点在 UniProt 里是哪一条，它的功能与可用结构是什么？

**输入：** query 与 accession 二选一。query 填名称、基因名或 UniProt 检索式；accession 填编号（形如 P00533）。

**输出：** 按名称查给候选清单（编号、基因名、长度、是否人工审阅、蛋白名与物种）；按编号查给功能、定位、结构域与实验结构，并把条目登记成 artifact。

**边界：** 编号必须是查出来的，不要自己拼；同一编号再次调用读本地缓存，只有确认远端注解已更新才带 refresh=true。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 否 | 检索词，例如 EGFR、epidermal growth factor receptor、kinase inhibitor。可带 UniProt 检索语法（gene:EGFR、organism_id:9606） |
| `accession` | string | 否 | UniProt 编号，例如 P00533；可带同工型后缀（P00533-2）。填了这一项就不再按名称检索 |
| `organism` | string | 否 | 限定物种，可填 taxid（9606）、英文/中文常见名（human、小鼠）或学名（Sus scrofa）；留空则跨物种检索 |
| `reviewed_only` | boolean | 否 | 只取人工审阅的条目（Swiss-Prot）。默认 true；这类条目里没有命中时会自动放宽并说明，不会让你空手而归 |
| `limit` | integer | 否 | 最多返回多少条候选，上限 50（再多就该细化检索式，而不是翻页） |
| `refresh` | boolean | 否 | 按编号查时忽略本地缓存强制重新下载（认为远端注解已更新时才用） |

### 2.3 `rsearch` 实验结构检索（1 个）

#### search_pdb

某条蛋白或某组条件下有哪些 PDB 结构，各自的配体是什么？

**输入：** uniprot 走属性检索（结果可复现），只给 query 走文本检索；两个至少给一个，查某个蛋白有哪些结构优先用 uniprot。

**输出：** 候选清单（条目号、标题、分辨率、实验方法、配体代号与名称）与命中总数。

**边界：** 这是候选，不筛配体、不判结合位点；文本检索的结果随说法变，不要用它猜条目。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 否 | 文本检索词，例如 Nav1.7 ProTx-II。与 uniprot 同时给出时，以 uniprot（属性搜）为准 |
| `uniprot` | string | 否 | UniProt 编号（如 Q15858）。给出它就走属性搜，按该蛋白列结构 |
| `ligand` | string | 否 | 配体**代号**（chem_comp id，三位字母数字，如 STI、9SR）。给出它则按「含该配体」筛。名称（如 tetrodotoxin）要先换成代号，`survey_structures` 会给出这个靶点的代号与常用名 |
| `method` | string | 否 | 实验方法（如 X-RAY DIFFRACTION）。给出它则按方法筛 |
| `limit` | integer | 否 | 最多列多少条，上限 100；命中更多时会在末尾写明 |

### 2.4 `structure` 实验结构与预测结构（5 个）

#### fetch_structure

这个 PDB 条目里有什么，坐标能不能直接用于对接？

**输入：** pdb_id 填四位条目号，必须来自 target_lookup 的结果；refresh=true 才重新下载。

**输出：** 坐标写盘并登记成状态 raw 的受体，返回方法、分辨率、每条链的残基范围、共晶配体、水与交替构象。

**边界：** UniProt 编号与 AlphaFold 编号都不是条目号；大条目只提供 mmCIF，本机没有 gemmi 时会明确报错。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pdb_id` | string | 是 | 四位的 PDB 条目编号，例如 1M17（大小写不敏感） |
| `refresh` | boolean | 否 | true 时忽略本地文件强制重新下载（认为远端条目已修订时才用） |

#### fetch_alphafold

这个靶点有没有 AlphaFold 预测模型，模型可不可信？

**输入：** uniprot_id 填 UniProt 编号或 AF- 编号；refresh=true 会连坐标一起重下。

**输出：** 只取 AF-<编号>-F1，写盘并登记成状态 raw 的受体，附逐残基 pLDDT 与低置信区。

**边界：** 这是预测结构，没有配体、水与分辨率；口袋残基的 pLDDT 普遍低于 70 时，位姿只能当排序参考。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `uniprot_id` | string | 是 | UniProt 编号（P00533）或模型编号（AF-P00533-F1） |
| `refresh` | boolean | 否 | true 时忽略本地坐标重新下载（AFDB 发了新版本才用） |

#### import_structure

本机这份坐标文件能不能导进来，之前是不是已经导过？

**输入：** path 填本机 .pdb 或 .ent 文件，mmCIF 不收。

**输出：** 文件复制进结构目录，登记成状态 raw 的受体；内容相同的不重复登记，直接给出已有编号。

**边界：** 导入后一律用 handle，不要再传路径；判重按文件内容，不按文件名。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 本机坐标文件的路径，建议给绝对路径 |

#### assess_structure

这套结构的质量够不够用来对接，缺了哪几段？

**输入：** structure 填结构或受体的 handle。

**输出：** 实验方法、分辨率、模型套数、每条链声明与观测的残基范围、缺口、金属离子、共晶配体与交替构象。

**边界：** 没有 SEQRES 时缺口写「无法判断」，不报 0；NMR 的多套模型只统计第一套；只读，不登记。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `structure` | string | 是 | 受体 handle（raw 或 prepared 都收） |

#### align_structures

两套结构叠得上不上，哪个残基的位置差得最多？

**输入：** fixed 是参考结构，moving 是要搬过去的那一套；mode 选 chain（默认，整条链配对）或 residue（只按 residues 里列出的残基配对）。

**输出：** 整体 RMSD、差值最大的几个残基、把 moving 搬到 fixed 的 4×4 变换矩阵；不输出坐标。

**边界：** 只按 CA 原子算，不做侧链叠合；两套结构的链序列对不上时明说，不勉强给一个数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `fixed` | string | 是 | 参考结构或受体的 handle |
| `moving` | string | 是 | 要叠到参考上的结构或受体的 handle |
| `mode` | string | 否 | 可选值：`chain` / `residue`。chain 用整条链的 CA（默认）；residue 只用 residues 列的残基 |
| `residues` | string | 否 | mode=residue 时用：残基号，逗号分隔，例如 745,788,790 |
| `top` | integer | 否 | 偏差最大的残基列几个，默认 10，上限 50 |

### 2.5 `pockets` 结合口袋（2 个）

#### detect_pockets

这个受体上有哪些结合口袋，各自长什么样？

**输入：** receptor 必填；sources 选来源，auto（默认）按共晶配体、先验编号、复合物、p2rank 的顺序取第一个非空的，也可写 ligand,complex 叠加。

**输出：** 口袋集（中心、半径与盒子现算），每处附画像：配体的元素组成与尺寸、壁按芳香/疏水/极性/带电四类计数、最近残基与 3.5 埃内的残基数。

**边界：** 不接受模型传的坐标；脂质、磷脂与去垢剂先剔掉并报数；空集不是失败，会分清「确实没有」与「没查成」；画像里没有分数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 受体 handle（raw 或 prepared 都收），例如 receptor#1 |
| `radius` | number | 否 | 口袋残基判据（埃），默认 5，范围 3–8 |
| `padding` | number | 否 | 由中心算盒子时四周留多少埃，默认 8，上限 20 |
| `sources` | string | 否 | 来源，逗号分隔：auto / ligand / complex / prior / predicted。auto 只取第一个非空的来源 |
| `uniprot` | string | 否 | 蛋白编号（如 P00519），复合物来源要用；不给就只能看手上这份结构 |
| `candidates` | string | 否 | 先验清单：PDB 编号，逗号或空格分隔（如 "1M17,2ITY"） |
| `pdb_id` | string | 否 | 预测来源用的 PDB 编号；不给时沿血缘找，再没有就不走预测 |
| `limit` | integer | 否 | 最多列几处，默认 5，上限 20 |

#### define_pocket

口袋集里该选哪一处，或者按残基自己定一处？

**输入：** 三路互斥：pockets 加 index 选现成的，residues 写链:序号，center 与 size 给坐标；同时给两路直接报错。

**输出：** 单个口袋，登记成口袋集里的一条，供 prepare_receptor 的 pocket 使用。

**边界：** 盒子由代码算，残基那一路取重原子包围盒再四周外扩；写错的残基会连同该链的实际编号范围一起列出，不会静默丢掉。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pockets` | string | 否 | detect_pockets 的口袋集 handle，例如 artifact#3 |
| `index` | integer | 否 | 口袋集里的序号（从 1 开始） |
| `receptor` | string | 否 | 受体 handle；用 residues / center+size 时必填 |
| `residues` | string | 否 | 残基清单，链:序号，逗号或空格分隔，例如 "A:315,A:319,A:330" |
| `center` | string | 否 | 盒子中心，例如 "12.3,4.5,8.9"，与 size 成对给 |
| `size` | string | 否 | 盒子边长（埃），例如 "22,18,20"，与 center 成对给 |
| `padding` | number | 否 | 由残基算盒子时四周留多少埃，默认 8，上限 20 |
| `name` | string | 否 | 显示名，例如 ATP 位点 |

### 2.6 `pocketcmp` 口袋比较（2 个）

#### compare_pockets

本靶的口袋与抗靶的同名口袋差在哪几个残基？

**输入：** target 与 anti 填靶点编号；pocket 给 define_pocket 的产物，或直接用 residues 写残基（A:745,A:788）。

**输出：** 只报口袋中心 8 埃以内的残基，逐条写明抗靶那边更大还是更小、性质变了没有。

**边界：** 两个靶点的对接分数不能互相比较，蛋白不同、没有共同零点；选择性只能从实测活性算。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target` | string | 是 | 本靶的受体 handle |
| `anti` | string | 是 | 抗靶的受体 handle |
| `pocket` | string | 否 | 口袋 handle；与 residues 二选一 |
| `residues` | string | 否 | 本靶的残基清单，如 `A:745,A:788`；与 pocket 二选一 |
| `radius` | number | 否 | 只比口袋中心这么远以内的残基（默认 8） |

#### similar_pockets

本靶点没有可用配体时，哪个邻近受体的口袋最像、能借它的配体？

**输入：** accession 填 UniProt 编号；candidates 填候选编号（至多 8 个，逗号分隔）；residues 或 pocket 定窗口。

**输出：** 候选按口袋窗口的局部序列相似度排序，各带 ChEMBL 里 pChEMBL 不低于 7 的活性条数，并给一个推荐。

**边界：** 相似度高不等于活性多，哪一边不足都会写明；借来的配体排序误差更大，下一轮只挑 20 个左右做实测。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `accession` | string | 是 | 本靶的 UniProt 编号 |
| `candidates` | string | 是 | 候选受体的 UniProt 编号，逗号分隔，至多 8 个 |
| `residues` | string | 否 | 口袋残基的 UniProt 编号，如 `741,745,788` |
| `pocket` | string | 否 | 口袋 handle；结构编号会先对齐到 UniProt 序列 |
| `window` | integer | 否 | 窗口在两端各多取几个残基（默认 4） |
| `actives` | boolean | 否 | 是否查每个候选的活性条数（默认查；关掉会快很多） |

### 2.7 `chembl` 已知活性（2 个）

#### search_actives

这个靶点在 ChEMBL 里有哪些已知活性的化合物？

**输入：** accession 填 UniProt 编号；min_pchembl 是活性下限，排序与阈值都用 pChEMBL，不用原始浓度。

**输出：** 配体集（可直接交给对接），附总记录数、命中数、最强活性与有没有上市药。

**边界：** 默认只取单一蛋白靶点、精确定量、置信度 8 以上的记录；到达上限时留下的是活性最强的那些；分页取数，单页约 5–11 秒。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `accession` | string | 是 | UniProt 编号，例如 P00533；来自 target_lookup |
| `min_pchembl` | number | 否 | 活性下限，越大越严：6≈1 μM、7≈100 nM、8≈10 nM（默认 6） |
| `measure_types` | string | 否 | 定量测定类型，默认 IC50,Ki,Kd；加 EC50 是另一套量纲 |
| `min_confidence` | integer | 否 | 测定置信度下限，8 是直接测定、5 以下多为同源推定（默认 8） |
| `max_rows` | integer | 否 | 最多取回多少条，上限 5000，到顶留下活性最强的（默认 2000） |
| `refresh` | boolean | 否 | true 时忽略本地缓存重新下载 |

#### search_analogs

ChEMBL 里有没有和这个分子结构相近的已知化合物？

**输入：** query 填 ChEMBL 编号或 SMILES，有编号优先给编号；min_similarity 是 40–100 的百分比，85 约同一系列、70 约同一母核。

**输出：** 配体集，按相似度降序、按母体归并，查询分子自身已排除。

**边界：** 相似只看结构、与活性无关；阈值调到 50 以下动辄几千个分子；到上限时留下最像的那些。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | ChEMBL 分子编号（如 CHEMBL939）或一段 SMILES |
| `min_similarity` | integer | 否 | 相似度下限，40–100 的整数；低于 40 服务端直接拒绝（默认 85） |
| `max_rows` | integer | 否 | 最多取回多少个，上限 1000，到顶留下最像的 |
| `refresh` | boolean | 否 | true 时忽略本地缓存重新检索 |

### 2.8 `pubchem` 化合物身份（2 个）

#### fetch_compound

这个化合物是什么，是不是已经上市的药？

**输入：** compound 可以填名称、CID、SMILES、InChIKey 或 ChEMBL 编号；对接与枚举出来的分子没有编号，填 SMILES。

**输出：** PubChem 给 CID、分子式、分子量、规范 SMILES、InChIKey 与计算性质；ChEMBL 有记录时补最高临床阶段、撤市与黑框警告、类药性、作用靶点与适应症。

**边界：** 计算性质（XLogP、TPSA、氢键数、可旋转键）不是实验值；一个名字对多个 CID 时不补 ChEMBL，先确认是哪一个。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `compound` | string | 是 | 名称、CID（2244）、SMILES、InChIKey 或 ChEMBL 编号 |
| `synonyms` | boolean | 否 | true 时多取一次别名（常用名/商品名），最多列 8 个 |

#### search_similar_compounds

PubChem 全库里有没有和这个结构相近的已知化合物？

**输入：** smiles 必填；threshold 是 50–100 的相似度阈值，默认 90；limit 默认 10。

**输出：** 命中总数与前几条的身份（CID、名称、分子式、MW、XLogP、SMILES）。

**边界：** 接口不给相似度分数，不编一个；它与本地的 Tanimoto 不是同一套指纹，不能互相比较；阈值调低时命中会迅速变多。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `smiles` | string | 是 | 查询结构的 SMILES（可从 fetch_compound 或登记表拿到） |
| `threshold` | integer | 否 | 相似度阈值百分比，50–100，默认 90 |
| `limit` | integer | 否 | 最多列几个化合物，上限 20 |

### 2.9 `opentargets` 可成药性与临床证据（2 个）

#### check_druggability

这个靶点值不值得做，有没有可用的阳性对照？

**输入：** target 填基因名或 Ensembl 编号；disease_limit 与 drug_limit 控制关联疾病与临床分子的条数。

**输出：** 可成药性（小分子、抗体、PROTAC 三种模态的证据）、化学探针、已报告的安全性风险、临床分子与关联疾病。

**边界：** 关联分是证据加权后的排序分，不是概率；同一天内重复查同一个靶点读本地缓存；这些证据都没有的孤儿靶点，对接结果没有基准。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target` | string | 是 | 基因符号（EGFR）、蛋白名或 Ensembl 编号（ENSG00000146648） |
| `disease_limit` | integer | 否 | 列几条关联疾病，默认 5，上限 20 |
| `drug_limit` | integer | 否 | 列几个已知药物/临床候选，默认 5，上限 20 |

#### disease_targets

手上只有一个疾病时，哪些靶点值得先做？

**输入：** disease 填疾病名或 EFO / MONDO 编号；limit 控制返回条数。

**输出：** 关联靶点及证据分解（临床、遗传、通路、文献、体细胞突变）。

**边界：** 分数高的不等于好做，可能既没有结构也没有口袋，挑中之后用 check_druggability 复核；同一天内重复查读本地缓存。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `disease` | string | 是 | 疾病名（英文，如 lung adenocarcinoma）或编号（MONDO_0005061） |
| `limit` | integer | 否 | 列几个靶点，默认 10，上限 25 |

### 2.10 `literature` 文献检索（2 个）

#### search_literature

这个主题在文献库里有哪些候选，哪几篇有开放全文？

**输入：** query 填检索式；source 选 pubmed（默认，只查期刊文献）或 europepmc（含预印本与开放全文入口）。

**输出：** 候选清单（编号、标题、期刊、年份、作者与摘要片段）与命中总数；europepmc 另给 PMCID，可直接交给 read_fulltext。

**边界：** 这是候选，不含「哪篇相关、可不可信」的判断；摘要只是片段，要读正文用 read_fulltext。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 检索式，例如 SCN9A Nav1.7 cryo-EM complex，或 SCN9A AND "PDB:6J8I"（按 PDB 编号反查） |
| `limit` | integer | 否 | 最多列多少条，上限 50 |
| `source` | string | 否 | 可选值：`pubmed` / `europepmc`。pubmed（默认）只查期刊文献；europepmc 多含预印本，检索式里加 SRC:PPR 可只看预印本 |
| `sort` | string | 否 | 可选值：`relevance` / `cited` / `date`。source=europepmc 时的排序：relevance（默认）/ cited / date |

#### read_fulltext

这篇论文的正文里，方法与结果那一节写了什么？

**输入：** article 填 PMCID、PMID 或 DOI；section 按标题子串匹配，不给就返回摘要与可读章节清单。

**输出：** 那一节的正文，按 max_chars 截断，并在末尾写清本节原本多长。

**边界：** 只有开放获取的条目有全文，预印本与订阅文献会明说没有；本地语料里的原文用 read_source。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `article` | string | 是 | PMCID（PMC12545111）、PMID（41142240）或 DOI |
| `section` | string | 否 | 章节标题的子串，例如 results / methods / discussion；留空则返回摘要与章节清单 |
| `max_chars` | integer | 否 | 这一节最多返回多少字，默认 4000，上限 12000 |

### 2.11 `corpus` 本地语料（3 个）

#### import_documents

本机这批文献能不能放进项目语料库？

**输入：** path 填一个文件或目录，只收 .pdf、.md、.txt；目录只收它直接包含的文件，不递归。

**输出：** 逐条报新增、重复与失败；同一份文件按内容判重，不会产生第二份。

**边界：** 扫描件没有文本层，会照实失败，本轮不做 OCR；导入之后用 search_corpus 检索、read_source 按页读。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 本机文件或目录的路径，建议给绝对路径 |

#### search_corpus

本地语料里有没有这个问题的答案？

**输入：** query 填检索词；limit 控制返回组数。

**输出：** 每组一个引用号（doc#… 或 web#…）与 600 字以内的片段，同一篇的同一节聚成一组。

**边界：** 免费、离线，先查这里再联网；中文提问也能命中英文文献；本地没有不等于这篇文献不存在，网上有没有是 search_web 的事。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 检索词。中英文都行；查具体术语、方法名、编号比查整句更准 |
| `limit` | integer | 否 | 返回几组（按「源 + 章节」聚合），默认 8，最多 20 |

#### read_source

这个引用号对应的原文里，某一页写了什么？

**输入：** ref 填工具返回过的引用号；page 只对文献有意义；max_chars 控制长度。

**输出：** 原文片段；不给页时给开头一段，并告诉你有多少页。

**边界：** 只接受工具返回过的引用号，不接受文件路径；网页只存了摘要时会直说，要全文用 fetch_webpage。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ref` | string | 是 | 引用号，形如 doc#ab12cd34ef 或 web#ab12cd34ef |
| `page` | integer | 否 | 第几页（从 1 起）。只对文献有效；不给自己取开头 |
| `max_chars` | integer | 否 | 这次最多返回多少字，默认 3000，最多 8000 |

### 2.12 `websearch` 联网检索（2 个）

#### search_web

网上有没有关于这个问题的资料？

**输入：** query 最长 128 个字符，支持 site:、-site:、精确短语、after:、before:、lang:；count 控制条数。

**输出：** 每条给引用号（web#…）、标题、站点、日期、权威度与 400 字摘要，结果自动存进本地语料。

**边界：** 计费，5 元 / 1000 次成功请求；先查 search_corpus，本地已有同样的 query 再搜不花钱；命中 0 条不是失败，换个说法再试。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 搜索词，1～128 个字符。中英文都可以；英文文献用英文关键词命中率更高 |
| `count` | integer | 否 | 列给模型看的条数，默认 5，最多 10（取更多不额外计费） |
| `refresh` | boolean | 否 | true 时不读本地档案、重新联网（会再付一次费）。默认 false |

#### fetch_webpage

这个网页的正文写了什么？

**输入：** url 必填；max_chars 控制长度；refresh=true 才重新联网。

**输出：** 正文（Markdown）存进本地语料成全文源，引用号不变，之后可以反复读。

**边界：** 计费，5 元 / 1000 次成功请求；本地已有全文时不再联网；抓不到时如实说明，本地那份摘要仍在。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | string | 是 | http / https 网页地址（通常来自 search_web 的返回） |
| `max_chars` | integer | 否 | 这次返回多少字，默认 3000，夹取到 500～8000。本地存的是完整正文 |
| `refresh` | boolean | 否 | true 时即使本地有全文也重新抓（会再付一次费）。默认 false |

### 2.13 `receptor` 准备受体（1 个）

#### prepare_receptor

这套原始结构要怎么准备，才能直接交给对接？

**输入：** structure 填状态 raw 的受体；chain 选链；pocket 给口袋；altloc 必须显式选；confirm=true 才写盘。

**输出：** 一个受体目录（结构、参数、盒子、体检报告），登记成状态 prepared 的受体。

**边界：** 默认只预演，列出会删掉的水与杂原子、盒子位置与匹配不上的残基；只出刚性受体，不生成 3D 坐标、不指派质子化态。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `structure` | string | 是 | raw 状态的受体 handle，例如 receptor#1 |
| `chain` | string | 否 | 留哪条链：单字母 / 逗号分隔 / all；单链结构可留空 |
| `ligand` | string | 否 | 共晶配体：残基名或 残基名:链；whole 盲对接；none 不取 |
| `pocket` | string | 否 | 口袋 handle（define_pocket 的产物）；给了就用它的盒子，与 box_center / box_size 互斥。ligand 仍可用来抽走共晶配体 |
| `box_center` | string | 否 | 盒子中心，例如 "21.7, 0.3, 52.1"，与 box_size 成对给 |
| `box_size` | string | 否 | 盒子边长（埃），例如 "24, 13, 15" |
| `padding` | number | 否 | 由配体算盒子时四周留多少埃，默认 4，上限 20 |
| `altloc` | string | 否 | 交替构象取哪个（通常是 A）；留空时遇到交替构象会拒绝 |
| `keep_hetero` | string | 否 | 要保留的杂原子残基名，逗号分隔（金属离子、糖基化位点） |
| `relax` | boolean | 否 | true 时允许丢掉模板匹配不上的残基，默认 false（拒绝并报告） |
| `confirm` | boolean | 否 | true 才真正写盘与登记，默认 false 只做预演 |

### 2.14 `ligand` 配体库（5 个）

#### import_ligandset

本机这份配体文件里有多少个可用分子？

**输入：** path 填 .sdf、.sd、.smi 或 .csv；CSV 至少要有 smiles 列，也认 canonical_smiles。

**输出：** 原始配体集；每条去盐、按规范 SMILES 去重，无效的跳过并逐条报原因。

**边界：** 同一份内容与同参数重复导入不会重复登记；单条 SMILES 写成 .smi 的一行，不收 SMILES 字符串本身。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | string | 是 | 本机配体文件的路径，建议给绝对路径；支持 .sdf / .sd / .smi / .csv |
| `name` | string | 否 | 配体集显示名；留空用文件名 |
| `smiles_column` | string | 否 | CSV 里 SMILES 的列名；留空依次尝试 smiles / canonical_smiles |
| `limit` | integer | 否 | 最多保留多少个有效分子，默认 20000，上限 50000 |

#### prepare_ligands

这批原始配体能不能生成可对接的 3D 构象？

**输入：** ligands 填原始配体集；limit 与 offset 分批；confirm=true 才生成坐标。

**输出：** 状态 prepared 的配体集（PDBQT），同一组参数按内容指纹复用，不覆盖旧产物。

**边界：** 默认只预演，不写盘；单条失败不中断整批，整批一个都没成功时不登记；大库按 offset 分批，两批不重叠。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ligands` | string | 是 | 原始配体集 handle，例如 ligandset#2。**不要传文件路径** |
| `limit` | integer | 否 | 这一批最多准备多少个（去重后按源顺序取），默认 200，上限 2000 |
| `offset` | integer | 否 | 从第几条开始取（去重后的序号），默认 0；大库分批用 |
| `seed` | integer | 否 | ETKDGv3 的随机种子，默认 42；0 表示随机。换 seed 会生成另一组构象 |
| `confirm` | boolean | 否 | true 才真正写盘与登记；默认 false 只预演 |

#### curate_ligands

待筛库该先去掉哪些分子？

**输入：** ligands 填配体集；理化窗口都是闭区间，不给就不设限；mode 选 drop（默认，真删）或 flag（只标注）。

**输出：** 筛过的配体集，命中的规则写进 flags 列，可直接交给 prepare_ligands。

**边界：** 判据由代码算（PAINS、Brenk、描述符），没有让模型自己判的开关；mode=flag 时一行都不删。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ligands` | string | 是 | 原始配体集的 handle（已经 prepare 过的不能筛） |
| `min_mw` | number | 否 | 分子量下限，0–2000；不给则不设下限 |
| `max_mw` | number | 否 | 分子量上限，0–2000，常用 500；不给则不设上限 |
| `max_logp` | number | 否 | logP 上限，−20–40；不给则不设上限 |
| `max_tpsa` | number | 否 | TPSA 上限（Å²），0–1000；不给则不设上限 |
| `max_hbd` | integer | 否 | 氢键供体数上限，0–50；不给则不设上限 |
| `max_hba` | integer | 否 | 氢键受体数上限，0–50；不给则不设上限 |
| `max_rotb` | integer | 否 | 可旋转键数上限，0–100；不给则不设上限 |
| `pains` | boolean | 否 | 是否查 PAINS_A/B/C，默认 true |
| `brenk` | boolean | 否 | 是否查 Brenk 不稳定/反应性片段，默认 true |
| `dedupe` | string | 否 | 去重键：none / canonical / inchikey（默认）/ scaffold（InChIKey 前 14 位） |
| `mode` | string | 否 | drop（默认，真删）/ flag（只标注，保留全集） |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### build_reference_set

哪些已知活性分子能当筛选的参考集？

**输入：** actives 填 search_actives 的产物；min_pchembl 是下限；max 控制条数。

**输出：** 状态 reference、label=active 的配体集，按活性排序，可直接当 rank_library 的查询集。

**边界：** 同一分子只取最强的那条；PAINS 命中只标不删；少于 10 条会标 too_small，分档之前先看一眼。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `actives` | string | 是 | search_actives 的产物 handle（也接受 import_ligandset 的目录） |
| `min_pchembl` | number | 否 | 活性下限，默认 6（≈1 μM）；越大越严 |
| `max` | integer | 否 | 最多保留多少条，默认 500，上限 5000 |
| `pains` | boolean | 否 | 是否标注 PAINS，默认 true（只标不删） |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### align_ligands

这组配体能不能叠到同一个参考分子上？

**输入：** ligands 填配体集；reference 指定参考分子；rmsd_cutoff 与 min_atoms 控制接受条件。

**输出：** 状态 aligned 的配体集（aligned.sdf 与 ligands.csv），可直接交给 build_pharmacophore。

**边界：** 叠不上的逐条列出原因，不静默丢掉；它们本来就不是同一个系列，叠出来的药效团没有意义。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ligands` | string | 是 | 待叠合的配体集 handle |
| `reference` | string | 否 | 可选：用哪个分子当参考（另一个配体集 handle）；默认用第一个能叠的分子 |
| `rmsd_cutoff` | number | 否 | 叠合后 RMSD 上限（埃），默认 2.5 |
| `min_atoms` | integer | 否 | 公共骨架至少多少个原子，默认 6 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

### 2.15 `screen` 不看靶点的粗筛（2 个）

#### rank_library

这个库按哪种相似性排序，最像参考的前几条是哪几个？

**输入：** library 必填；method 选 similarity（默认，2D 指纹）、shape（3D 形状）或 pharmacophore（药效团），再按方法给 query、smiles 或 pharmacophore。

**输出：** 一张候选表（分数列随方法变化），登记成配体集，可直接交给 rank_candidates。

**边界：** 三种分数不可比，不要混着排序；shape 与 pharmacophore 是粗筛，精细排序靠 dock。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `method` | string | 否 | 可选值：`similarity` / `shape` / `pharmacophore`。similarity（默认）/ shape / pharmacophore |
| `library` | string | 是 | 待筛配体集 handle |
| `query` | string | 否 | similarity：参考集 handle（与 smiles 二选一）；shape：已备 3D 的配体集或准备过的受体 |
| `smiles` | string | 否 | similarity 时的单个查询分子 |
| `pharmacophore` | string | 否 | pharmacophore 时用：build_pharmacophore 的产物 |
| `metric` | string | 否 | similarity：ecfp4（默认）/ maccs / atompair；shape：shape_tanimoto / overlap |
| `smarts` | string | 否 | similarity 时的子结构查询；给了它就不再算相似度 |
| `cutoff` | number | 否 | similarity 与 shape 的分数下限（0–1），默认 0 |
| `tolerance` | number | 否 | pharmacophore 时距离直方图的档宽（埃），默认 1.5 |
| `top` | integer | 否 | 最多保留多少条，默认 100，上限 2000 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### pick_diverse

这批候选里，哪几个彼此不像、值得一起送去做实验？

**输入：** source 填候选表或配体集；poses 填 dock 产物时才做位姿去重；limit 控制个数。

**输出：** 状态 selected 的配体集，列里有骨架、簇号、与已选集合的最大相似度、名次。

**边界：** 四步都不看分数——位姿去重、MaxMin、骨架配额、与上一轮的重合；选不满时如实报数，不降低门槛凑数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source` | string | 是 | 候选表或配体集的 handle（名次取表里的行序） |
| `poses` | string | 否 | 对接产物 handle；给了才按位姿体积去重 |
| `limit` | integer | 否 | 挑几个，默认 20，上限 200 |
| `max_similarity` | number | 否 | 两两 ECFP4 Tanimoto 的上限，默认 0.6；越小越严 |
| `per_scaffold` | integer | 否 | 同一骨架最多几个，默认 3 |
| `previous` | string | 否 | 上一轮的候选表或挑选结果；给了才算重合与新增骨架 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

### 2.16 `docking` 对接（1 个）

#### dock

已准备的受体和一组配体，对出来哪个位姿最可信？

**输入：** receptor 与 ligands 都必须来自确认过的准备产物；scoring 选 vina（默认）、vinardo 或 ad4；limit 与 offset 分批。

**输出：** 对接产物（每个分子的位姿、分数与结果表），分数越低越好，登记成 artifact。

**边界：** 三种力场的分数互不可比，也不能跨受体、跨盒子、跨靶点比较；vina 与 vinardo 没有显式静电，疏水而体积大的分子容易拿到更好的分数；ad4 需要 autogrid4 且盒子边长不超过 40 埃；参数由用户定，不替用户改。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 已准备的受体 handle，例如 receptor#2；不要传原始结构 |
| `ligands` | string | 是 | 已准备的配体集 handle，例如 ligandset#3；不要传原始配体集 |
| `scoring` | string | 否 | 可选值：`vina` / `vinardo` / `ad4`。力场，默认 vina。vinardo 是 vina 的改良版；ad4 是 AutoDock4 力场（要 autogrid4 先算图）。三者分数不能互相比较 |
| `exhaustiveness` | integer | 否 | Vina 搜索强度，默认 8，范围 1–64 |
| `num_modes` | integer | 否 | 每个配体最多保留多少个位姿，默认 9，范围 1–20 |
| `seed` | integer | 否 | Vina 随机种子，默认 42；0 表示随机 |
| `cpu` | integer | 否 | CPU 线程数，默认 0（Vina 用全部），范围 0–256 |
| `energy_range` | number | 否 | 保留位姿的能量窗口（kcal/mol），默认 3，范围 0.1–100 |
| `limit` | integer | 否 | 本次最多对接多少个配体，默认 50，上限 500 |
| `offset` | integer | 否 | 从第几个配体开始对接（按配体集顺序），默认 0；大库分批用 |
| `timeout` | number | 否 | 单个配体的超时秒数，默认 300，范围 10–3600；超时只跳过该配体 |

### 2.17 `finetune` 微调亲和力模型（1 个）

#### fit_affinity

这个靶点的实测活性够不够微调一个亲和力模型？

**输入：** receptor 填已准备的受体，ligands 填带 pchembl 的活性配体集；confirm=true 才真训。

**输出：** kind=model 的 artifact，之后交给 score_affinity 推理。

**边界：** 至少要 40 个带实测活性的化合物；只对训练时的口袋有效，换口袋要重训；40–100 个活性时排序力有限，结论要报样本量。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 已准备的受体 handle（receptor#2），必须留了共晶配体 |
| `ligands` | string | 是 | 带实测活性的配体集 handle（ligandset#3），来自 search_actives；必须有 pchembl 列 |
| `mode` | string | 否 | 可选值：`full` / `head`。full=训全部参数（默认，能恢复排序）；head=只训最后一层（只修绝对尺度，排序不变好） |
| `epochs` | integer | 否 | 训练轮数，默认 20；600 个化合物约 20 轮收敛 |
| `lr` | number | 否 | 学习率，默认 1e-05；再大一点就会把预训练权重冲掉 |
| `seed` | integer | 否 | 训练种子，默认 444 |
| `confirm` | boolean | 否 | false（默认）只预演并报告将训练多少条；true 才真跑 |

### 2.18 `affinity` 亲和力预测（1 个）

#### score_affinity

这批配体的结合亲和力大概是多少？

**输入：** receptor 与 ligands 填准备产物；model 填 fit_affinity 的产物；aff_unit 默认 Ki。

**输出：** 预测表（pK），分数越高越好（pK 越大结合越强），登记成 artifact。

**边界：** 与 dock 的 kcal/mol 方向相反，不能跨受体、跨口袋、跨模型比较；pK 是预测值，不是实验值；外推弱，结论要报样本量；需要 DTA 的独立 Python 环境（torch 与 dgl）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 已准备的受体 handle，例如 receptor#2；不要传原始结构 |
| `ligands` | string | 是 | 已准备的配体集 handle（ligandset#3），或 dock 的产物 handle（artifact#4，此时按对接姿势打分） |
| `aff_unit` | string | 否 | 可选值：`Kd` / `Ki` / `IC50`。条件单位，默认 Ki；它会影响预测值 |
| `pocket_radius` | number | 否 | 口袋半径（埃），默认 10，范围 3–20；以受体盒子的中心为球心截残基 |
| `limit` | integer | 否 | 本次最多打多少个配体，默认 50，上限 500 |
| `timeout` | number | 否 | 整批超时秒数，默认 900，范围 60–7200 |
| `device` | string | 否 | 可选值：`auto` / `cpu` / `cuda`。推理设备，默认 auto（有 GPU 用 GPU，否则 CPU） |
| `model` | string | 是 | 必填：fit_affinity 产出的模型 artifact（kind=model）。用它自己的权重与口袋打分。先微调再推理，零样本不进入流程 |

### 2.19 `analysis` 结果分析（8 个）

#### rank_candidates

这批分子按多路分数合起来该怎么排，前几名里哪几个是对照？

**输入：** sources 必填，填产物 handle，逗号分隔；with_controls=true 时再从生成的表里挑对照，可带 pocket 与 hypothesis。

**输出：** 一张候选表（平均秩、分档、并列组），登记成 artifact；with_controls=true 时后面附三类对照与各自的理由。

**边界：** 只合并、不重算，某一路缺这个分子时该列留空；平均秩没有物理单位，1 kcal/mol 以内分不出高低，结论里不要写「A 比 B 好」。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sources` | string | 是 | 产物 handle，逗号分隔，例如 artifact#4,artifact#5 |
| `cutoffs` | string | 否 | 两个百分位，默认 5,20；第一个是 high 的上界，第二个是 medium 的上界 |
| `top` | integer | 否 | 返回里列几行，默认 20，上限 200 |
| `with_controls` | boolean | 否 | true 时在生成的候选表里挑正向、阴性、背离三类对照 |
| `pocket` | string | 否 | with_controls 时用：这批对照对应哪处口袋；可选 |
| `hypothesis` | string | 否 | with_controls 时用：这批对照检验哪条假设，如 hyp#1；可选 |
| `limit` | integer | 否 | with_controls 时每类最多挑几个（默认 1，最多 3） |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### analyze_pose

这个分子的位姿靠哪些残基和氢键撑住？

**输入：** docking 填对接产物，molecule 填 id（不是序号）；interactions=true 才算键级指纹。

**输出：** 接触残基（最短距离与接触原子数）、氢键、原子冲突；打开 interactions 时另给盐桥、π 堆积、疏水、卤键、水桥与金属配位。

**边界：** 距离由代码算，口径写在第一段；PLIP 没装时照常给接触与氢键，只在末尾说明指纹没算成。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docking` | string | 是 | 对接产物的 handle，如 artifact#3 |
| `molecule` | string | 是 | 分子 id（dock 产物里的 id 列） |
| `cutoff` | number | 否 | 接触判据（埃），默认 4，范围 2.5–6 |
| `hbond` | boolean | 否 | 是否算氢键，默认 true（关掉只省一点时间，接触与冲突照算） |
| `interactions` | boolean | 否 | 是否加 PLIP 交互指纹（盐桥 / π 堆积 / 疏水 / 卤键 / 水桥 / 金属配位），默认 false。要多花 0.3–1 秒，本机没装 PLIP 时这一段会变成一句说明 |

#### alternative_poses

这次对接给出的第二种摆法，是不是真的不同？

**输入：** docking 与 molecule 必填；max_poses 控制看几个模式；jaccard 是聚簇阈值。

**输出：** 每簇的模式数、分数与接触残基差异。

**边界：** 只读位姿文件里记的全部模式；只有一簇时明说没有第二个可区分的摆法，这是有用的读数，不是空结果。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docking` | string | 是 | dock 产物的 handle |
| `molecule` | string | 是 | 产物里的 id 列（不是序号） |
| `max_poses` | integer | 否 | 最多看几个模式，默认 9 |
| `jaccard` | number | 否 | 判同簇的体积重叠下限，默认 0.5 |
| `cutoff` | number | 否 | 接触判据（埃），默认 4 |
| `top` | integer | 否 | 最多列几簇，默认 5，上限 10 |

#### contact_map

这一批分子是不是都挤在同一个亚口袋里？

**输入：** docking 填对接产物；cutoff 是接触判据；top 控制列几个残基。

**输出：** 每个残基在多少个位姿里接触过、最近一次多远、一共碰到多少个原子。

**边界：** 分母是算得出位姿的分子数，不是产物条数，第一行写明可用几个、跳过几个；单个位姿的氢键与冲突用 analyze_pose。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docking` | string | 是 | 对接产物的 handle，如 artifact#3 |
| `cutoff` | number | 否 | 接触判据（埃），默认 4，范围 2.5–6 |
| `top` | integer | 否 | 最多列几个残基，默认 30，上限 100 |

#### analyze_hydrophobic

这个位姿的高分是真结合，还是又大又油？

**输入：** docking 与 molecule 必填；radius 控制疏水口袋的范围。

**输出：** HP（疏水接触对数）、HM（配体疏水原子里落在疏水口袋的比例），以及脱水账：埋进去却没成氢键的极性原子与最近受体极性原子的距离。

**边界：** 只给结构证据，不给 kcal，不是 HYDE；HM 比 HP 更可靠；只读，不登记。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docking` | string | 是 | 对接产物的 handle，如 artifact#3 |
| `molecule` | string | 是 | 分子 id（dock 产物里的 id 列） |
| `radius` | number | 否 | 接触半径（埃），默认 4，范围 3–6 |

#### efficiency_profile

榜单前面那几个是不是分子肥胖？

**输入：** docking 填对接产物；actives 填带 pchembl_value 的配体集时才算 LLE；sort 与 top 控制排序与条数。

**输出：** 每个分子的 LE、LELP，有活性时另给 LLE，并标出「大而油」（logP 不低于 4 且 LELP 不低于 6）。

**边界：** 从对接分数算的 LE 不等于从 ΔG 算的 LE，只能在同一批、同一引擎、同一力场里比；只读，不登记。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docking` | string | 是 | 对接产物的 handle，如 artifact#3 |
| `actives` | string | 否 | 可选：带 pchembl_value 的配体集 handle（search_actives → build_reference_set，或自己导入的活性表）。给了才算 LLE |
| `sort` | string | 否 | 可选值：`lelp` / `le` / `score`。排序依据，默认 lelp（LELP 升序，越低越好）；le 是降序、score 是升序 |
| `top` | integer | 否 | 显示前多少条，默认 20，上限 200 |

#### validate_docking

这套对接参数把共晶配体放回原位了吗？

**输入：** receptor 填准备后的受体（必须有 ligand.pdb），docking 填对接产物；cutoff 是 RMSD 阈值。

**输出：** 重对接 RMSD，作为 gates.redock_rmsd 进候选表，超过阈值的分子降到 below_cutoff。

**边界：** 这是对协议体检，不是对候选打分；没有参照结构时直接说做不了；原子数或元素对不上时逐条说明，不给一个假的数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `receptor` | string | 是 | 准备后的受体 handle（必须有 ligand.pdb） |
| `docking` | string | 是 | 对接产物 handle |
| `cutoff` | number | 否 | 通过阈值（埃），默认 2，范围 0.5–5 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### compare_poses

同一个受体里，两个位姿差在哪？

**输入：** docking_a / molecule_a 与 docking_b / molecule_b 各一组，两组对接必须用同一份受体。

**输出：** 配体 RMSD（不叠合，直接在同一坐标系里量）、共有与各自独有的接触残基、氢键的共有与差异。

**边界：** RMSD 按原子顺序配对，不做对称等价原子校正；原子数不同时不给整体 RMSD；接触只算重原子，距离判据可调。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docking_a` | string | 是 | 第一份对接产物的 handle |
| `molecule_a` | string | 是 | 第一份里的分子 id |
| `docking_b` | string | 是 | 第二份对接产物的 handle |
| `molecule_b` | string | 是 | 第二份里的分子 id |
| `cutoff` | number | 否 | 接触判据（埃），默认 4，范围 2.5–6 |
| `top` | integer | 否 | 每类差异最多列几条，默认 20 |

### 2.20 `admet` 类药性（1 个）

#### predict_admet

这批分子的类药性指标是多少？

**输入：** ligands 填配体集；endpoints 选要算的项，默认全算。

**输出：** 状态 admet 的配体集，原有 id 与 SMILES 不变，多出几列数值。

**边界：** 只做规则层，每个数都能追到公式；hPPB 与 CYP 抑制明确不支持，宁可说没有也不编一个数；解析失败的分子该行留空。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ligands` | string | 是 | 配体集 handle |
| `endpoints` | string | 否 | 算哪些端点，逗号分隔，默认 solubility,veber,bbb；可选 solubility, veber, bbb, cyp |
| `name` | string | 否 | 显示名，只影响产物目录名 |

### 2.21 `sar` 构效关系（4 个）

#### scaffold_analysis

这批分子按骨架分成哪几组，跨组共有的子结构是什么？

**输入：** source 填候选表或配体集；mode 选 groups（默认，按 Murcko 骨架分组）或 mcs（最大共同子结构，先组内、再跨组）。

**输出：** groups 给每组的分子数与代表分子；mcs 给共同子结构与覆盖不到的分子及原因。

**边界：** 这是二维骨架，三维形状用 analyze_pose；无环分子单独归到 acyclic，不混进别的组。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source` | string | 是 | 候选表或配体集的 handle |
| `mode` | string | 否 | 可选值：`groups` / `mcs`。groups 分组（默认）；mcs 找最大共同子结构 |
| `level` | string | 否 | groups 时用：murcko（默认，保留元素）/ generic（杂原子抹成碳） |
| `min_size` | integer | 否 | groups 时用：小于它的组不单列，默认 2 |
| `min_atoms` | integer | 否 | mcs 时用：共同子结构至少几个原子，默认 8 |
| `timeout` | integer | 否 | mcs 时用：每次最多算几秒，默认 10 |
| `top` | integer | 否 | 最多列几组，groups 默认 20、mcs 默认 10，上限 200 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### matched_pairs

同一骨架上只改一处基团，活性差多少？

**输入：** source 填候选表或配体集；min_delta 过滤噪声级的差别；max_cut 控制切分大小。

**输出：** 匹配分子对，带两侧的分数与差值；分数方向按来源自动取。

**边界：** 只做单侧切分，一次切一刀；双侧切出来的对子证据力弱得多；没有对子时返回空并说明，不勉强凑数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source` | string | 是 | 带分数的候选表 handle |
| `max_cut` | integer | 否 | 单侧最多切几个键，默认 10（切得越多越慢） |
| `min_delta` | number | 否 | 分数变化下限，默认 0.5 |
| `top` | integer | 否 | 最多列几对，默认 50，上限 200 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### enumerate_analogs

这个骨架还能怎么改，能生出哪些分子？

**输入：** scaffold 与 group 填 scaffold_analysis 的分组；max 控制个数；max_mw 与 max_logp 过滤性质。

**输出：** 新配体集，可直接进 prepare_ligands。

**边界：** 生成的分子没有算过活性，只是结构上成立；化合价不合理或重复的会跳过并计数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `scaffold` | string | 是 | scaffold_analysis 的产物 handle |
| `group` | integer | 是 | 用第几组（从 1 开始，对应 scaffold_analysis 返回的序号） |
| `max` | integer | 否 | 最多生成多少，默认 200，上限 2000 |
| `max_mw` | number | 否 | 分子量上限；不给则不筛 |
| `max_logp` | number | 否 | logP 上限；不给则不筛 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

#### record_assay

用户测回来的这批数据怎么记，假设能不能结案？

**输入：** file 填实验数据 CSV（至少要有分子标识与数值两列）；campaign 与 assay 定这张表；batch 填这一批的配体集。

**输出：** 写进 runs/<campaign>/assay.csv；给了 hypothesis 就逐条比预期，返回达标、不达标与判不了各几条，并改假设状态。

**边界：** 同一个分子与测定的重复记录保留最新一条，旧值进 assay-history.csv；写盘是原子的，别的 campaign 一个字节都不动。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | string | 是 | 实验数据 CSV 的路径 |
| `campaign` | string | 是 | 项目名，落 runs/<campaign>/ |
| `assay` | string | 是 | 测定类型，如 IC50 |
| `hypothesis` | string | 否 | 要结案的假设编号（hyp#1）。给了就按它的 expected 与这批实测比，自动改状态；不给只记数据 |
| `batch` | string | 否 | 这一批候选的 handle（pick_diverse 的产物，或候选表）。给了就算本批的账：实测几个、未测哪几个、达标几个、产出率多少 |

### 2.22 `pharmacophore` 药效团（1 个）

#### build_pharmacophore

这组已知活性分子有哪些共同的特征点？

**输入：** actives 填活性配体集；给 docking 就用对接位姿，否则现场生成构象并叠到第一个分子；min_support 是共识阈值。

**输出：** 药效团产物，含疏水、氢键供体、氢键受体、芳香四类特征点。

**边界：** 少于 5 个可用分子直接拒绝；叠不上的分子会列出来，不静默丢掉；筛的时候不叠合，共识只用来定特征与距离。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `actives` | string | 是 | 参考活性集的 handle |
| `docking` | string | 否 | 可选：对接产物 handle，用它里面同名分子的位姿当 3D |
| `min_support` | number | 否 | 一个点至少要在多少比例的分子里出现，默认 0.6 |
| `tolerance` | number | 否 | 同类型特征聚类的距离上限（埃），默认 1.5 |
| `name` | string | 否 | 显示名，只影响产物目录名 |

### 2.23 `research` 研究对象与证据（3 个）

#### workspace

科研工作区里有哪些对象，一个对象怎么登记？

**输入：** action 选 register 或 list。register 要给 kind 与 key，可带 label、source、refs；list 可只列某一类，默认不含已停用的对象。

**输出：** register 回对象 ID 与版本，同一个身份键再登记一次是更新；list 回总数、每类几个与逐条对象，超过 limit 时写明还剩多少。

**边界：** 大文件不要放进来，只记 handle 或路径；证据走 add_evidence，它还要定等级，登记之后不能改。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `action` | string | 是 | 可选值：`register` / `list`。register 登记一个对象；list 看工作区里有哪些 |
| `kind` | string | 否 | 可选值：`target` / `structure` / `pocket` / `ligand` / `pose` / `score` / `evidence` / `hypothesis` / `report`。对象类别 |
| `key` | string | 否 | register 必填：PDB 编号 / UniProt 登录号 / ChEMBL 编号 / 受体 handle 加口袋序号 / 由输入拼出的位姿键 |
| `label` | string | 否 | 一句话名称，例如「EGFR 激酶域」 |
| `source` | string | 否 | 谁产出的：工具名、数据库或文献出处 |
| `refs` | array[string] | 否 | 指向的登记表 handle 或 runs/ 下的路径 |
| `include_retired` | boolean | 否 | list 时连已经停用的对象一起列，默认不列 |
| `limit` | integer | 否 | list 时最多列多少条，默认 40 |

#### add_evidence

这条材料算不算证据，它支持或反驳哪条假设？

**输入：** evidence_class 与 key 必填；supports 与 refutes 填假设编号。

**输出：** 一条证据，等级从强到弱共七种，登记之后不能改。

**边界：** 实测活性、实验结构、用户数据必须带至少一个引用；没有出处的实测结果与模型编的预测在文字上分不出来；假设写 hyp#1 或 hypothesis:<键> 都认。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `evidence_class` | string | 是 | 可选值：`experimental_activity` / `experimental_structure` / `database` / `literature` / `user_data` / `prediction` / `inference`。证据类别，从强到弱：experimental_activity、experimental_structure、database、literature、user_data、prediction、inference。只有前两类能让假设结案 |
| `key` | string | 是 | 身份键：文献用 DOI/PMID，活性用「分子@靶点@测定方式」，预测用产物 handle |
| `source` | string | 是 | 出处：数据库、文献、工具或用户 |
| `label` | string | 否 | 一句话说明这条证据说了什么 |
| `refs` | array[string] | 否 | 编号、handle 或文件路径 |
| `supports` | array[string] | 否 | 支持的假设编号，例如 ["hyp#1"] |
| `refutes` | array[string] | 否 | 反驳的假设编号 |

#### review_hypothesis

这条假设现在站不站得住？

**输入：** hypothesis 填假设编号；apply=true 才写回状态。

**输出：** 支持、削弱、需要修正、暂缓、否定五种意见之一，并摆出支持与反证。

**边界：** 只有实验级证据能改假设状态，预测与推断只记录；找不到编号就不写，也不新建；证据不足时建议暂缓并说明缺什么。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hypothesis` | string | 是 | 假设编号，例如 hyp#1 或 hypothesis:selectivity-hinge |
| `apply` | boolean | 否 | 是否把意见写回任务状态里的假设，默认 false（只看不改） |

---

## 3. 速查

### 3.1 模块与数量

| 模块 | 工具数 | 工具 |
|---|---|---|
| `listing` | 2 | `registry`、`switch_receptor` |
| `uniprot` | 1 | `target_lookup` |
| `rsearch` | 1 | `search_pdb` |
| `structure` | 5 | `fetch_structure`、`fetch_alphafold`、`import_structure`、`assess_structure`、`align_structures` |
| `pockets` | 2 | `detect_pockets`、`define_pocket` |
| `pocketcmp` | 2 | `compare_pockets`、`similar_pockets` |
| `chembl` | 2 | `search_actives`、`search_analogs` |
| `pubchem` | 2 | `fetch_compound`、`search_similar_compounds` |
| `opentargets` | 2 | `check_druggability`、`disease_targets` |
| `literature` | 2 | `search_literature`、`read_fulltext` |
| `corpus` | 3 | `import_documents`、`search_corpus`、`read_source` |
| `websearch` | 2 | `search_web`、`fetch_webpage` |
| `receptor` | 1 | `prepare_receptor` |
| `ligand` | 5 | `import_ligandset`、`prepare_ligands`、`curate_ligands`、`build_reference_set`、`align_ligands` |
| `screen` | 2 | `rank_library`、`pick_diverse` |
| `docking` | 1 | `dock` |
| `finetune` | 1 | `fit_affinity` |
| `affinity` | 1 | `score_affinity` |
| `analysis` | 8 | `rank_candidates`、`analyze_pose`、`alternative_poses`、`contact_map`、`analyze_hydrophobic`、`efficiency_profile`、`validate_docking`、`compare_poses` |
| `admet` | 1 | `predict_admet` |
| `sar` | 4 | `scaffold_analysis`、`matched_pairs`、`enumerate_analogs`、`record_assay` |
| `pharmacophore` | 1 | `build_pharmacophore` |
| `research` | 3 | `workspace`、`add_evidence`、`review_hypothesis` |

常驻 17 个，领域 54 个，合计 71 个。

### 3.2 按任务找工具

| 你要做什么 | 先看这几个 |
|---|---|
| 查靶点是谁、有哪些结构 | `target_lookup`、`check_druggability`、`search_pdb`、`survey_structures`、`search_literature` |
| 找口袋、比两个口袋 | `detect_pockets`、`define_pocket`、`compare_pockets`、`similar_pockets` |
| 准备受体与配体 | `prepare_receptor`、`import_ligandset`、`prepare_ligands`、`curate_ligands` |
| 从库里挑一批候选 | `search_actives`、`search_analogs`、`rank_library`、`pick_diverse`、`build_reference_set` |
| 对接与打分 | `dock`、`validate_docking`、`rank_candidates`、`rank_candidates(with_controls=true)` |
| 看位姿对不对 | `analyze_pose`、`contact_map`、`alternative_poses`、`compare_poses`、`analyze_hydrophobic` |
| 亲和力 | `fit_affinity`（先微调）、`score_affinity`（再打分） |
| 构效关系 | `scaffold_analysis`、`matched_pairs`、`enumerate_analogs`、`record_assay` |
| 假设与证据 | `workspace`、`add_evidence`、`review_hypothesis`、`hypothesis` |
| 画图与交付 | `write_run`、`open_viewer` |

### 3.3 参数里的常见约定

- **handle 的前缀**：`receptor#` 受体、`ligandset#` 配体集、`artifact#` 其余产物（口袋、位姿、分数表、报告）。编号只在本次会话有效，跨会话要重新登记。
- **`confirm`**：写盘动作的预演开关。不带只报影响范围，带 `true` 才执行；`dispatch_campaign` 例外，`confirm` 必填。
- **分数方向**：`dock` 的分数越低越好（负得越多越好）；`score_affinity` 给的是 pK，越高越好。两者不可比较，也不能混在一张表里排序。
- **`top` 与 `limit`**：都是截断条数，默认值与上限写在各自的参数说明里；返回值末尾会写「还有多少条没显示」。
- **`name`**：只影响产物目录名与显示，不参与计算，也不要拿它当编号。
