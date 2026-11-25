from __future__ import annotations

import unittest

from blind_telemetry.errors import (
    PeriodConflictError,
    UnknownDeviceError,
    BlindMessageError,
)
from blind_telemetry.protocol import Registrar
from blind_telemetry.rsa import DEMO_KEY_PAIR, DEMO_P


class RegistrarIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = DEMO_KEY_PAIR
        self.registrar = Registrar(
            self.keys.private_key, registered_devices={"device-a"}
        )

    def test_unregistered_device_is_rejected(self) -> None:
        with self.assertRaises(UnknownDeviceError):
            self.registrar.issue("stranger", 1, 2)

    def test_exact_retry_returns_original_reply_without_signing_again(self) -> None:
        first = self.registrar.issue("device-a", 1, 2)
        second = self.registrar.issue("device-a", 1, 2)

        self.assertIs(first, second)
        self.assertEqual(first.blinded_signature, pow(2, self.keys.private_key.d, 3233))
        self.assertEqual(self.registrar.issued_count(), 1)
        self.assertEqual(self.registrar.sign_operations, 1)

    def test_different_blind_for_same_period_conflicts(self) -> None:
        first = self.registrar.issue("device-a", 1, 2)

        with self.assertRaises(PeriodConflictError):
            self.registrar.issue("device-a", 1, 3)

        # The original answer remains retrievable and retries stay idempotent.
        self.assertIs(self.registrar.issue("device-a", 1, 2), first)
        self.assertIs(self.registrar.get_reply("device-a", 1), first)
        self.assertEqual(self.registrar.sign_operations, 1)

    def test_different_periods_are_independent(self) -> None:
        period_one = self.registrar.issue("device-a", 1, 2)
        period_two = self.registrar.issue("device-a", 2, 2)
        self.assertNotEqual(period_one.period, period_two.period)
        self.assertEqual(self.registrar.issued_count(), 2)

    def test_nonunit_or_out_of_range_blind_is_rejected(self) -> None:
        with self.assertRaises(BlindMessageError):
            self.registrar.issue("device-a", 1, DEMO_P)  # shares factor with n
        with self.assertRaises(BlindMessageError):
            self.registrar.issue("device-a", 1, 0)
        with self.assertRaises(BlindMessageError):
            self.registrar.issue("device-a", 1, 3233)

    def test_dynamic_registration(self) -> None:
        with self.assertRaises(UnknownDeviceError):
            self.registrar.issue("new-device", 0, 2)
        self.registrar.register_device("new-device")
        reply = self.registrar.issue("new-device", 0, 2)
        self.assertEqual(reply.device_id, "new-device")


if __name__ == "__main__":
    unittest.main()
