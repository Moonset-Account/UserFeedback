"""Core immutable data models for the authorization checker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Generic, Iterable, TypeVar

T = TypeVar("T")


class ApprovalKind(str, Enum):
    """The two approval responsibilities in the request."""

    BUSINESS_OWNER = "business_owner"
    ACCOUNT_CUSTODIAN = "account_custodian"

    @property
    def role_label(self) -> str:
        if self is ApprovalKind.BUSINESS_OWNER:
            return "business owner"
        return "account custodian"


_KIND_ALIASES = {
    ApprovalKind.BUSINESS_OWNER.value: ApprovalKind.BUSINESS_OWNER,
    "business": ApprovalKind.BUSINESS_OWNER,
    "owner": ApprovalKind.BUSINESS_OWNER,
    "bo": ApprovalKind.BUSINESS_OWNER,
    "business-owner": ApprovalKind.BUSINESS_OWNER,
    "business_owner_approval": ApprovalKind.BUSINESS_OWNER,
    "businessapproval": ApprovalKind.BUSINESS_OWNER,
    ApprovalKind.ACCOUNT_CUSTODIAN.value: ApprovalKind.ACCOUNT_CUSTODIAN,
    "custodian": ApprovalKind.ACCOUNT_CUSTODIAN,
    "account": ApprovalKind.ACCOUNT_CUSTODIAN,
    "ac": ApprovalKind.ACCOUNT_CUSTODIAN,
    "account-custodian": ApprovalKind.ACCOUNT_CUSTODIAN,
    "account_custodian_approval": ApprovalKind.ACCOUNT_CUSTODIAN,
    "custodian_approval": ApprovalKind.ACCOUNT_CUSTODIAN,
}


def normalize_kind(value: ApprovalKind | str) -> ApprovalKind:
    """Normalize common approval-type spellings."""

    if isinstance(value, ApprovalKind):
        return value
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key in _KIND_ALIASES:
        return _KIND_ALIASES[key]
    # Also accept the enum's plain value after repeated underscores collapse.
    collapsed = "_".join(part for part in key.split("_") if part)
    if collapsed in _KIND_ALIASES:
        return _KIND_ALIASES[collapsed]
    raise ValueError(f"unknown approval kind: {value!r}")


@dataclass(frozen=True, slots=True)
class Interval(Generic[T]):
    """A half-open interval ``[start, end)``.

    Half-open semantics make a boundary clean: an interval ending at ``t`` and
    another starting at ``t`` touch without either losing or double-counting
    the boundary instant.
    """

    start: T
    end: T

    def __post_init__(self) -> None:
        if self.start is None or self.end is None:
            raise ValueError("interval endpoints must not be None")
        try:
            valid = self.start < self.end
        except TypeError as exc:  # incomparable endpoint types
            raise TypeError("interval endpoints must be ordered values") from exc
        if not valid:
            raise ValueError("interval start must be strictly before end")

        if isinstance(self.start, datetime) and isinstance(self.end, datetime):
            naive_start = self.start.tzinfo is None
            naive_end = self.end.tzinfo is None
            if naive_start != naive_end:
                raise ValueError(
                    "datetime endpoints must both be timezone-aware or both naive"
                )

    def contains(self, value: T) -> bool:
        return self.start <= value < self.end

    def overlaps(self, other: "Interval[Any]") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval[T]") -> "Interval[T] | None":
        left = max(self.start, other.start)
        right = min(self.end, other.end)
        if left < right:
            return Interval(left, right)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"start": json_safe(self.start), "end": json_safe(self.end)}


def date_interval(start: date, inclusive_end: date) -> Interval[date]:
    """Create a date interval from an inclusive end date."""

    if isinstance(start, datetime) or isinstance(inclusive_end, datetime):
        raise TypeError("date_interval expects date objects, not datetimes")
    return Interval(start, inclusive_end + timedelta(days=1))


def merge_intervals(intervals: Iterable[Interval[T]]) -> list[Interval[T]]:
    """Merge overlapping or touching half-open intervals."""

    ordered = sorted(intervals, key=lambda item: item.start)
    merged: list[Interval[T]] = []
    for interval in ordered:
        if not merged:
            merged.append(interval)
            continue
        current = merged[-1]
        if interval.start <= current.end:
            if interval.end > current.end:
                merged[-1] = Interval(current.start, interval.end)
        else:
            merged.append(interval)
    return merged


def subtract_intervals(
    source: Interval[T], covered: Iterable[Interval[T]]
) -> list[Interval[T]]:
    """Return the parts of ``source`` not covered by ``covered``."""

    gaps: list[Interval[T]] = []
    cursor = source.start
    for interval in merge_intervals(covered):
        clipped = interval.intersection(source)
        if clipped is None:
            continue
        if cursor < clipped.start:
            gaps.append(Interval(cursor, clipped.start))
        if cursor < clipped.end:
            cursor = clipped.end
    if cursor < source.end:
        gaps.append(Interval(cursor, source.end))
    return gaps


@dataclass(frozen=True, slots=True)
class Identity:
    """A directory-specific principal name for a natural person."""

    directory: str
    subject: str

    def __post_init__(self) -> None:
        directory = str(self.directory).strip()
        subject = str(self.subject).strip()
        if not directory or not subject:
            raise ValueError("identity directory and subject are required")
        object.__setattr__(self, "directory", directory)
        object.__setattr__(self, "subject", subject)

    @property
    def urn(self) -> str:
        return f"{self.directory}:{self.subject}"

    def __str__(self) -> str:
        return self.urn

    @classmethod
    def parse(cls, value: "Identity | dict[str, Any] | str") -> "Identity":
        if isinstance(value, Identity):
            return value
        if isinstance(value, dict):
            directory = (
                value.get("directory")
                or value.get("directory_id")
                or value.get("issuer")
            )
            subject = (
                value.get("subject")
                or value.get("subject_id")
                or value.get("id")
                or value.get("username")
                or value.get("email")
                or value.get("principal")
            )
            if directory is None or subject is None:
                raise ValueError(f"invalid identity mapping: {value!r}")
            return cls(str(directory), str(subject))
        text = str(value)
        if ":" in text:
            directory, subject = text.split(":", 1)
            return cls(directory, subject)
        return cls("default", text)

    def to_dict(self) -> dict[str, str]:
        return {"directory": self.directory, "subject": self.subject, "urn": self.urn}


@dataclass(frozen=True, slots=True)
class NaturalPerson:
    """A canonical human and all known directory identities."""

    person_id: str
    identities: tuple[Identity, ...] = ()

    def __post_init__(self) -> None:
        person_id = str(self.person_id).strip()
        if not person_id:
            raise ValueError("person_id is required")
        object.__setattr__(self, "person_id", person_id)
        object.__setattr__(self, "identities", tuple(self.identities))


@dataclass(frozen=True, slots=True)
class Scope:
    """Jurisdiction/scope constraints.

    ``None`` means that dimension is unrestricted. A non-empty frozenset requires
    the requested target to be a member. An explicitly empty frozenset matches
    nothing.
    """

    project_ids: frozenset[str] | None = None
    service_ids: frozenset[str] | None = None
    jurisdiction_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        for field_name in ("project_ids", "service_ids", "jurisdiction_ids"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, str):
                normalized = frozenset({value})
            else:
                normalized = frozenset(str(item) for item in value)
            object.__setattr__(self, field_name, normalized)

    @classmethod
    def unrestricted(cls) -> "Scope":
        return cls()

    @classmethod
    def of(
        cls,
        *,
        project: str | Iterable[str] | None = None,
        service: str | Iterable[str] | None = None,
        jurisdiction: str | Iterable[str] | None = None,
    ) -> "Scope":
        return cls(
            project_ids=_as_set(project),
            service_ids=_as_set(service),
            jurisdiction_ids=_as_set(jurisdiction),
        )

    def covers(
        self,
        *,
        project_id: str,
        service_id: str,
        jurisdiction_id: str | None = None,
    ) -> bool:
        if self.project_ids is not None and str(project_id) not in self.project_ids:
            return False
        if self.service_ids is not None and str(service_id) not in self.service_ids:
            return False
        if self.jurisdiction_ids is not None:
            if jurisdiction_id is None:
                return False
            if str(jurisdiction_id) not in self.jurisdiction_ids:
                return False
        return True


def _as_set(value: str | Iterable[str] | None) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(item) for item in value)


@dataclass(frozen=True, slots=True)
class Approval:
    """One typed approval issued by a directory identity."""

    approval_id: str
    kind: ApprovalKind
    identity: Identity
    interval: Interval[Any]
    person_id: str | None = None
    scope: Scope = field(default_factory=Scope.unrestricted)
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not str(self.approval_id).strip():
            raise ValueError("approval_id is required")
        kind = normalize_kind(self.kind)
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.identity, Identity):
            object.__setattr__(self, "identity", Identity.parse(self.identity))
        if not isinstance(self.scope, Scope):
            raise TypeError("scope must be a Scope instance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind.value,
            "identity": self.identity.to_dict(),
            "person_id": self.person_id,
            "interval": self.interval.to_dict(),
            "scope": {
                "project_ids": _sorted_or_none(self.scope.project_ids),
                "service_ids": _sorted_or_none(self.scope.service_ids),
                "jurisdiction_ids": _sorted_or_none(self.scope.jurisdiction_ids),
            },
            "details": json_safe(self.details or {}),
        }


def _sorted_or_none(value: frozenset[str] | None) -> list[str] | None:
    return None if value is None else sorted(value)


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    service_id: str
    project_id: str
    interval: Interval[Any]
    jurisdiction_id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.service_id).strip():
            raise ValueError("service_id is required")
        if not str(self.project_id).strip():
            raise ValueError("project_id is required")


def json_safe(value: Any) -> Any:
    """Convert common model/date values to JSON-compatible data."""

    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Interval):
        return value.to_dict()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset, tuple, list)):
        return [json_safe(item) for item in value]
    return value
