import unittest

from ceremony_helpers import assert_result_invariants
from key_ceremony import schedule_ceremony
from key_ceremony.model import load_spec


class AvailabilityAndBoundaryTests(unittest.TestCase):
    def test_numeric_availability_windows_are_enforced(self):
        spec = {
            "people": [
                {"id": "A", "availability": [[0, 10]]},
                {"id": "B", "availability": [[0, 10]]},
                {"id": "C", "availability": [[10, 20]]},
                {"id": "D", "availability": [[10, 20]]},
            ],
            "slots": [{"id": "k:a", "key_id": "k"}, {"id": "k:b", "key_id": "k"}],
            "rounds": [
                {"id": "morning", "time": 9, "required_slots": ["k:a", "k:b"]},
                {"id": "evening", "time": 15, "required_slots": ["k:a", "k:b"]},
            ],
        }
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(set(result.rounds[0].assignments.values()), {"A", "B"})
        self.assertEqual(set(result.rounds[1].assignments.values()), {"C", "D"})

    def test_integer_available_rounds_are_one_based(self):
        spec = load_spec(
            {
                "people": [{"id": "A", "available_rounds": 1}],
                "rounds": [{"id": "first", "slots": []}, {"id": "second", "slots": []}],
            }
        )
        person = spec.person_by_id["A"]
        self.assertTrue(spec.person_available(person, spec.rounds[0]))
        self.assertFalse(spec.person_available(person, spec.rounds[1]))

    def test_closed_window_endpoints_and_iso_times_are_supported(self):
        spec = load_spec(
            {
                "people": [
                    {
                        "id": "A",
                        "availability": [["2026-01-01T09:00:00", "2026-01-01T17:00:00"]],
                    }
                ],
                "rounds": [
                    {"id": "endpoint", "time": "2026-01-01T17:00:00", "slots": []},
                    {"id": "after", "time": "2026-01-01T17:00:01", "slots": []},
                ],
            }
        )
        endpoint_round, after_round = spec.rounds
        self.assertTrue(spec.person_available(spec.person_by_id["A"], endpoint_round))
        self.assertFalse(spec.person_available(spec.person_by_id["A"], after_round))

    def test_slot_eligibility_is_enforced(self):
        spec = {
            "people": [
                {"id": "A", "eligible_slots": ["k:a"]},
                {"id": "B", "eligible_slots": ["k:b"]},
                {"id": "C", "eligible_slots": ["k:b"]},
            ],
            "slots": [
                {"id": "k:a", "key_id": "k"},
                {"id": "k:b", "key_id": "k"},
            ],
            "rounds": [{"id": "r1", "slots": ["k:a", "k:b"]}],
        }
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(result.rounds[0].assignments["k:a"], "A")
        self.assertIn(result.rounds[0].assignments["k:b"], {"B", "C"})

    def test_missing_one_seat_means_key_is_not_enabled(self):
        spec = {
            "people": [{"id": "A"}, {"id": "B"}],
            "slots": [
                {"id": "k:a", "key_id": "k", "seat": "a"},
                {"id": "k:b", "key_id": "k", "seat": "b"},
            ],
            "rounds": [
                {"id": "both", "slots": ["k:a", "k:b"]},
                {"id": "one", "slots": ["k:a"]},
            ],
        }
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(result.rounds[0].enabled_keys, ("k",))
        self.assertEqual(result.rounds[1].enabled_keys, ())
        transition = result.to_dict()["transitions"][0]
        self.assertEqual(transition["removed_slots"], ["k:b"])
        self.assertEqual(transition["change_count"], 0)

    def test_empty_round_list_is_trivially_feasible(self):
        result = schedule_ceremony({"people": [], "rounds": []})
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_changes, 0)
        self.assertEqual(result.to_dict()["rounds"], [])

    def test_first_unreachable_can_be_after_an_intermediate_reachable_round(self):
        spec = {
            "people": [
                {"id": "A", "available_rounds": ["r1"]},
                {"id": "B", "available_rounds": ["r1"]},
                {"id": "C", "available_rounds": ["r2"]},
                {"id": "D", "available_rounds": ["r2"]},
                {"id": "E", "available_rounds": ["r3"]},
                {"id": "F", "available_rounds": ["r3"]},
            ],
            # Complete old-to-new conflict graph at r2 -> r3 blocks every handover.
            "proxy_pairs": [
                ["C", "E"], ["C", "F"], ["D", "E"], ["D", "F"]
            ],
            "slots": [
                {"id": "k:a", "key_id": "k", "seat": "a"},
                {"id": "k:b", "key_id": "k", "seat": "b"},
            ],
            "rounds": [
                {"id": "r1", "slots": ["k:a", "k:b"]},
                {"id": "r2", "slots": ["k:a", "k:b"]},
                {"id": "r3", "slots": ["k:a", "k:b"]},
            ],
        }
        result = schedule_ceremony(spec)
        self.assertFalse(result.feasible)
        self.assertIsNone(result.local_infeasible_round)
        diagnosis = result.first_unreachable_round
        self.assertEqual(diagnosis["index"], 2)
        self.assertEqual(diagnosis["round_id"], "r3")
        self.assertEqual(diagnosis["after_round_id"], "r2")
        self.assertTrue(diagnosis["locally_assignable"])
        self.assertGreater(diagnosis["legal_assignment_count"], 0)


if __name__ == "__main__":
    unittest.main()
