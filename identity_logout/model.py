"""Public data models for the logout-completion decider.

The models are intentionally plain and JSON-friendly.  The decider accepts
these objects directly, but :func:`identity_logout.decide_logout_completion`
also accepts ordinary dictionaries for concise synthetic tests and scripts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class EventKind(str, Enum):
    """Normalized event categories used by the decider."""

    DERIVATION = "derivation"
    TRANSFER = "transfer"
    TRANSFER_RECEIPT = "transfer_receipt"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class Event:
    """One causal session event.

    ``predecessor_id`` is the identifier of the event's causal predecessor.
    It is an event reference, not the parent session identifier; the latter is
    :attr:`parent_session_id`.
    """

    event_id: str
    kind: EventKind
    session_id: str | None = None
    issuer: str | None = None
    sequence: int | None = None
    predecessor_id: str | None = None
    predecessor_endpoint: str | None = None
    parent_session_id: str | None = None
    child_session_id: str | None = None
    transfer_id: str | None = None
    from_endpoint: str | None = None
    to_endpoint: str | None = None
    receipt_id: str | None = None
    acknowledges_transfer_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def primary_session_id(self) -> str | None:
        """Session principally named by the event."""

        return self.child_session_id or self.session_id

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "issuer": self.issuer,
            "sequence": self.sequence,
            "predecessor_id": self.predecessor_id,
            "predecessor_endpoint": self.predecessor_endpoint,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "transfer_id": self.transfer_id,
            "from_endpoint": self.from_endpoint,
            "to_endpoint": self.to_endpoint,
            "receipt_id": self.receipt_id,
            "acknowledges_transfer_id": self.acknowledges_transfer_id,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class Watermark:
    """A signing endpoint's highest contiguous event sequence."""

    endpoint: str
    sequence: int

    def __post_init__(self) -> None:
        if not self.endpoint:
            raise ValueError("watermark endpoint must not be empty")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("watermark sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("watermark sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class RevocationBarrier:
    """Per-endpoint coordinates that prove revocation was observed.

    ``positions[endpoint]`` is the barrier sequence for that endpoint.  An
    endpoint's continuous watermark satisfies the barrier when it is greater
    than or equal to the required coordinate.  ``default_sequence`` may be used
    when all relevant endpoints share one barrier coordinate.
    """

    root_session_id: str | None = None
    positions: Mapping[str, int] = field(default_factory=dict)
    default_sequence: int | None = None
    barrier_id: str | None = None

    def required_sequence(self, endpoint: str) -> int | None:
        if endpoint in self.positions:
            return self.positions[endpoint]
        return self.default_sequence


@dataclass(frozen=True, slots=True)
class MissingEvidence:
    """One piece of evidence preventing a completion receipt."""

    kind: str
    endpoint: str | None = None
    session_id: str | None = None
    transfer_id: str | None = None
    event_id: str | None = None
    required: int | str | None = None
    actual: int | str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "endpoint": self.endpoint,
            "session_id": self.session_id,
            "transfer_id": self.transfer_id,
            "event_id": self.event_id,
            "required": self.required,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    """Deterministic completion receipt and the evidence it binds."""

    receipt_id: str
    root_session_id: str
    descendant_count: int
    closed_session_ids: tuple[str, ...]
    transfer_ids: tuple[str, ...]
    relevant_endpoints: tuple[str, ...]
    watermarks: Mapping[str, int]
    evidence_digest: str

    @property
    def issued(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "root_session_id": self.root_session_id,
            "issued": True,
            "descendant_count": self.descendant_count,
            "closed_session_ids": list(self.closed_session_ids),
            "transfer_ids": list(self.transfer_ids),
            "relevant_endpoints": list(self.relevant_endpoints),
            "watermarks": dict(sorted(self.watermarks.items())),
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class LogoutDecision:
    """Result of evaluating one ancestor-session revocation."""

    root_session_id: str
    complete: bool
    root_known: bool
    descendant_count: int
    closed_descendant_ids: tuple[str, ...]
    open_descendant_ids: tuple[str, ...]
    in_flight_transfer_ids: tuple[str, ...]
    relevant_endpoints: tuple[str, ...]
    watermarks: Mapping[str, int]
    missing: tuple[MissingEvidence, ...]
    receipt: CompletionReceipt | None = None

    @property
    def missing_endpoints(self) -> tuple[str, ...]:
        """Endpoints owing at least one piece of evidence, in stable order."""

        endpoints: list[str] = []
        for item in self.missing:
            if item.endpoint and item.endpoint not in endpoints:
                endpoints.append(item.endpoint)
        return tuple(endpoints)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Deduplicated machine-readable missing-evidence reason codes."""

        codes: list[str] = []
        for item in self.missing:
            if item.kind not in codes:
                codes.append(item.kind)
        return tuple(codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_session_id": self.root_session_id,
            "complete": self.complete,
            "root_known": self.root_known,
            "descendant_count": self.descendant_count,
            "closed_descendant_ids": list(self.closed_descendant_ids),
            "open_descendant_ids": list(self.open_descendant_ids),
            "in_flight_transfer_ids": list(self.in_flight_transfer_ids),
            "relevant_endpoints": list(self.relevant_endpoints),
            "watermarks": dict(sorted(self.watermarks.items())),
            "missing_endpoints": list(self.missing_endpoints),
            "reason_codes": list(self.reason_codes),
            "missing": [item.to_dict() for item in self.missing],
            "receipt": self.receipt.to_dict() if self.receipt else None,
        }
