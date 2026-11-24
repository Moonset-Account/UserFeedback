"""多组件差分约束（单元计数有自由度）的专项测试。

当观测的桶不能把所有前缀节点用等式连成一个连通组件时，
单元样本数本身存在自由度，需要 Bellman-Ford 最长/最短路求解
各前缀节点的最小/最大可行值。
"""

import unittest

from quantile_merger import Bucket, Observation, SegmentModel
from quantile_merger.window import compute_window_bounds

from tests._brute import brute_quantile_bounds


def obs(series, rev, buckets, lo=1, hi=8, start=0, length=10):
    return Observation("s", start, length, lo, hi, tuple(buckets), series, rev)


class MultiComponentTest(unittest.TestCase):
    def test_partial_coverage_leaves_free_cells(self):
        # 只有一个粗桶 [3,6]=2，值域 [1,8]：
        # 节点 S(0)、S(2)、S(6)、S(8) 中 S(2) 未被等式固定到锚点组件，
        # 单元 [1,2] 与 [7,8] 的计数可以自由取非负值（总数守恒下）
        o = obs("a", 1, [
            Bucket(1, 2, 0),
            Bucket(3, 6, 2),
            Bucket(7, 8, 0),
        ])
        # 上面零桶把节点又连起来了；改用真正只有部分约束的形式
        o_free = Observation(
            "s", 0, 10, 1, 8,
            (Bucket(1, 8, 2),),  # 单桶：全部前缀节点只有锚点与 S(8)
            "a", 1,
        )
        model = SegmentModel([o_free])
        self.assertTrue(model.feasible)
        self.assertEqual(model.total, 2)
        # 两个样本可在 [1,8] 任意布置
        for k in (1, 2):
            self.assertEqual(
                compute_window_bounds([model], k), (1, 8)
            )
            self.assertEqual(
                compute_window_bounds([model], k),
                brute_quantile_bounds([o_free], k),
            )

    def test_mixed_fixed_and_free_cells(self):
        # 桶 [1,2]=1 固定一个样本，剩余 2 个样本在 [3,6] 自由，
        # [7,8] 强制为 0
        o = obs("a", 1, [
            Bucket(1, 2, 1),
            Bucket(3, 6, 2),
            Bucket(7, 8, 0),
        ])
        model = SegmentModel([o])
        self.assertTrue(model.feasible)
        # 暴力对拍每个秩
        for k in (1, 2, 3):
            self.assertEqual(
                compute_window_bounds([model], k),
                brute_quantile_bounds([o], k),
            )
        # k=1 最小为 1（固定样本贴 1），最大为 2（固定样本贴 2）
        self.assertEqual(compute_window_bounds([model], 1), (1, 2))
        # k=3 必然落在自由单元
        self.assertEqual(compute_window_bounds([model], 3), (3, 6))

    def test_prefix_extrema_span_free_region(self):
        o = Observation(
            "s", 0, 10, 1, 5,
            (Bucket(1, 5, 3),),
            "a", 1,
        )
        model = SegmentModel([o])
        # 单元 (0,5] 整体自由：S(3) 可取 0..3
        smin, smax = model.prefix_bounds(3)
        self.assertEqual((smin, smax), (0, 3))
        smin, smax = model.prefix_bounds(1)
        self.assertEqual((smin, smax), (0, 3))
        self.assertEqual(model.prefix_bounds(0), (0, 0))
        self.assertEqual(model.prefix_bounds(5), (3, 3))

    def test_two_layered_observations_with_freedom(self):
        # 两套观测联合后仍保留单元内布置自由度
        o1 = obs("a", 1, [Bucket(1, 4, 2), Bucket(5, 8, 2)], hi=8)
        o2 = obs("b", 1, [Bucket(1, 2, 1), Bucket(3, 8, 3)], hi=8)
        model = SegmentModel([o1, o2])
        self.assertTrue(model.feasible)
        for k in range(1, 5):
            self.assertEqual(
                compute_window_bounds([model], k),
                brute_quantile_bounds([o1, o2], k),
            )

    def test_billion_scale_counts_bigint_safe(self):
        # 十亿级计数跨多个自由单元，验证全程整数精度
        n = 1_000_000_000
        o = Observation(
            "s", 0, 10, 1, 4,
            (Bucket(1, 4, 3 * n),),
            "a", 1,
        )
        model = SegmentModel([o])
        self.assertTrue(model.feasible)
        self.assertEqual(model.total, 3 * n)
        lo, hi = compute_window_bounds([model], 2 * n + 7)
        self.assertEqual((lo, hi), (1, 4))
        # 见证重数精确等于大整数
        alloc = model.build_allocation("min")
        self.assertEqual(alloc.total(), 3 * n)
        entry = alloc.rle[0]
        self.assertEqual(entry.count, 3 * n)
        self.assertEqual((entry.cell_lo, entry.cell_hi), (1, 1))


if __name__ == "__main__":
    unittest.main()
