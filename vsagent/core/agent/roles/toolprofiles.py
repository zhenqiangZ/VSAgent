"""常驻核心工具的两档名单：谁与用户对话，谁只做计算。

分档的根据是实测（2026-11-12，30 个会话、1,874 次工具调用）：常驻工具
合计 8,552 token，而子 agent 的清单与主 agent 只差 `agent_status` 与
`dispatch_campaign` 两个。也就是说，一个只负责取结构的子 agent，每一步仍然带着
`hypothesis`（1,324 token）、`write_run`（931）、`assess_campaign`（531）这些
它用不上的定义。

两档的边界有两条：**这个 agent 会不会直接和用户说话**，以及**它要不要亲自取数**。
前一条划掉交互与编排工具，后一条划掉开工盘点——盘点一次调用就去 UniProt /
ChEMBL / RCSB 取数，属于领域动作，按能力计划派给角色做。

- `orchestrator`：根会话。保留对话、派发与呈现那一套，去掉开工盘点
  （`assess_campaign` / `survey_structures`）——那是一次调用就去 UniProt / ChEMBL /
  RCSB 取数的领域动作，按能力计划派给角色做；
- `worker`：子 agent。去掉与用户交互、与全局编排有关的工具，保留读写、检索、
  工具族装入与呈现通道。`recall` 留着、`remember` 去掉：只读的项目事实谁都
  需要，写入长期记忆只应由主 agent 在用户明确要求时做；
- `full`：不裁，等于分档之前的行为，回滚与工具面板用它。

`load_tools` 与 `bash` 两档都保留：前者是子 agent 遇到计划外工具族时的回退通道，
后者是读文件之外唯一的兜底操作。去掉任何一个，缺工具的子 agent 就只能绕道自己实现
——`docs/review.md` 的 F1 正是这样绕了二十步。主 agent 那一边由
`toolset.hidden_names` 挡住：这一轮有可执行计划时，它的领域族一律不发，`load_tools`
也会退回一句「派给角色」。

名单是数据，不是判断：`keeps()` 只做集合查找，不读会话、不看任务。未知档位
一律按 `full` 处理——宁可多发几个定义，也不要因为一个拼错的档位把工具悄悄裁掉。
"""

from __future__ import annotations

import os

FULL = "full"
ORCHESTRATOR = "orchestrator"
WORKER = "worker"

PROFILES: tuple[str, ...] = (FULL, ORCHESTRATOR, WORKER)

# 环境变量：设成 `full` 时两档都退回分档之前的行为。配置项接入后由配置顶掉。
ENV = "VSA_TOOLSET"

# `worker` 档**明确**要留的常驻工具。
#
# `assess_campaign` 是开工盘点：一次调用同时看受体侧结构数与配体侧活性条数/骨架，
# 两个角色都要用（`ligand` 看活性，`structure_pocket` 看结构）。它不能挂在某一个
# 能力上，所以按档位留——但**写成名单而不是靠「没被排除」默认放行**：
# 「谁拿得到它」必须是一个写下来的决定，否则下一个人改 `WORKER_EXCLUDED` 时
# 会顺手把盘点从所有子 agent 手里拿走（P-83）。
WORKER_CORE: frozenset[str] = frozenset({"assess_campaign"})

# `worker` 档拿不到的核心工具。只列「拿不到」而不是「拿得到」：新加一个常驻
# 工具时，默认所有档都能看见——漏登记的好处是多发几个定义，坏处是白占额度；
# 反过来漏登记会让子 agent 少了工具却查不出来。
WORKER_EXCLUDED: frozenset[str] = frozenset(
    {
        "agent_status",
        "dispatch_campaign",  # 多角色编排是主 agent 的事
        "search_history",  # 跨会话检索是主 agent 的事
        "read_turn",  # 读取被移出的原文，属于会话级操作
        "remember",  # 长期记忆只由主 agent 写
        "hypothesis",  # 假设是交付单位，由主 agent 汇总
    }
)

# `orchestrator` 档拿不到的常驻工具。
#
# 只有两个，都是「开工盘点」：它们一次调用就去 UniProt / ChEMBL / RCSB 取数，
# 属于领域动作。主 agent 的职责是拆解、派发与汇总，盘点交给承担该能力的角色
# （`ligand` / `structure_pocket`），派发时按计划把族给它们。
#
# 为什么放在档位而不是校验器里：模型看不见的工具它不会去调。等到调用再拦，
# 拦下来之后它还要再想一条绕路，白烧一步；从清单里去掉，就没有这一步可走。
ORCHESTRATOR_EXCLUDED: frozenset[str] = frozenset(
    {
        "assess_campaign",  # 两侧盘点：受体侧结构数、配体侧活性条数与骨架
        "survey_structures",  # 一次取回全部实验结构
    }
)


def keeps(profile: str, name: str, role: str = "") -> bool:
    """这个档位要不要这个工具。未知档位一律保留。

    `role` 只用来放行**角色自己声明过**的常驻工具：`hypothesis` 角色要写假设状态，
    而 `hypothesis` 这个常驻工具在全量清单里、平时不给 worker。能力表
    （`capabilities.Capability.standalone_tools`）已经写明谁需要它，这里按那份声明
    放行，不另抄一份名单。

    为什么不是「把 `hypothesis` 从 `WORKER_EXCLUDED` 删掉」：那样每个子 agent 每步
    都多带 1,042 token（实测），而只有假设角色用得上。装配表按 `(档位, 角色)` 缓存，
    也就多一份注册表。
    """
    if profile == WORKER:
        if name in WORKER_CORE:
            return True
        if name not in WORKER_EXCLUDED:
            return True
        if not role:
            return False
        # 延迟导入：`capabilities` 会拉起整个领域包，而 `keeps` 在装配路径上被
        # 反复调用。走到这里的只有「被 worker 排除、但角色声明要」的那一个。
        from vsagent.core.agent.roles.capabilities import standalone_for

        return name in standalone_for(role)
    if profile == ORCHESTRATOR:
        return name not in ORCHESTRATOR_EXCLUDED
    return True


def selected(default: str = FULL) -> str:
    """当前生效的档位：环境变量优先，认不出来就用 `default`。"""
    value = os.environ.get(ENV, "").strip().lower()
    return value if value in PROFILES else default


def profile_for(subagent: bool) -> str:
    """按会话类型取默认档位，再让环境变量顶掉。

    只按「是不是子 agent」分，不按角色分：角色要到 Phase 9 才存在，现在按角色
    分档会把一个还没定义的边界提前固化下来。
    """
    return selected(WORKER if subagent else ORCHESTRATOR)
