import json
import unittest
from pathlib import Path

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES


PACK = Path(__file__).parents[1] / "packs" / "core-detections-v2.json"


class SourcePackMigrationTests(unittest.TestCase):
    def test_source_pack_covers_all_native_detectors_without_changes(self):
        data = json.loads(PACK.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "coderisktools.rule-source-pack")
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["detector_count"], 299)
        self.assertEqual(len(data["rules"]), 267)
        self.assertEqual(len(data["context_rules"]), 32)
        source_rules = {rule["rule_id"]: rule for rule in data["rules"]}
        current_rules = {rule.rule_id: rule for rule in DEFAULT_DETECTION_RULES}
        self.assertEqual(set(source_rules), set(current_rules))
        for rule_id, current in current_rules.items():
            self.assertEqual(source_rules[rule_id]["regex"], current.regex)
            self.assertEqual(tuple(source_rules[rule_id]["file_globs"]), current.file_globs)
        self.assertEqual({rule["rule_id"] for rule in data["context_rules"]}, {rule.rule_id for rule in DEFAULT_CONTEXT_RULES})

    def test_every_source_rule_has_provenance(self):
        data = json.loads(PACK.read_text(encoding="utf-8"))
        for rule in data["rules"] + data["context_rules"]:
            provenance = rule["provenance"]
            self.assertTrue(provenance["source"])
            self.assertTrue(provenance["url"])
            self.assertTrue(provenance["license"])
            self.assertEqual(provenance["source_lock"], "66924ea")


if __name__ == "__main__":
    unittest.main()
