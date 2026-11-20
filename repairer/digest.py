"""Canonical range summaries for the fixed 16-bit binary key space.

The key space is the set of all 16-bit keys (``0 .. 65535``).  A *range* is
identified by a binary prefix: a string of ``"0"`` / ``"1"`` characters with
length between 0 and :data:`KEY_BITS`.  Prefix ``""`` (depth 0) is the root and
covers the whole key space; a prefix of length 16 identifies exactly one key.

A summary is defined purely by the (key -> value) contents of a range, never by
how a store physically folds or materializes its tree:

* An empty leaf (the key is absent) has a fixed agreed sentinel digest
  :func:`empty_leaf_digest`.
* A present leaf hashes a domain-separated encoding of its value via
  :func:`leaf_digest`.
* An internal node hashes the fixed left/right partition: its left child is
  ``prefix + "0"`` and its right child is ``prefix + "1"``
  (:func:`node_digest`).

Two stores therefore report equal summaries for a prefix exactly when the
multiset of ``(key, value)`` pairs under that prefix is equal, regardless of
their internal folding shape.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from typing import Mapping, Optional, Tuple

#: Depth of the key space (16-bit keys).
KEY_BITS = 16
#: Number of distinct keys covered by the whole space.
KEY_COUNT = 1 << KEY_BITS

# Domain separation tags make different node kinds never collide.
_LEAF_TAG = b"leaf:v1:"
_EMPTY_TAG = b"leaf:v1:empty"
_NODE_TAG = b"node:v1:"


def _hash(data: bytes) -> bytes:
    """Return the canonical 32-byte digest of ``data`` (SHA-256)."""
    return hashlib.sha256(data).digest()


def leaf_digest(value: bytes) -> bytes:
    """Digest of a present leaf holding ``value``."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("leaf values must be bytes-like")
    return _hash(_LEAF_TAG + bytes(value))


def empty_leaf_digest() -> bytes:
    """Fixed, agreed digest of an absent key (empty leaf)."""
    return _hash(_EMPTY_TAG)


def node_digest(left: bytes, right: bytes) -> bytes:
    """Digest of an internal node from its fixed left/right child digests."""
    return _hash(_NODE_TAG + left + right)


def key_to_prefix(key: int) -> str:
    """Return the length-16 binary prefix that identifies ``key``."""
    if not isinstance(key, int) or isinstance(key, bool):
        raise TypeError("key must be an int")
    if not 0 <= key < KEY_COUNT:
        raise ValueError("key outside the 16-bit key space: %r" % (key,))
    return format(key, "0%db" % KEY_BITS)


def prefix_range(prefix: str) -> Tuple[int, int]:
    """Return the half-open key interval ``[lo, hi)`` covered by ``prefix``."""
    if not isinstance(prefix, str):
        raise TypeError("prefix must be a str of 0/1")
    depth = len(prefix)
    if depth > KEY_BITS or any(ch not in "01" for ch in prefix):
        raise ValueError("invalid binary prefix: %r" % (prefix,))
    if depth == 0:
        return 0, KEY_COUNT
    lo = int(prefix, 2) << (KEY_BITS - depth)
    hi = lo + (1 << (KEY_BITS - depth))
    return lo, hi


# Digest of an entirely empty subtree at each depth.  Indexed by depth
# (``0`` = root ... ``KEY_BITS`` = single empty leaf).
_EMPTY_SUBTREE: Tuple[bytes, ...] = (empty_leaf_digest(),) * (KEY_BITS + 1)
for _depth in range(KEY_BITS - 1, -1, -1):
    _EMPTY_SUBTREE[_depth] = node_digest(
        _EMPTY_SUBTREE[_depth + 1], _EMPTY_SUBTREE[_depth + 1]
    )


def empty_subtree_digest(depth: int) -> bytes:
    """Digest of a subtree at ``depth`` that contains no keys at all."""
    if not 0 <= depth <= KEY_BITS:
        raise ValueError("depth out of range: %r" % (depth,))
    return _EMPTY_SUBTREE[depth]


def canonical_summary(
    entries: Mapping[int, bytes],
    prefix: str,
    sorted_keys: Optional[list] = None,
) -> bytes:
    """Compute the canonical summary of ``entries`` under ``prefix``.

    ``entries`` maps integer keys to :class:`bytes` values (absent keys are
    simply missing from the mapping).  ``sorted_keys`` may be supplied to reuse
    a pre-sorted key list; otherwise the keys are sorted on each call.

    The result depends only on the contents of the range, not on any physical
    tree structure, which is what lets a fully expanded store and a folded
    store compare summaries directly.
    """
    lo, hi = prefix_range(prefix)
    depth = len(prefix)

    if sorted_keys is None:
        keys = sorted(entries)
    else:
        keys = sorted_keys

    start = bisect_left(keys, lo)
    end = bisect_left(keys, hi)
    if start == end:
        # Fast path: nothing present in the whole subtree.
        return empty_subtree_digest(depth)

    # Bottom-up recursion over the fixed binary partition.  At most KEY_BITS
    # stack frames deep (16), so a plain recursive function is safe.
    def build(cur_prefix: str, cur_lo: int, cur_depth: int) -> bytes:
        idx = bisect_left(keys, cur_lo, start, end)
        if idx == end or keys[idx] >= cur_lo + (1 << (KEY_BITS - cur_depth)):
            return empty_subtree_digest(cur_depth)
        if cur_depth == KEY_BITS:
            return leaf_digest(entries[cur_lo])
        half = 1 << (KEY_BITS - cur_depth - 1)
        left = build(cur_prefix + "0", cur_lo, cur_depth + 1)
        right = build(cur_prefix + "1", cur_lo + half, cur_depth + 1)
        return node_digest(left, right)

    return build(prefix, lo, depth)
