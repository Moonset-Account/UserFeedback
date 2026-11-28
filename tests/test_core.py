"""核心解释器测试：惰性绑定、记忆化共享、pc 标签、字段隔离与异常。"""

import unittest

from secure_expr import (
    BinOp,
    If,
    Input,
    Label,
    Let,
    Lit,
    Not,
    Pair,
    Fst,
    Snd,
    Throw,
    Try,
    Var,
    Level,
    Runtime,
    LabeledException,
)


class LazyMemoizationTests(unittest.TestCase):
    def test_let_binding_evaluates_only_on_first_read(self):
        bound = BinOp(Lit(1), Lit(2), "+")
        root = Let("x", bound, Pair(Var("x"), Var("x")))
        rt = Runtime(root)

        first = rt.observe((0,), Level.PUBLIC)
        second = rt.observe((1,), Level.PUBLIC)

        self.assertEqual(first.value, 3)
        self.assertEqual(second.value, 3)
        # 绑定表达式只求值一次，两次读取共享同一结果
        self.assertEqual(rt.node_eval_count(bound.node_id), 1)
        stats = [s for s in rt.thunk_stats() if s["node_id"] == bound.node_id]
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["misses"], 1)
        self.assertEqual(stats[0]["hits"], 1)
        self.assertTrue(stats[0]["memoized"])

    def test_unread_binding_is_never_evaluated(self):
        bound = Throw(Lit("never"))
        root = Let("x", bound, Lit("ok"))
        rt = Runtime(root)

        obs = rt.observe((), Level.PUBLIC)
        self.assertEqual(obs.kind, "value")
        self.assertEqual(obs.value, "ok")
        self.assertEqual(rt.node_eval_count(bound.node_id), 0)

    def test_exception_thunk_is_not_memoized_and_retries(self):
        payload = Lit("boom")
        throwing = Throw(payload)
        root = Pair(throwing, Lit(0))
        rt = Runtime(root)

        a = rt.observe((0,), Level.PUBLIC)
        b = rt.observe((0,), Level.PUBLIC)
        self.assertEqual(a.kind, "error")
        self.assertEqual(b.kind, "error")
        # 异常不记忆化：每次读取都重新求值
        self.assertEqual(rt.node_eval_count(payload.node_id), 2)

    def test_shared_result_label_depends_on_reader_context_without_creep(self):
        secret_flag = Input("b")
        root = Let(
            "x",
            Lit(7),
            Pair(
                Var("x"),                          # 公开上下文读取
                If(secret_flag, Var("x"), Lit(0))  # 秘密控制上下文读取同一 thunk
            ),
        )
        inputs = {"b": (True, Level.SECRET)}
        rt = Runtime(root, inputs)

        public_read = rt.observe((0,), Level.PUBLIC)
        secret_read = rt.observe((1,), Level.PUBLIC)
        again_public = rt.observe((0,), Level.PUBLIC)

        self.assertEqual(public_read.kind, "value")
        self.assertEqual(public_read.level, Level.PUBLIC)
        self.assertEqual(secret_read.kind, "hidden")
        # 高上下文读取后，低上下文读取仍为公开标签：无标签蠕变
        self.assertEqual(again_public.level, Level.PUBLIC)
        self.assertEqual(again_public.value, 7)
        # 底层只求值一次
        lit_x = root.bound
        self.assertEqual(rt.node_eval_count(lit_x.node_id), 1)


class PairFieldIsolationTests(unittest.TestCase):
    def test_pair_fields_are_independent_thunks(self):
        failing = Throw(Lit("bad"))
        root = Pair(Lit("clean"), failing)
        rt = Runtime(root)

        good = rt.observe((0,), Level.PUBLIC)
        bad = rt.observe((1,), Level.PUBLIC)

        self.assertEqual(good.kind, "value")
        self.assertEqual(good.value, "clean")
        self.assertEqual(bad.kind, "error")
        self.assertEqual(bad.value, "bad")
        self.assertEqual(bad.level, Level.PUBLIC)

    def test_untaken_branch_does_not_pollute_sibling_field(self):
        flag = Input("b")
        bad_relabel = Label(Input("s"), Level.PUBLIC)  # 仅在取用的 then 分支中才违规
        root = Pair(
            Lit("clean"),
            If(flag, bad_relabel, Lit(2)),
        )
        # flag=False：含非法降级的 then 分支完全不求值
        rt = Runtime(root, {"b": (False, Level.SECRET), "s": (9, Level.SECRET)})
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, "clean")
        # else 分支在秘密 pc 下求值，公开视角隐藏，高许可下可见取值 2
        self.assertEqual(rt.observe((1,), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((1,), Level.SECRET).value, 2)
        self.assertEqual(rt.violation_traces(), [])
        self.assertEqual(rt.node_eval_count(bad_relabel.node_id), 0)

    def test_taken_branch_failure_is_contained_in_its_field(self):
        flag = Input("b")
        root = Pair(
            Lit("clean"),
            If(flag, Label(Input("s"), Level.PUBLIC), Lit(2)),
        )
        rt = Runtime(root, {"b": (True, Level.SECRET), "s": (9, Level.SECRET)})
        # 字段 0 不受字段 1 中违规影响，仍可正常观察
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, "clean")
        with self.assertRaises(Exception) as ctx:
            rt.observe((1,), Level.PUBLIC)
        self.assertEqual(ctx.exception.node_id, root.second.then.node_id)


