"""Column-partition migration with snapshot reads and crash-safe publication."""

from .crash import CrashController, CrashEvent, SimulatedCrash
from .definitions import (
    CallableDefinition,
    ConcatDefinition,
    DefinitionRegistry,
    ScaledFieldDefinition,
)
from .errors import (
    BackfillConflict,
    CorruptChunkError,
    DefinitionNotRegisteredError,
    DuplicateKeyError,
    InvalidMigrationStateError,
    MigrationError,
    RowNotFoundError,
    SimulatedCrash as ErrorSimulatedCrash,
    SnapshotNotFoundError,
    StoreNotFoundError,
)
from .store import BackfillJob, ColumnMigrationStore, SnapshotRead
from .verification import (
    AllSnapshotsReport,
    SnapshotVerificationReport,
    verify_all_snapshots,
    verify_snapshot,
)

__all__ = [
    "AllSnapshotsReport",
    "BackfillConflict",
    "BackfillJob",
    "CallableDefinition",
    "ColumnMigrationStore",
    "ConcatDefinition",
    "CorruptChunkError",
    "CrashController",
    "CrashEvent",
    "DefinitionNotRegisteredError",
    "DefinitionRegistry",
    "DuplicateKeyError",
    "InvalidMigrationStateError",
    "MigrationError",
    "RowNotFoundError",
    "ScaledFieldDefinition",
    "SimulatedCrash",
    "SnapshotNotFoundError",
    "SnapshotRead",
    "SnapshotVerificationReport",
    "StoreNotFoundError",
    "verify_all_snapshots",
    "verify_snapshot",
]
