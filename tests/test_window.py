"""多时间段窗口测试：正常、边界与跨段交错，含暴力对拍。"""

import unittest

from quantile_merger import (
    Bucket,
    Observation,
    ValidationError,
    VersionedStore,
)
from quantile_merger.model import SegmentModel
from quantile_merger.window import compute_window_bounds

from tests._brute import brute_window_bounds


def obs(series, rev, buckets, start, length=10, lo=1, hi=6, source="s"):
    return Observation(source, start, length, lo, hi, tuple(buckets), series, rev)


class MultiSegmentWindowTest(unittest.TestCase):
    def setUp(self):
        # 两段：[0,10) 与 [10,20)，各 3 个样本
        self.seg0 = [
            obs("a", 1, [Bucket(1, 3, 2), Bucket(4, 6, 1)], start=0),
            obs("b", 1, [Bucket(1, 2, 1), Bucket(3, 6, 2)], start=0),
        ]
        self.seg1 = [
            obs("a", 1, [Bucket(1, 3, 1), Bucket(4, 6, 2)], start=10),
            obs("b", 1, [Bucket(1, 4, 2), Bucket(5, 6, 1)], start=10),
        ]
        self.m0 = SegmentModel(self.seg0)
        self.m1 = SegmentModel(self.seg1)

    def test_brute_force_all_ranks(self):
        total = self.m0.total + self.m1.total
        self.assertEqual(total, 6)
        for k in range(1, total + 1):
            got = compute_window_bounds([self.m0, self.m1], k)
            want = brute_window_bounds([self.seg0, self.seg1], k)
            self.assertEqual(got, want, f"k={k}")

    def test_store_window_query_matches_models(self):
        store = VersionedStore()
        for o in self.seg0 + self.seg1:
            store.ingest(o)
        result = store.query_window(
            "s", 0, 20, 4, segment_starts=[0, 10]
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.total_samples, 6)
        want = compute_window_bounds([self.m0, self.m1], 4)
        self.assertEqual((result.min_kth, result.max_kth), want)

    def test_auto_discover_segment_starts(self):
        # 不显式给 segment_starts，存储按窗口自动发现且忽略窗外段
        store = VersionedStore()
        for o in self.seg0 + self.seg1:
            store.ingest(o)
        # 窗外第三段，不应被自动发现
        store.ingest(
            obs(
                "a", 1, [Bucket(1, 6, 1)], start=20,
            )
        )
        result = store.query_window("s", 0, 20, 4)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.total_samples, 6)
        want = compute_window_bounds([self.m0, self.m1], 4)
        self.assertEqual((result.min_kth, result.max_kth), want)

    def test_window_boundaries_must_hit_segment_bounds(self):
        store = VersionedStore()
        for o in self.seg0 + self.seg1:
            store.ingest(o)
        with self.assertRaises(ValidationError):
            store.query_window("s", 0, 15, 1, segment_starts=[0, 10])

    def test_partial_window_requery_after_correction(self):
        store = VersionedStore()
        for o in self.seg0 + self.seg1:
            store.ingest(o)
        before = store.query_window(
            "s", 10, 20, 2, segment_starts=[10]
        )
        # 迟到更正只影响第二段
        store.ingest(
            obs(
                "a", 2,
                [Bucket(1, 3, 3), Bucket(4, 6, 0)], start=10,
            )
        )
        after = store.query_window(
            "s", 10, 20, 2, segment_starts=[10]
        )
        self.assertNotEqual(
            (before.min_kth, before.max_kth),
            (after.min_kth, after.max_kth),
        )
        # 旧见证仍可在旧版本上验算
        from quantile_merger import verify_witness

        report = verify_witness(before.min_witness)
        self.assertTrue(report.ok, report.errors)

    def test_one_infeasible_segment_poisons_window(self):
        store = VersionedStore()
        for o in self.seg0:
            store.ingest(o)
        store.ingest(self.seg1[0])
        bad = obs(
            "bad", 1, [Bucket(1, 3, 9), Bucket(4, 6, 9)], start=10
        )
        store.ingest(bad)
        result = store.query_window(
            "s", 0, 20, 1, segment_starts=[0, 10]
        )
        self.assertEqual(result.status, "infeasible")
        self.assertEqual(len(result.mus_reports), 1)
        self.assertEqual(result.mus_reports[0].segment_start, 10)

    def test_zero_sample_segment_in_window(self):
        seg_empty = [
            obs("a", 1, [Bucket(1, 3, 0), Bucket(4, 6, 0)], start=20)
        ]
        m_empty = SegmentModel(seg_empty)
        total = self.m0.total + self.m1.total + m_empty.total
        self.assertEqual(total, 6)
        for k in range(1, 7):
            got = compute_window_bounds(
                [self.m0, self.m1, m_empty], k
            )
            want = brute_window_bounds(
                [self.seg0, self.seg1, seg_empty], k
            )
            self.assertEqual(got, want)

    def test_disjoint_value_domains_across_segments(self):
        # 两段值域不相交：合并第 k 小的顺序被值域强制
        low = [
            obs(
                "a", 1, [Bucket(1, 2, 2), Bucket(3, 4, 0)],
                start=0, lo=1, hi=4,
            )
        ]
        high = [
            obs(
                "a", 1, [Bucket(8, 9, 1), Bucket(10, 10, 1)],
                start=10, lo=8, hi=10,
            )
        ]
        m_low = SegmentModel(low)
        m_high = SegmentModel(high)
        # 两个低值样本在 [1,2] 自由布置：k=2 最小为 1（都放 1）、最大为 2
        self.assertEqual(
            compute_window_bounds([m_low, m_high], 2), (1, 2)
        )
        self.assertEqual(
            compute_window_bounds([m_low, m_high], 3), (8, 9)
        )


if __name__ == "__main__":
    unittest.main()
