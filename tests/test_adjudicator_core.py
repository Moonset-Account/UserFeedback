import unittest

from readjudicator import (
    AdjudicationSession,
    OperationRequest,
    Proof,
    Requirement,
    adjudicate,
)


def token_proof(proof_id: str, capability: str) -> Proof:
    return Proof(proof_id=proof_id, kind="generic", capabilities={capability})


class MaximumSetTests(unittest.TestCase):
    def test_approves_normal_identity_bound_combination(self):
        proofs = [
            Proof.presence("p-alice", identity="alice", roles=["operator"]),
            Proof.device_proximity("d-console", device_id="tab-1", near="console"),
        ]
        op = OperationRequest(
            "O-normal",
            [
                Requirement(
                    "presence",
                    proof_kind="presence",
                    identity_matches_request=True,
                ),
                Requirement(
                    "device_proximity",
                    proof_kind="device",
                    attributes={"near": "console"},
                ),
            ],
            deadline=100,
            identity="alice",
        )

        result = adjudicate([op], proofs, now=10)

        self.assertEqual(result.maximum_size, 1)
        self.assertEqual(result.approved_ids, ("O-normal",))
        approved = result.approved[0]
        self.assertEqual(approved.proof_ids, ("p-alice", "d-console"))
        self.assertEqual(
            approved.proof_combination,
            {"presence": "p-alice", "device_proximity": "d-console"},
        )
        self.assertEqual(approved.assignments[0].identity, "alice")

    def test_repeated_requirement_reports_list_of_proof_ids(self):
        proofs = [token_proof("p1", "shared-slot"), token_proof("p2", "shared-slot")]
        op = OperationRequest("O-repeat", [Requirement("shared-slot", count=2)], deadline=10)

        result = adjudicate([op], proofs)

        self.assertEqual(result.approved_ids, ("O-repeat",))
        self.assertEqual(
            result.proof_combinations["O-repeat"],
            {"shared-slot": ["p1", "p2"]},
        )

    def test_exact_search_prefers_two_small_operations_over_one_big_operation(self):
        proofs = [token_proof("pa", "code-a"), token_proof("pb", "code-b")]
        operations = [
            OperationRequest(
                "O-big",
                [Requirement("code-a"), Requirement("code-b")],
                deadline=100,
            ),
            OperationRequest("O-small-a", [Requirement("code-a")], deadline=100),
            OperationRequest("O-small-b", [Requirement("code-b")], deadline=100),
        ]

        result = adjudicate(operations, proofs)

        self.assertEqual(result.maximum_size, 2)
        self.assertEqual(result.approved_ids, ("O-small-a", "O-small-b"))
        self.assertEqual(result.rejected_ids, ("O-big",))
        self.assertEqual(result.proof_combinations["O-small-a"], {"code-a": "pa"})
        self.assertEqual(result.proof_combinations["O-small-b"], {"code-b": "pb"})

    def test_parallel_requests_compete_for_same_one_time_proof(self):
        proofs = [token_proof("shared-token", "console-token")]
        operations = [
            OperationRequest("O1", [Requirement("console-token")], deadline=10),
            OperationRequest("O2", [Requirement("console-token")], deadline=10),
        ]

        result = adjudicate(operations, proofs)

        self.assertEqual(result.maximum_size, 1)
        self.assertEqual(result.approved_ids, ("O1",))
        rejected = result.rejected[0]
        self.assertEqual(rejected.request_id, "O2")
        missing = rejected.missing_capabilities[0]
        self.assertEqual(missing.reason_code, "exhausted_by_approval")
        self.assertEqual(missing.available_after, 0)
        self.assertEqual(missing.available_globally, 1)
        self.assertEqual(missing.consumed_by, {"shared-token": ("O1",)})

    def test_joint_backtracking_reassigns_proof_between_operations(self):
        proofs = [
            Proof("p1", "generic", capabilities={"approval", "audit"}),
            Proof("p2", "generic", capabilities={"approval"}),
        ]
        operations = [
            OperationRequest("O1", [Requirement("approval")], deadline=10),
            OperationRequest("O2", [Requirement("audit")], deadline=10),
        ]

        result = adjudicate(operations, proofs)

        self.assertEqual(result.maximum_size, 2)
        combinations = result.proof_combinations
        self.assertEqual(combinations["O1"], {"approval": "p2"})
        self.assertEqual(combinations["O2"], {"audit": "p1"})

    def test_absolute_missing_capability_is_reported(self):
        op = OperationRequest("O-missing", [Requirement("dual-control")], deadline=1)

        result = adjudicate([op], [])

        self.assertEqual(result.maximum_size, 0)
        rejected = result.rejected[0]
        self.assertEqual(rejected.reason_code, "requirements_unavailable")
        missing = rejected.missing_capabilities[0]
        self.assertEqual(missing.capability, "dual-control")
        self.assertEqual(missing.reason_code, "capability_absent")
        self.assertEqual(missing.available_globally, 0)
        self.assertEqual(missing.available_after, 0)

    def test_identity_mismatch_is_a_defensive_rejection(self):
        proofs = [Proof.presence("p-bob", identity="bob")]
        op = OperationRequest(
            "O-alice",
            [
                Requirement(
                    "presence",
                    proof_kind="presence",
                    identity_matches_request=True,
                )
            ],
            identity="alice",
            deadline=10,
        )

        result = adjudicate([op], proofs)

        self.assertEqual(result.maximum_size, 0)
        self.assertEqual(
            result.rejected[0].missing_capabilities[0].reason_code,
            "identity_mismatch",
        )

    def test_allowed_identity_can_satisfy_second_person_requirement(self):
        proofs = [Proof.presence("p-supervisor", identity="supervisor-7", roles=["supervisor"])]
        op = OperationRequest(
            "O-dual",
            [Requirement("supervisor", proof_kind="presence", allowed_identities={"supervisor-7"})],
            identity="operator-1",
            deadline=10,
        )

        result = adjudicate([op], proofs)

        self.assertEqual(result.approved_ids, ("O-dual",))
        self.assertEqual(result.approved[0].assignments[0].identity, "supervisor-7")


