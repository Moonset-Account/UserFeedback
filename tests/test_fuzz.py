"""Randomized cross-validation of the exact DP against full enumeration."""

import random
import unittest
from fractions import Fraction
from itertools import combinations

from finite_audit import (
    Plan,
    exhaustive_conditional,
    exhaustive_first_crossing,
    exhaustive_subset_first_crossing,
)


class TestFuzzAgainstEnumeration(unittest.TestCase):
    def test_random_plans_small_n_permutations(self):
        rng = random.Random(20261128)
        for _ in range(40):
            N = rng.randint(4, 7)
            M = rng.randint(0, N)
            k = rng.randint(1, min(3, N))
            positions = sorted(rng.sample(range(1, N + 1), k))
            checkpoints = [(n, rng.randint(1, max(1, n))) for n in positions]
            rule = rng.choice(("ge", "gt"))
            plan = Plan(N, M, checkpoints, rule=rule)

            for a in range(N + 1):
                dp = plan.first_crossing(a)
                brute = exhaustive_first_crossing(plan, a)
                for got, exp in zip(dp, brute):
                    self.assertEqual(got.probability, exp.probability)
                self.assertEqual(
                    sum(rp.probability for rp in dp),
                    sum(rp.probability for rp in brute),
                )

    def test_random_plans_ten_records_subsets(self):
        rng = random.Random(424242)
        for _ in range(25):
            N = 10
            M = rng.randint(1, N)
            k = rng.randint(1, 3)
            positions = sorted(rng.sample(range(1, N + 1), k))
            checkpoints = [(n, rng.randint(1, max(1, n))) for n in positions]
            rule = rng.choice(("ge", "gt"))
            plan = Plan(N, M, checkpoints, rule=rule)

            for a in range(N + 1):
                dp = plan.first_crossing(a)
                brute = exhaustive_subset_first_crossing(plan, a)
                for got, exp in zip(dp, brute):
                    self.assertEqual(got.probability, exp.probability)

            # Conditional fuzz on short prefixes that cannot have crossed.
            prefix_len = positions[0] - 1
            if prefix_len >= 1:
                for bits in range(min(4, 2**prefix_len)):
                    prefix = tuple((bits >> i) & 1 for i in range(prefix_len))
                    b = sum(prefix)
                    for a in range(b, M + 1):
                        if prefix_len - b > N - a:
                            continue
                        dp = plan.conditional_first_crossing(a, prefix)
                        brute = exhaustive_conditional(plan, a, prefix)
                        for got, exp in zip(dp.per_round, brute.per_round):
                            self.assertEqual(got.probability, exp.probability)
                        self.assertEqual(dp.ever_cross, brute.ever_cross)

    def test_worst_case_agrees_with_direct_scan(self):
        rng = random.Random(7)
        for _ in range(20):
            N = rng.randint(5, 9)
            M = rng.randint(0, N)
            positions = sorted(rng.sample(range(1, N + 1), rng.randint(1, 3)))
            checkpoints = [(n, rng.randint(1, max(1, n))) for n in positions]
            plan = Plan(N, M, checkpoints, rule=rng.choice(("ge", "gt")))

            direct = {
                a: plan.ever_cross_probability(a) for a in range(M + 1)
            }
            worst = plan.worst_case()
            self.assertEqual(worst.ever_cross, max(direct.values()))
            self.assertEqual(worst.worst_a, min(
                a for a, v in direct.items() if v == worst.ever_cross
            ))

            # Conditional worst case against a direct admissible-a scan.
            prefix = tuple(rng.choice((0, 1)) for _ in range(positions[0] - 1))
            b = sum(prefix)
            if b <= M:
                values = {}
                for a in range(b, M + 1):
                    if len(prefix) - b > N - a:
                        continue
                    values[a - b] = plan.conditional_first_crossing(
                        a, prefix
                    ).ever_cross
                if values:
                    cw = plan.conditional_worst_case(prefix)
                    self.assertEqual(cw.ever_cross, max(values.values()))
                    self.assertEqual(
                        cw.worst_remaining,
                        min(r for r, v in values.items()
                            if v == cw.ever_cross),
                    )

    def test_all_probabilities_are_fractions_in_unit_interval(self):
        rng = random.Random(99)
        for _ in range(15):
            N = rng.randint(4, 10)
            positions = sorted(rng.sample(range(1, N + 1), rng.randint(1, 3)))
            plan = Plan(
                N,
                rng.randint(0, N),
                [(n, rng.randint(0, n + 1)) for n in positions],
                rule=rng.choice(("ge", "gt")),
            )
            for a in range(N + 1):
                for rp in plan.first_crossing(a):
                    self.assertIsInstance(rp.probability, Fraction)
                    self.assertGreaterEqual(rp.probability, 0)
                    self.assertLessEqual(rp.probability, 1)
                total = sum(
                    rp.probability for rp in plan.first_crossing(a)
                )
                self.assertLessEqual(total, 1)

    def test_brute_force_counting_formula_on_fixed_case(self):
        # Independent subset enumeration written inline, guarding against a
        # shared bug between the helper and the DP.
        N, a = 10, 3
        plan = Plan(N, a, [(4, 2), (7, 3), (10, 4)])
        counts = [0, 0, 0]
        survivors = 0
        for pos in combinations(range(N), a):
            pos_set = set(pos)
            stopped = False
            for r, n in enumerate((4, 7, 10)):
                seen = sum(1 for p in range(n) if p in pos_set)
                if not stopped and seen >= (2, 3, 4)[r]:
                    counts[r] += 1
                    stopped = True
            if not stopped:
                survivors += 1
        denom = sum(counts) + survivors
        dp = plan.first_crossing(a)
        for r in range(3):
            self.assertEqual(dp[r].probability, Fraction(counts[r], denom))
        self.assertEqual(sum(rp.probability for rp in dp), Fraction(
            sum(counts), denom
        ))


if __name__ == "__main__":
    unittest.main()
