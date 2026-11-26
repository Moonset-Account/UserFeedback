"""Minimum weighted squared-error integer post-processing on a tree.

The optimisation problem
------------------------
Given a validated :class:`~hierpost.model.Hierarchy`, choose non-negative
integer counts ``x_v`` for every node such that

* ``x_root == root_total``,
* for every internal node ``v``: ``sum_{c in children(v)} x_c == x_v``,

minimising

    ``sum_{v measured} weight_v * (x_v - noisy_v) ** 2``.

A node is *measured* when its ``noisy`` value is present; missing ("缺测")
nodes still receive counts but contribute no error term.  Ties between
optimal solutions are broken by the lexicographically smallest vector of leaf
counts, leaves ordered by ``leaf_index``.

Dynamic program
---------------
For a subtree rooted at ``v`` let ``F_v(t)`` be the minimum error contributed
by measured nodes in that subtree, subject to ``x_v == t``.  Leaves have the
closed form ``w*(t-y)**2`` (or ``0`` when missing).  For an internal node the
children's totals range over all compositions of ``t``, so ``F_v`` is the
min-plus convolution of the child tables plus ``v``'s own term.  Counts are
bounded by ``root_total`` because all values are non-negative and sum upwards,
so each table only needs the domain ``0 .. root_total``.

For the tie-break each state also carries the lexicographically smallest leaf
count vector among its cost-optimal solutions.  Vectors are stored over *all*
leaves of the hierarchy (zeros outside the subtree), which makes the
lexicographic order the global one and lets two partial solutions be merged
by element-wise addition.  Because lexicographic order is invariant under
adding a fixed non-negative vector, keeping the lex-smallest vector per
(cost-optimal) state cannot hurt any later combination.  Previous children's
tables are released as soon as their parent has been combined, keeping peak
memory linear in the frontier.

Complexity is ``O(root_total**2 * L)`` per tree edge (``L`` = number of
leaves), which is comfortable for the publication-sized totals this module is
meant for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .model import Hierarchy, HierarchyError


@dataclass(frozen=True)
class PostprocessResult:
    """Outcome of :func:`solve`.

    Attributes
    ----------
    counts:
        ``{node_id: integer count}`` for every node, satisfying the
        hierarchy constraints exactly.
    total_cost:
        Weighted squared error of the published table.
    leaf_vector:
        Leaf counts ordered by ``leaf_index`` (the tie-break order).
    level_totals:
        ``{depth: sum of published counts of every node at that depth}``.
        The nodes at a fixed depth are disjoint subtrees covering every leaf
        that lies at that depth or deeper, so the total equals ``root_total``
        minus the mass held by leaves strictly shallower than that depth.  It
        is therefore ``root_total`` for depth 0, for every depth in a
        level-complete tree, and for any depth with no shallower leaves; it can
        be smaller only below leaves of mixed depth.
    measured_cost:
        Per-node breakdown ``{node_id: weight * (count - noisy) ** 2}`` for
        measured nodes only.
    """

    counts: dict[str, int]
    total_cost: int
    leaf_vector: tuple[int, ...]
    level_totals: dict[int, int]
    measured_cost: dict[str, int]


def _validate_solution(
    hierarchy: Hierarchy,
    counts: Mapping[str, int],
    total_cost: int,
    measured_cost: Mapping[str, int],
) -> None:
    """Re-check every hard constraint on the produced table.

    This guards against a bug in the DP silently publishing an invalid table;
    it only inspects the aggregate counts, never any individual record.
    """
    for node_id, value in counts.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AssertionError(
                f"node {node_id!r}: published count must be a non-negative "
                f"integer, got {value!r}"
            )
    if counts[hierarchy.root] != hierarchy.root_total:
        raise AssertionError("root count does not equal the public root total")
    for node_id, child_ids in hierarchy.children.items():
        if not child_ids:
            continue
        child_sum = sum(counts[c] for c in child_ids)
        if child_sum != counts[node_id]:
            raise AssertionError(
                f"node {node_id!r}: children sum to {child_sum}, node is "
                f"{counts[node_id]}"
            )
    recomputed = sum(measured_cost.values())
    if recomputed != total_cost:
        raise AssertionError(
            f"reported cost {total_cost} disagrees with breakdown {recomputed}"
        )
    expected_measured = {
        nid for nid, node in hierarchy.nodes.items() if node.noisy is not None
    }
    if set(measured_cost) != expected_measured:
        raise AssertionError("measured cost breakdown covers the wrong nodes")
    for node_id, node in hierarchy.nodes.items():
        if node.noisy is None:
            continue
        expected = node.weight * (counts[node_id] - node.noisy) ** 2
        if measured_cost[node_id] != expected:
            raise AssertionError(f"node {node_id!r}: measured cost mismatch")


def solve(hierarchy: Hierarchy) -> PostprocessResult:
    """Solve the constrained integer least-squares problem on ``hierarchy``."""
    root_total = hierarchy.root_total
    leaf_order = hierarchy.leaves
    n_leaves = len(leaf_order)
    leaf_position = {node_id: i for i, node_id in enumerate(leaf_order)}
    width = root_total + 1

    # cost_table[v][t] -> minimal subtree cost with x_v == t
    # vec_table[v][t]  -> lex-smallest leaf vector attaining that cost
    cost_table: dict[str, list[Optional[int]]] = {}
    vec_table: dict[str, list[Optional[list[int]]]] = {}

    for node_id in hierarchy.postorder:
        node = hierarchy.nodes[node_id]
        own_term: Optional[list[int]] = None
        if node.noisy is not None:
            own_term = [
                node.weight * (t - node.noisy) ** 2 for t in range(width)
            ]

        if hierarchy.is_leaf(node_id):
            if own_term is None:
                cost: list[Optional[int]] = [0] * width
            else:
                cost = list(own_term)
            position = leaf_position[node_id]
            vecs: list[Optional[list[int]]] = []
            for t in range(width):
                base = [0] * n_leaves
                base[position] = t
                vecs.append(base)
            cost_table[node_id] = cost
            vec_table[node_id] = vecs
            continue

        # Min-plus convolution of the children, one child at a time.
        accum_cost: list[Optional[int]] = [0] + [None] * root_total
        accum_vec: list[Optional[list[int]]] = [
            [0] * n_leaves
        ] + [None] * root_total

        for child_id in hierarchy.children[node_id]:
            child_cost = cost_table.pop(child_id)
            child_vec = vec_table.pop(child_id)
            new_cost: list[Optional[int]] = [None] * width
            new_vec: list[Optional[list[int]]] = [None] * width
            for s in range(width):
                base_cost = accum_cost[s]
                if base_cost is None:
                    continue
                base_vec = accum_vec[s]
                limit = root_total - s
                for tc in range(limit + 1):
                    child_c = child_cost[tc]
                    if child_c is None:
                        continue
                    cand_cost = base_cost + child_c
                    ns = s + tc
                    best_cost = new_cost[ns]
                    if best_cost is not None and cand_cost > best_cost:
                        # Cannot win on cost, avoid building the vector.
                        continue
                    cand_vec = _merge(base_vec, child_vec[tc])
                    if best_cost is None or cand_cost < best_cost:
                        new_cost[ns] = cand_cost
                        new_vec[ns] = cand_vec
                    elif cand_vec < new_vec[ns]:  # type: ignore[operator]
                        new_vec[ns] = cand_vec
            accum_cost, accum_vec = new_cost, new_vec

        if own_term is not None:
            for t in range(width):
                current = accum_cost[t]
                if current is not None:
                    accum_cost[t] = current + own_term[t]

        cost_table[node_id] = accum_cost
        vec_table[node_id] = accum_vec

    root_cost = cost_table[hierarchy.root][root_total]
    root_vec = vec_table[hierarchy.root][root_total]
    if root_cost is None or root_vec is None:
        raise HierarchyError("no feasible integer table for this hierarchy")

    # Internal counts are the subtree sums of the published leaf counts, so
    # the parent == sum(children) identity holds by construction.
    counts: dict[str, int] = {}
    for node_id in hierarchy.postorder:
        if hierarchy.is_leaf(node_id):
            counts[node_id] = root_vec[leaf_position[node_id]]
        else:
            counts[node_id] = sum(
                counts[c] for c in hierarchy.children[node_id]
            )

    measured_cost = {
        node_id: hierarchy.nodes[node_id].weight
        * (counts[node_id] - hierarchy.nodes[node_id].noisy) ** 2
        for node_id in hierarchy.preorder
        if hierarchy.nodes[node_id].noisy is not None
    }
    total_cost = sum(measured_cost.values())

    level_totals: dict[int, int] = {}
    for node_id, value in counts.items():
        depth = hierarchy.depth[node_id]
        level_totals[depth] = level_totals.get(depth, 0) + value

    _validate_solution(hierarchy, counts, total_cost, measured_cost)

    return PostprocessResult(
        counts=counts,
        total_cost=total_cost,
        leaf_vector=tuple(root_vec),
        level_totals=dict(sorted(level_totals.items())),
        measured_cost=measured_cost,
    )


def _merge(
    left: Sequence[int], right: Sequence[int]
) -> list[int]:
    """Element-wise sum of two leaf vectors."""
    return [a + b for a, b in zip(left, right)]


def solve_spec(spec: Mapping[str, object]) -> PostprocessResult:
    """Convenience wrapper accepting the plain-dict hierarchy form."""
    return solve(Hierarchy.from_dict(spec))
