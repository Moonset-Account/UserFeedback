import random
import unittest

from probe_planner import InfeasibleCoverageError, plan_probes
from probe_planner.normalization import normalize_faults, normalize_probes
from probe_planner.semantics import probe_distinction_mask


def brute_force_optimal(probe_dicts, fault_ids, required_entries, budget, sites):
    probes = normalize_probes(probe_dicts)
    faults = normalize_faults(fault_ids, known_sites=sites)
    masks, pairs, matrix = probe_distinction_mask(probes, faults)

    required_index = {entry: i for i, entry in enumerate(required_entries)}
    required_mask = (1 << len(required_entries)) - 1 if required_entries else 0
    entry_masks = []
    for probe in probes:
        bit = 0
        if probe.entry in required_index:
            bit = 1 << required_index[probe.entry]
        entry_masks.append(bit)

    n = len(probes)
    best = None

    for selected in range(1 << n):
        cost = 0
        covered = 0
        union = 0
        indices = []
        for i in range(n):
            if selected & (1 << i):
                cost += probes[i].cost
                covered |= entry_masks[i]
                union |= masks[i]
                indices.append(i)
        if cost > budget or covered != required_mask:
            continue

        score = union.bit_count()
        key = (score, -cost, -len(indices), tuple(-i for i in indices))
        # The transformed key maximizes score, minimizes cost/count, and
        # minimizes the ascending original-index tuple.
        if best is None or key > best[0]:
            best = (key, selected, cost, score, tuple(indices))

    return probes, pairs, matrix, best


class ExhaustiveRandomOptimalityTests(unittest.TestCase):
    def test_planner_matches_brute_force_for_small_layouts(self):
        for seed in range(250):
            with self.subTest(seed=seed):
                rng = random.Random(10_000 + seed)
                sites = [f"S{i}" for i in range(rng.randint(1, 3))]
                entries = [f"E{i}" for i in range(rng.randint(1, 4))]
                dependencies = [f"D{i}" for i in range(rng.randint(1, 5))]
                n = rng.randint(1, 11)

                probe_dicts = []
                for i in range(n):
                    probe_dicts.append(
                        {
                            "id": f"P{i}",
                            "site": rng.choice(sites),
                            "entry": rng.choice(entries),
                            "dependencies": rng.sample(
                                dependencies, k=rng.randint(0, min(3, len(dependencies)))
                            ),
                            "cost": rng.randint(0, 3),
                        }
                    )

                fault_ids = []
                for site in rng.sample(sites, k=rng.randint(0, len(sites))):
                    fault_ids.append(site)  # known site => site outage
                for dep in rng.sample(
                    dependencies, k=rng.randint(1, len(dependencies))
                ):
                    fault_ids.append(dep)
                rng.shuffle(fault_ids)

                required_entries = rng.sample(
                    entries, k=rng.randint(0, len(entries))
                )
                budget = rng.randint(0, 12)

                probes, pairs, matrix, brute = brute_force_optimal(
                    probe_dicts,
                    fault_ids,
                    required_entries,
                    budget,
                    sites,
                )

                if brute is None:
                    with self.assertRaises(InfeasibleCoverageError):
                        plan_probes(
                            probe_dicts,
                            fault_ids,
                            budget=budget,
                            required_entries=required_entries,
                            known_sites=sites,
                        )
                    continue

                _, expected_mask, expected_cost, expected_score, expected_indices = brute
                result = plan_probes(
                    probe_dicts,
                    fault_ids,
                    budget=budget,
                    required_entries=required_entries,
                    known_sites=sites,
                )

                self.assertEqual(
                    result.selected_ids,
                    tuple(f"P{i}" for i in expected_indices),
                )
                self.assertEqual(result.total_cost, expected_cost)
                self.assertEqual(result.distinguished_pair_count, expected_score)
                self.assertLessEqual(result.total_cost, budget)
                self.assertTrue(
                    set(required_entries).issubset(result.covered_entries)
                )
                self.assertEqual(
                    len(result.ambiguous_pairs),
                    len(pairs) - expected_score,
                )

                # Independent signature check from the returned selection.
                ambiguous_set = set(result.ambiguous_pairs)
                distinguished_set = set(result.distinguished_pairs)
                fault_ids_in_order = [fault.id for fault in normalize_faults(
                    fault_ids, known_sites=sites
                )]
                for pair_index, (left, right) in enumerate(pairs):
                    left_sig = tuple(matrix[left][i] for i in expected_indices)
                    right_sig = tuple(matrix[right][i] for i in expected_indices)
                    pair = (
                        fault_ids_in_order[left],
                        fault_ids_in_order[right],
                    )
                    if left_sig == right_sig:
                        self.assertIn(pair, ambiguous_set)
                    else:
                        self.assertIn(pair, distinguished_set)


if __name__ == "__main__":
    unittest.main()
