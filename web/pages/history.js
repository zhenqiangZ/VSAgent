// 会话工作台：左边会话树与主题导航，中间按主题分章的对话，右边产物。
//
// 页面不再一次拉整份会话。事件清单与正文是两条接口，正文按事件取一段：
// 一个会话的消息单元可以上百个、工具输出动辄几万字，整份发给页面时切换主题与
// 滚动都要重排整棵树；分段之后每次只搬当前主题那一段。
//
// 主题的边界来自事件层记录的切点，与模型装配时读的是同一组位置，所以页面上
// 「这一步属于哪个主题」不会与上下文里的分段对不上。
//
// 三层折叠的顺序：主题卡（标题、状态、一句话结论）→ 工具执行（步时间线）→
// 单个工具（参数、原始输出、错误）。默认只展开最后一个主题，工具过程仍收着；
// 用户点开一个主题时，那一块的工具过程跟着打开——这是「我要查这一段」的信号。

(function () {
  const { ensureMarkdown, fetchJson, formatTime, formatNumber } = window.vsWeb;

  const TEMPLATE = `
    <div class="history">
      <aside class="history-list">
        <div class="list-tools">
          <input id="session-search" type="search" placeholder="按标题过滤">
          <button id="session-refresh">刷新</button>
        </div>
        <ul id="session-list"></ul>
      </aside>
      <section class="history-detail">
        <header id="session-head" class="is-hidden"></header>
        <div class="workbench">
          <nav id="topic-rail" aria-label="主题导航"></nav>
          <div class="conversation-column">
            <div id="conversation"></div>
            <div id="chat-composer" class="chat-composer is-hidden"></div>
          </div>
          <aside id="artifact-pane" aria-label="任务产物"></aside>
        </div>
        <section id="analysis-pane" class="analysis-pane is-hidden" role="tabpanel"></section>
      </section>
    </div>
  `;

  const STATUS_TEXT = {
    pending: "等待",
    awaiting: "待确认",
    running: "执行中",
    completed: "完成",
    failed: "失败",
  };

  // 主题的显示状态。`interrupted` 只有循环层记下了停止原因时才会出现；
  // 空状态（旧会话、还在跑）按「已完成」画，不给它编一个没发生过的结论。
  const TOPIC_STATUS = {
    completed: "已完成",
    failed: "失败",
    interrupted: "已中断",
    running: "进行中",
  };

  // 子 agent 的运行状态。与 TUI 右栏用词一致，颜色由 CSS 的 agent-dot 决定。
  const AGENT_STATUS = {
    idle: "空闲",
    running: "运行中",
    done: "完成",
    failed: "失败",
    cancelled: "已中断",
  };

  const AGENT_DOT = {
    idle: "dot-idle",
    running: "dot-running",
    done: "dot-done",
    failed: "dot-failed",
    cancelled: "dot-cancelled",
  };

  // 主题是怎么封存的。自动封存那一档要能看出来：它按轮末或预算切的段，
  // 与模型自己写的主题不是一个粒度，读的人不该把它当成规划好的阶段。
  const EVENT_KIND = {
    manual: "主题",
    auto: "轮末自动封存",
    local: "本地补记",
  };

  const ARTIFACT_KIND = {
    receptor: "受体",
    ligandset: "配体集",
    artifact: "产物",
  };

  const LIVE_INTERVAL = 2000;

  let sessions = [];
  let activeId = "";
  let keyword = "";
  let root = null;
  // 折叠的父会话编号。只在本次页面生命周期内有效；刷新页面回到全展开，
  // 免得昨天折叠过的那一条一直藏着新派出去的子 agent。
  const collapsed = new Set();

  // 当前打开的会话。
  let events = [];
  let task = null;
  let compactUpto = 0;
  let artifacts = null;
  const segments = new Map();
  // 用户手动展开 / 收起的主题。存进 localStorage：刷新之后还是他选的样子。
  // 没碰过的主题按「最后一个自动展开」处理，新主题出现时上一条自然收起。
  let manualOpen = new Set();
  let manualClosed = new Set();
  // 主题里的「工具执行」块与单个步。与主题的展开状态一起写进本地存储：
  // 用户翻到某一步的原始输出之后，刷新页面回来还在原地。
  let runOpen = new Set();
  let stepOpen = new Set();
  // 左栏主题条下面挂着的步。与正文的展开状态彼此独立：左栏是导航，正文是阅读。
  const railOpen = new Set();
  // 实时卡片里已经画过一次的步。按「会话 + 主题序号 + 步号」记：
  // 步号在主题内从 1 起，不带主题序号的话下一个主题的第一步会被当成旧的。
  const seenSteps = new Set();
  let activeTopic = "";
  let live = null;
  let liveTimer = null;
  const segmentLoads = new Map();
  // 换会话时旧请求可能还在路上；回来时页码对不上就不画，免得把上一个会话的
  // 内容画到这一个上。
  let loadToken = 0;

  // 详情区当前是「对话」还是「分析」。切换会话时保持不变：正在读分析的人
  // 点下一条会话，看到的应当也是分析。
  let detailView = "conversation";
  // 分析按会话缓存。一次分析要读几十兆的 trace，来回切标签不该重新算一遍。
  const analysisCache = new Map();
  let analysisLoadToken = 0;
  let selectedTurn = 0;

  // 产物面板与专注模式。这两项跟着人走、不跟着会话走：宽度是屏幕的事，
  // 专注是此刻要不要被打扰的事，换一个任务不该把它们复位。
  const ARTIFACT_KEY = "vsa.history.artifacts";
  const FOCUS_KEY = "vsa.history.focus";
  // 0 表示收起。按钮按这个顺序循环，所以点三次依次是 240 / 300 / 380。
  const ARTIFACT_STEPS = [0, 240, 300, 380];
  let artifactWidth = 0;
  let focusMode = false;
  // 底部输入框。整页只建一次，换会话时只换它认的会话编号。
  let chatComposer = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(value) {
    if (value === null || value === undefined) return "";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function formatClock(seconds) {
    if (!seconds) return "—";
    const date = new Date(seconds * 1000);
    const pad = (value) => String(value).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function formatDuration(seconds) {
    if (!(seconds > 0)) return "";
    if (seconds < 10) return `${seconds.toFixed(1)} 秒`;
    if (seconds < 60) return `${Math.round(seconds)} 秒`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} 分 ${Math.round(seconds % 60)} 秒`;
  }

  function cmpPos(left, right) {
    if (left.message !== right.message) return left.message < right.message ? -1 : 1;
    if (left.part !== right.part) return left.part < right.part ? -1 : 1;
    return 0;
  }

  function oneLine(text, limit) {
    const line = String(text || "").split("\n").find((item) => item.trim()) || "";
    const trimmed = line.trim();
    return trimmed.length > limit ? `${trimmed.slice(0, limit)}…` : trimmed;
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"]/g,
      (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char])
    );
  }

  function formatPct(value) {
    return `${((value || 0) * 100).toFixed(1)}%`;
  }

  // 没有 trace 时消息自带的时间戳会跨过两次输入之间的空闲，不能当成耗时。
  function secondsText(seconds, hasTrace, hasReply) {
    if (!hasTrace || !hasReply || !(seconds > 0)) return "—";
    return formatDuration(seconds) || `${seconds.toFixed(1)} 秒`;
  }

  // ------------------------------------------------------------------
  // 展开状态
  // ------------------------------------------------------------------

  function storageKey() {
    return `vsa.history.open.${activeId}`;
  }

  function loadOpenState() {
    manualOpen = new Set();
    manualClosed = new Set();
    runOpen = new Set();
    stepOpen = new Set();
    try {
      const raw = window.localStorage.getItem(storageKey());
      if (!raw) return;
      const data = JSON.parse(raw);
      manualOpen = new Set(Array.isArray(data.open) ? data.open : []);
      manualClosed = new Set(Array.isArray(data.closed) ? data.closed : []);
      runOpen = new Set(Array.isArray(data.runs) ? data.runs : []);
      stepOpen = new Set(Array.isArray(data.steps) ? data.steps : []);
    } catch (error) {
      // 存坏了就当没存过：一条读不出来的偏好不该让整页打不开。
      manualOpen = new Set();
      manualClosed = new Set();
      runOpen = new Set();
      stepOpen = new Set();
    }
  }

  function saveOpenState() {
    if (!activeId) return;
    try {
      window.localStorage.setItem(
        storageKey(),
        JSON.stringify({
          open: [...manualOpen],
          closed: [...manualClosed],
          runs: [...runOpen],
          steps: [...stepOpen],
        })
      );
    } catch (error) {
      // 无痕模式禁用本地存储：这一页里照样能展开，只是刷新之后不记得。
    }
  }

  // ------------------------------------------------------------------
  // 产物面板与专注模式
  // ------------------------------------------------------------------

  function loadChrome() {
    const saved = Number(window.localStorage.getItem(ARTIFACT_KEY) || 0);
    artifactWidth = ARTIFACT_STEPS.includes(saved) ? saved : 0;
    focusMode = window.localStorage.getItem(FOCUS_KEY) === "1";
  }

  function saveChrome() {
    try {
      window.localStorage.setItem(ARTIFACT_KEY, String(artifactWidth));
      window.localStorage.setItem(FOCUS_KEY, focusMode ? "1" : "0");
    } catch (error) {
      // 同上：存不下就不记，页面里的状态仍然生效。
    }
  }

  // 三栏的显隐全部挂在 body 的类上，宽度也一样。集中在这一处改，
  // 免得按钮文案、类名与 CSS 三处各说一套。
  function applyChrome() {
    const body = document.body;
    body.classList.toggle("artifacts-open", artifactWidth > 0);
    body.classList.toggle("artifacts-w-240", artifactWidth === 240);
    body.classList.toggle("artifacts-w-380", artifactWidth === 380);
    body.classList.toggle("focus-mode", focusMode);

    if (!root) return;
    const toggle = root.querySelector("#artifact-toggle");
    if (toggle) {
      toggle.textContent = artifactWidth ? `产物 ${artifactWidth}` : "产物";
      toggle.setAttribute("aria-expanded", artifactWidth ? "true" : "false");
    }
    for (const button of root.querySelectorAll(
      ".pane-toggle, #artifact-toggle, #analysis-toggle"
    )) {
      // 专注模式下这几个按钮都不能用：打开抽屉或统计页就等于退出专注，
      // 而退出专注有专门的按钮与 Esc。
      button.disabled = focusMode;
    }
    const focus = root.querySelector("#focus-toggle");
    if (focus) {
      focus.textContent = focusMode ? "退出专注" : "专注";
      focus.setAttribute("aria-pressed", focusMode ? "true" : "false");
    }
    syncAnalysisToggle();
  }

  function cycleArtifacts() {
    const next = (ARTIFACT_STEPS.indexOf(artifactWidth) + 1) % ARTIFACT_STEPS.length;
    artifactWidth = ARTIFACT_STEPS[next];
    saveChrome();
    applyChrome();
  }

  function setFocusMode(value) {
    focusMode = value;
    saveChrome();
    applyChrome();
  }

  function topicOpen(eventId) {
    if (manualOpen.has(eventId)) return true;
    if (manualClosed.has(eventId)) return false;
    // 没碰过的按默认：只有最后一个主题展开。新主题出现时上一条就自动收起。
    const last = events[events.length - 1];
    return Boolean(last) && last.id === eventId;
  }

  // ------------------------------------------------------------------
  // 左侧：会话树
  // ------------------------------------------------------------------

  function buildTree(items) {
    // 子 agent 在接口里和根会话混在一个数组里，按 parentId 重新挂回去。
    // 找不到父节点的（父会话被删了）当根会话处理，不能让它从列表里消失。
    const byId = new Map(items.map((item) => [item.id, item]));
    const children = new Map();
    const roots = [];
    for (const item of items) {
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
    return { children, roots };
  }

  function renderList() {
    const list = root.querySelector("#session-list");
    list.setAttribute("role", "tree");
    list.innerHTML = "";
    const needle = keyword.trim().toLowerCase();
    const { children, roots } = buildTree(sessions);
    let shown = 0;

    // 过滤时保留命中节点的祖先：只画一条缩进的子 agent 而没有父行，
    // 看不出它是从哪条会话分出来的。
    function included(node) {
      if (!needle) return true;
      if ((node.title || "").toLowerCase().includes(needle)) return true;
      return (children.get(node.id) || []).some(included);
    }

    // 当前选中的子 agent 的祖先不能被折叠藏起来，否则从详情链接回来时
    // 列表里看不到自己正打开的那一条。
    function isActiveAncestor(node) {
      let current = sessions.find((item) => item.id === activeId);
      while (current && current.parentId) {
        if (current.parentId === node.id) return true;
        current = sessions.find((item) => item.id === current.parentId);
      }
      return false;
    }

    function appendNode(node, depth) {
      if (!included(node)) return;
      shown += 1;
      const item = document.createElement("li");
      item.dataset.sessionId = node.id;
      item.dataset.depth = String(depth);
      item.setAttribute("role", "treeitem");
      item.setAttribute("aria-level", String(depth + 1));
      if (depth > 0) item.classList.add("is-child");
      if (node.id === activeId) item.classList.add("is-active");

      const isChild = Boolean(node.parentId);
      const own = children.get(node.id) || [];
      const expanded =
        !collapsed.has(node.id) || Boolean(needle) || isActiveAncestor(node);
      const title = el("div", "session-title");

      // 有子会话才画折叠按钮；叶子留同宽的空位，标题列才对得齐。
      if (own.length) {
        const toggle = el("button", "tree-toggle", expanded ? "▾" : "▸");
        toggle.type = "button";
        toggle.setAttribute("aria-expanded", String(expanded));
        toggle.title = expanded ? "收起子 agent" : "展开子 agent";
        toggle.addEventListener("click", (event) => {
          event.stopPropagation();
          if (collapsed.has(node.id)) collapsed.delete(node.id);
          else collapsed.add(node.id);
          renderList();
        });
        title.appendChild(toggle);
        if (expanded) item.setAttribute("aria-expanded", "true");
        else item.setAttribute("aria-expanded", "false");
      } else {
        title.appendChild(el("span", "tree-spacer"));
      }

      if (isChild) {
        const dot = el("span", `agent-dot ${AGENT_DOT[node.status] || "dot-idle"}`);
        dot.title = `子 agent · ${AGENT_STATUS[node.status] || node.status || "空闲"}`;
        title.appendChild(dot);
      }
      title.appendChild(el("span", "session-name", node.title));
      if (node.runId) {
        title.appendChild(el("span", "run-mark", "有结构"));
      }
      if (own.length) {
        title.appendChild(el("span", "agent-count", `${own.length} 子`));
      }
      item.appendChild(title);

      const bits = [];
      if (isChild) {
        const label = AGENT_STATUS[node.status] || node.status || "空闲";
        const note = (node.statusNote || "").trim();
        bits.push(
          note
            ? `${label}（${note.length > 20 ? `${note.slice(0, 20)}…` : note}）`
            : label
        );
      }
      if (node.eventCount) bits.push(`${node.eventCount} 个主题`);
      bits.push(`${node.messageCount} 条`, formatTime(node.updatedAt), node.modelId || "未指定模型");
      const metaRow = el("div", "session-meta-row");
      metaRow.appendChild(el("div", "session-meta", bits.join(" · ")));
      // 每一行都能直接看分析：不先进会话再切标签，翻十几条会话时少一次点击。
      const analysis = el("button", "session-analysis", "分析");
      analysis.type = "button";
      analysis.title = "看这个会话的轮次、token、工具与异常";
      analysis.addEventListener("click", (event) => {
        event.stopPropagation();
        selectSession(node.id, "", "analysis");
      });
      metaRow.appendChild(analysis);
      item.appendChild(metaRow);
      item.addEventListener("click", () => selectSession(node.id));
      list.appendChild(item);

      // 折叠只影响显示；过滤时强制展开，否则命中的子 agent 会被藏起来。
      if (!expanded) return;
      for (const child of own) appendNode(child, depth + 1);
    }

    for (const node of roots) appendNode(node, 0);

    if (!shown) {
      // 两种空不是一回事：一个是「这个库里确实没有」，一个是「服务没答上来」。
      // 后者由 `showListError` 画，这里只负责前者。
      list.appendChild(
        el("li", "empty-hint", needle ? "没有匹配的会话。" : "还没有会话。")
      );
    }
  }

  function showListError(message) {
    // 读失败时列表不能只是空白：空白和「确实没有会话」看起来一模一样，
    // 用户会以为是历史丢了。
    const list = root.querySelector("#session-list");
    list.innerHTML = "";
    const item = el("li", "empty-hint", `读取会话失败：${message}`);
    const retry = el("button", "link-button", "重试");
    retry.type = "button";
    retry.addEventListener("click", () => loadSessions());
    item.appendChild(document.createElement("br"));
    item.appendChild(retry);
    list.appendChild(item);
  }

  // ------------------------------------------------------------------
  // 头部
  // ------------------------------------------------------------------

  function renderHead(session) {
    const head = root.querySelector("#session-head");
    head.innerHTML = "";
    head.classList.remove("is-hidden");

    // 子 agent 的头部先写一行来源：父会话可点，直接跳过去。
    if (session.parentId) {
      const parent = sessions.find((item) => item.id === session.parentId);
      const badge = el("div", "head-badge");
      badge.appendChild(el("span", "agent-badge", "子 agent"));
      badge.appendChild(el("span", "head-parent", "父会话 "));
      if (parent) {
        const link = el("a", "link-inline", parent.title);
        link.href = `#/history/${encodeURIComponent(parent.id)}`;
        link.addEventListener("click", (event) => {
          event.preventDefault();
          selectSession(parent.id);
        });
        badge.appendChild(link);
      } else {
        badge.appendChild(el("span", "head-parent", "（已删除）"));
      }
      head.appendChild(badge);
    }

    head.appendChild(el("div", "head-title", session.title));

    const bits = [
      `模型 ${session.modelId || "未指定"}`,
      `${session.messageCount} 条消息`,
      `${events.length} 个主题`,
      `${formatNumber(session.totalTokens)} tokens`,
      `$${session.totalCost.toFixed(4)}`,
      `更新 ${formatTime(session.updatedAt)}`,
    ];
    if (session.parentId) {
      bits.unshift(`状态 ${AGENT_STATUS[session.status] || session.status || "空闲"}`);
    }
    head.appendChild(el("div", "head-meta", bits.join(" · ")));

    // 目标放在头部一行：所有主题都收起来时，这一行仍然说明整件事要做什么。
    if (task && task.goal) {
      const goal = el("div", "head-goal");
      goal.append("目标 ", task.goal);
      goal.title = task.goal;
      head.appendChild(goal);
    }

    const actions = el("div", "head-actions");
    if (session.runId) {
      const link = el("a", "link-button", `查看这次运行的结构（${session.runId}）`);
      link.href = `#/structure/${encodeURIComponent(session.runId)}`;
      actions.appendChild(link);
    }
    // 产物面板在宽屏上也是一份按需打开的索引，按钮因此常显；
    // 主题栏只有窄屏才收成抽屉，它的按钮跟着 CSS 的断点走。
    const artifactToggle = el("button", "head-toggle", "产物");
    artifactToggle.type = "button";
    artifactToggle.id = "artifact-toggle";
    artifactToggle.addEventListener("click", cycleArtifacts);
    actions.appendChild(artifactToggle);

    // 「分析」与产物、专注并列：打开会话后三个开关在同一行，
    // 点一下在对话与统计之间来回切换。
    const analysisToggle = el("button", "head-toggle", "分析");
    analysisToggle.type = "button";
    analysisToggle.id = "analysis-toggle";
    analysisToggle.addEventListener("click", toggleAnalysisView);
    actions.appendChild(analysisToggle);

    const topicToggle = el("button", "pane-toggle", "主题");
    topicToggle.type = "button";
    topicToggle.dataset.pane = "topics";
    topicToggle.addEventListener("click", () => {
      root.querySelector(".history-detail").classList.toggle("show-topics");
    });
    actions.appendChild(topicToggle);

    const focusToggle = el("button", "head-toggle", "专注");
    focusToggle.type = "button";
    focusToggle.id = "focus-toggle";
    focusToggle.addEventListener("click", () => setFocusMode(!focusMode));
    actions.appendChild(focusToggle);
    if (actions.childNodes.length) head.appendChild(actions);
    // 头部是整块重建的，按钮文案与禁用状态得跟着当前的面板状态再写一遍。
    applyChrome();
  }

  // ------------------------------------------------------------------
  // 中间：主题章节
  // ------------------------------------------------------------------

  function toolNode(part, messageIndex, partIndex) {
    const box = el("div", "part part-tool");
    box.dataset.pos = `${messageIndex}:${partIndex}`;
    const failed = part.toolStatus === "failed";
    if (failed) box.classList.add("is-failed");

    const head = el("div", "tool-head");
    head.appendChild(el("span", "tool-name", part.toolName || "tool"));
    head.appendChild(
      el(
        "span",
        `status status-${part.toolStatus || "pending"}`,
        STATUS_TEXT[part.toolStatus] || part.toolStatus || "等待"
      )
    );
    if (part.permission) head.appendChild(el("span", "tool-label", `判定 ${part.permission}`));
    // 结果的第一行放在标题上：不展开也能看出这一步做成了什么。
    const preview = oneLine(part.toolOutput, 90);
    if (preview) head.appendChild(el("span", "tool-preview", preview));
    box.appendChild(head);

    const body = el("details", "tool-body");
    // 失败的那一条默认打开：它就是要被看见的那一段。其余收起，需要时再点开。
    body.open = failed;
    body.appendChild(
      el(
        "summary",
        "",
        `输入与输出（${formatNumber((part.toolOutput || "").length)} 字符）`
      )
    );
    if (part.toolInput && Object.keys(part.toolInput).length) {
      const section = el("div", "tool-section is-input");
      section.appendChild(el("div", "tool-label", "输入"));
      section.appendChild(el("pre", "", window.vsMarkdown.renderJson(part.toolInput)));
      body.appendChild(section);
    }
    if (part.toolOutput) {
      const section = el("div", "tool-section is-output");
      const tools = el("div", "tool-tools");
      const copy = el("button", "link-button", "复制");
      copy.type = "button";
      copy.addEventListener("click", () => {
        navigator.clipboard.writeText(part.toolOutput || "");
        copy.textContent = "已复制";
      });
      const save = el("button", "link-button", "下载");
      save.type = "button";
      save.addEventListener("click", () => {
        const blob = new Blob([part.toolOutput || ""], { type: "text/plain;charset=utf-8" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `${part.toolName || "tool"}-${messageIndex}-${partIndex}.txt`;
        link.click();
        URL.revokeObjectURL(link.href);
      });
      tools.append(copy, save);
      section.appendChild(tools);
      section.appendChild(el("pre", "", part.toolOutput));
      body.appendChild(section);
    }
    box.appendChild(body);
    return box;
  }

  function partNode(part, messageIndex, partIndex) {
    switch (part.type) {
      case "text": {
        const box = el("div", "part part-text");
        box.innerHTML = window.vsMarkdown.renderMarkdown(part.text || "");
        return box;
      }
      case "reasoning": {
        const details = el("details", "part part-reasoning");
        details.appendChild(el("summary", "", "思考过程"));
        details.appendChild(el("div", "reason-body", part.text || ""));
        return details;
      }
      case "tool":
        return toolNode(part, messageIndex, partIndex);
      case "patch": {
        const box = el("div", "part part-patch");
        const head = el("div", "patch-head");
        head.appendChild(el("span", "patch-path", part.filePath || part.text || "补丁"));
        head.appendChild(el("span", "patch-added", `+${part.patchAdded}`));
        head.appendChild(el("span", "patch-removed", `-${part.patchRemoved}`));
        box.appendChild(head);
        return box;
      }
      case "error": {
        const box = el("div", "part part-error");
        box.appendChild(el("div", "tool-label", "出错了"));
        box.appendChild(el("div", "part-text", part.text || ""));
        return box;
      }
      case "summary": {
        const details = el("details", "part part-reasoning part-summary");
        details.open = true;
        details.appendChild(el("summary", "", "这一轮停在哪儿"));
        const body = el("div", "reason-body");
        body.innerHTML = window.vsMarkdown.renderMarkdown(part.text || "");
        details.appendChild(body);
        return details;
      }
      case "file": {
        const box = el("div", "part part-note");
        box.append("引用文件 ", el("code", "", part.filePath));
        return box;
      }
      case "agent":
        return el("div", "part part-note", `子 agent ${part.agentName}`);
      case "step_start":
        return el("div", "part part-step", `第 ${(part.text || "").split(" · ")[0]} 步`);
      case "step_finish":
        return null;
      case "subtask":
        return el("div", "part part-note", `子任务 ${part.agentName || "subagent"} ${part.text}`);
      case "retry":
        return el("div", "part part-note", `重试 ${part.text}`);
      case "compaction":
        return el("div", "part part-note", `上下文已压缩 ${part.text}`);
      default:
        return el("div", "part part-note", part.text || part.type);
    }
  }

  const NARRATIVE_SKIP = new Set(["tool", "step_start", "step_finish"]);

  function partsInRange(message, from, to) {
    const offset = message.partOffset || 0;
    const kept = [];
    message.parts.forEach((part, index) => {
      const position = { message: message.index, part: offset + index };
      if (cmpPos(position, from) >= 0 && cmpPos(position, to) < 0) {
        kept.push({ part, partIndex: offset + index });
      }
    });
    return kept;
  }

  function messageNode(message, kept, compactUptoValue) {
    const nodes = [];
    if (compactUptoValue > 0 && message.index === compactUptoValue) {
      nodes.push(
        el(
          "div",
          "compact-mark",
          `此处之前的 ${compactUptoValue} 条消息已压缩，不再发送给模型`
        )
      );
    }

    const article = el("article", `msg msg-${message.role}`);
    const head = el("div", "msg-head");
    head.appendChild(el("span", "msg-role", message.role === "user" ? "你" : "assistant"));

    const bits = [formatTime(message.createdAt)];
    if (message.modelId) bits.push(message.modelId);
    const tokens = Number(message.tokens?.input || 0) + Number(message.tokens?.output || 0);
    if (tokens) bits.push(`${formatNumber(tokens)} tokens`);
    if (message.cost) bits.push(`$${message.cost.toFixed(4)}`);
    head.appendChild(el("span", "msg-meta", bits.join(" · ")));
    article.appendChild(head);

    for (const item of kept) {
      const node = partNode(item.part, message.index, item.partIndex);
      if (node) article.appendChild(node);
    }
    nodes.push(article);
    return nodes;
  }

  // 主题里的叙述：用户原话、模型文字、思考、文件引用。工具调用单独成块，
  // 由 `runBlock` 画——两类信息一个在讲结论、一个在给证据，混在一起读不出重点。
  function narrativeNode(payload, from, to) {
    const box = el("div", "topic-narrative");
    let drawn = 0;
    for (const message of payload.messages) {
      const kept = partsInRange(message, from, to).filter(
        (item) => !NARRATIVE_SKIP.has(item.part.type)
      );
      if (!kept.length) continue;
      for (const node of messageNode(message, kept, compactUpto)) box.appendChild(node);
      drawn += 1;
    }
    if (!drawn) box.appendChild(el("p", "empty-hint", "这一段没有可显示的文字。"));
    return box;
  }

  function spansOf(list) {
    const spans = [];
    let start = { message: 0, part: 0 };
    for (const event of list) {
      spans.push({ event, from: start, to: event.cut });
      start = event.cut;
    }
    return spans;
  }

  function spanOf(eventId) {
    return spansOf(events).find((item) => item.event.id === eventId) || null;
  }

  // 一步的时长：从它那个 `STEP_START` 到下一个 `STEP_START`（最后一步到消息末尾）。
  // 这是墙钟时间，含模型生成与工具执行；工具级耗时没有单独记，所以标题上写明口径，
  // 免得读的人把它当成一次工具调用的耗时。
  function stepDuration(step, payload) {
    const message = payload.messages.find((item) => item.index === step.messageIndex);
    if (!message) return "";
    const offset = message.partOffset || 0;
    let start = 0;
    let end = 0;
    message.parts.forEach((part, index) => {
      if (part.type !== "step_start") return;
      const position = offset + index;
      if (position <= step.partStart) start = part.timestamp;
      else if (!end) end = part.timestamp;
    });
    if (!start) return "";
    if (!end) {
      const last = message.parts[message.parts.length - 1];
      end = last ? last.timestamp : 0;
    }
    return end > start ? formatDuration(end - start) : "";
  }

  function stepTools(step, payload) {
    const message = payload.messages.find((item) => item.index === step.messageIndex);
    if (!message) return [];
    const offset = message.partOffset || 0;
    const kept = [];
    message.parts.forEach((part, index) => {
      const position = offset + index;
      if (position < step.partStart || position >= step.partEnd) return;
      if (part.type !== "tool") return;
      kept.push({ part, partIndex: position });
    });
    return kept;
  }

  function uncoveredTools(event, payload) {
    const covered = new Set();
    for (const step of event.steps) {
      for (const item of stepTools(step, payload)) covered.add(item.partIndex);
    }
    const kept = [];
    for (const message of payload.messages) {
      const offset = message.partOffset || 0;
      message.parts.forEach((part, index) => {
        if (part.type !== "tool") return;
        const position = offset + index;
        if (covered.has(position)) return;
        kept.push({ part, partIndex: position, messageIndex: message.index });
      });
    }
    return kept;
  }

  function stepRow(event, step, payload, extra) {
    const key = `${event.id}:${step.seq}`;
    const failed = step.issues.length > 0;
    // 失败的那一步默认打开：信息不该藏在一次点击后面。用户收起之后在本页记住。
    const open = stepOpen.has(key) || (failed && !stepOpen.has(`${key}:closed`));

    const row = el("li", "step-row");
    if (failed) row.classList.add("is-failed");
    if (open) row.classList.add("is-open");

    const head = el("button", "step-head");
    head.type = "button";
    head.appendChild(el("span", "step-no", extra ? "·" : String(step.seq).padStart(2, "0")));
    head.appendChild(el("span", "step-intent", step.intent || "（没有记录）"));
    const duration = stepDuration(step, payload);
    if (duration) {
      const clock = el("span", "step-time", duration);
      clock.title = "这一轮的全部时间，含模型生成与工具执行";
      head.appendChild(clock);
    }
    head.appendChild(
      el(
        "span",
        `step-status ${failed ? "is-failed" : "is-done"}`,
        failed ? "失败" : "完成"
      )
    );
    row.appendChild(head);

    const body = el("div", "step-body");
    if (step.result) body.appendChild(el("div", "step-result", step.result));
    const tools = extra || stepTools(step, payload);
    for (const item of tools) {
      body.appendChild(toolNode(item.part, item.messageIndex ?? step.messageIndex, item.partIndex));
    }
    if (step.issues.length) {
      const box = el("div", "step-issues");
      for (const line of step.issues.slice(0, 4)) box.appendChild(el("div", "issue-line", line));
      body.appendChild(box);
    }
    row.appendChild(body);

    // 展开状态在闭包里自己留着：这一行不重画，读闭包里那个初值会在第二次
    // 点击时给出相反的结论。
    let expanded = open;
    head.addEventListener("click", () => {
      expanded = !expanded;
      if (expanded) {
        stepOpen.add(key);
        stepOpen.delete(`${key}:closed`);
      } else {
        stepOpen.delete(key);
        stepOpen.add(`${key}:closed`);
      }
      row.classList.toggle("is-open", expanded);
      head.setAttribute("aria-expanded", String(expanded));
      saveOpenState();
    });
    head.setAttribute("aria-expanded", String(open));
    return row;
  }

  function runBlock(event, payload) {
    const block = el("div", "run-block");
    if (!event.steps.length) {
      block.appendChild(
        el("div", "run-head static", "这一步没有调用工具，只有模型自己的文字。")
      );
      return block;
    }

    const calls = event.steps.reduce((total, step) => total + step.tools.length, 0);
    const names = [];
    for (const step of event.steps) {
      for (const name of step.tools) if (!names.includes(name)) names.push(name);
    }
    const failed = event.steps.filter((step) => step.issues.length).length;
    const last = event.steps[event.steps.length - 1];
    // 有失败时摘要取自那一步，不取最后一步：最后一步常常是换了做法之后成功的
    // 那一次，拿它当摘要会把「没做成」写成「成功了」。
    const firstFailure = event.steps.find((step) => step.issues.length);
    const note = failed
      ? `${failed} 步没有做成${firstFailure && oneLine(firstFailure.result, 60) ? `：${oneLine(firstFailure.result, 60)}` : ""}`
      : oneLine(last.result, 80);

    const head = el("button", "run-head");
    head.type = "button";
    head.appendChild(el("span", "run-label", "工具执行"));
    head.appendChild(
      el("span", "run-count", `${calls} 次调用 · ${names.length} 个工具`)
    );
    if (note) head.appendChild(el("span", "run-note", note));
    head.appendChild(el("span", "run-toggle", runOpen.has(event.id) ? "收起" : "展开"));
    block.appendChild(head);

    const body = el("div", "run-body");
    const toolbar = el("div", "run-tools");
    const openAll = el("button", "link-button", "展开全部步");
    openAll.type = "button";
    openAll.addEventListener("click", () => {
      for (const step of event.steps) {
        stepOpen.add(`${event.id}:${step.seq}`);
        stepOpen.delete(`${event.id}:${step.seq}:closed`);
      }
      runOpen.add(event.id);
      fillTopicBody(event);
      renderTopicRail();
      saveOpenState();
    });
    const closeAll = el("button", "link-button", "收起全部步");
    closeAll.type = "button";
    closeAll.addEventListener("click", () => {
      for (const step of event.steps) {
        stepOpen.add(`${event.id}:${step.seq}:closed`);
        stepOpen.delete(`${event.id}:${step.seq}`);
      }
      fillTopicBody(event);
      renderTopicRail();
      saveOpenState();
    });
    const hint = el("span", "run-hint", "时长是这一轮的全部时间，含模型生成与工具执行");
    toolbar.append(openAll, closeAll, hint);
    body.appendChild(toolbar);

    const list = el("ol", "step-list");
    for (const step of event.steps) list.appendChild(stepRow(event, step, payload, null));
    const stray = uncoveredTools(event, payload);
    if (stray.length) {
      // 流水只记「有工具的那几步」，`close_topic` 这类记账调用不在里面。
      // 不补这一行的话它们在页面上无声消失。
      list.appendChild(
        stepRow(
          event,
          { seq: 0, intent: "其他工具调用", tools: [], result: "", issues: [] },
          payload,
          stray
        )
      );
    }
    body.appendChild(list);
    block.appendChild(body);
    block.classList.toggle("is-open", runOpen.has(event.id));

    head.addEventListener("click", () => {
      if (runOpen.has(event.id)) runOpen.delete(event.id);
      else runOpen.add(event.id);
      block.classList.toggle("is-open", runOpen.has(event.id));
      head.querySelector(".run-toggle").textContent = runOpen.has(event.id) ? "收起" : "展开";
      saveOpenState();
    });
    return block;
  }

  function topicCard(event) {
    const card = el("section", `topic-card status-${event.status || "completed"}`);
    card.dataset.event = event.id;
    if (topicOpen(event.id)) card.classList.add("is-open");

    const head = el("header", "topic-card-head");
    const line = el("div", "topic-line");
    line.appendChild(
      el("span", "topic-no", String(event.topicIndex || 0).padStart(2, "0"))
    );
    const title = el("button", "topic-name", event.title || "（没有标题）");
    title.type = "button";
    title.addEventListener("click", () => toggleTopic(event.id, true));
    line.appendChild(title);
    line.appendChild(
      el(
        "span",
        `topic-status status-${event.status || "completed"}`,
        TOPIC_STATUS[event.status || "completed"] || "已完成"
      )
    );
    if (event.kind !== "manual") {
      line.appendChild(
        el("span", `event-kind kind-${event.kind}`, EVENT_KIND[event.kind] || event.kind)
      );
    }

    const bits = [`${event.steps.length} 步`];
    const calls = event.steps.reduce((total, step) => total + step.tools.length, 0);
    if (calls && calls !== event.steps.length) bits.push(`${calls} 次调用`);
    if (event.artifacts.length) bits.push(`${event.artifacts.length} 个产物`);
    bits.push(formatClock(event.createdAt));
    line.appendChild(el("span", "topic-meta", bits.join(" · ")));

    const toggle = el("button", "topic-toggle", topicOpen(event.id) ? "收起" : "展开");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(topicOpen(event.id)));
    toggle.addEventListener("click", () => toggleTopic(event.id, true));
    line.appendChild(toggle);
    head.appendChild(line);

    if (event.summary) {
      const summary = el("div", "topic-summary", event.summary);
      summary.title = event.summary;
      head.appendChild(summary);
    }
    if (event.artifacts.length) {
      const chips = el("div", "event-chips");
      for (const name of event.artifacts.slice(0, 5)) chips.appendChild(el("span", "chip", name));
      if (event.artifacts.length > 5) {
        chips.appendChild(el("span", "chip chip-more", `还有 ${event.artifacts.length - 5} 个`));
      }
      head.appendChild(chips);
    }
    if (event.status === "failed" && event.issues.length) {
      // 收起状态也要看得见失败原因：一行就够，详情在正文里。
      const alert = el("div", "topic-alert", oneLine(event.issues[0], 120));
      alert.title = event.issues.join("\n");
      head.appendChild(alert);
    }
    if (event.degraded) {
      head.appendChild(el("div", "topic-degraded", "标题与结论是本地拼的"));
    }
    card.appendChild(head);

    const body = el("div", "topic-body");
    card.appendChild(body);
    return card;
  }

  function fillTopicBody(event) {
    const card = root.querySelector(`.topic-card[data-event="${event.id}"]`);
    if (!card) return;
    const body = card.querySelector(".topic-body");
    const open = topicOpen(event.id);
    card.classList.toggle("is-open", open);
    // 标题上的按钮跟着状态走：只改卡片类名的话，按钮还写着「展开」。
    const toggle = card.querySelector(".topic-toggle");
    if (toggle) {
      toggle.textContent = open ? "收起" : "展开";
      toggle.setAttribute("aria-expanded", String(open));
    }
    body.innerHTML = "";
    if (!open) return;

    const payload = segments.get(event.id);
    if (!payload) {
      body.appendChild(el("p", "empty-hint", "正在读取这一段…"));
      loadSegment(event).then((loaded) => {
        // 取不到（切了会话、或者服务报错）时节流不再往下走：那两种情况都已经
        // 各自画过提示，重试由下一次展开或刷新触发。
        if (loaded) fillTopicBody(event);
      });
      return;
    }
    const span = spanOf(event.id);
    if (!span) {
      body.appendChild(el("p", "empty-hint", "这一段不在当前会话的区间里。"));
      return;
    }
    body.appendChild(narrativeNode(payload, span.from, span.to));
    body.appendChild(runBlock(event, payload));
    const produced = topicArtifactEntries(event);
    if (produced.length) body.appendChild(artifactList(produced));

    if (event.handoff) {
      const box = el("div", "event-handoff");
      box.append(el("span", "tool-label", "留给后续主题："), event.handoff);
      body.appendChild(box);
    }
    if (event.issues.length) {
      const box = el("div", "event-issues");
      box.appendChild(el("div", "tool-label", `未解决（${event.issues.length}）`));
      for (const line of event.issues.slice(0, 4)) {
        box.appendChild(el("div", "issue-line", line));
      }
      body.appendChild(box);
    }
  }

  // 主题产出的 handle 落在登记表里才有下载地址。`event.artifacts` 里还混着
  // 文件路径与补丁文字，那几类按原样显示，不给链接。
  const ARTIFACT_HANDLE = /^(receptor|ligandset|artifact)#\d+$/;

  // 结构全景在登记表里是一个目录（survey.json 放在目录里），没有可下载的文件，
  // 所以 download 字段是空的。登记表另行给出 kind=survey 与 survey_id，
  // 这里换成指向结构全景页的页内地址，主题清单与右侧产物栏共用一份。
  function surveyView(entry) {
    const meta = entry.meta || {};
    const surveyId = String(meta.survey_id || "").trim();
    return meta.kind === "survey" && surveyId
      ? `#/survey/${encodeURIComponent(surveyId)}`
      : "";
  }

  function topicArtifactEntries(event) {
    const items = (artifacts && artifacts.items) || [];
    const byHandle = new Map(items.map((item) => [item.handle, item]));
    const found = [];
    for (const name of event.artifacts) {
      if (!ARTIFACT_HANDLE.test(name)) continue;
      const entry = byHandle.get(name);
      if (entry) found.push(entry);
    }
    return found;
  }

  function artifactList(entries) {
    const box = el("div", "topic-artifacts");
    box.appendChild(el("div", "tool-label", `本主题产物（${entries.length}）`));
    for (const entry of entries) {
      const row = el("div", "topic-artifact");
      const label = entry.summary || entry.name || entry.handle;
      const view = surveyView(entry);
      if (entry.download || view) {
        const link = el("a", "topic-artifact-name", label);
        link.href = entry.download || view;
        link.title = entry.path || entry.handle;
        // 打开页面的链接与下载文件的链接在样式上区分开：一个带 ↗，一个带 ↓。
        if (view && !entry.download) link.classList.add("is-view");
        row.appendChild(link);
      } else {
        const name = el("span", "topic-artifact-name", label);
        name.title = entry.path || entry.handle;
        row.appendChild(name);
        // 目录没有可下载的字节，只能列出名字。
        row.appendChild(el("span", "topic-artifact-kind", "目录"));
      }
      row.appendChild(el("span", "topic-artifact-kind", ARTIFACT_KIND[entry.kind] || entry.kind));
      if (entry.bytes) row.appendChild(el("span", "topic-artifact-size", formatBytes(entry.bytes)));
      box.appendChild(row);
    }
    return box;
  }

  function toggleTopic(eventId, byUser) {
    const open = topicOpen(eventId);
    if (open) {
      manualClosed.add(eventId);
      manualOpen.delete(eventId);
      runOpen.delete(eventId);
    } else {
      manualOpen.add(eventId);
      manualClosed.delete(eventId);
      // 用户主动点开一个主题时，工具过程跟着打开：这是「我要查这一段」的信号。
      runOpen.add(eventId);
    }
    if (byUser) saveOpenState();
    activeTopic = eventId;
    fillTopicBody(events.find((item) => item.id === eventId) || {});
    renderTopicRail();
  }

  function loadSegment(event) {
    if (segments.has(event.id)) return Promise.resolve(segments.get(event.id));
    // 同一段被两处同时要（正文与左栏跳转）时只发一次请求：并发取同一段时
    // 后回来的那一份会覆盖先回来的，滚动位置跟着跳。
    const pending = segmentLoads.get(event.id);
    if (pending) return pending;

    const token = loadToken;
    const promise = fetchJson(
      `/api/sessions/${encodeURIComponent(activeId)}/segment?event=${encodeURIComponent(event.id)}`
    )
      .then((payload) => {
        if (token !== loadToken) return null;
        segments.set(event.id, payload);
        return payload;
      })
      .catch((error) => {
        if (token !== loadToken) return null;
        const card = root.querySelector(`.topic-card[data-event="${event.id}"]`);
        if (card) {
          const body = card.querySelector(".topic-body");
          body.innerHTML = "";
          body.appendChild(el("p", "empty-hint", `读取这一段失败：${error.message}`));
        }
        return null;
      })
      .finally(() => {
        segmentLoads.delete(event.id);
      });
    segmentLoads.set(event.id, promise);
    return promise;
  }

  // 重画之前记住阅读位置。轮末刷新与运行中接上新主题都会整栏重建，
  // 不记的话页面跳回顶部，读到一半的地方找不到。
  function captureScroll(box) {
    if (!box) return null;
    return {
      top: box.scrollTop,
      // 差 80px 以内算「贴着底部」：原来在跟新内容，重画之后继续跟。
      atBottom: box.scrollHeight - box.scrollTop - box.clientHeight < 80,
    };
  }

  function restoreScroll(box, state) {
    if (!box || !state) return;
    box.scrollTop = state.atBottom ? box.scrollHeight : state.top;
  }

  function renderConversation() {
    const box = root.querySelector("#conversation");
    const scroll = captureScroll(box);
    box.innerHTML = "";
    box.appendChild(taskCard());
    if (!events.length && !(live && live.running)) {
      box.appendChild(el("p", "empty-hint", "这个会话还没有封存的主题。"));
      restoreScroll(box, scroll);
      return;
    }
    for (const event of events) {
      box.appendChild(topicCard(event));
      fillTopicBody(event);
    }
    if (live && live.running) box.appendChild(liveCard());
    restoreScroll(box, scroll);
  }

  function taskCard() {
    if (!task) return el("div", "is-hidden");
    const empty =
      !task.goal &&
      !task.constraints.length &&
      !task.todo.length &&
      !task.blockers.length &&
      !task.hypotheses.length;
    if (empty) return el("div", "is-hidden");

    const card = el("div", "task-card");
    const head = el("div", "task-head");
    head.appendChild(el("span", "task-label", "任务"));
    head.appendChild(el("span", "task-goal", task.goal || "（没有写下目标）"));
    card.appendChild(head);

    const rows = [
      ["约束", task.constraints],
      ["待办", task.todo],
      ["阻塞", task.blockers],
    ];
    for (const [label, items] of rows) {
      if (!items.length) continue;
      const row = el("div", `task-row task-${label}`);
      row.appendChild(el("div", "tool-label", `${label}（${items.length}）`));
      for (const line of items.slice(0, 6)) row.appendChild(el("div", "task-line", line));
      if (items.length > 6) row.appendChild(el("div", "task-line", `还有 ${items.length - 6} 条`));
      card.appendChild(row);
    }

    if (task.hypotheses.length) {
      const details = el("details", "task-hypotheses");
      details.appendChild(el("summary", "", `假设（${task.hypotheses.length}）`));
      for (const item of task.hypotheses) {
        const line = el("div", "task-line");
        line.appendChild(el("span", "chip", item.id || "hyp"));
        line.append(` ${item.statement || ""}`, el("span", "task-status", item.status || ""));
        details.appendChild(line);
      }
      card.appendChild(details);
    }
    return card;
  }

  // ------------------------------------------------------------------
  // 运行中的主题
  // ------------------------------------------------------------------

  // 这一步是不是第一次出现。实时卡片在步数变化时会整块重画，
  // 不按步号去重的话，每两秒的重画会让整段一直在重播淡入。
  function freshRow(row, seq) {
    const key = `${activeId}:${live.topicIndex}:${seq}`;
    if (!seenSteps.has(key)) {
      seenSteps.add(key);
      row.classList.add("is-new");
    }
    return row;
  }

  // 一行已完成的步。
  function completedStepRow(step) {
    const row = el("li", `step-row is-open${step.status === "failed" ? " is-failed" : ""}`);
    const head = el("div", "step-head static");
    head.appendChild(el("span", "step-no", String(step.seq).padStart(2, "0")));
    head.appendChild(el("span", "step-intent", step.intent || "（没有记录）"));
    head.appendChild(
      el(
        "span",
        `step-status ${step.status === "failed" ? "is-failed" : "is-done"}`,
        step.status === "failed" ? "失败" : "完成"
      )
    );
    row.appendChild(head);
    if (step.result) {
      // 运行中的流水只给一行：整段结果在主题封存之后的主卡片里看。
      row.appendChild(el("div", "step-result", oneLine(step.result, 100)));
    }
    return freshRow(row, step.seq);
  }

  // 流水行上写的那一句「这一步在干什么」。建行与刷新都走它，
  // 两处各写一份的话，改了一处的措辞另一处还是旧的。
  function currentLabel(current) {
    if (current.phase === "waiting") return `等待确认：${current.tool}`;
    if (current.phase === "tool") return `正在执行 ${current.tool}`;
    return "模型正在生成";
  }

  function clockNode(seconds) {
    const clock = el("span", "step-time", formatDuration(seconds));
    clock.title = "从这一次调用开始到现在";
    return clock;
  }

  // 一行正在进行的步。`seq` 由调用方给：这一步还没记进快照的 steps。
  function currentStepRow(current, seq) {
    const row = el("li", "step-row is-open is-current");
    const head = el("div", "step-head static");
    head.appendChild(el("span", "step-no", String(seq).padStart(2, "0")));
    head.appendChild(el("span", "step-intent", currentLabel(current)));
    const elapsed = live.updatedAt ? live.updatedAt - current.since : 0;
    if (elapsed > 0) head.appendChild(clockNode(elapsed));
    head.appendChild(el("span", "step-status is-running", "进行中"));
    row.appendChild(head);
    if (current.preview) row.appendChild(el("div", "step-result", current.preview));
    return freshRow(row, seq);
  }

  function liveCard() {
    // 运行中的这一块不折叠：它的内容就是「现在在做什么」，没有收起来的理由。
    const card = el("section", "topic-card is-live is-open");
    card.dataset.live = "1";

    const head = el("header", "topic-card-head");
    const line = el("div", "topic-line");
    line.appendChild(
      el("span", "topic-no", String(live.topicIndex || 0).padStart(2, "0"))
    );
    line.appendChild(el("span", "topic-name static", live.title || "正在进行的主题"));
    line.appendChild(el("span", "topic-status status-running", "进行中"));
    const bits = [];
    if (live.steps.length) bits.push(`${live.steps.length} 步完成`);
    if (live.calls) bits.push(`${live.calls} 次调用`);
    if (live.tools.length) bits.push(`${live.tools.length} 个工具`);
    if (bits.length) line.appendChild(el("span", "topic-meta", bits.join(" · ")));
    head.appendChild(line);

    const now = el("div", "live-now");
    head.appendChild(now);
    card.appendChild(head);

    const body = el("div", "topic-body is-open");
    const list = el("ol", "step-list live-steps");
    for (const step of live.steps) list.appendChild(completedStepRow(step));
    if (live.current) list.appendChild(currentStepRow(live.current, live.steps.length + 1));
    body.appendChild(list);
    card.appendChild(body);
    updateLiveNow(card);
    return card;
  }

  function updateLiveNow(card) {
    const box = card.querySelector(".live-now");
    if (!box) return;
    box.innerHTML = "";
    if (!live) return;
    if (live.stale) {
      // 断开只是一句提醒，最后已知的状态照样要画：只说「可能断开」而不说
      // 最后停在哪，用户还是不知道发生了什么。
      box.appendChild(
        el(
          "div",
          "topic-alert",
          `可能已断开：最后一次更新在 ${formatClock(live.updatedAt)}`
        )
      );
    }
    const current = live.current;
    if (!current) {
      // 刚起一轮时最后一条还是用户消息，取不到当前步；这一句与「已收尾」必须分开写，
      // 否则页面上一开跑就说这一轮结束了。
      box.appendChild(
        el(
          "div",
          "live-line",
          live.running ? "这一轮刚开始，正在准备第一步。" : "这一轮已经收尾。"
        )
      );
      return;
    }
    if (current.phase === "waiting") {
      box.appendChild(
        el(
          "div",
          live.stale ? "live-line" : "topic-alert",
          `${current.tool} 需要确认，请回到终端回答；网页端是只读的。`
        )
      );
    } else if (current.phase === "tool") {
      const line = el("div", "live-line");
      line.append("正在执行 ", el("strong", "", current.tool));
      if (current.preview) line.append(` · ${current.preview}`);
      box.appendChild(line);
    } else {
      box.appendChild(
        el("div", "live-line", "模型正在生成下一步；这一步还没有工具输出。")
      );
    }
  }

  // 把「进行中」那一行改写成已完成的步。原地改而不是换一张卡：
  // 状态色才有机会从蓝过渡到绿，滚动位置与悬停状态也不受影响。
  function markStepDone(row, step) {
    row.classList.remove("is-current");
    row.classList.toggle("is-failed", step.status === "failed");
    const head = row.querySelector(".step-head");
    head.querySelector(".step-intent").textContent = step.intent || "（没有记录）";
    const pill = head.querySelector(".step-status");
    pill.className = `step-status ${step.status === "failed" ? "is-failed" : "is-done"}`;
    pill.textContent = step.status === "failed" ? "失败" : "完成";
    const clock = head.querySelector(".step-time");
    if (clock) clock.remove();
    let result = row.querySelector(".step-result");
    if (step.result) {
      if (!result) {
        result = el("div", "step-result");
        row.appendChild(result);
      }
      result.textContent = oneLine(step.result, 100);
    } else if (result) {
      result.remove();
    }
  }

  // 当前步没变，只是它在做什么变了（模型生成 → 调工具 → 等确认）。
  function refreshCurrentRow(row, current) {
    const head = row.querySelector(".step-head");
    const intent = head.querySelector(".step-intent");
    const label = currentLabel(current);
    if (intent.textContent !== label) intent.textContent = label;
    // 时钟每次轮询重算：它写的是「从这一次调用开始到现在」。
    const clock = head.querySelector(".step-time");
    const elapsed = current.since && live.updatedAt ? live.updatedAt - current.since : 0;
    if (elapsed > 0) {
      if (clock) {
        clock.textContent = formatDuration(elapsed);
      } else {
        head.insertBefore(clockNode(elapsed), head.querySelector(".step-status"));
      }
    } else if (clock) {
      clock.remove();
    }
    let result = row.querySelector(".step-result");
    if (current.preview) {
      if (!result) {
        result = el("div", "step-result");
        row.appendChild(result);
      }
      result.textContent = current.preview;
    } else if (result) {
      result.remove();
    }
  }

  function updateLiveCard() {
    const card = root.querySelector('[data-live="1"]');
    if (!card) {
      if (live && live.running) renderConversation();
      return;
    }
    // 头部里的序号、步数与当前动作随时在变；整块重画会把用户展开的位置弄丢，
    // 所以只换掉这几个字段。
    const meta = card.querySelector(".topic-meta");
    if (meta) {
      const bits = [];
      if (live.steps.length) bits.push(`${live.steps.length} 步完成`);
      if (live.calls) bits.push(`${live.calls} 次调用`);
      if (live.tools.length) bits.push(`${live.tools.length} 个工具`);
      meta.textContent = bits.join(" · ");
    }
    const titleNode = card.querySelector(".topic-name");
    if (titleNode && live.title) titleNode.textContent = live.title;
    updateLiveNow(card);

    const list = card.querySelector(".live-steps");
    if (!list) return;
    const currentRow = list.querySelector(".is-current");
    const done = list.querySelectorAll(".step-row").length - (currentRow ? 1 : 0);
    // 快照里的步只增不减；变少只可能是换了主题，这种情况整块重画。
    if (done > live.steps.length) {
      card.replaceWith(liveCard());
      return;
    }
    if (currentRow && done === live.steps.length - 1) {
      markStepDone(currentRow, live.steps[done]);
      if (live.current) list.appendChild(currentStepRow(live.current, live.steps.length + 1));
      return;
    }
    if (done < live.steps.length) {
      // 一次补了不止一步（页面被切走一段时间）：中间那几步的身份对不上，
      // 整块重画更省事。
      card.replaceWith(liveCard());
      return;
    }
    if (!live.current) {
      // 当前步已经结束：进行中的那一行不该再留。
      if (currentRow) card.replaceWith(liveCard());
      return;
    }
    if (currentRow) refreshCurrentRow(currentRow, live.current);
  }

  async function pollLive() {
    if (!activeId || document.visibilityState !== "visible") return;
    const token = loadToken;
    let next = null;
    try {
      const data = await fetchJson(`/api/sessions/${encodeURIComponent(activeId)}/live`);
      if (token !== loadToken) return;
      next = data && data.present ? data : null;
    } catch (error) {
      // 轮询失败不打断阅读：下一轮再试，页面上保留上一帧。
      return;
    }
    if (token !== loadToken) return;
    const wasRunning = Boolean(live && live.running);
    const wasCount = live ? live.eventCount : 0;
    live = next;
    // 这一轮没结束就不让再交一件：服务端也会回 409，按钮上先说清楚更省一次往返。
    if (chatComposer) chatComposer.setBusy(Boolean(live && live.running));

    if (live && live.running) {
      if (wasRunning && wasCount !== live.eventCount) {
        // 轮中途封了一个主题：把新的那一段接进来，再更新实时块。
        await refreshSession();
        return;
      }
      if (!wasRunning) renderConversation();
      else updateLiveCard();
      return;
    }
    if (wasRunning) {
      // 这一轮结束了：状态文件这时候已经写完，重取主题清单就能看到刚封的那一段。
      // 左栏的「N 个主题」也要跟着变，所以连列表一起重取。
      await loadSessions();
      await refreshSession();
      return;
    }
    const card = root.querySelector('[data-live="1"]');
    if (card) card.remove();
  }

  function startLive() {
    stopLive();
    liveTimer = window.setInterval(pollLive, LIVE_INTERVAL);
  }

  function stopLive() {
    if (liveTimer) window.clearInterval(liveTimer);
    liveTimer = null;
  }

  // ------------------------------------------------------------------
  // 左侧导航：主题与步
  // ------------------------------------------------------------------

  function renderTopicRail() {
    const rail = root.querySelector("#topic-rail");
    rail.innerHTML = "";

    if (!events.length) {
      rail.appendChild(el("p", "empty-hint", "这个会话还没有封存的主题。"));
      return;
    }

    for (const event of events) {
      const box = el("div", "topic");
      const item = el("button", "topic-item");
      item.classList.toggle("is-active", event.id === activeTopic);
      item.type = "button";

      const head = el("div", "topic-head");
      head.appendChild(
        el("span", "topic-index", String(event.topicIndex || 0).padStart(2, "0"))
      );
      head.appendChild(el("span", "topic-title", event.title || "（没有标题）"));
      if (event.status === "failed") {
        head.appendChild(el("span", "topic-flag", "失败"));
      } else if (event.status === "interrupted") {
        head.appendChild(el("span", "topic-flag is-quiet", "中断"));
      } else if (event.issues.length) {
        head.appendChild(el("span", "topic-flag", `${event.issues.length} 未决`));
      }
      item.appendChild(head);

      const bits = [`${event.steps.length} 步`];
      if (event.artifacts.length) bits.push(`${event.artifacts.length} 个产物`);
      bits.push(formatClock(event.createdAt));
      item.appendChild(el("div", "topic-meta", bits.join(" · ")));

      item.addEventListener("click", () => {
        if (railOpen.has(event.id)) railOpen.delete(event.id);
        else railOpen.add(event.id);
        activeTopic = event.id;
        // 左栏是导航：点一条就把正文那一章打开并滚过去。
        if (!topicOpen(event.id)) {
          manualOpen.add(event.id);
          manualClosed.delete(event.id);
          saveOpenState();
        }
        fillTopicBody(event);
        renderTopicRail();
        const card = root.querySelector(`.topic-card[data-event="${event.id}"]`);
        if (card) card.scrollIntoView({ block: "start", behavior: "smooth" });
      });
      box.appendChild(item);

      if (railOpen.has(event.id) && event.steps.length) {
        const list = el("div", "topic-steps");
        for (const step of event.steps) {
          const row = el("button", "step-item");
          row.type = "button";
          row.appendChild(el("span", "step-seq", String(step.seq).padStart(2, "0")));
          const text = el("span", "step-text", step.intent || "（没有记录）");
          row.appendChild(text);
          if (step.issues.length) row.appendChild(el("span", "step-flag", "失败"));
          row.title = step.result || "";
          row.addEventListener("click", (clickEvent) => {
            clickEvent.stopPropagation();
            focusStep(step, event);
          });
          list.appendChild(row);
        }
        box.appendChild(list);
      }
      rail.appendChild(box);
    }
  }

  async function focusStep(step, event) {
    if (!topicOpen(event.id)) {
      manualOpen.add(event.id);
      manualClosed.delete(event.id);
      saveOpenState();
    }
    runOpen.add(event.id);
    const key = `${event.id}:${step.seq}`;
    stepOpen.add(key);
    stepOpen.delete(`${key}:closed`);
    activeTopic = event.id;
    // 段还没取回来时先等它：不等的话正文刚建出来，`[data-pos]` 还没挂上，
    // 滚动会静默地什么都不做。
    if (!segments.has(event.id)) await loadSegment(event);
    fillTopicBody(event);
    renderTopicRail();
    const card = root.querySelector(`.topic-card[data-event="${event.id}"]`);
    if (!card) return;
    const target = card.querySelector(`[data-pos="${step.messageIndex}:${step.partStart}"]`);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.classList.add("is-flash");
    window.setTimeout(() => target.classList.remove("is-flash"), 1200);
  }

  // ------------------------------------------------------------------
  // 右侧：产物
  // ------------------------------------------------------------------

  function renderArtifactPane() {
    const pane = root.querySelector("#artifact-pane");
    pane.innerHTML = "";
    pane.appendChild(el("div", "pane-title", "任务产物"));
    if (!artifacts) {
      pane.appendChild(el("p", "empty-hint", "正在读取产物…"));
      return;
    }

    if (artifacts.runs.length) {
      pane.appendChild(el("div", "pane-section", "结构运行"));
      for (const run of artifacts.runs) {
        const link = el("a", "artifact-item");
        link.href = `#/structure/${encodeURIComponent(run.id)}`;
        link.appendChild(el("div", "artifact-title", run.name || run.id));
        link.appendChild(
          el(
            "div",
            "artifact-meta",
            `${run.pdbId || "—"} · ${run.pocketCount} 口袋 · ${run.moleculeCount} 分子`
          )
        );
        pane.appendChild(link);
      }
    }

    if (artifacts.items.length) {
      pane.appendChild(el("div", "pane-section", "登记产物"));
      for (const item of artifacts.items) {
        const box = el("div", "artifact-item");
        const head = el("div", "artifact-title");
        head.appendChild(el("span", "chip", item.handle));
        head.appendChild(el("span", "artifact-kind", ARTIFACT_KIND[item.kind] || item.kind));
        if (!item.present) head.appendChild(el("span", "artifact-gone", "文件不在了"));
        box.appendChild(head);
        box.appendChild(el("div", "artifact-summary", item.summary || item.name));
        const bits = [];
        if (item.isFile) bits.push(formatBytes(item.bytes));
        if (item.isDir) bits.push("目录");
        if (item.parents.length) bits.push(`来自 ${item.parents.join("、")}`);
        if (bits.length) box.appendChild(el("div", "artifact-meta", bits.join(" · ")));
        if (item.download) {
          const link = el("a", "artifact-download", "下载");
          link.href = item.download;
          link.setAttribute("download", "");
          box.appendChild(link);
        }
        const view = surveyView(item);
        if (view) {
          const link = el("a", "artifact-download", "结构全景");
          link.href = view;
          box.appendChild(link);
        }
        pane.appendChild(box);
      }
    }

    if (!artifacts.runs.length && !artifacts.items.length) {
      pane.appendChild(
        el("p", "empty-hint", artifacts.note || "这个会话还没有登记产物。")
      );
    }
  }

  // ------------------------------------------------------------------
  // 分析：轮、步、token、工具、主题与异常
  // ------------------------------------------------------------------

  const ANALYSIS_STATUS = {
    done: "完成",
    running: "进行中",
    failed: "失败",
    cancelled: "中断",
    "no-reply": "无回复",
  };

  const ANALYSIS_LEVEL = {
    error: ["错误", "is-error"],
    warn: ["警告", "is-warn"],
    info: ["提示", "is-info"],
  };

  // 上下文八层。名字与 `core/agent/context.py` 的 `LAYER_ORDER` 一致，
  // 颜色由 `--layer-*` 给出；顺序就是装配顺序。
  const ANALYSIS_LAYERS = [
    ["prefix", "前缀"],
    ["profile_memory", "习惯"],
    ["project_memory", "项目事实"],
    ["task", "任务"],
    ["event", "事件"],
    ["recent", "最近"],
    ["evidence", "证据"],
    ["history", "历史摘要"],
  ];

  // 旧 trace 把三类记忆合成 `memory`。只在某一步确实有值时才追加到图例，
  // 不与上面八层合并，避免把旧数据里不存在的拆分当成事实。
  const COMPAT_LAYER = ["memory", "记忆（旧口径）"];

  function syncAnalysisToggle() {
    if (!root) return;
    const button = root.querySelector("#analysis-toggle");
    if (!button) return;
    const active = detailView === "analysis";
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = focusMode;
  }

  // 地址栏跟着视图走，用 replaceState 而不是改 location.hash：
  // 改 hash 会触发 hashchange，整页重挂，刚点开的那一轮就丢了。
  function syncAnalysisUrl(view) {
    const hash = window.location.hash || "";
    const [path, queryText = ""] = hash.replace(/^#\/?/, "").split("?");
    const params = new URLSearchParams(queryText);
    if (view === "analysis") params.set("tab", "analysis");
    else params.delete("tab");
    const query = params.toString();
    const next = `#/${path}${query ? `?${query}` : ""}`;
    if (next !== hash) window.history.replaceState(null, "", next);
  }

  function toggleAnalysisView() {
    const next = detailView === "analysis" ? "conversation" : "analysis";
    setDetailView(next);
    syncAnalysisUrl(next);
  }

  function setDetailView(view) {
    detailView = view;
    if (!root) return;
    const detail = root.querySelector(".history-detail");
    if (detail) detail.classList.toggle("is-analysis", view === "analysis");
    const pane = root.querySelector("#analysis-pane");
    if (pane) pane.classList.toggle("is-hidden", view !== "analysis");
    syncAnalysisToggle();
    if (view === "analysis") loadAnalysis();
  }

  function analysisSection(title, body) {
    const box = el("section", "analysis-section");
    box.appendChild(el("h3", "analysis-title", title));
    box.appendChild(body);
    return box;
  }

  function analysisCard(label, value, note) {
    const card = el("div", "analysis-card");
    card.appendChild(el("div", "analysis-card-label", label));
    card.appendChild(el("div", "analysis-card-value", value));
    if (note) card.appendChild(el("div", "analysis-card-note", note));
    return card;
  }

  function analysisTable(head, rows) {
    const wrap = el("div", "lib-table-wrap");
    const table = el("table", "lib-table analysis-table");
    table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${rows}</tbody>`;
    wrap.appendChild(table);
    return wrap;
  }

  function turnTable(data) {
    const rows = data.turns
      .map((turn) => {
        const text = oneLine(turn.user_text, 60) || "（空消息）";
        return `<tr class="is-clickable" data-turn="${turn.index}">
          <td class="is-num">${turn.index}</td>
          <td>${escapeHtml(text)}</td>
          <td class="is-num">${secondsText(turn.seconds, data.hasTrace, turn.has_reply)}</td>
          <td class="is-num">${turn.steps.length}</td>
          <td class="is-num">${turn.tools.length}</td>
          <td class="is-num">${turn.failed || ""}</td>
          <td class="is-num">${formatNumber(turn.reasoning_chars)}</td>
          <td class="is-num">${formatNumber(turn.tokens.input)}</td>
          <td class="is-num">${formatNumber(turn.tokens.cached)}</td>
          <td class="is-num">${formatNumber(turn.tokens.uncached)}</td>
          <td class="is-num">${formatNumber(turn.tokens.output)}</td>
          <td class="is-num">${formatPct(turn.cache_hit)}</td>
          <td class="is-num">${turn.cost ? `$${turn.cost.toFixed(4)}` : "—"}</td>
          <td>${escapeHtml(ANALYSIS_STATUS[turn.status] || turn.status || "未知")}</td>
        </tr>`;
      })
      .join("");
    const wrap = analysisTable(
      `<th class="is-num">轮</th><th>用户消息</th><th class="is-num">耗时</th>
       <th class="is-num">步</th><th class="is-num">工具</th><th class="is-num">失败</th>
       <th class="is-num">推理字</th><th class="is-num">输入</th><th class="is-num">缓存</th>
       <th class="is-num">新增</th><th class="is-num">输出</th><th class="is-num">命中</th>
       <th class="is-num">费用</th><th>结束</th>`,
      rows
    );
    for (const row of wrap.querySelectorAll("tbody tr")) {
      row.addEventListener("click", () => selectTurn(Number(row.dataset.turn)));
    }
    return wrap;
  }

  function stepTable(turn) {
    if (!turn.steps.length) return el("p", "analysis-empty", "这一轮没有记下步。");
    const rows = turn.steps
      .map((step) => {
        const metric = step.metric;
        return `<tr>
          <td class="is-num">${step.no}</td>
          <td class="is-num">${step.seconds === null ? "—" : step.seconds.toFixed(1)}</td>
          <td>${step.tools.length ? escapeHtml(step.tools.join("、")) : '<span class="analysis-tag">无工具</span>'}</td>
          <td class="is-num">${formatNumber(step.reasoning)}</td>
          <td class="is-num">${metric ? formatNumber(metric.input) : "—"}</td>
          <td class="is-num">${metric ? formatNumber(metric.cached) : "—"}</td>
          <td class="is-num">${metric ? formatNumber(metric.uncached) : "—"}</td>
          <td class="is-num">${metric ? formatNumber(metric.output) : "—"}</td>
          <td class="is-num">${metric ? formatPct(metric.hit) : "—"}</td>
          <td class="is-num">${metric ? formatNumber(metric.total) : "—"}</td>
          <td class="is-num">${metric ? formatPct(metric.window) : "—"}</td>
          <td class="is-num">${metric ? formatNumber(metric.events) : "—"}</td>
          <td class="is-num">${metric ? formatNumber(metric.evidence) : "—"}</td>
          <td>${metric && metric.degraded.length ? escapeHtml(metric.degraded.join("、")) : ""}</td>
        </tr>`;
      })
      .join("");
    return analysisTable(
      `<th class="is-num">步</th><th class="is-num">耗时</th><th>工具</th>
       <th class="is-num">推理字</th><th class="is-num">输入</th><th class="is-num">缓存</th>
       <th class="is-num">新增</th><th class="is-num">输出</th><th class="is-num">命中</th>
       <th class="is-num">总 token</th><th class="is-num">预算占用</th>
       <th class="is-num">主题</th><th class="is-num">证据</th><th>降级</th>`,
      rows
    );
  }

  function toolTable(turn) {
    if (!turn.tools.length) return el("p", "analysis-empty", "这一轮没有调用工具。");
    const rows = turn.tools
      .map(
        (tool) => `<tr>
          <td class="is-mono">${escapeHtml(tool.name)}</td>
          <td>${
            tool.status === "failed"
              ? '<span class="analysis-tag is-error">失败</span>'
              : '<span class="analysis-tag is-ok">成功</span>'
          }</td>
          <td class="is-num">${formatNumber(tool.chars)}</td>
          <td class="is-mono">${escapeHtml(tool.keys.join(", "))}</td>
          <td>${escapeHtml(tool.error || tool.permission)}</td>
        </tr>`
      )
      .join("");
    return analysisTable(
      `<th>工具</th><th>状态</th><th class="is-num">输出字符</th><th>参数键</th><th>失败原因 / 许可</th>`,
      rows
    );
  }

  // ------------------------------------------------------------------
  // 轮详情里的四张图，口径与 `tools/report_session.py` 的静态报告一致：
  // 每步 token 堆叠条、缓存命中折线、上下文分层堆叠条、每步耗时柱。
  // SVG 只能用 createElementNS 建；文本一律走 textContent，不拼 innerHTML。
  // ------------------------------------------------------------------

  const SVG_NS = "http://www.w3.org/2000/svg";
  const CHART_W = 720;
  const CHART_H = 220;
  const CHART_PAD = { l: 54, r: 12, t: 12, b: 26 };

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      node.setAttribute(name, String(value));
    }
    return node;
  }

  function chartColor(name) {
    return (
      getComputedStyle(document.documentElement).getPropertyValue(name).trim() ||
      "#94a3b8"
    );
  }

  function chartAxis(svg, max, formatValue) {
    for (let i = 0; i <= 4; i++) {
      const y = CHART_PAD.t + (CHART_H - CHART_PAD.t - CHART_PAD.b) * i / 4;
      svg.appendChild(
        svgEl("line", {
          x1: CHART_PAD.l,
          y1: y,
          x2: CHART_W - CHART_PAD.r,
          y2: y,
          stroke: chartColor("--line"),
        })
      );
      const label = svgEl("text", {
        x: CHART_PAD.l - 6,
        y: y + 4,
        "text-anchor": "end",
        class: "ax",
      });
      label.textContent = formatValue(max * (1 - i / 4));
      svg.appendChild(label);
    }
  }

  function chartStepLabel(svg, x, step, index, count) {
    if (count > 18 && index % Math.ceil(count / 18) !== 0) return;
    const label = svgEl("text", {
      x,
      y: CHART_H - 8,
      "text-anchor": "middle",
      class: "ax",
    });
    label.textContent = String(step === undefined ? index + 1 : step);
    svg.appendChild(label);
  }

  function chartFrame() {
    return svgEl("svg", {
      viewBox: `0 0 ${CHART_W} ${CHART_H}`,
      class: "analysis-chart",
    });
  }

  // series 是 [key, 名称, CSS 变量名] 的列表；每行按这些键叠成一根柱。
  function chartStack(container, rows, series) {
    const n = Math.max(rows.length, 1);
    const max = Math.max(
      1,
      ...rows.map((row) =>
        series.reduce((sum, [key]) => sum + (Number(row[key]) || 0), 0)
      )
    );
    const bw = (CHART_W - CHART_PAD.l - CHART_PAD.r) / n;
    const svg = chartFrame();
    chartAxis(svg, max, (value) => formatNumber(Math.round(value)));
    rows.forEach((row, index) => {
      const x = CHART_PAD.l + index * bw + bw * 0.16;
      const w = Math.max(bw * 0.68, 1);
      let y = CHART_H - CHART_PAD.b;
      for (const [key, name, cssVar] of series) {
        const value = Number(row[key]) || 0;
        if (value <= 0) continue;
        const height = (CHART_H - CHART_PAD.t - CHART_PAD.b) * value / max;
        y -= height;
        const rect = svgEl("rect", {
          x,
          y,
          width: w,
          height,
          fill: chartColor(cssVar),
        });
        const title = svgEl("title");
        title.textContent = `第 ${row.step} 步 ${name} ${formatNumber(value)}`;
        rect.appendChild(title);
        svg.appendChild(rect);
      }
      chartStepLabel(svg, x + w / 2, row.step, index, rows.length);
    });
    container.appendChild(svg);

    const legend = el("div", "analysis-chart-legend");
    for (const [key, name] of series) {
      const item = el("span", "", name);
      item.insertBefore(el("i", `is-${key}`), item.firstChild);
      legend.appendChild(item);
    }
    container.appendChild(legend);
  }

  function chartLine(container, rows, key, label, cssVar) {
    const n = Math.max(rows.length, 1);
    const bw = (CHART_W - CHART_PAD.l - CHART_PAD.r) / n;
    const svg = chartFrame();
    chartAxis(svg, 1, (value) => value.toFixed(2));
    const points = rows.map((row, index) => {
      const x = CHART_PAD.l + index * bw + bw / 2;
      const value = Math.min(Math.max(Number(row[key]) || 0, 0), 1);
      return {
        x,
        y: CHART_PAD.t + (CHART_H - CHART_PAD.t - CHART_PAD.b) * (1 - value),
        row,
        index,
      };
    });
    if (points.length) {
      svg.appendChild(
        svgEl("polyline", {
          points: points.map((point) => `${point.x},${point.y}`).join(" "),
          fill: "none",
          stroke: chartColor(cssVar),
          "stroke-width": 2,
        })
      );
      for (const point of points) {
        const dot = svgEl("circle", {
          cx: point.x,
          cy: point.y,
          r: 3,
          fill: chartColor(cssVar),
        });
        const title = svgEl("title");
        title.textContent = `第 ${point.row.step} 步 ${label} ${formatPct(point.row[key])}`;
        dot.appendChild(title);
        svg.appendChild(dot);
        chartStepLabel(svg, point.x, point.row.step, point.index, rows.length);
      }
    }
    container.appendChild(svg);
  }

  function chartBars(container, rows, key, label, cssVar) {
    const n = Math.max(rows.length, 1);
    const max = Math.max(1, ...rows.map((row) => Number(row[key]) || 0));
    const bw = (CHART_W - CHART_PAD.l - CHART_PAD.r) / n;
    const svg = chartFrame();
    chartAxis(svg, max, (value) => value.toFixed(0));
    rows.forEach((row, index) => {
      const value = Number(row[key]) || 0;
      const x = CHART_PAD.l + index * bw + bw * 0.18;
      const w = Math.max(bw * 0.64, 1);
      const height = (CHART_H - CHART_PAD.t - CHART_PAD.b) * value / max;
      const rect = svgEl("rect", {
        x,
        y: CHART_H - CHART_PAD.b - height,
        width: w,
        height,
        fill: chartColor(cssVar),
        rx: 2,
      });
      const title = svgEl("title");
      title.textContent = `第 ${row.step} 步 ${label} ${value.toFixed(1)} 秒`;
      rect.appendChild(title);
      svg.appendChild(rect);
      chartStepLabel(svg, x + w / 2, row.step, index, rows.length);
    });
    container.appendChild(svg);
  }

  function analysisCharts(data, turn) {
    const box = el("div", "analysis-charts");
    const holders = new Map();
    for (const [id, title] of [
      ["an-chart-tokens", "每步 token"],
      ["an-chart-cache", "缓存命中"],
      ["an-chart-layers", "上下文分层"],
      ["an-chart-time", "每步耗时（秒）"],
    ]) {
      const card = el("div", "analysis-card");
      card.appendChild(el("div", "analysis-card-label", title));
      const holder = el("div", "analysis-chart-holder");
      holder.id = id;
      card.appendChild(holder);
      box.appendChild(card);
      holders.set(id, holder);
    }

    const metrics = turn.metrics || [];
    if (!data.hasTrace || !metrics.length) {
      for (const holder of holders.values()) {
        holder.appendChild(
          el("p", "analysis-empty", "这一轮没有装配记录，画不出图表。")
        );
      }
      return box;
    }

    // 分层占用嵌在 usage 里，摊平之后再画；耗时不在 metrics 里，用步表补上。
    const rows = metrics.map((metric, index) => ({
      ...metric,
      ...(metric.usage || {}),
      seconds: turn.steps[index] ? turn.steps[index].seconds || 0 : 0,
    }));

    chartStack(holders.get("an-chart-tokens"), rows, [
      ["cached", "缓存命中", "--cached"],
      ["uncached", "新增输入", "--uncached"],
    ]);
    chartLine(holders.get("an-chart-cache"), rows, "hit", "命中", "--accent");

    const layers = [...ANALYSIS_LAYERS];
    // 旧 trace 的聚合层只在确实有值时画，避免凭空多出一段。
    if (rows.some((row) => (Number(row.memory) || 0) > 0)) layers.push(COMPAT_LAYER);
    chartStack(
      holders.get("an-chart-layers"),
      rows,
      layers.map(([key, name]) => [key, name, `--layer-${key.replace(/_/g, "-")}`])
    );
    chartBars(
      holders.get("an-chart-time"),
      rows,
      "seconds",
      "耗时",
      "--layer-evidence"
    );
    return box;
  }

  // 本轮封存的主题：按封存时间落在本轮起止之间筛选，与静态报告同一口径。
  function turnTopics(data, turn) {
    const items = (data.events || []).filter(
      (event) =>
        event.created_at >= turn.started_at - 0.5 &&
        event.created_at <= turn.ended_at + 0.5
    );
    if (!items.length) {
      return el("p", "analysis-empty", "这一轮没有封存主题。");
    }
    const rows = items
      .map(
        (event) => `<tr>
          <td class="is-mono">${escapeHtml(event.id)}</td>
          <td>${escapeHtml(event.title)}</td>
          <td class="is-num">${event.steps}</td>
          <td>${escapeHtml(TOPIC_STATUS[event.status] || event.status || "—")}</td>
          <td class="is-mono">${escapeHtml((event.artifacts || []).slice(0, 6).join("、"))}</td>
        </tr>`
      )
      .join("");
    return analysisTable(
      `<th>编号</th><th>标题</th><th class="is-num">步</th><th>状态</th><th>产物</th>`,
      rows
    );
  }

  function turnDetail(data, index) {
    const turn = data.turns.find((item) => item.index === index);
    const box = el("section", "analysis-section");
    box.appendChild(el("h3", "analysis-title", `第 ${index} 轮`));
    if (!turn) {
      box.appendChild(el("p", "analysis-empty", "这一轮的记录不在本次分析里。"));
      return box;
    }
    box.appendChild(el("div", "analysis-question", turn.user_text || "（空消息）"));

    const bits = [
      ["耗时", secondsText(turn.seconds, data.hasTrace, turn.has_reply)],
      ["步", String(turn.steps.length)],
      ["工具", `${turn.tools.length}（失败 ${turn.failed}）`],
      ["推理", `${formatNumber(turn.reasoning_chars)} 字`],
      ["正文", `${formatNumber(turn.text_chars)} 字`],
      ["输入", formatNumber(turn.tokens.input)],
      ["缓存", `${formatNumber(turn.tokens.cached)}（${formatPct(turn.tokens.input ? turn.tokens.cached / turn.tokens.input : 0)}）`],
      ["输出", formatNumber(turn.tokens.output)],
      ["费用", turn.cost ? `$${turn.cost.toFixed(4)}` : "—"],
      ["结束", ANALYSIS_STATUS[turn.status] || turn.status || "未知"],
    ];
    const kv = el("div", "analysis-kv");
    for (const [label, value] of bits) {
      const item = el("span", "", `${label} `);
      item.appendChild(el("b", "", value));
      kv.appendChild(item);
    }
    box.appendChild(kv);

    if (turn.answer_text) {
      const answer = el("div", "analysis-answer");
      const cut = turn.text_chars > turn.answer_text.length;
      answer.textContent = cut ? `${turn.answer_text}…` : turn.answer_text;
      box.appendChild(answer);
    }

    box.appendChild(analysisCharts(data, turn));
    box.appendChild(el("h4", "analysis-subtitle", "逐步明细"));
    box.appendChild(stepTable(turn));
    box.appendChild(el("h4", "analysis-subtitle", "工具调用"));
    box.appendChild(toolTable(turn));
    box.appendChild(el("h4", "analysis-subtitle", "本轮主题"));
    box.appendChild(turnTopics(data, turn));
    return box;
  }

  function selectTurn(index) {
    const data = analysisCache.get(activeId);
    if (!data || !root) return;
    selectedTurn = index;
    for (const row of root.querySelectorAll(".analysis-table tbody tr[data-turn]")) {
      row.classList.toggle("is-selected", Number(row.dataset.turn) === index);
    }
    const box = root.querySelector("#an-turn-detail");
    if (!box) return;
    box.classList.remove("is-hidden");
    box.innerHTML = "";
    box.appendChild(turnDetail(data, index));
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function topicsTable(data) {
    if (!data.events.length) return el("p", "analysis-empty", "这个会话还没有封存的主题。");
    const rows = data.events
      .map(
        (event) => `<tr>
          <td class="is-mono">${escapeHtml(event.id)}</td>
          <td>${escapeHtml(event.title)}</td>
          <td class="is-num">${event.step_start}–${event.step_end}</td>
          <td>${escapeHtml(TOPIC_STATUS[event.status] || event.status || "—")}</td>
          <td class="is-num">${event.artifacts.length}</td>
          <td>${escapeHtml(oneLine(event.issues.join("；"), 120))}</td>
          <td>${event.degraded ? "本地拼" : ""}</td>
        </tr>`
      )
      .join("");
    return analysisTable(
      `<th>编号</th><th>标题</th><th class="is-num">步区间</th><th>状态</th>
       <th class="is-num">产物</th><th>问题</th><th>降级</th>`,
      rows
    );
  }

  function issuesTable(data) {
    if (!data.issues.length) return el("p", "analysis-empty", "没有发现异常。");
    const order = { error: 0, warn: 1, info: 2 };
    const items = [...data.issues].sort(
      (left, right) => (order[left.level] ?? 3) - (order[right.level] ?? 3)
    );
    const rows = items
      .map((item) => {
        const [label, className] = ANALYSIS_LEVEL[item.level] || ["提示", "is-info"];
        return `<tr>
          <td><span class="analysis-tag ${className}">${label}</span></td>
          <td>${escapeHtml(item.text)}</td>
        </tr>`;
      })
      .join("");
    return analysisTable("<th>级别</th><th>说明</th>", rows);
  }

  function analysisNote() {
    const note = el("p", "analysis-note");
    note.innerHTML = [
      "耗时取 trace 时间戳之差（用户消息写入到 turn.done），不含两次输入之间的空闲。",
      "逐步的输入、缓存、输出与费用是模型返回的真实 token；总 token 与上下文分层是装配时按字符估算的，两种口径不同，不要相加。",
      "缓存命中 = cached / input，逐步取该步的读数，整轮取各步算术平均；预算占用 = 该步输入 / 上下文窗口额度。",
      "上下文分层取每一步装配的 usage，按八层顺序堆叠；旧会话若把三类记忆合成一层 memory，会单独标成「记忆（旧口径）」，缺失的层显示 0。",
      "没有 trace 时逐步读数留空，轮数、token 与主题统计仍来自状态文件。",
    ]
      .map(escapeHtml)
      .join("<br>");
    return note;
  }

  function renderAnalysis(data) {
    const pane = root.querySelector("#analysis-pane");
    if (!pane) return;
    pane.innerHTML = "";
    const summary = data.session;

    const cards = el("div", "analysis-cards");
    cards.appendChild(analysisCard("轮数", formatNumber(summary.turns), `${summary.steps} 步`));
    cards.appendChild(
      analysisCard(
        "工具调用",
        formatNumber(summary.tools),
        `失败 ${summary.failed} 次${summary.refused ? `（收束拒绝 ${summary.refused}）` : ""}`
      )
    );
    cards.appendChild(
      analysisCard(
        "输入 token",
        formatNumber(summary.tokens.input),
        `缓存复用 ${formatPct(summary.cache_reuse)}`
      )
    );
    cards.appendChild(
      analysisCard(
        "输出 token",
        formatNumber(summary.tokens.output),
        `新增输入 ${formatNumber(summary.tokens.uncached)}`
      )
    );
    cards.appendChild(
      analysisCard("费用", `$${summary.cost.toFixed(4)}`, summary.model || "未指定模型")
    );
    cards.appendChild(
      analysisCard(
        "活跃时长",
        data.hasTrace ? formatDuration(summary.active_seconds) || "—" : "—",
        data.hasTrace
          ? summary.last_exit_reason
            ? `上次停在 ${summary.last_exit_reason}`
            : "按 trace 时间戳累加"
          : "没有 trace，读不到"
      )
    );
    pane.appendChild(cards);

    pane.appendChild(
      el(
        "p",
        "analysis-note",
        data.hasTrace
          ? `trace ${formatBytes(data.traceBytes)}，统计于 ${formatTime(data.generated_at)}。`
          : "这个会话没有 trace 记录，缓存命中、分层占用与逐步耗时都读不到；轮数、token 与主题统计来自状态文件。"
      )
    );

    pane.appendChild(analysisSection("逐轮总览", turnTable(data)));
    const detail = el("div", "analysis-turn-detail is-hidden");
    detail.id = "an-turn-detail";
    pane.appendChild(detail);
    pane.appendChild(analysisSection("主题", topicsTable(data)));
    pane.appendChild(analysisSection("异常", issuesTable(data)));
    pane.appendChild(analysisSection("口径", analysisNote()));

    if (selectedTurn) selectTurn(selectedTurn);
  }

  async function loadAnalysis() {
    if (!activeId || !root) return;
    const sessionId = activeId;
    const pane = root.querySelector("#analysis-pane");
    const cached = analysisCache.get(sessionId);
    if (cached) {
      renderAnalysis(cached);
      return;
    }
    const token = ++analysisLoadToken;
    pane.innerHTML = "";
    pane.appendChild(el("p", "empty-hint", "正在统计这个会话…"));
    try {
      const data = await fetchJson(
        `/api/sessions/${encodeURIComponent(sessionId)}/analysis`
      );
      if (token !== analysisLoadToken || !root || sessionId !== activeId) return;
      analysisCache.set(sessionId, data);
      renderAnalysis(data);
    } catch (error) {
      if (token !== analysisLoadToken || !root || sessionId !== activeId) return;
      pane.innerHTML = "";
      pane.appendChild(el("p", "empty-hint", `读取分析失败：${error.message}`));
    }
  }

  // ------------------------------------------------------------------
  // 选择与取数
  // ------------------------------------------------------------------

  async function loadDetail(sessionId, token) {
    const [list, shelf] = await Promise.all([
      fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}/events`),
      fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}/artifacts`),
    ]);
    if (token !== loadToken) return false;
    events = list.events || [];
    task = list.task || null;
    compactUpto = Number(list.compactUpto || 0);
    artifacts = shelf;
    const session = sessions.find((item) => item.id === sessionId);
    renderHead({ ...(session || {}), title: list.title || (session && session.title) || "" });
    renderTopicRail();
    renderArtifactPane();
    renderConversation();
    return true;
  }

  async function selectSession(sessionId, focusEvent, view) {
    if (view) detailView = view;
    activeId = sessionId;
    loadToken += 1;
    const token = loadToken;
    events = [];
    task = null;
    compactUpto = 0;
    artifacts = null;
    selectedTurn = 0;
    segments.clear();
    segmentLoads.clear();
    runOpen.clear();
    stepOpen.clear();
    railOpen.clear();
    seenSteps.clear();
    live = null;
    activeTopic = "";
    startLive();
    loadOpenState();
    renderList();
    chatComposer.setSession(sessionId);
    chatComposer.setBusy(false);
    root.querySelector("#chat-composer").classList.remove("is-hidden");

    root.querySelector("#session-head").classList.add("is-hidden");
    root.querySelector("#analysis-pane").classList.add("is-hidden");
    const box = root.querySelector("#conversation");
    box.innerHTML = "";
    box.appendChild(el("p", "empty-hint", "正在读取会话…"));
    root.querySelector("#topic-rail").innerHTML = "";
    renderArtifactPane();

    const session = sessions.find((item) => item.id === sessionId);
    if (!session) {
      box.innerHTML = "";
      box.appendChild(el("p", "empty-hint", "会话列表里没有这一条，先刷新列表。"));
      return;
    }

    try {
      const ok = await loadDetail(sessionId, token);
      if (!ok) return;
      // 先问一次实时快照：正在跑的时候会整块重画，重画之后再定位深链接，
      // 滚动位置才留得住。
      await pollLive();
      // 深链接点名的那一条优先：它既不看本地存的展开状态，也不等默认规则。
      if (focusEvent && events.some((item) => item.id === focusEvent)) {
        manualOpen.add(focusEvent);
        manualClosed.delete(focusEvent);
        runOpen.add(focusEvent);
        activeTopic = focusEvent;
        fillTopicBody(events.find((item) => item.id === focusEvent));
        renderTopicRail();
        const card = root.querySelector(`.topic-card[data-event="${focusEvent}"]`);
        if (card) card.scrollIntoView({ block: "start" });
      }
      await pollLive();
      setDetailView(detailView);
    } catch (error) {
      if (token !== loadToken) return;
      box.innerHTML = "";
      box.appendChild(el("p", "empty-hint", `读取这个会话失败：${error.message}`));
    }
  }

  async function refreshSession() {
    if (!activeId) return;
    loadToken += 1;
    const token = loadToken;
    // 分析里含 token 与异常统计，跑完一轮之后旧的那份就对不上了。
    analysisCache.delete(activeId);
    // 已封存的主题不会变，旧的正文段可以留着；新主题按需再取。
    try {
      if (await loadDetail(activeId, token)) {
        if (detailView === "analysis") loadAnalysis();
        return;
      }
    } catch (error) {
      if (token !== loadToken) return;
      const box = root.querySelector("#conversation");
      box.innerHTML = "";
      box.appendChild(el("p", "empty-hint", `刷新这一段失败：${error.message}`));
    }
  }

  async function loadSessions() {
    try {
      const payload = await fetchJson("/api/sessions");
      sessions = payload.sessions || [];
      renderList();
    } catch (error) {
      sessions = [];
      showListError(error.message);
    }
  }

  async function mount(container, param, query) {
    await ensureMarkdown();
    container.innerHTML = TEMPLATE;
    root = container;
    sessions = [];
    activeId = "";
    keyword = "";
    events = [];
    task = null;
    artifacts = null;
    segments.clear();
    live = null;
    loadToken = 0;
    detailView = "conversation";
    analysisCache.clear();
    analysisLoadToken = 0;
    selectedTurn = 0;
    loadChrome();

    container.querySelector("#session-search").addEventListener("input", (event) => {
      keyword = event.target.value;
      renderList();
    });
    container.querySelector("#session-refresh").addEventListener("click", async () => {
      await loadSessions();
      if (activeId) await refreshSession();
    });
    const onVisible = () => {
      if (document.visibilityState === "visible") pollLive();
    };
    document.addEventListener("visibilitychange", onVisible);

    // Esc 退出专注。输入框里按 Esc 只清输入，不该顺手把专注模式也关掉。
    const onKeydown = (event) => {
      if (event.key !== "Escape" || !focusMode) return;
      const tag = (event.target && event.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      setFocusMode(false);
    };
    document.addEventListener("keydown", onKeydown);
    applyChrome();

    chatComposer = window.vsComposer.createComposer({
      placeholder: "接着这个任务说一句……",
      minHeight: 64,
      onSubmitted: async () => {
        // 新的一轮已经开始，状态文件里的消息还没到齐：先重取列表与这一段，
        // 再让两秒一次的实时轮询把运行中的主题接上来。
        await loadSessions();
        await refreshSession();
      },
    });
    container.querySelector("#chat-composer").appendChild(chatComposer.element);

    await loadSessions();
    if (param) {
      const wanted = query && query.get("event");
      const tab = query && query.get("tab") === "analysis" ? "analysis" : "";
      await selectSession(param, wanted || "", tab);
    }

    return {
      unmount() {
        document.removeEventListener("visibilitychange", onVisible);
        document.removeEventListener("keydown", onKeydown);
        stopLive();
        // 这几项挂在 body 上，离开这一页必须摘掉：否则工作空间也会少一栏。
        document.body.classList.remove(
          "artifacts-open",
          "artifacts-w-240",
          "artifacts-w-380",
          "focus-mode"
        );
        root = null;
        loadToken += 1;
        analysisLoadToken += 1;
      },
    };
  }

  window.vsPages.history = { mount };
})();
