"""运行结果的 JSON 文件存储（instance/runs/）。"""
from __future__ import annotations

import json

from .config import RUNS_DIR, ensure_dirs


def save_run(record) -> str:
    ensure_dirs()
    path = RUNS_DIR / f"{record.run_id}.json"
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _refresh_index()
    return str(path)


def list_runs() -> list[dict]:
    ensure_dirs()
    index_path = RUNS_DIR / "index.json"
    if index_path.exists():
        return json.loads(index_path.read_text(encoding="utf-8")).get("runs", [])
    return []


def load_run(run_id: str) -> dict:
    path = RUNS_DIR / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _refresh_index() -> None:
    items = []
    for path in sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        if path.name == "index.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items.append({
            "run_id": data["run_id"],
            "scenario": data["scenario"],
            "status": data["status"],
            "duration_ms": data["duration_ms"],
            "started_at": data["started_at"],
            "message_count": sum(len(g["messages"]) for g in data["generations"]),
            "error_count": len(data["errors"]),
            "warning_count": len(data["warnings"]),
        })
    (RUNS_DIR / "index.json").write_text(
        json.dumps({"runs": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
