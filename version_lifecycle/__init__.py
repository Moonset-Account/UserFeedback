"""In-memory lifecycle model for versioned object keys.

Public API
----------
``VersionedStore`` is the only stateful type.  Value objects live in
:mod:`version_lifecycle.models`.

Typical flow::

    store = VersionedStore()
    store.put_version("photos/a", "v1", retain_until=100, archived=True)
    store.grant_lease("photos/a", "v1", "restore-1", granted_at=10, expires_at=20)
    store.read("photos/a", now=15)                # VISIBLE (leased)
    store.read("photos/a", now=25)                # ARCHIVED
    plan = store.scan_expiry(now=200, batch_size=50)
    report = store.execute_plan(plan, now=200)
    store.replay(plan)                            # same receipts, no mutation

See ``README.md`` for the exact visibility, retention and delete-marker
rules, including the boundary conventions.
"""

from .models import (
    ArchiveLease,
    DestroyableReport,
    ExecutionReceipt,
    ExecutionReport,
    ExpiryBatch,
    ExpiryPlan,
    KeySnapshot,
    PlanItem,
    ReadResult,
    ReadState,
    ReceiptStatus,
    RetentionReason,
    VersionKind,
    VersionRecord,
)
from .store import (
    LifecycleError,
    RetentionBlockedError,
    UnknownKeyError,
    UnknownPlanError,
    UnknownReceiptError,
    UnknownVersionError,
    VersionedStore,
)

__all__ = [
    "ArchiveLease",
    "DestroyableReport",
    "ExecutionReceipt",
    "ExecutionReport",
    "ExpiryBatch",
    "ExpiryPlan",
    "KeySnapshot",
    "LifecycleError",
    "PlanItem",
    "ReadResult",
    "ReadState",
    "ReceiptStatus",
    "RetentionBlockedError",
    "RetentionReason",
    "UnknownKeyError",
    "UnknownPlanError",
    "UnknownReceiptError",
    "UnknownVersionError",
    "VersionKind",
    "VersionRecord",
    "VersionedStore",
]
