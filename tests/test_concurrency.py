from __future__ import annotations

import math
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from blind_telemetry.errors import DuplicateReportError, PeriodConflictError
from blind_telemetry.protocol import Collector, Device, Registrar
from blind_telemetry.randomness import ScriptedRandomSource
from blind_telemetry.rsa import DEMO_KEY_PAIR, DEMO_N


def make_valid_report(period: int = 5, ticket: bytes = b"concurrent-tckt"):
    keys = DEMO_KEY_PAIR
    registrar = Registrar(keys.private_key, registered_devices={"device-a"})
    device = Device(
        "device-a",
        keys.public_key,
        rng=ScriptedRandomSource(ticket=ticket, factors=[17]),
        ticket_length=len(ticket),
    )
    _prepared, credential = device.request_credential(
        registrar, period, ticket=ticket
    )
    return registrar, credential.to_report(b"concurrent-reading")


class CollectorConcurrencyTests(unittest.TestCase):
    def test_exact_double_concurrent_submission_is_accepted_once(self) -> None:
        _registrar, report = make_valid_report(
            period=10, ticket=b"double-submit-01"
        )
        collector = Collector(DEMO_KEY_PAIR.public_key)
        barrier = threading.Barrier(2)

        def worker() -> tuple[str, int | None]:
            barrier.wait(timeout=10)
            try:
                receipt = collector.receive(report)
                return "accepted", receipt.sequence
            except DuplicateReportError as exc:
                return "duplicate", exc.original_sequence

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]

        self.assertEqual(
            sorted(status for status, _ in outcomes), ["accepted", "duplicate"]
        )
        accepted = [value for status, value in outcomes if status == "accepted"]
        duplicate = [value for status, value in outcomes if status == "duplicate"]
        self.assertEqual(accepted, [1])
        self.assertEqual(duplicate, [1])
        self.assertEqual(collector.accepted_count(), 1)

    def test_many_racing_copies_still_have_exactly_one_winner(self) -> None:
        collector = Collector(DEMO_KEY_PAIR.public_key)

        for round_index in range(5):
            ticket = f"race-ticket-{round_index:02d}".encode("ascii")
            _registrar, report = make_valid_report(
                period=20 + round_index, ticket=ticket
            )
            parties = 8
            barrier = threading.Barrier(parties)

            def worker() -> str:
                barrier.wait(timeout=10)
                try:
                    collector.receive(report)
                    return "accepted"
                except DuplicateReportError:
                    return "duplicate"

            with ThreadPoolExecutor(max_workers=parties) as executor:
                outcomes = list(executor.map(lambda _: worker(), range(parties)))

            self.assertEqual(outcomes.count("accepted"), 1)
            self.assertEqual(outcomes.count("duplicate"), parties - 1)

        self.assertEqual(collector.accepted_count(), 5)


class RegistrarConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registrar = Registrar(
            DEMO_KEY_PAIR.private_key, registered_devices={"device-a"}
        )

    def test_concurrent_identical_retries_share_one_original_reply(self) -> None:
        parties = 8
        barrier = threading.Barrier(parties)

        def worker():
            barrier.wait(timeout=10)
            return self.registrar.issue("device-a", 1, 29)

        with ThreadPoolExecutor(max_workers=parties) as executor:
            replies = list(executor.map(lambda _: worker(), range(parties)))

        first = self.registrar.get_reply("device-a", 1)
        self.assertIsNotNone(first)
        self.assertTrue(all(reply is first for reply in replies))
        self.assertEqual(self.registrar.issued_count(), 1)
        self.assertEqual(self.registrar.sign_operations, 1)

    def test_concurrent_distinct_blinds_have_one_winner(self) -> None:
        blind_values = [
            value
            for value in range(2, 40)
            if math.gcd(value, DEMO_N) == 1
        ][:8]
        self.assertEqual(len(blind_values), 8)
        barrier = threading.Barrier(len(blind_values))

        def worker(index: int) -> tuple[str, int]:
            barrier.wait(timeout=10)
            try:
                reply = self.registrar.issue("device-a", 2, blind_values[index])
                return "accepted", reply.blinded_message
            except PeriodConflictError:
                return "conflict", blind_values[index]

        with ThreadPoolExecutor(max_workers=len(blind_values)) as executor:
            outcomes = list(executor.map(worker, range(len(blind_values))))

        accepted = [item for item in outcomes if item[0] == "accepted"]
        conflicts = [item for item in outcomes if item[0] == "conflict"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(conflicts), len(blind_values) - 1)
        self.assertEqual(self.registrar.issued_count(), 1)
        self.assertEqual(
            self.registrar.get_reply("device-a", 2).blinded_message,  # type: ignore[union-attr]
            accepted[0][1],
        )


if __name__ == "__main__":
    unittest.main()
