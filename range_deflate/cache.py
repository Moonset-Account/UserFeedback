"""Decompressed-frame cache with a budget measured in uncompressed bytes."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Optional

from .models import FrameEntry


@dataclass(frozen=True)
class CacheKey:
    """Identity of a physical frame independent of index version.

    Two index versions that point at the same compressed bytes and expect
    the same decompressed checksum describe identical frame content, so a
    validated entry may be shared across versions. Dictionary differences
    show up as a different checksum whenever they affect the output; when
    the output is genuinely identical the bytes are safe to reuse.
    """

    compressed_offset: int
    compressed_length: int
    checksum: int

    @classmethod
    def from_entry(cls, entry: FrameEntry) -> "CacheKey":
        return cls(
            compressed_offset=entry.compressed_offset,
            compressed_length=entry.compressed_length,
            checksum=entry.checksum,
        )


@dataclass(frozen=True)
class CacheStats:
    entries: int
    used_bytes: int
    budget_bytes: Optional[int]
    hits: int
    misses: int
    evictions: int
    bypassed: int


class DecompressedCache:
    """LRU cache of inflated frame payloads.

    The budget counts *decompressed* bytes only. A frame larger than the
    whole budget is still returned to the caller once, but never stored
    (a "bypass"). A budget of ``None`` means unlimited; ``0`` disables
    caching entirely.
    """

    def __init__(self, budget_bytes: Optional[int]):
        if budget_bytes is not None and budget_bytes < 0:
            raise ValueError("budget_bytes must be non-negative or None")
        self._budget = budget_bytes
        self._data: "OrderedDict[CacheKey, bytes]" = OrderedDict()
        self._used = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._bypassed = 0
        self._lock = RLock()

    @property
    def budget_bytes(self) -> Optional[int]:
        return self._budget

    def get(self, key: CacheKey) -> Optional[bytes]:
        with self._lock:
            payload = self._data.get(key)
            if payload is None:
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return payload

    def put(self, key: CacheKey, payload: bytes) -> None:
        size = len(payload)
        with self._lock:
            if self._budget == 0:
                self._bypassed += 1
                return
            if self._budget is not None and size > self._budget:
                # A single frame cannot fit at all: serve it but do not
                # evict the whole cache on its behalf.
                self._bypassed += 1
                return

            existing = self._data.pop(key, None)
            if existing is not None:
                self._used -= len(existing)

            self._data[key] = payload
            self._used += size
            if self._budget is not None:
                while self._used > self._budget and len(self._data) > 1:
                    _, dropped = self._data.popitem(last=False)
                    self._used -= len(dropped)
                    self._evictions += 1

    def contains(self, key: CacheKey) -> bool:
        with self._lock:
            return key in self._data

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                entries=len(self._data),
                used_bytes=self._used,
                budget_bytes=self._budget,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                bypassed=self._bypassed,
            )

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._used = 0
