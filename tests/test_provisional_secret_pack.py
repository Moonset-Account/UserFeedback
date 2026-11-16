import json
import unittest
from pathlib import Path

from src.patterns import DEFAULT_DETECTION_RULES


class ProvisionalSecretPackTests(unittest.TestCase):
    def test_candidates_are_separate_from_stable_registry(self):
        path = Path(__file__).parents[1] / "packs" / "provisional-secret-candidates-v2.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["tier"], "provisional")
        self.assertEqual(len(data["rules"]), 6)
        stable_ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        self.assertTrue(stable_ids.isdisjoint({rule["rule_id"] for rule in data["rules"]}))
        for rule in data["rules"]:
            self.assertIn("qualification", rule["provenance"])
            self.assertNotIn("EXAMPLE", rule["regex"])


if __name__ == "__main__":
    unittest.main()
