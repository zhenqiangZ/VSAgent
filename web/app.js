// 外壳：hash 路由、第三方库的按需加载、两个页面共用的格式化函数。
//
// Mol* 有 3 MB，只在进入结构页面时才加载；Markdown 库 75 KB，
// 只在进入会话页面时加载。两个页面都不会为对方付下载代价。

window.vsPages = window.vsPages || {};

const VENDOR = {
  markdown: "vendor/markdown-lib.js",
  viewer: "vendor/molstar-lib.js",
};

const pendingScripts = new Map();

function loadScript(src) {
  if (pendingScripts.has(src)) return pendingScripts.get(src);
  const promise = new Promise((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = src;
    tag.addEventListener("load", () => resolve());
    tag.addEventListener("error", () => reject(new Error(`脚本加载失败：${src}`)));
    document.head.appendChild(tag);
  });
  pendingScripts.set(src, promise);
  return promise;
}

function formatTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

window.vsWeb = {
  loadScript,
  ensureMarkdown: () => loadScript(VENDOR.markdown),
  ensureViewer: () => loadScript(VENDOR.viewer),
  formatTime,
  formatNumber,
};

async function fetchJson(path) {
  const response = await fetch(path);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

window.vsWeb.fetchJson = fetchJson;

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, queryText = ""] = raw.split("?");
  const segments = path.split("/").filter((item) => item.length > 0);
  return {
    // 默认页是工作空间：任务入口与最近任务都在那一页，会话历史是它的下钻。
    page: segments[0] ? decodeURIComponent(segments[0]) : "home",
    param: decodeURIComponent(segments[1] || ""),
    segments: segments.slice(1).map((item) => decodeURIComponent(item)),
    query: new URLSearchParams(queryText),
  };
}

// 旧地址改成新地址。语料页并进知识中心之后，旧书签还指着 `#/library`；
// 让它落回会话历史会让人以为「语料没了」，直接换成知识中心。
const PAGE_ALIAS = { library: "knowledge" };

let mounted = null;
// 页面模块把状态放在自己的闭包里（`root`、`loadToken`、`sessions` 都是模块级的），
// 两个挂载同时进行会互相覆盖：前一个还在取数据，后一个已经把它的 `loadToken`
// 归零，前一个取回来就丢掉结果，中间栏停在「正在读取会话…」。
// 所以挂载串行执行，一次只跑一个。
let mountQueue = Promise.resolve();
// 已经挂好的地址。同一个地址再触发一次 hashchange 时不重挂：重挂会清空中间栏，
// 用户刚点开的那一条跟着消失。
let mountedHash = null;

function render() {
  const hash = window.location.hash;
  mountQueue = mountQueue.then(() => mountFor(hash)).catch(() => {});
  return mountQueue;
}

async function mountFor(hash) {
  // 排到的时候地址可能又变了，这一份交给后面那一次。
  if (window.location.hash !== hash) return;
  if (hash === mountedHash) return;

  const { page, param, segments, query } = parseHash();
  const alias = PAGE_ALIAS[page];
  if (alias) {
    // `replace` 不留下一条历史：用户按后退不会在两个地址之间来回跳。
    window.location.replace(`#/${alias}`);
    return;
  }
  const module = window.vsPages[page] || window.vsPages.history;

  for (const link of document.querySelectorAll(".nav-link")) {
    link.classList.toggle("is-active", link.dataset.page === page);
  }

  if (mounted && mounted.unmount) {
    mounted.unmount();
  }
  mounted = null;

  const view = document.getElementById("app-view");
  view.innerHTML = "";
  delete view.dataset.ready;
  try {
    mounted = await module.mount(view, param, query, segments);
    mountedHash = hash;
    // 判据脚本靠这一项知道「这一页已经画完」：挂载是异步的，只看 DOM 里有没有
    // 某个元素会读到上一页残留的那一份。
    view.dataset.ready = "1";
  } catch (error) {
    const box = document.createElement("div");
    box.className = "page-error";
    box.textContent = `页面加载失败：${error.message}`;
    view.appendChild(box);
  }
}

