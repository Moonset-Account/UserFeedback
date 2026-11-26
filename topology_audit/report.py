"""Report objects and deterministic consensus summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Anchor


@dataclass(frozen=True)
class Match:
    """One complete legal device mapping and its chosen orientation."""

    mapping: tuple[tuple[str, str], ...]
    orientation: tuple[tuple[str, bool], ...] = ()
    port_mapping: tuple[tuple[str, str], ...] = ()
    bridge_details: tuple[tuple[str, str, str], ...] = ()

    def mapping_dict(self) -> dict[str, str]:
        return dict(self.mapping)

    def orientation_dict(self) -> dict[str, bool]:
        return dict(self.orientation)

    def port_mapping_dict(self) -> dict[str, str]:
        return dict(self.port_mapping)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": dict(self.mapping),
            "orientation": dict(self.orientation),
            "port_mapping": dict(self.port_mapping),
            "bridge_attachments": [
                {"bridge": bridge_id, "logical_port": lp, "observed_port": op}
                for bridge_id, lp, op in self.bridge_details
            ],
        }


def summarize_matches(
    matches: tuple[Match, ...] | list[Match],
    logical_device_ids: list[str] | tuple[str, ...],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[str]]]:
    candidates: dict[str, list[str]] = {}
    for logical_id in logical_device_ids:
        values = set()
        for match in matches:
            value = match.mapping_dict().get(logical_id)
            if value is not None:
                values.add(value)
        candidates[logical_id] = sorted(values)

    certain = {
        logical_id: values[0]
        for logical_id, values in candidates.items()
        if len(values) == 1
    }
    ambiguous = {
        logical_id: values
        for logical_id, values in candidates.items()
        if len(values) > 1
    }
    return certain, candidates, ambiguous


@dataclass(frozen=True)
class AnchorFeedback:
    anchor: Anchor
    status: str
    support_count: int | None = None
    reason: str | None = None
    conflicts_with: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical": self.anchor.logical,
            "observed": self.anchor.observed,
            "location": self.anchor.location,
            "source": self.anchor.source,
            "status": self.status,
            "support_count": self.support_count,
            "reason": self.reason,
            "conflicts_with": list(self.conflicts_with),
        }


@dataclass
class AuditReport:
    """Current candidate set after accepted anchors."""

    feasible: bool
    complete: bool
    matches: tuple[Match, ...]
    mapping: dict[str, str]
    candidates: dict[str, list[str]]
    ambiguous: dict[str, list[str]]
    baseline_mapping: dict[str, str]
    baseline_candidates: dict[str, list[str]]
    baseline_matches: tuple[Match, ...] = ()
    accepted_anchors: tuple[Anchor, ...] = ()
    anchor_feedback: tuple[AnchorFeedback, ...] = ()
    conflict_sets: tuple[tuple[str, ...], ...] = ()
    logical_device_ids: tuple[str, ...] = ()
    solution_limit: int | None = None

    @property
    def match_count(self) -> int:
        return len(self.matches)

    @property
    def unsatisfiable_anchors(self) -> tuple[AnchorFeedback, ...]:
        return tuple(f for f in self.anchor_feedback if f.status == "unsatisfiable")

    @property
    def rejected_anchors(self) -> tuple[AnchorFeedback, ...]:
        return tuple(f for f in self.anchor_feedback if f.status == "rejected")

    @property
    def unmet_anchors(self) -> tuple[AnchorFeedback, ...]:
        """All anchors that could not be accepted in the current session."""
        return tuple(
            f
            for f in self.anchor_feedback
            if f.status in ("unsatisfiable", "rejected")
        )

    def is_certain(self, logical_id: str) -> bool:
        return logical_id in self.mapping

    def candidates_are_narrowed_from(self, previous: "AuditReport") -> bool:
        """Return True when every current candidate set is a subset of prior."""
        for key, values in self.candidates.items():
            old = set(previous.candidates.get(key, ()))
            if not set(values).issubset(old):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "complete": self.complete,
            "match_count": self.match_count,
            "mapping": dict(sorted(self.mapping.items())),
            "candidates": {k: list(v) for k, v in sorted(self.candidates.items())},
            "ambiguous": {k: list(v) for k, v in sorted(self.ambiguous.items())},
            "baseline_mapping": dict(sorted(self.baseline_mapping.items())),
            "baseline_candidates": {
                k: list(v) for k, v in sorted(self.baseline_candidates.items())
            },
            "baseline_matches": [m.to_dict() for m in self.baseline_matches],
            "accepted_anchors": [a.__dict__ for a in self.accepted_anchors],
            "anchor_feedback": [f.to_dict() for f in self.anchor_feedback],
            "unsatisfiable_anchors": [f.to_dict() for f in self.unsatisfiable_anchors],
            "rejected_anchors": [f.to_dict() for f in self.rejected_anchors],
            "unmet_anchors": [f.to_dict() for f in self.unmet_anchors],
            "conflict_sets": [list(x) for x in self.conflict_sets],
            "matches": [m.to_dict() for m in self.matches],
        }
