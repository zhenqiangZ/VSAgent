"""角色目录：九个专业 Agent 的工作、权限与操作取值。

这个文件只放常量，不 import 任何模块。协议层、提示词、编译器、路由与判据都从这里
取同一份数据，避免同一条规则写两遍、改一处忘一处。

三组数据：

- `ROLES`：主控加九个专业角色。`AGENTS` 只含九个专业角色，也是 `operations` 的取值。
- `DUTIES`：每个角色能做什么，给意图提示词与角色身份段使用。它只描述工作，不参与路由。
- `PERMISSIONS`：每个角色允许的动作类别，供派发前核对任务授权。

`EXCLUSION_RULES` 是用户说“不要 X”时去掉哪些角色。编译器与路由共用这一份。
"""

from __future__ import annotations

AGENTS: tuple[str, ...] = (
    "research",
    "pocket",
    "ligand",
    "prepare",
    "docking",
    "affinity",
    "analysis",
    "mechanism",
    "visualization",
)
"""九个专业角色。意图里的 `operations` 只能取这些名字。"""

ROLES: tuple[str, ...] = ("principal",) + AGENTS
"""主控加九个专业角色。"""

OPERATIONS: tuple[str, ...] = AGENTS
"""意图操作的取值。操作就是子 Agent，不再经过能力名。"""

STAGES: tuple[str, ...] = (
    "structure_discovery",
    "pocket_analysis",
    "ligand_analysis",
    "screening",
    "evaluation",
)
"""五个科研阶段。子目标可以带一个阶段名，供阶段规则使用。"""

TITLES: dict[str, str] = {
    "principal": "主控：拆解目标、派发与汇总",
    "research": "调研：靶点、受体与配体情报",
    "pocket": "口袋：识别与画像",
    "ligand": "配体：发现与化学空间",
    "prepare": "准备：受体与配体",
    "docking": "对接：位姿与构象搜索",
    "affinity": "亲和力：预测与模型微调",
    "analysis": "分析：结果整合与候选评价",
    "mechanism": "机制：构象关系与假设",
    "visualization": "展示：交互与视图",
}

NOTES: dict[str, str] = {
    "principal": "只做拆解、派发与汇总，不亲自执行领域动作。",
    "research": "只读检索与入库；不替用户做决定。",
    "pocket": "结构、口袋与画像；改共享状态的动作由父会话确认后执行。",
    "ligand": "找配体、排化学空间；不生成新分子。",
    "prepare": "把原始结构准备成可对接的产物；每个动作先预演再确认。",
    "docking": "执行对接并检查位姿质量；不替用户改参数。",
    "affinity": "微调模型并预测亲和力；样本不足时直说做不了。",
    "analysis": "把多路结果合成候选表并做效率体检；不重新对接。",
    "mechanism": "解释结合模式与关键残基，写假设并评审；区分证据与推断。",
    "visualization": "只出视图页；取数与计算交给前面的角色。",
}

DUTIES: dict[str, tuple[str, ...]] = {
    "principal": (),
    "research": (
        "靶点身份与可成药性",
        "疾病与临床信息",
        "文献、本机语料与网页证据",
    ),
    "pocket": (
        "实验结构检索与结构目录",
        "候选结合口袋识别与口袋目录",
        "口袋几何与理化画像",
    ),
    "ligand": (
        "已知配体与活性数据",
        "骨架分布与化学空间",
        "可筛选的配体集",
    ),
    "prepare": (
        "受体准备",
        "配体清理与准备",
    ),
    "docking": (
        "分子对接与构象搜索",
        "位姿质量检查",
    ),
    "affinity": (
        "亲和力模型微调",
        "亲和力预测",
    ),
    "analysis": (
        "多路结果整合",
        "候选评价与类药性",
        "评分一致性检查",
    ),
    "mechanism": (
        "结合模式与关键残基",
        "构效关系",
        "支持与反证整理",
        "假设状态审查",
    ),
    "visualization": (
        "结构、口袋、位姿与候选表的视图页",
    ),
}
"""每个角色能做什么。只用于说明与提示词，不参与路由。"""

WHEN: dict[str, str] = {
    "principal": "",
    "research": "用户问靶点本身、问临床或文献证据，或者筛选前需要先查清靶点。没有给出 uniprot、pdb id 或者具体的结构、任务时，只能调用这个角色。",
    "pocket": "用户要结构、要口袋、要口袋特征，或者筛选前结构和口袋还没定。没有给出 uniprot、pdb id或者具体的结构、口袋时，不能调用这个角色。",
    "ligand": "用户要找已知抑制剂、活性数据、骨架分布或可筛选的配体集。没有给出 uniprot id 时，不能调用这个角色。",
    "prepare": "已有结构和配体，要整理成可以直接对接的输入。",
    "docking": "用户要对接、要位姿、要对接打分。",
    "affinity": "用户要预测亲和力或训练该靶点的模型。",
    "analysis": "用户要比较结果、整合候选表或评价可靠性。",
    "mechanism": "用户要解释机制、找关键残基或验证假设。",
    "visualization": "用户要看图、要页面或要可视化。",
}
"""每个角色的典型选择时机，给意图提示词使用。"""

