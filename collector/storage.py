"""本地持久化：运行记录、隔离记录、批准记录与契约版本。

目录结构（默认 ./data，可通过环境变量 COLLECTOR_DATA_DIR 覆盖，测试使用独立目录）：
  data/
    runs/<run_id>/result.json          每次采集的完整结果（含证据路径）
    runs/<run_id>/screenshot.png       浏览器整页截图
    runs/<run_id>/table.html           表格 HTML 原始片段
    contracts/<version>.json           已批准的契约版本（v1 为内置，不写盘）
    approvals/approval-<n>.json        批准记录（旧规则、受影响记录范围、理由）
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import BASELINE_CONTRACT, Contract, derive_contract


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        root = Path(data_dir) if data_dir else Path(os.environ.get("COLLECTOR_DATA_DIR", "data"))
        self.root = root
        self.runs_dir = root / "runs"
        self.contracts_dir = root / "contracts"
        self.approvals_dir = root / "approvals"
        for d in (self.runs_dir, self.contracts_dir, self.approvals_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = self._restore_seq()

    def _restore_seq(self) -> int:
        existing = list(self.runs_dir.glob("run-*"))
        nums = [int(p.name.split("-")[1]) for p in existing if p.name.split("-")[1].isdigit()]
        return max(nums, default=0)

    # ----- 运行记录 -----
    def new_run_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"run-{self._seq:04d}"

    def run_dir(self, run_id: str) -> Path:
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_run(self, run_id: str, result: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_run(self, run_id: str) -> dict[str, Any]:
        path = self.runs_dir / run_id / "result.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p in sorted(self.runs_dir.glob("run-*/result.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items.append(
                {
                    "run_id": data.get("run_id"),
                    "scenario": data.get("scenario"),
                    "contract_version": data.get("contract_version"),
                    "status": data.get("status"),
                    "started_at": data.get("started_at"),
                    "published_count": len(data.get("published", [])),
                    "quarantined_count": len(data.get("quarantined", [])),
                    "violation_codes": sorted({v["code"] for v in data.get("violations", [])}),
                }
            )
        return list(reversed(items))

    # ----- 契约版本 -----
    def save_contract(self, contract: Contract) -> Path:
        path = self.contracts_dir / f"{contract.version}.json"
        path.write_text(contract.to_json(), encoding="utf-8")
        return path

    def load_contract(self, version: str) -> Contract:
        if version == BASELINE_CONTRACT.version:
            return BASELINE_CONTRACT
        path = self.contracts_dir / f"{version}.json"
        return Contract.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_contract_versions(self) -> list[dict[str, Any]]:
        versions = [BASELINE_CONTRACT.to_dict()]
        for p in sorted(self.contracts_dir.glob("v*.json")):
            versions.append(json.loads(p.read_text(encoding="utf-8")))
        return versions

    def next_contract_version(self) -> str:
        existing = {v["version"] for v in self.list_contract_versions()}
        n = 2
        while f"v{n}" in existing:
            n += 1
        return f"v{n}"

    # ----- 批准 -----
    def approval_count(self) -> int:
        return len(list(self.approvals_dir.glob("approval-*.json")))

    def create_approval(
        self,
        *,
        run_id: str,
        overrides: dict[str, Any],
        base_contract: Contract,
        affected_scope: dict[str, Any],
        approved_by: str,
        reason: str,
    ) -> tuple[Contract, Path]:
        """生成新契约版本并落盘批准记录；旧契约与受影响范围原样保留。"""
        version = self.next_contract_version()
        from .contracts import FieldContract

        override_fields = {
            key: FieldContract(**payload) for key, payload in overrides.items()
        }
        new_contract = derive_contract(
            base_contract,
            override_fields,
            version=version,
            approved_by=approved_by,
            approved_reason=reason,
            approved_at=_now_iso(),
        )
        contract_path = self.save_contract(new_contract)

        approval_no = self.approval_count() + 1
        record = {
            "approval_id": f"approval-{approval_no:03d}",
            "created_at": _now_iso(),
            "source_run_id": run_id,
            "base_contract_version": base_contract.version,
            "new_contract_version": version,
            "approved_by": approved_by,
            "reason": reason,
            "old_rules": {
                key: base_contract.field(key).to_dict() if isinstance(key, str) else None
                for key in override_fields
            },
            "new_rules": {key: fc.to_dict() for key, fc in override_fields.items()},
            "affected_scope": affected_scope,
            "contract_file": str(contract_path),
        }
        path = self.approvals_dir / f"{record['approval_id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return new_contract, path

    def list_approvals(self) -> list[dict[str, Any]]:
        items = []
        for p in sorted(self.approvals_dir.glob("approval-*.json")):
            items.append(json.loads(p.read_text(encoding="utf-8")))
        return list(reversed(items))


@dataclass
class RunConfig:
    scenario: str
    url: str
    contract_version: str = "v1"
    note: str = ""
