"""包内常驻服务：跑在应用进程的后台线程里，随应用启停。

三个决定（方案稿 §1 与 §4.2）：

- **服务是环境的一部分，不是模型的活。** Agent 不该管进程生命周期——
  让模型负责启动、复用、关闭一个常驻服务，它会在这三件事上各错一次。
- **随应用启动就起。** 任何时候要开页面都是即时的，没有「已起未起」的中间态。
- **端口被占就避让。** 上次没退干净是最常见的失败，报错退出会让用户看不出为什么。

线程模型：`ThreadingHTTPServer` 是纯线程，不走 asyncio，与 Textual 的事件循环不冲突。
`serve_forever()` 跑在 `daemon=True` 的线程里，因此主线程结束时它一定跟着结束，
不会把进程吊住——这是「退出时不必显式停也能收干净」的依据；
但应用退出仍应显式调 `stop()`，否则端口要等到进程真正退出才释放。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .routes import SPEC, RouteSpec, WebServer

DEFAULT_PORT = 8860
"""默认监听端口。`.env` 的 `VSA_WEB_PORT` 可以改。"""

PORT_TRIES = 20
"""端口被占时往后试几个。与 `export._probe_port` 试的范围必须一致。"""

HOST = "127.0.0.1"
"""只绑本机。不做鉴权，也不打算做多人访问（方案稿 §6）。"""

WEB_DIR = "web"
"""网页文件目录的名字。源仓库里在仓根，打包后跟着 `datas` 走。"""


def web_root() -> Path:
    """网页文件（`index.html` / `pages/` / `assets/`）所在目录。

    两处必须指向同一个地方：
    - **源仓库**：仓根下的 `web/`，与 `tools/serve_web.py` 的默认值一致；
    - **打包产物**：`datas` 把它放到了 `_MEIPASS` 下（见 `opencode_tui.spec`），
      冻结后 `__file__` 指不到仓根，只能按 `sys._MEIPASS` 找。

    找不到时回一个不存在的路径：服务照常起来，访问页面得到 404。
    这比直接抛异常好——`open_viewer` 仍能给出地址，问题指向的是「网页文件没打进去」，
    而那要看 `Analysis-00.toc` 才能确认（方案稿 §7.3）。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / WEB_DIR
    return Path(__file__).resolve().parents[3] / WEB_DIR


def default_port() -> int:
    """监听端口：`.env` 的 `VSA_WEB_PORT` 优先，读不到或不是数就用默认值。

    读环境变量而不是读 `.env` 文件：`app` 起服务之前已经走过 `load_env()`，
    这里再读一遍文件会让「改了 `.env` 但没重启」出现两种说法。
    """
    raw = os.environ.get("VSA_WEB_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def _free(port: int) -> bool:
    """这个端口现在能不能用（没人监听 = 能用）。

    **不能用「绑得上」来判断**。Windows 上 `SO_REUSEADDR` 的语义与 POSIX 不同：
    另一个 socket 正在该端口上 listen 时，带这个选项的 bind 照样成功。
    早先这里就是 `bind` 一试，结果「上次没退干净、端口还占着」这个最需要避让的
    场景反而一路绑过去，`WebServer` 随后在同一端口上起第二个监听——
    两个服务抢同一个端口，请求归谁处理不确定。

    这里连一次：连得上说明有人在听，换下一个；连不上（`ECONNREFUSED`）说明没人。
    与 `export._probe_port` 用的是同一种判法，两处口径一致才不会
    「服务起在一个端口上、`open_viewer` 探到另一个端口」。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.05)
        try:
            return probe.connect_ex((HOST, port)) != 0
        except OSError:
            return True


def pick_port(preferred: int | None = None, tries: int = PORT_TRIES) -> int:
    """从 `preferred`（默认 8860）起往后找第一个能绑的端口。

    先探一次再交给 `WebServer` 绑定：`HTTPServer.__init__` 绑定失败会抛
    `OSError`，而且半建好的对象没有 `server_close` 的机会。这里探过之后再绑，
    中间那段空窗理论上会被别人抢走，实际不可能（同一个进程、同一毫秒）。
    """
    start = DEFAULT_PORT if preferred is None else preferred
    for offset in range(tries):
        candidate = start + offset
        if candidate >= 65536:
            break
        if _free(candidate):
            return candidate
    raise OSError(f"从 {start} 起试了 {tries} 个端口都被占着，本地结构服务起不来")


class WebService:
    """一个跑在后台线程里的只读服务。

    用法：

        service = WebService(state_path=..., runs_dir=..., web_root=...)
        service.start()          # 返回实际端口；已在跑时原样返回
        url = service.url()      # http://127.0.0.1:<port>/
        service.stop()           # 释放端口

    `start()` 幂等：应用可能因为别的原因回头再调一次（比如切了工作目录之后），
    不该因此起来第二个监听。
    """

    def __init__(
        self,
        state_path: Path,
        runs_dir: Path,
        web_root: Path,
        port: int | None = None,
        spec: RouteSpec | None = None,
        corpus_root: Path | None = None,
        ingest: Callable[[list[Path], Path], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.runs_dir = Path(runs_dir)
        self.web_root = Path(web_root)
        self.corpus_root = None if corpus_root is None else Path(corpus_root)
        # 入库要转交给**持有写权的那个进程**。应用装配时传进来；命令行壳不传，
        # 网页上的「入库」在那里会明确回 503。
        self.ingest = ingest
        self.preferred_port = port
        self.spec = SPEC if spec is None else spec
        self._server: WebServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> int:
        """起服务并返回实际端口。已经在跑时直接把现在的端口还回去。"""
        with self._lock:
            if self._server is not None:
                return int(self._server.server_address[1])
            port = pick_port(
                self.preferred_port if self.preferred_port is not None else default_port()
            )
            server = WebServer(
                (HOST, port),
                self.state_path,
                self.runs_dir,
                self.web_root,
                self.spec,
                corpus_root=self.corpus_root,
                ingest=self.ingest,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                name="vsagent-web",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            return port

    def stop(self) -> None:
        """停服务、释放端口。没在跑时什么都不做。"""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    # -- 查询 -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        """实际监听的端口；没在跑时返回 0。

        `export._resolve_port` 按名字 `port` / `web_port` 找注入的 viewer 状态，
        这个属性就是它取到的那个数——名字改了要同步改那边。
        """
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    # 别名：`_resolve_port` 两个名字都认，这里给第二个，将来换名字不至于漏。
    web_port = port

    def url(self, run_id: str = "") -> str:
        """页面上这一份运行的地址；`run_id` 留空时是首页。"""
        base = f"http://{HOST}:{self.port}/"
        return f"{base}#/structure/{run_id}" if run_id else base

    def __enter__(self) -> WebService:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
