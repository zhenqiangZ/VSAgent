"""包内的只读结构服务：路由、JSON 形状与后台线程。

- `routes.py` 是**接口形状的唯一真相**：路由分发、JSON 字段名、状态文件的等待读取。
  `tools/serve_web.py` 这个独立命令行脚本导入它，不另写一份——两边各写一遍时，
  改了一处忘了另一处，页面上的字段会悄悄少一个，而两边各自的判据都是绿的。
- `service.py` 把同一份路由跑在**应用进程的后台线程**里，随应用启停。

为什么服务要进包：`opencode_tui.spec` 的 `datas` 早先只有 `app.tcss`，
`tools/` 与 `web/` 都不在冻结产物里——「agent 起 `serve_web.py` 子进程」这条路
在 exe 上根本不存在。路由与静态资产都得进 `datas`，见方案稿 §0.3。
"""

from .routes import (
    SPEC,
    HttpError,
    JsonRoute,
    RouteSpec,
    WebHandler,
    WebServer,
    content_type_for,
    load_manager,
    message_json,
    part_json,
    read_settled,
    run_id_for_session,
    scan_runs,
    session_detail,
    session_summary,
)
from .service import WebService, default_port, web_root

__all__ = [
    "SPEC",
    "HttpError",
    "JsonRoute",
    "RouteSpec",
    "WebHandler",
    "WebServer",
    "WebService",
    "content_type_for",
    "default_port",
    "load_manager",
    "message_json",
    "part_json",
    "read_settled",
    "run_id_for_session",
    "scan_runs",
    "session_detail",
    "session_summary",
    "web_root",
]
