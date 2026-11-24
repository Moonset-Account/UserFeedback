"""单段约束引擎测试：手工场景、边界场景与暴力对拍。"""

import unittest

from quantile_merger import (
    Bucket,
    InfeasibleError,
    KOutOfRangeError,
    Observation,
    SegmentModel,
)
from quantile_merger.window import compute_window_bounds

from tests._brute import brute_quantile_bounds, enumerate_multisets


def obs(series, rev, lo, hi, buckets, start=0, length=10, source="s"):
    return Observation(source, start, length, lo, hi, tuple(buckets), series, rev)


class SingleSegmentHandcraftedTest(unittest.TestCase):
    def setUp(self):
        # 两套不同桶边界同时描述同一批 5 个样本，值域 [1,10]
        self.o1 = obs("a", 1, 1, 10, [Bucket(1, 5, 3), Bucket(6, 10, 2)])
        self.o2 = obs("b", 1, 1, 10, [Bucket(1, 3, 1), Bucket(4, 10, 4)])
        self.model = SegmentModel([self.o1, self.o2])

    def test_feasibility_and_totals(self):
        self.assertTrue(self.model.feasible)
        self.assertEqual(self.model.total, 5)
        # 原子单元 [1,3]=1, [4,5]=2, [6,10]=2 被等式唯一确定
        self.assertEqual(self.model.cell_counts(True), [1, 2, 2])
        self.assertEqual(self.model.cell_counts(False), [1, 2, 2])

    def test_prefix_bounds_inside_cells(self):
        # 单元 [4,5] 内两个样本可自由布置
        smin, smax = self.model.prefix_bounds(4)
        self.assertEqual((smin, smax), (1, 3))
        smin, smax = self.model.prefix_bounds(3)
        self.assertEqual((smin, smax), (1, 1))
        smin, smax = self.model.prefix_bounds(10)
        self.assertEqual((smin, smax), (5, 5))

    def test_quantile_bounds_handcrafted(self):
        # 单元 [1,3]=1, [4,5]=2, [6,10]=2，单元内样本可贴右边界
        expected = {1: (1, 3), 2: (4, 5), 3: (4, 5), 4: (6, 10), 5: (6, 10)}
        for k, want in expected.items():
            self.assertEqual(compute_window_bounds([self.model], k), want)

    def test_k_out_of_range(self):
        with self.assertRaises(KOutOfRangeError):
            compute_window_bounds([self.model], 0)
        with self.assertRaises(KOutOfRangeError):
            compute_window_bounds([self.model], 6)

    def test_infeasible_bucket_conflict(self):
        bad = obs("c", 1, 1, 10, [Bucket(1, 5, 4), Bucket(6, 10, 1)])
        model = SegmentModel([self.o1, bad])
        self.assertFalse(model.feasible)
        self.assertIn("前缀和冲突", model.reason)
        with self.assertRaises(InfeasibleError):
            model.prefix_bounds(3)

    def test_infeasible_total_conflict(self):
        bad = obs("c", 1, 1, 10, [Bucket(1, 5, 3), Bucket(6, 10, 3)])
        model = SegmentModel([self.o1, bad])
        self.assertFalse(model.feasible)
        # 总数不一致必然体现为某个前缀和等式冲突
        self.assertTrue("冲突" in model.reason)

    def test_domain_mismatch_rejected(self):
        other = obs("b", 1, 1, 9, [Bucket(1, 5, 3), Bucket(6, 9, 2)])
        with self.assertRaises(Exception):
            SegmentModel([self.o1, other])


