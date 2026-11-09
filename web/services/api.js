// 网页端统一的请求封装：写操作带自定义头、错误文本统一、超时统一。
//
// 写操作必须带 `X-VSA-Request: 1`：本地服务不做鉴权，而 `multipart/form-data`
// 属于 CORS 简单请求，别的网页不用预检就能往 127.0.0.1 发 POST。带上这个头
// 之后浏览器必须先预检，而服务不回 CORS 头，预检就过不去。
//
// 超时用 AbortController 而不是 `Promise.race`：后者只是不再等，请求仍然占着
// 连接；嵌入模型加载、整库重建这类动作会把连接占几十秒，页面一刷新就堆起来。

(function () {
  const WRITE_HEADER = "X-VSA-Request";
  const DEFAULT_TIMEOUT = 30000;

  function errorText(status, payload) {
    if (payload && typeof payload.error === "string" && payload.error) return payload.error;
    if (status === 0) return "请求没有发出去（服务可能已经停止）";
    return `HTTP ${status}`;
  }

  async function request(path, options = {}) {
    const {
      method = "GET",
      body,
      headers = {},
      timeout = DEFAULT_TIMEOUT,
    } = options;
    const merged = { ...headers };
    const upper = method.toUpperCase();
    if (upper !== "GET") merged[WRITE_HEADER] = "1";
    let payload = body;
    if (body !== undefined && body !== null && !(body instanceof FormData)) {
      merged["Content-Type"] = merged["Content-Type"] || "application/json";
      payload = typeof body === "string" ? body : JSON.stringify(body);
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    let response;
    try {
      response = await fetch(path, {
        method: upper,
        headers: merged,
        body: payload,
        signal: controller.signal,
      });
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error(`请求超过 ${Math.round(timeout / 1000)} 秒没有回应`);
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }

    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        data = { error: text.slice(0, 200) };
      }
    }
    if (!response.ok) throw new Error(errorText(response.status, data));
    return data;
  }

  window.vsApi = {
    request,
    get: (path, options) => request(path, { ...options, method: "GET" }),
    post: (path, body, options) => request(path, { ...options, method: "POST", body }),
  };
})();
