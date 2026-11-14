"""CLI-level tests: run app.py as a subprocess and check JSON + exit codes.

Covers: CASE-INTAKE-001 (archive + shared dir + size limits at the intake
entry), CASE-PLAN-001 (plan command), CASE-REPORT-001 (JSON error report).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import helpers  # noqa: E402

APP = os.path.join(_ROOT, "app.py")


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-I", APP] + list(args),
        capture_output=True, text=True, timeout=30)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zipimport-cli-")
        self.inbox = os.path.join(self.tmp, "inbox")
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(self.inbox)
        os.makedirs(self.shared)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_zip(self, name, entries, **kw):
        return helpers.write_zip(os.path.join(self.inbox, name), entries, **kw)

    def test_usage_error_exit_2(self):
        proc = run_cli()
        self.assertEqual(proc.returncode, 2)

    def test_plan_command_lists_targets(self):
        zpath = self.make_zip("p.zip", [
            ("docs/a.txt", b"aa", helpers.MODE_FILE),
            ("b.txt", b"bb", helpers.MODE_FILE),
        ])
        proc = run_cli("plan", zpath)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual([e["target"] for e in report["entries"]],
                         ["docs/a.txt", "b.txt"])

    def test_import_command_success(self):
        """CASE-INTAKE-001: intake receives archive, allowed dir and limits."""
        zpath = self.make_zip("ok.zip", [("a.txt", b"hello", helpers.MODE_FILE)])
        proc = run_cli("import", zpath, self.shared,
                       "--max-entry-bytes", "1K",
                       "--max-total-bytes", "1M",
                       "--batch-name", "cli-batch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["batch_name"], "cli-batch")
        self.assertEqual(report["limits"]["max_entry_bytes"], 1024)
        self.assertEqual(report["limits"]["max_total_bytes"], 1024 * 1024)
        self.assertEqual(
            helpers.read_file(os.path.join(self.shared, "cli-batch"), "a.txt"),
            b"hello")

    def test_import_command_failure_json_and_exit_1(self):
        zpath = self.make_zip("bad.zip", [
            ("fine.txt", b"x", helpers.MODE_FILE),
            ("../evil.txt", b"y", helpers.MODE_FILE),
        ])
        proc = run_cli("import", zpath, self.shared)
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error"]["code"], "path_escape")
        self.assertEqual(report["error"]["entry"], "../evil.txt")
        self.assertEqual(report["error"]["entry_index"], 1)
        self.assertEqual(os.listdir(self.shared), [])

    def test_import_command_limit_violation_exit_1(self):
        zpath = self.make_zip("big.zip", [("b.bin", b"b" * 2048,
                                           helpers.MODE_FILE)])
        proc = run_cli("import", zpath, self.shared, "--max-entry-bytes", "1K")
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertEqual(report["error"]["code"], "entry_too_large")
        self.assertEqual(os.listdir(self.shared), [])


if __name__ == "__main__":
    unittest.main()
