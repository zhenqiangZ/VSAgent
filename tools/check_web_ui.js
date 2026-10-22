// 界面判据：在真实浏览器里打开会话历史页与结构查看页，核对画出来的内容与接口给的一致。
//
// 判据挑的是那些「界面看着正常但内容是错的」的地方：会话列表少了几条时列表照样有内容，
// 工具卡片少了输入或输出时卡片照样显示，口袋选区被截断时高亮照样亮着。
// 所以这里逐条与接口的读数对照，并且按顺序逐个卡片比，不只看总数。
//
// 脚本自己准备运行目录、自己起服务、跑完关掉，不依赖已经在跑的服务。
// 状态文件用的是真实那一份，服务只读它，不会写回。

const { chromium } = require("playwright");
const { execFileSync, spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
// 解释器按平台找：Windows 的 venv 在 `Scripts/python.exe`，Linux/macOS 在 `bin/python`。
// 写死一个平台的结果是另一边一上来就 ENOENT，而报错像是判据脚本坏了。
const PYTHON = process.env.VSA_PYTHON || (process.platform === "win32"
  ? path.join(ROOT, ".venv", "Scripts", "python.exe")
  : path.join(ROOT, ".venv", "bin", "python"));
const FIXTURE = path.join(ROOT, ".probe", "web-ui-runs");
const LOG_PATH = path.join(ROOT, ".probe", "web-ui-serve.log");
const PORT = Number(process.env.WEB_PORT || 8866);
const BASE = `http://127.0.0.1:${PORT}`;

// 与 history.js 里的状态文字一致。卡片上的文字对不上时说明状态映射错了，
// 而徽标的颜色仍然正常，只看截图认不出来。
const STATUS_TEXT = {
  pending: "等待",
  awaiting: "待确认",
  running: "执行中",
  completed: "完成",
  failed: "失败",
};

// 口袋来源徽标的三态与结构页里的文字一致。核对「有没有挂」与「挂的是不是这一个」，
// 光看页面有没有出现某个字认不出「配体来源的写了预测」这一类错配。
const POCKET_SOURCE_TEXT = {
  ligand: "实验配体",
  p2rank: "预测",
  uniprot_site: "标注位点",
};

const failures = [];

function check(label, condition, detail) {
  console.log(`[${condition ? "通过" : "失败"}] ${label} —— ${detail}`);
  if (!condition) failures.push(`${label}（${detail}）`);
}

async function getJson(route) {
  const response = await fetch(`${BASE}${route}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(`${route} 返回 ${response.status}：${payload.error || ""}`);
  return payload;
}

async function waitReady() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      await getJson("/api/runs");
      return;
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }
  throw new Error(`服务没有在预期时间内就绪，日志在 ${LOG_PATH}`);
}

// 准备四个运行目录。
//   demo   —— 复制一份真实运行，用来核对运行选择器与三维视图。
//   linked —— 由真实构建脚本产出，带上产出它的会话，用来核对两个页面之间的跳转。
//   legacy —— 复制一份旧演示数据（`runs/1iep-demo`），口袋项里没有 `source`，
//             `provenance.pockets` 是字符串。用来核对旧形状仍能打开、且不会凭空长出徽标。
//   sourced—— 在 legacy 的基础上给口袋补上 `source`、把 `provenance.pockets` 换成对象。
//             旧数据里没有这个字段（那是第 3 步才定的口径），要核对新形状只能现造一份。
function prepareFixture(sessionId) {
  fs.rmSync(FIXTURE, { recursive: true, force: true });
  fs.mkdirSync(FIXTURE, { recursive: true });

  fs.cpSync(path.join(ROOT, "runs", "1iep-molstar"), path.join(FIXTURE, "demo"), {
    recursive: true,
  });

  const legacy = path.join(FIXTURE, "legacy");
  fs.cpSync(path.join(ROOT, "runs", "1iep-demo"), legacy, { recursive: true });
  makeSourcedRun(legacy, path.join(FIXTURE, "sourced"));

  const linked = path.join(FIXTURE, "linked");
  fs.mkdirSync(linked, { recursive: true });
  fs.copyFileSync(
    path.join(ROOT, "runs", "1iep-demo", "receptor_full.pdb"),
    path.join(linked, "receptor_full.pdb")
  );
  execFileSync(
    PYTHON,
    [
      path.join(ROOT, "tools", "build_demo_data.py"),
      "--run-dir", linked,
      "--session-id", sessionId,
    ],
    { stdio: "ignore" }
  );
}

// 把一份旧运行改写成新一轮工具会产出的形状：
// 前两个口袋标成实验配体圈出来的，其余标成预测来的；来源说明换成对象。
// 目录名即 run_id，写进 meta 里保持一致。
function makeSourcedRun(sourceDir, targetDir) {
  fs.cpSync(sourceDir, targetDir, { recursive: true });
  const metaPath = path.join(targetDir, "meta.json");
  const meta = JSON.parse(fs.readFileSync(metaPath, "utf8"));
  meta.run_id = path.basename(targetDir);
  meta.pockets.forEach((pocket, index) => {
    pocket.source = index < 2 ? "ligand" : "p2rank";
  });
  meta.provenance.pockets = {
    origin: "complex",
    note: "来自复合物结构里的共晶配体",
  };
  fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), "utf8");
}

function startServer() {
  const out = fs.openSync(LOG_PATH, "w");
  return spawn(
    PYTHON,
    [path.join(ROOT, "tools", "serve_web.py"), "--runs", FIXTURE, "--port", String(PORT)],
    { stdio: ["ignore", out, out] }
  );
}

// 挑一个只命中一部分会话的过滤词。全都命中时看不出过滤有没有生效。
function pickNeedle(sessions) {
  const titles = sessions.map((item) => item.title);
  for (let length = 2; length <= 8; length++) {
    for (const title of titles) {
      const needle = title.slice(0, length).trim();
      if (!needle) continue;
      const hits = titles.filter((item) => item.toLowerCase().includes(needle.toLowerCase())).length;
      if (hits > 0 && hits < titles.length) return { needle, expected: hits };
    }
  }
  // 标题全都一样时挑不出部分命中的词，换一个命中不到的词，
  // 期望列表里出现「没有匹配的会话」。
  return { needle: "zzz-没有这个标题-zzz", expected: 0 };
}

// 把三维视图切成 8x8 的网格，数出「含有结构」的格子。
// 相机被放进结构内部时近处的卡通被裁剪面切开，结构铺满整个画面，
// 连最外圈的格子都填满了；相机正常对准整条链时结构只占中间一片。
// 只统计非背景像素的总占比认不出这种情况：用户自己放大之后占比同样很高。
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

async function checkHistoryPage(page, listing, biggestId) {
  await page.goto(`${BASE}/#/history`, { waitUntil: "networkidle" });
  await page.waitForFunction(
    (count) => document.querySelectorAll("#session-list li[data-session-id]").length === count,
    listing.sessions.length,
    { timeout: 30000 }
  );

  const ids = await page
    .locator("#session-list li[data-session-id]")
    .evaluateAll((nodes) => nodes.map((node) => node.dataset.sessionId));
  check(
    "会话列表条数与接口一致",
    ids.length === listing.sessions.length,
    `列表 ${ids.length} 条，接口 ${listing.sessions.length} 条`
  );
  const expectedIds = listing.sessions.map((item) => item.id);
  check(
    "列表顺序与接口一致",
    JSON.stringify(ids) === JSON.stringify(expectedIds),
    `列表前三条 ${ids.slice(0, 3).join("/")}`
  );

  await page.locator(`#session-list li[data-session-id="${biggestId}"]`).click();
  const detail = await getJson(`/api/sessions/${biggestId}`);
  await page.waitForFunction(
    (count) => document.querySelectorAll("#session-body article.msg").length === count,
    detail.messages.length,
    { timeout: 60000 }
  );
  const messageCount = await page.locator("#session-body article.msg").count();
  check(
    "正文里的消息条数与接口一致",
    messageCount === detail.messages.length,
    `${biggestId} 画出 ${messageCount} 条，接口 ${detail.messages.length} 条`
  );

  const parts = detail.messages.flatMap((message) => message.parts);

  // 工具卡片：逐个比工具名、状态文字、输入与输出四项。
  // 只数卡片总数看不出「卡片在但输出丢了」这一类问题。
  const apiTools = parts.filter((part) => part.type === "tool");
  const domTools = await page.locator("#session-body .part-tool").evaluateAll((nodes) =>
    nodes.map((node) => ({
      name: node.querySelector(".tool-name")?.textContent.trim() ?? "",
      status: node.querySelector(".status")?.textContent.trim() ?? "",
      hasInput: Boolean(node.querySelector(":scope > .tool-section > pre")),
      hasOutput: Boolean(node.querySelector("details.tool-output-details")),
    }))
  );
  check(
    "工具卡片条数与接口一致",
    domTools.length === apiTools.length,
    `画出 ${domTools.length} 张，接口 ${apiTools.length} 个工具单元`
  );
  const toolMismatch = [];
  for (let index = 0; index < Math.min(domTools.length, apiTools.length); index++) {
    const want = apiTools[index];
    const got = domTools[index];
    const wantStatus = STATUS_TEXT[want.toolStatus] || want.toolStatus || "等待";
    const wantInput = Boolean(want.toolInput && Object.keys(want.toolInput).length);
    const wantOutput = Boolean(want.toolOutput);
    if (got.name !== (want.toolName || "tool")) {
      toolMismatch.push(`第 ${index + 1} 张的工具名是 ${got.name}，接口是 ${want.toolName}`);
    } else if (got.status !== wantStatus) {
      toolMismatch.push(`第 ${index + 1} 张的状态是 ${got.status}，接口是 ${wantStatus}`);
    } else if (got.hasInput !== wantInput) {
      toolMismatch.push(`第 ${index + 1} 张的输入${got.hasInput ? "有" : "没有"}，接口${wantInput ? "有" : "没有"}`);
    } else if (got.hasOutput !== wantOutput) {
      toolMismatch.push(`第 ${index + 1} 张的输出${got.hasOutput ? "有" : "没有"}，接口${wantOutput ? "有" : "没有"}`);
    }
  }
  check(
    "每张工具卡片的四项都与接口一致",
    toolMismatch.length === 0,
    toolMismatch.length ? `前一处：${toolMismatch[0]}` : `${apiTools.length} 张卡片逐张核对工具名、状态、输入、输出`
  );

  // 补丁卡片：增删行数逐个比。行数写成零时卡片照样显示，只有比数才看得出来。
  const apiPatches = parts.filter((part) => part.type === "patch");
  const domPatches = await page.locator("#session-body .part-patch").evaluateAll((nodes) =>
    nodes.map((node) => ({
      file: node.querySelector(".patch-path")?.textContent.trim() ?? "",
      added: node.querySelector(".patch-added")?.textContent.trim() ?? "",
      removed: node.querySelector(".patch-removed")?.textContent.trim() ?? "",
    }))
  );
  check(
    "补丁卡片条数与接口一致",
    domPatches.length === apiPatches.length,
    `画出 ${domPatches.length} 张，接口 ${apiPatches.length} 个补丁单元`
  );
  const patchMismatch = [];
  for (let index = 0; index < Math.min(domPatches.length, apiPatches.length); index++) {
    const want = apiPatches[index];
    const got = domPatches[index];
    if (got.added !== `+${want.patchAdded}` || got.removed !== `-${want.patchRemoved}`) {
      patchMismatch.push(
        `第 ${index + 1} 张写的是 ${got.added}/${got.removed}，接口是 +${want.patchAdded}/-${want.patchRemoved}`
      );
    } else if (!got.file) {
      patchMismatch.push(`第 ${index + 1} 张没有文件名`);
    }
  }
  check(
    "每张补丁卡片的增删行数与接口一致",
    patchMismatch.length === 0,
    patchMismatch.length ? `前一处：${patchMismatch[0]}` : `${apiPatches.length} 张卡片逐张核对增删行数与文件名`
  );

  // 思考过程：折叠块的数量与接口一致，并且默认是收起的。
  const apiReasoning = parts.filter((part) => part.type === "reasoning").length;
  const reasoningOpen = await page
    .locator("#session-body details.part-reasoning")
    .evaluateAll((nodes) => nodes.map((node) => node.open));
  check(
    "思考过程折叠块条数与接口一致",
    reasoningOpen.length === apiReasoning,
    `画出 ${reasoningOpen.length} 个，接口 ${apiReasoning} 个思考单元`
  );
  check(
    "思考过程默认收起",
    reasoningOpen.length > 0 && reasoningOpen.every((open) => !open),
    `${reasoningOpen.filter((open) => open).length} 个是展开的`
  );

  // 过滤：列表条数要跟着变，并且剩下的都含这个词。
  const { needle, expected } = pickNeedle(listing.sessions);
  await page.fill("#session-search", needle);
  await page.waitForTimeout(300);
  const filtered = await page
    .locator("#session-list li[data-session-id]")
    .evaluateAll((nodes) => nodes.map((node) => node.querySelector(".session-title").textContent));
  check(
    "过滤之后列表条数与接口一致",
    filtered.length === expected,
    `用「${needle}」过滤出 ${filtered.length} 条，接口里含这个词的有 ${expected} 条`
  );
  check(
    "过滤之后剩下的都含这个词",
    filtered.every((title) => title.toLowerCase().includes(needle.toLowerCase())),
    `${filtered.length} 条全部核对`
  );
  await page.fill("#session-search", "");
  await page.waitForTimeout(200);

  return detail;
}

async function checkStructurePage(page, runId, sessionId) {
  const link = page.locator("#session-head .head-actions a");
  check("会话头部有到结构页的链接", (await link.count()) === 1, `${await link.count()} 个链接`);
  const href = await link.getAttribute("href");
  check(
    "链接指向产出这个会话的运行",
    href === `#/structure/${encodeURIComponent(runId)}`,
    `链接是 ${href}，运行是 ${runId}`
  );

  await link.click();
  // waitForFunction 的第二个参数是传给页面函数的入参，不是选项。
  // 选项要放到第三个位置，否则超时时间会被当成入参丢掉，用回默认的 30 秒。
  await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });
  const snapshot = await page.evaluate(() => window.__snapshot());
  check(
    "结构页加载的运行就是链接里的那个",
    snapshot.runId === runId,
    `页面加载的是 ${snapshot.runId}`
  );

  const canvas = await page.evaluate(() => {
    const node = document.querySelector("#viewer canvas");
    const state = window.vsViewer.getSnapshot();
    return {
      width: node ? node.width : 0,
      height: node ? node.height : 0,
      structureCount: state.structureCount,
      elementCount: state.elementCount,
      representationTotal: state.representationTotal,
    };
  });
  check(
    "画布有尺寸",
    canvas.width > 0 && canvas.height > 0,
    `${canvas.width}x${canvas.height}`
  );
  check(
    "受体与十个位姿都载入了",
    canvas.structureCount === 11 && canvas.elementCount > 0,
    `${canvas.structureCount} 个结构，${canvas.elementCount} 个原子`
  );
  check(
    "画面上挂了表示",
    canvas.representationTotal > 0,
    `${canvas.representationTotal} 个表示`
  );

  const options = await page.locator("#run-picker option").allTextContents();
  check("运行选择器列出全部运行", options.length === 4, `${options.length} 项：${options.join("，")}`);

  // 相机位置要等取景动画结束再读。
  await page.waitForTimeout(1600);
  const coverage = await readCanvasCoverage(page);
  console.log(
    `三维视图：非背景像素 ${(coverage.coveredArea * 100).toFixed(1)}%，`
      + `含结构格子 ${coverage.filledCells}/${coverage.totalCells}，`
      + `最外圈含结构格子 ${coverage.filledEdgeCells}/${coverage.totalEdgeCells}`
  );
  check("画布上画出了结构", coverage.coveredArea >= 0.02, `${(coverage.coveredArea * 100).toFixed(2)}% 的像素不是背景色`);
  check(
    "相机没有贴进结构内部",
    coverage.filledEdgeCells <= coverage.totalEdgeCells * 0.5,
    `最外圈有 ${coverage.filledEdgeCells}/${coverage.totalEdgeCells} 个格子含结构`
  );

  // 口袋选区：逐个口袋比「整段选区取到的原子数」与「每个残基单独取一遍的和」。
  // 选区表达式被截断时整段取到的会少一截，两者对不上；只判选区非空看不出来。
  const meta = await getJson(`/runs/${encodeURIComponent(runId)}/meta.json`);
  const pockets = await page.evaluate(async (items) => {
    const out = [];
    for (const pocket of items) {
      const whole = await window.vsViewer.countResiduesInSelection(pocket.residues);
      let perResidue = 0;
      for (const residue of pocket.residues) {
        perResidue += await window.vsViewer.countResiduesInSelection([residue]);
      }
      out.push({ id: pocket.id, count: pocket.residues.length, whole, perResidue });
    }
    return out;
  }, meta.pockets);
  for (const pocket of pockets) {
    check(
      `口袋 ${pocket.id} 的选区覆盖全部 ${pocket.count} 个残基`,
      pocket.whole > 0 && pocket.whole === pocket.perResidue,
      `整段取到 ${pocket.whole} 个原子，逐个残基相加 ${pocket.perResidue}`
    );
  }
  const missing = await page.evaluate(async () => {
    const pocket = window.__snapshot();
    void pocket;
    return window.vsViewer.countResiduesInSelection([
      { chain: "A", resseq: 99999, resname: "XXX" },
    ]);
  });
  check("不存在的残基取到 0 个原子", missing === 0, `取到 ${missing} 个`);

  const back = page.locator("#run-session");
  check("结构页有回到会话的链接", (await back.count()) === 1, `${await back.count()} 个链接`);
  await back.click();
  await page.waitForFunction(
    () => document.querySelectorAll("#session-body article.msg").length > 0,
    undefined,
    { timeout: 60000 }
  );
  const hash = await page.evaluate(() => window.location.hash);
  check(
    "回到会话跳回了产出这次运行的会话",
    hash === `#/history/${sessionId}`,
    `地址是 ${hash}，会话是 ${sessionId}`
  );
}

