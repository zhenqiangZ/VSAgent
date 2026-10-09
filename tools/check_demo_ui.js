// 界面判据：在真实浏览器里打开演示页面，检查三维视图与面板的行为。
//
// 判据选取的思路是挑那些"界面看着正常但其实错了"的地方下手：
// 视图加载失败时画布照样在那儿，口袋点不动时列表照样高亮。

const { chromium } = require("playwright");
const path = require("path");

const BASE = process.env.DEMO_URL || "http://127.0.0.1:8848";
const OUT_DIR = path.resolve(__dirname, "..", "tests", "output");

// 受体模型的坐标读数。用显式持有的受体模型，不走 viewer.getModel()，
// 那个方法返回的是最后加入的模型，配体加载之后就指不到受体了。
async function readViewerSummary(page) {
  return page.evaluate(() => {
    const viewer = window.__viewer;
    if (!viewer) return { ready: false };
    const receptor = window.__receptorModel;
    const canvas = document.querySelector("#viewer canvas");
    return {
      ready: true,
      atomCount: receptor.selectedAtoms({}).length,
      chainBAtoms: receptor.selectedAtoms({ chain: "B" }).length,
      canvasWidth: canvas ? canvas.width : 0,
      canvasHeight: canvas ? canvas.height : 0,
    };
  });
}

// 画布里画出来的内容占比。做法是只截图三维视图那一块，再统计非背景色像素。
// 不直接读 WebGL 的像素缓冲：同一个画布不能二次取出上下文，而 3Dmol
// 已经持有它。截图这条路读的是合成之后的结果，与用户看到的是同一份。
async function readPaintedRatio(page) {
  const stats = await readCanvasCoverage(page);
  return { ratio: stats.coveredArea, width: stats.width, height: stats.height };
}

// 把三维视图切成 8x8 的网格，数出"含有结构"的格子。
// 这个读数用来认出"相机贴在裁剪面上"这种情况：那时结构铺满整个画面，
// 连最外圈的格子都填满了；相机正常对准整条链时，结构只占中间一片，
// 四边的格子是空的。只统计非背景像素的总占比认不出这种情况，
// 因为用户自己滚轮放大到 2 倍之后总占比同样是 60% 以上。
async function readCanvasCoverage(page) {
  const GRID = 8;
  const buffer = await page.locator("#viewer").screenshot();
  const { PNG } = require("pngjs");
  const png = PNG.sync.read(buffer);
  const cells = [];
  for (let index = 0; index < GRID * GRID; index++) cells.push({ painted: 0, total: 0 });
  let paintedTotal = 0;
  for (let y = 0; y < png.height; y++) {
    const row = Math.min(GRID - 1, Math.floor((y / png.height) * GRID));
    for (let x = 0; x < png.width; x++) {
      const column = Math.min(GRID - 1, Math.floor((x / png.width) * GRID));
      const index = (png.width * y + x) * 4;
      const r = png.data[index];
      const g = png.data[index + 1];
      const b = png.data[index + 2];
      const cell = cells[row * GRID + column];
      cell.total += 1;
      if (Math.abs(r - 242) > 12 || Math.abs(g - 244) > 12 || Math.abs(b - 247) > 12) {
        cell.painted += 1;
        paintedTotal += 1;
      }
    }
  }
  let filled = 0;
  let filledEdge = 0;
  let totalEdge = 0;
  for (let row = 0; row < GRID; row++) {
    for (let column = 0; column < GRID; column++) {
      const cell = cells[row * GRID + column];
      const isFilled = cell.painted / cell.total > 0.05;
      if (isFilled) filled += 1;
      if (row === 0 || row === GRID - 1 || column === 0 || column === GRID - 1) {
        totalEdge += 1;
        if (isFilled) filledEdge += 1;
      }
    }
  }
  return {
    coveredArea: paintedTotal / (png.width * png.height),
    filledCells: filled,
    totalCells: cells.length,
    filledEdgeCells: filledEdge,
    totalEdgeCells: totalEdge,
    width: png.width,
    height: png.height,
  };
}

