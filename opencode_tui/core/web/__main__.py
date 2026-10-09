"""`python -m opencode_tui.core.web`：把包内服务当独立进程起。

应用里的服务跑在后台线程（`service.WebService`），但有两处场合需要进程级入口：
人工查看、以及 `tools/check_web_api.py` 的判据——判据要能 `terminate()` 掉服务，
线程停不掉。两条路走同一份路由（`routes.SPEC`），没有第二套接口形状。

参数与 `tools/serve_web.py` 保持一致，`--port` 同样优先于 `.env` 的 `VSA_WEB_PORT`。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .routes import WebServer
from .service import HOST, default_port


def _project_root() -> Path:
    """工程根：本文件位于 `<root>/opencode_tui/core/web/` 下。"""
    return Path(__file__).resolve().parents[3]


def main() -> None:
    root = _project_root()
    parser = argparse.ArgumentParser(description="会话历史与结构查看的本地只读服务")
    parser.add_argument("--state", type=Path, default=None, help="状态文件路径")
    parser.add_argument("--runs", type=Path, default=root / "runs", help="结构运行目录")
    parser.add_argument("--web", type=Path, default=root / "web", help="网页文件目录")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"监听端口（默认取 .env 的 VSA_WEB_PORT，再退到 {default_port()}）",
    )
    args = parser.parse_args()

    state_path = args.state
    if state_path is None:
        from ..session_manager import default_state_path

        state_path = default_state_path()

    port = args.port if args.port is not None else default_port()
    server = WebServer(
        (HOST, port),
        Path(state_path).resolve(),
        args.runs.resolve(),
        args.web.resolve(),
    )
    print(f"状态文件 {server.state_path}")
    print(f"运行目录 {server.runs_dir}")
    print(f"网页目录 {server.web_root}")
    print(f"http://{HOST}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
