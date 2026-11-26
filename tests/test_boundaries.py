import unittest

from transfer_approval import ApprovalInputError, evaluate_case


def pair_case(sender_interval, receiver_interval, max_delay, **window_overrides):
    sender_window = {"id": "sw", "start": 0, "end": 100, "key_group": "k"}
    receiver_window = {"id": "rw", "start": 0, "end": 100, "key_group": "k"}
    sender_window.update(window_overrides.get("sender", {}))
    receiver_window.update(window_overrides.get("receiver", {}))
    return {
        "max_delay": max_delay,
        "key_group": "k",
        "sender": {"trusted_interval": sender_interval, "windows": [sender_window]},
        "receiver": {"trusted_interval": receiver_interval, "windows": [receiver_window]},
        "transcript": {"validated": True},
    }


class BoundaryTests(unittest.TestCase):
    def test_max_delay_equality_is_allowed(self):
        result = evaluate_case(pair_case([0, 0], [10, 10], 10))
        self.assertEqual(result["status"], "certain")
        self.assertTrue(result["delay"]["guaranteed_within_limit"])
        self.assertEqual(result["delay"]["witness"]["delay"], 10)

    def test_one_unit_over_max_delay_is_rejected(self):
        result = evaluate_case(pair_case([0, 0], [11, 11], 10))
        self.assertEqual(result["status"], "infeasible")
        self.assertFalse(result["approved"])
        self.assertIn(
            "delivery_delay_impossible",
            [c["code"] for c in result["conflicts"]],
        )

    def test_zero_delay_requires_same_instant(self):
        ok = evaluate_case(pair_case([4, 4], [4, 4], 0))
        self.assertEqual(ok["status"], "certain")

        bad = evaluate_case(pair_case([4, 4], [5, 5], 0))
        self.assertEqual(bad["status"], "infeasible")

    def test_delay_boundary_narrows_projections_to_points(self):
        # S=[1,3], R=[7,9], D=4.  Only s=3 and r=7 can work.
        result = evaluate_case(pair_case([1, 3], [7, 9], 4))
        self.assertTrue(result["feasible"])
        self.assertTrue(result["can_hold"])
        self.assertFalse(result["approved"])
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["sender"]["feasible_intervals"], [{
            "start": 3,
            "end": 3,
            "start_inclusive": True,
            "end_inclusive": True,
        }])
        self.assertEqual(result["receiver"]["feasible_intervals"], [{
            "start": 7,
            "end": 7,
            "start_inclusive": True,
            "end_inclusive": True,
        }])

    def test_closed_window_start_boundary_is_accepted(self):
        case = pair_case([3, 3], [4, 4], 10)
        case["sender"]["windows"][0].update({"start": 3, "end": 8})
        result = evaluate_case(case)
        self.assertTrue(result["feasible"])

    def test_excluded_window_start_boundary_is_rejected(self):
        case = pair_case([3, 3], [4, 4], 10)
        case["sender"]["windows"][0].update(
            {"start": 3, "end": 8, "start_inclusive": False}
        )
        result = evaluate_case(case)
        self.assertEqual(result["status"], "infeasible")
        self.assertIn("no_accepted_key_window", [c["code"] for c in result["conflicts"]])

    def test_excluded_window_end_boundary_is_rejected_for_point(self):
        case = pair_case([8, 8], [9, 9], 10)
        case["sender"]["windows"][0].update(
            {"start": 3, "end": 8, "end_inclusive": False}
        )
        result = evaluate_case(case)
        self.assertEqual(result["status"], "infeasible")

    def test_negative_max_delay_is_invalid_input(self):
        with self.assertRaises(ApprovalInputError):
            evaluate_case(pair_case([0, 0], [0, 0], -1))

    def test_asymmetric_clock_error_bounds(self):
        result = evaluate_case({
            "max_delay": 10,
            "key_group": "k",
            "sender": {
                "observed_at": 5,
                "clock_error": {"earlier": 2, "later": 4},
                "windows": [{"start": 3, "end": 9, "key_group": "k"}],
            },
            "receiver": {
                "observed_at": 8,
                "clock_error": {"negative": 1, "positive": 2},
                "windows": [{"start": 7, "end": 10, "key_group": "k"}],
            },
        })
        self.assertEqual(result["sender"]["fused_interval"]["start"], 3)
        self.assertEqual(result["sender"]["fused_interval"]["end"], 9)
        self.assertEqual(result["receiver"]["fused_interval"]["start"], 7)
        self.assertEqual(result["receiver"]["fused_interval"]["end"], 10)
        self.assertEqual(result["status"], "ambiguous")

    def test_explicit_false_transcript_is_rejected_as_precondition(self):
        case = pair_case([0, 0], [0, 0], 1)
        case["transcript"]["validated"] = False
        from transfer_approval import TranscriptValidationError

        with self.assertRaises(TranscriptValidationError):
            evaluate_case(case)


if __name__ == "__main__":
    unittest.main()