class ConditionalAndLabelTests(unittest.TestCase):
    def test_secret_condition_lifts_branch_result(self):
        root = If(Input("b"), Lit(1), Lit(2))
        rt_true = Runtime(root, {"b": (True, Level.SECRET)})
        rt_false = Runtime(root, {"b": (False, Level.SECRET)})
        self.assertEqual(rt_true.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt_false.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt_true.observe((), Level.SECRET).value, 1)
        self.assertEqual(rt_false.observe((), Level.SECRET).value, 2)

    def test_label_upgrade_is_allowed(self):
        root = Label(Lit(1), Level.SECRET)
        rt = Runtime(root)
        self.assertEqual(rt.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((), Level.SECRET).value, 1)

    def test_non_boolean_condition_raises_labeled_exception(self):
        root = If(Lit(1), Lit(1), Lit(2))
        rt = Runtime(root)
        obs = rt.observe((), Level.PUBLIC)
        self.assertEqual(obs.kind, "error")
        self.assertEqual(obs.level, Level.PUBLIC)

    def test_secret_cond_exception_is_hidden_publicly(self):
        root = If(Input("b"), Throw(Lit("x")), Lit(1))
        rt = Runtime(root, {"b": (True, Level.SECRET)})
        self.assertEqual(rt.observe((), Level.PUBLIC).kind, "hidden")
        high = rt.observe((), Level.SECRET)
        self.assertEqual(high.kind, "error")
        self.assertEqual(high.value, "x")


class ExceptionAndTryTests(unittest.TestCase):
    def test_try_catches_visible_exception(self):
        root = Try(Throw(Lit("oops")), BinOp(Var("_exn"), Lit("!"), "+"))
        rt = Runtime(root)
        obs = rt.observe((), Level.PUBLIC)
        self.assertEqual(obs.kind, "value")
        self.assertEqual(obs.value, "oops!")

    def test_try_cannot_catch_exception_invisible_to_pc(self):
        root = Try(
            If(Input("b"), Throw(Lit("secret-boom")), Lit(1)),
            Lit("handler-result"),
        )
        rt = Runtime(root, {"b": (True, Level.SECRET)})
        # 异常标签 H > 公开 pc：处理器不可见，异常上浮后对外隐藏
        self.assertEqual(rt.observe((), Level.PUBLIC).kind, "hidden")
        high = rt.observe((), Level.SECRET)
        self.assertEqual(high.kind, "error")
        self.assertEqual(high.value, "secret-boom")

    def test_try_inside_secret_context_catches_secret_exception(self):
        root = If(
            Input("b"),
            Try(Throw(Lit("X")), Var("_exn")),
            Try(Throw(Lit("Y")), Var("_exn")),
        )
        rt_x = Runtime(root, {"b": (True, Level.SECRET)})
        rt_y = Runtime(root, {"b": (False, Level.SECRET)})
        self.assertEqual(rt_x.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt_y.observe((), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt_x.observe((), Level.SECRET).value, "X")
        self.assertEqual(rt_y.observe((), Level.SECRET).value, "Y")


class BinOpAndPrimitiveTests(unittest.TestCase):
    def test_arith_and_compare_with_label_join(self):
        root = Pair(
            BinOp(Lit(7), Lit(2), "//"),
            BinOp(Input("s"), Lit(2), "+"),
        )
        rt = Runtime(root, {"s": (40, Level.SECRET)})
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, 3)
        self.assertEqual(rt.observe((1,), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((1,), Level.SECRET).value, 42)

    def test_division_by_zero_is_labeled_exception(self):
        root = BinOp(Lit(1), Lit(0), "//")
        rt = Runtime(root)
        obs = rt.observe((), Level.PUBLIC)
        self.assertEqual(obs.kind, "error")
        self.assertEqual(obs.origin, root.node_id)

    def test_type_error_is_labeled_exception_with_joined_label(self):
        root = BinOp(Input("s"), Lit(1), "+")
        rt = Runtime(root, {"s": ("not-int", Level.SECRET)})
        self.assertEqual(rt.observe((), Level.PUBLIC).kind, "hidden")
        high = rt.observe((), Level.SECRET)
        self.assertEqual(high.kind, "error")
        self.assertEqual(high.level, Level.SECRET)

    def test_not_and_boolean_operators(self):
        root = Pair(
            Not(Lit(False)),
            BinOp(Lit(True), Input("b"), "and"),
        )
        rt = Runtime(root, {"b": (False, Level.PUBLIC)})
        self.assertIs(rt.observe((0,), Level.PUBLIC).value, True)
        self.assertIs(rt.observe((1,), Level.PUBLIC).value, False)

    def test_missing_input_and_unbound_var(self):
        rt1 = Runtime(Input("missing"))
        self.assertEqual(rt1.observe((), Level.PUBLIC).kind, "error")
        rt2 = Runtime(Var("nope"))
        self.assertEqual(rt2.observe((), Level.PUBLIC).kind, "error")

    def test_projection_on_non_pair_raises(self):
        rt = Runtime(Fst(Lit(5)))
        obs = rt.observe((), Level.PUBLIC)
        self.assertEqual(obs.kind, "error")
        rt2 = Runtime(Snd(Lit(5)))
        self.assertEqual(rt2.observe((), Level.PUBLIC).kind, "error")


if __name__ == "__main__":
    unittest.main()
