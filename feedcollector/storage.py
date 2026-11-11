"""任务结果的文件存储（JSON + 原始证据）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import to_jsonable


class RunStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.runs_dir = self.base_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def save_run(self, run_id: str, report: dict) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
        path = self.runs_dir / f"{safe}.json"
        path.write_text(
            json.dumps(to_jsonable(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def list_runs(self) -> list[dict]:
        items = []
        for path in sorted(self.runs_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items.append({
                "run_id": data.get("run_id", path.stem),
                "scenario": data.get("scenario", ""),
                "started_at": data.get("started_at", ""),
                "elapsed_ms": data.get("elapsed_ms"),
                "status": data.get("status"),
                "feed_count": len(data.get("feeds", [])),
                "catalog_entry_count": len(data.get("catalog", {}).get("entries", [])),
                "conflict_count": len(data.get("catalog", {}).get("conflicts", [])),
                "error_count": len(data.get("errors", [])),
            })
        return items

    def load_run(self, run_id: str) -> dict | None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
        path = self.runs_dir / f"{safe}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
