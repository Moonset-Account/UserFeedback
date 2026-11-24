"""随机化对拍测试：随机生成分层直方图，引擎结果与暴力枚举逐秩比对。"""

import random
import unittest

from quantile_merger import Bucket, Observation, SegmentModel
from quantile_merger.model import KOutOfRangeError
from quantile_merger.window import compute_window_bounds

from tests._brute import (
    brute_quantile_bounds,
    brute_window_bounds,
    enumerate_multisets,
)


def random_partition_cuts(rng, lo, hi, max_buckets):
    """在 [lo,hi] 上生成随机无缝桶切分点（桶均为非空整数区间）。"""
    width = hi - lo + 1
    # 内部切分点取 lo+1..hi-1（共 width-1 个），故桶数上限为 width
    pool = list(range(lo + 1, hi))
    n_buckets = rng.randint(1, min(max_buckets, len(pool) + 1))
    internal = sorted(rng.sample(pool, n_buckets - 1))
    bounds = [lo - 1] + internal + [hi]
    return bounds


def random_observation(rng, series, rev, lo, hi, total, max_buckets):
    bounds = random_partition_cuts(rng, lo, hi, max_buckets)
    n_buckets = len(bounds) - 1
    # 随机把 total 拆成各桶非负计数
    counts = [0] * n_buckets
    for _ in range(total):
        counts[rng.randrange(n_buckets)] += 1
    buckets = tuple(
        Bucket(bounds[i] + 1, bounds[i + 1], counts[i])
        for i in range(n_buckets)
    )
    return Observation("s", 0, 10, lo, hi, buckets, series, rev)


class RandomizedSingleSegmentTest(unittest.TestCase):
    def test_random_feasible_layering(self):
        rng = random.Random(20261124)
        trials = 60
        for trial in range(trials):
            lo, hi = 1, rng.randint(3, 7)
            total = rng.randint(0, 6)
            base = random_observation(rng, "base", 1, lo, hi, total, 4)
            # 额外观测用不同桶边界、但由 base 的某个可行多重集生成，
            # 保证联合可行
            multiset = rng.choice(enumerate_multisets([base]))
            extras = []
            for s in range(rng.randint(1, 2)):
                bounds = random_partition_cuts(rng, lo, hi, 4)
                buckets = []
                for i in range(len(bounds) - 1):
                    a, b = bounds[i] + 1, bounds[i + 1]
                    c = sum(1 for v in multiset if a <= v <= b)
                    buckets.append(Bucket(a, b, c))
                extras.append(
                    Observation(
                        "s", 0, 10, lo, hi, tuple(buckets),
                        f"x{s}", 1,
                    )
                )
            all_obs = [base] + extras
            model = SegmentModel(all_obs)
            self.assertTrue(model.feasible, model.reason)
            if total == 0:
                with self.assertRaises(KOutOfRangeError):
                    compute_window_bounds([model], 1)
                continue
            for k in range(1, total + 1):
                got = compute_window_bounds([model], k)
                want = brute_quantile_bounds(all_obs, k)
                self.assertEqual(got, want, f"trial={trial} k={k}")

    def test_random_infeasible_detection(self):
        rng = random.Random(424242)
        detected = 0
        for _ in range(40):
            lo, hi = 1, rng.randint(3, 6)
            o1 = random_observation(
                rng, "a", 1, lo, hi, rng.randint(1, 5), 3
            )
            o2 = random_observation(
                rng, "b", 1, lo, hi, rng.randint(1, 5), 3
            )
            model = SegmentModel([o1, o2])
            # 引擎判定与"是否存在联合可行多重集"完全一致
            brute = enumerate_multisets([o1, o2])
            if model.feasible:
                self.assertTrue(brute)
            else:
                detected += 1
                self.assertFalse(brute)
        self.assertGreater(detected, 0)


class RandomizedWindowTest(unittest.TestCase):
    def test_two_segment_random_windows(self):
        rng = random.Random(99)
        for _ in range(25):
            seg_obs = []
            models = []
            totals = []
            for start in (0, 10):
                lo, hi = 1, rng.randint(3, 6)
                total = rng.randint(1, 5)
                base = random_observation(
                    rng, "base", 1, lo, hi, total, 3
                )
                multiset = rng.choice(enumerate_multisets([base]))
                bounds = random_partition_cuts(rng, lo, hi, 3)
                buckets = tuple(
                    Bucket(
                        bounds[i] + 1,
                        bounds[i + 1],
                        sum(
                            1
                            for v in multiset
                            if bounds[i] + 1 <= v <= bounds[i + 1]
                        ),
                    )
                    for i in range(len(bounds) - 1)
                )
                extra = Observation(
                    "s", start, 10, lo, hi, buckets, "x", 1
                )
                group = [
                    Observation(
                        base.source, start, 10, base.lo, base.hi,
                        base.buckets, "base", 1,
                    ),
                    extra,
                ]
                seg_obs.append(group)
                models.append(SegmentModel(group))
                totals.append(total)
            grand_total = sum(totals)
            for k in range(1, grand_total + 1):
                got = compute_window_bounds(models, k)
                want = brute_window_bounds(seg_obs, k)
                self.assertEqual(got, want, f"k={k}")


if __name__ == "__main__":
    unittest.main()
