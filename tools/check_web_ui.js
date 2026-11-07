// 界面判据：在真实浏览器里打开会话历史页与结构查看页，核对画出来的内容与接口给的一致。
//
// 判据挑的是那些「界面看着正常但内容是错的」的地方：会话列表少了几条时列表照样有内容，
// 工具卡片少了输入或输出时卡片照样显示，口袋选区被截断时高亮照样亮着。
// 所以这里逐条与接口的读数对照，并且按顺序逐个卡片比，不只看总数。
//
// 脚本自己准备运行目录、自己起服务、跑完关掉，不依赖已经在跑的服务。
// 状态文件用真实那一份的**副本**：判据要在旁边写实时快照，不能写进家目录。

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
const LEDGER_FIXTURE = path.join(ROOT, ".probe", "web-ui-ledgers");
const STATE_DIR = path.join(ROOT, ".probe", "web-ui-state");
const STATE_PATH = path.join(STATE_DIR, "state.toml");
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

// 主题卡上的状态词。与 history.js 的 TOPIC_STATUS 是同一组字。
const TOPIC_STATUS_TEXT = {
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  running: "进行中",
};

// 首页任务状态。与 `web/pages/home.js` 的 STATUS_TEXT 同一套词。
const TASK_STATUS_TEXT = {
  idle: "待开始",
  running: "运行中",
  done: "已完成",
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

// 打开一个页面并等到它画完。只等某个元素出现不够：地址从 `#/history/<id>`
// 换成 `#/history` 是同一份文档内的跳转，旧页面的元素还在，等到的可能是上一页。
// 外壳在挂载结束时给 `#app-view` 打上 `data-ready`，这里等它。
async function openView(page, hash, timeout = 30000) {
  await page.goto(`${BASE}/${hash}`, { waitUntil: "networkidle" });
  await page.waitForFunction(
    () => document.getElementById("app-view")?.dataset.ready === "1",
    undefined,
    { timeout }
  );
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

function prepareState() {
  // 默认状态文件在哪由包内那一个函数说了算（`VSA_CONFIG_HOME` 会改它），
  // 在 Node 里按 `~/.vsagent` 拼会漏掉那条环境变量。
  const source = execFileSync(
    PYTHON,
    [
      "-c",
      "from vsagent.core.session_manager import default_state_path; print(default_state_path())",
    ],
    { cwd: ROOT, encoding: "utf8" }
  ).trim();
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.rmSync(path.join(STATE_DIR, "live"), { recursive: true, force: true });
  // 登记表也清空：上一轮留下的那几份会让首页的产物数不是 0，与接口对不上。
  fs.rmSync(LEDGER_FIXTURE, { recursive: true, force: true });
  fs.copyFileSync(source, STATE_PATH);
  // 真实会话里几乎只有「完成」一种主题，失败与中断很难等到。追加一条三态会话，
  // 三种状态的颜色与文案就都能在浏览器里核对。
  const seeded = execFileSync(
    PYTHON,
    [path.join(ROOT, "tools", "seed_three_states.py"), "--state", STATE_PATH],
    { cwd: ROOT, encoding: "utf8" }
  ).trim();
  return { source, seeded };
}

function startServer() {
  const out = fs.openSync(LOG_PATH, "w");
  return spawn(
    PYTHON,
    [
      path.join(ROOT, "tools", "serve_web.py"),
      "--state", STATE_PATH,
      "--runs", FIXTURE,
      "--surveys", SURVEY_FIXTURE,
      "--ledgers", LEDGER_FIXTURE,
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
  await openView(page, "#/history");
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

  // 一章一张卡：所有封存的主题都在页面上，只有最后一个默认展开。
  // 这是「按主题分段阅读」与「一次铺开整份会话」的分界。
  if (shelf.events.length) {
    await page.waitForFunction(
      (count) => document.querySelectorAll("#conversation .topic-card:not(.is-live)").length === count,
      shelf.events.length,
      { timeout: 60000 }
    );
    const cards = await page.locator("#conversation .topic-card:not(.is-live)").count();
    check(
      "每个封存的主题各占一张章节卡",
      cards === shelf.events.length,
      `画出 ${cards} 张，接口 ${shelf.events.length} 条`
    );

    const opened = await page
      .locator("#conversation .topic-card.is-open")
      .evaluateAll((nodes) => nodes.map((node) => node.dataset.event));
    const lastId = shelf.events[shelf.events.length - 1].id;
    check(
      "默认只展开最后一个主题",
      opened.length === 1 && opened[0] === lastId,
      `展开 ${opened.join("/")}，最后一条是 ${lastId}`
    );

    const runCollapsed = await page.locator("#conversation .run-block").evaluateAll((nodes) =>
      nodes.map((node) => node.classList.contains("is-open"))
    );
    check(
      "刚完成的主题默认收起工具过程",
      runCollapsed.every((open) => !open),
      `${runCollapsed.filter(Boolean).length} 个工具块是展开的`
    );

    const statuses = await page
      .locator("#conversation .topic-card:not(.is-live)")
      .evaluateAll((nodes) =>
        nodes.map((node) => [
          node.dataset.event,
          node.querySelector(".topic-status")?.textContent.trim() ?? "",
        ])
      );
    const wantStatus = shelf.events.map(
      (event) => TOPIC_STATUS_TEXT[event.status || "completed"] || "已完成"
    );
    check(
      "主题状态与接口一致",
      JSON.stringify(statuses.map((item) => item[1])) === JSON.stringify(wantStatus),
      `页面 ${statuses.map((item) => item[1]).join("/")}，接口 ${wantStatus.join("/")}`
    );
  }

  const railTopics = await page.locator("#topic-rail .topic-item").count();
  check(
    "左栏主题条数与事件接口一致",
    railTopics === shelf.events.length,
    `左栏 ${railTopics} 条，接口 ${shelf.events.length} 条`
  );

  // 主动展开一个主题：卡片打开、工具过程跟着打开，选择写进本地存储。
  if (shelf.events.length) {
    const firstId = shelf.events[0].id;
    await page
      .locator(`#conversation .topic-card[data-event="${firstId}"] .topic-toggle`)
      .click();
    await page.waitForFunction(
      (id) =>
        document
          .querySelector(`.topic-card[data-event="${id}"]`)
          ?.classList.contains("is-open") === true,
      firstId,
      { timeout: 60000 }
    );
    const runOpened = await page
      .locator(`#conversation .topic-card[data-event="${firstId}"] .run-block`)
      .evaluate((node) => node.classList.contains("is-open"));
    check("用户展开主题时工具过程跟着打开", runOpened === true, `is-open=${runOpened}`);

    const stored = await page.evaluate(
      (key) => window.localStorage.getItem(key),
      `vsa.history.open.${biggestId}`
    );
    check(
      "主动展开的主题记在本地存储里",
      Boolean(stored) && stored.includes(firstId),
      `${stored}`
    );
  }

  // 把剩下的主题都打开，正文才全部挂到页面上；下面几项要数单元。
  await page.evaluate(() => {
    for (const card of document.querySelectorAll("#conversation .topic-card:not(.is-open)")) {
      card.querySelector(".topic-toggle")?.click();
    }
  });
  const narrativeCount = detail.messages.filter((message) =>
    message.parts.some(
      (part) => !["tool", "step_start", "step_finish"].includes(part.type)
    )
  ).length;
  await page.waitForFunction(
    (count) => document.querySelectorAll("#conversation article.msg").length === count,
    narrativeCount,
    { timeout: 120000 }
  );
  const messageCount = await page.locator("#conversation article.msg").count();
  check(
    "正文里的消息条数与接口一致（工具单元单独成块，不混在正文里）",
    messageCount === narrativeCount,
    `${biggestId} 画出 ${messageCount} 条，接口里有文字的 ${narrativeCount} 条`
  );

  // 摘要行在主题展开之后才建出来，放在这里核对：它算的是「调用次数」与
  // 「去重之后的工具数」，两个数字分开写，一个工具调到三次时要看得出。
  const runCounts = await page
    .locator("#conversation .topic-card:not(.is-live) .run-count")
    .evaluateAll((nodes) => nodes.map((node) => node.textContent.trim()));
  const wantCounts = shelf.events
    .filter((event) => event.steps.length)
    .map((event) => {
      const calls = event.steps.reduce((total, step) => total + step.tools.length, 0);
      const names = new Set(event.steps.flatMap((step) => step.tools));
      return `${calls} 次调用 · ${names.size} 个工具`;
    });
  check(
    "工具执行的摘要行写对了调用次数与工具数",
    JSON.stringify(runCounts) === JSON.stringify(wantCounts),
    `页面 ${runCounts.length} 行，应为 ${wantCounts.length} 行：${runCounts.slice(0, 2).join(" | ")}`
  );

  // 三级的最后一层：步展开之后才是工具卡片。
  const firstCard = page.locator("#conversation .topic-card:not(.is-live)").first();
  if (await firstCard.locator(".step-row").count()) {
    const rows = firstCard.locator(".step-row");
    await rows.first().locator(".step-head").click();
    await page.waitForTimeout(100);
    const openedRow = await rows.first().evaluate((node) => node.classList.contains("is-open"));
    check("点一步展开它的工具卡片", openedRow === true, `is-open=${openedRow}`);

    await firstCard.locator(".run-tools button", { hasText: "展开全部步" }).click();
    await page.waitForTimeout(150);
    const allOpen = await firstCard
      .locator(".step-row")
      .evaluateAll((nodes) => nodes.every((node) => node.classList.contains("is-open")));
    check("展开全部步只作用于这一个主题", allOpen, `${await rows.count()} 行`);

    await firstCard.locator(".run-tools button", { hasText: "收起全部步" }).click();
    await page.waitForTimeout(150);
    const allClosed = await firstCard
      .locator(".step-row")
      .evaluateAll((nodes) => nodes.every((node) => !node.classList.contains("is-open")));
    check("收起全部步同样只作用于这一个主题", allClosed, `${await rows.count()} 行`);
  }

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
  // 页面上工具按步分组，`close_topic` 这类不进流水的调用单独排在末行，
  // 所以两份的先后顺序必然不同。比的是「名字 / 状态 / 有无输入 / 有无输出」
  // 这四项组成的多重集：条数少了、某一张的字段错了，都会在这里显出来。
  const signature = (item) => `${item.name}|${item.status}|${item.hasInput}|${item.hasOutput}`;
  const apiSignatures = apiTools
    .map((part) =>
      signature({
        name: part.toolName || "tool",
        status: STATUS_TEXT[part.toolStatus] || part.toolStatus || "等待",
        hasInput: Boolean(part.toolInput && Object.keys(part.toolInput).length),
        hasOutput: Boolean(part.toolOutput),
      })
    )
    .sort();
  const domSignatures = domTools.map(signature).sort();
  const firstMismatch = apiSignatures.findIndex(
    (value, index) => value !== domSignatures[index]
  );
  check(
    "每张工具卡片的四项都与接口一致",
    apiSignatures.length === domSignatures.length && firstMismatch < 0,
    firstMismatch < 0
      ? `${apiSignatures.length} 张卡片逐张核对工具名、状态、输入、输出`
      : `页面 ${domSignatures[firstMismatch]}，接口 ${apiSignatures[firstMismatch]}`
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
    () => document.querySelectorAll("#conversation .topic-card").length > 0,
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

async function checkLiveCard(page, sessionId) {
  // 实时快照的文件在状态文件旁边。判据自己写一份进去：应用没在跑的时候，
  // 这一块是页面上唯一没有数据来源的部分，只能这样验。
  const liveDir = path.join(STATE_DIR, "live");
  const target = path.join(liveDir, `${sessionId}.json`);
  fs.mkdirSync(liveDir, { recursive: true });
  const payload = {
    sessionId,
    running: true,
    updatedAt: Date.now() / 1000,
    pid: 1,
    eventCount: 0,
    topicIndex: 1,
    title: "界面判据造的运行中主题",
    steps: [
      {
        seq: 1,
        intent: "dock(ligandset=ligandset#1)",
        tools: ["dock"],
        result: "成功：docking#1",
        issues: [],
        status: "completed",
      },
    ],
    current: {
      phase: "waiting",
      tool: "analyze_pose",
      status: "awaiting",
      preview: "准备读取位姿",
      since: Date.now() / 1000 - 4,
      calls: 1,
    },
    tools: ["dock", "analyze_pose"],
    calls: 2,
  };
  fs.writeFileSync(target, JSON.stringify(payload), "utf8");

  // 先离开再回来：同一个地址的 hash 导航不会重新挂载页面。
  await openView(page, "#/history");
  await openView(page, `#/history/${sessionId}`);
  await page.waitForSelector("#conversation .topic-card.is-live", { timeout: 30000 });
  const text = await page.locator("#conversation .topic-card.is-live").innerText();
  check(
    "运行中的主题显示当前工具与等待确认",
    text.includes("等待确认：analyze_pose") && text.includes("需要确认"),
    text.slice(0, 80).replace(/\n/g, " / ")
  );
  check(
    "运行中的主题把已完成的步列出来",
    text.includes("dock") && text.includes("完成"),
    text.slice(0, 80).replace(/\n/g, " / ")
  );

  // 一步做完：快照里多一条已完成的步，当前步换成下一个。这里盯的是「原地改写」：
  // 整块重画之后文字照样对，但状态色没有过渡，滚动位置与悬停状态也会丢。
  // 所以留下当前那一行的节点句柄，改完之后看它还在不在、类名变了没有。
  const beforeRow = await page
    .locator("#conversation .topic-card.is-live .step-row.is-current")
    .elementHandle();
  payload.steps.push({
    seq: 2,
    intent: "analyze_pose(handle=docking#1)",
    tools: ["analyze_pose"],
    result: "成功：3 个接触",
    issues: [],
    status: "completed",
  });
  payload.current = {
    phase: "tool",
    tool: "contact_map",
    status: "running",
    preview: "统计接触频次",
    since: Date.now() / 1000,
    calls: 1,
  };
  payload.updatedAt = Date.now() / 1000;
  fs.writeFileSync(target, JSON.stringify(payload), "utf8");
  let advanced = true;
  try {
    await page.waitForFunction(
      () =>
        document.querySelectorAll("#conversation .topic-card.is-live .step-row").length === 3,
      undefined,
      { timeout: 20000 }
    );
  } catch (error) {
    advanced = false;
  }
  const inPlace = beforeRow
    ? await beforeRow.evaluate((node) => ({
        connected: node.isConnected,
        current: node.classList.contains("is-current"),
        status: node.querySelector(".step-status")?.textContent.trim() ?? "",
      }))
    : null;
  check(
    "一步做完是原地改写，不是整块重画",
    advanced &&
      inPlace !== null &&
      inPlace.connected &&
      !inPlace.current &&
      inPlace.status === "完成",
    JSON.stringify(inPlace)
  );

  payload.running = false;
  payload.current = null;
  payload.updatedAt = Date.now() / 1000;
  fs.writeFileSync(target, JSON.stringify(payload), "utf8");
  try {
    await page.waitForFunction(
      () => !document.querySelector("#conversation .topic-card.is-live"),
      undefined,
      { timeout: 20000 }
    );
    check("轮末之后实时块自己撤掉", true, "两秒轮询之内撤掉");
  } catch (error) {
    check("轮末之后实时块自己撤掉", false, "等了二十秒还在");
  }
  fs.unlinkSync(target);
}

async function checkHomePage(page, listing) {
  // 地址不带哈希：默认页就是工作空间。这一条同时盯着「默认页改回 history」
  // 这种回退——导航高亮与表格都得在这一页上。
  await openView(page, "");
  await page.waitForSelector(".home", { timeout: 30000 });

  const active = await page.locator(".nav-link.is-active").innerText();
  check("默认页是工作空间", active.includes("工作空间"), `高亮的是「${active}」`);

  const roots = listing.sessions.filter((item) => !item.parentId);
  const rows = page.locator(".home-table tbody tr");
  const count = await rows.count();
  check(
    "最近任务表的行数与根会话数一致",
    count === Math.min(10, roots.length),
    `页面 ${count} 行，根会话 ${roots.length} 条，上限 10`
  );

  const taskId = await rows.first().getAttribute("data-task");
  const target = listing.sessions.find((item) => item.id === taskId);
  const firstText = await rows.first().innerText();
  check(
    "首行的标题与接口一致",
    Boolean(target) && firstText.includes(target.title),
    `首行 ${firstText.split("\n")[0]}，接口 ${target && target.title}`
  );
  check(
    "首行的状态与接口一致",
    Boolean(target) && firstText.includes(TASK_STATUS_TEXT[target.status] || "待开始"),
    `首行状态应是「${target && (TASK_STATUS_TEXT[target.status] || "待开始")}」`
  );

  // 左栏与表格用的是同一份数据：根会话在左栏各占一行（运行中的另外一组）。
  const sideRows = page.locator(".home-side-row");
  check(
    "左栏的任务行数与根会话数一致",
    (await sideRows.count()) === roots.length,
    `左栏 ${await sideRows.count()} 行，根会话 ${roots.length} 条`
  );

  await rows.first().click();
  await page.waitForFunction(() => window.location.hash.startsWith("#/history/"), undefined, {
    timeout: 30000,
  });
  const hash = await page.evaluate(() => window.location.hash);
  check(
    "点一行进入这个任务",
    hash.startsWith(`#/history/${taskId}`),
    `跳到 ${hash}`
  );

  await openView(page, "#/history");
  await page.waitForSelector("#session-list li", { timeout: 30000 });
  check("会话历史仍能直接打开", true, "左栏会话列表画出来了");
}

async function checkScrollKeeper(page, sessionId) {
  // 轮末刷新与运行中接上新主题都会整栏重建。判据用「刷新」按钮触发同一条路径：
  // 它走 refreshSession → loadDetail → renderConversation，比等两秒轮询确定。
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#conversation .topic-card", { timeout: 60000 });
  // 等默认展开的那一段正文到位再量：占位行与正文的高度差会把位置读偏。
  await page.waitForSelector("#conversation .topic-card.is-open .run-block", { timeout: 60000 });

  const before = await page.evaluate(() => {
    const box = document.querySelector("#conversation");
    box.scrollTop = Math.floor((box.scrollHeight - box.clientHeight) / 2);
    return box.scrollTop;
  });
  check("长会话可以滚动到中间", before > 0, `scrollTop=${before}`);

  await page.click("#session-refresh");
  await page.waitForTimeout(1500);
  const after = await page.evaluate(
    () => document.querySelector("#conversation").scrollTop
  );
  check("刷新之后阅读位置不变", Math.abs(after - before) <= 4, `${before} → ${after}`);

  await page.evaluate(() => {
    const box = document.querySelector("#conversation");
    box.scrollTop = box.scrollHeight;
  });
  await page.click("#session-refresh");
  await page.waitForTimeout(1500);
  const gap = await page.evaluate(() => {
    const box = document.querySelector("#conversation");
    return box.scrollHeight - box.scrollTop - box.clientHeight;
  });
  check("贴着底部时刷新继续跟到底部", gap <= 4, `底部还剩 ${gap}px`);
}

async function checkOpenStatePersist(page, sessionId) {
  // 工具块与单步的展开状态要活过刷新。主题级的状态早就在存，这里补的是另外两层：
  // 用户翻到某一步的原始输出之后刷新，回来应当还在那一步。
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#conversation .topic-card", { timeout: 60000 });

  const first = page.locator("#conversation .topic-card").first();
  const name = first.locator(".topic-name");
  if (!(await first.evaluate((node) => node.classList.contains("is-open")))) {
    await name.click();
  }
  const runHead = first.locator(".run-head");
  if (!(await first.locator(".run-block").evaluate((node) => node.classList.contains("is-open")))) {
    await runHead.click();
  }
  const step = first.locator(".step-row").first();
  if (!(await step.evaluate((node) => node.classList.contains("is-open")))) {
    await step.locator(".step-head").click();
  }

  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector("#conversation .topic-card", { timeout: 60000 });
  await page.waitForTimeout(600);

  const state = await page.evaluate(() => {
    const card = document.querySelector("#conversation .topic-card");
    const block = card.querySelector(".run-block");
    const row = card.querySelector(".step-row");
    return {
      topic: card.classList.contains("is-open"),
      run: Boolean(block && block.classList.contains("is-open")),
      step: Boolean(row && row.classList.contains("is-open")),
    };
  });
  check(
    "刷新之后主题、工具块与单步都还是展开的",
    state.topic && state.run && state.step,
    `主题 ${state.topic}，工具块 ${state.run}，单步 ${state.step}`
  );
}

async function checkComposer(page, sessionId, consoleErrors) {
  // `serve_web.py` 起的服务没有提交回调，页面必须把服务端的 503 原文显示出来，
  // 而不是自己编一句「提交失败」，也不能假装成功跳走。
  await openView(page, "");
  await page.waitForSelector(".home .composer-input", { timeout: 30000 });

  await page.click(".home .composer-chip");
  const filled = await page.locator(".home .composer-input").inputValue();
  check("快捷入口只往输入框里填模板", filled.includes("<"), `填进去的是「${filled.slice(0, 20)}…」`);

  const before = await page.evaluate(() => window.location.hash);
  const errorsBefore = consoleErrors.length;
  await page.fill(".home .composer-input", "列出 P00533 的 UniProt 记录");
  await page.click(".home .composer-send");
  await page.waitForSelector(".home .composer-error:not(.is-hidden)", { timeout: 30000 });
  const text = await page.locator(".home .composer-error").innerText();
  check(
    "没有回调的服务：提交后显示 503 的原文",
    text.includes("503") && text.includes("不负责跑任务"),
    text
  );
  check(
    "提交失败时页面不跳转",
    (await page.evaluate(() => window.location.hash)) === before,
    `提交前 ${before || "（空）"}，提交后 ${(await page.evaluate(() => window.location.hash)) || "（空）"}`
  );
  // 这一条 503 是判据自己发出来的，浏览器会把它记成一条资源错误；
  // 只摘掉这一次请求期间新增的那几条，别处的报错照旧算。
  await page.waitForTimeout(200);
  consoleErrors.splice(errorsBefore, consoleErrors.length - errorsBefore);

  // 会话页底部是同一个组件，换的只是它认的会话编号。
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#chat-composer .composer-input", { timeout: 30000 });
  check(
    "会话页底部也有输入框",
    await page.locator("#chat-composer .composer-send").isVisible(),
    "输入框与发送按钮都在"
  );
}

async function checkArtifactPaneWidths(page, sessionId) {
  // 产物面板默认收起。按钮一次点开一档：240 → 300 → 380 → 收起，
  // 第四下回到收起，宽度同时记在 localStorage 里。
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#artifact-toggle", { timeout: 30000 });
  check(
    "产物面板默认收起",
    !(await page.locator("#artifact-pane").isVisible()),
    "面板不可见"
  );

  const widths = [];
  for (let index = 0; index < 3; index += 1) {
    await page.click("#artifact-toggle");
    await page.waitForTimeout(200);
    widths.push(
      Math.round(
        await page.locator("#artifact-pane").evaluate((node) => node.getBoundingClientRect().width)
      )
    );
  }
  check(
    "产物面板按 240 / 300 / 380 三档循环",
    JSON.stringify(widths) === JSON.stringify([240, 300, 380]),
    `依次是 ${widths.join(" / ")}`
  );
  const label = await page.locator("#artifact-toggle").innerText();
  check("按钮上写着当前宽度", label.includes("380"), `按钮是「${label}」`);

  await page.click("#artifact-toggle");
  await page.waitForTimeout(200);
  check(
    "第四下回到收起",
    !(await page.locator("#artifact-pane").isVisible()),
    "面板不可见"
  );

  // 换一个会话不该把宽度复位：这是屏幕的事，不是这个任务的事。
  await page.click("#artifact-toggle");
  await page.waitForTimeout(200);
  const saved = await page.evaluate(() => window.localStorage.getItem("vsa.history.artifacts"));
  check("宽度写进本地存储", saved === "240", `存的是 ${saved}`);
  await page.click("#artifact-toggle");
  await page.click("#artifact-toggle");
  await page.click("#artifact-toggle");
  await page.waitForTimeout(200);
}

async function checkFocusMode(page, sessionId) {
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#focus-toggle", { timeout: 30000 });
  // 先打开产物面板：退出专注之后它应当回到打开的样子，而不是被顺手关掉。
  await page.click("#artifact-toggle");
  await page.waitForTimeout(200);

  await page.click("#focus-toggle");
  await page.waitForTimeout(200);
  const hidden = await page.evaluate(() => {
    const display = (selector) => getComputedStyle(document.querySelector(selector)).display;
    return {
      list: display(".history-list"),
      rail: display("#topic-rail"),
      artifacts: display("#artifact-pane"),
    };
  });
  check(
    "专注模式下三栏都不显示",
    hidden.list === "none" && hidden.rail === "none" && hidden.artifacts === "none",
    JSON.stringify(hidden)
  );
  check(
    "专注模式下抽屉按钮不可用",
    (await page.locator(".pane-toggle").first().isDisabled()) &&
      (await page.locator("#artifact-toggle").isDisabled()),
    "两个按钮都禁用"
  );

  // 刷新之后仍是专注：这一项跟着人走，不跟着会话走。
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector("#focus-toggle", { timeout: 60000 });
  const stored = await page.evaluate(() => window.localStorage.getItem("vsa.history.focus"));
  check("专注状态写进本地存储", stored === "1", `存的是 ${stored}`);

  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);
  const back = await page.evaluate(() => ({
    focus: document.body.classList.contains("focus-mode"),
    list: getComputedStyle(document.querySelector(".history-list")).display,
    artifacts: getComputedStyle(document.querySelector("#artifact-pane")).display,
  }));
  check(
    "Esc 退出专注并回到进入前的样子",
    !back.focus && back.list !== "none" && back.artifacts !== "none",
    JSON.stringify(back)
  );

  // 收尾：把产物面板关掉，后面的截图与检查用默认的样子。
  for (let index = 0; index < 4; index += 1) {
    if (!(await page.locator("#artifact-pane").isVisible())) break;
    await page.click("#artifact-toggle");
    await page.waitForTimeout(150);
  }
}

async function checkNarrowDrawer(page, sessionId) {
  // 1200px 以下不挤正文：产物面板改成盖在右边的一层，宽度固定。
  await page.setViewportSize({ width: 1180, height: 900 });
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#artifact-toggle", { timeout: 30000 });
  await page.click("#artifact-toggle");
  await page.waitForTimeout(250);
  const box = await page.evaluate(() => {
    const pane = document.querySelector("#artifact-pane");
    const conversation = document.querySelector("#conversation");
    return {
      visible: pane.getBoundingClientRect().width > 0,
      position: getComputedStyle(pane).position,
      left: pane.getBoundingClientRect().left,
      conversationRight: conversation.getBoundingClientRect().right,
      width: Math.round(pane.getBoundingClientRect().width),
    };
  });
  check(
    "窄屏下产物面板盖在正文上",
    box.visible && box.position === "absolute" && box.left < box.conversationRight,
    `position=${box.position}，宽 ${box.width}，左边界 ${Math.round(box.left)}`
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(150);
}

async function checkNarrowLayouts(page, sessionId) {
  // 两档窄屏各自的行为：
  //   1000px —— 会话历史的主题导航收成抽屉，点按钮盖在正文上；
  //    640px —— 导航折成两行、搜索独占一行，输入框的按钮不许折字；
  //    480px —— 工作空间的左栏让出一截，表里收掉两列。
  // 每一档都断言整页没有横向滚动：溢出会被外层裁掉，看上去只是「右边少了点东西」。
  await page.setViewportSize({ width: 1000, height: 900 });
  await openView(page, `#/history/${sessionId}`, 60000);
  await page.waitForSelector("#conversation .topic-card", { timeout: 30000 });

  const railClosed = await page.evaluate(() => ({
    rail: getComputedStyle(document.querySelector("#topic-rail")).display,
    toggle: getComputedStyle(document.querySelector('.pane-toggle[data-pane="topics"]')).display,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  check(
    "1000px：主题导航收成抽屉",
    railClosed.rail === "none" && railClosed.toggle !== "none" && railClosed.overflow === 0,
    JSON.stringify(railClosed)
  );

  await page.click('.pane-toggle[data-pane="topics"]');
  await page.waitForTimeout(250);
  const railOpen = await page.evaluate(() => {
    const rail = document.querySelector("#topic-rail");
    const conversation = document.querySelector("#conversation");
    return {
      display: getComputedStyle(rail).display,
      position: getComputedStyle(rail).position,
      right: Math.round(rail.getBoundingClientRect().right),
      conversationLeft: Math.round(conversation.getBoundingClientRect().left),
    };
  });
  check(
    "1000px：主题抽屉盖在正文左侧",
    railOpen.display !== "none"
      && railOpen.position === "absolute"
      && railOpen.right > railOpen.conversationLeft,
    JSON.stringify(railOpen)
  );
  await page.click('.pane-toggle[data-pane="topics"]');

  await page.setViewportSize({ width: 640, height: 900 });
  await page.waitForTimeout(200);
  const narrow = await page.evaluate(() => {
    const nav = document.querySelector("#app-nav");
    const search = document.querySelector("#app-search");
    const links = [...document.querySelectorAll(".nav-link")].map((node) =>
      Math.round(node.getBoundingClientRect().height)
    );
    const send = document.querySelector(".composer-send").getBoundingClientRect();
    return {
      navHeight: Math.round(nav.getBoundingClientRect().height),
      searchTop: Math.round(search.getBoundingClientRect().top),
      links,
      send: { w: Math.round(send.width), h: Math.round(send.height) },
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  check(
    "640px：整页没有横向滚动",
    narrow.overflow === 0,
    `溢出 ${narrow.overflow}px`
  );
  check(
    "640px：导航折成两行，搜索独占一行",
    narrow.navHeight > 52 && narrow.searchTop > 20,
    `导航高 ${narrow.navHeight}，搜索在 y=${narrow.searchTop}`
  );
  check(
    "640px：导航链接不折字",
    narrow.links.every((height) => height <= 36),
    `各链接高度 ${narrow.links.join("/")}`
  );
  check(
    "640px：发送按钮不折字",
    narrow.send.h <= 40 && narrow.send.w <= 80,
    `按钮 ${narrow.send.w}x${narrow.send.h}`
  );

  await page.setViewportSize({ width: 480, height: 900 });
  await openView(page, "", 60000);
  await page.waitForSelector(".home .composer", { timeout: 30000 });
  const home = await page.evaluate(() => {
    const table = document.querySelector(".home-table").getBoundingClientRect();
    const main = document.querySelector(".home-main").getBoundingClientRect();
    return {
      table: Math.round(table.width),
      main: Math.round(main.width),
      topics: getComputedStyle(document.querySelector(".home-col-topics")).display,
      side: Math.round(document.querySelector(".home-side").getBoundingClientRect().width),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  check(
    "480px：任务表放得进主区",
    home.table <= home.main && home.overflow === 0,
    `表 ${home.table}，主区 ${home.main}，溢出 ${home.overflow}`
  );
  check(
    "480px：工作空间先收掉主题与产物两列",
    home.topics === "none" && home.side <= 180,
    `主题列 ${home.topics}，左栏 ${home.side}`
  );

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(200);
}

async function prepareArtifactLedger(listing) {
  // 找一个事件里带 handle 的会话，给那些 handle 写一份临时登记表。
  // 判据不读用户家目录里的真实产物，也不断言它们的字节数。
  for (const item of listing.sessions) {
    if (item.parentId) continue;
    const payload = await getJson(`/api/sessions/${item.id}/events`);
    for (const event of payload.events || []) {
      const handles = (event.artifacts || []).filter((name) =>
        /^(receptor|ligandset|artifact)#\d+$/.test(name)
      );
      if (!handles.length) continue;
      fs.mkdirSync(LEDGER_FIXTURE, { recursive: true });
      const entries = [];
      for (const handle of handles) {
        const [kind, seq] = handle.split("#");
        const target = path.join(LEDGER_FIXTURE, `${kind}-${seq}.json`);
        fs.writeFileSync(target, JSON.stringify({ handle }), "utf8");
        entries.push({
          handle: { kind, seq: Number(seq) },
          path: target,
          summary: `${handle} 的判据产物`,
          fingerprint: "",
          file_state: "ok",
          parents: [],
          meta: { source: "check_web_ui" },
          created_at: 1700000000.0,
        });
      }
      fs.writeFileSync(
        path.join(LEDGER_FIXTURE, `${item.id}.json`),
        JSON.stringify({ version: 1, session_id: item.id, entries }, null, 2),
        "utf8"
      );
      return { sessionId: item.id, eventId: event.id, handles };
    }
  }
  return null;
}

async function checkArtifactLinks(page, target) {
  if (!target) {
    check("主题尾部列出本主题产物并给下载链接", false, "没有找到带 handle 的事件");
    return;
  }
  await openView(
    page,
    `#/history/${target.sessionId}?event=${encodeURIComponent(target.eventId)}`
  );
  await page.waitForSelector("#conversation .topic-card.is-open .topic-artifacts", {
    timeout: 30000,
  });
  const links = await page
    .locator("#conversation .topic-card.is-open .topic-artifact a")
    .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("href")));
  check(
    "主题尾部列出本主题产物并给下载链接",
    links.length === target.handles.length,
    `链接 ${links.length} 个，handle ${target.handles.length} 个`
  );
  check(
    "下载链接指向登记表里的 handle",
    target.handles.every((handle) =>
      links.some((href) => href.includes(encodeURIComponent(handle)))
    ),
    `链接 ${links.join("，")}`
  );
}

async function checkThreeStates(page, sessionId) {
  // 那条会话由 `tools/seed_three_states.py` 追加：完成 / 失败 / 中断各一个主题，
  // 外加一份运行中的快照。核对的是「哪种状态画成哪样」，不是数字。
  await openView(page, `#/history/${sessionId}`);
  await page.waitForFunction(
    () => document.querySelectorAll("#conversation .topic-card").length >= 4,
    undefined,
    { timeout: 30000 }
  );

  const cards = await page.locator("#conversation .topic-card").evaluateAll((nodes) =>
    nodes.map((node) => ({
      live: node.classList.contains("is-live"),
      status: node.querySelector(".topic-status")?.textContent.trim() ?? "",
      statusClass: node.querySelector(".topic-status")?.className ?? "",
      open: node.classList.contains("is-open"),
      border: getComputedStyle(node).borderLeftColor,
      alert: node.querySelector(".topic-alert")?.textContent.trim() ?? "",
    }))
  );
  const sealed = cards.filter((item) => !item.live);
  check(
    "三态会话里三个封存主题各一张卡",
    sealed.length === 3,
    `${sealed.length} 张封存卡`
  );
  check(
    "状态字是完成 / 失败 / 已中断",
    sealed.map((item) => item.status).join("/") === "已完成/失败/已中断",
    sealed.map((item) => item.status).join("/")
  );
  check(
    "状态类名跟着状态走",
    sealed[0].statusClass.includes("status-completed") &&
      sealed[1].statusClass.includes("status-failed") &&
      sealed[2].statusClass.includes("status-interrupted"),
    sealed.map((item) => item.statusClass).join(" | ")
  );
  check(
    "三种状态的左边框颜色互不相同",
    new Set(sealed.map((item) => item.border)).size === 3,
    sealed.map((item) => item.border).join(" / ")
  );
  check(
    "收起的失败卡也写出失败原因",
    sealed[1].open === false && sealed[1].alert.includes("缺少 meeko"),
    `收起=${!sealed[1].open}，提示「${sealed[1].alert}」`
  );
  check(
    "完成卡与中断卡没有失败提示",
    !sealed[0].alert && !sealed[2].alert,
    `「${sealed[0].alert}」「${sealed[2].alert}」`
  );
  check(
    "默认只展开最后一个封存主题",
    sealed[0].open === false && sealed[1].open === false && sealed[2].open === true,
    sealed.map((item) => item.open).join("/")
  );

  // 打开失败那个主题：失败的那一步默认展开，工具卡把输入输出也打开。
  const failedCard = page.locator('#conversation .topic-card[data-event="evt_0002"]');
  await failedCard.locator(".topic-toggle").click();
  await page.waitForFunction(
    () =>
      document
        .querySelector('.topic-card[data-event="evt_0002"]')
        ?.classList.contains("is-open") === true,
    undefined,
    { timeout: 15000 }
  );
  const step = await failedCard.locator(".step-row").evaluate((node) => ({
    failed: node.classList.contains("is-failed"),
    open: node.classList.contains("is-open"),
    bodyOpen: node.querySelector(".tool-body")?.open === true,
    issue: node.querySelector(".issue-line")?.textContent.trim() ?? "",
  }));
  check(
    "失败的那一步默认展开并写出原因",
    step.failed && step.open && step.issue.includes("缺少 meeko"),
    JSON.stringify(step)
  );
  check(
    "失败的工具卡默认打开输入输出",
    step.bodyOpen === true,
    `details.open=${step.bodyOpen}`
  );

  // 这份快照是判据准备阶段写的，跑到这里早就过期了。断开的提醒与最后已知的
  // 状态要同时在场：只说「可能断开」，用户还是不知道停在哪儿。
  const liveCard = page.locator("#conversation .topic-card.is-live");
  check("运行中的主题也画出来", (await liveCard.count()) === 1, `${await liveCard.count()} 张`);
  const stale = await liveCard.locator(".live-now").innerText();
  check(
    "过期快照同时给出警告与最后已知状态",
    stale.includes("可能已断开") && stale.includes("analyze_pose"),
    stale.replace(/\n/g, " / ")
  );
}

async function checkSearchBox(page, sessionId) {
  const shelf = await getJson(`/api/sessions/${sessionId}/events`);
  const source = shelf.events.length ? shelf.events[0].title : shelf.title;
  const term = source.slice(0, 4);

  await openView(page, `#/history/${sessionId}`);
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
  const { source: sourceState, seeded: threeStateId } = prepareState();
  console.log(`状态文件副本来自 ${sourceState}，三态会话是 ${threeStateId}`);
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

    await checkHomePage(page, listing);
    await checkComposer(page, biggest.id, consoleErrors);
    await checkHistoryPage(page, listing, biggest.id);
    await checkLiveCard(page, biggest.id);
    await checkScrollKeeper(page, biggest.id);
    await checkOpenStatePersist(page, biggest.id);
    await checkArtifactPaneWidths(page, biggest.id);
    await checkFocusMode(page, biggest.id);
    await checkNarrowDrawer(page, biggest.id);
    await checkNarrowLayouts(page, biggest.id);
    await checkThreeStates(page, threeStateId);
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

    // 放在最后：写进临时登记表之后，首页的产物数会变，前面那些核对
    // 都拿的是写入之前的那份列表。
    const artifactTarget = await prepareArtifactLedger(listing);
    await checkArtifactLinks(page, artifactTarget);

    check(
      "全程没有页面报错",
      consoleErrors.length === 0,
      consoleErrors.length ? `${consoleErrors.length} 条：${consoleErrors[0]}` : "控制台与页面都没有报错"
    );

    const output = path.join(ROOT, "tests", "output");
    fs.mkdirSync(output, { recursive: true });
    await openView(page, `#/history/${biggest.id}`, 60000);
    // 等第一段正文画出来再截：只等 networkidle 会拍到「正在读取会话…」那一帧，
    // 截图就失去了对照的价值。
    await page.waitForSelector("#conversation .topic-card", {
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
