"""把领域工具合成一份清单，供应用一次装完。

单独一个文件而不是让 `app.py` 逐个模块展开，理由是**装配处只该知道一件事**：
有一组领域工具要装。`app.py` 里出现五行 `from .domain import xxx` 之后，
每加一个领域模块都要回去改一次装配，而那个文件的本职是管屏幕与状态；
改动分散在两处，漏掉一处的结果是「工具写了但模型看不见」。

顺序有意如此：先浏览、再检索、最后改动。**浏览排在最前**——模型的第一件事
应当是看清手上有哪些条目，而不是先动手。同一份清单也决定了工具在请求体里的
先后，靠前的更容易被选中。

**2026-10 起这份清单还兼一个用途：按族加载。** 24 个领域工具的定义合计约 9.5K token，
每一步都要发；实际一轮任务通常只用到一两族。所以只把「核心工具」（文件、登记表、
任务、事件、记忆、load_tools）常驻，其余按族由 `load_tools` 现装现用。

`FAMILIES` 是这套分组的唯一真相，但**只记「族名 → 模块」，不写「这一族里有什么」**：
那句话原先手写，三处已经跟实际对不上（`structure` 里多写了 `search_pdb`、
`listing` 漏了 `switch_receptor`、`chembl` 漏了 `describe_molecule`）。族里有哪几个
工具一律由 `catalog()` 从模块现数——模型看到的清单因此不可能与真实装配不一致。

**族与模块不是一一对应**。模块是代码的组织（`rsearch.py` 里放着 RCSB 检索的整套机器），
族是给模型看的分组（「结构」这件事包含查条目、取坐标、导入登记表）。所以一个族可以
由几个模块拼成，顺序就是模块在元组里的顺序。
"""

from __future__ import annotations

from types import ModuleType

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

# 族名、组成它的模块。顺序即 `load_tools` 返回清单里的顺序，也是装配顺序。
FAMILIES: tuple[tuple[str, tuple[ModuleType, ...]], ...] = (
    ("listing", (listing,)),
    ("uniprot", (uniprot,)),
    # `search_pdb` 在 `rsearch` 模块里（RCSB 文本搜 / 属性搜 + 批量取配体都在那儿），
    # 但它属于「结构」这件事：查条目 → 取坐标 → 导进登记表是同一个动作的三步。
    # 原先单独立一个 `rsearch` 族，族里只有它一个，说明写的却是「反向检索」——
    # 名不副实，模型于是先装 rsearch，装完才发现里面只有 search_pdb。
    ("structure", (rsearch, structure)),
    ("chembl", (chembl,)),
    ("literature", (literature,)),
    ("corpus", (corpus,)),
    ("websearch", (websearch,)),
    ("receptor", (receptor,)),
    ("ligand", (ligand,)),
    ("docking", (docking,)),
    ("affinity", (affinity,)),
    ("export", (export,)),
)

# 族名 -> 该族的工具名。第一次用到时建一次，之后直接查。
_CATALOG: dict[str, tuple[str, ...]] | None = None


def families() -> tuple[tuple[str, tuple[ModuleType, ...]], ...]:
    """全部族的声明。"""
    return FAMILIES


def catalog() -> dict[str, tuple[str, ...]]:
    """族名 -> 工具名，按族内装配顺序。"""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = {
            name: tuple(
                tool.spec.name for module in modules for tool in module.domain_tools()
            )
            for name, modules in FAMILIES
        }
    return _CATALOG


def tool_family(tool_name: str) -> str:
    """某个工具属于哪一族；不在任何一族里时返回空串。"""
    for family, names in catalog().items():
        if tool_name in names:
            return family
    return ""


def domain_tools() -> list[Tool]:
    """全部领域工具，按装配顺序。"""
    return [
        tool
        for _name, modules in FAMILIES
        for module in modules
        for tool in module.domain_tools()
    ]
