"""End-to-end tests for the app.py CLI surface."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
FIXTURES = os.path.join(ROOT, "fixtures")


def run_cli(*args, stdin_text=None):
    return subprocess.run(
        [sys.executable, APP, *args],
        cwd=ROOT,
        input=stdin_text if stdin_text is not None else "",
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


class CliTests(unittest.TestCase):
    def test_decide_allow_exit_zero(self) -> None:
        result = run_cli(
            "decide",
            "--config", os.path.join(FIXTURES, "config.json"),
            "--request", os.path.join(FIXTURES, "request_allow.json"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(payload["blocked_layers"], [])
        self.assertEqual(payload["layers"][0]["used"], 1)

    def test_batch_deny_exit_two(self) -> None:
        config = {"account": {"limit": 1, "window_seconds": 10}, "source": {"limit": 9, "window_seconds": 10}, "dedup_ttl_seconds": 10}
        requests = [
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r1", "now": 0.0},
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r2", "now": 1.0},
        ]
        result = self._run_with(config, requests, "batch")
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][1]["decision"], "deny")
        self.assertEqual(payload["results"][1]["retry_at"], 10.0)

    def test_batch_end_to_end(self) -> None:
        result = run_cli(
            "batch",
            "--config", os.path.join(FIXTURES, "config.json"),
            "--requests", os.path.join(FIXTURES, "batch.json"),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        decisions = [entry.get("decision") for entry in payload["results"]]
        # r1@0 and r2@1 admitted; r3@2 denied on the account layer; r3@3 is a
        # deduplicated replay of that denial; r4@10 admitted (r1's t=0 slot has
        # left the 10s account window, source=3/3 now); r1@10 is a fresh
        # decision because the TTL expired at exactly t=10, and both layers are
        # full, so it is denied.
        self.assertEqual(decisions, ["allow", "allow", "deny", "deny", "allow", "deny"])
        self.assertTrue(payload["results"][3]["deduplicated"])
        self.assertEqual(payload["results"][3]["decided_at"], 2.0)
        self.assertEqual(payload["results"][2]["blocked_layers"], ["account"])
        self.assertEqual(payload["results"][2]["retry_at"], 10.0)
        self.assertEqual(payload["results"][5]["blocked_layers"], ["account", "source"])
        self.assertEqual(payload["results"][5]["retry_at"], 20.0)
        self.assertEqual(payload["errors"], 0)
        self.assertEqual(payload["allowed"], 3)

    def test_batch_conflict_exit_four(self) -> None:
        config = {"account": {"limit": 5, "window_seconds": 10}, "source": {"limit": 5, "window_seconds": 10}, "dedup_ttl_seconds": 10}
        requests = [
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "rid", "now": 0.0},
            {"tenant": "t", "account": "b", "source": "192.0.2.1", "request_id": "rid", "now": 1.0},
        ]
        result = self._run_with(config, requests, "batch")
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][1]["error"], "request_id_conflict")
        self.assertEqual(payload["errors"], 1)

    def test_batch_clock_regression_exit_five(self) -> None:
        config = {"account": {"limit": 5, "window_seconds": 10}, "source": {"limit": 5, "window_seconds": 10}, "dedup_ttl_seconds": 10}
        requests = [
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r1", "now": 10.0},
            {"tenant": "t", "account": "a", "source": "192.0.2.1", "request_id": "r2", "now": 9.0},
        ]
        result = self._run_with(config, requests, "batch")
        self.assertEqual(result.returncode, 5, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][1]["error"], "clock_regression")

    def test_missing_request_file_reports_io_error(self) -> None:
        result = run_cli(
            "decide",
            "--config", os.path.join(FIXTURES, "config.json"),
            "--request", os.path.join(FIXTURES, "does_not_exist.json"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"], "io_error")

    def test_invalid_json_reports_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            result = run_cli(
                "decide",
                "--config", os.path.join(FIXTURES, "config.json"),
                "--request", bad,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"], "invalid_input")

    def test_schema_violation_reports_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump({"tenant": "t", "account": "a", "source": "192.0.2.1", "now": 0}, handle)
            result = run_cli(
                "decide",
                "--config", os.path.join(FIXTURES, "config.json"),
                "--request", bad,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"], "invalid_input")
        self.assertIn("request_id", payload["message"])

    def test_config_validation_error_exit_one(self) -> None:
        result = self._run_with({"account": {"limit": 0, "window_seconds": 1}, "source": {"limit": 1, "window_seconds": 1}, "dedup_ttl_seconds": 1}, [], "batch")
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_stdin_dash_input(self) -> None:
        with open(os.path.join(FIXTURES, "config.json"), encoding="utf-8") as handle:
            cfg = handle.read()
        result = run_cli("batch", "--config", "-", "--requests", os.path.join(FIXTURES, "batch.json"), stdin_text=cfg)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["allowed"], 3)

    def test_decide_reads_request_from_stdin(self) -> None:
        with open(os.path.join(FIXTURES, "request_allow.json"), encoding="utf-8") as handle:
            req = handle.read()
        result = run_cli(
            "decide",
            "--config", os.path.join(FIXTURES, "config.json"),
            "--request", "-",
            stdin_text=req,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "allow")

    def test_double_stdin_rejected(self) -> None:
        result = run_cli("decide", "--config", "-", "--request", "-", stdin_text="{}")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"], "invalid_input")
        self.assertIn("stdin", payload["message"])

    def test_unknown_command_exit_two_argparse(self) -> None:
        result = run_cli("nonsense")
        self.assertEqual(result.returncode, 2)

    def test_demo_passes(self) -> None:
        result = run_cli("demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["failed_scenarios"], [])
        expected = {
            "demo-window-boundary",
            "demo-retry-advice",
            "demo-two-layer-gate",
            "demo-dedup-replay",
            "demo-last-unit-race",
            "demo-tenant-isolation",
        }
        self.assertEqual(set(payload["scenarios"]), expected)
        for case_id, body in payload["scenarios"].items():
            self.assertEqual(body["case_id"], case_id)
            self.assertTrue(body["passed"], case_id)
            self.assertGreaterEqual(len(body["checks"]), 3, case_id)

    # -- helpers ------------------------------------------------------------

    def _run_with(self, config, requests, command):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "cfg.json")
            req_path = os.path.join(tmp, "reqs.json")
            with open(cfg_path, "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            with open(req_path, "w", encoding="utf-8") as handle:
                json.dump(requests, handle)
            return run_cli(command, "--config", cfg_path, "--requests", req_path)


if __name__ == "__main__":
    unittest.main()
