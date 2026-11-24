"""Result objects returned by the authorization reconciliation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ApprovalKind, Interval, json_safe


@dataclass(frozen=True, slots=True)
class PersonApproval:
    """A natural person acting in one responsibility."""

    person_id: str
    role: ApprovalKind
    approval_ids: tuple[str, ...]
    identities: tuple[str, ...]
    primary_approval_id: str
    primary_identity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "role": self.role.value,
            "approval_ids": list(self.approval_ids),
            "identities": list(self.identities),
            "primary_approval_id": self.primary_approval_id,
            "primary_identity": self.primary_identity,
        }


@dataclass(frozen=True, slots=True)
class ResponsiblePair:
    """The two distinct natural persons responsible for an interval."""

    business_owner: PersonApproval
    account_custodian: PersonApproval

    @property
    def key(self) -> tuple[str, str]:
        return self.business_owner.person_id, self.account_custodian.person_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_owner": self.business_owner.to_dict(),
            "account_custodian": self.account_custodian.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Grant:
    """A continuous interval covered by one concrete responsible pair."""

    interval: Interval[Any]
    responsible: ResponsiblePair

    @property
    def start(self) -> Any:
        return self.interval.start

    @property
    def end(self) -> Any:
        return self.interval.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": json_safe(self.interval.start),
            "end": json_safe(self.interval.end),
            "interval": self.interval.to_dict(),
            "responsible": self.responsible.to_dict(),
            # Convenience projections for callers expecting flat role fields.
            "business_owner": self.responsible.business_owner.to_dict(),
            "account_custodian": self.responsible.account_custodian.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RemediationOption:
    """One approval that could satisfy an unmet independent-approval need."""

    approval_type: ApprovalKind
    distinct_from_person_id: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_type": self.approval_type.value,
            "distinct_from_person_id": self.distinct_from_person_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class IndependenceReport:
    """Canonical-person and separation-of-duties diagnosis."""

    status: str
    business_owner_person_ids: tuple[str, ...]
    account_custodian_person_ids: tuple[str, ...]
    same_person_ids: tuple[str, ...]
    missing_approval_types: tuple[ApprovalKind, ...]
    # Outer tuple = all requirement groups (AND); inner tuple = alternatives (OR).
    requirement_groups: tuple[tuple[RemediationOption, ...], ...]
    message: str

    @property
    def has_independent_pair(self) -> bool:
        return self.status == "independent_pair_available"

    @property
    def missing_independent_approval_types(self) -> list[str]:
        values = {
            option.approval_type.value
            for group in self.requirement_groups
            for option in group
        }
        return [kind.value for kind in ApprovalKind if kind.value in values]

    @property
    def primary_missing_independent_approval_type(self) -> str | None:
        types = self.missing_independent_approval_types
        return types[0] if len(types) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "has_independent_pair": self.has_independent_pair,
            "business_owner_person_ids": list(self.business_owner_person_ids),
            "account_custodian_person_ids": list(self.account_custodian_person_ids),
            "same_person_ids": list(self.same_person_ids),
            "missing_approval_types": [item.value for item in self.missing_approval_types],
            "missing_independent_approval_types": self.missing_independent_approval_types,
            "primary_missing_independent_approval_type": (
                self.primary_missing_independent_approval_type
            ),
            # Each inner list is alternatives; all outer items are required.
            "requirement_groups": [
                [option.to_dict() for option in group]
                for group in self.requirement_groups
            ],
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """A non-fatal authorization/input observation."""

    code: str
    message: str
    approval_id: str | None = None
    person_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "approval_id": self.approval_id,
            "person_id": self.person_id,
            "details": json_safe(self.details),
        }


@dataclass(frozen=True)
class AuthorizationDecision:
    """Complete reconciliation result for one requested access interval."""

    service_id: str
    project_id: str
    requested_interval: Interval[Any]
    grants: tuple[Grant, ...]
    feasible_pair_intervals: tuple[Grant, ...]
    continuous_coverage: tuple[Interval[Any], ...]
    gaps: tuple[Interval[Any], ...]
    independence: IndependenceReport
    interval_independence: IndependenceReport
    findings: tuple[Finding, ...] = ()
    strict_identities: bool = False
    identity_assurance_complete: bool = True

    @property
    def grantable(self) -> bool:
        return bool(self.grants)

    @property
    def any_authorized(self) -> bool:
        return bool(self.continuous_coverage)

    @property
    def fully_authorized(self) -> bool:
        return (
            len(self.continuous_coverage) == 1
            and self.continuous_coverage[0] == self.requested_interval
        )

    @property
    def authorized(self) -> bool:
        """Whether the entire requested interval can be granted."""

        return self.fully_authorized

    @property
    def missing_independent_approval_types(self) -> list[str]:
        return self.independence.missing_independent_approval_types

    @property
    def primary_missing_independent_approval_type(self) -> str | None:
        return self.independence.primary_missing_independent_approval_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "project_id": self.project_id,
            "requested_interval": self.requested_interval.to_dict(),
            "authorized": self.authorized,
            "fully_authorized": self.fully_authorized,
            "grantable": self.grantable,
            "any_authorized": self.any_authorized,
            "grants": [grant.to_dict() for grant in self.grants],
            # Explicit alias for callers using the problem's "continuous interval" term.
            "authorized_intervals": [grant.to_dict() for grant in self.grants],
            "feasible_pair_intervals": [
                grant.to_dict() for grant in self.feasible_pair_intervals
            ],
            "continuous_coverage": [
                interval.to_dict() for interval in self.continuous_coverage
            ],
            "gaps": [interval.to_dict() for interval in self.gaps],
            "independence": self.independence.to_dict(),
            "interval_independence": self.interval_independence.to_dict(),
            "missing_independent_approval_types": (
                self.missing_independent_approval_types
            ),
            "primary_missing_independent_approval_type": (
                self.primary_missing_independent_approval_type
            ),
            "findings": [finding.to_dict() for finding in self.findings],
            "strict_identities": self.strict_identities,
            "identity_assurance_complete": self.identity_assurance_complete,
        }
