// 对接排名与详情降级的前端判据。
//
// 这个脚本只关心两件在真实浏览器里才能确认的事：
//   1. 分数越低越好的对接结果，表格第一行必须是最负的那个（不是靠数组顺序碰巧对）；
//   2. 没有 contacts / 没有 pockets 的对接运行，点进详情不能抛异常，缺的字段显示 —。
// 另外用一份旧演示数据确认：没有 `provenance.poses` 时仍按「高分在前」，没有被新规则带偏。
//
// 依赖 playwright（不在 viz/package.json 里，自己装：npm i playwright && npx playwright install chromium）。
// 跑法：node tools/check_docking_ui.js

const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PYTHON =
  process.platform === "win32"
    ? path.join(ROOT, ".venv", "Scripts", "python.exe")
    : path.join(ROOT, ".venv", "bin", "python");
const FIXTURE = path.join(ROOT, ".probe", "docking-ui-runs");
const LOG_PATH = path.join(ROOT, ".probe", "docking-ui-serve.log");
const PORT = Number(process.env.DOCKING_UI_PORT || 8871);
const BASE = `http://127.0.0.1:${PORT}`;

const failures = [];

function check(label, condition, detail) {
  console.log(`[${condition ? "通过" : "失败"}] ${label} —— ${detail}`);
  if (!condition) failures.push(`${label}（${detail}）`);
}

function posePdb(resname) {
  return [
    `HETATM    1  C1  ${resname} A   1       0.000   0.000   0.000  1.00  0.00           C`,
    `HETATM    2  C2  ${resname} A   1       1.200   0.000   0.000  1.00  0.00           C`,
    `HETATM    3  O1  ${resname} A   1       2.000   1.000   0.000  1.00  0.00           O`,
    "END",
    "",
  ].join("\n");
}

function writeRun(directory, meta, poseCount) {
  fs.mkdirSync(path.join(directory, "ligands"), { recursive: true });
  fs.copyFileSync(
    path.join(ROOT, "tests", "fixtures", "1iep.pdb"),
    path.join(directory, "receptor.pdb")
  );
  meta.structure = "receptor.pdb";
  meta.target = meta.target || {
    name: path.basename(directory),
    pdb_id: "1IEP",
    chains: ["A"],
    reference_chain: "A",
    ligand: "",
  };
  fs.writeFileSync(
    path.join(directory, "meta.json"),
    JSON.stringify(meta, null, 2),
    "utf8"
  );
  for (let index = 1; index <= poseCount; index += 1) {
    fs.writeFileSync(
      path.join(directory, "ligands", `pose-${String(index).padStart(2, "0")}.pdb`),
      posePdb("LIG"),
      "utf8"
    );
  }
}

function prepareFixture() {
  fs.rmSync(FIXTURE, { recursive: true, force: true });
  fs.mkdirSync(FIXTURE, { recursive: true });

  // 新形状：rank 与 score 故意不同序，第一行必须是 rank=1 的 beta。
  writeRun(
    path.join(FIXTURE, "docking-demo"),
    {
      run_id: "docking-demo",
      target: {
        name: "对接演示",
        pdb_id: "1IEP",
        chains: ["A"],
        reference_chain: "A",
        ligand: "",
      },
      reference_ligand: "",
      pockets: [],
      molecules: [
        { id: "A", name: "alpha", score: -5.0, pose: "ligands/pose-01.pdb", chain: "A", rank: 3 },
        { id: "B", name: "beta", score: -9.0, pose: "ligands/pose-02.pdb", chain: "A", rank: 1 },
        { id: "C", name: "gamma", score: -7.0, pose: "ligands/pose-03.pdb", chain: "A", rank: 2 },
        { id: "D", name: "delta", score: -1.0, pose: "ligands/pose-04.pdb", chain: "A", rank: 4 },
      ],
      provenance: {
        pockets: "测试数据",
        poses: {
          score_direction: "lower_is_better",
          engine: "AutoDock Vina v1.2.5",
          counts: { written: 4 },
        },
      },
    },
    4
  );

  // 旧形状：没有 rank，`provenance.poses` 是字符串；必须仍按「高分在前」。
  writeRun(
    path.join(FIXTURE, "legacy-demo"),
    {
      run_id: "legacy-demo",
      target: {
        name: "旧演示",
        pdb_id: "1IEP",
        chains: ["A"],
        reference_chain: "A",
        ligand: "",
      },
      reference_ligand: "",
      pockets: [],
      molecules: [
        { id: "L1", name: "high", score: 9.34, pose: "ligands/pose-01.pdb", chain: "A" },
        { id: "L2", name: "mid", score: 5.0, pose: "ligands/pose-02.pdb", chain: "A" },
        { id: "L3", name: "low", score: 1.0, pose: "ligands/pose-03.pdb", chain: "A" },
      ],
      provenance: { pockets: "旧演示数据", poses: "合成的演示数据" },
    },
    3
  );
}

