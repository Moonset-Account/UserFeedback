"""End-to-end tests: JSON scenario -> app.py CLI -> result JSON.

Uses only 127.0.0.1-free local subprocess/temp files and the stdlib.
Case ids TC-24..TC-27 are referenced from README.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from remediation.scenario import build_demo_scenario, run_scenario

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"


class ScenarioDemoTests(unittest.TestCase):
    # TC-24 built-in demo exercises every rule branch through the real runner
    def test_tc24_builtin_demo_trace(self):
        result = run_scenario(build_demo_scenario())
        plans = {p["anomaly_id"]: p for p in result["final_plans"]}

        self.assertEqual(plans["A1"]["status"], "cancelled")
        self.assertEqual(plans["A1"]["released_points"], 6)
        self.assertEqual(plans["A2"]["status"], "budget_exceeded")
        self.assertEqual(plans["A2"]["locked_points"], 0)
        self.assertEqual(plans["A3"]["status"], "pending_review")
        self.assertEqual(plans["A4"]["status"], "exempted")
        self.assertEqual(plans["A5"]["status"], "pending_execution")
        self.assertEqual(plans["A5"]["locked_points"], 8)
        # duplicate report reused the original plan (reused=True on shared plan)
        dup_events = [e for e in result["events"] if e.get("anomaly_id") == "A1"]
        self.assertGreaterEqual(len(dup_events), 2)
        self.assertTrue(dup_events[1]["plan"]["reused"])
        # after release, A6 (7 pts) fits the refilled 10-point budget
        self.assertEqual(plans["A6"]["day"], "2026-11-14")
        self.assertEqual(plans["A6"]["released_points"], 0)
        self.assertEqual(plans["A6"]["status"], "cancelled")
        # next UTC day: fresh budget lets A7 lock 9 points
        self.assertEqual(plans["A7"]["day"], "2026-11-15")
        self.assertEqual(plans["A7"]["status"], "pending_execution")
        self.assertEqual(plans["A7"]["locked_points"], 9)
        # cross-day cancel of A6 releases nothing
        cancel_a6 = [e for e in result["events"] if e["type"] == "cancel" and e["anomaly_id"] == "A6"][0]
        self.assertEqual(cancel_a6["plan"]["released_points"], 0)

        snap = result["budget_snapshot"]
        self.assertEqual(snap["platform"]["days"]["2026-11-14"]["locked"], 7)
        self.assertEqual(snap["platform"]["days"]["2026-11-15"]["locked"], 9)
        self.assertEqual(snap["data"]["days"]["2026-11-14"]["locked"], 8)

    # TC-25 app.py demo subcommand emits parseable JSON and exits 0
    def test_tc25_cli_demo_subprocess(self):
        proc = subprocess.run(
            [sys.executable, str(APP), "demo"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("final_plans", payload)
        self.assertIn("budget_snapshot", payload)
        self.assertEqual(len(payload["events"]), 11)

    # TC-26 app.py run with a scenario file and --output round-trips JSON
    def test_tc26_cli_run_scenario_file(self):
        scenario = {
            "budgets": {"sre": 5},
            "start_time": "2026-11-14T10:00:00Z",
            "events": [
                {
                    "type": "report",
                    "anomaly_id": "X1",
                    "team": "sre",
                    "environment": "staging",
                    "severity": "high",
                    "estimated_impact_points": 5,
                    "exemption_window": None,
                },
                {
                    "type": "report",
                    "anomaly_id": "X2",
                    "team": "sre",
                    "environment": "staging",
                    "severity": "low",
                    "estimated_impact_points": 1,
                    "exemption_window": None,
                },
                {"type": "cancel", "anomaly_id": "X1"},
                {"type": "advance", "days": 1},
                {
                    "type": "report",
                    "anomaly_id": "X3",
                    "team": "sre",
                    "environment": "staging",
                    "severity": "low",
                    "estimated_impact_points": 5,
                    "exemption_window": None,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            scen_path = Path(tmp) / "scenario.json"
            out_path = Path(tmp) / "result.json"
            scen_path.write_text(json.dumps(scenario), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(APP), "run", str(scen_path), "--output", str(out_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))

        plans = {p["anomaly_id"]: p for p in payload["final_plans"]}
        self.assertEqual(plans["X1"]["status"], "cancelled")
        self.assertEqual(plans["X1"]["released_points"], 5)
        self.assertEqual(plans["X2"]["status"], "budget_exceeded")
        self.assertEqual(plans["X3"]["day"], "2026-11-15")
        self.assertEqual(plans["X3"]["status"], "pending_execution")

    # TC-27 malformed input fails with a non-zero exit and a stderr message
    def test_tc27_cli_rejects_bad_scenario(self):
        bad = {"budgets": {"sre": 5}, "events": [{"type": "report"}]}
        with tempfile.TemporaryDirectory() as tmp:
            scen_path = Path(tmp) / "bad.json"
            scen_path.write_text(json.dumps(bad), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(APP), "run", str(scen_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("error", proc.stderr.lower())

        # missing file also exits non-zero
        proc = subprocess.run(
            [sys.executable, str(APP), "run", str(Path(tempfile.gettempdir()) / "no-such-file.json")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
