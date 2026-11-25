import itertools
import unittest

from key_ceremony import CeremonyInputError, find_first_unreachable_round, schedule_ceremony
from key_ceremony.solver import (
    build_boundary,
    build_independent_subsets,
    enumerate_round_assignments,
    transition_cost,
)


def one_key_spec(round_availability=None, conflicts=None, people_extra=None):
    people = [
        {"id": "A", "management_line": "L1"},
        {"id": "B", "management_line": "L2"},
        {"id": "C", "management_line": "L3"},
        {"id": "D", "management_line": "L4"},
    ]
    if people_extra:
        people.extend(people_extra)
    if round_availability:
        for person in people:
            pid = person["id"]
            if pid in round_availability:
                person["available_rounds"] = round_availability[pid]
    spec = {
        "people": people,
        "slots": [
            {"id": "k:a", "key_id": "k", "seat": "a"},
            {"id": "k:b", "key_id": "k", "seat": "b"},
        ],
        "rounds": [
            {"id": "r1", "required_slots": ["k:a", "k:b"]},
            {"id": "r2", "required_slots": ["k:a", "k:b"]},
        ],
    }
    if conflicts:
        spec["proxy_pairs"] = conflicts
    return spec


def assert_result_invariants(testcase, result):
    """Validate the returned object against the parsed specification."""
    spec = result.spec
    testcase.assertIsNotNone(spec)
    independent = build_independent_subsets(spec)

    testcase.assertEqual(len(result.rounds), len(spec.rounds))
    used_change_total = 0
    for r_index, scheduled in enumerate(result.rounds):
        round_ = spec.rounds[r_index]
        testcase.assertEqual(scheduled.round_id, round_.id)
        testcase.assertEqual(set(scheduled.assignments), set(round_.slot_ids))

        seen_people = set()
        person_mask = 0
        for slot_id, pid in scheduled.assignments.items():
            person = spec.person_by_id[pid]
            slot = spec.slot_by_id[slot_id]
            testcase.assertTrue(spec.person_available(person, round_), f"{pid} unavailable in {round_.id}")
            testcase.assertTrue(spec.slot_person_eligible(person, slot), f"{pid} ineligible for {slot_id}")
            testcase.assertNotIn(pid, seen_people, f"{pid} holds two slots in {round_.id}")
            seen_people.add(pid)
            person_mask |= 1 << person.index

        for key_id, (sa, sb) in spec.key_slot_ids.items():
            if sa in round_.slot_index and sb in round_.slot_index:
                pa = spec.person_by_id[scheduled.assignments[sa]].index
                pb = spec.person_by_id[scheduled.assignments[sb]].index
                testcase.assertTrue(independent[(1 << pa) | (1 << pb)])
                testcase.assertIn(key_id, scheduled.enabled_keys)
            else:
                testcase.assertNotIn(key_id, scheduled.enabled_keys)

    for transition in result.transitions:
        boundary = build_boundary(spec, transition.from_round_index, transition.to_round_index)
        old_values = tuple(
            spec.person_by_id[result.rounds[transition.from_round_index].assignments[sid]].index
            for sid in spec.rounds[transition.from_round_index].slot_ids
        )
        new_values = tuple(
            spec.person_by_id[result.rounds[transition.to_round_index].assignments[sid]].index
            for sid in spec.rounds[transition.to_round_index].slot_ids
        )
        expected_cost = transition_cost(boundary, old_values, new_values, independent)
        testcase.assertIsNotNone(expected_cost)
        testcase.assertEqual(transition.change_count, expected_cost)
        testcase.assertEqual(
            transition.change_count,
            len(transition.changed_slots),
        )
        used_change_total += transition.change_count

        for handover in transition.handovers:
            mask = 0
            for pid in handover.participants:
                mask |= 1 << spec.person_by_id[pid].index
            testcase.assertTrue(independent[mask], "handover participants are not independent")
            testcase.assertIn(handover.selected_old_holder, handover.old_holders)
            testcase.assertTrue(
                set(handover.new_holders).issubset(set(handover.participants))
            )
            testcase.assertIn(handover.selected_old_holder, handover.participants)

    if result.feasible:
        testcase.assertEqual(result.minimum_changes, used_change_total)
        testcase.assertEqual(
            result.total_handovers,
            sum(len(t.handovers) for t in result.transitions),
        )


