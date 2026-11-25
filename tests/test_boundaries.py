from __future__ import annotations

import json
import unittest

from info_flow_checker import (
    CircuitBuilder,
    LimitExceededError,
    Policy,
    PolicyValidationError,
    check_information_flow,
)


def xor_node(builder: CircuitBuilder, left: str, right: str) -> str:
    either = builder.or_(left, right)
    both = builder.and_(left, right)
    not_both = builder.not_(both)
    return builder.and_(either, not_both)


class BoundaryAndPolicyTests(unittest.TestCase):
    def test_exact_eight_public_and_twelve_secret_bits_are_supported(self) -> None:
        builder = CircuitBuilder("max_boundary")
        publics = [builder.input("p" + str(i)) for i in range(8)]
        secrets = [builder.input("s" + str(i)) for i in range(12)]

        secret_parity = secrets[0]
        for bit in secrets[1:]:
            secret_parity = xor_node(builder, secret_parity, bit)

        public_parity = publics[0]
        for bit in publics[1:]:
            public_parity = xor_node(builder, public_parity, bit)

        decision = xor_node(builder, secret_parity, public_parity)
        builder.output("summary", secret_parity)
        builder.output("public_parity", public_parity)
        builder.output("decision", decision)
        circuit = builder.build()

        report = check_information_flow(
            circuit,
            public_inputs=["p" + str(i) for i in range(8)],
            secret_inputs=["s" + str(i) for i in range(12)],
            summary_outputs=["summary"],
            # Even decision = summary XOR public_parity is safe once summary
            # and the public assignment are fixed.
            external_outputs=["decision", "public_parity"],
        )

        self.assertTrue(report.secure, report.describe())
        self.assertEqual(report.public_assignment_count, 256)
        self.assertEqual(report.public_assignments_checked, 256)
        self.assertEqual(report.secret_assignment_count, 4096)
        # The complete report must be JSON-serializable for tooling/replay.
        json.dumps(report.to_dict())

    def test_maximum_boundary_can_still_return_replayable_leak(self) -> None:
        builder = CircuitBuilder("max_leak")
        publics = [builder.input("p" + str(i)) for i in range(8)]
        secrets = [builder.input("s" + str(i)) for i in range(12)]
        enabling = publics[0]
        for bit in publics[1:]:
            enabling = builder.and_(enabling, bit)
        leaked = builder.and_(enabling, secrets[3])
        builder.output("out", leaked)
        circuit = builder.build()

        report = check_information_flow(
            circuit,
            public_inputs=["p" + str(i) for i in range(8)],
            secret_inputs=["s" + str(i) for i in range(12)],
        )
        self.assertFalse(report.secure)
        pair = report.counterexample
        # First listed public is LSB; first all-ones enabling assignment under
        # that encoding is index 255.
        self.assertEqual(pair.public_index, 255)
        self.assertTrue(all(pair.public_assignment.values()))
        self.assertEqual(pair.secret_assignment_a["s3"], False)
        self.assertEqual(pair.secret_assignment_b["s3"], True)
        self.assertTrue(pair.replay(circuit).valid)
        json.dumps(pair.to_dict())

    def test_nine_public_or_thirteen_secret_bits_are_rejected(self) -> None:
        with self.assertRaises(LimitExceededError):
            Policy(public_inputs=["p" + str(i) for i in range(9)])
        with self.assertRaises(LimitExceededError):
            Policy(secret_inputs=["s" + str(i) for i in range(13)])

    def test_zero_input_domains_and_empty_labels(self) -> None:
        builder = CircuitBuilder("constants_only")
        builder.output("one", builder.const(True))
        circuit = builder.build()
        report = check_information_flow(circuit)
        self.assertTrue(report.secure)
        self.assertEqual(report.public_assignment_count, 1)
        self.assertEqual(report.secret_assignment_count, 1)

        public_only = CircuitBuilder("public_only")
        p = public_only.input("p")
        public_only.output("out", public_only.not_(p))
        public_circuit = public_only.build()
        report = check_information_flow(public_circuit, public_inputs=["p"])
        self.assertTrue(report.secure)
        self.assertEqual(report.public_assignment_count, 2)

        secret_only = CircuitBuilder("secret_only")
        s = secret_only.input("s")
        secret_only.output("out", s)
        secret_circuit = secret_only.build()
        report = check_information_flow(secret_circuit, secret_inputs=["s"])
        self.assertFalse(report.secure)
        self.assertEqual(report.public_assignment_count, 1)

    def test_incomplete_or_inconsistent_policy_is_rejected(self) -> None:
        builder = CircuitBuilder("labels")
        p = builder.input("p")
        s = builder.input("s")
        builder.output("out", builder.and_(p, s))
        circuit = builder.build()

        with self.assertRaises(PolicyValidationError):
            check_information_flow(circuit, public_inputs=["p"])  # s unlabeled

        with self.assertRaises(PolicyValidationError):
            check_information_flow(
                circuit,
                public_inputs=["p", "s"],
                secret_inputs=["s"],
            )

        with self.assertRaises(PolicyValidationError):
            check_information_flow(
                circuit,
                public_inputs=["p"],
                secret_inputs=["s"],
                summary_outputs=["missing_output"],
            )

    def test_summary_and_external_lists_may_overlap(self) -> None:
        builder = CircuitBuilder("overlap")
        s = builder.input("s")
        builder.output("summary_bit", s)
        builder.output("decision", s)
        circuit = builder.build()

        # If the only external bit is also the summary, equal summaries force
        # equal external values even though that bit reveals the secret.
        report = check_information_flow(
            circuit,
            secret_inputs=["s"],
            summary_outputs=["summary_bit"],
            external_outputs=["summary_bit"],
        )
        self.assertTrue(report.secure)

        # Adding the second copy does not add information beyond the summary.
        report = check_information_flow(
            circuit,
            secret_inputs=["s"],
            summary_outputs=["summary_bit"],
            external_outputs=["summary_bit", "decision"],
        )
        self.assertTrue(report.secure)


if __name__ == "__main__":
    unittest.main()
