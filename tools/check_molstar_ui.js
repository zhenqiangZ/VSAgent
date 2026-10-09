// 界面判据：在真实浏览器里打开 Mol* 版页面，检查三维视图与面板的行为。
//
// 判据挑的是那些「界面看着正常但其实错了」的地方：
//   - 结构载入了、原子数也对，但表示没挂上，画布是空的
//   - 口袋点了、列表高亮了，但选区只覆盖到一部分残基
//   - 相机取景把结构放进画面外，或者贴进结构内部
//   - 配体显示了、勾选框也对了，但画面上根本看不见分子
//   - 连点几次之后表示越积越多，每一帧都「正常」但整体越来越卡
//
// 每条判据都记「测什么 + 什么条件下会失败」，失败时打出来的东西要能直接定位。

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.env.DEMO_URL || "http://127.0.0.1:8851";
const OUT_DIR = path.resolve(__dirname, "..", "tests", "output");

// 视图状态的期望值。这几个数对不上就说明有一条渲染路径没生效。
//
// 口袋态 8 = 2 卡通（底色 + B 链覆盖）+ 2 选区子树 + 2 棍状 + 2 表面。
// 关掉 B 链后 6 = 少一个卡通覆盖与一个表面的份额。
// 候选分子态 5 = 2 卡通 + 1 选中的配体 + 2 选区子树（配体表示走整结构，不建子树）。
const EXPECT = {
  pockets: 8,
  pocketsNoChainB: 6,
  moleculesOne: 5,
};

const results = [];

function check(name, condition, detail) {
  results.push({ name, pass: !!condition, detail });
  const flag = condition ? "通过" : "失败";
  console.log(`[${flag}] ${name}${detail ? ` —— ${detail}` : ""}`);
}

async function readState(page) {
  return page.evaluate(() => {
    const snap = window.__snapshot();
    const viewer = window.vsViewer.getSnapshot();
    return { snap, viewer };
  });
}

// 等相机停止移动。
//
// 取景是一次带动画的过渡（focusLoci 的 durationMs 默认 250ms，但页面
// 连续调用几次、还夹着重建表示，实际落定要更久）。用固定 sleep 猜时长
// 会读到过渡中途的画面——实测同一条判据在 6 / 82 / 138 / 167 / 686 之间跳，
// 而 686 才是落定值。像素数对相机距离很敏感（大致 1/距离²），
// 差一点点就是几倍的读数差。
//
// 这里轮询相机矩阵，连续两次一致就认为停了。比 sleep 稳，也不会白等。
async function waitForCameraSettled(page, timeoutMs = 20000) {
  await page.evaluate(async (timeout) => {
    const plugin = window.vsViewer.getPlugin();
    const read = () => {
      const s = plugin.canvas3d.camera.getSnapshot();
      return [
        ...s.position.map((n) => n.toFixed(3)),
        ...s.target.map((n) => n.toFixed(3)),
      ].join(",");
    };
    const started = Date.now();
    let last = read();
    let stable = 0;
    while (Date.now() - started < timeout) {
      await new Promise((r) => requestAnimationFrame(r));
      const now = read();
      if (now === last) {
        stable++;
        if (stable >= 6) return;
      } else {
        stable = 0;
        last = now;
      }
    }
  }, timeoutMs);
}

// 画布内容占比与 8x8 网格里含结构的格子数。
//
// 不直接读 WebGL 像素缓冲：同一个画布不能二次取出上下文，Mol* 已经持有它。
// 截图读的是合成之后的结果，与用户看到的是同一份。
async function readCanvasCoverage(page) {
  const el = await page.$("#viewer");
  const buffer = await el.screenshot({ timeout: 120000 });
  const { PNG } = require("pngjs");
  const png = PNG.sync.read(buffer);
  const { width, height, data } = png;

  // 背景是 #f2f4f7，判定时给一点容差。
  const bg = [0xf2, 0xf4, 0xf7];
  const isStructure = (x, y) => {
    const i = (y * width + x) * 4;
    return Math.abs(data[i] - bg[0]) > 12
      || Math.abs(data[i + 1] - bg[1]) > 12
      || Math.abs(data[i + 2] - bg[2]) > 12;
  };

  let painted = 0;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (isStructure(x, y)) painted++;
    }
  }

  // 8x8 网格：数每个格子里有多少像素是结构。
  // 相机贴在裁剪面上时结构铺满画面，连最外圈都填满；
  // 正常对准整条链时结构只占中间一片。
  const gridSize = 8;
  const cells = [];
  for (let gy = 0; gy < gridSize; gy++) {
    for (let gx = 0; gx < gridSize; gx++) {
      const x0 = Math.floor((gx * width) / gridSize);
      const x1 = Math.floor(((gx + 1) * width) / gridSize);
      const y0 = Math.floor((gy * height) / gridSize);
      const y1 = Math.floor(((gy + 1) * height) / gridSize);
      let hit = 0;
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          if (isStructure(x, y)) hit++;
        }
      }
      const area = Math.max(1, (x1 - x0) * (y1 - y0));
      cells.push(hit / area > 0.02);
    }
  }
  // 最外圈：网格的第一行、最后一行、第一列、最后一列
  const outer = [];
  for (let g = 0; g < gridSize; g++) {
    outer.push(cells[g]);                                // 顶行
    outer.push(cells[(gridSize - 1) * gridSize + g]);    // 底行
    outer.push(cells[g * gridSize]);                     // 左列
    outer.push(cells[g * gridSize + gridSize - 1]);      // 右列
  }
  return {
    width,
    height,
    paintedRatio: painted / (width * height),
    outerFilled: outer.filter(Boolean).length,
    outerTotal: outer.length,
    cells,
  };
}

