// Mol* 的封装层，打包成一个静态库供 runs/<run_id>/ 的页面引用。
//
// 这一层是唯一依赖 npm 的地方。页面侧（app.js）不 import 任何 npm 包，
// 只调 window.vsViewer 上的方法，将来换三维库只改这个目录。
//
// 设计取舍：不引入 Mol* 自带的 React UI。右侧面板沿用项目已有的交互习惯，
// 用 Mol* 的 React UI 反而要跟它较劲布局。这里只用 PluginContext 管三维视口。

import { DefaultPluginSpec, PluginSpec } from "molstar/lib/mol-plugin/spec";
import { PluginContext } from "molstar/lib/mol-plugin/context";
import { PluginConfig } from "molstar/lib/mol-plugin/config";
import { MolScriptBuilder as MS } from "molstar/lib/mol-script/language/builder";
import { StructureSelectionQuery } from "molstar/lib/mol-plugin-state/helpers/structure-selection-query";
import { StructureElement, StructureProperties } from "molstar/lib/mol-model/structure";
const Loci = StructureElement.Loci;
import { RuntimeContext } from "molstar/lib/mol-task";
import { StateTransforms } from "molstar/lib/mol-plugin-state/transforms";

// StructureSelectionQuery.getSelection 的第二个参数是 RuntimeContext，
// 只在查询自带 ensureCustomProperties 时才真正用到（本项目的表达式不带），
// 所以传同步上下文即可。
//
// 早先写过 `current.state.data.frame([])` 当这个参数，运行时报
// `state.data.frame is not a function`——State 上没有这个方法，
// 它是从别处模仿来的写法。
const SYNC_RUNTIME = RuntimeContext.Synchronous;

export interface InitOptions {
  // 视口背景色，与页面主题一致
  background?: string;
}

export interface StructureInput {
  url: string;
  format?: "mmcif" | "pdb";
  isBinary?: boolean;
}

// 残基描述，与 meta.json 里 pockets[].residues 的形状一致
export interface ResidueRef {
  chain: string;
  resseq: number | string;
  resname?: string;
}

let plugin: PluginContext | null = null;

// 建实例。返回的 promise 在视口真正可用之后才 resolve。
export async function initViewer(
  target: string | HTMLElement,
  options: InitOptions = {}
): Promise<void> {
  const parent =
    typeof target === "string"
      ? (document.getElementById(target) as HTMLElement)
      : target;
  if (!parent) throw new Error("找不到视口容器");

  // 关掉不需要的扩展。VolumeStreaming 是给电子密度图用的，本项目用不到，
  // 大结构上它会常驻一个后台任务。
  const spec: PluginSpec = {
    ...DefaultPluginSpec(),
    config: [[PluginConfig.VolumeStreaming.Enabled, false]],
  };

  plugin = new PluginContext(spec);
  await plugin.init();

  // 画布与父容器都要给，Mol* 内部按父容器算尺寸。
  const canvas = document.createElement("canvas");
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  parent.appendChild(canvas);

  const ready = await plugin.initViewerAsync(canvas, parent);
  if (!ready) throw new Error("Mol* 视口初始化失败");

  if (options.background) {
    plugin.canvas3d?.setProps({
      renderer: { backgroundColor: colorToHex(options.background) },
    });
  }
}

function colorToHex(color: string): number {
  const text = color.startsWith("#") ? color.slice(1) : color;
  return parseInt(text, 16) || 0xffffff;
}

// 表示参数里的颜色。
//
// addRepresentation 收的是**扁平入参**，形状是
// `{ type, typeParams, color, colorParams, size, sizeParams }`。
// 其中 `color` 是**颜色主题的名字**，不是颜色值；颜色值放在 `colorParams` 里。
//
// 踩过两次，都是静默失败——不报错、表示照常加上，颜色落到默认主题：
//   一、把 CSS 色值当 `color` 传（`color: "#b8bec7"`）。
//       createStructureRepresentationParams 见到字符串就把它当主题名去 registry 查，
//       查不到就回退到类型的默认主题（卡通是 chain-id），于是画面上是按链配色。
//   二、以为颜色走 `colorTheme` 字段。那是状态树里存的形状，
//       不是入参的形状；传进去会被丢掉。
//
// 正确写法是 `color: "uniform", colorParams: { value: 0xb8bec7 }`，
// 并且 **Color 类型是数字**，不是 CSS 字符串。
function normalizeColorParams(
  params: Record<string, unknown>
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...params };
  const raw = out.color;
  delete out.color;
  delete out.colorTheme;
  if (typeof raw === "string" || typeof raw === "number") {
    const value = typeof raw === "string" ? colorToHex(raw) : raw;
    out.color = "uniform";
    out.colorParams = { ...(out.colorParams as object), value };
  }
  return out;
}

