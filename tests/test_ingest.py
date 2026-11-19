"""Validation and ingest acceptance tests.

Covers: a structurally valid table is accepted, malformed tables are rejected
before they can reach the state store, and immutable identity handling.
"""

from __future__ import annotations

import unittest

from helpers import at

from revocation_store import (
    ConflictingTableError,
    Reason,
    RevocationStore,
    Table,
    TableEntry,
    TableKind,
    TableValidationError,
    table_from_dict,
)


class TableValidationTests(unittest.TestCase):
    def test_valid_full_table_is_accepted(self) -> None:
        table = Table.full(
            issuer="Example CA",
            scope="root",
            number=7,
            this_update=at(0),
            next_update=at(12),
            entries=[TableEntry("0A10", Reason.KEY_COMPROMISE, at(1))],
        )
        self.assertEqual(table.kind, TableKind.FULL)
        self.assertEqual(table.base_number, 7)
        self.assertEqual(table.number, 7)

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(TableValidationError):
            Table.full(
                issuer="Example CA",
                scope="root",
                number=1,
                this_update=at(0).replace(tzinfo=None),
                next_update=at(1),
            )

    def test_expiry_must_be_after_effective_time(self) -> None:
        with self.assertRaises(TableValidationError):
            Table.full(
                issuer="Example CA",
                scope="root",
                number=1,
                this_update=at(5),
                next_update=at(5),
            )

    def test_full_table_number_must_be_its_own_baseline(self) -> None:
        with self.assertRaises(TableValidationError):
            Table(
                kind=TableKind.FULL,
                issuer="Example CA",
                scope="root",
                base_number=3,
                number=4,
                this_update=at(0),
                next_update=at(5),
            )

    def test_delta_without_positive_numbers_is_rejected(self) -> None:
        with self.assertRaises(TableValidationError):
            Table.delta(
                issuer="Example CA",
                scope="root",
                base_number=0,
                number=1,
                this_update=at(0),
                next_update=at(5),
            )

    def test_duplicate_serial_in_one_table_is_rejected(self) -> None:
        with self.assertRaises(TableValidationError):
            Table.full(
                issuer="Example CA",
                scope="root",
                number=1,
                this_update=at(0),
                next_update=at(5),
                entries=[
                    TableEntry("AA", Reason.KEY_COMPROMISE, at(1)),
                    TableEntry("AA", Reason.SUPERSEDED, at(1)),
                ],
            )

    def test_remove_from_crl_is_rejected_in_full_table(self) -> None:
        with self.assertRaises(TableValidationError):
            Table.full(
                issuer="Example CA",
                scope="root",
                number=1,
                this_update=at(0),
                next_update=at(5),
                entries=[TableEntry("AA", Reason.REMOVE_FROM_CRL, at(1))],
            )

    def test_unknown_reason_is_rejected(self) -> None:
        with self.assertRaises(TableValidationError):
            TableEntry("AA", "not-a-reason", at(1))

    def test_unknown_table_kind_is_rejected(self) -> None:
        with self.assertRaises(TableValidationError):
            table_from_dict(
                {
                    "kind": "PARTIAL",
                    "issuer": "Example CA",
                    "scope": "root",
                    "number": 1,
                    "this_update": at(0).isoformat(),
                    "next_update": at(5).isoformat(),
                    "entries": [],
                }
            )


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RevocationStore(":memory:")
        self.addCleanup(self.store.close)

    def _table(self, *, number: int = 1, serials=("AA",)) -> Table:
        return Table.full(
            issuer="Example CA",
            scope="root",
            number=number,
            this_update=at(0),
            next_update=at(24),
            entries=[TableEntry(serial, Reason.KEY_COMPROMISE, at(1)) for serial in serials],
        )

    def test_malformed_table_never_reaches_the_store(self) -> None:
        with self.assertRaises(TableValidationError):
            self.store.ingest(
                Table.full(
                    issuer="Example CA",
                    scope="root",
                    number=1,
                    this_update=at(9),
                    next_update=at(3),
                )
            )
        self.assertEqual(self.store.list_tables(), [])

    def test_identical_table_is_idempotent(self) -> None:
        first = self.store.ingest(self._table())
        second = self.store.ingest(self._table())
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.table_id, second.table_id)
        self.assertEqual(len(self.store.list_tables()), 1)

    def test_same_identity_with_different_content_is_rejected(self) -> None:
        self.store.ingest(self._table(serials=("AA",)))
        with self.assertRaises(ConflictingTableError):
            self.store.ingest(self._table(serials=("AA", "BB")))

    def test_late_old_table_is_marked_as_backfill(self) -> None:
        self.store.ingest(self._table(number=5))
        result = self.store.ingest(self._table(number=3))
        self.assertTrue(result.inserted)
        self.assertTrue(result.backfilled)
        self.assertEqual(result.latest_number, 5)

    def test_entries_round_trip(self) -> None:
        result = self.store.ingest(self._table(serials=("AA", "BB")))
        entries = self.store.table_entries(result.table_id)
        self.assertEqual([row["serial"] for row in entries], ["AA", "BB"])
        self.assertTrue(all(row["reason"] == "keyCompromise" for row in entries))


if __name__ == "__main__":
    unittest.main()
