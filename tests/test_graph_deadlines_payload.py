import json
import unittest

from readjudicator import (
    AdjudicationSession,
    OperationRequest,
    Proof,
    RelayEdge,
    RelayGraph,
    Requirement,
    adjudicate,
    adjudicate_payload,
)


class DeadlineBoundaryTests(unittest.TestCase):
    def test_deadline_is_inclusive_at_current_instant(self):
        proof = Proof(proof_id="p", kind="generic", capabilities={"c"})

        on_time = adjudicate(
            [OperationRequest("edge", [Requirement("c")], deadline=100)],
            [proof],
            now=100,
        )
        late = adjudicate(
            [OperationRequest("late", [Requirement("c")], deadline=99.999)],
            [proof],
            now=100,
        )

        self.assertEqual(on_time.approved_ids, ("edge",))
        self.assertEqual(late.maximum_size, 0)
        self.assertEqual(late.rejected[0].reason_code, "deadline_exceeded")

    def test_terminal_duration_completion_boundary(self):
        proof = Proof(proof_id="p", kind="generic", capabilities={"c"}, uses=None)
        exact = OperationRequest("exact", [Requirement("c")], deadline=5, duration=5)
        too_late = OperationRequest("late", [Requirement("c")], deadline=4, duration=5)

        ok = adjudicate([exact], [proof], now=0)
        bad = adjudicate([too_late], [proof], now=0)

        self.assertEqual(ok.approved[0].confirmed_at, 5)
        self.assertEqual(ok.approved[0].started_at, 0)
        self.assertEqual(bad.approved_ids, ())
        self.assertEqual(bad.rejected[0].reason_code, "deadline_exceeded")

    def test_edf_schedule_allows_two_operations_and_rejects_overlong_pair(self):
        proof = Proof(proof_id="p", kind="generic", capabilities={"c"}, uses=None)
        feasible = [
            OperationRequest("d5", [Requirement("c")], deadline=5, duration=5),
            OperationRequest("d10", [Requirement("c")], deadline=10, duration=5),
        ]
        result = adjudicate(feasible, [proof], now=0)
        self.assertEqual(result.approved_ids, ("d5", "d10"))
        self.assertEqual([op.order for op in result.approved], [1, 2])
        self.assertEqual([op.confirmed_at for op in result.approved], [5, 10])

        tight = [
            OperationRequest("a-d5", [Requirement("c")], deadline=5, duration=5),
            OperationRequest("b-d6", [Requirement("c")], deadline=6, duration=5),
        ]
        tight_result = adjudicate(tight, [proof], now=0)
        self.assertEqual(tight_result.maximum_size, 1)
        self.assertEqual(tight_result.approved_ids, ("a-d5",))


