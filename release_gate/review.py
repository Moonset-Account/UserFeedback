"""决定落盘与事后复核。

每条决定连同做出决定时的完整输入快照一起保存；事后更正事件/豁免后，
用更正后的整包输入重新计算，并与原决定逐条比对，输出复核报告。
存储使用单个 JSON 文件，写入采用临时文件 + 原子替换。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .engine import BundleEvaluation, Decision, evaluate_bundle
from .intervals import parse_dt, to_iso
from .models import EvaluationBundle


@dataclass
class StoredDecision:
    """带输入快照的历史决定。"""

    record_id: str
    decided_at: datetime
    bundle_snapshot: dict
    decision: Decision

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "decided_at": to_iso(self.decided_at),
            "bundle_snapshot": self.bundle_snapshot,
            "decision": self.decision.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StoredDecision":
        return cls(
            record_id=data["record_id"],
            decided_at=parse_dt(data["decided_at"]),
            bundle_snapshot=data["bundle_snapshot"],
            decision=Decision.from_dict(data["decision"]),
        )


@dataclass
class ReviewItem:
    """单条历史决定的复核结果。"""

    record_id: str
    application_id: str
    original_allowed: bool
    recomputed_allowed: bool
    changed: bool
    original_reason: Optional[str]
    recomputed_reason: Optional[str]
    recomputed: Decision


@dataclass
class ReviewReport:
    """整份复核报告。"""

    items: list[ReviewItem] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(item.changed for item in self.items)

    def to_dict(self) -> dict:
        return {
            "has_changes": self.has_changes,
            "items": [
                {
                    "record_id": item.record_id,
                    "application_id": item.application_id,
                    "original_allowed": item.original_allowed,
                    "recomputed_allowed": item.recomputed_allowed,
                    "changed": item.changed,
                    "original_reason": item.original_reason,
                    "recomputed_reason": item.recomputed_reason,
                    "recomputed_decision": item.recomputed.to_dict(),
                }
                for item in self.items
            ],
        }


class DecisionRepository:
    """决定记录的 JSON 文件仓库。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read_raw(self) -> dict:
        if not self.path.exists():
            return {"records": []}
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("records", [])
        return data

    def load_all(self) -> list[StoredDecision]:
        return [StoredDecision.from_dict(item) for item in self._read_raw()["records"]]

    def save(
        self,
        evaluation: BundleEvaluation,
        bundle_snapshot: dict,
        decided_at: Optional[datetime] = None,
    ) -> list[StoredDecision]:
        """把一次整包判定的全部决定落盘，返回生成的记录。"""
        raw = self._read_raw()
        decided_at = decided_at or datetime.now(timezone.utc)
        stored: list[StoredDecision] = []
        for decision in evaluation.decisions:
            record = StoredDecision(
                record_id=str(uuid.uuid4()),
                decided_at=decided_at,
                bundle_snapshot=bundle_snapshot,
                decision=decision,
            )
            stored.append(record)
            raw["records"].append(record.to_dict())
        self._write_atomic(raw)
        return stored

    def review(
        self,
        corrected_bundle: EvaluationBundle,
        as_of: Optional[datetime] = None,
    ) -> ReviewReport:
        """用更正后的输入重算历史决定并逐条比对。

        复核时点优先级：显式传入的 ``as_of`` > 更正包自带的 ``as_of`` >
        原决定快照里的 ``as_of``。默认沿用原时点，保证“站在当时的时点”
        重算，只反映被更正数据造成的差异，不被后续事件污染。
        """
        records = self.load_all()
        report = ReviewReport()
        for record in records:
            review_as_of = (
                as_of
                if as_of is not None
                else (
                    corrected_bundle.as_of
                    if corrected_bundle.as_of is not None
                    else parse_dt(record.bundle_snapshot["as_of"])
                    if record.bundle_snapshot.get("as_of")
                    else None
                )
            )
            re_bundle = EvaluationBundle(
                window=corrected_bundle.window,
                environments=corrected_bundle.environments,
                quotas=corrected_bundle.quotas,
                exemptions=corrected_bundle.exemptions,
                events=corrected_bundle.events,
                applications=corrected_bundle.applications,
                as_of=review_as_of,
            )
            re_evaluation = evaluate_bundle(re_bundle)
            match = next(
                (
                    d
                    for d in re_evaluation.decisions
                    if d.application_id == record.decision.application_id
                ),
                None,
            )
            if match is None:
                raise ValueError(
                    f"更正后的输入缺少申请 {record.decision.application_id!r}，"
                    "无法复核"
                )
            report.items.append(
                ReviewItem(
                    record_id=record.record_id,
                    application_id=record.decision.application_id,
                    original_allowed=record.decision.allowed,
                    recomputed_allowed=match.allowed,
                    changed=match.allowed != record.decision.allowed
                    or match.reason != record.decision.reason,
                    original_reason=record.decision.reason,
                    recomputed_reason=match.reason,
                    recomputed=match,
                )
            )
        return report

    def _write_atomic(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".decisions-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
