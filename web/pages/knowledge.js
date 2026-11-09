// 知识中心：语料、检索、记忆三个标签共用一页。
//
// 三块放在同一页而不是三个页面，是因为它们回答的是同一个问题的三段：
// 「我有什么材料」「这句问法能查到什么」「我已经确认过什么」。分页之后每次
// 都要重新选一遍上下文，而这三块本来就该并排看。
//
// 取数一律走 `window.vsApi`：写操作的自定义头、错误文本与超时只在一处定义。
// 详情用右侧抽屉，不跳页：关掉之后列表的滚动位置与筛选条件都还在。

(function () {
  const { formatTime, formatNumber } = window.vsWeb;
  const api = window.vsApi;
  const { detailPanel, statusBadge } = window.vsComponents;

  const KIND_TEXT = { doc: "文献", web: "网页" };
  const DEPTH_TEXT = { full: "全文", summary: "仅摘要" };
  const STRUCTURE_TEXT = {
    outline: "书签分节",
    heading: "标题分节",
    page: "按页",
    markdown: "Markdown 分节",
  };
  const MEMORY_KIND_TEXT = { fact: "事实", preference: "习惯", experience: "经验" };
  const MEMORY_STATE_TEXT = {
    ok: "有效",
    stale: "引用已变",
    archived: "已归档",
    superseded: "已被取代",
    expired: "已过期",
    missing: "引用丢失",
  };
  const CHUNK_PAGE = 40;
  const MODE_TEXT = { hybrid: "三路", lexical: "词法", semantic: "语义" };
  const GATE_TEXT = { "门禁 1": "三路增益", "门禁 2": "标识符掉幅", "门禁 3": "单次耗时" };

  const TEMPLATE = `
    <div class="knowledge">
      <div class="knowledge-head">
        <div class="knowledge-tabs" role="tablist">
          <button class="knowledge-tab is-active" type="button" data-tab="corpus" role="tab">语料</button>
          <button class="knowledge-tab" type="button" data-tab="search" role="tab">检索</button>
          <button class="knowledge-tab" type="button" data-tab="memory" role="tab">记忆</button>
        </div>
        <div class="knowledge-counts" id="kn-counts">正在读取…</div>
      </div>

      <div class="knowledge-panel is-active" data-panel="corpus">
        <div class="knowledge-toolbar">
          <input id="kn-corpus-q" type="search" placeholder="按标题、地址、标签过滤" autocomplete="off">
          <select id="kn-kind">
            <option value="">全部类型</option>
            <option value="doc">文献</option>
            <option value="web">网页</option>
          </select>
          <select id="kn-status">
            <option value="">全部状态</option>
            <option value="ok">正常</option>
            <option value="bad-meta">元数据缺失</option>
          </select>
          <select id="kn-sort">
            <option value="added">按加入时间</option>
            <option value="title">按标题</option>
            <option value="chunks">按块数</option>
            <option value="size">按占用</option>
          </select>
          <button id="kn-upload" type="button">上传文献</button>
          <input id="kn-file" type="file" multiple accept=".pdf,.md,.txt" hidden>
          <button id="kn-ingest" type="button">入库</button>
          <button id="kn-batch" type="button">删除选中</button>
          <button id="kn-refresh" type="button">刷新</button>
        </div>
        <div class="knowledge-message" id="kn-msg" hidden></div>
        <div class="knowledge-table-wrap">
          <table class="knowledge-table">
            <thead>
              <tr>
                <th><input id="kn-check-all" type="checkbox" aria-label="全选"></th>
                <th>标题</th><th>类型</th><th>块</th><th>向量</th><th>加入</th><th>状态</th>
              </tr>
            </thead>
            <tbody id="kn-rows"></tbody>
          </table>
          <p class="knowledge-empty" id="kn-empty" hidden>语料库还是空的。上传一份文献，或在 TUI 里搜一次网。</p>
        </div>
      </div>

      <div class="knowledge-panel" data-panel="search">
        <div class="knowledge-toolbar">
          <input id="kn-search-q" type="search" placeholder="查本地语料与记忆" autocomplete="off">
          <select id="kn-mode">
            <option value="hybrid">三路</option>
            <option value="lexical">只跑词法</option>
            <option value="semantic">只跑语义</option>
          </select>
          <select id="kn-limit">
            <option value="8">8 组</option>
            <option value="12">12 组</option>
            <option value="20">20 组</option>
          </select>
          <button id="kn-search-go" type="button" class="is-primary">检索</button>
        </div>
        <div class="knowledge-diagnostics" id="kn-diag"></div>
        <div class="knowledge-results" id="kn-results">
          <p class="knowledge-empty">输入一句问法，查本地语料与记忆。名次按每一路分别给，融合分不折算成百分比。</p>
        </div>
        <div class="knowledge-eval">
          <div class="knowledge-eval-head">
            <h4>检索评测</h4>
            <span class="knowledge-eval-note" id="kn-analytics">使用记录读取中…</span>
            <button id="kn-eval-refresh" type="button">刷新记录</button>
            <button id="kn-eval-run" type="button">跑一次评测</button>
          </div>
          <div class="knowledge-eval-body" id="kn-eval-body">
            <p class="knowledge-empty">评测在子进程里跑，三条门禁的读数与 <code>python tools/check_retrieval.py</code> 是同一份。</p>
          </div>
        </div>
      </div>

      <div class="knowledge-panel" data-panel="memory">
        <div class="knowledge-toolbar">
          <input id="kn-mem-q" type="search" placeholder="按正文或引用过滤" autocomplete="off">
          <select id="kn-mem-kind">
            <option value="">全部类型</option>
            <option value="fact">事实</option>
            <option value="preference">习惯</option>
            <option value="experience">经验</option>
          </select>
          <select id="kn-mem-state">
            <option value="">全部状态</option>
            <option value="ok">有效</option>
            <option value="stale">引用已变</option>
            <option value="archived">已归档</option>
            <option value="superseded">已被取代</option>
            <option value="expired">已过期</option>
            <option value="missing">引用丢失</option>
          </select>
          <button id="kn-mem-refresh" type="button">刷新</button>
        </div>
        <div class="knowledge-results" id="kn-mem-list"></div>
      </div>

      <dialog id="kn-rename" class="knowledge-dialog">
        <form id="kn-rename-form">
          <h3>改显示名</h3>
          <p class="knowledge-dialog-text">只改网页上显示的名字，不动原件、不动索引。清空则回到原始标题。</p>
          <input id="kn-rename-input" type="text" maxlength="200" autocomplete="off">
          <div class="knowledge-dialog-actions">
            <button id="kn-rename-cancel" type="button">取消</button>
            <button id="kn-rename-ok" type="button" class="is-primary">保存</button>
          </div>
        </form>
      </dialog>
    </div>
  `;

  let root = null;
  let panel = null;
  let state = {
    tab: "corpus",
    corpus: [],
    selected: new Set(),
    counts: "",
    memory: [],
    chunk: { id: "", offset: 0, limit: CHUNK_PAGE, total: 0, query: "" },
  };
  // 请求序号：切标签、改筛选、翻页时旧响应才回来，靠它丢弃。
  let seq = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function say(text, kind = "ok") {
    const box = root.querySelector("#kn-msg");
    box.textContent = text;
    box.className = `knowledge-message is-${kind}`;
    box.hidden = false;
  }

  function quiet() {
    root.querySelector("#kn-msg").hidden = true;
  }

  // ---- 标签切换 ----------------------------------------------------------

  function setTab(tab) {
    state.tab = tab;
    for (const node of root.querySelectorAll(".knowledge-tab")) {
      node.classList.toggle("is-active", node.dataset.tab === tab);
    }
    for (const node of root.querySelectorAll(".knowledge-panel")) {
      node.classList.toggle("is-active", node.dataset.panel === tab);
    }
    panel.close();
    // 标签的数据在切过去那一刻才取：一次挂载发三个请求，用户只看得到其中一个。
    if (tab === "memory") loadMemory();
    if (tab === "search") {
      loadEvaluation();
      loadAnalytics();
    }
  }

  // ---- 语料 --------------------------------------------------------------

  function corpusQuery() {
    const params = new URLSearchParams();
    const q = root.querySelector("#kn-corpus-q").value.trim();
    const kind = root.querySelector("#kn-kind").value;
    const status = root.querySelector("#kn-status").value;
    const sort = root.querySelector("#kn-sort").value;
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    if (status) params.set("status", status);
    if (sort) params.set("sort", sort);
    const text = params.toString();
    return text ? `?${text}` : "";
  }

  async function loadCorpus() {
    const current = ++seq;
    try {
      const payload = await api.get(`/api/library${corpusQuery()}`);
      if (current !== seq || !root) return;
      state.corpus = payload.items || [];
      state.selected = new Set();
      renderCorpus(payload);
    } catch (error) {
      if (current !== seq || !root) return;
      root.querySelector("#kn-rows").innerHTML = "";
      say(`读取语料库失败：${error.message}`, "warn");
    }
  }

  function renderCorpus(payload) {
    const rows = root.querySelector("#kn-rows");
    rows.innerHTML = "";
    for (const item of state.corpus) rows.appendChild(corpusRow(item));
    root.querySelector("#kn-empty").hidden = state.corpus.length > 0;

    const stats = payload.stats || {};
    const bits = [
      `${formatNumber(stats.sources || 0)} 份`,
      `${formatNumber(stats.chunks || 0)} 块`,
      formatBytes(stats.bytes),
    ];
    if (payload.total !== undefined && payload.total !== stats.sources) {
      bits.push(`筛出 ${formatNumber(payload.total)} 份`);
    }
    state.counts = bits.join(" · ");
    root.querySelector("#kn-counts").textContent = state.counts;
    root.querySelector("#kn-check-all").checked = false;
    root.querySelector("#kn-batch").disabled = true;
    updateIngestButton(payload);
  }

  function corpusRow(item) {
    const row = el("tr");
    row.dataset.id = item.id;

    const checkCell = el("td");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.addEventListener("click", (event) => event.stopPropagation());
    check.addEventListener("change", () => {
      if (check.checked) state.selected.add(item.id);
      else state.selected.delete(item.id);
      row.classList.toggle("is-selected", check.checked);
      root.querySelector("#kn-batch").disabled = state.selected.size === 0;
    });
    checkCell.appendChild(check);
    row.appendChild(checkCell);

    const title = el("td");
    title.appendChild(el("div", "knowledge-title", item.title));
    if (item.tags && item.tags.length) {
      const tags = el("div", "knowledge-tags");
      for (const tag of item.tags) tags.appendChild(el("span", "knowledge-tag", tag));
      title.appendChild(tags);
    }
    if (item.locator) {
      const locator = el("div", "knowledge-locator", item.locator);
      locator.title = item.locator;
      title.appendChild(locator);
    }
    row.appendChild(title);

    row.appendChild(el("td", "knowledge-muted", KIND_TEXT[item.kind] || item.kind));
    row.appendChild(el("td", "knowledge-muted", formatNumber(item.chunks || 0)));
    row.appendChild(el("td", "knowledge-muted", formatNumber(item.vectors || 0)));
    row.appendChild(el("td", "knowledge-muted", formatTime(item.addedAt)));
    const statusCell = el("td");
    statusCell.appendChild(statusBadge(item.status === "ok" ? "ok" : "bad-meta"));
    row.appendChild(statusCell);

    row.addEventListener("click", () => openSource(item.id, row));
    return row;
  }

  function selectRow(row) {
    for (const node of root.querySelectorAll("#kn-rows tr")) {
      node.classList.toggle("is-selected", node === row);
    }
  }

  function detailBlock(title, body) {
    const block = el("div", "knowledge-detail-block");
    block.appendChild(el("h4", "", title));
    block.appendChild(body);
    return block;
  }

  function keyValues(pairs) {
    const list = el("ul");
    for (const [name, value] of pairs) {
      if (value === undefined || value === null || value === "") continue;
      list.appendChild(el("li", "", `${name}：${value}`));
    }
    return list;
  }

  async function openSource(id, row) {
    selectRow(row);
    const current = ++seq;
    panel.setTitle("正在读取…");
    panel.setSubtitle(id);
    panel.setBody("");
    panel.open();
    try {
      const payload = await api.get(`/api/library/${encodeURIComponent(id)}`);
      if (current !== seq || !root) return;
      panel.setTitle(payload.title || id);
      panel.setSubtitle(`${payload.id} · ${payload.locator || "（没有地址）"}`);
      const body = document.createDocumentFragment();
      const meta = payload.meta || {};
      body.appendChild(
        detailBlock(
          "元数据",
          keyValues([
            ["原始标题", payload.rawTitle],
            ["页数", meta.pages],
            ["结构", STRUCTURE_TEXT[meta.structure] || meta.structure],
            ["深度", DEPTH_TEXT[meta.depth] || meta.depth],
            ["字数", meta.chars],
            ["加入", meta.added_at ? formatTime(meta.added_at) : ""],
            ["摘要", meta.summary],
            ["警告", (meta.warnings || []).join("；")],
          ])
        )
      );
      const labels = payload.labels || {};
      body.appendChild(
        detailBlock(
          "标签与文件夹",
          keyValues([
            ["显示名", labels.display_title],
            ["标签", (labels.tags || []).join("、")],
            ["收藏", labels.favorite ? "是" : ""],
            ["文件夹", labels.folderId],
          ])
        )
      );
      // 单条动作放在抽屉里，批量动作放在工具栏：两处入口各自对着一类操作。
      const actions = el("div", "knowledge-actions");
      const rename = el("button", "is-primary", "改显示名");
      rename.type = "button";
      rename.addEventListener("click", () => openRename(payload));
      actions.appendChild(rename);
      body.appendChild(detailBlock("操作", actions));
      const files = el("ul");
      for (const item of payload.files || []) {
        files.appendChild(el("li", "", `${item.name}（${formatBytes(item.bytes)}）`));
      }
      if (!files.children.length) files.appendChild(el("li", "", "（目录里没有文件）"));
      body.appendChild(
        detailBlock(
          `索引与文件（${formatNumber(payload.chunks)} 块 · 向量 ${formatNumber(payload.vectors)}）`,
          files
        )
      );
      const chunksBox = el("div");
      chunksBox.id = "kn-chunks";
      body.appendChild(detailBlock("切块（索引里那一份）", chunksBox));
      panel.setBody(body);
      state.chunk = { id, offset: 0, limit: CHUNK_PAGE, total: payload.chunks || 0, query: "" };
      await loadChunks();
    } catch (error) {
      if (current !== seq || !root) return;
      panel.setTitle("读取失败");
      panel.setBody(el("p", "knowledge-empty", error.message));
    }
  }

  async function loadChunks(offset = state.chunk.offset) {
    const box = root.querySelector("#kn-chunks");
    if (!box || !state.chunk.id) return;
    const current = ++seq;
    box.innerHTML = "";
    box.appendChild(el("p", "knowledge-empty", "正在读取切块…"));
    try {
      const params = new URLSearchParams({
        offset: String(offset),
        limit: String(state.chunk.limit),
      });
      if (state.chunk.query) params.set("q", state.chunk.query);
      const payload = await api.get(
        `/api/library/${encodeURIComponent(state.chunk.id)}/chunks?${params}`
      );
      if (current !== seq || !root) return;
      state.chunk.offset = payload.offset || 0;
      state.chunk.total = payload.total || 0;
      box.innerHTML = "";
      if (!payload.items || !payload.items.length) {
        box.appendChild(
          el("p", "knowledge-empty", payload.reason || "这一份在索引里还没有切块。")
        );
        return;
      }
      for (const item of payload.items) {
        const card = el("div", "knowledge-chunk");
        const head = el("div", "knowledge-chunk-head");
        head.appendChild(el("span", "", `#${item.no}`));
        if (item.section) head.appendChild(el("span", "", item.section));
        if (item.page) {
          head.appendChild(
            el("span", "", item.pageEnd && item.pageEnd !== item.page ? `p.${item.page}–${item.pageEnd}` : `p.${item.page}`)
          );
        }
        head.appendChild(el("span", "", `${formatNumber(item.chars)} 字`));
        card.appendChild(head);
        card.appendChild(el("div", "knowledge-chunk-text", item.text));
        box.appendChild(card);
      }
      const pager = el("div", "knowledge-pager");
      const from = state.chunk.offset + 1;
      const to = state.chunk.offset + payload.items.length;
      pager.appendChild(
        el("span", "knowledge-muted", `${from}–${to} / ${formatNumber(state.chunk.total)} 块`)
      );
      const prev = el("button", "", "上一页");
      prev.type = "button";
      prev.disabled = state.chunk.offset <= 0;
      prev.addEventListener("click", () => loadChunks(Math.max(0, state.chunk.offset - state.chunk.limit)));
      const next = el("button", "", "下一页");
      next.type = "button";
      next.disabled = to >= state.chunk.total;
      next.addEventListener("click", () => loadChunks(state.chunk.offset + state.chunk.limit));
      pager.append(prev, next);
      box.appendChild(pager);
    } catch (error) {
      if (current !== seq || !root) return;
      box.innerHTML = "";
      box.appendChild(el("p", "knowledge-empty", `读取切块失败：${error.message}`));
    }
  }

  // ---- 检索 --------------------------------------------------------------

  function rankChips(ranks) {
    const box = el("div", "knowledge-ranks");
    for (const name of ["fts", "cjk", "vec"]) {
      if (ranks[name] === undefined) continue;
      const text = name === "vec" ? `语义 ${ranks[name]}` : `${name.toUpperCase()} ${ranks[name]}`;
      box.appendChild(el("span", `knowledge-rank${name === "vec" ? " is-vec" : ""}`, text));
    }
    return box;
  }

  async function runSearch() {
    const term = root.querySelector("#kn-search-q").value.trim();
    if (!term) return;
    const mode = root.querySelector("#kn-mode").value;
    const limit = root.querySelector("#kn-limit").value;
    const box = root.querySelector("#kn-results");
    const diag = root.querySelector("#kn-diag");
    box.innerHTML = "";
    box.appendChild(el("p", "knowledge-empty", "正在检索…"));
    diag.innerHTML = "";
    const started = performance.now();
    try {
      const payload = await api.get(
        `/api/knowledge/search?q=${encodeURIComponent(term)}&mode=${encodeURIComponent(mode)}&limit=${encodeURIComponent(limit)}`
      );
      const elapsed = Math.round(performance.now() - started);
      box.innerHTML = "";
      const corpus = payload.corpus || [];
      const memory = payload.memory || [];
      if (!corpus.length && !memory.length) {
        box.appendChild(el("p", "knowledge-empty", `没有命中「${payload.query}」。`));
      }
      for (const item of corpus) {
        const card = el("div", "knowledge-result");
        const head = el("div", "knowledge-result-head");
        head.appendChild(el("span", "knowledge-result-title", item.title || item.ref));
        head.appendChild(el("span", "knowledge-result-ref", item.ref));
        card.appendChild(head);
        card.appendChild(rankChips(item.ranks || {}));
        card.appendChild(el("div", "knowledge-result-text", item.text));
        box.appendChild(card);
      }
      if (memory.length) {
        box.appendChild(el("h4", "knowledge-detail-block", "记忆"));
        for (const item of memory) {
          const card = el("div", "knowledge-result");
          const head = el("div", "knowledge-result-head");
          head.appendChild(el("span", "knowledge-result-title", MEMORY_KIND_TEXT[item.kind] || item.kind));
          head.appendChild(el("span", "knowledge-result-ref", item.id));
          card.appendChild(head);
          card.appendChild(el("div", "knowledge-result-text", item.text));
          box.appendChild(card);
        }
      }
      const channels = payload.diagnostics.channels || {};
      const bits = [
        `词法 ${channels.fts || 0}`,
        `中文 ${channels.cjk || 0}`,
        `语义 ${channels.vec || 0}`,
        `向量覆盖 ${Math.round((payload.diagnostics.vectorCoverage || 0) * 100)}%`,
        `分组 ${payload.diagnostics.groups || 0}`,
        `候选 ${payload.diagnostics.hitsTotal || 0}`,
        `${elapsed} ms`,
      ];
      if (payload.diagnostics.reason) bits.push(`原因 ${payload.diagnostics.reason}`);
      diag.textContent = bits.join(" · ");
      // 使用记录只是统计，失败不影响这次检索的结果展示。
      api
        .post("/api/knowledge/usage", {
          query: term,
          mode,
          hits: corpus.length + memory.length,
          ms: elapsed,
          empty: corpus.length + memory.length === 0,
        })
        .catch(() => {});
    } catch (error) {
      box.innerHTML = "";
      box.appendChild(el("p", "knowledge-empty", `检索失败：${error.message}`));
    }
  }

  // ---- 评测与使用记录 ----------------------------------------------------

  function gateText(gate) {
    const title = GATE_TEXT[gate.name] || gate.name;
    if (gate.skipped) return `${gate.name}（${title}）无法判定`;
    let value = "";
    if (gate.name === "门禁 3") {
      value = ` p95 ${Math.round((gate.value || 0) * 1000)}ms`;
    } else if (typeof gate.value === "number") {
      value = ` ${gate.value >= 0 ? "+" : ""}${gate.value.toFixed(3)}`;
    }
    return `${gate.name}（${title}）${value} → ${gate.ok ? "通过" : "未通过"}`;
  }

  async function loadAnalytics() {
    const box = root.querySelector("#kn-analytics");
    if (!box) return;
    try {
      const payload = await api.get("/api/knowledge/analytics");
      if (!root) return;
      if (!payload.count) {
        box.textContent = "还没有搜索记录";
        return;
      }
      const modes = payload.modes || {};
      const modeText = ["hybrid", "lexical", "semantic"]
        .filter((name) => modes[name])
        .map((name) => `${MODE_TEXT[name] || name} ${modes[name]}`)
        .join(" / ");
      box.textContent =
        `${formatNumber(payload.count)} 次检索 · p95 ${payload.p95}ms · `
        + `均值 ${payload.mean}ms · 空结果 ${Math.round((payload.emptyRate || 0) * 100)}%`
        + (modeText ? ` · ${modeText}` : "");
    } catch (error) {
      if (root) box.textContent = `使用记录读取失败：${error.message}`;
    }
  }

  function evaluationRow(job) {
    const row = el("button", "knowledge-eval-row");
    row.type = "button";
    const head = el("div", "knowledge-eval-row-head");
    head.appendChild(statusBadge(job.status));
    head.appendChild(el("span", "knowledge-eval-time", formatTime(job.createdAt)));
    const result = job.result;
    if (result && Array.isArray(result.gates) && result.gates.length) {
      head.appendChild(
        el("span", "knowledge-eval-verdict", result.ok ? "三条门禁全过" : "有门禁未通过")
      );
    }
    row.appendChild(head);
    let detail = "";
    if (job.status === "running") {
      detail = job.stage || "正在跑";
    } else if (job.error) {
      detail = job.error;
    } else if (result && Array.isArray(result.gates)) {
      detail = result.gates.map(gateText).join(" · ");
    }
    if (detail) row.appendChild(el("div", "knowledge-eval-row-detail", detail));
    row.addEventListener("click", () => openEvaluation(job.id));
    return row;
  }

  async function loadEvaluation() {
    const box = root.querySelector("#kn-eval-body");
    if (!box) return;
    try {
      const payload = await api.get("/api/knowledge/evaluation/runs");
      if (!root) return;
      const jobs = payload.jobs || [];
      box.innerHTML = "";
      if (!jobs.length) {
        box.appendChild(el("p", "knowledge-empty", "还没有跑过评测。"));
        return;
      }
      for (const job of jobs.slice(0, 8)) box.appendChild(evaluationRow(job));
    } catch (error) {
      if (!root) return;
      box.innerHTML = "";
      box.appendChild(el("p", "knowledge-empty", `读取评测记录失败：${error.message}`));
    }
  }

  async function openEvaluation(id) {
    const current = ++seq;
    panel.setTitle("正在读取评测…");
    panel.setSubtitle(id);
    panel.setBody("");
    panel.open();
    try {
      const job = await api.get(`/api/knowledge/evaluation/runs/${encodeURIComponent(id)}`);
      if (current !== seq || !root) return;
      panel.setTitle("检索评测");
      panel.setSubtitle(`${id} · ${formatTime(job.createdAt)}`);
      const body = document.createDocumentFragment();
      const progress = job.progress || {};
      body.appendChild(
        detailBlock(
          "任务",
          keyValues([
            ["状态", job.status],
            ["阶段", job.stage],
            ["进度", `${progress.done || 0} / ${progress.total || 0}`],
            ["失败原因", job.error],
            ["参数", JSON.stringify(job.meta || {})],
          ])
        )
      );
      const result = job.result;
      if (!result) {
        panel.setBody(body);
        return;
      }
      const gates = el("ul");
      for (const gate of result.gates || []) {
        gates.appendChild(el("li", gate.ok ? "" : "knowledge-bad", gateText(gate)));
      }
      body.appendChild(detailBlock("门禁", gates));

      const corpus = result.corpus || {};
      body.appendChild(
        detailBlock(
          "语料",
          keyValues([
            ["模型", corpus.model],
            ["源", corpus.sources],
            ["块", corpus.chunks],
            ["向量覆盖", corpus.coverage === undefined ? "" : `${Math.round(corpus.coverage * 100)}%`],
            ["语义通道", corpus.semanticMissing],
            ["评测集", `${result.questions?.count || 0} 条`],
          ])
        )
      );

      const combos = el("ul");
      for (const [label, item] of Object.entries(result.combos || {})) {
        combos.appendChild(
          el(
            "li",
            "",
            `${label}：recall@5 ${item.recall.toFixed(3)} · MRR ${item.mrr.toFixed(3)} · `
              + `引用命中 ${item.citation.toFixed(3)}`
          )
        );
      }
      const group = result.group;
      if (group) {
        combos.appendChild(
          el(
            "li",
            "",
            `组级：标注源命中 ${group.recall.toFixed(3)} · 答案在片段里 `
              + `${group.answer.toFixed(3)} · 平均 ${group.chars} 字`
          )
        );
      }
      if (result.rerank) {
        combos.appendChild(
          el(
            "li",
            "",
            result.rerank.reason
              ? `精排：${result.rerank.reason}`
              : `精排后：标注源命中 ${result.rerank.recall.toFixed(3)}`
                  + `（${result.rerank.gainRecall >= 0 ? "+" : ""}${result.rerank.gainRecall.toFixed(3)}）`
                  + ` · 答案在片段里 ${result.rerank.answer.toFixed(3)}`
                  + `（${result.rerank.gainAnswer >= 0 ? "+" : ""}${result.rerank.gainAnswer.toFixed(3)}）`
          )
        );
      }
      body.appendChild(detailBlock("读数", combos));

      const latency = result.latency || {};
      body.appendChild(
        detailBlock(
          "耗时与体积",
          keyValues([
            ["热态 p95", `${Math.round((latency.p95 || 0) * 1000)}ms`],
            ["均值", `${Math.round((latency.mean || 0) * 1000)}ms`],
            ["首次（含加载模型）", `${Math.round((latency.cold || 0) * 1000)}ms`],
            ["依赖", `${Math.round((result.footprint?.packages || 0) / 1024 / 1024)}MB`],
            ["模型", `${Math.round((result.footprint?.model || 0) / 1024 / 1024)}MB`],
          ])
        )
      );
      panel.setBody(body);
    } catch (error) {
      if (current !== seq || !root) return;
      panel.setTitle("读取失败");
      panel.setBody(el("p", "knowledge-empty", error.message));
    }
  }

  async function startEvaluation(button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "正在启动…";
    try {
      const payload = await api.post("/api/knowledge/evaluation/runs", {});
      button.textContent = "正在跑";
      await waitEvaluation(payload.jobId, button);
    } catch (error) {
      say(`起评测失败：${error.message}`, "bad");
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function waitEvaluation(jobId, button) {
    const original = "跑一次评测";
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!root) return;
      let job = null;
      try {
        job = await api.get(`/api/knowledge/evaluation/runs/${encodeURIComponent(jobId)}`);
      } catch (error) {
        say(`评测记录读不到：${error.message}`, "bad");
        break;
      }
      if (!job || job.status !== "running") break;
      button.textContent = job.stage ? `正在跑 · ${job.stage}` : "正在跑";
    }
    button.disabled = false;
    button.textContent = original;
    await loadEvaluation();
    await loadAnalytics();
  }

  // ---- 记忆 --------------------------------------------------------------

  function memoryQuery() {
    const params = new URLSearchParams();
    const q = root.querySelector("#kn-mem-q").value.trim();
    const kind = root.querySelector("#kn-mem-kind").value;
    const stateText = root.querySelector("#kn-mem-state").value;
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    if (stateText) params.set("state", stateText);
    const text = params.toString();
    return text ? `?${text}` : "";
  }

  async function loadMemory() {
    const current = ++seq;
    const box = root.querySelector("#kn-mem-list");
    box.innerHTML = "";
    box.appendChild(el("p", "knowledge-empty", "正在读取记忆…"));
    try {
      const payload = await api.get(`/api/memory${memoryQuery()}`);
      if (current !== seq || !root) return;
      state.memory = payload.entries || [];
      box.innerHTML = "";
      if (!state.memory.length) {
        box.appendChild(el("p", "knowledge-empty", "没有匹配的记忆。"));
        return;
      }
      for (const item of state.memory) box.appendChild(memoryCard(item));
      const counts = payload.counts || {};
      const byKind = counts.byKind || {};
      root.querySelector("#kn-counts").textContent =
        `${formatNumber(counts.live || 0)} 条有效 · ${formatNumber(counts.archived || 0)} 条归档 · `
        + `事实 ${byKind.fact || 0} / 习惯 ${byKind.preference || 0} / 经验 ${byKind.experience || 0}`;
    } catch (error) {
      if (current !== seq || !root) return;
      box.innerHTML = "";
      box.appendChild(el("p", "knowledge-empty", `读取记忆失败：${error.message}`));
    }
  }

  function memoryCard(item) {
    const card = el("div", "knowledge-memory-item");
    const head = el("div", "knowledge-memory-head");
    head.appendChild(el("span", "knowledge-result-title", MEMORY_KIND_TEXT[item.kind] || item.kind));
    head.appendChild(statusBadge(item.state, MEMORY_STATE_TEXT[item.state]));
    if (item.unverified) head.appendChild(statusBadge("expired", "引用未确认"));
    card.appendChild(head);
    card.appendChild(el("div", "knowledge-memory-text", item.text));
    if (item.refs && item.refs.length) {
      card.appendChild(el("div", "knowledge-memory-refs", item.refs.join("、")));
    }
    card.addEventListener("click", () => openMemory(item.id));
    return card;
  }

  async function openMemory(id) {
    const current = ++seq;
    panel.setTitle("正在读取…");
    panel.setSubtitle(id);
    panel.setBody("");
    panel.open();
    try {
      const payload = await api.get(`/api/memory/${encodeURIComponent(id)}`);
      if (current !== seq || !root) return;
      panel.setTitle(MEMORY_KIND_TEXT[payload.kind] || payload.kind);
      panel.setSubtitle(payload.id);
      const body = document.createDocumentFragment();
      const text = el("p", "", payload.text);
      body.appendChild(detailBlock("正文", text));
      const stateLine = el("p");
      stateLine.appendChild(statusBadge(payload.state, MEMORY_STATE_TEXT[payload.state]));
      body.appendChild(detailBlock("状态", stateLine));
      body.appendChild(
        detailBlock(
          "引用",
          keyValues([["文件", (payload.refs || []).join("、")], ["来源", payload.source]])
        )
      );
      const chain = [];
      if (payload.supersededBy) chain.push(`被 ${payload.supersededBy} 取代`);
      if (payload.supersedes && payload.supersedes.length) {
        chain.push(`取代了 ${payload.supersedes.join("、")}`);
      }
      if (payload.conflicts && payload.conflicts.length) {
        chain.push(`与 ${payload.conflicts.join("、")} 冲突（未解决）`);
      }
      if (chain.length) {
        const list = el("ul");
        for (const line of chain) list.appendChild(el("li", "", line));
        body.appendChild(detailBlock("版本与冲突", list));
      }
      body.appendChild(
        detailBlock(
          "时间",
          keyValues([
            ["创建", formatTime(payload.createdAt)],
            ["更新", formatTime(payload.updatedAt)],
            ["最近使用", formatTime(payload.lastUsedAt)],
            ["到期", formatTime(payload.expiresAt)],
          ])
        )
      );
      const actions = el("div", "knowledge-toolbar");
      const button = el("button", "is-primary", payload.archived ? "从归档恢复" : "归档这一条");
      button.type = "button";
      button.addEventListener("click", () => memoryAction(payload.id, payload.archived ? "restore" : "archive", button));
      actions.appendChild(button);
      body.appendChild(detailBlock("操作", actions));
      panel.setBody(body);
    } catch (error) {
      if (current !== seq || !root) return;
      panel.setTitle("读取失败");
      panel.setBody(el("p", "knowledge-empty", error.message));
    }
  }

  async function memoryAction(id, action, button) {
    button.disabled = true;
    try {
      await api.post(`/api/memory/${encodeURIComponent(id)}/${action}`, {});
      say(action === "archive" ? "已归档，可以在「已归档」里找回。" : "已恢复。");
      panel.close();
      await loadMemory();
    } catch (error) {
      say(`操作失败：${error.message}`, "warn");
    } finally {
      button.disabled = false;
    }
  }

  // ---- 上传与入库 --------------------------------------------------------

  async function upload(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    const form = new FormData();
    for (const file of list) form.append("file", file, file.name);
    try {
      const payload = await api.request("/api/library/upload", {
        method: "POST",
        body: form,
        timeout: 120000,
      });
      const saved = (payload.saved || []).filter((item) => item.saved).map((item) => item.name);
      say(`已投递 ${saved.length} 份：${saved.join("、")}。${payload.next || ""}`);
      await loadCorpus();
    } catch (error) {
      say(`上传失败：${error.message}`, "warn");
    }
  }

  function updateIngestButton(payload) {
    const button = root.querySelector("#kn-ingest");
    const stateText = payload.ingest || {};
    const busy = Boolean(stateText.running);
    const inbox = (payload.inbox || {}).count || 0;
    button.disabled = busy || !payload.can_ingest || !inbox;
    button.textContent = busy
      ? `入库中 ${stateText.done || 0}/${stateText.total || 0}`
      : inbox
        ? `入库 ${inbox} 份`
        : "入库";
    if (!payload.can_ingest) button.title = "这个进程不负责入库，用应用启动后再点";
  }

  async function startIngest() {
    const button = root.querySelector("#kn-ingest");
    button.disabled = true;
    try {
      const payload = await api.post("/api/library/ingest", {});
      say(`开始入库 ${payload.started} 份，解析、切块、算向量要一会儿…`);
      await waitIngest(payload.jobId);
    } catch (error) {
      say(`入库失败：${error.message}`, "warn");
      button.disabled = false;
    }
  }

  function ingestSummary(job) {
    const results = (job.result || {}).results || [];
    const added = results.filter((item) => item.status === "new").length;
    const duplicate = results.filter((item) => item.status === "duplicate").length;
    const failed = results.filter((item) => item.status === "failed").length;
    return `新增 ${added}、重复 ${duplicate}、失败 ${failed}`;
  }

  async function waitIngest(jobId) {
    for (let round = 0; round < 900; round += 1) {
      let job = null;
      try {
        job = jobId
          ? await api.get(`/api/ingestion/jobs/${encodeURIComponent(jobId)}`)
          : ((await api.get("/api/ingestion/jobs")).jobs || [])[0];
      } catch (error) {
        say(`读入库进度失败：${error.message}`, "warn");
        return;
      }
      if (!root) return;
      if (job) {
        const progress = job.progress || {};
        root.querySelector("#kn-ingest").textContent =
          job.status === "running"
            ? `入库中 ${progress.done || 0}/${progress.total || 0}${job.stage ? ` · ${job.stage}` : ""}`
            : "入库";
        if (job.status !== "running") {
          if (job.status === "done") {
            say(`入库结束：${ingestSummary(job)}。`);
          } else {
            say(`入库${job.status === "failed" ? "失败" : "中断"}：${job.error || ""}`, "warn");
          }
          await loadCorpus();
          return;
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
    say("入库还在跑，刷新页面可以看结果。", "warn");
  }

  async function batchDelete() {
    const ids = [...state.selected];
    if (!ids.length) return;
    const button = root.querySelector("#kn-batch");
    button.disabled = true;
    try {
      const payload = await api.post("/api/library/batch/delete", { ids });
      const done = (payload.results || []).filter((item) => item.deleted).length;
      say(`已挪进回收站 ${done} 份，可以找回。`);
      await loadCorpus();
    } catch (error) {
      say(`删除失败：${error.message}`, "warn");
    } finally {
      button.disabled = state.selected.size === 0;
    }
  }

  // ---- 改名 --------------------------------------------------------------

  let renameTarget = null;

  function openRename(item) {
    renameTarget = item;
    const dialog = root.querySelector("#kn-rename");
    const input = root.querySelector("#kn-rename-input");
    input.value = item.displayTitle || item.title || "";
    dialog.showModal();
    input.focus();
    input.select();
  }

  async function renameTo(value) {
    const item = renameTarget;
    if (!item) return;
    // 只写 `labels.json` 的显示名：原件与索引一概不碰，块不会因此重建。
    const payload = await api.post(`/api/library/${encodeURIComponent(item.id)}/rename`, {
      title: value,
    });
    say(value.trim() ? `已改名为「${payload.title}」。` : "已恢复原始标题。");
    await loadCorpus();
    if (!root) return;
    panel.setTitle(payload.title || item.id);
    const row = root.querySelector(`#kn-rows tr[data-id="${item.id}"]`);
    if (row) await openSource(item.id, row);
  }

  // ---- 挂载 --------------------------------------------------------------

  async function mount(container) {
    container.innerHTML = TEMPLATE;
    root = container;
    panel = detailPanel();
    container.appendChild(panel.element);

    for (const tab of container.querySelectorAll(".knowledge-tab")) {
      tab.addEventListener("click", () => setTab(tab.dataset.tab));
    }

    // 语料
    let corpusTimer = 0;
    const requery = () => {
      window.clearTimeout(corpusTimer);
      corpusTimer = window.setTimeout(() => loadCorpus(), 200);
    };
    container.querySelector("#kn-corpus-q").addEventListener("input", requery);
    for (const id of ["#kn-kind", "#kn-status", "#kn-sort"]) {
      container.querySelector(id).addEventListener("change", () => loadCorpus());
    }
    container.querySelector("#kn-refresh").addEventListener("click", () => {
      quiet();
      loadCorpus();
    });
    container.querySelector("#kn-check-all").addEventListener("change", (event) => {
      const checked = event.target.checked;
      state.selected = new Set();
      for (const row of container.querySelectorAll("#kn-rows tr")) {
        const box = row.querySelector('input[type="checkbox"]');
        box.checked = checked;
        row.classList.toggle("is-selected", checked);
        if (checked) state.selected.add(row.dataset.id);
      }
      container.querySelector("#kn-batch").disabled = state.selected.size === 0;
    });
    container.querySelector("#kn-batch").addEventListener("click", batchDelete);
    container.querySelector("#kn-upload").addEventListener("click", () =>
      container.querySelector("#kn-file").click()
    );
    container.querySelector("#kn-file").addEventListener("change", async (event) => {
      await upload(event.target.files);
      event.target.value = "";
    });
    container.querySelector("#kn-ingest").addEventListener("click", startIngest);

    // 检索
    container.querySelector("#kn-search-go").addEventListener("click", runSearch);
    container.querySelector("#kn-search-q").addEventListener("keydown", (event) => {
      if (event.key === "Enter") runSearch();
    });
    container.querySelector("#kn-eval-refresh").addEventListener("click", () => {
      loadEvaluation();
      loadAnalytics();
    });
    container.querySelector("#kn-eval-run").addEventListener("click", (event) =>
      startEvaluation(event.currentTarget)
    );

    // 记忆
    let memoryTimer = 0;
    const rememory = () => {
      window.clearTimeout(memoryTimer);
      memoryTimer = window.setTimeout(() => loadMemory(), 200);
    };
    container.querySelector("#kn-mem-q").addEventListener("input", rememory);
    for (const id of ["#kn-mem-kind", "#kn-mem-state"]) {
      container.querySelector(id).addEventListener("change", () => loadMemory());
    }
    container.querySelector("#kn-mem-refresh").addEventListener("click", () => loadMemory());

    // 改名对话框。两个按钮都是普通按钮，提交由这里发请求：失败时对话框不关，
    // 用户填的字还在，改一下再点保存即可。
    const renameDialog = container.querySelector("#kn-rename");
    const renameSave = container.querySelector("#kn-rename-ok");
    container.querySelector("#kn-rename-cancel").addEventListener("click", () =>
      renameDialog.close()
    );
    container.querySelector("#kn-rename-form").addEventListener("submit", (event) => {
      event.preventDefault();
      renameSave.click();
    });
    renameSave.addEventListener("click", async () => {
      const input = container.querySelector("#kn-rename-input");
      renameSave.disabled = true;
      try {
        await renameTo(input.value);
        renameDialog.close();
      } catch (error) {
        say(`改名失败：${error.message}`, "warn");
      } finally {
        renameSave.disabled = false;
      }
    });

    document.addEventListener("keydown", onKey);
    await loadCorpus();
    return {
      unmount() {
        seq += 1;
        document.removeEventListener("keydown", onKey);
        root = null;
        panel = null;
      },
    };
  }

  function onKey(event) {
    if (event.key !== "Escape") return;
    // 改名对话框开着时 Esc 只关对话框：一起把抽屉也关掉的话，用户刚要改的那份
    // 就从视野里消失了。
    const dialog = root && root.querySelector("#kn-rename");
    if (dialog && dialog.open) return;
    if (panel && panel.isOpen()) panel.close();
  }

  window.vsPages.knowledge = { mount };
})();
