"""用 2^n 穷举结果核对分支定界求解器（可暂缓外键组 <= 12）。"""

from __future__ import annotations

import random
import unittest

from migration_planner import (
    ColumnSpec,
    ConstraintChecker,
    ForeignKeySpec,
    Insert,
    Row,
    TableSpec,
    plan_load,
)
from migration_planner.graph import build_dependency_graph
from migration_planner.solver import (
    brute_force_minimum_feedback_edge_set,
    minimum_feedback_edge_set,
)

from helpers import assert_plan_healthy, pk


class TestBruteForceCrossCheck(unittest.TestCase):
    def test_explicit_graph_solvers_agree(self):
        """在一组手工/随机生成的显式边图上比较两个求解器。"""
        rng = random.Random(20261128)
        for trial in range(60):
            n = rng.randint(2, 7)
            spec, rows, n_breakable = _build_random_single_table(rng, n)
            self.assertLessEqual(n_breakable, 12)
            tables = {"node": spec}
            nodes, edges = build_dependency_graph(tables, rows, {})
            breakable = [e for e in edges if e.breakable]
            self.assertLessEqual(len(breakable), 12)

            bb = minimum_feedback_edge_set(nodes, edges)
            bf = brute_force_minimum_feedback_edge_set(nodes, edges)
            if bb is None:
                self.assertIsNone(bf)
                continue
            self.assertIsNotNone(bf)
            bb_ids, bb_cost = bb
            bf_ids, bf_cost = bf
            self.assertEqual(bb_cost, bf_cost)
            self.assertEqual(sorted(bb_ids), sorted(bf_ids))

            # 端到端规划必须成功，且代价与求解器一致。
            result = plan_load([spec], rows)
            self.assertTrue(result.feasible, result.reason)
            assert_plan_healthy(self, result)
            self.assertAlmostEqual(result.total_cost, bb_cost)
            self.assertEqual(len(result.deferred), len(bb_ids))

            # 每条生成语句独立过检查器。
            checker = ConstraintChecker(tables)
            for stmt in result.statements:
                if isinstance(stmt, Insert):
                    checker.apply_insert(stmt)
                else:
                    checker.apply_update(stmt)
            checker.assert_all_closed()

    def test_twelve_breakable_edges_exhaustive(self):
        """恰好 12 组可暂缓外键时，穷举与分支定界一致（4096 个子集）。"""
        rng = random.Random(42)
        spec, rows, n_breakable = _build_random_single_table(
            rng, n_nodes=8, force_breakable=12
        )
        self.assertEqual(n_breakable, 12)
        tables = {"node": spec}
        nodes, edges = build_dependency_graph(tables, rows, {})
        self.assertEqual(sum(1 for e in edges if e.breakable), 12)
        bb = minimum_feedback_edge_set(nodes, edges)
        bf = brute_force_minimum_feedback_edge_set(nodes, edges)
        self.assertIsNotNone(bb)
        self.assertEqual(bb[1], bf[1])
        self.assertEqual(sorted(bb[0]), sorted(bf[0]))

        result = plan_load([spec], rows)
        assert_plan_healthy(self, result)
        self.assertAlmostEqual(result.total_cost, bf[1])

    def test_brute_force_refuses_more_than_twelve(self):
        rng = random.Random(7)
        spec, rows, n_breakable = _build_random_single_table(
            rng, n_nodes=8, force_breakable=13
        )
        self.assertEqual(n_breakable, 13)
        tables = {"node": spec}
        nodes, edges = build_dependency_graph(tables, rows, {})
        with self.assertRaises(ValueError):
            brute_force_minimum_feedback_edge_set(nodes, edges)
        # 分支定界仍然给出答案。
        self.assertIsNotNone(minimum_feedback_edge_set(nodes, edges))


def _build_random_single_table(rng, n_nodes, force_breakable=None):
    """构造随机单表问题，返回 (TableSpec, rows, 可暂缓边数)。

    先随机决定边集（不含自环），再为每条边建一个独立外键列，避免一行
    多外键时的列值组装问题。
    """
    edge_list: list[tuple[int, int, bool]] = []
    for src in range(n_nodes):
        for dst in range(n_nodes):
            if src == dst:
                continue
            if rng.random() < 0.2:
                edge_list.append((src, dst, False))
    # 穷举核对约定可暂缓外键不超过 12 组，超出则随机截断。
    rng.shuffle(edge_list)
    edge_list = edge_list[:12]

    if force_breakable is not None:
        # 重置为确定数量的全可暂缓边，保证测试前提。
        edge_list = []
        pairs = [(s, d) for s in range(n_nodes) for d in range(n_nodes)
                 if s != d]
        rng.shuffle(pairs)
        for src, dst in pairs[:force_breakable]:
            edge_list.append((src, dst, True))

    columns = [pk()]
    fk_specs = []
    n_breakable = 0
    values_by_node = {i: {"id": i} for i in range(n_nodes)}
    for i, (src, dst, _forced_breakable) in enumerate(edge_list):
        col_name = f"ref_{i}"
        # 交叉核对只关注最小代价反馈边集：所有边都做成可暂缓的可空外键。
        # 不可暂缓（非空）边导致的硬环由 test_unsat 专门覆盖。
        columns.append(
            ColumnSpec(col_name, nullable=True,
                       defer_cost=rng.randint(1, 10))
        )
        n_breakable += 1
        fk_specs.append(
            ForeignKeySpec(f"fk_{i}", (col_name,), "node", ("id",))
        )
        values_by_node[src][col_name] = dst
        for node_id in range(n_nodes):
            values_by_node[node_id].setdefault(col_name, None)

    spec = TableSpec("node", ("id",), tuple(columns), tuple(fk_specs))
    rows = [Row("node", values_by_node[i]) for i in range(n_nodes)]
    return spec, rows, n_breakable


if __name__ == "__main__":
    unittest.main()
