"""Core in-memory store implementing the version lifecycle rules."""

from __future__ import annotations

import functools
import math
import threading
from dataclasses import replace
from typing import Iterable, Sequence

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


class LifecycleError(ValueError):
    """Raised when a lifecycle operation is rejected by an invariant."""


class UnknownKeyError(LifecycleError):
    """Raised for an unknown key, plan or batch ID, depending on context."""


class UnknownVersionError(LifecycleError):
    """Raised when a version ID does not exist for the given key."""


class UnknownPlanError(LifecycleError):
    """Raised for an unknown plan ID or a batch ID not in the plan."""


class UnknownReceiptError(LifecycleError):
    """Raised when no receipt exists for a plan item ID."""


class RetentionBlockedError(LifecycleError):
    """Raised when a direct removal is refused by a retention rule."""

    def __init__(self, key: str, version_id: str, reason: RetentionReason) -> None:
        super().__init__(
            f"version {version_id!r} of key {key!r} cannot be removed: {reason.value}"
        )
        self.key = key
        self.version_id = version_id
        self.reason = reason


def _synchronized(method):
    """Serialize one public operation on the store's re-entrant lock.

    The store is deterministic because time is always an explicit argument;
    the lock is only there so that "plan, then mutate, then execute" stays a
    well-defined interleaving instead of a torn read.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _validate_time(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number, got {type(value).__name__}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _normalize_holds(holds: Iterable[str] | None) -> frozenset[str]:
    if holds is None:
        return frozenset()
    normalized = []
    for hold in holds:
        if not isinstance(hold, str) or not hold:
            raise ValueError("hold IDs must be non-empty strings")
        normalized.append(hold)
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate hold IDs are not allowed")
    return frozenset(normalized)


def _make_lease(
    lease_id: str, granted_at: float, expires_at: float
) -> ArchiveLease:
    if not isinstance(lease_id, str) or not lease_id:
        raise ValueError("lease ID must be a non-empty string")
    granted = _validate_time("granted_at", granted_at)
    expires = _validate_time("expires_at", expires_at)
    if expires <= granted:
        raise LifecycleError("lease must expire after it is granted")
    return ArchiveLease(lease_id=lease_id, granted_at=granted, expires_at=expires)


def _blocker_without_key_context(
    record: VersionRecord, now: float
) -> RetentionReason | None:
    """First rule (fixed priority) blocking removal, ignoring sibling records."""

    if record.retain_until is not None and now < record.retain_until:
        return RetentionReason.RETENTION_NOT_REACHED
    if record.holds:
        return RetentionReason.HELD
    if record.lease is not None and record.lease.is_active(now):
        return RetentionReason.LEASE_ACTIVE
    return None


class VersionedStore:
    """Ordered, versioned object store with retention, holds and read leases.

    Records for a key are kept oldest-first by an internal sequence.  Nothing
    is ever reordered and a version ID is unique within its key, so a plan
    item can detect that its target was removed and recreated (sequence
    change) instead of silently deleting a different record.

    Every time-sensitive call takes an explicit ``now``; the store holds no
    clock of its own.  That makes retention boundaries, lease boundaries and
    plan/execute interleavings exactly reproducible.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._keys: dict[str, list[VersionRecord]] = {}
        self._plans: dict[str, ExpiryPlan] = {}
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._next_sequence: dict[str, int] = {}
        self._plan_counter = 0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @_synchronized
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    @_synchronized
    def has_key(self, key: str) -> bool:
        return bool(self._keys.get(key))

    @_synchronized
    def versions(self, key: str) -> tuple[VersionRecord, ...]:
        """All live records for *key*, oldest first."""

        records = self._keys.get(key)
        if not records:
            raise UnknownKeyError(f"unknown key: {key!r}")
        return tuple(records)

    @_synchronized
    def version(self, key: str, version_id: str) -> VersionRecord | None:
        for record in self._keys.get(key, ()):
            if record.version_id == version_id:
                return record
        return None

    @_synchronized
    def get_version(self, key: str, version_id: str) -> VersionRecord:
        if not self._keys.get(key):
            raise UnknownKeyError(f"unknown key: {key!r}")
        record = self.version(key, version_id)
        if record is None:
            raise UnknownVersionError(
                f"unknown version {version_id!r} for key {key!r}"
            )
        return record

    # ------------------------------------------------------------------
    # Mutations: version records
    # ------------------------------------------------------------------
    def _append_record(self, key: str, record: VersionRecord) -> VersionRecord:
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        if not isinstance(record.version_id, str) or not record.version_id:
            raise ValueError("version_id must be a non-empty string")
        if self.version(key, record.version_id) is not None:
            raise LifecycleError(
                f"version {record.version_id!r} already exists for key {key!r}"
            )
        sequence = self._next_sequence.get(key, 0)
        self._next_sequence[key] = sequence + 1
        stored = replace(record, sequence=sequence)
        self._keys.setdefault(key, []).append(stored)
        return stored

    @_synchronized
    def put_version(
        self,
        key: str,
        version_id: str,
        *,
        retain_until: float | None = None,
        holds: Iterable[str] | None = None,
        archived: bool = False,
        lease: ArchiveLease | None = None,
    ) -> VersionRecord:
        """Add a normal data version as the newest record for *key*."""

        retain_until = (
            None if retain_until is None else _validate_time("retain_until", retain_until)
        )
        if lease is not None and not archived:
            raise LifecycleError("read leases can only be attached to archived versions")
        if lease is not None:
            if not isinstance(lease, ArchiveLease):
                raise TypeError("lease must be an ArchiveLease")
            _validate_time("lease.granted_at", lease.granted_at)
            _validate_time("lease.expires_at", lease.expires_at)
            if lease.expires_at <= lease.granted_at:
                raise LifecycleError("lease must expire after it is granted")
        record = VersionRecord(
            version_id=version_id,
            sequence=-1,
            kind=VersionKind.DATA,
            archived=bool(archived),
            retain_until=retain_until,
            holds=_normalize_holds(holds),
            lease=lease,
        )
        return self._append_record(key, record)

    @_synchronized
    def put_delete_marker(
        self,
        key: str,
        version_id: str,
        *,
        retain_until: float | None = None,
        holds: Iterable[str] | None = None,
    ) -> VersionRecord:
        """Append a delete marker, shadowing every older version."""

        retain_until = (
            None if retain_until is None else _validate_time("retain_until", retain_until)
        )
        record = VersionRecord(
            version_id=version_id,
            sequence=-1,
            kind=VersionKind.DELETE_MARKER,
            archived=False,
            retain_until=retain_until,
            holds=_normalize_holds(holds),
            lease=None,
        )
        return self._append_record(key, record)

    @_synchronized
    def archive(self, key: str, version_id: str) -> VersionRecord:
        """Move a data version to archival storage, dropping any lease."""

        return self._replace_version(
            key,
            version_id,
            lambda record: self._require_data(record, "archive"),
            archived=True,
            lease=None,
        )

    @_synchronized
    def unarchive(self, key: str, version_id: str) -> VersionRecord:
        """Bring an archived version back to live storage, dropping any lease."""

        def require_archived(record: VersionRecord) -> VersionRecord:
            self._require_data(record, "unarchive")
            if not record.archived:
                raise LifecycleError(f"version {version_id!r} is not archived")
            return record

        return self._replace_version(
            key, version_id, require_archived, archived=False, lease=None
        )

    @_synchronized
    def set_retention(
        self, key: str, version_id: str, retain_until: float | None
    ) -> VersionRecord:
        retain_until = (
            None if retain_until is None else _validate_time("retain_until", retain_until)
        )
        return self._replace_version(
            key, version_id, lambda record: record, retain_until=retain_until
        )

    @_synchronized
    def add_hold(self, key: str, version_id: str, hold_id: str) -> VersionRecord:
        if not isinstance(hold_id, str) or not hold_id:
            raise ValueError("hold ID must be a non-empty string")

        def apply(record: VersionRecord) -> VersionRecord:
            return replace(record, holds=record.holds | {hold_id})

        return self._replace_version(
            key, version_id, lambda record: record, transform=apply
        )

    @_synchronized
    def release_hold(self, key: str, version_id: str, hold_id: str) -> VersionRecord:
        def apply(record: VersionRecord) -> VersionRecord:
            if hold_id not in record.holds:
                raise LifecycleError(
                    f"hold {hold_id!r} is not present on version {version_id!r}"
                )
            return replace(record, holds=record.holds - {hold_id})

        return self._replace_version(
            key, version_id, lambda record: record, transform=apply
        )

    @_synchronized
    def grant_lease(
        self,
        key: str,
        version_id: str,
        lease_id: str,
        *,
        granted_at: float,
        expires_at: float,
    ) -> VersionRecord:
        """Grant a temporary read lease on an archived version."""

        lease = _make_lease(lease_id, granted_at, expires_at)

        def require_archived(record: VersionRecord) -> VersionRecord:
            self._require_data(record, "grant a read lease")
            if not record.archived:
                raise LifecycleError(
                    "read leases can only be granted to archived versions"
                )
            if record.lease is not None and record.lease.is_active(granted_at):
                raise LifecycleError(
                    f"version {version_id!r} already has an active lease "
                    f"{record.lease.lease_id!r}"
                )
            return record

        return self._replace_version(
            key,
            version_id,
            require_archived,
            transform=lambda record: replace(record, lease=lease),
        )

    @_synchronized
    def revoke_lease(self, key: str, version_id: str) -> VersionRecord:
        return self._replace_version(
            key, version_id, lambda record: record, lease=None
        )

    @staticmethod
    def _require_data(record: VersionRecord, action: str) -> VersionRecord:
        if record.is_delete_marker:
            raise LifecycleError(f"cannot {action}: record is a delete marker")
        return record

    def _replace_version(
        self,
        key: str,
        version_id: str,
        check,
        *,
        transform=None,
        **changes,
    ) -> VersionRecord:
        records = self._keys.get(key)
        if not records:
            raise UnknownKeyError(f"unknown key: {key!r}")
        for index, record in enumerate(records):
            if record.version_id != version_id:
                continue
            record = check(record)
            if transform is not None:
                record = transform(record)
            record = replace(record, **changes)
            records[index] = record
            return record
        raise UnknownVersionError(f"unknown version {version_id!r} for key {key!r}")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def _newest(self, key: str) -> VersionRecord | None:
        records = self._keys.get(key)
        if not records:
            return None
        return max(records, key=lambda record: record.sequence)

    @_synchronized
    def read(self, key: str, *, now: float, version_id: str | None = None) -> ReadResult:
        """Read the default view, or one specific version.

        The default read never falls back past the newest record, never falls
        back from a delete marker, and never falls back from an archived
        version whose read lease is not active at *now*.
        """

        now = _validate_time("now", now)
        if version_id is None:
            record = self._newest(key)
            if record is None:
                return ReadResult(
                    key=key,
                    state=ReadState.MISSING,
                    version_id=None,
                    sequence=None,
                    reason="no_versions",
                    as_of=now,
                )
            return self._read_record(key, record, now, default=True)

        record = self.version(key, version_id)
        if record is None:
            return ReadResult(
                key=key,
                state=ReadState.MISSING,
                version_id=version_id,
                sequence=None,
                reason="unknown_version",
                as_of=now,
            )
        return self._read_record(key, record, now, default=False)

    @staticmethod
    def _read_record(
        key: str, record: VersionRecord, now: float, *, default: bool
    ) -> ReadResult:
        if record.is_delete_marker:
            return ReadResult(
                key=key,
                state=ReadState.DELETED,
                version_id=record.version_id,
                sequence=record.sequence,
                reason=(
                    "newest_record_is_delete_marker"
                    if default
                    else "version_is_delete_marker"
                ),
                as_of=now,
            )
        if record.archived:
            if record.lease is not None and record.lease.is_active(now):
                return ReadResult(
                    key=key,
                    state=ReadState.VISIBLE,
                    version_id=record.version_id,
                    sequence=record.sequence,
                    reason="archived_with_active_read_lease",
                    as_of=now,
                )
            return ReadResult(
                key=key,
                state=ReadState.ARCHIVED,
                version_id=record.version_id,
                sequence=record.sequence,
                reason="archived_without_active_read_lease",
                as_of=now,
            )
        return ReadResult(
            key=key,
            state=ReadState.VISIBLE,
            version_id=record.version_id,
            sequence=record.sequence,
            reason="live_version",
            as_of=now,
        )

    # ------------------------------------------------------------------
    # Destroyability
    # ------------------------------------------------------------------
    @_synchronized
    def removal_blocker(
        self, key: str, version_id: str, *, now: float
    ) -> RetentionReason | None:
        """First rule (in a fixed priority order) blocking removal, if any.

        Priority: retention deadline, hold, active read lease, then the
        delete-marker guard.  ``None`` means the version is destroyable now.
        """

        now = _validate_time("now", now)
        record = self.get_version(key, version_id)
        return self._blocker_for(key, record, now)

    def _blocker_for(
        self, key: str, record: VersionRecord, now: float
    ) -> RetentionReason | None:
        blocker = _blocker_without_key_context(record, now)
        if blocker is not None:
            return blocker
        if record.is_delete_marker and self._marker_guards(key, record):
            return RetentionReason.DELETE_MARKER_GUARD
        return None

    def _marker_guards(self, key: str, marker: VersionRecord) -> bool:
        """Whether clearing *marker* would promote an older data version.

        The default read resolves against the newest record, so only the
        newest record can change it.  A marker that is not newest is already
        invisible and may always be cleared.  For a newest marker, look at
        the record that would become newest:

        * nothing left -> the default read becomes ``MISSING`` (not a
          resurrection), so the marker may be cleared;
        * another delete marker -> the default read stays ``DELETED``, so the
          marker may be cleared;
        * a data version -> the default read would flip from ``DELETED`` to
          that version's ``VISIBLE``/``ARCHIVED`` state.  That is exactly the
          resurrection the guard forbids, so the marker is retained no
          matter how long its retention deadline has been expired.
        """

        records = self._keys.get(key, ())
        newest = max(records, key=lambda record: record.sequence, default=None)
        if newest is None or newest.sequence != marker.sequence:
            return False
        older = [record for record in records if record.sequence < marker.sequence]
        if not older:
            return False
        next_newest = max(older, key=lambda record: record.sequence)
        return not next_newest.is_delete_marker

    @_synchronized
    def is_destroyable(self, key: str, version_id: str, *, now: float) -> bool:
        return self.removal_blocker(key, version_id, now=now) is None

    @_synchronized
    def destroyable(self, *, now: float) -> DestroyableReport:
        now = _validate_time("now", now)
        by_key: dict[str, tuple[str, ...]] = {}
        for key in sorted(self._keys):
            ids = tuple(
                record.version_id
                for record in self._keys[key]
                if self._blocker_for(key, record, now) is None
            )
            if ids:
                by_key[key] = ids
        return DestroyableReport(as_of=now, by_key=by_key)

    @_synchronized
    def destroyable_version_ids(self, key: str, *, now: float) -> tuple[str, ...]:
        if key not in self._keys:
            raise UnknownKeyError(f"unknown key: {key!r}")
        return tuple(
            record.version_id
            for record in self._keys[key]
            if self._blocker_for(key, record, now) is None
        )

    @_synchronized
    def remove_version(self, key: str, version_id: str, *, now: float) -> VersionRecord:
        """Remove one version directly, or raise if a rule still blocks it.

        This is the non-planned counterpart to ``execute_plan``.  Both paths
        share ``_blocker_for`` so they can never disagree about eligibility.
        """

        now = _validate_time("now", now)
        record = self.get_version(key, version_id)
        blocker = self._blocker_for(key, record, now)
        if blocker is not None:
            raise RetentionBlockedError(key, version_id, blocker)
        records = self._keys[key]
        records.remove(record)
        if not records:
            del self._keys[key]
        return record

    @_synchronized
    def snapshot(self, key: str, *, now: float) -> KeySnapshot:
        """Read view plus destroyable list for one key at one instant."""

        now = _validate_time("now", now)
        if key not in self._keys:
            raise UnknownKeyError(f"unknown key: {key!r}")
        return KeySnapshot(
            key=key,
            as_of=now,
            read=self.read(key, now=now),
            versions=self.versions(key),
            destroyable_version_ids=self.destroyable_version_ids(key, now=now),
        )

    # ------------------------------------------------------------------
    # Expiry scanning
    # ------------------------------------------------------------------
    @_synchronized
    def scan_expiry(
        self,
        *,
        now: float,
        batch_size: int = 100,
        keys: Sequence[str] | None = None,
    ) -> ExpiryPlan:
        """Plan removal of every currently destroyable version.

        Candidates are ordered by ``(key, sequence)`` and split into batches
        of at most *batch_size* items.  The plan records the state observed
        at planning time; execution re-adjudicates every item against live
        state.
        """

        now = _validate_time("now", now)
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError("batch_size must be an integer")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        if keys is None:
            selected_keys = sorted(self._keys)
        else:
            selected_keys = list(dict.fromkeys(keys))
            for key in selected_keys:
                if not isinstance(key, str) or not key:
                    raise ValueError("keys must contain non-empty strings")
                if key not in self._keys:
                    raise UnknownKeyError(f"unknown key: {key!r}")

        self._plan_counter += 1
        plan_id = f"plan-{self._plan_counter:06d}"

        candidates: list[PlanItem] = []
        for key in selected_keys:
            for record in self._keys[key]:
                if self._blocker_for(key, record, now) is not None:
                    continue
                candidates.append(
                    PlanItem(
                        item_id="",
                        key=key,
                        version_id=record.version_id,
                        sequence=record.sequence,
                        retain_until=record.retain_until,
                        hold_ids=tuple(sorted(record.holds)),
                        lease_id=record.lease.lease_id if record.lease else None,
                        planned_at=now,
                    )
                )

        numbered = [
            replace(item, item_id=f"{plan_id}#item-{index:04d}")
            for index, item in enumerate(candidates)
        ]
        batches = tuple(
            ExpiryBatch(
                batch_id=f"{plan_id}#batch-{index:04d}",
                items=tuple(numbered[start : start + batch_size]),
            )
            for index, start in enumerate(range(0, len(numbered), batch_size))
        )
        plan = ExpiryPlan(plan_id=plan_id, created_at=now, batches=batches)
        self._plans[plan_id] = plan
        return plan

    @_synchronized
    def plan(self, plan_id: str) -> ExpiryPlan:
        try:
            return self._plans[plan_id]
        except KeyError:
            raise UnknownPlanError(f"unknown plan: {plan_id!r}") from None

    # ------------------------------------------------------------------
    # Plan execution
    # ------------------------------------------------------------------
    @_synchronized
    def execute_plan(
        self,
        plan: ExpiryPlan | str,
        *,
        now: float,
        batch_ids: Sequence[str] | None = None,
    ) -> ExecutionReport:
        """Execute plan batches, re-adjudicating every item against live state.

        * An item that already has a receipt is replayed from the receipt log
          instead of being reconsidered, so retries are safe.
        * Otherwise: a vanished key yields ``MISSING_KEY``, a vanished
          version ``MISSING_VERSION``, a version ID reused with a new
          sequence ``STALE_VERSION``, and anything no longer eligible yields
          ``RETAINED`` with the exact blocking reason.
        """

        now = _validate_time("now", now)
        resolved = self.plan(plan) if isinstance(plan, str) else plan
        selected = self._select_batches(resolved, batch_ids)

        receipts: list[ExecutionReceipt] = []
        replayed: list[str] = []
        for batch in selected:
            for item in batch.items:
                existing = self._receipts.get(item.item_id)
                if existing is not None:
                    receipts.append(existing)
                    replayed.append(item.item_id)
                    continue
                receipt = self._execute_item(item, batch.batch_id, now)
                self._receipts[item.item_id] = receipt
                receipts.append(receipt)

        return ExecutionReport(
            plan_id=resolved.plan_id,
            executed_at=now,
            receipts=tuple(receipts),
            batch_id=self._batch_label(selected, batch_ids),
            replayed_item_ids=tuple(replayed),
        )

    @staticmethod
    def _batch_label(
        selected: tuple[ExpiryBatch, ...], batch_ids: Sequence[str] | None
    ) -> str | None:
        """Name the batch when a call targeted exactly one batch."""

        if batch_ids is not None and len(selected) == 1:
            return selected[0].batch_id
        return None

    def _select_batches(
        self, plan: ExpiryPlan, batch_ids: Sequence[str] | None
    ) -> tuple[ExpiryBatch, ...]:
        if batch_ids is None:
            return plan.batches
        known = {batch.batch_id: batch for batch in plan.batches}
        selected = []
        for batch_id in batch_ids:
            if batch_id not in known:
                raise UnknownPlanError(
                    f"unknown batch {batch_id!r} for plan {plan.plan_id!r}"
                )
            selected.append(known[batch_id])
        return tuple(selected)

    def _execute_item(
        self, item: PlanItem, batch_id: str, now: float
    ) -> ExecutionReceipt:
        def build(
            status: ReceiptStatus,
            reason: str,
            observed: int | None,
            *,
            removed: bool = False,
        ) -> ExecutionReceipt:
            return ExecutionReceipt(
                item_id=item.item_id,
                batch_id=batch_id,
                key=item.key,
                version_id=item.version_id,
                status=status,
                reason=reason,
                planned_sequence=item.sequence,
                observed_sequence=observed,
                executed_at=now,
                removed=removed,
            )

        records = self._keys.get(item.key)
        if not records:
            return build(ReceiptStatus.MISSING_KEY, "key_no_longer_has_versions", None)

        record = next(
            (candidate for candidate in records if candidate.version_id == item.version_id),
            None,
        )
        if record is None:
            return build(
                ReceiptStatus.MISSING_VERSION,
                "version_already_removed_or_replaced",
                None,
            )
        if record.sequence != item.sequence:
            return build(
                ReceiptStatus.STALE_VERSION,
                "same_version_id_but_new_sequence",
                record.sequence,
            )

        blocker = self._blocker_for(item.key, record, now)
        if blocker is not None:
            return build(ReceiptStatus.RETAINED, blocker.value, record.sequence)

        records.remove(record)
        if not records:
            del self._keys[item.key]
        return build(
            ReceiptStatus.REMOVED,
            "eligible_for_removal",
            record.sequence,
            removed=True,
        )

    @_synchronized
    def receipt(self, item_id: str) -> ExecutionReceipt | None:
        return self._receipts.get(item_id)

    @_synchronized
    def get_receipt(self, item_id: str) -> ExecutionReceipt:
        receipt = self._receipts.get(item_id)
        if receipt is None:
            raise UnknownReceiptError(f"no receipt for item: {item_id!r}")
        return receipt

    @_synchronized
    def replay_item(self, item_id: str) -> ExecutionReceipt:
        """Replay one recorded receipt by plan item ID (no state change)."""

        return self.get_receipt(item_id)

    @_synchronized
    def replay(
        self, plan: ExpiryPlan | str, *, batch_ids: Sequence[str] | None = None
    ) -> ExecutionReport:
        """Return recorded receipts for a plan without mutating any state."""

        resolved = self.plan(plan) if isinstance(plan, str) else plan
        selected = self._select_batches(resolved, batch_ids)
        receipts: list[ExecutionReceipt] = []
        for batch in selected:
            for item in batch.items:
                receipt = self._receipts.get(item.item_id)
                if receipt is not None:
                    receipts.append(receipt)
        executed_at = (
            max(receipt.executed_at for receipt in receipts)
            if receipts
            else resolved.created_at
        )
        return ExecutionReport(
            plan_id=resolved.plan_id,
            executed_at=executed_at,
            receipts=tuple(receipts),
            batch_id=self._batch_label(selected, batch_ids),
            replayed_item_ids=tuple(receipt.item_id for receipt in receipts),
        )

    @_synchronized
    def receipt_log(self) -> tuple[ExecutionReceipt, ...]:
        return tuple(self._receipts.values())
