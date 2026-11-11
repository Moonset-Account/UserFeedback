"""结果存储：按目标名整体替换，保证修改例外后不残留旧实例。

每次成功采集都会用完整的新结果覆盖 <target>.json，而不是按开始时间合并，
因此源数据中删除/移动一个例外后，旧实例不会残留在存储里。
运行历史（每次采集的完整结果）单独追加保存，供网页回看两次运行的差异。
"""

from __future__ import annotations

import json
import pathlib


class JsonStore:
    def __init__(self, base_dir: str | pathlib.Path):
        self.base = pathlib.Path(base_dir)
        self.targets_dir = self.base / "targets"
        self.runs_dir = self.base / "runs"
        self.targets_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
        return cleaned[:80] or "unnamed"

    def save(self, target_name: str, result: dict) -> None:
        name = self._safe_name(target_name)
        # 整体替换：先写临时文件再原子改名，避免半成品结果
        target_path = self.targets_dir / f"{name}.json"
        tmp = target_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target_path)

        run_path = self.runs_dir / f"{result['run_id']}.json"
        run_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_target(self, target_name: str) -> dict | None:
        path = self.targets_dir / f"{self._safe_name(target_name)}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_targets(self) -> list[dict]:
        out = []
        for path in sorted(self.targets_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "target_name": path.stem,
                "run_id": data.get("run_id"),
                "finished_at": data.get("finished_at"),
                "status": data.get("status"),
                "instance_count": len(data.get("instances", [])),
            })
        return out

    def list_runs(self) -> list[dict]:
        out = []
        for path in sorted(self.runs_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "run_id": data.get("run_id"),
                "target": path.stem,
                "finished_at": data.get("finished_at"),
                "status": data.get("status"),
                "source": data.get("request", {}).get("source"),
                "instance_count": len(data.get("instances", [])),
            })
        return out

    def load_run(self, run_id: str) -> dict | None:
        path = self.runs_dir / f"{self._safe_name(run_id)}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
