from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assurance import (
    RecognitionEngine,
    RecognitionRule,
    VerificationFact,
    recognize,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
RECENT = NOW - timedelta(minutes=1)


def fact(authority="partner", level=3, method="hardware_key", occurred_at=RECENT):
    return VerificationFact(authority, level, method, occurred_at)


class NormalRecognitionTests(unittest.TestCase):
    def test_direct_mapping_meets_exact_minimum_boundary(self):
        rule = RecognitionRule(
            "partner", "our-org", rule_id="p-to-us", target_level=3
        )
        result = RecognitionEngine([rule]).evaluate(
            fact(), required_level=3, target_authority="our-org", at=NOW
        )

        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.reason, None)
        self.assertEqual(result.final_level, 3)
        self.assertEqual(result.chain.authorities, ("partner", "our-org"))
        self.assertEqual(result.chain.rule_ids, ("p-to-us",))
        self.assertFalse(result.chain.has_cycle)

    def test_level_above_minimum_succeeds(self):
        rule = RecognitionRule("a", "b", target_level=3)
        result = RecognitionEngine([rule]).evaluate(
            fact(authority="a", level=4), required_level=2, at=NOW
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.final_level, 3)

    def test_downgrade_to_exact_boundary_is_accepted(self):
        rule = RecognitionRule("a", "b", rule_id="downgrade", target_level=2)
        engine = RecognitionEngine([rule])

        accepted = engine.evaluate(fact(authority="a", level=3), 2, at=NOW)
        self.assertTrue(accepted.valid)
        self.assertEqual(accepted.final_level, 2)
        self.assertTrue(accepted.chain.steps[0].downgraded)

        denied = engine.evaluate(fact(authority="a", level=3), 3, at=NOW)
        self.assertFalse(denied.valid)
        self.assertEqual(denied.reason, "LEVEL_TOO_LOW")
        self.assertEqual(denied.final_level, None)
        self.assertIsNotNone(denied.closest_chain)
        self.assertEqual(denied.closest_chain.final_level, 2)
        self.assertIs(denied.best_chain, denied.closest_chain)
        self.assertEqual(denied.best_final_level, 2)
        self.assertEqual(denied.closest.projected_level, 2)
        self.assertIn("差 1 级", denied.message)

    def test_multi_hop_proof_chain(self):
        rules = [
            RecognitionRule("a", "b", rule_id="a-b", target_level=3),
            RecognitionRule("b", "c", rule_id="b-c", target_level=2),
        ]
        result = RecognitionEngine(rules).evaluate(
            fact(authority="a", level=3),
            required_level=2,
            target_authority="c",
            at=NOW,
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.chain.authorities, ("a", "b", "c"))
        self.assertEqual(result.chain.rule_ids, ("a-b", "b-c"))
        self.assertEqual(result.final_level, 2)

    def test_best_path_is_highest_final_level(self):
        rules = [
            RecognitionRule("a", "low", rule_id="low-path", target_level=1),
            RecognitionRule("a", "high", rule_id="high-path", target_level=3),
        ]
        result = RecognitionEngine(rules).evaluate(fact(authority="a"), 2, at=NOW)
        self.assertTrue(result.valid)
        self.assertEqual(result.final_level, 3)
        self.assertEqual(result.chain.final_authority, "high")

    def test_dict_rules_and_functional_api(self):
        rule = {
            "id": "dict-rule",
            "from": "partner",
            "to": "us",
            "from_level": "三级",
            "to_level": "L2",
            "methods": ["hardware_key", "platform_authenticator"],
            "business": "console",
            "max_age_seconds": 300,
        }
        result = recognize(
            {
                "issuer": "partner",
                "assurance_level": "三级",
                "authentication_method": "hardware_key",
                "timestamp": RECENT.isoformat(),
            },
            [rule],
            min_level=2,
            target_business="console",
            now=NOW,
            target_authority="us",
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.final_level, 2)
        self.assertEqual(result.chain.rule_ids, ("dict-rule",))

    def test_direct_trusted_authority_zero_hop(self):
        result = RecognitionEngine(trusted_authorities={"root"}).evaluate(
            fact(authority="root", level=3),
            required_level=3,
            at=NOW,
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.chain.length, 0)
        self.assertEqual(result.chain.authorities, ("root",))

        untrusted = RecognitionEngine(trusted_authorities={"root"}).evaluate(
            fact(authority="other", level=3), required_level=3, at=NOW
        )
        self.assertFalse(untrusted.valid)
        self.assertEqual(untrusted.reason, "AUTHORITY_NOT_TRUSTED")

    def test_allow_direct_fact_opt_in(self):
        result = RecognitionEngine([], allow_direct_fact=True).evaluate(
            fact(authority="any", level=3), required_level=3, at=NOW
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.chain.length, 0)

    def test_long_acyclic_chain_is_not_capped_at_small_depth(self):
        n = 160
        rules = [
            RecognitionRule(f"a{i}", f"a{i + 1}", rule_id=f"r{i}", target_level=3)
            for i in range(n)
        ]
        result = RecognitionEngine(rules).evaluate(
            fact(authority="a0", level=3),
            required_level=3,
            target_authority=f"a{n}",
            at=NOW,
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.chain.length, n)
        self.assertEqual(result.chain.final_authority, f"a{n}")


if __name__ == "__main__":
    unittest.main()
