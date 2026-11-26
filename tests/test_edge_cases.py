import unittest

from readjudicator import OperationRequest, Proof, Requirement, adjudicate


class TimeWindowEdgeTests(unittest.TestCase):
    def test_proof_expiry_boundary_is_inclusive(self):
        proof = Proof(
            "p-expiring",
            "generic",
            capabilities={"time-bound"},
            expires_at=100,
        )
        op = OperationRequest("O", [Requirement("time-bound")], deadline=200)

        at_expiry = adjudicate([op], [proof], now=100)
        after_expiry = adjudicate([op], [proof], now=100.001)

        self.assertEqual(at_expiry.approved_ids, ("O",))
        self.assertEqual(after_expiry.approved_ids, ())
        self.assertEqual(
            after_expiry.rejected[0].missing_capabilities[0].reason_code,
            "proof_expired",
        )

    def test_non_edf_order_can_preserve_early_expiring_proof(self):
        early = Proof(
            "p-early",
            "generic",
            capabilities={"early-cap"},
            expires_at=1,
        )
        normal = Proof("p-normal", "generic", capabilities={"normal-cap"})
        operations = [
            OperationRequest(
                "A-early-first",
                [Requirement("early-cap")],
                deadline=100,
                duration=0,
            ),
            OperationRequest(
                "B-urgent-deadline-2",
                [Requirement("normal-cap")],
                deadline=2,
                duration=2,
            ),
        ]

        result = adjudicate(operations, [early, normal], now=0)

        self.assertEqual(result.maximum_size, 2)
        self.assertEqual(result.approved_ids, ("A-early-first", "B-urgent-deadline-2"))
        self.assertEqual(result.approved[0].confirmed_at, 0)
        self.assertEqual(result.approved[1].confirmed_at, 2)


class CombinationDiagnosisTests(unittest.TestCase):
    def test_distinct_capabilities_on_one_one_time_proof_are_jointly_contended(self):
        proof = Proof("p-both", "generic", capabilities={"x", "y"}, uses=1)
        op = OperationRequest(
            "O-combo",
            [Requirement("x"), Requirement("y")],
            deadline=10,
        )

        result = adjudicate([op], [proof])

        self.assertEqual(result.maximum_size, 0)
        rejected = result.rejected[0]
        self.assertEqual(rejected.reason_code, "conflicting_proof_combination")
        self.assertEqual(
            {m.reason_code for m in rejected.missing_capabilities},
            {"joint_proof_contention"},
        )
        self.assertEqual(
            {m.capability for m in rejected.missing_capabilities},
            {"x", "y"},
        )


class EmptyAndSerializationTests(unittest.TestCase):
    def test_empty_batch_is_valid_json_result(self):
        result = adjudicate([])
        self.assertEqual(result.maximum_size, 0)
        self.assertEqual(result.approved_ids, ())
        self.assertEqual(result.rejected, ())
        json_text = result.to_json()
        self.assertIn('"maximum_size": 0', json_text)


if __name__ == "__main__":
    unittest.main()
