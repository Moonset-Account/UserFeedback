"""无解时的不可打断依赖环，以及检查器的负面断言。"""

from __future__ import annotations

import unittest

from migration_planner import (
    ColumnSpec,
    ConstraintChecker,
    ConstraintViolationError,
    CycleEdge,
    Insert,
    Row,
    Update,
    plan_load,
)

from helpers import col, fk, pk, table


class TestUnbreakableCycle(unittest.TestCase):
    def test_three_row_cycle_all_not_null(self):
        t = table(
            "node", ("id",),
            (pk(), ColumnSpec("next_id", nullable=False)),
            (fk("fk_next", ("next_id",), "node", ("id",)),),
        )
        rows = [
            Row("node", {"id": 1, "next_id": 2}),
            Row("node", {"id": 2, "next_id": 3}),
            Row("node", {"id": 3, "next_id": 1}),
        ]
        result = plan_load([t], rows)
        self.assertFalse(result.feasible)
        self.assertIsInstance(result.cycle, tuple)
        self.assertGreaterEqual(len(result.cycle), 2)
        for edge in result.cycle:
            self.assertIsInstance(edge, CycleEdge)
            self.assertEqual(edge.fk_name, "fk_next")
        # 环的闭合性：每条边的目标等于下一条边的源，末尾回到起点。
        for i, edge in enumerate(result.cycle):
            nxt = result.cycle[(i + 1) % len(result.cycle)]
            self.assertEqual(
                (edge.target_table, edge.target_key),
                (nxt.source_table, nxt.source_key),
            )
        text = result.describe_cycle()
        self.assertIn("node(1,)", text)
        self.assertIn("fk_next", text)

    def test_mixed_breakable_and_hard_cycle(self):
        # 环 a->b->c->a：只有 a->b 可暂缓，但 b->c、c->a 不可暂缓，
        # 不可暂缓子图 b->c->a... 实际硬环需要硬边自己闭合。
        # 这里构造：a 可暂缓指向 b；b 非空指向 c；c 非空指向 a。
        # 硬边子图为 b->c->a，无回边，因此可解（暂缓 a->b 后无环）。
        t_a = table(
            "a", ("id",), (pk(), col("b_id", cost=1)),
            (fk("fk_b", ("b_id",), "b", ("id",)),),
        )
        t_b = table(
            "b", ("id",), (pk(), ColumnSpec("c_id", nullable=False)),
            (fk("fk_c", ("c_id",), "c", ("id",)),),
        )
        t_c = table(
            "c", ("id",), (pk(), ColumnSpec("a_id", nullable=False)),
            (fk("fk_a", ("a_id",), "a", ("id",)),),
        )
        rows = [
            Row("a", {"id": 1, "b_id": 2}),
            Row("b", {"id": 2, "c_id": 3}),
            Row("c", {"id": 3, "a_id": 1}),
        ]
        result = plan_load([t_a, t_b, t_c], rows)
        self.assertTrue(result.feasible)
        self.assertEqual([d.fk_name for d in result.deferred], ["fk_b"])

        # 再加一条 a 的非空自引用硬边，制造硬环 a->a。
        t_a_self = table(
            "a", ("id",),
            (
                pk(),
                col("b_id", cost=1),
                ColumnSpec("self_id", nullable=False),
            ),
            (
                fk("fk_b", ("b_id",), "b", ("id",)),
                fk("fk_self", ("self_id",), "a", ("id",)),
            ),
        )
        rows_self = [
            Row("a", {"id": 1, "b_id": 2, "self_id": 1}),
            Row("b", {"id": 2, "c_id": 3}),
            Row("c", {"id": 3, "a_id": 1}),
        ]
        result2 = plan_load([t_a_self, t_b, t_c], rows_self)
        self.assertFalse(result2.feasible)
        self.assertTrue(result2.cycle)
        self.assertEqual(result2.cycle[0].fk_name, "fk_self")


class TestCheckerNegatives(unittest.TestCase):
    def setUp(self) -> None:
        self.t = table(
            "t", ("id",),
            (pk(), col("ref", cost=1)),
            (fk("fk_ref", ("ref",), "t", ("id",)),),
        )
        self.checker = ConstraintChecker({"t": self.t})

    def test_duplicate_primary_key_rejected(self):
        self.checker.apply_insert(
            Insert(table="t", values={"id": 1, "ref": None}, key=(1,))
        )
        with self.assertRaises(ConstraintViolationError):
            self.checker.apply_insert(
                Insert(table="t", values={"id": 1, "ref": None}, key=(1,))
            )

    def test_insert_with_missing_fk_target_rejected(self):
        with self.assertRaises(ConstraintViolationError):
            self.checker.apply_insert(
                Insert(table="t", values={"id": 1, "ref": 99}, key=(1,))
            )

    def test_update_before_insert_rejected(self):
        with self.assertRaises(ConstraintViolationError):
            self.checker.apply_update(
                Update(table="t", key=(1,), fk_name="fk_ref",
                       assignments={"ref": 2})
            )

    def test_update_with_null_rejected(self):
        from migration_planner import DeferredFK

        self.checker.apply_insert(
            Insert(
                table="t", values={"id": 1, "ref": None}, key=(1,),
                deferred=(
                    DeferredFK(
                        table="t", row_key=(1,), fk_name="fk_ref",
                        columns=("ref",), cost=1.0,
                    ),
                ),
            )
        )
        with self.assertRaises(ConstraintViolationError):
            self.checker.apply_update(
                Update(table="t", key=(1,), fk_name="fk_ref",
                       assignments={"ref": None})
            )

    def test_double_update_rejected(self):
        from migration_planner import DeferredFK

        self.checker.apply_insert(
            Insert(
                table="t",
                values={"id": 1, "ref": None},
                key=(1,),
                deferred=(
                    DeferredFK(
                        table="t", row_key=(1,), fk_name="fk_ref",
                        columns=("ref",), cost=1.0,
                    ),
                ),
            )
        )
        self.checker.apply_insert(
            Insert(table="t", values={"id": 2, "ref": 1}, key=(2,))
        )
        self.checker.apply_update(
            Update(table="t", key=(1,), fk_name="fk_ref",
                   assignments={"ref": 2})
        )
        with self.assertRaises(ConstraintViolationError):
            self.checker.apply_update(
                Update(table="t", key=(1,), fk_name="fk_ref",
                       assignments={"ref": 2})
            )
        self.checker.assert_all_closed()


if __name__ == "__main__":
    unittest.main()