class AtomicSessionTests(unittest.TestCase):
    def test_failed_double_use_rolls_back_then_later_operation_succeeds(self):
        session = AdjudicationSession([token_proof("only-x", "x")])

        bad = session.review(
            OperationRequest("bad", [Requirement("x", count=2)], deadline=10)
        )
        self.assertIsInstance(bad, type(session.history[0]))
        self.assertEqual(bad.request_id, "bad")
        self.assertEqual(session.ledger.uses_left["only-x"], 1)

        good = session.review(OperationRequest("good", [Requirement("x")], deadline=10))
        self.assertEqual(good.request_id, "good")
        self.assertEqual(session.ledger.uses_left["only-x"], 0)
        self.assertEqual([type(item).__name__ for item in session.history], ["RejectedOperation", "ApprovedOperation"])

    def test_failed_allocation_keeps_previously_confirmed_proof_consumed(self):
        session = AdjudicationSession(
            [token_proof("confirmed-proof", "persistent"), token_proof("rollback-proof", "x")]
        )

        first = session.review(OperationRequest("first", [Requirement("persistent")], deadline=10))
        self.assertEqual(first.request_id, "first")
        self.assertEqual(session.ledger.uses_left["confirmed-proof"], 0)

        bad = session.review(
            OperationRequest("bad", [Requirement("x", count=2)], deadline=10)
        )
        self.assertEqual(bad.request_id, "bad")

        # The failed attempt releases its tentative x hold, but does not roll
        # back the already confirmed first operation.
        self.assertEqual(session.ledger.uses_left["rollback-proof"], 1)
        self.assertEqual(session.ledger.uses_left["confirmed-proof"], 0)
        self.assertEqual([op.request_id for op in session.approved], ["first"])

    def test_interleaved_success_failure_retry_success(self):
        session = AdjudicationSession(
            [token_proof("a", "cap-a"), token_proof("b", "cap-b")],
            now=100,
        )

        first = session.review(OperationRequest("op-a", [Requirement("cap-a")], deadline=200))
        failed = session.review(
            OperationRequest(
                "op-impossible",
                [Requirement("cap-b"), Requirement("missing")],
                deadline=200,
            )
        )
        second = session.review(OperationRequest("op-b", [Requirement("cap-b")], deadline=200))

        self.assertEqual(first.request_id, "op-a")
        self.assertEqual(failed.request_id, "op-impossible")
        self.assertEqual(second.request_id, "op-b")
        self.assertEqual(session.ledger.uses_left["a"], 0)
        self.assertEqual(session.ledger.uses_left["b"], 0)
        self.assertEqual(len(session.history), 3)


if __name__ == "__main__":
    unittest.main()
