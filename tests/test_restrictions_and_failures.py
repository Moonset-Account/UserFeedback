from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assurance import RecognitionEngine, RecognitionRule, VerificationFact

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def make_fact(method="sms", minutes_ago=1, authority="partner", level=3):
    return VerificationFact(
        authority, level, method, NOW - timedelta(minutes=minutes_ago)
    )


class MethodAndBusinessScopeTests(unittest.TestCase):
    def setUp(self):
        self.rules = [
            RecognitionRule(
                "partner",
                "our-org",
                rule_id="hardware-sensitive",
                target_level=3,
                allowed_methods=frozenset(["hardware_key", "platform_authenticator"]),
                businesses=frozenset(["payments", "console-admin"]),
                max_age=timedelta(minutes=10),
            ),
            RecognitionRule(
                "partner",
                "our-org",
                rule_id="sms-standard",
                target_level=1,
                allowed_methods=frozenset(["sms"]),
                businesses=frozenset(["profile"]),
                max_age=timedelta(minutes=10),
            ),
        ]
        self.engine = RecognitionEngine(self.rules, target_authority="our-org")

    def test_hardware_level_three_covers_sensitive_business(self):
        result = self.engine.evaluate(
            make_fact("hardware_key"), required_level=3, business="payments", at=NOW
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.final_level, 3)
        self.assertEqual(result.chain.rule_ids, ("hardware-sensitive",))

    def test_sms_level_three_does_not_inherit_hardware_scope(self):
        result = self.engine.evaluate(
            make_fact("sms"), required_level=3, business="payments", at=NOW
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "METHOD_NOT_ALLOWED")
        self.assertEqual(result.closest.rule_id, "hardware-sensitive")
        self.assertIn("sms", result.message)
        self.assertTrue(
            any(v["code"] == "METHOD_NOT_ALLOWED" for v in result.closest.violations)
        )

    def test_sms_has_narrow_standard_business_proof(self):
        result = self.engine.evaluate(
            make_fact("sms"), required_level=1, business="profile", at=NOW
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.final_level, 1)
        self.assertEqual(result.chain.rule_ids, ("sms-standard",))

    def test_sms_cannot_use_standard_rule_for_sensitive_business(self):
        result = self.engine.evaluate(
            make_fact("sms"), required_level=1, business="payments", at=NOW
        )
        self.assertFalse(result.valid)
        codes = {c.code for c in result.candidates}
        self.assertIn("BUSINESS_NOT_ALLOWED", codes)
        self.assertIn("METHOD_NOT_ALLOWED", codes)

    def test_unknown_business_is_rejected_by_business_restriction(self):
        rule = RecognitionRule(
            "a", "b", businesses=frozenset(["allowed-service"])
        )
        result = RecognitionEngine([rule], target_authority="b").evaluate(
            make_fact(authority="a"),
            required_level=1,
            business="other-service",
            at=NOW,
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "BUSINESS_NOT_ALLOWED")
        self.assertEqual(result.closest.detail["rule"]["rule_id"], "rule-1")


