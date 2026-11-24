"""Power and lock-screen events, each tagged with a boot id.

Every event carries the ``boot_id`` of the device boot it belongs to.  A new
``boot_id`` means a new boot; the first-unlock confirmation from the previous
boot never carries over.

Event kinds and the transitions they drive:

``BOOT``
    The device powers on (or reboots).  Starts a fresh boot generation; the
    device is locked and has not been unlocked yet.
``UNLOCK``
    The user unlocked the screen.  Before the first unlock of a boot this also
    satisfies the "first unlock since boot" gate for credential-encrypted files.
``LOCK``
    The screen turned off/locked.  Strictly-unlocked reads that are still
    in flight must be withdrawn.
``SHUTDOWN``
    The device powers off orderly.  Any in-flight shard reads become
    unverifiable (their boot generation ends).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class EventKind(str, enum.Enum):
    BOOT = "boot"
    UNLOCK = "unlock"
    LOCK = "lock"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class PowerEvent:
    """A power-state change: boot or shutdown."""

    kind: EventKind
    boot_id: str
    seq: int

    def __post_init__(self) -> None:
        if self.kind not in (EventKind.BOOT, EventKind.SHUTDOWN):
            raise ValueError(f"{self.kind!r} is not a power event")
        _validate_common(self.boot_id, self.seq)

    @classmethod
    def boot(cls, boot_id: str, seq: int) -> "PowerEvent":
        return cls(EventKind.BOOT, boot_id, seq)

    @classmethod
    def shutdown(cls, boot_id: str, seq: int) -> "PowerEvent":
        return cls(EventKind.SHUTDOWN, boot_id, seq)


@dataclass(frozen=True, slots=True)
class LockEvent:
    """A screen lock-state change: unlock or lock."""

    kind: EventKind
    boot_id: str
    seq: int

    def __post_init__(self) -> None:
        if self.kind not in (EventKind.UNLOCK, EventKind.LOCK):
            raise ValueError(f"{self.kind!r} is not a lock event")
        _validate_common(self.boot_id, self.seq)

    @classmethod
    def unlock(cls, boot_id: str, seq: int) -> "LockEvent":
        return cls(EventKind.UNLOCK, boot_id, seq)

    @classmethod
    def lock(cls, boot_id: str, seq: int) -> "LockEvent":
        return cls(EventKind.LOCK, boot_id, seq)


Event = PowerEvent | LockEvent


def _validate_common(boot_id: str, seq: int) -> None:
    if not isinstance(boot_id, str) or not boot_id:
        raise ValueError("boot_id must be a non-empty string")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ValueError("seq must be an int")
    if seq < 0:
        raise ValueError("seq must be non-negative")
