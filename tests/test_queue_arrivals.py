"""队列上限、请求交错到达、不可中断性与空闲推进的测试。"""

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


class TestQueueCap(unittest.TestCase):
    def test_ninth_request_rejected_and_later_one_accepted(self):
        config = base_config(max_queue=8, head_cylinder=9)
        scheduler = DiskScheduler(config)
        # 9 个请求同时到达，队列上限 8：rid 9 被拒。
        requests = [
            Request(rid=i, cylinder=9, sector=0, length=1, arrival=0, deadline=999)
            for i in range(1, 10)
        ]
        result = scheduler.run(requests)
        self.assertEqual(result.rejected, (9,))
        self.assertEqual(len(result.records), 8)
        self.assertEqual(sorted(result.served_order), list(range(1, 9)))

        reject_event = next(
            e for e in result.timeline if e.kind == "arrival" and not e.accepted
        )
        self.assertEqual(reject_event.rid, 9)
        accepted_events = [
            e for e in result.timeline if e.kind == "arrival" and e.accepted
        ]
        self.assertEqual(len(accepted_events), 8)

    def test_slot_freed_after_completion_accepts_later_arrival(self):
        config = base_config(max_queue=1, head_cylinder=0)
        scheduler = DiskScheduler(config)
        requests = [
            # 同时到达两个请求：容量 1，rid 2 当场被拒。
            Request(rid=1, cylinder=0, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=0, sector=0, length=1, arrival=0, deadline=99),
            # tick 5 时 R1 已完成、队列空，R3 被接受。
            Request(rid=3, cylinder=0, sector=0, length=1, arrival=5, deadline=99),
        ]
        result = scheduler.run(requests)
        self.assertEqual(result.rejected, (2,))
        self.assertEqual(result.served_order, (1, 3))
        self.assertTrue(
            all(e.accepted for e in result.timeline
                if e.kind == "arrival" and e.rid in (1, 3))
        )


