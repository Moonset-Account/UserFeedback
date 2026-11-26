import unittest

from static_check import audit_flow


class WrappingDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "inner_fault",
                    "type": "raise",
                    "code": "E_INNER",
                    "diagnostics": [
                        {"id": "diag.inner.public", "source": "inner-source"},
                        {"id": "diag.inner.secret", "source": "inner-source"},
                    ],
                },
                {
                    "id": "wrap",
                    "type": "wrap",
                    "code": "E_JOB_PUBLIC",
                    "diagnostics": ["diag.wrap.public"],
                },
                {"id": "error_exit", "type": "response", "kind": "error"},
            ],
            "edges": [
                ["start", "inner_fault"],
                {"from": "inner_fault", "to": "wrap", "branch": "error"},
                {"from": "wrap", "to": "error_exit", "branch": "error"},
            ],
        }
        self.public_diagnostics = {
            "diag.inner.public": ["agent-a"],
            "diag.wrap.public": True,
        }
        self.public_codes = {
            "E_JOB_PUBLIC": True,
            "E_INNER": ["agent-a"],
        }

    def test_outer_public_code_does_not_authorize_inner_context(self):
        # agent-b may send the outer code, but not E_INNER or the secret diag.
        report = audit_flow(
            self.graph,
            self.public_diagnostics,
            "agent-b",
            public_codes=self.public_codes,
        )

        self.assertFalse(report.ok)
        codes = report.codes()
        self.assertIn("UNDECLARED_DIAGNOSTIC", codes)
        self.assertIn("DIAGNOSTIC_NOT_VISIBLE_TO_CALLER", codes)
        self.assertIn("ERROR_CODE_NOT_VISIBLE_TO_CALLER", codes)

        exit_report = report.exit("error_exit")
        self.assertIn("diag.wrap.public", exit_report.allowed_diagnostics)
        self.assertNotIn("diag.inner.public", exit_report.allowed_diagnostics)
        self.assertIn("diag.inner.public", exit_report.blocked_diagnostics)
        self.assertIn("diag.inner.secret", exit_report.blocked_diagnostics)

        safe = exit_report.safe_response
        self.assertIsNotNone(safe)
        self.assertEqual(safe["code"], "E_JOB_PUBLIC")
        self.assertIn("diag.wrap.public", safe["diagnostics"])
        self.assertNotIn("diag.inner.public", safe["diagnostics"])
        self.assertNotIn("diag.inner.secret", safe["diagnostics"])

    def test_identity_can_be_authorized_for_inner_code_and_diag(self):
        report = audit_flow(
            self.graph,
            self.public_diagnostics,
            "agent-a",
            public_codes=self.public_codes,
        )

        # The only remaining problem is the genuinely undeclared secret.
        self.assertFalse(report.ok)
        self.assertEqual(
            {f.code for f in report.violations},
            {"UNDECLARED_DIAGNOSTIC"},
        )
        safe = report.exit("error_exit").safe_response
        self.assertEqual(safe["cause"]["code"], "E_INNER")
        self.assertIn("diag.inner.public", safe["cause"]["diagnostics"])
        self.assertNotIn("diag.inner.secret", safe["cause"]["diagnostics"])

    def test_all_declared_context_passes(self):
        self.public_diagnostics["diag.inner.secret"] = ["agent-a"]
        report = audit_flow(
            self.graph,
            self.public_diagnostics,
            "agent-a",
            public_codes=self.public_codes,
        )
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        self.assertEqual(report.exit("error_exit").blocked_diagnostics, ())


if __name__ == "__main__":
    unittest.main()