class CoreSchedulingTests(unittest.TestCase):
    def test_identical_rounds_have_zero_changes_and_enable_key(self):
        result = schedule_ceremony(one_key_spec())
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_changes, 0)
        self.assertEqual(result.rounds[0].assignments, {"k:a": "A", "k:b": "B"})
        self.assertEqual(result.rounds[1].assignments, {"k:a": "A", "k:b": "B"})
        self.assertEqual(result.rounds[0].enabled_keys, ("k",))
        self.assertEqual(result.total_handovers, 0)

    def test_one_seat_change_needs_no_handover(self):
        spec = one_key_spec(
            round_availability={
                "A": ["r1", "r2"],
                "B": ["r1"],
                "C": ["r2"],
                "D": ["r2"],
            }
        )
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_changes, 1)
        self.assertEqual(result.rounds[0].assignments["k:a"], "A")
        self.assertEqual(result.rounds[1].assignments["k:a"], "A")
        self.assertNotEqual(
            result.rounds[0].assignments["k:b"], result.rounds[1].assignments["k:b"]
        )
        self.assertEqual(result.transitions[0].handovers, ())

    def test_both_seats_change_creates_independent_handover(self):
        spec = one_key_spec(
            round_availability={
                "A": ["r1"],
                "B": ["r1"],
                "C": ["r2"],
                "D": ["r2"],
            }
        )
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_changes, 2)
        handovers = result.to_dict()["handovers"]
        self.assertEqual(len(handovers), 1)
        handover = handovers[0]
        self.assertEqual(handover["key_id"], "k")
        self.assertEqual(handover["old_holders"], ["A", "B"])
        self.assertEqual(handover["new_holders"], ["C", "D"])
        self.assertEqual(handover["selected_old_holder"], "A")
        self.assertEqual(handover["participants"], ["A", "C", "D"])

    def test_second_old_holder_selected_when_first_is_not_independent(self):
        spec = one_key_spec(
            round_availability={
                "A": ["r1"],
                "B": ["r1"],
                "C": ["r2"],
                "D": ["r2"],
            },
            conflicts=[["A", "C"]],
        )
        result = schedule_ceremony(spec)
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)
        handover = result.to_dict()["handovers"][0]
        self.assertEqual(handover["eligible_old_holders"], ["B"])
        self.assertEqual(handover["selected_old_holder"], "B")
        self.assertEqual(handover["participants"], ["B", "C", "D"])

    def test_standalone_rounds_but_no_handover_reports_first_unreachable(self):
        spec = one_key_spec(
            round_availability={
                "A": ["r1"],
                "B": ["r1"],
                "C": ["r2"],
                "D": ["r2"],
            },
            conflicts=[["A", "C"], ["B", "D"]],
        )
        result = schedule_ceremony(spec)
        self.assertFalse(result.feasible)
        self.assertIsNone(result.local_infeasible_round)
        diagnosis = result.first_unreachable_round
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["index"], 1)
        self.assertEqual(diagnosis["round_id"], "r2")
        self.assertTrue(diagnosis["locally_assignable"])
        self.assertGreater(diagnosis["legal_assignment_count"], 0)
        alias_diagnosis = find_first_unreachable_round(spec)
        self.assertEqual(alias_diagnosis["index"], 1)

        # Both rounds really do have standalone legal assignments.
        standalone_counts = [
            len(enumerate_round_assignments(result.spec, round_))
            for round_ in result.spec.rounds
        ]
        self.assertEqual(standalone_counts, [2, 2])

    def test_local_independence_failure_is_distinguished_from_unreachable(self):
        spec = one_key_spec()
        # Only A and C are available in r2 and they share management line L1.
        for person in spec["people"]:
            if person["id"] == "C":
                person["management_line"] = "L1"
            if person["id"] in {"A", "C"}:
                person["available_rounds"] = ["r2"]
            else:
                person["available_rounds"] = ["r1"]
        result = schedule_ceremony(spec)
        self.assertFalse(result.feasible)
        self.assertIsNone(result.first_unreachable_round)
        self.assertEqual(result.local_infeasible_round["index"], 1)
        self.assertEqual(result.local_infeasible_round["round_id"], "r2")

    def test_person_cannot_hold_two_slots_in_one_round(self):
        spec = {
            "people": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
            "slots": ["s1", "s2", "s3", "s4"],
            "rounds": [{"id": "r1", "required_slots": ["s1", "s2", "s3", "s4"]}],
        }
        result = schedule_ceremony(spec)
        self.assertFalse(result.feasible)
        self.assertEqual(result.local_infeasible_round["index"], 0)

    def test_duplicate_person_and_unknown_slot_are_input_errors(self):
        bad_duplicate = {"people": [{"id": "A"}, {"id": "A"}], "rounds": []}
        with self.assertRaises(CeremonyInputError):
            schedule_ceremony(bad_duplicate)

        bad_slot = {
            "people": [{"id": "A"}],
            "slots": [{"id": "declared"}],
            "rounds": [{"required_slots": ["missing"]}],
        }
        with self.assertRaises(CeremonyInputError):
            schedule_ceremony(bad_slot)

    def test_chinese_substitute_relation_alias_creates_independence_conflict(self):
        spec = {
            "people": [{"id": "A"}, {"id": "B"}],
            "替班关系": [["A", "B"]],
            "slots": [
                {"id": "k:a", "key_id": "k"},
                {"id": "k:b", "key_id": "k"},
            ],
            "rounds": [{"id": "r1", "required_slots": ["k:a", "k:b"]}],
        }
        result = schedule_ceremony(spec)
        self.assertFalse(result.feasible)
        self.assertIsNotNone(result.local_infeasible_round)
        self.assertEqual(result.conflict_pairs, (("A", "B"),))

    def test_more_than_twelve_people_rejected(self):
        spec = {"people": [{"id": f"p{i}"} for i in range(13)], "rounds": []}
        with self.assertRaises(CeremonyInputError):
            schedule_ceremony(spec)


