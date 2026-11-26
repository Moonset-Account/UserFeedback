"""Errors raised by the column-partition migration component."""

from __future__ import annotations

from typing import Any, Mapping


class MigrationError(Exception):
    """Base class for all domain errors."""


class StoreNotFoundError(MigrationError):
    """Raised when a store directory has no committed manifest pointer."""


class SnapshotNotFoundError(MigrationError):
    """Raised when a snapshot id is unknown or not committed."""


class CorruptChunkError(MigrationError):
    """Raised when an existing chunk fails its checksum or format check."""


class DefinitionNotRegisteredError(MigrationError):
    """Raised when a declared definition version is not in the registry."""


class DuplicateKeyError(MigrationError):
    """Raised by insert/import calls that encounter an existing key."""


class RowNotFoundError(MigrationError):
    """Raised when an update targets a missing row."""


class InvalidMigrationStateError(MigrationError):
    """Raised when an operation is incompatible with migration metadata."""


class BackfillConflict(MigrationError):
    """Raised when row versions changed while a backfill job was reading.

    Publication is deliberately conditional only on the row-version map read by
    the background job.  The exception carries both maps so callers/tests can
    prove why publication was rejected.
    """

    def __init__(
        self,
        *,
        group_id: str,
        column: str,
        read_versions: Mapping[str, int],
        current_versions: Mapping[str, int],
        current_snapshot_id: int,
    ) -> None:
        self.group_id = group_id
        self.column = column
        self.read_versions = dict(read_versions)
        self.current_versions = dict(current_versions)
        self.current_snapshot_id = current_snapshot_id
        changed = sorted(
            key
            for key in set(self.read_versions) | set(self.current_versions)
            if self.read_versions.get(key) != self.current_versions.get(key)
        )
        super().__init__(
            f"backfill for {column} on group {group_id!r} lost row-version "
            f"CAS at current snapshot {current_snapshot_id}; changed keys={changed}"
        )


class SimulatedCrash(BaseException):
    """In-process fail-stop signal at a durable barrier.

    It derives from BaseException so that ordinary ``except Exception`` blocks
    cannot accidentally turn a crash point into a handled business error.
    """

    def __init__(self, event: Any) -> None:
        self.event = event
        super().__init__(f"simulated crash at event {event.seq}: {event.point}")
