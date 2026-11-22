"""Core data model for the SLI release-adjudication module.

The module is built around three ideas:

* One user request == exactly one *sample*.  A single request can be observed
  several times (edge entry, downstream, receipt stream, retries), but it must
  only ever contribute one sample to the denominator.
* Windows are assigned by the request's *first entry* timestamp and judged with
  the rule that was in force at that entry time.
* A window's verdict is computed from the lower and upper bound of the *bad
  request ratio*, where undecided samples are pessimistically counted as
  failures (upper bound) or optimistically as successes (lower bound).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Union

# ---------------------------------------------------------------------------
# Constants / vocabularies
# ---------------------------------------------------------------------------

STREAM_EDGE = "edge"
STREAM_DOWNSTREAM = "downstream"
STREAM_RECEIPT = "receipt"
STREAMS = frozenset({STREAM_EDGE, STREAM_DOWNSTREAM, STREAM_RECEIPT})

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUSES = frozenset({STATUS_SUCCESS, STATUS_FAILURE})

# Internal per-request outcome classes.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_PENDING = "pending"          # undecided (未决)
OUTCOMES = frozenset({OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_PENDING})

# Window verdicts.
VERDICT_ALLOW = "allow"              # 可放
VERDICT_REJECT = "reject"            # 拒绝
VERDICT_WAIT = "wait"                # 等证据

KIND_OBSERVATION = "observation"
KIND_CORRECTION = "correction"

Number = Union[int, float, Fraction, str]


def to_fraction(value: Number) -> Fraction:
    """Convert a number / numeric string to a :class:`Fraction` exactly.

    A ``float`` is converted through its exact binary value (``Fraction(0.1)``
    is *not* ``1/10``).  Pass a string such as ``"1/25"`` when decimal
    exactness matters at a decision boundary.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid ratio")
    if isinstance(value, (int, float, str)):
        return Fraction(value)
    raise TypeError(f"unsupported ratio type: {type(value)!r}")


def _require_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value)!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """The SLI rule in force from ``effective_from`` onwards.

    ``effective_from`` is compared against a request's first-entry timestamp, so
    a request that entered before a rule change is judged with the older rule.
    """

    effective_from: float
    allowed_latency_ms: float
    requires_receipt: bool
    user_deadline_ms: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "effective_from", _require_finite("effective_from", self.effective_from)
        )
        allowed = _require_finite("allowed_latency_ms", self.allowed_latency_ms)
        deadline = _require_finite("user_deadline_ms", self.user_deadline_ms)
        if allowed < 0:
            raise ValueError("allowed_latency_ms must be >= 0")
        if deadline < 0:
            raise ValueError("user_deadline_ms must be >= 0")
        if not isinstance(self.requires_receipt, bool):
            raise TypeError("requires_receipt must be a bool")
        object.__setattr__(self, "allowed_latency_ms", allowed)
        object.__setattr__(self, "user_deadline_ms", deadline)


