"""In-memory stores exposing range summaries over binary prefixes.

Two physical shapes with *identical* observable summary semantics are
provided, mirroring the source/target pair described in the task:

* :meth:`MemoryStore.fine` materializes every node along every populated
  path (a fully expanded tree).
* :meth:`MemoryStore.folded` materializes only the root plus the boundary
  nodes of collapsed subtrees; any requested prefix that is not materialized
  is answered by recomputing the canonical summary from the stored entries.
  A collapsed subtree therefore appears as a single folded node while the
  summary returned for any prefix inside it still equals the fine tree's
  summary.

Stores hand out immutable :class:`StoreSnapshot` objects.  The repairer takes
its starting snapshot once from each side and performs every read against
those fixed snapshots; later writes to the target store never alter a
snapshot already handed out (copy-on-write), so the computed change set is
always based on the agreed starting state.

Read counters on each snapshot record exactly which prefix summaries and
which single-key bodies were probed, which is what makes the read-amplification
guarantees testable.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Set, Tuple

from .digest import (
    KEY_BITS,
    canonical_summary,
    key_to_prefix,
)

_PREFIX_RE = re.compile(r"^[01]{0,%d}$" % KEY_BITS)


def _validate_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not _PREFIX_RE.fullmatch(prefix):
        raise ValueError("invalid binary prefix: %r" % (prefix,))


def _validate_entries(entries: Mapping[int, bytes]) -> Dict[int, bytes]:
    normalized: Dict[int, bytes] = {}
    for key, value in entries.items():
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError("keys must be ints")
        if not 0 <= key < (1 << KEY_BITS):
            raise ValueError("key outside 16-bit space: %r" % (key,))
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("values must be bytes-like")
        normalized[key] = bytes(value)
    return normalized


class SnapshotReader(ABC):
    """Read side of an immutable store snapshot."""

    @abstractmethod
    def summary(self, prefix: str) -> bytes:
        """Return the canonical range summary for ``prefix``.

        This is the only call needed to compare whole subtrees; it never
        reads object bodies.
        """

    @abstractmethod
    def get(self, key: int) -> Optional[bytes]:
        """Return the value of a single key, or ``None`` if absent.

        Only legal at single-key granularity from the repairer's point of
        view: it is the one and only body-reading operation.
        """

    @property
    @abstractmethod
    def summary_prefixes(self) -> Tuple[str, ...]:
        """Prefixes whose summary was probed, in probe order."""

    @property
    @abstractmethod
    def body_reads(self) -> Tuple[int, ...]:
        """Single keys whose object body was read, in read order."""


class Store(ABC):
    """Abstract store: snapshot source plus a mutating write side."""

    @abstractmethod
    def snapshot(self) -> "StoreSnapshot":
        """Return an immutable snapshot of the current contents."""

    @abstractmethod
    def put(self, key: int, value: bytes) -> None:
        """Create or overwrite ``key`` (used by the repairer on targets)."""

    @abstractmethod
    def delete(self, key: int) -> None:
        """Delete ``key`` if present (used by the repairer on targets)."""


class StoreSnapshot(SnapshotReader):
    """Immutable snapshot used by the repairer.

    A snapshot owns an immutable copy of the entries and an immutable set of
    *materialized* prefix nodes.  A summary probe for a materialized prefix is
    served directly from the node table; any other prefix is answered with a
    canonical recomputation over the entries.  Both paths return the same
    digest, so the folded shape is observable only through the counters and
    the materialization table, never through summary values.
    """

    def __init__(
        self,
        entries: Mapping[int, bytes],
        materialized: Iterable[str],
        label: str = "snapshot",
    ) -> None:
        self._entries: Dict[int, bytes] = dict(entries)
        self._sorted_keys = sorted(self._entries)
        self._materialized: FrozenSet[str] = frozenset(materialized)
        self._nodes: Dict[str, bytes] = {
            prefix: canonical_summary(self._entries, prefix, self._sorted_keys)
            for prefix in self._materialized
        }
        self._label = label
        self._summary_probes: list = []
        self._body_reads: list = []

    # -- observed state -----------------------------------------------------

    @property
    def entries(self) -> Mapping[int, bytes]:
        return self._entries

    @property
    def materialized_prefixes(self) -> FrozenSet[str]:
        return self._materialized

    # -- read interface -----------------------------------------------------

    def summary(self, prefix: str) -> bytes:
        _validate_prefix(prefix)
        self._summary_probes.append(prefix)
        node = self._nodes.get(prefix)
        if node is not None:
            return node
        # Folded/non-materialized region: shape-independent recomputation.
        return canonical_summary(self._entries, prefix, self._sorted_keys)

    def get(self, key: int) -> Optional[bytes]:
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError("key must be an int")
        if not 0 <= key < (1 << KEY_BITS):
            raise ValueError("key outside 16-bit space: %r" % (key,))
        self._body_reads.append(key)
        return self._entries.get(key)

    # -- probe accounting ---------------------------------------------------

    @property
    def summary_prefixes(self) -> Tuple[str, ...]:
        return tuple(self._summary_probes)

    @property
    def body_reads(self) -> Tuple[int, ...]:
        return tuple(self._body_reads)

    @property
    def summary_read_count(self) -> int:
        return len(self._summary_probes)

    @property
    def body_read_count(self) -> int:
        return len(self._body_reads)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            "StoreSnapshot(label=%r, keys=%d, materialized=%d, "
            "summary_probes=%d, body_reads=%d)"
            % (
                self._label,
                len(self._entries),
                len(self._materialized),
                len(self._summary_probes),
                len(self._body_reads),
            )
        )


def _fine_prefixes(entries: Mapping[int, bytes]) -> Set[str]:
    """All prefixes on paths leading to present keys (fully expanded tree)."""
    prefixes: Set[str] = {""}
    for key in entries:
        path = key_to_prefix(key)
        for depth in range(1, KEY_BITS + 1):
            prefixes.add(path[:depth])
    return prefixes


def _folded_prefixes(entries: Mapping[int, bytes], fold_depth: int) -> Set[str]:
    """Root plus, below ``fold_depth``, only boundary nodes of collapsed runs.

    Everything at depth <= ``fold_depth`` is materialized; deeper subtrees are
    kept folded (their boundary node at exactly ``fold_depth`` is the last
    materialized one).  Single-key leaves at depth 16 are never materialized
    in this shape, so probing them forces a canonical recomputation -- the
    folded counterpart of the fine tree's leaf node.
    """
    if not 0 <= fold_depth <= KEY_BITS:
        raise ValueError("fold_depth out of range: %r" % (fold_depth,))
    prefixes: Set[str] = set()
    for key in entries:
        path = key_to_prefix(key)
        for depth in range(0, min(fold_depth, KEY_BITS) + 1):
            prefixes.add(path[:depth])
    prefixes.add("")
    return prefixes


class MemoryStore(Store):
    """Mutable in-memory store with selectable materialization shape."""

    def __init__(
        self,
        entries: Optional[Mapping[int, bytes]] = None,
        materialized: Optional[Iterable[str]] = None,
        label: str = "store",
    ) -> None:
        self._entries: Dict[int, bytes] = _validate_entries(entries or {})
        if materialized is None:
            self._materialized: FrozenSet[str] = frozenset(_fine_prefixes(self._entries))
        else:
            mat = set(materialized)
            mat.add("")
            self._materialized = frozenset(mat)
        self._label = label

    # -- constructors for the two physical shapes ---------------------------

    @classmethod
    def fine(cls, entries: Optional[Mapping[int, bytes]] = None, label: str = "fine") -> "MemoryStore":
        """Fully expanded store: every node on every populated path."""
        data = _validate_entries(entries or {})
        return cls(data, _fine_prefixes(data), label=label)

    @classmethod
    def folded(
        cls,
        entries: Optional[Mapping[int, bytes]] = None,
        fold_depth: int = 4,
        label: str = "folded",
    ) -> "MemoryStore":
        """Collapsed store: root plus fold-boundary nodes only."""
        data = _validate_entries(entries or {})
        return cls(data, _folded_prefixes(data, fold_depth), label=label)

    # -- snapshots ----------------------------------------------------------

    def snapshot(self) -> StoreSnapshot:
        # Defensive copies keep the snapshot immutable across later writes.
        return StoreSnapshot(
            dict(self._entries),
            set(self._materialized),
            label=self._label,
        )

    # -- mutation side (target only) ----------------------------------------

    def put(self, key: int, value: bytes) -> None:
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError("key must be an int")
        if not 0 <= key < (1 << KEY_BITS):
            raise ValueError("key outside 16-bit space: %r" % (key,))
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("values must be bytes-like")
        self._entries[key] = bytes(value)

    def delete(self, key: int) -> None:
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError("key must be an int")
        if not 0 <= key < (1 << KEY_BITS):
            raise ValueError("key outside 16-bit space: %r" % (key,))
        self._entries.pop(key, None)

    # -- inspection ---------------------------------------------------------

    @property
    def entries(self) -> Mapping[int, bytes]:
        return self._entries

    @property
    def materialized_prefixes(self) -> FrozenSet[str]:
        return self._materialized

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MemoryStore(label=%r, keys=%d, materialized=%d)" % (
            self._label,
            len(self._entries),
            len(self._materialized),
        )
