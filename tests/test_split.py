import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import (
    REVIEW_LACKS_INDEPENDENT,
    RETAIN_SPLIT_PENDING,
    VERDICT_ESTABLISHED,
)

from scenarios import scenario_split_ok, scenario_split_same_source


class TestSplit(unittest.TestCase):
    def test_split_migrates_all_sub_conclusions_with_independent_sources(self):
        report = verify_migration(*scenario_split_ok())

        self.assertEqual(len(report.retained), 0)
        self.assertEqual(len(report.re_review), 0)
        self.assertEqual(len(report.migrated), 2)

        by_id = {item.conclusion_id: item for item in report.migrated}
        self.assertEqual(set(by_id), {"new-s1", "new-s2"})
        self.assertEqual(by_id["new-s1"].evidence_obs_ids, ("obs-s1",))
        self.assertEqual(by_id["new-s2"].evidence_obs_ids, ("obs-s2",))
        for item in report.migrated:
            self.assertEqual(item.verdict, VERDICT_ESTABLISHED)
            self.assertEqual(item.new_rule_id, "NEW-S")
            self.assertEqual(item.source_rule_ids, ("OLD-S",))

        # 两个子结论确实来自不同来源
        sources = {item.evidence_obs_ids[0] for item in report.migrated}
        self.assertEqual(len(sources), 2)

    def test_split_with_shared_source_is_retained_and_flagged(self):
        report = verify_migration(*scenario_split_same_source())

        self.assertEqual(len(report.migrated), 0)
        self.assertEqual(len(report.retained), 1)
        self.assertEqual(report.retained[0].reason, RETAIN_SPLIT_PENDING)
        self.assertIn("new-sx2", report.retained[0].detail)

        independence_reviews = [
            item for item in report.re_review
            if item.reason == REVIEW_LACKS_INDEPENDENT
        ]
        self.assertTrue(independence_reviews)
        flagged_facts = {item.fact for item in independence_reviews}
        # 两个独立要求都参与冲突诊断
        self.assertEqual(flagged_facts, {"fact-x1", "fact-x2"})
        for item in independence_reviews:
            self.assertEqual(item.related_rule_id, "NEW-SX")
            self.assertIn("src-same", item.detail)

        # 低等级的旁路观测不应被当作候选证据
        for item in independence_reviews:
            self.assertNotIn("obs-x1-low", item.obs_id or "")

    def test_independence_reason_distinct_from_threshold_reason(self):
        # 同来源场景证据等级本身达标，问题只在独立来源
        report = verify_migration(*scenario_split_same_source())
        reasons = {item.reason for item in report.re_review}
        self.assertIn(REVIEW_LACKS_INDEPENDENT, reasons)
        self.assertEqual(
            {item.reason for item in report.retained},
            {RETAIN_SPLIT_PENDING},
        )


if __name__ == "__main__":
    unittest.main()
