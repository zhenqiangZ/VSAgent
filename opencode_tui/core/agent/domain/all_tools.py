"""把领域工具合成一份清单，供应用一次装完。

单独一个文件而不是让 `app.py` 逐个模块展开，理由是**装配处只该知道一件事**：
有一组领域工具要装。`app.py` 里出现五行 `from .domain import xxx` 之后，
每加一个领域模块都要回去改一次装配，而那个文件的本职是管屏幕与状态；
改动分散在两处，漏掉一处的结果是「工具写了但模型看不见」。

顺序有意如此：先浏览、再检索、最后改动。**浏览排在最前**——模型的第一件事
应当是看清手上有哪些条目，而不是先动手。同一份清单也决定了工具在请求体里的
先后，靠前的更容易被选中。
"""

from __future__ import annotations

from ..tools import Tool
from . import (
    affinity,
    chembl,
    corpus,
    docking,
    export,
    ligand,
    listing,
    literature,
    receptor,
    rsearch,
    structure,
    uniprot,
    websearch,
)


def domain_tools() -> list[Tool]:
    """全部领域工具，按装配顺序。"""
    return [
        *listing.domain_tools(),
        *uniprot.domain_tools(),
        *structure.domain_tools(),
        *chembl.domain_tools(),
        *rsearch.domain_tools(),
        *literature.domain_tools(),
        *corpus.domain_tools(),
        *websearch.domain_tools(),
        *receptor.domain_tools(),
        *ligand.domain_tools(),
        *docking.domain_tools(),
        *affinity.domain_tools(),
        *export.domain_tools(),
    ]
