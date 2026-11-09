"""后台任务的台账：`corpus/.jobs/<id>.json`。

三件事需要它：入库、检索评测、以及以后任何跑几十秒的动作。任务状态写进文件
而不放在内存里，是为了「应用退出后任务文件仍在，重新打开显示为 interrupted」——
进程被关掉时没有人能执行清理，只能在下一次启动时按 pid 对账。

**不进 `library.db`**：索引库按设计可以删掉重建，任务记录与它没有关系；混在一起
时用户删掉 `.index` 会把「上次入库失败的原因」一起丢掉。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .. import corpus

JOBS_DIRECTORY = ".jobs"
"""任务目录名，挂在语料根下。"""

JOB_ID = re.compile(r"^job_[0-9]{14}_[0-9a-f]{6}$")
"""任务编号的形状。接口收编号时先按它挡一道路径拼接。"""

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

STATUSES = (STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_INTERRUPTED)
"""任务的全部状态。`interrupted` 只由启动对账与正常停止写入。"""


class JobStore:
    """任务的增、改、查。每个任务一个文件，写入走原子换名。"""

    def __init__(self, corpus_root: Path) -> None:
        self.root = Path(corpus_root) / JOBS_DIRECTORY

    # -- 写 ---------------------------------------------------------------

    def create(
        self,
        kind: str,
        *,
        note: str = "",
        total: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> dict:
        """新建一条任务，状态是 `running`。"""
        now = time.time()
        job_id = f"job_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(3).hex()}"
        job = {
            "id": job_id,
            "kind": kind,
            "status": STATUS_RUNNING,
            "stage": "",
            "progress": {"done": 0, "total": int(total)},
            "error": "",
            "note": note,
            "pid": os.getpid(),
            "createdAt": now,
            "updatedAt": now,
            "result": None,
            "meta": dict(meta or {}),
        }
        self._write(job)
        return job

    def update(self, job_id: str, **fields: Any) -> dict:
        """改几个字段并写盘。字段名不设白名单，调用方与读取方都在这个包里。"""
        job = self.get(job_id)
        if job is None:
            raise ValueError(f"没有这个任务：{job_id}")
        job.update(fields)
        job["updatedAt"] = time.time()
        self._write(job)
        return job

    def mark_interrupted(self) -> int:
        """把别的进程留下的 `running` 标成 `interrupted`，返回改了几条。

        启动时调一次。同一个进程里的第二个服务（判据会这么干）不该把对方正在跑的
        任务改掉，所以按 pid 区分；跨进程的存活探测在 Windows 上不可靠，
        「不是本进程的 running 一律算中断」是这里唯一能安全执行的判据。
        """
        changed = 0
        for job in self.list(limit=10_000):
            if job["status"] != STATUS_RUNNING:
                continue
            if int(job.get("pid") or 0) == os.getpid():
                continue
            self.update(
                job["id"],
                status=STATUS_INTERRUPTED,
                error="进程已退出，这一条没有跑完",
            )
            changed += 1
        return changed

    def stop_own(self) -> int:
        """服务停止时把本进程还在跑的任务标成中断，返回改了几条。

        跑任务的线程是 daemon，停不下来；能做的只有把状态写对，下次打开时
        页面不会显示一个永远不动的进度条。
        """
        changed = 0
        for job in self.list(limit=10_000):
            if job["status"] != STATUS_RUNNING:
                continue
            if int(job.get("pid") or 0) != os.getpid():
                continue
            self.update(
                job["id"],
                status=STATUS_INTERRUPTED,
                error="服务已停止，这一条没有跑完",
            )
            changed += 1
        return changed

    # -- 读 ---------------------------------------------------------------

    def get(self, job_id: str) -> dict | None:
        """按编号取一条。编号形状不对或文件不在都返回 None。"""
        if not JOB_ID.match(job_id or ""):
            return None
        path = self.root / f"{job_id}.json"
        try:
            return corpus.read_json(path)
        except (OSError, ValueError):
            return None

    def list(self, *, kind: str = "", limit: int = 50) -> list[dict]:
        """任务清单，新的在前。读不出来的文件跳过，不让一份坏记录挡住整页。"""
        if not self.root.is_dir():
            return []
        jobs: list[dict] = []
        for path in self.root.glob("job_*.json"):
            if not JOB_ID.match(path.stem):
                continue
            try:
                job = corpus.read_json(path)
            except (OSError, ValueError):
                continue
            if kind and str(job.get("kind") or "") != kind:
                continue
            jobs.append(job)
        jobs.sort(key=lambda item: (-float(item.get("createdAt") or 0), str(item["id"])))
        return jobs[:limit]

    # -- 内部 -------------------------------------------------------------

    def _write(self, job: dict) -> None:
        """写一个任务文件。目录按需建。"""
        self.root.mkdir(parents=True, exist_ok=True)
        corpus.write_json(self.root / f"{job['id']}.json", job)
