"""Shared fixtures for the screening tests.

All digests here are synthetic values derived from made-up passwords; nothing
is fetched from a network or read from the machine.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

MAX_SALT_ATTEMPTS = 8_000_000


def digest_of(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest().upper()


@lru_cache(maxsize=4096)
def forced_prefix_digest(prefix: str, label: str) -> str:
    """Return a 64-char digest whose first characters equal ``prefix``.

    Deterministic synthetic fixture: brute-forces a salt counter until the
    SHA-256 of ``label#counter`` starts with ``prefix``.  Keep prefixes short
    (<= 4 hex characters means <= 65 536 expected tries) so tests stay fast.
    """

    prefix = prefix.upper()
    if not prefix:
        raise ValueError("prefix must not be empty")
    for counter in range(MAX_SALT_ATTEMPTS):
        material = f"{label}#{counter}".encode("utf-8")
        candidate = hashlib.sha256(material).hexdigest().upper()
        if candidate.startswith(prefix):
            return candidate
    raise RuntimeError("could not find a digest with the requested prefix")


@lru_cache(maxsize=4096)
def make_bucket(prefix: str, count: int, label: str = "bucket") -> tuple[str, ...]:
    """Build ``count`` distinct digests that all share ``prefix``."""

    return tuple(
        forced_prefix_digest(prefix, f"{label}-{index}") for index in range(count)
    )
