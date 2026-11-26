"""Deterministic local synthetic scenario used by the demo and README.

This is not a load generator and performs no network or filesystem access.
It creates an interleaving of success, active withdrawal, throttled backlog,
cross-window batch recovery, duplicate reports, and inferred capacity loss.
"""

from __future__ import annotations

from .clock import ClockSpec, ManualClock


def build_synthetic_scenario():
    clock = ManualClock()
    events: list[dict] = []

    def emit(true_time: float, **event):
        clock.advance(true_time - clock.now)
        event.setdefault("timestamp", true_time)
        event.setdefault("true_timestamp", true_time)
        events.append(event)

    emit(1.0, event_id="ok-arr", kind="arrival", request_id="ok1")
    emit(2.0, event_id="wd-arr", kind="arrival", request_id="wd1")
    emit(3.0, event_id="lost-batch-arr", kind="arrival",
         lineage_id="lost-batch", count=2)
    emit(4.0, event_id="ok-done", kind="completion", request_id="ok1")
    events.append({"event_id": "ok-done", "kind": "completion",
                   "timestamp": 4.0, "request_id": "ok1"})

    emit(7.0, event_id="wd", kind="withdrawal", request_id="wd1")

    emit(9.0, event_id="cross-arr", kind="arrival", lineage_id="cross", count=4)
    emit(9.5, event_id="cross-limit", kind="rate_limited",
         lineage_id="cross", count=4)
    emit(10.6, event_id="cross-rr", kind="reissue", lineage_id="cross",
         batch_id="rr-next-window", count=4)
    emit(14.0, event_id="cross-done-3", kind="completion",
         lineage_id="cross", count=3)
    emit(15.0, event_id="cross-wd-1", kind="withdrawal",
         lineage_id="cross", count=1)

    clock.advance(25.0 - clock.now)
    clock_bounds = {"control": ClockSpec(offset=0.0, error_bound=0.05)}
    return {
        "events": events,
        "clock": clock,
        "exhaustion_at": 25.0,
        "window_width": 10.0,
        "clock_bounds": clock_bounds,
    }
