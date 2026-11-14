"""Identity normalization and validation.

Account names are expected to arrive already canonical (the login edge owns
that normalization step); the adjudicator rejects non-canonical spellings so
that two aliases can never split one account's quota. Source addresses are
normalized here because callers hand over raw literal addresses.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from .errors import ValidationError


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string, got {type(value).__name__}")
    if not value:
        raise ValidationError(f"{field} must be non-empty")
    if value != value.strip():
        raise ValidationError(f"{field} must not have leading/trailing whitespace: {value!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError(f"{field} must not contain control characters: {value!r}")
    return value


def canonical_account_name(value: Any) -> str:
    """Return the canonical form of an account name (trimmed + case-folded)."""

    if not isinstance(value, str):
        raise ValidationError(f"account must be a string, got {type(value).__name__}")
    canonical = value.strip().casefold()
    if not canonical:
        raise ValidationError("account must be non-empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in canonical):
        raise ValidationError("account must not contain control characters")
    return canonical


def require_canonical_account(value: Any) -> str:
    """Validate that the caller supplied an account name that is already canonical."""

    if not isinstance(value, str):
        raise ValidationError(f"account must be a string, got {type(value).__name__}")
    canonical = canonical_account_name(value)
    if value != canonical:
        raise ValidationError(
            "account must already be canonical; "
            f"use canonical_account_name() upstream ({value!r} -> {canonical!r})"
        )
    return canonical


def canonical_source_address(value: Any) -> str:
    """Normalize an IPv4/IPv6 literal to its compact textual form."""

    if not isinstance(value, str):
        raise ValidationError(f"source must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValidationError("source must be non-empty")
    if "%" in text:
        raise ValidationError("IPv6 zone identifiers are not supported")
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValidationError(f"source must be an IPv4/IPv6 literal address: {value!r}") from exc
    return address.compressed


def require_tenant(value: Any) -> str:
    return _require_text(value, "tenant")


def require_request_id(value: Any) -> str:
    return _require_text(value, "request_id")
