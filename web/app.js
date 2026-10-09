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
  const [page, param] = raw.split("/");
  return { page: page || "history", param: decodeURIComponent(param || "") };
}

let mounted = null;

async function render() {
  const { page, param } = parseHash();
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
  try {
    mounted = await module.mount(view, param);
  } catch (error) {
    const box = document.createElement("div");
    box.className = "page-error";
    box.textContent = `页面加载失败：${error.message}`;
    view.appendChild(box);
  }
}

window.addEventListener("hashchange", render);

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", render);
} else {
  render();
}
