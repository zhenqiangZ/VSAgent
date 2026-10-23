// 语料库页面：列表 / 投递 / 改名 / 删除。
//
// 三个写操作都要带 `X-VSA-Request: 1`。那不是装饰：本地服务不做鉴权，
// 而 `multipart/form-data` 属于 CORS 简单请求，别的网页不用预检就能往
// 127.0.0.1 发 POST。带上自定义头之后浏览器必须先预检，而服务不回 CORS 头，
// 预检就过不去（详见 `core/web/routes.py` 的 `REQUEST_HEADER`）。
//
// 页面本身**不碰索引**：列表里的块数、向量覆盖都是服务端只读读出来的。

(function () {
  const { fetchJson, formatTime, formatNumber } = window.vsWeb;

  const HEADER = { "X-VSA-Request": "1" };
  const KIND_TEXT = { doc: "文献", web: "网页" };
  const DEPTH_TEXT = { full: "全文", summary: "仅摘要" };
  const STRUCTURE_TEXT = {
    outline: "书签分节",
    heading: "标题分节",
    page: "按页",
    markdown: "Markdown 分节",
  };

  const TEMPLATE = `
    <div class="library">
      <div class="lib-head">
        <div class="lib-stats" id="lib-stats">正在读取…</div>
        <div class="lib-actions">
          <button id="lib-refresh" type="button">刷新</button>
          <button id="lib-pick" type="button" class="is-primary">上传文献</button>
          <input id="lib-file" type="file" multiple accept=".pdf,.md,.txt" hidden>
        </div>
      </div>
      <p class="lib-inbox" id="lib-inbox"></p>
      <div class="lib-message" id="lib-message" hidden></div>
      <div class="lib-table-wrap">
        <table class="lib-table">
          <thead>
            <tr>
              <th>标题</th><th>类型</th><th>规格</th><th>占用</th><th>加入</th><th></th>
            </tr>
          </thead>
          <tbody id="lib-rows"></tbody>
        </table>
        <p class="empty-hint" id="lib-empty" hidden>语料库还是空的。上传一份文献，或在 TUI 里搜一次网。</p>
      </div>
      <p class="lib-foot">
        删除只是挪进 <code>corpus/.trash/</code>，可以找回；上传只落进
        <code>corpus/inbox/</code>，入库由 TUI 那一侧完成。
      </p>
    </div>

    <dialog id="lib-dialog" class="lib-dialog">
      <form method="dialog" id="lib-form">
        <h3 id="lib-dialog-title"></h3>
        <p id="lib-dialog-text" class="lib-dialog-text"></p>
        <input id="lib-dialog-input" type="text" maxlength="200" autocomplete="off">
        <div class="lib-dialog-actions">
          <button value="cancel" type="submit" formnovalidate>取消</button>
          <button value="ok" id="lib-dialog-ok" type="submit" class="is-primary">确定</button>
        </div>
      </form>
    </dialog>
  `;

  let root = null;
  let items = [];
  let dialogMode = "";
  let dialogItem = null;

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
    const box = root.querySelector("#lib-message");
    box.textContent = text;
    box.className = `lib-message is-${kind}`;
    box.hidden = false;
  }

  function spec(item) {
    const bits = [];
    if (item.pages) bits.push(`${item.pages} 页`);
    if (item.structure) bits.push(STRUCTURE_TEXT[item.structure] || item.structure);
    if (item.depth) bits.push(DEPTH_TEXT[item.depth] || item.depth);
    if (item.chunks) bits.push(`${formatNumber(item.chunks)} 块`);
    if (item.vectors) bits.push(`向量 ${formatNumber(item.vectors)}`);
    if (!bits.length && item.chars) bits.push(`${formatNumber(item.chars)} 字`);
    return bits.join(" · ") || "—";
  }

  function rowNode(item) {
    const row = el("tr");
    row.dataset.id = item.id;

    const title = el("td", "lib-title");
    const line = el("div", "lib-title-line");
    line.appendChild(el("span", "lib-title-text", item.title));
    if (item.displayTitle) {
      const mark = el("span", "lib-badge", "已改名");
      mark.title = `原始标题：${item.rawTitle || item.ref}`;
      line.appendChild(mark);
    }
    if (item.status !== "ok") {
      line.appendChild(el("span", "lib-badge is-warn", "元数据缺失"));
    }
    title.appendChild(line);
    if (item.locator) {
      const loc = el("div", "lib-locator", item.locator);
      loc.title = item.locator;
      title.appendChild(loc);
    }
    row.appendChild(title);

    row.appendChild(el("td", "lib-kind", KIND_TEXT[item.kind] || item.kind));
    row.appendChild(el("td", "lib-spec", spec(item)));
    row.appendChild(el("td", "lib-size", formatBytes(item.size)));
    row.appendChild(el("td", "lib-time", formatTime(item.addedAt)));

    const actions = el("td", "lib-row-actions");
    const rename = el("button", "link-button", "改名");
    rename.type = "button";
    rename.addEventListener("click", () => openDialog("rename", item));
    const remove = el("button", "link-button is-danger", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => openDialog("delete", item));
    actions.append(rename, remove);
    row.appendChild(actions);
    return row;
  }

  function render(payload) {
    items = payload.items || [];
    const rows = root.querySelector("#lib-rows");
    rows.innerHTML = "";
    for (const item of items) rows.appendChild(rowNode(item));
    root.querySelector("#lib-empty").hidden = items.length > 0;

    const stats = payload.stats || {};
    const bits = [
      `语料 ${items.length} 份`,
      `${formatNumber(stats.chunks || 0)} 块`,
      formatBytes(stats.bytes),
      `查询归档 ${formatNumber((payload.archive || {}).count || 0)} 条`,
      (payload.config || {}).indexWeb ? "搜索结果自动入库" : "搜索结果不入库",
      (payload.config || {}).embed ? "语义通道开" : "语义通道关",
    ];
    root.querySelector("#lib-stats").textContent = bits.join(" · ");

    const inbox = payload.inbox || { count: 0 };
    const note = root.querySelector("#lib-inbox");
    if (inbox.count) {
      note.textContent =
        `待入库 ${inbox.count} 份：${(inbox.names || []).join("、")}。` +
        "在 TUI 里让模型 import_documents 指向 corpus/inbox/ 即可入库。";
      note.hidden = false;
    } else {
      note.textContent = "";
      note.hidden = true;
    }
  }

  async function reload() {
    const payload = await fetchJson("/api/library");
    render(payload);
    return payload;
  }

  async function upload(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const form = new FormData();
    for (const file of files) form.append("file", file, file.name);
    const response = await fetch("/api/library/upload", {
      method: "POST",
      headers: HEADER,
      body: form,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const names = (payload.saved || []).filter((item) => item.saved).map((item) => item.name);
    const rejected = (payload.saved || []).filter((item) => !item.saved);
    let text = `已投递 ${names.length} 份：${names.join("、")}。${payload.next || ""}`;
    if (rejected.length) {
      text += ` 未接收 ${rejected.length} 份（${rejected[0].reason}）。`;
    }
    say(text, rejected.length && !names.length ? "warn" : "ok");
    await reload();
  }

  function openDialog(mode, item) {
    dialogMode = mode;
    dialogItem = item;
    const dialog = root.querySelector("#lib-dialog");
    const input = root.querySelector("#lib-dialog-input");
    root.querySelector("#lib-dialog-title").textContent = mode === "rename" ? "改显示名" : "删除这一份？";
    if (mode === "rename") {
      root.querySelector("#lib-dialog-text").textContent =
        `只改网页上显示的名字，不动原件、不动索引。清空则回到原始标题。`;
      input.value = item.displayTitle || item.title || "";
      input.hidden = false;
      root.querySelector("#lib-dialog-ok").textContent = "保存";
      dialog.showModal();
      input.focus();
      input.select();
    } else {
      root.querySelector("#lib-dialog-text").textContent =
        `${item.title}（${item.ref}）会被挪进 corpus/.trash/，可以找回；` +
        "检索里会立刻查不到它。";
      input.hidden = true;
      root.querySelector("#lib-dialog-ok").textContent = "删除";
      dialog.showModal();
    }
  }

  async function submitDialog(value) {
    if (!dialogItem) return;
    const id = dialogItem.id;
    const path = `/api/library/${encodeURIComponent(id)}/`;
    try {
      if (dialogMode === "rename") {
        const response = await fetch(`${path}rename`, {
          method: "POST",
          headers: { ...HEADER, "Content-Type": "application/json" },
          body: JSON.stringify({ title: value }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        say(value ? `已改名为「${payload.title}」。` : "已恢复原始标题。");
      } else {
        const response = await fetch(`${path}delete`, {
          method: "POST",
          headers: { ...HEADER, "Content-Type": "application/json" },
          body: "{}",
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        say(payload.deleted ? `已挪进 ${payload.trash}，可以找回。` : "这一份已经不在了。");
      }
      await reload();
    } catch (error) {
      say(`操作失败：${error.message}`, "warn");
    }
  }

  async function mount(container) {
    container.innerHTML = TEMPLATE;
    root = container;

    const input = container.querySelector("#lib-file");
    container.querySelector("#lib-pick").addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
      try {
        await upload(input.files);
      } catch (error) {
        say(`上传失败：${error.message}`, "warn");
      } finally {
        input.value = "";
      }
    });
    container.querySelector("#lib-refresh").addEventListener("click", async () => {
      try {
        await reload();
        say("已刷新。");
      } catch (error) {
        say(`刷新失败：${error.message}`, "warn");
      }
    });

    // 拖进来就投递：这一页本来就是投递口。
    container.addEventListener("dragover", (event) => {
      event.preventDefault();
      container.classList.add("is-dragging");
    });
    container.addEventListener("dragleave", () => container.classList.remove("is-dragging"));
    container.addEventListener("drop", async (event) => {
      event.preventDefault();
      container.classList.remove("is-dragging");
      try {
        await upload(event.dataTransfer.files);
      } catch (error) {
        say(`上传失败：${error.message}`, "warn");
      }
    });

    const dialog = container.querySelector("#lib-dialog");
    const form = container.querySelector("#lib-form");
    dialog.addEventListener("close", () => {
      if (dialog.returnValue === "ok") {
        const inputNode = container.querySelector("#lib-dialog-input");
        submitDialog(inputNode.hidden ? "" : inputNode.value.trim());
      }
      dialogMode = "";
      dialogItem = null;
    });
    form.addEventListener("submit", (event) => {
      // 回车直接保存；`method="dialog"` 会把 returnValue 交给 close 事件。
      if (event.submitter && event.submitter.value === "cancel") return;
      event.preventDefault();
      dialog.close("ok");
    });

    try {
      await reload();
    } catch (error) {
      say(`读取语料库失败：${error.message}`, "warn");
    }

    return { unmount() { root = null; } };
  }

  window.vsPages.library = { mount };
})();
