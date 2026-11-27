"""Tests for the finite-population sampling adjudication module."""

import unittest
from fractions import Fraction
from itertools import combinations

from finite_audit import (
    Checkpoint,
    ConditionalResult,
    Plan,
    RoundProbability,
    WorstCaseResult,
    exhaustive_conditional,
    exhaustive_first_crossing,
    exhaustive_index_permutations,
    exhaustive_subset_first_crossing,
)


def ten_record_plan(rule: str = "ge") -> Plan:
    """The running ten-record example: N=10, M=3, checkpoints 4/7/10."""
    return Plan(
        N=10,
        M=3,
        checkpoints=[Checkpoint(4, 2), Checkpoint(7, 3), Checkpoint(10, 4)],
        rule=rule,
    )


class TestValidation(unittest.TestCase):
    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            Plan(0, 0, [(1, 1)])
        with self.assertRaises(ValueError):
            Plan(10, -1, [(1, 1)])
        with self.assertRaises(ValueError):
            Plan(10, 11, [(1, 1)])
        with self.assertRaises(ValueError):
            Plan(10, 3, [])
        with self.assertRaises(ValueError):
            Plan(10, 3, [(0, 1)])
        with self.assertRaises(ValueError):
            Plan(10, 3, [(11, 1)])
        with self.assertRaises(ValueError):
            Plan(10, 3, [(5, -1)])
        with self.assertRaises(ValueError):
            Plan(10, 3, [(5, 1), (5, 2)])
        with self.assertRaises(ValueError):
            Plan(10, 3, [(7, 1), (5, 2)])
        with self.assertRaises(ValueError):
            Plan(10, 3, [(5, 1)], rule="xx")

    def test_bad_a_and_prefix(self):
        plan = ten_record_plan()
        with self.assertRaises(ValueError):
            plan.first_crossing(-1)
        with self.assertRaises(ValueError):
            plan.first_crossing(11)
        with self.assertRaises(ValueError):
            plan.conditional_first_crossing(2, "111")  # too many anomalies
        with self.assertRaises(ValueError):
            plan.conditional_first_crossing(9, "0000000000")  # 10 clean > N-a
        with self.assertRaises(ValueError):
            plan.conditional_first_crossing(3, "010x")
        with self.assertRaises(ValueError):
            plan.conditional_first_crossing(3, [0, 1, 2])
        # A prefix covering the whole population leaves no checkpoint, and
        # with 10 anomalies it also crosses every rule.
        with self.assertRaises(ValueError):
            plan.conditional_first_crossing(3, "1" * 10)

    def test_prefix_already_crossed_raises(self):
        plan = ten_record_plan()
        # At n=4 two anomalies already cross the first threshold (>= 2).
        with self.assertRaises(ValueError):
            plan.conditional_first_crossing(3, "1100")
        plan_gt = ten_record_plan(rule="gt")
        # Under gt rule, 2 anomalies at n=4 do not cross (need > 2).
        result = plan_gt.conditional_first_crossing(3, "1100")
        self.assertIsInstance(result, ConditionalResult)