function requirePlugin(): PluginContext {
  if (!plugin) throw new Error("视口还没有初始化，先调 initViewer");
  return plugin;
}

// 载入结构。**不加任何表示**，表示全部由页面决定。
//
// 这里刻意不用默认的表示方式。默认 preset 是给交互式查看器用的：
// 它按内容把结构拆成多个 component（聚合物一个、配体一个），各自挂
// 自己的表示与配色。页面要的是「受体一律灰底、颜色由页面决定」，
// 跟这个拆分直接冲突。
//
// 实测踩过：载入 1IEP 之后结构节点数是 3，表示挂在最后那个上；
// 页面按下标 0 去设表示时，对象是那个 Polymer 节点，加完读回仍是 0 个表示，
// 画布上只剩坐标轴——而原子数、相机位置全都正常，看不出错在哪。
//
// 控制表示的是 `representationPreset: "empty"`，它是 preset 的一个**参数值**，
// 不是 preset 名。PresetTrajectoryHierarchy 里只有 default / all-models /
// unitcell / supercell / crystalContacts 五个预设，写成 `applyPreset(t, "empty")`
// 会走到空处，返回 undefined。
//
// 返回这次载入的结构的引用，后续按它设表示。
export async function loadStructure(input: StructureInput): Promise<string> {
  const current = requirePlugin();
  const data = await current.builders.data.download(
    { url: input.url, isBinary: input.isBinary },
    { state: { isGhost: true } }
  );
  const format = input.format ?? "mmcif";
  const trajectory = await current.builders.structure.parseTrajectory(data, format as any);
  const state = await current.builders.structure.hierarchy.applyPreset(
    trajectory,
    "default",
    { representationPreset: "empty" }
  );
  if (!state || !state.structure) {
    throw new Error(`结构载入失败：${input.url}`);
  }
  // applyPreset 返回的是 StateBuilder.To，引用在 .ref 上；
  // 层级视图里那个结构对象的引用在 .cell.transform.ref 上，是另一处。
  // 两者指向同一节点，但字段路径不同，取错会得到 undefined。
  return state.structure.ref;
}

// 结构状态的引用，后续加表示、改样式都用它。
//
// 传 ref 就直接按 ref 定位；不传时取当前视图里的第一个结构。
//
// 为什么不传下标：状态树里的结构个数会随载入与删除变化，
// 下标会失效（实测踩过：applyPreset("default") 一个文件建出三个结构节点，
// 之后按下标读到的对象与想改的那个对不上，加完表示读回仍是 0 个，
// 而原子数、相机各读数全都正常）。ref 是稳定标识，不受增删影响。
function structureRef(ref?: string): string {
  const current = requirePlugin();
  if (ref) return ref;
  const first = current.managers.structure.hierarchy.current.structures[0];
  if (!first) throw new Error("还没有载入结构");
  return first.cell.transform.ref;
}

// 按 ref 取结构数据。
function structureByRef(ref?: string) {
  const current = requirePlugin();
  const structures = current.managers.structure.hierarchy.current.structures;
  if (ref) {
    const found = structures.find((item) => item.cell.transform.ref === ref);
    return found?.cell.obj?.data;
  }
  return structures[0]?.cell.obj?.data;
}

// 换掉某个结构的表示。传 type 与 params，覆盖原有的全部表示。
export async function setRepresentation(
  type: string,
  params: Record<string, unknown>,
  ref?: string
): Promise<void> {
  const current = requirePlugin();
  const target = structureRef(ref);
  // 先清掉已有的表示，避免叠加。
  await clearRepresentations(target);
  await current.builders.structure.representation.addRepresentation(
    target,
    { type, ...normalizeColorParams(params) } as any
  );
}

