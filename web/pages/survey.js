// 结构全景页面：一个靶点的全部实验结构，按配体分组。
//
// 数据来自 `survey_structures` 写的 `~/.vsagent/surveys/<id>/survey.json`，
// 由 `/api/survey/<id>` 原样转出来。页面只读：不下载坐标、不算口袋，
// 也不替读者判断哪个配体算「真配体」。
//
// 表里每一套结构都链到 RCSB 的条目页——这里给的是清单，
// 想看坐标与实验细节应当去源头。

(function () {
  const { fetchJson, formatNumber } = window.vsWeb;

  const RCSB_ENTRY = "https://www.rcsb.org/structure/";

  const TEMPLATE = `
    <div class="survey">
      <div class="lib-head">
        <div class="lib-stats" id="survey-stats">正在读取…</div>
        <div class="lib-actions">
          <a class="nav-link" id="survey-refresh" href="#">刷新</a>
        </div>
      </div>
      <div class="lib-message" id="survey-message" hidden></div>

      <section class="survey-section">
        <h3>配体组</h3>
        <p class="survey-note">
          ★ 表示这套结构的标题里点了这个配体的名。膜蛋白结构里胆固醇、磷脂、
          去垢剂按套数永远最多，所以标题点名的一组排在前面。一套结构含多个配体时
          会出现在多组，组内计数之和大于结构总数。
        </p>
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead>
              <tr><th>代号</th><th>名称</th><th>分子量</th><th>套数</th><th>最佳分辨率</th><th>结构编号</th></tr>
            </thead>
            <tbody id="survey-groups"></tbody>
          </table>
          <p class="empty-hint" id="survey-groups-empty" hidden>没有小分子配体组。</p>
        </div>
      </section>

      <section class="survey-section" id="survey-polymers-section" hidden>
        <h3>其他聚合物链</h3>
        <p class="survey-note">不属于本靶点的聚合物实体：毒素肽、抗体片段、辅助亚基、融合标签都算在这里。</p>
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead><tr><th>描述</th><th>UniProt</th><th>套数</th><th>结构编号</th></tr></thead>
            <tbody id="survey-polymers"></tbody>
          </table>
        </div>
      </section>

      <section class="survey-section" id="survey-additives-section" hidden>
        <h3>添加剂与离子</h3>
        <p class="survey-note">结晶添加剂、缓冲液、单原子离子、糖基化残基。按项目的口袋口径，它们不当配体看。</p>
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead><tr><th>代号</th><th>名称</th><th>套数</th></tr></thead>
            <tbody id="survey-additives"></tbody>
          </table>
        </div>
      </section>

      <section class="survey-section" id="survey-apo-section" hidden>
        <h3>无配体</h3>
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead><tr><th>编号</th><th>分辨率</th><th>方法</th></tr></thead>
            <tbody id="survey-apo"></tbody>
          </table>
        </div>
      </section>

      <section class="survey-section">
        <h3>全部结构</h3>
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead>
              <tr><th>编号</th><th>标题</th><th>分辨率</th><th>方法</th><th>小分子配体</th><th>其他链</th></tr>
            </thead>
            <tbody id="survey-entries"></tbody>
          </table>
          <p class="empty-hint" id="survey-entries-empty" hidden>没有取回任何结构。</p>
        </div>
      </section>

      <p class="lib-foot" id="survey-foot"></p>
    </div>
  `;

  let root = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function entryLink(pdbId) {
    const link = el("a", "survey-pdb", pdbId);
    link.href = RCSB_ENTRY + encodeURIComponent(pdbId);
    link.target = "_blank";
    link.rel = "noreferrer";
    return link;
  }

  function idCell(ids) {
    const cell = el("td", "survey-ids");
    (ids || []).forEach((pdbId, index) => {
      if (index > 0) cell.appendChild(document.createTextNode(" "));
      cell.appendChild(entryLink(pdbId));
    });
    return cell;
  }

  function resolution(value) {
    return value === null || value === undefined ? "—" : `${Number(value).toFixed(2)} Å`;
  }

  function say(text, kind = "warn") {
    const box = root.querySelector("#survey-message");
    box.textContent = text;
    box.className = `lib-message is-${kind}`;
    box.hidden = false;
  }

  function renderGroups(payload) {
    const body = root.querySelector("#survey-groups");
    const groups = payload.groups || [];
    if (!groups.length) {
      root.querySelector("#survey-groups-empty").hidden = false;
      return;
    }
    for (const group of groups) {
      const row = el("tr");
      const code = el("td", "survey-code");
      if (group.title_hits) {
        code.appendChild(el("span", "survey-star", "★ "));
      }
      code.appendChild(document.createTextNode(group.code));
      row.appendChild(code);

      const name = el("td", "survey-name", group.name || "—");
      if (group.title_hits) name.title = `标题里出现 ${group.title_hits} 次`;
      row.appendChild(name);

      row.appendChild(
        el("td", "lib-size", group.weight ? `${Number(group.weight).toFixed(1)} Da` : "—")
      );
      row.appendChild(el("td", "lib-kind", `${group.count} 套`));
      row.appendChild(el("td", "lib-size", resolution((group.best || {}).resolution)));
      row.appendChild(idCell(group.pdb_ids));
      body.appendChild(row);
    }
  }

  function renderPolymers(payload) {
    const rows = payload.polymers || [];
    if (!rows.length) return;
    root.querySelector("#survey-polymers-section").hidden = false;
    const body = root.querySelector("#survey-polymers");
    for (const row of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", "survey-name", row.description || "未命名链"));
      tr.appendChild(el("td", "lib-size", (row.uniprot_ids || []).join("、") || "—"));
      tr.appendChild(el("td", "lib-kind", `${row.count} 套`));
      tr.appendChild(idCell(row.pdb_ids));
      body.appendChild(tr);
    }
  }

  function renderAdditives(payload) {
    const rows = payload.additives || [];
    if (!rows.length) return;
    root.querySelector("#survey-additives-section").hidden = false;
    const body = root.querySelector("#survey-additives");
    for (const row of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", "survey-code", row.code));
      tr.appendChild(el("td", "survey-name", row.name || "—"));
      tr.appendChild(el("td", "lib-kind", `${row.count} 套`));
      body.appendChild(tr);
    }
  }

  function renderApo(payload) {
    const rows = payload.apo || [];
    if (!rows.length) return;
    root.querySelector("#survey-apo-section").hidden = false;
    const body = root.querySelector("#survey-apo");
    for (const row of rows) {
      const tr = el("tr");
      const first = el("td");
      first.appendChild(entryLink(row.pdb_id));
      tr.appendChild(first);
      tr.appendChild(el("td", "lib-size", resolution(row.resolution)));
      tr.appendChild(el("td", "lib-size", row.method || "—"));
      body.appendChild(tr);
    }
  }

  function renderEntries(payload) {
    const rows = payload.entries || [];
    if (!rows.length) {
      root.querySelector("#survey-entries-empty").hidden = false;
      return;
    }
    const body = root.querySelector("#survey-entries");
    for (const row of rows) {
      const tr = el("tr");
      const first = el("td");
      first.appendChild(entryLink(row.pdb_id));
      tr.appendChild(first);
      tr.appendChild(el("td", "survey-title", row.title || "—"));
      tr.appendChild(el("td", "lib-size", resolution(row.resolution)));
      tr.appendChild(el("td", "lib-size", row.method || "—"));
      tr.appendChild(el("td", "lib-size", (row.ligands || []).join("、") || "—"));
      tr.appendChild(el("td", "lib-size", (row.other_polymers || []).join("、") || "—"));
      body.appendChild(tr);
    }
  }

  function renderStats(payload) {
    const stats = root.querySelector("#survey-stats");
    const parts = [
      payload.accession || "未知靶点",
      `RCSB 命中 ${formatNumber(payload.total)} 套`,
      `取回 ${formatNumber(payload.fetched)} 套`,
      `配体组 ${formatNumber((payload.groups || []).length)}`,
      `无配体 ${formatNumber((payload.apo || []).length)}`,
    ];
    const query = payload.query || {};
    if (query.method) parts.push(`方法 ${query.method}`);
    if (query.ligand_class && query.ligand_class !== "any") {
      parts.push(`只看 ${query.ligand_class}`);
    }
    stats.textContent = parts.join(" · ");
  }

  function renderFoot(payload) {
    const foot = root.querySelector("#survey-foot");
    const notes = [
      "这是一份清单：不下载坐标、不算口袋、不核对序列，也没有判断哪个配体是真实药物位点。",
    ];
    if (payload.total > payload.fetched) {
      notes.push(
        `还有 ${formatNumber(payload.total - payload.fetched)} 套没有取明细，`
        + "在 TUI 里调大 limit 重跑一次可取。"
      );
    }
    if ((payload.missing || []).length) {
      notes.push(`明细没取到 ${payload.missing.length} 套：${payload.missing.join("、")}。`);
    }
    foot.textContent = notes.join(" ");
  }

  async function mount(container, surveyId) {
    container.innerHTML = TEMPLATE;
    root = container;
    const id = (surveyId || "").trim();
    if (!id) {
      root.querySelector("#survey-stats").textContent = "没有指定全景编号。";
      say("地址应当形如 #/survey/Q15858-survey-1a2b3c4d。在 TUI 里用 open_viewer 取地址。");
      return { unmount() { root = null; } };
    }

    root.querySelector("#survey-refresh").addEventListener("click", (event) => {
      event.preventDefault();
      mount(container, id);
    });

    try {
      const payload = await fetchJson(`/api/survey/${encodeURIComponent(id)}`);
      renderStats(payload);
      renderGroups(payload);
      renderPolymers(payload);
      renderAdditives(payload);
      renderApo(payload);
      renderEntries(payload);
      renderFoot(payload);
    } catch (error) {
      say(`读取结构全景失败：${error.message}`);
    }

    return { unmount() { root = null; } };
  }

  window.vsPages.survey = { mount };
})();
