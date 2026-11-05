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
const SURVEY_FIXTURE = path.join(ROOT, ".probe", "web-ui-surveys");
const SURVEY_ID = "Q15858-survey-ui000001";
const LOG_PATH = path.join(ROOT, ".probe", "web-ui-serve.log");
const PORT = Number(process.env.WEB_PORT || 8866);
const BASE = `http://127.0.0.1:${PORT}`;
// 演示数据从仓库里带的那份原始结构现造，不依赖 `runs/` 下有没有留演示目录：
// `runs/` 整个目录是 gitignore 的，靠它准备夹具在干净克隆上第一步就 ENOENT，
// 而报错看起来像是判据脚本坏了。
const SOURCE_PDB = path.join(ROOT, "tests", "fixtures", "1iep.pdb");
const BUILDER = path.join(ROOT, "tools", "build_demo_data.py");

// 与 history.js 里的状态文字一致。卡片上的文字对不上时说明状态映射错了，
// 而徽标的颜色仍然正常，只看截图认不出来。
const STATUS_TEXT = {
  pending: "等待",
  awaiting: "待确认",
  running: "执行中",
  completed: "完成",
  failed: "失败",
};

// 与 history.js 里的子 agent 状态词一致。
const AGENT_STATUS_TEXT = {
  idle: "空闲",
  running: "运行中",
  done: "完成",
  failed: "失败",
  cancelled: "已中断",
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

// 准备五个运行目录，四个由 `tests/fixtures/1iep.pdb` 现造。
//   demo   —— 一份完整运行，用来核对运行选择器与三维视图。
//   legacy —— 旧形状：口袋项里没有 `source`，`provenance.pockets` 是字符串。
//             用来核对旧数据仍能打开、且不会凭空长出徽标。
//   sourced—— 在 legacy 的基础上给口袋补上 `source`、把 `provenance.pockets` 换成对象。
//   linked —— 带上产出它的会话，用来核对两个页面之间的跳转。
//   docking—— 手写的对接结果，用来核对「分数越低越好」的排序方向。
function buildRun(targetDir, sessionId) {
  fs.mkdirSync(targetDir, { recursive: true });
  fs.copyFileSync(SOURCE_PDB, path.join(targetDir, "receptor_full.pdb"));
  const args = [BUILDER, "--run-dir", targetDir];
  if (sessionId) args.push("--session-id", sessionId);
  execFileSync(PYTHON, args, { stdio: "ignore" });
}

// 对接夹具：数组顺序、分数顺序、id 顺序三者故意互不相同。
//   delta 有 `rank: 1`，分数却是最差的 -7.00 —— 排序时 rank 压过分数；
//   其余三个没有 rank，按分数从低到高（-9.00 / -8.20 / -7.50）。
// 只按数组画、或者把方向当成「高分在前」时，首行都会错。
const DOCKING_MOLECULES = [
  { id: "alpha", score: -7.5, rank: null },
  { id: "beta", score: -9.0, rank: null },
  { id: "gamma", score: -8.2, rank: null },
  { id: "delta", score: -7.0, rank: 1 },
];

function posePdb(resname) {
  return [
    `HETATM    1  C1  ${resname} A   1       0.000   0.000   0.000  1.00  0.00           C`,
    `HETATM    2  C2  ${resname} A   1       1.200   0.000   0.000  1.00  0.00           C`,
    `HETATM    3  O1  ${resname} A   1       2.000   1.000   0.000  1.00  0.00           O`,
    "END",
    "",
  ].join("\n");
}

function buildDockingRun(targetDir) {
  fs.mkdirSync(path.join(targetDir, "ligands"), { recursive: true });
  fs.copyFileSync(SOURCE_PDB, path.join(targetDir, "receptor.pdb"));
  const molecules = DOCKING_MOLECULES.map((item, index) => {
    const pose = `ligands/pose-${String(index + 1).padStart(2, "0")}.pdb`;
    fs.writeFileSync(path.join(targetDir, pose), posePdb("LIG"), "utf8");
    return {
      id: item.id,
      name: item.id,
      score: item.score,
      pose,
      chain: "A",
      ...(item.rank === null ? {} : { rank: item.rank }),
    };
  });
  const meta = {
    run_id: path.basename(targetDir),
    target: {
      name: "对接排序夹具",
      pdb_id: "1IEP",
      chains: ["A"],
      reference_chain: "A",
      ligand: "",
    },
    structure: "receptor.pdb",
    pockets: [],
    molecules,
    provenance: {
      structure_source: "tests/fixtures/1iep.pdb",
      poses: { score_direction: "lower_is_better", engine: "vina" },
    },
  };
  fs.writeFileSync(
    path.join(targetDir, "meta.json"),
    JSON.stringify(meta, null, 2),
    "utf8"
  );
}

function prepareFixture(sessionId) {
  fs.rmSync(FIXTURE, { recursive: true, force: true });
  fs.mkdirSync(FIXTURE, { recursive: true });

  buildRun(path.join(FIXTURE, "demo"));
  buildRun(path.join(FIXTURE, "legacy"));
  makeSourcedRun(path.join(FIXTURE, "legacy"), path.join(FIXTURE, "sourced"));
  buildRun(path.join(FIXTURE, "linked"), sessionId);
  buildDockingRun(path.join(FIXTURE, "docking"));
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
    [
      path.join(ROOT, "tools", "serve_web.py"),
      "--runs", FIXTURE,
      "--surveys", SURVEY_FIXTURE,
      "--port", String(PORT),
    ],
    { stdio: ["ignore", out, out] }
  );
}

