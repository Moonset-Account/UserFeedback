import hashlib
import json
import unittest
from pathlib import Path

from src.patterns import match_context_rules, match_rules
from tools.golden_corpus import build_corpus


MANIFEST = Path(__file__).parent / "corpora" / "golden" / "manifest.json"


class GoldenParityCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_is_complete_and_secret_safe(self):
        self.assertEqual(self.document["counts"], {"covered": 235, "expected_detectors": 235, "unreachable": 0})
        self.assertEqual(len(self.document["cases"]), 235)
        self.assertEqual(self.document["known_unreachable"], [])
        raw_cases = json.dumps(self.document["cases"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(raw_cases).hexdigest(), self.document["fixture_sha256"])
        serialized = MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("/workspace", serialized)
        self.assertNotIn("timestamp", serialized)

    def test_legacy_matcher_replays_exact_detector_ids(self):
        runtime_cases = {case["case_id"]: case for case in build_corpus()["cases"]}
        for case in self.document["cases"]:
            with self.subTest(case=case["case_id"]):
                runtime_case = runtime_cases[case["case_id"]]
                if runtime_case["type"] == "synthetic-line":
                    lines = [(number, content) for number, content in runtime_case["lines"]]
                    found = match_rules(lines[0][1], runtime_case["filepath"])
                    ids = sorted(rule.rule_id for rule, _match in found)
                else:
                    lines = [(number, content) for number, content in runtime_case["lines"]]
                    found = match_context_rules(lines, runtime_case["filepath"])
                    ids = sorted(item.rule.rule_id for item in found)
                self.assertEqual(ids, case["expected_rule_ids"])


if __name__ == "__main__":
    unittest.main()
