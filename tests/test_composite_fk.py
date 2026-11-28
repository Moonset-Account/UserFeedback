"""复合外键：只能整组暂缓；整组列均可置空才允许暂缓。"""

from __future__ import annotations

import unittest

from migration_planner import (
    ColumnSpec,
    ConstraintChecker,
    ConstraintViolationError,
    DeferredFK,
    Insert,
    Row,
    SchemaError,
    Update,
    plan_load,
)

from helpers import assert_plan_healthy, col, fk, pk, table


def build_part_tables(cost_a=4.0, cost_b=6.0, nullable=True):
    parent = table(
        "parent", ("k1", "k2"),
        (ColumnSpec("k1", nullable=False), ColumnSpec("k2", nullable=False)),
    )
    child = table(
        "child", ("id",),
        (
            pk(),
            ColumnSpec("p1", nullable=nullable, defer_cost=cost_a),
            ColumnSpec("p2", nullable=nullable, defer_cost=cost_b),
        ),
        (fk("fk_parent", ("p1", "p2"), "parent", ("k1", "k2")),),
    )
    return parent, child


class TestCompositeForeignKey(unittest.TestCase):
    def test_composite_cycle_defers_whole_group(self):
        parent, child = build_part_tables()
        rows = [
            Row("child", {"id": 1, "p1": "a", "p2": "b"}),
            Row("parent", {"k1": "a", "k2": "b"}),
        ]
        # 无环（parent 无外键），无需暂缓。
        result = plan_load([parent, child], rows)
        assert_plan_healthy(self, result)
        self.assertEqual(result.total_cost, 0.0)

        # 让 parent 反向引用 child，形成两表互引的复合外键环。
        parent_with_fk = table(
            "parent", ("k1", "k2"),
            (
                ColumnSpec("k1", nullable=False),
                ColumnSpec("k2", nullable=False),
                col("c_id", cost=10),
            ),
            (fk("fk_child", ("c_id",), "child", ("id",)),),
        )
        rows2 = [
            Row("child", {"id": 1, "p1": "a", "p2": "b"}),
            Row("parent", {"k1": "a", "k2": "b", "c_id": 1}),
        ]
        result2 = plan_load([parent_with_fk, child], rows2)
        assert_plan_healthy(self, result2)
        # 暂缓 child 的复合外键（代价 4+6=10）优于暂缓 parent 的单列（10），
        # 两者同价时按边 id 字典序，child 的边先建（id 更小）。
        self.assertEqual(len(result2.deferred), 1)
        d = result2.deferred[0]
        self.assertEqual(d.columns, ("p1", "p2"))
        self.assertEqual(d.cost, 10.0)
        insert = next(i for i in result2.inserts if i.table == "child")
        self.assertIsNone(insert.values["p1"])
        self.assertIsNone(insert.values["p2"])
        upd = next(u for u in result2.updates if u.table == "child")
        self.assertEqual(upd.assignments, {"p1": "a", "p2": "b"})

    def test_partial_null_composite_rejected_by_checker(self):
        parent, child = build_part_tables()
        checker = ConstraintChecker({"parent": parent, "child": child})
        bad_insert = Insert(
            table="child",
            values={"id": 1, "p1": None, "p2": "b"},
            key=(1,),
        )
        with self.assertRaises(ConstraintViolationError):
            checker.apply_insert(bad_insert)

    def test_partial_null_in_input_row_rejected(self):
        parent, child = build_part_tables()
        with self.assertRaises(SchemaError):
            plan_load(
                [parent, child],
                [
                    Row("child", {"id": 1, "p1": None, "p2": "b"}),
                    Row("parent", {"k1": "a", "k2": "b"}),
                ],
            )

    def test_one_non_null_member_makes_group_unbreakable(self):
        # p2 非空 -> 整组复合外键不可暂缓；环不可打断。
        parent, child = build_part_tables(nullable=True)
        child = table(
            "child", ("id",),
            (
                pk(),
                col("p1", cost=4),
                ColumnSpec("p2", nullable=False),
            ),
            (fk("fk_parent", ("p1", "p2"), "parent", ("k1", "k2")),),
        )
        parent_with_fk = table(
            "parent", ("k1", "k2"),
            (
                ColumnSpec("k1", nullable=False),
                ColumnSpec("k2", nullable=False),
                ColumnSpec("c_id", nullable=False),
            ),
            (fk("fk_child", ("c_id",), "child", ("id",)),),
        )
        rows = [
            Row("child", {"id": 1, "p1": "a", "p2": "b"}),
            Row("parent", {"k1": "a", "k2": "b", "c_id": 1}),
        ]
        result = plan_load([parent_with_fk, child], rows)
        self.assertFalse(result.feasible)
        self.assertTrue(result.cycle)
        fk_names = {e.fk_name for e in result.cycle}
        self.assertIn("fk_parent", fk_names)

    def test_one_member_not_deferrable_makes_group_unbreakable(self):
        # p2 可空但未授权临时置空（defer_cost=None）。
        child = table(
            "child", ("id",),
            (pk(), col("p1", cost=4), col("p2")),  # p2 无 defer_cost
            (fk("fk_parent", ("p1", "p2"), "parent", ("k1", "k2")),),
        )
        parent = table(
            "parent", ("k1", "k2"),
            (
                ColumnSpec("k1", nullable=False),
                ColumnSpec("k2", nullable=False),
                ColumnSpec("c_id", nullable=False),
            ),
            (fk("fk_child", ("c_id",), "child", ("id",)),),
        )
        rows = [
            Row("child", {"id": 1, "p1": "a", "p2": "b"}),
            Row("parent", {"k1": "a", "k2": "b", "c_id": 1}),
        ]
        result = plan_load([parent, child], rows)
        self.assertFalse(result.feasible)
        self.assertTrue(result.cycle)

    def test_update_must_assign_whole_group(self):
        parent, child = build_part_tables()
        checker = ConstraintChecker({"parent": parent, "child": child})
        checker.apply_insert(
            Insert(
                table="parent",
                values={"k1": "a", "k2": "b"},
                key=("a", "b"),
            )
        )
        checker.apply_insert(
            Insert(
                table="child",
                values={"id": 1, "p1": None, "p2": None},
                key=(1,),
                deferred=(
                    DeferredFK(
                        table="child",
                        row_key=(1,),
                        fk_name="fk_parent",
                        columns=("p1", "p2"),
                        cost=10.0,
                    ),
                ),
            )
        )
        with self.assertRaises(ConstraintViolationError):
            checker.apply_update(
                Update(
                    table="child", key=(1,), fk_name="fk_parent",
                    assignments={"p1": "a"},  # 只补一列
                )
            )


if __name__ == "__main__":
    unittest.main()
