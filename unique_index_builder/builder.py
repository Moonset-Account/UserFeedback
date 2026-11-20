"""Segmented, retry-friendly builder for a unique index over a live table.

The builder supports the database pattern where a unique index is created
online while the table keeps accepting writes:

1. A **base snapshot** is scanned in independent batches.  Batches may be
   delivered in any order and may lag behind newer transactions that touch the
   same rows.
2. **Transactions** with consecutive commit numbers are replayed on top.  A
   transaction may be submitted before the snapshot batch that contains the
   rows it modifies.  All changes are treated as *post-images*: the latest
   change for a primary key wins, regardless of when its snapshot batch shows
   up.  This makes the result independent of scan-batch arrival order.
3. A **publish watermark** is supplied up front.  Transactions above the
   watermark are buffered and must not influence the published index.
4. :meth:`UniqueIndexBuilder.publish` only succeeds when every expected
   snapshot batch arrived, every commit number between the snapshot floor and
   the watermark was received, and no index value is duplicated in the state
   as of the watermark.

The builder is deliberately pull/segment driven: callers feed one batch or one
transaction per call and ``poll`` pending input between calls.  This mirrors a
replication loop and keeps memory bounded by "snapshot plus change log".
There is no background thread, no I/O, and no dependency outside the Python
standard library.

Publication is atomic from a reader's point of view.  Before publish, lookups
raise :class:`~unique_index_builder.errors.IndexNotPublishedError`.  Publish
builds the complete maps first and then swaps them in, so a reader observes
either "not published" or the whole new index, never a half-filled map.
"""

from __future__ import annotations

import bisect
from typing import Any, Dict, Hashable, Iterable, Iterator, List, Mapping, Optional, Tuple

from .errors import (
    BuilderSealedError,
    ConfigurationError,
    DuplicateSnapshotRowError,
    IndexNotPublishedError,
    InvalidSnapshotRowError,
    InvalidTransactionError,
    PublishBlocked,
    SnapshotBatchConflictError,
    StaleTransactionError,
    TransactionConflictError,
    UnknownSnapshotBatchError,
)
from .model import (
    ChangeKind,
    PublishReport,
    SnapshotRow,
    SnapshotSegment,
    Transaction,
    UniqueConflict,
    _sort_key,
)