// 每个结构的表示节点，键是结构 ref、值是表示节点 ref 的数组。
//
// **表示的父节点不是结构节点。** 状态树里的层级是
// 「结构节点 → 组件节点 → 表示节点」，中间隔着组件那一层。
// 实测踩过：只按直接 parent 匹配，一个都匹配不到，于是
// 「先清再加」实际只做了「加」，表示越积越多——连点几次口袋之后
// 受体上的表示从 4 涨到 26，画面越来越卡，而每一步看着都正常。
//
// 所以这里从每个表示节点出发向上找祖先，命中结构 ref 才算它的表示。
//
// 向上走要带步数上限。状态树的根节点没有 parent，理论上走到 undefined 就停，
// 但实际数据里出现过祖先链绕回去的情况，不设上限会把页面卡死
// （实测：探针跑不出结果直接超时）。树不会超过二十层，给 32 足够。
const MAX_ANCESTOR_HOPS = 32;

function collectRepresentationMap(structures: readonly any[]): Map<string, string[]> {
  const current = requirePlugin();
  const cells = current.state.data.cells;
  const structureRefs = new Set<string>(
    structures.map((item) => item.cell.transform.ref)
  );
  const map = new Map<string, string[]>();
  for (const ref of structureRefs) map.set(ref, []);
  for (const cell of cells.values()) {
    if (structureRefs.has(cell.transform.ref)) continue;
    let parent = cell.transform.parent;
    let hops = 0;
    while (parent && hops < MAX_ANCESTOR_HOPS) {
      if (structureRefs.has(parent)) {
        map.get(parent)!.push(cell.transform.ref);
        break;
      }
      parent = cells.get(parent)?.transform.parent;
      hops++;
    }
  }
  return map;
}

// 收某个结构下全部表示的节点引用，供删除用。
function collectRepresentations(ref?: string): string[] {
  const current = requirePlugin();
  const structures = current.managers.structure.hierarchy.current.structures;
  const targets = ref
    ? structures.filter((item) => item.cell.transform.ref === ref)
    : structures;
  const map = collectRepresentationMap(targets);
  const refs: string[] = [];
  for (const list of map.values()) refs.push(...list);
  return refs;
}

// 给一组残基加表示，用来突出口袋区域。已有的整链表示不受影响，两者叠加。
//
// **选区要做成一棵子树，不能塞进表示的参数里。**
// StateTransform.State 里只有 isGhost / isLocked / isHidden / isCollapsed 四个字段，
// 没有 loci。早先写的 `{ initialState: { loci: selection } }` 被静默忽略——
// 不报错、不警告，表示照常加上，只是作用范围是整个结构。
// 症状是「口袋高亮变成了整链上色」，而代码看着完全合理。
//
// Mol* 的路径是两步：先用 StructureSelectionFromExpression 把选区变成
// 一个独立的结构节点，再给这个结构节点加表示。选区因此是一棵子树，
// 与整结构的表示互不干扰。
async function addSelectionRepresentation(
  label: string,
  expression: unknown,
  type: string,
  params: Record<string, unknown>,
  ref?: string
): Promise<void> {
  const current = requirePlugin();
  const target = structureRef(ref);
  const selection = await current.state.data.build()
    .to(target)
    .apply(
      StateTransforms.Model.StructureSelectionFromExpression,
      { expression: expression as any, label },
      { state: { isGhost: true } }
    )
    .commit();
  const selectionRef = selection?.ref;
  if (!selectionRef) return;
  await current.builders.structure.representation
    .addRepresentation(selectionRef, {
      type,
      ...normalizeColorParams(params),
    } as any)
    .catch(() => undefined);
}

// 给一组残基加棍状表示。
export async function addResidueRepresentation(
  residues: ResidueRef[],
  type: string,
  params: Record<string, unknown>,
  ref?: string
): Promise<void> {
  await addSelectionRepresentation(
    `pocket-${residues.length}`,
    buildResidueQuery("pocket", residues).expression,
    type,
    params,
    ref
  );
}

// 给某个选区的结构上表面。表面是独立的一类表示，参数与棍状不同。
export async function addResidueSurface(
  residues: ResidueRef[],
  params: Record<string, unknown> = {},
  ref?: string
): Promise<void> {
  await addResidueRepresentation(
    residues,
    "molecular-surface",
    { alpha: 0.5, ...params },
    ref
  );
}

