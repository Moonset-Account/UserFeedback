"""采集任务管理：后台线程执行、任务状态留存、日志收集。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from selective_zip.fetcher import run_selective_fetch
from sampler.experiment_data import EXPERIMENT_ID


@dataclass
class Task:
    id: str
    params: dict[str, Any]
    status: str = "queued"  # queued / running / finished
    created_at: float = 0.0
    finished_at: Optional[float] = None
    logs: list[str] = field(default_factory=list)
    report: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "logs": list(self.logs),
            "report": self.report,
            "error": self.error,
        }


class TaskManager:
    def __init__(self, max_tasks: int = 50):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_tasks = max_tasks

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._tasks[t].to_dict() for t in reversed(self._order)]

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def create_task(self, params: dict[str, Any]) -> Task:
        task_id = uuid.uuid4().hex[:12]
        task = Task(id=task_id, params=params, created_at=time.time())
        with self._lock:
            self._tasks[task_id] = task
            self._order.append(task_id)
            if len(self._order) > self._max_tasks:
                old = self._order.pop(0)
                self._tasks.pop(old, None)
        thread = threading.Thread(target=self._run, args=(task,), name=f"task-{task_id}", daemon=True)
        thread.start()
        return task

    def _run(self, task: Task) -> None:
        task.status = "running"
        task.logs.append("任务已启动，开始选择性采集……")

        def progress(message: str) -> None:
            task.logs.append(message)

        try:
            report = run_selective_fetch(
                url=task.params["url"],
                wanted_members=task.params["wanted_members"],
                experiment_id=task.params.get("experiment_id") or EXPERIMENT_ID,
                allowed_versions=tuple(task.params.get("allowed_versions", (1, 2, 3))),
                timeout=float(task.params.get("timeout", 30.0)),
                progress=progress,
            )
            task.report = report
            if report["status"] == "failed":
                task.logs.append(f"任务失败：{report['error']['message']}")
            else:
                task.logs.append(
                    f"任务结束：正式记录 {len(report['records'])} 条，"
                    f"被拒绝成员 {len(report['rejected_members'])} 个"
                )
        except Exception as exc:  # 防护：任何意外都要在任务结果中可见
            task.error = f"{type(exc).__name__}: {exc}"
            task.logs.append(f"任务异常：{task.error}")
        finally:
            task.status = "finished"
            task.finished_at = time.time()
