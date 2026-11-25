from __future__ import annotations

import unittest

from info_flow_checker import (
    CircuitBuilder,
    CircuitValidationError,
    circuit_from_dict,
    circuit_to_dict,
    evaluate,
    evaluate_nodes,
)


class CircuitCoreTests(unittest.TestCase):
    def test_gate_truth_values_and_mux_argument_order(self) -> None:
        builder = CircuitBuilder("truth")
        a = builder.input("a")
        b = builder.input("b")
        zero = builder.const(False, node_id="const_zero")
        builder.output("and", builder.and_(a, b))
        builder.output("or", builder.or_(a, b))
        builder.output("not_a", builder.not_(a))
        builder.output("mux", builder.mux(a, b, zero))
        circuit = builder.build()

        values = evaluate(circuit, {"a": True, "b": False})
        self.assertEqual((values["and"], values["or"], values["not_a"]), (False, True, False))
        # selector=a=1 -> when_true=b=false.
        self.assertEqual(values["mux"], False)

        mux_cases = CircuitBuilder("mux_truth")
        x = mux_cases.input("x")
        y = mux_cases.input("y")
        false = mux_cases.const(False)
        mux = mux_cases.mux(x, y, false)
        mux_cases.output("mux", mux)
        mux_circuit = mux_cases.build()

        self.assertEqual(evaluate(mux_circuit, {"x": False, "y": True})["mux"], False)
        self.assertEqual(evaluate(mux_circuit, {"x": True, "y": True})["mux"], True)
        self.assertEqual(evaluate(mux_circuit, {"x": True, "y": False})["mux"], False)

    def test_shared_subgraph_is_one_node_and_seen_by_all_outputs(self) -> None:
        builder = CircuitBuilder("shared")
        a = builder.input("a")
        b = builder.input("b")
        shared = builder.and_(a, b, node_id="shared_and")
        builder.output("direct", shared)
        builder.output("negated", builder.not_(shared, node_id="neg_shared"))
        circuit = builder.build()

        self.assertIn("shared_and", circuit.nodes)
        self.assertEqual([node_id for node_id in circuit.nodes].count("shared_and"), 1)
        result = evaluate(circuit, {"a": True, "b": True})
        self.assertEqual(result, {"direct": True, "negated": False})

        nodes = evaluate_nodes(circuit, {"a": False, "b": True})
        self.assertFalse(nodes["shared_and"])
        self.assertTrue(nodes["neg_shared"])

    def test_dict_roundtrip_and_compact_aliases(self) -> None:
        document = {
            "name": "json",
            "inputs": ["p", "s"],
            "nodes": [
                {"id": "g", "op": "&", "args": ["p", "s"]},
                {"id": "n", "op": "!", "arg": "g"},
                {"id": "m", "op": "mux", "condition": "p",
                 "when_true": "s", "when_false": "n"},
            ],
            "outputs": [{"name": "out", "node": "m"}],
        }
        circuit = circuit_from_dict(document)
        self.assertEqual(circuit.inputs, ("p", "s"))
        self.assertEqual(circuit.output_names, ("out",))
        result = evaluate(circuit, {"p": True, "s": False})
        # g=false, n=true, m selects s because p=true -> false.
        self.assertEqual(result["out"], False)

        restored = circuit_from_dict(circuit_to_dict(circuit))
        self.assertEqual(
            evaluate(restored, {"p": False, "s": True}),
            evaluate(circuit, {"p": False, "s": True}),
        )

    def test_cycle_and_undefined_reference_are_rejected(self) -> None:
        with self.assertRaises(CircuitValidationError):
            circuit_from_dict(
                {
                    "inputs": ["a"],
                    "nodes": [
                        {"id": "g", "op": "not", "args": ["h"]},
                        {"id": "h", "op": "not", "args": ["g"]},
                    ],
                    "outputs": {"out": "g"},
                }
            )

        with self.assertRaises(CircuitValidationError):
            circuit_from_dict(
                {
                    "inputs": ["a"],
                    "nodes": [{"id": "g", "op": "and", "args": ["a", "missing"]}],
                    "outputs": {"out": "g"},
                }
            )

    def test_missing_assignment_is_rejected(self) -> None:
        builder = CircuitBuilder("missing")
        a = builder.input("a")
        builder.output("out", builder.not_(a))
        circuit = builder.build()
        with self.assertRaises(Exception):
            evaluate(circuit, {})


if __name__ == "__main__":
    unittest.main()
