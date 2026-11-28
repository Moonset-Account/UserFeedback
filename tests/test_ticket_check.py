import unittest

from ticket_check import (
    Check,
    Erratum,
    FoldScheme,
    Observation,
    TicketError,
    analyze_ticket,
    count_solutions_by_enumeration,
    enumerate_solutions,
)


def obs(ticket, position, value):
    return Observation(ticket=ticket, position=position, value=value)


class FoldSchemeTests(unittest.TestCase):
    def test_opposing_fold_is_a_mirror(self):
        scheme = FoldScheme.opposing(4)
        self.assertEqual(scheme.cell_at("window", 0), 0)
        self.assertEqual(scheme.cell_at("stub", 0), 3)
        self.assertEqual(scheme.cell_at("stub", 3), 0)
        self.assertEqual(scheme.positions("stub"), 4)

    def test_from_permutation_maps_stub_positions_to_window_cells(self):
        # Stub position i shows the window position permutation[i].
        scheme = FoldScheme.from_permutation([2, 0, 3, 1])
        self.assertEqual(scheme.folds["window"], (0, 1, 2, 3))
        self.assertEqual(scheme.folds["stub"], (2, 0, 3, 1))
        self.assertEqual(scheme.cell_at("stub", 0), 2)

    def test_from_permutation_rejects_non_permutation(self):
        with self.assertRaises(TicketError):
            FoldScheme.from_permutation([1, 1, 0])
        with self.assertRaises(TicketError):
            FoldScheme.from_permutation([0, 2])

    def test_out_of_range_cell_is_rejected(self):
        with self.assertRaises(TicketError):
            FoldScheme(n_cells=3, folds={"window": (0, 1, 3), "stub": (2, 1, 0)})

    def test_unknown_ticket_name_is_rejected(self):
        scheme = FoldScheme.opposing(3)
        with self.assertRaises(TicketError):
            scheme.cell_at("margin", 0)

    def test_window_can_show_subset_of_cells(self):
        scheme = FoldScheme.from_permutation([1, 0], window=[4, 5])
        self.assertEqual(scheme.n_cells, 6)
        self.assertEqual(scheme.folds["window"], (4, 5))
        self.assertEqual(scheme.folds["stub"], (5, 4))
        self.assertEqual(scheme.cell_at("stub", 0), 5)


