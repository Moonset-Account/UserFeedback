"""正常场景：互引两表、已有目标键、无环 DAG、非空列首次满足。"""

from __future__ import annotations

import unittest

from migration_planner import (
    ColumnSpec,
    ConstraintChecker,
    ConstraintViolationError,
    DanglingReferenceError,
    Insert,
    Row,
    SchemaError,
    Update,
    plan_load,
)

from helpers import assert_plan_healthy, col, fk, pk, table


class TestMutualReference(unittest.TestCase):
    """题面核心：两个表互相引用，临时留空一列即可装入整批数据。"""

    def setUp(self) -> None:
        self.t_a = table(
            "a", ("id",),
            (pk(), col("b_id", cost=5)),
            (fk("fk_b", ("b_id",), "b", ("id",)),),
        )
        self.t_b = table(
            "b", ("id",),
            (pk(), col("a_id", cost=7)),
            (fk("fk_a", ("a_id",), "a", ("id",)),),
        )
        self.rows = [
            Row("a", {"id": 1, "b_id": 10}),
            Row("b", {"id": 10, "a_id": 1}),
        ]

    def test_chooses_cheapest_deferral(self):
        result = plan_load([self.t_a, self.t_b], self.rows)
        assert_plan_healthy(self, result)
        self.assertEqual(result.total_cost, 5.0)
        self.assertEqual(len(result.deferred), 1)
        d = result.deferred[0]
        self.assertEqual((d.table, d.fk_name), ("a", "fk_b"))

    def test_statement_sequence_and_null_placeholder(self):
        result = plan_load([self.t_a, self.t_b], self.rows)
        kinds = [type(s).__name__ for s in result.statements]
        self.assertEqual(kinds, ["Insert", "Insert", "Update"])
        first = result.statements[0]
        self.assertIsInstance(first, Insert)
        self.assertEqual(first.table, "a")
        self.assertIsNone(first.values["b_id"])
        self.assertEqual([d.fk_name for d in first.deferred], ["fk_b"])
        last = result.statements[-1]
        self.assertIsInstance(last, Update)
        self.assertEqual(last.assignments, {"b_id": 10})

    def test_every_statement_passes_independent_checker(self):
        result = plan_load([self.t_a, self.t_b], self.rows)
        checker = ConstraintChecker({"a": self.t_a, "b": self.t_b})
        for stmt in result.statements:
            if isinstance(stmt, Insert):
                checker.apply_insert(stmt)
            else:
                checker.apply_update(stmt)
        checker.assert_all_closed()
        self.assertEqual(checker.loaded[("a", (1,))]["b_id"], 10)

    def test_tie_breaks_by_edge_id_lexicographic(self):
        # 代价相同时选边 id 字典序最小的暂缓集合（这里即唯一的边 0）。
        t_a = table("a", ("id",), (pk(), col("b_id", cost=5)),
                    (fk("fk_b", ("b_id",), "b", ("id",)),))
        t_b = table("b", ("id",), (pk(), col("a_id", cost=5)),
                    (fk("fk_a", ("a_id",), "a", ("id",)),))
        result = plan_load([t_a, t_b], self.rows)
        assert_plan_healthy(self, result)
        self.assertEqual(result.deferred[0].table, "a")

    def test_reverse_row_order_still_works(self):
        result = plan_load(
            [self.t_a, self.t_b], list(reversed(self.rows))
        )
        assert_plan_healthy(self, result)
        self.assertEqual(result.total_cost, 5.0)


class TestExistingKeys(unittest.TestCase):
    """库中已存在的目标键不产生依赖，也不需要暂缓。"""

    def test_empty_batch_is_feasible_with_zero_cost(self):
        t_a = table("a", ("id",), (pk(),))
        result = plan_load([t_a], [])
        assert_plan_healthy(self, result)
        self.assertEqual(result.statements, ())
        self.assertEqual(result.total_cost, 0.0)

    def test_existing_composite_target_key(self):
        parent = table(
            "parent", ("k1", "k2"),
            (ColumnSpec("k1", nullable=False),
             ColumnSpec("k2", nullable=False)),
        )
        child = table(
            "child", ("id",),
            (pk(), col("p1", cost=1), col("p2", cost=1)),
            (fk("fk_p", ("p1", "p2"), "parent", ("k1", "k2")),),
        )
        result = plan_load(
            [parent, child],
            [Row("child", {"id": 1, "p1": "x", "p2": 9})],
            existing_keys={"parent": {("x", 9)}},
        )
        assert_plan_healthy(self, result)
        self.assertEqual(result.total_cost, 0.0)
        self.assertEqual(len(result.statements), 1)
        self.assertEqual(result.inserts[0].values["p1"], "x")

    def test_existing_target_needs_no_deferral(self):
        t_a = table(
            "a", ("id",), (pk(), col("b_id", cost=5)),
            (fk("fk_b", ("b_id",), "b", ("id",)),),
        )
        t_b = table("b", ("id",), (pk(),))
        rows = [Row("a", {"id": 1, "b_id": 99})]
        result = plan_load([t_a, t_b], rows,
                           existing_keys={"b": {(99,)}})
        assert_plan_healthy(self, result)
        self.assertEqual(result.deferred, ())
        self.assertEqual(result.total_cost, 0.0)
        self.assertEqual(len(result.statements), 1)
        self.assertEqual(result.inserts[0].values["b_id"], 99)

    def test_dangling_reference_rejected(self):
        t_a = table(
            "a", ("id",), (pk(), col("b_id", cost=5)),
            (fk("fk_b", ("b_id",), "b", ("id",)),),
        )
        t_b = table("b", ("id",), (pk(),))
        with self.assertRaises(DanglingReferenceError):
            plan_load([t_a, t_b], [Row("a", {"id": 1, "b_id": 404})])


