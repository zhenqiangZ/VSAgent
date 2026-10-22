from __future__ import annotations

import os
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

CONNECTION_FILE = "connection.toml"
CONNECTION_VERSION = 1


@dataclass(frozen=True)
class Connection:
    """模型接口的连接参数。

    前三个字段是必填项，也就是 `/model` 表单上那三行；后面几个是可选项，
    由清单外的模型用来补上下文窗口与单价。`None` 表示「没写」，
    与写成 0 是两件事：没写时按模型清单或兜底值算，写了就以写的为准。
    """

    base_url: str
    api_key: str
    model: str
    context_window: int | None = None
    price_in: float | None = None
    price_out: float | None = None

    def complete(self) -> bool:
        """三项必填是否都填了。"""
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())


def connection_path_for(state_path: Path) -> Path:
    """连接配置的位置：与状态文件同目录。

    放在一起是因为它俩是同一份「这台机器上的应用状态」；测试给一个隔离的
    `state_path` 也就同时隔离了连接配置，不会读到上一次跑剩下的那一份。
    """
    return state_path.parent / CONNECTION_FILE


def load_connection(path: Path) -> Connection | None:
    """读连接配置。文件不存在、读不动或缺少必填项时返回 None。

    这里不抛异常：配置坏了应该让应用照常起来，由 `/model` 重新填一遍，
    而不是在启动时就退出去，连改的机会都没有。
    """
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    connection = Connection(
        base_url=str(data.get("base_url", "")).strip(),
        api_key=str(data.get("api_key", "")).strip(),
        model=str(data.get("model", "")).strip(),
        context_window=_optional_int(data.get("context_window")),
        price_in=_optional_float(data.get("price_in")),
        price_out=_optional_float(data.get("price_out")),
    )
    return connection if connection.complete() else None


def save_connection(path: Path, connection: Connection) -> Path:
    """把连接配置写到磁盘，返回写入路径。

    与状态文件一样先写临时文件再换名，中途断电不会留下半截 TOML。
    文件里有密钥，所以权限收到 0600；Windows 上 chmod 基本是空操作，
    那边的隔离靠用户目录本身的 ACL。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": CONNECTION_VERSION,
        "base_url": connection.base_url,
        "api_key": connection.api_key,
        "model": connection.model,
    }
    if connection.context_window:
        payload["context_window"] = connection.context_window
    if connection.price_in is not None:
        payload["price_in"] = connection.price_in
    if connection.price_out is not None:
        payload["price_out"] = connection.price_out
    staged = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    with staged.open("wb") as handle:
        tomli_w.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(staged, 0o600)
    except OSError:
        # 有些文件系统（以及 Windows）不接受这个位，能用就行。
        pass
    os.replace(staged, path)
    return path


def _optional_int(value: object) -> int | None:
    """把 TOML 里的数转成正整数，空值或转不了时当作没写。"""
    if value is None or value == "":
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_float(value: object) -> float | None:
    """把 TOML 里的数转成非负浮点数，空值或转不了时当作没写。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
