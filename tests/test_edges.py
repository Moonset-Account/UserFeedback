"""边界与交错场景测试：标签格、嵌套控制上下文、thunk 交错读取。"""

import unittest

from secure_expr import (
    BinOp,
    If,
    Input,
    Label,
    Let,
    Lit,
    Pair,
    Fst,
    Throw,
    Var,
    Level,
    Runtime,
    join,
)
from secure_expr.ast import BIN_OPS, reset_node_ids
from secure_expr.labels import lub


class LabelLatticeTests(unittest.TestCase):
    def test_join_absorbing_and_commutative(self):
        p, s = Level.PUBLIC, Level.SECRET
        self.assertIs(join(p, p), p)
        self.assertIs(join(p, s), s)
        self.assertIs(join(s, p), s)
        self.assertIs(join(s, s), s)
        self.assertIs(lub(s, p), join(p, s))
        self.assertLess(int(Level.PUBLIC), int(Level.SECRET))


class NestedContextTests(unittest.TestCase):
    def test_nested_secret_condition_accumulates_pc(self):
        # 公开分支里再嵌一个秘密条件，内层结果仍为 H
        root = If(
            Lit(True),
            If(Input("b"), Lit(1), Lit(2)),
            Lit(3),
        )
        rt = Runtime(root, {"b": (True, Level.SECRET)})
        self.assertEqual(rt.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((), Level.SECRET).value, 1)

    def test_pair_built_under_secret_branch_is_whole_secret(self):
        root = Pair(
            Lit("sibling"),
            If(Input("b"), Pair(Lit(1), Lit(2)), Pair(Lit(3), Lit(4))),
        )
        rt = Runtime(root, {"b": (True, Level.SECRET)})
        # 兄弟字段公开可读
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, "sibling")
        # 秘密分支中的整个配对（含其字段）都不可见
        self.assertEqual(rt.observe((1,), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((1, 0), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((1, 1), Level.PUBLIC).kind, "hidden")
        # 高许可下字段可逐字段读取
        self.assertEqual(rt.observe((1, 0), Level.SECRET).value, 1)
        self.assertEqual(rt.observe((1, 1), Level.SECRET).value, 2)

    def test_projection_label_joins_pair_existence_label(self):
        # 秘密配对的字段即使内容是字面量，投影结果也必须是 H
        root = Fst(If(Input("b"), Pair(Lit(1), Lit(2)), Pair(Lit(3), Lit(4))))
        rt = Runtime(root, {"b": (False, Level.SECRET)})
        self.assertEqual(rt.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((), Level.SECRET).value, 3)


class ThunkInterleavingTests(unittest.TestCase):
    def test_interleaved_reads_of_two_fields_memoize_independently(self):
        exp_a = BinOp(Lit(1), Lit(1), "+")
        exp_b = BinOp(Lit(2), Lit(2), "+")
        root = Pair(exp_a, exp_b)
        rt = Runtime(root)
        # 交错读取 A B A B
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, 2)
        self.assertEqual(rt.observe((1,), Level.PUBLIC).value, 4)
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, 2)
        self.assertEqual(rt.observe((1,), Level.PUBLIC).value, 4)
        self.assertEqual(rt.node_eval_count(exp_a.node_id), 1)
        self.assertEqual(rt.node_eval_count(exp_b.node_id), 1)
        stats = {s["node_id"]: s for s in rt.thunk_stats()}
        self.assertEqual(stats[exp_a.node_id]["hits"], 1)
        self.assertEqual(stats[exp_b.node_id]["hits"], 1)

    def test_same_thunk_read_under_alternating_contexts(self):
        secret = Input("b")
        root = Let(
            "x",
            Lit(9),
            Pair(
                Var("x"),
                Pair(
                    If(secret, Var("x"), Lit(0)),
                    Var("x"),
                ),
            ),
        )
        rt = Runtime(root, {"b": (True, Level.SECRET)})
        # 低、高、低上下文交错访问同一绑定
        low_1 = rt.observe((0,), Level.PUBLIC)
        high_hidden = rt.observe((1, 0), Level.PUBLIC)
        low_2 = rt.observe((1, 1), Level.PUBLIC)
        self.assertEqual(low_1.level, Level.PUBLIC)
        self.assertEqual(high_hidden.kind, "hidden")
        self.assertEqual(low_2.level, Level.PUBLIC)
        self.assertEqual(low_2.value, 9)
        self.assertEqual(rt.node_eval_count(root.bound.node_id), 1)

    def test_secret_thunk_exception_interleaves_with_public_field(self):
        root = Pair(
            If(Input("b"), Throw(Input("t")), Lit("fine")),
            Lit("other"),
        )
        inputs = {"b": (True, Level.SECRET), "t": ("msg", Level.SECRET)}
        rt = Runtime(root, inputs)
        self.assertEqual(rt.observe((1,), Level.PUBLIC).value, "other")
        self.assertEqual(rt.observe((0,), Level.PUBLIC).kind, "hidden")
        high = rt.observe((0,), Level.SECRET)
        self.assertEqual(high.kind, "error")
        self.assertEqual(high.value, "msg")
        # 公开字段仍不受影响
        self.assertEqual(rt.observe((1,), Level.PUBLIC).value, "other")


class ConstructionValidationTests(unittest.TestCase):
    def test_label_requires_level_enum(self):
        with self.assertRaises(TypeError):
            Label(Lit(1), "SECRET")  # type: ignore[arg-type]

    def test_binop_rejects_unknown_operator(self):
        with self.assertRaises(ValueError):
            BinOp(Lit(1), Lit(1), "**")
        self.assertIn("+", BIN_OPS)

    def test_runtime_rejects_bad_input_level(self):
        with self.assertRaises(TypeError):
            Runtime(Lit(1), {"x": (1, 0)})  # type: ignore[dict-item]

    def test_node_ids_are_unique_and_continue_after_reset(self):
        a = Lit(1)
        b = Lit(2)
        self.assertNotEqual(a.node_id, b.node_id)
        reset_node_ids()
        c = Lit(3)
        self.assertEqual(c.node_id, 1)


if __name__ == "__main__":
    unittest.main()