class TestAcyclicGraph(unittest.TestCase):
    def test_dag_loads_without_any_deferral(self):
        t_parent = table("parent", ("id",), (pk(),))
        t_child = table(
            "child", ("id",), (pk(), col("pid", cost=3)),
            (fk("fk_p", ("pid",), "parent", ("id",)),),
        )
        rows = [
            Row("child", {"id": 7, "pid": 1}),
            Row("parent", {"id": 1}),
        ]
        result = plan_load([t_parent, t_child], rows)
        assert_plan_healthy(self, result)
        self.assertEqual(result.total_cost, 0.0)
        self.assertEqual(
            [s.table for s in result.inserts], ["parent", "child"]
        )
        self.assertEqual(result.updates, ())

    def test_self_reference_requires_one_deferral(self):
        t_emp = table(
            "emp", ("id",),
            (pk(), col("mgr_id", cost=2)),
            (fk("fk_mgr", ("mgr_id",), "emp", ("id",)),),
        )
        rows = [
            Row("emp", {"id": 1, "mgr_id": 2}),
            Row("emp", {"id": 2, "mgr_id": 1}),
        ]
        result = plan_load([t_emp], rows)
        assert_plan_healthy(self, result)
        self.assertEqual(len(result.deferred), 1)
        self.assertEqual(result.total_cost, 2.0)


class TestNotNullFirstInsert(unittest.TestCase):
    def test_not_null_column_cannot_be_deferred(self):
        # 环上两条边的外键列都为非空（不可暂缓），因此硬环无解；
        # 再单独验证仅一条边非空时，另一条边可暂缓即可解。
        t_a = table(
            "a", ("id",),
            (pk(), ColumnSpec("b_id", nullable=False)),
            (fk("fk_b", ("b_id",), "b", ("id",)),),
        )
        t_b = table(
            "b", ("id",),
            (pk(), ColumnSpec("a_id", nullable=False)),
            (fk("fk_a", ("a_id",), "a", ("id",)),),
        )
        rows = [Row("a", {"id": 1, "b_id": 10}),
                Row("b", {"id": 10, "a_id": 1})]
        result = plan_load([t_a, t_b], rows)
        self.assertFalse(result.feasible)
        self.assertTrue(result.cycle)
        # 环上的边至少包含不可暂缓的 fk_b。
        self.assertIn("fk_b", [e.fk_name for e in result.cycle])

        # 只有 b 侧可暂缓时，删 b->a 即可解。
        t_b_soft = table(
            "b", ("id",),
            (pk(), col("a_id", cost=7)),
            (fk("fk_a", ("a_id",), "a", ("id",)),),
        )
        soft = plan_load([t_a, t_b_soft], rows)
        self.assertTrue(soft.feasible)
        self.assertEqual(
            [d.fk_name for d in soft.deferred], ["fk_a"]
        )

    def test_checker_rejects_null_on_not_null_column(self):
        t = table("t", ("id",), (pk(), ColumnSpec("name", nullable=False)))
        checker = ConstraintChecker({"t": t})
        with self.assertRaises(ConstraintViolationError):
            checker.apply_insert(
                Insert(table="t", values={"id": 1, "name": None}, key=(1,))
            )

    def test_schema_rejects_defer_cost_on_not_null_column(self):
        bad = table(
            "t", ("id",),
            (pk(), ColumnSpec("x", nullable=False, defer_cost=1)),
        )
        with self.assertRaises(SchemaError):
            plan_load([bad], [Row("t", {"id": 1, "x": 9})])


class TestInterleavedComponents(unittest.TestCase):
    """两个互不相连的组件交错出现时，各自独立求解、顺序确定。"""

    def test_two_independent_cycles(self):
        t1 = table(
            "x", ("id",), (pk(), col("y_id", cost=1)),
            (fk("fk_y", ("y_id",), "y", ("id",)),),
        )
        t2 = table(
            "y", ("id",), (pk(), col("x_id", cost=9)),
            (fk("fk_x", ("x_id",), "x", ("id",)),),
        )
        t3 = table(
            "p", ("id",), (pk(), col("q_id", cost=2)),
            (fk("fk_q", ("q_id",), "q", ("id",)),),
        )
        t4 = table(
            "q", ("id",), (pk(), col("p_id", cost=8)),
            (fk("fk_p", ("p_id",), "p", ("id",)),),
        )
        rows = [
            Row("x", {"id": 1, "y_id": 2}),
            Row("p", {"id": 3, "q_id": 4}),
            Row("y", {"id": 2, "x_id": 1}),
            Row("q", {"id": 4, "p_id": 3}),
        ]
        result = plan_load([t1, t2, t3, t4], rows)
        assert_plan_healthy(self, result)
        self.assertEqual(result.total_cost, 3.0)  # 1 + 2
        names = {(d.table, d.fk_name) for d in result.deferred}
        self.assertEqual(names, {("x", "fk_y"), ("p", "fk_q")})


if __name__ == "__main__":
    unittest.main()