# ---------------------------------------------------------------------------
# Input records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One sighting of a user request on some stream.

    Fields
    ------
    request_id:
        Stable identity of the *user request*.  Repeated sightings with the same
        id collapse into one sample.
    entry_time:
        First entry timestamp (event time, seconds).  Drives window assignment
        and rule selection.
    attempt:
        Logical attempt number; a retry has a higher attempt than the original.
    version:
        Observation version.  Higher versions are newer and, once present,
        constitute the "valid version" that supersedes lower ones.
    stream:
        ``edge`` / ``downstream`` / ``receipt``.
    status:
        ``success`` / ``failure`` as reported by this sighting.
    latency_ms:
        Measured service latency.  ``None`` falls back to the *elapsed* time
        ``(observed_at - entry_time)``.
    receipt_present:
        Whether a final receipt accompanies this sighting.
    observed_at:
        Event time at which this fact became confirmable (seconds).  Defaults to
        ``entry_time``.
    """

    request_id: str
    entry_time: float
    attempt: int
    version: int
    stream: str
    status: str
    latency_ms: Optional[float] = None
    receipt_present: bool = False
    observed_at: Optional[float] = None

    def __post_init__(self) -> None:
        _validate_common(self.request_id, self.entry_time, self.attempt, self.version)
        if self.stream not in STREAMS:
            raise ValueError(f"unknown stream {self.stream!r}; expected one of {sorted(STREAMS)}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}; expected one of {sorted(STATUSES)}")
        _validate_latency(self.latency_ms)
        if not isinstance(self.receipt_present, bool):
            raise TypeError("receipt_present must be a bool")
        observed = self.entry_time if self.observed_at is None else _require_finite(
            "observed_at", self.observed_at
        )
        if observed < self.entry_time:
            raise ValueError("observed_at must not precede entry_time")
        object.__setattr__(self, "observed_at", observed)


@dataclass(frozen=True)
class Correction:
    """A late correction that supersedes earlier sightings of a request.

    A correction is the operator's authoritative repair of a previous
    classification.  As long as the window has not been sealed, applying it
    adjusts the success / failure / pending counts and appends an incremental
    log entry.

    ``result_at`` optionally records *when the true result became confirmable*.
    When it is provided and falls after the entry rule's user deadline the
    sample is demoted to *pending*; when it is ``None`` the correction is taken
    as authoritative and the deadline gate is not applied.
    """

    request_id: str
    entry_time: float
    attempt: int
    version: int
    status: str
    latency_ms: Optional[float] = None
    receipt_present: bool = False
    observed_at: Optional[float] = None
    result_at: Optional[float] = None
    note: str = ""

    def __post_init__(self) -> None:
        _validate_common(self.request_id, self.entry_time, self.attempt, self.version)
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}; expected one of {sorted(STATUSES)}")
        _validate_latency(self.latency_ms)
        if not isinstance(self.receipt_present, bool):
            raise TypeError("receipt_present must be a bool")
        observed = self.entry_time if self.observed_at is None else _require_finite(
            "observed_at", self.observed_at
        )
        if observed < self.entry_time:
            raise ValueError("observed_at must not precede entry_time")
        object.__setattr__(self, "observed_at", observed)
        result = observed if self.result_at is None else _require_finite("result_at", self.result_at)
        if result < self.entry_time:
            raise ValueError("result_at must not precede entry_time")
        object.__setattr__(self, "result_at", self.result_at)
        if not isinstance(self.note, str):
            raise TypeError("note must be a string")


def _validate_common(request_id: str, entry_time: float, attempt: int, version: int) -> None:
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a non-empty string")
    _require_finite("entry_time", entry_time)
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise TypeError("attempt must be an int")
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("version must be an int")
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    if version < 0:
        raise ValueError("version must be >= 0")


def _validate_latency(latency_ms: Optional[float]) -> None:
    if latency_ms is None:
        return
    value = _require_finite("latency_ms", latency_ms)
    if value < 0:
        raise ValueError("latency_ms must be >= 0")


# ---------------------------------------------------------------------------
# Output value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    success: int = 0
    failure: int = 0
    pending: int = 0

    @property
    def total(self) -> int:
        return self.success + self.failure + self.pending


@dataclass(frozen=True)
class Decision:
    """A window's adjudication, including the bad-ratio bounds."""

    window_id: int
    window_start: float
    window_end: float
    verdict: str
    lower_bound: float          # bad-ratio lower bound (pending counted as success)
    upper_bound: float          # bad-ratio upper bound (pending counted as failure)
    total: int
    success: int
    failure: int
    pending: int
    threshold: float
    sealed: bool

    @property
    def has_samples(self) -> bool:
        return self.total > 0


@dataclass(frozen=True)
class LogEntry:
    """One incremental adjustment to a window (audit trail).

    ``event_time`` is the event time of the record that produced this entry (the
    ``observed_at`` of an observation/correction, or the watermark for a seal).
    """

    seq: int
    window_id: int
    request_id: Optional[str]
    cause: str                  # observation | correction | seal
    applied: bool
    reason: Optional[str]
    prev_outcome: Optional[str]
    new_outcome: Optional[str]
    delta_success: int
    delta_failure: int
    delta_pending: int
    counts: Counts
    lower_bound: float
    upper_bound: float
    verdict_before: Optional[str]
    verdict_after: Optional[str]
    event_time: float
    watermark: float
