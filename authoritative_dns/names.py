"""Domain-name parsing and canonicalisation for the local authoritative kernel.

Names are handled internally as tuples of lowercase labels in normal
(left-to-right) order.  The root is the empty tuple.  ``to_text`` renders the
canonical absolute form with a trailing dot.

Only the ASCII subset needed by local fixtures is accepted; internationalised
names and escape sequences are intentionally out of scope because the kernel
never talks to a real network.
"""

from __future__ import annotations

import re
from typing import Iterator, Tuple

Name = Tuple[str, ...]

MAX_LABEL_LENGTH = 63
MAX_NAME_LENGTH = 255

_LABEL_RE = re.compile(r"^[a-z0-9_*\-]+$")


class InvalidDNSName(ValueError):
    """Raised when a domain name cannot be parsed or normalised."""


class OutOfZoneName(ValueError):
    """Raised when a name is not equal to or below the zone origin."""


def parse_name(name: str) -> Name:
    """Parse *name* into a canonical tuple of lowercase labels.

    ``"WWW.Example."`` and ``"www.example"`` both become
    ``("www", "example")``.  The root is represented by ``()``.

    A ``*`` label is allowed (in any position) but must stand alone.
    """
    if not isinstance(name, str):
        raise TypeError(f"domain name must be str, got {type(name).__name__}")

    text = name.strip()
    if not text:
        raise InvalidDNSName("domain name must not be empty")
    if text == ".":
        return ()
    if text.endswith("."):
        text = text[:-1]

    parsed: list[str] = []
    for label in text.split("."):
        if not label:
            raise InvalidDNSName(f"empty label in {name!r}")
        lowered = label.lower()
        if len(lowered) > MAX_LABEL_LENGTH:
            raise InvalidDNSName(f"label longer than 63 octets in {name!r}")
        if not _LABEL_RE.fullmatch(lowered):
            raise InvalidDNSName(f"invalid character in label {label!r}")
        if "*" in lowered and lowered != "*":
            raise InvalidDNSName("'*' must occupy a label by itself")
        parsed.append(lowered)

    wire_length = sum(len(label) + 1 for label in parsed) + 1
    if wire_length > MAX_NAME_LENGTH:
        raise InvalidDNSName(f"name longer than 255 octets: {name!r}")
    return tuple(parsed)


def to_text(name: Name) -> str:
    """Render *name* as a canonical absolute name with a trailing dot."""
    if not name:
        return "."
    return ".".join(name) + "."


def is_subdomain(name: Name, parent: Name) -> bool:
    """Return ``True`` when *name* equals or sits below *parent*."""
    if len(name) < len(parent):
        return False
    if not parent:
        return True
    return name[-len(parent) :] == parent


def proper_ancestors(name: Name) -> Iterator[Name]:
    """Yield the ancestors of *name* from closest to furthest (root last).

    Labels are stored in normal, left-to-right order, so dropping the first
    label moves one level up: ``a.b.example`` -> ``b.example`` ->
    ``example`` -> the root.
    """
    for index in range(1, len(name) + 1):
        yield name[index:]


def wildcard_child(name: Name) -> Name:
    """Return the single-label wildcard node directly below *name*."""
    return ("*",) + name
