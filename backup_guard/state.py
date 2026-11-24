"""Trust state derived from the event stream, and the errors it raises."""

from __future__ import annotations

from dataclasses import dataclass

from .events import EventKind


class GuardError(Exception):
    """Base class for backup read-permission failures."""


class NotBootedError(GuardError):
    """No active boot generation (never booted, or powered off)."""


class StaleBootError(GuardError):
    """Event or read belongs to a boot generation that is no longer current."""


class FirstUnlockRequiredError(GuardError):
    """Credential-encrypted data was read before the first unlock of this boot."""


class DeviceLockedError(GuardError):
    """Data requiring the device to be unlocked was read while locked."""


class EventOrderError(GuardError):
    """The event stream is out of order or otherwise inconsistent."""


class ShardError(GuardError):
    """A backup shard operation is invalid for the shard's current state."""


# Imported after the error classes to keep the public error surface together.
from .shards import FileCategory  # noqa: E402  (circular-safe: shards has no state import)


@dataclass(frozen=True, slots=True)
class TrustState:
    """Immutable snapshot of what the device currently permits.

    ``first_unlock_done`` is sticky for the lifetime of one boot: once the user
    has unlocked after booting, credential-encrypted files stay readable even
    while the screen is locked again.  ``unlocked`` is the live screen state and
    is what strict (always-unlocked) reads depend on.
    """

    boot_id: str | None
    booted: bool
    unlocked: bool
    first_unlock_done: bool
    seq: int

    @property
    def boot_generation(self) -> str | None:
        return self.boot_id if self.booted else None

    def readable(self, category: FileCategory) -> bool:
        if not self.booted:
            return False
        if category is FileCategory.DEVICE_ENCRYPTED:
            return True
        if not self.first_unlock_done:
            return False
        if category is FileCategory.CREDENTIAL_ENCRYPTED:
            return True
        if category is FileCategory.CREDENTIAL_ENCRYPTED_STRICT:
            return self.unlocked
        raise ValueError(f"unknown file category {category!r}")

    def check(self, category: FileCategory) -> None:
        """Raise the precise error explaining why ``category`` is unreadable."""

        if not self.booted:
            raise NotBootedError("device is not booted")
        if category is FileCategory.DEVICE_ENCRYPTED:
            return
        if category is not FileCategory.CREDENTIAL_ENCRYPTED and (
            category is not FileCategory.CREDENTIAL_ENCRYPTED_STRICT
        ):
            raise ValueError(f"unknown file category {category!r}")
        if not self.first_unlock_done:
            raise FirstUnlockRequiredError(
                f"{category.value} requires the first unlock since boot "
                f"(boot_id={self.boot_id!r})"
            )
        if category is FileCategory.CREDENTIAL_ENCRYPTED:
            return
        if not self.unlocked:
            raise DeviceLockedError(
                f"{category.value} requires the device to stay unlocked"
            )

    def describe(self) -> dict[str, object]:
        return {
            "boot_id": self.boot_id,
            "booted": self.booted,
            "unlocked": self.unlocked,
            "first_unlock_done": self.first_unlock_done,
            "seq": self.seq,
        }


def initial_state() -> TrustState:
    return TrustState(
        boot_id=None, booted=False, unlocked=False, first_unlock_done=False, seq=-1
    )


def apply_event(state: TrustState, event: "object") -> TrustState:
    """Return the successor state.  Validation of ordering is the log's job."""

    from .events import LockEvent, PowerEvent

    if isinstance(event, PowerEvent):
        if event.kind is EventKind.BOOT:
            return TrustState(
                boot_id=event.boot_id,
                booted=True,
                unlocked=False,
                first_unlock_done=False,
                seq=event.seq,
            )
        return TrustState(
            boot_id=state.boot_id,
            booted=False,
            unlocked=False,
            first_unlock_done=False,
            seq=event.seq,
        )

    if not isinstance(event, LockEvent):
        raise TypeError(f"unknown event type {type(event)!r}")

    if event.kind is EventKind.UNLOCK:
        return TrustState(
            boot_id=state.boot_id,
            booted=state.booted,
            unlocked=True,
            first_unlock_done=state.first_unlock_done or state.booted,
            seq=event.seq,
        )
    return TrustState(
        boot_id=state.boot_id,
        booted=state.booted,
        unlocked=False,
        first_unlock_done=state.first_unlock_done,
        seq=event.seq,
    )
