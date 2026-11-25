from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .model import CeremonySpec, load_spec
from .solver import (
    Boundary,
    TransitionPlan,
    build_boundary,
    build_independent_subsets,
    describe_transition,
    enumerate_round_assignments,
    transition_cost,
)


@dataclass
class ScheduledRound:
    index: int
    round_id: str
    assignments: dict[str, str]
    enabled_keys: tuple[str, ...]
    option_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "round_id": self.round_id,
            "assignments": dict(self.assignments),
            "enabled_keys": list(self.enabled_keys),
        }


@dataclass
class ScheduleResult:
    feasible: bool
    minimum_changes: int | None = None
    rounds: list[ScheduledRound] = field(default_factory=list)
    transitions: list[TransitionPlan] = field(default_factory=list)
    first_unreachable_round: dict[str, Any] | None = None
    local_infeasible_round: dict[str, Any] | None = None
    conflict_pairs: tuple[tuple[str, str], ...] = ()
    legal_assignment_counts: tuple[int, ...] = ()
    total_handovers: int = 0
    options: dict[str, Any] = field(default_factory=dict)
    spec: CeremonySpec | None = field(default=None, repr=False)

    @property
    def handovers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for transition in self.transitions:
            for handover in transition.handovers:
                item = handover.to_dict()
                item["after_round_index"] = transition.from_round_index
                item["after_round_id"] = transition.from_round_id
                item["before_round_index"] = transition.to_round_index
                item["before_round_id"] = transition.to_round_id
                result.append(item)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "minimum_changes": self.minimum_changes,
            "total_handovers": self.total_handovers,
            "rounds": [r.to_dict() for r in self.rounds],
            "transitions": [t.to_dict() for t in self.transitions],
            "handovers": self.handovers,
            "enabled_keys_by_round": [list(r.enabled_keys) for r in self.rounds],
            "first_unreachable_round": self.first_unreachable_round,
            "local_infeasible_round": self.local_infeasible_round,
            "conflict_pairs": [list(pair) for pair in self.conflict_pairs],
            "legal_assignment_counts": list(self.legal_assignment_counts),
            "options": dict(self.options),
        }


