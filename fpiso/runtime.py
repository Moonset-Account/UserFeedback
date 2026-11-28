"""The shared compute runtime: a strict protection-domain stack.

The runtime is the only object that knows which protection domain is currently
executing.  It enforces the following invariants on every transition:

1. **LIFO nesting only.**  A domain may be entered only while the previously
   active domain is suspended, and only the innermost domain may exit.
2. **Suspension is total.**  On suspension the runtime records an immutable
   snapshot of the outer domain's state and marks the domain suspended, so any
   attempt to run an operation inside it raises ``SuspendedDomainError``.  On
   resumption the snapshot is compared with the live state; any difference
   raises ``IsolationError``.
3. **No state sharing.**  Entering a domain copies nothing from, and clears
   nothing in, another domain.  Exiting a domain touches only that domain's
   own ledger.
4. **No implicit flag movement.**  Pending traps queued in an inner domain are
   reported to the caller of ``exit_domain``; they are never appended to the
   outer domain's queue.  An inner exit never drains the outer queue.
5. **Exact accounting.**  Each domain's ledger keeps an append-only log of the
   pending traps it ever queued, so the exit report states exactly which traps
   were generated during that tenure, even if the domain drained its own queue
   in the meantime.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Iterator

from .domain import Domain, DomainState, OpResult, OpTrace
from .errors import (
    DomainActiveError,
    DomainLifecycleError,
    DomainOwnershipError,
    IsolationError,
    NoActiveDomainError,
)
from .flags import Flag, flags_text


def _names(flags: tuple[Flag, ...]) -> str:
    return "[" + ", ".join(flag.value for flag in flags) + "]"


@dataclass(frozen=True)
class ExitReport:
    """What one domain's exit revealed, without touching any other domain."""

    name: str
    state: DomainState
    sticky_at_entry: frozenset[Flag]
    sticky_delta: frozenset[Flag]
    pending_at_entry: tuple[Flag, ...]
    pending_generated: tuple[Flag, ...]
    pending_remaining: tuple[Flag, ...]
    consumed: tuple[Flag, ...]
    ops: int

    def describe(self) -> str:
        return (
            f"exit {self.name!r}: ops={self.ops} "
            f"delta={flags_text(self.sticky_delta)} "
            f"generated={_names(self.pending_generated)} "
            f"remaining={_names(self.pending_remaining)} "
            f"consumed={_names(self.consumed)}"
        )


@dataclass
class _Frame:
    domain: Domain
    state_at_entry: DomainState
    pending_at_entry: tuple[Flag, ...]
    log_len_at_entry: int
    suspended_at: DomainState | None = None
    ops: int = 0