PERMISSIONS: dict[str, tuple[str, ...]] = {
    "principal": (),
    "research": ("read",),
    "pocket": ("read", "analyze", "execute", "write"),
    "ligand": ("read", "analyze", "write"),
    "prepare": ("execute", "write"),
    "docking": ("execute", "write"),
    "affinity": ("read", "analyze", "write"),
    "analysis": ("read", "analyze", "write"),
    "mechanism": ("read", "analyze", "write"),
    "visualization": ("read",),
}
"""角色允许的动作类别。读与写分开，便于派发前核对授权。"""

REPORT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "principal": (),
    "research": ("evidence_refs",),
    "pocket": ("evidence_refs", "artifact_refs"),
    "ligand": ("evidence_refs", "artifact_refs"),
    "prepare": ("artifact_refs", "limitations"),
    "docking": ("artifact_refs", "limitations"),
    "affinity": ("artifact_refs", "limitations"),
    "analysis": ("evidence_refs", "artifact_refs"),
    "mechanism": ("evidence_refs", "unresolved_questions"),
    "visualization": ("artifact_refs",),
}
"""每个角色交报告时不能空的字段。"""

TIMEOUT_MINUTES: dict[str, float | None] = {
    "principal": None,
    "research": 30,
    "pocket": 45,
    "ligand": 40,
    "prepare": 60,
    "docking": 120,
    "affinity": 90,
    "analysis": 40,
    "mechanism": 30,
    "visualization": 20,
}
"""单个角色的安全网（分钟）。"""

EXCLUSION_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("对接", "分子对接"), ("docking",)),
    (("筛选", "虚拟筛选"), ("prepare", "docking")),
    (("亲和力", "亲和力预测"), ("affinity",)),
    (("机制", "机制分析"), ("mechanism",)),
    (("可视化", "出图", "画图"), ("visualization",)),
)
"""用户说“不要 X”时去掉哪些角色。

编译器与路由共用这一份，避免一边删了模板、另一边仍把角色补进计划。
"""

ROLE_PROVIDED_INPUTS: dict[str, tuple[str, ...]] = {
    "pocket": ("pocket",),
    "ligand": ("ligand_set",),
}
"""用户自带哪些输入时，对应角色整段不必再跑。

键名与 `intent_parser.PROMPT` 里的 `inputs` 说明一致。
`prepare` 不设自带输入：原始配体集不等于已准备产物。
"""


def validate() -> list[str]:
    """核对目录自身。返回问题清单，空列表表示通过。"""
    problems: list[str] = []
    if set(AGENTS) - set(ROLES):
        problems.append(f"AGENTS 里有不在 ROLES 的名字：{'、'.join(sorted(set(AGENTS) - set(ROLES)))}")
    for role in ROLES:
        if role not in TITLES:
            problems.append(f"{role} 没有标题")
        if role not in NOTES:
            problems.append(f"{role} 没有一句话职责")
        if role != "principal" and not DUTIES.get(role):
            problems.append(f"{role} 没有工作说明")
        if role not in PERMISSIONS:
            problems.append(f"{role} 没有权限声明")
        for permission in PERMISSIONS.get(role, ()):
            if permission not in ("read", "analyze", "execute", "write"):
                problems.append(f"{role} 的权限取值不认识：{permission}")
        for field in REPORT_REQUIREMENTS.get(role, ()):
            if field not in ("evidence_refs", "artifact_refs", "limitations", "unresolved_questions"):
                problems.append(f"{role} 要求了不认识的报告字段：{field}")
        if role != "principal" and not TIMEOUT_MINUTES.get(role):
            problems.append(f"{role} 没有执行时限")
    for words, roles in EXCLUSION_RULES:
        for role in roles:
            if role not in AGENTS:
                problems.append(f"排除规则里有不存在的角色：{role}")
        if not words:
            problems.append("排除规则有一项没有关键词")
    for role, keys in ROLE_PROVIDED_INPUTS.items():
        if role not in AGENTS:
            problems.append(f"自带输入表里有不存在的角色：{role}")
        if not keys:
            problems.append(f"{role} 的自带输入是空的")
    return problems
