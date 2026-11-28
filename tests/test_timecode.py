import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evidence_locator import timecode as tc


class KnownBoundaryTest(unittest.TestCase):
    """手工核算的边界换算对。"""

    CASES = [
        ((0, 0, 0, 0), 0),
        ((0, 0, 0, 29), 29),
        ((0, 0, 1, 0), 30),
        ((0, 0, 59, 29), 1799),
        ((0, 1, 0, 2), 1800),    # 首个丢帧分钟的开头（00/01 被跳过）
        ((0, 1, 0, 29), 1827),
        ((0, 1, 1, 0), 1828),
        ((0, 9, 59, 29), 17981),  # 十分钟周期末尾
        ((0, 10, 0, 0), 17982),   # 整十分钟不丢帧
        ((0, 10, 0, 1), 17983),
        ((1, 0, 0, 0), 107892),
        ((23, 59, 59, 29), 2589407),  # 一天最后一帧
    ]

    def test_components_to_frame(self):
        for comp, n in self.CASES:
            with self.subTest(comp=comp):
                self.assertEqual(tc.components_to_frame(*comp), n)

    def test_frame_to_components(self):
        for comp, n in self.CASES:
            with self.subTest(n=n):
                self.assertEqual(tc.frame_to_components(n), comp)


class RoundTripTest(unittest.TestCase):
    def test_round_trip_samples(self):
        samples = (
            list(range(0, 4000))
            + list(range(17970, 18000))
            + list(range(107880, 107904))
            + list(range(2589390, 2589408))
        )
        for n in samples:
            with self.subTest(n=n):
                self.assertEqual(tc.components_to_frame(*tc.frame_to_components(n)), n)

    def test_constants(self):
        self.assertEqual(tc.FRAMES_PER_MINUTE, 1798)
        self.assertEqual(tc.FRAMES_PER_10_MINUTES, 17982)
        self.assertEqual(tc.FRAMES_PER_HOUR, 107892)
        self.assertEqual(tc.FRAMES_PER_DAY, 2589408)


class DroppedNumberTest(unittest.TestCase):
    def test_dropped_numbers_rejected_with_reason(self):
        for minute in (1, 2, 3, 9, 11, 21, 59):
            for f in (0, 1):
                with self.subTest(minute=minute, f=f):
                    with self.assertRaises(tc.DroppedFrameNumberError) as ctx:
                        tc.components_to_frame(0, minute, 0, f)
                    self.assertIn("跳过", str(ctx.exception))

    def test_ten_minute_marks_not_dropped(self):
        for minute in (0, 10, 20, 30, 40, 50):
            for f in (0, 1):
                with self.subTest(minute=minute, f=f):
                    n = tc.components_to_frame(0, minute, 0, f)
                    self.assertEqual(tc.frame_to_components(n), (0, minute, 0, f))

    def test_drop_only_at_second_zero(self):
        # 非整十分钟但秒不为 00 时，00/01 是合法编号
        self.assertEqual(tc.components_to_frame(0, 3, 5, 0),
                         tc.components_to_frame(0, 3, 4, 29) + 1)


class RangeErrorTest(unittest.TestCase):
    def test_out_of_range_components(self):
        bad = [
            (24, 0, 0, 0),
            (-1, 0, 0, 0),
            (0, 60, 0, 0),
            (0, -1, 0, 0),
            (0, 0, 60, 0),
            (0, 0, -1, 0),
            (0, 0, 0, 30),   # 每秒只有 30 个编号
            (0, 0, 0, -1),
        ]
        for comp in bad:
            with self.subTest(comp=comp):
                with self.assertRaises(tc.TimecodeError) as ctx:
                    tc.components_to_frame(*comp)
                self.assertTrue(str(ctx.exception))

    def test_out_of_range_frame(self):
        with self.assertRaises(tc.TimecodeError):
            tc.frame_to_components(-1)
        with self.assertRaises(tc.TimecodeError):
            tc.frame_to_components(tc.FRAMES_PER_DAY)


class ParseFormatTest(unittest.TestCase):
    def test_parse_accepts_colon_and_semicolon(self):
        self.assertEqual(tc.parse_timecode("00:01:00;02"), 1800)
        self.assertEqual(tc.parse_timecode("00:01:00:02"), 1800)

    def test_format_uses_semicolon(self):
        self.assertEqual(tc.format_timecode(1800), "00:01:00;02")
        self.assertEqual(tc.format_timecode(0), "00:00:00;00")
        self.assertEqual(tc.format_timecode(2589407), "23:59:59;29")

    def test_parse_format_round_trip(self):
        for n in (0, 1800, 17982, 107892, 2589407):
            self.assertEqual(tc.parse_timecode(tc.format_timecode(n)), n)

    def test_parse_rejects_dropped_number(self):
        with self.assertRaises(tc.DroppedFrameNumberError):
            tc.parse_timecode("00:07:00;01")

    def test_parse_rejects_garbage(self):
        for text in ("not a timecode", "00:00:00", "00:00:00:00:00", "", 123):
            with self.subTest(text=text):
                with self.assertRaises(tc.TimecodeError):
                    tc.parse_timecode(text)


class ActualRateTest(unittest.TestCase):
    def test_actual_frame_rate(self):
        self.assertEqual(tc.ACTUAL_FRAME_RATE, Fraction(30000, 1001))

    def test_frame_to_seconds(self):
        self.assertEqual(tc.frame_to_seconds(0), Fraction(0))
        # 30 个编号对应的真实时长略大于 1 秒
        self.assertEqual(tc.frame_to_seconds(30), Fraction(1001, 1000))
        # 一个时码日的真实时长（比 86400 秒略短，丢帧编号只是编号补偿）
        self.assertEqual(
            tc.frame_to_seconds(tc.FRAMES_PER_DAY),
            Fraction(tc.FRAMES_PER_DAY * 1001, 30000),
        )
        self.assertLess(tc.frame_to_seconds(tc.FRAMES_PER_DAY), 86400)


if __name__ == "__main__":
    unittest.main()
