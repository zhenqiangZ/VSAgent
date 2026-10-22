// 结构查看页面。三维部分全部走 vendor 里的 Mol* 封装，
// 这里不 import 任何 npm 包，只调 window.vsViewer 上的方法。
//
// 与单页版本的区别只有两处：结构文件从 /runs/<运行>/ 取，顶部多一个运行选择器。

(function () {
  const { ensureViewer, fetchJson } = window.vsWeb;

  // 每条链一个颜色。顺序固定（按链号排序后的下标取色），所以同一条链在
  // 图例、卡通、切模式之间颜色不变——颜色在这里是**身份**，不是装饰。
  // 调色板挑的是明度接近、色相拉开的 12 色，浅色背景上都看得清。
  const CHAIN_COLORS = [
    "#3b6fe0", "#e05a4f", "#2f9e6b", "#d68a2e", "#8b5cf6",
    "#0e9aa7", "#d9468f", "#6b8e23", "#e2663a", "#4f6bd8",
    "#2a9d8f", "#b05a1f",
  ];
  const BASE_COLOR = "#9aa4b2";
  const POCKET_COLOR = "#1a6dd4";
  const POCKET_SURFACE_COLOR = "#4a9eff";
  const CONTACT_COLOR = "#e08a3c";

  const MOLECULE_COLORS = [
    "#e0503a", "#2f8f4e", "#8a4fd8", "#e0a020", "#1a9aa8",
    "#c9407f", "#4a6fdc", "#7a8b1e", "#b45309", "#0f766e",
  ];

  const POCKET_STICK = { size: 0.22, sizeAromatic: 0.3, sizeHetero: 0.22 };
  const CONTACT_STICK = { size: 0.14 };
  const LIGAND_STICK = { size: 0.26, sizeAromatic: 0.34 };

  // 口袋的来源，三态。值与 `pockets[].source` 一一对应（第 3 步定的口径）：
  //   ligand  → 复合物结构里的共晶配体圈出来的，实测事实
  //   p2rank  → prankweb 预测，算出来的位置
  //   uniprot_site → 预留，UniProt 标注的位点
  // 查不到就返回 undefined，调用处据此决定挂不挂徽标——**不要给默认值**，
  // 旧运行目录的 `pockets[]` 里没有这个字段，那与「值是空」不是一回事。
  const POCKET_SOURCE_LABEL = {
    ligand: "实验配体",
    p2rank: "预测",
    uniprot_site: "标注位点",
  };

  // `provenance.pockets` 有两种形状，都要能读：
  //   对象（新一轮产出）→ 读 `note`，没有就退回按 `origin` 说一句
  //   字符串（旧演示目录 `runs/1iep-demo`）→ 原样显示
  const POCKET_ORIGIN_TEXT = {
    complex: "实验复合物共晶配体，实测事实",
    predicted: "p2rank 预测，算出来的位置",
    none: "没有可用来源，口袋留空",
  };

  function provenanceText(value) {
    if (!value) return "";
    if (typeof value === "string") return value;
    if (typeof value === "object") {
      if (value.note) return value.note;
      if (value.origin) return POCKET_ORIGIN_TEXT[value.origin] || value.origin;
    }
    return "";
  }

  // 配体取景时边界向外放宽多少埃。按可见配体个数分档。
  //
  // 相机半径是「配体集合的边界球半径 + 这个余量」。单个配体的边界球半径
  // 约 4 埃，而受体卡通的口袋壁紧贴在外侧：余量太小时相机贴进结构里，
  // 配体反而被近裁剪面切掉（实测余量 0 只剩 6 个像素）；
  // 余量太大时配体又缩成一点（余量 6 剩 138 像素、14 剩 6 像素）。
  //
  // 单个配体取 3：实测 686 个像素，配体清晰、周围口袋壁也留得住。
  // 配体叠在一起时边界球本身已经够大，再加同样的余量会把画面拉远。
  function framingMargin(count) {
    if (count <= 1) return 3;
    if (count <= 3) return 2;
    return 0;
  }

  const TEMPLATE = `
    <header id="topbar">
      <div id="title-block">
        <span id="target-name">加载中</span>
        <span id="target-meta"></span>
        <span id="pocket-origin" class="is-hidden"></span>
      </div>
      <div id="run-block">
        <select id="run-picker"></select>
        <a id="run-session" class="link-button is-hidden">回到会话</a>
      </div>
      <nav id="mode-tabs">
        <button class="tab" data-mode="structure">结构总览</button>
        <button class="tab is-active" data-mode="pockets">口袋浏览</button>
        <button class="tab" data-mode="molecules">候选分子</button>
      </nav>
      <div id="view-actions">
        <button id="reset-view" title="把镜头拉回整份结构">复位视角</button>
      </div>
    </header>

    <main id="stage">
      <div id="viewer-wrap">
        <div id="viewer"></div>
        <div id="chain-legend" class="is-hidden"></div>
      </div>
      <aside id="side">
        <section id="panel-structure" class="panel is-hidden">
          <h2>结构总览</h2>
          <p class="hint">每条链一种颜色。点链名可以隐藏或显示它，颜色在两种视图里都不变。</p>
          <ul id="chain-list"></ul>
          <p id="preprocess-note" class="note is-hidden"></p>
        </section>

        <section id="panel-pockets" class="panel">
          <h2>口袋清单</h2>
          <p class="hint">点一行，在三维视图里只突出这一个口袋，其余部分变暗。</p>
          <ul id="pocket-list"></ul>
          <p id="pocket-excluded" class="note is-hidden"></p>
        </section>

        <section id="panel-molecules" class="panel is-hidden">
          <h2>候选分子</h2>
          <p class="hint">勾选框控制三维视图里显示哪几个分子；点编号看单个分子的细节。</p>
          <div id="molecule-toolbar">
            <button id="select-all">全选</button>
            <button id="select-none">全不选</button>
            <button id="select-top3">只看前三</button>
          </div>
          <table id="molecule-table">
            <thead>
              <tr><th></th><th>编号</th><th>打分</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </section>

        <section id="panel-detail" class="panel is-hidden">
          <h2 id="detail-title">分子详情</h2>
          <button id="detail-back">返回列表</button>
          <dl id="detail-summary"></dl>
          <h3>与受体的接触</h3>
          <table id="contact-table">
            <thead>
              <tr><th>残基</th><th>类型</th><th>距离</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </section>
      </aside>
    </main>

    <div id="loading">正在加载结构数据</div>
    <div id="fatal" class="is-hidden"></div>
  `;

  let root = null;
  let base = "";
  let runs = [];
  let currentRunId = "";
  let meta = null;
  let mode = "pockets";
  let activePocketId = null;
  let visibleMolecules = new Set();
  let detailMoleculeId = null;
  let allChains = [];
  let visibleChains = new Set();
  let ligandIndexes = [];

  function colorOfMolecule(index) {
    return MOLECULE_COLORS[index % MOLECULE_COLORS.length];
  }

  // 链的颜色只由它在 `allChains` 里的下标决定。切模式、点口袋、开关链
  // 都不重排 `allChains`，所以颜色不会跳。
  //
  // 超出调色板时不能取模：两条链撞成同一个颜色，「颜色即身份」就断了
  // （未预处理的旧运行里，HETATM 链会让链数轻松超过 12 条）。溢出部分
  // 按黄金角在色相环上继续走、明度交替，相邻下标也拉得开。
  function chainColor(chain) {
    const index = allChains.indexOf(chain);
    const slot = index < 0 ? 0 : index;
    if (slot < CHAIN_COLORS.length) return CHAIN_COLORS[slot];
    const overflow = slot - CHAIN_COLORS.length;
    const hue = (198 + overflow * 137.508) % 360;
    const light = 42 + (overflow % 2) * 14;
    return hslToHex(hue, 62, light);
  }

  function hslToHex(hue, saturation, light) {
    const s = saturation / 100;
    const l = light / 100;
    const k = (n) => (n + hue / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const channel = (n) => {
      const value = l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
      return Math.round(255 * value).toString(16).padStart(2, "0");
    };
    return `#${channel(0)}${channel(8)}${channel(4)}`;
  }

  function shownChains() {
    return allChains.filter((chain) => visibleChains.has(chain));
  }

  // 旧运行目录没有 `provenance.poses`（或者是字符串），沿用「高分在前」；
  // 新的对接结果在 `provenance.poses.score_direction` 里写清方向。
  // 返回 1 表示按分数升序（越低越好），-1 表示降序。
  function scoreDirection() {
    const poses = meta && meta.provenance ? meta.provenance.poses : null;
    if (poses && typeof poses === "object" && !Array.isArray(poses)) {
      if (poses.score_direction === "lower_is_better") return 1;
      if (poses.score_direction === "higher_is_better") return -1;
    }
    return -1;
  }

  function formatScore(value) {
    return Number.isFinite(value) ? value.toFixed(2) : "—";
  }

  function moleculeList() {
    return meta && Array.isArray(meta.molecules) ? meta.molecules : [];
  }

  function pocketList() {
    return meta && Array.isArray(meta.pockets) ? meta.pockets : [];
  }

  // 有 rank 就按 rank；没有 rank 才看分数方向。两者都缺时按 id 稳定排序，
  // 避免同一分数下每次刷新顺序都变。
  function rankedMolecules() {
    const direction = scoreDirection();
    return [...moleculeList()].sort((a, b) => {
      const ar = Number.isFinite(a.rank) ? a.rank : null;
      const br = Number.isFinite(b.rank) ? b.rank : null;
      if (ar !== null || br !== null) {
        const byRank = (ar === null ? Infinity : ar) - (br === null ? Infinity : br);
        if (byRank) return byRank;
      }
      const as = Number.isFinite(a.score) ? a.score : null;
      const bs = Number.isFinite(b.score) ? b.score : null;
      if (as === null && bs === null) return String(a.id).localeCompare(String(b.id));
      if (as === null) return 1;
      if (bs === null) return -1;
      return (
        direction * (as - bs) || String(a.id).localeCompare(String(b.id))
      );
    });
  }

  function fail(message) {
    const box = root.querySelector("#fatal");
    box.textContent = message;
    box.classList.remove("is-hidden");
    root.querySelector("#loading").classList.add("is-hidden");
  }

  async function fetchText(path) {
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error(`读取 ${path} 失败：HTTP ${response.status}`);
    }
    return response.text();
  }

  // 画面上实际生效的渲染输入，供自动化判据读取。
  function renderSnapshot() {
    const detail = detailMoleculeId;
    const visible = detail ? [detail] : [...visibleMolecules];
    const pockets = pocketList();
    const pocket = activePocketId
      ? pockets.find((item) => item.id === activePocketId)
      : null;
    const residues = pocket && Array.isArray(pocket.residues) ? pocket.residues : [];
    return {
      mode,
      visibleChains: [...visibleChains],
      chainColors: Object.fromEntries(allChains.map((chain) => [chain, chainColor(chain)])),
      runId: currentRunId,
      pocketId: pocket ? pocket.id : null,
      pocketResidues: residues.map((item) => `${item.resname}${item.resseq}`),
      pocketResidueCount: residues.length,
      visibleMolecules: visible,
      ligandStructuresTotal: ligandIndexes.length,
      ligandStructuresShown: ligandIndexes.filter((item) => visible.includes(item.moleculeId)).length,
      structureCount: window.vsViewer.getSnapshot().structureCount,
      representationTotal: window.vsViewer.getSnapshot().representationTotal,
    };
  }

  function pocketResidueLabel(pocket) {
    const list = Array.isArray(pocket.residues) ? pocket.residues : [];
    if (!list.length) return "无残基";
    const formatter = (item) => `${item.resname}${item.resseq}`;
    if (list.length <= 4) return list.map(formatter).join("、");
    return `${formatter(list[0])} … ${formatter(list[list.length - 1])}（共 ${list.length} 个）`;
  }

  function isStructureView() {
    return !!(meta && meta.provenance && meta.provenance.view === "structure");
  }

  // 预处理报告是 `write_run` 写进 `provenance.preprocess` 的。旧运行目录没有
  // 这一项——那时**整行不出现**，而不是显示一句「无预处理」，否则用户会把
  // 「老数据」读成「这份结构确实没洗过」。
  function preprocessText() {
    const pre = meta && meta.provenance ? meta.provenance.preprocess : null;
    if (!pre || typeof pre !== "object") return "";
    const parts = [];
    if (Array.isArray(pre.chains) && pre.chains.length) {
      parts.push(`保留链 ${pre.chains.join("")}`);
    }
    if (pre.waters) parts.push(`去水 ${pre.waters} 个`);
    const removed = pre.removed && typeof pre.removed === "object" ? pre.removed : {};
    const reasons = Object.keys(removed);
    if (reasons.length) {
      const detail = reasons.map((key) => `${key} ${removed[key]} 个`).join("、");
      parts.push(`去掉杂原子组（${detail}）`);
    }
    if (Array.isArray(pre.kept_ligands) && pre.kept_ligands.length) {
      const shown = pre.kept_ligands.slice(0, 8);
      const suffix = pre.kept_ligands.length > shown.length
        ? ` 等 ${pre.kept_ligands.length} 个`
        : "";
      parts.push(`保留配体 ${shown.join("+")}${suffix}`);
    }
    if (pre.hydrogens) parts.push(`去氢 ${pre.hydrogens} 个`);
    if (pre.models) parts.push(`只留第一套模型`);
    return parts.join("；");
  }

  // 视口左下角的链图例：点一下就是显示/隐藏这条链。
  function renderChainLegend() {
    const box = root.querySelector("#chain-legend");
    box.innerHTML = "";
    box.classList.toggle("is-hidden", !allChains.length);
    for (const chain of allChains) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chain-chip" + (visibleChains.has(chain) ? "" : " is-off");
      chip.dataset.chain = chain;
      chip.title = visibleChains.has(chain) ? "点击隐藏这条链" : "点击显示这条链";
      const dot = document.createElement("span");
      dot.className = "color-dot";
      dot.style.background = chainColor(chain);
      const label = document.createElement("span");
      label.textContent = `链 ${chain}`;
      chip.append(dot, label);
      chip.addEventListener("click", () => {
        if (visibleChains.has(chain)) visibleChains.delete(chain);
        else visibleChains.add(chain);
        applyChainVisibility();
      });
      box.appendChild(chip);
    }
  }

  function renderChainList() {
    const list = root.querySelector("#chain-list");
    list.innerHTML = "";
    if (!allChains.length) {
      const empty = document.createElement("li");
      empty.className = "pocket-empty";
      empty.textContent = "这份结构里没有可识别的聚合物链。";
      list.appendChild(empty);
    }
    for (const chain of allChains) {
      const item = document.createElement("li");
      item.dataset.chain = chain;
      if (!visibleChains.has(chain)) item.classList.add("is-off");

      const label = document.createElement("label");
      label.className = "chain-row";
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = visibleChains.has(chain);
      check.addEventListener("change", () => {
        if (check.checked) visibleChains.add(chain);
        else visibleChains.delete(chain);
        applyChainVisibility();
      });

      const dot = document.createElement("span");
      dot.className = "color-dot";
      dot.style.background = chainColor(chain);

      const name = document.createElement("span");
      name.className = "chain-name";
      name.textContent = `链 ${chain}`;

      const count = document.createElement("span");
      count.className = "chain-count";
      count.textContent = `${window.vsViewer.countChainResidues(chain, 0)} 个残基`;

      label.append(check, dot, name, count);
      item.appendChild(label);
      list.appendChild(item);
    }

    const note = root.querySelector("#preprocess-note");
    const text = preprocessText();
    note.textContent = text ? `预处理：${text}` : "";
    note.classList.toggle("is-hidden", !text);
  }

  function renderPocketList() {
    renderExcluded();
    const list = root.querySelector("#pocket-list");
    list.innerHTML = "";
    const pockets = pocketList();
    // 零口袋是正常的一态（两侧来源都没有时 `write_run` 就写这个）。
    // 空列表要配一句话，否则用户分不清「没有口袋」与「页面坏了」。
    if (!pockets.length) {
      const empty = document.createElement("li");
      // 这一行的标识与真口袋分开：判据按 `data-pocket-id` 数真口袋，
      // 空态行没有这个属性，不会被算成一个口袋。
      empty.className = "pocket-empty";
      empty.textContent = isStructureView()
        ? "这是一份只展示结构的运行，没有计算口袋。切到「结构总览」看按链配色的结构。"
        : "这一份没有口袋。两条来源都没给出结果，具体原因见上方那行说明。";
      list.appendChild(empty);
      return;
    }
    for (const pocket of pockets) {
      const item = document.createElement("li");
      item.dataset.pocketId = pocket.id;
      if (pocket.id === activePocketId) item.classList.add("is-active");

      const head = document.createElement("div");
      head.className = "pocket-head";
      const id = document.createElement("span");
      id.className = "pocket-id";
      id.textContent = `${pocket.id} ${pocket.label || "口袋"}`;
      if (pocket.is_reference) {
        const mark = document.createElement("span");
        mark.className = "pocket-ref";
        mark.textContent = "已知位点";
        id.appendChild(mark);
      }
      // 来源徽标。三态各有一句话，**认不出来时一个都不挂**——
      // 旧运行目录（`runs/1iep-demo`）的 `pockets[]` 里根本没有 `source`，
      // 硬塞一个「未知」会把「没这个字段」与「字段值是空的」混成一样。
      const sourceLabel = POCKET_SOURCE_LABEL[pocket.source];
      if (sourceLabel) {
        const badge = document.createElement("span");
        badge.className = `pocket-source pocket-source-${pocket.source}`;
        badge.textContent = sourceLabel;
        id.appendChild(badge);
      }
      const score = document.createElement("span");
      score.className = "pocket-score";
      // P0 的实验口袋没有打分；写「打分 undefined」会被当成算出来是空的。
      // 没有分数就整格留空，而不是显示一个看起来像分数的值。
      const scoreText = Number.isFinite(pocket.score) ? `打分 ${pocket.score}` : "";
      score.textContent = scoreText;
      score.classList.toggle("is-hidden", !scoreText);
      head.append(id, score);

      const metaLine = document.createElement("div");
      metaLine.className = "pocket-meta";
      metaLine.textContent = pocketResidueLabel(pocket);

      item.append(head, metaLine);
      item.addEventListener("click", () => selectPocket(pocket.id));
      list.appendChild(item);
    }
  }

  // 口袋来源筛掉了哪些杂原子组。用户看不到的就是没发生的——不显示这一段，
  // 他就分不清「这个结构只有两个配体」与「另外五个被当成溶剂筛掉了」。
  function renderExcluded() {
    const note = root.querySelector("#pocket-excluded");
    const pockets = meta && meta.provenance ? meta.provenance.pockets : null;
    const excluded = pockets && typeof pockets === "object" ? pockets.excluded : null;
    const reasons = excluded && typeof excluded === "object" ? Object.keys(excluded) : [];
    const text = reasons.length
      ? `已筛掉的杂原子组：${reasons.map((key) => `${key} ${excluded[key]} 个`).join("、")}`
      : "";
    note.textContent = text;
    note.classList.toggle("is-hidden", !text);
  }

  function renderMoleculeTable() {
    const body = root.querySelector("#molecule-table tbody");
    body.innerHTML = "";
    const ranked = rankedMolecules();
    ranked.forEach((molecule, index) => {
      const row = document.createElement("tr");
      if (index === 0) row.classList.add("rank-1");
      if (molecule.id === detailMoleculeId) row.classList.add("is-focused");

      const checkCell = document.createElement("td");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = visibleMolecules.has(molecule.id);
      check.addEventListener("click", (event) => {
        event.stopPropagation();
        if (check.checked) visibleMolecules.add(molecule.id);
        else visibleMolecules.delete(molecule.id);
        drawMolecules();
      });
      checkCell.appendChild(check);

      const idCell = document.createElement("td");
      const dot = document.createElement("span");
      dot.className = "color-dot";
      dot.style.background = colorOfMolecule(index);
      idCell.append(dot, document.createTextNode(molecule.name || molecule.id || "—"));

      const scoreCell = document.createElement("td");
      scoreCell.textContent = formatScore(molecule.score);

      row.append(checkCell, idCell, scoreCell);
      row.addEventListener("click", () => showDetail(molecule.id));
      body.appendChild(row);
    });
  }

  function showDetail(moleculeId) {
    detailMoleculeId = moleculeId;
    const molecule = moleculeList().find((item) => item.id === moleculeId);
    if (!molecule) return;

    root.querySelector("#panel-molecules").classList.add("is-hidden");
    root.querySelector("#panel-pockets").classList.add("is-hidden");
    root.querySelector("#panel-detail").classList.remove("is-hidden");

    root.querySelector("#detail-title").textContent =
      `${molecule.name || molecule.id}（${molecule.id}）`;

    const summary = root.querySelector("#detail-summary");
    summary.innerHTML = "";
    const pockets = pocketList();
    const rows = [
      ["对接打分", formatScore(molecule.score)],
      ["位姿文件", molecule.pose || "—"],
      ["作用链", molecule.chain || "—"],
      [
        "口袋",
        pockets.length ? (pockets[0].label || pockets[0].id || "—") : "—",
      ],
    ];
    for (const [key, value] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = value;
      summary.append(dt, dd);
    }

    const body = root.querySelector("#contact-table tbody");
    body.innerHTML = "";
    // 对接结果只有分数与位姿，没有逐原子接触；这里必须是空态而不是抛异常。
    const contacts = Array.isArray(meta.contacts) ? meta.contacts : [];
    if (!contacts.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 3;
      cell.textContent = "这份运行没有接触数据（对接结果只给分数与位姿）。";
      row.appendChild(cell);
      body.appendChild(row);
    } else {
      for (const contact of contacts) {
        const row = document.createElement("tr");
        const residue = document.createElement("td");
        residue.textContent = contact.residue || "—";
        const kind = document.createElement("td");
        kind.textContent = contact.kind || "—";
        const distance = document.createElement("td");
        distance.textContent = Number.isFinite(contact.distance)
          ? `${contact.distance.toFixed(2)} A`
          : "—";
        row.append(residue, kind, distance);
        body.appendChild(row);
      }
    }

    renderMoleculeTable();
    drawMolecules();
  }

  function backToList() {
    detailMoleculeId = null;
    root.querySelector("#panel-detail").classList.add("is-hidden");
    root.querySelector("#panel-pockets").classList.add("is-hidden");
    root.querySelector("#panel-molecules").classList.remove("is-hidden");
    renderMoleculeTable();
    drawMolecules();
  }

  // 受体底色的重建：先清掉旧表示，再按当前可见的链逐条上色。
  //
  // 不能只给整结构一个底色再覆盖某条链——那样没被覆盖的链会共用同一个灰，
  // 用户分不清哪条是哪条。这里改成「每条可见链一个表示、一个颜色」，
  // 底色只在一条链都没有时兜底（正常不会发生）。
  async function paintReceptor(highlight) {
    const chains = shownChains();
    await window.vsViewer.showChains([], BASE_COLOR, "cartoon", {}, 0);
    if (chains.length) {
      const assignment = {};
      for (const chain of chains) assignment[chain] = chainColor(chain);
      await window.vsViewer.colorChains(assignment, "cartoon", {}, 0);
    }

    if (!highlight) return;
    await window.vsViewer.addResidueRepresentation(highlight.residues, "ball-and-stick", {
      color: highlight.color,
      ...POCKET_STICK,
    }, 0);
    if (highlight.surface) {
      await window.vsViewer.addResidueRepresentation(highlight.residues, "molecular-surface", {
        color: POCKET_SURFACE_COLOR,
        alpha: 0.45,
      }, 0);
    }
  }

  async function focusShownChains() {
    const chains = shownChains();
    if (!chains.length) {
      // `buildChainQuery` 收到空列表会抛；全部隐藏时把镜头拉回整份结构。
      await window.vsViewer.focusAll(0);
      return;
    }
    await window.vsViewer.focusChainsOnly(chains, 0);
  }

  async function selectPocket(pocketId) {
    activePocketId = pocketId;
    if (pocketId === null) {
      await paintReceptor(null);
      await focusShownChains();
      renderPocketList();
      return;
    }

    const pocket = pocketList().find((item) => item.id === pocketId);
    if (!pocket) {
      await paintReceptor(null);
      await focusShownChains();
      renderPocketList();
      return;
    }
    const residues = Array.isArray(pocket.residues) ? pocket.residues : [];
    await paintReceptor({ residues, color: POCKET_COLOR, surface: true });
    // 视野对准整份结构，口袋在蛋白里的位置才看得出来。
    await focusShownChains();
    renderPocketList();
  }

  async function drawMolecules() {
    const detail = detailMoleculeId;
    const visible = detail ? new Set([detail]) : visibleMolecules;
    const contacts = Array.isArray(meta.contacts) ? meta.contacts : [];

    let highlight = null;
    if (detail && contacts.length) {
      highlight = { residues: contacts, color: CONTACT_COLOR, surface: false };
    }
    await paintReceptor(highlight);

    // 配体位姿：逐个给表示或抹掉。每个位姿都是一个独立结构。
    const ranked = rankedMolecules();
    for (const item of ligandIndexes) {
      const index = ranked.findIndex((m) => m.id === item.moleculeId);
      const color = colorOfMolecule(index < 0 ? 0 : index);
      if (visible.has(item.moleculeId)) {
        await window.vsViewer.setRepresentation("ball-and-stick", {
          color,
          ...LIGAND_STICK,
        }, item.structureIndex);
      } else {
        await window.vsViewer.clearRepresentations(item.structureIndex);
      }
    }

    if (detail && contacts.length) {
      await window.vsViewer.addResidueRepresentation(contacts, "ball-and-stick", {
        color: CONTACT_COLOR,
        ...CONTACT_STICK,
      }, 0);
    }

    const refs = [];
    for (const item of ligandIndexes) {
      if (visible.has(item.moleculeId)) refs.push(item.structureIndex);
    }
    if (refs.length) {
      await window.vsViewer.focusStructures(refs, framingMargin(refs.length));
    } else {
      await focusShownChains();
    }
  }

  // 结构总览：不挂口袋高亮，只按链上色。
  async function drawStructure() {
    activePocketId = null;
    await paintReceptor(null);
    await focusShownChains();
    renderChainList();
  }

  async function setMode(next) {
    mode = next;
    for (const tab of root.querySelectorAll(".tab")) {
      tab.classList.toggle("is-active", tab.dataset.mode === next);
    }

    if (next === "structure") {
      root.querySelector("#panel-pockets").classList.add("is-hidden");
      root.querySelector("#panel-molecules").classList.add("is-hidden");
      root.querySelector("#panel-detail").classList.add("is-hidden");
      root.querySelector("#panel-structure").classList.remove("is-hidden");
      await drawStructure();
      return;
    }

    root.querySelector("#panel-structure").classList.add("is-hidden");

    if (next === "pockets") {
      root.querySelector("#panel-molecules").classList.add("is-hidden");
      root.querySelector("#panel-detail").classList.add("is-hidden");
      root.querySelector("#panel-pockets").classList.remove("is-hidden");
      // **零口袋是正常的一态**（`write_run` 报 `origin=none` 时就写这个），
      // 不是错误。取 `meta.pockets[0].id` 会在这一态抛异常，而异常在 async 里
      // 被吞掉、`window.__ready` 永远不置真——页面看着加载完了其实没就绪。
      const pockets = pocketList();
      if (pockets.length) {
        await selectPocket(activePocketId || pockets[0].id);
      } else {
        await paintReceptor(null);
        await focusShownChains();
        renderPocketList();
      }
      return;
    }

    root.querySelector("#panel-pockets").classList.add("is-hidden");
    activePocketId = null;
    detailMoleculeId = null;
    root.querySelector("#panel-detail").classList.add("is-hidden");
    root.querySelector("#panel-molecules").classList.remove("is-hidden");
    // 候选分子同样可能一个都没有（没有对接时 `molecules` 就是空数组）。
    if (visibleMolecules.size === 0) {
      const first = rankedMolecules().find((item) => item.pose);
      if (first) visibleMolecules.add(first.id);
    }
    renderMoleculeTable();
    await drawMolecules();
  }

  // 链的显示/隐藏对三种模式都生效：重画受体，再按当前模式决定镜头。
  async function applyChainVisibility() {
    renderChainLegend();
    renderChainList();
    if (mode === "pockets") {
      await selectPocket(activePocketId);
    } else if (mode === "molecules") {
      await drawMolecules();
    } else {
      await drawStructure();
    }
  }

  function renderRunPicker() {
    const picker = root.querySelector("#run-picker");
    picker.innerHTML = "";
    for (const run of runs) {
      const option = document.createElement("option");
      option.value = run.id;
      option.textContent = `${run.id}（${run.name || run.pdbId || "未命名"}）`;
      picker.appendChild(option);
    }
    picker.value = currentRunId;
  }

  function renderSessionLink(run) {
    const link = root.querySelector("#run-session");
    if (!run || !run.sessionId) {
      link.classList.add("is-hidden");
      return;
    }
    link.textContent = `回到会话 ${run.sessionId}`;
    link.href = `#/history/${encodeURIComponent(run.sessionId)}`;
    link.classList.remove("is-hidden");
  }

  function bindControls() {
    for (const tab of root.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => setMode(tab.dataset.mode));
    }

    root.querySelector("#run-picker").addEventListener("change", (event) => {
      loadRun(event.target.value);
    });

    root.querySelector("#reset-view").addEventListener("click", async () => {
      await window.vsViewer.focusAll(0);
    });

    root.querySelector("#select-all").addEventListener("click", () => {
      for (const molecule of moleculeList()) visibleMolecules.add(molecule.id);
      renderMoleculeTable();
      drawMolecules();
    });

    root.querySelector("#select-none").addEventListener("click", () => {
      visibleMolecules.clear();
      renderMoleculeTable();
      drawMolecules();
    });

    root.querySelector("#select-top3").addEventListener("click", () => {
      const ranked = rankedMolecules().slice(0, 3);
      visibleMolecules = new Set(ranked.map((item) => item.id));
      renderMoleculeTable();
      drawMolecules();
    });

    root.querySelector("#detail-back").addEventListener("click", backToList);
  }

  async function loadRun(runId) {
    currentRunId = runId;
    base = `/runs/${encodeURIComponent(runId)}/`;
    window.__ready = false;

    // 换运行时把上一份彻底清掉。视口与结构都重建，避免残留的表示混进来。
    window.vsViewer.disposeViewer();
    const viewerEl = root.querySelector("#viewer");
    viewerEl.innerHTML = "";
    root.querySelector("#loading").classList.remove("is-hidden");
    root.querySelector("#fatal").classList.add("is-hidden");

    meta = JSON.parse(await fetchText(`${base}meta.json`));
    mode = "pockets";
    activePocketId = null;
    visibleMolecules = new Set();
    detailMoleculeId = null;
    allChains = [];
    visibleChains = new Set();
    ligandIndexes = [];

    renderRunPicker();
    renderSessionLink(runs.find((item) => item.id === runId));

    await window.vsViewer.initViewer(viewerEl, { background: "#eef2f7" });
    window.__snapshot = renderSnapshot;

    await window.vsViewer.loadStructure({ url: `${base}${meta.structure}`, format: "pdb" });

    // 链清单以**结构里真实存在的链**为准，不信 `meta.target.chains`：
    // 预处理会删链，而 meta 是删之前写的。旧运行目录里 meta 的链也可能不全。
    allChains = window.vsViewer.listChains(0);
    if (!allChains.length) {
      const declared = Array.isArray(meta.target.chains) ? meta.target.chains : [];
      allChains = [...declared];
    }
    visibleChains = new Set(allChains);
    renderChainLegend();
    renderChainList();

    const ranked = rankedMolecules();
    for (const molecule of ranked) {
      // 没有 pose 的记录只用于列表/详情，不往 Mol* 里塞一个 undefined URL。
      if (!molecule.pose) continue;
      const index = await window.vsViewer.loadStructure({
        url: `${base}${molecule.pose}`,
        format: "pdb",
      });
      ligandIndexes.push({ moleculeId: molecule.id, structureIndex: index });
      await window.vsViewer.clearRepresentations(index);
    }

    // 判据要按「页面自己认为该看哪几个结构」去复现取景，不能自己拼引用。
    window.__ligandRefs = () =>
      ligandIndexes
        .filter((item) => visibleMolecules.has(item.moleculeId))
        .map((item) => item.structureIndex);

    root.querySelector("#target-name").textContent =
      `${meta.target.name}（${meta.target.pdb_id}）`;
    const pockets = pocketList();
    const molecules = moleculeList();
    root.querySelector("#target-meta").textContent =
      `链 ${allChains.join("/") || "—"} · 共晶配体 ${meta.target.ligand || "—"} · ` +
      `${pockets.length} 个口袋 · ${molecules.length} 个候选分子`;
    // 口袋的来源说明。旧的演示目录这一项是字符串，新一轮是对象，两种都能读；
    // 都没有（`provenance` 里压根没这个键）时把那一行藏起来，不留空行。
    const originLine = root.querySelector("#pocket-origin");
    if (isStructureView()) {
      originLine.textContent = "展示模式：结构总览（未计算口袋）";
    } else {
      const origin = provenanceText(meta.provenance && meta.provenance.pockets);
      originLine.textContent = origin ? `口袋来源：${origin}` : "";
    }
    originLine.classList.toggle("is-hidden", !originLine.textContent);

    root.querySelector("#loading").classList.add("is-hidden");

    // 结构模式写出的运行目录默认停在「结构总览」：它本来就没有口袋，
    // 直接落在口袋页会先看到一句「这一份没有口袋」，像是出错了。
    const view = meta.provenance && meta.provenance.view;
    await setMode(view === "structure" ? "structure" : "pockets");
    // 浏览器在首次布局之前给的画布尺寸与最终尺寸不同。
    requestAnimationFrame(() => {
      window.vsViewer.handleResize();
      focusShownChains();
    });
    window.__ready = true;
  }

  async function mount(container, param) {
    await ensureViewer();
    container.innerHTML = TEMPLATE;
    root = container;
    window.__ready = false;

    const payload = await fetchJson("/api/runs");
    runs = payload.runs;
    if (!runs.length) {
      root.querySelector("#loading").classList.add("is-hidden");
      fail("runs/ 下没有可查看的运行。");
      return { unmount() { root = null; } };
    }

    bindControls();
    const wanted = param && runs.some((item) => item.id === param) ? param : runs[0].id;
    await loadRun(wanted);

    return {
      unmount() {
        window.vsViewer.disposeViewer();
        root = null;
      },
    };
  }

  window.vsPages.structure = { mount };
})();
