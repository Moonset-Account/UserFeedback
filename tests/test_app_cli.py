"""Checks on the CLI entry point and on the wrapper's end-to-end gate paths.

The CLI tests assert observable output semantics: which case groups are named,
that all cases pass on the delivered comparator, and that no digest bytes or
digest hex appears in stdout.  The wrapper tests replace discovery/execution to
prove the non-zero exit paths without recursively re-running the suite.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import app
import digest_compare
import local_verify
from reference_impls import early_return_scan
from synthetic_cases import boundary_cases, build_cases


def _capture(main, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class AuditCommandTests(unittest.TestCase):
    def test_audit_passes_and_names_every_group(self) -> None:
        code, output = _capture(app.main, ["audit", "--length", "4"])
        self.assertEqual(code, 0)
        self.assertIn("group=per-position", output)
        self.assertIn("group=boundary", output)
        for position in range(4):
            self.assertIn(f"case_id=diff-{position:02d}", output)
        self.assertIn("case_id=diff-15", _capture(app.main, ["audit"])[1])
        self.assertIn("changed_case_ids=none", output)
        self.assertEqual(output.count("status=PASS"), 7)

    def test_audit_output_excludes_digest_material(self) -> None:
        _, output = _capture(app.main, ["audit", "--length", "4"])
        for case in build_cases(4) + boundary_cases():
            for side in (case.left, case.right):
                self.assertNotIn(repr(side), output)
                self.assertNotIn(side.hex(), output)

    def test_audit_fails_when_comparison_returns_early(self) -> None:
        original = digest_compare._scan
        digest_compare._scan = early_return_scan
        try:
            code, output = _capture(app.main, ["audit", "--length", "4"])
        finally:
            digest_compare._scan = original
        self.assertEqual(code, 1)
        self.assertIn("case_id=diff-00", output)
        self.assertIn("status=FAIL", output)
        self.assertIn("first_divergence=", output)
        # A difference on the last byte still scans every byte, so that group
        # legitimately keeps its full-scan trace even under early return.
        self.assertIn("changed_case_ids=diff-00,diff-01,diff-02", output)

    def test_length_check_command_reports_each_guard(self) -> None:
        code, output = _capture(app.main, ["length-check"])
        self.assertEqual(code, 0)
        for sample_id in ("left-too-short", "right-too-short", "left-empty", "both-too-long"):
            self.assertIn(f"sample_id={sample_id}", output)
        self.assertEqual(output.count("status=PASS"), 4)

    def test_invalid_audit_length_exits_non_zero(self) -> None:
        code, _output = _capture(app.main, ["audit", "--length", "0"])
        self.assertEqual(code, 2)


class WrapperGatePathTests(unittest.TestCase):
    def _run_with(self, suite_factory, samples, executed=None):
        original_discover = local_verify.discover_suite
        original_samples = local_verify.sample_checks
        original_run = local_verify.run_suite

        def stub_run(suite):
            count = suite.countTestCases() if executed is None else executed
            return True, count, []

        local_verify.discover_suite = suite_factory
        local_verify.sample_checks = lambda: samples
        local_verify.run_suite = stub_run
        try:
            return _capture(local_verify.main, ["--timeout", "20"])
        finally:
            local_verify.discover_suite = original_discover
            local_verify.sample_checks = original_samples
            local_verify.run_suite = original_run

    def test_empty_discovery_exits_non_zero(self) -> None:
        code, output = self._run_with(unittest.TestSuite, [])
        self.assertEqual(code, local_verify.EXIT_INSUFFICIENT)
        self.assertIn("reason=zero_tests_discovered", output)

    def test_zero_executed_exits_non_zero(self) -> None:
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        code, output = self._run_with(lambda: suite, [("s", "d", "a", True)], executed=0)
        self.assertEqual(code, local_verify.EXIT_INSUFFICIENT)
        self.assertIn("reason=no_actual_tests_executed", output)

    def test_failing_sample_exits_non_zero(self) -> None:
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        code, output = self._run_with(
            lambda: suite, [("good", "d", "a", True), ("bad", "d", "a", False)]
        )
        self.assertEqual(code, local_verify.EXIT_TEST_FAILURE)
        self.assertIn("reason=sample_assertion_failure", output)
        self.assertIn("sample_id=bad", output)


if __name__ == "__main__":
    unittest.main()
