# 筛选结果可视化架构（Mol\* 版）

这一版把上一轮的 3Dmol.js 演示前端换成 Mol\*，并把 AlphaFold 3 的结构与置信度纳入进来。
动笔前先说明为什么换，再定数据形状与模块划分，最后列实施顺序与待定项。

## 一、为什么换 Mol\*

上一轮选 3Dmol.js 的理由是「体积小、嵌入简单、一万原子够用」。现在前提变了：
结构来源改成 AlphaFold 3 预测，随之而来的是一组 3Dmol.js 需要自己搭的能力。

| 能力 | 3Dmol.js | Mol\* |
|---|---|---|
| pLDDT 按原子着色 | 自己算色标、自己写 map | `PluginStateTransform` 里有现成 preset |
| PAE 矩阵与三维视图联动 | 无 | 现成（AlphaFold DB 就是这套） |
| 序列面板与结构联动 | 无 | 现成 `SequenceView` |
| mmCIF 解析（AF3 默认输出） | 基本支持，扩展字段会丢 | 原生，且保留全部元数据 |
| 多模型 / 多态叠合 | 手动加载多个 model | 状态树原生支持 |
| 选区语法 | 单字符串，逗号不生效 | MolQL + `SelectionQuery`，可组合 |

**PAE 联动是决定性的一条。** 预测结构的可信度不是「整条链信不信」，
而是「哪两个区域之间信不信」。用户要在 PAE 图上圈一块、三维视图里对应残基高亮，
这个交互在 3Dmol.js 里要自己实现矩阵绘制与双向联动，Mol\* 里是内置的。
AF3 的 `chain_pair_pae_min` 还能区分结合与非结合，这是筛选场景直接要用的读数。

代价是体积与集成复杂度都上一个量级，下面第三节说怎么控制。

## 二、Mol\* 的四种集成方式

官方文档列了四条路，本项目的情况对应第三条。

| 方式 | 适用 | 本项目 |
|---|---|---|
| `Viewer` 包装（`molstar.js` 全局变量） | 只想显示一个结构 | 不够，要自定义面板 |
| `PluginContext` + 内置 React UI | 要定制又不想重做界面 | 可选 |
| **`PluginContext` 不带 React UI** | 自己完全掌控界面 | **选这条** |
| `Canvas3D` 裸用 | 不用状态管理 | 太底层 |

选第三条的理由：本项目的右侧面板（口袋清单、候选分子表、接触明细）要跟已有的
交互习惯一致，用 Mol\* 自带的 React UI 反而要跟它较劲布局。
Mol\* 在这里只负责三维视口，面板仍然是自己写。

核心 API 就三个：

```js
import { DefaultPluginSpec, PluginSpec } from 'molstar/lib/mol-plugin/spec';
import { PluginContext } from 'molstar/lib/mol-plugin/context';

const plugin = new PluginContext(DefaultPluginSpec());
await plugin.init();
await plugin.initViewerAsync(canvas, parent);

// 取结构数据
const data = await plugin.builders.data.download({ url: '...' }, { state: { isGhost: true } });
const trajectory = await plugin.builders.structure.parseTrajectory(data, 'mmcif');
await plugin.builders.structure.hierarchy.applyPreset(trajectory, 'default');
```

## 三、构建方式与体积

版本与许可（从 npm registry 读到的当前值）：

| 项 | 值 |
|---|---|
| 包名 | `molstar` |
| 版本 | 5.12.0 |
| 许可 | MIT |
| Node 要求 | `>=22.0.0`（本机有 22.22.2，满足） |
| 运行时依赖 | 19 个 |

Mol\* 的完整 `Viewer` 包体积很大，官方文档明确说 `mp4-export` 一个扩展
就接近 0.5 MB 压缩后。项目里不需要动画导出，两条路控制体积：

**首选：用 esbuild 打一个只含所需扩展的 bundle。**

```
esbuild viz/src/viewer.ts --bundle --minify \
    --outfile=viz/build/viewer/molstar-lib.js --global-name=vsViewer
```

