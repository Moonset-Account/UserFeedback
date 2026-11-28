"""Tests for contradiction certificates, randomized cross-checks, and scale.

The randomized tests compare the analyser against exhaustive enumeration on
small tickets and against an independent GF(2) solver in ``tests/reference.py``,
so the production elimination code is never checked only against itself.
"""

import random
import unittest

from ticket_check import (
    Check,
    Erratum,
    FoldScheme,
    Observation,
    analyze_ticket,
    enumerate_solutions,
)

try:  # works under `discover -s tests` and under `discover -t .`
    from .reference import forced_and_free, is_satisfiable, solve
except ImportError:  # pragma: no cover - depends on invocation style
    from reference import forced_and_free, is_satisfiable, solve


def obs(ticket, position, value):
    return Observation(ticket=ticket, position=position, value=value)


def equations_from(scheme, checks, observations, errata=()):
    """Effective equations plus the set of cells that were observed."""
    effective = {}
    cell_of = {}
    for observation in observations:
        key = (observation.ticket, observation.position)
        cell = scheme.cell_at(*key)
        cell_of[key] = cell
        effective[key] = observation.value
    for erratum in errata:
        key = (erratum.ticket, erratum.position)
        cell_of[key] = scheme.cell_at(*key)
        effective[key] = erratum.value
    equations = []
    observed_cells = set()
    for key, value in effective.items():
        if value is not None:
            equations.append(((cell_of[key],), value))
            observed_cells.add(cell_of[key])
    for check in checks:
        equations.append((tuple(check.cells), check.parity))
    return equations, observed_cells


# ---------------------------------------------------------------------------
# certificate properties
# ---------------------------------------------------------------------------


