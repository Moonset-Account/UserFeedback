"""Exact retention planner.

Objective (lexicographic, in this order):

1. Maximise the number of provable cases in the highest priority tier.
2. Then maximise it in the next tier, and so on.
3. Then minimise the total retained occupancy.
4. Then pick a deterministic lexicographic tie-break over the evidence.

A source is charged once even when several receipt copies from it are used, so
the cross-case search keeps the set of selected original sources as its state.
A source selected for one case is free for every other case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .model import (
    CaseSpec,
    EvidenceChoice,
    EvidenceRequirement,
    NormalisedModel,
    RetentionCapacityError,
    RetentionInputError,
)


# ---------------------------------------------------------------------------
# Per-case evidence combinations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Combo:
    """One feasible way to prove a single case."""

    sources: frozenset[str]
    choices: tuple[EvidenceChoice, ...]  # in declared requirement order

    @property
    def plan_key(self) -> tuple[tuple[str, str], ...]:
        return tuple((ch.role, ch.log_id) for ch in self.choices)


def _schedule(
    requirements: tuple[EvidenceRequirement, ...],
) -> tuple[str, ...]:
    """Schedule roles so an ``after`` role's reference is assigned first."""
    by_role = {req.role: req for req in requirements}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(role: str) -> None:
        mark = state.get(role, 0)
        if mark == 2:
            return
        if mark == 1:  # pragma: no cover - cycles rejected during normalisation
            raise RetentionInputError(f"cyclic evidence ordering at role {role!r}")
        state[role] = 1
        req = by_role[role]
        if req.after is not None:
            visit(req.after)
        state[role] = 2
        order.append(role)

    for req in requirements:
        visit(req.role)
    return tuple(order)


def _constraints_hold(
    model: NormalisedModel,
    requirements: tuple[EvidenceRequirement, ...],
    assigned: Mapping[str, str],
) -> bool:
    for req in requirements:
        log = model.logs[assigned[req.role]]
        if req.after is not None:
            ref = model.logs[assigned[req.after]]
            if req.strict_after:
                if not log.timestamp > ref.timestamp:
                    return False
            elif not log.timestamp >= ref.timestamp:
                return False
        if req.distinct_source_from is not None:
            ref = model.logs[assigned[req.distinct_source_from]]
            if log.source_id == ref.source_id:
                return False
    return True


