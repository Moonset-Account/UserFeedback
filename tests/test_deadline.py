"""截止期保护、安全首选寻道最小化与降级择优的测试。

每个场景的服务时刻均按题面计时规则手工推演，断言完整时间线数值，
而非只断言调度顺序。
"""

import unittest

from disksim import DiskConfig, DiskScheduler, Request
from disksim.model import MODE_BEST_EFFORT, MODE_NORMAL, MODE_SAFE


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


class TestDeadlineOverride(unittest.TestCase):
    """普通首选会违约时，改选仍能全保的安全首选。"""

    def setUp(self):
        self.scheduler = DiskScheduler(base_config(head_cylinder=0))
        # R1 柱面 3（普通 SCAN 首选，近），R2 柱面 6，截止期很紧。
        self.requests = [
            Request(rid=1, cylinder=3, sector=0, length=1, arrival=0, deadline=20),
            Request(rid=2, cylinder=6, sector=6, length=1, arrival=0, deadline=7),
        ]

    def test_manual_feasibility_analysis(self):
        # R2 先：寻道 6 -> tick 6，扇区 6 对齐，tick 7 完成（<=7）；
        #        再服务 R1：6->3 寻道至 tick 10，扇区 0 在 tick 16，
        #        tick 17 完成（<=20）。全保。
        # R1 先：寻道至 tick 3，扇区 0 在 tick 8 才读到，tick 9 完成；
        #        R2 寻道至 tick 12，扇区 6 在 tick 14，tick 15 完成（>7）。
        # 故安全首选只有 R2，普通首选 R1 必须被覆盖。
        result = self.scheduler.run(self.requests)
        first_decision = next(
            e for e in result.timeline if e.kind == "decision"
        )
        self.assertEqual(first_decision.rid, 2)
        self.assertEqual(first_decision.mode, MODE_SAFE)
        self.assertEqual(first_decision.extra["normal_first"], 1)
        self.assertEqual(first_decision.extra["safe_first_moves"], [2])
        self.assertIn("override_reason", first_decision.extra)

    def test_timeline_values(self):
        result = self.scheduler.run(self.requests)
        self.assertEqual(result.served_order, (2, 1))
        r2 = result.record_of(2)
        self.assertEqual((r2.seek_end, r2.read_start, r2.complete), (6, 6, 7))
        self.assertTrue(r2.on_time)
        r1 = result.record_of(1)
        self.assertEqual((r1.seek_end, r1.read_start, r1.complete), (10, 16, 17))
        self.assertTrue(r1.on_time)
        self.assertEqual(result.on_time_count, 2)


class TestSafeMinSeek(unittest.TestCase):
    """多个安全首选时，挑磁头移动最少者。"""

    def setUp(self):
        # 磁头在柱面 0，方向向上，手工计时推演（扇区 s 的起始边界为 tick s）：
        # A(rid1, 柱面1, 扇0) 普通首选但 A 开头时 B 最早 tick 18 完成 > 16；
        # B(rid2, 柱面3, 扇0) 开头：B@9、C@14、A@25，全部按时；
        # C(rid3, 柱面5, 扇5) 开头：C@11、B@16、A@25，全部按时。
        # 安全集 {2, 3}，寻道距离 3 < 5，选 B。
        self.scheduler = DiskScheduler(base_config(head_cylinder=0))
        self.requests = [
            Request(rid=1, cylinder=1, sector=0, length=1, arrival=0, deadline=26),
            Request(rid=2, cylinder=3, sector=0, length=1, arrival=0, deadline=16),
            Request(rid=3, cylinder=5, sector=5, length=1, arrival=0, deadline=14),
        ]

    def test_safe_set_and_choice(self):
        result = self.scheduler.run(self.requests)
        decision = next(e for e in result.timeline if e.kind == "decision")
        self.assertEqual(decision.mode, MODE_SAFE)
        self.assertEqual(decision.extra["normal_first"], 1)
        self.assertEqual(decision.extra["safe_first_moves"], [2, 3])
        self.assertEqual(decision.rid, 2)

    def test_all_three_eventually_on_time(self):
        result = self.scheduler.run(self.requests)
        # B -> C -> A：B tick 9 完成，C tick 14，A tick 25。
        self.assertEqual(result.served_order, (2, 3, 1))
        self.assertEqual(result.record_of(2).complete, 9)
        self.assertEqual(result.record_of(3).complete, 14)
        self.assertEqual(result.record_of(1).complete, 25)
        self.assertEqual(result.on_time_count, 3)


