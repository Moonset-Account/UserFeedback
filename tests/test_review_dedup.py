import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.migration import (
    REVIEW_OBSERVATION_CORRECTED,
    VERDICT_ESTABLISHED,
)

from scenarios import (
    conclusion_spec,
    legacy,
    obs,
    old_rule,
    replaces_rule,
    req,
)


class TestReviewDedup(unittest.TestCase):
    def test_shared_corrected_observation_produces_single_review_item(self):
        old_rules = [
            old_rule("OLD-X", "lx", "结论X", [req("fx")]),
            old_rule("OLD-Y", "ly", "结论Y", [req("fx")]),
        ]
        new_rules = [
            replaces_rule(
                "NEW-X",
                "OLD-X",
                conclusion_spec("nx", "新X", [req("fx", "confirmed")]),
            ),
            replaces_rule(
                "NEW-Y",
                "OLD-Y",
                conclusion_spec("ny", "新Y", [req("fx", "confirmed")]),
            ),
        ]
        observations = [
            obs(
                "ox1",
                "fx",
                "s1",
                "initial",
                valid_from="2026-03-01T00:00:00Z",
                valid_to="2026-03-02T00:00:00Z",
            ),
            obs(
                "ox2",
                "fx",
                "s2",
                "official",
                valid_from="2026-03-02T00:00:00Z",
                supersedes="ox1",
                corrected_at="2026-03-02T00:00:00Z",
            ),
        ]
        legacy_conclusions = [
            legacy("OLD-X", ["ox1"]),
            legacy("OLD-Y", ["ox1"]),
        ]

        report = verify_migration(
            old_rules, new_rules, legacy_conclusions, observations
        )

        corrected = [
            item
            for item in report.re_review
            if item.reason == REVIEW_OBSERVATION_CORRECTED
        ]
        self.assertEqual(len(corrected), 1)
        self.assertEqual(corrected[0].obs_id, "ox1")

        # 两条结论仍各自独立迁移，并都引用更正后的具体来源
        self.assertEqual(len(report.migrated), 2)
        self.assertTrue(
            all(m.verdict == VERDICT_ESTABLISHED for m in report.migrated)
        )
        self.assertEqual(
            {m.conclusion_id for m in report.migrated}, {"nx", "ny"}
        )
        for migrated in report.migrated:
            self.assertEqual(migrated.evidence_obs_ids, ("ox2",))


if __name__ == "__main__":
    unittest.main()
