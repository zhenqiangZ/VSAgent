"""可选化学依赖（RDKit / Meeko / gemmi）的守门人。

化学工具（准备受体、导入/准备配体、对接结果转换）要 import RDKit 与 Meeko，而它们装在
`requirements-chem.txt` 这个可选包里——**不装它的人应当照常能用内核**（检索、会话、
界面全都不受影响）。这个模块负责那件事：把「本机没有化学依赖」变成一条
**模型能原样转述给用户**的错误。

**为什么不能靠顶层 import 失败来表达缺依赖。** 工具的导入发生在装配登记表的时候，
那时候炸掉就是整个程序起不来——一个可选依赖有能力拖垮主线，而它本该只影响一个工具。
所以规矩是：**任何模块的顶层都不许 import rdkit**，一律延迟到函数体里；有断言用 AST
扫全仓把这个规矩守住（`tests/test_agent.py::check_chemistry_gate`）。

**为什么做成「工具照常出现在清单里、调用时才报错」。** 另一种做法是缺依赖就让工具
从清单里消失，但那会让模型面对一个它无从解释的缺口：它要么编一个工具调用，
要么对用户说「我做不到」。而这里返回的是一条**可操作**的错——模型的下一步动作
就是把那句安装指令念给用户，用户照做、重启会话，工具就能用了。签名、描述、参数校验
全部与装了依赖时一致，差别只在那一句错上。

**闸门自己也要能被证伪。** 判定「在不在场」用的是**真导入**而不是 `find_spec`：
后者探到名字就算过，看不见「装了却导不进来」，于是闸门放行、工具在函数体里炸。
理由与实测记录见 `_probe_deps`。

本项目的形态与参考实现不同：没有 `@tool` 装饰器，工具是 `Tool` 的子类、
`run` 是异步的。所以这一层给的是一个**同步的 `require_chem()`**，
由工具的 `run` 在开头调一次；没有把「要化学依赖」写成装饰器，是因为包在
`Tool` 实例外面会包错东西，而**包在 `run` 外面要改写一个抽象方法**——
那属于框架层，不该由一个领域模块来做。
"""

from __future__ import annotations

import functools
import importlib
from typing import Sequence

__all__ = [
    "CHEM_DEPS",
    "CHEM_INSTALL_HINT",
    "CHEM_TOOL_DESCRIPTION_NOTE",
    "chem_available",
    "chem_missing_message",
    "missing_chem",
]

CHEM_DEPS: tuple[str, ...] = ("rdkit", "meeko", "gemmi", "numpy", "scipy")
"""化学依赖的**导入名**。

改这里必须同步 `requirements-chem.txt`——两边对不上时，用户会拿到一条照着做也没用的
安装指令（提示说装这个文件，而文件里列的是别的东西）。

**为什么把 numpy / scipy / gemmi 这种「传递依赖」也列进来。** 一般不该这么写：
装了 meeko 就该有它们。但 meeko 0.8.0 的元数据里 `requires_dist` 是空的——它
**不声明依赖**，于是「装了 meeko 就有 numpy」这件事没有保障，而一旦真的缺了，
报错现场是 `import meeko` 抛 `ModuleNotFoundError`，离病因（依赖清单里少列了一行）
很远。列进来之后，守门人能直接说清缺的是哪一个。

**gemmi 这一项不是理论上的。** `meeko/__init__.py` → `polymer` → `chemtempgen`
一路 `import gemmi`，所以它是**导入 meeko 的硬前提**；上游不声明它，
单装 meeko 不会顺手装上。这不是推演出来的，是参考实现里 `scripts/probe_chem_stack.py`
实测记下来的：本机「meeko 装好、gemmi 没装」时 `import meeko` 直接抛
`ModuleNotFoundError: No module named 'gemmi'`。
"""

CHEM_INSTALL_HINT = 'pip install -r requirements-chem.txt'
"""装上化学依赖的命令。

**刻意不写成 `pip install -e ".[chem]"`**：那个形态要求本仓库是「可安装的包」，
而本项目是要冻结成单文件产物的应用，方括号里的 extra 名还要有人去维护一处
`pyproject.toml`。一个平铺的依赖文件少一层间接，也少一处会不同步的地方。
"""

