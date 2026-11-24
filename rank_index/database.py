"""Transactional MVCC database facade for the compressed rank index."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any, Hashable

from .mapping import RankMapping
from .model import (
    ConcurrentUpdateError,
    DuplicateRowError,
    InvalidRowError,
    Row,
    RowNotFoundError,
    TABLES,
)
from .snapshot import Snapshot
from .tables import TableSet, initial_tables, row_from_tuple


class Transaction:
    """A multi-table atomic transaction with a private mutable draft."""

    def __init__(self, db: "RankIndex") -> None:
        self._db = db
        self.base_snapshot = db.current
        self.base_version = self.base_snapshot.version
        self.draft_rows = self.base_snapshot.tables.copy_mutable_rows()
        self.changed: dict[str, set[Hashable]] = {t: set() for t in TABLES}
        self._committed = False
        self._rolled_back = False
        self.mapping: RankMapping | None = None

    def _check_open(self) -> None:
        if self._committed:
            raise InvalidRowError("transaction already committed")
        if self._rolled_back:
            raise InvalidRowError("transaction already rolled back")

    def insert_row(self, row: Row) -> "Transaction":
        self._check_open()
        table = row.table
        if row.row_id in self.draft_rows[table]:
            raise DuplicateRowError(f"{table} row {row.row_id!r} already exists")
        self.draft_rows[table][row.row_id] = row
        self.changed[table].add(row.row_id)
        return self

    def insert(
        self,
        table: str | Row,
        row_id: Hashable | None = None,
        x: Any = None,
        y: Any = None,
        multiplicity: int = 1,
    ) -> "Transaction":
        if isinstance(table, Row):
            if row_id is not None or x is not None or y is not None:
                raise InvalidRowError("do not pass extra values when inserting a Row object")
            return self.insert_row(table)
        if row_id is None:
            raise InvalidRowError("insert requires a row_id")
        return self.insert_row(Row(table, row_id, (x, y), multiplicity))

    def upsert(
        self,
        table: str | Row,
        row_id: Hashable | None = None,
        x: Any = None,
        y: Any = None,
        multiplicity: int = 1,
    ) -> "Transaction":
        if isinstance(table, Row):
            row = table
        else:
            if row_id is None:
                raise InvalidRowError("upsert requires a row_id")
            row = Row(table, row_id, (x, y), multiplicity)
        self._check_open()
        self.draft_rows[row.table][row.row_id] = row
        self.changed[row.table].add(row.row_id)
        return self

    def delete(self, table: str, row_id: Hashable) -> "Transaction":
        self._check_open()
        if row_id not in self.draft_rows[table]:
            raise RowNotFoundError(f"{table} row {row_id!r} does not exist")
        del self.draft_rows[table][row_id]
        self.changed[table].add(row_id)
        return self

    def set_multiplicity(self, table: str, row_id: Hashable, multiplicity: int) -> "Transaction":
        self._check_open()
        old = self.draft_rows[table].get(row_id)
        if old is None:
            raise RowNotFoundError(f"{table} row {row_id!r} does not exist")
        new = Row(table, row_id, old.values, multiplicity)
        self.draft_rows[table][row_id] = new
        self.changed[table].add(row_id)
        return self

    # Descriptive alias.
    update_multiplicity = set_multiplicity

    def update_values(
        self, table: str, row_id: Hashable, x: Any, y: Any
    ) -> "Transaction":
        """Replace a row's two key values while preserving its multiplicity."""
        self._check_open()
        old = self.draft_rows[table].get(row_id)
        if old is None:
            raise RowNotFoundError(f"{table} row {row_id!r} does not exist")
        new = Row(table, row_id, (x, y), old.multiplicity)
        self.draft_rows[table][row_id] = new
        self.changed[table].add(row_id)
        return self

    def commit(self) -> RankMapping:
        if self._committed:
            assert self.mapping is not None
            return self.mapping
        self._check_open()
        mapping = self._db._commit(self)
        self._committed = True
        self.mapping = mapping
        return mapping

    def rollback(self) -> None:
        self._check_open()
        self._rolled_back = True
        self.draft_rows = {t: {} for t in TABLES}
        self.changed = {t: set() for t in TABLES}

    def __enter__(self) -> "Transaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._committed or self._rolled_back:
            return False
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class RankIndex:
    """Transactional compressed rank index for R(a,b) ⋈ S(b,c) ⋈ T(c,a)."""

    def __init__(
        self,
        R: Iterable[Row | tuple] | None = None,
        S: Iterable[Row | tuple] | None = None,
        T: Iterable[Row | tuple] | None = None,
        *,
        tables: TableSet | None = None,
    ) -> None:
        self._lock = threading.RLock()
        if tables is None:
            tables = initial_tables(R, S, T)
        self._current = Snapshot.from_tables(tables, version=0)
        self.last_mapping: RankMapping | None = None
        self.mappings: list[RankMapping] = []

    @property
    def current(self) -> Snapshot:
        with self._lock:
            return self._current

    def snapshot(self) -> Snapshot:
        return self.current

    @property
    def total(self) -> int:
        return self.current.total

    def transaction(self) -> Transaction:
        return Transaction(self)

    def insert(self, *args, **kwargs) -> RankMapping:
        """Convenience autocommit insert."""
        with self.transaction() as tx:
            tx.insert(*args, **kwargs)
            return tx.commit()

    def set_multiplicity(self, *args, **kwargs) -> RankMapping:
        with self.transaction() as tx:
            tx.set_multiplicity(*args, **kwargs)
            return tx.commit()

    update_multiplicity = set_multiplicity

    def _commit(self, tx: Transaction) -> RankMapping:
        with self._lock:
            if tx.base_version != self._current.version:
                raise ConcurrentUpdateError(
                    f"transaction based on v{tx.base_version} cannot commit over v{self._current.version}"
                )

            old_snapshot = self._current
            new_tables = TableSet(tx.draft_rows)
            affected: set[tuple[Any, Any, Any]] = set()

            # Expand each changed physical edge against the state in which
            # that edge exists.  A triangle formed by simultaneous inserts is
            # therefore discovered once as one closed key group.
            for table in TABLES:
                for row_id in tx.changed[table]:
                    old_row = old_snapshot.tables.get(table, row_id)
                    if old_row is not None:
                        affected.update(
                            old_snapshot.tables.closed_keys_for_row(old_row)
                        )
                    new_row = new_tables.get(table, row_id)
                    if new_row is not None:
                        affected.update(new_tables.closed_keys_for_row(new_row))

            new_snapshot = old_snapshot.with_tables(
                new_tables, affected, old_snapshot.version + 1
            )
            mapping = RankMapping.build(old_snapshot, new_snapshot, affected)
            self._current = new_snapshot
            self.last_mapping = mapping
            self.mappings.append(mapping)
            return mapping
