"""SQLite-backed local revocation state store.

Contract implemented here (see README for the user-facing version):

* Tables are stored append-only and are immutable.  A table identity
  ``(kind, issuer, scope, base_number, number)`` re-sent with identical content
  is an idempotent no-op; re-sent with different content is a conflict.
* A DELTA only ever applies to the FULL baseline named by its ``base_number``.
  A delta whose baseline is absent is retained but inert.  Each delta is the
  *complete* change set relative to that baseline, so a query never chains
  deltas together - it uses the single newest valid delta.
* Within one baseline lineage (baseline + its deltas) a permanent revocation is
  sticky: it survives delta expiry, delta omission and any arrival order.
  ``removeFromCRL`` only lifts ``certificateHold``; it can never lift a
  permanent reason.
* Permanence is a monotonic overlay for the certificate lifecycle
  (issuer + serial): once a permanent reason is recorded by an interpretable
  table with an effective time at or before the query instant, no later table,
  newer full baseline, delta omission, table expiry or arrival order can clear
  it.  A table that omits a permanently revoked certificate is treated as
  inconsistent and cannot roll the revocation back.
* A newer full baseline is a new authoritative snapshot for *non-permanent*
  state.  If it omits a serial that was only temporarily suspended, the hold
  ends.  Permanence is the exception above and always wins.
* Queries select, among same-scope tables valid at the query instant, the
  baseline with the highest number and that baseline's newest valid delta.
  Arrival order never influences the answer, so a late old table can supply
  historical basis but can never roll back a newer state.
* ``not_listed`` is reported only when such a complete, valid, same-scope
  baseline was actually selected; otherwise the answer is ``unknown``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .model import (
    ConflictingTableError,
    Evidence,
    IngestResult,
    QueryResult,
    Reason,
    Status,
    Table,
    TableKind,
    TableValidationError,
    from_microseconds,
    isoformat_utc,
    to_microseconds,
    to_utc,
)

_UTC = timezone.utc

_PERMANENT_REASONS = tuple(sorted(r.value for r in Reason if r.is_permanent))
_PERMANENT_PLACEHOLDERS = ", ".join("?" for _ in _PERMANENT_REASONS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL,
    issuer         TEXT    NOT NULL,
    scope          TEXT    NOT NULL,
    base_number    INTEGER NOT NULL,
    number         INTEGER NOT NULL,
    this_update_us INTEGER NOT NULL,
    next_update_us INTEGER NOT NULL,
    fingerprint    TEXT    NOT NULL,
    received_at    TEXT    NOT NULL,
    UNIQUE (kind, issuer, scope, base_number, number)
);

CREATE TABLE IF NOT EXISTS entries (
    table_id           INTEGER NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    serial             TEXT    NOT NULL,
    reason             TEXT    NOT NULL,
    revocation_time_us INTEGER,
    PRIMARY KEY (table_id, serial)
);

CREATE INDEX IF NOT EXISTS idx_tables_full_lookup
    ON tables (kind, issuer, scope, this_update_us, next_update_us, number);
CREATE INDEX IF NOT EXISTS idx_tables_delta_lookup
    ON tables (kind, issuer, scope, base_number, this_update_us, next_update_us, number);
CREATE INDEX IF NOT EXISTS idx_entries_serial
    ON entries (serial, reason);
"""