class TestTenRecordExample(unittest.TestCase):
    def test_hand_computed_round_one_value(self):
        # P(X >= 2) in 4 draws from 10 records with a=3 anomalies.
        plan = ten_record_plan()
        per_round = plan.first_crossing(3)
        # Hand calculation: 1 - (C(7,4) + C(3,1)*C(7,3)) / C(10,4)
        # = 1 - (35 + 105) / 210 = 1 - 140/210 = 1/3.
        self.assertEqual(per_round[0].probability, Fraction(1, 3))

    def test_dp_matches_subset_enumeration_for_every_a(self):
        plan = ten_record_plan()
        for a in range(plan.N + 1):
            dp = plan.first_crossing(a)
            brute = exhaustive_subset_first_crossing(plan, a)
            self.assertEqual(len(dp), len(brute))
            for got, exp in zip(dp, brute):
                self.assertEqual(got.checkpoint, exp.checkpoint)
                self.assertEqual(
                    got.probability,
                    exp.probability,
                    msg=f"mismatch at a={a}, n={got.checkpoint.n}",
                )
            dp_ever = sum(rp.probability for rp in dp)
            brute_ever = sum(rp.probability for rp in brute)
            self.assertEqual(dp_ever, brute_ever)

    def test_ten_records_permutation_by_permutation(self):
        # Literal enumeration of every 10! = 3,628,800 record permutations;
        # each one is equally likely and charged to its first crossing.
        plan = ten_record_plan()
        perms = exhaustive_index_permutations(plan, 3)
        dp = plan.first_crossing(3)
        for got, exp in zip(perms, dp):
            self.assertEqual(got.probability, exp.probability)
        self.assertEqual(
            [rp.probability for rp in perms],
            [Fraction(1, 3), Fraction(13, 120), Fraction(0)],
        )
        # Denominator is exactly 10! and numerators are integer counts.
        self.assertEqual(
            sum(rp.probability.numerator * (3_628_800 // rp.probability.denominator)
                for rp in perms),
            1_209_600 + 393_120,
        )

    def test_permutation_enumeration_small_population(self):
        # Full factorial permutation enumeration on a small plan, both rules.
        for rule in ("ge", "gt"):
            plan = Plan(
                N=6,
                M=4,
                checkpoints=[(2, 1), (4, 2), (6, 3)],
                rule=rule,
            )
            for a in range(plan.N + 1):
                dp = plan.first_crossing(a)
                brute = exhaustive_first_crossing(plan, a)
                for got, exp in zip(dp, brute):
                    self.assertEqual(
                        got.probability,
                        exp.probability,
                        msg=f"rule={rule} a={a} n={got.checkpoint.n}",
                    )

    def test_permutation_cap(self):
        plan = Plan(10, 3, [(4, 2), (7, 3), (10, 4)])
        with self.assertRaises(ValueError):
            exhaustive_first_crossing(plan, 3, max_permutations=1000)
        with self.assertRaises(ValueError):
            exhaustive_index_permutations(plan, 3, max_permutations=1000)

    def test_index_permutations_match_dedup_enumeration(self):
        plan = Plan(6, 3, [(2, 1), (4, 2), (6, 3)])
        for a in range(plan.N + 1):
            indexed = exhaustive_index_permutations(plan, a)
            deduped = exhaustive_first_crossing(plan, a)
            dp = plan.first_crossing(a)
            for x, y, z in zip(indexed, deduped, dp):
                self.assertEqual(x.probability, y.probability)
                self.assertEqual(x.probability, z.probability)

    def test_results_are_exact_fractions(self):
        plan = ten_record_plan()
        for rp in plan.first_crossing(2):
            self.assertIsInstance(rp.probability, Fraction)
        self.assertIsInstance(plan.ever_cross_probability(3), Fraction)


class TestNoDoubleCounting(unittest.TestCase):
    def test_round_probabilities_are_disjoint_events(self):
        plan = ten_record_plan()
        for a in range(plan.N + 1):
            per_round = plan.first_crossing(a)
            # First-crossing mass at round r must never exceed the marginal
            # probability of crossing at checkpoint r (which includes paths
            # that had already stopped earlier).
            for i, rp in enumerate(per_round):
                cp = rp.checkpoint
                marginal = sum(
                    Plan.hypergeometric(plan.N, a, cp.n, k)
                    for k in range(cp.c, a + 1)
                )
                self.assertLessEqual(rp.probability, marginal)
            # Disjoint rounds: their sum is a valid probability.
            total = sum(rp.probability for rp in per_round)
            self.assertGreaterEqual(total, 0)
            self.assertLessEqual(total, 1)

    def test_survivor_complement_identity(self):
        plan = ten_record_plan()
        for a in range(plan.N + 1):
            per_round = plan.first_crossing(a)
            ever = sum(rp.probability for rp in per_round)
            self.assertEqual(ever, plan.ever_cross_probability(a))
            # Direct complement: survive all checkpoints.
            survive = Fraction(0)
            for positions in combinations(range(1, plan.N + 1), a):
                pos = set(positions)
                ok = True
                for cp in plan.checkpoints:
                    seen = sum(1 for p in range(1, cp.n + 1) if p in pos)
                    if plan.crosses(cp, seen):
                        ok = False
                        break
                if ok:
                    survive += Fraction(1, _binomial(plan.N, a))
            self.assertEqual(ever + survive, Fraction(1))


def _binomial(n: int, k: int) -> int:
    from math import comb

    return comb(n, k)


class TestWorstCase(unittest.TestCase):
    def test_worst_case_ten_records(self):
        plan = ten_record_plan()
        result = plan.worst_case()
        self.assertIsInstance(result, WorstCaseResult)
        self.assertEqual(result.worst_a, plan.M)
        # Every admissible a is evaluated and none beats the worst.
        for a in range(plan.M + 1):
            self.assertGreaterEqual(
                result.ever_cross, plan.ever_cross_probability(a)
            )
        self.assertEqual(
            result.ever_cross,
            sum(rp.probability for rp in result.per_round),
        )

    def test_worst_case_when_crossing_impossible(self):
        # Threshold above the maximum possible anomaly count: never crosses.
        plan = Plan(5, 2, [(3, 4), (5, 5)])
        result = plan.worst_case()
        self.assertEqual(result.ever_cross, Fraction(0))
        self.assertEqual(result.worst_a, 0)  # tie -> smallest a
        for rp in result.per_round:
            self.assertEqual(rp.probability, Fraction(0))

    def test_worst_case_monotone_on_fixtures(self):
        # No checkpoint at the final position, so saturation at probability
        # 1 cannot hide the stochastic-ordering monotonicity in a.
        plan = Plan(8, 4, [(2, 1), (5, 2), (7, 3)])
        values = [plan.ever_cross_probability(a) for a in range(plan.M + 1)]
        for earlier, later in zip(values, values[1:]):
            self.assertLessEqual(earlier, later)
        self.assertEqual(plan.worst_case().worst_a, plan.M)

    def test_worst_case_can_saturate_before_m(self):
        # A tight threshold at the final checkpoint makes crossing certain
        # once a >= 3; the tie then resolves to the smallest such a.
        plan = Plan(8, 4, [(2, 1), (5, 2), (8, 3)])
        result = plan.worst_case()
        self.assertEqual(result.ever_cross, Fraction(1))
        self.assertEqual(result.worst_a, 3)
        self.assertLess(plan.ever_cross_probability(2), Fraction(1))


class TestConditional(unittest.TestCase):
    def test_conditional_matches_enumeration(self):
        plan = Plan(7, 4, [(2, 1), (5, 2), (7, 3)])
        # Only prefixes that have not themselves crossed any checkpoint are
        # admissible (length-1 prefixes precede the first checkpoint; "00"
        # is clean at n=2).
        cases = [
            (3, "1"),
            (3, "0"),
            (4, "0"),
            (2, "1"),
            (4, "1"),
            (3, ""),
            (3, "00"),
        ]
        for a, prefix in cases:
            dp = plan.conditional_first_crossing(a, prefix)
            brute = exhaustive_conditional(plan, a, prefix)
            self.assertEqual(dp.observed_anomalies, sum(int(c) for c in prefix))
            for got, exp in zip(dp.per_round, brute.per_round):
                self.assertEqual(
                    got.probability,
                    exp.probability,
                    msg=f"a={a} prefix={prefix!r} n={got.checkpoint.n}",
                )
            self.assertEqual(dp.ever_cross, brute.ever_cross)
            self.assertLessEqual(dp.ever_cross, 1)

    def test_conditional_ten_record_subset_enumeration(self):
        plan = ten_record_plan()
        # Prefix of length 3 (< first checkpoint n=4) cannot have crossed.
        for prefix in ("101", "000", "111", "010"):
            b = prefix.count("1")
            k = len(prefix)
            for a in range(b, plan.M + 1):
                if k - b > plan.N - a:
                    continue
                dp = plan.conditional_first_crossing(a, prefix)
                # Exhaustive subset enumeration over anomaly positions that
                # include the prefix anomaly slots and exclude clean slots.
                fixed_bad = {i + 1 for i, ch in enumerate(prefix) if ch == "1"}
                fixed_good = {i + 1 for i, ch in enumerate(prefix) if ch == "0"}
                free = [p for p in range(1, plan.N + 1) if p > k]
                counts = [0] * len(plan.checkpoints)
                total = 0
                for extra in combinations(free, a - b):
                    pos = fixed_bad | set(extra)
                    assert not (pos & fixed_good)
                    total += 1
                    crossed = False
                    for i, cp in enumerate(plan.checkpoints):
                        seen = sum(1 for p in range(1, cp.n + 1) if p in pos)
                        if not crossed and plan.crosses(cp, seen):
                            counts[i] += 1
                            crossed = True
                brute_ever = Fraction(sum(counts), total)
                self.assertEqual(dp.ever_cross, brute_ever, msg=(a, prefix))
                for i, rp in enumerate(dp.per_round):
                    self.assertEqual(rp.probability, Fraction(counts[i], total))

    def test_conditional_continuation_is_remapped_hypergeometric(self):
        plan = ten_record_plan()
        # Prefix "100" with a=3: 6 unrevealed slots hold 2 anomalies;
        # next checkpoint n=4 draws 1 more record, crossing iff it is bad.
        result = plan.conditional_first_crossing(3, "100")
        self.assertEqual(result.per_round[0].checkpoint.n, 4)
        self.assertEqual(result.per_round[0].probability, Fraction(2, 7))

    def test_conditional_worst_case(self):
        plan = ten_record_plan()
        result = plan.conditional_worst_case("100")
        # M=3 and one anomaly already seen -> at most 2 remain.
        self.assertEqual(result.worst_remaining, 2)
        for a in range(1, plan.M + 1):
            cond = plan.conditional_first_crossing(a, "100")
            self.assertGreaterEqual(result.ever_cross, cond.ever_cross)

    def test_conditional_worst_case_infeasible_prefix(self):
        plan = Plan(5, 2, [(3, 2), (5, 3)])
        # More anomalies in prefix than M permits.
        with self.assertRaises(ValueError):
            plan.conditional_worst_case("111")

    def test_gt_rule_conditional(self):
        # gt rule: at n=3 one anomaly does NOT cross (need strictly more
        # than 1); the fixed total a=3 then surely crosses at n=6 since
        # 3 > 2.
        plan = Plan(6, 3, [(3, 1), (6, 2)], rule="gt")
        result = plan.conditional_first_crossing(3, "100")
        brute = exhaustive_conditional(plan, 3, "100")
        for got, exp in zip(result.per_round, brute.per_round):
            self.assertEqual(got.probability, exp.probability)
        self.assertEqual(result.ever_cross, brute.ever_cross)
        self.assertEqual(result.ever_cross, Fraction(1))


class TestEdgeCases(unittest.TestCase):
    def test_no_anomalies_anywhere(self):
        plan = Plan(6, 2, [(2, 1), (4, 2), (6, 3)])
        per_round = plan.first_crossing(0)
        for rp in per_round:
            self.assertEqual(rp.probability, Fraction(0))
        self.assertEqual(plan.ever_cross_probability(0), Fraction(0))

    def test_threshold_zero_crosses_immediately(self):
        # c=0 with the ge rule means crossing at the first checkpoint surely.
        plan = Plan(5, 2, [(2, 0), (5, 1)])
        per_round = plan.first_crossing(2)
        self.assertEqual(per_round[0].probability, Fraction(1))
        self.assertEqual(per_round[1].probability, Fraction(0))

    def test_checkpoint_at_n_equals_N(self):
        plan = Plan(5, 3, [(5, 3)])
        # At the final checkpoint all a anomalies are visible: deterministic.
        self.assertEqual(plan.first_crossing(3)[0].probability, Fraction(1))
        self.assertEqual(plan.first_crossing(2)[0].probability, Fraction(0))

    def test_tuple_checkpoints_accepted(self):
        plan = Plan(4, 2, [(2, 1), (4, 2)])
        self.assertIsInstance(plan.checkpoints[0], Checkpoint)
        per_round = plan.first_crossing(2)
        # P(at least 1 anomaly among 2 of 4 draws, 2 anomalies total):
        # 1 - C(2,2)/C(4,2) = 1 - 1/6 = 5/6.
        self.assertEqual(per_round[0].probability, Fraction(5, 6))

    def test_empty_prefix_equals_unconditional(self):
        plan = Plan(6, 3, [(2, 1), (4, 2), (6, 3)])
        cond = plan.conditional_first_crossing(3, "")
        uncond = plan.first_crossing(3)
        for got, exp in zip(cond.per_round, uncond):
            self.assertEqual(got.probability, exp.probability)
        self.assertEqual(cond.ever_cross, plan.ever_cross_probability(3))

    def test_round_probability_structure(self):
        plan = ten_record_plan()
        per_round = plan.first_crossing(1)
        self.assertEqual([rp.checkpoint.n for rp in per_round], [4, 7, 10])
        self.assertTrue(all(isinstance(rp, RoundProbability) for rp in per_round))


if __name__ == "__main__":
    unittest.main()
