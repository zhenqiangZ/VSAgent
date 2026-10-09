// 判据：界面显示的内容真的来自 meta.json，而不是写在代码里的常量。
//
// 做法是复制一份演示数据，改掉里面的打分、残基编号与口袋标签，
// 再打开它，看面板文字会不会跟着变。这条判据能发现的错误是
// "界面上写死了一份演示数据"。

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const RUN_DIR = path.resolve(__dirname, "..", "runs", "1iep-demo");
const COPY_DIR = path.resolve(__dirname, "..", ".probe", "demo-variant");

// 目录留一份已经改过的数据，脚本只覆盖文件内容。
// 服务进程占着这个目录，整目录删除会报 EBUSY。
function copyRun() {
  fs.mkdirSync(path.join(COPY_DIR, "ligands"), { recursive: true });
  fs.mkdirSync(path.join(COPY_DIR, "vendor"), { recursive: true });
  const files = ["index.html", "app.js", "app.css", "meta.json", "receptor.pdb"];
  for (const name of files) {
    fs.copyFileSync(path.join(RUN_DIR, name), path.join(COPY_DIR, name));
  }
  fs.copyFileSync(
    path.join(RUN_DIR, "vendor", "3dmol.min.js"),
    path.join(COPY_DIR, "vendor", "3dmol.min.js")
  );
  for (const name of fs.readdirSync(path.join(RUN_DIR, "ligands"))) {
    fs.copyFileSync(
      path.join(RUN_DIR, "ligands", name),
      path.join(COPY_DIR, "ligands", name)
    );
  }
}

function patchMeta() {
  const metaPath = path.join(COPY_DIR, "meta.json");
  const meta = JSON.parse(fs.readFileSync(metaPath, "utf-8"));
  meta.target.name = "改过的靶点名";
  meta.pockets[0].label = "改过的口袋标签";
  meta.pockets[0].score = 0.123;
  meta.pockets[0].residues = meta.pockets[0].residues.slice(0, 3);
  meta.molecules[0].score = 42.5;
  fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), "utf-8");
  return meta;
}

async function main() {
  copyRun();
  const meta = patchMeta();

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const failures = [];
  page.on("pageerror", (error) => failures.push("脚本报错：" + error));

  await page.goto("http://127.0.0.1:8849/index.html", { waitUntil: "networkidle" });
  await page.waitForFunction(
    () => document.querySelectorAll("#pocket-list li").length > 0,
    { timeout: 15000 }
  );

  const title = await page.locator("#target-name").textContent();
  const firstPocketLabel = await page.locator("#pocket-list li .pocket-id").first().textContent();
  const firstPocketScore = await page.locator("#pocket-list li .pocket-score").first().textContent();
  const firstPocketMeta = await page.locator("#pocket-list li .pocket-meta").first().textContent();

  await page.locator('.tab[data-mode="molecules"]').click();
  await page.waitForTimeout(300);
  const topScore = await page.locator("#molecule-table tbody tr td:nth-child(3)").first().textContent();

  console.log(`标题：${title.trim()}`);
  console.log(`首个口袋：${firstPocketLabel.trim()} / ${firstPocketScore.trim()}`);
  console.log(`口袋摘要：${firstPocketMeta.trim()}`);
  console.log(`候选分子首行打分：${topScore.trim()}`);

  if (!title.includes("改过的靶点名")) failures.push("改掉靶点名之后标题没有跟着变");
  if (!firstPocketLabel.includes("改过的口袋标签")) failures.push("改掉口袋标签之后列表没有跟着变");
  if (!firstPocketScore.includes("0.123")) failures.push("改掉口袋打分之后列表没有跟着变");
  if (!topScore.includes("42.5")) failures.push("改掉分子打分之后表格没有跟着变");

  // 判据的落点是"改了数据之后选区随之改变，并且这个选区在结构里真的取到原子"。
  // 直接比字符串会依赖区间表达式的具体写法，换个写法就对不上了。
  await page.locator('.tab[data-mode="pockets"]').click();
  await page.waitForTimeout(600);
  const expectedCount = meta.pockets[0].residues.length;
  const selected = await page.evaluate(() => {
    const viewer = window.__viewer;
    const snapshot = window.__snapshot();
    const atoms = viewer.getAtomsFromSel(snapshot.pocketSelector);
    const residues = new Set(atoms.map((atom) => atom.resi));
    return {
      atomCount: atoms.length,
      residueCount: residues.size,
      label: snapshot.pocketSelectorLabel,
    };
  });
  console.log(`选区表达式：${selected.label}`);
  console.log(`选区取到的原子 ${selected.atomCount} 个，覆盖残基 ${selected.residueCount} 个`);

  if (selected.atomCount === 0) failures.push("改过的口袋选区在结构里取不到任何原子");
  if (selected.residueCount !== expectedCount) {
    failures.push(`选区覆盖 ${selected.residueCount} 个残基，数据里是 ${expectedCount} 个`);
  }

  await browser.close();

  if (failures.length) {
    console.log("\n未通过：");
    for (const item of failures) console.log("  - " + item);
    process.exit(1);
  }
  console.log("\n全部通过");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
