"""Domain model for the privacy-review preview/publish module.

All data handled here is *synthetic*: subject identifiers are opaque internal
tokens (``s1`` ...) and are never carried into review results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping


class Missing:
    """Sentinel representing a value a member can see because it is missing.

    A historical column that does not cover a subject is still part of the
    member's visible row: the absence itself acts as one distinct visible value
    during grouping, as required by the task.
    """

    _instance: "Missing | None" = None

    def __new__(cls) -> "Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __str__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:  # absence is a real visible value, not "falsy"
        return True


# Module-level singleton used everywhere a missing-but-visible value is needed.
MISSING = Missing()


class Decision(str, Enum):
    """Outcome of a review evaluation."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


def _sorted_groups(groups: Any) -> tuple[str, ...]:
    if not groups:
        raise ValueError("member must belong to at least one analysis group")
    if not all(isinstance(g, str) and g for g in groups):
        raise ValueError("group ids must be non-empty strings")
    return tuple(sorted(dict.fromkeys(groups)))


@dataclass(frozen=True)
class Member:
    """An analyst who may belong to one or several analysis groups."""

    member_id: str
    groups: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.member_id, str) or not self.member_id:
            raise ValueError("member_id must be a non-empty string")
        object.__setattr__(self, "groups", _sorted_groups(self.groups))

    @property
    def combination(self) -> tuple[str, ...]:
        """Canonical audience-group combination this member belongs to."""
        return self.groups


@dataclass(frozen=True)
class PublishedView:
    """A previously released view, visible only to one analysis group.

    ``records`` maps an internal subject id to the visible column values.
    Columns absent from a subject's mapping are surfaced as :data:`MISSING`.
    """

    version: int
    group_id: str
    columns: tuple[str, ...]
    records: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
        if not isinstance(self.group_id, str) or not self.group_id:
            raise ValueError("group_id must be a non-empty string")
        if not self.columns or not all(isinstance(c, str) and c for c in self.columns):
            raise ValueError("columns must be a non-empty sequence of strings")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be unique")
        for subject, row in self.records.items():
            if not isinstance(subject, str) or not subject:
                raise ValueError("subject ids must be non-empty strings")
            unknown = set(row) - set(self.columns)
            if unknown:
                raise ValueError(f"row for {subject!r} has unknown columns {sorted(unknown)}")
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(
            self,
            "records",
            {s: dict(row) for s, row in self.records.items()},
        )


@dataclass(frozen=True)
class PublishRequest:
    """A proposed release of new columns for one batch of internal subjects."""

    request_id: str
    columns: tuple[str, ...]
    records: Mapping[str, Mapping[str, Any]]
    audience_groups: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a non-empty string")
        if not self.columns or not all(isinstance(c, str) and c for c in self.columns):
            raise ValueError("columns must be a non-empty sequence of strings")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be unique")
        if not self.records:
            raise ValueError("records must contain at least one subject")
        for subject, row in self.records.items():
            if not isinstance(subject, str) or not subject:
                raise ValueError("subject ids must be non-empty strings")
            if set(row) != set(self.columns):
                raise ValueError(
                    f"row for {subject!r} must define exactly the request columns"
                )
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(
            self, "records", {s: dict(row) for s, row in self.records.items()}
        )
        object.__setattr__(self, "audience_groups", _sorted_groups(self.audience_groups))


@dataclass(frozen=True)
class Risk:
    """One under-sized equivalence class finding.

    Subject identifiers are deliberately omitted; only the audience-group
    combination whose view creates the small class is reported.
    """

    audience_combination: tuple[str, ...]
    minimum_group_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "audience_combination": list(self.audience_combination),
            "minimum_group_size": self.minimum_group_size,
        }


@dataclass(frozen=True)
class ReviewResult:
    """Result of a preview or publish evaluation."""

    request_id: str
    decision: Decision
    base_version: int
    k: int
    risks: tuple[Risk, ...] = field(default_factory=tuple)
    minimum_group_size: int | None = None
    recomputed: bool = False
    published_version: int | None = None

    @property
    def accepted(self) -> bool:
        return self.decision is Decision.ACCEPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "decision": self.decision.value,
            "base_version": self.base_version,
            "k": self.k,
            "risks": [r.to_dict() for r in self.risks],
            "minimum_group_size": self.minimum_group_size,
            "recomputed": self.recomputed,
            "published_version": self.published_version,
        }


FrozenGroupSet = FrozenSet[str]
