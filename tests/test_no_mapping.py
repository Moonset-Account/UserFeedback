import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import RETAIN_NO_MAPPING

from scenarios import scenario_no_mapping


class TestNoMapping(unittest.TestCase):
    def test_legacy_rule_without_successor_is_retained(self):
        report = verify_migration(*scenario_no_mapping())

        self.assertEqual(len(report.migrated), 0)
        self.assertEqual(len(report.re_review), 0)
        self.assertEqual(len(report.retained), 1)

        retained = report.retained[0]
        self.assertEqual(retained.legacy_rule_id, "OLD-Z")
        self.assertEqual(retained.reason, RETAIN_NO_MAPPING)
        self.assertIn("没有承接规则", retained.detail)
        self.assertIsNone(retained.valid_to)


if __name__ == "__main__":
    unittest.main()