// 清掉全部表示，只留一个整结构卡通。切换视图状态时先调它。
export async function resetRepresentations(ref?: string): Promise<void> {
  await setRepresentation("cartoon", {}, ref);
}

// 按链隐藏。Mol* 里没有「隐藏某条链」的开关，
// 做法是给整结构一个表示，再给要隐藏的链单独加一个空的表示覆盖掉——
// 这里改用更直接的办法：整结构只保留目标链的表示。
export async function showOnlyChains(
  chains: string[],
  type: string,
  params: Record<string, unknown>,
  ref?: string
): Promise<void> {
  const current = requirePlugin();
  const structure = structureByRef(ref);
  if (!structure) return;
  // 清掉整结构的表示
  const toDelete = collectRepresentations(ref);
  if (toDelete.length) {
    await current.state.data.build().delete(...toDelete).commit();
  }
  // 只给目标链加表示
  const selection = await buildChainQuery("chains", chains).getSelection(
    current,
    SYNC_RUNTIME,
    structure
  );
  if (selection.kind === "empty-loci") return;
  await current.builders.structure.representation.addRepresentation(
    structureRef(ref),
    { type, ...params } as any,
    { initialState: { loci: selection as any } }
  );
}

// 卡片式读数：某个结构下有多少个表示节点。
// 判据用它分辨「界面上点了按钮但表示没加上」。
//
// 读状态树、不读层级视图，并且按祖先链归属（理由见 collectRepresentationMap）。
// 层级视图的组件列表与状态树不是同一步刷新，用它做读数会误判。
export function countRepresentations(ref?: string): number {
  const current = requirePlugin();
  const structures = current.managers.structure.hierarchy.current.structures;
  const targets = ref
    ? structures.filter((item) => item.cell.transform.ref === ref)
    : structures;
  const map = collectRepresentationMap(targets);
  let count = 0;
  for (const list of map.values()) count += list.length;
  return count;
}

// 用残基列表做选区。每个残基一个分支再 merge，
// 不怕编号离散、也不用归并区间——上一轮在 3Dmol.js 上踩过
// 「resi 逗号列表被静默截断」的坑，这里的 API 是可组合的表达式。
export function buildResidueQuery(label: string, residues: ResidueRef[]) {
  const groups = residues.map((item) =>
    MS.struct.generator.atomGroups({
      "chain-test": MS.core.rel.eq([
        MS.struct.atomProperty.macromolecular.auth_asym_id(),
        item.chain,
      ]),
      "residue-test": MS.core.rel.eq([
        MS.struct.atomProperty.macromolecular.auth_seq_id(),
        Number(item.resseq),
      ]),
    })
  );
  if (groups.length === 0) throw new Error("残基列表是空的");
  return StructureSelectionQuery(label, MS.struct.combinator.merge(groups));
}

// 按链做选区。
export function buildChainQuery(label: string, chains: string[]) {
  const groups = chains.map((chain) =>
    MS.struct.generator.atomGroups({
      "chain-test": MS.core.rel.eq([
        MS.struct.atomProperty.macromolecular.auth_asym_id(),
        chain,
      ]),
    })
  );
  if (groups.length === 0) throw new Error("链列表是空的");
  return StructureSelectionQuery(label, MS.struct.combinator.merge(groups));
}

// 把一组残基选出来（描边高亮）。
export async function selectResidues(
  label: string,
  residues: ResidueRef[]
): Promise<void> {
  const current = requirePlugin();
  current.managers.structure.selection.fromSelectionQuery(
    "set",
    buildResidueQuery(label, residues)
  );
}

// 相机对准一组残基。**只在用户明确要看局部时用。**
// 按局部选区取缩放范围会把相机放进结构内部、近处被裁剪面切开，
// 这是上一轮 3Dmol.js 上的实测结论，Mol* 的 focusLoci 同样按 Loci 的包围盒取景，
// 所以多数场景应该用 focusAll。
//
// 取结构走 structureByRef，与 focusAll / focusChainsOnly 一致。
// 这里原先是 `currentStructure()`，那个函数在本文件里并不存在，
// 一调用就抛 ReferenceError；页面没有用到这个导出，所以一直没露出来。
export async function focusResidues(
  label: string,
  residues: ResidueRef[]
): Promise<void> {
  const current = requirePlugin();
  const structure = structureByRef();
  if (!structure) return;
  const selection = await buildResidueQuery(label, residues).getSelection(
    current,
    SYNC_RUNTIME,
    structure
  );
  if (selection.kind === "empty-loci") return;
  current.managers.camera.focusLoci(selection as any);
}

