"""判据：稳定前缀在预算之内，而且它点名的事模型真的做得到。

前缀是每一步都要发的固定开销，也是唯一一段「模型照着做、错了没人拦」的文字。
它出问题的四种样子，靠人眼读是看不出来的：

1. **太长**：6604 token 那次占掉 18K 窗口的 36.7%，挤压的是最近几轮原文；
2. **点名了看不见的工具**：写着「用 registry 取 handle」，而它不在这个角色的
   名单里——模型撞一次墙才知道这件事该换人，一次撞墙就是一步；
3. **枚举漂移**：写着「全库只有三个动作收路径」，而实际是四个。枚举写进提示的那天
   就开始过期，过期之后模型会照着错的清单推理；
4. **说法与工具对不上**：Plan 身份段说「这些都会被拒绝」，而实际被拒绝的工具里有一类
   没写进去；或者越界判定认的参数名在工具的参数表里已经不存在了。

这四条都能自动判：长度可算、工具名可与注册表对、参数名可与 schema 对、
Plan 的类别可与 `PLAN_DENY` 对。判据只管这几件**能机械判的**事，判断质量本身不在这里。

    python tools/check_prefix.py
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.roles.catalog import ROLES
from vsagent.core.agent.domain.all_tools import all_names, domain_tools  # noqa: E402
from vsagent.core.agent.domain.survey import SurveyStructuresTool  # noqa: E402
from vsagent.core.agent.domain.export import OpenViewerTool, WriteRunTool  # noqa: E402
from vsagent.core.agent.permission import PATH_ARGUMENTS, PLAN_DENY  # noqa: E402
from vsagent.core.agent.roles.prompt import (  # noqa: E402
    REFERENCE_RULE_DIR,
    RULES_DIR_ENV,
    agent_rules,
    estimate_tokens,
    global_rules,
    project_rules,
    read_rule,
    reference_files,
    rule_inventory,
    rules_directory,
    rules_home,
    stable_prefix,
    stage_rules,
)
from vsagent.core.agent.planning.stages import STAGES
from vsagent.core.agent.roles import agents  # noqa: E402
from vsagent.core.agent.tools import (  # noqa: E402
    BashTool,
    GrepTool,
    PatchTool,
    ReadTool,
    ToolRegistry,
    WriteTool,
)
from vsagent.core.agent.research.workspace import Workspace
from vsagent.core.models import Session  # noqa: E402

# 固定那几段的预算：身份、假设契约、路线判断、操作纪律、shell、登记表、记忆、事件。
# 这几段每步都发，涨了会挤压最近几轮的原文，所以照旧卡住。
PREFIX_BUDGET = 1900
# 一份角色规则的预算。角色规则进稳定前缀，每多一份就在每一步重复计费；
# 400 是其余角色的实测上界，留了余量，但不允许它们长成第二本手册。
# 主控承接了原 `AGENTS.md` 的交付、禁止与检查清单，单独给 1,200 的额度；
# 整段前缀仍有 `PREFIX_BUDGET` 兜底，主控不会把窗口吃满。
ROLE_RULE_BUDGET = 400
ROLE_RULE_BUDGETS: dict[str, int] = {"principal": 1_200}
# 规则文件（`AGENTS.md`）不设上限：它是许可与判据的来源，少一条就可能让模型做一个
# 本来被禁止的动作。这里只报它的体量，供改的人心里有数。
# 这些句式一出现就说明有人又在提示里抄了一份会过期的清单。
DRIFT_PATTERNS = ("全库只有", "只有三个动作", "三个动作收路径", "唯一收路径的动作")

# Plan 身份段必须覆盖的类别：每个被拒绝的工具 → 段里对应那一类动作的说法。
#
# `PLAN_DENY` 有二十几个工具，身份段写不下它们的名字，也不该写——名字属于工具清单。
# 但**类别一个都不能漏**：P0 那次段落里只提了「准备受体」，模型以为 dock / 打分 /
# 排序照常可用，试一次被拒一次。新增一个被拒绝的工具时，这里必须补一行；
# 漏补会让判据失败，而不是让模型去撞墙。
PLAN_ACTIONS: dict[str, str] = {
    "write": "写文件",
    "patch": "改文件",
    "bash": "bash",
    "prepare_receptor": "准备受体与配体",
    "prepare_ligands": "准备受体与配体",
    "detect_pockets": "检测与定义口袋",
    "define_pocket": "检测与定义口袋",
    "dock": "对接",
    "validate_docking": "重对接验证与参照集",
    "build_reference_set": "重对接验证与参照集",
    "score_affinity": "打分与微调",
    "fit_affinity": "打分与微调",
    "write_run": "写运行目录",
    "rank_candidates": "批量筛选与排序",
    "rank_library": "批量筛选与排序",
    "curate_ligands": "批量筛选与排序",
    "build_pharmacophore": "药效团构建与筛选",
    "align_ligands": "结构叠合",
    "predict_admet": "性质预测",
    "scaffold_analysis": "骨架与配对分析",
    "matched_pairs": "骨架与配对分析",
    "enumerate_analogs": "枚举类似物",
    "record_assay": "记录实验数据",
}


def build(
    agent_name: str = "Build",
    depth: int = 0,
    rules_dir: Path | None = None,
    role: str = "",
) -> str:
    """按给定身份装一次前缀。工作目录用仓库根，规则也从这里取。

    `rules_dir` 给了就临时顶掉规则目录：量「不含规则文件」的固定部分用它。
    那个目录下不放 `AGENTS.md`，`project_rules` 读出空字符串，规则段整段不出现。
    `role` 用来量某个专业角色拿到的前缀。
    """
    session = Session(id="ses_check_prefix", title="前缀判据", agent_name=agent_name)
    session.depth = depth
    session.agent_role = role
    workspace = Workspace(session.id, root=ROOT)
    if rules_dir is None:
        return stable_prefix(workspace, session)
    os.environ[RULES_DIR_ENV] = str(rules_dir)
    try:
        return stable_prefix(workspace, session)
    finally:
        os.environ.pop(RULES_DIR_ENV, None)


def tool_specs() -> dict[str, dict]:
    """全部工具的名字与参数表，装配顺序与应用里一致。

    核心五个文件工具加全部领域工具就够这两条判据用了；记忆、任务、事件那几个
    不带路径参数，也不进 `PATH_ARGUMENTS`，装不装不影响结论。
    常驻工具要装：前缀里点名的那些，不装的话「点名了看不见的工具」这条判据
    永远查不到它。
    """
    registry = ToolRegistry()
    for tool in (
        BashTool(cwd=ROOT),
        ReadTool(cwd=ROOT),
        WriteTool(cwd=ROOT),
        PatchTool(cwd=ROOT),
        GrepTool(cwd=ROOT),
        SurveyStructuresTool(),
        WriteRunTool(),
        OpenViewerTool(),
    ):
        registry.register(tool)
    for tool in domain_tools():
        registry.register(tool)
    return {name: registry.get(name).spec.parameters for name in registry.names()}


def main() -> int:
    failures: list[str] = []
    prefix = build()
    plan_prefix = build("Plan")
    child_prefix = build(depth=1)
    rules = project_rules(rules_directory(ROOT))
    # 空规则目录：固定段的体量要单独量，否则规则一涨，这一条就跟着假红。
    empty_dir = ROOT / ".probe" / "no-rules"
    empty_dir.mkdir(parents=True, exist_ok=True)
    fixed = build(rules_dir=empty_dir)
    specs = tool_specs()

    total = estimate_tokens(prefix)
    fixed_tokens = estimate_tokens(fixed)
    rules_tokens = estimate_tokens(rules)
    print(f"固定段 {fixed_tokens} token（预算 {PREFIX_BUDGET}）")
    print(
        f"项目规则 {rules_tokens} token / {len(rules.splitlines())} 行"
        "（不再注入，仅 /init 与人工查阅）"
    )
    print(f"整段前缀 {total} token")
    print(f"  Plan 身份 {estimate_tokens(plan_prefix) - total:+d} token"
          f"，子 agent 前缀 {estimate_tokens(child_prefix) - total:+d} token")

    if fixed_tokens > PREFIX_BUDGET:
        failures.append(f"固定段 {fixed_tokens} token，超过预算 {PREFIX_BUDGET}")

    # `AGENTS.md` 不再进任何会话的前缀：主控行为以 `principal.md` 为准。
    # 判据仍然读文件本身，报它的体量；只是不再允许它出现在前缀里。
    title = "# VSAgent — Multi-Agent Operating Specification"
    if rules and title in prefix:
        failures.append("主 agent 的前缀里仍然有 AGENTS.md")
    if rules and title in child_prefix:
        failures.append("子 agent 的前缀里仍然有 AGENTS.md")

    # 前缀同一份输入必须产出同一份字节：差一个字节，整段 prompt cache 就废了。
    if prefix != build():
        failures.append("同样的输入装出来的前缀不一致（前缀必须字节稳定）")

    # 点名的工具：名字出现在前缀里，就得保证模型这一步拿得到，或者知道怎么装。
    #
    # 可见性由角色名单现算，不写死名单：新加一个领域工具、
    # 或者把哪个常驻工具从主 agent 档位里移走时，谁被点名、它首步看不看得见，
    # 都跟着那两处走。常驻工具从主 agent 档位移走时，前缀里那些调用提示也必须
    # 跟着改，否则模型会去调一个看不见的工具。
    named = sorted(
        name for name in specs if re.search(rf"\b{re.escape(name)}\b", prefix)
    )
    visible = set(agents.core_tools("orchestrator", ROOT)) - {"*"}
    off_profile = sorted(name for name in named if name not in visible)
    print(f"  点名的工具 {len(named)} 个：{'、'.join(named) if named else '无'}")
    if off_profile:
        failures.append(
            f"前缀点名了主控清单里没有的工具 {off_profile}："
            "它看不见，只会照着这句话去调一次"
        )

    # 分级规则：全局、角色、阶段三层都会进入模型看到的文字（前两层进稳定前缀，
    # 阶段层进任务块），所以「不点名首步看不见的工具」对它们同样成立。
    # 参考材料不默认注入，不在这一条的范围里——它本来就可以点名具体工具。
    base = rules_directory(ROOT)
    inventory = rule_inventory(base)
    extra_roles = sorted(set(inventory["agents"]) - set(ROLES))
    if extra_roles:
        failures.append(f"rules/agents 里有不属于任何角色的文件：{extra_roles}")
    missing_roles = sorted(set(ROLES) - set(inventory["agents"]))
    if missing_roles:
        failures.append(f"这些角色没有规则文件：{missing_roles}")
    extra_stages = sorted(set(inventory["stages"]) - set(STAGES))
    if extra_stages:
        failures.append(f"rules/stages 里有不属于任何阶段的文件：{extra_stages}")
    missing_stages = sorted(set(STAGES) - set(inventory["stages"]))
    if missing_stages:
        failures.append(f"这些阶段没有规则文件：{missing_stages}")

    tiers = [global_rules(base)]
    tiers += [agent_rules(base, role) for role in ROLES]
    tiers += [stage_rules(stage, ROOT) for stage in STAGES]
    tier_text = "\n".join(tiers)
    # 角色规则里点名的工具，必须至少属于某一个角色的名单或基础档。
    usable: set[str] = set(agents.core_tools("orchestrator", ROOT))
    usable |= set(agents.core_tools("worker", ROOT)) - {"*"}
    for role in ROLES:
        usable.update(agents.role_tools(role, ROOT))
    tier_named = sorted(
        name
        for name in specs
        if name not in usable and re.search(rf"\b{re.escape(name)}\b", tier_text)
    )
    print(f"  分级规则全部 {estimate_tokens(tier_text)} token（不会同时注入）")
    per_step = (
        estimate_tokens(global_rules(base))
        + max(estimate_tokens(agent_rules(base, role)) for role in ROLES)
        + max((estimate_tokens(stage_rules(stage, ROOT)) for stage in STAGES), default=0)
    )
    print(f"  每一步最多 {per_step} token（全局 + 一份角色 + 一份阶段）")
    if tier_named:
        failures.append(
            f"分级规则里点名了领域工具 {tier_named}：它们首步看不见，点名等于教模型撞墙"
        )

    # 一份角色规则太长，等于把手册塞进每一步。逐份卡，不卡总和：
    # 总和会随着角色变多而涨，那时该拆的是内容，不是放宽这一条。
    for role in ROLES:
        tokens = estimate_tokens(agent_rules(base, role))
        limit = ROLE_RULE_BUDGETS.get(role, ROLE_RULE_BUDGET)
        if tokens > limit:
            failures.append(f"{role} 的角色规则 {tokens} token，超过 {limit}")

    # 参考材料只列名字。被注入了就说明装配器读错了目录。
    reference_home = rules_home(base) / REFERENCE_RULE_DIR
    for name in reference_files(base):
        body = read_rule(reference_home / name)
        if body and body in prefix:
            failures.append(f"参考材料 {name} 被默认注入了前缀：它应当按需读取")

    # 呈现纪律只给主控与 visualization；评分纪律只给三个专业角色。
    # 缺了它们，模型不会按角色请求视图，也不会把亲和力模型当成单独的一路。
    if "页面地址必须来自工具返回" not in prefix:
        failures.append("呈现纪律不在前缀里：页面地址会变成编造")
    docking_prefix = build(role="docking")
    if "评分模型" not in docking_prefix or "亲和力模型训练" not in docking_prefix:
        failures.append("打分纪律不在专业角色前缀里：亲和力通道不会被想到")
    core_names = set(agents.core_tools("orchestrator", ROOT)) | set(
        agents.core_tools("worker", ROOT)
    )
    for name in ("write_run", "open_viewer"):
        if name not in core_names:
            failures.append(f"{name} 不在基础档里：主控与子会话都看不见它")
    if set(all_names()) & core_names - {"survey_structures", "hypothesis", "open_viewer", "registry", "add_evidence", "workspace"}:
        overlap = sorted(set(all_names()) & core_names)
        failures.append(f"这些工具既在领域清单又在基础档里：{overlap}")

    # 越界判定认的那些参数名，必须在工具的参数表里真的存在。
    # 参数改名之后，这张表会**静默失效**：路径不再被检查，而一切看起来正常。
    for name, params in sorted(PATH_ARGUMENTS.items()):
        properties = specs.get(name)
        if properties is None:
            failures.append(f"PATH_ARGUMENTS 里的 {name} 不是已注册的工具")
            continue
        available = properties.get("properties") or {}
        missing = [param for param in params if param not in available]
        if missing:
            failures.append(f"{name} 的参数表里没有 {missing}（越界判定会失效）")

    # Plan 的类别覆盖：`PLAN_DENY` 里每个工具都要在身份段里有对应说法。
    unclassified = sorted(set(PLAN_DENY) - set(PLAN_ACTIONS))
    stale = sorted(set(PLAN_ACTIONS) - set(PLAN_DENY))
    if unclassified:
        failures.append(f"PLAN_DENY 里这些工具没有在身份段里归类：{unclassified}")
    if stale:
        failures.append(f"PLAN_ACTIONS 里这些工具已经不在 PLAN_DENY：{stale}")
    for phrase in sorted(set(PLAN_ACTIONS.values())):
        if phrase not in plan_prefix:
            failures.append(f"Plan 身份段没有提到「{phrase}」，模型会以为它照常可用")

    for pattern in DRIFT_PATTERNS:
        if pattern in prefix:
            failures.append(f"前缀里还有枚举式说法「{pattern}」，枚举必然漂移")

    # 子 agent 不能派，也不该看见派发规则；反过来它必须知道自己是子 agent。
    if "该派子 agent" in child_prefix:
        failures.append("子 agent 的前缀里出现了派发规则")
    if "你是由 Principal 派发的专业 Worker Agent" not in child_prefix:
        failures.append("子 agent 的前缀里没有身份段")

    # Plan 只读：它必须说清「哪些会被拒绝」，而不是只提一个例子。
    if "都会被拒绝" not in plan_prefix:
        failures.append("Plan 身份段没有说清「会被拒绝」的范围")

    if failures:
        print("\n未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