// 结构全景的夹具。**判据不往家目录里写**：服务用 `--surveys` 指到这里。
function prepareSurveys() {
  const target = path.join(SURVEY_FIXTURE, SURVEY_ID);
  fs.rmSync(SURVEY_FIXTURE, { recursive: true, force: true });
  fs.mkdirSync(target, { recursive: true });
  const payload = {
    format: 1,
    accession: "Q15858",
    query: { method: "", ligand_class: "any", limit: 400 },
    total: 3,
    fetched: 3,
    groups: [
      {
        code: "9SR",
        name: "Tetrodotoxin",
        names: ["Tetrodotoxin"],
        kind: "non-polymer",
        weight: 319.268,
        count: 1,
        title_hits: 1,
        best: { pdb_id: "6J8I", resolution: 3.2, method: "ELECTRON MICROSCOPY" },
        pdb_ids: ["6J8I"],
      },
      {
        code: "Y01",
        name: "CHOLESTEROL HEMISUCCINATE",
        names: [],
        kind: "non-polymer",
        weight: 486.726,
        count: 2,
        title_hits: 0,
        best: { pdb_id: "7W9K", resolution: 2.2, method: "ELECTRON MICROSCOPY" },
        pdb_ids: ["7W9K", "7W9L"],
      },
    ],
    polymers: [
      {
        description: "Sodium channel subunit beta-1",
        uniprot_ids: ["Q07699"],
        count: 2,
        pdb_ids: ["6J8I", "7W9K"],
      },
    ],
    additives: [{ code: "NAG", name: "N-acetylglucosamine", count: 2 }],
    apo: [{ pdb_id: "2EB2", resolution: 2.5, method: "X-RAY DIFFRACTION" }],
    missing: [],
    entries: [
      {
        pdb_id: "6J8I",
        title: "Nav1.7 with tetrodotoxin",
        resolution: 3.2,
        method: "ELECTRON MICROSCOPY",
        ligands: ["9SR"],
        additives: ["NAG"],
        other_polymers: ["Sodium channel subunit beta-1"],
      },
      {
        pdb_id: "7W9K",
        title: "Nav1.7-beta1-beta2 complex",
        resolution: 2.2,
        method: "ELECTRON MICROSCOPY",
        ligands: ["Y01"],
        additives: ["NAG"],
        other_polymers: ["Sodium channel subunit beta-1"],
      },
      {
        pdb_id: "2EB2",
        title: "EGFR kinase domain",
        resolution: 2.5,
        method: "X-RAY DIFFRACTION",
        ligands: [],
        additives: [],
        other_polymers: [],
      },
    ],
    names_unavailable: [],
  };
  fs.writeFileSync(
    path.join(target, "survey.json"),
    JSON.stringify(payload, null, 2),
    "utf8"
  );
}