class ConsistencyTests(unittest.TestCase):
    def test_fully_observed_consistent_ticket_has_one_solution(self):
        scheme = FoldScheme.opposing(3)
        observations = [
            obs("window", 0, 1), obs("window", 1, 0), obs("window", 2, 1),
            obs("stub", 0, 1), obs("stub", 1, 0), obs("stub", 2, 1),
        ]
        result = analyze_ticket(scheme, [], observations)
        self.assertTrue(result.compatible)
        self.assertEqual(result.solution_count, 1)
        self.assertEqual(result.free_variables, 0)
        self.assertEqual(dict(result.observed_cells), {0: 1, 1: 0, 2: 1})
        self.assertEqual(dict(result.determined_unobserved), {})
        self.assertEqual(result.undetermined_unobserved, ())

    def test_two_parts_disagree_on_same_cell(self):
        scheme = FoldScheme.opposing(3)
        observations = [obs("window", 0, 0), obs("stub", 2, 1)]
        result = analyze_ticket(scheme, [], observations)
        self.assertFalse(result.compatible)
        self.assertEqual(result.contradiction.reason, "value_conflict")
        cells = {item.cells[0] for item in result.contradiction.items}
        self.assertEqual(cells, {0})
        self.assertIn("window[0]", result.contradiction.describe())
        self.assertIn("stub[2]", result.contradiction.describe())

    def test_unobserved_cell_is_fixed_by_a_parity_check(self):
        scheme = FoldScheme.opposing(4)
        # Observe cells 0..2 entirely; only cell 3 is unobserved.
        observations = [
            obs("window", 0, 1), obs("window", 1, 1), obs("window", 2, 0),
        ]
        checks = [Check(cells=(0, 1, 2, 3), parity=1, label="row")]
        result = analyze_ticket(scheme, checks, observations)
        self.assertTrue(result.compatible)
        self.assertEqual(dict(result.determined_unobserved), {3: 1})
        self.assertEqual(result.undetermined_unobserved, ())
        self.assertEqual(result.solution_count, 1)

    def test_unknown_observation_value_is_an_erasure(self):
        scheme = FoldScheme.opposing(3)
        observations = [obs("window", 0, None), obs("window", 1, 1)]
        result = analyze_ticket(scheme, [], observations)
        self.assertTrue(result.compatible)
        self.assertEqual(result.observed_cells, {1: 1})
        self.assertEqual(result.free_variables, 2)
        self.assertEqual(result.solution_count, 4)
        self.assertEqual(set(result.undetermined_unobserved), {0, 2})

    def test_parity_check_contradiction_has_a_certificate(self):
        scheme = FoldScheme.opposing(3)
        observations = [obs("window", 0, 1), obs("window", 1, 1)]
        checks = [Check(cells=(0, 1), parity=1, label="pair")]
        result = analyze_ticket(scheme, checks, observations)
        self.assertFalse(result.compatible)
        contradiction = result.contradiction
        self.assertEqual(contradiction.reason, "parity_conflict")
        self.assertEqual(contradiction.xor_rhs, 1)
        lhs = 0
        rhs = 0
        for item in contradiction.items:
            for cell in item.cells:
                lhs ^= 1 << cell
            rhs ^= item.value
        self.assertEqual(lhs, 0)
        self.assertEqual(rhs, 1)
        labels = {item.label for item in contradiction.items}
        self.assertEqual(labels, {"window[0]", "window[1]", "pair"})

    def test_empty_check_cell_list_is_allowed_only_with_matching_parity(self):
        scheme = FoldScheme.opposing(2)
        consistent = analyze_ticket(scheme, [Check(cells=(), parity=0)], [])
        self.assertTrue(consistent.compatible)
        broken = analyze_ticket(scheme, [Check(cells=(), parity=1)], [])
        self.assertFalse(broken.compatible)
        self.assertEqual(broken.contradiction.reason, "parity_conflict")

    def test_large_ticket_with_hundreds_of_unknown_cells(self):
        n = 300
        scheme = FoldScheme.opposing(n)
        observations = [obs("window", 0, 1), obs("stub", n - 1, 1)]
        result = analyze_ticket(scheme, [], observations)
        self.assertTrue(result.compatible)
        self.assertEqual(result.observed_cells, {0: 1})
        self.assertEqual(result.free_variables, n - 1)
        self.assertEqual(result.solution_count, 2 ** (n - 1))
        self.assertEqual(len(result.undetermined_unobserved), n - 1)

    def test_duplicate_observation_is_rejected(self):
        scheme = FoldScheme.opposing(2)
        with self.assertRaises(TicketError):
            analyze_ticket(scheme, [], [obs("window", 0, 0), obs("window", 0, 1)])

    def test_observation_outside_part_is_rejected(self):
        scheme = FoldScheme.opposing(2)
        with self.assertRaises(TicketError):
            analyze_ticket(scheme, [], [obs("window", 5, 0)])

    def test_bool_values_are_rejected(self):
        with self.assertRaises(TicketError):
            Observation(ticket="window", position=0, value=True)
        with self.assertRaises(TicketError):
            Check(cells=(0,), parity=False)

    def test_check_duplicate_cells_are_rejected(self):
        scheme = FoldScheme.opposing(2)
        with self.assertRaises(TicketError):
            analyze_ticket(scheme, [Check(cells=(0, 0), parity=0)], [])
        with self.assertRaises(TicketError):
            analyze_ticket(scheme, [Check(cells=(0, 5), parity=0)], [])


