"""领域工具共用的网络出口。

**整个仓库只有这一个地方真的发起网络请求。** 把它单独抽出来，是为了让测试能
整体替换掉它——把出口散在各工具里的话，每个工具都要留一个可被 monkeypatch 的
模块级函数，替换点从一处变成七八处，而且新工具的作者得记住「要留那个函数」。

两个约定：

- **错误分两档**。4xx（编号不存在、检索式语法错）是 `ToolError`，不重试——
  重试一百次还是同一个结果。429 / 5xx / 连不上是 `RetryableToolError`，
  交给内核的重试策略。httpx 的网络异常（`ConnectError` / `TimeoutException`）
  **不是** `OSError`，内核默认那条只认 `OSError`，所以这里必须显式转换；
  不转的话断网与超时反而成了一次性的，而它们恰恰最该重试。
- **输出按 JSON 解**。这些接口（UniProt、ChEMBL）都是 JSON 接口，
  解不出来时把响应体开头几段带上，让模型知道它拿到的到底是什么。

三个出口：`get_json` / `post_json`（JSON 接口）、`get_text`（纯文本资源，如坐标文件）、
`probe`（只问有没有、不取内容，且**不按状态码抛错**——404 在那里是个答案）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from ..tools import RetryableToolError, ToolError

__all__ = [
    "DEFAULT_TIMEOUT",
    "EmptyResult",
    "NotFound",
    "TEXT_PREVIEW",
    "USER_AGENT",
    "get_json",
    "get_text",
    "get_text_or_none",
    "join_query",
    "post_json",
    "probe",
]
# 表明身份是这些公共接口的公平使用要求
USER_AGENT = "opencode-tui/0.1 (virtual screening; +https://rest.uniprot.org)"

DEFAULT_TIMEOUT = 45.0
TEXT_PREVIEW = 200


class NotFound(ToolError):
    """资源不存在（HTTP 404）。

    与其它 4xx 分开，是因为「不存在」在有些接口里是一个有用的答案而不是失败：
    RCSB 的 404 可能是「没有这个编号」，也可能是「这个条目只提供 mmCIF」，
    调用方要按不同的话去说。其余 4xx（参数语法错之类）仍然只是 ToolError。
    """


class EmptyResult(ToolError):
    """查到了、结果为空（HTTP 204 无内容）。

    **与「没查成」是两回事**，对用户的含义相反：这一条说明接口正常应答、
    只是没有符合条件的记录（照着当前条件重试永远还是空）；
    `RetryableToolError` 那条才该说「稍后重试」。

    RCSB 的属性搜就是这个约定：**零命中回 204 且不带响应体**。不单独接住的话，
    它会被当成「返回的不是 JSON」——一句听起来像接口坏了的报错，
    调用方据此写出的「没查成」正好把话反过来说。
    """


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    server_5xx_as_error: str = "",
) -> Any:
    """发一次 GET 并解析 JSON。**测试替换的就是这个函数。**

    非 JSON 的接口（比如直接取 PDB 文本）不走这里，用 `get_text`。

    `server_5xx_as_error` 填一段说明文字时，**5xx 不再算「可重试」**，而是当成
    确定性失败报出去、并把这段文字带上。ChEMBL 的相似性检索需要它：那个端点把
    「这段结构解析不了」也回成 HTTP 500，与真的服务器故障同码——照默认口径报出去，
    模型会看到「服务暂时不可用，稍后重试即可」，然后对一个写错的结构照做一遍。
    """
    response = _request(
        url, params, timeout=timeout, headers=headers, server_5xx_as_error=server_5xx_as_error
    )
    try:
        return response.json()
    except ValueError as error:
        raise ToolError(
            f"接口返回的不是 JSON（HTTP {response.status_code}）："
            f"{(response.text or '')[:TEXT_PREVIEW]}"
        ) from error


def post_json(
    url: str,
    payload: Any,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    server_5xx_as_error: str = "",
) -> Any:
    """发一次 POST（JSON 请求体）并解析 JSON。

    RCSB 的检索与 GraphQL 都是 POST——把查询条件放在请求体里，因为它在 URL 里
    会超长（属性搜的查询串动辄几 KB）。形状与 `get_json` 完全一致：
    4xx 是 `ToolError`、429/5xx 是 `RetryableToolError`、404 另有 `NotFound`。
    """
    response = _request(
        url,
        params,
        method="POST",
        payload=payload,
        timeout=timeout,
        headers=headers,
        server_5xx_as_error=server_5xx_as_error,
    )
    # 204 无内容 = 查到了、结果为空。见 `EmptyResult`：**不能**混进下面那条
    # 「返回的不是 JSON」——那句听起来像接口坏了，会把「查了没有」说成「没查成」。
    if response.status_code == 204:
        raise EmptyResult("接口正常应答，但结果为空（HTTP 204）")
    try:
        return response.json()
    except ValueError as error:
        raise ToolError(
            f"接口返回的不是 JSON（HTTP {response.status_code}）："
            f"{(response.text or '')[:TEXT_PREVIEW]}"
        ) from error


def get_text(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> str:
    """发一次 GET 并返回文本（取 PDB 这类纯文本资源用）。"""
    return _request(url, params, timeout=timeout, headers=headers).text


def get_text_or_none(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> str | None:
    """取文本，**404 返回 None 而不是抛错**。

    有的接口会把「没有这个东西」和「有、但太大/太久」编码成不同的状态码，而调用方
    需要按状态码分档给出不同的话。这一层只把 404 单独拿出来，其余失败照旧抛——
    用字符串去比对报错里有没有「404」太脆，响应体里凑巧带上这几个字符就会误判。
    """
    try:
        return _request(url, params, timeout=timeout, headers=headers).text
    except NotFound:
        return None


def probe(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> int:
    """只问「这个地址有没有东西」，把响应体丢掉，返回状态码。

    RCSB 对超出 PDB 格式上限的条目不提供 `.pdb`、只提供 mmCIF，而那些 mmCIF
    动辄几十 MB——所以判断「这条 404 是编号不存在、还是结构太大」时不能下载，
    只能探一下。**这一层刻意不按状态码抛错**：404 在这里是一个有用的答案，
    不是失败，判读留给调用方。
    """
    try:
        response = httpx.head(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    except httpx.HTTPError as error:
        raise RetryableToolError(
            f"连接失败（{type(error).__name__}：{error}）。"
            "这通常是网络或代理问题，稍后重试即可"
        ) from error
    return response.status_code


def _request(
    url: str,
    params: dict[str, Any] | None,
    *,
    method: str = "GET",
    payload: Any = None,
    timeout: float,
    headers: dict[str, str] | None,
    server_5xx_as_error: str = "",
) -> httpx.Response:
    """发请求并处理失败。成功时返回响应对象。"""
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    merged.update(headers or {})
    try:
        response = httpx.request(
            method,
            url,
            params=_clean_params(params),
            json=payload,
            timeout=timeout,
            headers=merged,
            follow_redirects=True,
        )
    except httpx.HTTPError as error:
        raise RetryableToolError(
            f"连接失败（{type(error).__name__}：{error}）。"
            "这通常是网络或代理问题，稍后重试即可"
        ) from error

    if response.status_code == 429 or response.status_code >= 500:
        if server_5xx_as_error:
            raise ToolError(
                f"{server_5xx_as_error}（HTTP {response.status_code}）：{_message(response)}"
            )
        raise RetryableToolError(
            f"接口暂时不可用（HTTP {response.status_code}）：{_message(response)}"
        )
    if response.status_code == 404:
        raise NotFound("资源不存在（HTTP 404）")
    if response.status_code >= 400:
        raise ToolError(f"接口拒绝了这次请求（HTTP {response.status_code}）：{_message(response)}")
    return response


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """去掉值为空的参数。

    值为 None 的参数会变成字符串 `"None"` 发出去，服务端收到的是
    「用户查了一个叫 None 的物种」，而报错信息完全指不到真正的原因。
    """
    if not params:
        return {}
    return {key: value for key, value in params.items() if value is not None}


def _message(response: httpx.Response) -> str:
    """把错误响应里那句能照着改的话抠出来。

    UniProt 的报错是 `{"url": ..., "messages": ["query parameter has an
    invalid syntax"]}`——`messages` 才是要给模型看的部分。整段 JSON 连同
    URL 一起糊进上下文的话，真正有用的那半句会被淹掉。

    **先挡 HTML。** ChEMBL 的 Django 路由在几种情况下回的是错误页而不是 API 消息
    （相似度阈值写成 `0.85`、分子编号写成 `gefitinib`，都是 404 加一页 HTML）。
    把那一页截一段塞进错误里，模型看到的是 `<!DOCTYPE html>` 加一串样式表，
    既读不出病因也照不了做——不如直说「这是网页，不是接口消息」。
    """
    content_type = (response.headers.get("content-type") or "").lower()
    raw = (response.text or "").lstrip()
    if "html" in content_type or raw[:15].lower().startswith(("<!doctype", "<html")):
        return "服务端回的是一页 HTML 错误页，不是接口消息（多半是路径或编号格式不对）"
    try:
        payload = response.json()
    except ValueError:
        return raw.strip()[:TEXT_PREVIEW] or "（无附加说明）"
    if isinstance(payload, dict):
        for key in ("error", "detail", "message", "messages"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:TEXT_PREVIEW]
            if isinstance(value, list) and value:
                return "；".join(str(item) for item in value)[:TEXT_PREVIEW]
    return json.dumps(payload, ensure_ascii=False)[:TEXT_PREVIEW]


def join_query(path: str, params: dict[str, Any]) -> str:
    """把参数拼进 URL。测试里核对请求地址时用得到。"""
    cleaned = _clean_params(params)
    return f"{path}?{urlencode(cleaned)}" if cleaned else path
