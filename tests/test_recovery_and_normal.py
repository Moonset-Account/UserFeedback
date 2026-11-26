import unittest

from static_check import audit_flow


class RecoveryReplacementTests(unittest.TestCase):
    def test_replacement_drops_original_fault_by_default(self):
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "original",
                    "type": "raise",
                    "code": "E_OLD",
                    "diagnostics": ["diag.old.secret"],
                },
                {
                    "id": "replace",
                    "type": "replace",
                    "code": "E_FALLBACK",
                    "diagnostics": ["diag.fallback"],
                },
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [
                ["start", "original"],
                ["original", "replace", "error"],
                ["replace", "exit", "error"],
            ],
        }
        report = audit_flow(
            graph,
            {"diag.fallback"},
            "job-agent",
            public_codes={"E_FALLBACK"},
        )

        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        safe = report.exit("exit").safe_response
        self.assertEqual(safe["code"], "E_FALLBACK")
        self.assertEqual(safe["diagnostics"], ["diag.fallback"])
        self.assertNotIn("E_OLD", str(safe))
        self.assertNotIn("diag.old.secret", str(safe))

    def test_replacement_can_keep_original_as_checked_cause(self):
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "original",
                    "type": "raise",
                    "code": "E_OLD",
                    "diagnostics": ["diag.old.secret"],
                },
                {
                    "id": "replace",
                    "type": "replace",
                    "code": "E_FALLBACK",
                    "keep_cause": True,
                },
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [
                ["start", "original"],
                ["original", "replace", "error"],
                ["replace", "exit", "error"],
            ],
        }
        report = audit_flow(
            graph,
            set(),
            "job-agent",
            public_codes={"E_FALLBACK"},
        )
        self.assertFalse(report.ok)
        self.assertIn("UNDECLARED_DIAGNOSTIC", report.codes())
        self.assertIn("UNDECLARED_ERROR_CODE", report.codes())
        self.assertEqual(
            report.exit("exit").scenarios[0].error.cause.code,
            "E_OLD",
        )


class RecoveryNormalResponseTests(unittest.TestCase):
    def _graph(self, *, propagate=None, response_fields=None):
        recovery = {
            "id": "recover",
            "type": "recover",
        }
        if propagate is not None:
            recovery["propagate_diagnostics"] = propagate
        return {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "fault",
                    "type": "raise",
                    "code": "E_FAIL",
                    "diagnostics": ["diag.old.public", "diag.old.secret"],
                },
                recovery,
                {
                    "id": "ok_exit",
                    "type": "response",
                    "kind": "normal",
                    "fields": response_fields
                    or {"diagnostics": "@diagnostics", "status": {"literal": "recovered"}},
                },
            ],
            "edges": [
                ["start", "fault"],
                ["fault", "recover", "error"],
                ["recover", "ok_exit", "normal"],
            ],
        }

    def test_normal_recovery_retains_nothing_by_default(self):
        graph = self._graph()
        report = audit_flow(graph, {"diag.old.public"}, "agent")
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        safe = report.exit("ok_exit").safe_response
        self.assertEqual(safe["diagnostics"], [])
        self.assertEqual(safe["status"], "recovered")

    def test_normal_recovery_reference_to_dropped_diagnostic_is_flagged(self):
        graph = self._graph(
            response_fields={"trace": {"source": "diag.old.public"}}
        )
        report = audit_flow(graph, {"diag.old.public"}, "agent")
        self.assertFalse(report.ok)
        self.assertIn("DIAGNOSTIC_NOT_PROPAGATED", report.codes())
        finding = next(
            f for f in report.violations if f.code == "DIAGNOSTIC_NOT_PROPAGATED"
        )
        self.assertEqual(finding.diagnostic_id, "diag.old.public")
        self.assertEqual(finding.field_name, "trace")
        self.assertNotIn("trace", report.exit("ok_exit").safe_response)

    def test_explicitly_propagated_normal_diagnostic_is_checked_and_retained(self):
        graph = self._graph(propagate=["diag.old.public"])
        report = audit_flow(graph, {"diag.old.public"}, "agent")
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        self.assertEqual(
            report.exit("ok_exit").safe_response["diagnostics"],
            ["diag.old.public"],
        )

    def test_propagated_blocked_diagnostic_fails_normal_exit(self):
        graph = self._graph(propagate="all")
        report = audit_flow(graph, {"diag.old.public"}, "agent")
        self.assertFalse(report.ok)
        self.assertIn("UNDECLARED_DIAGNOSTIC", report.codes())
        exit_report = report.exit("ok_exit")
        self.assertEqual(exit_report.safe_response["diagnostics"], ["diag.old.public"])
        self.assertNotIn("diag.old.secret", exit_report.safe_response["diagnostics"])


if __name__ == "__main__":
    unittest.main()
