"""按注册表现状生成 `docs/tools.md`。

文案部分（怎么读、按任务找工具）写在 `HEAD`、`READING`、`BY_TASK` 里；
每个工具的小节从 `ToolSpec` 直接取，参数表从 JSON Schema 生成，避免手抄漂移。

    python tools/gen_tools_doc.py

`tools/check_tools_doc.py` 调同一个 `render()`，与仓库里那一份逐字节比对。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vsagent.core.agent.coretools import build_core_tools  # noqa: E402
from vsagent.core.agent.domain.all_tools import MODULES  # noqa: E402
from vsagent.core.agent.roles.prompt import estimate_tokens  # noqa: E402


CORE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("文件与命令", ("bash", "read", "write", "patch", "grep")),
    ("记忆、任务与假设", ("remember", "recall", "task", "hypothesis")),
    ("会话历史", ("read_turn", "search_history", "close_topic", "recall_event")),
    ("开工盘点", ("survey_structures",)),
    ("呈现与交付", ("write_run", "open_viewer")),
    ("多 agent", ("agent_status", "dispatch_campaign")),
)

MODULE_TITLES: dict[str, str] = {
    "listing": "浏览登记表",
    "uniprot": "靶点信息",
    "rsearch": "实验结构检索",
    "structure": "实验结构与预测结构",
    "pockets": "结合口袋",
    "pocketcmp": "口袋比较",
    "chembl": "已知活性",
    "pubchem": "化合物身份",
    "opentargets": "可成药性与临床证据",
    "literature": "文献检索",
    "corpus": "本地语料",
    "websearch": "联网检索",
    "receptor": "准备受体",
    "ligand": "配体库",
    "screen": "不看靶点的粗筛",
    "docking": "对接",
    "finetune": "微调亲和力模型",
    "affinity": "亲和力预测",
    "analysis": "结果分析",
    "admet": "类药性",
    "sar": "构效关系",
    "pharmacophore": "药效团",
    "research": "研究对象与证据",
}

READING = """## 0. 怎么读这份清单

**清单由角色决定。** 每个角色能看见哪些工具，写在 `vsagent/core/agent/roles/agents.toml` 里：每个角色一条名单，会话开始时就定下来，中途不能自己装卸。常驻工具按三档装配（`orchestrator` / `worker` / `full`），领域工具逐条列出。改名单只改这个文件，不用动代码。

**根会话只编排。** 有可执行计划时，根会话拿到的只有 `orchestrator` 档的常驻工具，领域结果靠 `agent_status` 从子会话取回。要算东西就派给对应角色，不要指望根会话自己调领域工具。

**参数表的读法。** 「必填」指请求里必须带这个键，缺了工具直接报错；「选填」指可以不带，不带时按说明里的默认值处理。类型取自工具声明的 JSON Schema，`array` 表示字符串数组。

**引用一律用编号。** 登记表里的产物用 `receptor#1`、`ligandset#3`、`artifact#4` 这样的编号引用。领域工具里只有 `import_structure`、`import_ligandset`、`import_documents`、`record_assay` 收本机路径，因为这几样的来源在磁盘上，模型编不出来。其余领域工具收路径一律视为错误用法。

**写盘类工具有预演。** `prepare_receptor`、`prepare_ligands`、`fit_affinity`、`switch_receptor` 默认只报告会发生什么，带 `confirm=true` 才真正执行；`dispatch_campaign` 的 `confirm` 是必填项，传 `false` 只返回计划预览。第一次调用不带 `confirm`，看过影响范围再决定。

**需要本机依赖的工具。** 化学相关的工具（准备受体、准备配体、相似度、形状、对接、类药性、构效关系、药效团）需要 RDKit、Meeko、Vina 等依赖；亲和力那两件需要独立的 DTA 环境；`analyze_pose` 的相互作用指纹需要 PLIP。缺依赖时工具返回安装指引，不抛异常。
"""

BY_TASK = """### 3.2 按任务找工具

| 你要做什么 | 先看这几个 |
|---|---|
| 查靶点是谁、有哪些结构 | `target_lookup`、`check_druggability`、`search_pdb`、`survey_structures`、`search_literature` |
| 找口袋、比两个口袋 | `detect_pockets`、`define_pocket`、`compare_pockets`、`similar_pockets` |
| 准备受体与配体 | `prepare_receptor`、`import_ligandset`、`prepare_ligands`、`curate_ligands` |
| 从库里挑一批候选 | `search_actives`、`search_analogs`、`rank_library`、`pick_diverse`、`build_reference_set` |
| 对接与打分 | `dock`、`validate_docking`、`rank_candidates`、`rank_candidates(with_controls=true)` |
| 看位姿对不对 | `analyze_pose`、`contact_map`、`alternative_poses`、`compare_poses`、`analyze_hydrophobic` |
| 亲和力 | `fit_affinity`（先微调）、`score_affinity`（再打分） |
| 构效关系 | `scaffold_analysis`、`matched_pairs`、`enumerate_analogs`、`record_assay` |
| 假设与证据 | `workspace`、`add_evidence`、`review_hypothesis`、`hypothesis` |
| 画图与交付 | `write_run`、`open_viewer` |

### 3.3 参数里的常见约定