// 挑一个只命中一部分会话的过滤词。全都命中时看不出过滤有没有生效。
// 列表的实际顺序：根会话按接口给的顺序，各自的子 agent 紧跟在下面、
// 按创建时间升序。接口本身是按 updatedAt 倒序的平铺数组，直接拿它比会错位。
function treeOrder(sessions) {
  const byId = new Map(sessions.map((item) => [item.id, item]));
  const children = new Map();
  const roots = [];
  for (const item of sessions) {
    if (item.parentId && byId.has(item.parentId)) {
      if (!children.has(item.parentId)) children.set(item.parentId, []);
      children.get(item.parentId).push(item);
    } else {
      roots.push(item);
    }
  }
  for (const list of children.values()) {
    list.sort((left, right) => left.createdAt - right.createdAt);
  }
  const ordered = [];
  const walk = (node) => {
    ordered.push(node);
    for (const child of children.get(node.id) || []) walk(child);
  };
  for (const node of roots) walk(node);
  return ordered.map((item) => item.id);
}

function pickNeedle(sessions) {
  const titles = sessions.map((item) => item.title);
  const byId = new Map(sessions.map((item) => [item.id, item]));
  for (let length = 2; length <= 8; length++) {
    for (const title of titles) {
      const needle = title.slice(0, length).trim();
      if (!needle) continue;
      const hits = sessions.filter((item) =>
        (item.title || "").toLowerCase().includes(needle.toLowerCase())
      );
      if (!hits.length || hits.length === sessions.length) continue;
      // 子 agent 命中时父会话会作为上下文一起画出来，期望条数要把它算进去。
      const shown = new Set(hits.map((item) => item.id));
      for (const hit of hits) {
        let parent = byId.get(hit.parentId);
        while (parent && !shown.has(parent.id)) {
          shown.add(parent.id);
          parent = byId.get(parent.parentId);
        }
      }
      if (shown.size === sessions.length) continue;
      const hitIds = new Set(hits.map((item) => item.id));
      const contextTitles = [...shown]
        .filter((id) => !hitIds.has(id))
        .map((id) => byId.get(id).title);
      return { needle, expected: shown.size, contextTitles };
    }
  }
  // 标题全都一样时挑不出部分命中的词，换一个命中不到的词，
  // 期望列表里出现「没有匹配的会话」。
  return { needle: "zzz-没有这个标题-zzz", expected: 0, contextTitles: [] };
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
  const expectedIds = treeOrder(listing.sessions);
  check(
    "列表顺序与接口一致（子 agent 紧跟父会话）",
    JSON.stringify(ids) === JSON.stringify(expectedIds),
    `列表前三条 ${ids.slice(0, 3).join("/")}`
  );

  // 有子 agent 时，逐条核对缩进、状态点与父行位置。
  const childSessions = listing.sessions.filter(
    (item) => item.parentId && listing.sessions.some((other) => other.id === item.parentId)
  );
  if (childSessions.length) {
    const child = childSessions[0];
    const row = await page
      .locator(`#session-list li[data-session-id="${child.id}"]`)
      .evaluate((node) => ({
        child: node.classList.contains("is-child"),
        depth: node.dataset.depth,
        dot: node.querySelector(".agent-dot")?.className || "",
        meta: node.querySelector(".session-meta")?.textContent || "",
      }));
    check("子 agent 缩进挂在父会话下", row.child && row.depth === "1", JSON.stringify(row));
    check(
      "子 agent 有状态点且与状态对应",
      row.dot.includes(`dot-${child.status || "idle"}`),
      `状态点是 ${row.dot}，状态是 ${child.status}`
    );
    check(
      "子 agent 的元信息写出状态",
      row.meta.includes(AGENT_STATUS_TEXT[child.status] || child.status || "空闲"),
      row.meta
    );
    check(
      "父会话排在子 agent 前面",
      ids.indexOf(child.parentId) >= 0 && ids.indexOf(child.parentId) < ids.indexOf(child.id),
      `父在 ${ids.indexOf(child.parentId)}，子在 ${ids.indexOf(child.id)}`
    );

    // 折叠按钮：收起之后子行消失，再点回来。
    const toggle = page.locator(
      `#session-list li[data-session-id="${child.parentId}"] .tree-toggle`
    );
    if (await toggle.count()) {
      await toggle.click();
      await page.waitForTimeout(150);
      const hidden = await page.locator(`#session-list li[data-session-id="${child.id}"]`).count();
      const expanded = await toggle.getAttribute("aria-expanded");
      check("收起父会话之后子 agent 不再显示", hidden === 0, `还剩 ${hidden} 行`);
      check("收起后 aria-expanded 为 false", expanded === "false", `是 ${expanded}`);
      await toggle.click();
      await page.waitForTimeout(150);
      const shownAgain = await page.locator(`#session-list li[data-session-id="${child.id}"]`).count();
      check("再点一次子 agent 回来", shownAgain === 1, `回来 ${shownAgain} 行`);
    }
  }

  await page.locator(`#session-list li[data-session-id="${biggestId}"]`).click();
  const detail = await getJson(`/api/sessions/${biggestId}`);
  const shelf = await getJson(`/api/sessions/${biggestId}/events`);

  // 打开会话时只载入最后一个主题：这是工作台与旧页面的分界，整份会话不随
  // 点选一起发过来。下面先核对这一点，再点「整段对话」把全文铺开。
  if (shelf.events.length) {
    await page.waitForSelector("#conversation .event-card", { timeout: 60000 });
    const loaded = await page.locator("#conversation .event-card").count();
    check("打开会话只载入最后一个主题", loaded === 1, `载入了 ${loaded} 张事件卡`);
    const title = await page.locator("#conversation .event-title").first().textContent();
    check(
      "载入的是最后一条事件",
      title.trim() === shelf.events[shelf.events.length - 1].title,
      `页面上是「${title.trim()}」`
    );
  }

  await page.click(".workbench .topic-all");
  await page.waitForFunction(
    (count) => document.querySelectorAll("#conversation article.msg").length === count,
    detail.messages.length,
    { timeout: 60000 }
  );
  const messageCount = await page.locator("#conversation article.msg").count();
  check(
    "正文里的消息条数与接口一致",
    messageCount === detail.messages.length,
    `${biggestId} 画出 ${messageCount} 条，接口 ${detail.messages.length} 条`
  );

  const railTopics = await page.locator("#topic-rail .topic-item").count();
  check(
    "左栏主题条数与事件接口一致",
    railTopics === shelf.events.length,
    `左栏 ${railTopics} 条，接口 ${shelf.events.length} 条`
  );
  const cards = await page.locator("#conversation .event-card").count();
  check(
    "整段对话按主题分段，每个主题一张卡",
    cards === shelf.events.length,
    `画出 ${cards} 张，接口 ${shelf.events.length} 条`
  );

  const parts = detail.messages.flatMap((message) => message.parts);

  // 工具卡片：逐个比工具名、状态文字、输入与输出四项。
  // 只数卡片总数看不出「卡片在但输出丢了」这一类问题。
  const apiTools = parts.filter((part) => part.type === "tool");
  const domTools = await page.locator("#conversation .part-tool").evaluateAll((nodes) =>
    nodes.map((node) => ({
      name: node.querySelector(".tool-name")?.textContent.trim() ?? "",
      status: node.querySelector(".status")?.textContent.trim() ?? "",
      hasInput: Boolean(node.querySelector(".tool-body .tool-section.is-input pre")),
      hasOutput: Boolean(node.querySelector(".tool-body .tool-section.is-output")),
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
  const domPatches = await page.locator("#conversation .part-patch").evaluateAll((nodes) =>
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
    .locator("#conversation details.part-reasoning")
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
  const { needle, expected, contextTitles } = pickNeedle(listing.sessions);
  await page.fill("#session-search", needle);
  await page.waitForTimeout(300);
  const filtered = await page
    .locator("#session-list li[data-session-id]")
    .evaluateAll((nodes) => nodes.map((node) => node.querySelector(".session-name").textContent));
  check(
    "过滤之后列表条数与接口一致（含作为上下文的父会话）",
    filtered.length === expected,
    `用「${needle}」过滤出 ${filtered.length} 条，接口里应当有 ${expected} 条`
  );
  check(
    "过滤之后剩下的都含这个词，或者是命中项的父会话",
    filtered.every(
      (title) =>
        title.toLowerCase().includes(needle.toLowerCase()) ||
        contextTitles.includes(title)
    ),
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
  // 夹具目录有几个就应当列出几项：写死一个数的话，夹具增删之后这条断言会先红一次、
  // 改完数又永久绿着，而它要挡的正是「有的运行没列出来」。
  const fixtureRuns = fs
    .readdirSync(FIXTURE, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name);
  check(
    "运行选择器列出全部运行",
    options.length === fixtureRuns.length,
    `${options.length} 项，夹具目录 ${fixtureRuns.length} 个：${options.join("，")}`
  );

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
    () => document.querySelectorAll("#conversation article.msg").length > 0,
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
  // 深链接：地址里带 pocket 时，打开就选中那一处并切到局部视图。
  if (meta.pockets.length && meta.pockets[0].id) {
    const first = meta.pockets[0].id;
    await page.goto(
      `${BASE}/#/structure/${encodeURIComponent(runId)}?pocket=${encodeURIComponent(first)}&view=local`,
      { waitUntil: "networkidle" }
    );
    await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });
    const active = await page.locator("#pocket-list li.is-active").getAttribute("data-pocket-id");
    const local = await page.locator("#scope-local").evaluate((node) =>
      node.classList.contains("is-active")
    );
    check(
      `${runId}：深链接选中 ${first}`,
      active === first,
      `打开后选中的是「${active || "（没有）"}」`
    );
    check(`${runId}：深链接切到局部视图`, local === true, `scope-local 的状态是 ${local}`);
    await page.goto(`${BASE}/#/structure/${encodeURIComponent(runId)}`, {
      waitUntil: "networkidle",
    });
    await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });
  }
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

// 界面上的字是不是真的来自 meta.json：改掉数据再打开，看画面跟不跟着变。
//
// 这条抓的是「界面上写死了一份演示数据」。只核对「页面有内容」看不出来——
// 写死的那份同样有内容，而且永远是同一份。
async function checkDataDriven(page, runId) {
  const metaPath = path.join(FIXTURE, runId, "meta.json");
  const meta = JSON.parse(fs.readFileSync(metaPath, "utf8"));
  const target = "改过的靶点名";
  const label = "改过的口袋标签";
  const pocketScore = 0.123;
  const moleculeScore = 42.5;
  meta.target.name = target;
  meta.pockets[0].label = label;
  meta.pockets[0].score = pocketScore;
  meta.molecules[0].score = moleculeScore;
  fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), "utf8");

  await page.goto(`${BASE}/#/structure/${encodeURIComponent(runId)}`, { waitUntil: "networkidle" });
  // 地址与上一步相同，`goto` 只改哈希、不会重新挂载页面；不 reload 读到的是改之前的画面。
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });

  const title = (await page.locator("#target-name").textContent()).trim();
  const pocketText = (
    await page.locator("#pocket-list li[data-pocket-id] .pocket-id").first().textContent()
  ).trim();
  const pocketScoreText = (
    await page.locator("#pocket-list li[data-pocket-id] .pocket-score").first().textContent()
  ).trim();

  // 分子表在另一个面板里，切过去才有行。
  await page.locator('.tab[data-mode="molecules"]').click();
  await page.waitForTimeout(300);
  const topScore = (
    await page.locator("#molecule-table tbody tr td:nth-child(3)").first().textContent()
  ).trim();

  check(`${runId}：标题跟着 meta 变`, title.includes(target), `页面写的是「${title}」`);
  check(`${runId}：口袋标签跟着 meta 变`, pocketText.includes(label), `页面写的是「${pocketText}」`);
  check(
    `${runId}：口袋打分跟着 meta 变`,
    pocketScoreText.includes(String(pocketScore)),
    `页面写的是「${pocketScoreText}」`
  );
  check(
    `${runId}：分子打分跟着 meta 变`,
    topScore.includes(String(moleculeScore)),
    `表格首行写的是「${topScore}」`
  );
}

