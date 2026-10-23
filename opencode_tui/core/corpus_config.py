"""语料的两个开关。

一个文件、两个开关、三个环境变量——**不引入第三套配置机制**（方案稿 §10）。
位置与 `tools.toml` 同目录（项目侧 `.opencode_tui/`），已 gitignore。

| 设置 | 环境变量 | 默认 | 作用 |
|---|---|---|---|
| `index_web` | `VSA_CORPUS_INDEX_WEB=0/1` | 开 | `search_web` 的摘要自动入库 |
| `embed` | `VSA_CORPUS_EMBED=0/1` | 开 | 语义通道（S3） |
| 模型名 | `VSA_EMBED_MODEL` | 见 `embed.DEFAULT_MODEL` | 换模型触发整表重建 |

环境变量优先，与 `env.py:load_env(override=False)` 同一条规则：真实环境变量压过文件。
**每次调用现读**，改完立即生效；文件缺失或写坏按默认值跑，不报错——一个开关文件
不该让整条语料链起不来。浏览器里的 `/library` 只显示当前值，不能改：那是写操作。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import corpus
from .embed import DEFAULT_MODEL as DEFAULT_EMBED_MODEL

__all__ = [
    "CORPUS_FILE",
    "DEFAULT_EMBED_MODEL",
    "ENV_EMBED",
    "ENV_EMBED_MODEL",
    "ENV_INDEX_WEB",
    "CorpusConfig",
    "config_path",
    "load",
]

CORPUS_FILE = "corpus.toml"
CONFIG_DIRECTORY = ".opencode_tui"

ENV_INDEX_WEB = "VSA_CORPUS_INDEX_WEB"
ENV_EMBED = "VSA_CORPUS_EMBED"
ENV_EMBED_MODEL = "VSA_EMBED_MODEL"


@dataclass(frozen=True)
class CorpusConfig:
    """当前生效的三个值。`source` 只在报错/显示时用来说明从哪读到的。"""

    index_web: bool = True
    embed: bool = True
    embed_model: str = DEFAULT_EMBED_MODEL
    source: str = "默认值"


def config_path(root: Path | None = None) -> Path:
    """项目侧那份配置文件的位置：`<仓根>/.opencode_tui/corpus.toml`。"""
    base = Path(root) if root is not None else corpus.corpus_root().parent
    return base / CONFIG_DIRECTORY / CORPUS_FILE


def load(root: Path | None = None) -> CorpusConfig:
    """读一次配置。文件不在、写坏了、值类型不对——都按默认值走。"""
    path = config_path(root)
    values: dict[str, object] = {}
    source = "默认值"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    else:
        source = str(path)
    table = data.get("corpus")
    if isinstance(table, dict):
        values = table

    index_web = _flag(values.get("index_web"), default=True)
    embed = _flag(values.get("embed"), default=True)
    model = str(values.get("embed_model") or "").strip() or DEFAULT_EMBED_MODEL

    env_index = _flag(os.environ.get(ENV_INDEX_WEB), default=None)
    if env_index is not None:
        index_web = env_index
        source = ENV_INDEX_WEB
    env_embed = _flag(os.environ.get(ENV_EMBED), default=None)
    if env_embed is not None:
        embed = env_embed
        source = ENV_EMBED
    env_model = (os.environ.get(ENV_EMBED_MODEL) or "").strip()
    if env_model:
        model = env_model
        source = ENV_EMBED_MODEL

    return CorpusConfig(index_web=index_web, embed=embed, embed_model=model, source=source)


def _flag(value: object, *, default: bool | None) -> bool | None:
    """开关只认几种写法。认不出来时**用默认值**，不抛——写错一个字母不该让搜索罢工。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default