class SingleSegmentBruteForceTest(unittest.TestCase):
    """与暴力枚举逐 k 对拍，覆盖不同桶边界交错的正常场景。"""

    CASES = [
        # 单观测、宽桶（单元内自由放置）
        [obs("a", 1, 1, 4, [Bucket(1, 2, 2), Bucket(3, 4, 1)])],
        # 两套交错桶边界
        [
            obs("a", 1, 1, 6, [Bucket(1, 3, 2), Bucket(4, 6, 2)]),
            obs("b", 1, 1, 6, [Bucket(1, 2, 1), Bucket(3, 6, 3)]),
        ],
        # 三套细化桶
        [
            obs("a", 1, 1, 6, [Bucket(1, 6, 4)]),
            obs("b", 1, 1, 6, [Bucket(1, 3, 1), Bucket(4, 6, 3)]),
            obs("c", 1, 1, 6, [Bucket(1, 2, 1), Bucket(3, 4, 1),
                               Bucket(5, 6, 2)]),
        ],
        # 含零计数桶
        [
            obs("a", 1, 1, 5, [Bucket(1, 2, 0), Bucket(3, 5, 3)]),
            obs("b", 1, 1, 5, [Bucket(1, 3, 1), Bucket(4, 5, 2)]),
        ],
        # 含负值域
        [
            obs("a", 1, -3, 2, [Bucket(-3, -1, 2), Bucket(0, 2, 2)]),
            obs("b", 1, -3, 2, [Bucket(-3, 0, 3), Bucket(1, 2, 1)]),
        ],
    ]

    def test_matches_brute_force(self):
        for case in self.CASES:
            with self.subTest(case=[(o.series_id, o.revision) for o in case]):
                model = SegmentModel(case)
                self.assertTrue(model.feasible, model.reason)
                multisets = enumerate_multisets(case)
                self.assertTrue(multisets)
                total = model.total
                for k in range(1, total + 1):
                    got = compute_window_bounds([model], k)
                    want = brute_quantile_bounds(case, k)
                    self.assertEqual(got, want)

    def test_all_brute_feasible_multisets_satisfy_observations(self):
        case = self.CASES[1]
        multisets = enumerate_multisets(case)
        for ms in multisets:
            for o in case:
                for b in o.buckets:
                    cnt = sum(1 for v in ms if b.lo <= v <= b.hi)
                    self.assertEqual(cnt, b.count)


class EdgeCaseTest(unittest.TestCase):
    def test_single_value_domain(self):
        o = obs("a", 1, 7, 7, [Bucket(7, 7, 4)])
        model = SegmentModel([o])
        self.assertTrue(model.feasible)
        self.assertEqual(compute_window_bounds([model], 1), (7, 7))
        self.assertEqual(compute_window_bounds([model], 4), (7, 7))

    def test_zero_samples_segment(self):
        o = obs("a", 1, 1, 3, [Bucket(1, 3, 0)])
        model = SegmentModel([o])
        self.assertTrue(model.feasible)
        self.assertEqual(model.total, 0)
        with self.assertRaises(KOutOfRangeError):
            compute_window_bounds([model], 1)

    def test_wide_single_bucket_freedom(self):
        # 3 个样本全在 [1,4]，无其他约束：kth 可在整个域内移动
        o = obs("a", 1, 1, 4, [Bucket(1, 4, 3)])
        model = SegmentModel([o])
        self.assertTrue(model.feasible)
        self.assertEqual(compute_window_bounds([model], 1), (1, 4))
        self.assertEqual(compute_window_bounds([model], 2), (1, 4))
        self.assertEqual(compute_window_bounds([model], 3), (1, 4))
        # 暴力对拍
        self.assertEqual(
            compute_window_bounds([model], 2), brute_quantile_bounds([o], 2)
        )

    def test_large_counts_bigint(self):
        # 十亿级计数，确保不依赖浮点
        n = 1_000_000_000
        o = obs("a", 1, 1, 2, [Bucket(1, 1, n), Bucket(2, 2, n)])
        model = SegmentModel([o])
        self.assertEqual(model.total, 2 * n)
        self.assertEqual(compute_window_bounds([model], n), (1, 1))
        self.assertEqual(compute_window_bounds([model], n + 1), (2, 2))

    def test_eighty_buckets_limit(self):
        # 合计 80 桶的细化观测
        buckets = [Bucket(i, i, 1) for i in range(1, 81)]
        o = obs("a", 1, 1, 80, buckets)
        model = SegmentModel([o])
        self.assertTrue(model.feasible)
        self.assertEqual(model.total, 80)
        self.assertEqual(compute_window_bounds([model], 40), (40, 40))


if __name__ == "__main__":
    unittest.main()