- **handle 的前缀**：`receptor#` 受体、`ligandset#` 配体集、`artifact#` 其余产物（口袋、位姿、分数表、报告）。编号只在本次会话有效，跨会话要重新登记。
- **`confirm`**：写盘动作的预演开关。不带只报影响范围，带 `true` 才执行；`dispatch_campaign` 例外，`confirm` 必填。
- **分数方向**：`dock` 的分数越低越好（负得越多越好）；`score_affinity` 给的是 pK，越高越好。两者不可比较，也不能混在一张表里排序。
- **`top` 与 `limit`**：都是截断条数，默认值与上限写在各自的参数说明里；返回值末尾会写「还有多少条没显示」。
- **`name`**：只影响产物目录名与显示，不参与计算，也不要拿它当编号。
"""


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _params(spec: dict) -> str:
    properties = spec.get("parameters", {}).get("properties", {})
    required = set(spec.get("parameters", {}).get("required", []))
    if not properties:
        return "无参数。\n"
    lines = ["| 参数 | 类型 | 必填 | 说明 |", "|---|---|---|---|"]
    for name, item in properties.items():
        kind = item.get("type", "")
        if kind == "array":
            inner = item.get("items", {}).get("type", "string")
            kind = f"array[{inner}]"
        text = item.get("description", "")
        enum = item.get("enum")
        if enum:
            shown = " / ".join(f"`{value}`" for value in enum)
            text = f"可选值：{shown}。" + (text if text else "")
        lines.append(
            f"| `{name}` | {kind} | {'是' if name in required else '否'} | {_cell(text)} |"
        )
    return "\n".join(lines) + "\n"


def _description(text: str) -> str:
    """把说明拆成段：第一行是问句或一句话，其余按标签分段。"""
    parts = [line.strip() for line in text.split("\n") if line.strip()]
    out: list[str] = []
    for index, line in enumerate(parts):
        head = line.split("：", 1)[0]
        if index > 0 and head in ("输入", "输出", "边界"):
            body = line.split("：", 1)[1]
            out.append(f"**{head}：** {body}")
        else:
            out.append(line)
    return "\n\n".join(out) + "\n"


def _cost(spec: dict) -> int:
    return estimate_tokens(json.dumps(spec, ensure_ascii=False))


def render() -> tuple[str, int, int]:
    """渲染整份清单，同时给出常驻与领域的条数（给命令行那一行用）。"""
    core = build_core_tools(root=ROOT, memory=None, sessions=None, bash_timeout=120)
    core_specs = {tool.spec.name: tool.spec.to_openai()["function"] for tool in core}
    modules = [(module.__name__.split(".")[-1], module.domain_tools()) for module in MODULES]
    domain_specs = {
        tool.spec.name: tool.spec.to_openai()["function"]
        for _name, tools in modules
        for tool in tools
    }
    domain_count = sum(len(tools) for _name, tools in modules)

    out: list[str] = []
    out.append("# 工具清单\n")
    out.append(
        "> 生成物，不要手改。改完工具或角色配置之后跑 `python tools/gen_tools_doc.py`；"
        "`tools/check_tools_doc.py` 会逐字节比对。\n\n"
        "本文列出 VSAgent 当前装配的全部工具，按「常驻」与「领域」两层分开写。"
        "每一条给出用途、参数与关键限制。\n\n"
        f"数据来源是 `vsagent/core/agent/coretools.py` 的 `build_core_tools()` 与 "
        f"`vsagent/core/agent/domain/all_tools.py` 的 `domain_tools()`，"
        f"共 **{len(core) + domain_count} 个**：常驻 {len(core)} 个，"
        f"领域 {domain_count} 个（{len(modules)} 个模块）。"
        f"模型实际能看见哪些，由 `roles/agents.toml` 里的角色名单决定。\n"
    )
    out.append("---\n")
    out.append(READING)
    out.append("---\n")
    out.append(f"## 1. 常驻工具（{len(core)} 个）\n")
    out.append("常驻工具的顺序就是请求体里的顺序。子会话看不到 `agent_status` 与 `dispatch_campaign`。\n")
    for order, (title, names) in enumerate(CORE_GROUPS, start=1):
        out.append(f"### 1.{order} {title}\n")
        for name in names:
            spec = core_specs[name]
            out.append(f"#### {name}\n")
            out.append(_description(spec["description"]))
            out.append(_params(spec))
    out.append("---\n")
    out.append(f"## 2. 领域工具（{len(modules)} 个模块，{domain_count} 个）\n")
    out.append("领域工具按模块列，模块顺序与装配顺序一致。\n")
    for order, (module_name, tools) in enumerate(modules, start=1):
        title = MODULE_TITLES[module_name]
        out.append(f"### 2.{order} `{module_name}` {title}（{len(tools)} 个）\n")
        for tool in tools:
            spec = domain_specs[tool.spec.name]
            out.append(f"#### {spec['name']}\n")
            out.append(_description(spec["description"]))
            out.append(_params(spec))
    out.append("---\n")
    out.append("## 3. 速查\n")
    out.append("### 3.1 模块与数量\n")
    out.append("| 模块 | 工具数 | 工具 |")
    out.append("|---|---|---|")
    for module_name, tools in modules:
        names = "、".join(f"`{tool.spec.name}`" for tool in tools)
        out.append(f"| `{module_name}` | {len(tools)} | {names} |")
    out.append(
        f"\n常驻 {len(core)} 个，领域 {domain_count} 个，合计 {len(core) + domain_count} 个。\n"
    )
    out.append(BY_TASK)
    text = "\n".join(out).rstrip() + "\n"
    return text, len(core), domain_count


def main() -> None:
    text, core_count, domain_count = render()
    (ROOT / "docs" / "tools.md").write_text(text, encoding="utf-8")
    print(f"写入 docs/tools.md：{len(text.splitlines())} 行，{core_count + domain_count} 个工具")


if __name__ == "__main__":
    main()
