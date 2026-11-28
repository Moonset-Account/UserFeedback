"""Protection domains and their strictly per-domain state.

Each :class:`Domain` object names an immutable configuration (name, format,
rounding attribute, mask bits) and owns a mutable :class:`StickyLedger`
holding its sticky flags and pending-trap queue.  The ledger is created inside
the constructor, so no two domains ever share state; :meth:`Domain.copy`
performs a deep copy of the ledger.

There is intentionally no module-level mutable "current state" anywhere in the
code base.  The active domain is tracked solely by the :class:`Runtime`
stack, whose ``suspend``/``resume`` moves a whole ``DomainState`` snapshot in
and out atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Iterable

from .errors import FormatError, IsolationError, SuspendedDomainError
from .flags import (
    FLAG_ORDER,
    Flag,
    MaskBits,
    StickyLedger,
    coerce_flag,
    flags_text,
    ordered_flags,
)
from .format import Format, RoundingMode, coerce_rounding
from .ops import AddResult, FmaResult, MulResult, add, convert, fma, mul
from .rounding import Rounded
from .value import FPValue

__all__ = ["Domain", "DomainState", "OpResult", "OpTrace", "DomainConfig"]


@dataclass(frozen=True)
class DomainState:
    """Immutable snapshot of everything a domain can observe about itself."""

    name: str
    format: Format
    rounding: RoundingMode
    masks: MaskBits
    sticky: frozenset[Flag]
    pending: tuple[Flag, ...]

    @property
    def raised(self) -> tuple[Flag, ...]:
        """Sticky flags that are currently unmasked (actionable as traps)."""

        return tuple(
            flag
            for flag in FLAG_ORDER
            if flag in self.sticky and self.masks.is_unmasked(flag)
        )

    def describe(self) -> str:
        return (
            f"domain {self.name!r} fmt={self.format.name} "
            f"rounding={self.rounding.value} [{self.masks.describe()}] "
            f"sticky={flags_text(self.sticky)} "
            f"pending=[{', '.join(flag.value for flag in self.pending)}]"
        )


@dataclass(frozen=True)
class DomainConfig:
    """The immutable configuration part of a domain."""

    name: str
    format: Format
    rounding: RoundingMode = RoundingMode.RNE
    masks: MaskBits = field(default_factory=MaskBits.all_masked)


@dataclass(frozen=True)
class OpResult:
    """One operation's outcome together with the state left in its domain."""

    value: FPValue
    flags: frozenset[Flag]
    pending: tuple[Flag, ...]
    state: DomainState

    def __str__(self) -> str:
        return (
            f"{self.value} flags={flags_text(self.flags)} "
            f"pending=[{', '.join(flag.value for flag in self.pending)}]"
        )


@dataclass(frozen=True)
class OpTrace:
    """Record of a fused multiply-add's exact intermediates and its rounding."""

    a: Fraction
    b: Fraction
    c: Fraction
    product: Fraction
    exact: Fraction
    result: OpResult

    def describe(self) -> str:
        return (
            f"fma({self.a}, {self.b}, {self.c}) product={self.product} "
            f"exact={self.exact} -> {self.result}"
        )


