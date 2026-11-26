"""Engines for testing: exhaustive enumeration of every valid integer table.

Used only by the test-suite to confirm the dynamic program's optimum and its
lexicographic tie-break on small instances.  Not an execution/attack tool and
not part of the publishing path.
"""

from __future__ import annotations

from typing import Iterator

from .model import Hierarchy


def all_valid_vectors(hierarchy: Hierarchy) -> Iterator[tuple[int, ...]]:
    """Yield every leaf vector whose counts are non-negative and sum correctly.

    Only leaf vectors summing to ``root_total`` are valid; internal counts are
    then forced.  Enumeration is over ``range(root_total + 1)`` per leaf with an
    early sum prune, which is fine for the tiny reference instances.
    """
    leaves = hierarchy.leaves
    total = hierarchy.root_total
    n = len(leaves)

    def rec(index: int, remaining: int, prefix: list[int]):
        if index == n - 1:
            prefix.append(remaining)
            yield tuple(prefix)
            prefix.pop()
            return
        for value in range(remaining + 1):
            prefix.append(value)
            yield from rec(index + 1, remaining - value, prefix)
            prefix.pop()

    yield from rec(0, total, [])


def cost_of(hierarchy: Hierarchy, leaf_vector: tuple[int, ...]) -> int:
    """Objective value of the table induced by ``leaf_vector``."""
    counts = _counts_from_leaves(hierarchy, leaf_vector)
    cost = 0
    for node_id, node in hierarchy.nodes.items():
        if node.noisy is not None:
            cost += node.weight * (counts[node_id] - node.noisy) ** 2
    return cost


def _counts_from_leaves(
    hierarchy: Hierarchy, leaf_vector: tuple[int, ...]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node_id, value in zip(hierarchy.leaves, leaf_vector):
        counts[node_id] = value
    for node_id in hierarchy.postorder:
        if not hierarchy.is_leaf(node_id):
            counts[node_id] = sum(
                counts[c] for c in hierarchy.children[node_id]
            )
    return counts


def brute_force(hierarchy: Hierarchy) -> tuple[int, tuple[int, ...]]:
    """Return ``(min_cost, lexicographically smallest optimal leaf vector)``."""
    best_cost = None
    best_vector = None
    for vector in all_valid_vectors(hierarchy):
        cost = cost_of(hierarchy, vector)
        if (
            best_cost is None
            or cost < best_cost
            or (cost == best_cost and vector < best_vector)
        ):
            best_cost = cost
            best_vector = vector
    if best_cost is None or best_vector is None:
        raise AssertionError("no feasible table found by brute force")
    return best_cost, best_vector
