from __future__ import annotations

import unittest

from info_flow_checker import (
    CircuitBuilder,
    Policy,
    check_information_flow,
    evaluate,
)
from info_flow_checker.examples import (
    cancellation_circuit,
    mux_equal_branches_circuit,
    xor_summary_circuit,
)


class InformationFlowCheckerTests(unittest.TestCase):
    def test_direct_secret_output_gives_replayable_pair_and_first_bit(self) -> None:
        builder = CircuitBuilder("direct")
        p = builder.input("p")
        s = builder.input("s")
        builder.output("out", s)
        builder.output("pub", p)
        circuit = builder.build()

        report = check_information_flow(
            circuit,
            public_inputs=["p"],
            secret_inputs=["s"],
            # Actual external list deliberately excludes the public echo.
            external_outputs=["out"],
        )

        self.assertFalse(report.secure)
        self.assertEqual(report.public_assignments_checked, 2)
        self.assertEqual(report.secret_assignment_count, 2)
        pair = report.counterexample
        self.assertIsNotNone(pair)
        self.assertEqual(pair.public_assignment, {"p": False})
        self.assertEqual(pair.inputs_a, {"p": False, "s": False})
        self.assertEqual(pair.inputs_b, {"p": False, "s": True})
        self.assertEqual(pair.first_difference.index, 0)
        self.assertEqual(pair.first_difference.output, "out")
        self.assertEqual(
            (pair.first_difference.value_a, pair.first_difference.value_b),
            (False, True),
        )

        outcome = pair.replay(circuit)
        self.assertTrue(outcome.valid, outcome)
        self.assertEqual(outcome.first_difference, pair.first_difference)

    def test_conditional_leak_is_found_only_under_enabling_public_value(self) -> None:
        builder = CircuitBuilder("conditional")
        p = builder.input("p")
        s = builder.input("s")
        builder.output("out", builder.and_(p, s))
        circuit = builder.build()

        report = check_information_flow(
            circuit, public_inputs=["p"], secret_inputs=["s"]
        )
        self.assertFalse(report.secure)
        pair = report.counterexample
        self.assertEqual(pair.public_assignment, {"p": True})
        self.assertEqual(pair.inputs_a["s"], False)
        self.assertEqual(pair.inputs_b["s"], True)
        self.assertEqual(report.leaking_public_assignment_count, 1)
        self.assertTrue(pair.replay(circuit).valid)

        # The p=0 slice alone is secure for both secrets.
        values_s0 = evaluate(circuit, {"p": False, "s": False})
        values_s1 = evaluate(circuit, {"p": False, "s": True})
        self.assertEqual(values_s0["out"], values_s1["out"])

    def test_summary_groups_find_collision_and_respect_output_order(self) -> None:
        circuit = xor_summary_circuit()

        report = check_information_flow(
            circuit,
            Policy(
                secret_inputs=["a", "b"],
                summary_outputs=["summary"],
                external_outputs=["summary", "leak_a"],
            ),
        )
        self.assertFalse(report.secure)
        pair = report.counterexample

        # Secret index 1 is (a=1,b=0), index 2 is (a=0,b=1); both XOR to 1.
        self.assertEqual(pair.secret_assignment_a, {"a": True, "b": False})
        self.assertEqual(pair.secret_assignment_b, {"a": False, "b": True})
        self.assertEqual(pair.summary_a, {"summary": True})
        self.assertEqual(pair.summary_b, {"summary": True})

        # Summary is external bit 0 and equal; leak_a is the first difference.
        self.assertEqual(pair.first_difference.index, 1)
        self.assertEqual(pair.first_difference.output, "leak_a")
        self.assertTrue(pair.replay(circuit).valid)

        reordered = check_information_flow(
            circuit,
            secret_inputs=["a", "b"],
            summary_outputs=["summary"],
            external_outputs=["leak_a", "summary"],
        )
        difference = reordered.counterexample.first_difference
        self.assertEqual(difference.index, 0)
        self.assertEqual(difference.output, "leak_a")

    def test_external_output_equal_to_approved_summary_is_secure(self) -> None:
        circuit = xor_summary_circuit()
        report = check_information_flow(
            circuit,
            secret_inputs=["a", "b"],
            summary_outputs=["summary"],
            external_outputs=["summary"],
        )
        self.assertTrue(report.secure, report.describe())
        self.assertIsNone(report.counterexample)
        self.assertEqual(report.public_assignments_checked, 1)
        self.assertEqual(report.secret_assignment_count, 4)

    def test_cancellation_circuit_is_secure_even_though_internal_wires_depend_on_secret(
        self,
    ) -> None:
        circuit = cancellation_circuit()
        report = check_information_flow(
            circuit,
            public_inputs=["p"],
            secret_inputs=["s"],
            summary_outputs=[],
            external_outputs=["decision"],
        )
        self.assertTrue(report.secure, report.describe())

        # Concrete evidence that the checker is not merely tainting every wire
        # touched by s: at p=1 the branches swap while the OR result stays 1.
        low_secret = evaluate(circuit, {"p": True, "s": False})
        high_secret = evaluate(circuit, {"p": True, "s": True})
        self.assertFalse(low_secret["left"])
        self.assertTrue(low_secret["right"])
        self.assertTrue(high_secret["left"])
        self.assertFalse(high_secret["right"])
        self.assertEqual(low_secret["decision"], high_secret["decision"])
        self.assertEqual(low_secret["decision"], True)

        for p in (False, True):
            for s in (False, True):
                result = evaluate(circuit, {"p": p, "s": s})
                self.assertEqual(result["decision"], p)

    def test_mux_equal_branches_cancellation_is_secure(self) -> None:
        circuit = mux_equal_branches_circuit()
        report = check_information_flow(
            circuit,
            public_inputs=["p"],
            secret_inputs=["s"],
            external_outputs=["decision"],
        )
        self.assertTrue(report.secure, report.describe())
        for p in (False, True):
            for s in (False, True):
                self.assertEqual(
                    evaluate(circuit, {"p": p, "s": s})["decision"], p
                )

    def test_empty_external_output_list_cannot_leak(self) -> None:
        builder = CircuitBuilder("no_external")
        s = builder.input("s")
        builder.output("hidden", s)
        circuit = builder.build()
        report = check_information_flow(
            circuit,
            secret_inputs=["s"],
            summary_outputs=[],
            external_outputs=[],
        )
        self.assertTrue(report.secure)
        self.assertEqual(report.external_outputs, ())


if __name__ == "__main__":
    unittest.main()
