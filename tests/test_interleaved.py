import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import (
    RETAIN_PRE_CORRECTION,
    REVIEW_OBSERVATION_CORRECTED,
    VERDICT_ESTABLISHED,
    VERDICT_UNCERTAIN,
)

from scenarios import T3, T5, scenario_interleaved


class TestInterleaved(unittest.TestCase):
    def test_all_rule_shapes_and_correction_in_one_input(self):
        report = verify_migration(*scenario_interleaved())

        # 迁移项：替换类 1 + 拆分 2 + 合并不确定 1
        self.assertEqual(len(report.migrated), 4)
        verdicts = {}
        for item in report.migrated:
            verdicts.setdefault(item.verdict, []).append(item)

        self.assertEqual(len(verdicts[VERDICT_ESTABLISHED]), 3)
        self.assertEqual(len(verdicts[VERDICT_UNCERTAIN]), 1)

        # 替换类：更正点 T3 切分，后段用更正后来源 obs-a2 重算
        migrated_a = next(m for m in report.migrated if m.conclusion_id == "new-a")
        self.assertEqual(migrated_a.evidence_obs_ids, ("obs-a2",))
        self.assertEqual(migrated_a.valid_from.isoformat().endswith("+00:00"), True)
        self.assertEqual(
            migrated_a.valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"), T3
        )
        self.assertEqual(migrated_a.valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"), T5)

        retained_a = next(r for r in report.retained if r.legacy_rule_id == "OLD-A")
        self.assertEqual(retained_a.reason, RETAIN_PRE_CORRECTION)
        self.assertEqual(
            retained_a.valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"), T3
        )

        # 拆分类：两个子结论都迁移，来源互不相同
        split_items = [m for m in report.migrated if m.new_rule_id == "NEW-S"]
        self.assertEqual(len(split_items), 2)
        split_sources = {m.evidence_obs_ids[0] for m in split_items}
        self.assertEqual(split_sources, {"obs-s1", "obs-s2"})

        # 合并类：互斥分支同时成立 -> 单条 uncertain
        uncertain = verdicts[VERDICT_UNCERTAIN][0]
        self.assertEqual(uncertain.new_rule_id, "NEW-M")
        self.assertEqual(set(uncertain.source_rule_ids), {"OLD-M1", "OLD-M2"})
        self.assertEqual(
            set(uncertain.evidence_obs_ids), {"obs-m1", "obs-m2"}
        )

        # 复核项只有更正那一条，且只出现一次（跨结论去重）
        self.assertEqual(len(report.re_review), 1)
        self.assertEqual(
            report.re_review[0].reason, REVIEW_OBSERVATION_CORRECTED
        )
        self.assertEqual(report.re_review[0].obs_id, "obs-a1")

    def test_segment_periods_cover_full_legacy_period_without_gap(self):
        report = verify_migration(*scenario_interleaved())
        old_a_pieces = [
            *[r for r in report.retained if r.legacy_rule_id == "OLD-A"],
            *[m for m in report.migrated if m.new_rule_id == "NEW-A"],
        ]
        old_a_pieces.sort(key=lambda p: p.valid_from)
        # 前段的终点就是后段的起点，时段首尾相接
        self.assertEqual(old_a_pieces[0].valid_to, old_a_pieces[1].valid_from)
        self.assertEqual(
            old_a_pieces[-1].valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"), T5
        )


if __name__ == "__main__":
    unittest.main()
