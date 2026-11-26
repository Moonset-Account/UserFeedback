"""Core reconciliation and window analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

from .clock import ClockSpec, ManualClock, TimeBounds, _finite_number
from .ingest import IngestionResult, ingest_records
from .model import (
    ARRIVAL,
    CAPACITY_LOST,
    COMPLETED,
    QUEUE_EXHAUSTED,
    RATE_LIMITED,
    REISSUED,
    WITHDRAWN,
    Event,
    Issue,
)
from .report import (
    AttributionEntry,
    CohortSummary,
    Incident,
    MetricEvidence,
    QueueInterval,
    ReissueAttribution,
    StageSpan,
    AnalysisResult,
    WindowReport,
)
from .windows import MetricBounds, WindowGrid

_METRIC_NAMES = {
    ARRIVAL: "arrivals",
    RATE_LIMITED: "rate_limited",
    REISSUED: "reissues",
    COMPLETED: "completions",
    WITHDRAWN: "withdrawals",
    CAPACITY_LOST: "capacity_lost",
    QUEUE_EXHAUSTED: "queue_exhausted",
}
_METRIC_KEYS = tuple(_METRIC_NAMES.values()) + ("admission_gaps",)

_TERMINAL_KINDS = {COMPLETED, WITHDRAWN, CAPACITY_LOST}


def _display_id(event: Event) -> str:
    if event.event_id:
        return event.event_id
    return f"payload:{event.payload_fingerprint()[:12]}"


def _coerce_clock_spec(value: Any) -> ClockSpec:
    if isinstance(value, ClockSpec):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ClockSpec(error_bound=float(value))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return ClockSpec(offset=float(value[0]), error_bound=float(value[1]))
    if isinstance(value, Mapping):
        return ClockSpec(
            offset=float(value.get("offset", 0.0)),
            error_bound=float(value.get("error_bound", value.get("error", 0.0))),
        )
    raise ValueError(f"invalid clock bound specification: {value!r}")


@dataclass
class _CohortData:
    key: str
    arrivals: list[Event]
    rate_limits: list[Event]
    reissues: list[Event]
    completions: list[Event]
    withdrawals: list[Event]
    losses: list[Event]

    def __init__(self, key: str) -> None:
        self.key = key
        self.arrivals = []
        self.rate_limits = []
        self.reissues = []
        self.completions = []
        self.withdrawals = []
        self.losses = []

    def by_kind(self, kind: str) -> list[Event]:
        return {
            ARRIVAL: self.arrivals,
            RATE_LIMITED: self.rate_limits,
            REISSUED: self.reissues,
            COMPLETED: self.completions,
            WITHDRAWN: self.withdrawals,
            CAPACITY_LOST: self.losses,
        }.get(kind, [])


@dataclass
class _Reconciliation:
    suppressed_uids: set[int]
    disputed_uids: set[int]
    canonical_arrival_by_request: dict[str, Event]
    canonical_terminal_by_request: dict[str, Event]
    conflict_requests: set[str]
    issues: list[Issue]


class CapacityAnalyzer:
    """Stateful thin wrapper around :func:`analyze`."""

    def __init__(
        self,
        *,
        window_width: float = 10.0,
        window_origin: float = 0.0,
        clock: ManualClock | None = None,
        default_clock_bounds: ClockSpec | Any = None,
        clock_bounds: Mapping[str, Any] | None = None,
        exhaustion_at: float | None = None,
        horizon: float | None = None,
        initial_backlog: int = 0,
    ) -> None:
        self.window_width = window_width
        self.window_origin = window_origin
        self.clock = clock or ManualClock(0.0)
        self.default_clock_bounds = default_clock_bounds
        self.clock_bounds = clock_bounds or {}
        self.exhaustion_at = exhaustion_at
        self.horizon = horizon
        self.initial_backlog = initial_backlog

    def analyze(self, records: Any, **overrides: Any) -> AnalysisResult:
        params = dict(
            window_width=self.window_width,
            window_origin=self.window_origin,
            clock=self.clock,
            default_clock_bounds=self.default_clock_bounds,
            clock_bounds=self.clock_bounds,
            exhaustion_at=self.exhaustion_at,
            horizon=self.horizon,
            initial_backlog=self.initial_backlog,
        )
        params.update(overrides)
        return analyze(records, **params)


def analyze(
    records: Any,
    *,
    window_width: float = 10.0,
    window_size: float | None = None,
    window_origin: float = 0.0,
    clock: ManualClock | None = None,
    default_clock_bounds: ClockSpec | Any = None,
    clock_bounds: Mapping[str, Any] | None = None,
    exhaustion_at: float | None = None,
    settle_at: float | None = None,
    horizon: float | None = None,
    initial_backlog: int = 0,
    detect_sequence_gaps: bool = True,
    assume_seq_starts_at_one: bool = False,
) -> AnalysisResult:
    """Analyze events and produce explainable windows, intervals, and incidents.

    ``exhaustion_at`` (alias ``settle_at``) closes unresolved queue intervals.
    Unresolved accepted arrivals at that boundary are located as inferred
    capacity loss.  Without it, unresolved work remains ``open``.
    """

    if window_size is not None:
        window_width = window_size
    width = _finite_number(window_width, "window_width")
    origin = _finite_number(window_origin, "window_origin")
    if width <= 0:
        raise ValueError("window_width must be positive")
    if exhaustion_at is None:
        exhaustion_at = settle_at
    if exhaustion_at is not None:
        exhaustion_at = _finite_number(exhaustion_at, "exhaustion_at")
    if horizon is None and clock is not None:
        horizon = clock.now
    if horizon is not None:
        horizon = _finite_number(horizon, "horizon")
    if isinstance(initial_backlog, bool) or not isinstance(initial_backlog, int) or initial_backlog < 0:
        raise ValueError("initial_backlog must be a non-negative integer")

    default_spec = (
        _coerce_clock_spec(default_clock_bounds)
        if default_clock_bounds is not None
        else ClockSpec()
    )
    client_specs = {
        client: _coerce_clock_spec(spec) for client, spec in (clock_bounds or {}).items()
    }

    ingestion = ingest_records(
        records,
        detect_sequence_gaps=detect_sequence_gaps,
        assume_seq_starts_at_one=assume_seq_starts_at_one,
    )
    issues = list(ingestion.issues)
    grid = WindowGrid(width=width, origin=origin)

    times: dict[int, TimeBounds] = {}
    for event in ingestion.events:
        client_spec = client_specs.get(event.client_id, default_spec)
        spec = event.clock_spec(client_spec)
        bounds = spec.correct(event.timestamp)
        times[id(event)] = bounds
        if event.true_timestamp is not None and not (
            bounds.lower <= event.true_timestamp <= bounds.upper
        ):
            issues.append(
                Issue(
                    code="clock_bound_violation",
                    message=(
                        f"true timestamp {event.true_timestamp} is outside corrected bounds "
                        f"[{bounds.lower}, {bounds.upper}] for {_display_id(event)}"
                    ),
                    event_id=event.event_id,
                    client_id=event.client_id,
                    details={
                        "true_timestamp": event.true_timestamp,
                        "lower": bounds.lower,
                        "upper": bounds.upper,
                    },
                )
            )

    exhaustion_event: Event | None = None
    if exhaustion_at is None:
        exhaustion_reports = [
            event for event in ingestion.events if event.kind == QUEUE_EXHAUSTED
        ]
        if exhaustion_reports:
            exhaustion_event = max(
                exhaustion_reports, key=lambda e: times[id(e)].point
            )
            exhaustion_at = times[id(exhaustion_event)].point
            if len(exhaustion_reports) > 1:
                issues.append(
                    Issue(
                        code="multiple_queue_exhausted_reports",
                        message=(
                            "multiple queue_exhausted reports found; using the latest "
                            "corrected point time as the settlement boundary"
                        ),
                        event_id=exhaustion_event.event_id,
                        details={
                            "exhaustion_at": exhaustion_at,
                            "report_count": len(exhaustion_reports),
                        },
                    )
                )

    reconciliation = _reconcile_request_reports(ingestion.events, times)
    issues.extend(reconciliation.issues)

    active_events = [
        event
        for event in ingestion.events
        if id(event) not in reconciliation.suppressed_uids
    ]

    # Resolve lineage/cohort identity.
    lineage_aliases: dict[str, str] = {}
    cohort_keys: dict[int, str] = {}
    cohorts_data: dict[str, _CohortData] = {}

    for event in active_events:
        if event.kind != ARRIVAL:
            continue
        key = (
            event.lineage_id
            or event.event_id
            or event.request_id
            or event.batch_id
            or f"arrival:{event.payload_fingerprint()[:12]}"
        )
        cohorts_data.setdefault(key, _CohortData(key))
        for alias in (
            event.lineage_id,
            event.event_id,
            event.request_id,
            event.batch_id,
        ):
            if alias:
                lineage_aliases[alias] = key

    for event in active_events:
        key = _lineage_key(event, lineage_aliases)
        cohort_keys[id(event)] = key
        cohorts_data.setdefault(key, _CohortData(key))

    for event in active_events:
        key = cohort_keys[id(event)]
        cohorts_data[key].by_kind(event.kind).append(event)
        if event.kind == REISSUED and not cohorts_data[key].arrivals:
            issues.append(
                Issue(
                    code="orphan_reissue",
                    message=(
                        f"reissue {_display_id(event)} cannot be matched to an original arrival; "
                        "it is counted as physical recovery traffic but not new unique demand"
                    ),
                    event_id=event.event_id,
                    client_id=event.client_id,
                    details={"lineage_id": key, "batch_id": event.batch_id},
                )
            )

    # Determine contiguous window range, including uncertain possible windows.
    index_set: set[int] = set()
    for event in active_events:
        assignment = grid.assignment(times[id(event)])
        index_set.update(assignment.possible_indices)
    if exhaustion_at is not None:
        index_set.add(grid.index_at(exhaustion_at))
    if horizon is not None:
        index_set.add(grid.index_at(horizon))
    if not index_set:
        windows: list[WindowReport] = []
        windows_by_index: dict[int, WindowReport] = {}
    else:
        # Always include grid window zero (the window starting at
        # ``window_origin``), even if it is an empty pre-event window.
        first_index = min(0, min(index_set))
        last_index = max(index_set)
        windows_by_index = {}
        windows = []
        for index in range(first_index, last_index + 1):
            window = grid.window(index)
            counts = {metric: MetricBounds.zero() for metric in _METRIC_KEYS}
            evidence = {metric: MetricEvidence() for metric in _METRIC_KEYS}
            report = WindowReport(
                index=index,
                start=window.start,
                end=window.end,
                width=width,
                counts=counts,
                rates={},
                evidence=evidence,
            )
            windows_by_index[index] = report
            windows.append(report)

    # Accumulate observed de-duplicated events.
    for event in active_events:
        metric = _METRIC_NAMES.get(event.kind)
        if metric is None:
            continue
        assignment = grid.assignment(times[id(event)])
        display = _display_id(event)
        disputed = id(event) in reconciliation.disputed_uids
        for index in assignment.possible_indices:
            report = windows_by_index.get(index)
            if report is None:
                continue
            ev = report.evidence[metric]
            if display not in ev.possible_only_event_ids:
                ev.possible_only_event_ids.append(display)
            if not disputed and assignment.certain_index == index:
                report.counts[metric] = report.counts[metric].add_certain(event.count)
                if display not in ev.certain_event_ids:
                    ev.certain_event_ids.append(display)
                if display not in ev.point_event_ids:
                    ev.point_event_ids.append(display)
            elif not disputed and assignment.point_index == index:
                report.counts[metric] = report.counts[metric].add_point_only(event.count)
                if display not in ev.point_event_ids:
                    ev.point_event_ids.append(display)
            else:
                # Uncertain event: upper bound in every possible window.
                report.counts[metric] = report.counts[metric].add_possible_only(event.count)
                if not disputed and assignment.point_index == index:
                    if display not in ev.point_event_ids:
                        ev.point_event_ids.append(display)

    # Event-level incidents.
    withdrawals: list[Incident] = []
    capacity_losses: list[Incident] = []
    admission_gaps: list[Incident] = []
    for event in active_events:
        if event.kind == WITHDRAWN:
            withdrawals.append(
                _withdrawal_incident(event, times[id(event)], grid, cohort_keys.get(id(event)))
            )
        elif event.kind == CAPACITY_LOST:
            capacity_losses.append(
                _explicit_loss_incident(
                    event, times[id(event)], grid, cohort_keys.get(id(event))
                )
            )

    # Request-level intervals and inferred request losses/admission gaps.
    request_events: dict[str, list[Event]] = defaultdict(list)
    for event in active_events:
        for request_id in event.subject_ids():
            request_events[request_id].append(event)

    intervals: list[QueueInterval] = []
    inferred_loss_requests_by_cohort: dict[str, list[str]] = defaultdict(list)
    named_open_by_cohort: dict[str, set[str]] = defaultdict(set)
    named_admission_gap_by_cohort: dict[str, set[str]] = defaultdict(set)
    request_arrival_bounds: dict[str, TimeBounds] = {}

    for request_id in sorted(request_events):
        evs = request_events[request_id]
        interval, inferred_incident = _build_request_interval(
            request_id=request_id,
            events=evs,
            times=times,
            grid=grid,
            reconciliation=reconciliation,
            cohort_keys=cohort_keys,
            exhaustion_at=exhaustion_at,
            horizon=horizon,
        )
        intervals.append(interval)
        arrival_event = reconciliation.canonical_arrival_by_request.get(request_id)
        if arrival_event is not None:
            request_arrival_bounds[request_id] = times[id(arrival_event)]
            cohort_key = cohort_keys.get(id(arrival_event))
            if cohort_key:
                terminal = reconciliation.canonical_terminal_by_request.get(request_id)
                has_completion = any(e.kind == COMPLETED for e in evs if id(e) not in reconciliation.suppressed_uids)
                has_noncompletion_terminal = terminal is not None and terminal.kind in {
                    WITHDRAWN,
                    CAPACITY_LOST,
                }
                if not has_completion and not has_noncompletion_terminal:
                    named_open_by_cohort[cohort_key].add(request_id)
        if inferred_incident is not None:
            cohort_key = inferred_incident.lineage_id
            if inferred_incident.category == "admission_gap":
                admission_gaps.append(inferred_incident)
                metric = "admission_gaps"
                if cohort_key:
                    named_admission_gap_by_cohort[cohort_key].add(request_id)
            else:
                capacity_losses.append(inferred_incident)
                metric = CAPACITY_LOST
                if cohort_key:
                    inferred_loss_requests_by_cohort[cohort_key].append(request_id)
            if cohort_key:
                _add_incident_count_to_window(
                    windows_by_index, grid, inferred_incident, metric
                )

    # Cohort summaries and aggregate inferred losses for unnamed batches.
    cohort_summaries: dict[str, CohortSummary] = {}
    for key in sorted(cohorts_data):
        data = cohorts_data[key]
        summary = _build_cohort_summary(
            key,
            data,
            times=times,
            reconciliation=reconciliation,
        )
        cohort_summaries[key] = summary
        if summary.missing_arrival_count:
            issues.append(
                Issue(
                    code="missing_arrival_evidence",
                    message=(
                        f"lineage {key} has {summary.missing_arrival_count} more terminal "
                        "outcomes than known original arrivals"
                    ),
                    details={
                        "lineage_id": key,
                        "missing_count": summary.missing_arrival_count,
                    },
                )
            )

        cohort_interval = _build_cohort_interval(
            data=data,
            summary=summary,
            times=times,
            grid=grid,
            exhaustion_at=exhaustion_at,
            horizon=horizon,
        )
        if cohort_interval is not None:
            intervals.append(cohort_interval)

        named_open_count = len(named_open_by_cohort.get(key, set()))
        unnamed_open = max(0, summary.open_count - named_open_count)

        # Evidence-based split: throttled copies that were never followed by a
        # reissue are admission/backlog gaps, not confidently queued capacity
        # loss.  Reissued or unthrottled unresolved mass remains capacity loss.
        estimated_admission_total = min(
            summary.open_count,
            max(0, summary.rate_limited_count - summary.reissue_attempt_count),
        )
        named_admission_count = len(named_admission_gap_by_cohort.get(key, set()))
        unnamed_admission = max(
            0,
            min(unnamed_open, estimated_admission_total - named_admission_count),
        )
        unnamed_loss_count = unnamed_open - unnamed_admission

        if exhaustion_at is not None and unnamed_admission > 0:
            if data.rate_limits:
                last_limit = max(data.rate_limits, key=lambda e: times[id(e)].point)
                gap_bounds = times[id(last_limit)]
                detected_by = "rate_limit_balance"
            else:
                gap_bounds = TimeBounds(exhaustion_at, exhaustion_at, exhaustion_at)
                detected_by = "cohort_mass_balance"
            incident = Incident(
                incident_id=f"admission-gap:cohort:{key}:unrecovered",
                category="admission_gap",
                title="限速后未观察到补发或终态",
                count=unnamed_admission,
                time_bounds=gap_bounds,
                window=grid.assignment(gap_bounds),
                lineage_id=key,
                detected_by=detected_by,
                evidence_event_ids=[
                    _display_id(e) for e in data.arrivals + data.rate_limits
                ],
                explanation=(
                    f"lineage {key} 有限速 {summary.rate_limited_count} 件、"
                    f"补发尝试 {summary.reissue_attempt_count} 件；按余额将 "
                    f"{unnamed_admission} 个未闭合名额定位为限速后未追偿的接入/积压缺口，"
                    "不计作服务完成，也不默认等同于服务端容量丢失。"
                ),
                counts_as_admission_gap=True,
            )
            admission_gaps.append(incident)
            _add_incident_count_to_window(
                windows_by_index, grid, incident, "admission_gaps"
            )

        if exhaustion_at is not None and unnamed_loss_count > 0:
            loss_bounds = TimeBounds(exhaustion_at, exhaustion_at, exhaustion_at)
            assignment = grid.assignment(loss_bounds)
            evidence_ids = [_display_id(e) for e in data.arrivals]
            incident = Incident(
                incident_id=f"capacity-loss:cohort:{key}:unresolved",
                category="capacity_loss",
                title="队列/追偿耗尽后仍无完成或主动撤回证据（批量余额推断）",
                count=unnamed_loss_count,
                time_bounds=loss_bounds,
                window=assignment,
                lineage_id=key,
                detected_by="cohort_mass_balance",
                evidence_event_ids=evidence_ids,
                explanation=(
                    f"lineage {key} 有 {summary.arrival_count} 个原始到达，"
                    f"完成 {summary.completed_count}、撤回 {summary.withdrawn_count}、"
                    f"显式丢失 {summary.explicit_lost_count}；扣除限速未追偿缺口后，剩余 "
                    f"{unnamed_loss_count} 个在 exhaustion_at={exhaustion_at} "
                    "仍无终态证据，定位为推断容量丢失，而不是主动撤回。"
                ),
                counts_as_capacity_loss=True,
            )
            capacity_losses.append(incident)
            _add_incident_count_to_window(windows_by_index, grid, incident, CAPACITY_LOST)

    # Missing-report upper ceiling for known tracked arrivals.
    for report in windows:
        ceiling = 0
        potential_ids: list[str] = []

        for request_id, arrival_bounds in request_arrival_bounds.items():
            if _request_can_have_missing_completion(
                request_id=request_id,
                events=request_events[request_id],
                reconciliation=reconciliation,
                arrival_bounds=arrival_bounds,
                window=report,
                exhaustion_at=exhaustion_at,
                horizon=horizon,
            ):
                ceiling += 1
                potential_ids.append(request_id)

        for key, summary in cohort_summaries.items():
            named_open = named_open_by_cohort.get(key, set())
            unnamed_open = max(0, summary.open_count - len(named_open))
            if unnamed_open <= 0 or not cohorts_data[key].arrivals:
                continue
            earliest_lower = min(times[id(e)].lower for e in cohorts_data[key].arrivals)
            if _time_can_fit_before_window_end(
                earliest_lower, report.end, exhaustion_at, horizon, report.start
            ):
                ceiling += unnamed_open

        ceiling += initial_backlog
        report.missing_report_ceiling = ceiling
        report.potential_missing_completion_ids = potential_ids

    # Finalize rates.
    for report in windows:
        report.rates = {
            metric: bounds.scale(1.0 / report.width)
            for metric, bounds in report.counts.items()
        }

    # Reissue attribution trails.
    reissue_attributions: list[ReissueAttribution] = []
    for event in ingestion.events:
        if event.kind != REISSUED:
            continue
        key = cohort_keys.get(id(event)) or _lineage_key(event, lineage_aliases)
        attribution = _build_reissue_attribution(
            event=event,
            key=key,
            cohort=cohorts_data.get(key),
            times=times,
            grid=grid,
        )
        reissue_attributions.append(attribution)

    cross_window = [r for r in reissue_attributions if r.crosses_point_window]

    total_duplicates = len(ingestion.duplicates)
    total_gaps = len(ingestion.gaps)
    inferred_loss_count = sum(
        incident.count
        for incident in capacity_losses
        if incident.detected_by != "event"
    )
    summary = {
        "event_reports_received": len(records) if hasattr(records, "__len__") else None,
        "unique_events": len(ingestion.events),
        "duplicate_event_reports": total_duplicates,
        "sequence_gap_count": total_gaps,
        "issue_count": len(issues),
        "unique_original_arrivals": sum(c.arrival_count for c in cohort_summaries.values()),
        "physical_reissue_count": sum(
            e.count for e in ingestion.events if e.kind == REISSUED
        ),
        "rate_limited_count": sum(
            e.count for e in ingestion.events if e.kind == RATE_LIMITED
        ),
        "completed_count": sum(c.completed_count for c in cohort_summaries.values()),
        "withdrawn_count": sum(c.withdrawn_count for c in cohort_summaries.values()),
        "explicit_capacity_lost_count": sum(c.explicit_lost_count for c in cohort_summaries.values()),
        "inferred_capacity_lost_count": inferred_loss_count,
        "capacity_lost_count": sum(i.count for i in capacity_losses),
        "active_withdrawal_count": sum(i.count for i in withdrawals),
        "admission_gap_count": sum(i.count for i in admission_gaps),
        "open_count": sum(c.open_count for c in cohort_summaries.values()),
        "missing_arrival_count": sum(c.missing_arrival_count for c in cohort_summaries.values()),
        "reissue_attribution_count": len(reissue_attributions),
        "cross_window_reissue_count": len(cross_window),
        "window_count": len(windows),
        "initial_backlog": initial_backlog,
        "capacity_rule": (
            "完成按完成窗口计量；原始到达按真实到达窗口计量为唯一需求；"
            "补发按补发窗口计量为物理追偿发送但不是新需求；主动撤回单独定位，"
            "不计为完成容量；限速后未补发的余额单列为接入/追偿缺口；"
            "其余耗尽边界后仍无完成/撤回证据的已知到达推断为容量丢失。"
        ),
    }

    return AnalysisResult(
        window_width=width,
        window_origin=origin,
        windows=windows,
        intervals=intervals,
        withdrawals=withdrawals,
        capacity_losses=capacity_losses,
        admission_gaps=admission_gaps,
        reissue_attributions=reissue_attributions,
        cross_window_reissues=cross_window,
        cohorts=cohort_summaries,
        duplicates=ingestion.duplicates,
        sequence_gaps=ingestion.gaps,
        issues=issues,
        summary=summary,
        exhaustion_at=exhaustion_at,
        horizon=horizon,
    )


def _lineage_key(event: Event, aliases: Mapping[str, str]) -> str:
    if event.lineage_id:
        return event.lineage_id
    for alias in (
        event.source_arrival_id,
        event.request_id,
        event.batch_id,
        event.event_id,
    ):
        if alias and alias in aliases:
            return aliases[alias]
    if event.source_arrival_id:
        return event.source_arrival_id
    if event.request_id:
        return event.request_id
    if event.batch_id:
        return event.batch_id
    return f"uncorrelated:{event.payload_fingerprint()[:12]}"


def _reconcile_request_reports(
    events: list[Event], times: Mapping[int, TimeBounds]
) -> _Reconciliation:
    suppressed: set[int] = set()
    disputed: set[int] = set()
    canonical_arrivals: dict[str, Event] = {}
    canonical_terminals: dict[str, Event] = {}
    conflicts: set[str] = set()
    issues: list[Issue] = []

    by_request: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        for request_id in event.subject_ids():
            by_request[request_id].append(event)

    for request_id, request_events in by_request.items():
        arrivals = [e for e in request_events if e.kind == ARRIVAL]
        if len(arrivals) > 1:
            canonical = min(arrivals, key=lambda e: (times[id(e)].point, _display_id(e)))
            canonical_arrivals[request_id] = canonical
            for event in arrivals:
                if event is canonical:
                    continue
                suppressed.add(id(event))
                issues.append(
                    Issue(
                        code="duplicate_arrival_report",
                        message=(
                            f"request {request_id} has multiple arrival reports; counting the "
                            "earliest corrected report once"
                        ),
                        event_id=event.event_id,
                        details={"request_id": request_id},
                    )
                )
        elif arrivals:
            canonical_arrivals[request_id] = arrivals[0]

        terminals = [e for e in request_events if e.kind in _TERMINAL_KINDS]
        by_kind: dict[str, list[Event]] = defaultdict(list)
        for event in terminals:
            by_kind[event.kind].append(event)

        for kind, group in by_kind.items():
            if len(group) == 1:
                continue
            canonical = min(group, key=lambda e: (times[id(e)].point, _display_id(e)))
            for event in group:
                if event is canonical:
                    continue
                suppressed.add(id(event))
                issues.append(
                    Issue(
                        code="duplicate_terminal_report",
                        message=(
                            f"request {request_id} has multiple {kind} reports; "
                            "counting one logical outcome"
                        ),
                        event_id=event.event_id,
                        details={"request_id": request_id, "kind": kind},
                    )
                )

        if len(by_kind) > 1:
            conflicts.add(request_id)
            all_terminals = terminals
            canonical = min(
                all_terminals, key=lambda e: (times[id(e)].point, _display_id(e))
            )
            canonical_terminals[request_id] = canonical
            for event in all_terminals:
                disputed.add(id(event))
            issues.append(
                Issue(
                    code="terminal_conflict",
                    message=(
                        f"request {request_id} has conflicting terminal reports "
                        f"{sorted(by_kind)}; window upper bounds retain both possibilities"
                    ),
                    details={"request_id": request_id, "kinds": sorted(by_kind)},
                )
            )
        elif terminals:
            # After same-kind suppression there should be one, but choose first
            # deterministically if equivalent reports remain.
            canonical = min(terminals, key=lambda e: (times[id(e)].point, _display_id(e)))
            canonical_terminals[request_id] = canonical

    return _Reconciliation(
        suppressed_uids=suppressed,
        disputed_uids=disputed,
        canonical_arrival_by_request=canonical_arrivals,
        canonical_terminal_by_request=canonical_terminals,
        conflict_requests=conflicts,
        issues=issues,
    )


def _withdrawal_incident(
    event: Event,
    bounds: TimeBounds,
    grid: WindowGrid,
    lineage_id: str | None,
) -> Incident:
    ids = list(event.subject_ids())
    return Incident(
        incident_id=f"withdrawal:{_display_id(event)}",
        category="active_withdrawal",
        title="客户端主动撤回",
        count=event.count,
        time_bounds=bounds,
        window=grid.assignment(bounds),
        lineage_id=lineage_id,
        request_id=event.request_id,
        client_id=event.client_id,
        event_id=event.event_id,
        batch_id=event.batch_id,
        request_ids=ids,
        evidence_event_ids=[_display_id(event)],
        detected_by="event",
        counts_as_withdrawal=True,
        explanation=(
            "主动撤回由客户端发起，说明需求离开队列；它不是服务完成容量，"
            "默认也不是服务端容量丢失。"
        ),
    )


def _explicit_loss_incident(
    event: Event,
    bounds: TimeBounds,
    grid: WindowGrid,
    lineage_id: str | None,
) -> Incident:
    ids = list(event.subject_ids())
    return Incident(
        incident_id=f"capacity-loss:{_display_id(event)}",
        category="capacity_loss",
        title=event.reason or "显式容量丢失/丢弃",
        count=event.count,
        time_bounds=bounds,
        window=grid.assignment(bounds),
        lineage_id=lineage_id,
        request_id=event.request_id,
        client_id=event.client_id,
        event_id=event.event_id,
        batch_id=event.batch_id,
        request_ids=ids,
        evidence_event_ids=[_display_id(event)],
        detected_by="event",
        counts_as_capacity_loss=True,
        explanation=(
            "显式容量丢失事件表明已接纳/排队工作被服务端丢弃或在耗尽时未得到服务；"
            "它与客户端主动撤回分开定位。"
        ),
    )


def _build_request_interval(
    *,
    request_id: str,
    events: list[Event],
    times: Mapping[int, TimeBounds],
    grid: WindowGrid,
    reconciliation: _Reconciliation,
    cohort_keys: Mapping[int, str],
    exhaustion_at: float | None,
    horizon: float | None,
) -> tuple[QueueInterval, Incident | None]:
    arrival = reconciliation.canonical_arrival_by_request.get(request_id)
    terminal = reconciliation.canonical_terminal_by_request.get(request_id)
    conflict = request_id in reconciliation.conflict_requests

    reissues = [e for e in events if e.kind == REISSUED and id(e) not in reconciliation.suppressed_uids]
    rate_limits = [e for e in events if e.kind == RATE_LIMITED and id(e) not in reconciliation.suppressed_uids]
    evidence = [_display_id(e) for e in events if id(e) not in reconciliation.suppressed_uids]

    start_bounds = times[id(arrival)] if arrival is not None else None
    start_window = grid.assignment(start_bounds) if start_bounds else None

    inferred_incident: Incident | None = None
    end_bounds: TimeBounds | None = None
    end_window = None
    closed = False
    inferred = False
    status: str

    if terminal is not None and id(terminal) not in reconciliation.suppressed_uids:
        end_bounds = times[id(terminal)]
        end_window = grid.assignment(end_bounds)
        closed = True
        status = f"terminal_conflict:{terminal.kind}" if conflict else terminal.kind
        explanation = (
            f"请求 {request_id} 从真实到达至 {terminal.kind} 事件的可解释等待区间；"
            "存在冲突终态报告，窗口上下界保留两种可能。"
            if conflict
            else f"请求 {request_id} 从真实到达至 {terminal.kind} 的等待/排队区间。"
        )
    elif exhaustion_at is not None and arrival is not None:
        arrival_bounds = times[id(arrival)]
        if arrival_bounds.lower <= exhaustion_at:
            lineage = cohort_keys.get(id(arrival))
            if rate_limits and not reissues:
                last_limit = max(rate_limits, key=lambda e: times[id(e)].point)
                end_bounds = times[id(last_limit)]
                end_window = grid.assignment(end_bounds)
                closed = True
                inferred = False
                status = "rate_limited_unrecovered"
                inferred_incident = Incident(
                    incident_id=f"admission-gap:request:{request_id}",
                    category="admission_gap",
                    title="限速后未补发且无终态",
                    count=1,
                    time_bounds=end_bounds,
                    window=end_window,
                    lineage_id=lineage,
                    request_id=request_id,
                    client_id=arrival.client_id,
                    evidence_event_ids=[_display_id(arrival), *evidence],
                    detected_by="rate_limit_balance",
                    explanation=(
                        f"请求 {request_id} 有 {len(rate_limits)} 次限速证据但没有补发批次，"
                        "在耗尽边界前也没有完成/撤回；定位为限速后未追偿的接入/积压缺口，"
                        "不记作主动撤回，也不直接记作服务端容量丢失。"
                    ),
                    counts_as_admission_gap=True,
                )
                explanation = inferred_incident.explanation
            else:
                end_bounds = TimeBounds(exhaustion_at, exhaustion_at, exhaustion_at)
                end_window = grid.assignment(end_bounds)
                closed = True
                inferred = True
                status = "capacity_lost"
                inferred_incident = Incident(
                    incident_id=f"capacity-loss:request:{request_id}",
                    category="capacity_loss",
                    title="耗尽边界后仍无完成或撤回证据",
                    count=1,
                    time_bounds=end_bounds,
                    window=end_window,
                    lineage_id=lineage,
                    request_id=request_id,
                    client_id=arrival.client_id,
                    evidence_event_ids=[_display_id(arrival), *evidence],
                    detected_by="request_mass_balance",
                    explanation=(
                        f"请求 {request_id} 有原始到达证据，但到 exhaustion_at="
                        f"{exhaustion_at} 仍无完成或主动撤回终态；"
                        + (
                            "已有补发尝试，因此剩余未服务名额推断为容量丢失。"
                            if reissues
                            else "无限速后未追偿证据，推断为容量丢失。"
                        )
                    ),
                    counts_as_capacity_loss=True,
                )
                explanation = inferred_incident.explanation
        else:
            status = "arrives_after_exhaustion"
            explanation = f"请求 {request_id} 的到达可能晚于耗尽边界，未计入该次耗尽损失。"
    elif horizon is not None and arrival is not None:
        end_bounds = TimeBounds(horizon, horizon, horizon)
        end_window = grid.assignment(end_bounds)
        status = "open_observed_to_horizon"
        explanation = f"请求 {request_id} 在观察截止前仍无终态；区间仅表示已观察时长，不判定丢失。"
    elif arrival is not None:
        status = "open"
        explanation = f"请求 {request_id} 已有真实到达但缺少终态和截止边界，保持未闭合。"
    else:
        status = "missing_arrival_evidence"
        if terminal is not None:
            end_bounds = times[id(terminal)]
            end_window = grid.assignment(end_bounds)
            closed = True
        explanation = f"请求 {request_id} 有终态/事件但缺少可关联的原始到达，不能可靠计算完整排队时长。"

    if arrival is None:
        # A terminal event still exists above, but the interval cannot claim a
        # real queue start; keep the dedicated missing-arrival status.
        status = "missing_arrival_evidence"
        explanation = f"请求 {request_id} 有终态/事件但缺少可关联的原始到达，不能可靠计算完整排队时长。"

    duration = None
    if start_bounds is not None and end_bounds is not None:
        duration = start_bounds.duration_bounds_to(end_bounds)

    if terminal is not None:
        end_event_id: str | None = _display_id(terminal)
    elif status == "rate_limited_unrecovered" and rate_limits:
        end_event_id = _display_id(
            max(rate_limits, key=lambda e: times[id(e)].point)
        )
    elif inferred_incident is not None:
        end_event_id = f"exhaustion@{exhaustion_at}"
    else:
        end_event_id = None

    spans: list[StageSpan] = []
    if start_bounds is not None and end_bounds is not None:
        spans.append(
            StageSpan(
                name="waiting_or_queue",
                start_bounds=start_bounds,
                end_bounds=end_bounds,
                basis="original arrival to terminal/settlement boundary",
            )
        )
    for reissue in reissues:
        if start_bounds is not None:
            spans.append(
                StageSpan(
                    name="backlog_age_at_reissue",
                    start_bounds=start_bounds,
                    end_bounds=times[id(reissue)],
                    basis=f"original arrival to reissue {_display_id(reissue)}",
                )
            )

    interval = QueueInterval(
        interval_id=f"request:{request_id}",
        subject_type="request",
        status=status,
        count=1,
        start_bounds=start_bounds,
        end_bounds=end_bounds,
        duration_bounds=duration,
        start_window=start_window,
        end_window=end_window,
        closed=closed,
        inferred=inferred,
        explanation=explanation,
        lineage_id=cohort_keys.get(id(arrival)) if arrival else None,
        request_id=request_id,
        start_event_id=_display_id(arrival) if arrival else None,
        end_event_id=end_event_id,
        event_ids=evidence,
        reissue_event_ids=[_display_id(e) for e in reissues],
        rate_limit_event_ids=[_display_id(e) for e in rate_limits],
        spans=spans,
    )
    return interval, inferred_incident


def _effective_event_count(
    event: Event, reconciliation: _Reconciliation
) -> int:
    if id(event) in reconciliation.suppressed_uids:
        return 0
    if id(event) in reconciliation.disputed_uids and event.request_id:
        canonical = reconciliation.canonical_terminal_by_request.get(event.request_id)
        if canonical is not event:
            return 0
    return event.count


def _build_cohort_summary(
    key: str,
    data: _CohortData,
    *,
    times: Mapping[int, TimeBounds],
    reconciliation: _Reconciliation,
) -> CohortSummary:
    def count(events: list[Event]) -> int:
        return sum(_effective_event_count(e, reconciliation) for e in events)

    arrivals = count(data.arrivals)
    reissues = sum(e.count for e in data.reissues)
    rates = sum(e.count for e in data.rate_limits)
    completed = count(data.completions)
    withdrawn = count(data.withdrawals)
    lost = count(data.losses)
    terminal = completed + withdrawn + lost
    open_count = max(0, arrivals - terminal)
    missing_arrival = max(0, terminal - arrivals)

    if arrivals == 0 and terminal > 0:
        status = "missing_arrival_evidence"
    elif arrivals == 0:
        status = "non_arrival_events_only"
    elif open_count == 0:
        if completed == arrivals and withdrawn == 0 and lost == 0:
            status = "completed"
        elif withdrawn == arrivals and completed == 0 and lost == 0:
            status = "withdrawn"
        elif lost == arrivals and completed == 0 and withdrawn == 0:
            status = "capacity_lost"
        else:
            status = "settled_mixed"
    elif terminal == 0:
        status = "open"
    else:
        status = "partially_open"

    issue_codes: list[str] = []
    if missing_arrival:
        issue_codes.append("missing_arrival_evidence")
    if data.reissues and not data.arrivals:
        issue_codes.append("orphan_reissue")

    return CohortSummary(
        lineage_id=key,
        arrival_count=arrivals,
        reissue_attempt_count=reissues,
        rate_limited_count=rates,
        completed_count=completed,
        withdrawn_count=withdrawn,
        explicit_lost_count=lost,
        open_count=open_count,
        missing_arrival_count=missing_arrival,
        status=status,
        arrival_event_ids=[_display_id(e) for e in data.arrivals],
        reissue_event_ids=[_display_id(e) for e in data.reissues],
        completion_event_ids=[_display_id(e) for e in data.completions],
        withdrawal_event_ids=[_display_id(e) for e in data.withdrawals],
        loss_event_ids=[_display_id(e) for e in data.losses],
        issue_codes=issue_codes,
    )


def _build_cohort_interval(
    *,
    data: _CohortData,
    summary: CohortSummary,
    times: Mapping[int, TimeBounds],
    grid: WindowGrid,
    exhaustion_at: float | None,
    horizon: float | None,
) -> QueueInterval | None:
    if not data.arrivals:
        return None
    first_arrival = min(data.arrivals, key=lambda e: times[id(e)].point)
    start_bounds = times[id(first_arrival)]

    end_bounds: TimeBounds | None = None
    closed = False
    inferred = False
    status = summary.status
    explanation = (
        f"lineage {data.key} 的聚合排队区间：从最早原始到达起，按 mass balance "
        f"保留 {summary.open_count} 个未闭合名额。"
    )

    if summary.open_count == 0:
        terminal_events = data.completions + data.withdrawals + data.losses
        if terminal_events:
            last_terminal = max(terminal_events, key=lambda e: times[id(e)].point)
            end_bounds = times[id(last_terminal)]
            closed = True
    elif exhaustion_at is not None:
        end_bounds = TimeBounds(exhaustion_at, exhaustion_at, exhaustion_at)
        closed = True
        inferred = True
        status = (
            "capacity_lost"
            if summary.completed_count == 0 and summary.withdrawn_count == 0
            else "partially_served_with_capacity_loss"
        )
        explanation += (
            f" exhaustion_at={exhaustion_at} 后剩余名额推断为容量丢失；"
            "个体定位见 capacity_losses。"
        )
    elif horizon is not None:
        end_bounds = TimeBounds(horizon, horizon, horizon)
        explanation += " 到达观察截止但未到耗尽结算边界，保持 open。"

    duration = start_bounds.duration_bounds_to(end_bounds) if end_bounds else None
    return QueueInterval(
        interval_id=f"cohort:{data.key}",
        subject_type="cohort",
        status=status,
        count=summary.arrival_count,
        start_bounds=start_bounds,
        end_bounds=end_bounds,
        duration_bounds=duration,
        start_window=grid.assignment(start_bounds),
        end_window=grid.assignment(end_bounds) if end_bounds else None,
        closed=closed,
        inferred=inferred,
        explanation=explanation,
        lineage_id=data.key,
        start_event_id=_display_id(first_arrival),
        event_ids=[
            _display_id(e)
            for e in data.arrivals
            + data.rate_limits
            + data.reissues
            + data.completions
            + data.withdrawals
            + data.losses
        ],
        reissue_event_ids=summary.reissue_event_ids,
        rate_limit_event_ids=[_display_id(e) for e in data.rate_limits],
    )


def _add_incident_count_to_window(
    windows_by_index: Mapping[int, WindowReport],
    grid: WindowGrid,
    incident: Incident,
    metric: str,
) -> None:
    assignment = incident.window
    if assignment.certain_index is not None:
        report = windows_by_index.get(assignment.certain_index)
        if report:
            report.counts[metric] = report.counts[metric].add_certain(incident.count)
            eid = incident.incident_id
            report.evidence[metric].certain_event_ids.append(eid)
            report.evidence[metric].point_event_ids.append(eid)
            return
    for index in assignment.possible_indices:
        report = windows_by_index.get(index)
        if report:
            report.counts[metric] = report.counts[metric].add_possible_only(incident.count)
            report.evidence[metric].possible_only_event_ids.append(incident.incident_id)
            if index == assignment.point_index:
                report.evidence[metric].point_event_ids.append(incident.incident_id)


def _request_can_have_missing_completion(
    *,
    request_id: str,
    events: list[Event],
    reconciliation: _Reconciliation,
    arrival_bounds: TimeBounds,
    window: WindowReport,
    exhaustion_at: float | None,
    horizon: float | None,
) -> bool:
    # Any non-suppressed completion means the completion report is not missing.
    has_completion = any(
        e.kind == COMPLETED and id(e) not in reconciliation.suppressed_uids
        for e in events
    )
    if has_completion:
        return False
    has_withdrawal_or_loss = any(
        e.kind in {WITHDRAWN, CAPACITY_LOST}
        and id(e) not in reconciliation.suppressed_uids
        for e in events
    )
    if has_withdrawal_or_loss:
        return False
    cutoff = exhaustion_at if exhaustion_at is not None else horizon
    return _time_can_fit_before_window_end(
        arrival_bounds.lower, window.end, cutoff, horizon, window.start
    )


def _time_can_fit_before_window_end(
    earliest_start: float,
    window_end: float,
    exhaustion_at: float | None,
    horizon: float | None,
    window_start: float,
) -> bool:
    cutoff = exhaustion_at if exhaustion_at is not None else horizon
    effective_end = window_end if cutoff is None else min(window_end, cutoff)
    if cutoff is not None and window_start > cutoff:
        return False
    return earliest_start <= effective_end and earliest_start < window_end


def _build_reissue_attribution(
    *,
    event: Event,
    key: str,
    cohort: _CohortData | None,
    times: Mapping[int, TimeBounds],
    grid: WindowGrid,
) -> ReissueAttribution:
    reissue_assignment = grid.assignment(times[id(event)])
    original_event = None
    original_index: int | None = None
    original_certain: int | None = None
    entries: list[AttributionEntry] = []
    rate_ids: list[str] = []
    completion_ids: list[str] = []

    if cohort and cohort.arrivals:
        original_event = min(cohort.arrivals, key=lambda e: times[id(e)].point)
        original_bounds = times[id(original_event)]
        original_assignment = grid.assignment(original_bounds)
        original_index = original_assignment.point_index
        original_certain = original_assignment.certain_index
        original_window = grid.window(original_index)
        entries.append(
            AttributionEntry(
                window_index=original_index,
                window_start=original_window.start,
                window_end=original_window.end,
                view="origin_demand",
                metric="arrivals",
                count=original_event.count,
                role="original_unique_demand_cohort_total",
                note=(
                    f"原始真实到达 {_display_id(original_event)} 已在此窗口记为唯一需求；"
                    f"本次补发批次 {_display_id(event)} 数量为 {event.count}，"
                    "该分录用于解释来源，不能再次加总为新到达。"
                ),
                lineage_id=key,
                event_id=_display_id(original_event),
            )
        )
        entries.append(
            AttributionEntry(
                window_index=original_index,
                window_start=original_window.start,
                window_end=original_window.end,
                view="origin_demand",
                metric="backlog_to_be_recovered",
                count=event.count,
                role="memo_not_additive_event_count",
                note="属于原始窗口形成、后来被追偿的积压额度。",
                lineage_id=key,
                event_id=_display_id(event),
            )
        )
        rate_ids = [_display_id(e) for e in cohort.rate_limits]
        completion_ids = [_display_id(e) for e in cohort.completions]
        for completion in cohort.completions:
            assignment = grid.assignment(times[id(completion)])
            idx = assignment.point_index
            w = grid.window(idx)
            entries.append(
                AttributionEntry(
                    window_index=idx,
                    window_start=w.start,
                    window_end=w.end,
                    view="completion",
                    metric="completions",
                    count=completion.count,
                    role="service_throughput_in_completion_window",
                    note="无论请求来自哪个原始窗口，完成容量都在实际完成窗口计量。",
                    lineage_id=key,
                    event_id=_display_id(completion),
                )
            )
    else:
        # Still provide a source-window placeholder only if source id can be
        # interpreted as a timestamp? No: explicitly leave unknown.
        pass

    reissue_window = grid.window(reissue_assignment.point_index)
    entries.append(
        AttributionEntry(
            window_index=reissue_assignment.point_index,
            window_start=reissue_window.start,
            window_end=reissue_window.end,
            view="physical_send",
            metric="reissues",
            count=event.count,
            role="recovered_backlog_send",
            note="补发/追偿批次计入本窗口的物理发送压力，但不是新增唯一客户端需求。",
            lineage_id=key,
            event_id=_display_id(event),
        )
    )
    entries.append(
        AttributionEntry(
            window_index=reissue_assignment.point_index,
            window_start=reissue_window.start,
            window_end=reissue_window.end,
            view="origin_demand",
            metric="new_unique_demand",
            count=0,
            role="no_double_count",
            note="若把补发误记为新到达，会把同一批需求重复计入容量估计。",
            lineage_id=key,
            event_id=_display_id(event),
        )
    )

    rule = (
        "原始窗口：按原始 arrival 记新增唯一需求和积压；"
        "补发窗口：按 reissued 记物理追偿发送/尝试及该窗口完成吞吐，"
        "但 new_unique_demand=0；不得把补发数量再加到 arrivals。"
    )
    return ReissueAttribution(
        reissue_event_id=_display_id(event),
        lineage_id=key,
        batch_id=event.batch_id,
        count=event.count,
        original_window_index=original_index,
        original_window_certain=original_certain,
        reissue_window=reissue_assignment,
        entries=entries,
        rule=rule,
        naive_new_arrival_overcount=event.count,
        rate_limit_event_ids=rate_ids,
        completion_event_ids=completion_ids,
    )
