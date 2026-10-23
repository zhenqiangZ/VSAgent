"""独立命令行入口：起一个只读的会话历史与结构查看服务。

**这里不写路由**。路由、JSON 形状与状态文件的等待读取都在
`opencode_tui.core.web.routes` 里（唯一真相），包内的常驻服务走的是同一份。
两边各写一遍时，改了一处忘另一处，页面上的字段会悄悄少一个，
而两边各自的判据都是绿的。

这个脚本与包内服务的差别只有两点：
- 它是**独立进程**，用来在没有 TUI 的场合（人工查看、`tools/check_web_api.py`）起服务；
- 它多一个 `--port` 参数，**优先级高于 `.env` 的 `VSA_WEB_PORT`**（方案稿 §7.2）。

`--corpus` 也在两个入口上都有：判据要在一个临时语料根上跑，免得动到真实语料。

在冻结成 exe 之后这个脚本不在磁盘上——应用里跑的是包内服务，
它不是「另一个实现」，只是同一个实现的另一个壳。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opencode_tui.core.session_manager import default_state_path  # noqa: E402
from opencode_tui.core.web.routes import WebServer  # noqa: E402
from opencode_tui.core.web.service import default_port  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="会话历史与结构查看的本地只读服务")
    parser.add_argument(
        "--state",
        type=Path,
        default=default_state_path(),
        help="状态文件路径",
    )
    parser.add_argument(
        "--runs",
        type=Path,
        default=PROJECT_ROOT / "runs",
        help="结构运行目录",
    )
    parser.add_argument(
        "--web",
        type=Path,
        default=PROJECT_ROOT / "web",
        help="网页文件目录",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"监听端口（默认取 .env 的 VSA_WEB_PORT，再退到 {default_port()}）",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="语料根目录（默认按代码位置定位到仓根下的 corpus/）",
    )
    args = parser.parse_args()

    port = args.port if args.port is not None else default_port()
    server = WebServer(
        ("127.0.0.1", port),
        args.state.resolve(),
        args.runs.resolve(),
        args.web.resolve(),
        corpus_root=args.corpus.resolve() if args.corpus else None,
    )
    print(f"状态文件 {server.state_path}")
    print(f"运行目录 {server.runs_dir}")
    print(f"网页目录 {server.web_root}")
    print(f"语料目录 {server.corpus_root}")
    print(f"http://127.0.0.1:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
