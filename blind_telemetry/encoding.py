"""Canonical length-prefixed encoding and counter-based hash-to-unit mapping.

The signed plaintext is intentionally limited to the report's routing-free
identity: the cycle/period and a random ticket.  Both fields are encoded as
4-byte big-endian lengths followed by their bytes, so concatenations cannot be
repartitioned into different valid fields.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Final

from .errors import EncodingError, HashToUnitError

UINT32_MAX: Final[int] = (1 << 32) - 1
UINT64_MAX: Final[int] = (1 << 64) - 1
MAX_TICKET_LENGTH: Final[int] = 1 << 20  # 1 MiB; far larger than a ticket needs
MAX_MEASUREMENT_LENGTH: Final[int] = 1 << 20
HASH_DOMAIN: Final[bytes] = b"blind-telemetry/hash-to-unit/v1\n"
DEFAULT_HASH_COUNTER_LIMIT: Final[int] = 10_000


def length_prefix(data: bytes | bytearray | memoryview) -> bytes:
    """Return ``uint32be(len(data)) || data``."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("length-prefixed field must be bytes-like")
    raw = bytes(data)
    if len(raw) > UINT32_MAX:
        raise EncodingError(f"field of length {len(raw)} exceeds uint32 prefix")
    return len(raw).to_bytes(4, "big") + raw


def read_length_prefixed(blob: bytes, offset: int = 0) -> tuple[bytes, int]:
    """Read one length-prefixed field and return ``(value, next_offset)``."""

    if not isinstance(blob, bytes):
        raise TypeError("blob must be bytes")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise TypeError("offset must be an integer")
    if offset < 0 or offset > len(blob):
        raise EncodingError("offset is outside blob")
    if len(blob) - offset < 4:
        raise EncodingError("truncated length prefix")

    size = int.from_bytes(blob[offset : offset + 4], "big")
    start = offset + 4
    end = start + size
    if end > len(blob):
        raise EncodingError("truncated length-prefixed field")
    return blob[start:end], end


def period_to_bytes(period: int) -> bytes:
    """Canonicalize a cycle number to its ASCII decimal representation."""

    if isinstance(period, bool):
        raise TypeError("period must be an integer, not bool")
    if not isinstance(period, int):
        raise TypeError("period must be an integer cycle number")
    if not 0 <= period <= UINT64_MAX:
        raise EncodingError("period must be in [0, 2**64 - 1]")
    return str(period).encode("ascii")


def ticket_to_bytes(ticket: bytes | bytearray | memoryview | str) -> bytes:
    """Normalize a ticket represented as bytes-like data or UTF-8 text."""

    if isinstance(ticket, str):
        raw = ticket.encode("utf-8")
    elif isinstance(ticket, (bytes, bytearray, memoryview)):
        raw = bytes(ticket)
    else:
        raise TypeError("ticket must be bytes-like or str")

    if not raw:
        raise EncodingError("ticket must not be empty")
    if len(raw) > MAX_TICKET_LENGTH:
        raise EncodingError(
            f"ticket is {len(raw)} bytes; limit is {MAX_TICKET_LENGTH}"
        )
    return raw


def measurement_to_bytes(
    measurement: bytes | bytearray | memoryview | str | None,
) -> bytes:
    if measurement is None:
        return b""
    if isinstance(measurement, str):
        raw = measurement.encode("utf-8")
    elif isinstance(measurement, (bytes, bytearray, memoryview)):
        raw = bytes(measurement)
    else:
        raise TypeError("measurement must be bytes-like, str, or None")
    if len(raw) > MAX_MEASUREMENT_LENGTH:
        raise EncodingError("measurement is too large")
    return raw


def encode_credential_message(
    period: int, ticket: bytes | bytearray | memoryview | str
) -> bytes:
    """Encode ``lp(period_decimal) || lp(ticket)``."""

    period_raw = period_to_bytes(period)
    ticket_raw = ticket_to_bytes(ticket)
    return length_prefix(period_raw) + length_prefix(ticket_raw)


def decode_credential_message(blob: bytes) -> tuple[bytes, bytes]:
    """Decode and strictly consume one encoded period/ticket message."""

    period_raw, offset = read_length_prefixed(blob, 0)
    ticket_raw, offset = read_length_prefixed(blob, offset)
    if offset != len(blob):
        raise EncodingError("trailing bytes after length-prefixed fields")
    return period_raw, ticket_raw


@dataclass(frozen=True)
class HashToUnitResult:
    """A canonical invertible residue and the counter that produced it."""

    value: int
    counter: int
    encoded_message: bytes


def _digest_candidate(encoded_message: bytes, counter: int) -> int:
    digest = hashlib.sha256()
    digest.update(HASH_DOMAIN)
    digest.update(encoded_message)
    digest.update(counter.to_bytes(4, "big"))
    return int.from_bytes(digest.digest(), "big")


def hash_to_unit(
    n: int,
    period: int | None = None,
    ticket: bytes | bytearray | memoryview | str | None = None,
    *,
    encoded_message: bytes | None = None,
    max_counter: int = DEFAULT_HASH_COUNTER_LIMIT,
) -> HashToUnitResult:
    """Map an encoded credential message to a unit modulo RSA modulus ``n``.

    The counter starts at zero and increments until SHA-256 truncated/reduced
    modulo ``n`` is a non-weak canonical residue coprime to ``n``.  Both device
    and collector run this same deterministic search, so the counter does not
    need to be sent on the wire.
    """

    if isinstance(n, bool) or not isinstance(n, int) or n <= 3:
        raise ValueError("RSA modulus n must be an integer greater than 3")
    if (
        isinstance(max_counter, bool)
        or not isinstance(max_counter, int)
        or max_counter < 0
    ):
        raise ValueError("max_counter must be a nonnegative integer")

    if encoded_message is None:
        if period is None or ticket is None:
            raise TypeError(
                "provide either encoded_message or both period and ticket"
            )
        encoded_message = encode_credential_message(period, ticket)
    elif period is not None or ticket is not None:
        raise TypeError("do not mix encoded_message with period/ticket arguments")

    if not isinstance(encoded_message, bytes):
        encoded_message = bytes(encoded_message)

    for counter in range(max_counter + 1):
        candidate = _digest_candidate(encoded_message, counter) % n
        # 0 and 1 are avoided as weak/ambiguous values even though 1 is a unit.
        if 2 <= candidate < n and math.gcd(candidate, n) == 1:
            return HashToUnitResult(candidate, counter, encoded_message)

    raise HashToUnitError(
        f"no invertible hash residue found within counters 0..{max_counter}"
    )
