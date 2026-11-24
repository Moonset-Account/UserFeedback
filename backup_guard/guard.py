"""The guard: an event-driven trust state plus a ledger of shard reads.

The guard is intentionally a pure in-memory state machine.  Feed it the
power/lock events in ``seq`` order, open and finish shard reads, then call
:meth:`BackupReadGuard.pack_check` to decide which shards may be packed.

Behaviour that matters for the prompt:

* A screen lock mid-read withdraws every still-open shard whose category
  requires continuous unlocking (``CREDENTIAL_ENCRYPTED_STRICT``) and leaves
  shards readable after first unlock (``CREDENTIAL_ENCRYPTED``) running.
* A reboot starts a new boot generation: the first-unlock confirmation is
  cleared, and every read from the previous boot becomes unverifiable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import Event, EventKind, LockEvent, PowerEvent
from .shards import (
    FileCategory,
    ReadRecord,
    ReadStatus,
    ReadVerdict,
    VerdictCode,
    verify_record,
)
from .state import (
    EventOrderError,
    ShardError,
    StaleBootError,
    TrustState,
    apply_event,
    initial_state,
)


@dataclass(frozen=True, slots=True)
class PackReport:
    """The result of checking every shard before packing a backup archive."""

    verdicts: tuple[ReadVerdict, ...]
    missing: tuple[str, ...] = ()

    @property
    def packable(self) -> bool:
        return not self.missing and all(v.packable for v in self.verdicts)

    @property
    def rejected(self) -> tuple[ReadVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.packable)

    @property
    def accepted(self) -> tuple[ReadVerdict, ...]:
        return tuple(v for v in self.verdicts if v.packable)

    def by_shard(self) -> dict[str, ReadVerdict]:
        return {v.shard_id: v for v in self.verdicts}

    def explain(self) -> list[str]:
        lines = [f"MISSING {sid}: no read record" for sid in self.missing]
        for v in self.verdicts:
            mark = "OK " if v.packable else "SKIP"
            lines.append(
                f"{mark} {v.shard_id} attempt={v.attempt} "
                f"category={v.category.value if v.category else '?'} "
                f"[{v.code.value}] {v.detail}"
            )
        return lines


class BackupReadGuard:
    """Tracks boot/lock state and authorises backup shard reads."""

    def __init__(self) -> None:
        self._state: TrustState = initial_state()
        self._events: list[Event] = []
        self._records: dict[str, list[ReadRecord]] = {}
        self._open: dict[str, ReadRecord] = {}
        self._retired_boots: set[str] = set()

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> TrustState:
        return self._state

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def boot_id(self) -> str | None:
        return self._state.boot_generation

    @property
    def open_reads(self) -> tuple[ReadRecord, ...]:
        return tuple(self._open.values())

    def snapshot(self) -> TrustState:
        """Alias for :attr:`state`, kept for readability at call sites."""

        return self._state

    # ----------------------------------------------------------------- events

    def feed(self, event: Event) -> TrustState:
        """Apply one event after validating that it belongs to this timeline."""

        self._validate_event(event)
        previous = self._state
        self._state = apply_event(self._state, event)
        self._events.append(event)
        if (
            event.kind is EventKind.BOOT
            and previous.booted
            and previous.boot_id
            and previous.boot_id != event.boot_id
        ):
            # A reboot with no explicit shutdown still ends the old boot.
            self._retired_boots.add(previous.boot_id)
        self._reconcile(event)
        return self._state

    def feed_all(self, events: "Event | list[Event] | tuple[Event, ...]") -> TrustState:
        if isinstance(events, (PowerEvent, LockEvent)):
            return self.feed(events)
        for event in events:
            self.feed(event)
        return self._state

    def _validate_event(self, event: Event) -> None:
        if not isinstance(event, (PowerEvent, LockEvent)):
            raise TypeError(f"not a power/lock event: {type(event)!r}")
        if event.seq <= self._state.seq:
            raise EventOrderError(
                f"event seq {event.seq} does not follow {self._state.seq}"
            )

        if event.kind is EventKind.BOOT:
            if self._state.booted and event.boot_id == self._state.boot_id:
                raise EventOrderError(
                    f"boot_id {event.boot_id!r} is already the active boot"
                )
            if event.boot_id in self._retired_boots:
                raise StaleBootError(
                    f"boot_id {event.boot_id!r} belonged to an ended boot"
                )
            return

        if not self._state.booted:
            raise EventOrderError(f"{event.kind.value} event while the device is off")
        if event.boot_id != self._state.boot_id:
            raise StaleBootError(
                f"event boot_id {event.boot_id!r} != active "
                f"{self._state.boot_id!r}"
            )

    # --------------------------------------------------------------- readings

    def start_read(self, shard_id: str, category: FileCategory) -> ReadRecord:
        """Begin reading one shard, if the current trust state allows it."""

        if not isinstance(shard_id, str) or not shard_id:
            raise ValueError("shard_id must be a non-empty string")
        if not isinstance(category, FileCategory):
            raise ValueError("category must be a FileCategory")
        if shard_id in self._open:
            raise ShardError(f"shard {shard_id!r} is already being read")

        # Raises NotBootedError / FirstUnlockRequiredError / DeviceLockedError as
        # appropriate for the category and the current trust state.
        self._state.check(category)

        history = self._records.setdefault(shard_id, [])
        record = ReadRecord(
            shard_id=shard_id,
            category=category,
            boot_id=self._state.boot_id or "",
            start_seq=self._state.seq,
            attempt=len(history) + 1,
            state_at_start={
                "boot_id": self._state.boot_id,
                "booted": self._state.booted,
                "unlocked": self._state.unlocked,
                "first_unlock_done": self._state.first_unlock_done,
            },
        )
        history.append(record)
        self._open[shard_id] = record
        return record

    def finish_read(self, shard_id: str) -> ReadRecord:
        record = self._open.get(shard_id)
        if record is None:
            raise ShardError(f"shard {shard_id!r} has no open read")
        del self._open[shard_id]
        record.status = ReadStatus.COMPLETED
        record.end_seq = self._state.seq
        return record

    def abort_read(self, shard_id: str, reason: str = "aborted by caller") -> ReadRecord:
        record = self._open.get(shard_id)
        if record is None:
            raise ShardError(f"shard {shard_id!r} has no open read")
        return self._withdraw(record, reason)

    def records(self, shard_id: str) -> tuple[ReadRecord, ...]:
        return tuple(self._records.get(shard_id, ()))

    def latest_record(self, shard_id: str) -> ReadRecord | None:
        history = self._records.get(shard_id)
        return history[-1] if history else None

    def all_shard_ids(self) -> tuple[str, ...]:
        return tuple(self._records)

    # ---------------------------------------------------------------- packing

    def verdict(self, shard_id: str) -> ReadVerdict:
        record = self.latest_record(shard_id)
        if record is None:
            return ReadVerdict(
                shard_id=shard_id,
                category=None,
                attempt=0,
                packable=False,
                code=VerdictCode.NOT_FOUND,
                detail="no read record for this shard",
                evidence={},
            )
        return verify_record(record, self._state.boot_generation)

    def pack_check(self, expected: "list[str] | tuple[str, ...]" = ()) -> PackReport:
        """Verify every recorded shard (and any expected ones) before packing."""

        shard_ids = list(self.all_shard_ids())
        shard_ids += [sid for sid in expected if sid not in self._records]
        verdicts = tuple(self.verdict(sid) for sid in shard_ids)
        missing = tuple(sid for sid in expected if sid not in self._records)
        return PackReport(verdicts=verdicts, missing=missing)

    # ------------------------------------------------------------- internals

    def _withdraw(self, record: ReadRecord, reason: str) -> ReadRecord:
        record.status = ReadStatus.WITHDRAWN
        record.withdrawn_reason = reason
        record.end_seq = record.end_seq if record.end_seq is not None else self._state.seq
        if self._open.get(record.shard_id) is record:
            del self._open[record.shard_id]
        return record

    def _reconcile(self, event: Event) -> None:
        if event.kind is EventKind.BOOT:
            self._retired_boots.add(event.boot_id)
            for record in list(self._open.values()):
                self._withdraw(
                    record,
                    f"device rebooted (new boot_id {event.boot_id!r}); "
                    "first unlock must be confirmed again",
                )
            return

        if event.kind is EventKind.SHUTDOWN:
            for record in list(self._open.values()):
                self._withdraw(record, "device powered off mid-read")
            return

        if event.kind is not EventKind.LOCK:
            return

        # A lock invalidates continuous-unlock requirements only.  Reads that
        # need just the first unlock keep their already-open handle.
        for record in list(self._open.values()):
            if record.category is FileCategory.CREDENTIAL_ENCRYPTED_STRICT:
                self._withdraw(
                    record,
                    "device locked mid-read; shard requires unlocking throughout",
                )
            else:
                record.locked_during_read = True
