"""非干涉性测试：两组只在秘密输入上不同的样本，公开观察必须完全一致。"""

import unittest

from secure_expr import (
    BinOp,
    If,
    Input,
    Label,
    Let,
    Lit,
    NoninterferenceResult,
    Pair,
    Throw,
    Try,
    Var,
    Level,
    noninterference_check,
    Runtime,
)


def _value_exception_sample() -> tuple:
    """样本一：秘密条件同时影响一个值字段与一个异常字段。

    程序形状在两个样本间完全相同，仅输入 b / s / t 的 *取值* 不同。
    """
    flag = Input("b")
    secret_val = Input("s")
    secret_text = Input("t")
    root = Let(
        "shared",
        Lit(100),  # 两个样本共享的公开惰性绑定
        Pair(
            Lit("public-constant"),                       # 路径 (0,)：始终公开
            If(flag, secret_val, BinOp(Lit(0), Lit(1), "+")),  # (1,)：值随秘密变化
            ),
    )
    # 第三字段：异常内容随秘密变化
    root = Pair(
        root,
        If(flag, Throw(secret_text), Throw(Lit("default-error"))),
    )
    paths = [(), (0,), (0, 0), (0, 1), (1,)]
    inputs_a = {
        "b": (True, Level.SECRET),
        "s": (11, Level.SECRET),
        "t": ("secret-text-A", Level.SECRET),
    }
    inputs_b = {
        "b": (False, Level.SECRET),
        "s": (99, Level.SECRET),
        "t": ("secret-text-B", Level.SECRET),
    }
    return root, paths, inputs_a, inputs_b


def _try_catch_sample() -> tuple:
    """样本二：Try 处理器在两种秘密取值下都产生秘密结果。

    形状不变性保证：无论是否抛出，字段 1 都是秘密标签，公开视角只能看到 hidden。
    """
    flag = Input("b")
    secret_num = Input("n")
    body = If(flag, Throw(Lit("e")), Lit(1))
    handler = BinOp(Var("_exn"), secret_num, "+")  # 字符串+整数会抛类型错误，仍是 H
    root = Pair(
        Lit("anchor"),
        Try(body, handler),
    )
    paths = [(), (0,), (1,)]
    inputs_a = {"b": (True, Level.SECRET), "n": (1, Level.SECRET)}
    inputs_b = {"b": (False, Level.SECRET), "n": (2, Level.SECRET)}
    return root, paths, inputs_a, inputs_b


class NoninterferenceTests(unittest.TestCase):
    def assert_public_consistent(self, result: NoninterferenceResult) -> None:
        self.assertTrue(
            result.consistent,
            msg=f"公开观察在路径 {result.first_divergence} 处发生分歧",
        )
        for obs in result.observations_a + result.observations_b:
            if obs.kind != "hidden":
                # 可见应答必须逐字段相等；hidden 天然相等
                pass

    def test_value_and_exception_sample_is_consistent_publicly(self):
        root, paths, inputs_a, inputs_b = _value_exception_sample()
        result = noninterference_check(root, inputs_a, inputs_b, paths, Level.PUBLIC)
        self.assert_public_consistent(result)
        # 根是成对占位，两个样本一致
        self.assertEqual(result.observations_a[0], result.observations_b[0])
        # 公开字段两个样本都可见且相等
        anchor = result.observations_a[2]
        self.assertEqual(anchor.kind, "value")
        self.assertEqual(anchor.value, "public-constant")
        # 秘密值字段与秘密异常字段公开视角都是 hidden，且互不可区分
        self.assertTrue(all(o.is_hidden for o in result.observations_a[3:]))
        self.assertTrue(all(o.is_hidden for o in result.observations_b[3:]))

    def test_secret_clearance_distinguishes_the_two_samples(self):
        root, paths, inputs_a, inputs_b = _value_exception_sample()
        result = noninterference_check(root, inputs_a, inputs_b, paths, Level.SECRET)
        # 高许可下两个样本的秘密取值不同，观察 *应当* 不同（非干涉只约束低许可）
        self.assertFalse(result.consistent)
        self.assertIn(result.first_divergence, {(0, 1), (1,)})

    def test_secret_exception_payloads_are_distinguishable_with_clearance(self):
        root, paths, inputs_a, inputs_b = _value_exception_sample()
        rt_a = Runtime(root, inputs_a)
        rt_b = Runtime(root, inputs_b)
        err_a = rt_a.observe((1,), Level.SECRET)
        err_b = rt_b.observe((1,), Level.SECRET)
        self.assertEqual(err_a.kind, "error")
        self.assertEqual(err_b.kind, "error")
        self.assertNotEqual(err_a.value, err_b.value)
        self.assertEqual(err_a.value, "secret-text-A")
        self.assertEqual(err_b.value, "default-error")

    def test_try_catch_sample_is_consistent_publicly(self):
        root, paths, inputs_a, inputs_b = _try_catch_sample()
        result = noninterference_check(root, inputs_a, inputs_b, paths, Level.PUBLIC)
        self.assert_public_consistent(result)
        # 字段 1 在两种秘密取值下都只能看到 hidden
        idx = paths.index((1,))
        self.assertTrue(result.observations_a[idx].is_hidden)
        self.assertTrue(result.observations_b[idx].is_hidden)
        # 公开锚点字段不受影响
        idx0 = paths.index((0,))
        self.assertEqual(result.observations_a[idx0].value, "anchor")

    def test_unread_shared_binding_is_never_forced_in_either_sample(self):
        root, paths, inputs_a, inputs_b = _value_exception_sample()
        rt_a = Runtime(root, inputs_a)
        rt_b = Runtime(root, inputs_b)
        for rt in (rt_a, rt_b):
            rt.observe((0, 0), Level.PUBLIC)
            rt.observe((0, 1), Level.PUBLIC)
        shared_id = root.first.bound.node_id  # 外层 Pair 的首字段即 Let 节点
        # 两个样本的 body 都没有引用 shared：惰性绑定在两个运行时中都不求值
        self.assertEqual(rt_a.node_eval_count(shared_id), 0)
        self.assertEqual(rt_b.node_eval_count(shared_id), 0)

    def test_read_shared_binding_is_memoized_within_each_runtime(self):
        shared = BinOp(Lit(21), Lit(2), "*")
        program = Let("x", shared, Pair(Var("x"), Var("x")))
        inputs_a = {"b": (True, Level.SECRET)}
        inputs_b = {"b": (False, Level.SECRET)}
        rt_a = Runtime(program, inputs_a)
        rt_b = Runtime(program, inputs_b)
        for rt in (rt_a, rt_b):
            self.assertEqual(rt.observe((0,), Level.PUBLIC).value, 42)
            self.assertEqual(rt.observe((1,), Level.PUBLIC).value, 42)
            # 每个运行时内部共享，仅求值一次
            self.assertEqual(rt.node_eval_count(shared.node_id), 1)

    def test_identical_inputs_trivially_agree(self):
        root, paths, inputs_a, _ = _value_exception_sample()
        result = noninterference_check(root, inputs_a, dict(inputs_a), paths,
                                       Level.PUBLIC)
        self.assertTrue(result.consistent)


if __name__ == "__main__":
    unittest.main()