window.addEventListener("hashchange", render);

// ------------------------------------------------------------------
// 顶部搜索：会话、主题、产物三类，点一条跳过去。
//
// 长任务里找东西不该靠翻聊天记录，但这里**只搜标题与结论**：工具正文在状态
// 文件里有几 MB，逐字匹配既慢又吵（一个 "PDB" 能命中几百条）。正文检索归
// 语料库那条链。
// ------------------------------------------------------------------

const searchInput = document.getElementById("global-search");
const searchResults = document.getElementById("search-results");
let searchTimer = 0;
let searchSerial = 0;

function closeSearch() {
  searchResults.classList.add("is-hidden");
  searchResults.innerHTML = "";
}

function searchGroup(title, items, builder) {
  if (!items.length) return null;
  const group = document.createElement("div");
  group.className = "search-group";
  const head = document.createElement("div");
  head.className = "search-group-title";
  head.textContent = `${title}（${items.length}）`;
  group.appendChild(head);
  for (const item of items) group.appendChild(builder(item));
  return group;
}

function searchItem(title, meta, href) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = "search-item";
  const name = document.createElement("div");
  name.className = "search-item-title";
  name.textContent = title;
  node.appendChild(name);
  if (meta) {
    const note = document.createElement("div");
    note.className = "search-item-meta";
    note.textContent = meta;
    node.appendChild(note);
  }
  node.addEventListener("click", () => {
    window.location.hash = href;
    searchInput.value = "";
    closeSearch();
  });
  return node;
}

async function runSearch(term) {
  const serial = ++searchSerial;
  let payload;
  try {
    payload = await fetchJson(`/api/search?q=${encodeURIComponent(term)}`);
  } catch (error) {
    if (serial !== searchSerial) return;
    searchResults.innerHTML = "";
    searchResults.appendChild(
      Object.assign(document.createElement("div"), {
        className: "search-empty",
        textContent: `搜索失败：${error.message}`,
      })
    );
    searchResults.classList.remove("is-hidden");
    return;
  }
  if (serial !== searchSerial) return;

  searchResults.innerHTML = "";
  const groups = [
    searchGroup("任务", payload.sessions || [], (item) =>
      searchItem(item.title, `${item.eventCount} 个主题 · ${item.messageCount} 条消息`, `#/history/${encodeURIComponent(item.id)}`)
    ),
    searchGroup("主题", payload.topics || [], (item) =>
      searchItem(
        item.title,
        `${item.sessionTitle} · ${item.eventId}`,
        `#/history/${encodeURIComponent(item.sessionId)}?event=${encodeURIComponent(item.eventId)}`
      )
    ),
    searchGroup("产物", payload.artifacts || [], (item) =>
      searchItem(
        `${item.handle} ${item.name}`,
        item.summary,
        `#/history/${encodeURIComponent(item.sessionId)}`
      )
    ),
  ].filter(Boolean);

  if (!groups.length) {
    const empty = document.createElement("div");
    empty.className = "search-empty";
    empty.textContent = `没有匹配「${payload.query}」的任务、主题或产物。`;
    searchResults.appendChild(empty);
  } else {
    for (const group of groups) searchResults.appendChild(group);
  }
  searchResults.classList.remove("is-hidden");
}

searchInput.addEventListener("input", () => {
  const term = searchInput.value.trim();
  window.clearTimeout(searchTimer);
  if (!term) {
    closeSearch();
    return;
  }
  searchTimer = window.setTimeout(() => runSearch(term), 240);
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    searchInput.value = "";
    closeSearch();
  }
});

document.addEventListener("click", (event) => {
  if (!document.getElementById("app-search").contains(event.target)) closeSearch();
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", render);
} else {
  render();
}
