"""Typed analysis outputs and JSON-friendly serialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .clock import MetricRange, TimeBounds
from .ingest import DuplicateReport, SequenceGap
from .model import Issue
from .windows import MetricBounds, WindowAssignment


@dataclass
class StageSpan:
    name: str
    start_bounds: TimeBounds
    end_bounds: TimeBounds | float
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueueInterval:
    """An explainable waiting/queue interval for one request or cohort."""

    interval_id: str
    subject_type: str  # "request" or "cohort"
    status: str
    count: int
    start_bounds: TimeBounds | None
    end_bounds: TimeBounds | None
    duration_bounds: MetricRange | None
    start_window: WindowAssignment | None
    end_window: WindowAssignment | None
    closed: bool
    inferred: bool
    explanation: str
    lineage_id: str | None = None
    request_id: str | None = None
    start_event_id: str | None = None
    end_event_id: str | None = None
    event_ids: list[str] = field(default_factory=list)
    reissue_event_ids: list[str] = field(default_factory=list)
    rate_limit_event_ids: list[str] = field(default_factory=list)
    spans: list[StageSpan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    """A located withdrawal, capacity loss, or explicit loss report."""

    incident_id: str
    category: str
    title: str
    count: int
    time_bounds: TimeBounds
    window: WindowAssignment
    explanation: str
    lineage_id: str | None = None
    request_id: str | None = None
    client_id: str | None = None
    event_id: str | None = None
    batch_id: str | None = None
    request_ids: list[str] = field(default_factory=list)
    evidence_event_ids: list[str] = field(default_factory=list)
    detected_by: str = "event"
    counts_as_withdrawal: bool = False
    counts_as_capacity_loss: bool = False
    counts_as_admission_gap: bool = False

    @property
    def point_window_index(self) -> int:
        return self.window.point_index

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["point_window_index"] = self.point_window_index
        return data


@dataclass
class AttributionEntry:
    window_index: int
    window_start: float
    window_end: float
    view: str  # origin_demand, physical_send, completion
    metric: str
    count: int
    role: str
    note: str
    lineage_id: str | None = None
    event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReissueAttribution:
    """Explains where an original arrival and its later batch are counted."""

    reissue_event_id: str | None
    lineage_id: str
    batch_id: str | None
    count: int
    original_window_index: int | None
    original_window_certain: int | None
    reissue_window: WindowAssignment
    entries: list[AttributionEntry]
    rule: str
    naive_new_arrival_overcount: int
    rate_limit_event_ids: list[str] = field(default_factory=list)
    completion_event_ids: list[str] = field(default_factory=list)

    @property
    def crosses_point_window(self) -> bool:
        return (
            self.original_window_index is not None
            and self.original_window_index != self.reissue_window.point_index
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CohortSummary:
    lineage_id: str
    arrival_count: int
    reissue_attempt_count: int
    rate_limited_count: int
    completed_count: int
    withdrawn_count: int
    explicit_lost_count: int
    open_count: int
    missing_arrival_count: int
    status: str
    arrival_event_ids: list[str] = field(default_factory=list)
    reissue_event_ids: list[str] = field(default_factory=list)
    completion_event_ids: list[str] = field(default_factory=list)
    withdrawal_event_ids: list[str] = field(default_factory=list)
    loss_event_ids: list[str] = field(default_factory=list)
    issue_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MetricEvidence:
    certain_event_ids: list[str] = field(default_factory=list)
    possible_only_event_ids: list[str] = field(default_factory=list)
    point_event_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WindowReport:
    index: int
    start: float
    end: float
    width: float
    counts: dict[str, MetricBounds]
    rates: dict[str, MetricBounds]
    evidence: dict[str, MetricEvidence] = field(default_factory=dict)
    missing_report_ceiling: int = 0
    potential_missing_completion_ids: list[str] = field(default_factory=list)

    def metric(self, name: str) -> MetricBounds:
        if name not in self.counts:
            return MetricBounds.zero()
        return self.counts[name]

    @property
    def arrivals(self) -> MetricBounds:
        return self.metric("arrivals")

    @property
    def reissues(self) -> MetricBounds:
        return self.metric("reissues")

    @property
    def rate_limited(self) -> MetricBounds:
        return self.metric("rate_limited")

    @property
    def completions(self) -> MetricBounds:
        return self.metric("completions")

    @property
    def withdrawals(self) -> MetricBounds:
        return self.metric("withdrawals")

    @property
    def capacity_lost(self) -> MetricBounds:
        return self.metric("capacity_lost")

    @property
    def admission_gaps(self) -> MetricBounds:
        return self.metric("admission_gaps")

    @property
    def queue_exhausted(self) -> MetricBounds:
        return self.metric("queue_exhausted")

    @property
    def reported_throughput(self) -> MetricBounds:
        """Completion/sec using observed, de-duplicated completion events."""
        return self.rates["completions"]

    @property
    def settled_throughput(self) -> MetricBounds:
        """Booked throughput bounds after duplicate/outcome reconciliation."""
        return self.reported_throughput

    @property
    def throughput_bounds(self) -> MetricBounds:
        return self.throughput

    @property
    def throughput(self) -> MetricBounds:
        """Conservative lower to tracked missing-report upper in completions/sec.

        The upper adds known unresolved work that could have arrived by this
        window end.  It is conditional on all original arrivals being known.
        """

        observed = self.counts["completions"]
        upper_count = observed.upper + self.missing_report_ceiling
        return MetricBounds(
            lower=observed.lower / self.width,
            point=observed.point / self.width,
            upper=upper_count / self.width,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "width": self.width,
            "counts": {k: v.to_dict() for k, v in self.counts.items()},
            "rates": {k: v.to_dict() for k, v in self.rates.items()},
            "reported_throughput_rps": self.reported_throughput.to_dict(),
            "settled_throughput_rps": self.settled_throughput.to_dict(),
            "throughput_rps_with_missing_report_ceiling": self.throughput.to_dict(),
            "missing_report_ceiling": self.missing_report_ceiling,
            "potential_missing_completion_ids": list(
                self.potential_missing_completion_ids
            ),
            "evidence": {k: v.to_dict() for k, v in self.evidence.items()},
        }


@dataclass
class AnalysisResult:
    window_width: float
    window_origin: float
    windows: list[WindowReport]
    intervals: list[QueueInterval]
    withdrawals: list[Incident]
    capacity_losses: list[Incident]
    admission_gaps: list[Incident]
    reissue_attributions: list[ReissueAttribution]
    cross_window_reissues: list[ReissueAttribution]
    cohorts: dict[str, CohortSummary]
    duplicates: list[DuplicateReport]
    sequence_gaps: list[SequenceGap]
    issues: list[Issue]
    summary: dict[str, Any]
    exhaustion_at: float | None = None
    horizon: float | None = None

    def window(self, index: int) -> WindowReport | None:
        for window in self.windows:
            if window.index == index:
                return window
        return None

    def window_at_time(self, timestamp: float) -> WindowReport | None:
        if not self.windows:
            return None
        first_start = self.windows[0].start
        width = self.windows[0].width
        index = int((timestamp - self.window_origin) // width)
        return self.window(index)

    @property
    def queue_intervals(self) -> list[QueueInterval]:
        """Descriptive alias for :attr:`intervals`."""
        return self.intervals

    @property
    def losses(self) -> list[Incident]:
        """Descriptive alias for :attr:`capacity_losses`."""
        return self.capacity_losses

    def throughput_table(self) -> list[dict[str, Any]]:
        return [
            {
                "window_index": window.index,
                "start": window.start,
                "end": window.end,
                "settled_rps": window.settled_throughput.to_dict(),
                "missing_report_upper_rps": window.throughput.to_dict(),
            }
            for window in self.windows
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_width": self.window_width,
            "window_origin": self.window_origin,
            "exhaustion_at": self.exhaustion_at,
            "horizon": self.horizon,
            "summary": self.summary,
            "windows": [w.to_dict() for w in self.windows],
            "intervals": [i.to_dict() for i in self.intervals],
            "withdrawals": [i.to_dict() for i in self.withdrawals],
            "capacity_losses": [i.to_dict() for i in self.capacity_losses],
            "admission_gaps": [i.to_dict() for i in self.admission_gaps],
            "reissue_attributions": [r.to_dict() for r in self.reissue_attributions],
            "cross_window_reissues": [r.to_dict() for r in self.cross_window_reissues],
            "cohorts": {k: v.to_dict() for k, v in self.cohorts.items()},
            "duplicates": [d.to_dict() for d in self.duplicates],
            "sequence_gaps": [g.to_dict() for g in self.sequence_gaps],
            "issues": [i.to_dict() for i in self.issues],
        }
