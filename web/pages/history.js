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
  let activeTopic = "";
  let live = null;
  let liveTimer = null;
  const segmentLoads = new Map();
  // 换会话时旧请求可能还在路上；回来时页码对不上就不画，免得把上一个会话的
  // 内容画到这一个上。
  let loadToken = 0;

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
    for (const button of root.querySelectorAll(".pane-toggle, #artifact-toggle")) {
      // 专注模式下两个抽屉按钮都不能用：打开抽屉就等于退出专注，
      // 而退出专注有专门的按钮与 Esc。
      button.disabled = focusMode;
    }
    const focus = root.querySelector("#focus-toggle");
    if (focus) {
      focus.textContent = focusMode ? "退出专注" : "专注";
      focus.setAttribute("aria-pressed", focusMode ? "true" : "false");
    }
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
      item.appendChild(el("div", "session-meta", bits.join(" · ")));
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
        details.appendChild(el("summary", "", "这一轮停在哪儿"));
        details.appendChild(el("div", "reason-body", part.text || ""));
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
      if (entry.download) {
        const link = el("a", "topic-artifact-name", label);
        link.href = entry.download;
        link.title = entry.path || entry.handle;
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
    for (const step of live.steps) {
      const row = el("li", `step-row is-open ${step.status === "failed" ? "is-failed" : ""}`);
      const rowHead = el("div", "step-head static");
      rowHead.appendChild(el("span", "step-no", String(step.seq).padStart(2, "0")));
      rowHead.appendChild(el("span", "step-intent", step.intent || "（没有记录）"));
      rowHead.appendChild(
        el(
          "span",
          `step-status ${step.status === "failed" ? "is-failed" : "is-done"}`,
          step.status === "failed" ? "失败" : "完成"
        )
      );
      row.appendChild(rowHead);
      if (step.result) {
        // 运行中的流水只给一行：整段结果在主题封存之后的主卡片里看。
        row.appendChild(el("div", "step-result", oneLine(step.result, 100)));
      }
      list.appendChild(row);
    }
    const current = live.current;
    if (current) {
      const row = el("li", "step-row is-open is-current");
      const rowHead = el("div", "step-head static");
      rowHead.appendChild(el("span", "step-no", String((live.steps.length || 0) + 1).padStart(2, "0")));
      const label =
        current.phase === "waiting"
          ? `等待确认：${current.tool}`
          : current.phase === "tool"
            ? `正在执行 ${current.tool}`
            : "模型正在生成";
      rowHead.appendChild(el("span", "step-intent", label));
      if (current.since && live.updatedAt) {
        const elapsed = live.updatedAt - current.since;
        if (elapsed > 0) {
          const clock = el("span", "step-time", formatDuration(elapsed));
          clock.title = "从这一次调用开始到现在";
          rowHead.appendChild(clock);
        }
      }
      rowHead.appendChild(el("span", "step-status is-running", "进行中"));
      row.appendChild(rowHead);
      if (current.preview) row.appendChild(el("div", "step-result", current.preview));
      list.appendChild(row);
    }
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
    // 步数变了才重画流水；否则每两秒重建整段会让滚动位置抖动。
    const rows = list.querySelectorAll(".step-row").length;
    const expected = live.steps.length + (live.current ? 1 : 0);
    if (rows !== expected) {
      const fresh = liveCard();
      card.replaceWith(fresh);
    }
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

  async function selectSession(sessionId, focusEvent) {
    activeId = sessionId;
    loadToken += 1;
    const token = loadToken;
    events = [];
    task = null;
    compactUpto = 0;
    artifacts = null;
    segments.clear();
    segmentLoads.clear();
    runOpen.clear();
    stepOpen.clear();
    railOpen.clear();
    live = null;
    activeTopic = "";
    startLive();
    loadOpenState();
    renderList();
    chatComposer.setSession(sessionId);
    chatComposer.setBusy(false);
    root.querySelector("#chat-composer").classList.remove("is-hidden");

    root.querySelector("#session-head").classList.add("is-hidden");
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
    // 已封存的主题不会变，旧的正文段可以留着；新主题按需再取。
    try {
      if (await loadDetail(activeId, token)) return;
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
      await selectSession(param, wanted || "");
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
      },
    };
  }

  window.vsPages.history = { mount };
})();