class ErratumTests(unittest.TestCase):
    def test_erratum_resolves_a_disagreement_and_keeps_the_original(self):
        scheme = FoldScheme.opposing(3)
        observations = [obs("window", 0, 0), obs("stub", 2, 1)]
        broken = analyze_ticket(scheme, [], observations)
        self.assertFalse(broken.compatible)

        fixed = analyze_ticket(
            scheme, [], observations,
            [Erratum(ticket="stub", position=2, value=0, note="misread digit")],
        )
        self.assertTrue(fixed.compatible)
        self.assertEqual(fixed.solution_count, 1 << 2)

        # The original record is intact.
        records = {r.erratum.ticket + str(r.erratum.position): r for r in fixed.erratum_records}
        record = records["stub2"]
        self.assertTrue(record.had_original)
        self.assertTrue(record.replaced)
        self.assertEqual(record.original, 1)
        self.assertEqual(record.previous, 1)
        self.assertEqual(record.erratum.value, 0)
        self.assertEqual(record.erratum.note, "misread digit")

        effective = {(e.ticket, e.position): e for e in fixed.effective_observations}
        stub_two = effective[("stub", 2)]
        self.assertEqual(stub_two.original, 1)
        self.assertEqual(stub_two.effective, 0)
        self.assertTrue(stub_two.amended)

    def test_erratum_with_none_turns_observation_into_erasure(self):
        scheme = FoldScheme.opposing(2)
        observations = [obs("window", 0, 1), obs("window", 1, 1)]
        result = analyze_ticket(
            scheme, [], observations,
            [Erratum(ticket="window", position=0, value=None)],
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.observed_cells, {1: 1})
        # Erasing window[0] removes the only observation of cell 0, so it becomes
        # free again; nothing constrains it any more.
        self.assertEqual(dict(result.determined_unobserved), {})
        self.assertEqual(result.undetermined_unobserved, (0,))
        self.assertEqual(result.solution_count, 2)
        effective = {(e.ticket, e.position): e for e in result.effective_observations}
        erased = effective[("window", 0)]
        self.assertEqual(erased.original, 1)
        self.assertIsNone(erased.effective)
        self.assertTrue(erased.amended)

    def test_erratum_can_introduce_a_fresh_observation(self):
        scheme = FoldScheme.opposing(2)
        result = analyze_ticket(
            scheme, [], [],
            [Erratum(ticket="stub", position=0, value=1)],  # cell 1
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.observed_cells, {1: 1})
        record = result.erratum_records[0]
        self.assertFalse(record.had_original)
        self.assertFalse(record.replaced)
        self.assertIsNone(record.previous)

    def test_errata_apply_in_order(self):
        scheme = FoldScheme.opposing(2)
        observations = [obs("window", 0, 0)]
        result = analyze_ticket(
            scheme, [], observations,
            [
                Erratum(ticket="window", position=0, value=1),
                Erratum(ticket="window", position=0, value=0),
            ],
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.observed_cells, {0: 0})
        first, second = result.erratum_records
        self.assertEqual(first.previous, 0)
        self.assertEqual(first.original, 0)
        self.assertEqual(second.previous, 1)
        self.assertEqual(second.original, 0)
        self.assertEqual(second.erratum.value, 0)

    def test_erratum_can_also_create_a_contradiction(self):
        scheme = FoldScheme.opposing(2)
        observations = [obs("window", 0, 0), obs("stub", 1, 0)]  # both show cell 0
        result = analyze_ticket(
            scheme, [], observations,
            [Erratum(ticket="stub", position=1, value=1)],
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.contradiction.reason, "value_conflict")


