"""Immutable value objects for the version lifecycle model.

The types in this module are intentionally data-only.  All behaviour lives in
:mod:`version_lifecycle.store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class VersionKind(StrEnum):
    """Kind of a version record."""

    DATA = "data"
    DELETE_MARKER = "delete_marker"


class ReadState(StrEnum):
    """Outcome of a read."""

    VISIBLE = "visible"
    DELETED = "deleted"
    ARCHIVED = "archived"
    MISSING = "missing"


class ReceiptStatus(StrEnum):
    """Outcome recorded for one executed plan item."""

    REMOVED = "removed"
    RETAINED = "retained"
    STALE_VERSION = "stale_version"
    MISSING_VERSION = "missing_version"
    MISSING_KEY = "missing_key"


class RetentionReason(StrEnum):
    """Why a version that looks expired still cannot be removed."""

    RETENTION_NOT_REACHED = "retention_not_reached"
    HELD = "held"
    LEASE_ACTIVE = "lease_active"
    DELETE_MARKER_GUARD = "delete_marker_guard"


@dataclass(frozen=True, slots=True)
class ArchiveLease:
    """A temporary read lease granted to an archived version.

    The lease is active for ``granted_at <= now < expires_at``.  At the exact
    expiry instant it is already inactive, so a lease never outlives its
    stated deadline.
    """

    lease_id: str
    granted_at: float
    expires_at: float

    def is_active(self, now: float) -> bool:
        return self.granted_at <= now < self.expires_at


@dataclass(frozen=True, slots=True)
class VersionRecord:
    """One ordered version of an object key."""

    version_id: str
    sequence: int
    kind: VersionKind
    archived: bool
    retain_until: float | None
    holds: frozenset[str] = frozenset()
    lease: ArchiveLease | None = None

    @property
    def is_delete_marker(self) -> bool:
        return self.kind is VersionKind.DELETE_MARKER

    @property
    def is_held(self) -> bool:
        return bool(self.holds)


@dataclass(frozen=True, slots=True)
class ReadResult:
    """Result of a default read or of reading one specific version."""

    key: str
    state: ReadState
    version_id: str | None
    sequence: int | None
    reason: str
    as_of: float


@dataclass(frozen=True, slots=True)
class PlanItem:
    """One removal candidate captured by an expiry scan.

    ``retain_until``, ``hold_ids`` and ``lease_id`` are the snapshot observed
    at planning time.  They are informational: execution always re-reads live
    state instead of trusting the snapshot.
    """

    item_id: str
    key: str
    version_id: str
    sequence: int
    retain_until: float | None
    hold_ids: tuple[str, ...]
    lease_id: str | None
    planned_at: float
    action: str = "remove"
    reason: str = "eligible_for_removal"


@dataclass(frozen=True, slots=True)
class ExpiryBatch:
    """A contiguous group of plan items executed together."""

    batch_id: str
    items: tuple[PlanItem, ...]


@dataclass(frozen=True, slots=True)
class ExpiryPlan:
    """Deterministic, batched output of one expiry scan."""

    plan_id: str
    created_at: float
    batches: tuple[ExpiryBatch, ...]

    @property
    def items(self) -> tuple[PlanItem, ...]:
        return tuple(item for batch in self.batches for item in batch.items)

    @property
    def item_count(self) -> int:
        return sum(len(batch.items) for batch in self.batches)

    @property
    def batch_count(self) -> int:
        return len(self.batches)


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Immutable, replayable result of executing one plan item."""

    item_id: str
    batch_id: str
    key: str
    version_id: str
    status: ReceiptStatus
    reason: str
    planned_sequence: int
    observed_sequence: int | None
    executed_at: float
    removed: bool


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Receipts produced (or replayed) by an execution call."""

    plan_id: str
    executed_at: float
    receipts: tuple[ExecutionReceipt, ...]
    batch_id: str | None = None
    replayed_item_ids: tuple[str, ...] = ()

    @property
    def removed_count(self) -> int:
        return sum(1 for r in self.receipts if r.status is ReceiptStatus.REMOVED)

    @property
    def retained_count(self) -> int:
        return sum(1 for r in self.receipts if r.status is ReceiptStatus.RETAINED)

    @property
    def stale_count(self) -> int:
        return sum(1 for r in self.receipts if r.status is ReceiptStatus.STALE_VERSION)

    @property
    def missing_count(self) -> int:
        return sum(
            1
            for r in self.receipts
            if r.status in (ReceiptStatus.MISSING_KEY, ReceiptStatus.MISSING_VERSION)
        )

    @property
    def removed_now_count(self) -> int:
        replayed = set(self.replayed_item_ids)
        return sum(
            1
            for r in self.receipts
            if r.removed and r.item_id not in replayed
        )


@dataclass(frozen=True, slots=True)
class KeySnapshot:
    """Everything observable about one key at a single instant."""

    key: str
    as_of: float
    read: ReadResult
    versions: tuple[VersionRecord, ...]
    destroyable_version_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DestroyableReport:
    """Version IDs that could be removed at a given instant."""

    as_of: float
    by_key: Mapping[str, tuple[str, ...]]

    @property
    def version_ids(self) -> tuple[str, ...]:
        return tuple(vid for key in sorted(self.by_key) for vid in self.by_key[key])

    @property
    def total(self) -> int:
        return sum(len(ids) for ids in self.by_key.values())
