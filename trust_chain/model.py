"""Data types used by the trust-chain validator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class TrustValidationError(ValueError):
    """Base class for invalid validator inputs."""


class InvalidTicketError(TrustValidationError):
    """Raised when a ticket is structurally unusable."""


class InvalidPolicyError(TrustValidationError):
    """Raised when trust or policy configuration is invalid."""


@dataclass(frozen=True)
class ParentSegment:
    """A compressed ``start -> end`` portion of a ticket route.

    The segment is expanded through immediate parent-domain edges.  When
    ``start`` is omitted, it is inferred from the preceding route domain.
    """

    start: str | None = None
    end: str | None = None
    compressed: bool = True

    # Descriptive alias used in some ticket schemas.
    @property
    def segment_type(self) -> str:
        return "parent_chain"


CompressedSegment = ParentSegment


@dataclass(frozen=True)
class Ticket:
    """Verified ticket claims.

    ``transit`` contains domains and :class:`ParentSegment` tokens between
    ``origin`` and ``target``.  Alternatively, ``path`` may be the complete
    route specification, including endpoints.
    """

    origin: str | None = None
    target: str | None = None
    transit: tuple[Any, ...] | list[Any] = ()
    path: tuple[Any, ...] | list[Any] | None = None


@dataclass(frozen=True)
class ReviewRequirement:
    """Policy requiring ``source`` to pass through ``reviewer``.

    ``targets=None`` means the requirement applies to every target protected by
    this validator.  A frozenset restricts it to selected ticket targets.
    """

    source: str
    reviewer: str
    targets: frozenset[str] | None = None


@dataclass
class ValidationResult:
    """Outcome of validating one verified ticket summary.

    ``position`` is the index in :attr:`expanded_path` of the domain responsible
    for the reported decision.  For a rejected hop it is the issuing/current
    domain; for a missing review it is the first occurrence of the policy
    source.  Additional coordinates are available on the object and in
    :meth:`to_dict`.
    """

    allowed: bool
    code: str
    reason: str
    expanded_path: tuple[str, ...] = ()
    position: int | None = None
    domain: str | None = None
    next_domain: str | None = None
    edge_index: int | None = None
    item_index: int | None = None
    source_position: int | None = None
    target_position: int | None = None
    source: str | None = None
    required_reviewer: str | None = None
    target: str | None = None
    cycle_domain: str | None = None
    cycle_start_position: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    # Convenience aliases for callers that prefer boolean-ish fields.
    @property
    def ok(self) -> bool:
        return self.allowed

    @property
    def valid(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "allowed": self.allowed,
            "ok": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "expanded_path": list(self.expanded_path),
            "position": self.position,
            "domain": self.domain,
            "next_domain": self.next_domain,
            "edge_index": self.edge_index,
            "item_index": self.item_index,
            "source_position": self.source_position,
            "target_position": self.target_position,
            "source": self.source,
            "required_reviewer": self.required_reviewer,
            "target": self.target,
            "cycle_domain": self.cycle_domain,
            "cycle_start_position": self.cycle_start_position,
            "details": dict(self.details),
        }
        return data
