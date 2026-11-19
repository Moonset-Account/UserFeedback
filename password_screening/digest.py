"""Fixed-length digest handling.

Digests are SHA-256 values encoded as 64 uppercase hexadecimal characters.
Only the prefix leaves the client; the suffix never leaves the client during a
lookup.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from .errors import DigestError, InvalidPrefix

DIGEST_LENGTH = 64
_HEX_RE = re.compile(r"^[0-9A-F]+$")
_DIGEST_RE = re.compile(r"^[0-9A-F]{64}$")


def normalize_digest(value: str) -> str:
    """Validate one digest and return its canonical uppercase form."""

    if not isinstance(value, str):
        raise DigestError(f"digest must be a string, got {type(value).__name__}")
    digest = value.strip().upper()
    if len(digest) != DIGEST_LENGTH:
        raise DigestError(
            f"digest must be exactly {DIGEST_LENGTH} hex characters, got {len(digest)}"
        )
    if not _DIGEST_RE.fullmatch(digest):
        raise DigestError("digest must contain only hexadecimal characters")
    return digest


def normalize_digest_list(values: Iterable[str]) -> tuple[str, ...]:
    """Validate, de-duplicate and deterministically sort a digest list."""

    if isinstance(values, (str, bytes, bytearray)):
        raise DigestError("digest list must be an iterable of digests, not a string")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise DigestError("digest list must be iterable") from exc

    unique: set[str] = set()
    for index, item in enumerate(iterator):
        try:
            unique.add(normalize_digest(item))
        except DigestError as exc:
            raise DigestError(f"digest at index {index}: {exc}") from exc
    return tuple(sorted(unique))


def validate_prefix_length(prefix_length: int) -> int:
    """Validate the number of digest characters sent to the service."""

    if isinstance(prefix_length, bool) or not isinstance(prefix_length, int):
        raise DigestError("prefix_length must be an integer")
    if not 1 <= prefix_length < DIGEST_LENGTH:
        raise DigestError(
            f"prefix_length must be between 1 and {DIGEST_LENGTH - 1}, got {prefix_length}"
        )
    return prefix_length


def validate_page_size(page_size: int) -> int:
    """Validate the fixed number of entries returned per page."""

    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise DigestError("page_size must be an integer")
    if not 1 <= page_size <= 1000:
        raise DigestError(f"page_size must be between 1 and 1000, got {page_size}")
    return page_size


def split_digest(digest: str, prefix_length: int) -> tuple[str, str]:
    """Return ``(prefix, suffix)`` for a validated digest."""

    normalized = normalize_digest(digest)
    validate_prefix_length(prefix_length)
    return normalized[:prefix_length], normalized[prefix_length:]


def password_digest(password: str) -> str:
    """Derive the screening digest for a password (SHA-256, uppercase hex)."""

    if not isinstance(password, str):
        raise DigestError("password must be a string")
    if password == "":
        raise DigestError("password must not be empty")
    return hashlib.sha256(password.encode("utf-8")).hexdigest().upper()


def validate_prefix(prefix: str, prefix_length: int) -> str:
    """Validate a client-supplied prefix and return its canonical form."""

    if not isinstance(prefix, str):
        raise InvalidPrefix("prefix must be a string")
    normalized = prefix.upper()
    if not _HEX_RE.fullmatch(normalized):
        raise InvalidPrefix("prefix must contain only hexadecimal characters")
    if len(normalized) != prefix_length:
        raise InvalidPrefix(
            f"prefix must be exactly {prefix_length} characters, got {len(normalized)}"
        )
    return normalized
