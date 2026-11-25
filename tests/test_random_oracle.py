from __future__ import annotations

import random
import unittest

from info_flow_checker import (
    CircuitBuilder,
    check_information_flow,
    evaluate,
    propagate_constants,
)


def _decode(index: int, names: list[str]) -> dict[str, bool]:
    return {name: bool((index >> i) & 1) for i, name in enumerate(names)}


def naive_check(circuit, publics: list[str], secrets: list[str],
                summaries: list[str], externals: list[str]):
    """Independent scalar implementation used as an oracle in fuzz tests."""

    for p_index in range(1 << len(publics)):
        public_values = _decode(p_index, publics)
        groups: dict[tuple[bool, ...], tuple[int, tuple[bool, ...]]] = {}
        for s_index in range(1 << len(secrets)):
            assignment = {**public_values, **_decode(s_index, secrets)}
            outputs = evaluate(circuit, assignment)
            summary_key = tuple(outputs[name] for name in summaries)
            external_key = tuple(outputs[name] for name in externals)
            previous = groups.get(summary_key)
            if previous is None:
                groups[summary_key] = (s_index, external_key)
                continue
            first_s, first_external = previous
            if first_external != external_key:
                difference = next(
                    i for i, (a, b) in enumerate(zip(first_external, external_key))
                    if a != b
                )
                return {
                    "secure": False,
                    "public_index": p_index,
                    "secret_a": first_s,
                    "secret_b": s_index,
                    "difference": difference,
                }
    return {"secure": True}


def build_random_circuit(seed: int):
    rng = random.Random(seed)
    builder = CircuitBuilder("random_" + str(seed))

    public_names = ["p" + str(i) for i in range(rng.randint(0, 3))]
    secret_names = ["s" + str(i) for i in range(rng.randint(1, 4))]
    available: list[str] = []
    for name in public_names + secret_names:
        available.append(builder.input(name))

    for _ in range(rng.randint(0, 2)):
        available.append(builder.const(rng.choice([False, True])))

    for gate_index in range(rng.randint(1, 18)):
        op = rng.choice(["and", "or", "not", "mux"])
        if op == "and":
            left = rng.choice(available)
            right = rng.choice(available)
            available.append(builder.and_(left, right))
        elif op == "or":
            left = rng.choice(available)
            right = rng.choice(available)
            available.append(builder.or_(left, right))
        elif op == "not":
            available.append(builder.not_(rng.choice(available)))
        else:
            condition = rng.choice(available)
            true_branch = rng.choice(available)
            false_branch = rng.choice(available)
            available.append(builder.mux(condition, true_branch, false_branch))

    output_count = rng.randint(1, min(4, len(available)))
    output_nodes = rng.sample(available, output_count)
    output_names = ["out" + str(i) for i in range(output_count)]
    for name, node in zip(output_names, output_nodes):
        builder.output(name, node)

    return builder.build(), public_names, secret_names, output_names, rng


class RandomOracleTests(unittest.TestCase):
    def test_bitparallel_checker_matches_independent_scalar_oracle(self) -> None:
        for seed in range(160):
            circuit, publics, secrets, output_names, rng = build_random_circuit(
                10_000 + seed
            )
            summaries = rng.sample(output_names, rng.randint(0, min(2, len(output_names))))
            external_count = rng.randint(0, len(output_names))
            externals = rng.sample(output_names, external_count)

            with self.subTest(seed=seed):
                expected = naive_check(
                    circuit, publics, secrets, summaries, externals
                )
                report = check_information_flow(
                    circuit,
                    public_inputs=publics,
                    secret_inputs=secrets,
                    summary_outputs=summaries,
                    external_outputs=externals,
                )

                self.assertEqual(report.secure, expected["secure"], report.describe())
                if expected["secure"]:
                    self.assertIsNone(report.counterexample)
                    continue

                pair = report.counterexample
                self.assertEqual(pair.public_index, expected["public_index"])
                self.assertEqual(pair.secret_index_a, expected["secret_a"])
                self.assertEqual(pair.secret_index_b, expected["secret_b"])
                self.assertEqual(
                    pair.first_difference.index, expected["difference"]
                )
                outcome = pair.replay(circuit)
                self.assertTrue(outcome.valid, outcome.to_dict())

    def test_constant_propagation_preserves_random_behavior_and_verdict(self) -> None:
        for seed in range(60):
            circuit, publics, secrets, output_names, rng = build_random_circuit(
                20_000 + seed
            )
            summaries = rng.sample(output_names, rng.randint(0, min(2, len(output_names))))
            externals = output_names
            optimized = propagate_constants(circuit)

            with self.subTest(seed=seed):
                for p in range(1 << len(publics)):
                    for s in range(1 << len(secrets)):
                        assignment = {
                            **_decode(p, publics),
                            **_decode(s, secrets),
                        }
                        self.assertEqual(
                            evaluate(circuit, assignment),
                            evaluate(optimized, assignment),
                        )

                before = check_information_flow(
                    circuit,
                    public_inputs=publics,
                    secret_inputs=secrets,
                    summary_outputs=summaries,
                    external_outputs=externals,
                )
                after = check_information_flow(
                    optimized,
                    public_inputs=publics,
                    secret_inputs=secrets,
                    summary_outputs=summaries,
                    external_outputs=externals,
                )
                self.assertEqual(after.secure, before.secure)
                if not before.secure:
                    self.assertEqual(
                        after.counterexample.inputs_a,
                        before.counterexample.inputs_a,
                    )
                    self.assertTrue(after.counterexample.replay(circuit).valid)


if __name__ == "__main__":
    unittest.main()
