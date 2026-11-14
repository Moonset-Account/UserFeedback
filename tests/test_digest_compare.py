"""Regression checks for the fixed-length digest comparison teaching example.

Run via ``tools/local_verify.py`` (which discovers this suite) or directly:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import binascii
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import digest_compare
import trace_audit
from reference_impls import early_return_compare_with_trace as early_return_comparator
from synthetic_cases import DEFAULT_LENGTH, boundary_cases, build_cases


class ResultTests(unittest.TestCase):
    def test_identical_digest_reports_equal(self) -> None:
        case = build_cases(DEFAULT_LENGTH)[0]
        self.assertTrue(digest_compare.compare_digests(case.left, case.right, case.length))

    def test_each_single_position_difference_reports_unequal(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)[1:]
        self.assertEqual(len(cases), DEFAULT_LENGTH)
        for case in cases:
            with self.subTest(case_id=case.case_id):
                self.assertFalse(
                    digest_compare.compare_digests(case.left, case.right, case.length)
                )

    def test_boundary_single_byte_cases(self) -> None:
        for case in boundary_cases():
            with self.subTest(case_id=case.case_id):
                self.assertEqual(
                    digest_compare.compare_digests(case.left, case.right, case.length),
                    case.expected_equal,
                )


class LengthValidationTests(unittest.TestCase):
    def _assert_length_error(self, left: bytes, right: bytes, expected: int) -> None:
        with self.assertRaises(digest_compare.DigestLengthError) as captured:
            digest_compare.compare_digests(left, right, expected)
        self.assertEqual(captured.exception.expected_length, expected)

    def test_short_left_is_rejected(self) -> None:
        base = bytes(DEFAULT_LENGTH)
        self._assert_length_error(base[:-1], base, DEFAULT_LENGTH)

    def test_short_right_is_rejected(self) -> None:
        base = bytes(DEFAULT_LENGTH)
        self._assert_length_error(base, base[:-1], DEFAULT_LENGTH)

    def test_both_wrong_lengths_are_reported_together(self) -> None:
        with self.assertRaises(digest_compare.DigestLengthError) as captured:
            digest_compare.compare_digests(b"", b"\x00", DEFAULT_LENGTH)
        self.assertEqual(
            dict(captured.exception.actual_lengths), {"left": 0, "right": 1}
        )

    def test_length_error_is_not_a_false_comparison_result(self) -> None:
        base = bytes(DEFAULT_LENGTH)
        with self.assertRaises(digest_compare.DigestLengthError):
            digest_compare.compare_digests_with_trace(base[:-1], base, DEFAULT_LENGTH)

    def test_invalid_expected_length_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            digest_compare.compare_digests(b"", b"", 0)

    def test_length_is_checked_before_any_byte_is_read(self) -> None:
        class ExplodingBytes(bytes):
            def __getitem__(self, index):
                raise AssertionError(f"byte read at {index} before length check")

        bad = ExplodingBytes(bytes(DEFAULT_LENGTH - 1))
        good = ExplodingBytes(bytes(DEFAULT_LENGTH))
        with self.assertRaises(digest_compare.DigestLengthError):
            digest_compare.compare_digests(bad, good, DEFAULT_LENGTH)


class TraceTests(unittest.TestCase):
    def test_trace_records_every_read_position(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        expected_positions = tuple(range(DEFAULT_LENGTH))
        for case in cases:
            with self.subTest(case_id=case.case_id):
                _, trace = digest_compare.compare_digests_with_trace(
                    case.left, case.right, case.length
                )
                self.assertEqual(trace.read_positions("left"), expected_positions)
                self.assertEqual(trace.read_positions("right"), expected_positions)
                self.assertEqual(trace.step_count, 4 * DEFAULT_LENGTH)

    def test_trace_shape_is_identical_across_difference_positions(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        signatures = set()
        for case in cases:
            _, trace = digest_compare.compare_digests_with_trace(
                case.left, case.right, case.length
            )
            signatures.add(trace.signature)
        self.assertEqual(len(signatures), 1)

    def test_every_case_matches_written_full_scan_spec(self) -> None:
        records = trace_audit.audit_group(build_cases(DEFAULT_LENGTH))
        self.assertEqual(len(records), DEFAULT_LENGTH + 1)
        for record in records:
            with self.subTest(case_id=record.case_id):
                self.assertTrue(record.result_matches)
                self.assertTrue(record.trace_matches, record.difference)
                self.assertIn("status=PASS", record.report_line())

    def test_trace_difference_description_has_no_digest_bytes(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        record = trace_audit.audit_case(cases[3])
        self.assertEqual(record.difference, "none")

        # A real (non-"none") difference description must also stay clean.
        mutated_records = trace_audit.audit_group(cases, comparator=early_return_comparator)
        failed = [r for r in mutated_records if not r.trace_matches]
        self.assertTrue(failed)
        rendered = "\n".join(r.report_line() for r in mutated_records)
        for case in cases:
            for side in (case.left, case.right):
                self.assertNotIn(binascii.hexlify(side).decode(), rendered)
                self.assertNotIn(repr(side), rendered)


class EarlyReturnRegressionTests(unittest.TestCase):
    def test_audit_flags_exactly_the_groups_whose_trace_shortens(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        records = trace_audit.audit_group(cases, comparator=early_return_comparator)
        changed = trace_audit.changed_case_ids(records)
        expected_changed = [
            f"diff-{position:02d}" for position in range(DEFAULT_LENGTH - 1)
        ]
        self.assertEqual(changed, expected_changed)
        self.assertNotIn("same", changed)

    def test_results_still_correct_under_early_return(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        records = trace_audit.audit_group(cases, comparator=early_return_comparator)
        for record in records:
            with self.subTest(case_id=record.case_id):
                self.assertTrue(record.result_matches)

    def test_difference_names_the_first_divergence_step(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        records = trace_audit.audit_group(cases, comparator=early_return_comparator)
        by_id = {record.case_id: record for record in records}
        self.assertIn("first_divergence=", by_id["diff-00"].difference)
        self.assertIn("step_count 64->4", by_id["diff-00"].difference)
        # A difference at the last byte still scans every byte, so its trace
        # is indistinguishable from the full scan; earlier positions are not.
        self.assertEqual(by_id["diff-15"].difference, "none")
        self.assertIn("first_divergence=", by_id["diff-14"].difference)


class ReportSafetyTests(unittest.TestCase):
    def test_report_lines_do_not_contain_digest_material(self) -> None:
        cases = build_cases(DEFAULT_LENGTH)
        records = trace_audit.audit_group(cases)
        rendered = "\n".join(record.report_line() for record in records)
        rendered += "\n" + trace_audit.summarize(records)
        for case in cases:
            for side in (case.left, case.right):
                self.assertNotIn(binascii.hexlify(side).decode(), rendered)
                self.assertNotIn(repr(side), rendered)
        self.assertIn("length=16", rendered)
        self.assertIn("status=PASS", rendered)

    def test_summary_names_changed_groups_under_early_return(self) -> None:
        records = trace_audit.audit_group(
            build_cases(DEFAULT_LENGTH), comparator=early_return_comparator
        )
        summary = trace_audit.summarize(records)
        self.assertIn("changed_case_ids=", summary)
        self.assertIn("diff-00", summary)
        self.assertNotIn("changed_case_ids=none", summary)


if __name__ == "__main__":
    unittest.main()