// 口袋来源徽标：逐个口袋比「页面挂的徽标文字」与「meta 里 source 的映射」。
//
// 两种形状都要能打开：
//   demo    —— 口袋项有 `source`，`provenance.pockets` 是对象；
//   legacy  —— 口袋项没有 `source`，`provenance.pockets` 是字符串。
// 旧形状里**一个徽标都不该有**。硬塞一个「未知」会把「没这个字段」与「字段值是空的」
// 混成一样，只看页面有没有徽标认不出来。顺带核对来源说明那一行：有说明才显示。
async function checkPocketSourceBadges(page, runId, expectAnySource, expectBadges) {
  await page.goto(`${BASE}/#/structure/${encodeURIComponent(runId)}`, { waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });

  const meta = await getJson(`/runs/${encodeURIComponent(runId)}/meta.json`);
  const expected = meta.pockets.map((pocket) =>
    POCKET_SOURCE_TEXT[pocket.source] || ""
  );

  const badges = await page.locator("#pocket-list li .pocket-source").allTextContents();
  const mapped = expected.filter(Boolean);
  check(
    `${runId}：徽标条数与带来源的口袋数一致`,
    badges.length === mapped.length,
    `页面挂了 ${badges.length} 个徽标，meta 里有来源的口袋 ${mapped.length} 个`
  );
  const mismatch = [];
  const wantTexts = meta.pockets.map((pocket) => POCKET_SOURCE_TEXT[pocket.source] || "");
  const domTexts = await page.locator("#pocket-list li").evaluateAll((nodes) =>
    nodes.map((node) => node.querySelector(".pocket-source")?.textContent.trim() ?? "")
  );
  for (let index = 0; index < Math.min(domTexts.length, wantTexts.length); index++) {
    if (domTexts[index] !== wantTexts[index]) {
      mismatch.push(`第 ${index + 1} 个口袋挂的是「${domTexts[index]}」，应为「${wantTexts[index]}」`);
    }
  }
  check(
    `${runId}：每个口袋的徽标文字都对得上`,
    mismatch.length === 0,
    mismatch.length ? mismatch[0] : `${domTexts.length} 个口袋逐个核对，空串表示不该挂徽标`
  );
  if (!expectAnySource) {
    check(
      `${runId}：旧形状一个徽标都不挂`,
      badges.length === 0,
      `挂了 ${badges.length} 个徽标`,
    );
  }
  if (expectBadges) {
    for (const [source, text] of Object.entries(expectBadges)) {
      check(
        `${runId}：${source} 显示成「${text}」`,
        badges.includes(text),
        `页面上的徽标是：${badges.join("，") || "（没有）"}`
      );
    }
  }

  // 来源说明那一行：有说明才显示，没有就整行藏起来（不留空行）。
  const originText = meta.provenance ? meta.provenance.pockets : undefined;
  const wantsLine = Boolean(originText);
  const line = page.locator("#pocket-origin");
  const visible = await line.isVisible();
  const text = (await line.textContent()).trim();
  check(
    `${runId}：来源说明那一行按有无显示`,
    visible === wantsLine,
    wantsLine ? `meta 里有说明，页面${visible ? "显示" : "没显示"}` : `meta 里没说明，页面${visible ? "仍显示" : "已藏起"}`
  );
  if (wantsLine) {
    const want = typeof originText === "string" ? originText : originText.note || "";
    check(
      `${runId}：来源说明的文字与 meta 一致`,
      !want || text.includes(want),
      `页面是「${text}」，meta 是「${want || "（对象里没有 note）"}」`
    );
  }
  return { badges, text };
}

