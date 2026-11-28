import _bootstrap  # noqa: F401

import unittest

from attribution_migration import verify_migration
from attribution_migration.model import RuleValidationError

from scenarios import (
    conclusion_spec,
    legacy,
    merge_rule,
    obs,
    old_rule,
    replaces_rule,
    req,
    split_rule,
)


def _base_rule():
    return old_rule("OLD-1", "legacy-1", "旧规则", [req("fact-1")])


def _base_observation(**overrides):
    base = obs("obs-1", "fact-1", "src-1", "confirmed")
    base.update(overrides)
    return base


class TestValidation(unittest.TestCase):
    def _run(self, *, old_rules=None, new_rules=None, legacy_conclusions=None,
             observations=None):
        return verify_migration(
            old_rules if old_rules is not None else [_base_rule()],
            new_rules
            if new_rules is not None
            else [
                replaces_rule(
                    "NEW-1", "OLD-1",
                    conclusion_spec("new-1", "新规则", [req("fact-1")]),
                )
            ],
            legacy_conclusions
            if legacy_conclusions is not None
            else [legacy("OLD-1", ["obs-1"])],
            observations if observations is not None else [_base_observation()],
        )

    def test_unknown_evidence_level_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(observations=[_base_observation(evidence_level="hearsay")])

    def test_bad_timestamp_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(observations=[_base_observation(valid_from="not-a-time")])

    def test_valid_to_before_valid_from_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                observations=[
                    _base_observation(
                        valid_from="2026-03-02T00:00:00Z",
                        valid_to="2026-03-01T00:00:00Z",
                    )
                ]
            )

    def test_supersedes_missing_target_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                observations=[
                    _base_observation(),
                    obs(
                        "obs-2", "fact-1", "src-2", "official",
                        supersedes="obs-missing",
                        corrected_at="2026-03-02T00:00:00Z",
                    ),
                ]
            )

    def test_correction_chain_cycle_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                observations=[
                    obs(
                        "obs-1", "fact-1", "src-1", "confirmed",
                        supersedes="obs-2",
                        corrected_at="2026-03-02T00:00:00Z",
                    ),
                    obs(
                        "obs-2", "fact-1", "src-2", "official",
                        supersedes="obs-1",
                        corrected_at="2026-03-03T00:00:00Z",
                    ),
                ]
            )

    def test_corrected_at_without_supersedes_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                observations=[
                    _base_observation(corrected_at="2026-03-02T00:00:00Z"),
                ]
            )

    def test_new_rule_referencing_missing_old_rule_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                new_rules=[
                    replaces_rule(
                        "NEW-X", "OLD-GHOST",
                        conclusion_spec("new-x", "幽灵", [req("fact-1")]),
                    )
                ]
            )

    def test_old_rule_referenced_by_two_successors_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                new_rules=[
                    replaces_rule(
                        "NEW-1", "OLD-1",
                        conclusion_spec("new-1", "新规则1", [req("fact-1")]),
                    ),
                    split_rule(
                        "NEW-2", "OLD-1",
                        [
                            conclusion_spec("new-2a", "子a", [req("fact-1")]),
                            conclusion_spec("new-2b", "子b", [req("fact-1")]),
                        ],
                    ),
                ]
            )

    def test_legacy_referencing_missing_observation_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(legacy_conclusions=[legacy("OLD-1", ["obs-ghost"])])

    def test_merge_branch_count_mismatch_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                old_rules=[
                    _base_rule(),
                    old_rule("OLD-2", "legacy-2", "旧规则2", [req("fact-2")]),
                ],
                new_rules=[
                    merge_rule(
                        "NEW-M", ["OLD-1", "OLD-2"],
                        [conclusion_spec("b1", "分支1", [req("fact-1")])],
                    )
                ],
            )

    def test_split_needs_at_least_two_conclusions(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                new_rules=[
                    split_rule(
                        "NEW-S", "OLD-1",
                        [conclusion_spec("only", "唯一", [req("fact-1")])],
                    )
                ]
            )

    def test_duplicate_observation_id_rejected(self):
        with self.assertRaises(RuleValidationError):
            self._run(
                observations=[
                    _base_observation(),
                    obs("obs-1", "fact-2", "src-2", "confirmed"),
                ]
            )

    def test_unsupported_legacy_verdict_rejected(self):
        bad_legacy = legacy("OLD-1", ["obs-1"])
        bad_legacy["verdict"] = "uncertain"
        with self.assertRaises(RuleValidationError):
            self._run(legacy_conclusions=[bad_legacy])

    def test_empty_input_rejected(self):
        with self.assertRaises(RuleValidationError):
            verify_migration([], [], [], [])


if __name__ == "__main__":
    unittest.main()
