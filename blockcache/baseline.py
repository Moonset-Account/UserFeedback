"""A lazy access-distance baseline used to demonstrate optimality gaps."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from .errors import InfeasiblePlanError
from .interpreter import TickInterpreter, sym_value
from .model import Request, coerce_requests
from .plan import (
    AdvanceEvent,
    BeginRequestEvent,
    EvictEvent,
    Plan,
    StartWritebackEvent,
)


def _next_read_write_use(requests: tuple[Request, ...], page: str, cursor: int) -> int:
    for j in range(cursor, len(requests)):
        req = requests[j]
        if req.__class__.__name__ in {"Read", "Write"} and req.page == page:
            return j
    return 10**9


def access_distance_greedy_plan(
    planner: Any,
    requests: Sequence[Any],
    *,
    proactive_writeback: bool = False,
) -> Plan:
    """Construct a Belady-style plan.

    The default policy is deliberately lazy: it writes a dirty victim back only
    when replacement needs its slot.  That is the policy that can lose to the
    optimal planner's early overlapping writeback.
    """

    reqs = coerce_requests(requests)
    driver = TickInterpreter(planner, reqs)
    events: list[Any] = []

    def emit(event: Any) -> None:
        events.append(event)
        driver.execute_event(event)

    def flush_one(page: str) -> None:
        r = driver.residents[page]
        duration = planner.writeback_duration(page)
        job_id = driver.next_job_id
        value = r.value if r.value is not None else sym_value(page, r.generation)
        start = StartWritebackEvent(
            driver.now,
            job_id,
            page,
            r.generation,
            planner.pages[page].size,
            duration,
            value,
        )
        emit(start)
        advance = AdvanceEvent(driver.now, duration)
        emit(advance)

    while driver.cursor < len(reqs):
        i = driver.cursor
        req = reqs[i]
        kind = req.__class__.__name__

        if kind in {"Read", "Write"}:
            pid = req.page
            needed = planner.pages[pid].size
            is_missing = pid not in driver.residents
            if is_missing:
                while driver.used_bytes + needed > planner.capacity:
                    candidates = [
                        p
                        for p, r in driver.residents.items()
                        if driver.pins.get(p, 0) == 0 and r.ready and not driver.active
                    ]
                    if not candidates:
                        raise InfeasiblePlanError("greedy baseline has no evictable candidate")
                    victim = max(
                        candidates,
                        key=lambda p: (
                            _next_read_write_use(reqs, p, i),
                            planner.pages[p].size,
                            p,
                        ),
                    )
                    if driver.residents[victim].dirty:
                        flush_one(victim)
                    evict = EvictEvent(driver.now, victim, planner.pages[victim].size)
                    emit(evict)

            if kind == "Read":
                if not is_missing or pid in driver.residents:
                    duration = (
                        req.hit_ticks if req.hit_ticks is not None else planner.read_hit_ticks
                    )
                    op = "read_hit"
                else:
                    duration = (
                        req.miss_ticks if req.miss_ticks is not None else planner.read_miss_ticks
                    )
                    op = "read_miss"
            else:
                duration = req.ticks if req.ticks is not None else planner.write_ticks
                op = "write"
            begin = BeginRequestEvent(driver.now, i, op, pid, duration)
            emit(begin)
            if duration > 0:
                emit(AdvanceEvent(driver.now, duration))
            continue

        op = "pin" if kind == "Pin" else "unpin"
        begin = BeginRequestEvent(driver.now, i, op, req.page, 0)
        emit(begin)

    # Durability phase.  Any order has the same channel duration; use page id
    # order for deterministic output.
    for page in sorted(p for p, r in driver.residents.items() if r.dirty):
        flush_one(page)

    verified = TickInterpreter(planner, reqs).build_plan(events, search_states=0)
    return replace(verified, optimal=False)
