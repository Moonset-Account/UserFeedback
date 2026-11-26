"""Local synthetic demo for the hybrid scheduler.

Run with:

    python3 examples/demo.py

It uses only in-memory objects and exercises:

1. a feasible holder-first buffer cascade;
2. canonical resource-cycle explanation under reversed input order;
3. a three-borrower simultaneous reclamation permutation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hybrid_scheduler import (
    Event,
    EventKind,
    Phase,
    ResourceVector,
    TaskKind,
    TaskSpec,
    run_event_log,
)

MEM = ResourceVector.of


def stream(sid: str, quota_memory: int) -> TaskSpec:
    return TaskSpec(
        id=sid,
        kind=TaskKind.STREAM,
        quota=MEM(memory=quota_memory),
        phases=(Phase(id="run", resources=MEM(slots=1, memory=1)),),
        deadline=100,
    )


def batch(bid: str, *, buffer_memory: int = 0, continue_memory: int | None = 0) -> TaskSpec:
    return TaskSpec(
        id=bid,
        kind=TaskKind.BATCH,
        deadline=50,
        phases=(
            Phase(
                id="boundary",
                legal_boundary=True,
                savepoint_allowed=True,
                checkpoint_buffer=MEM(memory=buffer_memory),
                continue_resources=None if continue_memory is None else MEM(memory=continue_memory),
            ),
        ),
    )


def feasible_cascade() -> None:
    events = [
        Event(0, EventKind.SUBMIT, {"spec": stream("s1", 4)}),
        Event(0, EventKind.SUBMIT, {"spec": batch("holder")}),
        Event(0, EventKind.SUBMIT, {"spec": batch("waiter", buffer_memory=2, continue_memory=None)}),
        Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "holder", "amount": MEM(memory=2)}),
        Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "waiter", "amount": MEM(memory=2)}),
        Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": ["waiter", "holder"]}),
        Event(8, EventKind.STREAM_FINISH, {"task_id": "s1"}),
    ]
    result = run_event_log(events, capacity=MEM(memory=4))
    plan = result.plans[0]
    print("[feasible] release order:", plan.release_order)
    print("[feasible] suspended:", plan.suspended)
    print("[feasible] final waiter status:", result.scheduler.state.task("waiter").status.value)


def cycle(order: list[str]) -> None:
    events = [
        Event(0, EventKind.SUBMIT, {"spec": stream("s1", 4)}),
        Event(0, EventKind.SUBMIT, {"spec": batch("a", buffer_memory=2, continue_memory=None)}),
        Event(0, EventKind.SUBMIT, {"spec": batch("b", buffer_memory=2, continue_memory=None)}),
        Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "a", "amount": MEM(memory=2)}),
        Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": "b", "amount": MEM(memory=2)}),
        Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": order}),
    ]
    result = run_event_log(events, capacity=MEM(memory=4))
    plan = result.plans[0]
    assert plan.cycle is not None
    print(f"[cycle input={order}] nodes={plan.cycle.nodes}")
    print(" ", plan.cycle.message)


def permutations() -> None:
    import itertools

    for order in itertools.permutations(("a", "b", "c")):
        events = [
            Event(0, EventKind.SUBMIT, {"spec": stream("s1", 6)}),
            *[Event(0, EventKind.SUBMIT, {"spec": batch(name)}) for name in order],
            *[
                Event(0, EventKind.LEND, {"owner_id": "s1", "borrower_id": name, "amount": MEM(memory=2)})
                for name in order
            ],
            Event(5, EventKind.RECLAIM, {"stream_id": "s1", "borrower_order": list(order)}),
        ]
        plan = run_event_log(events, capacity=MEM(memory=6)).plans[0]
        print("[permutation]", order, "->", plan.release_order)


if __name__ == "__main__":
    feasible_cascade()
    cycle(["a", "b"])
    cycle(["b", "a"])
    permutations()
