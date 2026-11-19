"""End-to-end tests for the ``python -m release_check`` entry point.

The CLI is driven as a subprocess so the real exit codes and the serialised
output are exercised, not just the in-process functions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run_cli(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "release_check", *arguments],
        cwd=PROJECT_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


class CliTests(unittest.TestCase):
    def write_spec(self, document: dict) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        with handle:
            json.dump(document, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_clean_specification_exits_zero(self) -> None:
        path = self.write_spec(
            {
                "callers": ["billing"],
                "interfaces": [
                    {"name": "IA", "methods": {"generate": {"billing": ["id"]}}}
                ],
                "implementations": [
                    {
                        "name": "Ok",
                        "implements": ["IA"],
                        "methods": {"generate": {"accepts": ["billing"], "returns": ["id"]}},
                    }
                ],
            }
        )

        result = run_cli(path)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=ready", result.stdout)

    def test_violation_exits_one_and_names_entry_and_identity(self) -> None:
        result = run_cli(str(FIXTURES / "diamond.json"))

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("IDiamondProfile.generate", result.stdout)
        self.assertIn("identity:       billing", result.stdout)
        self.assertIn("constrained by: IAuditedProfile, IReadOnlyProfile", result.stdout)

    def test_json_format_is_parseable_and_carries_the_summary(self) -> None:
        result = run_cli(str(FIXTURES / "diamond.json"), "--format", "json")

        document = json.loads(result.stdout)
        self.assertEqual(document["status"], "rejected")
        self.assertGreater(document["summary"]["errors"], 0)
        self.assertTrue(all("entry" in item for item in document["findings"]))

    def test_pending_specification_exits_two(self) -> None:
        path = self.write_spec(
            {
                "callers": ["billing"],
                "interfaces": [
                    {"name": "IReader", "public": True, "exposes": ["generate"]},
                ],
                "implementations": [],
            }
        )

        result = run_cli(path)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("status=pending", result.stdout)

    def test_invalid_specification_exits_two_with_a_reason(self) -> None:
        path = self.write_spec({"callers": ["billing"]})

        result = run_cli(path)

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid specification", result.stderr)
        self.assertIn("interfaces: required", result.stderr)

    def test_unreadable_path_exits_two(self) -> None:
        result = run_cli(str(FIXTURES / "does-not-exist.json"))

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read specification", result.stderr)

    def test_stdin_input_is_supported(self) -> None:
        payload = json.dumps(
            {
                "callers": ["billing"],
                "interfaces": [
                    {"name": "IA", "methods": {"generate": {"billing": ["id"]}}}
                ],
                "implementations": [],
            }
        )

        result = run_cli("-", stdin=payload)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=ready", result.stdout)

    def test_substitution_mode_narrows_to_one_entry(self) -> None:
        result = run_cli(
            str(FIXTURES / "diamond.json"),
            "--substitute",
            "IDiamondProfile",
            "SafeProfileGenerator",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("substitution check IDiamondProfile <- SafeProfileGenerator", result.stdout)
        self.assertIn("status=ready", result.stdout)

    def test_substitution_mode_reports_an_unknown_implementation(self) -> None:
        result = run_cli(
            str(FIXTURES / "diamond.json"),
            "--substitute",
            "IDiamondProfile",
            "NoSuchImplementation",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown implementation", result.stderr)

    def test_unknown_interface_in_substitution_mode_exits_two(self) -> None:
        result = run_cli(
            str(FIXTURES / "diamond.json"),
            "--substitute",
            "INotThere",
            "SafeProfileGenerator",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown interface", result.stderr)

    def test_output_is_deterministic_across_runs(self) -> None:
        first = run_cli(str(FIXTURES / "diamond.json"), "--format", "json")
        second = run_cli(str(FIXTURES / "diamond.json"), "--format", "json")

        self.assertEqual(first.stdout, second.stdout)


if __name__ == "__main__":
    unittest.main()
