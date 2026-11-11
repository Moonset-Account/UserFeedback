"""Finding and report data types."""

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"
    VIOLATION = "VIOLATION"
    UNDECIDABLE = "UNDECIDABLE"


@dataclass
class Finding:
    rule_id: str
    verdict: Verdict
    events: list[str]
    message: str
    evidence: list[str] = field(default_factory=list)
    unknown_reason: str | None = None

    def to_dict(self) -> dict:
        out = {
            "rule_id": self.rule_id,
            "verdict": self.verdict.value,
            "events": list(self.events),
            "message": self.message,
            "evidence": list(self.evidence),
        }
        if self.unknown_reason:
            out["unknown_reason"] = self.unknown_reason
        return out


@dataclass
class AuditReport:
    verdict: Verdict
    findings: list[Finding]
    summary: dict[str, int]
    stats: dict[str, int]
    structural_issues: list[str]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "summary": self.summary,
            "stats": self.stats,
            "structural_issues": list(self.structural_issues),
            "findings": [f.to_dict() for f in self.findings],
        }
