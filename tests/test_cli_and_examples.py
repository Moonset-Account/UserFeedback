import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliAndExampleTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "memory_accounting", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_normal_example_summary(self) -> None:
        result = self.run_cli("examples/normal_group.json", "--summary")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("physical_frames=3", result.stdout)
        self.assertIn("pid=A exclusive=4096 proportional=6144", result.stdout)
        self.assertIn("pid=B exclusive=4096 proportional=6144", result.stdout)

    def test_interleaved_example_reports_gap_and_new_generation(self) -> None:
        result = self.run_cli(
            "examples/interleaved_reuse_and_gap.json", "--summary"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("latest_accountable_position=3", result.stdout)
        self.assertIn("'position': 4", result.stdout)
        self.assertIn("physical_frames=1", result.stdout)
        self.assertIn("pid=B exclusive=4096", result.stdout)
        self.assertNotIn("pid=A", result.stdout)

    def test_json_output_is_parseable(self) -> None:
        result = self.run_cli("examples/normal_group.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["report"]["physical_frame_count"], 3)
        self.assertEqual(payload["latest_accountable_position"], 1)


if __name__ == "__main__":
    unittest.main()
