"""区间运算基础测试：裁剪、并集合并、重叠去重、超量尾部、时长解析。"""

import unittest
from datetime import timedelta

from release_gate.intervals import (
    Interval,
    clip,
    format_duration,
    intersect,
    merge,
    parse_duration,
    parse_dt,
    tail_after_budget,
    union_duration,
)

from factory import dt, iv


class TestIntervalMath(unittest.TestCase):
    def test_merge_overlapping_and_adjacent_counts_once(self):
        merged = merge(
            [
                iv("2026-11-02T00:00:00Z", "2026-11-02T06:00:00Z"),
                iv("2026-11-02T03:00:00Z", "2026-11-02T09:00:00Z"),
                iv("2026-11-02T09:00:00Z", "2026-11-02T10:00:00Z"),
            ]
        )
        self.assertEqual(
            merged, [iv("2026-11-02T00:00:00Z", "2026-11-02T10:00:00Z")]
        )
        # 重叠的 3 小时与相邻区间都不能重复计时。
        self.assertEqual(union_duration(merged), timedelta(hours=10))

    def test_union_duration_dedup_disjoint_and_overlap(self):
        intervals = [
            iv("2026-11-02T00:00:00Z", "2026-11-02T05:00:00Z"),
            iv("2026-11-02T03:00:00Z", "2026-11-02T08:00:00Z"),
            iv("2026-11-03T00:00:00Z", "2026-11-03T02:00:00Z"),
        ]
        # 并集：[00,08)=8h，外加次日 2h，共 10h；朴素求和会得到 12h。
        self.assertEqual(union_duration(intervals), timedelta(hours=10))

    def test_clip_to_window(self):
        window = iv("2026-11-01T00:00:00Z", "2026-11-11T00:00:00Z")
        self.assertEqual(
            clip(iv("2026-10-30T00:00:00Z", "2026-11-02T00:00:00Z"), window),
            iv("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"),
        )
        self.assertIsNone(
            clip(iv("2026-11-12T00:00:00Z", "2026-11-13T00:00:00Z"), window)
        )

    def test_intersect(self):
        a = [iv("2026-11-02T00:00:00Z", "2026-11-02T08:00:00Z")]
        b = [
            iv("2026-11-02T03:00:00Z", "2026-11-02T05:00:00Z"),
            iv("2026-11-02T07:00:00Z", "2026-11-02T10:00:00Z"),
        ]
        self.assertEqual(
            intersect(a, b),
            [
                iv("2026-11-02T03:00:00Z", "2026-11-02T05:00:00Z"),
                iv("2026-11-02T07:00:00Z", "2026-11-02T08:00:00Z"),
            ],
        )

    def test_tail_after_budget(self):
        intervals = merge(
            [
                iv("2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z"),
                iv("2026-11-06T04:00:00Z", "2026-11-06T06:00:00Z"),
            ]
        )
        # 已用 3h、额度 4h：只剩 1h 预算，超出部分从第一段 1h 处开始。
        overage = tail_after_budget(
            intervals, used=timedelta(hours=3), budget=timedelta(hours=4)
        )
        self.assertEqual(
            overage,
            [
                iv("2026-11-06T01:00:00Z", "2026-11-06T02:00:00Z"),
                iv("2026-11-06T04:00:00Z", "2026-11-06T06:00:00Z"),
            ],
        )
        # 预算充足时没有超量段。
        self.assertEqual(
            tail_after_budget(
                intervals, used=timedelta(0), budget=timedelta(hours=10)
            ),
            [],
        )
        # 预算已耗尽时全部为超量段。
        self.assertEqual(
            tail_after_budget(
                intervals, used=timedelta(hours=4), budget=timedelta(hours=4)
            ),
            intervals,
        )

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            Interval(dt("2026-11-02T01:00:00Z"), dt("2026-11-02T00:00:00Z"))

    def test_duration_parse_and_format(self):
        self.assertEqual(parse_duration("PT1H30M"), timedelta(hours=1.5))
        self.assertEqual(parse_duration("P2D"), timedelta(days=2))
        self.assertEqual(parse_duration(90), timedelta(seconds=90))
        self.assertEqual(parse_duration(timedelta(minutes=5)), timedelta(minutes=5))
        with self.assertRaises(ValueError):
            parse_duration("not-a-duration")
        with self.assertRaises(TypeError):
            parse_duration(True)
        self.assertEqual(format_duration(timedelta(hours=1, minutes=30)), "PT1H30M")
        self.assertEqual(format_duration(timedelta(days=2)), "P2D")
        self.assertEqual(
            parse_duration(format_duration(timedelta(seconds=7265))),
            timedelta(seconds=7265),
        )

    def test_naive_datetime_treated_as_utc(self):
        naive = parse_dt("2026-11-02T00:00:00")
        zoned = parse_dt("2026-11-02T00:00:00Z")
        self.assertEqual(naive, zoned)
        self.assertIsNotNone(naive.tzinfo)


if __name__ == "__main__":
    unittest.main()
