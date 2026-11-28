"""分字段观察接口与非法降级节点定位测试。"""

import unittest

from secure_expr import (
    BinOp,
    DowngradeViolation,
    Fst,
    If,
    Input,
    Label,
    Let,
    Lit,
    Observation,
    ObservationPathError,
    Pair,
    Throw,
    Try,
    Var,
    Level,
    Runtime,
    PAIR,
)


class ObserveInterfaceTests(unittest.TestCase):
    def test_pair_is_exposed_as_pair_marker_only(self):
        root = Pair(Lit(1), Lit(2))
        rt = Runtime(root)
        root_obs = rt.observe((), Level.PUBLIC)
        self.assertEqual(root_obs.kind, "value")
        self.assertIs(root_obs.value, PAIR)
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, 1)
        self.assertEqual(rt.observe((1,), Level.PUBLIC).value, 2)

    def test_hidden_value_and_hidden_error_are_indistinguishable(self):
        value_program = If(Input("b"), Lit(1), Lit(2))
        error_program = If(Input("b"), Throw(Lit("e")), Lit(2))
        rt_v = Runtime(value_program, {"b": (True, Level.SECRET)})
        rt_e = Runtime(error_program, {"b": (True, Level.SECRET)})
        self.assertEqual(
            rt_v.observe((), Level.PUBLIC),
            rt_e.observe((), Level.PUBLIC),
        )
        self.assertEqual(rt_v.observe((), Level.PUBLIC), Observation.hidden())

    def test_secret_clearance_distinguishes_value_and_error(self):
        value_program = If(Input("b"), Lit(1), Lit(2))
        error_program = If(Input("b"), Throw(Lit("e")), Lit(2))
        rt_v = Runtime(value_program, {"b": (True, Level.SECRET)})
        rt_e = Runtime(error_program, {"b": (True, Level.SECRET)})
        self.assertEqual(rt_v.observe((), Level.SECRET).kind, "value")
        self.assertEqual(rt_e.observe((), Level.SECRET).kind, "error")

    def test_nested_pair_paths(self):
        root = Pair(Pair(Lit("a"), Lit("b")), Lit("c"))
        rt = Runtime(root)
        self.assertEqual(rt.observe((0, 0), Level.PUBLIC).value, "a")
        self.assertEqual(rt.observe((0, 1), Level.PUBLIC).value, "b")
        self.assertEqual(rt.observe((1,), Level.PUBLIC).value, "c")

    def test_invalid_paths_raise_observation_path_error(self):
        rt = Runtime(Pair(Lit(1), Lit(2)))
        with self.assertRaises(ObservationPathError):
            rt.observe((2,), Level.PUBLIC)
        with self.assertRaises(ObservationPathError):
            rt.observe((0, 0), Level.PUBLIC)  # 标量上继续下降

    def test_partial_clearance_on_mixed_pair(self):
        root = Pair(Lit("pub"), Input("s"))
        rt = Runtime(root, {"s": ("sec", Level.SECRET)})
        self.assertEqual(rt.observe((0,), Level.PUBLIC).value, "pub")
        self.assertEqual(rt.observe((1,), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((1,), Level.SECRET).value, "sec")


class DowngradeLocationTests(unittest.TestCase):
    def _build_program(self):
        flag = Input("b")
        bad_relabel = Label(Input("s"), Level.PUBLIC)  # H -> L 的非法降级节点
        root = Pair(
            Lit("clean"),
            If(flag, bad_relabel, Lit(0)),
        )
        return root, bad_relabel

    def test_illegal_downgrade_reports_source_node(self):
        root, bad_node = self._build_program()
        rt = Runtime(root, {"b": (True, Level.SECRET), "s": (9, Level.SECRET)})

        with self.assertRaises(DowngradeViolation) as ctx:
            rt.observe((1,), Level.PUBLIC)

        self.assertEqual(ctx.exception.node_id, bad_node.node_id)
        self.assertEqual(ctx.exception.from_level, Level.SECRET)
        self.assertEqual(ctx.exception.to_level, Level.PUBLIC)
        traces = rt.violation_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["kind"], "downgrade_violation")
        self.assertEqual(traces[0]["node_id"], bad_node.node_id)

    def test_violation_does_not_fire_when_branch_untaken(self):
        root, bad_node = self._build_program()
        rt = Runtime(root, {"b": (False, Level.SECRET), "s": (9, Level.SECRET)})
        # else 结果处于秘密 pc：公开视角隐藏，高许可下取值 0
        self.assertEqual(rt.observe((1,), Level.PUBLIC).kind, "hidden")
        self.assertEqual(rt.observe((1,), Level.SECRET).value, 0)
        self.assertEqual(rt.violation_traces(), [])
        # 未取用分支里的降级节点从未求值
        self.assertEqual(rt.node_eval_count(bad_node.node_id), 0)

    def test_downgrade_violation_cannot_be_caught_by_try(self):
        bad = Label(Input("s"), Level.PUBLIC)
        root = Try(bad, Lit("recovered"))
        rt = Runtime(root, {"s": (1, Level.SECRET)})
        with self.assertRaises(DowngradeViolation):
            rt.observe((), Level.PUBLIC)
        self.assertEqual(rt.violation_traces()[0]["node_id"], bad.node_id)

    def test_redundant_public_relabel_is_legal(self):
        root = Label(Lit(1), Level.PUBLIC)
        rt = Runtime(root)
        self.assertEqual(rt.observe((), Level.PUBLIC).value, 1)
        self.assertEqual(rt.violation_traces(), [])

    def test_violation_nested_inside_let_thunk_located_on_force(self):
        bad = Label(Input("s"), Level.PUBLIC)
        root = Let("x", bad, Fst(Pair(Var("x"), Lit(0))))
        rt = Runtime(root, {"s": (1, Level.SECRET)})
        # 绑定在首次读取（Fst 投影）时才求值，违规定位仍准确
        with self.assertRaises(DowngradeViolation) as ctx:
            rt.observe((), Level.PUBLIC)
        self.assertEqual(ctx.exception.node_id, bad.node_id)


if __name__ == "__main__":
    unittest.main()
