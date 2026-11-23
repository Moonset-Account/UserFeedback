"""Branch-and-bound repair scheduler.

The search enumerates precedence-feasible stage orderings. At every node each
eligible stage is tried at its *earliest* feasible start: deliberately idling
a renewable resource can never create capacity, spare stock or component
availability that does not already exist, so an optimal schedule exists in
which every stage starts at its earliest feasible time given the stages ahead
of it.

Costs are evaluated on every unit tick. Capacity of a repaired component is
only credited from the completion tick of its *last* stage onward; future
restoration is never counted in advance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Problem, ScheduleResult, Segment, Stage
from .timeline import simulate
from .validation import validate_problem

_EPS = 1e-9


@dataclass
class _Builder:
    problem: Problem
    stages: list[Stage]
    stage_by_id: dict[str, Stage]
    horizon: int

    # mutable search state ---------------------------------------------------
    starts: dict[str, int] = field(default_factory=dict)
    ends: dict[str, int] = field(default_factory=dict)
    personnel_used: list[int] = field(default_factory=list)
    spare_events: list[dict[str, int]] = field(default_factory=list)
    stop_count: list[dict[str, int]] = field(default_factory=list)
    completion: dict[str, int] = field(default_factory=dict)
    service_ids: list[str] = field(default_factory=list)
    minimum: dict[str, float] = field(default_factory=dict)
    demand: dict[str, float] = field(default_factory=dict)
    weight: dict[str, float] = field(default_factory=dict)
    cap_of: dict[str, dict[str, float]] = field(default_factory=dict)
    healthy_capacity: dict[str, dict[str, float]] = field(default_factory=dict)
    initial_spares: dict[str, int] = field(default_factory=dict)
    nodes: int = 0

    @classmethod
    def create(cls, problem: Problem) -> "_Builder":
        stages = [stage for c in problem.components for stage in c.stages]
        stage_by_id = {s.stage_id: s for s in stages}

        service_ids = [s.service_id for s in problem.services]
        builder = cls(
            problem=problem,
            stages=stages,
            stage_by_id=stage_by_id,
            horizon=problem.horizon,
            personnel_used=[0] * (problem.horizon + 1),
            spare_events=[dict() for _ in range(problem.horizon + 1)],
            stop_count=[dict() for _ in range(problem.horizon)],
            service_ids=service_ids,
            minimum={s.service_id: s.min_capacity for s in problem.services},
            demand={s.service_id: s.demand for s in problem.services},
            weight={s.service_id: s.weight for s in problem.services},
            cap_of={c.component_id: dict(c.service_capacity) for c in problem.components},
            healthy_capacity={
                c.component_id: dict(c.service_capacity)
                for c in problem.components
                if not c.stages
            },
            initial_spares=dict(problem.initial_spares),
        )
        return builder

    # ------------------------------------------------------------------
    def predecessors_done(self, stage: Stage) -> bool:
        component = next(
            c for c in self.problem.components if c.component_id == stage.component_id
        )
        idx = component.stages.index(stage)
        if idx == 0:
            return True
        return component.stages[idx - 1].stage_id in self.starts

    def earliest_start(self, stage: Stage) -> int:
        component = next(
            c for c in self.problem.components if c.component_id == stage.component_id
        )
        idx = component.stages.index(stage)
        if idx == 0:
            return 0
        return self.ends[component.stages[idx - 1].stage_id]

    def eligible_stages(self) -> list[Stage]:
        return [s for s in self.stages if s.stage_id not in self.starts and self.predecessors_done(s)]

    def is_feasible(self, stage: Stage, start: int) -> bool:
        end = start + stage.duration
        if end > self.horizon:
            return False
        if stage.personnel > 0:
            for t in range(start, end):
                if self.personnel_used[t] + stage.personnel > self.problem.personnel_capacity:
                    return False
        # spare stock at each tick. Initial stock plus placed events in
        # [0, start] (events at `start` belong to previously placed stages
        # only; the candidate is not in spare_events yet), then the
        # candidate's own start events with releases applied before needs.
        stock = dict(self.initial_spares)
        for t in range(0, start + 1):
            for sid, delta in self.spare_events[t].items():
                stock[sid] = stock.get(sid, 0) + delta
        for sid, amount in stage.spare_releases.items():
            stock[sid] = stock.get(sid, 0) + amount
        for sid, amount in stage.spare_needs.items():
            stock[sid] = stock.get(sid, 0) - amount
        if any(v < 0 for v in stock.values()):
            return False
        for t in range(start + 1, end):
            for sid, delta in self.spare_events[t].items():
                stock[sid] = stock.get(sid, 0) + delta
            if any(v < 0 for v in stock.values()):
                return False
        # capacity: failed component stays offline; stopped components offline
        stopped = stage.stop_components
        caps = self.cap_of
        minimum = self.minimum
        stop_count = self.stop_count
        completion = self.completion
        for t in range(start, end):
            available = {sid: 0.0 for sid in self.service_ids}
            # healthy components without repair stages are always online
            for cid, caps_c in self.healthy_capacity.items():
                if stop_count[t].get(cid, 0) > 0 or cid in stopped:
                    continue
                for sid, amount in caps_c.items():
                    available[sid] += amount
            # only components whose *last* stage already completed are online;
            # a component under repair contributes nothing until then.
            for cid, comp_end in completion.items():
                if t < comp_end:
                    continue
                if stop_count[t].get(cid, 0) > 0 or cid in stopped:
                    continue
                for sid, amount in caps[cid].items():
                    available[sid] += amount
            for sid in self.service_ids:
                if available[sid] + _EPS < minimum[sid]:
                    return False
        return True

    def place(self, stage: Stage, start: int) -> None:
        end = start + stage.duration
        self.starts[stage.stage_id] = start
        self.ends[stage.stage_id] = end
        for t in range(start, end):
            self.personnel_used[t] += stage.personnel
            for cid in stage.stop_components:
                self.stop_count[t][cid] = self.stop_count[t].get(cid, 0) + 1
        for sid, amount in stage.spare_releases.items():
            self.spare_events[start][sid] = self.spare_events[start].get(sid, 0) + amount
        for sid, amount in stage.spare_needs.items():
            self.spare_events[start][sid] = self.spare_events[start].get(sid, 0) - amount
            self.spare_events[end][sid] = self.spare_events[end].get(sid, 0) + amount
        component = next(
            c for c in self.problem.components if c.component_id == stage.component_id
        )
        if stage is component.stages[-1]:
            self.completion[stage.component_id] = end

    def remove(self, stage: Stage) -> None:
        start = self.starts.pop(stage.stage_id)
        end = self.ends.pop(stage.stage_id)
        for t in range(start, end):
            self.personnel_used[t] -= stage.personnel
            for cid in stage.stop_components:
                self.stop_count[t][cid] -= 1
                if self.stop_count[t][cid] == 0:
                    del self.stop_count[t][cid]
        for sid, amount in stage.spare_releases.items():
            value = self.spare_events[start].get(sid, 0) - amount
            if value:
                self.spare_events[start][sid] = value
            else:
                self.spare_events[start].pop(sid, None)
        for sid, amount in stage.spare_needs.items():
            value = self.spare_events[start].get(sid, 0) + amount
            if value:
                self.spare_events[start][sid] = value
            else:
                self.spare_events[start].pop(sid, None)
            end_value = self.spare_events[end].get(sid, 0) - amount
            if end_value:
                self.spare_events[end][sid] = end_value
            else:
                self.spare_events[end].pop(sid, None)
        component = next(
            c for c in self.problem.components if c.component_id == stage.component_id
        )
        if stage is component.stages[-1]:
            del self.completion[stage.component_id]

    # ------------------------------------------------------------------
    def committed_cost(self, upto: int) -> float:
        """Cost on [0, upto) under the current partial schedule."""
        total = 0.0
        caps = self.cap_of
        stop_count = self.stop_count
        completion = self.completion
        for t in range(upto):
            available = {sid: 0.0 for sid in self.service_ids}
            for cid, caps_c in self.healthy_capacity.items():
                if stop_count[t].get(cid, 0) > 0:
                    continue
                for sid, amount in caps_c.items():
                    available[sid] += amount
            for cid, comp_end in completion.items():
                if t >= comp_end and stop_count[t].get(cid, 0) == 0:
                    for sid, amount in caps[cid].items():
                        available[sid] += amount
            for sid in self.service_ids:
                gap = self.demand[sid] - available[sid]
                if gap > 0:
                    total += gap * self.weight[sid]
        return total


def solve(problem: Problem, *, brute_force_limit: int = 200_000) -> ScheduleResult:
    """Find the lexicographically optimal repair schedule.

    Objective order:
      1. minimum weighted integrated unmet demand over the horizon;
      2. minimum time at which all repairs are complete;
      3. lexicographically smallest sequence of component completion times
         ordered by component id;
      4. lexicographically smallest sequence of stage start times ordered by
         stage id (final deterministic tie break).
    """

    validate_problem(problem)
    builder = _Builder.create(problem)

    incumbent: dict[str, int] | None = None
    incumbent_key: tuple | None = None

    def leaf_key(starts: dict[str, int]) -> tuple:
        assignments = [(builder.stage_by_id[sid], st) for sid, st in starts.items()]
        report = simulate(problem, assignments)
        if not report.feasible:
            # Should be unreachable: the builder's own checks already enforce
            # every constraint. Keep the guard for safety.
            raise RuntimeError("internal error: infeasible leaf reached")  # pragma: no cover
        completion_seq = tuple(
            (end, cid) for cid, end in sorted(report.completion_time.items())
        )
        start_seq = tuple(starts[s.stage_id] for s in builder.stages)
        return (
            round(report.weighted_objective, 12),
            report.makespan,
            completion_seq,
            start_seq,
        ), report

    def dfs() -> None:
        builder.nodes += 1
        eligible = builder.eligible_stages()
        if not eligible:
            if len(builder.starts) == len(builder.stages):
                key, _ = leaf_key(builder.starts)
                nonlocal incumbent, incumbent_key
                if incumbent_key is None or key < incumbent_key:
                    incumbent_key = key
                    incumbent = dict(builder.starts)
            return

        # lower bound on the primary objective: cost already locked in up to
        # the earliest possible next start. Unscheduled stages cannot restore
        # any capacity before that tick, so this cannot overestimate.
        earliest = min(builder.earliest_start(s) for s in eligible)
        lb = builder.committed_cost(earliest)
        if incumbent_key is not None and round(lb, 12) > incumbent_key[0] + _EPS:
            return

        ordered = sorted(eligible, key=lambda s: s.stage_id)
        for stage in ordered:
            start = builder.earliest_start(stage)
            while start + stage.duration <= problem.horizon:
                if builder.is_feasible(stage, start):
                    builder.place(stage, start)
                    dfs()
                    builder.remove(stage)
                    break  # earliest feasible start only; see module docstring
                start += 1
            # if no feasible start exists on this branch, another branch that
            # orders the stages differently may still succeed.

    # greedy seed gives an incumbent quickly and tightens pruning
    seed = _greedy_seed(builder)
    if seed is not None:
        incumbent = dict(seed)
        key, _ = leaf_key(incumbent)
        incumbent_key = key

    dfs()

    if incumbent is None:
        return _diagnose(problem, brute_force_limit)

    return _build_result(problem, builder, incumbent)


def _greedy_seed(builder: _Builder) -> dict[str, int] | None:
    """Construct one feasible schedule greedily as an initial incumbent."""
    progress = True
    while progress and len(builder.starts) < len(builder.stages):
        progress = False
        for stage in sorted(builder.eligible_stages(), key=lambda s: s.stage_id):
            start = builder.earliest_start(stage)
            while start + stage.duration <= builder.horizon:
                if builder.is_feasible(stage, start):
                    builder.place(stage, start)
                    progress = True
                    break
                start += 1
            if progress:
                break
    if len(builder.starts) == len(builder.stages):
        seed = dict(builder.starts)
        for stage in list(builder.stages):
            if stage.stage_id in builder.starts:
                builder.remove(stage)
        return seed
    # roll back partial placement
    for stage in list(builder.stages):
        if stage.stage_id in builder.starts:
            builder.remove(stage)
    return None


def _build_result(
    problem: Problem, builder: _Builder, starts: dict[str, int]
) -> ScheduleResult:
    assignments = [
        (builder.stage_by_id[sid], st) for sid, st in starts.items()
    ]
    report = simulate(problem, assignments)
    if not report.feasible:  # pragma: no cover - defensive
        raise RuntimeError("internal error: final schedule failed simulation")

    segments: list[Segment] = []
    for stage, start in sorted(assignments, key=lambda pair: (pair[1], pair[0].stage_id)):
        segments.append(
            Segment(
                stage_id=stage.stage_id,
                component_id=stage.component_id,
                start=start,
                end=start + stage.duration,
                personnel=stage.personnel,
                spare_needs=dict(stage.spare_needs),
                spare_releases=dict(stage.spare_releases),
                stopped_components=tuple(stage.stop_components),
                unmet_demand=dict(report.segment_unmet[stage.stage_id]),
            )
        )

    completion_order = sorted(
        (end, cid)
        for cid, end in report.completion_time.items()
    )
    return ScheduleResult(
        feasible=True,
        segments=segments,
        total_unmet=dict(report.total_unmet),
        weighted_objective=report.weighted_objective,
        makespan=report.makespan,
        completion_order=completion_order,
        failure=None,
    )


def _diagnose(problem: Problem, brute_force_limit: int) -> ScheduleResult:
    from .brute_force import first_failure, StateLimitReached
    from .diagnose import greedy_diagnose

    try:
        failure = first_failure(problem, state_limit=brute_force_limit)
    except StateLimitReached:
        failure = greedy_diagnose(problem)
    return ScheduleResult(feasible=False, failure=failure)
