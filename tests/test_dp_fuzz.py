import itertools
import random
import unittest

from key_ceremony import schedule_ceremony
from key_ceremony.solver import (
    build_boundary,
    build_independent_subsets,
    enumerate_round_assignments,
    transition_cost,
)


class DeterministicDPFuzzTests(unittest.TestCase):
    def test_dp_matches_bruteforce_on_fixed_seed_small_specs(self):
        comparisons = 0
        for seed in range(80):
            rng = random.Random(1000 + seed)
            n = rng.randrange(3, 7)
            people = []
            for i in range(n):
                person = {"id": f"p{i}"}
                if rng.random() < 0.35:
                    person["management_line"] = rng.choice(["L1", "L2"])
                if rng.random() < 0.7:
                    person["eligible_slots"] = [
                        slot for slot in ("a", "b") if rng.random() < 0.8
                    ]
                people.append(person)

            proxy_pairs = []
            for i in range(n):
                for j in range(i + 1, n):
                    if rng.random() < 0.12:
                        proxy_pairs.append([f"p{i}", f"p{j}"])

            rounds = [{"id": f"r{r}", "required_slots": ["a", "b"]} for r in range(3)]
            for r in range(3):
                for i in range(n):
                    if rng.random() < 0.75:
                        people[i].setdefault("available_rounds", []).append(f"r{r}")

            spec_dict = {"people": people, "proxy_pairs": proxy_pairs, "rounds": rounds}
            result = schedule_ceremony(spec_dict)
            spec = result.spec
            options = [enumerate_round_assignments(spec, round_) for round_ in spec.rounds]

            if any(not values for values in options):
                self.assertFalse(result.feasible)
                self.assertIsNotNone(result.local_infeasible_round)
                continue

            independent = build_independent_subsets(spec)
            boundaries = [build_boundary(spec, 0, 1), build_boundary(spec, 1, 2)]
            brute_best = None
            brute_reachable = False
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
                if ok:
                    brute_reachable = True
                    if brute_best is None or total < brute_best:
                        brute_best = total

            comparisons += 1
            if not brute_reachable:
                self.assertFalse(result.feasible, seed)
                self.assertIsNotNone(result.first_unreachable_round, seed)
            else:
                self.assertTrue(result.feasible, seed)
                self.assertEqual(result.minimum_changes, brute_best, seed)

        # Most fixed seeds should produce at least some locally feasible cases.
        self.assertGreater(comparisons, 20)


if __name__ == "__main__":
    unittest.main()
