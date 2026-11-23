"""Exhaustive time-indexed search for small fixtures.

The search alternates two kinds of moves at the current tick ``t``:

* **decision moves** start one eligible stage at ``t``; several stages can be
  started on the same tick (parallel work), and only stages whose id is larger
  than the last-started one are considered afterwards, which removes the
  permutation duplicates of the same parallel set;
* **advance move** closes tick ``t`` (checking every constraint and charging
  the unmet-demand cost) and moves to ``t + 1``.

Because every feasible decision sequence is enumerated, the result is an exact
optimum and :func:`first_failure` gives the earliest *unavoidable* constraint
violation rather than the failure of one particular greedy path.

The module is intentionally simple and only meant for small instances (the
test fixtures stay below twelve stages).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Failure, Problem, Stage
from .timeline import simulate


class StateLimitReached(RuntimeError):
    """Raised when exhaustive exploration exceeds its state budget."""


@dataclass
class _State:
    t: int
    starts: dict[str, int]
    ends: dict[str, int]
    personnel: list[int]
    spare_events: list[dict[str, int]]
    stop_count: list[dict[str, int]]
    completion: dict[str, int]
    cost: float
    started_this_tick: frozenset[str] = frozenset()


def _clone_state(state: _State) -> _State:
    return _State(
        t=state.t,
        starts=dict(state.starts),
        ends=dict(state.ends),
        personnel=list(state.personnel),
        spare_events=[dict(m) for m in state.spare_events],
        stop_count=[dict(m) for m in state.stop_count],
        completion=dict(state.completion),
        cost=state.cost,
        started_this_tick=state.started_this_tick,
    )


def _initial_state(problem: Problem) -> _State:
    return _State(
        t=0,
        starts={},
        ends={},
        personnel=[0] * (problem.horizon + 1),
        spare_events=[dict() for _ in range(problem.horizon + 1)],
        stop_count=[dict() for _ in range(problem.horizon)],
        completion={},
        cost=0.0,
    )


def _state_signature(state: _State) -> tuple:
    return (
        state.t,
        tuple(sorted(state.starts.items())),
        round(state.cost, 12),
    )


def _eligible(problem: Problem, state: _State) -> list[Stage]:
    out = []
    for component in problem.components:
        for idx, stage in enumerate(component.stages):
            if stage.stage_id in state.starts:
                continue
            if idx == 0 or component.stages[idx - 1].stage_id in state.starts:
                out.append(stage)
    return out


def _tick_resources_ok(
    problem: Problem, state: _State, t: int
) -> tuple[bool, str, str, dict]:
    personnel = sum(
        stage.personnel
        for sid, start in state.starts.items()
        if (stage := _stage_by_id(problem, sid)) is not None
        and start <= t < state.ends[sid]
    )
    if personnel > problem.personnel_capacity:
        return False, "personnel", (
            f"personnel usage {personnel} exceeds capacity "
            f"{problem.personnel_capacity} at t={t}"
        ), {"usage": personnel, "capacity": problem.personnel_capacity}

    stock = dict(problem.initial_spares)
    for u in range(t + 1):
        for sid, delta in state.spare_events[u].items():
            stock[sid] = stock.get(sid, 0) + delta
    for spare_id, amount in sorted(stock.items()):
        if amount < 0:
            return False, "spare", (
                f"spare {spare_id} stock is {amount} (< 0) at t={t}"
            ), {"spare_id": spare_id, "stock": amount}
    return True, "", "", {}


def _tick_capacity(
    problem: Problem, state: _State, t: int
) -> tuple[dict[str, float], dict[str, float]]:
    caps = {c.component_id: c.service_capacity for c in problem.components}
    available = {s.service_id: 0.0 for s in problem.services}
    stopped = state.stop_count[t]
    for component in problem.components:
        cid = component.component_id
        if not component.stages:
            online = True
        else:
            comp_end = state.completion.get(cid)
            online = comp_end is not None and t >= comp_end
        if not online or stopped.get(cid, 0) > 0:
            continue
        for sid, amount in caps[cid].items():
            available[sid] += amount
    unmet = {
        service.service_id: max(0.0, service.demand - available[service.service_id])
        for service in problem.services
    }
    return available, unmet


def _stage_by_id(problem: Problem, stage_id: str) -> Stage | None:
    for component in problem.components:
        for stage in component.stages:
            if stage.stage_id == stage_id:
                return stage
    return None  # pragma: no cover


def _can_start(problem: Problem, state: _State, stage: Stage, t: int) -> bool:
    end = t + stage.duration
    if end > problem.horizon:
        return False
    component = next(
        c for c in problem.components if c.component_id == stage.component_id
    )
    idx = component.stages.index(stage)
    if idx > 0:
        prev_end = state.ends.get(component.stages[idx - 1].stage_id)
        if prev_end is None or prev_end > t:
            return False
    for u in range(t, end):
        if state.personnel[u] + stage.personnel > problem.personnel_capacity:
            return False

    # spares: placed events on [0, t] then the candidate's own start events
    stock = dict(problem.initial_spares)
    for u in range(t + 1):
        for sid, delta in state.spare_events[u].items():
            stock[sid] = stock.get(sid, 0) + delta
    for sid, amount in stage.spare_releases.items():
        stock[sid] = stock.get(sid, 0) + amount
    for sid, amount in stage.spare_needs.items():
        stock[sid] = stock.get(sid, 0) - amount
    if any(v < 0 for v in stock.values()):
        return False
    for u in range(t + 1, end):
        for sid, delta in state.spare_events[u].items():
            stock[sid] = stock.get(sid, 0) + delta
        if any(v < 0 for v in stock.values()):
            return False

    tentative = _clone_state(state)
    _apply_start(problem, tentative, stage, t)
    caps = {c.component_id: c.service_capacity for c in problem.components}
    for u in range(t, end):
        available = {s.service_id: 0.0 for s in problem.services}
        for comp in problem.components:
            cid = comp.component_id
            if not comp.stages:
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
                return False
    return True


def _start_blocker(
    problem: Problem, state: _State, stage: Stage, t: int
) -> tuple[str, str, dict] | None:
    """Diagnostic counterpart of :func:`_can_start`: first violated rule."""
    end = t + stage.duration
    if end > problem.horizon:
        return (
            "horizon",
            (
                f"stage {stage.stage_id} needs {stage.duration} ticks but only "
                f"{problem.horizon - t} remain at t={t}"
            ),
            {"stage_id": stage.stage_id},
        )
    component = next(
        c for c in problem.components if c.component_id == stage.component_id
    )
    idx = component.stages.index(stage)
    if idx > 0:
        prev_end = state.ends.get(component.stages[idx - 1].stage_id)
        if prev_end is None or prev_end > t:
            return (
                "precedence",
                (
                    f"stage {stage.stage_id} cannot start before its predecessor "
                    f"finishes at {prev_end}"
                ),
                {"stage_id": stage.stage_id},
            )
    for u in range(t, end):
        if state.personnel[u] + stage.personnel > problem.personnel_capacity:
            return (
                "personnel",
                (
                    f"stage {stage.stage_id} would raise personnel usage to "
                    f"{state.personnel[u] + stage.personnel} above capacity "
                    f"{problem.personnel_capacity} at t={u}"
                ),
                {"stage_id": stage.stage_id, "time": u},
            )

    stock = dict(problem.initial_spares)
    for u in range(t + 1):
        for sid, delta in state.spare_events[u].items():
            stock[sid] = stock.get(sid, 0) + delta
    for sid, amount in stage.spare_releases.items():
        stock[sid] = stock.get(sid, 0) + amount
    for sid, amount in stage.spare_needs.items():
        stock[sid] = stock.get(sid, 0) - amount
    for u in range(t, end):
        for spare_id, amount_stock in sorted(stock.items()):
            if amount_stock < 0:
                return (
                    "spare",
                    (
                        f"stage {stage.stage_id} would drive spare {spare_id} "
                        f"stock to {amount_stock} at t={u}"
                    ),
                    {"stage_id": stage.stage_id, "spare_id": spare_id, "time": u},
                )
        if u + 1 < end:
            for sid, delta in state.spare_events[u + 1].items():
                stock[sid] = stock.get(sid, 0) + delta

    tentative = _clone_state(state)
    _apply_start(problem, tentative, stage, t)
    caps = {c.component_id: c.service_capacity for c in problem.components}
    for u in range(t, end):
        available = {s.service_id: 0.0 for s in problem.services}
        for comp in problem.components:
            cid = comp.component_id
            if not comp.stages:
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
                return (
                    "min_capacity",
                    (
                        f"starting stage {stage.stage_id} at t={t} would drop "
                        f"service {service.service_id} to "
                        f"{available[service.service_id]} below its minimum "
                        f"{service.min_capacity} at t={u}"
                    ),
                    {
                        "stage_id": stage.stage_id,
                        "service_id": service.service_id,
                        "time": u,
                        "available": available[service.service_id],
                        "min_capacity": service.min_capacity,
                    },
                )
    return None


def _apply_start(problem: Problem, state: _State, stage: Stage, t: int) -> None:
    end = t + stage.duration
    state.starts[stage.stage_id] = t
    state.ends[stage.stage_id] = end
    for u in range(t, end):
        state.personnel[u] += stage.personnel
        for cid in stage.stop_components:
            state.stop_count[u][cid] = state.stop_count[u].get(cid, 0) + 1
    for sid, amount in stage.spare_releases.items():
        state.spare_events[t][sid] = state.spare_events[t].get(sid, 0) + amount
    for sid, amount in stage.spare_needs.items():
        state.spare_events[t][sid] = state.spare_events[t].get(sid, 0) - amount
        state.spare_events[end][sid] = state.spare_events[end].get(sid, 0) + amount
    component = next(
        c for c in problem.components if c.component_id == stage.component_id
    )
    if stage is component.stages[-1]:
        state.completion[stage.component_id] = end
    state.started_this_tick = state.started_this_tick | {stage.stage_id}


def _settle_tick(problem: Problem, state: _State, t: int) -> tuple[bool, str, str, dict]:
    """Validate tick t and charge its unmet cost. Returns a violation tuple."""
    ok, kind, detail, related = _tick_resources_ok(problem, state, t)
    if not ok:
        return False, kind, detail, related
    available, unmet = _tick_capacity(problem, state, t)
    for service in problem.services:
        if available[service.service_id] + 1e-12 < service.min_capacity:
            return (
                False,
                "min_capacity",
                (
                    f"service {service.service_id} available capacity "
                    f"{available[service.service_id]} is below its minimum "
                    f"{service.min_capacity} at t={t}"
                ),
                {
                    "service_id": service.service_id,
                    "available": available[service.service_id],
                    "min_capacity": service.min_capacity,
                },
            )
    state.cost += sum(unmet[s.service_id] * s.weight for s in problem.services)
    return True, "", "", {}


@dataclass
class ExhaustiveResult:
    feasible: bool
    starts: dict[str, int] = field(default_factory=dict)
    weighted_objective: float = float("inf")
    makespan: int = 0
    states_visited: int = 0


def enumerate_best(
    problem: Problem, *, state_limit: int = 200_000
) -> ExhaustiveResult:
    """Return the exact optimum using exhaustive time-indexed DFS."""

    initial = _initial_state(problem)
    best = ExhaustiveResult(feasible=False, starts={})
    best_key: tuple | None = None
    counter = {"n": 0}
    total_stages = sum(len(c.stages) for c in problem.components)
    ordered_stages = [s for c in problem.components for s in c.stages]
    # Full state signature for memoization. Start times must be included:
    # the same set of started stages reaches the current tick with different
    # occupied intervals and different accumulated cost under different
    # histories. The tuple is sorted by stage id for a canonical ordering.
    expanded: set[tuple] = set()

    def key_of(starts: dict[str, int]) -> tuple:
        assignments = [(stage, starts[stage.stage_id]) for stage in ordered_stages]
        report = simulate(problem, assignments)
        assert report.feasible
        completion_seq = tuple(
            (end, cid) for cid, end in sorted(report.completion_time.items())
        )
        start_seq = tuple(starts[s.stage_id] for s in ordered_stages)
        return (
            round(report.weighted_objective, 12),
            report.makespan,
            completion_seq,
            start_seq,
        )

    def dfs(state: _State) -> None:
        nonlocal best_key
        counter["n"] += 1
        if counter["n"] > state_limit:
            raise StateLimitReached(f"exceeded {state_limit} explored states")

        signature = _state_signature(state)
        if signature in expanded:
            return
        expanded.add(signature)

        if state.t == problem.horizon:
            if len(state.starts) == total_stages:
                key = key_of(state.starts)
                if best_key is None or key < best_key:
                    best_key = key
                    best.feasible = True
                    best.starts = dict(state.starts)
                    best.weighted_objective = key[0]
                    best.makespan = key[1]
            return

        if best.feasible and round(state.cost, 12) > best.weighted_objective + 1e-9:
            return

        # decision moves: start one more eligible stage on this tick
        for stage in sorted(_eligible(problem, state), key=lambda s: s.stage_id):
            if stage.stage_id in state.started_this_tick:
                continue
            if _can_start(problem, state, stage, state.t):
                child = _clone_state(state)
                _apply_start(problem, child, stage, state.t)
                dfs(child)

        # advance move: settle this tick and move on
        nxt = _clone_state(state)
        ok, _kind, _detail, _related = _settle_tick(problem, nxt, state.t)
        if ok:
            nxt.t = state.t + 1
            nxt.started_this_tick = frozenset()
            dfs(nxt)

    dfs(initial)
    best.states_visited = counter["n"]
    return best


def first_failure(
    problem: Problem, *, state_limit: int = 200_000
) -> Failure:
    """Find the earliest tick on which every continuation breaks a constraint.

    Exact reachability analysis: a tick is only reported when no reachable
    state can satisfy it. Horizon failures (work that cannot finish in time)
    are detected from the remaining stage durations.
    """

    initial = _initial_state(problem)
    counter = {"n": 0}
    earliest: Failure | None = None
    # feasibility memo keyed by the full state signature
    feasible_memo: set[tuple] = set()
    infeasible_memo: set[tuple] = set()

    def record(t: int, kind: str, detail: str, related: dict) -> None:
        nonlocal earliest
        failure = Failure(
            time=t, kind=kind, detail=detail, exact=True, related=related
        )
        if earliest is None or (t, kind, detail) < (
            earliest.time,
            earliest.kind,
            earliest.detail,
        ):
            earliest = failure

    def dfs(state: _State) -> bool:
        """Return True when this state can be extended to a full schedule."""
        counter["n"] += 1
        if counter["n"] > state_limit:
            raise StateLimitReached(f"exceeded {state_limit} explored states")

        signature = _state_signature(state)
        if signature in feasible_memo:
            return True
        if signature in infeasible_memo:
            return False

        if state.t == problem.horizon:
            total = sum(len(c.stages) for c in problem.components)
            result = len(state.starts) == total
            (feasible_memo if result else infeasible_memo).add(signature)
            return result

        # remaining work includes the tails of stages already in flight:
        # work already performed on tick t is not available again.
        remaining_duration = 0
        for component in problem.components:
            for index, stage in enumerate(component.stages):
                if stage.stage_id in state.starts:
                    end = state.ends[stage.stage_id]
                    remaining_duration += max(0, end - state.t)
                else:
                    remaining_duration += sum(
                        s.duration for s in component.stages[index:]
                    )
                    break
        if remaining_duration > problem.horizon - state.t:
            record(
                problem.horizon,
                "horizon",
                (
                    f"remaining stage work of {remaining_duration} ticks cannot fit "
                    f"in the {problem.horizon - state.t} ticks left at t={state.t}"
                ),
                {
                    "remaining_duration": remaining_duration,
                    "ticks_left": problem.horizon - state.t,
                },
            )
            infeasible_memo.add(signature)
            return False

        can_continue = False
        for stage in sorted(_eligible(problem, state), key=lambda s: s.stage_id):
            if stage.stage_id in state.started_this_tick:
                continue
            if _can_start(problem, state, stage, state.t):
                child = _clone_state(state)
                _apply_start(problem, child, stage, state.t)
                if dfs(child):
                    can_continue = True
            else:
                # record the concrete reason this stage cannot start now;
                # another branch may still survive, so do not return yet.
                blocker = _start_blocker(problem, state, stage, state.t)
                if blocker is not None:
                    record(state.t, blocker[0], blocker[1], blocker[2])

        nxt = _clone_state(state)
        ok, kind, detail, related = _settle_tick(problem, nxt, state.t)
        if ok:
            nxt.t = state.t + 1
            nxt.started_this_tick = frozenset()
            if dfs(nxt):
                can_continue = True
        else:
            record(state.t, kind, detail, related)
        (feasible_memo if can_continue else infeasible_memo).add(signature)
        return can_continue

    feasible = dfs(initial)
    if feasible:
        raise ValueError("instance is feasible; no failure to report")
    if earliest is None:
        earliest = Failure(
            time=problem.horizon,
            kind="horizon",
            detail="not all stages can be completed within the finite horizon",
            exact=True,
        )
    return earliest
