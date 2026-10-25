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
`families()` 就是这套分组的唯一真相：族名、给人看的一句话、以及哪些工具属于它。
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

# 族名、一句话说明、模块。顺序即 `load_tools` 返回清单里的顺序。
FAMILIES: tuple[tuple[str, str, ModuleType], ...] = (
    ("listing", "工作区与登记表（list_workspace / describe_entry）", listing),
    ("uniprot", "靶点检索（search_targets / fetch_target）", uniprot),
    ("structure", "结构检索与导入（search_pdb / fetch_structure / import_structure）", structure),
    ("chembl", "活性数据与类似物（search_actives / find_analogs）", chembl),
    ("rsearch", "反向检索（按结构找靶点）", rsearch),
    ("literature", "文献检索（search_literature）", literature),
    ("corpus", "本地语料（search_corpus / import_documents / read_source）", corpus),
    ("websearch", "网页检索（search_web / fetch_webpage）", websearch),
    ("receptor", "受体准备（prepare_receptor）", receptor),
    ("ligand", "配体准备（import_ligandset / prepare_ligands）", ligand),
    ("docking", "对接（dock）", docking),
    ("affinity", "亲和力打分（score_affinity）", affinity),
    ("export", "运行目录与查看器（write_run / open_viewer）", export),
)

# 族名 -> 该族的工具名。第一次用到时建一次，之后直接查。
_CATALOG: dict[str, tuple[str, ...]] | None = None


def families() -> tuple[tuple[str, str, ModuleType], ...]:
    """全部族的声明，供 `load_tools` 生成说明。"""
    return FAMILIES


def catalog() -> dict[str, tuple[str, ...]]:
    """族名 -> 工具名，按族内装配顺序。"""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = {
            name: tuple(tool.spec.name for tool in module.domain_tools())
            for name, _label, module in FAMILIES
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
    return [tool for _name, _label, module in FAMILIES for tool in module.domain_tools()]