class BruteForceOptimalityTests(unittest.TestCase):
    def test_dp_matches_exhaustive_sequences_on_small_randomish_specs(self):
        # Four people, one key, three availability-constrained rounds.
        spec = {
            "people": [
                {"id": "A", "line": "L1", "available_rounds": ["r1", "r2"]},
                {"id": "B", "line": "L2", "available_rounds": ["r1", "r3"]},
                {"id": "C", "line": "L2", "available_rounds": ["r2", "r3"]},
                {"id": "D", "line": "L3"},
                {"id": "E", "line": "L4", "available_rounds": ["r1", "r2"]},
            ],
            "proxy_pairs": [["D", "E"]],
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
        assert_result_invariants(self, result)
        self.assertTrue(result.feasible)

        parsed = result.spec
        independent = build_independent_subsets(parsed)
        options = [enumerate_round_assignments(parsed, r) for r in parsed.rounds]
        boundaries = [build_boundary(parsed, i, i + 1) for i in range(2)]

        brute_best = None
        for sequence in itertools.product(*options):
            total = 0
            ok = True
            for i, boundary in enumerate(boundaries):
                cost = transition_cost(
                    boundary, sequence[i], sequence[i + 1], independent
                )
                if cost is None:
                    ok = False
                    break
                total += cost
            if ok and (brute_best is None or total < brute_best):
                brute_best = total
        self.assertIsNotNone(brute_best)
        self.assertEqual(result.minimum_changes, brute_best)


if __name__ == "__main__":
    unittest.main()
