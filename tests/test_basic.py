"""基础计时、SCAN 方向规则与同柱面扇区顺序的测试。"""

import unittest

from disksim import DiskConfig, DiskScheduler, Request, plan_service
from disksim.model import MODE_NORMAL


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


class TestEarliestSectorTime(unittest.TestCase):
    def setUp(self):
        self.config = base_config()

    def test_already_aligned(self):
        # tick 0 时扇区 0 正在磁头下。
        from disksim.scheduler import earliest_sector_time

        self.assertEqual(earliest_sector_time(self.config, 0, 0), 0)
        self.assertEqual(earliest_sector_time(self.config, 0, 3), 3)

    def test_within_same_rotation(self):
        from disksim.scheduler import earliest_sector_time

        self.assertEqual(earliest_sector_time(self.config, 2, 5), 5)
        self.assertEqual(earliest_sector_time(self.config, 5, 5), 5)

    def test_wrap_to_next_rotation(self):
        from disksim.scheduler import earliest_sector_time

        # 扇区 2 已过，等下一圈的 tick 10。
        self.assertEqual(earliest_sector_time(self.config, 6, 2), 10)
        # 恰好在扇区结束边界到达：tick 5 是扇区 4 的起始，扇区 4 已错过。
        self.assertEqual(earliest_sector_time(self.config, 5, 4), 12)

    def test_ticks_per_sector_gt_one(self):
        from disksim.scheduler import earliest_sector_time

        config = base_config(ticks_per_sector=2)  # 一圈 16 tick
        self.assertEqual(earliest_sector_time(config, 0, 3), 6)
        self.assertEqual(earliest_sector_time(config, 7, 1), 18)
        self.assertEqual(earliest_sector_time(config, 6, 3), 6)


class TestSingleRequest(unittest.TestCase):
    def setUp(self):
        self.scheduler = DiskScheduler(base_config(head_cylinder=2))

    def test_full_timeline_and_record(self):
        request = Request(
            rid=1, cylinder=5, sector=4, length=2, arrival=0, deadline=100
        )
        result = self.scheduler.run([request])

        self.assertEqual(result.served_order, (1,))
        self.assertEqual(len(result.records), 1)
        record = result.record_of(1)
        # 寻道 3 tick（tick 3 到达），等扇区 4 至 tick 4，读 2 tick。
        self.assertEqual(record.seek_start, 0)
        self.assertEqual(record.seek_end, 3)
        self.assertEqual(record.read_start, 4)
        self.assertEqual(record.complete, 6)
        self.assertEqual(record.seek_distance, 3)
        self.assertEqual(record.rotational_latency, 1)
        self.assertTrue(record.on_time)
        self.assertFalse(record.just_missed)

        kinds = [e.kind for e in result.timeline]
        self.assertEqual(
            kinds, ["arrival", "decision", "seek", "rotational_wait", "read"]
        )

    def test_deadline_miss_recorded(self):
        request = Request(
            rid=1, cylinder=9, sector=7, length=1, arrival=0, deadline=5
        )
        result = DiskScheduler(base_config(head_cylinder=0)).run([request])
        record = result.record_of(1)
        # 寻道 9 tick，tick 9 时扇区 1 在头下，等扇区 7 至 tick 15。
        self.assertEqual(record.complete, 16)
        self.assertFalse(record.on_time)


