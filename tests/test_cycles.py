from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assurance import RecognitionEngine, RecognitionRule, Scope, VerificationFact

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
FACT_TIME = NOW - timedelta(minutes=1)


def fact(method="sms", level=3, authority="a"):
    return VerificationFact(authority, level, method, FACT_TIME)


class CycleSafetyTests(unittest.TestCase):
    def test_symmetric_cycle_terminates_and_returns_original_scope(self):
        rules = [
            RecognitionRule("a", "b", rule_id="a-b", target_level=3),
            RecognitionRule("b", "a", rule_id="b-a", target_level=3),
        ]
        result = RecognitionEngine(rules).evaluate(
            fact(), required_level=3, target_authority="a", at=NOW
        )

        self.assertTrue(result.valid, result.message)
        self.assertTrue(result.chain.has_cycle)
        self.assertEqual(result.chain.authorities, ("a", "b", "a"))
        self.assertEqual(result.final_level, 3)
        self.assertEqual(result.effective_scope, Scope(methods=frozenset({"sms"})))
        self.assertEqual(result.effective_scope.methods, frozenset({"sms"}))
        self.assertIsNone(result.effective_scope.businesses)
        self.assertTrue(result.cycles)
        self.assertTrue(any(item["exact_state_already_seen"] for item in result.cycles))

    def test_cycle_cannot_restore_a_downgraded_level(self):
        rules = [
            RecognitionRule("a", "b", rule_id="down", target_level=2),
            RecognitionRule("b", "a", rule_id="attempt-up", target_level=3),
        ]
        engine = RecognitionEngine(rules)

        accepted = engine.evaluate(fact(), required_level=2, target_authority="a", at=NOW)
        self.assertTrue(accepted.valid, accepted.message)
        self.assertEqual(accepted.final_level, 2)
        self.assertEqual([step.output_level for step in accepted.chain.steps], [2, 2])
        self.assertEqual(accepted.chain.steps[1].note, "target_level_capped_to_prevent_upgrade")

        denied = engine.evaluate(fact(), required_level=3, target_authority="a", at=NOW)
        self.assertFalse(denied.valid)
        self.assertEqual(denied.reason, "LEVEL_TOO_LOW")
        self.assertEqual(denied.final_level, None)

    def test_cycle_cannot_change_sms_into_hardware_scope(self):
        rules = [
            RecognitionRule("a", "b", rule_id="loop-out", target_level=3),
            RecognitionRule("b", "a", rule_id="loop-back", target_level=3),
            RecognitionRule(
                "a",
                "sensitive",
                rule_id="hardware-only",
                target_level=3,
                allowed_methods=frozenset(["hardware_key"]),
            ),
        ]
        result = RecognitionEngine(rules).evaluate(
            fact(method="sms"), required_level=3, target_authority="sensitive", at=NOW
        )
        self.assertFalse(result.valid)
        method_failures = [c for c in result.candidates if c.code == "METHOD_NOT_ALLOWED"]
        self.assertTrue(method_failures)
        self.assertTrue(all(c.rule_id == "hardware-only" for c in method_failures))
        # The cyclic state remains SMS-only.
        cyclic = [item for item in result.cycles if item["repeated_authority"] == "a"]
        self.assertTrue(cyclic)
        self.assertEqual(cyclic[0]["scope"]["methods"], ["sms"])

    def test_cycle_business_restriction_is_monotonic(self):
        rules = [
            RecognitionRule(
                "a", "b", rule_id="restrict-to-x", target_level=3, businesses={"x"}
            ),
            RecognitionRule("b", "a", rule_id="back", target_level=3),
        ]
        allowed = RecognitionEngine(rules).evaluate(
            fact(), required_level=3, business="x", target_authority="a", at=NOW
        )
        self.assertTrue(allowed.valid, allowed.message)
        self.assertEqual(allowed.effective_scope.businesses, frozenset({"x"}))
        self.assertEqual(allowed.chain.authorities, ("a", "b", "a"))

        denied = RecognitionEngine(rules).evaluate(
            fact(), required_level=3, business="y", target_authority="a", at=NOW
        )
        self.assertFalse(denied.valid)
        self.assertEqual(denied.reason, "BUSINESS_NOT_ALLOWED")

    def test_downgrading_cycle_can_reach_exact_lower_rule_without_looping(self):
        rules = [
            RecognitionRule("a", "b", rule_id="down", target_level=2),
            RecognitionRule("b", "a", rule_id="back-level-2", target_level=2),
            RecognitionRule(
                "a",
                "lower-service",
                rule_id="exact-lower-exit",
                source_level=2,
                level_match="exact",
                target_level=2,
            ),
        ]
        result = RecognitionEngine(rules).evaluate(
            fact(level=3), required_level=2, target_authority="lower-service", at=NOW
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(
            result.chain.rule_ids, ("down", "back-level-2", "exact-lower-exit")
        )
        self.assertEqual(result.final_level, 2)

    def test_cycle_and_successful_diamond_path_are_interleaved_safely(self):
        rules = [
            RecognitionRule("a", "b", rule_id="ab", target_level=3),
            RecognitionRule("b", "done", rule_id="bd", target_level=3),
            RecognitionRule("a", "c", rule_id="ac", target_level=3),
            RecognitionRule("c", "a", rule_id="ca", target_level=3),
        ]
        result = RecognitionEngine(rules).evaluate(
            fact(), required_level=3, target_authority="done", at=NOW
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.chain.rule_ids, ("ab", "bd"))
        self.assertTrue(any(item["authorities"][:3] == ["a", "c", "a"] for item in result.cycles))

    def test_dense_cyclic_graph_completes_under_state_limit(self):
        authorities = [f"org-{i}" for i in range(6)]
        rules = []
        for i, source in enumerate(authorities):
            for target in authorities:
                if source != target:
                    rules.append(RecognitionRule(source, target, target_level=3))
        result = RecognitionEngine(rules).evaluate(
            fact(authority=authorities[0]),
            required_level=3,
            target_authority=authorities[-1],
            at=NOW,
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.chain.final_authority, authorities[-1])
        self.assertLessEqual(result.chain.length, 2)


if __name__ == "__main__":
    unittest.main()
