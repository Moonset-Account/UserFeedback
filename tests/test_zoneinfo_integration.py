"""基于真实 IANA 时区的端到端控制器测试（America/New_York）。

合成时区测试保证确定性与边界可控；本组测试用标准库 zoneinfo 验证
控制器与真实时区规则协作时行为一致。无 tzdata 数据时整组跳过。
"""

import unittest
from datetime import time, timedelta

from job_controller import (
    CreateDecision,
    InMemoryRunStateStore,
    JobController,
    RecreateDecision,
    Schedule,
    ZoneInfoLocalTimeMap,
)

from synthetic import local, utc


@unittest.skipUnless(
    ZoneInfoLocalTimeMap is not None, "zoneinfo 不可用"
)
class ZoneInfoEndToEndTest(unittest.TestCase):
    def setUp(self):
        try:
            self.time_map = ZoneInfoLocalTimeMap("America/New_York")
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"zoneinfo 数据不可用: {exc}")
        self.store = InMemoryRunStateStore()
        self.controller = JobController(self.store, self.time_map)
        self.schedule = Schedule(
            schedule_id="ny-job",
            local_time=time(1, 30),
            allow_concurrent=False,
            lookback=timedelta(hours=18),
        )

    def test_fallback_day_creates_two_distinct_instances(self):
        result = self.controller.plan(
            self.schedule,
            now_utc=utc(2026, 11, 1, 12, 0),
            last_confirmed_utc=utc(2026, 10, 30, 0, 0),
        )
        scheduled = [d.scheduled_utc for d in result.decisions]
        self.assertEqual(
            scheduled,
            [utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 6, 30)],
        )
        self.assertTrue(
            all(isinstance(d, CreateDecision) for d in result.decisions)
        )

    def test_spring_gap_day_emits_notice_without_candidate(self):
        result = self.controller.plan(
            self.schedule,
            now_utc=utc(2026, 3, 9, 12, 0),
            last_confirmed_utc=utc(2026, 3, 8, 12, 0),
        )
        # 窗口只覆盖 3-9；3-8 的缺口用更宽的确认点单独验证
        self.assertEqual(
            [d.scheduled_utc for d in result.decisions],
            [utc(2026, 3, 9, 5, 30)],
        )

        wide = Schedule(
            schedule_id="ny-job",
            local_time=time(2, 30),  # 纽约 02:30 在 2026-03-08 不存在
            lookback=timedelta(hours=18),
        )
        gap_result = self.controller.plan(
            wide,
            now_utc=utc(2026, 3, 8, 12, 0),
            last_confirmed_utc=utc(2026, 3, 7, 0, 0),
        )
        self.assertEqual(gap_result.decisions, ())
        self.assertEqual(len(gap_result.gap_notices), 1)
        self.assertEqual(
            gap_result.gap_notices[0].local_wall_time,
            local(2026, 3, 8, 2, 30),
        )

    def test_restart_keeps_creation_id_with_real_zone(self):
        first = self.controller.plan(
            self.schedule,
            now_utc=utc(2026, 6, 2, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        original_id = first.decisions[0].creation_id

        restarted = JobController(self.store, self.time_map).plan(
            self.schedule,
            now_utc=utc(2026, 6, 2, 6, 1),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        self.assertIsInstance(restarted.decisions[0], RecreateDecision)
        self.assertEqual(restarted.decisions[0].creation_id, original_id)


if __name__ == "__main__":
    unittest.main()
