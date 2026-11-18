"""End-to-end smoke demo for the result publishing module.

Run from the repository root with:

    python3 -I demo.py
"""

import random
import sys
from pathlib import Path

# Make the package importable when run directly with -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from result_publisher import (
    BudgetRegistry,
    BudgetScope,
    DataSnapshot,
    LaplaceCountAdapter,
    Query,
    QuotaExceededError,
    ResultPublisher,
)


def main() -> None:
    scope = BudgetScope(dataset="hr", purpose="staffing_audit")
    registry = BudgetRegistry({scope: 5.0})
    adapter = LaplaceCountAdapter(
        sensitivity=1.0, epsilon=2.0, rng=random.Random(11)
    )
    publisher = ResultPublisher(registry, adapter, costs=lambda q: 2.0)

    snap1 = DataSnapshot(
        "hr", "snap-1", payload=[{"id": i} for i in range(12)]
    )
    q1 = Query(
        "hr",
        "staffing_audit",
        "on_duty_count",
        {"shift": "day", "teams": {"a"}},
    )
    # Same semantics, different raw filter layout.
    q2 = Query(
        "hr",
        "staffing_audit",
        "on_duty_count",
        {"teams": ["a"], "shift": "day"},
    )

    r1 = publisher.publish(q1, snap1)
    r2 = publisher.publish(q2, snap1)
    assert r1 is r2, "equivalent queries must share one cached result"
    print(
        "cached reuse ok:",
        round(r1.value, 4),
        "consumed:",
        registry.get(scope).consumed,
    )

    snap2 = DataSnapshot(
        "hr", "snap-2", payload=[{"id": i} for i in range(12)]
    )
    r3 = publisher.publish(q1, snap2)
    assert r3 is not r1
    print(
        "snapshot change re-charged; consumed:",
        registry.get(scope).consumed,
    )

    q3 = Query(
        "hr", "staffing_audit", "on_duty_count", {"shift": "night"}
    )
    try:
        publisher.publish(q3, snap2)
    except QuotaExceededError as exc:
        print("blocked as expected:", exc)
    print("available after block:", registry.get(scope).available)
    print("stats:", publisher.stats())


if __name__ == "__main__":
    main()