class CertificateTests(unittest.TestCase):
    def assert_certificate_irredundant(self, result, n_cells):
        contradiction = result.contradiction
        self.assertIsNotNone(contradiction)
        items = contradiction.items
        self.assertGreaterEqual(len(items), 1)
        # The reported invariant: XOR(left-hand sides) is empty, XOR(rhs) = 1.
        lhs = 0
        rhs = 0
        for item in items:
            for cell in item.cells:
                lhs ^= 1 << cell
            rhs ^= item.value
        self.assertEqual(lhs, 0)
        self.assertEqual(rhs, 1)
        self.assertEqual(contradiction.xor_rhs, 1)
        # The items really are mutually contradictory...
        self.assertFalse(is_satisfiable(n_cells, items))
        # ...and every proper subset is satisfiable (irredundancy).
        for index in range(len(items)):
            subset = items[:index] + items[index + 1 :]
            self.assertTrue(
                is_satisfiable(n_cells, subset),
                f"removing {items[index].label} still leaves a contradiction",
            )

    def test_value_conflict_certificate_is_irredundant(self):
        scheme = FoldScheme.opposing(3)
        observations = [obs("window", 0, 0), obs("stub", 2, 1)]
        result = analyze_ticket(scheme, [], observations)
        self.assertFalse(result.compatible)
        self.assertEqual(result.contradiction.reason, "value_conflict")
        self.assertEqual(len(result.contradiction.items), 2)
        self.assert_certificate_irredundant(result, scheme.n_cells)

    def test_parity_conflict_certificate_is_irredundant(self):
        scheme = FoldScheme.opposing(4)
        observations = [obs("window", 0, 1), obs("window", 1, 1), obs("window", 2, 0)]
        checks = [
            Check(cells=(0, 1, 2), parity=1, label="row"),
            Check(cells=(0, 1), parity=0, label="pair"),
        ]
        result = analyze_ticket(scheme, checks, observations)
        self.assertFalse(result.compatible)
        self.assertEqual(result.contradiction.reason, "parity_conflict")
        self.assert_certificate_irredundant(result, scheme.n_cells)

    def test_larger_cycle_certificate_is_irredundant(self):
        scheme = FoldScheme.opposing(6)
        checks = [
            Check(cells=(0, 1, 2, 3), parity=1, label="wide"),
            Check(cells=(0, 1), parity=1, label="left"),
            Check(cells=(2, 3), parity=1, label="right"),
        ]
        result = analyze_ticket(scheme, checks, [])
        self.assertFalse(result.compatible)
        self.assert_certificate_irredundant(result, scheme.n_cells)
        # The wide check plus both narrow checks is the only circuit here.
        self.assertEqual(len(result.contradiction.items), 3)

    def test_contradiction_description_lists_every_item(self):
        scheme = FoldScheme.opposing(3)
        result = analyze_ticket(
            scheme,
            [Check(cells=(0, 1), parity=1, label="pair")],
            [obs("window", 0, 0), obs("window", 1, 0)],
        )
        text = result.describe()
        self.assertIn("cannot come from one original string", text)
        self.assertIn("pair", text)
        self.assertIn("window[0]", text)
        self.assertIn("window[1]", text)

    def test_randomized_certificates_are_always_irredundant(self):
        rng = random.Random(424242)
        contradictions = 0
        for trial in range(500):
            n = rng.randint(2, 6)
            scheme = FoldScheme.opposing(n)
            checks = []
            for _ in range(rng.randint(1, 7)):
                length = rng.randint(0, n)
                checks.append(
                    Check(
                        cells=tuple(sorted(rng.sample(range(n), length))),
                        parity=rng.randint(0, 1),
                    )
                )
            observations = [
                obs(ticket, position, rng.randint(0, 1))
                for ticket in scheme.tickets
                for position in range(scheme.positions(ticket))
                if rng.random() < 0.55
            ]
            result = analyze_ticket(scheme, checks, observations)
            if result.compatible:
                continue
            contradictions += 1
            with self.subTest(trial=trial):
                self.assert_certificate_irredundant(result, n)
        # Make sure the sweep actually exercised the contradiction path.
        self.assertGreater(contradictions, 100)

    def test_redundant_duplicate_rows_still_give_an_irredundant_certificate(self):
        # The same row three times is a zero-sum sub-collection; a certificate
        # must not include it.  The real culprit is the empty-row check.
        scheme = FoldScheme.opposing(8)
        duplicate = Check(cells=(0, 1), parity=1, label="dup")
        checks = [duplicate, duplicate, duplicate, Check(cells=(), parity=1, label="bad")]
        result = analyze_ticket(scheme, checks, [])
        self.assertFalse(result.compatible)
        self.assert_certificate_irredundant(result, scheme.n_cells)
        self.assertEqual([item.label for item in result.contradiction.items], ["bad"])

    def test_randomized_redundant_systems_keep_certificates_irredundant(self):
        # Duplicate and near-duplicate checks create many zero-sum
        # sub-collections; the certificate must still be irredundant.
        rng = random.Random(2026)
        contradictions = 0
        for trial in range(400):
            n = rng.randint(3, 8)
            scheme = FoldScheme.opposing(n)
            bases = [
                tuple(sorted(rng.sample(range(n), rng.randint(0, n)))) for _ in range(3)
            ]
            checks = []
            for _ in range(rng.randint(3, 12)):
                cells = rng.choice(bases)
                if rng.random() < 0.5 and cells:
                    flipped = list(cells)
                    position = rng.randrange(len(flipped))
                    flipped[position] = (flipped[position] + 1) % n
                    cells = tuple(sorted(set(flipped)))
                checks.append(Check(cells=cells, parity=rng.randint(0, 1)))
            result = analyze_ticket(scheme, checks, [])
            if result.compatible:
                continue
            contradictions += 1
            with self.subTest(trial=trial):
                self.assert_certificate_irredundant(result, n)
        self.assertGreater(contradictions, 100)


# ---------------------------------------------------------------------------
# randomized agreement with brute force and an independent solver
# ---------------------------------------------------------------------------


