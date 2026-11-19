"""Immutable, versioned digest index with uniform k-anonymity pagination.

Every lookup against one version returns exactly ``page_size`` entries and the
same ``total_pages`` count, whether or not the prefix has any records.  Pages
are filled with explicitly typed ``record`` entries (real suffixes) and
``padding`` entries (synthetic suffixes that can never be reported as a hit).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .digest import (
    DIGEST_LENGTH,
    normalize_digest_list,
    validate_page_size,
    validate_prefix,
    validate_prefix_length,
)
from .errors import DigestError, InvalidPrefix, PageOutOfRange, ScreeningError

RECORD = "record"
PADDING = "padding"
ENTRY_KINDS = frozenset({RECORD, PADDING})


@dataclass(frozen=True)
class PageEntry:
    """One item in a response page.

    ``kind`` is part of the wire contract: only ``record`` entries may be
    compared by the client.  A ``padding`` entry that happens to share its
    suffix with the queried secret is still not a match.
    """

    kind: str
    suffix: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "suffix": self.suffix}


def _suffix_space(suffix_length: int) -> int:
    return 16**suffix_length


def _padding_suffix(
    version_id: str, prefix: str, page: int, slot: int, suffix_length: int, used: set[str]
) -> str:
    """Derive a deterministic suffix that is not a real record for ``prefix``."""

    counter = 0
    while True:
        material = f"padding|{version_id}|{prefix}|{page}|{slot}|{counter}".encode("utf-8")
        candidate = hashlib.sha256(material).hexdigest().upper()[:suffix_length]
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1
        if counter > _suffix_space(suffix_length) * 2:
            raise ScreeningError("unable to derive non-colliding padding suffix")


class DigestVersion:
    """One immutable snapshot of the screening list."""

    __slots__ = (
        "version_id",
        "prefix_length",
        "page_size",
        "digest_count",
        "_buckets",
        "total_pages",
        "_capacity",
        "_suffix_length",
    )

    def __init__(
        self,
        version_id: str,
        prefix_length: int,
        page_size: int,
        digests: Iterable[str],
        *,
        _normalized: bool = False,
    ) -> None:
        if not isinstance(version_id, str) or not version_id:
            raise DigestError("version_id must be a non-empty string")
        validate_prefix_length(prefix_length)
        validate_page_size(page_size)

        normalized = tuple(digests) if _normalized else normalize_digest_list(digests)
        buckets: dict[str, list[str]] = {}
        for digest in normalized:
            buckets.setdefault(digest[:prefix_length], []).append(digest[prefix_length:])

        self.version_id = version_id
        self.prefix_length = prefix_length
        self.page_size = page_size
        self.digest_count = len(normalized)
        self._buckets = MappingProxyType(
            {key: tuple(sorted(values)) for key, values in buckets.items()}
        )

        self._suffix_length = DIGEST_LENGTH - prefix_length
        self._capacity = _suffix_space(self._suffix_length)
        if page_size > self._capacity:
            raise DigestError(
                f"page_size {page_size} exceeds the {self._capacity} suffixes available "
                f"for a {self._suffix_length}-character suffix"
            )
        if self.digest_count > self._capacity:
            raise DigestError(
                f"{self.digest_count} digests cannot fit a {prefix_length}-character prefix"
            )

        largest = max((len(values) for values in self._buckets.values()), default=0)
        # Uniform page count: derived from the fullest bucket only, never from
        # the bucket being queried.  An empty list still serves one page.
        self.total_pages = max(1, math.ceil(largest / page_size)) if largest else 1

        # Verify every last page can be padded without colliding with real
        # suffixes for the same prefix, and without exhausting the suffix space.
        for key, values in self._buckets.items():
            remainder = len(values) % page_size
            padding_needed = (page_size - remainder) % page_size
            if padding_needed and self._capacity - len(values) < padding_needed:
                raise DigestError(
                    f"prefix {key!r}: not enough suffix space to pad {padding_needed} entries"
                )

    # -- inspection ---------------------------------------------------------
    @property
    def buckets(self) -> Mapping[str, tuple[str, ...]]:
        return self._buckets

    def bucket(self, prefix: str) -> tuple[str, ...]:
        prefix = validate_prefix(prefix, self.prefix_length)
        return self._buckets.get(prefix, ())

    def info(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "prefix_length": self.prefix_length,
            "page_size": self.page_size,
            "total_pages": self.total_pages,
            "digest_count": self.digest_count,
        }

    def has_digest(self, digest: str) -> bool:
        if len(digest) != DIGEST_LENGTH:
            return False
        prefix, suffix = digest[: self.prefix_length], digest[self.prefix_length :]
        return suffix in self._buckets.get(prefix, ())

    # -- pagination ---------------------------------------------------------
    def page(self, prefix: str, page: int) -> tuple[PageEntry, ...]:
        """Return exactly ``page_size`` typed entries for ``prefix``/``page``."""

        prefix = validate_prefix(prefix, self.prefix_length)
        if isinstance(page, bool) or not isinstance(page, int):
            raise InvalidPrefix("page must be an integer")
        if not 0 <= page < self.total_pages:
            raise PageOutOfRange(page, self.total_pages)

        records = self._buckets.get(prefix, ())
        start = page * self.page_size
        visible = records[start : start + self.page_size]
        entries = [PageEntry(RECORD, suffix) for suffix in visible]

        padding_needed = self.page_size - len(entries)
        if padding_needed:
            used = set(records)
            for slot in range(padding_needed):
                suffix = _padding_suffix(
                    self.version_id, prefix, page, slot, self._suffix_length, used
                )
                entries.append(PageEntry(PADDING, suffix))
        return tuple(entries)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"DigestVersion(version_id={self.version_id!r}, "
            f"prefix_length={self.prefix_length}, page_size={self.page_size}, "
            f"digests={self.digest_count}, total_pages={self.total_pages})"
        )


def build_version(
    digests: Iterable[str],
    *,
    prefix_length: int,
    page_size: int,
    version_id: str | None = None,
) -> DigestVersion:
    """Build an immutable version; the id defaults to a content hash."""

    normalized = normalize_digest_list(digests)
    if version_id is None:
        material = (
            f"{prefix_length}|{page_size}|" + "|".join(normalized)
        ).encode("utf-8")
        version_id = hashlib.sha256(material).hexdigest()[:16]
    return DigestVersion(version_id, prefix_length, page_size, normalized, _normalized=True)