// 相机对准整个结构。这是「相机别贴进结构内部」的正确做法。
//
// 走 StructureElement.Loci.all(structure) 直接构造整结构的 Loci 交给相机，
// 不经过 selection manager。原因有两条：
// 一是 fromSelectionQuery 是异步解析的，紧接着同步读回选区会拿到空结果；
// 二是焦点本来就该按结构边界算，不需要先「选中」再取景。
// 实测踩过：按选区那条路走，相机停在 (0,0,10.45) 不动，
// 而结构边界球心在 (14,74,38)、半径 65，蛋白整个在画面外，画布上只剩坐标轴。
//
// 要按链取景时用 focusChainsOnly，它会把非目标链排除在边界之外。
export async function focusAll(ref?: string): Promise<void> {
  const current = requirePlugin();
  const structure = structureByRef(ref);
  if (!structure) return;
  current.managers.camera.focusLoci(StructureElement.Loci.all(structure));
}

// 相机对准某个结构。传结构 ref，取的是这个结构自己的边界。
//
// 候选分子态要用它：配体只有几十个原子，对准整条链取景时
// 它在画面上只占十几个像素，看着像没渲染出来（实测截图上的
// 配体配色像素数是 6 / 10 / 2）。
export async function focusStructure(ref: string): Promise<void> {
  const current = requirePlugin();
  const structure = structureByRef(ref);
  if (!structure) return;
  current.managers.camera.focusLoci(StructureElement.Loci.all(structure));
}

// 相机对准一组结构，边界额外放宽一段距离。
//
// 取景半径的算法在 molstar/lib/mol-plugin-state/manager/camera.js:184：
//   radius = Math.max(sphere.radius + extraRadius, minRadius)
// extraRadius 是从「结构边界」向外再放的距离，单位是埃。
//
// 实测单个配体（几十个原子、边界球半径约 4 Å）在不同 extraRadius 下的读数：
//
//   extraRadius   配体像素   相机距场景中心
//       0            6          23.9
//       2          852          27.0
//       3          686          28.8
//       4          508          30.7
//       6          138          34.7
//       8           28          39.1
//      14            6          53.0
//
// 像素数大致按 1/半径² 衰减。extraRadius=0 反而只有 6 个像素：
// 半径收到配体自己那么大时相机贴进结构里，配体落到近裁剪面之外。
// 所以「贴着配体取景」不是把 extraRadius 调小到 0，是给一段刚够把
// 近处卡通让开、又不至于把配体缩成一点的余量。
//
// 这个值随可见配体个数变化，由调用方决定（见 app.js 的 framingMargin）。
export async function focusStructures(
  refs: string[],
  extraRadius = 6
): Promise<void> {
  const current = requirePlugin();
  const loci: any[] = [];
  for (const ref of refs) {
    const structure = structureByRef(ref);
    if (structure) loci.push(StructureElement.Loci.all(structure));
  }
  if (loci.length === 0) return;
  const merged = loci.reduce((acc, item) => (acc ? Loci.union(acc, item) : item), null);
  if (merged) {
    current.managers.camera.focusLoci(merged, { extraRadius });
  }
}

// 相机对准若干条链。用查询解析出目标链的 Loci 再取景，
// 这样「只显示 A 链」时相机会贴着 A 链，不会因为 B 链把视野撑大。
export async function focusChainsOnly(chains: string[], ref?: string): Promise<void> {
  const current = requirePlugin();
  const structure = structureByRef(ref);
  if (!structure) return;
  // getSelection 返回 promise，必须 await——这一条是上面那个
  // 「相机不动」的根因之外的第二处：同步读会拿到空的 Loci。
  const selection = await buildChainQuery("focus-chain", chains).getSelection(
    current,
    SYNC_RUNTIME,
    structure
  );
  const loci = selection.kind === "empty-loci"
    ? StructureElement.Loci.all(structure)
    : (selection as any);
  current.managers.camera.focusLoci(loci);
}

