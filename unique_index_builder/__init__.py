"""Public API for the online unique-index builder."""

from .builder import UniqueIndexBuilder
from .errors import (
    BuilderError,
    BuilderSealedError,
    ConfigurationError,
    DuplicateSnapshotRowError,
    IndexNotPublishedError,
    InvalidChangeError,
    InvalidSnapshotRowError,
    InvalidTransactionError,
    PublishBlocked,
    SnapshotBatchConflictError,
    SnapshotError,
    StaleTransactionError,
    TransactionConflictError,
    UnknownSnapshotBatchError,
)
from .model import (
    Change,
    ChangeKind,
    PublishReport,
    SnapshotRow,
    SnapshotSegment,
    Transaction,
    UniqueConflict,
)

__all__ = [
    "BuilderError",
    "BuilderSealedError",
    "Change",
    "ChangeKind",
    "ConfigurationError",
    "DuplicateSnapshotRowError",
    "IndexNotPublishedError",
    "InvalidChangeError",
    "InvalidSnapshotRowError",
    "InvalidTransactionError",
    "PublishBlocked",
    "PublishReport",
    "SnapshotBatchConflictError",
    "SnapshotError",
    "SnapshotRow",
    "SnapshotSegment",
    "StaleTransactionError",
    "Transaction",
    "TransactionConflictError",
    "UniqueConflict",
    "UniqueIndexBuilder",
    "UnknownSnapshotBatchError",
]

__version__ = "1.0.0"
