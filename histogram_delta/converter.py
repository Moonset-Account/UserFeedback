"""Cumulative-histogram delta conversion for the collector.

Collector agents report, per process, periodic samples containing:

* a process identity (``process_id`` plus the process start identifier
  ``process_start``);
* a sample timestamp;
* bucket upper bounds in strictly ascending order, the last one being
  positive infinity;
* the cumulative request count reached at each upper bound.

Downstream consumers need the number of *new* requests that fell into
each non-overlapping bucket between two adjacent samples.  This module
performs that conversion and enforces the following rules:

1. Settlement only uses upper bounds shared by the two samples.  When
   bucket boundaries change, requests recorded against old buckets are
   never guessed or split into the new bucket layout.
2. The first sample seen for a process only establishes a baseline and
   produces no increments.  A changed process start identifier is
   treated as a process restart, whose first sample likewise only
   establishes a baseline.
3. If a sample implies a negative interval increment, the sample is
   rejected and the last valid baseline is retained.  The next good
   sample therefore settles across the rejected one.
4. An identical retransmission at the same timestamp is ignored; a
   conflicting retransmission (same timestamp, different payload) is
   surfaced to the caller as an exception.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

__all__ = [
    "INF",
    "ProcessSample",
    "BucketIncrement",
    "Settlement",
    "BaselineEstablished",
    "Retransmission",
    "ConversionResult",
    "ConversionError",
    "InvalidSampleError",
    "OutOfOrderTimestampError",
    "ConflictingRetransmissionError",
    "NegativeIncrementError",
    "HistogramDeltaConverter",
    "compute_increments",
]

#: Positive infinity, used as the mandatory last bucket upper bound.
INF = math.inf


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConversionError(Exception):
    """Base class for every error raised by this module."""


class InvalidSampleError(ConversionError):
    """The sample is structurally invalid and cannot be processed at all."""


class OutOfOrderTimestampError(ConversionError):
    """A sample arrived with a timestamp older than the retained baseline."""

    def __init__(self, process_id: str, timestamp: float, baseline_timestamp: float):
        self.process_id = process_id
        self.timestamp = timestamp
        self.baseline_timestamp = baseline_timestamp
        super().__init__(
            "sample timestamp %s for process %r is older than baseline timestamp %s"
            % (timestamp, process_id, baseline_timestamp)
        )


class ConflictingRetransmissionError(ConversionError):
    """A same-timestamp sample carries different data than the baseline."""

    def __init__(self, process_id: str, timestamp: float):
        self.process_id = process_id
        self.timestamp = timestamp
        super().__init__(
            "conflicting retransmission for process %r at timestamp %s: "
            "same timestamp as the retained sample but different payload"
            % (process_id, timestamp)
        )


class NegativeIncrementError(ConversionError):
    """Settlement produced a negative number of new requests in an interval."""

    def __init__(
        self,
        process_id: str,
        baseline_timestamp: float,
        sample_timestamp: float,
        lower_bound: float,
        upper_bound: float,
        interval_increment: int,
    ):
        self.process_id = process_id
        self.baseline_timestamp = baseline_timestamp
        self.sample_timestamp = sample_timestamp
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.interval_increment = interval_increment
        super().__init__(
            "negative interval increment %s for process %r on interval (%s, %s] "
            "between timestamps %s and %s; sample rejected"
            % (
                interval_increment,
                process_id,
                lower_bound,
                upper_bound,
                baseline_timestamp,
                sample_timestamp,
            )
        )


# ---------------------------------------------------------------------------
# Input and output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessSample:
    """One collector report for a single process.

    ``upper_bounds`` are strictly ascending bucket upper bounds and the
    last element must be :data:`math.inf`.  ``cumulative_counts`` are the
    cumulative request counts at the corresponding bounds and must be
    non-decreasing non-negative integers.
    """

    process_id: str
    process_start: str
    timestamp: float
    upper_bounds: Tuple[float, ...]
    cumulative_counts: Tuple[int, ...]

    @classmethod
    def build(
        cls,
        process_id: str,
        process_start: str,
        timestamp: float,
        upper_bounds: Tuple[float, ...],
        cumulative_counts: Tuple[int, ...],
    ) -> "ProcessSample":
        """Validate the raw values and construct a :class:`ProcessSample`."""
        bounds = tuple(upper_bounds)
        counts = tuple(cumulative_counts)
        _validate_sample(process_id, process_start, timestamp, bounds, counts)
        return cls(process_id, process_start, timestamp, bounds, counts)


@dataclass(frozen=True)
class BucketIncrement:
    """New requests counted inside the half-open interval
    ``(lower_bound, upper_bound]``."""

    lower_bound: float
    upper_bound: float
    new_requests: int


@dataclass(frozen=True)
class Settlement:
    """The result of settling one sample against the retained baseline."""

    process_id: str
    process_start: str
    baseline_timestamp: float
    sample_timestamp: float
    increments: Tuple[BucketIncrement, ...]
    #: Upper bounds present in both samples; settlement only covers these.
    common_upper_bounds: Tuple[float, ...]
    #: ``True`` when the two samples did not use the exact same bound set.
    boundaries_changed: bool

    @property
    def total_new_requests(self) -> int:
        """Sum of every settled interval increment."""
        return sum(item.new_requests for item in self.increments)


@dataclass(frozen=True)
class BaselineEstablished:
    """A first sample (or first sample after a restart) only sets a baseline."""

    process_id: str
    process_start: str
    timestamp: float


@dataclass(frozen=True)
class Retransmission:
    """An identical same-timestamp retransmission; it was ignored."""

    process_id: str
    timestamp: float
    ignored: bool = True


#: Anything :meth:`HistogramDeltaConverter.submit` may return.
ConversionResult = Union[BaselineEstablished, Retransmission, Settlement]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_real_number(value: object) -> bool:
    # ``bool`` is an ``int`` subclass but must never be accepted as a number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_sample(
    process_id: object,
    process_start: object,
    timestamp: object,
    bounds: Tuple[object, ...],
    counts: Tuple[object, ...],
) -> None:
    if not isinstance(process_id, str) or not process_id:
        raise InvalidSampleError("process_id must be a non-empty string")
    if not isinstance(process_start, str) or not process_start:
        raise InvalidSampleError("process_start must be a non-empty string")
    if not _is_real_number(timestamp) or not math.isfinite(timestamp):
        raise InvalidSampleError("timestamp must be a finite int or float")

    if not isinstance(bounds, tuple) or not bounds:
        raise InvalidSampleError("upper_bounds must be a non-empty tuple")
    if not isinstance(counts, tuple) or len(counts) != len(bounds):
        raise InvalidSampleError(
            "cumulative_counts must be a tuple with the same length as upper_bounds"
        )

    previous_bound: Optional[float] = None
    last_index = len(bounds) - 1
    for index, bound in enumerate(bounds):
        if not _is_real_number(bound):
            raise InvalidSampleError("upper_bounds[%d] must be an int or float" % index)
        if index == last_index:
            if bound != INF:
                raise InvalidSampleError(
                    "the last upper bound must be positive infinity (math.inf)"
                )
        elif not math.isfinite(bound):
            raise InvalidSampleError(
                "upper_bounds[%d] must be finite; only the last bound may be infinite"
                % index
            )
        if previous_bound is not None and bound <= previous_bound:
            raise InvalidSampleError("upper_bounds must be strictly ascending")
        previous_bound = bound  # type: ignore[assignment]

    previous_count = 0
    for index, count in enumerate(counts):
        if not isinstance(count, int) or isinstance(count, bool):
            raise InvalidSampleError("cumulative_counts[%d] must be an int" % index)
        if count < 0:
            raise InvalidSampleError(
                "cumulative_counts[%d] must be non-negative" % index
            )
        if count < previous_count:
            raise InvalidSampleError(
                "cumulative_counts must be non-decreasing within a sample"
            )
        previous_count = count


# ---------------------------------------------------------------------------
# Pure settlement logic
# ---------------------------------------------------------------------------


def compute_increments(
    baseline: ProcessSample, sample: ProcessSample
) -> Settlement:
    """Settle two valid samples without touching any converter state.

    Only upper bounds appearing in *both* samples are used.  Raises
    :class:`NegativeIncrementError` if any settled interval has a
    negative number of new requests; in that case the caller's baseline
    is left untouched.
    """
    baseline_counts = dict(zip(baseline.upper_bounds, baseline.cumulative_counts))
    sample_counts = dict(zip(sample.upper_bounds, sample.cumulative_counts))

    # Filtering the new sample's ascending bounds keeps the shared
    # bounds ordered and ascending.
    shared_bounds = tuple(
        bound for bound in sample.upper_bounds if bound in baseline_counts
    )

    increments = []
    lower_bound = 0.0
    previous_cumulative_delta = 0

    for upper_bound in shared_bounds:
        cumulative_delta = (
            sample_counts[upper_bound] - baseline_counts[upper_bound]
        )
        interval_increment = cumulative_delta - previous_cumulative_delta
        if interval_increment < 0:
            raise NegativeIncrementError(
                process_id=sample.process_id,
                baseline_timestamp=baseline.timestamp,
                sample_timestamp=sample.timestamp,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                interval_increment=interval_increment,
            )
        increments.append(
            BucketIncrement(
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                new_requests=interval_increment,
            )
        )
        lower_bound = upper_bound
        previous_cumulative_delta = cumulative_delta

    return Settlement(
        process_id=sample.process_id,
        process_start=sample.process_start,
        baseline_timestamp=baseline.timestamp,
        sample_timestamp=sample.timestamp,
        increments=tuple(increments),
        common_upper_bounds=shared_bounds,
        boundaries_changed=(
            set(baseline.upper_bounds) != set(sample.upper_bounds)
        ),
    )


# ---------------------------------------------------------------------------
# Stateful converter
# ---------------------------------------------------------------------------


class HistogramDeltaConverter:
    """Maintains one baseline per process and converts incoming samples."""

    def __init__(self) -> None:
        self._baselines: Dict[str, ProcessSample] = {}

    def __len__(self) -> int:
        return len(self._baselines)

    def tracked_processes(self) -> Tuple[str, ...]:
        """Process IDs whose baseline is currently retained."""
        return tuple(sorted(self._baselines))

    def baseline_for(self, process_id: str) -> Optional[ProcessSample]:
        """Return the retained baseline for *process_id*, if any."""
        return self._baselines.get(process_id)

    def submit(self, sample: ProcessSample) -> ConversionResult:
        """Process one sample for its process.

        Returns:
            * :class:`BaselineEstablished` for the first sample of a
              process (or the first sample after a process restart);
            * :class:`Retransmission` for an identical same-timestamp
              retransmission;
            * :class:`Settlement` with the per-interval new-request
              counts otherwise.

        Raises:
            InvalidSampleError: the sample structure is invalid.
            OutOfOrderTimestampError: timestamp older than the baseline.
            ConflictingRetransmissionError: same timestamp as the
                baseline but different bounds or counts.
            NegativeIncrementError: an interval increment is negative;
                the retained baseline is not modified.
        """
        if not isinstance(sample, ProcessSample):
            raise InvalidSampleError(
                "submit() expects a ProcessSample; build one with ProcessSample.build()"
            )
        # Re-validate defensively so converter state can never hold bad data.
        _validate_sample(
            sample.process_id,
            sample.process_start,
            sample.timestamp,
            sample.upper_bounds,
            sample.cumulative_counts,
        )

        baseline = self._baselines.get(sample.process_id)

        # Unknown process, or the same ID with a new process-start
        # identifier: the first sample only establishes a baseline.
        if baseline is None or baseline.process_start != sample.process_start:
            self._baselines[sample.process_id] = sample
            return BaselineEstablished(
                process_id=sample.process_id,
                process_start=sample.process_start,
                timestamp=sample.timestamp,
            )

        if sample.timestamp < baseline.timestamp:
            raise OutOfOrderTimestampError(
                sample.process_id, sample.timestamp, baseline.timestamp
            )

        if sample.timestamp == baseline.timestamp:
            if (
                sample.upper_bounds == baseline.upper_bounds
                and sample.cumulative_counts == baseline.cumulative_counts
            ):
                return Retransmission(sample.process_id, sample.timestamp)
            raise ConflictingRetransmissionError(
                sample.process_id, sample.timestamp
            )

        # May raise NegativeIncrementError; state is only updated after
        # settlement succeeds, so a rejected sample keeps the old baseline.
        settlement = compute_increments(baseline, sample)
        self._baselines[sample.process_id] = sample
        return settlement
