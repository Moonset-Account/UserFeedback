import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests._fixtures import EXAMPLE_DIR

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO_ROOT, "app.py")


class CliTest(unittest.TestCase):
    def _run(self, *extra):
        cmd = [
            sys.executable,
            "-I",
            APP,
            *extra,
            "--inventory", os.path.join(EXAMPLE_DIR, "inventory.json"),
            "--subjects", os.path.join(EXAMPLE_DIR, "subjects_v1.json"),
            "--snapshot", os.path.join(EXAMPLE_DIR, "snapshot.json"),
        ]
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )
        return proc

    def test_analyze_emits_findings_for_all_services(self):
        proc = self._run("analyze")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        services = {f["service"] for f in payload["findings"]}
        self.assertEqual(
            services, {"svc_billing", "svc_auth", "svc_router", "svc_ledger"}
        )
        self.assertIn("priority_policy", payload)

    def test_diff_command_reports_changed_rows(self):
        proc = self._run("diff")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["summary"]["total"], 8)
        self.assertGreaterEqual(payload["summary"]["changed"], 1)

    def test_remediate_writes_fixed_snapshot_and_passes_fail_on_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixed_path = os.path.join(tmp, "fixed_snapshot.json")
            proc = self._run(
                "remediate",
                "--fixed-snapshot", fixed_path,
                "--fail-on-risk",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(fixed_path))
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["residual_allow_for_ordinary_accounts"])
            with open(fixed_path, encoding="utf-8") as fixed_handle:
                fixed = json.load(fixed_handle)
            # The explicit allow ACE on ledger.ini must have been removed.
            self.assertEqual(
                fixed["objects"]["/srv/fictional/ledger/ledger.ini"]["aces"], []
            )
            # Explicit deny entries now guard the billing config.
            billing_aces = fixed["objects"]["/srv/fictional/billing/billing.conf"]["aces"]
            self.assertTrue(
                any(a["ace_type"] == "deny" for a in billing_aces)
            )

    def test_missing_input_file_returns_exit_code_2(self):
        cmd = [
            sys.executable, "-I", APP, "analyze",
            "--inventory", os.path.join(EXAMPLE_DIR, "does_not_exist.json"),
            "--subjects", os.path.join(EXAMPLE_DIR, "subjects_v1.json"),
            "--snapshot", os.path.join(EXAMPLE_DIR, "snapshot.json"),
        ]
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_malformed_ace_returns_exit_code_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad_snapshot.json")
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump(
                    {"objects": {"/x": {"type": "file", "aces": [
                        {"ace_type": "maybe", "principal": "acc_bob", "rights": "all"}
                    ]}}},
                    handle,
                )
            cmd = [
                sys.executable, "-I", APP, "analyze",
                "--inventory", os.path.join(EXAMPLE_DIR, "inventory.json"),
                "--subjects", os.path.join(EXAMPLE_DIR, "subjects_v1.json"),
                "--snapshot", bad,
            ]
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
            )
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
