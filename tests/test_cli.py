"""End-to-end tests that drive the CLI entry point as a subprocess.

These prove the documented exit-code contract and the JSON output shape that
deployment tooling consumes, rather than only the in-process API.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APP), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def write_inputs(
    tmp: Path, environment: dict, services: dict, catalog: dict
) -> tuple[Path, Path, Path]:
    env_path = tmp / "environment.json"
    svc_path = tmp / "services.json"
    cat_path = tmp / "catalog.json"
    env_path.write_text(json.dumps(environment), encoding="utf-8")
    svc_path.write_text(json.dumps(services), encoding="utf-8")
    cat_path.write_text(json.dumps(catalog), encoding="utf-8")
    return env_path, svc_path, cat_path


def cli_args(env_path: Path, svc_path: Path, cat_path: Path, *extra: str) -> list[str]:
    return [
        "--environment", str(env_path),
        "--services", str(svc_path),
        "--catalog", str(cat_path),
        *extra,
    ]


class CliTests(unittest.TestCase):
    def test_success_exit_zero_and_json_shape(self) -> None:
        """CLI-OK: a resolvable deployment exits 0 with a full JSON plan."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "alpha", "version": 2}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1, 2], "current": 1}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            paths = write_inputs(Path(raw), environment, services, catalog)
            result = run_cli(*cli_args(*paths))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["executable"])
        self.assertEqual(payload["environment"], "prod")
        svc = payload["services"][0]
        self.assertEqual(svc["variables"][0]["variable"], "X")
        self.assertEqual(svc["variables"][0]["version"], 2)
        self.assertEqual(svc["variables"][0]["source_layer"], "common")

    def test_blocked_exit_one_keeps_independent_service(self) -> None:
        """CLI-BLOCKED: one blocked service exits 1 while the other stays resolved."""

        environment = {"environment": "prod", "common": []}
        services = {
            "services": ["broken", "fine"],
            "service_overrides": {
                "broken": [{"variable": "X", "ref": "ghost"}],
                "fine": [{"variable": "Y", "ref": "real"}],
            },
        }
        catalog = {
            "environment": "prod",
            "services": ["broken", "fine"],
            "references": [
                {"service": "fine", "ref": "real", "versions": [1], "current": 1},
                {"service": "fine", "ref": "ghost", "versions": [1], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            paths = write_inputs(Path(raw), environment, services, catalog)
            result = run_cli(*cli_args(*paths))

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        states = {svc["service"]: svc["executable"] for svc in payload["services"]}
        self.assertEqual(states, {"broken": False, "fine": True})
        fine = next(svc for svc in payload["services"] if svc["service"] == "fine")
        self.assertEqual(len(fine["variables"]), 1)
        self.assertEqual(fine["variables"][0]["resolved_ref"], "real")
        self.assertFalse(payload["executable"])

    def test_invalid_input_exit_two(self) -> None:
        """CLI-INPUT-ERR: malformed inputs exit 2 with an error payload."""

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env_path = tmp / "environment.json"
            env_path.write_text("{not json", encoding="utf-8")
            svc_path = tmp / "services.json"
            svc_path.write_text('{"services":["svc"]}', encoding="utf-8")
            cat_path = tmp / "catalog.json"
            cat_path.write_text('{"environment":"prod"}', encoding="utf-8")
            result = run_cli(*cli_args(env_path, svc_path, cat_path))

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"], "invalid_input")
        self.assertIn("not valid JSON", payload["message"])

    def test_text_format_reports_chain(self) -> None:
        """CLI-TEXT: text output shows the alias chain for a cycle."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "one"}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "one", "alias_of": {"ref": "two"}},
                {"service": "svc", "ref": "two", "alias_of": {"ref": "one"}},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            paths = write_inputs(Path(raw), environment, services, catalog)
            result = run_cli(*cli_args(*paths, "--format", "text"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT EXECUTABLE", result.stdout)
        self.assertIn("chain", result.stdout)
        self.assertIn("prod/svc/one -> prod/svc/two -> prod/svc/one", result.stdout)

    def test_sample_directory_runs_clean(self) -> None:
        """CLI-SAMPLE: the shipped prod sample resolves end to end."""

        samples = ROOT / "samples" / "prod"
        result = run_cli(
            "--environment", str(samples / "environment.json"),
            "--services", str(samples / "services.json"),
            "--catalog", str(samples / "catalog.json"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [svc["service"] for svc in payload["services"]], ["api", "worker", "billing"]
        )
        self.assertTrue(all(svc["executable"] for svc in payload["services"]))


if __name__ == "__main__":
    unittest.main()
