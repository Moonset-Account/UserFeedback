"""Exception hierarchy for the online unique-index builder.

All exceptions raised deliberately by the library derive from
:class:`BuilderError`, so callers may catch that single type.  Input-shape
mistakes and protocol violations have dedicated subclasses to keep error
handling precise.
"""

from __future__ import annotations

from typing import Any


class BuilderError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(BuilderError):
    """The builder was constructed with an impossible configuration."""


class InvalidChangeError(BuilderError):
    """A :class:`~unique_index_builder.model.Change` is malformed."""


class InvalidTransactionError(BuilderError):
    """A transaction is malformed (bad commit sequence or change list)."""


class StaleTransactionError(InvalidTransactionError):
    """A transaction's commit sequence is at or before the snapshot."""


class TransactionConflictError(InvalidTransactionError):
    """A different transaction was already recorded for the same commit."""


class SnapshotError(BuilderError):
    """Base class for snapshot-ingestion errors."""


class UnknownSnapshotBatchError(SnapshotError):
    """A batch id that was not declared in the snapshot manifest arrived."""


class SnapshotBatchConflictError(SnapshotError):
    """A batch id was redelivered with different contents."""


class DuplicateSnapshotRowError(SnapshotError):
    """A primary key occurs twice in the base snapshot."""


class InvalidSnapshotRowError(SnapshotError):
    """A snapshot row is not a ``(primary_key, value)`` pair or is invalid."""


class BuilderSealedError(BuilderError):
    """The index has been published; this build accepts no more input."""


class IndexNotPublishedError(BuilderError):
    """A lookup was attempted before a successful publish."""


class PublishBlocked(BuilderError):
    """Raised by :meth:`publish` when the publish preconditions are not met.

    The attached :attr:`report` distinguishes missing snapshot batches,
    missing commit sequence numbers, and duplicate-index-value conflicts.
    """

    def __init__(self, report: Any) -> None:
        self.report = report
        super().__init__(report.summary())