这样页面里的用法与上一轮的 3Dmol.js 一致——一个 `<script>` 标签，
一个全局变量，不需要页面本身有构建步骤。这是关键：
**前端页面仍然是「静态文件 + 一个打包好的库」这种形态，不引入 npm/React 开发链。**

理由是这个项目的前端是要嵌进 Agent 产出的运行目录（`runs/<run_id>/`）里的，
一旦引入构建步骤，每次生成运行结果都要跑一遍打包。把 Mol\* 预先打包成一个
静态库文件放进 `vendor/`，之后每个运行目录直接拷贝即可。

**体积已实测**（2026-09-30，molstar 5.12.0 + esbuild 0.28.2）：

| 读数 | 值 |
|---|---|
| 磁盘大小 | 3.0 MB |
| gzip 传输大小 | 0.83 MB |

比预期小，原因是这个封装没有引入 Mol\* 自带的 React UI，也关掉了
`VolumeStreaming` 扩展。**不需要维护 fork 去删 `mp4-export`**，
首选方案就是最终方案。

`VolumeStreaming` 是给电子密度图用的，本项目用不到，而且它在大结构上
会常驻一个后台任务。关掉它是明确写了 `PluginConfig` 的：

```ts
const spec: PluginSpec = {
  ...DefaultPluginSpec(),
  config: [[PluginConfig.VolumeStreaming.Enabled, false]],
};
```

## 四、数据形状

上一轮定的约定保持不变（前端只读 `meta.json` + 结构文件），
改动是 `meta.json` 要容纳预测结构的信息。

### 目录约定

```
runs/<run_id>/
├── meta.json                 # 前端唯一入口
├── structure/
│   ├── receptor.cif          # 受体；已知结构或 AF3 预测
│   ├── confidence.json       # AF3 的 <job>_confidences.json（pLDDT / PAE）
│   └── summary.json          # AF3 的 <job>_summary_confidences.json
├── ligands/
│   ├── crystal-sti.pdb       # 参考配体（已知结构时）
│   ├── pose-01.pdb           # 候选位姿
│   └── ...
└── vendor/                   # 预打包的 Mol* 静态库
```

### `meta.json` 的改动

新增 `structure_source` 与 `confidence` 两段，其余字段沿用。

```json
{
  "run_id": "af3-demo",
  "target": { "name": "...", "reference_chain": "A" },
  "structure": {
    "path": "structure/receptor.cif",
    "format": "mmcif",
    "source": "alphafold3",
    "job_name": "hello_fold",
    "plddt_scale": [0, 100]
  },
  "confidence": {
    "per_residue": "structure/confidence.json",
    "summary": "structure/summary.json",
    "metrics": { "ptm": 0.87, "iptm": 0.79, "ranking_score": 0.82 }
  },
  "pockets": [ ... ],
  "molecules": [ ... ]
}
```

`source` 取 `"alphafold3"` 或 `"experimental"`。前端据此决定是否显示置信度面板——
已知结构没有 pLDDT，面板要隐藏而不是画一条平的曲线。这一条是要害：
**把「有没有置信度」写成数据里的一个字段，而不是靠前端猜。**

### 口袋残基的写法

沿用现有的 `{chain, resseq, resname, min_distance}` 列表。
前端转成 MolQL `SelectionQuery` 时不再有上一轮那个逗号问题——
Mol\* 的 `StructureSelectionQuery` 接受可组合的表达式：

```js
const groups = residues.map((item) => MS.struct.generator.atomGroups({
  'chain-test': MS.core.rel.eq([MS.struct.atomProperty.macromolecular.auth_asym_id(), item.chain]),
  'residue-test': MS.core.rel.eq([MS.struct.atomProperty.macromolecular.auth_seq_id(), item.resseq]),
}));
const query = StructureSelectionQuery('pocket', MS.struct.combinator.merge(groups));
```

每个残基一个分支再 merge，不怕编号离散，也不用归并区间。

## 五、模块划分

