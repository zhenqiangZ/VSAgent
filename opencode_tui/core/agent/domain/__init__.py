"""领域工具：认识「靶点」「配体」「受体」的那些工具。

内核那批工具（bash / read / write / patch / grep）不认识任何业务概念，
这组认识。分界线按**它需要知道什么**划，不按它做什么：

- 需要知道「UniProt 编号长什么样」「口袋残基怎么算」「配体效率怎么定义」的 → 这里
- 只需要知道「有个文件、有段文字」的 → 内核那批
## 八条纪律

这些不是风格喜好，是踩过的坑。新加领域工具时逐条对一遍：

1. **收 handle，不收路径。** 模型编不出 `ligandset#3`，却编得出 `D:/data/lib.csv`——
   后者可能不存在、可能是上一次那个文件、可能是它在上一轮编的。全库只允许三个动作
   收路径（`import_structure`、`import_ligandset`、`import_documents`，因为这三下
   真的需要人给出文件），其余一律收登记表里的 handle 或语料的引用号。
2. **纯查询不声明 `context_argument`。** 查完不留东西的工具挂了登记表，会让模型以为
   它留下了什么，然后在后面的步骤里引用一个不存在的 handle。
3. **失败返回结果，不抛异常。** `ToolResult(failed=True, output=原因)`：错误文本要能
   回灌给模型自纠，抛出去就只剩一条调用链。
4. **只重试瞬时故障。** 用 `RetryableToolError` 标出网络抖动这一类；参数写错、
   依赖没装重试多少次都一样。
5. **返回值要么有预算，要么就别返回。** 长清单按条数截断，并在末尾写明「还有多少条没显示、
   怎么取到」。截断了不说，模型会以为自己看到了全部。
6. **描述的第一段就是工具清单里那一行。** 打分方向、成本量级这类关键语义必须写在
   第一段，后面的 Args 只有参数提示时才展开。
7. **要算数就用代码算。** RMSD、Tanimoto、接触距离、配体效率这类绝不交给模型心算——
   它算不对，而且算错时看上去很像对的。
8. **可选依赖不许在顶层 import。** RDKit / Meeko 这类在函数体里延迟导入，
   缺依赖时返回一条带安装指令的错误，而不是让整个程序起不来。

## 模块

| 模块 | 内容 |
|---|---|
| `http.py` | 全仓唯一的网络出口，测试整体替换的就是它 |
| `uniprot.py` | 靶点检索与取详情（2 个工具） |
| `pdbtext.py` | PDB 纯文本编辑层（零第三方依赖，不登记工具） |
| `preprocess.py` | 结构清洗与杂原子组几何筛查（不登记工具） |
| `structure.py` | 取结构、导结构、结构评估（3 个工具） |
| `chembl.py` | 已知活性与类似物（3 个工具） |
| `chemistry.py` | 可选化学依赖的守门人（不登记工具） |
| `export.py` | 从坐标算口袋的纯几何（第 1 步）+ `write_run` / `open_viewer` 两个工具（第 4 步） |
| `complexes.py` | P0 主来源：搜该蛋白所有复合物 → 排序 → 下载坐标（不登记工具） |
| `docking.py` | AutoDock Vina 对接（1 个工具） |
| `prankweb.py` | P1 副选：prankweb.cz 的 p2rank 预测（状态轮询 + 缓存，不登记工具） |
| `products.py` | 内容寻址产物目录、`params.json` 提交标记、原子写盘（不登记工具） |
| `pockets.py` | 四层口袋来源的装配点 + `detect_pockets` / `define_pocket` 两个工具（第 2 步） |
| `analysis.py` | 把多路分数合成候选表：对齐、平均秩、分档（第 4 步之后） |
| `screen.py` | 不看靶点的通道：2D 指纹相似度 + 3D 形状（USR），两个工具 |
| `admet.py` | 类药性规则层：ESOL / Veber / BBB / CYP 警示（1 个工具） |
| `sar.py` | 构效关系：骨架分组、匹配分子对、类似物枚举、实验回填（4 个工具） |
| `pharmacophore.py` | 共识药效团：从活性里提炼特征点 + 按距离指纹筛库（2 个工具） |
| `align.py` | 先验清单的取证与叠合：核序列、按 CA 把配体搬进受体参考系（不登记工具） |
| `rsearch.py` | 搜索工具（一）：RCSB 文本/属性搜 + GraphQL 批量取配体（1 个工具） |
| `literature.py` | 搜索工具（二）：PubMed E-utilities 文献检索（1 个工具） |
| `websearch.py` | 搜索工具（三）：联网搜索与单页抓取（2 个工具，结果落进本地语料） |
| `corpus.py` | 本地语料：入库 / 检索 / 按页读原文（3 个工具，免费离线；检索是三路 RRF） |
| `receptor.py` | 准备受体（1 个工具） |
| `ligand.py` | 导入 / 准备 / 筛库 / 参考集 / 叠合（5 个工具） |
| `listing.py` | 浏览登记表（3 个工具） |
| `all_tools.py` | 把上面各模块的工具合成一份，供应用一次装完 |

调试入口在**仓库根目录**的 `debug_tools.py`（不在包内，也不登记工具）：它被各模块
文件末尾的 `if __name__ == "__main__"` 块导入，也可以直接从根目录跑。
"""

from __future__ import annotations

__all__ = [
    "admet",
    "affinity",
    "align",
    "analysis",
    "chembl",
    "chemistry",
    "complexes",
    "corpus",
    "docking",
    "export",
    "http",
    "ligand",
    "listing",
    "literature",
    "pdbtext",
    "pharmacophore",
    "pockets",
    "prankweb",
    "preprocess",
    "products",
    "receptor",
    "rsearch",
    "sar",
    "screen",
    "structure",
    "uniprot",
    "websearch",
]
