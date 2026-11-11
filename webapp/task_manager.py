"""线程安全的任务管理：后台线程执行真实采集，前端轮询状态。

任务结果只来自真实执行；本模块不预置任何"成功结果"。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from collector.browser import BrowserCatalogScanner
from collector.runner import run_sampling_task
from collector.scanner import CatalogScanner


@dataclass
class TaskHandle:
    task_id: str
    label: str
    state: str = "queued"          # queued | running | done | error
    progress: list[dict] = field(default_factory=list)
    result: dict | None = None
    fatal_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "label": self.label,
            "state": self.state,
            "progress": self.progress[-50:],
            "result": self.result,
            "fatal_error": self.fatal_error,
        }


class TaskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskHandle] = {}
        self._order: list[str] = []
        self._demo_busy = False

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [self._tasks[tid].to_dict() for tid in self._order]

    def get(self, task_id: str) -> TaskHandle | None:
        with self._lock:
            return self._tasks.get(task_id)

    def submit(
        self,
        *,
        label: str,
        start_url: str,
        seed: int,
        sample_size: int,
        request_budget: int,
        transport: str = "http",
        headless: bool = True,
        timeout: float = 8.0,
        max_retries: int = 2,
    ) -> TaskHandle:
        handle = TaskHandle(task_id=uuid.uuid4().hex[:12], label=label)
        with self._lock:
            self._tasks[handle.task_id] = handle
            self._order.append(handle.task_id)

        def work() -> None:
            handle.state = "running"
            try:
                if transport == "browser":
                    scanner = BrowserCatalogScanner(
                        start_url, request_budget, timeout=timeout,
                        max_retries=max_retries, headless=headless,
                        event_callback=lambda evt: handle.progress.append(
                            {"stage": "page", "page_index": evt.page_index,
                             "requests_used": evt.requests_used,
                             "raw_count": evt.raw_count,
                             "duplicate_on_page": evt.duplicate_on_page}),
                    )
                else:
                    scanner = CatalogScanner(
                        start_url, request_budget, timeout=timeout, max_retries=max_retries,
                        event_callback=lambda evt: handle.progress.append(
                            {"stage": "page", "page_index": evt.page_index,
                             "requests_used": evt.requests_used,
                             "raw_count": evt.raw_count,
                             "duplicate_on_page": evt.duplicate_on_page}),
                    )
                result = run_sampling_task(
                    handle.task_id, start_url, int(seed), int(sample_size),
                    int(request_budget), scanner=scanner,
                    progress_callback=lambda stage, payload: handle.progress.append(
                        {"stage": stage, **payload}),
                )
                handle.result = result.to_dict()
                handle.state = "done"
            except Exception as exc:  # 网页必须能看到具体错误
                handle.fatal_error = f"{type(exc).__name__}: {exc}"
                handle.state = "error"

        threading.Thread(target=work, name=f"task-{handle.task_id}", daemon=True).start()
        return handle

    def start_demo_chain(self, jobs: list[dict], *, on_chain_done=None) -> str:
        """依次运行多个小场景（演示按钮）。返回链 ID。"""
        chain_id = uuid.uuid4().hex[:12]
        if self._demo_busy:
            raise RuntimeError("已有演示链在运行，请等待结束")
        self._demo_busy = True

        def chain() -> None:
            try:
                for job in jobs:
                    handle = self.submit(**job)
                    while handle.state in ("queued", "running"):
                        threading.Event().wait(0.15)
            finally:
                self._demo_busy = False
                if on_chain_done:
                    on_chain_done(chain_id)

        threading.Thread(target=chain, name=f"demo-{chain_id}", daemon=True).start()
        return chain_id

    @property
    def demo_busy(self) -> bool:
        return self._demo_busy
