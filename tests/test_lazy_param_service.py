"""lazy_param_service 的 unittest 测试。

从工作区根目录可发现运行：
    python3 -m unittest discover -v
"""

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lazy_param_service import LazyParamService, StepByStepParams

TOL = 1e-9


def assert_close(testcase, got, ref, msg):
    """绝对或相对误差在 1e-9 内即视为一致。"""
    err = abs(got - ref)
    if err > TOL and err > TOL * abs(ref):
        testcase.fail(f"{msg}: got={got!r} ref={ref!r} err={err!r}")


class LazyParamServiceTest(unittest.TestCase):
    def make_pair(self, initial, mu):
        return LazyParamService(initial, mu), StepByStepParams(initial, mu)

    def check_all_rows(self, lazy, ref, rows, ctx):
        for row in rows:
            w_l, v_l = lazy.read(row)
            w_r, v_r = ref.read(row)
            assert_close(self, w_l, w_r, f"{ctx} row={row} w")
            assert_close(self, v_l, v_r, f"{ctx} row={row} v")

    # ---------------------------------------------------------- 正常场景

    def test_randomized_sparse_matches_reference(self):
        """小表 + 随机稀疏梯度 + 交错读取，与逐步更新逐行比对。"""
        rng = random.Random(20261122)
        mu = 0.9
        rows = [f"row{i}" for i in range(6)]
        initial = {r: rng.uniform(-1.0, 1.0) for r in rows}
        lazy, ref = self.make_pair(initial, mu)

        for t in range(1, 301):
            eta = rng.uniform(0.0, 0.5)
            d = rng.uniform(0.0, 1.0)
            grads = {
                r: rng.uniform(-2.0, 2.0) for r in rows if rng.random() < 0.25
            }
            lazy.commit(eta, d, grads)
            ref.commit(eta, d, grads)
            # 交错读取：每步随机读几行
            for r in rng.sample(rows, k=rng.randint(0, 3)):
                w_l, v_l = lazy.read(r)
                w_r, v_r = ref.read(r)
                assert_close(self, w_l, w_r, f"t={t} row={r} w")
                assert_close(self, v_l, v_r, f"t={t} row={r} v")
        self.assertEqual(lazy.step, 300)
        self.check_all_rows(lazy, ref, rows, "final")

    def test_dense_gradient_every_step(self):
        """每行每步都有梯度（无惰性区间），仍与参考一致。"""
        rng = random.Random(7)
        rows = [0, 1, 2]
        initial = {r: 0.5 * (r + 1) for r in rows}
        lazy, ref = self.make_pair(initial, mu=0.5)
        for _ in range(50):
            eta, d = rng.uniform(0.0, 1.0), rng.uniform(0.0, 1.0)
            grads = {r: rng.uniform(-1.0, 1.0) for r in rows}
            lazy.commit(eta, d, grads)
            ref.commit(eta, d, grads)
        self.check_all_rows(lazy, ref, rows, "dense")

    # ---------------------------------------------------------- 边界场景

    def test_initial_read_before_any_commit(self):
        lazy, _ = self.make_pair({"a": 3.25, "b": -1.5}, mu=0.9)
        self.assertEqual(lazy.read("a"), (3.25, 0.0))
        self.assertEqual(lazy.read("b"), (-1.5, 0.0))
        self.assertEqual(lazy.last_summary_reads, 0)

    def test_boundary_mu_zero(self):
        """mu=0：动量每步被清零，v 恒等于当步梯度。"""
        rng = random.Random(11)
        rows = ["x", "y"]
        lazy, ref = self.make_pair({"x": 1.0, "y": -2.0}, mu=0.0)
        for _ in range(40):
            grads = {"x": rng.uniform(-1, 1)} if rng.random() < 0.5 else {}
            eta, d = rng.uniform(0, 1), rng.uniform(0, 1)
            lazy.commit(eta, d, grads)
            ref.commit(eta, d, grads)
        self.check_all_rows(lazy, ref, rows, "mu=0")

    def test_boundary_mu_one_d_one(self):
        """mu=1 且 d=1：无衰减累积。"""
        rng = random.Random(13)
        rows = ["x", "y"]
        lazy, ref = self.make_pair({"x": 0.0, "y": 0.0}, mu=1.0)
        for _ in range(60):
            grads = {"y": rng.uniform(-1, 1)} if rng.random() < 0.3 else {}
            eta = rng.uniform(0, 0.3)
            lazy.commit(eta, 1.0, grads)
            ref.commit(eta, 1.0, grads)
        self.check_all_rows(lazy, ref, rows, "mu=1,d=1")

    def test_boundary_d_zero_and_eta_zero(self):
        """d=0：w 完全由动量决定；eta=0：w 只衰减。"""
        rng = random.Random(17)
        rows = ["p", "q"]
        lazy, ref = self.make_pair({"p": 2.0, "q": -0.75}, mu=0.8)
        for t in range(50):
            d = 0.0 if t % 2 == 0 else 0.5
            eta = 0.0 if t % 3 == 0 else rng.uniform(0, 0.4)
            grads = {"p": rng.uniform(-1, 1)} if rng.random() < 0.4 else {}
            lazy.commit(eta, d, grads)
            ref.commit(eta, d, grads)
        self.check_all_rows(lazy, ref, rows, "d/eta 边界")

    def test_single_step(self):
        lazy, ref = self.make_pair({"a": 1.0}, mu=0.9)
        lazy.commit(0.1, 0.99, {"a": 0.5})
        ref.commit(0.1, 0.99, {"a": 0.5})
        self.check_all_rows(lazy, ref, ["a"], "single step")

    def test_never_updated_row_after_many_steps(self):
        """从未上报的行，跨全部步一次性补齐。"""
        rng = random.Random(19)
        lazy, ref = self.make_pair({"hot": 0.0, "cold": 4.2}, mu=0.95)
        for _ in range(500):
            eta, d = rng.uniform(0, 0.5), rng.uniform(0, 1)
            grads = {"hot": rng.uniform(-1, 1)}
            lazy.commit(eta, d, grads)
            ref.commit(eta, d, grads)
        self.check_all_rows(lazy, ref, ["hot", "cold"], "cold row")

    # ------------------------------------------------------ 对数级摘要数量

    def test_log_summary_count_over_long_gap(self):
        """跨 N 个空步的单行查询，实际读取摘要数 <= 2*log2(N)+2，且与参考一致。"""
        rng = random.Random(23)
        mu = 0.9
        lazy = LazyParamService({"r": 1.0}, mu)
        ref = StepByStepParams({"r": 1.0}, mu)
        n_empty = 1000
        for _ in range(n_empty):
            eta, d = rng.uniform(0, 0.5), rng.uniform(0, 1)
            lazy.commit(eta, d, {})
            ref.commit(eta, d, {})
        lazy.read("r")
        count = lazy.last_summary_reads
        bound = 2 * math.ceil(math.log2(n_empty)) + 2
        self.assertGreater(count, 0)
        self.assertLessEqual(
            count, bound, f"读取 {count} 个摘要，超过对数界 {bound}"
        )
        print(f"\n[info] N={n_empty} 空步，实际读取摘要数={count}，对数界={bound}")
        w_l, v_l = lazy.read("r")
        w_r, v_r = ref.read("r")
        assert_close(self, w_l, w_r, "long gap w")
        assert_close(self, v_l, v_r, "long gap v")

    def test_summary_count_after_repeated_reads_is_zero(self):
        """已同步的行再次读取不需要任何摘要。"""
        lazy = LazyParamService({"r": 1.0}, 0.9)
        for _ in range(10):
            lazy.commit(0.1, 0.9, {})
        lazy.read("r")
        self.assertGreater(lazy.last_summary_reads, 0)
        lazy.read("r")
        self.assertEqual(lazy.last_summary_reads, 0)

    # ---------------------------------------------------------- 交错场景

    def test_interleaved_rows_and_reads(self):
        """不同行在不同步上报，读取与提交交错。"""
        rng = random.Random(29)
        mu = 0.85
        rows = ["A", "B", "C"]
        initial = {"A": 1.0, "B": -0.5, "C": 2.0}
        lazy, ref = self.make_pair(initial, mu)
        for t in range(1, 201):
            grads = {}
            if t % 3 == 0:
                grads["A"] = rng.uniform(-1, 1)
            if t % 5 == 0:
                grads["B"] = rng.uniform(-1, 1)
            # C 从不上报
            eta, d = rng.uniform(0, 0.4), rng.uniform(0.1, 1.0)
            lazy.commit(eta, d, grads)
            ref.commit(eta, d, grads)
            if t % 7 == 0:
                self.check_all_rows(lazy, ref, rows, f"t={t}")
        self.check_all_rows(lazy, ref, rows, "interleaved final")

    # ---------------------------------------------------------- 参数校验

    def test_validation(self):
        with self.assertRaises(ValueError):
            LazyParamService({"a": 1.0}, mu=1.5)
        with self.assertRaises(ValueError):
            LazyParamService({"a": 1.0}, mu=-0.1)
        lazy = LazyParamService({"a": 1.0}, mu=0.9)
        with self.assertRaises(ValueError):
            lazy.commit(0.1, 1.2, {})
        with self.assertRaises(ValueError):
            lazy.commit(-0.1, 0.9, {})
        with self.assertRaises(KeyError):
            lazy.commit(0.1, 0.9, {"ghost": 1.0})
        with self.assertRaises(KeyError):
            lazy.read("ghost")


if __name__ == "__main__":
    unittest.main()
