import json
import unittest

from static_check import AuditReport, audit_flow
from static_check.policy import DisclosurePolicy


class ComplexInterleavingTests(unittest.TestCase):
    def test_wrapping_recovery_and_aggregate_interleave(self):
        graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "io_fault",
                    "type": "raise",
                    "code": "E_IO",
                    "diagnostics": ["D_IO_SECRET"],
                },
                {
                    "id": "wrap_io",
                    "type": "wrap",
                    "code": "E_JOB",
                    "public": True,
                    "diagnostics": ["D_JOB_PUBLIC"],
                },
                {
                    "id": "net_fault",
                    "type": "raise",
                    "code": "E_NET_INTERNAL",
                    "diagnostics": ["D_NET_SECRET"],
                },
                {
                    "id": "recover_net",
                    "type": "replace",
                    "code": "E_NET_PUBLIC",
                    "diagnostics": ["D_NET_DIAG"],
                },
                {"id": "aggregate", "type": "aggregate", "code": "E_MULTI"},
                {"id": "exit", "type": "response", "kind": "error"},
            ],
            "edges": [
                ["start", "io_fault"],
                ["start", "net_fault"],
                ["io_fault", "wrap_io", "error"],
                ["net_fault", "recover_net", "error"],
                {"from": "wrap_io", "to": "aggregate", "branch": "member"},
                {"from": "recover_net", "to": "aggregate", "branch": "member"},
                ["aggregate", "exit", "error"],
            ],
        }
        report = audit_flow(
            graph,
            {"D_JOB_PUBLIC", "D_NET_DIAG"},
            "agent-7",
            # No public_codes argument: E_JOB certifies itself in the graph,
            # while the replacement code is granted below.
            policy=DisclosurePolicy(
                caller="agent-7",
                public_diagnostics={"D_JOB_PUBLIC", "D_NET_DIAG"},
                public_codes={"E_NET_PUBLIC", "E_MULTI"},
            ),
        )

        self.assertFalse(report.ok)
        codes = report.codes()
        self.assertIn("UNDECLARED_DIAGNOSTIC", codes)  # inner IO/NET secrets
        self.assertIn("UNDECLARED_ERROR_CODE", codes)  # E_IO/E_NET_INTERNAL

        aggregate = report.exit("exit").scenarios[0].error
        retained_codes = [m.code for m in aggregate.retained_members]
        self.assertEqual(retained_codes, ["E_NET_PUBLIC"])
        safe_members = report.exit("exit").safe_response["members"]
        self.assertEqual(len(safe_members), 1)
        self.assertEqual(safe_members[0]["code"], "E_NET_PUBLIC")
        self.assertEqual(safe_members[0]["diagnostics"], ["D_NET_DIAG"])

    def test_identity_mapping_and_roles(self):
        policy = DisclosurePolicy(
            caller={"id": "alice", "roles": ["ops"]},
            public_diagnostics={
                "D_PUBLIC": True,
                "D_ALICE": ["alice"],
                "D_OPS": {"roles": ["ops"]},
                "D_DEV": {"roles": ["dev"]},
            },
        )
        report = audit_flow(
            {
                "nodes": [
                    {"id": "e", "type": "raise", "code": "E",
                     "diagnostics": ["D_PUBLIC", "D_ALICE", "D_OPS", "D_DEV"]},
                    {"id": "out", "type": "response", "kind": "error",
                     "emit_cause": False},
                ],
                "edges": [["e", "out", "error"]],
            },
            policy=policy,
            public_codes={"E"},
        )
        allowed = report.exit("out").allowed_diagnostics
        self.assertIn("D_PUBLIC", allowed)
        self.assertIn("D_ALICE", allowed)
        self.assertIn("D_OPS", allowed)
        self.assertNotIn("D_DEV", allowed)
        self.assertFalse(report.ok)


class ApiAndBoundaryTests(unittest.TestCase):
    def test_inline_json_graph_is_accepted(self):
        graph = json.dumps(
            {
                "nodes": [
                    {"id": "e", "type": "raise", "code": "E", "diagnostics": ["D"]},
                    {"id": "out", "type": "response", "kind": "error"},
                ],
                "edges": [["e", "out"]],
            }
        )
        report = audit_flow(graph, {"D", "E"}, "agent")
        self.assertIsInstance(report, AuditReport)
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])

    def test_combined_public_identifiers_can_authorize_codes(self):
        report = audit_flow(
            {
                "nodes": [
                    {"id": "e", "type": "raise", "code": "E_PUBLIC",
                     "diagnostics": ["D_PUBLIC"]},
                    {"id": "out", "type": "response", "kind": "error"},
                ],
                "edges": [["e", "out"]],
            },
            {"E_PUBLIC", "D_PUBLIC"},
            "agent",
        )
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])

    def test_malformed_graph_is_reported_not_raised(self):
        report = audit_flow(
            {
                "nodes": [{"id": "out", "type": "response"}],
                "edges": [{"from": "missing", "to": "out"}],
            },
            set(),
            "agent",
        )
        self.assertFalse(report.ok)
        self.assertIn("INVALID_EDGE_SOURCE", report.codes())

    def test_cycle_is_fail_closed_and_does_not_hang(self):
        report = audit_flow(
            {
                "entry": "start",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "a", "type": "wrap", "code": "E_A"},
                    {"id": "b", "type": "wrap", "code": "E_B"},
                    {"id": "out", "type": "response", "kind": "error"},
                ],
                "edges": [
                    ["start", "a"],
                    ["a", "b", "error"],
                    ["b", "a", "error"],
                    ["a", "out", "error"],
                ],
            },
            {"E_A", "E_B"},
            "agent",
        )
        self.assertFalse(report.ok)
        self.assertIn("UNSUPPORTED_FLOW_CYCLE", report.codes())

    def test_empty_graph_is_an_audit_failure(self):
        report = audit_flow({}, set(), "agent")
        self.assertFalse(report.ok)
        self.assertIn("NO_ENTRY", report.codes())
        self.assertEqual(report.reachable_exits, [])

    def test_unauthenticated_caller_only_sees_public_grants(self):
        report = audit_flow(
            {
                "nodes": [
                    {"id": "e", "type": "raise", "code": "E",
                     "diagnostics": ["D_PUBLIC", "D_ALICE"]},
                    {"id": "out", "type": "response", "kind": "error"},
                ],
                "edges": [["e", "out"]],
            },
            {"D_PUBLIC": True, "D_ALICE": ["alice"], "E": True},
            None,
        )
        self.assertFalse(report.ok)
        allowed = report.exit("out").allowed_diagnostics
        self.assertEqual(allowed, ("D_PUBLIC",))

    def test_explicit_identity_filter_node_removes_blocked_diagnostics(self):
        report = audit_flow(
            {
                "entry": "start",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "e", "type": "raise", "code": "E",
                     "diagnostics": ["D_PUBLIC", "D_ALICE"]},
                    {"id": "filter", "type": "filter"},
                    {"id": "out", "type": "response", "kind": "error"},
                ],
                "edges": [
                    ["start", "e"],
                    ["e", "filter", "error"],
                    ["filter", "out", "error"],
                ],
            },
            {"D_PUBLIC": True, "D_ALICE": ["alice"], "E": True},
            "bob",
        )
        self.assertTrue(report.ok, [f.to_dict() for f in report.all_findings])
        safe = report.exit("out").safe_response
        self.assertEqual(safe["diagnostics"], ["D_PUBLIC"])


if __name__ == "__main__":
    unittest.main()