class RandomizedCrossCheckTests(unittest.TestCase):
    def test_random_tickets_agree_with_enumeration(self):
        rng = random.Random(20261128)
        for trial in range(120):
            n = rng.randint(2, 6)
            k = rng.randint(1, n)
            window_cells = sorted(rng.sample(range(n), k))
            local_perm = list(range(k))
            rng.shuffle(local_perm)
            if rng.random() < 0.5:
                scheme = FoldScheme.from_permutation(local_perm, window=window_cells)
            else:
                scheme = FoldScheme.opposing(n)

            checks = []
            for _ in range(rng.randint(0, 4)):
                length = rng.randint(0, scheme.n_cells)
                checks.append(
                    Check(
                        cells=tuple(sorted(rng.sample(range(scheme.n_cells), length))),
                        parity=rng.randint(0, 1),
                        label=f"k{len(checks)}",
                    )
                )

            observations = []
            for ticket in scheme.tickets:
                for position in range(scheme.positions(ticket)):
                    roll = rng.random()
                    if roll < 0.35:
                        continue
                    value = None if roll < 0.5 else rng.randint(0, 1)
                    observations.append(obs(ticket, position, value))

            errata = []
            if rng.random() < 0.4:
                ticket = rng.choice(scheme.tickets)
                position = rng.randrange(scheme.positions(ticket))
                errata.append(
                    Erratum(ticket=ticket, position=position, value=rng.choice([None, 0, 1]))
                )

            with self.subTest(trial=trial):
                result = analyze_ticket(scheme, checks, observations, errata)
                solutions = list(
                    enumerate_solutions(scheme, checks, observations, errata, max_cells=10)
                )
                width = scheme.n_cells
                equations, observed_cells = equations_from(scheme, checks, observations, errata)
                consistent, rank, _, _ = solve(width, equations)
                forced_reference, free_reference = forced_and_free(
                    width, equations, observed_cells
                )

                self.assertEqual(result.compatible, consistent)
                self.assertEqual(result.compatible, bool(solutions))
                if not result.compatible:
                    continue

                self.assertEqual(result.solution_count, len(solutions))
                self.assertEqual(result.solution_count, 1 << (width - rank))
                self.assertEqual(result.rank, rank)
                self.assertEqual(dict(result.determined_unobserved), forced_reference)
                self.assertEqual(result.undetermined_unobserved, tuple(free_reference))

                # Every enumerated solution agrees with every reported fixed cell.
                for solution in solutions:
                    for cell, value in result.determined_unobserved.items():
                        self.assertEqual(solution[cell], value)

    def test_observation_values_are_really_constrained_by_checks(self):
        # A satisfiable system built from a hidden string: the analyser must
        # report the hidden string's values for all fixed cells.
        rng = random.Random(31337)
        for trial in range(60):
            n = rng.randint(3, 7)
            hidden = {cell: rng.randint(0, 1) for cell in range(n)}
            checks = []
            for _ in range(rng.randint(1, 4)):
                length = rng.randint(1, n)
                cells = tuple(sorted(rng.sample(range(n), length)))
                parity = 0
                for cell in cells:
                    parity ^= hidden[cell]
                checks.append(Check(cells=cells, parity=parity))
            scheme = FoldScheme.opposing(n)
            observations = [
                obs("window", index, hidden[index])
                for index in range(n)
                if rng.random() < 0.4
            ]
            with self.subTest(trial=trial):
                result = analyze_ticket(scheme, checks, observations)
                self.assertTrue(result.compatible)
                for cell, value in result.determined_unobserved.items():
                    self.assertEqual(value, hidden[cell])
                self.assertEqual(result.solution_count, 1 << result.free_variables)


# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------


