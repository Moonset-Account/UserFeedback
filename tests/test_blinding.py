from __future__ import annotations

import math
import random
import unittest

from blind_telemetry.encoding import hash_to_unit
from blind_telemetry.errors import RandomSourceError
from blind_telemetry.protocol import BlindReply, Collector, Device, Registrar
from blind_telemetry.randomness import ScriptedRandomSource, random_unit_factor
from blind_telemetry.rsa import DEMO_KEY_PAIR, DEMO_N


class BlindAlgebraTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = DEMO_KEY_PAIR
        self.public = self.keys.public_key
        self.private = self.keys.private_key

    def test_raw_blind_unblind_equation(self) -> None:
        hashed = hash_to_unit(DEMO_N, period=4, ticket=b"raw-algebra-tckt")
        plaintext_signatures = set()

        for factor in (2, 3, 42):
            blinded = (
                hashed.value * pow(factor, self.public.e, DEMO_N)
            ) % DEMO_N
            blinded_signature = self.private.sign_unit(blinded)
            signature = (
                blinded_signature * pow(factor, -1, DEMO_N)
            ) % DEMO_N

            self.assertEqual(pow(signature, self.public.e, DEMO_N), hashed.value)
            plaintext_signatures.add(signature)

        # RSA signing is deterministic for this textbook demonstration, so all
        # valid blind factors recover exactly the same plaintext signature.
        self.assertEqual(len(plaintext_signatures), 1)

    def test_one_injected_source_supplies_two_distinct_factors(self) -> None:
        ticket = b"0123456789abcdef"
        rng = ScriptedRandomSource(ticket=ticket, factors=[2, 3])
        device = Device(
            "same-device",
            self.public,
            rng=rng,
            ticket_length=len(ticket),
        )

        state_one = device.prepare(period=11)
        state_two = device.prepare(period=11)

        self.assertEqual(state_one.ticket, state_two.ticket)
        self.assertEqual(state_one.hash_value, state_two.hash_value)
        self.assertEqual((state_one.blinding_factor, state_two.blinding_factor), (2, 3))
        self.assertNotEqual(state_one.blinded_message, state_two.blinded_message)

        # The registrar's one-blind-per-device-period policy is deliberately
        # bypassed here to test the cryptographic factor property directly.
        reply_one = BlindReply(
            "same-device",
            11,
            state_one.blinded_message,
            self.private.sign_unit(state_one.blinded_message),
        )
        reply_two = BlindReply(
            "same-device",
            11,
            state_two.blinded_message,
            self.private.sign_unit(state_two.blinded_message),
        )
        credential_one = device.unblind(state_one, reply_one)
        credential_two = device.unblind(state_two, reply_two)
        self.assertEqual(credential_one.signature, credential_two.signature)
        self.assertTrue(
            self.public.verify_signature_integer(
                credential_one.signature, state_one.hash_value
            )
        )

    def test_two_injected_sources_produce_same_working_credential(self) -> None:
        ticket = b"0123456789abcdef"
        registrar = Registrar(
            self.private, registered_devices={"factor-a", "factor-b"}
        )
        collector = Collector(self.public)

        device_a = Device(
            "factor-a",
            self.public,
            rng=ScriptedRandomSource(ticket=ticket, factors=[2]),
            ticket_length=len(ticket),
        )
        device_b = Device(
            "factor-b",
            self.public,
            rng=ScriptedRandomSource(ticket=ticket, factors=[3]),
            ticket_length=len(ticket),
        )

        state_a = device_a.prepare(period=8)
        state_b = device_b.prepare(period=8)

        self.assertEqual(state_a.ticket, ticket)
        self.assertEqual(state_b.ticket, ticket)
        self.assertEqual(state_a.blinding_factor, 2)
        self.assertEqual(state_b.blinding_factor, 3)
        self.assertNotEqual(state_a.blinded_message, state_b.blinded_message)

        reply_a = registrar.issue(
            "factor-a", state_a.period, state_a.blinded_message
        )
        reply_b = registrar.issue(
            "factor-b", state_b.period, state_b.blinded_message
        )
        credential_a = device_a.unblind(state_a, reply_a)
        credential_b = device_b.unblind(state_b, reply_b)

        self.assertEqual(credential_a.signature, credential_b.signature)
        self.assertTrue(
            self.public.verify_signature_integer(
                credential_a.signature, state_a.hash_value
            )
        )

        receipt = collector.receive(credential_a.to_report(b"synthetic"))
        self.assertEqual(receipt.sequence, 1)
        # Same period/ticket from factor B is the same anonymous report.
        from blind_telemetry.errors import DuplicateReportError

        with self.assertRaises(DuplicateReportError):
            collector.receive(credential_b.to_report(b"synthetic-copy"))

    def test_seeded_random_source_always_returns_unit_factor(self) -> None:
        rng = random.Random(987654321)
        for _ in range(100):
            factor = random_unit_factor(DEMO_N, rng)
            self.assertTrue(2 <= factor < DEMO_N)
            self.assertEqual(math.gcd(factor, DEMO_N), 1)

    def test_failing_injected_source_is_reported(self) -> None:
        class AlwaysNonUnit:
            def randrange(self, start: int, stop: int | None = None) -> int:
                # DEMO_P divides DEMO_N = 61 * 53.
                return 61

            def randbytes(self, size: int) -> bytes:
                return b"x" * size

        with self.assertRaises(RandomSourceError):
            random_unit_factor(DEMO_N, AlwaysNonUnit(), max_attempts=3)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
