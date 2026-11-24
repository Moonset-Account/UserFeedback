"""Persistent augmented treap for compressed key-group prefix totals.

Only nodes on update/query paths are copied or read; unaffected
:class:`~rank_index.groups.KeyGroup` objects and treap nodes are shared
between immutable snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .groups import KeyGroup


@dataclass(frozen=True, slots=True)
class Node:
    key: tuple[Any, Any, Any]
    group: KeyGroup
    priority: int
    left: "Node | None"
    right: "Node | None"
    subtotal: int


def total(node: Node | None) -> int:
    return node.subtotal if node is not None else 0


def _make(
    key: tuple[Any, Any, Any],
    group: KeyGroup,
    priority: int,
    left: Node | None,
    right: Node | None,
) -> Node:
    return Node(
        key=key,
        group=group,
        priority=priority,
        left=left,
        right=right,
        subtotal=group.total + total(left) + total(right),
    )


def _priority(key: tuple[Any, Any, Any]) -> int:
    # Built-in hash is stable for the integer/string synthetic keys used here
    # within one interpreter and gives a well-distributed treap.
    return hash(("rank-index-treap", key)) & 0x7FFFFFFF_FFFFFFFF


def _rotate_right(y: Node) -> Node:
    x = y.left
    assert x is not None
    new_y = _make(y.key, y.group, y.priority, x.right, y.right)
    return _make(x.key, x.group, x.priority, x.left, new_y)


def _rotate_left(y: Node) -> Node:
    x = y.right
    assert x is not None
    new_y = _make(y.key, y.group, y.priority, y.left, x.left)
    return _make(x.key, x.group, x.priority, new_y, x.right)


def upsert(root: Node | None, group: KeyGroup) -> Node:
    key = group.key
    if root is None:
        return _make(key, group, _priority(key), None, None)

    if key == root.key:
        return _make(key, group, root.priority, root.left, root.right)

    if key < root.key:
        new_left = upsert(root.left, group)
        copied = _make(root.key, root.group, root.priority, new_left, root.right)
        if new_left.priority > copied.priority:
            return _rotate_right(copied)
        return copied

    new_right = upsert(root.right, group)
    copied = _make(root.key, root.group, root.priority, root.left, new_right)
    if new_right.priority > copied.priority:
        return _rotate_left(copied)
    return copied


def _merge(left: Node | None, right: Node | None) -> Node | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.priority > right.priority:
        merged_right = _merge(left.right, right)
        return _make(left.key, left.group, left.priority, left.left, merged_right)
    merged_left = _merge(left, right.left)
    return _make(right.key, right.group, right.priority, merged_left, right.right)


def erase(root: Node | None, key: tuple[Any, Any, Any]) -> Node | None:
    if root is None:
        return None
    if key < root.key:
        new_left = erase(root.left, key)
        return _make(root.key, root.group, root.priority, new_left, root.right)
    if key > root.key:
        new_right = erase(root.right, key)
        return _make(root.key, root.group, root.priority, root.left, new_right)
    return _merge(root.left, root.right)


def get(root: Node | None, key: tuple[Any, Any, Any]) -> KeyGroup | None:
    while root is not None:
        if key == root.key:
            return root.group
        root = root.left if key < root.key else root.right
    return None


def rank_before(root: Node | None, key: tuple[Any, Any, Any]) -> int:
    result = 0
    while root is not None:
        if key <= root.key:
            root = root.left
        else:
            result += total(root.left) + root.group.total
            root = root.right
    return result


def find_by_rank(root: Node | None, rank: int) -> tuple[KeyGroup, int, int]:
    """Return ``(group, local_rank, global_group_start)``."""
    base = 0
    current = root
    while current is not None:
        left_total = total(current.left)
        start = base + left_total
        group_total = current.group.total
        if rank < start:
            current = current.left
            continue
        if rank < start + group_total:
            return current.group, rank - start, start
        base = start + group_total
        current = current.right
    raise IndexError("rank outside treap total")


def iter_keys(root: Node | None) -> Iterator[tuple[Any, Any, Any]]:
    if root is None:
        return
    yield from iter_keys(root.left)
    yield root.key
    yield from iter_keys(root.right)


def collect_groups(root: Node | None) -> dict[tuple[Any, Any, Any], KeyGroup]:
    return {key: group for key, group in _iter_groups(root)}


def _iter_groups(root: Node | None):
    if root is None:
        return
    yield from _iter_groups(root.left)
    yield root.key, root.group
    yield from _iter_groups(root.right)


def validate(root: Node | None) -> None:
    """Test/debug helper: verify BST, heap, and augmented totals."""
    if root is None:
        return

    def check(node: Node | None, low, high) -> int:
        if node is None:
            return 0
        if low is not None and node.key <= low:
            raise AssertionError("BST lower bound violated")
        if high is not None and node.key >= high:
            raise AssertionError("BST upper bound violated")
        if node.left is not None:
            if node.left.priority > node.priority:
                raise AssertionError("left heap priority violated")
            check(node.left, low, node.key)
        if node.right is not None:
            if node.right.priority > node.priority:
                raise AssertionError("right heap priority violated")
            check(node.right, node.key, high)
        expected = node.group.total + total(node.left) + total(node.right)
        if node.subtotal != expected:
            raise AssertionError("augmented subtotal is incorrect")
        return node.subtotal

    check(root, None, None)