class EnumerationCrossCheckTests(unittest.TestCase):
    """Every analytic answer is compared against brute force on small tickets."""

    def _brute_force_summary(self, scheme, checks, observations, errata=()):
        solutions = list(enumerate_solutions(scheme, checks, observations, errata, max_cells=12))
        determined = {}
        undetermined = []
        for cell in range(scheme.n_cells):
            observed = False
            for observation in observations:
                if scheme.cell_at(observation.ticket, observation.position) == cell:
                    if observation.value is not None:
                        observed = True
            for erratum in errata:
                if scheme.cell_at(erratum.ticket, erratum.position) == cell:
                    if erratum.value is not None:
                        observed = True
            if observed:
                continue
            values = {solution[cell] for solution in solutions}
            if len(values) == 1:
                determined[cell] = values.pop()
            else:
                undetermined.append(cell)
        return len(solutions), determined, tuple(undetermined)

    def test_hand_written_small_case(self):
        # Two observations plus two checks that pin both unobserved cells:
        #   c0 xor c1 xor c2 = 0  with c0=1, c1=0  =>  c2 = 1
        #   c1 xor c2 xor c3 = 1  with c1=0, c2=1  =>  c3 = 0
        scheme = FoldScheme.opposing(4)
        observations = [obs("window", 0, 1), obs("window", 1, 0)]
        checks = [
            Check(cells=(0, 1, 2), parity=0, label="a"),
            Check(cells=(1, 2, 3), parity=1, label="b"),
        ]
        result = analyze_ticket(scheme, checks, observations)
        count, determined, undetermined = self._brute_force_summary(scheme, checks, observations)
        self.assertTrue(result.compatible)
        self.assertEqual(result.solution_count, 1)
        self.assertEqual(result.solution_count, count)
        self.assertEqual(dict(result.determined_unobserved), {2: 1, 3: 0})
        self.assertEqual(dict(result.determined_unobserved), determined)
        self.assertEqual(result.undetermined_unobserved, undetermined)
        self.assertEqual(result.undetermined_unobserved, ())

    def test_many_generated_systems_agree_with_enumeration(self):
        # Small deterministic sweep: every combination of observed values on a
        # fixed folding, with several parity check sets.
        scheme = FoldScheme.opposing(4)
        check_sets = [
            [],
            [Check(cells=(0, 1), parity=0)],
            [Check(cells=(0, 1, 2, 3), parity=0), Check(cells=(1, 2), parity=1)],
            [Check(cells=(0, 3), parity=1), Check(cells=(1,), parity=0)],
        ]
        observation_sets = [
            [],
            [obs("window", 0, 0)],
            [obs("window", 0, 1), obs("window", 1, 1)],
            [obs("window", 0, 1), obs("stub", 3, 0)],  # conflict on cell 0
            [obs("window", 1, 0), obs("window", 2, 1), obs("window", 3, 0)],
            [obs("stub", 0, 1), obs("stub", 1, 1), obs("stub", 2, 0)],
        ]
        for checks in check_sets:
            for observations in observation_sets:
                with self.subTest(checks=checks, observations=observations):
                    result = analyze_ticket(scheme, checks, observations)
                    count, determined, undetermined = self._brute_force_summary(
                        scheme, checks, observations
                    )
                    self.assertEqual(result.compatible, count > 0)
                    if result.compatible:
                        self.assertEqual(result.solution_count, count)
                        self.assertEqual(dict(result.determined_unobserved), determined)
                        self.assertEqual(result.undetermined_unobserved, undetermined)

    def test_erratum_case_agrees_with_enumeration(self):
        scheme = FoldScheme.opposing(4)
        observations = [obs("window", 0, 1), obs("stub", 3, 0), obs("window", 2, 1)]
        errata = [Erratum(ticket="stub", position=3, value=1)]
        checks = [Check(cells=(2, 3), parity=0)]
        result = analyze_ticket(scheme, checks, observations, errata)
        count, determined, undetermined = self._brute_force_summary(
            scheme, checks, observations, errata
        )
        self.assertTrue(result.compatible)
        self.assertEqual(result.solution_count, count)
        self.assertEqual(dict(result.determined_unobserved), determined)
        self.assertEqual(result.undetermined_unobserved, undetermined)

    def test_count_solutions_by_enumeration_matches_analysis(self):
        scheme = FoldScheme.opposing(5)
        checks = [Check(cells=(0, 2, 4), parity=1), Check(cells=(1, 3), parity=1)]
        observations = [obs("window", 0, 1)]
        result = analyze_ticket(scheme, checks, observations)
        brute = count_solutions_by_enumeration(scheme, checks, observations, max_cells=12)
        self.assertEqual(result.solution_count, brute)

    def test_enumeration_refuses_large_tickets(self):
        scheme = FoldScheme.opposing(25)
        with self.assertRaises(TicketError):
            count_solutions_by_enumeration(scheme, [], [], max_cells=20)


if __name__ == "__main__":
    unittest.main()