// 复位视角到整个结构。
export function resetCamera(): void {
  const current = requirePlugin();
  const structure = structureByRef();
  if (structure) {
    current.managers.camera.focusLoci(StructureElement.Loci.all(structure));
  } else {
    current.managers.camera.reset();
  }
}

// 判据读的数：结构加载了没有、有几个单元、相机在哪。
// 判据要能分辨「界面上按钮按了但渲染没跟着变」，所以读的必须是渲染真正用的那份值。
//
// **相机位置要等过渡结束再读。** focusLoci 是一段带动画的过渡，
// 调用之后立刻读会拿到动画前的位置（实测读到 (0,0,10.45)，看着像「取景没生效」，
// 其实 1.5 秒后就是正确值 (14.1,74.0,218.4)）。判据里读相机之前要留出这段时间。
export function getSnapshot(ref?: string) {
  const current = requirePlugin();
  const structures = current.managers.structure.hierarchy.current.structures;
  // 读哪个结构：给了 ref 就按 ref 找，否则取第一个。
  // 多结构场景下「第一个」是受体——配体位姿按顺序加在后面，
  // 页面把受体放在最前面载入。
  const first = ref
    ? structures.find((item) => item.cell.transform.ref === ref)
    : structures[0];
  const data = first?.cell.obj?.data;
  const camera = current.canvas3d?.camera;
  // 状态的节点数：结构加载成功但表示方式没挂上时，
  // 原子数照样对、画面上却是空的，这个读数能分辨这种情况。
  const stateCells = current.state.data.cells.size;
  return {
    structureCount: structures.length,
    unitCount: data ? data.units.length : 0,
    elementCount: data ? data.elementCount : 0,
    componentCount: first?.components?.length ?? 0,
    stateCellCount: stateCells,
    // 每个结构各有多少个表示。多结构场景下要逐个看，
    // 只读第一个会漏掉配体没上表示的情况。
    // 走状态树而不是层级视图，理由见 countRepresentations 的注释。
    representations: structures.map((item) =>
      countRepresentations(item.cell.transform.ref)
    ),
    // 全结构的表示总数：页面切状态时有没有把该加的表示都加上，看这个
    representationTotal: countRepresentations(),
    // 选区数量：面板高亮有没有真的落到渲染状态上，看这个
    selectionCount: current.managers.structure.selection.elementCount(),
    cameraTarget: camera ? [camera.target[0], camera.target[1], camera.target[2]] : null,
    cameraPosition: camera
      ? [camera.position[0], camera.position[1], camera.position[2]]
      : null,
  };
}

// 某个链上的原子数。用来核对结构真的解析出来了，而不是「画布上有东西」。
//
// 注意取值方式：`model.atomicHierarchy.chains.auth_asym_id` 是按链索引的表，
// 不是逐原子的列，按原子下标去读会读到越界的值（实测每链只数出 1 个原子，
// 而实际是 2229 个，画面上完全看不出来）。逐原子的链标识要用
// StructureProperties.chain.auth_asym_id，参数取 StructureElement.Location。
//
// 一个 unit 的 elements 就是它的全部原子，取首元素建一个 Location 就能读到
// 这个 unit 属于哪条链，再按 elements 的长度累加。
export function countChainAtoms(chain: string, ref?: string): number {
  const structure = structureByRef(ref);
  if (!structure) return 0;
  let count = 0;
  for (const unit of structure.units) {
    if (unit.elements.length === 0) continue;
    const location = StructureElement.Location.create(structure, unit, unit.elements[0]);
    if (StructureProperties.chain.auth_asym_id(location) === chain) {
      count += unit.elements.length;
    }
  }
  return count;
}

// 选区实际覆盖到多少个原子。判据要逐个数，不能只判选区非空——
// 上一轮就是「选区看着有值、实际只匹配到一小部分」。
// fromSelectionQuery 是异步解析的，所以这里返回一个 promise。
export async function countResiduesInSelection(
  residues: ResidueRef[]
): Promise<number> {
  const current = requirePlugin();
  const query = buildResidueQuery("count", residues);
  current.managers.structure.selection.fromSelectionQuery("set", query);
  // 等一轮微任务让选区解析完成，再读计数。
  await new Promise((resolve) => setTimeout(resolve, 0));
  const count = current.managers.structure.selection.elementCount();
  current.managers.structure.selection.clear();
  return count;
}