class LargeTicketTests(unittest.TestCase):
    def test_hundreds_of_unknowns_with_structured_checks(self):
        n = 400
        # Chain checks c[i] xor c[i+1] = 0 tie every cell to cell 0; the one
        # observation of cell 0 then pins the whole string.  Rank is n.
        checks = [Check(cells=(i, i + 1), parity=0) for i in range(n - 1)]
        scheme = FoldScheme.opposing(n)
        result = analyze_ticket(scheme, checks, [obs("window", 0, 1)])
        self.assertTrue(result.compatible)
        self.assertEqual(result.solution_count, 1)
        self.assertEqual(result.rank, n)
        self.assertEqual(len(result.determined_unobserved), n - 1)
        self.assertTrue(all(value == 1 for value in result.determined_unobserved.values()))
        self.assertEqual(result.undetermined_unobserved, ())

    def test_hundreds_of_unknowns_chain_without_observation_leaves_one_free(self):
        n = 400
        checks = [Check(cells=(i, i + 1), parity=0) for i in range(n - 1)]
        scheme = FoldScheme.opposing(n)
        result = analyze_ticket(scheme, checks, [])
        self.assertTrue(result.compatible)
        self.assertEqual(result.rank, n - 1)
        self.assertEqual(result.solution_count, 2)
        # All cells move together, so none is individually fixed.
        self.assertEqual(dict(result.determined_unobserved), {})
        self.assertEqual(len(result.undetermined_unobserved), n)

    def test_hundreds_of_unknowns_without_checks_gives_exact_count(self):
        n = 350
        scheme = FoldScheme.opposing(n)
        observations = [obs("window", 0, 1), obs("window", 1, 0), obs("stub", 0, 0)]
        checks = [Check(cells=(0, 1, 2), parity=1)]
        result = analyze_ticket(scheme, checks, observations)
        equations, observed_cells = equations_from(scheme, checks, observations)
        consistent, rank, _, _ = solve(n, equations)
        forced_reference, free_reference = forced_and_free(n, equations, observed_cells)
        self.assertTrue(consistent)
        self.assertTrue(result.compatible)
        self.assertEqual(result.rank, rank)
        self.assertEqual(result.solution_count, 1 << (n - rank))
        self.assertEqual(dict(result.determined_unobserved), forced_reference)
        self.assertEqual(result.undetermined_unobserved, tuple(free_reference))

    def test_large_contradiction_is_detected_and_certified(self):
        n = 400
        checks = [Check(cells=(i, i + 1), parity=0) for i in range(n - 1)]
        checks.append(Check(cells=(0, n - 1), parity=1))
        scheme = FoldScheme.opposing(n)
        result = analyze_ticket(scheme, checks, [obs("window", 0, 0)])
        self.assertFalse(result.compatible)
        self.assertEqual(result.contradiction.reason, "parity_conflict")
        items = result.contradiction.items
        self.assertEqual(len(items), n)
        lhs = 0
        rhs = 0
        for item in items:
            for cell in item.cells:
                lhs ^= 1 << cell
            rhs ^= item.value
        self.assertEqual(lhs, 0)
        self.assertEqual(rhs, 1)

    def test_large_random_system_matches_reference_solver(self):
        rng = random.Random(7)
        n = 220
        scheme = FoldScheme.opposing(n)
        hidden = {cell: rng.randint(0, 1) for cell in range(n)}
        checks = []
        for _ in range(120):
            length = rng.randint(1, 6)
            cells = tuple(sorted(rng.sample(range(n), length)))
            parity = 0
            for cell in cells:
                parity ^= hidden[cell]
            checks.append(Check(cells=cells, parity=parity))
        observations = [
            obs("window", index, hidden[index]) for index in rng.sample(range(n), 60)
        ]
        result = analyze_ticket(scheme, checks, observations)
        equations, observed_cells = equations_from(scheme, checks, observations)
        consistent, rank, _, _ = solve(n, equations)
        forced_reference, free_reference = forced_and_free(n, equations, observed_cells)
        self.assertTrue(consistent)
        self.assertTrue(result.compatible)
        self.assertEqual(result.rank, rank)
        self.assertEqual(result.solution_count, 1 << (n - rank))
        self.assertEqual(dict(result.determined_unobserved), forced_reference)
        self.assertEqual(result.undetermined_unobserved, tuple(free_reference))


if __name__ == "__main__":
    unittest.main()