class Domain:
    """A protection domain with its own rounding, masks and sticky state.

    The configuration is exposed as read-only properties; change it with
    :meth:`with_rounding` / :meth:`with_masks` / :meth:`with_format`, which
    return a *new* domain carrying a deep copy of the current ledger.  That
    makes it structurally impossible for a configuration change to alias
    another domain's flags.
    """

    __slots__ = ("_config", "_ledger", "_active", "_suspended", "_owner")

    def __init__(
        self,
        name: str,
        format: Format,
        rounding: RoundingMode | str = RoundingMode.RNE,
        masks: MaskBits | None = None,
        *,
        _ledger: StickyLedger | None = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise FormatError("domain name must be a non-empty string")
        if not isinstance(format, Format):
            raise FormatError("domain format must be a Format")
        if masks is None:
            masks = MaskBits.all_masked()
        if not isinstance(masks, MaskBits):
            raise FormatError("domain masks must be MaskBits")
        self._config = DomainConfig(
            name=name,
            format=format,
            rounding=coerce_rounding(rounding),
            masks=masks,
        )
        self._ledger = _ledger.copy() if _ledger is not None else StickyLedger()
        self._active = False
        self._suspended = False
        self._owner = None

    # -- configuration ------------------------------------------------------
    @property
    def name(self) -> str:
        return self._config.name

    @property
    def format(self) -> Format:
        return self._config.format

    @property
    def rounding(self) -> RoundingMode:
        return self._config.rounding

    @property
    def masks(self) -> MaskBits:
        return self._config.masks

    @property
    def config(self) -> DomainConfig:
        return self._config

    @property
    def active(self) -> bool:
        """True while this domain is the runtime's innermost active domain."""

        return self._active

    @property
    def suspended(self) -> bool:
        """True while a nested domain is running and this one is paused."""

        return self._suspended

    def with_rounding(self, rounding: RoundingMode | str) -> "Domain":
        clone = self._clone()
        clone._config = replace(clone._config, rounding=coerce_rounding(rounding))
        return clone

    def with_masks(self, masks: MaskBits) -> "Domain":
        if not isinstance(masks, MaskBits):
            raise FormatError("masks must be MaskBits")
        clone = self._clone()
        clone._config = replace(clone._config, masks=masks)
        return clone

    def with_format(self, fmt: Format) -> "Domain":
        if not isinstance(fmt, Format):
            raise FormatError("format must be a Format")
        clone = self._clone()
        clone._config = replace(clone._config, format=fmt)
        return clone

    def renamed(self, name: str) -> "Domain":
        clone = self._clone()
        clone._config = replace(clone._config, name=name)
        return clone

    def copy(self) -> "Domain":
        """Independent copy of this domain, with a deep-copied ledger."""

        return self._clone()

    def _clone(self) -> "Domain":
        return Domain(
            name=self.name,
            format=self.format,
            rounding=self.rounding,
            masks=self.masks,
            _ledger=self._ledger.copy(),
        )

    # -- state access -------------------------------------------------------
    def state(self) -> DomainState:
        """Current snapshot of the domain's own state."""

        return DomainState(
            name=self.name,
            format=self.format,
            rounding=self.rounding,
            masks=self.masks,
            sticky=frozenset(self._ledger.sticky),
            pending=tuple(self._ledger.pending),
        )

    @property
    def sticky(self) -> frozenset[Flag]:
        return frozenset(self._ledger.sticky)

    @property
    def pending(self) -> tuple[Flag, ...]:
        return tuple(self._ledger.pending)

    @property
    def raised(self) -> tuple[Flag, ...]:
        return self.state().raised

    def has_flag(self, flag: Flag | str) -> bool:
        return coerce_flag(flag) in self._ledger.sticky

    def take_pending(self) -> tuple[Flag, ...]:
        """Remove and return this domain's queued pending traps.

        A suspended domain must be resumed before its traps can be drained, so
        a nested audit domain cannot swallow the outer domain's pending
        exceptions.
        """

        self._require_runnable("drain pending traps")
        return self._ledger.take_pending()

    def clear_sticky(self) -> None:
        """Clear the sticky flags (the pending queue is untouched)."""

        self._require_runnable("clear sticky flags")
        self._ledger.clear_sticky()

    def clear_pending(self) -> None:
        self._require_runnable("clear pending traps")
        self._ledger.clear_pending()

    def clear_flags(self) -> None:
        self._require_runnable("clear flags")
        self._ledger.clear_sticky()
        self._ledger.clear_pending()

    @property
    def ledger(self) -> StickyLedger:
        """The live ledger.  Only the runtime and tests should need this."""

        return self._ledger

    # -- arithmetic ---------------------------------------------------------
    def _require_runnable(self, action: str = "execute operations") -> None:
        """Refuse state changes unless this domain may legitimately run.

        Two rules, both aimed at the leak this model exists to prevent:

        * A **suspended** domain cannot run or drain/clear anything while a
          nested domain is executing.
        * A domain registered with a runtime whose stack is non-empty *must be
          the innermost active domain* to change floating-point state.  A
          dormant neighbour cannot latch flags into itself behind the back of
          the domain the runtime believes is executing.

        Domains that are not registered with any runtime are pure calculators
        and are always runnable.
        """

        if self._suspended:
            raise SuspendedDomainError(
                f"domain {self.name!r} is suspended and cannot {action}; only "
                "the innermost active domain may change floating-point state"
            )
        owner = self._owner
        if owner is not None and owner.depth > 0 and owner.active is not self:
            raise IsolationError(
                f"domain {self.name!r} is not the active domain "
                f"({owner.active.name!r} is); a dormant or suspended domain "
                f"cannot {action}"
            )

    def _finish(
        self,
        rounded: Rounded,
        *,
        extra_pending: Iterable[Flag] = (),
    ) -> OpResult:
        flags = ordered_flags(set(rounded.flags) | set(extra_pending))
        newly_pending = self._ledger.latch(flags, self.masks)
        return OpResult(
            value=rounded.value,
            flags=frozenset(flags),
            pending=tuple(newly_pending),
            state=self.state(),
        )

    def add(self, x: object, y: object) -> OpResult:
        self._require_runnable()
        result: AddResult = add(x, y, self.format, self.rounding)
        return self._finish(result.rounded)

    def mul(self, x: object, y: object) -> OpResult:
        self._require_runnable()
        result: MulResult = mul(x, y, self.format, self.rounding)
        return self._finish(result.rounded)

    def fma(self, a: object, b: object, c: object) -> OpTrace:
        self._require_runnable()
        result: FmaResult = fma(a, b, c, self.format, self.rounding)
        return OpTrace(
            a=result.a,
            b=result.b,
            c=result.c,
            product=result.product,
            exact=result.exact,
            result=self._finish(result.rounded),
        )

    def convert(self, value: object) -> OpResult:
        self._require_runnable()
        return self._finish(convert(value, self.format, self.rounding))

    def __repr__(self) -> str:
        return (
            f"Domain({self.name!r}, {self.format.name}, "
            f"rounding={self.rounding.value}, masks={self.masks!r}, "
            f"sticky={flags_text(self.sticky)})"
        )
