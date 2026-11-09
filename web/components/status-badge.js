// 状态标签：一个圆点加一句话，颜色由 `is-<status>` 决定。
//
// 状态名沿用服务端返回的那一套（`ok` / `running` / `done` / `failed` /
// `interrupted` / `archived` / `superseded` / `expired` / `missing` / `stale`），
// 页面不再另起一套词。认不出的状态按中性色画，文字原样显示——多一个状态时页面
// 不会因为查不到映射而变成空白。

(function () {
  window.vsComponents = window.vsComponents || {};

  const STATUS_TEXT = {
    ok: "正常",
    running: "进行中",
    done: "完成",
    failed: "失败",
    interrupted: "中断",
    archived: "已归档",
    superseded: "已被取代",
    expired: "已过期",
    missing: "引用丢失",
    stale: "引用已变",
    "bad-meta": "元数据缺失",
  };

  function statusBadge(status, text) {
    const key = String(status || "ok");
    const node = document.createElement("span");
    node.className = `status-badge is-${key.replace(/[^a-z0-9-]/g, "")}`;
    node.dataset.status = key;
    const dot = document.createElement("span");
    dot.className = "status-badge-dot";
    dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "status-badge-text";
    label.textContent = text || STATUS_TEXT[key] || key;
    node.append(dot, label);
    return node;
  }

  window.vsComponents.statusBadge = statusBadge;
  window.vsComponents.STATUS_TEXT = STATUS_TEXT;
})();
