import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evidence_locator import (
    EditLog,
    EditLogError,
    ProgramGapError,
    timecode as tc,
)

DAY = tc.FRAMES_PER_DAY


class SimpleRoundTripTest(unittest.TestCase):
    def test_locate_program_and_back(self):
        log = EditLog()
        log.add_event(
            "R1",
            0, "00:00:10;00",
            0, "00:00:20;00",
            program_in_day=0, program_in="01:00:00;00",
        )
        hits = log.locate_program("R1", 0, "00:00:15;00")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].timecode, "01:00:05;00")
        self.assertEqual(hits[0].reel, "R1")
        self.assertEqual((hits[0].source_day_seq, hits[0].source_frame), (0, 450))

        src = log.locate_source(0, "01:00:05;00")
        self.assertEqual(src.reel, "R1")
        self.assertEqual(src.timecode, "00:00:15;00")
        self.assertEqual((src.program_day_seq, src.program_frame),
                         (0, tc.parse_timecode("01:00:05;00")))

    def test_int_frames_accepted(self):
        log = EditLog()
        log.add_event("R1", 0, 300, 0, 600, program_in_day=0, program_in=0)
        hits = log.locate_program("R1", 0, 450)
        self.assertEqual([h.frame for h in hits], [150])
        self.assertEqual(log.locate_source(0, 150).frame, 450)


class MidnightCrossingTest(unittest.TestCase):
    def test_source_crosses_midnight(self):
        log = EditLog()
        # 素材从日序0的 23:59:50;00 跨午夜到日序1的 00:00:10;00，共 600 帧
        log.add_event(
            "R1",
            0, "23:59:50;00",
            1, "00:00:10;00",
            program_in_day=0, program_in=0,
        )
        # 跨日之后的一侧：日序1的 00:00:05;00
        hits = log.locate_program("R1", 1, "00:00:05;00")
        self.assertEqual([h.frame for h in hits], [450])
        # 跨日之前的一侧：日序0的 23:59:55;00
        hits = log.locate_program("R1", 0, "23:59:55;00")
        self.assertEqual([h.frame for h in hits], [150])
        # 反向：成片帧 450 应回到日序1的素材帧
        src = log.locate_source(0, 450)
        self.assertEqual((src.reel, src.day_seq, src.frame), ("R1", 1, 150))
        src = log.locate_source(0, 0)
        self.assertEqual((src.day_seq, src.timecode), (0, "23:59:50;00"))

    def test_program_crosses_midnight(self):
        log = EditLog()
        log.add_event(
            "R1", 0, 0, 0, 600,
            program_in_day=0, program_in="23:59:50;00",
        )
        hits = log.locate_program("R1", 0, 450)
        self.assertEqual((hits[0].day_seq, hits[0].timecode), (1, "00:00:05;00"))
        # 反向定位跨日后的成片位置
        src = log.locate_source(1, "00:00:05;00")
        self.assertEqual((src.reel, src.day_seq, src.frame), ("R1", 0, 450))

    def test_multi_day_source(self):
        log = EditLog()
        # 素材跨两个完整午夜：日序2中午 -> 日序4中午
        log.add_event("R9", 2, "12:00:00;00", 4, "12:00:00;00",
                      program_in_day=0, program_in=0)
        # 日序3中午的素材帧落在成片日序1的 00:00:00;00（成片同样跨日）
        hits = log.locate_program("R9", 3, "12:00:00;00")
        self.assertEqual([(h.day_seq, h.frame) for h in hits], [(1, 0)])
        src = log.locate_source(1, "00:00:00;00")
        self.assertEqual((src.day_seq, src.timecode), (3, "12:00:00;00"))


class ReusedSourceTest(unittest.TestCase):
    def test_source_copied_into_two_program_intervals(self):
        log = EditLog()
        log.add_event("R1", 0, 1000, 0, 2000, program_in_day=0, program_in=0)
        log.add_event("R1", 0, 1000, 0, 2000, program_in_day=0, program_in=5000)
        hits = log.locate_program("R1", 0, 1500)
        self.assertEqual([h.frame for h in hits], [500, 5500])
        # 两个成片区间各自反查回同一素材帧
        for pf in (500, 5500):
            src = log.locate_source(0, pf)
            self.assertEqual((src.reel, src.frame), ("R1", 1500))

    def test_no_hit_returns_empty_list(self):
        log = EditLog()
        log.add_event("R1", 0, 0, 0, 100, program_in_day=0, program_in=0)
        self.assertEqual(log.locate_program("R1", 0, 5000), [])
        self.assertEqual(log.locate_program("OTHER", 0, 50), [])


