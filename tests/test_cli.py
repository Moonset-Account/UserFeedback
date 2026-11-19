"""Acceptance tests: the CLI is the real entry point and does real work."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from support import REPO_ROOT


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.scenario_path = os.path.join(self.tmpdir.name, "scenario.json")
        scenario = {
            "initial": {
                "objects": {"root": "dir", "proj": "dir"},
                "entries": [
                    {"dir": "root", "name": "proj", "id": "proj"},
                    {"dir": "proj", "name": "a.txt", "id": "f1"},
                ],
            },
            "events": [
                {"op": "link", "dir": "root", "name": "b.txt", "id": "f1"},
                {
                    "op": "rename",
                    "from": {"dir": "root", "name": "proj"},
                    "to": {"dir": "root", "name": "docs"},
                },
                {
                    "op": "exchange",
                    "a": {"dir": "root", "name": "b.txt"},
                    "b": {"dir": "root", "name": "docs"},
                },
            ],
        }
        with open(self.scenario_path, "w", encoding="utf-8") as handle:
            json.dump(scenario, handle)

    # ``-I`` isolates the interpreter (no cwd on sys.path, no PYTHONPATH),
    # so the package root is injected explicitly by this bootstrap.
    BOOTSTRAP = (
        "import sys; sys.path.insert(0, %r); "
        "from naming_history.cli import main; main()"
    )

    def run_cli(self, *args):
        command = [
            sys.executable,
            "-I",
            "-c",
            self.BOOTSTRAP % REPO_ROOT,
            self.scenario_path,
        ] + list(args)
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_query_returns_paths_at_the_requested_sequence(self):
        proc = self.run_cli("--seq", "1", "--id", "f1")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(["/b.txt", "/proj/a.txt"], proc.stdout.splitlines())

    def test_query_reflects_directory_rename(self):
        proc = self.run_cli("--seq", "2", "--id", "f1")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(["/b.txt", "/docs/a.txt"], proc.stdout.splitlines())

    def test_sequence_zero_is_initial_tree(self):
        proc = self.run_cli("--seq", "0", "--id", "f1")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(["/proj/a.txt"], proc.stdout.splitlines())

    def test_json_output_is_machine_readable(self):
        proc = self.run_cli("--seq", "2", "--id", "f1", "--json")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(["/b.txt", "/docs/a.txt"], json.loads(proc.stdout))

    def test_future_object_query_fails_loudly(self):
        proc = self.run_cli("--seq", "0", "--id", "does-not-exist")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("error:", proc.stderr)

    def test_out_of_range_sequence_fails_loudly(self):
        proc = self.run_cli("--seq", "99", "--id", "f1")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("outside the committed history", proc.stderr)

    def test_rejected_event_aborts_the_whole_scenario(self):
        broken = {
            "initial": {"entries": [{"dir": "root", "name": "a", "id": "d"}]},
            "events": [
                {"op": "link", "dir": "root", "name": "a", "id": "other"},
            ],
        }
        with open(self.scenario_path, "w", encoding="utf-8") as handle:
            json.dump(broken, handle)
        proc = self.run_cli("--seq", "1", "--id", "a")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("already exists", proc.stderr)
        self.assertEqual("", proc.stdout)


if __name__ == "__main__":
    unittest.main()
