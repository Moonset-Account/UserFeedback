"""Backup read-permission guard for Android-style file-based encryption.

The device has three file protection classes:

* ``DEVICE_ENCRYPTED`` -- readable as soon as the device boots.
* ``CREDENTIAL_ENCRYPTED`` -- readable only after the first unlock since the
  current boot.
* ``CREDENTIAL_ENCRYPTED_STRICT`` -- readable only while the device is
  unlocked for the whole read.

The module models power/lock events, the trust state derived from them, and a
ledger of backup-shard reads so a shard's provenance can be checked before it
is packed into a backup archive.
"""

from .events import Event, EventKind, LockEvent, PowerEvent
from .guard import BackupReadGuard, PackReport
from .shards import (
    FileCategory,
    ReadRecord,
    ReadStatus,
    ReadVerdict,
    VerdictCode,
    verify_record,
)
from .state import (
    DeviceLockedError,
    EventOrderError,
    FirstUnlockRequiredError,
    GuardError,
    NotBootedError,
    ShardError,
    StaleBootError,
    TrustState,
)

__all__ = [
    "BackupReadGuard",
    "DeviceLockedError",
    "Event",
    "EventKind",
    "EventOrderError",
    "FileCategory",
    "FirstUnlockRequiredError",
    "GuardError",
    "LockEvent",
    "NotBootedError",
    "PackReport",
    "PowerEvent",
    "ReadRecord",
    "ReadStatus",
    "ReadVerdict",
    "ShardError",
    "StaleBootError",
    "TrustState",
    "VerdictCode",
    "verify_record",
]

__version__ = "1.0.0"
