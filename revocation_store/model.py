"""Core value objects for the local certificate-revocation state store.

The model mirrors the vocabulary used by the validator's feed:

* every table names an issuer (``issuer``), a distribution scope (``scope``),
  the baseline it belongs to (``base_number``) and the half-open period in
  which it is usable (``this_update`` / ``next_update``);
* a ``FULL`` table is a complete baseline snapshot and its own baseline number;
* a ``DELTA`` table is a *complete change set* relative to exactly one baseline
  and carries its own delta number.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Optional, Tuple

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class TableValidationError(ValueError):
    """Raised when a table is structurally malformed and must not be stored."""


class ConflictingTableError(ValueError):
    """Raised when a table identity is reused with different content."""


class Reason(str, Enum):
    """Revocation entry reasons (RFC 5280 reason codes)."""

    UNSPECIFIED = "unspecified"
    KEY_COMPROMISE = "keyCompromise"
    CA_COMPROMISE = "cACompromise"
    AFFILIATION_CHANGED = "affiliationChanged"
    SUPERSEDED = "superseded"
    CESSATION_OF_OPERATION = "cessationOfOperation"
    CERTIFICATE_HOLD = "certificateHold"
    PRIVILEGE_WITHDRAWN = "privilegeWithdrawn"
    AA_COMPROMISE = "aACompromise"
    REMOVE_FROM_CRL = "removeFromCRL"

    @property
    def is_permanent(self) -> bool:
        """Permanent revocations survive every later table in a lifecycle."""
        return self not in (Reason.CERTIFICATE_HOLD, Reason.REMOVE_FROM_CRL)

    @property
    def is_hold(self) -> bool:
        return self is Reason.CERTIFICATE_HOLD

    @property
    def is_removal(self) -> bool:
        return self is Reason.REMOVE_FROM_CRL


class TableKind(str, Enum):
    FULL = "FULL"
    DELTA = "DELTA"


class Status(str, Enum):
    REVOKED = "revoked"
    SUSPENDED = "suspended"
    NOT_LISTED = "not_listed"
    UNKNOWN = "unknown"


def to_utc(value: datetime, field_name: str = "time") -> datetime:
    """Normalise an aware datetime to UTC, rejecting naive input."""
    if not isinstance(value, datetime):
        raise TableValidationError(f"{field_name} must be a datetime, got {type(value)!r}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TableValidationError(
            f"{field_name} must be timezone-aware (e.g. datetime(..., tzinfo=timezone.utc))"
        )
    return value.astimezone(UTC)


def to_microseconds(value: datetime) -> int:
    """Exact integer epoch microseconds (avoids float rounding)."""
    value = to_utc(value)
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_microseconds(value: int) -> datetime:
    return EPOCH + timedelta(microseconds=int(value))


def isoformat_utc(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def parse_reason(value: object) -> Reason:
    if isinstance(value, Reason):
        return value
    try:
        return Reason(str(value))
    except ValueError as exc:
        raise TableValidationError(f"unknown revocation reason: {value!r}") from exc


@dataclass(frozen=True)
class TableEntry:
    serial: str
    reason: Reason
    revocation_time: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.serial, str) or not self.serial.strip():
            raise TableValidationError("entry serial must be a non-empty string")
        object.__setattr__(self, "reason", parse_reason(self.reason))
        if self.revocation_time is not None:
            object.__setattr__(
                self, "revocation_time", to_utc(self.revocation_time, "revocation_time")
            )

    def canonical(self) -> dict:
        return {
            "serial": self.serial,
            "reason": self.reason.value,
            "revocation_time": (
                isoformat_utc(self.revocation_time) if self.revocation_time else None
            ),
        }


@dataclass(frozen=True)
class Table:
    kind: TableKind
    issuer: str
    scope: str
    base_number: int
    number: int
    this_update: datetime
    next_update: datetime
    entries: Tuple[TableEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        try:
            kind = TableKind(self.kind)
        except ValueError as exc:
            raise TableValidationError(f"unknown table kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)

        for name in ("issuer", "scope"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise TableValidationError(f"table {name} must be a non-empty string")

        for name in ("base_number", "number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise TableValidationError(f"table {name} must be a positive integer")

        this_update = to_utc(self.this_update, "this_update")
        next_update = to_utc(self.next_update, "next_update")
        object.__setattr__(self, "this_update", this_update)
        object.__setattr__(self, "next_update", next_update)
        if next_update <= this_update:
            raise TableValidationError("next_update must be strictly after this_update")

        if kind is TableKind.FULL and self.base_number != self.number:
            raise TableValidationError(
                "a FULL table is its own baseline: base_number must equal number"
            )

        normalized = []
        seen = set()
        for entry in self.entries:
            if not isinstance(entry, TableEntry):
                raise TableValidationError("entries must be TableEntry instances")
            if entry.serial in seen:
                raise TableValidationError(
                    f"duplicate serial {entry.serial!r} in one table"
                )
            if kind is TableKind.FULL and entry.reason.is_removal:
                raise TableValidationError(
                    "removeFromCRL is only meaningful in a DELTA table"
                )
            seen.add(entry.serial)
            normalized.append(entry)
        # Sort for a deterministic fingerprint; order has no semantic meaning.
        normalized.sort(key=lambda item: item.serial)
        object.__setattr__(self, "entries", tuple(normalized))

    # -- constructors -----------------------------------------------------
    @classmethod
    def full(
        cls,
        *,
        issuer: str,
        scope: str,
        number: int,
        this_update: datetime,
        next_update: datetime,
        entries: Iterable[TableEntry] = (),
    ) -> "Table":
        return cls(
            kind=TableKind.FULL,
            issuer=issuer,
            scope=scope,
            base_number=number,
            number=number,
            this_update=this_update,
            next_update=next_update,
            entries=tuple(entries),
        )

    @classmethod
    def delta(
        cls,
        *,
        issuer: str,
        scope: str,
        base_number: int,
        number: int,
        this_update: datetime,
        next_update: datetime,
        entries: Iterable[TableEntry] = (),
    ) -> "Table":
        return cls(
            kind=TableKind.DELTA,
            issuer=issuer,
            scope=scope,
            base_number=base_number,
            number=number,
            this_update=this_update,
            next_update=next_update,
            entries=tuple(entries),
        )

    # -- helpers ----------------------------------------------------------
    def canonical(self) -> dict:
        return {
            "kind": self.kind.value,
            "issuer": self.issuer,
            "scope": self.scope,
            "base_number": self.base_number,
            "number": self.number,
            "this_update": isoformat_utc(self.this_update),
            "next_update": isoformat_utc(self.next_update),
            "entries": [entry.canonical() for entry in self.entries],
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Evidence:
    """A concrete stored table that contributed to a query answer."""

    table_id: int
    kind: TableKind
    number: int
    base_number: int
    this_update: datetime
    next_update: datetime

    def as_dict(self) -> dict:
        return {
            "table_id": self.table_id,
            "kind": self.kind.value,
            "number": self.number,
            "base_number": self.base_number,
            "this_update": isoformat_utc(self.this_update),
            "next_update": isoformat_utc(self.next_update),
        }


@dataclass(frozen=True)
class QueryResult:
    status: Status
    issuer: str
    scope: str
    serial: str
    at: datetime
    baseline: Optional[Evidence] = None
    delta: Optional[Evidence] = None
    reason: Optional[Reason] = None
    source: Optional[Evidence] = None
    explanation: str = ""

    @property
    def is_definitive_non_revocation(self) -> bool:
        """True only when complete, valid, same-scope evidence proves absence."""
        return self.status is Status.NOT_LISTED

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "issuer": self.issuer,
            "scope": self.scope,
            "serial": self.serial,
            "at": isoformat_utc(self.at),
            "reason": self.reason.value if self.reason else None,
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "delta": self.delta.as_dict() if self.delta else None,
            "source": self.source.as_dict() if self.source else None,
            "definitive_non_revocation": self.is_definitive_non_revocation,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class IngestResult:
    table_id: int
    inserted: bool
    backfilled: bool
    latest_number: int
    explanation: str

    def as_dict(self) -> dict:
        return {
            "table_id": self.table_id,
            "inserted": self.inserted,
            "backfilled": self.backfilled,
            "latest_number": self.latest_number,
            "explanation": self.explanation,
        }
