from __future__ import annotations

import dataclasses
import unittest

from blind_telemetry.errors import DuplicateReportError, InvalidCredentialError
from blind_telemetry.protocol import Collector, Device, Registrar
from blind_telemetry.randomness import ScriptedRandomSource
from blind_telemetry.rsa import DEMO_KEY_PAIR, RsaPrivateKey


class CollectorVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = DEMO_KEY_PAIR
        self.public = self.keys.public_key
        self.registrar = Registrar(
            self.keys.private_key, registered_devices={"device-a"}
        )
        self.collector = Collector(self.public)

    def make_report(
        self,
        period: int = 1,
        ticket: bytes = b"collector-ticket",
        factor: int = 13,
        measurement: bytes = b"synthetic-reading",
    ):
        rng = ScriptedRandomSource(ticket=ticket, factors=[factor])
        device = Device(
            "device-a", self.public, rng=rng, ticket_length=len(ticket)
        )
        _prepared, credential = device.request_credential(
            self.registrar, period, ticket=ticket
        )
        return credential.to_report(measurement)

    def test_valid_anonymous_report_is_accepted(self) -> None:
        report = self.make_report()
        receipt = self.collector.receive(report)
        self.assertEqual(receipt.period, 1)
        self.assertEqual(receipt.sequence, 1)
        self.assertEqual(self.collector.accepted_count(), 1)
        stored = self.collector.report_for(1, b"collector-ticket")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.measurement, b"synthetic-reading")
        # The collector-facing report has no device identifier field.
        field_names = {field.name for field in dataclasses.fields(report)}
        self.assertNotIn("device_id", field_names)

    def test_same_period_and_ticket_can_only_be_accepted_once(self) -> None:
        report = self.make_report()
        first = self.collector.receive(report)
        with self.assertRaises(DuplicateReportError) as ctx:
            self.collector.receive(report)
        self.assertEqual(ctx.exception.original_sequence, first.sequence)
        self.assertEqual(self.collector.accepted_count(), 1)

    def test_cross_period_replay_fails_signature_verification(self) -> None:
        report = self.make_report(period=1, ticket=b"replay-ticket-0001")
        self.collector.receive(report)

        replayed = dataclasses.replace(report, period=2)
        with self.assertRaises(InvalidCredentialError):
            self.collector.receive(replayed)

        self.assertEqual(self.collector.accepted_count(), 1)
        self.assertIsNone(self.collector.receipt_for(2, b"replay-ticket-0001"))
        self.assertIsNotNone(self.collector.receipt_for(1, b"replay-ticket-0001"))

    def test_tampered_ticket_or_signature_is_rejected(self) -> None:
        report = self.make_report(ticket=b"tamper-ticket-001")

        changed_ticket = dataclasses.replace(report, ticket=b"tamper-ticket-002")
        with self.assertRaises(InvalidCredentialError):
            self.collector.receive(changed_ticket)

        changed_signature = dataclasses.replace(
            report, signature=(report.signature + 1) % self.public.n
        )
        with self.assertRaises(InvalidCredentialError):
            self.collector.receive(changed_signature)

        self.assertEqual(self.collector.accepted_count(), 0)

    def test_collector_constructor_rejects_private_key(self) -> None:
        with self.assertRaises(TypeError):
            Collector(self.keys.private_key)  # type: ignore[arg-type]
        self.assertIsInstance(self.keys.private_key, RsaPrivateKey)


if __name__ == "__main__":
    unittest.main()
