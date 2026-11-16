import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SecretTierInventoryTests(unittest.TestCase):
    def test_inventory_reports_shortfall_without_inflation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tiers.json"
            subprocess.run([sys.executable, "tools/secret_tier_inventory.py", "--output", str(output)], check=True)
            data = json.loads(output.read_text())
        self.assertEqual(data["tiers"]["stable"], 187)
        self.assertEqual(data["tiers"]["provisional"], 6)
        self.assertEqual(data["tiers"]["contextual_external_pack"], 28)
        self.assertEqual(data["shortfall"]["stable_core"], 113)
        self.assertEqual(data["targets"]["stable_core"], 300)


if __name__ == "__main__":
    unittest.main()
