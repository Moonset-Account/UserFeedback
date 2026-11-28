"""Exception flags, per-domain mask bits, and the sticky/pending ledger.

A mask bit of ``True`` means *masked*: the event is recorded in the domain's
sticky state but does not create a pending trap.  ``False`` means *unmasked*:
the event is recorded in the sticky state **and** appended to the domain's
pending-trap queue.

The sticky state and the pending queue are per protection domain.  They are
never global, so suspending a domain cannot let it observe another domain's
flags and resuming it restores only its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Flag(str, Enum):
    OVERFLOW = "overflow"
    UNDERFLOW = "underflow"
    INEXACT = "inexact"


# Stable display/order convention.
FLAG_ORDER: tuple[Flag, ...] = (Flag.OVERFLOW, Flag.UNDERFLOW, Flag.INEXACT)


def coerce_flag(value: "Flag | str") -> Flag:
    if isinstance(value, Flag):
        return value
    try:
        return Flag(str(value).lower())
    except ValueError as exc:
        raise ValueError(f"unknown flag: {value!r}") from exc


def ordered_flags(flags: Iterable[Flag]) -> tuple[Flag, ...]:
    """Return flags in the model's canonical order, de-duplicated."""

    present = {coerce_flag(flag) for flag in flags}
    return tuple(flag for flag in FLAG_ORDER if flag in present)


def flags_text(flags: Iterable[Flag]) -> str:
    ordered = ordered_flags(flags)
    if not ordered:
        return "{}"
    return "{" + ", ".join(flag.value for flag in ordered) + "}"


@dataclass(frozen=True)
class MaskBits:
    """Mask bits for one protection domain (True == masked / no trap)."""

    overflow: bool = True
    underflow: bool = True
    inexact: bool = True

    def __post_init__(self) -> None:
        for name in ("overflow", "underflow", "inexact"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"mask bit {name!r} must be bool")

    def is_masked(self, flag: "Flag | str") -> bool:
        return bool(getattr(self, coerce_flag(flag).value))

    def is_unmasked(self, flag: "Flag | str") -> bool:
        return not self.is_masked(flag)

    @property
    def masked(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in FLAG_ORDER if self.is_masked(flag))

    @property
    def unmasked(self) -> tuple[Flag, ...]:
        return tuple(flag for flag in FLAG_ORDER if self.is_unmasked(flag))

    @classmethod
    def all_masked(cls) -> "MaskBits":
        return cls(overflow=True, underflow=True, inexact=True)

    @classmethod
    def all_unmasked(cls) -> "MaskBits":
        return cls(overflow=False, underflow=False, inexact=False)

    @classmethod
    def unmasking(cls, *flags: Flag) -> "MaskBits":
        """Return mask bits with everything masked except ``flags``."""

        values = {coerce_flag(flag) for flag in flags}
        return cls(
            overflow=Flag.OVERFLOW not in values,
            underflow=Flag.UNDERFLOW not in values,
            inexact=Flag.INEXACT not in values,
        )

    def describe(self) -> str:
        parts = []
        for flag in FLAG_ORDER:
            parts.append(
                f"{flag.value}={'masked' if self.is_masked(flag) else 'unmasked'}"
            )
        return " ".join(parts)


@dataclass
class StickyLedger:
    """The mutable cumulative state owned by exactly one domain.

    ``sticky`` holds the cumulative status.  ``pending`` holds traps that were
    raised while unmasked and not yet drained.  ``pending_log`` is an
    append-only record of every trap ever queued, which lets the runtime report
    precisely which traps a domain generated during one tenure even if the
    domain drained its queue in the middle of that tenure.
    """

    sticky: set[Flag] = field(default_factory=set)
    pending: list[Flag] = field(default_factory=list)
    pending_log: list[Flag] = field(default_factory=list)
    raised_total: int = 0

    def latch(self, flags: Iterable[Flag], masks: MaskBits) -> tuple[Flag, ...]:
        """Record ``flags``; return the new pending entries in canonical order."""

        newly_pending: list[Flag] = []
        for flag in ordered_flags(flags):
            self.sticky.add(flag)
            self.raised_total += 1
            if masks.is_unmasked(flag):
                self.pending.append(flag)
                self.pending_log.append(flag)
                newly_pending.append(flag)
        return tuple(newly_pending)

    def take_pending(self) -> tuple[Flag, ...]:
        taken = tuple(self.pending)
        self.pending.clear()
        return taken

    def clear_sticky(self) -> None:
        self.sticky.clear()

    def clear_pending(self) -> None:
        self.pending.clear()

    def reset_audit_log(self) -> None:
        """Drop the historical pending log (it carries no live semantics)."""

        self.pending_log.clear()

    def copy(self) -> "StickyLedger":
        return StickyLedger(
            sticky=set(self.sticky),
            pending=list(self.pending),
            pending_log=list(self.pending_log),
            raised_total=self.raised_total,
        )
