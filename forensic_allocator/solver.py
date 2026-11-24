"""Exact small-batch optimizer.

The solver enumerates every feasible reviewer pair for each exhibit and uses
memoized dynamic programming over remaining reviewer capacities.  It therefore
finds a true maximum-cardinality cover rather than a greedy approximation.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .models import Exhibit, Reviewer


@dataclass(frozen=True, slots=True)
class _Choice:
    count: int
    mask: int
    priority_score: float
    signature: tuple[int, ...]


def _prefer(
    candidate: _Choice,
    current: _Choice,
    tie_break: str,
) -> _Choice:
    if candidate.count != current.count:
        return candidate if candidate.count > current.count else current

    if tie_break == "lexicographic":
        if candidate.mask != current.mask:
            # A higher set bit represents inclusion of an earlier
            # (higher-priority) exhibit.
            return candidate if candidate.mask > current.mask else current
    elif tie_break == "priority_sum":
        if candidate.priority_score != current.priority_score:
            return (
                candidate
                if candidate.priority_score > current.priority_score
                else current
            )
        if candidate.mask != current.mask:
            return candidate if candidate.mask > current.mask else current
    else:  # validated by public API
        raise ValueError(f"unknown tie_break: {tie_break}")

    # Same coverage set/objective: deterministic reviewer-level tie-break.
    return candidate if candidate.signature < current.signature else current


def find_full_assignment(
    options_by_index: tuple[tuple[tuple[int, int], ...], ...],
    remaining0: tuple[int, ...],
    *,
    node_budget: int = 20_000,
) -> tuple[int, ...] | None:
    """Find any complete assignment using MRV backtracking.

    This is a fast path for the common case where every exhibit can be covered.
    It remains exhaustive within its node budget; if the budget is exhausted,
    the caller falls back to memoized optimization.
    """

    n = len(options_by_index)
    if sum(remaining0) < 2 * n:
        return None

    remaining = list(remaining0)
    signature = [-1] * n
    unassigned = set(range(n))
    visited_nodes = 0

    class _BudgetExceeded(Exception):
        pass

    def feasible_options(index: int) -> list[tuple[int, int, int]]:
        result: list[tuple[int, int, int]] = []
        for option_index, (left, right) in enumerate(options_by_index[index]):
            if remaining[left] > 0 and remaining[right] > 0:
                result.append((option_index, left, right))
        return result

    def backtrack() -> bool:
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > node_budget:
            raise _BudgetExceeded
        if not unassigned:
            return True

        chosen_index: int | None = None
        chosen_options: list[tuple[int, int, int]] | None = None
        for index in sorted(unassigned):
            options = feasible_options(index)
            if not options:
                return False
            if chosen_options is None or len(options) < len(chosen_options):
                chosen_index = index
                chosen_options = options
                if len(options) == 1:
                    break

        assert chosen_index is not None and chosen_options is not None
        for option_index, left, right in chosen_options:
            signature[chosen_index] = option_index
            remaining[left] -= 1
            remaining[right] -= 1
            unassigned.remove(chosen_index)

            if backtrack():
                return True

            unassigned.add(chosen_index)
            remaining[left] += 1
            remaining[right] += 1
            signature[chosen_index] = -1
        return False

    try:
        found = backtrack()
    except _BudgetExceeded:
        return None
    if found:
        return tuple(signature)
    return None


def solve_optimal(
    ranked_exhibits: tuple[Exhibit, ...],
    reviewers: tuple[Reviewer, ...],
    options_by_rank: tuple[tuple[tuple[int, int], ...], ...],
    *,
    priority_weights: tuple[float, ...],
    tie_break: str = "lexicographic",
) -> tuple[tuple[int, ...], int]:
    """Return selected pair option index per ranked exhibit (``-1`` if absent).

    The second return value is the maximum covered exhibit count.
    """

    n = len(ranked_exhibits)
    # No reviewer can receive more than one slot per exhibit.
    remaining0 = tuple(
        min(max(0, reviewer.capacity), n) for reviewer in reviewers
    )

    full_signature = find_full_assignment(options_by_rank, remaining0)
    if full_signature is not None:
        return full_signature, n

    priority_values = priority_weights
    bits = tuple(1 << (n - 1 - i) for i in range(n))

    # Local alias helps repeated recursive calls.
    options = options_by_rank

    @lru_cache(maxsize=None)
    def dp(index: int, remaining: tuple[int, ...]) -> _Choice:
        if index == n:
            return _Choice(0, 0, 0.0, ())

        suffix = dp(index + 1, remaining)
        best = _Choice(
            suffix.count,
            suffix.mask,
            suffix.priority_score,
            (-1,) + suffix.signature,
        )

        for option_index, (left, right) in enumerate(options[index]):
            if remaining[left] <= 0 or remaining[right] <= 0:
                continue

            next_remaining = list(remaining)
            next_remaining[left] -= 1
            next_remaining[right] -= 1
            suffix = dp(index + 1, tuple(next_remaining))
            candidate = _Choice(
                1 + suffix.count,
                bits[index] | suffix.mask,
                priority_values[index] + suffix.priority_score,
                (option_index,) + suffix.signature,
            )
            best = _prefer(candidate, best, tie_break)

        return best

    optimal = dp(0, remaining0)
    return optimal.signature, optimal.count
