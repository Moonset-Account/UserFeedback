import unittest

from static_check import audit_flow, reverse_identity_filter, reverse_scan


class AggregateTests(unittest.TestCase):
    def test_only_independently_passing_members_are_retained(self):
        graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "many",
                    "type": "aggregate",
                    "code": "E_MANY",
                    "members": [
                        {"code": "E_OK", "diagnostics": ["D_OK"]},
                        {"code": "E_BAD_CODE", "diagnostics": ["D_BAD_CODE"]},
                        {"code": "E_BAD_DIAG", "diagnostics": ["D_BAD_DIAG"]},
                    ],
                },
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [["start", "many"], ["many", "exit", "error"]],
        }
        report = audit_flow(
            graph,
            {"D_OK"},
            "caller-1",
            public_codes={"E_MANY", "E_OK"},
        )

        self.assertFalse(report.ok)
        self.assertIn("UNDECLARED_ERROR_CODE", report.codes())
        self.assertIn("UNDECLARED_DIAGNOSTIC", report.codes())

        exit_report = report.exit("exit")
        aggregate_report = exit_report.scenarios[0].error
        self.assertTrue(aggregate_report.aggregate)
        self.assertEqual(len(aggregate_report.members), 3)
        self.assertEqual(len(aggregate_report.retained_members), 1)
        self.assertEqual(aggregate_report.retained_members[0].code, "E_OK")

        safe = exit_report.safe_response
        self.assertEqual(safe["code"], "E_MANY")
        self.assertEqual(safe["diagnostics"], ["D_OK"])
        self.assertEqual(len(safe["members"]), 1)
        self.assertEqual(safe["members"][0]["code"], "E_OK")

    def test_all_members_may_pass(self):
        graph = {
            "nodes": [
                {"id": "many", "type": "aggregate", "code": "E_MANY",
                 "members": [{"code": "E_A", "diagnostics": ["D_A"]}]},
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [["many", "exit"]],
        }
        report = audit_flow(
            graph, {"D_A"}, "c", public_codes={"E_MANY", "E_A"}
        )
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        self.assertEqual(len(report.exit("exit").scenarios[0].error.retained_members), 1)

    def test_empty_aggregate_is_a_warning_but_does_not_crash(self):
        graph = {
            "nodes": [
                {"id": "many", "type": "aggregate", "code": "E_MANY"},
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [["many", "exit", "error"]],
        }
        report = audit_flow(graph, set(), "c", public_codes={"E_MANY"})
        finding = next(f for f in report.all_findings if f.code == "EMPTY_AGGREGATE")
        self.assertEqual(finding.severity, "warning")
        self.assertTrue(report.ok)
        self.assertEqual(
            report.exit("exit").scenarios[0].error.members, ()
        )


class ReachabilityAndInterleavingTests(unittest.TestCase):
    def test_only_reachable_response_exits_are_checked_for_success(self):
        graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "safe_fault", "type": "raise", "code": "E_SAFE",
                 "diagnostics": ["D_SAFE"]},
                {"id": "reachable_exit", "type": "response", "kind": "error"},
                {"id": "orphan_fault", "type": "raise", "code": "E_ORPHAN",
                 "diagnostics": ["D_ORPHAN"]},
                {"id": "unreachable_exit", "type": "response", "kind": "error"},
            ],
            "edges": [
                ["start", "safe_fault"],
                ["safe_fault", "reachable_exit", "error"],
                ["orphan_fault", "unreachable_exit", "error"],
            ],
        }
        report = audit_flow(
            graph, {"D_SAFE"}, "agent", public_codes={"E_SAFE"}
        )
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        self.assertEqual(len(report.exits), 2)
        self.assertTrue(report.exit("reachable_exit").reachable)
        self.assertFalse(report.exit("unreachable_exit").reachable)

    def test_alternative_paths_into_one_exit_are_separate_scenarios(self):
        graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "fault_a", "type": "raise", "code": "E_A",
                 "diagnostics": ["D_A"]},
                {"id": "fault_b", "type": "raise", "code": "E_B",
                 "diagnostics": ["D_B"]},
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [
                ["start", "fault_a"],
                ["start", "fault_b"],
                ["fault_a", "exit", "error"],
                ["fault_b", "exit", "error"],
            ],
        }
        report = audit_flow(
            graph, {"D_A", "E_A"}, "agent",
            public_codes={"E_A", "E_B"},
        )
        exit_report = report.exit("exit")
        self.assertEqual(len(exit_report.scenarios), 2)
        self.assertFalse(exit_report.ok)
        safe_responses = exit_report.safe_responses
        diagnostic_sets = [tuple(r["diagnostics"]) for r in safe_responses]
        self.assertIn(("D_A",), diagnostic_sets)
        self.assertIn((), diagnostic_sets)  # D_B is redacted from second path

    def test_reverse_identity_filter_uses_semantic_propagation(self):
        graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "fault", "type": "raise", "code": "E",
                 "diagnostics": ["D_PUBLIC", "D_ALICE_ONLY"]},
                {"id": "recover", "type": "recover",
                 "propagate_diagnostics": ["D_PUBLIC"]},
                {"id": "ok", "type": "response", "kind": "normal"},
            ],
            "edges": [
                ["start", "fault"],
                ["fault", "recover", "error"],
                ["recover", "ok", "normal"],
            ],
        }
        grants = {"D_PUBLIC": True, "D_ALICE_ONLY": ["alice"]}
        slices = reverse_identity_filter(graph, "bob", grants)
        ok_slice = slices["ok"]
        self.assertEqual([d.id for d in ok_slice.diagnostics], ["D_PUBLIC"])
        self.assertEqual([d.id for d in ok_slice.allowed_diagnostics], ["D_PUBLIC"])
        self.assertEqual(ok_slice.blocked_diagnostics, ())

        alice_slice = reverse_scan(graph, "ok", public_diagnostics=grants, caller="alice")
        self.assertEqual([d.id for d in alice_slice.allowed_diagnostics], ["D_PUBLIC"])
        # D_ALICE_ONLY existed in the caught fault but was not propagated by
        # recovery, so it must not appear in the semantic reverse slice.
        self.assertNotIn("D_ALICE_ONLY", [d.id for d in alice_slice.diagnostics])


