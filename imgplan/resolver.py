"""Resolution adapter protocol and an in-memory, mutable implementation.

The planner never touches the DNS itself. A resolver adapter is any object
with a synchronous ``resolve(hostname) -> Resolution`` coroutine-free method;
:class:`InMemoryResolver` is a deterministic, scriptable implementation used
for tests and demonstrations whose answers can be swapped at runtime to
simulate DNS rebinding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Resolution:
    """One resolver answer snapshot.

    ``cname_chain`` is the full chain in resolution order, starting with the
    queried hostname and ending with the canonical name that owns the
    addresses (a one-element tuple when no CNAME exists).

    ``addresses`` is the *complete* address set (A and AAAA, if any). Every
    address - not only the one selected for dialing - is policy checked.

    ``ttl_seconds`` is the remaining validity of the snapshot as reported by
    the adapter; it must be a positive number.
    """

    hostname: str
    cname_chain: tuple[str, ...]
    addresses: tuple[str, ...]
    ttl_seconds: float


@runtime_checkable
class ResolverAdapter(Protocol):
    """Structural type every resolver adapter must satisfy."""

    def resolve(self, hostname: str) -> Resolution:
        """Return a fresh :class:`Resolution` for *hostname*."""
        ...


@dataclass
class InMemoryResolver:
    """Scriptable resolver backed entirely by memory.

    Records are added with :meth:`set_record`; answers can be replaced at any
    time with another :meth:`set_record` call, which is how the rebinding
    demonstration changes results between hops. Every call is logged so tests
    can prove that re-planning really re-queries the name instead of reusing
    an old snapshot.
    """

    _records: dict[str, Resolution] = field(default_factory=dict)
    _call_log: list[tuple[str, int]] = field(default_factory=list)

    def set_record(
        self,
        hostname: str,
        cname_chain: list[str] | tuple[str, ...],
        addresses: list[str] | tuple[str, ...],
        ttl_seconds: float,
    ) -> None:
        chain = tuple(cname_chain)
        if not chain:
            raise ValueError("cname_chain must contain at least the queried hostname")
        if chain[0] != hostname:
            raise ValueError("cname_chain must start with the queried hostname")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not addresses:
            raise ValueError("addresses must not be empty")
        self._records[hostname.lower()] = Resolution(
            hostname=hostname.lower(),
            cname_chain=chain,
            addresses=tuple(addresses),
            ttl_seconds=float(ttl_seconds),
        )

    def resolve(self, hostname: str) -> Resolution:
        key = hostname.lower()
        count = self.call_count(hostname) + 1
        self._call_log.append((key, count))
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"no in-memory record for {hostname!r}")
        return record

    def call_count(self, hostname: str) -> int:
        key = hostname.lower()
        return sum(1 for name, _ in self._call_log if name == key)

    @property
    def total_calls(self) -> int:
        return len(self._call_log)

    def call_sequence(self) -> tuple[str, ...]:
        """Hostnames in the exact order they were queried."""
        return tuple(name for name, _ in self._call_log)

    def replace_addresses(
        self,
        hostname: str,
        addresses: list[str] | tuple[str, ...],
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        """Swap only the address set of an existing record (rebinding helper)."""
        record = self._records[hostname.lower()]
        self._records[hostname.lower()] = Resolution(
            hostname=record.hostname,
            cname_chain=record.cname_chain,
            addresses=tuple(addresses),
            ttl_seconds=record.ttl_seconds if ttl_seconds is None else float(ttl_seconds),
        )
