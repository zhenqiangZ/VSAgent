// 任务输入框：工作空间与历史页共用同一份。
//
// 提交走 `POST /api/tasks`，真正的执行发生在终端进程里——网页只把文字交过去。
// 需要确认的工具仍然在终端弹窗，所以这里不假装自己是一个完整的对话界面：
// 输入框下面那行说明就是这件事的交代。
//
// 高度随内容增长，到 320px 之后内部滚动。写死一个 rows 会让长任务被截成
// 三行，而让输入框无限长会把下面的表推走。

(function () {
  const MIN_HEIGHT = 120;
  const MAX_HEIGHT = 320;

  // 四个入口只往输入框里填模板，不直接提交：模板里留着尖括号，
  // 用户得先写清楚要做什么，再按发送。
  const QUICK = [
    {
      label: "研究",
      text: "调研靶点 <基因名>：先取它的实验结构与已知配体，再给出三条可检验的假设与各自的判据。",
    },
    {
      label: "结构",
      text: "为 <PDB 编号> 做结构准备：识别口袋、准备受体，并说明每个口袋的来源与可信度。",
    },
    {
      label: "对接",
      text: "对 <靶点> 跑一轮对接：准备受体与配体，跑完按效率指标排序，并挑出正负对照。",
    },
    {
      label: "知识库",
      text: "在本地语料库里查 <关键词>，把命中的片段与出处列出来，再说明它们支持或反驳什么。",
    },
  ];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function createComposer(options = {}) {
    const box = el("div", "composer");
    let sessionId = options.sessionId || "";
    let busy = false;
    let sending = false;
    // 会话页底部的输入框不必占 120px：那里是接着说话，不是开一件新任务。
    const minHeight = options.minHeight || MIN_HEIGHT;

    if (options.quick) {
      const quick = el("div", "composer-quick");
      for (const item of QUICK) {
        const button = el("button", "composer-chip", item.label);
        button.type = "button";
        button.title = item.text;
        button.addEventListener("click", () => {
          area.value = item.text;
          grow();
          area.focus();
        });
        quick.appendChild(button);
      }
      box.appendChild(quick);
    }

    const area = el("textarea", "composer-input");
    area.rows = 3;
    area.placeholder = options.placeholder || "交办一件事：查靶点、取结构、准备受体、跑对接……";
    box.appendChild(area);

    const foot = el("div", "composer-foot");
    const note = el(
      "span",
      "composer-note",
      "提交后任务在终端进程里运行；需要确认的工具会在终端弹窗。"
    );
    foot.appendChild(note);
    const send = el("button", "composer-send", "发送");
    send.type = "button";
    foot.appendChild(send);
    box.appendChild(foot);

    const errorLine = el("div", "composer-error is-hidden");
    box.appendChild(errorLine);

    function grow() {
      area.style.height = "auto";
      const wanted = Math.max(minHeight, Math.min(MAX_HEIGHT, area.scrollHeight));
      area.style.height = `${wanted}px`;
    }

    function applyState() {
      const locked = busy || sending;
      send.disabled = locked;
      box.classList.toggle("is-busy", busy);
      note.textContent = busy
        ? "这一轮还没结束，等它跑完再交下一件。"
        : "提交后任务在终端进程里运行；需要确认的工具会在终端弹窗。";
    }

    function showError(message) {
      errorLine.textContent = message;
      errorLine.classList.toggle("is-hidden", !message);
    }

    async function submit() {
      const text = area.value.trim();
      if (!text || busy || sending) return;
      sending = true;
      showError("");
      applyState();
      try {
        const response = await fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-VSA-Request": "1" },
          body: JSON.stringify({ text, sessionId }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          // 服务端返回的原文照登：503 与 409 的原因完全不同，
          // 页面自己编一句话会把它抹平。
          showError(`${response.status} ${payload.error || "提交失败"}`);
          return;
        }
        area.value = "";
        grow();
        if (options.onSubmitted) options.onSubmitted(payload.sessionId, Boolean(payload.created));
      } catch (error) {
        showError(`提交失败：${error.message}`);
      } finally {
        sending = false;
        applyState();
      }
    }

    area.addEventListener("input", grow);
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submit();
      }
    });
    send.addEventListener("click", submit);

    grow();
    applyState();

    return {
      element: box,
      focus() {
        area.focus();
      },
      setSession(value) {
        sessionId = value || "";
      },
      setBusy(value) {
        busy = Boolean(value);
        applyState();
      },
      get value() {
        return area.value;
      },
    };
  }

  window.vsComposer = { createComposer };
})();
