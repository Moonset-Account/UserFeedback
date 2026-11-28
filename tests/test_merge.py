import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import (
    RETAIN_MERGE_PENDING,
    REVIEW_BELOW_THRESHOLD,
    VERDICT_ESTABLISHED,
    VERDICT_UNCERTAIN,
)

from scenarios import (
    scenario_merge_single_branch,
    scenario_merge_uncertain,
)


class TestMerge(unittest.TestCase):
    def test_mutually_exclusive_branches_both_hold_yield_uncertain(self):
        report = verify_migration(*scenario_merge_uncertain())

        # 两条旧结论都不应原样保留，也没有复核项
        self.assertEqual(len(report.retained), 0)
        self.assertEqual(len(report.re_review), 0)
        self.assertEqual(len(report.migrated), 1)

        item = report.migrated[0]
        self.assertEqual(item.verdict, VERDICT_UNCERTAIN)
        self.assertEqual(item.conclusion_id, "new-m")
        self.assertEqual(item.new_rule_id, "NEW-M")
        self.assertEqual(set(item.source_rule_ids), {"OLD-M1", "OLD-M2"})
        self.assertEqual(set(item.evidence_obs_ids), {"obs-m1", "obs-m2"})
        self.assertIn("不确定", item.basis)

    def test_single_supported_branch_migrates_as_established(self):
        report = verify_migration(*scenario_merge_single_branch())

        # 分支一成立 -> OLD-N1 迁移；分支二不成立 -> OLD-N2 保留并复核
        established = [m for m in report.migrated if m.verdict == VERDICT_ESTABLISHED]
        self.assertEqual(len(established), 1)
        self.assertEqual(established[0].conclusion_id, "branch-n1")
        self.assertEqual(established[0].source_rule_ids, ("OLD-N1",))

        retained = report.retained
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].legacy_rule_id, "OLD-N2")
        self.assertEqual(retained[0].reason, RETAIN_MERGE_PENDING)
        self.assertIn("branch-n1", retained[0].detail)

        review_reasons = {item.reason for item in report.re_review}
        self.assertIn(REVIEW_BELOW_THRESHOLD, review_reasons)
        flagged = next(i for i in report.re_review if i.fact == "fact-n2")
        self.assertEqual(flagged.evidence_level, "initial")


if __name__ == "__main__":
    unittest.main()
