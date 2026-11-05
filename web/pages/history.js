// 会话工作台：左边会话树与主题导航，中间按主题分段的对话，右边产物。
//
// 页面不再一次拉整份会话。事件清单与正文是两条接口，正文按事件取一段：
// 一个会话的消息单元可以上百个、工具输出动辄几万字，整份发给页面时切换主题与
// 滚动都要重排整棵树；分段之后每次只搬当前主题那一段。
//
// 主题的边界来自事件层记录的切点，与模型装配时读的是同一组位置，所以页面上
// 「这一步属于哪个主题」不会与上下文里的分段对不上。

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
          <div id="conversation"></div>
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

  let sessions = [];
  let activeId = "";
  let keyword = "";
  let root = null;
  // 折叠的父会话编号。只在本次页面生命周期内有效；刷新页面回到全展开，
  // 免得昨天折叠过的那条一直藏着新派出去的子 agent。
  const collapsed = new Set();

  // 当前打开的会话。
  let events = [];
  let task = null;
  let compactUpto = 0;
  let activeEvent = "all";
  let artifacts = null;
  const segmentCache = new Map();
  const expandedTopics = new Set();
  // 换会话时旧请求可能还在路上；回来时页码对不上就不画，免得把上一个会话的
  // 内容画到这一个上。
  let loadToken = 0;

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

  function cmpPos(left, right) {
    if (left.message !== right.message) return left.message < right.message ? -1 : 1;
    if (left.part !== right.part) return left.part < right.part ? -1 : 1;
    return 0;
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

    // 目标放在头部一行：切到单个主题时任务卡不出现，而「这一轮在做什么」是
    // 读任何一段之前都要先知道的。
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
    // 窄屏上这两栏收起来，按钮放在头部；宽屏上由 CSS 藏掉这两个按钮。
    for (const [pane, label] of [["topics", "主题"], ["artifacts", "产物"]]) {
      const toggle = el("button", "pane-toggle", label);
      toggle.type = "button";
      toggle.dataset.pane = pane;
      toggle.addEventListener("click", () => {
        const box = root.querySelector(".history-detail");
        box.classList.toggle(`show-${pane}`);
      });
      actions.appendChild(toggle);
    }
    if (actions.childNodes.length) head.appendChild(actions);
  }

  // ------------------------------------------------------------------
  // 中间：对话
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
    const preview = (part.toolOutput || "").split("\n").find((line) => line.trim());
    if (preview) {
      head.appendChild(
        el("span", "tool-preview", preview.length > 90 ? `${preview.slice(0, 90)}…` : preview)
      );
    }
    box.appendChild(head);

    const body = el("details", "tool-body");
    // 失败的那一步默认打开：它就是要被看见的那一条。其余收起，需要时再点开。
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
      case "file": {
        const box = el("div", "part part-note");
        box.append("引用文件 ", el("code", "", part.filePath));
        return box;
      }
      case "agent":
        return el("div", "part part-note", `子 agent ${part.agentName}`);
      case "step_start":
        // 步分隔只在同一段里出现多次时才有信息量，样式做得很轻。
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

  function eventCard(event) {
    const card = el("div", "event-card");
    const head = el("div", "event-head");
    head.appendChild(el("span", "event-index", String(event.topicIndex || 0).padStart(2, "0")));
    head.appendChild(el("span", "event-title", event.title || "（没有标题）"));
    head.appendChild(
      el("span", `event-kind kind-${event.kind}`, EVENT_KIND[event.kind] || event.kind)
    );
    card.appendChild(head);

    const bits = [
      formatClock(event.createdAt),
      `${event.steps.length} 步`,
      `${event.artifacts.length} 个产物`,
    ];
    if (event.degraded) bits.push("标题与结论是本地拼的");
    card.appendChild(el("div", "event-meta", bits.join(" · ")));

    if (event.summary) {
      const summary = el("div", "event-summary", event.summary);
      // 结论最长能写到 600 字，整段铺开会把对话挤到一屏之外；先收成四行，
      // 要看全文的点一下。
      if (event.summary.length > 160) {
        summary.classList.add("is-clamped");
        const toggle = el("button", "link-button event-more", "展开摘要");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
          const open = summary.classList.toggle("is-clamped");
          toggle.textContent = open ? "展开摘要" : "收起摘要";
        });
        card.appendChild(summary);
        card.appendChild(toggle);
      } else {
        card.appendChild(summary);
      }
    }
    if (event.handoff) {
      const box = el("div", "event-handoff");
      box.append(el("span", "tool-label", "留给后续主题："), event.handoff);
      card.appendChild(box);
    }
    if (event.artifacts.length) {
      const chips = el("div", "event-chips");
      for (const name of event.artifacts.slice(0, 8)) {
        chips.appendChild(el("span", "chip", name));
      }
      if (event.artifacts.length > 8) {
        chips.appendChild(el("span", "chip chip-more", `还有 ${event.artifacts.length - 8} 个`));
      }
      card.appendChild(chips);
    }
    if (event.issues.length) {
      const box = el("div", "event-issues");
      box.appendChild(el("div", "tool-label", `未解决（${event.issues.length}）`));
      for (const line of event.issues.slice(0, 4)) {
        box.appendChild(el("div", "issue-line", line));
      }
      card.appendChild(box);
    }
    return card;
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

  function renderRange(messages, from, to, container) {
    let drawn = 0;
    for (const message of messages) {
      const kept = partsInRange(message, from, to);
      if (!kept.length) continue;
      for (const node of messageNode(message, kept, compactUpto)) {
        container.appendChild(node);
      }
      drawn += 1;
    }
    if (!drawn) container.appendChild(el("p", "empty-hint", "这一段没有可显示的原文。"));
  }

  function renderConversation(payload) {
    const box = root.querySelector("#conversation");
    box.innerHTML = "";

    if (activeEvent === "all") {
      box.appendChild(taskCard());
      if (!events.length) {
        renderRange(payload.messages, payload.from, payload.to, box);
        return;
      }
      for (const span of spansOf(events)) {
        box.appendChild(eventCard(span.event));
        renderRange(payload.messages, span.from, span.to, box);
      }
      // 最后一条事件之后的内容：通常是还在进行、尚未封存的那一段。
      const last = events[events.length - 1];
      if (cmpPos(last.cut, payload.to) < 0) {
        renderRange(payload.messages, last.cut, payload.to, box);
      }
      return;
    }

    const event = events.find((item) => item.id === activeEvent);
    if (event) box.appendChild(eventCard(event));
    renderRange(payload.messages, payload.from, payload.to, box);
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
  // 左侧导航：主题与步
  // ------------------------------------------------------------------

  function renderTopicRail() {
    const rail = root.querySelector("#topic-rail");
    rail.innerHTML = "";

    const all = el("button", "topic-all", "整段对话");
    all.classList.toggle("is-active", activeEvent === "all");
    all.addEventListener("click", () => selectEvent("all"));
    rail.appendChild(all);

    if (!events.length) {
      rail.appendChild(el("p", "empty-hint", "这个会话还没有封存的主题。"));
      return;
    }

    for (const event of events) {
      const box = el("div", "topic");
      const item = el("button", "topic-item");
      item.classList.toggle("is-active", event.id === activeEvent);
      item.type = "button";

      const head = el("div", "topic-head");
      head.appendChild(
        el("span", "topic-index", String(event.topicIndex || 0).padStart(2, "0"))
      );
      head.appendChild(el("span", "topic-title", event.title || "（没有标题）"));
      if (event.issues.length) {
        head.appendChild(el("span", "topic-flag", `${event.issues.length} 未决`));
      }
      item.appendChild(head);

      const bits = [`${event.steps.length} 步`];
      if (event.artifacts.length) bits.push(`${event.artifacts.length} 个产物`);
      bits.push(formatClock(event.createdAt));
      item.appendChild(el("div", "topic-meta", bits.join(" · ")));

      item.addEventListener("click", () => {
        if (expandedTopics.has(event.id)) expandedTopics.delete(event.id);
        else expandedTopics.add(event.id);
        selectEvent(event.id);
      });
      box.appendChild(item);

      if (expandedTopics.has(event.id) && event.steps.length) {
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

  function focusStep(step, event) {
    const box = root.querySelector("#conversation");
    const target = box.querySelector(`[data-pos="${step.messageIndex}:${step.partStart}"]`);
    if (!target) {
      // 这一步不在当前载入的片段里（正看另一个主题）：先切到它所属的主题再定位。
      // 主题由调用处给，不靠步号猜——每条主题的步号都从 1 起，猜不出唯一的归属。
      if (event && event.id !== activeEvent) {
        selectEvent(event.id).then(() => focusStep(step, event));
      }
      return;
    }
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

  async function selectEvent(eventId) {
    activeEvent = eventId;
    expandedTopics.add(eventId);
    renderTopicRail();
    const box = root.querySelector("#conversation");
    box.innerHTML = "";
    box.appendChild(el("p", "empty-hint", "正在读取这一段…"));

    const token = loadToken;
    let payload = segmentCache.get(eventId);
    if (!payload) {
      try {
        payload = await fetchJson(
          `/api/sessions/${encodeURIComponent(activeId)}/segment?event=${encodeURIComponent(eventId)}`
        );
      } catch (error) {
        if (token !== loadToken) return;
        box.innerHTML = "";
        box.appendChild(el("p", "empty-hint", `读取这一段失败：${error.message}`));
        return;
      }
      if (token !== loadToken) return;
      segmentCache.set(eventId, payload);
    }
    if (token !== loadToken) return;
    renderConversation(payload);
    box.scrollTop = 0;
  }

  async function selectSession(sessionId) {
    activeId = sessionId;
    loadToken += 1;
    const token = loadToken;
    events = [];
    task = null;
    compactUpto = 0;
    artifacts = null;
    segmentCache.clear();
    expandedTopics.clear();
    activeEvent = "all";
    renderList();

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
      const [list, shelf] = await Promise.all([
        fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}/events`),
        fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}/artifacts`),
      ]);
      if (token !== loadToken) return;
      events = list.events || [];
      task = list.task || null;
      compactUpto = Number(list.compactUpto || 0);
      artifacts = shelf;
      renderHead({ ...session, title: list.title || session.title });
      renderTopicRail();
      renderArtifactPane();
      // 默认打开最后一个主题：那是刚做完的一段，通常也是要接着看的地方。
      const last = events[events.length - 1];
      await selectEvent(last ? last.id : "all");
    } catch (error) {
      if (token !== loadToken) return;
      box.innerHTML = "";
      box.appendChild(el("p", "empty-hint", `读取这个会话失败：${error.message}`));
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
    activeEvent = "all";
    segmentCache.clear();
    expandedTopics.clear();
    loadToken = 0;

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
      const wanted = query && query.get("event");
      if (wanted && events.some((item) => item.id === wanted)) await selectEvent(wanted);
    }

    return { unmount() { root = null; loadToken += 1; } };
  }

  window.vsPages.history = { mount };
})();