class TestBestEffortCountPriority(unittest.TestCase):
    """无全保方案：先最大化按时数，哪怕首选不是最近柱面。"""

    def setUp(self):
        self.scheduler = DiskScheduler(base_config(head_cylinder=0))
        # R1 柱面 1 即使先服务也赶不上截止期 1；
        # R2 柱面 3 先服务可在 tick 4 按时。
        self.requests = [
            Request(rid=1, cylinder=1, sector=0, length=1, arrival=0, deadline=1),
            Request(rid=2, cylinder=3, sector=3, length=1, arrival=0, deadline=4),
        ]

    def test_chooses_feasible_request_first(self):
        result = self.scheduler.run(self.requests)
        decision = next(e for e in result.timeline if e.kind == "decision")
        self.assertEqual(decision.mode, MODE_BEST_EFFORT)
        self.assertEqual(decision.rid, 2)
        self.assertEqual(decision.extra["best_effort_key"]["on_time"], 1)
        self.assertEqual(result.served_order, (2, 1))
        self.assertTrue(result.record_of(2).on_time)
        self.assertFalse(result.record_of(1).on_time)
        self.assertEqual(result.on_time_count, 1)


class TestBestEffortSeekTieBreak(unittest.TestCase):
    """按时数相同：总寻道距离更小者胜。"""

    def setUp(self):
        self.scheduler = DiskScheduler(base_config(head_cylinder=0))
        # 两者各自先服务都能按时、第二个必违约；
        # 先 R1（柱面2）总寻道 2+4=6，先 R2（柱面6）总寻道 6+4=10。
        self.requests = [
            Request(rid=1, cylinder=2, sector=2, length=1, arrival=0, deadline=3),
            Request(rid=2, cylinder=6, sector=6, length=1, arrival=0, deadline=7),
        ]

    def test_lower_total_seek_wins(self):
        result = self.scheduler.run(self.requests)
        decision = next(e for e in result.timeline if e.kind == "decision")
        self.assertEqual(decision.mode, MODE_BEST_EFFORT)
        self.assertEqual(decision.rid, 1)
        key = decision.extra["best_effort_key"]
        self.assertEqual(key["on_time"], 1)
        self.assertEqual(key["total_seek"], 6)
        self.assertEqual(result.total_seek_distance, 6)
        self.assertEqual(result.on_time_count, 1)


class TestBestEffortIdSequenceTieBreak(unittest.TestCase):
    """按时数与总寻道都相同：编号序列字典序更靠前。"""

    def setUp(self):
        self.scheduler = DiskScheduler(base_config(head_cylinder=0))
        common = dict(cylinder=4, sector=4, length=1, arrival=0, deadline=5)
        self.requests = [
            Request(rid=1, **common),
            Request(rid=2, **common),
        ]

    def test_lexicographic_sequence(self):
        result = self.scheduler.run(self.requests)
        decision = next(e for e in result.timeline if e.kind == "decision")
        self.assertEqual(decision.mode, MODE_BEST_EFFORT)
        self.assertEqual(
            decision.extra["best_effort_key"]["sequence"], [1, 2]
        )
        self.assertEqual(result.served_order, (1, 2))
        self.assertTrue(result.record_of(1).on_time)
        self.assertFalse(result.record_of(2).on_time)


class TestDeadlineBoundary(unittest.TestCase):
    def test_complete_equal_deadline_is_on_time(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        # 柱面 4、扇区 4：tick 4 寻道结束且对齐，tick 5 完成。
        request = Request(
            rid=1, cylinder=4, sector=4, length=1, arrival=0, deadline=5
        )
        result = scheduler.run([request])
        self.assertEqual(result.record_of(1).complete, 5)
        self.assertTrue(result.record_of(1).on_time)
        self.assertEqual(result.on_time_count, 1)

    def test_complete_one_tick_late_is_miss(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        request = Request(
            rid=1, cylinder=4, sector=4, length=1, arrival=0, deadline=4
        )
        result = scheduler.run([request])
        self.assertFalse(result.record_of(1).on_time)


class TestBestEffortStillServesEverybody(unittest.TestCase):
    def test_missed_request_still_served_nonpreemptibly(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        requests = [
            Request(rid=1, cylinder=2, sector=2, length=1, arrival=0, deadline=3),
            Request(rid=2, cylinder=6, sector=6, length=1, arrival=0, deadline=7),
        ]
        result = scheduler.run(requests)
        # 即使降级，队列中的请求仍全部被服务且读取不可中断。
        self.assertEqual(len(result.records), 2)
        self.assertEqual(
            sorted(r.rid for r in result.records), [1, 2]
        )
        r2 = result.record_of(2)
        # R1 于 tick 3 完成后 R2 才开始，期间无插入点。
        self.assertEqual(r2.seek_start, 3)
        self.assertGreaterEqual(r2.read_start, r2.seek_end)


if __name__ == "__main__":
    unittest.main()