class UniqueIndexBuilder:
    """Build a unique index from a batched snapshot plus a change log.

    :param watermark: The publish watermark.  Transactions with a commit
        sequence greater than this value are buffered and are not required for
        publication.
    :param snapshot_floor: The first commit sequence number that is *not*
        contained in the base snapshot.  Changes with a smaller sequence number
        belong to the snapshot and are rejected as stale.  Defaults to ``1``,
        so ``commit_seq`` values are expected to start at 1.
    :param expected_snapshot_batches: Iterable of the batch identifiers that
        the snapshot scanner is expected to produce.  Delivery order is
        irrelevant, but every identifier must eventually arrive.
    """

    def __init__(
        self,
        watermark: int,
        expected_snapshot_batches: Iterable[Hashable],
        snapshot_floor: int = 1,
    ) -> None:
        self._watermark = self._require_int(watermark, "watermark")
        self._snapshot_floor = self._require_int(snapshot_floor, "snapshot_floor")

        if self._snapshot_floor < 0:
            raise ConfigurationError("snapshot_floor must be >= 0")
        if self._snapshot_floor > self._watermark + 1:
            raise ConfigurationError(
                "snapshot_floor ({}) must be <= watermark + 1 ({})".format(
                    self._snapshot_floor, self._watermark + 1
                )
            )

        expected = self._normalize_batch_ids(expected_snapshot_batches)
        # An empty manifest is meaningful: it declares that the base snapshot
        # has no batches (for example, an empty table).  The builder then only
        # needs a gapless commit log up to the watermark.
        self._expected_batches: Tuple[Hashable, ...] = expected
        self._expected_batch_set = frozenset(expected)

        # Snapshot side.
        self._arrived_batches: Dict[Hashable, Dict[Hashable, Hashable]] = {}
        self._base_rows: Dict[Hashable, Hashable] = {}

        # Change-log side.
        self._transactions: Dict[int, Transaction] = {}
        self._sorted_seqs: List[int] = []
        self._polled: List[Transaction] = []

        # Published state.
        self._published = False
        self._published_at: Optional[int] = None
        self._by_key: Dict[Hashable, Hashable] = {}
        self._index: Dict[Hashable, Hashable] = {}
        self._covered_commits: range = range(0)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _require_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(
                "{} must be an int, got {!r}".format(label, value)
            )
        return value

    @staticmethod
    def _normalize_batch_ids(batch_ids: Iterable[Hashable]) -> Tuple[Hashable, ...]:
        if isinstance(batch_ids, (str, bytes)):
            raise ConfigurationError(
                "expected_snapshot_batches must be an iterable of batch ids, "
                "not a single string"
            )
        try:
            raw = list(batch_ids)
        except TypeError as exc:
            raise ConfigurationError(
                "expected_snapshot_batches must be iterable"
            ) from exc

        seen: Dict[Hashable, None] = {}
        for batch_id in raw:
            if batch_id is None:
                raise ConfigurationError("snapshot batch ids must not be None")
            try:
                hash(batch_id)
            except TypeError as exc:
                raise ConfigurationError(
                    "snapshot batch ids must be hashable: {!r}".format(batch_id)
                ) from exc
            if batch_id in seen:
                raise ConfigurationError(
                    "duplicate snapshot batch id in manifest: {!r}".format(batch_id)
                )
            seen[batch_id] = None

        return tuple(sorted(seen, key=_sort_key))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def watermark(self) -> int:
        return self._watermark

    @property
    def snapshot_floor(self) -> int:
        return self._snapshot_floor

    @property
    def published(self) -> bool:
        return self._published

    @property
    def published_at(self) -> Optional[int]:
        return None if self._published_at is None else self._published_at

    @property
    def expected_snapshot_batches(self) -> Tuple[Hashable, ...]:
        return self._expected_batches

    @property
    def arrived_snapshot_batches(self) -> Tuple[Hashable, ...]:
        return tuple(sorted(self._arrived_batches, key=_sort_key))

    @property
    def missing_snapshot_batches(self) -> Tuple[Hashable, ...]:
        missing = self._expected_batch_set.difference(self._arrived_batches)
        return tuple(sorted(missing, key=_sort_key))

    @property
    def snapshot_complete(self) -> bool:
        return not self.missing_snapshot_batches

    @property
    def received_commit_seqs(self) -> Tuple[int, ...]:
        return tuple(self._sorted_seqs)

    @property
    def covered_commit_seqs(self) -> range:
        """Commit numbers reflected in the published index.

        Returned as a ``range`` so a large watermark does not materialise a
        huge tuple.  It is empty (``range(0)``) until the index is published.
        """

        return self._covered_commits

    def missing_commit_seqs(self, upto: Optional[int] = None) -> Tuple[int, ...]:
        """Consecutive commit numbers in ``[snapshot_floor, upto]`` not yet seen.

        The walk follows the already-received commits rather than the whole
        interval, so a large watermark with a small gap stays cheap until the
        gap itself is expanded.
        """

        ceiling = self._resolve_upto(upto)
        missing: List[int] = []
        expected = self._snapshot_floor
        for seq in self._sorted_seqs:
            if seq > ceiling:
                break
            if seq < expected:
                continue
            if seq > expected:
                missing.extend(range(expected, seq))
            expected = seq + 1
        if expected <= ceiling:
            missing.extend(range(expected, ceiling + 1))
        return tuple(missing)

    # ------------------------------------------------------------------
    # Snapshot ingestion
    # ------------------------------------------------------------------
    def add_snapshot_batch(
        self,
        batch_id: Hashable,
        rows: Iterable[Any],
    ) -> None:
        """Ingest one snapshot batch.

        ``rows`` accepts :class:`SnapshotRow` instances or ``(key, value)``
        pairs.  Delivering the same batch id twice with identical content is a
        no-op, which makes retries safe.  Redelivery with different content,
        an undeclared batch id, or a primary key that already exists in
        another batch raises an error instead of silently overwriting data.
        """

        self._ensure_not_published("add_snapshot_batch")

        if batch_id is None:
            raise UnknownSnapshotBatchError("snapshot batch id must not be None")
        try:
            hash(batch_id)
        except TypeError as exc:
            raise UnknownSnapshotBatchError(
                "snapshot batch id must be hashable: {!r}".format(batch_id)
            ) from exc
        if batch_id not in self._expected_batch_set:
            raise UnknownSnapshotBatchError(
                "snapshot batch {!r} was not declared in the manifest".format(batch_id)
            )

        normalized = self._normalize_rows(rows)

        previous = self._arrived_batches.get(batch_id)
        if previous is not None:
            if previous == normalized:
                return
            raise SnapshotBatchConflictError(
                "snapshot batch {!r} was redelivered with different contents".format(
                    batch_id
                )
            )

        for key in normalized:
            if key in self._base_rows:
                raise DuplicateSnapshotRowError(
                    "primary key {!r} appears in more than one snapshot batch".format(
                        key
                    )
                )

        self._arrived_batches[batch_id] = normalized
        self._base_rows.update(normalized)

    def _normalize_rows(self, rows: Iterable[Any]) -> Dict[Hashable, Hashable]:
        if isinstance(rows, (str, bytes)):
            raise InvalidSnapshotRowError("rows must be an iterable of rows")
        try:
            iterator = iter(rows)
        except TypeError as exc:
            raise InvalidSnapshotRowError("rows must be iterable") from exc

        result: Dict[Hashable, Hashable] = {}
        for row in iterator:
            key, value = self._normalize_row(row)
            if key in result:
                raise DuplicateSnapshotRowError(
                    "primary key {!r} occurs twice in the same snapshot batch".format(
                        key
                    )
                )
            result[key] = value
        return result

    @staticmethod
    def _normalize_row(row: Any) -> Tuple[Hashable, Hashable]:
        if isinstance(row, SnapshotRow):
            return row.primary_key, row.value
        if isinstance(row, (tuple, list)):
            if len(row) != 2:
                raise InvalidSnapshotRowError(
                    "snapshot row tuple must be (primary_key, value), got {!r}".format(
                        row
                    )
                )
            validated = SnapshotRow(row[0], row[1])
            return validated.primary_key, validated.value
        raise InvalidSnapshotRowError(
            "unsupported snapshot row {!r}; expected SnapshotRow or a pair".format(row)
        )

    # ------------------------------------------------------------------
    # Transaction ingestion
    # ------------------------------------------------------------------
    def apply_transaction(self, transaction: Transaction) -> None:
        """Submit one committed transaction.

        Transactions may arrive in any order relative to snapshot batches and
        relative to each other.  Re-submitting an identical transaction is a
        no-op; reusing a commit number for different content is rejected.
        """

        self._ensure_not_published("apply_transaction")

        if not isinstance(transaction, Transaction):
            raise InvalidTransactionError(
                "expected a Transaction, got {!r}".format(transaction)
            )
        seq = transaction.commit_seq
        if seq < self._snapshot_floor:
            raise StaleTransactionError(
                "commit {} is older than the snapshot floor {}".format(
                    seq, self._snapshot_floor
                )
            )

        existing = self._transactions.get(seq)
        if existing is not None:
            if existing == transaction:
                return
            raise TransactionConflictError(
                "commit {} was already recorded with different contents".format(seq)
            )

        self._transactions[seq] = transaction
        self._insert_sorted_seq(seq)
        self._polled.append(transaction)

    def _insert_sorted_seq(self, seq: int) -> None:
        """Keep ``_sorted_seqs`` ordered without re-sorting on every replay.

        Commit numbers usually arrive nearly in order, so an insertion at the
        tail is the common case.  A binary search keeps out-of-order arrivals
        cheap while preserving deterministic replay order.
        """

        position = bisect.bisect_left(self._sorted_seqs, seq)
        self._sorted_seqs.insert(position, seq)

    def poll_transactions(self) -> Tuple[Transaction, ...]:
        """Return transactions submitted since the previous poll, in commit order."""

        if not self._polled:
            return ()
        drained = tuple(sorted(self._polled, key=lambda tx: tx.commit_seq))
        self._polled = []
        return drained

    def drive(self, segments: Iterable[Any]) -> Tuple[Transaction, ...]:
        """Ingest an arbitrary stream of segments and return drained transactions.

        ``segments`` may mix :class:`SnapshotSegment` and
        :class:`Transaction` objects in any order.  This is the entry point for
        a replication loop that pulls one segment at a time from upstream;
        publish preconditions are checked separately via :meth:`check_publish`
        so the loop can decide when the watermark has been fully covered.
        """

        for segment in segments:
            if isinstance(segment, SnapshotSegment):
                self.add_snapshot_batch(segment.batch_id, segment.rows)
            elif isinstance(segment, Transaction):
                self.apply_transaction(segment)
            else:
                raise InvalidTransactionError(
                    "unsupported segment {!r}; expected SnapshotSegment or "
                    "Transaction".format(segment)
                )
        return self.poll_transactions()

    # ------------------------------------------------------------------
    # Publish validation
    # ------------------------------------------------------------------
    def check_publish(self, upto: Optional[int] = None) -> PublishReport:
        """Evaluate whether the index can be published at ``upto``.

        This is a pure check: it never mutates state and can be called as many
        times as needed while segments are still streaming in.  ``upto`` must
        not exceed the watermark configured at construction.
        """

        ceiling = self._resolve_upto(upto)
        return self._evaluate(ceiling)[0]

    def try_publish(self, upto: Optional[int] = None) -> PublishReport:
        """Return the publish report without raising on blocked preconditions."""

        return self.check_publish(upto)

    def publish(self, upto: Optional[int] = None) -> PublishReport:
        """Atomically publish the unique index at ``upto``.

        Raises :class:`~unique_index_builder.errors.PublishBlocked` with a
        :class:`PublishReport` attached when input is missing or duplicate
        values exist at the watermark.  After a block, callers may keep
        submitting the missing batches/commits and retry.
        """

        if self._published:
            return self.check_publish(upto)

        ceiling = self._resolve_upto(upto)
        report, rows = self._evaluate(ceiling)
        if report.blocked:
            raise PublishBlocked(report)

        # ``rows`` is already known to be injective: every value has exactly
        # one holder, otherwise ``_evaluate`` would have reported a conflict.
        by_value: Dict[Hashable, Hashable] = {}
        for key in sorted(rows, key=_sort_key):
            by_value[rows[key]] = key

        # Two reference swaps with no interpolation point for readers: they
        # observe either "not published" or the complete, consistent pair of
        # maps.  No intermediate half-filled dict is ever reachable.
        self._by_key = rows
        self._index = by_value
        self._published = True
        self._published_at = ceiling
        self._covered_commits = range(self._snapshot_floor, ceiling + 1)
        return PublishReport(
            published=True,
            watermark=ceiling,
            snapshot_complete=True,
        )

    def _evaluate(
        self, ceiling: int
    ) -> Tuple[PublishReport, Dict[Hashable, Hashable]]:
        if self._published:
            report = PublishReport(
                published=True,
                watermark=self._published_at
                if self._published_at is not None
                else ceiling,
                snapshot_complete=True,
            )
            return report, self._by_key

        missing_batches = self.missing_snapshot_batches
        missing_commits = self.missing_commit_seqs(ceiling)
        rows, conflicts = self._state_at(ceiling)

        published = not missing_batches and not missing_commits and not conflicts
        return (
            PublishReport(
                published=published,
                watermark=ceiling,
                snapshot_complete=not missing_batches,
                missing_snapshot_batches=missing_batches,
                missing_commit_seqs=missing_commits,
                conflicts=tuple(conflicts),
            ),
            rows,
        )

    def _state_at(
        self, upto: int
    ) -> Tuple[Dict[Hashable, Hashable], List[UniqueConflict]]:
        """Replay the state visible at ``upto`` in commit order.

        Snapshot rows are applied first (they are the pre-snapshot image),
        then changes with ``snapshot_floor <= commit_seq <= upto`` are applied
        as post-images in ascending commit order.  Snapshot batch arrival order
        therefore cannot influence the result.
        """

        rows: Dict[Hashable, Hashable] = dict(self._base_rows)
        for seq in self._sorted_seqs:
            if seq > upto:
                break
            if seq < self._snapshot_floor:
                continue
            for change in self._transactions[seq].changes:
                if change.kind is ChangeKind.DELETE:
                    rows.pop(change.primary_key, None)
                else:
                    rows[change.primary_key] = change.value
        return rows, self._find_conflicts(rows)

    @staticmethod
    def _find_conflicts(rows: Mapping[Hashable, Hashable]) -> List[UniqueConflict]:
        holders: Dict[Hashable, List[Hashable]] = {}
        for key in sorted(rows, key=_sort_key):
            holders.setdefault(rows[key], []).append(key)
        conflicts = [
            UniqueConflict(value=value, primary_keys=tuple(keys))
            for value, keys in holders.items()
            if len(keys) > 1
        ]
        conflicts.sort(key=lambda conflict: _sort_key(conflict.value))
        return conflicts

    def _resolve_upto(self, upto: Optional[int]) -> int:
        if upto is None:
            return self._watermark
        ceiling = self._require_int(upto, "upto")
        if ceiling < self._snapshot_floor - 1:
            raise ConfigurationError(
                "upto ({}) must be >= snapshot_floor - 1 ({})".format(
                    ceiling, self._snapshot_floor - 1
                )
            )
        if ceiling > self._watermark:
            raise ConfigurationError(
                "upto ({}) must not exceed the watermark ({})".format(
                    ceiling, self._watermark
                )
            )
        return ceiling

    def _ensure_not_published(self, operation: str) -> None:
        if self._published:
            raise BuilderSealedError(
                "{} is not allowed: the index was published at watermark {}".format(
                    operation, self._published_at
                )
            )

    # ------------------------------------------------------------------
    # Published index access
    # ------------------------------------------------------------------
    def _require_published(self) -> None:
        if not self._published:
            raise IndexNotPublishedError(
                "the unique index is not published yet; call publish() first"
            )

    def lookup(self, primary_key: Hashable, default: Any = None) -> Any:
        """Return the current indexed value for ``primary_key`` or ``default``.

        A value that was overwritten or whose row was deleted cannot be found
        here; :meth:`find_key` on the old value returns ``default`` as well.
        """

        self._require_published()
        return self._by_key.get(primary_key, default)

    def find_key(self, value: Hashable, default: Any = None) -> Any:
        """Return the primary key holding ``value`` in the published index.

        ``default`` is returned for an old value that no longer exists at the
        watermark, so callers never observe a stale value -> key mapping.
        """

        self._require_published()
        return self._index.get(value, default)

    def entries(self) -> Mapping[Hashable, Hashable]:
        """Return a defensive copy of the published value -> key mapping.

        Mutating the returned dict cannot affect the index.
        """

        self._require_published()
        return dict(self._index)

    def __len__(self) -> int:
        self._require_published()
        return len(self._index)

    def __contains__(self, value: object) -> bool:
        self._require_published()
        return value in self._index

    def __iter__(self) -> Iterator[Hashable]:
        self._require_published()
        return iter(self._index)
