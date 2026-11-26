from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutual_recognition
import assurance_recognition
from assurance import (
    AssuranceInputError,
    RecognitionEngine,
    recognize,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class PublicApiAndReportingTests(unittest.TestCase):
    def test_keyword_fact_fields_and_zero_level_boundary(self):
        engine = RecognitionEngine(
            [{"from": "a", "to": "b", "to_level": 0}],
            target_authority="b",
        )
        result = engine.evaluate(
            authority="a",
            level=0,
            verification_method="sms",
            occurred_at=NOW - timedelta(minutes=1),
            min_level=0,
            at=NOW,
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.final_level, 0)

    def test_functional_api_accepts_common_aliases_and_nested_conditions(self):
        result = recognize(
            fact={
                "authority": "certifier",
                "level": "L3",
                "method": "hardware",
                "timestamp": (NOW - timedelta(seconds=30)).isoformat(),
            },
            rules=[
                {
                    "source": "certifier",
                    "target": "relying-party",
                    "target_level": "2",
                    "conditions": {
                        "verification_methods": ["hardware", "passkey"],
                        "target_business": "login",
                        "max_age_seconds": 60,
                    },
                }
            ],
            minimum_level=2,
            target_business="login",
            evaluated_at=NOW.isoformat(),
            target_authority="relying-party",
        )
        self.assertTrue(result.valid, result.message)
        self.assertEqual(result.chain.rule_ids, ("rule-1",))

    def test_result_and_full_report_are_json_serializable(self):
        result = recognize(
            {
                "authority": "a",
                "level": 3,
                "method": "sms",
                "occurred_at": NOW - timedelta(minutes=1),
            },
            [
                {
                    "from": "a",
                    "to": "b",
                    "to_level": 2,
                    "methods": ["hardware"],
                    "business": "finance",
                }
            ],
            min_level=3,
            business="finance",
            at=NOW,
            target_authority="b",
        )
        self.assertFalse(result.valid)
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertIn("METHOD_NOT_ALLOWED", payload)
        self.assertIn("closest", payload)
        self.assertGreaterEqual(len(result.candidates), 1)

    def test_invalid_call_input_raises_input_error(self):
        with self.assertRaises(AssuranceInputError):
            RecognitionEngine([]).evaluate(authority="a", level=3, at=NOW)

        with self.assertRaises(AssuranceInputError):
            RecognitionEngine([]).evaluate(
                {"authority": "a", "level": 3, "method": "m", "occurred_at": "not-a-date"},
                min_level=1,
            )

    def test_top_level_compatibility_modules_export_engine(self):
        self.assertIs(mutual_recognition.RecognitionEngine, RecognitionEngine)
        self.assertTrue(hasattr(mutual_recognition, "MutualRecognitionEngine"))
        self.assertTrue(hasattr(assurance_recognition, "AssuranceRecognitionEngine"))

    def test_reversed_rules_first_call_and_method_aliases(self):
        rules = [{"from": "a", "to": "b", "to_level": 2}]
        fact = {
            "authority": "a",
            "level": 3,
            "method": "m",
            "occurred_at": NOW - timedelta(seconds=1),
        }
        result = recognize(rules, fact, 2, at=NOW, target_authority="b")
        self.assertTrue(result.valid, result.message)

        engine = RecognitionEngine(rules)
        self.assertTrue(engine.match(fact, 2, at=NOW, target_authority="b").valid)
        self.assertTrue(engine.find_chain(fact, 2, at=NOW, target_authority="b").valid)

    def test_engine_is_reusable_and_interleaved_calls_do_not_leak_state(self):
        engine = RecognitionEngine(
            [
                {
                    "id": "hw",
                    "from": "partner",
                    "to": "us",
                    "to_level": 3,
                    "methods": ["hardware"],
                    "business": "admin",
                },
                {
                    "id": "sms",
                    "from": "partner",
                    "to": "us",
                    "to_level": 1,
                    "methods": ["sms"],
                    "business": "profile",
                },
            ],
            target_authority="us",
        )

        hw = {
            "authority": "partner",
            "level": 3,
            "method": "hardware",
            "occurred_at": NOW - timedelta(minutes=1),
        }
        sms = {
            "authority": "partner",
            "level": 3,
            "method": "sms",
            "occurred_at": NOW - timedelta(minutes=1),
        }

        first_hw = engine.evaluate(hw, min_level=3, business="admin", at=NOW)
        first_sms = engine.evaluate(sms, min_level=1, business="profile", at=NOW)
        second_hw = engine.evaluate(hw, min_level=3, business="admin", at=NOW)
        second_sms_admin = engine.evaluate(sms, min_level=3, business="admin", at=NOW)

        self.assertTrue(first_hw.valid)
        self.assertTrue(first_sms.valid)
        self.assertTrue(second_hw.valid)
        self.assertFalse(second_sms_admin.valid)
        self.assertEqual(first_hw.chain.rule_ids, second_hw.chain.rule_ids)
        self.assertEqual(first_sms.final_level, 1)
        self.assertEqual(second_hw.final_level, 3)
        self.assertEqual(second_sms_admin.reason, "METHOD_NOT_ALLOWED")

    def test_wildcard_methods_and_businesses_mean_unrestricted(self):
        result = RecognitionEngine(
            [
                {
                    "from": "a",
                    "to": "b",
                    "methods": "*",
                    "business": ["*"],
                    "to_level": 2,
                }
            ],
            target_authority="b",
        ).evaluate(
            {
                "authority": "a",
                "level": 3,
                "method": "any-new-method",
                "occurred_at": NOW - timedelta(seconds=1),
            },
            min_level=2,
            business="any-business",
            at=NOW,
        )
        self.assertTrue(result.valid, result.message)
        self.assertIsNone(result.chain.scope.businesses)

    def test_duration_string_aliases_are_supported(self):
        rule = {
            "from": "a",
            "to": "b",
            "to_level": 2,
            "max_age": "5m",
        }
        engine = RecognitionEngine([rule], target_authority="b")
        fresh = {
            "authority": "a",
            "level": 3,
            "method": "m",
            "occurred_at": NOW - timedelta(minutes=5),
        }
        stale = {
            "authority": "a",
            "level": 3,
            "method": "m",
            "occurred_at": NOW - timedelta(minutes=6),
        }
        self.assertTrue(engine.evaluate(fresh, min_level=2, at=NOW).valid)
        self.assertEqual(
            engine.evaluate(stale, min_level=2, at=NOW).reason,
            "AUTH_TIME_TOO_OLD",
        )


if __name__ == "__main__":
    unittest.main()
