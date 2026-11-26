from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topology_audit import audit_from_file  # noqa: E402

EXAMPLE = ROOT / "examples" / "bridge_topology.json"


class ExampleAndCliTests(unittest.TestCase):
    def test_example_file_audits_to_unique_mapping(self) -> None:
        report = audit_from_file(EXAMPLE)
        self.assertEqual(1, report.match_count)
        self.assertEqual("ttyUSB1", report.mapping["alpha-meter"])
        self.assertEqual("ttyUSB0", report.mapping["alpha-valve"])

    def test_demo_module_prints_parseable_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "topology_audit.demo", str(EXAMPLE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, proc.returncode, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(1, payload["match_count"])
        self.assertIn("alpha-meter", payload["mapping"])


if __name__ == "__main__":
    unittest.main()
