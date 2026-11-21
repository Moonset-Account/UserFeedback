"""Resource-record model and validation for the in-memory authoritative zone.

Supported data types are ``A``, ``AAAA``, ``TXT`` and ``NS``.  ``NS`` is only
meaningful as delegation data (or as the zone's own apex NS set); it is stored
like any other RRset.  Queries are answered for ``A``, ``AAAA`` and ``TXT``;
other type mnemonics are accepted syntactically and simply have no data, which
yields an ordinary "no data of this type" response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Tuple

from .names import InvalidDNSName, parse_name, to_text

DEFAULT_TTL = 300
MAX_TTL = 2**31 - 1

SUPPORTED_RECORD_TYPES = frozenset({"A", "AAAA", "TXT", "NS"})
SUPPORTED_QUERY_TYPES = frozenset({"A", "AAAA", "TXT"})

_TYPE_RE = re.compile(r"^[A-Za-z0-9]+$")
_TEXT_CHUNK_LIMIT = 255


class RecordError(ValueError):
    """Base class for record validation failures."""


class InvalidRecord(RecordError):
    """Raised when an RR cannot be constructed or validated."""


def normalise_rtype(rtype: str) -> str:
    """Validate and uppercase an RR type mnemonic."""
    if not isinstance(rtype, str):
        raise InvalidRecord(
            f"record type must be str, got {type(rtype).__name__}"
        )
    text = rtype.strip().upper()
    if not text or not _TYPE_RE.fullmatch(text):
        raise InvalidRecord(f"invalid record type: {rtype!r}")
    return text


def _normalise_txt(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        chunks: Tuple[str, ...] = (value,)
    elif isinstance(value, (tuple, list)):
        if not value:
            raise InvalidRecord("TXT value must contain at least one string")
        if not all(isinstance(chunk, str) for chunk in value):
            raise InvalidRecord("TXT chunks must all be strings")
        chunks = tuple(value)
    else:
        raise InvalidRecord(
            "TXT value must be a string or a sequence of strings, "
            f"got {type(value).__name__}"
        )
    for chunk in chunks:
        if len(chunk.encode("utf-8")) > _TEXT_CHUNK_LIMIT:
            raise InvalidRecord(
                "each TXT character-string must be at most 255 octets"
            )
    return chunks


def _normalise_value(rtype: str, value: Any) -> Any:
    if rtype in ("A", "AAAA"):
        if not isinstance(value, str):
            raise InvalidRecord(f"{rtype} value must be a string")
        address_cls = IPv4Address if rtype == "A" else IPv6Address
        try:
            return str(address_cls(value))
        except ValueError as exc:
            raise InvalidRecord(f"invalid {rtype} address {value!r}: {exc}") from exc

    if rtype == "NS":
        if not isinstance(value, str):
            raise InvalidRecord("NS value must be a domain name string")
        try:
            return to_text(parse_name(value))
        except (InvalidDNSName, TypeError) as exc:
            raise InvalidRecord(f"invalid NS target {value!r}: {exc}") from exc

    if rtype == "TXT":
        return _normalise_txt(value)

    raise InvalidRecord(
        f"unsupported record type {rtype!r}; "
        f"supported types: {', '.join(sorted(SUPPORTED_RECORD_TYPES))}"
    )


@dataclass(frozen=True)
class Record:
    """A single resource record.

    ``value`` is a string for ``A``, ``AAAA`` and ``NS`` records and a tuple of
    character-strings for ``TXT`` records.  Names and addresses are stored in
    canonical form, so records compare independent of input case and a
    trailing dot.
    """

    name: str
    rtype: str
    value: Any
    ttl: int = DEFAULT_TTL

    def __post_init__(self) -> None:
        try:
            labels = parse_name(self.name)
        except (InvalidDNSName, TypeError) as exc:
            raise InvalidRecord(f"invalid owner name {self.name!r}: {exc}") from exc
        object.__setattr__(self, "name", to_text(labels))

        rtype = normalise_rtype(self.rtype)
        if rtype not in SUPPORTED_RECORD_TYPES:
            raise InvalidRecord(
                f"unsupported record type {rtype!r}; "
                f"supported types: {', '.join(sorted(SUPPORTED_RECORD_TYPES))}"
            )
        object.__setattr__(self, "rtype", rtype)

        if isinstance(self.ttl, bool) or not isinstance(self.ttl, int):
            raise InvalidRecord("TTL must be an integer")
        if not 0 <= self.ttl <= MAX_TTL:
            raise InvalidRecord(f"TTL out of range: {self.ttl}")
        object.__setattr__(self, "value", _normalise_value(rtype, self.value))

    @property
    def labels(self) -> Tuple[str, ...]:
        return parse_name(self.name)

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation (TXT as a list of strings)."""
        value: Any
        if self.rtype == "TXT":
            value = list(self.value)
        else:
            value = self.value
        return {
            "name": self.name,
            "type": self.rtype,
            "ttl": self.ttl,
            "value": value,
        }

    def __str__(self) -> str:  # pragma: no cover - debugging convenience
        if self.rtype == "TXT":
            rendered = " ".join(_quote(chunk) for chunk in self.value)
        else:
            rendered = str(self.value)
        return f"{self.name} {self.ttl} IN {self.rtype} {rendered}"


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
