"""P1 副选：prankweb.cz 的 p2rank 口袋预测。

**这一层只在 P0 报「该蛋白一个复合物都没有」时才会被调用**（方案稿 §2.3.9）。
预测是「算出来的猜测」，有实验事实时不该拿它当主结果。

协议上的两个坑，都在这里处理（§2.3.4）：

1. **「假 201」**：接口对**任何**编号都返回 `201` + `status: "queued"`
   （实测连 `9xyz` / `8zzz` 都排队）。所以 **HTTP 码不能判「有没有结果」，只有
   `status` 能**（四态 `queued` / `running` / `successful` / `failed`）。
   照 HTTP 码判的话，每一个查不到的编号都会被当成「正在算」而无限等下去。
2. **未算过的条目要现算**（实测 `1lyz` 约 25 秒）。放进 `write_run` 会让导出卡住，
   所以那边设 15 秒短超时、超时就写 `unavailable`，另给 `/pockets refresh` 走长超时
   慢慢补齐（§7.7）。**不引后台线程池**——多一个线程池就多一套生命周期要管。

缓存放 `~/.opencode_tui/prankweb/<PDBID>.json`，**默认不自动过期**（§7.6）：
p2rank 的预测结果不会因为时间而改变，一个已算出的结构永远算过了。
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools import ToolError
from . import http

__all__ = [
    "PRANKWEB_API",
    "PredictionStatus",
    "PredictionResult",
    "PrankwebError",
    "fetch_prediction",
    "prankweb_cache_dir",
    "clear_cache",
    "cache_report",
    "CACHE_VERSION",
]

PRANKWEB_API = "https://prankweb.cz/api/v2/prediction/v3-conservation-hmm"

CACHE_VERSION = 1
"""缓存文件里的版本号。将来结果形状变了，旧缓存要能被认出来是旧的。"""

DEFAULT_POLL_LIMIT = 20
"""轮询上限（次）。**必须有上限**——不然一个永远 `queued` 的编号会把整条链路卡住。"""

DEFAULT_POLL_INTERVAL = 3.0
SHORT_TIMEOUT = 15.0
"""`write_run` 里同步查用的超时（§7.7）。超了就写 `unavailable`，不重试到底。"""

LONG_TIMEOUT = 180.0
"""`/pockets refresh` 用的长超时——现算一个条目实测约 25 秒，留足余量。"""


class PrankwebError(ToolError):
    """这一侧的接口不可用或返回的形状不对。当「该来源不可用」，不重试到底。"""


class PredictionStatus(str, enum.Enum):
    """四个状态。**只有 `SUCCESSFUL` 能取到数据。**"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    """我们这一侧没查成（网络异常、超时、缓存写不进）。**与服务端的 `failed` 分开**——
    前者「稍后可能能查」，后者「这条就是算不出来」。"""

    @classmethod
    def parse(cls, value: Any) -> "PredictionStatus":
        """把接口回的 `status` 字符串认成枚举。认不出当 `FAILED`，**不当成功**。"""
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.FAILED


@dataclass
class PredictionResult:
    """一次查询的结果。`status` 之外的一切都可能为空。"""

    pdb_id: str
    status: PredictionStatus
    payload: Any = None
    detail: str = ""
    cached: bool = False
    polls: int = 0

    @property
    def ok(self) -> bool:
        return self.status is PredictionStatus.SUCCESSFUL and self.payload is not None

    def describe(self) -> str:
        """给用户看的一句话。**把「查了没有」与「没查成」分开**（§2.3.9）。"""
        if self.status is PredictionStatus.SUCCESSFUL:
            return f"{self.pdb_id} 的循环预测拿到了"
        if self.status is PredictionStatus.UNAVAILABLE:
            return f"预测这一侧没查成：{self.detail}"
        if self.status is PredictionStatus.FAILED:
            return f"prankweb 对这个条目报失败：{self.detail or '服务端没给原因'}"
        # queued / running 到了这里说明轮询到上限了。
        return f"prankweb 一直停在「{self.status.value}」，等了 {self.polls} 次仍没好"


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def prankweb_cache_dir() -> Path:
    """按 §7.6 定的位置。"""
    return Path.home() / ".opencode_tui" / "prankweb"


def _cache_file(pdb_id: str) -> Path:
    return prankweb_cache_dir() / f"{pdb_id.upper()}.json"


def _read_cache(pdb_id: str) -> Any | None:
    """读缓存。**损坏或版本对不上就当没有**——损坏的缓存不能变成一次假结果。

    与搜索缓存不同，这里**不看时间**：p2rank 对同一个结构的预测不会变。
    """
    try:
        payload = json.loads(_cache_file(pdb_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    return payload.get("prediction")


def _write_cache(pdb_id: str, prediction: Any) -> None:
    """写缓存。**失败不影响主流程**。"""
    try:
        path = _cache_file(pdb_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"version": CACHE_VERSION, "saved_at": time.time(), "prediction": prediction},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def clear_cache(pdb_id: str | None = None) -> int:
    """清掉缓存（一个条目或全部）。返回删了几个。给 `/prankweb` 命令用。"""
    directory = prankweb_cache_dir()
    if not directory.is_dir():
        return 0
    targets = [_cache_file(pdb_id)] if pdb_id else list(directory.glob("*.json"))
    removed = 0
    for path in targets:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def cache_report() -> list[tuple[str, float]]:
    """缓存里有哪些条目、各是什么时候存的（给 `/prankweb` 命令看状态用）。"""
    directory = prankweb_cache_dir()
    if not directory.is_dir():
        return []
    rows: list[tuple[str, float]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved = float(payload.get("saved_at", 0.0))
        except (OSError, ValueError):
            saved = 0.0
        rows.append((path.stem.upper(), saved))
    return rows


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def fetch_prediction(
    pdb_id: str,
    *,
    timeout: float = SHORT_TIMEOUT,
    poll_limit: int = DEFAULT_POLL_LIMIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    use_cache: bool = True,
) -> PredictionResult:
    """取一个条目的预测。**先看缓存，再看服务端状态。** 不抛异常——失败也是一种结果。

    流程（§2.3.4）：

    1. 缓存命中 → 直接 `SUCCESSFUL`。
    2. `GET <API>/<PDBID>` 取状态（实测 10 秒内命中缓存的算过的条目）。**只看 `status`**。
    3. `successful` → `GET <API>/<PDBID>/public/prediction.json` 取数据并写缓存。
    4. `queued` / `running` → 按 `poll_limit` 轮询，到上限返回这两个状态之一。
    5. `failed` / 网络异常 → `FAILED` / `UNAVAILABLE`。**不重试到底。**

    `timeout` 是一次请求的超时，不是整条链路的总时长；`write_run` 传 15 秒，
    `/pockets refresh` 传 180 秒。
    """
    entry_id = (pdb_id or "").strip().upper()
    if not entry_id:
        return PredictionResult(
            pdb_id="", status=PredictionStatus.UNAVAILABLE, detail="没有 PDB 编号"
        )

    if use_cache:
        cached = _read_cache(entry_id)
        if cached is not None:
            return PredictionResult(
                pdb_id=entry_id, status=PredictionStatus.SUCCESSFUL, payload=cached, cached=True
            )

    polls = 0
    while True:
        polls += 1
        try:
            status_body = http.get_json(f"{PRANKWEB_API}/{entry_id}", timeout=timeout)
        except Exception as error:  # 网络 / 4xx / 5xx 一律算我们这一侧没查成
            return PredictionResult(
                pdb_id=entry_id,
                status=PredictionStatus.UNAVAILABLE,
                detail=str(error),
                polls=polls,
            )

        status = PredictionStatus.parse(_status_of(status_body))
        if status is PredictionStatus.SUCCESSFUL:
            payload = _fetch_payload(entry_id, timeout=timeout)
            if payload is None:
                return PredictionResult(
                    pdb_id=entry_id,
                    status=PredictionStatus.UNAVAILABLE,
                    detail="状态说算好了，但结果文件取不回来",
                    polls=polls,
                )
            if use_cache:
                _write_cache(entry_id, payload)
            return PredictionResult(
                pdb_id=entry_id, status=status, payload=payload, polls=polls
            )

        if status is PredictionStatus.FAILED:
            return PredictionResult(
                pdb_id=entry_id,
                status=status,
                detail=_detail_of(status_body),
                polls=polls,
            )

        # queued / running：还要等。**到上限就停**，不无限循环。
        if polls >= max(1, poll_limit):
            return PredictionResult(
                pdb_id=entry_id, status=status, detail="轮询到上限仍没算好", polls=polls
            )
        time.sleep(max(0.0, poll_interval))


def _status_of(body: Any) -> str:
    """从状态响应里取 `status`。**取不到就是取不到，不当成功。**

    实测成功的响应形状是 `{"status": "successful", ...}`；未算过的是
    `{"status": "queued", ...}`。这里只认 `status` 这一个字段——
    照 HTTP 码判会掉进「假 201」那个坑里。
    """
    if isinstance(body, dict):
        return str(body.get("status") or "")
    return ""


def _detail_of(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _fetch_payload(pdb_id: str, *, timeout: float) -> Any | None:
    """取 `prediction.json`。取不到返回 None，不抛。"""
    try:
        return http.get_json(f"{PRANKWEB_API}/{pdb_id}/public/prediction.json", timeout=timeout)
    except Exception:
        return None
