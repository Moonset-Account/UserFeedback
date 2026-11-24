"""Exact budget-constrained pair-discrimination optimizer.

The objective has a useful union form: each probe distinguishes a fixed set of
fault pairs, and a pair is distinguished by a selected probe set iff at least
one selected probe distinguishes it.  Selecting probes is therefore a
budget/coverage-constrained maximum-union problem.

The solver uses depth-first subset enumeration with:

* an exact suffix dynamic program for minimum-cost entry coverage;
* a union upper bound for pair-discrimination pruning;
* deterministic input-order tie breaking.

The worst case is exponential, as the underlying constrained maximum-coverage
problem is NP-hard, but synthetic patrol layouts normally have a small number
of sites/entries and remain fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key, lru_cache
import math
from typing import Hashable, Sequence

from .errors import InfeasibleCoverageError
from .model import Number


@dataclass(frozen=True)
class OptimalSubset:
    mask: int
    cost: Number
    score: int
    probe_count: int


def _indices(mask: int, count: int) -> tuple[int, ...]:
    return tuple(i for i in range(count) if mask & (1 << i))


def optimal_probe_subset(
    *,
    costs: Sequence[Number],
    distinction_masks: Sequence[int],
    entry_masks: Sequence[int],
    required_entry_mask: int,
    budget: Number,
    required_entries: frozenset[Hashable] | None = None,
    entry_ids: Sequence[Hashable] | None = None,
    tie_order: Sequence[int] | None = None,
) -> OptimalSubset:
    """Return an optimal internal probe bit mask.

    Input arrays are assumed to be in candidate order; that order is the final
    lexicographic tie breaker.
    """

    n = len(costs)
    required_entries = required_entries or frozenset()
    entry_ids = entry_ids or []
    original_indices = list(tie_order if tie_order is not None else range(n))

    suffix_or = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_or[i] = suffix_or[i + 1] | distinction_masks[i]

    def tail_key(mask: int) -> tuple[int, ...]:
        return tuple(sorted(original_indices[i] for i in _indices(mask, n)))

    def tail_better(
        candidate: tuple[Number, int], current: tuple[Number, int] | None
    ) -> bool:
        if current is None:
            return True
        candidate_cost, candidate_mask = candidate
        current_cost, current_mask = current
        if candidate_cost != current_cost:
            return candidate_cost < current_cost
        candidate_count = candidate_mask.bit_count()
        current_count = current_mask.bit_count()
        if candidate_count != current_count:
            return candidate_count < current_count
        return tail_key(candidate_mask) < tail_key(current_mask)

    @lru_cache(maxsize=None)
    def cover_plan(i: int, uncovered_mask: int) -> tuple[Number, int] | None:
        """Minimum-cost suffix plan covering uncovered required-entry bits."""

        if uncovered_mask == 0:
            return (0, 0)
        if i == n:
            return None

        best = cover_plan(i + 1, uncovered_mask)

        covers = entry_masks[i] & uncovered_mask
        if covers:
            remaining = cover_plan(i + 1, uncovered_mask & ~entry_masks[i])
            if remaining is not None:
                included_mask = remaining[1] | (1 << i)
                candidate = (costs[i] + remaining[0], included_mask)
                if tail_better(candidate, best):
                    best = candidate
        return best

    initial = cover_plan(0, required_entry_mask)
    if initial is None:
        raise InfeasibleCoverageError(
            "no candidate probe covers all required entries",
            required_entries=required_entries,
            missing_entries=required_entries,
            minimum_cost=None,
            budget=budget,
        )

    minimum_cost, best_mask = initial
    if minimum_cost > budget:
        raise InfeasibleCoverageError(
            f"minimum coverage cost {minimum_cost} exceeds budget {budget}",
            required_entries=required_entries,
            missing_entries=None,
            minimum_cost=minimum_cost,
            budget=budget,
        )

    best_cost: Number = minimum_cost
    best_score = _union_score(best_mask, distinction_masks)
    best_count = best_mask.bit_count()
    best_index_key = tail_key(best_mask)

    def is_better(
        mask: int, cost: Number, score: int
    ) -> bool:
        nonlocal best_cost, best_score, best_count, best_index_key
        count = mask.bit_count()
        index_key = tail_key(mask)

        if score != best_score:
            return score > best_score
        if cost != best_cost:
            return cost < best_cost
        if count != best_count:
            return count < best_count
        return index_key < best_index_key

    def accept(mask: int, cost: Number, union_mask: int | None = None) -> None:
        nonlocal best_cost, best_score, best_count, best_index_key, best_mask
        score = union_mask.bit_count() if union_mask is not None else _union_score(
            mask, distinction_masks
        )
        if is_better(mask, cost, score):
            best_mask = mask
            best_cost = cost
            best_score = score
            best_count = mask.bit_count()
            best_index_key = tail_key(mask)

    def ratio_order(items: list[tuple[int, Number]]) -> list[tuple[int, Number]]:
        def compare(left: tuple[int, Number], right: tuple[int, Number]) -> int:
            left_weight, left_cost = left
            right_weight, right_cost = right
            lhs = left_weight * right_cost
            rhs = right_weight * left_cost
            if lhs > rhs:
                return -1
            if lhs < rhs:
                return 1
            return 0

        return sorted(items, key=cmp_to_key(compare))

    def budget_knapsack_upper(
        i: int, union_mask: int, budget_remaining: Number
    ) -> int:
        """Safe upper bound from a fractional knapsack relaxation.

        Each remaining probe has weight = number of not-yet-covered pair bits
        it would add.  Overlap between probes means the true union gain is no
        larger than the sum of those weights, so maximizing weights under the
        remaining budget is a relaxation.
        """

        if budget_remaining == math.inf:
            return (union_mask | suffix_or[i]).bit_count()

        zero_cost_weight = 0
        items: list[tuple[int, Number]] = []
        for j in range(i, n):
            weight = (distinction_masks[j] & ~union_mask).bit_count()
            if weight == 0:
                continue
            if costs[j] == 0:
                zero_cost_weight += weight
            else:
                items.append((weight, costs[j]))

        capacity = budget_remaining
        added_weight = zero_cost_weight
        for weight, cost in ratio_order(items):
            if cost <= capacity:
                added_weight += weight
                capacity -= cost
            else:
                # The exact fractional item contributes at most ``weight``;
                # using the whole weight keeps this integer bound simple and
                # always safe.
                added_weight += weight
                break

        return union_mask.bit_count() + added_weight

    # The recursion depth is at most the number of candidates.  Synthetic
    # layouts are small, but do not let Python's default 1000 limit surprise a
    # caller with a few thousand candidates.
    #
    # A local recursion limit change is intentionally avoided here; the public
    # API sets it before calling this function.
    def dfs(
        i: int,
        spent: Number,
        selected_mask: int,
        union_mask: int,
        uncovered_mask: int,
    ) -> None:
        if uncovered_mask == 0:
            accept(selected_mask, spent, union_mask)

        if i == n:
            return

        coverage = cover_plan(i, uncovered_mask)
        if coverage is None:
            return

        minimum_added_cost, _ = coverage
        if spent + minimum_added_cost > budget:
            return

        maximum_union = union_mask | suffix_or[i]
        absolute_maximum_score = maximum_union.bit_count()
        budget_remaining = math.inf if budget == math.inf else budget - spent
        budget_upper_score = budget_knapsack_upper(
            i, union_mask, budget_remaining
        )
        maximum_score = min(absolute_maximum_score, budget_upper_score)

        if maximum_score < best_score:
            return

        # Every remaining branch can at best tie the current score.  A total
        # cost lower bound above the incumbent cost cannot improve the result.
        lower_bound_cost = spent + minimum_added_cost
        if maximum_score == best_score and lower_bound_cost > best_cost:
            return

        # No remaining probe can distinguish a currently undistinguished pair.
        # Only entry coverage remains relevant, so take the cheapest suffix
        # cover rather than enumerating irrelevant request subsets.
        if absolute_maximum_score == union_mask.bit_count():
            if uncovered_mask:
                tail_cost, tail_mask = coverage
                total_cost = spent + tail_cost
                if total_cost <= budget:
                    accept(selected_mask | tail_mask, total_cost)
            return

        adds_pair = bool(distinction_masks[i] & ~union_mask)
        adds_coverage = bool(entry_masks[i] & uncovered_mask)

        if adds_pair or adds_coverage:
            new_cost = spent + costs[i]
            if new_cost <= budget:
                new_uncovered = uncovered_mask & ~entry_masks[i]
                remaining_cover = cover_plan(i + 1, new_uncovered)
                if remaining_cover is not None:
                    tail_cost, _ = remaining_cover
                    if new_cost + tail_cost <= budget:
                        dfs(
                            i + 1,
                            new_cost,
                            selected_mask | (1 << i),
                            union_mask | distinction_masks[i],
                            new_uncovered,
                        )

        dfs(i + 1, spent, selected_mask, union_mask, uncovered_mask)

    dfs(0, 0, 0, 0, required_entry_mask)

    return OptimalSubset(
        mask=best_mask,
        cost=best_cost,
        score=best_score,
        probe_count=best_count,
    )


def _union_score(mask: int, distinction_masks: Sequence[int]) -> int:
    union = 0
    selected = mask
    while selected:
        low_bit = selected & -selected
        index = low_bit.bit_length() - 1
        union |= distinction_masks[index]
        selected -= low_bit
    return union.bit_count()
