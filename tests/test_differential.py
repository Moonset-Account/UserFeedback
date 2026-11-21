"""Randomised differential tests: DP planner vs. brute-force enumeration.

These are still ordinary project unit tests over synthetic fixtures; they
just generate many small random query graphs so that subtle pruning or
partition-propagation bugs that hand-written cases miss surface quickly.
Every seed is fixed so failures are reproducible.
"""

from __future__ import annotations

import random
import unittest

from joinplan import parse_query, plan_query
from joinplan.parse import connectable_sets
from joinplan.model import JoinEdge

from reference_planner import assert_plan_shape, best_cost


def _random_connected_graph(rng: random.Random, size: int) -> list[tuple[str, str]]:
    """A random spanning tree (plus optional extra edges) over ``size`` tables."""
    names = [f"T{index}" for index in range(size)]
    edges: list[tuple[str, str]] = []
    columns = list("abcdefgh")

    for index in range(1, size):
        parent = rng.randrange(index)
        column = rng.choice(columns)
        edges.append((f"{names[index]}.{column}", f"{names[parent]}.{column}"))

    used = {tuple(sorted(edge)) for edge in edges}
    extra = rng.randrange(size + 1)
    for _ in range(extra):
        left, right = rng.sample(names, 2)
        column = rng.choice(columns)
        pair = (f"{left}.{column}", f"{right}.{column}")
        if tuple(sorted(pair)) not in used:
            used.add(tuple(sorted(pair)))
            edges.append(pair)
    return edges


def _random_query(seed: int):
    rng = random.Random(seed)
    size = rng.randint(2, 5)
    names = [f"T{index}" for index in range(size)]
    edges = _random_connected_graph(rng, size)

    # Draw partition keys from the columns that actually appear on this
    # table's join edges, so partition-propagation paths are exercised often
    # instead of rarely (a random unrelated key would seldom match a join).
    columns_by_table: dict[str, list[str]] = {name: [] for name in names}
    for left, right in edges:
        left_table, left_column = left.split(".")
        right_table, right_column = right.split(".")
        columns_by_table[left_table].append(left_column)
        columns_by_table[right_table].append(right_column)

    tables = []
    for name in names:
        table = {"name": name, "bytes": rng.choice([10, 100, 1000, 5000, 20000])}
        candidates = sorted(set(columns_by_table[name]))
        if candidates and rng.random() < 0.7:
            table["partition_key"] = rng.choice(candidates)
        tables.append(table)

    table_by_name = {table["name"]: table for table in tables}
    edge_objects = [JoinEdge(left=left, right=right) for left, right in edges]
    outputs = []
    for group in sorted(connectable_sets(table_by_name, edge_objects), key=lambda s: (len(s), sorted(s))):
        outputs.append(
            {"tables": sorted(group), "bytes": rng.choice([50, 500, 5000, 50000])}
        )

    request = {
        "nodes": rng.choice([2, 4, 8]),
        "tables": tables,
        "edges": [{"left": left, "right": right} for left, right in edges],
        "outputs": outputs,
    }
    limit_roll = rng.random()
    if limit_roll < 0.3:
        # A very small cap forces repartition on every join.
        request["broadcast_limit"] = rng.choice([0, 5])
    elif limit_roll < 0.6:
        request["broadcast_limit"] = rng.choice([100, 1000, 10000])
    return parse_query(request)


class DifferentialTests(unittest.TestCase):
    def test_random_queries_match_bruteforce(self):
        for seed in range(200):
            with self.subTest(seed=seed):
                query = _random_query(seed)
                plan = plan_query(query)
                self.assertEqual(
                    plan.total_cost,
                    best_cost(query),
                    msg=f"seed {seed}: DP and brute force disagree",
                )
                assert_plan_shape(self, plan, query)

    def test_plans_only_move_bytes_the_model_licenses(self):
        for seed in range(100):
            with self.subTest(seed=seed):
                query = _random_query(seed)
                plan = plan_query(query)
                for step in plan.steps:
                    if step.method == "broadcast":
                        self.assertEqual(step.left_transfer, 0)
                        if query.broadcast_limit is not None:
                            right = query.table(step.right_table)
                            self.assertLessEqual(right.nbytes, query.broadcast_limit)
                        self.assertEqual(
                            step.right_transfer,
                            query.table(step.right_table).nbytes * (query.nodes - 1),
                        )
                    else:
                        self.assertEqual(step.method, "repartition")


if __name__ == "__main__":
    unittest.main()
