"""Discrete-time sweep simulation of a concrete schedule.

The simulation independently re-evaluates every unit interval ``[t, t+1)`` of
the horizon, so it double-checks every feasibility rule rather than trusting
the solver's internal bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Problem, Stage


@dataclass
class Violation:
    time: int
    kind: str
    detail: str
    related: dict[str, object] = field(default_factory=dict)


@dataclass
class SimulationReport:
    feasible: bool
    violation: Violation | None
    #: unmet[service_id] integrated over all horizon ticks
    total_unmet: dict[str, float]
    weighted_objective: float
    #: segment_unmet[stage_id][service_id] integrated on that stage's interval
    segment_unmet: dict[str, dict[str, float]]
    #: segment_personnel[stage_id] -> personnel occupied per tick of interval
    segment_personnel: dict[str, int]
    completion_time: dict[str, int]
    makespan: int
    #: per-tick service capacity actually available
    capacity_trace: list[dict[str, float]]
    #: per-tick unmet demand per service
    unmet_trace: list[dict[str, float]]


def simulate(
    problem: Problem,
    assignments: list[tuple[Stage, int]],
) -> SimulationReport:
    """Evaluate a full assignment ``(stage, start)`` over ``[0, horizon)``."""

    horizon = problem.horizon
    services = problem.services
    service_ids = [s.service_id for s in services]
    demand = {s.service_id: s.demand for s in services}
    minimum = {s.service_id: s.min_capacity for s in services}
    weight = {s.service_id: s.weight for s in services}

    components = {c.component_id: c for c in problem.components}
    last_stage_of = {
        c.component_id: c.stages[-1].stage_id
        for c in problem.components
        if c.stages
    }

    # ---- event-based resource accounting -----------------------------------
    # personnel_events[t]: signed personnel delta
    personnel_delta: list[int] = [0] * (horizon + 1)
    # spare_events[t]: {spare_id: signed delta}
    spare_delta: list[dict[str, int]] = [
        dict() for _ in range(horizon + 1)
    ]
    # active stages per tick
    active_at: list[list[Stage]] = [[] for _ in range(horizon)]
    # stages that stop a given component per tick
    stopped_at: list[set[str]] = [set() for _ in range(horizon)]
    stage_end: dict[str, int] = {}
    stage_by_id: dict[str, Stage] = {}

    for stage, start in assignments:
        if not isinstance(start, int) or start < 0:
            return _infeasible(
                problem,
                Violation(
                    time=max(start, 0) if isinstance(start, int) else 0,
                    kind="horizon",
                    detail=f"stage {stage.stage_id} has invalid start {start}",
                ),
            )
        end = start + stage.duration
        if end > horizon:
            return _infeasible(
                problem,
                Violation(
                    time=horizon,
                    kind="horizon",
                    detail=(
                        f"stage {stage.stage_id} runs past the horizon "
                        f"([{start}, {end}) > {horizon})"
                    ),
                    related={"stage_id": stage.stage_id, "start": start, "end": end},
                ),
            )
        stage_by_id[stage.stage_id] = stage
        stage_end[stage.stage_id] = end
        personnel_delta[start] += stage.personnel
        personnel_delta[end] -= stage.personnel
        for spare_id, amount in stage.spare_releases.items():
            spare_delta[start][spare_id] = (
                spare_delta[start].get(spare_id, 0) + amount
            )
        for spare_id, amount in stage.spare_needs.items():
            spare_delta[start][spare_id] = (
                spare_delta[start].get(spare_id, 0) - amount
            )
            spare_delta[end][spare_id] = spare_delta[end].get(spare_id, 0) + amount
        for t in range(start, end):
            active_at[t].append(stage)
            for cid in stage.stop_components:
                if cid in stopped_at[t]:
                    return _infeasible(
                        problem,
                        Violation(
                            time=t,
                            kind="min_capacity",
                            detail=(
                                f"component {cid} is forced offline by two "
                                f"overlapping stages at t={t}"
                            ),
                            related={"component_id": cid},
                        ),
                    )
                stopped_at[t].add(cid)

    # ---- precedence: stages of one component must stay ordered -------------
    for component in problem.components:
        ordered = sorted(
            (s for s in component.stages),
            key=lambda s: next(
                start for st, start in assignments if st.stage_id == s.stage_id
            ),
        )
        cursor = 0
        for stage in ordered:
            start = next(
                start for st, start in assignments if st.stage_id == stage.stage_id
            )
            if start < cursor:
                return _infeasible(
                    problem,
                    Violation(
                        time=start,
                        kind="precedence",
                        detail=(
                            f"stage {stage.stage_id} of component {component.component_id} "
                            f"starts at {start} before the previous stage ends at {cursor}"
                        ),
                        related={"stage_id": stage.stage_id},
                    ),
                )
            cursor = start + stage.duration

    # ---- completion times ---------------------------------------------------
    completion_time: dict[str, int] = {}
    for cid, last_stage_id in last_stage_of.items():
        if last_stage_id in stage_end:
            completion_time[cid] = stage_end[last_stage_id]

    # ---- sweep every tick ---------------------------------------------------
    capacity_trace: list[dict[str, float]] = []
    unmet_trace: list[dict[str, float]] = []
    total_unmet = {sid: 0.0 for sid in service_ids}
    segment_unmet: dict[str, dict[str, float]] = {
        stage.stage_id: {sid: 0.0 for sid in service_ids}
        for stage, _ in assignments
    }
    segment_personnel = {stage.stage_id: stage.personnel for stage, _ in assignments}

    personnel_used = 0
    spare_stock = dict(problem.initial_spares)
    violation: Violation | None = None

    for t in range(horizon):
        # resource deltas apply at the beginning of tick t
        personnel_used += personnel_delta[t]
        for spare_id, delta in spare_delta[t].items():
            spare_stock[spare_id] = spare_stock.get(spare_id, 0) + delta

        if personnel_used > problem.personnel_capacity:
            violation = Violation(
                time=t,
                kind="personnel",
                detail=(
                    f"personnel usage {personnel_used} exceeds capacity "
                    f"{problem.personnel_capacity} at t={t}"
                ),
                related={
                    "usage": personnel_used,
                    "capacity": problem.personnel_capacity,
                },
            )
            break
        short = [
            (sid, stock) for sid, stock in spare_stock.items() if stock < 0
        ]
        if short:
            sid, stock = sorted(short)[0]
            violation = Violation(
                time=t,
                kind="spare",
                detail=(
                    f"spare {sid} stock is {stock} (< 0) at t={t}"
                ),
                related={"spare_id": sid, "stock": stock},
            )
            break

        # a component is online unless it is still failed or forced offline
        available = {sid: 0.0 for sid in service_ids}
        for cid, component in components.items():
            if not component.stages:
                online = True  # healthy redundant unit, never failed
            elif cid in completion_time:
                online = t >= completion_time[cid]
            else:  # failed component not finished within this assignment
                online = False
            if cid in stopped_at[t]:
                online = False
            if online:
                for sid, amount in component.service_capacity.items():
                    available[sid] += amount

        unmet = {}
        for sid in service_ids:
            cap = available[sid]
            if cap < minimum[sid] - 1e-12:
                violation = Violation(
                    time=t,
                    kind="min_capacity",
                    detail=(
                        f"service {sid} available capacity {cap} is below its "
                        f"minimum {minimum[sid]} at t={t}"
                    ),
                    related={
                        "service_id": sid,
                        "available": cap,
                        "min_capacity": minimum[sid],
                    },
                )
                break
            gap = max(0.0, demand[sid] - cap)
            unmet[sid] = gap
            total_unmet[sid] += gap
            for stage in active_at[t]:
                segment_unmet[stage.stage_id][sid] += gap
        if violation is not None:
            break
        capacity_trace.append(available)
        unmet_trace.append(unmet)

    weighted = sum(total_unmet[s.service_id] * s.weight for s in services)

    if violation is None:
        makespan = max(completion_time.values()) if completion_time else 0
        return SimulationReport(
            feasible=True,
            violation=None,
            total_unmet=total_unmet,
            weighted_objective=weighted,
            segment_unmet=segment_unmet,
            segment_personnel=segment_personnel,
            completion_time=completion_time,
            makespan=makespan,
            capacity_trace=capacity_trace,
            unmet_trace=unmet_trace,
        )

    return _infeasible(problem, violation)


def _infeasible(problem: Problem, violation: Violation) -> SimulationReport:
    service_ids = [s.service_id for s in problem.services]
    return SimulationReport(
        feasible=False,
        violation=violation,
        total_unmet={sid: 0.0 for sid in service_ids},
        weighted_objective=float("inf"),
        segment_unmet={},
        segment_personnel={},
        completion_time={},
        makespan=problem.horizon,
        capacity_trace=[],
        unmet_trace=[],
    )
