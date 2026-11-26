"""Event model, normalization, and ingestion-time issues."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC, Sequence as SequenceABC
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from .clock import ClockSpec, _finite_number


ARRIVAL = "arrival"
RATE_LIMITED = "rate_limited"
REISSUED = "reissued"
COMPLETED = "completed"
WITHDRAWN = "withdrawn"
CAPACITY_LOST = "capacity_lost"
QUEUE_EXHAUSTED = "queue_exhausted"

EVENT_KINDS = {
    ARRIVAL,
    RATE_LIMITED,
    REISSUED,
    COMPLETED,
    WITHDRAWN,
    CAPACITY_LOST,
    QUEUE_EXHAUSTED,
}

_KIND_ALIASES = {
    "arrival": ARRIVAL,
    "arrivals": ARRIVAL,
    "arrived": ARRIVAL,
    "submit": ARRIVAL,
    "submitted": ARRIVAL,
    "new_request": ARRIVAL,
    "true_arrival": ARRIVAL,
    "rate_limit": RATE_LIMITED,
    "rate_limited": RATE_LIMITED,
    "rate-limited": RATE_LIMITED,
    "ratelimit": RATE_LIMITED,
    "throttle": RATE_LIMITED,
    "throttled": RATE_LIMITED,
    "limited": RATE_LIMITED,
    "rejected": RATE_LIMITED,
    "reissue": REISSUED,
    "reissued": REISSUED,
    "retry": REISSUED,
    "retried": REISSUED,
    "recovery": REISSUED,
    "recovery_batch": REISSUED,
    "batch_recovery": REISSUED,
    "backlog_batch": REISSUED,
    "completion": COMPLETED,
    "completed": COMPLETED,
    "complete": COMPLETED,
    "service_completion": COMPLETED,
    "served": COMPLETED,
    "done": COMPLETED,
    "withdrawal": WITHDRAWN,
    "withdraw": WITHDRAWN,
    "withdrawn": WITHDRAWN,
    "cancel": WITHDRAWN,
    "cancelled": WITHDRAWN,
    "canceled": WITHDRAWN,
    "active_withdrawal": WITHDRAWN,
    "client_withdrawal": WITHDRAWN,
    "loss": CAPACITY_LOST,
    "lost": CAPACITY_LOST,
    "capacity_loss": CAPACITY_LOST,
    "capacity_lost": CAPACITY_LOST,
    "dropped": CAPACITY_LOST,
    "drop": CAPACITY_LOST,
    "queue_exhausted": QUEUE_EXHAUSTED,
    "exhausted": QUEUE_EXHAUSTED,
    "exhaustion": QUEUE_EXHAUSTED,
}

_TERMINAL_KINDS = {COMPLETED, WITHDRAWN, CAPACITY_LOST}


@dataclass
class Issue:
    code: str
    message: str
    event_id: str | None = None
    client_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "event_id": self.event_id,
            "client_id": self.client_id,
            "details": self.details,
        }


class EventValidationError(ValueError):
    """Raised by strict event parsing for an unusable event record."""


def normalize_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"event kind must be a string, got {value!r}")
    key = value.strip().lower().replace(" ", "_")
    if key in _KIND_ALIASES:
        return _KIND_ALIASES[key]
    raise EventValidationError(f"unknown event kind: {value!r}")


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        result = value.strip()
        return result or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return str(value)
    raise EventValidationError(f"{name} must be a string, got {value!r}")


@dataclass
class Event:
    """One normalized pressure-test event.

    ``count`` allows a batch report.  ``request_ids`` may optionally enumerate
    the individual requests represented by that batch.
    """

    kind: str = ARRIVAL
    timestamp: float = 0.0
    client_id: str = "control"
    event_id: str | None = None
    request_id: str | None = None
    lineage_id: str | None = None
    batch_id: str | None = None
    source_arrival_id: str | None = None
    count: int = 1
    seq: int | None = None
    seq_stream: str | None = None
    request_ids: tuple[str, ...] = ()
    clock_offset: float | None = None
    clock_error: float | None = None
    true_timestamp: float | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = normalize_kind(self.kind)
        self.timestamp = _finite_number(self.timestamp, "timestamp")
        if not isinstance(self.client_id, str) or not self.client_id:
            raise EventValidationError("client_id must be a non-empty string")
        self.event_id = _optional_string(self.event_id, "event_id")
        self.request_id = _optional_string(self.request_id, "request_id")
        self.lineage_id = _optional_string(self.lineage_id, "lineage_id")
        self.batch_id = _optional_string(self.batch_id, "batch_id")
        self.source_arrival_id = _optional_string(
            self.source_arrival_id, "source_arrival_id"
        )
        self.reason = _optional_string(self.reason, "reason")

        if isinstance(self.count, bool) or not isinstance(self.count, int):
            if isinstance(self.count, float) and self.count.is_integer():
                self.count = int(self.count)
            else:
                raise EventValidationError("count must be a positive integer")
        if self.count <= 0:
            raise EventValidationError("count must be positive")

        if self.seq is not None:
            if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
                raise EventValidationError("seq must be a non-negative integer or None")
        self.seq_stream = _optional_string(self.seq_stream, "seq_stream")

        if self.request_ids is None:
            self.request_ids = ()
        if not isinstance(self.request_ids, (tuple, list, SequenceABC)):
            raise EventValidationError("request_ids must be a sequence of strings")
        ids = tuple(_optional_string(x, "request_id") for x in self.request_ids)
        self.request_ids = tuple(x for x in ids if x)
        if self.request_id is None and len(self.request_ids) == 1:
            self.request_id = self.request_ids[0]

        if self.clock_offset is not None:
            self.clock_offset = _finite_number(self.clock_offset, "clock_offset")
        if self.clock_error is not None:
            err = _finite_number(self.clock_error, "clock_error")
            if err < 0:
                raise EventValidationError("clock_error must be non-negative")
            self.clock_error = err
        if self.true_timestamp is not None:
            self.true_timestamp = _finite_number(self.true_timestamp, "true_timestamp")

        if self.metadata is None:
            self.metadata = {}
        if not isinstance(self.metadata, MappingABC):
            raise EventValidationError("metadata must be a mapping")
        self.metadata = dict(self.metadata)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Event":
        if not isinstance(data, MappingABC):
            raise EventValidationError(f"event must be a mapping, got {type(data)!r}")
        raw = dict(data)

        def pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in raw and raw[name] is not None:
                    return raw.pop(name)
            return default

        kind = pick("kind", "type", "event_type", "event", default=ARRIVAL)
        timestamp = pick("timestamp", "ts", "time", "reported_at", "@timestamp", default=0.0)
        if isinstance(timestamp, str):
            try:
                timestamp = float(timestamp)
            except ValueError as exc:
                raise EventValidationError(
                    f"timestamp {timestamp!r} is not numeric; use numeric seconds in this API"
                ) from exc

        known = {
            "kind": kind,
            "timestamp": timestamp,
            "client_id": pick("client_id", "client", "node", "source", default="control"),
            "event_id": pick("event_id", "id", "report_id"),
            "request_id": pick("request_id", "req_id", "request"),
            "lineage_id": pick("lineage_id", "cohort_id", "original_id", "trace_id"),
            "batch_id": pick("batch_id", "recovery_batch_id"),
            "source_arrival_id": pick(
                "source_arrival_id",
                "arrival_id",
                "original_arrival_id",
                "source_event_id",
                "source_id",
            ),
            "count": pick("count", "quantity", "items", "n", default=1),
            "seq": pick("seq", "sequence", "sequence_number"),
            "seq_stream": pick("seq_stream", "stream"),
            "request_ids": pick("request_ids", "ids"),
            "clock_offset": pick("clock_offset", "offset"),
            "clock_error": pick("clock_error", "time_error_bound", "error_bound", "skew_bound"),
            "true_timestamp": pick("true_timestamp", "true_time", "actual_timestamp"),
            "reason": pick("reason", "cause", "message"),
        }
        metadata = dict(pick("metadata", default={}) or {})
        # Preserve unmapped fields without letting aliases collide.
        metadata.update(raw)
        return cls(**known, metadata=metadata)

    def subject_ids(self) -> tuple[str, ...]:
        if self.request_ids:
            return self.request_ids
        if self.request_id:
            return (self.request_id,)
        return ()

    def payload_fingerprint(self) -> str:
        """Fingerprint the logical payload while ignoring ``event_id``.

        Repeated reports often receive a new report/event id but carry the same
        occurrence fields.  Event id equality is checked separately.
        """

        payload = {
            "kind": self.kind,
            "timestamp": self.timestamp,
            "client_id": self.client_id,
            "request_id": self.request_id,
            "lineage_id": self.lineage_id,
            "batch_id": self.batch_id,
            "source_arrival_id": self.source_arrival_id,
            "count": self.count,
            "seq": self.seq,
            "seq_stream": self.seq_stream,
            "request_ids": list(self.request_ids),
            "reason": self.reason,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def clock_spec(self, default: ClockSpec) -> ClockSpec:
        offset = self.clock_offset if self.clock_offset is not None else default.offset
        error = self.clock_error if self.clock_error is not None else default.error_bound
        return ClockSpec(offset=offset, error_bound=error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "timestamp": self.timestamp,
            "client_id": self.client_id,
            "event_id": self.event_id,
            "request_id": self.request_id,
            "lineage_id": self.lineage_id,
            "batch_id": self.batch_id,
            "source_arrival_id": self.source_arrival_id,
            "count": self.count,
            "seq": self.seq,
            "seq_stream": self.seq_stream,
            "request_ids": list(self.request_ids),
            "clock_offset": self.clock_offset,
            "clock_error": self.clock_error,
            "true_timestamp": self.true_timestamp,
            "reason": self.reason,
            "metadata": self.metadata,
        }


def parse_events(records: Iterable[Event | Mapping[str, Any]]) -> list[Event]:
    events: list[Event] = []
    for index, record in enumerate(records):
        if isinstance(record, Event):
            events.append(record)
        elif isinstance(record, MappingABC):
            events.append(Event.from_mapping(record))
        else:
            raise EventValidationError(
                f"event at index {index} must be Event or mapping, got {type(record)!r}"
            )
    return events
