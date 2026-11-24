"""LocalTimeMap 语义测试：普通时刻、回拨歧义、前拨跳过。"""

import unittest
from datetime import timedelta

from job_controller import SyntheticZone, Transition, ZoneInfoLocalTimeMap

from synthetic import (
    FALLBACK_FIRST_UTC,
    FALLBACK_SECOND_UTC,
    SPRING_GAP_UTC,
    SYNTHETIC_ZONE,
    WINTER_INSTANT_UTC,
    local,
    utc,
)


class SyntheticZoneResolveTest(unittest.TestCase):
    def test_normal_time_resolves_to_single_instant(self):
        result = SYNTHETIC_ZONE.resolve(local(2026, 6, 2, 1, 30))
        self.assertEqual(result, (WINTER_INSTANT_UTC,))

    def test_fallback_ambiguous_time_resolves_to_two_instants_in_order(self):
        result = SYNTHETIC_ZONE.resolve(local(2026, 11, 1, 1, 30))
        self.assertEqual(result, (FALLBACK_FIRST_UTC, FALLBACK_SECOND_UTC))
        # 两遍相隔一小时，且按真实发生时间升序
        self.assertEqual(
            result[1] - result[0], timedelta(hours=1)
        )

    def test_spring_forward_gap_resolves_to_no_instant(self):
        result = SYNTHETIC_ZONE.resolve(local(2026, 3, 8, 1, 30))
        self.assertEqual(result, ())

    def test_to_local_round_trip(self):
        for instant in (
            FALLBACK_FIRST_UTC,
            FALLBACK_SECOND_UTC,
            SPRING_GAP_UTC,
            WINTER_INSTANT_UTC,
        ):
            wall = SYNTHETIC_ZONE.to_local(instant)
            self.assertIsNone(wall.tzinfo)
            self.assertIn(instant, SYNTHETIC_ZONE.resolve(wall))

    def test_offset_at_transition_boundaries(self):
        # 换表瞬间起即采用新偏移（春令时换表点 06:00Z）
        self.assertEqual(
            SYNTHETIC_ZONE.offset_at(utc(2026, 3, 8, 5, 59)),
            timedelta(hours=-5),
        )
        self.assertEqual(
            SYNTHETIC_ZONE.offset_at(utc(2026, 3, 8, 6)),
            timedelta(hours=-4),
        )
        self.assertEqual(
            SYNTHETIC_ZONE.offset_at(utc(2026, 11, 1, 5, 59)),
            timedelta(hours=-4),
        )
        self.assertEqual(
            SYNTHETIC_ZONE.offset_at(utc(2026, 11, 1, 6)),
            timedelta(hours=-5),
        )

    def test_rejects_naive_input_for_offset(self):
        with self.assertRaises(ValueError):
            SYNTHETIC_ZONE.offset_at(local(2026, 6, 1, 0, 0))

    def test_rejects_aware_input_for_resolve(self):
        with self.assertRaises(ValueError):
            SYNTHETIC_ZONE.resolve(utc(2026, 6, 1, 1, 30))


class ZoneInfoAdapterTest(unittest.TestCase):
    """用真实 IANA 时区 America/New_York 校验适配器（2026 年规则）。"""

    def setUp(self):
        try:
            self.zone_map = ZoneInfoLocalTimeMap("America/New_York")
        except Exception as exc:  # pragma: no cover - 无 tzdata 时跳过
            self.skipTest(f"zoneinfo 数据不可用: {exc}")

    def test_new_york_fallback_ambiguous(self):
        # 2026-11-01 01:30 在纽约出现两次：05:30Z(-4) 与 06:30Z(-5)
        result = self.zone_map.resolve(local(2026, 11, 1, 1, 30))
        self.assertEqual(
            result,
            (utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 6, 30)),
        )

    def test_new_york_spring_forward_gap(self):
        # 2026-03-08 02:00->03:00，02:30 不存在
        self.assertEqual(
            self.zone_map.resolve(local(2026, 3, 8, 2, 30)), ()
        )

    def test_new_york_normal_time(self):
        self.assertEqual(
            self.zone_map.resolve(local(2026, 6, 1, 1, 30)),
            (utc(2026, 6, 1, 5, 30),),
        )

    def test_new_york_to_local(self):
        wall = self.zone_map.to_local(utc(2026, 11, 1, 5, 30))
        self.assertEqual(wall, local(2026, 11, 1, 1, 30))


class ZoneConstructionValidationTest(unittest.TestCase):
    def test_transition_requires_utc(self):
        with self.assertRaises(ValueError):
            SyntheticZone(
                name="bad",
                initial_offset=timedelta(hours=0),
                transitions=[
                    Transition(
                        at_utc=local(2026, 1, 1, 0, 0),
                        new_offset=timedelta(hours=1),
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
