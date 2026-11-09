"""检索评测：起子进程跑 `tools/check_retrieval.py --json`，把结果记进任务台账。

**子进程**是必要的：评测要加载嵌入模型、跑几十条查询，放在服务进程里会把
网页卡住几十秒，而 HTTP 服务是纯线程模型，卡住的是所有请求。子进程退出之后
只把一份 JSON 交回来，服务这边不需要 import 任何评测代码。

评测集与参数都不在这里解释：`--questions` / `--model` 原样交给脚本，
读的是 `corpus/eval/questions.jsonl` 那一份。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from ..jobs import JobStore

SCRIPT = Path("tools") / "check_retrieval.py"
"""评测脚本相对项目根的位置。打包产物里没有它，那种安装下评测会明确失败。"""

TIMEOUT = 1800
"""一次评测最多跑半小时。五十五道题加上模型加载，正常在几分钟内结束。"""


def start_run(
    project_root: Path,
    jobs: JobStore,
    *,
    questions: str = "",
    model: str = "",
    rerank: bool = False,
) -> dict:
    """起一次评测，返回任务记录。调用方把 `id` 交给页面轮询。"""
    script = Path(project_root) / SCRIPT
    if not script.is_file():
        raise ValueError(f"这个安装里没有评测脚本：{script}")
    arguments: list[str] = ["--json"]
    if questions:
        arguments.extend(["--questions", questions])
    if model:
        arguments.extend(["--model", model])
    if rerank:
        arguments.append("--rerank")
    job = jobs.create(
        "evaluation",
        note="检索评测",
        total=1,
        meta={"questions": questions, "model": model, "rerank": rerank},
    )
    threading.Thread(
        target=_run,
        args=(jobs, job["id"], Path(project_root), arguments),
        name="vsagent-evaluation",
        daemon=True,
    ).start()
    return job


def _run(jobs: JobStore, job_id: str, project_root: Path, arguments: list[str]) -> None:
    """跑完一次评测并写状态。子进程失败时把末行输出写进 `error`。

    **门禁没过不算任务失败**：脚本跑完、JSON 交得回来，就是一份有效的报告，
    状态记 `done`，三条门禁的结论在 `result.ok` 与 `result.gates` 里。
    只有「没跑起来」「输出不是 JSON」才算 `failed`——那是这一条任务本身没完成。
    """
    jobs.update(job_id, stage="加载模型与评测集")
    try:
        completed = subprocess.run(
            [sys.executable, str(project_root / SCRIPT), *arguments],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        jobs.update(
            job_id,
            status="failed",
            stage="超时",
            error=f"评测超过 {TIMEOUT // 60} 分钟没有结束",
        )
        return
    except OSError as error:
        jobs.update(job_id, status="failed", stage="启动失败", error=str(error))
        return

    # 先解析再看待退出码：门禁没过时脚本回 1，但报告是完整的，
    # 那份 JSON 就是任务的结果。
    payload: Any = None
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        payload = None
    if payload is None:
        detail = _last_line(completed.stderr) or _last_line(completed.stdout) or "没有输出"
        jobs.update(
            job_id,
            status="failed",
            stage="失败",
            error=f"退出码 {completed.returncode}：{detail}",
        )
        return
    jobs.update(
        job_id,
        status="done",
        stage="完成",
        progress={"done": 1, "total": 1},
        result=payload,
    )


def _last_line(text: str) -> str:
    """最后一行非空输出。子进程的报错通常只有末行有用。"""
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""
