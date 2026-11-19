"""Acceptance tests: baseline/delta selection, holds, permanence, unknowns."""

from __future__ import annotations

import unittest

from helpers import at

from revocation_store import (
    Reason,
    RevocationStore,
    Status,
    Table,
    TableEntry,
)


def full(number: int, *, start: float, end: float, entries=(), issuer="Example CA", scope="root"):
    return Table.full(
        issuer=issuer,
        scope=scope,
        number=number,
        this_update=at(start),
        next_update=at(end),
        entries=entries,
    )


def delta(number: int, *, base: int, start: float, end: float, entries=(), issuer="Example CA", scope="root"):
    return Table.delta(
        issuer=issuer,
        scope=scope,
        base_number=base,
        number=number,
        this_update=at(start),
        next_update=at(end),
        entries=entries,
    )


def revoked(serial: str, when: float, reason: Reason = Reason.KEY_COMPROMISE) -> TableEntry:
    return TableEntry(serial, reason, at(when))


def held(serial: str, when: float) -> TableEntry:
    return TableEntry(serial, Reason.CERTIFICATE_HOLD, at(when))


def removed(serial: str, when: float) -> TableEntry:
    return TableEntry(serial, Reason.REMOVE_FROM_CRL, at(when))


class BaselineSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_accepts_revoked_serial_from_baseline(self) -> None:
        self.store.ingest(full(1, start=0, end=24, entries=[revoked("A1", 1)]))
        result = self.store.lookup("Example CA", "root", "A1", at(2))
        self.assertEqual(result.status, Status.REVOKED)
        self.assertEqual(result.reason, Reason.KEY_COMPROMISE)
        self.assertEqual(result.baseline.number, 1)

    def test_accepts_not_listed_only_with_valid_baseline(self) -> None:
        self.store.ingest(full(1, start=0, end=24, entries=[revoked("A1", 1)]))
        result = self.store.lookup("Example CA", "root", "Z9", at(2))
        self.assertEqual(result.status, Status.NOT_LISTED)
        self.assertTrue(result.is_definitive_non_revocation)

    def test_unknown_when_no_baseline_covers_the_instant(self) -> None:
        self.store.ingest(full(1, start=0, end=5))
        for moment in (at(-1), at(5), at(6)):
            with self.subTest(moment=moment):
                result = self.store.lookup("Example CA", "root", "Z9", moment)
                self.assertEqual(result.status, Status.UNKNOWN)
                self.assertFalse(result.is_definitive_non_revocation)

    def test_unknown_when_scope_differs(self) -> None:
        self.store.ingest(full(1, start=0, end=24, scope="root"))
        result = self.store.lookup("Example CA", "intermediate", "Z9", at(2))
        self.assertEqual(result.status, Status.UNKNOWN)

    def test_newest_valid_baseline_wins_when_several_overlap(self) -> None:
        # Temporary state (a hold) is what the "newest baseline" rule governs;
        # permanence is asserted separately in PermanenceTests.
        self.store.ingest(full(1, start=0, end=24, entries=[held("OLD", 1)]))
        self.store.ingest(full(2, start=10, end=24, entries=[held("NEW", 10)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "NEW", at(12)).status, Status.SUSPENDED)
        # The newer baseline is the authoritative snapshot and does not carry
        # the older hold forward.
        self.assertEqual(self.store.lookup("Example CA", "root", "OLD", at(12)).status, Status.NOT_LISTED)
        # Before the newer baseline was valid, the older one was authoritative.
        self.assertEqual(self.store.lookup("Example CA", "root", "OLD", at(6)).status, Status.SUSPENDED)

    def test_expired_baseline_is_not_used(self) -> None:
        self.store.ingest(full(1, start=0, end=10, entries=[revoked("A1", 1)]))
        self.store.ingest(full(2, start=20, end=30))
        result = self.store.lookup("Example CA", "root", "Z9", at(15))
        self.assertEqual(result.status, Status.UNKNOWN)


class DeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_delta_adds_revocation_on_top_of_baseline(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(2, base=1, start=4, end=48, entries=[revoked("A2", 4)]))
        result = self.store.lookup("Example CA", "root", "A2", at(6))
        self.assertEqual(result.status, Status.REVOKED)
        self.assertEqual(result.delta.number, 2)

    def test_delta_not_effective_before_its_this_update(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(2, base=1, start=10, end=48, entries=[revoked("A2", 10)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "A2", at(5)).status, Status.NOT_LISTED)
        self.assertEqual(self.store.lookup("Example CA", "root", "A2", at(11)).status, Status.REVOKED)

    def test_delta_for_absent_baseline_is_inert(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        # Base 9 was never delivered.
        self.store.ingest(delta(2, base=9, start=2, end=48, entries=[revoked("GHOST", 2)]))
        result = self.store.lookup("Example CA", "root", "GHOST", at(6))
        self.assertEqual(result.status, Status.NOT_LISTED)
        self.assertIsNone(result.delta)

    def test_only_newest_valid_delta_is_applied(self) -> None:
        # Deltas are complete change sets relative to the baseline, so they are
        # never composed: delta 3 replaces delta 2 rather than extending it.
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(2, base=1, start=2, end=48, entries=[held("D2", 2)]))
        self.store.ingest(delta(3, base=1, start=6, end=48, entries=[held("D3", 6)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "D3", at(8)).status, Status.SUSPENDED)
        self.assertEqual(self.store.lookup("Example CA", "root", "D2", at(8)).status, Status.NOT_LISTED)

    def test_expired_delta_is_skipped_in_favour_of_older_valid_one(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(2, base=1, start=2, end=48, entries=[held("H", 2)]))
        self.store.ingest(delta(3, base=1, start=5, end=9, entries=[removed("H", 5)]))
        # While delta 3 is valid the hold is lifted...
        self.assertEqual(self.store.lookup("Example CA", "root", "H", at(6)).status, Status.NOT_LISTED)
        # ...once it expires the newest valid delta is delta 2 again.
        self.assertEqual(self.store.lookup("Example CA", "root", "H", at(10)).status, Status.SUSPENDED)

    def test_delta_for_other_baseline_does_not_apply(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(full(2, start=10, end=48))
        self.store.ingest(delta(2, base=1, start=5, end=48, entries=[held("D", 5)]))
        # While baseline 1 is selected the delta applies.
        self.assertEqual(self.store.lookup("Example CA", "root", "D", at(6)).status, Status.SUSPENDED)
        # Once baseline 2 becomes the newest valid baseline the base-1 delta is
        # out of scope, even though it is still time-valid.
        self.assertEqual(self.store.lookup("Example CA", "root", "D", at(12)).status, Status.NOT_LISTED)


class HoldRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_hold_is_suspended_not_revoked(self) -> None:
        self.store.ingest(full(1, start=0, end=48, entries=[held("H1", 1)]))
        result = self.store.lookup("Example CA", "root", "H1", at(2))
        self.assertEqual(result.status, Status.SUSPENDED)
        self.assertFalse(result.is_definitive_non_revocation)

    def test_remove_from_crl_restores_held_certificate(self) -> None:
        self.store.ingest(full(1, start=0, end=48, entries=[held("H1", 1)]))
        self.store.ingest(delta(2, base=1, start=6, end=48, entries=[removed("H1", 6)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "H1", at(4)).status, Status.SUSPENDED)
        result = self.store.lookup("Example CA", "root", "H1", at(8))
        self.assertEqual(result.status, Status.NOT_LISTED)
        self.assertTrue(result.is_definitive_non_revocation)

    def test_hold_added_by_delta_then_removed_by_later_delta(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(2, base=1, start=2, end=48, entries=[held("H1", 2)]))
        self.store.ingest(delta(3, base=3, start=4, end=48))
        self.store.ingest(delta(4, base=1, start=6, end=48, entries=[removed("H1", 6)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "H1", at(5)).status, Status.SUSPENDED)
        self.assertEqual(self.store.lookup("Example CA", "root", "H1", at(7)).status, Status.NOT_LISTED)

    def test_remove_from_crl_cannot_lift_permanent_revocation(self) -> None:
        self.store.ingest(full(1, start=0, end=48, entries=[revoked("P1", 1)]))
        self.store.ingest(delta(2, base=1, start=4, end=48, entries=[removed("P1", 4)]))
        result = self.store.lookup("Example CA", "root", "P1", at(6))
        self.assertEqual(result.status, Status.REVOKED)
        self.assertEqual(result.reason, Reason.KEY_COMPROMISE)


class PermanenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_permanent_revocation_survives_delta_omission(self) -> None:
        self.store.ingest(full(1, start=0, end=48, entries=[revoked("P1", 1)]))
        # A complete later delta that does not mention P1 must not clear it.
        self.store.ingest(delta(2, base=1, start=4, end=48, entries=[revoked("OTHER", 4)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "P1", at(6)).status, Status.REVOKED)

    def test_permanent_revocation_survives_delta_expiry(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(2, base=1, start=2, end=6, entries=[revoked("P2", 2)]))
        self.assertEqual(self.store.lookup("Example CA", "root", "P2", at(4)).status, Status.REVOKED)
        # After the delta expires the newest valid delta is gone, yet permanence holds.
        self.assertEqual(self.store.lookup("Example CA", "root", "P2", at(10)).status, Status.REVOKED)

    def test_permanent_revocation_survives_newer_baseline_omission(self) -> None:
        self.store.ingest(full(1, start=0, end=48, entries=[revoked("P3", 1)]))
        self.store.ingest(full(2, start=10, end=48, entries=[]))
        self.assertEqual(self.store.lookup("Example CA", "root", "P3", at(12)).status, Status.REVOKED)

    def test_permanent_revocation_holds_after_tables_expire(self) -> None:
        self.store.ingest(full(1, start=0, end=10, entries=[revoked("P4", 1)]))
        result = self.store.lookup("Example CA", "root", "P4", at(20))
        self.assertEqual(result.status, Status.REVOKED)
        self.assertEqual(result.source.number, 1)

    def test_hold_ends_with_newer_baseline_that_omits_it(self) -> None:
        self.store.ingest(full(1, start=0, end=48, entries=[held("H9", 1)]))
        self.store.ingest(full(2, start=10, end=48, entries=[]))
        self.assertEqual(self.store.lookup("Example CA", "root", "H9", at(12)).status, Status.NOT_LISTED)


class LateArrivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_late_old_baseline_cannot_roll_back_newer_state(self) -> None:
        self.store.ingest(full(2, start=10, end=48, entries=[held("H5", 10)]))
        self.store.ingest(full(1, start=0, end=48, entries=[revoked("P5", 1)]))
        # Newer baseline 2 remains authoritative for temporary state...
        self.assertEqual(self.store.lookup("Example CA", "root", "Z9", at(12)).status, Status.NOT_LISTED)
        self.assertEqual(self.store.lookup("Example CA", "root", "H5", at(12)).status, Status.SUSPENDED)
        # ...while a permanent revocation from the late table still holds.
        self.assertEqual(self.store.lookup("Example CA", "root", "P5", at(12)).status, Status.REVOKED)
        # The late table also supplies historical basis for earlier instants.
        self.assertEqual(self.store.lookup("Example CA", "root", "P5", at(5)).status, Status.REVOKED)
        self.assertEqual(self.store.lookup("Example CA", "root", "Z9", at(5)).status, Status.NOT_LISTED)

    def test_late_old_delta_is_historical_only(self) -> None:
        self.store.ingest(full(1, start=0, end=48))
        self.store.ingest(delta(3, base=1, start=10, end=48, entries=[held("NEW", 10)]))
        self.store.ingest(delta(2, base=1, start=4, end=48, entries=[held("OLD", 4)]))
        # The newer delta stays authoritative after the old one arrives late.
        self.assertEqual(self.store.lookup("Example CA", "root", "NEW", at(12)).status, Status.SUSPENDED)
        self.assertEqual(self.store.lookup("Example CA", "root", "OLD", at(12)).status, Status.NOT_LISTED)
        # At an earlier instant the late delta is the newest valid one, so it can
        # supply the historical basis for that time.
        self.assertEqual(self.store.lookup("Example CA", "root", "OLD", at(6)).status, Status.SUSPENDED)

    def test_arrival_order_does_not_change_answers(self) -> None:
        tables = [
            full(1, start=0, end=48, entries=[revoked("A", 1), held("B", 1)]),
            delta(2, base=1, start=4, end=48, entries=[removed("B", 4)]),
            full(2, start=20, end=48),
        ]
        forward = RevocationStore(":memory:")
        self.addCleanup(forward.close)
        reverse = RevocationStore(":memory:")
        self.addCleanup(reverse.close)
        for table in tables:
            forward.ingest(table)
        for table in reversed(tables):
            reverse.ingest(table)
        for serial in ("A", "B", "C"):
            for moment in (at(2), at(6), at(22)):
                with self.subTest(serial=serial, moment=moment):
                    self.assertEqual(
                        forward.lookup("Example CA", "root", serial, moment).status,
                        reverse.lookup("Example CA", "root", serial, moment).status,
                    )


class EvidenceScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_other_scope_baseline_cannot_prove_absence(self) -> None:
        self.store.ingest(full(1, start=0, end=48, issuer="Example CA", scope="root"))
        result = self.store.lookup("Example CA", "intermediate", "Z9", at(2))
        self.assertEqual(result.status, Status.UNKNOWN)

    def test_other_issuer_cannot_prove_absence(self) -> None:
        self.store.ingest(full(1, start=0, end=48, issuer="Example CA"))
        result = self.store.lookup("Other CA", "root", "Z9", at(2))
        self.assertEqual(result.status, Status.UNKNOWN)

    def test_unknown_is_also_returned_when_baseline_only_begins_later(self) -> None:
        self.store.ingest(full(1, start=10, end=48))
        self.assertEqual(self.store.lookup("Example CA", "root", "Z9", at(9)).status, Status.UNKNOWN)
        self.assertEqual(self.store.lookup("Example CA", "root", "Z9", at(10)).status, Status.NOT_LISTED)

    def test_result_carries_evidence_metadata(self) -> None:
        self.store.ingest(full(4, start=0, end=48))
        self.store.ingest(delta(5, base=4, start=2, end=48, entries=[revoked("E1", 2)]))
        result = self.store.lookup("Example CA", "root", "E1", at(3)).as_dict()
        self.assertEqual(result["status"], "revoked")
        self.assertEqual(result["baseline"]["kind"], "FULL")
        self.assertEqual(result["delta"]["kind"], "DELTA")
        self.assertEqual(result["delta"]["base_number"], 4)
        self.assertFalse(result["definitive_non_revocation"])


if __name__ == "__main__":
    unittest.main()
