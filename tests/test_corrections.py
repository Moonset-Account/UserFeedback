import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import (
    RETAIN_PRE_CORRECTION,
    REVIEW_OBSERVATION_CORRECTED,
    VERDICT_ESTABLISHED,
)

from scenarios import (
    T0,
    T2,
    T4,
    T6,
    scenario_correction_boundary,
    scenario_correction_outside_period,
    scenario_double_correction,
)


class TestCorrectionBoundary(unittest.TestCase):
    def test_period_split_at_correction_time(self):
        report = verify_migration(*scenario_correction_boundary())

        # 前段 [T0, T2) 保留旧归因；后段 [T2, T4) 用更正后来源重算并迁移
        retained = report.retained
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].reason, RETAIN_PRE_CORRECTION)
        self.assertEqual(retained[0].valid_from.isoformat(), _utc(T0))
        self.assertEqual(retained[0].valid_to.isoformat(), _utc(T2))

        migrated = report.migrated
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0].conclusion_id, "new-c")
        self.assertEqual(migrated[0].verdict, VERDICT_ESTABLISHED)
        self.assertEqual(migrated[0].valid_from.isoformat(), _utc(T2))
        self.assertEqual(migrated[0].valid_to.isoformat(), _utc(T4))
        # 迁移依据必须是更正后的具体来源
        self.assertEqual(migrated[0].evidence_obs_ids, ("obs-c2",))

        review = report.re_review
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0].reason, REVIEW_OBSERVATION_CORRECTED)
        self.assertEqual(review[0].obs_id, "obs-c1")
        self.assertIn("obs-c1 -> obs-c2", review[0].detail)

    def test_double_correction_chain_three_segments(self):
        report = verify_migration(*scenario_double_correction())

        # 三段：[T0,T2) 保留、[T2,T4) 保留、[T4,T6) 迁移（official 达标）
        self.assertEqual(len(report.retained), 2)
        self.assertEqual(
            [item.reason for item in report.retained],
            [RETAIN_PRE_CORRECTION, RETAIN_PRE_CORRECTION],
        )
        self.assertEqual(report.retained[0].valid_to.isoformat(), _utc(T2))
        self.assertEqual(report.retained[1].valid_from.isoformat(), _utc(T2))
        self.assertEqual(report.retained[1].valid_to.isoformat(), _utc(T4))

        self.assertEqual(len(report.migrated), 1)
        migrated = report.migrated[0]
        self.assertEqual(migrated.valid_from.isoformat(), _utc(T4))
        self.assertEqual(migrated.valid_to.isoformat(), _utc(T6))
        self.assertEqual(migrated.evidence_obs_ids, ("obs-d3",))

        # 更正链在一个复核项里完整呈现
        corrected_items = [
            i for i in report.re_review
            if i.reason == REVIEW_OBSERVATION_CORRECTED
        ]
        self.assertEqual(len(corrected_items), 1)
        self.assertIn("obs-d1 -> obs-d2 -> obs-d3", corrected_items[0].detail)

    def test_correction_before_valid_from_recomputes_whole_period(self):
        report = verify_migration(*scenario_correction_outside_period())

        # 更正点 T1 早于结论生效 T2，整段直接按更正后观测求值
        self.assertEqual(len(report.retained), 0)
        self.assertEqual(len(report.migrated), 1)
        migrated = report.migrated[0]
        self.assertEqual(
            migrated.valid_from.isoformat(),
            _utc("2026-03-03T00:00:00Z"),
        )
        self.assertEqual(migrated.evidence_obs_ids, ("obs-e2",))

        # 即使更正发生在结论时段外，支撑观测被更正仍需登记复核
        self.assertEqual(len(report.re_review), 1)
        self.assertEqual(
            report.re_review[0].reason, REVIEW_OBSERVATION_CORRECTED
        )


def _utc(value: str) -> str:
    """把测试用的 Z 时间转成内部 datetime 的 isoformat 形式用于比较。"""
    from datetime import datetime, timezone

    text = value[:-1] if value.endswith("Z") else value
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
