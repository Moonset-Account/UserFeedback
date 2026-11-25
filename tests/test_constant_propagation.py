from __future__ import annotations

import itertools
import unittest

from info_flow_checker import (
    CircuitBuilder,
    check_information_flow,
    evaluate,
    propagate_constants,
)
from info_flow_checker.examples import constant_heavy_circuit


class ConstantPropagationTests(unittest.TestCase):
    def test_folds_constants_identities_and_dead_nodes(self) -> None:
        original = constant_heavy_circuit()
        optimized = propagate_constants(original)

        # Every input assignment remains observationally identical.
        for p, s in itertools.product((False, True), repeat=2):
            assignment = {"p": p, "s": s}
            self.assertEqual(
                evaluate(original, assignment),
                evaluate(optimized, assignment),
            )

        optimized_ops = {node.id: node.op for node in optimized.nodes.values()}
        # Shared s AND p remains a gate because it is non-constant.
        self.assertEqual(optimized_ops["shared"], "and")
        # These outputs are identities of the shared node and should alias it.
        self.assertEqual(optimized.output_node("and_true"), "shared")
        self.assertEqual(optimized.output_node("or_false"), "shared")
        # Double negation and constant MUX selector are simplified to p/s.
        self.assertEqual(optimized.output_node("double_not"), "s")
        self.assertEqual(optimized.output_node("mux_const"), "p")
        self.assertEqual(optimized.output_node("mux_same"), "p")
        # Constant false output points at a folded constant node.
        dead_zero_node = optimized.output_node("dead_zero")
        self.assertEqual(optimized.nodes[dead_zero_node].op, "const")
        self.assertFalse(optimized.nodes[dead_zero_node].value)
        # The intermediate NOT used only by double negation is dead.
        self.assertNotIn("not_s", optimized.nodes)

    def test_shared_subgraph_is_not_duplicated_by_propagation(self) -> None:
        builder = CircuitBuilder("shared_after_opt")
        a = builder.input("a")
        b = builder.input("b")
        t = builder.const(True)
        shared = builder.and_(a, b, node_id="shared")
        first = builder.and_(shared, t, node_id="first")
        second = builder.or_(shared, t, node_id="second")  # always true
        builder.output("o1", first)
        builder.output("o2", second)
        circuit = builder.build()
        optimized = propagate_constants(circuit)

        # first aliases shared; second folds to true.
        self.assertEqual(optimized.output_node("o1"), "shared")
        second_node = optimized.output_node("o2")
        self.assertEqual(optimized.nodes[second_node].op, "const")
        self.assertTrue(optimized.nodes[second_node].value)
        self.assertIn("shared", optimized.nodes)
        self.assertEqual(optimized.nodes["shared"].args, ("a", "b"))

    def test_checker_result_is_unchanged_when_propagation_is_enabled(self) -> None:
        builder = CircuitBuilder("opt_check")
        p = builder.input("p")
        s = builder.input("s")
        false = builder.const(False)
        true = builder.const(True)
        leak = builder.mux(p, s, false)  # p ? s : false == p AND s
        padded = builder.and_(leak, true)
        builder.output("out", padded)
        circuit = builder.build()

        plain = check_information_flow(
            circuit,
            public_inputs=["p"],
            secret_inputs=["s"],
            constant_propagation=False,
        )
        optimized = check_information_flow(
            circuit,
            public_inputs=["p"],
            secret_inputs=["s"],
            constant_propagation=True,
        )
        self.assertFalse(plain.secure)
        self.assertFalse(optimized.secure)
        self.assertEqual(
            plain.counterexample.inputs_a,
            optimized.counterexample.inputs_a,
        )
        self.assertTrue(optimized.constant_propagation_applied)
        self.assertTrue(optimized.counterexample.replay(circuit).valid)


if __name__ == "__main__":
    unittest.main()