class FieldAndSourceTests(unittest.TestCase):
    def test_response_field_allowlist_and_sources_are_checked(self):
        graph = {
            "nodes": [
                {"id": "fault", "type": "raise", "code": "E",
                 "diagnostics": [{"id": "D_STACK", "source": "job-host"}]},
                {
                    "id": "exit",
                    "type": "response",
                    "kind": "error",
                    "permitted_fields": ["code", "diagnostics"],
                    "fields": {
                        "code": "@code",
                        "diagnostics": "@diagnostics",
                        "stack": {"source": "D_STACK"},
                    },
                },
            ],
            "edges": [["fault", "exit", "error"]],
        }
        report = audit_flow(graph, {"D_STACK"}, "agent", public_codes={"E"})
        self.assertFalse(report.ok)
        self.assertIn("FIELD_NOT_PERMITTED", report.codes())
        field_report = next(f for f in report.exit("exit").scenarios[0].fields if f.name == "stack")
        self.assertFalse(field_report.licensed)
        self.assertEqual(field_report.diagnostics[0].id, "D_STACK")
        self.assertEqual(field_report.diagnostics[0].diagnostic.source, "job-host")

    def test_declared_identifier_does_not_authorize_wrong_source(self):
        graph = {
            "nodes": [
                {
                    "id": "fault",
                    "type": "raise",
                    "code": "E",
                    "diagnostics": [{"id": "D_METRIC", "source": "unexpected-source"}],
                },
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [["fault", "exit", "error"]],
        }
        report = audit_flow(
            graph,
            {"D_METRIC": {"public": True, "sources": ["approved-source"]}},
            "agent",
            public_codes={"E"},
        )
        self.assertFalse(report.ok)
        finding = next(
            f for f in report.violations if f.diagnostic_id == "D_METRIC"
        )
        self.assertEqual(finding.code, "DIAGNOSTIC_NOT_VISIBLE_TO_CALLER")
        self.assertIn("source_not_licensed", finding.details["reasons"])


if __name__ == "__main__":
    unittest.main()
