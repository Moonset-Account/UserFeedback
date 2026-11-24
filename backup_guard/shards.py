"""File protection categories, shard read records, and the packing verdict.

A shard record captures the permission evidence that existed *at the moment the
read started* plus whether the device stayed in the required state until the
read finished.  ``verify_record`` re-checks that evidence instead of trusting
the ledger's bookkeeping, so a forged or replayed record is rejected.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class FileCategory(str, enum.Enum):
    """The three file-based-encryption protection classes."""

    DEVICE_ENCRYPTED = "device_encrypted"
    CREDENTIAL_ENCRYPTED = "credential_encrypted"
    CREDENTIAL_ENCRYPTED_STRICT = "credential_encrypted_strict"


class ReadStatus(str, enum.Enum):
    OPEN = "open"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"


class VerdictCode(str, enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    STILL_OPEN = "still_open"
    WITHDRAWN = "withdrawn"
    STALE_BOOT = "stale_boot"
    UNLOCK_GAP = "unlock_gap"
    MISSING_PERMISSION = "missing_permission"


@dataclass(slots=True)
class ReadRecord:
    """One attempt to read one backup shard.

    ``state_at_start`` is a plain dict snapshot of the trust state's relevant
    fields.  It is data, so verification can be rerun later and can detect an
    inconsistent record rather than believing it.
    """

    shard_id: str
    category: FileCategory
    boot_id: str
    start_seq: int
    attempt: int = 1
    end_seq: int | None = None
    status: ReadStatus = ReadStatus.OPEN
    withdrawn_reason: str | None = None
    locked_during_read: bool = False
    state_at_start: dict[str, object] = field(default_factory=dict)

    def evidence(self) -> dict[str, object]:
        def name(value: object) -> object:
            return value.value if isinstance(value, enum.Enum) else value

        return {
            "shard_id": self.shard_id,
            "category": name(self.category),
            "boot_id": self.boot_id,
            "attempt": self.attempt,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "status": name(self.status),
            "withdrawn_reason": self.withdrawn_reason,
            "locked_during_read": self.locked_during_read,
            "state_at_start": dict(self.state_at_start),
        }


@dataclass(frozen=True, slots=True)
class ReadVerdict:
    shard_id: str
    category: FileCategory | None
    attempt: int
    packable: bool
    code: VerdictCode
    detail: str
    evidence: dict[str, object]

    def __bool__(self) -> bool:  # convenience: ``if verdict:`` means packable
        return self.packable


def _coerce_category(value: object) -> FileCategory | None:
    if isinstance(value, FileCategory):
        return value
    if isinstance(value, str):
        try:
            return FileCategory(value)
        except ValueError:
            return None
    return None


def verify_record(
    record: ReadRecord,
    current_boot_id: str | None,
    *,
    require_current_boot: bool = True,
) -> ReadVerdict:
    """Decide whether a shard record may be packed into a backup archive.

    ``require_current_boot`` pins credential-encrypted shards to the boot that
    is active at packing time.  The default refuses shards read under an
    earlier boot because the first-unlock confirmation is per boot and must be
    re-established after a reboot; set it to ``False`` to accept a
    self-consistent record from a previous boot instead.
    """

    category = _coerce_category(record.category)

    def verdict(packable: bool, code: VerdictCode, detail: str) -> ReadVerdict:
        return ReadVerdict(
            shard_id=record.shard_id,
            category=category,
            attempt=record.attempt,
            packable=packable,
            code=code,
            detail=detail,
            evidence=record.evidence() if category is not None else {},
        )

    if category is None:
        return verdict(
            False,
            VerdictCode.MISSING_PERMISSION,
            f"unrecognised file category {record.category!r}",
        )
    if record.status is ReadStatus.OPEN:
        return verdict(False, VerdictCode.STILL_OPEN, "read has not finished")
    if record.status is ReadStatus.WITHDRAWN:
        return verdict(
            False,
            VerdictCode.WITHDRAWN,
            record.withdrawn_reason or "read was withdrawn",
        )
    if record.status is not ReadStatus.COMPLETED:
        return verdict(
            False,
            VerdictCode.MISSING_PERMISSION,
            f"unrecognised read status {record.status!r}",
        )

    start = record.state_at_start
    if start.get("booted") is not True:
        return verdict(
            False, VerdictCode.MISSING_PERMISSION, "read started while not booted"
        )
    if start.get("boot_id") != record.boot_id:
        return verdict(
            False,
            VerdictCode.STALE_BOOT,
            "recorded boot id does not match the state it claims to rely on",
        )

    if category is FileCategory.DEVICE_ENCRYPTED:
        # Readable from boot onwards; no unlock-derived state is involved, so a
        # shard completed under an earlier boot stays valid.
        return verdict(True, VerdictCode.OK, "device-encrypted shard readable at boot")

    if start.get("first_unlock_done") is not True:
        return verdict(
            False,
            VerdictCode.MISSING_PERMISSION,
            "credential-encrypted shard was read before the first unlock",
        )

    if require_current_boot and record.boot_id != current_boot_id:
        # The first unlock is confirmed per boot.  Data whose read depended on
        # it must be re-read after the next boot's first unlock.
        return verdict(
            False,
            VerdictCode.STALE_BOOT,
            "credential-encrypted shard predates the current boot; "
            "re-read after the first unlock",
        )

    if category is FileCategory.CREDENTIAL_ENCRYPTED:
        if record.locked_during_read:
            return verdict(
                True,
                VerdictCode.OK,
                "credential-encrypted shard continued across a lock after first unlock",
            )
        return verdict(True, VerdictCode.OK, "read under first unlock of this boot")

    # STRICT: must have been unlocked from start to finish.
    if start.get("unlocked") is not True:
        return verdict(
            False,
            VerdictCode.MISSING_PERMISSION,
            "strict shard started while the device was locked",
        )
    if record.locked_during_read:
        return verdict(
            False,
            VerdictCode.UNLOCK_GAP,
            "device locked before the strict read finished",
        )
    return verdict(True, VerdictCode.OK, "strict shard read while unlocked throughout")
