"""Shared small-instance builders for the test-suite.

Every fixture deliberately stays below twelve stages so that the exhaustive
time-indexed search in :mod:`emergency_scheduler.brute_force` can serve as an
independent oracle.
"""

from __future__ import annotations

import random

from emergency_scheduler import Component, Problem, Service, Stage


def sequencing_problem() -> Problem:
    """Two failed units, one technician; bigger unit should be repaired first."""
    a = Component("A", {"s1": 6.0}, [Stage("a1", "A", duration=2, personnel=1)])
    b = Component("B", {"s1": 4.0}, [Stage("b1", "B", duration=2, personnel=1)])
    service = Service("s1", demand=10.0, min_capacity=0.0, weight=1.0)
    return Problem(
        components=[a, b],
        services=[service],
        horizon=8,
        personnel_capacity=1,
    )


def spare_release_problem() -> Problem:
    """Small-recovery unit B must go first: its repair releases the spare part
    that unit A (large recovery) is waiting for."""
    a = Component(
        "A",
        {"s1": 8.0},
        [
            Stage(
                "a1",
                "A",
                duration=3,
                personnel=1,
                spare_needs={"pump": 1},
            )
        ],
    )
    b = Component(
        "B",
        {"s1": 2.0},
        [
            Stage(
                "b1",
                "B",
                duration=2,
                personnel=1,
                spare_releases={"pump": 1},
            )
        ],
    )
    service = Service("s1", demand=10.0, weight=1.0)
    return Problem(
        components=[a, b],
        services=[service],
        horizon=10,
        personnel_capacity=1,
        initial_spares={"pump": 0},
    )


def stop_redundant_problem() -> Problem:
    """Repairing A forces redundant worker C offline for the whole stage."""
    a = Component(
        "A",
        {"s1": 5.0},
        [
            Stage(
                "a1",
                "A",
                duration=3,
                personnel=1,
                stop_components=("C",),
            )
        ],
    )
    b = Component("B", {"s1": 4.0}, [Stage("b1", "B", duration=2, personnel=1)])
    c = Component("C", {"s1": 3.0}, stages=[])
    # C is a healthy redundant unit: no stages, always online unless stopped.
    service = Service("s1", demand=12.0, min_capacity=2.0, weight=2.0)
    return Problem(
        components=[a, b, c],
        services=[service],
        horizon=10,
        personnel_capacity=1,
    )


def stop_violates_minimum_problem() -> Problem:
    """Stopping redundant C drops capacity below the hard minimum immediately.

    The horizon is long enough to hold the work, so the first unavoidable
    constraint is the minimum-capacity breach, not the horizon.
    """
    a = Component(
        "A",
        {"s1": 5.0},
        [Stage("a1", "A", duration=2, personnel=1, stop_components=("C",))],
    )
    c = Component("C", {"s1": 3.0}, stages=[])
    service = Service("s1", demand=10.0, min_capacity=3.0)
    return Problem(
        components=[a, c],
        services=[service],
        horizon=4,
        personnel_capacity=1,
    )


def spare_starvation_problem() -> Problem:
    """A stage needs a spare that is neither stocked nor ever released."""
    a = Component(
        "A",
        {"s1": 4.0},
        [Stage("a1", "A", duration=2, personnel=1, spare_needs={"seal": 1})],
    )
    b = Component("B", {"s1": 2.0}, [Stage("b1", "B", duration=1, personnel=1)])
    service = Service("s1", demand=6.0)
    return Problem(
        components=[a, b],
        services=[service],
        horizon=6,
        personnel_capacity=2,
        initial_spares={"seal": 0},
    )


def horizon_infeasible_problem() -> Problem:
    """Total serial work exceeds the finite horizon."""
    a = Component("A", {"s1": 1.0}, [Stage("a1", "A", duration=4, personnel=1)])
    b = Component("B", {"s1": 1.0}, [Stage("b1", "B", duration=4, personnel=1)])
    service = Service("s1", demand=2.0)
    return Problem(
        components=[a, b],
        services=[service],
        horizon=5,
        personnel_capacity=1,
    )


def multistage_problem() -> Problem:
    """One component with two ordered, non-preemptive stages."""
    a = Component(
        "A",
        {"s1": 5.0},
        [
            Stage("a1", "A", duration=2, personnel=1),
            Stage("a2", "A", duration=1, personnel=1, spare_needs={"kit": 1}),
        ],
    )
    b = Component("B", {"s1": 3.0}, [Stage("b1", "B", duration=2, personnel=1)])
    service = Service("s1", demand=8.0, weight=1.0)
    return Problem(
        components=[a, b],
        services=[service],
        horizon=9,
        personnel_capacity=1,
        initial_spares={"kit": 1},
    )


def interleaving_problem() -> Problem:
    """Two technicians allow genuine stage interleaving; spares remain serial."""
    a = Component(
        "A",
        {"s1": 4.0},
        [
            Stage("a1", "A", duration=2, personnel=1, spare_needs={"v": 1}),
            Stage("a2", "A", duration=2, personnel=1),
        ],
    )
    b = Component(
        "B",
        {"s1": 3.0},
        [
            Stage("b1", "B", duration=2, personnel=1, spare_needs={"v": 1}),
            Stage("b2", "B", duration=1, personnel=1),
        ],
    )
    service = Service("s1", demand=7.0, weight=1.0)
    return Problem(
        components=[a, b],
        services=[service],
        horizon=10,
        personnel_capacity=2,
        initial_spares={"v": 1},
    )


def random_problem(rng: random.Random, *, allow_stops: bool = True) -> Problem:
    """Generate a bounded random instance for cross-checking against brute force."""
    n_components = rng.randint(2, 3)
    horizon = rng.randint(8, 12)
    personnel_capacity = rng.randint(1, 2)
    spare_kinds = ["v", "kit"]
    initial_spares = {sid: rng.randint(0, 1) for sid in spare_kinds}
    component_ids = [f"C{i}" for i in range(n_components)]
    components: list[Component] = []
    total_stages = 0
    for idx, cid in enumerate(component_ids):
        n_stages = rng.randint(1, 2)
        total_stages += n_stages
        stages = []
        for j in range(n_stages):
            needs = {}
            if rng.random() < 0.5:
                needs[rng.choice(spare_kinds)] = 1
            releases = {}
            if rng.random() < 0.4:
                releases[rng.choice(spare_kinds)] = 1
            stops = ()
            if allow_stops and n_components > 1 and rng.random() < 0.35:
                others = [other for other in component_ids if other != cid]
                stops = (rng.choice(others),)
            stages.append(
                Stage(
                    stage_id=f"{cid.lower()}{j + 1}",
                    component_id=cid,
                    duration=rng.randint(1, 3),
                    personnel=rng.randint(1, personnel_capacity),
                    spare_needs=needs,
                    spare_releases=releases,
                    stop_components=stops,
                )
            )
        components.append(
            Component(
                cid,
                {"s1": float(rng.randint(1, 6))},
                stages,
            )
        )
    service = Service(
        "s1",
        demand=float(rng.randint(2, 10)),
        min_capacity=float(rng.choice([0, 0, 0, 2])),
        weight=float(rng.randint(1, 3)),
    )
    problem = Problem(
        components=components,
        services=[service],
        horizon=horizon,
        personnel_capacity=personnel_capacity,
        initial_spares=initial_spares,
    )
    # keep fixtures under twelve stages as promised
    assert total_stages < 12
    return problem
