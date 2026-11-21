"""An order-statistic multiset backed by a randomized treap.

The structure supports, per value:

* ``add(value)``    -- O(log n) expected
* ``discard(value)``-- O(log n) expected
* ``kth(k)``        -- O(log n) expected (1-based rank)
* ``median()``      -- O(log n) expected

Values may repeat; a multiplicity counter is kept on each node, so equal
values never create extra nodes.  No operation ever re-sorts or rebuilds the
whole window: a single insertion/removal touches one root-to-leaf path and
rotates at most O(log n) nodes (O(1) expected for random priorities).

The counters ``insertions``, ``deletions``, ``rotations``, ``merge_steps``,
``probes`` and ``kth_queries`` let a caller observe structural churn.
"""

from __future__ import annotations

import random


class _Node:
    __slots__ = ("key", "priority", "count", "size", "left", "right")

    def __init__(self, key, priority: float) -> None:
        self.key = key
        self.priority = priority
        self.count = 1
        self.size = 1
        self.left: _Node | None = None
        self.right: _Node | None = None


def _size(node: _Node | None) -> int:
    return node.size if node is not None else 0


class OrderedMultiset:
    """A multiset with logarithmic insertion, removal and order statistics."""

    def __init__(self, seed: int = 0) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an int")
        self._root: _Node | None = None
        self._rng = random.Random(seed)
        # Observable structural counters.
        self.insertions = 0
        self.deletions = 0
        self.rotations = 0
        self.merge_steps = 0
        self.kth_queries = 0
        self.probes = 0  # nodes visited while descending/merging

    # ------------------------------------------------------------------ size
    @property
    def size(self) -> int:
        """Total multiplicity currently stored (0 for an empty structure)."""
        return _size(self._root)

    def __len__(self) -> int:
        return self.size

    def __bool__(self) -> bool:
        return self._root is not None

    # --------------------------------------------------------------- writing
    def add(self, key) -> None:
        """Insert one occurrence of ``key``."""
        self.insertions += 1
        self._root = self._add(self._root, key, self._rng.random())

    def discard(self, key) -> bool:
        """Remove one occurrence of ``key``; return ``False`` if absent."""
        root, removed = self._discard(self._root, key)
        self._root = root
        if removed:
            self.deletions += 1
        return removed

    def clear(self) -> None:
        """Drop all keys while *keeping* the cumulative churn counters."""
        self._root = None

    def _add(self, node: _Node | None, key, priority: float) -> _Node:
        if node is None:
            return _Node(key, priority)
        self.probes += 1
        if key == node.key:
            node.count += 1
            node.size += 1
            return node
        if key < node.key:
            node.left = self._add(node.left, key, priority)
            if node.left.priority < node.priority:
                node = self._rotate_right(node)
        else:
            node.right = self._add(node.right, key, priority)
            if node.right.priority < node.priority:
                node = self._rotate_left(node)
        node.size = _size(node.left) + _size(node.right) + node.count
        return node

    def _discard(self, node: _Node | None, key) -> tuple[_Node | None, bool]:
        if node is None:
            return None, False
        self.probes += 1
        if key < node.key:
            node.left, removed = self._discard(node.left, key)
        elif key > node.key:
            node.right, removed = self._discard(node.right, key)
        else:
            if node.count > 1:
                node.count -= 1
                node.size -= 1
                return node, True
            return self._merge(node.left, node.right), True
        if removed:
            node.size = _size(node.left) + _size(node.right) + node.count
        return node, removed

    def _merge(self, left: _Node | None, right: _Node | None) -> _Node | None:
        """Merge two treaps where every key in ``left`` is < every key in ``right``."""
        if left is None:
            return right
        if right is None:
            return left
        self.merge_steps += 1
        self.probes += 1
        if left.priority < right.priority:
            left.right = self._merge(left.right, right)
            left.size = _size(left.left) + _size(left.right) + left.count
            return left
        right.left = self._merge(left, right.left)
        right.size = _size(right.left) + _size(right.right) + right.count
        return right

    def _rotate_right(self, node: _Node) -> _Node:
        self.rotations += 1
        child = node.left
        node.left = child.right
        child.right = node
        node.size = _size(node.left) + _size(node.right) + node.count
        child.size = _size(child.left) + _size(child.right) + child.count
        return child

    def _rotate_left(self, node: _Node) -> _Node:
        self.rotations += 1
        child = node.right
        node.right = child.left
        child.left = node
        node.size = _size(node.left) + _size(node.right) + node.count
        child.size = _size(child.left) + _size(child.right) + child.count
        return child

    # --------------------------------------------------------------- reading
    def kth(self, k: int):
        """Return the ``k``-th smallest value (1-based)."""
        if k < 1 or k > self.size:
            raise IndexError(f"rank {k} out of range for size {self.size}")
        self.kth_queries += 1
        node = self._root
        while node is not None:
            self.probes += 1
            left_size = _size(node.left)
            if k <= left_size:
                node = node.left
            elif k <= left_size + node.count:
                return node.key
            else:
                k -= left_size + node.count
                node = node.right
        raise IndexError(f"rank {k} out of range")  # pragma: no cover

    def median(self):
        """Median of the multiset: average of the two middle values when even.

        Returns ``None`` for an empty multiset.
        """
        n = self.size
        if n == 0:
            return None
        if n % 2 == 1:
            return self.kth(n // 2 + 1)
        lower = self.kth(n // 2)
        upper = self.kth(n // 2 + 1)
        return (lower + upper) / 2
