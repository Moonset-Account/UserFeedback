"""Evidence ledger and history records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import Evidence


@dataclass(frozen=True)
class Rejection:
    evidence_version: int
    wiring_id: str
    reason: str
    message: str
    expected: Any = None
    actual: Any = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_version": self.evidence_version,
            "wiring_id": self.wiring_id,
            "reason": self.reason,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class HistoryEntry:
    version: int
    evidence: Evidence
    before_ids: tuple[str, ...]
    after_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    rejections: tuple[Rejection, ...]

    @property
    def evidence_summary(self) -> str:
        return self.evidence.summary()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "evidence": self.evidence.to_dict(),
            "summary": self.evidence_summary,
            "before_ids": list(self.before_ids),
            "after_ids": list(self.after_ids),
            "removed_ids": list(self.removed_ids),
            "rejections": [item.to_dict() for item in self.rejections],
        }
