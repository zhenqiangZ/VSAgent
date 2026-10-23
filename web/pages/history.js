// 会话历史页面。左侧按父子层级列出会话（子 agent 缩进挂在父会话下），
// 右侧对话正文，正文按内容单元的类型分别渲染。

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
        <div id="session-body">
          <p class="empty-hint">从左侧选一个会话。</p>
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

  // 工具输出短的时候直接展开，长的时候收起来。全文都在页面里，
  // 收起来只是省地方，不影响查找。
  const OUTPUT_OPEN_LIMIT = 800;

  let sessions = [];
  let activeId = "";
  let keyword = "";
  let root = null;
  // 折叠的父会话编号。只在本次页面生命周期内有效；刷新页面回到全展开，
  // 免得昨天折叠过的那条一直藏着新派出去的子 agent。
  const collapsed = new Set();

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function partNode(part) {
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
        return toolNode(part);
      case "patch":
        return patchNode(part);
      case "file": {
        const box = el("div", "part part-note");
        box.append("引用文件 ", el("code", "", part.filePath));
        return box;
      }
      case "agent":
        return el("div", "part part-note", `子 agent ${part.agentName}`);
      case "step_start":
        return el("div", "part part-step", `步骤开始 ${part.text}`.trim());
      case "step_finish":
        return el("div", "part part-step", `步骤结束 ${part.text}`.trim());
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

  function toolNode(part) {
    const box = el("div", "part part-tool");

    const head = el("div", "tool-head");
    head.appendChild(el("span", "tool-name", part.toolName || "tool"));
    head.appendChild(
      el("span", `status status-${part.toolStatus || "pending"}`, STATUS_TEXT[part.toolStatus] || part.toolStatus || "等待")
    );
    if (part.permission) {
      head.appendChild(el("span", "tool-label", `判定 ${part.permission}`));
    }
    box.appendChild(head);

    if (part.toolInput && Object.keys(part.toolInput).length) {
      const section = el("div", "tool-section");
      section.appendChild(el("div", "tool-label", "输入"));
      section.appendChild(el("pre", "", window.vsMarkdown.renderJson(part.toolInput)));
      box.appendChild(section);
    }

    if (part.toolOutput) {
      const section = el("div", "tool-section");
      const details = el("details", "tool-output-details");
      if (part.toolOutput.length <= OUTPUT_OPEN_LIMIT) details.open = true;
      details.appendChild(el("summary", "", `输出（${part.toolOutput.length} 字符）`));
      details.appendChild(el("pre", "", part.toolOutput));
      section.appendChild(details);
      box.appendChild(section);
    }

    return box;
  }

  function patchNode(part) {
    const box = el("div", "part part-patch");
    const head = el("div", "patch-head");
    head.appendChild(el("span", "patch-path", part.filePath || part.text || "补丁"));
    head.appendChild(el("span", "patch-added", `+${part.patchAdded}`));
    head.appendChild(el("span", "patch-removed", `-${part.patchRemoved}`));
    box.appendChild(head);
    return box;
  }

  function messageNode(message, index, compactUpto) {
    const nodes = [];
    if (compactUpto > 0 && index === compactUpto) {
      nodes.push(
        el("div", "compact-mark", `此处之前的 ${compactUpto} 条消息已压缩，不再发送给模型`)
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

    for (const part of message.parts) {
      article.appendChild(partNode(part));
    }

    nodes.push(article);
    return nodes;
  }

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
      bits.push(
        `${node.messageCount} 条`,
        formatTime(node.updatedAt),
        node.modelId || "未指定模型"
      );
      item.appendChild(el("div", "session-meta", bits.join(" · ")));
      item.addEventListener("click", () => selectSession(node.id));
      list.appendChild(item);

      // 折叠只影响显示；过滤时强制展开，否则命中的子 agent 会被藏起来。
      if (!expanded) return;
      for (const child of own) appendNode(child, depth + 1);
    }

    for (const node of roots) appendNode(node, 0);

    if (!shown) {
      list.appendChild(el("li", "empty-hint", "没有匹配的会话。"));
    }
  }

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
      `Agent ${session.agentName}`,
      `${session.messageCount} 条消息`,
      `${formatNumber(session.totalTokens)} tokens`,
      `$${session.totalCost.toFixed(4)}`,
      `更新 ${formatTime(session.updatedAt)}`,
    ];
    if (session.parentId) {
      bits.unshift(`状态 ${AGENT_STATUS[session.status] || session.status || "空闲"}`);
      if (session.origin) {
        bits.push(`来源 ${session.origin === "agent" ? "模型派发" : "用户派发"}`);
      }
      if (session.statusNote) {
        bits.push(`说明 ${session.statusNote.slice(0, 60)}`);
      }
      if (Number(session.seedEnd) > Number(session.seedStart)) {
        bits.push(`区间 父会话第 ${session.seedStart + 1}–${session.seedEnd} 条`);
      }
    }
    head.appendChild(el("div", "head-meta", bits.join(" · ")));

    if (session.runId) {
      const actions = el("div", "head-actions");
      const link = el("a", "link-button", `查看这次运行的结构（${session.runId}）`);
      link.href = `#/structure/${encodeURIComponent(session.runId)}`;
      actions.appendChild(link);
      head.appendChild(actions);
    }
  }

  function renderDetail(session) {
    renderHead(session);
    const body = root.querySelector("#session-body");
    body.innerHTML = "";
    if (!session.messages.length) {
      body.appendChild(el("p", "empty-hint", "这个会话还没有消息。"));
      return;
    }
    session.messages.forEach((message, index) => {
      for (const node of messageNode(message, index, session.compactUpto)) {
        body.appendChild(node);
      }
    });
  }

  async function selectSession(sessionId) {
    activeId = sessionId;
    renderList();
    const body = root.querySelector("#session-body");
    body.innerHTML = "";
    body.appendChild(el("p", "empty-hint", "正在读取会话…"));
    const session = await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (activeId !== sessionId) return;
    renderDetail(session);
  }

  async function loadSessions() {
    const payload = await fetchJson("/api/sessions");
    sessions = payload.sessions;
    renderList();
  }

  async function mount(container, param) {
    await ensureMarkdown();
    container.innerHTML = TEMPLATE;
    root = container;
    sessions = [];
    activeId = "";
    keyword = "";

    container.querySelector("#session-search").addEventListener("input", (event) => {
      keyword = event.target.value;
      renderList();
    });
    container.querySelector("#session-refresh").addEventListener("click", async () => {
      await loadSessions();
      if (activeId) await selectSession(activeId);
    });

    await loadSessions();
    if (param) {
      await selectSession(param);
    }

    return { unmount() { root = null; } };
  }

  window.vsPages.history = { mount };
})();
