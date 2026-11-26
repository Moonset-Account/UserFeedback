import unittest

from transfer_approval import evaluate_case


class InterleavingEvidenceTests(unittest.TestCase):
    def test_overlapping_late_evidence_narrows_certain_core(self):
        case = {
            "max_delay": 20,
            "key_group": "g",
            "sender": {
                # Independent clock interval is broad enough not to widen the
                # intersection of commitment and late evidence.
                "trusted_interval": [0, 10],
                "windows": [{"id": "sw", "start": 4, "end": 7, "key_group": "g"}],
            },
            "receiver": {
                "trusted_interval": [8, 8],
                "windows": [{"id": "rw", "start": 0, "end": 20, "key_group": "g"}],
            },
            "transcript": {
                "validated": True,
                "commitments": [
                    {
                        "id": "send-c",
                        "subject": "send",
                        "key_group": "g",
                        "interval": [0, 6],
                    }
                ],
                "supplemental_evidence": [
                    {
                        "id": "late-ntp",
                        "source": "backup-ntp",
                        "commitment_id": "send-c",
                        "interval": [5, 10],
                    }
                ],
            },
        }
        result = evaluate_case(case)

        self.assertEqual(result["status"], "certain", result["conflicts"])
        sender = result["sender"]
        self.assertEqual(sender["fused_interval"]["start"], 5)
        self.assertEqual(sender["fused_interval"]["end"], 6)
        self.assertEqual(sender["feasible_intervals"], [{
            "start": 5,
            "end": 6,
            "start_inclusive": True,
            "end_inclusive": True,
        }])

        reasons = {item["reason"] for item in result["ambiguous_periods"]}
        self.assertIn("partial_source_coverage", reasons)
        partial = [
            item["interval"]
            for item in result["ambiguous_periods"]
            if item["reason"] == "partial_source_coverage"
            and item["subject"] == "send"
        ]
        self.assertIn({
            "start": 0,
            "end": 5,
            "start_inclusive": True,
            "end_inclusive": False,
        }, partial)
        self.assertIn({
            "start": 6,
            "end": 10,
            "start_inclusive": False,
            "end_inclusive": True,
        }, partial)

    def test_interleaved_send_receive_ranges_are_possible_but_not_guaranteed(self):
        case = {
            "max_delay": 2,
            "key_group": "g",
            "sender": {
                "trusted_interval": [0, 10],
                "windows": [{"id": "sw", "start": 0, "end": 10, "key_group": "g"}],
            },
            "receiver": {
                "trusted_interval": [0, 10],
                "windows": [{"id": "rw", "start": 0, "end": 10, "key_group": "g"}],
            },
        }
        result = evaluate_case(case)

        self.assertTrue(result["feasible"])
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result["certain"])
        self.assertFalse(result["delay"]["guaranteed_nonnegative"])
        self.assertEqual(result["sender"]["feasible_intervals"][0]["start"], 0)
        self.assertEqual(result["sender"]["feasible_intervals"][0]["end"], 10)
        self.assertEqual(result["receiver"]["feasible_intervals"][0]["start"], 0)
        self.assertEqual(result["receiver"]["feasible_intervals"][0]["end"], 10)

    def test_multiple_interleaved_windows_delay_filters_only_right_cluster(self):
        case = {
            "max_delay": 3,
            "key_group": "g",
            "sender": {
                "trusted_interval": [0, 10],
                "windows": [
                    {"id": "early", "start": 0, "end": 2, "key_group": "g"},
                    {"id": "late", "start": 8, "end": 10, "key_group": "g"},
                ],
            },
            "receiver": {
                "trusted_interval": [9, 9],
                "windows": [{"id": "rw", "start": 0, "end": 20, "key_group": "g"}],
            },
        }
        result = evaluate_case(case)

        self.assertTrue(result["feasible"])
        self.assertEqual(len(result["sender"]["accepted_intervals"]), 2)
        self.assertEqual(result["sender"]["feasible_intervals"], [{
            "start": 8,
            "end": 9,
            "start_inclusive": True,
            "end_inclusive": True,
        }])
        excluded = result["sender"]["delay_excluded_intervals"]
        self.assertIn({"start": 0, "end": 2, "start_inclusive": True, "end_inclusive": True}, excluded)
        self.assertIn({"start": 9, "end": 10, "start_inclusive": False, "end_inclusive": True}, excluded)

    def test_window_gap_is_reported_as_ambiguous_period(self):
        case = {
            "max_delay": 20,
            "key_group": "g",
            "sender": {
                "trusted_interval": [0, 10],
                "windows": [{"id": "sw", "start": 2, "end": 8, "key_group": "g"}],
            },
            "receiver": {
                "trusted_interval": [5, 5],
                "windows": [{"id": "rw", "start": 0, "end": 20, "key_group": "g"}],
            },
        }
        result = evaluate_case(case)
        self.assertEqual(result["status"], "ambiguous")
        outside = [
            item for item in result["ambiguous_periods"]
            if item["reason"] == "outside_announced_key_window"
        ]
        self.assertEqual(len(outside), 2)
        starts_ends = {(x["interval"]["start"], x["interval"]["end"]) for x in outside}
        self.assertEqual(starts_ends, {(0, 2), (8, 10)})


if __name__ == "__main__":
    unittest.main()