// 画面上有多少像素接近给定的颜色。用来判「配色有没有真的落到渲染上」。
async function countColorPixels(page, hexColors, tolerance = 40) {
  const el = await page.$("#viewer");
  const buffer = await el.screenshot({ timeout: 120000 });
  const { PNG } = require("pngjs");
  const png = PNG.sync.read(buffer);
  const targets = hexColors.map((hex) => {
    const text = hex.replace("#", "");
    return [
      parseInt(text.slice(0, 2), 16),
      parseInt(text.slice(2, 4), 16),
      parseInt(text.slice(4, 6), 16),
    ];
  });
  const counts = targets.map(() => 0);
  for (let i = 0; i < png.data.length; i += 4) {
    for (let t = 0; t < targets.length; t++) {
      const [r, g, b] = targets[t];
      if (Math.abs(png.data[i] - r) <= tolerance
        && Math.abs(png.data[i + 1] - g) <= tolerance
        && Math.abs(png.data[i + 2] - b) <= tolerance) {
        counts[t]++;
      }
    }
  }
  return counts;
}

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({
    args: [
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
    ],
  });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") pageErrors.push(`console: ${message.text()}`);
  });

  await page.goto(`${BASE}/index.html`, { waitUntil: "load" });

  // 载入 11 个结构（1 受体 + 10 配体），给足时间
  try {
    await page.waitForFunction(() => window.__ready === true, { timeout: 240000 });
  } catch (error) {
    const fatal = await page.textContent("#fatal").catch(() => "");
    check("页面完成载入", false, `致命错误框：${JSON.stringify(fatal)}`);
    console.log("页面错误：", pageErrors.slice(0, 10).join("\n"));
    await browser.close();
    process.exit(1);
  }
  check("页面完成载入", true);

  // ---- 结构真的解析出来了 ----
  const initial = await readState(page);
  check(
    "受体原子数正确",
    initial.viewer.elementCount === 4458,
    `读到 ${initial.viewer.elementCount}，期望 4458`
  );
  check(
    "结构个数正确",
    initial.viewer.structureCount === 11,
    `读到 ${initial.viewer.structureCount}，期望 11（1 受体 + 10 配体）`
  );

  // ---- 表示挂上了，不是「结构在但画布空」 ----
  const reprCount = await page.evaluate(() => window.vsViewer.countRepresentations());
  check(
    "受体挂上了表示",
    reprCount >= EXPECT.pockets,
    `读到 ${reprCount} 个表示，期望至少 ${EXPECT.pockets}`
  );

  // ---- 口袋态的画面内容 ----
  const pocketCoverage = await readCanvasCoverage(page);
  check(
    "口袋态画布上有结构",
    pocketCoverage.paintedRatio > 0.05,
    `非背景像素占比 ${(pocketCoverage.paintedRatio * 100).toFixed(1)}%`
  );
  check(
    "口袋态相机没有贴进结构内部",
    pocketCoverage.outerFilled <= pocketCoverage.outerTotal / 2,
    `最外圈含结构的格子 ${pocketCoverage.outerFilled}/${pocketCoverage.outerTotal}`
  );

  // ---- 配色真的是页面指定的，不是 Mol* 的默认主题 ----
  // 默认主题会按链分配橙色与绿色。页面指定的灰底不会出现这两种色。
  const defaultThemePixels = await countColorPixels(page, ["#e07030", "#00b090"]);
  check(
    "受体用的是页面指定的灰底",
    defaultThemePixels[0] < 200 && defaultThemePixels[1] < 200,
    `默认主题配色像素 橙 ${defaultThemePixels[0]} / 绿 ${defaultThemePixels[1]}`
  );
  const pocketBlue = await countColorPixels(page, ["#1a6dd4", "#4a9eff"]);
  check(
    "口袋高亮颜色出现",
    pocketBlue[0] + pocketBlue[1] > 500,
    `口袋配色像素 ${pocketBlue[0]} + ${pocketBlue[1]}`
  );

  // ---- 选区覆盖的残基要逐个数 ----
  // 上一轮 3Dmol.js 的 resi 逗号列表被静默截断，选区看着有值、实际少一部分。
  // 这一条按「选区覆盖的原子数」核对，不判选区对象非空。
  const pocketNumbers = await page.evaluate(() => {
    const meta = window.__snapshot();
    return meta.pocketResidueCount;
  });
  check(
    "P1 有 28 个残基",
    pocketNumbers === 28,
    `读到 ${pocketNumbers}`
  );

  const p3Atoms = await page.evaluate(async () => {
    return window.vsViewer.countResiduesInSelection([
      { chain: "A", resseq: 481 }, { chain: "A", resseq: 480 },
      { chain: "A", resseq: 482 }, { chain: "A", resseq: 479 },
      { chain: "A", resseq: 483 }, { chain: "A", resseq: 419 },
    ]);
  });
  check(
    "口袋选区覆盖 49 个原子",
    p3Atoms === 49,
    `读到 ${p3Atoms}，期望 49（与 3Dmol.js 版修好逗号坑后的读数一致）`
  );

  // ---- 反向核对：选区不会静默吞掉不存在的残基 ----
  const missing = await page.evaluate(() =>
    window.vsViewer.countResiduesInSelection([{ chain: "A", resseq: 9999 }])
  );
  check(
    "不存在的残基选区是空的",
    missing === 0,
    `读到 ${missing}，期望 0`
  );

  // ---- 切换口袋 ----
  await page.click("#pocket-list li[data-pocket-id='P3']");
  await page.waitForTimeout(2500);
  const p3State = await readState(page);
  check(
    "切换到 P3 生效",
    p3State.snap.pocketId === "P3" && p3State.snap.pocketResidueCount === 6,
    `口袋 ${p3State.snap.pocketId}，残基 ${p3State.snap.pocketResidueCount} 个`
  );
  check(
    "切换口袋后表示数不累积",
    p3State.viewer.representationTotal === EXPECT.pockets,
    `读到 ${p3State.viewer.representationTotal}，期望 ${EXPECT.pockets}`
  );

  // ---- 关掉 B 链 ----
  await page.click("#chain-toggle");
  await page.waitForTimeout(2000);
  const noB = await readState(page);
  check(
    "关掉 B 链后状态正确",
    noB.snap.chainBVisible === false,
    `chainBVisible=${noB.snap.chainBVisible}`
  );
  check(
    "关掉 B 链后表示数下降",
    noB.viewer.representationTotal === EXPECT.pocketsNoChainB,
    `读到 ${noB.viewer.representationTotal}，期望 ${EXPECT.pocketsNoChainB}`
  );
  await page.click("#chain-toggle");
  await page.waitForTimeout(1500);
  const withB = await readState(page);
  check(
    "打开 B 链后恢复",
    withB.snap.chainBVisible === true
      && withB.viewer.representationTotal === EXPECT.pockets,
    `chainBVisible=${withB.snap.chainBVisible}，表示 ${withB.viewer.representationTotal}`
  );

  // ---- 候选分子态 ----
  await page.click(".tab[data-mode='molecules']");
  await page.waitForTimeout(2500);
  const molOne = await readState(page);
  check(
    "候选分子态默认显示一个",
    molOne.snap.ligandStructuresShown === 1 && molOne.snap.pocketId === null,
    `显示 ${molOne.snap.ligandStructuresShown} 个，口袋 ${molOne.snap.pocketId}`
  );
  check(
    "候选分子态表示数正确",
    molOne.viewer.representationTotal === EXPECT.moleculesOne,
    `读到 ${molOne.viewer.representationTotal}，期望 ${EXPECT.moleculesOne}`
  );

  // 配体要在画面上真的看得见。
  // 只判「结构加载了、表示加上了」不够——对准整条链取景时，
  // 几十个原子的配体只有十几个像素，读数全对而用户看不到东西。
  //
  // 判据要能把「取景拉得太远」和「取景合适」分开。实测单个配体在不同
  // 取景余量下的像素数：
  //
  //   extraRadius=2 → 852     extraRadius=3 → 686     extraRadius=6 → 138
  //
  // 曾想过只判「配体态比整链视图清楚」。实测这条不够：取景拉到 6 时
  // 配体是 138 像素、整链基准也只剩 6，比值 23 倍照样过。整链基准会
  // 跟着一起变小，比值失灵。
  //
  // 所以判据落在像素数本身，下限取 400：这一档在实测里对应「配体能看清
  // 每根键、周围口袋壁也留得住」，而取景拉到 6 时的 138 会被挡住。
  //
  // 量之前必须等相机停住，否则读到的是过渡中途的画面，读数在
  // 6 到 712 之间跳。见 waitForCameraSettled。
  await waitForCameraSettled(page);
  const ligandColors = ["#e0503a", "#2f8f4e", "#8a4fd8"];
  const ligandPixels = await countColorPixels(page, ligandColors);
  check(
    "配体在画面上的像素足够",
    ligandPixels[0] > 400,
    `L01 配色像素 ${ligandPixels[0]}，期望 >400（低于这一档说明取景拉得太远）`
  );

  // ---- 只看前三 ----
  await page.click("#select-top3");
  await page.waitForTimeout(3000);
  const top3 = await readState(page);
  check(
    "只看前三生效",
    top3.snap.ligandStructuresShown === 3,
    `显示 ${top3.snap.ligandStructuresShown} 个`
  );
  const top3Pixels = await countColorPixels(page, ["#e0503a", "#2f8f4e", "#8a4fd8"]);
  check(
    "三个配体的配色都出现",
    top3Pixels.every((n) => n > 30),
    `红 ${top3Pixels[0]} / 绿 ${top3Pixels[1]} / 紫 ${top3Pixels[2]}，各期望 >30`
  );

  // ---- 全选与全不选 ----
  await page.click("#select-all");
  await page.waitForTimeout(3500);
  const all = await readState(page);
  check(
    "全选显示十个",
    all.snap.ligandStructuresShown === 10,
    `显示 ${all.snap.ligandStructuresShown} 个`
  );

  await page.click("#select-none");
  await page.waitForTimeout(2500);
  const none = await readState(page);
  check(
    "全不选清空",
    none.snap.ligandStructuresShown === 0,
    `显示 ${none.snap.ligandStructuresShown} 个`
  );

  // ---- 详情态 ----
  await page.click("#select-top3");
  await page.waitForTimeout(2500);
  await page.click("#molecule-table tbody tr:first-child");
  await page.waitForTimeout(3000);
  const detail = await readState(page);
  check(
    "详情态只显示一个分子",
    detail.snap.ligandStructuresShown === 1 && detail.snap.visibleMolecules.length === 1,
    `显示 ${detail.snap.ligandStructuresShown} 个`
  );
  const contactRows = await page.$$eval("#contact-table tbody tr", (rows) => rows.length);
  check(
    "接触表有内容",
    contactRows === 28,
    `读到 ${contactRows} 行，期望 28`
  );
  const detailPixels = await countColorPixels(page, ["#e08a3c"]);
  check(
    "接触残基配色出现",
    detailPixels[0] > 200,
    `接触残基配色像素 ${detailPixels[0]}`
  );

  // ---- 反复切换不累积 ----
  // 连点几个口袋之后表示数只增不减，每一帧看着都正常、整体越来越卡。
  // 这一条专门盯这种泄漏。
  await page.click(".tab[data-mode='pockets']");
  await page.waitForTimeout(2000);
  let maxRepr = 0;
  for (const id of ["P2", "P3", "P4", "P1", "P2", "P1"]) {
    await page.click(`#pocket-list li[data-pocket-id='${id}']`);
    await page.waitForTimeout(1800);
    const state = await readState(page);
    maxRepr = Math.max(maxRepr, state.viewer.representationTotal);
  }
  check(
    "反复切换口袋不累积表示",
    maxRepr <= EXPECT.pockets,
    `六次切换中最大表示数 ${maxRepr}，期望不超过 ${EXPECT.pockets}`
  );

  // ---- 全程没有页面错误 ----
  check(
    "全程没有页面错误",
    pageErrors.length === 0,
    pageErrors.slice(0, 5).join(" | ") || "（无）"
  );

  await browser.close();

  const failed = results.filter((item) => !item.pass);
  console.log("");
  console.log(`共 ${results.length} 条，通过 ${results.length - failed.length}，失败 ${failed.length}`);
  if (failed.length) {
    console.log("失败清单：");
    for (const item of failed) console.log(`  - ${item.name}（${item.detail}）`);
    process.exit(1);
  }
})().catch((error) => {
  console.error("判据执行失败：", error);
  process.exit(1);
});
