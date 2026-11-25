"""旋转延迟、寻道期间盘片转动与“刚好错过扇区”时间线的测试。"""

import unittest

from disksim import DiskConfig, DiskScheduler, Request


def base_config(**overrides):
    params = dict(
        cylinders=10,
        sectors_per_track=8,
        ticks_per_sector=1,
        seek_ticks_per_cylinder=1,
        max_queue=8,
        head_cylinder=0,
    )
    params.update(overrides)
    return DiskConfig(**params)


class TestRotationDuringSeek(unittest.TestCase):
    def test_platter_keeps_spinning_during_seek(self):
        # 磁头柱面 0 -> 请求柱面 3、扇区 1：
        # 寻道 3 tick，tick 3 到达时磁头下已是扇区 3，扇区 1 已转过。
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        request = Request(
            rid=1, cylinder=3, sector=1, length=1, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.seek_end, 3)
        # 下一圈扇区 1 的起始边界为 tick 9。
        self.assertEqual(record.read_start, 9)
        self.assertEqual(record.rotational_latency, 6)
        self.assertEqual(record.complete, 10)

        wait_event = next(e for e in result.timeline if e.kind == "rotational_wait")
        self.assertEqual(wait_event.time, 3)
        self.assertEqual(wait_event.extra["head_sector"], 3)
        self.assertEqual(wait_event.extra["target_sector"], 1)


class TestJustMissedSector(unittest.TestCase):
    def test_miss_at_sector_end_boundary(self):
        # 柱面 0 -> 5 寻道 5 tick；tick 5 恰为扇区 5 的起始边界，
        # 目标扇区 4 的结束边界就是 tick 5 —— 刚好错过，等一整圈。
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        request = Request(
            rid=1, cylinder=5, sector=4, length=1, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.seek_end, 5)
        self.assertEqual(record.read_start, 12)  # 下一圈 tick 8+4
        self.assertEqual(record.rotational_latency, 7)
        self.assertTrue(record.just_missed)

        miss = next(e for e in result.timeline if e.kind == "sector_miss")
        self.assertEqual(miss.time, 5)
        self.assertEqual(miss.rid, 1)
        self.assertEqual(miss.extra["head_sector"], 5)
        self.assertEqual(miss.extra["target_sector"], 4)
        self.assertEqual(miss.extra["latency"], 7)

        # sector_miss 与 rotational_wait 同刻出现，且先等待后读取。
        kinds = [e.kind for e in result.timeline]
        self.assertIn("sector_miss", kinds)
        self.assertLess(kinds.index("sector_miss"), kinds.index("read"))

    def test_just_missed_with_larger_ticks_per_sector(self):
        # ticks_per_sector=2：一圈 16 tick；seek_ticks_per_cylinder=2，
        # 柱面 0->4 寻道 8 tick 后到达 tick 8，即槽 4 的起始；
        # 目标槽 3 的结束边界正是 tick 8 —— 刚好错过。
        config = base_config(ticks_per_sector=2, seek_ticks_per_cylinder=2)
        scheduler = DiskScheduler(config)
        request = Request(
            rid=1, cylinder=4, sector=3, length=1, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.seek_end, 8)
        self.assertEqual(record.read_start, 22)  # 下一圈 16 + 3*2
        self.assertEqual(record.rotational_latency, 14)
        self.assertTrue(record.just_missed)
        self.assertTrue(
            any(e.kind == "sector_miss" for e in result.timeline)
        )

    def test_partial_latency_is_not_just_missed(self):
        # 寻道 3 tick 后扇区 3 在头下，目标扇区 5：只等 2 tick。
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        request = Request(
            rid=1, cylinder=3, sector=5, length=1, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.rotational_latency, 2)
        self.assertFalse(record.just_missed)
        self.assertFalse(
            any(e.kind == "sector_miss" for e in result.timeline)
        )

    def test_zero_latency_produces_no_wait_event(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        # 柱面 3、扇区 3：tick 3 寻道结束且扇区正好对齐。
        request = Request(
            rid=1, cylinder=3, sector=3, length=1, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.rotational_latency, 0)
        self.assertFalse(record.just_missed)
        kinds = [e.kind for e in result.timeline]
        self.assertNotIn("rotational_wait", kinds)
        self.assertNotIn("sector_miss", kinds)


class TestMultiSectorRead(unittest.TestCase):
    def test_length_multiplies_read_time(self):
        scheduler = DiskScheduler(base_config(head_cylinder=2))
        request = Request(
            rid=1, cylinder=2, sector=1, length=4, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.read_start, 1)
        self.assertEqual(record.complete, 5)  # 4 扇区 × 1 tick

    def test_length_with_ticks_per_sector(self):
        config = base_config(ticks_per_sector=3)
        scheduler = DiskScheduler(config)
        request = Request(
            rid=1, cylinder=0, sector=2, length=2, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.read_start, 6)   # 槽 2 × 3 tick
        self.assertEqual(record.complete, 12)   # 再加 2 × 3 tick

    def test_cross_slot_arrival_waits_for_boundary(self):
        # ticks_per_sector=2，槽 s 的边界为 tick 2s；tick 7 落在槽 3 中间，
        # 目标槽 3 若此刻未对齐则需等到下一圈的边界 tick 22。
        # seek_ticks_per_cylinder=2：柱面 3 -> 6 寻道 3 柱面 = 6 tick。
        config = base_config(
            ticks_per_sector=2, seek_ticks_per_cylinder=2, head_cylinder=3
        )
        scheduler = DiskScheduler(config)
        request = Request(
            rid=1, cylinder=6, sector=3, length=1, arrival=1, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        # 空闲到 tick 1，寻道至 tick 7（槽 3 中间），下一圈槽 3 边界 tick 22。
        self.assertEqual(record.seek_start, 1)
        self.assertEqual(record.seek_end, 7)
        self.assertEqual(record.read_start, 22)
        self.assertEqual(record.rotational_latency, 15)


class TestSeekCostScaling(unittest.TestCase):
    def test_seek_ticks_per_cylinder_scaling(self):
        config = base_config(seek_ticks_per_cylinder=3)
        scheduler = DiskScheduler(config)
        request = Request(
            rid=1, cylinder=2, sector=0, length=1, arrival=0, deadline=99
        )
        result = scheduler.run([request])
        record = result.record_of(1)
        self.assertEqual(record.seek_distance, 2)
        self.assertEqual(record.seek_end, 6)
        # tick 6 时槽 6 -> 扇区 0 下一圈 tick 8。
        self.assertEqual(record.read_start, 8)


if __name__ == "__main__":
    unittest.main()