CHEM_TOOL_DESCRIPTION_NOTE = "（需要本机安装化学依赖，见下文的安装指引）"
"""化学工具描述里该跟的一句。

描述的第一段是唯一会进工具清单的部分，所以「这个工具要额外依赖」属于必须放在那里的
关键语义——让模型在**调用之前**就有一半心理准备，而不是撞了错才知道。

这句话是**原样抄进各工具描述里的**，不是运行时拼的：描述在 `ToolSpec` 里是个字面量，
两处措辞一旦不同步，模型看到的清单里就会少掉这半句提醒。代价是措辞在两处，
所以有一条断言机械守着「每个要化学依赖的工具，描述里都含这句话」。
"""


@functools.lru_cache(maxsize=1)
def _probe_deps() -> tuple[str, ...]:
    """真导入一遍，返回**用不了**的那些。

    **为什么不是 `find_spec`。** 第一版用的是 `find_spec`——探顶层名字、不真导入，
    理由是「探测永远安全」。那个理由本身没错，但它**看不见「装了却导不进来」**：
    实测本机 `find_spec("meeko")` 正常返回，而 `import meeko` 抛
    `ModuleNotFoundError: No module named 'gemmi'`（meeko 不声明依赖，
    `pip install` 不会替它装）。于是闸门放行、工具在函数体里炸，报错现场离病因
    （依赖清单里少一行）很远——**探不出问题的闸门等于没有闸门。**

    报的是 `exc.name`（真正缺席的那个）而不是正在探测的那个名字：用户需要装的是
    gemmi，说「缺 meeko」只会让他把已经装好的东西再装一遍。

    **返回值可能带说明文字**（`"meeko（装了但导入失败：…）"`），不全是裸名字——
    那种情况下安装指令会说 already satisfied，只报名字等于把用户送进绝路。
    两个 `except` 分支的措辞区别是有意的：前者是「缺」，后者是「坏了」，修法不同。
    """
    problems: list[str] = []
    for name in CHEM_DEPS:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            problems.append(exc.name or name)
        except Exception as exc:  # noqa: BLE001 —— 装上了但导入就炸，原因必须带上
            problems.append(f"{name}（装了但导入失败：{type(exc).__name__}: {exc}）")
    return tuple(dict.fromkeys(problems))


def missing_chem() -> list[str]:
    """当前环境**用不了**的化学依赖。

    结果缓存（见 `_probe_deps`）：一个会话里装上依赖不会被看到——安装提示本来就是
    这么说的（「装完重启会话」）。不缓存的话，每次化学工具调用都要真导入一遍 meeko，
    实测 1.2 秒。断言要改环境就 `_probe_deps.cache_clear()`，与状态文件那套同一个套路。
    """
    return list(_probe_deps())


def chem_available() -> bool:
    """化学工具能不能用。给断言与界面上的配置检查用。"""
    return not missing_chem()


def chem_missing_message(missing: Sequence[str] | None = None) -> str:
    """缺依赖时给模型看的那句话。

    「缺什么 + 影响什么 + 怎么装」三样都要有：只说「模块不存在」，模型除了道歉没有
    别的动作可做；带上安装指令，它就能把这句话原样交给用户。
    """
    names = list(missing) if missing is not None else missing_chem()
    return (
        f"本机缺化学依赖：{'、'.join(names)}。"
        "准备受体、导入/准备配体、对接结果转换这几个工具要它，其余功能不受影响。"
        f"装上它：{CHEM_INSTALL_HINT}，装完重启会话即可。"
    )


def require_chem() -> None:
    """确认化学依赖在场；不在场就抛一条能照着做的错。

    抛 `ToolError` 而不是 `ImportError`：前者是「这次调用不行、重试也没用、
    但用户能修」，后者到模型手里就是一句它无从转述的调用链信息。也不抛
    `RetryableToolError`——装依赖这件事不会因为重试而发生。
    """
    from ..tools import ToolError

    missing = missing_chem()
    if missing:
        raise ToolError(chem_missing_message(missing))