class TestInterleavedArrivals(unittest.TestCase):
    def test_arrival_during_nonpreemptible_service_waits(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        requests = [
            Request(rid=1, cylinder=4, sector=4, length=2, arrival=0, deadline=99),
            Request(rid=2, cylinder=0, sector=0, length=1, arrival=2, deadline=99),
        ]
        result = scheduler.run(requests)
        r1 = result.record_of(1)
        # R1 不可中断：tick 0 寻道至 tick 4，读 tick 4-6。
        self.assertEqual((r1.seek_end, r1.read_start, r1.complete), (4, 4, 6))
        r2 = result.record_of(2)
        # R2 在 tick 2 已到达，但只能在 tick 6 的决策点参与；
        # 磁头在柱面 4，回柱面 0 寻道 4 tick 至 tick 10，扇区 0 在 tick 16。
        self.assertEqual(r2.seek_start, 6)
        self.assertEqual((r2.seek_end, r2.read_start, r2.complete), (10, 16, 17))
        self.assertEqual(result.served_order, (1, 2))

        # R2 的 arrival 事件夹在 R1 的 seek 与 read 之间。
        kinds_with_time = [(e.time, e.kind, e.rid) for e in result.timeline]
        arrival2 = next(item for item in kinds_with_time if item[2] == 2)
        self.assertEqual(arrival2[0:2], (2, "arrival"))
        r1_read = next(
            item for item in kinds_with_time if item[1] == "read" and item[2] == 1
        )
        self.assertLess(arrival2[0], r1_read[0])

    def test_future_arrival_not_counted_in_feasibility(self):
        # R1 截止期宽松；R2 很晚才到达，决策点 t=0 的可行性只看已到请求。
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        requests = [
            Request(rid=1, cylinder=2, sector=2, length=1, arrival=0, deadline=100),
            Request(rid=2, cylinder=3, sector=3, length=1, arrival=20, deadline=100),
        ]
        result = scheduler.run(requests)
        first_decision = next(
            e for e in result.timeline if e.kind == "decision"
        )
        self.assertEqual(first_decision.rid, 1)
        self.assertEqual(first_decision.extra["candidates"], [1])
        self.assertEqual(result.served_order, (1, 2))


class TestIdleAdvance(unittest.TestCase):
    def test_idle_until_first_arrival(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        request = Request(
            rid=1, cylinder=1, sector=1, length=1, arrival=10, deadline=99
        )
        result = scheduler.run([request])
        idle = next(e for e in result.timeline if e.kind == "idle")
        self.assertEqual(idle.time, 0)
        self.assertEqual(idle.extra["resume_time"], 10)
        record = result.record_of(1)
        # 从 tick 10 开始寻道 1 tick，tick 11 时扇区 3 在头下，
        # 目标扇区 1 下一圈 tick 17。
        self.assertEqual(record.seek_start, 10)
        self.assertEqual(record.seek_end, 11)
        self.assertEqual(record.read_start, 17)

    def test_idle_between_two_batches(self):
        scheduler = DiskScheduler(base_config(head_cylinder=0))
        requests = [
            Request(rid=1, cylinder=0, sector=0, length=1, arrival=0, deadline=99),
            Request(rid=2, cylinder=0, sector=0, length=1, arrival=20, deadline=99),
        ]
        result = scheduler.run(requests)
        idle_events = [e for e in result.timeline if e.kind == "idle"]
        self.assertEqual(len(idle_events), 1)
        # R1 tick 0-1 服务完，空队列，空闲至 tick 20。
        self.assertEqual(idle_events[0].time, 1)
        self.assertEqual(idle_events[0].extra["resume_time"], 20)
        r2 = result.record_of(2)
        self.assertEqual(r2.seek_start, 20)


class TestSimultaneousArrivals(unittest.TestCase):
    def test_same_time_arrivals_all_absorbed_before_decision(self):
        scheduler = DiskScheduler(base_config(head_cylinder=4))
        requests = [
            Request(rid=3, cylinder=6, sector=0, length=1, arrival=5, deadline=99),
            Request(rid=1, cylinder=2, sector=0, length=1, arrival=5, deadline=99),
            Request(rid=2, cylinder=5, sector=0, length=1, arrival=5, deadline=99),
        ]
        result = scheduler.run(requests)
        # 先空闲到 tick 5，三个到达全部吸收后才产生 decision。
        kinds = [e.kind for e in result.timeline]
        self.assertEqual(kinds.count("arrival"), 3)
        self.assertEqual(kinds.index("decision"), 4)  # idle + 3 arrivals
        first_decision = next(
            e for e in result.timeline if e.kind == "decision"
        )
        self.assertEqual(sorted(first_decision.extra["candidates"]), [1, 2, 3])
        # SCAN 向上：柱面 5（rid2）最近。
        self.assertEqual(first_decision.rid, 2)


class TestValidation(unittest.TestCase):
    def test_duplicate_rid_rejected(self):
        scheduler = DiskScheduler(base_config())
        requests = [
            Request(rid=1, cylinder=0, sector=0, length=1, arrival=0, deadline=9),
            Request(rid=1, cylinder=1, sector=0, length=1, arrival=0, deadline=9),
        ]
        with self.assertRaises(ValueError):
            scheduler.run(requests)

    def test_bad_geometry_rejected(self):
        scheduler = DiskScheduler(base_config(cylinders=4, sectors_per_track=4))
        with self.assertRaises(ValueError):
            scheduler.run(
                [Request(rid=1, cylinder=4, sector=0, length=1, arrival=0, deadline=9)]
            )
        with self.assertRaises(ValueError):
            scheduler.run(
                [Request(rid=2, cylinder=0, sector=4, length=1, arrival=0, deadline=9)]
            )
        with self.assertRaises(ValueError):
            scheduler.run(
                [Request(rid=3, cylinder=0, sector=2, length=3, arrival=0, deadline=9)]
            )

    def test_bad_config_rejected(self):
        with self.assertRaises(ValueError):
            DiskConfig(cylinders=0, sectors_per_track=8)
        with self.assertRaises(ValueError):
            DiskConfig(cylinders=8, sectors_per_track=0)
        with self.assertRaises(ValueError):
            DiskConfig(cylinders=8, sectors_per_track=8, max_queue=0)

    def test_bad_request_fields(self):
        with self.assertRaises(ValueError):
            Request(rid=1, cylinder=0, sector=0, length=0, arrival=0, deadline=9)
        with self.assertRaises(ValueError):
            Request(rid=1, cylinder=0, sector=0, length=1, arrival=-1, deadline=9)
        with self.assertRaises(ValueError):
            Request(rid=1, cylinder=0, sector=0, length=1, arrival=5, deadline=4)


if __name__ == "__main__":
    unittest.main()
