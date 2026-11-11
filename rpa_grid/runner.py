"""任务编排：采集 -> 规划 -> 浏览器自动化 -> 结果落盘。

线程模型：Flask 请求线程里启动同步 Playwright（Playwright sync API
不能跨线程复用，因此每次 run 都在同线程内新建并关闭浏览器）。
任务结果以 JSON 存到 data/runtime/<task_id>/，网页据此展示证据与错误。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .automation import GridAutomation, RunResult
from .collector import CollectorError, collect
from .planner import plan_execution

RUNTIME_DIR = Path(__file__).resolve().parent.parent / "data" / "runtime"


class TaskRegistry:
    """内存任务表 + JSON 落盘（进程内有效，重启后历史仍可从磁盘列出）。"""

    def __init__(self, runtime_dir: Path = RUNTIME_DIR):
        self.runtime_dir = runtime_dir
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._restore()

    def _restore(self) -> None:
        for d in sorted(self.runtime_dir.glob("task-*")):
            f = d / "result.json"
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self._tasks[data["task_id"]] = data
                except json.JSONDecodeError:
                    continue

    def create(self, scenario: str) -> str:
        task_id = f"task-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        data = {
            "task_id": task_id, "scenario": scenario,
            "status": "queued", "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stage": "已创建，等待执行", "messages": [],
        }
        with self._lock:
            self._tasks[task_id] = data
        self._persist(data)
        return task_id

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.update(fields)
            self._persist(task)

    def add_message(self, task_id: str, msg: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task["messages"].append(
                {"at": time.strftime("%H:%M:%S"), "text": msg})
            self._persist(task)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)

    def list(self) -> List[Dict[str, Any]]:
        return sorted(
            ({"task_id": t["task_id"], "scenario": t["scenario"],
              "status": t["status"], "created_at": t["created_at"],
              "ok": t.get("result", {}).get("ok") if t.get("result") else None}
             for t in self._tasks.values()),
            key=lambda x: x["created_at"], reverse=True)

    def _persist(self, task: Dict[str, Any]) -> None:
        d = self.runtime_dir / task["task_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "result.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")


def run_task(registry: TaskRegistry, base_url: str, scenario: str,
             headless: bool = True) -> str:
    """同步执行一个完整任务，返回 task_id。异常被捕获并写入任务记录。"""
    task_id = registry.create(scenario)

    def fail(stage: str, exc: Exception) -> None:
        registry.add_message(task_id, f"[失败] {stage}: {exc}")
        registry.update(task_id, status="failed", stage=stage,
                        error=f"{type(exc).__name__}: {exc}")

    try:
        registry.update(task_id, status="collecting", stage="采集实验矩阵")
        registry.add_message(task_id, f"经公开接口采集场景 {scenario}（只读 TSV/JSON）")
        bundle = collect(base_url, scenario)
        registry.add_message(
            task_id,
            f"采集完成：{bundle.matrix.n_data_rows} 行 × "
            f"{len(bundle.matrix.columns)} 列；来源指纹 "
            f"{bundle.evidence['matrix_sha256_12']}")

        registry.update(task_id, status="planning", stage="生成逐格执行计划")
        plan = plan_execution(bundle.matrix, bundle.grid_config)
        registry.add_message(
            task_id,
            f"规划完成：可执行 {len(plan.writable_plans)} 格，"
            f"拒绝 {len(plan.rejected_plans)} 格（隐藏/只读）")
        # 保存计划供网页查看
        plan_path = registry.runtime_dir / task_id / "plan.json"
        plan_path.write_text(
            json.dumps([p.to_dict() for p in plan.cell_plans],
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

        registry.update(task_id, status="automating",
                        stage="Playwright 真实浏览器录入与回读")
        registry.add_message(task_id, "启动 Chromium，按可见列标题定位数据区")
        automation = GridAutomation(base_url=base_url, headless=headless)
        result: RunResult = automation.run(
            plan, bundle.matrix, bundle.grid_config, bundle.evidence)

        registry.add_message(
            task_id,
            f"执行结束：成功 {result.succeeded}，拒绝 {result.rejected}，"
            f"失败 {result.failed}，跳过(缺失) {result.skipped}，"
            f"耗时 {result.elapsed_ms} ms")
        registry.update(
            task_id, status="done", stage="完成",
            result=result.to_dict(),
            plan_file=str(plan_path))
    except CollectorError as exc:
        fail("采集阶段", exc)
    except Exception as exc:  # noqa: BLE001 - 任何异常都如实落盘
        fail("自动化阶段", exc)
    return task_id