class TestScanDirection(unittest.TestCase):
    def test_picks_nearest_ahead_in_current_direction(self):
        scheduler = DiskScheduler(base_config(head_cylinder=4))
        requests = [
            Request(rid=1, cylinder=2, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=6, sector=0, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        # 初始方向为柱面号增大：先 6 后 2。
        self.assertEqual(result.served_order, (2, 1))

    def test_reverses_when_nothing_ahead(self):
        scheduler = DiskScheduler(base_config(head_cylinder=4))
        requests = [
            Request(rid=1, cylinder=3, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=1, sector=0, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        # 增大方向无请求，反向先服务较近的柱面 3，再到柱面 1。
        self.assertEqual(result.served_order, (1, 2))
        decision = next(e for e in result.timeline if e.kind == "decision")
        self.assertTrue(decision.extra["direction_reversed"])

    def test_nearest_cylinder_within_direction_group(self):
        scheduler = DiskScheduler(base_config(head_cylinder=4))
        requests = [
            Request(rid=1, cylinder=8, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=6, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=3, cylinder=2, sector=0, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        # 方向组优先：先 6（近）再 8，反向最后服务 2。
        self.assertEqual(result.served_order, (2, 1, 3))


class TestSameCylinderSectorOrder(unittest.TestCase):
    def test_earlier_rotating_sector_served_first(self):
        scheduler = DiskScheduler(base_config(head_cylinder=5))
        requests = [
            Request(rid=1, cylinder=5, sector=6, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=5, sector=2, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        # 同柱面无寻道：tick 0 时扇区 0 在头下，扇区 2 先于扇区 6 转到。
        self.assertEqual(result.served_order, (2, 1))
        first = result.record_of(2)
        second = result.record_of(1)
        self.assertEqual((first.read_start, first.complete), (2, 3))
        # 第一个请求读完于 tick 3，扇区 6 当圈仍可读到。
        self.assertEqual((second.read_start, second.complete), (6, 7))

    def test_sector_wrap_between_two_same_cylinder_requests(self):
        scheduler = DiskScheduler(base_config(head_cylinder=5))
        requests = [
            Request(rid=1, cylinder=5, sector=1, length=4, arrival=0, deadline=99),
            Request(rid=2, cylinder=5, sector=6, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        # R1 扇区 1 先转到，tick 1-5 读取；读取不可中断，R2 的扇区 6
        # 在 tick 6 才到起始边界（同圈仍赶得上，无需绕圈）。
        self.assertEqual(result.served_order, (1, 2))
        first = result.record_of(1)
        self.assertEqual((first.read_start, first.complete), (1, 5))
        second = result.record_of(2)
        self.assertEqual(second.read_start, 6)
        self.assertEqual(second.complete, 7)

    def test_second_request_waits_full_next_rotation(self):
        scheduler = DiskScheduler(base_config(head_cylinder=5))
        requests = [
            Request(rid=1, cylinder=5, sector=1, length=5, arrival=0, deadline=99),
            Request(rid=2, cylinder=5, sector=2, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        # R1 tick 1-6 读取，R2 的扇区 2 当圈已过，下一圈起始边界 tick 10。
        self.assertEqual(result.served_order, (1, 2))
        first = result.record_of(1)
        self.assertEqual((first.read_start, first.complete), (1, 6))
        second = result.record_of(2)
        self.assertEqual(second.seek_start, 6)
        self.assertEqual(second.read_start, 10)
        self.assertEqual(second.complete, 11)
        self.assertEqual(second.rotational_latency, 4)


class TestNormalModeMarked(unittest.TestCase):
    def test_decision_mode_normal_when_deadlines_loose(self):
        scheduler = DiskScheduler(base_config(head_cylinder=4))
        requests = [
            Request(rid=1, cylinder=2, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=6, sector=0, length=1, arrival=0, deadline=99),
        ]
        result = scheduler.run(requests)
        decisions = [e for e in result.timeline if e.kind == "decision"]
        self.assertTrue(all(e.mode == MODE_NORMAL for e in decisions))

    def test_plan_service_zero_seek_same_cylinder(self):
        config = base_config()
        plan = plan_service(
            config, 0, 5, Request(1, 5, 3, 1, 0, 99)
        )
        self.assertEqual(plan.seek_distance, 0)
        self.assertEqual(plan.seek_end, 0)
        self.assertEqual(plan.read_start, 3)
        self.assertEqual(plan.complete, 4)


if __name__ == "__main__":
    unittest.main()
