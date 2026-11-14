"""Failure-diagnostics property: a property violation must produce a
re-runnable repro artifact plus source sample / transform / seed details.
"""

import os
import unittest

import helpers


class FailureDiagnosticsTests(unittest.TestCase):
    def test_violation_writes_repro_and_reports_context(self):
        case_id = "diagnostics/selfcheck"
        text = "name,email\nX,x@y.z\n"
        expected = {"contacts": [], "errors": []}
        actual = {"contacts": [{"name": "X"}], "errors": []}
        with self.assertRaises(AssertionError) as ctx:
            helpers.assert_signature_equal(
                case_id, "valid_basic", "demo-transform", text, expected, actual
            )
        message = str(ctx.exception)
        for token in (
            case_id,
            "valid_basic",
            "demo-transform",
            "random seed:",
            "%d" % helpers.SEED,
            "rerun locally:",
            "app.py",
        ):
            self.assertIn(token, message, "diagnostics missing %r" % token)
        repro_path = os.path.join(helpers.REPRO_DIR, "diagnostics_selfcheck.csv")
        self.assertTrue(os.path.exists(repro_path), "repro input not written")
        with open(repro_path, "r", encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), text)

    def test_matching_signatures_pass_silently(self):
        sig = {"contacts": [], "errors": []}
        helpers.assert_signature_equal(
            "diagnostics/noop", "valid_basic", "none", "", sig, dict(sig)
        )


if __name__ == "__main__":
    unittest.main()