def schedule_ceremony(
    specification: CeremonySpec | Mapping[str, Any] | str | bytes | Path,
    *,
    handover_trigger: str = "both_seats",
) -> ScheduleResult:
    """Compute an exact minimum-change schedule.

    Parameters
    ----------
    specification:
        Parsed :class:`CeremonySpec`, a mapping, JSON text, or a JSON path.
    handover_trigger:
        ``"both_seats"`` (default) triggers a handover when both fixed seats
        changed occupants.  ``"disjoint_crew"`` triggers only when the old and
        new two-person crews have no person in common.
    """

    if handover_trigger not in {"both_seats", "disjoint_crew"}:
        raise ValueError("handover_trigger must be 'both_seats' or 'disjoint_crew'")

    if isinstance(specification, CeremonySpec):
        spec = specification
    else:
        spec = load_spec(specification)

    options = {"handover_trigger": handover_trigger}
    independent = build_independent_subsets(spec)

    if not spec.rounds:
        return ScheduleResult(
            feasible=True,
            minimum_changes=0,
            conflict_pairs=spec.conflict_pairs,
            options=options,
            spec=spec,
        )

    all_assignments: list[list[tuple[int, ...]]] = []
    counts: list[int] = []
    for round_ in spec.rounds:
        assignments = enumerate_round_assignments(spec, round_)
        counts.append(len(assignments))
        all_assignments.append(assignments)
        if not assignments:
            local_info = {
                "index": round_.index,
                "round_id": round_.id,
                "locally_assignable": False,
                "legal_assignment_count": 0,
                "message": (
                    "round has no legal assignment after availability, "
                    "eligibility, one-slot-per-person, and independence checks"
                ),
            }
            return ScheduleResult(
                feasible=False,
                local_infeasible_round=local_info,
                conflict_pairs=spec.conflict_pairs,
                legal_assignment_counts=tuple(counts),
                options=options,
                spec=spec,
            )

    # Forward DP.  dp[r][j] is the minimum number of seat changes needed to
    # reach legal assignment j of round r.
    dp_by_round: list[list[int | None]] = [[0] * len(all_assignments[0])]
    parents_by_round: list[list[int | None]] = [[None] * len(all_assignments[0])]
    boundaries: dict[int, Boundary] = {}

    for to_index in range(1, len(spec.rounds)):
        from_index = to_index - 1
        boundary = build_boundary(spec, from_index, to_index)
        boundaries[to_index] = boundary
        old_assignments = all_assignments[from_index]
        new_assignments = all_assignments[to_index]
        old_dp = dp_by_round[-1]

        reachable_old = [i for i, cost in enumerate(old_dp) if cost is not None]
        min_old_cost = min(old_dp[i] for i in reachable_old)
        old_round = spec.rounds[from_index]
        new_round = spec.rounds[to_index]
        same_slot_order = old_round.slot_ids == new_round.slot_ids
        old_tuple_index = {values: i for i, values in enumerate(old_assignments)}
        old_identity = (
            None
            if same_slot_order
            else {
                _identity(old_round, values): i
                for i, values in enumerate(old_assignments)
            }
        )
        order: list[int] | None = None

        new_dp: list[int | None] = [None] * len(new_assignments)
        parents: list[int | None] = [None] * len(new_assignments)

        for new_index, new_values in enumerate(new_assignments):
            best_cost: int | None = None
            best_parent: int | None = None

            if same_slot_order:
                same = old_tuple_index.get(new_values)
            else:
                assert old_identity is not None
                same = old_identity.get(_identity(new_round, new_values))
            if same is not None and old_dp[same] is not None:
                preliminary = transition_cost(
                    boundary,
                    old_assignments[same],
                    new_values,
                    independent,
                    handover_trigger,
                )
                if preliminary is not None:
                    best_cost = old_dp[same] + preliminary  # type: ignore[operator]
                    best_parent = same

            # A predecessor at min_old_cost with a zero-step transition is the
            # theoretical lower bound, so no pairwise scan is needed.
            if best_cost != min_old_cost:
                if order is None:
                    order = sorted(
                        reachable_old,
                        key=lambda i: (old_dp[i], old_assignments[i]),  # type: ignore[arg-type]
                    )
                for old_index in order:
                    base_cost = old_dp[old_index]
                    assert base_cost is not None
                    if best_cost is not None and base_cost >= best_cost:
                        break
                    step_cost = transition_cost(
                        boundary,
                        old_assignments[old_index],
                        new_values,
                        independent,
                        handover_trigger,
                    )
                    if step_cost is None:
                        continue
                    candidate = base_cost + step_cost
                    if best_cost is None or candidate < best_cost:
                        best_cost = candidate
                        best_parent = old_index
                        # No remaining predecessor can beat the smallest old DP
                        # value plus a non-negative transition cost.
                        if best_cost == min_old_cost:
                            break

            new_dp[new_index] = best_cost
            parents[new_index] = best_parent

        if all(cost is None for cost in new_dp):
            unreachable = {
                "index": to_index,
                "round_id": spec.rounds[to_index].id,
                "after_round_index": from_index,
                "after_round_id": spec.rounds[from_index].id,
                "locally_assignable": True,
                "legal_assignment_count": len(new_assignments),
                "reachable_previous_assignments": len(reachable_old),
                "message": (
                    "round has legal standalone assignments, but none is "
                    "reachable through a legal continuity/handover transition"
                ),
            }
            return ScheduleResult(
                feasible=False,
                first_unreachable_round=unreachable,
                conflict_pairs=spec.conflict_pairs,
                legal_assignment_counts=tuple(counts),
                options=options,
                spec=spec,
            )

        dp_by_round.append(new_dp)
        parents_by_round.append(parents)

    final_dp = dp_by_round[-1]
    final_index = min(
        (i for i, cost in enumerate(final_dp) if cost is not None),
        key=lambda i: (final_dp[i], all_assignments[-1][i]),  # type: ignore[arg-type]
    )
    minimum_changes = final_dp[final_index]

    chosen_indices: list[int] = [0] * len(spec.rounds)
    chosen_indices[-1] = final_index
    for round_index in range(len(spec.rounds) - 1, 0, -1):
        parent = parents_by_round[round_index][chosen_indices[round_index]]
        if parent is None:  # defensive; a feasible final state has a parent chain
            raise RuntimeError("internal solver error: missing DP parent")
        chosen_indices[round_index - 1] = parent

    scheduled_rounds: list[ScheduledRound] = []
    for round_index, option_index in enumerate(chosen_indices):
        round_ = spec.rounds[round_index]
        values = all_assignments[round_index][option_index]
        assignments = {
            slot_id: spec.people[person_index].id
            for slot_id, person_index in zip(round_.slot_ids, values)
        }
        enabled: list[str] = []
        for key_id, (slot_a, slot_b) in spec.key_slot_ids.items():
            if slot_a in round_.slot_index and slot_b in round_.slot_index:
                pa = values[round_.slot_index[slot_a]]
                pb = values[round_.slot_index[slot_b]]
                if spec.conflict_masks[pa] & (1 << pb) == 0:
                    enabled.append(key_id)
        scheduled_rounds.append(
            ScheduledRound(
                index=round_index,
                round_id=round_.id,
                assignments=assignments,
                enabled_keys=tuple(sorted(enabled)),
                option_index=option_index,
            )
        )

    transitions: list[TransitionPlan] = []
    total_handovers = 0
    for to_index in range(1, len(spec.rounds)):
        boundary = boundaries[to_index]
        old_values = all_assignments[to_index - 1][chosen_indices[to_index - 1]]
        new_values = all_assignments[to_index][chosen_indices[to_index]]
        transition = describe_transition(
            spec,
            boundary,
            old_values,
            new_values,
            independent,
            handover_trigger,
        )
        transitions.append(transition)
        total_handovers += len(transition.handovers)

    return ScheduleResult(
        feasible=True,
        minimum_changes=minimum_changes,
        rounds=scheduled_rounds,
        transitions=transitions,
        conflict_pairs=spec.conflict_pairs,
        legal_assignment_counts=tuple(len(a) for a in all_assignments),
        total_handovers=total_handovers,
        options=options,
        spec=spec,
    )


def schedule_from_dict(data: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return schedule_ceremony(data, **kwargs).to_dict()


def schedule_from_json(text: str | bytes, **kwargs: Any) -> dict[str, Any]:
    return schedule_ceremony(load_spec(text), **kwargs).to_dict()


def find_first_unreachable_round(
    specification: CeremonySpec | Mapping[str, Any] | str | bytes | Path,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Return the first unreachable-round diagnosis, or ``None`` if feasible."""
    return schedule_ceremony(specification, **kwargs).first_unreachable_round


def minimum_change_schedule(
    specification: CeremonySpec | Mapping[str, Any] | str | bytes | Path,
    **kwargs: Any,
) -> ScheduleResult:
    """Descriptive alias for :func:`schedule_ceremony`."""
    return schedule_ceremony(specification, **kwargs)


def _identity(round_: Any, values: tuple[int, ...]) -> tuple[tuple[str, int], ...]:
    # Local import avoids making this helper public in solver.py by accident.
    from .solver import assignment_identity

    return assignment_identity(round_, values)
