from key_ceremony.solver import build_boundary, build_independent_subsets, transition_cost


def assert_result_invariants(testcase, result):
    """Check every promised structural, availability, independence, and handover rule."""
    spec = result.spec
    testcase.assertIsNotNone(spec)
    independent = build_independent_subsets(spec)

    testcase.assertEqual(len(result.rounds), len(spec.rounds))
    change_total = 0

    for scheduled in result.rounds:
        round_ = spec.rounds[scheduled.index]
        testcase.assertEqual(scheduled.round_id, round_.id)
        testcase.assertEqual(set(scheduled.assignments), set(round_.slot_ids))

        seen = set()
        for slot_id, person_id in scheduled.assignments.items():
            person = spec.person_by_id[person_id]
            slot = spec.slot_by_id[slot_id]
            testcase.assertTrue(
                spec.person_available(person, round_),
                f"{person_id} is unavailable for {round_.id}",
            )
            testcase.assertTrue(
                spec.slot_person_eligible(person, slot),
                f"{person_id} is ineligible for {slot_id}",
            )
            testcase.assertNotIn(person_id, seen, f"{person_id} is double-booked in {round_.id}")
            seen.add(person_id)

        for key_id, (slot_a, slot_b) in spec.key_slot_ids.items():
            if slot_a in round_.slot_index and slot_b in round_.slot_index:
                pa = spec.person_by_id[scheduled.assignments[slot_a]].index
                pb = spec.person_by_id[scheduled.assignments[slot_b]].index
                testcase.assertTrue(independent[(1 << pa) | (1 << pb)])
                testcase.assertIn(key_id, scheduled.enabled_keys)

    for transition in result.transitions:
        boundary = build_boundary(spec, transition.from_round_index, transition.to_round_index)
        old_round = spec.rounds[transition.from_round_index]
        new_round = spec.rounds[transition.to_round_index]
        old_values = tuple(
            spec.person_by_id[result.rounds[transition.from_round_index].assignments[sid]].index
            for sid in old_round.slot_ids
        )
        new_values = tuple(
            spec.person_by_id[result.rounds[transition.to_round_index].assignments[sid]].index
            for sid in new_round.slot_ids
        )
        cost = transition_cost(boundary, old_values, new_values, independent)
        testcase.assertIsNotNone(cost)
        testcase.assertEqual(transition.change_count, cost)
        testcase.assertEqual(transition.change_count, len(transition.changed_slots))
        change_total += cost

        for handover in transition.handovers:
            participant_mask = 0
            for pid in handover.participants:
                participant_mask |= 1 << spec.person_by_id[pid].index
            testcase.assertTrue(
                independent[participant_mask],
                f"handover for {handover.key_id} is not independent",
            )
            testcase.assertIn(handover.selected_old_holder, handover.old_holders)
            testcase.assertIn(handover.selected_old_holder, handover.participants)
            testcase.assertTrue(set(handover.new_holders).issubset(set(handover.participants)))

    if result.feasible:
        testcase.assertEqual(result.minimum_changes, change_total)
        testcase.assertEqual(
            result.total_handovers,
            sum(len(t.handovers) for t in result.transitions),
        )
        payload = result.to_dict()
        testcase.assertEqual(payload["minimum_changes"], result.minimum_changes)
        testcase.assertEqual(len(payload["rounds"]), len(result.rounds))