// 对接结果的排序方向：`provenance.poses.score_direction = "lower_is_better"` 时，
// 分数越低越靠前；分子带 `rank` 时以 rank 为准。
//
// 这条只有真跑一次页面才能确认：排序在浏览器里做，Python 侧看不到。
// 三种错法都长得像对的——按数组顺序画（首行碰巧是 -9.00）、按分数绝对值排、
// 方向写反成「高分在前」——只有逐行比对才分得出来。
async function checkDockingOrder(page) {
  const runId = "docking";
  await page.goto(`${BASE}/#/structure/${runId}`, { waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });
  await page.locator('.tab[data-mode="molecules"]').click();
  await page.waitForTimeout(300);

  const rows = await page.locator("#molecule-table tbody tr").evaluateAll((nodes) =>
    nodes.map((node) => ({
      name: node.querySelector("td:nth-child(2)")?.textContent.trim() ?? "",
      score: node.querySelector("td:nth-child(3)")?.textContent.trim() ?? "",
    }))
  );
  // delta 有 rank=1 但分数最差；其余三个没有 rank，按分数从低到高。
  const wantNames = ["delta", "beta", "gamma", "alpha"];
  check(
    `${runId}：分数越低越靠前，且 rank 压过分数`,
    JSON.stringify(rows.map((row) => row.name)) === JSON.stringify(wantNames),
    `页面顺序是 ${rows.map((row) => row.name).join("/")}，应为 ${wantNames.join("/")}`
  );
  check(
    `${runId}：首行是 rank 1 而不是分数最低的`,
    rows[0] && rows[0].name === "delta" && rows[0].score === "-7.00",
    `首行是 ${rows[0] ? `${rows[0].name} ${rows[0].score}` : "（没有行）"}`
  );
  check(
    `${runId}：第二行是最负的分数`,
    rows[1] && rows[1].name === "beta" && rows[1].score === "-9.00",
    `第二行是 ${rows[1] ? `${rows[1].name} ${rows[1].score}` : "（没有行）"}`
  );

  // 点进详情：分数按对接口径显示，缺 contacts 时给空态而不是抛异常。
  await page.locator("#molecule-table tbody tr").first().click();
  await page.waitForFunction(
    () => !document.querySelector("#panel-detail").classList.contains("is-hidden"),
    undefined,
    { timeout: 30000 }
  );
  const summary = (await page.locator("#detail-summary").textContent()).trim();
  check(
    `${runId}：详情按对接口径写分数`,
    summary.includes("对接打分") && summary.includes("-7.00"),
    `详情是「${summary}」`
  );
  const contactText = (await page.locator("#contact-table tbody").textContent()).trim();
  check(
    `${runId}：没有 contacts 时详情给空态`,
    contactText.includes("没有接触数据"),
    `接触表是「${contactText}」`
  );

  // 没有 `provenance.poses` 的运行仍按「高分在前」。默认方向写反的话，
  // 所有旧运行的第一行都会变成最差的那个分子。
  const demoMeta = await getJson(`/runs/demo/meta.json`);
  const best = demoMeta.molecules.reduce((left, right) =>
    right.score > left.score ? right : left
  );
  await page.goto(`${BASE}/#/structure/demo`, { waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__ready === true, undefined, { timeout: 120000 });
  await page.locator('.tab[data-mode="molecules"]').click();
  await page.waitForTimeout(300);
  const demoFirst = (
    await page.locator("#molecule-table tbody tr td:nth-child(2)").first().textContent()
  ).trim();
  check(
    "demo：没有 score_direction 时仍按高分在前",
    demoFirst === (best.name || best.id),
    `首行是「${demoFirst}」，分数最高的是「${best.name || best.id}」`
  );
}

// 结构全景页：配体组、全部条目、无配体三节都要按接口给的数画出来。
// 挑的是「画出来了但画错了」的那几处：组少了照样有内容、排序反了看不出、
// 条目链不到源头。
async function checkSurveyPage(page) {
  await page.goto(`${BASE}/#/survey/${SURVEY_ID}`, { waitUntil: "networkidle" });
  await page.waitForSelector("#survey-groups tr", { timeout: 15000 });

  const stats = (await page.textContent("#survey-stats")).replace(/\s+/g, " ");
  check(
    "全景页报出靶点与命中数",
    stats.includes("Q15858") && stats.includes("3 套"),
    stats
  );

  const groupRows = await page.locator("#survey-groups tr").count();
  check("配体组逐行画出来", groupRows === 2, `${groupRows} 行`);

  const firstGroup = (await page.textContent("#survey-groups tr")).replace(/\s+/g, " ");
  check(
    "标题点名的配体排在第一行，代号与常用名一起给",
    firstGroup.includes("★") && firstGroup.includes("9SR") && firstGroup.includes("Tetrodotoxin"),
    firstGroup
  );
  check("最佳分辨率带单位", firstGroup.includes("3.20 Å"), firstGroup);

  const entryRows = await page.locator("#survey-entries tr").count();
  check("全部条目都列出来", entryRows === 3, `${entryRows} 行`);

  const firstHref = await page.getAttribute("#survey-entries tr td a", "href");
  check(
    "条目链到 RCSB 的源头页",
    (firstHref || "").includes("rcsb.org/structure/"),
    String(firstHref)
  );

  const apoRows = await page.locator("#survey-apo tr").count();
  check("无配体单独一节", apoRows === 1, `${apoRows} 行`);

  const foot = (await page.textContent("#survey-foot")).replace(/\s+/g, " ");
  check(
    "页脚写明这是一份清单",
    foot.includes("不下载坐标") && foot.includes("不算口袋"),
    foot.slice(0, 80)
  );
}

async function checkSearchBox(page, sessionId) {
  const shelf = await getJson(`/api/sessions/${sessionId}/events`);
  const source = shelf.events.length ? shelf.events[0].title : shelf.title;
  const term = source.slice(0, 4);

  await page.goto(`${BASE}/#/history/${sessionId}`, { waitUntil: "networkidle" });
  await page.fill("#global-search", term);
  await page.waitForSelector("#search-results .search-item", { timeout: 30000 });

  const groups = await page.locator("#search-results .search-group-title").allTextContents();
  check(
    "顶部搜索给出分组结果",
    groups.length > 0,
    `用「${term}」搜到 ${groups.join(" / ")}`
  );
  const first = page.locator("#search-results .search-item").first();
  const label = (await first.textContent()).trim().slice(0, 24);
  await first.click();
  await page.waitForTimeout(800);
  const hash = await page.evaluate(() => window.location.hash);
  check(
    "点搜索结果能跳到会话或主题",
    hash.startsWith("#/history/"),
    `「${label}」跳到了 ${hash}`
  );
  check(
    "跳转之后结果面板收起",
    (await page.locator("#search-results.is-hidden").count()) === 1,
    `面板收起状态 ${await page.locator("#search-results").getAttribute("class")}`
  );
  // 深链接会整页重挂：等头部画出来再交给下一组判据，否则它读到的是空壳。
  await page.waitForSelector("#session-head .head-actions", { timeout: 30000 });
}

async function main() {
  fs.mkdirSync(path.join(ROOT, ".probe"), { recursive: true });
  prepareSurveys();
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
    await checkSearchBox(page, biggest.id);
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

    // 放在徽标之后：这一条会改掉 sourced 的 meta，改完那份数据就不能再当「原始形状」用。
    await checkDataDriven(page, "sourced");
    await checkDockingOrder(page);
    await checkSurveyPage(page);

    check(
      "全程没有页面报错",
      consoleErrors.length === 0,
      consoleErrors.length ? `${consoleErrors.length} 条：${consoleErrors[0]}` : "控制台与页面都没有报错"
    );

    const output = path.join(ROOT, "tests", "output");
    fs.mkdirSync(output, { recursive: true });
    await page.goto(`${BASE}/#/history/${biggest.id}`, { waitUntil: "networkidle" });
    // 等第一段正文画出来再截：只等 networkidle 会拍到「正在读取会话…」那一帧，
    // 截图就失去了对照的价值。
    await page.waitForSelector("#conversation .event-card, #conversation article.msg", {
      timeout: 60000,
    });
    await page.waitForTimeout(400);
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
