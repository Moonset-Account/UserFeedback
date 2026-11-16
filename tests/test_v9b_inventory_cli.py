"""RED tests for V9b read-only inventory CLI."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestV9bInventoryCli(unittest.TestCase):
    def test_emits_versioned_deterministic_inventory_with_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("requests==2.31.0\nflask>=2.0\n", encoding="utf-8")
            command = [sys.executable, "-m", "src", "vuln", "inventory", "--root", str(root)]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            second = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            report = json.loads(first.stdout)
            self.assertEqual(report["schema"], "coderisktools.vulnerability.inventory")
            self.assertEqual(report["version"], 1)
            self.assertEqual(report["component_count"], 1)
            self.assertEqual(report["unresolved_count"], 1)
            self.assertEqual(report["components"][0]["name"], "requests")
            self.assertEqual(report["components"][0]["version"], "2.31.0")
            self.assertEqual(report["unresolved"][0]["name"], "flask")
            self.assertNotIn("/tmp/", first.stdout)

    def test_invalid_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "src", "vuln", "inventory", "--root", str(Path(directory) / "missing")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual(json.loads(result.stderr)["state"], "rejected")


if __name__ == "__main__":
    unittest.main()
