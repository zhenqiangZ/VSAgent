// 工作空间首页：默认页。只做三件事——发现任务、恢复任务、看哪一条还在跑。
//
// 列表不轮询整表：解析状态文件要半秒（6.6MB），运行中的行只拉 live 快照（几 KB）。
// 某一行从「运行中」变成别的状态时，才重取一次整表，那时状态文件刚写完，正好读到
// 新封存的主题。

(function () {
  const { fetchJson, formatNumber, formatTime } = window.vsWeb;

  const TEMPLATE = `
    <div class="home">
      <aside class="home-side">
        <div class="home-brand">VSAgent</div>
        <div class="home-side-label">工作空间</div>
        <div id="home-running" class="home-running"></div>
        <div class="home-side-label">全部任务</div>
        <ul id="home-side-list" class="home-side-list"></ul>
      </aside>
      <main class="home-main">
        <section class="home-hero">
          <h1>把一件事交给它</h1>
          <p>查靶点、取结构、准备受体、对接、打分。过程按主题分段记录，产物留在右侧。</p>
        </section>
        <div id="home-composer" class="home-composer"></div>
        <section class="home-panel">
          <div class="home-panel-head">
            <span>最近任务</span>
            <a href="#/history">打开会话历史</a>
          </div>
          <div id="home-tasks"></div>
        </section>
      </main>
    </div>
  `;

  // 与 Chat 左栏同一套词。`idle` 是刚建出来还没跑过的会话。
  const STATUS_TEXT = {
    idle: "待开始",
    running: "运行中",
    done: "已完成",
    failed: "失败",
    cancelled: "已中断",
  };

  const POLL_INTERVAL = 5000;

  let root = null;
  let sessions = [];
  let timer = null;
  // 换页时旧请求可能还在路上；回来时页面对不上就不画。
  let token = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function relativeTime(seconds) {
    if (!seconds) return "—";
    const gap = Date.now() / 1000 - seconds;
    if (gap < 60) return "刚刚";
    if (gap < 3600) return `${Math.floor(gap / 60)} 分钟前`;
    if (gap < 86400) return `${Math.floor(gap / 3600)} 小时前`;
    if (gap < 86400 * 7) return `${Math.floor(gap / 86400)} 天前`;
    return formatTime(seconds).slice(0, 10);
  }

  function statusBadge(status) {
    const value = STATUS_TEXT[status] ? status : "idle";
    return el("span", `home-status status-${value}`, STATUS_TEXT[value]);
  }

  // 任务按「在跑的排前面，其余按更新时间倒序」排。服务端已经按时间倒序给过一次，
  // 这里只把运行中的挑到前面。
  function ordered() {
    const roots = sessions.filter((item) => !item.parentId);
    const running = roots.filter((item) => item.status === "running");
    const rest = roots.filter((item) => item.status !== "running");
    return running.concat(rest);
  }

  function liveLine(item) {
    const data = item.live;
    if (!data || !data.running) return "";
    const step = (data.steps ? data.steps.length : 0) + 1;
    const title = data.title || "正在进行的主题";
    return `第 ${data.topicIndex || 0} 个主题 · ${title} · 第 ${step} 步`;
  }

  function openTask(item, eventId) {
    const suffix = eventId ? `?event=${encodeURIComponent(eventId)}` : "";
    window.location.hash = `#/history/${encodeURIComponent(item.id)}${suffix}`;
  }

  // 左栏：一行一个任务，只放标题与状态。这里是导航，详细信息在右边那张表里。
  function renderSide(list) {
    const box = root.querySelector("#home-side-list");
    const runningBox = root.querySelector("#home-running");
    box.innerHTML = "";
    runningBox.innerHTML = "";

    const running = list.filter((item) => item.status === "running");
    if (running.length) {
      for (const item of running) runningBox.appendChild(sideRow(item, true));
    } else {
      runningBox.appendChild(el("div", "home-empty-line", "没有正在跑的任务"));
    }

    for (const item of list) {
      if (item.status === "running") continue;
      box.appendChild(sideRow(item, false));
    }
    if (!list.length) {
      box.appendChild(el("li", "home-empty-line", "还没有任务"));
    }
  }

  function sideRow(item, withLive) {
    const row = el("button", "home-side-row");
    row.type = "button";
    row.dataset.task = item.id;
    row.appendChild(el("span", "home-side-title", item.title || "（没有标题）"));
    row.appendChild(statusBadge(item.status));
    if (withLive) {
      const line = el("span", "home-side-live");
      line.dataset.liveFor = item.id;
      line.textContent = liveLine(item);
      row.appendChild(line);
    }
    row.addEventListener("click", () => openTask(item, item.lastEvent && item.lastEvent.id));
    return row;
  }

  // 主区：最近任务表。一行给到「最近一个主题」，点进去直接落在那一章。
  function renderTable(list) {
    const box = root.querySelector("#home-tasks");
    box.innerHTML = "";
    if (!list.length) {
      box.appendChild(
        el("p", "home-empty", "还没有任务。在终端里开一轮，跑过的任务会出现在这里。")
      );
      return;
    }

    const table = el("table", "home-table");
    const head = el("thead");
    const headRow = el("tr");
    for (const [label, name] of [
      ["任务", "task"],
      ["状态", "status"],
      ["主题", "topics"],
      ["产物", "artifacts"],
      ["更新", "updated"],
    ]) {
      headRow.appendChild(el("th", `home-col-${name}`, label));
    }
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el("tbody");
    for (const item of list.slice(0, 10)) {
      const row = el("tr", "home-task-row");
      row.dataset.task = item.id;
      row.tabIndex = 0;

      const name = el("td", "home-col-task");
      name.appendChild(el("div", "home-task-title", item.title || "（没有标题）"));
      const sub = el("div", "home-task-sub");
      if (item.status === "running") {
        const live = el("span", "home-task-live");
        live.dataset.liveFor = item.id;
        live.textContent = liveLine(item);
        sub.appendChild(live);
      } else if (item.lastEvent) {
        sub.appendChild(
          el(
            "span",
            "",
            `主题 ${String(item.lastEvent.topicIndex || 0).padStart(2, "0")} · ${item.lastEvent.title || "（没有标题）"}`
          )
        );
      } else {
        sub.appendChild(el("span", "", "还没有封存的主题"));
      }
      name.appendChild(sub);

      row.appendChild(name);
      const status = el("td", "home-col-status");
      status.appendChild(statusBadge(item.status));
      row.appendChild(status);
      row.appendChild(el("td", "home-col-topics", formatNumber(item.eventCount)));
      row.appendChild(el("td", "home-col-artifacts", formatNumber(item.artifactCount)));
      const updated = el("td", "home-col-updated", relativeTime(item.updatedAt));
      updated.title = formatTime(item.updatedAt);
      row.appendChild(updated);

      row.addEventListener("click", () => openTask(item, item.lastEvent && item.lastEvent.id));
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openTask(item, item.lastEvent && item.lastEvent.id);
      });
      body.appendChild(row);
    }
    table.appendChild(body);
    box.appendChild(table);
  }

  function render() {
    const list = ordered();
    renderSide(list);
    renderTable(list);
  }

  // 只改运行中那几行的实时文字，不重画整页：重画会打断悬停与键盘焦点。
  function patchLive(data) {
    const step = (data.steps ? data.steps.length : 0) + 1;
    const text = `第 ${data.topicIndex || 0} 个主题 · ${data.title || "正在进行的主题"} · 第 ${step} 步`;
    for (const node of root.querySelectorAll(`[data-live-for="${data.sessionId}"]`)) {
      node.textContent = text;
    }
  }

  async function pollRunning() {
    if (document.visibilityState !== "visible") return;
    const running = sessions.filter((item) => item.status === "running");
    if (!running.length) return;

    let finished = false;
    for (const item of running) {
      let data = null;
      try {
        data = await fetchJson(`/api/sessions/${encodeURIComponent(item.id)}/live`);
      } catch (error) {
        // 单行取不到就跳过这一轮，其它行照常更新；下一轮再试。
        continue;
      }
      if (!data || !data.present) continue;
      if (!data.running) {
        finished = true;
        continue;
      }
      item.live = data;
      patchLive(data);
    }
    if (finished) await loadSessions();
  }

  function startPolling() {
    stopPolling();
    timer = window.setInterval(pollRunning, POLL_INTERVAL);
  }

  function stopPolling() {
    if (timer) window.clearInterval(timer);
    timer = null;
  }

  async function loadSessions() {
    const current = token;
    const payload = await fetchJson("/api/sessions");
    if (current !== token) return;
    sessions = payload.sessions || [];
    render();
  }

  async function mount(container) {
    container.innerHTML = TEMPLATE;
    root = container;
    token += 1;
    sessions = [];

    const composer = window.vsComposer.createComposer({
      quick: true,
      // 提交成功就进这个任务的会话页：进度、主题与产物都在那一页，
      // 留在首页只能看到一个刚变成「运行中」的行。
      onSubmitted(sessionId) {
        window.location.hash = `#/history/${encodeURIComponent(sessionId)}`;
      },
    });
    container.querySelector("#home-composer").appendChild(composer.element);

    try {
      await loadSessions();
    } catch (error) {
      const box = container.querySelector("#home-tasks");
      box.innerHTML = "";
      box.appendChild(el("p", "home-empty", `读取任务列表失败：${error.message}`));
    }
    startPolling();

    const onVisible = () => {
      if (document.visibilityState === "visible") pollRunning();
    };
    document.addEventListener("visibilitychange", onVisible);

    return {
      unmount() {
        document.removeEventListener("visibilitychange", onVisible);
        stopPolling();
        root = null;
        token += 1;
      },
    };
  }

  window.vsPages.home = { mount };
})();
