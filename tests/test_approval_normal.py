import unittest

from transfer_approval import approve_transfer, evaluate_case


def base_case(**overrides):
    case = {
        "max_delay": 10,
        "key_group": "duty-handover-key",
        "transcript": {"validated": True, "session_id": "handover-1"},
        "sender": {
            "observed_at": 2,
            "clock_error": 1,
            "windows": [
                {"id": "sender-window", "start": 0, "end": 6, "key_group": "duty-handover-key"}
            ],
        },
        "receiver": {
            "observed_at": 8,
            "clock_error": 1,
            "windows": [
                {"id": "receiver-window", "start": 6, "end": 12, "key_group": "duty-handover-key"}
            ],
        },
    }
    case.update(overrides)
    return case


class NormalApprovalTests(unittest.TestCase):
    def test_consistent_transfer_is_certainly_approvable(self):
        result = evaluate_case(base_case())

        self.assertEqual(result["status"], "certain")
        self.assertEqual(result["decision"], "approve")
        self.assertTrue(result["approved"])
        self.assertTrue(result["can_hold"])
        self.assertTrue(result["certain"])
        self.assertEqual(result["conflicts"], [])

        self.assertEqual(result["sender"]["fused_interval"], {
            "start": 1,
            "end": 3,
            "start_inclusive": True,
            "end_inclusive": True,
        })
        self.assertEqual(result["receiver"]["fused_interval"]["start"], 7)
        self.assertEqual(result["receiver"]["fused_interval"]["end"], 9)
        self.assertTrue(result["sender"]["window_guaranteed"])
        self.assertTrue(result["receiver"]["window_guaranteed"])
        self.assertEqual(result["delay"]["raw_possible_delay_bounds"], {
            "minimum": 4,
            "maximum": 8,
        })
        self.assertTrue(result["delay"]["guaranteed"])

        witness = result["delay"]["witness"]
        self.assertIsNotNone(witness)
        self.assertGreaterEqual(witness["delay"], 0)
        self.assertLessEqual(witness["delay"], 10)

        subjects = {item["subject"] for item in result["certain_periods"]}
        self.assertEqual(subjects, {"send", "receive"})

    def test_keyword_api_matches_case_api(self):
        result = approve_transfer(
            max_delay=10,
            key_group="k",
            sender_observed_at=2,
            sender_clock_error=1,
            sender_windows=[{"id": "s", "start": 0, "end": 6, "key_group": "k"}],
            receiver_observed_at=8,
            receiver_clock_error=1,
            receiver_windows=[{"id": "r", "start": 6, "end": 12, "key_group": "k"}],
        )
        self.assertEqual(result["status"], "certain")

    def test_different_key_groups_are_rejected(self):
        case = base_case()
        case["receiver"]["windows"][0]["key_group"] = "other-key"
        result = evaluate_case(case)

        self.assertFalse(result["approved"])
        self.assertEqual(result["status"], "infeasible")
        codes = {c["code"] for c in result["conflicts"]}
        self.assertIn("no_accepted_key_window", codes)

    def test_explicit_fixed_trusted_intervals(self):
        result = approve_transfer(
            max_delay=5,
            key_group="k",
            sender_interval=[0, 2],
            receiver_interval=[3, 5],
            sender_windows=[{"start": 0, "end": 4, "key_group": "k"}],
            receiver_windows=[{"start": 2, "end": 8, "key_group": "k"}],
        )
        self.assertEqual(result["status"], "certain")
        self.assertEqual(result["delay"]["raw_possible_delay_bounds"], {
            "minimum": 1,
            "maximum": 5,
        })

    def test_common_key_group_is_inferred_when_omitted(self):
        case = base_case()
        case.pop("key_group")
        result = evaluate_case(case)
        self.assertEqual(result["status"], "certain", result["conflicts"])
        self.assertEqual(result["key"]["key_group"], "duty-handover-key")

    def test_mismatched_advertised_key_groups_are_source_conflicts(self):
        case = base_case()
        case.pop("key_group")
        case["receiver"]["windows"][0]["key_group"] = "other-key"
        result = evaluate_case(case)
        self.assertEqual(result["status"], "contradictory")
        self.assertIn("no_common_key_group", [c["code"] for c in result["conflicts"]])

    def test_solution_module_reexports_public_api(self):
        import solution
        import transfer_approval

        self.assertTrue(callable(solution.evaluate_case))
        self.assertTrue(callable(solution.approve_transfer))
        self.assertIs(transfer_approval.solve, transfer_approval.evaluate_case)
        self.assertIs(solution.check_approval, solution.approve_transfer)

    def test_exact_key_ids_are_matched(self):
        case = {
            "max_delay": 2,
            "key_ids": ["k1", "k2"],
            "sender": {
                "trusted_interval": [0, 0],
                "windows": [{"start": 0, "end": 2, "key_ids": ["k1", "k2", "k3"]}],
            },
            "receiver": {
                "trusted_interval": [1, 1],
                "windows": [{"start": 0, "end": 2, "key_ids": ["k1", "k2"]}],
            },
        }
        result = evaluate_case(case)
        self.assertEqual(result["status"], "certain", result["conflicts"])

        case["receiver"]["windows"][0]["key_ids"] = ["k1"]
        bad = evaluate_case(case)
        self.assertEqual(bad["status"], "infeasible")


if __name__ == "__main__":
    unittest.main()