class HalfOpenIntervalTest(unittest.TestCase):
    def test_consecutive_edits_unique_ownership(self):
        log = EditLog()
        log.add_event("A", 0, 0, 0, 100, program_in_day=0, program_in=0)
        log.add_event("B", 0, 0, 0, 100, program_in_day=0, program_in=100)
        # 剪接点帧 100 属于后一段（含头不含尾）
        self.assertEqual(log.locate_source(0, 99).reel, "A")
        self.assertEqual(log.locate_source(0, 100).reel, "B")
        self.assertEqual(log.locate_source(0, 199).reel, "B")
        # 出帧本身不属于该事件
        with self.assertRaises(ProgramGapError):
            log.locate_source(0, 200)

    def test_source_side_half_open(self):
        log = EditLog()
        log.add_event("A", 0, 0, 0, 100, program_in_day=0, program_in=0)
        log.add_event("A", 0, 100, 0, 200, program_in_day=0, program_in=1000)
        # 素材帧 100 只属于第二段
        hits = log.locate_program("A", 0, 100)
        self.assertEqual([h.frame for h in hits], [1000])
        hits = log.locate_program("A", 0, 99)
        self.assertEqual([h.frame for h in hits], [99])

    def test_touching_program_intervals_allowed(self):
        log = EditLog()
        log.add_event("A", 0, 0, 0, 100, program_in_day=0, program_in=0)
        log.add_event("B", 0, 0, 0, 100, program_in_day=0, program_in=100)
        self.assertEqual(len(log.events), 2)


class ValidationTest(unittest.TestCase):
    def test_program_overlap_rejected(self):
        log = EditLog()
        log.add_event("A", 0, 0, 0, 100, program_in_day=0, program_in=0)
        for bad_in in (0, 50, 99):
            with self.subTest(bad_in=bad_in):
                with self.assertRaises(EditLogError) as ctx:
                    log.add_event("B", 0, 0, 0, 100,
                                  program_in_day=0, program_in=bad_in)
                self.assertIn("重叠", str(ctx.exception))

    def test_source_range_must_be_forward(self):
        log = EditLog()
        with self.assertRaises(EditLogError):
            log.add_event("A", 0, 100, 0, 100, program_in_day=0, program_in=0)
        with self.assertRaises(EditLogError):
            log.add_event("A", 1, 0, 0, DAY - 1, program_in_day=0, program_in=0)

    def test_illegal_timecode_reports_reason(self):
        log = EditLog()
        with self.assertRaises(tc.DroppedFrameNumberError) as ctx:
            log.add_event("A", 0, "00:03:00;01", 0, "00:04:00;00")
        self.assertIn("跳过", str(ctx.exception))
        with self.assertRaises(tc.DroppedFrameNumberError):
            log.locate_program("A", 0, "00:03:00;00")
        with self.assertRaises(tc.DroppedFrameNumberError):
            log.locate_source(0, "00:03:00;01")

    def test_bad_arguments_rejected(self):
        log = EditLog()
        with self.assertRaises(EditLogError):
            log.add_event("", 0, 0, 0, 100)
        with self.assertRaises(EditLogError):
            log.add_event("A", -1, 0, 0, 100)
        with self.assertRaises(EditLogError):
            log.add_event("A", 0, 0, 0, DAY + 1)
        with self.assertRaises(EditLogError):
            log.locate_program("A", -1, 0)


class GapTest(unittest.TestCase):
    def test_gap_reports_reason(self):
        log = EditLog()
        log.add_event("A", 0, 0, 0, 100, program_in_day=0, program_in=0)
        with self.assertRaises(ProgramGapError) as ctx:
            log.locate_source(0, 500)
        self.assertIn("不在任何剪辑事件", str(ctx.exception))

    def test_empty_log(self):
        log = EditLog()
        with self.assertRaises(ProgramGapError):
            log.locate_source(0, 0)
        self.assertEqual(log.locate_program("A", 0, 0), [])


if __name__ == "__main__":
    unittest.main()
