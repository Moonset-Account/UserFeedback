"""任务运行记录存储：内存 + 运行目录下 JSON 落盘。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path


class RunStore:
    def __init__(self, data_dir: str | Path):
        self._dir = Path(data_dir) / "runs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        for path in self._dir.glob("*.json"):
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
                self._runs[run["run_id"]] = run
            except (json.JSONDecodeError, KeyError, OSError):
                continue

    def create(self, source: str, scenario: str | None, result: dict) -> dict:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        run = {
            "run_id": run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "scenario": scenario,
            "result": result,
        }
        with self._lock:
            self._runs[run_id] = run
            (self._dir / f"{run_id}.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return run

    def list_runs(self) -> list[dict]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r["created_at"], reverse=True)

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(run_id)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            for path in self._dir.glob("*.json"):
                path.unlink()