export function getPlugin(): PluginContext | null {
  return plugin;
}

// 画布尺寸跟着容器走。容器尺寸变化之后要调它，
// 否则 Mol* 还是按旧尺寸投影，画面被拉伸。
export function handleResize(): void {
  plugin?.handleResize();
}

// 清空状态树里所有结构。切靶点或重新载入时用。
export async function clearAll(): Promise<void> {
  const current = requirePlugin();
  await current.clear();
}

// 给若干条链单独上色，盖在已有的底色之上。
//
// Mol* 表示参数里的 color 只接受单一色值，「按链自动分配颜色」是它自己的
// colorTheme 行为。本项目要的是颜色由页面决定，所以逐条链加表示、每条一个色。
// 底色由 showChains 给，这里只做覆盖，不清旧表示。
export async function colorChains(
  assignment: Record<string, string>,
  type = "cartoon",
  baseParams: Record<string, unknown> = {},
  ref?: string
): Promise<void> {
  for (const [chain, color] of Object.entries(assignment)) {
    await addSelectionRepresentation(
      `chain-${chain}`,
      buildChainQuery(`chain-${chain}`, [chain]).expression,
      type,
      { color, ...baseParams },
      ref
    );
  }
}

// 只显示给定的链。做法是给这些链的选区加表示，整结构的表示不加，
// 非目标链自然就不出现。
export async function showChains(
  chains: string[],
  baseColor: string,
  type = "cartoon",
  baseParams: Record<string, unknown> = {},
  ref?: string
): Promise<void> {
  await clearRepresentations(ref);
  if (chains.length === 0) return;
  await addSelectionRepresentation(
    "shown-chains",
    buildChainQuery("shown-chains", chains).expression,
    type,
    { color: baseColor, ...baseParams },
    ref
  );
}

// 清掉某个结构的全部表示，但保留结构本身。
// 页面用它做「这一帧不显示这个配体」，以及切视图状态前的清场。
//
// **一次 delete 提交删不干净，必须循环删到空为止。**
// 状态树里的表示是一棵有依赖的子树，Mol* 每批只处理能同时安全删掉的那些，
// 剩下的留到下一批。实测读数：结构下有 7 个表示时，一次 delete 只删掉 2 个，
// 连续三次调用才降到 1——所以「一次 delete 就算清完」的写法会让表示越积越多，
// 每轮都「成功」返回、没有任何报错，只是数量从 4 涨到 26。
//
// 循环上限按「每批 2 个」的实测速率留足余量：60 轮的容量远超实际会有的表示数。
// 不用「剩余数不变就退出」做收敛判断——实测那样会提前退出：
// 7 个表示时按 5→3→1 递减，最后一轮剩 1 个看似删不动，其实再删一次就空了。
export async function clearRepresentations(ref?: string): Promise<void> {
  const current = requirePlugin();
  for (let round = 0; round < 60; round++) {
    const toDelete = collectRepresentations(ref);
    if (toDelete.length === 0) return;
    await current.state.data.build().delete(...toDelete).commit();
  }
}

// 结构里有哪些链。页面与判据都要用，不能写死成 ["A","B"]。
export function listChains(ref?: string): string[] {
  const structure = structureByRef(ref);
  if (!structure) return [];
  const seen = new Set<string>();
  for (const unit of structure.units) {
    if (unit.elements.length === 0) continue;
    const location = StructureElement.Location.create(structure, unit, unit.elements[0]);
    seen.add(StructureProperties.chain.auth_asym_id(location));
  }
  return [...seen].sort();
}

// 一条链上全部残基的编号。判据要核对「选区覆盖的残基数」，
// 得先知道这条链总共有多少残基，不能拿原子数代替。
export function countChainResidues(chain: string, ref?: string): number {
  const structure = structureByRef(ref);
  if (!structure) return 0;
  const seen = new Set<string>();
  for (const unit of structure.units) {
    for (let i = 0; i < unit.elements.length; i++) {
      const location = StructureElement.Location.create(structure, unit, unit.elements[i]);
      if (StructureProperties.chain.auth_asym_id(location) !== chain) continue;
      seen.add(`${StructureProperties.residue.auth_seq_id(location)}`);
    }
  }
  return seen.size;
}

export function disposeViewer(): void {
  plugin?.dispose();
  plugin = null;
}
