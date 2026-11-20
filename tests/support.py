"""Shared helpers for the unique-index-builder test suite.

Everything here is local, synthetic, and deterministic: no network, no
database, no wall clock.  Randomised tests use explicit seeds so failures are
reproducible.
"""

from __future__ import annotations

import unittest
from typing import Dict, Hashable, Iterable, Optional, Sequence, Tuple

from unique_index_builder import (
    PublishBlocked,
    PublishReport,
    Transaction,
    UniqueIndexBuilder,
)


def tx(seq: int, *changes) -> Transaction:
    """Shorthand for building a transaction with a fixed commit number."""

    return Transaction(commit_seq=seq, changes=tuple(changes))


def seed_builder(
    batches: Sequence[Tuple[Hashable, Sequence[Tuple[Hashable, Hashable]]]],
    transactions: Iterable[Transaction] = (),
    *,
    watermark: Optional[int] = None,
    snapshot_floor: int = 1,
) -> UniqueIndexBuilder:
    # Materialise once: the same iterable is inspected for the watermark and
    # then replayed, and a one-shot generator would silently lose the changes.
    transactions = tuple(transactions)
    if watermark is None:
        seqs = [transaction.commit_seq for transaction in transactions]
        watermark = max(seqs) if seqs else snapshot_floor - 1

    builder = UniqueIndexBuilder(
        watermark=watermark,
        expected_snapshot_batches=[batch_id for batch_id, _ in batches],
        snapshot_floor=snapshot_floor,
    )
    for batch_id, rows in batches:
        builder.add_snapshot_batch(batch_id, rows)
    for transaction in transactions:
        builder.apply_transaction(transaction)
    return builder


def blocked_report(builder: UniqueIndexBuilder) -> PublishReport:
    """Publish, expecting a block, and return the attached report."""

    try:
        builder.publish()
    except PublishBlocked as exc:
        return exc.report
    raise AssertionError("expected the publish to be blocked")


def state_after(
    base_rows: Dict[Hashable, Hashable],
    transactions: Sequence[Transaction],
    upto: int,
) -> Dict[Hashable, Hashable]:
    """A straightforward reference model used to cross-check the builder.

    It applies every transaction in commit order with no awareness of snapshot
    batch arrival timing, which is exactly the semantics the builder promises.
    """

    rows = dict(base_rows)
    for transaction in sorted(transactions, key=lambda item: item.commit_seq):
        if transaction.commit_seq > upto:
            continue
        for change in transaction.changes:
            if change.kind.value == "delete":
                rows.pop(change.primary_key, None)
            else:
                rows[change.primary_key] = change.value
    return rows


class BuilderTestCase(unittest.TestCase):
    """Common assertions shared by the behavioural tests."""

    def assertIndexMatches(
        self,
        builder: UniqueIndexBuilder,
        expected: Dict[Hashable, Hashable],
    ) -> None:
        self.assertEqual(
            builder.entries(), {value: key for key, value in expected.items()}
        )
        for key, value in expected.items():
            self.assertEqual(builder.lookup(key), value)
            self.assertEqual(builder.find_key(value), key)
