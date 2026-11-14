"""CLI-level tests for app.py (stdlib unittest only)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"

POLICY = {
    "services": {
        "svc-a": {
            "fingerprints": [
                {"fingerprint": "fp-old", "valid_from": "2026-11-14T00:00:00Z",
                 "valid_until": "2026-11-14T08:30:00Z"},
                {"fingerprint": "fp-new", "valid_from": "2026-11-14T08:00:00Z",
                 "valid_until": None},
            ],
            "min_samples": 2,
            "anomaly_ratio": 0.5,
        }
    }
}

RECORDS = "\n".join([
    json.dumps({"ts": "2026-11-14T08:31:00Z", "service": "svc-a",
                "conn_id": "c-1", "result": "success", "fingerprint": "fp-new"}),
    json.dumps({"ts": "2026-11-14T08:32:00Z", "service": "svc-a",
                "conn_id": "c-2", "result": "success", "fingerprint": "fp-old"}),
]) + "\n"


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(APP), *args],
            capture_output=True, text=True, timeout=30,
        )

    def test_cli_emits_alert_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records.jsonl"
            policy = Path(tmp) / "policy.json"
            records.write_text(RECORDS, encoding="utf-8")
            policy.write_text(json.dumps(POLICY), encoding="utf-8")
            proc = self.run_cli("--input", str(records), "--policy", str(policy))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(len(report["alerts"]), 1)
            self.assertEqual(report["alerts"][0]["window_start"],
                             "2026-11-14T08:30:00Z")
            self.assertEqual(report["alerts"][0]["anomalous_fingerprints"],
                             [{"fingerprint": "fp-old", "count": 1}])

    def test_cli_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records.jsonl"
            policy = Path(tmp) / "policy.json"
            out = Path(tmp) / "report.json"
            records.write_text(RECORDS, encoding="utf-8")
            policy.write_text(json.dumps(POLICY), encoding="utf-8")
            proc = self.run_cli("--input", str(records), "--policy", str(policy),
                                "--output", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(report["alerts"]), 1)

    def test_cli_missing_input_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps(POLICY), encoding="utf-8")
            proc = self.run_cli("--input", str(Path(tmp) / "nope.jsonl"),
                                "--policy", str(policy))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error:", proc.stderr)

    def test_cli_invalid_policy_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records.jsonl"
            policy = Path(tmp) / "policy.json"
            records.write_text(RECORDS, encoding="utf-8")
            policy.write_text(json.dumps({"services": {"svc-a": {}}}),
                              encoding="utf-8")
            proc = self.run_cli("--input", str(records), "--policy", str(policy))
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
