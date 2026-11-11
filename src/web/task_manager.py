"""采集任务管理：每个任务在独立线程内运行，持有独立引擎与全部快照/证据。"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from rip_collector import CollectorEngine
from rip_collector.transport import run_file_collection, run_http_collection

from sample_generator import SCENARIOS, raw_ndjson_lines

MAX_TASKS = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class TaskRecord:
    task_id: str
    scenario_key: str
    transport: str
    target: str
    created_at: str
    status: str = "queued"
    engine: CollectorEngine | None = None
    error: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def public_view(self) -> dict[str, Any]:
        with self.lock:
            view = {
                "task_id": self.task_id,
                "scenario_key": self.scenario_key,
                "transport": self.transport,
                "target": self.target,
                "created_at": self.created_at,
                "status": self.status,
                "error": self.error,
                "history": list(self.history),
            }
            if self.engine is not None:
                view["result"] = self.engine.result()
            return view


class TaskManager:
    def __init__(self, recordings_dir: str, source_base_url: str) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
        self._recordings_dir = recordings_dir
        self._source_base_url = source_base_url.rstrip("/")

    def list_scenarios(self) -> list[dict[str, str]]:
        return [
            {
                "key": s.key,
                "title": s.title,
                "description": s.description,
                "group": s.group,
                "transport": "http",
                "drop_connection": s.drop_connection,
                "message_count": len(s.messages),
            }
            for s in SCENARIOS.values()
        ]

    def create_task(self, scenario_key: str, transport: str) -> TaskRecord:
        if scenario_key not in SCENARIOS:
            raise KeyError(f"未知场景: {scenario_key}")
        if transport not in ("http", "file"):
            raise ValueError("transport 仅支持 http 或 file")

        scenario = SCENARIOS[scenario_key]
        if transport == "http":
            target = f"{self._source_base_url}/source/stream/{scenario_key}"
        else:
            # 文件传输：确保录制文件已由生成器写出，采集器只拿到公开文件路径
            from sample_generator import write_recording
            path = write_recording(scenario, self._recordings_dir)
            target = str(path)

        task = TaskRecord(
            task_id=uuid.uuid4().hex[:12],
            scenario_key=scenario_key,
            transport=transport,
            target=target,
            created_at=_now_iso(),
        )
        with self._lock:
            self._tasks[task.task_id] = task
            # 控制内存占用，淘汰最旧任务
            if len(self._tasks) > MAX_TASKS:
                oldest = next(iter(self._tasks))
                self._tasks.pop(oldest)

        thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        thread.start()
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._tasks.values())
        return [r.public_view() for r in reversed(records)]

    # ------------------------------------------------------------------
    def _run(self, task: TaskRecord) -> None:
        scenario = SCENARIOS[task.scenario_key]
        engine = CollectorEngine()
        task.engine = engine
        task.status = "running"
        task.history.append({"at": _now_iso(), "event": "start",
                             "detail": f"通过 {task.transport} 接口采集场景 {task.scenario_key}"})
        started = time.monotonic()
        try:
            if task.transport == "http":
                run_http_collection(engine, task.target)
            else:
                run_file_collection(engine, task.target)
        except Exception as exc:  # 防御：任何意外都冻结为中断，不伪装成功
            engine.mark_interrupted(f"collector_exception: {type(exc).__name__}: {exc}")
            task.error = str(exc)

        elapsed = time.monotonic() - started
        result = engine.result()
        task.status = result["status"]
        task.history.append({
            "at": _now_iso(),
            "event": "finish",
            "detail": f"状态={result['status']}，快照 {result['snapshot_count']} 个，"
                      f"决策 {result['decision_count']} 条，耗时 {elapsed:.2f}s",
        })
        # 采集结束后，把样例原始消息行数留作证据（来自生成器的公开录制，不是真值）
        task.history.append({
            "at": _now_iso(),
            "event": "source_lines",
            "detail": f"源消息 {len(raw_ndjson_lines(scenario))} 行（NDJSON，含坏行）",
        })
