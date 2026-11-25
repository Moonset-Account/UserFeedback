"""Ordered leaves, static B-tree construction, and leaf lookup."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .encoder import Separator, choose_separator
from .model import (
    KeyOrderError,
    KeySchema,
    canonicalize_key,
    coerce_schema,
    compare_keys,
    validate_sorted_keys,
)


@dataclass
class Leaf:
    """An ordered leaf page.  ``keys`` must already follow the composite order."""

    keys: list[tuple[Any, ...]] | Sequence[Sequence[Any]]
    name: str | None = None
    values: list[Any] | None = None

    def __post_init__(self) -> None:
        self.keys = [tuple(key) for key in self.keys]

    @property
    def minimum(self) -> tuple[Any, ...]:
        if not self.keys:
            raise KeyOrderError("leaf is empty; cannot derive minimum key")
        return self.keys[0]

    @property
    def maximum(self) -> tuple[Any, ...]:
        if not self.keys:
            raise KeyOrderError("leaf is empty; cannot derive maximum key")
        return self.keys[-1]


@dataclass
class LookupResult:
    leaf_index: int
    leaf: Leaf
    found: bool
    matching_key: tuple[Any, ...] | None = None
    tree_path: list["Node"] = field(default_factory=list)
    routing_decisions: list[int] = field(default_factory=list)

    @property
    def leaf_name(self) -> str | None:
        return self.leaf.name


@dataclass
class Node:
    is_leaf: bool
    children: list["Node"] = field(default_factory=list)
    separators: list[Separator] = field(default_factory=list)
    leaf: Leaf | None = None
    leaf_index: int | None = None
    first_key: tuple[Any, ...] | None = None
    last_key: tuple[Any, ...] | None = None

    @property
    def level(self) -> int:
        if self.is_leaf:
            return 0
        return 1 + max(child.level for child in self.children)


class BTree:
    """A small static B-tree/B+ tree with shortest encoded separators."""

    def __init__(
        self,
        leaves: Sequence[Leaf],
        schema: KeySchema | Sequence[Any],
        *,
        fanout: int = 4,
        validate: bool = True,
        check_rowid_unique: bool = True,
    ) -> None:
        if fanout < 2:
            raise ValueError("fanout must be at least 2")
        self.schema = coerce_schema(schema)
        self.leaves = list(leaves)
        self.fanout = fanout
        if not self.leaves:
            raise ValueError("at least one leaf is required")

        if validate:
            self.validate(check_rowid_unique=check_rowid_unique)

        # Adjacent-leaf separators are useful independently of the fanout tree.
        self.adjacent_separators: list[Separator] = [
            choose_separator(left.maximum, right.minimum, self.schema)
            for left, right in zip(self.leaves, self.leaves[1:])
        ]
        self.root = self._build()

    def validate(self, *, check_rowid_unique: bool = True) -> None:
        seen_rowids: set[Any] = set()
        for leaf_index, leaf in enumerate(self.leaves):
            if not leaf.keys:
                raise KeyOrderError(f"leaf {leaf_index} is empty")
            for key in leaf.keys:
                if len(key) != self.schema.size:
                    raise KeyOrderError(
                        f"leaf {leaf_index} contains key {key!r} with wrong arity"
                    )
            validate_sorted_keys(leaf.keys, self.schema, strict=True)

            if check_rowid_unique:
                for key in leaf.keys:
                    rowid = key[-1]
                    if rowid in seen_rowids:
                        raise KeyOrderError(f"duplicate rowid encountered: {rowid!r}")
                    seen_rowids.add(rowid)

        for index, (left, right) in enumerate(zip(self.leaves, self.leaves[1:])):
            if compare_keys(left.maximum, right.minimum, self.schema) >= 0:
                raise KeyOrderError(
                    f"adjacent leaves {index} and {index + 1} are not ordered"
                )

    def _make_leaf_node(self, leaf: Leaf, index: int) -> Node:
        return Node(
            is_leaf=True,
            leaf=leaf,
            leaf_index=index,
            first_key=leaf.minimum,
            last_key=leaf.maximum,
        )

    def _build(self) -> Node:
        nodes = [self._make_leaf_node(leaf, i) for i, leaf in enumerate(self.leaves)]
        if len(nodes) == 1:
            return nodes[0]

        while len(nodes) > 1:
            parents: list[Node] = []
            for start in range(0, len(nodes), self.fanout):
                group = nodes[start : start + self.fanout]
                separators = [
                    choose_separator(
                        group[i].last_key,
                        group[i + 1].first_key,
                        self.schema,
                    )
                    for i in range(len(group) - 1)
                ]
                parents.append(
                    Node(
                        is_leaf=False,
                        children=group,
                        separators=separators,
                        first_key=group[0].first_key,
                        last_key=group[-1].last_key,
                    )
                )
            nodes = parents
        return nodes[0]

    def _route_index(self, node: Node, key: Sequence[Any]) -> int:
        """Return child index using Lmax < separator <= Rmin invariant."""
        for index, separator in enumerate(node.separators):
            if compare_keys(key, separator.comparison_values(), self.schema) < 0:
                return index
        return len(node.separators)

    def lookup_leaf(self, key: Sequence[Any] | Separator) -> LookupResult:
        """Land on the leaf that should contain ``key``.

        A search key below every stored key still lands on leaf zero, and one
        above every key lands on the last leaf.  ``found`` reports an exact
        full-key match.
        """
        search_key = (
            key.comparison_values() if isinstance(key, Separator) else tuple(key)
        )
        search_key = canonicalize_key(search_key, self.schema)

        node = self.root
        path: list[Node] = []
        decisions: list[int] = []
        while not node.is_leaf:
            path.append(node)
            child_index = self._route_index(node, search_key)
            decisions.append(child_index)
            node = node.children[child_index]

        assert node.leaf is not None and node.leaf_index is not None
        matching_key: tuple[Any, ...] | None = None
        for stored in node.leaf.keys:
            if compare_keys(search_key, stored, self.schema) == 0:
                matching_key = stored
                break

        return LookupResult(
            leaf_index=node.leaf_index,
            leaf=node.leaf,
            found=matching_key is not None,
            matching_key=matching_key,
            tree_path=path,
            routing_decisions=decisions,
        )

    # Convenience aliases.
    find_leaf = lookup_leaf

    def find(self, key: Sequence[Any]) -> LookupResult:
        return self.lookup_leaf(key)


class BTreeFinder:
    """Small facade for callers that separate building and finding."""

    def __init__(
        self,
        leaves: Sequence[Leaf],
        schema: KeySchema | Sequence[Any],
        *,
        fanout: int = 4,
    ) -> None:
        self.tree = BTree(leaves, schema, fanout=fanout)

    def find_leaf(self, key: Sequence[Any]) -> LookupResult:
        return self.tree.lookup_leaf(key)

    def locate(self, key: Sequence[Any]) -> Leaf:
        return self.tree.lookup_leaf(key).leaf


def build_adjacent_separators(
    leaves: Sequence[Leaf],
    schema: KeySchema | Sequence[Any],
) -> list[Separator]:
    """Build the separator between every consecutive pair of leaves."""
    schema = coerce_schema(schema)
    return [
        choose_separator(left.maximum, right.minimum, schema)
        for left, right in zip(leaves, leaves[1:])
    ]


def find_leaf(
    leaves: Sequence[Leaf],
    key: Sequence[Any],
    schema: KeySchema | Sequence[Any],
) -> LookupResult:
    """Scan adjacent separators directly and then check for an exact match."""
    schema = coerce_schema(schema)
    if not leaves:
        raise ValueError("at least one leaf is required")
    separators = [
        choose_separator(left.maximum, right.minimum, schema)
        for left, right in zip(leaves, leaves[1:])
    ]
    leaf_index = len(leaves) - 1
    for index, separator in enumerate(separators):
        if compare_keys(key, separator.comparison_values(), schema) < 0:
            leaf_index = index
            break

    leaf = leaves[leaf_index]
    matching_key = None
    for stored in leaf.keys:
        if compare_keys(key, stored, schema) == 0:
            matching_key = stored
            break
    return LookupResult(
        leaf_index=leaf_index,
        leaf=leaf,
        found=matching_key is not None,
        matching_key=matching_key,
    )
