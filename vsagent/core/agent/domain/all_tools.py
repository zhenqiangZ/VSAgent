"""把领域工具合成一份清单，供应用一次装完。

单独一个文件而不是让 `app.py` 逐个模块展开，理由是**装配处只该知道一件事**：
有一组领域工具要装。`app.py` 里出现五行 `from .domain import xxx` 之后，
每加一个领域模块都要回去改一次装配，而那个文件的本职是管屏幕与状态；
改动分散在两处，漏掉一处的结果是「工具写了但模型看不见」。

**模块是代码的组织单位。** `MODULES` 决定装配顺序，也就是请求体里的顺序：
先浏览、再检索、最后改动。浏览排在最前——模型的第一件事应当是看清手上有哪些
条目，而不是先动手。靠前的更容易被选中。

哪些工具发给哪个角色，不在这里决定：`roles/agents.toml` 按名字逐条列出，
`roles/agents.py` 负责读。这里只负责「全部领域工具有哪些、按什么顺序排」。
"""

from __future__ import annotations

from types import ModuleType

from ..tools import Tool
from . import (
    admet,
    affinity,
    finetune,
    analysis,
    chembl,
    corpus,
    docking,
    ligand,
    listing,
    literature,
    opentargets,
    pharmacophore,
    pocketcmp,
    pockets,
    pubchem,
    receptor,
    research,
    rsearch,
    sar,
    screen,
    structure,
    uniprot,
    websearch,
)

# 模块的装配顺序。浏览器与登记表在前，检索与取数居中，计算与整理在后。
# 这个顺序就是请求体里的顺序：靠前的更容易被选中，所以「先看清手上有哪些
# 条目」排在最前，「整理结论」排在最后。
MODULES: tuple[ModuleType, ...] = (
    listing,
    uniprot,
    rsearch,
    structure,
    pockets,
    pocketcmp,
    chembl,
    pubchem,
    opentargets,
    literature,
    corpus,
    websearch,
    receptor,
    ligand,
    screen,
    docking,
    finetune,
    affinity,
    analysis,
    admet,
    sar,
    pharmacophore,
    research,
)

def domain_tools() -> list[Tool]:
    """全部领域工具，按装配顺序。"""
    return [tool for module in MODULES for tool in module.domain_tools()]


def all_names() -> tuple[str, ...]:
    """全部领域工具的名字，按装配顺序。"""
    return tuple(tool.spec.name for tool in domain_tools())