async function main() {
  const failures = [];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));

  await page.goto(`${BASE}/index.html`, { waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__viewer !== undefined, { timeout: 20000 });

  // 判据一：结构加载成功，画布有尺寸
  const loaded = await readViewerSummary(page);
  console.log(`结构加载：受体原子 ${loaded.atomCount} 个，画布 ${loaded.canvasWidth}x${loaded.canvasHeight}`);
  if (!loaded.ready) failures.push("三维视图没有初始化");
  if (loaded.atomCount !== 4458) {
    failures.push(`受体原子数 ${loaded.atomCount}，期望 4458`);
  }
  if (loaded.canvasWidth === 0 || loaded.canvasHeight === 0) {
    failures.push("画布尺寸为 0，三维视图没有真正渲染");
  }

  // 判据二：画布上真的画出了东西
  await page.waitForTimeout(1200);
  const painted = await readPaintedRatio(page);
  console.log(`三维视图有效像素占比：${(painted.ratio * 100).toFixed(1)}% `
    + `（${painted.width}x${painted.height}）`);
  if (painted.ratio < 0.02) {
    failures.push(`三维视图里只有 ${(painted.ratio * 100).toFixed(2)}% 的像素不是背景色，`
      + "结构没有画出来");
  }

  // 判据三：配体模型真的加载了完整的原子表。
  // 伊马替尼的重原子是 29 个碳、7 个氮、1 个氧，共 37 个。
  // 配体文件的列位置写错时，解析器只会读出其中几个原子，
  // 画面上依旧是一条棍状结构，看不出少了东西。
  const ligandCounts = await page.evaluate(() => {
    const viewer = window.__viewer;
    const counts = [];
    for (let index = 1; index < viewer.models.length; index++) {
      const model = viewer.models[index];
      if (model) counts.push(model.selectedAtoms({}).length);
    }
    return counts;
  });
  const expectedLigandAtoms = 37;
  const wrong = ligandCounts.filter((count) => count !== expectedLigandAtoms);
  console.log(`配体模型 ${ligandCounts.length} 个，原子数 ${ligandCounts[0]} `
    + `（期望 ${expectedLigandAtoms}）`);
  if (ligandCounts.length !== 10) {
    failures.push(`加载了 ${ligandCounts.length} 个配体模型，期望 10`);
  }
  if (wrong.length) {
    failures.push(`有 ${wrong.length} 个配体模型的原子数不是 ${expectedLigandAtoms}：${wrong}`);
  }

  // 判据四：口袋清单画出全部口袋，默认选中第一个
  await page.waitForFunction(
    () => document.querySelectorAll("#pocket-list li").length === 4,
    { timeout: 10000 }
  );
  const pocketCount = await page.locator("#pocket-list li").count();
  const activePocket = await page.locator("#pocket-list li.is-active .pocket-id").first().textContent();
  console.log(`口袋清单：${pocketCount} 项，默认选中 ${activePocket.trim()}`);
  if (pocketCount !== 4) failures.push(`口袋清单画出 ${pocketCount} 项，期望 4`);
  if (!activePocket.includes("P1")) failures.push("默认没有选中 P1");

  // 判据五：点第二个口袋，高亮跟着换，且三维视图里真的多出了表面
  await page.locator("#pocket-list li").nth(1).click();
  await page.waitForTimeout(400);
  const afterSwitch = await page.locator("#pocket-list li.is-active .pocket-id").first().textContent();
  console.log(`切换后选中：${afterSwitch.trim()}`);
  if (!afterSwitch.includes("P2")) failures.push("点第二项之后 P2 没有变成选中状态");

  const switched = await page.evaluate(() => window.__snapshot());
  console.log(`切换口袋后表面数：${switched.surfaceCount}`);
  if (switched.surfaceCount === 0) failures.push("切换到口袋之后没有生成表面");

  await page.locator("#pocket-list li").nth(0).click();
  await page.waitForTimeout(400);

  // 判据六：切到候选分子模式，表格按打分从高到低
  await page.locator('.tab[data-mode="molecules"]').click();
  await page.waitForTimeout(300);
  const scores = await page.locator("#molecule-table tbody tr td:nth-child(3)").allTextContents();
  const numeric = scores.map((value) => parseFloat(value));
  const sorted = [...numeric].sort((a, b) => b - a);
  console.log(`候选分子表：${numeric.length} 行，首行打分 ${numeric[0]}，末行 ${numeric[numeric.length - 1]}`);
  if (numeric.length !== 10) failures.push(`候选分子表画出 ${numeric.length} 行，期望 10`);
  if (JSON.stringify(numeric) !== JSON.stringify(sorted)) {
    failures.push("候选分子表没有按打分从高到低排列");
  }

  // 判据七：勾选框控制三维视图里显示的分子数量
  await page.locator("#select-top3").click();
  await page.waitForTimeout(500);
  const top3 = await page.evaluate(() => window.__snapshot());
  console.log(`只看前三之后：显示 ${top3.ligandModelsShown} 个配体，`
    + `共加载 ${top3.ligandModelsTotal} 个`);
  if (top3.ligandModelsShown !== 3) {
    failures.push(`只看前三之后显示的配体数 ${top3.ligandModelsShown}，期望 3`);
  }
  if (top3.ligandModelsTotal !== 10) {
    failures.push(`加载的配体模型数 ${top3.ligandModelsTotal}，期望 10`);
  }

  // 判据八：取消全选之后没有配体显示
  await page.locator("#select-none").click();
  await page.waitForTimeout(500);
  const none = await page.evaluate(() => window.__snapshot());
  console.log(`全不选之后：显示 ${none.ligandModelsShown} 个配体`);
  if (none.ligandModelsShown !== 0) {
    failures.push(`全不选之后还显示 ${none.ligandModelsShown} 个配体`);
  }

  // 判据九：点编号进入详情，接触表有内容，返回之后回到列表
  await page.locator("#molecule-table tbody tr").first().click();
  await page.waitForTimeout(400);
  const detailTitle = await page.locator("#detail-title").textContent();
  const contactRows = await page.locator("#contact-table tbody tr").count();
  const detailVisible = await page.locator("#panel-detail").isVisible();
  const detailSnapshot = await page.evaluate(() => window.__snapshot());
  console.log(`详情面板：${detailTitle.trim()}，接触 ${contactRows} 条，可见 ${detailVisible}，`
    + `显示配体 ${detailSnapshot.ligandModelsShown} 个`);
  if (!detailVisible) failures.push("点编号之后详情面板没有出现");
  if (contactRows === 0) failures.push("详情面板里的接触表是空的");
  if (detailSnapshot.ligandModelsShown !== 1) {
    failures.push(`详情视图里显示的配体数 ${detailSnapshot.ligandModelsShown}，期望 1`);
  }

  // 判据十：详情模式下的相机能看到整个参考链，没有被裁剪面切进结构内部。
  // 按局部选区（接触残基）取缩放范围时，相机被放到这十几个残基中间，
  // 近处的卡通被裁剪面切开，画面被切面填满。此时非背景像素的总占比
  // 反而很高（实测 70%，比正常版本的 16% 还多），只测占比认不出来。
  // 能认出来的是结构在画面里的铺开范围：正常版本结构只占中间一片，
  // 最外圈的格子是空的（边缘填充 5/28）；被切面填满时连四边都填满了（27/28）。
  await page.waitForTimeout(800);
  const detailCamera = await page.evaluate(() => {
    const viewer = window.__viewer;
    const receptor = window.__receptorModel;
    const atoms = receptor.selectedAtoms({ chain: "A" });
    let minX = Infinity, maxX = -Infinity;
    let minY = Infinity, maxY = -Infinity;
    let minZ = Infinity, maxZ = -Infinity;
    for (const atom of atoms) {
      minX = Math.min(minX, atom.x); maxX = Math.max(maxX, atom.x);
      minY = Math.min(minY, atom.y); maxY = Math.max(maxY, atom.y);
      minZ = Math.min(minZ, atom.z); maxZ = Math.max(maxZ, atom.z);
    }
    return {
      view: viewer.getView(),
      slab: [viewer.slabNear, viewer.slabFar],
      radius: Math.hypot(maxX - minX, maxY - minY, maxZ - minZ) / 2,
    };
  });
  console.log(`详情相机：位置 ${detailCamera.view.slice(0, 3).map((v) => v.toFixed(1))}，`
    + `距离 ${Math.abs(detailCamera.view[3]).toFixed(1)}，`
    + `裁剪面 ${detailCamera.slab.map((v) => Number(v).toFixed(0))}，`
    + `参考链半径 ${detailCamera.radius.toFixed(1)}`);

  const detailCoverage = await readCanvasCoverage(page);
  console.log(`详情视图：有效像素 ${(detailCoverage.coveredArea * 100).toFixed(1)}%，`
    + `含结构格子 ${detailCoverage.filledCells}/${detailCoverage.totalCells}，`
    + `最外圈含结构格子 ${detailCoverage.filledEdgeCells}/${detailCoverage.totalEdgeCells}`);
  if (detailCoverage.coveredArea < 0.02) failures.push("详情视图里看不到结构");
  if (detailCoverage.filledEdgeCells > detailCoverage.totalEdgeCells * 0.5) {
    failures.push(`详情视图的最外圈有 ${detailCoverage.filledEdgeCells}/`
      + `${detailCoverage.totalEdgeCells} 个格子含结构，结构铺满整个画面，`
      + "相机被放进了口袋内部、近处被裁剪面切开");
  }

  await page.locator("#detail-back").click();
  await page.waitForTimeout(300);
  if (!(await page.locator("#panel-molecules").isVisible())) {
    failures.push("返回之后没有回到候选分子列表");
  }

  // 判据十一：B 链开关真的改变了渲染用的状态
  const beforeToggle = await page.evaluate(() => window.__snapshot());
  await page.locator("#chain-toggle").click();
  await page.waitForTimeout(400);
  const hiddenB = await page.evaluate(() => window.__snapshot());
  await page.locator("#chain-toggle").click();
  await page.waitForTimeout(300);
  const restoredB = await page.evaluate(() => window.__snapshot());
  console.log(`B 链开关：初始 ${beforeToggle.chainBVisible}，关掉后 ${hiddenB.chainBVisible}，`
    + `打开后 ${restoredB.chainBVisible}`);
  if (beforeToggle.chainBVisible !== true) failures.push("初始状态里 B 链应当是显示的");
  if (hiddenB.chainBVisible !== false) failures.push("关掉 B 链开关之后渲染状态没有跟着变");
  if (restoredB.chainBVisible !== true) failures.push("再次打开 B 链开关之后状态没有恢复");

  // 判据十二：口袋模式下高亮的选区覆盖当前口袋的全部残基，且真的生成了表面。
  // 只检查选区字符串非空没有用：resi 写成含逗号的列表时，3Dmol 只认到其中一段，
  // 高亮少了十几个残基，界面上完全看不出来。
  await page.locator('.tab[data-mode="pockets"]').click();
  await page.waitForTimeout(400);
  await page.locator("#pocket-list li").nth(2).click();
  await page.waitForTimeout(500);
  const pocketSnapshot = await page.evaluate(() => window.__snapshot());
  console.log(`P3 选区：chain=${pocketSnapshot.pocketSelector.chain || "(or 组合)"}，`
    + `表达式 ${pocketSnapshot.pocketSelectorLabel}`);
  if (pocketSnapshot.surfaceCount === 0) failures.push("选中口袋之后画面上没有表面");
  if (!pocketSnapshot.pocketSelectorLabel) failures.push("选中口袋之后选区里没有残基编号");

  // 逐个数覆盖到的残基。判据要求选区取到的残基数等于数据里的残基数，
  // 少一个就说明选区表达式被 3Dmol 截断了。
  const covered = await page.evaluate(() => {
    const receptor = window.__receptorModel;
    const selector = window.__snapshot().pocketSelector;
    const atoms = receptor.selectedAtoms(selector);
    return {
      atoms: atoms.length,
      residues: [...new Set(atoms.map((atom) => atom.resi))].length,
    };
  });
  console.log(`P3 选区取到 ${covered.atoms} 个原子，覆盖 ${covered.residues} 个残基 `
    + `（数据里 ${pocketSnapshot.pocketResidueCount} 个）`);
  if (covered.residues !== pocketSnapshot.pocketResidueCount) {
    failures.push(`口袋选区只覆盖 ${covered.residues} 个残基，`
      + `数据里是 ${pocketSnapshot.pocketResidueCount} 个，高亮少了残基`);
  }

  // 逐个口袋都过一遍：只要有一个口袋的选区被截断就要报出来。
  for (let index = 0; index < 4; index++) {
    await page.locator("#pocket-list li").nth(index).click();
    await page.waitForTimeout(400);
    const check = await page.evaluate(() => {
      const receptor = window.__receptorModel;
      const snapshot = window.__snapshot();
      const atoms = receptor.selectedAtoms(snapshot.pocketSelector);
      return {
        label: snapshot.pocketSelectorLabel,
        covered: [...new Set(atoms.map((atom) => atom.resi))].length,
        expected: snapshot.pocketResidueCount,
      };
    });
    if (check.covered !== check.expected) {
      failures.push(`第 ${index + 1} 个口袋的选区 ${check.label} 覆盖 `
        + `${check.covered} 个残基，数据里是 ${check.expected} 个`);
    }
  }
  console.log("四个口袋的选区覆盖全部核对完毕");
  await page.locator("#pocket-list li").nth(2).click();
  await page.waitForTimeout(400);

  const p3Residues = await page.evaluate(() => {
    const row = document.querySelectorAll("#pocket-list li")[2];
    return row.querySelector(".pocket-meta").textContent;
  });
  console.log(`P3 面板文字：${p3Residues}`);

  // 判据十三：整段操作期间没有脚本报错
  if (consoleErrors.length) {
    failures.push(`浏览器控制台有 ${consoleErrors.length} 条报错：${consoleErrors[0]}`);
  }

  const fs = require("fs");
  if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
  await page.locator('.tab[data-mode="pockets"]').click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT_DIR, "demo-pockets.png") });
  await page.locator('.tab[data-mode="molecules"]').click();
  await page.locator("#select-top3").click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT_DIR, "demo-molecules.png") });
  console.log("截图写出到 tests/output/demo-pockets.png 与 demo-molecules.png");

  await browser.close();

  if (failures.length) {
    console.log("\n未通过：");
    for (const item of failures) console.log(`  - ${item}`);
    process.exit(1);
  }
  console.log("\n全部通过");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
