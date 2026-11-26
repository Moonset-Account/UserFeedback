"""Randomized structural tests: constraints always hold, optimum matches brute force.

The generator only produces aggregate specs (noisy counts, weights, weights,
root total); it never models individuals.
"""

import random
import unittest

from hierpost import Hierarchy, Node, solve
from hierpost.reference import brute_force


def random_hierarchy(rng: random.Random) -> Hierarchy:
    """Build a small random tree with aggregate noisy readings."""
    total = rng.randint(0, 8)
    nodes = [Node("root", None, weight=1, noisy=rng.randint(-6, 12))]
    internal = ["root"]
    next_id = 0
    for _ in range(rng.randint(0, 2)):
        parent = rng.choice(internal)
        for _ in range(2):
            child = f"n{next_id}"
            next_id += 1
            weight = rng.randint(1, 4)
            noisy = rng.randint(-8, total + 6) if rng.random() < 0.7 else None
            nodes.append(Node(child, parent, weight=weight, noisy=noisy))
            internal.append(child)
        if len(internal) > 7:
            break

    child_ids = {n.parent for n in nodes if n.parent is not None}
    leaves = [n.node_id for n in nodes if n.node_id not in child_ids]
    order = sorted(leaves)
    index = {nid: i for i, nid in enumerate(order)}
    finalized = [
        Node(n.node_id, n.parent, n.weight, n.noisy, index.get(n.node_id))
        for n in nodes
    ]
    return Hierarchy(finalized, total)


class RandomizedTests(unittest.TestCase):
    def test_constraints_and_optimality(self):
        rng = random.Random(20261127)
        checked = 0
        for _ in range(400):
            hierarchy = random_hierarchy(rng)
            if len(hierarchy.leaves) > 6 or hierarchy.root_total > 9:
                continue
            result = solve(hierarchy)
            # non-negativity / root total / additivity
            for value in result.counts.values():
                self.assertGreaterEqual(value, 0)
            self.assertEqual(
                result.counts[hierarchy.root], hierarchy.root_total
            )
            for nid, children in hierarchy.children.items():
                if children:
                    self.assertEqual(
                        result.counts[nid],
                        sum(result.counts[c] for c in children),
                    )
            # optimality + tie-break against exhaustive search
            best_cost, best_vec = brute_force(hierarchy)
            self.assertEqual(result.total_cost, best_cost)
            self.assertEqual(result.leaf_vector, best_vec)
            checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