class Runtime:
    """A cooperative shared compute runtime with nested protection domains."""

    def __init__(self, *, name: str = "runtime") -> None:
        self.name = name
        self._stack: list[_Frame] = []
        self._registry: dict[str, Domain] = {}
        self.events: list[str] = []

    # -- introspection ------------------------------------------------------
    @property
    def active(self) -> Domain | None:
        return self._stack[-1].domain if self._stack else None

    @property
    def depth(self) -> int:
        return len(self._stack)

    def require_active(self) -> Domain:
        if not self._stack:
            raise NoActiveDomainError("no protection domain is active")
        return self._stack[-1].domain

    def stack_names(self) -> tuple[str, ...]:
        return tuple(frame.domain.name for frame in self._stack)

    def domain(self, name: str) -> Domain:
        try:
            return self._registry[name]
        except KeyError as exc:
            raise DomainOwnershipError(
                f"runtime does not own a domain named {name!r}"
            ) from exc

    def register(self, domain: Domain) -> Domain:
        if not isinstance(domain, Domain):
            raise DomainOwnershipError("register expects a Domain")
        existing = self._registry.get(domain.name)
        if existing is not None and existing is not domain:
            raise DomainOwnershipError(
                f"a different domain named {domain.name!r} is already registered"
            )
        self._check_owner(domain)
        domain._owner = self
        self._registry[domain.name] = domain
        return domain

    def inspect(self, name: str) -> DomainState:
        """Privileged read of a registered domain's own state.

        This is the harness/observation channel used by the demonstrations to
        show exactly what each domain holds while paused.  It returns data and
        never merges or moves flags.  A running domain has no API that could
        reach another domain's ledger; ``inspect`` exists on the runtime, not on
        ``Domain``.
        """

        return self.domain(name).state()

    def inspect_all(self) -> dict[str, DomainState]:
        return {name: domain.state() for name, domain in self._registry.items()}

    # -- lifecycle ----------------------------------------------------------
    def _check_owner(self, domain: Domain) -> None:
        owner = getattr(domain, "_owner", None)
        if owner is not None and owner is not self:
            raise DomainOwnershipError(
                f"domain {domain.name!r} belongs to another runtime"
            )

    def enter_domain(
        self,
        domain: Domain,
        *,
        label: str | None = None,
        audit: bool = False,
    ) -> Domain:
        if not isinstance(domain, Domain):
            raise DomainLifecycleError("enter_domain expects a Domain")
        self.register(domain)
        if any(frame.domain is domain for frame in self._stack):
            raise DomainActiveError(
                f"domain {domain.name!r} is already on the runtime stack"
            )

        if self._stack:
            outer = self._stack[-1]
            if outer.suspended_at is not None:
                raise DomainLifecycleError(
                    f"domain {outer.domain.name!r} is already suspended"
                )
            outer.suspended_at = outer.domain.state()
            outer.domain._suspended = True
            outer.domain._active = False
            self.events.append(
                f"suspend {outer.domain.name!r} "
                f"(sticky={flags_text(outer.suspended_at.sticky)}, "
                f"pending={len(outer.suspended_at.pending)})"
            )

        frame = _Frame(
            domain=domain,
            state_at_entry=domain.state(),
            pending_at_entry=tuple(domain.pending),
            log_len_at_entry=len(domain.ledger.pending_log),
        )
        self._stack.append(frame)
        domain._active = True
        domain._suspended = False
        kind = "audit" if audit else "protect"
        label_text = f" label={label!r}" if label else ""
        self.events.append(f"enter {kind} {domain.name!r}{label_text}")
        return domain

    def exit_domain(
        self,
        expected: str | None = None,
        *,
        consume_pending: bool = False,
    ) -> ExitReport:
        if not self._stack:
            raise DomainLifecycleError("exit_domain called with an empty stack")
        if expected is not None and self._stack[-1].domain.name != expected:
            raise DomainLifecycleError(
                f"cannot exit {expected!r}: innermost domain is "
                f"{self._stack[-1].domain.name!r} (exits must be LIFO)"
            )

        frame = self._stack.pop()
        domain = frame.domain
        state = domain.state()
        pending_now = tuple(domain.pending)
        domain._active = False
        domain._suspended = False

        consumed: tuple[Flag, ...] = ()
        if consume_pending:
            # The runtime owns the state machine, so it drains the ledger
            # directly: the public Domain API refuses to act for a domain that
            # is no longer the innermost active one.
            consumed = domain.ledger.take_pending()
            pending_remaining: tuple[Flag, ...] = ()
        else:
            pending_remaining = pending_now

        # Every trap queued during this tenure, in the order it was queued
        # (multiplicity preserved).  Using the append-only log makes this exact
        # even if the domain drained its own queue mid-tenure.
        generated = tuple(domain.ledger.pending_log[frame.log_len_at_entry:])

        delta = state.sticky - frame.state_at_entry.sticky

        self.events.append(
            f"exit {domain.name!r} delta={flags_text(delta)} "
            f"pending_left={len(pending_remaining)}"
        )

        if self._stack:
            outer = self._stack[-1]
            if outer.suspended_at is None:
                raise IsolationError(
                    f"nested exit left {outer.domain.name!r} without a "
                    "suspension snapshot"
                )
            restored = outer.domain.state()
            if restored != outer.suspended_at:
                raise IsolationError(
                    f"isolation violation: {outer.domain.name!r} changed while "
                    "suspended"
                )
            outer.suspended_at = None
            outer.domain._suspended = False
            outer.domain._active = True
            self.events.append(f"resume {outer.domain.name!r}")

        return ExitReport(
            name=domain.name,
            state=state,
            sticky_at_entry=frame.state_at_entry.sticky,
            sticky_delta=frozenset(delta),
            pending_at_entry=frame.pending_at_entry,
            pending_generated=generated,
            pending_remaining=pending_remaining,
            consumed=consumed,
            ops=frame.ops,
        )

    @contextlib.contextmanager
    def domain_scope(
        self,
        domain: Domain,
        *,
        label: str | None = None,
        audit: bool = False,
        consume_pending: bool = False,
    ) -> Iterator[Domain]:
        """Enter ``domain`` and always exit it, propagating exceptions."""

        entered = self.enter_domain(domain, label=label, audit=audit)
        try:
            yield entered
        finally:
            if self._stack and self._stack[-1].domain is domain:
                self.exit_domain(consume_pending=consume_pending)
            else:
                raise IsolationError(
                    f"stack corrupted while leaving domain {domain.name!r}"
                )

    @contextlib.contextmanager
    def audit_scope(
        self, domain: Domain, *, label: str | None = None
    ) -> Iterator[Domain]:
        with self.domain_scope(domain, label=label, audit=True) as entered:
            yield entered

    # -- operations dispatch ------------------------------------------------
    def add(self, x: object, y: object) -> OpResult:
        result = self.require_active().add(x, y)
        self._stack[-1].ops += 1
        return result

    def mul(self, x: object, y: object) -> OpResult:
        result = self.require_active().mul(x, y)
        self._stack[-1].ops += 1
        return result

    def fma(self, a: object, b: object, c: object) -> OpTrace:
        result = self.require_active().fma(a, b, c)
        self._stack[-1].ops += 1
        return result

    def convert(self, value: object) -> OpResult:
        result = self.require_active().convert(value)
        self._stack[-1].ops += 1
        return result

    def observe(self) -> DomainState:
        """Exactly what the currently executing domain can read about itself."""

        return self.require_active().state()