class TimeBoundaryTests(unittest.TestCase):
    def test_max_age_boundary_is_inclusive(self):
        rule = RecognitionRule("a", "b", target_level=2, max_age=timedelta(seconds=300))
        fact = VerificationFact("a", 3, "hardware", NOW - timedelta(seconds=300))
        result = RecognitionEngine([rule], target_authority="b").evaluate(
            fact, 2, at=NOW
        )
        self.assertTrue(result.valid, result.message)

    def test_one_second_beyond_max_age_is_denied(self):
        rule = RecognitionRule("a", "b", target_level=2, max_age=timedelta(seconds=300))
        fact = VerificationFact("a", 3, "hardware", NOW - timedelta(seconds=301))
        result = RecognitionEngine([rule], target_authority="b").evaluate(
            fact, 2, at=NOW
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "AUTH_TIME_TOO_OLD")
        self.assertIn("max_age_seconds", result.closest.violations[0]["rule_time_scope"])

    def test_future_occurrence_is_denied(self):
        rule = RecognitionRule("a", "b", target_level=2, max_age=timedelta(minutes=5))
        fact = VerificationFact("a", 3, "hardware", NOW + timedelta(seconds=30))
        result = RecognitionEngine([rule], target_authority="b").evaluate(
            fact, 2, at=NOW
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "AUTH_TIME_IN_FUTURE")

    def test_clock_skew_tolerance_accepts_small_future_time(self):
        rule = RecognitionRule("a", "b", target_level=2, max_age=timedelta(minutes=5))
        fact = VerificationFact("a", 3, "hardware", NOW + timedelta(seconds=30))
        engine = RecognitionEngine([rule], clock_skew=timedelta(seconds=60))
        result = engine.evaluate(fact, 2, target_authority="b", at=NOW)
        self.assertTrue(result.valid, result.message)

    def test_absolute_window_boundaries_are_inclusive(self):
        start = NOW - timedelta(hours=2)
        end = NOW
        rule = RecognitionRule("a", "b", not_before=start, not_after=end)
        engine = RecognitionEngine([rule], target_authority="b")

        at_start = VerificationFact("a", 3, "m", start)
        self.assertTrue(engine.evaluate(at_start, 1, at=NOW).valid)

        at_end = VerificationFact("a", 3, "m", end)
        self.assertTrue(engine.evaluate(at_end, 1, at=NOW).valid)

        too_early = VerificationFact("a", 3, "m", start - timedelta(seconds=1))
        result = engine.evaluate(too_early, 1, at=NOW)
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "AUTH_TIME_BEFORE_WINDOW")

        too_late = VerificationFact("a", 3, "m", end + timedelta(seconds=1))
        result = engine.evaluate(too_late, 1, at=NOW)
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "AUTH_TIME_AFTER_WINDOW")

    def test_rule_effective_interval_is_checked_at_evaluation_time(self):
        rule = RecognitionRule(
            "a",
            "b",
            effective_from=NOW - timedelta(days=1),
            effective_until=NOW + timedelta(days=1),
        )
        fact = VerificationFact("a", 3, "m", NOW - timedelta(minutes=1))
        engine = RecognitionEngine([rule], target_authority="b")
        self.assertTrue(engine.evaluate(fact, 1, at=NOW).valid)

        before = engine.evaluate(fact, 1, at=NOW - timedelta(days=2))
        self.assertFalse(before.valid)
        self.assertEqual(before.reason, "RULE_NOT_YET_EFFECTIVE")

        after = engine.evaluate(fact, 1, at=NOW + timedelta(days=2))
        self.assertFalse(after.valid)
        self.assertEqual(after.reason, "RULE_EXPIRED")


class GraphFailureTests(unittest.TestCase):
    def test_unknown_authority_gives_specific_reason(self):
        result = RecognitionEngine([]).evaluate(
            VerificationFact("ghost", 3, "sms", NOW), 2, at=NOW
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "UNKNOWN_AUTHORITY")
        self.assertEqual(result.closest.authority, "ghost")

    def test_missing_target_authority_reports_nearest_dead_end(self):
        rules = [RecognitionRule("a", "b", target_level=3)]
        result = RecognitionEngine(rules).evaluate(
            VerificationFact("a", 3, "m", NOW),
            3,
            target_authority="c",
            at=NOW,
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "TARGET_AUTHORITY_NOT_REACHED")
        self.assertEqual(result.closest.authority, "b")
        self.assertEqual(result.closest.path_authorities, ("a", "b"))

    def test_source_level_too_low_reason(self):
        rules = [RecognitionRule("a", "b", source_level=3, target_level=3)]
        result = RecognitionEngine(rules).evaluate(
            VerificationFact("a", 2, "m", NOW), 2, at=NOW
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "SOURCE_LEVEL_TOO_LOW")

    def test_exact_source_level_mismatch_for_stronger_level(self):
        rules = [
            RecognitionRule(
                "a", "b", source_level=3, target_level=2, level_match="exact"
            )
        ]
        result = RecognitionEngine(rules).evaluate(
            VerificationFact("a", 4, "m", NOW), 2, at=NOW
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "SOURCE_LEVEL_MISMATCH")


if __name__ == "__main__":
    unittest.main()
