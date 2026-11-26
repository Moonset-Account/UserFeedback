"""Discrete event input support.

An event log is sorted by ``(time, explicit_order, stable input index)``.
Thus events at different times are chronological, while simultaneous events
retain the caller's permutation unless explicit ``order`` values override it.
This is exactly what tests need to compare different simultaneous reclamation
orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .model import EventKind, TaskSpec
from .planner import PlannerConfig, ReclamationPlan
from .resources import ResourceVector, as_vector
from .scheduler import HybridScheduler, SchedulerTraceRecord


@dataclass(frozen=True)
class Event:
    time: float
    kind: EventKind | str
    payload: Mapping[str, Any] = field(default_factory=dict)
    order: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EventKind(self.kind))
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass
class EventResult:
    scheduler: HybridScheduler
    records: list[SchedulerTraceRecord]
    plans: list[ReclamationPlan]
    event_count: int

    @property
    def final_state(self):
        return self.scheduler.state


def run_event_log(
    events: Sequence[Event | Mapping[str, Any]],
    *,
    capacity: ResourceVector | Mapping[str, Any],
    config: PlannerConfig | None = None,
) -> EventResult:
    """Run a complete synthetic discrete-event log."""
    scheduler = HybridScheduler(capacity=capacity, config=config)
    normalized: list[tuple[float, int, int, Event]] = []
    for index, raw in enumerate(events):
        event = _coerce_event(raw)
        normalized.append((event.time, event.order, index, event))
    normalized.sort(key=lambda item: (item[0], item[1], item[2]))

    processed = 0
    for _, _, input_index, event in normalized:
        processed += 1
        _handle_event(scheduler, event, input_index)

    return EventResult(
        scheduler=scheduler,
        records=list(scheduler.trace),
        plans=list(scheduler.plans),
        event_count=processed,
    )


def _coerce_event(raw: Event | Mapping[str, Any]) -> Event:
    if isinstance(raw, Event):
        return raw
    return Event(
        time=float(raw["time"]),
        kind=raw["kind"],
        payload=raw.get("payload", {k: v for k, v in raw.items() if k not in {"time", "kind", "order", "payload"}}),
        order=int(raw.get("order", 0)),
    )


def _handle_event(scheduler: HybridScheduler, event: Event, input_index: int) -> None:
    payload = dict(event.payload)
    kind = event.kind
    at = event.time

    if kind == EventKind.SUBMIT:
        spec = payload["spec"]
        if not isinstance(spec, TaskSpec):
            raise TypeError("submit event payload must contain TaskSpec under 'spec'")
        scheduler.submit(
            spec,
            at=at,
            start=bool(payload.get("start", False)),
            allocation=payload.get("allocation"),
            event_order=int(payload.get("event_order", input_index)),
        )
    elif kind == EventKind.START:
        scheduler.start(payload["task_id"], at=at, allocation=payload.get("allocation"))
    elif kind == EventKind.LEND:
        scheduler.lend(
            payload["owner_id"],
            payload["borrower_id"],
            as_vector(payload["amount"]),
            at=at,
        )
    elif kind == EventKind.RECLAIM:
        if "requests" in payload or "stream_ids" in payload:
            requests = payload.get("requests")
            if requests is None:
                requests = [
                    {
                        "stream_id": stream_id,
                        "borrower_order": payload.get("borrower_order"),
                    }
                    for stream_id in payload["stream_ids"]
                ]
            scheduler.reclaim_many(
                requests,
                at=at,
                apply=bool(payload.get("apply", True)),
                borrower_order=payload.get("global_borrower_order"),
            )
        else:
            scheduler.reclaim(
                payload["stream_id"],
                at=at,
                amount=None if payload.get("amount") is None else as_vector(payload["amount"]),
                borrower_order=payload.get("borrower_order"),
                apply=bool(payload.get("apply", True)),
            )
    elif kind in (EventKind.STREAM_FINISH, EventKind.BATCH_FINISH):
        scheduler.finish(payload["task_id"], at=at)
        resumed = scheduler.try_resume_deferred(at=at)
        if resumed:
            scheduler._trace(  # noqa: SLF001 - concise event composition
                at,
                "recovery_scan",
                f"任务完成后恢复暂停批任务: {', '.join(resumed)}",
                details={"resumed": list(resumed)},
            )
    elif kind == EventKind.OFFER_IDLE_QUOTA:
        # A stream lends/offers quota back to the scheduler pool.  In this
        # accounting model finishing a stream already frees its allocation;
        # this event explicitly triggers another recovery scan.
        scheduler.state.now = at
        resumed = scheduler.try_resume_deferred(at=at)
        scheduler._trace(
            at,
            "offer_idle_quota",
            payload.get("description", f"出现新的可用额度，恢复扫描结果: {list(resumed)}"),
            details={"resumed": list(resumed), "amount": as_vector(payload.get("amount")).to_json()},
        )
    elif kind == EventKind.TICK:
        scheduler.state.now = at
        scheduler._trace(at, "tick", payload.get("description", f"时钟推进到 {at}"))
    else:  # pragma: no cover - enum validation prevents this
        raise ValueError(f"unsupported event kind {kind}")
