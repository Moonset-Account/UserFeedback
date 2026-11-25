"""8 项队列上限下的端到端集成测试（交错到达 + 降级 + sector_miss）。"""

import unittest

from disksim import DiskConfig, DiskScheduler, Request
from disksim.model import MODE_BEST_EFFORT, MODE_NORMAL


def full_config():
    return DiskConfig(
        cylinders=12,
        sectors_per_track=8,
        ticks_per_sector=1,
        seek_ticks_per_cylinder=1,
        max_queue=8,
        head_cylinder=6,
    )


class TestFullQueueIntegration(unittest.TestCase):
    def setUp(self):
        self.scheduler = DiskScheduler(full_config())
        self.requests = [
            Request(1, 9, 2, 2, 0, 40),
            Request(2, 3, 5, 1, 1, 30),
            Request(3, 6, 0, 1, 2, 12),
            Request(4, 10, 4, 3, 3, 60),
            Request(5, 2, 1, 1, 4, 35),
            Request(6, 6, 4, 2, 5, 20),
            Request(7, 11, 3, 1, 6, 80),
            Request(8, 0, 6, 1, 7, 90),
        ]

    def test_everybody_served_none_rejected(self):
        result = self.scheduler.run(self.requests)
        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.records), 8)
        self.assertEqual(tuple(sorted(result.served_order)), tuple(range(1, 9)))

    def test_modes_and_order(self):
        result = self.scheduler.run(self.requests)
        decisions = [e for e in result.timeline if e.kind == "decision"]
        modes = [e.mode for e in decisions]
        # 首批只有 R1 可决策（normal）；R3/R6 的紧截止期无法全保，
        # 中段连续 best-effort；后期只剩宽松截止期请求，回到 normal。
        self.assertEqual(modes[0], MODE_NORMAL)
        self.assertIn(MODE_BEST_EFFORT, modes)
        self.assertEqual(modes[-1], MODE_NORMAL)
        self.assertEqual(result.served_order, (1, 2, 5, 8, 3, 6, 4, 7))

    def test_first_request_just_misses_target_sector_during_seek(self):
        result = self.scheduler.run(self.requests)
        first = result.record_of(1)
        # 柱面 6 -> 9：寻道 tick 0-3；tick 3 时磁头下是扇区 3，
        # 目标扇区 2 刚转走，刚好错过，等 7 tick 到下一圈 tick 10。
        self.assertEqual(first.seek_end, 3)
        self.assertTrue(first.just_missed)
        self.assertEqual(first.rotational_latency, 7)
        self.assertEqual(first.read_start, 10)
        self.assertEqual(first.complete, 12)

        miss = next(e for e in result.timeline if e.kind == "sector_miss")
        self.assertEqual((miss.time, miss.rid), (3, 1))
        self.assertEqual(miss.extra["head_sector"], 3)
        self.assertEqual(miss.extra["target_sector"], 2)

    def test_arrivals_interleave_with_nonpreemptible_service(self):
        result = self.scheduler.run(self.requests)
        # R2 到达于 tick 1，此时 R1 正在寻道/读取，R2 不能抢占，
        # 其服务开始时刻不早于 R1 完成时刻 tick 12。
        r2 = result.record_of(2)
        self.assertEqual(r2.seek_start, 12)
        arrival_times = [
            e.time for e in result.timeline if e.kind == "arrival"
        ]
        self.assertEqual(arrival_times, [0, 1, 2, 3, 4, 5, 6, 7])

    def test_on_time_and_seek_metrics(self):
        result = self.scheduler.run(self.requests)
        # R3（deadline 12）与 R6（deadline 20）在 best-effort 下违约，
        # 其余 6 个按时。
        self.assertEqual(result.on_time_count, 6)
        self.assertFalse(result.record_of(3).on_time)
        self.assertFalse(result.record_of(6).on_time)
        self.assertEqual(result.total_seek_distance, 23)


class TestQueueExactlyAtCap(unittest.TestCase):
    def test_eight_accepted_ninth_waits_for_free_slot(self):
        config = DiskConfig(
            cylinders=4,
            sectors_per_track=4,
            ticks_per_sector=1,
            seek_ticks_per_cylinder=1,
            max_queue=8,
            head_cylinder=0,
        )
        scheduler = DiskScheduler(config)
        requests = [
            Request(rid=i, cylinder=0, sector=0, length=1, arrival=0, deadline=999)
            for i in range(1, 9)
        ]
        # 第 9 个在 tick 1 到达：首个请求 tick 1 才读完，此刻队列仍有 7 项，
        # 但吸收发生在 tick 1 的决策点（R1 已出队），故可被接受。
        requests.append(
            Request(rid=9, cylinder=0, sector=0, length=1, arrival=1, deadline=999)
        )
        result = scheduler.run(requests)
        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.records), 9)
        self.assertEqual(result.served_order[0], 1)
        self.assertEqual(result.served_order[-1], 9)


if __name__ == "__main__":
    unittest.main()
