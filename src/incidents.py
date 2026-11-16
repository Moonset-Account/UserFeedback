"""Immutable, scoped Stage 7 incident aggregation primitives."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable, Any

from .facts import _ids, _bounded_text

_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_CONFIDENCES = frozenset({"low", "medium", "high"})
_RAW_MARKER = re.compile(r"(?:matched_text|line_content|synthetic|password|api[_ -]?key)", re.IGNORECASE)


@dataclass(frozen=True)
class Incident:
    incident_id: str
    status: str
    scope: str
    fact_ids: tuple[str, ...]
    delta_ids: tuple[str, ...]
    severity: str
    confidence: str
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.incident_id, str) or not re.fullmatch(r"incident-sha256:[0-9a-f]{64}", self.incident_id):
            raise ValueError("incident_id must be an incident-sha256 identity")
        if self.status not in {"open", "resolved", "superseded"}:
            raise ValueError("invalid incident status")
        object.__setattr__(self, "scope", _bounded_text(self.scope, "scope"))
        object.__setattr__(self, "fact_ids", _ids(self.fact_ids, "fact"))
        object.__setattr__(self, "delta_ids", _ids(self.delta_ids, "delta"))
        if not self.fact_ids:
            raise ValueError("incident requires at least one fact")
        if self.severity not in _SEVERITIES or self.confidence not in _CONFIDENCES:
            raise ValueError("invalid incident severity or confidence")
        summary = _bounded_text(self.summary, "summary", 1024)
        if _RAW_MARKER.search(summary):
            raise ValueError("incident summary contains a raw-evidence marker")
        object.__setattr__(self, "summary", summary)

    @classmethod
    def create(cls, status: str, scope: str, fact_ids: Iterable[str], delta_ids: Iterable[str],
               severity: str, confidence: str, summary: str) -> "Incident":
        facts = _ids(fact_ids, "fact"); deltas = _ids(delta_ids, "delta")
        identity = {"version": 1, "status": status, "scope": scope, "fact_ids": facts,
                    "delta_ids": deltas, "severity": severity, "confidence": confidence, "summary": summary}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return cls("incident-sha256:" + digest, status, scope, facts, deltas, severity, confidence, summary)

    @staticmethod
    def allowed_transitions(status: str) -> tuple[str, ...]:
        return {"open": ("resolved", "superseded"), "resolved": (), "superseded": ()}.get(status, ())

    @staticmethod
    def can_transition(current: str, target: str) -> bool:
        return target in Incident.allowed_transitions(current)

    def to_dict(self) -> dict[str, Any]:
        return {"incident_id": self.incident_id, "status": self.status, "scope": self.scope,
                "fact_ids": list(self.fact_ids), "delta_ids": list(self.delta_ids),
                "severity": self.severity, "confidence": self.confidence, "summary": self.summary}