class RevocationStore:
    """A local, persistent store of full and delta revocation tables."""

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RevocationStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- ingest -----------------------------------------------------------
    def ingest(self, table: Table) -> IngestResult:
        """Store a validated table.

        Raises :class:`TableValidationError` for structurally invalid input and
        :class:`ConflictingTableError` when an already-known table identity is
        re-sent with different content (a fork or a rewrite attempt).
        """
        if not isinstance(table, Table):
            raise TableValidationError(
                f"ingest expects a Table instance, got {type(table)!r}"
            )

        existing = self._identity_row(table)
        if existing is not None:
            if existing["fingerprint"] == table.fingerprint():
                return IngestResult(
                    table_id=int(existing["id"]),
                    inserted=False,
                    backfilled=False,
                    latest_number=self._latest_number(
                        table.kind, table.issuer, table.scope, table.number
                    ),
                    explanation="identical table already stored; ingest is idempotent",
                )
            raise ConflictingTableError(
                "table identity "
                f"(kind={table.kind.value}, issuer={table.issuer!r}, "
                f"scope={table.scope!r}, base_number={table.base_number}, "
                f"number={table.number}) already exists with different content"
            )

        if table.kind is TableKind.DELTA:
            base = self._full_row(table.issuer, table.scope, table.base_number)
            if base is not None and table.this_update < from_microseconds(
                base["this_update_us"]
            ):
                raise TableValidationError(
                    "delta this_update is earlier than the baseline it extends"
                )

        previous_max = self._max_number(table.kind, table.issuer, table.scope)
        backfilled = previous_max is not None and table.number < previous_max

        received_at = datetime.now(_UTC).isoformat().replace("+00:00", "Z")
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO tables (
                    kind, issuer, scope, base_number, number,
                    this_update_us, next_update_us, fingerprint, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    table.kind.value,
                    table.issuer,
                    table.scope,
                    table.base_number,
                    table.number,
                    to_microseconds(table.this_update),
                    to_microseconds(table.next_update),
                    table.fingerprint(),
                    received_at,
                ),
            )
            table_id = int(cursor.lastrowid)
            self._conn.executemany(
                """
                INSERT INTO entries (table_id, serial, reason, revocation_time_us)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        table_id,
                        entry.serial,
                        entry.reason.value,
                        to_microseconds(entry.revocation_time)
                        if entry.revocation_time is not None
                        else None,
                    )
                    for entry in table.entries
                ],
            )

        latest = self._latest_number(
            table.kind, table.issuer, table.scope, table.number
        )
        explanation = (
            "stored as historical backfill; it cannot override a newer valid table"
            if backfilled
            else "stored"
        )
        return IngestResult(
            table_id=table_id,
            inserted=True,
            backfilled=backfilled,
            latest_number=latest,
            explanation=explanation,
        )

    # -- query ------------------------------------------------------------
    def lookup(
        self,
        issuer: str,
        scope: str,
        serial: str,
        at: datetime,
    ) -> QueryResult:
        """Resolve the status of ``serial`` for ``issuer``/``scope`` at ``at``."""
        if not isinstance(issuer, str) or not issuer.strip():
            raise TableValidationError("issuer must be a non-empty string")
        if not isinstance(scope, str) or not scope.strip():
            raise TableValidationError("scope must be a non-empty string")
        if not isinstance(serial, str) or not serial.strip():
            raise TableValidationError("serial must be a non-empty string")
        moment = to_utc(at, "at")
        at_us = to_microseconds(moment)

        baseline_row = self._valid_baseline(issuer, scope, at_us)
        delta_row = (
            self._valid_delta(baseline_row, at_us) if baseline_row is not None else None
        )
        baseline_evidence = self._evidence(baseline_row)
        delta_evidence = self._evidence(delta_row)

        # Permanence is a monotonic overlay for the certificate lifecycle.  It is
        # checkable without a currently valid baseline: a permanent revocation is
        # affirmative evidence that never has to be "proven absent".
        permanent = self._lifecycle_permanent_revocation(issuer, serial, at_us)
        if permanent is not None:
            reason, source_evidence = permanent
            return QueryResult(
                status=Status.REVOKED,
                issuer=issuer,
                scope=scope,
                serial=serial,
                at=moment,
                baseline=baseline_evidence,
                delta=delta_evidence,
                reason=reason,
                source=source_evidence,
                explanation=(
                    "permanently revoked earlier in this certificate lifecycle; "
                    "newer baselines, delta expiry, omission and arrival order "
                    "cannot roll this back"
                ),
            )

        # Absence ("not listed") is the only answer that requires complete,
        # still-valid, same-scope evidence.  Without it the answer is unknown.
        if baseline_row is None:
            return QueryResult(
                status=Status.UNKNOWN,
                issuer=issuer,
                scope=scope,
                serial=serial,
                at=moment,
                explanation=(
                    "no full baseline for this issuer/scope is valid at the query "
                    "time, so non-listing cannot be proven"
                ),
            )

        base_entry = self._entry(baseline_row["id"], serial)
        delta_entry = (
            self._entry(delta_row["id"], serial) if delta_row is not None else None
        )

        # The newest valid delta is a complete change set relative to the
        # baseline: an effective delta entry replaces the baseline entry for
        # this serial; otherwise the baseline entry stands.
        entry = None
        entry_source = None
        if delta_entry is not None and self._effective_us(delta_entry, delta_row) <= at_us:
            entry = delta_entry
            entry_source = delta_row
        elif base_entry is not None and self._effective_us(base_entry, baseline_row) <= at_us:
            entry = base_entry
            entry_source = baseline_row

        if entry is None:
            return QueryResult(
                status=Status.NOT_LISTED,
                issuer=issuer,
                scope=scope,
                serial=serial,
                at=moment,
                baseline=baseline_evidence,
                delta=delta_evidence,
                explanation=(
                    "complete, still-valid baseline for this scope does not list "
                    "the certificate"
                ),
            )

        reason = Reason(entry["reason"])
        source_evidence = self._evidence(entry_source)
        if reason.is_removal:
            return QueryResult(
                status=Status.NOT_LISTED,
                issuer=issuer,
                scope=scope,
                serial=serial,
                at=moment,
                baseline=baseline_evidence,
                delta=delta_evidence,
                explanation=(
                    "the newest valid delta lifts the temporary suspension with "
                    "removeFromCRL"
                ),
            )
        if reason.is_hold:
            return QueryResult(
                status=Status.SUSPENDED,
                issuer=issuer,
                scope=scope,
                serial=serial,
                at=moment,
                baseline=baseline_evidence,
                delta=delta_evidence,
                reason=reason,
                source=source_evidence,
                explanation=(
                    "temporarily suspended (certificateHold); a later removeFromCRL "
                    "may restore it"
                ),
            )
        return QueryResult(
            status=Status.REVOKED,
            issuer=issuer,
            scope=scope,
            serial=serial,
            at=moment,
            baseline=baseline_evidence,
            delta=delta_evidence,
            reason=reason,
            source=source_evidence,
            explanation="listed with a permanent revocation reason",
        )

    # -- introspection ----------------------------------------------------
    def list_tables(
        self,
        issuer: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[dict]:
        clauses = []
        args: list[object] = []
        if issuer is not None:
            clauses.append("issuer = ?")
            args.append(issuer)
        if scope is not None:
            clauses.append("scope = ?")
            args.append(scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT * FROM tables{where}
            ORDER BY issuer, scope, kind, base_number, number
            """,
            args,
        ).fetchall()
        return [
            {
                "table_id": row["id"],
                "kind": row["kind"],
                "issuer": row["issuer"],
                "scope": row["scope"],
                "base_number": row["base_number"],
                "number": row["number"],
                "this_update": isoformat_utc(from_microseconds(row["this_update_us"])),
                "next_update": isoformat_utc(from_microseconds(row["next_update_us"])),
                "fingerprint": row["fingerprint"],
                "received_at": row["received_at"],
            }
            for row in rows
        ]

    def table_entries(self, table_id: int) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT serial, reason, revocation_time_us
            FROM entries WHERE table_id = ?
            ORDER BY serial
            """,
            (table_id,),
        ).fetchall()
        return [
            {
                "serial": row["serial"],
                "reason": row["reason"],
                "revocation_time": (
                    isoformat_utc(from_microseconds(row["revocation_time_us"]))
                    if row["revocation_time_us"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    # -- internals --------------------------------------------------------
    def _identity_row(self, table: Table) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM tables
            WHERE kind = ? AND issuer = ? AND scope = ?
              AND base_number = ? AND number = ?
            """,
            (
                table.kind.value,
                table.issuer,
                table.scope,
                table.base_number,
                table.number,
            ),
        ).fetchone()

    def _full_row(
        self, issuer: str, scope: str, number: int
    ) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM tables
            WHERE kind = 'FULL' AND issuer = ? AND scope = ? AND number = ?
            """,
            (issuer, scope, number),
        ).fetchone()

    def _max_number(self, kind: TableKind, issuer: str, scope: str) -> Optional[int]:
        row = self._conn.execute(
            """
            SELECT MAX(number) AS max_number FROM tables
            WHERE kind = ? AND issuer = ? AND scope = ?
            """,
            (kind.value, issuer, scope),
        ).fetchone()
        return row["max_number"]

    def _latest_number(
        self, kind: TableKind, issuer: str, scope: str, candidate: int
    ) -> int:
        current = self._max_number(kind, issuer, scope)
        return candidate if current is None else max(current, candidate)

    def _valid_baseline(
        self, issuer: str, scope: str, at_us: int
    ) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM tables
            WHERE kind = 'FULL'
              AND issuer = ? AND scope = ?
              AND this_update_us <= ? AND next_update_us > ?
            ORDER BY number DESC
            LIMIT 1
            """,
            (issuer, scope, at_us, at_us),
        ).fetchone()

    def _valid_delta(
        self, baseline_row: sqlite3.Row, at_us: int
    ) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM tables
            WHERE kind = 'DELTA'
              AND issuer = ? AND scope = ? AND base_number = ?
              AND this_update_us <= ? AND next_update_us > ?
              AND this_update_us >= ?
            ORDER BY number DESC
            LIMIT 1
            """,
            (
                baseline_row["issuer"],
                baseline_row["scope"],
                baseline_row["number"],
                at_us,
                at_us,
                baseline_row["this_update_us"],
            ),
        ).fetchone()

    def _entry(self, table_id: int, serial: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM entries WHERE table_id = ? AND serial = ?
            """,
            (table_id, serial),
        ).fetchone()

    @staticmethod
    def _effective_us(entry: sqlite3.Row, table_row: sqlite3.Row) -> int:
        if entry["revocation_time_us"] is not None:
            return int(entry["revocation_time_us"])
        return int(table_row["this_update_us"])

    def _lifecycle_permanent_revocation(
        self, issuer: str, serial: str, at_us: int
    ) -> Optional[tuple[Reason, Evidence]]:
        """Earliest permanent revocation for ``serial`` effective at ``at_us``.

        Scope is the certificate lifecycle (issuer + serial): a certificate is
        one object even if different distribution scopes carry its tables.  Once
        a permanent reason is visible in an interpretable table, nothing later
        can clear it.  A DELTA only counts when the FULL baseline it names is
        stored, because a delta cannot be interpreted on its own.
        """
        rows = self._conn.execute(
            f"""
            SELECT t.*, e.reason AS entry_reason, e.revocation_time_us AS entry_time_us
            FROM entries e
            JOIN tables t ON t.id = e.table_id
            WHERE t.issuer = ? AND e.serial = ?
              AND e.reason IN ({_PERMANENT_PLACEHOLDERS})
              AND t.this_update_us <= ?
              AND (
                    t.kind = 'FULL'
                    OR EXISTS (
                        SELECT 1 FROM tables b
                        WHERE b.kind = 'FULL'
                          AND b.issuer = t.issuer
                          AND b.scope = t.scope
                          AND b.number = t.base_number
                    )
                  )
            """,
            (issuer, serial, *_PERMANENT_REASONS, at_us),
        ).fetchall()

        candidates = []
        for row in rows:
            effective = (
                int(row["entry_time_us"])
                if row["entry_time_us"] is not None
                else int(row["this_update_us"])
            )
            if effective <= at_us:
                candidates.append((effective, int(row["id"]), row))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        row = candidates[0][2]
        return Reason(row["entry_reason"]), self._evidence(row)

    @staticmethod
    def _evidence(row: Optional[sqlite3.Row]) -> Optional[Evidence]:
        if row is None:
            return None
        return Evidence(
            table_id=int(row["id"]),
            kind=TableKind(row["kind"]),
            number=int(row["number"]),
            base_number=int(row["base_number"]),
            this_update=from_microseconds(row["this_update_us"]),
            next_update=from_microseconds(row["next_update_us"]),
        )
