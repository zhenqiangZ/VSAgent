"""能力注册表：科研能力 → 专业角色 → 工具族与常驻工具。

这一层回答的问题是"要做口袋发现，该给哪个角色、开哪几族工具"，而不是"有哪些
工具"。工具族的唯一事实来源仍然是 `domain/all_tools.py` 的 `FAMILIES`；这里只
**引用**族名，并在 `validate()` 里核对它们真的存在。

三条刻意的设计：

- **表写死在代码里，不从 `FAMILIES` 推导。** 族名能推导，"口袋发现该用哪几族"
  推导不出来——那是领域判断，需要被评审、被 diff。
- **常驻工具单独声明。** `survey_structures` 与 `assess_campaign` 不属于任何族，
  把它们硬塞进一个族会让 `load_tools` 的行为变味（它们必须首步可见）。
- **权限按动作分四类。** 同一族里的工具风险不同：看口袋与算口袋不是一回事。
  Phase 8 的能力守卫按这里的声明判，不按族判。
- **报告要求 `evidence_refs` 的角色都配 `research` 族。** 这条是机械的：要求它
  引用证据，就得给它把证据登记成对象的工具；否则「结论要有出处」只能指向聊天
  记录，而那正是 Phase 11 要消掉的东西。`conformation` / `screening` 只要求
  产物路径，因此不配。

表是数据，判据在 `tools/check_capabilities.py` 与 `--only capabilities` 组：
族名必须存在于目录、常驻工具必须真的装配、一个能力不能既没有族也没有常驻工具。
"""

from __future__ import annotations

from dataclasses import dataclass

from vsagent.core.agent.domain.all_tools import catalog

PERMISSIONS: tuple[str, ...] = ("read", "analyze", "execute", "write")
"""一个能力能做哪几类动作。读与写分开，是为了 Phase 8 拦得住"只让看不让改"。"""

ROLES: tuple[str, ...] = (
    "principal",
    "target",
    "structure_pocket",
    "conformation",
    "ligand",
    "literature",
    "screening",
    "scoring",
    "mechanism",
    "hypothesis",
)
"""专业角色。`principal` 不承担具体能力，它负责调度与汇总。"""


@dataclass(frozen=True)
class Capability:
    """一项科研能力。"""

    name: str
    role: str
    families: tuple[str, ...] = ()
    standalone_tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    note: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="target_intelligence",
        role="target",
        families=("uniprot", "opentargets", "research"),
        permissions=("read",),
        note="靶点身份、可成药性与临床证据；不产生产物",
    ),
    Capability(
        name="structure_discovery",
        role="structure_pocket",
        # `listing` 是选结构要用的：`switch_receptor` 在这里，换当前受体就是它的职责。
        # 2026-11-13 之前这一族不归任何角色，那三个准备/切换动作对 worker 一律 DENY，
        # 结果主 agent 纯编排之后没人能做；改成按角色开放，族随能力一起给。
        families=("structure", "research", "listing"),
        standalone_tools=("survey_structures",),
        permissions=("read", "execute", "write"),
        note="结构全景与条目检索；survey_structures 常驻，不登记族",
    ),
    Capability(
        name="pocket_discovery",
        role="structure_pocket",
        families=("pocket", "structure", "research"),
        permissions=("read", "execute", "write"),
        note="口袋检索与预测；产物是口袋目录",
    ),
    Capability(
        name="pocket_profiling",
        role="conformation",
        families=("pocket", "structure", "analysis"),
        permissions=("read", "analyze"),
        note="口袋几何与理化画像；只读分析优先，不改受体",
    ),
    Capability(
        name="ligand_intelligence",
        role="ligand",
        families=("chembl", "pubchem", "literature", "research"),
        permissions=("read",),
        note="已知配体、活性与骨架分布",
    ),
    Capability(
        name="literature_evidence",
        role="literature",
        families=("literature", "corpus", "websearch", "research"),
        permissions=("read",),
        note="文献、本地语料与网页证据",
    ),
    Capability(
        name="virtual_screening",
        role="screening",
        families=("screen", "pharmacophore", "receptor", "ligand", "docking", "affinity"),
        permissions=("execute", "write"),
        note="受体准备、对接与候选集；对接与亲和力分成两族，见 P-19",
    ),
    Capability(
        name="scoring_evaluation",
        role="scoring",
        families=("affinity", "analysis", "admet", "research"),
        permissions=("read", "analyze"),
        note="对已有产物做独立评分；默认不带对接能力",
    ),
    Capability(
        name="mechanism_analysis",
        role="mechanism",
        families=("analysis", "pocket", "sar", "literature", "research"),
        permissions=("read", "analyze"),
        note="结合模式、关键残基与构效关系",
    ),
    Capability(
        name="hypothesis_validation",
        role="hypothesis",
        families=("literature", "analysis", "sar", "research"),
        standalone_tools=("hypothesis",),
        permissions=("read", "analyze", "write"),
        note="支持与反证整理；写的是假设状态，不是产物",
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
    """一个角色承担的全部能力，按表里的顺序。"""
    return tuple(item for item in CAPABILITIES if item.role == role)


def families_for(role: str) -> tuple[str, ...]:
    """一个角色需要的工具族：去重、保持表里的顺序。

    顺序会影响 `load_tools` 返回的清单顺序，而清单顺序会影响模型先看见什么，
    所以不去排序，只去重。
    """
    out: list[str] = []
    for item in capabilities_for(role):
        for family in item.families:
            if family not in out:
                out.append(family)
    return tuple(out)


def standalone_for(role: str) -> tuple[str, ...]:
    """一个角色要用到的常驻工具（不属于任何族）。"""
    out: list[str] = []
    for item in capabilities_for(role):
        for tool in item.standalone_tools:
            if tool not in out:
                out.append(tool)
    return tuple(out)


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
    table = catalogue if catalogue is not None else catalog()
    known_tools = core_tools if core_tools is not None else _core_tool_names()

    seen: set[str] = set()
    for item in capabilities:
        if item.name in seen:
            problems.append(f"能力重名：{item.name}")
        seen.add(item.name)
        if item.role not in ROLES:
            problems.append(f"{item.name} 的角色不存在：{item.role}")
        if not item.families and not item.standalone_tools:
            problems.append(f"{item.name} 既没有工具族也没有常驻工具，什么也做不了")
        for family in item.families:
            if family not in table:
                problems.append(f"{item.name} 引用了不存在的族：{family}")
        for tool in item.standalone_tools:
            if tool not in known_tools:
                problems.append(f"{item.name} 引用了不存在的常驻工具：{tool}")
            if any(tool in names for names in table.values()):
                problems.append(
                    f"{item.name} 把 {tool} 声明成常驻工具，但它登记在某一个族里："
                    "两处声明会打架"
                )
        for permission in item.permissions:
            if permission not in PERMISSIONS:
                problems.append(f"{item.name} 的权限取值不认识：{permission}")
        if not item.permissions:
            problems.append(f"{item.name} 没有声明权限：读还是写说不清")
    return problems


def _core_tool_names() -> frozenset[str]:
    """常驻工具的名字。延迟 import：装配清单要构造工具对象，代价不小。"""
    from pathlib import Path

    from vsagent.core.agent.coretools import build_core_tools

    tools = build_core_tools(
        root=Path.cwd(), memory=None, sessions=None, bash_timeout=120, allow_agents=True
    )
    return frozenset(tool.spec.name for tool in tools)
