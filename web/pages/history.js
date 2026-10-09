// 会话历史页面。左侧列表、右侧对话正文，正文按内容单元的类型分别渲染。

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

  // 工具输出短的时候直接展开，长的时候收起来。全文都在页面里，
  // 收起来只是省地方，不影响查找。
  const OUTPUT_OPEN_LIMIT = 800;

  let sessions = [];
  let activeId = "";
  let keyword = "";
  let root = null;

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
        return el("div", "part part-note", `Agent ${part.agentName}`);
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

  function renderList() {
    const list = root.querySelector("#session-list");
    list.innerHTML = "";
    const needle = keyword.trim().toLowerCase();
    const shown = sessions.filter(
      (item) => !needle || item.title.toLowerCase().includes(needle)
    );

    for (const session of shown) {
      const item = document.createElement("li");
      item.dataset.sessionId = session.id;
      if (session.id === activeId) item.classList.add("is-active");

      const title = el("div", "session-title", session.title);
      if (session.runId) {
        title.appendChild(el("span", "run-mark", "有结构"));
      }
      item.appendChild(title);
      item.appendChild(
        el(
          "div",
          "session-meta",
          `${session.messageCount} 条 · ${formatTime(session.updatedAt)} · ${session.modelId || "未指定模型"}`
        )
      );
      item.addEventListener("click", () => selectSession(session.id));
      list.appendChild(item);
    }

    if (!shown.length) {
      list.appendChild(el("li", "empty-hint", "没有匹配的会话。"));
    }
  }

  function renderHead(session) {
    const head = root.querySelector("#session-head");
    head.innerHTML = "";
    head.classList.remove("is-hidden");
    head.appendChild(el("div", "head-title", session.title));

    const bits = [
      `模型 ${session.modelId || "未指定"}`,
      `Agent ${session.agentName}`,
      `${session.messageCount} 条消息`,
      `${formatNumber(session.totalTokens)} tokens`,
      `$${session.totalCost.toFixed(4)}`,
      `更新 ${formatTime(session.updatedAt)}`,
    ];
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
