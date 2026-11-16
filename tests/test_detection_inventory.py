import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.detection_inventory import build_inventory, write_inventory


class DetectionInventoryTests(unittest.TestCase):
    def test_authoritative_registry_reconciles_to_299(self):
        inventory = build_inventory()
        self.assertEqual(inventory["counts"]["native_rule_count"], 299)
        self.assertEqual(inventory["counts"]["line_rule_count"], 267)
        self.assertEqual(inventory["counts"]["context_rule_count"], 32)
        self.assertEqual(sum(inventory["counts"]["category"].values()), 299)
        self.assertEqual(inventory["counts"]["infrastructure_partition"], {
            "I0_iac_cloud": 8,
            "C0_containers_kubernetes": 13,
        })

    def test_detector_ids_are_unique_and_families_are_seeded(self):
        inventory = build_inventory()
        detectors = inventory["detectors"]
        self.assertEqual(len({item["detector_id"] for item in detectors}), 299)
        self.assertTrue(all(item["family_id"].startswith("FAM-") for item in detectors))
        self.assertEqual(
            {item["family_id"] for item in inventory["families"]},
            {"FAM-SECRET", "FAM-CI", "FAM-IAC-CLOUD", "FAM-CONTAINERS-KUBERNETES", "FAM-SUPPLY-CHAIN", "FAM-AI-MCP"},
        )

    def test_cli_uses_canonical_checkout_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "cli.json"
            subprocess.run([sys.executable, "tools/detection_inventory.py", "--output", str(output)], check=True)
            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["counts"]["native_rule_count"], 299)
        self.assertEqual(document["counts"]["line_rule_count"], 267)

    def test_output_is_deterministic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            write_inventory(str(first))
            write_inventory(str(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            document = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], "coderisktools.detection-inventory")
            self.assertNotIn("/workspace", first.read_text(encoding="utf-8"))
            self.assertNotIn("timestamp", first.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
