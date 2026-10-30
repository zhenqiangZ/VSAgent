"""联网搜索的凭据与地址。

搜索接口与模型接口**不是同一个东西**，尽管默认都指向 DeepSeek。所以凭据的取法
要单独写一份，顺序只有两条：

1. `VSA_SEARCH_API_KEY`（配 `VSA_SEARCH_BASE_URL`，默认 `https://api.deepseek.com`）
2. 复用 `connection.toml` 里那把 key —— **仅当它的 host 恰好是 `api.deepseek.com`**

第二条是给「我就用 DeepSeek 那一个 key」的人省一次配置。加 host 限制是因为
`connection.toml` 也可能是任何 OpenAI 兼容网关的地址：把网关的 key 发给
DeepSeek 不只是拿不到结果，而是**把一个第三方凭据泄露给了另一家服务**。

每次调用现读、不缓存：`/model` 改完之后下一个工具调用就该生效，而不是等重启。
错误信息里**绝不回显 key**——它会被回灌给模型、写进 trace、显示在界面上。

`connection.toml` 按**默认状态文件**的位置找（`~/.opencode_tui/`）。领域工具拿不到
应用侧的状态路径——它们只收登记表、任务状态这几个通道，没有「应用配置」这一项。
所以用自定义 `--state` 跑的脚本要走第一条路（环境变量），别指望读到别处的连接配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .connection import connection_path_for, load_connection
from .session_manager import default_state_path

__all__ = [
    "DEFAULT_SEARCH_BASE_URL",
    "FETCH_TIMEOUT",
    "SEARCH_ENV_BASE_URL",
    "SEARCH_ENV_KEY",
    "SEARCH_HOST",
    "SEARCH_TIMEOUT",
    "SearchConfig",
    "endpoint",
    "missing_message",
    "resolve",
]

SEARCH_ENV_KEY = "VSA_SEARCH_API_KEY"
SEARCH_ENV_BASE_URL = "VSA_SEARCH_BASE_URL"
DEFAULT_SEARCH_BASE_URL = "https://api.deepseek.com"

SEARCH_HOST = "api.deepseek.com"
"""能复用 `connection.toml` 那把 key 的唯一 host。换别的地址就不复用。"""

SEARCH_TIMEOUT = 30.0
"""搜索：30 秒。它背后是一次真正的检索，比普通 JSON 接口慢。"""

FETCH_TIMEOUT = 60.0
"""抓取：60 秒。要等目标站点返回整页正文，且官方限速只有 25 QPS。"""


@dataclass(frozen=True)
class SearchConfig:
    """一次搜索调用要的三样东西。

    `source` 只用来在报错时说明「这把 key 从哪来的」——用户手上可能有两把，
    分不清是哪一个没配好。
    """

    base_url: str
    api_key: str
    source: str

    @property
    def search_url(self) -> str:
        return endpoint(self.base_url, "/v1/tools/search")

    @property
    def fetch_url(self) -> str:
        return endpoint(self.base_url, "/v1/tools/fetch")


def resolve() -> SearchConfig | None:
    """按环境变量 → `connection.toml` 的顺序找一份能用的凭据。都没有时给 None。"""
    key = (os.environ.get(SEARCH_ENV_KEY) or "").strip()
    if key:
        base = (os.environ.get(SEARCH_ENV_BASE_URL) or "").strip() or DEFAULT_SEARCH_BASE_URL
        return SearchConfig(base_url=base, api_key=key, source=SEARCH_ENV_KEY)

    connection = load_connection(connection_path_for(default_state_path()))
    if connection is None:
        return None
    # 只认 host 恰好是 DeepSeek 的那一份，理由见模块说明。
    if (urlsplit(connection.base_url).hostname or "").lower() != SEARCH_HOST:
        return None
    return SearchConfig(
        base_url=connection.base_url,
        api_key=connection.api_key,
        source="connection.toml",
    )


def missing_message() -> str:
    """没有凭据时的说法：三条现在就能走的路，不写「请配置 API Key」这种空话。"""
    return (
        "没有可用的搜索凭据，这次不联网。三条路任选一条：\n"
        f"  1. 设环境变量 {SEARCH_ENV_KEY}=sk-…（用别的网关再加 {SEARCH_ENV_BASE_URL}）；\n"
        f"  2. 在应用里用 /model 填一个 {SEARCH_HOST} 的连接，"
        "搜索会复用那把 key（其它 host 不复用，那是另一家的凭据）；\n"
        "  3. 不联网也能查：用 search_literature 查 PubMed，或把文献放进语料库后查本地。"
    )


def endpoint(base_url: str, path: str) -> str:
    """把 `path` 接到 base 上，容忍 `.../v1` 这种写法。

    DeepSeek 的 base 有两种常见写法：`https://api.deepseek.com` 与
    `https://api.deepseek.com/v1`（后者是 OpenAI SDK 的习惯）。直接拼会得到
    `/v1/v1/tools/search`——一个 404，而错误信息完全指不到病因。
    """
    base = (base_url or DEFAULT_SEARCH_BASE_URL).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}{path}"