function startServer() {
  fs.mkdirSync(path.join(ROOT, ".probe"), { recursive: true });
  const out = fs.openSync(LOG_PATH, "w");
  return spawn(
    PYTHON,
    [
      path.join(ROOT, "tools", "serve_web.py"),
      "--runs",
      FIXTURE,
      "--port",
      String(PORT),
    ],
    { stdio: ["ignore", out, out] }
  );
}

async function getJson(route) {
  const response = await fetch(`${BASE}${route}`);
  if (!response.ok) throw new Error(`${route} 返回 ${response.status}`);
  return response.json();
}

async function waitReady() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const payload = await getJson("/api/runs");
      if (payload.runs.length >= 2) return;
    } catch (error) {
      // 服务还没起来
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`服务没有在预期时间内就绪，日志在 ${LOG_PATH}`);
}

async function openMolecules(page, runId) {
  await page.goto(`${BASE}/#/structure/${encodeURIComponent(runId)}`, {
    waitUntil: "networkidle",
  });
  await page.waitForFunction(() => window.__ready === true, undefined, {
    timeout: 120000,
  });
  await page.click('.tab[data-mode="molecules"]');
  await page.waitForSelector("#panel-molecules:not(.is-hidden)");
}

async function readFirstRow(page) {
  const row = page.locator("#molecule-table tbody tr").first();
  const name = (await row.locator("td:nth-child(2)").textContent()).trim();
  const score = (await row.locator("td:nth-child(3)").textContent()).trim();
  return { name, score };
}

async function main() {
  prepareFixture();
  const server = startServer();
  let browser = null;
  try {
    await waitReady();
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });

    // ---- 新形状：rank 优先，分数越低越靠前 ----
    await openMolecules(page, "docking-demo");
    const rows = await page.locator("#molecule-table tbody tr").count();
    check("对接结果表格画出 4 行", rows === 4, `实际 ${rows} 行`);
    const first = await readFirstRow(page);
    check(
      "第一行是 rank=1 的 beta，不是数组里的 alpha",
      first.name === "beta",
      `第一行是 ${first.name}`
    );
    check("第一行分数是 -9.00", first.score === "-9.00", `实际 ${first.score}`);

    // ---- 详情：没有 contacts / pockets 也不抛异常，缺的字段显示 — ----
    await page.locator("#molecule-table tbody tr").first().click();
    await page.waitForSelector("#panel-detail:not(.is-hidden)");
    const summary = (await page.locator("#detail-summary").textContent()).trim();
    const contacts = (await page.locator("#contact-table tbody").textContent()).trim();
    check("详情写了对接口径的分数", summary.includes("-9.00"), summary);
    check("没有口袋时详情显示 —", summary.includes("口袋") && summary.includes("—"), summary);
    check(
      "没有 contacts 时详情给出空态而不是抛异常",
      contacts.includes("没有接触数据"),
      contacts
    );

    // ---- Top3 用同一套排序，不能把 delta（-1.0）选进来 ----
    await page.click("#detail-back");
    await page.click("#select-top3");
    await page.waitForFunction(
      () => {
        const snapshot = window.__snapshot && window.__snapshot();
        return snapshot && snapshot.visibleMolecules.length === 3;
      },
      undefined,
      { timeout: 30000 }
    );
    const visible = await page.evaluate(() => window.__snapshot().visibleMolecules.slice().sort());
    check(
      "只看前三选的是 B/C/A，不是 delta",
      JSON.stringify(visible) === JSON.stringify(["A", "B", "C"]),
      JSON.stringify(visible)
    );

    // ---- 旧形状：没有 rank / provenance.poses 是字符串，仍高分在前 ----
    // 详情页的 drawMolecules 是一串异步的 Mol* 调用；切换运行前等它落地，
    // 否则测到的是「切页竞态」而不是排序规则。
    await page.waitForTimeout(1200);
    await openMolecules(page, "legacy-demo");
    const legacyFirst = await readFirstRow(page);
    check(
      "旧演示仍按高分在前（9.34 排第一）",
      legacyFirst.name === "high" && legacyFirst.score === "9.34",
      `第一行是 ${legacyFirst.name} / ${legacyFirst.score}`
    );
    await page.locator("#molecule-table tbody tr").first().click();
    await page.waitForSelector("#panel-detail:not(.is-hidden)");
    const legacyContacts = (await page.locator("#contact-table tbody").textContent()).trim();
    check(
      "旧演示没有 contacts 时也不抛异常",
      legacyContacts.includes("没有接触数据"),
      legacyContacts
    );

    check(
      "全程没有页面报错",
      errors.length === 0,
      errors.length ? `${errors.length} 条：${errors[0]}` : "控制台与页面都没有报错"
    );

    const output = path.join(ROOT, "tests", "output");
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, "docking-detail.png") });
    console.log("截图写出到 tests/output/docking-detail.png");
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
