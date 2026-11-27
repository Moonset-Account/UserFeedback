"""Randomized differential test against a from-scratch reference division.

The engine is incremental; these tests recompute the relational division
naively after every mutation and compare both the result set and the emitted
transition events.  Only local synthetic data is used.
"""

import random
import unittest

from compliance_division import ENTER, LEAVE, DivisionEngine, Fact, Requirement


class NaiveDivision:
    """Straightforward reference implementation (no incrementality)."""

    def __init__(self, models, requirements=(), facts=()):
        self.models = list(dict.fromkeys(models))
        self.requirements = {}
        self.facts = {}
        for requirement in requirements:
            self.requirements[requirement.requirement_id] = requirement
        for fact in facts:
            self.facts[fact.source_id] = fact

    def result(self):
        selected = set()
        for model in self.models:
            if self._selected(model):
                selected.add(model)
        return frozenset(selected)

    def _selected(self, model):
        for requirement in self.requirements.values():
            covered = any(
                fact.model == model
                and fact.capability == requirement.capability
                and fact.version_from <= requirement.version_from
                and requirement.version_to <= fact.version_to
                for fact in self.facts.values()
            )
            if not covered:
                return False
        return True


def sorted_events(events):
    return sorted((event.kind, event.model) for event in events)


class RandomizedDifferentialTests(unittest.TestCase):
    def test_random_mutation_sequence_matches_reference(self):
        rng = random.Random(20261128)
        capabilities = ["gpu", "cpu", "nic", "disk"]
        models = ["m0", "m1", "m2", "m3"]
        engine = DivisionEngine(models)
        events = []
        engine.subscribe(events.append)
        reference = NaiveDivision(models)

        next_requirement = 0
        next_fact = 0

        for step in range(400):
            before = reference.result()
            action = rng.choice(
                ["add_req", "remove_req", "add_fact", "remove_fact", "add_fact", "remove_fact"]
            )

            if action == "add_req":
                lo = rng.randint(0, 6)
                requirement = Requirement(
                    f"r{next_requirement}",
                    rng.choice(capabilities),
                    lo,
                    lo + rng.randint(0, 4),
                )
                next_requirement += 1
                engine.add_requirement(requirement)
                reference.requirements[requirement.requirement_id] = requirement
            elif action == "remove_req":
                if not reference.requirements:
                    continue
                requirement_id = rng.choice(list(reference.requirements))
                engine.remove_requirement(requirement_id)
                del reference.requirements[requirement_id]
            elif action == "add_fact":
                lo = rng.randint(0, 6)
                fact = Fact(
                    f"f{next_fact}",
                    rng.choice(models),
                    rng.choice(capabilities),
                    lo,
                    lo + rng.randint(0, 4),
                )
                next_fact += 1
                engine.add_fact(fact)
                reference.facts[fact.source_id] = fact
            else:
                if not reference.facts:
                    continue
                source_id = rng.choice(list(reference.facts))
                engine.remove_fact(source_id)
                del reference.facts[source_id]

            after = reference.result()
            self.assertEqual(
                engine.current_result(),
                after,
                msg=f"step {step} action {action}: result mismatch",
            )
            expected_events = []
            for model in before - after:
                expected_events.append((LEAVE, model))
            for model in after - before:
                expected_events.append((ENTER, model))
            self.assertEqual(
                sorted_events(events),
                sorted(expected_events),
                msg=f"step {step} action {action}: events mismatch",
            )
            for model in models:
                self.assertEqual(
                    engine.is_selected(model), model in after,
                    msg=f"step {step}: is_selected mismatch for {model}",
                )
            events.clear()

    def test_random_duplicate_heavy_sequence(self):
        """Bias toward exact duplicate reports to exercise reference counts."""
        rng = random.Random(7)
        models = ["m0", "m1"]
        engine = DivisionEngine(models)
        events = []
        engine.subscribe(events.append)
        reference = NaiveDivision(models)

        payloads = [("gpu", 0, 3), ("gpu", 1, 2), ("cpu", 2, 4), ("gpu", 0, 5)]
        next_fact = 0
        for step in range(300):
            before = reference.result()
            if rng.random() < 0.5 or not reference.facts:
                model = rng.choice(models)
                capability, lo, hi = rng.choice(payloads)
                fact = Fact(f"f{next_fact}", model, capability, lo, hi)
                next_fact += 1
                engine.add_fact(fact)
                reference.facts[fact.source_id] = fact
            else:
                source_id = rng.choice(list(reference.facts))
                engine.remove_fact(source_id)
                del reference.facts[source_id]
            if rng.random() < 0.2:
                requirement = Requirement(
                    f"r{step}", rng.choice(["gpu", "cpu"]), 0, 3
                )
                engine.add_requirement(requirement)
                reference.requirements[requirement.requirement_id] = requirement

            after = reference.result()
            self.assertEqual(engine.current_result(), after, msg=f"step {step}")
            expected = [(LEAVE, m) for m in before - after] + [
                (ENTER, m) for m in after - before
            ]
            self.assertEqual(
                sorted_events(events), sorted(expected), msg=f"step {step} events"
            )
            events.clear()


if __name__ == "__main__":
    unittest.main()
