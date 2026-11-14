"""End-to-end CLI tests against the bundled synthetic sample records.

  CM-30  sample run: statuses, separate per-checkpoint records, nearest
         observed checkpoint and global unattributable list all match
         hand-computed expectations
  CM-31  missing input file exits 2 with an error message
  CM-32  --fail-on-uncovered exits 1 while uncovered entries remain
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def run_cli(*extra):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "app.py"),
            "--entries", str(SAMPLES / "entries.json"),
            "--graph", str(SAMPLES / "graph.json"),
            "--checkpoints", str(SAMPLES / "checkpoints.json"),
            "--traces", str(SAMPLES / "traces.json"),
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def entry_by_id(report, eid):
    return next(e for e in report["entries"] if e["id"] == eid)


class TestCliSamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = run_cli("--format", "json")
        assert proc.returncode == 0, proc.stderr
        cls.report = json.loads(proc.stdout)

    def test_cm30_sample_statuses(self):
        # Expected statuses computed by hand from samples/*.
        expected = {
            "E1": "observed_covered",        # T1 covers CP1+CP2
            "E2": "unattributable",          # T2 recorded against old build
            "E3": "observed_covered",        # T5 covers CP3
            "E4": "unattributable",          # T4 claims unreachable CP1
            "E5": "no_reachable_checkpoint",  # report.gen reaches nothing
            "E6": "statically_reachable",    # reachable, never tested
        }
        actual = {e["id"]: e["status"] for e in self.report["entries"]}
        self.assertEqual(actual, expected)

    def test_cm30_sample_multi_checkpoint_recorded_separately(self):
        e1 = entry_by_id(self.report, "E1")
        # T1 hit CP1 and CP2 in one test run -> two coverage records.
        self.assertEqual(
            e1["coverage_records"],
            [
                {"test_id": "T1", "checkpoint_id": "CP1"},
                {"test_id": "T1", "checkpoint_id": "CP2"},
            ],
        )
        # CP3 is reachable from E1 but was never observed.
        self.assertEqual(e1["missing_checkpoints"], ["CP3"])

    def test_cm30_sample_nearest_observed_for_uncovered(self):
        # E2 sits in parser.shared where CP1 was observed -> distance 0.
        e2 = entry_by_id(self.report, "E2")
        self.assertEqual(
            e2["nearest_observed_checkpoint"],
            {"id": "CP1", "module": "parser.shared", "distance": 0,
             "path": ["parser.shared"]},
        )
        # E6 sits in store.cache, one hop from parser.shared/CP1.
        e6 = entry_by_id(self.report, "E6")
        self.assertEqual(
            e6["nearest_observed_checkpoint"],
            {"id": "CP1", "module": "parser.shared", "distance": 1,
             "path": ["store.cache", "parser.shared"]},
        )
        # E4 (admin.console) reaches nothing -> no nearest checkpoint.
        e4 = entry_by_id(self.report, "E4")
        self.assertIsNone(e4["nearest_observed_checkpoint"])

    def test_cm30_sample_unattributable_records(self):
        # T3 names unknown entry E99 -> global unattributable list.
        self.assertEqual(len(self.report["unattributable_traces"]), 1)
        rec = self.report["unattributable_traces"][0]
        self.assertEqual(rec["test_id"], "T3")
        self.assertEqual(rec["reason"], "unknown_entry")
        # T2 (build mismatch) attaches to E2; T6 (unknown CP9) attaches to E1.
        e2 = entry_by_id(self.report, "E2")
        self.assertEqual(e2["invalid_traces"][0]["reason"], "build_mismatch")
        e1 = entry_by_id(self.report, "E1")
        self.assertEqual(e1["invalid_traces"][0]["reason"], "unknown_checkpoint")

    def test_cm30_sample_uncovered_entries(self):
        self.assertEqual(self.report["uncovered_entries"], ["E2", "E4", "E6"])

    def test_cm31_missing_input_file_exits_2(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "app.py"),
                "--entries", str(SAMPLES / "does-not-exist.json"),
                "--graph", str(SAMPLES / "graph.json"),
                "--checkpoints", str(SAMPLES / "checkpoints.json"),
                "--traces", str(SAMPLES / "traces.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_cm32_fail_on_uncovered_exits_1(self):
        proc = run_cli("--fail-on-uncovered")
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