```
viz/
├── src/
│   ├── viewer.ts          # 打包入口，导出 initViewer / loadStructure / 选区操作
│   ├── selection.ts       # 残基列表 → SelectionQuery
│   ├── style.ts           # 表示方式与配色（口袋、配体、接触残基）
│   └── confidence.ts      # pLDDT 着色与 PAE 面板
├── build/viewer/molstar-lib.js   # 打包产物，拷进各 run 的 vendor/
└── package.json
```

页面侧（`runs/<run_id>/` 里）保持上一轮的结构不变：

```
index.html    # 顶栏 + 视口 + 右侧面板
app.css
app.js        # 只管面板逻辑，三维操作全部调 vendor 里的 vsViewer
```

**边界这样切**：`viz/` 是唯一依赖 npm 的地方，产出的是一个静态 JS；
`app.js` 不 import 任何 npm 包，只调 `window.vsViewer.*`。
将来换三维库只改 `viz/`，页面与面板不动。

## 六、三个视图状态

沿用上一轮定下的三态，但实现方式变了。

**口袋浏览。** 受体整链卡通灰底，选中口袋的残基换一种表示（棍状）加半透明表面。
Mol\* 里是给同一结构加两个 component，各带自己的表示与选区。

**候选分子叠合。** 每个位姿一个 model，配色区分。
Mol\* 的状态树可以按 model 分别设表示，不需要像 3Dmol.js 那样全部预加载再切显隐。

**单分子详情。** 接触残基高亮 + 接触表。
相机对整条参考链取景——上一轮在这里踩过「按局部选区取缩放范围会把相机放进蛋白内部」，
用 `plugin.managers.camera.focusLoci(loci)` 时传整个结构而不传选区。

新增**第四态：置信度**。pLDDT 按残基着色 + PAE 热图。
只在 `structure.source === "alphafold3"` 时显示这个 tab。

## 七、上一轮踩过的坑，这一版要避开

1. **选区被静默截断。** 上一轮 `resi` 逗号列表只匹配到一段，高亮少了残基却不报错。
   这一版用 `MS.struct.combinator.merge`，但判据仍要**逐个数覆盖的残基数**，
   不能只判选区对象非空。

2. **相机被放进结构内部。** 判据是「把画面切成 8x8 网格数含结构的格子」，
   最外圈含结构的格子超过一半就说明结构铺满画面、相机贴在切面上。
   这个读数在 Mol\* 上同样适用，照搬。

3. **截图读像素。** Mol\* 用 WebGL2，同一个画布同样不能二次取上下文。
   判据继续走「截图 + pngjs 统计」。

4. **静态服务。** 页面要用 HTTP 打开，`file://` 下 `fetch` 读 `meta.json` 会被拒。

## 八、实施顺序

分四步，每步做完都能单独验证。

1. **打包环境。** 装 `molstar` 与 `esbuild`，写 `viz/src/viewer.ts`，
   打出 `molstar-lib.js`，用一个最小页面确认结构能显示。这一步验证体积与加载时间。
2. **数据形状。** 改 `tools/build_demo_data.py` 输出新目录结构，
   加一份真的 AF3 输出（跑一次或用官方示例）验证 `confidence.json` 的解析。
3. **三个视图状态。** 把上一轮 `app.js` 的面板逻辑接过来，三维部分换成 Mol\*。
4. **第四态与判据。** pLDDT 着色、PAE 面板，以及新判据。

## 九、待定项

- **AF3 怎么跑在服务器上**：官方实现要 GPU 与权重，参数规模决定的。
  如果走 AlphaFold Server（网页版），拿不到本地文件，数据来源要另说。
  这一条影响第二节的结构文件从哪来。
- **PDBQT 要不要支持**：对接工具（Vina）输出 PDBQT，Mol\* 不原生认这个格式，
  要么在服务器转成 PDB，要么自己写一个 loader。倾向前者。
- **多模型位姿的显示数**：上一轮是 10 个配体一次加载。AF3 会给多个 sample，
  数量可能更多，要不要分页或只显示前 N 个。
