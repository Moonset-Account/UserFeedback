"""Checks on the local verification gate itself.

These tests prove the wrapper cannot report success on thin evidence: zero
discovered cases, all-skipped runs, failures and sample mismatches all map to a
non-zero exit, and an early-return comparison makes the wrapper's own sample
report name the affected case groups.
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import digest_compare
import local_verify
from reference_impls import early_return_compare_with_trace as early_return_comparator
from reference_impls import early_return_scan


class GateDecisionTests(unittest.TestCase):
    def test_zero_discovered_tests_is_not_success(self) -> None:
        code, reason = local_verify.decide_outcome(True, 0, 0, 5, 5)
        self.assertNotEqual(code, local_verify.EXIT_OK)
        self.assertEqual(reason, "zero_tests_discovered")

    def test_all_skipped_is_not_success(self) -> None:
        code, reason = local_verify.decide_outcome(True, 4, 0, 5, 5)
        self.assertNotEqual(code, local_verify.EXIT_OK)
        self.assertEqual(reason, "no_actual_tests_executed")

    def test_failures_map_to_non_zero(self) -> None:
        code, reason = local_verify.decide_outcome(False, 4, 4, 5, 5)
        self.assertNotEqual(code, local_verify.EXIT_OK)
        self.assertEqual(reason, "unittest_failures_or_errors")

    def test_sample_failure_maps_to_non_zero(self) -> None:
        code, reason = local_verify.decide_outcome(True, 4, 4, 5, 4)
        self.assertNotEqual(code, local_verify.EXIT_OK)
        self.assertEqual(reason, "sample_assertion_failure")

    def test_full_evidence_passes(self) -> None:
        code, reason = local_verify.decide_outcome(True, 4, 4, 5, 5)
        self.assertEqual((code, reason), (local_verify.EXIT_OK, "PASS"))

    def test_empty_sample_set_is_not_success(self) -> None:
        code, reason = local_verify.decide_outcome(True, 4, 4, 0, 0)
        self.assertNotEqual(code, local_verify.EXIT_OK)
        self.assertEqual(reason, "sample_assertion_failure")


class GateDetectsEarlyReturnTests(unittest.TestCase):
    def test_sample_report_flags_trace_groups_when_comparison_shortens(self) -> None:
        original = digest_compare.compare_digests_with_trace
        digest_compare.compare_digests_with_trace = early_return_comparator
        try:
            checks = local_verify.sample_checks()
        finally:
            digest_compare.compare_digests_with_trace = original

        failed_ids = [sample_id for sample_id, _d, _a, ok in checks if not ok]
        self.assertTrue(failed_ids, "early-return comparison must fail a sample")
        self.assertTrue(any(sid.endswith("/diff-00/trace") for sid in failed_ids))
        self.assertFalse(any(sid.endswith("/diff-15/trace") for sid in failed_ids))

        passed = sum(1 for _sid, _d, _a, ok in checks if ok)
        code, reason = local_verify.decide_outcome(
            True, 17, 17, len(checks), passed
        )
        self.assertNotEqual(code, local_verify.EXIT_OK)
        self.assertEqual(reason, "sample_assertion_failure")

    def test_boolean_samples_alone_do_not_hide_the_regression(self) -> None:
        # The early-return comparator still returns correct booleans, which is
        # exactly why the trace samples must exist in the gate.
        cases = digest_compare.compare_digests_with_trace
        try:
            digest_compare.compare_digests_with_trace = early_return_comparator
            boolean_checks = [
                item for item in local_verify.sample_checks()
                if item[0].endswith("/boolean")
            ]
        finally:
            digest_compare.compare_digests_with_trace = cases
        self.assertTrue(all(ok for _s, _d, _a, ok in boolean_checks))

    def test_patched_scan_core_alone_fails_the_gate(self) -> None:
        # Patch the shared scan used by *both* comparison entry points, as a
        # real source change would; boolean samples stay correct but the trace
        # samples and the runner's own suite both fail.
        original = digest_compare._scan
        digest_compare._scan = early_return_scan
        try:
            checks = local_verify.sample_checks()
        finally:
            digest_compare._scan = original
        boolean_results = [ok for sid, _d, _a, ok in checks if sid.endswith("/boolean")]
        trace_results = [ok for sid, _d, _a, ok in checks if sid.endswith("/trace")]
        self.assertTrue(all(boolean_results))
        self.assertFalse(all(trace_results))

        passed = sum(1 for _sid, _d, _a, ok in checks if ok)
        code, reason = local_verify.decide_outcome(
            True, 34, 34, len(checks), passed
        )
        self.assertEqual(code, local_verify.EXIT_TEST_FAILURE)
        self.assertEqual(reason, "sample_assertion_failure")


class GateDiscoveryTests(unittest.TestCase):
    def test_discover_uses_literal_tests_dir_from_project_root(self) -> None:
        original_cwd = os.getcwd()
        try:
            os.chdir(str(ROOT / "tests"))  # arbitrary caller cwd
            suite = local_verify.discover_suite()
            self.assertGreaterEqual(suite.countTestCases(), 1)
            self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(str(ROOT)))
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
