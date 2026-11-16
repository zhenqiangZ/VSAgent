"""能力注册表：科研能力 → 专业角色 → 权限。

这一层回答的问题是"要做口袋发现，该给哪个角色、允许哪几类动作"，而不是"有哪些
工具"。每个角色装哪些工具由 `roles/agents.toml` 决定，这里不列工具名。

三条刻意的设计：

- **表写死在代码里。** 角色怎么分、能力归谁，是领域判断，需要被评审、被 diff，
  不从工具清单推导。
- **权限按动作分四类。** 同一个能力里的工具风险不同：看口袋与算口袋不是一回事。
  能力守卫按这里的声明判，不按工具名判。
- **报告要求 `evidence_refs` 的角色都配科研对象那一组工具。** 这条是机械的：要求
  它引用证据，就得给它把证据登记成对象的工具；否则「结论要有出处」只能指向聊天
  记录，而那正是 Phase 11 要消掉的东西。

表是数据，判据在 `tools/check_capabilities.py` 与 `--only capabilities` 组：
每个角色至少一件工具、名单里的名字真的存在、领域工具没有一件被漏掉。
"""

from __future__ import annotations

from dataclasses import dataclass


PERMISSIONS: tuple[str, ...] = ("read", "analyze", "execute", "write")
"""一个能力能做哪几类动作。读与写分开，是为了 Phase 8 拦得住"只让看不让改"。"""

ROLES: tuple[str, ...] = (
    "principal",
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
"""十个角色：主控加九个专业角色。一个职责单元对应一个角色。"""

LEGACY_ROLES: dict[str, str] = {
    "target": "research",
    "literature": "research",
    "structure_pocket": "pocket",
    "conformation": "pocket",
    "screening": "prepare",
    "scoring": "analysis",
    "hypothesis": "mechanism",
}
"""旧会话里存着的角色名 → 现在承担同一职责的角色。

会话状态文件里记着 `agent_role`，恢复旧会话时 `profile()` 会按旧名查。没有这张表，
读一个旧会话会当场 KeyError。只用于查表与恢复：派发侧只认新名字。
"""


def canonical(role: str) -> str:
    """把旧角色名换成现在的名字。认不出来的原样返回，交给调用方报错。"""
    return LEGACY_ROLES.get(role, role)


@dataclass(frozen=True)
class Capability:
    """一项科研能力。"""

    name: str
    role: str
    permissions: tuple[str, ...] = ()
    note: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="target_intelligence",
        role="research",
        permissions=("read",),
        note="靶点身份、可成药性与临床证据；不产生产物",
    ),
    Capability(
        name="structure_discovery",
        role="pocket",
        permissions=("read", "execute", "write"),
        note="结构全景与条目检索；survey_structures 常驻，不登记族",
    ),
    Capability(
        name="pocket_discovery",
        role="pocket",
        permissions=("read", "execute", "write"),
        note="口袋检索与预测；产物是口袋目录",
    ),
    Capability(
        name="pocket_profiling",
        role="pocket",
        permissions=("read", "analyze"),
        note="口袋几何与理化画像；只读分析优先，不改受体",
    ),
    Capability(
        name="ligand_intelligence",
        role="ligand",
        permissions=("read", "analyze", "write"),
        note="已知配体、活性、骨架与化学空间；可产出配体集",
    ),
    Capability(
        name="literature_evidence",
        role="research",
        permissions=("read",),
        note="文献、本地语料与网页证据",
    ),
    Capability(
        name="receptor_preparation",
        role="prepare",
        permissions=("execute", "write"),
        note="受体与配体的准备；两段都写新产物",
    ),
    Capability(
        name="docking_run",
        role="docking",
        permissions=("execute", "write"),
        note="对接与重对接验证；analysis 族里只有 validate_docking 用得上",
    ),
    Capability(
        name="affinity_prediction",
        role="affinity",
        permissions=("read", "analyze", "write"),
        note="微调模型与亲和力预测；单独一步，不并进评分",
    ),
    Capability(
        name="scoring_evaluation",
        role="analysis",
        permissions=("read", "analyze", "write"),
        note="多路合成候选表与类药性；只读已有产物，不重新对接",
    ),
    Capability(
        name="mechanism_analysis",
        role="mechanism",
        permissions=("read", "analyze"),
        note="结合模式、关键残基与构效关系",
    ),
    Capability(
        name="hypothesis_validation",
        role="mechanism",
        permissions=("read", "analyze", "write"),
        note="支持与反证整理；写的是假设状态，不是产物",
    ),
    Capability(
        name="visualization",
        role="visualization",
        permissions=("read",),
        note="出视图页；不取数、不算",
    ),
)

_BY_NAME: dict[str, Capability] = {item.name: item for item in CAPABILITIES}


def capability(name: str) -> Capability:
    """按名字取一项。取不到时给出可用名字，便于调用方直接展示。"""
    try:
        return _BY_NAME[name]
    except KeyError as error:
        raise KeyError(f"没有这个能力：{name}。可用：{'、'.join(_BY_NAME)}") from error


def capabilities_for(role: str) -> tuple[Capability, ...]:
    """一个角色承担的全部能力，按表里的顺序。旧角色名按 `LEGACY_ROLES` 查。"""
    name = canonical(role)
    return tuple(item for item in CAPABILITIES if item.role == name)


def roles_with_capabilities() -> tuple[str, ...]:
    """真正承担能力的角色，按 ROLES 的顺序。`principal` 不在里面。"""
    used = {item.role for item in CAPABILITIES}
    return tuple(role for role in ROLES if role in used)


def validate(
    capabilities: tuple[Capability, ...] = CAPABILITIES,
    *,
    catalogue: dict[str, tuple[str, ...]] | None = None,
    core_tools: frozenset[str] | None = None,
) -> list[str]:
    """核对这张表。返回问题清单，空列表表示通过。

    可注入目录与常驻工具名，测试用它验"坏表会被拦住"，不必改全局常量。
    """
    problems: list[str] = []
    seen: set[str] = set()
    for item in capabilities:
        if item.name in seen:
            problems.append(f"能力重名：{item.name}")
        seen.add(item.name)
        if item.role not in ROLES:
            problems.append(f"{item.name} 的角色不存在：{item.role}")
        for permission in item.permissions:
            if permission not in PERMISSIONS:
                problems.append(f"{item.name} 的权限取值不认识：{permission}")
        if not item.permissions:
            problems.append(f"{item.name} 没有声明权限：读还是写说不清")
    return problems
