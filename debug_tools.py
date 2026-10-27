"""领域工具的调试入口：**直接跑，不用敲任何参数**。

放在仓库根目录，不放在 `opencode_tui/core/agent/domain/` 里：它是开发期用的东西，
不该和那 42 个工具挤在同一个目录里。两种跑法，工作目录都在仓库根：

    # 一、全跑：19 个模块、44 次调用（42 个工具），按依赖顺序一个接一个（不需要任何参数）
    .venv/Scripts/python.exe debug_tools.py

    # 二、只跑一个模块（10 个工具模块末尾的块就是这么用的）
    .venv/Scripts/python.exe -m opencode_tui.core.agent.domain.structure

想看清单而不是跑，加 `--list`：

    .venv/Scripts/python.exe debug_tools.py --list
    .venv/Scripts/python.exe -m opencode_tui.core.agent.domain.structure --list

参数与顺序都在本文件的 `CALLS` 里（一次一行，真实的检索词、真实的编号），所以上面
几条命令**一个参数都不用敲**。要单独盯某个工具、或换一组参数时，才用得上显式写法：

    .venv/Scripts/python.exe debug_tools.py structure fetch_structure pdb_id=4HJO
    .venv/Scripts/python.exe -m opencode_tui.core.agent.domain.structure fetch_structure '{"pdb_id": "4HJO"}'

显式写法的参数三种给法：一段 JSON、`键=值`、`--file 路径`（`-` 读标准输入）。
两个开关：`--reset` 清掉上一轮攒下的登记表，`--raw` 跳过参数模式校验
（默认校验，与内核那一步一致）。

几个刻意如此的地方：

- **调用顺序写在 `CALLS` 里，不按模块排。** 先空手看一眼登记表，再依次取靶点、取结构、
  准备受体、写运行目录，最后回来看表里攒下了什么。开跑前把登记表清空，所以
  `fetch_structure` 登记下的那条受体正好能被后面的 `prepare_receptor` 用上，整条链
  在一份登记表里串起来——单跑某个模块时没有这个保证，前置没做过的工具会照实报
  「依赖不存在」。
- **注入物必须是真的。** `Tool.execute` 对「声明了某个通道却收到 None」直接返回
  「内核装配问题」，而 `fetch_target` / `list_workspace` 这类工具里还有
  `isinstance(ledger, SessionLedger)` 的硬校验——所以这里造一个真的 `SessionLedger`，
  拿 None 糊弄只会得到一句与参数无关的报错。
- **登记表写在 `.probe/debug-ledger.json`**，不碰 `~/.opencode_tui/ledger/` 里真实的
  会话快照。单个工具模式下它在两次命令之间是**接着用**的：先跑 `fetch_structure`，
  再跑 `list_workspace`，能看到刚登记的那一条——正好当一轮会话使。全跑模式开跑前
  清空，保证每次跑的都是同一份起点。
- **handle 写占位，不写编号。** 参数里可以写 `{receptor:raw}`（最新的、阶段是 raw 的
  受体）或 `{artifact}`（最新的产物），开跑前从登记表现取。直接写 `receptor#3` 会在
  前面少登记一条时悄悄指错东西，而编号本来就会变。
- **退出码跟着结果走**：全跑时只要有工具失败就是 1；单个工具时报失败是 1、成功是 0。
  这样用 `&&` 串起来、或者以后写进判据都还能用。

有一条是环境决定的：`open_viewer` 要本地结构服务在监听，`python debug_tools.py`
单独跑起来时那个服务不在，它会照实报「没连上」。先起应用或
`python -m opencode_tui.core.web`，这一条才会是绿的。`dock` 同理：PATH 里要有
`vina`，或用 `VSA_VINA_BIN` 指向它；没有引擎时这一条会照实失败，`--list` 不受影响。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from opencode_tui.core.agent.context import EvidenceStore
from opencode_tui.core.agent.ledger import SessionLedger
from opencode_tui.core.agent.tools import Tool, ToolError, argument_error
from opencode_tui.core.models import TaskState
from opencode_tui.env import load_env

# 全跑时依次调用的每一个工具：**顺序与参数都写在这里，一次一行**。
#
# 顺序就是依赖顺序，不按模块排（`domain/__init__.py` 的 `__all__` 是字母序，拿它当
# 顺序会让 `prepare_receptor` 跑在 `fetch_structure` 前面）。开跑前登记表会清空，
# 所以一条链从头串到尾：取结构 → 准备受体 → 写运行目录 → 回来看表。
#
# 参数**不是工具的默认值**，是调试用的样例：真实的检索词、真实的编号、仓内真有的
# 坐标文件。handle 不写编号，写 `{receptor:raw}`（最新的、阶段是 raw 的受体）这类
# 占位，开跑前从登记表现取——编号会随前面登记了几条而变，占位跟着表走。
#
# 新加工具时在这里补一行（漏掉也不会丢，会接在末尾跑，只是顺序不一定对）。
CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    # 先看空表——它会自己说「表是空的」，这就是它空手跑的意义
    ("list_workspace", {}),
    # 靶点：检索 → 取详情
    ("search_targets", {"query": "EGFR", "limit": 5}),
    ("fetch_target", {"accession": "P00533"}),
    # 取一份远端结构（1M17，EGFR 激酶域），登记成 raw 的受体
    ("fetch_structure", {"pdb_id": "1M17"}),
    # 先看一眼这套结构能不能用：方法、分辨率、缺口、交替构象
    ("assess_structure", {"structure": "{receptor:raw}"}),
    # 口袋：先看这份结构自带的口袋（离线，不联网），再选一处。
    # 1M17 带 AQ4，所以 sources=auto 走 ligand 那一层就够。
    ("detect_pockets", {"receptor": "{receptor:raw}"}),
    ("define_pocket", {"pockets": "{artifact:pockets}", "index": 1}),
    # 把它准备成可以直接对接的受体，再设为当前受体。1M17 是单链但带交替构象，
    # 所以只给 altloc；盒子来自上一步的口袋，不再由共晶配体现算。
    # **`ligand=AQ4` 是必须的**：不给就只留盒子、`ligand_file` 为空，后面
    # `validate_docking` 与 `screen_shape` 全都拿不到参照（实测两条都红）。
    ("prepare_receptor", {
        "structure": "{receptor:raw}",
        "pocket": "{artifact:pocket}",
        "ligand": "AQ4",
        "confirm": True,
        "altloc": "A",
    }),
    ("switch_receptor", {"receptor": "{receptor:prepared}"}),
    # 导一份本机坐标（全库唯一收路径的动作）。**排在这里是为了让 `{receptor:raw}`
    # 仍指向 1M17**：它也会登记一条 raw 受体，排在准备受体之前就会被换掉。
    # 路径必须用仓库里那份 `tests/fixtures/1iep.pdb`——原先写 `.probe/1iep.pdb`，
    # 那是 gitignore 的本地文件，干净 checkout 上根本不存在（实测这一步一直红）。
    ("import_structure", {"path": "tests/fixtures/1iep.pdb"}),
    # 导入一份本机配体文件（第二个收路径的动作）：.smi 每行 `SMILES 名字`。
    # 里面的盐会被去掉、无效 SMILES 会被跳过并逐条报出来。
    ("import_ligandset", {"path": "tests/fixtures/ligands.smi"}),
    # 过第一道闸：PAINS / Brenk / 去重 / 性质窗口。夹具 6 条（5 条可用），能筛掉几条。
    ("curate_ligands", {"ligands": "{ligandset:raw}", "max_mw": 500}),
    # 把它准备成 PDBQT：真跑 ETKDGv3 + Meeko，5 个分子通常几秒
    ("prepare_ligands", {"ligands": "{ligandset:raw}", "confirm": True, "limit": 10}),
    # 真跑 Vina：需要 PATH 里有 vina 或设置 VSA_VINA_BIN；没有时这一步会照实失败。
    # 参数调小是为了调试快，不是推荐值。
    ("dock", {
        "receptor": "{receptor:prepared}",
        "ligands": "{ligandset:prepared}",
        "exhaustiveness": 4,
        "num_modes": 3,
        "limit": 3,
        "timeout": 120,
    }),
    # 重对接验证：拿受体产物里的共晶配体当普通配体比一遍位姿。
    # **夹具第一条就是伊马替尼 —— 1IEP 的共晶配体本身（STI）**，而且必须排在
    # `dock` 的 `limit` 之内：排在后面会被 limit 截掉，这一步就只剩「原子数不同，
    # 不是同一个分子」，看着像工具坏了，其实是链上没有可比的分子。
    # 只读已有产物，不跑引擎；阈值默认 2 埃。
    ("validate_docking", {"receptor": "{receptor:prepared}", "docking": "{artifact:docking}"}),
    # 亲和力打分：真加载模型，需要 DTA 的独立 Python 环境（见 models/DTA/README.md）。
    # 没有环境时这一步会照实失败并给出安装指引；limit 调小是为了调试快。
    ("score_affinity", {
        "receptor": "{receptor:prepared}",
        "ligands": "{ligandset:prepared}",
        "limit": 3,
        "timeout": 300,
    }),
    # 多路合成候选表：把上面两路分数对齐、转秩、分档。只读产物，不跑引擎。
    # 上一步没跑成（没有 DTA 环境）时这一条会照实失败并说清缺哪个 handle。
    ("rank_candidates", {
        "sources": "{artifact:docking},{artifact:affinity}",
        "top": 5,
    }),
    # 看某一条的接触依据：只读，不登记。分子用 id（夹具里的名字，不是序号）。
    ("analyze_pose", {"docking": "{artifact:docking}", "molecule": "aspirin"}),
    # 参考活性集：导一份带 pChEMBL 的 CSV（第三个收路径的动作），按活性下限筛一遍。
    # 4 条里 1 条低于 6、1 条命中 PAINS（只标不删），所以参考集只有 3 条 → too_small。
    ("import_ligandset", {"path": "tests/fixtures/actives.csv"}),
    ("build_reference_set", {"actives": "{ligandset:raw}", "min_pchembl": 6}),
    # 两条不看靶点的通道：2D 指纹（毫秒级）与 3D 形状（读已备的 PDBQT）。
    # 库用筛过的那一份；`{ligandset:raw}` 现在指向刚导入的活性集，别用错了。
    ("screen_similarity", {
        "query": "{ligandset:reference}",
        "library": "{ligandset:curated}",
        "top": 5,
    }),
    ("screen_shape", {
        "query": "{receptor:prepared}",
        "library": "{ligandset:prepared}",
        "top": 5,
    }),
    # 类药性：在同一份筛过的库上算规则层指标（不需要 3D、不需要引擎）
    ("predict_admet", {"ligands": "{ligandset:curated}", "endpoints": "solubility,veber,bbb,cyp"}),
    # 构效关系：先分组，再找匹配分子对，最后用第一组枚举一批类似物。
    # 候选表来自 rank_candidates；那一步没跑成时这几条会照实失败。
    ("scaffold_analysis", {"source": "{artifact:candidates}", "min_size": 1}),
    ("matched_pairs", {"source": "{artifact:candidates}", "min_delta": 0.5}),
    ("enumerate_analogs", {"scaffold": "{artifact:scaffolds}", "group": 1, "max": 10}),
    # 药效团：一份同系列 6 个分子（前一节的参考集只有 3 条，凑不出共识）。
    # 这一步要 MCS 叠合，所以用同一个系列的夹具，而不是化学型五花八门的那批。
    ("import_ligandset", {"path": "tests/fixtures/series.csv"}),
    ("align_ligands", {"ligands": "{ligandset:raw}"}),
    ("build_pharmacophore", {"actives": "{ligandset:aligned}", "min_support": 0.5}),
    ("screen_pharmacophore", {
        "pharmacophore": "{artifact:pharmacophore}",
        "library": "{ligandset:raw}",
        "top": 5,
    }),
    # 实验回填（第四个收路径的动作）：夹具里 3 条可解析、1 条写成 n/a
    ("record_assay", {
        "file": "tests/fixtures/assay.csv",
        "campaign": "debug",
        "assay": "IC50",
    }),
    # 两个搜索工具：只给候选、不给结论
    ("search_pdb", {"query": "EGFR kinase", "limit": 5}),
    # 已知活性 → 类似物 → 分子画像
    ("search_actives", {"accession": "P00533"}),
    ("find_analogs", {"query": "CHEMBL939"}),
    ("describe_molecule", {"molecule": "CHEMBL25"}),
    ("search_literature", {"query": "EGFR inhibitor resistance", "limit": 5}),
    # 本地语料：把仓里那份夹具文献导进去 → 检索 → 按页读。
    # 导入会**真的写进 `corpus/doc/`**（已 gitignore），用一份小 PDF 而不是整篇论文。
    # 同一份文件导两次是「重复」，所以重复跑这条也是绿的。
    ("import_documents", {"path": "tests/fixtures/paper-outline.pdf"}),
    ("search_corpus", {"query": "gefitinib IC50", "limit": 3}),
    # 引用号是**文件内容的哈希**（`tests/fixtures/paper-outline.pdf` → 3147dcb3e1）。
    # 夹具换了内容就要跟着换这一行——占位符救不了它，语料没有登记表。
    ("read_source", {"ref": "doc#3147dcb3e1", "page": 2}),
    # 联网搜索：**真花钱**（5 元 / 1000 次成功请求，这一次约 0.005 元）。
    # 结果会落进 corpus/web/，所以跑完 `git status` 里看不到它（整个 corpus/ 已 ignore）。
    ("search_web", {"query": "EGFR T790M resistance mechanism", "count": 3}),
    # 抓单页正文（同样计费）。用一个稳定的公开页面，**不依赖上一条的返回**——
    # 全跑时参数是静态的，占位只认登记表里的 handle，认不了「上一条结果里的某个 URL」。
    ("fetch_webpage", {
        "url": "https://en.wikipedia.org/wiki/Epidermal_growth_factor_receptor",
        "max_chars": 800,
    }),
    # 产物：受体 → 运行目录 → 网页地址
    ("write_run", {
        "receptor": "{receptor:prepared}",
        "docking": "{artifact:docking}",
        "uniprot": "P00533",
    }),
    ("open_viewer", {"run": "{artifact}"}),
    # 最后回来看准备后的受体那一条
    ("describe_entry", {"handle": "{receptor:prepared}"}),
)

# 按工具名查样例参数：单个工具模式（`debug_tools.py <模块> <工具名>`）用它兜底，
# `--list` 也用它印样例命令行。内容就是上面那张表的另一面，不另写一份。
DEFAULTS: dict[str, dict[str, Any]] = {name: arguments for name, arguments in CALLS}

# 参数里的占位：`{receptor}` = 最新的受体，`{receptor:raw}` = 最新的、阶段是 raw 的受体。
# 阶段读条目的 `meta["stage"]`（`structure` 登记 raw、`prepare_receptor` 登记 prepared）。
# 占位可以整段是一个，也可以夹在一串里（`rank_candidates` 的 sources 要两三个 handle）。
INLINE = re.compile(r"\{([a-z]+)(?::([a-z]+))?\}")

# 本文件就在仓库根目录。
REPO_ROOT = Path(__file__).resolve().parent
DEBUG_LEDGER_PATH = REPO_ROOT / ".probe" / "debug-ledger.json"

USAGE_ROOT = (
    "python debug_tools.py [<模块>] [<工具名> [参数…]] [--list] [--raw] [--reset] [--clean]"
)
USAGE_MODULE = "python -m opencode_tui.core.agent.domain.<模块> [<工具名> [参数…]] [--list] [--raw] [--reset]"

SWITCHES = ("--raw", "--reset", "--list", "--clean")
LIST_FLAGS = ("-h", "--help", "--list")


class _ViewerStub:
    """本地结构服务的替身：`open_viewer` 只读 `port` / `web_port` 这两个属性。

    两个都给 0，等于告诉它「端口不知道」——它会自己从 8860 起探一遍，
    应用真在跑的时候就能拿到地址。
    """

    port = 0
    web_port = 0


def main(module_name: str, tools: list[Tool], argv: list[str] | None = None) -> int:
    """模块那一侧的入口：`main(__name__, domain_tools())`。

    不带参数时把这一组的工具**全跑一遍**（参数取自 `CALLS`），加 `--list` 才只列清单。
    """
    load_env()
    return _dispatch(_module_name(module_name), tools, list(sys.argv[1:] if argv is None else argv))


def root_main(argv: list[str] | None = None) -> int:
    """根目录那一侧的入口：第一个词是模块名就用它，是工具名就去找它在哪个模块。

    不带参数时按模块表把 19 个模块、44 条 `CALLS` 全跑一遍；`--clean` 跑完清产物。
    """
    load_env()
    args = list(sys.argv[1:] if argv is None else argv)
    positional = [item for item in args if item not in SWITCHES]
    modules = _tool_modules()
    if not positional:
        if "--list" in args or "-h" in args or "--help" in args:
            _print_all(modules)
            return 0
        return _run_batch(modules, clean="--clean" in args)
    if positional[0] in LIST_FLAGS:
        _print_all(modules)
        return 0
    head = positional[0]
    by_module = dict(modules)
    if head in by_module:
        rest = list(args)
        rest.remove(head)
        return _dispatch(head, by_module[head], rest)
    matched = [(name, tools) for name, tools in modules if _has_tool(tools, head)]
    if len(matched) == 1:
        return _dispatch(matched[0][0], matched[0][1], args)
    if not matched:
        print(f"没有叫 {head} 的模块或工具。模块有：{'、'.join(by_module)}", file=sys.stderr)
        print(f"用法：{USAGE_ROOT}", file=sys.stderr)
        return 2
    print(f"工具名 {head} 在多个模块里都有，请带上模块名：{'、'.join(n for n, _ in matched)}", file=sys.stderr)
    return 2


def _dispatch(module_name: str, tools: list[Tool], args: list[str]) -> int:
    """共同的执行路径：先摘开关，再定位工具、读参数、校验、跑。

    不带工具名时把这一组全跑一遍；`--list` 只列清单。
    """
    if "--reset" in args:
        args.remove("--reset")
        _reset_ledger()
    if "--clean" in args:
        # 只有全跑模式会在跑完之后清；单跑一个工具时清掉产物等于把刚看的东西删了。
        args.remove("--clean")
        print("（--clean 只对全跑有效，这一次忽略）", file=sys.stderr)
    raw = "--raw" in args
    if raw:
        args.remove("--raw")
    listing = "--list" in args
    if listing:
        args.remove("--list")
    if listing or (args and args[0] in ("-h", "--help")):
        _print_tools(module_name, tools)
        return 0
    if not args:
        return _run_module(module_name, tools)
    name = args[0]
    tool = next((item for item in tools if item.spec.name == name), None)
    if tool is None:
        known = "、".join(item.spec.name for item in tools)
        print(f"{module_name} 里没有叫 {name} 的工具。这一组是：{known}", file=sys.stderr)
        print(f"用法：{_usage(module_name)}", file=sys.stderr)
        return 2
    arguments, error = _read_arguments(tool, args[1:])
    if error:
        print(error, file=sys.stderr)
        return 2
    if not raw:
        # 内核在真正执行之前先用参数模式挡一道（`loop._execute_tool`），这里照做：
        # 否则「参数写错了」在调试时看不见，反而要等模型那一侧才暴露。
        error = argument_error(tool.spec, arguments)
        if error:
            print(f"（内核会先挡下这次调用：{error}）", file=sys.stderr)
            print("要绕过这一道看工具自己怎么处理，加 --raw", file=sys.stderr)
            return 2
    ok, text = _execute(tool, arguments)
    print(text)
    return 0 if ok else 1


def _tool_modules() -> list[tuple[str, list[Tool]]]:
    """按 `domain` 包的模块表逐个取 `domain_tools()`。

    模块名不写成一串常量：新加一个领域模块时，只要它在 `domain/__init__.py` 的 `__all__`
    里，这里自动就能看到它。没有 `domain_tools()` 的辅助模块（http / chemistry /
    pdbtext / complexes / prankweb / products）自然被跳过。

    顺序也是**全跑时的顺序**，`__all__` 里就是依赖顺序。
    """
    package = importlib.import_module("opencode_tui.core.agent.domain")
    found: list[tuple[str, list[Tool]]] = []
    for name in package.__all__:
        module = importlib.import_module(f"{package.__name__}.{name}")
        maker = getattr(module, "domain_tools", None)
        if callable(maker):
            found.append((name, maker()))
    return found


def _clean_products() -> None:
    """删掉这一轮登记表指向的产物，只删 `~/.opencode_tui/` 下的那些。

    **不碰 `runs/` 与 `corpus/`**：前者是交付物（`write_run` 的产物，用户可能要拿去
    看页面），后者是语料库，删了要重新入库。只清缓存性质的东西——受体、配体集、
    对接位姿、候选表、验证结果。清理完把调试登记表也删掉，否则单工具模式下一次
    读到的是一串指向空目录的 handle。
    """
    import shutil

    ledger = _debug_ledger(quiet=True)
    root = Path.home() / ".opencode_tui"
    removed: list[str] = []
    kept: list[str] = []
    for entry in ledger.entries(include_archived=True):
        path = Path(str(entry.path))
        try:
            inside = path == root or root in path.parents
        except OSError:
            inside = False
        if not inside:
            kept.append(str(path))
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            removed.append(str(path).replace(str(Path.home()), "~", 1))
        except OSError as error:
            kept.append(f"{path}（没删掉：{error}）")
    try:
        DEBUG_LEDGER_PATH.unlink()
    except OSError:
        pass
    print("=" * 64)
    print(f"清理：删掉 {len(removed)} 个产物、登记表已删。")
    for item in removed[:8]:
        print(f"  - {item}")
    if len(removed) > 8:
        print(f"  …… 还有 {len(removed) - 8} 个")
    if kept:
        print("没删（不在 ~/.opencode_tui 下，或删不掉）：")
        for item in kept[:6]:
            print(f"  · {item}")


def _run_batch(groups: list[tuple[str, list[Tool]]], *, clean: bool = False) -> int:
    """全跑：`CALLS` 里那 44 条调用依次跑一遍，最后给一张表。

    开跑前清空登记表：全跑的价值在于「一条链从头串到尾」，起点必须是同一份空表。
    单个工具失败不打断后面的——调试要看的正是「哪一个不行、为什么」。
    """
    _reset_ledger()
    plan = _plan(groups)
    distinct = len({tool.spec.name for _label, tool, _args in plan})
    head = (
        f"全跑：{len(groups)} 个模块、{len(plan)} 次调用（{distinct} 个工具），按依赖顺序来。\n"
        f"登记表：{_relative(DEBUG_LEDGER_PATH)}（开跑前已清空）"
    )
    code = _run_plan(plan, head)
    if clean:
        _clean_products()
    return code


def _run_module(module_name: str, tools: list[Tool]) -> int:
    """单跑一个模块：这一组的工具依次跑一遍。

    登记表**接着上一轮用**（与只跑一个工具时一致），所以全跑过一遍之后再来单跑某个
    模块，前面的 `receptor#1` / `artifact#1` 还在，依赖是齐的；从零开始单跑就得
    自己先补前置，工具会照实报「依赖不存在」。
    """
    short = module_name.rsplit(".", 1)[-1]
    plan = [
        (f"{short}.{tool.spec.name}", tool, DEFAULTS.get(tool.spec.name)) for tool in tools
    ]
    head = f"跑 {short} 这一组：{len(plan)} 个工具（登记表接着上一轮用）。"
    return _run_plan(plan, head)


def _run_plan(plan: list[tuple[str, Tool, dict[str, Any] | None]], head: str) -> int:
    """执行一串 (标签, 工具, 参数)：逐个打印参数与结果，末尾给小结。"""
    total = len(plan)
    print(head + "\n")

    report: list[tuple[str, bool, str, float]] = []
    started = time.monotonic()
    for index, (label, tool, sample) in enumerate(plan, start=1):
        name = tool.spec.name
        print(f"==== [{index}/{total}] {label} " + "=" * max(2, 60 - len(label)))
        if sample is None:
            print("（`CALLS` 里没有它，跳过——补一行就能跑）\n")
            report.append((label, False, "跳过", 0.0))
            continue
        ledger = _debug_ledger(quiet=True)
        arguments, error = _resolve_handles(dict(sample), ledger)
        if error:
            print(f"（{error}）\n")
            report.append((label, False, "跳过", 0.0))
            continue
        print(f"参数：{_json(arguments)}")
        begin = time.monotonic()
        ok, text = _execute(tool, arguments, ledger)
        cost = time.monotonic() - begin
        print(text)
        print()
        report.append((label, ok, "正常" if ok else "失败", cost))

    print("=" * 64)
    print(_summary(report, time.monotonic() - started))
    return 0 if all(ok for _, ok, _, _ in report) else 1


def _plan(groups: list[tuple[str, list[Tool]]]) -> list[tuple[str, Tool, dict[str, Any] | None]]:
    """按 `CALLS` 的顺序排出这一轮要跑的 (标签, 工具, 参数)。

    **一条 `CALLS` 就是一次调用**，同名工具可以出现多次（三次 `import_ligandset`
    各导入一份不同的夹具，供后面三步各取所需）。原先按工具名查参数、并把工具从池子里
    `pop` 掉，于是这三条被合成一条、用的是**最后那条**的参数——链看起来跑满 42 个，
    实际 `ligands.smi` 与 `actives.csv` 一次都没导入，参考集与指纹筛选全建在错的库上。

    `CALLS` 没提到的工具接在末尾（参数为 None，跑的时候报「跳过——补一行就能跑」），
    新加的工具这样才不会被漏掉。
    """
    by_tool: dict[str, tuple[str, Tool]] = {}
    for module_name, tools in groups:
        for tool in tools:
            by_tool.setdefault(tool.spec.name, (module_name, tool))
    plan: list[tuple[str, Tool, dict[str, Any] | None]] = []
    used: set[str] = set()
    for name, arguments in CALLS:
        found = by_tool.get(name)
        if found is None:
            continue
        plan.append((f"{found[0]}.{name}", found[1], arguments))
        used.add(name)
    for name, (module_name, tool) in by_tool.items():
        if name not in used:
            plan.append((f"{module_name}.{name}", tool, None))
    return plan


def _summary(report: list[tuple[str, bool, str, float]], elapsed: float) -> str:
    """一张排好的小表：每个工具的耗时与结果，末行给合计。"""
    lines = ["小结："]
    for label, _ok, state, cost in report:
        lines.append(f"  {label:<34s} {cost:6.1f}s  {state}")
    good = sum(1 for _, ok, _, _ in report if ok)
    bad = sum(1 for _, ok, state, _ in report if not ok and state == "失败")
    skipped = sum(1 for _, ok, state, _ in report if not ok and state == "跳过")
    tail = f"  共 {len(report)} 个：正常 {good}"
    if bad:
        tail += f"、失败 {bad}"
    if skipped:
        tail += f"、跳过 {skipped}"
    lines.append(tail + f"；总计 {elapsed:.1f}s")
    return "\n".join(lines)


def _resolve_handles(
    arguments: dict[str, Any], ledger: SessionLedger
) -> tuple[dict[str, Any], str]:
    """把参数里的 `{receptor:raw}` 这类占位换成登记表里现成的那一条 handle。

    占位可以整段是一个（`"receptor": "{receptor:raw}"`），也可以夹在一串里
    （`"sources": "{artifact:docking},{artifact:affinity}"`）——后者是
    `rank_candidates` 这类「收一串 handle」的工具要的。不含 `{` 的值原样留着，
    所以 `"receptor#1"`、`"CHEMBL25"` 这些不受影响。
    表里还没有这样的条目时返回一条说明——那多半是前置没跑，照实报出来比让它去猜好。
    """
    resolved = dict(arguments)
    for key, value in arguments.items():
        if not isinstance(value, str) or "{" not in value:
            continue
        error: list[str] = []

        def replace(match: re.Match[str]) -> str:
            kind, stage = match.group(1), match.group(2) or ""
            entry = _latest_entry(ledger, kind, stage)
            if entry is None:
                want = f"{kind}/{stage}" if stage else kind
                error.append(
                    f"{key} 要的是 {match.group(0)}，但登记表里还没有这样的条目（{want}）——"
                    "先把它跑出来，或者直接给一个具体的 handle"
                )
                return match.group(0)
            return str(entry.handle)

        resolved[key] = INLINE.sub(replace, value)
        if error:
            return {}, error[0]
    return resolved, ""


def _latest_entry(ledger: SessionLedger, kind: str, stage: str) -> Any:
    """登记表里最新的那一条：同类型里序号最大的。给了 stage 就只看那个阶段。"""
    found = ledger.find(kind=kind)
    if stage:
        found = [entry for entry in found if str(entry.meta.get("stage", "")) == stage]
    return found[-1] if found else None


def _execute(
    tool: Tool, arguments: dict[str, Any], ledger: SessionLedger | None = None
) -> tuple[bool, str]:
    """按内核的口径跑一次工具，返回 (是否成功, 要印出来的文本)。

    工具允许抛 `ToolError`（参数写错这类失败是**抛**出来的，内核那一侧会把它转成一条
    工具结果）；这里收成文本，好让全跑模式接着往下走，而不是整批断在这里。
    """
    ledger = ledger if ledger is not None else _debug_ledger()
    arguments, error = _resolve_handles(arguments, ledger)
    if error:
        return False, error
    try:
        result = asyncio.run(
            tool.execute(
                arguments,
                context=ledger,
                task_state=TaskState(),
                evidence=EvidenceStore(),
                viewer=_ViewerStub(),
            )
        )
    except ToolError as error:
        return False, f"工具抛出了 {type(error).__name__}：{error}"
    except Exception as error:  # noqa: BLE001 - 调试入口要把任何异常都摆出来
        return False, f"没预期的异常 {type(error).__name__}：{error}"
    lines = [result.output]
    if result.meta:
        lines.append(f"meta：{_json(result.meta)}")
    if result.added or result.removed:
        lines.append(f"改动行数：+{result.added} -{result.removed}")
    if result.failed:
        lines.append("（这一次工具报的是失败）")
    return (not result.failed), "\n".join(lines)


def _has_tool(tools: list[Tool], name: str) -> bool:
    return any(item.spec.name == name for item in tools)


def _module_name(raw: str) -> str:
    """`-m` 跑起来时 `__name__` 就是 `"__main__"`，真正的模块名挂在 `__spec__` 上。

    不换回来的话，清单里会印出「模块 __main__.py」与一条抄不动的样例命令行。
    """
    if raw != "__main__":
        return raw
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", None) or raw


def _print_all(modules: list[tuple[str, list[Tool]]]) -> None:
    """根目录那一侧的总清单：哪些模块、各有哪些工具。"""
    total = sum(len(tools) for _, tools in modules)
    print(f"{len(modules)} 个领域模块共 {total} 个工具。")
    print(f"用法：{USAGE_ROOT}")
    for name, tools in modules:
        print(f"\n  [{name}]  {'、'.join(item.spec.name for item in tools)}")
    print(f"\n看某个模块的样例参数：python debug_tools.py <模块> --list")
    print(f"登记表：{_relative(DEBUG_LEDGER_PATH)}（两次命令之间接着用；--reset 清掉它）")


def _print_tools(module_name: str, tools: list[Tool]) -> None:
    """列出这一组工具：说明第一段、必填参数、注入通道，以及现成的样例命令行。"""
    module = module_name.rsplit(".", 1)[-1]
    print(f"{module_name} 登记 {len(tools)} 个工具（模块 {module}.py）。")
    print(f"用法：{_usage(module_name)}")
    for tool in tools:
        spec = tool.spec
        required = spec.parameters.get("required") or []
        injected = "、".join(
            name
            for name in (tool.context_argument, tool.viewer_argument)
            if name
        )
        print(f"\n  {spec.name}")
        print(f"    {_first_line(spec.description)}")
        line = f"    必填：{'、'.join(required) if required else '（无）'}"
        if injected:
            line += f"    注入：{injected}（这里自动造）"
        print(line)
        sample = DEFAULTS.get(spec.name)
        if sample is None:
            print("    样例：没有现成的，自己给一段 JSON 或 键=值")
        else:
            print(f"    样例：{_sample_command(module_name, spec.name, sample)}")
    print(f"\n登记表：{_relative(DEBUG_LEDGER_PATH)}（两次命令之间接着用；--reset 清掉它）")
    print("参数模式校验与内核那一步一致；要绕过它加 --raw")


def _usage(module_name: str) -> str:
    """按进来那一侧的写法给用法：短模块名（根目录入口）给根目录写法。"""
    return USAGE_MODULE if "." in module_name else USAGE_ROOT


def _sample_command(module_name: str, tool_name: str, sample: dict[str, Any]) -> str:
    """样例命令行。**要能直接抄走**，所以两种入口各印自己那一种。"""
    head = f"-m {module_name}" if "." in module_name else f"debug_tools.py {module_name}"
    return f"{head} {tool_name} {_quote(_json(sample))}"


def _read_arguments(tool: Tool, rest: list[str]) -> tuple[dict[str, Any], str]:
    """从命令行读参数，返回 (参数, 出错说明)。出错说明非空时调用方直接报出去。

    四种写法依次是：`--file 路径`、一段 JSON、若干 `键=值`、什么都不给（用样例参数）。
    """
    if not rest:
        sample = DEFAULTS.get(tool.spec.name)
        if sample is None:
            return {}, (
                f"{tool.spec.name} 没有现成的样例参数，"
                "请给它一段 JSON 或 键=值，例如 query=EGFR"
            )
        print(f"（没给参数，用样例：{_json(sample)}）", file=sys.stderr)
        return dict(sample), ""
    if rest[0] in ("--file", "-f"):
        if len(rest) < 2:
            return {}, "--file 后面要跟一个路径（`-` 表示从标准输入读）"
        return _read_file(rest[1])
    raw = " ".join(rest)
    if raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError as error:
            return {}, f"这段 JSON 没解开：{error}"
        if not isinstance(parsed, dict):
            return {}, '参数要是一个 JSON 对象，例如 {"query": "EGFR"}'
        return parsed, ""
    return _read_pairs(rest)


def _read_pairs(rest: list[str]) -> tuple[dict[str, Any], str]:
    """把若干 `键=值` 收成参数字典。值先按 JSON 解，解不开就当字符串。"""
    arguments: dict[str, Any] = {}
    for item in rest:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            return {}, f"看不出来 {item!r} 是参数：写成 键=值，或者给一段 JSON"
        arguments[key.strip()] = _loose_value(value)
    return arguments, ""


def _loose_value(text: str) -> Any:
    """值先按 JSON 解（数字、true/false、带引号的串、数组都认），解不开就当字符串。"""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _read_file(target: str) -> tuple[dict[str, Any], str]:
    """从文件或标准输入读一段 JSON 参数。"""
    if target == "-":
        text = sys.stdin.read()
        where = "标准输入"
    else:
        path = Path(target).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            return {}, f"读 {path} 出错：{error}"
        where = str(path)
    try:
        parsed = json.loads(text)
    except ValueError as error:
        return {}, f"{where} 里的 JSON 没解开：{error}"
    if not isinstance(parsed, dict):
        return {}, f"{where} 里要是一个 JSON 对象"
    return parsed, ""


def _debug_ledger(quiet: bool = False) -> SessionLedger:
    """造一份调试用的登记表，写在 `.probe/` 下，并接着上一轮的内容。

    全跑时逐个工具都重新读一遍盘：前一个工具刚登记的东西，后一个要看得见。
    `quiet` 用在全跑里——那句话印 15 遍只会把输出淹了。
    """
    DEBUG_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ledger = SessionLedger(session_id="debug", snapshot_path=DEBUG_LEDGER_PATH)
    if ledger.load() and not quiet:
        print(f"（登记表接着 {_relative(DEBUG_LEDGER_PATH)} 用）", file=sys.stderr)
    return ledger


def _reset_ledger() -> None:
    """清掉调试登记表，让下一次跑从空表开始。"""
    if DEBUG_LEDGER_PATH.is_file():
        DEBUG_LEDGER_PATH.unlink()
        print(f"（已清掉 {_relative(DEBUG_LEDGER_PATH)}）", file=sys.stderr)


def _first_line(text: str) -> str:
    """说明的第一段——它就是工具清单里给模型看的那一行。"""
    line = text.strip().split("\n", 1)[0].strip()
    return line if len(line) <= 80 else line[:79] + "…"


def _json(value: Any) -> str:
    """紧凑的 JSON，中文不转义（样例参数要能直接抄进命令行）。"""
    return json.dumps(value, ensure_ascii=False)


def _quote(text: str) -> str:
    """把一段参数裹成 shell 认的单引号形式（Windows 的 Git Bash 也认）。"""
    return "'" + text.replace("'", "'\\''") + "'"


def _relative(path: Path) -> str:
    """能写短就写短：路径在仓库里时给相对路径。"""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(root_main())
