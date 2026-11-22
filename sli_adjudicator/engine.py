"""Windowed adjudication engine for the SLI release gate.

See ``README.md`` for the full contract.  The engine:

1. Assigns each user request to the fixed-size window that contains its
   *first entry* time, using the rule that was in force at that entry time.
2. Collapses every repeated sighting (edge / downstream / receipt / retry) of a
   request into exactly one sample.
3. Classifies each sample as success / failure / pending, folding the entry
   rule's allowed latency and required-receipt into the "bad request" notion.
4. Computes the bad-ratio lower and upper bounds (pending counted as success /
   failure respectively) and derives the verdict.
5. Lets late corrections adjust an *open* window and appends an incremental
   audit log; sealed windows keep their original decision forever.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Optional, Union
from .model import (
    KIND_CORRECTION,
    KIND_OBSERVATION,
    OUTCOME_FAILURE,
    OUTCOME_PENDING,
    OUTCOME_SUCCESS,
    STATUS_FAILURE,
    VERDICT_ALLOW,
    VERDICT_REJECT,
    VERDICT_WAIT,
    Correction,
    Counts,
    Decision,
    Observation,
    LogEntry,
    Rule,
    to_fraction,
)

Record = Union[Observation, Correction]


# ---------------------------------------------------------------------------
# Pure verdict maths (independently testable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bounds:
    lower: Fraction
    upper: Fraction
    total: int

    def as_floats(self) -> tuple[float, float]:
        return (float(self.lower), float(self.upper))


def compute_bounds(success: int, failure: int, pending: int) -> Bounds:
    """Bad-request ratio bounds.

    ``lower`` treats every undecided (pending) sample as a success, ``upper``
    treats every pending sample as a failure.  With no samples both bounds are
    ``0`` and the caller must emit ``wait``.
    """
    for name, val in (("success", success), ("failure", failure), ("pending", pending)):
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError(f"{name} must be a non-negative int")
    total = success + failure + pending
    if total == 0:
        return Bounds(Fraction(0), Fraction(0), 0)
    lower = Fraction(failure, total)
    upper = Fraction(failure + pending, total)
    return Bounds(lower, upper, total)


def evaluate(
    success: int, failure: int, pending: int, threshold: Union[int, float, Fraction, str]
) -> tuple[str, Bounds]:
    """Return ``(verdict, bounds)`` for the given counts and threshold.

    * no samples            -> ``wait``  (等证据)
    * upper bound <= thresh -> ``allow`` (可放)
    * lower bound >  thresh -> ``reject``(拒绝)
    * otherwise             -> ``wait``  (等证据)
    """
    thr = to_fraction(threshold)
    if thr < 0 or thr > 1:
        raise ValueError("threshold must be within [0, 1]")
    bounds = compute_bounds(success, failure, pending)
    if bounds.total == 0:
        return VERDICT_WAIT, bounds
    if bounds.upper <= thr:
        return VERDICT_ALLOW, bounds
    if bounds.lower > thr:
        return VERDICT_REJECT, bounds
    return VERDICT_WAIT, bounds


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass
class _Window:
    wid: int
    start: float
    end: float
    success: int = 0
    failure: int = 0
    pending: int = 0
    sealed: bool = False
    sealed_decision: Optional[Decision] = None

    @property
    def counts(self) -> Counts:
        return Counts(self.success, self.failure, self.pending)


@dataclass
class _Request:
    request_id: str
    entry_time: float
    wid: int
    rule: Rule
    observations: list[Observation] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    outcome: Optional[str] = None


# ---------------------------------------------------------------------------
# Adjudicator
# ---------------------------------------------------------------------------


class Adjudicator:
    """Stateful, event-time adjudicator over fixed-size windows."""

    def __init__(
        self,
        rules: Iterable[Rule],
        window_size_s: float,
        threshold: Union[int, float, Fraction, str],
        allowed_lateness_s: float = 0.0,
        auto_seal: bool = True,
    ) -> None:
        rule_list = list(rules)
        if not rule_list:
            raise ValueError("at least one Rule is required")
        self._rules = sorted(rule_list, key=lambda r: r.effective_from)
        self._rule_starts = [r.effective_from for r in self._rules]
        self._window_size = _positive(window_size_s, "window_size_s")
        self._lateness = _non_negative(allowed_lateness_s, "allowed_lateness_s")
        self._threshold = to_fraction(threshold)
        if self._threshold < 0 or self._threshold > 1:
            raise ValueError("threshold must be within [0, 1]")
        self.auto_seal = bool(auto_seal)

        self._watermark = -math.inf
        self._windows: dict[int, _Window] = {}
        self._requests: dict[str, _Request] = {}
        self._log: list[LogEntry] = []
        self._seq = 0

    # -- public config ------------------------------------------------------

    @property
    def threshold(self) -> Fraction:
        return self._threshold

    @property
    def window_size_s(self) -> float:
        return self._window_size

    @property
    def watermark(self) -> float:
        return self._watermark

    # -- rule selection -----------------------------------------------------

    def rule_at(self, entry_time: float) -> Rule:
        t = _finite(entry_time, "entry_time")
        idx = bisect.bisect_right(self._rule_starts, t) - 1
        if idx < 0:
            raise ValueError(
                f"no rule is in force at entry_time={t!r}; earliest rule starts at "
                f"{self._rule_starts[0]!r}"
            )
        return self._rules[idx]

    # -- window helpers -----------------------------------------------------

    def window_of(self, entry_time: float) -> tuple[int, float, float]:
        t = _finite(entry_time, "entry_time")
        wid = math.floor(t / self._window_size)
        start = wid * self._window_size
        return wid, start, start + self._window_size

    def _ensure_window(self, wid: int, start: float, end: float) -> _Window:
        win = self._windows.get(wid)
        if win is None:
            win = _Window(wid=wid, start=start, end=end)
            self._windows[wid] = win
        return win

    # -- ingestion ----------------------------------------------------------

    def ingest(self, record: Record) -> list[LogEntry]:
        """Ingest one :class:`Observation` or :class:`Correction`.

        Returns every log entry appended during the call (the record's own entry
        followed by any window-seal entries triggered by the watermark).
        """
        if not isinstance(record, (Observation, Correction)):
            raise TypeError("ingest expects an Observation or Correction")
        cause = KIND_CORRECTION if isinstance(record, Correction) else KIND_OBSERVATION

        wid, start, end = self.window_of(record.entry_time)
        win = self._ensure_window(wid, start, end)

        appended: list[LogEntry] = []

        req = self._requests.get(record.request_id)
        if req is None:
            req = _Request(
                request_id=record.request_id,
                entry_time=record.entry_time,
                wid=wid,
                rule=self.rule_at(record.entry_time),
            )
            is_new = True
        else:
            if req.entry_time != record.entry_time:
                raise ValueError(
                    f"request {record.request_id!r} first entered at {req.entry_time!r}, "
                    f"but a later record claims {record.entry_time!r}; first-entry time "
                    f"is immutable"
                )
            is_new = False

        if win.sealed:
            entry = self._emit(
                win=win,
                request_id=record.request_id,
                cause=cause,
                applied=False,
                reason="window sealed",
                prev_outcome=req.outcome,
                new_outcome=req.outcome,
                d_success=0,
                d_failure=0,
                d_pending=0,
                verdict_before=win.sealed_decision.verdict if win.sealed_decision else None,
                verdict_after=win.sealed_decision.verdict if win.sealed_decision else None,
                event_time=record.observed_at,
            )
            appended.append(entry)
            self._advance_watermark(record.observed_at)
            appended.extend(self._seal_due())
            return appended

        # Recognise the record and recompute this request's single outcome.
        if is_new:
            self._requests[record.request_id] = req
            prev_outcome = None
        else:
            prev_outcome = req.outcome

        if cause == KIND_CORRECTION:
            req.corrections.append(record)  # type: ignore[arg-type]
        else:
            req.observations.append(record)  # type: ignore[arg-type]

        new_outcome = self._classify(req)
        req.outcome = new_outcome

        verdict_before = self._live_verdict(win)
        d_success, d_failure, d_pending = _delta(prev_outcome, new_outcome)
        win.success += d_success
        win.failure += d_failure
        win.pending += d_pending
        verdict_after = self._live_verdict(win)

        entry = self._emit(
            win=win,
            request_id=record.request_id,
            cause=cause,
            applied=True,
            reason=None,
            prev_outcome=prev_outcome,
            new_outcome=new_outcome,
            d_success=d_success,
            d_failure=d_failure,
            d_pending=d_pending,
            verdict_before=verdict_before,
            verdict_after=verdict_after,
            event_time=record.observed_at,
        )
        appended.append(entry)

        self._advance_watermark(record.observed_at)
        appended.extend(self._seal_due())
        return appended

    def ingest_many(self, records: Iterable[Record]) -> list[LogEntry]:
        out: list[LogEntry] = []
        for rec in records:
            out.extend(self.ingest(rec))
        return out

    # -- classification -----------------------------------------------------

    def _classify(self, req: _Request) -> str:
        rule = req.rule
        deadline = req.entry_time + rule.user_deadline_ms / 1000.0

        if req.corrections:
            # A correction is the authoritative repair.  It is not gated by its
            # own arrival time (that is the whole point of a *late* correction),
            # but if it states when the truth became confirmable (``result_at``)
            # and that falls after the deadline, the user still had no
            # confirmable result in time -> pending.
            fact = max(req.corrections, key=_order_key)
            if fact.result_at is not None and fact.result_at > deadline:
                return OUTCOME_PENDING
            return _classify_fact(fact, rule)

        if not req.observations:
            return OUTCOME_PENDING
        fact = max(req.observations, key=_order_key)
        if fact.observed_at > deadline:
            # Nothing confirmable before the user deadline.
            return OUTCOME_PENDING
        return _classify_fact(fact, rule)

    # -- verdict helpers ----------------------------------------------------

    def _live_verdict(self, win: _Window) -> str:
        verdict, _ = evaluate(win.success, win.failure, win.pending, self._threshold)
        return verdict

    def _make_decision(self, win: _Window, sealed: bool) -> Decision:
        if sealed and win.sealed_decision is not None:
            return win.sealed_decision
        verdict, bounds = evaluate(win.success, win.failure, win.pending, self._threshold)
        return Decision(
            window_id=win.wid,
            window_start=win.start,
            window_end=win.end,
            verdict=verdict,
            lower_bound=float(bounds.lower),
            upper_bound=float(bounds.upper),
            total=win.success + win.failure + win.pending,
            success=win.success,
            failure=win.failure,
            pending=win.pending,
            threshold=float(self._threshold),
            sealed=sealed,
        )

    # -- sealing ------------------------------------------------------------

    def _seal_due(self, force: bool = False) -> list[LogEntry]:
        if (not self.auto_seal and not force) or self._watermark == -math.inf:
            return []
        out: list[LogEntry] = []
        for wid in sorted(self._windows):
            win = self._windows[wid]
            if win.sealed:
                continue
            if win.end + self._lateness <= self._watermark:
                out.append(self._seal_window(win))
        return out

    def _seal_window(self, win: _Window) -> LogEntry:
        decision = self._make_decision(win, sealed=False)
        win.sealed = True
        win.sealed_decision = Decision(
            window_id=decision.window_id,
            window_start=decision.window_start,
            window_end=decision.window_end,
            verdict=decision.verdict,
            lower_bound=decision.lower_bound,
            upper_bound=decision.upper_bound,
            total=decision.total,
            success=decision.success,
            failure=decision.failure,
            pending=decision.pending,
            threshold=decision.threshold,
            sealed=True,
        )
        return self._emit(
            win=win,
            request_id=None,
            cause="seal",
            applied=True,
            reason=None,
            prev_outcome=None,
            new_outcome=None,
            d_success=0,
            d_failure=0,
            d_pending=0,
            verdict_before=decision.verdict,
            verdict_after=decision.verdict,
            event_time=self._watermark if self._watermark != -math.inf else win.end,
        )

    def seal_window(self, wid: int) -> Optional[LogEntry]:
        """Explicitly seal a window regardless of the watermark."""
        win = self._windows.get(wid)
        if win is None or win.sealed:
            return None
        return self._seal_window(win)

    def advance_watermark(self, observed_at: float) -> list[LogEntry]:
        """Advance the watermark unconditionally, then seal any due windows.

        With ``auto_seal=False`` the watermark still moves (so callers can track
        progress) but windows are only sealed via :meth:`seal_window`.
        """
        t = _finite(observed_at, "observed_at")
        if t > self._watermark:
            self._watermark = t
        return self._seal_due(force=True)

    def _advance_watermark(self, observed_at: float) -> None:
        # Only ingest-driven advancement is gated by auto_seal; an explicit
        # advance_watermark call always moves the clock.
        if self.auto_seal and observed_at > self._watermark:
            self._watermark = observed_at

    # -- queries ------------------------------------------------------------

    def decision(self, wid: int) -> Decision:
        win = self._windows.get(wid)
        if win is None:
            _, start, end = _window_bounds_from_id(wid, self._window_size)
            verdict, bounds = evaluate(0, 0, 0, self._threshold)
            return Decision(
                window_id=wid,
                window_start=start,
                window_end=end,
                verdict=verdict,
                lower_bound=float(bounds.lower),
                upper_bound=float(bounds.upper),
                total=0,
                success=0,
                failure=0,
                pending=0,
                threshold=float(self._threshold),
                sealed=False,
            )
        return self._make_decision(win, sealed=win.sealed)

    def decision_for_time(self, entry_time: float) -> Decision:
        wid, _, _ = self.window_of(entry_time)
        return self.decision(wid)

    def decisions(self) -> list[Decision]:
        return [self._make_decision(self._windows[w], self._windows[w].sealed) for w in sorted(self._windows)]

    def request_outcome(self, request_id: str) -> Optional[str]:
        req = self._requests.get(request_id)
        return None if req is None else req.outcome

    def log(self) -> list[LogEntry]:
        return list(self._log)

    def timeline(self, request_id: str) -> list[LogEntry]:
        return [e for e in self._log if e.request_id == request_id]

    def seal_entries(self, wid: Optional[int] = None) -> list[LogEntry]:
        return [
            e
            for e in self._log
            if e.cause == "seal" and (wid is None or e.window_id == wid)
        ]

    # -- logging ------------------------------------------------------------

    def _emit(
        self,
        *,
        win: _Window,
        request_id: Optional[str],
        cause: str,
        applied: bool,
        reason: Optional[str],
        prev_outcome: Optional[str],
        new_outcome: Optional[str],
        d_success: int,
        d_failure: int,
        d_pending: int,
        verdict_before: Optional[str],
        verdict_after: Optional[str],
        event_time: float,
    ) -> LogEntry:
        self._seq += 1
        verdict, bounds = evaluate(win.success, win.failure, win.pending, self._threshold)
        entry = LogEntry(
            seq=self._seq,
            window_id=win.wid,
            request_id=request_id,
            cause=cause,
            applied=applied,
            reason=reason,
            prev_outcome=prev_outcome,
            new_outcome=new_outcome,
            delta_success=d_success,
            delta_failure=d_failure,
            delta_pending=d_pending,
            counts=win.counts,
            lower_bound=float(bounds.lower),
            upper_bound=float(bounds.upper),
            verdict_before=verdict_before,
            verdict_after=verdict_after,
            event_time=event_time,
            watermark=self._watermark,
        )
        self._log.append(entry)
        return entry


# ---------------------------------------------------------------------------
# Free functions
# ---------------------------------------------------------------------------


def _classify_fact(fact: Union[Observation, Correction], rule: Rule) -> str:
    """Map a single authoritative fact to success / failure / pending."""
    if fact.status == STATUS_FAILURE:
        return OUTCOME_FAILURE
    # status == success
    if rule.requires_receipt and not _is_receipt(fact):
        return OUTCOME_PENDING
    latency = _effective_latency_ms(fact)
    if latency > rule.allowed_latency_ms:
        return OUTCOME_FAILURE
    return OUTCOME_SUCCESS


def _is_receipt(fact: Union[Observation, Correction]) -> bool:
    return bool(fact.receipt_present) or getattr(fact, "stream", None) == "receipt"


def _effective_latency_ms(fact: Union[Observation, Correction]) -> float:
    if fact.latency_ms is not None:
        return float(fact.latency_ms)
    # For a correction, ``observed_at`` is the (possibly very late) arrival of
    # the correction, not when the service completed; prefer the stated
    # ``result_at`` there.  Observations fall back to their observation time.
    reference = fact.observed_at
    result_at = getattr(fact, "result_at", None)
    if result_at is not None:
        reference = result_at
    return max(0.0, (reference - fact.entry_time) * 1000.0)


def _order_key(fact: Union[Observation, Correction]) -> tuple:
    """Authoritative ordering among facts of one request.

    Highest version wins ("valid version").  Within a version a final receipt
    outranks a partial success, then the later logical attempt, then the most
    recent observation.  Corrections are chosen in a separate pool and always
    supersede observations.
    """
    return (
        fact.version,
        1 if _is_receipt(fact) else 0,
        fact.attempt,
        fact.observed_at,
    )


def _delta(prev: Optional[str], new: Optional[str]) -> tuple[int, int, int]:
    """Return ``(d_success, d_failure, d_pending)`` for an outcome change."""
    if prev == new:
        return (0, 0, 0)
    d = {OUTCOME_SUCCESS: 0, OUTCOME_FAILURE: 0, OUTCOME_PENDING: 0}
    if prev in d:
        d[prev] -= 1
    if new in d:
        d[new] += 1
    return (d[OUTCOME_SUCCESS], d[OUTCOME_FAILURE], d[OUTCOME_PENDING])


def _window_bounds_from_id(wid: int, size: float) -> tuple[int, float, float]:
    start = wid * size
    return wid, start, start + size


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value)!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


def _positive(value: float, name: str) -> float:
    value = _finite(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _non_negative(value: float, name: str) -> float:
    value = _finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value