class RelayGraphTests(unittest.TestCase):
    def graph(self) -> RelayGraph:
        return RelayGraph(
            [
                RelayEdge.device_only("gateway", "control", "dev-edge"),
                RelayEdge.presence_handoff("floor", "control", "pres-edge"),
            ]
        )

    def test_device_and_presence_follow_different_relay_edges(self):
        proofs = [
            Proof.presence("p-alice", identity="alice", node="floor"),
            Proof.device_proximity(
                "d-tab", device_id="tab-1", near="terminal-1", node="gateway"
            ),
        ]
        op = OperationRequest(
            "O-relay",
            [
                Requirement(
                    "presence",
                    proof_kind="presence",
                    identity_matches_request=True,
                ),
                Requirement(
                    "device_proximity",
                    proof_kind="device",
                    attributes={"near": "terminal-1"},
                ),
            ],
            identity="alice",
            entry_node="control",
            deadline=10,
        )

        result = adjudicate([op], proofs, graph=self.graph())

        self.assertEqual(result.approved_ids, ("O-relay",))
        by_requirement = {a.requirement: a for a in result.approved[0].assignments}
        self.assertEqual(by_requirement["presence"].route_edges, ("pres-edge",))
        self.assertEqual(
            by_requirement["presence"].authorization_costs,
            {"presence_authorization": 1},
        )
        self.assertEqual(by_requirement["device_proximity"].route_edges, ("dev-edge",))
        self.assertEqual(by_requirement["device_proximity"].authorization_costs, {})

    def test_device_only_edge_does_not_carry_presence(self):
        graph = RelayGraph([RelayEdge.device_only("field", "control", "device-only")])
        proof = Proof.presence("p-alice", identity="alice", node="field")
        op = OperationRequest(
            "O-no-route",
            [Requirement("presence", proof_kind="presence")],
            identity="alice",
            entry_node="control",
            deadline=10,
        )

        result = adjudicate([op], [proof], graph=graph)

        self.assertEqual(result.maximum_size, 0)
        missing = result.rejected[0].missing_capabilities[0]
        self.assertEqual(missing.reason_code, "no_relay_route")
        self.assertEqual(missing.candidate_proof_ids, ())

    def test_parallel_graph_branches_share_one_time_proof(self):
        graph = RelayGraph(
            [
                RelayEdge("shared", "op-a", "to-a"),
                RelayEdge("shared", "op-b", "to-b"),
            ]
        )
        proof = Proof("token", "generic", capabilities={"branch-token"}, node="shared")
        operations = [
            OperationRequest(
                "op-a", [Requirement("branch-token")], entry_node="op-a", deadline=10
            ),
            OperationRequest(
                "op-b", [Requirement("branch-token")], entry_node="op-b", deadline=10
            ),
        ]

        result = adjudicate(operations, [proof], graph=graph)

        self.assertEqual(result.approved_ids, ("op-a",))
        assignment = result.approved[0].assignments[0]
        self.assertEqual(assignment.route_edges, ("to-a",))
        self.assertEqual(
            result.rejected[0].missing_capabilities[0].reason_code,
            "exhausted_by_approval",
        )

    def test_presence_edge_authorization_is_consumed_once_but_proof_use_remains_available_on_failure(self):
        graph = RelayGraph([RelayEdge.presence_handoff("floor", "control", "hand-off")])
        proof = Proof(
            proof_id="p-multi",
            kind="presence",
            identity="alice",
            capabilities=frozenset({"presence"}),
            uses=2,
            authorizations={"presence_authorization": 1},
            node="floor",
        )
        operations = [
            OperationRequest(
                "op1",
                [Requirement("presence", proof_kind="presence")],
                identity="alice",
                entry_node="control",
                deadline=10,
            ),
            OperationRequest(
                "op2",
                [Requirement("presence", proof_kind="presence")],
                identity="alice",
                entry_node="control",
                deadline=10,
            ),
        ]

        session = AdjudicationSession([proof], graph=graph)
        first = session.review(operations[0])
        second = session.review(operations[1])

        self.assertEqual(first.request_id, "op1")
        self.assertEqual(second.request_id, "op2")
        self.assertEqual(type(second).__name__, "RejectedOperation")
        self.assertEqual(session.ledger.uses_left["p-multi"], 1)
        self.assertEqual(session.ledger.auth_left["p-multi"]["presence_authorization"], 0)


class PayloadTests(unittest.TestCase):
    def test_json_like_payload_and_serialization(self):
        payload = {
            "now": 100,
            "terminal_id": "control-room-1",
            "presence_proofs": [
                {"id": "p1", "identity": "alice", "capabilities": ["operator"], "node": "floor"}
            ],
            "device_proofs": [
                {"id": "d1", "near": "terminal-1", "node": "gateway", "one_time": True}
            ],
            "graph": {
                "edges": [
                    {"id": "e-pres", "from": "floor", "to": "control", "mode": "presence_handoff"},
                    {"id": "e-dev", "from": "gateway", "to": "control", "mode": "device_only"},
                ]
            },
            "operations": [
                {
                    "id": "O1",
                    "identity": "alice",
                    "deadline": 101,
                    "entry_node": "control",
                    "requirements": [
                        {"capability": "presence", "kind": "presence", "identity_matches_request": True},
                        {"capability": "device_proximity", "kind": "device", "attributes": {"near": "terminal-1"}},
                    ],
                }
            ],
        }

        result = adjudicate_payload(payload)
        encoded = result.to_json()
        decoded = json.loads(encoded)

        self.assertEqual(result.approved_ids, ("O1",))
        self.assertEqual(decoded["terminal_id"], "control-room-1")
        self.assertEqual(
            decoded["approved"][0]["proof_combination"]["presence"], "p1"
        )
        self.assertEqual(
            decoded["approved"][0]["assignments"][0]["route_edges"], ["e-pres"]
        )


if __name__ == "__main__":
    unittest.main()
