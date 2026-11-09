// 右侧抽屉：语料详情、记忆详情共用。
//
// 只做「标题 + 副标题 + 一块正文 + 关闭按钮」，不内置取数：两个页面的数据来源
// 不同，抽屉要是自己去请求，它就得知道 `doc:` 与 `mem_` 两种编号各该打哪个接口。
// 正文接受节点或字符串，调用方按自己的渲染方式填。
//
// 开关只有 `open` / `close` 两个入口，状态落在 `is-open` 这个类上；页面按类名
// 决定宽度与动画，不在 JS 里另存一份。

(function () {
  window.vsComponents = window.vsComponents || {};

  function createDetailPanel(options = {}) {
    const element = document.createElement("aside");
    element.className = "detail-panel";
    element.setAttribute("aria-hidden", "true");
    element.innerHTML = `
      <div class="detail-panel-head">
        <div class="detail-panel-titles">
          <div class="detail-panel-title"></div>
          <div class="detail-panel-subtitle"></div>
        </div>
        <button class="detail-panel-close" type="button" aria-label="关闭">×</button>
      </div>
      <div class="detail-panel-body"></div>
    `;

    const titleNode = element.querySelector(".detail-panel-title");
    const subtitleNode = element.querySelector(".detail-panel-subtitle");
    const bodyNode = element.querySelector(".detail-panel-body");
    const closeButton = element.querySelector(".detail-panel-close");

    function open() {
      element.classList.add("is-open");
      element.setAttribute("aria-hidden", "false");
    }

    function close() {
      element.classList.remove("is-open");
      element.setAttribute("aria-hidden", "true");
    }

    function setTitle(text) {
      titleNode.textContent = text || "";
    }

    function setSubtitle(text) {
      subtitleNode.textContent = text || "";
      subtitleNode.hidden = !text;
    }

    function setBody(content) {
      bodyNode.innerHTML = "";
      if (content === undefined || content === null) return;
      if (typeof content === "string") {
        bodyNode.textContent = content;
        return;
      }
      bodyNode.appendChild(content);
    }

    closeButton.addEventListener("click", close);
    setTitle(options.title || "");
    setSubtitle(options.subtitle || "");

    return {
      element,
      open,
      close,
      setTitle,
      setSubtitle,
      setBody,
      isOpen: () => element.classList.contains("is-open"),
      body: bodyNode,
    };
  }

  window.vsComponents.detailPanel = createDetailPanel;
})();
