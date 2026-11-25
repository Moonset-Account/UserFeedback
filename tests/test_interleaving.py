import time
import unittest

from ceremony_helpers import assert_result_invariants
from key_ceremony import schedule_ceremony


def interleaved_spec():
    return {
        "people": [
            {"id": "A", "eligible_slots": ["k1:a", "k1:b", "k2:a", "k2:b"],
             "available_rounds": ["r1", "r2"]},
            {"id": "B", "eligible_slots": ["k1:a", "k1:b"], "available_rounds": ["r1"]},
            {"id": "C", "eligible_slots": ["k1:a", "k1:b"], "available_rounds": ["r2"]},
            {"id": "D", "eligible_slots": ["k1:a", "k1:b"], "available_rounds": ["r2"]},
            {"id": "E", "eligible_slots": ["k2:a", "k2:b"], "available_rounds": ["r1"]},
            {"id": "F", "eligible_slots": ["k2:a", "k2:b"], "available_rounds": ["r1"]},
            {"id": "G", "eligible_slots": ["k2:a", "k2:b"], "available_rounds": ["r2"]},
        ],
        "slots": [
            {"id": "k1:a", "key_id": "k1", "seat": "a"},
            {"id": "k1:b", "key_id": "k1", "seat": "b"},
            {"id": "k2:a", "key_id": "k2", "seat": "a"},
            {"id": "k2:b", "key_id": "k2", "seat": "b"},
        ],
        "rounds": [
            {"id": "r1", "required_slots": ["k1:a", "k1:b", "k2:a", "k2:b"]},
            {"id": "r2", "required_slots": ["k1:a", "k1:b", "k2:a", "k2:b"]},
        ],
    }


class InterleavingTests(unittest.TestCase):
    def test_two_keys_have_simultaneous_interlocking_handovers(self):
        result = schedule_ceremony(interleaved_spec())
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible, result.first_unreachable_round)
        self.assertEqual(result.minimum_changes, 4)

        first = result.rounds[0].assignments
        second = result.rounds[1].assignments
        self.assertEqual((first["k1:a"], first["k1:b"]), ("A", "B"))
        self.assertEqual((first["k2:a"], first["k2:b"]), ("E", "F"))
        self.assertEqual((second["k1:a"], second["k1:b"]), ("C", "D"))
        self.assertEqual((second["k2:a"], second["k2:b"]), ("A", "G"))
        self.assertEqual(result.rounds[0].enabled_keys, ("k1", "k2"))
        self.assertEqual(result.rounds[1].enabled_keys, ("k1", "k2"))

        handovers = result.to_dict()["handovers"]
        self.assertEqual({h["key_id"] for h in handovers}, {"k1", "k2"})
        by_key = {h["key_id"]: h for h in handovers}
        self.assertEqual(by_key["k1"]["participants"], ["A", "C", "D"])
        self.assertEqual(by_key["k2"]["participants"], ["E", "A", "G"])

        # A is the outgoing k1 custodian and incoming k2 custodian.  The
        # ceremony allows that interlock because A holds only one slot in each
        # round and each per-key handover remains pairwise independent.
        self.assertIn("A", by_key["k1"]["participants"])
        self.assertIn("A", by_key["k2"]["new_holders"])

    def test_conflicting_custodians_on_different_keys_are_allowed(self):
        spec = interleaved_spec()
        # A and F are proxies, but in r1 A holds k1 and F holds k2.  The
        # conflict rule is scoped to dual custody of the same key/handovers.
        spec["proxy_pairs"] = [["A", "F"]]
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        # The k2 handover cannot use F as old participant with A present, but E can.
        k2 = next(h for h in result.to_dict()["handovers"] if h["key_id"] == "k2")
        self.assertEqual(k2["selected_old_holder"], "E")
        self.assertEqual(k2["eligible_old_holders"], ["E"])

    def test_twelve_people_four_slots_boundary_is_fast_and_exact(self):
        people = [{"id": f"p{i:02d}"} for i in range(12)]
        spec = {
            "people": people,
            "slots": [
                {"id": f"k{key}:{seat}", "key_id": f"k{key}", "seat": seat}
                for key in (1, 2)
                for seat in ("a", "b")
            ],
            "rounds": [
                {"id": "r1", "required_slots": ["k1:a", "k1:b", "k2:a", "k2:b"]},
                {"id": "r2", "required_slots": ["k1:a", "k1:b", "k2:a", "k2:b"]},
            ],
        }
        start = time.perf_counter()
        result = schedule_ceremony(spec)
        elapsed = time.perf_counter() - start
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_changes, 0)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
