"""Data models for the forensic exhibit allocation module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Exhibit:
    """A forensic exhibit that needs two independent reviews.

    Parameters
    ----------
    exhibit_id:
        Stable exhibit identifier used by dispatch staff.
    required_qualifications:
        Qualifications an independent reviewer must hold in the default
        ``"each"`` qualification mode.
    source_group:
        Identifier of the same-source / homology group. A reviewer who has
        contacted material from this group must recuse.
    priority:
        Tie-break priority. The default convention is that a larger number
        means higher priority; pass ``priority_direction="lower"`` to
        :func:`forensic_allocator.allocate` for ranks where 1 is highest.
    """

    exhibit_id: str
    required_qualifications: frozenset[str] = field(default_factory=frozenset)
    source_group: str | None = None
    priority: int = 0


@dataclass(frozen=True, slots=True)
class Reviewer:
    """A reviewer, laboratory, capacity, and recusal information."""

    reviewer_id: str
    lab: str
    qualifications: frozenset[str] = field(default_factory=frozenset)
    capacity: int = 1
    name: str | None = None
    exposed_source_groups: frozenset[str] = field(default_factory=frozenset)
    recused_exhibit_ids: frozenset[str] = field(default_factory=frozenset)
    active: bool = True


@dataclass(frozen=True, slots=True)
class CandidatePairStatus:
    """Quota status for one structurally eligible reviewer pair."""

    reviewer_ids: tuple[str, str]
    labs: tuple[str, str]
    remaining_capacities: tuple[int, int]
    saturated_blocker_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewer_ids": list(self.reviewer_ids),
            "labs": list(self.labs),
            "remaining_capacities": list(self.remaining_capacities),
            "saturated_blocker_ids": list(self.saturated_blocker_ids),
        }


@dataclass(frozen=True, slots=True)
class ReviewerQualificationGap:
    """Qualifications missing from one non-recused reviewer."""

    reviewer_id: str
    missing_qualifications: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewer_id": self.reviewer_id,
            "missing_qualifications": list(self.missing_qualifications),
        }


@dataclass(frozen=True, slots=True)
class UnassignedReport:
    """Explain why an exhibit was not part of an optimal allocation."""

    exhibit_id: str
    primary_reason: str
    reason_codes: tuple[str, ...]
    message: str
    eligible_reviewer_ids: tuple[str, ...]
    recused_reviewer_ids: tuple[str, ...]
    inactive_reviewer_ids: tuple[str, ...]
    missing_qualifications: tuple[str, ...]
    reviewer_qualification_gaps: tuple[ReviewerQualificationGap, ...]
    cross_lab_pair_count: int
    candidate_pairs: tuple[CandidatePairStatus, ...]
    minimum_additional_slots: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "exhibit_id": self.exhibit_id,
            "primary_reason": self.primary_reason,
            "reason_codes": list(self.reason_codes),
            "message": self.message,
            "eligible_reviewer_ids": list(self.eligible_reviewer_ids),
            "recused_reviewer_ids": list(self.recused_reviewer_ids),
            "inactive_reviewer_ids": list(self.inactive_reviewer_ids),
            "missing_qualifications": list(self.missing_qualifications),
            "reviewer_qualification_gaps": [
                gap.to_dict() for gap in self.reviewer_qualification_gaps
            ],
            "cross_lab_pair_count": self.cross_lab_pair_count,
            "candidate_pairs": [pair.to_dict() for pair in self.candidate_pairs],
            "minimum_additional_slots": self.minimum_additional_slots,
        }


@dataclass(frozen=True, slots=True)
class Assignment:
    """One complete exhibit assignment: exactly two different-lab reviewers."""

    exhibit_id: str
    reviewer_ids: tuple[str, str]
    reviewer_names: tuple[str, str]
    labs: tuple[str, str]
    anonymous_case_numbers: tuple[str, str]

    def to_dispatch_dict(self) -> dict[str, object]:
        return {
            "exhibit_id": self.exhibit_id,
            "reviewer_ids": list(self.reviewer_ids),
            "reviewer_names": list(self.reviewer_names),
            "labs": list(self.labs),
            "anonymous_case_numbers": list(self.anonymous_case_numbers),
        }


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Optimal allocation plus diagnostics and capacity state."""

    assignments: tuple[Assignment, ...]
    unassigned: tuple[UnassignedReport, ...]
    coverage_count: int
    maximum_coverage: int
    coverage_exhibit_ids: tuple[str, ...]
    exhibits: tuple[Exhibit, ...]
    reviewers: tuple[Reviewer, ...]
    loads: Mapping[str, int]
    capacities: Mapping[str, int]
    anonymous_scope: str
    priority_direction: str
    tie_break: str

    def assignment_for_exhibit(self, exhibit_id: str) -> Assignment | None:
        for assignment in self.assignments:
            if assignment.exhibit_id == exhibit_id:
                return assignment
        return None

    def anonymous_number_for_reviewer(
        self, reviewer_id: str, exhibit_id: str
    ) -> str | None:
        assignment = self.assignment_for_exhibit(exhibit_id)
        if assignment is None:
            return None
        try:
            index = assignment.reviewer_ids.index(reviewer_id)
        except ValueError:
            return None
        return assignment.anonymous_case_numbers[index]
