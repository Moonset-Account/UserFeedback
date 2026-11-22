"""Concrete, executable scenarios that exercise the adjudicator end to end.

These are **not** canned outputs.  Every value in a :class:`TraceStep` is read
back from a live :class:`~sli_adjudicator.engine.Adjudicator` after ingesting a
record, so the trace is a faithful record of what the engine actually decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .engine import Adjudicator
from .model import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STREAM_DOWNSTREAM,
    STREAM_EDGE,
    STREAM_RECEIPT,
    Correction,
    Decision,
    Observation,
    Rule,
)


@dataclass(frozen=True)
class TraceStep:
    label: str
    event_time: float
    window_id: int
    verdict: str
    success: int
    failure: int
    pending: int
    lower_bound: float
    upper_bound: float
    prev_outcome: Optional[str]
    new_outcome: Optional[str]


@dataclass
class ReplayResult:
    adjudicator: Adjudicator
    target_window: int
    steps: list[TraceStep]

    def verdict_timeline(self) -> list[tuple[float, str]]:
        """``(event_time, verdict)`` at each step where the verdict moved."""
        out: list[tuple[float, str]] = []
        last: Optional[str] = None
        for step in self.steps:
            if step.verdict != last:
                out.append((step.event_time, step.verdict))
                last = step.verdict
        return out

    def final_decision(self) -> Decision:
        return self.adjudicator.decision(self.target_window)


def _step_from_log(label: str, entry, adj: Adjudicator, wid: int) -> TraceStep:
    dec = adj.decision(wid)
    return TraceStep(
        label=label,
        event_time=entry.event_time,
        window_id=wid,
        verdict=dec.verdict,
        success=dec.success,
        failure=dec.failure,
        pending=dec.pending,
        lower_bound=dec.lower_bound,
        upper_bound=dec.upper_bound,
        prev_outcome=entry.prev_outcome,
        new_outcome=entry.new_outcome,
    )


def replay_failure_retry_correction() -> ReplayResult:
    """Replay one request: failure -> retry success (partial) -> late correction.

    Setup
    -----
    * window size 10 s, window 1 == ``[10, 20)``; target request enters at 12.0
    * rule: allowed latency 500 ms, receipt required, user deadline 4000 ms
    * threshold ``0.05``; allowed lateness 3 s (window 1 seals once the
      watermark reaches 23.0)
    * nine confirmed successes already sit in window 1, so the request is the
      only lever that can move the verdict

    Story
    -----
    1. ``t=12.2``  the edge reports **failure**            -> window bad-ratio 0.10 -> **reject**
    2. ``t=13.0``  the retry (attempt 1) succeeds in 200 ms but carries no final
       receipt, so it is a *partial* success -> sample becomes **pending**
       -> bounds [0.00, 0.10] straddle 0.05 -> **wait**
    3. ``t=20.5``  a late correction (version 3) confirms the timely success with
       a receipt; the window is still open (20 < 23) -> sample becomes
       **success** -> bad-ratio 0.00 -> **allow**
    4. ``t=24.0``  traffic in window 2 pushes the watermark past 23, sealing
       window 1 with its **allow** verdict
    5. ``t=24.5``  a further correction arrives for the sealed window and is
       recorded but not applied -- sealed history keeps its verdict
    """
    rule = Rule(
        effective_from=0.0,
        allowed_latency_ms=500.0,
        requires_receipt=True,
        user_deadline_ms=4000.0,
    )
    adj = Adjudicator(
        [rule],
        window_size_s=10.0,
        threshold="0.05",
        allowed_lateness_s=3.0,
    )
    target_wid, _, _ = adj.window_of(12.0)

    steps: list[TraceStep] = []

    # Nine confirmed successes (one sample each) already in window 1.
    for i in range(9):
        entry = 11.0 + i
        out = adj.ingest(
            Observation(
                request_id=f"filler-{i}",
                entry_time=entry,
                attempt=0,
                version=1,
                stream=STREAM_RECEIPT,
                status=STATUS_SUCCESS,
                latency_ms=100.0,
                receipt_present=True,
                observed_at=entry + 0.1,
            )
        )
        steps.append(_step_from_log(f"filler-{i}", out[0], adj, target_wid))

    # 1. Edge reports failure for req-42 (attempt 0, version 1).
    out = adj.ingest(
        Observation(
            request_id="req-42",
            entry_time=12.0,
            attempt=0,
            version=1,
            stream=STREAM_EDGE,
            status=STATUS_FAILURE,
            observed_at=12.2,
        )
    )
    steps.append(_step_from_log("retry-failure", out[0], adj, target_wid))

    # 2. Retry (attempt 1, version 2) succeeds in 200 ms, but only a downstream
    #    partial success is seen -- no final receipt yet -> pending.
    out = adj.ingest(
        Observation(
            request_id="req-42",
            entry_time=12.0,
            attempt=1,
            version=2,
            stream=STREAM_DOWNSTREAM,
            status=STATUS_SUCCESS,
            latency_ms=200.0,
            receipt_present=False,
            observed_at=13.0,
        )
    )
    steps.append(_step_from_log("retry-partial-success", out[0], adj, target_wid))

    # 3. Late correction (version 3) confirms the timely success with a receipt.
    out = adj.ingest(
        Correction(
            request_id="req-42",
            entry_time=12.0,
            attempt=1,
            version=3,
            status=STATUS_SUCCESS,
            latency_ms=200.0,
            receipt_present=True,
            observed_at=20.5,
            note="receipt stream confirmed the retried attempt",
        )
    )
    steps.append(_step_from_log("late-correction", out[0], adj, target_wid))

    # 4. Traffic in window 2 advances the watermark past 23.0 -> seal window 1.
    out = adj.ingest(
        Observation(
            request_id="filler-w2",
            entry_time=23.5,
            attempt=0,
            version=1,
            stream=STREAM_RECEIPT,
            status=STATUS_SUCCESS,
            latency_ms=90.0,
            receipt_present=True,
            observed_at=24.0,
        )
    )
    seal = next((e for e in out if e.cause == "seal" and e.window_id == target_wid), None)
    if seal is not None:
        steps.append(_step_from_log("seal-window-1", seal, adj, target_wid))

    # 5. A correction aimed at the sealed window is recorded but not applied.
    out = adj.ingest(
        Correction(
            request_id="req-42",
            entry_time=12.0,
            attempt=1,
            version=4,
            status=STATUS_FAILURE,
            latency_ms=200.0,
            receipt_present=True,
            observed_at=24.5,
            note="too late: window already sealed",
        )
    )
    steps.append(_step_from_log("post-seal-correction", out[0], adj, target_wid))

    return ReplayResult(adjudicator=adj, target_window=target_wid, steps=steps)
