// 结构查看页面。三维部分全部走 vendor 里的 Mol* 封装，
// 这里不 import 任何 npm 包，只调 window.vsViewer 上的方法。
//
// 与单页版本的区别只有两处：结构文件从 /runs/<运行>/ 取，顶部多一个运行选择器。

(function () {
  const { ensureViewer, fetchJson } = window.vsWeb;

  const RECEPTOR_COLOR = "#b8bec7";
  const CHAIN_B_COLOR = "#cdd2d8";
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
      <select id="run-picker"></select>
      <a id="run-session" class="link-button is-hidden">回到会话</a>
      <nav id="mode-tabs">
        <button class="tab is-active" data-mode="pockets">口袋浏览</button>
        <button class="tab" data-mode="molecules">候选分子</button>
      </nav>
      <div id="view-actions">
        <label class="toggle"><input type="checkbox" id="chain-toggle" checked> 显示 B 链</label>
        <button id="reset-view">复位视角</button>
      </div>
    </header>

    <main id="stage">
      <div id="viewer"></div>
      <aside id="side">
        <section id="panel-pockets" class="panel">
          <h2>口袋清单</h2>
          <p class="hint">点一行，在三维视图里只突出这一个口袋，其余部分变暗。</p>
          <ul id="pocket-list"></ul>
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
  let chainBVisible = true;
  let ligandIndexes = [];

  function colorOfMolecule(index) {
    return MOLECULE_COLORS[index % MOLECULE_COLORS.length];
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
    const pocket = activePocketId
      ? meta.pockets.find((item) => item.id === activePocketId)
      : null;
    return {
      mode,
      chainBVisible,
      runId: currentRunId,
      pocketId: pocket ? pocket.id : null,
      pocketResidues: pocket ? pocket.residues.map((item) => `${item.resname}${item.resseq}`) : [],
      pocketResidueCount: pocket ? pocket.residues.length : 0,
      visibleMolecules: visible,
      ligandStructuresTotal: ligandIndexes.length,
      ligandStructuresShown: ligandIndexes.filter((item) => visible.includes(item.moleculeId)).length,
      structureCount: window.vsViewer.getSnapshot().structureCount,
      representationTotal: window.vsViewer.getSnapshot().representationTotal,
    };
  }

  function pocketResidueLabel(pocket) {
    const list = pocket.residues;
    if (!list.length) return "无残基";
    const formatter = (item) => `${item.resname}${item.resseq}`;
    if (list.length <= 4) return list.map(formatter).join("、");
    return `${formatter(list[0])} … ${formatter(list[list.length - 1])}（共 ${list.length} 个）`;
  }

  function renderPocketList() {
    const list = root.querySelector("#pocket-list");
    list.innerHTML = "";
    // 零口袋是正常的一态（两侧来源都没有时 `write_run` 就写这个）。
    // 空列表要配一句话，否则用户分不清「没有口袋」与「页面坏了」。
    if (!meta.pockets.length) {
      const empty = document.createElement("li");
      // 这一行的标识与真口袋分开：判据按 `data-pocket-id` 数真口袋，
      // 空态行没有这个属性，不会被算成一个口袋。
      empty.className = "pocket-empty";
      empty.textContent = "这一份没有口袋。两条来源都没给出结果，具体原因见上方那行说明。";
      list.appendChild(empty);
      return;
    }
    for (const pocket of meta.pockets) {
      const item = document.createElement("li");
      item.dataset.pocketId = pocket.id;
      if (pocket.id === activePocketId) item.classList.add("is-active");

      const head = document.createElement("div");
      head.className = "pocket-head";
      const id = document.createElement("span");
      id.className = "pocket-id";
      id.textContent = `${pocket.id} ${pocket.label}`;
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
      score.textContent = `打分 ${pocket.score}`;
      head.append(id, score);

      const metaLine = document.createElement("div");
      metaLine.className = "pocket-meta";
      metaLine.textContent = pocketResidueLabel(pocket);

      item.append(head, metaLine);
      item.addEventListener("click", () => selectPocket(pocket.id));
      list.appendChild(item);
    }
  }

  function renderMoleculeTable() {
    const body = root.querySelector("#molecule-table tbody");
    body.innerHTML = "";
    const ranked = [...meta.molecules].sort((a, b) => b.score - a.score);
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
      idCell.append(dot, document.createTextNode(molecule.name));

      const scoreCell = document.createElement("td");
      scoreCell.textContent = molecule.score.toFixed(2);

      row.append(checkCell, idCell, scoreCell);
      row.addEventListener("click", () => showDetail(molecule.id));
      body.appendChild(row);
    });
  }

  function showDetail(moleculeId) {
    detailMoleculeId = moleculeId;
    const molecule = meta.molecules.find((item) => item.id === moleculeId);

    root.querySelector("#panel-molecules").classList.add("is-hidden");
    root.querySelector("#panel-pockets").classList.add("is-hidden");
    root.querySelector("#panel-detail").classList.remove("is-hidden");

    root.querySelector("#detail-title").textContent = `${molecule.name}（${molecule.id}）`;

    const summary = root.querySelector("#detail-summary");
    summary.innerHTML = "";
    const rows = [
      ["对接打分", molecule.score.toFixed(2)],
      ["位姿文件", molecule.pose],
      ["作用链", molecule.chain],
      ["口袋", meta.pockets[0].label],
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
    for (const contact of meta.contacts) {
      const row = document.createElement("tr");
      const residue = document.createElement("td");
      residue.textContent = contact.residue;
      const kind = document.createElement("td");
      kind.textContent = contact.kind;
      const distance = document.createElement("td");
      distance.textContent = `${contact.distance.toFixed(2)} A`;
      row.append(residue, kind, distance);
      body.appendChild(row);
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

  // 受体底色的重建。整结构给灰底，B 链按开关给浅灰或抹掉。
  async function paintReceptor(highlight, showB) {
    const chains = showB
      ? meta.target.chains
      : meta.target.chains.filter((chain) => chain !== "B");

    await window.vsViewer.showChains(chains, RECEPTOR_COLOR, "cartoon", {}, 0);
    if (showB) {
      await window.vsViewer.colorChains({ B: CHAIN_B_COLOR }, "cartoon", {}, 0);
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

  async function selectPocket(pocketId) {
    activePocketId = pocketId;
    if (pocketId === null) {
      await paintReceptor(null, chainBVisible);
      await window.vsViewer.focusChainsOnly(
        meta.target.chains.filter((chain) => chainBVisible || chain !== "B"), 0
      );
      renderPocketList();
      return;
    }

    const pocket = meta.pockets.find((item) => item.id === pocketId);
    await paintReceptor(
      { residues: pocket.residues, color: POCKET_COLOR, surface: true },
      chainBVisible
    );
    // 视野对准整条参考链，口袋在蛋白里的位置才看得出来。
    await window.vsViewer.focusChainsOnly(
      meta.target.chains.filter((chain) => chainBVisible || chain !== "B"), 0
    );
    renderPocketList();
  }

  async function drawMolecules() {
    const detail = detailMoleculeId;
    const visible = detail ? new Set([detail]) : visibleMolecules;

    let highlight = null;
    if (detail) {
      highlight = { residues: meta.contacts, color: CONTACT_COLOR, surface: false };
    }
    await paintReceptor(highlight, chainBVisible);

    // 配体位姿：逐个给表示或抹掉。每个位姿都是一个独立结构。
    const ranked = [...meta.molecules].sort((a, b) => b.score - a.score);
    for (const item of ligandIndexes) {
      const index = ranked.findIndex((m) => m.id === item.moleculeId);
      const color = colorOfMolecule(index);
      if (visible.has(item.moleculeId)) {
        await window.vsViewer.setRepresentation("ball-and-stick", {
          color,
          ...LIGAND_STICK,
        }, item.structureIndex);
      } else {
        await window.vsViewer.clearRepresentations(item.structureIndex);
      }
    }

    if (detail) {
      await window.vsViewer.addResidueRepresentation(meta.contacts, "ball-and-stick", {
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
      await window.vsViewer.focusChainsOnly(
        meta.target.chains.filter((chain) => chainBVisible || chain !== "B"), 0
      );
    }
  }

  async function setMode(next) {
    mode = next;
    for (const tab of root.querySelectorAll(".tab")) {
      tab.classList.toggle("is-active", tab.dataset.mode === next);
    }

    if (next === "pockets") {
      root.querySelector("#panel-molecules").classList.add("is-hidden");
      root.querySelector("#panel-detail").classList.add("is-hidden");
      root.querySelector("#panel-pockets").classList.remove("is-hidden");
      // **零口袋是正常的一态**（`write_run` 报 `origin=none` 时就写这个），
      // 不是错误。取 `meta.pockets[0].id` 会在这一态抛异常，而异常在 async 里
      // 被吞掉、`window.__ready` 永远不置真——页面看着加载完了其实没就绪。
      if (meta.pockets.length) {
        await selectPocket(activePocketId || meta.pockets[0].id);
      } else {
        await paintReceptor(null, chainBVisible);
        await window.vsViewer.focusChainsOnly(
          meta.target.chains.filter((chain) => chainBVisible || chain !== "B"), 0
        );
        renderPocketList();
      }
      return;
    }

    root.querySelector("#panel-pockets").classList.add("is-hidden");
    activePocketId = null;
    detailMoleculeId = null;
    root.querySelector("#panel-detail").classList.add("is-hidden");
    root.querySelector("#panel-molecules").classList.remove("is-hidden");
    // 候选分子同样可能一个都没有（本轮不做对接，`molecules` 就是空数组）。
    if (visibleMolecules.size === 0 && meta.molecules.length) {
      visibleMolecules.add(meta.molecules[0].id);
    }
    renderMoleculeTable();
    await drawMolecules();
  }

  async function applyChainVisibility(showB) {
    chainBVisible = showB;
    if (mode === "pockets") {
      await selectPocket(activePocketId);
    } else {
      await drawMolecules();
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

    root.querySelector("#chain-toggle").addEventListener("change", (event) => {
      applyChainVisibility(event.target.checked);
    });

    root.querySelector("#select-all").addEventListener("click", () => {
      for (const molecule of meta.molecules) visibleMolecules.add(molecule.id);
      renderMoleculeTable();
      drawMolecules();
    });

    root.querySelector("#select-none").addEventListener("click", () => {
      visibleMolecules.clear();
      renderMoleculeTable();
      drawMolecules();
    });

    root.querySelector("#select-top3").addEventListener("click", () => {
      const ranked = [...meta.molecules].sort((a, b) => b.score - a.score).slice(0, 3);
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
    chainBVisible = root.querySelector("#chain-toggle").checked;
    ligandIndexes = [];

    renderRunPicker();
    renderSessionLink(runs.find((item) => item.id === runId));

    await window.vsViewer.initViewer(viewerEl, { background: "#f2f4f7" });
    window.__snapshot = renderSnapshot;

    await window.vsViewer.loadStructure({ url: `${base}${meta.structure}`, format: "pdb" });

    const ranked = [...meta.molecules].sort((a, b) => b.score - a.score);
    for (const molecule of ranked) {
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
    root.querySelector("#target-meta").textContent =
      `链 ${meta.target.chains.join("/")} · 共晶配体 ${meta.target.ligand} · ` +
      `${meta.pockets.length} 个口袋 · ${meta.molecules.length} 个候选分子`;
    // 口袋的来源说明。旧的演示目录这一项是字符串，新一轮是对象，两种都能读；
    // 都没有（`provenance` 里压根没这个键）时把那一行藏起来，不留空行。
    const originLine = root.querySelector("#pocket-origin");
    const origin = provenanceText(meta.provenance && meta.provenance.pockets);
    originLine.textContent = origin ? `口袋来源：${origin}` : "";
    originLine.classList.toggle("is-hidden", !origin);

    root.querySelector("#loading").classList.add("is-hidden");

    await setMode("pockets");
    // 浏览器在首次布局之前给的画布尺寸与最终尺寸不同。
    requestAnimationFrame(() => {
      window.vsViewer.handleResize();
      window.vsViewer.focusChainsOnly(
        meta.target.chains.filter((chain) => chainBVisible || chain !== "B"), 0
      );
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
