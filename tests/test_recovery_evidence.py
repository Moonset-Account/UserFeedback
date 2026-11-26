import json
import unittest

from transfer_approval import evaluate_case


def recovery_case():
    return {
        "max_delay": 10,
        "key_group": "g",
        "transcript": {
            "validated": True,
            "prior_windows": [
                {"id": "sw1", "side": "sender", "key_group": "g"},
                {"id": "rw1", "side": "receiver", "key_group": "g"},
            ],
            "commitments": [
                {"id": "c-send", "subject": "send", "key_group": "g", "window_id": "sw1", "interval": [1, 3]},
                {"id": "c-recv", "subject": "receive", "key_group": "g", "window_id": "rw1", "interval": [7, 9]},
            ],
            "recoveries": [
                {"id": "r1", "inherits": {"sender": ["sw1"], "receiver": ["rw1"]}}
            ],
        },
        "sender": {
            "trusted_interval": [1, 3],
            "windows": [
                {"id": "sw1", "start": 0, "end": 6, "key_group": "g", "inherited": True, "recovery_id": "r1"}
            ],
        },
        "receiver": {
            "trusted_interval": [7, 9],
            "windows": [
                {"id": "rw1", "start": 6, "end": 12, "key_group": "g", "inherited": True, "recovery_id": "r1"}
            ],
        },
    }


class RecoveryAndLateEvidenceTests(unittest.TestCase):
    def test_declared_inherited_windows_are_usable(self):
        result = evaluate_case(recovery_case())
        self.assertEqual(result["status"], "certain", result["conflicts"])
        self.assertEqual(result["recoveries"][0]["declared"], {
            "sender": ["sw1"],
            "receiver": ["rw1"],
        })
        self.assertTrue(result["sender"]["windows"][0]["inheritance_valid"])
        self.assertTrue(result["receiver"]["windows"][0]["inheritance_valid"])
        # The complete public report must be JSON serializable.
        json.dumps(result)

    def test_undeclared_inherited_window_is_contradiction(self):
        case = recovery_case()
        case["sender"]["windows"][0]["id"] = "sw2"
        result = evaluate_case(case)

        self.assertEqual(result["status"], "contradictory")
        self.assertFalse(result["approved"])
        codes = {c["code"] for c in result["conflicts"]}
        self.assertIn("undeclared_window_inheritance", codes)
        self.assertTrue(
            any(c["code"] == "undeclared_window_inheritance" for c in result["source_contradictions"])
        )

    def test_declaration_of_unknown_prior_window_is_contradiction(self):
        case = recovery_case()
        case["transcript"]["recoveries"][0]["inherits"]["sender"].append("ghost-window")
        result = evaluate_case(case)

        self.assertEqual(result["status"], "contradictory")
        self.assertIn("unknown_inherited_window", [c["code"] for c in result["conflicts"]])

    def test_inheritance_is_never_implicit(self):
        case = recovery_case()
        case["transcript"]["recoveries"] = []
        result = evaluate_case(case)

        self.assertEqual(result["status"], "contradictory")
        self.assertIn("undeclared_window_inheritance", [c["code"] for c in result["conflicts"]])

    def test_recovery_may_declare_only_one_sides_window(self):
        case = recovery_case()
        case["transcript"]["recoveries"][0]["inherits"] = {"sender": ["sw1"]}
        result = evaluate_case(case)

        self.assertEqual(result["status"], "contradictory")
        undeclared = [
            c for c in result["conflicts"]
            if c["code"] == "undeclared_window_inheritance"
        ]
        self.assertEqual(len(undeclared), 1)
        self.assertEqual(undeclared[0]["side"], "receive")
        self.assertEqual(undeclared[0]["window_id"], "rw1")

    def test_disjoint_late_time_evidence_contradicts_commitment(self):
        case = recovery_case()
        case["transcript"]["supplemental_evidence"] = [
            {
                "id": "late-backup-clock",
                "source": "backup-clock",
                "commitment_id": "c-send",
                "interval": [20, 22],
            }
        ]
        # Keep the endpoint clock broad enough that the contradiction is from
        # transcript/provenance rather than malformed endpoint data.
        case["sender"]["trusted_interval"] = [1, 22]
        result = evaluate_case(case)

        self.assertEqual(result["status"], "contradictory")
        self.assertFalse(result["can_hold"])
        conflict_codes = {c["code"] for c in result["conflicts"]}
        self.assertIn("supplemental_time_conflict", conflict_codes)
        self.assertIn("time_evidence_conflict", conflict_codes)
        contradiction = next(
            c for c in result["source_contradictions"]
            if c["code"] == "supplemental_time_conflict"
        )
        self.assertEqual(contradiction["sources"], [
            "evidence:late-backup-clock",
            "commitment:c-send",
        ])
        json.dumps(result)

    def test_late_key_mismatch_is_contradiction(self):
        case = recovery_case()
        case["transcript"]["supplemental_evidence"] = [
            {
                "id": "late-key-note",
                "commitment_id": "c-send",
                "key_group": "rotated-key",
                "interval": [2, 2],
            }
        ]
        result = evaluate_case(case)
        self.assertEqual(result["status"], "contradictory")
        self.assertIn("evidence_key_mismatch", [c["code"] for c in result["conflicts"]])

    def test_unprefixed_global_inheritance_ids_are_resolved_from_prior_side(self):
        case = recovery_case()
        case["transcript"]["recoveries"][0]["inherits"] = ["sw1", "rw1"]
        result = evaluate_case(case)
        self.assertEqual(result["status"], "certain", result["conflicts"])
        declared = result["recoveries"][0]["declared"]
        self.assertEqual(declared["sender"], ["sw1"])
        self.assertEqual(declared["receiver"], ["rw1"])


if __name__ == "__main__":
    unittest.main()