async function main() {
  fs.mkdirSync(path.join(ROOT, ".probe"), { recursive: true });
  const server = startServer();
  let browser = null;
  try {
    await waitReady();

    const before = await getJson("/api/sessions");
    const biggest = before.sessions.reduce((a, b) => (b.messageCount > a.messageCount ? b : a));
    console.log(
      `状态文件里 ${before.sessions.length} 个会话，消息最多的是 ${biggest.id}（${biggest.messageCount} 条）`
    );
    prepareFixture(biggest.id);

    const listing = await getJson("/api/sessions");
    const session = listing.sessions.find((item) => item.id === biggest.id);
    check(
      "产出运行的会话拿到了运行标识",
      Boolean(session.runId),
      `${biggest.id} 的 runId 是 ${session.runId}`
    );
    if (!session.runId) throw new Error("运行目录没有与会话关联上，后面的跳转检查没法做");

    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const consoleErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => consoleErrors.push(String(error)));

    await checkHistoryPage(page, listing, biggest.id);
    await checkStructurePage(page, session.runId, biggest.id);

    // 新形状与旧形状各走一遍。新形状那份按 meta 现算期望值，不写死几个徽标。
    const legacy = await checkPocketSourceBadges(page, "legacy", false, null);
    const sourced = await checkPocketSourceBadges(page, "sourced", true, {
      ligand: POCKET_SOURCE_TEXT.ligand,
      p2rank: POCKET_SOURCE_TEXT.p2rank,
    });
    console.log(
      `来源徽标：sourced 挂了 ${sourced.badges.length} 个（${sourced.badges.join("，") || "无"}），`
        + `legacy 挂了 ${legacy.badges.length} 个`
    );

    check(
      "全程没有页面报错",
      consoleErrors.length === 0,
      consoleErrors.length ? `${consoleErrors.length} 条：${consoleErrors[0]}` : "控制台与页面都没有报错"
    );

    const output = path.join(ROOT, "tests", "output");
    fs.mkdirSync(output, { recursive: true });
    await page.goto(`${BASE}/#/history/${biggest.id}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(output, "web-history.png"), fullPage: false });
    await page.goto(`${BASE}/#/structure/${encodeURIComponent(session.runId)}`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });
    await page.waitForTimeout(1600);
    await page.screenshot({ path: path.join(output, "web-structure.png") });
    console.log("截图写出到 tests/output/web-history.png 与 web-structure.png");
  } finally {
    if (browser) await browser.close();
    server.kill();
  }

  console.log("");
  if (failures.length) {
    console.log(`共 ${failures.length} 条未通过：`);
    for (const item of failures) console.log(`  - ${item}`);
    process.exit(1);
  }
  console.log("全部通过");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