def enumerate_combinations(model: NormalisedModel, case: CaseSpec) -> list[Combo]:
    """Enumerate every feasible evidence combination for ``case``.

    Combinations are deduplicated by their set of original sources: two
    assignments over the same sources cost the same and are interchangeable
    for the objective, so only the lexicographically smallest is kept.
    """
    requirements = case.requirements
    order = _schedule(requirements)
    by_role = {req.role: req for req in requirements}
    assigned: dict[str, str] = {}
    used_logs: set[str] = set()
    by_source_set: dict[frozenset[str], Combo] = {}
    nodes = 0
    limit = model.max_combo_nodes

    def record() -> None:
        if not _constraints_hold(model, requirements, assigned):
            return
        sources = frozenset(
            model.logs[assigned[req.role]].source_id for req in requirements
        )
        choices = tuple(
            EvidenceChoice(
                role=req.role,
                log_id=assigned[req.role],
                source_id=model.logs[assigned[req.role]].source_id,
                timestamp=model.logs[assigned[req.role]].timestamp,
                size=model.logs[assigned[req.role]].size,
            )
            for req in requirements
        )
        combo = Combo(sources=sources, choices=choices)
        existing = by_source_set.get(sources)
        if existing is None or combo.plan_key < existing.plan_key:
            by_source_set[sources] = combo

    def dfs(index: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > limit:
            raise RetentionCapacityError(
                f"case {case.case_id!r} exceeded max_combo_nodes={limit}; "
                "reduce candidate links or raise the limit"
            )
        if index == len(order):
            record()
            return
        req = by_role[order[index]]
        for log_id in model.candidates.get((case.case_id, req.role), ()):
            if log_id in used_logs:
                # Every required item is one distinct log entry.
                continue
            log = model.logs[log_id]
            if req.after is not None and req.after in assigned:
                ref = model.logs[assigned[req.after]]
                if req.strict_after and not log.timestamp > ref.timestamp:
                    continue
                if not req.strict_after and not log.timestamp >= ref.timestamp:
                    continue
            if (
                req.distinct_source_from is not None
                and req.distinct_source_from in assigned
                and model.logs[assigned[req.distinct_source_from]].source_id
                == log.source_id
            ):
                continue
            assigned[req.role] = log_id
            used_logs.add(log_id)
            dfs(index + 1)
            used_logs.discard(log_id)
            del assigned[req.role]

    dfs(0)
    return sorted(
        by_source_set.values(),
        key=lambda c: (tuple(sorted(c.sources)), c.plan_key),
    )


# ---------------------------------------------------------------------------
# Cross-case selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _State:
    score: tuple[int, ...]
    selected: tuple[tuple[str, tuple[EvidenceChoice, ...]], ...] = ()


def _plan_key(state: _State) -> tuple:
    return tuple(
        (case_id, tuple((ch.role, ch.log_id) for ch in choices))
        for case_id, choices in state.selected
    )


def _solve_states(
    model: NormalisedModel,
    tiers: list[int],
    combos_by_case: dict[str, list[Combo]],
    budget: int,
) -> tuple[_State, frozenset[str], bool, int]:
    zeros = tuple(0 for _ in tiers)
    tier_index = {tier: i for i, tier in enumerate(tiers)}
    source_cost = {s: rec.size for s, rec in model.sources.items()}

    # mask -> best entry
    states: dict[frozenset[str], _State] = {frozenset(): _State(zeros)}
    optimal = True
    search_nodes = 0

    for case in model.cases:
        unit = tuple(1 if i == tier_index[case.tier] else 0 for i in range(len(tiers)))
        options = combos_by_case[case.case_id]
        next_states: dict[frozenset[str], _State] = {}

        def offer(mask: frozenset[str], state: _State) -> None:
            existing = next_states.get(mask)
            if existing is None:
                next_states[mask] = state
                return
            if state.score != existing.score:
                if state.score > existing.score:
                    next_states[mask] = state
                return
            if _plan_key(state) < _plan_key(existing):
                next_states[mask] = state

        for mask, state in states.items():
            search_nodes += 1
            offer(mask, state)  # case left unproven
            base_cost = _sum_cost(mask, source_cost)
            for combo in options:
                if base_cost + _sum_cost(combo.sources - mask, source_cost) > budget:
                    continue
                new_state = _State(
                    score=tuple(a + b for a, b in zip(state.score, unit)),
                    selected=state.selected + ((case.case_id, combo.choices),),
                )
                offer(mask | combo.sources, new_state)

        if len(next_states) > model.max_search_nodes:
            # Best-effort cut; the resulting plan is no longer proven optimal.
            optimal = False
            next_states = _prune(next_states, source_cost, model.max_search_nodes)
        states = next_states

    best_mask: frozenset[str] | None = None
    best_state: _State | None = None
    for mask, state in states.items():
        if best_state is None:
            best_mask, best_state = mask, state
            continue
        state_cost_value = _sum_cost(mask, source_cost)
        best_cost_value = _sum_cost(best_mask, source_cost)
        if state.score > best_state.score:
            best_mask, best_state = mask, state
        elif state.score == best_state.score:
            if state_cost_value < best_cost_value or (
                state_cost_value == best_cost_value
                and _plan_key(state) < _plan_key(best_state)
            ):
                best_mask, best_state = mask, state
    assert best_mask is not None and best_state is not None
    return best_state, best_mask, optimal, search_nodes


def _sum_cost(mask: frozenset[str], source_cost: Mapping[str, int]) -> int:
    return sum(source_cost[s] for s in mask)


def _prune(
    states: dict[frozenset[str], _State],
    source_cost: Mapping[str, int],
    limit: int,
) -> dict[frozenset[str], _State]:
    ranked = sorted(
        states.items(),
        key=lambda item: (item[1].score, -_sum_cost(item[0], source_cost)),
        reverse=True,
    )
    return dict(ranked[:limit])


# ---------------------------------------------------------------------------
# Public planner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SolveResult:
    selected: tuple[tuple[str, tuple[EvidenceChoice, ...]], ...]
    sources: frozenset[str]
    total_size: int
    optimal: bool
    search_nodes: int
    combos_by_case: dict[str, list[Combo]]


def solve(model: NormalisedModel) -> SolveResult:
    tiers = sorted({case.tier for case in model.cases}, reverse=True)
    combos_by_case = {
        case.case_id: enumerate_combinations(model, case) for case in model.cases
    }
    state, mask, optimal, search_nodes = _solve_states(
        model, tiers, combos_by_case, model.budget
    )
    total = sum(model.sources[s].size for s in mask)
    return SolveResult(
        selected=state.selected,
        sources=mask,
        total_size=total,
        optimal=optimal,
        search_nodes=search_nodes,
        combos_by_case=combos_by_case,
    )
