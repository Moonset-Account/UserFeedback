"""Constructive fallback diagnostic for instances too large to exhaustively
search. It mimics the solver's greedy construction and reports the first
constraint that blocks every stage currently eligible, with ``exact=False``.
"""

from __future__ import annotations

from .brute_force import _can_start, _clone_state, _eligible, _initial_state, _tick_capacity, _tick_resources_ok
from .model import Failure, Problem, Stage


def greedy_diagnose(problem: Problem) -> Failure:
    state = _initial_state(problem)
    total_stages = sum(len(c.stages) for c in problem.components)

    while state.t < problem.horizon and len(state.starts) < total_stages:
        # check the tick we are about to leave behind
        if state.t > 0:
            t = state.t - 1
            ok, kind, detail, related = _tick_resources_ok(problem, state, t)
            if not ok:
                return Failure(time=t, kind=kind, detail=detail, exact=False, related=related)
            available, _ = _tick_capacity(problem, state, t)
            for service in problem.services:
                if available[service.service_id] + 1e-12 < service.min_capacity:
                    return Failure(
                        time=t,
                        kind="min_capacity",
                        detail=(
                            f"service {service.service_id} available capacity "
                            f"{available[service.service_id]} is below its minimum "
                            f"{service.min_capacity} at t={t}"
                        ),
                        exact=False,
                        related={
                            "service_id": service.service_id,
                            "available": available[service.service_id],
                            "min_capacity": service.min_capacity,
                        },
                    )

        eligible = _eligible(problem, state)
        if not eligible:
            break  # precedence deadlock, structurally prevented by validation

        progressed = False
        rejection: Failure | None = None
        for stage in sorted(eligible, key=lambda s: s.stage_id):
            start = state.t
            found = False
            while start + stage.duration <= problem.horizon:
                if _can_start(problem, state, stage, start):
                    _apply_local(problem, state, stage, start)
                    state.t = start + stage.duration
                    progressed = True
                    found = True
                    break
                start += 1
            if found:
                break
            rejection = _first_blocker(problem, state, stage, state.t)
        if not progressed:
            if rejection is not None:
                return rejection
            break

    if len(state.starts) == total_stages:
        # all stages placed; verify remaining idle ticks for min capacity
        for t in range(max(state.t - 1, 0), problem.horizon):
            available, _ = _tick_capacity(problem, state, t)
            for service in problem.services:
                if available[service.service_id] + 1e-12 < service.min_capacity:
                    return Failure(
                        time=t,
                        kind="min_capacity",
                        detail=(
                            f"service {service.service_id} available capacity "
                            f"{available[service.service_id]} is below its minimum "
                            f"{service.min_capacity} at t={t}"
                        ),
                        exact=False,
                    )

    return Failure(
        time=min(state.t, problem.horizon),
        kind="horizon",
        detail=(
            f"only {len(state.starts)} of {total_stages} stages can be placed "
            f"inside the horizon {problem.horizon}"
        ),
        exact=False,
        related={"placed": len(state.starts), "total": total_stages},
    )


def _apply_local(problem: Problem, state, stage: Stage, t: int) -> None:
    # reuse brute force's mutator via a throwaway clone path is wasteful;
    # replicate the small state transition directly.
    from .brute_force import _apply_start

    _apply_start(problem, state, stage, t)


def _first_blocker(problem: Problem, state, stage: Stage, t: int) -> Failure:
    end = t + stage.duration
    if end > problem.horizon:
        return Failure(
            time=problem.horizon,
            kind="horizon",
            detail=(
                f"stage {stage.stage_id} needs {stage.duration} ticks but only "
                f"{problem.horizon - t} remain at t={t}"
            ),
            exact=False,
            related={"stage_id": stage.stage_id},
        )
    if stage.personnel > problem.personnel_capacity:
        return Failure(
            time=t,
            kind="personnel",
            detail=(
                f"stage {stage.stage_id} needs {stage.personnel} personnel but "
                f"capacity is {problem.personnel_capacity}"
            ),
            exact=False,
            related={"stage_id": stage.stage_id},
        )
    stock = dict(problem.initial_spares)
    for u in range(t + 1):
        for sid, delta in state.spare_events[u].items():
            stock[sid] = stock.get(sid, 0) + delta
    for sid, amount in stage.spare_releases.items():
        stock[sid] = stock.get(sid, 0) + amount
    for sid, amount in stage.spare_needs.items():
        stock[sid] = stock.get(sid, 0) - amount
    for sid, amount in sorted(stock.items()):
        if amount < 0:
            return Failure(
                time=t,
                kind="spare",
                detail=(
                    f"stage {stage.stage_id} cannot start: spare {sid} stock "
                    f"would be {amount} at t={t}"
                ),
                exact=False,
                related={"stage_id": stage.stage_id, "spare_id": sid, "stock": amount},
            )
    tentative = _clone_state(state, problem.horizon)
    from .brute_force import _apply_start

    _apply_start(problem, tentative, stage, t)
    caps = {c.component_id: c.service_capacity for c in problem.components}
    for u in range(t, min(end, problem.horizon)):
        available = {s.service_id: 0.0 for s in problem.services}
        for component in problem.components:
            cid = component.component_id
            if not component.stages:
                online = True
            else:
                comp_end = tentative.completion.get(cid)
                online = comp_end is not None and u >= comp_end
            if not online or tentative.stop_count[u].get(cid, 0) > 0:
                continue
            for sid, amount in caps[cid].items():
                available[sid] += amount
        for service in problem.services:
            if available[service.service_id] + 1e-12 < service.min_capacity:
                return Failure(
                    time=u,
                    kind="min_capacity",
                    detail=(
                        f"starting stage {stage.stage_id} at t={t} would push "
                        f"service {service.service_id} to {available[service.service_id]} "
                        f"(minimum {service.min_capacity}) at t={u}"
                    ),
                    exact=False,
                    related={
                        "stage_id": stage.stage_id,
                        "service_id": service.service_id,
                        "available": available[service.service_id],
                        "min_capacity": service.min_capacity,
                    },
                )
    return Failure(
        time=t,
        kind="horizon",
        detail=f"stage {stage.stage_id} has no feasible start from t={t}",
        exact=False,
        related={"stage_id": stage.stage_id},
    )
