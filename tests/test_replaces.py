import _bootstrap  # noqa: F401

import json
import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import (
    REVIEW_BELOW_THRESHOLD,
    RETAIN_THRESHOLD,
    VERDICT_ESTABLISHED,
)

from scenarios import (
    scenario_replaces_below_threshold,
    scenario_replaces_ok,
)


class TestReplaces(unittest.TestCase):
    def test_direct_migration_when_evidence_meets_new_rule(self):
        report = verify_migration(*scenario_replaces_ok())

        self.assertEqual(len(report.retained), 0)
        self.assertEqual(len(report.re_review), 0)
        self.assertEqual(len(report.migrated), 1)

        item = report.migrated[0]
        self.assertEqual(item.conclusion_id, "new-a")
        self.assertEqual(item.verdict, VERDICT_ESTABLISHED)
        self.assertEqual(item.new_rule_id, "NEW-A")
        self.assertEqual(item.source_rule_ids, ("OLD-A",))
        self.assertEqual(item.evidence_obs_ids, ("obs-a1",))
        self.assertIsNone(item.valid_to)

    def test_below_threshold_keeps_legacy_and_flags_review(self):
        report = verify_migration(*scenario_replaces_below_threshold())

        self.assertEqual(len(report.migrated), 0)
        self.assertEqual(len(report.retained), 1)
        self.assertEqual(len(report.re_review), 1)

        retained = report.retained[0]
        self.assertEqual(retained.legacy_rule_id, "OLD-B")
        self.assertEqual(retained.reason, RETAIN_THRESHOLD)
        self.assertIn("official", retained.detail)

        review = report.re_review[0]
        self.assertEqual(review.reason, REVIEW_BELOW_THRESHOLD)
        self.assertEqual(review.obs_id, "obs-b1")
        self.assertEqual(review.fact, "fact-b")
        self.assertEqual(review.evidence_level, "preliminary")
        self.assertEqual(review.related_rule_id, "NEW-B")

    def test_report_is_json_serializable(self):
        report = verify_migration(*scenario_replaces_ok())
        encoded = json.dumps(report.to_dict(), ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["migrated"][0]["conclusion_id"], "new-a")
        self.assertIsNone(decoded["migrated"][0]["valid_to"])


if __name__ == "__main__":
    unittest.main()
