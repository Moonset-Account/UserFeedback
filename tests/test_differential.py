"""Randomized differential test: planner vs independent brute-force oracle."""

import random
import unittest

from oracle import brute_force

from wallet_planner import plan_presentation


def _random_spec(rng, group_budget=8):
    attributes = ["a", "b", "c", "d", "e"]
    holders = ["alice", "bob"] if rng.random() < 0.5 else ["alice"]
    issuers = ["gov", "bank"]

    credentials = []
    total_groups = 0
    for c_index in range(rng.randint(2, 4)):
        if total_groups >= group_budget:
            break
        groups = []
        for g_index in range(rng.randint(1, 2)):
            if total_groups >= group_budget:
                break
            size = rng.randint(1, 2)
            groups.append(
                {
                    "id": "c%d.g%d" % (c_index + 1, g_index + 1),
                    "attributes": rng.sample(attributes, size),
                }
            )
            total_groups += 1
        credentials.append(
            {
                "id": "c%d" % (c_index + 1),
                "issuer": rng.choice(issuers),
                "holder": rng.choice(holders),
                "verified": rng.random() < 0.92,
                "groups": groups,
            }
        )

    # Bias conditions toward attributes that actually appear, so a good share
    # of the generated specifications is satisfiable and the optimum often
    # has to span several credentials.
    present = sorted(
        {
            attribute
            for credential in credentials
            for group in credential["groups"]
            for attribute in group["attributes"]
        }
    )
    if not present:
        return {
            "credentials": credentials,
            "conditions": [],
            "attribute_weights": {},
        }

    conditions = []
    for k in range(rng.randint(1, 4)):
        alternatives = []
        for _ in range(rng.randint(1, 3)):
            size = rng.randint(1, min(2, len(present)))
            required = rng.sample(present, size)
            if rng.random() < 0.2:
                alternatives.append(
                    {
                        "attributes": required,
                        "acceptable_issuers": [rng.choice(issuers)],
                    }
                )
            else:
                alternatives.append(required)
        condition = {"id": "k%d" % k, "alternatives": alternatives}
        if rng.random() < 0.2:
            condition["acceptable_issuers"] = [rng.choice(issuers)]
        if rng.random() < 0.25 and len(present) >= 2:
            condition["same_credential"] = [rng.sample(present, 2)]
        conditions.append(condition)

    weights = {}
    for name in attributes:
        if rng.random() < 0.75:
            weights[name] = rng.choice([0, 1, 2, 3, 5, 0.5, 1.5])

    return {
        "attribute_weights": weights,
        "credentials": credentials,
        "conditions": conditions,
    }


class DifferentialTests(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = random.Random(20261128)
        satisfied = 0
        for case in range(500):
            spec = _random_spec(rng)
            with self.subTest(case=case):
                plan = plan_presentation(spec)
                expected = brute_force(spec)
                satisfied += plan.satisfied
                self.assertEqual(plan.satisfied, expected[0], spec)
                if not plan.satisfied:
                    continue
                _, holder, group_ids, weight, credentials = expected
                self.assertEqual(plan.holder, holder, spec)
                self.assertEqual(plan.selected_group_ids, group_ids, spec)
                self.assertEqual(plan.total_weight, weight, spec)
                self.assertEqual(plan.credential_ids, credentials, spec)
        # Guard against the generator collapsing into a degenerate corpus.
        self.assertGreater(satisfied, 100)


if __name__ == "__main__":
    unittest.main()
